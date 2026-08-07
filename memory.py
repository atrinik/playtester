"""Transactional file-backed memory scoped to one server and character."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


class BotMemory:
    """Small SQLite key/value store for durable autonomous decisions."""

    def __init__(self, path: Path, *, server: str, account: str):
        self.path = path
        self.server = server.casefold().strip()
        self.account = account.casefold().strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS memory (
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       kind TEXT NOT NULL,
                       value_json TEXT NOT NULL,
                       updated_at REAL NOT NULL,
                       PRIMARY KEY (server, account, character, kind)
                   )""")
            db.execute(
                """CREATE TABLE IF NOT EXISTS metadata (
                       key TEXT PRIMARY KEY,
                       value TEXT NOT NULL
                   )""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10.0)
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    def load(self, character: str, kind: str, default: Any) -> Any:
        with self._connect() as db:
            row = db.execute(
                """SELECT value_json FROM memory
                   WHERE server = ? AND account = ? AND character = ?
                     AND kind = ?""",
                (self.server, self.account, character.casefold(), kind),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            log.warning("invalid %s memory for %s on %s",
                        kind, character, self.server)
            return default

    def save(self, character: str, kind: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                """INSERT INTO memory
                       (server, account, character, kind, value_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(server, account, character, kind) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (self.server, self.account, character.casefold(), kind,
                 encoded, time.time()),
            )

    def import_legacy(self, state_path: Path, activity_path: Path) -> None:
        """Import the old unscoped JSON files once without modifying them."""
        # The legacy files had no server/account namespace.  Import a given
        # source into only the first namespace that opens it; copying it into
        # every subsequently used server would manufacture false memories.
        marker = f"legacy-import:{state_path.resolve()}"
        with self._connect() as db:
            if db.execute(
                    "SELECT 1 FROM metadata WHERE key = ?", (marker,)
            ).fetchone() is not None:
                return
        states: dict[str, Any] = {}
        decisions: dict[str, Any] = {}
        try:
            value = json.loads(state_path.read_text())
            if isinstance(value, dict):
                states = value
        except (OSError, TypeError, ValueError):
            pass
        try:
            value = json.loads(activity_path.read_text())
            if isinstance(value, dict):
                decisions = value
        except (OSError, TypeError, ValueError):
            pass
        with self._connect() as db:
            for character in sorted(set(states) | set(decisions)):
                folded = character.casefold()
                if isinstance(states.get(character), dict):
                    db.execute(
                        """INSERT OR IGNORE INTO memory
                               (server, account, character, kind, value_json,
                                updated_at)
                           VALUES (?, ?, ?, 'state', ?, ?)""",
                        (self.server, self.account, folded,
                         json.dumps(states[character], separators=(",", ":")),
                         time.time()),
                    )
                if isinstance(decisions.get(character), list):
                    db.execute(
                        """INSERT OR IGNORE INTO memory
                               (server, account, character, kind, value_json,
                                updated_at)
                           VALUES (?, ?, ?, 'decisions', ?, ?)""",
                        (self.server, self.account, folded,
                         json.dumps(decisions[character][-2000:],
                                    separators=(",", ":")),
                         time.time()),
                    )
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (marker, str(time.time())),
            )
