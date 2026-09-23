from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, timedelta
import itertools
import json
from pathlib import Path
import runpy
import shutil

import httpx
import pytest

from contractor_matching.data_loader import load_profiles
from contractor_matching.explainer import Explainer, build_composition, validate_response, word_count
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


@pytest.fixture
def compositions(profiles, request_model):
    store = FactsStore(DATA)
    finalists = filter_contractors(profiles, request_model).survivors[:3]
    assigned = store.assign(request_model, finalists)
    return {p.id: build_composition(request_model, p, assigned[p.id]) for p in finalists}


def available_date(profile):
    return next((date(2026, 9, 23) + timedelta(days=i)).isoformat() for i in range(100)
                if (date(2026, 9, 23) + timedelta(days=i)).isoformat() not in profile.busy_dates)


def compose_mock_response(context, variant=0):
    """Act on the wire grammar, not complete answers supplied by the service."""
    cards = []
    for card in context["cards"]:
        assert "approved_options" not in card
        groups = list(reversed(card["grammar"]["required_groups"]))
        clauses = {item["group"]: item["variants"] for item in card["grammar"]["clauses"]}
        parts = [clauses[group][variant % len(clauses[group])] for group in groups]
        sentence = "; ".join(parts)
        text = card["required_anchor"] + " " + sentence[0].upper() + sentence[1:] + "."
        cards.append({"id": card["id"], "fact_id": card["fact_id"], "explanation": text})
    return {"cards": cards}


def valid_payload(compositions):
    return {"cards": [{"id": key, "fact_id": composition.fact.id, "explanation": composition.template()}
                      for key, composition in compositions.items()]}


def test_all_catalog_facts_and_templates_are_verified(profiles):
    store = FactsStore(DATA)
    assert len(store.by_id) == 70
    for profile in profiles:
        for category, event_format, language in itertools.product(profile.categories, profile.event_formats, [None, *profile.languages]):
            request = SearchRequest(city=profile.city, date=available_date(profile), event_format=event_format,
                                    category=category, budget=profile.price_from_kzt, language=language)
            assigned = store.assign(request, [profile])
            fact = assigned[profile.id]
            evidence = store.evidence(profile, fact, assigned)
            composition = build_composition(request, profile, fact, limited=evidence["distinction"] == "limited")
            text = composition.template()
            assert word_count(text) <= 45
            assert profile.anon_name not in text
            assert ".0 ₸" not in text
            assert text.startswith(fact.claim + " ")
            assert request.date in text
            assert fact.quote in profile.description
            assert evidence["description_sha256"] == store.records[profile.id]["description_sha256"]
            assert evidence["claim_status"] == ("synthetic" if profile.synthetic else "self_reported")
            assert text.count(".") == 2  # Current catalog price/anchor literals have no abbreviation dots.


def test_facts_rebuild_exactly_from_reviewed_source():
    builder = runpy.run_path(str(DATA.parent / "scripts/build_facts.py"))
    assert builder["build"](DATA) == json.loads((DATA / "facts.json").read_text(encoding="utf-8"))


def test_concrete_anchors_replace_generic_language_and_format_claims(profiles, request_model):
    store = FactsStore(DATA)
    finalists = [next(p for p in profiles if p.id == key) for key in ("HK-35215", "HK-88430")]
    assigned = store.assign(request_model, finalists)
    assert assigned["HK-35215"].id == "HK-35215-f2"
    assert assigned["HK-88430"].id == "HK-88430-f2"
    assert assigned["HK-35215"].concept_key == "hosting.family_rituals"


