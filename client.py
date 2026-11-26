"""Async headless Atrinik client for the current binary socket protocol."""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import copy
import json
import logging
import math
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import builds
from . import constants as c
from .access_codes import normalize_access_code
from .farm_analyzer import DEFAULT_OBJECTIVE, OBJECTIVES
from .model import (STARTER_APARTMENT, STARTER_APARTMENT_BED, Character, GameState,
                    InterfaceState, Item, MapObject, MapState, PartyVitals,
                    TileAnimation)
from .alerts import ProgressWatch
from .memory import BotMemory
from .metrics import BotMetrics
from . import facts as fact_parsers
from . import feedback
from . import feedback_producers
from .observations import AuthoredContent, Observation, ObservationRecorder, text_hash
from . import post_mortem
from .owner_watch import OwnerWatchRecorder, name_digest
from .owner_watch import capture as owner_watch_capture
from .terrain import FightRecorder
from . import route_events, route_shadow
from . import storage
from .self_improvement import PLAYER_CHANNELS, from_server
from .trends import Trends
from .protocol import Cursor, Event, Packet, ProtocolError, decompress_frame, read_frame
from .transport import QuicStream

# Item types that cast a spell when fired or applied; a no-magic square
# refuses them all (server spell_util.c cast_spell).
MAGIC_CAST_TYPES = frozenset((c.TYPE_SPELL, c.TYPE_SCROLL, c.TYPE_ROD,
                              c.TYPE_WAND))
# The longest a death waits for a locked memory database (seconds).
POST_MORTEM_SAVE_TIMEOUT = 0.25
log = logging.getLogger(__name__)

# Memory kind holding the owner's persisted Stop (see operator_stopped).
OPERATOR_STOP_KIND = "operator_stop"

# Words that open a line with a comma without addressing anyone ("Hi, ..."),
# matched with stretched letters folded ("Heyy,", "Hmmm," and "Hellooo,").
CHAT_INTERJECTIONS = frozenset((
    "ah", "alright", "aye", "btw", "cool", "great", "greetings", "haha", "hehe",
    "hey", "heya", "hi", "hiya", "hello", "hm", "howdy", "lmao", "lol",
    "nice", "no", "nope", "np", "oh", "ok", "okay", "oops", "so",
    "sorry", "sure", "thanks", "ty", "um", "wait", "welcome", "well", "wow",
    "yay", "yeah", "yep", "yes", "yo"))


def _fold_letters(word: str) -> str:
    return re.sub(r"(.)\1+", r"\1", word)


CHAT_INTERJECTIONS_FOLDED = frozenset(map(_fold_letters, CHAT_INTERJECTIONS))
# A player gets at most one such fallback reply per this many seconds.
CHAT_FALLBACK_SECONDS = 600.0
# And at most this many replies of any kind per window: no conversation
# with another auto-responder can run on.
CHAT_REPLIES_PER_SENDER = 5
CHAT_REPLY_WINDOW_SECONDS = 300.0
# Friendly replies to a message meant for her that no rule answers, in turn.
CHAT_FALLBACKS = (
    "Ha, I'm not sure! I'm busy training right now.",
    "Sorry, I didn't quite get that. I'm out training!",
    "Hmm, good question! I'll have to think about that one.",
    "I hear you! Can't stop right now, I'm in the middle of training.",
)
EventHandler = Callable[[Event], Awaitable[None] | None]

COIN_VALUES = {
    "copper": 1, "silver": 100, "gold": 10_000,
    "jade": 1_000_000, "mithril": 10_000_000,
    "amber": 100_000_000,
}
# How a copper amount is written for people, largest coin first.
COIN_LABELS = (
    (100_000_000, " amber"), (10_000_000, " mithril"), (1_000_000, " jade"),
    (10_000, "g"), (100, "s"), (1, "c"),
)


def format_money(copper: float) -> str:
    """A copper amount in the game's coins, e.g. ``76g 81s 92c``."""
    # Halves round up, as the dashboard's Math.round does.
    value = math.floor(copper + 0.5)
    sign, value = ("-" if value < 0 else ""), abs(value)
    parts = []
    for worth, label in COIN_LABELS:
        count, value = divmod(value, worth)
        if count:
            parts.append(f"{count}{label}")
    return sign + (" ".join(parts) or "0c")


def parse_money_value(text: str) -> int | None:
    """Parse a server CostString fragment into its copper value."""
    total = sum(
        int(amount.replace(",", "")) * COIN_VALUES[denomination.casefold()]
        for amount, denomination in re.findall(
            r"([\d,]+)\s+(copper|silver|gold|jade|mithril|amber)", text, re.I)
    )
    return total or None


def update_bank_balance(state: GameState, text: str) -> bool:
    if re.search(r"(?:have|has) no money stored in (?:your|the) bank", text,
                 re.I):
        state.bank_balance = 0
        state.bank_balance_known = True
        return True
    match = re.search(r"your (?:new )?balance is ([^.]+)", text, re.I)
    if match is None:
        return False
    value = parse_money_value(match.group(1))
    if value is None:
        return False
    state.bank_balance = value
    state.bank_balance_known = True
    return True


def map_object_visual_name(client, obj: MapObject) -> str:
    """Resolve the wire visual ID in its conditional namespace.

    MAP_FLAG_ANIMATION changes the nominal face uint16 into an animation ID.
    Looking that number up in bmaps produces an unrelated but valid sprite.
    """
    if obj.animation:
        name = getattr(client, "animations", {}).get(obj.animation, "")
        if name:
            return name
    return getattr(client, "faces", {}).get(obj.face, "")


def party_vitals(cur: Cursor, *, repeated: bool) -> list[PartyVitals]:
    """``CMD_PARTY_UPDATE`` (one member) or ``CMD_PARTY_WHO`` (every member)
    after the command byte: each a name, then health and mana percent,
    which the server clamps to 1..100 (server ``party.c``,
    ``commands/player/party.c``). A truncated record or trailing bytes
    are a protocol error, like the other party commands."""
    members: list[PartyVitals] = []
    while not members or repeated and cur.remaining:
        name, hp, sp = cur.cstring(), cur.u8(), cur.u8()
        members.append(PartyVitals(name, min(hp, 100), min(sp, 100)))
    if cur.remaining:
        raise ProtocolError("party vitals trailing bytes")
    return members


def logical_map_path(path: str) -> str:
    """Convert a server-side player-unique filename to its authored path."""
    if path.startswith("./data/players/"):
        encoded = path.rsplit("/", 1)[-1]
        if encoded.startswith("$"):
            return encoded.replace("$", "/")
    return path


@dataclass(slots=True)
class ClientConfig:
    host: str = "127.0.0.1"
    port: int = 1728
    transport: str = "auto"
    quic_port: int = 1730
    certificate_sha256: str = ""
    connect_timeout: float = 5.0
    account: str = ""
    password: str = ""
    character: str = ""
    party_name: str = ""
    access_code: str = field(default="", repr=False)
    # The server accepts viewports up to 17x17. Asking for a larger map causes
    # it to reject the setup field and silently use its fallback dimensions.
    map_width: int = 17
    map_height: int = 17
    register: bool = False
    create_character: bool = False
    character_archetype: str = "half_elf_male"
    reconnect: bool = True
    reconnect_delay: float = 3.0
    runtime_state_path: str = ""
    chat_rules_path: str = ""
    runtime_content_path: str = ""
    # docs/LEARNING_AND_FEEDBACK.md 4.11.1: whether a persistent no-magic
    # tile fact puts a non-magical heal first on its square. Off (the
    # default) they are only written and shown.
    persist_tile_facts: bool = False
    # docs/ROUTING.md P4: plan with NPC dialogue voyages and quest-gated
    # exits (dialogue_edges.py). Off (the default) plans exactly as before;
    # the owner turns it on after one supervised voyage.
    dialogue_edges: bool = False
    # docs/ROUTING.md P6/P7: plan with learned route costs, errand budgets
    # and the essential override (route_costs.py). Off (the default), plans
    # and farm choices are as before and the learned ones run in the shadow
    # (route_shadow.py); the owner turns it on after the shadow soak.
    learned_route_costs: bool = False
    # Another character on the same account (ATRINIK_BOT_OPERATOR_CHARACTER):
    # the only player whose party vitals she keeps and, behind
    # ``watch_owner``, whom she watches play.
    operator_character: str = ""
    # docs/LEARNING_AND_FEEDBACK.md 4.14, phase W1: record how the operator
    # character fights while she is not fighting herself (owner_watch.py).
    # Off (the default) nothing is recorded; the client still decodes tile
    # animations into a bounded ring and keeps the operator's vitals.
    watch_owner: bool = False
    # Whether food still matters (hunger.food_obsolete): "on", "off" or
    # "auto". Off (the default here) keeps the hunger rules; the command
    # line defaults to on, for the live server (Classic 5.119.0 and later).
    food_obsolete: str = "off"


# kill_player (server types/player.c) sends exactly one of these, as the whole
# message, per defeat: a PvP-area defeat, or a death after its stat losses.
# Matching the whole text keeps a player's chat that quotes one from passing
# for a death.
DEATH_NOTICES = ("you have been defeated in combat!", "you have died.")

# Tile facts (facts.py): positions of recent refusal lines remembered, the
# least time between writes of the same fact, and the wait after a failed
# write.
TILE_REFUSAL_SQUARES = 64
TILE_FACT_REFRESH_SECONDS = 3600.0
TILE_FACT_RETRY_SECONDS = 60.0
# The longest a feedback producer waits for a locked database (seconds):
# drafts are best effort and never stall a tick.
FEEDBACK_READ_TIMEOUT = 0.25
EQUIPMENT_FEEDBACK_SECONDS = 30.0
# How often the running bot screens the feedback queue with her full
# private terms (approved items, and items the owner edited), so the CLI
# may approve them and the filer file them (phase F6).
FEEDBACK_SCREEN_SECONDS = 60.0


def _locked_content_inputs() -> str:
    """``content:<revision>`` of the locked content, or "" without the lock."""
    try:
        from .compatibility import load_lock
        revision = load_lock()["inputs"]["content"]["revision"]
        return f"content:{revision}" if isinstance(revision, str) and revision else ""
    except Exception:
        return ""


def _after(records: list, mark: Any) -> list:
    """The records after ``mark``: found by identity, or by value in a
    reloaded history (a decoded record equals the one saved); all of them
    when it is gone (trimmed away)."""
    if mark is not None:
        for same in (lambda record: record is mark, lambda record: record == mark):
            for index in range(len(records) - 1, -1, -1):
                if same(records[index]):
                    return records[index + 1:]
    return records


