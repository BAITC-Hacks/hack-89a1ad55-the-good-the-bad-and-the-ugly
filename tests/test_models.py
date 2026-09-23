from datetime import date

import pytest
from pydantic import ValidationError

from contractor_matching.models import ContractorProfile, FinalRejectionStats, SearchRequest


def request(**changes):
    return SearchRequest.model_validate({
        "city": "Алматы", "date": "2026-10-04", "event_format": "корпоратив",
        "category": "Ведущий", "budget": 2000000, **changes,
    })


def profile(**changes):
    return ContractorProfile.model_validate({
        "id": "TEST-01", "anon_name": "Тестовый профиль", "city": "Алматы",
        "categories": "Ведущий|Ведущий церемонии", "price_from_kzt": "500000",
        "event_formats": "корпоратив|свадьба", "languages": "русский|казахский",
        "max_hours": "6", "busy_dates": "2026-10-05|2026-10-06",
        "description": "Проверка импорта каталога.", "synthetic": "False",
        "city_imputed": "True", "price_imputed": "False", **changes,
    })


@pytest.mark.parametrize("field,value", [
    ("city", "алматы"), ("city", "Москва"), ("category", "Неизвестная категория"),
    ("event_format", "вечеринка"), ("language", "французский"), ("language", ""),
    ("date", "2026-9-23"), ("date", "2026-10-4"), ("date", "2026-11-31"),
    ("date", "2026-09-22"), ("date", "2027-01-01"), ("date", date(2026, 10, 4)),
    ("date", "2026-10-04T00:00:00"), ("budget", 0), ("budget", -1),
    ("budget", float("nan")), ("budget", float("inf")), ("budget", True),
    ("budget", "2000000"), ("budget", 10 ** 1000), ("duration", 0), ("duration", -1), ("duration", True),
    ("duration", 2.5), ("duration", 2.0), ("duration", "2"),
])
def test_request_rejects_invalid_inputs(field, value):
    with pytest.raises(ValidationError):
        request(**{field: value})


@pytest.mark.parametrize("day", ["2026-09-23", "2026-12-31"])
def test_dates_include_both_boundaries(day):
    assert request(date=day).date == day


def test_optional_constraints_are_absent_not_empty():
    parsed = request()
    assert parsed.language is None and parsed.duration is None
    assert request(duration=3, language="казахский").duration == 3
    with pytest.raises(ValidationError):
        request(unrecognized="value")


def test_csv_bool_and_pipe_parsing_are_explicit():
    parsed = profile()
    assert parsed.synthetic is False
    assert parsed.price_imputed is False
    assert parsed.city_imputed is True
    assert parsed.max_hours == 6
    assert parsed.categories == ["Ведущий", "Ведущий церемонии"]
    assert parsed.busy_dates == frozenset({"2026-10-05", "2026-10-06"})


@pytest.mark.parametrize("bad", ["false", "TRUE", "yes", "0", "", 0, 1, None])
def test_csv_bool_does_not_guess(bad):
    with pytest.raises(ValidationError):
        profile(synthetic=bad)


@pytest.mark.parametrize("value", [None, ""])
def test_only_empty_hours_becomes_none(value):
    assert profile(max_hours=value).max_hours is None


@pytest.mark.parametrize("bad", ["null", "не применимо", "ошибка", "3.5", 3.0, True, 0, -2])
def test_bad_max_hours_is_not_silently_unlimited(bad):
    with pytest.raises(ValidationError):
        profile(max_hours=bad)


@pytest.mark.parametrize("bad", [None, 3, "2026-11-31", "2027-01-01", "2026-10-05|2026-10-05"])
def test_bad_calendar_is_not_silently_empty(bad):
    with pytest.raises(ValidationError):
        profile(busy_dates=bad)


def test_valid_empty_calendar_and_frozen_model():
    parsed = profile(busy_dates="")
    assert parsed.busy_dates == frozenset()
    with pytest.raises(ValidationError):
        parsed.city = "Астана"


def test_api_stats_total_serializes_and_core_total_remains_callable():
    parsed = FinalRejectionStats.model_validate({"busy": 2, "budget": 1, "total": 99})
    assert parsed.total() == 3
    assert parsed.model_dump(by_alias=True) == {"busy": 2, "budget": 1, "format": 0, "duration": 0, "language": 0, "total": 3}
    schema = FinalRejectionStats.model_json_schema(mode="serialization", by_alias=True)
    assert "total" in schema["properties"]
    assert schema["properties"]["total"]["readOnly"]
