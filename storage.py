"""Home storage: the gear sets she keeps per build, bags of holding, and the
index of what her apartment chest holds.

She carries only her active set (worn gear, heals, recall, the identify
device, the mana crystal, quest items, keys and money); everything else
useful goes to her apartment chest (FarmCircuitTask._storable_items). The
chest's contents are not part of the dashboard state, and are sent only
while she has it open, so what she deposited and retrieved is kept in her
character memory (``GameState.apartment_storage``) and replaced by the
chest's real contents whenever she opens it.

Each stored piece of gear carries the build sets it belongs to
("melee:weapon", "caster:ring", ...), ranked per build by the gear scorer
(equipment.GearScorer), so a later build change can fetch the right set.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from . import constants as c
from .model import Item

# The builds a set is kept for: GearScorer's styles (equipment.GEAR_STYLES).
# The client loads the index without the gear model (equipment.py needs
# the content tools), so that is imported only where sets are ranked.
BUILDS = ("melee", "caster", "bow")

# Bag of holding multipliers (content arch/artifacts.art: the artifact's
# weapon_speed). The server multiplies everything inside by it
# (server/object.c, sack_can_hold).
BAG_TITLES: dict[str, float] = {
    "minor": 0.85, "small": 0.75, "medium": 0.65, "": 0.55,
    "fine": 0.45, "great": 0.40, "greater": 0.35, "weightless": 0.30,
}
BAG_TITLE = re.compile(
    r"\bof (?:(minor|small|medium|fine|great|greater|weightless) )?holding\b", re.I)
# Examine of an identified container (types/player.c).
BAG_EXAMINE = re.compile(
    r"It decreases the weight of items inside by (\d+(?:\.\d+)?)%", re.I)
HOLD_ONLY = re.compile(r"It can hold only (.+?)(?: and its weight limit|\.)", re.I)
# What a container's name says it may hold, where examine has not.
NAMED_RACES = (
    ("key ring", "keys"), ("stone quiver", "sling stones"),
    ("bolt quiver", "crossbow bolts"), ("quiver", "arrows"),
    ("pouch", "gold and jewels"),
)

# She carries her best bag of holding only while what she carries would
# weigh more than this share of her limit without it (where
# GearScorer starts to score weight); a carried bag goes home again
# once that drops below BAG_SPARE, so it does not shuttle back and forth.
BAG_NEEDED = 0.50
BAG_SPARE = 0.40

# The index keeps at most this many entries.
INDEX_LIMIT = 500

# What goes into a carried bag of holding: gems and ammunition. Never her
# coins (her wallet is read from the coins she carries) nor a heal or
# recall she must use at once.
BAG_FILL_TYPES = (c.TYPE_GEM, c.TYPE_JEWEL, c.TYPE_PEARL, c.TYPE_NUGGET,
                  c.TYPE_ARROW)
GEM_TYPES = (c.TYPE_GEM, c.TYPE_JEWEL, c.TYPE_PEARL, c.TYPE_NUGGET)


def bag_factor(item: Item) -> float | None:
    """A bag of holding's weight multiplier, from its examine or its
    "of ... holding" title; None for anything else."""
    if item.item_type != c.TYPE_CONTAINER:
        return None
    examined = item.extra.get("bag_factor")
    if isinstance(examined, (int, float)) and 0 < examined < 1:
        return float(examined)
    match = BAG_TITLE.search(item.name)
    if match is None:
        return None
    return BAG_TITLES[(match.group(1) or "").casefold()]


def read_bag_examine(item: Item, text: str) -> bool:
    """Keep what a container's examine says: its weight reduction and
    what it may hold. True when it said either."""
    found = False
    match = BAG_EXAMINE.search(text)
    if match is not None:
        item.extra["bag_factor"] = round(1.0 - float(match.group(1)) / 100.0, 4)
        found = True
    match = HOLD_ONLY.search(text)
    if match is not None:
        item.extra["bag_race"] = match.group(1).strip().casefold()
        found = True
    return found


def bag_race(item: Item) -> str:
    """What a container may hold ("" for anything)."""
    race = item.extra.get("bag_race")
    if isinstance(race, str):
        return race
    name = item.name.casefold()
    return next((race for word, race in NAMED_RACES if word in name), "")


def holds(bag: Item, item: Item) -> bool:
    """Whether the bag takes this item, as far as its race shows."""
    race = bag_race(bag)
    if not race:
        return True
    if race == "gold and jewels":
        return item.item_type in GEM_TYPES
    if race == "keys":
        return item.item_type == c.TYPE_KEY
    name = item.name.casefold()
    return item.item_type == c.TYPE_ARROW and any(
        word in name for word in race.split() if len(word) > 3)


def best_bag(items: Iterable[Item]) -> Item | None:
    """The bag of holding with the lowest multiplier."""
    bags = [(factor, item.tag, item) for item in items
            if (factor := bag_factor(item)) is not None and
            not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)]
    return min(bags, key=lambda entry: entry[:2])[2] if bags else None


def bag_fill(item: Item) -> bool:
    """Whether a carried item belongs in her bag of holding."""
    return (item.item_type in BAG_FILL_TYPES and
            not item.flags & (c.ITEM_APPLIED | c.ITEM_UNPAID | c.ITEM_LOCKED))


def unbagged_weight(inventory: Iterable[Item]) -> float:
    """What she carries would weigh without her bags' reduction."""
    total = 0.0
    for item in inventory:
        weight = float(item.weight or 0) * max(1, int(item.quantity or 1))
        factor = bag_factor(item)
        total += weight / factor if factor else weight
    return total


