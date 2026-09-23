from __future__ import annotations
from pathlib import Path
from time import perf_counter
import random
import pytest
from fastapi.testclient import TestClient

from contractor_matching.api import create_app
from contractor_matching.models import SearchRequest
from contractor_matching.presets import BASE

DATA = Path(__file__).resolve().parents[1] / 'data'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for name in ['ASTRA_API_KEY_1', 'ASTRA_API_KEY_2', 'ASTRA_API_KEY_3', 'ASTRA_API_KEY', 'FALLBACK_LLM_API_KEY', 'FALLBACK_LLM_API_KEY_1', 'FALLBACK_LLM_API_KEY_2', 'FALLBACK_LLM_API_KEY_3']:
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(DATA, tmp_path / 'cache.sqlite3')) as test_client:
        yield test_client


def search(client, **changes):
    response = client.post('/api/search', json={**BASE, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def test_ready_meta_static_and_source_integrity(client):
    assert client.get('/health/ready').json()['status'] == 'ready'
    meta = client.get('/api/meta').json()
    assert meta['catalog'] == {'total': 70, 'original': 66, 'team_added': 4, 'synthetic': 17}
    assert meta['runtime']['rank_mode'] == 'frozen_semantic'
    assert len(meta['categories']) == 17
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/data/original.csv').status_code == 404
    schema = client.get('/openapi.json').json()
    assert schema['paths']['/api/search']['post']['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/SearchResponse')


def test_twenty_repeats_keep_order_cards_and_explanations(client):
    first = search(client)
    assert first['outcome'] == 'SUCCESS'
    assert first['pool_count'] == 10 and first['eligible_count'] == 5
    assert first['stats']['busy'] == 5
    assert len(first['cards']) == 3
    assert first['diagnostics']['cache_hit'] is False
    for _ in range(20):
        again = search(client)
        assert again['cards'] == first['cards']
        assert again['diagnostics']['cache_hit'] is True
        assert again['elapsed_ms'] < 10_000


def test_two_dates_change_top3_due_to_busy_dates(client):
    first, second = search(client), search(client, date='2026-10-05')
    ids_first = {c['id'] for c in first['cards']}
    ids_second = {c['id'] for c in second['cards']}
    assert ids_first != ids_second
    removed = ids_first - ids_second
    assert removed
    assert all(second['diagnostics']['rejected_by_id'][item] == 'busy' for item in removed)
    assert second['eligible_count'] == 4
    assert '2026-10-05' in second['message']


def test_all_presets_with_real_data(client):
    meta = client.get('/api/meta').json()
    expected = {'dense': ('SUCCESS', 3), 'rare': ('SUCCESS', 3), 'florists': ('SUCCESS', 2),
                'absent': ('NO_CATEGORY_IN_CITY', 0), 'budget': ('ALL_FILTERED_OUT', 0),
                'busy': ('ALL_FILTERED_OUT', 0), 'venue': ('SUCCESS', 3)}
    for preset in meta['presets']:
        result = client.post('/api/search', json=preset['request']).json()
        assert (result['outcome'], len(result['cards'])) == expected[preset['id']]
        assert result['message']
        assert result['stats']['total'] == result['pool_count'] - result['eligible_count']
        if preset['id'] == 'rare':
            assert all(card['synthetic'] and card['origin'] == 'team_extension' for card in result['cards'])
        if preset['id'] == 'florists':
            assert all(card['max_hours'] is None for card in result['cards'])
            assert 'всего' in result['message']
        if preset['id'] in ('budget', 'busy'):
            assert result['stats'][preset['id']] == 2


@pytest.mark.parametrize('changes', [
    {'date': '2026-10-4'}, {'date': '2026-11-31'}, {'date': '2027-01-01'},
    {'city': 'Любой'}, {'event_format': 'вечеринка'}, {'category': 'DJ'},
    {'budget': 0}, {'budget': -1}, {'budget': True}, {'budget': '2000000'},
    {'duration': 1.5}, {'duration': 0}, {'language': 'неизвестный'}, {'guest_count': 200},
])
def test_invalid_requests_are_readable_422(client, changes):
    response = client.post('/api/search', json={**BASE, **changes})
    assert response.status_code == 422
    assert response.json()['message']
    assert 'traceback' not in response.text.lower()


def test_runtime_errors_do_not_expose_provider_details(client, monkeypatch):
    async def broken(_):
        raise RuntimeError('SECRET-TEST-TOKEN-NEVER-PRINT')
    monkeypatch.setattr(client.app.state.service, 'search', broken)
    response = client.post('/api/search', json=BASE)
    assert response.status_code == 503
    assert 'SECRET-TEST' not in response.text


def test_valid_requests_obey_all_hard_rules_and_fact_limit(client):
    profiles = client.app.state.service.profiles
    by_id = {p.id: p for p in profiles}
    rng = random.Random(79)
    started = perf_counter()
    for _ in range(100):
        p = rng.choice(profiles)
        query = {**BASE, 'city': p.city, 'category': rng.choice(p.categories),
                 'event_format': rng.choice(p.event_formats), 'date': f'2026-10-{rng.randrange(1, 32):02d}',
                 'budget': float(rng.choice([300000, 900000, 2000000, 6000000])),
                 'duration': rng.choice([None, 4, 8]), 'language': rng.choice([None, 'русский', 'казахский', 'английский'])}
        response = client.post('/api/search', json=query)
        assert response.status_code == 200, response.text
        result = response.json()
        assert len(result['cards']) <= 3
        assert result['stats']['total'] == result['pool_count'] - result['eligible_count']
        assert len({c['explanation'] for c in result['cards']}) == len(result['cards'])
        for card in result['cards']:
            original = by_id[card['id']]
            assert original.city == query['city']
            assert query['date'] not in original.busy_dates
            assert query['category'] in original.categories
            assert query['event_format'] in original.event_formats
            assert original.price_from_kzt <= query['budget']
            assert query['language'] is None or query['language'] in original.languages
            assert query['duration'] is None or original.max_hours is None or query['duration'] <= original.max_hours
            assert 1 <= len(card['explanation'].split()) <= 45
    assert perf_counter() - started < 10  # 100 local requests; no network required.
