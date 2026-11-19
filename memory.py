"""Transactional file-backed memory scoped to one server and character."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

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
# Fights by terrain (4.2 and 4.7, phase F10, terrain.py): at most this many
# rows a character, the oldest evicted first, checked every
# FIGHT_PRUNE_EVERY new rows. A row is best effort: it waits at most
# FIGHT_SAVE_TIMEOUT for a locked database and is otherwise dropped.
FIGHT_ROW_LIMIT = 20_000
FIGHT_PRUNE_EVERY = 100
FIGHT_SAVE_TIMEOUT = OBSERVATION_SAVE_TIMEOUT
FIGHT_COLUMNS = ("id", "started", "ended", "map", "x", "y", "cell", "feature",
                 "reach", "peak_attackers", "ranged_attackers", "ranged_covered",
                 "hostiles_in_view", "players_in_view", "kills", "damage_taken",
                 "seconds", "outcome")
# Route events (docs/ROUTING.md 4.4, route_events.py): the longest a write
# waits for a locked database (seconds).
ROUTE_EVENT_SAVE_TIMEOUT = 0.25
FACT_COLUMNS = ("id", "kind", "subject", "value", "confidence", "status",
                "observation_id", "parser", "detail", "inputs", "created_at",
                "updated_at", "expires_at")
# The feedback queue (4.10, phase F5, feedback.py): drafts are capped, the
# lowest-occurrence, least recently seen drafts dropped first; other
# statuses are kept. Each draft remembers its newest occurrence keys.
FEEDBACK_DRAFT_LIMIT = 500
FEEDBACK_OCCURRENCE_KEYS = 200
FEEDBACK_SAVE_TIMEOUT = OBSERVATION_SAVE_TIMEOUT
FEEDBACK_COLUMNS = ("id", "fingerprint", "repo", "kind", "subject", "target", "title",
                    "body_md", "labels_json", "issue_type", "evidence_json", "status",
                    "occurrences", "days", "first_seen", "last_seen", "decided_at",
                    "decided_via", "issue_url", "error", "withheld", "updated_at",
                    "screened_at")
# Distinct UTC dates kept per item for its day count.
FEEDBACK_DATES = 1000


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
            # One row a fight (terrain.py): where she stood, how many hit
            # her at once and how it ended. Counts only: no attacker's or
            # other player's name.
            db.execute(
                """CREATE TABLE IF NOT EXISTS fight_positions (
                       id INTEGER PRIMARY KEY,
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       started REAL NOT NULL,
                       ended REAL NOT NULL,
                       map TEXT NOT NULL,
                       x INTEGER, y INTEGER,
                       cell TEXT NOT NULL,
                       feature TEXT NOT NULL,
                       reach INTEGER,
                       peak_attackers INTEGER NOT NULL,
                       ranged_attackers INTEGER NOT NULL,
                       ranged_covered INTEGER NOT NULL DEFAULT 0,
                       hostiles_in_view INTEGER NOT NULL DEFAULT 0,
                       players_in_view INTEGER NOT NULL DEFAULT 0,
                       kills INTEGER NOT NULL DEFAULT 0,
                       damage_taken INTEGER NOT NULL,
                       seconds REAL NOT NULL,
                       outcome TEXT NOT NULL
                   )""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS fight_positions_by_age
                   ON fight_positions (server, account, character, ended)""")
            # The owner's route and map blocks (docs/ROUTING.md 4.7): each a
            # transit wall until ``until`` (wall clock), set and removed only
            # from the loopback dashboard (route_blocks.py).
            db.execute(
                """CREATE TABLE IF NOT EXISTS route_blocks (
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       subject TEXT NOT NULL,
                       created_at REAL NOT NULL,
                       until REAL NOT NULL,
                       note TEXT NOT NULL DEFAULT '',
                       PRIMARY KEY (server, account, character, subject)
                   )""")
            # Her route events (docs/ROUTING.md 4.4, route_events.py): rare
            # danger and failure signals, one row each, capped per signal;
            # successful traversals aggregated per subject (``timed`` counts
            # the ones whose seconds are in the mean: a seeded count has
            # none); requirement outcomes per edge.
            db.execute(
                """CREATE TABLE IF NOT EXISTS route_events (
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       at REAL NOT NULL,
                       subject TEXT NOT NULL,
                       signal TEXT NOT NULL,
                       magnitude REAL NOT NULL,
                       kit TEXT NOT NULL DEFAULT '',
                       content TEXT NOT NULL DEFAULT ''
                   )""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS route_events_by_signal
                   ON route_events (server, account, character, signal, at)""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS route_events_by_age
                   ON route_events (server, account, character, at)""")
            db.execute(
                """CREATE TABLE IF NOT EXISTS route_traversals (
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       subject TEXT NOT NULL,
                       count INTEGER NOT NULL,
                       mean_seconds REAL NOT NULL,
                       last_at REAL NOT NULL,
                       timed INTEGER NOT NULL DEFAULT 0,
                       PRIMARY KEY (server, account, character, subject)
                   )""")
            db.execute(
                """CREATE TABLE IF NOT EXISTS route_requirements (
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       edge TEXT NOT NULL,
                       outcome TEXT NOT NULL,
                       at REAL NOT NULL,
                       until REAL NOT NULL,
                       inputs TEXT NOT NULL,
                       evidence TEXT NOT NULL DEFAULT '',
                       PRIMARY KEY (server, account, character, edge)
                   )""")

            # The draft feedback queue (feedback.py): one row a fingerprint.
            # Only drafts exist before phase F6 adds approval and filing.
            db.execute(
                """CREATE TABLE IF NOT EXISTS feedback_items (
                       id INTEGER PRIMARY KEY,
                       server TEXT NOT NULL,
                       account TEXT NOT NULL,
                       character TEXT NOT NULL,
                       fingerprint TEXT NOT NULL,
                       repo TEXT NOT NULL,
                       kind TEXT NOT NULL,
                       subject TEXT NOT NULL,
                       target TEXT NOT NULL DEFAULT 'new-issue',
                       title TEXT NOT NULL,
                       body_md TEXT NOT NULL,
                       labels_json TEXT NOT NULL DEFAULT '[]',
                       issue_type TEXT NOT NULL DEFAULT '',
                       evidence_json TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'draft',
                       occurrences INTEGER NOT NULL DEFAULT 1,
                       days INTEGER NOT NULL DEFAULT 1,
                       occurrence_keys_json TEXT NOT NULL DEFAULT '[]',
                       dates_json TEXT NOT NULL DEFAULT '[]',
                       withheld INTEGER NOT NULL DEFAULT 0,
                       updated_at REAL NOT NULL DEFAULT 0,
                       screened_at REAL NOT NULL DEFAULT 0,
                       first_seen REAL NOT NULL,
                       last_seen REAL NOT NULL,
                       remote_candidates_json TEXT,
                       decided_at REAL,
                       decided_via TEXT,
                       issue_url TEXT,
                       error TEXT,
                       UNIQUE (server, account, character, fingerprint)
                   )""")
            db.execute(
                """CREATE INDEX IF NOT EXISTS feedback_by_status
                   ON feedback_items (server, account, character, status, occurrences)""")
        self._chat_appends = 0
        self._observation_inserts = 0
        self._fact_inserts = 0
        self._fight_inserts = 0

    @classmethod
    def open_read_only(cls, path: Path, *, server: str, account: str) -> "BotMemory":
        """A store that only reads an existing database: opened with
        ``mode=ro``, creating nothing and migrating nothing (the feedback
        CLI). A write through it fails."""
        memory = cls.__new__(cls)
        memory.path = Path(path)
        memory.server = server.casefold().strip()
        memory.account = account.casefold().strip()
        memory.read_only = True
        memory._chat_appends = memory._observation_inserts = memory._fact_inserts = 0
        return memory

    def has_table(self, name: str, *, timeout: float = 10.0) -> bool:
        with self._connect(timeout) as db:
            return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                              (name,)).fetchone() is not None

    @contextmanager
    def _connect(self, timeout: float = 10.0) -> Iterator[sqlite3.Connection]:
        if getattr(self, "read_only", False):
            db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", timeout=timeout,
                                 uri=True)
        else:
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
            stored: list[tuple[int, bool]] = []
            if version and version != read_by:
                # In a savepoint: a fact that fails to store loses only the
                # facts (read again on the next sighting), never the reading.
                db.execute("SAVEPOINT observation_facts")
                try:
                    stored = [self._upsert_fact(db, folded,
                                                {**fact, "observation_id": row_id})
                              for fact in facts]
                    db.execute("UPDATE observations SET facts_version = ? WHERE id = ?",
                               (version, row_id))
                except Exception as error:
                    db.execute("ROLLBACK TO observation_facts")
                    log.warning("facts of observation %s not kept: %s", row_id, error)
                    stored = []
                db.execute("RELEASE observation_facts")
            # Pruned last, so the new row's facts already cite it.
            if seen == 1:
                self._observation_inserts += 1
                if self._observation_inserts % OBSERVATION_PRUNE_EVERY == 0:
                    self._prune_observations(db, folded)
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

    def record_fight(self, character: str, row: dict[str, Any], *,
                     timeout: float = FIGHT_SAVE_TIMEOUT) -> int:
        """Store one fight (``terrain.Fight.row()``); returns its id.
        ``timeout`` bounds the wait for a locked database."""
        folded = character.casefold()
        values = tuple(row[column] for column in FIGHT_COLUMNS[1:])
        with self._connect(timeout) as db:
            cursor = db.execute(
                f"""INSERT INTO fight_positions
                        (server, account, character, {", ".join(FIGHT_COLUMNS[1:])})
                    VALUES (?, ?, ?, {", ".join("?" * (len(FIGHT_COLUMNS) - 1))})""",
                (self.server, self.account, folded, *values))
            self._fight_inserts += 1
            if self._fight_inserts % FIGHT_PRUNE_EVERY == 0:
                db.execute(
                    """DELETE FROM fight_positions WHERE id IN (
                           SELECT id FROM fight_positions
                           WHERE server = ? AND account = ? AND character = ?
                           ORDER BY ended DESC, id DESC LIMIT -1 OFFSET ?)""",
                    (self.server, self.account, folded, FIGHT_ROW_LIMIT))
        return int(cursor.lastrowid)

    def fights(self, character: str, *, limit: int = 100, since: float | None = None,
               timeout: float = 10.0) -> list[dict[str, Any]]:
        """Her most recent fights, newest first; with ``since``, only those
        that ended then or later."""
        query = f"""SELECT {", ".join(FIGHT_COLUMNS)} FROM fight_positions
                    WHERE server = ? AND account = ? AND character = ?"""
        params: list[Any] = [self.server, self.account, character.casefold()]
        if since is not None:
            query += " AND ended >= ?"
            params.append(float(since))
        query += " ORDER BY ended DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), FIGHT_ROW_LIMIT)))
        with self._connect(timeout) as db:
            rows = db.execute(query, params).fetchall()
        return [dict(zip(FIGHT_COLUMNS, row)) for row in rows]

    def fight_buckets(self, character: str, *, now: float, days: int = 60,
                      timeout: float = 10.0) -> list[dict[str, Any]]:
        """Her fights of the last ``days`` days summed per feature, peak
        attackers (0, 1, 2, 3 for three or more), ranged monsters in view
        or not, and whole days of age: a few hundred rows at most, however
        many fights are kept."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT feature, min(3, max(0, peak_attackers)), ranged_attackers > 0,
                          CAST((? - ended) / 86400 AS INTEGER) AS age,
                          count(*), sum(max(0, seconds)), sum(max(0, damage_taken)),
                          sum(outcome = 'died')
                   FROM fight_positions
                   WHERE server = ? AND account = ? AND character = ? AND ended >= ?
                   GROUP BY 1, 2, 3, 4""",
                (float(now), self.server, self.account, character.casefold(),
                 float(now) - days * 86400.0)).fetchall()
        return [{"feature": feature, "attackers": int(attackers), "ranged": bool(ranged),
                 "age_days": max(0, int(age)), "fights": int(count),
                 "seconds": float(seconds or 0.0), "damage": float(damage or 0.0),
                 "deaths": int(deaths or 0)}
                for feature, attackers, ranged, age, count, seconds, damage, deaths in rows]

    def fight_count(self, character: str, *, timeout: float = 10.0) -> int:
        """Stored fights."""
        with self._connect(timeout) as db:
            (count,) = db.execute(
                """SELECT count(*) FROM fight_positions
                   WHERE server = ? AND account = ? AND character = ?""",
                (self.server, self.account, character.casefold())).fetchone()
        return int(count)
    def record_feedback(self, character: str, *, fingerprint: str, repo: str, kind: str,
                        subject: str, at: float, occurrence: str,
                        build: Callable[..., dict[str, Any]],
                        timeout: float = FEEDBACK_SAVE_TIMEOUT) -> tuple[int, bool]:
        """Start a draft for ``fingerprint`` or count another occurrence of it,
        in one transaction (feedback.note).

        An ``occurrence`` key already counted (among the newest
        ``FEEDBACK_OCCURRENCE_KEYS``) adds no occurrence; an empty key always
        adds one. ``days`` is the number of distinct UTC dates of counted
        occurrences, whatever order they arrive in. ``build(stored_evidence,
        occurrences, days, first_seen, last_seen)`` returns the title, body,
        evidence, labels and type; it is called for a new draft and for a
        draft, never for an item past draft, which only gains occurrences.
        ``build`` checks the text against the running bot's full private
        terms, so a stored render is also screened (``updated_at`` and
        ``screened_at`` both set to the time of the write). A withheld item
        stays withheld: only an explicit owner action (phase F6) may release
        it. Whatever ``build`` raises
        leaves the row as it was. A new draft in a full queue first evicts
        the weakest draft (``_make_room``). Returns the id and whether it is
        new.
        """
        folded = character.casefold()
        scope = (self.server, self.account, folded)
        day = time.strftime("%Y-%m-%d", time.gmtime(at))
        # When the text was written (and screened), on the wall clock: the
        # occurrence time ``at`` may be earlier.
        written = time.time()
        with self._connect(timeout) as db:
            row = db.execute(
                """SELECT id, status, occurrences, occurrence_keys_json, dates_json,
                          first_seen, last_seen, evidence_json
                   FROM feedback_items
                   WHERE server = ? AND account = ? AND character = ? AND fingerprint = ?""",
                (*scope, fingerprint)).fetchone()
            if row is None:
                fields = build(None, 1, 1, at, at)
                self._make_room(db, folded)
                cursor = db.execute(
                    """INSERT INTO feedback_items
                           (server, account, character, fingerprint, repo, kind, subject,
                            title, body_md, labels_json, issue_type, evidence_json,
                            occurrence_keys_json, dates_json, first_seen, last_seen,
                            updated_at, screened_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*scope, fingerprint, repo, kind, subject, fields["title"],
                     fields["body_md"], fields["labels_json"], fields["issue_type"],
                     fields["evidence_json"], json.dumps([occurrence] if occurrence else []),
                     json.dumps([day]), at, at, written, written))
                return int(cursor.lastrowid), True
            row_id, status, occurrences, keys_json, dates_json, first_seen, last_seen, stored = row

            def decoded(value: str) -> list[str]:
                try:
                    return [str(entry) for entry in json.loads(value)]
                except (TypeError, ValueError):
                    return []

            keys, dates = decoded(keys_json), set(decoded(dates_json))
            if not occurrence or occurrence not in keys:
                occurrences += 1
                dates.add(day)
                if occurrence:
                    keys = (keys + [occurrence])[-FEEDBACK_OCCURRENCE_KEYS:]
            ordered = sorted(dates)[-FEEDBACK_DATES:]
            first_seen, last_seen = min(first_seen, at), max(last_seen, at)
            counts = (occurrences, len(ordered), json.dumps(keys), json.dumps(ordered),
                      first_seen, last_seen)
            if status != "draft":
                db.execute(
                    """UPDATE feedback_items SET occurrences = ?, days = ?,
                           occurrence_keys_json = ?, dates_json = ?, first_seen = ?,
                           last_seen = ?
                       WHERE id = ?""", (*counts, row_id))
                return int(row_id), False
            try:
                evidence = json.loads(stored)
            except (TypeError, ValueError):
                evidence = None
            fields = build(evidence, occurrences, len(ordered), first_seen, last_seen)
            db.execute(
                """UPDATE feedback_items SET occurrences = ?, days = ?,
                       occurrence_keys_json = ?, dates_json = ?, first_seen = ?,
                       last_seen = ?, title = ?, body_md = ?, labels_json = ?,
                       issue_type = ?, evidence_json = ?, updated_at = ?,
                       screened_at = ?
                   WHERE id = ?""",
                (*counts, fields["title"], fields["body_md"], fields["labels_json"],
                 fields["issue_type"], fields["evidence_json"], written, written, row_id))
        return int(row_id), False

    def _make_room(self, db: sqlite3.Connection, character: str) -> None:
        """Before a new draft: while the queue holds ``FEEDBACK_DRAFT_LIMIT``
        drafts, evict the weakest (fewest occurrences, then least recently
        seen), so a new draft always gets in and is never itself the one
        dropped. Other statuses are never evicted."""
        scope = (self.server, self.account, character)
        (count,) = db.execute(
            """SELECT count(*) FROM feedback_items
               WHERE server = ? AND account = ? AND character = ? AND status = 'draft'""",
            scope).fetchone()
        excess = count - FEEDBACK_DRAFT_LIMIT + 1
        if excess > 0:
            db.execute(
                """DELETE FROM feedback_items WHERE id IN (
                       SELECT id FROM feedback_items
                       WHERE server = ? AND account = ? AND character = ?
                         AND status = 'draft'
                       ORDER BY occurrences, last_seen, id LIMIT ?)""",
                (*scope, excess))

    def withhold_feedback(self, character: str, ids: Iterable[int], *,
                          timeout: float = FEEDBACK_SAVE_TIMEOUT) -> None:
        """Mark items withheld: their stored title or body names a private
        term. They are shown and filed only once a clean render replaces it."""
        ids = [int(item) for item in ids]
        if not ids:
            return
        with self._connect(timeout) as db:
            db.executemany(
                """UPDATE feedback_items SET withheld = 1
                   WHERE server = ? AND account = ? AND character = ? AND id = ?""",
                [(self.server, self.account, character.casefold(), item) for item in ids])

    def mark_feedback_screened(self, character: str, ids: Iterable[int], *,
                               at: float | None = None,
                               timeout: float = FEEDBACK_SAVE_TIMEOUT) -> None:
        """Record that the running bot screened these items with its full
        private terms (only those whose text changed since the last screen)."""
        ids = [int(item) for item in ids]
        if not ids:
            return
        at = time.time() if at is None else at
        with self._connect(timeout) as db:
            db.executemany(
                """UPDATE feedback_items SET screened_at = ?
                   WHERE server = ? AND account = ? AND character = ? AND id = ?
                     AND screened_at < updated_at""",
                [(at, self.server, self.account, character.casefold(), item)
                 for item in ids])

    def withhold_feedback_fingerprint(self, character: str, fingerprint: str, *,
                                      timeout: float = FEEDBACK_SAVE_TIMEOUT) -> None:
        """Mark the item of ``fingerprint`` withheld, if there is one."""
        with self._connect(timeout) as db:
            db.execute(
                """UPDATE feedback_items SET withheld = 1
                   WHERE server = ? AND account = ? AND character = ? AND fingerprint = ?""",
                (self.server, self.account, character.casefold(), fingerprint))

    def feedback_items(self, character: str, *, kind: str = "", repo: str = "",
                       status: str = "", limit: int = 200,
                       timeout: float = 10.0) -> list[dict[str, Any]]:
        """Her feedback items, most recently seen first."""
        query = f"""SELECT {", ".join(FEEDBACK_COLUMNS)} FROM feedback_items
                    WHERE server = ? AND account = ? AND character = ?"""
        params: list[Any] = [self.server, self.account, character.casefold()]
        for column, value in (("kind", kind), ("repo", repo), ("status", status)):
            if value:
                query += f" AND {column} = ?"
                params.append(value)
        query += " ORDER BY last_seen DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._connect(timeout) as db:
            rows = db.execute(query, params).fetchall()
        return [dict(zip(FEEDBACK_COLUMNS, row)) for row in rows]

    def feedback_item(self, character: str, reference: str, *,
                      timeout: float = 10.0) -> dict[str, Any] | None:
        """One item by its id, or by its fingerprint (full, or ``pt-`` and
        at least 12 hex digits); None when none or more than one match."""
        reference = reference.strip().casefold()
        scope = (self.server, self.account, character.casefold())
        columns = ", ".join(FEEDBACK_COLUMNS)
        with self._connect(timeout) as db:
            if reference.isdigit():
                rows = db.execute(
                    f"""SELECT {columns} FROM feedback_items
                        WHERE server = ? AND account = ? AND character = ? AND id = ?""",
                    (*scope, int(reference))).fetchall()
            else:
                prefix = reference.removeprefix("pt-")
                if len(prefix) < 12 or any(ch not in "0123456789abcdef" for ch in prefix):
                    return None
                rows = db.execute(
                    f"""SELECT {columns} FROM feedback_items
                        WHERE server = ? AND account = ? AND character = ?
                          AND substr(fingerprint, 1, ?) = ? LIMIT 2""",
                    (*scope, len(prefix), prefix)).fetchall()
        return dict(zip(FEEDBACK_COLUMNS, rows[0])) if len(rows) == 1 else None

    def feedback_counts(self, character: str, *,
                        timeout: float = 10.0) -> dict[str, dict[str, int]]:
        """Her feedback items by status, then kind."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT status, kind, count(*) FROM feedback_items
                   WHERE server = ? AND account = ? AND character = ?
                   GROUP BY status, kind""",
                (self.server, self.account, character.casefold())).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for status, kind, count in rows:
            counts.setdefault(status, {})[kind] = int(count)
        return counts

    def chat_senders(self, *, limit: int = 5000, timeout: float = 10.0) -> list[str]:
        """The distinct senders in this account's chat log (any of its
        characters), most recent first: names drafts must never contain."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT sender FROM chat_log
                   WHERE server = ? AND account = ?
                   GROUP BY sender ORDER BY max(at) DESC LIMIT ?""",
                (self.server, self.account, max(1, int(limit)))).fetchall()
        return [str(sender) for (sender,) in rows if sender]

    def set_route_block(self, character: str, subject: str, *, created_at: float,
                        until: float, note: str = "", timeout: float = 10.0) -> None:
        """Store one owner block (a new one, or a new expiry for the same
        subject); ``timeout`` bounds the wait for a locked database."""
        with self._connect(timeout) as db:
            db.execute(
                """INSERT INTO route_blocks
                       (server, account, character, subject, created_at, until, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(server, account, character, subject) DO UPDATE SET
                       created_at = excluded.created_at,
                       until = excluded.until,
                       note = excluded.note""",
                (self.server, self.account, character.casefold(), subject,
                 float(created_at), float(until), note))

    def remove_route_block(self, character: str, subject: str, *,
                           timeout: float = 10.0) -> bool:
        """Delete one owner block; whether there was one."""
        with self._connect(timeout) as db:
            cursor = db.execute(
                """DELETE FROM route_blocks
                   WHERE server = ? AND account = ? AND character = ? AND subject = ?""",
                (self.server, self.account, character.casefold(), subject))
        return cursor.rowcount > 0

    def route_blocks(self, character: str, *, now: float | None = None,
                     timeout: float = 10.0) -> list[dict[str, Any]]:
        """Her active owner blocks, soonest expiry first. Expired rows are
        deleted on the way: a block lasts exactly until ``until``."""
        now = time.time() if now is None else now
        scope = (self.server, self.account, character.casefold())
        with self._connect(timeout) as db:
            db.execute(
                """DELETE FROM route_blocks
                   WHERE server = ? AND account = ? AND character = ? AND until <= ?""",
                (*scope, now))
            rows = db.execute(
                """SELECT subject, created_at, until, note FROM route_blocks
                   WHERE server = ? AND account = ? AND character = ?
                   ORDER BY until, subject""", scope).fetchall()
        return [{"subject": subject, "created_at": created_at, "until": until,
                 "note": note} for subject, created_at, until, note in rows]

    def _route_marker(self, character: str) -> str:
        return f"route-events-migrated:{self.server}:{self.account}:{character.casefold()}"

    def route_events_migrated(self, character: str, *, timeout: float = 10.0) -> bool:
        """Whether her pre-P5 route history has been migrated."""
        with self._connect(timeout) as db:
            return db.execute("SELECT 1 FROM metadata WHERE key = ?",
                              (self._route_marker(character),)).fetchone() is not None

    def record_route_events(self, character: str, *, events: Iterable[dict[str, Any]] = (),
                            traversals: Iterable[dict[str, Any]] = (),
                            requirements: dict[str, dict[str, Any]] | None = None,
                            migration: Any = None, now: float | None = None,
                            timeout: float = ROUTE_EVENT_SAVE_TIMEOUT) -> bool:
        """Write her route events, traversals and requirement outcomes in one
        transaction, then hold every signal to its cap and drop aged rows.

        ``migration`` (route_events.Migration) is written too, unless her
        marker says it already was: history is migrated exactly once. Returns
        whether this call migrated it. ``timeout`` bounds the wait for a
        locked database."""
        from .route_events import CAPS, EVENT_MAX_AGE, TRAVERSAL_MAX_IDLE

        now = time.time() if now is None else float(now)
        scope = (self.server, self.account, character.casefold())
        migrated = False
        with self._connect(timeout) as db:
            rows = list(events)
            seeds: list[dict[str, Any]] = []
            if migration is not None:
                marker = self._route_marker(character)
                if db.execute("SELECT 1 FROM metadata WHERE key = ?",
                              (marker,)).fetchone() is None:
                    rows = [*migration.events, *rows]
                    seeds = list(migration.traversals)
                    db.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                               (marker, json.dumps({"at": now,
                                                    "installed_at": migration.installed_at,
                                                    "events": len(migration.events),
                                                    "seeded": len(seeds)})))
                    migrated = True
            db.executemany(
                """INSERT INTO route_events
                       (server, account, character, at, subject, signal, magnitude,
                        kit, content)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(*scope, float(row["at"]), str(row["subject"]), str(row["signal"]),
                  float(row["magnitude"]), str(row.get("kit", "")),
                  str(row.get("content", ""))) for row in rows
                 if now - float(row["at"]) <= EVENT_MAX_AGE])
            db.executemany(
                """INSERT INTO route_traversals
                       (server, account, character, subject, count, mean_seconds,
                        last_at, timed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(server, account, character, subject) DO UPDATE SET
                       mean_seconds = CASE
                           WHEN route_traversals.timed + excluded.timed > 0 THEN
                               (route_traversals.mean_seconds * route_traversals.timed +
                                excluded.mean_seconds * excluded.timed) /
                               (route_traversals.timed + excluded.timed)
                           ELSE 0 END,
                       count = route_traversals.count + excluded.count,
                       timed = route_traversals.timed + excluded.timed,
                       last_at = max(route_traversals.last_at, excluded.last_at)""",
                [(*scope, str(row["subject"]), int(row["count"]),
                  float(row["mean_seconds"]), float(row["last_at"]), int(row["timed"]))
                 for row in [*seeds, *traversals] if int(row["count"]) > 0])
            db.executemany(
                """INSERT OR REPLACE INTO route_requirements
                       (server, account, character, edge, outcome, at, until, inputs,
                        evidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(*scope, edge, str(row["outcome"]), float(row["at"]), float(row["until"]),
                  str(row.get("inputs", "")), str(row.get("evidence", "")))
                 for edge, row in (requirements or {}).items()])
            # Each signal its own cap, newest kept: a flood of one never
            # evicts another's rows. Then the age limits.
            for signal, cap in CAPS.items():
                db.execute(
                    """DELETE FROM route_events WHERE rowid IN (
                           SELECT rowid FROM route_events
                           WHERE server = ? AND account = ? AND character = ?
                             AND signal = ?
                           ORDER BY at DESC, rowid DESC LIMIT -1 OFFSET ?)""",
                    (*scope, signal, cap))
            db.execute(
                """DELETE FROM route_events
                   WHERE server = ? AND account = ? AND character = ? AND at < ?""",
                (*scope, now - EVENT_MAX_AGE))
            db.execute(
                """DELETE FROM route_traversals
                   WHERE server = ? AND account = ? AND character = ? AND last_at < ?""",
                (*scope, now - TRAVERSAL_MAX_IDLE))
            db.execute(
                """DELETE FROM route_requirements
                   WHERE server = ? AND account = ? AND character = ? AND until <= ?""",
                (*scope, now))
        return migrated

    def route_events(self, character: str, *, since: float = 0.0,
                     timeout: float = 10.0) -> list[tuple[float, str, str, float]]:
        """Her route events since ``since``: (at, subject, signal,
        magnitude), oldest first."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT at, subject, signal, magnitude FROM route_events
                   WHERE server = ? AND account = ? AND character = ? AND at >= ?
                   ORDER BY at, rowid""",
                (self.server, self.account, character.casefold(), float(since))).fetchall()
        return [(float(at), str(subject), str(signal), float(magnitude))
                for at, subject, signal, magnitude in rows]

    def route_event_rows(self, character: str, *,
                         timeout: float = 10.0) -> list[dict[str, Any]]:
        """Every column of her route events (for tests and audits)."""
        with self._connect(timeout) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                """SELECT * FROM route_events
                   WHERE server = ? AND account = ? AND character = ?
                   ORDER BY at, rowid""",
                (self.server, self.account, character.casefold())).fetchall()
        return [dict(row) for row in rows]

    def route_traversals(self, character: str, *,
                         timeout: float = 10.0) -> dict[str, int]:
        """Her traversal count per subject."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT subject, count FROM route_traversals
                   WHERE server = ? AND account = ? AND character = ?""",
                (self.server, self.account, character.casefold())).fetchall()
        return {str(subject): int(count) for subject, count in rows}

    def route_traversal_rows(self, character: str, *,
                             timeout: float = 10.0) -> dict[str, dict[str, Any]]:
        """Her traversal rows per subject: count, timed, mean seconds, last."""
        with self._connect(timeout) as db:
            rows = db.execute(
                """SELECT subject, count, timed, mean_seconds, last_at FROM route_traversals
                   WHERE server = ? AND account = ? AND character = ?""",
                (self.server, self.account, character.casefold())).fetchall()
        return {str(subject): {"count": int(count), "timed": int(timed),
                               "mean_seconds": float(mean), "last_at": float(last)}
                for subject, count, timed, mean, last in rows}

    def route_requirements(self, character: str, *, now: float | None = None,
                           inputs: dict[str, str] | None = None,
                           timeout: float = 10.0) -> dict[str, dict[str, Any]]:
        """Her requirement outcomes per edge. Expired rows, and rows of an
        edge in ``inputs`` whose fingerprint no longer matches, are deleted
        on the way. A row kept with no fingerprint depends on no item or
        quest state and is kept until it expires."""
        now = time.time() if now is None else now
        scope = (self.server, self.account, character.casefold())
        with self._connect(timeout) as db:
            db.execute(
                """DELETE FROM route_requirements
                   WHERE server = ? AND account = ? AND character = ? AND until <= ?""",
                (*scope, now))
            for edge, fingerprint in (inputs or {}).items():
                db.execute(
                    """DELETE FROM route_requirements
                       WHERE server = ? AND account = ? AND character = ? AND edge = ?
                         AND inputs != '' AND inputs != ?""", (*scope, edge, fingerprint))
            rows = db.execute(
                """SELECT edge, outcome, at, until, inputs, evidence FROM route_requirements
                   WHERE server = ? AND account = ? AND character = ?""", scope).fetchall()
        return {str(edge): {"outcome": outcome, "at": at, "until": until,
                            "inputs": fingerprint, "evidence": evidence}
                for edge, outcome, at, until, fingerprint, evidence in rows}

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
