"""Prometheus metrics: what the bot does, for a few hours of analysis.

A tiny dependency-free registry of counters and gauges that renders the
Prometheus text exposition format 0.0.4. ``BotMetrics`` counts the client's
own observations (server messages, the action ledger, spells, potions, item
moves, chat, deaths, connections) as they happen; the gauges (vitals,
skills, money, the current task) are read from live state on each scrape.

Every label value comes from a bounded set: code-defined kinds, server-named
monsters, skills, spells and damage types, each capped at a fixed number of
distinct values with the rest folded into ``other``. Player-typed text is
never a label. Counters live in memory and start again with the process,
which Prometheus' ``rate()`` and ``increase()`` already expect.
"""

from __future__ import annotations

import functools
import logging
import math
import re
import time
from typing import Any, Callable, Iterable

from . import constants as c
from .self_improvement import _HIT as HIT_PATTERN, from_server

log = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
PREFIX = "atrinik_playtester_"
OTHER = "other"
# Characters kept of any label value.
MAX_LABEL_LENGTH = 64

# Server lines (attack.c, exp.c, shop.c, spell_effect.c). Bounded digits, so
# an oversized amount never reaches int()'s digit limit.
KILL = re.compile(r"^You killed (?P<name>.+?)(?: with (?P<weapon>.+?))?\.$", re.I)
XP = re.compile(r"^You got (?P<amount>\d{1,15}) exp in skill (?P<skill>.+?)\.$", re.I)
DEALT = re.compile(r"^You hit (?P<who>.+?) for (?P<amount>\d{1,9}) \(-?\d{1,9}\)"
                   r" with (?P<attack>.+?)\.$", re.I)
HEALED = re.compile(r"^The spell heals you for (?P<amount>\d{1,9})", re.I)
PAID = re.compile(r"^You paid (?P<cost>.+?) for (?P<what>.+)\.$", re.I)
RECEIVED = re.compile(r"^You receive (?P<cost>.+?) for (?P<what>.+)\.$", re.I)

# Time-in-state classification from the action ledger. Actions not listed
# (clear, combat off, apply, move item, command) keep the current state.
STATES = ("fighting", "resting", "travelling", "shopping", "busy", "idle", "disconnected")
FIGHTING_ACTIONS = frozenset({
    "target", "fire", "caster-orbit", "melee-kite", "melee-kite-gap",
    "melee-dodge", "melee-position", "combat-finish-window",
    "engagement-pursue", "ranged-pull-align", "pull-wait",
    "pull-wait-approach", "pull-anchor-step", "lure-to-ground",
    "farm-pack-split", "cornered-breakout", "pursued-stand",
    "emergency-disengage", "invisible-finish-hold",
})
RESTING_ACTIONS = frozenset({"training-rest", "navigation-recovery-hold"})
TRAVELLING_ACTIONS = frozenset({
    "step", "retreat", "pathfind", "approach-step", "savebed-move",
    "emergency-recall",
})
SHOPPING_ACTIONS = frozenset({
    "talk", "dialogue-stock-buy", "temple-service", "temple-food",
    "mass-identify",
})


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _number(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 2 ** 53:
        return str(int(value))
    return repr(float(value))


def clean_label(value: Any, fold: bool = True) -> str:
    """A label value: markup removed, one line, bounded length, and folded
    case unless it is a name (a character's)."""
    text = " ".join(re.sub(r"\[[^]]*\]", "", str(value or "")).split())
    text = (text.casefold() if fold else text)[:MAX_LABEL_LENGTH]
    return text or "unknown"


class LabelLimit:
    """At most ``limit`` distinct values; later new ones become ``other``."""

    def __init__(self, limit: int, fold: bool = True):
        self.limit, self.fold = limit, fold
        self.seen: set[str] = set()

    def __call__(self, value: Any) -> str:
        label = clean_label(value, self.fold)
        if label in self.seen:
            return label
        if len(self.seen) >= self.limit:
            return OTHER
        self.seen.add(label)
        return label


class Family:
    """One metric family: its samples keyed by label values."""

    def __init__(self, name: str, kind: str, help_text: str,
                 labels: tuple[str, ...]):
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", name):
            raise ValueError(f"invalid metric name {name!r}")
        for label in labels:
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", label) or label.startswith("__"):
                raise ValueError(f"invalid label name {label!r}")
        self.name, self.kind, self.help = name, kind, help_text
        self.labels = labels
        self.samples: dict[tuple[str, ...], float] = {}

    def _key(self, values: Iterable[Any]) -> tuple[str, ...]:
        key = tuple(str(value) for value in values)
        if len(key) != len(self.labels):
            raise ValueError(f"{self.name} takes labels {self.labels}")
        return key

    def inc(self, values: Iterable[Any] = (), amount: float = 1.0) -> None:
        if self.kind == "counter" and amount < 0:
            raise ValueError("a counter never decreases")
        key = self._key(values)
        self.samples[key] = self.samples.get(key, 0.0) + amount

    def set(self, values: Iterable[Any], value: float) -> None:
        self.samples[self._key(values)] = float(value)

    def value(self, values: Iterable[Any] = ()) -> float:
        return self.samples.get(self._key(values), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help)}",
                 f"# TYPE {self.name} {self.kind}"]
        for key, value in self.samples.items():
            labels = ",".join(f'{name}="{_escape_label(part)}"'
                              for name, part in zip(self.labels, key))
            lines.append(f"{self.name}{{{labels}}} {_number(value)}"
                         if labels else f"{self.name} {_number(value)}")
        return lines


