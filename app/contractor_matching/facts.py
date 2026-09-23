"""Reviewed facts, source verification and deterministic allocation after ranking."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from itertools import product
from pathlib import Path
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ContractorProfile, SearchRequest


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Fact:
    id: str
    quote: str
    claim: str
    tags: tuple[str, ...]
    concept_key: str
    specificity: int


class FactsStore:
    """Fail closed on stale descriptions or unreviewed catalog rows."""

    def __init__(self, data_dir: Path):
        raw = (data_dir / "facts.json").read_text(encoding="utf-8")
        payload = json.loads(raw)
        if payload.get("schema_version") != 2 or not isinstance(payload.get("profiles"), dict):
            raise ValueError("Unsupported facts schema")
        self.version = sha256_text(raw)
        self.records = payload["profiles"]
        rows = []
        for filename in ("original.csv", "team_synthetic.csv"):
            with (data_dir / filename).open(encoding="utf-8-sig", newline="") as stream:
                rows.extend(csv.DictReader(stream))
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("Duplicate catalog IDs")
        if {row["id"] for row in rows} != set(self.records):
            raise ValueError("Facts do not cover the catalog exactly")
        self.by_id: dict[str, tuple[Fact, ...]] = {}
        fact_ids = set()
        for row in rows:
            record = self.records[row["id"]]
            if record.get("description_sha256") != sha256_text(row["description"]):
                raise ValueError(f"Stale description facts for {row['id']}")
            if record.get("quality") not in {"specific", "sparse"}:
                raise ValueError("Invalid facts quality")
            entries = record.get("facts")
            if not isinstance(entries, list) or not entries:
                raise ValueError("Every profile needs a reviewed fact")
            facts = []
            for entry in entries:
                if set(entry) != {"id", "quote", "claim", "source_field", "tags", "concept_key", "specificity"}:
                    raise ValueError("Invalid fact fields")
                if entry["source_field"] != "description" or not entry["quote"] or entry["quote"] not in row["description"]:
                    raise ValueError(f"Unsupported source quote for {row['id']}")
                if not isinstance(entry["claim"], str) or not 1 <= len(entry["claim"].split()) <= 26:
                    raise ValueError("Invalid reviewed claim")
                if not entry["claim"].endswith(".") or re.search(r"[.!?]", entry["claim"][:-1]):
                    raise ValueError("A reviewed claim must be exactly one sentence")
                if not isinstance(entry["concept_key"], str) or not re.fullmatch(r"[a-z][a-z0-9_.]+", entry["concept_key"]):
                    raise ValueError("Invalid reviewed concept key")
                if type(entry["specificity"]) is not int or entry["specificity"] not in {1, 2, 3}:
                    raise ValueError("Invalid reviewed specificity")
                if not isinstance(entry["tags"], list) or not all(isinstance(tag, str) for tag in entry["tags"]):
                    raise ValueError("Invalid fact tags")
                if entry["id"] in fact_ids or not entry["id"].startswith(row["id"] + "-f"):
                    raise ValueError("Invalid or duplicate fact ID")
                fact_ids.add(entry["id"])
                facts.append(Fact(entry["id"], entry["quote"], entry["claim"], tuple(entry["tags"]),
                                  entry["concept_key"], entry["specificity"]))
            self.by_id[row["id"]] = tuple(facts)

    def assign(self, request: SearchRequest, profiles: list[ContractorProfile]) -> dict[str, Fact]:
        """Allocate reviewed, specific concepts without changing ranking.

        Exhaustive allocation is tiny for <=3 finalists. Semantic keys group
        close paraphrases; editorial specificity favours concrete details over
        generic category/language claims. None of this proves competitors lack
        a trait, nor does it alter their rank or invent missing detail.
        """
        if len(profiles) > 3 or len({p.id for p in profiles}) != len(profiles):
            raise ValueError("Explanations accept at most three distinct finalists")
        for profile in profiles:
            if sha256_text(profile.description) != self.records[profile.id]["description_sha256"]:
                raise ValueError(f"Profile changed after facts validation: {profile.id}")
        if not profiles:
            return {}
        frequency = Counter(key for profile in profiles for key in {f.concept_key for f in self.by_id[profile.id]})
        choices = product(*(tuple(enumerate(self.by_id[p.id])) for p in profiles))

        def allocation_key(choice):
            facts = [fact for _, fact in choice]
            return (
                -len({fact.claim.casefold() for fact in facts}),
                -sum(fact.specificity for fact in facts),
                -len({fact.concept_key for fact in facts}),
                sum(frequency[fact.concept_key] for fact in facts),
                -sum(tag in {request.category, request.event_format} for fact in facts for tag in fact.tags),
                tuple(index for index, _ in choice),
            )

        selected = min(choices, key=allocation_key)
        return {profile.id: fact for profile, (_, fact) in zip(profiles, selected)}

    def evidence(self, profile: ContractorProfile, fact: Fact, assigned: dict[str, Fact]) -> dict:
        shared_generic = fact.specificity < 3 and sum(other.concept_key == fact.concept_key for other in assigned.values()) > 1
        limited = self.records[profile.id]["quality"] == "sparse" or fact.specificity == 1 or shared_generic
        return {
            "fact_id": fact.id, "claim": fact.claim, "quote": fact.quote,
            "source_field": "description", "description_sha256": self.records[profile.id]["description_sha256"],
            "concept_key": fact.concept_key, "specificity": fact.specificity,
            "claim_status": "synthetic" if profile.synthetic else "self_reported",
            "distinction": "limited" if limited else "specific",
        }

    def warnings(self, profile: ContractorProfile, request: SearchRequest) -> list[str]:
        warnings = []
        if self.records[profile.id]["quality"] == "sparse":
            warnings.append("В описании мало конкретных деталей; состав услуг и портфолио требуют уточнения.")
        if profile.id == "HK-77838" and request.language == "русский":
            warnings.append("В поле языков указан русский, но текст описания упоминает только казахский; язык стоит подтвердить.")
        if profile.id == "HK-90009" and request.duration is not None and request.duration < 3:
            warnings.append("Описание указывает аренду от 3 часов; минимальная длительность не входит в жёсткие фильтры задания.")
        return warnings