class AtrinikClient:
    BANK_BALANCE_CACHE_SECONDS = 6 * 60 * 60
    # Prometheus activity metrics; None on a client built without __init__.
    metrics: BotMetrics | None = None

    """Maintains a live state model and exposes typed game actions."""

    def __init__(self, config: ClientConfig):
        self.config = config
        self.state = GameState(account=config.account,
                               food_obsolete=config.food_obsolete)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._quic_stream: QuicStream | None = None
        self.transport = ""
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._handlers: list[EventHandler] = []
        self._reconnect_requested = False
        self._closed = False
        self._map_pending = None
        self._setup_sent = False
        self._access_accepted = False
        self._account_sent = False
        self._character_sent = False
        # Her keepalives (_keepalive_loop): the last id sent, the ones not
        # yet echoed and when each was sent (time.monotonic()), when the last
        # echo came back, and its round trip in seconds (None before one).
        self._keepalive_id = 0
        self._keepalive_sent: dict[int, float] = {}
        self._last_keepalive = 0.0
        self.keepalive_latency: float | None = None
        self._death_depletion_until = 0.0
        self._pending_death_losses: list[tuple[float, str]] = []
        self._party_setup_name = ""
        self._party_setup_pending = ""
        self._last_chat_reply_at = 0.0
        self._last_chat_prompt = ""
        self._chat_correspondents: dict[str, float] = {}
        self._chat_rules_path = (
            Path(config.chat_rules_path) if config.chat_rules_path else
            Path(__file__).with_name("chat_rules.json"))
        self._chat_rules_signature: tuple[int, int, int] | None = None
        self._chat_rules_failed_signature: tuple[int, int, int] | None = None
        self._chat_rules: list[
            tuple[str, tuple[re.Pattern, ...], tuple[str, ...]]] = []
        self._chat_rule_uses: dict[str, int] = {}
        self._chat_fallback_uses = 0
        self._chat_fallback_at: dict[str, float] = {}
        self._chat_replies: dict[str, deque[float]] = {}
        self.chat_context_provider: Callable[[], dict[str, str]] | None = None
        self._reload_chat_rules()
        requested_memory = (Path(config.runtime_state_path)
                            if config.runtime_state_path else
                            Path(__file__).with_name("bot_memory.sqlite3"))
        if requested_memory.suffix.casefold() == ".json":
            self._legacy_runtime_state_path = requested_memory
            self._memory_path = requested_memory.with_suffix(".sqlite3")
        else:
            self._memory_path = requested_memory
            self._legacy_runtime_state_path = (
                requested_memory.with_suffix(".json")
                if config.runtime_state_path else
                Path(__file__).with_name("runtime_state.json"))
        self._legacy_activity_history_path = (
            self._legacy_runtime_state_path.with_name(
                self._legacy_runtime_state_path.stem + ".activity.json"))
        server_key = f"{config.host.casefold()}:{config.port}"
        self._memory_server_key = server_key
        self.memory: BotMemory | None = None
        self._runtime_memory: dict[str, dict[str, int]] = {}
        self.action_history: list[dict] = []
        # Every message of the last review window, for post-mortems: the UI's
        # 500-message buffer can drop a busy minute's oldest hits.
        # Bounded, and pruned from the front in O(1): a busy minute's 20,000
        # messages after a long gap cost no shifting.
        self.review_messages: deque[tuple] = deque(maxlen=post_mortem.REVIEW_HISTORY_LIMIT)
        self._memory_character = ""
        self.decision_history: list[dict] = []
        # The square and message position of the last spell cast.
        # The square, message position and wall-clock time of the last
        # spell cast.
        self.spell_cast: tuple[tuple[str, int, int], int, float] | None = None
        # Health, SP and food samples, and the deaths reviewed from them.
        self.vitals: deque[list[float]] = deque(maxlen=post_mortem.MAX_LIVE_VITALS)
        # Creature kinds whose hits outpaced her heals, until when
        # (monotonic; FarmTask.out_healed).
        self.out_of_league_kinds: dict[str, float] = {}
        # Each other character's live reports, while this client plays another.
        self._live_post_mortems: dict[str, list[dict]] = {}
        # The last record of each history at the previous report: the next
        # report reads only what follows (identity, not time).
        self._life_marks: dict[str, Any] = {}
        # When the last death's bookkeeping ended (its records are its own).
        self._post_mortem_booked = 0.0
        self.post_mortems: list[dict] = []
        # Whether a mana-crystal backoff without a character to persist it
        # for is already logged (set_mana_crystal_retry).
        self._mana_crystal_unpersisted_warned = False
        # (time, path) of each arrival on a new map, newest last.
        self.map_arrivals: list[tuple[float, str]] = []
        # Per-minute XP, deaths, heals, depletion and food for the dashboard.
        self.trends = Trends()
        self._trends_character = ""
        # What she does, for the dashboard's Prometheus endpoint.
        self.metrics = BotMetrics(self)
        self.add_handler(self.metrics.on_event)
        # When XP, position and the connection last changed, for alerts.
        self.progress_watch = ProgressWatch()
        self.action_context = ""
        self._runtime_content_root = (
            Path(config.runtime_content_path) if config.runtime_content_path else
            Path(os.environ["ATRINIK_RUNTIME_CONTENT"])
            if "ATRINIK_RUNTIME_CONTENT" in os.environ else
            Path(__file__).resolve().parents[2]
        )
        self.faces = self._load_faces()
        self.animations = self._load_animations()
        # Background farm ranking; set by the CLI when the bundle provides it.
        self.farm_analyzer = None
        # Her own readings (observations.py), kept in memory's observations
        # table. The authored-text index is loaded by the CLI at start-up;
        # without it only unattributed fact lines are kept.
        self.authored_content: AuthoredContent | None = None
        self.observations = ObservationRecorder(
            lambda: self.authored_content, self._observation_dropped)
        # Her fights by terrain (terrain.py), fed by the task engine's steps
        # (tasks.observe_fight) and her death and recall decisions.
        self.fight_recorder = FightRecorder()
        # How the operator character fights, watched only behind the
        # owner's watch_owner flag (owner_watch.py), fed like the above.
        self.owner_watch = OwnerWatchRecorder()
        # The authored world graph the CLI built (web and autoplay), for
        # the terrain of her fights; without it a fight's feature is unknown.
        self.world_graph = None
        # Her route events (route_events.py, docs/ROUTING.md 4.4): what
        # crossing the world costs her, written best effort.
        self.route_recorder = route_events.RouteRecorder()
        # Shadow planning with learned costs (route_shadow.py, ROUTING P6).
        self.route_shadow = route_shadow.RouteShadow()
        # The feedback queue (feedback.py): (fingerprint, occurrence, detail)
        # already noted in this process, so a repeat costs no write, and
        # other names drafts must never contain (the operator character).
        self._feedback_noted: set[tuple[str, str, str]] = set()
        self.feedback_private_names: set[str] = set()
        # Chat senders her memory keeps, read once a login and extended by
        # each new sender (_log_chat); None until first needed.
        self._feedback_senders: set[str] | None = None
        # Private terms already warned about as common words.
        self._feedback_common_warned: set[str] = set()
        # When the "+N with no effect" producer last ran (monotonic).
        self._equipment_feedback_at = float("-inf")
        # Items needing the owner after filing (filed-unlabelled,
        # needs-review, failed), counted by the queue screen for the
        # dashboard's alerts; never read from the database on a live poll.
        self.feedback_attention: dict[str, int] = {}
        # Her confirmed tile facts (facts.py), by (map, x, y), x = y = -1 for
        # a whole map, loaded with her memory: each property and when it was
        # last confirmed (wall time).
        self.tile_facts: dict[tuple[str, int, int], dict[str, float]] = {}
        # The content revision tile facts are recorded under; a fact from
        # another revision is ignored.
        self.content_inputs = _locked_content_inputs()
        # When each tile fact was last written in this process: a repeat
        # within TILE_FACT_REFRESH_SECONDS costs no write.
        self._tile_facts_noted: dict[tuple[tuple[str, int, int], str], float] = {}
        # After a failed write, no tile-fact write before this (monotonic).
        self._tile_fact_retry_at = 0.0
        # Decisions a tile fact already recorded in this process.
        self._tile_fact_decisions: set[tuple[tuple[str, int, int], str]] = set()
        # Where she stood when each recent tile refusal line arrived, by
        # message position.
        self._tile_refusal_squares: dict[int, tuple[str, int, int]] = {}
        # When she last sent a step, and when a map update last moved her
        # (monotonic): a cast between the two may be on either square.
        self._step_sent_at = 0.0
        self._position_at = 0.0
        self._position: tuple[str, int, int] | None = None
        # Whether the square recorded with the last cast (spell_cast) is
        # uncertain that way.
        self._spell_cast_uncertain = False

    def load_character_memory(self, character: str) -> None:
        # Switching characters on a long-lived client: the previous one's
        # vitals, messages and actions must not explain this one's death.
        same_character = self._memory_character == character
        if self._memory_character and not same_character:
            self.vitals.clear()
            self.review_messages.clear()
            self.out_of_league_kinds.clear()
            self.action_history.clear()
            # A loss buffered for the previous character's death is not hers.
            self._pending_death_losses.clear()
            self._death_depletion_until = 0.0
            # Another character's death bounds nothing in this one's life.
            self._post_mortem_booked = 0.0
            # Kept for that character, not dropped: a report whose save failed
            # is still merged when she comes back.
            if self.post_mortems:
                self._live_post_mortems.pop(self._memory_character, None)
                self._live_post_mortems[self._memory_character] = self.post_mortems
                while len(self._live_post_mortems) > post_mortem.MAX_CACHED_CHARACTERS:
                    del self._live_post_mortems[next(iter(self._live_post_mortems))]
            self.post_mortems = []
        self._memory_character = character
        self._route_events("load", lambda recorder: recorder.load(self, character))
        memory = self._memory_store()
        memory.import_legacy(
            self._legacy_runtime_state_path,
            self._legacy_activity_history_path)
        record = memory.load(character, "state", {})
        record = record if isinstance(record, dict) else {}
        self._runtime_memory = {character: record}
        records = memory.load(character, "decisions", [])
        self.decision_history = [
            value for value in records if isinstance(value, dict)
        ][-2000:] if isinstance(records, list) else []
        self._load_tile_facts(character)
        self._feedback_senders = None
        self._feedback_sweep(character)
        # The life boundary in this (possibly reloaded) ledger: the previous
        # report's mark, found by value, or, in a fresh process with no mark,
        # the restored ledger's last death.
        mark = self._life_marks.get("decisions")
        if mark is None or mark not in self.decision_history:
            self._life_marks["decisions"] = next(
                (record for record in reversed(self.decision_history) if record.get("action") == "death"), None)
        # Newest first, stopping at KEEP valid reports, and looking at no more
        # than a few times KEEP candidates: an oversized stored list, valid or
        # not, is never normalized whole.
        deaths = memory.load(character, "post_mortems", [], max_bytes=post_mortem.MAX_STORED_BYTES)
        kept: list[dict] = []
        for value in reversed((deaths if isinstance(deaths, list) else [])[-4 * post_mortem.KEEP:]):
            if len(kept) == post_mortem.KEEP:
                break
            if (restored := post_mortem.restore(value)) is not None:
                kept.append(restored)
        # A reconnect reloads the same character: its live reports may be
        # newer than the store (a save can fail), so they win for the same
        # death and are never dropped for the older saved list. Order is the
        # order they were recorded, live last (never wall time, which can
        # step back), so the cap drops the oldest saved ones first.
        live = self.post_mortems if same_character else self._live_post_mortems.pop(character, [])
        live_times = {death["time"] for death in live}
        self.post_mortems = ([death for death in kept[::-1] if death["time"] not in live_times]
                             + list(live))[-post_mortem.KEEP:]
        buckets = memory.load(character, "trends", [])
        saved = Trends(buckets if isinstance(buckets, list) else ()).buckets
        # A reconnect reloads the same character: its live buckets are newer
        # than the saved snapshot (the open minute is saved only when the
        # next starts), so they win for the same minute.
        live = self.trends.buckets if self._trends_character == character else []
        merged = {bucket["t"]: bucket for bucket in saved}
        merged.update((bucket["t"], bucket) for bucket in live)
        self.trends = Trends(sorted(merged.values(), key=lambda bucket: bucket["t"]))
        self._trends_character = character
        if isinstance(record, dict) and "depletion_points" in record:
            self.state.depletion_points = max(
                0, int(record.get("depletion_points", 0)))
        else:
            self.state.depletion_points = 0
        self.state.depletion_points_known = True
        if isinstance(record, dict):
            try:
                self.state.last_upgrade_shop_sweep_at = max(
                    0.0, float(record.get(
                        "last_upgrade_shop_sweep_at", 0.0)))
            except (TypeError, ValueError):
                self.state.last_upgrade_shop_sweep_at = 0.0
            try:
                self.state.last_upgrade_shop_sweep_level = max(
                    0, int(record.get(
                        "last_upgrade_shop_sweep_level", 0)))
                self.state.last_upgrade_shop_sweep_wallet = max(
                    0, int(record.get(
                        "last_upgrade_shop_sweep_wallet", 0)))
                self.state.last_upgrade_shop_sweep_policy = max(
                    0, int(record.get(
                        "last_upgrade_shop_sweep_policy", 0)))
                self.state.active_upgrade_shop_sweep_policy = max(
                    0, int(record.get(
                        "active_upgrade_shop_sweep_policy", 0)))
                self.state.active_upgrade_shop_sweep_cursor = max(
                    0, int(record.get(
                        "active_upgrade_shop_sweep_cursor", 0)))
            except (TypeError, ValueError):
                self.state.last_upgrade_shop_sweep_level = 0
                self.state.last_upgrade_shop_sweep_wallet = 0
                self.state.last_upgrade_shop_sweep_policy = 0
                self.state.active_upgrade_shop_sweep_policy = 0
                self.state.active_upgrade_shop_sweep_cursor = 0
            try:
                self.state.last_recall_shop_check_at = max(
                    0.0, float(record.get(
                        "last_recall_shop_check_at", 0.0)))
            except (TypeError, ValueError):
                self.state.last_recall_shop_check_at = 0.0
            try:
                self.state.recall_unavailable = str(
                    record.get("recall_unavailable", "") or "")[:500]
                self.state.recall_unavailable_at = max(
                    0.0, float(record.get("recall_unavailable_at", 0.0) or 0.0))
            except (TypeError, ValueError):
                self.state.recall_unavailable = ""
                self.state.recall_unavailable_at = 0.0
            try:
                self.state.last_utility_shop_check_at = max(
                    0.0, float(record.get(
                        "last_utility_shop_check_at", 0.0)))
            except (TypeError, ValueError):
                self.state.last_utility_shop_check_at = 0.0
            try:
                self.state.heal_restock_note = str(
                    record.get("heal_restock_note", "") or "")[:500]
                self.state.heal_restock_retry_until = max(
                    0.0, float(record.get(
                        "heal_restock_retry_until", 0.0) or 0.0))
            except (TypeError, ValueError):
                self.state.heal_restock_note = ""
                self.state.heal_restock_retry_until = 0.0
            self.state.apartment_storage = storage.load_index(
                record.get("apartment_storage", []))
            try:
                self.state.paid_restoration_retry_until = max(
                    0.0, float(record.get(
                        "paid_restoration_retry_until", 0.0)))
            except (TypeError, ValueError):
                self.state.paid_restoration_retry_until = 0.0
            try:
                self.state.cure_funding_farm_since = max(0.0, float(
                    record.get("cure_funding_farm_since", 0.0) or 0.0))
                self.state.cure_funding_farm_deaths = max(0, int(
                    record.get("cure_funding_farm_deaths", 0) or 0))
                self.state.cure_funding_farm_counted_at = max(0.0, float(
                    record.get("cure_funding_farm_counted_at", 0.0) or 0.0))
                self.state.cure_funding_farm_funded_at = max(0.0, float(
                    record.get("cure_funding_farm_funded_at", 0.0) or 0.0))
                self.state.cure_funding_farm_retry_until = max(0.0, float(
                    record.get("cure_funding_farm_retry_until", 0.0) or 0.0))
                if not all(math.isfinite(value) for value in (
                        self.state.cure_funding_farm_since,
                        self.state.cure_funding_farm_counted_at,
                        self.state.cure_funding_farm_funded_at,
                        self.state.cure_funding_farm_retry_until)):
                    raise ValueError("not finite")
            except (TypeError, ValueError, OverflowError):
                self.state.cure_funding_farm_since = 0.0
                self.state.cure_funding_farm_deaths = 0
                self.state.cure_funding_farm_counted_at = 0.0
                self.state.cure_funding_farm_funded_at = 0.0
                self.state.cure_funding_farm_retry_until = 0.0
            backoffs = record.get("mana_crystal_retry_until", {})
            self.state.mana_crystal_retry_until = {
                str(detour): float(until)
                for detour, until in (backoffs.items() if isinstance(backoffs, dict) else ())
                if isinstance(until, (int, float)) and not isinstance(until, bool) and
                math.isfinite(until) and until > 0}
            hazards = record.get("route_hazards", {})
            self.state.route_hazards = {
                str(path): float(until)
                for path, until in (hazards.items() if isinstance(hazards, dict) else ())
                if isinstance(until, (int, float))}
            quotes = record.get("vendor_quotes", {})
            self.state.vendor_quotes = {
                str(key): dict(value)
                for key, value in quotes.items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(quotes, dict) else {}
            quarantine = record.get("farm_zone_quarantine", {})
            if isinstance(quarantine, dict):
                now = time.time()
                self.state.farm_zone_quarantine = {
                    str(path): float(until)
                    for path, until in quarantine.items()
                    if isinstance(path, str) and
                    isinstance(until, (int, float)) and float(until) > now
                }
            else:
                self.state.farm_zone_quarantine = {}
            holds = record.get("farm_train_holds", {})
            now = time.time()
            self.state.farm_train_holds = {
                str(path): [float(at) for at in times
                            if isinstance(at, (int, float)) and
                            not isinstance(at, bool) and
                            math.isfinite(float(at)) and
                            0.0 <= now - float(at) < 86400.0]
                for path, times in (holds.items()
                                    if isinstance(holds, dict) else ())
                if isinstance(path, str) and isinstance(times, list)}
            self.state.farm_train_holds = {
                path: times for path, times in
                self.state.farm_train_holds.items() if times}
            checks = record.get("farm_zone_last_checked", {})
            if isinstance(checks, dict):
                self.state.farm_zone_last_checked = {
                    str(path): max(0.0, float(checked_at))
                    for path, checked_at in checks.items()
                    if isinstance(path, str) and
                    isinstance(checked_at, (int, float))
                }
            else:
                self.state.farm_zone_last_checked = {}
            lore_attempts = record.get("lore_attempt_counts", {})
            if isinstance(lore_attempts, dict):
                self.state.lore_attempt_counts = {
                    str(signature): max(0, int(count))
                    for signature, count in lore_attempts.items()
                    if isinstance(signature, str) and
                    isinstance(count, (int, float))
                }
            else:
                self.state.lore_attempt_counts = {}
            self.state.apartment_bed_bound = bool(
                record.get("apartment_bed_bound", False))
            savebed = record.get("savebed")
            if (isinstance(savebed, list) and len(savebed) == 3 and
                    isinstance(savebed[0], str) and
                    all(isinstance(v, int) for v in savebed[1:])):
                self.state.savebed_map = savebed[0]
                self.state.savebed_xy = (savebed[1], savebed[2])
            elif self.state.apartment_bed_bound:
                # Records from before savebeds were tracked by location.
                self.state.savebed_map = STARTER_APARTMENT
                self.state.savebed_xy = STARTER_APARTMENT_BED
            else:
                self.state.savebed_map = ""
                self.state.savebed_xy = None
            returns = record.get("apartment_returns", {})
            self.state.apartment_returns = {
                apartment: (back[0], (back[1], back[2]))
                for apartment, back in (returns.items() if isinstance(returns, dict) else ())
                if isinstance(apartment, str) and isinstance(back, list) and len(back) == 3
                and isinstance(back[0], str) and all(isinstance(v, int) for v in back[1:])
            }
            adaptations = record.get("farm_adaptations", {})
            self.state.farm_adaptations = {
                str(key): dict(value) for key, value in adaptations.items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(adaptations, dict) else {}
            objective = record.get("farm_objective", DEFAULT_OBJECTIVE)
            self.state.farm_objective = (
                objective if objective in OBJECTIVES else DEFAULT_OBJECTIVE)
            choices = record.get("build_choices", {})
            loaded = {
                str(spot)[:300]: value for spot, value in choices.items()
                if isinstance(spot, str) and value in builds.CHOICES
            } if isinstance(choices, dict) else {}
            self.state.spell_samples = builds.load_spell_samples(
                record.get("spell_samples", {}))
            self.state.build_state = builds.load_build_state(record.get("build_state", {}))
            for name, empty in (("active_build", ""), ("active_build_since", 0.0),
                                ("pending_swap_build", "")):
                setattr(self, name, self.state.build_state.get(name, empty))
            # The most recent spots, never the every-spot choice dropped.
            spots = [key for key in loaded if key != builds.EVERY_SPOT]
            kept = spots[-(builds.MAX_BUILD_CHOICES - (builds.EVERY_SPOT in loaded)):]
            self.state.build_choices = {
                key: loaded[key] for key in loaded
                if key == builds.EVERY_SPOT or key in set(kept)}
            try:
                self.state.last_bank_deposit_at = max(
                    0.0, float(record.get("last_bank_deposit_at", 0.0)))
            except (TypeError, ValueError):
                self.state.last_bank_deposit_at = 0.0
            try:
                observed_at = min(time.time(), max(
                    0.0, float(record.get("bank_balance_observed_at", 0.0))))
                fresh = (observed_at > 0 and
                         time.time() - observed_at <=
                         self.BANK_BALANCE_CACHE_SECONDS)
                self.state.bank_balance = max(
                    0, int(record.get("bank_balance", 0))) if fresh else 0
                self.state.bank_balance_known = fresh
                self.state.bank_balance_observed_at = (
                    observed_at if fresh else 0.0)
            except (TypeError, ValueError):
                self.state.bank_balance = 0
                self.state.bank_balance_known = False
                self.state.bank_balance_observed_at = 0.0
        else:
            self.state.last_upgrade_shop_sweep_at = 0.0
            self.state.last_upgrade_shop_sweep_level = 0
            self.state.last_upgrade_shop_sweep_wallet = 0
            self.state.last_upgrade_shop_sweep_policy = 0
            self.state.active_upgrade_shop_sweep_policy = 0
            self.state.active_upgrade_shop_sweep_cursor = 0
            self.state.last_recall_shop_check_at = 0.0
            self.state.recall_unavailable = ""
            self.state.recall_unavailable_at = 0.0
            self.state.last_utility_shop_check_at = 0.0
            self.state.heal_restock_note = ""
            self.state.heal_restock_retry_until = 0.0
            self.state.apartment_storage = []
            self.state.paid_restoration_retry_until = 0.0
            self.state.cure_funding_farm_since = 0.0
            self.state.cure_funding_farm_deaths = 0
            self.state.cure_funding_farm_counted_at = 0.0
            self.state.cure_funding_farm_funded_at = 0.0
            self.state.cure_funding_farm_retry_until = 0.0
            self.state.mana_crystal_retry_until = {}
            self.state.route_hazards = {}
            self.state.vendor_quotes = {}
            self.state.farm_zone_quarantine = {}
            self.state.farm_train_holds = {}
            self.state.farm_zone_last_checked = {}
            self.state.lore_attempt_counts = {}
            self.state.apartment_bed_bound = False
            self.state.savebed_map = ""
            self.state.savebed_xy = None
            self.state.apartment_returns = {}
            self.state.farm_objective = DEFAULT_OBJECTIVE
            self.state.build_choices = {}
            self.state.spell_samples = {}
            self.state.build_state = {}
            for name in ("active_build", "pending_swap_build"):
                setattr(self, name, "")
            self.active_build_since = 0.0
            self.state.farm_adaptations = {}
            self.state.bank_balance = 0
            self.state.bank_balance_known = False
            self.state.bank_balance_observed_at = 0.0
            self.state.last_bank_deposit_at = 0.0

    def _persist_character_memory(self) -> None:
        character = self.state.player_name or self.config.character
        if character:
            self._memory_store().save(
                character, "state",
                self._runtime_memory.get(character, {}))

    def _load_tile_facts(self, character: str) -> None:
        """Her confirmed, unexpired tile facts of this content revision."""
        self.tile_facts = {}
        self._tile_facts_noted.clear()
        self._tile_fact_decisions.clear()
        if not character:
            return
        try:
            rows = self._memory_store().tile_facts(
                character, now=time.time(), inputs=self.content_inputs)
        except Exception as error:  # Never fails a login.
            log.warning("tile facts not loaded: %s", error)
            return
        for subject, value, at in rows:
            square = fact_parsers.parse_tile_subject(subject)
            if square is not None:
                self.tile_facts.setdefault(square, {})[value] = at

    def _note_tile_refusal_line(self, text: str) -> None:
        """Where she stands as a tile refusal arrives (note_tile_refusal)."""
        if (not from_server(self.state.messages[-1]) or
                fact_parsers.tile_property(text) is None):
            return
        m = self.state.map
        self._tile_refusal_squares[self.state.message_count - 1] = (
            m.path, m.world_x, m.world_y)
        while len(self._tile_refusal_squares) > TILE_REFUSAL_SQUARES:
            del self._tile_refusal_squares[next(iter(self._tile_refusal_squares))]

    def note_tile_refusal(self, position: int, value: str, whole_map: bool,
                          text: str, attributed: bool) -> None:
        """A tile refusal ``tasks.spell_blocked_here`` read at message
        ``position``; ``attributed`` when her recent cast explains it.

        Kept as a fact only when it certainly belongs to the cast: for a
        whole map, she was on the cast's map when the line arrived; for a
        square, she still stood on the square she cast from. Otherwise
        (a potion drunk after an exit, say) nothing is kept.
        """
        cast = self.spell_cast
        arrival = self._tile_refusal_squares.get(position)
        if not attributed or cast is None or arrival is None or self._spell_cast_uncertain:
            return
        if whole_map:
            if cast[0][0] != arrival[0]:
                return
            where = (arrival[0], -1, -1)
        elif cast[0] != arrival:
            return
        else:
            where = arrival
        self.note_tile_fact(where, value, text)

    def note_tile_fact(self, square: tuple[str, int, int], value: str,
                       text: str) -> None:
        """Keep a fixed server refusal at ``square`` (x = y = -1: the whole
        map) as a confirmed tile fact (facts.py) for TILE_FACT_SECONDS under
        this content revision; never fails or holds up play.

        The fact cites the stored observation of the same line, if any. A
        repeat is written at most once in TILE_FACT_REFRESH_SECONDS, and a
        failed write waits TILE_FACT_RETRY_SECONDS before the next.
        """
        if not square[0]:
            return
        now = time.time()
        self.tile_facts.setdefault(square, {})[value] = now
        character = getattr(self, "_memory_character", "")
        if not character or character != self.state.player_name:
            return
        written = self._tile_facts_noted.get((square, value))
        if ((written is not None and now - written < TILE_FACT_REFRESH_SECONDS) or
                time.monotonic() < self._tile_fact_retry_at):
            return
        fact = fact_parsers.tile_fact(*square, value, text)
        try:
            memory = self._memory_store()
            observation = memory.observation_id(
                character, f"system:{square[0]}", text_hash(text))
            _, created = memory.record_fact(
                character, fact.row(at=now, observation_id=observation,
                                    inputs=self.content_inputs,
                                    expires_at=now + fact_parsers.TILE_FACT_SECONDS))
        except Exception as error:
            log.warning("tile fact not kept: %s", error)
            self._tile_fact_retry_at = time.monotonic() + TILE_FACT_RETRY_SECONDS
            return
        self._tile_facts_noted[(square, value)] = now
        if created and self.metrics is not None:
            self.metrics.fact(fact.kind, fact.status)

    def _tile_decision(self, where: tuple[str, int, int], action: str, *,
                       once: bool = True) -> None:
        """Record a tile-fact decision; never fails or waits long."""
        if once:
            if (where, action) in self._tile_fact_decisions:
                return
            self._tile_fact_decisions.add((where, action))
        try:
            self.record_action(action, f"{where[0]} (whole map)" if where[1:] == (-1, -1)
                               else f"{where[0]} ({where[1]},{where[2]})",
                               save_timeout=fact_parsers.TILE_DECISION_SAVE_SECONDS)
        except Exception as error:
            log.warning("tile fact decision not recorded: %s", error)

    def _remembered_no_magic(self, square: tuple[str, int, int]
                             ) -> tuple[str, int, int] | None:
        """The square or map whose unexpired no-magic fact covers ``square``."""
        now = time.time()
        for key in (square, (square[0], -1, -1)):
            at = self.tile_facts.get(key, {}).get("no_magic")
            if at is not None and now - at < fact_parsers.TILE_FACT_SECONDS:
                return key
        return None

    def tile_fact_prefers_other_heal(self, square: tuple[str, int, int]) -> bool:
        """Whether a remembered no-magic fact should put a non-magical heal
        first on ``square`` (``SafetyPolicy._known_heal``). It only reorders:
        with no other heal she still casts. Off (``persist_tile_facts``
        unset), never; it records once that she would have preferred another
        heal here."""
        where = self._remembered_no_magic(square) if square[0] else None
        if where is None:
            return False
        if not self.config.persist_tile_facts:
            self._tile_decision(where, "tile-fact-no-magic-shadow")
            return False
        self._tile_decision(where, "tile-fact-no-magic")
        return True

    def _parse_facts(self, observation: Observation) -> list[fact_parsers.Fact]:
        """The facts a kept observation states (facts.py); never fails play."""
        content = self.authored_content
        try:
            return fact_parsers.parse(observation.row(),
                                      content.names if content is not None else None)
        except Exception:
            log.exception("fact parsers failed")
            return []

    def _route_events(self, name: str, call: Callable[[route_events.RouteRecorder], Any]
                      ) -> None:
        """Run one route event hook; it never fails or holds up play."""
        recorder = getattr(self, "route_recorder", None)
        if recorder is None:
            return
        try:
            call(recorder)
        except Exception:
            log.exception("route events: %s failed", name)

    def _memory_store(self) -> BotMemory:
        if self.memory is None:
            self.memory = BotMemory(
                self._memory_path, server=self._memory_server_key,
                account=self.config.account)
        return self.memory

    def operator_stopped(self) -> bool:
        """Whether the owner's last dashboard order was Stop, not autoplay.

        Persisted per server, account and the configured ``--character``
        (the one key known before login), so the ``autoplay`` subcommand
        does not resume after a crash or restart the owner's Stop. Without a
        configured character nothing is persisted and this is False.
        Storage errors propagate: the caller decides how to fail.
        """
        character = self.config.character
        if not character:
            return False
        value = self._memory_store().load(character, OPERATOR_STOP_KIND, {})
        return isinstance(value, dict) and value.get("stopped") is True

    def set_operator_stopped(self, stopped: bool) -> bool:
        """Persist the owner's Stop (True) or Start autoplay (False).

        Returns whether it was saved. A storage error is logged, never
        raised: the order itself still takes effect in this process.
        """
        character = self.config.character
        if not character:
            return False
        try:
            self._memory_store().save(
                character, OPERATOR_STOP_KIND,
                {"stopped": bool(stopped), "at": time.time()}, timeout=2.0)
        except (OSError, sqlite3.Error):
            log.exception("could not persist the operator %s",
                          "stop" if stopped else "autoplay start")
            return False
        return True

    def set_depletion_points(self, points: int) -> None:
        self.state.depletion_points = max(0, int(points))
        self.state.depletion_points_known = True
        # Death stat losses change depletion between stats packets.
        if self.trends.observe(self.state):
            self._persist_trends()
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "depletion_points"] = self.state.depletion_points
        self._persist_character_memory()

    def set_last_upgrade_shop_sweep(self, timestamp: float, *,
                                    level: int = 0,
                                    wallet: int = 0,
                                    policy: int = 0) -> None:
        self.state.last_upgrade_shop_sweep_at = max(0.0, float(timestamp))
        self.state.last_upgrade_shop_sweep_level = max(0, int(level))
        self.state.last_upgrade_shop_sweep_wallet = max(0, int(wallet))
        self.state.last_upgrade_shop_sweep_policy = max(0, int(policy))
        character = self.state.player_name or self.config.character
        if not character:
            return
        record = self._runtime_memory.setdefault(character, {})
        record["last_upgrade_shop_sweep_at"] = (
            self.state.last_upgrade_shop_sweep_at)
        record["last_upgrade_shop_sweep_level"] = (
            self.state.last_upgrade_shop_sweep_level)
        record["last_upgrade_shop_sweep_wallet"] = (
            self.state.last_upgrade_shop_sweep_wallet)
        record["last_upgrade_shop_sweep_policy"] = (
            self.state.last_upgrade_shop_sweep_policy)
        self._persist_character_memory()

    def set_active_upgrade_shop_sweep(self, cursor: int, *,
                                      policy: int) -> None:
        """Persist the next regional stock waypoint across restarts."""
        self.state.active_upgrade_shop_sweep_cursor = max(0, int(cursor))
        self.state.active_upgrade_shop_sweep_policy = max(0, int(policy))
        character = self.state.player_name or self.config.character
        if not character:
            return
        record = self._runtime_memory.setdefault(character, {})
        record["active_upgrade_shop_sweep_cursor"] = (
            self.state.active_upgrade_shop_sweep_cursor)
        record["active_upgrade_shop_sweep_policy"] = (
            self.state.active_upgrade_shop_sweep_policy)
        self._persist_character_memory()

    def set_last_recall_shop_check(self, timestamp: float) -> None:
        """Persist a dynamic recall-stock observation across restarts."""
        self.state.last_recall_shop_check_at = max(0.0, float(timestamp))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "last_recall_shop_check_at"] = self.state.last_recall_shop_check_at
        self._persist_character_memory()

    def set_recall_unavailable(self, detail: str) -> None:
        """Persist why replacing her recall source came to nothing ("" once
        an attempt starts or succeeds)."""
        self.state.recall_unavailable = str(detail or "")[:500]
        self.state.recall_unavailable_at = time.time() if detail else 0.0
        character = self.state.player_name or self.config.character
        if not character:
            return
        memory = self._runtime_memory.setdefault(character, {})
        memory["recall_unavailable"] = self.state.recall_unavailable
        memory["recall_unavailable_at"] = self.state.recall_unavailable_at
        self._persist_character_memory()

    def set_last_utility_shop_check(self, timestamp: float) -> None:
        """Persist a strategic random-stock observation across restarts."""
        self.state.last_utility_shop_check_at = max(0.0, float(timestamp))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "last_utility_shop_check_at"] = (
                self.state.last_utility_shop_check_at)
        self._persist_character_memory()

    def set_heal_restock_backoff(self, note: str, until: float) -> None:
        """Persist why the heal restock came to nothing and the wall-clock
        time before which it is not tried again ("" and 0 once it worked)."""
        self.state.heal_restock_note = str(note or "")[:500]
        self.state.heal_restock_retry_until = max(0.0, float(until))
        character = self.state.player_name or self.config.character
        if not character:
            return
        memory = self._runtime_memory.setdefault(character, {})
        memory["heal_restock_note"] = self.state.heal_restock_note
        memory["heal_restock_retry_until"] = (
            self.state.heal_restock_retry_until)
        self._persist_character_memory()

    def set_apartment_storage(self, rows: list[dict]) -> None:
        """Persist what her apartment chest holds (storage.py)."""
        self.state.apartment_storage = storage.load_index(rows)
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "apartment_storage"] = self.state.apartment_storage
        self._persist_character_memory()

    def defer_paid_restoration(self, until: float) -> None:
        """Persist the wall-clock time before which a paid temple service
        that did not work is not bought again, across restarts."""
        self.state.paid_restoration_retry_until = max(0.0, float(until))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "paid_restoration_retry_until"] = (
                self.state.paid_restoration_retry_until)
        self._persist_character_memory()

    def set_cure_funding_farm(self, *, since: float, deaths: int = 0,
                              counted_at: float = 0.0, funded_at: float = 0.0,
                              retry_until: float = 0.0) -> None:
        """Persist the cure-funding farm's state (navigation.
        cure_funding_farm_state) across restarts: its decision records
        alone would scroll out of the bounded decision history."""
        self.state.cure_funding_farm_since = max(0.0, float(since))
        self.state.cure_funding_farm_deaths = max(0, int(deaths))
        self.state.cure_funding_farm_counted_at = max(0.0, float(counted_at))
        self.state.cure_funding_farm_funded_at = max(0.0, float(funded_at))
        self.state.cure_funding_farm_retry_until = max(0.0, float(retry_until))
        character = self.state.player_name or self.config.character
        if not character:
            return
        memory = self._runtime_memory.setdefault(character, {})
        for key in ("cure_funding_farm_since", "cure_funding_farm_deaths",
                    "cure_funding_farm_counted_at", "cure_funding_farm_funded_at",
                    "cure_funding_farm_retry_until"):
            memory[key] = getattr(self.state, key)
        self._persist_character_memory()

    def set_mana_crystal_retry(self, detour: str, until: float) -> None:
        """Persist the wall-clock time before which the mana-crystal errand
        ``detour`` does not start again (AutoplayTask): the bounded decision
        history alone would scroll its failure out, and an in-process timer
        would not survive a restart. Elapsed deadlines are dropped."""
        now = time.time()
        backoffs = {key: value
                    for key, value in self.state.mana_crystal_retry_until.items()
                    if value > now}
        until = float(until)
        if math.isfinite(until) and until > now:
            backoffs[str(detour)] = until
        else:
            backoffs.pop(str(detour), None)
        self.state.mana_crystal_retry_until = backoffs
        character = self.state.player_name or self.config.character
        if not character:
            if not self._mana_crystal_unpersisted_warned:
                self._mana_crystal_unpersisted_warned = True
                log.warning("mana-crystal %s backoff kept in memory only: no "
                            "character to persist it for", detour)
            return
        self._runtime_memory.setdefault(character, {})[
            "mana_crystal_retry_until"] = dict(backoffs)
        self._persist_character_memory()

    def remember_route_hazard(self, path: str, until: float) -> None:
        """Persist a map navigation avoids until ``until`` (wall clock)."""
        now = time.time()
        self.state.route_hazards = {
            key: value for key, value in self.state.route_hazards.items()
            if value > now}
        self.state.route_hazards[path] = max(
            float(until), self.state.route_hazards.get(path, 0.0))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "route_hazards"] = dict(self.state.route_hazards)
        self._persist_character_memory()

    def record_vendor_quote(
            self, key: str, item: str, unit_cost: int) -> None:
        """Persist an exact dialogue quote in this character's namespace."""
        if not key or not item or unit_cost <= 0:
            return
        observation: dict[str, int | float | str] = {
            "item": item,
            "unit_cost": int(unit_cost),
            "observed_at": time.time(),
        }
        self.state.vendor_quotes[key] = observation
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "vendor_quotes"] = dict(self.state.vendor_quotes)
        self._persist_character_memory()

    def set_farm_adaptation(self, key: str, record: dict) -> None:
        """Persist what the character learned at one farm spot."""
        self.state.farm_adaptations[key] = dict(record)
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})["farm_adaptations"] = {
            spot: dict(value) for spot, value in self.state.farm_adaptations.items()}
        self._persist_character_memory()

    def set_farm_objective(self, objective: str) -> None:
        """Persist which farming-analyzer ranking autoplay follows."""
        if objective not in OBJECTIVES:
            raise ValueError(f"unknown farming objective {objective!r}")
        self.state.farm_objective = objective
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})["farm_objective"] = objective
        self._persist_character_memory()

    def set_build_choice(self, spot: str, choice: str) -> None:
        """Persist the operator's combat build for a farm spot (its path)
        or for every spot (builds.EVERY_SPOT); "auto" for a spot clears its
        own choice, so every spot's applies again."""
        if choice not in builds.CHOICES:
            raise ValueError(f"unknown build {choice!r}")
        spot = str(spot or builds.EVERY_SPOT)[:300]
        choices = dict(self.state.build_choices)
        choices.pop(spot, None)
        if choice != builds.AUTO or spot == builds.EVERY_SPOT:
            choices[spot] = choice
        # At most MAX_BUILD_CHOICES spots, the oldest choice going first.
        while len(choices) > builds.MAX_BUILD_CHOICES:
            oldest = next(key for key in choices if key != builds.EVERY_SPOT)
            choices.pop(oldest)
        self.state.build_choices = choices
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})["build_choices"] = dict(choices)
        self._persist_character_memory()

    def set_spell_samples(self, samples: dict) -> None:
        """Persist her observed hits per damage spell (bounded)."""
        self.state.spell_samples = builds.load_spell_samples(samples)
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})["spell_samples"] = dict(
            self.state.spell_samples)
        self._persist_character_memory()

    def set_build_state(self, **changes) -> None:
        """Persist the build she fields, since when, and a pending swap,
        so the hold and the swap survive a restart."""
        state = dict(self.state.build_state)
        state.update(changes)
        self.state.build_state = builds.load_build_state(state)
        for name, value in changes.items():
            setattr(self, name, value)
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})["build_state"] = dict(
            self.state.build_state)
        self._persist_character_memory()

    def set_apartment_bed_bound(self, bound: bool = True) -> None:
        self.state.apartment_bed_bound = bool(bound)
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "apartment_bed_bound"] = self.state.apartment_bed_bound
        self._persist_character_memory()

    def set_savebed(self, path: str, x: int, y: int) -> None:
        """Persist where the server now respawns the character."""
        self.state.savebed_map = path
        self.state.savebed_xy = (int(x), int(y))
        self.state.apartment_bed_bound = path == STARTER_APARTMENT
        character = self.state.player_name or self.config.character
        if not character:
            return
        record = self._runtime_memory.setdefault(character, {})
        record["savebed"] = [path, int(x), int(y)]
        record["apartment_bed_bound"] = self.state.apartment_bed_bound
        self._persist_character_memory()

    def _observe_map_change(self, old_path: str, m: MapState) -> None:
        """Learn where leaving an apartment put the character.

        The server returns to the per-player entrance recorded at runtime, so
        the authored graph's static apartment exits are only placeholders.
        """
        if m.path and old_path != m.path:
            self.map_arrivals.append((time.time(), m.path))
            del self.map_arrivals[:-100]
        if (old_path and old_path != m.path and "/apartments/" in old_path and
                "/apartments/" not in m.path and not self._recently_moved_by_force()):
            self.set_apartment_return(old_path, m.path, m.world_x, m.world_y)

    def _recently_moved_by_force(self, seconds: float = 15.0) -> bool:
        """Whether a death or recall, not an exit, may explain a map change."""
        cutoff = time.time() - seconds
        for entry in reversed(self.decision_history):
            if not isinstance(entry, dict):
                continue
            if float(entry.get("time", 0) or 0) < cutoff:
                break
            if entry.get("action") in ("death", "emergency-recall"):
                return True
        return False

    def set_apartment_return(self, apartment: str, path: str, x: int, y: int) -> None:
        """Persist where leaving an apartment put the character."""
        back = (path, (int(x), int(y)))
        if self.state.apartment_returns.get(apartment) == back:
            return
        self.state.apartment_returns[apartment] = back
        character = self.state.player_name or self.config.character
        if not character:
            return
        record = self._runtime_memory.setdefault(character, {})
        record["apartment_returns"] = {
            name: [where, point[0], point[1]]
            for name, (where, point) in self.state.apartment_returns.items()}
        self._persist_character_memory()

    def quarantine_farm_zone(self, path: str, until: float) -> None:
        """Persist a dangerous autoplay tier across reconnects/restarts."""
        if not path:
            return
        now = time.time()
        self.state.farm_zone_quarantine = {
            zone: expiry
            for zone, expiry in self.state.farm_zone_quarantine.items()
            if expiry > now
        }
        # A quarantine is never shortened.
        self.state.farm_zone_quarantine[path] = max(
            now, float(until),
            float(self.state.farm_zone_quarantine.get(path, 0.0) or 0.0))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "farm_zone_quarantine"] = dict(
                self.state.farm_zone_quarantine)
        self._persist_character_memory()

    def note_farm_train_hold(self, path: str, at: float) -> None:
        """Persist a train-held approach to farm map ``path`` at ``at``,
        keeping the last day's (FarmCircuitTask._leave_train_held_spot)."""
        at = float(at)
        if not path or not math.isfinite(at) or at > time.time() + 1.0:
            # A non-finite or future time would hold the count up for ever.
            return
        holds = [held for held in self.state.farm_train_holds.get(path, [])
                 if math.isfinite(held) and 0.0 <= at - held < 86400.0]
        holds.append(float(at))
        self.state.farm_train_holds[path] = holds
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "farm_train_holds"] = {
                key: list(value)
                for key, value in self.state.farm_train_holds.items()}
        self._persist_character_memory()

    def set_farm_zone_last_checked(self, path: str,
                                   checked_at: float) -> None:
        """Persist when an expensive optional farm detour was last sampled."""
        if not path:
            return
        self.state.farm_zone_last_checked[path] = max(
            0.0, float(checked_at))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "farm_zone_last_checked"] = dict(
                self.state.farm_zone_last_checked)
        self._persist_character_memory()

    def _lore_attempt_signature(self, item: Item) -> str:
        literacy = next((
            int(skill.extra.get("level", 0) or 0)
            for skill in self.state.inventory
            if skill.item_type == c.TYPE_SKILL and
            skill.name.casefold() == "literacy"), 0)
        return json.dumps(
            [literacy, item.face, item.item_type, item.name.casefold()],
            ensure_ascii=False, separators=(",", ":"))

    def lore_book_attempted(self, item: Item) -> bool:
        """Whether this occurrence was tried at the current Literacy level."""
        signature = self._lore_attempt_signature(item)
        occurrence = 0
        for candidate in self.state.inventory:
            if self._lore_attempt_signature(candidate) == signature:
                occurrence += 1
            if candidate is item or candidate.tag == item.tag:
                break
        return self.state.lore_attempt_counts.get(signature, 0) >= occurrence

    def mark_lore_book_attempted(self, item: Item) -> None:
        """Persist an occurrence count without relying on reconnect tags."""
        signature = self._lore_attempt_signature(item)
        occurrence = 0
        for candidate in self.state.inventory:
            if self._lore_attempt_signature(candidate) == signature:
                occurrence += 1
            if candidate is item or candidate.tag == item.tag:
                break
        self.state.lore_attempt_counts[signature] = max(
            occurrence, self.state.lore_attempt_counts.get(signature, 0))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "lore_attempt_counts"] = dict(self.state.lore_attempt_counts)
        self._persist_character_memory()

    def set_bank_balance(self, balance: int,
                         observed_at: float | None = None) -> None:
        """Persist a recently server-confirmed balance across short restarts."""
        self.state.bank_balance = max(0, int(balance))
        self.state.bank_balance_known = True
        # Never in the future: a stamp from a clock that was ahead would
        # otherwise look fresh until the clock caught up.
        now = time.time()
        self.state.bank_balance_observed_at = min(now, max(
            0.0, float(now if observed_at is None else observed_at)))
        character = self.state.player_name or self.config.character
        if not character:
            return
        record = self._runtime_memory.setdefault(character, {})
        record["bank_balance"] = self.state.bank_balance
        record["bank_balance_observed_at"] = (
            self.state.bank_balance_observed_at)
        self._persist_character_memory()

    def forget_bank_balance(self) -> None:
        """Drop a cached balance that may no longer match the server.

        An unknown balance reads as 0, exactly as after a restart without a
        fresh observation, so no wallet check counts the stale amount; the
        budgeted flows read the bank again before relying on it.
        """
        self.state.bank_balance = 0
        self.state.bank_balance_known = False
        self.state.bank_balance_observed_at = 0.0
        character = self.state.player_name or self.config.character
        if not character:
            return
        record = self._runtime_memory.setdefault(character, {})
        record["bank_balance"] = 0
        record["bank_balance_observed_at"] = 0.0
        self._persist_character_memory()

    def set_last_bank_deposit(self, timestamp: float) -> None:
        """Persist the farm-circuit deposit cooldown across restarts."""
        self.state.last_bank_deposit_at = max(0.0, float(timestamp))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "last_bank_deposit_at"] = self.state.last_bank_deposit_at
        self._persist_character_memory()

    def record_action(self, action: str, detail: str = "", *,
                      save_timeout: float | None = None) -> None:
        """Keep a bounded, map-aware history of orders sent by automation.

        With ``save_timeout``, saving the ledger waits at most that long and
        a failed save is logged, not raised."""
        if self.metrics is not None:
            self.metrics.action(action, detail)
        m = self.state.map
        record = {
            "time": time.time(), "action": action, "detail": detail,
            "task": self.action_context, "map": m.path,
            "x": m.world_x, "y": m.world_y,
        }
        self.action_history.append(record)
        del self.action_history[:-500]
        fights = getattr(self, "fight_recorder", None)
        if fights is not None:
            fights.note(action, record["time"])
        self._route_events("decision", lambda recorder: recorder.on_decision(self, record))
        noisy = {
            "step", "clear", "target", "combat", "fire", "pathfind",
            "apply", "move item", "approach-step", "melee-dodge",
            "melee-position",
        }
        if action not in noisy:
            previous = (self.decision_history[-1]
                        if self.decision_history else None)
            # Each death keeps its own record and time, so alerts can age
            # deaths out of their window one by one.
            repeated = bool(
                action != "death" and
                previous and record["time"] - previous.get("time", 0) <= 5.0
                and all(previous.get(key) == record[key]
                        for key in ("action", "detail", "task", "map")))
            if repeated:
                previous.update(
                    time=record["time"], x=record["x"], y=record["y"])
                previous["count"] = int(previous.get("count", 1)) + 1
                if previous["count"] % 5:
                    return
            else:
                # A copy: coalescing updates the decision in place, which must
                # not move the action history entry it came from.
                self.decision_history.append(dict(record))
                del self.decision_history[:-2000]
            # Before the player packet arrives, config.character names the
            # intended selection but its persisted history has not been
            # loaded yet. Saving the new process's short pre-login list under
            # that key would overwrite the existing SQLite decision ledger.
            character = self.state.player_name
            if character and save_timeout is not None:
                try:
                    self._memory_store().save(character, "decisions",
                                              self.decision_history, timeout=save_timeout)
                except sqlite3.Error as error:
                    log.warning("decision not saved: %s", error)
            elif character:
                self._memory_store().save(
                    character, "decisions", self.decision_history)

    def _latest_ledger_death(self) -> float:
        """The persisted ledger's latest death (0 if none), overflow-safe."""
        for record in reversed(self.decision_history[-200:]):
            if isinstance(record, dict) and record.get("action") == "death" \
                    and post_mortem.finite(record.get("time")):
                return float(record["time"])
        return 0.0

    def _record_post_mortem(self, when: float | None = None,
                            ledger_death: float | None = None) -> None:
        """Keep what happened in the minute before this death.

        kill_player queues its notice before the savebed transfer's map update,
        which goes out at the end of the server tick, so the square she is on
        now is where she fell. The report is built once, here, and ends at the
        notice: what follows a death (a respawn, the next fight) never belongs
        to it. `when` is the notice's arrival, taken before any bookkeeping
        (whose saves can wait on a locked database) can delay it.
        """
        # The previous life ends with its death's bookkeeping, which comes
        # after its notice; a restored report has only its notice time.
        notice = time.time() if when is None else when
        # Without a report or a booking for it (an upgrade, a discarded store),
        # the persisted ledger's latest earlier death still ends that life.
        # The handler reads it before this death's own record is written, so
        # it is never this death, whatever the wall clock does.
        if ledger_death is None:
            ledger_death = self._latest_ledger_death()
        previous = max(self._post_mortem_booked, ledger_death,
                       self.post_mortems[-1]["time"] if self.post_mortems else 0.0)
        m = self.state.map
        # Lives are separated by position as well as by time: only what was
        # recorded since the previous report counts, so no wall-clock step,
        # backward or forward, lets the last life into this one.
        self.post_mortems.append(post_mortem.build(
            notice, m.path, m.world_x, m.world_y, self.vitals,
            list(self.review_messages),
            _after(self.action_history, self._life_marks.get("actions")),
            _after(self.decision_history, self._life_marks.get("decisions")),
            since=previous, name=self.state.player_name or self.config.character,
            recorded=time.time()))
        if self.metrics is not None and self.post_mortems[-1] is not None:
            self.metrics.death(self.post_mortems[-1].get("cause"))
        # A death in transit: written at once, waiting at most the
        # post-mortem's own limit for a locked database.
        report = self.post_mortems[-1]
        self._route_events("death", lambda recorder: recorder.on_post_mortem(self, report))
        self._post_mortem_booked = time.time()
        # The next life starts here: its messages afresh, its vitals from the
        # latest sample (the state it starts in), its records after these.
        self.review_messages.clear()
        while len(self.vitals) > 1:
            self.vitals.popleft()
        self._life_marks = {"actions": self.action_history[-1] if self.action_history else None,
                            "decisions": self.decision_history[-1] if self.decision_history else None}
        del self.post_mortems[:-post_mortem.KEEP]
        # Best effort, like the trends: a full or read-only disk, or a locked
        # database, must never stop or stall play for dashboard telemetry. The
        # save waits at most a quarter second for a lock (not SQLite's usual
        # ten) and is otherwise skipped; the report stays in memory, and the
        # next death's save carries it.
        character = self.state.player_name
        if character:
            self._save_post_mortems(character, self.post_mortems)

    def _save_post_mortems(self, character: str, reports: list[dict]) -> None:
        try:
            self._memory_store().save(character, "post_mortems", reports,
                                      timeout=POST_MORTEM_SAVE_TIMEOUT)
        except Exception as error:
            log.warning("could not save post-mortems for %s: %s", character, error)

    def _trend_count(self, key: str) -> None:
        if self.trends.count(key):
            self._persist_trends()

    def _persist_trends(self) -> None:
        """Save the trend buckets once a minute, when a new one starts.

        Best effort: a full or read-only disk, or a locked database, must
        never stop play or abort a clean shutdown for dashboard telemetry.
        """
        character = self.state.player_name
        if not character:
            return
        try:
            self._memory_store().save(character, "trends", self.trends.buckets)
        except Exception as error:
            log.warning("could not save trends for %s: %s", character, error)

    def _load_faces(self) -> dict[int, str]:
        faces: dict[int, str] = {}
        paths = (
            self._runtime_content_root / "lib" / "bmaps",
            self._runtime_content_root / "server" / "data" / "bmaps",
        )
        for path in paths:
            try:
                for index, line in enumerate(
                        path.read_text(errors="replace").splitlines()):
                    parts = line.split()
                    if parts:
                        faces[index] = parts[-1]
                return faces
            except OSError:
                continue
        log.warning("could not load face metadata from %s", paths)
        return faces

    def _load_animations(self) -> dict[int, str]:
        paths = (self._runtime_content_root / "lib" / "animations",
                 self._runtime_content_root / "arch" / "animations")
        for path in paths:
            try:
                names = [line[5:].strip()
                         for line in path.read_text(errors="replace").splitlines()
                         if line.startswith("anim ")]
            except OSError:
                continue
            # Server animation IDs start at one; slot zero is ###none.
            return {index: name for index, name in enumerate(names, 1)}
        log.warning("could not load animation metadata from %s", paths)
        return {}

    def add_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def _reset_target_identity(self) -> None:
        # A new connection starts every acknowledgement window afresh.
        self.state.target_request = (0, 0.0, 0.0)
        self.state.target_ack = (0, 0.0)
        self.state.target_code = c.TARGET_SELF
        self.state.target_level = 0
        self.state.target_actor_count = 0
        self.state.target_relative_depth = 0
        self.state.target_id = 0
        self.state.target_color = ""
        self.state.target_name = ""
        self.state.combat = False
        self.state.combat_force = False

    async def emit(self, kind: str, data=None) -> None:
        event = Event(kind, data)
        await self.events.put(event)
        for handler in tuple(self._handlers):
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result

    async def connect(self) -> None:
        if not self.config.account or not self.config.password:
            raise ValueError("account and password are required")
        if self.config.access_code:
            self.config.access_code = normalize_access_code(self.config.access_code)
        # Reset map-derived action authority before any reconnect attempt,
        # including a rejected transport configuration.
        self.state.phase = "connecting"
        self._reset_target_identity()
        self._map_pending = None
        self.state.map = MapState(width=self.config.map_width,
                                  height=self.config.map_height)
        if self.config.transport not in ("auto", "quic"):
            raise ValueError("Classic protocol requires encrypted QUIC")
        if not self.config.certificate_sha256:
            raise ValueError("QUIC requires a pinned certificate SHA-256")
        self._quic_stream = await QuicStream.connect(
            self.config.host, self.config.quic_port,
            self.config.certificate_sha256, self.config.connect_timeout,
        )
        self.reader = self._quic_stream.reader
        self.writer = self._quic_stream.writer
        self.transport = "quic"
        self.state.phase = "version"
        self._setup_sent = self._account_sent = self._character_sent = False
        self._access_accepted = False
        await self.send(Packet(c.S_VERSION).add("I", c.SOCKET_VERSION))
        await self.emit("connected", (self.config.host, self.config.quic_port,
                                      self.transport))

    async def _close_transport(self) -> None:
        self._map_pending = None
        writer = self.writer
        quic_stream = self._quic_stream
        self.reader = self.writer = None
        self._quic_stream = None
        self.transport = ""
        if quic_stream is not None:
            await quic_stream.close()
        elif writer is not None:
            writer.close()
            await writer.wait_closed()

    async def close(self) -> None:
        # The current minute is otherwise saved only when the next one starts.
        self._persist_trends()
        # A report whose save failed lives only in memory: one more try, for
        # this character and any other this client played.
        current = self._memory_character or self.state.player_name
        if current and self.post_mortems:
            self._save_post_mortems(current, self.post_mortems)
        for character, reports in self._live_post_mortems.items():
            if reports:
                self._save_post_mortems(character, reports)
        route_shadow.close(self)
        self._closed = True
        await self._close_transport()
        self.state.phase = "disconnected"
        self.progress_watch.observe(self.state)

    async def checkpoint_reconnect(self) -> bool:
        """Gracefully reconnect so the server persists the live character."""
        if (self.reader is None or self.writer is None or
                self.state.phase != "playing"):
            return False
        # Closing the TCP/QUIC transport makes the server run the ordinary
        # logout/save path. The read loop observes EOF (or its detached reader)
        # and proceeds through the configured reconnect delay.
        self._reconnect_requested = True
        await self._close_transport()
        self.state.phase = "disconnected"
        self.progress_watch.observe(self.state)
        return True

    async def run(self) -> None:
        while not self._closed:
            try:
                await self.connect()
                keepalive = asyncio.create_task(self._keepalive_loop())
                try:
                    await self._read_loop()
                finally:
                    keepalive.cancel()
                    # Its outcome never replaces the read loop's: a failure
                    # of its own is only logged.
                    outcome, = await asyncio.gather(
                        keepalive, return_exceptions=True)
                    if (isinstance(outcome, BaseException) and
                            not isinstance(outcome, asyncio.CancelledError)):
                        log.warning("keepalive task failed: %r", outcome)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, asyncio.IncompleteReadError, OSError, ProtocolError) as exc:
                if not self._closed:
                    if self._reconnect_requested:
                        log.info("progress checkpoint: reconnecting cleanly")
                    else:
                        log.warning("connection ended: %s", exc)
                    await self.emit("disconnected", str(exc))
            finally:
                try:
                    await self._close_transport()
                except OSError:
                    pass
                self.state.phase = "disconnected"
                self.progress_watch.observe(self.state)
            if not self.config.reconnect or self._closed:
                break
            self._reconnect_requested = False
            await asyncio.sleep(self.config.reconnect_delay)

    # While she plays, a keepalive every this many seconds (a random point
    # in the range): the server drops a playing connection that sends
    # nothing for SOCKET_LOGIN_IDLE_TIMEOUT (10 minutes; server
    # socket/server.c), and every keepalive resets that idle counter
    # (socket_command_keepalive, socket/request.c). Live, before
    # 2026-11-26, she never sent one and was dropped 83 times a day.
    KEEPALIVE_SECONDS = (30.0, 60.0)
    # Unanswered keepalives remembered for their round trip, at most.
    KEEPALIVE_PENDING = 8

    async def _keepalive_loop(self) -> None:
        """Send a keepalive every KEEPALIVE_SECONDS while she plays, for one
        connection: run() starts it after connecting and cancels it when
        the read loop ends."""
        self._keepalive_sent.clear()
        while not self._closed:
            await asyncio.sleep(random.uniform(*self.KEEPALIVE_SECONDS))
            if self.state.phase != "playing" or self.writer is None:
                continue
            try:
                await self.send_keepalive()
            except ConnectionError:
                # The read loop sees the same failure and reconnects.
                return

    async def send_keepalive(self) -> None:
        """One keepalive with a fresh id; its echo (_handle_keepalive)
        gives the round trip."""
        self._keepalive_id = (self._keepalive_id + 1) & 0xFFFFFFFF
        ident = self._keepalive_id
        self._keepalive_sent[ident] = time.monotonic()
        while len(self._keepalive_sent) > self.KEEPALIVE_PENDING:
            self._keepalive_sent.pop(next(iter(self._keepalive_sent)))
        await self.send(Packet(c.S_KEEPALIVE).u32(ident))

    async def _read_loop(self) -> None:
        while not self._closed:
            reader = self.reader
            if reader is None:
                raise ConnectionError("transport closed for reconnect")
            frame = await read_frame(reader)
            frame = decompress_frame(frame, c.C_COMPRESSED)
            self.state.last_packet_at = time.time()
            packet_type, payload = frame[0], frame[1:]
            try:
                await self._dispatch(packet_type, payload)
            except ProtocolError:
                log.exception("failed decoding packet type %d (%d bytes)", packet_type, len(payload))
                raise

    # A drain still pending after this long (or connect_timeout) is a stall.
    SEND_STALL_SECONDS = 5.0

    async def send(self, packet: Packet) -> None:
        if self.writer is None:
            raise ConnectionError("not connected")
        writer = self.writer
        try:
            writer.write(packet.encode())
            # asyncio.wait reports an expiry without raising, so a transport
            # TimeoutError (ETIMEDOUT, an OSError) is never mistaken for it.
            drain = asyncio.ensure_future(writer.drain())
            done, _ = await asyncio.wait(
                {drain}, timeout=max(self.SEND_STALL_SECONDS,
                                     self.config.connect_timeout))
            if done:
                drain.result()
        except OSError as exc:
            # Any transport failure (ENETUNREACH, EPIPE, a reset, ETIMEDOUT,
            # ...) ends the connection just like run() treats it. Detach the
            # transport, as for a stalled drain, so the read loop wakes and
            # reconnects, and report it as a lost connection.
            log.warning("transport send failed (%s); forcing reconnect", exc)
            await self._detach_failed_transport(writer, f"send failed: {exc}")
            if isinstance(exc, ConnectionError):
                raise
            raise ConnectionError(str(exc) or type(exc).__name__) from exc
        if not done:
            drain.cancel()
            # A QUIC stream can remain nominally connected while flow-control
            # drain never completes.  That otherwise freezes the task engine
            # forever on one recorded action. Detaching the transport wakes
            # the read loop and preserves the in-memory autoplay task across
            # its normal reconnect.
            log.warning("transport send stalled; forcing reconnect")
            await self._detach_failed_transport(
                writer, "send drain exceeded timeout")

    async def _detach_failed_transport(self, writer: Any, reason: str) -> None:
        """Tear down the transport a send failed on, unless already replaced.

        A send can fail late, after the read loop noticed the same outage and
        reconnected; that fresh transport must not be closed for it.
        """
        if self.writer is not writer:
            return
        self.record_action("transport-reconnect", reason)
        self._reconnect_requested = True
        with contextlib.suppress(OSError):
            await self._close_transport()
        self.state.phase = "disconnected"
        self.progress_watch.observe(self.state)

    async def _dispatch(self, packet_type: int, data: bytes) -> None:
        if (self.state.phase in ("version", "access-policy", "access-auth")
                and packet_type not in (c.C_VERSION, c.C_ACCESS_POLICY,
                                        c.C_ACCESS_RESULT, c.C_KEEPALIVE)):
            raise ProtocolError("application packet before access admission")
        handlers = {
            c.C_VERSION: self._handle_version,
            c.C_ACCESS_POLICY: self._handle_access_policy,
            c.C_ACCESS_RESULT: self._handle_access_result,
            c.C_SETUP: self._handle_setup,
            c.C_CHARACTERS: self._handle_characters,
            c.C_PLAYER: self._handle_player,
            c.C_DRAWINFO: self._handle_drawinfo,
            c.C_STATS: self._handle_stats,
            c.C_TARGET: self._handle_target,
            c.C_ITEM: self._handle_item,
            c.C_ITEM_UPDATE: self._handle_item_update,
            c.C_ITEM_DELETE: self._handle_item_delete,
            c.C_MAP: self._handle_map,
            c.C_MAPSTATS: self._handle_mapstats,
            c.C_INTERFACE: self._handle_interface,
            c.C_BOOK: self._handle_book,
            c.C_PARTY: self._handle_party,
            c.C_KEEPALIVE: self._handle_keepalive,
        }
        handler = handlers.get(packet_type)
        if handler is None:
            await self.emit("packet_ignored", (packet_type, len(data)))
            return
        await handler(Cursor(data), data)

    async def _handle_version(self, cur: Cursor, raw: bytes) -> None:
        if self.state.phase != "version" or len(raw) != 4:
            raise ProtocolError("unexpected protocol version frame")
        self.state.server_version = cur.u32()
        if self.state.server_version != c.SOCKET_VERSION:
            raise ProtocolError("incompatible server protocol version")
        self.state.phase = "access-policy"

    async def _handle_access_policy(self, cur: Cursor, raw: bytes) -> None:
        if (self.state.phase != "access-policy" or self.transport != "quic"
                or len(raw) != 2 or raw[0] != 1 or raw[1] not in (0, 1)):
            raise ProtocolError("invalid access policy")
        if raw[1] == 0:
            self._access_accepted = True
            await self._send_setup()
            return
        if not self.config.access_code:
            raise ProtocolError("access unavailable")
        code = normalize_access_code(self.config.access_code).encode("ascii")
        packet = Packet(c.S_ACCESS_AUTH).u8(1)
        packet.data.extend(code)
        self.state.phase = "access-auth"
        await self.send(packet)

    async def _handle_access_result(self, cur: Cursor, raw: bytes) -> None:
        if (self.state.phase != "access-auth" or self.transport != "quic"
                or len(raw) != 2 or raw[0] != 1 or raw[1] not in (0, 1)):
            raise ProtocolError("invalid access result")
        if raw[1] != 0:
            raise ProtocolError("access unavailable")
        self._access_accepted = True
        await self._send_setup()

    async def _send_setup(self) -> None:
        packet = (Packet(c.S_SETUP)
                  .u8(c.SETUP_SOUND).u8(0)
                  .u8(c.SETUP_MAPSIZE).u8(self.config.map_width).u8(self.config.map_height)
                  .u8(c.SETUP_DATA_URL).string(""))
        self._setup_sent = True
        self.state.phase = "setup"
        await self.send(packet)

    async def _handle_setup(self, cur: Cursor, raw: bytes) -> None:
        if not self._access_accepted or not self._setup_sent or self.state.phase != "setup":
            raise ProtocolError("setup before access admission")
        while cur.remaining:
            setup_type = cur.u8()
            if setup_type == c.SETUP_SOUND:
                cur.u8()
            elif setup_type == c.SETUP_MAPSIZE:
                self.state.map.width, self.state.map.height = cur.u8(), cur.u8()
            elif setup_type == c.SETUP_DATA_URL:
                cur.cstring()
            else:
                raise ProtocolError(f"unknown setup field {setup_type}")
        if not self._account_sent:
            packet = Packet(c.S_ACCOUNT)
            if self.config.register:
                packet.u8(c.ACCOUNT_REGISTER).string(self.config.account)
                packet.string(self.config.password).string(self.config.password)
            else:
                packet.u8(c.ACCOUNT_LOGIN).string(self.config.account).string(self.config.password)
            await self.send(packet)
            self._account_sent = True
            self.state.phase = "account"

    async def _handle_characters(self, cur: Cursor, raw: bytes) -> None:
        if not raw:
            self.state.characters = []
            await self.emit("login_failed", None)
            return
        self.state.account = cur.cstring()
        connection_id = cur.cstring()
        previous_connection_id = cur.cstring()
        previous_connection_time = cur.u64()
        if (len(connection_id) != 32 or
                any(ch not in "0123456789abcdef" for ch in connection_id) or
                (previous_connection_id and
                 (len(previous_connection_id) != 32 or
                  any(ch not in "0123456789abcdef"
                      for ch in previous_connection_id)))):
            raise ProtocolError("invalid connection ID in characters packet")
        self.state.connection_id = connection_id
        self.state.previous_connection_id = previous_connection_id
        self.state.previous_connection_time = previous_connection_time
        characters: list[Character] = []
        while cur.remaining:
            characters.append(Character(
                cur.cstring(), cur.cstring(), cur.cstring(), cur.u16(), cur.u8()
            ))
        self.state.characters = characters
        await self.emit("characters", characters)

        selected = None
        if self.config.character:
            selected = next((ch for ch in characters
                             if ch.name.casefold() == self.config.character.casefold()), None)
        elif len(characters) == 1:
            selected = characters[0]

        if selected is not None and not self._character_sent:
            await self.select_character(selected.name)
        elif not characters and self.config.create_character and not self._character_sent:
            await self.create_character(
                self.config.character, self.config.character_archetype
            )
        else:
            self.state.phase = "characters"

    async def _handle_player(self, cur: Cursor, raw: bytes) -> None:
        previous_tag = self.state.player_tag
        self.state.player_tag = cur.u32()
        # A new login: the server closed every open container at logout
        # (player.c container_close), but that close never reached this
        # connection. Forget their listed contents, or the login's ground
        # redraw reattaches them to the same chest tag.
        for tag in [tag for tag in self.state.inventories
                    if tag not in (0, previous_tag, self.state.player_tag)]:
            for child in self.state.inventories.pop(tag, []):
                self.state.remove_item(child)
        weight, face = cur.u32(), cur.u32()
        self.state.player_name = cur.cstring()
        self.state.party_name = ""
        self.state.operator_vitals = None
        self.state.party_member_digests = set()
        # PLAYER arrives before the full ITEM inventory packet. Preserve this
        # explicit barrier across reconnects even though the prior inventory
        # objects remain in memory for those few event-loop turns.
        self.state.inventory_replay_complete = False
        self.load_character_memory(self.state.player_name)
        # A heal's wait (tasks.heal_waiting) never survives a new login.
        self.heal_combat_force = None
        self.heal_waiting_until = 0.0
        self.state.phase = "playing"
        self.state.quests_loaded = False
        self.state.inventories.setdefault(self.state.player_tag, [])
        await self.emit("playing", {
            "name": self.state.player_name, "tag": self.state.player_tag,
            "weight": weight / 1000.0, "face": self.faces.get(face, str(face)),
        })
        await self.request_quests()
        await self._ensure_configured_party()

    async def _ensure_configured_party(self) -> None:
        """Form the configured open party, or rejoin it after reconnect."""
        name = self.config.party_name.strip()
        if not name:
            return
        if any(ord(char) < 32 for char in name):
            log.error("configured party name contains control characters")
            return
        self._party_setup_name = name
        self._party_setup_pending = "form"
        await self.execute_client_command(f"/party form {name}")

    async def _handle_party(self, cur: Cursor, raw: bytes) -> None:
        """Track the legacy CLIENT_CMD_PARTY membership contract."""
        command = cur.u8()
        if command == c.CMD_PARTY_JOIN:
            self.state.party_name = cur.cstring()
            # A new party: its members' vitals and names arrive afresh.
            self.state.operator_vitals = None
            self.state.party_member_digests = set()
            self._party_setup_pending = ""
            self.record_action("party-ready", self.state.party_name)
            await self.emit("party", {
                "name": self.state.party_name, "joined": True})
        elif command == c.CMD_PARTY_LEAVE:
            old_name = self.state.party_name
            self.state.party_name = ""
            self.state.operator_vitals = None
            self.state.party_member_digests = set()
            await self.emit("party", {"name": old_name, "joined": False})
        elif command == c.CMD_PARTY_PASSWORD:
            # Preserve defensive framing validation even though the bot does
            # not interactively enter passwords. Configured parties are open.
            await self.emit("party_password", cur.cstring())
        elif command in (c.CMD_PARTY_UPDATE, c.CMD_PARTY_WHO):
            size = cur.remaining
            members = party_vitals(cur, repeated=command == c.CMD_PARTY_WHO)
            self._note_operator_vitals(members, roster=command == c.CMD_PARTY_WHO)
            await self.emit("party_packet", (command, size))
        elif command == c.CMD_PARTY_REMOVE_MEMBER:
            size = cur.remaining
            name = cur.cstring()
            if cur.remaining:
                raise ProtocolError("party member removal trailing bytes")
            if name == self.config.operator_character.strip():
                self.state.operator_vitals = None
            self.state.party_member_digests.discard(name_digest(name))
            await self.emit("party_packet", (command, size))
        else:
            # The LIST payload belongs to the graphical party list. The bot
            # needs only membership and the operator character's vitals.
            await self.emit("party_packet", (command, cur.remaining))

    def _note_operator_vitals(self, members: list[PartyVitals], *, roster: bool) -> None:
        """Keep the configured operator character's vitals, and only theirs:
        every other member's vitals are dropped here, and their names kept
        only as in-memory digests (``owner_watch.name_digest``), so the
        owner watch can tell a party member from a monster of the same
        name. A roster (``CMD_PARTY_WHO``) replaces both; one without the
        operator character clears its vitals."""
        digests = {name_digest(member.name) for member in members}
        if roster:
            self.state.party_member_digests = digests
        else:
            self.state.party_member_digests |= digests
        operator = self.config.operator_character.strip()
        if not operator or operator == self.state.player_name:
            self.state.operator_vitals = None
            return
        for member in members:
            if member.name == operator:
                self.state.operator_vitals = (member.hp, member.sp, time.time())
                return
        if roster:
            self.state.operator_vitals = None

    async def _handle_drawinfo(self, cur: Cursor, raw: bytes) -> None:
        msg_type, color = cur.u8(), cur.cstring()
        text = cur.cstring() if cur.remaining else ""
        self.state.add_message(msg_type, color, text)
        self._note_tile_refusal_line(text)
        # Only what a review reads (time, type, text), bounded in size as well
        # as count: a report's own text length of each line, and no other
        # server field (a frame can be megabytes).
        stamp, msg_type, *_, line = self.state.messages[-1]
        self.review_messages.append((stamp, msg_type, str(line)[:post_mortem.MAX_TEXT]))
        arrived = time.time()
        cutoff = self.state.messages[-1][0] - post_mortem.REVIEW_HISTORY_SECONDS
        while self.review_messages and self.review_messages[0][0] < cutoff:
            self.review_messages.popleft()
        self._observe(lambda recorder: recorder.on_message(
            self.state, msg_type, color, text))
        self._route_events("message", lambda recorder: recorder.on_message(
            self, msg_type, text))
        # A player can type "your balance is ..." in chat; only game and NPC
        # messages are the bank's (the banker itself answers in a dialogue).
        if (msg_type not in c.PLAYER_CHAT_TYPES and
                update_bank_balance(self.state, text)):
            self.set_bank_balance(self.state.bank_balance)
        lowered = text.casefold()
        normalized = lowered.strip()
        if self._party_setup_pending == "form" and normalized == (
                f"the party {self._party_setup_name.casefold()} already "
                "exists, pick another name."):
            # An open party can survive Sera's reconnect while another player
            # remains in it. Rejoin that party instead of choosing a new name.
            self._party_setup_pending = "join"
            await self.execute_client_command(
                f"/party join {self._party_setup_name}")
        elif normalized == (
                f"you have formed party: "
                f"{self._party_setup_name.casefold()}"):
            # The JOIN packet is authoritative; this fallback also makes the
            # state useful against a server that coalesces the GUI packet.
            self.state.party_name = self._party_setup_name
            self._party_setup_pending = ""
        now = time.monotonic()
        death_losses = {
            "you feel weaker!", "you feel clumsy!",
            "you feel less healthy!", "you feel stupid!",
            "you feel less potent!",
        }
        direct_drains = (
            "oh no! you are weakened!", "you're feeling clumsy!",
            "you feel less healthy", "watch out, your mind is going!",
            "your spirit feels drained!",
        )
        if lowered.strip() == "you save and your save bed location is updated.":
            # Whichever task applied it, the server binds the respawn to the
            # square the character stands on.
            m = self.state.map
            if m.path:
                self.set_savebed(m.path, m.world_x, m.world_y)
        # kill_player (server types/player.c) sends exactly one of these, as
        # the whole message, per defeat: a PvP-area defeat, or a death after
        # its stat losses. Matching the whole text keeps a player's chat that
        # quotes one from passing for a death, and a player channel (where
        # anyone can type the exact notice) never carries a real one.
        died = normalized in DEATH_NOTICES and msg_type not in PLAYER_CHANNELS
        if lowered.startswith("the spell heals you for"):
            self._trend_count("heals")
        if died:
            # Before this death's own record: the previous life's end.
            previous_death = self._latest_ledger_death()
            self.record_action("death", text)
            self._trend_count("deaths")
            # The server emits death stat-loss lines before its final death
            # announcement. Commit the short buffered group now, then retain a
            # post-death window for protocol/order variants that arrive later.
            pending = [(stamp, message)
                       for stamp, message in self._pending_death_losses
                       if now - stamp <= 8.0]
            self._pending_death_losses.clear()
            for _, message in pending:
                self.set_depletion_points(self.state.depletion_points + 1)
                self.record_action(
                    "depletion",
                    f"point {self.state.depletion_points}: {message}")
            self._death_depletion_until = now + 8.0
            # After the bookkeeping above, so its depletion records are in it,
            # but dated at the notice's arrival.
            self._record_post_mortem(arrived, previous_death)
        death_loss = normalized in death_losses
        direct_drain = any(normalized == marker for marker in direct_drains)
        if death_loss and now > self._death_depletion_until:
            self._pending_death_losses = [
                (stamp, message)
                for stamp, message in self._pending_death_losses
                if now - stamp <= 8.0
            ]
            self._pending_death_losses.append((now, text))
        elif death_loss or direct_drain:
            self.set_depletion_points(self.state.depletion_points + 1)
            self.record_action(
                "depletion", f"point {self.state.depletion_points}: {text}")
        await self._respond_to_player_chat(msg_type, text)
        await self.emit("message", (msg_type, color, text))

    @staticmethod
    def _incoming_player_chat(msg_type: int, text: str) -> tuple[str, str]:
        """Extract the sender and body from server-authored chat markup."""
        if msg_type in (c.CHAT_TYPE_CHAT, c.CHAT_TYPE_LOCAL,
                        c.CHAT_TYPE_PARTY):
            # /chat, /say and party chat share the server's named prefix.
            match = re.fullmatch(
                r"\[a=#charname\]([^[]+)\[/a\]:\s*(.*)", text, re.S)
        elif msg_type == c.CHAT_TYPE_PRIVATE:
            match = re.fullmatch(
                r"\[a=#charname\]([^[]+)\[/a\]\s+tells you:\s*(.*)",
                text, re.S | re.I)
        else:
            return "", ""
        if match is None:
            return "", ""
        sender, body = match.group(1).strip(), match.group(2).strip()
        if msg_type == c.CHAT_TYPE_PARTY:
            # Party chat reads "[Party] Name says: text" after the prefix.
            body = re.sub(
                rf"^\[[^\]]*\]\s+{re.escape(sender)}\s+says:\s*", "",
                body, flags=re.I)
        return sender, body

    def _current_place_description(self) -> str:
        m = self.state.map
        name = re.sub(r"\[[^]]+\]", "", m.name).strip()
        region = re.sub(
            r"\[[^]]+\]", "", m.region_longname or m.region).strip()
        if name and region and region.casefold() not in name.casefold():
            return f"{name} in {region}"
        if name or region:
            return name or region
        leaf = logical_map_path(m.path).rsplit("/", 1)[-1]
        return leaf.replace("_", " ") if leaf else "the wilderness"

    def _reload_chat_rules(self) -> None:
        """Atomically replace small-talk rules when their file changes."""
        try:
            stat = self._chat_rules_path.stat()
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        except OSError:
            return
        if signature in (
                self._chat_rules_signature,
                self._chat_rules_failed_signature):
            return
        try:
            document = json.loads(self._chat_rules_path.read_text())
            records = document.get("rules", [])
            if not isinstance(records, list):
                raise ValueError("rules must be a list")
            compiled = []
            seen = set()
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("each chat rule must be an object")
                rule_id = str(record.get("id", "")).strip()
                patterns = record.get("patterns", [])
                responses = record.get("responses", [])
                if (not rule_id or rule_id in seen or
                        not isinstance(patterns, list) or not patterns or
                        not isinstance(responses, list) or not responses):
                    raise ValueError(
                        "chat rules need a unique id, patterns and responses")
                if len(patterns) > 20 or len(responses) > 20:
                    raise ValueError("chat rule is unreasonably large")
                compiled.append((
                    rule_id,
                    tuple(re.compile(str(pattern), re.I)
                          for pattern in patterns),
                    tuple(str(response)[:240] for response in responses
                          if str(response).strip()),
                ))
                if not compiled[-1][2]:
                    raise ValueError("chat rule responses cannot be empty")
                seen.add(rule_id)
        except (OSError, TypeError, ValueError, re.error) as exc:
            self._chat_rules_failed_signature = signature
            log.warning("keeping prior chat rules after reload failure: %s",
                        exc)
            return
        self._chat_rules = compiled
        self._chat_rules_signature = signature
        self._chat_rules_failed_signature = None
        log.info("loaded %s hot-reloadable chat rules from %s",
                 len(compiled), self._chat_rules_path)

    def _small_talk_response(self, text: str) -> str:
        """Return a hot-reloadable lightweight response for a known intent."""
        self._reload_chat_rules()
        for rule_id, patterns, responses in self._chat_rules:
            if not any(pattern.search(text) for pattern in patterns):
                continue
            uses = self._chat_rule_uses.get(rule_id, 0)
            self._chat_rule_uses[rule_id] = uses + 1
            return responses[uses % len(responses)]
        return ""

    def chat_policy_status(self) -> dict[str, int | str | bool]:
        """Expose chat hot-reload state without leaking message contents."""
        return {
            "path": str(self._chat_rules_path),
            "rules": len(self._chat_rules),
            "loaded": self._chat_rules_signature is not None,
            "reload_error": self._chat_rules_failed_signature is not None,
        }

    def _chat_task_context(self) -> dict[str, str]:
        if self.chat_context_provider is None:
            return {}
        try:
            value = self.chat_context_provider()
        except Exception:
            log.exception("chat task-context provider failed")
            return {}
        return value if isinstance(value, dict) else {}

    def _training_description(self) -> str:
        context = self._chat_task_context()
        skill_name = str(context.get("combat_skill", "")).strip()
        skill = None
        if skill_name:
            skill = next((
                item for item in self.state.inventory
                if item.item_type == c.TYPE_SKILL and
                item.name.casefold() == skill_name.casefold()), None)
        if skill is None:
            weapon_tag = int(self.state.equipment.get(c.EQUIP_WEAPON, 0) or 0)
            weapon = self.state.items.get(weapon_tag)
            required_tag = int(
                getattr(weapon, "required_skill_tag", 0) or 0)
            candidate = self.state.items.get(required_tag)
            if candidate is not None and candidate.item_type == c.TYPE_SKILL:
                skill = candidate
                skill_name = candidate.name
        if not skill_name:
            return "my combat skills"
        level = int(skill.extra.get("level", 0) or 0) if skill else 0
        display = skill_name.removesuffix(" weapons").title()
        return f"{display}, currently level {level}" if level else display

    def _observe(self, step: Callable[[ObservationRecorder], Any]) -> None:
        """Feed the observation recorder and keep what it returns; never
        fails or delays play (a locked database drops the reading)."""
        recorder = getattr(self, "observations", None)
        if recorder is None:
            return
        try:
            observations = step(recorder) or ()
        except Exception:
            log.exception("observation recorder failed")
            return
        # Only once her memory is open: a reading belongs to her character.
        character = getattr(self, "_memory_character", "")
        if not observations or not character or character != self.state.player_name:
            return
        for observation in observations:
            if not isinstance(observation, Observation):
                continue
            # Parsed before the write, so the facts share its transaction; a
            # repeat the same parser set read stores none again.
            found = self._parse_facts(observation)
            try:
                _, stored = self._memory_store().record_observation_facts(
                    character, observation.row(),
                    [fact.row(at=observation.at) for fact in found], fact_parsers.VERSION)
            except sqlite3.Error as error:
                log.warning("observation not kept: %s", error)
                continue
            except Exception:
                log.exception("observation not kept")
                continue
            self._feedback_from_observation(observation.row())
            if self.metrics is not None:
                self.metrics.observation(observation.source_kind)
                # Facts come only from what was kept: the row is their source.
                for fact, (_, created) in zip(found, stored):
                    if created:
                        self.metrics.fact(fact.kind, fact.status)

    def store_fight(self, row: dict[str, Any]) -> bool:
        """Keep one fight row (terrain.py) for the character it belongs to,
        if her memory is open; never fails or stalls play (a locked database
        waits at most memory.FIGHT_SAVE_TIMEOUT and drops the row)."""
        character = getattr(self, "_memory_character", "")
        if not character or character != row.get("character") or \
                character != self.state.player_name:
            return False
        try:
            self._memory_store().record_fight(character, row)
        except sqlite3.Error as error:
            log.warning("fight not kept: %s", error)
            return False
        except Exception:
            log.exception("fight not kept")
            return False
        if self.metrics is not None:
            self.metrics.fight_position(str(row.get("feature")), str(row.get("outcome")))
        return True

    # The feedback queue (feedback.py, feedback_producers.py): drafts only;
    # nothing here sends anything anywhere.

    def _feedback_other_names(self) -> list[str]:
        """Other characters she has seen: in this process, and every chat
        sender her memory keeps."""
        names = list(getattr(self.observations, "player_names", {}))
        if self._feedback_senders is None:
            # A failure propagates: without the chat senders no draft is made.
            self._feedback_senders = set(
                self._memory_store().chat_senders(timeout=FEEDBACK_READ_TIMEOUT))
        names.extend(self._feedback_senders)
        return names

    def feedback_private_terms(self) -> list[str]:
        """Every term no draft may show (feedback.screen, the dashboard)."""
        return self._feedback_private(self._feedback_other_names())

    def _feedback_private(self, others: list[str]) -> list[str]:
        """Every term no draft may contain (4.10.4). A term that is a common
        word is warned about once: it withholds nearly every draft."""
        terms = self._feedback_private_terms(others)
        for term in feedback.common_terms(terms):
            if term not in self._feedback_common_warned:
                self._feedback_common_warned.add(term)
                log.warning("a private term is the common word %r: feedback drafts "
                            "containing it are withheld", term)
        return terms

    def _feedback_private_terms(self, others: list[str]) -> list[str]:
        config = self.config
        return [config.account, config.password, config.access_code, config.host,
                config.certificate_sha256, config.character, config.party_name,
                self.state.player_name, self.state.party_name,
                *self.feedback_private_names, *others]

    def _note_feedback(self, findings: list[feedback.Finding], detail: str = "",
                       others: list[str] | None = None) -> None:
        """Store findings as drafts; never fails play. A finding already noted
        in this process with the same occurrence and detail costs nothing."""
        character = getattr(self, "_memory_character", "")
        if not character or character != self.state.player_name:
            return
        for finding in findings:
            key = (finding.fingerprint, finding.occurrence, detail)
            if key in self._feedback_noted:
                continue
            try:
                names = self._feedback_other_names() if others is None else others
                stored = feedback.note(self._memory_store(), character, finding,
                                       private=self._feedback_private(names))
            except Exception as error:
                log.warning("feedback draft not kept: %s", error)
                continue
            if stored is None:
                # Withheld for a private term: tried again on its next sighting.
                continue
            self._feedback_noted.add(key)
            if len(self._feedback_noted) > 10_000:
                self._feedback_noted.clear()

    def _players_present(self) -> int:
        try:
            return len(self.observations.players_in_view(self.state))
        except Exception:
            return 0

    def _feedback_from_observation(self, row: dict[str, Any]) -> None:
        """Producers that read one stored observation: typos in authored
        lock and sign text, and a book read at low Int."""
        findings: list[feedback.Finding] = []
        try:
            if row.get("source_kind") == "book":
                book = feedback_producers.book_experience(
                    row, stats=self.state.stats, literacy=self._skill_level("literacy"),
                    players_present=self._players_present())
                if book is not None:
                    findings.append(book)
            elif (row.get("source_kind") in feedback_producers.TYPO_SOURCES and
                  feedback_producers.find_typos(str(row.get("text") or ""))):
                others = self._feedback_other_names()
                found = feedback_producers.typo(
                    row, names=others, stats=self.state.stats,
                    players_present=self._players_present())
                if found is not None:
                    self._note_feedback([found], others=others)
                return
        except sqlite3.Error as error:
            log.warning("feedback producers skipped: %s", error)
            return
        except Exception:
            log.exception("feedback producers failed")
            return
        self._note_feedback(findings)

    def _feedback_sweep(self, character: str) -> None:
        """Typos in the lock and sign texts her memory already keeps (each
        kind's newest 1000), once a login."""
        try:
            memory = self._memory_store()
            rows = [row for kind in feedback_producers.TYPO_SOURCES
                    for row in memory.observations(character, limit=1000, source_kind=kind,
                                                   timeout=FEEDBACK_READ_TIMEOUT)
                    if feedback_producers.find_typos(str(row.get("text") or ""))]
            if not rows:
                return
            others = self._feedback_other_names()
            findings = [found for row in rows
                        if (found := feedback_producers.typo(row, names=others)) is not None]
        except Exception as error:
            log.warning("feedback sweep skipped: %s", error)
            return
        self._note_feedback(findings, others=others)

    def screen_feedback_queue(self) -> int:
        """Screen the queue with her full private terms (feedback.screen):
        every approved item, and every open item whose text changed since
        its last screen (an owner's edit). A failing item is withheld; the
        rest are marked screened, so the CLI may approve them and the filer
        may file them (the filer also needs a screen after approval). Only
        text unchanged since it was read here is marked. Returns how many
        were screened clean; never raises."""
        character = getattr(self, "_memory_character", "")
        if not character:
            return 0
        try:
            memory = self._memory_store()
            # Marked screened as of the read: an approval after it needs
            # the next pass.
            read_at = time.time()
            items = memory.feedback_items_to_screen(character, timeout=FEEDBACK_READ_TIMEOUT)
            versions = {int(item["id"]): item["updated_at"] for item in items}
            newly = feedback.screen(items, self.feedback_private_terms())
            if newly:
                memory.withhold_feedback(character, newly, timeout=FEEDBACK_READ_TIMEOUT)
            clean = [int(item["id"]) for item in items if not item["withheld"]]
            memory.mark_feedback_screened(character, clean, versions=versions, refresh=True,
                                          at=read_at, timeout=FEEDBACK_READ_TIMEOUT)
            self.feedback_attention = memory.feedback_attention(
                character, timeout=FEEDBACK_READ_TIMEOUT)
        except Exception as error:
            log.warning("feedback queue not screened: %s", error)
            return 0
        return len(clean)

    async def screen_feedback_periodically(self,
                                           interval: float = FEEDBACK_SCREEN_SECONDS) -> None:
        """``screen_feedback_queue`` every ``interval`` seconds, for good."""
        while True:
            await asyncio.sleep(interval)
            self.screen_feedback_queue()

    def note_equipment_feedback(self) -> None:
        """The "+N with no effect" producer, over her identified gear. Called
        on the gear examiner's quiet tick; writes only when the set of items
        that qualify changes in this process (or a new day starts), and runs
        at most every EQUIPMENT_FEEDBACK_SECONDS."""
        now = time.monotonic()
        if now - self._equipment_feedback_at < EQUIPMENT_FEEDBACK_SECONDS:
            return
        self._equipment_feedback_at = now
        try:
            from .equipment import (FAMILY_NAMES, GEAR_LABELS, item_base,
                                    item_effective_stats, magic_from_name)
            entries = []
            for item in self.state.inventory:
                if item.item_type not in GEAR_LABELS:
                    continue
                magic = magic_from_name(item.name, item.item_type)
                if not magic:
                    continue
                known = item_base(self, item)
                family = (known[0] if known is not None and known[0] else
                          FAMILY_NAMES.get(item.item_type, ""))
                entries.append((family, item.item_type, magic,
                                item_effective_stats(self, item)))
            found = feedback_producers.plus_n_no_effect(
                entries, stats=self.state.stats, at=time.time())
        except Exception:
            log.exception("equipment feedback producer failed")
            return
        if found is not None:
            detail = ",".join(sorted(item["key"] for item in found.evidence["items"]))
            self._note_feedback([found], detail)

    def _skill_level(self, skill_name: str) -> int | None:
        return next((int(skill.extra.get("level", 0) or 0)
                     for skill in self.state.inventory
                     if skill.item_type == c.TYPE_SKILL and
                     skill.name.casefold() == skill_name), None)

    def _observation_dropped(self, reason: str) -> None:
        if getattr(self, "metrics", None) is not None:
            self.metrics.observation_dropped(reason)

    def _log_chat(self, msg_type: int, direction: str, sender: str,
                  text: str) -> None:
        """Keep a chat line in the durable chat log; never fail the bot."""
        if self.metrics is not None:
            self.metrics.chat(msg_type, direction)
        if self._feedback_senders is not None and sender:
            # Kept as the chat log keeps it, so drafts never name the sender.
            self._feedback_senders.add(sender[:64])
        character = self.state.player_name or self.config.character
        if not character:
            return
        try:
            self._memory_store().append_chat(
                character, msg_type, direction, sender, text)
        except Exception:
            log.exception("could not keep a chat line")

    async def _respond_to_player_chat(self, msg_type: int, text: str) -> None:
        """Answer small, verifiable social questions without chat spam."""
        sender, body = self._incoming_player_chat(msg_type, text)
        if not sender or not body:
            return
        player_name = (self.state.player_name or
                       self.config.character).casefold()
        if sender.casefold() == player_name:
            return
        self._log_chat(msg_type, "in", sender, body)
        folded = re.sub(r"\s+", " ", body.casefold()).strip()
        chat_now = time.monotonic()
        recent_correspondent = (
            chat_now - self._chat_correspondents.get(
                sender.casefold(), 0.0) <= 90.0)
        # A line opening with another name ("Mira, ...") is someone else's,
        # as her own replies are: never hers to answer (no bot loops).
        # "Hi, Sera" or "Ok, where are you" open with a word, not a name;
        # "Ok, Kitty, ..." is still Kitty's. Only that plain "Name," form is
        # read: missing chat to her is worse than answering "Hey Kitty,
        # where is Sera?", and the reply caps bound what that lets through.
        names_her = bool(player_name and re.search(
            rf"\b{re.escape(player_name)}\b", folded))
        # The first leading "word," that is not an opener names the addressee.
        addressee = ""
        leading = re.match(r"(?:[a-z][\w'-]*\s*,\s*)*", folded).group()
        for word in re.findall(r"[a-z][\w'-]*", leading):
            if _fold_letters(word) not in CHAT_INTERJECTIONS_FOLDED:
                addressee = word
                break
        if (msg_type != c.CHAT_TYPE_PRIVATE and addressee and
                addressee != player_name):
            return
        # A tell is meant for her; /say, party and /chat only when they name
        # her or continue a conversation she answered.
        direct = bool(
            msg_type == c.CHAT_TYPE_PRIVATE or recent_correspondent or names_her)
        location_question = bool(re.search(
            r"\b(?:where\s+(?:are|r)\s+(?:you|u)|"
            r"where(?:'s|\s+is)\s+(?:sera|she)|"
            r"what(?:'s|\s+is)\s+your\s+(?:location|position)|"
            r"what\s+(?:map|zone|area)\s+are\s+you\s+(?:in|at))\b",
            folded))
        presence_question = bool(
            re.search(r"\b(?:are\s+)?(?:you|u)\s+there\b", folded) or
            (player_name and re.fullmatch(
                rf"(?:yo\s+|hey\s+)?{re.escape(player_name)}[?!. ]*",
                folded)))
        status_question = bool(re.search(
            r"\bwhat(?:'re|\s+are)\s+you\s+(?:doing|farming|fighting)|"
            r"\bwhat\s+are\s+you\s+up\s+to\b", folded))
        wellbeing_question = bool(re.search(
            r"\bhow\s+are\s+you(?:\s+doing)?\b|"
            r"\bhow(?:'s|\s+is)\s+it\s+going\b|"
            r"\bare\s+you\s+(?:okay|ok|alright)\b", folded))
        level_question = bool(re.search(
            r"\bwhat\s+level\s+are\s+you|\bwhat(?:'s|\s+is)\s+your\s+level",
            folded))
        party_question = bool(re.search(
            r"\b(?:can|could|may)\s+i\s+join(?:\s+(?:your|the))?\s+party\b|"
            r"\bhow\s+do\s+i\s+join(?:\s+(?:your|the))?\s+party\b",
            folded))
        farming_invitation = bool(re.search(
            r"\b(?:would|do)\s+you\s+like\s+to\s+(?:come\s+)?join\s+me\b|"
            r"\b(?:want|wanna)\s+to\s+(?:come\s+)?(?:farm|hunt|fight)\b|"
            r"\bcome\s+(?:farm|hunt|fight)\s+with\s+me\b", folded))
        farming_tip = bool(re.search(
            r"\b(?:best|good|great|better|efficient)\b.*"
            r"\b(?:farm|farming|xp|experience)\b|"
            r"\b(?:farm|farming)\b.*\b(?:spot|place|zone|map)\b",
            folded))
        training_question = bool(re.search(
            r"\btraining\s+what\b|\bwhat\s+are\s+you\s+training\b|"
            r"\bwhich\s+skill\s+are\s+you\s+training\b", folded))
        swamp_join_question = bool(
            re.search(r"\b(?:asteria\s+swamp|swamp\s+near\s+asteria)\b",
                      folded) and
            re.search(
                r"\b(?:are\s+you\s+(?:going|coming)|will\s+you|"
                r"show\s+you\s+how|join\s+me|meet\s+me)\b", folded))
        greeting = bool(
            direct and re.fullmatch(
                r"(?:hi|hey|hello|hiya|howdy)[,! ]*(?:sera)?[!? ]*",
                folded))

        response = ""
        fallback = False
        if location_question and direct:
            response = f"I'm in {self._current_place_description()} right now."
        elif status_question and direct:
            response = (
                f"I'm training and exploring around "
                f"{self._current_place_description()} right now.")
        elif wellbeing_question and direct:
            hp = int(self.state.stats.get("hp", 0) or 0)
            maxhp = int(self.state.stats.get("maxhp", 0) or 0)
            if maxhp and hp / maxhp < 0.5:
                response = "A little battered, but I'm getting somewhere safe!"
            elif maxhp and hp < maxhp:
                response = "Doing okay, thanks! Just recovering from a fight."
            else:
                response = "Doing well, thanks! Just out training."
        elif training_question and direct:
            response = f"I'm training {self._training_description()}."
        elif level_question and direct:
            level = int(self.state.stats.get("level", 0) or 0)
            response = f"I'm level {level}." if level else (
                "I'm not sure—my stats are still loading.")
        elif party_question and direct:
            party = self.state.party_name or self.config.party_name
            response = (f"Sure—my party is {party}. You can use "
                        f"/party join {party}." if party else
                        "Sure—I'll let you know once my party is open.")
        elif swamp_join_question and direct:
            destination = self._chat_task_context().get("destination", "")
            if str(destination).endswith("/world_1_50"):
                response = (
                    "Yep—I'm already on my way to the Asteria swamp. "
                    "I'm taking a careful route through the mountains.")
            else:
                response = (
                    "Thanks! I'm following a different training route "
                    "right now, though.")
        elif farming_invitation and direct:
            # Public chat must never become an unauthenticated remote-control
            # surface. Acknowledge the player naturally while retaining the
            # autonomous, safety-reviewed progression route.
            response = (
                "Thanks for the invite! I'm following my training route "
                "right now, though.")
        elif farming_tip and direct:
            response = "Oh nice, thanks for the tip! I'll check it out."
            self.record_action(
                "chat-farming-tip", f"sender={sender} text={body}")
        elif greeting:
            response = "Hey!"
        elif presence_question and direct:
            response = "I'm here! Just out training."
        elif direct and (small_talk := self._small_talk_response(folded)):
            response = small_talk
        elif direct:
            # At most one fallback per player every few minutes.
            if (chat_now - self._chat_fallback_at.get(sender.casefold(), -1e9)
                    < CHAT_FALLBACK_SECONDS):
                return
            fallback = True
        else:
            return

        prompt_key = f"{msg_type}:{sender.casefold()}:{folded}"
        now = chat_now
        replies = self._chat_replies.get(sender.casefold(), ())
        if (prompt_key == self._last_chat_prompt or
                now - self._last_chat_reply_at < 12.0 or
                (len(replies) == CHAT_REPLIES_PER_SENDER and
                 now - replies[0] < CHAT_REPLY_WINDOW_SECONDS)):
            return
        self._last_chat_prompt = prompt_key
        self._last_chat_reply_at = now
        # Reinserted on each reply, so the dict runs least recently used
        # first; senders quiet for a whole window are forgotten.
        replies = self._chat_replies.pop(
            sender.casefold(), None) or deque(maxlen=CHAT_REPLIES_PER_SENDER)
        replies.append(now)
        for key in [key for key, value in self._chat_replies.items()
                    if now - value[-1] >= CHAT_REPLY_WINDOW_SECONDS]:
            del self._chat_replies[key]
        self._chat_replies[sender.casefold()] = replies
        if len(self._chat_replies) > 64:
            del self._chat_replies[next(iter(self._chat_replies))]
        if fallback:
            # The review queue for new rules from questions real players
            # actually ask; the chat log keeps the full conversation.
            self.record_action(
                "chat-unhandled",
                f"channel={msg_type} sender={sender} text={body}")
            response = CHAT_FALLBACKS[
                self._chat_fallback_uses % len(CHAT_FALLBACKS)]
            self._chat_fallback_uses += 1
            for key in [key for key, at in self._chat_fallback_at.items()
                        if now - at >= CHAT_FALLBACK_SECONDS]:
                del self._chat_fallback_at[key]
            self._chat_fallback_at[sender.casefold()] = now
        else:
            # Only an answered question keeps the conversation open.
            self._chat_correspondents[sender.casefold()] = now
        if len(self._chat_correspondents) > 32:
            oldest = min(
                self._chat_correspondents,
                key=self._chat_correspondents.__getitem__)
            del self._chat_correspondents[oldest]
        clean_sender = sender.replace('"', "")
        # Answer on the channel she was spoken to on.
        if msg_type == c.CHAT_TYPE_PRIVATE:
            command = f'/tell "{clean_sender}" {response}'
        elif msg_type == c.CHAT_TYPE_LOCAL:
            command = f"/say {clean_sender}, {response}"
        elif msg_type == c.CHAT_TYPE_PARTY:
            command = f"/gsay {clean_sender}, {response}"
        else:
            command = f"/chat {clean_sender}, {response}"
        try:
            await self.command(command)
        except ConnectionError:
            # The connection dropped as she answered; chat is never worth
            # failing the message handler for.
            log.info("chat reply to %s not sent: disconnected", clean_sender)
            return
        self._log_chat(msg_type, "out", clean_sender, response)
        self.record_action(
            "chat-response",
            f"channel={msg_type} sender={sender} response={response}")

    async def _handle_target(self, cur: Cursor, raw: bytes) -> None:
        # Parse and validate the complete reply before publishing any of it.
        # A malformed frame must not replace a currently authoritative target.
        target_code = cur.u8()
        target_color = cur.cstring()
        target_name = cur.cstring()
        target_level = cur.u8()
        target_actor_count = cur.u32()
        target_relative_depth = cur.i8()
        combat = cur.u8()
        combat_force = cur.u8()
        if (cur.remaining or target_code > c.TARGET_FRIEND or
                not -c.TARGET_MAX_RELATIVE_DEPTH <= target_relative_depth <=
                c.TARGET_MAX_RELATIVE_DEPTH or
                (target_code == c.TARGET_SELF and
                 (target_actor_count != 0 or target_relative_depth != 0)) or
                (target_code != c.TARGET_SELF and target_actor_count == 0)):
            raise ProtocolError("invalid target response")
        self.state.target_code = target_code
        self.state.target_color = target_color
        self.state.target_name = target_name
        self.state.target_level = target_level
        self.state.target_actor_count = target_actor_count
        self.state.target_relative_depth = target_relative_depth
        self.state.target_id = target_actor_count
        if target_actor_count:
            self.state.target_ack = (target_actor_count, time.monotonic())
        self.state.combat = bool(combat)
        self.state.combat_force = bool(combat_force)
        await self.emit("target", target_name)

    async def _handle_stats(self, cur: Cursor, raw: bytes) -> None:
        changed: dict[str, int | float] = {}
        while cur.remaining:
            stat = cur.u8()
            if c.STAT_EQUIP_START <= stat <= c.STAT_EQUIP_END:
                self.state.equipment[stat - c.STAT_EQUIP_START] = cur.u32()
                continue
            if c.STAT_PROT_START <= stat <= c.STAT_PROT_END:
                self.state.protections[stat - c.STAT_PROT_START] = cur.i8()
                continue
            fmt = c.STAT_FORMATS.get(stat)
            if fmt is None:
                raise ProtocolError(f"unknown stat field {stat}")
            value = cur.unpack(fmt)
            name = c.STAT_NAMES[stat]
            if stat in (c.STAT_REGEN_HP, c.STAT_REGEN_SP):
                value /= 10.0
            elif stat == c.STAT_WEIGHT_LIMIT:
                value /= 1000.0
            self.state.stats[name] = value
            self.state.stat_observed_at[name] = time.monotonic()
            changed[name] = value
        if any(key in changed for key in post_mortem.VITAL_KEYS):
            self.vitals.append(post_mortem.sample(self.state.stats))
            cutoff = time.time() - post_mortem.VITALS_SECONDS
            # A deque: each expired sample leaves from the front in O(1), so
            # a full window costs the same per packet as an empty one. The
            # newest expired sample stays: samples come only on change, so it
            # is the state until the next one, the baseline of a sudden death.
            while len(self.vitals) > 1 and self.vitals[1][0] < cutoff:
                self.vitals.popleft()
        if self.trends.observe(self.state):
            self._persist_trends()
        self.progress_watch.observe(self.state)
        self.state.food_watch.observe(self.state)
        await self.emit("stats", changed)

    def _decode_item_fields(self, cur: Cursor, flags: int, item: Item) -> None:
        if flags & c.UPD_LOCATION:
            item.location = cur.u32()
        if flags & c.UPD_FLAGS:
            item.flags = cur.u32()
        if flags & c.UPD_WEIGHT:
            item.weight = cur.u32() / 1000.0
        if flags & c.UPD_FACE:
            item.face = cur.u16()
        if flags & c.UPD_DIRECTION:
            item.direction = cur.u8()
        if flags & c.UPD_TYPE:
            item.item_type, item.subtype, item.quality = cur.u8(), cur.u8(), cur.u8()
            if item.quality != 255:
                item.condition = cur.u8()
                item.required_level = cur.u8()
                item.required_skill_tag = cur.u32()
        if flags & c.UPD_NAME:
            item.name = cur.cstring()
        if flags & c.UPD_ANIM:
            item.animation = cur.u16()
        if flags & c.UPD_ANIMSPEED:
            item.animation_speed = cur.u8()
        if flags & c.UPD_NROF:
            item.quantity = cur.u32() or 1
        if flags & c.UPD_EXTRA:
            if item.item_type == c.TYPE_SPELL:
                item.extra.update(cost=cur.u16(), path=cur.u32(), flags=cur.u32(), message=cur.cstring())
            elif item.item_type == c.TYPE_SKILL:
                item.extra.update(level=cur.u8(), experience=cur.i64(), message=cur.cstring())
            # The server writes extra fields only for SPELL and SKILL
            # (server/src/socket/item.c add_object_to_packet). Force and
            # poisoning effects travel in the explicit status-effect packet
            # since Classic #134, yet poison pulses still send UPD_EXTRA
            # for the poisoning object: reading fields here desynchronizes
            # the rest of the packet.
        if flags & c.UPD_GLOW:
            item.glow, item.glow_speed = cur.cstring(), cur.u8()

    async def _handle_item(self, cur: Cursor, raw: bytes) -> None:
        delete_env = bool(cur.u8())
        completes_player_replay = False
        # Listed contents of the items being replaced, kept for those the
        # packet sends again.
        nested: dict[int, list[int]] = {}
        if delete_env:
            location_to_clear = cur.u32()
            completes_player_replay = (
                location_to_clear == self.state.player_tag)
            if not cur.remaining and location_to_clear not in (
                    0, self.state.player_tag):
                # esrv_close_container (server/src/socket/item.c): the
                # contents are no longer shown, which is not an empty
                # container. Forget them, so a later visit opens it again.
                for tag in list(self.state.inventories.pop(
                        location_to_clear, [])):
                    self.state.remove_item(tag)
                return
            replaced = list(self.state.inventories.get(location_to_clear, []))
            # Opening an underfoot chest changes its face, so the server
            # redraws the ground (esrv_draw_look) right after sending the
            # chest's contents (esrv_send_inventory). That redraw replaces
            # the chest itself, not what it holds: keep its contents.
            nested = {tag: self.state.inventories.pop(tag)
                      for tag in replaced if tag in self.state.inventories}
            for tag in replaced:
                self.state.remove_item(tag)
            self.state.inventories[location_to_clear] = []
            if not cur.remaining:
                self._drop_nested(nested, set())
                if completes_player_replay:
                    self.state.inventory_replay_complete = True
                    self._forget_cured_depletion()
                return
        location, append = cur.u32(), bool(cur.u8())
        changed: list[Item] = []
        while cur.remaining:
            tag = cur.u32()
            apply_action = 0
            if tag == 0:
                apply_action = cur.u8()
                # Virtual ground paginator. Preserve a stable synthetic ID.
                tag = -(apply_action + 1)
            item = self.state.items.get(tag, Item(tag=tag))
            item.extra["apply_action"] = apply_action
            flags = (c.UPD_FLAGS | c.UPD_WEIGHT | c.UPD_FACE | c.UPD_DIRECTION |
                     c.UPD_NAME | c.UPD_ANIM | c.UPD_ANIMSPEED | c.UPD_NROF | c.UPD_GLOW)
            if location > 0:
                flags |= c.UPD_TYPE | c.UPD_EXTRA
            self._decode_item_fields(cur, flags, item)
            self.state.place_item(item, location, append)
            changed.append(item)
        for tag in [tag for tag in nested if tag in self.state.items]:
            self.state.inventories[tag] = nested.pop(tag)
        self._drop_nested(nested, {item.tag for item in changed})
        if completes_player_replay:
            self.state.inventory_replay_complete = True
            self._forget_cured_depletion()
        await self.emit("items", changed)

    def _drop_nested(self, nested: dict[int, list[int]],
                     resent: set[int]) -> None:
        """Forget the contents of replaced items the server did not resend.

        A child the same packet sent again (moved out to the ground, say)
        stays where the packet put it.
        """
        for children in nested.values():
            for child in children:
                if child not in resent:
                    self.state.remove_item(child)

    async def _handle_item_update(self, cur: Cursor, raw: bytes) -> None:
        flags, tag = cur.u16(), cur.u32()
        item = self.state.items.get(tag)
        if item is None:
            await self.emit("unknown_item_update", tag)
            return
        self._decode_item_fields(cur, flags, item)
        await self.emit("item_updated", item)

    async def _handle_item_delete(self, cur: Cursor, raw: bytes) -> None:
        deleted = []
        lost_depletion = False
        while cur.remaining:
            tag = cur.u32()
            item = self.state.items.get(tag)
            lost_depletion = lost_depletion or (
                item is not None and self._is_depletion_force(item) and
                tag in self.state.inventories.get(self.state.player_tag, []))
            self.state.remove_item(tag)
            deleted.append(tag)
        if lost_depletion:
            self._forget_cured_depletion()
        await self.emit("items_deleted", deleted)

    @staticmethod
    def _is_depletion_force(item: Item) -> bool:
        return (item.item_type == c.TYPE_FORCE and
                item.name.casefold() == "depletion")

    def _forget_cured_depletion(self) -> None:
        """Reset the depletion count once the authoritative bag lacks the force.

        Only a full replay or the force's own removal proves a cure: a cure
        from the dashboard or while offline otherwise leaves the old count
        for the next single loss to extend past the service threshold.
        """
        if self.state.depletion_points and not any(
                self._is_depletion_force(item)
                for item in self.state.inventory):
            self.set_depletion_points(0)
            self.record_action("depletion", "cleared: the depletion force is gone")
            if self.state.cure_funding_farm_since:
                # Cured: a cure-funding farm belongs to the depletion just
                # removed, never to the next one (navigation.cure_funding_farm).
                self.set_cure_funding_farm(since=0.0)
                self.record_action("cure-funding-farm-ended",
                                   "depletion cured")

    async def _handle_mapstats(self, cur: Cursor, raw: bytes) -> None:
        # Publish metadata and time together only after the complete payload
        # validates, so a truncated record cannot expose partial observations.
        m = copy.deepcopy(self.state.map)
        game_seconds = self.state.game_seconds
        game_millis_per_minute = self.state.game_millis_per_minute
        while cur.remaining:
            kind = cur.u8()
            if kind == 1:
                m.name = self._map_string(cur, 4095)
            elif kind == 2:
                m.music = self._map_string(cur, 4095)
            elif kind == 3:
                m.weather = self._map_string(cur, 255)
            elif kind == 4:
                self._map_string(cur, 4095); self._map_string(cur, 4095)
            elif kind == 5:
                game_seconds, game_millis_per_minute = cur.u64(), cur.u32()
            else:
                raise ProtocolError(f"unknown mapstats field {kind}")
        self.state.map = m
        self.state.game_seconds = game_seconds
        self.state.game_millis_per_minute = game_millis_per_minute

    @staticmethod
    def _map_string(cur: Cursor, maximum: int) -> str:
        end = cur.data.find(b"\0", cur.pos)
        if end < cur.pos or end - cur.pos > maximum:
            raise ProtocolError("invalid MAP2 string")
        return cur.cstring()

    @staticmethod
    def _map_tile_copy(tiles: dict[tuple[int, int], object], x: int, y: int):
        from .model import Tile
        return tiles.setdefault((x, y), Tile())

    def _map_level(self, cur: Cursor, size: int, m, depth: int) -> None:
        from .model import MapObject, Tile
        if size > cur.remaining:
            raise ProtocolError("MAP2 level exceeds envelope")
        body = Cursor(cur.take(size))
        tiles = m.tiles_at_depth(depth)
        known_mask = (c.MAP2_MASK_SUPPORT_HEIGHT | c.MAP2_MASK_CLEAR |
                      c.MAP2_MASK_LIGHT_LEVEL | c.MAP2_MASK_LIGHT_LEVEL_MORE |
                      c.MAP2_MASK_HARD_CLEAR | c.MAP2_MASK_FOW)
        known_flags2 = (c.MAP_FLAG2_ALPHA | c.MAP_FLAG2_ROTATE | c.MAP_FLAG2_ZOOM |
                        c.MAP_FLAG2_TARGET | c.MAP_FLAG2_PROBE | c.MAP_FLAG2_PRIORITY |
                        c.MAP_FLAG2_SECONDPASS | c.MAP_FLAG2_GLOW | c.MAP_FLAG2_ROOF |
                        c.MAP_FLAG2_DOOR | c.MAP_FLAG2_EXIT | c.MAP_FLAG2_EMITTER)
        while body.remaining:
            mask = body.u16(); x, y = (mask >> 11) & 31, (mask >> 6) & 31
            values = mask & 63
            if x >= m.width or y >= m.height or values & ~known_mask:
                raise ProtocolError("invalid MAP2 tile mask")
            if values & c.MAP2_MASK_CLEAR:
                if values & ~(c.MAP2_MASK_CLEAR | c.MAP2_MASK_HARD_CLEAR):
                    raise ProtocolError("invalid MAP2 clear")
                if values & c.MAP2_MASK_HARD_CLEAR or (x, y) not in tiles:
                    tiles[(x, y)] = Tile()
                    if not values & c.MAP2_MASK_HARD_CLEAR:
                        tiles[(x, y)].fogged = True
                else:
                    # A soft clear is the client's fog-of-war transition.
                    remembered = self._map_tile_copy(tiles, x, y)
                    remembered.remember_as_fog()
                continue
            if values & c.MAP2_MASK_HARD_CLEAR:
                raise ProtocolError("invalid MAP2 hard clear")
            tile = self._map_tile_copy(tiles, x, y)
            if values & c.MAP2_MASK_SUPPORT_HEIGHT: tile.support_height = body.i16()
            fogged = tile.fogged
            if values & c.MAP2_MASK_FOW:
                fog_value = body.u8()
                if fog_value > 1: raise ProtocolError("invalid MAP2 fow")
                fogged = bool(fog_value)
            # Signed Q4.11 environment radiance (2048 is daylight).
            if values & c.MAP2_MASK_LIGHT_LEVEL: tile.darkness[0] = body.i16()
            if values & c.MAP2_MASK_LIGHT_LEVEL_MORE:
                for sub in range(1, 7): tile.darkness[sub] = body.i16()
            count = body.u8()
            if count > c.MAP2_REAL_LAYERS: raise ProtocolError("too many MAP2 layers")
            seen=set()
            for _ in range(count):
                layer=body.u8()
                if layer == c.MAP_LAYER_CLEAR:
                    layer=body.u8()
                    if layer >= c.MAP2_REAL_LAYERS or layer in seen: raise ProtocolError("invalid MAP2 layer")
                    seen.add(layer); tile.objects.pop(layer, None); continue
                if layer >= c.MAP2_REAL_LAYERS or layer in seen: raise ProtocolError("invalid MAP2 layer")
                seen.add(layer)
                face, object_flags, flags = body.u16(), body.u8(), body.u8()
                obj=MapObject(layer, face, object_flags, flags)
                if flags & c.MAP_FLAG_MULTI: body.u8()
                if flags & c.MAP_FLAG_NAME: obj.name, obj.name_color = self._map_string(body,63), self._map_string(body,6)
                if flags & c.MAP_FLAG_ANIMATION:
                    obj.animation=face; obj.animation_speed=body.u8(); obj.direction=body.u8(); anim=body.u8()
                    if anim & ~15: raise ProtocolError("invalid MAP2 animation")
                    if anim & c.ANIM_MOVING: body.u8()
                if flags & c.MAP_FLAG_HEIGHT: body.i16()
                if flags & c.MAP_FLAG_ALIGN: body.i16()
                if flags & c.MAP_FLAG_MORE:
                    flags2=body.u32()
                    if flags2 & ~known_flags2: raise ProtocolError("unknown MAP2 object flags")
                    if flags2 & c.MAP_FLAG2_ALPHA: body.u8()
                    if flags2 & c.MAP_FLAG2_ROTATE: body.i16()
                    if flags2 & c.MAP_FLAG2_ZOOM: body.u16(); body.u16()
                    if flags2 & c.MAP_FLAG2_TARGET:
                        actor_count, friend=body.u32(), body.u8()
                        if friend > 1: raise ProtocolError("invalid MAP2 target friend")
                        # TARGET identity is authoritative only for living
                        # layers. Other drawable layers may carry the bit for
                        # rendering but cannot grant a player action.
                        if layer % 7 == 5 and face != 0:
                            obj.target_id, obj.is_friend = actor_count, bool(friend)
                    if flags2 & c.MAP_FLAG2_PROBE:
                        obj.target_hp=body.u8()
                        if obj.target_hp > 100: raise ProtocolError("invalid MAP2 target hp")
                    if flags2 & c.MAP_FLAG2_GLOW: self._map_string(body,6); body.u8()
                    if flags2 & c.MAP_FLAG2_EMITTER:
                        # Darkness (negative radius) is achromatic and never
                        # flickers; zero is never sent.
                        radius, emitter=body.i8(), body.u8(); rgb=(body.u8(), body.u8(), body.u8())
                        if (not radius or emitter & c.MAP2_EMITTER_RESERVED or
                                radius < 0 and (emitter & c.MAP2_EMITTER_FLICKER or any(rgb))):
                            raise ProtocolError("invalid MAP2 emitter")
                tile.objects[layer]=obj
            ext=body.u8()
            if ext & ~(c.MAP_EXT_ANIM|c.MAP_EXT_LIGHT_RGB|c.MAP_EXT_LOCAL_LIGHT|c.MAP_EXT_OCCLUDER): raise ProtocolError("unknown MAP2 tile flags")
            if ext & c.MAP_EXT_LIGHT_RGB:
                bitmap=body.u8()
                if bitmap & ~127: raise ProtocolError("invalid MAP2 rgb bitmap")
                for sub in range(7):
                    if bitmap & (1<<sub): body.u16(); body.u16(); body.u16()
            if ext & c.MAP_EXT_ANIM:
                animations=body.u8(); seen_sub=set()
                if animations > 7: raise ProtocolError("too many MAP2 animations")
                for _ in range(animations):
                    sub, kind=body.u8(), body.u8(); value=body.i16()
                    if sub >= 7 or sub in seen_sub or kind not in (c.ANIM_DAMAGE, c.ANIM_KILL): raise ProtocolError("invalid MAP2 animation")
                    seen_sub.add(sub)
                    # A damage number or kill marker on this tile this round:
                    # published with the generation (_publish_tile_animations).
                    m.animations.append((x, y, depth, sub, kind, value))
            if ext & c.MAP_EXT_LOCAL_LIGHT:
                # Coarse per-sublayer local light: a signed 5-bit level
                # (-16 is invalid), the tli bit, a reserved bit, vision floor.
                levels=body.u8()
                if not 1 <= levels <= 7: raise ProtocolError("invalid MAP2 local light")
                for _ in range(levels):
                    value=body.u8()
                    if (value & c.MAP2_LOCAL_LIGHT_RESERVED or
                            value & c.MAP2_LOCAL_LIGHT_LEVEL_MASK == 0x10):
                        raise ProtocolError("invalid MAP2 local light")
            if ext & c.MAP_EXT_OCCLUDER:
                occluder, _horizontal, vertical=body.u16(), body.u16(), body.u8()
                if (occluder & c.MAP2_OCCLUDER_RESERVED or
                        vertical & c.MAP2_OCCLUDER_VERTICAL_RESERVED):
                    raise ProtocolError("invalid MAP2 occluder")
            # Fog applies after the record: remembered geometry may remain,
            # but it must never remain an actionable target identity.
            if fogged:
                tile.remember_as_fog()
            else:
                tile.fogged = False
        if body.remaining: raise ProtocolError("MAP2 level trailing bytes")

    def _publish_tile_animations(self, m: MapState) -> None:
        """The published generation's damage and kill animations, in world
        squares of its map, onto GameState.tile_animations."""
        if not m.animations:
            return
        now = time.time()
        cx, cy = m.width // 2, m.height // 2
        for x, y, depth, sub, kind, value in m.animations:
            self.state.add_tile_animation(TileAnimation(
                now, m.path, m.world_x + x - cx, m.world_y + y - cy, depth, sub,
                kind, value))

    async def _handle_map(self, cur: Cursor, raw: bytes) -> None:
        # A malformed envelope invalidates an unfinished generation.  The
        # published state remains untouched because `_handle_map_inner` stages
        # all mutations on a copy.
        try:
            await self._handle_map_inner(cur, raw)
        except ProtocolError:
            # Only a rejected continuation invalidates its active generation.
            # A malformed new header has not superseded the validated pending
            # header, so it cannot revoke that generation's eventual publish.
            if raw and raw[0] == c.MAP_PARTIAL:
                self._map_pending = None
            raise

    async def _handle_map_inner(self, cur: Cursor, raw: bytes) -> None:
        if len(raw) > c.MAP2_ENVELOPE_MAX:
            raise ProtocolError("MAP2 envelope exceeds server limit")
        update=cur.u8()
        if update not in (c.MAP_SAME,c.MAP_NEW,c.MAP_CONNECTED,c.MAP_PARTIAL):
            raise ProtocolError("unknown MAP2 update")
        if update == c.MAP_PARTIAL:
            pending=self._map_pending
            if pending is None: raise ProtocolError("unsolicited MAP2 continuation")
            m, total, sequence, px, py, sublayer, declared, old_path = pending
        else:
            m=copy.deepcopy(self.state.map); old_path=m.path
            # A new generation's animations are its own.
            m.animations=[]
            physical_width = physical_height = None
            if update != c.MAP_SAME:
                m.name,m.music,m.weather=self._map_string(cur,4095),self._map_string(cur,4095),self._map_string(cur,255)
                if cur.u8()>1 or cur.u8()>1: raise ProtocolError("invalid MAP2 metadata")
                m.region,m.region_longname=self._map_string(cur,255),self._map_string(cur,255)
                m.raw_path=self._map_string(cur,4095); m.path=logical_map_path(m.raw_path)
                if update == c.MAP_NEW:
                    map_width,map_height=cur.u8(),cur.u8()
                    if not map_width or not map_height: raise ProtocolError("invalid MAP2 dimensions")
                    physical_width, physical_height = map_width, map_height
                    m.level_tiles={0:{}}; m.tiles=m.level_tiles[0]
                else:
                    tile,xoff,yoff,zoff=cur.u8(),cur.i8(),cur.i8(),cur.i8()
                    if not 1<=tile<=10 or (tile==9 and zoff!=1) or (tile==10 and zoff!=-1) or (tile<9 and zoff): raise ProtocolError("invalid MAP2 connected update")
                    m.scroll(xoff,yoff); m.scroll_depths(zoff)
        px,py,sublayer=cur.u8(),cur.u8(),cur.u8(); marker=cur.u16()
        if sublayer>=7: raise ProtocolError("invalid MAP2 sublayer")
        if (update == c.MAP_NEW and
                (px >= physical_width or py >= physical_height)):
            raise ProtocolError("MAP2 player position outside map")
        if update == c.MAP_SAME:
            m.scroll(px - m.world_x, py - m.world_y)
        if marker & c.MAP2_CONTINUATION_TIMED_LIGHT:
            # The timed-light descriptor was retired in protocol 1085.
            raise ProtocolError("reserved MAP2 marker bit")
        if update == c.MAP_PARTIAL:
            if not marker or marker != sequence or (px,py,sublayer)!=(pending[3],pending[4],pending[5]): raise ProtocolError("invalid MAP2 continuation")
        elif marker>4096: raise ProtocolError("too many MAP2 continuations")
        count=cur.u8()
        if not 1<=count<=13: raise ProtocolError("invalid MAP2 level count")
        depths=set()
        for _ in range(count):
            depth,size=cur.i8(),cur.u32()
            if (not -6<=depth<=6 or depth in depths or
                    size > c.MAP2_LEVEL_PAYLOAD_MAX or size > cur.remaining or
                    (update==c.MAP_PARTIAL and not size)):
                raise ProtocolError("invalid MAP2 level")
            depths.add(depth); self._map_level(cur,size,m,depth)
        if cur.remaining: raise ProtocolError("MAP2 trailing bytes")
        if update == c.MAP_PARTIAL:
            if not depths <= declared: raise ProtocolError("undeclared MAP2 continuation depth")
            if sequence == total:
                self._map_pending=None; self.state.map=m
            else:
                self._map_pending=(m,total,sequence+1,px,py,sublayer,declared,old_path); return
        else:
            if 0 not in depths: raise ProtocolError("MAP2 full update missing depth zero")
            # A fully validated complete header supersedes an interrupted
            # generation; late partials from it cannot publish stale state.
            self._map_pending = None
            m.world_x,m.world_y,m.player_sub_layer=px,py,sublayer
            m.player_x,m.player_y=m.width//2,m.height//2
            # Full headers declare the active set; stale linked levels lose authority.
            m.level_tiles={d:m.level_tiles.get(d,{}) for d in depths}; m.tiles=m.level_tiles[0]
            if marker:
                self._map_pending=(m,marker,1,px,py,sublayer,depths,old_path); return
            self.state.map=m
        self._publish_tile_animations(m)
        # The owner watch's frame of this generation (owner_watch.py):
        # behind its flag, never raises.
        owner_watch_capture(self, m)
        self._note_position(m)
        self._observe_map_change(old_path, m)
        self._observe(lambda recorder: recorder.note_position(self.state))
        # Movement arrives in map packets, between stats packets and polls.
        self.progress_watch.observe(self.state)
        self.state.food_watch.observe(self.state)
        await self.emit("map", {"path": m.path, "name": m.name, "x": m.world_x, "y": m.world_y, "changed_map": old_path != m.path})

    async def _handle_interface(self, cur: Cursor, raw: bytes) -> None:
        if not raw:
            self.state.interface = None
            await self.emit("interface_closed", None)
            return
        old = self.state.interface
        interface = InterfaceState()
        texts: list[str] = []
        while cur.remaining:
            kind = cur.u8()
            if kind == c.IF_TEXT:
                texts.append(cur.cstring())
            elif kind == c.IF_LINK:
                interface.links.append(cur.cstring())
            elif kind == c.IF_ICON:
                cur.cstring()
            elif kind == c.IF_TITLE:
                interface.title = cur.cstring()
            elif kind == c.IF_INPUT:
                interface.input_text = cur.cstring()
            elif kind == c.IF_INPUT_PREPEND:
                interface.input_prepend = cur.cstring()
            elif kind in (c.IF_ALLOW_TAB, c.IF_INPUT_CLEANUP_DISABLE,
                          c.IF_INPUT_ALLOW_EMPTY, c.IF_SCROLL_BOTTOM):
                pass
            elif kind == c.IF_AUTOCOMPLETE:
                interface.autocomplete = cur.cstring()
            elif kind == c.IF_RESTORE:
                interface = old or interface
            elif kind == c.IF_APPEND_TEXT:
                texts.append(cur.cstring())
            elif kind == c.IF_ANIM:
                cur.u16(); cur.u8(); cur.u8()
            elif kind == c.IF_OBJECT:
                flags, tag = cur.u16(), cur.u32()
                item = Item(tag)
                self._decode_item_fields(cur, flags, item)
                interface.objects.append(item)
            else:
                raise ProtocolError(f"unknown interface field {kind}")
        interface.text = "".join(texts)
        self.state.interface = interface
        if update_bank_balance(self.state, interface.text):
            self.set_bank_balance(self.state.bank_balance)
        await self.emit("interface", interface)

    async def _handle_book(self, cur: Cursor, raw: bytes) -> None:
        self.state.books.append(raw)
        del self.state.books[:-20]
        self._observe(lambda recorder: recorder.on_book(self.state, raw))
        if b"Quest List" in raw or b"No quests to speak of" in raw:
            # Quest catalog loading depends on verified content tooling. Keep
            # ordinary client imports and non-book protocol handling usable
            # before callers configure that content provider.
            from .quests import parse_quest_book
            self.state.quests = parse_quest_book(raw)
            self.state.quests_loaded = True
            await self.emit("quests", self.state.quests)
        await self.emit("book", raw)

    async def _handle_keepalive(self, cur: Cursor, raw: bytes) -> None:
        """The server's echo of her keepalive (socket_command_keepalive):
        record when it came and its round trip. Never answered: the server
        echoes every keepalive with an id, so an answer would ping-pong."""
        if cur.remaining < 4:
            return
        ident = cur.u32()
        now = time.monotonic()
        self._last_keepalive = now
        sent = self._keepalive_sent.pop(ident, None)
        if sent is not None:
            self.keepalive_latency = now - sent

    async def select_character(self, name: str) -> None:
        await self.send(Packet(c.S_ACCOUNT).u8(c.ACCOUNT_LOGIN_CHAR).string(name))
        self._character_sent = True
        self.state.phase = "character_login"

    async def create_character(self, name: str, archetype: str) -> None:
        if not name:
            raise ValueError("character name is required to create a character")
        await self.send(Packet(c.S_ACCOUNT).u8(c.ACCOUNT_NEW_CHAR).string(name).string(archetype))
        self.state.phase = "character_create"

    async def command(self, command: str) -> None:
        self.record_action("command", command)
        await self.send(Packet(c.S_PLAYER_CMD).string(command))

    async def move(self, direction: int, run: bool = False) -> None:
        if direction not in range(0, 9):
            raise ValueError("direction must be 0..8")
        self.record_action("retreat" if run else "step",
                           f"direction={direction}")
        self._step_sent_at = time.monotonic()
        self._observe(lambda recorder: recorder.note_move(self.state, direction))
        # Epoch zero keeps ordinary direct actions outside keyboard replacement.
        await self.send(Packet(c.S_MOVE).u8(direction).u8(int(run)).u32(0))

    async def move_to_view(self, x: int, y: int) -> None:
        self.record_action("pathfind", f"view={x},{y}")
        self._step_sent_at = time.monotonic()
        await self.send(Packet(c.S_MOVE_PATH).u8(x).u8(y))

    async def clear_actions(self) -> None:
        """Cancel buffered commands, queued click paths and persistent run."""
        self.record_action("clear", "cancel queued movement/actions")
        await self.send(Packet(c.S_CLEAR))

    async def target(self, x: int, y: int, actor_count: int,
                     relative_depth: int = 0) -> None:
        if not -c.TARGET_MAX_RELATIVE_DEPTH <= relative_depth <= c.TARGET_MAX_RELATIVE_DEPTH:
            raise ValueError(
                f"relative_depth must be -{c.TARGET_MAX_RELATIVE_DEPTH}.."
                f"{c.TARGET_MAX_RELATIVE_DEPTH}")
        if actor_count <= 0:
            raise ValueError("actor_count must be nonzero")
        self.record_action(
            "target",
            f"actor_count={actor_count} view={x},{y} depth={relative_depth}")
        await self.send(Packet(c.S_TARGET).u8(c.TARGET_MAPXY).u8(x).u8(y)
                        .i8(relative_depth).u32(actor_count))

    async def clear_target(self) -> None:
        self.record_action("target", "clear")
        self._reset_target_identity()
        await self.send(Packet(c.S_TARGET).u8(c.TARGET_CLEAR))

    async def set_combat(self, enabled: bool, force: bool = False) -> None:
        if not enabled:
            # A deliberate combat-off (a Stop, a handover, a retreat) wins
            # over a heal's pending restore; the heal's own wait sets it
            # again after this (tasks.SafetyPolicy._heal).
            self.heal_combat_force = None
        self.record_action("combat",
                           f"{'on' if enabled else 'off'}{' forced' if force else ''}")
        await self.send(Packet(c.S_COMBAT).u8(int(enabled)).u8(int(force)))

    def _note_position(self, m: MapState) -> None:
        """When a map update last moved her (``_spell_cast_uncertain``); map
        packets also come when only monsters move, which is no move."""
        position = (m.path, m.world_x, m.world_y)
        if position != self._position:
            self._position, self._position_at = position, time.monotonic()

    def _note_spell_cast(self, item: Item | None) -> None:
        """Remember where a spell, scroll, rod or wand was used, and after
        which message, so a "Something blocks your spellcasting." answer
        marks that square, not wherever she has stepped since."""
        if item is not None and item.item_type in MAGIC_CAST_TYPES:
            m = self.state.map
            self.spell_cast = ((m.path, m.world_x, m.world_y),
                               self.state.message_count, time.time())
            # A step sent since a map update last moved her may land in the
            # same server tick, before the cast: either square is possible.
            self._spell_cast_uncertain = self._step_sent_at > self._position_at

    async def fire(self, direction: int, item_tag: int = 0) -> None:
        item = self.state.items.get(item_tag)
        self._note_spell_cast(item)
        if self.metrics is not None:
            self.metrics.fire(item)
        self.record_action("fire",
                           f"{item.name if item else item_tag or 'readied item'} direction={direction}")
        # The tag is required even when firing the readied item (tag zero).
        await self.send(Packet(c.S_FIRE).u8(direction).u32(item_tag).u32(0))

    async def apply(self, tag: int, apply_action: int = 0) -> None:
        item = self.state.items.get(tag)
        self._note_spell_cast(item)
        if self.metrics is not None:
            self.metrics.apply(item)
        self.record_action("apply", item.name if item else str(tag or "below"))
        self._observe(lambda recorder: recorder.note_apply(self.state, item))
        packet = Packet(c.S_ITEM_APPLY).u32(max(0, tag))
        if tag <= 0:
            packet.u8(apply_action)
        await self.send(packet)

    async def examine(self, tag: int) -> None:
        item = self.state.items.get(tag)
        self._observe(lambda recorder: recorder.note_examine(self.state, item))
        await self.send(Packet(c.S_ITEM_EXAMINE).u32(tag))

    async def move_item(self, destination: int, tag: int, quantity: int = 0) -> None:
        item = self.state.items.get(tag)
        if self.metrics is not None:
            self.metrics.item_moved(item, destination)
        self.record_action("move item",
                           f"{item.name if item else tag} x{quantity or 'all'} -> {destination}")
        await self.send(Packet(c.S_ITEM_MOVE).u32(destination).u32(tag).u32(quantity))

    async def lock_item(self, tag: int) -> None:
        await self.send(Packet(c.S_ITEM_LOCK).u32(tag))

    async def mark_item(self, tag: int) -> None:
        await self.send(Packet(c.S_ITEM_MARK).u32(tag))

    async def request_quests(self) -> None:
        await self.send(Packet(c.S_QUESTLIST))

    async def talk(self, message: str, npc_name: str | None = None) -> None:
        self.record_action("talk", f"{npc_name or 'nearby NPC'}: {message}")
        packet = Packet(c.S_TALK)
        if npc_name:
            packet.u8(c.TALK_NPC_NAME).string(npc_name)
        else:
            packet.u8(c.TALK_NPC)
        packet.string(message)
        await self.send(packet)

    async def talk_to_target(self, x: int, y: int, relative_depth: int,
                             actor_count: int, message: str) -> None:
        """Talk to one currently published MAP2 actor without selecting it."""
        if any(not isinstance(value, int) or isinstance(value, bool)
               for value in (x, y, relative_depth, actor_count)):
            raise TypeError("target coordinates, depth, and actor_count must be integers")
        game_map = self.state.map
        if not (0 <= x < game_map.width and 0 <= y < game_map.height):
            raise ValueError("target coordinates are outside the map viewport")
        if not -c.MAP2_MAX_DEPTH <= relative_depth <= c.MAP2_MAX_DEPTH:
            raise ValueError(
                f"relative_depth must be -{c.MAP2_MAX_DEPTH}..{c.MAP2_MAX_DEPTH}")
        if not 0 < actor_count <= 0xFFFFFFFF:
            raise ValueError("actor_count must be a nonzero uint32")
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        encoded_message = message.encode("utf-8")
        if (not encoded_message or b"\0" in encoded_message or
                len(encoded_message) > c.TALK_NPC_TARGET_MESSAGE_MAX):
            raise ValueError("message must be nonempty, NUL-free, and within the TALK limit")
        self.record_action(
            "talk",
            f"actor_count={actor_count} view={x},{y} depth={relative_depth}: {message}")
        await self.send(Packet(c.S_TALK).u8(c.TALK_NPC_TARGET).u8(x).u8(y)
                        .i8(relative_depth).u32(actor_count).string(message))

    async def talk_to_item(self, talk_type: int, tag: int, message: str) -> None:
        if talk_type not in (c.TALK_INV, c.TALK_BELOW, c.TALK_CONTAINER):
            raise ValueError("invalid item talk type")
        await self.send(Packet(c.S_TALK).u8(talk_type).u32(tag).string(message))

    async def execute_client_command(self, command: str) -> None:
        """Execute commands that the graphical client normally intercepts."""
        if self.metrics is not None:
            self.metrics.command(command)
        match = re.fullmatch(r'/talk\s+1\s+(.+)', command, re.S)
        if match:
            await self.talk(match.group(1))
            return
        match = re.fullmatch(r'/talk\s+5\s+"([^"]+)"\s+(.+)', command, re.S)
        if match:
            await self.talk(match.group(2), match.group(1))
            return
        match = re.fullmatch(r"/talk\s+([234])\s+(\d+)\s+(.+)", command, re.S)
        if match:
            await self.talk_to_item(int(match.group(1)), int(match.group(2)),
                                    match.group(3))
            return
        await self.command(command)

    async def choose_interface_link(self, index: int) -> None:
        interface = self.state.interface
        if interface is None or not 0 <= index < len(interface.links):
            raise IndexError("interface link does not exist")
        markup = interface.links[index]
        match = re.search(r"\[a=([^:\]]*):([^\]]*)\](.*?)\[/a\]", markup, re.S)
        if match:
            action, destination, label = match.groups()
            if action == "close":
                await self.send(Packet(c.S_TALK).u8(c.TALK_CLOSE))
                # The graphical client dismisses close-action links locally;
                # the server's TALK_CLOSE handler only clears its conversation
                # bookkeeping and does not echo an empty interface packet.
                self.state.interface = None
                await self.emit("interface_closed", None)
            elif destination.startswith("/"):
                await self.execute_client_command(destination)
            else:
                await self.talk(destination, interface.title or None)
            return
        match = re.search(r"\[a(?:=[^\]]*)?\](.*?)\[/a\]", markup, re.S)
        if not match:
            raise ProtocolError(f"cannot decode interface link: {markup!r}")
        label = re.sub(r"\[[^]]+\]", "", match.group(1))
        if label.startswith("/"):
            await self.execute_client_command(label)
        else:
            await self.talk(label, interface.title or None)
