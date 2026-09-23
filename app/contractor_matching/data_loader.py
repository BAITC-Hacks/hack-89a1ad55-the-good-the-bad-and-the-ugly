from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable

from .models import Contractor


def _parse_dates(values: Iterable[str]) -> tuple[date, ...]:
    return tuple(date.fromisoformat(value) for value in values)


def load_contractors(path: str | Path) -> list[Contractor]:
    raw_contractors = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Contractor(
            contractor_id=item["contractor_id"],
            name=item["name"],
            city=item["city"],
            categories=tuple(item["categories"]),
            event_formats=tuple(item["event_formats"]),
            price_from_kzt=int(item["price_from_kzt"]),
            max_guests=int(item["max_guests"]),
            rating=float(item["rating"]),
            completed_events=int(item["completed_events"]),
            skills=tuple(item.get("skills", ())),
            busy_dates=_parse_dates(item.get("busy_dates", ())),
        )
        for item in raw_contractors
    ]
