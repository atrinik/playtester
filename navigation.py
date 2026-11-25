"""Offline map graph, landmark index, and live route following."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import logging
import math
import multiprocessing
import os
import posixpath
import threading
import dataclasses
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
import types
from collections.abc import Callable, Iterable, Sequence
from typing import Any

try:
    from tools.world_content_audit import (ROOT as CONTENT_ROOT,
                                           artifact_inventory, flatten,
                                           load_archetypes, map_files,
                                           parse_blocks)
except ModuleNotFoundError:
    from world_content_audit import (ROOT as CONTENT_ROOT, artifact_inventory,
                                     flatten, load_archetypes, map_files,
                                     parse_blocks)

from . import constants as c
from . import dialogue_edges
from . import requirements as req
from . import route_blocks
from . import route_costs
from . import route_events
from . import planner
from . import route_shadow
from . import unlocks
from .equipment import abandon_gear_examine
from .route_blocks import NO_WALLS, OwnerWalls
from .route_costs import drain
from .requirements import NAME_LOCK_PREFIX, Requirement, name_lock_id
from .targeting import select_target, target_set_aside

from .alerts import HUNGRY_FOOD
from .client import AtrinikClient, format_money, map_object_visual_name
from .farm_analyzer import DEFAULT_OBJECTIVE
from . import farm_analyzer as farm_analyzer_plan
from . import self_improvement
from .model import STARTER_APARTMENT, Item
from .pathfinding import graph_search, grid_reachable, grid_search
from .tasks import (BankBalanceTask, BankTask, BindSavebedTask, BotTask,
                    BuyDialogueStockTask, BuyGroundItemsTask,
                    BuyHealingStockTask, BuyShopUpgradeTask,
                    DepositItemsTask, FarmTask, InventoryPolicy, JunkPolicy,
                    InventoryCapabilityTask, MassIdentifyTask, SafetyPolicy, safe_food,
                    RANGED_RESIDENT_MARKERS, RANGED_RESIDENT_RADIUS,
                    RetrieveItemsTask, SellJunkTask, TaskStatus,
                    TempleFoodTask, TempleServiceTask,
                    corpse_tile_standable, movement_ack_timeout,
                    healing_outpaced, recent_hostile_attackers,
                    recent_hostile_contact,
                    retreat_mobility, unresolved_hostile_contact,
                    combat_allowed, heal_waiting, out_of_heals_and_recall,
                    UnpreparedHold, wand_known_empty,
                    area_caster_identities, tiled_neighbours)
from .tasks import RECALL_REQUIRED_LEVEL, heal_consumables, reads_scrolls
log = logging.getLogger(__name__)


def content_attr(attrs: dict, key: str, default=None):
    """Read parsed-map list values and archetype scalar values uniformly."""
    value = attrs.get(key)
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return value[-1] if value else default
    return value


# Keep the graph compiler readable while avoiding world_content_audit.one(),
# which is intentionally shaped for parse_blocks()' list-valued attributes.
one = content_attr



def _graph_parse_workers() -> int:
    """Worker processes for map parsing: ATRINIK_GRAPH_WORKERS or up to 8 CPUs."""
    configured = os.environ.get("ATRINIK_GRAPH_WORKERS", "").strip()
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            return 1
    return max(1, min(8, os.cpu_count() or 1))


def _parse_map_file(path: Path) -> dict:
    return parse_blocks(path)


def _parse_map_files(paths: list[Path]) -> list[dict]:
    """Parse authored map files, in parallel where the platform allows.

    Parsing dominates WorldGraph.build(). Forked workers inherit the loaded
    content tooling; results return in input order, so the graph is identical
    to a sequential build. Falls back to sequential parsing without fork.
    """
    workers = min(_graph_parse_workers(), len(paths))
    if workers <= 1 or "fork" not in multiprocessing.get_all_start_methods():
        return [parse_blocks(path) for path in paths]
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        return pool.map(_parse_map_file, paths, chunksize=32)

@dataclass(slots=True, frozen=True)
class Waypoint:
    map_path: str
    x: int
    y: int
    name: str = ""
    archetype: str = ""


@dataclass(slots=True, frozen=True)
class NamedSpawn:
    map_path: str
    x: int
    y: int
    named: str
    candidates: tuple[str, ...]
    start_minute: int | None = None
    end_minute: int | None = None
    level: int = 0
    peaceful: bool = False
    aggro_radius: int = 0

    def available_at(self, minute: float) -> bool:
        if self.start_minute is None or self.end_minute is None:
            return True
        value = minute % (24 * 60)
        if self.start_minute <= self.end_minute:
            return self.start_minute <= value <= self.end_minute
        return value >= self.start_minute or value <= self.end_minute

    @property
    def ranged(self) -> bool:
        """Whether this spawn's names mark a ranged attacker."""
        return any(marker in name.casefold()
                   for name in (self.named, *self.candidates)
                   for marker in RANGED_RESIDENT_MARKERS)

    @property
    def pull_clearance_radius(self) -> int:
        """Tiles a pull square keeps from this resident."""
        radius = max(1, self.aggro_radius)
        return max(radius, RANGED_RESIDENT_RADIUS) if self.ranged else radius


@dataclass(slots=True, frozen=True)
class DialogueVendor:
    map_path: str
    x: int
    y: int
    name: str
    script: str
    stock: tuple[str, ...] = ()
    treasure_lists: tuple[str, ...] = ()
    level: int = 0


@dataclass(slots=True, frozen=True)
class FoodProfile:
    """Authored nutrition and carrying economics for one food archetype."""

    name: str
    nutrition: int
    value: int
    weight: float
    stackable: bool


class ServerGameClock:
    """Calibrated /time clock; one game minute is 25 ticks at 8 Hz."""

    real_seconds_per_game_minute = 25 / 8

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 resync_seconds: float = 600.0):
        self.clock = clock
        self.resync_seconds = max(60.0, float(resync_seconds))
        self.anchor_minute: float | None = None
        self.anchor_at = 0.0
        self.last_request_at: float | None = None
        self.message_index = 0

    @staticmethod
    def parse(text: str) -> int | None:
        match = re.search(
            r"It is [^,]+, (\d+) minute(?:s)? past (\d+) o'clock "
            r"(am|pm)", text, re.I)
        if match is None:
            return None
        minute, hour = int(match.group(1)), int(match.group(2)) % 12
        if match.group(3).casefold() == "pm":
            hour += 12
        return hour * 60 + minute

    def game_minute(self) -> float | None:
        if self.anchor_minute is None:
            return None
        elapsed = max(0.0, self.clock() - self.anchor_at)
        return (self.anchor_minute +
                elapsed / self.real_seconds_per_game_minute) % (24 * 60)

    async def sync(self, client: AtrinikClient) -> bool:
        for entry in client.state.messages_since(self.message_index):
            if len(entry) <= 3:
                continue
            parsed = self.parse(entry[3])
            if parsed is not None:
                self.anchor_minute = float(parsed)
                self.anchor_at = self.clock()
        self.message_index = client.state.message_count
        now = self.clock()
        due = (self.anchor_minute is None or self.last_request_at is None or
               now - self.last_request_at >= self.resync_seconds)
        command = getattr(client, "execute_client_command", None)
        if (command is not None and due and
                (self.last_request_at is None or
                 now - self.last_request_at >= 3)):
            await command("/time")
            self.last_request_at = now
            return True
        return False


@dataclass(slots=True, frozen=True)
class MapEdge:
    source: str
    destination: str
    x: int
    y: int
    destination_x: int = 0
    destination_y: int = 0
    kind: str = "exit"
    label: str = ""
    automatic: bool = False


@dataclass(slots=True)
class MapNode:
    path: str
    name: str = ""
    region: str = ""
    width: int = 0
    height: int = 0
    difficulty: int = 0
    enter_x: int = 0
    enter_y: int = 0
    edges: list[MapEdge] = field(default_factory=list)
    terrain: dict[tuple[int, int], int] = field(default_factory=dict)
    blocked: set[tuple[int, int]] = field(default_factory=set)
    # Gated tiles: locked doors and blocking inventory checkers, each with
    # what passing requires (requirements.py).
    locks: dict[tuple[int, int], Requirement] = field(default_factory=dict)
    doors: set[tuple[int, int]] = field(default_factory=set)
    occupied: set[tuple[int, int]] = field(default_factory=set)
    automatic_exits: set[tuple[int, int]] = field(default_factory=set)
    peaceful_identities: set[str] = field(default_factory=set)
    caster_identities: set[str] = field(default_factory=set)
    # The map header's ``pvp 1``: players may attack each other here.
    pvp: bool = False

    def lock_allowed(self, point: tuple[int, int], access=False) -> bool:
        """Check one gated tile against an access set (requirements.py)."""
        requirement = self.locks.get(point)
        return requirement is None or requirement.met(access)

    def walkable(self, x: int, y: int, *, allow_locked=False) -> bool:
        """Return static player walkability compiled from authored content."""
        return (0 <= x < self.width and 0 <= y < self.height and
                bool(self.terrain.get((x, y), 0) & 1) and
                (x, y) not in self.blocked and
                self.lock_allowed((x, y), allow_locked))


def _internal(path: Path, map_root: Path) -> str:
    return "/" + path.relative_to(map_root).as_posix()


def _resolve(source: str, destination: str) -> str:
    if destination.startswith("/"):
        value = destination
    else:
        value = posixpath.join(posixpath.dirname(source), destination)
    return posixpath.normpath(value)


def _coord(node: dict, parent: dict | None, name: str) -> int:
    value = one(node["attrs"], name)
    if value is None and parent is not None:
        value = one(parent["attrs"], name)
    return int(value or 0)


def _clear_components(graph) -> None:
    """Drop a graph's cached components, and their access-set order."""
    graph._component_cache.clear()
    route_open = getattr(graph, "_route_open_cache", None)
    if isinstance(route_open, dict):
        route_open.clear()
    order = getattr(graph, "_component_access_order", None)
    if isinstance(order, list):
        order.clear()


def _checker_requirement(attrs: dict, base: dict
                         ) -> tuple[Requirement, bool, str] | None:
    """Compile one inventory checker (type 64) as the server evaluates it.

    The server's ``check_inv()`` walks the player's inventory, descending
    into containers, and matches an object by ``slaying`` when the checker's
    ``sp`` is non-zero and by ``name`` otherwise; ``hp`` narrows by object
    type and ``race`` by archetype. ``blocked_tile()`` applies a checker
    only when ``last_grace`` is set: then a set ``last_sp`` requires a match
    and a zero one forbids it. Stepping on a checker with ``last_sp`` and
    ``last_heal`` set uses up the matched object, blocking or not.

    Returns the tile's requirement, whether the checker blocks, and the lock
    id it consumes (empty if none), or None if it has no routing effect.
    """
    def number(key: str) -> int:
        try:
            return int(one(attrs, key, base.get(key)) or 0)
        except (TypeError, ValueError):
            return 0

    slaying = str(one(attrs, "slaying", base.get("slaying")) or "")
    required, blocking = number("last_sp") != 0, number("last_grace") != 0
    consumes = required and number("last_heal") != 0
    if not blocking and not consumes:
        return None
    lock_id = ""
    if slaying:
        lock_id = slaying if number("sp") != 0 else name_lock_id(slaying)
    atoms = []
    if number("hp") or one(attrs, "race", base.get("race")):
        # Matching by object type or archetype is not visible to the client.
        atoms.append(req.Unknown("checker matches by object type or archetype"))
    if lock_id:
        atoms.append(req.Consumes(lock_id) if consumes else
                     req.Item(lock_id) if required else
                     req.NotItem(lock_id))
    elif not atoms:
        # No filter at all matches the first object she carries. Requiring
        # one is open in practice; forbidding one is closed.
        if required:
            return None
        atoms.append(req.Unknown("checker forbids carrying anything"))
    return req.requires(*atoms), blocking, lock_id if consumes else ""


def _multipart_footprints(
        archetypes: dict[str, dict], arch_root: Path,
        ) -> dict[str, tuple[tuple[int, int, dict], ...]]:
    """Map each primary archetype to its server-expanded More parts."""
    footprints: dict[str, list[tuple[int, int, dict]]] = defaultdict(list)
    for path in arch_root.rglob("*.arc"):
        primary = ""
        follows_more = False
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.strip()
            if line == "More":
                follows_more = True
                continue
            if not line.startswith("Object "):
                continue
            name = line[7:]
            if follows_more and primary:
                record = archetypes.get(name)
                if record is not None:
                    attrs = record["attrs"]
                    footprints[primary].append((
                        int(one(attrs, "x", 0) or 0),
                        int(one(attrs, "y", 0) or 0),
                        attrs,
                    ))
            else:
                primary = name
            follows_more = False
    return {
        name: tuple(parts) for name, parts in footprints.items()
    }


def _coordinate_name(path: str) -> tuple[str, int, int, int] | None:
    """Parse coordinate-map basenames the same way as server/map.c."""
    name = posixpath.basename(path)
    parts = name.split("_")
    values: list[tuple[int, int]] = []
    for index, part in enumerate(parts):
        if re.fullmatch(r"-?\d+", part) and len(part.lstrip("-")) <= 3:
            values.append((index, int(part)))
            if len(values) == 3:
                break
    if len(values) < 2:
        return None
    prefix = "_".join(parts[:values[0][0]])
    return prefix, values[0][1], values[1][1], (
        values[2][1] if len(values) > 2 else 0)


def _apartment_destinations(map_root: Path) -> dict[str, list[tuple[str, int, int]]]:
    """Load scripted apartment destinations without executing map Python."""
    path = map_root / "python" / "Apartments.py"
    try:
        tree = ast.parse(path.read_text(errors="replace"), filename=str(path))
    except (OSError, SyntaxError):
        return {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and
                   target.id == "apartments_info"
                   for target in statement.targets):
            continue
        try:
            raw = ast.literal_eval(statement.value)
        except (ValueError, TypeError, SyntaxError):
            return {}
        result: dict[str, list[tuple[str, int, int]]] = {}
        for region, region_info in raw.items():
            destinations = []
            for info in region_info.get("apartments", {}).values():
                destinations.append(
                    (str(info["path"]), int(info["x"]), int(info["y"])))
            result[str(region)] = destinations
        return result
    return {}


def _dialogue_seller_interfaces(
        map_root: Path) -> dict[str, frozenset[str] | None]:
    """Map XML merchant resources to their seller NPCs, or any attached NPC."""
    interfaces = map_root / "interfaces"
    sellers: dict[str, frozenset[str] | None] = {}
    if not interfaces.is_dir():
        return sellers
    for path in interfaces.rglob("*.xml"):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        merchant_interfaces = [
            interface for interface in root.iter("interface")
            if interface.get("inherit") in (
                "Merchant.Seller", "Merchant.SpellSeller")
        ]
        if not merchant_interfaces:
            continue
        resource = "/" + path.relative_to(map_root).as_posix()
        if any(not interface.get("npc")
               for interface in merchant_interfaces):
            sellers[resource] = None
        else:
            sellers[resource] = frozenset(
                str(interface.get("npc"))
                for interface in merchant_interfaces)
    return sellers


# Makes each planner view once (WorldGraph.planner_view).
_PLANNER_VIEW_LOCK = threading.Lock()


class WorldGraph:
    def __init__(self, map_root: Path | None = None):
        self.map_root = map_root or CONTENT_ROOT / "maps"
        self.nodes: dict[str, MapNode] = {}
        self.landmarks: list[Waypoint] = []
        self._landmarks_by_name: dict[str, list[Waypoint]] = defaultdict(list)
        self.named_spawns: dict[str, list[NamedSpawn]] = defaultdict(list)
        self.dialogue_vendors: list[DialogueVendor] = []
        # Temple priests (an /interfaces/temples/ dialogue), for services
        # such as the free food a starving character can ask for.
        self.temple_priests: list[DialogueVendor] = []
        self.food_profiles: dict[str, FoodProfile] = {}
        self.shop_stocks: dict[str, list[tuple[int, int]]] = defaultdict(list)
        # Savebed ("bed to reality") tiles per map, public and private.
        self.savebeds: dict[str, list[tuple[int, int]]] = defaultdict(list)
        # The authored name of each savebed, such as "hammock to reality".
        self.savebed_names: dict[tuple[str, int, int], str] = {}
        self.access_item_names: dict[str, set[str]] = defaultdict(set)
        self.monster_levels: dict[str, int] = {}
        # Archetype-level passive identities remain valid when the protocol
        # viewport shows a creature across a tiled-map seam. The object then
        # lies outside the current map, whose per-map identity set cannot
        # classify it.
        self.peaceful_monster_identities: set[str] = set()
        self.map_monster_levels: dict[str, dict[str, int]] = defaultdict(dict)
        # Identities of monsters whose spawn rerolls their level from the
        # map's difficulty (``item_condition``, server spawn_point.c): their
        # authored level is no guide to the level that spawns.
        self.map_rerolled_monsters: dict[str, set[str]] = defaultdict(set)
        self.map_roaming_monster_levels: dict[
            str, dict[str, int]] = defaultdict(dict)
        self._component_cache: dict[
            tuple[str, bool, tuple[int, int]], frozenset[tuple[int, int]]
        ] = {}
        self._aggro_pack_cache: dict[tuple[str, int, bool], int] = {}
        # route_open answers, by start component and inputs.
        self._route_open_cache: dict[tuple, bool] = {}
        self._seam_exposure_cache: dict[
            frozenset[str], farm_analyzer_plan.SeamExposure] = {}
        self._world_search_snapshot: tuple[
            tuple[str, ...], dict[str, int], tuple[int, ...],
            tuple[int, ...], tuple[MapEdge, ...]
        ] | None = None
        # route() under an access set, per set (_gated_route_snapshot).
        self._gated_route_snapshots: dict = {}
        # Dialogue voyages and quest-gated exits (dialogue_edges.py, P4).
        # Kept out of node.edges: only a plan with a DialogueOffer (the
        # owner's dialogue_edges flag) uses them.
        self.voyages: list[dialogue_edges.Voyage] = []
        self.gated_exits: dict[tuple[str, int, int], Requirement] = {}

    # The derived caches the planners fill as they search; a planner view
    # (planner_view) has its own.
    _PLANNER_CACHES = ("_component_cache", "_aggro_pack_cache", "_route_open_cache",
                       "_seam_exposure_cache", "_gated_route_snapshots")

    def planner_view(self, generation: int | None = None) -> "WorldGraph":
        """This graph for a planner worker (planner.py): the same map data,
        with caches of its own; one view per worker ``generation`` (the
        current worker's by default).

        After build the map data is never changed, so the view shares it.
        The derived caches are filled (and cleared) while a search runs, so
        the view has its own and the worker never changes one the loop is
        reading, nor reads one it changes. A defaultdict grows on a lookup
        of a missing key, so the view has its own copy of each of those
        too; each copy is one C-level dict copy, which the GIL keeps whole
        whichever thread makes it. Only one worker thread plans on a view:
        a worker that replaces a stuck one (planner.replace) has its own."""
        if self.__dict__.get("_is_planner_view"):
            return self
        if generation is None:
            generation = planner.generation()
        views = self.__dict__.setdefault("_planner_views", {})
        view = views.get(generation)
        if view is None:
            with _PLANNER_VIEW_LOCK:
                view = views.get(generation)
                if view is None:
                    view = copy.copy(self)
                    view.__dict__.pop("_component_access_order", None)
                    view.__dict__.pop("_planner_views", None)
                    for name in self._PLANNER_CACHES:
                        setattr(view, name, {})
                    view._world_search_snapshot = None
                    for name, value in list(view.__dict__.items()):
                        if isinstance(value, defaultdict):
                            setattr(view, name, copy.copy(value))
                    view._is_planner_view = True
                    views[generation] = view
                    # Older views go: only the worker before this one (an
                    # abandoned worker may still run) keeps its own.
                    for old in [old for old in views if old < generation - 1]:
                        del views[old]
        return view

    def build(self) -> "WorldGraph":
        archetypes = load_archetypes()
        multipart_footprints = _multipart_footprints(
            archetypes, CONTENT_ROOT / "arch")
        # The wire map protocol exposes a hostile's name/visual but not its
        # level. Build an exact semantic index from the same archetypes the
        # server loads so travel can avoid grossly over-levelled wildlife.
        for archetype, record in archetypes.items():
            attrs = record.get("attrs", {})
            if str(one(attrs, "type", "")) == str(c.TYPE_FOOD):
                name = str(one(
                    attrs, "name", archetype.replace("_", " ")) or
                    archetype.replace("_", " "))
                try:
                    profile = FoodProfile(
                        name=name,
                        nutrition=max(0, int(one(attrs, "food", 0) or 0)),
                        value=max(0, int(one(attrs, "value", 0) or 0)),
                        weight=max(
                            0.0, float(one(attrs, "weight", 0) or 0) /
                            1000.0),
                        stackable=(one(attrs, "can_stack", "0") == "1"),
                    )
                except (TypeError, ValueError):
                    profile = None
                if profile is not None and profile.nutrition > 0:
                    self.food_profiles[name.casefold()] = profile
            if one(attrs, "monster") != "1":
                continue
            try:
                level = int(one(attrs, "level", 0) or 0)
            except (TypeError, ValueError):
                continue
            names = {
                archetype.replace("_", " "),
                one(attrs, "name", "") or "",
                one(attrs, "animation", "") or "",
                one(attrs, "face", "") or "",
            }
            for raw_name in names:
                normalized = self._semantic_name(str(raw_name))
                if normalized:
                    self.monster_levels[normalized] = max(
                        level, self.monster_levels.get(normalized, 0))
        apartments = _apartment_destinations(self.map_root)
        seller_interfaces = _dialogue_seller_interfaces(self.map_root)
        parsed_maps: dict[str, tuple[Path, dict]] = {}
        paths = list(map_files())
        for path, parsed in zip(paths, _parse_map_files(paths)):
            header = parsed["header"]
            if header is None:
                continue
            key = _internal(path, self.map_root)
            attrs = header["attrs"]
            self.nodes[key] = MapNode(
                key, one(attrs, "name", "") or "", one(attrs, "region", "") or "",
                int(one(attrs, "width", 0) or 0), int(one(attrs, "height", 0) or 0),
                int(one(attrs, "difficulty", 0) or 0),
                int(one(attrs, "enter_x", 0) or 0),
                int(one(attrs, "enter_y", 0) or 0),
            )
            self.nodes[key].pvp = str(one(attrs, "pvp", "0") or "0").strip() not in ("", "0")
            parsed_maps[key] = (path, parsed)

        auto_exits: dict[str, list[tuple[int, int, int, str, bool]]] = defaultdict(list)
        apartment_entrances: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        apartment_returns: dict[str, list[tuple[str, int, int, str, bool]]] = defaultdict(list)
        consumed_locks: dict[tuple[str, int, int], set[str]] = defaultdict(set)
        # NPCs with an interface (map, x, y, name, resource), and exits
        # whose script is a recognised quest gate (dialogue_edges.py).
        npc_interfaces: list[tuple[str, int, int, str, str]] = []
        quest_gates: dict[tuple[str, int, int], tuple[str, str]] = {}
        for source, (path, parsed) in parsed_maps.items():
            node = self.nodes[source]
            header_attrs = parsed["header"]["attrs"]
            self._add_declared_tiles(node, header_attrs)
            self._add_coordinate_tiles(node)
            objects = list(flatten(parsed["objects"]))
            conditional_access: dict[tuple[int, int], str] = {}
            for hint, hint_parent in objects:
                hint_attrs = hint["attrs"]
                hint_base = archetypes.get(
                    hint["arch"], {}).get("attrs", {})
                if one(hint_attrs, "type", hint_base.get("type")) != "98":
                    continue
                hint_requirement = one(
                    hint_attrs, "slaying", hint_base.get("slaying"))
                if (hint_requirement and
                        one(hint_attrs, "last_sp",
                            hint_base.get("last_sp")) not in
                        (None, "", "0", 0)):
                    conditional_access[(
                        _coord(hint, hint_parent, "x"),
                        _coord(hint, hint_parent, "y"),
                    )] = hint_requirement
            for obj, parent in objects:
                attrs = obj["attrs"]
                base = archetypes.get(obj["arch"], {}).get("attrs", {})
                obj_type = one(attrs, "type", base.get("type"))
                name = one(attrs, "name", base.get("name")) or ""
                x, y = _coord(obj, parent, "x"), _coord(obj, parent, "y")
                terrain = int(one(attrs, "terrain_type",
                                  base.get("terrain_type")) or 0)
                if terrain:
                    node.terrain[(x, y)] = node.terrain.get((x, y), 0) | terrain
                authored_unpaid = (
                    one(attrs, "unpaid", base.get("unpaid")) == "1" and
                    obj_type != str(c.TYPE_MONEY))
                dynamic_shop_stock = bool(
                    obj["arch"].startswith("shop_floor") and
                    one(attrs, "auto_apply", base.get("auto_apply")) == "1" and
                    one(attrs, "randomitems", base.get("randomitems")))
                if authored_unpaid or dynamic_shop_stock:
                    point = (x, y)
                    if point not in self.shop_stocks[source]:
                        self.shop_stocks[source].append(point)
                if obj_type == "106" and (x, y) not in self.savebeds[source]:
                    self.savebeds[source].append((x, y))
                    self.savebed_names[(source, x, y)] = name
                # Normal doors are deliberately left to the server pathfinder,
                # which opens them just like the graphical client's click path.
                # Static walls, water-only terrain, furniture, and counters are
                # excluded from locally selected intermediate destinations.
                no_pass = one(attrs, "no_pass", base.get("no_pass"))
                if no_pass == "1" and obj_type not in ("20", "64", "66"):
                    node.blocked.add((x, y))
                if obj_type == "20":
                    node.doors.add((x, y))
                root_is_monster = (
                    one(attrs, "monster", base.get("monster")) == "1")
                # Map files place only a multipart archetype's anchor. The
                # server expands every More component at its relative offset;
                # compile those blocking/occupied squares as well.
                for dx, dy, part in multipart_footprints.get(
                        obj["arch"], ()):
                    point = x + dx, y + dy
                    if not (0 <= point[0] < node.width and
                            0 <= point[1] < node.height):
                        continue
                    part_type = one(part, "type")
                    if (one(part, "no_pass") == "1" and
                            part_type not in ("20", "64", "66")):
                        node.blocked.add(point)
                    if root_is_monster:
                        node.occupied.add(point)
                # Living authored spawns can move at runtime and therefore do
                # not divide the normal terrain mesh. They must, however, not
                # be selected as the exact arrival square of a tiled-map
                # crossing: the server cannot push through an NPC on the far
                # side and will repeatedly sidestep instead.
                if root_is_monster:
                    node.occupied.add((x, y))
                    try:
                        spawn_level = int(one(
                            attrs, "level", base.get("level")) or 0)
                    except (TypeError, ValueError):
                        spawn_level = 0
                    rerolled = str(one(
                        attrs, "item_condition",
                        base.get("item_condition")) or "0") != "0"
                    for raw_name in (
                            obj["arch"].replace("_", " "), name,
                            one(attrs, "animation",
                                base.get("animation")) or "",
                            one(attrs, "face", base.get("face")) or ""):
                        normalized = self._semantic_name(str(raw_name))
                        if normalized:
                            self.monster_levels[normalized] = max(
                                spawn_level,
                                self.monster_levels.get(normalized, 0))
                            node_levels = self.map_monster_levels[source]
                            node_levels[normalized] = max(
                                spawn_level, node_levels.get(normalized, 0))
                            if rerolled:
                                self.map_rerolled_monsters[source].add(
                                    normalized)
                    if one(attrs, "can_cast_spell",
                           base.get("can_cast_spell")) == "1":
                        for raw_name in (
                                obj["arch"].replace("_", " "), name,
                                one(attrs, "animation",
                                    base.get("animation")) or "",
                                one(attrs, "face", base.get("face")) or ""):
                            normalized = self._semantic_name(str(raw_name))
                            if normalized:
                                node.caster_identities.add(normalized)
                    for event in obj["children"]:
                        event_base = archetypes.get(
                            event["arch"], {}).get("attrs", {})
                        script = str(one(
                            event["attrs"], "race",
                            event_base.get("race")) or "")
                        if (script.startswith("/interfaces/") and
                                script.endswith(".xml") and
                                str(one(event["attrs"], "sub_type",
                                        event_base.get("sub_type"))) == "6"):
                            npc_interfaces.append((
                                source, x, y, str(name or obj["arch"]), script))
                        if script.startswith("/interfaces/temples/"):
                            priest = DialogueVendor(
                                source, x, y, str(name or obj["arch"]),
                                script, level=spawn_level)
                            if priest not in self.temple_priests:
                                self.temple_priests.append(priest)
                            continue
                        generic_seller = script.endswith((
                            "/generic/merchant.py",
                            "/generic/bartender.py",
                            "/generic/spell_seller.py"))
                        seller_npcs = seller_interfaces.get(script, frozenset())
                        if not generic_seller and \
                                seller_npcs is not None and \
                                str(name or obj["arch"]) not in seller_npcs:
                            continue
                        stock = []
                        for item in event["children"]:
                            item_base = archetypes.get(
                                item["arch"], {}).get("attrs", {})
                            item_name = one(
                                item["attrs"], "name",
                                item_base.get("name")) or \
                                item["arch"].replace("_", " ")
                            stock.append(str(item_name))
                        options = str(one(
                            event["attrs"], "slaying",
                            event_base.get("slaying")) or "")
                        stock.extend(value.strip() for value in
                                     options.split(",") if value.strip())
                        treasure_lists = []
                        raw_treasure = one(
                            event["attrs"], "msg", event_base.get("msg"))
                        if raw_treasure:
                            try:
                                generated = json.loads(str(raw_treasure))
                            except (TypeError, ValueError):
                                generated = []
                            treasure_lists.extend(
                                str(entry[0]) for entry in generated
                                if isinstance(entry, list) and entry and
                                entry[0])
                        vendor = DialogueVendor(
                            source, x, y, str(name or obj["arch"]), script,
                            tuple(dict.fromkeys(stock)),
                            tuple(dict.fromkeys(treasure_lists)), spawn_level)
                        if vendor not in self.dialogue_vendors:
                            self.dialogue_vendors.append(vendor)
                unaggressive = one(
                    attrs, "unaggressive",
                    base.get("unaggressive")) == "1"
                faction = one(attrs, "faction", base.get("faction"))
                authored_peaceful = unaggressive or (
                    obj_type == "83" and (
                        one(attrs, "friendly",
                            base.get("friendly")) == "1" or
                        one(attrs, "no_attack",
                            base.get("no_attack")) == "1" or
                        one(attrs, "invulnerable",
                            base.get("invulnerable")) == "1" or
                        bool(faction) and faction != "monsters"))
                if obj_type == "83" and authored_peaceful:
                    for raw_name in (
                            obj["arch"].replace("_", " "), name,
                            one(attrs, "animation",
                                base.get("animation")) or "",
                            one(attrs, "face", base.get("face")) or ""):
                        normalized = self._semantic_name(str(raw_name))
                        if normalized:
                            node.peaceful_identities.add(normalized)
                            if unaggressive:
                                self.peaceful_monster_identities.add(
                                    normalized)
                lock_id = one(attrs, "slaying", base.get("slaying"))
                door_lock_id = lock_id
                if (obj_type == "20" and not door_lock_id and
                        "locked" in obj["arch"].casefold()):
                    # One authored Ogre Guard passage places the intended
                    # key identifier on its colocated conditional magic-mouth
                    # warning instead of on the locked gate itself. The key's
                    # description explicitly names that one-way passage.
                    door_lock_id = conditional_access.get((x, y))
                if (obj_type == "20" and
                        (("locked" in obj["arch"].casefold() and
                          door_lock_id) or
                         one(attrs, "locked", base.get("locked")) == "1")):
                    node.locks[(x, y)] = node.locks.get(
                        (x, y), req.NONE) & req.requires(
                            req.Item(door_lock_id) if door_lock_id else
                            req.Unknown("locked door without a key id"))
                if obj_type == "64":
                    checker = _checker_requirement(attrs, base)
                    if checker is not None:
                        requirement, blocking, consumed = checker
                        if blocking:
                            # Blocking inventory checkers are authoritative
                            # access gates even when no locked door occupies
                            # their tile.
                            node.locks[(x, y)] = node.locks.get(
                                (x, y), req.NONE) & requirement
                        if consumed:
                            consumed_locks[(source, x, y)].add(consumed)
                        for atom in requirement.atoms:
                            lock = getattr(atom, "lock_id", "")
                            if lock.startswith(NAME_LOCK_PREFIX):
                                # A name match needs no key catalogue: the
                                # carried object's own name is the id.
                                self.access_item_names[lock].add(
                                    lock[len(NAME_LOCK_PREFIX):])
                if lock_id and name and obj_type == str(c.TYPE_KEY):
                    # Only KEY objects (type 21) carry a lock's slaying to
                    # the server's key_match(); a key ring container with
                    # slaying "locked" is not a key.
                    self.access_item_names[lock_id].add(name.casefold())
                if name:
                    point = Waypoint(source, x, y, name, obj["arch"])
                    self.landmarks.append(point)
                    self._landmarks_by_name[name.casefold()].append(point)
                explicit_name = one(attrs, "name")
                if (parent is not None and
                        parent["arch"] == "spawn_point" and
                        (one(attrs, "monster", base.get("monster")) == "1" or
                         obj_type == "83")):
                    candidates = []
                    aggro_radii = []
                    for sibling in parent["children"]:
                        sibling_base = archetypes.get(
                            sibling["arch"], {}).get("attrs", {})
                        sibling_type = one(
                            sibling["attrs"], "type", sibling_base.get("type"))
                        if (one(sibling["attrs"], "monster",
                                sibling_base.get("monster")) != "1" and
                                sibling_type != "83"):
                            continue
                        sibling_name = (one(sibling["attrs"], "name",
                                            sibling_base.get("name")) or
                                        sibling["arch"].replace("_", " "))
                        candidates.append(str(sibling_name))
                        aggro_radii.append(int(one(
                            sibling["attrs"], "item_power",
                            sibling_base.get("item_power")) or 0))
                    schedule = one(
                        attrs, "spawn_time", base.get("spawn_time")) or ""
                    schedule_match = re.fullmatch(
                        r"\s*(\d+):(\d+)\s*-\s*(\d+):(\d+)\s*",
                        schedule)
                    start_minute = end_minute = None
                    if schedule_match is not None:
                        sh, sm, eh, em = map(int, schedule_match.groups())
                        start_minute = sh * 60 + sm
                        end_minute = eh * 60 + em
                    spawn = NamedSpawn(
                        source, x, y, str(explicit_name or name),
                        tuple(dict.fromkeys(candidates)),
                        start_minute, end_minute,
                        int(one(attrs, "level", base.get("level")) or 0),
                        authored_peaceful, max(aggro_radii, default=0))
                    if spawn not in self.named_spawns[source]:
                        self.named_spawns[source].append(spawn)
                destination = one(attrs, "slaying", base.get("slaying"))
                automatic = (one(attrs, "walk_on", base.get("walk_on")) == "1" or
                             one(attrs, "fly_on", base.get("fly_on")) == "1")
                # Apartment entrance exits are redirected entirely by their
                # event script, so the ordinary exit has no authored slaying.
                # Compile every apartment tier for that region; live ownership
                # determines which one the script actually selects.
                if obj_type == "66":
                    for child in obj["children"]:
                        child_attrs = child["attrs"]
                        child_base = archetypes.get(
                            child["arch"], {}).get("attrs", {})
                        script = one(child_attrs, "race",
                                     child_base.get("race")) or ""
                        if not script.endswith("/apartment_teleport.py"):
                            continue
                        region = one(child_attrs, "slaying",
                                     child_base.get("slaying")) or ""
                        apartment_entrances[region].append((
                            source,
                            int(one(attrs, "hp", base.get("hp")) or 0),
                            int(one(attrs, "sp", base.get("sp")) or 0),
                        ))
                        for apartment_path, ax, ay in apartments.get(region, []):
                            if apartment_path in self.nodes:
                                node.edges.append(MapEdge(
                                    source, apartment_path, x, y, ax, ay,
                                    "exit",
                                    f"{name or obj['arch']} scripted apartment",
                                    automatic,
                                ))
                if obj_type == "66":
                    for child in obj["children"]:
                        child_attrs = child["attrs"]
                        child_base = archetypes.get(
                            child["arch"], {}).get("attrs", {})
                        script = one(child_attrs, "race",
                                     child_base.get("race")) or ""
                        if script.endswith("/apartment_out.py"):
                            region = one(child_attrs, "slaying",
                                         child_base.get("slaying")) or ""
                            apartment_returns[region].append(
                                (source, x, y, name or obj["arch"], automatic))
                if obj_type == "66" and destination:
                    node.edges.append(MapEdge(
                        source, _resolve(source, destination), x, y,
                        int(one(attrs, "hp", base.get("hp")) or 0),
                        int(one(attrs, "sp", base.get("sp")) or 0),
                        "exit", name or obj["arch"], automatic,
                    ))
                    # Some authored exit events conditionally redirect by
                    # appending a suffix to the ordinary exit path (notably
                    # Portal of Llwyfen's Nyhelobo encounter). Add that
                    # destination as an alternate graph edge; runtime quest
                    # state still decides which destination the server uses.
                    for child in obj["children"]:
                        child_attrs = child["attrs"]
                        child_base = archetypes.get(
                            child["arch"], {}).get("attrs", {})
                        script = one(child_attrs, "race",
                                     child_base.get("race"))
                        if not script or not script.endswith(".py"):
                            continue
                        script_path = (self.map_root /
                                       _resolve(source, script).lstrip("/"))
                        if not script_path.is_file():
                            continue
                        source_code = script_path.read_text(errors="replace")
                        gate = dialogue_edges.quest_gate(source_code)
                        if gate is not None:
                            quest_gates[(source, x, y)] = gate
                        suffix_match = re.search(
                            r"me\.slaying\s*\+\s*[\"']([^\"']+)[\"']",
                            source_code,
                        )
                        if suffix_match:
                            scripted_destination = _resolve(
                                source, destination + suffix_match.group(1))
                            if scripted_destination in self.nodes:
                                node.edges.append(MapEdge(
                                    source, scripted_destination, x, y,
                                    int(one(attrs, "hp", base.get("hp")) or 0),
                                    int(one(attrs, "sp", base.get("sp")) or 0),
                                    "exit", f"{name or obj['arch']} scripted",
                                    automatic,
                                ))
                elif obj_type == "66":
                    # Coordinate maps dynamically derive vertical tile paths.
                    # Destination-less stairs select those paths through
                    # last_heal=9/10; the transition occurs at the stair, not
                    # at the invented map-center edge used previously.
                    tiled = int(one(attrs, "last_heal",
                                    base.get("last_heal")) or 0)
                    if tiled in (9, 10):
                        vertical = self._coordinate_neighbor(source,
                                                             1 if tiled == 9 else -1)
                        if vertical in self.nodes:
                            node.edges.append(MapEdge(
                                source, vertical, x, y, x, y, "exit",
                                name or obj["arch"], automatic,
                            ))
                    else:
                        # exit_find() pairs destination-less exits of the same
                        # type/subtype within a five-tile radius. Shop mats use
                        # this to cross an otherwise impassable counter/grate.
                        subtype = int(one(attrs, "sub_type",
                                          base.get("sub_type")) or 0)
                        auto_exits[source].append(
                            (x, y, subtype, name or obj["arch"], automatic))

        # A consuming checker (last_heal) need not be the blocking one: in
        # underground_city_5_3_-1 a separate, non-blocking checker on the
        # gate's tile takes the force. Either way, passing that tile uses up
        # what it requires.
        for (source, x, y), lock_ids in consumed_locks.items():
            node = self.nodes[source]
            requirement = node.locks.get((x, y))
            if requirement is None:
                continue
            node.locks[(x, y)] = requirement & req.requires(*(
                req.Consumes(lock_id) for lock_id in sorted(lock_ids)
                if lock_id in requirement.lock_ids(req.Item)))

        # Apartment-out scripts return to the exact authored teleporter
        # recorded on entry. Compile all entrances for the region because the
        # per-player saved return map is runtime state.
        for region, exits in apartment_returns.items():
            for source, x, y, name, automatic in exits:
                node = self.nodes[source]
                for destination, dx, dy in apartment_entrances.get(region, []):
                    node.edges.append(MapEdge(
                        source, destination, x, y, dx, dy, "exit",
                        f"{name} scripted apartment return", automatic,
                    ))

        # Mirror the server's local automatically-connected exit search.
        for source, exits in auto_exits.items():
            node = self.nodes[source]
            for sx, sy, subtype, name, automatic in exits:
                for dx, dy, other_subtype, other_name, _ in exits:
                    if ((sx, sy) == (dx, dy) or subtype != other_subtype or
                            max(abs(dx - sx), abs(dy - sy)) > 5):
                        continue
                    node.edges.append(MapEdge(
                        source, source, sx, sy, dx, dy, "exit",
                        f"{name} auto-connected to {other_name}", automatic,
                    ))

        # Keep unresolved scripted destinations queryable but not routable.
        for node in self.nodes.values():
            node.edges[:] = [edge for edge in node.edges
                             if edge.destination in self.nodes and not (
                                 edge.kind == "tile" and
                                 (edge.x < 0 or edge.x >= node.width) and
                                 (edge.y < 0 or edge.y >= node.height) and
                                 not node.walkable(
                                     max(0, min(node.width - 1, edge.x)),
                                     max(0, min(node.height - 1, edge.y)),
                                 )
                             )]
            # Equivalent walk-on exits are safer than decorative/manual
        # variants and transition as soon as the server path reaches them.
            node.edges.sort(key=lambda edge: not edge.automatic)
            # A walk-on exit is a transition endpoint, not ordinary terrain.
            # Local paths may deliberately end on it through _transition(),
            # but must not use it as a shortcut: entering the square makes the
            # server teleport before the following local step can happen.
            node.automatic_exits = {
                (edge.x, edge.y) for edge in node.edges
                if edge.automatic and
                0 <= edge.x < node.width and 0 <= edge.y < node.height
            }
        # Monsters may walk over ordinary tiled-map seams. Keep neighboring
        # populations separate from native spawn coordinates so farm audits
        # see migration risk without inventing patrol points on the wrong map.
        for source, node in self.nodes.items():
            roaming = self.map_roaming_monster_levels[source]
            for edge in node.edges:
                if edge.kind != "tile" or edge.destination == source:
                    continue
                for identity, level in self.map_monster_levels.get(
                        edge.destination, {}).items():
                    roaming[identity] = max(
                        level, roaming.get(identity, 0))
        # Keys that no map places: quest rewards and NPC gifts are archetypes
        # or artifacts with the lock's slaying (unlocks.py).
        gated = unlocks.gate_counts(self)
        for lock_id, names in unlocks.content_key_aliases(
                gated, archetypes, artifact_inventory()).items():
            self.access_item_names[lock_id].update(names)
        # Only lock ids that gate a tile matter, and only they decide
        # whether a key name is ambiguous (unlocks.py).
        for lock_id in [lock_id for lock_id in self.access_item_names
                        if lock_id not in gated]:
            del self.access_item_names[lock_id]
        dialogue_edges.compile_graph(
            self, npc_interfaces, quest_gates,
            lambda source, destination, x, y, dx, dy, npc: MapEdge(
                source, destination, x, y, dx, dy, dialogue_edges.DIALOGUE,
                npc))
        return self

    @staticmethod
    def _semantic_name(value: str) -> str:
        value = value.casefold().strip().replace("_", " ")
        return re.sub(r"\.\d+$", "", value)

    def monster_level(self, *identities: str,
                      map_path: str = "") -> int:
        # Resolve an exact live name/animation to an authored monster level.
        normalized = tuple(self._semantic_name(value)
                           for value in identities if value)
        if map_path:
            paths = [map_path]
            node = self.nodes.get(map_path)
            if node is not None:
                paths.extend(edge.destination for edge in node.edges
                             if edge.kind == "tile")
            local = [
                self.map_monster_levels.get(path, {}).get(identity, 0)
                for path in paths for identity in normalized
            ]
            if any(local):
                return max(local)
        return max((
            self.monster_levels.get(identity, 0)
            for identity in normalized
        ), default=0)

    def level_rerolled(self, *identities: str, map_path: str) -> bool:
        """Whether a spawn on this map or one tiled to it rerolls this
        monster's level from the map difficulty (map_rerolled_monsters)."""
        normalized = {self._semantic_name(value)
                      for value in identities if value}
        paths = [map_path]
        node = self.nodes.get(map_path)
        if node is not None:
            paths.extend(edge.destination for edge in node.edges
                         if edge.kind == "tile")
        return any(normalized & self.map_rerolled_monsters.get(path, set())
                   for path in paths)

    def is_monster(self, name: str) -> bool:
        """Whether a name (as in "<name> hit you") is an authored monster
        kind: a monster archetype, or a monster placed on a map, by name,
        animation or face. A spell projectile, an effect or a trap is not."""
        return self._semantic_name(str(name)) in self.monster_levels

    def farm_priorities(self, map_path: str,
                        target_pattern: str) -> list[NamedSpawn]:
        if not target_pattern:
            return []
        pattern = re.compile(target_pattern, re.I)
        matches = [spawn for spawn in self.named_spawns.get(map_path, [])
                   if any(pattern.search(name)
                          for name in (spawn.named, *spawn.candidates))]
        # Authored proper-name variants remain the primary respawn target:
        # killing their ordinary replacement maximizes future boss rolls.
        # Among otherwise equivalent ordinary spawns, visit the highest-level
        # target first so a short circuit dwell is not consumed by grey,
        # zero-XP filler while worthwhile targets remain elsewhere on the map.
        return sorted(matches, key=lambda spawn: (
            not spawn.named[:1].isupper(), -spawn.level))

    def _max_aggro_pack(self, map_path: str, *, radius: int,
                        include_peaceful_target: bool) -> int:
        """Estimate direct simultaneous aggro at authored pull points.

        Each resident uses its source-authored ``item_power`` detection
        radius. Nearby spawn points are not transitively merged: being in one
        viewport-sized density pocket does not prove that every resident will
        acquire the player together. ``radius`` is retained in the cache/API
        key for callers that rebuild graphs with a different observation
        horizon, but source detection radii govern this estimate.
        """
        cache_key = map_path, radius, include_peaceful_target
        cached = self._aggro_pack_cache.get(cache_key)
        if cached is not None:
            return cached
        spawns = self.named_spawns.get(map_path, [])
        if not spawns:
            return 0
        maximum = 0
        for target in spawns:
            aggressive = sum(
                not spawn.peaceful and
                max(abs(target.x - spawn.x), abs(target.y - spawn.y)) <=
                spawn.aggro_radius
                for spawn in spawns)
            maximum = max(
                maximum,
                aggressive + int(
                    include_peaceful_target and target.peaceful))
        self._aggro_pack_cache[cache_key] = maximum
        return maximum

    def _seam_neighbours(self, map_path: str
                         ) -> list[tuple[str, int, int]]:
        """(map, x offset, y offset) of each map tiled to ``map_path`` on
        one of its eight sides, in ``map_path``'s coordinates. Vertical
        links (inside the map's bounds) are not neighbours."""
        node = self.nodes.get(map_path)
        if node is None:
            return []
        neighbours: dict[str, tuple[int, int]] = {}
        for edge in node.edges:
            other = self.nodes.get(edge.destination)
            if edge.kind != "tile" or other is None or edge.destination == map_path:
                continue
            dx = (-other.width if edge.x < 0 else
                  node.width if edge.x >= node.width else 0)
            dy = (-other.height if edge.y < 0 else
                  node.height if edge.y >= node.height else 0)
            if dx or dy:
                neighbours.setdefault(edge.destination, (dx, dy))
        return [(path, dx, dy) for path, (dx, dy) in sorted(neighbours.items())]

    def seam_exposure(self, map_paths: Iterable[str]
                      ) -> "farm_analyzer_plan.SeamExposure":
        """What maps tiled to a farm spot's maps add to its fights.

        The analyzer judges a spot's pack per map, from its own maps alone.
        A farm spot is pulled at its maps' authored spawns, from a square
        at the spawn's detection radius. A neighbour is a map tiled to one
        of the spot's maps, side or diagonal, outside the spot; the circuit
        never clears it. Its aggressive spawns join the pack at a spawn
        the analyzer's way, within their own detection radius of it, now
        across the seam; its casters within farm_analyzer.SEAM_REACH_TILES
        of the pull square, since a bolt flies as far as there is line of
        sight. Counting every melee spawn that near would put the open
        Burnt Forest at a pack of three to eight. The pack at each spawn
        adds them to its own map's and the spot's other maps' spawns within
        their detection radius, as for the analyzer, and is reported only
        where a seam adds to it (else 0): a neighbour's spawns, or the
        spot's other maps' within detection radius across its own seams,
        which the analyzer, judging each map alone, misses too. ``aggressive`` counts every
        neighbour spawn within SEAM_REACH_TILES. Distances are Chebyshev,
        ignoring walls, as in the analyzer's own estimate.
        """
        spot = frozenset(path for path in map_paths if path in self.nodes)
        cached = self._seam_exposure_cache.get(spot)
        if cached is not None:
            return cached
        reach = farm_analyzer_plan.SEAM_REACH_TILES
        pack = 0
        near: set[tuple[str, int, int]] = set()
        casters: set[tuple[str, int, int]] = set()
        maps: set[str] = set()
        for path in sorted(spot):
            farm = self.named_spawns.get(path, ())
            if not farm:
                continue
            around = []
            for other, dx, dy in self._seam_neighbours(path):
                node = self.nodes[other]
                for spawn in self.named_spawns.get(other, ()):
                    if not spawn.peaceful:
                        caster = any(
                            self._semantic_name(name) in node.caster_identities
                            for name in (spawn.named, *spawn.candidates))
                        around.append((spawn, dx, dy, other in spot, caster))
            own = [spawn for spawn in farm if not spawn.peaceful]
            for target in farm:
                ring = max(1, target.aggro_radius)
                local = int(target.peaceful) + sum(
                    max(abs(target.x - spawn.x), abs(target.y - spawn.y)) <=
                    spawn.aggro_radius for spawn in own)
                seam = 0
                for spawn, dx, dy, inside, caster in around:
                    distance = max(abs(target.x - spawn.x - dx),
                                   abs(target.y - spawn.y - dy))
                    detects = distance <= spawn.aggro_radius
                    if inside:
                        # The spot's other map, across its own seam: the
                        # analyzer, judging each map alone, misses it too.
                        seam += detects
                        continue
                    key = (spawn.map_path, spawn.x, spawn.y)
                    if distance - ring <= reach:
                        near.add(key)
                        maps.add(spawn.map_path)
                        if caster:
                            casters.add(key)
                    if detects or (caster and distance - ring <= reach):
                        seam += 1
                if seam:
                    pack = max(pack, local + seam)
        exposure = farm_analyzer_plan.SeamExposure(
            pack, len(near), len(casters), tuple(sorted(maps)))
        self._seam_exposure_cache[spot] = exposure
        return exposure

    def farm_max_aggro_pack(self, map_path: str, *, radius: int = 8) -> int:
        """Estimate the maximum pack when a peaceful farm target is pulled."""
        return self._max_aggro_pack(
            map_path, radius=radius, include_peaceful_target=True)

    def transit_max_aggro_pack(self, map_path: str, *, radius: int = 8) -> int:
        """Conservatively bound a moving traveler's proximity exposure."""
        spawns = [spawn for spawn in self.named_spawns.get(map_path, [])
                  if not spawn.peaceful]
        if not spawns:
            return 0
        remaining = set(range(len(spawns)))
        maximum = 0
        while remaining:
            component = {remaining.pop()}
            frontier = list(component)
            while frontier:
                source = spawns[frontier.pop()]
                joined = {
                    index for index in remaining
                    if max(abs(source.x - spawns[index].x),
                           abs(source.y - spawns[index].y)) <= radius
                }
                remaining.difference_update(joined)
                component.update(joined)
                frontier.extend(joined)
            maximum = max(maximum, len(component))
        return maximum

    def _add_declared_tiles(self, node: MapNode, attrs: dict) -> None:
        # N,E,S,W,NE,SE,SW,NW. Vertical links are included when declared.
        points = (
            (node.width // 2, -1), (node.width, node.height // 2),
            (node.width // 2, node.height), (-1, node.height // 2),
            (node.width, -1), (node.width, node.height),
            (-1, node.height), (-1, -1),
            (node.width // 2, node.height // 2),
            (node.width // 2, node.height // 2),
        )
        for index, (x, y) in enumerate(points, 1):
            destination = one(attrs, f"tile_path_{index}")
            if destination:
                node.edges.append(MapEdge(
                    node.path, _resolve(node.path, destination), x, y,
                    kind="tile", label=f"tile_path_{index}",
                ))

    def _add_coordinate_tiles(self, node: MapNode) -> None:
        coordinate = _coordinate_name(node.path)
        if coordinate is None:
            return
        prefix, x, y, z = coordinate
        parent = posixpath.dirname(node.path)
        neighbors = (
            (0, -1, 0, node.width // 2, -1),
            (1, 0, 0, node.width, node.height // 2),
            (0, 1, 0, node.width // 2, node.height),
            (-1, 0, 0, -1, node.height // 2),
            (1, -1, 0, node.width, -1),
            (1, 1, 0, node.width, node.height),
            (-1, 1, 0, -1, node.height),
            (-1, -1, 0, -1, -1),
            (0, 0, 1, node.width // 2, node.height // 2),
            (0, 0, -1, node.width // 2, node.height // 2),
        )
        for dx, dy, dz, wx, wy in neighbors:
            # Vertical coordinate neighbours require an authored stair. They
            # are added while parsing type-66 objects above.
            if dz:
                continue
            nz = z + dz
            suffix = f"_{x + dx}_{y + dy}" + (f"_{nz}" if nz else "")
            destination = f"{parent}/{prefix}{suffix}"
            if destination in self.nodes and not any(
                    edge.destination == destination for edge in node.edges):
                node.edges.append(MapEdge(
                    node.path, destination, wx, wy, kind="tile",
                    label=f"coordinate {dx},{dy},{dz}",
                ))

    @staticmethod
    def _coordinate_neighbor(source: str, dz: int) -> str:
        coordinate = _coordinate_name(source)
        if coordinate is None:
            return ""
        prefix, x, y, z = coordinate
        z += dz
        suffix = f"{prefix}_{x}_{y}" + (f"_{z}" if z else "")
        return posixpath.join(posixpath.dirname(source), suffix)

    def local_path(self, map_path: str, start: tuple[int, int],
                   goal: tuple[int, int], *,
                   allow_locked: bool = False,
                   excluded: set[tuple[int, int]] | None = None) -> list[tuple[int, int]]:
        """Find an authored-terrain path within one map for safe click legs."""
        node = self.nodes.get(map_path)
        if node is None:
            return []
        exceptional_start = not node.walkable(
            *start, allow_locked=allow_locked)

        def traversable(point: tuple[int, int]) -> bool:
            if (node.walkable(*point, allow_locked=allow_locked) and
                    (point not in node.automatic_exits or
                     point == start or point == target)):
                return True
            # A live position can arrive in a sparse authored-map margin whose
            # cells have no floor record. The server treats an otherwise empty
            # cell as passable. Permit only that empty margin while escaping
            # an authoritative exceptional start; explicit terrain, walls,
            # locks and occupied objects remain excluded.
            return (
                exceptional_start and
                0 <= point[0] < node.width and
                0 <= point[1] < node.height and
                point not in node.terrain and
                point not in node.blocked and
                point not in node.occupied and
                node.lock_allowed(point, allow_locked)
            )
        gx = max(0, min(node.width - 1, goal[0]))
        gy = max(0, min(node.height - 1, goal[1]))
        target = (gx, gy)
        outside_x = goal[0] < 0 or goal[0] >= node.width
        outside_y = goal[1] < 0 or goal[1] >= node.height
        # Component routing resolves a tiled-map edge to one exact mutually
        # walkable crossing. Honor its orthogonal coordinate here. Treating an
        # adjusted goal as “any square on this border” caused the follower to
        # cross immediately from the wrong row and enter another component.
        candidates = [target]
        candidates = [point for point in candidates if traversable(point)]
        if not candidates:
            return []
        if start in candidates:
            return [start]
        walkability = bytes(
            traversable((x, y))
            for y in range(node.height)
            for x in range(node.width)
        )
        excluded_states = (
            y * node.width + x for x, y in (excluded or ())
            if 0 <= x < node.width and 0 <= y < node.height
        )
        result = grid_search(
            node.width, node.height, walkability,
            start[1] * node.width + start[0],
            (point[1] * node.width + point[0] for point in candidates),
            excluded=excluded_states,
        )
        if result.status != "found":
            return []
        return [(state % node.width, state // node.width)
                for state in result.path]

    # Components are cached per access set; the cache keeps this many sets,
    # least recently used first out (her access set changes only when her
    # keys do).
    COMPONENT_ACCESS_SETS = 4

    def _note_component_access(self, access) -> None:
        order = self.__dict__.setdefault("_component_access_order", [])
        if order and order[-1] == access:
            return
        if access in order:
            order.remove(access)
            order.append(access)
            return
        order.append(access)
        if len(order) > self.COMPONENT_ACCESS_SETS:
            dropped = order.pop(0)
            for key in [key for key in self._component_cache
                        if key[1] == dropped]:
                del self._component_cache[key]

    def _component(self, map_path: str, start: tuple[int, int], *,
                   allow_locked: bool = False) -> frozenset[tuple[int, int]]:
        """Return the complete static walkable component containing start."""
        node = self.nodes.get(map_path)
        cache_key = map_path, allow_locked, start
        self._note_component_access(allow_locked)
        cached = self._component_cache.get(cache_key)
        if cached is not None:
            return cached
        if node is None or not (
                0 <= start[0] < node.width and 0 <= start[1] < node.height):
            return frozenset()
        # The live server position is authoritative. A movable object, runtime
        # map mutation, or deliberately conservative static classification can
        # make that one square appear blocked offline. Permit routing outward
        # from it, while every subsequently visited square still must pass the
        # authored walkability test.
        start_walkable = node.walkable(*start, allow_locked=allow_locked)
        def traversable(point: tuple[int, int]) -> bool:
            return (
                (node.walkable(*point, allow_locked=allow_locked) and
                 (point not in node.automatic_exits or point == start)) or
                (not start_walkable and
                 0 <= point[0] < node.width and
                 0 <= point[1] < node.height and
                 point not in node.terrain and
                 point not in node.blocked and
                 point not in node.occupied and
                 node.lock_allowed(point, allow_locked))
            )
        walkability = bytes(
            traversable((x, y))
            for y in range(node.height)
            for x in range(node.width)
        )
        status, states, _ = grid_reachable(
            node.width, node.height, walkability,
            start[1] * node.width + start[0],
        )
        if status != "complete":
            return frozenset()
        component = frozenset(
            (state % node.width, state // node.width) for state in states)
        if start_walkable:
            for point in component:
                self._component_cache[(map_path, allow_locked, point)] = component
        else:
            # Do not let the exceptional live square merge otherwise separate
            # static components for future graph states.
            self._component_cache[cache_key] = component
        return component

    def _tile_transitions(self, edge: MapEdge, start: tuple[int, int], *,
                          allow_locked: bool = False
                          ) -> list[tuple[MapEdge, tuple[int, int]]]:
        """Return one reachable border crossing per destination component."""
        source = self.nodes.get(edge.source)
        destination = self.nodes.get(edge.destination)
        if source is None or destination is None:
            return []
        # A cardinal tile link is a whole shared border, not one fixed
        # midpoint. Preserve crossings into every distinct component on the
        # adjacent map; a building may straddle a map boundary beside a street.
        component = self._component(
            edge.source, start, allow_locked=allow_locked)
        crossings: list[tuple[tuple[int, int], tuple[int, int],
                              tuple[int, int]]] = []
        if edge.x < 0 and 0 <= edge.y < source.height:
            for y in range(min(source.height, destination.height)):
                crossings.append(((-1, y), (0, y),
                                  (destination.width - 1, y)))
        elif edge.x >= source.width and 0 <= edge.y < source.height:
            for y in range(min(source.height, destination.height)):
                crossings.append(((source.width, y),
                                  (source.width - 1, y), (0, y)))
        elif edge.y < 0 and 0 <= edge.x < source.width:
            for x in range(min(source.width, destination.width)):
                crossings.append(((x, -1), (x, 0),
                                  (x, destination.height - 1)))
        elif edge.y >= source.height and 0 <= edge.x < source.width:
            for x in range(min(source.width, destination.width)):
                crossings.append(((x, source.height),
                                  (x, source.height - 1), (x, 0)))
        if not crossings:
            return []
        viable = [item for item in crossings
                  if item[1] in component and source.walkable(
                      *item[1], allow_locked=allow_locked) and
                  destination.walkable(
                      *item[2], allow_locked=allow_locked) and
                  item[2] not in destination.occupied]
        best: dict[frozenset[tuple[int, int]],
                   tuple[tuple[int, int], tuple[int, int], tuple[int, int]]] = {}
        def rank(item):
            return (
                max(abs(item[1][0] - start[0]),
                    abs(item[1][1] - start[1])),
                abs(item[1][0] - edge.x) + abs(item[1][1] - edge.y),
            )
        for item in viable:
            destination_component = self._component(
                edge.destination, item[2], allow_locked=allow_locked)
            current = best.get(destination_component)
            if destination_component and (
                    current is None or rank(item) < rank(current)):
                best[destination_component] = item
        return [(replace(edge, x=crossing[0], y=crossing[1]), arrival)
                for crossing, _, arrival in sorted(best.values(), key=rank)]

    def _transition(self, edge: MapEdge, start: tuple[int, int], *,
                    allow_locked: bool = False
                    ) -> tuple[MapEdge, tuple[int, int]] | None:
        """Resolve one reachable edge and its exact destination arrival square."""
        source = self.nodes.get(edge.source)
        destination = self.nodes.get(edge.destination)
        if source is None or destination is None:
            return None
        if edge.kind == "tile":
            transitions = self._tile_transitions(
                edge, start, allow_locked=allow_locked)
            return transitions[0] if transitions else None
        if edge.kind == dialogue_edges.DIALOGUE:
            # A voyage departs from beside the NPC and arrives where its
            # teleport puts her.
            arrival = edge.destination_x, edge.destination_y
            if not (self.dialogue_approaches(
                    edge, start, allow_locked=allow_locked) and
                    destination.walkable(*arrival, allow_locked=allow_locked)):
                return None
            return edge, arrival
        path = self.local_path(
            edge.source, start, (edge.x, edge.y),
            allow_locked=allow_locked)
        if not path:
            return None
        departure = path[-1]
        if edge.kind == "exit":
            arrival = (
                edge.destination_x if edge.destination_x >= 0 else destination.enter_x,
                edge.destination_y if edge.destination_y >= 0 else destination.enter_y,
            )
            adjusted = edge
        elif edge.x < 0 and edge.y < 0:
            adjusted = replace(edge, x=-1, y=-1)
            arrival = destination.width - 1, destination.height - 1
        elif edge.x >= source.width and edge.y < 0:
            adjusted = replace(edge, x=source.width, y=-1)
            arrival = 0, destination.height - 1
        elif edge.x >= source.width and edge.y >= source.height:
            adjusted = replace(edge, x=source.width, y=source.height)
            arrival = 0, 0
        elif edge.x < 0 and edge.y >= source.height:
            adjusted = replace(edge, x=-1, y=source.height)
            arrival = destination.width - 1, 0
        elif edge.x < 0:
            adjusted = replace(edge, x=-1, y=departure[1])
            arrival = destination.width - 1, min(
                destination.height - 1, departure[1])
        elif edge.x >= source.width:
            adjusted = replace(edge, x=source.width, y=departure[1])
            arrival = 0, min(destination.height - 1, departure[1])
        elif edge.y < 0:
            adjusted = replace(edge, x=departure[0], y=-1)
            arrival = min(destination.width - 1, departure[0]), destination.height - 1
        elif edge.y >= source.height:
            adjusted = replace(edge, x=departure[0], y=source.height)
            arrival = min(destination.width - 1, departure[0]), 0
        else:
            # A same-coordinate vertical tile declaration is not itself a
            # usable transition; its authored type-66 stair supplies the edge.
            return None
        if not destination.walkable(*arrival, allow_locked=allow_locked):
            return None
        return adjusted, arrival

    def dialogue_approaches(self, edge: MapEdge, start: tuple[int, int], *,
                            allow_locked=False) -> list[tuple[int, int]]:
        """The squares beside a voyage's NPC that she can walk to from
        ``start``, nearest first: where she talks to it from."""
        node = self.nodes.get(edge.source)
        if node is None:
            return []
        component = self._component(edge.source, start,
                                     allow_locked=allow_locked)
        squares = [
            (edge.x + dx, edge.y + dy)
            for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy]
        squares = [
            point for point in squares
            if point in component and point not in node.automatic_exits and
            point not in node.occupied]
        return sorted(squares, key=lambda point: (
            max(abs(point[0] - start[0]), abs(point[1] - start[1])), point))

    def _component_states(self, source: str, source_xy: tuple[int, int],
                          destination: str | None, *,
                          allow_locked=False,
                          excluded: set[tuple[str, str]] | None = None,
                          avoided_maps: set[str] | None = None,
                          penalized_maps: set[str] | None = None,
                          walls: OwnerWalls | None = None,
                          offer: dialogue_edges.DialogueOffer | None = None,
                          stop_maps: set[str] | None = None):
        """Every walkable component reachable from ``source_xy``, and how
        (_expand_component_states, run to the end)."""
        return drain(self._expand_component_states(
            source, source_xy, destination, allow_locked=allow_locked,
            excluded=excluded, avoided_maps=avoided_maps,
            penalized_maps=penalized_maps, walls=walls, offer=offer,
            stop_maps=stop_maps))

    def _expand_component_states(
            self, source: str, source_xy: tuple[int, int],
            destination: str | None, *,
            allow_locked=False,
            excluded: set[tuple[str, str]] | None = None,
            avoided_maps: set[str] | None = None,
            penalized_maps: set[str] | None = None,
            walls: OwnerWalls | None = None,
            offer: dialogue_edges.DialogueOffer | None = None,
            stop_maps: set[str] | None = None,
            edge_cost: Callable[[MapEdge, tuple[int, int], tuple[int, int]], float]
            | None = None):
        """Every walkable component reachable from ``source_xy``, and how.

        A generator: it yields after each state it expands, so a caller
        with a per-tick time budget (route_shadow.py) can spread one search
        over several ticks; drain() runs it to the end. Its return value is
        the search below.

        Breadth-first over exact walkable components across maps: the
        states (map, component key), the arrival square of each, and the
        transitions between them as a CSR graph with route metadata and
        penalty flags. ``avoided_maps`` are never entered, except the
        ``destination``; apartments are entered only as the destination.
        ``walls`` are the owner's blocks (route_blocks): a blocked exit or
        seam is never taken, a blocked map never entered except as the
        ``destination``. ``offer`` (dialogue_edges.py, the owner's
        ``dialogue_edges`` flag) adds its voyages and closes the quest-gated
        exits it names; without one the graph is the static one.
        ``stop_maps`` ends the search once it reaches one of them.
        ``edge_cost`` (route_costs.py, ROUTING P6) prices each transition
        from (edge, the square she leaves from, the arrival square); its
        costs replace the penalty flags in the result's sixth field.
        """
        start_component = self._component(
            source, source_xy, allow_locked=allow_locked)
        if not start_component:
            raise ValueError(f"current position {source_xy} is not walkable on {source}")
        start_key = source, min(start_component)
        states = [start_key]
        state_ids = {start_key: 0}
        positions = [source_xy]
        offsets = [0]
        targets: list[int] = []
        edge_metadata: list[int] = []
        edge_costs: list[int] = []
        route_edges: list[MapEdge] = []
        cursor = 0
        while cursor < len(states):
            state = states[cursor]
            map_path, _ = state
            position = positions[cursor]
            node = self.nodes.get(map_path)
            if stop_maps and map_path in stop_maps:
                break
            if node is not None:
                edges = node.edges if offer is None else (
                    *node.edges, *offer.from_map(map_path))
                for edge in edges:
                    if offer is not None and offer.closes(edge):
                        continue
                    if excluded and (
                            (edge.source, edge.destination) in excluded or
                            (edge.source, edge.destination, edge.x, edge.y)
                            in excluded):
                        continue
                    if (avoided_maps and edge.destination in avoided_maps and
                            edge.destination != destination):
                        continue
                    if walls and walls.blocks(edge, destination):
                        continue
                    # An apartment's out script returns to the per-player entrance
                    # recorded at runtime. Static alternate return edges describe
                    # possibilities, not a teleport network. Enter an apartment
                    # only when it is the requested destination; when starting
                    # inside, use its exit and then replan from the actual map.
                    if ("/apartments/apartment_" in edge.destination and
                            edge.destination != destination):
                        continue
                    if edge.kind == "tile":
                        transitions = self._tile_transitions(
                            edge, position, allow_locked=allow_locked)
                    else:
                        transition = self._transition(
                            edge, position, allow_locked=allow_locked)
                        transitions = [transition] if transition else []
                    for adjusted, arrival in transitions:
                        component = self._component(
                            edge.destination, arrival,
                            allow_locked=allow_locked)
                        if not component:
                            continue
                        next_state = edge.destination, min(component)
                        target = state_ids.get(next_state)
                        if target is None:
                            target = len(states)
                            state_ids[next_state] = target
                            states.append(next_state)
                            positions.append(arrival)
                        targets.append(target)
                        edge_metadata.append(len(route_edges))
                        if edge_cost is None:
                            edge_costs.append(edge.destination in (penalized_maps or ()))
                        else:
                            edge_costs.append(edge_cost(adjusted, position, arrival))
                        route_edges.append(adjusted)
            offsets.append(len(targets))
            cursor += 1
            yield
        return (states, positions, offsets, targets, edge_metadata,
                edge_costs, route_edges)

    def reachable_maps(self, source: str, source_xy: tuple[int, int], *,
                       allow_locked=False,
                       avoided_maps: set[str] | None = None,
                       offer: dialogue_edges.DialogueOffer | None = None,
                       stop_maps: set[str] | None = None) -> frozenset[str]:
        """Maps she can walk to from ``source_xy`` without entering
        ``avoided_maps``: one whole-world expansion, as route_points does,
        with ``offer``'s voyages, ended early once one of ``stop_maps`` is
        reached."""
        states = self._component_states(
            source, source_xy, None, allow_locked=allow_locked,
            avoided_maps=avoided_maps, offer=offer, stop_maps=stop_maps)[0]
        return frozenset(map_path for map_path, _ in states)

    ROUTE_OPEN_CACHE_SIZE = 256

    def route_open(self, *args, **kwargs) -> bool:
        """route_open_iter run to the end."""
        return drain(self.route_open_iter(*args, **kwargs))

    def route_open_iter(self, source: str, source_xy: tuple[int, int],
                        destination: str,
                        destination_points: list[tuple[int, int]], *,
                        allow_locked=False,
                        excluded: set[tuple] | None = None,
                        avoided_maps: set[str] | None = None,
                        offer: dialogue_edges.DialogueOffer | None = None):
        """Whether route_points would find a route (no walls, no
        penalties), without building it; a generator that yields as it
        expands (_expand_component_states), returning the answer. Cheap
        first: the search stops on reaching the destination map (a few
        milliseconds); only a miss there runs the whole expansion (about
        1.4 s for the Old Outpost with both stairs closed). Answers are
        cached per graph by her start component and the inputs (cleared
        with the components)."""
        start = self._component(source, source_xy, allow_locked=allow_locked)
        if not start:
            return False
        key = (source, start, destination, tuple(destination_points),
               frozenset(excluded or ()), frozenset(avoided_maps or ()),
               allow_locked, offer)
        cache = self.__dict__.setdefault("_route_open_cache", {})
        cached = cache.get(key)
        if cached is not None:
            return cached

        result = False
        for stop_maps in ({destination}, None):
            states, positions = (yield from self._expand_component_states(
                source, source_xy, destination, allow_locked=allow_locked,
                excluded=excluded, avoided_maps=avoided_maps, offer=offer,
                stop_maps=stop_maps))[:2]
            if any(map_path == destination and (
                    not destination_points or any(
                        self.local_path(map_path, position, point,
                                        allow_locked=allow_locked)
                        for point in destination_points))
                   for (map_path, _), position in zip(states, positions)):
                result = True
                break
        if len(cache) >= self.ROUTE_OPEN_CACHE_SIZE:
            cache.clear()
        cache[key] = result
        return result

    def route_points(self, source: str, source_xy: tuple[int, int],
                     destination: str,
                     destination_points: list[tuple[int, int]], *,
                     allow_locked: bool = False,
                     excluded: set[tuple[str, str]] | None = None,
                     avoided_maps: set[str] | None = None,
                     penalized_maps: set[str] | None = None,
                     walls: OwnerWalls | None = None,
                     offer: dialogue_edges.DialogueOffer | None = None,
                     edge_cost: Callable[[MapEdge, tuple[int, int], tuple[int, int]], float]
                     | None = None,
                     ) -> list[MapEdge]:
        """Route end-to-end between exact walkable components across maps.

        Entering a ``penalized_maps`` map costs more than any route without
        one, so the route crosses as few of them as it can and, among those,
        is the shortest. ``walls`` (the owner's blocks) are never crossed.
        ``offer`` adds dialogue voyages and closes quest-gated exits
        (dialogue_edges.py); None plans on the static graph. ``edge_cost``
        (route_costs.py) makes it the cheapest route by those costs instead,
        penalized maps ignored (route_points_iter).
        """
        return drain(self.route_points_iter(
            source, source_xy, destination, destination_points,
            allow_locked=allow_locked, excluded=excluded,
            avoided_maps=avoided_maps, penalized_maps=penalized_maps,
            walls=walls, offer=offer, edge_cost=edge_cost))

    def route_points_iter(self, source: str, source_xy: tuple[int, int],
                          destination: str,
                          destination_points: list[tuple[int, int]], *,
                          allow_locked: bool = False,
                          excluded: set[tuple[str, str]] | None = None,
                          avoided_maps: set[str] | None = None,
                          penalized_maps: set[str] | None = None,
                          walls: OwnerWalls | None = None,
                          offer: dialogue_edges.DialogueOffer | None = None,
                          edge_cost: Callable[[MapEdge, tuple[int, int], tuple[int, int]],
                                              float] | None = None):
        """route_points as a generator that yields while it expands the
        world (_expand_component_states); its return value is the route.
        With ``edge_cost`` each transition costs that many seconds, in
        tenths (route_costs.COST_SCALE) and at least one, and the native
        search is Dijkstra over them."""
        states, positions, offsets, targets, edge_metadata, edge_costs, \
            route_edges = yield from self._expand_component_states(
                source, source_xy, destination, allow_locked=allow_locked,
                excluded=excluded, avoided_maps=avoided_maps,
                penalized_maps=penalized_maps, walls=walls, offer=offer,
                edge_cost=edge_cost)
        goals = [
            index for index, ((map_path, _), position)
            in enumerate(zip(states, positions))
            if map_path == destination and (
                not destination_points or any(
                    self.local_path(map_path, position, point,
                                    allow_locked=allow_locked)
                    for point in destination_points))]
        if not goals:
            raise ValueError(
                f"no component route from {source} {source_xy} to "
                f"{destination} {destination_points}")
        if edge_cost is not None:
            costs = [route_costs.scaled(cost) for cost in edge_costs]
        else:
            # A simple route has fewer transitions than there are states, so
            # one penalized entry outweighs any detour: crossings first, then
            # length.
            penalty = len(states) + 1
            costs = ([1 + penalty * flagged for flagged in edge_costs]
                     if penalized_maps else None)
        result = graph_search(
            offsets, targets, 0, goals, metadata=edge_metadata, costs=costs)
        if result.status != "found":
            raise ValueError(
                f"no component route from {source} {source_xy} to "
                f"{destination} {destination_points}")
        return [route_edges[index] for index in result.transitions]

    def find_landmarks(self, query: str, *, archetype: str = "") -> list[Waypoint]:
        pattern = re.compile(query, re.I)
        return [point for point in self.landmarks
                if pattern.search(point.name) and
                (not archetype or point.archetype == archetype)]

    def find_dialogue_vendors(
            self, query: str = "", *,
            treasure_lists: tuple[str, ...] = ()) -> list[DialogueVendor]:
        """Find compiled seller/bartender stock without opening dialogue."""
        pattern = re.compile(query, re.I) if query else None
        treasures = {value.casefold() for value in treasure_lists}
        return [
            vendor for vendor in self.dialogue_vendors
            if (pattern is None or any(pattern.search(value) for value in
                                       (vendor.name, *vendor.stock))) and
            (not treasures or treasures.intersection(
                value.casefold() for value in vendor.treasure_lists))
        ]

    def dialogue_food_stock(
            self, vendor: DialogueVendor) -> tuple[FoodProfile, ...]:
        """Return typed authored food stock, independent of naming heuristics."""
        return tuple(
            self.food_profiles[name.casefold()]
            for name in vendor.stock
            if name.casefold() in self.food_profiles)

    def _entry_points(self, edge: MapEdge) -> list[tuple[int, int]]:
        """Return possible authored arrival squares for a graph edge."""
        source = self.nodes.get(edge.source)
        destination = self.nodes.get(edge.destination)
        if source is None or destination is None:
            return []
        if edge.kind == "exit":
            point = edge.destination_x, edge.destination_y
            return [point] if destination.walkable(*point) else []
        if edge.x < 0 and edge.y < 0:
            candidates = [(destination.width - 1, destination.height - 1)]
        elif edge.x >= source.width and edge.y < 0:
            candidates = [(0, destination.height - 1)]
        elif edge.x >= source.width and edge.y >= source.height:
            candidates = [(0, 0)]
        elif edge.x < 0 and edge.y >= source.height:
            candidates = [(destination.width - 1, 0)]
        elif edge.x < 0:
            candidates = [(destination.width - 1, y)
                          for y in range(destination.height)]
        elif edge.x >= source.width:
            candidates = [(0, y) for y in range(destination.height)]
        elif edge.y < 0:
            candidates = [(x, destination.height - 1)
                          for x in range(destination.width)]
        elif edge.y >= source.height:
            candidates = [(x, 0) for x in range(destination.width)]
        else:
            candidates = []
        return [point for point in candidates if destination.walkable(*point)]

    def routes_into_component(self, source: str, destination: str,
                              points: list[tuple[int, int]], *,
                              source_point: tuple[int, int] | None = None,
                              excluded: set[tuple[str, str]] | None = None,
                              access=None) -> list[list[MapEdge]]:
        """Find routes which enter the destination component containing points."""
        routes: list[list[MapEdge]] = []
        for node in self.nodes.values():
            for edge in node.edges:
                if edge.destination != destination:
                    continue
                if excluded and (edge.source, edge.destination) in excluded:
                    continue
                arrivals = self._entry_points(edge)
                if not any(self.local_path(destination, arrival, point,
                                           allow_locked=False if access is None else access)
                           for arrival in arrivals for point in points):
                    continue
                try:
                    prefix = self.route(source, edge.source,
                                        excluded=excluded, access=access,
                                        source_xy=source_point)
                except ValueError:
                    continue
                route = prefix + [edge]
                if not route:
                    continue
                if (source_point is not None and route[0].source == source and
                        not self.local_path(
                            source, source_point,
                            (route[0].x, route[0].y),
                            allow_locked=False if access is None else access)):
                    continue
                routes.append(route)
        routes.sort(key=len)
        return routes

    def _route_base(self):
        """The map-level search graph: one state per map, gates ignored."""
        if self._world_search_snapshot is None:
            paths = tuple(self.nodes)
            state_ids = {
                path: index for index, path in enumerate(paths)
            }
            offsets = [0]
            targets: list[int] = []
            route_edges: list[MapEdge] = []
            for current in paths:
                for edge in self.nodes[current].edges:
                    target = state_ids.get(edge.destination)
                    if target is None:
                        continue
                    targets.append(target)
                    route_edges.append(edge)
                offsets.append(len(targets))
            self._world_search_snapshot = (
                paths, state_ids, tuple(offsets), tuple(targets),
                tuple(route_edges),
            )
            self._gated_route_snapshots = {}
        return self._world_search_snapshot

    def _edge_endpoints(self, edge: MapEdge
                        ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        """The (departure, arrival) squares an edge can be crossed by.

        An exit has one; a cardinal tile seam has one per shared border
        row or column, as ``_tile_transitions`` crosses it; a corner seam
        has its corner squares. A tile declaration inside the map has none,
        as in ``_transition``.
        """
        source = self.nodes.get(edge.source)
        destination = self.nodes.get(edge.destination)
        if source is None or destination is None:
            return []
        if edge.kind != "tile":
            return [((edge.x, edge.y), (
                edge.destination_x if edge.destination_x >= 0
                else destination.enter_x,
                edge.destination_y if edge.destination_y >= 0
                else destination.enter_y))]
        west, east = edge.x < 0, edge.x >= source.width
        north, south = edge.y < 0, edge.y >= source.height
        if (west or east) and (north or south):
            return [((0 if west else source.width - 1,
                      0 if north else source.height - 1),
                     (destination.width - 1 if west else 0,
                      destination.height - 1 if north else 0))]
        if west or east:
            return [((0 if west else source.width - 1, y),
                     (destination.width - 1 if west else 0, y))
                    for y in range(min(source.height, destination.height))]
        if north or south:
            return [((x, 0 if north else source.height - 1),
                     (x, destination.height - 1 if north else 0))
                    for x in range(min(source.width, destination.width))]
        return []

    def _gated_route_snapshot(self, access):
        """The map-level search graph under one access set.

        A map where ``access`` leaves a gated tile closed becomes one state
        per walkable component its edges touch, as ``route_points`` sees
        it; every other map stays one state. Cached per access set, and
        rebuilt with the map-level graph.
        """
        paths, state_ids, _, _, route_edges = self._route_base()
        key = access if isinstance(access, bool) else frozenset(access)
        cached = self._gated_route_snapshots.get(key)
        if cached is not None:
            return cached
        split = {path for path, node in self.nodes.items() if node.locks and
                 not all(requirement.met(access)
                         for requirement in node.locks.values())}
        state_maps = list(paths)
        components: dict[tuple[str, tuple[int, int]], int] = {}

        def state_of(path: str, point: tuple[int, int]) -> int | None:
            if path not in split:
                return state_ids[path]
            node = self.nodes[path]
            if not node.walkable(*point, allow_locked=access):
                return None
            component = self._component(path, point, allow_locked=access)
            # A walk-on exit joins the component only as its start: key
            # by the rest, so leaving by it and arriving beside it agree.
            rest = component - node.automatic_exits
            if not rest:
                return None
            identity = path, min(rest)
            if identity not in components:
                components[identity] = len(state_maps)
                state_maps.append(path)
            return components[identity]

        adjacency: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for index, edge in enumerate(route_edges):
            if edge.source not in split and edge.destination not in split:
                adjacency[state_ids[edge.source]].append(
                    (state_ids[edge.destination], index))
                continue
            pairs = set()
            for departure, arrival in self._edge_endpoints(edge):
                start = state_of(edge.source, departure)
                end = state_of(edge.destination, arrival)
                if start is not None and end is not None:
                    pairs.add((start, end))
            for start, end in sorted(pairs):
                adjacency[start].append((end, index))
        offsets, targets, metadata = [0], [], []
        for state in range(len(state_maps)):
            for target, index in adjacency.get(state, ()):
                targets.append(target)
                metadata.append(index)
            offsets.append(len(targets))
        map_states: dict[str, list[int]] = defaultdict(list)
        for state, path in enumerate(state_maps):
            if path not in split or state >= len(paths):
                map_states[path].append(state)
        snapshot = (dict(map_states), components, tuple(offsets),
                    tuple(targets), tuple(metadata), split)
        if len(self._gated_route_snapshots) >= 8:
            self._gated_route_snapshots.clear()
        self._gated_route_snapshots[key] = snapshot
        return snapshot

    def route(self, source: str, destination: str, *,
              excluded: set[tuple[str, str]] | None = None,
              avoided_maps: set[str] | None = None,
              access=None,
              source_xy: tuple[int, int] | None = None) -> list[MapEdge]:
        """The fewest map transitions from ``source`` to ``destination``.

        A map-level estimate: walking inside a map is not checked. With
        ``access`` (her access set, unlocks.py), a gate it leaves closed
        splits its map into the components the gate separates, so the route
        never passes it; ``source_xy`` then picks her component of a split
        source map, otherwise any of them may start the route. Without
        ``access`` gates are ignored (tests and offline tools).
        """
        if source == destination:
            return []
        paths, state_ids, offsets, targets, route_edges = self._route_base()
        if source not in state_ids or destination not in state_ids:
            raise ValueError(f"no route from {source} to {destination}")

        def skipped(edge: MapEdge) -> bool:
            return bool(
                (excluded and (edge.source, edge.destination) in excluded) or
                (avoided_maps and edge.destination in avoided_maps and
                 edge.destination != destination) or
                ("/apartments/apartment_" in edge.destination and
                 edge.destination != destination))

        if access is None:
            result = graph_search(
                offsets, targets, state_ids[source], (state_ids[destination],),
                metadata=range(len(route_edges)),
                excluded_edges=(index for index, edge in enumerate(route_edges)
                                if skipped(edge)),
            )
            if result.status != "found":
                raise ValueError(f"no route from {source} to {destination}")
            return [route_edges[index] for index in result.transitions]
        map_states, components, offsets, targets, metadata, split = (
            self._gated_route_snapshot(access))
        starts = map_states.get(source, [])
        if (source in split and source_xy is not None and
                self.nodes[source].walkable(*source_xy, allow_locked=access)):
            # An unwalkable live square (a closed gate she stands on, an
            # unmapped margin) keeps every component as a possible start.
            component = self._component(source, source_xy,
                                        allow_locked=access)
            rest = component - self.nodes[source].automatic_exits
            if rest:
                # Her part of the map; one no edge touches has no way out.
                mine = components.get((source, min(rest)))
                starts = [] if mine is None else [mine]
        goals = tuple(map_states.get(destination, ()))
        excluded_edges = frozenset(
            position for position, index in enumerate(metadata)
            if skipped(route_edges[index]))
        best: list[MapEdge] | None = None
        for start in starts:
            result = graph_search(
                offsets, targets, start, goals, metadata=metadata,
                excluded_edges=excluded_edges)
            if result.status == "found" and (
                    best is None or len(result.transitions) < len(best)):
                best = [route_edges[index] for index in result.transitions]
        if best is None:
            raise ValueError(f"no route from {source} to {destination}")
        return best


# Essential trips (ROUTING 4.5): never declined into stranding. Food counts
# only while she is hungry or starving (NavigateTask.is_essential); a
# funding trip pays for a cure or the depletion service; "home" is the
# recovery phase's walk to her savebed, from which every recovery errand
# starts. Recall shopping is not one: a working recall is never a blocker
# (owner decision, 2026-11-22), so it never crosses an owner block.
ESSENTIAL_TRIPS = frozenset({"food", "cure", "depletion", "savebed", "funding",
                             "home"})
# The care rule's wait (ROUTING 4.5, P3). A budget or strict-plan override
# (CARE_BOUNDED, from P6) waits at most this long, then goes anyway and
# records essential-care-timeout. An owner-block crossing (CARE_OWNER_BLOCK)
# waits without limit and raises a dashboard alert after this long.
CARE_WAIT_SECONDS = 600.0
CARE_OWNER_BLOCK = "owner-block"
CARE_BOUNDED = "bounded"
# A safe place to wait (NavigateTask._care_place): no creature that is not
# authored peaceful this close.
CARE_SAFE_RADIUS = 8
# Within this many tiles of her savebed she waits there.
CARE_HOME_TOLERANCE = 2
# After a failed walk to her savebed, she waits where she stands this long
# before trying again.
CARE_STAGING_RETRY_SECONDS = 120.0
# Resting at a safe place, HP or SP that has not risen for this long counts
# as full: a disease or poison may keep it below the maximum.
CARE_PLATEAU_SECONDS = 60.0
# A plateau also ends when the vital falls more than this share of its
# maximum below the plateau's peak: damage over time stays within it (the
# worst in the content, pneumonic plague, takes 11 % a pulse and regenerates
# back), steady damage from an attacker she cannot see does not.
CARE_PLATEAU_BAND = 0.15
# A gap this long between two care checks (a fight held the trip, which
# skips them) forgets the plateau: what happened meanwhile is unknown.
CARE_PLATEAU_GAP_SECONDS = 5.0
# A new trip's owner-block wait for the same subjects keeps the start of one
# left within this long (autoplay's handover rebuilds trips).
CARE_WAIT_RESUME_SECONDS = 300.0


class OwnerBlockRefusal(Exception):
    """An optional trip's only route crosses an owner block. Not a
    ValueError, so no hazard tier catches it and relaxes (NavigateTask._plan
    turns it into one)."""


def learned_route_costs(client: Any) -> bool:
    """The owner's learned_route_costs flag (ROUTING P6/P7): live plans use
    learned costs, budgets and the essential override. Off (the default),
    plans are as before and the learned plans run in the shadow
    (route_shadow.py)."""
    return bool(getattr(getattr(client, "config", None), "learned_route_costs", False))


def learned_route(snapshot: route_costs.TripSnapshot):
    """Plan ``snapshot``'s trip with learned costs (ROUTING 4.5): a
    generator that yields while it expands the world and returns a
    route_costs.LearnedPlan.

    One weighted search: each edge costs its travel time plus the entered
    map's content prior and learned danger (route_costs.CostContext), so
    content hazards are costs, never walls, and the safest route wins.
    Walls are the owner's blocks (P3's rules: an essential trip crosses
    one with care when every route does; any other trip is BLOCKED), and
    for a strict trip the strict avoid set, with learned danger at
    STRICT_LEARNED_AVOID_SECONDS or more in place of route_hazards. A
    strict trip that finds no route is NO_ROUTE, or, if essential, takes
    the cheapest route with CARE_BOUNDED care (OVERRIDE). Any other trip but
    a farm trip has a budget: learned danger at or above it DECLINES an
    optional trip and makes an essential one an OVERRIDE. Content priors
    never count toward a budget.
    """
    graph = snapshot.graph
    context = snapshot.context
    base = {"allow_locked": snapshot.access, "excluded": snapshot.excluded,
            "offer": snapshot.offer, "edge_cost": context.edge_cost}
    args = (snapshot.source, snapshot.source_xy, snapshot.destination, snapshot.goals)

    def search(avoided):
        try:
            route = yield from graph.route_points_iter(
                *args, avoided_maps=avoided, walls=snapshot.walls or None, **base)
            return route, ()
        except ValueError as walled:
            if not snapshot.walls:
                raise
            route = yield from graph.route_points_iter(*args, avoided_maps=avoided, **base)
            crossed = snapshot.walls.crossings(route, snapshot.destination)
            if not crossed:
                raise walled from None
            if not snapshot.essential:
                raise OwnerBlockRefusal(
                    f"owner block: every route to {snapshot.destination} crosses "
                    f"{', '.join(crossed)} ({walled})") from None
            return route, crossed

    outcome, care, relaxed = route_costs.PLANNED, "", False
    try:
        if snapshot.strict:
            try:
                if (snapshot.destination != snapshot.source and
                        snapshot.destination in snapshot.strict_avoided):
                    raise ValueError(f"{snapshot.destination} is itself a known hazard")
                route, crossed = yield from search(set(snapshot.strict_avoided))
            except ValueError as exc:
                if not snapshot.essential:
                    return route_costs.LearnedPlan(
                        None, route_costs.NO_ROUTE, snapshot.trip,
                        reason=f"no route to {snapshot.destination} avoiding every "
                               f"known hazard: {exc}")
                route, crossed = yield from search(None)
                outcome, care, relaxed = route_costs.OVERRIDE, CARE_BOUNDED, True
        else:
            route, crossed = yield from search(None)
    except OwnerBlockRefusal as refusal:
        return route_costs.LearnedPlan(None, route_costs.BLOCKED, snapshot.trip,
                                       reason=str(refusal))
    except ValueError as exc:
        return route_costs.LearnedPlan(None, route_costs.NO_ROUTE, snapshot.trip,
                                       reason=str(exc))
    cost = context.evaluate(route, snapshot.source_xy,
                            lambda edge: _edge_arrival(graph, edge))
    budget = route_costs.plan_budget(snapshot)
    # A death counts in full for the budget until it is older than its
    # half-life (owner decision, 2026-11-19): budget_learned.
    if budget is not None and cost.budget_learned >= budget:
        worst = ", ".join(f"{subject} {value:.0f} s" for subject, value in cost.worst)
        if not snapshot.essential:
            return route_costs.LearnedPlan(
                None, route_costs.DECLINED, snapshot.trip, cost=cost, budget=budget,
                candidate=route,
                reason=f"route declined: learned danger {cost.budget_learned:.0f} s on "
                       f"the way to {snapshot.destination} is over the {snapshot.trip} "
                       f"budget of {budget:.0f} s ({worst})")
        outcome, care = route_costs.OVERRIDE, CARE_BOUNDED
    return route_costs.LearnedPlan(route, outcome, snapshot.trip, cost=cost, budget=budget,
                                   care=care, crossed=crossed, strict_relaxed=relaxed)


def danger_weight(client: Any, *, optional: bool) -> float:
    """λ (ROUTING 4.5): SAFETY_LAMBDA while she plays for safety, is
    depleted or has no ready recall, or on an optional trip; else 1."""
    try:
        recall = bool(FarmTask.emergency_recall_candidates(client))
    except Exception:
        recall = True
    state = client.state
    careful = (effective_farm_objective(state) == "safety" or
               depleted_for_temple(state) or not recall or optional)
    return route_costs.SAFETY_LAMBDA if careful else 1.0


def bounded_care_subjects(plan: route_costs.LearnedPlan,
                          snapshot: route_costs.TripSnapshot) -> tuple[str, ...]:
    """What a budget or strict-plan override crosses with care: the
    learned subjects that make its danger, else (an override for content
    hazards alone) the route's maps in the strict avoid set or with a
    content prior at the avoid level, else every map it enters."""
    view, context = snapshot.context.view, snapshot.context
    route = plan.route or ()

    def learned_subjects(threshold: float) -> list[str]:
        found = []
        for edge in route:
            if view.budget_danger(edge.destination) >= threshold > 0 or (
                    threshold <= 0 and view.budget_danger(edge.destination) > 0):
                found.append(route_blocks.map_subject(edge.destination))
            subject = context.subject(edge)
            value = view.edge_danger(subject)
            if (value >= threshold > 0) or (threshold <= 0 and value > 0):
                found.append(subject)
        return found

    # Every subject on the route with learned danger at the strict level,
    # else every one with any (not the truncated worst list).
    subjects = (learned_subjects(route_costs.STRICT_LEARNED_AVOID_SECONDS) or
                learned_subjects(0.0))
    if not subjects:
        priors = snapshot.context.priors
        subjects = [route_blocks.map_subject(edge.destination) for edge in plan.route or ()
                    if edge.destination in snapshot.strict_avoided or
                    priors.get(edge.destination, 0.0) >= route_costs.PRIOR_OVERLEVEL_SECONDS]
    if not subjects:
        subjects = [route_blocks.map_subject(edge.destination) for edge in plan.route or ()]
    return tuple(dict.fromkeys(subjects))


def _care_waits(client: AtrinikClient) -> dict[tuple[str, ...], tuple[float, float, bool]]:
    """Owner-block care waits by (trip kind, *subjects): (start, last seen,
    alerted), per client."""
    waits = getattr(client, "_care_waits", None)
    if not isinstance(waits, dict):
        waits = {}
        try:
            client._care_waits = waits
        except AttributeError:
            pass
    return waits


@dataclass(slots=True)
class CareCrossing:
    """One trip's care crossing (NavigateTask.require_care)."""

    mode: str
    subjects: tuple[str, ...]
    since: float
    clock: Callable[[], float] = time.monotonic
    cleared: bool = False
    cleared_at: float = 0.0
    # Her route no longer crosses anything: hold-fire ends.
    finished: bool = False
    starving: bool = False
    alerted: bool = False
    holding: bool = False
    missing: tuple[str, ...] = ()
    # The (trip kind, *subjects) key of _care_waits, and whether this is a
    # food trip.
    key: tuple[str, ...] = ()
    food: bool = False
    # Per vital ("hp", "sp"): the highest value seen resting and since when;
    # and the last care check.
    peaks: dict = field(default_factory=dict)
    plateaued: bool = False
    checked_at: float = 0.0
    # The subjects a budget or strict-plan override (CARE_BOUNDED) crosses;
    # kept when an owner block makes the crossing CARE_OWNER_BLOCK, so its
    # destination hold-fire survives the merge.
    bounded: tuple[str, ...] = ()
    # Whether a recall was ready when it cleared; without one she crossed
    # with hold-fire (and, off a food trip, full HP and SP) only.
    recall_ready: bool = False


def care_recall_ready(client: AtrinikClient, *, starving: bool = False) -> bool:
    """The care rule's recall condition: a recall ready now, when she owns
    one she could ready. Without one she crosses with the rest of the care
    rule alone: a working recall is never a blocker (owner decision,
    2026-11-22), so owning only empty or unusable sources never makes her
    wait.

    She owns one when she carries a source FarmTask.emergency_recall_candidates
    could fire once ready: the Word of Recall spell, or a paid, identified,
    uncursed rod or wand not known to be empty. A spell costing more SP
    than her maximum could never be readied, so it does not count. While
    starving, a spell she lacks the SP for does not count either: at food 0
    the server regenerates no SP (player_do_some_living), so waiting for it
    would only starve her.
    """
    policy = InventoryPolicy()
    sp = int(client.state.stats.get("sp", 0) or 0)
    maxsp = int(client.state.stats.get("maxsp", 0) or 0)
    owned = False
    for item in client.state.inventory:
        if (not re.search(r"\bword of recall\b", item.name, re.I) or
                item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)):
            continue
        if item.item_type == c.TYPE_SPELL:
            cost = int(item.extra.get("cost", 0) or 0)
            if cost > maxsp or starving and cost > sp:
                continue
            owned = True
        elif (item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and policy.identified(item)
              and not wand_known_empty(client, item)):
            owned = True
    return not owned or bool(FarmTask.emergency_recall_candidates(client))


def mark_essential(task: BotTask | None, kind: str) -> BotTask | None:
    """Mark ``task``'s navigation essential (ESSENTIAL_TRIPS), and any
    navigation it starts later (MorgeeanShipKeyTask)."""
    seen: set[int] = set()
    pending = [task]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if hasattr(current, "essential"):
            current.essential = kind
        for attribute in ("navigation", "chest_navigation", "return_navigation",
                          "child", "task"):
            nested = getattr(current, attribute, None)
            if isinstance(nested, BotTask):
                pending.append(nested)
    return task


@dataclass(frozen=True)
class PlanInputs:
    """What a plan reads from her memory and unlock state (her owner walls,
    dialogue offer and access set), taken on the loop so a planner thread
    (planner.py) never reads them from her live client."""
    walls: OwnerWalls
    offer: Any
    access: Any


def plan_inputs(client: AtrinikClient, graph: "WorldGraph") -> PlanInputs:
    """Her plan inputs now (PlanInputs). Call on the loop."""
    return PlanInputs(route_blocks.owner_walls(client),
                      dialogue_edges.dialogue_offer(client, graph),
                      unlocks.unlock_state(client, graph).access)


def _plan_inputs_of(client: Any) -> PlanInputs | None:
    """The inputs a planning client carries (planning_client), or None for
    her live client, whose plans read them themselves."""
    inputs = getattr(client, "plan_inputs", None)
    return inputs if isinstance(inputs, PlanInputs) else None


class PlanCancelled(Exception):
    """A background ranking was dropped: its remaining probes stop (not a
    ValueError, so no ranking takes it for an unreachable destination)."""


def _check_cancelled(client: Any) -> None:
    flag = getattr(client, "cancelled", None)
    if isinstance(flag, threading.Event) and flag.is_set():
        raise PlanCancelled("the ranking was dropped")


class ComponentRefresh:
    """One cache refresh shared by a ranking's probes (NavigateTask.refresh).

    A failed plan is retried once with the graph's components cleared, in
    case a stale one closed the route. A ranking plans many trips from the
    same place: after its first refresh the components are fresh, and
    clearing them again for every unreachable destination only rebuilt
    them each time (1.1 to 1.3 s a priest, about 22 s of the temple
    ranking's 23 s from world_7_79)."""

    def __init__(self) -> None:
        self.done = False


class NavigateTask(BotTask):
    """Follow the authored path one verified protocol move at a time."""

    STALL_RETRY_SECONDS = 0.65
    # A stall waits at least one movement acknowledgement at her speed
    # (movement_ack_timeout), up to this. Live: heavy depletion slowed Sera,
    # so every 0.65 s wait read as a stall; she learned the square she was
    # stepping onto as blocked, cancelled the step for another and zigzagged
    # in place until she starved, map after map.
    STALL_RETRY_MAX_SECONDS = 6.0
    # Learned stall blockers are guesses. Once they leave no route at all and
    # the character has not moved for this long, they are the trap.
    LEARNED_BLOCK_RESET_SECONDS = 5.0
    EARLY_ROUTE_HAZARD = re.compile(
        r"(?:rock thrower|battle thrower|skel(?:eton)? mage|"
        r"skeleton (?:novice )?archer|lost soul)", re.I)
    KNOWN_LETHAL_TRANSIT = frozenset({
        "/shattered_islands/world_4_69",
    })
    # A runtime edge exclusion (a pack-held exit, a seam with no crossing, a
    # click that found no path) is a detour hint, never a wall: it expires.
    # Live, a permanent one closed the Old Outpost's only stairs back to the
    # surface, and every replan to the Underground City failed for half an
    # hour while she fought grey golems.
    # Each repeat of the same exclusion within the trip doubles it, up to
    # the cap: a detour that keeps leading back to a held exit settles
    # instead of ping-ponging every 75 s.
    EDGE_EXCLUSION_SECONDS = 75.0
    EDGE_EXCLUSION_MAX_SECONDS = 300.0

    @property
    def _excluded_edges(self) -> set[tuple]:
        """The live exclusions: (source, destination, x, y) for an exit,
        (source, destination) for a tile seam. Expired ones are dropped."""
        now = time.monotonic()
        self._edge_exclusions = {
            key: until for key, until in self._edge_exclusions.items()
            if until > now}
        return set(self._edge_exclusions)

    def _exclude_edge(self, edge: MapEdge) -> None:
        """Exclude one exit (other stairs between the maps stay open) or a
        whole tile seam: EDGE_EXCLUSION_SECONDS, doubled on each repeat of
        the same key, up to EDGE_EXCLUSION_MAX_SECONDS."""
        key = ((edge.source, edge.destination, edge.x, edge.y)
               if edge.kind != "tile" else (edge.source, edge.destination))
        strikes = self._edge_exclusion_strikes.get(key, 0)
        self._edge_exclusion_strikes[key] = strikes + 1
        seconds = min(self.EDGE_EXCLUSION_MAX_SECONDS,
                      self.EDGE_EXCLUSION_SECONDS * 2 ** min(strikes, 8))
        self._edge_exclusions[key] = time.monotonic() + seconds

    def graph_route_open(self, client: AtrinikClient,
                         excluded: set[tuple] | None = None) -> bool:
        """Whether the static graph, behind ``excluded`` (and the lethal
        transit every tier avoids), still joins where she stands to the
        destination: no hazard tier, no strict avoidance, no owner block,
        only the map graph (WorldGraph.route_open, cached)."""
        return drain(self.graph_route_open_iter(client, excluded))

    def graph_route_open_iter(self, client: AtrinikClient,
                              excluded: set[tuple] | None = None):
        """graph_route_open as a generator (WorldGraph.route_open_iter)."""
        m = client.state.map
        return self.graph.route_open_iter(
            m.path, (m.world_x, m.world_y), self.destination,
            list(self._goal_points()),
            allow_locked=self._allow_locked(client),
            excluded=set(excluded or ()),
            avoided_maps=set(self.KNOWN_LETHAL_TRANSIT),
            offer=dialogue_edges.dialogue_offer(client, self.graph))

    # A cooperative graph_route_open hands the event loop back after this
    # many expanded states.
    ROUTE_OPEN_YIELD_STATES = 200

    async def graph_route_open_async(self, client: AtrinikClient,
                                     excluded: set[tuple] | None = None) -> bool:
        """graph_route_open from a coroutine: a cold whole-world miss
        hands the event loop back every ROUTE_OPEN_YIELD_STATES states, so
        the connection keeps being served while it runs."""
        search = self.graph_route_open_iter(client, excluded)
        steps = 0
        while True:
            try:
                next(search)
            except StopIteration as done:
                return done.value
            steps += 1
            if steps % self.ROUTE_OPEN_YIELD_STATES == 0:
                await asyncio.sleep(0)

    def _wait_for_exclusions(self, client: AtrinikClient,
                             error: Exception) -> bool:
        """A plan failed: wait instead of failing when only live edge
        exclusions close it (ROUTING 1.8).

        Exclusions are detour hints that expire; live, the Old Outpost's
        two stairs down were both excluded for a pack beside them and the
        errand failed on "no component route" with the pool one floor
        below. When the graph joins her to the destination without them
        but not with them, the route is dropped until the first expiry that
        reopens it (her defence holds meanwhile), then replanned. Only once a
        trip: an edge that fails again after it (an NPC that never answers,
        a click that never finds a path) fails the trip as before. Any other
        failure fails as before."""
        excluded = self._excluded_edges
        if (self._exclusion_waited or not excluded or
                self.graph_route_open(client, excluded) or
                not self.graph_route_open(client, set())):
            return False
        self._exclusion_waited = True
        now = time.monotonic()
        # The first expiry that reopens the route, not merely the soonest:
        # an unrelated short exclusion (a seam elsewhere) must not spend the
        # trip's one wait.
        exclusions = dict(self._edge_exclusions)
        until = max(exclusions.values())
        for expiry in sorted(set(exclusions.values())):
            if self.graph_route_open(client, {
                    key for key, ends in exclusions.items() if ends > expiry}):
                until = expiry
                break
        self.route = []
        self._issued_goal = self._issued_click = None
        self._last_progress = now
        self._exclusion_wait_until = until
        self._record(client, "navigation-exclusion-wait",
                     f"{self.destination}: {len(excluded)} excluded edge(s) "
                     f"close every route; replanning in {until - now:.0f}s "
                     f"({error})")
        return True

    @property
    def graph(self) -> WorldGraph:
        """Her world graph; on the planner worker, its planner view
        (WorldGraph.planner_view), so a plan made there never touches a
        cache the loop uses."""
        graph = self._graph
        generation = planner.worker_generation()
        return graph if generation is None else graph.planner_view(generation)

    @graph.setter
    def graph(self, graph: WorldGraph) -> None:
        self._graph = graph

    def __init__(self, graph: WorldGraph, destination: str,
                 destination_xy: tuple[int, int] | None = None,
                 tolerance: int = 0,
                 allow_locked: bool | None = None):
        super().__init__(f"navigate:{destination}")
        self.graph = graph
        self.destination = destination
        self.destination_xy = destination_xy
        self.tolerance = tolerance
        self._allow_locked_override = allow_locked
        self.route: list[MapEdge] = []
        self._last_action = 0.0
        self._approach: list[tuple[int, int]] = []
        self._approach_index = 0
        self._last_position: tuple[str, int, int] | None = None
        self._last_progress = 0.0
        self._issued_goal: tuple[str, int, int] | None = None
        self._issued_click: tuple[str, int, int] | None = None
        # Runtime edge exclusions and when each expires (time.monotonic());
        # see _exclude_edge.
        self._edge_exclusions: dict[tuple, float] = {}
        # How often each key was excluded on this trip (see _exclude_edge).
        self._edge_exclusion_strikes: dict[tuple, int] = {}
        # Until when a plan that only live exclusions closed waits for the
        # first expiry that reopens it (_wait_for_exclusions); 0.0 when none.
        self._exclusion_wait_until = 0.0
        self._exclusion_waited = False
        self._runtime_blocked: set[tuple[int, int]] = set()
        # When the current local route first disappeared (0.0 while one exists).
        # Separate from _last_progress, which approach-candidate rotation resets.
        self._no_route_since = 0.0
        # Dynamic avoidance halos around currently visible threat packs.
        # Unlike learned collision squares, these must disappear as soon as
        # the pack moves or is cleared.
        self._threat_blocked: set[tuple[int, int]] = set()
        self._temporary_blocked: set[tuple[int, int]] = set()
        self._route_threat_maps: set[str] = set()
        self._threat_fallback = False
        self.allow_ranged_hazard_fallback = False
        # The last _plan_avoiding took a fallback tier that enters hazard
        # maps (navigation-threat-fallback): not the tunnel retry, not a
        # learned-cost plan.
        self.hazard_fallback_taken = False
        # Avoid every known hazard, the maps that killed her included, with
        # no fallback tier: no safe route means no trip (the task fails and
        # is retried later), never a dangerous one.
        self.strict_safety = False
        self._failed_tile_crossings: set[tuple[str, str, int, int]] = set()
        # (map, x, y) seam departures that rejected the step onto them and
        # when each rejection expires (time.monotonic()), with how often each
        # was rejected on this trip; see _reject_departure.
        self._rejected_departures: dict[tuple[str, int, int], float] = {}
        self._departure_rejection_strikes: dict[
            tuple[str, int, int], int] = {}
        # The step onto a seam departure being timed: ((map, start,
        # departure), first seen, the send then seen, when the timer
        # started or None); see _retry_stalled_tile_crossing.
        self._departure_attempt: tuple[
            tuple, float, float, float | None] | None = None
        # The kind of essential trip this is (ESSENTIAL_TRIPS), or "".
        self.essential = ""
        # The owner walls of the plan under way (_plan), its client, and the
        # blocks its route crosses (_route_points).
        self._walls: OwnerWalls = NO_WALLS
        # The plan's dialogue voyages and closed quest gates, or None with
        # the owner's dialogue_edges flag off (dialogue_edges.py).
        self._offer: dialogue_edges.DialogueOffer | None = None
        # The voyage under way: its edge, the voyage and its dialogue task.
        self._voyage: tuple[MapEdge, dialogue_edges.Voyage, BotTask] | None = None
        # The walk beside a voyage's NPC: ((edge, map, square), since when
        # she has stood on that square, the last tick), for
        # VOYAGE_APPROACH_STALL_SECONDS.
        self._voyage_approach: tuple[tuple, float, float] | None = None
        self._plan_client: AtrinikClient | None = None
        self._crossed: tuple[str, ...] = ()
        # What kind of trip this is for its route budget and the shadow
        # report (route_costs: FARM, or "" for an errand); an essential
        # trip reports its essential kind.
        self.trip = ""
        # The learned-cost plan (ROUTING P6, the owner's learned_route_costs
        # flag): the last one, and the subjects a budget or strict-plan
        # override crosses with CARE_BOUNDED care.
        self.learned_plan: route_costs.LearnedPlan | None = None
        self._bounded: tuple[str, ...] = ()
        # A care crossing (require_care), and while she walks to her
        # savebed to wait for it, the trip's own destination fields.
        self.care: CareCrossing | None = None
        self._care_staging: tuple | None = None
        self._care_staging_retry_at = 0.0
        # A ranking's probes share one component refresh.
        self.refresh: ComponentRefresh | None = None

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self._last_position = (
            client.state.map.path, client.state.map.world_x,
            client.state.map.world_y)
        if self.tolerance and not self._approach:
            self._approach = self._approach_candidates()
        self.route = self._plan(client)
        self._last_progress = time.monotonic()

    def complete(self) -> None:
        self._temporary_blocked.clear()
        super().complete()

    def _allow_locked(self, client: AtrinikClient):
        """Her access set: the unlock state's, unless the task overrides it."""
        if self._allow_locked_override is not None:
            return self._allow_locked_override
        inputs = _plan_inputs_of(client)
        if inputs is not None:
            return inputs.access
        return unlocks.unlock_state(client, self.graph).access

    def _goal_points(self) -> list[tuple[int, int]]:
        if self.destination_xy is None:
            return []
        if self.tolerance and self._approach:
            return self._approach
        return [self.destination_xy]

    def _known_ranged_hazard_maps(self, client: AtrinikClient, *,
                                  any_level: bool = False) -> set[str]:
        """Avoid authored ranged/caster transit maps while Sera is fragile."""
        stats = getattr(client.state, "stats", {})
        if not any_level and int(stats.get("level", 0) or 0) >= 18:
            return set()
        hazards = set()
        for path, identities in self.graph.map_monster_levels.items():
            node = self.graph.nodes.get(path)
            peaceful = node.peaceful_identities if node is not None else set()
            for identity in identities:
                normalized = identity.casefold().strip().replace("_", " ")
                normalized = re.sub(r"\.\d+$", "", normalized)
                if (self.EARLY_ROUTE_HAZARD.search(normalized) and
                        normalized not in peaceful):
                    hazards.add(path)
                    break
        return hazards

    def _known_overlevel_hazard_maps(self, client: AtrinikClient) -> set[str]:
        """Return maps with aggressive spawns materially above the player.

        The relative margin grows with the character so ordinary adjacent
        progression remains routable while a fragile early character does
        not take a short path through crocodiles, wolves, or similar threats.
        """
        stats = getattr(client.state, "stats", {})
        level = max(1, int(stats.get("level", 0) or 0))
        margin = max(3, (level + 4) // 5)
        hazards = set()
        for path, identities in self.graph.map_monster_levels.items():
            node = self.graph.nodes.get(path)
            peaceful = node.peaceful_identities if node is not None else set()
            if any(
                    monster_level >= level + margin and
                    identity not in peaceful
                    for identity, monster_level in identities.items()):
                hazards.add(path)
        return hazards

    SURFACE_MAP = re.compile(r"/shattered_islands/world_-?\d+_-?\d+")
    TILED_LEVEL_MAP = re.compile(r"/shattered_islands/world_-?\d+_-?\d+_-?\d+")

    def _known_pack_hazard_maps(self, source: str = "") -> set[str]:
        """Return open-world maps with three-plus joining aggressors.

        Interior maps often contain several separated rooms and may require a
        brief exterior hop to reach another component. Treating every pair on
        every authored map as an impassable country-sized hazard made ordinary
        buildings unroutable. Three connected aggressors on an open surface is
        the live-validated pursuit failure this coarse map-level gate models;
        smaller or room-separated contacts remain the live combat policy's
        responsibility.

        Tiled levels above and below the world grid (``world_X_Y_-2``) are
        rated too: leaving them out made a surface-pack detour look safe
        through the tunnels below Fort Sether, whose spider and bat packs held
        Sera for over ten minutes. Those levels split into components joined
        only through each other, so ``_plan`` retries without them when no
        route avoids them. Negative grid coordinates (``world_-2_57``) are
        part of the grid as well.
        """
        estimator = getattr(self.graph, "transit_max_aggro_pack", None)
        if estimator is None:
            return set()
        return {
            path for path in self.graph.named_spawns
            if (self.SURFACE_MAP.fullmatch(path) or
                self.TILED_LEVEL_MAP.fullmatch(path)) and
            estimator(path) >= 3
        }

    def _learned_route_hazards(self, client: AtrinikClient) -> set[str]:
        """Maps where a recent route death took combat damage."""
        now = time.time()
        hazards = getattr(client.state, "route_hazards", None) or {}
        return {path for path, until in hazards.items() if until > now}

    def _route_points(self, *args, unwalled_retry: bool = True,
                      **kwargs) -> list[MapEdge]:
        """One tier's component route, behind this plan's owner walls.

        A block never relaxes a hazard tier: when the tier finds no route
        behind the walls, the same tier is tried without them
        (_unwalled_route), unless the caller still has soft retries that
        relax no hazard (``unwalled_retry=False``) and tries without walls
        itself after them. Only when the tier fails without the walls too
        does its ValueError reach the tier, which then relaxes as it always
        did.
        """
        try:
            route = self.graph.route_points(
                *args, walls=self._walls, offer=self._offer, **kwargs)
        except ValueError as blocked:
            if not self._walls or not unwalled_retry:
                raise
            return self._unwalled_route(blocked, args, [kwargs])
        self._crossed = ()
        return route

    def _unwalled_route(self, blocked: ValueError, args: tuple,
                        tiers: list[dict]) -> list[MapEdge]:
        """The first of ``tiers`` routed without the owner walls, or
        ``blocked``. When one routes, the block is the only obstacle: an
        essential trip takes it and crosses with care (_plan), any other
        trip is refused with OwnerBlockRefusal, which no tier catches."""
        walls = self._walls
        for kwargs in tiers:
            try:
                route = self.graph.route_points(*args, offer=self._offer, **kwargs)
            except ValueError:
                continue
            crossed = walls.crossings(route, self.destination)
            if not crossed:
                continue
            if self._care_staging is not None or not self.is_essential(self._plan_client):
                raise OwnerBlockRefusal(
                    f"owner block: every route to {self.destination} crosses "
                    f"{', '.join(crossed)} ({blocked})") from None
            self._crossed = crossed
            return route
        raise blocked

    def is_essential(self, client: AtrinikClient) -> bool:
        """Whether this trip is essential now (ROUTING 4.5): a cure, the
        depletion service, the savebed move or a trip that funds one of
        them, and a food trip only while she is hungry or starving."""
        if self.essential not in ESSENTIAL_TRIPS:
            return False
        return self.essential != "food" or hungry(client) or starving_without_food(client)

    def _plan(self, client: AtrinikClient, *,
              origin: tuple[str, tuple[int, int]] | None = None) -> list[MapEdge]:
        """The route from where she stands (or ``origin``), behind the
        owner's blocks, or ValueError.

        Owner blocks (route_blocks) are transit walls for every trip, tier
        by tier (_route_points): a block never makes the planner relax a
        hazard. When a tier's only route crosses one, an essential trip
        (is_essential) takes it and crosses with care (require_care); any
        other trip fails with the block as its reason. A walk to a safe
        place for that care never crosses one.
        """
        _check_cancelled(client)
        inputs = _plan_inputs_of(client)
        if inputs is not None:
            self._walls, self._offer = inputs.walls, inputs.offer
        else:
            self._walls = route_blocks.owner_walls(client)
            self._offer = dialogue_edges.dialogue_offer(client, self.graph)
        self._plan_client, self._crossed, self._bounded = client, (), ()
        learned = learned_route_costs(client)
        try:
            route = self._plan_tiers(client, origin=origin)
        except OwnerBlockRefusal as refusal:
            if not learned:
                self._shadow(client, origin, None, str(refusal))
            raise ValueError(str(refusal)) from None
        except ValueError as exc:
            if not learned:
                self._shadow(client, origin, None, str(exc))
            raise
        finally:
            self._plan_client = None
        if not learned:
            self._shadow(client, origin, route, "")
        if self._crossed or self._bounded:
            # Both may hold at once (a blocked and over-budget route): the
            # crossing waits as an owner block and keeps the bounded
            # subjects too.
            if self._crossed:
                self.require_care(CARE_OWNER_BLOCK, self._crossed, client=client)
            if self._bounded:
                self.require_care(CARE_BOUNDED, self._bounded, client=client)
            return route
        if self.care is not None and self._care_staging is None:
            # A block that expired or was removed, or one already behind
            # her: nothing left to cross. Hold-fire ends with it.
            if self.care.cleared:
                self.care.finished = True
            elif self.care.mode in (CARE_OWNER_BLOCK, CARE_BOUNDED):
                # Nothing left that needs care (a block lifted, or, with
                # learned costs on, danger decayed under the budget).
                self.care = None
        return route

    def require_care(self, mode: str, subjects: Sequence[str], *,
                     clock: Callable[[], float] = time.monotonic,
                     client: AtrinikClient | None = None) -> "CareCrossing":
        """Cross only with care (ROUTING 4.5): before the first step she
        waits, at a safe place, for full HP and SP and a ready recall when
        she owns one, then holds fire in transit. ``mode`` is
        CARE_OWNER_BLOCK (wait without limit, alerting after
        CARE_WAIT_SECONDS) or CARE_BOUNDED (budget and strict-plan
        overrides: after CARE_WAIT_SECONDS she goes anyway). A food trip
        waits only for the recall.

        An owner-block wait of a running trip for the same kind of trip and
        the same subjects as one another trip left within
        CARE_WAIT_RESUME_SECONDS (autoplay's handover rebuilds trips) keeps
        its start and its alert, so the alert is neither postponed nor
        repeated (``client`` holds that record). A planning probe (a trip
        not started, such as the temple ranking's) neither reads nor writes
        it."""
        care = self.care
        if care is None or care.finished:
            since, alerted = clock(), False
            key = (self.essential, *sorted(subjects))
            if (mode == CARE_OWNER_BLOCK and client is not None and
                    self.status == TaskStatus.RUNNING):
                waits = _care_waits(client)
                previous = waits.get(key)
                if previous is not None and since - previous[1] <= CARE_WAIT_RESUME_SECONDS:
                    since, alerted = previous[0], previous[2]
                waits[key] = (since, clock(), alerted)
            care = self.care = CareCrossing(mode, tuple(subjects), since, clock=clock,
                                            key=key, alerted=alerted)
        else:
            care.subjects = tuple(dict.fromkeys((*care.subjects, *subjects)))
            if mode == CARE_OWNER_BLOCK:
                # An owner block never times out, whatever else asked.
                care.mode = CARE_OWNER_BLOCK
        if mode == CARE_BOUNDED:
            care.bounded = tuple(dict.fromkeys((*care.bounded, *subjects)))
        return care

    def _care_ahead(self, map_path: str) -> bool:
        """Whether a blocked subject of the care crossing is where she stands
        or still on her route."""
        care = self.care
        if care is None:
            return False
        subjects = set(care.subjects)
        if route_blocks.map_subject(map_path) in subjects:
            return True
        # A blocked destination is no crossing (a block is a transit wall);
        # a dangerous one under a budget or strict override is, also once an
        # owner block has merged into that crossing.
        bounded = set(care.bounded)
        return any(
            route_blocks.edge_subject(edge) in subjects or
            ((edge.destination != self.destination or
              route_blocks.map_subject(edge.destination) in bounded) and
             route_blocks.map_subject(edge.destination) in subjects)
            for edge in self.route)

    def care_holds_fire(self, map_path: str) -> bool:
        """Hold fire for a care crossing on ``map_path``: while waiting, and
        once crossing until no block it crosses is left, here or ahead. Then
        the crossing is finished."""
        care = self.care
        if care is None or care.finished:
            return False
        # An empty route is not yet replanned: nothing is known to be behind her.
        if (care.cleared and self._care_staging is None and
                (self.route or map_path == self.destination) and
                not self._care_ahead(map_path)):
            care.finished = True
            return False
        return True

    def care_status(self, now: float | None = None) -> dict | None:
        """The care crossing for the dashboard, or None."""
        care = self.care
        if care is None or care.finished:
            return None
        now = care.clock() if now is None else now
        return {"mode": care.mode, "subjects": list(care.subjects),
                "trip": self.essential, "destination": self.destination,
                "cleared": care.cleared, "starving": care.starving, "food": care.food,
                "waiting_seconds": round(max(0.0, (care.cleared_at or now) - care.since), 1),
                "alerted": care.alerted, "missing": list(care.missing),
                "recall_ready": care.recall_ready,
                "staging": self._care_staging is not None}

    def _hostile_map(self, path: str) -> bool:
        return any(not spawn.peaceful for spawn in self.graph.named_spawns.get(path, ()))

    def _care_place(self, client: AtrinikClient) -> str:
        """Why where she stands is no place to wait for care, or "".

        Safe is: no creature that is not authored peaceful within
        CARE_SAFE_RADIUS, and either her savebed or a map without hostile
        spawns (a town, a building). Combat text is not read: damage over
        time ("poison hit you", a disease symptom) reads like a hit, so a
        poisoned or diseased character would never be safe. A fight holds
        the trip in its defence, which skips the care checks, and an attack
        she cannot see holds it too (the unseen-attacker retreat), so that
        gap restarts the plateau (_care_missing).
        """
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        node = self.graph.nodes.get(m.path)
        peaceful = set(getattr(self.graph, "peaceful_monster_identities", set()) or ())
        if node is not None:
            peaceful |= node.peaceful_identities
        for x, y, obj in m.targets(friendly=False):
            if max(abs(x - cx), abs(y - cy)) > CARE_SAFE_RADIUS:
                continue
            identities = (WorldGraph._semantic_name(obj.name),
                          WorldGraph._semantic_name(map_object_visual_name(client, obj)))
            if not any(identity and identity in peaceful for identity in identities):
                return "a hostile in view"
        if self._at_savebed(client):
            return ""
        if self._hostile_map(m.path):
            return "a map with hostile spawns"
        return ""

    def _savebed(self, client: AtrinikClient) -> tuple[str, tuple[int, int]] | None:
        path = getattr(client.state, "savebed_map", "") or ""
        xy = getattr(client.state, "savebed_xy", None)
        if not path or xy is None or path not in self.graph.nodes:
            return None
        return path, (int(xy[0]), int(xy[1]))

    def _at_savebed(self, client: AtrinikClient) -> bool:
        home = self._savebed(client)
        m = client.state.map
        return home is not None and m.path == home[0] and max(
            abs(m.world_x - home[1][0]), abs(m.world_y - home[1][1])) <= CARE_HOME_TOLERANCE

    @staticmethod
    def _vital_full(care: "CareCrossing", key: str, value: int, maximum: int,
                    now: float, resting: bool) -> bool:
        """At its maximum, or, resting at a safe place, stable for
        CARE_PLATEAU_SECONDS: not risen above its peak, nor fallen more than
        CARE_PLATEAU_BAND of the maximum below it. A disease or poison that
        keeps it below the maximum must not hold her forever (the plateau
        counts as full), and damage from an attacker she cannot see must
        not pass for one. Not resting (a hostile in view, on the walk home)
        starts it afresh, and so does a gap between checks (_care_missing)."""
        if maximum > 0 and value >= maximum:
            return True
        best = care.peaks.get(key)
        if (not resting or best is None or value > best[0] or
                best[0] - value > CARE_PLATEAU_BAND * maximum):
            care.peaks[key] = (value, now)
            return False
        if now - best[1] >= CARE_PLATEAU_SECONDS:
            care.plateaued = True
            return True
        return False

    def _care_missing(self, client: AtrinikClient, lenient: bool) -> list[str]:
        """What the care rule still waits for. A food trip (``lenient``)
        waits for the recall only."""
        care = self.care
        assert care is not None
        missing = []
        place = "" if lenient else self._care_place(client)
        if not lenient:
            stats = client.state.stats
            now = care.clock()
            hp, maxhp = int(stats.get("hp", 0) or 0), int(stats.get("maxhp", 0) or 0)
            sp, maxsp = int(stats.get("sp", 0) or 0), int(stats.get("maxsp", 0) or 0)
            resting = not place and self._care_staging is None
            if care.checked_at and now - care.checked_at > CARE_PLATEAU_GAP_SECONDS:
                care.peaks.clear()
            care.checked_at = now
            if maxhp <= 0 or not self._vital_full(care, "hp", hp, maxhp, now, resting):
                missing.append("full HP")
            if maxsp > 0 and not self._vital_full(care, "sp", sp, maxsp, now, resting):
                missing.append("full SP")
        if not care_recall_ready(client, starving=care.starving):
            missing.append("a ready recall")
        if place:
            missing.append(f"a safe place ({place})")
        return missing

    def _record(self, client: AtrinikClient, action: str, detail: str) -> None:
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(action, detail)

    def _begin_care_staging(self, client: AtrinikClient) -> bool:
        """Walk to her savebed to wait there; False when she cannot."""
        home = self._savebed(client)
        now = time.monotonic()
        if (home is None or self._at_savebed(client) or
                now < self._care_staging_retry_at or
                (home[0] == self.destination and self.destination_xy is not None and
                 max(abs(home[1][0] - self.destination_xy[0]),
                     abs(home[1][1] - self.destination_xy[1])) <= CARE_HOME_TOLERANCE)):
            return False
        saved = (self.destination, self.destination_xy, self.tolerance,
                 self._approach, self._approach_index, self.route)
        self._care_staging = saved
        self.destination, self.destination_xy = home
        self.tolerance, self._approach, self._approach_index = CARE_HOME_TOLERANCE, [], 0
        try:
            self.route = self._plan(client)
        except ValueError as exc:
            self._end_care_staging()
            self._care_staging_retry_at = now + CARE_STAGING_RETRY_SECONDS
            log.info("care wait: no block-free walk to her savebed: %s", exc)
            return False
        self._issued_goal = self._issued_click = None
        self._last_progress = now
        if self.care is not None:
            self.care.holding = False
        self._record(client, "essential-care-staging",
                     f"to her savebed {home[0]} to wait for care before crossing "
                     f"{', '.join(self.care.subjects) if self.care else ''}")
        return True

    def _end_care_staging(self) -> None:
        saved, self._care_staging = self._care_staging, None
        if saved is None:
            return
        (self.destination, self.destination_xy, self.tolerance,
         self._approach, self._approach_index, _) = saved
        self.route = []
        self._issued_goal = self._issued_click = None
        self._last_progress = time.monotonic()
        if self.care is not None:
            # The wait at her savebed starts with its own clear.
            self.care.holding = False

    async def _care_hold(self, client: AtrinikClient) -> bool:
        """The care rule before a care crossing: True while she must not
        move on this tick (waiting, or the trip just yielded).

        She waits only at a safe place (_care_place). On a map with hostile
        spawns she first walks to her savebed, by a block-free route, and
        waits where she stands only when there is none. Everything that
        keeps her alive runs meanwhile, outside this task: healing, eating,
        the low-health retreat and defence (NavigateThenTask), the
        emergency recall (the recovery phase), Stop and the operator hold.
        A trip other than a food trip yields to hunger rather than wait
        while she starves: it fails, and the food trip goes first. A food
        trip waits only for a ready recall: waiting for full HP and SP
        burns food (player_do_some_living), so it would starve her.
        """
        care = self.care
        if care is None or care.cleared:
            return False
        now = care.clock()
        if care.key in _care_waits(client):
            _care_waits(client)[care.key] = (care.since, now, care.alerted)
        care.food = self.essential == "food"
        care.starving = care.food and starving_without_food(client)
        if (care.mode == CARE_OWNER_BLOCK and
                not (set(care.subjects) - set(care.bounded)) &
                route_blocks.active_subjects(client)):
            # Every block it waited for expired or was removed meanwhile:
            # plan again, behind whatever walls are left.
            _care_waits(client).pop(care.key, None)
            self._end_care_staging()
            self.care, self.route = None, []
            self._issued_goal = self._issued_click = None
            return False
        if not care.food and hungry(client):
            self._record(client, "essential-care-yield",
                         f"{self.essential} trip to {self.destination}: hungry, the "
                         f"food trip goes first ({', '.join(care.subjects)})")
            self._end_care_staging()
            await client.clear_actions()
            self.fail(f"care wait before crossing {', '.join(care.subjects)} "
                      "yielded to hunger")
            return True
        if self._care_staging is not None:
            if self._at_savebed(client):
                self._end_care_staging()
            else:
                # Walking to her savebed: the ordinary movement runs.
                care.missing = tuple(self._care_missing(client, care.food))
                return False
        missing = self._care_missing(client, care.food)
        care.missing = tuple(missing)
        timeout = (care.mode == CARE_BOUNDED and now - care.since >= CARE_WAIT_SECONDS)
        if not missing or timeout:
            self._clear_care(client, timeout=timeout and bool(missing))
            return False
        if (care.mode == CARE_OWNER_BLOCK and not care.alerted and
                now - care.since >= CARE_WAIT_SECONDS):
            care.alerted = True
            if care.key in _care_waits(client):
                _care_waits(client)[care.key] = (care.since, now, True)
            self._record(client, "essential-care-alert",
                         f"{self.essential} trip to {self.destination} waited "
                         f"{now - care.since:.0f} s to cross {', '.join(care.subjects)}; "
                         f"still waiting for {', '.join(missing)}")
        # The walk home is decided by the map alone: a hostile in view or a
        # hit on a respawning map must not keep her waiting there.
        if (not care.food and not self._at_savebed(client) and
                self._hostile_map(client.state.map.path) and
                self._begin_care_staging(client)):
            return False
        if not care.holding:
            care.holding = True
            await client.clear_actions()
            self._issued_goal = self._issued_click = None
            self._record(client, "essential-care-wait",
                         f"{self.essential} trip to {self.destination} waits to cross "
                         f"{', '.join(care.subjects)} for {', '.join(missing)}")
        # Waiting is not a stall.
        self._last_progress = time.monotonic()
        return True

    def _clear_care(self, client: AtrinikClient, *, timeout: bool) -> None:
        care = self.care
        assert care is not None
        care.cleared, care.cleared_at = True, care.clock()
        _care_waits(client).pop(care.key, None)
        subjects = ", ".join(care.subjects)
        care.recall_ready = bool(FarmTask.emergency_recall_candidates(client))
        recall = "a ready recall" if care.recall_ready else "no working recall"
        how = (f"starving: {'a ready recall and ' if care.recall_ready else ''}"
               "hold-fire only" if care.starving else
               f"hungry: {'a ready recall and ' if care.recall_ready else ''}"
               "hold-fire only" if care.food else
               "waited too long; going anyway, holding fire" if timeout else
               f"HP and SP full or no longer rising, {recall}, holding fire"
               if care.plateaued else
               f"full HP and SP, {recall}, holding fire")
        self._record(client, "essential-route-override",
                     f"{self.essential} trip to {self.destination} crosses {subjects} "
                     f"({care.mode}): {how}")
        if care.starving:
            self._record(client, "essential-care-starving",
                         f"food trip to {self.destination} crosses {subjects} starving")
        elif care.food:
            self._record(client, "essential-care-food",
                         f"food trip to {self.destination} crosses {subjects} hungry")
        if timeout:
            self._record(client, "essential-care-timeout",
                         f"{self.essential} trip to {self.destination} after "
                         f"{CARE_WAIT_SECONDS:.0f} s without {', '.join(care.missing)}")
        log.warning("essential %s trip to %s crosses %s with care (%s)",
                    self.essential, self.destination, subjects, how)
        self._issued_goal = self._issued_click = None
        self._last_progress = time.monotonic()

    def _refresh_components(self) -> bool:
        """Clear the graph's components before retrying a failed plan; False
        when this plan's ranking already did (ComponentRefresh): they are
        fresh, so the failure is not retried. A background ranking's
        planning client carries one for all its probes."""
        refresh = self.refresh
        if refresh is None:
            refresh = getattr(self._plan_client, "refresh", None)
        if isinstance(refresh, ComponentRefresh):
            if refresh.done:
                return False
            refresh.done = True
        _clear_components(self.graph)
        return True

    def _plan_tiers(self, client: AtrinikClient, *,
                    origin: tuple[str, tuple[int, int]] | None = None) -> list[MapEdge]:
        # Learned hazards are a cost, never a wall: one search finds the route
        # that enters as few of them as it can. They never make a trip
        # unroutable or change which other hazards are relaxed, and leaving
        # the map she stands on is free while re-entering it is not.
        # ``origin`` plans from another (map, tile) than where she stands.
        if learned_route_costs(client):
            return self._plan_learned(client, origin)
        learned = self._learned_route_hazards(client)
        if self.strict_safety:
            return self._plan_strict(client, learned, origin)
        route = self._plan_avoiding(client, learned, origin)
        if any(edge.destination in learned for edge in route):
            # Entering a map that killed her, the destination included, is
            # never "threat-free" when choosing between optional destinations.
            self._threat_fallback = True
        return route

    @property
    def over_budget(self) -> bool:
        """The last learned plan's route was over its budget, or needed a
        strict override (a planning probe's ranking puts it after safe
        ones, as it does a route across an owner block)."""
        plan = self.learned_plan
        return plan is not None and plan.outcome in (route_costs.DECLINED,
                                                     route_costs.OVERRIDE)

    def trip_kind(self, client: AtrinikClient | None) -> str:
        """The trip's kind for the shadow report and metrics: "home" for
        the walk to her savebed to wait for care, its essential kind (a
        food trip is food-kind whether or not she is hungry yet: whether it
        is essential now is is_essential's), FARM, STRICT for a strict
        trip, or ERRAND (quest steps included: they share its budget)."""
        if self._care_staging is not None:
            return "home"
        if self.essential:
            return self.essential
        if self.trip:
            return self.trip
        return route_costs.STRICT if self.strict_safety else route_costs.ERRAND

    def _trip_snapshot(self, client: AtrinikClient,
                       origin: tuple[str, tuple[int, int]] | None = None,
                       ) -> route_costs.TripSnapshot:
        """This trip's planning inputs for the learned-cost planner
        (learned_route): what it plans from and to, behind which walls,
        and the costs it prices edges with."""
        source, source_xy = origin or (
            client.state.map.path,
            (client.state.map.world_x, client.state.map.world_y))
        stats = getattr(client.state, "stats", {}) or {}
        level = max(1, int(stats.get("level", 0) or 0))
        model = route_costs.learned_model(client)
        view = model.view(time.time(), route_costs.current_kit(client))
        priors = route_costs.content_priors(self.graph, level,
                                            lethal=self.KNOWN_LETHAL_TRANSIT)
        staging = self._care_staging is not None
        essential = not staging and self.is_essential(client)
        trip = self.trip_kind(client)
        context = route_costs.CostContext(
            self.graph, view, priors, pace=route_costs.seconds_per_tile(stats),
            lam=danger_weight(client, optional=not (essential or staging or
                                                    trip == route_costs.FARM)))
        strict_avoided = frozenset()
        if self.strict_safety:
            strict_avoided = frozenset(
                self._known_ranged_hazard_maps(client, any_level=True) |
                self._known_overlevel_hazard_maps(client) |
                self._known_pack_hazard_maps(source) |
                set(self.KNOWN_LETHAL_TRANSIT) |
                {path for path, seconds in view.maps.items()
                 if seconds >= route_costs.STRICT_LEARNED_AVOID_SECONDS})
        return route_costs.TripSnapshot(
            self.graph, source, source_xy, self.destination, list(self._goal_points()),
            self._allow_locked(client), set(self._excluded_edges), self._walls, self._offer,
            self.strict_safety, essential, trip, strict_avoided, context, staging=staging)

    def _plan_learned(self, client: AtrinikClient,
                      origin: tuple[str, tuple[int, int]] | None = None) -> list[MapEdge]:
        """The live plan with learned costs (the owner's learned_route_costs
        flag; ROUTING 4.5, P7 turns it on): learned_route, then its side
        effects. An optional trip over its budget fails (it is retried
        later); an essential one over its budget, or whose strict plan
        failed, takes the cheapest route with CARE_BOUNDED care (_plan);
        owner blocks keep P3's rules."""
        snapshot = self._trip_snapshot(client, origin)
        plan = drain(learned_route(snapshot))
        if plan.outcome == route_costs.NO_ROUTE and self._refresh_components():
            # Components are derived data; retry once with a fresh cache.
            plan = drain(learned_route(snapshot))
        self.learned_plan = plan
        # A planning probe (a trip not started: the temple, food and recall
        # vendor rankings, the town reach check) has no side effects: no
        # decision, no metric, no care. It ranks by the cheapest route even
        # over its budget; the trip it ranks for decides when it runs.
        live = self.status == TaskStatus.RUNNING
        if live:
            route_shadow.note_live_plan(client, plan)
        if plan.outcome == route_costs.BLOCKED:
            raise OwnerBlockRefusal(plan.reason)
        # The threat fields describe the route returned, a probe's
        # over-budget candidate included, so its ranking sees the hazards.
        hazards = {path for path, seconds in snapshot.context.priors.items()
                   if seconds >= route_costs.PRIOR_OVERLEVEL_SECONDS}
        taken = plan.route or plan.candidate or []
        self._route_threat_maps = {edge.destination for edge in taken
                                   if edge.destination in hazards}
        self._threat_fallback = bool(self._route_threat_maps) or any(
            snapshot.context.view.map_danger(edge.destination) >=
            route_costs.STRICT_LEARNED_AVOID_SECONDS for edge in taken)
        if plan.route is None:
            if plan.outcome == route_costs.DECLINED:
                if not live and plan.candidate:
                    return plan.candidate
                if live:
                    self._record(client, "route-declined", route_shadow.plan_detail(
                        plan, self.destination))
            raise ValueError(plan.reason)
        self._crossed = plan.crossed
        if plan.care == CARE_BOUNDED and live:
            self._bounded = bounded_care_subjects(plan, snapshot)
            self._record(client, "route-budget-override" if not plan.strict_relaxed
                         else "route-strict-override",
                         route_shadow.plan_detail(plan, self.destination))
        return plan.route

    def _shadow(self, client: AtrinikClient,
                origin: tuple[str, tuple[int, int]] | None,
                route: list[MapEdge] | None, error: str) -> None:
        """With the learned_route_costs flag off: hand the live plan to the
        shadow planner (route_shadow.py), which plans the same trip with
        learned costs on later ticks and logs where they differ. Only a
        trip under way (not a planning probe), never its walk home to wait
        for care. Never raises."""
        if self.status != TaskStatus.RUNNING or self._care_staging is not None:
            return
        route_shadow.request_trip(client, self, origin, route, error)

    def _plan_strict(self, client: AtrinikClient, learned: set[str],
                     origin: tuple[str, tuple[int, int]] | None = None,
                     ) -> list[MapEdge]:
        """Route around every known hazard, or raise ValueError.

        Ranged maps count at any level here: a depleted character is fragile
        whatever her level. Avoidance applies to entering a map, so leaving a
        hazardous map she stands on stays possible while re-entering it as
        transit does not. A hazardous destination is refused outright (the
        graph would otherwise enter it): the trip waits, for example until a
        map that killed her stops counting.
        """
        source, source_xy = origin or (
            client.state.map.path,
            (client.state.map.world_x, client.state.map.world_y))
        avoided = (
            self._known_ranged_hazard_maps(client, any_level=True) |
            self._known_overlevel_hazard_maps(client) |
            self._known_pack_hazard_maps(source) |
            set(self.KNOWN_LETHAL_TRANSIT) | learned
        )
        if self.destination != source and self.destination in avoided:
            raise ValueError(
                f"{self.destination} is itself a known hazard")
        args = (source, source_xy, self.destination, self._goal_points())
        kwargs = {
            "allow_locked": self._allow_locked(client),
            "excluded": self._excluded_edges,
            "avoided_maps": avoided,
        }
        try:
            route = self._route_points(*args, unwalled_retry=False, **kwargs)
        except ValueError as first:
            # Components are derived data; retry once with a fresh cache.
            try:
                try:
                    if not self._refresh_components():
                        raise first
                    route = self._route_points(*args, unwalled_retry=False, **kwargs)
                except ValueError as walled:
                    if not self._walls:
                        raise
                    route = self._unwalled_route(walled, args, [kwargs])
            except ValueError as exc:
                raise ValueError(
                    f"no route to {self.destination} avoiding every known "
                    f"hazard: {exc}") from exc
        self._threat_fallback = False
        self._route_threat_maps = set()
        return route

    def _plan_avoiding(self, client: AtrinikClient, penalized: set[str],
                       origin: tuple[str, tuple[int, int]] | None = None,
                       ) -> list[MapEdge]:
        source, source_xy = origin or (
            client.state.map.path,
            (client.state.map.world_x, client.state.map.world_y))
        args = (source, source_xy, self.destination, self._goal_points())
        ranged_hazards = self._known_ranged_hazard_maps(client)
        overlevel_hazards = self._known_overlevel_hazard_maps(client)
        pack_hazards = self._known_pack_hazard_maps(source)
        protected_hazards = (
            ranged_hazards | pack_hazards | set(self.KNOWN_LETHAL_TRANSIT))
        kwargs = {
            "allow_locked": self._allow_locked(client),
            "excluded": self._excluded_edges,
            "avoided_maps": protected_hazards | overlevel_hazards,
            "penalized_maps": set(penalized),
        }
        previous_fallback = self._threat_fallback
        self._threat_fallback = False
        self.hazard_fallback_taken = False

        def relaxed(avoided: set[str]) -> dict:
            # A hazard a fallback tier stops avoiding is still a known threat:
            # it joins the penalized maps, so the route enters as few known
            # threats as it can, learned or relaxed alike, never trading one
            # learned map for several relaxed ones.
            return {**kwargs, "avoided_maps": avoided,
                    "penalized_maps": kwargs["penalized_maps"] | (kwargs["avoided_maps"] - avoided)}

        try:
            route = self._route_points(*args, unwalled_retry=False, **kwargs)
        except ValueError as first:
            # Components are derived acceleration data. A live exceptional
            # start square (closed door, runtime map mutation, occupied seam)
            # must never make a later route permanently unroutable.
            try:
                if not self._refresh_components():
                    raise first
                route = self._route_points(*args, unwalled_retry=False, **kwargs)
            except ValueError as preferred_exc:
                # Tiled levels only joined the hazards to keep surface trips
                # off dense tunnels. A trip that needs them (one inside the
                # tunnels, or with no surface way round) routes through them
                # as before, still avoiding every other hazard.
                tiled = {path for path in pack_hazards
                         if self.TILED_LEVEL_MAP.fullmatch(path)}
                route = None
                tunnels = relaxed(kwargs["avoided_maps"] - tiled)
                if tiled:
                    # Only this retry lifts them; later tiers relax other
                    # hazards first and keep avoiding dense tunnels.
                    try:
                        route = self._route_points(*args, unwalled_retry=False, **tunnels)
                    except ValueError:
                        route = None
                if route is None and self._walls:
                    # Those retries relax no hazard: only after them is the
                    # same tier tried without the owner's blocks.
                    try:
                        route = self._unwalled_route(
                            preferred_exc, args, [kwargs] + ([tunnels] if tiled else []))
                    except ValueError:
                        route = None
                if route is None and overlevel_hazards:
                    fallback_kwargs = relaxed(protected_hazards)
                    try:
                        route = self._route_points(
                            *args, **fallback_kwargs)
                    except ValueError:
                        if not self.allow_ranged_hazard_fallback:
                            excluded = sorted(self._excluded_edges)
                            raise ValueError(
                                f"{preferred_exc}; excluded edges: "
                                f"{excluded}") from preferred_exc
                        fallback_kwargs = relaxed(set(self.KNOWN_LETHAL_TRANSIT))
                        route = self._route_points(
                            *args, **fallback_kwargs)
                    self._threat_fallback = self.hazard_fallback_taken = True
                    if not previous_fallback:
                        recorder = getattr(client, "record_action", None)
                        if recorder is not None:
                            recorder(
                                "navigation-threat-fallback",
                                f"no threat-free route to {self.destination}")
                elif (route is None and self.allow_ranged_hazard_fallback and
                      (ranged_hazards or pack_hazards)):
                    fallback_kwargs = relaxed(set(self.KNOWN_LETHAL_TRANSIT))
                    route = self._route_points(
                        *args, **fallback_kwargs)
                    self._threat_fallback = self.hazard_fallback_taken = True
                    if not previous_fallback:
                        recorder = getattr(client, "record_action", None)
                        if recorder is not None:
                            recorder(
                                "navigation-threat-fallback",
                                "careful-clear expedition requires hostile "
                                f"transit to {self.destination}")
                elif route is None:
                    excluded = sorted(self._excluded_edges)
                    raise ValueError(
                        f"{preferred_exc}; excluded edges: {excluded}") \
                        from preferred_exc
        route_hazards = overlevel_hazards | (
            (ranged_hazards | pack_hazards)
            if self.allow_ranged_hazard_fallback else set())
        self._route_threat_maps = {
            edge.destination for edge in route
            if edge.destination in route_hazards
        }
        return route

    def _approach_candidates(self) -> list[tuple[int, int]]:
        """Return perimeter squares instead of an occupied interaction tile."""
        assert self.destination_xy is not None and self.tolerance > 0
        tx, ty = self.destination_xy
        node = self.graph.nodes.get(self.destination)
        candidates = []
        for dx in range(-self.tolerance, self.tolerance + 1):
            for dy in range(-self.tolerance, self.tolerance + 1):
                if max(abs(dx), abs(dy)) != self.tolerance:
                    continue
                x, y = tx + dx, ty + dy
                if node and not (0 <= x < node.width and 0 <= y < node.height):
                    continue
                if node and not node.walkable(x, y):
                    continue
                candidates.append((x, y))
        _, px, py = self._last_position or (self.destination, tx, ty)
        candidates.sort(key=lambda point: (
            max(abs(point[0] - px), abs(point[1] - py)),
            abs(point[0] - px) + abs(point[1] - py),
        ))
        return candidates

    @staticmethod
    def _viewport_target(client: AtrinikClient, x: int, y: int) -> tuple[int, int]:
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        vx, vy = cx + x - m.world_x, cy + y - m.world_y
        return max(1, min(m.width - 2, vx)), max(1, min(m.height - 2, vy))

    def _safe_click_target(self, client: AtrinikClient, x: int,
                           y: int) -> tuple[int, int] | None:
        """Choose the furthest visible authored-walkable point toward a goal."""
        m = client.state.map
        start = (m.world_x, m.world_y)
        path = self.graph.local_path(
            m.path, start, (x, y), allow_locked=self._allow_locked(client),
            excluded=self._runtime_blocked | self._threat_blocked)
        if not path:
            return None if m.path in self.graph.nodes else (x, y)
        cx, cy = m.width // 2, m.height // 2
        for px, py in reversed(path[1:]):
            vx, vy = cx + px - m.world_x, cy + py - m.world_y
            if 1 <= vx < m.width - 1 and 1 <= vy < m.height - 1:
                return px, py
        # At a map boundary the out-of-range destination itself is now visible
        # and is what tells the server to cross into the connected map.
        if start == path[-1]:
            node = self.graph.nodes.get(m.path)
            if node is None:
                return x, y
            cross_x = (-1 if x < 0 else node.width
                       if x >= node.width else start[0])
            cross_y = (-1 if y < 0 else node.height
                       if y >= node.height else start[1])
            return cross_x, cross_y
        return path[min(1, len(path) - 1)]

    @staticmethod
    def _ground_exit(client: AtrinikClient, edge: MapEdge):
        """Find the server-tagged exit under the player, if it is visible."""
        items = [item for item in client.state.ground if item.tag > 0]
        if not items:
            return None
        label = edge.label.casefold().strip()
        # Ground inventory packets do not carry TYPE in the current protocol,
        # but retain this fast path for servers which provide it in an update.
        typed = [item for item in items if item.item_type == c.TYPE_EXIT]
        if typed:
            return typed[0]
        if label:
            labelled = [item for item in items
                        if label == item.name.casefold().strip() or
                        label in item.name.casefold() or
                        item.name.casefold() in label]
            if labelled:
                return labelled[0]
        transit = re.compile(
            r"\b(stairs?|ladder|trapdoor|portal|entrance|exit|gangway|rope)\b",
            re.I,
        )
        return next((item for item in items if transit.search(item.name)), None)

    def _record_progress(self, client: AtrinikClient) -> None:
        position = (client.state.map.path, client.state.map.world_x,
                    client.state.map.world_y)
        if position != self._last_position:
            now = time.monotonic()
            if (self._last_position is not None and
                position[0] != self._last_position[0]):
                self._runtime_blocked.clear()
                self._threat_blocked.clear()
            self._last_position = position
            self._last_progress = now

    def _stall_seconds(self, client: AtrinikClient | None = None) -> float:
        """How long without a step counts as a stall, at her own pace.

        At least one movement acknowledgement at her speed and action time
        (movement_ack_timeout), so a slow step finishes instead of being
        cancelled as a stall; STALL_RETRY_SECONDS when her speed is not known
        yet, and never above the cap.
        """
        stats = getattr(getattr(client, "state", None), "stats", None)
        if not stats or not float(stats.get("speed", 0) or 0):
            return self.STALL_RETRY_SECONDS
        return min(self.STALL_RETRY_MAX_SECONDS,
                   max(self.STALL_RETRY_SECONDS, movement_ack_timeout(client)))

    def _movement_blockers(self, client: AtrinikClient) -> set[tuple[int, int]]:
        """Combine learned fixtures with currently visible living occupants."""
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        targets = getattr(m, "targets", None)
        temporary = set()
        if callable(targets):
            for view_x, view_y, obj in targets():
                if not obj.target_id:
                    continue
                point = (m.world_x + view_x - cx,
                         m.world_y + view_y - cy)
                if point != (m.world_x, m.world_y):
                    temporary.add(point)
        newly_blocked = temporary - self._temporary_blocked
        self._temporary_blocked = temporary
        if newly_blocked:
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "navigation-occupant-blocked",
                    ", ".join(f"{x},{y}" for x, y in sorted(newly_blocked)))
        return (self._runtime_blocked | self._threat_blocked |
                self._temporary_blocked |
                self._rejected_departures_on(m.path, (m.world_x, m.world_y)))

    def _reject_departure(self, key: tuple[str, int, int]) -> None:
        """Close one seam departure square that rejected her step.

        Like _exclude_edge it is a detour hint that expires:
        EDGE_EXCLUSION_SECONDS, doubled on each repeat on this trip, up to
        EDGE_EXCLUSION_MAX_SECONDS. A guard or a crowd moves on, and the
        task instance can outlive one trip by hours.
        """
        strikes = self._departure_rejection_strikes.get(key, 0)
        self._departure_rejection_strikes[key] = strikes + 1
        seconds = min(self.EDGE_EXCLUSION_MAX_SECONDS,
                      self.EDGE_EXCLUSION_SECONDS * 2 ** min(strikes, 8))
        self._rejected_departures[key] = time.monotonic() + seconds

    def _rejected_departures_on(self, map_path: str,
                                start: tuple[int, int]) -> set[tuple[int, int]]:
        """Live rejected seam departure squares on one map.

        Unlike learned stall blockers, a learned-block reset does not reopen
        these before they expire: live, an unseen guard held the departure
        of Greyton's lower west seam, and resetting it as a stale guess sent
        Sera stepping into it every half second for twenty minutes. The
        square she stands on is never excluded, so a path can still start
        there. Expired rejections are dropped.
        """
        now = time.monotonic()
        self._rejected_departures = {
            key: until for key, until in self._rejected_departures.items()
            if until > now}
        return {
            (x, y) for path, x, y in self._rejected_departures
            if path == map_path} - {start}

    async def _click_path(self, client: AtrinikClient, world_x: int,
                          world_y: int) -> bool:
        """Send one authored direct move, learning live blockers on stalls."""
        goal = (client.state.map.path, world_x, world_y)
        position = (client.state.map.path, client.state.map.world_x,
                    client.state.map.world_y)
        now = time.monotonic()
        blocked = self._movement_blockers(client)
        if (self._issued_click is not None and
                self._issued_click[0] == client.state.map.path and
                self._issued_click[1:] in self._temporary_blocked):
            await client.clear_actions()
            self._issued_goal = None
            self._issued_click = None
            self._last_action = now
            return True
        if (goal == self._issued_goal and
                position != self._issued_click and
                now - self._last_progress < self._stall_seconds(client)):
            return True
        if (goal == self._issued_goal and
                position != self._issued_click and
                await self._escape_closed_door(client, world_x, world_y, now)):
            return True
        if (goal == self._issued_goal and position != self._issued_click):
            m = client.state.map
            start = (m.world_x, m.world_y)
            stalled_path = self.graph.local_path(
                m.path, start, (world_x, world_y),
                allow_locked=self._allow_locked(client),
                excluded=blocked)
            node = self.graph.nodes.get(m.path)
            if (len(stalled_path) > 1 and node is not None and
                    stalled_path[1] not in node.doors):
                self._runtime_blocked.add(stalled_path[1])
                blocked = self._movement_blockers(client)
                alternate = self.graph.local_path(
                    m.path, start, (world_x, world_y),
                    allow_locked=self._allow_locked(client),
                    excluded=blocked)
                if len(alternate) > 1:
                    dx = alternate[1][0] - start[0]
                    dy = alternate[1][1] - start[1]
                    direction = next((
                        value for value, delta in c.DIRECTION_DELTAS.items()
                        if delta == (dx, dy)
                    ), 0)
                    if direction:
                        # Force one verified authored step around the live
                        # blocker. A second long server click can otherwise
                        # choose the same corpse/runtime obstacle again.
                        await client.clear_actions()
                        await client.move(direction)
                        self._issued_goal = goal
                        self._issued_click = (m.path, *alternate[1])
                        self._last_action = now
                        return True
                self._issued_goal = None
                self._issued_click = None

        if now - self._last_action < 0.2:
            return True
        if await self._step_along_boundary(
                client, world_x, world_y, now):
            return True
        m = client.state.map
        start = (m.world_x, m.world_y)
        path = self.graph.local_path(
            m.path, start, (world_x, world_y),
            allow_locked=self._allow_locked(client),
            excluded=blocked)
        if path:
            self._no_route_since = 0.0
        elif not self._no_route_since:
            self._no_route_since = now
        if not path:
            # Reset learned blockers only when they are what closes the route:
            # a route must exist once they alone are lifted. Threats and live
            # occupants keep their own waiting and boundary handling.
            if (self._runtime_blocked and
                    now - self._no_route_since >= self.LEARNED_BLOCK_RESET_SECONDS and
                    self.graph.local_path(
                        m.path, start, (world_x, world_y),
                        allow_locked=self._allow_locked(client),
                        excluded=(self._threat_blocked |
                                  self._temporary_blocked |
                                  self._rejected_departures_on(
                                      m.path, start)))):
                # Live trap on world_2_67: six learned blocks ringed Sera while a
                # visible guard kept this branch on the occupant path below, so
                # she cancelled and replanned for minutes without one step.
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("navigation-learned-blocks-reset",
                             ", ".join(f"{x},{y}" for x, y in sorted(self._runtime_blocked)))
                self._runtime_blocked.clear()
                self._no_route_since = 0.0
                await client.clear_actions()
                self._issued_goal = None
                self._issued_click = None
                self._last_action = now
                return True
            if self._temporary_blocked:
                if await self._step_to_open_boundary(
                        client, world_x, world_y, now, blocked):
                    return True
                await client.clear_actions()
                self._issued_goal = None
                self._issued_click = None
                self._last_action = now
                return True
            if self._runtime_blocked:
                if self.tolerance:
                    return True
                self._runtime_blocked.clear()
                return True
            return False
        if len(path) > 1:
            next_point = path[1]
            dx, dy = next_point[0] - start[0], next_point[1] - start[1]
        else:
            node = self.graph.nodes.get(m.path)
            if node is None:
                return False
            # The local path clamps an out-of-bounds tiled-map goal to its
            # border square. Once standing there, take the final direct step
            # across the seam instead of invoking another pathfinder.
            dx = -1 if world_x < 0 else 1 if world_x >= node.width else 0
            dy = -1 if world_y < 0 else 1 if world_y >= node.height else 0
            if not dx and not dy:
                return True
            next_point = start[0] + dx, start[1] + dy
        direction = next((
            value for value, delta in c.DIRECTION_DELTAS.items()
            if delta == (dx, dy)
        ), 0)
        if not direction:
            return False
        await client.move(direction)
        self._issued_goal = goal
        self._issued_click = (m.path, *next_point)
        self._last_action = now
        return True

    async def _step_along_boundary(self, client: AtrinikClient, world_x: int,
                                   world_y: int, now: float) -> bool:
        """Prevent server A* from shortcutting through an adjacent map."""
        m = client.state.map
        node = self.graph.nodes.get(m.path)
        if node is None:
            return False
        outside_x = world_x < 0 or world_x >= node.width
        outside_y = world_y < 0 or world_y >= node.height
        if outside_x and outside_y:
            return False
        target = (
            max(0, min(node.width - 1, world_x)),
            max(0, min(node.height - 1, world_y)),
        )
        start = (m.world_x, m.world_y)
        if not (start[0] in (0, node.width - 1) or
                start[1] in (0, node.height - 1)):
            return False
        path = self.graph.local_path(
            m.path, start, target,
            allow_locked=self._allow_locked(client),
            excluded=self._movement_blockers(client))
        if len(path) < 2:
            return False
        dx, dy = path[1][0] - start[0], path[1][1] - start[1]
        direction = next((
            direction for direction, delta in c.DIRECTION_DELTAS.items()
            if delta == (dx, dy)
        ), 0)
        if not direction:
            return False
        await client.clear_actions()
        await client.move(direction)
        self._issued_goal = (m.path, world_x, world_y)
        self._issued_click = (m.path, *path[1])
        self._last_action = now
        return True

    async def _step_to_open_boundary(
            self, client: AtrinikClient, world_x: int, world_y: int,
            now: float, blocked: set[tuple[int, int]]) -> bool:
        """Approach/cross any open square on a tiled-map seam."""
        m = client.state.map
        node = self.graph.nodes.get(m.path)
        if node is None:
            return False
        outside_x = world_x < 0 or world_x >= node.width
        outside_y = world_y < 0 or world_y >= node.height
        if outside_x == outside_y:
            return False
        if outside_x:
            edge_x = 0 if world_x < 0 else node.width - 1
            candidates = [(edge_x, y) for y in range(node.height)]
            outward = (-1 if world_x < 0 else 1, 0)
        else:
            edge_y = 0 if world_y < 0 else node.height - 1
            candidates = [(x, edge_y) for x in range(node.width)]
            outward = (0, -1 if world_y < 0 else 1)
        start = (m.world_x, m.world_y)
        access = self._allow_locked(client)
        paths = [
            path for point in candidates
            if point not in blocked and node.walkable(
                *point, allow_locked=access)
            if (path := self.graph.local_path(
                m.path, start, point,
                allow_locked=access, excluded=blocked))
        ]
        if not paths:
            return False
        path = min(paths, key=lambda value: (len(value), value[-1]))
        if len(path) > 1:
            next_point = path[1]
            delta = next_point[0] - start[0], next_point[1] - start[1]
        else:
            next_point = start[0] + outward[0], start[1] + outward[1]
            delta = outward
        direction = next((
            direction for direction, value in c.DIRECTION_DELTAS.items()
            if value == delta), 0)
        if not direction:
            return False
        await client.clear_actions()
        await client.move(direction)
        self._issued_goal = (m.path, world_x, world_y)
        self._issued_click = (m.path, *next_point)
        self._last_action = now
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-seam-alternate",
                f"nominal={world_x},{world_y} "
                f"open={path[-1][0]},{path[-1][1]}")
        return True

    async def _escape_closed_door(self, client: AtrinikClient, world_x: int,
                                  world_y: int, now: float) -> bool:
        """Recover when a closed door stalls the server click path.

        The server's click-path search can reject a path when its starting
        square became NO_PASS after the player entered it, or when the next
        route square is a closed door which must be bumped open. Use one
        cardinal move, then return immediately to normal click routing.
        """
        m = client.state.map
        node = self.graph.nodes.get(m.path)
        start = (m.world_x, m.world_y)
        if node is None:
            return False
        candidates: list[tuple[int, int, int, int]] = []
        if start in node.doors:
            for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
                if dx and dy:
                    continue
                point = start[0] + dx, start[1] + dy
                if point in node.doors or not node.walkable(
                        *point, allow_locked=self._allow_locked(client)):
                    continue
                path = self.graph.local_path(
                    m.path, point, (world_x, world_y),
                    allow_locked=self._allow_locked(client))
                if path:
                    candidates.append(
                        (len(path), direction, point[0], point[1]))
        else:
            path = self.graph.local_path(
                m.path, start, (world_x, world_y),
                allow_locked=self._allow_locked(client))
            if len(path) > 1 and path[1] in node.doors:
                dx = path[1][0] - start[0]
                dy = path[1][1] - start[1]
                if not (dx and dy):
                    direction = next((
                        direction for direction, delta
                        in c.DIRECTION_DELTAS.items() if delta == (dx, dy)
                    ), 0)
                    if direction:
                        candidates.append(
                            (len(path), direction, path[1][0], path[1][1]))
        if not candidates:
            return False
        _, direction, x, y = min(candidates)
        await client.clear_actions()
        await client.move(direction)
        self._issued_click = (m.path, x, y)
        self._last_action = now
        return True

    async def _bump_boundary_door(self, client: AtrinikClient,
                                  edge: MapEdge) -> bool:
        """Open a door occupying the arrival square across a tiled-map seam."""
        if edge.kind != "tile":
            return False
        source = self.graph.nodes.get(edge.source)
        destination = self.graph.nodes.get(edge.destination)
        if source is None or destination is None:
            return False
        x, y = client.state.map.world_x, client.state.map.world_y
        direction = 0
        arrival: tuple[int, int] | None = None
        if edge.x < 0 and x == 0 and y == edge.y:
            direction, arrival = 7, (destination.width - 1, edge.y)
        elif (edge.x >= source.width and x == source.width - 1 and
              y == edge.y):
            direction, arrival = 3, (0, edge.y)
        elif edge.y < 0 and y == 0 and x == edge.x:
            direction, arrival = 1, (edge.x, destination.height - 1)
        elif (edge.y >= source.height and y == source.height - 1 and
              x == edge.x):
            direction, arrival = 5, (edge.x, 0)
        if arrival is None or arrival not in destination.doors:
            return False
        if time.monotonic() - self._last_action < 0.4:
            return True
        await client.clear_actions()
        await client.move(direction)
        self._last_action = time.monotonic()
        self._issued_goal = (edge.source, edge.x, edge.y)
        return True

    async def _retry_stalled_tile_crossing(
            self, client: AtrinikClient, edge: MapEdge) -> bool:
        """Retarget a rejected whole-border seam instead of spamming a step."""
        if (edge.kind != "tile" or
                self._issued_goal != (edge.source, edge.x, edge.y)):
            return False
        source = self.graph.nodes.get(edge.source)
        destination = self.graph.nodes.get(edge.destination)
        if source is None or destination is None:
            return False
        start = (client.state.map.world_x, client.state.map.world_y)

        def side(candidate: MapEdge) -> str:
            if candidate.x < 0 and 0 <= candidate.y < source.height:
                return "west"
            if (candidate.x >= source.width and
                    0 <= candidate.y < source.height):
                return "east"
            if candidate.y < 0 and 0 <= candidate.x < source.width:
                return "north"
            if (candidate.y >= source.height and
                    0 <= candidate.x < source.width):
                return "south"
            return ""

        direction = side(edge)
        at_border = {
            "west": start[0] == 0,
            "east": start[0] == source.width - 1,
            "north": start[1] == 0,
            "south": start[1] == source.height - 1,
        }.get(direction, False)
        stats = getattr(client.state, "stats", None)
        acknowledgement = (
            movement_ack_timeout(client) if stats is not None else 1.5)
        now = time.monotonic()
        stalled_departure = (max(0, min(source.width - 1, edge.x)),
                             max(0, min(source.height - 1, edge.y)))
        # An unseen occupant (a guard on a fogged square sends no object)
        # can hold the departure square itself, so the step onto it is the
        # one rejected while she is still one tile inside the border. A
        # corner edge has no departure side.
        onto_departure = (
            bool(direction) and not at_border and
            start != stalled_departure and
            self._issued_click == (edge.source, *stalled_departure))
        stalled_for = now - self._last_progress
        if onto_departure:
            # Time the step itself, not her last move: she may have idled
            # beside the departure for long before stepping. The attempt
            # lasts until she moves or steps from elsewhere, however often
            # the step is re-sent in between.
            key = (edge.source, start, stalled_departure)
            attempt = self._departure_attempt
            if (attempt is None or attempt[0] != key or
                    self._last_progress > attempt[1]):
                # A closed door absorbs the first step (it opens instead of
                # moving her), so its timer starts at the next send.
                attempt = (key, now, self._last_action,
                           None if stalled_departure in source.doors
                           else now)
            elif attempt[3] is None and self._last_action != attempt[2]:
                attempt = (*attempt[:3], now)
            self._departure_attempt = attempt
            stalled_for = (-1.0 if attempt[3] is None
                           else now - attempt[3])
        if (not (at_border or onto_departure) or
                stalled_for < acknowledgement):
            return False
        self._failed_tile_crossings.add(
            (edge.source, edge.destination, edge.x, edge.y))
        if onto_departure:
            self._departure_attempt = None
            self._reject_departure((edge.source, *stalled_departure))
        authored = next((
            candidate for candidate in source.edges
            if candidate.kind == "tile" and
            candidate.destination == edge.destination and
            side(candidate) == direction
        ), edge)
        authored_orthogonal = (
            authored.y if direction in ("west", "east") else authored.x)

        crossings = []
        if direction == "west":
            crossings = [
                ((-1, y), (0, y), (destination.width - 1, y), y)
                for y in range(min(source.height, destination.height))]
        elif direction == "east":
            crossings = [
                ((source.width, y), (source.width - 1, y), (0, y), y)
                for y in range(min(source.height, destination.height))]
        elif direction == "north":
            crossings = [
                ((x, -1), (x, 0), (x, destination.height - 1), x)
                for x in range(min(source.width, destination.width))]
        elif direction == "south":
            crossings = [
                ((x, source.height), (x, source.height - 1), (x, 0), x)
                for x in range(min(source.width, destination.width))]

        candidates = []
        access = self._allow_locked(client)
        for crossing, departure, arrival, orthogonal in crossings:
            key = (edge.source, edge.destination, *crossing)
            if (key in self._failed_tile_crossings or
                    not source.walkable(*departure, allow_locked=access) or
                    not destination.walkable(*arrival, allow_locked=access) or
                    arrival in destination.occupied):
                continue
            path = self.graph.local_path(
                edge.source, start, departure,
                allow_locked=access,
                excluded=(self._runtime_blocked | self._threat_blocked |
                          self._rejected_departures_on(edge.source, start)))
            if path:
                candidates.append((
                    abs(orthogonal - authored_orthogonal),
                    len(path), crossing))
        if not candidates:
            route_events.note_edge(client, self, edge)
            self._exclude_edge(edge)
            self.route = []
            self._issued_goal = None
            self._issued_click = None
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "navigation-seam-excluded",
                    f"{edge.source} -> {edge.destination}")
            return True

        _, _, crossing = min(candidates)
        failed = edge.x, edge.y
        self.route[0] = replace(edge, x=crossing[0], y=crossing[1])
        self._issued_goal = None
        self._issued_click = None
        self._last_progress = time.monotonic()
        await client.clear_actions()
        route_events.note_edge(client, self, edge)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-seam-retry",
                f"{failed} rejected; trying {crossing}")
        return True

    # A voyage whose last link was chosen must leave within this long.
    VOYAGE_LEAVE_SECONDS = 8.0
    # The walk to a square beside a voyage's NPC gives up after this long
    # without a step, and excludes the edge for a while, as an NPC that
    # does not answer does.
    VOYAGE_APPROACH_STALL_SECONDS = 20.0

    def _replan(self, client: AtrinikClient) -> None:
        try:
            self.route = self._plan(client)
        except ValueError as exc:
            if not self._wait_for_exclusions(client, exc):
                self.fail(str(exc))

    async def _take_voyage(self, client: AtrinikClient, edge: MapEdge) -> None:
        """Take one dialogue voyage (dialogue_edges.py): walk beside the
        NPC, then follow its authored pages exactly (VoyageDialogTask).

        A voyage no longer offered (her journal, a refusal) is dropped for
        a new plan. A page that is not the authored one, a voyage that does
        not leave, or one that lands elsewhere is refused for her, so plans
        leave it out until her unlock state changes or a day passes. An NPC
        that does not answer (it wandered, a fight), and a walk beside it
        that finds no square or has not moved her for
        VOYAGE_APPROACH_STALL_SECONDS (a gap of more than 5 s between ticks
        starts that afresh), only exclude the edge for a while, like a held
        exit.
        """
        offer = dialogue_edges.dialogue_offer(client, self.graph)
        voyage = offer.voyage(edge) if offer is not None else None
        if self._voyage is not None and (
                self._voyage[0] != edge or self._voyage[1] is not voyage):
            self._voyage = None
        if voyage is None:
            self._replan(client)
            return
        if self._voyage is None:
            m = client.state.map
            here = m.world_x, m.world_y
            if max(abs(here[0] - edge.x), abs(here[1] - edge.y)) > 1:
                approaches = self.graph.dialogue_approaches(
                    edge, here, allow_locked=self._allow_locked(client))
                now = time.monotonic()
                key = (edge, client.state.map.path, here)
                # A gap between ticks (a fight held the trip) starts afresh.
                if (self._voyage_approach is None or
                        self._voyage_approach[0] != key or
                        now - self._voyage_approach[2] > 5.0):
                    self._voyage_approach = (key, now, now)
                self._voyage_approach = (key, self._voyage_approach[1], now)
                stalled = (now - self._voyage_approach[1] >
                           self.VOYAGE_APPROACH_STALL_SECONDS)
                if stalled or not approaches or not await self._click_path(
                        client, *approaches[0]):
                    self._exclude_edge(edge)
                    self._record(client, "navigation-voyage-failed",
                                 f"{voyage.subject}: " + (
                                     "the walk to the NPC stalled" if stalled
                                     else "no walk to the NPC"))
                    self._voyage_approach = None
                    self._replan(client)
                return
            self._voyage_approach = None
            from .quest_tasks import VoyageDialogTask
            await client.clear_actions()
            self._issued_goal = self._issued_click = None
            self._voyage = (edge, voyage, VoyageDialogTask(
                voyage.npc, voyage.pages, edge.destination,
                leave_seconds=self.VOYAGE_LEAVE_SECONDS))
            self._record(client, "navigation-voyage", voyage.subject)
        task = self._voyage[2]
        await task.tick(client)
        self._last_progress = time.monotonic()
        if task.status != TaskStatus.FAILED:
            return
        self._voyage = None
        if task.refused:
            dialogue_edges.refuse(client, self.graph, voyage, task.error)
            self._record(client, "navigation-voyage-refused",
                         f"{voyage.subject}: {task.error}")
        else:
            self._exclude_edge(edge)
            self._record(client, "navigation-voyage-failed",
                         f"{voyage.subject}: {task.error}")
        self._replan(client)

    def _voyage_landed_elsewhere(self, client: AtrinikClient) -> None:
        """She left the voyage's map but not for its destination. Once its
        last link was chosen, that is the voyage's doing: refuse it. Before
        then (a death, a recall) nothing is learned."""
        edge, voyage, task = self._voyage
        self._voyage = None
        if not getattr(task, "left_at", 0.0):
            return
        reason = f"landed on {client.state.map.path}, not {edge.destination}"
        dialogue_edges.refuse(client, self.graph, voyage, reason)
        self._record(client, "navigation-voyage-refused",
                     f"{voyage.subject}: {reason}")

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            try:
                await self.start(client)
            except ValueError as exc:
                self.fail(str(exc))
                return
        if self.status != TaskStatus.RUNNING or client.state.phase != "playing":
            return
        self._record_progress(client)
        # Her route events: a crossing since the last tick, the edge ahead.
        route_events.note_tick(client, self)
        if self.care is not None:
            # A crossing with no block left here or ahead is finished.
            self.care_holds_fire(client.state.map.path)
            if await self._care_hold(client):
                return
        if client.state.map.path == self.destination and not self.route:
            if self.destination_xy is None:
                # The final S_MOVE_PATH may contain compressed steps beyond a
                # tiled-map boundary. Stop it before handing control to a farm
                # or interaction task, otherwise the character can walk out of
                # the requested zone after navigation reports completion.
                await client.clear_actions()
                self.complete()
                return
            x, y = self._viewport_target(client, *self.destination_xy)
            if max(abs(client.state.map.world_x - self.destination_xy[0]),
                   abs(client.state.map.world_y - self.destination_xy[1])) <= self.tolerance:
                # S_MOVE_PATH is asynchronous. Without clearing it, entering
                # interaction range can complete this task while the server
                # continues along the old path, often into or past the NPC.
                await client.clear_actions()
                self.complete()
                return
            if self.tolerance:
                if not self._approach:
                    self._approach = self._approach_candidates()
                elif (time.monotonic() - self._last_progress >
                      max(1.5, self._stall_seconds(client) + 1.0)):
                    # Later than the stall deadline: the current goal's stall
                    # is learned (its blocker, the cancelled move) first.
                    # An authored wall, counter, or NPC can occupy a candidate.
                    # Try the next perimeter square only after movement stalls,
                    # so a long valid path is not repeatedly cleared.
                    self._approach_index = (
                        self._approach_index + 1) % len(self._approach)
                    if self._approach_index == 0:
                        self._runtime_blocked.clear()
                    self._last_progress = time.monotonic()
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder(
                            "navigation-approach-retry",
                            f"candidate={self._approach_index + 1}/"
                            f"{len(self._approach)}")
                if self._approach:
                    destination = self._approach[self._approach_index]
                else:
                    destination = self.destination_xy
            else:
                destination = self.destination_xy
            if time.monotonic() < self._exclusion_wait_until:
                # Waiting for an exclusion: the goal is not reachable on
                # this map without the edge it closes.
                return
            if not await self._click_path(client, *destination):
                self.fail(f"no authored path to destination {destination}")
            return
        # Preserve the remainder of the component-aware plan after crossing a
        # map edge. Replanning immediately can choose a different entrance to
        # the map component we just left and create a two-map oscillation.
        if (self.route and
                self.route[0].source != client.state.map.path and
                self.route[0].destination == client.state.map.path):
            arrived = self.route.pop(0)
            self._issued_goal = None
            self._issued_click = None
            if arrived.kind == dialogue_edges.DIALOGUE:
                self._voyage = None
                self._record(client, "navigation-voyage-arrived",
                             route_blocks.edge_subject(arrived))
        if not self.route or self.route[0].source != client.state.map.path:
            if self._voyage is not None:
                self._voyage_landed_elsewhere(client)
            if time.monotonic() < self._exclusion_wait_until:
                return
            self._replan(client)
            if self.status != TaskStatus.RUNNING or not self.route:
                return
            if self.care is not None and await self._care_hold(client):
                return
        edge = self.route[0]
        if edge.kind == dialogue_edges.DIALOGUE:
            await self._take_voyage(client, edge)
            return
        if await self._retry_stalled_tile_crossing(client, edge):
            return
        if await self._bump_boundary_door(client, edge):
            return
        if (edge.kind == "exit" and
                (client.state.map.world_x, client.state.map.world_y) ==
                (edge.x, edge.y)):
            exit_item = self._ground_exit(client, edge)
            if (exit_item is not None and
                    time.monotonic() - self._last_action >= 0.4):
                # Stairs and similar type-66 exits do not trigger when merely
                # walked onto. Apply their ground-inventory tag exactly as the
                # official client does.
                await client.apply(exit_item.tag)
                self._last_action = time.monotonic()
                return
            if (exit_item is None and
                    time.monotonic() - self._last_action >= 1.0):
                # The below-inventory window is paginated and can omit an
                # applicable exit tag. Tag zero uses the protocol's apply-below
                # form, which examines the current square for an exit.
                await client.apply(0)
                self._last_action = time.monotonic()
                return
        if not await self._click_path(client, edge.x, edge.y):
            route_events.note_edge(client, self, edge)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                detail = f"{edge.source} -> {edge.destination} at={edge.x},{edge.y}"
                if isinstance(client, AtrinikClient):
                    # A tick never waits SQLite's usual 10 s for it.
                    recorder("navigation-click-failed", detail,
                             save_timeout=route_events.WRITE_TIMEOUT)
                else:
                    recorder("navigation-click-failed", detail)
            self._exclude_edge(edge)
            self._replan(client)
            return
        if (client.state.map.path == edge.destination and
                (edge.source != edge.destination or
                 (client.state.map.world_x, client.state.map.world_y) in
                 self.graph._component(
                     edge.destination,
                     (edge.destination_x, edge.destination_y),
                     allow_locked=self._allow_locked(client)))):
            self.route.pop(0)


class NavigateThenTask(BotTask):
    """Route to an authored map, then run another character-state task."""

    PACK_EXIT_BYPASS_SECONDS = 15.0
    # A pack-held exit kept as the last way on is held at most this long in
    # all, from when it was first kept, then excluded as any other.
    PACK_EXIT_KEEP_SECONDS = 180.0
    # Off the destination map, fight back only once something has been
    # hitting her and the route has made no progress for this long.
    TRANSIT_CORNERED_SECONDS = 4.0
    # A held-fire halo that alone leaves no route yields after this long.
    HALO_YIELD_SECONDS = 5.0
    # A hostile whose authored level is more than this many levels below
    # hers does not count toward a pack (_outclassed_ids): she still fights
    # it, but two of them do not make her split and retreat.
    OUTCLASSED_LEVEL_GAP = 10
    # This many outclassed creatures within 4 tiles count as a pack again.
    OUTCLASSED_PACK = 3
    # A cornered pack split that holds (nothing beside her, nothing hitting
    # her, no step that gains distance) fights the nearest after this long.
    CORNERED_HOLD_SECONDS = 8.0

    def __init__(self, graph: WorldGraph, destination: str, task: BotTask,
                 destination_xy: tuple[int, int] | None = None,
                 allow_locked: bool | None = None,
                 combat_approach: bool = False,
                 safety: SafetyPolicy | None = None):
        super().__init__(f"navigate-then:{task.name}")
        self.navigation = NavigateTask(
            graph, destination, destination_xy,
            allow_locked=allow_locked)
        self.task = task
        if isinstance(task, FarmTask):
            node = getattr(graph, "nodes", {}).get(destination)
            if node is not None:
                task.map_bounds = (node.width, node.height)
                task.map_node = node
        self.combat_approach = combat_approach
        self.safety = safety or SafetyPolicy()
        self._defending = False
        self._last_defense_action = 0.0
        self._retreat_attempt: tuple[str, int, int, float] | None = None
        # Where she stood when the step in _retreat_attempt was sent.
        self._retreat_from: tuple[str, int, int] | None = None
        self._retreat_sent_at = 0.0
        # Retreat squares a step to which never landed, and when each was
        # learned; they expire (_prune_retreat_blocked).
        self._retreat_blocked: set[tuple[str, int, int]] = set()
        self._retreat_blocked_at: dict[tuple[str, int, int], float] = {}
        self._trail: list[tuple[str, int, int]] = []
        # The square her planned route leaves the current map by, kept for
        # this map while a pack avoidance or a failed plan clears the route
        # (_way_out): her retreat steps prefer making progress towards it.
        self._way_out_goal: tuple[str, int, int, tuple | None] | None = None
        self._combat_attempt: tuple[str, int, int, float] | None = None
        self._combat_blocked: set[tuple[str, int, int]] = set()
        self._last_pull_target = 0
        self._last_pull_at = 0.0
        # Where and when the character last actually moved. The cornered
        # test needs real movement: route bookkeeping such as pack-avoidance
        # replans refreshes the navigation's own progress clock.
        self._last_position: tuple[str, int, int] | None = None
        self._last_moved_at = 0.0
        # Threats (id and world tile) the last held-fire halo was built around.
        self._hold_fire_signature: tuple[tuple[int, int, int], ...] = ()
        # Held-fire creatures whose halo has yielded to a closed route.
        self._halo_yield: set[int] = set()
        # Since when held-fire halos alone have closed the local route.
        self._halo_closed_since = 0.0
        self._halo_closed_for: frozenset[int] = frozenset()
        self._last_danger_target = 0
        self._last_pack_signature: tuple[int, ...] = ()
        self._pack_exit_stall: tuple[
            tuple[str, str, int, int], float] | None = None
        # Pack-held exits kept because they were the last way on, and when
        # each was first kept (PACK_EXIT_KEEP_SECONDS).
        self._pack_exit_kept: dict[tuple[str, str, int, int], float] = {}
        self._recovering_after_combat = False
        self._transit_stall: tuple[
            int, str, int, int, int, int, int, float] | None = None
        # A critical escape (_critical_escape_due) under way: it holds until
        # she is back above the heal threshold or has a heal or recall again.
        self._escaping = False
        # The pack-split hold of the last tick: (position, nearby ids). A
        # repeat with no change resends neither its clear nor combat off.
        self._split_hold: tuple | None = None
        # The attacker a cornered pack-split fights (no retreat square):
        # fought until it dies or is more than 4 tiles off, so an opening
        # square does not flip her back to retreating mid-fight.
        self._cornered_target = 0
        # Targets a cornered fight has been logged for, once each.
        self._cornered_logged: set[int] = set()
        # Since when a cornered split has held with no step (0: not).
        self._cornered_hold_since = 0.0

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        # A new walk starts no escape of an earlier one.
        self._escaping = False
        self._cornered_target = 0
        self._cornered_logged.clear()
        self._cornered_hold_since = 0.0
        self._split_hold = None

    async def _bypass_pack_blocked_exit(
            self, client: AtrinikClient) -> bool:
        """Abandon an exit whose approach stays occupied by a live pack."""
        if not self.navigation.route:
            self._pack_exit_stall = None
            return False
        edge = self.navigation.route[0]
        if edge.kind != "exit":
            self._pack_exit_stall = None
            return False
        key = (edge.source, edge.destination, edge.x, edge.y)
        now = time.monotonic()
        if (self._pack_exit_stall is None or
                self._pack_exit_stall[0] != key):
            self._pack_exit_stall = (key, now)
            return False
        held = now - self._pack_exit_stall[1]
        if held < self.PACK_EXIT_BYPASS_SECONDS:
            return False
        kept_since = self._pack_exit_kept.get(key)
        last_way = ((kept_since is None or
                     now - kept_since < self.PACK_EXIT_KEEP_SECONDS) and
                    not await self.navigation.graph_route_open_async(
                        client, self.navigation._excluded_edges | {key}))
        # The search may have handed the loop back: act only on the same
        # exit, at the time it ended.
        if (not self.navigation.route or self.navigation.route[0] != edge or
                self._pack_exit_stall is None or
                self._pack_exit_stall[0] != key):
            return False
        now = time.monotonic()
        if last_way:
            # The last way on toward the destination (live: both Old
            # Outpost stairs down, a pack beside them) is not excluded:
            # she holds here and tries again after another
            # PACK_EXIT_BYPASS_SECONDS, for PACK_EXIT_KEEP_SECONDS in all.
            self._pack_exit_stall = (key, now)
            if kept_since is None:
                self._pack_exit_kept[key] = now
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder(
                        "navigation-pack-exit-kept",
                        f"{edge.source} -> {edge.destination} "
                        f"at={edge.x},{edge.y} held={held:.0f}s: the last "
                        "way on")
            return False
        route_events.note_edge(client, self.navigation, edge)
        self.navigation._exclude_edge(edge)
        self.navigation.route = []
        self.navigation._issued_goal = None
        self.navigation._issued_click = None
        self.navigation._last_progress = now
        self._pack_exit_stall = None
        await client.clear_actions()
        await client.set_combat(False)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-pack-exit-bypass",
                f"{edge.source} -> {edge.destination} at={edge.x},{edge.y} "
                f"held={held:.0f}s")
        return True

    async def _abandon_stalled_transit_target(
            self, client: AtrinikClient, threat: tuple | None) -> bool:
        """Route around a selected road target making no observable progress."""
        if (not isinstance(self.task, FarmTask) or threat is None or
                client.state.map.path == self.navigation.destination):
            self._transit_stall = None
            return False
        distance, view_x, view_y, obj = threat
        if distance <= 1:
            self._transit_stall = None
            return False
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        target_x = m.world_x + view_x - cx
        target_y = m.world_y + view_y - cy
        target_hp = int(getattr(obj, "target_hp", 0) or (
            client.state.stats.get("target_hp", 0)
            if client.state.target_id == obj.target_id else 0) or 0)
        signature = (
            obj.target_id, m.path, m.world_x, m.world_y,
            target_x, target_y, target_hp)
        now = time.monotonic()
        previous = self._transit_stall
        if (recent_hostile_contact(client, seconds=3.0) or
                previous is None or previous[:-1] != signature):
            self._transit_stall = (*signature, now)
            return False
        if now - previous[-1] < 10.0:
            return False
        await client.clear_actions()
        await client.set_combat(False)
        clear_target = getattr(client, "clear_target", None)
        if clear_target is not None:
            await clear_target()
        self.task._unreachable_targets[obj.target_id] = (
            now + self.task.UNREACHABLE_TARGET_COOLDOWN_SECONDS)
        if (self.task._engaged_target is not None and
                self.task._engaged_target[0] == obj.target_id):
            self.task._engaged_target = None
        self._last_pull_target = 0
        self._defending = False
        self._transit_stall = None
        self.navigation.route = []
        self.navigation._issued_goal = None
        self.navigation._issued_click = None
        self.navigation._last_progress = now
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-target-stalled",
                f"target={obj.target_id} at={target_x},{target_y} "
                f"hp={target_hp}%")
        log.info(
            "routing around stalled transit target %s at %s (%s, %s)",
            obj.target_id, m.path, target_x, target_y)
        return True

    def _record_trail(self, client: AtrinikClient) -> None:
        """Remember the cleared route and collapse it while backtracking."""
        m = client.state.map
        position = (m.path, m.world_x, m.world_y)
        if self._trail and self._trail[-1] == position:
            return
        if len(self._trail) >= 2 and self._trail[-2] == position:
            self._trail.pop()
        else:
            self._trail.append(position)
            del self._trail[:-200]

    def _way_out(self, client: AtrinikClient) -> tuple[int, int] | None:
        """Where her planned route leaves the current map, if known.

        The next route edge's square when it starts here, else her own
        destination square on the destination map, else the last such
        square noted on this map: avoiding a pack clears the route, and the
        retreat that follows still knows which way is out. A noted edge
        that has since been excluded (_bypass_pack_blocked_exit, a failed
        click or seam) is forgotten.
        """
        m = client.state.map
        navigation = self.navigation
        route = getattr(navigation, "route", None) or ()
        goal = key = None
        if route and route[0].source == m.path:
            edge = route[0]
            goal = edge.x, edge.y
            # _exclude_edge's key: an excluded edge is no way out.
            key = ((edge.source, edge.destination, edge.x, edge.y)
                   if edge.kind != "tile" else (edge.source, edge.destination))
        elif (m.path == navigation.destination and
              navigation.destination_xy is not None and
              not isinstance(self.task, FarmTask)):
            goal = tuple(navigation.destination_xy)
        if goal is not None:
            self._way_out_goal = (m.path, int(goal[0]), int(goal[1]), key)
            return int(goal[0]), int(goal[1])
        cached = self._way_out_goal
        if cached is None or cached[0] != m.path:
            return None
        if cached[3] is not None and cached[3] in set(
                getattr(navigation, "_excluded_edges", ()) or ()):
            # Bypassed (a pack held its approach) or failed: forgotten.
            self._way_out_goal = None
            return None
        return cached[1], cached[2]

    @staticmethod
    def _way_out_distances(node, goal: tuple[int, int], *,
                           allow_locked=False) -> dict[tuple[int, int], int]:
        """Steps from each square of ``node`` to ``goal`` (8-way, authored
        walkability, static occupants excluded). An edge square beyond the
        map's bounds (a tiled-map seam) is measured from the nearest square
        inside them."""
        width, height = int(node.width or 0), int(node.height or 0)
        if width <= 0 or height <= 0:
            return {}
        gx = min(max(int(goal[0]), 0), width - 1)
        gy = min(max(int(goal[1]), 0), height - 1)
        occupied = set(getattr(node, "occupied", ()))
        seeds = [(gx, gy)] if node.walkable(gx, gy, allow_locked=allow_locked) else [
            (gx + dx, gy + dy) for dx, dy in c.DIRECTION_DELTAS.values()
            if (dx or dy) and node.walkable(gx + dx, gy + dy,
                                            allow_locked=allow_locked)]
        distances = {point: 0 for point in seeds}
        queue = deque(seeds)
        while queue:
            current = queue.popleft()
            for dx, dy in c.DIRECTION_DELTAS.values():
                nxt = current[0] + dx, current[1] + dy
                if (nxt in distances or nxt in occupied or
                        not node.walkable(*nxt, allow_locked=allow_locked)):
                    continue
                distances[nxt] = distances[current] + 1
                queue.append(nxt)
        return distances

    # A step back to where a retreat step was sent from this recently
    # undoes it (_retreat_step).
    RETREAT_REVERSAL_SECONDS = 10.0

    # A retreat square learned as blocked is tried again after this long:
    # what blocked it was most often a creature, which moves on.
    RETREAT_BLOCKED_SECONDS = 30.0

    def _prune_retreat_blocked(self, path: str, now: float) -> None:
        """Forget blocked retreat squares older than RETREAT_BLOCKED_SECONDS
        or on another map than ``path``."""
        for key in list(self._retreat_blocked):
            learned = self._retreat_blocked_at.get(key)
            if (key[0] != path or learned is None or
                    now - learned >= self.RETREAT_BLOCKED_SECONDS):
                self._retreat_blocked.discard(key)
                self._retreat_blocked_at.pop(key, None)

    def _retreat_step_pending(self, client: AtrinikClient,
                              now: float) -> bool:
        """Resolve the direct retreat step in flight, or wait for it.

        It is done once she stands on its square. Once she stands anywhere
        else than where she sent it (pushed, teleported, a map change) it is
        forgotten. Still where she was after movement_ack_timeout, its square
        is learned as blocked: static map analysis cannot see every runtime
        wall or blocking object. Until then it is waited for (True), as the
        farm's retreat_ack_pending does: replacing it sooner learned nothing.
        Live (2026-11-22, the Old Outpost) Sera alternated two unlanding
        steps every half second in a doorway for 12 s until she died.
        """
        m = client.state.map
        self._prune_retreat_blocked(m.path, now)
        if self._retreat_attempt is None:
            return False
        position = (m.path, m.world_x, m.world_y)
        path, x, y, sent_at = self._retreat_attempt
        if position == (path, x, y) or (
                self._retreat_from is not None and
                position != self._retreat_from):
            self._retreat_attempt = None
            return False
        if now - sent_at >= movement_ack_timeout(client):
            self._retreat_blocked.add((path, x, y))
            self._retreat_blocked_at[(path, x, y)] = now
            self._retreat_attempt = None
            return False
        return True

    def _send_retreat_step(self, client: AtrinikClient, x: int, y: int,
                           now: float) -> None:
        m = client.state.map
        self._retreat_attempt = (m.path, x, y, now)
        self._retreat_from = (m.path, m.world_x, m.world_y)
        self._retreat_sent_at = now

    async def _retreat_step(self, client: AtrinikClient, threats,
                            *, avoid_backtrack: bool = False,
                            gain_only: bool = False,
                            probe: bool = False) -> bool:
        """Take one authored-walkable step away from the visible pack.

        While a step is in flight it waits for it (_retreat_step_pending)
        and answers True. ``gain_only`` takes only a step that increases
        her separation from the nearest threat: an equal-distance step can
        shuffle between two squares beside a creature that stands still.
        ``probe`` sends nothing: it answers whether there is a step to take
        (or one in flight), so a caller can tell she is cornered.
        """
        m = client.state.map
        now = time.monotonic()
        if self._retreat_step_pending(client, now):
            return True

        nodes = getattr(self.navigation.graph, "nodes", {})
        node = nodes.get(m.path)
        if node is None:
            return False
        cx, cy = m.width // 2, m.height // 2
        threat_world = [
            (m.world_x + x - cx, m.world_y + y - cy)
            for _, x, y, _ in threats
        ]
        current_min = min(
            max(abs(m.world_x - x), abs(m.world_y - y))
            for x, y in threat_world)
        previous = self._trail[-2] if len(self._trail) >= 2 else None
        candidates = []
        mobility_blocked = {(m.world_x, m.world_y), *threat_world}
        mobility_blocked.update(
            (x, y) for path, x, y in self._retreat_blocked
            if path == m.path)
        for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
            point = m.world_x + dx, m.world_y + dy
            if ((m.path, *point) in self._retreat_blocked or
                    point in node.occupied or
                    not node.walkable(*point,
                                      allow_locked=self.navigation._allow_locked(client))):
                continue
            distances = [max(abs(point[0] - x), abs(point[1] - y))
                         for x, y in threat_world]
            minimum = min(distances)
            if minimum < current_min or (
                    gain_only and minimum == current_min):
                continue
            backtrack = int(previous == (m.path, *point))
            # Undoing her last retreat step: the trail collapses a step
            # back, so where a recent step was sent from counts as well.
            reverses = int(bool(backtrack) or (
                now - self._retreat_sent_at <= self.RETREAT_REVERSAL_SECONDS
                and self._retreat_from == (m.path, *point)))
            trail_score = -reverses if avoid_backtrack else backtrack
            escape_depth, escape_area = retreat_mobility(
                node, point, blocked=mobility_blocked,
                allow_locked=self.navigation._allow_locked(client))
            candidates.append(
                (int(escape_depth >= 2), minimum, point, reverses,
                 escape_depth, escape_area, trail_score, sum(distances),
                 direction))
        if not candidates or probe:
            return bool(candidates)
        # Progress towards the way out (_way_out), measured only once a
        # step exists: 1 closer to where her route leaves this map, 0 as
        # close (or no way out known), -1 farther, -2 cut off from it. With
        # avoid_backtrack, a step undoing her last one (her previous trail
        # square, or where her last retreat step was sent from) never
        # counts as progress (at best -1, as a step away): a step back
        # towards the exit beat the backtrack penalty and shuffled her
        # between two squares of a corridor.
        goal = self._way_out(client)
        out_distances = (self._way_out_distances(
            node, goal, allow_locked=self.navigation._allow_locked(client))
            if goal is not None else {})
        here_out = out_distances.get((m.world_x, m.world_y))

        def progress_of(point, reverses) -> int:
            if here_out is None:
                return 0
            if point not in out_distances:
                return -2
            progress = max(-1, min(1, here_out - out_distances[point]))
            return min(progress, -1) if avoid_backtrack and reverses else progress

        # Continuing escape mobility outranks immediate separation: a
        # farther tile in a dead end is not safer than an open flank. Then
        # the way out: of the steps that separate her as far, the one
        # towards where her route leaves the map, not the open room beyond
        # it (live, 2026-11-24, the Old Outpost: she walked off the way out
        # into a five-walled corner and stalled there for 14 minutes). For a
        # dangerous single monster, an equal-distance step must not reverse
        # immediately or a two-tile plateau becomes an endless oscillation.
        scored = [
            (viable, minimum, progress_of(point, reverses), escape_depth,
             escape_area, trail_score, total, direction)
            for (viable, minimum, point, reverses, escape_depth, escape_area,
                 trail_score, total, direction) in candidates]
        # A square with no onward step at all (escape depth 0) is a dead
        # end: taken only when nothing else is open.
        open_candidates = [entry for entry in scored if entry[3] > 0]
        (viable, minimum, progress, escape_depth, escape_area, _, _,
         direction) = max(open_candidates or scored)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-retreat-choice",
                f"direction={direction} separation={minimum} "
                f"escape_depth={escape_depth} escape_area={escape_area} "
                f"continuation={viable} progress={progress}")
        await client.clear_actions()
        # run=True is a persistent directional run mode in the protocol, not
        # a faster single step. Leaving it enabled caused the character to
        # continue into walls after combat had ended.
        await client.move(direction)
        dx, dy = c.DIRECTION_DELTAS[direction]
        self._send_retreat_step(client, m.world_x + dx, m.world_y + dy, now)
        self.navigation._issued_goal = None
        self.navigation._issued_click = None
        self.navigation._last_progress = time.monotonic()
        self._last_defense_action = time.monotonic()
        return True

    def _critical_escape_due(self, client: AtrinikClient, ratio: float,
                             nearby) -> bool:
        """Whether to run from ``nearby`` rather than fight or walk on.

        Only while she has neither a usable heal nor a ready recall
        (out_of_heals_and_recall), and an attacker is on her: she was hit in
        the last 3 s or one is within 2 tiles. It starts at the safety's flee
        threshold and, once started (``_escaping``), holds until she is above
        its heal threshold, so she does not turn back into the fight at the
        first point of health over it. Live (2026-11-22, v1.87.0) Sera,
        starving with no heal and no recall on the mana-crystal-upgrade
        errand, went on through the Old Outpost for three minutes from 69 %
        and died among its troll guards.
        """
        if (ratio > self.safety.heal_below or
                not out_of_heals_and_recall(client, self.safety)):
            self._escaping = False
            return False
        if not self._escaping and ratio > self.safety.flee_below:
            return False
        return bool(nearby) and (
            recent_hostile_contact(client, 3.0) or
            any(threat[0] <= 2 for threat in nearby))

    async def _unseen_attacker_retreat(self, client: AtrinikClient) -> bool:
        """Back away along the trail from an attacker she cannot see.

        An unlit square sends no object, so there is nothing to select or
        route around. Live: a spider in a dark two-wide corridor below Fort
        Sether held Sera on one square for an hour while her stalled steps
        learned and reset the same two blocks. Backing out draws an aggressive
        attacker into view, where the cornered rule fights it; defending keeps
        these steps from restarting the cornered clock.
        """
        m = client.state.map
        now = time.monotonic()
        if self._retreat_step_pending(client, now):
            return True
        node = getattr(self.navigation.graph, "nodes", {}).get(m.path)
        if node is None or len(self._trail) < 2:
            return False
        path, x, y = self._trail[-2]
        dx, dy = x - m.world_x, y - m.world_y
        direction = next((
            value for value, delta in c.DIRECTION_DELTAS.items()
            if value and delta == (dx, dy)), 0)
        if (path != m.path or not direction or
                (path, x, y) in self._retreat_blocked or
                (x, y) in node.occupied or
                not node.walkable(
                    x, y, allow_locked=self.navigation._allow_locked(client))):
            return False
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("navigation-unseen-attacker-retreat",
                     f"direction={direction} to={x},{y}")
        self._defending = True
        await client.clear_actions()
        await client.move(direction)
        self._send_retreat_step(client, x, y, now)
        self.navigation._issued_goal = None
        self.navigation._issued_click = None
        self.navigation._last_progress = now
        self._last_defense_action = now
        return True

    async def _combat_step_toward(
            self, client: AtrinikClient, view_x: int, view_y: int) -> bool:
        """Take one authored step toward a target and learn live blockers."""
        m = client.state.map
        now = time.monotonic()
        if self._combat_attempt is not None:
            path, x, y, sent_at = self._combat_attempt
            if (m.path, m.world_x, m.world_y) == (path, x, y):
                self._combat_attempt = None
            elif now - sent_at < 0.55:
                return True
            else:
                self._combat_blocked.add((path, x, y))
                self._combat_attempt = None
        target = (
            m.world_x + view_x - m.width // 2,
            m.world_y + view_y - m.height // 2,
        )
        try:
            path = self.navigation.graph.local_path(
                m.path, (m.world_x, m.world_y), target,
                allow_locked=self.navigation._allow_locked(client),
                excluded={(x, y) for map_path, x, y in self._combat_blocked
                          if map_path == m.path},
            )
        except (AttributeError, ValueError):
            path = []
        if len(path) < 2:
            return False
        dx = path[1][0] - m.world_x
        dy = path[1][1] - m.world_y
        direction = next((
            value for value, delta in c.DIRECTION_DELTAS.items()
            if delta == (dx, dy)), 0)
        if not direction:
            return False
        await client.move(direction)
        self._combat_attempt = (m.path, *path[1], now)
        return True

    def _halo_alone_closes_route(self, client: AtrinikClient,
                                 hard: set[tuple[int, int]]) -> bool:
        """Whether held-fire halos, beyond ``hard`` blocks, close the route.

        Learned stall blocks are lifted here: the navigator's own reset
        clears them only once a path exists without the halos, so counting
        them would leave each waiting on the other. On a seam edge this
        checks only the exact crossing, so a halo may yield slightly early
        where another seam square was still open.
        """
        navigation = self.navigation
        m = client.state.map
        if not navigation.route or navigation.route[0].source != m.path:
            return False
        edge = navigation.route[0]
        start = (m.world_x, m.world_y)
        allow_locked = navigation._allow_locked(client)
        base = navigation._temporary_blocked | hard
        if navigation.graph.local_path(
                m.path, start, (edge.x, edge.y), allow_locked=allow_locked,
                excluded=base | navigation._threat_blocked):
            return False
        return bool(navigation.graph.local_path(
            m.path, start, (edge.x, edge.y), allow_locked=allow_locked,
            excluded=base))

    def _halo_threats(self, client: AtrinikClient, threats, *, radius: int) -> None:
        """Mark the walkable squares around threats as temporarily blocked."""
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        node = getattr(self.navigation.graph, "nodes", {}).get(m.path)
        if node is None:
            return
        allow_locked = self.navigation._allow_locked(client)
        for _, view_x, view_y, _ in threats:
            world_x = m.world_x + view_x - cx
            world_y = m.world_y + view_y - cy
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    point = world_x + dx, world_y + dy
                    if (point != (m.world_x, m.world_y) and
                            node.walkable(*point, allow_locked=allow_locked)):
                        self.navigation._threat_blocked.add(point)

    async def _cornered_split(self, client: AtrinikClient, nearby):
        """The attacker to fight when a pack split has no retreat step.

        None while a step that gains distance exists (or one is in
        flight): the split retreats. With none she is cornered, and the closest attacker is
        fought (the farm's cornered breakout does the same): one beside
        her, or one farther off only while she is being hit; otherwise she
        holds (None) and lets them come to her, for at most
        CORNERED_HOLD_SECONDS, then fights the nearest, on for as long as she
        stays cornered and overwhelmed. The one she fights stays
        her target (``_cornered_target``) until it dies or is more than 4
        tiles off, so a square opening mid-fight, or the step she takes
        toward one farther off, does not turn her back into a combat-off
        retreat. Off her side it stays her target only while no other
        attacker is beside her and it is still hitting her (_hit_by).
        Live (2026-11-24, the Old Outpost): boxed in by walls on five sides beside two stone giants
        and a lava golem, she turned combat off twice a second and never
        swung.
        """
        latched = next((threat for threat in nearby
                        if threat[3].target_id == self._cornered_target),
                       None)
        if latched is not None and latched[0] > 1 and (
                any(threat[0] <= 1 for threat in nearby) or
                not self._hit_by(client, latched[3])):
            # Off her side, it stays her target only while nothing else is
            # beside her and it (its kind: hit text names no individual)
            # is still hitting her.
            latched = None
        if latched is None:
            self._cornered_target = 0
            # Cornered means no step that gains distance: an equal-distance
            # one only shuffles her beside the pack, combat off.
            if await self._retreat_step(
                    client, nearby, avoid_backtrack=True, gain_only=True,
                    probe=True):
                self._cornered_hold_since = 0.0
                return None
        target = latched or min(nearby, key=lambda threat: threat[0])
        if (latched is None and target[0] > 1 and
                not recent_hostile_contact(client, 3.0)):
            # Holding, combat off, to let them come; not for longer than
            # CORNERED_HOLD_SECONDS, or two that never approach would hold
            # her there for ever.
            now = time.monotonic()
            if not self._cornered_hold_since:
                self._cornered_hold_since = now
            if now - self._cornered_hold_since < self.CORNERED_HOLD_SECONDS:
                return None
            # An expired hold stays expired while she stays cornered and
            # overwhelmed: she fights on rather than hold another bound.
        if target[3].target_id != self._cornered_target:
            self._cornered_target = target[3].target_id
        if target[3].target_id not in self._cornered_logged:
            self._cornered_logged.add(target[3].target_id)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("navigation-cornered-fight",
                         f"target={target[3].target_id} nearby={len(nearby)}")
        return target

    @staticmethod
    def _hit_by(client: AtrinikClient, obj) -> bool:
        """Whether hit text of the last 3 s names this creature's kind;
        any hostile contact counts when none names a kind."""
        if not recent_hostile_contact(client, 3.0):
            return False
        attackers = recent_hostile_attackers(client, seconds=3.0)
        if not attackers:
            return True
        names = {FarmTask._kind_key(obj.name),
                 FarmTask._kind_key(map_object_visual_name(client, obj))}
        names.discard("")
        return any(FarmTask._kind_key(attacker) in names
                   for attacker in attackers)

    def _outclassed_ids(self, client: AtrinikClient, threats) -> set[int]:
        """Threats far below her level, which count toward no pack.

        One is outclassed when its authored level (the world graph's, for
        this map and the maps tiled to it) is known and more than
        OUTCLASSED_LEVEL_GAP below hers. An unknown level always counts, as
        does a monster whose spawn rerolls its level from the map's
        difficulty (WorldGraph.level_rerolled) and an authored caster of
        the map. OUTCLASSED_PACK or more of them within 4 tiles count
        again: none is outclassed then.
        Live (2026-11-24, the Old Outpost): Sera at level 37 shuffled for
        14 minutes between two squares beside four level 16 lava golems,
        and stood boxed in beside two level 17 stone giants, combat off,
        because two within four tiles made her split the pack; 100 of
        their 102 attacks missed.
        """
        player_level = int(client.state.stats.get("level", 0) or 0)
        resolver = getattr(self.navigation.graph, "monster_level", None)
        if resolver is None or player_level <= self.OUTCLASSED_LEVEL_GAP:
            return set()
        m = client.state.map
        graph = self.navigation.graph
        rerolled = getattr(graph, "level_rerolled", None)
        node = getattr(graph, "nodes", {}).get(m.path)
        # Casters of this map and of the maps tiled to it, as for levels.
        casters = area_caster_identities(node, tiled_neighbours(graph, node))
        outclassed = set()
        for threat in threats:
            obj = threat[3]
            visual = map_object_visual_name(client, obj)
            if ({WorldGraph._semantic_name(obj.name),
                 WorldGraph._semantic_name(visual)} & casters or
                    (rerolled is not None and
                     rerolled(obj.name, visual, map_path=m.path))):
                continue
            level = resolver(obj.name, visual, map_path=m.path)
            if 0 < level < player_level - self.OUTCLASSED_LEVEL_GAP:
                outclassed.add(obj.target_id)
        if sum(threat[0] <= 4 and threat[3].target_id in outclassed
               for threat in threats) >= self.OUTCLASSED_PACK:
            return set()
        return outclassed

    async def _defend_while_navigating(self, client: AtrinikClient,
                                       *, own_walk: bool = False) -> bool:
        """Pause an encounter approach to heal or clear an aggroed pack.

        ``own_walk``: called from this task's own tick, the only caller for
        which the critical escape (_critical_escape_due) runs. The farm
        circuit's legs and holds and the recovery phase's defence call it
        without.
        """
        if not self.combat_approach or not combat_allowed(client):
            # In recall mode (combat_allowed) the farm task only retreats.
            return False
        # Threat halos describe this viewport, not permanent terrain. Rebuild
        # them every defensive tick so killing a road pack opens its corridor.
        self.navigation._threat_blocked.clear()
        m = client.state.map
        position = (m.path, m.world_x, m.world_y)
        if position != self._last_position:
            self._last_position = position
            # Steps taken while defending (kiting, closing in, retreating)
            # are not route progress: they must not end the last resort.
            if not self._defending:
                self._last_moved_at = time.monotonic()
        cx, cy = m.width // 2, m.height // 2
        threats = []
        node = getattr(self.navigation.graph, "nodes", {}).get(m.path)
        destination_node = getattr(
            self.navigation.graph, "nodes", {}).get(
                self.navigation.destination)
        global_peaceful = getattr(
            self.navigation.graph, "peaceful_monster_identities", set())
        for x, y, obj in m.targets(friendly=False):
            if target_set_aside(client.state, obj.target_id):
                continue  # cannot be selected; still counted as a threat elsewhere
            if (isinstance(self.task, FarmTask) and
                    self.task.target_temporarily_unreachable(
                        obj.target_id) and
                    not recent_hostile_contact(client)):
                continue
            if (isinstance(self.task, FarmTask) and
                    m.path == self.navigation.destination and
                    not self.task.within_farm_map(client, x, y)):
                continue
            identities = (
                WorldGraph._semantic_name(obj.name),
                WorldGraph._semantic_name(
                    map_object_visual_name(client, obj)),
            )
            world_x = m.world_x + x - cx
            world_y = m.world_y + y - cy
            if (node is not None and
                    not (0 <= world_x < node.width and
                         0 <= world_y < node.height) and
                    not recent_hostile_contact(client)):
                # Tiled maps expose part of the neighboring map in the same
                # viewport. A creature there cannot be reached by this map's
                # local pathfinder; selecting it caused non-farm maintenance
                # travel to hold combat at 100% target HP indefinitely. The
                # adjacent map will classify and handle it after the seam is
                # crossed. Actual hit text still overrides this filter.
                continue
            peaceful = (node is not None and
                        any(identity in node.peaceful_identities
                            for identity in identities if identity)) or any(
                                identity in global_peaceful
                                for identity in identities if identity)
            destination_peaceful = (
                destination_node is not None and
                any(identity in destination_node.peaceful_identities
                    for identity in identities if identity))
            if peaceful and isinstance(self.task, FarmTask):
                # A matching species is requested only after reaching the
                # authored farm. Treating passive trees/animals on a transit
                # map as targets creates pack-avoid walls and route loops.
                if (m.path != self.navigation.destination or
                        self.task.ignore_unrequested_peaceful(
                            client, obj, node)):
                    continue
            if (destination_peaceful and isinstance(self.task, FarmTask) and
                    m.path != self.navigation.destination and
                    not recent_hostile_contact(client)):
                # A destination spawn can be visible across a tiled-map seam
                # while the player is still on the transit map. The current
                # node cannot classify that out-of-bounds object, and trying
                # to path to its foreign coordinate deadlocks the last route
                # edge. Treat the destination's authored passive identity as
                # authoritative until combat text proves it attacked.
                continue
            if peaceful and not isinstance(self.task, FarmTask):
                continue
            threats.append((max(abs(x - cx), abs(y - cy)), x, y, obj))
        if isinstance(self.task, FarmTask):
            self.task.observe_engaged_target(client, threats)
            if await self.task.restore_normal_weapon(
                    client, {entry[3].target_id for entry in threats}):
                return True
        committed_id = self._last_pull_target
        if isinstance(self.task, FarmTask) and self.task._engaged_target:
            committed_id = self.task._engaged_target[0]
        committed = next((entry for entry in threats
                          if entry[3].target_id == committed_id), None)
        nearest = committed or min(
            threats, key=lambda value: value[0], default=None)
        if committed_id and committed is None:
            self._last_pull_target = 0
        nearby = [threat for threat in threats if threat[0] <= 4]
        # Pack counts leave out creatures far below her (_outclassed_ids);
        # she still fights them, one at a time.
        outclassed = self._outclassed_ids(client, threats)
        pull_pack = [threat for threat in threats if threat[0] <= 6 and
                     threat[3].target_id not in outclassed]
        # The pack-split hold of the last tick, if this tick repeats it.
        split_held, self._split_hold = self._split_hold, None
        # Travel is not farming. Off the destination map, combat is the last
        # resort: only when something is hitting her and she has not moved
        # for a while (or has no route at all). Otherwise she keeps
        # walking and the route
        # steers around them (live: zero-XP spiders and bats below Fort
        # Sether kept her fighting and looting instead of travelling).
        # A care crossing (an essential trip across an owner block) holds
        # fire on every map until it ends, the destination map included;
        # the cornered last resort below still fights back.
        hold_fire = (
            (m.path != self.navigation.destination or
             self.navigation.care_holds_fire(m.path)) and
            not (recent_hostile_contact(client, 3.0) and
                 (self.navigation.status == TaskStatus.FAILED or
                  time.monotonic() - self._last_moved_at >=
                  self.TRANSIT_CORNERED_SECONDS)))
        combat_released = False
        if hold_fire:
            engaged = (self.task._engaged_target
                       if isinstance(self.task, FarmTask) else None)
            abandoned = self._last_pull_target or (engaged[0] if engaged else 0)
            if abandoned:
                # Drop the engagement entirely, wherever the creature is: a
                # kept FarmTask engagement would hold patrol and steer
                # retreats around a creature left behind on this map.
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("navigation-transit-hold-fire",
                             f"target={abandoned}")
                self._last_pull_target = 0
                if engaged is not None:
                    self.task._engaged_target = None
                if getattr(client.state, "combat", False):
                    await client.set_combat(False)
                    combat_released = True
                if getattr(client.state, "target_id", 0):
                    await client.clear_target()
        if await self._abandon_stalled_transit_target(client, committed):
            return True
        hp = client.state.stats.get("hp", 0)
        maxhp = max(1, client.state.stats.get("maxhp", 1))
        ratio = hp / maxhp
        # Without a verified area-control option, never begin or continue
        # fighting two nearby enemies. Split them before committing to one.
        overwhelmed = sum(threat[3].target_id not in outclassed
                          for threat in nearby) >= 2
        # A healthy character may reserve a deliberate training finisher
        # before a routine heal. FarmTask applies its stricter 80% health gate;
        # below that, survival immediately wins and normal melee remains free
        # to finish the enemy after healing.
        # On the farm's own map, spell-only training fights after safety
        # instead (see below). Transit keeps the last-resort fight.
        spells_only = (isinstance(self.task, FarmTask) and
                       m.path == self.navigation.destination and
                       self.task.spell_only_training(client))
        if (ratio > self.safety.flee_below and nearest is not None and
                not hold_fire and not spells_only and
                isinstance(self.task, FarmTask) and
                self.task.should_training_finish(client, nearest[3])):
            _, x, y, obj = nearest
            self.task.remember_engagement(client, x, y, obj.target_id)
            selected = await select_target(client, x, y, obj.target_id)
            if not selected:
                self.task.release_engagement(obj.target_id)
                await client.set_combat(False)
            if selected and await self.task.training_finisher(client, x, y, obj):
                self._defending = True
                self._last_defense_action = time.monotonic()
                return True
        heal_action = self.safety._heal_action
        last_food = self.safety._last_food
        safe = await self.safety.enforce(client)
        safety_action_issued = (
            self.safety._heal_action != heal_action or
            self.safety._last_food != last_food)
        escape_due = (own_walk and not isinstance(self.task, FarmTask) and
                      not safety_action_issued and
                      self._critical_escape_due(client, ratio, nearby))
        if (escape_due and
                await self._retreat_step(
                    client, nearby, avoid_backtrack=True, gain_only=True)):
            # Critical with no heal and no recall, and attacked: run, combat
            # off, starving or not (only resting is futile then). With no
            # step that gains distance the defence below stands.
            if not self._escaping:
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("navigation-critical-escape",
                             f"hp={hp}/{maxhp}: no usable heal, no ready "
                             "recall")
            self._escaping = True
            self._defending = True
            self._last_pull_target = 0
            if getattr(client.state, "combat", False):
                await client.set_combat(False)
            return True
        player_level = int(client.state.stats.get("level", 0) or 0)
        resolver = getattr(self.navigation.graph, "monster_level", None)
        dangerous = []
        if resolver is not None:
            for threat in threats:
                obj = threat[3]
                authored_level = resolver(
                    obj.name, map_object_visual_name(client, obj),
                    map_path=m.path)
                if authored_level >= player_level + 4:
                    dangerous.append(threat)
        if dangerous:
            danger_ids = {threat[3].target_id for threat in dangerous}
            manageable = [threat for threat in threats
                          if threat[3].target_id not in danger_ids]
            committed = next((
                entry for entry in manageable
                if entry[3].target_id == committed_id), None)
            nearest = committed or min(
                manageable, key=lambda value: value[0], default=None)
            nearby = [threat for threat in manageable if threat[0] <= 4]
            pull_pack = [threat for threat in manageable if threat[0] <= 6 and
                         threat[3].target_id not in outclassed]
            overwhelmed = sum(threat[3].target_id not in outclassed
                              for threat in nearby) >= 2
            # Visible high-level wildlife is removed from routine road combat.
            # Within likely approach range, its tile halo also becomes a live
            # path obstacle. Actual retreat is reserved for melee range or an
            # already-selected fight, avoiding impossible flee vectors between
            # harmless animals visible on opposite sides of the viewport.
            node = getattr(self.navigation.graph, "nodes", {}).get(m.path)
            for distance, view_x, view_y, _ in dangerous:
                if node is None or distance > 5:
                    continue
                world_x = m.world_x + view_x - cx
                world_y = m.world_y + view_y - cy
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        point = world_x + dx, world_y + dy
                        if (point != (m.world_x, m.world_y) and
                                node.walkable(
                                    *point,
                                    allow_locked=self.navigation._allow_locked(
                                        client))):
                            self.navigation._threat_blocked.add(point)
            closest_danger = min(dangerous, key=lambda value: value[0])
            danger_id = closest_danger[3].target_id
            if danger_id != self._last_danger_target:
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    level = resolver(
                        closest_danger[3].name,
                        map_object_visual_name(client, closest_danger[3]),
                        map_path=m.path)
                    identity = (closest_danger[3].name or
                                map_object_visual_name(
                                    client, closest_danger[3]))
                    recorder(
                        "navigation-danger-avoid",
                        f"{identity}: level {level} vs player {player_level}")
                self._last_danger_target = danger_id
            selected_danger = (
                bool(getattr(client.state, "combat", False)) and
                getattr(client.state, "target_id", 0) in danger_ids)
            if closest_danger[0] <= 2 or selected_danger:
                self._defending = True
                await client.clear_actions()
                await client.set_combat(False)
                if time.monotonic() - self._last_defense_action >= 0.30:
                    await self._retreat_step(
                        client, dangerous, avoid_backtrack=True)
                return True
        if not overwhelmed or not any(
                threat[3].target_id == self._cornered_target
                for threat in nearby):
            # A cornered fight ends with the pack split or its target.
            self._cornered_target = 0
        if not overwhelmed:
            self._cornered_hold_since = 0.0
        pack_offensive = (self.task.primary_combat_spell(client)
                          if isinstance(self.task, FarmTask) else None)
        if (safe and nearest is not None and nearest[0] > 2 and
                len(pull_pack) >= 2 and
                (pack_offensive is None or hold_fire) and
                not self._cornered_target):
            # A cornered fight at range is not turned into a pack avoidance
            # (combat off, replan) between its own ticks.
            if await self._bypass_pack_blocked_exit(client):
                return False
            node = getattr(self.navigation.graph, "nodes", {}).get(m.path)
            if node is not None:
                signature = tuple(sorted(
                    threat[3].target_id for threat in pull_pack))
                for _, view_x, view_y, _ in pull_pack:
                    world_x = m.world_x + view_x - cx
                    world_y = m.world_y + view_y - cy
                    # A one-tile halo still allowed the replanner to step onto
                    # a square only two tiles from one member, where the next
                    # safety tick immediately retreated and oscillated. Packs
                    # need a two-tile approach buffer; single threats retain
                    # the narrower handling in the branches above/below.
                    for dx in range(-2, 3):
                        for dy in range(-2, 3):
                            point = world_x + dx, world_y + dy
                            if (point != (m.world_x, m.world_y) and
                                    node.walkable(
                                        *point, allow_locked=
                                        self.navigation._allow_locked(client))):
                                self.navigation._threat_blocked.add(point)
                if signature != self._last_pack_signature:
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder(
                            "navigation-pack-avoid",
                            f"targets={','.join(map(str, signature))}")
                    self._last_pack_signature = signature
                await client.clear_actions()
                await client.set_combat(False)
                self.navigation.route = []
                self.navigation._issued_goal = None
                self.navigation._issued_click = None
                self.navigation._last_progress = time.monotonic()
                self._defending = False
                return False
        if (safe and nearest is not None and nearest[0] > 2 and
                len(pull_pack) >= 2 and not hold_fire and
                time.monotonic() - self._last_pull_at >= 2.5):
            offensive = pack_offensive
            if (offensive is not None and
                    self.task.offensive_spell_affordable(client, offensive)):
                _, x, y, obj = nearest
                await client.clear_actions()
                if isinstance(self.task, FarmTask):
                    self.task.remember_engagement(
                        client, x, y, obj.target_id)
                if not await select_target(client, x, y, obj.target_id):
                    if isinstance(self.task, FarmTask):
                        self.task.release_engagement(obj.target_id)
                    await client.set_combat(False)
                    return False
                direction = self.task.spell_fire_direction(
                    offensive, x, y, cx, cy)
                if direction is not None:
                    await client.fire(direction, offensive.tag)
                    self._last_pull_target = obj.target_id
                    self._last_pull_at = time.monotonic()
                    self._defending = True
                    self._last_defense_action = time.monotonic()
                    return True
        if (overwhelmed and pack_offensive is None and
                await self._bypass_pack_blocked_exit(client)):
            return False
        if not safe and safety_action_issued:
            # FIRE/APPLY was just sent by SafetyPolicy. Clearing in this
            # same tick cancels it, starving or not.
            return True
        # Safety's own hold (low health, a heal she cannot cast) is futile
        # too when she is starving with nothing to eat: only food helps.
        # A critical escape that found no step gaining distance skips it too:
        # its equal-distance steps shuffled her beside a troll, combat off,
        # and the cornered defence below fights instead.
        # A heal waiting for her action timer holds even then: a fight
        # below would restart the timer it waits for.
        safety_hold = not safe and (not starving_without_food(client) or
                                    heal_waiting(client))
        split = overwhelmed and not hold_fire and not safety_hold
        cornered = None
        if split and not escape_due:
            cornered = await self._cornered_split(client, nearby)
        if cornered is not None:
            if safety_action_issued:
                # A heal or meal just sent: the fight below would clear it.
                return True
            # Cornered (no retreat square): the fight below, at the
            # attacker beside her, rather than combat off every tick with
            # nowhere to go.
            nearest = cornered
        elif not escape_due and (safety_hold or split):
            if safety_action_issued:
                # FIRE/APPLY was just sent by SafetyPolicy. Clearing in this
                # same tick cancels it and creates an endless heal/food retry
                # loop at a fixed coordinate.
                return True
            self._defending = True
            hold = (m.path, m.world_x, m.world_y,
                    frozenset(threat[3].target_id for threat in nearby))
            if not split or split_held != hold:
                await client.clear_actions()
                await client.set_combat(False)
            elif getattr(client.state, "combat", False):
                # The same split hold as last tick: nothing to clear again,
                # and combat off only if something turned it back on.
                await client.set_combat(False)
            if split:
                self._split_hold = hold
            if nearby and time.monotonic() - self._last_defense_action >= 0.30:
                # A pack-splitting step must not immediately reverse on an
                # equal-distance plateau. Route following and the next safety
                # tick otherwise alternate forever between the same squares.
                # A split takes only a step that gains distance, the kind
                # whose absence makes her cornered (_cornered_split).
                await self._retreat_step(
                    client, nearby, avoid_backtrack=True, gain_only=split)
            return True
        if hold_fire and nearby:
            # Keep walking; mark their squares so the route steers around.
            # The halo is a preference: once it alone has closed the route
            # for a while, those creatures' halos yield and she walks past. Live:
            # a brown bat hovering over water beside a one-tile shore path
            # below Fort Sether held Sera for half an hour; it never hit her,
            # so the cornered rule never let her fight it either.
            # A group never yields.
            hard = set(self.navigation._threat_blocked)
            soft = [threat for threat in nearby
                    if threat[3].target_id not in self._halo_yield]
            self._halo_threats(client, soft, radius=1)
            now = time.monotonic()
            soft_ids = frozenset(threat[3].target_id for threat in soft)
            if not (soft and len(pull_pack) < 2 and
                    self._halo_alone_closes_route(client, hard)):
                self._halo_closed_since = 0.0
            elif (not self._halo_closed_since or
                    soft_ids != self._halo_closed_for):
                # The wait belongs to these creatures; a newcomer restarts it.
                self._halo_closed_since = now
                self._halo_closed_for = soft_ids
            elif now - self._halo_closed_since >= self.HALO_YIELD_SECONDS:
                yielded = {threat[3].target_id for threat in soft}
                self._halo_yield |= yielded
                self._halo_closed_since = 0.0
                self.navigation._threat_blocked.clear()
                self.navigation._threat_blocked.update(hard)
                soft = []
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("navigation-halo-yield",
                             f"targets={','.join(map(str, sorted(yielded)))}")
            if getattr(client.state, "combat", False) and not combat_released:
                await client.set_combat(False)
            m = client.state.map
            cx, cy = m.width // 2, m.height // 2
            # A yielded creature moving does not invalidate the plan.
            signature = tuple(sorted(
                (threat[3].target_id, m.world_x + threat[1] - cx,
                 m.world_y + threat[2] - cy) for threat in soft))
            if signature != self._hold_fire_signature:
                # A click path already in flight was planned without these
                # threats; cancel it so the next plan steers around them.
                self._hold_fire_signature = signature
                await client.clear_actions()
                self.navigation._issued_goal = None
                self.navigation._issued_click = None
            self._defending = False
            return False
        self._hold_fire_signature = ()
        self._halo_yield.clear()
        self._halo_closed_since = 0.0
        if (spells_only and nearest is not None and nearest[0] <= 4 and
                time.monotonic() - self._last_defense_action < 0.30):
            self._defending = True
            return True
        if spells_only and nearest is not None and nearest[0] <= 4:
            # Spell-only training never melees what it fights here: safety
            # has had its chance to heal, so cast; when the spell cannot be
            # cast, keep the distance with combat off. Only an adjacent
            # attacker she cannot step away from is fought below (survival).
            self._defending = True
            _, x, y, obj = nearest
            self.task.remember_engagement(client, x, y, obj.target_id)
            if not await select_target(client, x, y, obj.target_id):
                self.task.release_engagement(obj.target_id)
                await client.set_combat(False)
                return False
            if await self.task.training_finisher(client, x, y, obj):
                self._last_defense_action = time.monotonic()
                return True
            await client.clear_actions()
            await client.set_combat(False)
            spell = self.task.combat_spell_item(client)
            if not ((spell is not None and await self.task.caster_kite(
                    client, x, y, obj, spell)) or
                    nearest[0] > 1 or not recent_hostile_contact(client, 3.0)):
                # Survival: an adjacent attacker she can neither cast at nor
                # step away from is fought, here and now.
                await client.set_combat(True, force=True)
            self._last_defense_action = time.monotonic()
            return True
        # Only close a short gap. Chasing a merely visible target across the
        # viewport was what repeatedly pulled the character into larger packs.
        # After a critical escape she closes no gap until she has recovered;
        # one within two tiles that she cannot step away from is fought.
        if self._escaping and nearby:
            # Cornered in an escape: the closest attacker, not a committed
            # one farther off.
            nearest = min(nearby, key=lambda value: value[0])
        if (nearest is not None and nearest[0] <= 4 and
                not (self._escaping and nearest[0] > 2)):
            self._defending = True
            if (isinstance(self.task, FarmTask) and
                    self.task._engaged_target is not None and
                    self.task._engaged_target[0] == nearest[3].target_id and
                    await self.task.melee_kite(
                        client, nearest[1], nearest[2], nearest[3],
                        map_node=node)):
                self._last_defense_action = time.monotonic()
                return True
            if time.monotonic() - self._last_defense_action >= 0.30:
                _, x, y, obj = nearest
                await client.clear_actions()
                if self._last_pull_target != obj.target_id:
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder("navigation-target-commit",
                                 f"target={obj.target_id}")
                self._last_pull_target = obj.target_id
                if isinstance(self.task, FarmTask):
                    self.task.remember_engagement(
                        client, x, y, obj.target_id)
                if not await select_target(client, x, y, obj.target_id):
                    if isinstance(self.task, FarmTask):
                        self.task.release_engagement(obj.target_id)
                    await client.set_combat(False)
                    return False
                semantic = f"{obj.name} {map_object_visual_name(client, obj)}"
                force = bool(
                    isinstance(self.task, FarmTask) and
                    self.task.target_pattern and
                    self.task.target_pattern.search(semantic))
                await client.set_combat(True, force=force)
                if nearest[0] > 1:
                    await self._combat_step_toward(client, x, y)
                self._last_defense_action = time.monotonic()
            return True
        if (not nearby and not hold_fire and
                not any(threat[0] <= 5 for threat in dangerous) and
                client.state.map.path != self.navigation.destination):
            # Hit and cornered with nothing close enough to fight: the
            # attacker is unseen. Between retreat steps the defence holds;
            # the recovery branch below would cancel the step in flight.
            if (self._defending and
                    time.monotonic() - self._last_defense_action < 0.30):
                return True
            if await self._unseen_attacker_retreat(client):
                return True
        if self._defending:
            maxsp = int(client.state.stats.get("maxsp", 0) or 0)
            sp = int(client.state.stats.get("sp", 0) or 0)
            # Starving with nothing to eat, resting is futile: hunger drains
            # health faster than she recovers it, so she keeps walking, to
            # food above all. Live: Sera held four squares from Saruthar with
            # food 0 until starvation killed her. With food on her, safety
            # eats it and the hold stands.
            recovered = starving_without_food(client) or (
                ratio > self.safety.heal_below and
                (maxsp <= 0 or sp / maxsp >= 0.80))
            if not recovered:
                if getattr(client.state, "combat", False):
                    await client.set_combat(False)
                if not self._recovering_after_combat:
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder(
                            "navigation-recovery-hold",
                            f"hp={hp}/{maxhp} sp={sp}/{maxsp}")
                self._recovering_after_combat = True
                return True
            await client.move(0, run=False)
            await client.set_combat(False)
            await client.clear_actions()
            # The clear may cancel a retreat step in flight; it must not be
            # learned later as a blocked square.
            self._retreat_attempt = None
            self.navigation._issued_goal = None
            self.navigation._issued_click = None
            self.navigation._last_progress = time.monotonic()
            self._defending = False
            self._recovering_after_combat = False
            return True
        return False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if client.state.phase != "playing":
            return
        self._record_trail(client)
        # Noted before a defence clears the route (_way_out).
        self._way_out(client)
        # Her trip stays under way while a fight or a recovery hold keeps
        # its navigation from ticking: what happens meanwhile is the route's.
        route_events.note_hold(client, self.navigation)
        # Zone containment comes before defensive pursuit. Otherwise a farm
        # can chase one retreating monster across a connected-map seam, then
        # keep fighting there forever before the return route gets a tick.
        if (self.navigation.status == TaskStatus.COMPLETE and
                client.state.map.path != self.navigation.destination):
            await client.clear_actions()
            await client.set_combat(False)
            self.navigation.status = TaskStatus.RUNNING
            self.navigation.route = []
            self.navigation._issued_goal = None
            self.navigation._issued_click = None
            self.navigation._last_progress = time.monotonic()
            self._defending = False
            await self.navigation.tick(client)
            return
        # Once the authored destination is reached, the child owns behavior.
        # Letting the travel wrapper keep intercepting farm combat prevented
        # FarmTask pull and emergency-retreat policies from ever running.
        if (self.navigation.status == TaskStatus.COMPLETE and
                client.state.map.path == self.navigation.destination):
            await self.task.tick(client)
            if self.task.status == TaskStatus.FAILED:
                self.fail(self.task.error)
            elif self.task.status == TaskStatus.COMPLETE:
                self.complete()
            return
        # Finish a corpse before pulling another distant group. An immediate
        # enemy still has priority, and FarmTask itself refuses trapped-corpse
        # work below half health.
        if isinstance(self.task, FarmTask):
            m = client.state.map
            cx, cy = m.width // 2, m.height // 2
            immediate = any(
                max(abs(x - cx), abs(y - cy)) <= 2
                for x, y, _ in m.targets(friendly=False))
            if not immediate and await self.task.loot_nearby(client):
                return
            if not immediate:
                corpses = []
                for (view_x, view_y), tile in m.tiles.items():
                    distance = max(abs(view_x - cx), abs(view_y - cy))
                    if not 1 < distance <= 6:
                        continue
                    if not any(
                            "corpse" in
                            f"{obj.name} {map_object_visual_name(client, obj)}".casefold()
                            for obj in tile.objects.values()):
                        continue
                    world = (
                        m.path,
                        m.world_x + view_x - cx,
                        m.world_y + view_y - cy,
                    )
                    node = self.navigation.graph.nodes.get(m.path)
                    if node is not None and not (
                            0 <= world[1] < node.width and
                            0 <= world[2] < node.height):
                        continue
                    if not corpse_tile_standable(node, m.path, world[1], world[2]):
                        # Corpses in water cannot be stood on to loot.
                        continue
                    if world in self.task._ignored_corpse_tiles:
                        continue
                    corpses.append((distance, view_x, view_y))
                if corpses:
                    _, view_x, view_y = min(corpses)
                    await client.clear_actions()
                    if await self._combat_step_toward(
                            client, view_x, view_y):
                        return
        if await self._defend_while_navigating(client, own_walk=True):
            return
        if (isinstance(self.task, FarmTask) and
                await self.task.loot_nearby(client)):
            return
        if (self.navigation.status == TaskStatus.COMPLETE and
                client.state.map.path != self.navigation.destination):
            self.navigation.status = TaskStatus.RUNNING
            self.navigation.route = []
            self.navigation._issued_goal = None
            self.navigation._issued_click = None
        if self.navigation.status != TaskStatus.COMPLETE:
            await self.navigation.tick(client)
            if self.navigation.status == TaskStatus.FAILED:
                if self.combat_approach and recent_hostile_contact(client, 3.0):
                    # No route while something is hitting her: stay alive so
                    # the next defensive tick can fight back as a last resort;
                    # the failure surfaces once the contact ends.
                    return
                self.fail(self.navigation.error)
            return
        await self.task.tick(client)
        if self.task.status == TaskStatus.FAILED:
            self.fail(self.task.error)
        elif self.task.status == TaskStatus.COMPLETE:
            self.complete()


class ShopUpgradeSweepTask(BotTask):
    """Visit authored stock tiles until one safely budgeted upgrade is found."""

    def __init__(self, graph: WorldGraph, maps: Sequence[str], *,
                 allow_launchers: bool = True,
                 allow_hostile_transit: bool = False,
                 start_index: int = 0,
                 caster_stats: bool = False):
        super().__init__("shop-upgrade-sweep")
        self.graph = graph
        self.waypoints = [
            (path, point) for path in maps
            for point in graph.shop_stocks.get(path, ())
            if path in graph.nodes and graph.nodes[path].walkable(*point)
        ]
        self.index = min(max(0, int(start_index)), len(self.waypoints))
        self.child: NavigateThenTask | None = None
        self.settlement: NavigateTask | None = None
        self._settlement_inherited = False
        self._settlement_complete_at = 0.0
        self.purchased = False
        self.allow_launchers = bool(allow_launchers)
        self.allow_hostile_transit = bool(allow_hostile_transit)
        # Value stock as a Wizardry caster would (see InventoryPolicy).
        self.caster_stats = bool(caster_stats)

    def _new_child(self) -> NavigateThenTask:
        path, point = self.waypoints[self.index]
        child = NavigateThenTask(
            self.graph, path, BuyShopUpgradeTask(
                allow_launchers=self.allow_launchers,
                caster_stats=self.caster_stats),
            destination_xy=point, combat_approach=True,
            # Regional stock is reached through one authored level-14/15
            # crocodile/frog road. At level 18+, permit the live defensive
            # navigator to split, clear, or retreat around that chokepoint;
            # otherwise the coarse three-spawn map gate silently skips every
            # Asteria weapon and armour tile.
            safety=SafetyPolicy(heal_below=0.85, flee_below=0.70))
        child.navigation.allow_ranged_hazard_fallback = (
            self.allow_hostile_transit)
        return child

    @staticmethod
    def _unpaid_inventory(client: AtrinikClient) -> list[Item]:
        return [item for item in client.state.inventory
                if item.flags & c.ITEM_UNPAID]

    def _new_settlement(self, client: AtrinikClient) -> NavigateTask | None:
        """Route across a shop mat so acquired stock becomes paid."""
        path = client.state.map.path
        start = client.state.map.world_x, client.state.map.world_y
        node = self.graph.nodes.get(path)
        if node is None:
            return None
        source_component = self.graph._component(path, start)
        choices: list[tuple[int, tuple[int, int], NavigateTask]] = []
        for edge in node.edges:
            if (edge.source != path or edge.destination != path or
                    edge.kind != "exit"):
                continue
            transition = self.graph._transition(edge, start)
            if transition is None:
                continue
            _, arrival = transition
            arrival_component = self.graph._component(path, arrival)
            if not arrival_component or arrival_component == source_component:
                continue
            ordinary = [
                point for point in arrival_component
                if point not in node.automatic_exits and node.walkable(*point)
            ]
            goal = min(
                ordinary or [arrival],
                key=lambda point: (
                    max(abs(point[0] - arrival[0]),
                        abs(point[1] - arrival[1])), point))
            navigation = NavigateTask(self.graph, path, goal)
            navigation.allow_ranged_hazard_fallback = (
                self.allow_hostile_transit)
            try:
                route = navigation._plan(client)
            except ValueError:
                continue
            choices.append((len(route), goal, navigation))
        return min(choices, key=lambda value: (value[0], value[1]))[2] \
            if choices else None

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        unpaid = self._unpaid_inventory(client)
        # A process can restart after pickup but before crossing the shop mat.
        # Reconcile that inherited checkout before inspecting another tile;
        # otherwise unpaid stock is excluded from the owned-item baseline and
        # the sweep can buy duplicates of the same slot.
        if self.settlement is None and self.child is None and unpaid:
            self.settlement = self._new_settlement(client)
            self._settlement_inherited = True
            self._settlement_complete_at = 0.0
            if self.settlement is None:
                self.fail("unpaid shop stock has no reachable checkout mat")
                return
        if self.settlement is not None:
            await self.settlement.tick(client)
            if self.settlement.status == TaskStatus.FAILED:
                self.fail("shop payment settlement failed: " +
                          self.settlement.error)
                return
            if self.settlement.status != TaskStatus.COMPLETE:
                return
            if self._unpaid_inventory(client):
                if not self._settlement_complete_at:
                    self._settlement_complete_at = time.monotonic()
                elif time.monotonic() - self._settlement_complete_at > 5.0:
                    self.fail("shop stock remained unpaid after checkout")
                return
            inherited = self._settlement_inherited
            self.settlement = None
            self._settlement_inherited = False
            self._settlement_complete_at = 0.0
            if not inherited:
                self.purchased = True
                self.complete()
                return
        if not self.waypoints or self.index >= len(self.waypoints):
            self.complete()
            return
        if self.child is None:
            self.child = self._new_child()
        await self.child.tick(client)
        if self.child.status == TaskStatus.FAILED:
            log.warning("shop stock waypoint skipped: %s", self.child.error)
            self.index += 1
            self.child = None
        elif self.child.status == TaskStatus.COMPLETE:
            buyer = self.child.task
            # Stop after one purchase, but only after physically crossing the
            # shop mat and observing ITEM_UNPAID clear. A delay alone neither
            # settles payment nor makes the item a valid owned baseline.
            if isinstance(buyer, BuyShopUpgradeTask) and buyer._move_at:
                if not self._unpaid_inventory(client):
                    self.purchased = True
                    self.complete()
                    return
                self.settlement = self._new_settlement(client)
                self._settlement_inherited = False
                self._settlement_complete_at = 0.0
                self.child = None
                if self.settlement is None:
                    self.fail("purchased shop stock has no reachable checkout mat")
                return
            self.index += 1
            self.child = None
        if self.index >= len(self.waypoints):
            self.complete()


class MorgeeanShipKeyTask(BotTask):
    """Ask Morg'eean for permission, then take his free spare ship key."""

    MAP = "/shattered_islands/world_6_51"
    SAFE_RETURN_MAP = "/shattered_islands/world_2_70"
    KEY = re.compile(r"^Morg'eean's Ship Key$", re.I)

    def __init__(self, graph: WorldGraph):
        super().__init__("acquire:Morg'eean's Ship Key")
        self.graph = graph
        self.navigation = NavigateTask(graph, self.MAP, (15, 16), tolerance=2)
        self.chest_navigation: NavigateTask | None = None
        self.return_navigation: NavigateTask | None = None
        # An essential trip (mark_essential) marks the walks it starts later.
        self.essential = ""
        self.talked_at = 0.0
        self.opened: set[int] = set()
        self.last_action = 0.0

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if any(self.KEY.search(item.name) for item in client.state.inventory):
            # Do not hand control to the next maintenance task while still on
            # the crocodile-populated island. The newly unlocked dock gives a
            # two-transition ship route straight back to Brynknot.
            if client.state.map.path == self.SAFE_RETURN_MAP:
                self.complete()
                return
            if self.return_navigation is None:
                self.return_navigation = NavigateTask(
                    self.graph, self.SAFE_RETURN_MAP, (5, 12))
                self.return_navigation.essential = self.essential
            await self.return_navigation.tick(client)
            if self.return_navigation.status == TaskStatus.FAILED:
                self.fail(self.return_navigation.error)
            return
        if self.navigation.status != TaskStatus.COMPLETE:
            await self.navigation.tick(client)
            if self.navigation.status == TaskStatus.FAILED:
                self.fail(self.navigation.error)
            return
        if not self.talked_at:
            await client.talk("Can I use your ship?", "Morg'eean")
            self.talked_at = time.monotonic()
            return
        if time.monotonic() - self.talked_at < 0.75:
            return
        if self.chest_navigation is None:
            self.chest_navigation = NavigateTask(
                self.graph, self.MAP, (15, 14))
            self.chest_navigation.essential = self.essential
        if self.chest_navigation.status != TaskStatus.COMPLETE:
            await self.chest_navigation.tick(client)
            if self.chest_navigation.status == TaskStatus.FAILED:
                self.fail(self.chest_navigation.error)
            return
        if time.monotonic() - self.last_action < 0.5:
            return
        key = next((item for item in client.state.items.values()
                    if item.location in self.opened and
                    self.KEY.search(item.name)), None)
        if key is not None:
            await client.move_item(
                client.state.player_tag, key.tag, key.quantity)
            self.last_action = time.monotonic()
            return
        container = next((
            item for item in client.state.ground
            if item.item_type == c.TYPE_CONTAINER or
            "chest" in item.name.casefold()), None)
        if container is None:
            self.fail("Morg'eean's goods chest is not visible")
            return
        await client.apply(container.tag)
        self.opened.add(container.tag)
        self.last_action = time.monotonic()


class BuySpellTask(BotTask):
    """Buy one exact permanent spell from an authored spell seller."""

    def __init__(self, npc: str, spell: str, *, cost: int = 0):
        super().__init__(f"buy-spell:{spell}")
        self.npc = npc
        self.spell = spell
        self.cost = max(0, int(cost))
        self.sent_at = 0.0
        self._carried_before = 0
        self._bank_before: int | None = None
        self._accounted = False

    def known(self, client: AtrinikClient) -> bool:
        return any(item.item_type == c.TYPE_SPELL and
                   item.name.casefold() == self.spell.casefold()
                   for item in client.state.inventory)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self.known(client):
            if self.sent_at and not self._accounted and self.cost:
                bank_spend = max(0, self.cost - self._carried_before)
                if self._bank_before is not None and bank_spend:
                    balance = max(0, self._bank_before - bank_spend)
                    setter = getattr(client, "set_bank_balance", None)
                    if setter is not None:
                        # Local accounting keeps the last observation time.
                        setter(balance, observed_at=float(getattr(
                            client.state, "bank_balance_observed_at", 0.0) or 0.0))
                    else:
                        client.state.bank_balance = balance
                self._accounted = True
            self.complete()
            return
        if not self.sent_at:
            self._carried_before = \
                BuyShopUpgradeTask.carried_wallet_value(client)
            if getattr(client.state, "bank_balance_known", False):
                self._bank_before = int(client.state.bank_balance)
            await client.talk(f"buy {self.spell}", self.npc)
            self.sent_at = time.monotonic()
            return
        if time.monotonic() - self.sent_at >= 3.0:
            self.fail(f"could not buy {self.spell}; check funds and seller")


_MONSTER_LABEL = re.compile(r"\s+L\d+(?:\u00d7\d+)?$")


def analyzer_spot_legs(spot: dict, graph: "WorldGraph") -> tuple[tuple[str, str], ...]:
    """Circuit legs for one analyzer result, or () if any map is unknown."""
    legs = []
    names = sorted({
        _MONSTER_LABEL.sub("", str(label)).strip()
        for label in spot.get("monsters") or ()} - {""})
    # Escape regex syntax but keep spaces readable in logs and the dashboard.
    target = "|".join(re.escape(name).replace("\\ ", " ") for name in names)
    for raw in spot.get("map_paths") or ():
        path = "/" + str(raw).removeprefix("maps/").lstrip("/")
        if path not in graph.nodes:
            return ()
        legs.append((path, target))
    return tuple(legs)


def depleted_for_temple(state) -> bool:
    """Depleted at the temple-service threshold, by the live depletion force.

    The counter can outlive a cure applied elsewhere (an item, or while
    offline); only the live force proves she is still depleted.
    """
    depleted = any(item.item_type == c.TYPE_FORCE and
                   item.name.casefold() == "depletion"
                   for item in getattr(state, "inventory", ()))
    return bool(depleted and getattr(state, "depletion_points_known", False) and
                int(getattr(state, "depletion_points", 0) or 0) >=
                FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD)


def effective_farm_objective(state) -> str:
    """The objective farming follows right now.

    While depletion is at the temple-service threshold, its lowered stats
    make the chosen objective's spots deadly, so farming prefers the safest
    spots until the temple removes it; then the choice applies again.
    """
    chosen = getattr(state, "farm_objective", DEFAULT_OBJECTIVE)
    return "safety" if depleted_for_temple(state) else chosen


# RECALL_REQUIRED_LEVEL (tasks.py): from this level recall shopping buys a
# recall source when she can afford one (FarmCircuitTask._needs_recall_shopping);
# below it nothing would.
# Where recall shopping keeps the latest recall quote she was given
# (client.state.vendor_quotes): the cheapest one the merchant offered.
RECALL_QUOTE_KEY = "recall-source"


def _recall_sources(client: AtrinikClient) -> tuple[bool, bool]:
    """Whether she owns a recall source, and whether one of them works now.

    A source is what FarmTask.emergency_recall_candidates fires, read
    without its mana check: a known Word of Recall spell, or a paid,
    identified, uncursed rod or wand. A spell costing more SP than her
    maximum is none (as in care_recall_ready): she could never cast it. A
    source works unless it is a wand known to be empty
    (observe_wand_feedback) or a device waiting out
    RECALL_DEVICE_RETRY_SECONDS after it said it cannot fire. A wand of
    unknown charge works until its examine text says otherwise.
    """
    retry = getattr(client, "recall_device_retry_at", {}) or {}
    now = time.monotonic()
    policy = InventoryPolicy()
    maxsp = int((getattr(client.state, "stats", {}) or {}).get("maxsp", 0) or 0)
    owned = False
    for item in getattr(client.state, "inventory", ()):
        if (not re.search(r"\bword of recall\b", item.name, re.I) or
                item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)):
            continue
        if item.item_type == c.TYPE_SPELL:
            if int(item.extra.get("cost", 0) or 0) > maxsp:
                continue
            return True, True
        if (item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and
                policy.identified(item)):
            owned = True
            if (not wand_known_empty(client, item) and
                    float(retry.get(item.tag, 0.0)) <= now):
                return True, True
    return owned, False


def has_working_recall(client: AtrinikClient) -> bool:
    """Whether a recall source of hers works now (_recall_sources)."""
    return _recall_sources(client)[1]


def recall_quote(client: AtrinikClient) -> tuple[str, int] | None:
    """The recall source recall shopping was last quoted, and its price:
    the cheapest stock the merchant offered (RECALL_QUOTE_KEY), while it is
    younger than FarmCircuitTask.RECALL_SHOP_INTERVAL. An older quote is
    no price: merchant stock changes, and the next trip finds out."""
    quote = (getattr(client.state, "vendor_quotes", {}) or {}).get(
        RECALL_QUOTE_KEY) or {}
    cost = quote.get("unit_cost")
    observed = float(quote.get("observed_at", 0.0) or 0.0)
    if (not isinstance(cost, (int, float)) or cost <= 0 or
            time.time() - observed >= FarmCircuitTask.RECALL_SHOP_INTERVAL):
        return None
    return str(quote.get("item", "") or "recall source"), int(cost)


def recall_alerts(client: AtrinikClient) -> list[dict[str, str]]:
    """The dashboard's recall note: from RECALL_REQUIRED_LEVEL, while no
    recall source of hers works.

    A working emergency recall is never a blocker (owner decision,
    2026-11-22): she farms on without one, and recall shopping buys one
    once she can afford it. So none of these is critical:

    - recall-unfunded (warning): the recall she was quoted within a day
      (recall_quote), plus the reserve recall shopping keeps, costs more
      than she has;
    - recall-unavailable (info): the last recall shopping failed or bought
      nothing, or getting the ship key it needs failed, within a day
      (GameState.recall_unavailable);
    - recall-missing (info): otherwise.
    """
    level = int((getattr(client.state, "stats", {}) or {}).get("level", 0) or 0)
    if level < RECALL_REQUIRED_LEVEL or has_working_recall(client):
        return []
    tail = "; she farms on without one"
    wallet = BuyShopUpgradeTask.wallet_value(client)
    reserve = FarmCircuitTask.RECALL_SHOP_RESERVE
    quote = recall_quote(client)
    if quote is not None and wallet < quote[1] + reserve:
        item, cost = quote
        return [{"id": "recall-unfunded", "level": "warn",
                 "text": "No working recall: buying one needs "
                         f"{format_money(cost + reserve)} ({item} at "
                         f"{format_money(cost)} plus a {format_money(reserve)} "
                         f"reserve), she has {format_money(wallet)}" + tail}]
    note = getattr(client.state, "recall_unavailable", "") or ""
    at = float(getattr(client.state, "recall_unavailable_at", 0.0) or 0.0)
    if note and time.time() - at < FarmCircuitTask.RECALL_SHOP_INTERVAL:
        return [{"id": "recall-unavailable", "level": "info",
                 "text": f"No working recall: {note}" + tail}]
    return [{"id": "recall-missing", "level": "info",
             "text": "No working recall: recall shopping buys one when she "
                     "can afford it" + tail}]


# Where the heal restock keeps the cheapest healing price it was last
# given (client.state.vendor_quotes), for heal_restock_quote.
HEAL_QUOTE_KEY = "heal-restock"
# Compiled merchant stock names are archetype names ("scroll min heal").
_HEAL_STOCK_ARCH = re.compile(r"^(?:potion|balm|scroll)\b.*\b(?:heal|wounds)",
                              re.I)


def heal_restock_vendors(graph: "WorldGraph", client: AtrinikClient
                         ) -> list[tuple[DialogueVendor, bool]]:
    """Dialogue merchants that may sell her a healing consumable, each
    with whether its authored stock holds one she can use (a scroll only
    while she reads scrolls); the others roll random potions
    (``random_potion``), which can be balms of healing."""
    readable = reads_scrolls(client)
    found = []
    for vendor in graph.dialogue_vendors:
        fixed = any(
            _HEAL_STOCK_ARCH.search(name) and
            (readable or not name.casefold().startswith("scroll"))
            for name in vendor.stock)
        rolled = any(value.casefold() == "random_potion"
                     for value in vendor.treasure_lists)
        if fixed or rolled:
            found.append((vendor, fixed))
    return found


def heal_restock_quote(client: AtrinikClient) -> tuple[str, int] | None:
    """The cheapest healing item the heal restock was last quoted, and its
    price, while younger than FarmCircuitTask.HEAL_RESTOCK_INTERVAL."""
    quote = (getattr(client.state, "vendor_quotes", {}) or {}).get(
        HEAL_QUOTE_KEY) or {}
    cost = quote.get("unit_cost")
    observed = float(quote.get("observed_at", 0.0) or 0.0)
    if (not isinstance(cost, (int, float)) or cost <= 0 or
            time.time() - observed >= FarmCircuitTask.HEAL_RESTOCK_INTERVAL):
        return None
    return str(quote.get("item", "") or "healing item"), int(cost)


def heal_restock_alerts(client: AtrinikClient) -> list[dict[str, str]]:
    """The dashboard's heal-restock note: while the heal restock backs off
    (GameState.heal_restock_note) and she still carries fewer usable
    healing consumables than FarmCircuitTask.HEAL_RESTOCK_LOW. Info only:
    she farms on, and rests rather than pulls while unprepared."""
    state = client.state
    note = getattr(state, "heal_restock_note", "") or ""
    until = float(getattr(state, "heal_restock_retry_until", 0.0) or 0.0)
    now = time.time()
    if (not note or until <= now or
            heal_consumables(client) >= FarmCircuitTask.HEAL_RESTOCK_LOW):
        return []
    minutes = max(1, int((until - now) // 60))
    return [{"id": "heal-restock-unavailable", "level": "info",
             "text": f"No healing potions: {note}; next try in "
                     f"{minutes // 60}h {minutes % 60:02d}m"}]


def farming_unsafe_reason(client: AtrinikClient) -> str:
    """Why farming must wait, or "" while it is safe enough to farm.

    One condition, which kept farming going into a death spiral live
    (2026-11-17: ten deaths in under three hours, depletion 68 to 92):
    depletion at the temple-service threshold (depleted_for_temple). That
    is where the circuit already holds that the lowered stats make the
    chosen spots deadly (effective_farm_objective). Every death adds a
    point and the fee does not grow with them, so farming on to pay the
    fee only adds deaths.

    While it holds, autoplay runs its recovery phase (RecoveryTask)
    instead of farming: only food, the bank, the cure and its funding,
    and otherwise a wait at her savebed.

    A working emergency recall is not a condition (owner decision,
    2026-11-22, superseding the recall half of 2026-11-17): live, on
    2026-11-22, both her recall wands were empty, the cheapest recall cost
    more than she had, and with farming suspended she could not earn it.
    She farms without one; recall shopping buys one when she can afford it
    (FarmCircuitTask._needs_recall_shopping), and recall_alerts says so.

    An unaffordable cure is not a blocker either (owner decision,
    2026-11-23): once the cure-funding path has nothing left to sell,
    RecoveryTask starts a cure-funding farm (cure_funding_farm), and while
    it runs this returns "".
    """
    state = client.state
    if depleted_for_temple(state) and not cure_funding_farm(client):
        return (f"depletion {int(state.depletion_points)} at or above "
                f"{FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD}" +
                ("; home for errands from the cure-funding farm"
                 if cure_funding_farm_home(client) else ""))
    return ""


# The cure-funding farm (owner decision, 2026-11-23: an unaffordable cure
# must never block her). Live, on 2026-11-23 (v1.92.0), depletion 7 held
# farming, the cure cost 2,865 copper against her 2,245, the chest trip that
# would have funded it failed, and she parked at her savebed for good.
# RecoveryTask starts it when depletion is due, the cure is short and the
# funding path (identify-all, junk sale, deposit, the apartment chest) has
# nothing left to start. Its state is persisted character memory
# (GameState.cure_funding_farm_*), so it survives Stop and restarts; the
# decision records are its audit trail. It pauses while the cure is
# affordable (funded) and ends once the cure is done (ended), or after
# CURE_FUNDING_FARM_DEATHS deaths (backoff): she parks and alerts again,
# and may start another only after CURE_FUNDING_FARM_BACKOFF_SECONDS.
CURE_FUNDING_FARM_STARTED = "cure-funding-farm"
CURE_FUNDING_FARM_ENDED = "cure-funding-farm-ended"
CURE_FUNDING_FARM_BACKOFF = "cure-funding-farm-backoff"
CURE_FUNDING_FARM_FUNDED = "cure-funding-farm-funded"
CURE_FUNDING_FARM_DEATHS = 3
CURE_FUNDING_FARM_BACKOFF_SECONDS = 24 * 3600.0
# An affordable cure keeps the farm (and its deaths) this long for the
# temple to cure her; after that, uncured, it ends.
CURE_FUNDING_FARM_KEEP_SECONDS = 24 * 3600.0


@dataclass(frozen=True, slots=True)
class CureFundingFarm:
    """The cure-funding farm's state (cure_funding_farm_state).

    ``state`` is "farming" (since ``since``, with ``deaths`` since then),
    "backoff" (no new start before ``retry_at``, wall-clock) or ""."""

    state: str = ""
    since: float = 0.0
    deaths: int = 0
    retry_at: float = 0.0


def _deaths_after(client: AtrinikClient, since: float,
                  after: float) -> tuple[int, float]:
    """Deaths in her decision history at or after ``since`` and later than
    ``after``, and the time of the latest (``after`` when none)."""
    deaths, latest = 0, after
    for record in reversed(getattr(client, "decision_history", ()) or ()):
        if not isinstance(record, dict):
            continue
        try:
            at = float(record.get("time", 0.0) or 0.0)
            count = max(1, int(record.get("count", 1) or 1))
        except (TypeError, ValueError, OverflowError):
            continue
        if at < since or at <= after:
            break
        if record.get("action") == "death":
            deaths += count
            latest = max(latest, at)
    return deaths, latest


def cure_funding_farm_state(client: AtrinikClient) -> CureFundingFarm:
    """The cure-funding farm's persisted state, with the deaths since it
    started: the persisted count, plus the deaths recorded after the time
    it counted up to (settle_cure_funding_farm folds those in, before the
    bounded history drops them). A farm whose cure became affordable
    CURE_FUNDING_FARM_KEEP_SECONDS ago without being cured is no farm."""
    state = client.state
    since = float(getattr(state, "cure_funding_farm_since", 0.0) or 0.0)
    if since > 0.0:
        funded_at = float(getattr(state, "cure_funding_farm_funded_at", 0.0)
                          or 0.0)
        if funded_at and time.time() - funded_at >= CURE_FUNDING_FARM_KEEP_SECONDS:
            return CureFundingFarm()
        counted, _ = _deaths_after(client, since, float(getattr(
            state, "cure_funding_farm_counted_at", 0.0) or 0.0))
        deaths = int(getattr(state, "cure_funding_farm_deaths", 0) or 0) + counted
        return CureFundingFarm("farming", since, deaths)
    retry_at = float(getattr(state, "cure_funding_farm_retry_until", 0.0) or 0.0)
    if retry_at > time.time():
        return CureFundingFarm("backoff", 0.0, 0, retry_at)
    return CureFundingFarm()


_CURE_FUNDING_FARM_FIELDS = ("since", "deaths", "counted_at", "funded_at",
                             "retry_until")


def _set_cure_funding_farm(client: AtrinikClient, **changes) -> None:
    """Persist the cure-funding farm's fields: the ``changes``, the rest as
    they are."""
    state = client.state
    values = {name: getattr(state, f"cure_funding_farm_{name}", 0)
              for name in _CURE_FUNDING_FARM_FIELDS}
    values.update(changes)
    setter = getattr(client, "set_cure_funding_farm", None)
    if setter is not None:
        setter(**values)
        return
    for name, value in values.items():
        setattr(state, f"cure_funding_farm_{name}", value)


def _end_cure_funding_farm(client: AtrinikClient, **changes) -> None:
    _set_cure_funding_farm(client, **{
        "since": 0.0, "deaths": 0, "counted_at": 0.0, "funded_at": 0.0,
        **changes})
    client.state.cure_funding_farm_home_at = 0.0
    client.state.cure_funding_farm_home_resumed = False
    client.state.cure_funding_farm_home_fruitless = 0
    client.state.cure_funding_farm_home_given_up = False


# A strict town trip (bank, junk sale, identify-all, the apartment chest, a
# heal restock) that cannot start from the cure-funding farm's spot sends
# her home: autoplay hands over to RecoveryTask, which walks her to her
# savebed, runs the funding errands from there and resumes the same farm,
# deaths kept. Live, on 2026-11-23, every town service failed strictly from
# every eastern candidate (content pack-hazard maps on every way back), so
# from the farm she could never sell, bank or restock heals. A break lasts
# at most this long, and another starts no sooner than this after one
# began: no tight loop between the farm and home. After
# CURE_FUNDING_FARM_HOME_FRUITLESS breaks in a row with no town errand
# completed since the break before, she stops going home for the rest of
# that funding farm and farms on (cure_funding_farm_alerts says so).
CURE_FUNDING_FARM_HOME_SECONDS = 1800.0
CURE_FUNDING_FARM_HOME_FRUITLESS = 2
CURE_FUNDING_FARM_HOME = "cure-funding-farm-home"
CURE_FUNDING_FARM_RESUMED = "cure-funding-farm-resumed"
CURE_FUNDING_FARM_HOME_GIVEN_UP = "cure-funding-farm-home-given-up"
# The strict town trips a break home is for: their strict-route retry
# deadlines are cleared when the farm resumes, so they run from her savebed
# at once; longer backoffs (a full chest, a failed trip) are kept.
CURE_FUNDING_FARM_HOME_TRIPS = (
    "_heal_restock_retry_at", "_selling_retry_at", "_banking_retry_at",
    "_identification_retry_at", "_storage_retry_at")


def note_cure_funding_errand(client: AtrinikClient) -> None:
    """A town errand (bank deposit, junk sale, identify-all, the apartment
    chest, a heal restock) completed: a break home achieved something."""
    client.state.cure_funding_farm_errand_at = time.time()


def cure_funding_farm_home(client: AtrinikClient) -> bool:
    """Whether a running cure-funding farm is on its break home
    (send_cure_funding_farm_home); cure_funding_farm is False meanwhile."""
    state = client.state
    at = float(getattr(state, "cure_funding_farm_home_at", 0.0) or 0.0)
    return bool(at and float(getattr(state, "cure_funding_farm_since", 0.0)
                             or 0.0) > 0.0 and
                not getattr(state, "cure_funding_farm_home_resumed", False) and
                time.time() - at < CURE_FUNDING_FARM_HOME_SECONDS)


def send_cure_funding_farm_home(client: AtrinikClient, detail: str) -> bool:
    """Start a break home for the cure-funding farm, unless none runs or
    one began less than CURE_FUNDING_FARM_HOME_SECONDS ago; recorded."""
    if not cure_funding_farm(client):
        return False
    state = client.state
    if getattr(state, "cure_funding_farm_home_given_up", False):
        return False
    at = float(getattr(state, "cure_funding_farm_home_at", 0.0) or 0.0)
    if at and time.time() - at < CURE_FUNDING_FARM_HOME_SECONDS:
        return False
    recorder = getattr(client, "record_action", None)
    if at:
        errand = float(getattr(state, "cure_funding_farm_errand_at", 0.0) or 0.0)
        state.cure_funding_farm_home_fruitless = (
            0 if errand >= at else
            int(getattr(state, "cure_funding_farm_home_fruitless", 0) or 0) + 1)
        if state.cure_funding_farm_home_fruitless >= CURE_FUNDING_FARM_HOME_FRUITLESS:
            state.cure_funding_farm_home_given_up = True
            log.warning("cure-funding farm: %d breaks home achieved nothing; "
                        "farming on: %s",
                        state.cure_funding_farm_home_fruitless, detail)
            if recorder is not None:
                recorder(CURE_FUNDING_FARM_HOME_GIVEN_UP,
                         f"{state.cure_funding_farm_home_fruitless} breaks "
                         f"home achieved nothing; {detail}")
            return False
    state.cure_funding_farm_home_at = time.time()
    state.cure_funding_farm_home_resumed = False
    log.warning("cure-funding farm: home for errands: %s", detail)
    if recorder is not None:
        recorder(CURE_FUNDING_FARM_HOME, detail)
    return True


def resume_cure_funding_farm(client: AtrinikClient) -> bool:
    """End the break home (RecoveryTask, at her savebed with no errand
    left); recorded. False when none is on."""
    if not cure_funding_farm_home(client):
        return False
    client.state.cure_funding_farm_home_resumed = True
    recorder = getattr(client, "record_action", None)
    if recorder is not None:
        recorder(CURE_FUNDING_FARM_RESUMED, "errands done at her savebed")
    return True


def start_cure_funding_farm(client: AtrinikClient, detail: str) -> None:
    """Start the cure-funding farm now (RecoveryTask), and record it."""
    _end_cure_funding_farm(client, since=time.time(), retry_until=0.0)
    log.warning("cure-funding farm: %s", detail)
    recorder = getattr(client, "record_action", None)
    if recorder is not None:
        recorder(CURE_FUNDING_FARM_STARTED, detail)


def cure_affordable(client: AtrinikClient) -> bool:
    """Whether she can pay for the cure: the coins she carries alone, or
    with her known bank balance (the funding path's own measure,
    FarmCircuitTask._cure_shortfall)."""
    cost = FarmCircuitTask._depletion_service_cost(client)
    return (BuyShopUpgradeTask.carried_wallet_value(client) >= cost or
            bool(client.state.bank_balance_known and
                 BuyShopUpgradeTask.wallet_value(client) >= cost))


def cure_funding_farm(client: AtrinikClient) -> bool:
    """Whether she farms to fund her cure now: started, depletion still
    due, fewer than CURE_FUNDING_FARM_DEATHS deaths since, and the cure
    not yet affordable (an unknown bank balance does not prove it is)."""
    if not depleted_for_temple(client.state):
        return False
    funding = cure_funding_farm_state(client)
    return (funding.state == "farming" and
            funding.deaths < CURE_FUNDING_FARM_DEATHS and
            not cure_affordable(client) and
            not _funded_bank_unknown(client) and
            not cure_funding_farm_home(client))


def _funded_bank_unknown(client: AtrinikClient) -> bool:
    """Funded once, and her bank balance unknown since (a restart, or a
    purchase that lost its connection): the fee may still be in the bank,
    so she does not farm; recovery observes the bank again."""
    state = client.state
    return bool(float(getattr(state, "cure_funding_farm_funded_at", 0.0) or 0.0)
                and not state.bank_balance_known)


def settle_cure_funding_farm(client: AtrinikClient) -> str:
    """Keep a running cure-funding farm's state (every autoplay tick).

    It folds the deaths since its start into the persisted count, backs
    off after CURE_FUNDING_FARM_DEATHS deaths, and ends once depletion is
    no longer due. An affordable cure only marks it funded: her deaths
    and the farm stay until the cure, so dropping short again before the
    temple resumes it with the same count; CURE_FUNDING_FARM_KEEP_SECONDS
    after that mark, without a cure, it ends. Returns the action
    recorded, or "". Bookkeeping: cure_funding_farm reads the same
    conditions live."""
    state = client.state
    if not float(getattr(state, "cure_funding_farm_since", 0.0) or 0.0):
        return ""
    funding = cure_funding_farm_state(client)
    now = time.time()
    funded_at = float(getattr(state, "cure_funding_farm_funded_at", 0.0) or 0.0)
    if funding.state != "farming":
        action = CURE_FUNDING_FARM_ENDED
        detail = (f"funded {int(CURE_FUNDING_FARM_KEEP_SECONDS // 3600)} h "
                  "ago and not cured since")
        _end_cure_funding_farm(client)
    elif funding.deaths >= CURE_FUNDING_FARM_DEATHS:
        action = CURE_FUNDING_FARM_BACKOFF
        detail = (f"{funding.deaths} deaths while farming to fund the cure; "
                  "parked for "
                  f"{int(CURE_FUNDING_FARM_BACKOFF_SECONDS // 3600)} h")
        _end_cure_funding_farm(
            client, retry_until=now + CURE_FUNDING_FARM_BACKOFF_SECONDS)
    elif (getattr(state, "inventory_replay_complete", False) and
          not depleted_for_temple(state) and
          (not FarmCircuitTask._has_depletion(client) or
           getattr(state, "depletion_points_known", False))):
        # Only a replayed inventory proves the force gone; the force with
        # an unknown counter proves nothing.
        action, detail = CURE_FUNDING_FARM_ENDED, "depletion no longer due"
        _end_cure_funding_farm(client)
    elif cure_affordable(client) and not funded_at:
        cost = FarmCircuitTask._depletion_service_cost(client)
        action = CURE_FUNDING_FARM_FUNDED
        detail = (f"funded: she has "
                  f"{format_money(BuyShopUpgradeTask.wallet_value(client))} "
                  f"for the {format_money(cost)} cure")
        _set_cure_funding_farm(client, funded_at=now)
    else:
        if (funded_at and state.bank_balance_known and
                not cure_affordable(client)):
            # Short again before the temple, by the funding path's own
            # measure (a known bank balance): farming resumes, deaths kept.
            log.info("cure-funding farm: short of the cure again")
            _set_cure_funding_farm(client, funded_at=0.0)
        counted_at = float(getattr(state, "cure_funding_farm_counted_at", 0.0)
                           or 0.0)
        counted, latest = _deaths_after(client, funding.since, counted_at)
        if counted:
            _set_cure_funding_farm(client, deaths=funding.deaths,
                                   counted_at=latest)
        else:
            # Nothing new: later scans stop here. In memory only, since
            # a restart rescanning from the persisted time finds the same.
            state.cure_funding_farm_counted_at = max(counted_at, now)
        return ""
    log.info("cure-funding farm: %s", detail)
    recorder = getattr(client, "record_action", None)
    if recorder is not None:
        recorder(action, detail)
    return action


def cure_funding_farm_alerts(client: AtrinikClient) -> list[dict[str, str]]:
    """While the cure-funding farm runs: the cure-unfunded alert, as a
    warning that says she farms to fund it."""
    if not cure_funding_farm(client):
        return []
    funding = cure_funding_farm_state(client)
    cost = FarmCircuitTask._depletion_service_cost(client)
    wallet = BuyShopUpgradeTask.wallet_value(client)
    alerts = [{"id": "cure-unfunded", "level": "warn",
               "text": "Cure unfunded: remove depletion costs "
                       f"{format_money(cost)}, she has {format_money(wallet)} "
                       "and nothing left to sell, so she farms the safest "
                       "spot to fund it (deaths while funding: "
                       f"{funding.deaths} of {CURE_FUNDING_FARM_DEATHS}); "
                       "the owner can give her gold with the operator assist"}]
    if getattr(client.state, "cure_funding_farm_home_given_up", False):
        alerts.append({
            "id": "funding-farm-town-unreachable", "level": "warn",
            "text": "Funding farm: town trips unreachable from the farm "
                    f"spot, and {CURE_FUNDING_FARM_HOME_FRUITLESS} breaks "
                    "home achieved nothing; she farms on without them"})
    return alerts


# Services a depleted character must reach for her cure and its funding, all
# planned with strict_safety: the bank, the apartment chest, the Brynknot shop
# floor, Kulgar's identify-all and Saruthar's temple.
# Each entry carries the approach tolerance its task's navigation uses.
TOWN_SERVICES: tuple[tuple[str, tuple[int, int], int], ...] = (
    ("/shattered_islands/world_0_67", (19, 15), 0),
    ("/shattered_islands/strakewood_island/apartments/apartment_cheap", (1, 3), 0),
    ("/shattered_islands/world_0_69", (15, 7), 0),
    ("/shattered_islands/world_8_79", (4, 9), 1),
    ("/shattered_islands/world_3_58", (13, 13), 1),
)
# Strict town routes already planned: per (graph, map, level, learned hazards,
# access items), the result of each (start, service) pair.
_town_reach_cache: dict[tuple, dict[tuple, bool]] = {}


def _farm_starts(graph: "WorldGraph", path: str) -> list[tuple[int, int]]:
    """One square per walkable component holding the map's hostile spawns,
    where a farm on it fights; the most central walkable square without any."""
    node = graph.nodes.get(path)
    if node is None:
        return []
    starts, seen = [], set()
    for spawn in graph.named_spawns.get(path, ()):
        point = (spawn.x, spawn.y)
        if spawn.peaceful or not node.walkable(*point):
            continue
        component = graph._component(path, point)
        if component and not component & seen:
            seen |= component
            starts.append(point)
    if starts:
        return starts
    points = [(x, y) for x in range(node.width) for y in range(node.height)
              if node.walkable(x, y)]
    return [min(points, key=lambda point: (
        (point[0] - node.width / 2) ** 2 +
        (point[1] - node.height / 2) ** 2))] if points else []


def town_safely_reachable(client: AtrinikClient, graph: "WorldGraph",
                          path: str, budget: list[int]) -> bool | None:
    """Whether every town service is strictly reachable from where she
    farms on ``path``; None while still undecided.

    Economy trips plan with strict_safety, so from a map where this fails her
    selling and cure trips cannot start. Each strict plan is a whole-world
    search, so at most ``budget[0]`` new ones run per call (the caller's
    shared budget); results are cached.
    """
    stats = getattr(client.state, "stats", {})
    level = int(stats.get("level", 0) or 0)
    now = time.time()
    learned = {map_path: until for map_path, until in
               (getattr(client.state, "route_hazards", None) or {}).items()
               if until > now}
    # With learned_route_costs on, the proxy plans as her live strict trips
    # do: her flag, her learned model (learned danger of 900 s or more in
    # place of route_hazards) and her kit.
    proxy = types.SimpleNamespace(state=types.SimpleNamespace(
        stats={"level": level}, route_hazards=learned,
        map=types.SimpleNamespace(path=path, world_x=0, world_y=0)),
        config=getattr(client, "config", None), route_cost_source=client,
        farm_analyzer=getattr(client, "farm_analyzer", None))
    access = unlocks.unlock_state(client, graph).access
    strict_learned: frozenset[str] = frozenset()
    if learned_route_costs(client):
        view = route_costs.learned_model(client).view(now, route_costs.current_kit(client))
        strict_learned = frozenset(
            map_path for map_path, seconds in view.maps.items()
            if seconds >= route_costs.STRICT_LEARNED_AVOID_SECONDS)
    key = (id(graph), path, level, frozenset(learned), access, strict_learned)
    if key not in _town_reach_cache and len(_town_reach_cache) > 512:
        _town_reach_cache.clear()
    results = _town_reach_cache.setdefault(key, {})
    for start in _farm_starts(graph, path) or [None]:
        for destination, xy, tolerance in TOWN_SERVICES:
            pair = (start, destination)
            if pair not in results:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
                if start is None:
                    results[pair] = False
                else:
                    proxy.state.map.world_x, proxy.state.map.world_y = start
                    # Her own access set: the proxy carries no inventory.
                    navigation = NavigateTask(graph, destination, destination_xy=xy,
                                              allow_locked=access)
                    navigation.strict_safety = True
                    if tolerance:
                        navigation.tolerance = tolerance
                        navigation._approach = navigation._approach_candidates()
                    try:
                        navigation._plan(proxy)
                        results[pair] = True
                    except ValueError:
                        results[pair] = False
            if not results[pair]:
                return False
    return True


def current_combat_kit(client: AtrinikClient):
    """The combat profile the analyzer ranked spots for, if known."""
    return getattr(getattr(client, "farm_analyzer", None), "profile", None)


def seam_ranking(client: AtrinikClient, graph: "WorldGraph",
                 objective: str) -> list[dict]:
    """The analyzer's ranking for an objective, each spot with its seam
    exposure folded in (farm_analyzer.seam_adjusted), in the analyzer's
    order. A spot whose exposed pack exceeds the ranked profile's
    max_aggro_pack is left out, as the analyzer leaves out its own; a spot
    with a map the graph lacks is kept as ranked (analyzer_spot_legs skips
    it anyway)."""
    analyzer = getattr(client, "farm_analyzer", None)
    if analyzer is None:
        return []
    profile = getattr(analyzer, "profile", None)
    ranked = []
    for spot in analyzer.ranking(objective):
        paths = ["/" + str(raw).removeprefix("maps/").lstrip("/")
                 for raw in spot.get("map_paths") or ()]
        adjusted = farm_analyzer_plan.seam_adjusted(
            spot, graph.seam_exposure(paths), profile)
        if adjusted is not None:
            ranked.append(adjusted)
    return ranked


# An area (each map header's region and name, MapNode.region and .name, as
# the analyzer's circuit_area names a circuit of one area) with at most this many maps is one
# place for kit hazards and pauses (map_areas). The Burnt Forest has 25
# maps; a name used more widely names a whole island or sea (Eld Woods
# Island 42, Strakewood Island 190, World 2115), where a hazard or a pause
# for every map would be out of proportion, and those count by shared maps
# alone, as before.
AREA_HAZARD_MAX_MAPS = 30
# Maps of each bounded area, per graph (area_maps).
_area_maps_cache: dict[tuple[int, int], dict[str, frozenset[str]]] = {}


def _area_key(node) -> str:
    region = str(getattr(node, "region", "") or "")
    name = str(getattr(node, "name", "") or "")
    return f"{region}|{name}" if name else ""


def area_maps(graph: "WorldGraph | None") -> dict[str, frozenset[str]]:
    """Every bounded area's maps (at most AREA_HAZARD_MAX_MAPS), by area
    key ("region|name"); cached per node table."""
    nodes = getattr(graph, "nodes", None) or {}
    # Planner views share the loop graph's nodes: one entry for all.
    key = (id(nodes), len(nodes))
    cached = _area_maps_cache.get(key)
    if cached is not None:
        return cached
    if len(_area_maps_cache) > 8:
        _area_maps_cache.clear()
    grouped: dict[str, set[str]] = defaultdict(set)
    for path, node in nodes.items():
        area = _area_key(node)
        if area:
            grouped[area].add(path)
    areas = {area: frozenset(paths) for area, paths in grouped.items()
             if len(paths) <= AREA_HAZARD_MAX_MAPS}
    _area_maps_cache[key] = areas
    return areas


def map_areas(graph: "WorldGraph | None", paths: Iterable[str]) -> set[str]:
    """The bounded areas (area_maps) of ``paths``: those of a spot's legs
    are the spot's own."""
    nodes = getattr(graph, "nodes", None) or {}
    bounded = area_maps(graph)
    return {area for path in paths
            if (area := _area_key(nodes.get(path))) in bounded}


def kit_hazardous_spot(client: AtrinikClient, spot: dict | None,
                       legs, graph: "WorldGraph | None" = None) -> bool:
    """Whether live failures rate this spot hazardous for her current kit.

    See self_improvement.kit_hazard: repeated pack or out-damaged failures,
    or deaths on the way, at any spot sharing a map or (with ``graph``) a
    bounded area (map_areas) with this one, with a kit she has not
    improved on since (CombatProfile.no_survivability_gain: a weaker kit
    still counts them).
    """
    profile = current_combat_kit(client)
    if profile is None:
        return False
    adaptations = getattr(client.state, "farm_adaptations", {}) or {}

    def same_kit(kit: dict) -> bool:
        old = farm_analyzer_plan.CombatProfile.from_description(kit)
        return old is not None and profile.no_survivability_gain(old)

    return self_improvement.kit_hazard(
        adaptations, self_improvement.spot_key(spot, legs),
        (path for path, _ in legs), same_kit,
        areas=map_areas(graph, (path for path, _ in legs)))


# Map transitions from a map to every map it reaches, per graph and map
# (map_hops).
_map_hops_cache: dict[tuple[int, str, int, int], dict[str, int]] = {}


def map_hops(graph: "WorldGraph", source: str) -> dict[str, int]:
    """Fewest map transitions from ``source`` to each map it reaches: a
    cheap estimate of a trip's length from where she stands, by the
    authored edges alone (no component, lock or hazard)."""
    nodes = getattr(graph, "nodes", {}) or {}
    # Edges change without the map count changing (a rebuild or a test
    # graph): their count is part of the key.
    key = (id(graph), source, len(nodes),
           sum(len(getattr(node, "edges", ())) for node in nodes.values()))
    cached = _map_hops_cache.get(key)
    if cached is not None:
        return cached
    if len(_map_hops_cache) > 64:
        _map_hops_cache.clear()
    hops = {source: 0} if source in nodes else {}
    queue = deque(hops)
    while queue:
        current = queue.popleft()
        for edge in getattr(nodes.get(current), "edges", ()):
            if edge.destination not in hops and edge.destination in nodes:
                hops[edge.destination] = hops[current] + 1
                queue.append(edge.destination)
    _map_hops_cache[key] = hops
    return hops


# Each objective's own measure in a ranked spot (the analyzer's views are
# ordered by it). Spots of one preference tier measuring at least
# FARM_COMPARABLE_SHARE of the tier's first-ranked spot are comparable:
# among them the nearest from where she stands goes first
# (_analyzer_farm_choice).
FARM_OBJECTIVE_MEASURES = {"xp": "xp_hour", "money": "money_hour",
                           "loot": "loot_hour", "balanced": "score",
                           "safety": "safety_score"}
FARM_COMPARABLE_SHARE = 0.8
# Off the surface grid (a dungeon, a cave, a building) and farther than
# this many map transitions from both her savebed and the chosen spot, she
# walks home before farming (farm_home_first).
FARM_HOME_FIRST_HOPS = 3


def _farm_measure(objective: str, spot: dict) -> float | None:
    value = spot.get(FARM_OBJECTIVE_MEASURES.get(objective, ""))
    if (isinstance(value, (int, float)) and not isinstance(value, bool) and
            value == value):
        return float(value)
    return None


def farm_home_first(client: AtrinikClient, graph: "WorldGraph",
                    legs) -> tuple[str, tuple[int, int]] | None:
    """Her savebed, when she should walk there before farming ``legs``.

    Only while she stands off the surface grid, more than
    FARM_HOME_FIRST_HOPS map transitions from her savebed and from the
    circuit's first map (map_hops; unreachable counts as far). The farming
    analyzer judged the circuit's approach from her savebed, not from where
    she stands. Live, on 2026-11-24, a failed errand left Sera deep in the
    Old Outpost and autoplay sent her 49 map transitions to the Burnt
    Forest from there, through six pack-threat maps.
    """
    state = client.state
    here = getattr(getattr(state, "map", None), "path", "") or ""
    home = getattr(state, "savebed_map", "") or ""
    xy = getattr(state, "savebed_xy", None)
    nodes = getattr(graph, "nodes", {}) or {}
    if (not here or not home or xy is None or not legs or here == home or
            home not in nodes or here not in nodes or
            NavigateTask.SURFACE_MAP.fullmatch(here)):
        return None
    hops = map_hops(graph, here)
    far = FARM_HOME_FIRST_HOPS
    if (hops.get(home, far + 1) <= far or
            hops.get(legs[0][0], far + 1) <= far):
        return None
    return home, (int(xy[0]), int(xy[1]))


def farm_home_walk(graph: "WorldGraph", client: AtrinikClient,
                   home: tuple[str, tuple[int, int]]) -> "NavigateThenTask":
    """The walk to her savebed before farming (farm_home_first): the
    recovery phase's ordinary walk home (RecoveryTask._new_home_walk), not
    an essential trip."""
    walk = NavigateThenTask(graph, home[0], _ArriveTask("savebed"),
                            destination_xy=home[1], combat_approach=True)
    walk.navigation.tolerance = 1
    walk.navigation.allow_ranged_hazard_fallback = (
        FarmCircuitTask._combat_progression_level(client) >=
        FarmCircuitTask.SWAMP_TRANSIT_LEVEL)
    return walk


# The cure-funding farm never takes a spot whose analyzer danger is this
# share of her maximum health or more: at most half, so she survives two
# such encounters back to back (_analyzer_farm_choice).
FUNDING_FARM_DANGER_SHARE = 0.5


def _spot_danger(spot: dict) -> float:
    """The analyzer's danger for a ranked spot; infinite when unknown."""
    danger = spot.get("danger")
    if (isinstance(danger, (int, float)) and not isinstance(danger, bool) and
            danger == danger):
        return float(danger)
    return float("inf")


def recent_kit_failures(client: AtrinikClient) -> list[frozenset[str]]:
    """Her failures within the kit window, one entry of maps each.

    Any cause counts, emergency recalls on the way included, with the kit
    matching of kit_hazardous_spot (a failure without a kit, or with one
    she has since genuinely improved on, does not). Each counts for the
    maps of its spot's record and for the map it happened on. The
    cure-funding farm prefers spots with the fewest of them: live, on
    2026-11-23, an orc pack forced a recall at the Underground City
    entrance at 02:43, and that entrance (danger 35.7) still outranked
    Hemlock Cave (35.8).
    """
    profile = current_combat_kit(client)
    if profile is None:
        return []
    now = time.time()
    failures = []
    for key, record in (getattr(client.state, "farm_adaptations", {}) or {}).items():
        if not isinstance(record, dict):
            continue
        maps = frozenset(self_improvement.record_maps(key, record))
        for entry in record.get("kit_failures", ()):
            if not isinstance(entry, dict) or (
                    now - float(entry.get("time", 0.0) or 0.0) >
                    self_improvement.KIT_HAZARD_WINDOW_SECONDS):
                continue
            old = farm_analyzer_plan.CombatProfile.from_description(
                entry.get("kit") or {})
            if old is not None and profile.no_survivability_gain(old):
                failures.append(maps | {str(entry.get("map") or "")})
    return failures


def route_death_maps(client: AtrinikClient) -> set[str]:
    """Maps where she recently died on the way to a farm spot.

    See self_improvement.route_death_maps. A death recorded with a kit she
    has since genuinely improved on stops counting, as for kit hazards
    (CombatProfile.no_survivability_gain); without a known kit to compare,
    every recent route death counts.
    """
    return set(route_death_times(client))


def route_death_overdue(client: AtrinikClient) -> bool:
    """Whether the newest counting route failure is older than
    ROUTE_DEATH_VERDICT_WAIT_SECONDS: its verdict is searched for despite
    contact or an unfinished fight."""
    times = route_death_times(client)
    return bool(times) and (time.time() - max(times.values()) >=
                            ROUTE_DEATH_VERDICT_WAIT_SECONDS)


def route_death_times(client: AtrinikClient) -> dict[str, float]:
    """route_death_maps, each with its newest counting failure's time."""
    adaptations = getattr(client.state, "farm_adaptations", {}) or {}
    profile = current_combat_kit(client)

    def applies(kit: dict) -> bool:
        if profile is None:
            return True
        old = farm_analyzer_plan.CombatProfile.from_description(kit)
        return old is None or profile.no_survivability_gain(old)

    return self_improvement.route_death_times(adaptations, applies)


# A route-death map that cuts off more than this share of the maps she can
# reach from her respawn is a chokepoint (a town gate, the island's only
# road): blocking it would halt all farming, so it keeps only the planner's
# penalised cost. Live, a hazard on world_7_64, next to her respawn, would
# have blocked all 40 ranked spots.
ROUTE_DEATH_CHOKEPOINT_SHARE = 0.5
# A route death still undecided this long after it happened (she stayed in
# contact, or kept finishing fights) is searched anyway, still rate limited:
# neither selection nor the circuit's hold waits for a search for good.
ROUTE_DEATH_VERDICT_WAIT_SECONDS = 30.0
# At most one whole-world reachability search (most of a second on the
# event loop) per this many seconds, shared by every caller.
ROUTE_DEATH_SEARCH_INTERVAL_SECONDS = 2.0
# Maps reachable from a respawn component: per (graph, map, component,
# avoided maps, access items).
_reach_cache: dict[tuple, frozenset[str]] = {}
_reach_search_after = [0.0]


def route_death_origin(client: AtrinikClient, graph: "WorldGraph"
                       ) -> tuple[str, tuple[int, int], tuple[int, int], bool] | None:
    """(map, square, component key, from savebed) where reach is judged from.

    Her savebed, where every death returns her and every farm trip after
    one starts, so verdicts do not depend on where she happens to stand
    (inside the hazard, or behind it). An apartment savebed is left by its
    recorded return. Without a known savebed, where she stands (keyed by
    her component, so a verdict is never reused from elsewhere); the last
    field says which. None while no walkable square is known.
    """
    state = client.state
    path = getattr(state, "savebed_map", "") or ""
    xy = getattr(state, "savebed_xy", None)
    if path and "/apartments/" in path:
        back = (getattr(state, "apartment_returns", {}) or {}).get(path)
        path, xy = (back[0], (int(back[1][0]), int(back[1][1]))) if back else ("", None)
    savebed = bool(path and xy is not None and path in graph.nodes)
    if not savebed:
        origin = apartment_exit_origin(client)
        path, xy = origin or (state.map.path,
                              (state.map.world_x, state.map.world_y))
    if path not in graph.nodes:
        return None
    component = graph._component(path, xy)
    if not component:
        return None
    return path, xy, min(component), savebed


def _contact_this_life(client: AtrinikClient, seconds: float = 5.0) -> bool:
    """recent_hostile_contact, not counting hits from before her last death:
    those were taken in the life that just ended, wherever she is now."""
    deaths = [float(record.get("time", 0.0) or 0.0)
              for record in getattr(client, "decision_history", ())
              if isinstance(record, dict) and record.get("action") == "death"]
    if deaths:
        seconds = min(seconds, max(0.0, time.time() - max(deaths)))
    return recent_hostile_contact(client, seconds=seconds)


def _reachable_from(client: AtrinikClient, graph: "WorldGraph", origin,
                    avoided: frozenset[str], access, search: bool,
                    overdue: bool = False) -> frozenset[str] | None:
    """Cached reachable_maps from ``origin``; None while not yet searched.

    A search runs only with ``search``, without hostile contact, and at most
    once per ROUTE_DEATH_SEARCH_INTERVAL_SECONDS across all callers. Contact
    is read from the hits on her, not the combat-mode flag: the server turns
    combat mode on with every hit and keeps it through a death, so live the
    flag held a verdict undecided after a route death for as long as she
    walked back towards the map that killed her. An ``overdue`` verdict
    (route_death_overdue) is searched despite contact.
    """
    path, xy, component, _ = origin
    # With the dialogue_edges flag on, reach follows the planner: offered
    # voyages count, closed quest gates do not.
    offer = dialogue_edges.dialogue_offer(client, graph)
    key = (id(graph), path, component, avoided, access,
           None if offer is None else (offer.edges, offer.closed_exits))
    if key in _reach_cache:
        return _reach_cache[key]
    if (not search or (not overdue and _contact_this_life(client)) or
            time.monotonic() < _reach_search_after[0]):
        return None
    if len(_reach_cache) > 256:
        _reach_cache.clear()
    try:
        reach = graph.reachable_maps(path, xy, allow_locked=access,
                                     avoided_maps=set(avoided), offer=offer)
    except ValueError:
        reach = frozenset()
    _reach_cache[key] = reach
    _reach_search_after[0] = (time.monotonic() +
                              ROUTE_DEATH_SEARCH_INTERVAL_SECONDS)
    return reach


def route_death_blocked_maps(client: AtrinikClient, graph: "WorldGraph", *,
                             search: bool = True) -> frozenset[str] | None:
    """Maps farming must not go to while a recent route death counts.

    The maps she can reach from her respawn (route_death_origin), less
    those she can still reach without entering a route-death map
    (route_death_maps): the death maps and everything only reachable
    through them. Live, Sera died six times in 13 minutes at the
    underground city entrance on the way to two circuits that do not farm
    it: each circuit's own record had to collect its own deaths there.
    The coarse map graph is not enough: it reaches those circuits around
    the entrance through seams she cannot walk.

    A death on her respawn map, or on a chokepoint, blocks nothing: it
    keeps only the planner's penalised cost. None while still undecided
    (searches are cached and rate limited, _reachable_from); empty when
    nothing is blocked. ROUTE_DEATH_VERDICT_WAIT_SECONDS after the newest
    failure the searches no longer wait for contact to end
    (route_death_overdue); they stay rate limited, and None still means
    undecided until they have run.
    """
    hazards = route_death_maps(client)
    if not hazards:
        return frozenset()
    overdue = route_death_overdue(client)
    origin = route_death_origin(client, graph)
    if origin is None:
        return None
    if not origin[3] and origin[0] in hazards:
        # Judged from where she stands, and that is a death map: what it
        # reaches says nothing about the way in. Block only the death maps
        # themselves, so a spot elsewhere is picked and she walks off;
        # waiting for a verdict would hold her there for good.
        return frozenset(hazards)
    hazards.discard(origin[0])
    hazards.discard(getattr(client.state, "savebed_map", "") or "")
    access = NavigateTask(graph, origin[0])._allow_locked(client)
    everywhere = _reachable_from(client, graph, origin, frozenset(), access,
                                 search, overdue)
    if everywhere is None:
        return None
    limit = ROUTE_DEATH_CHOKEPOINT_SHARE * len(everywhere)
    cuts: dict[str, int] = {}
    for hazard in sorted(hazards & everywhere):
        without = _reachable_from(client, graph, origin, frozenset({hazard}),
                                  access, search, overdue)
        if without is None:
            return None
        if len(everywhere - without) <= limit:
            cuts[hazard] = len(everywhere - without)
    # Hazards each under the share can together still cut off most of the
    # world. Then the largest cuts stay cost-only, one at a time, until
    # the rest together are under it: a small dead end (the underground
    # city entrance) stays blocked beside a large-cut hazard.
    blocking = sorted(cuts, key=lambda hazard: (-cuts[hazard], hazard))
    while blocking:
        reach = _reachable_from(client, graph, origin, frozenset(blocking),
                                access, search, overdue)
        if reach is None:
            return None
        if len(everywhere - reach) <= limit:
            return frozenset(everywhere - reach)
        blocking.pop(0)
    return frozenset()


def analyzer_farm_choice(
        client: AtrinikClient, graph: "WorldGraph", *, search: bool = True,
        search_verdict: bool | None = None,
        current_legs: tuple[tuple[str, str], ...] | None = None
) -> tuple[str, tuple[tuple[str, str], ...], dict] | None:
    """The objective farmed now, with its highest-ranked usable spot
    (_analyzer_farm_choice). With ``search`` (autoplay's own call, not the
    dashboard's or the metrics'), the farm score's shadow (ROUTING P6,
    route_shadow.py) is offered this choice to compare against; that never
    changes the choice or raises. ``current_legs`` is the circuit she is
    on, if any."""
    choice = _analyzer_farm_choice(client, graph, search=search,
                                   search_verdict=search_verdict,
                                   current_legs=current_legs)
    if search:
        route_shadow.request_farm(client, graph, choice)
    return choice


def _analyzer_farm_choice(
        client: AtrinikClient, graph: "WorldGraph", *, search: bool = True,
        search_verdict: bool | None = None,
        current_legs: tuple[tuple[str, str], ...] | None = None
) -> tuple[str, tuple[tuple[str, str], ...], dict] | None:
    """The objective farmed now, with its highest-ranked usable spot.

    The farming analyzer is the source of truth. The only exceptions are live
    failures: a spot quarantined after a death, an emergency recall, ten minutes
    without XP, or an unreachable route is skipped until its quarantine ends;
    a spot whose route enters a map where she recently died on the way to a
    farm (route_death_blocked_maps) is skipped while that death counts, for
    every circuit behind the map, not only the one she was heading to; and
    a spot rated hazardous for her current kit (kit_hazardous_spot) is
    skipped until her survivability improves, unless every usable spot is
    hazardous; then the least dangerous of them is farmed (the safer
    approach on a tie). Within each objective's ranking, usable spots with
    a known, safe approach from her savebed (farm_analyzer.risky_approach)
    come first in rank order, then the rest from the safest down (the
    analyzer's safety_score, else farm_analyzer.approach_safety). Route-death
    spots are never that fallback, nor any depletion fallback below. While
    a route death is still being judged (route_death_blocked_maps returns
    None), nothing is usable yet and None is returned. Without ``search``
    (the dashboard and metrics), only cached results are read: no
    whole-world search runs on their behalf. ``search_verdict`` overrides
    ``search`` for the route-death verdict alone (an overdue one is
    searched mid-fight); town routes still follow ``search``.

    While depleted, a safety spot is skipped once any map on it is known not
    to reach every town service on a strictly safe route
    (town_safely_reachable): her selling and cure trips plan strictly, so she
    farms where they can start. At most one such route is planned per call;
    a spot still being checked counts until it is shown to fail, so callers
    always get a spot and move off it on a later call.

    If depletion prefers safety but no safety spot qualifies, the chosen
    objective's spot applies, and failing that the best usable safety spot.
    While she is depleted that far, autoplay farms only to fund the cure
    (farming_unsafe_reason, RecoveryTask): it removes depletion first, funds
    the fee from what she owns, and only once nothing is left to sell
    farms for it (cure_funding_farm). That farm follows the safety ranking
    alone, never the chosen objective, from the deeper safety view
    (FarmAnalyzer.SAFETY_TOP): only spots with a safe approach whose danger
    is under half her health and under every spot rated hazardous for her
    kit; those with the fewest recent failures (recent_kit_failures) first,
    then the safest, then the least dangerous (the analyzer's danger). With
    none left, it waits. It skips the strict town check above.
    """
    analyzer = getattr(client, "farm_analyzer", None)
    if analyzer is None:
        return None
    chosen = getattr(client.state, "farm_objective", DEFAULT_OBJECTIVE)
    quarantine = getattr(client.state, "farm_zone_quarantine", {}) or {}
    now = time.time()
    usable = []
    hazardous = []
    # No pick, the fallbacks included, may send her back across a map that
    # just killed her; with no route death recorded, nothing is searched.
    blocked = route_death_blocked_maps(
        client, graph, search=search if search_verdict is None else search_verdict)
    if blocked is None:
        return None
    plan = farm_analyzer_plan
    order: dict[int, tuple[int, int]] = {}
    funding = cure_funding_farm(client)
    objectives = (("safety",) if funding else
                  tuple(dict.fromkeys(
                      (effective_farm_objective(client.state), chosen))))
    for position, objective in enumerate(objectives):
        for rank, spot in enumerate(seam_ranking(client, graph, objective)):
            legs = analyzer_spot_legs(spot, graph)
            if (legs and all(float(quarantine.get(path, 0.0) or 0.0) <= now
                             for path, _ in legs) and
                    not any(path in blocked for path, _ in legs)):
                entry = (objective, legs, spot)
                order[id(entry)] = (position, rank)
                (hazardous if kit_hazardous_spot(client, spot, legs, graph)
                 else usable).append(entry)
    if not usable and hazardous and not funding:
        # Every usable spot is hazardous for her kit: farm the least
        # dangerous one rather than idle until the hazard ages out, the
        # one with the safer approach on a tie; a spot whose own
        # encounters stay under her health (not risky_spot, seam exposure
        # included) before any that do not. Never on the cure-funding
        # farm: depleted, she waits for a spot that is not (the 2026-11-17
        # death spiral farmed exactly that fallback).
        usable = [min(hazardous, key=lambda entry: (
            plan.risky_spot(entry[2]),
            float(entry[2].get("danger", 0.0) or 0.0),
            -plan.approach_safety(entry[2]), order[id(entry)]))]
    if funding:
        # The cure-funding farm takes only a spot with a safe approach and
        # a safe spot (neither risky_approach nor risky_spot, seam exposure
        # included) whose danger is under half her health and under every
        # spot rated hazardous for her kit, and waits when none is left. Live, on
        # 2026-11-23, with the safety view 250 deep, a replay walked down
        # to a 118.3 spot with a risky approach; the circuits that killed
        # and out-damaged her rated 103.5 and 125.8. Single failures only
        # order the rest (recent_kit_failures). A hazardous spot caps
        # whether or not it is quarantined or behind a route death: the
        # spot her kit just failed on still says what is too much, and so
        # does one seam exposure leaves out (the raw ranking caps: the
        # exposure never changes a spot's danger).
        stats = getattr(client.state, "stats", {}) or {}
        profile = current_combat_kit(client)
        health = float(getattr(profile, "max_health", 0) or
                        stats.get("maxhp", 0) or 0)
        cap = FUNDING_FARM_DANGER_SHARE * health if health > 0 else float("inf")
        for spot in analyzer.ranking("safety"):
            legs = analyzer_spot_legs(spot, graph)
            if (legs and _spot_danger(spot) < cap and
                    kit_hazardous_spot(client, spot, legs, graph)):
                cap = _spot_danger(spot)
        usable = [entry for entry in usable
                  if not plan.risky_approach(entry[2]) and
                  not plan.risky_spot(entry[2]) and
                  _spot_danger(entry[2]) < cap]
        failures = recent_kit_failures(client)
        recent = {id(entry): sum(1 for maps in failures
                                 if maps & {path for path, _ in entry[1]})
                  for entry in usable}
    # Travel from where she stands: among comparable spots (each tier's
    # measure at least FARM_COMPARABLE_SHARE of its first-ranked spot's),
    # fewest map transitions to the circuit's first map first (map_hops).
    # The analyzer judged every approach from her savebed; a failed errand
    # can leave her far from it.
    here = getattr(getattr(client.state, "map", None), "path", "") or ""
    hops = map_hops(graph, here) if here else {}
    # Each tier's first-ranked spot sets what is comparable: the analyzer
    # would farm it.
    best_measure: dict[tuple[int, bool], float] = {}
    for entry in sorted(usable, key=lambda entry: order[id(entry)]):
        position, _ = order[id(entry)]
        tier = (position, plan.risky_approach(entry[2]) or
                plan.risky_spot(entry[2]))
        measure = _farm_measure(entry[0], entry[2])
        if measure is not None:
            best_measure.setdefault(tier, measure)

    def travel(entry, position: int, risky: bool) -> tuple[int, float]:
        # (0, transitions) for a comparable spot, (1, 0) for the rest. The
        # circuit she is on (``current_legs``), while comparable, stays
        # first: re-chosen on every farm tick from wherever her walk has
        # got to, a comparable circuit starting on a map she passes would
        # replace it mid-trip.
        measure = _farm_measure(entry[0], entry[2])
        best = best_measure.get((position, risky))
        if measure is None or best is None or (
                measure < FARM_COMPARABLE_SHARE * best):
            return 1, 0.0
        if current_legs is not None and entry[1] == tuple(current_legs):
            return 0, -1.0
        return 0, float(hops.get(entry[1][0][0], math.inf))

    def preference(entry) -> tuple:
        # Within each objective's ranking (the effective one first, so the
        # depletion safety rule holds): spots with a known, safe approach
        # from her savebed whose own encounters stay under her health
        # (neither risky_approach nor risky_spot, seam exposure included)
        # in rank order, then the risky ones from the
        # safest down by the analyzer's safety_score (the lower of spot and
        # approach safety), or by approach safety (unreachable counts as
        # UNREACHABLE_APPROACH_SAFETY) without one, in rank order on ties.
        position, rank = order[id(entry)]
        spot = entry[2]
        risky = plan.risky_approach(spot) or plan.risky_spot(spot)
        overall = spot.get("safety_score")
        safety = (float(overall) if isinstance(overall, (int, float)) and
                  not isinstance(overall, bool) else plan.approach_safety(spot))
        if funding:
            # The cure-funding farm wants the easiest spot, not the safety
            # view's order: its score is capped at 1, and the analyzer
            # orders those ties by its balanced score (FarmAnalyzer.
            # SAFETY_TOP). So the fewest recent failures first, then the
            # safest, and among equally safe spots the least dangerous.
            return (position, recent[id(entry)], -safety,
                    _spot_danger(spot), rank)
        # Risky spots tied on safety (one approach caps them all) go by
        # their own safety, seam exposure included, then by rank. Safe
        # ones go nearest first among comparable spots (travel), then by
        # rank.
        own = plan.spot_safety(spot) if risky else 0.0
        return (position, risky, -safety if risky else 0.0, -own,
                *((1, 0.0) if risky else travel(entry, position, risky)),
                rank)

    usable.sort(key=preference)
    if funding or not depleted_for_temple(client.state):
        # The funding farm skips the strict town check: east of her
        # savebed every candidate fails it statically (the content pack
        # hazard maps on every way back), so it only walked her down the
        # list a spot per tick. A strict town trip refused from the farm
        # sends her home for it instead (send_cure_funding_farm_home).
        return usable[0] if usable else None
    budget = [1 if search else 0]
    for objective, legs, spot in usable:
        if objective != "safety":
            return objective, legs, spot
        verdict: bool | None = True
        for path in dict.fromkeys(path for path, _ in legs):
            verdict = town_safely_reachable(client, graph, path, budget)
            if verdict is not True:
                break
        if verdict is not False:
            return objective, legs, spot
    return usable[0] if usable else None


# The farm score's shadow (ROUTING 4.5, 4.9, P6): spots scored per ranking,
# and learned maps checked for being a chokepoint, at most this many.
SHADOW_FARM_CANDIDATES = 25
SHADOW_CHOKEPOINT_CHECKS = 12


@dataclass(slots=True)
class FarmShadowSnapshot:
    """The farm score's inputs, taken once (route_shadow.py)."""

    graph: "WorldGraph"
    origin: str
    origin_xy: tuple[int, int]
    free: frozenset[str]
    access: Any
    walls: Any
    offer: Any
    context: route_costs.CostContext
    candidates: list[tuple[route_costs.FarmCandidate, tuple[tuple[str, str], ...]]]
    live: str


def farm_spot_key(objective: str, spot: dict) -> str:
    return f"{objective}:{spot.get('path') or spot.get('name') or ''}"


def farm_shadow_snapshot(client: AtrinikClient, graph: "WorldGraph",
                         live: tuple | None) -> FarmShadowSnapshot | None:
    """What the farm score (route_costs.farm_score) would choose from,
    mirroring analyzer_farm_choice's usable spots without #94's
    route_death_blocked_maps, which P8 replaces with the score: the
    effective objective's ranking first, then the chosen one; quarantined
    spots skipped; spots hazardous for her kit skipped unless every spot
    is, then the least dangerous. None when there is nothing to score, or
    while she is depleted at the temple threshold (autoplay does not farm
    then, and the strict town check, not the score, decides)."""
    analyzer = getattr(client, "farm_analyzer", None)
    if analyzer is None or depleted_for_temple(client.state):
        return None
    origin = route_death_origin(client, graph)
    if origin is None:
        return None
    chosen = getattr(client.state, "farm_objective", DEFAULT_OBJECTIVE)
    quarantine = getattr(client.state, "farm_zone_quarantine", {}) or {}
    now = time.time()
    usable, hazardous = [], []
    for position, objective in enumerate(dict.fromkeys(
            (effective_farm_objective(client.state), chosen))):
        for rank, spot in enumerate(
                seam_ranking(client, graph, objective)[:SHADOW_FARM_CANDIDATES]):
            legs = analyzer_spot_legs(spot, graph)
            if not legs or any(float(quarantine.get(path, 0.0) or 0.0) > now
                               for path, _ in legs):
                continue
            candidate = route_costs.FarmCandidate(
                farm_spot_key(objective, spot), str(spot.get("name") or ""), objective,
                position, rank, route_costs.spot_rate(spot, objective))
            entry = (candidate, legs, spot)
            (hazardous if kit_hazardous_spot(client, spot, legs, graph) else usable).append(entry)
    if not usable and hazardous:
        usable = [min(hazardous, key=lambda entry: (
            farm_analyzer_plan.risky_spot(entry[2]),
            float(entry[2].get("danger", 0.0) or 0.0),
            -farm_analyzer_plan.approach_safety(entry[2]),
            entry[0].position, entry[0].rank))]
    if not usable:
        return None
    stats = getattr(client.state, "stats", {}) or {}
    level = max(1, int(stats.get("level", 0) or 0))
    model = route_costs.learned_model(client)
    view = model.view(now, route_costs.current_kit(client))
    context = route_costs.CostContext(
        graph, view, route_costs.content_priors(
            graph, level, lethal=NavigateTask.KNOWN_LETHAL_TRANSIT),
        pace=route_costs.seconds_per_tile(stats),
        lam=danger_weight(client, optional=False))
    free = frozenset(path for path in (origin[0], getattr(client.state, "savebed_map", "") or "")
                     if path)
    live_key = farm_spot_key(live[0], live[2]) if live else ""
    return FarmShadowSnapshot(
        graph, origin[0], origin[1], free,
        NavigateTask(graph, origin[0])._allow_locked(client),
        route_blocks.owner_walls(client), dialogue_edges.dialogue_offer(client, graph),
        context, [(candidate, legs) for candidate, legs, _ in usable], live_key)


def _reach_without(offsets: list[int], targets: list[int], maps: list[str],
                   removed: str) -> set[str]:
    """The maps reachable from state 0 of a component search without
    entering ``removed`` (#94's chokepoint test, on the search's states)."""
    if maps[0] == removed:
        return set()
    seen = {0}
    pending = [0]
    while pending:
        state = pending.pop()
        for target in targets[offsets[state]:offsets[state + 1]]:
            if target not in seen and maps[target] != removed:
                seen.add(target)
                pending.append(target)
    return {maps[state] for state in seen}


def farm_shadow(snapshot: FarmShadowSnapshot):
    """The farm score's choice (ROUTING 4.5) for ``snapshot``: a generator
    that yields while it searches and returns (route_costs.FarmScore, the
    chokepoints found).

    One weighted search from her savebed (route_death_origin) prices every
    spot's cheapest route at once. ``D(spot)`` is the learned danger of
    every map on that route and on the spot's legs, each map once, plus
    the failures of the route's edges; her savebed map and the map she
    starts from add nothing. A learned map whose removal cuts off more than
    CHOKEPOINT_SHARE of the maps she reaches is a chokepoint (#94's test):
    its danger counts toward the chokepoint cap, not the per-spot cap.
    Content priors steer the route only; they are not in ``D`` (4.5).
    """
    graph, context = snapshot.graph, snapshot.context
    states, _, offsets, targets, metadata, costs, route_edges = \
        yield from graph._expand_component_states(
            snapshot.origin, snapshot.origin_xy, None, allow_locked=snapshot.access,
            walls=snapshot.walls or None, offer=snapshot.offer,
            edge_cost=context.edge_cost)
    weights = [route_costs.scaled(cost) for cost in costs]
    maps = [path for path, _ in states]
    everywhere = set(maps)
    view = context.view
    learned = sorted((path for path in everywhere
                      if path not in snapshot.free and view.map_danger(path) > 0),
                     key=lambda path: (-view.map_danger(path), path))
    chokepoints = set()
    for path in learned[:SHADOW_CHOKEPOINT_CHECKS]:
        cut = len(everywhere - _reach_without(offsets, targets, maps, path))
        if cut > route_costs.CHOKEPOINT_SHARE * len(everywhere):
            chokepoints.add(path)
        yield
    first_states: dict[str, list[int]] = {}
    for index, path in enumerate(maps):
        first_states.setdefault(path, []).append(index)
    for candidate, legs in snapshot.candidates:
        goals = first_states.get(legs[0][0])
        result = None
        if goals:
            result = graph_search(offsets, targets, 0, goals, metadata=metadata,
                                  costs=weights)
        if result is None or result.status != "found":
            candidate.reason = f"no route to {legs[0][0]}"
            continue
        route = [route_edges[index] for index in result.transitions]
        crossed = dict.fromkeys([edge.destination for edge in route] +
                                [path for path, _ in legs])
        danger, choke = 0.0, {}
        for path in crossed:
            if path in snapshot.free:
                continue
            value = view.map_danger(path)
            danger += value
            if value and path in chokepoints:
                choke[path] = value
        danger += sum(view.edge_danger(context.subject(edge)) for edge in route)
        candidate.danger, candidate.chokepoints = danger, choke
        candidate.maps = tuple(crossed)
        yield
    return route_costs.farm_score([candidate for candidate, _ in snapshot.candidates]), \
        chokepoints


def analyzer_farm_selection(
        client: AtrinikClient, graph: "WorldGraph"
) -> tuple[tuple[tuple[str, str], ...], dict] | None:
    """The legs and spot of `analyzer_farm_choice`."""
    choice = analyzer_farm_choice(client, graph)
    return None if choice is None else choice[1:]


def food_reserve(client: AtrinikClient, *, include_unpaid: bool = False) -> int:
    """Edible items she carries and may eat (safe_food: identified, paid,
    not cursed or damned). With include_unpaid, also food just picked up on
    a shop floor, paid for when she leaves the shop."""
    patterns = SafetyPolicy().food_patterns

    def edible(item) -> bool:
        if include_unpaid and item.flags & c.ITEM_UNPAID:
            return safe_food(dataclasses.replace(item, flags=item.flags & ~c.ITEM_UNPAID))
        return safe_food(item)

    return sum(item.quantity for item in client.state.inventory
               if edible(item) and (
                   item.item_type == c.TYPE_FOOD or
                   any(pattern in item.name.casefold()
                       for pattern in patterns)))


def unidentified_food(client: AtrinikClient) -> list:
    """Carried food she would eat once identified (it may be cursed until
    then): server-typed food she owns, the kind safety eats. Unpaid items are
    left out, as the identify capability leaves them out."""
    policy = InventoryPolicy()
    return [item for item in client.state.inventory
            if item.item_type == c.TYPE_FOOD and not policy.identified(item) and
            not item.flags & (c.ITEM_APPLIED | c.ITEM_UNPAID)]


def starving_without_food(client: AtrinikClient) -> bool:
    """Food 0 and no safe food carried (food_reserve: edible items only):
    no rest can help, only food."""
    return (int(client.state.stats.get("food", 1) or 0) <= 0 and
            food_reserve(client) == 0)


def hungry(client: AtrinikClient) -> bool:
    """Low on food with nothing left to eat: she must go and get some."""
    return (food_reserve(client) <= 1 and
            int(client.state.stats.get("food", 1000) or 0) < HUNGRY_FOOD)


def apartment_exit_origin(
        client: AtrinikClient) -> tuple[str, tuple[int, int]] | None:
    """Where leaving the apartment she stands in really puts her.

    The graph gives an apartment a static exit to every entrance in its
    region, so a route planned from inside one can start from a teleporter
    she will never arrive at. Live: inside her apartment Sera ranked Talrain
    one hop away, left to her recorded return on world_4_45, and found no
    route to him from there. None outside an apartment or before leaving it
    has shown where its exit leads.
    """
    path = client.state.map.path
    if "/apartments/" not in path:
        return None
    back = (getattr(client.state, "apartment_returns", {}) or {}).get(path)
    if not back:
        return None
    return back[0], (int(back[1][0]), int(back[1][1]))


def rank_temple_priests(graph: "WorldGraph", client: Any, *,
                        exclude: Iterable[str] = (),
                        origin: tuple[str, tuple[int, int]] | None = None
                        ) -> DialogueVendor | None:
    """The nearest priest reachable without a threat fallback, or None.

    Priests named in ``exclude`` (whose trip just failed) are passed over.
    One route plan per priest: a whole-world search each, so live this
    runs on the planner worker (TempleFoodPlanner). Its probes share one
    component refresh (ComponentRefresh).
    """
    ranked = []
    excluded = {name.casefold() for name in exclude}
    refresh = ComponentRefresh()
    for priest in graph.temple_priests:
        if priest.name.casefold() in excluded:
            continue
        navigation = NavigateTask(
            graph, priest.map_path, (priest.x, priest.y), tolerance=2)
        navigation.essential = "food"
        navigation.refresh = refresh
        try:
            route = navigation._plan(client, origin=origin)
        except ValueError:
            continue
        if not navigation._threat_fallback:
            # A priest only reachable across an owner block comes last.
            ranked.append((navigation.care is not None or navigation.over_budget,
                           len(route), priest.name.casefold(), priest))
    if not ranked:
        return None
    return min(ranked, key=lambda entry: entry[:-1])[-1]


def temple_food_task(graph: "WorldGraph", priest: DialogueVendor) -> NavigateThenTask:
    """The free temple food trip to ``priest``."""
    task = NavigateThenTask(
        graph, priest.map_path, TempleFoodTask(priest.name),
        destination_xy=(priest.x, priest.y), combat_approach=True)
    task.navigation.tolerance = 2
    task.navigation.essential = "food"
    return task


def temple_food_trip(graph: "WorldGraph", client: AtrinikClient, *,
                     exclude: Iterable[str] = ()) -> NavigateThenTask | None:
    """Route to the nearest priest reachable without a threat fallback,
    ranked right here (rank_temple_priests). A tick never calls this: it
    can take most of a minute, so ticks use TempleFoodPlanner.

    Priests named in ``exclude`` (whose trip just failed) are passed over.
    """
    priest = rank_temple_priests(graph, client, exclude=exclude,
                                 origin=apartment_exit_origin(client))
    return None if priest is None else temple_food_task(graph, priest)


class _Planning:
    """A background plan's answer while it still runs (BackgroundPlans)."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "PLANNING"


PLANNING = _Planning()


def planning_client(client: AtrinikClient, graph: "WorldGraph") -> planner.PlanningClient:
    """Her client for the planner worker (planner.PlanningClient), with her
    plan inputs (PlanInputs) taken now, on the loop: every route a worker
    plans with it reads her walls, offer and access set from here, never
    from her live client. With learned costs on, her learned model is
    refreshed here too, so the worker reads it warm."""
    model = route_costs.learned_model(client) if learned_route_costs(client) else None
    proxy = planner.PlanningClient(client)
    proxy.plan_inputs = plan_inputs(client, graph)
    proxy.refresh = ComponentRefresh()
    if model is not None:
        # Frozen for this plan: learned_model(proxy) returns it and never
        # reads her memory on the worker (its state reads as never stale).
        state = getattr(client, "_route_cost_model_state", (model.character, 0.0, 0))
        proxy._route_cost_model = model
        proxy._route_cost_model_state = (state[0], math.inf, state[2])
    return proxy


class _BackgroundPlan:
    """One build(PlanningClient) call on the planner worker."""

    def __init__(self, graph: "WorldGraph", client: AtrinikClient,
                 build: Callable[[Any], Any], key: Any,
                 clock: Callable[[], float], priority: int):
        self.key = key
        # When the worker was first seen running it: time in the queue
        # behind other rankings does not count towards MAX_PENDING_SECONDS.
        self.running_since: float | None = None
        self.finished_at: float | None = None
        # The view is made here, warm for the worker.
        graph.planner_view()
        self.proxy = planning_client(client, graph)
        self.future = planner.submit(build, self.proxy, priority=priority)
        self.future.add_done_callback(lambda _: setattr(self, "finished_at", clock()))


class BackgroundPlans:
    """Route rankings on the planner worker, so no tick waits for one
    (ROUTING 1.12).

    ``get(name, graph, client, build)`` runs ``build`` with her
    PlanningClient on the worker, where every NavigateTask plans on the
    graph's planner view, and answers PLANNING (falsy) until it is done,
    while safety, healing, combat and the connection carry on; then its
    result, the actions it recorded replayed on her client and its error
    raised as the ranking itself would have raised it. A result is used
    only for the same ``key`` (the inputs that change what it finds), and
    within RESULT_SECONDS of finishing: one left waiting while she did
    something else is planned again.
    """

    RESULT_SECONDS = 60.0
    # A plan still running after this is given up (planner-timeout) and
    # answers None, so the caller's no-route path, retry and alert run
    # instead of waiting for ever.
    MAX_PENDING_SECONDS = 300.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._plans: dict[str, _BackgroundPlan] = {}

    def _stale(self, plan: _BackgroundPlan) -> bool:
        return (plan.finished_at is not None and
                self.clock() - plan.finished_at > self.RESULT_SECONDS)

    def planning(self, name: str) -> bool:
        """Whether ``name`` was started and is still to be collected (a
        result left past RESULT_SECONDS is dropped)."""
        plan = self._plans.get(name)
        if plan is not None and self._stale(plan):
            self.forget(name)
            return False
        return plan is not None

    def forget(self, name: str) -> None:
        """Drop ``name``'s plan under way, if any: a queued one never runs,
        a running one stops at its next probe (PlanCancelled)."""
        plan = self._plans.pop(name, None)
        if plan is not None:
            plan.future.cancel()
            plan.proxy.cancelled.set()

    def get(self, name: str, graph: "WorldGraph", client: AtrinikClient,
            build: Callable[[Any], Any], *, key: Any = None,
            priority: int = planner.PRIORITY_OTHER) -> Any:
        plan = self._plans.get(name)
        if plan is not None and (plan.key != key or self._stale(plan)):
            self.forget(name)
            plan = None
        if plan is None:
            plan = self._plans[name] = _BackgroundPlan(
                graph, client, build, key, self.clock, priority)
        if not plan.future.done():
            recorder = getattr(client, "record_action", None)
            stuck = planner.unstick(self.MAX_PENDING_SECONDS)
            if stuck and recorder is not None:
                recorder("planner-replaced", f"{stuck}; a fresh planner worker took over")
            now = self.clock()
            if plan.running_since is None and plan.future.running():
                plan.running_since = now
            ran = 0.0 if plan.running_since is None else now - plan.running_since
            if ran <= self.MAX_PENDING_SECONDS:
                return PLANNING
            self.forget(name)
            log.warning("%s plan gave no answer in %.0f s; giving up", name, ran)
            if recorder is not None:
                recorder("planner-timeout", f"{name}: no answer in {ran:.0f} s")
            return None
        del self._plans[name]
        plan.proxy.replay_actions(client)
        return plan.future.result()


class TempleFoodPlanner:
    """The temple food ranking (rank_temple_priests) as a background plan
    (BackgroundPlans): ``trip`` answers PLANNING until it is done, then the
    trip to the priest it chose, or None when none is reachable.

    A ranking holds for the priests it passed over and runs to the end
    wherever she walks meanwhile (the trip plans its own route from where
    she is). The priest it chose is remembered for RESUME_SECONDS with the
    map the ranking started from: when a disconnect drops the trip while
    she is still on that map, the next one goes to the same priest at once
    (one route, not a new ranking); anywhere else she ranks again.
    ``forget`` ends both: a trip done or failed, an operator hold, a task
    taking over.
    """

    RESUME_SECONDS = 600.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.plans = BackgroundPlans(clock=clock)
        self._chosen: tuple[DialogueVendor, float, str] | None = None
        # The map the ranking under way started from.
        self._origin_map = ""

    @property
    def planning(self) -> bool:
        """Whether a ranking was started and not yet collected."""
        return self.plans.planning("temple")

    def forget(self) -> None:
        """Drop a ranking under way and the remembered priest."""
        self.plans.forget("temple")
        self._chosen = None

    def trip(self, graph: "WorldGraph", client: AtrinikClient, *,
             exclude: Iterable[str] = ()) -> NavigateThenTask | None | _Planning:
        """The trip to start now, None, or PLANNING (class doc)."""
        excluded = tuple(sorted({name.casefold() for name in exclude}))
        here = client.state.map.path
        now = self.clock()
        if self._chosen is not None:
            priest, chosen_at, ranked_from = self._chosen
            if (now - chosen_at <= self.RESUME_SECONDS and ranked_from == here and
                    priest.name.casefold() not in excluded):
                return temple_food_task(graph, priest)
            self._chosen = None
        if not self.plans.planning("temple"):
            self._origin_map = here
        origin = apartment_exit_origin(client)
        priest = self.plans.get(
            "temple", graph, client,
            lambda proxy: rank_temple_priests(graph, proxy, exclude=excluded,
                                              origin=origin),
            key=excluded, priority=planner.PRIORITY_FOOD)
        if priest is PLANNING or priest is None:
            return priest
        self._chosen = (priest, now, self._origin_map)
        return temple_food_task(graph, priest)


# Paths of private savebeds: an apartment or house bed only works for its owner.
PRIVATE_SAVEBED_MARKERS = ("/apartments/", "/house/")
# Exact walks cost a tile-level route each (about 0.2 s on the live world), so
# only the bed maps within this many map transitions of the nearest bed, at
# most SAVEBED_CANDIDATES of them, get one.
SAVEBED_HOP_SLACK = 2
SAVEBED_CANDIDATES = 6


def _nearest_spot_maps(graph: WorldGraph, source: str,
                       spot_maps: Iterable[str], *, access=False,
                       source_xy: tuple[int, int] | None = None
                       ) -> tuple[int, list[str]] | None:
    """The fewest map transitions from ``source`` to a spot map, and those maps.

    Gates follow ``access``, her access set, as the exact walks do.
    """
    best: tuple[int, list[str]] | None = None
    for spot in spot_maps:
        if source == spot:
            return 0, [spot]
        try:
            hops = len(graph.route(source, spot, access=access,
                                   source_xy=source_xy))
        except (ValueError, KeyError):
            continue
        if best is None or hops < best[0]:
            best = (hops, [spot])
        elif hops == best[0]:
            best[1].append(spot)
    return best


def _edge_arrival(graph: WorldGraph, edge: MapEdge) -> tuple[int, int]:
    """Where a route edge lands on its destination map.

    Exits carry their authored arrival. Tile links between tiled world maps
    keep the default (0, 0) arrival; their crossing square lies just past
    the source map's border and maps onto the opposite edge of the
    destination, exactly as ``_tile_transitions`` computes it.
    """
    destination = graph.nodes.get(edge.destination)
    if edge.kind != "tile" or destination is None:
        if destination is not None and edge.kind == "exit":
            return (edge.destination_x if edge.destination_x >= 0 else destination.enter_x,
                    edge.destination_y if edge.destination_y >= 0 else destination.enter_y)
        return edge.destination_x, edge.destination_y
    source = graph.nodes.get(edge.source)
    width = source.width if source is not None else destination.width
    height = source.height if source is not None else destination.height
    x = (destination.width - 1 if edge.x < 0 else 0 if edge.x >= width else edge.x)
    y = (destination.height - 1 if edge.y < 0 else 0 if edge.y >= height else edge.y)
    return x, y


def _segment_steps(graph: WorldGraph, map_path: str, here: tuple[int, int],
                   edge: MapEdge, access=False) -> int:
    """Moves on ``map_path`` from ``here`` through ``edge``'s departure.

    Follows the authored terrain (``local_path``) so walls, counters and
    winding corridors count; the straight-line distance is only a fallback.
    A tile link's crossing lies past the border: walk to the border square,
    then one move across. Stepping onto an exit is itself the transfer.
    """
    node = graph.nodes.get(map_path)
    target = (edge.x, edge.y)
    if node is not None:
        target = (max(0, min(node.width - 1, edge.x)),
                  max(0, min(node.height - 1, edge.y)))
    path = (graph.local_path(map_path, here, target, allow_locked=access)
            if node is not None else [])
    steps = (len(path) - 1 if path else
             max(abs(target[0] - here[0]), abs(target[1] - here[1])))
    return steps + (1 if edge.kind == "tile" else 0)


def _spawn_points(graph: WorldGraph, map_path: str) -> list[tuple[int, int]]:
    """Walkable authored spawn tiles of a map: where its farm actually is."""
    node = graph.nodes.get(map_path)
    if node is None:
        return []
    return sorted({(spawn.x, spawn.y) for spawn in graph.named_spawns.get(map_path, ())
                   if node.walkable(spawn.x, spawn.y)})


def _reaches(graph: WorldGraph, map_path: str, point: tuple[int, int],
             targets: list[tuple[int, int]], access=False) -> bool:
    """Whether point shares a walkable component with a target (or none known)."""
    if not targets:
        return True
    component = graph._component(map_path, point, allow_locked=access)
    return any(target in component for target in targets)


def _route_walk(graph: WorldGraph, start_map: str, start_xy: tuple[int, int],
                edges: Sequence[MapEdge], access=False) -> int:
    """Moves along a planned route until it enters its destination map."""
    tiles, here, current = 0, start_xy, start_map
    for edge in edges:
        tiles += _segment_steps(graph, current, here, edge, access)
        here, current = _edge_arrival(graph, edge), edge.destination
    return tiles


ApartmentReturns = dict[str, tuple[str, tuple[int, int]]]


def _bed_walk(graph: WorldGraph, bed_map: str, point: tuple[int, int],
              spot_maps: Sequence[str], returns: ApartmentReturns,
              access=False):
    """Generator: yields once per exact route; returns the walk options.

    Each option is (walk, first edge) for one route found, ``first`` being
    the edge the walk leaves ``bed_map`` by, so other beds in the same room
    can be priced by their own steps to each route's first edge. An apartment's exit
    returns to the per-player entrance recorded at runtime, not to the
    graph's static alternatives, so its walk continues from the observed
    return point in ``returns``; without one it is unknown (None). Gates
    follow ``access``, her access set, so a keyed shortcut such as
    Morg'eean's ship counts only with its key.
    """
    targets = {spot: _spawn_points(graph, spot) for spot in spot_maps}
    if bed_map in spot_maps and _reaches(graph, bed_map, point, targets[bed_map],
                                         access):
        return [(0, None)]
    start_map, start_xy, base, first = bed_map, point, 0, None
    if "/apartments/" in bed_map:
        back = returns.get(bed_map)
        node = graph.nodes.get(bed_map)
        if back is None or node is None:
            return []
        exits = [edge for edge in node.edges if edge.kind != "tile" and (
            graph.local_path(bed_map, point, (edge.x, edge.y),
                             allow_locked=access) or
            (edge.x, edge.y) == point)]
        if not exits:
            return []
        first = min(exits, key=lambda edge: _segment_steps(
            graph, bed_map, point, edge, access))
        base = _segment_steps(graph, bed_map, point, first, access)
        start_map, start_xy = back[0], tuple(back[1])
        if start_map in spot_maps and _reaches(graph, start_map, start_xy,
                                               targets[start_map], access):
            return [(base, first)]
    options = []
    for spot in spot_maps:
        try:
            # Arrive in the part of the circuit map the farm's spawns are
            # in: a bed (or arrival) in a disconnected room does not count.
            edges = graph.route_points(start_map, start_xy, spot, targets[spot],
                                       allow_locked=access)
        except (ValueError, KeyError):
            edges = None
        yield
        if not edges:
            continue
        options.append((base + _route_walk(graph, start_map, start_xy, edges,
                                           access),
                        first or edges[0]))
    return options


def savebed_walk(graph: WorldGraph, bed_map: str, bed_xy: tuple[int, int],
                 spot_maps: Iterable[str], *,
                 returns: ApartmentReturns | None = None,
                 access=False) -> int | None:
    """Moves walked from a respawn on this bed to the nearest circuit map.

    Every reachable circuit map is routed and walked along its authored
    paths, through the gates ``access`` opens; None when none is reachable
    (or an apartment's return is unknown).
    """
    steps = _bed_walk(graph, bed_map, tuple(bed_xy), tuple(spot_maps), returns or {},
                      access)
    try:
        while True:
            next(steps)
    except StopIteration as done:
        return min((walk for walk, _ in done.value), default=None)


class SavebedSearch:
    """Find the usable savebed whose respawn is the shortest walk from a spot.

    Public beds (inns, taverns) work for anyone; private ones only in
    ``owned`` maps, and apartments only once their runtime return point is
    known (``returns``). Each exact walk is a tile-level route, so ``step()``
    computes at most one per call and a live caller spreads the search over
    ticks. ``result`` is (map, bed tile, walk tiles) or None; with
    ``current`` (map, tile), ``current_walk`` is that bed's walk. Gates
    follow ``access``, her access set (unlocks.py), in the shortlist and
    the walks alike.
    """

    def __init__(self, graph: WorldGraph, spot_maps: Iterable[str], *,
                 owned: Iterable[str] = (STARTER_APARTMENT,),
                 excluded: Iterable[str] = (),
                 excluded_tiles: Iterable[tuple[str, tuple[int, int]]] = (),
                 current: tuple[str, tuple[int, int]] | None = None,
                 returns: ApartmentReturns | None = None,
                 access=False):
        self.result: tuple[str, tuple[int, int], int] | None = None
        self.current_walk: int | None = None
        self.done = False
        self.access = access
        self._steps = self._search(
            graph, tuple(dict.fromkeys(spot_maps)), set(owned), set(excluded),
            {(path, tuple(point)) for path, point in excluded_tiles}, current,
            dict(returns or {}))

    def step(self) -> bool:
        """Advance by at most one exact route; True once the search is done."""
        if not self.done:
            try:
                next(self._steps)
            except StopIteration:
                self.done = True
        return self.done

    def run(self) -> "SavebedSearch":
        while not self.step():
            pass
        return self

    def _consider(self, bed_map: str, point: tuple[int, int], walk: int) -> None:
        if self.result is None or (walk, bed_map, point) < (
                self.result[2], self.result[0], self.result[1]):
            self.result = (bed_map, point, walk)

    def _search(self, graph, spot_maps, owned, excluded, excluded_tiles, current,
                returns):
        eligible: dict[str, list[tuple[int, int]]] = {}
        hops: dict[str, int] = {}
        for bed_map in sorted(graph.savebeds):
            if bed_map in excluded or bed_map not in graph.nodes:
                continue
            if bed_map not in owned and any(
                    marker in bed_map for marker in PRIVATE_SAVEBED_MARKERS):
                continue
            blocked = graph.nodes[bed_map].blocked
            tiles = [point for point in sorted(graph.savebeds[bed_map])
                     if point not in blocked and (bed_map, point) not in excluded_tiles]
            # Maps whose beds have all failed must not take candidate slots.
            if not tiles:
                continue
            if "/apartments/" in bed_map:
                back = returns.get(bed_map)
                nearest = (None if back is None else
                           _nearest_spot_maps(graph, back[0], spot_maps,
                                              access=self.access,
                                              source_xy=tuple(back[1])))
                if nearest is not None:
                    nearest = (nearest[0] + 1, nearest[1])
            else:
                # Any bed's component may start: the exact walks decide.
                nearest = _nearest_spot_maps(graph, bed_map, spot_maps,
                                             access=self.access)
            if nearest is not None:
                hops[bed_map] = nearest[0]
                eligible[bed_map] = tiles
        yield
        candidates: list[str] = []
        if hops:
            cutoff = min(hops.values()) + SAVEBED_HOP_SLACK
            candidates = sorted((m for m in hops if hops[m] <= cutoff),
                                key=lambda m: (hops[m], m))
        # Nearest maps first, until SAVEBED_CANDIDATES of them are priced: a
        # map whose beds turn out unroutable (the hop count only proves the
        # map is connected) does not use up a slot.
        priced_maps = 0
        for bed_map in candidates:
            if priced_maps >= SAVEBED_CANDIDATES:
                break
            priced = False
            # Beds in one walkable component share everything after their
            # first exit; beds in another component get their own route.
            groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
            access = self.access
            for point in eligible[bed_map]:
                component = graph._component(bed_map, point, allow_locked=access)
                groups.setdefault(min(component) if component else point, []).append(point)
            for group in groups.values():
                anchor = group[0]
                options = yield from _bed_walk(
                    graph, bed_map, anchor, spot_maps, returns, access)
                for point in group:
                    # Each bed takes whichever leg's route suits it best: a
                    # bed beside another exit is priced through that exit.
                    prices = []
                    for walk, first in options:
                        if point == anchor or first is None:
                            prices.append(walk)
                        elif graph.local_path(bed_map, point, (first.x, first.y),
                                              allow_locked=access):
                            prices.append(
                                walk - _segment_steps(graph, bed_map, anchor, first,
                                                      access) +
                                _segment_steps(graph, bed_map, point, first, access))
                    if prices:
                        self._consider(bed_map, point, min(prices))
                        priced = True
            priced_maps += priced
        if current is not None and current[0] in graph.nodes:
            options = yield from _bed_walk(
                graph, current[0], tuple(current[1]), spot_maps, returns,
                self.access)
            self.current_walk = min((walk for walk, _ in options), default=None)


def nearest_savebed(graph: WorldGraph, spot_maps: Iterable[str], *,
                    owned: Iterable[str] = (STARTER_APARTMENT,),
                    excluded: Iterable[str] = (),
                    excluded_tiles: Iterable[tuple[str, tuple[int, int]]] = (),
                    returns: ApartmentReturns | None = None,
                    access=False) -> tuple[str, tuple[int, int], int] | None:
    """The usable savebed whose respawn is the shortest walk from the spot."""
    return SavebedSearch(graph, spot_maps, owned=owned, excluded=excluded,
                         excluded_tiles=excluded_tiles, returns=returns,
                         access=access).run().result


class FarmCircuitTask(BotTask):
    """Rotate between nearby farm maps without abandoning fights or loot."""

    PROGRESS_CHECKPOINT_INTERVAL = 5 * 60
    UPGRADE_SWEEP_INTERVAL = 24 * 60 * 60
    RECALL_SHOP_INTERVAL = 24 * 60 * 60
    # Recall shopping starts only with this much money (wallet_value), and,
    # while a recall quote from the last RECALL_SHOP_INTERVAL is known
    # (recall_quote), only with that price plus RECALL_SHOP_RESERVE.
    RECALL_SHOP_MINIMUM_WALLET = 1_250
    # What recall shopping keeps back after paying (BuyDialogueStockTask).
    RECALL_SHOP_RESERVE = 1_000
    UTILITY_SHOP_INTERVAL = 24 * 60 * 60
    # The heal restock (_needs_heal_restock): from RECALL_REQUIRED_LEVEL,
    # with fewer than HEAL_RESTOCK_LOW usable healing consumables she buys
    # up to HEAL_RESTOCK_TARGET, keeping HEAL_RESTOCK_RESERVE; without a
    # healing quote from the last HEAL_RESTOCK_INTERVAL she starts only
    # with HEAL_RESTOCK_MINIMUM_WALLET, with one only with that price plus
    # the reserve. No seller, no stock she can use, or stock that ran short
    # backs off for HEAL_RESTOCK_INTERVAL; a failed trip for
    # HEAL_RESTOCK_RETRY_SECONDS. While the unprepared hold holds
    # (UnpreparedHold) she goes only by a strict route and from
    # HEAL_RESTOCK_SAFETY's flee threshold up; with no strict route she
    # tries again after HEAL_RESTOCK_STRICT_RETRY_SECONDS.
    HEAL_RESTOCK_LOW = 2
    HEAL_RESTOCK_TARGET = 5
    HEAL_RESTOCK_RESERVE = 1_000
    HEAL_RESTOCK_MINIMUM_WALLET = 1_250
    HEAL_RESTOCK_INTERVAL = 24 * 60 * 60
    HEAL_RESTOCK_RETRY_SECONDS = 2 * 60 * 60
    HEAL_RESTOCK_STRICT_RETRY_SECONDS = 600.0
    HEAL_RESTOCK_SAFETY = (0.85, 0.70)
    # After a restock that reached the low mark, the next waits this long:
    # at high level a fight can use all five, and restocking at once would
    # empty the merchant's stock in back-to-back trips.
    HEAL_RESTOCK_MIN_INTERVAL = 2 * 60 * 60
    # Out of heals and recall, a trip whose route needs the hostile-transit
    # fallback departs only at full HP and SP; meanwhile the farm rests
    # (FarmTask.rest_for_departure), the trip is re-planned this often, and
    # after HEAL_RESTOCK_FULL_WAIT_SECONDS she gives up for
    # HEAL_RESTOCK_RETRY_SECONDS.
    HEAL_RESTOCK_FULL_RECHECK_SECONDS = 30.0
    HEAL_RESTOCK_FULL_WAIT_SECONDS = 600.0
    UPGRADE_SWEEP_WALLET_GROWTH = 5_000
    UPGRADE_SWEEP_POLICY = 8
    BANK_DEPOSIT_MINIMUM = 1_000
    BANK_DEPOSIT_WEALTH_OVERRIDE = 10_000
    ROUTINE_TOWN_TRIP_WEIGHT = 0.85
    MASS_IDENTIFY_WEIGHT = 0.70
    # Within this many tiles of her savebed, a transit recall is pointless.
    TRANSIT_RECALL_BED_RADIUS = 4
    # How long a map where a route death took combat damage is avoided; as
    # long as a route death there skips farm spots behind it.
    ROUTE_HAZARD_SECONDS = self_improvement.ROUTE_DEATH_HAZARD_SECONDS
    # After a failed temple food request, wait this long before another.
    TEMPLE_FOOD_RETRY_SECONDS = 600.0
    FOOD_RESUPPLY_QUANTITY = 25
    # Fewer safe food items than this and she restocks from the nearest
    # merchant, before she is hungry, so she always carries food.
    FOOD_RESERVE_LOW = 3
    # A proactive restock only with room and money for it: under this share of
    # the carrying limit, and with more than the purchases' protected cash.
    FOOD_RESERVE_MAX_LOAD = 0.80
    FOOD_RESERVE_CASH = 1_000

    FOOD_RESUPPLY_NUTRITION = 10_000
    FOOD_RESUPPLY_MAX_QUANTITY = 50
    DEPLETION_SERVICE_THRESHOLD = 3
    # The character level from which trips may cross the crocodile swamps.
    SWAMP_TRANSIT_LEVEL = 18
    MASS_IDENTIFY_BATCH_MINIMUM = 20
    MASS_IDENTIFY_COST = 1_450
    # Hybrid progression is deliberately staged.  Level 10 wizardry improves
    # the existing heal, more than doubles the base mana multiplier compared
    # with an untrained caster, and satisfies greater-healing's level gate if
    # a spellbook drops.  The later level-15 review unlocks the first authored
    # cone/AoE spells without diverting every early kill away from the main
    # weapon school.

    # Live failures exclude an analyzer pick for this long; see the README.
    FARM_DEATH_QUARANTINE_SECONDS = 6 * 60 * 60
    # A savebed nearer the circuit must save at least this walk to be worth a detour.
    SAVEBED_MIN_GAIN_TILES = 20
    FARM_NO_PROGRESS_SECONDS = 10 * 60
    # Time off the farm restarts the no-XP clock instead of counting against it.
    FARM_OBSERVATION_GAP_SECONDS = 60
    OPTIONAL_RARE_DETOUR_INTERVAL = 30 * 60
    # Margins for analyzer picks with aggressive residents, and for the rest.
    AGGRESSIVE_SAFETY = (0.80, 0.95)
    PASSIVE_SAFETY = (0.75, 0.90)

    def __init__(self, graph: WorldGraph,
                 legs: Sequence[tuple[str, str]], *,
                 dwell_seconds: float = 90.0, switch_grace: float = 30.0,
                 level_until: int = 0, combat_skill: str = "",
                 combat_spell: str = "",
                 combat_skill_until_level: int = 0,
                 clear_hostile_route: bool = False,
                 analyzer_driven: bool = False,
                 spot: dict | None = None,
                 backoffs: dict[str, float] | None = None,
                 avoid_priests: Callable[[], Iterable[str]] = tuple,
                 food_planner: "TempleFoodPlanner | None" = None,
                 clock: Callable[[], float] = time.monotonic):
        if not legs:
            raise ValueError("a farm circuit needs at least one leg")
        super().__init__("farm-circuit:" + "+".join(
            path.rsplit("/", 1)[-1] for path, _ in legs))
        self.graph = graph
        self.legs = tuple((path, target) for path, target in legs)
        # Autoplay farms whatever the farming analyzer ranks first for the
        # preferred objective; explicit circuits keep their given legs.
        self.analyzer_driven = bool(analyzer_driven)
        self.spot = spot
        # Analyzer-derived safety applies to every analyzer spot, including one
        # an operator starts by hand (which does not follow later rankings).
        self._analyzer_safety = self.analyzer_driven or spot is not None
        # The character's persisted spot lessons; refreshed on start and tick.
        self._adaptations: dict[str, dict] = {}
        self.dwell_seconds = max(5.0, float(dwell_seconds))
        self.switch_grace = max(0.0, float(switch_grace))
        self.level_until = max(0, int(level_until))
        self.combat_skill = combat_skill
        self.combat_spell = combat_spell
        self.combat_skill_until_level = max(
            0, int(combat_skill_until_level))
        self._auto_combat_build = bool(
            self.analyzer_driven and
            not combat_skill and not combat_spell and
            not combat_skill_until_level)
        self._lore_book_attempts: set[int] = set()
        self._spellbook_attempts: set[int] = set()
        self.clear_hostile_route = bool(clear_hostile_route)
        self.clock = clock
        self.server_clock = ServerGameClock(clock=clock)
        # The hold without a usable heal or a ready recall: its optional
        # trips, and its farms' pulls (each farm shares it).
        self._unprepared_hold = UnpreparedHold(clock=clock)
        self.leg_index = 0
        self.child: NavigateThenTask | None = None
        self._farm_started_at = 0.0
        self._empty_spawn_since = 0.0
        self._current_map_path = ""
        self._starting_exp = 0
        self._current_exp = 0
        self._xp_started_at = 0.0
        self._checkpoint_at = self.clock()
        self._checkpoint_exp = 0
        self._progression_level = 0
        self._farm_zone_last_checked: dict[str, float] = {}
        self._unreachable_targets: dict[int, float] = {}
        self._resupply: NavigateThenTask | None = None
        self._resupply_retry_at = 0.0
        # Whether the running resupply is a shop purchase or temple food.
        self._resupply_kind = ""
        # Route rankings off the event loop (ROUTING 1.12): the temple's
        # (autoplay's own planner, shared: one ranking and one remembered
        # priest across its circuits), and the food, heal and recall
        # merchants'.
        self._food_planner = food_planner or TempleFoodPlanner()
        self._plans = BackgroundPlans()
        self._temple_food_retry_at = 0.0
        self._storage: NavigateThenTask | None = None
        self._storage_retry_at = 0.0
        self._identification: NavigateThenTask | None = None
        self._identification_retry_at = 0.0
        self._capability: InventoryCapabilityTask | None = None
        # Purposes whose last use succeeded (their cooldown is a short pause).
        self._capability_succeeded: set[str] = set()
        self._capability_retry_at: dict[str, float] = {}
        self._bed_binding: NavigateThenTask | None = None
        self._bed_retry_at = 0.0
        self._bed_target: tuple[str, tuple[int, int], int] | None = None
        # Bed tiles that could not be reached or applied, as (map, tile).
        self._bed_failed: set[tuple[str, tuple[int, int]]] = set()
        self._bed_search: tuple[tuple, SavebedSearch] | None = None
        self._selling: NavigateThenTask | None = None
        self._selling_retry_at = 0.0
        # Raising money for the depletion cure from apartment stock: the
        # chest trip and its retry time. The sale and identify-all it leads
        # to follow from what she carries, so a restart loses nothing.
        self._cure_funding: NavigateThenTask | None = None
        self._cure_funding_retry_at = 0.0
        self._banking: NavigateThenTask | None = None
        self._banking_retry_at = 0.0
        self._force_bank_deposit = False
        self._bank_sync: NavigateThenTask | None = None
        self._bank_sync_retry_at = 0.0
        self._shopping: ShopUpgradeSweepTask | None = None
        self._shopping_retry_at = 0.0
        self._recall_shopping: NavigateThenTask | None = None
        self._recall_shopping_retry_at = 0.0
        self._utility_shopping: NavigateThenTask | None = None
        self._utility_shopping_retry_at = 0.0
        self._heal_restock: NavigateThenTask | None = None
        # Only for a strict (unprepared) plan that found no seller; every
        # other backoff is persisted (GameState.heal_restock_retry_until).
        self._heal_restock_retry_at = 0.0
        # Since when a hostile-transit restock waits for full HP and SP.
        self._heal_restock_full_since: float | None = None
        self._ship_key: MorgeeanShipKeyTask | None = None
        self._ship_key_retry_at = 0.0
        self._spell_purchase: NavigateThenTask | None = None
        self._spell_retry_at = 0.0
        self._cure: NavigateThenTask | None = None
        self._cure_retry_at = 0.0
        self._cure_apply_at = 0.0
        self._disease_message_index = 0
        self._disease_suspected = False
        self._restoration: NavigateThenTask | None = None
        self._restoration_retry_at = 0.0
        self._expedition_origin: tuple[str, int, int] | None = None
        self._expedition_return: NavigateTask | None = None
        self._expedition_abort_reason = ""
        self._decision_history_index = 0
        self._decision_history_time = 0.0
        self._tier_progress_at: float | None = None
        # Last time the no-XP watchdog observed the farm; a longer gap means
        # the character was away (maintenance, travel, reconnect).
        self._tier_observed_at = 0.0
        # Failures recorded before this circuit existed were handled by
        # whichever circuit saw them; a persisted ledger replay is not new.
        self._failure_epoch = time.time()
        # The newest death after which combat mode was turned off, and
        # whether she is held for a route-death verdict.
        # A circuit built right after a death (autoplay replaces one that
        # ran out of spots) still turns combat mode off for that death.
        self._combat_cleared_after = (self._failure_epoch -
                                      ROUTE_DEATH_VERDICT_WAIT_SECONDS)
        self._verdict_hold = False
        self._tier_progress_exp = 0
        # Priests to pass over for temple food (autoplay's failed trips).
        self.avoid_priests = avoid_priests
        # A rebuilt circuit starts with fresh service tasks but keeps the
        # backoffs of the circuit it replaces (see backoffs()).
        for name, until in (backoffs or {}).items():
            if name in self.BACKOFF_ATTRIBUTES:
                setattr(self, name, max(getattr(self, name), float(until)))

    # Maintenance retry deadlines (on ``clock``) a replacement circuit
    # inherits. Live: autoplay rebuilt the circuit after every temple food
    # trip, and each new one walked straight back to a full apartment chest.
    # Food deadlines are left out: a fresh circuit feeds her as soon as she
    # is hungry, rather than farming hungry while an old backoff runs out.
    BACKOFF_ATTRIBUTES = (
        "_storage_retry_at",
        "_identification_retry_at", "_bed_retry_at", "_selling_retry_at",
        "_cure_funding_retry_at", "_banking_retry_at", "_bank_sync_retry_at",
        "_shopping_retry_at", "_recall_shopping_retry_at",
        "_utility_shopping_retry_at", "_ship_key_retry_at", "_spell_retry_at",
        "_cure_retry_at", "_restoration_retry_at", "_heal_restock_retry_at",
    )

    def backoffs(self) -> dict[str, float]:
        """The maintenance retry deadlines still pending, for a successor."""
        now = self.clock()
        return {name: getattr(self, name) for name in self.BACKOFF_ATTRIBUTES
                if getattr(self, name) > now}

    def _observe_farm_death(self, client: AtrinikClient) -> bool:
        """Quarantine an analyzer pick after a death or an emergency recall."""
        history = getattr(client, "decision_history", ())
        if self._decision_history_index > len(history):
            self._decision_history_index = 0
        records = history[self._decision_history_index:]
        # Once the bounded ledger reaches its cap, appending a decision also
        # removes its oldest entry and its length no longer changes. Recover
        # those new records by timestamp instead of silently observing an
        # empty slice forever.
        if (not records and history and
                float(history[-1].get("time", 0.0) or 0.0) >
                self._decision_history_time):
            records = [
                record for record in history
                if float(record.get("time", 0.0) or 0.0) >
                self._decision_history_time
            ]
        self._decision_history_index = len(history)
        if history:
            self._decision_history_time = max(
                self._decision_history_time,
                float(history[-1].get("time", 0.0) or 0.0))
        active_paths = {path for path, _ in self.legs}
        failure = next((record for record in reversed(records)
                        if record.get("action") in ("death", "emergency-recall") and
                        float(record.get("time", 0.0) or 0.0) >= self._failure_epoch),
                       None)
        if failure is None:
            return False
        failure_map = str(failure.get("map", ""))
        path = failure_map if failure_map in active_paths else ""
        # The destination can be suitable while its only approach is not (the
        # live Old Outpost trial proved exactly this). During the ordinary farm
        # NavigateThen child, attribute a transit failure to the requested
        # destination so autoplay moves on instead of replaying the same lethal
        # road. Maintenance children are held separately and never reach this
        # (nor the seam rule below: _seam_failure_leg).
        if (not path and self.child is not None and
                self.child.navigation.destination in active_paths and
                self.child.navigation.status != TaskStatus.COMPLETE):
            path = self.child.navigation.destination
        seam = False
        if not path:
            path = self._seam_failure_leg(client, failure)
            seam = bool(path)
        if not path:
            return False
        what = "combat death" if failure.get("action") == "death" else "emergency recall"
        if self._analyzer_safety:
            # Analyzer spots are kept: review what happened and adapt.
            return self._learn_from_failure(client, failure, path, what,
                                            seam=seam)
        self._quarantine_farm_path(
            client, path, f"{what} on {failure_map or 'unknown map'}")
        return True

    RECALL_STATE = ("_emergency_recall_source", "_emergency_recall_apply_at",
                    "_emergency_recall_cast_at", "_emergency_recall_origin",
                    "_emergency_recall_sent_at", "_emergency_recall_feedback",
                    "_recall_wand_fired")

    def _spot_key(self) -> str:
        return self_improvement.spot_key(self.spot, self.legs)

    @staticmethod
    def _entered_map_at(client: AtrinikClient, path: str, before: float) -> float | None:
        """When she arrived on ``path``, last before ``before``.

        The client's map arrivals are authoritative. Without them, the first
        record of her on ``path`` since she was last elsewhere: a step is
        recorded on the map it leaves, so the last record elsewhere is not
        yet proof of arrival. None when nothing shows her there.
        """
        arrivals = [(stamp, where) for stamp, where in getattr(client, "map_arrivals", ())
                    if stamp <= before]
        if arrivals:
            stamp, where = arrivals[-1]
            return stamp if where == path else None
        records = sorted(
            ((float(record.get("time", 0.0) or 0.0), record.get("map"))
             for record in (*getattr(client, "action_history", ()),
                            *getattr(client, "decision_history", ()))
             if record.get("map")),
            key=lambda entry: entry[0])
        entered = None
        for stamp, where in records:
            if stamp > before:
                break
            if where != path:
                entered = None
            elif entered is None:
                entered = stamp
        return entered

    # A death or emergency recall off the spot's maps this soon after she
    # was on one of them counts for the spot (_seam_failure_leg), and on a
    # seam neighbour or a map of the same bounded area, this soon.
    FARM_SEAM_FAILURE_SECONDS = 30.0
    FARM_AREA_FAILURE_SECONDS = 120.0
    # The maintenance trips _tick_running_service runs: while one is under
    # way, the farm child is not ticked, and a failure is not the spot's.
    SERVICE_ATTRIBUTES = (
        "_bank_sync", "_capability", "_storage", "_identification",
        "_bed_binding", "_resupply", "_selling", "_cure_funding", "_banking",
        "_shopping", "_recall_shopping", "_utility_shopping", "_heal_restock",
        "_ship_key", "_spell_purchase", "_cure", "_restoration")

    def _seam_failure_leg(self, client: AtrinikClient, failure: dict) -> str:
        """The leg map a failure just off the spot's maps counts for, or "".

        Once she is at the spot (the farm child's walk complete), a death
        or emergency recall counts for it when it happened within
        FARM_SEAM_FAILURE_SECONDS of her being on one of its maps (map
        arrivals, else her records), or on a map tiled to one of them
        (a seam neighbour) or of the same bounded area (map_areas) within
        FARM_AREA_FAILURE_SECONDS: the leg she was on last. Never during a
        maintenance trip (SERVICE_ATTRIBUTES): the farm child is not ticked
        then, so its walk still reads complete. Live, on 2026-11-25, a retreat
        from world_16_74 stepped across the seam onto world_17_74 and she
        died there at 16:15:19; the review, the kit failure and its cause
        were never recorded, because only a leg map or the walk's
        destination counted.
        """
        child = self.child
        if (child is None or child.navigation.status != TaskStatus.COMPLETE or
                any(getattr(self, name, None) is not None
                    for name in self.SERVICE_ATTRIBUTES)):
            return ""
        failure_map = str(failure.get("map", ""))
        when = float(failure.get("time", 0.0) or 0.0)
        legs = [path for path, _ in self.legs]
        if not failure_map or not legs or failure_map in legs:
            return ""
        arrivals = sorted((float(stamp), str(where)) for stamp, where in
                          getattr(client, "map_arrivals", ()) if stamp <= when)
        stamps = [(float(record.get("time", 0.0) or 0.0), record.get("map"))
                  for record in (*getattr(client, "action_history", ()),
                                 *getattr(client, "decision_history", ()))
                  if isinstance(record, dict)]

        def last_leg(window: float) -> str:
            """The leg she was on last, if she left it within ``window``."""
            recent = ""
            for index, (_, where) in enumerate(arrivals):
                left = (arrivals[index + 1][0] if index + 1 < len(arrivals)
                        else when)
                if where in legs and when - left <= window:
                    recent = where
            if recent:
                return recent
            return max(((stamp, where) for stamp, where in stamps
                        if where in legs and 0.0 <= when - stamp <= window),
                       default=(0.0, ""))[1]

        recent = last_leg(self.FARM_SEAM_FAILURE_SECONDS)
        if recent:
            return recent
        neighbours = getattr(self.graph, "_seam_neighbours", None)
        nearby = neighbours is not None and any(
            failure_map == neighbour
            for leg in legs for neighbour, _, _ in neighbours(leg))
        if not nearby and not (map_areas(self.graph, (failure_map,)) &
                               map_areas(self.graph, legs)):
            return ""
        return last_leg(self.FARM_AREA_FAILURE_SECONDS)

    def _learn_from_failure(self, client: AtrinikClient, failure: dict,
                            path: str, what: str, *,
                            seam: bool = False) -> bool:
        """Review a failure at an analyzer spot and adapt instead of leaving it.

        Returns True only for the last resort, a short pause after repeated
        failures with every tactic already at its maximum.
        """
        decisions = list(getattr(client, "decision_history", ()))
        when = float(failure.get("time", 0.0) or 0.0)
        # Only this life counts: a starvation or fight before the previous
        # death must not explain this one. Every earlier death record splits
        # lives, however soon before this failure; only this failure's own
        # record is left out.
        previous_death = max(
            (float(record.get("time", 0.0) or 0.0) for record in decisions
             if isinstance(record, dict) and record.get("action") == "death" and
             record is not failure and record != failure and
             float(record.get("time", 0.0) or 0.0) <= when), default=0.0)
        messages = [entry for entry in getattr(client.state, "messages", ())
                    if float(entry[0]) > previous_death]
        this_life = [record for record in decisions if isinstance(record, dict) and
                     float(record.get("time", 0.0) or 0.0) > previous_death]
        # A failure across the spot's seam (_seam_failure_leg) is the
        # spot's own fight, not one on the way to it: its cause is judged
        # as on the spot, and it teaches no route hazard.
        spot_paths = [leg for leg, _ in self.legs]
        if seam:
            spot_paths.append(str(failure.get("map", "")))
        review = self_improvement.review_failure(
            messages, this_life, failure, spot_paths,
            monster=self.graph.is_monster)
        # Hits stamped before she entered the death map were taken elsewhere:
        # only damage taken on it makes that map the hazard.
        entered = self._entered_map_at(client, review.map, review.time)
        # Up to the death (with message slack), never past her next arrival:
        # a hit after respawning elsewhere is not the death map's.
        left = min((stamp for stamp, _ in getattr(client, "map_arrivals", ())
                    if entered is not None and stamp > entered), default=review.time + 2.0)
        hits = {} if entered is None else self_improvement.hits_between(
            messages, max(entered, review.time - self_improvement.REVIEW_WINDOW_SECONDS),
            min(review.time + 2.0, left))
        on_map = sum(hits.values())
        if review.cause == "route" and on_map > 0 and review.map:
            # Killed crossing a map on the way: route around it for a while
            # instead of walking back into the same fight after respawning.
            until = time.time() + self.ROUTE_HAZARD_SECONDS
            remember = getattr(client, "remember_route_hazard", None)
            if remember is not None:
                remember(review.map, until)
            else:
                client.state.route_hazards[review.map] = until
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("route-hazard-learned",
                         f"{review.map} for {self.ROUTE_HAZARD_SECONDS / 3600:.0f} h: "
                         f"{on_map} damage from " + ", ".join(sorted(hits)))
        key = self._spot_key()
        adaptations = getattr(client.state, "farm_adaptations", {}) or {}
        kit = current_combat_kit(client)
        record, pause = self_improvement.learn(
            adaptations.get(key, {}), review,
            kit=kit.describe() if kit is not None else None,
            maps=[leg for leg, _ in self.legs], map_damage=on_map,
            areas=map_areas(self.graph, (leg for leg, _ in self.legs)))
        setter = getattr(client, "set_farm_adaptation", None)
        if setter is not None:
            setter(key, record)
        else:
            adaptations[key] = record
        self._adaptations = getattr(client.state, "farm_adaptations", {}) or {}
        remedies =", ".join(self_improvement.remedies(review))
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farm-failure-review",
                     f"{what} at {(self.spot or {}).get('name', key)}: {review.cause} "
                     f"({review.detail}); adaptation level {record['level']}; "
                     f"improve: {remedies}")
        log.warning("learning from %s at %s: %s (%s); adaptation level %s",
                    what, (self.spot or {}).get("name", key), review.cause,
                    review.detail, record["level"])
        # Rebuild the farm child so the tighter margins apply immediately, but
        # keep any Word of Recall still in flight: the recall decision is
        # recorded when it is confirmed, and a fresh task would fire another.
        # A pause rebuilds it too: the old task's engagement with the pack
        # that just killed her otherwise reads as a fight still being
        # finished (_busy_finishing_fight_or_loot), which holds off the
        # farm-spot choice that must move her away.
        if self.child is not None:
            previous = getattr(self.child, "task", None)
            self.child = self._new_child()
            for name in self.RECALL_STATE:
                if hasattr(previous, name):
                    setattr(self.child.task, name, getattr(previous, name))
            self._farm_started_at = 0.0
            self._empty_spawn_since = 0.0
        if pause:
            self._quarantine_farm_path(
                client, path, "repeated failures at the maximum adaptation level",
                seconds=self_improvement.PAUSE_SECONDS)
            self._quarantine_farm_area(
                client, "repeated failures at the maximum adaptation level",
                seconds=self_improvement.PAUSE_SECONDS)
            return True
        if kit_hazardous_spot(client, self.spot, self.legs, self.graph):
            # Her kit cannot hold this spot: choose again now. It stays
            # skipped while her survivability is the same
            # (analyzer_farm_choice).
            if recorder is not None:
                recorder("farm-kit-hazard",
                         f"{(self.spot or {}).get('name', key)}: "
                         f"{self_improvement.KIT_HAZARD_FAILURES} pack, "
                         "out-damaged or approach-death failures in this "
                         "area with this kit")
            return True
        return False

    def _quarantine_farm_path(self, client: AtrinikClient, path: str,
                              reason: str, *, seconds: float | None = None) -> None:
        seconds = self.FARM_DEATH_QUARANTINE_SECONDS if seconds is None else seconds
        until = time.time() + seconds
        setter = getattr(client, "quarantine_farm_zone", None)
        if setter is not None:
            setter(path, until)
        else:
            client.state.farm_zone_quarantine[path] = until
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farm-zone-quarantine",
                     f"{path} until={int(until)} after {reason}")
        log.warning("autoplay quarantined farm map for %.1f hours: %s (%s)",
                    seconds / 3600, path, reason)

    def _quarantine_farm_area(self, client: AtrinikClient, reason: str, *,
                              seconds: float) -> None:
        """Quarantine every map of the spot's bounded areas (map_areas) for
        ``seconds``, as its pause quarantines the failure's leg map: a
        sibling circuit or single map of the same area is the same fight.
        Live, on 2026-11-25, the Burnt Forest pauses covered one map each,
        and she died on three different Burnt Forest maps within 90
        minutes. Recorded once (farm-area-quarantine); an area too large
        for map_areas is never quarantined whole."""
        areas = map_areas(self.graph, (path for path, _ in self.legs))
        bounded = area_maps(self.graph)
        maps = sorted(set().union(*(bounded[area] for area in areas))
                      if areas else ())
        if not maps:
            return
        until = time.time() + seconds
        quarantine = client.state.farm_zone_quarantine
        for path in maps:
            quarantine[path] = max(float(quarantine.get(path, 0.0) or 0.0), until)
        # One persist for the whole area: the setter saves every entry.
        setter = getattr(client, "quarantine_farm_zone", None)
        if setter is not None:
            setter(maps[-1], quarantine[maps[-1]])
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farm-area-quarantine",
                     f"{', '.join(sorted(area.split('|', 1)[-1] for area in areas))}: "
                     f"{len(maps)} maps until={int(until)} after {reason}")
        log.warning("autoplay quarantined farm area for %.1f hours: %s (%d maps; %s)",
                    seconds / 3600, ", ".join(sorted(areas)), len(maps), reason)

    # How long a spot whose every resident kind is out of her league stays
    # quarantined (_observe_out_of_league): the failure review's pause.
    FARM_OUT_OF_LEAGUE_QUARANTINE_SECONDS = self_improvement.PAUSE_SECONDS

    def _observe_out_of_league(self, client: AtrinikClient) -> bool:
        """Leave a spot with nothing left she may pull.

        Out-healed disengages mark the kinds that hit her out of her league
        (FarmTask.out_healed, out_of_league_kinds) for
        FarmTask.OUT_OF_LEAGUE_SECONDS: she neither pulls nor engages them.
        Once every resident kind of the spot (the authored, not peaceful,
        spawns of its maps, by name or archetype) is marked while she is
        there (a spawn that can roll several kinds counts once all are),
        the spot's maps are quarantined for
        FARM_OUT_OF_LEAGUE_QUARANTINE_SECONDS (farm-out-of-league), so the
        analyzer choice (_upgrade_progression) picks another spot and her
        ordinary walk takes her off the map by its route; with none left,
        the circuit ends as for any quarantined spot. Live, on 2026-11-25,
        she stayed on world_16_74 with every kind there marked, ran its
        edges from slow treants and was cornered at (19,23).
        """
        if not self.analyzer_driven or self.child is None:
            return False
        legs = [path for path, _ in self.legs]
        if (self.child.navigation.status != TaskStatus.COMPLETE or
                client.state.map.path not in legs):
            return False
        store = getattr(client, "out_of_league_kinds", None) or {}
        now = time.monotonic()
        marked = {FarmTask._kind_key(kind) for kind, until in store.items()
                  if float(until) > now}
        if not marked:
            return False
        quarantine = getattr(client.state, "farm_zone_quarantine", {}) or {}
        if all(float(quarantine.get(path, 0.0) or 0.0) > time.time()
               for path in legs):
            return False
        residents = [spawn for path in legs
                     for spawn in self.graph.named_spawns.get(path, ())
                     if not spawn.peaceful]
        if not residents:
            return False
        kinds = set()
        for spawn in residents:
            names = {FarmTask._kind_key(name)
                     for name in (spawn.named, *spawn.candidates) if name}
            if not names or not names <= marked:
                return False
            kinds.add(spawn.named)
        detail = (f"{(self.spot or {}).get('name', self._spot_key())}: every "
                  f"resident kind is out of her league "
                  f"({', '.join(sorted(kinds))})")
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farm-out-of-league", detail)
        for path in legs:
            self._quarantine_farm_path(
                client, path, "every resident kind out of her league",
                seconds=self.FARM_OUT_OF_LEAGUE_QUARANTINE_SECONDS)
        return True

    def _observe_stalled_farm(self, client: AtrinikClient,
                              farm: FarmTask) -> bool:
        """Exclude an analyzer pick which produces no character XP for ten minutes."""
        if (not self.analyzer_driven or
                self.child is None or
                client.state.map.path != self.child.navigation.destination):
            self._tier_progress_at = None
            return False
        now = self.clock()
        away = now - self._tier_observed_at > self.FARM_OBSERVATION_GAP_SECONDS
        self._tier_observed_at = now
        if (self._tier_progress_at is None or away or
                self._current_exp > self._tier_progress_exp):
            self._tier_progress_at = now
            self._tier_progress_exp = self._current_exp
            return False
        if (now - self._tier_progress_at < self.FARM_NO_PROGRESS_SECONDS or
                self._busy_finishing_fight_or_loot(client, farm)):
            return False
        path = self.child.navigation.destination
        self._quarantine_farm_path(
            client, path,
            f"no character XP for {self.FARM_NO_PROGRESS_SECONDS}s")
        self._tier_progress_at = None
        return True

    @staticmethod
    def _combat_progression_level(client: AtrinikClient) -> int:
        """Use the equipped weapon school's level for authored farm bands."""
        levels = []
        for slot in (c.EQUIP_WEAPON, c.EQUIP_WEAPON_RANGED):
            weapon = client.state.items.get(
                client.state.equipment.get(slot, 0))
            if weapon is None or not weapon.required_skill_tag:
                continue
            skill = client.state.items.get(weapon.required_skill_tag)
            if skill is None or skill.item_type != c.TYPE_SKILL:
                continue
            levels.append(int(skill.extra.get("level", 0) or 0))
        return max(levels, default=int(
            client.state.stats.get("level", 0) or 0))

    @staticmethod
    def _skill_level(client: AtrinikClient, name: str) -> int:
        # One reading for the circuit and the farming analyzer.
        return farm_analyzer_plan.owned_skill_level(client.state, name)

    def _automatic_combat_plan(
            self, client: AtrinikClient) -> tuple[str, str, int]:
        """Choose the next bounded secondary-combat milestone."""
        if not self._auto_combat_build:
            return (self.combat_skill, self.combat_spell,
                    self.combat_skill_until_level)
        # Shared with the farming analyzer's profile (farm_analyzer.py).
        return farm_analyzer_plan.automatic_training_plan(client.state)

    def _review_combat_build(self, client: AtrinikClient,
                             farm: FarmTask) -> bool:
        """Reconfigure secondary training only between fights and loot."""
        if not self._auto_combat_build:
            return False
        desired = self._automatic_combat_plan(client)
        current = (self.combat_skill, self.combat_spell,
                   self.combat_skill_until_level)
        if desired == current or self._busy_finishing_fight_or_loot(
                client, farm):
            return False
        self.combat_skill, self.combat_spell, target = desired
        self.combat_skill_until_level = target
        self.child = self._new_child()
        self._farm_started_at = 0.0
        self._empty_spawn_since = 0.0
        self._tier_progress_at = None
        self._tier_progress_exp = self._current_exp
        detail = (f"{desired[0]} via {desired[1]} until L{desired[2]}"
                  if desired[0] else "main weapon XP")
        log.info("combat build review selected %s", detail)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("combat-build-review", detail)
        return True

    def _upgrade_progression(self, client: AtrinikClient,
                             farm: FarmTask) -> bool:
        """Follow the analyzer's best usable spot for the preferred objective."""
        level = self._combat_progression_level(client)
        self._progression_level = level
        if not self.analyzer_driven:
            return False
        # No whole-world search mid-fight or mid-loot; while held for this
        # verdict, a corpse her defence left, or a target the server
        # selected that is not fighting her, does not hold it off.
        busy = self._busy_finishing_fight_or_loot(
            client, farm, held=self._verdict_hold)
        # An overdue verdict still undecided is searched even mid-fight,
        # that verdict alone: town routes are not planned mid-fight.
        overdue = (route_death_overdue(client) and
                   route_death_blocked_maps(client, self.graph, search=False) is None)
        choice = analyzer_farm_choice(
            client, self.graph, search=not busy,
            search_verdict=not busy or overdue,
            current_legs=tuple(self.legs))
        if choice is None:
            # Nothing usable is ranked. Never keep farming a spot that live
            # failures just excluded (quarantined, or only reachable through
            # a map she just died crossing): finish, and autoplay holds
            # position safely until a usable spot is ranked again. While a
            # route death is still being judged, or a fight or loot is
            # being finished, keep the current plan.
            blocked = route_death_blocked_maps(client, self.graph, search=False)
            if blocked is None or busy:
                return False
            quarantine = getattr(client.state, "farm_zone_quarantine", {}) or {}
            now = time.time()
            if any(float(quarantine.get(path, 0.0) or 0.0) > now or
                   path in blocked for path, _ in self.legs):
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("farm-spots-exhausted",
                             "every ranked spot is quarantined or unusable")
                log.warning("every ranked farm spot is quarantined; holding")
                self.complete()
                return True
            return False
        objective, desired, spot = choice
        if desired == self.legs:
            self.spot = spot
            return False
        if busy:
            return False
        self.legs = desired
        self.spot = spot
        self.leg_index = self._next_ready_leg(0, level)
        self.child = self._new_child()
        self._farm_started_at = 0.0
        self._empty_spawn_since = 0.0
        self._tier_progress_at = None
        self._tier_progress_exp = self._current_exp
        self.name = "farm-circuit:" + "+".join(
            path.rsplit("/", 1)[-1] for path, _ in self.legs)
        log.info("farming analyzer selected %s for %s: %s",
                 spot.get("name", "?"), objective,
                 ", ".join(path.rsplit("/", 1)[-1] for path, _ in self.legs))
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farm-spot-selected",
                     f"{spot.get('name', '?')} ({objective}): "
                     + ", ".join(path for path, _ in self.legs))
        return True

    async def _recover_failed_navigation(
            self, client: AtrinikClient, farm: FarmTask) -> bool:
        """Keep escaping a live threat instead of dropping to idle safety."""
        assert self.child is not None
        navigation = self.child.navigation
        if navigation.status != TaskStatus.FAILED:
            return False
        m = client.state.map
        threats = [
            (max(abs(x - m.width // 2), abs(y - m.height // 2)),
             x, y, obj)
            for x, y, obj in m.targets(friendly=False)
        ]
        if not threats and not recent_hostile_contact(client, seconds=15.0):
            return False
        error = navigation.error
        navigation.status = TaskStatus.RUNNING
        navigation.error = ""
        navigation.route = []
        navigation._issued_goal = None
        navigation._issued_click = None
        navigation._last_progress = time.monotonic()
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("navigation-route-failure-escape", error)
        if await self.child._defend_while_navigating(client):
            return True
        if threats and await self.child._retreat_step(
                client, threats, avoid_backtrack=True):
            return True
        await farm.low_health_retreat(client)
        # Keep the circuit alive during the bounded hostile-contact window even
        # if movement is awaiting acknowledgement. The next tick retries from
        # the authoritative position instead of handing a fight to idle safety.
        return True

    async def _clear_combat_after_death(self, client: AtrinikClient) -> None:
        """Turn combat mode off once after each death.

        The server turns it on with every hit (attack_player_retarget) and
        kill_player leaves it on, so she respawned still "in combat": the
        client's flag then read as a fight that never ended.
        """
        death = max((float(record.get("time", 0.0) or 0.0)
                     for record in getattr(client, "decision_history", ())
                     if isinstance(record, dict) and record.get("action") == "death"),
                    default=0.0)
        if death <= self._combat_cleared_after:
            return
        self._combat_cleared_after = death
        setter = getattr(client, "set_combat", None)
        if setter is not None and getattr(client.state, "combat", False):
            await setter(False)

    def _route_crosses(self, client: AtrinikClient, maps: set[str]) -> bool:
        """Whether the current leg is on, or routed through, one of ``maps``.

        The route is the leg navigation's own, planned from where she stands
        exactly as its next tick would plan it; an unroutable leg is left to
        navigation's own failure handling.
        """
        if any(path in maps for path, _ in self.legs):
            return True
        if self.child is None:
            return False
        navigation = self.child.navigation
        if client.state.map.path == navigation.destination:
            return False
        if not navigation.route or navigation.route[0].source != client.state.map.path:
            try:
                navigation.route = navigation._plan(client)
            except ValueError:
                return False
        return any(edge.destination in maps for edge in navigation.route)

    async def _hold_for_route_death_verdict(self, client: AtrinikClient) -> bool:
        """Hold her in transit while a route death's verdict is pending.

        route_death_blocked_maps is undecided (None) until its reachability
        searches have run, and they wait while she is in contact or busy.
        _upgrade_progression keeps the current plan meanwhile; that plan
        must not lead her back across a map that just killed her. Live,
        Sera respawned and walked straight back towards the underground city
        entrance for three minutes while the verdict stayed undecided. Only
        a leg on, or a route through, a counting route-death map is held.
        The circuit's transit tick asks this after recall, healing and the
        low-health retreat, so she still uses them; here she defends
        herself. While held, a corpse or loot left by that defence does not
        count as busy (_upgrade_progression), and from
        ROUTE_DEATH_VERDICT_WAIT_SECONDS after the death the verdict is
        searched despite contact or a fight (route_death_overdue), so the
        hold lasts at most that plus a few rate-limited searches.
        """
        if not self.analyzer_driven or self.child is None:
            self._verdict_hold = False
            return False
        deaths = route_death_maps(client)
        blocked = (route_death_blocked_maps(client, self.graph, search=False)
                   if deaths else frozenset())
        # Undecided, or decided against this circuit while a fight delays
        # the switch (_upgrade_progression): either way, not across it.
        pending = blocked is None or any(path in blocked for path, _ in self.legs)
        if not deaths or not pending or not self._route_crosses(client, deaths):
            self._verdict_hold = False
            return False
        if not self._verdict_hold:
            self._verdict_hold = True
            await client.clear_actions()
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("farm-route-death-hold",
                         "waiting for the route-death verdict before crossing "
                         + ", ".join(sorted(deaths)))
        await self.child._defend_while_navigating(client)
        return True

    def _exclude_unreachable_leg(self, client: AtrinikClient,
                                 farm: FarmTask) -> bool:
        """Quarantine an analyzer pick whose map cannot be reached, then reselect."""
        assert self.child is not None
        navigation = self.child.navigation
        self._quarantine_farm_path(
            client, navigation.destination, f"unreachable: {navigation.error}")
        return self._upgrade_progression(client, farm)

    def _reset_dwell_off_farm(self, map_path: str) -> bool:
        """Count dwell only while physically present on the farm map."""
        if (self.child is None or
                map_path == self.child.navigation.destination):
            return False
        self._farm_started_at = 0.0
        self._empty_spawn_since = 0.0
        return True

    async def _transit_emergency_recall(self, client: AtrinikClient,
                                        farm: FarmTask) -> bool:
        """Word of Recall on the way to a farm map, by the farm's own rule.

        FarmTask.tick decides an emergency recall only on the farm map, so
        on the road she only healed and retreated. Live, Sera fled at 8-16
        hit points with a seven-charge recall wand in her pack and died six
        times in 13 minutes crossing one map. The same farm task decides
        here, with its device readiness, charge and in-flight bookkeeping
        (an applied wand is fired on a later tick, a fired recall is not
        repeated, even across a map change), and it records the same
        emergency-recall decision. It is asked every transit tick except
        beside her savebed: it acts only on hostile contact at a critical
        ratio, or to finish a recall it already started. Threats are
        counted as FarmTask counts them (FarmTask.transit_threat_count).
        """
        path = client.state.map.path
        # Recall leads to the savebed: pointless beside the bed itself. The
        # rest of an outdoor savebed map is ordinary road (the live bed is
        # on world_7_79); a recall that just landed is held off by the
        # farm task's 30 s in-flight guard.
        bed = getattr(client.state, "savebed_xy", None)
        if (path and bed and path == getattr(client.state, "savebed_map", "") and
                max(abs(client.state.map.world_x - bed[0]),
                    abs(client.state.map.world_y - bed[1])) <=
                self.TRANSIT_RECALL_BED_RADIUS):
            return False
        hp = int(client.state.stats.get("hp", 0) or 0)
        maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
        nearby_count = farm.transit_threat_count(
            client, self.graph.nodes.get(path))
        hostile_contact = unresolved_hostile_contact(client, seconds=10.0)
        if await farm.emergency_recall(
                client, ratio=hp / maxhp, nearby_count=nearby_count,
                hostile_contact=hostile_contact,
                healing_outpaced=hostile_contact and healing_outpaced(client)):
            farm._last_action = time.monotonic()
            return True
        return False

    def _retry_unavoidable_return_route(self, client: AtrinikClient) -> bool:
        """Broaden a failed level-18+ return only after safe routing fails."""
        if self.child is None:
            return False
        navigation = self.child.navigation
        level = self._combat_progression_level(client)
        if (navigation.status != TaskStatus.FAILED or
                not navigation.error.startswith("no component route") or
                navigation.allow_ranged_hazard_fallback or level < 18):
            return False
        navigation.allow_ranged_hazard_fallback = True
        navigation.status = TaskStatus.READY
        navigation.error = ""
        navigation.route = []
        navigation._issued_goal = None
        navigation._issued_click = None
        navigation._last_progress = time.monotonic()
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "farm-return-threat-fallback",
                f"destination={navigation.destination} combat-level={level}")
        log.info(
            "no pack-free farm return exists; enabling careful level-%s "
            "route clear to %s", level, navigation.destination)
        return True

    def _new_child(self) -> NavigateThenTask:
        zone, target = self.legs[self.leg_index]
        priorities = self.graph.farm_priorities(zone, target)
        node = self.graph.nodes.get(zone)
        peaceful = node.peaceful_identities if node is not None else set()

        def neutral_spawn(spawn: NamedSpawn) -> bool:
            identities = (spawn.named,) + tuple(spawn.candidates)
            return any(
                self.graph._semantic_name(identity) in peaceful
                for identity in identities)

        neutral_targets = bool(priorities) and all(
            neutral_spawn(spawn) for spawn in priorities)
        aggressive_spawns = [
            spawn for spawn in priorities if not neutral_spawn(spawn)]
        # Pull squares keep clear of every aggressive resident of the map, not
        # only the spawns this leg targets: Sera died seven times in forty
        # minutes to orc packs whose other members were never part of the
        # clearance check.
        residents = [
            spawn for spawn in self.graph.named_spawns.get(zone, [])
            if not spawn.peaceful and not neutral_spawn(spawn)]
        # Aggressive residents are pulled one at a time from boundary squares
        # outside every other resident's authored detection radius; passive
        # spawns keep the adjacent patrol.
        aggressive = self._analyzer_safety and (bool(aggressive_spawns) or float(
            (self.spot or {}).get("aggressive_spawns", 0) or 0) > 0)
        isolated_radius_pulls = aggressive and bool(aggressive_spawns)
        patrol = []
        pull_avoidance: dict[
            tuple[int, int], set[tuple[int, int]]] = {}
        # Spawn square -> its clear pull square, the farm's kite anchor.
        pull_squares: dict[tuple[int, int], tuple[int, int]] = {}

        def walkable(point: tuple[int, int]) -> bool:
            x, y = point
            return bool(
                node is None or
                (((node.walkable(x, y) if node.terrain else
                   0 <= x < node.width and 0 <= y < node.height)) and
                 (x, y) not in node.occupied))

        for spawn in priorities:
            if neutral_spawn(spawn):
                points = (
                    (spawn.x + 1, spawn.y), (spawn.x, spawn.y + 1),
                    (spawn.x - 1, spawn.y), (spawn.x, spawn.y - 1),
                    (spawn.x + 4, spawn.y), (spawn.x, spawn.y + 4),
                    (spawn.x - 4, spawn.y), (spawn.x, spawn.y - 4),
                )
                patrol.extend(point for point in points if walkable(point))
                continue

            if not isolated_radius_pulls:
                points = (
                    (spawn.x + 1, spawn.y), (spawn.x, spawn.y + 1),
                    (spawn.x - 1, spawn.y), (spawn.x, spawn.y - 1),
                    (spawn.x + 4, spawn.y), (spawn.x, spawn.y + 4),
                    (spawn.x - 4, spawn.y), (spawn.x, spawn.y - 4),
                )
                patrol.extend(point for point in points if walkable(point))
                continue

            radius = max(1, spawn.aggro_radius)
            # Search the entire Chebyshev perimeter. Edge spawns often have
            # all four cardinal choices outside the map or behind authored
            # fixtures even though a safe boundary square exists between
            # them; limiting pulls to eight compass points can silently omit
            # an otherwise farmable resident.
            offsets = tuple(
                (dx, dy)
                for dx in range(-radius, radius + 1)
                for dy in range(-radius, radius + 1)
                if max(abs(dx), abs(dy)) == radius)
            candidates = []
            for dx, dy in offsets:
                point = spawn.x + dx, spawn.y + dy
                if not walkable(point):
                    continue
                clearance = min((
                    max(abs(point[0] - other.x),
                        abs(point[1] - other.y)) -
                    other.pull_clearance_radius
                    for other in residents if other is not spawn
                ), default=99)
                if clearance <= 0:
                    continue
                mobility = sum(
                    walkable((point[0] + sx, point[1] + sy))
                    for sx, sy in c.DIRECTION_DELTAS.values())
                candidates.append((clearance, mobility, point))
            if not candidates:
                continue
            _, _, goal = max(candidates)
            patrol.append(goal)
            pull_squares[(spawn.x, spawn.y)] = goal
            blocked = set()
            for resident in aggressive_spawns:
                resident_radius = max(1, resident.aggro_radius)
                for x in range(resident.x - resident_radius,
                               resident.x + resident_radius + 1):
                    for y in range(resident.y - resident_radius,
                                   resident.y + resident_radius + 1):
                        if (max(abs(x - resident.x),
                                abs(y - resident.y)) <= resident_radius):
                            blocked.add((x, y))
            blocked.discard(goal)
            pull_avoidance[goal] = blocked
        aggressive_detection_ranges: dict[str, int] = {}
        if isolated_radius_pulls:
            for spawn in priorities:
                if neutral_spawn(spawn):
                    continue
                for identity in (spawn.named, *spawn.candidates):
                    semantic = self.graph._semantic_name(identity)
                    if semantic:
                        aggressive_detection_ranges[semantic] = max(
                            aggressive_detection_ranges.get(semantic, 0),
                            spawn.aggro_radius)
            # The lone-pull check measures every other resident by its own
            # authored radius where one is known; the rest default to 4.
            for spawn in residents:
                if spawn in priorities or spawn.aggro_radius <= 0:
                    continue
                for identity in (spawn.named, *spawn.candidates):
                    semantic = self.graph._semantic_name(identity)
                    if semantic:
                        aggressive_detection_ranges[semantic] = max(
                            aggressive_detection_ranges.get(semantic, 0),
                            spawn.aggro_radius)
        farm = FarmTask(
            zone=zone, target=target, combat_skill=self.combat_skill,
            combat_spell=self.combat_spell, patrol=patrol,
            combat_skill_until_level=self.combat_skill_until_level,
            priority_spawns=[(spawn.x, spawn.y, spawn.named)
                             for spawn in priorities],
            neutral_targets=neutral_targets,
            aggressive_detection_ranges=aggressive_detection_ranges,
            pull_avoidance=pull_avoidance,
            pull_squares=pull_squares,
            allow_launchers=(
                not self._auto_combat_build and not bool(self.combat_spell)))
        farm._lore_book_attempts = self._lore_book_attempts
        farm._spellbook_attempts = self._spellbook_attempts
        farm._unreachable_targets = self._unreachable_targets
        farm._unprepared_hold = self._unprepared_hold
        if self._analyzer_safety:
            flee, heal = (self.AGGRESSIVE_SAFETY if aggressive else
                          self.PASSIVE_SAFETY)
            # Each lesson learned at this spot tightens its recovery margins.
            level = int((self._adaptations.get(self._spot_key()) or {}).get("level", 0) or 0)
            flee, heal = self_improvement.margins(level, flee, heal)
            farm.safety.flee_below = flee
            farm.safety.heal_below = heal
        if "lost soul" in target.casefold():
            # One soul produces closely spaced physical + secondary damage
            # packets; a 55% trigger can lose the entire margin while a heal
            # merely matches the next attack. Begin control early enough to
            # create separation before the following weapon cycle.
            farm.safety.flee_below = max(farm.safety.flee_below, 0.72)
            farm.safety.heal_below = max(farm.safety.heal_below, 0.88)
        task = NavigateThenTask(
            self.graph, zone, farm, combat_approach=True,
            safety=farm.safety)
        task.navigation.allow_ranged_hazard_fallback = (
            self.clear_hostile_route)
        # A farm trip has no route budget: farm selection weighs its
        # danger (ROUTING 4.5).
        task.navigation.trip = route_costs.FARM
        return task

    @staticmethod
    def _food_reserve(client: AtrinikClient) -> int:
        return food_reserve(client)

    def _hungry(self, client: AtrinikClient) -> bool:
        return hungry(client)

    # Food stat below which a food trip starts even in the middle of a fight
    # or corpse: at the observed ~15 food a minute that leaves about six
    # minutes. Live: on a busy circuit she fought continuously, the food trip
    # waited for a lull that never came, and she starved four times.
    FOOD_URGENT = 90

    def _food_urgent(self, client: AtrinikClient) -> bool:
        """Hungry and nearly starving: no fight may hold the food trip back."""
        return (self._hungry(client) and
                int(client.state.stats.get("food", 1000) or 0) < self.FOOD_URGENT)

    def _food_trip(self, client: AtrinikClient
                   ) -> tuple[NavigateThenTask | None | _Planning, str]:
        """The food trip to start now: a temple priest, the shop, or none;
        PLANNING while its ranking runs off the loop.

        Hungry, she asks a temple priest first: the food is free, so no
        wallet ever turns her away (live, a broke character walked to the
        shop each hungry cycle only to be refused). The shop restocks the
        carried reserve before she is hungry, and feeds her when no temple
        is available. Each honours its own retry deadline.
        """
        now = self.clock()
        temple_ready = bool(self.graph.temple_priests) and now >= self._temple_food_retry_at
        if self._hungry(client) and temple_ready:
            return self._new_temple_food(client), "temple"
        if now >= self._resupply_retry_at:
            # Its merchant ranking runs off the loop too.
            return self._plans.get("food-resupply", self.graph, client,
                                   self._new_food_resupply,
                                   priority=planner.PRIORITY_FOOD), "shop"
        return None, ""

    def _food_order_cost(self) -> int:
        """What a usual restock order costs at the cheapest authored merchant
        food (its value per item, times the usual quantity)."""
        values = [profile.value for vendor in self.graph.dialogue_vendors
                  for profile in self.graph.dialogue_food_stock(vendor)
                  if profile.stackable and profile.value > 0]
        return self.FOOD_RESUPPLY_QUANTITY * min(values, default=0)

    def _food_identification_pending(self, client: AtrinikClient) -> bool:
        """Carried food an identify device will reveal now or within the
        short cooldown after a success (not the failure backoff)."""
        purpose = InventoryCapabilityTask.PURPOSE_IDENTIFY
        ready = (self.clock() >= self._capability_retry_at.get(purpose, 0.0) or
                 purpose in self._capability_succeeded)
        return bool(unidentified_food(client) and ready and
                    InventoryCapabilityTask.candidates(client, purpose))

    def _needs_food_resupply(self, client: AtrinikClient) -> bool:
        # While shop resupply backs off, a temple can still feed her; a low
        # reserve alone waits for the shop, she is not hungry yet. Carried
        # food she can identify on the spot comes first: it may already be
        # the reserve (a failed identification backs off, and then she goes).
        if (self._food_identification_pending(client) and
                not self._food_urgent(client)):
            return False
        now = self.clock()
        if (now >= self._resupply_retry_at and
                self._food_reserve(client) < self.FOOD_RESERVE_LOW and
                InventoryPolicy().weight_ratio(client) < self.FOOD_RESERVE_MAX_LOAD and
                BuyShopUpgradeTask.wallet_value(client) >=
                self.FOOD_RESERVE_CASH + self._food_order_cost()):
            return True
        return self._hungry(client) and (
            now >= self._resupply_retry_at or
            bool(self.graph.temple_priests) and now >= self._temple_food_retry_at)

    def _settle_resupply(self, client: AtrinikClient) -> None:
        """Record a finished food resupply and when the next may start."""
        if self._resupply is None:
            return
        temple = self._resupply_kind == "temple"
        if temple and self._resupply.status in (TaskStatus.FAILED, TaskStatus.COMPLETE):
            # Fed, or that priest failed: the next trip ranks afresh.
            self._food_planner.forget()
        if self._resupply.status == TaskStatus.FAILED:
            log.warning("%s failed: %s",
                        "temple food" if temple else "food resupply",
                        self._resupply.error)
            if temple:
                self._temple_food_retry_at = (
                    self.clock() + self.TEMPLE_FOOD_RETRY_SECONDS)
            else:
                self._resupply_retry_at = self.clock() + 1800.0
            self._resupply = None
        elif self._resupply.status == TaskStatus.COMPLETE:
            bought = food_reserve(client, include_unpaid=True)
            if not temple and (self._hungry(client) and bought <= 1 or
                               bought < self.FOOD_RESERVE_LOW):
                # A merchant that offered no matching stock completes without
                # selling anything; back off (a temple feeds her if hungry).
                log.warning("food resupply bought no food; backing off")
                self._resupply_retry_at = self.clock() + 1800.0
            else:
                log.info("%s complete; returning to farm circuit",
                         "temple food" if temple else "food resupply")
            self._resupply = None

    def _new_temple_food(self, client: AtrinikClient
                         ) -> NavigateThenTask | None | _Planning:
        """The temple food trip, None, or PLANNING while the ranking runs
        off the loop (TempleFoodPlanner)."""
        return self._food_planner.trip(self.graph, client,
                                       exclude=tuple(self.avoid_priests()))

    def _food_resupply_quantity(
            self, client: AtrinikClient, profile: FoodProfile) -> int:
        """Buy a long reserve without consuming the loot headroom."""
        wanted = min(
            self.FOOD_RESUPPLY_MAX_QUANTITY,
            max(self.FOOD_RESUPPLY_QUANTITY, math.ceil(
                self.FOOD_RESUPPLY_NUTRITION / profile.nutrition)))
        limit = float(client.state.stats.get("weight_limit", 0) or 0)
        if limit <= 0 or profile.weight <= 0:
            return wanted
        carried = sum(
            item.weight * item.quantity for item in client.state.inventory)
        # Food is bought only when nearly exhausted. Keep at least twenty per
        # cent of capacity free so a bulk tavern order cannot immediately
        # force the town/storage trip it is intended to postpone.
        by_weight = math.floor(
            max(0.0, limit * 0.80 - carried) / profile.weight)
        return max(1, min(wanted, by_weight))

    def _new_food_resupply(self, client: AtrinikClient) -> NavigateThenTask:
        ranked = []
        for vendor in self.graph.dialogue_vendors:
            profiles = tuple(
                profile for profile in self.graph.dialogue_food_stock(vendor)
                if profile.stackable)
            if not profiles:
                continue
            profile = min(
                profiles,
                key=lambda value: (
                    value.value / value.nutrition,
                    value.weight / value.nutrition,
                    -value.nutrition,
                    value.name.casefold()))
            quote_key = (
                f"{vendor.map_path}:{vendor.x}:{vendor.y}:"
                f"{vendor.name.casefold()}:{profile.name.casefold()}")
            quote = client.state.vendor_quotes.get(quote_key, {})
            observed_cost = quote.get("unit_cost")
            unit_cost = (
                int(observed_cost)
                if isinstance(observed_cost, (int, float)) and
                observed_cost > 0 else profile.value)
            navigation = NavigateTask(
                self.graph, vendor.map_path, (vendor.x, vendor.y), tolerance=2)
            navigation.essential = "food"
            try:
                route = navigation._plan(
                    client, origin=apartment_exit_origin(client))
            except ValueError:
                continue
            if not navigation._threat_fallback:
                ranked.append((
                    navigation.care is not None or navigation.over_budget, len(route),
                    unit_cost / profile.nutrition,
                    profile.weight / profile.nutrition,
                    -profile.nutrition,
                    vendor.name.casefold(), vendor, profile))
        if ranked:
            *_, vendor, profile = min(
                ranked, key=lambda entry: entry[:-2])
            quantity = self._food_resupply_quantity(client, profile)
            task = NavigateThenTask(
                self.graph, vendor.map_path,
                BuyDialogueStockTask(
                    vendor.name,
                    rf"^{re.escape(profile.name)}$",
                    quantity=quantity,
                    preferred=(profile.name,),
                    cash_reserve=1_000,
                    observation_key=(
                        f"{vendor.map_path}:{vendor.x}:{vendor.y}:"
                        f"{vendor.name.casefold()}:"
                        f"{profile.name.casefold()}")),
                destination_xy=(vendor.x, vendor.y), combat_approach=True)
            task.navigation.tolerance = 2
            task.navigation.essential = "food"
            return task
        return mark_essential(NavigateThenTask(
            self.graph, "/shattered_islands/world_0_69",
            BuyGroundItemsTask(
                r"^staple food$", self.FOOD_RESUPPLY_QUANTITY,
                cash_reserve=1_000),
            destination_xy=(15, 7), combat_approach=True), "food")

    @staticmethod
    def _unidentified_items(client: AtrinikClient) -> list[Item]:
        policy = InventoryPolicy()
        return [item for item in client.state.inventory
                if policy.apartment_unidentified(item) and
                not item.flags & c.ITEM_APPLIED]

    def _needs_apartment_storage(self, client: AtrinikClient) -> bool:
        policy = InventoryPolicy()
        # Unknown stock waiting for a cure identify-all stays carried.
        unknown = ([] if self._cure_identify_due(client)
                   else self._unidentified_items(client))
        valuables = [item for item in client.state.inventory
                     if policy.apartment_valuable(item)]
        return bool(unknown or valuables) and (
            bool(valuables) or
            policy.weight_ratio(client) >= self.ROUTINE_TOWN_TRIP_WEIGHT
        ) and self.clock() >= self._storage_retry_at

    def _needs_mass_identification(self, client: AtrinikClient) -> bool:
        unknown = self._unidentified_items(client)
        # An unknown wand/rod may be the emergency recall capability which a
        # hands-off character otherwise cannot discover until an arbitrary
        # carrying threshold. Pay for one immediate reveal while recall is
        # still missing; routine gear remains economically batched.
        urgent_recall_candidate = bool(
            int(client.state.stats.get("level", 0) or 0) >= 18 and
            not self._known_recall_spell(client) and
            not self._owned_recall_device(client) and
            any(item.item_type in (c.TYPE_ROD, c.TYPE_WAND)
                for item in unknown))
        return (
            (urgent_recall_candidate or self._cure_identify_due(client) or (
                len(unknown) >= self.MASS_IDENTIFY_BATCH_MINIMUM and
                InventoryPolicy().weight_ratio(client) >=
                self.MASS_IDENTIFY_WEIGHT)) and
            BuyShopUpgradeTask.wallet_value(client) >=
            self.MASS_IDENTIFY_COST + 1_000 and
            self.clock() >= self._identification_retry_at
        )

    def _needs_inventory_capability(
            self, client: AtrinikClient, purpose: str) -> bool:
        if self.clock() < self._capability_retry_at.get(purpose, 0.0):
            return False
        if purpose == InventoryCapabilityTask.PURPOSE_IDENTIFY:
            # Found food too: identified, it shows whether it is safe to eat.
            # And books, which are sold once identified rather than stored.
            policy = InventoryPolicy()
            needed = bool(
                self._unidentified_items(client) or unidentified_food(client) or
                any(item.item_type == c.TYPE_BOOK and not policy.identified(item)
                    for item in client.state.inventory))
        elif purpose == InventoryCapabilityTask.PURPOSE_DISEASE:
            needed = self._disease_suspected
        elif purpose == InventoryCapabilityTask.PURPOSE_DEPLETION:
            needed = self._has_depletion(client)
        else:
            return False
        return bool(
            needed and InventoryCapabilityTask.candidates(client, purpose))

    def _queue_inventory_capability(
            self, client: AtrinikClient, purpose: str) -> bool:
        if not self._needs_inventory_capability(client, purpose):
            return False
        self._capability = InventoryCapabilityTask(purpose)
        return True

    def _new_mass_identification(self) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_8_79",
            MassIdentifyTask("Kulgar", self.MASS_IDENTIFY_COST),
            destination_xy=(4, 9), combat_approach=True)
        task.navigation.tolerance = 1
        task.navigation.strict_safety = True
        return task

    def _new_apartment_storage(self, *, keep_unknown: bool = False) -> NavigateThenTask:
        # Every visit also takes sellable stock (books, worthless-plus items)
        # back out of the chest for the next sale.
        task = NavigateThenTask(
            self.graph,
            "/shattered_islands/strakewood_island/apartments/apartment_cheap",
            DepositItemsTask(
                "chest", unidentified_only=not keep_unknown, valuable_only=True,
                then_retrieve=RetrieveItemsTask(
                    "chest", predicate=InventoryPolicy().sellable_junk,
                    max_weight_ratio=self.STOCK_RETRIEVAL_WEIGHT)),
            destination_xy=(1, 3), combat_approach=True)
        task.navigation.strict_safety = True
        return task

    def _spot_maps(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for path, _ in self.legs))

    def _savebed_search(self, client: AtrinikClient) -> SavebedSearch:
        """This circuit's nearest-savebed search, restarted when its inputs change."""
        bed_map = getattr(client.state, "savebed_map", "")
        bed_xy = getattr(client.state, "savebed_xy", None)
        current = (bed_map, tuple(bed_xy)) if bed_map and bed_xy else None
        returns = dict(getattr(client.state, "apartment_returns", {}) or {})
        # Her keys open shortcuts such as Morg'eean's ship for the walks too.
        access = unlocks.unlock_state(client, self.graph).access
        key = (self._spot_maps(), frozenset(self._bed_failed), current,
               frozenset(returns.items()), access)
        if self._bed_search is None or self._bed_search[0] != key:
            self._bed_search = (key, SavebedSearch(
                self.graph, key[0], excluded_tiles=self._bed_failed, current=current,
                returns=returns, access=access))
        return self._bed_search[1]

    def _needs_savebed(self, client: AtrinikClient) -> bool:
        """Whether a usable bed nearer this circuit beats the current respawn.

        A death then costs only the short walk back from that bed. Binding is
        a one-time detour per circuit, so it must save a real walk. The search
        advances one exact route per call and answers False until it is done.
        """
        if (int(client.state.stats.get("level", 0) or 0) < 1 or
                self.clock() < self._bed_retry_at):
            return False
        search = self._savebed_search(client)
        if not search.step():
            return False
        target = search.result
        if target is None:
            return False
        bound = (getattr(client.state, "savebed_map", ""),
                 tuple(getattr(client.state, "savebed_xy", None) or ()))
        if bound == (target[0], tuple(target[1])):
            return False
        # Another bed on the same map counts like any other: only a real gain
        # (an inn's far corner versus its exit-side beds) is worth a rebind.
        current = search.current_walk
        if current is None:
            # A bound apartment bed is unpriced until leaving that apartment
            # has shown where its exit leads; keep it until then rather than
            # rebinding without proof of a gain. Any other unpriced bed (no
            # route, an unknown map, none bound) loses to a reachable one.
            bound_map = bound[0]
            returns = getattr(client.state, "apartment_returns", {}) or {}
            return not ("/apartments/" in bound_map and bound_map in self.graph.nodes and
                        bound_map not in returns)
        return target[2] + self.SAVEBED_MIN_GAIN_TILES <= current

    def _new_savebed(self, client: AtrinikClient) -> NavigateThenTask:
        target = self._savebed_search(client).result
        assert target is not None
        self._bed_target = target
        return mark_essential(NavigateThenTask(
            self.graph, target[0], BindSavebedTask(names=tuple(filter(None, (
                getattr(self.graph, "savebed_names", {}).get((target[0], *target[1])),)))),
            destination_xy=target[1], combat_approach=True), "savebed")

    def _observe_disease(self, client: AtrinikClient) -> None:
        for entry in client.state.messages_since(self._disease_message_index):
            text = (entry[3] if len(entry) > 3 else "").casefold()
            if "you are healed from disease" in text:
                self._disease_suspected = False
                self._cure_apply_at = 0.0
            elif any(marker in text for marker in (
                    "your illness seems less severe", "your feet itch",
                    "can't control your sphincter", "cannot control your sphincter",
                    "athelete's foot hit you", "athlete's foot hit you")):
                self._disease_suspected = True
        self._disease_message_index = client.state.message_count

    @staticmethod
    def _disease_cures(client: AtrinikClient) -> list[Item]:
        return [item for item in client.state.inventory
                if "cure illness" in item.name.casefold()]

    def _needs_disease_cure_purchase(self, client: AtrinikClient) -> bool:
        return (self._disease_suspected and
                not self._disease_cures(client) and
                BuyShopUpgradeTask.wallet_value(client) >= 3_000 and
                self.clock() >= self._cure_retry_at)

    def _new_disease_cure(self) -> NavigateThenTask:
        return mark_essential(NavigateThenTask(
            self.graph, "/shattered_islands/world_5_58",
            BuyGroundItemsTask(r"cure illness", 1, cash_reserve=500),
            destination_xy=(9, 17), combat_approach=True), "cure")

    async def _apply_disease_cure(self, client: AtrinikClient) -> bool:
        if not self._disease_suspected:
            return False
        now = self.clock()
        if self._cure_apply_at and now - self._cure_apply_at < 3.0:
            return True
        policy = InventoryPolicy()
        cure = next((item for item in self._disease_cures(client)
                     if not item.flags & c.ITEM_UNPAID and
                     policy.identified(item)), None)
        if cure is None:
            return False
        await client.clear_actions()
        await client.set_combat(False)
        await client.apply(cure.tag)
        self._cure_apply_at = now
        self._cure_retry_at = now + 30.0
        log.info("applying identified cure-illness potion")
        return True

    @staticmethod
    def _has_depletion(client: AtrinikClient) -> bool:
        return any(item.item_type == c.TYPE_FORCE and
                   item.name.casefold() == "depletion"
                   for item in client.state.inventory)

    # Seconds before retrying a failed depletion removal. A service the
    # priest was paid for that still left the depletion force waits much
    # longer: retrying it every half hour only spends the purse.
    RESTORATION_RETRY_SECONDS = 1800.0
    PAID_RESTORATION_RETRY_SECONDS = 6 * 3600.0

    # A strict trip with no safe route from where she stands has not failed:
    # it retries shortly, once farming has moved her somewhere it routes from
    # (or a map that killed her stops counting), never after a long backoff.
    STRICT_ROUTE_RETRY_SECONDS = 120.0
    # A full apartment chest stays full until a later visit has taken
    # sellable stock back out and sold it; do not walk back every half hour.
    STORAGE_FULL_RETRY_SECONDS = 6 * 3600.0

    def _strict_trip_refused(self, client: AtrinikClient, what: str,
                             error: str) -> None:
        """A strict town trip found no strictly safe route: on the
        cure-funding farm, go home for it (send_cure_funding_farm_home)."""
        if (self.failure_backoff(error, 0.0) ==
                self.STRICT_ROUTE_RETRY_SECONDS):
            send_cure_funding_farm_home(client, f"{what}: {error}")

    @classmethod
    def failure_backoff(cls, error: str, default: float) -> float:
        if ("avoiding every known hazard" in error or
                "is itself a known hazard" in error):
            return cls.STRICT_ROUTE_RETRY_SECONDS
        if error.startswith(DepositItemsTask.STORAGE_FULL):
            return cls.STORAGE_FULL_RETRY_SECONDS
        return default

    @classmethod
    def restoration_backoff(cls, error: str) -> float:
        if error.startswith("temple service took payment"):
            return cls.PAID_RESTORATION_RETRY_SECONDS
        return cls.RESTORATION_RETRY_SECONDS

    @staticmethod
    def _depletion_service_cost(client: AtrinikClient) -> int:
        level = int(client.state.stats.get("level", 0) or 0)
        return int(level / 2 * 130 + 525)

    def _needs_depletion_service(self, client: AtrinikClient) -> bool:
        return (self._has_depletion(client) and
                client.state.depletion_points_known and
                client.state.depletion_points >=
                self.DEPLETION_SERVICE_THRESHOLD and
                BuyShopUpgradeTask.wallet_value(client) >=
                self._depletion_service_cost(client) and
                self.clock() >= self._restoration_retry_at and
                time.time() >= float(getattr(
                    client.state, "paid_restoration_retry_until", 0.0) or 0.0))

    def _new_depletion_service(self, client: AtrinikClient) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_3_58",
            TempleServiceTask("Saruthar", "remove depletion", "depletion",
                              self._depletion_service_cost(client)),
            destination_xy=(13, 13), combat_approach=True,
            safety=SafetyPolicy(heal_below=0.85, flee_below=0.70))
        task.navigation.tolerance = 1
        # The temple lies south of the Strakewood swamps, whose crocodile and
        # frog packs make every way there a pack-hazard map. Depletion keeps
        # Sera weak until it is removed, so from the level the regional
        # shopping trips already cross that crocodile habitat take the
        # careful route through them rather than give up; the stricter
        # safety policy heals early. Below it the trip still waits for a
        # hazard-free route, since an early character cannot survive one.
        level = int(client.state.stats.get("level", 0) or 0)
        task.navigation.allow_ranged_hazard_fallback = (
            level >= self.SWAMP_TRANSIT_LEVEL)
        # While depleted she is fragile whatever her level, so the cure trip
        # never crosses a known hazard; with no safe route it waits.
        task.navigation.strict_safety = True
        task.navigation.essential = "depletion"
        return task

    def _queue_depletion_service(self, client: AtrinikClient) -> bool:
        if not self._needs_depletion_service(client):
            return False
        self._restoration = self._new_depletion_service(client)
        return True

    # Seconds before another funding trip once one found nothing to take, or
    # failed; after a productive one the follow-up sale decides.
    CURE_FUNDING_RETRY_SECONDS = 6 * 3600.0
    # Stop taking stock out of the chest at this share of the weight limit:
    # an overloaded character is slow and easy to kill.
    STOCK_RETRIEVAL_WEIGHT = 0.80

    def _cure_shortfall(self, client: AtrinikClient) -> bool:
        """Depleted enough to need the priest, but unable to pay him."""
        return (self._has_depletion(client) and
                client.state.depletion_points_known and
                client.state.depletion_points >=
                self.DEPLETION_SERVICE_THRESHOLD and
                client.state.bank_balance_known and
                BuyShopUpgradeTask.wallet_value(client) <
                self._depletion_service_cost(client))

    def _cure_identify_due(self, client: AtrinikClient) -> bool:
        """Short of the cure, with an identify-all batch carried and affordable.

        The batch minimum is the routine one: identify-all costs 1,450, so a
        stray unknown drop never buys it; only the carrying condition goes.
        """
        return (self._cure_shortfall(client) and
                len(self._unidentified_items(client)) >=
                self.MASS_IDENTIFY_BATCH_MINIMUM and
                BuyShopUpgradeTask.wallet_value(client) >=
                self.MASS_IDENTIFY_COST + 1_000)

    def _needs_cure_funding(self, client: AtrinikClient) -> bool:
        """Short of the cure, with nothing carried to sell or identify."""
        return (self._cure_shortfall(client) and
                not self._identified_junk(client) and
                not self._cure_identify_due(client) and
                self.clock() >= self._cure_funding_retry_at)

    def _cure_funding_wants(self, client: AtrinikClient) -> Callable[[Item], bool]:
        """What to take from the chest: identified sellable junk, and the
        unidentified stock too once identify-all is affordable."""
        policy = InventoryPolicy()
        identify = (BuyShopUpgradeTask.wallet_value(client) >=
                    self.MASS_IDENTIFY_COST + 1_000)
        return lambda item: (
            policy.sellable_junk(item) or
            (identify and policy.apartment_unidentified(item)))

    def _new_cure_funding(self, client: AtrinikClient) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph,
            "/shattered_islands/strakewood_island/apartments/apartment_cheap",
            RetrieveItemsTask(
                "chest", predicate=self._cure_funding_wants(client),
                max_weight_ratio=self.STOCK_RETRIEVAL_WEIGHT),
            destination_xy=(1, 3), combat_approach=True)
        task.navigation.strict_safety = True
        task.navigation.essential = "funding"
        return task

    def _settle_cure_funding(self, client: AtrinikClient) -> None:
        """After the chest trip: what it brought is sold or identified by
        the ordinary checks; an empty or failed trip waits."""
        task = self._cure_funding
        self._cure_funding = None
        if task is None or task.status == TaskStatus.FAILED or not task.task.retrieved:
            failed = task is not None and task.status == TaskStatus.FAILED
            if failed:
                log.warning("cure funding trip failed: %s", task.error)
            self._cure_funding_retry_at = self.clock() + (
                self.failure_backoff(task.error, self.CURE_FUNDING_RETRY_SECONDS)
                if failed else self.CURE_FUNDING_RETRY_SECONDS)
            return
        log.info("cure funding: took %d item(s) from the apartment",
                 len(task.task.retrieved))

    @staticmethod
    def _junk_policy(tags=()) -> JunkPolicy:
        return JunkPolicy((
            r"^paper ",
            r"^(?:iron|bronze|steel|wooden|spruce|soft leather) .*(?:sword|axe|mace|club|spear|bow|shield|armour|skirt|sandals|boots|gloves|helmet)$",
        ), tags=frozenset(tags), sell_identified_junk=True)

    @staticmethod
    def _surplus_equipment(client: AtrinikClient) -> list[Item]:
        """Return identified gear proven inferior to every occupied slot."""
        policy = InventoryPolicy()
        level = int(client.state.stats.get("level", 0) or 0)
        equipped = {
            tag: client.state.items[tag]
            for tag in client.state.equipment.values()
            if tag in client.state.items
        }

        def score(item: Item) -> int:
            prototype = BuyShopUpgradeTask._prototype(client, item)
            return (policy.gear_score(item) +
                    (prototype.base_score if prototype is not None else 0))

        surplus = []
        for item in client.state.inventory:
            if (item.item_type not in policy.equipment_types or
                    not policy.identified(item) or
                    item.flags & (c.ITEM_APPLIED | c.ITEM_UNPAID |
                                  c.ITEM_CURSED | c.ITEM_DAMNED |
                                  c.ITEM_MAGICAL) or
                    item.required_level > level or item.quality >= 90 or
                    any(word in item.name.casefold()
                        for word in policy.reserve_words)):
                continue
            slots = policy.equipment_slots.get(item.item_type, ())
            current = [equipped[tag]
                       for slot in slots
                       if (tag := client.state.equipment.get(slot)) in equipped]
            if not current:
                continue
            different_weapon_school = bool(
                item.item_type in (c.TYPE_WEAPON, c.TYPE_BOW) and
                item.required_skill_tag and
                any(equipped_item.required_skill_tag and
                    equipped_item.required_skill_tag != item.required_skill_tag
                    for equipped_item in current))
            if (different_weapon_school or
                    score(item) <= min(score(value) for value in current) + 15):
                surplus.append(item)
        return surplus

    def _identified_junk(self, client: AtrinikClient) -> list[Item]:
        surplus = self._surplus_equipment(client)
        policy = self._junk_policy(item.tag for item in surplus)
        return [item for item in client.state.inventory if policy.junk(item)]

    def _needs_junk_sale(self, client: AtrinikClient) -> bool:
        junk = self._identified_junk(client)
        return bool(junk) and (
            self._cure_shortfall(client) or
            InventoryPolicy().weight_ratio(client) >=
            self.ROUTINE_TOWN_TRIP_WEIGHT
        ) and self.clock() >= self._selling_retry_at

    def _new_junk_sale(self, client: AtrinikClient) -> NavigateThenTask:
        surplus = self._surplus_equipment(client)
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_0_69",
            SellJunkTask(
                "Brynknot shop floor",
                self._junk_policy(item.tag for item in surplus)),
            destination_xy=(15, 7), combat_approach=True)
        task.navigation.strict_safety = True
        return task

    def _needs_bank_sync(self, client: AtrinikClient) -> bool:
        # Persistent balance knowledge is needed only before a budgeted
        # service. Avoid gratuitous bank travel for ordinary farming tasks.
        needs_budget = (
            (self._has_depletion(client) and
             client.state.depletion_points_known and
             client.state.depletion_points >=
             self.DEPLETION_SERVICE_THRESHOLD) or
            (self._disease_suspected and not self._disease_cures(client)) or
            int(client.state.stats.get("level", 0) or 0) >= 8
        )
        return (needs_budget and (not client.state.bank_balance_known or
                                  self._stale_balance_short(client)) and
                self.clock() >= self._bank_sync_retry_at)

    # A cached balance older than this is checked again before she is held
    # back from a cure it cannot pay: the bank may hold money she has not
    # seen. Live, an owner's deposit went unseen for hours behind a cached
    # balance of 35 copper, and she kept dying depleted instead of curing.
    BANK_RESYNC_SECONDS = 1800.0

    def _stale_balance_short(self, client: AtrinikClient) -> bool:
        """Depleted, a cached balance too small for the cure, and old."""
        observed = float(getattr(client.state, "bank_balance_observed_at", 0.0) or 0.0)
        # The temple's own retry gates first: a balance it cannot use yet
        # is not worth a bank trip.
        return (client.state.bank_balance_known and
                self._has_depletion(client) and
                client.state.depletion_points_known and
                client.state.depletion_points >= self.DEPLETION_SERVICE_THRESHOLD and
                BuyShopUpgradeTask.wallet_value(client) <
                self._depletion_service_cost(client) and
                time.time() - observed > self.BANK_RESYNC_SECONDS and
                self.clock() >= self._restoration_retry_at and
                time.time() >= float(getattr(
                    client.state, "paid_restoration_retry_until", 0.0) or 0.0))

    def _bank_sync_backoff(self, task: NavigateThenTask) -> float:
        """Seconds before another bank sync after ``task`` failed.

        A refresh of a known balance is optional (the known shortfall already
        drives cure funding), so with no safe route it waits a whole re-sync
        interval rather than interrupting farming every two minutes.
        """
        backoff = self.failure_backoff(task.error, 1800.0)
        if getattr(task.task, "refresh", False):
            backoff = max(backoff, self.BANK_RESYNC_SECONDS)
        return backoff

    def _new_bank_sync(self, *, refresh: bool = False) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_0_67",
            BankBalanceTask("Tolmir", refresh=refresh), destination_xy=(19, 15),
            combat_approach=True)
        task.navigation.strict_safety = True
        return task

    def _needs_bank_deposit(self, client: AtrinikClient) -> bool:
        # Cross-world banking costs enough farm uptime that carrying one stray
        # copper coin is safer for total wealth than repeatedly abandoning a
        # high-XP spawn pocket. Batch ordinary corpse money, while a completed
        # junk sale explicitly forces its larger proceeds into the bank.
        carried = BuyShopUpgradeTask.carried_wallet_value(client)
        carrying_pressure = (
            InventoryPolicy().weight_ratio(client) >=
            self.ROUTINE_TOWN_TRIP_WEIGHT)
        return (carried > 0 and
                (self._force_bank_deposit or
                 (carried >= self.BANK_DEPOSIT_MINIMUM and
                  (carrying_pressure or
                   carried >= self.BANK_DEPOSIT_WEALTH_OVERRIDE))) and
                time.time() - client.state.last_bank_deposit_at >= 3600.0 and
                self.clock() >= self._banking_retry_at)

    def _needs_progress_checkpoint(self, client: AtrinikClient,
                                   farm: FarmTask) -> bool:
        """Checkpoint earned XP only at a completely fight-safe boundary."""
        return bool(
            callable(getattr(client, "checkpoint_reconnect", None)) and
            self._current_exp > self._checkpoint_exp and
            self.clock() - self._checkpoint_at >=
            self.PROGRESS_CHECKPOINT_INTERVAL and
            not self._busy_finishing_fight_or_loot(client, farm)
        )

    def _maintenance_services(self) -> tuple[BotTask | None, ...]:
        """Every maintenance trip but food resupply; each runs to completion."""
        return (
            self._storage, self._identification, self._cure_funding,
            self._bed_binding, self._selling, self._banking,
            self._bank_sync, self._shopping, self._recall_shopping,
            self._utility_shopping, self._heal_restock,
            self._ship_key,
            self._spell_purchase, self._cure, self._restoration,
            self._expedition_return, self._capability,
        )

    def can_feed_her(self) -> bool:
        """Whether its own food resupply can run, with no other trip under way.

        Each maintenance trip returns early until it completes, so a long one
        (storage, banking, a temple cure) can outlast her food.
        """
        return not any(service is not None
                       for service in self._maintenance_services())

    def needs_outside_food(self) -> bool:
        """Whether only a detour outside the circuit can feed her now.

        Not while a hostile-route abort retraces to its safe origin: turning
        that towards a priest could lead the attackers along. A running
        inventory capability (seconds long, such as identifying the food she
        carries) is no such trip: it finishes first.
        """
        blocking = [service for service in self._maintenance_services()
                    if service is not None and service is not self._capability]
        return bool(blocking) and self._expedition_return is None

    # Trips autoplay drops at once when farming turns unsafe: discretionary
    # purchases, recall shopping and the heal restock (never bought with
    # the cure's money) and savebed rebinding. Every other trip is an
    # essential (food, bank, identify, storage, the cure and its funding,
    # the ship key) and finishes first.
    DROPPED_AT_HANDOVER = ("_shopping", "_spell_purchase", "_utility_shopping",
                           "_recall_shopping", "_heal_restock", "_bed_binding")

    def drop_discretionary_trips(self, client: AtrinikClient) -> list[str]:
        """End the trips in DROPPED_AT_HANDOVER; the names of those ended.

        A shop trip settling a purchase (its ``settlement`` set) is kept,
        and so, while she carries unpaid stock, is the one trip paying for
        it (_paying_trip): each finishes paying before the switch.
        """
        paying = self._paying_trip(client)
        dropped = []
        for name in self.DROPPED_AT_HANDOVER:
            task = getattr(self, name)
            if (task is not None and name != paying and
                    getattr(task, "settlement", None) is None):
                dropped.append(task.name)
                setattr(self, name, None)
        return dropped

    @staticmethod
    def carries_unpaid(client: AtrinikClient) -> bool:
        return any(item.flags & c.ITEM_UNPAID for item in client.state.inventory)

    def _paying_trip(self, client: AtrinikClient) -> str:
        """The trip in DROPPED_AT_HANDOVER that pays for her unpaid stock,
        or "": the one settling a purchase, else a shop-floor trip under way
        (an upgrade sweep, then utility shopping). Food bought on a shop
        floor is the resupply's, an essential that is never dropped."""
        if not self.carries_unpaid(client) or self._resupply is not None:
            return ""
        running = [name for name in self.DROPPED_AT_HANDOVER
                   if getattr(self, name) is not None]
        return next((name for name in running if getattr(
            getattr(self, name), "settlement", None) is not None), next((
                name for name in ("_shopping", "_utility_shopping")
                if name in running), ""))

    def unprepared_resting(self) -> bool:
        """Whether its farm rests for want of a heal or a recall
        (FarmTask.unprepared_resting): an explained wait, not being stuck.
        Never while a trip of its own runs."""
        if self._resupply is not None or any(
                service is not None for service in self._maintenance_services()):
            return False
        farm = self.child.task if self.child is not None else None
        return isinstance(farm, FarmTask) and (
            farm.unprepared_resting() or
            # A heal restock waiting for full HP and SP to take the
            # hostile road (_heal_restock_waiting) is a wait too.
            (farm.rest_for_departure and
             self._heal_restock_full_since is not None))

    def hold_new_pulls(self, hold: bool = True) -> None:
        """While the switch to recovery waits: the farm pulls no new target
        (FarmTask.hold_new_pulls); a rebuilt farm child is held again.
        ``hold`` False lifts it once farming is safe again."""
        farm = self.child.task if self.child is not None else None
        if isinstance(farm, FarmTask):
            farm.hold_new_pulls = hold

    def ready_for_handover(self, client: AtrinikClient) -> bool:
        """Whether autoplay may replace this circuit by its recovery phase.

        Not while an essential trip is under way, nor while she fights on
        the farm map: the farm's own combat and emergency recall finish
        that fight. On the road a fight is no reason to wait: that would
        walk her on along the farm route, and recovery defends her itself.
        """
        if self._resupply is not None or any(
                service is not None for service in self._maintenance_services()):
            return False
        farm = self.child.task if self.child is not None else None
        on_farm = (self.child is not None and
                   client.state.map.path == self.child.navigation.destination)
        return not (on_farm and isinstance(farm, FarmTask) and (
            _contact_this_life(client) or
            self._busy_finishing_fight_or_loot(client, farm, held=True)))

    def safe_for_progression_detour(self, client: AtrinikClient) -> bool:
        """Return whether autoplay may replace this circuit between pulls."""
        if (self._resupply is not None or not self.can_feed_her() or
                self.child is None):
            return False
        farm = self.child.task
        return (isinstance(farm, FarmTask) and
                not self._busy_finishing_fight_or_loot(client, farm))

    async def _maintain_safe_inventory(self, client: AtrinikClient,
                                       farm: FarmTask) -> bool:
        """Reconcile loot between pulls even on continuously respawning maps."""
        if self._busy_finishing_fight_or_loot(client, farm):
            return False
        return await farm.maintain_inventory(client)

    async def _progress_checkpoint(self, client: AtrinikClient) -> bool:
        await client.clear_actions()
        await client.set_combat(False)
        requested = await client.checkpoint_reconnect()
        if not requested:
            return False
        self._checkpoint_at = self.clock()
        self._checkpoint_exp = self._current_exp
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("progress-checkpoint",
                     f"experience={self._checkpoint_exp}")
        return True

    def _new_bank_deposit(self, client: AtrinikClient) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_0_67",
            BankTask("Tolmir", "all"), destination_xy=(19, 15),
            combat_approach=True)
        task.navigation.strict_safety = True
        return task

    def _needs_upgrade_shopping(self, client: AtrinikClient) -> bool:
        level = int(client.state.stats.get("level", 0) or 0)
        wallet = BuyShopUpgradeTask.wallet_value(client)
        last_at = client.state.last_upgrade_shop_sweep_at
        last_level = getattr(
            client.state, "last_upgrade_shop_sweep_level", 0)
        last_wallet = getattr(
            client.state, "last_upgrade_shop_sweep_wallet", 0)
        last_policy = getattr(
            client.state, "last_upgrade_shop_sweep_policy", 0)
        current_tier = 18 if level >= 18 else 8
        # Runtime records written before tier/wallet tracking represent the
        # early Brynknot sweep. Avoid an immediate duplicate while still
        # opening the regional tier once level 18 makes those roads safe.
        last_tier = (18 if last_level >= 18 else 8) if last_level else (
            8 if last_at else 0)
        tier_due = current_tier > last_tier
        wealth_due = bool(
            last_wallet and wallet >= max(
                last_wallet * 2,
                last_wallet + self.UPGRADE_SWEEP_WALLET_GROWTH))
        interval_due = (
            not last_at or
            time.time() - last_at >= self.UPGRADE_SWEEP_INTERVAL)
        policy_due = level >= 18 and last_policy < self.UPGRADE_SWEEP_POLICY
        return (not self._disease_suspected and
                level >= 8 and
                # The shopper independently preserves 1,000 copper and caps
                # each purchase at 25% of the wallet. Start inspecting local
                # stock once at least 250 copper is genuinely discretionary;
                # waiting for 2,000 left early characters in starter gear even
                # when inexpensive improvements were already affordable.
                wallet >= 1_250 and
                (tier_due or wealth_due or interval_due or policy_due) and
                self.clock() >= self._shopping_retry_at)

    @staticmethod
    def _known_recall_spell(client: AtrinikClient) -> bool:
        return any(
            item.item_type == c.TYPE_SPELL and
            item.name.casefold() == "word of recall"
            for item in client.state.inventory)

    @staticmethod
    def _owned_recall_device(client: AtrinikClient) -> bool:
        # A wand known to be empty is no escape: recall shopping replaces it.
        policy = InventoryPolicy()
        return any(
            item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and
            "word of recall" in item.name.casefold() and
            policy.identified(item) and
            not wand_known_empty(client, item) and
            not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)
            for item in client.state.inventory)

    @staticmethod
    def _owned_recall_spellbook(client: AtrinikClient) -> bool:
        return any(
            item.item_type == c.TYPE_SPELLBOOK and
            "word of recall" in item.name.casefold()
            for item in client.state.inventory)

    def _recall_shopping_funds(self, client: AtrinikClient) -> int:
        """What recall shopping needs in her wallet before it starts: the
        recall she was quoted within RECALL_SHOP_INTERVAL plus the reserve,
        else RECALL_SHOP_MINIMUM_WALLET. An older quote no longer holds her
        back: merchant stock changes, and a trip finds out the new price."""
        quote = recall_quote(client)
        if quote is None:
            return self.RECALL_SHOP_MINIMUM_WALLET
        return max(self.RECALL_SHOP_MINIMUM_WALLET,
                   quote[1] + self.RECALL_SHOP_RESERVE)

    def _needs_recall_shopping(self, client: AtrinikClient) -> bool:
        """Whether to buy a recall source now: an opportunity, never a
        precondition for farming (owner decision, 2026-11-22). The circuit
        runs it between fights like its other maintenance trips, only
        while she can afford what she was last quoted."""
        if (int(client.state.stats.get("level", 0) or 0) <
                RECALL_REQUIRED_LEVEL or
                not self._has_ship_key(client) or
                BuyShopUpgradeTask.wallet_value(client) <
                self._recall_shopping_funds(client) or
                self.clock() < self._recall_shopping_retry_at):
            return False
        wizardry = self._skill_level(client, "wizardry spells")
        has_immediate = (
            self._known_recall_spell(client) or
            self._owned_recall_device(client))
        has_permanent = (
            self._known_recall_spell(client) or
            self._owned_recall_spellbook(client))
        if has_immediate and (wizardry < 12 or has_permanent):
            return False
        last_check = float(getattr(
            client.state, "last_recall_shop_check_at", 0.0) or 0.0)
        return (not last_check or
                time.time() - last_check >= self.RECALL_SHOP_INTERVAL)

    @staticmethod
    def note_recall_unavailable(client: AtrinikClient, detail: str) -> None:
        """Persist why buying a recall source came to nothing ("" once it
        worked), for recall_alerts (GameState.recall_unavailable)."""
        setter = getattr(client, "set_recall_unavailable", None)
        if setter is not None:
            setter(detail)
        else:
            client.state.recall_unavailable = detail
            client.state.recall_unavailable_at = time.time() if detail else 0.0

    def _new_recall_shopping(self, client: AtrinikClient) -> NavigateThenTask:
        vendors = {
            (vendor.map_path, vendor.x, vendor.y, vendor.name): vendor
            for vendor in (
                self.graph.find_dialogue_vendors(r"word of recall") +
                self.graph.find_dialogue_vendors(
                    treasure_lists=(
                        "random_wand", "random_mtools", "random_scroll")))
        }
        ranked = []
        for vendor in vendors.values():
            navigation = NavigateTask(
                self.graph, vendor.map_path, (vendor.x, vendor.y), tolerance=2)
            try:
                route = navigation._plan(client)
            except ValueError:
                continue
            if navigation._threat_fallback:
                continue
            immediate_roll = any(
                value.casefold() in ("random_wand", "random_mtools")
                for value in vendor.treasure_lists)
            # Over its budget comes after every safe vendor.
            ranked.append((navigation.over_budget, len(route), not immediate_roll,
                           vendor.level, vendor.name.casefold(), vendor))
        vendor = (min(ranked, key=lambda entry: entry[:-1])[-1]
                  if ranked else DialogueVendor(
            "/shattered_islands/world_4_47", 11, 13, "sage",
            "/python/generic/merchant.py"))
        task = NavigateThenTask(
            self.graph, vendor.map_path,
            BuyDialogueStockTask(
                vendor.name, r"word of recall",
                cash_reserve=self.RECALL_SHOP_RESERVE,
                observation_key=RECALL_QUOTE_KEY, try_alternatives=True),
            destination_xy=(vendor.x, vendor.y), combat_approach=True)
        task.navigation.tolerance = 2
        task.navigation.allow_ranged_hazard_fallback = True
        task.safety = SafetyPolicy(heal_below=0.85, flee_below=0.70)
        return task

    def _missing_utility_capabilities(
            self, client: AtrinikClient) -> tuple[str, ...]:
        """Return strategic maintenance effects lacking a safe owned source."""
        policy = InventoryPolicy()
        wizardry = self._skill_level(client, "wizardry spells")
        definitions = (
            ("identify", ("identify",), 1),
            ("remove depletion", ("remove depletion",), 1),
            ("cure disease", ("cure disease", "cure illness"), 15),
        )
        missing = []
        for capability, names, spell_level in definitions:
            covered = False
            for item in client.state.inventory:
                folded = item.name.casefold()
                if not any(name in folded for name in names):
                    continue
                if item.flags & (
                        c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED):
                    continue
                if item.item_type == c.TYPE_SPELL:
                    covered = True
                elif (item.item_type == c.TYPE_SPELLBOOK and
                      wizardry >= spell_level and policy.identified(item)):
                    covered = True
                elif (item.item_type in (c.TYPE_ROD, c.TYPE_WAND,
                                         c.TYPE_SCROLL, c.TYPE_POTION) and
                      policy.identified(item)):
                    covered = True
                if covered:
                    break
            if not covered:
                missing.append(capability)
        return tuple(missing)

    def _needs_utility_shopping(self, client: AtrinikClient) -> bool:
        if (int(client.state.stats.get("level", 0) or 0) < 18 or
                not self._inventory_ready(client) or
                not self._has_ship_key(client) or
                BuyShopUpgradeTask.wallet_value(client) < 1_250 or
                self.clock() < self._utility_shopping_retry_at or
                not self._missing_utility_capabilities(client)):
            return False
        last_check = float(getattr(
            client.state, "last_utility_shop_check_at", 0.0) or 0.0)
        return (not last_check or
                time.time() - last_check >= self.UTILITY_SHOP_INTERVAL)

    def _new_utility_shopping(
            self, client: AtrinikClient) -> NavigateThenTask:
        missing = self._missing_utility_capabilities(client)
        wizardry = self._skill_level(client, "wizardry spells")
        patterns = []
        for capability in missing:
            if capability == "cure disease" and wizardry < 15:
                patterns.append(
                    r"(?:(?:rod|wand|scroll) of cure disease|cure illness)")
            else:
                patterns.append(re.escape(capability))
        combined = "(?:" + "|".join(patterns) + ")"
        preferred = []
        # Permanent learnable books first, then renewable and finite devices,
        # and finally one-shot scrolls/potions. Every missing effect is a
        # distinct purchase group so duplicate forms cannot crowd out another
        # capability during the same daily stock observation.
        for kind in ("spellbook", "rod", "wand", "scroll", "potion"):
            for capability in missing:
                if (kind == "spellbook" and capability == "cure disease" and
                        wizardry < 15):
                    continue
                preferred.append(f"{kind} of {capability}")
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_4_47",
            BuyDialogueStockTask(
                "sage", combined, preferred=tuple(preferred),
                cash_reserve=1_000, max_purchases=len(missing),
                distinct_patterns=tuple(patterns),
                observation_key="asteria:sage:strategic-utility"),
            destination_xy=(11, 13), combat_approach=True,
            safety=SafetyPolicy(heal_below=0.85, flee_below=0.70))
        task.navigation.tolerance = 2
        task.navigation.allow_ranged_hazard_fallback = True
        return task

    def _heal_restock_funds(self, client: AtrinikClient) -> int:
        """What the heal restock needs in her wallet before it starts: the
        healing price she was last quoted (heal_restock_quote) plus the
        reserve, at least HEAL_RESTOCK_MINIMUM_WALLET."""
        quote = heal_restock_quote(client)
        if quote is None:
            return self.HEAL_RESTOCK_MINIMUM_WALLET
        return max(self.HEAL_RESTOCK_MINIMUM_WALLET,
                   quote[1] + self.HEAL_RESTOCK_RESERVE)

    def _needs_heal_restock(self, client: AtrinikClient, *,
                            ignore_retry: bool = False) -> bool:
        """Whether to go and buy healing consumables now: from
        RECALL_REQUIRED_LEVEL (where being out of heals and recall makes
        her rest rather than pull), with fewer than HEAL_RESTOCK_LOW she
        can use, room to carry them, the money for one plus the reserve,
        no depletion due (the recovery phase comes first) unless she farms
        to fund the cure (cure_funding_farm: a depleted farm without heals
        is the death spiral the backoff only bounds) and no backoff
        in force (GameState.heal_restock_retry_until). ``ignore_retry``
        leaves out the in-memory re-plan pause (_heal_restock_retry_at)."""
        state = client.state
        if (int(state.stats.get("level", 0) or 0) < RECALL_REQUIRED_LEVEL or
                not self._inventory_ready(client) or
                (depleted_for_temple(state) and
                 not cure_funding_farm(client)) or
                heal_consumables(client) >= self.HEAL_RESTOCK_LOW or
                time.time() < float(getattr(
                    state, "heal_restock_retry_until", 0.0) or 0.0) or
                (not ignore_retry and
                 self.clock() < self._heal_restock_retry_at)):
            return False
        if InventoryPolicy().weight_ratio(client) >= self.FOOD_RESERVE_MAX_LOAD:
            return False
        return (BuyShopUpgradeTask.wallet_value(client) >=
                self._heal_restock_funds(client))

    def _heal_restock_may_leave(self, client: AtrinikClient,
                                unprepared: bool) -> bool:
        """Whether the heal restock may set out now. Unlike every other
        optional trip, the unprepared hold (UnpreparedHold) does not hold
        it: it is what ends being unprepared. But while the hold holds she
        leaves only from HEAL_RESTOCK_SAFETY's flee threshold up; below it
        the farm's unprepared rest regenerates her first."""
        if not unprepared:
            return True
        stats = client.state.stats
        maxhp = max(1, int(stats.get("maxhp", 1) or 1))
        return (int(stats.get("hp", 0) or 0) / maxhp >=
                self.HEAL_RESTOCK_SAFETY[1])

    def back_off_heal_restock(self, client: AtrinikClient, note: str,
                              seconds: float) -> None:
        """Persist why the heal restock came to nothing and wait
        ``seconds`` before the next (heal_restock_alerts shows the note);
        a note of "" is a pause without an alert, and 0 seconds clears."""
        until = time.time() + seconds if seconds > 0 else 0.0
        setter = getattr(client, "set_heal_restock_backoff", None)
        if setter is not None:
            setter(note, until)
        else:
            client.state.heal_restock_note = note
            client.state.heal_restock_retry_until = until
        recorder = getattr(client, "record_action", None)
        if note and recorder is not None:
            recorder("heal-restock-backoff",
                     f"{note}; next try in {int(seconds // 60)} min")

    def _new_heal_restock(self, client: AtrinikClient, *,
                          strict: bool) -> NavigateThenTask | None:
        """A trip to the nearest merchant that may sell her a usable heal
        (heal_restock_vendors), or None when there is none to go to.

        Planned as recall shopping is: the nearest by a route without a
        threat fallback, authored healing stock before random potions, a
        route over its budget after every other. When no such route
        exists, the same ranking over routes that need the hostile-transit
        fallback (Asteria is reached by the crocodile road), as recall
        shopping's own trip may take; the trip is then marked
        ``hostile_transit``. ``strict`` (while unprepared): only a strict
        route, never that fallback, else None.
        """
        vendors = heal_restock_vendors(self.graph, client)
        hostile = False

        def ranking(fallback: bool) -> list:
            ranked = []
            for vendor, fixed in vendors:
                navigation = NavigateTask(
                    self.graph, vendor.map_path, (vendor.x, vendor.y),
                    tolerance=2)
                navigation.strict_safety = strict
                navigation.allow_ranged_hazard_fallback = fallback
                # A probe: no navigation-threat-fallback decision.
                navigation._threat_fallback = fallback
                try:
                    route = navigation._plan(client)
                except ValueError:
                    continue
                if navigation._threat_fallback and not fallback:
                    continue
                ranked.append((navigation.over_budget, not fixed, len(route),
                               vendor.name.casefold(), vendor.map_path, vendor))
            return ranked

        ranked = ranking(False)
        if not ranked and not strict:
            ranked, hostile = ranking(True), True
        if not ranked:
            return None
        vendor = min(ranked, key=lambda entry: entry[:-1])[-1]
        heal_below, flee_below = self.HEAL_RESTOCK_SAFETY
        safety = SafetyPolicy(heal_below=heal_below, flee_below=flee_below)
        task = NavigateThenTask(
            self.graph, vendor.map_path,
            BuyHealingStockTask(
                vendor.name,
                # On the cure-funding farm only up to HEAL_RESTOCK_LOW: just
                # prepared, at the least cost to the cure's savings.
                quantity=(self.HEAL_RESTOCK_LOW if cure_funding_farm(client)
                          else self.HEAL_RESTOCK_TARGET) -
                heal_consumables(client),
                cash_reserve=self.HEAL_RESTOCK_RESERVE,
                observation_key=HEAL_QUOTE_KEY),
            destination_xy=(vendor.x, vendor.y), combat_approach=True,
            safety=safety)
        task.navigation.tolerance = 2
        task.navigation.strict_safety = strict
        task.navigation.allow_ranged_hazard_fallback = not strict
        task.hostile_transit = hostile
        return task

    @staticmethod
    def _full_hp_sp(client: AtrinikClient) -> bool:
        stats = client.state.stats
        return (int(stats.get("hp", 0) or 0) >= int(stats.get("maxhp", 0) or 0)
                and int(stats.get("sp", 0) or 0) >=
                int(stats.get("maxsp", 0) or 0))

    def _heal_restock_waiting(self, client: AtrinikClient,
                              farm: FarmTask) -> bool:
        """Whether a hostile-transit restock still waits for full HP and SP
        (HEAL_RESTOCK_FULL_WAIT_SECONDS at most). Once she is full, or has
        a heal or a ready recall again, the trip is re-planned at once."""
        since = self._heal_restock_full_since
        if since is None:
            return False
        if not self._needs_heal_restock(client, ignore_retry=True):
            # No longer due (heals found, money spent, a backoff, too
            # heavy): no wait, and no backoff of its own.
            self._heal_restock_full_since = None
            return False
        if (self._full_hp_sp(client) or
                not out_of_heals_and_recall(client, farm.safety)):
            self._heal_restock_full_since = None
            self._heal_restock_retry_at = 0.0
            return False
        if self.clock() - since >= self.HEAL_RESTOCK_FULL_WAIT_SECONDS:
            self._heal_restock_full_since = None
            self.back_off_heal_restock(
                client, "no full HP and SP within 10 minutes for the "
                "hostile road, without a heal or a ready recall",
                self.HEAL_RESTOCK_RETRY_SECONDS)
            return False
        return True

    def _settle_heal_restock(self, client: AtrinikClient) -> None:
        """Record a finished heal restock and when the next may start."""
        trip = self._heal_restock
        if trip is None:
            return
        shopper = trip.task
        if trip.status == TaskStatus.FAILED:
            log.warning("heal restock failed: %s", trip.error)
            self.back_off_heal_restock(
                client, f"heal restock failed: {trip.error}",
                self.HEAL_RESTOCK_RETRY_SECONDS)
        elif not (isinstance(shopper, BuyHealingStockTask) and
                  shopper.purchased):
            self.back_off_heal_restock(
                client, f"{getattr(shopper, 'merchant', 'the merchant')} on "
                f"{trip.navigation.destination} had no healing stock she "
                "can use", self.HEAL_RESTOCK_INTERVAL)
        elif heal_consumables(client) < self.HEAL_RESTOCK_LOW:
            self.back_off_heal_restock(
                client, f"{shopper.merchant}'s healing stock ran short",
                self.HEAL_RESTOCK_INTERVAL)
        else:
            log.info("heal restock bought %d %s; returning to farm",
                     shopper.bought, shopper.bought_name)
            # No alert, only the pause before the next.
            self.back_off_heal_restock(client, "",
                                       self.HEAL_RESTOCK_MIN_INTERVAL)
        self._heal_restock = None

    def _new_upgrade_shopping(
            self, client: AtrinikClient) -> ShopUpgradeSweepTask:
        level = int(client.state.stats.get("level", 0) or 0)
        maps = [
            "/shattered_islands/world_0_69",  # Brynknot general store
        ]
        # Reaching the farther shops can cross level-23 crocodile habitat. A
        # level-8 shopper cannot reliably survive a pursuing crocodile's
        # double attacks. At level 18 the fight-safe route clearer and ship
        # shortcut make the trip reasonable, and delaying Asteria's level-9
        # stock leaves a large damage and armour gap.
        if level >= 18:
            maps.extend((
                "/shattered_islands/world_7_46",  # Asteria weapons
                "/shattered_islands/world_6_46",  # Asteria level-9 armour
                "/shattered_islands/world_5_46",  # Asteria launchers
                "/shattered_islands/world_5_58",  # Aris shops
            ))
        # Greyton's general store stocks random rings (difficulty 50), which
        # can roll Pow, Int or mana regeneration. The road there from Aris
        # passes level-28/29 orc spawns, so it waits for level 30.
        if level >= 30:
            maps.append("/shattered_islands/world_9_67")  # Greyton store
        cursor = 0
        if (getattr(client.state, "active_upgrade_shop_sweep_policy", 0) ==
                self.UPGRADE_SWEEP_POLICY):
            cursor = getattr(
                client.state, "active_upgrade_shop_sweep_cursor", 0)
        return ShopUpgradeSweepTask(
            self.graph, maps,
            # The adaptive build is permanently Slash + Wizardry even while
            # its bounded spell-training phase is between milestones. A
            # transient empty combat_spell must not turn Bow Archery 1 stock
            # into a desirable upgrade and terminate a regional sweep early.
            allow_launchers=(
                not self._auto_combat_build and not bool(self.combat_spell)),
            allow_hostile_transit=level >= 18,
            start_index=cursor,
            caster_stats=self._trains_wizardry(client))

    def _trains_wizardry(self, client: AtrinikClient) -> bool:
        """Whether her current build trains Wizardry, as FarmTask decides
        it for maintain_inventory."""
        if "wizardry" not in self.combat_skill:
            return False
        level = self._skill_level(client, self.combat_skill)
        return level > 0 and (not self.combat_skill_until_level or
                              level < self.combat_skill_until_level)

    @staticmethod
    def _has_ship_key(client: AtrinikClient) -> bool:
        return any(MorgeeanShipKeyTask.KEY.search(item.name)
                   for item in client.state.inventory)

    @staticmethod
    def _inventory_ready(client: AtrinikClient) -> bool:
        """Whether the authoritative post-login inventory packet arrived."""
        return bool(client.state.inventory_replay_complete)

    def _needs_ship_key(self, client: AtrinikClient) -> bool:
        return (int(client.state.stats.get("level", 0) or 0) >= 8 and
                self._inventory_ready(client) and
                not self._has_ship_key(client) and
                self.clock() >= self._ship_key_retry_at)

    @staticmethod
    def _knows_spell(client: AtrinikClient, name: str) -> bool:
        return any(item.item_type == c.TYPE_SPELL and
                   item.name.casefold() == name.casefold()
                   for item in client.state.inventory)

    def _needs_spell_purchase(self, client: AtrinikClient) -> bool:
        return (not self._knows_spell(client, "magic bullet") and
                BuyShopUpgradeTask.wallet_value(client) >= 7_000 and
                self.clock() >= self._spell_retry_at)

    def _new_spell_purchase(self) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_1_69",
            BuySpellTask(
                "Feldain Goodwin", "magic bullet", cost=6_000),
            # Feldain occupies (12, 3); (12, 4) is an authored blocking shop
            # fixture. Route to a reachable adjacent dialogue square instead
            # of failing the entire purchase before leaving the farm circuit.
            destination_xy=(12, 3), combat_approach=True)
        task.navigation.tolerance = 1
        return task

    def _leg_available(self, index: int) -> bool:
        minute = self.server_clock.game_minute()
        if minute is None:
            return True
        zone, target = self.legs[index]
        priorities = self.graph.farm_priorities(zone, target)
        scheduled = [spawn for spawn in priorities
                     if spawn.start_minute is not None]
        if not scheduled:
            return True
        future_minute = minute + self.switch_grace / 3.125
        return any(spawn.available_at(minute) and
                   spawn.available_at(future_minute)
                   for spawn in scheduled)

    def _next_available_leg(self, start: int) -> int:
        for offset in range(len(self.legs)):
            index = (start + offset) % len(self.legs)
            if self._leg_available(index):
                return index
        return start % len(self.legs)

    def _leg_ready(self, index: int, level: int) -> bool:
        path, target = self.legs[index]
        if ("lost soul" in target.casefold() and
                time.time() - self._farm_zone_last_checked.get(
                    path, 0.0) < self.OPTIONAL_RARE_DETOUR_INTERVAL):
            return False
        return self._leg_available(index)

    def _mark_current_optional_check(self, client: AtrinikClient) -> None:
        path, target = self.legs[self.leg_index]
        if "lost soul" not in target.casefold():
            return
        checked_at = time.time()
        self._farm_zone_last_checked[path] = checked_at
        setter = getattr(client, "set_farm_zone_last_checked", None)
        if setter is not None:
            setter(path, checked_at)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "rare-detour-checked",
                f"{path}; next check in "
                f"{self.OPTIONAL_RARE_DETOUR_INTERVAL // 60}m")

    def _next_ready_leg(self, start: int, level: int) -> int:
        for offset in range(len(self.legs)):
            index = (start + offset) % len(self.legs)
            if self._leg_ready(index, level):
                return index
        return start % len(self.legs)

    @staticmethod
    def _live_matching_targets(client: AtrinikClient,
                               farm: FarmTask) -> int:
        if farm.target_pattern is None:
            return sum(1 for _ in client.state.map.targets(friendly=False))
        return sum(
            1 for _, _, obj in client.state.map.targets(friendly=False)
            if not farm.target_temporarily_unreachable(obj.target_id) and
            farm.target_pattern.search(
                f"{obj.name} {map_object_visual_name(client, obj)}"))

    @staticmethod
    def _has_proper_named_priority(farm: FarmTask) -> bool:
        return any(named[:1].isupper()
                   for _, _, named in farm.priority_spawns)

    @staticmethod
    def _busy_finishing_fight_or_loot(client: AtrinikClient,
                                      farm: FarmTask, *,
                                      held: bool = False) -> bool:
        """Whether a fight or loot is still being finished.

        ``held`` (held for a route-death verdict): only an engagement of
        hers or hostile contact in this life counts. A corpse or loot left
        by her defence does not, nor a target the server selected for her
        (attack_player_retarget) that has stopped attacking, such as a
        stationary guard still in view: either would hold her for good.
        """
        selected_is_visible = any(
            obj.target_id == client.state.target_id
            for _, _, obj in client.state.map.targets(friendly=False))
        corpse_with_contents = any(
            "corpse" in item.name.casefold() and
            bool(client.state.inventories.get(item.tag))
            for item in client.state.ground)
        ground_corpse_tags = {
            item.tag for item in client.state.ground
            if "corpse" in item.name.casefold()
        }
        corpse_operation = bool(
            farm._corpse_step_attempt or
            any(tag in ground_corpse_tags for tag in farm._corpse_take_all))
        # A fresh target selection can arrive one protocol update before its
        # map object enters the viewport.  Positive selected-target HP is
        # authoritative enough to prevent a cross-map pull during that gap.
        selected_is_alive = bool(
            client.state.combat and client.state.target_id and
            int(client.state.stats.get("target_hp", 0) or 0) > 0)
        # Hits from before her last death were her last life's fight.
        fighting = bool(_contact_this_life(client) or farm._engaged_target)
        if held:
            return fighting
        return fighting or selected_is_visible or selected_is_alive or bool(
            corpse_with_contents or
            corpse_operation or farm._suspected_corpse_tiles or
            farm._suspected_corpse_probe)

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self._adaptations = getattr(client.state, "farm_adaptations", {}) or {}
        if self.clear_hostile_route:
            m = client.state.map
            self._expedition_origin = (m.path, m.world_x, m.world_y)
        self._starting_exp = int(client.state.stats.get("exp", 0) or 0)
        self._current_exp = self._starting_exp
        self._xp_started_at = self.clock()
        self._checkpoint_at = self.clock()
        self._checkpoint_exp = self._starting_exp
        self._progression_level = self._combat_progression_level(client)
        self._decision_history_index = len(getattr(
            client, "decision_history", ()))
        history = getattr(client, "decision_history", ())
        self._decision_history_time = (
            float(history[-1].get("time", 0.0) or 0.0)
            if history else 0.0)
        self._farm_zone_last_checked = dict(getattr(
            client.state, "farm_zone_last_checked", {}) or {})
        current_path = client.state.map.path
        if (time.time() - self._farm_zone_last_checked.get(
                current_path, 0.0) < self.OPTIONAL_RARE_DETOUR_INTERVAL):
            expiry = (time.monotonic() +
                      FarmTask.UNREACHABLE_TARGET_COOLDOWN_SECONDS)
            self._unreachable_targets.update({
                obj.target_id: expiry
                for _, _, obj in client.state.map.targets(friendly=False)
                if obj.target_id
            })
        self.leg_index = self._next_ready_leg(
            self.leg_index, self._progression_level)
        self.child = self._new_child()

    def _hostile_route_abort_reason(self, client: AtrinikClient) -> str:
        """Reject a staged-clear premise once independently named adds join."""
        if (not self.clear_hostile_route or
                self._expedition_origin is None or
                self._expedition_return is not None or
                self.child is None or
                client.state.map.path == self.child.navigation.destination):
            return ""
        attackers = sorted(recent_hostile_attackers(client, seconds=6.0))
        if len(attackers) < 2:
            return ""
        return "multiple transit attackers joined: " + ", ".join(attackers)

    async def _tick_expedition_return(self, client: AtrinikClient) -> bool:
        """Continuously retrace to the expedition origin after unsafe adds."""
        reason = self._hostile_route_abort_reason(client)
        if reason:
            assert self._expedition_origin is not None
            path, x, y = self._expedition_origin
            self._expedition_abort_reason = reason
            self._expedition_return = NavigateTask(
                self.graph, path, (x, y), tolerance=1)
            await client.clear_actions()
            await client.set_combat(False)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("hostile-route-abort", reason)
            log.warning("hostile-route expedition aborting to %s: %s",
                        path, reason)
        if self._expedition_return is None:
            return False
        await self._expedition_return.tick(client)
        if self._expedition_return.status == TaskStatus.COMPLETE:
            self.fail("hostile-route expedition aborted safely: " +
                      self._expedition_abort_reason)
        elif self._expedition_return.status == TaskStatus.FAILED:
            # Keep the parent alive so the normal engine safety loop cannot
            # inherit an idle character under pursuit. Replan from the latest
            # authoritative position on the following tick.
            self._expedition_return.status = TaskStatus.READY
            self._expedition_return.error = ""
            self._expedition_return.route = []
        return True

    async def _await_recall_wand_charges(self, client: AtrinikClient,
                                         farm: FarmTask) -> bool:
        """Hold the walk while a recall wand of unknown charge is examined.

        True while a wand examine waits for its answer
        (FarmTask.check_recall_wand gives it up after 5 s), so she walks out
        only once every wand's charge is known or its examine timed out. A
        gear examine still waiting is abandoned at once (GearExaminer.abandon
        pauses the examiner until its late answer has passed), so the hold
        is never longer than the wand's own. The farm
        examines on its own quiet ticks, but only on the farm map: live, each
        life brought new tags for her two empty wands (most likely her
        checkpoint relogs), the empty marks were lost with the old tags, and
        she walked back out to fight with no escape. An empty wand found here
        is no longer an emergency candidate, and recall shopping may replace
        it; she farms on without it (owner decision, 2026-11-22). Never
        while she is being hit.
        """
        if getattr(client, "examine", None) is None or _contact_this_life(client):
            return False
        if farm._wand_examine is None and farm.next_recall_wand(client) is not None:
            abandon_gear_examine(client)
        await farm.check_recall_wand(client)
        if farm._wand_examine is None:
            # That answer settled one wand: start the next, if any.
            if farm.next_recall_wand(client) is not None:
                abandon_gear_examine(client)
            await farm.check_recall_wand(client)
        return farm._wand_examine is not None

    async def _tick_running_service(self, client: AtrinikClient) -> bool:
        """Tick the maintenance trip under way, if any, and settle its end.

        True when one ran this tick. Autoplay's recovery phase
        (RecoveryTask) runs the same trips through this.
        """
        if self._bank_sync is not None:
            await self._bank_sync.tick(client)
            if self._bank_sync.status in (TaskStatus.COMPLETE,
                                           TaskStatus.FAILED):
                if self._bank_sync.status == TaskStatus.FAILED:
                    log.warning("bank balance sync failed: %s",
                                self._bank_sync.error)
                    self._bank_sync_retry_at = (
                        self.clock() + self._bank_sync_backoff(self._bank_sync))
                else:
                    log.info("bank balance synchronized: %s",
                             format_money(client.state.bank_balance))
                    self._bank_sync_retry_at = self.clock() + 3600.0
                self._bank_sync = None
                # Chain urgent stat restoration before a dense farm respawn
                # can acquire a new target and starve the service indefinitely.
                if self._queue_depletion_service(client):
                    await client.clear_actions()
                    await client.set_combat(False)
                    await self._restoration.tick(client)
            return True
        if self._capability is not None:
            purpose = self._capability.purpose
            await self._capability.tick(client)
            if self._capability.status in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                if self._capability.status == TaskStatus.COMPLETE:
                    log.info("%s maintenance capability succeeded", purpose)
                    self._capability_retry_at[purpose] = self.clock() + 1.0
                    # A pause after a success, not a backoff: carried food
                    # still waits for the next use.
                    self._capability_succeeded.add(purpose)
                    if purpose == InventoryCapabilityTask.PURPOSE_DISEASE:
                        self._disease_suspected = False
                    elif purpose == InventoryCapabilityTask.PURPOSE_DEPLETION:
                        setter = getattr(client, "set_depletion_points", None)
                        if setter is not None:
                            setter(0)
                else:
                    log.warning("%s maintenance capability failed: %s",
                                purpose, self._capability.error)
                    self._capability_succeeded.discard(purpose)
                    self._capability_retry_at[purpose] = (
                        self.clock() + 30 * 60)
                self._capability = None
            return True
        if self._cure is not None:
            await self._cure.tick(client)
            if self._cure.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                if self._cure.status == TaskStatus.FAILED:
                    log.warning("disease cure purchase failed: %s",
                                self._cure.error)
                    self._cure_retry_at = self.clock() + 1800.0
                else:
                    log.info("cure-illness potion acquired; leaving shop to pay")
                    self._cure_retry_at = self.clock() + 30.0
                self._cure = None
            return True
        if self._restoration is not None:
            await self._restoration.tick(client)
            if self._restoration.status in (TaskStatus.COMPLETE,
                                             TaskStatus.FAILED):
                if self._restoration.status == TaskStatus.FAILED:
                    log.warning("depletion removal failed: %s",
                                self._restoration.error)
                    backoff = self.failure_backoff(
                        self._restoration.error,
                        self.restoration_backoff(self._restoration.error))
                    self._restoration_retry_at = self.clock() + backoff
                    if backoff == self.PAID_RESTORATION_RETRY_SECONDS:
                        # Survives restarts, which rebuild this task.
                        deferrer = getattr(client, "defer_paid_restoration", None)
                        if deferrer is not None:
                            deferrer(time.time() + backoff)
                else:
                    log.info("depletion removed; returning to farm")
                    self._restoration_retry_at = self.clock() + 30.0
                self._restoration = None
            return True
        if self._ship_key is not None:
            await self._ship_key.tick(client)
            if self._ship_key.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                if self._ship_key.status == TaskStatus.FAILED:
                    log.warning("ship key acquisition failed: %s",
                                self._ship_key.error)
                    self._ship_key_retry_at = self.clock() + 1800.0
                    if (int(client.state.stats.get("level", 0) or 0) >=
                            RECALL_REQUIRED_LEVEL and
                            not has_working_recall(client)):
                        self.note_recall_unavailable(
                            client, "recall shopping needs Morg'eean's ship "
                            f"key, and getting it failed: {self._ship_key.error}")
                else:
                    log.info("Morg'eean ship key acquired; shortcuts enabled")
                    self._ship_key_retry_at = self.clock() + 86400.0
                self._ship_key = None
            return True
        if self._spell_purchase is not None:
            await self._spell_purchase.tick(client)
            if self._spell_purchase.status in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                if self._spell_purchase.status == TaskStatus.FAILED:
                    log.warning("spell purchase failed: %s",
                                self._spell_purchase.error)
                    self._spell_retry_at = self.clock() + 1800.0
                else:
                    log.info("magic bullet learned; returning to farm")
                    self._spell_retry_at = self.clock() + 86400.0
                self._spell_purchase = None
            return True
        if self._recall_shopping is not None:
            await self._recall_shopping.tick(client)
            if self._recall_shopping.status in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                shopper = self._recall_shopping.task
                if self._recall_shopping.status == TaskStatus.FAILED:
                    log.warning("recall shopping failed: %s",
                                self._recall_shopping.error)
                    self._recall_shopping_retry_at = self.clock() + 7200.0
                    self.note_recall_unavailable(
                        client, "recall shopping failed: "
                        f"{self._recall_shopping.error}")
                else:
                    setter = getattr(
                        client, "set_last_recall_shop_check", None)
                    if setter is not None:
                        setter(time.time())
                    else:
                        client.state.last_recall_shop_check_at = time.time()
                    purchased = bool(
                        isinstance(shopper, BuyDialogueStockTask) and
                        shopper.purchased)
                    log.info(
                        "recall stock check complete%s; returning to farm",
                        " with purchase" if purchased else "")
                    self.note_recall_unavailable(client, "" if purchased else
                                                 "recall shopping found no "
                                                 "recall source to buy")
                    self._recall_shopping_retry_at = self.clock() + 60.0
                self._recall_shopping = None
            return True
        if self._heal_restock is not None:
            await self._heal_restock.tick(client)
            if self._heal_restock.status in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                shopper = self._heal_restock.task
                if (self._heal_restock.status == TaskStatus.COMPLETE and
                        isinstance(shopper, BuyHealingStockTask) and
                        shopper.purchased):
                    note_cure_funding_errand(client)
                self._settle_heal_restock(client)
            return True
        if self._utility_shopping is not None:
            await self._utility_shopping.tick(client)
            if self._utility_shopping.status in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                shopper = self._utility_shopping.task
                if self._utility_shopping.status == TaskStatus.FAILED:
                    log.warning("utility stock check failed: %s",
                                self._utility_shopping.error)
                    self._utility_shopping_retry_at = self.clock() + 7200.0
                else:
                    checked_at = time.time()
                    setter = getattr(
                        client, "set_last_utility_shop_check", None)
                    if setter is not None:
                        setter(checked_at)
                    else:
                        client.state.last_utility_shop_check_at = checked_at
                    purchases = (shopper.purchase_count
                                 if isinstance(shopper,
                                               BuyDialogueStockTask) else 0)
                    log.info(
                        "strategic utility stock check complete with %s "
                        "purchase(s); returning to farm", purchases)
                    self._utility_shopping_retry_at = self.clock() + 60.0
                self._utility_shopping = None
            return True
        if self._shopping is not None:
            previous_shop_index = self._shopping.index
            await self._shopping.tick(client)
            if (self._shopping.status == TaskStatus.RUNNING and
                    self._shopping.index != previous_shop_index):
                cursor_setter = getattr(
                    client, "set_active_upgrade_shop_sweep", None)
                if cursor_setter is not None:
                    cursor_setter(
                        self._shopping.index,
                        policy=self.UPGRADE_SWEEP_POLICY)
                else:
                    client.state.active_upgrade_shop_sweep_cursor = (
                        self._shopping.index)
                    client.state.active_upgrade_shop_sweep_policy = (
                        self.UPGRADE_SWEEP_POLICY)
            if self._shopping.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                if self._shopping.status == TaskStatus.FAILED:
                    log.warning("upgrade shopping failed: %s",
                                self._shopping.error)
                    self._shopping_retry_at = self.clock() + 7200.0
                elif self._shopping.purchased:
                    # One purchase must settle by leaving its shop floor
                    # before another can be budgeted. Do not mark the regional
                    # policy complete: resume shortly and keep scanning until
                    # an entire pass finds no further upgrade.
                    log.info(
                        "upgrade purchased; settling payment before "
                        "continuing regional sweep")
                    setter = getattr(
                        client, "set_active_upgrade_shop_sweep", None)
                    cursor = self._shopping.index + 1
                    if setter is not None:
                        setter(cursor, policy=self.UPGRADE_SWEEP_POLICY)
                    else:
                        client.state.active_upgrade_shop_sweep_cursor = cursor
                        client.state.active_upgrade_shop_sweep_policy = (
                            self.UPGRADE_SWEEP_POLICY)
                    self._shopping_retry_at = self.clock() + 30.0
                else:
                    log.info("upgrade shop sweep complete; returning to farm")
                    setter = getattr(
                        client, "set_last_upgrade_shop_sweep", None)
                    if setter is not None:
                        setter(
                            time.time(),
                            level=int(client.state.stats.get(
                                "level", 0) or 0),
                            wallet=BuyShopUpgradeTask.wallet_value(client),
                            policy=self.UPGRADE_SWEEP_POLICY)
                    else:
                        client.state.last_upgrade_shop_sweep_at = time.time()
                        client.state.last_upgrade_shop_sweep_level = int(
                            client.state.stats.get("level", 0) or 0)
                        client.state.last_upgrade_shop_sweep_wallet = (
                            BuyShopUpgradeTask.wallet_value(client))
                        client.state.last_upgrade_shop_sweep_policy = (
                            self.UPGRADE_SWEEP_POLICY)
                    cursor_setter = getattr(
                        client, "set_active_upgrade_shop_sweep", None)
                    if cursor_setter is not None:
                        cursor_setter(0, policy=0)
                    else:
                        client.state.active_upgrade_shop_sweep_cursor = 0
                        client.state.active_upgrade_shop_sweep_policy = 0
                    self._shopping_retry_at = self.clock() + 7200.0
                self._shopping = None
            return True
        if self._banking is not None:
            await self._banking.tick(client)
            if self._banking.status == TaskStatus.FAILED:
                log.warning("bank deposit failed: %s", self._banking.error)
                self._banking_retry_at = self.clock() + self.failure_backoff(
                    self._banking.error, 1800.0)
                self._strict_trip_refused(
                    client, "bank deposit", self._banking.error)
                self._banking = None
            elif self._banking.status == TaskStatus.COMPLETE:
                log.info("all carried cash banked; returning to farm")
                note_cure_funding_errand(client)
                setter = getattr(client, "set_last_bank_deposit", None)
                if setter is not None:
                    setter(time.time())
                else:
                    client.state.last_bank_deposit_at = time.time()
                self._banking_retry_at = self.clock() + 3600.0
                self._force_bank_deposit = False
                self._banking = None
            return True
        if self._selling is not None:
            await self._selling.tick(client)
            if self._selling.status == TaskStatus.FAILED:
                log.warning("identified junk sale failed: %s",
                            self._selling.error)
                self._selling_retry_at = self.clock() + self.failure_backoff(
                    self._selling.error, 1800.0)
                self._strict_trip_refused(
                    client, "junk sale", self._selling.error)
                self._selling = None
            elif self._selling.status == TaskStatus.COMPLETE:
                log.info("identified junk sale complete; returning to farm")
                note_cure_funding_errand(client)
                # Sale proceeds can be far larger than routine corpse coins.
                # Invalidate travel batching so the next safe maintenance tick
                # deposits the entire payment immediately.
                setter = getattr(client, "set_last_bank_deposit", None)
                if setter is not None:
                    setter(0.0)
                else:
                    client.state.last_bank_deposit_at = 0.0
                self._banking_retry_at = 0.0
                self._force_bank_deposit = True
                self._selling = None
            return True
        if self._identification is not None:
            await self._identification.tick(client)
            if self._identification.status == TaskStatus.FAILED:
                log.warning("mass identification failed: %s",
                            self._identification.error)
                self._identification_retry_at = self.clock() + self.failure_backoff(
                    self._identification.error, 1800.0)
                self._strict_trip_refused(
                    client, "identify-all", self._identification.error)
                self._identification = None
            elif self._identification.status == TaskStatus.COMPLETE:
                # Evaluate the newly revealed batch before returning to a
                # spawn map, where an immediate target could otherwise defer
                # a weapon or armour upgrade indefinitely.
                farm = self.child.task if self.child is not None else None
                if (isinstance(farm, FarmTask) and
                        await farm.maintain_inventory(client)):
                    return True
                log.info("mass identification complete; returning to farm")
                note_cure_funding_errand(client)
                self._identification_retry_at = self.clock() + 3600.0
                self._identification = None
            return True
        if self._cure_funding is not None:
            await self._cure_funding.tick(client)
            if self._cure_funding.status in (TaskStatus.FAILED,
                                             TaskStatus.COMPLETE):
                self._settle_cure_funding(client)
            return True
        if self._storage is not None:
            await self._storage.tick(client)
            if self._storage.status == TaskStatus.FAILED:
                log.warning("unidentified storage failed: %s",
                            self._storage.error)
                self._storage_retry_at = self.clock() + self.failure_backoff(
                    self._storage.error, 1800.0)
                self._strict_trip_refused(
                    client, "apartment storage", self._storage.error)
                self._storage = None
            elif self._storage.status == TaskStatus.COMPLETE:
                log.info("apartment loot storage complete; returning to farm")
                note_cure_funding_errand(client)
                self._storage = None
            return True
        if self._bed_binding is not None:
            await self._bed_binding.tick(client)
            target = self._bed_target or ("?", (0, 0), 0)
            if self._bed_binding.status == TaskStatus.FAILED:
                # Try the next-nearest bed shortly, another one in the same
                # room first; this tile stays excluded for the circuit.
                log.warning("savebed binding at %s %s failed: %s",
                            target[0], target[1], self._bed_binding.error)
                self._bed_failed.add((target[0], target[1]))
                self._bed_retry_at = self.clock() + 300.0
                self._bed_binding = None
            elif self._bed_binding.status == TaskStatus.COMPLETE:
                log.info("savebed bound at %s %s, about %d tiles from the farm",
                         target[0], target[1], target[2])
                self._bed_binding = None
            return True
        if self._resupply is not None:
            await self._resupply.tick(client)
            self._settle_resupply(client)
            return True
        return False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self.status != TaskStatus.RUNNING or client.state.phase != "playing":
            return
        self._current_exp = int(client.state.stats.get("exp", 0) or 0)
        self._current_map_path = client.state.map.path
        self._adaptations = getattr(client.state, "farm_adaptations", {}) or {}
        if await self._tick_expedition_return(client):
            return
        # PLAYER precedes the authoritative inventory replay on login and
        # reconnect. Do not infer a missing weapon skill, food stack, key,
        # spell, cure, device, or equipped upgrade in that short gap. Such an
        # inference can select the wrong combat tier or launch a cross-world
        # maintenance trip which remains queued after the real bag arrives.
        if not self._inventory_ready(client):
            return
        level = self._combat_progression_level(client)
        self._progression_level = level
        await self._clear_combat_after_death(client)
        if self._observe_farm_death(client):
            farm = self.child.task if self.child is not None else None
            if isinstance(farm, FarmTask) and self._upgrade_progression(
                    client, farm):
                return
        # Nothing left she may pull here: the choice below moves her on.
        self._observe_out_of_league(client)
        if (self.level_until and
                int(client.state.stats.get("level", 0)) >= self.level_until):
            self.complete()
            return
        self._observe_disease(client)
        if await self.server_clock.sync(client):
            return
        assert self.child is not None
        farm = self.child.task
        if isinstance(farm, FarmTask) and self._upgrade_progression(
                client, farm):
            return
        if (isinstance(farm, FarmTask) and
                self._review_combat_build(client, farm)):
            return
        if (not self._leg_ready(self.leg_index, level) and
                isinstance(farm, FarmTask) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            self.leg_index = self._next_ready_leg(
                self.leg_index + 1, level)
            self.child = self._new_child()
            self._farm_started_at = 0.0
            self._empty_spawn_since = 0.0
            log.info("scheduled farm leg unavailable; switching to %s",
                     self.legs[self.leg_index][0])
            return
        if await self._tick_running_service(client):
            return
        farm = self.child.task
        # All active maintenance branches returned above. Checkpoint only at
        # this boundary so reconnect cannot split a bank, shop, storage, or
        # resupply transaction in addition to the fight/corpse safety gate.
        if (isinstance(farm, FarmTask) and
                await self._maintain_safe_inventory(client, farm)):
            return
        if (isinstance(farm, FarmTask) and
                self._needs_progress_checkpoint(client, farm)):
            await self._progress_checkpoint(client)
            return
        # Persistent bank funds are server state, so learn them once after a
        # process restart before any affordability or service decision.
        if (isinstance(farm, FarmTask) and self._needs_bank_sync(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            # With a stale balance known, keep it (cure funding still needs
            # the known shortfall if the trip fails) and ask for a fresh one.
            self._bank_sync = self._new_bank_sync(
                refresh=client.state.bank_balance_known)
            await self._bank_sync.tick(client)
            return
        # Treat disease before routine logistics: corpse-trap diseases can
        # deal damage and reduce combat stats indefinitely. Never interrupt an
        # active target or corpse operation, and never consume an unidentified
        # or still-unpaid potion.
        if (isinstance(farm, FarmTask) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            if self._queue_inventory_capability(
                    client, InventoryCapabilityTask.PURPOSE_DISEASE):
                await client.clear_actions()
                await client.set_combat(False)
                await self._capability.tick(client)
                return
            if await self._apply_disease_cure(client):
                return
            if self._needs_disease_cure_purchase(client):
                await client.clear_actions()
                await client.set_combat(False)
                self._cure = self._new_disease_cure()
                await self._cure.tick(client)
                return
            if self._queue_inventory_capability(
                    client, InventoryCapabilityTask.PURPOSE_DEPLETION):
                await client.clear_actions()
                await client.set_combat(False)
                await self._capability.tick(client)
                return
            if self._queue_depletion_service(client):
                await client.clear_actions()
                await client.set_combat(False)
                await self._restoration.tick(client)
                return
            if self._needs_cure_funding(client):
                await client.clear_actions()
                await client.set_combat(False)
                self._cure_funding = self._new_cure_funding(client)
                await self._cure_funding.tick(client)
                return
        # Survival supplies first, then clear unknown/cursed-item risk and
        # carrying pressure; discretionary shopping is always last. While a
        # survival trip's ranking runs off the loop, no later errand starts
        # in its place: the farm carries on until it is done.
        survival_pending = False
        if (isinstance(farm, FarmTask) and self._needs_food_resupply(client) and
                (self._food_urgent(client) or
                 not self._busy_finishing_fight_or_loot(client, farm))):
            resupply, kind = self._food_trip(client)
            if resupply is PLANNING:
                resupply, survival_pending = None, True
            elif resupply is None and kind == "temple":
                self._temple_food_retry_at = (
                    self.clock() + self.TEMPLE_FOOD_RETRY_SECONDS)
            elif resupply is None and kind == "shop":
                # Its ranking gave no answer (planner-timeout).
                self._resupply_retry_at = self.clock() + 1800.0
            if resupply is not None:
                await client.clear_actions()
                await client.set_combat(False)
                self._resupply, self._resupply_kind = resupply, kind
                await self._resupply.tick(client)
                return
        if (isinstance(farm, FarmTask) and
                not self._busy_finishing_fight_or_loot(client, farm) and
                self._queue_inventory_capability(
                    client, InventoryCapabilityTask.PURPOSE_IDENTIFY)):
            await client.clear_actions()
            await client.set_combat(False)
            await self._capability.tick(client)
            return
        if (isinstance(farm, FarmTask) and not survival_pending and
                self._needs_mass_identification(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._identification = self._new_mass_identification()
            await self._identification.tick(client)
            return
        if (isinstance(farm, FarmTask) and not survival_pending and
                self._needs_apartment_storage(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._storage = self._new_apartment_storage(
                keep_unknown=self._cure_identify_due(client))
            await self._storage.tick(client)
            return
        # The cure-funding farm (cure_funding_farm) makes no optional trip:
        # no savebed rebinding, shopping, ship key or spell purchase spends
        # the cure's money or time.
        funding = cure_funding_farm(client)
        if (isinstance(farm, FarmTask) and not funding and not survival_pending and
                not self._busy_finishing_fight_or_loot(client, farm) and
                self._needs_savebed(client)):
            await client.clear_actions()
            await client.set_combat(False)
            self._bed_binding = self._new_savebed(client)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                target = self._bed_target
                current = self._savebed_search(client).current_walk
                recorder("savebed-move",
                         f"{target[0]} {target[1]}: about {target[2]} tiles "
                         f"from the farm, current "
                         f"{current if current is not None else 'unknown'}")
            await self._bed_binding.tick(client)
            return
        if (isinstance(farm, FarmTask) and not survival_pending and
                self._needs_junk_sale(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._selling = self._new_junk_sale(client)
            await self._selling.tick(client)
            return
        if (isinstance(farm, FarmTask) and not survival_pending and
                self._needs_bank_deposit(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._banking = self._new_bank_deposit(client)
            await self._banking.tick(client)
            return
        # Optional trips wait while she has neither a usable heal nor a
        # ready recall below UNPREPARED_HEALTH (unprepared_for_trips), for
        # at most UNPREPARED_REST_LIMIT_SECONDS; the farm rests meanwhile
        # on the same hold (FarmTask._unprepared_rest).
        unprepared = (isinstance(farm, FarmTask) and
                      self._unprepared_hold.active(client, farm.safety))
        held = unprepared or funding or survival_pending
        # The heal restock is the one optional trip the hold leaves alone:
        # it is the cure for being unprepared. While the hold holds it goes
        # only by a strict route, and not below its flee threshold
        # (_heal_restock_may_leave); once the hold's cap runs out, like
        # any optional trip.
        # Out of heals and recall, a restock over the hostile road waits
        # for full HP and SP, checked once, before it departs; the farm
        # rests meanwhile.
        if isinstance(farm, FarmTask):
            farm.rest_for_departure = self._heal_restock_waiting(client, farm)
        if (isinstance(farm, FarmTask) and
                self._needs_heal_restock(client) and
                not self._busy_finishing_fight_or_loot(client, farm) and
                self._heal_restock_may_leave(client, unprepared)):
            # Depleted on the cure-funding farm, it never takes the hazard
            # fallback either; with no strict route it is deferred.
            strict = unprepared or funding
            # Its merchant ranking runs off the loop (BackgroundPlans): the
            # farm carries on meanwhile.
            trip = self._plans.get(
                "heal-restock", self.graph, client,
                lambda proxy: self._new_heal_restock(proxy, strict=strict), key=strict,
                priority=planner.PRIORITY_HEAL)
            recorder = getattr(client, "record_action", None)
            if trip is PLANNING:
                held = True
            elif (trip is not None and getattr(trip, "hostile_transit", False)
                    and out_of_heals_and_recall(client, farm.safety) and
                    not self._full_hp_sp(client)):
                self._heal_restock_retry_at = (
                    self.clock() + self.HEAL_RESTOCK_FULL_RECHECK_SECONDS)
                if self._heal_restock_full_since is None:
                    self._heal_restock_full_since = self.clock()
                    if recorder is not None:
                        recorder("heal-restock-waits",
                                 "no heal and no ready recall: the road to "
                                 f"{trip.navigation.destination} crosses "
                                 "hostile transit, so she waits for full "
                                 "HP and SP")
                farm.rest_for_departure = True
            elif trip is not None:
                self._heal_restock_full_since = None
                farm.rest_for_departure = False
                await client.clear_actions()
                await client.set_combat(False)
                self._heal_restock = trip
                if recorder is not None:
                    recorder("heal-restock",
                             f"{heal_consumables(client)} usable heal(s); to "
                             f"{trip.task.merchant} on "
                             f"{trip.navigation.destination}"
                             f"{' by a strict route' if strict else ''}")
                await trip.tick(client)
                return
            elif strict:
                self._heal_restock_full_since = None
                farm.rest_for_departure = False
                self._heal_restock_retry_at = (
                    self.clock() + self.HEAL_RESTOCK_STRICT_RETRY_SECONDS)
                if recorder is not None:
                    recorder("heal-restock-deferred",
                             "no strict route to a healing merchant while "
                             + ("unprepared" if unprepared
                                else "on the cure-funding farm"))
                if funding:
                    send_cure_funding_farm_home(
                        client, "heal restock: no strict route to a healing "
                                "merchant from the farm")
            elif trip is None:
                self._heal_restock_full_since = None
                farm.rest_for_departure = False
                self.back_off_heal_restock(
                    client, "no reachable merchant sells healing potions",
                    self.HEAL_RESTOCK_INTERVAL)
        if (isinstance(farm, FarmTask) and
                self._needs_ship_key(client) and not held and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._ship_key = MorgeeanShipKeyTask(self.graph)
            await self._ship_key.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_spell_purchase(client) and not held and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._spell_purchase = self._new_spell_purchase()
            await self._spell_purchase.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_recall_shopping(client) and not held and
                not self._busy_finishing_fight_or_loot(client, farm)):
            # Its vendor ranking runs off the loop (BackgroundPlans): the
            # farm carries on meanwhile.
            shopping = self._plans.get("recall-shopping", self.graph, client,
                                       self._new_recall_shopping)
            if shopping is PLANNING:
                # Recall before utility and upgrade shopping, as before.
                held = True
            elif shopping is not None:
                await client.clear_actions()
                await client.set_combat(False)
                self._recall_shopping = shopping
                await self._recall_shopping.tick(client)
                return
        if (isinstance(farm, FarmTask) and
                self._needs_utility_shopping(client) and not held and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._utility_shopping = self._new_utility_shopping(client)
            await self._utility_shopping.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_upgrade_shopping(client) and not held and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._shopping = self._new_upgrade_shopping(client)
            await self._shopping.tick(client)
            return
        # Once a leg changes, leave the respawning farm directly. The generic
        # combat-aware wrapper is intentionally biased toward clearing every
        # visible pack, which can make departure from a farm impossible. Keep
        # its health/food safety policy, but give authored routing priority
        # until the next circuit map is reached.
        if client.state.map.path != self.child.navigation.destination:
            self._reset_dwell_off_farm(client.state.map.path)
            if (isinstance(farm, FarmTask) and
                    (await self._transit_emergency_recall(client, farm) or
                     await farm.recall_pending_step(client))):
                return
            hp = int(client.state.stats.get("hp", 0) or 0)
            maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
            if hp / maxhp <= self.child.safety.flee_below:
                heal_action = self.child.safety._heal_action
                await self.child.safety.enforce(client)
                if self.child.safety._heal_action != heal_action:
                    return
                farm = self.child.task
                if (isinstance(farm, FarmTask) and
                        await farm.low_health_retreat(client)):
                    return
            elif not await self.child.safety.enforce(client):
                return
            # After recall, healing and the low-health retreat had their say:
            # do not walk on across a map that just killed her.
            if (isinstance(farm, FarmTask) and
                    await self._hold_for_route_death_verdict(client)):
                return
            if (isinstance(farm, FarmTask) and
                    await self._await_recall_wand_charges(client, farm)):
                return
            # A newly selected circuit leg can begin while the previous farm's
            # pack is still pursuing across a seam. Defend/retreat before route
            # planning so an exceptional disconnected start square cannot fail
            # the task and leave idle safety healing in place under attack.
            if await self.child._defend_while_navigating(client):
                return
            if self.child.navigation.status == TaskStatus.COMPLETE:
                self.child.navigation.status = TaskStatus.RUNNING
                self.child.navigation.route = []
                self.child.navigation._issued_goal = None
                self.child.navigation._issued_click = None
                self.child.navigation._last_progress = time.monotonic()
            await self.child.navigation.tick(client)
            if self.child.navigation.status == TaskStatus.FAILED:
                if self._retry_unavoidable_return_route(client):
                    return
                if (isinstance(farm, FarmTask) and
                        await self._recover_failed_navigation(client, farm)):
                    return
                if (self.analyzer_driven and isinstance(farm, FarmTask) and
                        self._exclude_unreachable_leg(client, farm)):
                    return
                self.fail(self.child.navigation.error)
            return
        # Held only in transit: on the farm, its loot counts as busy again.
        self._verdict_hold = False
        # A combat-aware wrapper may defend before ticking a zero-length
        # NavigateTask. Mark an already-reached leg explicitly so permanent
        # surface respawns cannot prevent the circuit timer from starting.
        if client.state.map.path == self.child.navigation.destination:
            self.child.navigation.complete()
        await self.child.tick(client)
        if self.child.status == TaskStatus.FAILED:
            self.fail(self.child.error)
            return
        if self.child.navigation.status != TaskStatus.COMPLETE:
            self._farm_started_at = 0.0
            return
        if (isinstance(farm, FarmTask) and
                self._observe_stalled_farm(client, farm)):
            if self._upgrade_progression(client, farm):
                return
        now = self.clock()
        if not self._farm_started_at:
            self._farm_started_at = now
            return
        elapsed = now - self._farm_started_at
        farm = self.child.task
        live_targets = (self._live_matching_targets(client, farm)
                        if isinstance(farm, FarmTask) else 0)
        if live_targets:
            self._empty_spawn_since = 0.0
        named_spawn_empty = bool(
            isinstance(farm, FarmTask) and farm.priority_spawns and
            self._has_proper_named_priority(farm))
        patrol_swept_empty = bool(
            isinstance(farm, FarmTask) and farm.patrol and
            farm._patrol_index >= len(farm.patrol))
        if (isinstance(farm, FarmTask) and
                (named_spawn_empty or patrol_swept_empty) and
                live_targets == 0 and elapsed >= 15.0 and
                not self._busy_finishing_fight_or_loot(client, farm) and
                not farm.training_rest_active() and
                not self._unprepared_hold.holding()):
            if not self._empty_spawn_since:
                self._empty_spawn_since = now
                return
            if now - self._empty_spawn_since < 3.0:
                return
            next_leg = self._next_ready_leg(self.leg_index + 1, level)
            # When every other leg is time-gated, "switching" back to this
            # leg only recreates FarmTask. Besides needless path churn, that
            # discards its terminal empty-corpse and blocked-tile memory.
            if next_leg == self.leg_index:
                return
            await client.clear_actions()
            await client.set_combat(False)
            self._mark_current_optional_check(client)
            self.leg_index = next_leg
            self.child = self._new_child()
            self._farm_started_at = 0.0
            self._empty_spawn_since = 0.0
            log.info("farm sweep empty; switching early to %s",
                     self.legs[self.leg_index][0])
            return
        if elapsed < self.dwell_seconds:
            return
        # Dwell expiry is permission to leave only when combat and looting
        # are actually finished.  A long fight must never become disposable
        # merely because the old grace timer elapsed; doing so can drag a
        # late-spawning attacker into the next map's pack.
        if (isinstance(farm, FarmTask) and
                self._busy_finishing_fight_or_loot(client, farm)):
            return
        # Nor mid-rest for mana, or she sets off with a partial bar. Services
        # may still detour during a rest.
        if isinstance(farm, FarmTask) and farm.training_rest_active():
            return
        # Nor while she rests without a heal or a recall: no unprepared
        # walk to another map; the hold's cap still runs.
        if self._unprepared_hold.holding():
            return
        next_leg = self._next_ready_leg(self.leg_index + 1, level)
        if next_leg == self.leg_index:
            # Preserve the active farm's learned state until another leg
            # actually becomes available (for example, at nightfall).
            self._farm_started_at = self.clock()
            return
        await client.clear_actions()
        await client.set_combat(False)
        self._mark_current_optional_check(client)
        self.leg_index = next_leg
        self.child = self._new_child()
        self._farm_started_at = 0.0
        self._empty_spawn_since = 0.0
        log.info("farm circuit switching to %s", self.legs[self.leg_index][0])


class _ArriveTask(BotTask):
    """Nothing to do on arrival: a walk whose only purpose is to get there."""

    def __init__(self, name: str = "arrive"):
        super().__init__(name)

    async def tick(self, client: AtrinikClient) -> None:
        self.complete()


class RecoveryTask(BotTask):
    """Autoplay's recovery phase while farming is unsafe (farming_unsafe_reason).

    Live, on 2026-11-17, Sera kept farming the Burnt Forest depleted to 92
    points, with both recall wands empty and the cure unaffordable, and
    died ten times in under three hours. Autoplay runs this phase instead of
    a farm circuit whenever farming is unsafe, whether or not the farming
    analyzer ranks a spot. Each tick:

    - while she is hit: the emergency recall, by the farm's own rule, then
      healing, the low-health retreat and defence where she stands, or the
      walk under way defends itself. There is no farm route to resume;
    - healing and eating (her safety policy; autoplay's temple food trip
      covers hunger with nothing to eat);
    - off her savebed with nothing running: a walk home, strictly safe
      where such a route exists and otherwise by the ordinary route
      (recovery-home-unsafe-route), which from level 18 may cross hazard
      maps as the farm return does, resting first on a safe map.
      Three failed walks in a row raise home-unreachable. Every errand
      starts from her savebed;
    - depleted: the temple cure when she can pay (a depletion potion
      first), otherwise the cure-funding path: identify-all, junk sale,
      deposit, the apartment chest. No recall is bought with the cure's
      money: recall shopping is the farm circuit's, never recovery's
      (owner decision, 2026-11-22);
    - depleted, short of the cure, and with none of those trips left to
      start (each found nothing or failed and waits its retry): the
      cure-funding farm (cure_funding_farm, owner decision, 2026-11-23).
      It records cure-funding-farm and completes, and autoplay farms the
      safest spot until the cure is affordable; then recovery takes her
      to the temple. After CURE_FUNDING_FARM_DEATHS deaths while funding
      she waits here again, for CURE_FUNDING_FARM_BACKOFF_SECONDS;
    - otherwise she waits at her savebed. No farm walk, savebed rebinding
      or discretionary purchase runs.

    The trips are the farm circuit's own (``services``, a FarmCircuitTask
    that is never started), with their retry deadlines; ``alerts`` reads
    what blocks her from live state on every call. Completes, recording
    farming-resumed, once nothing is unsafe and nothing is under way.
    """

    survives_disconnect = False
    # A strict temple trip refused this many times in a row for want of a
    # safe route raises cure-unreachable.
    CURE_ROUTE_ATTEMPTS = 3
    # A strict walk home that found no route waits this long.
    HOME_RETRY_SECONDS = FarmCircuitTask.STRICT_ROUTE_RETRY_SECONDS
    # Within this many tiles of her savebed she is home.
    HOME_TOLERANCE = 2
    # This many walks home failing in a row raise home-unreachable.
    HOME_ROUTE_ATTEMPTS = 3

    def __init__(self, graph: WorldGraph, *,
                 backoffs: dict[str, float] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__("recovery")
        self.graph = graph
        self.clock = clock
        self.services = FarmCircuitTask(
            graph, [("/recovery", "")], backoffs=backoffs, clock=clock)
        # Fights only through emergency_recall, low_health_retreat and the
        # defence below; it never farms.
        self.guard = FarmTask()
        self.safety = SafetyPolicy()
        self._defense = NavigateThenTask(graph, "/recovery", _ArriveTask(),
                                         combat_approach=True)
        self._home_walk: NavigateThenTask | None = None
        self._home_retry_at = 0.0
        # The rest before a hazard crossing home ends by this clock time.
        self._hazard_rest_until: float | None = None
        # Walks home failed in a row, and the latest failure.
        self._home_failures = 0
        self._home_error = ""
        # Strict-route refusals of the cure are recorded decisions
        # (cure_route_refusals): they outlive a temple food trip, Stop and
        # restarts.
        self.reason = ""

    def backoffs(self) -> dict[str, float]:
        return self.services.backoffs()

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self.reason = farming_unsafe_reason(client)
        log.warning("farming suspended: %s", self.reason or "?")
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farming-suspended", self.reason)

    def _home(self, client: AtrinikClient) -> tuple[str, tuple[int, int]] | None:
        """Her savebed, if known and mapped."""
        path = getattr(client.state, "savebed_map", "") or ""
        xy = getattr(client.state, "savebed_xy", None)
        if not path or xy is None or path not in self.graph.nodes:
            return None
        return path, (int(xy[0]), int(xy[1]))

    def at_home(self, client: AtrinikClient) -> bool:
        """At her savebed; with none known, wherever she stands."""
        home = self._home(client)
        if home is None:
            return True
        m = client.state.map
        return (m.path == home[0] and
                max(abs(m.world_x - home[1][0]),
                    abs(m.world_y - home[1][1])) <= self.HOME_TOLERANCE)

    @property
    def child(self) -> BotTask | None:
        """The trip or walk under way, for the dashboard's task tree."""
        if self._home_walk is not None:
            return self._home_walk
        return next((service for service in (
            *self.services._maintenance_services(), self.services._resupply)
            if service is not None), None)

    def busy(self) -> bool:
        """Whether a trip or the walk home is under way."""
        walking = (self._home_walk is not None and
                   self._home_walk.status in (TaskStatus.READY,
                                              TaskStatus.RUNNING))
        return walking or any(service is not None for service in (
            *self.services._maintenance_services(), self.services._resupply))

    def waiting(self, client: AtrinikClient) -> bool:
        """Idle at her savebed: nothing under way, and not being hit."""
        return (not self.busy() and self.at_home(client) and
                not _contact_this_life(client))

    def _funding_running(self) -> bool:
        """Whether a funding trip is under way: only then is a short cure
        not yet unfunded. A trip that is merely due does not count, so the
        alert does not flap with every retry deadline."""
        s = self.services
        return any(task is not None for task in (
            s._cure_funding, s._selling, s._identification, s._banking,
            s._bank_sync))

    def alerts(self, client: AtrinikClient) -> list[dict[str, str]]:
        """What blocks her way out, from live state: each clears as soon
        as its condition does."""
        alerts = self._cure_alerts(client)
        if (self._home_failures >= self.HOME_ROUTE_ATTEMPTS and
                not self.at_home(client)):
            home = self._home(client)
            alerts.append({
                "id": "home-unreachable", "level": "critical",
                "text": f"Savebed unreachable: {self._home_failures} walks "
                        f"home to {home[0] if home else '?'} failed in a "
                        f"row (she retries every "
                        f"{int(self.HOME_RETRY_SECONDS)} s; content hazards "
                        f"such as pack maps stay until her level changes): "
                        f"{self._home_error}"})
        return alerts

    def _cure_alerts(self, client: AtrinikClient) -> list[dict[str, str]]:
        s = self.services
        wallet = BuyShopUpgradeTask.wallet_value(client)
        assist = "; the owner can give her gold with the operator assist"
        if depleted_for_temple(client.state):
            if s._cure_shortfall(client):
                if self._funding_running():
                    return []
                cost = s._depletion_service_cost(client)
                if cure_funding_farm_home(client):
                    return [{"id": "cure-unfunded", "level": "warn",
                             "text": "Cure unfunded: remove depletion costs "
                                     f"{format_money(cost)}, she has "
                                     f"{format_money(wallet)}; home for "
                                     "errands from the cure-funding farm, "
                                     "which resumes after them" + assist}]
                return [{"id": "cure-unfunded", "level": "critical",
                         "text": "Cure unfunded: remove depletion costs "
                                 f"{format_money(cost)}, "
                                 f"she has {format_money(wallet)} and nothing "
                                 "left to sell" +
                                 self._funding_farm_note(client) + assist}]
            failures, error = self.cure_route_refusals(client)
            if failures >= self.CURE_ROUTE_ATTEMPTS:
                return [{"id": "cure-unreachable", "level": "critical",
                         "text": "Cure unreachable: no strictly safe route to "
                                 f"the temple from her savebed after {failures} "
                                 f"attempts: {error}"}]
        return []

    @staticmethod
    def _funding_farm_note(client: AtrinikClient) -> str:
        """Why she parks rather than farms for the cure, when the
        cure-funding farm backs off after its deaths."""
        funding = cure_funding_farm_state(client)
        if funding.state == "backoff":
            minutes = max(1, int((funding.retry_at - time.time()) // 60))
            return (f"; farming to fund it stopped after "
                    f"{CURE_FUNDING_FARM_DEATHS} deaths, next try in "
                    f"{minutes // 60}h {minutes % 60:02d}m")
        if (funding.state == "farming" and
                funding.deaths >= CURE_FUNDING_FARM_DEATHS):
            return (f"; farming to fund it stopped after "
                    f"{funding.deaths} deaths")
        return ""

    def _cure_funding_farm_due(self, client: AtrinikClient) -> bool:
        """Whether to farm for the cure: depleted, short of the fee with the
        bank balance known, no funding trip under way or left to start (the
        caller found no errand), and no cure-funding farm running or
        backing off (cure_funding_farm_state)."""
        return (depleted_for_temple(client.state) and
                self.services._cure_shortfall(client) and
                not self.busy() and
                cure_funding_farm_state(client).state == "")

    # Refusals older than this no longer count toward cure-unreachable.
    CURE_ROUTE_WINDOW_SECONDS = 24 * 3600.0
    CURE_ROUTE_ACTIONS = ("cure-route-refused", "cure-route-cleared",
                          "farming-resumed")

    @classmethod
    def cure_route_refusals(cls, client: AtrinikClient) -> tuple[int, str]:
        """Strict temple trips refused in a row, and the latest refusal.

        Read from her persisted decisions, so the count survives temple
        food trips, Stop and restarts: refusals since the last
        cure-route-cleared or farming-resumed, within a day.
        """
        count, error = 0, ""
        cutoff = time.time() - cls.CURE_ROUTE_WINDOW_SECONDS
        for record in getattr(client, "decision_history", ()):
            if (not isinstance(record, dict) or
                    record.get("action") not in cls.CURE_ROUTE_ACTIONS):
                continue
            if record.get("action") != "cure-route-refused":
                count, error = 0, ""
            elif float(record.get("time", 0.0) or 0.0) >= cutoff:
                count, error = count + 1, str(record.get("detail", ""))
        return count, error

    @staticmethod
    def _record(client: AtrinikClient, action: str, detail: str) -> None:
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(action, detail)

    async def _tick_services(self, client: AtrinikClient) -> bool:
        """The circuit's trip under way, noting the outcomes alerts need."""
        s = self.services
        restoration = s._restoration
        ran = await s._tick_running_service(client)
        if restoration is not None and s._restoration is not restoration:
            if restoration.status == TaskStatus.FAILED:
                if (s.failure_backoff(restoration.error, 0.0) ==
                        s.STRICT_ROUTE_RETRY_SECONDS):
                    self._record(client, "cure-route-refused",
                                 restoration.error)
            elif self.cure_route_refusals(client)[0]:
                self._record(client, "cure-route-cleared",
                             f"temple trip {restoration.status.value}")
        return ran

    # Recovery's errands are essential trips (ROUTING 4.5): the cure and
    # what funds it.
    ESSENTIAL_ERRANDS = {
        "_restoration": "depletion", "_cure": "cure", "_bank_sync": "funding",
        "_identification": "funding", "_selling": "funding", "_banking": "funding",
        "_cure_funding": "funding",
    }

    async def _start(self, client: AtrinikClient, errand: str, task) -> bool:
        await client.clear_actions()
        await client.set_combat(False)
        if errand in self.ESSENTIAL_ERRANDS:
            mark_essential(task, self.ESSENTIAL_ERRANDS[errand])
        setattr(self.services, errand, task)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("recovery-errand", task.name)
        await self._tick_services(client)
        return True

    async def _start_errand(self, client: AtrinikClient, reason: str) -> bool:
        s = self.services
        if s._needs_bank_sync(client):
            return await self._start(client, "_bank_sync", s._new_bank_sync(
                refresh=client.state.bank_balance_known))
        if depleted_for_temple(client.state):
            if s._queue_inventory_capability(
                    client, InventoryCapabilityTask.PURPOSE_DEPLETION):
                return await self._start(client, "_capability", s._capability)
            if s._needs_depletion_service(client):
                return await self._start(
                    client, "_restoration", s._new_depletion_service(client))
            if s._needs_mass_identification(client):
                return await self._start(
                    client, "_identification", s._new_mass_identification())
            if s._needs_junk_sale(client):
                return await self._start(
                    client, "_selling", s._new_junk_sale(client))
            if s._needs_bank_deposit(client):
                return await self._start(
                    client, "_banking", s._new_bank_deposit(client))
            if s._needs_cure_funding(client):
                return await self._start(
                    client, "_cure_funding", s._new_cure_funding(client))
        return False

    def _new_home_walk(self, client: AtrinikClient,
                       home: tuple[str, tuple[int, int]], *,
                       strict: bool) -> NavigateThenTask:
        walk = NavigateThenTask(self.graph, home[0], _ArriveTask("savebed"),
                                destination_xy=home[1], combat_approach=True)
        walk.navigation.tolerance = 1
        walk.navigation.strict_safety = strict
        walk.navigation.essential = "home"
        # The ordinary walk home may clear its way through hazard maps
        # from the level the farm circuit's own return does
        # (_retry_unavoidable_return_route). Live, from 2026-11-18, the
        # Asteria sage's recall trip took that careful route out, recovery
        # began at the sage, and every way back to her Clearhaven savebed
        # crossed pack-hazard maps: 3,052 walks home failed. The hazard-free
        # route is still preferred; the planner falls back only when none
        # exists (navigation-threat-fallback). Such a walk rests first
        # where that is safe and heals early (_start_ordinary_home_walk).
        walk.navigation.allow_ranged_hazard_fallback = (
            not strict and FarmCircuitTask._combat_progression_level(client) >=
            FarmCircuitTask.SWAMP_TRANSIT_LEVEL)
        return walk

    # The walk home's safety policy once its route crosses hazard maps:
    # the recall shopping and cure trips' early healing and step-away.
    HAZARD_HEAL_BELOW = 0.85
    HAZARD_FLEE_BELOW = 0.70
    # Before such a walk departs from a map without hostile spawns she
    # rests there for full HP and SP, at most this long.
    HAZARD_REST_SECONDS = CARE_WAIT_SECONDS

    def _careful(self, walk: NavigateThenTask) -> None:
        if walk.safety.heal_below < self.HAZARD_HEAL_BELOW:
            walk.safety = SafetyPolicy(heal_below=self.HAZARD_HEAL_BELOW,
                                       flee_below=self.HAZARD_FLEE_BELOW)

    def _start_ordinary_home_walk(self, client: AtrinikClient,
                                  home: tuple[str, tuple[int, int]]
                                  ) -> NavigateThenTask:
        """The ordinary walk home, and once, before it departs, whether its
        route takes the hazard fallback. If so it heals and steps away
        early, and where she stands has no hostile spawns she rests first
        (_hazard_rest_until): depleted, she crosses rested. On a map with
        hostile spawns she goes at once, since staying is worse. Replans
        within the walk never rest again."""
        walk = self._new_home_walk(client, home, strict=False)
        self._hazard_rest_until = None
        navigation = walk.navigation
        if not navigation.allow_ranged_hazard_fallback:
            return walk
        try:
            # A planning probe: the walk has not started.
            navigation._plan(client)
        except ValueError:
            return walk
        if not navigation.hazard_fallback_taken:
            return walk
        self._careful(walk)
        here = client.state.map.path
        if not navigation._hostile_map(here):
            self._hazard_rest_until = self.clock() + self.HAZARD_REST_SECONDS
            self._record(client, "recovery-home-hazard-rest",
                         f"resting on {here} before crossing "
                         f"{', '.join(sorted(navigation._route_threat_maps))}")
        return walk

    @staticmethod
    def _rested(client: AtrinikClient) -> bool:
        stats = client.state.stats
        hp, maxhp = int(stats.get("hp", 0) or 0), int(stats.get("maxhp", 0) or 0)
        sp, maxsp = int(stats.get("sp", 0) or 0), int(stats.get("maxsp", 0) or 0)
        return maxhp > 0 and hp >= maxhp and sp >= maxsp

    async def _hazard_rest(self, client: AtrinikClient) -> bool:
        """True while she rests before a hazard crossing home."""
        until = self._hazard_rest_until
        if until is None:
            return False
        # Home already (a death during the rest respawned her there): the
        # walk completes, nothing is left to rest for.
        if (self._rested(client) or self.clock() >= until or
                self.at_home(client)):
            self._hazard_rest_until = None
            return False
        if _contact_this_life(client):
            await self._defend(client)
        else:
            await self.safety.enforce(client)
        return True

    async def _tick_home_walk(self, client: AtrinikClient) -> None:
        """One step of the walk home: strictly safe, and when no such route
        exists, by the ordinary route that only costs learned hazards more.

        Recovery usually starts where her wands were found empty, on the
        farm, and live no strictly safe route led home from any Burnt
        Forest map: holding out for one stranded her on a respawning map.
        The errands still start only from her savebed, strictly.
        """
        walk = self._home_walk
        assert walk is not None
        if await self._hazard_rest(client):
            return
        await walk.tick(client)
        if (walk.status == TaskStatus.FAILED and
                walk.navigation.strict_safety and
                FarmCircuitTask.failure_backoff(walk.error, 0.0) ==
                FarmCircuitTask.STRICT_ROUTE_RETRY_SECONDS):
            home = self._home(client)
            assert home is not None
            log.warning("recovery: no strictly safe route home (%s); taking "
                        "the ordinary route", walk.error)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("recovery-home-unsafe-route", walk.error)
            walk = self._home_walk = self._start_ordinary_home_walk(
                client, home)
            if await self._hazard_rest(client):
                return
            await walk.tick(client)
        if walk.navigation.hazard_fallback_taken:
            # A replan that took the fallback mid-walk: careful, no rest.
            self._careful(walk)
        if walk.status == TaskStatus.FAILED:
            log.warning("recovery: walk to her savebed failed: %s", walk.error)
            self._home_retry_at = self.clock() + self.HOME_RETRY_SECONDS
            self._home_walk = None
            self._home_failures += 1
            self._home_error = walk.error
            if self._home_failures == self.HOME_ROUTE_ATTEMPTS:
                # Once per streak. The retries go on, harmlessly: a learned
                # hazard expires, a content one (a pack map) lifts only at
                # a new level. The dashboard alert says why she is not home.
                self._record(client, "recovery-home-unreachable", walk.error)
        elif walk.status == TaskStatus.COMPLETE:
            self._home_walk = None
            self._home_failures, self._home_error = 0, ""

    async def _defend(self, client: AtrinikClient) -> None:
        """Hit with nothing under way: heal, flee or fight back here."""
        hp = int(client.state.stats.get("hp", 0) or 0)
        maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
        heal_action = self.safety._heal_action
        await self.safety.enforce(client)
        if self.safety._heal_action != heal_action:
            return
        if (hp / maxhp <= self.safety.flee_below and
                await self.guard.low_health_retreat(client)):
            return
        home = self._home(client)
        self._defense.navigation.destination = (
            home[0] if home is not None else client.state.map.path)
        await self._defense._defend_while_navigating(client)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if (self.status != TaskStatus.RUNNING or
                client.state.phase != "playing" or
                not client.state.inventory_replay_complete):
            return
        contact = _contact_this_life(client)
        if contact and await self.services._transit_emergency_recall(
                client, self.guard):
            return
        # A recall waiting for her action timer: no combat, only potions
        # and stepping away (FarmTask.recall_pending_step).
        if await self.guard.recall_pending_step(client):
            return
        if await self._tick_services(client):
            return
        if self._home_walk is not None:
            await self._tick_home_walk(client)
            return
        if contact:
            await self._defend(client)
            return
        if not await self.safety.enforce(client):
            return
        await self.guard.check_recall_wand(client)
        reason = farming_unsafe_reason(client)
        if not reason:
            self._resume(client)
            return
        self.reason = reason
        home = self._home(client)
        if (home is not None and not self.at_home(client) and
                self.clock() >= self._home_retry_at):
            await client.clear_actions()
            await client.set_combat(False)
            self._home_walk = self._new_home_walk(client, home, strict=True)
            self._hazard_rest_until = None
            await self._tick_home_walk(client)
            return
        # Every errand starts from her savebed; off it, she waits for the
        # next walk home.
        if self.at_home(client):
            self._home_failures, self._home_error = 0, ""
            if await self._start_errand(client, reason):
                return
            if not self.busy() and resume_cure_funding_farm(client):
                # Home from the cure-funding farm for its errands: the
                # same farm resumes, deaths kept, and the town trips it
                # came home for (the heal restock and the storage chest
                # are not recovery errands) run from here first. Only
                # their strict-route retries are cleared: a longer backoff
                # (a full chest, a failed trip) still holds.
                services = self.services
                now = services.clock()
                for name in CURE_FUNDING_FARM_HOME_TRIPS:
                    window = (services.HEAL_RESTOCK_STRICT_RETRY_SECONDS
                              if name == "_heal_restock_retry_at"
                              else services.STRICT_ROUTE_RETRY_SECONDS)
                    if getattr(services, name) - now <= window:
                        setattr(services, name, 0.0)
                self._resume(client, "back to the cure-funding farm")
            elif self._cure_funding_farm_due(client):
                self._start_cure_funding_farm(client)

    def _resume(self, client: AtrinikClient, note: str = "") -> None:
        log.info("farming resumed (was suspended: %s)%s", self.reason,
                 f": {note}" if note else "")
        self._record(client, "farming-resumed",
                     f"was suspended: {self.reason}" +
                     (f"; {note}" if note else ""))
        self.complete()

    def _start_cure_funding_farm(self, client: AtrinikClient) -> None:
        """Nothing left to sell for the cure: farm for it (owner decision,
        2026-11-23), persisted so it outlives Stop and restarts."""
        s = self.services
        cost = s._depletion_service_cost(client)
        wallet = BuyShopUpgradeTask.wallet_value(client)
        detail = (f"remove depletion costs {format_money(cost)}, she has "
                  f"{format_money(wallet)} and nothing left to sell")
        start_cure_funding_farm(client, detail)
        self._resume(client, "farming to fund the cure")
