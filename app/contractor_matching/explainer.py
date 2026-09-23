"""Fact-grounded constrained wording with stable persistent fallback."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .cache import ResponseCache
from .facts import Fact, FactsStore, sha256_text
from .providers import ProviderError, configured_providers

if TYPE_CHECKING:
    from .models import ContractorProfile, SearchRequest

EXPLAINER_VERSION = "bounded-reviewed-v2"
CLICHES = ("идеально подходит", "лучший выбор", "отличный выбор", "высокое качество",
           "индивидуальный подход", "профессионал своего дела", "настоящий мастер",
           "сделает праздник незабываемым")


@dataclass
class ExplanationBatch:
    by_id: dict[str, str]
    source_by_id: dict[str, str]
    cache_hit: bool
    warnings_by_id: dict[str, list[str]]


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def approved_options(request: SearchRequest, profile: ContractorProfile, fact: Fact, *, sparse: bool = False) -> list[str]:
    value = profile.price_from_kzt
    price = (f"{value:,.0f}" if value == int(value) else f"{value:,.2f}").replace(",", " ")
    conditions = f"Формат «{request.event_format}» заявлен; цена от {price} ₸ укладывается в бюджет."
    if sparse and profile.max_hours is not None:
        conditions = f"В каталоге для «{request.event_format}»: до {profile.max_hours} ч, от {price} ₸ в рамках бюджета."
    variants = [f"{fact.claim} {conditions}", f"{conditions} {fact.claim}"]
    if not sparse and request.language and request.language in profile.languages:
        variants.append(f"{fact.claim} Для формата «{request.event_format}» указан язык {request.language}; цена от {price} ₸ в пределах бюджета.")
    result = []
    for text in variants:
        if word_count(text) <= 45 and not any(word in text.casefold() for word in CLICHES):
            if text not in result:
                result.append(text)
    if not result:
        raise ValueError(f"Reviewed explanation exceeds output contract: {profile.id}")
    return result


def validate_response(payload: dict, options: dict[str, list[str]]) -> dict[str, str]:
    """Closed output vocabulary validates claims, entities and numbers together.

    Regex alone cannot prove arbitrary paraphrases. Any unsupported clause,
    number, entity, ID, repeated ID, missing card or ranking change is rejected.
    """
    if not isinstance(payload, dict) or set(payload) != {"cards"}:
        raise ValueError("Invalid explanation schema")
    cards = payload["cards"]
    if not isinstance(cards, list) or len(cards) != len(options):
        raise ValueError("Missing explanation cards")
    result = {}
    for card, expected_id in zip(cards, options):
        if not isinstance(card, dict) or set(card) != {"id", "explanation"} or card["id"] != expected_id:
            raise ValueError("Invalid explanation ID/order")
        text = card["explanation"]
        if not isinstance(text, str) or text not in options[expected_id] or word_count(text) > 45:
            raise ValueError("Unsupported explanation")
        if any(word in text.casefold() for word in CLICHES):
            raise ValueError("Explanation contains a cliché")
        result[expected_id] = text
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
        payload = {"request": request.model_dump(mode="json"),
                   "profiles": canonical_profiles,
                   "facts": self.facts.version, "version": EXPLAINER_VERSION,
                   "providers": [provider.config.public_identity() for provider in self.providers]}
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    async def explain(self, request: SearchRequest, profiles: list[ContractorProfile]) -> ExplanationBatch:
        assigned = self.facts.assign(request, profiles)
        options = {profile.id: approved_options(request, profile, assigned[profile.id],
                   sparse=self.facts.records[profile.id]["quality"] == "sparse") for profile in profiles}
        key = self._cache_key(request, profiles)
        cached = self.cache.get(key)
        if cached is not None:
            return self._batch_from_cache(cached, options, True)
        # Do not serialize network work globally: database insertion selects the
        # first accepted response across racing processes and instances.
        context = {"request": request.model_dump(mode="json"), "cards": [
            {"id": profile.id, "fact_id": assigned[profile.id].id,
             "source_quote": assigned[profile.id].quote, "approved_options": options[profile.id]}
            for profile in profiles
        ]}
        by_id = {contractor_id: choices[0] for contractor_id, choices in options.items()}
        source = "template"
        if profiles:
            for provider in self.providers:
                try:
                    candidate = await provider.generate(context)
                    by_id = validate_response(candidate, options)
                    source = provider.config.name
                    break
                except (ProviderError, ValueError):
                    continue
        payload = {"by_id": by_id, "source_by_id": {profile.id: source for profile in profiles},
                   "warnings_by_id": {profile.id: self.facts.warnings(profile, request) for profile in profiles}}
        async with self._lock:
            accepted = self.cache.put_if_absent(key, payload)
        return self._batch_from_cache(accepted, options, False)

    @staticmethod
    def _batch_from_cache(payload: dict, options: dict[str, list[str]], cache_hit: bool) -> ExplanationBatch:
        # Never trust persistent cache as a new authority for unsupported prose.
        by_id = validate_response({"cards": [{"id": key, "explanation": payload["by_id"][key]} for key in options]}, options)
        if set(payload["source_by_id"]) != set(options) or any(value not in {"astra", "fallback_llm", "template"} for value in payload["source_by_id"].values()):
            raise ValueError("Invalid cached explanation provenance")
        return ExplanationBatch(by_id=by_id, source_by_id=payload["source_by_id"],
                                warnings_by_id=payload["warnings_by_id"], cache_hit=cache_hit)
