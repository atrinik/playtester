"""Rank farming spots by running the locked farming analyzer in the background.

The analyzer (atrinik/tools ``farming_analyzer``) scores every authored map and
world-tile circuit from the pinned content and Classic experience tables. It
takes tens of seconds, so it runs as a separate low-priority process while the
bot keeps playing. It is fed the character's live combat profile (the
killing skill's level and the player doll's DAM, WC, weapon speed, AC and
protections; while training Wizardry, the spell's observed damage per cast,
cast delay and mana instead of the weapon) and re-runs when that profile
changes materially or the result ages out. One ``--ranking all`` run yields every objective, so switching the
preferred objective never needs a new run.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import statistics
import shutil
import sys
import time
from typing import Any, Callable

from . import constants as c

log = logging.getLogger(__name__)

# Dashboard objectives, mapped to the analyzer's ranking views.
OBJECTIVES = {
    "xp": "XP per hour",
    "money": "Money per hour",
    "loot": "Loot per hour",
    "balanced": "Balanced",
    "safety": "Safety",
}
DEFAULT_OBJECTIVE = "xp"
# Melee weapon schools and the damage type their weapons deal.
WEAPON_SCHOOLS = {
    "slash weapons": "slash",
    "impact weapons": "impact",
    "pierce weapons": "pierce",
    "cleave weapons": "cleave",
}
# The automatic secondary-combat plan: Wizardry, trained by spell-only kills
# (the server splits kill XP by each skill's share of the damage). One pure
# function of the character's state, shared by the farm circuit and the
# analyzer profile, so spots are ranked for the trained skill.
AUTO_WIZARD_START_LEVEL = 18
AUTO_WIZARD_FIRST_TARGET = 10
AUTO_WIZARD_AOE_REVIEW_LEVEL = 25
AUTO_WIZARD_AOE_TARGET = 15
# Her own hit text: "You hit <name> for <done> (<done - raw>) with <attack>."
HIT_TEXT = re.compile(r"^You hit .+ for (\d+) \((-?\d+)\) with ([a-z ]+)\.$", re.I)
# Attack types a damage spell's hit text can name, as the analyzer knows them.
SPELL_ATTACKS = frozenset({
    "fire", "cold", "electricity", "poison", "acid", "drain", "magic"})
# Hit-text attack names the locked analyzer lacks, mapped to the nearest it
# knows. Magic bullet deals weapon magic, which no monster archetype
# protects against; as "magic" it is understated only against the few that
# resist magic.
SPELL_ATTACK_ALIASES = {"weapon magic": "magic"}
SPELL_SAMPLES = 30
# Hit text this soon after her own offensive cast is that spell's damage.
SPELL_HIT_WINDOW_SECONDS = 3.0
# Seconds per cast in the caster model. The client's action_time is the delay
# of whatever she did last (a sword swing included), and mana, not the cast
# rate, bounds a spell-only kill rate, so a fixed second is used.
SPELL_CAST_DELAY = 1.0


def owned_skill_level(state, name: str) -> int:
    """The level of the named skill she has, or 0 (also when unreadable)."""
    folded = name.casefold()
    levels = []
    for item in state.inventory:
        if item.item_type == c.TYPE_SKILL and item.name.casefold() == folded:
            try:
                levels.append(int(item.extra.get("level", 0) or 0))
            except (TypeError, ValueError):
                levels.append(0)
    return max(levels, default=0)


def automatic_training_plan(state) -> tuple[str, str, int]:
    """The (skill, spell, target level) to train next, or ("", "", 0)."""
    level = int(state.stats.get("level", 0) or 0)
    wizardry = owned_skill_level(state, "wizardry spells")
    known = {item.name.casefold() for item in state.inventory
             if item.item_type == c.TYPE_SPELL}
    spell = next((name for name in ("magic bullet", "cause light wounds")
                  if name in known), "")
    if not spell or level < AUTO_WIZARD_START_LEVEL:
        return "", "", 0
    if wizardry < AUTO_WIZARD_FIRST_TARGET:
        return "wizardry spells", spell, AUTO_WIZARD_FIRST_TARGET
    if level >= AUTO_WIZARD_AOE_REVIEW_LEVEL and wizardry < AUTO_WIZARD_AOE_TARGET:
        return "wizardry spells", spell, AUTO_WIZARD_AOE_TARGET
    return "", "", 0


# Fields copied from each ranked result into the dashboard snapshot.
SUMMARY_FIELDS = (
    "path", "name", "region", "location", "map_paths", "monsters", "xp_hour",
    "money_hour", "loot_hour", "danger", "score", "safety_score",
    "aggressive_spawns", "max_aggro_pack", "max_monster_level", "danger_flags",
)


@dataclass(frozen=True)
class CombatProfile:
    """The live melee profile the analyzer simulates."""

    skill: str
    skill_level: int
    attack: str
    damage: int
    weapon_class: int
    weapon_speed: float
    character_level: int
    max_health: int
    speed: float
    armour_class: int
    protections: tuple[tuple[str, int], ...]
    # Largest likely simultaneous aggro a spot may have; 0 allows any. While
    # a secondary skill trains one target at a time, spots must not pack.
    max_aggro_pack: int = 0
    # Spell-only training: raw damage per cast (0 simulates the weapon),
    # seconds per cast, mana per cast, mana regenerated per second, the mana
    # usable for offence (max mana less the healing reserve), attack type.
    spell_damage: float = 0.0
    cast_delay: float = 0.0
    mana_cost: int = 0
    mana_regen: float = 0.0
    max_mana: int = 0
    spell_attack: str = ""
    # She has Meditation: resting out of combat ramps regeneration up to
    # tenfold (types/player.c, player_do_some_living).
    meditation: bool = False

    def arguments(self) -> list[str]:
        if self.spell_damage > 0:
            args = [
                str(self.skill_level),
                "--damage-per-attack", f"{self.spell_damage:g}",
                "--attack-delay", f"{self.cast_delay:g}",
                "--mana-cost", str(self.mana_cost),
                "--mana-regen", f"{self.mana_regen:g}",
                "--max-mana", str(self.max_mana),
                "--attack-type", self.spell_attack,
            ]
            if self.meditation:
                args.append("--meditation")
        else:
            args = [
                str(self.skill_level),
                "--melee-damage", str(self.damage),
                "--weapon-class", str(self.weapon_class),
                "--weapon-speed", f"{self.weapon_speed:g}",
                "--melee-attack", f"{self.attack}=100",
            ]
        args += [
            "--character-level", str(self.character_level),
            "--max-health", str(self.max_health),
            "--character-speed", f"{self.speed:g}",
            "--player-ac", str(self.armour_class),
        ]
        for name, value in self.protections:
            args += ["--player-protection", f"{name}={value}"]
        if self.max_aggro_pack:
            args += ["--max-aggro-pack", str(self.max_aggro_pack)]
        return args

    def materially_differs(self, other: "CombatProfile | None") -> bool:
        """Whether a new analysis could rank spots differently."""
        if other is None:
            return True
        if (self.skill, self.skill_level, self.attack, self.character_level,
                self.max_aggro_pack, self.spell_attack, self.mana_cost,
                self.meditation) != (
                other.skill, other.skill_level, other.attack, other.character_level,
                other.max_aggro_pack, other.spell_attack, other.mana_cost,
                other.meditation):
            return True
        if (self.spell_damage > 0) != (other.spell_damage > 0):
            return True

        def relative(new: float, old: float, fraction: float) -> bool:
            return abs(new - old) > fraction * max(abs(old), 1.0)

        if (relative(self.spell_damage, other.spell_damage, 0.15) or
                relative(self.mana_regen, other.mana_regen, 0.10) or
                relative(self.max_mana, other.max_mana, 0.10) or
                relative(self.damage, other.damage, 0.10) or
                relative(self.weapon_speed, other.weapon_speed, 0.05) or
                relative(self.speed, other.speed, 0.05) or
                relative(self.max_health, other.max_health, 0.10) or
                abs(self.weapon_class - other.weapon_class) >= 2 or
                abs(self.armour_class - other.armour_class) >= 2):
            return True
        old = dict(other.protections)
        new = dict(self.protections)
        return any(abs(new.get(name, 0) - old.get(name, 0)) >= 5
                   for name in set(old) | set(new))

    @classmethod
    def from_description(cls, data: dict) -> "CombatProfile | None":
        """Rebuild a profile from ``describe()``, or None if incomplete."""
        names = {entry.name for entry in dataclasses.fields(cls)}
        try:
            values = {key: value for key, value in data.items() if key in names}
            values["protections"] = tuple(sorted(
                (str(name), int(amount))
                for name, amount in dict(data.get("protections") or {}).items()))
            return cls(**values)
        except (TypeError, ValueError, AttributeError):
            return None

    def describe(self) -> dict[str, Any]:
        return {
            "skill": self.skill, "skill_level": self.skill_level,
            "attack": self.attack, "damage": self.damage,
            "weapon_class": self.weapon_class, "weapon_speed": self.weapon_speed,
            "character_level": self.character_level, "max_health": self.max_health,
            "speed": self.speed, "armour_class": self.armour_class,
            "protections": dict(self.protections),
            "max_aggro_pack": self.max_aggro_pack,
            "spell_damage": self.spell_damage, "cast_delay": self.cast_delay,
            "mana_cost": self.mana_cost, "mana_regen": self.mana_regen,
            "max_mana": self.max_mana, "spell_attack": self.spell_attack,
            "meditation": self.meditation,
        }


def combat_profile(client, spell_damage: float = 0.0,
                   spell_attack: str = "") -> CombatProfile | None:
    """Read the killing skill and player doll, or None if unsupported.

    ``spell_damage``/``spell_attack`` are the observed raw damage per hit of
    her damage spell and its attack type (see FarmAnalyzer.observe_spells).
    """
    state = client.state
    weapon = state.items.get(state.equipment.get(c.EQUIP_WEAPON, 0))
    if weapon is None or not getattr(weapon, "required_skill_tag", 0):
        return None
    skill = state.items.get(weapon.required_skill_tag)
    if skill is None or skill.item_type != c.TYPE_SKILL:
        return None
    school = skill.name.casefold()
    attack = WEAPON_SCHOOLS.get(school)
    stats = state.stats
    try:
        skill_level = int(skill.extra.get("level", 0) or 0)
        damage = int(stats.get("dam", 0) or 0)
        weapon_class = int(stats.get("wc", 0) or 0)
        weapon_speed = float(stats.get("weapon_speed", 0) or 0)
        character_level = int(stats.get("level", 0) or 0)
        max_health = int(stats.get("maxhp", 0) or 0)
        speed = float(stats.get("speed", 0) or 0)
        armour_class = int(stats.get("ac", 0) or 0)
    except (TypeError, ValueError):
        return None
    if (attack is None or skill_level < 1 or damage < 1 or weapon_speed <= 0 or
            character_level < 1 or max_health < 1 or speed <= 0):
        return None
    protections = tuple(sorted(
        (name.replace("_", ""), max(-100, min(100, int(value))))
        for index, name in enumerate(c.ATTACK_NAMES)
        for value in (state.protections.get(index, 0),)
        if value))
    # Training Wizardry, every hit is the spell (kill XP is split by damage
    # share), so it is ranked at that skill's level on spots that send one
    # monster at a time, killing with the spell's damage and mana. Until a
    # cast has been observed, the weapon stands in.
    max_aggro_pack = 0
    caster: dict[str, Any] = {}
    training, spell_name, _ = automatic_training_plan(state)
    trained_level = owned_skill_level(state, training) if training else 0
    if trained_level >= 1:
        school, skill_level, max_aggro_pack = training, trained_level, 1
        spell = next((item for item in state.inventory
                      if item.item_type == c.TYPE_SPELL and
                      item.name.casefold() == spell_name), None)
        try:
            cost = int(spell.extra.get("cost", 0) or 0) if spell else 0
            regen = float(stats.get("regen_sp", 0) or 0)
            maxsp = int(stats.get("maxsp", 0) or 0)
        except (TypeError, ValueError):
            cost, regen, maxsp = 0, 0.0, 0
        heal_costs = [
            int(item.extra.get("cost", 0) or 0) for item in state.inventory
            if item.item_type == c.TYPE_SPELL and "heal" in item.name.casefold() and
            int(item.extra.get("cost", 0) or 0) > 0]
        offence = maxsp - min(heal_costs, default=0) * 3
        if (spell_damage > 0 and spell_attack in SPELL_ATTACKS and
                0 < cost <= offence and regen > 0):
            caster = dict(
                spell_damage=round(spell_damage, 1),
                cast_delay=SPELL_CAST_DELAY, mana_cost=cost,
                mana_regen=round(regen, 3), max_mana=offence,
                spell_attack=spell_attack,
                meditation=any(
                    item.item_type == c.TYPE_SKILL and
                    item.name.casefold() == "meditation"
                    for item in state.inventory))
    return CombatProfile(
        skill=school, skill_level=skill_level, attack=attack, damage=damage,
        weapon_class=weapon_class, weapon_speed=round(weapon_speed, 3),
        character_level=character_level, max_health=max_health,
        speed=round(speed, 3), armour_class=armour_class, protections=protections,
        max_aggro_pack=max_aggro_pack, **caster)


class FarmAnalyzer:
    """Owns at most one background analyzer process and its latest ranking."""

    # Never re-run more often than this, however much the profile changes.
    MIN_INTERVAL_SECONDS = 600.0
    # Re-run an unchanged profile after this long.
    REFRESH_SECONDS = 6 * 3600.0
    # After a failure, retry no sooner than this.
    RETRY_SECONDS = 900.0
    TIMEOUT_SECONDS = 900.0
    TOP = 10

    def __init__(self, analyzer_root: Path, content_root: Path, server_root: Path, *,
                 python: str = sys.executable,
                 clock: Callable[[], float] = time.time) -> None:
        self.analyzer_root = Path(analyzer_root)
        self.content_root = Path(content_root)
        self.server_root = Path(server_root)
        self.python = python
        self.clock = clock
        self.status = "idle"
        self.error = ""
        self.profile: CombatProfile | None = None
        self.report: dict | None = None
        self.started_at = 0.0
        self.finished_at = 0.0
        # When the current report was produced; failures never move it.
        self.reported_at = 0.0
        self.duration = 0.0
        self.runs = 0
        self._last_attempt_at = 0.0
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        # Raw damage of her recent damage-spell hits, their attack type, the
        # spell they came from and the next message position to read.
        self.spell_samples: list[int] = []
        self.spell_attack = ""
        self.spell_name = ""
        self._message_position = 0

    @classmethod
    def from_bundle(cls, installed: dict, **kwargs) -> "FarmAnalyzer":
        return cls(installed["analyzer"], installed["source"],
                   installed["classic_server"] / "src", **kwargs)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def command(self, profile: CombatProfile) -> list[str]:
        """The isolated analyzer invocation for one profile."""
        bootstrap = (
            "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
            "sys.argv[0] = 'farming_analyzer'; "
            "runpy.run_module('farming_analyzer', run_name='__main__', alter_sys=True)")
        command = [
            self.python, "-E", "-s", "-c", bootstrap, str(self.analyzer_root),
            *profile.arguments(),
            "--content-root", str(self.content_root),
            "--server-root", str(self.server_root),
            "--ranking", "all", "--top", str(self.TOP), "--json",
        ]
        nice = shutil.which("nice")
        return [nice, "-n", "10", *command] if nice else command

    def due(self, profile: CombatProfile | None) -> bool:
        if profile is None or self.running:
            return False
        now = self.clock()
        if now - self._last_attempt_at < self.MIN_INTERVAL_SECONDS and self.runs:
            return False
        if self.status == "failed":
            # A failed run is retried on its own backoff, whatever the age or
            # profile of the report it failed to replace.
            return now - self._last_attempt_at >= self.RETRY_SECONDS
        if self.report is None:
            return True
        return (profile.materially_differs(self.profile) or
                now - self.reported_at >= self.REFRESH_SECONDS)

    def observe_spells(self, client) -> None:
        """Record the raw damage of her damage spell from her own hit text.

        Only hit text within a few seconds of her own offensive cast counts,
        so elemental melee (rings of fire and the like) is never sampled.
        """
        state = client.state
        entries = state.messages_since(self._message_position)
        self._message_position = state.message_count
        casts = list(getattr(client, "offensive_casts", ()))
        for timestamp, _, _, text in entries:
            spell = next((
                name for cast_at, name in reversed(casts)
                if 0.0 <= timestamp - cast_at <= SPELL_HIT_WINDOW_SECONDS), "")
            if not spell:
                continue
            if spell != self.spell_name:
                self.spell_name = spell
                self.spell_samples = []
                self.spell_attack = ""
            match = HIT_TEXT.match(text)
            if match is None:
                continue
            attack = match.group(3).casefold()
            attack = SPELL_ATTACK_ALIASES.get(attack, attack)
            if attack not in SPELL_ATTACKS:
                continue
            raw = int(match.group(1)) - int(match.group(2))
            if raw > 0:
                self.spell_samples = (self.spell_samples + [raw])[-SPELL_SAMPLES:]
                self.spell_attack = attack

    def poll(self, client) -> bool:
        """Start a background analysis if the live profile calls for one."""
        self.observe_spells(client)
        # Only samples of the spell the training plan casts describe it.
        planned = automatic_training_plan(client.state)[1]
        observed = self.spell_samples and self.spell_name == planned
        profile = combat_profile(
            client,
            statistics.median(self.spell_samples) if observed else 0.0,
            self.spell_attack)
        if profile is None:
            if self.report is None and not self.running:
                self.status = "unsupported"
                self.error = "no melee weapon school to simulate"
            return False
        if not self.due(profile):
            return False
        self._last_attempt_at = self.clock()
        # Report the run as soon as it is scheduled, not when it first runs.
        self.status = "running"
        self.error = ""
        self.started_at = self._last_attempt_at
        self.runs += 1
        self._task = asyncio.get_running_loop().create_task(self._run(profile))
        return True

    async def _run(self, profile: CombatProfile) -> None:
        command = self.command(profile)
        log.info("farming analyzer started for %s %d", profile.skill, profile.skill_level)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(
                    self._process.communicate(), self.TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
                raise RuntimeError(f"timed out after {self.TIMEOUT_SECONDS:.0f}s")
            if self._process.returncode != 0:
                detail = stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
                raise RuntimeError(
                    f"exited with status {self._process.returncode}: {detail[0][:300]}")
            report = json.loads(stdout)
            rankings = report.get("rankings") if isinstance(report, dict) else None
            if not isinstance(rankings, dict) or not all(
                    isinstance(rankings.get(name), list) for name in OBJECTIVES):
                raise RuntimeError("report has no ranking for every objective")
        except asyncio.CancelledError:
            if self._process is not None and self._process.returncode is None:
                self._process.kill()
            raise
        except Exception as error:  # Keep playing on the previous ranking.
            self.status = "failed"
            self.error = str(error)
            log.warning("farming analyzer failed: %s", error)
        else:
            self.report = report
            self.profile = profile
            self.status = "ready"
            self.reported_at = self.clock()
            log.info("farming analyzer finished in %.1fs; top xp spot: %s",
                     self.clock() - self.started_at,
                     (self.best("xp") or {}).get("name", "none"))
        finally:
            self._process = None
            self.finished_at = self.clock()
            self.duration = self.finished_at - self.started_at

    def ranking(self, objective: str) -> list[dict]:
        if self.report is None or objective not in OBJECTIVES:
            return []
        return list(self.report["rankings"].get(objective) or [])

    def best(self, objective: str) -> dict | None:
        """The analyzer's #1 spot for an objective."""
        ranked = self.ranking(objective)
        return ranked[0] if ranked else None

    def snapshot(self, objective: str) -> dict[str, Any]:
        objective = objective if objective in OBJECTIVES else DEFAULT_OBJECTIVE
        return {
            "status": self.status,
            "error": self.error,
            "objective": objective,
            "objectives": OBJECTIVES,
            "running": self.running,
            "runs": self.runs,
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
            "duration": round(self.duration, 1) if self.duration else None,
            "profile": self.profile.describe() if self.profile else None,
            "ranking": [
                {key: item.get(key) for key in SUMMARY_FIELDS}
                for item in self.ranking(objective)[:5]
            ],
        }

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


async def run_periodically(analyzer: FarmAnalyzer, client, *, interval: float = 30.0) -> None:
    """Check for a due analysis while the character is in the game."""
    while True:
        state = client.state
        if state.phase == "playing" and getattr(state, "inventory_replay_complete", True):
            try:
                analyzer.poll(client)
            except Exception:  # A broken profile must never stop the bot.
                log.exception("farming analyzer poll failed")
        await asyncio.sleep(interval)
