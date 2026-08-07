"""Curated farming routes derived from exact authored monster levels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .navigation import WorldGraph


@dataclass(frozen=True, slots=True)
class FarmSpot:
    id: str
    name: str
    min_level: int
    max_level: int
    zone: str
    target: str = ""
    category: str = "xp"
    notable_drops: tuple[str, ...] = ()
    notes: str = ""
    circuit: tuple[tuple[str, str], ...] = ()

    def as_dict(self, graph: "WorldGraph") -> dict:
        node = graph.nodes.get(self.zone)
        routes = self.circuit or ((self.zone, self.target),)
        monster_levels = []
        for zone, target in routes:
            if target:
                monster_levels.extend(
                    spawn.level for spawn in
                    graph.farm_priorities(zone, target) if spawn.level > 0)
            else:
                monster_levels.extend(
                    level for level in
                    graph.map_monster_levels.get(zone, {}).values()
                    if level > 0)
        monster_levels = sorted(set(monster_levels))
        roaming_levels = sorted({
            level for zone, _ in routes
            for level in graph.map_roaming_monster_levels.get(
                zone, {}).values() if level > 0
        })
        return {
            "id": self.id, "name": self.name,
            "min_level": self.min_level, "max_level": self.max_level,
            "zone": self.zone, "target": self.target,
            "category": self.category,
            # Difficulty controls generated treasure/traps, not combat fit.
            "treasure_difficulty": node.difficulty if node else None,
            "difficulty": node.difficulty if node else None,
            "monster_levels": monster_levels,
            "monster_level_min": min(monster_levels, default=None),
            "monster_level_max": max(monster_levels, default=None),
            "roaming_monster_levels": roaming_levels,
            "roaming_monster_level_min": min(roaming_levels, default=None),
            "roaming_monster_level_max": max(roaming_levels, default=None),
            "map_name": node.name if node else "",
            "notable_drops": list(self.notable_drops), "notes": self.notes,
            "circuit": [{"zone": zone, "target": target}
                        for zone, target in self.circuit],
        }


# The bands refer primarily to the active combat skill, matching the world
# report. Empty target patterns intentionally farm all hostile targets in the
# zone. Boss entries are loot routes, not necessarily efficient repeatable XP.
FARM_SPOTS: tuple[FarmSpot, ...] = (
    FarmSpot("incuna_barn", "Incuna Barn ants", 1, 4,
             "/shattered_islands/world_4_85_-2", "ant", notes=
             "Requires access through Arvend's barn during Lost Memories."),
    FarmSpot("forgotten_tunnels", "Forgotten Tunnels kobolds", 2, 6,
             "/shattered_islands/world_3_81_-1", "kobold", notes=
             "Rick Grizley's western-gate key is needed on the first visit."),
    FarmSpot("fahrgorm", "Fahrgorm", 5, 10,
             "/shattered_islands/world_3_68", "Fahrgorm", "boss",
             ("Bow of Accuracy — 1/25",),
             "Named surface treant; useful early bow loot route."),
    FarmSpot("thrakir", "Thrakir (night)", 6, 11,
             "/shattered_islands/world_3_69", "Thrakir|lost soul", "boss",
             ("Ring of the Ghost — 1/30",),
             "Lost souls spawn only 19:00–07:00; Thrakir is level 7."),
    FarmSpot("fahrgorm_thrakir", "Fahrgorm + Thrakir + passive wildlife circuit", 6, 13,
             "/shattered_islands/world_3_68",
             "Fahrgorm|evil treant|quickwood", "boss",
             ("Bow of Accuracy — 1/25", "Ring of the Ghost — 1/30"),
             "Prioritizes adjacent Thrakir and Fahrgorm rerolls and farms "
             "neutral level-7 giant wasps/bees, then six passive level-6/7 "
             "Eld Woods trees as a threat-free "
             "respawn filler. The adjacent mixed world_4_69 bear/wolf map is "
             "excluded from both farming and transit after a roaming wolf made "
             "a live rotation lethal; isolated world_5_69 is also excluded "
             "after its durable bears forced a retreat into a one-exit tree "
             "pocket and killed Sera. The misleading static ogre tier is "
             "excluded after migrated ettins and a stone giant made a live "
             "world_6_56 trial lethal; progression instead advances through "
             "audited passive Eld Woods trees, deer, giant slugs, and red ants "
             "while excluding ranged and caster-heavy zones.",
             (("/shattered_islands/world_3_69", "Thrakir|lost soul"),
              ("/shattered_islands/world_3_68",
               "Fahrgorm|evil treant"),
              ("/shattered_islands/world_3_69", "giant wasp|wasp_giant|wasp giant"),
              ("/shattered_islands/world_4_68",
               "killer bee|bee_killer|bee killer"),
              ("/shattered_islands/world_10_78",
               "evil treant|quickwood"))),
    FarmSpot("eld_passive_trees", "Eld Woods passive trees", 11, 17,
             "/shattered_islands/world_14_79",
             "Kotung|evil treant|quickwood", notes=
             "Three adjacent passive-only pockets cover authored levels "
             "11–17. Kotung is checked first from combat level 15 and has a "
             "1/40 chance to drop Crossbow Accuracy.",
             notable_drops=("Crossbow Accuracy — 1/40",),
             circuit=(("/shattered_islands/world_14_79",
                       "evil treant|quickwood"),
                      ("/shattered_islands/world_14_78",
                       "evil treant|quickwood"),
                      ("/shattered_islands/world_14_77",
                       "Kotung|evil treant|quickwood"))),
    FarmSpot("eld_mud_hands", "Eld Woods mud hands (unsafe pack)", 6, 10,
             "/shattered_islands/world_12_79", "mud hand", notes=
             "Historical Wizardry 6-8 candidate, now rejected for automatic "
             "use. Neutral alignment is not passivity; the corrected graph "
             "models the two maps as aggressive packs of six and seven.",
             circuit=(("/shattered_islands/world_12_79", "mud hand"),
                      ("/shattered_islands/world_12_78", "mud hand"))),
    FarmSpot("strakewood_neutrals", "Strakewood shoreline (rejected)", 18, 21,
             "/shattered_islands/world_4_51", "mud hand", notes=
             "Rejected automatic Slash tier. Neutral-aligned mud hands and "
             "frogs are still aggressive, and live crocodile/frog joins "
             "exhausted healing and caused a death.",
             circuit=(("/shattered_islands/world_4_51", "mud hand"),
                      ("/shattered_islands/world_3_51",
                       "mud hand|giant frog"),
                      ("/shattered_islands/world_3_50", "giant frog"))),
    FarmSpot("below_fort_sether", "Below Fort Sether", 19, 21,
             "/shattered_islands/world_4_51_-2",
             "spider|brown bat|sword spider", notes=
             "Rejected automatic tier. A live Slash-19 trial proved its "
             "nominally passive spider factions assist one another: at least "
             "two attacked simultaneously, exhausted healing, and killed "
             "Sera. Brownrott is correctly classified as a friendly quest "
             "NPC, but local neutral-alignment flags do not make this "
             "farm isolatable."),
    FarmSpot("eld_deer", "Eld Woods deer", 18, 22,
             "/shattered_islands/world_11_75", "deer", notes=
             "Nine passive authored level-19–21 deer. The route is deferred "
             "until combat level 18 so its level-16–18 crocodile transit is "
             "not attempted by an under-levelled character."),
    FarmSpot("eld_giant_slugs", "Eld Woods giant slugs", 22, 27,
             "/shattered_islands/world_13_73", "giant slug", notes=
             "Nine passive authored level-23–25 slugs; elevated retreat and "
             "healing margins account for their acid damage."),
    FarmSpot("eld_red_ants", "Eld Woods red ants", 25, 30,
             "/shattered_islands/world_14_73", "red ant", notes=
             "Seven passive authored level-25–27 ants provide the final "
             "audited low-risk ladder toward overall level 30."),
    FarmSpot("giant_foothills", "Giant Mountains foothills", 14, 18,
             "/shattered_islands/world_7_57", "hill giant|ogre", notes=
             "Two adjacent treasure-difficulty-8 edge tiles with five authored level-7 "
             "spawns, including three ranged rock throwers. A live level-9 "
             "trial exhausted full healing mana during retreat and was lethal; "
             "reserve this standalone spot for substantially stronger gear.",
             circuit=(("/shattered_islands/world_7_57", "hill giant|ogre"),
                      ("/shattered_islands/world_7_56", "hill giant|ogre"))),
    FarmSpot("giant_stronghold", "Giant Stronghold", 6, 10,
             "/shattered_islands/strakewood_island/giant_stronghold/giant_stronghold_0201",
             "giant", notes="Difficulty 6 outer stronghold."),
    FarmSpot("dark_cave", "Dark Cave", 10, 17,
             "/shattered_islands/strakewood_island/dark_cave/dark_cave_0102",
             notes="Entrance monsters are exactly level 10–12; the deeper "
             "level-13–15 boss room is represented by its boss entries. "
             "Fire protection becomes important there."),
    FarmSpot("yrchhhh", "Yrchhhh", 13, 20,
             "/shattered_islands/strakewood_island/dark_cave/dark_cave_0101",
             "Yrchhhh", "boss", ("Minor-shielding amulet — 1/50",),
             "Level-13 authored boss."),
    FarmSpot("fire_wyvern_lord", "Fire wyvern lord", 15, 22,
             "/shattered_islands/strakewood_island/dark_cave/dark_cave_0101",
             "fire wyvern lord", "boss",
             ("Fire-wyvern bracers — 1/80",),
             "Level-15 boss; bring fire protection."),
    FarmSpot("old_outpost_outer", "Old Outpost outer complex", 14, 20,
             "/shattered_islands/strakewood_island/old_outpost/old_outpost_0101"),
    FarmSpot("torathog", "Torathog", 18, 24,
             "/shattered_islands/strakewood_island/old_outpost/old_outpost_0202",
             "Torathog", "boss", notes="Level-19 Galann's Revenge target."),
    FarmSpot("old_outpost_labs", "Old Outpost laboratories", 20, 29,
             "/shattered_islands/strakewood_island/old_outpost/old_outpost_a_0201"),
    FarmSpot("king_rhun", "King Rhun's vault", 27, 35,
             "/shattered_islands/strakewood_island/old_outpost/old_outpost_a_0203",
             "King Rhun", "boss",
             ("Cloak of King Rhun — guaranteed personal drop",
              "Precision gauntlets — 1/80"),
             "Level-30 boss and the end of the Old Outpost ladder."),
    FarmSpot("hemlock_mid", "Hemlock Cave middle", 28, 39,
             "/shattered_islands/eld_woods_island/hemlock_cave/hemlock_cave_0103"),
    FarmSpot("underground_city_grid", "Underground City grid", 30, 42,
             "/shattered_islands/strakewood_island/underground_city/underground_city_0_1",
             notes="Large connected treasure-difficulty-35 grid; exact monster "
                   "levels determine when it is suitable."),
    FarmSpot("rockforge_mines", "Rockforge Mines", 38, 50,
             "/shattered_islands/strakewood_island/rockforge/rockforge_c_0101"),
    FarmSpot("vielumin_outer", "Vielumin Ruins outer", 40, 50,
             "/shattered_islands/eld_woods_island/vielumin_ruins/ruins_0201"),
    FarmSpot("underground_city_ii_48", "Underground City II — tier 48", 44, 54,
             "/shattered_islands/strakewood_island/underground_city/underground_city_4_4_-1"),
    FarmSpot("zechna_entrance", "Temple of Zechna entrance", 48, 58,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_0_0",
             notes="Entrance tier only; deeper maps jump sharply."),
    FarmSpot("vielumin_deep", "Vielumin Ruins deep", 50, 60,
             "/shattered_islands/eld_woods_island/vielumin_ruins/ruins_0202"),
    FarmSpot("underground_city_ii_55", "Underground City II — tier 55", 52, 62,
             "/shattered_islands/strakewood_island/underground_city/underground_city_5_2_-1"),
    FarmSpot("underground_city_ii_60", "Underground City II — tier 60", 57, 68,
             "/shattered_islands/strakewood_island/underground_city/underground_city_2_3_-1"),
    FarmSpot("zechna_surface_60", "Temple of Zechna — tier 60", 57, 68,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_1_0"),
    FarmSpot("underground_city_iii_65", "Underground City III — tier 65", 62, 72,
             "/shattered_islands/strakewood_island/underground_city/underground_city_1_3_-2"),
    FarmSpot("dragons_island_65", "Dragons Island outer", 62, 75,
             "/shattered_islands/world_4_71", "dragon", notes=
             "Elemental protections matter more than the surface geography suggests."),
    FarmSpot("bone_cairn", "Bone Cairn", 65, 78,
             "/planes/bone_cairn/bone_cairn_0_0", notes=
             "Optional branch reached from deep Underground City."),
    FarmSpot("kriabe", "Lom lobon Kriabe", 68, 78,
             "/shattered_islands/strakewood_island/underground_city/underground_city_5_3_-1",
             "lom lobon Kriabe", "boss",
             ("Drula's Gloves — guaranteed personal drop",
              "Kriabe's Boots — 1/160"),
             "Level-70 authored encounter despite the map's lower base difficulty."),
    FarmSpot("nyhelobo", "Nyhelobo", 70, 80,
             "/shattered_islands/strakewood_island/brynknot/sewers/lab_nyhelobo",
             "Nyhelobo", "boss", notes=
             "Level-72 Portal of Llwyfen finale; requires its sewer route."),
    FarmSpot("underground_city_iii_75", "Underground City III — tier 75", 72, 82,
             "/shattered_islands/strakewood_island/underground_city/underground_city_0_0_-2"),
    FarmSpot("underground_city_iii_80", "Underground City III — tier 80", 77, 87,
             "/shattered_islands/strakewood_island/underground_city/underground_city_1_0_-2"),
    FarmSpot("scursaur", "Scursaur", 78, 88,
             "/shattered_islands/world_6_72", "Scursaur", "boss",
             ("Smoking Pipe — guaranteed personal drop",
              "Crown of Xebinon — 1/100",
              "Ring of Greater Storm — 1/175"),
             "Level-80 dragon; compensate for electricity damage."),
    FarmSpot("underground_city_iii_85", "Underground City III — tier 85", 82, 92,
             "/shattered_islands/strakewood_island/underground_city/underground_city_1_-2_-2"),
    FarmSpot("zechna_tier_88", "Temple of Zechna — tier 88", 85, 95,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_1_0_-2"),
    FarmSpot("underground_city_iv_88", "Underground City IV — tier 88", 85, 95,
             "/shattered_islands/strakewood_island/underground_city/underground_city_4_0_-3"),
    FarmSpot("loki_temple", "Temple of Loki", 90, 102,
             "/shattered_islands/berri_lur/temple_loki/tol_0101", notes=
             "Treasure difficulty 90, with authored monsters extending above level 100."),
    FarmSpot("zechna_tier_95", "Temple of Zechna — tier 95", 92, 102,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_0_1_-2"),
    FarmSpot("underground_city_iv_95", "Underground City IV — tier 95", 92, 104,
             "/shattered_islands/strakewood_island/underground_city/underground_city_6_-1_-3"),
    FarmSpot("zechna_tier_100", "Temple of Zechna — tier 100", 97, 107,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_0_0_-2"),
    FarmSpot("underground_city_iv_105", "Underground City IV — tier 105", 102, 112,
             "/shattered_islands/strakewood_island/underground_city/underground_city_2_-1_-3"),
    FarmSpot("zechna_deep_105", "Temple of Zechna — deep tier 105", 102, 112,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_0_0_-3"),
    FarmSpot("underground_city_iv_110", "Underground City IV — tier 110", 107, 115,
             "/shattered_islands/strakewood_island/underground_city/underground_city_3_-1_-3"),
    FarmSpot("zechna", "Zechna", 108, 115,
             "/shattered_islands/strakewood_island/zechna_temple/zechna_temple_1_0_-3",
             "Zechna", "boss", ("Zechna's Glowing Crystal — 1/200",),
             "Level-110 endgame boss; this is a loot route, not safe AFK XP."),
)


def farm_spot_catalog(graph: "WorldGraph") -> list[dict]:
    return [spot.as_dict(graph) for spot in FARM_SPOTS]
