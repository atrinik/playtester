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
from collections.abc import Callable, Iterable, Sequence

try:
    from tools.world_content_audit import (ROOT as CONTENT_ROOT, flatten,
                                           load_archetypes, map_files,
                                           parse_blocks)
except ModuleNotFoundError:
    from world_content_audit import (ROOT as CONTENT_ROOT, flatten,
                                     load_archetypes, map_files, parse_blocks)

from . import constants as c
from .targeting import select_target, target_set_aside

from .client import AtrinikClient, map_object_visual_name
from .farm_analyzer import DEFAULT_OBJECTIVE
from . import self_improvement
from .model import STARTER_APARTMENT
from .pathfinding import graph_search, grid_reachable, grid_search
from .tasks import (BankBalanceTask, BankTask, BindSavebedTask, BotTask,
                    BuyDialogueStockTask, BuyGroundItemsTask,
                    BuyShopUpgradeTask,
                    DepositItemsTask, FarmTask, InventoryPolicy, JunkPolicy,
                    InventoryCapabilityTask, MassIdentifyTask, SafetyPolicy, safe_food,
                    SellJunkTask, TaskStatus,
                    TempleFoodTask, TempleServiceTask,
                    corpse_tile_standable, movement_ack_timeout,
                    recent_hostile_attackers,
                    recent_hostile_contact,
                    retreat_mobility)
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
    locked: set[tuple[int, int]] = field(default_factory=set)
    lock_requirements: dict[tuple[int, int], str] = field(default_factory=dict)
    lock_exclusions: dict[tuple[int, int], str] = field(default_factory=dict)
    doors: set[tuple[int, int]] = field(default_factory=set)
    occupied: set[tuple[int, int]] = field(default_factory=set)
    automatic_exits: set[tuple[int, int]] = field(default_factory=set)
    peaceful_identities: set[str] = field(default_factory=set)
    caster_identities: set[str] = field(default_factory=set)

    def lock_allowed(self, point: tuple[int, int], access=False) -> bool:
        """Check one locked tile against an exact authored requirement."""
        if point not in self.locked:
            return True
        if access is True:
            return True
        if not access:
            return False
        requirement = self.lock_requirements.get(point, "")
        if requirement and requirement not in access:
            return False
        exclusion = self.lock_exclusions.get(point, "")
        if exclusion and exclusion in access:
            return False
        return bool(requirement or exclusion)

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
            parsed_maps[key] = (path, parsed)

        auto_exits: dict[str, list[tuple[int, int, int, str, bool]]] = defaultdict(list)
        apartment_entrances: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        apartment_returns: dict[str, list[tuple[str, int, int, str, bool]]] = defaultdict(list)
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
                    node.locked.add((x, y))
                    if door_lock_id:
                        node.lock_requirements[(x, y)] = door_lock_id
                if (obj_type == "64" and lock_id and
                        one(attrs, "last_grace",
                            base.get("last_grace")) == "1"):
                    # Blocking inventory checkers are authoritative access
                    # gates even when no locked door occupies their tile.
                    node.locked.add((x, y))
                    if one(attrs, "sp", base.get("sp")) == "0":
                        node.lock_exclusions[(x, y)] = lock_id
                    else:
                        node.lock_requirements[(x, y)] = lock_id
                if (lock_id and name and
                        ("key" in obj["arch"].casefold() or
                         "key" in name.casefold())):
                    self.access_item_names[lock_id].add(name.casefold())
                    if lock_id == "set_individual_value":
                        # These keys receive their effective slaying value from
                        # the server-side pickup script; their unique authored
                        # name is the checker requirement visible in the maps.
                        self.access_item_names[name.casefold()].add(
                            name.casefold())
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

    def _component(self, map_path: str, start: tuple[int, int], *,
                   allow_locked: bool = False) -> frozenset[tuple[int, int]]:
        """Return the complete static walkable component containing start."""
        node = self.nodes.get(map_path)
        cache_key = map_path, allow_locked, start
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

    def route_points(self, source: str, source_xy: tuple[int, int],
                     destination: str,
                     destination_points: list[tuple[int, int]], *,
                     allow_locked: bool = False,
                     excluded: set[tuple[str, str]] | None = None,
                     avoided_maps: set[str] | None = None,
                     penalized_maps: set[str] | None = None,
                     ) -> list[MapEdge]:
        """Route end-to-end between exact walkable components across maps.

        Entering a ``penalized_maps`` map costs more than any route without
        one, so the route crosses as few of them as it can and, among those,
        is the shortest.
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
        goals: list[int] = []
        cursor = 0
        while cursor < len(states):
            state = states[cursor]
            map_path, _ = state
            position = positions[cursor]
            if map_path == destination and (
                    not destination_points or any(
                        self.local_path(map_path, position, point,
                                        allow_locked=allow_locked)
                        for point in destination_points)):
                goals.append(cursor)
            node = self.nodes.get(map_path)
            if node is not None:
                for edge in node.edges:
                    if excluded and (edge.source, edge.destination) in excluded:
                        continue
                    if (avoided_maps and edge.destination in avoided_maps and
                            edge.destination != destination):
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
                        edge_costs.append(edge.destination in (penalized_maps or ()))
                        route_edges.append(adjusted)
            offsets.append(len(targets))
            cursor += 1
        if not goals:
            raise ValueError(
                f"no component route from {source} {source_xy} to "
                f"{destination} {destination_points}")
        # A simple route has fewer transitions than there are states, so one
        # penalized entry outweighs any detour: crossings first, then length.
        penalty = len(states) + 1
        result = graph_search(
            offsets, targets, 0, goals, metadata=edge_metadata,
            costs=[1 + penalty * flagged for flagged in edge_costs] if penalized_maps else None)
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
                              excluded: set[tuple[str, str]] | None = None
                              ) -> list[list[MapEdge]]:
        """Find routes which enter the destination component containing points."""
        routes: list[list[MapEdge]] = []
        for node in self.nodes.values():
            for edge in node.edges:
                if edge.destination != destination:
                    continue
                if excluded and (edge.source, edge.destination) in excluded:
                    continue
                arrivals = self._entry_points(edge)
                if not any(self.local_path(destination, arrival, point)
                           for arrival in arrivals for point in points):
                    continue
                try:
                    prefix = self.route(source, edge.source,
                                        excluded=excluded)
                except ValueError:
                    continue
                route = prefix + [edge]
                if not route:
                    continue
                if (source_point is not None and route[0].source == source and
                        not self.local_path(
                            source, source_point,
                            (route[0].x, route[0].y))):
                    continue
                routes.append(route)
        routes.sort(key=len)
        return routes

    def route(self, source: str, destination: str, *,
              excluded: set[tuple[str, str]] | None = None,
              avoided_maps: set[str] | None = None) -> list[MapEdge]:
        if source == destination:
            return []
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
        paths, state_ids, offsets, targets, route_edges = (
            self._world_search_snapshot)
        if source not in state_ids or destination not in state_ids:
            raise ValueError(f"no route from {source} to {destination}")
        excluded_edges = (
            index for index, edge in enumerate(route_edges)
            if ((excluded and (edge.source, edge.destination) in excluded) or
                (avoided_maps and edge.destination in avoided_maps and
                 edge.destination != destination) or
                ("/apartments/apartment_" in edge.destination and
                 edge.destination != destination))
        )
        result = graph_search(
            offsets, targets, state_ids[source], (state_ids[destination],),
            metadata=range(len(route_edges)), excluded_edges=excluded_edges,
        )
        if result.status != "found":
            raise ValueError(f"no route from {source} to {destination}")
        return [route_edges[index] for index in result.transitions]


class NavigateTask(BotTask):
    """Follow the authored path one verified protocol move at a time."""

    STALL_RETRY_SECONDS = 0.65
    # Learned stall blockers are guesses. Once they leave no route at all and
    # the character has not moved for this long, they are the trap.
    LEARNED_BLOCK_RESET_SECONDS = 5.0
    ACCESS_ITEM = re.compile(r"\b(key|talisman|amulet|pass|token)\b", re.I)
    EARLY_ROUTE_HAZARD = re.compile(
        r"(?:rock thrower|battle thrower|skel(?:eton)? mage|"
        r"skeleton (?:novice )?archer|lost soul)", re.I)
    KNOWN_LETHAL_TRANSIT = frozenset({
        "/shattered_islands/world_4_69",
    })

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
        self._excluded_edges: set[tuple[str, str]] = set()
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
        self._failed_tile_crossings: set[tuple[str, str, int, int]] = set()

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
        if self._allow_locked_override is not None:
            return self._allow_locked_override
        inventory = getattr(client.state, "inventory", ())
        names = {item.name.casefold().strip() for item in inventory}
        return frozenset(
            requirement for requirement, aliases
            in getattr(self.graph, "access_item_names", {}).items()
            if names & aliases)

    def _goal_points(self) -> list[tuple[int, int]]:
        if self.destination_xy is None:
            return []
        if self.tolerance and self._approach:
            return self._approach
        return [self.destination_xy]

    def _known_ranged_hazard_maps(self, client: AtrinikClient) -> set[str]:
        """Avoid authored ranged/caster transit maps while Sera is fragile."""
        stats = getattr(client.state, "stats", {})
        if int(stats.get("level", 0) or 0) >= 18:
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

    def _plan(self, client: AtrinikClient) -> list[MapEdge]:
        # Learned hazards are a cost, never a wall: one search finds the route
        # that enters as few of them as it can. They never make a trip
        # unroutable or change which other hazards are relaxed, and leaving
        # the map she stands on is free while re-entering it is not.
        learned = self._learned_route_hazards(client)
        route = self._plan_avoiding(client, learned)
        if any(edge.destination in learned for edge in route):
            # Entering a map that killed her, the destination included, is
            # never "threat-free" when choosing between optional destinations.
            self._threat_fallback = True
        return route

    def _plan_avoiding(self, client: AtrinikClient,
                       penalized: set[str]) -> list[MapEdge]:
        source = client.state.map.path
        args = (
            source,
            (client.state.map.world_x, client.state.map.world_y),
            self.destination,
            self._goal_points(),
        )
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
            route = self.graph.route_points(*args, **kwargs)
        except ValueError:
            # Components are derived acceleration data. A live exceptional
            # start square (closed door, runtime map mutation, occupied seam)
            # must never make a later route permanently unroutable.
            self.graph._component_cache.clear()
            try:
                route = self.graph.route_points(*args, **kwargs)
            except ValueError as preferred_exc:
                # Tiled levels only joined the hazards to keep surface trips
                # off dense tunnels. A trip that needs them (one inside the
                # tunnels, or with no surface way round) routes through them
                # as before, still avoiding every other hazard.
                tiled = {path for path in pack_hazards
                         if self.TILED_LEVEL_MAP.fullmatch(path)}
                route = None
                if tiled:
                    # Only this retry lifts them; later tiers relax other
                    # hazards first and keep avoiding dense tunnels.
                    try:
                        route = self.graph.route_points(*args, **relaxed(kwargs["avoided_maps"] - tiled))
                    except ValueError:
                        route = None
                if route is None and overlevel_hazards:
                    fallback_kwargs = relaxed(protected_hazards)
                    try:
                        route = self.graph.route_points(
                            *args, **fallback_kwargs)
                    except ValueError:
                        if not self.allow_ranged_hazard_fallback:
                            excluded = sorted(self._excluded_edges)
                            raise ValueError(
                                f"{preferred_exc}; excluded edges: "
                                f"{excluded}") from preferred_exc
                        fallback_kwargs = relaxed(set(self.KNOWN_LETHAL_TRANSIT))
                        route = self.graph.route_points(
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
                    route = self.graph.route_points(
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
            if (self._last_position is not None and
                position[0] != self._last_position[0]):
                self._runtime_blocked.clear()
                self._threat_blocked.clear()
            self._last_position = position
            self._last_progress = time.monotonic()

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
                self._temporary_blocked)

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
                now - self._last_progress < self.STALL_RETRY_SECONDS):
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
                        excluded=self._threat_blocked | self._temporary_blocked)):
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
        paths = [
            path for point in candidates
            if point not in blocked and node.walkable(
                *point, allow_locked=self._allow_locked(client))
            if (path := self.graph.local_path(
                m.path, start, point,
                allow_locked=self._allow_locked(client), excluded=blocked))
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
        if (not at_border or
                time.monotonic() - self._last_progress < acknowledgement):
            return False
        self._failed_tile_crossings.add(
            (edge.source, edge.destination, edge.x, edge.y))
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
        for crossing, departure, arrival, orthogonal in crossings:
            key = (edge.source, edge.destination, *crossing)
            if (key in self._failed_tile_crossings or
                    not source.walkable(
                        *departure, allow_locked=self._allow_locked(client)) or
                    not destination.walkable(
                        *arrival, allow_locked=self._allow_locked(client)) or
                    arrival in destination.occupied):
                continue
            path = self.graph.local_path(
                edge.source, start, departure,
                allow_locked=self._allow_locked(client),
                excluded=self._runtime_blocked | self._threat_blocked)
            if path:
                candidates.append((
                    abs(orthogonal - authored_orthogonal),
                    len(path), crossing))
        if not candidates:
            self._excluded_edges.add((edge.source, edge.destination))
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
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "navigation-seam-retry",
                f"{failed} rejected; trying {crossing}")
        return True

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
                elif time.monotonic() - self._last_progress > 1.5:
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
            self.route.pop(0)
            self._issued_goal = None
            self._issued_click = None
        if not self.route or self.route[0].source != client.state.map.path:
            try:
                self.route = self._plan(client)
            except ValueError as exc:
                self.fail(str(exc))
                return
            if not self.route:
                return
        edge = self.route[0]
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
            self._excluded_edges.add((edge.source, edge.destination))
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
        if now - self._pack_exit_stall[1] < self.PACK_EXIT_BYPASS_SECONDS:
            return False
        self.navigation._excluded_edges.add((edge.source, edge.destination))
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
                f"{edge.source} -> {edge.destination} at={edge.x},{edge.y}")
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
        if not self.combat_approach:
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
        hold_fire = (
            m.path != self.navigation.destination and
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
        if (ratio > self.safety.flee_below and nearest is not None and
                not hold_fire and isinstance(self.task, FarmTask) and
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
        if not safe or (overwhelmed and not hold_fire):
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
            self._halo_threats(client, nearby, radius=1)
            if getattr(client.state, "combat", False) and not combat_released:
                await client.set_combat(False)
            m = client.state.map
            cx, cy = m.width // 2, m.height // 2
            signature = tuple(sorted(
                (threat[3].target_id, m.world_x + threat[1] - cx,
                 m.world_y + threat[2] - cy) for threat in nearby))
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
        if self._defending:
            maxsp = int(client.state.stats.get("maxsp", 0) or 0)
            sp = int(client.state.stats.get("sp", 0) or 0)
            recovered = (
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
                 start_index: int = 0):
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

    def _new_child(self) -> NavigateThenTask:
        path, point = self.waypoints[self.index]
        child = NavigateThenTask(
            self.graph, path, BuyShopUpgradeTask(
                allow_launchers=self.allow_launchers),
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
                        setter(balance)
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


def effective_farm_objective(state) -> str:
    """The objective farming follows right now.

    While depletion is at the temple-service threshold, its lowered stats
    make the chosen objective's spots deadly, so farming prefers the safest
    spots until the temple removes it; then the choice applies again.
    """
    chosen = getattr(state, "farm_objective", DEFAULT_OBJECTIVE)
    # The counter can outlive a cure applied elsewhere (an item, or while
    # offline); only the live depletion force proves she is still depleted.
    depleted = any(item.item_type == c.TYPE_FORCE and
                   item.name.casefold() == "depletion"
                   for item in getattr(state, "inventory", ()))
    if (depleted and getattr(state, "depletion_points_known", False) and
            int(getattr(state, "depletion_points", 0) or 0) >=
            FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD):
        return "safety"
    return chosen


def analyzer_farm_choice(
        client: AtrinikClient, graph: "WorldGraph"
) -> tuple[str, tuple[tuple[str, str], ...], dict] | None:
    """The objective farmed now, with its highest-ranked usable spot.

    The farming analyzer is the source of truth. The only exceptions are live
    failures: a spot quarantined after a death, an emergency recall, ten minutes
    without XP, or an unreachable route is skipped until its quarantine ends.

    If depletion prefers safety but no safety spot is usable, the chosen
    objective's spot applies. The circuit removes depletion before any farming
    whenever an item or the temple fee allows, and farming is what pays that
    fee; holding instead could leave her depleted for good.
    """
    analyzer = getattr(client, "farm_analyzer", None)
    if analyzer is None:
        return None
    chosen = getattr(client.state, "farm_objective", DEFAULT_OBJECTIVE)
    quarantine = getattr(client.state, "farm_zone_quarantine", {}) or {}
    now = time.time()
    for objective in dict.fromkeys(
            (effective_farm_objective(client.state), chosen)):
        for spot in analyzer.ranking(objective):
            legs = analyzer_spot_legs(spot, graph)
            if legs and all(float(quarantine.get(path, 0.0) or 0.0) <= now
                            for path, _ in legs):
                return objective, legs, spot
    return None


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


def hungry(client: AtrinikClient) -> bool:
    """Low on food with nothing left to eat: she must go and get some."""
    return (food_reserve(client) <= 1 and
            int(client.state.stats.get("food", 1000) or 0) < 180)


def temple_food_trip(graph: "WorldGraph",
                     client: AtrinikClient) -> NavigateThenTask | None:
    """Route to the nearest priest reachable without a threat fallback."""
    ranked = []
    for priest in graph.temple_priests:
        navigation = NavigateTask(
            graph, priest.map_path, (priest.x, priest.y), tolerance=2)
        try:
            route = navigation._plan(client)
        except ValueError:
            continue
        if not navigation._threat_fallback:
            ranked.append((len(route), priest.name.casefold(), priest))
    if not ranked:
        return None
    priest = min(ranked, key=lambda entry: entry[:-1])[-1]
    task = NavigateThenTask(
        graph, priest.map_path, TempleFoodTask(priest.name),
        destination_xy=(priest.x, priest.y), combat_approach=True)
    task.navigation.tolerance = 2
    return task


# Paths of private savebeds: an apartment or house bed only works for its owner.
PRIVATE_SAVEBED_MARKERS = ("/apartments/", "/house/")
# Exact walks cost a tile-level route each (about 0.2 s on the live world), so
# only the bed maps within this many map transitions of the nearest bed, at
# most SAVEBED_CANDIDATES of them, get one.
SAVEBED_HOP_SLACK = 2
SAVEBED_CANDIDATES = 6


def _nearest_spot_maps(graph: WorldGraph, source: str,
                       spot_maps: Iterable[str]) -> tuple[int, list[str]] | None:
    """The fewest map transitions from ``source`` to a spot map, and those maps."""
    best: tuple[int, list[str]] | None = None
    for spot in spot_maps:
        if source == spot:
            return 0, [spot]
        try:
            hops = len(graph.route(source, spot))
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
                   edge: MapEdge) -> int:
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
    path = graph.local_path(map_path, here, target) if node is not None else []
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
             targets: list[tuple[int, int]]) -> bool:
    """Whether point shares a walkable component with a target (or none known)."""
    if not targets:
        return True
    component = graph._component(map_path, point)
    return any(target in component for target in targets)


def _route_walk(graph: WorldGraph, start_map: str, start_xy: tuple[int, int],
                edges: Sequence[MapEdge]) -> int:
    """Moves along a planned route until it enters its destination map."""
    tiles, here, current = 0, start_xy, start_map
    for edge in edges:
        tiles += _segment_steps(graph, current, here, edge)
        here, current = _edge_arrival(graph, edge), edge.destination
    return tiles


ApartmentReturns = dict[str, tuple[str, tuple[int, int]]]


def _bed_walk(graph: WorldGraph, bed_map: str, point: tuple[int, int],
              spot_maps: Sequence[str], returns: ApartmentReturns):
    """Generator: yields once per exact route; returns the walk options.

    Each option is (walk, first edge) for one route found, ``first`` being
    the edge the walk leaves ``bed_map`` by, so other beds in the same room
    can be priced by their own steps to each route's first edge. An apartment's exit
    returns to the per-player entrance recorded at runtime, not to the
    graph's static alternatives, so its walk continues from the observed
    return point in ``returns``; without one it is unknown (None).
    """
    targets = {spot: _spawn_points(graph, spot) for spot in spot_maps}
    if bed_map in spot_maps and _reaches(graph, bed_map, point, targets[bed_map]):
        return [(0, None)]
    start_map, start_xy, base, first = bed_map, point, 0, None
    if "/apartments/" in bed_map:
        back = returns.get(bed_map)
        node = graph.nodes.get(bed_map)
        if back is None or node is None:
            return []
        exits = [edge for edge in node.edges if edge.kind != "tile" and (
            graph.local_path(bed_map, point, (edge.x, edge.y)) or
            (edge.x, edge.y) == point)]
        if not exits:
            return []
        first = min(exits, key=lambda edge: _segment_steps(graph, bed_map, point, edge))
        base = _segment_steps(graph, bed_map, point, first)
        start_map, start_xy = back[0], tuple(back[1])
        if start_map in spot_maps and _reaches(graph, start_map, start_xy, targets[start_map]):
            return [(base, first)]
    options = []
    for spot in spot_maps:
        try:
            # Arrive in the part of the circuit map the farm's spawns are
            # in: a bed (or arrival) in a disconnected room does not count.
            edges = graph.route_points(start_map, start_xy, spot, targets[spot])
        except (ValueError, KeyError):
            edges = None
        yield
        if not edges:
            continue
        options.append((base + _route_walk(graph, start_map, start_xy, edges),
                        first or edges[0]))
    return options


def savebed_walk(graph: WorldGraph, bed_map: str, bed_xy: tuple[int, int],
                 spot_maps: Iterable[str], *,
                 returns: ApartmentReturns | None = None) -> int | None:
    """Moves walked from a respawn on this bed to the nearest circuit map.

    Every reachable circuit map is routed and walked along its authored
    paths; None when none is reachable (or an apartment's return is unknown).
    """
    steps = _bed_walk(graph, bed_map, tuple(bed_xy), tuple(spot_maps), returns or {})
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
    ``current`` (map, tile), ``current_walk`` is that bed's walk.
    """

    def __init__(self, graph: WorldGraph, spot_maps: Iterable[str], *,
                 owned: Iterable[str] = (STARTER_APARTMENT,),
                 excluded: Iterable[str] = (),
                 excluded_tiles: Iterable[tuple[str, tuple[int, int]]] = (),
                 current: tuple[str, tuple[int, int]] | None = None,
                 returns: ApartmentReturns | None = None):
        self.result: tuple[str, tuple[int, int], int] | None = None
        self.current_walk: int | None = None
        self.done = False
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
                           _nearest_spot_maps(graph, back[0], spot_maps))
                if nearest is not None:
                    nearest = (nearest[0] + 1, nearest[1])
            else:
                nearest = _nearest_spot_maps(graph, bed_map, spot_maps)
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
            for point in eligible[bed_map]:
                component = graph._component(bed_map, point)
                groups.setdefault(min(component) if component else point, []).append(point)
            for group in groups.values():
                anchor = group[0]
                options = yield from _bed_walk(
                    graph, bed_map, anchor, spot_maps, returns)
                for point in group:
                    # Each bed takes whichever leg's route suits it best: a
                    # bed beside another exit is priced through that exit.
                    prices = []
                    for walk, first in options:
                        if point == anchor or first is None:
                            prices.append(walk)
                        elif graph.local_path(bed_map, point, (first.x, first.y)):
                            prices.append(
                                walk - _segment_steps(graph, bed_map, anchor, first) +
                                _segment_steps(graph, bed_map, point, first))
                    if prices:
                        self._consider(bed_map, point, min(prices))
                        priced = True
            priced_maps += priced
        if current is not None and current[0] in graph.nodes:
            options = yield from _bed_walk(
                graph, current[0], tuple(current[1]), spot_maps, returns)
            self.current_walk = min((walk for walk, _ in options), default=None)


