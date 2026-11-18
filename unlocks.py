"""Her unlock state: what her client knows she can pass.

An :class:`UnlockState` is one immutable snapshot of the inputs that open
gated tiles for her, built only from what an ordinary client sees:

- the objects she carries, walked through containers as the server's
  ``check_inv()`` does. A closed container's contents are not sent, so the
  last contents seen while it was open are remembered (the key ring case)
  and forgotten once the container leaves her inventory;
- key aliases, which tie a lock id to the names of the objects that carry
  that ``slaying``: map-placed keys (``WorldGraph.build``), plus archetypes
  and artifacts (``*.art``) with a matching ``slaying``. Quest rewards
  (``<object arch=...>`` in quest XML) name an archetype or artifact, so
  they are covered by the same aliases;
- her quest journal, unknown until it has loaded.

Its ``access`` is the access set that requirements.py evaluates and every
planner call receives, so `NavigateTask`, savebed walks and ``route()`` use
one set. ``learned_tiles`` is the hook for learned tile access (LEARNING F8):
it is always empty here, and nothing reads a learned record yet.

See docs/ROUTING.md, section 4.3.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from . import constants as c
from .requirements import NAME_LOCK_PREFIX, Consumes, Item, Requirement

# Where a carried object was found.
INVENTORY = "inventory"      # her own top-level inventory
CONTAINER = "container"      # inside a container whose contents are visible
REMEMBERED = "remembered"    # last seen inside a container now closed

_EMPTY: Mapping = MappingProxyType({})


@dataclass(slots=True, frozen=True)
class CarriedObject:
    """One object she carries, as her client knows it."""
    name: str
    where: str = INVENTORY
    container: str = ""


@dataclass(slots=True)
class ContainerMemory:
    """The last seen contents of her own containers, by container tag.

    Lives for one game state (one connection); a reconnect starts empty
    until she opens her containers again.
    """
    contents: dict[int, tuple[str, ...]] = field(default_factory=dict)


# Container memory per game state. GameState has slots and no weak
# references, so a few recent states are kept in a small bounded table.
_MEMORIES: "OrderedDict[int, tuple[object, ContainerMemory]]" = OrderedDict()
_MEMORY_LIMIT = 8


def container_memory(state) -> ContainerMemory:
    """The container memory of one game state."""
    key = id(state)
    entry = _MEMORIES.get(key)
    if entry is None or entry[0] is not state:
        entry = (state, ContainerMemory())
        _MEMORIES[key] = entry
        while len(_MEMORIES) > _MEMORY_LIMIT:
            _MEMORIES.popitem(last=False)
    _MEMORIES.move_to_end(key)
    return entry[1]


def carried_objects(state, memory: ContainerMemory | None = None
                    ) -> tuple[CarriedObject, ...]:
    """Every object she carries, walking containers like ``check_inv()``.

    A container whose contents the client has seen stands for those
    contents, as on the server. A closed one stands for what it held when
    last open (``memory``); one never opened stands for itself, because
    what it holds is unknown. A state without container bookkeeping gives
    its top-level inventory.
    """
    inventories = getattr(state, "inventories", None)
    player_tag = getattr(state, "player_tag", None)
    if not isinstance(inventories, dict) or player_tag not in inventories:
        return tuple(CarriedObject(item.name)
                     for item in getattr(state, "inventory", ()))
    items = getattr(state, "items", {})
    carried: list[CarriedObject] = []
    pending = [(tag, INVENTORY, "") for tag in reversed(inventories[player_tag])]
    seen: set[int] = set()
    while pending:
        tag, where, parent = pending.pop()
        if tag in seen or tag not in items:
            continue
        seen.add(tag)
        item = items[tag]
        contents = [child for child in inventories.get(tag, ())
                    if child in items]
        is_open = bool(getattr(item, "flags", 0) & c.ITEM_CONTAINER_OPEN)
        if contents or is_open:
            # Visible contents are current, even an open container's
            # empty ones.
            if memory is not None:
                memory.contents[tag] = tuple(items[child].name
                                             for child in contents)
            pending.extend((child, CONTAINER, item.name)
                           for child in reversed(contents))
            if contents:
                continue
        elif memory is not None and memory.contents.get(tag):
            carried.extend(CarriedObject(name, REMEMBERED, item.name)
                           for name in memory.contents[tag])
            continue
        carried.append(CarriedObject(item.name, where, parent))
    if memory is not None and getattr(state, "inventory_replay_complete", True):
        # A container that left her inventory takes its memory with it.
        for tag in [tag for tag in memory.contents if tag not in seen]:
            del memory.contents[tag]
    return tuple(carried)


def lock_matches(lock_id: str, aliases: Iterable[str],
                 carried: Iterable[CarriedObject]) -> tuple[CarriedObject, ...]:
    """The carried objects that open one lock id.

    A name lock mirrors the server's exact, case-sensitive comparison of
    the object's name. A slaying lock is not visible to the client, so it
    goes through the key aliases, by case-folded name.
    """
    if lock_id.startswith(NAME_LOCK_PREFIX):
        wanted = lock_id[len(NAME_LOCK_PREFIX):]
        return tuple(obj for obj in carried if obj.name == wanted)
    names = {alias.casefold().strip() for alias in aliases}
    return tuple(obj for obj in carried
                 if obj.name.casefold().strip() in names)


@dataclass(slots=True, frozen=True)
class UnlockState:
    """One immutable snapshot of what opens gated tiles for her.

    ``access`` is what requirements.py evaluates: a frozenset of lock ids,
    or ``True`` for an explicit override that opens everything.
    """
    access: frozenset[str] | bool = frozenset()
    # Lock id -> the carried objects that open it.
    keys: tuple[tuple[str, tuple[CarriedObject, ...]], ...] = ()
    # Her quest journal as (quest, part, status); see quest_part().
    quests: tuple[tuple[str, str, str], ...] = ()
    quests_loaded: bool = False
    inventory_known: bool = True
    # LEARNING F8 hook: (map path, x, y) -> "passed" or "rejected", from her
    # own trials. Always empty until F8 lands; tile_allowed() reads it.
    learned_tiles: Mapping[tuple[str, int, int], str] = _EMPTY

    def tile_allowed(self, path: str, point: tuple[int, int],
                     requirement: Requirement | None) -> bool:
        """Whether a gated tile is open to her.

        A learned record never satisfies a ``Consumes`` atom: she passed by
        spending the item. Until F8 records exist, this is
        ``requirement.met(access)``.
        """
        if requirement is None:
            return True
        learned = self.learned_tiles.get((path, point[0], point[1]))
        if learned == "rejected":
            return False
        if requirement.met(self.access):
            return True
        return (learned == "passed" and
                not requirement.lock_ids(Consumes))

    def quest_part(self, quest: str, part: str = "") -> bool | None:
        """Whether a journal part (or, with no part, the quest) is done.

        None before the journal has loaded: unknown, not false. Journal
        names are what the client shows; P4's ``Quest`` atoms map them to
        part uids through the quest catalog.
        """
        if not self.quests_loaded:
            return None
        return any(name == quest and title == part and
                   status in ("done", "completed")
                   for name, title, status in self.quests)

    @property
    def fingerprint(self) -> str:
        """A short, stable digest of the item and quest state.

        ROUTING 4.4's ``inputs``: a learned rejection lasts until this
        changes. Names no item, so it can be stored and shown.
        """
        access = ("*" if self.access is True else
                  ",".join(sorted(self.access or ())))
        quests = ";".join(f"{q}\x1f{p}\x1f{s}" for q, p, s in self.quests)
        raw = f"{access}\x1e{int(self.quests_loaded)}\x1e{quests}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def summary(self, gates: Mapping[str, int] | None = None
                ) -> dict[str, object]:
        """A read-only view for the dashboard.

        ``gates`` maps every lock id in the graph to its gated tile count,
        so the view can say how many tiles her keys open.
        """
        held = dict(self.keys)
        rows = []
        for lock_id in sorted(held):
            rows.append({
                "lock": lock_id,
                "tiles": int((gates or {}).get(lock_id, 0)),
                "items": [{"name": obj.name, "where": obj.where,
                           "container": obj.container}
                          for obj in held[lock_id]],
            })
        return {
            "override": self.access is True,
            "inventory_known": self.inventory_known,
            "keys": rows,
            "locks_known": len(gates or {}),
            "quests_loaded": self.quests_loaded,
            "quest_parts_done": sum(status in ("done", "completed")
                                    for _, _, status in self.quests),
            "learned_tiles": len(self.learned_tiles),
            "fingerprint": self.fingerprint,
        }


def _journal(state) -> tuple[tuple[str, str, str], ...]:
    rows = []
    for title, progress in sorted((getattr(state, "quests", None) or {}).items()):
        section = str(getattr(progress, "status", "") or "")
        rows.append((title, "", "completed" if section == "completed" else
                     "active"))
        for part in getattr(progress, "parts", ()):
            rows.append((title, str(getattr(part, "name", "")),
                         str(getattr(part, "status", ""))))
    return tuple(rows)


def _snapshot(carried: tuple[CarriedObject, ...],
              aliases: Mapping[str, Iterable[str]],
              journal: tuple[tuple[str, str, str], ...], quests_loaded: bool,
              inventory_known: bool) -> UnlockState:
    keys = []
    for lock_id in sorted(aliases):
        matches = lock_matches(lock_id, aliases[lock_id], carried)
        if matches:
            keys.append((lock_id, matches))
    return UnlockState(
        access=frozenset(lock_id for lock_id, _ in keys), keys=tuple(keys),
        quests=journal, quests_loaded=quests_loaded,
        inventory_known=inventory_known)


def compute_unlock_state(state, aliases: Mapping[str, Iterable[str]], *,
                         memory: ContainerMemory | None = None
                         ) -> UnlockState:
    """Build her unlock state from a game state and the key aliases."""
    return _snapshot(
        carried_objects(state, memory), aliases, _journal(state),
        bool(getattr(state, "quests_loaded", False)),
        bool(getattr(state, "inventory_replay_complete", True)))


# One snapshot per (graph, inputs): plans call this many times per tick.
# Each entry keeps its graph, so a reused id() never returns another
# graph's snapshot.
_STATES: "OrderedDict[tuple, tuple[object, UnlockState]]" = OrderedDict()
_STATE_LIMIT = 32


def unlock_state(client, graph) -> UnlockState:
    """Her current unlock state, cached until one of its inputs changes."""
    state = client.state
    memory = container_memory(state)
    carried = carried_objects(state, memory)
    key = (id(graph), carried, _journal(state),
           bool(getattr(state, "quests_loaded", False)),
           bool(getattr(state, "inventory_replay_complete", True)))
    entry = _STATES.get(key)
    if entry is None or entry[0] is not graph:
        entry = (graph, _snapshot(
            carried, getattr(graph, "access_item_names", {}) or {}, *key[2:]))
        _STATES[key] = entry
        while len(_STATES) > _STATE_LIMIT:
            _STATES.popitem(last=False)
    _STATES.move_to_end(key)
    return entry[1]


def gate_counts(graph) -> dict[str, int]:
    """Every lock id that gates a tile in the graph, with its tile count."""
    counts: dict[str, int] = defaultdict(int)
    for node in getattr(graph, "nodes", {}).values():
        for requirement in getattr(node, "locks", {}).values():
            for lock_id in (requirement.lock_ids(Item) |
                            requirement.lock_ids(Consumes)):
                counts[lock_id] += 1
    return dict(counts)


def content_key_aliases(lock_ids: Iterable[str],
                        archetypes: Mapping[str, Mapping],
                        artifacts: Iterable[Mapping]) -> dict[str, set[str]]:
    """Key names from archetypes and artifacts whose ``slaying`` is a lock.

    Only slaying lock ids that gate a tile are kept, so an archetype's or
    artifact's race ``slaying`` (a weapon of undead slaying) never becomes
    a key. An artifact inherits ``name`` and ``slaying`` from its
    ``def_arch`` when it sets none; a quest reward ``<object arch=...>``
    names one of these, so it needs no separate source. Names are
    case-folded, like map-placed key aliases; ``name_pl`` is kept too,
    because a stack of keys is sent by its plural name.
    """
    wanted = {lock_id for lock_id in lock_ids
              if lock_id and not lock_id.startswith(NAME_LOCK_PREFIX)}
    aliases: dict[str, set[str]] = defaultdict(set)

    def scalar(attrs: Mapping, key: str) -> str:
        value = attrs.get(key)
        if isinstance(value, (list, tuple)):
            value = value[-1] if value else ""
        return str(value or "").strip()

    def add(attrs: Mapping, base: Mapping, default_name: str) -> None:
        slaying = scalar(attrs, "slaying") or scalar(base, "slaying")
        if slaying not in wanted:
            return
        # A plural belongs to the name it was written with: an artifact
        # that renames its key does not inherit the archetype's plural.
        source = attrs if scalar(attrs, "name") else base
        for name in (scalar(source, "name") or default_name,
                     scalar(source, "name_pl")):
            if name:
                aliases[slaying].add(name.casefold())

    for identity, record in archetypes.items():
        add(record.get("attrs", {}) or {}, {}, identity)
    for artifact in artifacts:
        base_id = str(artifact.get("def_arch") or "")
        base = (archetypes.get(base_id) or {}).get("attrs", {}) or {}
        add(artifact.get("attrs", {}) or {}, base,
            base_id or str(artifact.get("id") or ""))
    return dict(aliases)
