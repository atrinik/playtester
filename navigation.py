"""Offline map graph, landmark index, and live route following."""

from __future__ import annotations

import ast
import json
import logging
import math
import multiprocessing
import os
import posixpath
import dataclasses
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
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
                    BuyShopUpgradeTask,
                    DepositItemsTask, FarmTask, InventoryPolicy, JunkPolicy,
                    InventoryCapabilityTask, MassIdentifyTask, SafetyPolicy, safe_food,
                    RANGED_RESIDENT_MARKERS, RANGED_RESIDENT_RADIUS,
                    RetrieveItemsTask, SellJunkTask, TaskStatus,
                    TempleFoodTask, TempleServiceTask,
                    corpse_tile_standable, movement_ack_timeout,
                    healing_outpaced, recent_hostile_attackers,
                    recent_hostile_contact,
                    retreat_mobility, unresolved_hostile_contact,
                    combat_allowed, wand_known_empty)
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
        self.map_roaming_monster_levels: dict[
            str, dict[str, int]] = defaultdict(dict)
        self._component_cache: dict[
            tuple[str, bool, tuple[int, int]], frozenset[tuple[int, int]]
        ] = {}
        self._aggro_pack_cache: dict[tuple[str, int, bool], int] = {}
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
        if plan.outcome == route_costs.NO_ROUTE:
            # Components are derived data; retry once with a fresh cache.
            _clear_components(self.graph)
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
        except ValueError:
            # Components are derived data; retry once with a fresh cache.
            _clear_components(self.graph)
            try:
                try:
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

        def relaxed(avoided: set[str]) -> dict:
            # A hazard a fallback tier stops avoiding is still a known threat:
            # it joins the penalized maps, so the route enters as few known
            # threats as it can, learned or relaxed alike, never trading one
            # learned map for several relaxed ones.
            return {**kwargs, "avoided_maps": avoided,
                    "penalized_maps": kwargs["penalized_maps"] | (kwargs["avoided_maps"] - avoided)}

        try:
            route = self._route_points(*args, unwalled_retry=False, **kwargs)
        except ValueError:
            # Components are derived acceleration data. A live exceptional
            # start square (closed door, runtime map mutation, occupied seam)
            # must never make a later route permanently unroutable.
            _clear_components(self.graph)
            try:
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
                    self._threat_fallback = True
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
                    self._threat_fallback = True
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
            try:
                self.route = self._plan(client)
            except ValueError as exc:
                self.fail(str(exc))
                return
            if not self.route:
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
            try:
                self.route = self._plan(client)
            except ValueError as exc:
                self.fail(str(exc))
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
    # Off the destination map, fight back only once something has been
    # hitting her and the route has made no progress for this long.
    TRANSIT_CORNERED_SECONDS = 4.0
    # A held-fire halo that alone leaves no route yields after this long.
    HALO_YIELD_SECONDS = 5.0

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
        self._retreat_blocked: set[tuple[str, int, int]] = set()
        self._trail: list[tuple[str, int, int]] = []
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
        self._recovering_after_combat = False
        self._transit_stall: tuple[
            int, str, int, int, int, int, int, float] | None = None

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

    async def _retreat_step(self, client: AtrinikClient, threats,
                            *, avoid_backtrack: bool = False) -> bool:
        """Take one authored-walkable step away from the visible pack."""
        m = client.state.map
        now = time.monotonic()
        if self._retreat_attempt is not None:
            path, x, y, sent_at = self._retreat_attempt
            if (m.path, m.world_x, m.world_y) == (path, x, y):
                self._retreat_attempt = None
            elif now - sent_at >= movement_ack_timeout(client):
                # Learn from a direct move which made no progress; static map
                # analysis cannot see every runtime wall or blocking object.
                self._retreat_blocked.add((path, x, y))
                self._retreat_attempt = None

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
            if minimum < current_min:
                continue
            backtrack = int(previous == (m.path, *point))
            trail_score = -backtrack if avoid_backtrack else backtrack
            escape_depth, escape_area = retreat_mobility(
                node, point, blocked=mobility_blocked,
                allow_locked=self.navigation._allow_locked(client))
            # Continuing escape mobility outranks immediate separation: a
            # farther tile in a dead end is not safer than an open flank. For a
            # dangerous single monster, an equal-distance step must not reverse
            # immediately or a two-tile plateau becomes an endless oscillation.
            candidates.append(
                (int(escape_depth >= 2), minimum, escape_depth, escape_area,
                 trail_score, sum(distances), direction))
        if not candidates:
            return False
        (viable, minimum, escape_depth, escape_area, _, _,
         direction) = max(candidates)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-retreat-choice",
                f"direction={direction} separation={minimum} "
                f"escape_depth={escape_depth} escape_area={escape_area} "
                f"continuation={viable}")
        await client.clear_actions()
        # run=True is a persistent directional run mode in the protocol, not
        # a faster single step. Leaving it enabled caused the character to
        # continue into walls after combat had ended.
        await client.move(direction)
        dx, dy = c.DIRECTION_DELTAS[direction]
        self._retreat_attempt = (
            m.path, m.world_x + dx, m.world_y + dy, now)
        self.navigation._issued_goal = None
        self.navigation._issued_click = None
        self.navigation._last_progress = time.monotonic()
        self._last_defense_action = time.monotonic()
        return True

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
        if self._retreat_attempt is not None:
            path, x, y, sent_at = self._retreat_attempt
            if (m.path, m.world_x, m.world_y) == (path, x, y):
                self._retreat_attempt = None
            elif now - sent_at < movement_ack_timeout(client):
                return True
            else:
                self._retreat_blocked.add((path, x, y))
                self._retreat_attempt = None
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
        self._retreat_attempt = (path, x, y, now)
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

    async def _defend_while_navigating(self, client: AtrinikClient) -> bool:
        """Pause an encounter approach to heal or clear an aggroed pack."""
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
        pull_pack = [threat for threat in threats if threat[0] <= 6]
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
        overwhelmed = len(nearby) >= 2
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
        last_heal = self.safety._last_heal
        last_food = self.safety._last_food
        safe = await self.safety.enforce(client)
        safety_action_issued = (
            self.safety._last_heal != last_heal or
            self.safety._last_food != last_food)
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
            pull_pack = [threat for threat in manageable if threat[0] <= 6]
            overwhelmed = len(nearby) >= 2
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
        pack_offensive = (self.task.primary_combat_spell(client)
                          if isinstance(self.task, FarmTask) else None)
        if (safe and nearest is not None and nearest[0] > 2 and
                len(pull_pack) >= 2 and (pack_offensive is None or hold_fire)):
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
        if ((not safe and not starving_without_food(client)) or
                (overwhelmed and not hold_fire)):
            if safety_action_issued:
                # FIRE/APPLY was just sent by SafetyPolicy. Clearing in this
                # same tick cancels it and creates an endless heal/food retry
                # loop at a fixed coordinate.
                return True
            self._defending = True
            await client.clear_actions()
            await client.set_combat(False)
            if nearby and time.monotonic() - self._last_defense_action >= 0.30:
                # A pack-splitting step must not immediately reverse on an
                # equal-distance plateau. Route following and the next safety
                # tick otherwise alternate forever between the same squares.
                await self._retreat_step(
                    client, nearby, avoid_backtrack=True)
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
        if nearest is not None and nearest[0] <= 4:
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
        if await self._defend_while_navigating(client):
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


