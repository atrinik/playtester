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
safest build she can field there by the encounter model's rating of each
build's own profile (farm_analyzer.build_profile,
FarmAnalyzer.style_ratings), ranked by XP, with a hold against near ties.
Fielding a caster build is not training: the farm casts its spell while
it is fielded, and the circuit's training plan is unchanged. A build she
cannot field leaves a gap: the next build she can field is used, the gap
is shown, and the farm circuit runs a "get this build" errand where a
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
# The most spots with their own choice kept in her memory.
MAX_BUILD_CHOICES = 200

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


def build_spell(name: str, state: Any, spell_levels: Mapping[str, int]) -> str:
    """The spell a caster build fights with ("" for the others). Fielding
    it is not training: the circuit's training plan (until levels) stays
    as it was, and the farm casts this spell while the build is fielded."""
    if name == "aoe":
        return castable(state, AREA_SPELLS, spell_levels)
    if name == "single":
        return castable(state, SINGLE_SPELLS, spell_levels)
    return ""


def fieldable_spells(state: Any, spell_levels: Mapping[str, int]) -> dict[str, str]:
    """The builds she can field and each one's spell ("" for none), for
    the analyzer's per-build encounter runs."""
    return {name: build_spell(name, state, spell_levels)
            for name in fieldable(state, spell_levels)}


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


# On auto she leaves the build she fields only for a safer one (hers warns,
# the other does not) or one earning at least SWITCH_XP_GAIN more XP an
# hour, and not within HOLD_SECONDS of taking it up: a swap trip and a farm
# restart are not worth a near tie.
SWITCH_XP_GAIN = 0.20
HOLD_SECONDS = 3600.0
# When both warn, one this much safer (encounter safety) wins at once.
SWITCH_SAFETY_GAIN = 0.10


def wanted_build(choices: Mapping[str, str], missing: Mapping[str, list[str]],
                 chosen: str) -> str:
    """The build a "get this build" errand works towards: the operator's
    unmet choice here, else the AoE build while she cannot field it and
    auto or the operator wants it somewhere. With every spot set to
    another build, none."""
    if chosen != AUTO and chosen in missing:
        return chosen
    if "aoe" not in missing:
        return ""
    every = choices.get(EVERY_SPOT, AUTO)
    if every == AUTO or every == "aoe" or "aoe" in choices.values():
        return "aoe"
    return ""


def choose(state: Any, spot: str, ratings: Mapping[str, Mapping[str, Any] | None],
           choices: Mapping[str, str], spell_levels: Mapping[str, int], *,
           warn: Any = lambda rating: False, rated: Any = bool,
           current: str = "melee", held: bool = False) -> Choice | None:
    """The build she fields on ``spot`` (its key, FarmAnalyzer.spot_key's
    path), or None when she can field none.

    ``ratings`` maps each build to the spot's encounter rating with that
    build's own profile (FarmAnalyzer.style_ratings), or None; only a
    rating ``rated`` accepts (farm_analyzer.encounter_rated) counts, and
    ``warn`` is farm_analyzer.encounter_warning. An operator's choice
    applies at once. On auto, of the builds she can field and the analyzer
    rated, the safest (no warning) with the most XP; she keeps ``current``
    unless another is safer (hers warns and the other does not, or both
    warn and it is SWITCH_SAFETY_GAIN safer: at once) or
    earns SWITCH_XP_GAIN more (not while ``held``, HOLD_SECONDS since she
    took it up). Unrated, she keeps ``current``
    while she can field it, else melee (her default plan).
    """
    missing = {name: gaps(name, state, spell_levels) for name in BUILD_NAMES}
    missing = {name: reasons for name, reasons in missing.items() if reasons}
    ready = [name for name in BUILD_NAMES if name not in missing]
    chosen = operator_choice(choices, spot)
    wanted = wanted_build(choices, missing, chosen)
    if not ready:
        return None
    if chosen != AUTO and chosen not in missing:
        return Choice(chosen, chosen, "operator's choice", missing, False, wanted)
    scored = {name: ratings.get(name) for name in ready
              if ratings.get(name) and rated(ratings.get(name))}

    def xp(name: str) -> float:
        return float((scored.get(name) or {}).get("xp_hour") or 0.0)

    def safety(name: str) -> float:
        return float((scored.get(name) or {}).get("encounter_safety") or 0.0)

    if scored:
        best = min(scored, key=lambda name: (
            bool(warn(scored[name])), -safety(name) if warn(scored[name]) else -xp(name),
            -xp(name), BUILD_NAMES.index(name)))
        if current in scored and best != current:
            safer = bool(warn(scored[current])) and (
                not warn(scored[best]) or
                safety(best) >= safety(current) + SWITCH_SAFETY_GAIN)
            richer = (not warn(scored[best]) and
                      xp(best) >= xp(current) * (1.0 + SWITCH_XP_GAIN))
            # Safety does not wait for the hold.
            if not safer and (held or not richer):
                best = current
        reason = (f"{'kept' if best == current else 'safest she can field'}: "
                  f"{BUILDS[best].encounter_style} encounter safety {safety(best):.2f}, "
                  f"XP {xp(best):.0f}/h")
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


# What her memory keeps of her spell samples (FarmAnalyzer.observe_spells).
MAX_SAMPLED_SPELLS = 20
MAX_SPELL_SAMPLES = 30


def load_spell_samples(value: Any) -> dict[str, dict]:
    """Her saved hits per spell, validated and bounded."""
    if not isinstance(value, dict):
        return {}
    samples: dict[str, dict] = {}
    for spell, entry in list(value.items())[-MAX_SAMPLED_SPELLS:]:
        if not isinstance(spell, str) or not isinstance(entry, dict):
            continue
        hits = [int(hit) for hit in entry.get("hits", ()) or ()
                if isinstance(hit, int) and not isinstance(hit, bool) and 0 < hit <= 100_000]
        attack = entry.get("attack", "")
        if hits and isinstance(attack, str):
            samples[spell.casefold()[:64]] = {"hits": hits[-MAX_SPELL_SAMPLES:],
                                              "attack": attack[:32]}
    return samples


def load_build_state(value: Any) -> dict:
    """The saved build state (active_build, active_build_since,
    pending_swap_build), validated."""
    if not isinstance(value, dict):
        return {}
    state: dict = {}
    for name in ("active_build", "pending_swap_build"):
        if value.get(name) in ("", *BUILD_NAMES):
            state[name] = value[name]
    since = value.get("active_build_since")
    if isinstance(since, (int, float)) and not isinstance(since, bool) and since >= 0:
        state["active_build_since"] = float(since)
    return state


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
