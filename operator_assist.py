"""Owner-triggered operator assist: an operator character hands Sera gold.

This is the one, narrow exception to "behave only through ordinary player
capabilities" (AGENTS.md). Only the owner starts it, with the one-shot
``atrinik-playtester operator-assist`` command, which posts to the running
bot's loopback dashboard. Autoplay and tasks never reach it: neither the
client nor the task engine holds a reference to it; only the dashboard
server does.

Server facts it relies on (Classic server and ``atrinik/content@main``):

- An account may play several characters at once: character login refuses
  nothing but a second session of the same character (``player_login``,
  server ``types/player.c``), and an invite code admits any number of
  connections. So Sera stays logged in, and a second connection plays the
  operator character.
- ``/tp NAME`` puts the operator on Sera's exact square
  (``commands/permission/tp.c``); ``/create goldcoin`` puts one gold coin
  in the operator's inventory (``maps/python/commands/create.py``). Both are
  permission commands: grant the operator character exactly ``tp`` and
  ``create`` (``/cmd_permission NAME add tp``, ``/cmd_permission NAME add
  create``). Never give a character this bot logs into ``[OP]`` or ``*``:
  that includes ``/console``, a server Python console.
- Logging the operator in disconnects any other session of that character
  (``player_login``), and its logout saves it on Sera's square.
- The operator drops the coin on that square and logs out; Sera picks it
  up. A drop and a pickup are journaled item moves like any other.
- The ground (look) window leaves out item types (``esrv_draw_look``,
  server ``socket/item.c``, sends no ``UPD_TYPE``), so a coin underfoot is
  known by its name alone.
- A pickup is refused unless ``carrying + weight < weight_limit[Str]``
  (``player_can_carry``, ``types/player.c``). Depletion lowers Str, so a
  depleted character may already carry more than she may pick up: the run
  is refused before anything is created.

Every run, and every refusal, is kept as an audit record in Sera's memory
(``memory.sqlite3``, kind ``operator_assist``), and a run whose coin left the
operator marks her dashboard and metrics as operator-assisted from then on.

The dashboard route also requires the owner-only operator key (see
``ensure_operator_key``), so reaching the dashboard is not enough to start
a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from . import constants as c
from .client import COIN_VALUES, AtrinikClient
from .operator_key import (KEY_HEADER, OperatorKeyError, ensure_operator_key,
                           key_matches, operator_key_path, read_operator_key)
from .tasks import unresolved_hostile_contact

log = logging.getLogger(__name__)

MEMORY_KIND = "operator_assist"
# Owner decision (2026-11-16): at most one gold coin per run, one run a day.
MAX_GOLD_PER_RUN = 1
COOLDOWN_SECONDS = 24 * 60 * 60
GOLD_ARCHETYPE = "goldcoin"
GOLD_COPPER = COIN_VALUES["gold"]
# coin_gold.arc: weight 10 (grams).
GOLD_WEIGHT_KG = 0.01
# A gold coin as the look window names it: "gold coin" or "12 gold coins".
GROUND_GOLD_NAME = re.compile(r"(?:\d+ )?gold coins?")
# Safe to stand still for the few seconds a run takes.
MIN_HEALTH_FRACTION = 0.9
MIN_FOOD = 300
HOSTILE_QUIET_SECONDS = 10.0
MAX_RECORDS = 200
# Seconds each step may take before the run aborts.
TIMEOUTS = {
    "operator-login": 30.0,
    "teleport": 15.0,
    "create": 15.0,
    "drop": 15.0,
    "operator-logout": 10.0,
    "pickup": 30.0,
}
PERMISSION_DENIED = "you do not have sufficient permissions for that command."
NO_SUCH_PLAYER = "no such player."
# commands_handle (server/commands.c) for a command nobody registered, such
# as /create when the content's Python commands are not loaded.
UNKNOWN_TP = "'/tp' is not a valid command."
UNKNOWN_CREATE = "'/create' is not a valid command."
# Server answers proving the command did nothing.
NOTHING_CREATED = (PERMISSION_DENIED, UNKNOWN_CREATE)
# Where the coin is when nothing can tell: it may exist, so it counts.
GIVEN_COIN_STATES = ("ground", "character", "unknown")

def hostile_contact(client: AtrinikClient, seconds: float) -> bool:
    """``unresolved_hostile_contact`` over server lines only: a player who
    types "... hit you ..." in chat must not stop or refuse a run."""
    state = client.state
    server_lines = [message for message in getattr(state, "messages", ())
                    if message[1] not in c.PLAYER_CHAT_TYPES]
    return unresolved_hostile_contact(
        SimpleNamespace(state=SimpleNamespace(messages=server_lines)), seconds)


class OperatorAssistRefused(ValueError):
    """The run did not start: nothing was sent to the server."""


class OperatorAssistAborted(RuntimeError):
    """A step failed; the run stopped there and restored Sera."""

    def __init__(self, step: str, reason: str):
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason


def gold_stack(items: list, *, ground: bool = False) -> tuple[int, Any]:
    """Gold coins among ``items``: their count, and the largest stack.

    ``ground``: the items come from the look window, which sends no item
    type (``esrv_draw_look``); an untyped item counts when its whole name
    is a gold coin's."""
    total, largest = 0, None
    for item in items:
        name = item.name.casefold()
        if item.item_type == c.TYPE_MONEY:
            # The first denomination named, as carried_wallet_value reads it.
            denomination = next((coin for coin in COIN_VALUES if coin in name), None)
            if denomination != "gold":
                continue
        elif not (ground and not item.item_type and
                  GROUND_GOLD_NAME.fullmatch(name.strip())):
            continue
        total += item.quantity
        if largest is None or item.quantity > largest.quantity:
            largest = item
    return total, largest


