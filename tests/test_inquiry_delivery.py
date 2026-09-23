"""Telegram transport tests are mock-only and never send messages."""
import asyncio
import json
import logging
import smtplib
import ssl

import httpx
import pytest

from contractor_matching.inquiries import DeliveryRejected
from contractor_matching.inquiry_delivery import (
    InquiryDeliveryUnknown, SMTPTransport, TelegramTransport, configured_inquiry_transport,
)

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456789"
CHAT = "-1001234567890"
INQUIRY = "a" * 32


def run(handler, text="Plain inquiry"):
    adapter = TelegramTransport(TOKEN, CHAT, transport=httpx.MockTransport(handler))
    return asyncio.run(adapter.send(INQUIRY, text))


def test_success_has_literal_body_fixed_url_no_parse_mode_or_logging(caplog):
    caplog.set_level(logging.DEBUG)
    malicious = "<b>Keep literal</b> [link](https://example.invalid) $HOME\nКонтакт: +77770000000"
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload == {"chat_id": CHAT, "text": malicious, "link_preview_options": {"is_disabled": True}}
        assert "parse_mode" not in payload
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 73}})

    assert run(handler, malicious) == "telegram:73"
    assert len(requests) == 1
    assert TOKEN not in caplog.text
    assert CHAT not in caplog.text
    assert malicious not in caplog.text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_explicit_rejections_are_not_mistaken_for_delivery(status):
    def handler(request):
        return httpx.Response(status, json={"ok": False, "error_code": status, "description": TOKEN + " private body"})
    with pytest.raises(DeliveryRejected) as caught:
        run(handler)
    assert TOKEN not in repr(caught.value)
    assert "private body" not in str(caught.value)


@pytest.mark.parametrize("status,payload", [
    (500, {"ok": False, "error_code": 500}),
    (503, {"ok": True, "result": {"message_id": 73}}),
    (200, {"ok": True, "result": {}}),
    (200, {"ok": True, "result": {"message_id": True}}),
    (200, {"ok": True, "result": {"message_id": 0}}),
    (200, {"ok": "true", "result": {"message_id": 73}}),
    (200, []),
    (302, {"ok": True, "result": {"message_id": 73}}),
])
def test_server_errors_or_missing_confirmation_are_uncertain(status, payload):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=payload, headers={"Location": "https://example.invalid/"})
    with pytest.raises(InquiryDeliveryUnknown):
        run(handler)
    assert len(requests) == 1


@pytest.mark.parametrize("status,body", [(200, b"not json"), (400, b"not an API envelope"), (200, b"x" * 65_537)],
                         ids=["malformed-success", "malformed-rejection", "oversized"])
def test_malformed_or_oversized_response_is_uncertain(status, body):
    with pytest.raises(InquiryDeliveryUnknown):
        run(lambda request: httpx.Response(status, content=body))


def test_timeout_has_no_retry_or_token_in_error(caplog):
    caplog.set_level(logging.DEBUG)
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(f"Secret endpoint: {request.url}", request=request)
    with pytest.raises(InquiryDeliveryUnknown) as caught:
        run(handler)
    assert len(calls) == 1
    assert TOKEN not in repr(caught.value)
    assert TOKEN not in caplog.text
    assert caught.value.__suppress_context__ is True


def test_total_deadline_is_uncertain():
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    adapter = TelegramTransport(TOKEN, CHAT, transport=httpx.MockTransport(handler), total_timeout=0.001)
    with pytest.raises(InquiryDeliveryUnknown):
        asyncio.run(adapter.send(INQUIRY, "test"))


def test_bad_message_is_rejected_before_transport_call():
    def forbidden(request):
        raise AssertionError("Must not send")
    for value in ("", "x" * 4097, "invalid\ud800"):
        with pytest.raises(DeliveryRejected):
            run(forbidden, value)