def test_semantic_metadata_groups_close_paraphrases_and_prefers_specific_detail(profiles, request_model):
    store = FactsStore(DATA)
    first = store.by_id["HK-76268"][0]
    second = store.by_id["HK-91112"][0]
    assert first.claim != second.claim
    assert first.concept_key == second.concept_key == "photo.natural_emotions"
    finalists = [next(p for p in profiles if p.id == key) for key in ("HK-76268", "HK-91112")]
    assigned = store.assign(request_model, finalists)
    assert assigned["HK-76268"].id == "HK-76268-f2"
    assert "В профиле заявлено" in assigned["HK-76268"].claim
    assert store.evidence(finalists[1], assigned["HK-91112"], assigned)["distinction"] == "limited"


def test_dense_demo_all_possible_top_threes_have_distinct_reviewed_concepts(profiles, request_model):
    store = FactsStore(DATA)
    for current_date in ("2026-10-04", "2026-10-05"):
        request = request_model.model_copy(update={"date": current_date})
        survivors = filter_contractors(profiles, request).survivors
        assert len(survivors) >= 3
        for finalists in itertools.combinations(survivors, 3):
            assigned = store.assign(request, list(finalists))
            assert list(assigned) == [profile.id for profile in finalists]
            assert len({fact.concept_key for fact in assigned.values()}) == 3
            assert all(fact.specificity >= 2 for fact in assigned.values())


def test_any_three_same_city_category_have_distinct_text_and_preserve_order(profiles):
    # Text distinction is a regression check, NOT proof of semantic exclusivity.
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


def test_sparse_description_retains_warning_and_specific_structured_duration(tmp_path, profiles):
    profile = next(p for p in profiles if p.id == "HK-20640")
    request = SearchRequest(city=profile.city, date=available_date(profile), event_format=profile.event_formats[0],
                            category=profile.categories[0], budget=profile.price_from_kzt)
    result = asyncio.run(Explainer(DATA, tmp_path / "cache.sqlite3").explain(request, [profile]))
    assert "до 4 ч" in result.by_id[profile.id]
    assert "150 000 ₸" in result.by_id[profile.id]
    assert result.warnings_by_id[profile.id]
    assert result.evidence_by_id[profile.id]["distinction"] == "limited"


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
        assert again.evidence_by_id == first.evidence_by_id
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