# From this level recall shopping buys a recall source when she can afford
# one (FarmCircuitTask._needs_recall_shopping); below it nothing would.
RECALL_REQUIRED_LEVEL = 18
# Where recall shopping keeps the latest recall quote she was given
# (client.state.vendor_quotes): the cheapest one the merchant offered.
RECALL_QUOTE_KEY = "recall-source"


def _recall_sources(client: AtrinikClient) -> tuple[bool, bool]:
    """Whether she owns a recall source, and whether one of them works now.

    A source is what FarmTask.emergency_recall_candidates fires, read
    without its mana check: a known Word of Recall spell, or a paid,
    identified, uncursed rod or wand. It works unless it is a wand known to
    be empty (observe_wand_feedback) or a device waiting out
    RECALL_DEVICE_RETRY_SECONDS after it said it cannot fire. A wand of
    unknown charge works until its examine text says otherwise.
    """
    retry = getattr(client, "recall_device_retry_at", {}) or {}
    now = time.monotonic()
    policy = InventoryPolicy()
    owned = False
    for item in getattr(client.state, "inventory", ()):
        if (not re.search(r"\bword of recall\b", item.name, re.I) or
                item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)):
            continue
        if item.item_type == c.TYPE_SPELL:
            return True, True
        if (item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and
                policy.identified(item)):
            owned = True
            if (not wand_known_empty(client, item) and
                    float(retry.get(item.tag, 0.0)) <= now):
                return True, True
    return owned, False


def emergency_recall_unusable(client: AtrinikClient) -> bool:
    """Whether she owns a recall source but none of them works now
    (_recall_sources). Owning none at all is not this."""
    owned, working = _recall_sources(client)
    return owned and not working


