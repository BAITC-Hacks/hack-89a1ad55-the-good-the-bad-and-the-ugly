from copy import deepcopy
from pathlib import Path
import random

import pytest

from contractor_matching.data_loader import load_profiles
from contractor_matching.filters import filter_contractors
from contractor_matching.guidance import build_guidance
from contractor_matching.models import (
    DATE_MAX, DATE_MIN, ContractorProfile, SearchOutcome, SearchRequest,
)
from contractor_matching.presets import PRESETS

DATA = Path(__file__).resolve().parents[1] / "data"


def request(**changes):
    return SearchRequest.model_validate({
        "city": "Алматы", "category": "Ведущий", "date": "2026-10-04",
        "event_format": "корпоратив", "budget": 1_000_000, "duration": 6,
        "language": "русский", **changes,
    })


def profile(**changes):
    return ContractorProfile.model_validate({
        "id": "TEST-01", "anon_name": "Тестовый профиль", "city": "Алматы",
        "categories": ["Ведущий"], "price_from_kzt": 1_000_000,
        "event_formats": ["корпоратив"], "languages": ["русский"], "max_hours": 6,
        "busy_dates": [], "description": "Тестовый профиль.", **changes,
    })


def guidance(profiles, query):
    filtered = filter_contractors(profiles, query)
    return build_guidance(profiles, query, filtered)


def verify_replay(profiles, query, suggestions):
    baseline = filter_contractors(profiles, query)
    assert len(suggestions) <= 3
    assert len({item["id"] for item in suggestions}) == len(suggestions)
    for item in suggestions:
        replay_request = SearchRequest.model_validate(item["request"])
        replay = filter_contractors(profiles, replay_request)
        assert replay.outcome == SearchOutcome.SUCCESS
        assert len(replay.survivors) == item["eligible_count"] > len(baseline.survivors)
        assert replay_request.city == query.city
        if baseline.outcome != SearchOutcome.NO_CATEGORY_IN_CITY:
            assert replay_request.category == query.category
        changed = {
            key: value for key, value in replay_request.model_dump(mode="json").items()
            if value != query.model_dump(mode="json")[key]
        }
        assert changed == item["changes"]
        assert len(changed) == 1
        assert item["label"] and item["explanation"]


@pytest.mark.parametrize("preset", PRESETS, ids=lambda preset: preset["id"])
def test_every_catalog_preset_suggestion_replays_all_hard_filters(preset):
    profiles = load_profiles(DATA)
    query = SearchRequest.model_validate(preset["request"])
    verify_replay(profiles, query, guidance(profiles, query))


def test_date_is_nearest_valid_day_and_ties_prefer_future():
    query = request()
    profiles = [profile(busy_dates=[query.date])]
    proposals = guidance(profiles, query)
    assert len(proposals) == 1
    assert proposals[0]["changes"] == {"date": "2026-10-05"}
    assert "2026-10-04" in proposals[0]["explanation"]
    assert "календар" in proposals[0]["explanation"]
    verify_replay(profiles, query, proposals)


@pytest.mark.parametrize("day,expected", [(DATE_MIN, "2026-09-24"), (DATE_MAX, "2026-12-30")])
def test_date_proposals_stay_in_supported_window(day, expected):
    query = request(date=day)
    profiles = [profile(busy_dates=[day])]
    assert guidance(profiles, query)[0]["changes"] == {"date": expected}


def test_budget_is_minimum_that_passes_every_other_condition():
    profiles = [
        profile(id="BUSY-CHEAP", price_from_kzt=1_000_001, busy_dates=["2026-10-04"]),
        profile(id="LANG-CHEAP", price_from_kzt=1_000_002, languages=["казахский"]),
        profile(id="PASS", price_from_kzt=1_050_000.5),
        profile(id="PASS-EXPENSIVE", price_from_kzt=2_000_000),
    ]
    proposals = guidance(profiles, request())
    assert len(proposals) == 1
    assert proposals[0]["changes"] == {"budget": 1_050_000.5}
    assert "от»" in proposals[0]["explanation"]
    verify_replay(profiles, request(), proposals)


