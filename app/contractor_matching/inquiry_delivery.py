"""Explicitly configured team delivery; construction never sends a message.

Telegram contract: https://core.telegram.org/bots/api#sendmessage
Only an accepted JSON response with a message ID confirms delivery. Unknown
outcomes are never retried here. No endpoint, body, response or credential logs.
"""
from __future__ import annotations

import asyncio
from email.headerregistry import Address
from email.message import EmailMessage
from email.errors import HeaderParseError
import json
import math
import os
import re
import smtplib
import ssl
from time import monotonic

import httpx

from .inquiries import DeliveryRejected


class InquiryDeliveryUnknown(Exception):
    """Sanitized uncertain delivery; the service must not retry automatically."""


class TelegramTransport:
    channel_label = "Telegram команды"

    def __init__(self, token: str, chat_id: str, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 total_timeout: float = 5.0) -> None:
        if (not isinstance(token, str) or not re.fullmatch(r"[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,256}", token)
                or not isinstance(chat_id, str)
                or not re.fullmatch(r"(?:-?[1-9][0-9]{0,19}|@[A-Za-z0-9_]{5,32})", chat_id)):
            raise ValueError("Telegram delivery configuration is invalid")
        if (isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float))
                or not math.isfinite(total_timeout) or not 0 < total_timeout <= 7):
            raise ValueError("Telegram delivery timeout must be finite and between 0 and 7 seconds")
        self._token = token
        self._chat_id = chat_id
        self._transport = transport
        self._total_timeout = float(total_timeout)

    def __repr__(self) -> str:
        return "TelegramTransport(channel_label='Telegram команды', credentials=<redacted>)"

    async def send(self, inquiry_id: str, text: str) -> str:
        if not isinstance(inquiry_id, str) or not re.fullmatch(r"[a-f0-9]{32}", inquiry_id):
            raise DeliveryRejected("Inquiry identifier is invalid")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise DeliveryRejected("Inquiry message length is invalid")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise DeliveryRejected("Inquiry message encoding is invalid") from None
        # A direct public transport call avoids AsyncClient's INFO log containing
        # the request URL. Telegram puts credentials in its URL path. Transport
        # retries, redirects, environment proxies and custom hosts are disabled.
        request = httpx.Request(
            "POST", f"https://api.telegram.org/bot{self._token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text,
                  "link_preview_options": {"is_disabled": True}},
            extensions={"timeout": {"connect": min(2.0, self._total_timeout),
                                    "read": self._total_timeout, "write": self._total_timeout,
                                    "pool": self._total_timeout}},
        )
        try:
            async with asyncio.timeout(self._total_timeout):
                transport = self._transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
                async with transport:
                    response = await transport.handle_async_request(request)
                    try:
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(payload) + len(chunk) > 65_536:
                                raise InquiryDeliveryUnknown("Delivery confirmation is unavailable")
                            payload.extend(chunk)
                    finally:
                        await response.aclose()
            data = json.loads(payload)
        except (TimeoutError, httpx.HTTPError, UnicodeError, ValueError, TypeError):
            raise InquiryDeliveryUnknown("Delivery confirmation is unavailable") from None
        if not isinstance(data, dict):
            raise InquiryDeliveryUnknown("Delivery confirmation is unavailable")
        # A documented error envelope is an explicit rejection. A server error,
        # malformed response or redirect cannot safely prove non-delivery.
        error_code = data.get("error_code")
        if (data.get("ok") is False and type(error_code) is int
                and 400 <= error_code < 500 and 200 <= response.status_code < 500):
            raise DeliveryRejected("Telegram rejected the inquiry")
        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not (200 <= response.status_code < 300 and data.get("ok") is True
                and type(message_id) is int and message_id > 0):
            raise InquiryDeliveryUnknown("Delivery confirmation is unavailable")
        return f"telegram:{message_id}"


