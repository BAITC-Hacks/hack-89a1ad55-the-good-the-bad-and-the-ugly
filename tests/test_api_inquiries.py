from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest

from contractor_matching import api
from contractor_matching.data_loader import load_profiles
from contractor_matching.filters import filter_contractors
from contractor_matching.models import SearchRequest
from contractor_matching.presets import BASE

DATA = Path(__file__).resolve().parents[1] / "data"
CONTACT = "private-customer@example.com"
NAME = "Приватное Имя"
COMMENT = "Личное сообщение для команды"


class FakeTransport:
    channel_label = "Тестовый канал"

    def __init__(self, error=None):
        self.messages = []
        self.error = error

    async def send(self, inquiry_id, text):
        self.messages.append((inquiry_id, text))
        if self.error:
            raise self.error
        return "accepted-test-message"


@pytest.fixture(autouse=True)
def no_external_network_or_dotenv(monkeypatch):
    # Local API tests must never use a user's provider settings or send messages.
    monkeypatch.setattr(api, "load_project_env", lambda _: None)

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("Unexpected external HTTP in inquiry integration test")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_network)


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(api, "configured_inquiry_transport", lambda *args, **kwargs: transport)
    database = tmp_path / "private-inquiries.sqlite3"
    app = api.create_app(DATA, tmp_path / "explanations.sqlite3", inquiry_db_path=database)
    with TestClient(app) as client:
        yield client, transport, database


def payload(**changes):
    eligible = filter_contractors(load_profiles(DATA), SearchRequest.model_validate(BASE)).survivors
    real = next(profile for profile in eligible if not profile.synthetic)
    return {
        "contractor_id": real.id, "search": dict(BASE), "name": NAME,
        "contact": CONTACT, "message": COMMENT, "consent": True, **changes,
    }


def post(client, body=None, key="inquiry-api-key-0001", **kwargs):
    return client.post(
        "/api/inquiries", json=payload() if body is None else body,
        headers={"Idempotency-Key": key, **kwargs.pop("headers", {})}, **kwargs,
    )


def assert_private(response):
    assert CONTACT not in response.text
    assert NAME not in response.text
    assert COMMENT not in response.text
    assert "traceback" not in response.text.lower()


def stored_count(database):
    if not database.exists():
        return 0
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return connection.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0] if "inquiries" in tables else 0


def test_disabled_channel_is_honest_and_does_not_save_contacts(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "configured_inquiry_transport", lambda *args, **kwargs: None)
    database = tmp_path / "disabled.sqlite3"
    with TestClient(api.create_app(DATA, tmp_path / "cache.sqlite3", inquiry_db_path=database)) as client:
        meta = client.get("/api/meta").json()
        assert meta["inquiries"]["enabled"] is False
        response = post(client)
        assert response.status_code == 503
        assert response.json()["message"]
        assert_private(response)
    assert stored_count(database) == 0


def test_happy_path_idempotency_and_authenticated_private_receipt(enabled):
    client, transport, database = enabled
    meta = client.get("/api/meta").json()["inquiries"]
    assert meta["enabled"] is True
    assert meta["channel_label"] == "Тестовый канал"
    first = post(client, headers={"Origin": "http://testserver"})
    assert first.status_code in (200, 201), first.text
    receipt = first.json()
    assert receipt["status"] == "delivered_to_team"
    assert "не бронирование" in receipt["message"]
    assert first.headers["Cache-Control"] == "no-store"
    assert_private(first)
    same = post(client)
    new_key = post(client, key="inquiry-api-key-0002")
    assert same.json() == new_key.json() == receipt
    assert len(transport.messages) == stored_count(database) == 1
    assert CONTACT in transport.messages[0][1]
    response = client.get(
        f"/api/inquiries/{receipt['id']}",
        headers={"Authorization": f"Bearer {receipt['receipt_token']}"},
    )
    assert response.status_code == 200
    assert response.json() == receipt
    assert_private(response)
    wrong = client.get(f"/api/inquiries/{receipt['id']}", headers={"Authorization": "Bearer " + "0" * 64})
    assert wrong.status_code == 404
    assert_private(wrong)
    missing = client.get("/api/inquiries/" + "0" * 32, headers={"Authorization": f"Bearer {receipt['receipt_token']}"})
    assert missing.status_code == 404
    assert missing.json() == wrong.json()


def test_different_payload_with_used_key_is_conflict_without_resending(enabled):
    client, transport, database = enabled
    assert post(client).status_code in (200, 201)
    response = post(client, payload(message="Изменённые условия"))
    assert response.status_code == 409
    assert_private(response)
    assert len(transport.messages) == stored_count(database) == 1


def test_synthetic_contractor_cannot_receive_real_inquiry(enabled):
    client, transport, database = enabled
    response = post(client, payload(
        contractor_id="TEAM-DEC-AST-01",
        search={**BASE, "city": "Астана", "category": "Декоратор", "event_format": "свадьба", "date": "2026-10-05", "budget": 3_000_000},
    ))
    assert response.status_code == 422
    assert_private(response)
    assert not transport.messages and stored_count(database) == 0


