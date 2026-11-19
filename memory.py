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

# Observations (docs/LEARNING_AND_FEEDBACK.md 4.2): at most this many rows a
# character, the least recently seen unreferenced ones evicted first,
# checked every OBSERVATION_PRUNE_EVERY new rows.
OBSERVATION_ROW_LIMIT = 50_000
OBSERVATION_PRUNE_EVERY = 100
OBSERVATION_TEXT_LIMIT = 4000
# The longest an observation waits for a locked database (seconds): a
# reading is best effort and never stalls a tick.
OBSERVATION_SAVE_TIMEOUT = 0.25
# Tables and columns that keep an observation from eviction, where they
# exist (the facts and locks of later phases).
OBSERVATION_REFERENCES = (("facts", "observation_id"),
                          ("locks", "hint_observation_id"))
# Facts (4.2, phase F2): at most this many a character, the least recently
# updated candidates evicted first, then confirmed ones.
FACT_ROW_LIMIT = 20_000
FACT_PRUNE_EVERY = 100
FACT_SAVE_TIMEOUT = OBSERVATION_SAVE_TIMEOUT
FACT_COLUMNS = ("id", "kind", "subject", "value", "confidence", "status",
                "observation_id", "parser", "detail", "inputs", "created_at",
                "updated_at", "expires_at")


