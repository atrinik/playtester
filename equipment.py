"""Effective item stats: what a piece of gear really adds when she wears it.

Phase F3 of docs/LEARNING_AND_FEEDBACK.md (4.8.1 and 4.8.2; "F3 as built"
there records where this differs from the design). The rules follow the
Classic server's ``living_update_player_item`` (``server/living.c``), its
item descriptions (``server/item.c``), magic bonuses
(``server/treasure.c``) and missiles (``types/arrow.c``):

- An item's "+N" (its ``magic``) adds N to each of AC, block and absorb
  that the item has a base value for, and, unless the item is a launcher,
  to WC and damage likewise. A base of zero gets nothing, so "+2" on a
  cape, which has no AC, adds no AC.
- A launcher's "+N" goes to each arrow it fires instead, as does an
  arrow's own. A launcher adds no protections, attacks, speed, hit points
  or mana; worn ammunition and a light source add nothing at all.
- Protections and attacks scale with the item's condition, as does a
  wielded weapon's damage.
- Examine text shows the base ``(wc+N)(dam+N)(ac+N)...`` without the
  magic, while the name shows "+N" separately. Rings, amulets and
  trinkets never show "+N".

- An item's ``(speed+N)`` (its ``stats.exp``) adds N/3 before the armour
  speed cap and 1 + N/3 after it, a tenth each (``living_update_player``);
  the lowest speed cap of her worn armour clamps the first part.

Carried gear is examined once a session by ``GearExaminer``, on a quiet
farm tick, one item at a time. ``GearScorer`` decides what she wears and
buys (phase F4): the effective stats valued for her style (melee, caster or
bow), with speed, the armour speed cap and encumbrance recomputed by the
server's formula. An item with no examine data falls back to the name-based
``InventoryPolicy.gear_score``; ``equipment_report`` still logs where that
old score would choose differently (the shadow log).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

try:
    from tools.world_content_audit import load_archetypes
except ModuleNotFoundError:
    from world_content_audit import load_archetypes

from . import constants as c
from .model import Item
from .observations import authored_item_name

log = logging.getLogger(__name__)

# Server ticks a second (MAX_TICKS): weapon delays are in ticks.
TICKS_PER_SECOND = 8

# The gear types she can wear, with a family label for the panel.
GEAR_LABELS: dict[int, str] = {
    c.TYPE_WEAPON: "Weapon",
    c.TYPE_BOW: "Launcher",
    c.TYPE_ARMOUR: "Body armour",
    c.TYPE_SHIELD: "Shield",
    c.TYPE_HELMET: "Helmet",
    c.TYPE_CLOAK: "Cloak",
    c.TYPE_GIRDLE: "Girdle",
    c.TYPE_PANTS: "Legs",
    c.TYPE_BOOTS: "Boots",
    c.TYPE_GLOVES: "Gloves",
    c.TYPE_BRACERS: "Bracers",
    c.TYPE_AMULET: "Amulet",
    c.TYPE_RING: "Ring",
    c.TYPE_LIGHT_APPLY: "Light",
}
# A generic family name for notes when the archetype is not known.
FAMILY_NAMES: dict[int, str] = {
    c.TYPE_WEAPON: "weapon", c.TYPE_BOW: "launcher", c.TYPE_ARROW: "arrow",
    c.TYPE_ARMOUR: "body armour", c.TYPE_SHIELD: "shield",
    c.TYPE_HELMET: "helmet", c.TYPE_CLOAK: "cloak", c.TYPE_GIRDLE: "girdle",
    c.TYPE_PANTS: "legwear", c.TYPE_BOOTS: "pair of boots",
    c.TYPE_GLOVES: "pair of gloves", c.TYPE_BRACERS: "pair of bracers",
    c.TYPE_AMULET: "amulet", c.TYPE_RING: "ring", c.TYPE_TRINKET: "trinket",
    c.TYPE_LIGHT_APPLY: "light",
}
# IS_ARMOR (include/define.h): the types with a speed cap.
ARMOUR_TYPES = frozenset((
    c.TYPE_ARMOUR, c.TYPE_SHIELD, c.TYPE_HELMET, c.TYPE_CLOAK, c.TYPE_BOOTS,
    c.TYPE_GLOVES, c.TYPE_BRACERS, c.TYPE_GIRDLE, c.TYPE_PANTS))
# IS_WEAPON: no hit points, mana or regeneration from these.
WEAPON_TYPES = frozenset((c.TYPE_ARROW, c.TYPE_BOW, c.TYPE_WEAPON))
# Rings, amulets and trinkets show their description, never "+N", in the
# name (item.c object_get_title).
MAGIC_HIDDEN_TYPES = frozenset((c.TYPE_RING, c.TYPE_AMULET, c.TYPE_TRINKET))
STAT_NAMES = ("Str", "Dex", "Con", "Int", "Pow")
SPECIALS = ("see invisible", "makes ethereal", "ethereal", "makes invisible",
            "invisible", "xray-vision", "infravision", "lifesaving",
            "reflect spells", "reflect missiles", "stealth", "flying")
PHYSICAL_ATTACKS = frozenset(("impact", "slash", "cleave", "pierce"))
# GearScorer's weights. They match the shop examine score
# (BuyShopUpgradeTask._apply_examine_metadata) so the two scales compare.
PROTECTION_WEIGHT = 8
SPECIAL_WEIGHT = 50


@dataclass(frozen=True, slots=True)
class BaseStats:
    """An item's own values, magic excluded, as examine or its archetype shows."""

    ac: int = 0
    wc: int = 0
    dam: int = 0
    block: int = 0
    absorb: int = 0
    # Armour speed cap; examine shows it with the magic already applied.
    speed_cap: float | None = None
    # A weapon's attack delay in seconds.
    delay: float | None = None
    protections: tuple[tuple[str, int], ...] = ()
    attacks: tuple[tuple[str, int], ...] = ()
    stats: tuple[tuple[str, int], ...] = ()
    maxhp: int = 0
    maxsp: int = 0
    # Regeneration points (the server's stats.sp and stats.hp); examine
    # shows 0.4 a point.
    regen_sp: int = 0
    regen_hp: int = 0
    specials: tuple[str, ...] = ()
    source: str = "examine"          # examine | archetype
    # The item's "(speed+N)" (its stats.exp, item.c).
    speed: int = 0


def base_to_json(base: BaseStats) -> dict[str, Any]:
    """A plain form for the item's extras, which the dashboard serializes."""
    return {name: getattr(base, name) for name in BaseStats.__slots__}


def base_from_json(value: Any) -> BaseStats | None:
    if not isinstance(value, dict):
        return None
    fields: dict[str, Any] = {}
    for name in BaseStats.__slots__:
        if name not in value:
            continue
        field_value = value[name]
        if name in ("protections", "attacks", "stats"):
            field_value = tuple((str(key), int(number)) for key, number in field_value)
        elif name == "specials":
            field_value = tuple(str(entry) for entry in field_value)
        fields[name] = field_value
    try:
        return BaseStats(**fields)
    except TypeError:
        return None


def _pairs(pattern: str, text: str) -> tuple[tuple[str, int], ...]:
    return tuple((name.strip().casefold(), int(value))
                 for name, value in re.findall(pattern, text, re.I))


