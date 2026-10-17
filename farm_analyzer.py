"""Rank farming spots by running the locked farming analyzer in the background.

The analyzer (atrinik/tools ``farming_analyzer``) scores every authored map and
world-tile circuit from the pinned content and Classic experience tables. It
takes tens of seconds, so it runs as a separate low-priority process while the
bot keeps playing. It is fed the character's live combat profile (the
killing skill's level and the player doll's DAM, WC, weapon speed, AC and
protections) and re-runs when that profile changes materially or the result
ages out. One ``--ranking all`` run yields every objective, so switching the
preferred objective never needs a new run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
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

    def arguments(self) -> list[str]:
        args = [
            str(self.skill_level),
            "--melee-damage", str(self.damage),
            "--weapon-class", str(self.weapon_class),
            "--weapon-speed", f"{self.weapon_speed:g}",
            "--melee-attack", f"{self.attack}=100",
            "--character-level", str(self.character_level),
            "--max-health", str(self.max_health),
            "--character-speed", f"{self.speed:g}",
            "--player-ac", str(self.armour_class),
        ]
        for name, value in self.protections:
            args += ["--player-protection", f"{name}={value}"]
        return args

    def materially_differs(self, other: "CombatProfile | None") -> bool:
        """Whether a new analysis could rank spots differently."""
        if other is None:
            return True
        if (self.skill, self.skill_level, self.attack, self.character_level) != (
                other.skill, other.skill_level, other.attack, other.character_level):
            return True

        def relative(new: float, old: float, fraction: float) -> bool:
            return abs(new - old) > fraction * max(abs(old), 1.0)

        if (relative(self.damage, other.damage, 0.10) or
                relative(self.weapon_speed, other.weapon_speed, 0.05) or
                relative(self.max_health, other.max_health, 0.10) or
                abs(self.weapon_class - other.weapon_class) >= 2 or
                abs(self.armour_class - other.armour_class) >= 2):
            return True
        old = dict(other.protections)
        new = dict(self.protections)
        return any(abs(new.get(name, 0) - old.get(name, 0)) >= 5
                   for name in set(old) | set(new))

    def describe(self) -> dict[str, Any]:
        return {
            "skill": self.skill, "skill_level": self.skill_level,
            "attack": self.attack, "damage": self.damage,
            "weapon_class": self.weapon_class, "weapon_speed": self.weapon_speed,
            "character_level": self.character_level, "max_health": self.max_health,
            "speed": self.speed, "armour_class": self.armour_class,
            "protections": dict(self.protections),
        }


def combat_profile(client) -> CombatProfile | None:
    """Read the killing skill and player doll, or None if unsupported."""
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
    return CombatProfile(
        skill=school, skill_level=skill_level, attack=attack, damage=damage,
        weapon_class=weapon_class, weapon_speed=round(weapon_speed, 3),
        character_level=character_level, max_health=max_health,
        speed=round(speed, 3), armour_class=armour_class, protections=protections)


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
        self.duration = 0.0
        self.runs = 0
        self._last_attempt_at = 0.0
        self._task: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None

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
        if self.status == "failed" and now - self._last_attempt_at < self.RETRY_SECONDS:
            return False
        if self.report is None:
            return True
        return (profile.materially_differs(self.profile) or
                now - self.finished_at >= self.REFRESH_SECONDS)

    def poll(self, client) -> bool:
        """Start a background analysis if the live profile calls for one."""
        profile = combat_profile(client)
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
