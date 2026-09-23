"""Frozen-embedding ranking applied only to hard-filter survivors."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .models import ContractorProfile, SearchRequest


@dataclass(frozen=True)
class RankedCandidate:
    profile: ContractorProfile
    score: float
    score_breakdown: dict[str, float]


def rank_contractors(
    survivors: Iterable[ContractorProfile],
    request: SearchRequest,
    similarities: dict[str, float],
) -> list[RankedCandidate]:
    ranked: list[RankedCandidate] = []
    for profile in survivors:
        cosine = similarities[profile.id]
        if isinstance(cosine, bool) or not math.isfinite(cosine) or not -1 <= cosine <= 1:
            raise ValueError(f"Invalid cosine similarity for {profile.id}")
        price_ratio = profile.price_from_kzt / request.budget
        language = 0.1 if request.language is None else (0.2 if request.language in profile.languages else 0.0)
        semantic = 0.5 * cosine
        budget = 0.3 * price_ratio
        synthetic_penalty = -0.05 if profile.synthetic else 0.0
        score = semantic + budget + language + synthetic_penalty
        ranked.append(RankedCandidate(profile, score, {
            "cosine": cosine,
            "semantic": semantic,
            "price_ratio": price_ratio,
            "budget": budget,
            "language": language,
            "synthetic_penalty": synthetic_penalty,
        }))
    return sorted(ranked, key=lambda candidate: (
        -round(candidate.score, 4), candidate.profile.price_from_kzt, candidate.profile.id,
    ))[:3]