def has_working_recall(client: AtrinikClient) -> bool:
    """Whether a recall source of hers works now (_recall_sources)."""
    return _recall_sources(client)[1]


def recall_quote(client: AtrinikClient) -> tuple[str, int] | None:
    """The recall source recall shopping was last quoted, and its price:
    the cheapest stock the merchant offered (RECALL_QUOTE_KEY)."""
    quote = (getattr(client.state, "vendor_quotes", {}) or {}).get(
        RECALL_QUOTE_KEY) or {}
    cost = quote.get("unit_cost")
    if not isinstance(cost, (int, float)) or cost <= 0:
        return None
    return str(quote.get("item", "") or "recall source"), int(cost)


def recall_alerts(client: AtrinikClient) -> list[dict[str, str]]:
    """The dashboard's recall note: from RECALL_REQUIRED_LEVEL, while no
    recall source of hers works.

    A working emergency recall is never a blocker (owner decision,
    2026-11-22): she farms on without one, and recall shopping buys one
    once she can afford it. So none of these is critical:

    - recall-unfunded (warning): the recall she was last quoted, plus the
      reserve recall shopping keeps, costs more than she has;
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
    """
    state = client.state
    if depleted_for_temple(state):
        return (f"depletion {int(state.depletion_points)} at or above "
                f"{FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD}")
    return ""


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


