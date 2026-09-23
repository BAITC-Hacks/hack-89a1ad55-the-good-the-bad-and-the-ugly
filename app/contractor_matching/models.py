"""Validated catalog and API contracts for the HackAlem matching service."""
from __future__ import annotations

from datetime import date as calendar_date
from enum import Enum
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

CITIES = ("Алматы", "Астана", "Зарубежье")
CATEGORIES = (
    "Ведущий", "Фотограф", "Видеограф", "Банкетный зал", "Ресторан", "Отель",
    "Загородная площадка", "Лайв-бэнд", "Инструменталист", "Национальный ансамбль",
    "Танцевальный коллектив", "Шоу-программа", "Ведущий церемонии", "Декоратор",
    "Флорист", "Подарки и сувениры", "Фото и видеобудки",
)
EVENT_FORMATS = ("свадьба", "той", "корпоратив", "конференция", "юбилей", "день рождения")
LANGUAGES = ("русский", "казахский", "английский")
DATE_MIN = "2026-09-23"
DATE_MAX = "2026-12-31"


def validate_iso_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Дата должна быть строкой YYYY-MM-DD")
    try:
        calendar_date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Указана несуществующая календарная дата") from exc
    if not DATE_MIN <= value <= DATE_MAX:
        raise ValueError(f"Дата должна быть в диапазоне {DATE_MIN}–{DATE_MAX}")
    return value


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    city: Literal["Алматы", "Астана", "Зарубежье"]
    date: str
    event_format: Literal["свадьба", "той", "корпоратив", "конференция", "юбилей", "день рождения"]
    category: Literal[
        "Ведущий", "Фотограф", "Видеограф", "Банкетный зал", "Ресторан", "Отель",
        "Загородная площадка", "Лайв-бэнд", "Инструменталист", "Национальный ансамбль",
        "Танцевальный коллектив", "Шоу-программа", "Ведущий церемонии", "Декоратор",
        "Флорист", "Подарки и сувениры", "Фото и видеобудки",
    ]
    budget: float = Field(gt=0)
    duration: int | None = Field(default=None, gt=0, strict=True)
    language: Literal["русский", "казахский", "английский"] | None = None

    @field_validator("date", mode="before")
    @classmethod
    def valid_date(cls, value: Any) -> str:
        return validate_iso_date(value)

    @field_validator("budget", mode="before")
    @classmethod
    def valid_budget(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Бюджет должен быть числом")
        try:
            numeric = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("Бюджет должен быть конечным числом") from exc
        if not math.isfinite(numeric):
            raise ValueError("Бюджет должен быть конечным числом")
        return numeric

    @field_validator("category")
    @classmethod
    def known_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError("Неизвестная категория")
        return value

    @field_validator("event_format")
    @classmethod
    def known_format(cls, value: str) -> str:
        if value not in EVENT_FORMATS:
            raise ValueError("Неизвестный формат мероприятия")
        return value

    @field_validator("language")
    @classmethod
    def known_language(cls, value: str | None) -> str | None:
        if value is not None and value not in LANGUAGES:
            raise ValueError("Неизвестный язык")
        return value


class ContractorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: str = Field(min_length=1)
    anon_name: str = Field(min_length=1)
    categories: list[str] = Field(min_length=1)
    city: Literal["Алматы", "Астана", "Зарубежье"]
    city_imputed: bool = False
    synthetic: bool = False
    price_from_kzt: float = Field(gt=0)
    price_imputed: bool = False
    event_formats: list[str] = Field(min_length=1)
    languages: list[str] = Field(min_length=1)
    max_hours: int | None = Field(default=None, gt=0, strict=True)
    busy_dates: frozenset[str] = Field(default_factory=frozenset)
    description: str = Field(min_length=1)
    origin: Literal["source_dataset", "team_extension"] = "source_dataset"

    @field_validator("city_imputed", "synthetic", "price_imputed", mode="before")
    @classmethod
    def explicit_bool(cls, value: Any) -> bool:
        if type(value) is bool:
            return value
        if isinstance(value, str) and value in ("True", "False"):
            return value == "True"
        raise ValueError("Булево поле должно быть True или False")

    @field_validator("categories", "event_formats", "languages", mode="before")
    @classmethod
    def parse_pipe_values(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split("|") if part.strip()]
        return value

    @field_validator("categories", "event_formats", "languages")
    @classmethod
    def known_values(cls, value: list[str], info: Any) -> list[str]:
        allowed = {"categories": CATEGORIES, "event_formats": EVENT_FORMATS, "languages": LANGUAGES}[info.field_name]
        if len(set(value)) != len(value) or any(item not in allowed for item in value):
            raise ValueError(f"Неизвестные или повторяющиеся значения {info.field_name}")
        return value

    @field_validator("price_from_kzt", mode="before")
    @classmethod
    def valid_price(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("Цена должна быть числом")
        return value

    @field_validator("max_hours", mode="before")
    @classmethod
    def parse_max_hours(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value):
            return int(value)
        if type(value) is int:
            return value
        raise ValueError("max_hours должен быть положительным целым числом или пустым")

    @field_validator("busy_dates", mode="before")
    @classmethod
    def parse_busy_dates(cls, value: Any) -> frozenset[str]:
        if isinstance(value, str):
            values = [part.strip() for part in value.split("|") if part.strip()]
        elif isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
        else:
            raise ValueError("busy_dates должен быть массивом дат или строкой с разделителем |")
        checked = [validate_iso_date(item) for item in values]
        if len(set(checked)) != len(checked):
            raise ValueError("busy_dates содержит повторяющиеся даты")
        return frozenset(checked)


class SearchOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    NO_CATEGORY_IN_CITY = "NO_CATEGORY_IN_CITY"
    ALL_FILTERED_OUT = "ALL_FILTERED_OUT"


class RejectionStats(BaseModel):
    busy: int = Field(default=0, ge=0)
    budget: int = Field(default=0, ge=0)
    format: int = Field(default=0, ge=0)
    duration: int = Field(default=0, ge=0)
    language: int = Field(default=0, ge=0)

    def total(self) -> int:
        return self.busy + self.budget + self.format + self.duration + self.language


class FinalRejectionStats(RejectionStats):
    """Serialize the total while preserving the core stats.total() interface."""

    @computed_field(alias="total")
    @property
    def total_count(self) -> int:
        return self.total()


class ContractorCard(BaseModel):
    id: str
    anon_name: str
    category: str
    categories: list[str]
    city: str
    price_from_kzt: float
    explanation: str
    synthetic: bool
    origin: Literal["source_dataset", "team_extension"]
    city_imputed: bool
    price_imputed: bool
    available_on: str
    languages: list[str]
    max_hours: int | None
    explanation_source: str
    warnings: list[str] = Field(default_factory=list)
    score: float
    score_breakdown: dict[str, float]


class SearchResponse(BaseModel):
    outcome: SearchOutcome
    message: str
    cards: list[ContractorCard] = Field(default_factory=list)
    stats: FinalRejectionStats = Field(default_factory=FinalRejectionStats)
    pool_count: int
    eligible_count: int
    diagnostics: dict[str, Any]
    elapsed_ms: float
