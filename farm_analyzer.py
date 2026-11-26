"""Rank farming spots by running the locked farming analyzer in the background.

The analyzer (atrinik/tools ``farming_analyzer``) scores every authored map and
world-tile circuit from the pinned content and Classic experience tables. It
takes tens of seconds, so it runs as a separate low-priority process while the
bot keeps playing. It is fed the character's live combat profile (the
killing skill's level and the player doll's DAM, WC, weapon speed, AC and
protections; while training Wizardry, the spell's observed damage per cast,
cast delay and mana instead of the weapon) and re-runs when that profile
changes materially, her savebed moves, or the result ages out. A spot is
judged from her savebed: the analyzer charges the maps crossed from there to
the spot (approach danger) against her maximum health. One ``--ranking all`` run yields every objective, so switching the
preferred objective never needs a new run.

Since analyzer v3.11.0 (report ``schema_version`` 2) the run also names her
combat style, measured healing and speed, so the analyzer's encounter model
rates each spot by the packs a pull really brings (``spot_safety_score``,
``expected_pack``, ``worst_pack``, ``encounter_reasons``). A report without a
schema version (an older analyzer) is read by its older fields.
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
# The server's answer to every heal that lands (tasks.HEALED_MESSAGE).
HEALED_TEXT = re.compile(r"^The spell heals you for (\d{1,9}) hp", re.I)
# A heal answer this soon after her FIRE of a healing spell is that spell's.
HEAL_ATTRIBUTION_SECONDS = 2.0
HEAL_SAMPLES = 30
# The analyzer's combat styles (--combat-style) for her two kits.
MELEE_STYLE = "melee"
RANGED_KITE_STYLE = "ranged-kite"


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


# The largest likely simultaneous aggro (the analyzer's --max-aggro-pack)
# a spot may have for her ordinary kit. She fights one target at a time and
# casts no area spell, so a pack larger than two outlasts her heals. Live,
# on 2026-11-23 (level 37, 330 health), the analyzer allowed any pack
# (0), and she died at an Underground City circuit of pack 3. The analyzer
# rejects such a map before it forms circuits.
SINGLE_TARGET_MAX_AGGRO_PACK = 2
# Seam exposure (seam_adjusted, WorldGraph.seam_exposure): a caster on a
# map tiled to a spot's map, outside the spot, within this many tiles of
# where she pulls (a spawn's detection radius from it) joins her fights
# there, as does a melee spawn within its own detection radius. The
# analyzer models only the spot's own maps, each alone. An orc sorcerer's
# magic bullet flies until something blocks its line of sight.
SEAM_REACH_TILES = 10
# Danger added per seam caster, the analyzer's own caster bonus.
SEAM_CASTER_DANGER = 20.0
# The analyzer report schema that carries the encounter model's fields.
ENCOUNTER_SCHEMA = 2
# Below this encounter-aware spot safety (the analyzer's blend of the
# expected pull's and the bad pull's cost against her health, which is
# never saturated at 1) a spot counts as risky. With analyzer v3.11.0 and
# her live kit (2026-11-25: level 37, 333 health, melee, 4.25 HP/s), every
# Burnt Forest map she died on rated 0.40 to 0.77, and the maps whose bad
# pull stays under four health bars rated 0.82 and up.
ENCOUNTER_SAFETY_FLOOR = 0.8
# A spot's bad pull (the analyzer's worst_pack: the engaged radius plus
# every wanderer and ally call more likely than not to join) may hold at
# most this many times the profile's max_aggro_pack. Live, on 2026-11-25,
# she died on Burnt Forest maps of bad pull 7 and 8 (one treant is about
# break-even for her heals, three are lethal without a recall).
BAD_PULL_PACK_FACTOR = 3

# Fields copied from each ranked result into the dashboard snapshot.
SUMMARY_FIELDS = (
    "path", "name", "region", "location", "map_paths", "monsters", "xp_hour",
    "money_hour", "loot_hour", "danger", "score", "safety_score",
    "aggressive_spawns", "max_aggro_pack", "max_monster_level", "danger_flags",
    # Approach danger (analyzer v3.10.0): safety_score is the lower of the
    # spot's own safety and the safety of the maps crossed to reach it.
    "spot_safety_score", "approach_status", "approach_origin", "approach_maps",
    "approach_danger", "approach_safety_score", "approach_worst_map",
    "approach_danger_flags", "approach_unknown",
    # The bot's seam exposure (seam_adjusted).
    "seam_aggressive_spawns", "seam_casters", "seam_maps",
    # The encounter model (analyzer v3.11.0, schema_version 2).
    "analyzer_schema_version", "encounter_applied", "encounter_style",
    "encounter_safety", "expected_pack", "worst_pack", "encounter_reasons",
)
# The approach safety an unreachable spot is capped at. The analyzer's walk
# follows exits and tiled seams only (no ships, scripts or keys), so
# "unreachable" means "not modelled", not "deadly": half credit ranks such a
# spot below every spot with a known safe approach in the balanced and safety
# views, but above a known lethal approach.
UNREACHABLE_APPROACH_SAFETY = 0.5
# Below this approach safety (maximum health / the worst approach pack's
# expected damage) crossing to the spot is expected to cost more than her
# whole health bar. The analyzer does not model fight variance, so anything
# short of full safety counts as a dangerous approach.
APPROACH_SAFETY_FLOOR = 1.0


def approach_safety(spot: dict) -> float:
    """A ranked spot's approach safety: an unreachable spot counts as
    UNREACHABLE_APPROACH_SAFETY, a result without approach fields as 1."""
    if spot.get("approach_unknown") is True or spot.get("approach_status") == "unreachable":
        return UNREACHABLE_APPROACH_SAFETY
    score = spot.get("approach_safety_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return 1.0


def risky_approach(spot: dict) -> bool:
    """Whether the way to a ranked spot is unknown or expected to be lethal.

    Autoplay prefers spots with a known, safe approach and farms a risky one
    only when no other spot is usable. Results without approach fields (an
    analyzer before v3.10.0) are not penalized.
    """
    if spot.get("approach_unknown") is True or spot.get("approach_status") == "unreachable":
        return True
    score = spot.get("approach_safety_score")
    return (isinstance(score, (int, float)) and not isinstance(score, bool) and
            score < APPROACH_SAFETY_FLOOR)


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
        return float(value)
    return None


def encounter_rated(spot: dict) -> bool:
    """Whether a ranked spot carries the analyzer's encounter model: its
    report has schema_version ENCOUNTER_SCHEMA or later (stamped on each
    spot as analyzer_schema_version, FarmAnalyzer._bounded) and the model
    ran for a combat style. Otherwise only the older fields are read."""
    version = spot.get("analyzer_schema_version")
    return (isinstance(version, int) and not isinstance(version, bool) and
            version >= ENCOUNTER_SCHEMA and spot.get("encounter_applied") is True)


def spot_safety(spot: dict) -> float:
    """A ranked spot's own safety, without its approach: the analyzer's
    spot_safety_score (v3.10.0; encounter-aware under a combat style since
    v3.11.0), else its safety_score, else 1."""
    for key in ("spot_safety_score", "safety_score"):
        value = _number(spot.get(key))
        if value is not None:
            return value
    return 1.0


def spot_pack(spot: dict) -> float:
    """The pack a ranked spot's pull may bring, as the #130 pack cap reads
    it: the analyzer's bad pull (worst_pack) when the encounter model rated
    the spot, else its largest direct aggro pack (max_aggro_pack)."""
    if encounter_rated(spot):
        worst = _number(spot.get("worst_pack"))
        if worst is not None:
            return worst
    return _number(spot.get("max_aggro_pack")) or 0.0


def pack_cap(spot: dict, profile: "CombatProfile | None") -> int:
    """The largest spot_pack the profile accepts; 0 allows any. A bad pull
    (worst_pack) may hold BAD_PULL_PACK_FACTOR times the direct pack."""
    cap = int(getattr(profile, "max_aggro_pack", 0) or 0)
    return cap * BAD_PULL_PACK_FACTOR if cap and encounter_rated(spot) else cap


def risky_spot(spot: dict) -> bool:
    """Whether one encounter at a ranked spot is expected to cost more than
    her whole health bar (its own safety below APPROACH_SAFETY_FLOOR), or,
    rated by the encounter model, its safety is below
    ENCOUNTER_SAFETY_FLOOR.

    Autoplay prefers spots that are neither this nor risky_approach. Live,
    on 2026-11-23, she farmed the XP view's first spot, an Underground City
    circuit of spot safety 0.95, and died there to three attackers at once.
    """
    floor = ENCOUNTER_SAFETY_FLOOR if encounter_rated(spot) else APPROACH_SAFETY_FLOOR
    return spot_safety(spot) < floor


def encounter_summary(spot: dict, reasons: int = 3) -> str:
    """A short account of the encounter model's rating for the decision
    record, or "" for a spot it did not rate."""
    if not encounter_rated(spot):
        return ""
    safety = _number(spot.get("encounter_safety"))
    expected = _number(spot.get("expected_pack"))
    worst = _number(spot.get("worst_pack"))
    text = (f"{spot.get('encounter_style', '?')} encounter safety "
            f"{safety if safety is not None else 0:.2f}, pack "
            f"{expected if expected is not None else 0:.1f}/"
            f"{worst if worst is not None else 0:g}")
    listed = [str(reason) for reason in (spot.get("encounter_reasons") or ())
              if isinstance(reason, str)][:reasons]
    return text + (": " + "; ".join(listed) if listed else "")


@dataclass(frozen=True)
class SeamExposure:
    """What a spot's seam neighbours add (navigation.WorldGraph.seam_exposure).

    ``pack``: the most aggressive spawns at once at any of the spot's
    spawns where a seam adds to it, a neighbour's or one between the
    spot's own maps (0 where none does): all within
    their detection radius, and the neighbours' casters within
    SEAM_REACH_TILES of her pull square. ``aggressive`` and ``casters``:
    the neighbours' aggressive spawns, and casters among them, within
    SEAM_REACH_TILES of a pull square; ``maps``: those neighbours' maps.
    """

    pack: int = 0
    aggressive: int = 0
    casters: int = 0
    maps: tuple[str, ...] = ()


def seam_adjusted(spot: dict, exposure: SeamExposure,
                  profile: "CombatProfile | None") -> dict | None:
    """A ranked spot with its seam exposure folded in, or None when its
    pack (spot_pack), the exposed one included, exceeds the profile's
    pack_cap.

    The analyzer scores a circuit from its own maps alone, and judges its
    pack per map. Live, on 2026-11-23, it rated the Underground City circuit
    of 1_3, 2_2, 2_3 and 3_2 at pack 3 with casters only from 3_2, while
    three orc sorcerers on 1_2, seamlessly north of 1_3, stood about seven
    tiles from where she fought; they did 259 of the 565 damage that killed
    her. This keeps the analyzer's order and changes only what it missed:
    ``max_aggro_pack`` becomes the larger of the two packs, seam casters
    join ``danger_flags``, and the spot's own safety becomes at most her
    maximum health over the exposed encounter (the spot's danger per
    monster times the pack, plus SEAM_CASTER_DANGER per seam caster);
    ``safety_score`` is at most that. A spot nothing reaches is returned
    as it is.

    A spot the encounter model rated (encounter_rated) is capped by its
    bad pull (worst_pack) instead, and keeps the analyzer's safety: that
    model already counts the spawns on tiled neighbour maps within reach
    of a pull, casters included, so the exposure only annotates it.
    """
    encounter = encounter_rated(spot)
    ranked_pack = spot_pack(spot)
    pack = max(ranked_pack, float(exposure.pack))
    cap = pack_cap(spot, profile)
    if cap and pack > cap:
        return None
    if exposure.aggressive <= 0 and exposure.pack <= 0:
        return spot
    adjusted = dict(spot)
    adjusted.update(
        seam_aggressive_spawns=exposure.aggressive,
        seam_casters=exposure.casters, seam_maps=list(exposure.maps))
    if not encounter:
        adjusted.update(max_aggro_pack=pack, analyzer_max_aggro_pack=ranked_pack)
    if exposure.casters:
        adjusted["danger_flags"] = [
            *(spot.get("danger_flags") or ()), f"seam caster×{exposure.casters}"]
    if encounter:
        return adjusted
    danger = _number(spot.get("danger"))
    health = float(getattr(profile, "max_health", 0) or 0)
    if danger is not None and health > 0 and (
            pack > ranked_pack or exposure.casters):
        encounter = danger * max(pack, 1.0) + SEAM_CASTER_DANGER * exposure.casters
        own = min(spot_safety(spot), min(1.0, health / max(encounter, 0.001)))
        adjusted["spot_safety_score"] = own
        overall = _number(spot.get("safety_score"))
        adjusted["safety_score"] = own if overall is None else min(overall, own)
    return adjusted


def survivability_unchanged(now: dict, earlier: dict) -> bool:
    """CombatProfile.no_survivability_gain on the four fields it compares
    (maximum health, armour class, protections, character level), as plain
    descriptions. A route event keeps only these (route_events.KIT_FIELDS),
    so the learned route cost (route_costs.py) applies the same predicate.
    A missing field counts as 0."""
    def number(data: dict, key: str) -> float:
        try:
            return float(data.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def protections(data: dict) -> float:
        try:
            return float(sum(int(value) for value in dict(data.get("protections") or {}).values()))
        except (TypeError, ValueError, AttributeError):
            return 0.0

    changes = (
        (number(now, "max_health") - number(earlier, "max_health"),
         0.10 * max(number(earlier, "max_health"), 1)),
        (number(now, "armour_class") - number(earlier, "armour_class"), 2),
        (protections(now) - protections(earlier), 5),
        (number(now, "character_level") - number(earlier, "character_level"), 2),
    )
    gain = any(change > tolerance for change, tolerance in changes)
    loss = any(-change > tolerance for change, tolerance in changes)
    return not gain or loss


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
    # a secondary skill trains one target at a time, spots must not pack;
    # otherwise her single-target kit holds SINGLE_TARGET_MAX_AGGRO_PACK.
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
    # The analyzer's encounter model (v3.11.0): how she fights (combat_style)
    # and the HP per second her heal spell sustains in a fight
    # (healing_rate); "" runs without the model.
    combat_style: str = ""
    healing_per_second: float = 0.0

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
        if self.combat_style:
            args += ["--combat-style", self.combat_style,
                     "--healing-per-second", f"{self.healing_per_second:g}"]
        return args

    def materially_differs(self, other: "CombatProfile | None") -> bool:
        """Whether a new analysis could rank spots differently."""
        if other is None:
            return True
        if (self.skill, self.skill_level, self.attack, self.character_level,
                self.max_aggro_pack, self.spell_attack, self.mana_cost,
                self.meditation, self.combat_style) != (
                other.skill, other.skill_level, other.attack, other.character_level,
                other.max_aggro_pack, other.spell_attack, other.mana_cost,
                other.meditation, other.combat_style):
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
                relative(self.healing_per_second, other.healing_per_second, 0.15) or
                relative(self.max_health, other.max_health, 0.10) or
                abs(self.weapon_class - other.weapon_class) >= 2 or
                abs(self.armour_class - other.armour_class) >= 2):
            return True
        old = dict(other.protections)
        new = dict(self.protections)
        return any(abs(new.get(name, 0) - old.get(name, 0)) >= 5
                   for name in set(old) | set(new))

    def no_survivability_gain(self, earlier: "CombatProfile") -> bool:
        """Whether a failure she had with ``earlier`` still applies to her now.

        Only what keeps her alive counts: maximum health, armour class,
        total protections and character level. Skill level and mana move
        constantly while she trains and say little about surviving a pack.
        The test is one-sided: being weaker or equal keeps the failure. It
        stops applying only after a genuine gain beyond the tolerance in
        one of them (maximum health more than 10%, armour class more than
        2, total protections more than 5, character level more than 2)
        with no loss beyond the tolerance in any other: armour class 15
        down for protections 6 up is no improvement. Live, depletion
        dropped Sera's maximum health from 322 to 244; a symmetric "within
        10%" test then stopped counting the deaths she had at full
        strength, so the weaker character walked back into the same fights.
        """
        return survivability_unchanged(
            {"max_health": self.max_health, "armour_class": self.armour_class,
             "protections": dict(self.protections),
             "character_level": self.character_level},
            {"max_health": earlier.max_health, "armour_class": earlier.armour_class,
             "protections": dict(earlier.protections),
             "character_level": earlier.character_level})

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
            "combat_style": self.combat_style,
            "healing_per_second": self.healing_per_second,
        }


def healing_rate(state, heal: float, swing: float) -> float:
    """The HP per second her healing spell sustains in a fight, for the
    analyzer's --healing-per-second; 0 until a heal of it has landed.

    ``heal`` is the median of her landed heal-spell casts
    (FarmAnalyzer.observe_spells; potions and scrolls do not count: they
    run out). In a fight a cast waits for her action timer (COMBAT.md,
    heals in melee), so one lands about every ``swing`` (her weapon's
    speed, or a cast while she trains with spells) plus SPELL_CAST_DELAY.
    Her mana bounds it too: regen_sp over the spell's cost, casts a
    second. Live, on 2026-11-25, minor healing landed 8 to 26 (median
    about 17) with a 3 s weapon: about 4.25 HP/s, against at most 5.5 seen.
    """
    if heal <= 0 or swing <= 0:
        return 0.0
    rate = heal / (swing + SPELL_CAST_DELAY)
    costs = [int(item.extra.get("cost", 0) or 0) for item in state.inventory
             if item.item_type == c.TYPE_SPELL and "heal" in item.name.casefold()]
    try:
        regen = float(state.stats.get("regen_sp", 0) or 0)
    except (TypeError, ValueError):
        regen = 0.0
    cost = min((value for value in costs if value > 0), default=0)
    if cost and regen > 0:
        rate = min(rate, heal * regen / cost)
    return round(rate, 2)


def combat_profile(client, spell_damage: float = 0.0,
                   spell_attack: str = "", heal: float = 0.0) -> CombatProfile | None:
    """Read the killing skill and player doll, or None if unsupported.

    ``spell_damage``/``spell_attack`` are the observed raw damage per hit of
    her damage spell and its attack type, ``heal`` the median of her landed
    heal-spell casts (see FarmAnalyzer.observe_spells).

    Her combat style for the analyzer's encounter model: ``ranged-kite``
    while she trains with spells only (caster_kite holds one target at
    range and steps away), else ``melee``: her weapon fights one target in
    place, and melee_kite steps away only from a lone pursuer, standing
    when another hostile is close (KITE_CROWD_RADIUS). She casts no area
    spell, so never ``aoe-kite``.
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
    # cast has been observed, the weapon stands in. Otherwise she fights one
    # target at a time (she casts no area spell), so spots are capped at
    # SINGLE_TARGET_MAX_AGGRO_PACK.
    max_aggro_pack = SINGLE_TARGET_MAX_AGGRO_PACK
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
    swing = SPELL_CAST_DELAY if caster else weapon_speed
    return CombatProfile(
        skill=school, skill_level=skill_level, attack=attack, damage=damage,
        weapon_class=weapon_class, weapon_speed=round(weapon_speed, 3),
        character_level=character_level, max_health=max_health,
        speed=round(speed, 3), armour_class=armour_class, protections=protections,
        max_aggro_pack=max_aggro_pack,
        combat_style=RANGED_KITE_STYLE if caster else MELEE_STYLE,
        healing_per_second=healing_rate(state, heal, swing), **caster)


