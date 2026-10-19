"""Async headless Atrinik client for the current binary socket protocol."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import constants as c
from .access_codes import normalize_access_code
from .farm_analyzer import DEFAULT_OBJECTIVE, OBJECTIVES
from .model import (STARTER_APARTMENT, STARTER_APARTMENT_BED, Character, GameState,
                    InterfaceState, Item, MapObject, MapState)
from .alerts import ProgressWatch
from .memory import BotMemory
from . import post_mortem
from .protocol import Cursor, Event, Packet, ProtocolError, decompress_frame, read_frame
from .transport import QuicStream

log = logging.getLogger(__name__)
EventHandler = Callable[[Event], Awaitable[None] | None]

COIN_VALUES = {
    "copper": 1, "silver": 100, "gold": 10_000,
    "jade": 1_000_000, "mithril": 10_000_000,
    "amber": 100_000_000,
}


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


# kill_player (server types/player.c) sends exactly one of these, as the whole
# message, per defeat: a PvP-area defeat, or a death after its stat losses.
# Matching the whole text keeps a player's chat that quotes one from passing
# for a death.
DEATH_NOTICES = ("you have been defeated in combat!", "you have died.")

class AtrinikClient:
    BANK_BALANCE_CACHE_SECONDS = 6 * 60 * 60

    """Maintains a live state model and exposes typed game actions."""

    def __init__(self, config: ClientConfig):
        self.config = config
        self.state = GameState(account=config.account)
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
        self._last_keepalive = 0.0
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
        self.decision_history: list[dict] = []
        # Health, SP and food samples, and the deaths reviewed from them.
        self.vitals: list[list[float]] = []
        self.post_mortems: list[dict] = []
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

    def load_character_memory(self, character: str) -> None:
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
        # Newest first, stopping at KEEP valid reports: an oversized stored
        # list is never normalized whole.
        deaths = memory.load(character, "post_mortems", [])
        kept: list[dict] = []
        for value in reversed(deaths if isinstance(deaths, list) else []):
            if len(kept) == post_mortem.KEEP:
                break
            if (restored := post_mortem.restore(value)) is not None:
                kept.append(restored)
        self.post_mortems = kept[::-1]
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
                self.state.last_utility_shop_check_at = max(
                    0.0, float(record.get(
                        "last_utility_shop_check_at", 0.0)))
            except (TypeError, ValueError):
                self.state.last_utility_shop_check_at = 0.0
            try:
                self.state.paid_restoration_retry_until = max(
                    0.0, float(record.get(
                        "paid_restoration_retry_until", 0.0)))
            except (TypeError, ValueError):
                self.state.paid_restoration_retry_until = 0.0
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
            try:
                self.state.last_bank_deposit_at = max(
                    0.0, float(record.get("last_bank_deposit_at", 0.0)))
            except (TypeError, ValueError):
                self.state.last_bank_deposit_at = 0.0
            try:
                observed_at = max(
                    0.0, float(record.get("bank_balance_observed_at", 0.0)))
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
            self.state.last_utility_shop_check_at = 0.0
            self.state.paid_restoration_retry_until = 0.0
            self.state.vendor_quotes = {}
            self.state.farm_zone_quarantine = {}
            self.state.farm_zone_last_checked = {}
            self.state.lore_attempt_counts = {}
            self.state.apartment_bed_bound = False
            self.state.savebed_map = ""
            self.state.savebed_xy = None
            self.state.apartment_returns = {}
            self.state.farm_objective = DEFAULT_OBJECTIVE
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

    def _memory_store(self) -> BotMemory:
        if self.memory is None:
            self.memory = BotMemory(
                self._memory_path, server=self._memory_server_key,
                account=self.config.account)
        return self.memory

    def set_depletion_points(self, points: int) -> None:
        self.state.depletion_points = max(0, int(points))
        self.state.depletion_points_known = True
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
        self.state.farm_zone_quarantine[path] = max(now, float(until))
        character = self.state.player_name or self.config.character
        if not character:
            return
        self._runtime_memory.setdefault(character, {})[
            "farm_zone_quarantine"] = dict(
                self.state.farm_zone_quarantine)
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
        self.state.bank_balance_observed_at = max(
            0.0, float(time.time() if observed_at is None else observed_at))
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

    def record_action(self, action: str, detail: str = "") -> None:
        """Keep a bounded, map-aware history of orders sent by automation."""
        m = self.state.map
        record = {
            "time": time.time(), "action": action, "detail": detail,
            "task": self.action_context, "map": m.path,
            "x": m.world_x, "y": m.world_y,
        }
        self.action_history.append(record)
        del self.action_history[:-500]
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
            if character:
                self._memory_store().save(
                    character, "decisions", self.decision_history)

    def _record_post_mortem(self) -> None:
        """Keep what happened in the minute before this death.

        kill_player queues its notice before the savebed transfer's map update,
        which goes out at the end of the server tick, so the square she is on
        now is where she fell. The report is built once, here, and ends at the
        notice: what follows a death (a respawn, the next fight) never belongs
        to it.
        """
        previous = self.post_mortems[-1]["time"] if self.post_mortems else 0.0
        m = self.state.map
        self.post_mortems.append(post_mortem.build(
            time.time(), m.path, m.world_x, m.world_y, self.vitals,
            self.state.messages, self.action_history, self.decision_history,
            since=previous, name=self.state.player_name or self.config.character))
        del self.post_mortems[:-post_mortem.KEEP]
        character = self.state.player_name
        if character:
            self._memory_store().save(character, "post_mortems", self.post_mortems)

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
                await self._read_loop()
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
        self.state.player_tag = cur.u32()
        weight, face = cur.u32(), cur.u32()
        self.state.player_name = cur.cstring()
        self.state.party_name = ""
        # PLAYER arrives before the full ITEM inventory packet. Preserve this
        # explicit barrier across reconnects even though the prior inventory
        # objects remain in memory for those few event-loop turns.
        self.state.inventory_replay_complete = False
        self.load_character_memory(self.state.player_name)
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
            self._party_setup_pending = ""
            self.record_action("party-ready", self.state.party_name)
            await self.emit("party", {
                "name": self.state.party_name, "joined": True})
        elif command == c.CMD_PARTY_LEAVE:
            old_name = self.state.party_name
            self.state.party_name = ""
            await self.emit("party", {"name": old_name, "joined": False})
        elif command == c.CMD_PARTY_PASSWORD:
            # Preserve defensive framing validation even though the bot does
            # not interactively enter passwords. Configured parties are open.
            await self.emit("party_password", cur.cstring())
        else:
            # LIST/WHO/UPDATE payloads belong to the graphical roster. The bot
            # needs only authoritative join/leave membership state.
            await self.emit("party_packet", (command, cur.remaining))

    async def _handle_drawinfo(self, cur: Cursor, raw: bytes) -> None:
        msg_type, color = cur.u8(), cur.cstring()
        text = cur.cstring() if cur.remaining else ""
        self.state.add_message(msg_type, color, text)
        if update_bank_balance(self.state, text):
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
        died = normalized in DEATH_NOTICES
        if died:
            self.record_action("death", text)
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
            # After the bookkeeping above, so its depletion records are in it.
            self._record_post_mortem()
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
        if msg_type == c.CHAT_TYPE_CHAT:
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
        return match.group(1).strip(), match.group(2).strip()

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

    async def _respond_to_player_chat(self, msg_type: int, text: str) -> None:
        """Answer small, verifiable social questions without chat spam."""
        sender, body = self._incoming_player_chat(msg_type, text)
        if not sender or not body:
            return
        player_name = (self.state.player_name or
                       self.config.character).casefold()
        if sender.casefold() == player_name:
            return
        folded = re.sub(r"\s+", " ", body.casefold()).strip()
        chat_now = time.monotonic()
        recent_correspondent = (
            chat_now - self._chat_correspondents.get(
                sender.casefold(), 0.0) <= 90.0)
        direct = bool(
            msg_type == c.CHAT_TYPE_PRIVATE or
            recent_correspondent or
            (player_name and re.search(
                rf"\b{re.escape(player_name)}\b", folded)))
        location_question = bool(re.search(
            r"\b(?:where\s+(?:are|r)\s+(?:you|u)|"
            r"where(?:'s|\s+is)\s+(?:sera|she)|"
            r"what\s+(?:map|zone|area)\s+are\s+you\s+(?:in|at))\b",
            folded))
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
        if location_question:
            response = f"I'm in {self._current_place_description()} right now."
        elif status_question:
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
        elif direct and (small_talk := self._small_talk_response(folded)):
            response = small_talk
        elif direct:
            # Decisions are persisted in the server/account/character-scoped
            # SQLite ledger. This is the review queue for extending Sera's
            # conversational rules from questions real players actually ask.
            self.record_action(
                "chat-unhandled",
                f"channel={msg_type} sender={sender} text={body}")
            return
        else:
            return

        prompt_key = f"{msg_type}:{sender.casefold()}:{folded}"
        now = chat_now
        if (prompt_key == self._last_chat_prompt or
                now - self._last_chat_reply_at < 12.0):
            return
        self._last_chat_prompt = prompt_key
        self._last_chat_reply_at = now
        self._chat_correspondents[sender.casefold()] = now
        if len(self._chat_correspondents) > 32:
            oldest = min(
                self._chat_correspondents,
                key=self._chat_correspondents.__getitem__)
            del self._chat_correspondents[oldest]
        clean_sender = sender.replace('"', "")
        if msg_type == c.CHAT_TYPE_PRIVATE:
            command = f'/tell "{clean_sender}" {response}'
        else:
            command = f"/chat {clean_sender}, {response}"
        await self.command(command)
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
            while self.vitals and self.vitals[0][0] < cutoff:
                self.vitals.pop(0)
        self.progress_watch.observe(self.state)
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
        if delete_env:
            location_to_clear = cur.u32()
            completes_player_replay = (
                location_to_clear == self.state.player_tag)
            for tag in list(self.state.inventories.get(location_to_clear, [])):
                self.state.remove_item(tag)
            self.state.inventories[location_to_clear] = []
            if not cur.remaining:
                if completes_player_replay:
                    self.state.inventory_replay_complete = True
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
        if completes_player_replay:
            self.state.inventory_replay_complete = True
        await self.emit("items", changed)

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
        while cur.remaining:
            tag = cur.u32()
            self.state.remove_item(tag)
            deleted.append(tag)
        await self.emit("items_deleted", deleted)

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
                        c.MAP_FLAG2_DOOR | c.MAP_FLAG2_EXIT)
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
            if values & c.MAP2_MASK_LIGHT_LEVEL: tile.darkness[0] = body.u16()
            if values & c.MAP2_MASK_LIGHT_LEVEL_MORE:
                for sub in range(1, 7): tile.darkness[sub] = body.u16()
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
                tile.objects[layer]=obj
            ext=body.u8()
            if ext & ~(c.MAP_EXT_ANIM|c.MAP_EXT_LIGHT_RGB|c.MAP_EXT_LIGHT_KEYFRAME): raise ProtocolError("unknown MAP2 tile flags")
            if ext & c.MAP_EXT_LIGHT_RGB:
                bitmap=body.u8()
                if bitmap & ~127: raise ProtocolError("invalid MAP2 rgb bitmap")
                for sub in range(7):
                    if bitmap & (1<<sub): body.u16(); body.u16(); body.u16()
            if ext & c.MAP_EXT_LIGHT_KEYFRAME:
                scalar=body.u8()
                if scalar & ~127: raise ProtocolError("invalid MAP2 keyframe bitmap")
                for sub in range(7):
                    if scalar & (1<<sub): body.u16()
                rgb=body.u8()
                if rgb & ~scalar: raise ProtocolError("invalid MAP2 keyframe rgb")
                for sub in range(7):
                    if rgb & (1<<sub): body.u16(); body.u16(); body.u16()
            if ext & c.MAP_EXT_ANIM:
                animations=body.u8(); seen_sub=set()
                if animations > 7: raise ProtocolError("too many MAP2 animations")
                for _ in range(animations):
                    sub, kind=body.u8(), body.u8(); body.i16()
                    if sub >= 7 or sub in seen_sub or kind not in (1,2): raise ProtocolError("invalid MAP2 animation")
                    seen_sub.add(sub)
            # Fog applies after the record: remembered geometry may remain,
            # but it must never remain an actionable target identity.
            if fogged:
                tile.remember_as_fog()
            else:
                tile.fogged = False
        if body.remaining: raise ProtocolError("MAP2 level trailing bytes")

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
        timed=marker & c.MAP2_CONTINUATION_TIMED_LIGHT; marker &= ~c.MAP2_CONTINUATION_TIMED_LIGHT
        if update == c.MAP_PARTIAL:
            if timed or not marker or marker != sequence or (px,py,sublayer)!=(pending[3],pending[4],pending[5]): raise ProtocolError("invalid MAP2 continuation")
        elif marker>4096: raise ProtocolError("too many MAP2 continuations")
        if timed:
            generation,start,end,flags=cur.u64(),cur.u64(),cur.u64(),cur.u8()
            if not generation or end<=start or end-start!=3600 or not flags or flags & ~3: raise ProtocolError("invalid MAP2 timed light")
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
        self._observe_map_change(old_path, m)
        # Movement arrives in map packets, between stats packets and polls.
        self.progress_watch.observe(self.state)
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
        packet = Packet(c.S_KEEPALIVE)
        if cur.remaining >= 4:
            packet.u32(cur.u32())
        await self.send(packet)

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
        # Epoch zero keeps ordinary direct actions outside keyboard replacement.
        await self.send(Packet(c.S_MOVE).u8(direction).u8(int(run)).u32(0))

    async def move_to_view(self, x: int, y: int) -> None:
        self.record_action("pathfind", f"view={x},{y}")
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
        self.record_action("combat",
                           f"{'on' if enabled else 'off'}{' forced' if force else ''}")
        await self.send(Packet(c.S_COMBAT).u8(int(enabled)).u8(int(force)))

    async def fire(self, direction: int, item_tag: int = 0) -> None:
        item = self.state.items.get(item_tag)
        self.record_action("fire",
                           f"{item.name if item else item_tag or 'readied item'} direction={direction}")
        # The tag is required even when firing the readied item (tag zero).
        await self.send(Packet(c.S_FIRE).u8(direction).u32(item_tag).u32(0))

    async def apply(self, tag: int, apply_action: int = 0) -> None:
        item = self.state.items.get(tag)
        self.record_action("apply", item.name if item else str(tag or "below"))
        packet = Packet(c.S_ITEM_APPLY).u32(max(0, tag))
        if tag <= 0:
            packet.u8(apply_action)
        await self.send(packet)

    async def examine(self, tag: int) -> None:
        await self.send(Packet(c.S_ITEM_EXAMINE).u32(tag))

    async def move_item(self, destination: int, tag: int, quantity: int = 0) -> None:
        item = self.state.items.get(tag)
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
