import hashlib
import json
from pathlib import Path
import shutil
import pytest
from contractor_matching.data_loader import load_profiles
from contractor_matching.embeddings import FrozenEmbeddings
from contractor_matching.models import SearchRequest
from contractor_matching.presets import BASE

DATA = Path(__file__).resolve().parents[1] / 'data'


def test_frozen_real_model_vectors_cover_every_allowed_query():
    profiles = load_profiles(DATA)
    embeddings = FrozenEmbeddings(DATA, profiles)
    assert len(embeddings.profiles) == 70
    assert len(embeddings.queries) == 408
    assert all(len(v) == 384 for v in embeddings.profiles.values())
    result = embeddings.similarities(SearchRequest(**BASE), profiles)
    assert len(set(round(v, 5) for v in result.values())) > 60
    assert all(-1 <= v <= 1 for v in result.values())


def test_description_change_invalidates_embedding_instead_of_silent_fallback():
    profiles = load_profiles(DATA)
    profiles[0] = profiles[0].model_copy(update={'description': profiles[0].description + ' Изменено.'})
    with pytest.raises(ValueError, match='Stale embedding'):
        FrozenEmbeddings(DATA, profiles)


def test_artifact_checksum_tampering_fails_startup(tmp_path):
    shutil.copy(DATA / 'embeddings_manifest.json', tmp_path)
    raw = (DATA / 'embeddings.json').read_bytes() + b'\n'
    (tmp_path / 'embeddings.json').write_bytes(raw)
    with pytest.raises(ValueError, match='checksum mismatch'):
        FrozenEmbeddings(tmp_path, load_profiles(DATA))