def _add_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    """Add a column unless it exists (race-free: the duplicate is ignored)."""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
    except sqlite3.OperationalError as error:
        if "duplicate column name" not in str(error):
            raise


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
            # Pruning works per account, newest first.
            db.execute(
                """CREATE INDEX IF NOT EXISTS chat_log_by_account
                   ON chat_log (server, account, at)""")
            # What she read or was told, attributed where possible: only
            # her own observations, never other players' text (observations.py).
            db.execute(
                """CREATE TABLE IF NOT EXISTS observations (
                       id INTEGER PRIMARY KEY,
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       first_at REAL NOT NULL,
                       last_at REAL NOT NULL,
                       seen INTEGER NOT NULL DEFAULT 1,
                       map TEXT, x INTEGER, y INTEGER,
                       source_kind TEXT NOT NULL,
                       source_key TEXT NOT NULL,
                       source_name TEXT,
                       attribution TEXT NOT NULL,
                       authored INTEGER NOT NULL,
                       text_hash TEXT NOT NULL,
                       text TEXT NOT NULL,
                       color TEXT,
                       msg_type INTEGER
                   )""")
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS observations_dedup
                   ON observations (server, account, character, source_key, text_hash)""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS observations_by_age
                   ON observations (server, account, character, last_at)""")
            # Facts extracted from her observations (facts.py): candidates
            # until an outcome confirms them. One row per (kind, subject,
            # value); a repeat refreshes it.
            db.execute(
                """CREATE TABLE IF NOT EXISTS facts (
                       id INTEGER PRIMARY KEY,
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       kind TEXT NOT NULL,
                       subject TEXT NOT NULL,
                       value TEXT NOT NULL,
                       confidence REAL NOT NULL,
                       status TEXT NOT NULL,
                       observation_id INTEGER,
                       parser TEXT NOT NULL,
                       detail TEXT NOT NULL DEFAULT '',
                       inputs TEXT NOT NULL DEFAULT '',
                       created_at REAL NOT NULL,
                       updated_at REAL NOT NULL,
                       expires_at REAL
                   )""")
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS facts_dedup
                   ON facts (server, account, character, kind, subject, value)""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS facts_by_age
                   ON facts (server, account, character, updated_at)""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS facts_by_observation
                   ON facts (observation_id)""")
            # Columns added after a table first shipped. Another process may
            # add one at the same moment: an existing column is no error.
            # facts_version is the parser set that last read an observation
            # (facts.VERSION): a repeat is read again only after it changes.
            _add_column(db, "facts", "inputs TEXT NOT NULL DEFAULT ''")
            _add_column(db, "observations", "facts_version TEXT")
        self._chat_appends = 0
        self._observation_inserts = 0
        self._fact_inserts = 0

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

    def record_observation(self, character: str, row: dict[str, Any], *,
                           timeout: float = OBSERVATION_SAVE_TIMEOUT) -> int:
        """Store one observation (``Observation.row()``), or raise its seen
        count when the same text came from the same source before; returns
        the row id. ``timeout`` bounds the wait for a locked database."""
        return self.record_observation_facts(character, row, timeout=timeout)[0]

    def record_observation_facts(self, character: str, row: dict[str, Any],
                                 facts: list[dict[str, Any]] = (), version: str = "", *,
                                 timeout: float = OBSERVATION_SAVE_TIMEOUT
                                 ) -> tuple[int, list[tuple[int, bool]]]:
        """``record_observation`` and, in the same transaction, the facts
        (``Fact.row()``) parser set ``version`` read from it, citing it.
        A row that parser set already read stores no facts again. Returns
        the row id and each stored fact's id and whether it is new."""
        folded = character.casefold()
        at = float(row["at"])
        with self._connect(timeout) as db:
            db.execute(
                """INSERT INTO observations
                       (server, account, character, first_at, last_at, seen,
                        map, x, y, source_kind, source_key, source_name,
                        attribution, authored, text_hash, text, color, msg_type)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(server, account, character, source_key, text_hash)
                   DO UPDATE SET seen = seen + 1,
                                 last_at = max(last_at, excluded.last_at)""",
                (self.server, self.account, folded, at, at,
                 row.get("map") or "", row.get("x"), row.get("y"),
                 str(row["source_kind"]), str(row["source_key"]),
                 str(row.get("source_name") or "")[:200], str(row["attribution"]),
                 int(bool(row.get("authored"))), str(row["text_hash"]),
                 str(row["text"])[:OBSERVATION_TEXT_LIMIT],
                 row.get("color") or "", row.get("msg_type")))
            row_id, seen, read_by = db.execute(
                """SELECT id, seen, facts_version FROM observations
                   WHERE server = ? AND account = ? AND character = ?
                     AND source_key = ? AND text_hash = ?""",
                (self.server, self.account, folded, str(row["source_key"]),
                 str(row["text_hash"]))).fetchone()
            if seen == 1:
                self._observation_inserts += 1
                if self._observation_inserts % OBSERVATION_PRUNE_EVERY == 0:
                    self._prune_observations(db, folded)
            stored: list[tuple[int, bool]] = []
            if version and version != read_by:
                stored = [self._upsert_fact(db, folded, {**fact, "observation_id": row_id})
                          for fact in facts]
                db.execute("UPDATE observations SET facts_version = ? WHERE id = ?",
                           (version, row_id))
        return int(row_id), stored

    def _prune_observations(self, db: sqlite3.Connection, character: str) -> None:
        """Evict the least recently seen unreferenced rows over the cap."""
        scope = (self.server, self.account, character)
        (count,) = db.execute(
            """SELECT count(*) FROM observations
               WHERE server = ? AND account = ? AND character = ?""", scope).fetchone()
        excess = count - OBSERVATION_ROW_LIMIT
        if excess <= 0:
            return
        # Only references that exist: a table of a later phase, or one
        # without the column, never fails the insert that pruned.
        referenced = " ".join(
            f"AND id NOT IN (SELECT {column} FROM {table} WHERE {column} IS NOT NULL)"
            for table, column in OBSERVATION_REFERENCES
            if column in {row[1] for row in db.execute(f"PRAGMA table_info({table})")})
        db.execute(
            f"""DELETE FROM observations WHERE id IN (
                    SELECT id FROM observations
                    WHERE server = ? AND account = ? AND character = ? {referenced}
                    ORDER BY last_at, id LIMIT ?)""",
            (*scope, excess))

    def observations(self, character: str, *, limit: int = 100, source_kind: str = "",
                     timeout: float = 10.0) -> list[dict[str, Any]]:
        """Her most recently seen observations, newest first."""
        query = """SELECT id, first_at, last_at, seen, map, x, y, source_kind,
                          source_key, source_name, attribution, authored, text
                   FROM observations
                   WHERE server = ? AND account = ? AND character = ?"""
        params: list[Any] = [self.server, self.account, character.casefold()]
        if source_kind:
            query += " AND source_kind = ?"
            params.append(source_kind)
        query += " ORDER BY last_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        columns = ("id", "first_at", "last_at", "seen", "map", "x", "y", "source_kind",
                   "source_key", "source_name", "attribution", "authored", "text")
        with self._connect(timeout) as db:
            rows = db.execute(query, params).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def observation_counts(self, character: str, *,
                           timeout: float = 10.0) -> dict[str, int]:
        """Stored observations by source kind."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT source_kind, count(*) FROM observations
                   WHERE server = ? AND account = ? AND character = ?
                   GROUP BY source_kind""",
                (self.server, self.account, character.casefold())).fetchall()
        return {kind: int(count) for kind, count in rows}

    def observation_id(self, character: str, source_key: str, digest: str, *,
                       timeout: float = FACT_SAVE_TIMEOUT) -> int | None:
        """The id of the observation of this text from this source, if kept."""
        with self._connect(timeout) as db:
            row = db.execute(
                """SELECT id FROM observations
                   WHERE server = ? AND account = ? AND character = ?
                     AND source_key = ? AND text_hash = ?""",
                (self.server, self.account, character.casefold(), source_key,
                 digest)).fetchone()
        return None if row is None else int(row[0])

    def record_fact(self, character: str, row: dict[str, Any], *,
                    timeout: float = FACT_SAVE_TIMEOUT) -> tuple[int, bool]:
        """Store one fact (``Fact.row()``), or refresh the same (kind, subject,
        value): the higher confidence wins, a confirmation confirms (also a
        refuted fact), the first observation stays its source, and a later
        expiry and non-empty inputs replace the stored ones. Returns the row
        id and whether the fact is new."""
        folded = character.casefold()
        with self._connect(timeout) as db:
            return self._upsert_fact(db, folded, row)

    def _upsert_fact(self, db: sqlite3.Connection, folded: str,
                     row: dict[str, Any]) -> tuple[int, bool]:
        at = float(row["at"])
        key = (self.server, self.account, folded, str(row["kind"]),
               str(row["subject"]), str(row["value"]))
        existing = db.execute(
            """SELECT id FROM facts WHERE server = ? AND account = ? AND character = ?
                 AND kind = ? AND subject = ? AND value = ?""", key).fetchone()
        db.execute(
            """INSERT INTO facts
                   (server, account, character, kind, subject, value, confidence,
                    status, observation_id, parser, detail, inputs, created_at,
                    updated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(server, account, character, kind, subject, value)
               DO UPDATE SET
                   confidence = max(confidence, excluded.confidence),
                   status = CASE WHEN excluded.status = 'confirmed'
                                 THEN 'confirmed' ELSE status END,
                   observation_id = coalesce(observation_id, excluded.observation_id),
                   parser = excluded.parser,
                   detail = CASE WHEN detail = '' THEN excluded.detail ELSE detail END,
                   inputs = CASE WHEN excluded.inputs = '' THEN inputs
                                 ELSE excluded.inputs END,
                   updated_at = max(updated_at, excluded.updated_at),
                   expires_at = CASE WHEN excluded.expires_at IS NULL THEN expires_at
                                     ELSE max(coalesce(expires_at, 0), excluded.expires_at)
                                END""",
            (*key, float(row["confidence"]), str(row["status"]),
             row.get("observation_id"), str(row["parser"]),
             str(row.get("detail") or ""), str(row.get("inputs") or ""), at, at,
             row.get("expires_at")))
        (row_id,) = db.execute(
            """SELECT id FROM facts WHERE server = ? AND account = ? AND character = ?
                 AND kind = ? AND subject = ? AND value = ?""", key).fetchone()
        if existing is None:
            self._fact_inserts += 1
            if self._fact_inserts % FACT_PRUNE_EVERY == 0:
                self._prune_facts(db, folded)
        return int(row_id), existing is None

    def refute_fact(self, character: str, kind: str, subject: str, value: str, *,
                    at: float, timeout: float = FACT_SAVE_TIMEOUT) -> bool:
        """Mark a fact refuted (an outcome contradicted it); whether one was."""
        with self._connect(timeout) as db:
            cursor = db.execute(
                """UPDATE facts SET status = 'refuted', updated_at = ?
                   WHERE server = ? AND account = ? AND character = ?
                     AND kind = ? AND subject = ? AND value = ?""",
                (float(at), self.server, self.account, character.casefold(),
                 kind, subject, value))
        return cursor.rowcount > 0

    def _prune_facts(self, db: sqlite3.Connection, character: str) -> None:
        """Evict the least recently updated facts over the cap, candidates first."""
        scope = (self.server, self.account, character)
        (count,) = db.execute(
            """SELECT count(*) FROM facts
               WHERE server = ? AND account = ? AND character = ?""", scope).fetchone()
        excess = count - FACT_ROW_LIMIT
        if excess > 0:
            db.execute(
                """DELETE FROM facts WHERE id IN (
                       SELECT id FROM facts
                       WHERE server = ? AND account = ? AND character = ?
                       ORDER BY status = 'confirmed', updated_at, id LIMIT ?)""",
                (*scope, excess))

    def facts(self, character: str, *, limit: int = 100, kind: str = "",
              status: str = "", timeout: float = 10.0) -> list[dict[str, Any]]:
        """Her most recently updated facts, newest first, each with the map
        and source name of the observation it came from."""
        query = f"""SELECT {", ".join("f." + column for column in FACT_COLUMNS)},
                           o.map, o.source_name
                    FROM facts f LEFT JOIN observations o ON o.id = f.observation_id
                    WHERE f.server = ? AND f.account = ? AND f.character = ?"""
        params: list[Any] = [self.server, self.account, character.casefold()]
        if kind:
            query += " AND f.kind = ?"
            params.append(kind)
        if status:
            query += " AND f.status = ?"
            params.append(status)
        query += " ORDER BY f.updated_at DESC, f.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        columns = (*FACT_COLUMNS, "map", "source_name")
        with self._connect(timeout) as db:
            rows = db.execute(query, params).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def fact_counts(self, character: str, *, timeout: float = 10.0) -> dict[str, int]:
        """Stored facts by kind."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT kind, count(*) FROM facts
                   WHERE server = ? AND account = ? AND character = ?
                   GROUP BY kind""",
                (self.server, self.account, character.casefold())).fetchall()
        return {kind: int(count) for kind, count in rows}

    def tile_facts(self, character: str, *, now: float, inputs: str,
                   timeout: float = 10.0) -> list[tuple[str, str, float]]:
        """Her confirmed, unexpired tile-property facts recorded under these
        ``inputs`` (the content revision), as (subject, value, updated_at)."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT subject, value, updated_at FROM facts
                   WHERE server = ? AND account = ? AND character = ?
                     AND kind = 'tile-property' AND status = 'confirmed'
                     AND expires_at > ? AND inputs = ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (self.server, self.account, character.casefold(), float(now),
                 inputs, FACT_ROW_LIMIT)).fetchall()
        return [(str(subject), str(value), float(at)) for subject, value, at in rows]

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
