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

# Chat lines are kept this long, pruned now and then while appending.
CHAT_RETENTION_SECONDS = 365 * 24 * 3600
CHAT_PRUNE_EVERY = 500
CHAT_TEXT_LIMIT = 1000
# And at most this many lines per server and account, newest kept.
CHAT_ROW_LIMIT = 300_000


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
            # Player chat she saw and her replies, one row each, so a talk
            # can be reviewed weeks later (the decision log keeps hours).
            db.execute(
                """CREATE TABLE IF NOT EXISTS chat_log (
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       at REAL NOT NULL,
                       channel INTEGER NOT NULL,
                       direction TEXT NOT NULL,
                       sender TEXT NOT NULL,
                       text TEXT NOT NULL
                   )""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS chat_log_by_character
                   ON chat_log (server, account, character, at)""")
        self._chat_appends = 0

    @contextmanager
    def _connect(self, timeout: float = 10.0) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=timeout)
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()
        finally:
            db.close()

    def load(self, character: str, kind: str, default: Any, *,
             max_bytes: int | None = None) -> Any:
        """The stored value, or ``default``. With ``max_bytes``, a larger
        stored value is never fetched or decoded (SQLite checks its length)."""
        with self._connect() as db:
            row = db.execute(
                """SELECT CASE WHEN ? IS NULL OR length(CAST(value_json AS BLOB)) <= ?
                               THEN value_json END,
                          length(CAST(value_json AS BLOB))
                   FROM memory
                   WHERE server = ? AND account = ? AND character = ?
                     AND kind = ?""",
                (max_bytes, max_bytes, self.server, self.account, character.casefold(), kind),
            ).fetchone()
        if row is None:
            return default
        if row[0] is None:
            log.warning("ignoring %s memory for %s on %s: %s bytes is over %s",
                        kind, character, self.server, row[1], max_bytes)
            return default
        try:
            return json.loads(row[0])
        # RecursionError: a small value nested too deep for the decoder.
        except (TypeError, ValueError, RecursionError):
            log.warning("invalid %s memory for %s on %s",
                        kind, character, self.server)
            return default

    def save(self, character: str, kind: str, value: Any, *, timeout: float = 10.0) -> None:
        """Store ``value``; ``timeout`` bounds the wait for a locked database."""
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._connect(timeout) as db:
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

    def append_chat(self, character: str, channel: int, direction: str,
                    sender: str, text: str, *, at: float | None = None) -> None:
        """Keep one chat line (``direction`` "in" or "out") for a year."""
        now = time.time() if at is None else at
        with self._connect() as db:
            db.execute(
                """INSERT INTO chat_log
                       (server, account, character, at, channel, direction,
                        sender, text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.server, self.account, character.casefold(), now,
                 int(channel), direction, sender[:64],
                 text[:CHAT_TEXT_LIMIT]))
            self._chat_appends += 1
            if self._chat_appends % CHAT_PRUNE_EVERY == 1:
                db.execute(
                    """DELETE FROM chat_log
                       WHERE server = ? AND account = ? AND at < ?""",
                    (self.server, self.account, now - CHAT_RETENTION_SECONDS))
                db.execute(
                    """DELETE FROM chat_log
                       WHERE server = ? AND account = ? AND at < (
                           SELECT at FROM chat_log
                           WHERE server = ? AND account = ?
                           ORDER BY at DESC LIMIT 1 OFFSET ?)""",
                    (self.server, self.account, self.server, self.account,
                     CHAT_ROW_LIMIT - 1))

    def chat_history(self, character: str, *, limit: int = 200,
                     sender: str = "") -> list[dict[str, Any]]:
        """Her most recent chat lines, oldest first, optionally one sender's."""
        query = """SELECT at, channel, direction, sender, text FROM chat_log
                   WHERE server = ? AND account = ? AND character = ?"""
        params: list[Any] = [self.server, self.account, character.casefold()]
        if sender:
            query += " AND lower(sender) = lower(?)"
            params.append(sender)
        query += " ORDER BY at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [{"at": at, "channel": channel, "direction": direction,
                 "sender": who, "text": text}
                for at, channel, direction, who, text in reversed(rows)]

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