def parse_description(text: str) -> BaseStats | None:
    """The base stats in an item's examine line (``item.c``
    ``object_get_description``), or None when it shows none.

    Generalizes the shop examine reader: only the exact parenthesised forms
    the server prints count, so a word in an item's name never does.
    """
    def one(name: str, suffix: str = "") -> int:
        match = re.search(rf"\({name}([+-]\d+){suffix}\)", text, re.I)
        return int(match.group(1)) if match else 0

    def regen(name: str) -> int:
        match = re.search(rf"\({name} reg\.\s*([+-]\d+(?:\.\d+)?)\)", text, re.I)
        return round(float(match.group(1)) / 0.4) if match else 0

    protections = re.search(r"\(Protections:\s*([^)]*)\)", text, re.I)
    attacks = re.search(r"\(Attacks:\s*([^)]*)\)", text, re.I)
    speed = re.search(r"\(speed cap (\d+(?:\.\d+)?)\)", text, re.I)
    delay = (re.search(r"\((\d+(?:\.\d+)?) sec\)", text, re.I) or
             # A launcher's or arrow's: "(delay+1.0s)" (item.c).
             re.search(r"\(delay\+(\d+(?:\.\d+)?)s\)", text, re.I))
    base = BaseStats(
        ac=one("ac"), wc=one("wc"), dam=one("dam"), block=one("block"),
        absorb=one("absorb", "%"),
        speed_cap=float(speed.group(1)) if speed else None,
        delay=float(delay.group(1)) if delay else None,
        protections=_pairs(r"([a-z ]+?)\s*([+-]\d+)%",
                           protections.group(1)) if protections else (),
        attacks=_pairs(r"([a-z ]+?)\s*\+(\d+)%",
                       attacks.group(1)) if attacks else (),
        stats=tuple((name.capitalize(), int(value)) for name, value in
                    re.findall(r"\((Str|Dex|Con|Int|Pow)([+-]\d+)\)", text, re.I)),
        maxhp=one("hp"), maxsp=one("mana"),
        regen_sp=regen("mana"), regen_hp=regen("hp"),
        specials=tuple(name for name in SPECIALS
                       if f"({name})" in text.casefold()),
        speed=one("speed"),
    )
    return None if base == BaseStats() else base


def magic_from_name(name: str, item_type: int) -> int:
    """The "+N" an identified item's name shows; 0 when none or hidden."""
    if item_type in MAGIC_HIDDEN_TYPES:
        return 0
    bare = re.sub(r"\([^()]*\)", " ", name)
    match = re.search(r"(?:^|\s)([+-]\d+)(?=\s|$)", bare)
    return int(match.group(1)) if match else 0


