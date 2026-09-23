from datetime import date
from pathlib import Path
from unittest import TestCase

from app.contractor_matching import MatchQuery, build_match_report, load_contractors, rank_contractors


DATASET = Path(__file__).resolve().parents[1] / "data" / "contractors.json"


class RankingTests(TestCase):
    def test_ranking_is_deterministic_and_explainable(self):
        contractors = load_contractors(DATASET)
        query = MatchQuery(
            city="Astana",
            category="host",
            event_format="conference",
            event_date=date(2026, 10, 12),
            budget_kzt=180000,
            guest_count=300,
            required_skills=("english", "kazakh"),
        )

        ranked = rank_contractors(contractors, query)

        self.assertEqual([candidate.contractor.contractor_id for candidate in ranked], ["host-astana-001"])
        self.assertEqual(ranked[0].matched_skills, ("english", "kazakh"))
        self.assertIn("passes_hard_filters", ranked[0].reasons)
        self.assertGreater(ranked[0].score, 120)

    def test_match_report_keeps_rejected_contractors(self):
        contractors = load_contractors(DATASET)
        query = MatchQuery(
            city="Astana",
            category="photo",
            event_format="wedding",
            event_date=date(2026, 10, 4),
            budget_kzt=200000,
            guest_count=120,
            required_skills=("drone",),
        )

        report = build_match_report(contractors, query)

        self.assertEqual(report.recommendations, ())
        rejection_map = {
            result.contractor.contractor_id: result.reasons
            for result in report.rejected
        }
        self.assertIn("busy_on_event_date", rejection_map["photo-astana-001"])
        self.assertIn("city_mismatch", rejection_map["decor-almaty-001"])
