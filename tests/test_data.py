import csv
import hashlib
from pathlib import Path
import runpy
import shutil

import pytest

from contractor_matching.data_loader import load_profiles
from contractor_matching.models import CATEGORIES, DATE_MAX, DATE_MIN

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def test_original_source_is_byte_identical_and_catalog_has_expected_provenance():
    assert hashlib.sha256((DATA / "original.csv").read_bytes()).hexdigest() == "6a724b6b7dfb5973343e68ba18dadb60fc807d87e3d78f03ee86fb26cb089f7d"
    profiles = load_profiles(DATA)
    assert len(profiles) == len({profile.id for profile in profiles}) == 70
    assert sum(profile.origin == "source_dataset" for profile in profiles) == 66
    assert sum(profile.origin == "team_extension" for profile in profiles) == 4
    assert sum(profile.synthetic for profile in profiles) == 17
    assert sum(profile.city_imputed for profile in profiles) == 8
    assert sum(profile.price_imputed for profile in profiles) == 18
    assert sum(profile.city == "Астана" for profile in profiles) == 19
    assert [profile.id for profile in profiles] == sorted(profile.id for profile in profiles)


def test_extension_adds_only_planned_categories_and_preserves_foreign_city():
    profiles = load_profiles(DATA)
    additions = [profile for profile in profiles if profile.origin == "team_extension"]
    assert all(profile.synthetic and profile.city == "Астана" for profile in additions)
    assert sum(profile.categories == ["Декоратор"] for profile in additions) == 3
    assert sum(profile.categories == ["Инструменталист"] for profile in additions) == 1
    assert all(not profile.city_imputed and not profile.price_imputed for profile in additions)
    assert all(category in CATEGORIES for profile in profiles for category in profile.categories)
    foreign = [profile for profile in profiles if profile.city == "Зарубежье"]
    assert len(foreign) == 1 and foreign[0].categories == ["Фотограф"]
    assert all(DATE_MIN <= day <= DATE_MAX for profile in profiles for day in profile.busy_dates)


def test_generated_calendar_and_profiles_are_reproducible(tmp_path):
    script = runpy.run_path(str(ROOT / "scripts" / "generate_synthetic.py"))
    destination = tmp_path / "team_synthetic.csv"
    script["write_synthetic"](destination)
    assert destination.read_bytes() == (DATA / "team_synthetic.csv").read_bytes()


def test_duplicate_id_fails_instead_of_overwriting_a_profile(tmp_path):
    shutil.copyfile(DATA / "original.csv", tmp_path / "original.csv")
    with (DATA / "original.csv").open(encoding="utf-8-sig", newline="") as stream:
        original_id = next(csv.DictReader(stream))["id"]
    with (DATA / "team_synthetic.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames
        rows = list(reader)
    rows[0]["id"] = original_id
    with (tmp_path / "team_synthetic.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="Duplicate profile ID"):
        load_profiles(tmp_path)
