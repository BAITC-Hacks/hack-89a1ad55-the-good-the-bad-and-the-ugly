"""Persistent accepted-result cache, including deterministic fallback text."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class ResponseCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS explanations (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        # Cache locks must never consume the complete request latency budget.
        return sqlite3.connect(self.path, timeout=0.1)

    def get(self, key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM explanations WHERE cache_key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_if_absent(self, key: str, payload: dict) -> dict:
        """All racing requests receive the first accepted response, not their own draft."""
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO explanations(cache_key,payload) VALUES (?, ?)", (key, serialized))
            accepted = db.execute("SELECT payload FROM explanations WHERE cache_key = ?", (key,)).fetchone()
        return json.loads(accepted[0])
