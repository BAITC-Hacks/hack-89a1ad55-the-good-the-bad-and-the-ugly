"""Small, literal .env loader; no shell, interpolation, or secret-bearing errors."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import MutableMapping


PROVIDER_FIELDS = ("API_PROTOCOL", "CHAT_COMPLETIONS_URL", "MODEL", "API_KEY", "API_KEY_1", "API_KEY_2", "API_KEY_3")
SUPPORTED_KEYS = frozenset(
    f"{prefix}_{field}" for prefix in ("ASTRA", "FALLBACK_LLM") for field in PROVIDER_FIELDS
) | {"DATA_DIR", "CACHE_PATH", "NVIDIA_API_KEY", "APP_ALLOWED_HOSTS", "INQUIRY_DB_PATH", "INQUIRY_TRANSPORT",
     "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
     "SMTP_PASSWORD", "SMTP_FROM", "INQUIRY_EMAIL_TO"}
_ASSIGNMENT = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(.*)")


class EnvFileError(ValueError):
    """A deliberately sanitized configuration error."""


def _invalid(line_number: int) -> EnvFileError:
    return EnvFileError(f"Invalid project .env configuration at line {line_number}")


def parse_env(text: str) -> dict[str, str]:
    """Parse supported keys atomically. Quoted text and $/backticks are literal.

    Inline comments start with whitespace + # in unquoted values. Inside
    single/double quotes, # is literal; escapes and multiline values are not
    interpreted. Unknown keys, duplicate keys and export statements are errors.
    Error messages never contain the offending line, key, or value.
    """
    parsed: dict[str, str] = {}
    for number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if any(ord(char) < 32 and char != "\t" for char in raw):
            raise _invalid(number)
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            raise _invalid(number)
        key, value = match.groups()
        if key not in SUPPORTED_KEYS or key in parsed:
            raise _invalid(number)
        if value.startswith(("'", '"')):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing < 0:
                raise _invalid(number)
            trailing = value[closing + 1:].strip()
            if trailing and not trailing.startswith("#"):
                raise _invalid(number)
            value = value[1:closing]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            if value.startswith("#"):
                value = ""
        parsed[key] = value
    return parsed


def load_project_env(project_root: Path, environ: MutableMapping[str, str] | None = None) -> tuple[str, ...]:
    """Fill missing environment keys from project_root/.env, never override.

    Container-injected variables and even explicitly empty environment values
    take precedence. A missing .env is normal. Nothing reads the current working
    directory or walks parent directories. Return only names that were loaded.
    """
    target = Path(project_root) / ".env"
    try:
        if not target.exists():
            return ()
        if target.stat().st_size > 65_536:
            raise EnvFileError("Project .env exceeds the 64 KiB configuration limit")
        contents = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise EnvFileError("Cannot read project .env configuration") from None
    values = parse_env(contents)
    destination = os.environ if environ is None else environ
    loaded = []
    for key, value in values.items():
        if key not in destination:
            destination[key] = value
            loaded.append(key)
    return tuple(loaded)


def project_env_path(name: str, default: str | Path, project_root: Path) -> Path:
    """Resolve optional storage paths consistently from the project root."""
    value = os.environ.get(name, "").strip()
    path = Path(value) if value else Path(default)
    return path if path.is_absolute() else Path(project_root) / path
