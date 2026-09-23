import asyncio
import sqlite3

import pytest
from pydantic import ValidationError

from contractor_matching.inquiries import (
    DeliveryRejected, InquiryConflict, InquiryError, InquiryNotFound,
    InquiryRequest, InquiryService, InquiryUnavailable,
)
from contractor_matching.models import ContractorProfile


def profile(**changes):
    return ContractorProfile.model_validate({
        "id": "TEST-01", "anon_name": "Тестовый подрядчик", "city": "Алматы",
        "categories": ["Ведущий"], "price_from_kzt": 1_000_000,
        "event_formats": ["корпоратив"], "languages": ["русский"], "max_hours": 6,
        "busy_dates": [], "description": "Тестовый профиль.", **changes,
    })


def payload(**changes):
    return InquiryRequest.model_validate({
        "contractor_id": "TEST-01",
        "search": {"city": "Алматы", "category": "Ведущий", "date": "2026-10-04",
                   "event_format": "корпоратив", "budget": 1_000_000, "duration": 6, "language": "русский"},
        "name": "Тест Клиент", "contact": "client@example.com", "message": "Уточните условия.", "consent": True,
        **changes,
    })


class Transport:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    async def send(self, inquiry_id, text):
        self.messages.append((inquiry_id, text))
        if self.error is not None:
            raise self.error
        return "team-message-123"


def service(tmp_path, transport=None, **kwargs):
    return InquiryService(tmp_path / "private" / "inquiries.sqlite3", [profile()], transport, **kwargs)


def run(coro):
    return asyncio.run(coro)


def count_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]


def test_unconfigured_channel_rejects_without_storing_personal_data(tmp_path):
    app = service(tmp_path)
    with pytest.raises(InquiryUnavailable, match="не сохранены"):
        run(app.submit(payload(), "test-key-00000001"))
    assert count_rows(app.db_path) == 0
    assert not app.delivery_configured


def test_explicit_local_draft_does_not_claim_or_attempt_delivery(tmp_path):
    app = service(tmp_path, allow_drafts=True)
    receipt = run(app.submit(payload(), "test-key-00000001"))
    assert receipt.status == "awaiting_channel"
    assert "не отправлена" in receipt.message
    assert run(app.retry_pending()) == 0
    assert app.get_receipt(receipt.id, receipt.receipt_token) == receipt


def test_real_inquiry_delivers_full_context_and_private_receipt(tmp_path, caplog):
    transport = Transport()
    app = service(tmp_path, transport)
    receipt = run(app.submit(payload(), "test-key-00000001"))
    assert receipt.status == "delivered_to_team"
    assert "не бронирование" in receipt.message
    assert len(transport.messages) == 1
    text = transport.messages[0][1]
    for expected in ["TEST-01", "Алматы", "Ведущий", "2026-10-04", "корпоратив", "русский", "client@example.com", "Уточните условия.", "не бронирование"]:
        assert expected in text
    serialized = receipt.model_dump_json()
    assert "client@example.com" not in serialized
    assert "Тест Клиент" not in serialized
    assert "Уточните условия." not in serialized
    assert "client@example.com" not in caplog.text


def test_same_key_and_new_key_for_identical_payload_never_resend(tmp_path):
    transport = Transport()
    app = service(tmp_path, transport)
    first = run(app.submit(payload(), "test-key-00000001"))
    assert run(app.submit(payload(), "test-key-00000001")) == first
    assert run(app.submit(payload(), "test-key-00000002")) == first
    assert len(transport.messages) == count_rows(app.db_path) == 1
    with pytest.raises(InquiryConflict):
        run(app.submit(payload(message="Другие условия"), "test-key-00000001"))
    assert len(transport.messages) == 1


def test_receipt_and_idempotency_survive_service_restart(tmp_path):
    transport = Transport()
    first_app = service(tmp_path, transport)
    first = run(first_app.submit(payload(), "test-key-00000001"))
    second_app = service(tmp_path, Transport())
    assert second_app.get_receipt(first.id, first.receipt_token) == first
    assert run(second_app.submit(payload(), "test-key-00000001")) == first
    assert second_app.transport.messages == []
    with sqlite3.connect(first_app.db_path) as connection:
        token_hash = connection.execute("SELECT receipt_token_hash FROM inquiries").fetchone()[0]
    assert token_hash != first.receipt_token


def test_wrong_token_and_unknown_id_do_not_reveal_existence_or_personal_data(tmp_path):
    app = service(tmp_path, Transport())
    receipt = run(app.submit(payload(), "test-key-00000001"))
    messages = []
    for inquiry_id, token in [(receipt.id, "0" * 64), ("0" * 32, receipt.receipt_token), ("bad-id", "bad-token")]:
        with pytest.raises(InquiryNotFound) as error:
            app.get_receipt(inquiry_id, token)
        messages.append(str(error.value))
    assert len(set(messages)) == 1


@pytest.mark.parametrize("changes", [
    {"synthetic": True}, {"busy_dates": ["2026-10-04"]}, {"price_from_kzt": 2_000_000},
    {"city": "Астана"}, {"categories": ["Фотограф"]}, {"event_formats": ["свадьба"]},
    {"max_hours": 5}, {"languages": ["казахский"]},
])
def test_real_submission_rejects_synthetic_and_every_hard_filter_failure(tmp_path, changes):
    transport = Transport()
    app = InquiryService(tmp_path / "inquiries.sqlite3", [profile(**changes)], transport)
    with pytest.raises(InquiryError):
        run(app.submit(payload(), "test-key-00000001"))
    assert count_rows(app.db_path) == 0
    assert transport.messages == []


