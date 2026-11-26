"""Combat builds: how she fights a spot, and which build she fields there.

A build is one way to fight: her melee weapon, an area spell, a
single-target spell, or a launcher. Each has requirements she must meet to
field it (a known spell her Wizardry can cast, a launcher with ammunition
and its archery skill, a weapon and its school), a combat routine the farm
already has (FarmTask: the melee fight, the Wizardry spell kite, the
launcher pull), the gear scorer's style for its set (equipment.GearScorer,
storage.BUILDS) and the analyzer's encounter style that rates a spot for it
(tools v3.11.0 ``--combat-style``).

The build for a spot is the operator's choice for that spot or for every
spot (the dashboard, kept in her character memory), or on "auto" the
safest build she can field there by the encounter model's per-style
ratings (farm_analyzer.FarmAnalyzer.style_ratings), ranked by XP. A build
she cannot field leaves a gap: the next build she can field is used, the
gap is shown, and the farm circuit runs a "get this build" errand where a
known spell seller can fix it (navigation.LearnSpellTask).

Pure functions of her state, so the circuit, the analyzer and the dashboard
agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import constants as c

# Choices the operator can make per spot or for every spot.
AUTO = "auto"
# Where a choice for every spot is kept (GameState.build_choices).
EVERY_SPOT = "*"


@dataclass(frozen=True)
class Build:
    name: str
    label: str
    # The gear scorer's style (equipment.GEAR_STYLES) for its set.
    gear_style: str
    # The analyzer's encounter style (--combat-style) for its ratings.
    encounter_style: str
    # The FarmTask routine it fights with (for the dashboard).
    routine: str


BUILDS: dict[str, Build] = {
    "melee": Build("melee", "Melee", "melee", "melee",
                   "melee fight with her weapon"),
    "aoe": Build("aoe", "AoE caster", "caster", "aoe-kite",
                 "area spell kite (Wizardry)"),
    "single": Build("single", "Single-target caster", "caster", "ranged-kite",
                    "single-target spell kite (Wizardry)"),
    "bow": Build("bow", "Bow", "bow", "ranged-kite",
                 "launcher pull, then her weapon"),
}
BUILD_NAMES = tuple(BUILDS)
CHOICES = (AUTO, *BUILD_NAMES)

# Spells that strike several monsters at once (cones and swarms), first
# the ones to learn first: the owner's first target is Firestorm from
# Dunowen (Wizardry 15).
AREA_SPELLS = ("firestorm", "icestorm", "bullet storm", "bullet swarm",
               "meteor swarm", "frost nova")
# Damage spells that strike one monster.
SINGLE_SPELLS = ("magic bullet", "firebolt", "frostbolt", "lightning",
                 "forked lightning", "meteor", "asteroid", "magic missile",
                 "cause light wounds")
# A launcher's archery skill by the word in its name.
LAUNCHER_SKILLS = (("crossbow", "crossbow archery"), ("sling", "sling archery"),
                   ("bow", "bow archery"))
# Ammunition a launcher fires, by the same word.
AMMO_WORDS = {"crossbow archery": ("bolt",), "sling archery": ("stone", "pebble"),
              "bow archery": ("arrow",)}
# The bow build needs at least this much ammunition.
MIN_AMMO = 20
# The melee weapon schools (farm_analyzer.WEAPON_SCHOOLS).
WEAPON_SKILLS = ("slash weapons", "impact weapons", "pierce weapons", "cleave weapons")
_BAD = c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED


def skill_level(state: Any, name: str) -> int:
    levels = [int(item.extra.get("level", 0) or 0) for item in state.inventory
              if item.item_type == c.TYPE_SKILL and item.name.casefold() == name]
    return max(levels, default=0)


def known_spells(state: Any) -> set[str]:
    return {item.name.casefold() for item in state.inventory
            if item.item_type == c.TYPE_SPELL}


def castable(state: Any, names: Iterable[str],
             spell_levels: Mapping[str, int]) -> str:
    """The first of ``names`` she knows and her Wizardry can cast (the
    server refuses a spell above her Wizardry level, object_apply_item),
    or ""."""
    known = known_spells(state)
    wizardry = skill_level(state, "wizardry spells")
    return next((name for name in names if name in known and
                 wizardry >= int(spell_levels.get(name, 1) or 1)), "")


def launcher(state: Any) -> tuple[Any, str, int] | None:
    """Her best launcher, its archery skill and the ammunition she carries
    for it, or None."""
    best = None
    for item in state.inventory:
        if item.item_type != c.TYPE_BOW or item.flags & _BAD:
            continue
        name = item.name.casefold()
        skill = next((skill for word, skill in LAUNCHER_SKILLS if word in name), "")
        if not skill:
            continue
        ammo = sum(max(1, int(arrow.quantity or 1)) for arrow in state.inventory
                   if arrow.item_type == c.TYPE_ARROW and not arrow.flags & _BAD and
                   any(word in arrow.name.casefold() for word in AMMO_WORDS[skill]))
        entry = (item, skill, ammo)
        if best is None or (skill_level(state, skill), ammo) > (
                skill_level(state, best[1]), best[2]):
            best = entry
    return best


def gaps(name: str, state: Any, spell_levels: Mapping[str, int]) -> list[str]:
    """Why she cannot field a build now ([] when she can)."""
    if name == "melee":
        school = max(WEAPON_SKILLS, key=lambda skill: skill_level(state, skill))
        weapon = any(item.item_type == c.TYPE_WEAPON and not item.flags & _BAD
                     for item in state.inventory)
        missing = []
        if skill_level(state, school) < 1:
            missing.append("no melee weapon skill")
        if not weapon:
            missing.append("no melee weapon")
        return missing
    if name in ("aoe", "single"):
        names = AREA_SPELLS if name == "aoe" else SINGLE_SPELLS
        if castable(state, names, spell_levels):
            return []
        wizardry = skill_level(state, "wizardry spells")
        known = sorted(known_spells(state) & set(names))
        text = (f"no {'area' if name == 'aoe' else 'single-target'} spell she can cast "
                f"(Wizardry {wizardry}" +
                (f"; {', '.join(known)} needs more" if known else "") + ")")
        devices = sorted({item.name for item in state.inventory
                          if item.item_type in (c.TYPE_WAND, c.TYPE_ROD) and
                          any(spell in item.name.casefold() for spell in names)})
        if devices:
            # The farm fights with spells, not wands: a device is carried
            # with the set (consumables) but fields no build.
            text += f"; {', '.join(devices[:2])} do not count: the farm casts, it does not fire wands"
        return [text]
    if name == "bow":
        found = launcher(state)
        if found is None:
            return ["no launcher"]
        item, skill, ammo = found
        missing = []
        if skill_level(state, skill) < 1:
            missing.append(f"no {skill} skill for {item.name}")
        if ammo < MIN_AMMO:
            missing.append(f"{ammo} ammunition for {item.name} (needs {MIN_AMMO})")
        return missing
    return [f"unknown build {name}"]


def fieldable(state: Any, spell_levels: Mapping[str, int]) -> tuple[str, ...]:
    return tuple(name for name in BUILD_NAMES if not gaps(name, state, spell_levels))


def plan(name: str, state: Any, spell_levels: Mapping[str, int],
         default: tuple[str, str, int]) -> tuple[str, str, int]:
    """The circuit's (combat_skill, combat_spell, until level) for a
    build: melee keeps ``default`` (the automatic training plan,
    farm_analyzer.automatic_training_plan); the casters cast their spell
    with Wizardry for good; the bow trains its archery skill with the
    launcher pull."""
    if name == "aoe":
        return "wizardry spells", castable(state, AREA_SPELLS, spell_levels), 0
    if name == "single":
        return "wizardry spells", castable(state, SINGLE_SPELLS, spell_levels), 0
    if name == "bow":
        found = launcher(state)
        return (found[1] if found else "bow archery"), "", 0
    return default


@dataclass
class Choice:
    """The build for one spot and why."""

    build: str
    # The operator's choice that applied ("auto", or a build name).
    chosen: str = AUTO
    reason: str = ""
    # Gaps of the builds she cannot field (name -> reasons).
    gaps: dict[str, list[str]] = field(default_factory=dict)
    # Whether the operator's build could not be fielded.
    fallback: bool = False
    # The build a "get this build" errand works towards, or "".
    wanted: str = ""


def operator_choice(choices: Mapping[str, str], spot: str) -> str:
    """The operator's choice for a spot: that spot's, else every spot's,
    else auto."""
    for key in (spot, EVERY_SPOT):
        value = choices.get(key) if key else None
        if value in CHOICES:
            return value
    return AUTO


def _rank(build: str, rating: Mapping[str, Any] | None,
          warn: Any) -> tuple:
    """Safest first (no encounter warning), then most XP; unwarned
    builds by XP, warned ones by encounter safety."""
    rating = rating or {}
    safety = float(rating.get("encounter_safety") or 0.0)
    xp = float(rating.get("xp_hour") or 0.0)
    warned = bool(warn(rating))
    return (warned, -safety if warned else -xp, -xp, BUILD_NAMES.index(build))


def choose(state: Any, spot: str, ratings: Mapping[str, Mapping[str, Any] | None],
           choices: Mapping[str, str], spell_levels: Mapping[str, int], *,
           warn: Any = lambda rating: False, current: str = "melee") -> Choice | None:
    """The build she fields on ``spot`` (its key, FarmAnalyzer.spot_key's
    path), or None when she can field none.

    ``ratings`` maps each encounter style to the spot's rating in that
    style (the encounter fields, xp_hour), or None when unrated; ``warn``
    is farm_analyzer.encounter_warning. On auto, of the builds she can
    field and the analyzer rated, the safest (no warning) with the most
    XP; unrated, the build she fields (``current``) while she can, else
    melee (her default plan, plan()).
    """
    missing = {name: gaps(name, state, spell_levels) for name in BUILD_NAMES}
    missing = {name: reasons for name, reasons in missing.items() if reasons}
    ready = [name for name in BUILD_NAMES if name not in missing]
    chosen = operator_choice(choices, spot)
    # The owner's first target is the AoE build: an errand works towards
    # it while she cannot field it, or towards the operator's choice.
    wanted = (chosen if chosen != AUTO and chosen in missing
              else "aoe" if "aoe" in missing else "")
    if not ready:
        return None
    if chosen != AUTO and chosen not in missing:
        return Choice(chosen, chosen, "operator's choice", missing, False, wanted)
    rated = [name for name in ready
             if ratings.get(BUILDS[name].encounter_style)]
    if rated:
        best = min(rated, key=lambda name: _rank(
            name, ratings.get(BUILDS[name].encounter_style), warn))
        rating = ratings.get(BUILDS[best].encounter_style) or {}
        reason = (f"safest she can field: {BUILDS[best].encounter_style} "
                  f"encounter safety {float(rating.get('encounter_safety') or 0):.2f}, "
                  f"XP {float(rating.get('xp_hour') or 0):.0f}/h")
    elif current in ready and current != "melee":
        # Unrated (a new spot), she keeps what she fields: no swap back and
        # forth while a rating comes.
        best = current
        reason = "no encounter ratings yet: the build she fields"
    else:
        # Unrated, her default plan (melee: her weapon, with the automatic
        # Wizardry training).
        best = "melee"
        reason = "no encounter ratings yet: her default plan"
    if chosen != AUTO:
        reason = (f"{BUILDS[chosen].label} needs: " + "; ".join(missing[chosen]) +
                  f"; falling back to {BUILDS[best].label} ({reason})")
    return Choice(best, chosen, reason, missing, chosen != AUTO, wanted)


def fieldable_styles(state: Any, spell_levels: Mapping[str, int]) -> tuple[str, ...]:
    """The encounter styles of the builds she can field, for the
    analyzer's per-style encounter runs."""
    return tuple(dict.fromkeys(BUILDS[name].encounter_style
                               for name in fieldable(state, spell_levels)))


def learn_target(wanted: str, state: Any, offers: Iterable[Any]) -> Any | None:
    """The spell offer (navigation.SpellOffer) that fields ``wanted``: the
    first of its spells, in learning order, that a known seller sells and
    her Wizardry can cast once learnt. None when no seller has one."""
    names = AREA_SPELLS if wanted == "aoe" else SINGLE_SPELLS if wanted == "single" else ()
    known = known_spells(state)
    wizardry = skill_level(state, "wizardry spells")
    offers = list(offers)
    for name in names:
        if name in known:
            continue
        offer = next((offer for offer in offers
                      if offer.spell == name and offer.level <= wizardry), None)
        if offer is not None:
            return offer
    return None