@pytest.mark.parametrize("changes", [
    {"consent": False}, {"consent": 1}, {"contact": "private-invalid-contact"},
    {"name": " "}, {"message": "x" * 2001}, {"contractor_id": "MISSING"},
    {"search": {**BASE, "budget": 1}},
])
def test_bad_requests_do_not_echo_personal_data_or_send(enabled, changes):
    client, transport, database = enabled
    response = post(client, payload(**changes))
    assert response.status_code == 422
    assert response.json()["message"]
    assert_private(response)
    assert "private-invalid-contact" not in response.text
    assert not transport.messages and stored_count(database) == 0


def test_unknown_json_field_names_are_not_reflected_as_personal_data(enabled):
    client, transport, database = enabled
    response = post(client, {**payload(), CONTACT: COMMENT})
    assert response.status_code == 422
    assert_private(response)
    assert not transport.messages and stored_count(database) == 0


def test_foreign_origin_is_rejected_before_any_inquiry_is_persisted(enabled):
    client, transport, database = enabled
    response = post(client, headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    assert_private(response)
    assert not transport.messages and stored_count(database) == 0


def test_host_spoof_cannot_make_foreign_origin_look_local(enabled):
    client, transport, database = enabled
    response = post(client, headers={"Host": "untrusted.example", "Origin": "http://untrusted.example"})
    assert response.status_code == 400
    assert_private(response)
    assert not transport.messages and stored_count(database) == 0


@pytest.mark.parametrize("path_value", ["app/static/private-inquiries.sqlite3", str(api.STATIC_DIR / "private-inquiries.sqlite3")])
def test_inquiry_db_cannot_be_placed_in_public_static_directory(tmp_path, monkeypatch, path_value):
    database = api.STATIC_DIR / "private-inquiries.sqlite3"
    assert not database.exists()
    monkeypatch.setenv("INQUIRY_DB_PATH", path_value)
    with pytest.raises(ValueError, match="outside the public static directory"):
        api.create_app(DATA, tmp_path / "explanations.sqlite3")
    assert not database.exists()


def test_public_hostname_requires_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "demo.example")
    monkeypatch.setattr(api, "configured_inquiry_transport", lambda *args, **kwargs: None)
    app = api.create_app(DATA, tmp_path / "explanations.sqlite3", tmp_path / "inquiries.sqlite3")
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 400
        assert client.get("/health/ready", headers={"Host": "demo.example"}).status_code == 200


@pytest.mark.parametrize("value", ["*", "*.example.org", "", "https://example.org"])
def test_wildcard_or_invalid_allowed_hosts_are_rejected(value, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ALLOWED_HOSTS", value)
    with pytest.raises(ValueError, match="Invalid APP_ALLOWED_HOSTS"):
        api.create_app(DATA, tmp_path / "explanations.sqlite3", tmp_path / "inquiries.sqlite3")


@pytest.mark.parametrize("streamed", [False, True])
def test_actual_body_size_is_limited_even_without_content_length(enabled, streamed):
    client, transport, database = enabled
    large_body = json.dumps(payload(message="x" * 17_000)).encode()
    content = iter([large_body[:8000], large_body[8000:]]) if streamed else large_body
    response = client.post(
        "/api/inquiries", content=content,
        headers={"Content-Type": "application/json", "Idempotency-Key": "inquiry-api-key-0001"},
    )
    assert response.status_code == 413
    assert_private(response)
    assert not transport.messages and stored_count(database) == 0


def test_non_json_submission_is_not_accepted(enabled):
    client, transport, database = enabled
    response = client.post(
        "/api/inquiries", content=json.dumps(payload()),
        headers={"Content-Type": "text/plain", "Idempotency-Key": "inquiry-api-key-0001"},
    )
    assert response.status_code == 415
    assert_private(response)
    assert not transport.messages and stored_count(database) == 0


def test_inquiry_rate_limit_does_not_trust_forwarded_for(enabled):
    client, transport, database = enabled
    responses = [
        post(client, payload(message=f"Заявка {index}"), key=f"inquiry-api-key-{index:04d}")
        for index in range(11)
    ]
    assert all(response.status_code in (200, 201) for response in responses[:10])
    assert responses[10].status_code == 429
    assert responses[10].headers.get("Retry-After")
    bypass = post(
        client, payload(message="Ещё одна заявка"), key="inquiry-api-key-9999",
        headers={"X-Forwarded-For": "203.0.113.22"},
    )
    assert bypass.status_code == 429
    assert len(transport.messages) == stored_count(database) == 10


def test_no_public_inquiry_listing_or_database_download(enabled):
    client, _, _ = enabled
    post(client)
    for path in ("/api/inquiries", "/private-inquiries.sqlite3", "/static/private-inquiries.sqlite3"):
        response = client.get(path)
        assert response.status_code in (404, 405)
        assert_private(response)


def test_unknown_delivery_is_not_reported_as_sent_or_retried(enabled):
    client, transport, database = enabled
    transport.error = TimeoutError("PRIVATE-PROVIDER-SECRET " + CONTACT)
    response = post(client)
    assert response.status_code in (200, 201)
    assert response.json()["status"] == "delivery_unknown"
    assert "PRIVATE-PROVIDER-SECRET" not in response.text
    assert_private(response)
    repeat = post(client, key="inquiry-api-key-0002")
    assert repeat.json() == response.json()
    assert len(transport.messages) == stored_count(database) == 1