def load_ratio(state: Any) -> float:
    limit = float((getattr(state, "stats", {}) or {}).get("weight_limit", 0) or 0)
    if limit <= 0:
        return 0.0
    return unbagged_weight(getattr(state, "inventory", ())) / limit


def bag_wanted(state: Any, *, carried: bool = False) -> bool:
    """Whether she should carry her best bag of holding: above BAG_NEEDED
    of her limit; one she carries stays until below BAG_SPARE."""
    return load_ratio(state) > (BAG_SPARE if carried else BAG_NEEDED)


# --- Build sets -----------------------------------------------------------

def slot_count(item_type: int) -> int:
    return 2 if item_type == c.TYPE_RING else 1


def slot_label(item_type: int) -> str:
    from .equipment import GEAR_LABELS
    return GEAR_LABELS.get(item_type, "gear").casefold()


def rank(scorer: Any, items: list[Item]) -> list[Item]:
    """Items best first, on one scale: the effective one when every item
    has examine data, else the fallback for all (GearScorer.compare)."""
    values = [scorer.value(item) for item in items]
    if not all(value is not None for value in values):
        values = [scorer.fallback(item) for item in items]
    order = sorted(zip(values, items),
                   key=lambda pair: (-pair[0], not pair[1].flags & c.ITEM_APPLIED,
                                     pair[1].tag))
    return [item for _, item in order]


def best_sets(items: Iterable[Item], scorer_for: Callable[[str], Any], *,
              usable: Callable[[Item], bool],
              builds: Iterable[str] = BUILDS) -> dict[str, dict[int, list[Item]]]:
    """Per build, per gear family, the best ``usable`` items she owns
    (one per slot; two rings), by that build's gear scorer."""
    from .equipment import GEAR_LABELS
    pool = [item for item in items
            if item.item_type in GEAR_LABELS and
            item.item_type != c.TYPE_LIGHT_APPLY and usable(item)]
    families: dict[int, list[Item]] = {}
    for item in pool:
        families.setdefault(item.item_type, []).append(item)
    sets: dict[str, dict[int, list[Item]]] = {}
    for build in builds:
        scorer = scorer_for(build)
        sets[build] = {
            item_type: rank(scorer, members)[:slot_count(item_type)]
            for item_type, members in families.items()}
    return sets


def set_labels(sets: dict[str, dict[int, list[Item]]]) -> dict[int, list[str]]:
    """Each item's places in the sets: "melee:weapon", "caster:ring"."""
    labels: dict[int, list[str]] = {}
    for build, families in sets.items():
        for item_type, members in families.items():
            for item in members:
                labels.setdefault(item.tag, []).append(f"{build}:{slot_label(item_type)}")
    return labels


def set_tags(sets: dict[str, dict[int, list[Item]]], build: str | None = None) -> set[int]:
    """The tags in one build's set, or in any build's."""
    return {item.tag for name, families in sets.items()
            if build is None or name == build
            for members in families.values() for item in members}


# --- The stored index -------------------------------------------------------

def entry(item: Item, labels: Iterable[str] = (), *, identified: bool = True) -> dict:
    """One stored item as kept in her memory."""
    row = {
        "name": item.name, "type": int(item.item_type),
        "quantity": max(1, int(item.quantity or 1)),
        "weight": round(float(item.weight or 0), 4),
        "identified": bool(identified),
        "magical": bool(item.flags & c.ITEM_MAGICAL),
        "cursed": bool(item.flags & (c.ITEM_CURSED | c.ITEM_DAMNED)),
        "sets": sorted(set(labels)),
        "at": round(time.time(), 1),
    }
    factor = bag_factor(item)
    if factor is not None:
        row["bag_factor"] = factor
    if item.face:
        row["face"] = int(item.face)
    return row