class _OriginRejected(RuntimeError):
    """The analyzer refused the approach origin map (tools cli.py raises
    ``SystemExit("unknown approach origin map: ...")``, exit status 1)."""


ORIGIN_REJECTED_TEXT = "unknown approach origin map"


class FarmAnalyzer:
    """Owns at most one background analyzer process and its latest ranking."""

    # Never re-run more often than this, however much the profile changes.
    MIN_INTERVAL_SECONDS = 600.0
    # Re-run an unchanged profile after this long.
    REFRESH_SECONDS = 6 * 3600.0
    # After a failure, retry no sooner than this.
    RETRY_SECONDS = 900.0
    TIMEOUT_SECONDS = 900.0
    # Results kept per objective. Raw rankings list every permutation of a
    # circuit: at Sera's level 36 profile the XP view holds 23 Underground
    # City circuits behind one dangerous entrance (and one unreachable spot)
    # before the first spot with a safe approach, at rank 25, so ten would
    # leave autoplay nothing safe to prefer.
    TOP = 40
    # The safety view is kept deeper (SAFETY_TOP), so the cure-funding farm
    # (navigation._analyzer_farm_choice) can reach the genuinely safer
    # spots. Its score is her maximum health over a spot's danger, capped at
    # 1, so every spot whose danger stays under her health ties at 1, and
    # the analyzer orders the ties by its balanced score. Live, on
    # 2026-11-23 (level 36, 322 health), 126 of 199 ranked spots tied at 1;
    # the first 40 were all Burnt Forest permutations at danger 71 to 126,
    # while the Underground City's outer maps, Hemlock Cave and Eld Woods
    # Island (danger 36 to 66) ranked 60th to 125th. A kit hazard on the
    # Burnt Forest rejected all 40, and she waited for a spot for good.
    # The analyzer scores every candidate in every run, so a deeper list
    # costs only its JSON (about 14 MB at 199 spots, every view at that
    # depth, parsed off the event loop); the other views are cut back to
    # TOP once it is read.
    SAFETY_TOP = 250
    # Circuits of one area (the analyzer's region and name) kept per view
    # but safety. The raw views list every permutation of a circuit: live,
    # on 2026-11-24, the XP view's first 40 were all Burnt Forest circuits,
    # every one rated hazardous for her kit, so she farmed the least
    # dangerous of them 49 map transitions away while the Underground
    # City's outer maps, Hemlock Cave and Eld Woods Island ranked 60th to
    # 125th. Capping the circuits per area leaves room for other areas; it
    # does not ensure that a kept circuit avoids a map that failed (live,
    # every Burnt Forest circuit shared one).
    AREA_CIRCUITS = 3

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
        # The approach origin (her savebed map) the current report was
        # ranked from; empty for the analyzer's default origins.
        self.origin = ""
        # Origins the analyzer rejected; their runs use the default origins.
        self.rejected_origins: set[str] = set()
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
        # HP of her recent landed heal-spell casts (healing_rate).
        self.heal_samples: list[int] = []
        self._message_position = 0

    @classmethod
    def from_bundle(cls, installed: dict, **kwargs) -> "FarmAnalyzer":
        return cls(installed["analyzer"], installed["source"],
                   installed["classic_server"] / "src", **kwargs)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def approach_origin(self, state) -> str:
        """Where her trips start, if known and in the locked content, else "".

        That is her savebed map, or for an apartment savebed its recorded
        return: an apartment is left only through a script, which the
        analyzer's walk does not follow, so from inside it every spot would
        be unreachable (as navigation.route_death_origin judges reach). The
        analyzer rejects an unknown origin map, so a savebed outside the
        content, an apartment without a recorded return, an origin the
        analyzer already rejected, or none yet fall back to its default
        origins.
        """
        path = str(getattr(state, "savebed_map", "") or "")
        if "/apartments/" in path:
            back = (getattr(state, "apartment_returns", {}) or {}).get(path)
            path = str(back[0]) if back else ""
        if path in self.rejected_origins:
            return ""
        relative = Path(path.lstrip("/"))
        if (not path.startswith("/") or ".." in relative.parts or
                not relative.parts or
                not (self.content_root / "maps" / relative).is_file()):
            return ""
        return path

    def command(self, profile: CombatProfile, origin: str = "") -> list[str]:
        """The isolated analyzer invocation for one profile.

        With an ``origin`` (her savebed map) the approach to every spot is
        judged from there alone, as she actually travels.
        """
        bootstrap = (
            "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
            "sys.argv[0] = 'farming_analyzer'; "
            "runpy.run_module('farming_analyzer', run_name='__main__', alter_sys=True)")
        command = [
            self.python, "-E", "-s", "-c", bootstrap, str(self.analyzer_root),
            *profile.arguments(),
            "--content-root", str(self.content_root),
            "--server-root", str(self.server_root),
            "--ranking", "all", "--top", str(max(self.TOP, self.SAFETY_TOP)),
            "--json",
            "--unreachable-approach-safety", f"{UNREACHABLE_APPROACH_SAFETY:g}",
        ]
        if origin:
            command += ["--approach-origin", origin, "--approach-origins-only"]
        nice = shutil.which("nice")
        return [nice, "-n", "10", *command] if nice else command

    def due(self, profile: CombatProfile | None, origin: str = "") -> bool:
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
                origin != self.origin or
                now - self.reported_at >= self.REFRESH_SECONDS)

    def observe_spells(self, client) -> None:
        """Record the raw damage of her damage spell from her own hit text,
        and the HP of each heal-spell cast that lands (healing_rate).

        Only hit text within a few seconds of her own offensive cast counts,
        so elemental melee (rings of fire and the like) is never sampled.
        A heal counts only when her latest FIRE or APPLY, at most
        HEAL_ATTRIBUTION_SECONDS before the server's answer, fired a
        healing spell she knows.
        """
        state = client.state
        entries = state.messages_since(self._message_position)
        self._message_position = state.message_count
        casts = list(getattr(client, "offensive_casts", ()))
        heal_spells = {item.name.casefold() for item in state.inventory
                       if item.item_type == c.TYPE_SPELL and
                       "heal" in item.name.casefold()}
        # Her FIREs and APPLYs, newest last: a heal answer belongs to the
        # latest one before it, and counts only if that FIRE cast a
        # healing spell (an applied potion or scroll does not).
        orders = [(float(record.get("time", 0) or 0), record.get("action"),
                   str(record.get("detail", "")).casefold())
                  for record in list(getattr(client, "action_history", ()) or ())[-50:]
                  if isinstance(record, dict) and
                  record.get("action") in ("fire", "apply")]
        for timestamp, _, _, text in entries:
            healed = HEALED_TEXT.match(text)
            if healed and heal_spells:
                order = next((entry for entry in reversed(orders)
                              if 0.0 <= timestamp - entry[0] <= HEAL_ATTRIBUTION_SECONDS),
                             None)
                if (order is not None and order[1] == "fire" and
                        any(order[2].startswith(name + " ") or order[2] == name
                            for name in heal_spells) and int(healed.group(1)) > 0):
                    self.heal_samples = (
                        self.heal_samples + [int(healed.group(1))])[-HEAL_SAMPLES:]
                continue
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
            self.spell_attack,
            statistics.median(self.heal_samples) if self.heal_samples else 0.0)
        if profile is None:
            if self.report is None and not self.running:
                self.status = "unsupported"
                self.error = "no melee weapon school to simulate"
            return False
        origin = self.approach_origin(client.state)
        if not self.due(profile, origin):
            return False
        self._last_attempt_at = self.clock()
        # Report the run as soon as it is scheduled, not when it first runs.
        self.status = "running"
        self.error = ""
        self.started_at = self._last_attempt_at
        self.runs += 1
        self._task = asyncio.get_running_loop().create_task(self._run(profile, origin))
        return True

    async def _analyze(self, command: list[str]) -> dict:
        """One analyzer process's validated report, or RuntimeError."""
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
            text = stderr.decode(errors="replace")
            detail = text.strip().splitlines()[-1:] or [""]
            error = (_OriginRejected if ORIGIN_REJECTED_TEXT in text
                     else RuntimeError)
            raise error(
                f"exited with status {self._process.returncode}: {detail[0][:300]}")
        report = await asyncio.to_thread(json.loads, stdout)
        rankings = report.get("rankings") if isinstance(report, dict) else None
        if not isinstance(rankings, dict) or not all(
                isinstance(rankings.get(name), list) for name in OBJECTIVES):
            raise RuntimeError("report has no ranking for every objective")
        return self._bounded(report)

    @staticmethod
    def circuit_area(spot) -> tuple[str, str] | None:
        """The area a circuit permutation belongs to (its region and name);
        None for a single-map spot, which is its own area."""
        if not isinstance(spot, dict) or not str(
                spot.get("path", "")).startswith("circuit["):
            return None
        return str(spot.get("region", "")), str(spot.get("name", ""))

    def _distinct(self, ranked: list) -> list:
        """The first TOP spots of a view, at most AREA_CIRCUITS circuits of
        any one area (circuit_area), in rank order."""
        kept: list = []
        per_area: dict[tuple[str, str], int] = {}
        for spot in ranked:
            area = self.circuit_area(spot)
            if area is not None:
                if per_area.get(area, 0) >= self.AREA_CIRCUITS:
                    continue
                per_area[area] = per_area.get(area, 0) + 1
            kept.append(spot)
            if len(kept) >= self.TOP:
                break
        return kept

    def _bounded(self, report: dict) -> dict:
        """The report with its safety view cut back to SAFETY_TOP, every
        other view to TOP spots with at most AREA_CIRCUITS circuits of one
        area (_distinct; the analyzer's --top, SAFETY_TOP, applies to all
        of them), and its unranked results to TOP. Each spot is stamped
        with the report's schema_version (analyzer_schema_version, absent
        before v3.11.0), which encounter_rated checks before any encounter
        field is read."""
        version = report.get("schema_version")
        if isinstance(version, int) and not isinstance(version, bool):
            for spot in [*(spot for ranked in report["rankings"].values()
                           if isinstance(ranked, list) for spot in ranked),
                         *(report.get("results") or ()),
                         *(group.get("representative") for group in report.get("groups") or ()
                           if isinstance(group, dict))]:
                if isinstance(spot, dict):
                    spot["analyzer_schema_version"] = version
        rankings = report["rankings"]
        for name, ranked in list(rankings.items()):
            if isinstance(ranked, list):
                rankings[name] = (ranked[:self.SAFETY_TOP] if name == "safety"
                                  else self._distinct(ranked))
        for name in ("results", "groups"):
            if isinstance(report.get(name), list):
                report[name] = report[name][:self.TOP]
        return report

    async def _run(self, profile: CombatProfile, origin: str = "") -> None:
        log.info("farming analyzer started for %s %d", profile.skill, profile.skill_level)
        try:
            try:
                report = await self._analyze(self.command(profile, origin))
            except _OriginRejected as error:
                if not origin:
                    raise
                # The analyzer refused the origin itself: it is never passed
                # again, and this run retries once from the default origins.
                # Any other failure is an ordinary one.
                log.warning("farming analyzer rejected origin %s (%s); "
                            "retrying from the default origins", origin, error)
                self.rejected_origins.add(origin)
                origin = ""
                report = await self._analyze(self.command(profile))
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
            self.origin = origin
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

    def snapshot(self, objective: str,
                 ranked: list[dict] | None = None) -> dict[str, Any]:
        """The dashboard's view; ``ranked`` replaces the raw ranking (the
        seam-adjusted one, navigation.seam_ranking)."""
        objective = objective if objective in OBJECTIVES else DEFAULT_OBJECTIVE
        ranked = self.ranking(objective) if ranked is None else ranked
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
            "approach_origin": self.origin or None,
            "rejected_origins": sorted(self.rejected_origins),
            "ranking": [
                {**{key: item.get(key) for key in SUMMARY_FIELDS},
                 "approach_risky": risky_approach(item),
                 "spot_risky": risky_spot(item)}
                for item in ranked[:5]
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
