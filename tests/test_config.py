"""Configuration must never evaluate or reveal an API key."""
from pathlib import Path

import pytest

from contractor_matching.config import EnvFileError, load_project_env, parse_env, project_env_path


def test_comments_quotes_and_shell_syntax_are_literal():
    values = parse_env('''# Optional API configuration
ASTRA_API_KEY_1 = 'secret#with spaces $HOME $(echo nope) `nope`'
ASTRA_MODEL="model\\nversion" # a comment
ASTRA_API_PROTOCOL=openai-chat-completions # inline comment
ASTRA_API_KEY_2=secret#hash
ASTRA_API_KEY_3= # blank
''')
    assert values["ASTRA_API_KEY_1"] == "secret#with spaces $HOME $(echo nope) `nope`"
    assert values["ASTRA_MODEL"] == "model\\nversion"
    assert values["ASTRA_API_PROTOCOL"] == "openai-chat-completions"
    assert values["ASTRA_API_KEY_2"] == "secret#hash"
    assert values["ASTRA_API_KEY_3"] == ""


@pytest.mark.parametrize("line", [
    "ASTRA_API_KEY_1='very-secret", "export ASTRA_API_KEY_1=very-secret",
    "ASTRA_API_KEY_1='very-secret' trailing", "UNKNOWN=very-secret",
    "ASTRA_API_KEY_1: very-secret", "ASTRA_API_KEY_1=very-secret\x00",
    "ASTRA_API_KEY_1=very-secret\nASTRA_API_KEY_1=another-secret",
])
def test_invalid_lines_never_disclose_input(line):
    with pytest.raises(EnvFileError) as caught:
        parse_env("# first line\n" + line)
    assert "very-secret" not in str(caught.value)
    assert "another-secret" not in str(caught.value)
    assert "ASTRA" not in str(caught.value)
    assert "line" in str(caught.value)


def test_existing_environment_even_empty_takes_precedence(tmp_path):
    (tmp_path / ".env").write_text("ASTRA_MODEL=from-file\nASTRA_API_KEY_1=file-key\nASTRA_API_KEY_2=new-key", encoding="utf-8")
    env = {"ASTRA_MODEL": "from-environment", "ASTRA_API_KEY_1": ""}
    assert load_project_env(tmp_path, env) == ("ASTRA_API_KEY_2",)
    assert env == {"ASTRA_MODEL": "from-environment", "ASTRA_API_KEY_1": "", "ASTRA_API_KEY_2": "new-key"}


def test_validation_is_atomic_no_partial_environment_change(tmp_path):
    (tmp_path / ".env").write_text("ASTRA_MODEL=valid\ninvalid-secret", encoding="utf-8")
    env = {}
    with pytest.raises(EnvFileError, match="line 2"):
        load_project_env(tmp_path, env)
    assert env == {}


def test_missing_file_is_normal(tmp_path):
    env = {"UNRELATED": "keep"}
    assert load_project_env(tmp_path, env) == ()
    assert env == {"UNRELATED": "keep"}


def test_explicit_project_path_bom_and_no_cwd_lookup(tmp_path, monkeypatch):
    project = tmp_path / "folder with spaces"
    project.mkdir()
    (project / ".env").write_text("\ufeffASTRA_MODEL=correct", encoding="utf-8")
    (tmp_path / ".env").write_text("ASTRA_MODEL=wrong", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    env = {}
    load_project_env(project, env)
    assert env["ASTRA_MODEL"] == "correct"


def test_unreadable_encoding_is_sanitized(tmp_path):
    (tmp_path / ".env").write_bytes(b"ASTRA_API_KEY_1=very-secret\xff")
    with pytest.raises(EnvFileError) as caught:
        load_project_env(tmp_path, {})
    assert str(caught.value) == "Cannot read project .env configuration"


def test_relative_and_absolute_data_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", "alternate/data")
    assert project_env_path("DATA_DIR", "data", tmp_path) == tmp_path / "alternate/data"
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.db"))
    assert project_env_path("CACHE_PATH", "default.db", tmp_path) == tmp_path / "cache.db"
    monkeypatch.setenv("CACHE_PATH", "")
    assert project_env_path("CACHE_PATH", Path(".cache/default.db"), tmp_path) == tmp_path / ".cache/default.db"


def test_shipped_env_example_parses():
    root = Path(__file__).resolve().parents[1]
    parse_env((root / ".env.example").read_text(encoding="utf-8"))


def test_inquiry_transport_configuration_is_literal_and_secret_safe(tmp_path):
    (tmp_path / ".env").write_text(
        "INQUIRY_DB_PATH=.cache/inquiries.sqlite3\nINQUIRY_TRANSPORT=telegram\n"
        "TELEGRAM_BOT_TOKEN='local-token#literal'\nTELEGRAM_CHAT_ID=-1001234567890\n",
        encoding="utf-8",
    )
    env = {}
    loaded = load_project_env(tmp_path, env)
    assert set(loaded) == {"INQUIRY_DB_PATH", "INQUIRY_TRANSPORT", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert env["TELEGRAM_BOT_TOKEN"] == "local-token#literal"
    assert env["TELEGRAM_CHAT_ID"] == "-1001234567890"
    assert "local-token" not in repr(loaded)