def test_absent_channel_is_disabled_even_if_token_exists(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert configured_inquiry_transport() is None


def test_configured_channel_has_safe_repr_and_label(monkeypatch):
    monkeypatch.setenv("INQUIRY_TRANSPORT", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    adapter = configured_inquiry_transport()
    assert adapter.channel_label == "Telegram команды"
    assert TOKEN not in repr(adapter)
    assert CHAT not in repr(adapter)


@pytest.mark.parametrize("transport,token,chat", [
    ("telegram", "", ""), ("telegram", TOKEN, ""),
    ("telegram", "private-invalid-token", CHAT),
    ("telegram", TOKEN + "/bad/path", CHAT),
    ("telegram", TOKEN, "secret destination with spaces"),
    ("private-unsupported-value", TOKEN, CHAT),
])
def test_invalid_configuration_errors_are_sanitized(monkeypatch, transport, token, chat):
    monkeypatch.setenv("INQUIRY_TRANSPORT", transport)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)
    with pytest.raises(ValueError) as caught:
        configured_inquiry_transport()
    for secret in (TOKEN, "private-invalid-token", "secret destination", "private-unsupported-value"):
        assert secret not in str(caught.value)


def smtp_adapter(port=465, **changes):
    values = {"host": "smtp.example.invalid", "port": port, "username": "sender@example.invalid",
              "password": "private-smtp-password", "sender": "sender@example.invalid", "recipient": "team@example.invalid"}
    return SMTPTransport(**{**values, **changes})


class FakeSocket:
    def settimeout(self, timeout):
        assert 0 < timeout <= 5


class FakeSMTP:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.calls = []
        self.sock = FakeSocket()
        self.error = None
        self.refused = {}

    def ehlo(self):
        self.calls.append("ehlo")
        return 250, b"ok"

    def starttls(self, *, context):
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
        self.calls.append("starttls")

    def login(self, username, password):
        assert username == "sender@example.invalid"
        assert password == "private-smtp-password"
        self.calls.append("login")

    def send_message(self, message, **kwargs):
        self.calls.append("send_message")
        self.message, self.envelope = message, kwargs
        if self.error:
            raise self.error
        return self.refused

    def close(self):
        self.calls.append("close")


@pytest.mark.parametrize("port", [465, 587])
def test_smtp_verified_tls_and_literal_contact_body(monkeypatch, caplog, port):
    caplog.set_level(logging.DEBUG)
    instances = []
    def factory(*args, **kwargs):
        client = FakeSMTP(*args, **kwargs)
        instances.append(client)
        return client
    monkeypatch.setattr(smtplib, "SMTP_SSL" if port == 465 else "SMTP", factory)
    text = "Контакт: visitor@example.invalid\nBcc: attacker@example.invalid\n<b>literal text</b>"
    adapter = smtp_adapter(port)
    assert asyncio.run(adapter.send(INQUIRY, text)) == f"smtp:{INQUIRY}"
    client = instances[0]
    assert client.args == ("smtp.example.invalid", port)
    assert 0 < client.kwargs["timeout"] <= 5
    if port == 465:
        context = client.kwargs["context"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert client.calls == ["login", "send_message", "close"]
    else:
        assert client.calls == ["ehlo", "starttls", "ehlo", "login", "send_message", "close"]
    assert client.envelope == {"from_addr": "sender@example.invalid", "to_addrs": ["team@example.invalid"]}
    assert client.message["To"] == "team@example.invalid"
    assert client.message["Bcc"] is None and client.message["Reply-To"] is None
    assert text in client.message.get_content()
    assert "private-smtp-password" not in repr(adapter) + caplog.text
    assert "team@example.invalid" not in repr(adapter) + caplog.text
    assert text not in caplog.text


@pytest.mark.parametrize("error", [
    smtplib.SMTPDataError(451, b"private failure"),
    smtplib.SMTPDataError(550, b"private failure"),
    smtplib.SMTPAuthenticationError(535, b"private failure"),
    smtplib.SMTPRecipientsRefused({"team@example.invalid": (550, b"private failure")}),
], ids=["temporary-rejection", "permanent-rejection", "authentication-rejection", "recipient-rejection"])
def test_smtp_definitive_rejection_is_sanitized(monkeypatch, error):
    client = FakeSMTP()
    client.error = error
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: client)
    with pytest.raises(DeliveryRejected) as caught:
        asyncio.run(smtp_adapter().send(INQUIRY, "test"))
    assert "private failure" not in str(caught.value)
    assert "team@example.invalid" not in str(caught.value)
    assert client.calls.count("send_message") == 1


@pytest.mark.parametrize("error", [
    smtplib.SMTPServerDisconnected("private-smtp-password"),
    TimeoutError("private-smtp-password"),
    OSError("private-smtp-password"),
], ids=["disconnect", "timeout", "socket-error"])
def test_smtp_ambiguous_outcome_is_not_retried(monkeypatch, error):
    client = FakeSMTP()
    client.error = error
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: client)
    with pytest.raises(InquiryDeliveryUnknown) as caught:
        asyncio.run(smtp_adapter().send(INQUIRY, "test"))
    assert "private-smtp-password" not in repr(caught.value)
    assert client.calls.count("send_message") == 1


