from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class MatchQuery:
    city: str
    category: str
    event_format: str
    event_date: date
    budget_kzt: int
    guest_count: int
    required_skills: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Contractor:
    contractor_id: str
    name: str
    city: str
    categories: tuple[str, ...]
    event_formats: tuple[str, ...]
    price_from_kzt: int
    max_guests: int
    rating: float
    completed_events: int
    skills: tuple[str, ...] = field(default_factory=tuple)
    busy_dates: tuple[date, ...] = field(default_factory=tuple)
