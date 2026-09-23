import random

import pytest

from contractor_matching.models import ContractorProfile, SearchRequest
from contractor_matching.ranking import rank_contractors


def profile(profile_id="A", **changes):
    return ContractorProfile.model_validate({
        "id": profile_id, "anon_name": "Тестовый профиль", "city": "Алматы",
        "categories": ["Ведущий"], "price_from_kzt": 1000000,
        "event_formats": ["корпоратив"], "languages": ["русский"], "max_hours": 6,
        "busy_dates": [], "description": "Тестовый профиль.", **changes,
    })


def query(language="русский"):
    return SearchRequest(city="Алматы", category="Ведущий", date="2026-10-04",
                         event_format="корпоратив", budget=2000000, language=language)


def test_exact_formula_and_raw_score_are_preserved():
    result = rank_contractors([profile(synthetic=True)], query(), {"A": 0.823456})[0]
    assert result.score == pytest.approx(0.5 * 0.823456 + 0.3 * (1000000 / 2000000) + 0.2 - 0.05)
    assert result.score != round(result.score, 4)
    assert result.score_breakdown == {
        "cosine": 0.823456, "semantic": 0.411728, "price_ratio": 0.5,
        "budget": 0.15, "language": 0.2, "synthetic_penalty": -0.05,
    }


@pytest.mark.parametrize("language,expected", [(None, 0.1), ("русский", 0.2), ("казахский", 0.0)])
def test_language_formula_exactly_matches_spec(language, expected):
    result = rank_contractors([profile()], query(language), {"A": 0.5})[0]
    assert result.score_breakdown["language"] == expected


def test_price_component_rewards_higher_price_within_budget():
    profiles = [profile("CHEAP", price_from_kzt=500000), profile("EXPENSIVE", price_from_kzt=1500000)]
    ranked = rank_contractors(profiles, query(), {"CHEAP": 0.5, "EXPENSIVE": 0.5})
    assert [candidate.profile.id for candidate in ranked] == ["EXPENSIVE", "CHEAP"]


def test_synthetic_penalty_uses_boolean_not_nonempty_csv_string():
    profiles = [profile("REAL", synthetic="False"), profile("SYNTHETIC", synthetic="True")]
    ranked = rank_contractors(profiles, query(), {"REAL": 0.5, "SYNTHETIC": 0.5})
    assert ranked[0].profile.id == "REAL"
    assert ranked[0].score - ranked[1].score == pytest.approx(0.05)


def test_rounded_score_tie_breaks_by_price_then_id_not_unrounded_score():
    # Raw B is slightly larger, but both round to 0.6; cheaper A must win.
    profiles = [profile("B", price_from_kzt=1000100), profile("A", price_from_kzt=1000000)]
    ranked = rank_contractors(profiles, query(), {"A": 0.5, "B": 0.5})
    assert ranked[0].profile.id == "A"
    assert ranked[0].score < ranked[1].score
    identical_price = rank_contractors([profile("Z"), profile("A")], query(), {"Z": 0.5, "A": 0.5})
    assert [candidate.profile.id for candidate in identical_price] == ["A", "Z"]


def test_top_three_are_selected_after_scoring_every_survivor():
    profiles = [profile(letter) for letter in ["A", "B", "C", "D", "E"]]
    similarities = {letter: index / 10 for index, letter in enumerate(["A", "B", "C", "D", "E"])}
    expected = rank_contractors(profiles, query(), similarities)
    assert [candidate.profile.id for candidate in expected] == ["E", "D", "C"]
    rng = random.Random(32)
    for _ in range(20):
        rng.shuffle(profiles)
        assert rank_contractors(profiles, query(), similarities) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.1, 1.1, True])
def test_broken_embedding_does_not_silently_change_ranking_model(bad):
    with pytest.raises(ValueError, match="Invalid cosine"):
        rank_contractors([profile()], query(), {"A": bad})
    with pytest.raises(KeyError):
        rank_contractors([profile()], query(), {})


def test_empty_pool_is_valid():
    assert rank_contractors([], query(), {}) == []