def nearest_savebed(graph: WorldGraph, spot_maps: Iterable[str], *,
                    owned: Iterable[str] = (STARTER_APARTMENT,),
                    excluded: Iterable[str] = (),
                    excluded_tiles: Iterable[tuple[str, tuple[int, int]]] = (),
                    returns: ApartmentReturns | None = None
                    ) -> tuple[str, tuple[int, int], int] | None:
    """The usable savebed whose respawn is the shortest walk from the spot."""
    return SavebedSearch(graph, spot_maps, owned=owned, excluded=excluded,
                         excluded_tiles=excluded_tiles, returns=returns).run().result


class FarmCircuitTask(BotTask):
    """Rotate between nearby farm maps without abandoning fights or loot."""

    PROGRESS_CHECKPOINT_INTERVAL = 5 * 60
    UPGRADE_SWEEP_INTERVAL = 24 * 60 * 60
    RECALL_SHOP_INTERVAL = 24 * 60 * 60
    UTILITY_SHOP_INTERVAL = 24 * 60 * 60
    UPGRADE_SWEEP_WALLET_GROWTH = 5_000
    UPGRADE_SWEEP_POLICY = 7
    BANK_DEPOSIT_MINIMUM = 1_000
    BANK_DEPOSIT_WEALTH_OVERRIDE = 10_000
    ROUTINE_TOWN_TRIP_WEIGHT = 0.85
    MASS_IDENTIFY_WEIGHT = 0.70
    # How long a map where a route death took combat damage is avoided.
    ROUTE_HAZARD_SECONDS = 6 * 3600.0
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
    AUTO_WIZARD_START_LEVEL = 18
    AUTO_WIZARD_FIRST_TARGET = 10
    AUTO_WIZARD_AOE_REVIEW_LEVEL = 25
    AUTO_WIZARD_AOE_TARGET = 15

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
        self._tier_progress_exp = 0

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
                    "_emergency_recall_cast_at", "_emergency_recall_origin")

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
        record, pause = self_improvement.learn(adaptations.get(key, {}), review)
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
        if pause:
            self._quarantine_farm_path(
                client, path, "repeated failures at the maximum adaptation level",
                seconds=self_improvement.PAUSE_SECONDS)
            return True
        # Rebuild the farm child so the tighter margins apply immediately, but
        # keep any Word of Recall still in flight: the recall decision is
        # recorded when it is fired, and a fresh task would fire another.
        if self.child is not None:
            previous = getattr(self.child, "task", None)
            self.child = self._new_child()
            for name in self.RECALL_STATE:
                if hasattr(previous, name):
                    setattr(self.child.task, name, getattr(previous, name))
            self._farm_started_at = 0.0
            self._empty_spawn_since = 0.0
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
        folded = name.casefold()
        return max((
            int(item.extra.get("level", 0) or 0)
            for item in client.state.inventory
            if item.item_type == c.TYPE_SKILL and
            item.name.casefold() == folded
        ), default=0)

    def _automatic_combat_plan(
            self, client: AtrinikClient) -> tuple[str, str, int]:
        """Choose the next bounded secondary-combat milestone."""
        if not self._auto_combat_build:
            return (self.combat_skill, self.combat_spell,
                    self.combat_skill_until_level)
        level = int(client.state.stats.get("level", 0) or 0)
        wizardry = self._skill_level(client, "wizardry spells")
        known = {
            item.name.casefold()
            for item in client.state.inventory
            if item.item_type == c.TYPE_SPELL
        }
        spell = next((name for name in ("magic bullet", "cause light wounds")
                      if name in known), "")
        if not spell or level < self.AUTO_WIZARD_START_LEVEL:
            return "", "", 0
        if wizardry < self.AUTO_WIZARD_FIRST_TARGET:
            return ("wizardry spells", spell,
                    self.AUTO_WIZARD_FIRST_TARGET)
        if (level >= self.AUTO_WIZARD_AOE_REVIEW_LEVEL and
                wizardry < self.AUTO_WIZARD_AOE_TARGET):
            return ("wizardry spells", spell,
                    self.AUTO_WIZARD_AOE_TARGET)
        return "", "", 0

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
        choice = analyzer_farm_choice(client, self.graph)
        if choice is None:
            # Nothing usable is ranked. Never keep farming a spot that live
            # failures just excluded: finish, and autoplay holds position
            # safely until a usable spot is ranked again.
            quarantine = getattr(client.state, "farm_zone_quarantine", {}) or {}
            now = time.time()
            if any(float(quarantine.get(path, 0.0) or 0.0) > now
                   for path, _ in self.legs):
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
        if self._busy_finishing_fight_or_loot(client, farm):
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
        # Aggressive residents are pulled one at a time from boundary squares
        # outside every other resident's authored detection radius; passive
        # spawns keep the adjacent patrol.
        aggressive = self._analyzer_safety and (bool(aggressive_spawns) or float(
            (self.spot or {}).get("aggressive_spawns", 0) or 0) > 0)
        isolated_radius_pulls = aggressive and bool(aggressive_spawns)
        patrol = []
        pull_avoidance: dict[
            tuple[int, int], set[tuple[int, int]]] = {}

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
                    max(1, other.aggro_radius)
                    for other in aggressive_spawns if other is not spawn
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
        farm = FarmTask(
            zone=zone, target=target, combat_skill=self.combat_skill,
            combat_spell=self.combat_spell, patrol=patrol,
            combat_skill_until_level=self.combat_skill_until_level,
            priority_spawns=[(spawn.x, spawn.y, spawn.named)
                             for spawn in priorities],
            neutral_targets=neutral_targets,
            aggressive_detection_ranges=aggressive_detection_ranges,
            pull_avoidance=pull_avoidance,
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
        return task

    @staticmethod
    def _food_reserve(client: AtrinikClient) -> int:
        return food_reserve(client)

    def _hungry(self, client: AtrinikClient) -> bool:
        return hungry(client)

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
        if self._food_identification_pending(client):
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
        return temple_food_trip(self.graph, client)

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
            try:
                route = navigation._plan(client)
            except ValueError:
                continue
            if not navigation._threat_fallback:
                ranked.append((
                    len(route),
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
            return task
        return NavigateThenTask(
            self.graph, "/shattered_islands/world_0_69",
            BuyGroundItemsTask(
                r"^staple food$", self.FOOD_RESUPPLY_QUANTITY,
                cash_reserve=1_000),
            destination_xy=(15, 7), combat_approach=True)

    @staticmethod
    def _unidentified_items(client: AtrinikClient) -> list[Item]:
        policy = InventoryPolicy()
        return [item for item in client.state.inventory
                if policy.apartment_unidentified(item) and
                not item.flags & c.ITEM_APPLIED]

    def _needs_apartment_storage(self, client: AtrinikClient) -> bool:
        policy = InventoryPolicy()
        unknown = self._unidentified_items(client)
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
            (urgent_recall_candidate or (
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
            needed = bool(self._unidentified_items(client) or unidentified_food(client))
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
        return task

    def _new_apartment_storage(self) -> NavigateThenTask:
        return NavigateThenTask(
            self.graph,
            "/shattered_islands/strakewood_island/apartments/apartment_cheap",
            DepositItemsTask(
                "chest", unidentified_only=True, valuable_only=True),
            destination_xy=(1, 3), combat_approach=True)

    def _spot_maps(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for path, _ in self.legs))

    def _savebed_search(self, client: AtrinikClient) -> SavebedSearch:
        """This circuit's nearest-savebed search, restarted when its inputs change."""
        bed_map = getattr(client.state, "savebed_map", "")
        bed_xy = getattr(client.state, "savebed_xy", None)
        current = (bed_map, tuple(bed_xy)) if bed_map and bed_xy else None
        returns = dict(getattr(client.state, "apartment_returns", {}) or {})
        key = (self._spot_maps(), frozenset(self._bed_failed), current,
               frozenset(returns.items()))
        if self._bed_search is None or self._bed_search[0] != key:
            self._bed_search = (key, SavebedSearch(
                self.graph, key[0], excluded_tiles=self._bed_failed, current=current,
                returns=returns))
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
        return NavigateThenTask(
            self.graph, target[0], BindSavebedTask(names=tuple(filter(None, (
                getattr(self.graph, "savebed_names", {}).get((target[0], *target[1])),)))),
            destination_xy=target[1], combat_approach=True)

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
        return NavigateThenTask(
            self.graph, "/shattered_islands/world_5_58",
            BuyGroundItemsTask(r"cure illness", 1, cash_reserve=500),
            destination_xy=(9, 17), combat_approach=True)

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
        return task

    def _queue_depletion_service(self, client: AtrinikClient) -> bool:
        if not self._needs_depletion_service(client):
            return False
        self._restoration = self._new_depletion_service(client)
        return True

    @staticmethod
    def _junk_policy(tags=()) -> JunkPolicy:
        return JunkPolicy((
            r"^paper ",
            r"^(?:iron|bronze|steel|wooden|spruce|soft leather) .*(?:sword|axe|mace|club|spear|bow|shield|armour|skirt|sandals|boots|gloves|helmet)$",
        ), tags=frozenset(tags))

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
            InventoryPolicy().weight_ratio(client) >=
            self.ROUTINE_TOWN_TRIP_WEIGHT
        ) and self.clock() >= self._selling_retry_at

    def _new_junk_sale(self, client: AtrinikClient) -> NavigateThenTask:
        surplus = self._surplus_equipment(client)
        return NavigateThenTask(
            self.graph, "/shattered_islands/world_0_69",
            SellJunkTask(
                "Brynknot shop floor",
                self._junk_policy(item.tag for item in surplus)),
            destination_xy=(15, 7), combat_approach=True)

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
        return (needs_budget and not client.state.bank_balance_known and
                self.clock() >= self._bank_sync_retry_at)

    def _new_bank_sync(self) -> NavigateThenTask:
        return NavigateThenTask(
            self.graph, "/shattered_islands/world_0_67",
            BankBalanceTask("Tolmir"), destination_xy=(19, 15),
            combat_approach=True)

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
            self._storage, self._identification,
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
        return NavigateThenTask(
            self.graph, "/shattered_islands/world_0_67",
            BankTask("Tolmir", "all"), destination_xy=(19, 15),
            combat_approach=True)

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
        policy = InventoryPolicy()
        return any(
            item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and
            "word of recall" in item.name.casefold() and
            policy.identified(item) and
            not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)
            for item in client.state.inventory)

    @staticmethod
    def _owned_recall_spellbook(client: AtrinikClient) -> bool:
        return any(
            item.item_type == c.TYPE_SPELLBOOK and
            "word of recall" in item.name.casefold()
            for item in client.state.inventory)

    def _needs_recall_shopping(self, client: AtrinikClient) -> bool:
        if (int(client.state.stats.get("level", 0) or 0) < 18 or
                not self._has_ship_key(client) or
                BuyShopUpgradeTask.wallet_value(client) < 1_250 or
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
            ranked.append((len(route), not immediate_roll, vendor.level,
                           vendor.name.casefold(), vendor))
        vendor = (min(ranked, key=lambda entry: entry[:-1])[-1]
                  if ranked else DialogueVendor(
            "/shattered_islands/world_4_47", 11, 13, "sage",
            "/python/generic/merchant.py"))
        task = NavigateThenTask(
            self.graph, vendor.map_path,
            BuyDialogueStockTask(vendor.name, r"word of recall"),
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
            start_index=cursor)

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
                                      farm: FarmTask) -> bool:
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
        return bool(
            selected_is_visible or selected_is_alive or
            recent_hostile_contact(client) or
            farm._engaged_target or corpse_with_contents or
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
        if self._bank_sync is not None:
            await self._bank_sync.tick(client)
            if self._bank_sync.status in (TaskStatus.COMPLETE,
                                           TaskStatus.FAILED):
                if self._bank_sync.status == TaskStatus.FAILED:
                    log.warning("bank balance sync failed: %s",
                                self._bank_sync.error)
                    self._bank_sync_retry_at = self.clock() + 1800.0
                else:
                    log.info("bank balance synchronized: %d copper",
                             client.state.bank_balance)
                    self._bank_sync_retry_at = self.clock() + 3600.0
                self._bank_sync = None
                # Chain urgent stat restoration before a dense farm respawn
                # can acquire a new target and starve the service indefinitely.
                if self._queue_depletion_service(client):
                    await client.clear_actions()
                    await client.set_combat(False)
                    await self._restoration.tick(client)
            return
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
            return
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
            return
        if self._restoration is not None:
            await self._restoration.tick(client)
            if self._restoration.status in (TaskStatus.COMPLETE,
                                             TaskStatus.FAILED):
                if self._restoration.status == TaskStatus.FAILED:
                    log.warning("depletion removal failed: %s",
                                self._restoration.error)
                    backoff = self.restoration_backoff(self._restoration.error)
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
            return
        if self._ship_key is not None:
            await self._ship_key.tick(client)
            if self._ship_key.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                if self._ship_key.status == TaskStatus.FAILED:
                    log.warning("ship key acquisition failed: %s",
                                self._ship_key.error)
                    self._ship_key_retry_at = self.clock() + 1800.0
                else:
                    log.info("Morg'eean ship key acquired; shortcuts enabled")
                    self._ship_key_retry_at = self.clock() + 86400.0
                self._ship_key = None
            return
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
            return
        if self._recall_shopping is not None:
            await self._recall_shopping.tick(client)
            if self._recall_shopping.status in (
                    TaskStatus.COMPLETE, TaskStatus.FAILED):
                shopper = self._recall_shopping.task
                if self._recall_shopping.status == TaskStatus.FAILED:
                    log.warning("recall shopping failed: %s",
                                self._recall_shopping.error)
                    self._recall_shopping_retry_at = self.clock() + 7200.0
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
                    self._recall_shopping_retry_at = self.clock() + 60.0
                self._recall_shopping = None
            return
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
            return
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
            return
        if self._banking is not None:
            await self._banking.tick(client)
            if self._banking.status == TaskStatus.FAILED:
                log.warning("bank deposit failed: %s", self._banking.error)
                self._banking_retry_at = self.clock() + 1800.0
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
            return
        if self._selling is not None:
            await self._selling.tick(client)
            if self._selling.status == TaskStatus.FAILED:
                log.warning("identified junk sale failed: %s",
                            self._selling.error)
                self._selling_retry_at = self.clock() + 1800.0
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
            return
        if self._identification is not None:
            await self._identification.tick(client)
            if self._identification.status == TaskStatus.FAILED:
                log.warning("mass identification failed: %s",
                            self._identification.error)
                self._identification_retry_at = self.clock() + 1800.0
                self._identification = None
            elif self._identification.status == TaskStatus.COMPLETE:
                # Evaluate the newly revealed batch before returning to a
                # spawn map, where an immediate target could otherwise defer
                # a weapon or armour upgrade indefinitely.
                farm = self.child.task
                if (isinstance(farm, FarmTask) and
                        await farm.maintain_inventory(client)):
                    return
                log.info("mass identification complete; returning to farm")
                self._identification_retry_at = self.clock() + 3600.0
                self._identification = None
            return
        if self._storage is not None:
            await self._storage.tick(client)
            if self._storage.status == TaskStatus.FAILED:
                log.warning("unidentified storage failed: %s",
                            self._storage.error)
                self._storage_retry_at = self.clock() + 1800.0
                self._storage = None
            elif self._storage.status == TaskStatus.COMPLETE:
                log.info("apartment loot storage complete; returning to farm")
                self._storage = None
            return
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
            return
        if self._resupply is not None:
            await self._resupply.tick(client)
            self._settle_resupply(client)
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
            self._bank_sync = self._new_bank_sync()
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
        # Survival supplies first, then clear unknown/cursed-item risk and
        # carrying pressure; discretionary shopping is always last.
        if (isinstance(farm, FarmTask) and self._needs_food_resupply(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            if self.clock() >= self._resupply_retry_at:
                resupply, kind = self._new_food_resupply(client), "shop"
            else:
                resupply, kind = self._new_temple_food(client), "temple"
                if resupply is None:
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
            self._storage = self._new_apartment_storage()
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
                not self._busy_finishing_fight_or_loot(client, farm)):
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