def _number(attrs: dict[str, Any], name: str) -> int:
    try:
        return int(attrs.get(name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def archetype_base(attrs: dict[str, Any]) -> BaseStats:
    """An archetype's base stats, in the examine text's terms."""
    item_type = _number(attrs, "type")
    armour = item_type in ARMOUR_TYPES
    weapon = item_type == c.TYPE_WEAPON
    wearable = item_type not in WEAPON_TYPES
    return BaseStats(
        ac=_number(attrs, "ac"), wc=_number(attrs, "wc"), dam=_number(attrs, "dam"),
        block=_number(attrs, "block"), absorb=_number(attrs, "absorb"),
        speed_cap=(_number(attrs, "last_sp") / 10.0
                   if armour and _number(attrs, "last_sp") else None),
        delay=(_number(attrs, "last_grace") / TICKS_PER_SECOND
               if weapon and _number(attrs, "last_grace") else None),
        protections=tuple((key[len("protect_"):].replace("_", " "), _number(attrs, key))
                          for key in sorted(attrs)
                          if key.startswith("protect_") and _number(attrs, key)),
        attacks=tuple((key[len("attack_"):].replace("_", " "), _number(attrs, key))
                      for key in sorted(attrs)
                      if key.startswith("attack_") and _number(attrs, key) > 0),
        stats=tuple((name, _number(attrs, name)) for name in STAT_NAMES
                    if _number(attrs, name)),
        maxhp=_number(attrs, "maxhp") if wearable else 0,
        maxsp=_number(attrs, "maxsp") if wearable else 0,
        regen_sp=_number(attrs, "sp") if wearable else 0,
        regen_hp=_number(attrs, "hp") if wearable else 0,
        source="archetype",
        speed=_number(attrs, "exp"),
    )


@lru_cache(maxsize=1)
def archetypes_by_face() -> dict[tuple[str, int], tuple[tuple[str, BaseStats], ...]]:
    """Wearable archetypes by (face, type): their names and base stats."""
    index: dict[tuple[str, int], list[tuple[str, BaseStats]]] = {}
    for record in load_archetypes().values():
        attrs = record.get("attrs", {})
        item_type = _number(attrs, "type")
        face = str(attrs.get("face", "") or "").casefold()
        if item_type not in FAMILY_NAMES or not face:
            continue
        entry = (str(attrs.get("name", "") or ""), archetype_base(attrs))
        values = index.setdefault((face, item_type), [])
        if entry not in values:
            values.append(entry)
    return {key: tuple(values) for key, values in index.items()}


def archetype_for(face: str, item_type: int) -> tuple[str, BaseStats] | None:
    """The archetype a carried item's face and type name, if unambiguous.

    Faces are shared: when the archetypes sharing one differ in base stats,
    nothing is assumed. The name is kept only when they all share it.
    """
    entries = archetypes_by_face().get((face.casefold(), item_type), ())
    if not entries or len({base for _, base in entries}) != 1:
        return None
    names = {name for name, _ in entries}
    return (names.pop() if len(names) == 1 else "", entries[0][1])


@dataclass(frozen=True, slots=True)
class EffectiveStats:
    """What an item adds when worn (or, for a launcher, to each arrow)."""

    item_type: int
    magic: int = 0
    condition: int = 100
    ac: int = 0
    wc: int = 0
    dam: float = 0.0
    block: int = 0
    absorb: int = 0
    # Added to each fired arrow's WC and damage (launchers and arrows).
    missile_wc: int = 0
    missile_dam: int = 0
    delay: float | None = None
    speed_cap: float | None = None
    protections: tuple[tuple[str, float], ...] = ()
    attacks: tuple[tuple[str, float], ...] = ()
    stats: tuple[tuple[str, int], ...] = ()
    maxhp: int = 0
    maxsp: int = 0
    regen_sp: int = 0
    regen_hp: int = 0
    specials: tuple[str, ...] = ()
    source: str = "examine"          # examine | archetype
    notes: tuple[str, ...] = ()
    # "(speed+N)": counts unless the item is a launcher (living.c).
    speed: int = 0


def _plus(value: int) -> str:
    return f"{value:+d}"


def _on(family: str) -> tuple[str, str]:
    """"a cape" and "it", or "sandals" and "they", for a note."""
    if family.endswith("s") and not family.endswith("ss"):
        return family, "they have"
    return f"{'an' if family[:1] in 'aeiou' else 'a'} {family}", "it has"


def effective_stats(item_type: int, base: BaseStats, magic: int = 0,
                    condition: int = 100, family: str = "") -> EffectiveStats:
    """Apply the server's rules for "+N" and condition to an item's base."""
    family = family or FAMILY_NAMES.get(item_type, "item")
    # 255 is the protocol's "not sent" (an unidentified item).
    condition = 100 if condition == 255 else condition
    scale = max(0, min(100, condition)) / 100.0
    notes: list[str] = []
    if item_type == c.TYPE_LIGHT_APPLY:
        # The light slot is skipped (living_update_player): a light adds
        # nothing to her, whatever it is.
        if magic:
            notes.append(f"{_plus(magic)} on {_on(family)[0]} adds nothing: "
                         "a light gives no stats when worn")
        return EffectiveStats(item_type, magic, condition, source=base.source,
                              notes=tuple(notes))
    if item_type == c.TYPE_ARROW:
        # The ammunition slot is skipped too; its magic counts when fired.
        if magic:
            notes.append(f"{_plus(magic)} adds WC and damage {_plus(magic)} "
                         "when it is fired")
        return EffectiveStats(item_type, magic, condition,
                              missile_wc=base.wc + magic, missile_dam=base.dam + magic,
                              source=base.source, notes=tuple(notes))
    ranged = item_type == c.TYPE_BOW
    ac = base.ac + magic if base.ac else 0
    block = base.block + magic if base.block else 0
    absorb = base.absorb + magic if base.absorb else 0
    wc = base.wc + magic if base.wc and not ranged else 0
    dam: float = base.dam + magic if base.dam and not ranged else 0
    if item_type == c.TYPE_WEAPON:
        dam = round(dam * scale, 1)
    protections: tuple[tuple[str, float], ...] = ()
    attacks: tuple[tuple[str, float], ...] = ()
    if not ranged:
        protections = tuple((name, round(value * scale, 1) if value > 0 else float(value))
                            for name, value in base.protections)
        attacks = tuple((name, round(value * scale, 1)) for name, value in base.attacks)
    wearable = item_type not in WEAPON_TYPES
    if ranged:
        if magic:
            notes.append(f"{_plus(magic)} adds WC and damage {_plus(magic)} "
                         "to each arrow it fires")
        if base.protections:
            notes.append("a launcher's protections do not count")
    elif magic and item_type not in MAGIC_HIDDEN_TYPES:
        added = [label for label, value in (("AC", base.ac), ("WC", base.wc),
                                            ("damage", base.dam), ("block", base.block),
                                            ("absorb", base.absorb)) if value]
        if not added:
            what, has = _on(family)
            notes.append(f"{_plus(magic)} on {what} adds no AC: {has} no base AC, "
                         "WC, damage, block or absorb")
        else:
            notes.append(f"{_plus(magic)} adds " + ", ".join(
                f"{label} {_plus(magic)}{'%' if label == 'absorb' else ''}"
                for label in added))
    if condition < 100 and (protections or attacks or
                            item_type == c.TYPE_WEAPON and base.dam):
        scaled = [what for what, present in (
            ("protections", protections), ("attacks", attacks),
            ("damage", item_type == c.TYPE_WEAPON and base.dam)) if present]
        notes.append(f"condition {condition}% scales its " + " and ".join(scaled))
    if base.source == "archetype":
        notes.append("not examined yet: base stats from its archetype")
    return EffectiveStats(
        item_type, magic, condition, ac=ac, wc=wc, dam=dam, block=block,
        absorb=absorb,
        missile_wc=base.wc + magic if ranged else 0,
        missile_dam=base.dam + magic if ranged else 0,
        delay=base.delay, speed_cap=base.speed_cap,
        protections=protections, attacks=attacks, stats=base.stats,
        maxhp=base.maxhp if wearable else 0, maxsp=base.maxsp if wearable else 0,
        regen_sp=base.regen_sp if wearable else 0,
        regen_hp=base.regen_hp if wearable else 0,
        specials=base.specials, source=base.source, notes=tuple(notes),
        speed=0 if ranged else base.speed)


def item_base(client: Any, item: Item) -> tuple[str, BaseStats] | None:
    """An item's family name and base stats: from its examine, else its
    archetype; None when neither is known."""
    faces = getattr(client, "faces", {}) or {}
    known = archetype_for(str(faces.get(item.face, "") or ""), item.item_type)
    family = known[0] if known is not None else ""
    examined = item.extra.get("gear_examine")
    if isinstance(examined, dict) and examined.get("name") == item.name:
        base = base_from_json(examined.get("base")) or BaseStats()
        if base != BaseStats() or item.item_type not in MAGIC_HIDDEN_TYPES:
            return family, base
    if item.item_type in MAGIC_HIDDEN_TYPES:
        # An untitled ring, amulet or trinket shows its description in its
        # name, and examine does not repeat it (item.c object_get_title,
        # object_get_name_description).
        shown = parse_description(item.name)
        if shown is not None:
            return family, shown
    return known


def item_effective_stats(client: Any, item: Item) -> EffectiveStats | None:
    """What a carried item adds when worn, or None when its base is unknown."""
    known = item_base(client, item)
    if known is None:
        return None
    family, base = known
    return effective_stats(item.item_type, base, magic_from_name(item.name, item.item_type),
                           item.condition, family)


def stat_lines(stats: EffectiveStats) -> list[tuple[str, str]]:
    """Labelled values for the dashboard, nonzero only."""
    lines: list[tuple[str, str]] = []

    def add(label: str, value: float, suffix: str = "", signed: bool = False) -> None:
        if value:
            shown = f"{value:g}" if not signed else f"{value:+g}"
            lines.append((label, f"{shown}{suffix}"))

    add("AC", stats.ac)
    add("WC", stats.wc)
    add("Damage", stats.dam)
    add("Block", stats.block)
    add("Absorb", stats.absorb, "%")
    add("Arrow WC", stats.missile_wc, signed=True)
    add("Arrow damage", stats.missile_dam, signed=True)
    if stats.delay:
        lines.append(("Delay", f"{stats.delay:g} s"))
    if stats.speed_cap:
        lines.append(("Speed cap", f"{stats.speed_cap:.2f}"))
    add("Speed", stats.speed, signed=True)
    for name, value in stats.protections:
        add(name.capitalize(), value, "%", signed=True)
    for name, value in stats.attacks:
        add(f"Attack {name}", value, "%")
    for name, value in stats.stats:
        add(name, value, signed=True)
    add("HP", stats.maxhp, signed=True)
    add("Mana", stats.maxsp, signed=True)
    add("Mana regen", stats.regen_sp, signed=True)
    add("HP regen", stats.regen_hp, signed=True)
    lines.extend((name, "yes") for name in stats.specials)
    return lines


def stat_deltas(new: EffectiveStats, old: EffectiveStats | None) -> list[tuple[str, float]]:
    """Per-stat differences of a candidate over what she wears."""
    def numbers(stats: EffectiveStats | None) -> dict[str, float]:
        if stats is None:
            return {}
        values = {"AC": stats.ac, "WC": stats.wc, "Damage": stats.dam,
                  "Block": stats.block, "Absorb %": stats.absorb,
                  "Arrow WC": stats.missile_wc, "Arrow damage": stats.missile_dam,
                  "HP": stats.maxhp, "Mana": stats.maxsp,
                  "Mana regen": stats.regen_sp, "HP regen": stats.regen_hp,
                  "Speed": stats.speed, "Speed cap": stats.speed_cap or 0}
        values.update((f"{name.capitalize()} %", value) for name, value in stats.protections)
        values.update((f"Attack {name} %", value) for name, value in stats.attacks)
        values.update(stats.stats)
        return values

    mine, theirs = numbers(new), numbers(old)
    deltas = []
    for label in dict.fromkeys([*mine, *theirs]):
        delta = round(mine.get(label, 0) - theirs.get(label, 0), 1)
        if delta:
            deltas.append((label, delta))
    return deltas


# The server's per-stat tables (server/living.c), indexed by the stat (0-30).
SPEED_BONUS = (
    -0.4, -0.4, -0.3, -0.3, -0.2, -0.2, -0.2, -0.1, -0.1, -0.1,
    -0.05, 0.0, 0.0, 0.0, 0.025, 0.05, 0.075, 0.1, 0.125, 0.15,
    0.175, 0.2, 0.225, 0.25, 0.275, 0.3, 0.325, 0.35, 0.4, 0.45, 0.5)
DAM_BONUS = (
    -2.5, -2.0, -2.0, -1.5, -1.5, -1.5, -1.0, -1.0, -1.0, -0.5,
    -0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.5, 1.0, 1.0,
    1.5, 1.5, 1.5, 2.0, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0)
WC_BONUS = (
    -5, -4, -4, -3, -3, -3, -2, -2, -2, -1, -1, 0, 0, 0, 0, 0,
    1, 1, 2, 2, 3, 3, 3, 4, 4, 5, 5, 5, 6, 7, 8)
HP_BONUS = (
    -0.8, -0.6, -0.5, -0.4, -0.35, -0.3, -0.25, -0.2, -0.15, -0.11,
    -0.07, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.15, 0.2, 0.25,
    0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0)
# weight_limit[Str] in kg (the server's grams / 1000, as the stat is sent).
WEIGHT_LIMIT = (
    20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150,
    165, 180, 195, 210, 225, 240, 255, 270, 285, 300, 325, 350, 375, 400, 450)
# A player's archetype speed, the speed cap with no armour (max_speed), the
# floor (PLAYER_MIN_SPEED) and the encumbrance start (ENCUMBRANCE_LIMIT).
PLAYER_BASE_SPEED = 0.85
UNCAPPED_SPEED = 9.0
PLAYER_MIN_SPEED = 0.10
ENCUMBRANCE_START = 0.65
# Above this share of her weight limit the scorer starts weighing an item's
# weight, ahead of the real slowdown at ENCUMBRANCE_START.
ENCUMBRANCE_SCORED = 0.50

GEAR_STYLES = ("melee", "caster", "bow")
# Scorer weights, on the shop examine score's scale (AC +1 = 20, a protection
# percent = PROTECTION_WEIGHT). Damage per second counts DPS_WEIGHT a point
# after LEVEL_DAMAGE and the Str bonus; a speed of 1.0 counts SPEED_WEIGHT
# (+0.1, a tenth faster walking and commands, is worth an AC+7).
DPS_WEIGHT = 10
WC_WEIGHT = 10
SPEED_WEIGHT = 1500
# Speed is her only escape without a working recall. SPEED_WEIGHT and this
# factor are provisional until the owner settles them (2026-11-26).
NO_RECALL_SPEED_FACTOR = 1.5
# Which items an item's weight is charged to (GearScorer weight_for): none
# when she swaps between items she carries anyway (the equip loop); an
# item not yet hers (a shop purchase adds it); an unworn carried item (the
# surplus sale sheds it).
WEIGHT_NONE, WEIGHT_UNOWNED, WEIGHT_UNWORN = "none", "unowned", "unworn"
# A stat point is worth at least this, for the next table step.
STAT_FLOOR = 5
# What melee damage and WC are worth to a caster, and arrow WC and damage
# to anyone but an archer, as a share of their full worth.
OFF_STYLE_SHARE = 0.25
# Arrow damage is taken per second over the launcher's delay, its examine
# "(delay+N.Ns)"; this stands in when it is not known.
BOW_REFERENCE_DELAY = 1.0
# A plain arrow's own delay (last_grace 7 ticks), added to the launcher's.
ARROW_REFERENCE_DELAY = 7 / TICKS_PER_SECOND


def _table(values: tuple, stat: int) -> Any:
    return values[max(0, min(len(values) - 1, int(stat)))]


def level_damage(level: int) -> float:
    """LEVEL_DAMAGE (include/living.h): the skill level's damage factor."""
    return 0.75 + level * 0.25 if level > 0 else 1.0


def player_speed(dex: int, worn: Any, carried: float = 0.0,
                 limit: float = 0.0) -> float:
    """Her speed as ``living_update_player`` computes it from the worn
    items' effective stats: the Dex bonus and N/3 per "(speed+N)" item,
    clamped to the lowest armour speed cap, then 1 + N/3 per item a tenth
    after the cap, then encumbrance above 65% of her weight limit."""
    speed = PLAYER_BASE_SPEED + _table(SPEED_BONUS, dex)
    worn = [stats for stats in worn if stats is not None]
    added = sum(stats.speed / 3.0 if stats.speed > 0 else float(stats.speed)
                for stats in worn if stats.speed)
    bonus = sum(1.0 + stats.speed / 3.0 for stats in worn if stats.speed > 0)
    if added >= 0:
        speed += added / 10.0
    else:
        speed /= 1.0 - added
    cap = min((stats.speed_cap for stats in worn
               if stats.speed_cap and stats.item_type in ARMOUR_TYPES),
              default=UNCAPPED_SPEED)
    speed = min(speed, cap) + bonus / 10.0
    return max(PLAYER_MIN_SPEED, speed * encumbrance_factor(carried, limit))


def encumbrance_factor(carried: float, limit: float) -> float:
    """The server's encumbrance multiplier: 1 up to 65% of the limit, then
    linearly down to 0 at the limit."""
    if limit <= 0 or carried <= limit * ENCUMBRANCE_START:
        return 1.0
    if carried >= limit:
        return 0.0
    return (limit - carried) / (limit * (1.0 - ENCUMBRANCE_START))


def gear_style(combat_skill: str, caster: bool) -> str:
    """Her scoring style from her build: caster while Wizardry trains, bow
    when she trains an archery skill, melee otherwise."""
    if caster:
        return "caster"
    return "bow" if "archery" in (combat_skill or "").casefold() else "melee"


def _stat_total(stats: EffectiveStats | None, name: str) -> int:
    return 0 if stats is None else sum(value for key, value in stats.stats if key == name)


class GearScorer:
    """The one gear decider: the equip loop (``FarmTask.maintain_inventory``),
    shop upgrades (``BuyShopUpgradeTask``) and the surplus sale use it.

    An item with examine data is valued by its effective stats for her
    style; one without falls back to ``fallback`` (the name-based score).
    Two items are compared on one scale only: when either lacks examine
    data, both take the fallback (``compare``).

    Context terms are the real change on her: an item is valued against her
    worn set without whatever it would replace (its slot; for a ring, see
    ``_rest``), so speed (with the armour cap clamp), the stat tables
    and caster gains count only what they add at her stats.
    """

    def __init__(self, client: Any, policy: Any, fallback: Callable[[Item], int],
                 *, style: str | None = None, recall: bool = True,
                 weight_for: str = WEIGHT_NONE):
        self.client, self.policy, self.fallback = client, policy, fallback
        self.weight_for = weight_for
        style = style or getattr(policy, "gear_style", "") or (
            "caster" if getattr(policy, "caster_stats", False) else "melee")
        self.style = style if style in GEAR_STYLES else "melee"
        self.recall = bool(recall)
        state = client.state
        stats = getattr(state, "stats", {}) or {}

        def stat(name: str, default: int) -> int:
            try:
                return int(stats.get(name, 0) or 0) or default
            except (TypeError, ValueError):
                return default

        self.level = stat("level", 1)
        self.stats = {name: stat(name.casefold(), 13) for name in STAT_NAMES}
        inventory = list(getattr(state, "inventory", ()) or ())
        items = getattr(state, "items", {}) or {}
        equipment = getattr(state, "equipment", {}) or {}
        worn = {tag: items[tag] for tag in equipment.values() if tag in items}
        worn.update((item.tag, item) for item in inventory
                    if item.flags & c.ITEM_APPLIED)
        self.worn = {tag: item for tag, item in worn.items()
                     if item.item_type in GEAR_LABELS and
                     item.item_type != c.TYPE_LIGHT_APPLY}
        self._context: dict[int, EffectiveStats | None] = {}
        self._values: dict[int, int | None] = {}
        self._owned = {item.tag for item in inventory}
        self._families: dict[int, bool] = {}
        self.carried = sum(float(item.weight or 0) * item.quantity for item in inventory)
        try:
            self.limit = float(stats.get("weight_limit", 0) or 0)
        except (TypeError, ValueError):
            self.limit = 0.0
        if self.limit <= 0:
            self.limit = float(_table(WEIGHT_LIMIT, self.stats["Str"]))
        worn_hp = sum(stats.maxhp for stats in map(self.context_stats, self.worn.values())
                      if stats is not None)
        self.base_hp = max(0.0, (stat("maxhp", 0) - worn_hp) /
                           (1.0 + _table(HP_BONUS, self.stats["Con"])))
        weapon = next((item for item in self.worn.values()
                       if item.item_type == c.TYPE_WEAPON), None)
        launcher = next((item for item in self.worn.values()
                         if item.item_type == c.TYPE_BOW), None)
        weapon_stats = self.context_stats(weapon) if weapon is not None else None
        try:
            shown_delay = float(stats.get("weapon_speed", 0) or 0)
        except (TypeError, ValueError):
            shown_delay = 0.0
        self.weapon_delay = ((weapon_stats.delay if weapon_stats is not None else None) or
                             shown_delay or 2.0)
        self.skill_level = self._skill_level(launcher if self.style == "bow" else weapon)

    def _skill_level(self, item: Item | None) -> int:
        """The level of the skill her weapon (or launcher) trains; her level
        when that skill is not known."""
        items = getattr(self.client.state, "items", {}) or {}
        skill = items.get(item.required_skill_tag) if item is not None else None
        if skill is None and self.style == "bow":
            skill = max((entry for entry in getattr(self.client.state, "inventory", ())
                         if entry.item_type == c.TYPE_SKILL and
                         "archery" in entry.name.casefold()),
                        key=lambda entry: int(entry.extra.get("level", 0) or 0),
                        default=None)
        level = int(skill.extra.get("level", 0) or 0) if skill is not None else 0
        return level or self.level

    def context_stats(self, item: Item) -> EffectiveStats | None:
        """Effective stats from examine or archetype, for her worn set."""
        if item.tag not in self._context:
            self._context[item.tag] = item_effective_stats(self.client, item)
        return self._context[item.tag]

    def effective(self, item: Item) -> EffectiveStats | None:
        """Effective stats from examine data only, else None."""
        stats = self.context_stats(item)
        return stats if stats is not None and stats.source == "examine" else None

    def _rest(self, item: Item) -> list[Item]:
        """Her worn set without what this item would replace: its slot; for
        a ring, itself when worn, else the weakest worn ring when both ring
        slots are full (the one the equip loop takes off), else nothing."""
        if item.item_type != c.TYPE_RING:
            return [worn for worn in self.worn.values() if worn.item_type != item.item_type]
        if item.tag in self.worn:
            return [worn for tag, worn in self.worn.items() if tag != item.tag]
        rings = [worn for worn in self.worn.values() if worn.item_type == c.TYPE_RING]
        slots = len(getattr(self.policy, "equipment_slots", {}).get(c.TYPE_RING, ())) or 2
        if len(rings) < slots:
            return list(self.worn.values())
        # A worn ring's value depends only on itself, so this never recurses.
        replaced = min(rings, key=lambda ring: (self.value(ring) is None,
                                                self.value(ring) or 0, ring.tag))
        return [worn for worn in self.worn.values() if worn.tag != replaced.tag]

    @staticmethod
    def _two_handed(item: Item) -> bool:
        """A two-handed weapon or launcher: the server's CS_FLAG_WEAPON_2H."""
        return (item.item_type in (c.TYPE_WEAPON, c.TYPE_BOW) and
                bool(item.flags & c.ITEM_WEAPON_2H))

    def _shield_off(self, items: list[Item]) -> bool:
        """Whether a shield gives nothing with these items worn: a
        two-handed weapon or launcher among them (living_update_player)."""
        return any(self._two_handed(item) for item in items)

    def _shield_worth(self, rest: list[Item]) -> int:
        """What the worn shield among ``rest`` gives her apart from speed
        (which _speed_stats handles), as if nothing two-handed were worn."""
        shield = next((item for item in rest if item.item_type == c.TYPE_SHIELD), None)
        if shield is None:
            return 0
        return self._value(shield, speed=False, shield_rule=False) or 0

    def _speed_stats(self, items: list[Item], extra: Item | None = None,
                     extra_stats: EffectiveStats | None = None
                     ) -> list[EffectiveStats | None]:
        """The effective stats that count for her speed with ``items`` (and
        ``extra``, with ``extra_stats``) worn: a shield counts for nothing
        while a two-handed weapon or launcher is wielded
        (living_update_player)."""
        two_handed = self._shield_off([*items, *([extra] if extra is not None else [])])

        def counts(item: Item) -> bool:
            return not (two_handed and item.item_type == c.TYPE_SHIELD)

        stats = [self.context_stats(item) for item in items if counts(item)]
        if extra is not None and counts(extra):
            stats.append(extra_stats)
        return stats

    def _rest_stats(self, rest: list[Item]) -> dict[str, int]:
        """Her stats without the worn items left out of ``rest``."""
        stats = dict(self.stats)
        kept = {item.tag for item in rest}
        for tag, item in self.worn.items():
            if tag not in kept:
                for name in STAT_NAMES:
                    stats[name] -= _stat_total(self.context_stats(item), name)
        return stats

    def speed_delta(self, item: Item, stats: EffectiveStats | None = None) -> float:
        """How much faster (or slower) she is with this item worn in its
        slot than with the slot empty."""
        stats = stats if stats is not None else self.context_stats(item)
        if stats is None:
            return 0.0
        rest = self._rest(item)
        own = self._rest_stats(rest)
        others = self._speed_stats(rest)
        with_item = self._speed_stats(rest, item, stats)
        return (player_speed(own["Dex"] + _stat_total(stats, "Dex"), with_item,
                             self.carried, self.limit) -
                player_speed(own["Dex"], others, self.carried, self.limit))

    def weight_cost(self, item: Item) -> float:
        """The speed her carrying this item's weight costs, once she carries
        more than ENCUMBRANCE_SCORED of her limit; ramps in up to the real
        slowdown at ENCUMBRANCE_START. Charged only where the decision
        changes what she carries (``weight_for``): never in the equip
        loop, which swaps between items she carries either way."""
        charged = (self.weight_for == WEIGHT_UNOWNED and item.tag not in self._owned or
                   self.weight_for == WEIGHT_UNWORN and item.tag in self._owned and
                   item.tag not in self.worn)
        if self.limit <= 0 or not charged:
            return 0.0
        ratio = self.carried / self.limit
        if ratio <= ENCUMBRANCE_SCORED:
            return 0.0
        ramp = min(1.0, (ratio - ENCUMBRANCE_SCORED) /
                   (ENCUMBRANCE_START - ENCUMBRANCE_SCORED))
        unloaded = player_speed(self.stats["Dex"], self._speed_stats(list(self.worn.values())))
        per_kg = unloaded / (self.limit * (1.0 - ENCUMBRANCE_START))
        return float(item.weight or 0) * per_kg * ramp

    def _speed_weight(self) -> float:
        return SPEED_WEIGHT * (1.0 if self.recall else NO_RECALL_SPEED_FACTOR)

    def value(self, item: Item) -> int | None:
        """The item's worth on the effective scale, or None without examine
        data or for a non-gear item. It may be negative (a low armour speed
        cap can cost more than the armour gives). A scorer is made
        per decision, so each item's value is kept for its lifetime."""
        if item.tag not in self._values:
            self._values[item.tag] = self._value(item)
        return self._values[item.tag]

    def _value(self, item: Item, *, speed: bool = True,
               shield_rule: bool = True) -> int | None:
        """``speed``: count the speed and weight terms. ``shield_rule``:
        a shield gives nothing under a two-handed weapon or launcher, and a
        two-handed one is charged the worn shield it switches off."""
        # A light is chosen for its light, which no stat shows.
        if (item.item_type not in getattr(self.policy, "equipment_types", GEAR_LABELS) or
                item.item_type == c.TYPE_LIGHT_APPLY):
            return None
        stats = self.effective(item)
        if stats is None:
            return None
        rest = self._rest(item)
        if shield_rule and item.item_type == c.TYPE_SHIELD and self._shield_off(rest):
            return 0
        own = self._rest_stats(rest)
        new = {name: own[name] + _stat_total(stats, name) for name in STAT_NAMES}
        melee_share = OFF_STYLE_SHARE if self.style == "caster" else (
            OFF_STYLE_SHARE if self.style == "bow" else 1.0)
        missile_share = 1.0 if self.style == "bow" else OFF_STYLE_SHARE
        level_factor = level_damage(self.skill_level)

        def strength(value: int) -> float:
            return 1.0 + _table(DAM_BONUS, value) / 10.0

        # Melee damage per second: dam x LEVEL_DAMAGE x Str bonus / delay.
        rest_dam = sum(worn.dam for worn in map(self.context_stats, rest) if worn is not None)
        delay = max(0.125, (stats.delay if stats.item_type == c.TYPE_WEAPON else None) or
                    self.weapon_delay)
        dps_with = (rest_dam + stats.dam) * level_factor * strength(new["Str"]) / delay
        dps_without = rest_dam * level_factor * strength(own["Str"]) / self.weapon_delay
        damage = (dps_with - dps_without) * DPS_WEIGHT * melee_share
        # Arrows: the launcher's (or arrow's) WC and damage per shot.
        # Time between shots: the launcher's delay plus an arrow's (bow.c).
        shot = (stats.delay if stats.item_type == c.TYPE_BOW and stats.delay is not None
                else BOW_REFERENCE_DELAY) + ARROW_REFERENCE_DELAY
        missile = (stats.missile_wc * WC_WEIGHT + stats.missile_dam * DPS_WEIGHT *
                   level_factor * strength(new["Str"]) / max(0.125, shot)) * missile_share
        wc_share = max(melee_share, missile_share)
        wc = (stats.wc * melee_share +
              (_table(WC_BONUS, new["Dex"]) - _table(WC_BONUS, own["Dex"])) * wc_share
              ) * WC_WEIGHT
        hp = stats.maxhp + self.base_hp * (_table(HP_BONUS, new["Con"]) -
                                           _table(HP_BONUS, own["Con"]))
        floor = sum(value * STAT_FLOOR for name, value in stats.stats
                    if name in ("Str", "Dex", "Con"))
        caster = 0.0
        maxsp, regen = stats.maxsp, stats.regen_sp
        caster_value = getattr(self.policy, "caster_value", None)
        if self.style == "caster" and caster_value is not None:
            bonuses = {key: value for key, value in (
                ("int", _stat_total(stats, "Int")), ("pow", _stat_total(stats, "Pow")),
                ("mana", stats.maxsp), ("regen", stats.regen_sp)) if value}
            caster = caster_value(bonuses, worn=bool(item.flags & c.ITEM_APPLIED),
                                  weapon=item.item_type in WEAPON_TYPES) if bonuses else 0.0
            maxsp = regen = 0
        else:
            floor += sum(value * STAT_FLOOR for name, value in stats.stats
                         if name in ("Int", "Pow"))
        physical = sum(value for name, value in stats.attacks if name in PHYSICAL_ATTACKS)
        attacks = (max(0.0, physical - 100) + sum(
            value for name, value in stats.attacks if name not in PHYSICAL_ATTACKS)
                   ) * (melee_share if item.item_type == c.TYPE_WEAPON else 1.0)
        speed_term = ((self.speed_delta(item, stats) - self.weight_cost(item)) *
                      self._speed_weight() if speed else 0.0)
        shield = (self._shield_worth(rest) if shield_rule and self._two_handed(item)
                  else 0)
        total = (damage + missile + wc + stats.ac * 20 + stats.block * 2 + stats.absorb * 2 +
                 sum(value for _, value in stats.protections) * PROTECTION_WEIGHT +
                 attacks + hp + floor + maxsp + regen + stats.regen_hp +
                 len(stats.specials) * SPECIAL_WEIGHT + caster + speed_term - shield)
        return round(total)

    def score(self, item: Item) -> int:
        """For ordering: the effective value, else the fallback."""
        value = self.value(item)
        return value if value is not None else self.fallback(item)

    def family_effective(self, item_type: int) -> bool:
        """Whether every gear item of this type she carries has examine
        data, so the family can be ordered on the effective scale."""
        if item_type not in self._families:
            self._families[item_type] = all(
                self.value(item) is not None
                for item in getattr(self.client.state, "inventory", ())
                if item.item_type == item_type and
                not item.flags & c.ITEM_UNPAID and self.policy.identified(item))
        return self._families[item_type]

    def order(self, item: Item) -> int:
        """For ordering candidates as ``compare`` would: the effective
        value only when the whole family has examine data, else the
        fallback for the whole family."""
        value = self.value(item)
        if value is not None and self.family_effective(item.item_type):
            return value
        return self.fallback(item)

    def compare(self, candidate: Item, current: list[Item]
                ) -> tuple[int, list[int], bool]:
        """The candidate's and the current items' scores on one scale, and
        whether that scale is the effective one: the fallback for all when
        any of them has no examine data."""
        values = [self.value(item) for item in (candidate, *current)]
        if all(value is not None for value in values):
            return values[0], values[1:], True
        return (self.fallback(candidate), [self.fallback(item) for item in current], False)

    def displacement_margin(self, item: Item, effective: bool) -> int:
        """What a caster upgrade must beat by once more to take off this
        item (``InventoryPolicy.displacement_margin``): its protections'
        worth, from its effective stats on the effective scale."""
        if not effective:
            return int(self.policy.displacement_margin(item))
        stats = self.effective(item)
        if self.style != "caster" or stats is None:
            return 0
        return round(sum(max(0.0, value) for _, value in stats.protections) *
                     PROTECTION_WEIGHT)

    def fills(self, candidate: Item) -> bool:
        """Whether a free slot takes this item: anything that is not a net
        loss, and armour that is a net gain apart from speed, whatever its
        speed cap costs (a body slot is never left empty for its cap)."""
        value = self.value(candidate)
        if value is None or value >= 0:
            return True
        apart_from_speed = self._value(candidate, speed=False)
        return (candidate.item_type in ARMOUR_TYPES and
                apart_from_speed is not None and apart_from_speed > 0)

    def beats(self, candidate: Item, current: list[Item], slots: int = 1
              ) -> tuple[bool, Item | None]:
        """Whether the candidate should replace the weakest of what she
        wears in its slot, and that weakest item: it must beat it by
        CHOICE_MARGIN plus the caster displacement margin. A free slot (one
        of ``slots``, such as the second ring) takes what ``fills`` takes."""
        if len(current) < slots:
            return self.fills(candidate), None
        mine, theirs, effective = self.compare(candidate, current)
        # Ties go to the lower tag, as _rest picks the ring it replaces.
        index = min(range(len(current)), key=lambda i: (theirs[i], current[i].tag))
        worst = current[index]
        margin = CHOICE_MARGIN + self.displacement_margin(worst, effective)
        return mine > theirs[index] + margin, worst


# Margin a candidate must beat, in either score, before the choice changes;
# the same as InventoryPolicy's equip loop (maintain_inventory).
CHOICE_MARGIN = 15


def equipment_report(client: Any, policy: Any,
                     old_score: Callable[[Item], int], *,
                     recall: bool = True) -> dict[str, Any]:
    """Per gear family: what she wears, the best carried candidate, both
    scores, the per-stat deltas and where the old name score would choose
    differently from ``GearScorer``, which decides (the shadow comparison;
    nothing here equips anything). A new score is None without examine
    data; the equip loop then falls back to the old score."""
    state = client.state
    equipped = set(state.equipment.values())
    level = int(state.stats.get("level", 0) or 0)
    # ``recall`` as the equip loop reads it (tasks.working_recall).
    scorer = GearScorer(client, policy, old_score, recall=recall)

    def stats_of(item: Item) -> EffectiveStats | None:
        return scorer.context_stats(item)

    def new_score(item: Item) -> int | None:
        return scorer.value(item)

    def row(item: Item) -> dict[str, Any]:
        stats = stats_of(item)
        return {
            "tag": item.tag, "name": item.name,
            "old_score": old_score(item), "new_score": new_score(item),
            "source": stats.source if stats is not None else "unknown",
            "magic": magic_from_name(item.name, item.item_type),
            "stats": stat_lines(stats) if stats is not None else [],
            "notes": list(stats.notes) if stats is not None else
            ["not examined yet, and its archetype is not known"],
        }

    families = []
    for item_type, label in GEAR_LABELS.items():
        mine = [item for item in state.inventory if item.item_type == item_type]
        if not mine:
            continue
        worn = [item for item in mine
                if item.tag in equipped or item.flags & c.ITEM_APPLIED]
        worn_tags = {item.tag for item in worn}
        candidates = [
            item for item in mine
            if item.tag not in worn_tags and policy.identified(item) and
            not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED) and
            item.required_level <= level and
            # The equip loop never switches the trained weapon school.
            not (item_type in (c.TYPE_WEAPON, c.TYPE_BOW) and item.required_skill_tag and
                 any(w.required_skill_tag and w.required_skill_tag != item.required_skill_tag
                     for w in worn))]

        def choice() -> Item | None:
            """The candidate the old name score would put on, or None to
            keep what she wears: the best candidate must beat the weakest
            worn item by the margin; an empty slot takes any."""
            best = max(((value, item) for item in candidates
                        if (value := old_score(item)) >= 0),
                       key=lambda pair: pair[0], default=None)
            if best is None or not worn:
                return best[1] if best is not None else None
            worst = min(worn, key=old_score)
            return (best[1] if best[0] > old_score(worst) + CHOICE_MARGIN +
                    policy.displacement_margin(worst) else None)

        def decided() -> Item | None:
            """The candidate GearScorer puts on, as the equip loop does."""
            slots = len(policy.equipment_slots.get(item_type, ())) or 1
            return next((item for item in sorted(candidates, key=scorer.order, reverse=True)
                         if scorer.beats(item, worn, slots)[0]), None)

        # Only what the equip loop chooses is compared: a light is chosen
        # for its light, which no effective stat shows.
        compared = (item_type in policy.equipment_types and
                    item_type != c.TYPE_LIGHT_APPLY)
        old_pick = choice() if compared else None
        new_pick = decided() if compared else None
        known = [item for item in candidates if stats_of(item) is not None]
        candidate = max(known, key=scorer.score, default=None)
        weakest = min((item for item in worn if stats_of(item) is not None),
                      key=scorer.score, default=None)
        families.append({
            "type": item_type, "label": label,
            "worn": [row(item) for item in worn],
            "candidate": row(candidate) if candidate is not None else None,
            "deltas": (stat_deltas(stats_of(candidate),
                                   stats_of(weakest) if weakest is not None else None)
                       if candidate is not None else []),
            # What the old name score and GearScorer (the decider) would
            # put on; None keeps what she wears.
            "old_choice": row(old_pick) if old_pick is not None else None,
            "new_choice": row(new_pick) if new_pick is not None else None,
            "diverges": old_pick is not new_pick,
        })
    examiner = getattr(client, "gear_examiner", None)
    return {
        "families": families,
        "examined": sum(1 for item in state.inventory
                        if isinstance(item.extra.get("gear_examine"), dict) and
                        item.extra["gear_examine"].get("name") == item.name),
        "shadow": list(examiner.shadow_log) if examiner is not None else [],
    }


class GearExaminer:
    """Examines her carried gear once a session, one item at a time.

    Called on a quiet farm tick (no threat in view or contact, the recall
    wand rule). It waits for each answer, or gives up after
    ``ANSWER_TIMEOUT`` seconds, before the next one, and leaves at least
    ``SPACING`` seconds between examines; it never starts while a recall
    wand or mana crystal examine waits for its answer, and those wait for
    its. Each reader reads only the lines after its own examine was sent.
    The walk out of a farm may abandon a waiting examine (``abandon``);
    nothing is then sent for ``ANSWER_TIMEOUT + SPACING`` seconds. Results
    live in the item's extras, which a relog replaces: each new tag is
    examined again.
    """

    ANSWER_TIMEOUT = 5.0
    SPACING = 2.0
    RETRY_AFTER = 300.0
    MAX_ATTEMPTS = 3
    # How often the shadow comparison is recomputed and logged at most.
    SHADOW_INTERVAL = 30.0
    SHADOW_LOG_SIZE = 50

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.pending: tuple[int, str, int, float] | None = None
        self.last_sent = float("-inf")
        self.shadow_at = float("-inf")
        self.shadow_signature: tuple = ()
        self.shadow_log: list[dict[str, Any]] = []
        self.policy: Any = None
        # After an abandon, nothing is sent before this (clock time), so the
        # abandoned examine's late answer comes before any new one is asked.
        self.paused_until = float("-inf")
        self.old_score: Callable[[Item], int] | None = None
        self.recall = True

    @staticmethod
    def wanted(item: Item, policy: Any) -> bool:
        examined = item.extra.get("gear_examine")
        return (item.item_type in GEAR_LABELS and
                item.item_type != c.TYPE_LIGHT_APPLY and
                policy.identified(item) and
                not item.flags & c.ITEM_UNPAID and
                not (isinstance(examined, dict) and examined.get("name") == item.name) and
                int(item.extra.get("gear_examine_attempts", 0) or 0) < GearExaminer.MAX_ATTEMPTS)

    @staticmethod
    def other_examine_pending(client: Any) -> bool:
        """A mana crystal examine waiting for its answer (ManaCrystal); the
        caller checks its own recall wand examine."""
        return any(item.extra.get("crystal_examine") for item in client.state.inventory)

    def read_answer(self, client: Any) -> bool:
        """Settle the pending examine: True once answered or given up."""
        if self.pending is None:
            return True
        tag, name, start, sent_at = self.pending
        item = client.state.items.get(tag)
        prefix = f"That is {name}"
        # Only lines after this examine was sent; only server game lines,
        # never one with a player's name link (messages_since drops those).
        for entry in client.state.messages_since(start):
            if len(entry) < 4 or entry[1] != c.CHAT_TYPE_GAME:
                continue
            text = str(entry[3]).strip()
            if not _answers(text, name):
                continue
            text = re.sub(r"^That is an? ", "That is ", text)
            rest = text[len(prefix):]
            if item is not None:
                # A ring's, amulet's or trinket's description is in its name.
                shown = (name + rest if item.item_type in MAGIC_HIDDEN_TYPES else rest)
                item.extra["gear_examine"] = {
                    "name": name,
                    "base": base_to_json(parse_description(shown) or BaseStats()),
                    "at": time.time()}
                item.extra.pop("gear_examine_attempts", None)
            self.pending = None
            return True
        if self.clock() - sent_at <= self.ANSWER_TIMEOUT:
            return False
        if item is not None:
            # A name that is not authored, even without its title, is most
            # likely her own rename, which examine never shows: one try.
            content = getattr(client, "authored_content", None)
            renamed = content is not None and looks_renamed(content, name)
            item.extra["gear_examine_attempts"] = (
                self.MAX_ATTEMPTS if renamed else
                int(item.extra.get("gear_examine_attempts", 0) or 0) + 1)
            item.extra["gear_examine_retry_at"] = self.clock() + self.RETRY_AFTER
        log.info("gear examine of %s got no answer", name)
        self.pending = None
        return True

    def abandon(self, client: Any) -> None:
        """Give up the pending examine at once, without counting an attempt,
        so a recall wand examine need not wait for it.

        Nothing more is sent for ``ANSWER_TIMEOUT + SPACING`` seconds: the
        abandoned answer, if it still comes, arrives before any new examine
        is asked, and no reader takes it (nothing is pending for it).
        """
        if self.pending is None or self.read_answer(client):
            return
        name = self.pending[1]
        self.pending = None
        self.paused_until = self.clock() + self.ANSWER_TIMEOUT + self.SPACING
        log.info("gear examine of %s abandoned for a recall wand examine", name)

    async def tick(self, client: Any, policy: Any,
                   old_score: Callable[[Item], int], *, recall: bool = True) -> bool:
        """Examine the next unexamined item, or settle the last one; True
        while an examine waits for its answer. ``recall``: whether a recall
        source of hers works, as the equip loop weighs speed."""
        self.policy, self.old_score, self.recall = policy, old_score, recall
        settling = self.pending is not None
        if not self.read_answer(client):
            return True
        if settling:
            # Yield this tick: a crystal or wand examine that waited for
            # this answer goes next, not her next gear item.
            return False
        now = self.clock()
        self.shadow(client)
        # The "+N with no effect" feedback producer reads the same gear
        # (AtrinikClient.note_equipment_feedback); it only writes a draft.
        note_feedback = getattr(client, "note_equipment_feedback", None)
        if callable(note_feedback):
            note_feedback()
        examine = getattr(client, "examine", None)
        if (examine is None or now - self.last_sent < self.SPACING or
                now < self.paused_until or
                not getattr(client.state, "inventory_replay_complete", True) or
                self.other_examine_pending(client)):
            return False
        item = next((item for item in sorted(
            client.state.inventory,
            # What she wears first: the panel compares against it.
            key=lambda item: not item.flags & c.ITEM_APPLIED)
            if self.wanted(item, policy) and
            now >= float(item.extra.get("gear_examine_retry_at", 0.0) or 0.0)), None)
        if item is None:
            return False
        self.pending = (item.tag, item.name, client.state.message_count, now)
        self.last_sent = now
        await examine(item.tag)
        return True

    def shadow(self, client: Any, *, force: bool = False) -> None:
        """Log where the old name score would choose differently from
        GearScorer, which decides (kept as a diagnostic after F4)."""
        now = self.clock()
        if self.old_score is None or (not force and now - self.shadow_at < self.SHADOW_INTERVAL):
            return
        self.shadow_at = now
        try:
            report = equipment_report(client, self.policy, self.old_score,
                                      recall=self.recall)
        except Exception as error:   # a diagnostic must never stop her farm
            log.warning("gear shadow comparison failed: %s", error)
            return
        divergent = [family for family in report["families"] if family["diverges"]]
        signature = tuple((family["type"], *((family[key] or {}).get("tag", 0)
                                             for key in ("old_choice", "new_choice")))
                          for family in divergent)
        if signature == self.shadow_signature:
            return
        self.shadow_signature = signature
        recorder = getattr(client, "record_action", None)

        def describe(family: dict[str, Any], entry: dict[str, Any] | None) -> str:
            if entry is None:
                worn = ", ".join(f"{row['name']} (old {row['old_score']}, "
                                 f"new {row['new_score']})" for row in family["worn"])
                return f"keeps {worn or 'the slot empty'}"
            return (f"puts on {entry['name']} (old {entry['old_score']}, "
                    f"new {entry['new_score']})")

        details = [f"{family['label'].lower()}: name scoring "
                   f"{describe(family, family['old_choice'])}; gear scorer "
                   f"{describe(family, family['new_choice'])}" for family in divergent]
        if not details:
            details = ["no divergence"]
        for detail in details:
            self.shadow_log.append({"time": time.time(), "detail": detail})
            del self.shadow_log[:-self.SHADOW_LOG_SIZE]
            log.info("gear-shadow %s", detail)
            if recorder is not None:
                recorder("gear-shadow", detail)


def _answers(text: str, name: str) -> bool:
    """Whether a line is the examine answer for an item of this shown name:
    "That is " and the name (one of it: no count), then only its state
    flags, "(...)" or a locked item's " *", and its description; never
    more name (types/player.c examine, item.c object_get_name)."""
    text = re.sub(r"^That is an? ", "That is ", text)
    prefix = f"That is {name}"
    rest = text[len(prefix):]
    return text.startswith(prefix) and (not rest or rest.startswith((" (", " *")))


# The examine-answer test, for the shop buyer's reader too.
examine_answers = _answers


def looks_renamed(content: Any, name: str) -> bool:
    """Whether a shown name is not authored even without its title.

    The authored-name index holds object and archetype names, not artifact
    titles ("of Fire"), so the name is tried bare too: before " of ..." and
    before a "+N" that a title follows.
    """
    forms = {name, re.sub(r"\s+of\s.*$", "", name),
             re.sub(r"(\s[+-]\d+)\s.*$", r"\1", name)}
    return all(authored_item_name(content, form) is None for form in forms if form)


def abandon_gear_examine(client: Any) -> None:
    """Give up her pending gear examine now (GearExaminer.abandon)."""
    examiner = getattr(client, "gear_examiner", None)
    if examiner is not None:
        examiner.abandon(client)


def gear_examine_pending(client: Any) -> bool:
    """Whether her gear examine still waits for its answer.

    The recall wand and mana crystal examines wait for it, so that only one
    examine is ever in flight. It settles itself here (answered, or given up
    after ``ANSWER_TIMEOUT``), so a farm that stopped ticking never holds
    them for longer.
    """
    examiner = getattr(client, "gear_examiner", None)
    return examiner is not None and not examiner.read_answer(client)


def gear_examiner(client: Any) -> GearExaminer:
    """The connection's one examiner, so every farm task shares its state."""
    examiner = getattr(client, "gear_examiner", None)
    if examiner is None:
        examiner = client.gear_examiner = GearExaminer()
    return examiner