def test_smtp_empty_configuration_is_disabled_or_rejected_without_network(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: pytest.fail("No network during configuration"))
    monkeypatch.setenv("INQUIRY_TRANSPORT", "")
    assert configured_inquiry_transport() is None
    monkeypatch.setenv("INQUIRY_TRANSPORT", "smtp")
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "INQUIRY_EMAIL_TO"):
        monkeypatch.setenv(name, "")
    with pytest.raises(ValueError, match="incomplete"):
        configured_inquiry_transport()


def test_smtp_environment_factory_preserves_literal_password(monkeypatch):
    config = {"INQUIRY_TRANSPORT": "smtp", "SMTP_HOST": "smtp.example.invalid", "SMTP_PORT": "587",
              "SMTP_USERNAME": "sender@example.invalid", "SMTP_PASSWORD": " literal password ",
              "SMTP_FROM": "sender@example.invalid", "INQUIRY_EMAIL_TO": "team@example.invalid"}
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    adapter = configured_inquiry_transport()
    assert adapter.channel_label == "email команды"
    assert adapter._password == " literal password "


@pytest.mark.parametrize("changes", [
    {"port": 25}, {"port": True}, {"host": "smtp.invalid/path"},
    {"sender": "from@example.invalid\r\nBcc: attacker@example.invalid"},
    {"recipient": "a@example.invalid, b@example.invalid"},
    {"recipient": "not-an-email"}, {"username": "private\nusername"},
], ids=["plaintext-port", "bool-port", "host-path", "header-injection", "recipient-list", "invalid-address", "username-newline"])
def test_smtp_bad_configuration_is_sanitized(changes):
    with pytest.raises(ValueError) as caught:
        smtp_adapter(**changes)
    assert "attacker" not in str(caught.value)
    assert "username" not in str(caught.value)


def test_smtp_no_downgrade_or_login_if_starttls_fails(monkeypatch):
    client = FakeSMTP()
    def unsupported(*, context):
        client.calls.append("starttls")
        raise smtplib.SMTPNotSupportedError("private provider details")
    client.starttls = unsupported
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: client)
    with pytest.raises(DeliveryRejected):
        asyncio.run(smtp_adapter(587).send(INQUIRY, "test"))
    assert "login" not in client.calls and "send_message" not in client.calls


def test_smtp_post_acceptance_close_error_does_not_lose_confirmation(monkeypatch):
    client = FakeSMTP()
    def close_error():
        raise OSError("close failed")
    client.close = close_error
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: client)
    assert asyncio.run(smtp_adapter().send(INQUIRY, "test")) == f"smtp:{INQUIRY}"
