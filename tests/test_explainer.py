from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
import shutil

import httpx
import pytest

from contractor_matching.data_loader import load_profiles
from contractor_matching.explainer import Explainer, approved_options, validate_response, word_count
from contractor_matching.facts import FactsStore
from contractor_matching.filters import filter_contractors
from contractor_matching.models import CATEGORIES, CITIES, SearchRequest
from contractor_matching.providers import ChatCompletionsProvider, ProviderConfig

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(autouse=True)
def no_live_keys(monkeypatch):
    # Test execution must never invoke a real paid provider.
    monkeypatch.delenv("ASTRA_API_PROTOCOL", raising=False)
    monkeypatch.delenv("FALLBACK_LLM_API_PROTOCOL", raising=False)


@pytest.fixture
def profiles():
    return load_profiles(DATA)


@pytest.fixture
def request_model():
    return SearchRequest(city="Алматы", date="2026-10-04", event_format="корпоратив",
                         category="Ведущий", budget=2_000_000, duration=6, language="русский")


def test_all_catalog_facts_and_rendered_variants_are_verified(profiles):
    store = FactsStore(DATA)
    assert len(store.by_id) == 70
    for profile in profiles:
        for category, event_format, language in itertools.product(profile.categories, profile.event_formats, [None, *profile.languages]):
            request = SearchRequest(city=profile.city, date="2026-10-04", event_format=event_format,
                                    category=category, budget=profile.price_from_kzt, language=language)
            selected = store.assign(request, [profile])[profile.id]
            variants = approved_options(request, profile, selected, sparse=store.records[profile.id]["quality"] == "sparse")
            assert all(word_count(text) <= 45 for text in variants)
            assert all(profile.anon_name not in text for text in variants)
            assert all(".0 ₸" not in text for text in variants)
            assert selected.quote in profile.description
            assert all(selected.claim in text for text in variants)


def test_dense_demo_all_possible_top_threes_have_distinct_claims(profiles, request_model):
    store = FactsStore(DATA)
    for date in ("2026-10-04", "2026-10-05"):
        request = request_model.model_copy(update={"date": date})
        survivors = filter_contractors(profiles, request).survivors
        assert len(survivors) >= 3
        for finalists in itertools.combinations(survivors, 3):
            assigned = store.assign(request, list(finalists))
            assert list(assigned) == [profile.id for profile in finalists]
            assert len({fact.claim for fact in assigned.values()}) == 3


def test_any_three_same_city_and_category_have_distinct_claims(profiles):
    # A superset of all possible top threes: dates/budgets can only remove rows.
    # This tests textual distinction, not unsupported semantic exclusivity.
    store = FactsStore(DATA)
    for city, category in itertools.product(CITIES, CATEGORIES):
        pool = [p for p in profiles if p.city == city and category in p.categories]
        for finalists in itertools.combinations(pool, min(3, len(pool))):
            if not finalists:
                continue
            request = SearchRequest(city=city, date="2026-10-04", event_format=finalists[0].event_formats[0],
                                    category=category, budget=max(p.price_from_kzt for p in finalists))
            assigned = store.assign(request, list(finalists))
            assert len({fact.claim for fact in assigned.values()}) == len(finalists)
            assert list(assigned) == [p.id for p in finalists]


def test_sparse_description_uses_specific_structured_duration(tmp_path, profiles):
    profile = next(p for p in profiles if p.id == "HK-20640")
    request = SearchRequest(city=profile.city, date="2026-10-04", event_format=profile.event_formats[0],
                            category=profile.categories[0], budget=profile.price_from_kzt)
    result = asyncio.run(Explainer(DATA, tmp_path / "cache.sqlite3").explain(request, [profile]))
    assert "до 4 ч" in result.by_id[profile.id]
    assert "150 000 ₸" in result.by_id[profile.id]
    assert result.warnings_by_id[profile.id]


