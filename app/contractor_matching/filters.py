"""Pure hard filters; every excluded pool member has exactly one first reason."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import ContractorProfile, RejectionStats, SearchOutcome, SearchRequest

REASON_ORDER = ("busy", "budget", "format", "duration", "language")


@dataclass(frozen=True)
class FilterResult:
    outcome: SearchOutcome
    message: str
    survivors: list[ContractorProfile]
    stats: RejectionStats
    pool_count: int
    rejected_by_id: dict[str, str]


def first_rejection_reason(profile: ContractorProfile, request: SearchRequest) -> str | None:
    if request.date in profile.busy_dates:
        return "busy"
    if profile.price_from_kzt > request.budget:
        return "budget"
    if request.event_format not in profile.event_formats:
        return "format"
    if request.duration is not None and profile.max_hours is not None and request.duration > profile.max_hours:
        return "duration"
    if request.language is not None and request.language not in profile.languages:
        return "language"
    return None


def filter_contractors(profiles: Iterable[ContractorProfile], request: SearchRequest) -> FilterResult:
    pool = sorted(
        (profile for profile in profiles if profile.city == request.city and request.category in profile.categories),
        key=lambda profile: profile.id,
    )
    stats = RejectionStats()
    if not pool:
        return FilterResult(
            SearchOutcome.NO_CATEGORY_IN_CITY,
            "В выбранном городе нет подрядчиков этой категории.", [], stats, 0, {},
        )
    survivors: list[ContractorProfile] = []
    rejected_by_id: dict[str, str] = {}
    for profile in pool:
        reason = first_rejection_reason(profile, request)
        if reason is None:
            survivors.append(profile)
        else:
            rejected_by_id[profile.id] = reason
            setattr(stats, reason, getattr(stats, reason) + 1)
    assert stats.total() == len(pool) - len(survivors)
    outcome = SearchOutcome.SUCCESS if survivors else SearchOutcome.ALL_FILTERED_OUT
    message = "Подрядчики найдены." if survivors else "Все подрядчики этой категории в городе исключены условиями поиска."
    return FilterResult(outcome, message, survivors, stats, len(pool), rejected_by_id)