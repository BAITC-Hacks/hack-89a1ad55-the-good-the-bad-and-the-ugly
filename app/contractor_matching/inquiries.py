"""Private, durable team inquiries with explicit consent and delivery receipts.

A delivered inquiry means only that the configured team channel accepted the
message. It is never a reservation, a confirmed quote, or confirmed availability.
Unknown delivery outcomes are deliberately excluded from retries: a local
outbox cannot provide exactly-once delivery by an external provider.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Iterator, Literal, Protocol
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .filters import filter_contractors
from .models import ContractorProfile, SearchRequest


class InquiryError(ValueError):
    """An expected inquiry error; messages must not contain submitted PII."""


class InquiryUnavailable(InquiryError):
    pass


class InquiryConflict(InquiryError):
    pass


class InquiryNotFound(InquiryError):
    pass


class DeliveryRejected(Exception):
    """Transport guarantees that the provider did not accept the message."""


class InquiryTransport(Protocol):
    async def send(self, inquiry_id: str, text: str) -> str | None:
        """Return a provider reference only after explicit provider acceptance.

        Raise DeliveryRejected only when non-delivery is certain. Any other
        exception is an uncertain outcome and must never trigger automatic retry.
        The transport must set its own finite connection and request timeouts.
        """


class InquiryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contractor_id: str = Field(min_length=1, max_length=100, strict=True)
    search: SearchRequest
    name: str = Field(min_length=2, max_length=100, strict=True)
    contact: str = Field(min_length=3, max_length=254, strict=True)
    message: str = Field(default="", max_length=2000, strict=True)
    consent: Literal[True]

    @field_validator("consent", mode="before")
    @classmethod
    def explicit_consent(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Необходимо явное согласие на передачу контактов команде")
        return True

    @field_validator("contractor_id", "name", "contact", "message", mode="before")
    @classmethod
    def bounded_plain_text(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Ожидается текст")
        value = value.strip()
        allowed_controls = {"\n", "\t"} if info.field_name == "message" else set()
        if any((ord(char) < 32 and char not in allowed_controls) or ord(char) == 127 for char in value):
            raise ValueError("Недопустимый управляющий символ")
        return value

    @field_validator("contact")
    @classmethod
    def recognizable_contact(cls, value: str) -> str:
        if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value):
            return value
        if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", value):
            return value
        if re.fullmatch(r"\+?[0-9][0-9 ().-]*[0-9]", value):
            if 7 <= len(re.sub(r"\D", "", value)) <= 15:
                return value
        raise ValueError("Укажите email, телефон или Telegram @username")


InquiryStatus = Literal[
    "awaiting_channel", "pending_delivery", "delivered_to_team",
    "delivery_failed", "delivery_unknown",
]


class InquiryReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    receipt_token: str
    status: InquiryStatus
    message: str
    created_at: str


_MESSAGES = {
    "awaiting_channel": "Черновик сохранён. Канал команды не настроен, заявка не отправлена. Это не бронирование.",
    "pending_delivery": "Заявка сохранена и ожидает передачи команде. Это не бронирование.",
    "delivered_to_team": "Канал команды принял заявку. Получение и прочтение командой не подтверждены. Подрядчик, доступность и итоговая цена ещё не согласованы; это не бронирование.",
    "delivery_failed": "Заявка сохранена, но не передана команде. Условия или канал отправки нужно проверить. Это не бронирование.",
    "delivery_unknown": "Подтверждение отправки не получено. Заявка могла дойти до команды; повторная отправка отключена, чтобы избежать дубля. Это не бронирование.",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class InquiryService:
    """SQLite outbox; keep db_path outside static assets and version control.

    No transport means submissions are disabled by default, before PII is stored.
    allow_drafts=True is an explicit local-only opt-in for a future draft feature.
    The service exposes neither an inquiry list nor submitted contact details.
    """

    def __init__(
        self,
        db_path: Path,
        profiles: list[ContractorProfile],
        transport: InquiryTransport | None = None,
        *,
        allow_drafts: bool = False,
        delivery_timeout_seconds: float = 7.0,
    ) -> None:
        if (
            isinstance(delivery_timeout_seconds, bool)
            or not isinstance(delivery_timeout_seconds, (int, float))
            or not math.isfinite(delivery_timeout_seconds)
            or not 0 < delivery_timeout_seconds <= 30
        ):
            raise ValueError("delivery_timeout_seconds must be finite and between 0 and 30")
        self.db_path = Path(db_path)
        self.transport = transport
        self.allow_drafts = allow_drafts
        self.delivery_timeout_seconds = float(delivery_timeout_seconds)
        self._profiles = {profile.id: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("Duplicate contractor IDs")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS inquiry_settings (
                    key TEXT PRIMARY KEY,
                    value BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inquiries (
                    id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    receipt_token_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    provider_reference TEXT
                );
                CREATE TABLE IF NOT EXISTS inquiry_keys (
                    key_hash TEXT PRIMARY KEY,
                    inquiry_id TEXT NOT NULL REFERENCES inquiries(id),
                    payload_hash TEXT NOT NULL
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO inquiry_settings(key, value) VALUES ('receipt_secret', ?)",
                (secrets.token_bytes(32),),
            )
            connection.commit()
            self._receipt_secret = bytes(connection.execute(
                "SELECT value FROM inquiry_settings WHERE key = 'receipt_secret'",
            ).fetchone()[0])
        # On POSIX this limits the file to its owner. Windows deployments should
        # additionally restrict the containing directory through normal ACLs.
        self.db_path.chmod(0o600)

    @property
    def delivery_configured(self) -> bool:
        return self.transport is not None

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _token(self, inquiry_id: str) -> str:
        return hmac.new(self._receipt_secret, inquiry_id.encode("ascii"), hashlib.sha256).hexdigest()

    def _receipt(self, row: sqlite3.Row) -> InquiryReceipt:
        # A crash during a provider call leaves 'delivering'. It is uncertain,
        # not an invitation to retry an external side effect.
        status = "delivery_unknown" if row["status"] == "delivering" else row["status"]
        return InquiryReceipt(
            id=row["id"], receipt_token=self._token(row["id"]), status=status,
            message=_MESSAGES[status], created_at=row["created_at"],
        )

    def _validate_candidate(self, payload: InquiryRequest) -> None:
        profile = self._profiles.get(payload.contractor_id)
        if profile is None:
            raise InquiryError("Подрядчик не найден в каталоге")
        if profile.synthetic:
            raise InquiryError("Для демонстрационного профиля нельзя отправить реальную заявку")
        if not filter_contractors([profile], payload.search).survivors:
            raise InquiryError("Подрядчик не соответствует условиям запроса; повторите поиск")

    async def submit(self, payload: InquiryRequest, idempotency_key: str) -> InquiryReceipt:
        """Persist once and attempt delivery once; identical payloads deduplicate.

        Reusing a key with other content raises InquiryConflict. Reusing the same
        content with a new key returns the original receipt without another send.
        """
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key):
            raise InquiryError("Недопустимый ключ повторной отправки")
        # Revalidation protects direct callers from Pydantic model_construct.
        payload = InquiryRequest.model_validate(payload.model_dump(mode="json"))
        serialized = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload_hash = _digest(serialized)
        key_hash = _digest(idempotency_key)
        created = False
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            keyed = connection.execute(
                "SELECT inquiry_id, payload_hash FROM inquiry_keys WHERE key_hash = ?", (key_hash,),
            ).fetchone()
            if keyed is not None:
                if not hmac.compare_digest(keyed["payload_hash"], payload_hash):
                    raise InquiryConflict("Этот ключ уже использован для другой заявки")
                row = connection.execute("SELECT * FROM inquiries WHERE id = ?", (keyed["inquiry_id"],)).fetchone()
            else:
                row = connection.execute("SELECT * FROM inquiries WHERE payload_hash = ?", (payload_hash,)).fetchone()
                if row is None:
                    if not self.delivery_configured and not self.allow_drafts:
                        raise InquiryUnavailable("Канал команды не настроен; контакты не сохранены и заявка не отправлена")
                    self._validate_candidate(payload)
                    inquiry_id = uuid.uuid4().hex
                    timestamp = _now()
                    status = "pending_delivery" if self.delivery_configured else "awaiting_channel"
                    connection.execute(
                        "INSERT INTO inquiries(id, payload_hash, payload_json, receipt_token_hash, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (inquiry_id, payload_hash, serialized, _digest(self._token(inquiry_id)), status, timestamp, timestamp),
                    )
                    row = connection.execute("SELECT * FROM inquiries WHERE id = ?", (inquiry_id,)).fetchone()
                    created = True
                connection.execute(
                    "INSERT INTO inquiry_keys(key_hash, inquiry_id, payload_hash) VALUES (?, ?, ?)",
                    (key_hash, row["id"], payload_hash),
                )
            connection.commit()
            inquiry_id = row["id"]
            receipt = self._receipt(row)
        if created and self.delivery_configured:
            return await self._deliver(inquiry_id)
        return receipt

    def get_receipt(self, inquiry_id: str, receipt_token: str) -> InquiryReceipt:
        # Unknown IDs and wrong tokens share the same response and reveal no PII.
        if not isinstance(inquiry_id, str) or not re.fullmatch(r"[a-f0-9]{32}", inquiry_id):
            raise InquiryNotFound("Заявка не найдена")
        if not isinstance(receipt_token, str) or not re.fullmatch(r"[a-f0-9]{64}", receipt_token):
            raise InquiryNotFound("Заявка не найдена")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM inquiries WHERE id = ?", (inquiry_id,)).fetchone()
        if row is None or not hmac.compare_digest(_digest(receipt_token), row["receipt_token_hash"]):
            raise InquiryNotFound("Заявка не найдена")
        return self._receipt(row)

    def _message(self, inquiry_id: str, payload: InquiryRequest) -> str:
        profile = self._profiles[payload.contractor_id]
        query = payload.search
        return "\n".join([
            f"Новая заявка команде · {inquiry_id}",
            "Это запрос на уточнение условий, не бронирование и не подтверждение доступности.",
            f"Подрядчик: {profile.anon_name} ({profile.id})",
            f"Город: {query.city}; категория: {query.category}",
            f"Дата: {query.date}; формат: {query.event_format}",
            f"Бюджет: {query.budget:g} ₸; цена в каталоге: от {profile.price_from_kzt:g} ₸",
            f"Длительность: {query.duration if query.duration is not None else 'не указана'} ч; язык: {query.language or 'не указан'}",
            f"Имя: {payload.name}",
            f"Контакт: {payload.contact}",
            f"Комментарий: {payload.message or 'не указан'}",
            "Пользователь согласился передать эти данные команде для обработки данной заявки.",
        ])

    async def _deliver(self, inquiry_id: str) -> InquiryReceipt:
        if self.transport is None:
            raise InquiryUnavailable("Канал команды не настроен")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM inquiries WHERE id = ?", (inquiry_id,)).fetchone()
            if row["status"] != "pending_delivery":
                return self._receipt(row)
            payload = InquiryRequest.model_validate_json(row["payload_json"])
            try:
                # A queued request must still be valid if the catalog changed.
                self._validate_candidate(payload)
            except InquiryError:
                connection.execute("UPDATE inquiries SET status = 'delivery_failed', updated_at = ? WHERE id = ?", (_now(), inquiry_id))
                connection.commit()
                return self.get_receipt(inquiry_id, self._token(inquiry_id))
            connection.execute("UPDATE inquiries SET status = 'delivering', updated_at = ? WHERE id = ?", (_now(), inquiry_id))
            connection.commit()
        reference: str | None = None
        try:
            async with asyncio.timeout(self.delivery_timeout_seconds):
                reference = await self.transport.send(inquiry_id, self._message(inquiry_id, payload))
            status = "delivered_to_team"
        except DeliveryRejected:
            status = "delivery_failed"
        except Exception:
            # Never store/log exception messages: providers may include PII or
            # credentials. A timeout can happen after successful acceptance.
            status = "delivery_unknown"
        with self._connection() as connection:
            connection.execute(
                "UPDATE inquiries SET status = ?, provider_reference = ?, updated_at = ? WHERE id = ?",
                (status, str(reference)[:200] if reference is not None and status == "delivered_to_team" else None, _now(), inquiry_id),
            )
            connection.commit()
        return self.get_receipt(inquiry_id, self._token(inquiry_id))

    async def retry_pending(self, limit: int = 10) -> int:
        """Deliver only outbox entries never handed to a provider.

        Not an HTTP endpoint. Unknown, rejected, delivered, and local draft
        entries are excluded; configure a transport before invoking explicitly.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if not self.delivery_configured:
            return 0
        with self._connection() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM inquiries WHERE status = 'pending_delivery' ORDER BY created_at, id LIMIT ?", (limit,),
            ).fetchall()]
        for inquiry_id in ids:
            await self._deliver(inquiry_id)
        return len(ids)