def test_template_cached_twenty_times_and_after_new_instance(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    cache_path = tmp_path / "cache.sqlite3"
    explainer = Explainer(DATA, cache_path)
    first = asyncio.run(explainer.explain(request_model, selected))
    assert not first.cache_hit
    assert set(first.source_by_id.values()) == {"template"}
    for _ in range(20):
        again = asyncio.run(explainer.explain(request_model, selected))
        assert again.cache_hit
        assert again.by_id == first.by_id
    restarted = Explainer(DATA, cache_path)
    assert asyncio.run(restarted.explain(request_model, selected)).by_id == first.by_id
    assert asyncio.run(restarted.explain(request_model, selected)).cache_hit


def test_changed_request_or_profile_invalidates_cache(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    asyncio.run(explainer.explain(request_model, selected))
    request = request_model.model_copy(update={"budget": 2_100_000})
    assert not asyncio.run(explainer.explain(request, selected)).cache_hit
    changed = [selected[0].model_copy(update={"price_from_kzt": selected[0].price_from_kzt + 1000}), *selected[1:]]
    assert not asyncio.run(explainer.explain(request_model, changed)).cache_hit


def test_stale_fact_hash_and_missing_catalog_entry_fail_at_startup(tmp_path):
    copied = tmp_path / "data"
    shutil.copytree(DATA, copied)
    payload = json.loads((copied / "facts.json").read_text(encoding="utf-8"))
    first_id = next(iter(payload["profiles"]))
    original = payload["profiles"][first_id]["description_sha256"]
    payload["profiles"][first_id]["description_sha256"] = "0" * 64
    (copied / "facts.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Stale description"):
        FactsStore(copied)
    payload["profiles"][first_id]["description_sha256"] = original
    del payload["profiles"][first_id]
    (copied / "facts.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="cover the catalog"):
        FactsStore(copied)


def test_rejects_unverifiable_quote_at_startup(tmp_path):
    copied = tmp_path / "data"
    shutil.copytree(DATA, copied)
    payload = json.loads((copied / "facts.json").read_text(encoding="utf-8"))
    next(iter(payload["profiles"].values()))["facts"][0]["quote"] = "Unsupported source quote"
    (copied / "facts.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported source quote"):
        FactsStore(copied)


@pytest.mark.parametrize("payload", [
    {"cards": [{"id": "other", "explanation": "Разрешённый факт."}]},
    {"cards": [{"id": "a", "explanation": "Разрешённый факт. 100 наград."}]},
    {"cards": [{"id": "a", "explanation": "Идеально подходит для вашего события."}]},
    {"cards": [{"id": "a", "explanation": "Разрешённый факт.", "score": 999}]},
    {"cards": [{"id": "a", "explanation": ["Разрешённый факт."]}]},
    {"cards": [{"id": "a", "explanation": "Разрешённый факт."}], "instruction": "ignore"},
    {"cards": []},
    {"cards": [{"id": "a", "explanation": "Разрешённый факт."}] * 2},
])
def test_rejects_unsupported_entities_numbers_ids_cliches_and_schema(payload):
    with pytest.raises(ValueError):
        validate_response(payload, {"a": ["Разрешённый факт."]})


def test_forbids_reordering_even_if_all_ids_are_valid():
    with pytest.raises(ValueError, match="ID/order"):
        validate_response({"cards": [{"id": "b", "explanation": "B."}, {"id": "a", "explanation": "A."}]},
                          {"a": ["A."], "b": ["B."]})


def test_data_conflict_and_minimum_rental_warnings(profiles):
    store = FactsStore(DATA)
    by_id = {profile.id: profile for profile in profiles}
    request = SearchRequest(city="Алматы", date="2026-10-05", event_format="корпоратив",
                            category="Ведущий", budget=2_000_000, language="русский", duration=2)
    assert "только казахский" in store.warnings(by_id["HK-77838"], request)[0]
    assert "от 3 часов" in store.warnings(by_id["HK-90009"], request)[0]


def test_primary_rejection_uses_fallback_and_caches_accepted_result(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        context = json.loads(body["messages"][1]["content"])
        if body["model"] == "primary":
            cards = [{"id": card["id"], "explanation": "100 выдуманных наград."} for card in context["cards"]]
        else:
            cards = [{"id": card["id"], "explanation": card["approved_options"][-1]} for card in context["cards"]]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"cards": cards}, ensure_ascii=False)}}]})

    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    transport = httpx.MockTransport(handler)
    explainer.providers = [ChatCompletionsProvider(ProviderConfig("astra", "https://primary.test/chat", "primary", ("test-key",), 2.5), transport),
                          ChatCompletionsProvider(ProviderConfig("fallback_llm", "https://secondary.test/chat", "secondary", ("test-key",), 1.5), transport)]
    first = asyncio.run(explainer.explain(request_model, selected))
    assert calls == ["primary", "secondary"]
    assert set(first.source_by_id.values()) == {"fallback_llm"}
    second = asyncio.run(explainer.explain(request_model, selected))
    assert second.cache_hit and first.by_id == second.by_id
    assert calls == ["primary", "secondary"]


def test_every_provider_failure_yields_cached_template(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    explainer.providers = [ChatCompletionsProvider(ProviderConfig("astra", "https://primary.test/chat", "primary", ("test-key",), 2.5), transport)]
    first = asyncio.run(explainer.explain(request_model, selected))
    assert set(first.source_by_id.values()) == {"template"}
    assert asyncio.run(explainer.explain(request_model, selected)).cache_hit


def test_concurrent_results_share_the_same_accepted_text(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    counter = 0

    async def handler(request):
        nonlocal counter
        counter += 1
        index = counter % 2
        await asyncio.sleep(0.002)
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        cards = [{"id": card["id"], "explanation": card["approved_options"][index]} for card in context["cards"]]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"cards": cards}, ensure_ascii=False)}}]})

    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    explainer.providers = [ChatCompletionsProvider(ProviderConfig("astra", "https://primary.test/chat", "primary", ("test-key",), 2.5), httpx.MockTransport(handler))]

    async def run_concurrent():
        return await asyncio.gather(*(explainer.explain(request_model, selected) for _ in range(8)))

    results = asyncio.run(run_concurrent())
    assert all(result.by_id == results[0].by_id for result in results)
