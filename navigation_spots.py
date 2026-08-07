"""Curated travel destinations derived from Atrinik's authored world maps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .navigation import WorldGraph


@dataclass(frozen=True, slots=True)
class NavigationSpot:
    id: str
    name: str
    category: str
    destination: str
    x: int | None = None
    y: int | None = None
    notes: str = ""
    requirements: tuple[str, ...] = ()

    def as_dict(self, graph: "WorldGraph") -> dict:
        node = graph.nodes.get(self.destination)
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "destination": self.destination,
            "x": self.x,
            "y": self.y,
            "map_name": node.name if node else "",
            "region": node.region if node else "",
            "difficulty": node.difficulty if node else None,
            "notes": self.notes,
            "requirements": list(self.requirements),
        }


# Coordinates are included only when a useful walkable square is known. A
# map-only destination deliberately completes on entry rather than guessing a
# potentially occupied NPC, door, or counter tile.
NAVIGATION_SPOTS: tuple[NavigationSpot, ...] = (
    NavigationSpot("incuna_docks", "Incuna docks", "town",
                   "/shattered_islands/world_4_85", 7, 13,
                   "Arrival point from Sam's Deserted Island voyage."),
    NavigationSpot("incuna_sam", "Incuna — dockside Sam", "quest",
                   "/shattered_islands/world_4_85", 7, 13,
                   "Lost Memories quest giver; safe gangway interaction square."),
    NavigationSpot("incuna_strakewood_ship",
                   "Incuna — ship to Strakewood", "transport",
                   "/shattered_islands/world_0_83", 8, 17,
                   "Later voyage captain; this Sam does not start Lost Memories."),
    NavigationSpot("incuna_bank", "Incuna bank district", "service",
                   "/shattered_islands/world_3_83",
                   notes="Bank guards and bank chests are authored here."),
    NavigationSpot("incuna_church", "Incuna church", "quest",
                   "/shattered_islands/world_4_84",
                   notes="Brelend Lee, Ken Berger, and early Lost Memories objectives."),
    NavigationSpot("incuna_mines", "Forgotten Tunnels", "dungeon",
                   "/shattered_islands/world_3_81_-1",
                   notes="Lost Memories kobold route.",
                   requirements=("Rick Grizley's western-gate key on the first visit",)),

    NavigationSpot("brynknot_center", "Brynknot center", "town",
                   "/shattered_islands/world_1_67",
                   notes="Main early-game hub; tavern and several quest NPCs."),
    NavigationSpot("brynknot_bank", "Brynknot bank", "service",
                   "/shattered_islands/world_0_67",
                   notes="Banks Inc. guards and chests."),
    NavigationSpot("brynknot_apartments", "Brynknot apartments", "service",
                   "/shattered_islands/world_0_68",
                   notes="Apartment entrance and access to the civic depths."),
    NavigationSpot("brynknot_library", "Brynknot library district", "quest",
                   "/shattered_islands/world_2_68",
                   notes="Lairwenn and the route for her missing notes."),
    NavigationSpot("brynknot_sewers", "Brynknot sewers", "dungeon",
                   "/shattered_islands/world_0_68_1",
                   notes="Talthor Redeye and the Portal of Llwyfen route."),
    NavigationSpot("nyhelobo_lab", "Nyhelobo's laboratory", "boss",
                   "/shattered_islands/strakewood_island/brynknot/sewers/lab_nyhelobo",
                   notes="Level-72 Portal of Llwyfen finale.",
                   requirements=("Portal quest access and sewer maze key",)),

    NavigationSpot("asteria_docks", "Asteria docks", "transport",
                   "/shattered_islands/world_8_41",
                   notes="Northern ship and dockhouse district."),
    NavigationSpot("asteria_market", "Asteria auction house", "service",
                   "/shattered_islands/world_9_44",
                   notes="Auction and central market district."),
    NavigationSpot("asteria_shops", "Asteria equipment shops", "service",
                   "/shattered_islands/world_5_46",
                   notes="Bow and armour shop district."),
    NavigationSpot("asteria_apartments", "Asteria apartments", "service",
                   "/shattered_islands/world_4_44"),
    NavigationSpot("charob_brewery", "Charob brewery", "quest",
                   "/shattered_islands/world_5_48",
                   notes="Steve Bruck and the beer shipment chest."),

    NavigationSpot("aris", "Aris", "town",
                   "/shattered_islands/world_5_58",
                   notes="Southern Strakewood shops and quest routes."),
    NavigationSpot("fort_sether", "Fort Sether", "town",
                   "/shattered_islands/world_4_53",
                   notes="Fort Sether Illness quest hub."),
    NavigationSpot("centennial", "Centennial", "town",
                   "/shattered_islands/world_0_52",
                   notes="Central Strakewood settlement and potion shop."),
    NavigationSpot("greyton", "Greyton", "town",
                   "/shattered_islands/world_9_69",
                   notes="Eastern city; apartments and access to civic services."),
    NavigationSpot("greyton_houses", "Greyton house agency", "service",
                   "/shattered_islands/world_10_69",
                   notes="House ownership and agency district."),
    NavigationSpot("portu_baserrian", "Portu Baserrian", "town",
                   "/shattered_islands/world_12_64",
                   notes="Berri Lur port and bank guards."),
    NavigationSpot("clearhaven", "Clearhaven", "town",
                   "/shattered_islands/world_7_79",
                   notes="Eld Woods settlement and inn."),

    NavigationSpot("giant_stronghold", "Giant Stronghold entrance", "dungeon",
                   "/shattered_islands/strakewood_island/giant_stronghold/giant_stronghold_0201"),
    NavigationSpot("dark_cave", "Dark Cave entrance", "dungeon",
                   "/shattered_islands/strakewood_island/dark_cave/dark_cave_0101",
                   notes="Bring fire protection for the deeper encounters."),
    NavigationSpot("old_outpost", "Old Outpost entrance", "dungeon",
                   "/shattered_islands/strakewood_island/old_outpost/old_outpost_0101"),
    NavigationSpot("king_rhun", "King Rhun's vault", "boss",
                   "/shattered_islands/strakewood_island/old_outpost/old_outpost_a_0203",
                   requirements=("Old Outpost progression and keys",)),
    NavigationSpot("underground_city", "Underground City entrance grid", "dungeon",
                   "/shattered_islands/strakewood_island/underground_city/underground_city_0_1",
                   notes="Difficulty-35 main grid reached through Brynknot's portal."),
    NavigationSpot("rockforge", "Rockforge Mines", "dungeon",
                   "/shattered_islands/strakewood_island/rockforge/rockforge_c_0101"),
    NavigationSpot("zechna_entrance", "Temple of Zechna entrance", "dungeon",
                   "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_0_0",
                   notes="Entrance tier; deeper temple maps rise sharply in difficulty."),
    NavigationSpot("zechna", "Zechna's chamber", "boss",
                   "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_1_0_-3",
                   notes="Level-110 endgame encounter.",
                   requirements=("Deep Temple of Zechna route",)),
    NavigationSpot("hemlock_cave", "Hemlock Cave", "dungeon",
                   "/shattered_islands/eld_woods_island/hemlock_cave/hemlock_cave_0103"),
    NavigationSpot("vielumin_ruins", "Vielumin Ruins", "dungeon",
                   "/shattered_islands/eld_woods_island/vielumin_ruins/ruins_0201"),
    NavigationSpot("bone_cairn", "Bone Cairn", "dungeon",
                   "/planes/bone_cairn/bone_cairn_0_0",
                   requirements=("Deep Underground City portal",)),
    NavigationSpot("loki_temple", "Temple of Loki", "dungeon",
                   "/shattered_islands/berri_lur/temple_loki/tol_0101",
                   notes="Endgame dungeon despite its accessible surface island."),
    NavigationSpot("dragons_island", "Dragons Island", "dungeon",
                   "/shattered_islands/world_4_71",
                   notes="High-level elemental dragon region."),
    NavigationSpot("scursaur", "Scursaur's region", "boss",
                   "/shattered_islands/world_6_72",
                   notes="Level-80 dragon encounter."),
)


def navigation_spot_catalog(graph: "WorldGraph") -> list[dict]:
    return [spot.as_dict(graph) for spot in NAVIGATION_SPOTS]