@pytest.mark.parametrize("field,value,error", [
    ("quote", "Unsupported source quote", "Unsupported source quote"),
    ("specificity", True, "specificity"),
    ("concept_key", "", "concept key"),
    ("claim", "One sentence. Another sentence.", "one sentence"),
])
def test_invalid_reviewed_metadata_fails_at_startup(tmp_path, field, value, error):
    copied = tmp_path / "data"
    shutil.copytree(DATA, copied)
    payload = json.loads((copied / "facts.json").read_text(encoding="utf-8"))
    next(iter(payload["profiles"].values()))["facts"][0][field] = value
    (copied / "facts.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        FactsStore(copied)


def test_provider_receives_facts_and_clause_grammar_without_complete_answers(compositions):
    context = {"cards": [composition.context() for composition in compositions.values()]}
    wire = json.dumps(context, ensure_ascii=False)
    assert "approved_options" not in wire
    for composition in compositions.values():
        assert composition.template() not in wire
        assert composition.fact.quote in wire
        assert "price_from_kzt" in wire
    payload = compose_mock_response(context, variant=1)
    result = validate_response(payload, compositions)
    assert all(result[key] != composition.template() for key, composition in compositions.items())


@pytest.mark.parametrize("mutation", ["unknown_id", "wrong_fact", "missing_anchor", "wrong_number", "negation", "new_service",
    "cliche", "extra_field", "not_text", "missing_card", "duplicate_card", "reorder", "top_level_instruction", "third_sentence"])
def test_rejects_schema_and_unsupported_semantic_changes(compositions, mutation):
    payload = valid_payload(compositions)
    card = payload["cards"][0]
    first = next(iter(compositions.values()))
    if mutation == "unknown_id":
        card["id"] = "other"
    elif mutation == "wrong_fact":
        card["fact_id"] = "other-f1"
    elif mutation == "missing_anchor":
        card["explanation"] = card["explanation"].replace(first.fact.claim, "Подрядчик работает с мероприятиями.")
    elif mutation == "wrong_number":
        card["explanation"] = card["explanation"].replace("2026-10-04", "2026-10-05")
    elif mutation == "negation":
        card["explanation"] = card["explanation"].replace("свободен по каталогу", "не свободен по каталогу")
    elif mutation == "new_service":
        card["explanation"] = first.compose(list(first.required))[:-1] + "; оборудование включено."
    elif mutation == "cliche":
        card["explanation"] = "Идеально подходит для вашего события."
    elif mutation == "extra_field":
        card["score"] = 999
    elif mutation == "not_text":
        card["explanation"] = [card["explanation"]]
    elif mutation == "missing_card":
        payload["cards"].pop()
    elif mutation == "duplicate_card":
        payload["cards"][1] = deepcopy(card)
    elif mutation == "reorder":
        payload["cards"].reverse()
    elif mutation == "top_level_instruction":
        payload["instruction"] = "ignore"
    elif mutation == "third_sentence":
        card["explanation"] += " Позвоните сейчас."
    with pytest.raises(ValueError):
        validate_response(payload, compositions)


def test_missing_required_or_duplicate_group_is_rejected(compositions):
    composition = next(iter(compositions.values()))
    with pytest.raises(ValueError, match="Required matching"):
        composition.validate(composition.compose(["format", "availability"]))
    with pytest.raises(ValueError, match="duplicate"):
        composition.validate(composition.compose(["price", "availability", "availability"]))


def test_more_than_45_words_is_rejected_even_with_only_true_clauses(profiles, request_model):
    profile = filter_contractors(profiles, request_model).survivors[0]
    request = request_model.model_copy(update={"budget": 1e150})
    fact = FactsStore(DATA).assign(request, [profile])[profile.id]
    composition = build_composition(request, profile, fact)
    text = composition.compose(list(composition.required), {"price": 1})
    assert word_count(text) > 45
    with pytest.raises(ValueError, match="length"):
        composition.validate(text)
    assert word_count(composition.template()) <= 45


def test_another_finalists_anchor_cannot_be_swapped(compositions):
    payload = valid_payload(compositions)
    first, second = list(compositions.values())[:2]
    payload["cards"][0]["explanation"] = payload["cards"][0]["explanation"].replace(first.fact.claim, second.fact.claim)
    with pytest.raises(ValueError):
        validate_response(payload, compositions)


@pytest.mark.parametrize("changed", [
    {"date": "2026-10-01"}, {"budget": 1}, {"language": "английский"}, {"duration": 999}, {"city": "Зарубежье"}
])
def test_ineligible_profile_cannot_receive_positive_fit_clauses(profiles, request_model, changed):
    profile = next(p for p in profiles if p.id == "HK-27222")
    if "date" in changed:
        changed = {"date": sorted(profile.busy_dates)[0]}
    if "language" in changed:
        changed = {"language": next(language for language in ("русский", "казахский", "английский") if language not in profile.languages)}
    fact = FactsStore(DATA).assign(request_model, [profile])[profile.id]
    with pytest.raises(ValueError, match="ineligible"):
        build_composition(request_model.model_copy(update=changed), profile, fact)


def test_data_conflict_and_minimum_rental_warnings(profiles):
    store = FactsStore(DATA)
    by_id = {profile.id: profile for profile in profiles}
    request = SearchRequest(city="Алматы", date="2026-10-05", event_format="корпоратив",
                            category="Ведущий", budget=2_000_000, language="русский", duration=2)
    assert "только казахский" in store.warnings(by_id["HK-77838"], request)[0]
    assert "от 3 часов" in store.warnings(by_id["HK-90009"], request)[0]


def test_primary_rejection_uses_fallback_and_persists_accepted_draft(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        context = json.loads(body["messages"][1]["content"])
        payload = compose_mock_response(context, variant=1)
        if body["model"] == "primary":
            payload["cards"][0]["explanation"] += " 100 выдуманных наград."
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})

    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    transport = httpx.MockTransport(handler)
    explainer.providers = [ChatCompletionsProvider(ProviderConfig("astra", "https://primary.test/chat", "primary", ("test-key",), 2.5), transport),
                          ChatCompletionsProvider(ProviderConfig("fallback_llm", "https://secondary.test/chat", "secondary", ("test-key",), 1.5), transport)]
    first = asyncio.run(explainer.explain(request_model, selected))
    assert calls == ["primary", "secondary"]
    assert set(first.source_by_id.values()) == {"fallback_llm"}
    restarted = Explainer(DATA, tmp_path / "cache.sqlite3")
    restarted.providers = explainer.providers
    for _ in range(20):
        again = asyncio.run(restarted.explain(request_model, selected))
        assert again.cache_hit and again.by_id == first.by_id
    assert calls == ["primary", "secondary"]