def kit_hazardous_spot(client: AtrinikClient, spot: dict | None,
                       legs) -> bool:
    """Whether live failures rate this spot hazardous for her current kit.

    See self_improvement.kit_hazard: repeated pack or out-damaged failures,
    or deaths on the way, at any spot sharing a map with this one, with a
    kit she has not improved on since (CombatProfile.no_survivability_gain:
    a weaker kit still counts them).
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
        (path for path, _ in legs), same_kit)


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
        search_verdict: bool | None = None
) -> tuple[str, tuple[tuple[str, str], ...], dict] | None:
    """The objective farmed now, with its highest-ranked usable spot
    (_analyzer_farm_choice). With ``search`` (autoplay's own call, not the
    dashboard's or the metrics'), the farm score's shadow (ROUTING P6,
    route_shadow.py) is offered this choice to compare against; that never
    changes the choice or raises."""
    choice = _analyzer_farm_choice(client, graph, search=search,
                                   search_verdict=search_verdict)
    if search:
        route_shadow.request_farm(client, graph, choice)
    return choice


def _analyzer_farm_choice(
        client: AtrinikClient, graph: "WorldGraph", *, search: bool = True,
        search_verdict: bool | None = None
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
    While she is depleted that far, autoplay does not farm at all
    (farming_unsafe_reason, RecoveryTask): it removes depletion first, funds
    the fee from what she owns, and otherwise waits for money rather than
    farm for it, so this pick matters only to a circuit started by hand.
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
    for position, objective in enumerate(dict.fromkeys(
            (effective_farm_objective(client.state), chosen))):
        for rank, spot in enumerate(analyzer.ranking(objective)):
            legs = analyzer_spot_legs(spot, graph)
            if (legs and all(float(quarantine.get(path, 0.0) or 0.0) <= now
                             for path, _ in legs) and
                    not any(path in blocked for path, _ in legs)):
                entry = (objective, legs, spot)
                order[id(entry)] = (position, rank)
                (hazardous if kit_hazardous_spot(client, spot, legs)
                 else usable).append(entry)
    if not usable and hazardous:
        # Every usable spot is hazardous for her kit: farm the least
        # dangerous one rather than idle until the hazard ages out, the
        # one with the safer approach on a tie.
        usable = [min(hazardous, key=lambda entry: (
            float(entry[2].get("danger", 0.0) or 0.0),
            -plan.approach_safety(entry[2]), order[id(entry)]))]

    def preference(entry) -> tuple:
        # Within each objective's ranking (the effective one first, so the
        # depletion safety rule holds): spots with a known, safe approach
        # from her savebed in rank order, then the risky ones from the
        # safest down by the analyzer's safety_score (the lower of spot and
        # approach safety), or by approach safety (unreachable counts as
        # UNREACHABLE_APPROACH_SAFETY) without one, in rank order on ties.
        position, rank = order[id(entry)]
        spot = entry[2]
        risky = plan.risky_approach(spot)
        overall = spot.get("safety_score")
        safety = (float(overall) if isinstance(overall, (int, float)) and
                  not isinstance(overall, bool) else plan.approach_safety(spot))
        return (position, risky, -safety if risky else 0.0, rank)

    usable.sort(key=preference)
    if not depleted_for_temple(client.state):
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
        for rank, spot in enumerate(analyzer.ranking(objective)[:SHADOW_FARM_CANDIDATES]):
            legs = analyzer_spot_legs(spot, graph)
            if not legs or any(float(quarantine.get(path, 0.0) or 0.0) > now
                               for path, _ in legs):
                continue
            candidate = route_costs.FarmCandidate(
                farm_spot_key(objective, spot), str(spot.get("name") or ""), objective,
                position, rank, route_costs.spot_rate(spot, objective))
            entry = (candidate, legs, spot)
            (hazardous if kit_hazardous_spot(client, spot, legs) else usable).append(entry)
    if not usable and hazardous:
        usable = [min(hazardous, key=lambda entry: (
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


def temple_food_trip(graph: "WorldGraph", client: AtrinikClient, *,
                     exclude: Iterable[str] = ()) -> NavigateThenTask | None:
    """Route to the nearest priest reachable without a threat fallback.

    Priests named in ``exclude`` (whose trip just failed) are passed over.
    """
    ranked = []
    origin = apartment_exit_origin(client)
    excluded = {name.casefold() for name in exclude}
    for priest in graph.temple_priests:
        if priest.name.casefold() in excluded:
            continue
        navigation = NavigateTask(
            graph, priest.map_path, (priest.x, priest.y), tolerance=2)
        navigation.essential = "food"
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
    priest = min(ranked, key=lambda entry: entry[:-1])[-1]
    task = NavigateThenTask(
        graph, priest.map_path, TempleFoodTask(priest.name),
        destination_xy=(priest.x, priest.y), combat_approach=True)
    task.navigation.tolerance = 2
    task.navigation.essential = "food"
    return task


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
        "_cure_retry_at", "_restoration_retry_at",
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
        # road. Maintenance children are held separately and never reach this.
        if (not path and self.child is not None and
                self.child.navigation.destination in active_paths and
                self.child.navigation.status != TaskStatus.COMPLETE):
            path = self.child.navigation.destination
        if not path:
            return False
        what = "combat death" if failure.get("action") == "death" else "emergency recall"
        if self._analyzer_safety:
            # Analyzer spots are kept: review what happened and adapt.
            return self._learn_from_failure(client, failure, path, what)
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

    def _learn_from_failure(self, client: AtrinikClient, failure: dict,
                            path: str, what: str) -> bool:
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
        review = self_improvement.review_failure(
            messages, this_life, failure, [leg for leg, _ in self.legs])
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
            maps=[leg for leg, _ in self.legs], map_damage=on_map)
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
            return True
        if kit_hazardous_spot(client, self.spot, self.legs):
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
            search_verdict=not busy or overdue)
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

    def _food_trip(self, client: AtrinikClient) -> tuple[NavigateThenTask | None, str]:
        """The food trip to start now: a temple priest, the shop, or none.

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
            return self._new_food_resupply(client), "shop"
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

    def _new_temple_food(self, client: AtrinikClient) -> NavigateThenTask | None:
        return temple_food_trip(self.graph, client,
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
            self._utility_shopping,
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
    # purchases, recall shopping (never bought with the cure's money) and
    # savebed rebinding. Every other trip is an
    # essential (food, bank, identify, storage, the cure and its funding,
    # the ship key) and finishes first.
    DROPPED_AT_HANDOVER = ("_shopping", "_spell_purchase", "_utility_shopping",
                           "_recall_shopping", "_bed_binding")

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
        observed = float(((getattr(client.state, "vendor_quotes", {}) or {})
                          .get(RECALL_QUOTE_KEY) or {}).get("observed_at", 0.0)
                         or 0.0)
        if quote is None or time.time() - observed >= self.RECALL_SHOP_INTERVAL:
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
                self._banking = None
            elif self._banking.status == TaskStatus.COMPLETE:
                log.info("all carried cash banked; returning to farm")
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
                self._selling = None
            elif self._selling.status == TaskStatus.COMPLETE:
                log.info("identified junk sale complete; returning to farm")
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
                self._storage = None
            elif self._storage.status == TaskStatus.COMPLETE:
                log.info("apartment loot storage complete; returning to farm")
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
        # carrying pressure; discretionary shopping is always last.
        if (isinstance(farm, FarmTask) and self._needs_food_resupply(client) and
                (self._food_urgent(client) or
                 not self._busy_finishing_fight_or_loot(client, farm))):
            resupply, kind = self._food_trip(client)
            if resupply is None and kind == "temple":
                self._temple_food_retry_at = (
                    self.clock() + self.TEMPLE_FOOD_RETRY_SECONDS)
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
        if (isinstance(farm, FarmTask) and
                self._needs_mass_identification(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._identification = self._new_mass_identification()
            await self._identification.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_apartment_storage(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._storage = self._new_apartment_storage(
                keep_unknown=self._cure_identify_due(client))
            await self._storage.tick(client)
            return
        if (isinstance(farm, FarmTask) and
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
        if (isinstance(farm, FarmTask) and
                self._needs_junk_sale(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._selling = self._new_junk_sale(client)
            await self._selling.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_bank_deposit(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._banking = self._new_bank_deposit(client)
            await self._banking.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_ship_key(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._ship_key = MorgeeanShipKeyTask(self.graph)
            await self._ship_key.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_spell_purchase(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._spell_purchase = self._new_spell_purchase()
            await self._spell_purchase.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_recall_shopping(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._recall_shopping = self._new_recall_shopping(client)
            await self._recall_shopping.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_utility_shopping(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._utility_shopping = self._new_utility_shopping(client)
            await self._utility_shopping.tick(client)
            return
        if (isinstance(farm, FarmTask) and
                self._needs_upgrade_shopping(client) and
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
                last_heal = self.child.safety._last_heal
                await self.child.safety.enforce(client)
                if self.child.safety._last_heal != last_heal:
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
                not farm.training_rest_active()):
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
      (recovery-home-unsafe-route). Every errand starts from her savebed;
    - depleted: the temple cure when she can pay (a depletion potion
      first), otherwise the cure-funding path: identify-all, junk sale,
      deposit, the apartment chest. No recall is bought with the cure's
      money: recall shopping is the farm circuit's, never recovery's
      (owner decision, 2026-11-22);
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
        s = self.services
        wallet = BuyShopUpgradeTask.wallet_value(client)
        assist = "; the owner can give her gold with the operator assist"
        if depleted_for_temple(client.state):
            if s._cure_shortfall(client):
                if self._funding_running():
                    return []
                cost = s._depletion_service_cost(client)
                return [{"id": "cure-unfunded", "level": "critical",
                         "text": "Cure unfunded: remove depletion costs "
                                 f"{format_money(cost)}, "
                                 f"she has {format_money(wallet)} and nothing "
                                 "left to sell" + assist}]
            failures, error = self.cure_route_refusals(client)
            if failures >= self.CURE_ROUTE_ATTEMPTS:
                return [{"id": "cure-unreachable", "level": "critical",
                         "text": "Cure unreachable: no strictly safe route to "
                                 f"the temple from her savebed after {failures} "
                                 f"attempts: {error}"}]
        return []

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

    def _new_home_walk(self, home: tuple[str, tuple[int, int]], *,
                       strict: bool) -> NavigateThenTask:
        walk = NavigateThenTask(self.graph, home[0], _ArriveTask("savebed"),
                                destination_xy=home[1], combat_approach=True)
        walk.navigation.tolerance = 1
        walk.navigation.strict_safety = strict
        walk.navigation.essential = "home"
        return walk

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
            walk = self._home_walk = self._new_home_walk(home, strict=False)
            await walk.tick(client)
        if walk.status == TaskStatus.FAILED:
            log.warning("recovery: walk to her savebed failed: %s", walk.error)
            self._home_retry_at = self.clock() + self.HOME_RETRY_SECONDS
            self._home_walk = None
        elif walk.status == TaskStatus.COMPLETE:
            self._home_walk = None

    async def _defend(self, client: AtrinikClient) -> None:
        """Hit with nothing under way: heal, flee or fight back here."""
        hp = int(client.state.stats.get("hp", 0) or 0)
        maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
        last_heal = self.safety._last_heal
        await self.safety.enforce(client)
        if self.safety._last_heal != last_heal:
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
            log.info("farming resumed (was suspended: %s)", self.reason)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("farming-resumed", f"was suspended: {self.reason}")
            self.complete()
            return
        self.reason = reason
        home = self._home(client)
        if (home is not None and not self.at_home(client) and
                self.clock() >= self._home_retry_at):
            await client.clear_actions()
            await client.set_combat(False)
            self._home_walk = self._new_home_walk(home, strict=True)
            await self._tick_home_walk(client)
            return
        # Every errand starts from her savebed; off it, she waits for the
        # next walk home.
        if self.at_home(client):
            await self._start_errand(client, reason)
