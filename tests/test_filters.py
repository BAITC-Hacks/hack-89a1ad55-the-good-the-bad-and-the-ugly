from pathlib import Path
import random

import pytest

from contractor_matching.data_loader import load_profiles
from contractor_matching.filters import filter_contractors
from contractor_matching.models import ContractorProfile, SearchOutcome, SearchRequest

DATA = Path(__file__).resolve().parents[1] / "data"


def request(**changes):
    return SearchRequest.model_validate({
        "city": "Алматы", "category": "Ведущий", "date": "2026-10-04",
        "event_format": "корпоратив", "budget": 2000000, "duration": 6,
        "language": "русский", **changes,
    })


def profile(**changes):
    return ContractorProfile.model_validate({
        "id": "TEST-01", "anon_name": "Тестовый профиль", "city": "Алматы",
        "categories": ["Ведущий"], "price_from_kzt": 1000000,
        "event_formats": ["корпоратив"], "languages": ["русский"], "max_hours": 6,
        "busy_dates": [], "description": "Тестовый профиль.", **changes,
    })


def test_each_exclusion_has_only_the_first_reason_in_required_order():
    invalid = dict(
        busy_dates=["2026-10-04"], price_from_kzt=3000000, event_formats=["свадьба"],
        max_hours=2, languages=["казахский"],
    )
    overrides = [
        {}, {"busy_dates": []}, {"busy_dates": [], "price_from_kzt": 1000000},
        {"busy_dates": [], "price_from_kzt": 1000000, "event_formats": ["корпоратив"]},
        {"busy_dates": [], "price_from_kzt": 1000000, "event_formats": ["корпоратив"], "max_hours": 6},
    ]
    profiles = [profile(**{**invalid, **change, "id": f"TEST-{index}"}) for index, change in enumerate(overrides)]
    before = [item.model_dump() for item in profiles]
    result = filter_contractors(profiles, request())
    assert result.outcome == SearchOutcome.ALL_FILTERED_OUT
    assert result.stats.model_dump() == {"busy": 1, "budget": 1, "format": 1, "duration": 1, "language": 1}
    assert list(result.rejected_by_id.values()) == ["busy", "budget", "format", "duration", "language"]
    assert result.stats.total() == result.pool_count - len(result.survivors) == 5
    assert before == [item.model_dump() for item in profiles]


def test_city_and_category_form_exact_pool_before_counters():
    profiles = [
        profile(id="OTHER-CITY", city="Астана", busy_dates=["2026-10-04"]),
        profile(id="OTHER-CATEGORY", categories=["Фотограф"], price_from_kzt=9000000),
        profile(id="MULTI", categories=["Ведущий церемонии", "Ведущий"]),
    ]
    result = filter_contractors(profiles, request())
    assert result.pool_count == 1
    assert [item.id for item in result.survivors] == ["MULTI"]
    assert result.stats.total() == 0


def test_price_and_duration_equality_are_eligible_and_unlimited_duration_passes():
    result = filter_contractors([profile(price_from_kzt=2000000)], request())
    assert result.outcome == SearchOutcome.SUCCESS
    result = filter_contractors([profile(max_hours=None)], request(duration=1000))
    assert result.outcome == SearchOutcome.SUCCESS
    result = filter_contractors([profile(max_hours=1, languages=["казахский"])], request(duration=None, language=None))
    assert result.outcome == SearchOutcome.SUCCESS


def test_foreign_regression_does_not_offer_almaty_or_astana_hosts():
    result = filter_contractors(load_profiles(DATA), request(city="Зарубежье", date="2026-10-10"))
    assert result.outcome == SearchOutcome.NO_CATEGORY_IN_CITY
    assert result.pool_count == 0
    assert result.survivors == [] and result.rejected_by_id == {}
    assert result.stats.total() == 0


def test_dense_fixture_changes_with_date_and_filter_never_truncates_to_three():
    profiles = load_profiles(DATA)
    first = filter_contractors(profiles, request())
    second = filter_contractors(profiles, request(date="2026-10-05"))
    assert {item.id for item in first.survivors} == {"HK-27222", "HK-29829", "HK-35215", "HK-72938", "HK-75012"}
    assert {item.id for item in second.survivors} == {"HK-44733", "HK-72938", "HK-77838", "HK-88430"}
    assert first.pool_count == second.pool_count == 10
    assert first.stats.model_dump() == {"busy": 5, "budget": 0, "format": 0, "duration": 0, "language": 0}
    assert second.stats.model_dump() == {"busy": 5, "budget": 0, "format": 1, "duration": 0, "language": 0}
    for profile_id in {item.id for item in first.survivors} - {item.id for item in second.survivors}:
        assert second.rejected_by_id[profile_id] == "busy"


def test_rare_fixture_returns_two_and_all_filtered_fixture_has_budget_only():
    profiles = load_profiles(DATA)
    query = request(category="Флорист", event_format="свадьба", budget=300000, duration=10)
    result = filter_contractors(profiles, query)
    assert result.outcome == SearchOutcome.SUCCESS
    assert {item.id for item in result.survivors} == {"HK-39372", "HK-90001"}
    assert all(item.max_hours is None for item in result.survivors)
    assert result.stats.total() == 0
    empty = filter_contractors(profiles, request(category="Флорист", event_format="свадьба", budget=100000, duration=10))
    assert empty.outcome == SearchOutcome.ALL_FILTERED_OUT
    assert empty.stats.model_dump() == {"busy": 0, "budget": 2, "format": 0, "duration": 0, "language": 0}


def test_twenty_repeats_and_shuffled_input_preserve_ids_stats_and_reasons():
    profiles = load_profiles(DATA)
    expected = filter_contractors(profiles, request())
    rng = random.Random(421)
    for _ in range(20):
        rng.shuffle(profiles)
        actual = filter_contractors(profiles, request())
        assert actual == expected


def test_astana_decorator_demo_matches_frozen_calendars_and_planned_prices():
    profiles = load_profiles(DATA)
    result = filter_contractors(profiles, request(
        city="Астана", category="Декоратор", event_format="свадьба",
        date="2026-10-05", budget=3000000, duration=10,
    ))
    assert result.outcome == SearchOutcome.SUCCESS
    assert [item.id for item in result.survivors] == ["TEAM-DEC-AST-01", "TEAM-DEC-AST-02", "TEAM-DEC-AST-03"]
    assert result.stats.total() == 0
    assert all(item.synthetic and item.origin == "team_extension" for item in result.survivors)
    lower_budget = filter_contractors(profiles, request(
        city="Астана", category="Декоратор", event_format="свадьба",
        date="2026-10-05", budget=900000, duration=10,
    ))
    assert lower_budget.outcome == SearchOutcome.ALL_FILTERED_OUT
    assert lower_budget.stats.budget == 3


@pytest.mark.parametrize("category", ["Загородная площадка", "Лайв-бэнд", "Ресторан"])
def test_synthetic_extension_does_not_fill_unplanned_categories(category):
    result = filter_contractors(load_profiles(DATA), request(city="Астана", category=category))
    assert result.outcome == SearchOutcome.NO_CATEGORY_IN_CITY