def _well_formed(record: Any) -> bool:
    """A stored record the status can read: a damaged one is skipped."""
    if not isinstance(record, dict) or not isinstance(record.get("id"), str):
        return False
    return all(isinstance(record.get(key, 0), (int, float)) and
               not isinstance(record.get(key, 0), bool)
               for key in ("started_at", "ended_at", "copper"))


def _keeps_evidence(record: dict) -> bool:
    """Whether a record bounds the daily limit or proves she was given gold:
    such records are never trimmed."""
    return bool(record.get("counts")) or record.get("coin") in GIVEN_COIN_STATES


class OperatorSession(AtrinikClient):
    """The operator character's short-lived second connection.

    Its memory lives in a scratch directory removed at close, so nothing of
    the operator character reaches Sera's memory database. It never answers
    chat and never reconnects.
    """

    def __init__(self, config, scratch: Path):
        super().__init__(config)
        self._scratch = scratch

    async def _respond_to_player_chat(self, msg_type: int, text: str) -> None:
        return None

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            shutil.rmtree(self._scratch, ignore_errors=True)


def operator_session(client: AtrinikClient, character: str) -> OperatorSession:
    """A connection like ``client``'s, playing ``character`` once."""
    scratch = Path(tempfile.mkdtemp(prefix="atrinik-operator-"))
    try:
        config = dataclasses.replace(
            client.config, character=character, register=False,
            create_character=False, reconnect=False, party_name="",
            runtime_state_path=str(scratch / "operator.sqlite3"),
            runtime_content_path=str(getattr(client, "_runtime_content_root", "")))
        return OperatorSession(config, scratch)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