def _number(value: Any, default: float) -> float:
    """A finite number from memory, else ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if math.isfinite(value) else default


def valid_entry(value: Any) -> dict | None:
    """A stored row as loaded from memory, or None when malformed."""
    if (not isinstance(value, dict) or not isinstance(value.get("name"), str) or
            not isinstance(value.get("type"), int) or isinstance(value.get("type"), bool)):
        return None
    sets = value.get("sets", [])
    row = {
        "name": value["name"][:200], "type": value["type"],
        "quantity": max(1, min(1_000_000, int(_number(value.get("quantity", 1), 1.0)))),
        "weight": max(0.0, _number(value.get("weight", 0), 0.0)),
        "identified": bool(value.get("identified", True)),
        "magical": bool(value.get("magical", False)),
        "cursed": bool(value.get("cursed", False)),
        "sets": [str(label)[:40] for label in sets][:8] if isinstance(sets, list) else [],
        "at": _number(value.get("at", 0), 0.0),
    }
    factor = _number(value.get("bag_factor"), 0.0)
    if 0 < factor < 1:
        row["bag_factor"] = factor
    face = value.get("face")
    if isinstance(face, int) and not isinstance(face, bool):
        row["face"] = face
    return row


def load_index(value: Any) -> list[dict]:
    rows = [row for row in map(valid_entry, value if isinstance(value, list) else ())
            if row is not None]
    return rows[-INDEX_LIMIT:]


def stored(state: Any) -> list[dict]:
    """What her chest holds, as far as she knows."""
    return list(getattr(state, "apartment_storage", None) or [])


def save(client: Any, rows: list[dict]) -> None:
    rows = rows[-INDEX_LIMIT:]
    setter = getattr(client, "set_apartment_storage", None)
    if setter is not None:
        setter(rows)
    else:
        client.state.apartment_storage = rows


def note_deposited(client: Any, rows: Iterable[dict]) -> None:
    added = list(rows)
    if added:
        save(client, stored(client.state) + added)


def note_retrieved(client: Any, items: Iterable[Item]) -> None:
    rows = stored(client.state)
    changed = False
    for item in items:
        index = next((i for i, row in enumerate(rows)
                      if row["type"] == item.item_type and row["name"] == item.name), None)
        if index is None:
            # Identify-all renamed it: any unknown row of its type.
            index = next((i for i, row in enumerate(rows)
                          if row["type"] == item.item_type and not row["identified"]), None)
        if index is not None:
            rows.pop(index)
            changed = True
    if changed:
        save(client, rows)


def stored_bags(state: Any) -> list[dict]:
    return sorted((row for row in stored(state)
                   if row.get("bag_factor") and not row.get("cursed")),
                  key=lambda row: row["bag_factor"])


def stored_unidentified(state: Any, types: Iterable[int]) -> list[dict]:
    wanted = set(types)
    return [row for row in stored(state)
            if not row["identified"] and row["type"] in wanted]


def no_labels(client: Any, items: list[Item]) -> dict[int, list[str]]:
    """For chest visits outside the farm circuit (the dashboard, the CLI,
    the mana crystal's recovery): rows without set labels, which the
    circuit's next visit adds back."""
    return {}


def plain_indexer() -> "ChestIndexer":
    from .tasks import InventoryPolicy
    return ChestIndexer(no_labels, InventoryPolicy().identified)


class ChestIndexer:
    """Keeps the index current through one chest visit.

    Each tick (``observe``) it notes the items a deposit moved in and a
    retrieval took out, and, while the chest's contents are known (open),
    replaces the index with those contents, labelled with their sets. The
    labels are recomputed only when the contents change.
    """

    def __init__(self, labeler: Callable[[Any, list[Item]], dict[int, list[str]]],
                 identified: Callable[[Item], bool]):
        self.labeler = labeler
        self.identified = identified
        self._signature: tuple | None = None
        self._deposits: dict[int, Item] = {}
        self._retrievals: dict[int, Item] = {}

    def deposit_sent(self, item: Item) -> None:
        self._deposits.setdefault(item.tag, item)

    def retrieval_sent(self, item: Item) -> None:
        self._retrievals.setdefault(item.tag, item)

    def observe(self, client: Any, container: Item) -> None:
        state = client.state
        if "/apartments/" not in str(getattr(getattr(state, "map", None), "path", "") or ""):
            # Only her apartment's chest is indexed (a dashboard deposit
            # may name any container).
            return
        carried = {item.tag for item in state.inventory}
        contents = state.inventories.get(container.tag)
        if contents is not None:
            items = [item for tag in contents
                     if (item := state.items.get(tag)) is not None]
            signature = tuple(sorted((item.tag, item.name, item.quantity) for item in items))
            if signature != self._signature:
                self._signature = signature
                labels = self.labeler(client, items)
                save(client, [entry(item, labels.get(item.tag, ()),
                                    identified=self.identified(item))
                              for item in items])
                self._log(client, f"{len(items)} item(s) in the chest")
            # The contents are the truth: nothing to count by hand.
            self._deposits.clear()
            self._retrievals.clear()
            return
        moved_in = [item for tag, item in self._deposits.items() if tag not in carried]
        if moved_in:
            labels = self.labeler(client, moved_in)
            note_deposited(client, [entry(item, labels.get(item.tag, ()),
                                          identified=self.identified(item))
                                    for item in moved_in])
            for item in moved_in:
                self._deposits.pop(item.tag, None)
            self._log(client, "stored " + ", ".join(item.name for item in moved_in[:5]))
        taken = [item for tag, item in self._retrievals.items() if tag in carried]
        if taken:
            note_retrieved(client, taken)
            for item in taken:
                self._retrievals.pop(item.tag, None)
            self._log(client, "took " + ", ".join(item.name for item in taken[:5]))

    @staticmethod
    def _log(client: Any, detail: str) -> None:
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("apartment-index", detail)
