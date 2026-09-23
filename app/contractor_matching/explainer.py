"""Constrained LLM composition from reviewed facts and true matching clauses.

The model drafts a two-sentence explanation, not a choice of complete canned
answers. Its factual anchor is fixed; its matching sentence follows a small
grammar. Acceptance proves membership in that grammar, not arbitrary natural
language entailment. Source claims remain self-reported catalog information.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

from .cache import ResponseCache
from .facts import Fact, FactsStore, sha256_text
from .providers import ProviderError, configured_providers

if TYPE_CHECKING:
    from .models import ContractorProfile, SearchRequest

EXPLAINER_VERSION = "grounded-composition-v3"
CLICHES = ("идеально подходит", "лучший выбор", "отличный выбор", "высокое качество",
           "индивидуальный подход", "профессионал своего дела", "настоящий мастер",
           "сделает праздник незабываемым")


@dataclass
class ExplanationBatch:
    by_id: dict[str, str]
    source_by_id: dict[str, str]
    cache_hit: bool
    warnings_by_id: dict[str, list[str]]
    evidence_by_id: dict[str, dict] = field(default_factory=dict)


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _money(value: float) -> str:
    amount = Decimal(str(value))
    text = format(amount, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ")


@dataclass(frozen=True)
class Composition:
    contractor_id: str
    fact: Fact
    clauses: dict[str, tuple[str, ...]]
    required: tuple[str, ...]
    matches: dict

    def context(self) -> dict:
        return {
            "id": self.contractor_id, "fact_id": self.fact.id,
            "required_anchor": self.fact.claim, "source_quote": self.fact.quote,
            "matches": self.matches,
            "grammar": {
                "required_groups": list(self.required),
                "clauses": [{"group": group, "variants": list(variants)} for group, variants in self.clauses.items()],
                "separator": "; ", "min_clauses": len(self.required), "max_clauses": 4,
                "max_words": 45,
            },
        }

    def compose(self, groups: list[str], variants: dict[str, int] | None = None) -> str:
        variants = variants or {}
        pieces = [self.clauses[group][variants.get(group, 0)] for group in groups]
        sentence = "; ".join(pieces)
        return f"{self.fact.claim} {sentence[0].upper()}{sentence[1:]}."

    def template(self) -> str:
        groups = list(self.required)
        for group in ("format", "language"):
            if group in self.clauses and group not in groups and len(groups) < 4:
                candidate = self.compose([*groups, group])
                if word_count(candidate) <= 45:
                    groups.append(group)
        text = self.compose(groups)
        self.validate(text)
        return text

    def validate(self, text: str) -> None:
        if not isinstance(text, str) or text != text.strip() or word_count(text) > 45:
            raise ValueError("Invalid explanation length")
        if any(cliche in text.casefold() for cliche in CLICHES):
            raise ValueError("Explanation contains a cliché")
        prefix = self.fact.claim + " "
        if not text.startswith(prefix) or not text.endswith("."):
            raise ValueError("Required factual anchor missing or changed")
        sentence = text[len(prefix):-1]
        if not sentence or sentence[0] != sentence[0].upper():
            raise ValueError("Matching sentence must start with a capital")
        parts = (sentence[0].lower() + sentence[1:]).split("; ")
        if not len(self.required) <= len(parts) <= 4:
            raise ValueError("Invalid matching clause count")
        groups = []
        for part in parts:
            matching = [group for group, variants in self.clauses.items() if part in variants]
            if len(matching) != 1 or matching[0] in groups:
                raise ValueError("Unsupported or duplicate matching clause")
            groups.append(matching[0])
        if not set(self.required).issubset(groups):
            raise ValueError("Required matching clause missing")


def build_composition(request: SearchRequest, profile: ContractorProfile, fact: Fact, *, limited: bool = False) -> Composition:
    """Only surviving candidates may receive truthful availability/fit clauses."""
    if (profile.city != request.city or request.category not in profile.categories
            or request.date in profile.busy_dates or profile.price_from_kzt > request.budget
            or request.event_format not in profile.event_formats
            or (request.language and request.language not in profile.languages)
            or (request.duration is not None and profile.max_hours is not None and request.duration > profile.max_hours)):
        raise ValueError("Cannot explain an ineligible contractor as a match")
    price = _money(profile.price_from_kzt)
    budget = _money(request.budget)
    clauses = {
        "price": (f"цена от {price} ₸ в рамках бюджета", f"от {price} ₸ при бюджете {budget} ₸"),
        "availability": (f"на {request.date} свободен по каталогу", f"на {request.date} занятость не отмечена"),
        "format": (f"формат «{request.event_format}» указан в профиле", f"в каталоге заявлен формат «{request.event_format}»"),
    }
    if request.language:
        clauses["language"] = (f"в профиле указан язык {request.language}", f"язык {request.language} заявлен в каталоге")
    if profile.max_hours is not None:
        clauses["duration"] = (f"до {profile.max_hours} ч по каталогу", f"лимит длительности — {profile.max_hours} ч")
    elif request.duration is not None:
        clauses["duration"] = ("в каталоге нет ограничения длительности", "ограничение длительности в каталоге не задано")
    required = ("price", "availability", "duration") if limited and profile.max_hours is not None else ("price", "availability")
    return Composition(profile.id, fact, clauses, required, {
        "price_from_kzt": profile.price_from_kzt, "budget_kzt": request.budget,
        "available_on": request.date, "event_format": request.event_format,
        "requested_language": request.language, "requested_hours": request.duration,
        "catalog_max_hours": profile.max_hours,
    })


def validate_response(payload: dict, compositions: dict[str, Composition]) -> dict[str, str]:
    """Validate fixed IDs/anchors and a closed grammar of true fit conditions.

    This deliberately does NOT claim that regex can verify arbitrary prose.
    Exact grammar membership prevents added claims, entities, negations and
    numbers. The reviewed anchor is separately tied to its source quote/hash.
    """
    if not isinstance(payload, dict) or set(payload) != {"cards"}:
        raise ValueError("Invalid explanation schema")
    cards = payload["cards"]
    if not isinstance(cards, list) or len(cards) != len(compositions):
        raise ValueError("Missing explanation cards")
    result = {}
    for card, (expected_id, composition) in zip(cards, compositions.items()):
        if not isinstance(card, dict) or set(card) != {"id", "fact_id", "explanation"} or card["id"] != expected_id:
            raise ValueError("Invalid explanation ID/order")
        if card["fact_id"] != composition.fact.id:
            raise ValueError("Invalid explanation fact ID")
        composition.validate(card["explanation"])
        result[expected_id] = card["explanation"]
    return result


class Explainer:
    def __init__(self, data_dir: Path, cache_path: Path):
        self.facts = FactsStore(Path(data_dir))
        self.cache = ResponseCache(Path(cache_path))
        self.providers = configured_providers()
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        names = [provider.config.name for provider in self.providers]
        return "_then_".join(names + ["template"])

    def _cache_key(self, request: SearchRequest, profiles: list[ContractorProfile]) -> str:
        canonical_profiles = []
        for profile in profiles:
            record = profile.model_dump(mode="json")
            record["busy_dates"] = sorted(record["busy_dates"])
            canonical_profiles.append(record)
        payload = {"request": request.model_dump(mode="json"), "profiles": canonical_profiles,
                   "facts": self.facts.version, "version": EXPLAINER_VERSION,
                   "providers": [provider.config.public_identity() for provider in self.providers]}
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    async def explain(self, request: SearchRequest, profiles: list[ContractorProfile]) -> ExplanationBatch:
        assigned = self.facts.assign(request, profiles)
        evidence = {profile.id: self.facts.evidence(profile, assigned[profile.id], assigned) for profile in profiles}
        compositions = {profile.id: build_composition(request, profile, assigned[profile.id],
                        limited=evidence[profile.id]["distinction"] == "limited") for profile in profiles}
        warnings = {profile.id: self.facts.warnings(profile, request) for profile in profiles}
        for profile in profiles:
            if evidence[profile.id]["distinction"] == "limited" and not warnings[profile.id]:
                warnings[profile.id].append("Описание содержит мало отличительных деталей; сравните цену, длительность и уточните портфолио.")
        key = self._cache_key(request, profiles)
        cached = self.cache.get(key)
        if cached is not None:
            return self._batch_from_cache(cached, compositions, warnings, evidence, True)
        context = {"task": "compose_grounded_explanations", "cards": [composition.context() for composition in compositions.values()]}
        by_id = {contractor_id: composition.template() for contractor_id, composition in compositions.items()}
        source = "template"
        if profiles:
            for provider in self.providers:
                try:
                    candidate = await provider.generate(context)
                    by_id = validate_response(candidate, compositions)
                    source = provider.config.name
                    break
                except (ProviderError, ValueError):
                    continue
        payload = {"by_id": by_id, "source_by_id": {profile.id: source for profile in profiles},
                   "fact_ids": {profile.id: assigned[profile.id].id for profile in profiles}}
        # Separate calls may race; SQLite chooses the first accepted result and
        # every caller receives it. Network work is not serialized globally.
        async with self._lock:
            accepted = self.cache.put_if_absent(key, payload)
        return self._batch_from_cache(accepted, compositions, warnings, evidence, False)

    @staticmethod
    def _batch_from_cache(payload: dict, compositions: dict[str, Composition], warnings: dict, evidence: dict, cache_hit: bool) -> ExplanationBatch:
        by_id = validate_response({"cards": [{"id": key, "fact_id": payload["fact_ids"][key],
                    "explanation": payload["by_id"][key]} for key in compositions]}, compositions)
        if set(payload["source_by_id"]) != set(compositions) or any(value not in {"astra", "openai", "fallback_llm", "template"} for value in payload["source_by_id"].values()):
            raise ValueError("Invalid cached explanation provenance")
        return ExplanationBatch(by_id, payload["source_by_id"], cache_hit, warnings, evidence)
