"""Verified, optional next searches; never relax the current search silently.

This assistant layer is deterministic. Every proposal changes one explicit field
and is replayed through the same hard filters as a normal request. It does not
alter ranking, availability, the original cards, or the selected city.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from .filters import FilterResult, filter_contractors
from .models import (
    CATEGORIES, DATE_MAX, DATE_MIN, EVENT_FORMATS, LANGUAGES,
    ContractorProfile, SearchOutcome, SearchRequest,
)


def _money(value: float) -> str:
    if value.is_integer():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,}".replace(",", " ")


def build_guidance(
    profiles: Iterable[ContractorProfile],
    request: SearchRequest,
    filtered: FilterResult,
) -> list[dict[str, Any]]:
    """Return at most three useful, fully validated alternative requests.

    A useful proposal produces strictly more eligible profiles than the original
    request. Prefer one nearest date, one minimal budget increase, and then the
    smallest useful duration change. Language and format changes are explicit
    alternatives, not inferred preferences. Absent categories are the only case
    where an alternative service category can be proposed.
    """
    baseline = len(filtered.survivors)
    if baseline >= 3:
        return []
    catalog = tuple(profiles)
    original = request.model_dump(mode="json")
    proposals: list[dict[str, Any]] = []

    def add(field: str, value: Any, label: str, explanation: str, identifier: str) -> bool:
        if value == original[field]:
            return False
        # model_validate (rather than model_copy) also validates changed values.
        candidate = SearchRequest.model_validate({**original, field: value})
        verified = filter_contractors(catalog, candidate)
        eligible_count = len(verified.survivors)
        if eligible_count <= baseline:
            return False
        proposals.append({
            "id": identifier,
            "label": label,
            "explanation": explanation.format(count=eligible_count),
            "request": candidate.model_dump(mode="json"),
            "eligible_count": eligible_count,
            "changes": {field: candidate.model_dump(mode="json")[field]},
        })
        return True

    if filtered.outcome == SearchOutcome.NO_CATEGORY_IN_CITY:
        # These are other services, not substitute cards in the requested pool.
        available_categories = {
            category for profile in catalog if profile.city == request.city
            for category in profile.categories
        }
        for index, category in enumerate(CATEGORIES):
            if category not in available_categories or category == request.category:
                continue
            add(
                "category", category, f"Другая категория: {category}",
                f"Категории «{request.category}» в городе «{request.city}» нет. "
                f"Для другой услуги «{category}» проходят условия: {{count}}. "
                "Город, дата и остальные условия сохраняются.",
                f"category-{index}",
            )
            if len(proposals) == 3:
                break
        return proposals

    pool = tuple(
        profile for profile in catalog
        if profile.city == request.city and request.category in profile.categories
    )
    chosen_day = date.fromisoformat(request.date)
    minimum_day = date.fromisoformat(DATE_MIN)
    maximum_day = date.fromisoformat(DATE_MAX)
    dates = (
        minimum_day + timedelta(days=offset)
        for offset in range((maximum_day - minimum_day).days + 1)
    )
    # On equal distance prefer a later day, then earlier. No wall-clock dependency.
    nearby_days = sorted(
        (day for day in dates if day != chosen_day),
        key=lambda day: (abs((day - chosen_day).days), day < chosen_day, day),
    )
    for day in nearby_days:
        next_date = day.isoformat()
        if add(
            "date", next_date, f"Проверить дату {next_date}",
            f"При смене даты с {request.date} на {next_date} проходят условия: {{count}}. "
            "Это ближайшая дата с более широким выбором по календарю каталога; "
            "занятость нужно подтвердить у подрядчика.",
            "change-date",
        ):
            break

    # Crossing a catalog price is the only point where a budget change can help.
    for budget in sorted({profile.price_from_kzt for profile in pool if profile.price_from_kzt > request.budget}):
        if add(
            "budget", budget, f"Бюджет {_money(budget)} ₸",
            f"Минимальный бюджет, расширяющий выбор при остальных условиях: {_money(budget)} ₸. "
            "Проходят условия: {count}. В каталоге указаны цены «от»; "
            "итоговую стоимость нужно согласовать.",
            "change-budget",
        ):
            break

    if request.duration is not None:
        durations = sorted({
            profile.max_hours for profile in pool
            if profile.max_hours is not None and profile.max_hours < request.duration
        }, reverse=True)
        for duration in durations:
            if add(
                "duration", duration, f"Длительность {duration} ч",
                f"Если сократить длительность с {request.duration} до {duration} ч, "
                "проходят условия: {count}. Остальные условия сохраняются.",
                "change-duration",
            ):
                break

    if len(proposals) >= 3:
        return proposals[:3]

    if request.language is not None:
        for language in LANGUAGES:
            if add(
                "language", language, f"Другой язык: {language}",
                f"Только если вам подходит язык «{language}» вместо «{request.language}»: "
                "проходят условия: {count}. Остальные условия сохраняются.",
                "change-language",
            ):
                break
    if len(proposals) >= 3:
        return proposals[:3]

    for event_format in EVENT_FORMATS:
        if add(
            "event_format", event_format, f"Другой формат: {event_format}",
            f"Только если мероприятие имеет формат «{event_format}» вместо «{request.event_format}»: "
            "проходят условия: {count}. Остальные условия сохраняются.",
            "change-format",
        ):
            break
    return proposals[:3]