def test_duration_uses_smallest_shortening_and_never_clears_requirement():
    profiles = [profile(id="SHORT", max_hours=2), profile(id="LONG", max_hours=5)]
    proposals = guidance(profiles, request())
    assert len(proposals) == 1
    assert proposals[0]["changes"] == {"duration": 5}
    verify_replay(profiles, request(), proposals)


def test_language_and_format_alternatives_explicitly_identify_replaced_choice():
    profiles = [
        profile(id="LANGUAGE", languages=["казахский"]),
        profile(id="FORMAT", event_formats=["свадьба"]),
    ]
    proposals = guidance(profiles, request())
    assert [item["changes"] for item in proposals] == [
        {"language": "казахский"}, {"event_format": "свадьба"},
    ]
    assert "вместо «русский»" in proposals[0]["explanation"]
    assert "вместо «корпоратив»" in proposals[1]["explanation"]
    verify_replay(profiles, request(), proposals)


def test_multiple_failures_do_not_produce_unverified_relaxation():
    profiles = [profile(price_from_kzt=1_100_000, languages=["казахский"])]
    assert guidance(profiles, request()) == []


def test_existing_one_or_two_candidates_only_get_broader_results():
    profiles = [profile(), profile(id="COSTLIER", price_from_kzt=1_200_000)]
    proposals = guidance(profiles, request())
    assert len(proposals) == 1
    assert proposals[0]["changes"] == {"budget": 1_200_000}
    assert proposals[0]["eligible_count"] == 2
    assert guidance([profile()], request()) == []


def test_three_candidates_need_no_guidance():
    profiles = [profile(id=f"TEST-{index}") for index in range(3)]
    profiles.append(profile(id="COSTLIER", price_from_kzt=1_200_000))
    assert guidance(profiles, request()) == []


def test_absent_category_suggestions_are_real_other_services_in_same_city():
    profiles = [
        profile(id="OTHER-CITY", city="Астана"),
        profile(id="PHOTO", categories=["Фотограф"]),
        profile(id="BUSY-FLOWER", categories=["Флорист"], busy_dates=["2026-10-04"]),
        profile(id="VIDEO", categories=["Видеограф"]),
    ]
    proposals = guidance(profiles, request())
    assert [item["changes"] for item in proposals] == [
        {"category": "Фотограф"}, {"category": "Видеограф"},
    ]
    assert all("Другая категория:" in item["label"] for item in proposals)
    verify_replay(profiles, request(), proposals)


def test_foreign_regression_and_empty_catalog_never_invent_candidates():
    assert guidance([], request()) == []
    profiles = [profile()]
    assert guidance(profiles, request(city="Зарубежье")) == []
    actual = load_profiles(DATA)
    proposals = guidance(actual, request(city="Зарубежье"))
    verify_replay(actual, request(city="Зарубежье"), proposals)
    assert all(item["request"]["category"] == "Фотограф" for item in proposals)


def test_mutation_free_deterministic_and_at_most_three_actions():
    profiles = [
        profile(id="BUSY", busy_dates=["2026-10-04"]),
        profile(id="BUDGET", price_from_kzt=1_100_000),
        profile(id="DURATION", max_hours=5),
        profile(id="LANGUAGE", languages=["казахский"]),
        profile(id="FORMAT", event_formats=["свадьба"]),
    ]
    query = request()
    filtered = filter_contractors(profiles, query)
    original_request = query.model_dump()
    original_profiles = deepcopy([item.model_dump() for item in profiles])
    original_filtered = deepcopy(filtered)
    expected = build_guidance(profiles, query, filtered)
    assert [item["id"] for item in expected] == ["change-date", "change-budget", "change-duration"]
    rng = random.Random(99)
    for _ in range(20):
        rng.shuffle(profiles)
        assert build_guidance(profiles, query, filtered) == expected
    assert query.model_dump() == original_request
    assert sorted((item.model_dump() for item in profiles), key=lambda item: item["id"]) == sorted(original_profiles, key=lambda item: item["id"])
    assert filtered == original_filtered
    verify_replay(profiles, query, expected)