class SMTPTransport:
    """Team email via verified TLS; success means SMTP server acceptance only.

    SMTP contract: https://docs.python.org/3/library/smtplib.html
    A timeout cannot cancel a blocking socket operation already in flight. It
    therefore returns an uncertain outcome and never retries the transaction.
    """
    channel_label = "email команды"

    def __init__(self, host: str, port: int, username: str, password: str,
                 sender: str, recipient: str, *, total_timeout: float = 5.0) -> None:
        if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
                or type(port) is not int or port not in {465, 587}
                or not isinstance(username, str) or not username or len(username) > 254
                or any(ord(char) < 32 for char in username)
                or not isinstance(password, str) or not password or len(password) > 2048
                or any(ord(char) < 32 for char in password)):
            raise ValueError("Email delivery configuration is invalid")
        try:
            for address in (sender, recipient):
                if not isinstance(address, str) or len(address) > 254 or not address.isascii():
                    raise ValueError()
                parsed = Address(addr_spec=address)
                if not parsed.username or not parsed.domain or parsed.addr_spec != address:
                    raise ValueError()
        except (ValueError, TypeError, HeaderParseError):
            raise ValueError("Email delivery address configuration is invalid") from None
        if (isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float))
                or not math.isfinite(total_timeout) or not 0 < total_timeout <= 7):
            raise ValueError("Email delivery timeout must be finite and between 0 and 7 seconds")
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._sender, self._recipient = sender, recipient
        self._total_timeout = float(total_timeout)

    def __repr__(self) -> str:
        return "SMTPTransport(channel_label='email команды', configuration=<redacted>)"

    async def send(self, inquiry_id: str, text: str) -> str:
        if not isinstance(inquiry_id, str) or not re.fullmatch(r"[a-f0-9]{32}", inquiry_id):
            raise DeliveryRejected("Inquiry identifier is invalid")
        if not isinstance(text, str) or not 1 <= len(text) <= 16_384:
            raise DeliveryRejected("Inquiry message length is invalid")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise DeliveryRejected("Inquiry message encoding is invalid") from None
        try:
            async with asyncio.timeout(self._total_timeout):
                return await asyncio.to_thread(self._send_blocking, inquiry_id, text)
        except TimeoutError:
            raise InquiryDeliveryUnknown("Delivery confirmation is unavailable") from None

    def _send_blocking(self, inquiry_id: str, text: str) -> str:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = self._recipient
        message["Subject"] = f"Новая заявка сервиса событие. · {inquiry_id}"
        message["Message-ID"] = f"<{inquiry_id}@hackalem.local>"
        # The visitor's contact is body text only; it cannot alter recipients,
        # envelope addresses, Reply-To, Subject or any other mail header.
        message.set_content(text)
        context = ssl.create_default_context()
        deadline = monotonic() + self._total_timeout
        client = None

        def remaining() -> float:
            timeout = deadline - monotonic()
            if timeout <= 0:
                raise TimeoutError()
            if client is not None and client.sock is not None:
                client.sock.settimeout(timeout)
            return timeout

        try:
            if self._port == 465:
                client = smtplib.SMTP_SSL(self._host, self._port, timeout=remaining(), context=context,
                                          local_hostname="hackalem.local")
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=remaining(), local_hostname="hackalem.local")
                remaining()
                client.ehlo()
                remaining()
                client.starttls(context=context)
                remaining()
                client.ehlo()
            # Credentials are sent only after implicit TLS or successful STARTTLS.
            remaining()
            client.login(self._username, self._password)
            remaining()
            refused = client.send_message(message, from_addr=self._sender, to_addrs=[self._recipient])
            if not isinstance(refused, dict):
                raise InquiryDeliveryUnknown("Delivery confirmation is unavailable")
            if refused:
                raise DeliveryRejected("Email server rejected the inquiry")
            return f"smtp:{inquiry_id}"
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryRejected("Email server rejected the inquiry") from None
        except smtplib.SMTPResponseException as error:
            if type(error.smtp_code) is int and 400 <= error.smtp_code < 600:
                raise DeliveryRejected("Email server rejected the inquiry") from None
            raise InquiryDeliveryUnknown("Delivery confirmation is unavailable") from None
        except smtplib.SMTPNotSupportedError:
            raise DeliveryRejected("Email server does not support the required secure configuration") from None
        except (OSError, smtplib.SMTPException):
            raise InquiryDeliveryUnknown("Delivery confirmation is unavailable") from None
        finally:
            # After DATA acceptance, a failed QUIT must not downgrade success.
            # Close the local socket without another network transaction.
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass


def configured_inquiry_transport() -> TelegramTransport | SMTPTransport | None:
    """Require an explicit channel choice; do not autodetect from a stored token."""
    selected = os.getenv("INQUIRY_TRANSPORT", "").strip().lower()
    if selected in {"", "disabled"}:
        return None
    if selected == "smtp":
        names = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "INQUIRY_EMAIL_TO")
        values = [os.getenv(name, "") if name == "SMTP_PASSWORD" else os.getenv(name, "").strip() for name in names]
        if not all(values):
            raise ValueError("Email delivery configuration is incomplete")
        try:
            port = int(values[1])
        except ValueError:
            raise ValueError("Email delivery configuration is invalid") from None
        return SMTPTransport(values[0], port, *values[2:])
    if selected != "telegram":
        raise ValueError("Inquiry delivery configuration is unsupported")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise ValueError("Telegram delivery configuration is incomplete")
    return TelegramTransport(token, chat_id)
