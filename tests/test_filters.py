from datetime import date
from pathlib import Path
from unittest import TestCase

from app.contractor_matching import MatchQuery, filter_eligible_contractors, load_contractors
from app.contractor_matching.filters import rejection_reasons


DATASET = Path(__file__).resolve().parents[1] / "data" / "contractors.json"


class FilterTests(TestCase):
    def test_filters_to_available_contractor(self):
        contractors = load_contractors(DATASET)
        query = MatchQuery(
            city="Astana",
            category="photo",
            event_format="wedding",
            event_date=date(2026, 10, 5),
            budget_kzt=200000,
            guest_count=120,
        )

        results = filter_eligible_contractors(contractors, query)

        self.assertEqual([result.contractor.contractor_id for result in results], ["photo-astana-001"])

    def test_rejection_reasons_are_explicit(self):
        contractor = load_contractors(DATASET)[0]
        query = MatchQuery(
            city="Astana",
            category="photo",
            event_format="wedding",
            event_date=date(2026, 10, 4),
            budget_kzt=100000,
            guest_count=120,
        )

        self.assertEqual(rejection_reasons(contractor, query), ("over_budget", "busy_on_event_date"))
