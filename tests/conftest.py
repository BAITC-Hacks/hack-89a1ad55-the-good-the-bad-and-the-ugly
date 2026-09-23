"""The test suite never inherits real provider/delivery credentials."""
import pytest


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch, tmp_path):
    from contractor_matching import api
    monkeypatch.setattr(api, 'load_project_env', lambda root: ())
    monkeypatch.delenv('APP_ALLOWED_HOSTS', raising=False)
    for prefix in ('ASTRA', 'FALLBACK_LLM'):
        for suffix in ('API_PROTOCOL', 'API_KEY', 'API_KEY_1', 'API_KEY_2', 'API_KEY_3'):
            monkeypatch.setenv(f'{prefix}_{suffix}', '')
    for key in ('INQUIRY_TRANSPORT', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.setenv(key, '')
    monkeypatch.setenv('INQUIRY_DB_PATH', str(tmp_path / 'isolated-inquiries.sqlite3'))
