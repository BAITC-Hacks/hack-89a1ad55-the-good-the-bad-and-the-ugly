"""Read the preserved source CSV and the explicitly separate team extension."""
from __future__ import annotations

import csv
from pathlib import Path

from .models import ContractorProfile

CSV_COLUMNS = (
    "id", "anon_name", "categories", "city", "city_imputed", "synthetic",
    "price_from_kzt", "price_imputed", "event_formats", "languages", "max_hours",
    "busy_dates", "description",
)


def load_profiles(data_dir: Path) -> list[ContractorProfile]:
    profiles: list[ContractorProfile] = []
    seen: set[str] = set()
    for filename, origin in (("original.csv", "source_dataset"), ("team_synthetic.csv", "team_extension")):
        path = Path(data_dir) / filename
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(CSV_COLUMNS):
                raise ValueError(f"{filename}: unexpected CSV columns")
            for line, row in enumerate(reader, start=2):
                try:
                    profile = ContractorProfile.model_validate({**row, "origin": origin})
                except ValueError as exc:
                    raise ValueError(f"{filename}, row {line}: invalid profile: {exc}") from exc
                if profile.id in seen:
                    raise ValueError(f"Duplicate profile ID: {profile.id}")
                if origin == "team_extension" and not profile.synthetic:
                    raise ValueError(f"Team extension must be marked synthetic: {profile.id}")
                seen.add(profile.id)
                profiles.append(profile)
    return sorted(profiles, key=lambda profile: profile.id)