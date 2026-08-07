"""Offline map graph, landmark index, and live route following."""

from __future__ import annotations

import ast
import json
import logging
import math
import posixpath
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from collections.abc import Callable, Sequence

try:
    from tools.world_content_audit import (ROOT as CONTENT_ROOT, flatten,
                                           load_archetypes, map_files,
                                           parse_blocks)
except ModuleNotFoundError:
    from world_content_audit import (ROOT as CONTENT_ROOT, flatten,
                                     load_archetypes, map_files, parse_blocks)

from . import constants as c

from .client import AtrinikClient, map_object_visual_name
from .pathfinding import graph_search, grid_reachable, grid_search
from .tasks import (BankBalanceTask, BankTask, BindSavebedTask, BotTask,
                    BuyDialogueStockTask, BuyGroundItemsTask,
                    BuyShopUpgradeTask,
                    DepositItemsTask, FarmTask, InventoryPolicy, JunkPolicy,
                    InventoryCapabilityTask, MassIdentifyTask, SafetyPolicy,
                    SellJunkTask, TaskStatus,
                    TempleServiceTask,
                    movement_ack_timeout, recent_hostile_attackers,
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
        messages = client.state.messages
        for entry in messages[self.message_index:]:
            if len(entry) <= 3:
                continue
            parsed = self.parse(entry[3])
            if parsed is not None:
                self.anchor_minute = float(parsed)
                self.anchor_at = self.clock()
        self.message_index = len(messages)
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
        self.food_profiles: dict[str, FoodProfile] = {}
        self.shop_stocks: dict[str, list[tuple[int, int]]] = defaultdict(list)
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
        for path in map_files():
            parsed = parse_blocks(path)
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
                     ) -> list[MapEdge]:
        """Route end-to-end between exact walkable components across maps."""
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
                        route_edges.append(adjusted)
            offsets.append(len(targets))
            cursor += 1
        if not goals:
            raise ValueError(
                f"no component route from {source} {source_xy} to "
                f"{destination} {destination_points}")
        result = graph_search(
            offsets, targets, 0, goals, metadata=edge_metadata)
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

    def _known_pack_hazard_maps(self) -> set[str]:
        """Return open-world maps with three-plus joining aggressors.

        Interior maps often contain several separated rooms and may require a
        brief exterior hop to reach another component. Treating every pair on
        every authored map as an impassable country-sized hazard made ordinary
        buildings unroutable. Three connected aggressors on an open surface is
        the live-validated pursuit failure this coarse map-level gate models;
        smaller or room-separated contacts remain the live combat policy's
        responsibility.
        """
        estimator = getattr(self.graph, "transit_max_aggro_pack", None)
        if estimator is None:
            return set()
        return {
            path for path in self.graph.named_spawns
            if re.fullmatch(r"/shattered_islands/world_\d+_\d+", path) and
            estimator(path) >= 3
        }

    def _plan(self, client: AtrinikClient) -> list[MapEdge]:
        source = client.state.map.path
        args = (
            source,
            (client.state.map.world_x, client.state.map.world_y),
            self.destination,
            self._goal_points(),
        )
        ranged_hazards = self._known_ranged_hazard_maps(client)
        overlevel_hazards = self._known_overlevel_hazard_maps(client)
        pack_hazards = self._known_pack_hazard_maps()
        protected_hazards = (
            ranged_hazards | pack_hazards | set(self.KNOWN_LETHAL_TRANSIT))
        kwargs = {
            "allow_locked": self._allow_locked(client),
            "excluded": self._excluded_edges,
            "avoided_maps": protected_hazards | overlevel_hazards,
        }
        previous_fallback = self._threat_fallback
        self._threat_fallback = False
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
                if overlevel_hazards:
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs["avoided_maps"] = protected_hazards
                    try:
                        route = self.graph.route_points(
                            *args, **fallback_kwargs)
                    except ValueError:
                        if not self.allow_ranged_hazard_fallback:
                            excluded = sorted(self._excluded_edges)
                            raise ValueError(
                                f"{preferred_exc}; excluded edges: "
                                f"{excluded}") from preferred_exc
                        fallback_kwargs["avoided_maps"] = set(
                            self.KNOWN_LETHAL_TRANSIT)
                        route = self.graph.route_points(
                            *args, **fallback_kwargs)
                    self._threat_fallback = True
                    if not previous_fallback:
                        recorder = getattr(client, "record_action", None)
                        if recorder is not None:
                            recorder(
                                "navigation-threat-fallback",
                                f"no threat-free route to {self.destination}")
                elif (self.allow_ranged_hazard_fallback and
                      (ranged_hazards or pack_hazards)):
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs["avoided_maps"] = set(
                        self.KNOWN_LETHAL_TRANSIT)
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
                else:
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
        if not path:
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

    async def _defend_while_navigating(self, client: AtrinikClient) -> bool:
        """Pause an encounter approach to heal or clear an aggroed pack."""
        if not self.combat_approach:
            return False
        # Threat halos describe this viewport, not permanent terrain. Rebuild
        # them every defensive tick so killing a road pack opens its corridor.
        self.navigation._threat_blocked.clear()
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        threats = []
        node = getattr(self.navigation.graph, "nodes", {}).get(m.path)
        destination_node = getattr(
            self.navigation.graph, "nodes", {}).get(
                self.navigation.destination)
        global_peaceful = getattr(
            self.navigation.graph, "peaceful_monster_identities", set())
        for x, y, obj in m.targets(friendly=False):
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
                isinstance(self.task, FarmTask) and
                self.task.should_training_finish(client, nearest[3])):
            _, x, y, obj = nearest
            self.task.remember_engagement(client, x, y, obj.target_id)
            await client.target(x, y, obj.target_id)
            if await self.task.training_finisher(client, x, y, obj):
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
                len(pull_pack) >= 2 and pack_offensive is None):
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
                len(pull_pack) >= 2 and
                time.monotonic() - self._last_pull_at >= 2.5):
            offensive = pack_offensive
            if (offensive is not None and
                    self.task.offensive_spell_affordable(client, offensive)):
                _, x, y, obj = nearest
                await client.clear_actions()
                if isinstance(self.task, FarmTask):
                    self.task.remember_engagement(
                        client, x, y, obj.target_id)
                await client.target(x, y, obj.target_id)
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
        if not safe or overwhelmed:
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
                await client.target(x, y, obj.target_id)
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
    FOOD_RESUPPLY_QUANTITY = 25
    FOOD_RESUPPLY_NUTRITION = 10_000
    FOOD_RESUPPLY_MAX_QUANTITY = 50
    DEPLETION_SERVICE_THRESHOLD = 3
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

    EARLY_NAMED_LEGS = (
        ("/shattered_islands/world_3_69", "Thrakir|lost soul"),
        ("/shattered_islands/world_3_68", "Fahrgorm|evil treant"),
    )
    EARLY_WASP_LEGS = (
        ("/shattered_islands/world_3_69", "giant wasp|wasp_giant|wasp giant"),
    )
    EARLY_DAY_BEE_LEGS = (
        ("/shattered_islands/world_4_68",
         "killer bee|bee_killer|bee killer"),
    )
    EARLY_TREE_LEGS = (
        ("/shattered_islands/world_10_78", "evil treant|quickwood"),
    )
    WIZARD_LOST_SOUL_LEGS = (
        ("/shattered_islands/world_3_69", "Thrakir|lost soul"),
    )
    # farming_analyzer.py ranks this connected level-9--12 mud-hand circuit
    # second for Sera's measured Wizardry 6 and 8 builds. Its nominally
    # higher-scoring Brynknot sewer circuit is not component-routable from the
    # surface entrance, while both of these maps are a verified short route
    # from the existing Eld Woods farm. Their residents are aggressive, but
    # their authored detection radii permit controlled one-at-a-time pulls;
    # the wider viewport cluster is density context, not one encounter.
    WIZARD_MUD_HAND_CATCHUP_LEGS = (
        ("/shattered_islands/world_12_79", "mud hand"),
        ("/shattered_islands/world_12_78", "mud hand"),
    )
    MID_TREE_LEGS = (
        ("/shattered_islands/world_14_79", "evil treant|quickwood"),
    )
    QUICKWOOD_11_LEGS = (
        ("/shattered_islands/world_14_79", "quickwood"),
    )
    TREE_13_LEGS = (
        ("/shattered_islands/world_14_78", "evil treant|quickwood"),
    ) + MID_TREE_LEGS
    TREE_15_LEGS = (
        ("/shattered_islands/world_14_77",
         "Kotung|evil treant|quickwood"),
    ) + TREE_13_LEGS
    # Quickwoods cease awarding Sera XP at level 18.  Keep the same proven
    # three-map respawn circuit but stop spending combat time, HP, food and
    # mana on its zero-value filler population.
    TREE_18_LEGS = (
        ("/shattered_islands/world_14_77", "Kotung|evil treant"),
        ("/shattered_islands/world_14_78", "evil treant"),
    )
    # At Slash 19 the level-13 evil treants on world_14_78 cross the server's
    # grey threshold and award no XP. Keep only the level-15--17 residents in
    # the upper pocket rather than paying combat, healing, food, corpse and
    # travel overhead for a zero-value fallback map.
    TREE_19_LEGS = (
        ("/shattered_islands/world_14_77", "Kotung|evil treant"),
    )
    # The server's exact level-color table makes level-15 residents grey at
    # skill 22, while level-16 evil treants and level-17 Kotung still pay.
    # Patrol only Kotung's shared spawn point instead of automatically moving
    # to the live-rejected deer or untested zero-acid-protection slug tier.
    TREE_22_LEGS = (
        ("/shattered_islands/world_14_77", "Kotung"),
    )
    # Historical guarded trial retained for its quarantine regression. Live
    # play showed that the nominally passive spider factions assist one
    # another: three became visible, at least two attacked together, exhausted
    # healing, and killed Sera. It must not be selected automatically.
    FORT_SETHER_19_LEGS = ((
        "/shattered_islands/world_4_51_-2",
        "spider|brown bat|sword spider"),)
    # Historical analyzer candidates retained only for explicit guarded tests.
    # Live play invalidated the selected-subset premise: nearby crocodiles and
    # frogs joined, pursued across map seams, exhausted healing, and caused a
    # death. Automatic Slash progression uses TREE_18_LEGS instead. A manual
    # retry still receives death and ten-minute no-XP quarantine protection.
    ANALYZER_SLASH_18_LEGS = (
        ("/shattered_islands/world_4_51", "mud hand"),
    )
    # Kill XP is calculated from the finishing skill's level, not character
    # level.  These lower-level passive targets still reach the per-kill cap
    # for Wizardry 6-9 while dying faster and costing less healing than the
    # level-15-17 main-school tier.  The night-only lost-soul check adds a
    # 1/30 Ring of the Ghost opportunity without idling for its schedule.
    WIZARD_LOW_CATCHUP_LEGS = (
        WIZARD_LOST_SOUL_LEGS + WIZARD_MUD_HAND_CATCHUP_LEGS)
    WIZARD_MID_CATCHUP_LEGS = (
        WIZARD_LOST_SOUL_LEGS + WIZARD_MUD_HAND_CATCHUP_LEGS)
    DARK_CAVE_WYVERN_LEGS = ((
        "/shattered_islands/strakewood_island/dark_cave/dark_cave_0101",
        "fire wyvern lord|fire wyvern"),)
    DEER_LEGS = (("/shattered_islands/world_11_75", "deer"),)
    DEER_TIER_LEGS = DEER_LEGS + TREE_15_LEGS
    # Revisit the previously rejected deer only after concrete equipment
    # improvements reduce modeled per-kill damage by roughly thirty percent.
    # The one remaining XP-bearing tree pocket is a respawn fallback.
    DEER_READINESS_LEGS = DEER_LEGS + TREE_19_LEGS
    # The corrected analyzer separates direct acquisition from viewport
    # density. This level-18--20 field has six nearby aggressive residents but
    # no isolated pull point is inside more than one resident's detection
    # radius. Its modeled L20 profile is both safer and several times faster
    # than the last Eld Woods tree spawn. Retain the map after recoverable bad
    # pulls: retreat, approach a different isolated boundary, and retry.
    STRAKEWOOD_20_LEGS = ((
        "/shattered_islands/world_1_50",
        "giant frog|crocodile|mud hand"),)
    SLUG_LEGS = (("/shattered_islands/world_13_73", "giant slug"),)
    RED_ANT_LEGS = (("/shattered_islands/world_14_73", "red ant"),)
    FARM_DEATH_QUARANTINE_SECONDS = 6 * 60 * 60
    FARM_NO_PROGRESS_SECONDS = 10 * 60
    OPTIONAL_RARE_DETOUR_INTERVAL = 30 * 60
    # Higher-level autoplay tiers are deliberately anchored to maps whose
    # authored residents and graph routes are known exactly.  Empty target
    # patterns farm every server-confirmed hostile on the destination map;
    # peaceful NPC classification still excludes guards and quest actors.
    # Thresholds trail the strongest resident where practical so a secondary
    # skill cannot push the character into a nominal map-difficulty band.
    UNDERGROUND_CITY_31_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_0_1", ""),)
    HEMLOCK_37_LEGS = ((
        "/shattered_islands/eld_woods_island/hemlock_cave/"
        "hemlock_cave_0103", ""),)
    UNDERGROUND_CITY_46_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_4_4_-1", ""),)
    UNDERGROUND_CITY_55_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_5_2_-1", ""),)
    ZECHNA_60_LEGS = ((
        "/shattered_islands/strakewood_island/zechna_temple/"
        "zechna_temple_1_0", ""),)
    UNDERGROUND_CITY_68_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_1_3_-2", ""),)
    UNDERGROUND_CITY_80_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_0_0_-2", ""),)
    UNDERGROUND_CITY_83_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_1_0_-2", ""),)
    ZECHNA_88_LEGS = ((
        "/shattered_islands/strakewood_island/zechna_temple/"
        "zechna_temple_1_0_-2", ""),)
    ZECHNA_95_LEGS = ((
        "/shattered_islands/strakewood_island/zechna_temple/"
        "zechna_temple_0_1_-2", ""),)
    UNDERGROUND_CITY_100_LEGS = ((
        "/shattered_islands/strakewood_island/underground_city/"
        "underground_city_6_-1_-3", ""),)
    ZECHNA_99_LEGS = ((
        "/shattered_islands/strakewood_island/zechna_temple/"
        "zechna_temple_0_0_-3", "lom lobon|dread gazer|gazer dread"),)
    EARLY_BASE_LEGS = (EARLY_NAMED_LEGS + EARLY_WASP_LEGS +
                       EARLY_DAY_BEE_LEGS + EARLY_TREE_LEGS)
    EARLY_SAFE_LEGS = EARLY_BASE_LEGS

    def __init__(self, graph: WorldGraph,
                 legs: Sequence[tuple[str, str]], *,
                 dwell_seconds: float = 90.0, switch_grace: float = 30.0,
                 level_until: int = 0, combat_skill: str = "",
                 combat_spell: str = "",
                 combat_skill_until_level: int = 0,
                 clear_hostile_route: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        if not legs:
            raise ValueError("a farm circuit needs at least one leg")
        super().__init__("farm-circuit:" + "+".join(
            path.rsplit("/", 1)[-1] for path, _ in legs))
        self.graph = graph
        self.legs = tuple((path, target) for path, target in legs)
        self._adaptive_early_progression = (
            self.legs == self.EARLY_SAFE_LEGS)
        self.dwell_seconds = max(5.0, float(dwell_seconds))
        self.switch_grace = max(0.0, float(switch_grace))
        self.level_until = max(0, int(level_until))
        self.combat_skill = combat_skill
        self.combat_spell = combat_spell
        self.combat_skill_until_level = max(
            0, int(combat_skill_until_level))
        self._auto_combat_build = bool(
            self._adaptive_early_progression and
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
        self._storage: NavigateThenTask | None = None
        self._storage_retry_at = 0.0
        self._identification: NavigateThenTask | None = None
        self._identification_retry_at = 0.0
        self._capability: InventoryCapabilityTask | None = None
        self._capability_retry_at: dict[str, float] = {}
        self._bed_binding: NavigateThenTask | None = None
        self._bed_retry_at = 0.0
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
        self._tier_progress_exp = 0

    def _progression_legs(self, level: int) -> tuple[tuple[str, str], ...]:
        """Retain rare-spawn checks while upgrading out-levelled XP legs."""
        if not self._adaptive_early_progression:
            return self.legs
        if level >= 102:
            # These level-98/99 residents remain above the server's level-115
            # grey threshold (82), while avoiding the adjacent level-114/115
            # balrog tier until protections and live evidence justify it.
            return self.ZECHNA_99_LEGS
        if level >= 98:
            return self.UNDERGROUND_CITY_100_LEGS
        if level >= 92:
            return self.ZECHNA_95_LEGS
        if level >= 85:
            return self.ZECHNA_88_LEGS
        if level >= 82:
            return self.UNDERGROUND_CITY_83_LEGS
        if level >= 76:
            return self.UNDERGROUND_CITY_80_LEGS
        if level >= 64:
            return self.UNDERGROUND_CITY_68_LEGS
        if level >= 57:
            return self.ZECHNA_60_LEGS
        if level >= 52:
            return self.UNDERGROUND_CITY_55_LEGS
        if level >= 44:
            return self.UNDERGROUND_CITY_46_LEGS
        if level >= 36:
            return self.HEMLOCK_37_LEGS
        if level >= 30:
            return self.UNDERGROUND_CITY_31_LEGS
        if level >= 22:
            return self.TREE_22_LEGS
        if level >= 19:
            return self.TREE_19_LEGS
        if level >= 18:
            # The analyzer's selected-spawn estimates omitted inevitable
            # crocodile/frog joins on a fresh map. Live trials exhausted the
            # healing loop on both western and eastern candidates; retain the
            # already-proven XP-bearing Eld Woods tree pockets.
            return self.TREE_18_LEGS
        if level >= 15:
            return self.TREE_15_LEGS
        if level >= 13:
            return self.TREE_13_LEGS
        if level >= 11:
            # Live trials invalidated the static "melee-only" ogre audit:
            # world_6_56 spawned a stone giant, two ettins and two ogres in
            # immediate mutual aggro range, while the later belts remain
            # unverified. Keep automatic progression on isolated passive trees
            # until every higher-risk pocket has survived a controlled trial.
            return self.MID_TREE_LEGS
        if level >= 10:
            return self.QUICKWOOD_11_LEGS
        if level >= 9:
            return self.EARLY_SAFE_LEGS
        return self.EARLY_BASE_LEGS

    def _safe_progression_legs(
            self, client: AtrinikClient,
            level: int) -> tuple[tuple[str, str], ...]:
        """Fall back below quarantined or inherently crowded farm maps."""
        now = time.time()
        quarantine = getattr(
            client.state, "farm_zone_quarantine", {}) or {}
        if (19 <= level < 25 and self._deer_readiness(client) and
                float(quarantine.get(
                    self.DEER_LEGS[0][0], 0.0) or 0.0) <= now and
                all(self.graph.farm_max_aggro_pack(path) <= 1
                    for path, _ in self.DEER_READINESS_LEGS)):
            return self.DEER_READINESS_LEGS
        if (20 <= level < 22 and self._strakewood_readiness(client) and
                all(self.graph.farm_max_aggro_pack(path) <= 1
                    for path, _ in self.STRAKEWOOD_20_LEGS)):
            return self.STRAKEWOOD_20_LEGS
        for candidate_level in range(max(1, level), 0, -1):
            legs = self._progression_legs(candidate_level)
            if all(
                    float(quarantine.get(path, 0.0) or 0.0) <= now and
                    self.graph.farm_max_aggro_pack(path) <= 1
                    for path, _ in legs):
                return legs
        return self.EARLY_BASE_LEGS

    @staticmethod
    def _deer_readiness(client: AtrinikClient) -> bool:
        """Require the live defence/offence profile that justifies a retrial."""
        stats = client.state.stats
        protections = client.state.protections
        return bool(
            int(stats.get("level", 0) or 0) >= 20 and
            int(stats.get("maxhp", 0) or 0) >= 190 and
            int(stats.get("ac", 0) or 0) >= 35 and
            int(stats.get("wc", 0) or 0) >= 28 and
            int(stats.get("dam", 0) or 0) >= 38 and
            int(protections.get(c.ATTACK_IMPACT, 0) or 0) >= 40 and
            int(protections.get(c.ATTACK_SLASH, 0) or 0) >= 45 and
            int(protections.get(c.ATTACK_CLEAVE, 0) or 0) >= 50 and
            int(protections.get(c.ATTACK_PIERCE, 0) or 0) >= 45)

    @staticmethod
    def _strakewood_readiness(client: AtrinikClient) -> bool:
        """Require the live L21 profile used by the corrected farm model."""
        stats = client.state.stats
        protections = client.state.protections
        return bool(
            int(stats.get("level", 0) or 0) >= 21 and
            int(stats.get("maxhp", 0) or 0) >= 200 and
            int(stats.get("ac", 0) or 0) >= 36 and
            int(stats.get("wc", 0) or 0) >= 29 and
            int(stats.get("dam", 0) or 0) >= 40 and
            int(protections.get(c.ATTACK_IMPACT, 0) or 0) >= 40 and
            int(protections.get(c.ATTACK_SLASH, 0) or 0) >= 45 and
            int(protections.get(c.ATTACK_CLEAVE, 0) or 0) >= 50 and
            int(protections.get(c.ATTACK_PIERCE, 0) or 0) >= 45)

    def _catchup_progression_legs(
            self, client: AtrinikClient) -> tuple[tuple[str, str], ...]:
        """Select faster, loot-aware targets for a lagging trained skill."""
        if (not self._auto_combat_build or
                self.combat_skill != "wizardry spells" or
                not self.combat_skill_until_level):
            return ()
        wizardry = self._skill_level(client, "wizardry spells")
        if wizardry >= self.combat_skill_until_level:
            return ()
        legs = list(self.WIZARD_LOW_CATCHUP_LEGS if wizardry < 8 else
                    self.WIZARD_MID_CATCHUP_LEGS)

        # The fire-wyvern lord is a useful catch-up target and has a 1/80
        # special bracer drop, but its authored firestorm makes an unprotected
        # automatic visit needless tail risk.  Add it only from authoritative
        # live protection state, and stop the rare hunt once the drop is owned.
        has_bracers = any(
            "fire wyvern" in item.name.casefold()
            for item in client.state.inventory)
        fire_protection = int(client.state.protections.get(
            c.ATTACK_FIRE, 0) or 0)
        if fire_protection >= 10 and not has_bracers:
            legs[1:1] = self.DARK_CAVE_WYVERN_LEGS

        now = time.time()
        quarantine = getattr(
            client.state, "farm_zone_quarantine", {}) or {}
        return tuple(
            leg for leg in legs
            if float(quarantine.get(leg[0], 0.0) or 0.0) <= now and
            self.graph.farm_max_aggro_pack(leg[0]) <= 1)

    def _observe_farm_death(self, client: AtrinikClient) -> bool:
        """Quarantine a farm after death or a guarded-trial safety failure."""
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
        death = next((record for record in reversed(records)
                      if record.get("action") == "death"), None)
        if death is None:
            quarantine = getattr(
                client.state, "farm_zone_quarantine", {}) or {}
            recent_cutoff = time.time() - 10 * 60
            maxhp = int(client.state.stats.get("maxhp", 0) or 0)
            hp = int(client.state.stats.get("hp", maxhp) or 0)
            guarded_trials = (
                (self.DEER_READINESS_LEGS, self.DEER_LEGS[0][0],
                 {"emergency-disengage"}, 0.82,
                 "guarded deer trial required emergency disengagement"),
            )
            for legs, path, actions, threshold, reason in guarded_trials:
                if (self.legs != legs or path not in active_paths or
                        float(quarantine.get(path, 0.0) or 0.0) >
                        time.time()):
                    continue
                failure = next((
                    record for record in reversed(history)
                    if (record.get("action") in actions and
                        record.get("map") == path and
                        (record in records or
                         float(record.get("time", 0.0) or 0.0) >=
                         recent_cutoff))), None)
                live_breach = bool(
                    client.state.map.path == path and maxhp > 0 and
                    hp / maxhp <= threshold)
                if failure is None and not live_breach:
                    continue
                self._quarantine_farm_path(client, path, reason)
                return True
            return False
        death_map = str(death.get("map", ""))
        path = death_map if death_map in active_paths else ""
        # The authored destination can be suitable while its only approach is
        # not (the live Old Outpost trial proved exactly this). During the
        # ordinary farm NavigateThen child, attribute a transit death to the
        # requested destination so autoplay demotes instead of replaying the
        # same lethal road after every respawn. Maintenance children are held
        # separately and therefore never reach this branch.
        if (not path and self.child is not None and
                self.child.navigation.destination in active_paths and
                self.child.navigation.status != TaskStatus.COMPLETE):
            path = self.child.navigation.destination
        if not path:
            return False
        if path == self.STRAKEWOOD_20_LEGS[0][0]:
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "farm-retry-after-death",
                    f"{path} retained for improved pull recovery")
            return False
        self._quarantine_farm_path(
            client, path,
            f"combat death on {death_map or 'unknown map'}")
        return True

    def _quarantine_farm_path(self, client: AtrinikClient, path: str,
                              reason: str) -> None:
        until = time.time() + self.FARM_DEATH_QUARANTINE_SECONDS
        setter = getattr(client, "quarantine_farm_zone", None)
        if setter is not None:
            setter(path, until)
        else:
            client.state.farm_zone_quarantine[path] = until
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("farm-zone-quarantine",
                     f"{path} until={int(until)} after {reason}")
        log.warning("autoplay quarantined farm map for six hours: %s (%s)",
                    path, reason)

    def _observe_stalled_farm(self, client: AtrinikClient,
                              farm: FarmTask) -> bool:
        """Demote a high tier which produces no character XP for ten minutes."""
        analyzer_trial = self.legs in (
            self.ANALYZER_SLASH_18_LEGS,
            self.FORT_SETHER_19_LEGS,
        )
        if (not self._adaptive_early_progression or
                (self._progression_level < 30 and not analyzer_trial) or
                self.child is None or
                client.state.map.path != self.child.navigation.destination):
            self._tier_progress_at = None
            return False
        now = self.clock()
        if (self._tier_progress_at is None or
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
        level = self._combat_progression_level(client)
        self._progression_level = level
        desired = (self._catchup_progression_legs(client) or
                   self._safe_progression_legs(client, level))
        if desired == self.legs or self._busy_finishing_fight_or_loot(
                client, farm):
            return False
        self.legs = desired
        self.leg_index = self._next_ready_leg(0, level)
        self.child = self._new_child()
        self._farm_started_at = 0.0
        self._empty_spawn_since = 0.0
        self._tier_progress_at = None
        self._tier_progress_exp = self._current_exp
        self.name = "farm-circuit:" + "+".join(
            path.rsplit("/", 1)[-1] for path, _ in self.legs)
        log.info("farm progression upgraded for combat level %s: %s",
                 level,
                 ", ".join(path.rsplit("/", 1)[-1]
                           for path, _ in self.legs))
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
        # The isolated-radius planner is enabled for the analyzed dense farm
        # whose authored geometry and direct-aggro result were verified. Keep
        # established adjacent patrols elsewhere until each map has the same
        # evidence; an item_power radius alone does not prove its approach is
        # suitable for this pull style.
        isolated_radius_pulls = (
            zone == self.STRAKEWOOD_20_LEGS[0][0])
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
        if "lost soul" in target.casefold():
            # One soul produces closely spaced physical + secondary damage
            # packets; a 55% trigger can lose the entire margin while a heal
            # merely matches the next attack. Begin control early enough to
            # create separation before the following weapon cycle.
            farm.safety.flee_below = 0.72
            farm.safety.heal_below = 0.88
        if zone.endswith("/world_6_56"):
            farm.safety.flee_below = 0.72
            farm.safety.heal_below = 0.88
        elif zone.endswith(("/world_14_79", "/world_14_78")):
            farm.safety.flee_below = 0.75
            farm.safety.heal_below = 0.90
        elif zone.endswith("/world_4_51_-2"):
            farm.safety.flee_below = 0.80
            farm.safety.heal_below = 0.95
        elif zone.endswith("/world_4_51"):
            farm.safety.flee_below = 0.75
            farm.safety.heal_below = 0.92
        elif zone.endswith("/world_14_77"):
            farm.safety.flee_below = 0.78
            farm.safety.heal_below = 0.92
        elif zone.endswith("/world_11_75"):
            # This high-throughput field previously produced a 1%-HP escape.
            # The readiness-gated retrial starts disengagement well before
            # that tail and automatically demotes after the first emergency.
            farm.safety.flee_below = 0.82
            farm.safety.heal_below = 0.95
        elif zone.endswith("/world_1_50"):
            # Enter each resident's radius through its isolated pull square,
            # then use timer-synchronized kiting to draw it away. A bad pull
            # disengages and recovers but does not abandon this good map.
            farm.safety.flee_below = 0.82
            farm.safety.heal_below = 0.95
        elif zone.endswith("/world_13_73"):
            farm.safety.flee_below = 0.78
            farm.safety.heal_below = 0.92
        elif zone.endswith("/world_14_73"):
            farm.safety.flee_below = 0.75
            farm.safety.heal_below = 0.90
        elif any(root in zone for root in (
                "/hemlock_cave/", "/underground_city/",
                "/vielumin_ruins/", "/zechna_temple/",
                "/rockforge/")):
            # Source-level fit is not live survival proof. New autonomous
            # dungeon tiers begin with an early escape and near-full recovery
            # margin; empirical kill/death telemetry can justify relaxing a
            # specific tier later without exposing a fresh character to the
            # generic 55/75 defaults.
            farm.safety.flee_below = 0.80
            farm.safety.heal_below = 0.95
        elif "/old_outpost/" in zone:
            # Elemental golems are individually kiteable, but the entrance
            # geometry can wake more than one.  Keep a larger recovery margin
            # for controlled trials until the pocket has live pack evidence.
            farm.safety.flee_below = 0.80
            farm.safety.heal_below = 0.98
        elif zone.endswith(("/world_5_57", "/world_6_57")):
            farm.safety.flee_below = 0.75
            farm.safety.heal_below = 0.90
        task = NavigateThenTask(
            self.graph, zone, farm, combat_approach=True,
            safety=farm.safety)
        task.navigation.allow_ranged_hazard_fallback = (
            self.clear_hostile_route)
        return task

    @staticmethod
    def _food_reserve(client: AtrinikClient) -> int:
        patterns = SafetyPolicy().food_patterns
        return sum(item.quantity for item in client.state.inventory
                   if not item.flags & c.ITEM_UNPAID and (
                       item.item_type == c.TYPE_FOOD or
                       any(pattern in item.name.casefold()
                           for pattern in patterns)))

    def _needs_food_resupply(self, client: AtrinikClient) -> bool:
        return (self._food_reserve(client) <= 1 and
                int(client.state.stats.get("food", 1000) or 0) < 180 and
                self.clock() >= self._resupply_retry_at)

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
            needed = bool(self._unidentified_items(client))
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

    def _needs_apartment_bed(self, client: AtrinikClient) -> bool:
        apartment = (
            "/shattered_islands/strakewood_island/apartments/apartment_cheap")
        return (int(client.state.stats.get("level", 0) or 0) >= 1 and
                apartment in self.graph.nodes and
                not client.state.apartment_bed_bound and
                self.clock() >= self._bed_retry_at)

    def _new_apartment_bed(self) -> NavigateThenTask:
        return NavigateThenTask(
            self.graph,
            "/shattered_islands/strakewood_island/apartments/apartment_cheap",
            BindSavebedTask(), destination_xy=(4, 1), combat_approach=True)

    def _observe_disease(self, client: AtrinikClient) -> None:
        messages = client.state.messages
        for entry in messages[self._disease_message_index:]:
            text = (entry[3] if len(entry) > 3 else "").casefold()
            if "you are healed from disease" in text:
                self._disease_suspected = False
                self._cure_apply_at = 0.0
            elif any(marker in text for marker in (
                    "your illness seems less severe", "your feet itch",
                    "can't control your sphincter", "cannot control your sphincter",
                    "athelete's foot hit you", "athlete's foot hit you")):
                self._disease_suspected = True
        self._disease_message_index = len(messages)

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
                self.clock() >= self._restoration_retry_at)

    def _new_depletion_service(self, client: AtrinikClient) -> NavigateThenTask:
        task = NavigateThenTask(
            self.graph, "/shattered_islands/world_3_58",
            TempleServiceTask("Saruthar", "remove depletion", "depletion",
                              self._depletion_service_cost(client)),
            destination_xy=(13, 13), combat_approach=True)
        task.navigation.tolerance = 1
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

    def safe_for_progression_detour(self, client: AtrinikClient) -> bool:
        """Return whether autoplay may replace this circuit between pulls."""
        services = (
            self._resupply, self._storage, self._identification,
            self._bed_binding, self._selling, self._banking,
            self._bank_sync, self._shopping, self._recall_shopping,
            self._utility_shopping,
            self._ship_key,
            self._spell_purchase, self._cure, self._restoration,
            self._expedition_return, self._capability,
        )
        if any(service is not None for service in services) or self.child is None:
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
        if self._adaptive_early_progression:
            folded = target.casefold()
            if level < 11 and "lost soul" in folded:
                return False
            if "killer bee" in folded or "bee_killer" in folded:
                return 8 <= level < 10 and self._leg_available(index)
            if (level < 10 and path.endswith("/world_1_58") and
                    "skeleton" in folded):
                return False
            if (level < 10 and
                    path.startswith("/shattered_islands/world_7_") and
                    ("hill giant" in folded or "ogre" in folded)):
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
                    if purpose == InventoryCapabilityTask.PURPOSE_DISEASE:
                        self._disease_suspected = False
                    elif purpose == InventoryCapabilityTask.PURPOSE_DEPLETION:
                        setter = getattr(client, "set_depletion_points", None)
                        if setter is not None:
                            setter(0)
                else:
                    log.warning("%s maintenance capability failed: %s",
                                purpose, self._capability.error)
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
                    self._restoration_retry_at = self.clock() + 1800.0
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
            if self._bed_binding.status == TaskStatus.FAILED:
                log.warning("apartment savebed binding failed: %s",
                            self._bed_binding.error)
                self._bed_retry_at = self.clock() + 1800.0
                self._bed_binding = None
            elif self._bed_binding.status == TaskStatus.COMPLETE:
                log.info("apartment savebed bound; future deaths return home")
                self._bed_retry_at = self.clock() + 86400.0
                self._bed_binding = None
            return
        if self._resupply is not None:
            await self._resupply.tick(client)
            if self._resupply.status == TaskStatus.FAILED:
                log.warning("food resupply failed: %s", self._resupply.error)
                self._resupply_retry_at = self.clock() + 1800.0
                self._resupply = None
            elif self._resupply.status == TaskStatus.COMPLETE:
                log.info("food resupply complete; returning to farm circuit")
                self._resupply = None
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
            await client.clear_actions()
            await client.set_combat(False)
            self._resupply = self._new_food_resupply(client)
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
                self._needs_apartment_bed(client) and
                not self._busy_finishing_fight_or_loot(client, farm)):
            await client.clear_actions()
            await client.set_combat(False)
            self._bed_binding = self._new_apartment_bed()
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