class Registry:
    def __init__(self) -> None:
        self.families: dict[str, Family] = {}

    def _add(self, name: str, kind: str, help_text: str,
             labels: tuple[str, ...]) -> Family:
        if name in self.families:
            raise ValueError(f"duplicate metric {name}")
        family = self.families[name] = Family(name, kind, help_text, labels)
        return family

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Family:
        return self._add(name, "counter", help_text, labels)

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Family:
        return self._add(name, "gauge", help_text, labels)

    def render(self) -> str:
        lines: list[str] = []
        for family in self.families.values():
            lines.extend(family.render())
        return "\n".join(lines) + "\n"


BASE = ("character", "server")


def _never_raises(method: Callable) -> Callable:
    """Telemetry must never stop play: an observation that fails is dropped."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            log.exception("metrics: could not record %s", method.__name__)
            return None
    return wrapper


class BotMetrics:
    """The bot's activity counters, and its gauges read at scrape time."""

    def __init__(self, client: Any):
        self.client = client
        self.engine: Any = None
        self.registry = registry = Registry()
        self.characters = LabelLimit(16)
        self.monsters = LabelLimit(300)
        self.skills = LabelLimit(64)
        self.attacks = LabelLimit(100)
        self.damage_types = LabelLimit(32)
        self.spells = LabelLimit(100)
        self.potions = LabelLimit(64)
        self.actions = LabelLimit(200)
        self.causes = LabelLimit(32)
        self.tasks = LabelLimit(64, fold=False)
        self.phases = LabelLimit(64)
        self._connected_before = False
        # Nothing is played before the first connection.
        self._state = "disconnected"
        self._state_since = time.monotonic()

        def counter(name: str, help_text: str, *labels: str) -> Family:
            return registry.counter(PREFIX + name, help_text, BASE + labels)

        def gauge(name: str, help_text: str, *labels: str) -> Family:
            return registry.gauge(PREFIX + name, help_text, BASE + labels)

        # Gauges, refreshed from live state on every scrape.
        self.connected = gauge("connected", "1 while playing on the game server.")
        self.vitals = {key: gauge(key, f"Current {key}.")
                       for key in ("hp", "maxhp", "sp", "maxsp", "food")}
        self.level = gauge("level", "Character level.")
        self.experience = gauge("experience", "Total character experience.")
        self.regen = {key: gauge(key, f"Server-reported {key.replace('_', ' ')} per second.")
                      for key in ("regen_hp", "regen_sp")}
        self.skill_level = gauge("skill_level", "Level of each known skill.", "skill")
        self.skill_experience = gauge("skill_experience", "Experience of each known skill.",
                                      "skill")
        self.money_carried = gauge("money_carried_copper", "Coins carried, in copper.")
        self.money_banked = gauge("money_banked_copper",
                                  "Last known bank balance, in copper.")
        self.depletion = gauge("depletion_points", "Depletion points counted since the "
                               "depletion force appeared.")
        self.predicted_xp = gauge(
            "farm_predicted_xp_per_hour",
            "The farming analyzer's estimated XP per hour at the spot it ranks first.",
            "objective")
        self.task_info = gauge("task_info", "1 for the running top-level task type.", "task")
        self.phase_info = gauge("phase_info", "1 for the current autoplay phase.", "phase")

        # Counters, from what the client observes and sends.
        self.kills = counter("kills_total", "Kills reported by the server, by monster.",
                             "monster")
        self.xp_gained = counter("experience_gained_total", "Experience gained, by skill.",
                                 "skill")
        self.damage_dealt = counter(
            "damage_dealt_total", "Damage dealt, by attack type or hitting skill and spell.",
            "attack")
        self.hits_dealt = counter(
            "hits_dealt_total",
            "Damaging hit lines, by attack: one per attack type of each swing.", "attack")
        self.damage_taken = counter("damage_taken_total", "Damage taken, by damage type.",
                                    "type")
        self.hits_taken = counter(
            "hits_taken_total",
            "Damaging hit lines taken, by damage type: one per attack type.", "type")
        self.spells_cast = counter("spells_cast_total", "Spell casts ordered, by spell.", "spell")
        self.mana_spent = counter(
            "mana_spent_total", "Spell points for ordered casts, from each spell's listed cost.")
        self.heals = counter("heals_total",
                             "Her own heals: healing spells on herself, healing potions.",
                             "source")
        self.healed = counter("healed_hp_total",
                              "Health restored by her healing spells on herself.")
        self.potions_used = counter("potions_used_total", "Potions applied, by potion.",
                                    "potion")
        self.deaths = counter("deaths_total", "Deaths, by post-mortem cause.", "cause")
        self.rests = counter("rests_total", "Rests started to regain spell points or health.")
        self.actions_total = counter("actions_total", "Actions and decisions recorded, by kind.",
                                     "kind")
        self.purchases = counter("purchases_total", "Purchases paid for.")
        self.sales = counter("sales_total", "Items sold.")
        self.money_spent = counter("money_spent_copper_total", "Money spent, in copper.",
                                   "reason")
        self.money_gained = counter("money_gained_copper_total", "Money received, in copper.",
                                    "source")
        self.item_moves = counter(
            "item_moves_total",
            "Item moves ordered: store, retrieve, loot (with /take), pickup or drop.",
            "direction")
        self.chat_lines = counter("chat_lines_total", "Player chat lines, by direction "
                                  "and channel.", "direction", "channel")
        self.connections = counter("connections_total", "Connections to the game server.")
        self.reconnects = counter("reconnects_total",
                                  "Connections after the first in this process.")
        self.disconnects = counter("disconnects_total", "Connections or connection attempts that failed.")
        self.tasks_started = counter("tasks_started_total", "Top-level tasks started, by type.",
                                     "task")
        self.tasks_finished = counter("tasks_finished_total",
                                      "Top-level tasks ended, by type and status.",
                                      "task", "status")
        self.state_seconds = counter(
            "state_seconds_total",
            "Seconds spent fighting, resting, travelling, shopping, idle or disconnected.",
            "state")

    def attach(self, engine: Any) -> None:
        """The task engine: task types, and idle time when no task runs."""
        self.engine = engine

    def base(self) -> tuple[str, str]:
        client = self.client
        state, config = getattr(client, "state", None), getattr(client, "config", None)
        character = (getattr(state, "player_name", "") or
                     getattr(config, "character", "") or "unknown")
        server = f"{getattr(config, 'host', '')}:{getattr(config, 'port', '')}"
        return self.characters(character), clean_label(server)

    # Time in state.

    def _classify(self, action: str = "", detail: str = "") -> str:
        state = getattr(self.client, "state", None)
        if getattr(state, "phase", "") != "playing":
            return "disconnected"
        if self.engine is not None and getattr(self.engine, "task", None) is None:
            return "idle"
        if action == "combat" and detail.startswith("on"):
            return "fighting"
        if action == "target" and detail == "clear":
            return self._kept_state()
        if action in RESTING_ACTIONS:
            return "resting"
        if action in FIGHTING_ACTIONS:
            return "fighting"
        if action in TRAVELLING_ACTIONS or action.startswith("navigation-"):
            return "travelling"
        if action in SHOPPING_ACTIONS or action.startswith("shop-"):
            return "shopping"
        return self._kept_state()

    def _kept_state(self) -> str:
        # A running task stays in its last activity; one that has done
        # nothing classified yet (dialogue, quest steps) is busy.
        if self._state not in ("idle", "disconnected"):
            return self._state
        return "busy" if self.engine is not None else "idle"

    def _accrue(self, action: str = "", detail: str = "") -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self._state_since)
        self._state_since = now
        self.state_seconds.inc((*self.base(), self._state), elapsed)
        self._state = self._classify(action, detail)

    # Observations.

    @_never_raises
    def action(self, action: str, detail: str = "") -> None:
        """One record_action entry: its kind, and what it says about state."""
        base = self.base()
        self.actions_total.inc((*base, self.actions(action)))
        if action == "training-rest":
            self.rests.inc(base)
        elif action in ("task-start", "task-end"):
            task = getattr(self.engine, "task", None)
            name = self.tasks(type(task).__name__ if task is not None else "unknown")
            if action == "task-start":
                self.tasks_started.inc((*base, name))
            else:
                status = getattr(getattr(task, "status", None), "value", "unknown")
                self.tasks_finished.inc((*base, name, clean_label(status)))
        self._accrue(action, str(detail))

    @_never_raises
    def on_event(self, event: Any) -> None:
        """A client event handler: connections and server messages."""
        kind, data = event.kind, event.data
        base = self.base()
        if kind == "connected":
            self.connections.inc(base)
            if self._connected_before:
                self.reconnects.inc(base)
            self._connected_before = True
        elif kind == "disconnected":
            self.disconnects.inc(base)
            # Emitted before the client marks the phase: the gap until the
            # reconnect is offline time, whatever she was doing.
            self._accrue()
            self._state = "disconnected"
            return
        elif kind == "message" and isinstance(data, tuple) and len(data) == 3:
            self.message(data[0], data[2])
        self._accrue()

    def message(self, msg_type: int, text: str) -> None:
        """Count what a server line reports; player chat never counts."""
        if not from_server((0.0, msg_type, "", text)):
            return
        line = str(text).strip()
        base = self.base()
        if match := DEALT.match(line):
            attack, amount = self.attacks(match.group("attack")), int(match.group("amount"))
            self.damage_dealt.inc((*base, attack), amount)
            if amount:
                # An immune attack type still reports a line "for 0".
                self.hits_dealt.inc((*base, attack))
        elif match := HIT_PATTERN.match(line):
            kind = self.damage_types(match.group("type") or "physical")
            amount = int(match.group("amount"))
            self.damage_taken.inc((*base, kind), amount)
            if amount:
                self.hits_taken.inc((*base, kind))
        elif match := KILL.match(line):
            self.kills.inc((*base, self.monsters(match.group("name"))))
        elif match := XP.match(line):
            self.xp_gained.inc((*base, self.skills(match.group("skill"))),
                               int(match.group("amount")))
        elif match := HEALED.match(line):
            self.heals.inc((*base, "spell"))
            self.healed.inc(base, int(match.group("amount")))
        elif (match := PAID.match(line)) or (match := RECEIVED.match(line)):
            from .client import parse_money_value

            value = parse_money_value(match.group("cost"))
            if value is None:
                return
            if line[:8].casefold() == "you paid":
                self.purchases.inc(base)
                self.money_spent.inc((*base, "purchase"), value)
            else:
                self.sales.inc(base)
                self.money_gained.inc((*base, "sale"), value)

    @_never_raises
    def fire(self, item: Any) -> None:
        """A FIRE order: a spell cast when the fired object is a spell."""
        if item is None or getattr(item, "item_type", 0) != c.TYPE_SPELL:
            return
        base = self.base()
        self.spells_cast.inc((*base, self.spells(item.name)))
        try:
            cost = max(0, int(item.extra.get("cost", 0) or 0))
        except (TypeError, ValueError):
            cost = 0
        self.mana_spent.inc(base, cost)

    @_never_raises
    def apply(self, item: Any) -> None:
        if item is not None and getattr(item, "item_type", 0) == c.TYPE_POTION:
            base = self.base()
            self.potions_used.inc((*base, self.potions(item.name)))
            if "heal" in item.name.casefold():
                self.heals.inc((*base, "potion"))

    @_never_raises
    def item_moved(self, item: Any, destination: int) -> None:
        state = self.client.state
        if destination == 0:
            direction = "drop"
        elif destination != state.player_tag:
            direction = "store"
        else:
            location = getattr(item, "location", 0) if item is not None else 0
            source = (state.items.get(location)
                      if location not in (0, state.player_tag) else None)
            if source is None:
                direction = "pickup"
            elif "corpse" in source.name.casefold():
                direction = "loot"
            else:
                direction = "retrieve"
        self.item_moves.inc((*self.base(), direction))

    @_never_raises
    def command(self, command: str) -> None:
        """A client command: corpse looting goes through /take."""
        if str(command).strip().casefold().startswith("/take"):
            self.item_moves.inc((*self.base(), "loot"))

    @_never_raises
    def chat(self, msg_type: int, direction: str) -> None:
        channel = {c.CHAT_TYPE_CHAT: "chat", c.CHAT_TYPE_LOCAL: "say",
                   c.CHAT_TYPE_PRIVATE: "tell", c.CHAT_TYPE_PARTY: "party"}.get(
                       msg_type, OTHER)
        self.chat_lines.inc((*self.base(), "out" if direction == "out" else "in", channel))

    @_never_raises
    def death(self, cause: Any) -> None:
        self.deaths.inc((*self.base(), self.causes(cause or "unclassified")))

    # Scrape.

    def render(self, *, carried: int | None = None, banked: int | None = None,
               predicted_xp: tuple[str, float] | None = None) -> str:
        """The exposition text, with the gauges read from live state now."""
        self._accrue()
        client, base = self.client, self.base()
        state = client.state
        for family in (self.connected, self.level, self.experience, self.skill_level,
                       self.skill_experience, self.money_carried, self.money_banked,
                       self.depletion, self.predicted_xp, self.task_info, self.phase_info,
                       *self.vitals.values(), *self.regen.values()):
            # Gauges describe now: an earlier character's or task's are gone.
            family.samples.clear()
        self.connected.set(base, 1 if state.phase == "playing" else 0)
        stats = state.stats
        for key, family in (*self.vitals.items(), *self.regen.items()):
            if key in stats:
                family.set(base, float(stats.get(key) or 0))
        if "level" in stats:
            self.level.set(base, int(stats.get("level") or 0))
        if "exp" in stats:
            self.experience.set(base, int(stats.get("exp") or 0))
        for item in state.inventory:
            if item.item_type == c.TYPE_SKILL:
                skill = self.skills(item.name)
                self.skill_level.set((*base, skill), int(item.extra.get("level", 0) or 0))
                self.skill_experience.set((*base, skill),
                                          int(item.extra.get("experience", 0) or 0))
        if carried is not None:
            self.money_carried.set(base, carried)
        if banked is not None:
            self.money_banked.set(base, banked)
        if getattr(state, "depletion_points_known", False):
            self.depletion.set(base, int(state.depletion_points or 0))
        if predicted_xp is not None:
            self.predicted_xp.set((*base, clean_label(predicted_xp[0])), predicted_xp[1])
        task = getattr(self.engine, "task", None)
        if task is not None:
            self.task_info.set((*base, self.tasks(type(task).__name__)), 1)
            phase = getattr(task, "phase", "")
            if phase and isinstance(phase, str):
                self.phase_info.set((*base, self.phases(phase)), 1)
        return self.registry.render()