def test_unknown_contractor_is_rejected(tmp_path):
    app = service(tmp_path, Transport())
    with pytest.raises(InquiryError, match="не найден"):
        run(app.submit(payload(contractor_id="MISSING"), "test-key-00000001"))
    assert count_rows(app.db_path) == 0


@pytest.mark.parametrize("consent", [False, 0, 1, "true", None])
def test_consent_must_be_explicit_boolean_true(consent):
    with pytest.raises(ValidationError):
        payload(consent=consent)


@pytest.mark.parametrize("changes", [
    {"name": " "}, {"name": "A" * 101}, {"contact": "нет контакта"},
    {"contact": "a@example.com\nInjected"}, {"name": "Name\x00"},
    {"message": "M" * 2001}, {"contact": "@abc"}, {"contact": "12345"},
])
def test_personal_data_is_bounded_plain_text_with_recognizable_contact(changes):
    with pytest.raises(ValidationError):
        payload(**changes)


@pytest.mark.parametrize("contact", ["hello@example.com", "+7 (701) 123-45-67", "@team_user"])
def test_supported_contact_forms(contact):
    assert payload(contact=contact).contact == contact


@pytest.mark.parametrize("key", ["short", "bad key" * 3, "a" * 129, None])
def test_invalid_idempotency_key_does_not_store_request(tmp_path, key):
    app = service(tmp_path, Transport())
    with pytest.raises(InquiryError):
        run(app.submit(payload(), key))
    assert count_rows(app.db_path) == 0


@pytest.mark.parametrize("error,expected", [
    (DeliveryRejected("provider rejected"), "delivery_failed"),
    (TimeoutError("client@example.com SECRET"), "delivery_unknown"),
    (RuntimeError("provider may have accepted"), "delivery_unknown"),
])
def test_transport_failure_is_honest_private_and_not_retried(tmp_path, caplog, error, expected):
    transport = Transport(error)
    app = service(tmp_path, transport)
    receipt = run(app.submit(payload(), "test-key-00000001"))
    assert receipt.status == expected
    assert run(app.retry_pending()) == 0
    assert run(app.submit(payload(), "test-key-00000002")) == receipt
    assert len(transport.messages) == 1
    assert "SECRET" not in receipt.model_dump_json()
    assert "SECRET" not in caplog.text


def test_concurrent_identical_submissions_attempt_delivery_only_once(tmp_path):
    class SlowTransport(Transport):
        async def send(self, inquiry_id, text):
            self.messages.append((inquiry_id, text))
            await asyncio.sleep(0)
            return "accepted"

    transport = SlowTransport()
    app = service(tmp_path, transport)

    async def concurrent():
        return await asyncio.gather(
            app.submit(payload(), "test-key-00000001"),
            app.submit(payload(), "test-key-00000002"),
        )

    receipts = run(concurrent())
    assert len({receipt.id for receipt in receipts}) == 1
    assert len({receipt.receipt_token for receipt in receipts}) == 1
    assert len(transport.messages) == 1
    assert app.get_receipt(receipts[0].id, receipts[0].receipt_token).status == "delivered_to_team"


def test_interrupted_delivery_stays_unknown_after_restart_and_never_resends(tmp_path):
    class InterruptedTransport(Transport):
        async def send(self, inquiry_id, text):
            raise asyncio.CancelledError()

    app = service(tmp_path, InterruptedTransport())
    with pytest.raises(asyncio.CancelledError):
        run(app.submit(payload(), "test-key-00000001"))
    restarted = service(tmp_path, Transport())
    receipt = run(restarted.submit(payload(), "test-key-00000001"))
    assert receipt.status == "delivery_unknown"
    assert run(restarted.retry_pending()) == 0
    assert restarted.transport.messages == []


def test_service_deadline_bounds_a_hanging_transport_and_prevents_retry(tmp_path):
    class HangingTransport(Transport):
        async def send(self, inquiry_id, text):
            self.messages.append((inquiry_id, text))
            await asyncio.Event().wait()

    transport = HangingTransport()
    app = service(tmp_path, transport, delivery_timeout_seconds=0.01)
    receipt = run(app.submit(payload(), "test-key-00000001"))
    assert receipt.status == "delivery_unknown"
    assert run(app.retry_pending()) == 0
    assert len(transport.messages) == 1


def test_pending_outbox_entries_are_only_sent_once_and_drafts_are_not_promoted(tmp_path):
    app = service(tmp_path, allow_drafts=True)
    first = run(app.submit(payload(), "test-key-00000001"))
    second = run(app.submit(payload(message="Другая заявка"), "test-key-00000002"))
    # Simulate a process exit after a configured request was committed but before
    # any provider call started; only this pending state can safely be retried.
    with sqlite3.connect(app.db_path) as connection:
        connection.execute("UPDATE inquiries SET status = 'pending_delivery' WHERE id = ?", (first.id,))
    transport = Transport()
    restarted = service(tmp_path, transport)
    assert run(restarted.retry_pending()) == 1
    assert run(restarted.retry_pending()) == 0
    assert len(transport.messages) == 1
    assert restarted.get_receipt(first.id, first.receipt_token).status == "delivered_to_team"
    assert restarted.get_receipt(second.id, second.receipt_token).status == "awaiting_channel"