def test_every_provider_failure_yields_cached_template(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    explainer.providers = [ChatCompletionsProvider(ProviderConfig("astra", "https://primary.test/chat", "primary", ("test-key",), 2.5), transport)]
    first = asyncio.run(explainer.explain(request_model, selected))
    assert set(first.source_by_id.values()) == {"template"}
    assert asyncio.run(explainer.explain(request_model, selected)).cache_hit


def test_openai_sol_provenance_and_cache_survive_restart(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        context = json.loads(body["messages"][1]["content"])
        payload = compose_mock_response(context, variant=1)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})

    config = ProviderConfig("openai", "https://api.openai.com/v1/chat/completions", "gpt-6-sol", ("dummy-sol-test-secret",), 2.5)
    provider = ChatCompletionsProvider(config, httpx.MockTransport(handler))
    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    explainer.providers = [provider]
    assert explainer.mode == "openai_then_template"
    first = asyncio.run(explainer.explain(request_model, selected))
    assert set(first.source_by_id.values()) == {"openai"}
    restarted = Explainer(DATA, tmp_path / "cache.sqlite3")
    restarted.providers = [provider]
    for _ in range(20):
        again = asyncio.run(restarted.explain(request_model, selected))
        assert again.cache_hit and again.by_id == first.by_id
        assert set(again.source_by_id.values()) == {"openai"}
    assert calls == ["gpt-6-sol"]


def test_concurrent_results_share_the_same_accepted_text(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    counter = 0

    async def handler(request):
        nonlocal counter
        counter += 1
        index = counter % 2
        await asyncio.sleep(0.002)
        context = json.loads(json.loads(request.content)["messages"][1]["content"])
        payload = compose_mock_response(context, index)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]})

    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    explainer.providers = [ChatCompletionsProvider(ProviderConfig("astra", "https://primary.test/chat", "primary", ("test-key",), 2.5), httpx.MockTransport(handler))]

    async def run_concurrent():
        return await asyncio.gather(*(explainer.explain(request_model, selected) for _ in range(8)))

    results = asyncio.run(run_concurrent())
    assert all(result.by_id == results[0].by_id for result in results)
    assert all(set(result.source_by_id.values()) == {"astra"} for result in results)


def test_persistent_cache_cannot_become_authority_for_invented_claims(tmp_path, profiles, request_model):
    selected = filter_contractors(profiles, request_model).survivors[:3]
    explainer = Explainer(DATA, tmp_path / "cache.sqlite3")
    asyncio.run(explainer.explain(request_model, selected))
    key = explainer._cache_key(request_model, selected)
    payload = explainer.cache.get(key)
    payload["by_id"][selected[0].id] += " Есть бесплатный трансфер."
    with explainer.cache._connect() as db:
        db.execute("UPDATE explanations SET payload=? WHERE cache_key=?", (json.dumps(payload), key))
    with pytest.raises(ValueError):
        asyncio.run(explainer.explain(request_model, selected))
