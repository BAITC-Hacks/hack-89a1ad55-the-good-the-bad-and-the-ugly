from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import Contractor, MatchQuery


@dataclass(frozen=True)
class EligibilityResult:
    contractor: Contractor
    eligible: bool
    reasons: tuple[str, ...] = ()


def rejection_reasons(contractor: Contractor, query: MatchQuery) -> tuple[str, ...]:
    reasons: list[str] = []

    if contractor.city.casefold() != query.city.casefold():
        reasons.append("city_mismatch")
    if query.category.casefold() not in {category.casefold() for category in contractor.categories}:
        reasons.append("category_mismatch")
    if query.event_format.casefold() not in {fmt.casefold() for fmt in contractor.event_formats}:
        reasons.append("format_mismatch")
    if contractor.price_from_kzt > query.budget_kzt:
        reasons.append("over_budget")
    if contractor.max_guests < query.guest_count:
        reasons.append("insufficient_capacity")
    if query.event_date in contractor.busy_dates:
        reasons.append("busy_on_event_date")

    return tuple(reasons)


def is_eligible(contractor: Contractor, query: MatchQuery) -> bool:
    return not rejection_reasons(contractor, query)


def filter_eligible_contractors(
    contractors: Iterable[Contractor],
    query: MatchQuery,
) -> list[EligibilityResult]:
    results = [
        EligibilityResult(
            contractor=contractor,
            eligible=not (reasons := rejection_reasons(contractor, query)),
            reasons=reasons,
        )
        for contractor in contractors
    ]
    return [result for result in results if result.eligible]
