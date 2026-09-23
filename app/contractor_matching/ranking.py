from __future__ import annotations

from dataclasses import dataclass
from math import log10
from typing import Iterable

from .filters import EligibilityResult, rejection_reasons
from .models import Contractor, MatchQuery


@dataclass(frozen=True)
class MatchCandidate:
    contractor: Contractor
    score: float
    matched_skills: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MatchReport:
    recommendations: tuple[MatchCandidate, ...]
    rejected: tuple[EligibilityResult, ...]


def _skill_overlap(contractor: Contractor, query: MatchQuery) -> tuple[str, ...]:
    requested = {skill.casefold(): skill for skill in query.required_skills}
    contractor_skills = {skill.casefold(): skill for skill in contractor.skills}
    return tuple(
        requested[key]
        for key in sorted(requested)
        if key in contractor_skills
    )


def _score_candidate(contractor: Contractor, query: MatchQuery, matched_skills: tuple[str, ...]) -> float:
    rating_score = contractor.rating * 20
    experience_score = min(log10(contractor.completed_events + 1) * 8, 18)
    skill_score = 0.0
    if query.required_skills:
        skill_score = (len(matched_skills) / len(query.required_skills)) * 18
    budget_fit_score = max((query.budget_kzt - contractor.price_from_kzt) / query.budget_kzt, 0) * 12
    capacity_fit_score = min((contractor.max_guests - query.guest_count) / max(query.guest_count, 1), 1) * 6
    return round(rating_score + experience_score + skill_score + budget_fit_score + capacity_fit_score, 2)


def _positive_reasons(contractor: Contractor, query: MatchQuery, matched_skills: tuple[str, ...]) -> tuple[str, ...]:
    reasons = [
        "passes_hard_filters",
        f"rating_{contractor.rating:.1f}",
        f"completed_events_{contractor.completed_events}",
    ]
    if matched_skills:
        reasons.append("matches_skills_" + "_".join(matched_skills))
    if contractor.price_from_kzt <= query.budget_kzt:
        reasons.append("within_budget")
    return tuple(reasons)


def rank_contractors(contractors: Iterable[Contractor], query: MatchQuery) -> tuple[MatchCandidate, ...]:
    candidates: list[MatchCandidate] = []

    for contractor in contractors:
        if rejection_reasons(contractor, query):
            continue
        matched_skills = _skill_overlap(contractor, query)
        candidates.append(
            MatchCandidate(
                contractor=contractor,
                score=_score_candidate(contractor, query, matched_skills),
                matched_skills=matched_skills,
                reasons=_positive_reasons(contractor, query, matched_skills),
            )
        )

    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                candidate.contractor.price_from_kzt,
                candidate.contractor.contractor_id,
            ),
        )
    )


def build_match_report(contractors: Iterable[Contractor], query: MatchQuery) -> MatchReport:
    contractor_list = list(contractors)
    rejected = tuple(
        EligibilityResult(contractor=contractor, eligible=False, reasons=reasons)
        for contractor in contractor_list
        if (reasons := rejection_reasons(contractor, query))
    )
    return MatchReport(
        recommendations=rank_contractors(contractor_list, query),
        rejected=rejected,
    )
