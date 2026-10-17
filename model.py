"""Character-independent live world state reconstructed from server packets."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class Character:
    archetype: str
    name: str
    region: str
    animation: int
    level: int


@dataclass(slots=True)
class Item:
    tag: int
    location: int = 0
    flags: int = 0
    weight: float = 0.0
    face: int = 0
    direction: int = 0
    item_type: int = 0
    subtype: int = 0
    quality: int = 255
    condition: int = 255
    required_level: int = 0
    required_skill_tag: int = 0
    name: str = ""
    animation: int = 0
    animation_speed: int = 0
    quantity: int = 1
    glow: str = ""
    glow_speed: int = 0
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class MapObject:
    layer: int
    face: int
    object_flags: int
    flags: int
    name: str = ""
    name_color: str = ""
    target_id: int = 0
    is_friend: bool = False
    target_hp: int = 0
    direction: int = 0
    # For animated map objects the wire ``face`` field is actually an
    # animation-table ID. Keep that ID explicit; the following packet byte is
    # animation speed, not another identifier.
    animation: int = 0
    animation_speed: int = 0


# Floor, floor mask and wall: the per-sub-layer kinds the client keeps in fog.
REMEMBERED_LAYER_KINDS = frozenset({0, 1, 4})


@dataclass(slots=True)
class Tile:
    darkness: list[int] = field(default_factory=lambda: [0] * 7)
    # Wire radiance is Q5.11, independent of target authority.
    support_height: int = 0
    objects: dict[int, MapObject] = field(default_factory=dict)
    fogged: bool = False

    @property
    def fog_of_war(self) -> bool:
        """Compatibility observation backed by the authority fog flag."""
        return self.fogged

    @fog_of_war.setter
    def fog_of_war(self, value: bool) -> None:
        self.fogged = bool(value)

    def remember_as_fog(self) -> None:
        """Keep remembered geometry and forget transient state, like the client.

        Classic's map_clear_live_cell() drops item, item2, living and effect
        layers but keeps floor, floor-mask and wall layers as fog-of-war
        memory. Wire layers are 7 * sub_layer + object_layer - 1, so geometry
        is layer % 7 in (0, 1, 4).
        """
        self.objects = {layer: obj for layer, obj in self.objects.items()
                        if layer % 7 in REMEMBERED_LAYER_KINDS}
        self.fogged = True

    @property
    def targets(self) -> list[MapObject]:
        if self.fogged:
            return []
        return [obj for obj in self.objects.values() if obj.target_id]


@dataclass(slots=True)
class MapState:
    name: str = ""
    path: str = ""
    raw_path: str = ""
    region: str = ""
    region_longname: str = ""
    music: str = ""
    weather: str = ""
    width: int = 17
    height: int = 17
    world_x: int = 0
    world_y: int = 0
    player_x: int = 8
    player_y: int = 8
    player_sub_layer: int = 0
    in_building: bool = False
    tiles: dict[tuple[int, int], Tile] = field(default_factory=dict)
    # MAP2 carries independently cached maps from six levels below through six
    # levels above the player's current map.  ``tiles`` remains the depth-zero
    # compatibility view used by the navigation policy.
    level_tiles: dict[int, dict[tuple[int, int], Tile]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if 0 in self.level_tiles:
            self.tiles = self.level_tiles[0]
        else:
            self.level_tiles[0] = self.tiles

    def tiles_at_depth(self, depth: int) -> dict[tuple[int, int], Tile]:
        if not -6 <= depth <= 6:
            raise ValueError("map depth outside MAP2 range")
        return self.level_tiles.setdefault(depth, {})

    def tile(self, x: int, y: int, depth: int = 0) -> Tile:
        return self.tiles_at_depth(depth).setdefault((x, y), Tile())

    def clear(self, x: int, y: int, depth: int = 0) -> None:
        self.tiles_at_depth(depth)[(x, y)] = Tile()

    def scroll(self, dx: int, dy: int) -> None:
        shifted: dict[tuple[int, int], Tile] = {}
        for depth, tiles in self.level_tiles.items():
            shifted: dict[tuple[int, int], Tile] = {}
            for (x, y), tile in tiles.items():
                nx, ny = x - dx, y - dy
                if 0 <= nx < self.width and 0 <= ny < self.height:
                    shifted[(nx, ny)] = tile
            self.level_tiles[depth] = shifted
        self.tiles = self.level_tiles.setdefault(0, {})

    def scroll_depths(self, zoff: int) -> None:
        shifted: dict[int, dict[tuple[int, int], Tile]] = {}
        for depth, tiles in self.level_tiles.items():
            new_depth = depth - zoff
            if -6 <= new_depth <= 6:
                shifted[new_depth] = tiles
        self.level_tiles = shifted
        self.tiles = self.level_tiles.setdefault(0, {})

    def targets(self, friendly: bool | None = None):
        for (x, y), tile in self.tiles.items():
            for obj in tile.targets:
                if friendly is None or obj.is_friend == friendly:
                    yield x, y, obj

    def depth_targets(self, friendly: bool | None = None):
        for depth, tiles in self.level_tiles.items():
            for (x, y), tile in tiles.items():
                for obj in tile.targets:
                    if friendly is None or obj.is_friend == friendly:
                        yield depth, x, y, obj


@dataclass(slots=True)
class InterfaceState:
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    input_text: str | None = None
    input_prepend: str = ""
    autocomplete: str = ""
    objects: list[Item] = field(default_factory=list)


@dataclass(slots=True)
class QuestPartProgress:
    name: str
    description: str = ""
    status: str = "active"
    current: int | None = None
    required: int | None = None


@dataclass(slots=True)
class QuestProgress:
    name: str
    status: str = "active"
    parts: list[QuestPartProgress] = field(default_factory=list)


@dataclass(slots=True)
class GameState:
    phase: str = "disconnected"
    account: str = ""
    connection_id: str = ""
    previous_connection_id: str = ""
    previous_connection_time: int = 0
    characters: list[Character] = field(default_factory=list)
    player_tag: int = 0
    player_name: str = ""
    # Constructed/test states are complete by default. The live client lowers
    # this at PLAYER login and raises it only after the server's authoritative
    # delete-and-replace inventory packet has been decoded atomically.
    inventory_replay_complete: bool = True
    stats: dict[str, int | float] = field(default_factory=dict)
    stat_observed_at: dict[str, float] = field(default_factory=dict)
    bank_balance: int = 0
    bank_balance_known: bool = False
    bank_balance_observed_at: float = 0.0
    last_bank_deposit_at: float = 0.0
    depletion_points: int = 0
    depletion_points_known: bool = False
    last_upgrade_shop_sweep_at: float = 0.0
    last_upgrade_shop_sweep_level: int = 0
    last_upgrade_shop_sweep_wallet: int = 0
    last_upgrade_shop_sweep_policy: int = 0
    active_upgrade_shop_sweep_policy: int = 0
    active_upgrade_shop_sweep_cursor: int = 0
    last_recall_shop_check_at: float = 0.0
    last_utility_shop_check_at: float = 0.0
    vendor_quotes: dict[str, dict[str, int | float | str]] = field(
        default_factory=dict)
    farm_zone_quarantine: dict[str, float] = field(default_factory=dict)
    # Which farming-analyzer ranking autoplay follows (see farm_analyzer.OBJECTIVES).
    farm_objective: str = "xp"
    farm_zone_last_checked: dict[str, float] = field(default_factory=dict)
    lore_attempt_counts: dict[str, int] = field(default_factory=dict)
    apartment_bed_bound: bool = False
    equipment: dict[int, int] = field(default_factory=dict)
    protections: dict[int, int] = field(default_factory=dict)
    items: dict[int, Item] = field(default_factory=dict)
    inventories: dict[int, list[int]] = field(default_factory=dict)
    map: MapState = field(default_factory=MapState)
    game_seconds: int = 0
    game_millis_per_minute: int = 0
    interface: InterfaceState | None = None
    messages: list[tuple[float, int, str, str]] = field(default_factory=list)
    books: list[bytes] = field(default_factory=list)
    quests: dict[str, QuestProgress] = field(default_factory=dict)
    quests_loaded: bool = False
    target_code: int = 0
    target_level: int = 0
    target_actor_count: int = 0
    target_relative_depth: int = 0
    target_id: int = 0
    target_color: str = ""
    target_name: str = ""
    combat: bool = False
    combat_force: bool = False
    party_name: str = ""
    server_version: int = 0
    last_packet_at: float = 0.0

    def add_message(self, msg_type: int, color: str, text: str) -> None:
        self.messages.append((time.time(), msg_type, color, text))
        del self.messages[:-500]

    @property
    def inventory(self) -> list[Item]:
        return [self.items[tag] for tag in self.inventories.get(self.player_tag, [])
                if tag in self.items]

    @property
    def ground(self) -> list[Item]:
        return [self.items[tag] for tag in self.inventories.get(0, [])
                if tag in self.items]

    def remove_item(self, tag: int) -> None:
        item = self.items.pop(tag, None)
        if item is not None and item.location in self.inventories:
            try:
                self.inventories[item.location].remove(tag)
            except ValueError:
                pass
        for children in self.inventories.values():
            if tag in children:
                children.remove(tag)
        for child in list(self.inventories.pop(tag, [])):
            self.remove_item(child)

    def place_item(self, item: Item, location: int, append: bool = True) -> None:
        old = self.items.get(item.tag)
        if old is not None and old.location in self.inventories:
            children = self.inventories[old.location]
            if item.tag in children:
                children.remove(item.tag)
        item.location = location
        self.items[item.tag] = item
        children = self.inventories.setdefault(location, [])
        if item.tag not in children:
            if append:
                children.append(item.tag)
            else:
                children.insert(0, item.tag)