class OperatorAssist:
    """Run and audit the owner's operator assist for the played character."""

    def __init__(self, client: AtrinikClient, engine: Any,
                 operator_character: str = "", *,
                 session_factory: Callable[[AtrinikClient, str], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 timeouts: dict[str, float] | None = None,
                 poll_seconds: float = 0.1):
        self.client, self.engine = client, engine
        self.operator_character = operator_character.strip()
        self.session_factory = session_factory or operator_session
        self.clock = clock
        self.timeouts = {**TIMEOUTS, **(timeouts or {})}
        self.poll_seconds = poll_seconds
        self.running = False
        self._cache: tuple[str, list[dict]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.operator_character)

    # Audit records.

    def _character(self) -> str:
        state, config = self.client.state, self.client.config
        return state.player_name or config.character

    def records(self) -> list[dict]:
        character = self._character()
        if not character:
            return []
        key = character.casefold()
        if self._cache is None or self._cache[0] != key:
            try:
                stored = self.client._memory_store().load(character, MEMORY_KIND, [])
            except (OSError, sqlite3.Error) as error:
                log.warning("could not read operator-assist records: %s", error)
                return []
            self._cache = (key, [record for record in stored
                                 if _well_formed(record)]
                           if isinstance(stored, list) else [])
        return self._cache[1]

    def _save(self, record: dict) -> bool:
        """Insert or update ``record`` in the played character's audit list;
        False when the memory database could not store it."""
        character = record["character"]
        records = list(self.records()) if (
            self._character().casefold() == character.casefold()) else []
        for index, existing in enumerate(records):
            if existing.get("id") == record["id"]:
                records[index] = dict(record)
                break
        else:
            records.append(dict(record))
        # Refusals and runs that never reached /create are trimmed oldest
        # first; evidence of a counted run or given gold is kept for good.
        excess = len(records) - MAX_RECORDS
        if excess > 0:
            kept = []
            for existing in records:
                if excess > 0 and not _keeps_evidence(existing) and (
                        existing.get("id") != record["id"]):
                    excess -= 1
                    continue
                kept.append(existing)
            records = kept
        self._cache = (character.casefold(), records)
        saved = True
        try:
            self.client._memory_store().save(character, MEMORY_KIND, records)
        except (OSError, sqlite3.Error) as error:
            # The log line below still carries the record.
            log.error("could not save operator-assist record: %s", error)
            saved = False
        level = (logging.INFO if record["outcome"] in ("running", "completed")
                 else logging.WARNING)
        log.log(level, "operator assist %s: %s", record["outcome"], {
            key: record.get(key) for key in (
                "id", "character", "operator_character", "gold", "map", "x",
                "y", "sub_layer", "failed_step", "reason", "coin")})
        return saved

    def status(self) -> dict[str, Any]:
        """The dashboard's and metrics' view: whether, when and how much."""
        records = self.records()
        # Given: the coin left the operator, to her square or her pack, or
        # it may have (unknown). A coin left on her square by an aborted run
        # is hers to loot, so it counts too.
        completed = [record for record in records
                     if record.get("coin") in GIVEN_COIN_STATES]
        counted = [float(record.get("started_at", 0.0))
                   for record in records if record.get("counts")]
        last = max(counted) if counted else None
        return {
            "enabled": self.enabled,
            "running": self.running,
            "assisted": bool(completed),
            "completed_runs": sum(1 for record in records
                                  if record.get("outcome") == "completed"),
            "copper_total": sum(int(record.get("copper", 0) or 0)
                                for record in completed),
            "first_assisted_at": (min(float(record.get("ended_at", record.get("started_at", 0.0)))
                                      for record in completed) if completed else None),
            "last_assisted_at": (max(float(record.get("ended_at", record.get("started_at", 0.0)))
                                     for record in completed) if completed else None),
            "next_allowed_at": last + COOLDOWN_SECONDS if last is not None else None,
            "max_gold_per_run": MAX_GOLD_PER_RUN,
            "records": [dict(record) for record in records[-10:]],
        }

    # Preconditions.

    def refusal(self, gold: Any, to: str) -> str:
        """Why a run for ``gold`` coins to ``to`` may not start, or ""."""
        client, state = self.client, self.client.state
        if not self.enabled:
            return ("operator assist is disabled: no operator character is "
                    "configured (ATRINIK_BOT_OPERATOR_CHARACTER)")
        if self.running:
            return "an operator assist is already running"
        if (not isinstance(gold, int) or isinstance(gold, bool) or
                not 1 <= gold <= MAX_GOLD_PER_RUN):
            return f"give 1 to {MAX_GOLD_PER_RUN} gold coin(s) per run"
        name = state.player_name
        if state.phase != "playing" or not name:
            return "the character is not logged in"
        if str(to).strip().casefold() != name.casefold():
            return f"only {name}, the character this bot plays, can be assisted"
        if self.operator_character.casefold() == name.casefold():
            return "the operator character must not be the assisted character"
        now = self.clock()
        counted = [float(record.get("started_at", 0.0))
                   for record in self.records() if record.get("counts")]
        if counted and now - max(counted) < COOLDOWN_SECONDS:
            wait = COOLDOWN_SECONDS - (now - max(counted))
            return (f"one run per 24 h: the next is allowed in "
                    f"{wait / 3600:.1f} h")
        if not state.inventory_replay_complete or not state.map.path:
            return "the character's inventory or map is not loaded yet"
        task = getattr(self.engine, "task", None)
        if task is not None:
            return (f"not idle: task {task.name} is running; stop it from "
                    "the dashboard first")
        survival = getattr(self.engine, "idle_survival", None)
        if getattr(survival, "child", None) is not None:
            return "not idle: an idle food trip is running"
        if state.target_id or state.combat:
            return "not at a safe square: she has a target or combat is on"
        if hostile_contact(client, HOSTILE_QUIET_SECONDS):
            return "not at a safe square: something attacked her recently"
        stats = state.stats
        hp, maxhp = float(stats.get("hp", 0) or 0), float(stats.get("maxhp", 0) or 0)
        if maxhp <= 0 or hp < MIN_HEALTH_FRACTION * maxhp:
            return (f"not at a safe square: health below "
                    f"{MIN_HEALTH_FRACTION:.0%}")
        if int(stats.get("food", 0) or 0) < MIN_FOOD:
            return f"not at a safe square: food below {MIN_FOOD}"
        limit = float(stats.get("weight_limit", 0) or 0)
        carried = sum(item.weight * item.quantity for item in state.inventory)
        if limit > 0 and carried + gold * GOLD_WEIGHT_KG >= limit:
            # player_can_carry: the server would refuse her pickup.
            return (f"she cannot pick up the coin: she carries "
                    f"{carried:.2f} kg of her {limit:.2f} kg limit; "
                    "free some weight first")
        return ""

    # The sequence.


    def _danger(self) -> str:
        """Why holding her still is no longer safe, or "": checked on every
        poll, since the hold also suspends her idle healing and fleeing."""
        state = self.client.state
        if state.phase != "playing":
            return ""  # Her reconnect is waited out; nothing can hit her.
        if state.target_id or state.combat:
            return "she got a target or combat turned on"
        if hostile_contact(self.client, HOSTILE_QUIET_SECONDS):
            return "something attacked her"
        stats = state.stats
        hp, maxhp = float(stats.get("hp", 0) or 0), float(stats.get("maxhp", 0) or 0)
        if maxhp > 0 and hp < MIN_HEALTH_FRACTION * maxhp:
            return f"her health fell below {MIN_HEALTH_FRACTION:.0%}"
        return ""

    def _ensure_safe(self, step: str) -> None:
        """Before each order to the server: abort while holding her is unsafe."""
        danger = self._danger()
        if danger:
            raise OperatorAssistAborted(step, f"not safe to hold her: {danger}")

    async def run(self, gold: Any, to: str) -> dict:
        """Run the assist once; return its audit record.

        Raises ``OperatorAssistRefused`` (also audited) when a precondition
        fails. Any later failure aborts the run, logs the operator out,
        releases Sera and returns the record with outcome ``aborted``.
        """
        state = self.client.state
        now = self.clock()
        record: dict[str, Any] = {
            "id": secrets.token_hex(8),
            "started_at": now,
            "character": state.player_name or self.client.config.character,
            "operator_character": self.operator_character,
            "gold": gold if isinstance(gold, int) and not isinstance(gold, bool) else 0,
            "copper": (gold * GOLD_COPPER
                       if isinstance(gold, int) and not isinstance(gold, bool) else 0),
            "map": state.map.path, "x": state.map.world_x, "y": state.map.world_y,
            "sub_layer": state.map.player_sub_layer,
            "outcome": "running",
            # Whether it counts toward the daily limit: from the /create on.
            "counts": False,
            # Where the coin is: none, operator-inventory, ground, character.
            "coin": "none",
            "steps": [],
        }
        reason = self.refusal(gold, to)
        if reason:
            # A disabled feature keeps no records: nothing could have run.
            if record["character"] and self.enabled:
                record.update(outcome="refused", reason=reason, ended_at=now)
                self._save(record)
            raise OperatorAssistRefused(reason)
        # Nothing awaits between the refusal check and this flag.
        self.running = True
        session, runner = None, None
        try:
            record["carried_before"] = self._carried()
            self._save(record)
            self.client.record_action(
                "operator-assist",
                f"started: {gold} gold for {record['character']} from operator "
                f"{self.operator_character} at {record['map']} "
                f"({record['x']}, {record['y']})")
            # Nothing of hers runs meanwhile: no task, no idle survival, no
            # inventory upkeep. She stands still on the recorded square.
            self.engine.operator_hold = "operator-assist"
            survival = getattr(self.engine, "idle_survival", None)
            if survival is not None:
                survival.drop()
            with contextlib.suppress(ConnectionError):
                await self.client.clear_actions()
            session = self.session_factory(self.client, self.operator_character)
            messages: list[str] = []
            session.add_handler(
                lambda event: messages.append(str(event.data[2]).strip().casefold())
                if event.kind == "message" and isinstance(event.data, tuple)
                and len(event.data) == 3 else None)
            runner = asyncio.create_task(session.run())
            await self._sequence(record, gold, session, runner, messages)
            record["outcome"] = "completed"
        except OperatorAssistAborted as error:
            record.update(outcome="aborted", failed_step=error.step,
                          reason=error.reason)
        except Exception as error:
            log.exception("operator assist failed")
            record.update(outcome="aborted", failed_step=self._last_step(record),
                          reason=f"{type(error).__name__}: {error}")
        finally:
            # Release her first: this cannot fail, whatever cleanup does.
            self.engine.operator_hold = ""
            if record["outcome"] == "running":
                # Cancelled (the bot is shutting down).
                record.update(outcome="aborted", reason="cancelled",
                              failed_step=self._last_step(record))
            try:
                if session is not None:
                    try:
                        await self._settle_coin(record, session, gold)
                    finally:
                        await self._end_session(record, session, runner)
            finally:
                # Only now, after the operator's logout: a second request
                # during that wait is still refused as already running.
                self.running = False
                record["ended_at"] = self.clock()
                record["carried_after"] = self._carried()
                # Sera never logs out; a dropped connection of hers is
                # restored by her own reconnect loop.
                record["character_phase"] = self.client.state.phase
                self._save(record)
                detail = (f"{record['outcome']}: {gold} gold "
                          f"({record['copper']} copper) from operator "
                          f"{self.operator_character}")
                if record["outcome"] != "completed":
                    detail += (f" at {record.get('failed_step')}: "
                               f"{record.get('reason')}; coin {record['coin']}")
                self.client.record_action("operator-assist", detail)
        return dict(record)

    async def _settle_coin(self, record: dict, session: Any, gold: Any) -> None:
        """After an abort past /create, look once more where the coin is.

        Updates arrive after the orders: a coin that timed out may still
        land. Whatever no inventory or square accounts for is ``unknown``,
        which counts as given, so the assisted mark errs on the safe side.
        """
        if (record["outcome"] == "completed" or not record["counts"] or
                record["coin"] not in ("none", "operator-inventory") or
                not isinstance(gold, int)):
            return
        await asyncio.sleep(min(1.0, self.timeouts["drop"]))
        before = int(record.get("operator_gold_before", 0))
        if gold_stack(session.state.inventory)[0] >= before + gold:
            record["coin"] = "operator-inventory"
        elif gold_stack(self.client.state.ground, ground=True)[0] >= gold and (
                record["coin"] == "operator-inventory"):
            record["coin"] = "ground"
        else:
            record["coin"] = "unknown"

    @staticmethod
    def _last_step(record: dict) -> str:
        return record["steps"][-1]["step"] if record["steps"] else "start"

    def _carried(self) -> int:
        return gold_stack(self.client.state.inventory)[0] * GOLD_COPPER

    def _step(self, record: dict, step: str, detail: str = "") -> None:
        record["steps"].append({"step": step, "at": self.clock(), "detail": detail})

    async def _wait(self, step: str, ready: Callable[[], bool], *,
                    runner: asyncio.Task | None = None,
                    messages: list[str] | None = None, since: int = 0,
                    failures: tuple[str, ...] = ()) -> None:
        """Poll until ``ready()``; abort when holding her is no longer safe,
        on a failure line among the operator's ``messages`` from index
        ``since`` on, a lost operator connection, or the step's timeout."""
        deadline = time.monotonic() + self.timeouts[step]
        seen = since
        while True:
            # Readiness first, so the record tells where the coin is.
            if ready():
                return
            danger = self._danger()
            if danger:
                raise OperatorAssistAborted(step, f"not safe to hold her: {danger}")
            if messages is not None:
                for text in messages[seen:]:
                    if text in failures:
                        raise OperatorAssistAborted(step, f"server said {text!r}")
                seen = len(messages)
            if runner is not None and runner.done():
                error = (runner.exception() if not runner.cancelled() else None)
                raise OperatorAssistAborted(
                    step, f"operator connection ended: {error or 'closed'}")
            if time.monotonic() >= deadline:
                raise OperatorAssistAborted(
                    step, f"timed out after {self.timeouts[step]:g} s")
            await asyncio.sleep(self.poll_seconds)

    async def _sequence(self, record: dict, gold: int, session: Any,
                        runner: asyncio.Task, messages: list[str]) -> None:
        client, name = self.client, record["character"]
        square = (record["map"], record["x"], record["y"])
        ops = session.state

        self._step(record, "operator-login", self.operator_character)

        def logged_in() -> bool:
            if ops.phase == "characters":
                raise OperatorAssistAborted(
                    "operator-login", "the operator character is not on this account")
            return (ops.phase == "playing" and ops.inventory_replay_complete
                    and bool(ops.map.path))

        await self._wait("operator-login", logged_in, runner=runner)

        def on_square() -> bool:
            # The sub-layer too: a drop lands on the dropper's sub-layer
            # (drop_object), and /tp does not set it.
            return ((ops.map.path, ops.map.world_x, ops.map.world_y) == square and
                    ops.map.player_sub_layer == record["sub_layer"])

        self._step(record, "teleport", f"/tp {name}")
        self._ensure_safe("teleport")
        mark = len(messages)
        await session.command(f"/tp {name}")
        await self._wait("teleport", on_square, runner=runner, messages=messages,
                         since=mark,
                         failures=(PERMISSION_DENIED, NO_SUCH_PLAYER, UNKNOWN_TP))
        if (client.state.map.path, client.state.map.world_x,
                client.state.map.world_y) != square:
            raise OperatorAssistAborted("teleport", f"{name} left her square")

        before, _ = gold_stack(ops.inventory)
        record["operator_gold_before"] = before
        stacks_before = {item.tag: item.quantity for item in ops.inventory}
        command = (f"/create {GOLD_ARCHETYPE}" if gold == 1 else
                   f"/create {gold} {GOLD_ARCHETYPE}")
        self._step(record, "create", command)
        self._ensure_safe("create")
        # From here a coin may exist: the run counts toward the daily limit,
        # and it must not start unless that is on record.
        record["counts"] = True
        if not self._save(record):
            record["counts"] = False
            raise OperatorAssistAborted(
                "create", "the audit record could not be saved; nothing created")
        mark = len(messages)
        await session.command(command)
        try:
            await self._wait("create",
                             lambda: gold_stack(ops.inventory)[0] >= before + gold,
                             runner=runner, messages=messages, since=mark,
                             failures=NOTHING_CREATED)
        except OperatorAssistAborted as error:
            if any(text in error.reason for text in NOTHING_CREATED):
                # The server refused before the command ran
                # (commands_handle): nothing was created.
                record["counts"] = False
            raise
        record["coin"] = "operator-inventory"

        if not on_square():
            raise OperatorAssistAborted("drop", "the operator left her square")
        # The stack the new coin landed in (a fresh one, or one that grew).
        grown = [item for item in ops.inventory
                 if item.item_type == c.TYPE_MONEY and
                 item.quantity > stacks_before.get(item.tag, 0) and
                 gold_stack([item])[0] and not item.flags & c.ITEM_LOCKED]
        if not grown:
            raise OperatorAssistAborted("drop", "cannot tell which gold stack is new")
        self._step(record, "drop", f"{gold} gold coin(s) at {square}")
        self._ensure_safe("drop")
        await session.move_item(0, grown[0].tag, gold)
        await self._wait("drop", lambda: gold_stack(ops.inventory)[0] <= before,
                         runner=runner)
        record["coin"] = "ground"

        self._step(record, "operator-logout")
        await self._end_session(record, session, runner)

        sera_before = gold_stack(client.state.inventory)[0]
        self._step(record, "pickup", f"{name} picks up {gold} gold coin(s)")

        def coin_underfoot():
            if client.state.phase != "playing":
                return None
            _, coin = gold_stack(client.state.ground, ground=True)
            return coin if coin is not None and coin.quantity >= gold else None

        await self._wait("pickup", lambda: coin_underfoot() is not None)
        await client.move_item(client.state.player_tag, coin_underfoot().tag, gold)
        await self._wait("pickup", lambda: client.state.phase == "playing" and
                         gold_stack(client.state.inventory)[0] >= sera_before + gold)
        record["coin"] = "character"

    async def _end_session(self, record: dict, session: Any,
                           runner: asyncio.Task | None) -> None:
        """Log the operator out (closing the connection saves it)."""
        if getattr(session, "_operator_closed", False):
            return
        session._operator_closed = True
        try:
            await asyncio.wait_for(session.close(), self.timeouts["operator-logout"])
        except Exception as error:  # Logged; the connection is dropped below.
            log.warning("operator logout: %s", error)
            record.setdefault("cleanup", []).append(f"logout: {error}")
        if runner is not None and not runner.done():
            runner.cancel()
            # Wait for the runner without absorbing a cancellation of ours.
            await asyncio.wait({runner})
        if runner is not None and runner.done() and not runner.cancelled():
            runner.exception()  # Retrieved: its failure is already recorded.
