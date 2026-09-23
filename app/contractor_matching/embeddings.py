"""Frozen pretrained embeddings. No network, LLM, or model switching at request time."""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path

from .models import CATEGORIES, EVENT_FORMATS, LANGUAGES, ContractorProfile, SearchRequest

MODEL = 'Xenova/multilingual-e5-small'
REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
DIMENSION = 384
TEXT_VERSION = 'ru-catalog-v1'


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def query_key(category: str, event_format: str, language: str | None) -> str:
    return json.dumps([category, event_format, language], ensure_ascii=False, separators=(',', ':'))


def query_text(category: str, event_format: str, language: str | None) -> str:
    return f'query: Подрядчик категории {category} для мероприятия «{event_format}». Язык: {language or "любой"}.'


def profile_text(profile: ContractorProfile, description: str | None = None) -> str:
    return (f'passage: Категории: {", ".join(sorted(profile.categories))}. '
            f'Форматы: {", ".join(sorted(profile.event_formats))}. '
            f'Языки: {", ".join(sorted(profile.languages))}. '
            f'Описание: {profile.description if description is None else description}')


def query_space():
    return itertools.product(sorted(CATEGORIES), sorted(EVENT_FORMATS), [None, *sorted(LANGUAGES)])


def _unit(values: list[float]) -> tuple[float, ...]:
    if len(values) != DIMENSION or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        raise ValueError('Invalid embedding vector')
    norm = math.sqrt(math.fsum(x * x for x in values))
    if norm < 1e-8 or abs(norm - 1) > 0.001:
        raise ValueError('Embedding must be L2 normalized')
    return tuple(x / norm for x in values)


class FrozenEmbeddings:
    mode = 'frozen_semantic'
    model = MODEL

    def __init__(self, data_dir: Path, profiles: list[ContractorProfile]):
        raw = (data_dir / 'embeddings.json').read_bytes()
        manifest = json.loads((data_dir / 'embeddings_manifest.json').read_text(encoding='utf-8'))
        self.version = hashlib.sha256(raw).hexdigest()
        if self.version != manifest['artifact_sha256']:
            raise ValueError('Embedding artifact checksum mismatch; rebuild explicitly')
        artifact = json.loads(raw)
        if (artifact['model'], artifact['revision'], artifact['text_version']) != (MODEL, REVISION, TEXT_VERSION):
            raise ValueError('Embedding model/version mismatch')
        if set(artifact['profiles']) != {p.id for p in profiles}:
            raise ValueError('Catalog IDs and embeddings differ; rebuild explicitly')
        self.profiles = {}
        for p in profiles:
            entry = artifact['profiles'][p.id]
            if entry['description_sha256'] != digest(p.description) or entry['input_sha256'] != digest(profile_text(p)):
                raise ValueError(f'Stale embedding for {p.id}; run scripts/build_embeddings.py')
            self.profiles[p.id] = _unit(entry['vector'])
        expected = {query_key(*args): query_text(*args) for args in query_space()}
        if set(artifact['queries']) != set(expected):
            raise ValueError('Incomplete semantic query cache')
        self.queries = {}
        for key, text in expected.items():
            entry = artifact['queries'][key]
            if entry['input_sha256'] != digest(text):
                raise ValueError('Stale query template')
            self.queries[key] = _unit(entry['vector'])

    def similarities(self, request: SearchRequest, profiles: list[ContractorProfile]) -> dict[str, float]:
        query = self.queries[query_key(request.category, request.event_format, request.language)]
        return {p.id: max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(query, self.profiles[p.id])))) for p in profiles}
