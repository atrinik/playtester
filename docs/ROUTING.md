# Routing: alternative routes, unlocks and learned costs

Status: design proposal, not implemented. Nothing here changes behaviour
until the phased pull requests at the end land.

The owner wants the navigator to reach a place another way when one route is
dangerous. That includes the world's shortcuts: one-way and two-way ones,
locked ones, ships and NPC voyages, teleporters. Examples are Morg'eean's
ship between Asteria and Brynknot, the entrances to the Underground City and
the Old Outpost. She should always take the safest route her unlocks allow,
and learn from her own experience when terrain or monsters block the way.

The owner chose the model: **a map-derived graph with learned costs**.

- The content graph says which routes exist and what each one requires.
- Only routes she has unlocked are offered.
- Every edge and area carries a cost learned from her own experience, stored
  in her memory database. The cost decays over time.
- She takes the cheapest available route. A route that keeps failing gets
  more expensive until an alternative wins.

This document first records what the navigator does today, then the gaps
against that model, then the design and a plan of small pull requests.

## How the evidence was gathered

Every claim about content behaviour below was checked, not inferred from
names:

- Source: this repository at `995685d` (origin/main). Line numbers refer to
  that revision.
- Content: the locked content input, `atrinik/content` v1.14.0 at
  `49f5aab`, from `dependencies.lock.json`. The world graph was built offline
  from that tree with this revision's `WorldGraph.build()` (3,660 maps,
  16,814 edges: 15,334 tile seams and 1,480 exits), then queried with
  `route_points()` and `NavigateTask._plan()`.
- Server behaviour: Classic server source (`server/src/types/check_inv.c`,
  `server/src/server/map.c`).
- Live state: one read-only `GET /api/state` of the running bot (build
  v1.64.0, `479efb5`) on 2026-11-14, for the incident's decisions,
  post-mortems, inventory and quest journal.

The probe scripts are not part of this change. Where something is
unverified, the text says so.

## 1. Inventory: what the navigator does today

### 1.1 Graph building (`WorldGraph.build`)

`WorldGraph.build()` (`navigation.py:443-946`) parses every map file once.
The graph is a set of `MapNode`s (`navigation.py:234-276`), each with
directed `MapEdge`s (`navigation.py:221-231`: `source`, `destination`,
departure `x, y`, arrival `destination_x, destination_y`, `kind` of `"tile"`
or `"exit"`, `label`, `automatic`).

Edge sources:

- **Declared tile paths.** `_add_declared_tiles` (`navigation.py:1051-1067`)
  turns `tile_path_1..10` header attributes into `kind="tile"` edges at a
  nominal border point.
- **Coordinate seams.** `_coordinate_name` (`navigation.py:331-345`) parses
  `prefix_X_Y[_Z]` basenames as `server/map.c` does. `_add_coordinate_tiles`
  (`navigation.py:1069-1100`) adds the eight horizontal neighbours that exist.
  Vertical neighbours are never invented; they need an authored stair.
- **Type-66 exits.** An exit with a `slaying` destination becomes a
  `kind="exit"` edge with its authored `hp/sp` arrival
  (`navigation.py:820-826`). `walk_on`/`fly_on` mark it `automatic`.
- **Scripted exit suffixes.** If an exit's event script appends a literal to
  `me.slaying` (Portal of Llwyfen's Nyhelobo variant), the suffixed map is
  added as an alternative edge (`navigation.py:827-859`). Quest state, which
  decides the real destination, is not modelled.
- **Destination-less stairs.** `last_heal` 9/10 selects the coordinate map
  above or below (`navigation.py:860-874`).
- **Auto-connected exits.** Other destination-less exits are paired within
  five tiles by type and subtype, as `exit_find()` does
  (`navigation.py:875-883`, `896-907`; 470 such edges). Shop mats crossing a
  counter use this.
- **Apartments.** `_apartment_destinations` (`navigation.py:348-374`) reads
  `python/Apartments.py` with `ast`, without running it. Entrance exits
  scripted with `apartment_teleport.py` get an edge to every tier of the
  region (`navigation.py:780-807`); `apartment_out.py` exits get an edge back
  to every entrance (`navigation.py:808-818`, `884-894`). Routing enters an
  apartment only when it is the destination (`navigation.py:1392-1399`).
- **Filtering and ordering.** Edges to maps that do not exist, and seams whose
  border square is not walkable, are dropped (`navigation.py:909-920`).
  Walk-on exits sort first (`navigation.py:921-923`) and are kept out of
  ordinary walking as `automatic_exits` (`navigation.py:924-932`).

Tile walkability comes from `terrain_type` bit 1, `no_pass` objects other
than doors, checkers and exits, and multipart footprints
(`navigation.py:560-586`). Living spawns are `occupied`: walkable, but never
an exact seam arrival (`navigation.py:582-588`). Spawn levels, casters,
peaceful identities, named spawns with `aggro_radius` and schedules, savebeds,
shop stock and dialogue vendors are indexed in the same pass.

### 1.2 Locked doors and inventory checkers

`MapNode` has `locked`, `lock_requirements` and `lock_exclusions`
(`navigation.py:244-247`). `lock_allowed` (`navigation.py:255-269`) checks
one locked tile against an access set: `True` opens everything, an empty set
opens nothing, otherwise the tile's requirement must be in the set and its
exclusion must not.

- **Locked doors** (type 20): a door whose archetype name contains "locked"
  and has a `slaying`, or one with `locked 1`, is locked with that
  requirement (`navigation.py:705-711`). One Ogre Guard passage keeps its key
  id on a colocated conditional `magic_mouth`; `conditional_access` reads it
  (`navigation.py:515-531`, `698-704`).
- **Inventory checkers** (type 64) with a `slaying` and `last_grace 1` are
  locked tiles. `sp == "0"` makes the slaying an exclusion, anything else a
  requirement (`navigation.py:712-721`).
- **`access_item_names`** (`navigation.py:722-731`) maps each lock id to the
  names of key objects authored in map files with that `slaying`. For
  `set_individual_value` keys, whose slaying a pickup script sets, the key's
  own name is the id.
- **`_allow_locked`** (`navigation.py:1700-1708`) builds her access set from
  the top-level inventory names that match `access_item_names`. A task can
  override it (`allow_locked=True` is used once, `catalog_quest_tasks.py:988`).
  `ACCESS_ITEM` (`navigation.py:1594`) is defined but unused.

### 1.3 How keys and unlocks are known

Only from inventory item names, as in 1.2. Nothing reads the quest journal
for routing: `client.state.quests` (parsed by `quests.parse_quest_book`,
`quests.py:143-182`) drives quest executors, not edges. Nothing remembers a
route that worked. The ship key has a dedicated acquisition task,
`MorgeeanShipKeyTask` (`navigation.py:3848-3920`), queued from level 8 when
she lacks it (`navigation.py:6402-6406`).

### 1.4 Ships and NPC transport

- **Morg'eean's ship.** The "enchanted" ship is a static map,
  `/shattered_islands/ships/ship_2`, with gangway stairs to Brynknot's dock
  (`world_2_70`, 5,12), Asteria/Morg'eean's yard (`world_6_51`, 16,6),
  Morliana (`world_15_4`) and Lyondale (`world_18_3`). The deck splits into
  two walkable halves: Brynknot and Asteria share one, Morliana and Lyondale
  the other. There is no dialogue or script in the crossing: the ordinary
  graph already models it. Morg'eean's dialogue
  (`interfaces/strakewood_island/morgeean.xml`) only gives permission; the
  key opens a `celestial_gate1_locked` gate with a colocated `check_inv`
  (`morgeean_ship_key`) on both docks.
  Verified: with the key, Brynknot `world_2_69` to `world_6_51` is 3
  transitions (`world_2_70 > ship_2 > world_6_51`); without it, 28 surface
  maps. Brynknot general store to Asteria armour is 10 transitions with the
  key and 35 without. Without the key the Brynknot dock is a sealed 16-tile
  component; on the Asteria side the static model reaches the gangway
  without passing the gate. Whether that matches the live map is unverified.
- **NPC voyages (Sam Goodberry).** These are dialogue teleports:
  `<action teleport="map x y [savebed]"/>` in interface XML attached to an
  NPC. Seven exist in content. The graph models none of them:
  - Incuna to Brynknot: Sam's Lost Memories interface with
    `state="completed"` teleports to the ocean map `world_0_83` (8,17), whose
    stairs lead below deck and out at Brynknot `world_0_70`. From
    `world_0_83` the graph already routes to `world_0_70` (2 transitions);
    only the dialogue hop is missing, so Incuna `world_4_85` to Brynknot is
    unroutable today, and so is the way back.
  - Brynknot back to Incuna: Sam on `world_0_70`
    (`interfaces/strakewood_island/brynknot/sam_goodberry.xml`) teleports to
    `world_2_87` (5,6), then Sam there to the Incuna lower deck.
  - The Deserted Island voyages, two of them with `savebed`, which also moves
    her respawn point.
  The bot handles the scripted intro voyage with a bespoke task,
  `LostMemoriesArrivalTask` (`catalog_quest_tasks.py:377-430`), whose
  docstring says the ocean map is "deliberately disconnected from the static
  world graph".
- **Other scripted teleports.** Twelve map Python files call `TeleportTo`
  (guilds, houses, jail, the arena exit, apartments, the lake and Nyhelobo
  portals). Only the apartment scripts are modelled.
- **Ship 1.** `ships/ship_1` links six pairs of ports, Brynknot's south dock
  `world_0_70` (20,16) with `world_6_78` among them. These are ordinary
  edges.

### 1.5 One-way edges

Edges are directed and `route_points` searches directed component states, so
an exit with no reverse is already one-way. 303 map pairs have no reverse
edge, carried by 73 exit and 272 seam edges. The seams were not checked one
by one; a border walkable on one side only would explain them. One example
is the Brynknot sewers `lab` exit into the Underground City.

There is no notion of a direction-dependent lock: a door "not possible to
open from this side" (`underground_city_5_2_-1`) is one undirected locked
tile. Time-limited forces granted by markers are not modelled either; see
G9.

### 1.6 Route planning and cost

- **Component routing.** `route_points` (`navigation.py:1342-1440`) is the
  planner every trip uses. A state is a (map, static walkable component),
  found with `_component` (`navigation.py:1177-1226`, native
  `grid_reachable`). Tile seams resolve to one crossing per destination
  component (`_tile_transitions`, `navigation.py:1228-1283`); exits must be
  reachable by `local_path` (`_transition`, `navigation.py:1285-1340`). The
  graph is then searched with native `graph_search`.
- **Cost.** Every transition costs 1. With `penalized_maps`, entering one of
  them costs `1 + (states + 1)`, so the route enters as few as possible, then
  takes the fewest transitions (`navigation.py:1428-1435`). Walking distance
  inside a map is not counted: a 60-tile walk across one map costs the same
  as one step through a doorway.
- **Map-level routing.** `route()` (`navigation.py:1535-1578`) searches
  map-to-map and ignores components and locks. It serves
  `routes_into_component` (`navigation.py:1501-1533`) and the savebed
  shortlist `_nearest_spot_maps` (`navigation.py:4304-4319`).
- **Native core.** `pathfinding.py` wraps the pinned libatrinik pathfinding
  release. `graph_search` (`pathfinding.py:76-92`) accepts `uint64` per-edge
  costs and runs Dijkstra when costs are given, breadth-first otherwise
  (`native/pathfinding_module.c:379`, `601-625`). Overflow is reported as
  `"cost overflow"` (`test_pathfinding.py:104-110`). `grid_search` accepts a
  per-tile `uint8` cost grid (`pathfinding.py:50-62`), which `local_path`
  does not use yet. Learned costs therefore need no native change.
- **Tile walks.** `_segment_steps` and `_route_walk`
  (`navigation.py:4344-4391`) estimate the tiles walked along a planned route.
  Only savebed selection uses them.

### 1.7 Hazard tiers in `_plan`

`NavigateTask._plan` (`navigation.py:1795-1810`):

- **Learned hazards** (`_learned_route_hazards`, `navigation.py:1789-1793`)
  are `penalized_maps`: "a cost, never a wall". Leaving the map she stands on
  is free; re-entering it is not.
- **`_plan_avoiding`** (`navigation.py:1857-1956`) avoids content-derived
  hazards. Ranged and caster transit maps are avoided below level 18
  (`navigation.py:1717-1734`); maps with spawns `level + max(3, (level+4)//5)`
  or more above her (`navigation.py:1736-1755`); open-world and tiled-level
  `world_*` maps where three or more aggressors join (`navigation.py:1760-1787`,
  regexes at `1757-1758`); `KNOWN_LETHAL_TRANSIT` (`navigation.py:1598-1600`,
  one map). It relaxes in tiers: tiled levels, then over-level maps, then, if
  `allow_ranged_hazard_fallback`, ranged and pack maps. A relaxed hazard joins
  the penalized set.
- **`_plan_strict`** (`navigation.py:1812-1855`) avoids every hazard with no
  fallback. Economy and cure trips use it (`TOWN_SERVICES`,
  `navigation.py:4019-4025`).
- **Level-18 fallback.** `FarmCircuitTask._retry_unavoidable_return_route`
  (`navigation.py:5172-5197`) retries a failed farm trip once with
  `allow_ranged_hazard_fallback` when the combat level is 18 or more.

The pack estimate (`transit_max_aggro_pack`) only rates `world_*` maps.
Dungeon maps are never pack hazards, however dense: Underground City
`underground_city_0_0` has an eight-monster aggro pack.

### 1.8 Runtime exclusions (one trip, in memory)

- `_exclude_edge` (`navigation.py:1622-1632`) excludes one exit, or a whole
  seam, for 75 s, doubling on each repeat up to 300 s
  (`navigation.py:1609-1610`). The callers are a failed click path
  (`navigation.py:2696-2697`), a seam with no remaining crossing
  (`navigation.py:2570-2580`) and an exit held by a pack for 15 s
  (`NavigateThenTask._bypass_pack_blocked_exit`, `navigation.py:2769-2800`).
- `_failed_tile_crossings` (`navigation.py:1673`, filled at `2519`) holds
  seam crossings that stalled, so `_retry_stalled_tile_crossing`
  (`navigation.py:2450-2595`) tries another row or column.
- `_rejected_departures` (`navigation.py:1677`, `2093-2124`) holds seam
  departure squares that rejected a step, with the same expiry rule.
- `_runtime_blocked`, `_threat_blocked` and `_temporary_blocked` are tile sets
  for learned stall blockers, threat halos and visible occupants
  (`_movement_blockers`, `navigation.py:2067-2091`). They clear on a map
  change (`_record_progress`, `navigation.py:2041-2051`).

None of this persists, or reaches another trip or task.

### 1.9 Learned hazards and memory persistence

`memory.py` (`BotMemory`) is one SQLite file with a `memory` key/value table
keyed by (server, account, character, kind), plus `metadata` and `chat_log`
(`memory.py:30-68`). The kinds in use are `state`, `decisions`,
`post_mortems` and `trends` (`client.py:315-348`).

Route learning today is one dictionary in the `state` record:
`route_hazards: {map path: expiry wall time}`. It is loaded at
`client.py:411-415`, kept in `GameState.route_hazards` (`model.py:257`) and
written by `remember_route_hazard` (`client.py:626-640`). Only
`FarmCircuitTask._learn_from_failure` writes it (`navigation.py:4903-4917`):
after a farm-trip death that the review classifies as `route`, when she took
damage on the death map, that map is a hazard for `ROUTE_HAZARD_SECONDS`
(6 h, `navigation.py:4598`). There is no gradation, no count, no edge or area
key, no travel time and no success signal. `state` is one JSON blob rewritten
on every save.

Related farm memory: `farm_adaptations`, with per-kit `kit_failures`, feeds
`self_improvement.kit_hazard` (`self_improvement.py:285-312`).
`farm_zone_quarantine` maps quarantined spot maps to their expiry.

A separate pull request, in progress, makes route deaths mark the transit map
for farm selection, lets recall work while travelling, and keeps hazards when
her kit gets weaker. This design builds on it and does not repeat it.

### 1.10 Farming and the planner

- `analyzer_farm_choice` (`navigation.py:4134-4193`) takes the analyzer's
  ranking and skips quarantined and kit-hazardous spots. While she is
  depleted, it also skips spots whose maps cannot reach every town service on
  a strict route (`town_safely_reachable`, `navigation.py:4055-4103`). The
  only route check is whether a route exists. Its cost and danger are never
  compared.
- `_observe_farm_death` (`navigation.py:4781-4834`) blames a death on the way
  on the requested destination. `_learn_from_failure`
  (`navigation.py:4869-4968`) adapts margins, pauses the spot for an hour at
  the maximum level after repeated failures, and records route hazards as in
  1.9.
- Savebed selection routes with `route_points` but no access set
  (`navigation.py:4430`), so keyed shortcuts such as the ship do not count.

### 1.11 Dashboard and metrics

The Tasks view shows the active `NavigateTask`: remaining edges, current edge,
runtime, threat and occupant blockers, threat maps, the fallback flag and
failed seam crossings (`web_server.py:364-415`). Learned route hazards, the
access set and per-edge costs are not shown. Prometheus has
`deaths_total{cause}`, `state_seconds_total{state="travelling"}` and
`actions_total{kind}`, whose kinds include `route-hazard-learned`,
`navigation-threat-fallback` and `navigation-seam-excluded`. No series
describes routes.

## 2. The 2026-11-14 incident

From the live decision log and post-mortems (all times UTC, all on the way
to the analyzer's Underground City circuit at `underground_city_0_4`):

| UTC | Event |
| --- | --- |
| 20:34:13 | death in `old_outpost_0101`, cause `resources` |
| 20:38:44 | death on `underground_city_0_0` at (19,1), cause `pack` |
| 20:40:32 | death on `underground_city_0_0` at (19,4), cause `pack` (puddings, jellies, orcs, poison) |
| 20:42:29 | death on `underground_city_0_2` at (17,16), cause `pack`; hazard learned for 6 h (364 damage from orcs); review `route`; adaptation level 4 |
| 20:42:31 | `farm-return-threat-fallback` (combat level 34), then `navigation-threat-fallback` to `underground_city_0_4` |
| 20:45:24 | death on `underground_city_0_0`; hazard learned |
| 20:47:07 | death on `underground_city_0_0`; spot paused for an hour after repeated failures at the maximum level |
| 20:47:12 | Burnt Forest circuit selected |

That is five deaths in nine minutes in the Underground City, four of them on
`underground_city_0_0`. Two more `underground_city_0_0` deaths are on record
from 2026-11-07, at (19,2) and (21,1). Every `underground_city_0_0` death
with a recorded position is between x 19-21 and y 1-4, the map's north-east,
about 18 tiles from the arrival stairs at (1,1). The 20:45 and 20:47
post-mortems were not among the reports the dashboard returned.

What the graph says, with her actual access set
(`morgeean_ship_key`, `forgotten_tunnels_kobold_key`):

- **`underground_city_0_0` is the only way into the Underground City's upper
  halls.** From Brynknot the route is 9 surface maps to `world_5_65`, the
  stairs down into `underground_city_0_0`, then south through `0_1`, `0_2`,
  `0_3` to `0_4`. With `underground_city_0_0` avoided, no route exists.
  Penalizing it, the "cost, never a wall" rule, cannot change the route,
  because there is no alternative to prefer.
- **The Brynknot entrances do not lead into the halls.** `world_2_69`
  (0,6) and the sewers `lab` (11,12) are both in the graph. Both arrive at
  (23,13) in `underground_city_0_1`, which is an 11-tile walled pocket
  (Dunsor's corpse) cut off from the halls. Its only exit is the green lake
  portal (22,12). That portal's script, `scripts/lake_portal.py`, bounces the
  player unless Portal of Llwyfen's `portal_found` part is complete, and the
  graph treats it as an ordinary exit. The portal lands on a single-tile
  component at `world_2_69` (0,7), surrounded by water. So, statically, the
  Brynknot side is a loop, not a way in. Sera has not done Portal of
  Llwyfen.
- **Real alternatives need unlocks she lacks.** With every lock open, the
  drow entrance `world_4_67_-1` (7,17) to `underground_city_5_2_-1` reaches
  `0_4` in 14 transitions without `0_0`. The minimum access is
  `underground_city_key_drow`, `underground_city_force_drow` and
  `underground_city_key1`. The drow key is a quest reward inside
  `underground_city_5_2_-1`; its description calls it a one-way passage. The
  force is granted by a `marker` as she walks through, and the door is "not
  possible to open from this side". The yellow portal from `world_6_67_-1`
  and the Bone Cairn plane give lower-level entrances.
- **Inside, one alternative exists but is blocked.** With `0_0` and `0_2`
  penalized, the planner switches to the `1_x` column
  (`0_0 > 1_0 > 1_1 > ... > 1_4 > 0_4`). At level 34, though, `1_1` holds a
  level-49 spawn, an over-level hazard that is avoided before learned
  penalties are weighed.

The conclusion is the most important input to this design. **For this
incident, a better graph alone would not have saved her.** With her unlocks,
every route to that spot crosses `underground_city_0_0`. What was missing was
a route cost that the trip and the spot selection could refuse. "Only route
is lethal" should mean "do not go", not "go anyway, cheapest". The design
keeps "a cost, never a wall" for choosing between routes, and adds a
**budget** that a trip's cheapest route must fit.

## 3. Gaps against the owner's model

Each gap names the content that shows it.

- **G1. Route cost ignores time.** Every transition costs 1 (1.6), so a route
  through many small maps can lose to one long walk across a single large
  map. The ship (3 transitions against 28) wins anyway, but the planner
  cannot compare walking time with risk, and cannot learn that a route is
  slow.
- **G2. No refusal budget.** Learned hazards only reorder routes. When every
  route crosses a learned hazard, the trip goes ahead (section 2). Farm
  selection never compares route danger with what the spot is worth.
- **G3. Learned memory is one bit per map with a fixed expiry.** Five deaths
  and one death look the same. Damage without death, emergency recalls,
  blockers, failed crossings and slow traversals are not remembered. There
  are no edge or area keys, though deaths cluster in parts of a map (every
  located `underground_city_0_0` death is in its north-east), and no success
  signal to clear a hazard sooner.
- **G4. Dungeons are never pack hazards.** The prior pack estimate only rates
  `world_*` maps (1.7), so `underground_city_0_0` (pack 8) has no prior cost
  until she dies there.
- **G5. Quest-gated edges are not modelled.** The lake portal needs Portal of
  Llwyfen `portal_found`. Sam's Incuna voyage needs Lost Memories completed
  (`<interface npc="Sam Goodberry" state="completed">`). The Nyhelobo exit
  suffix depends on quest state (1.1). The journal is parsed but never read
  for routing.
- **G6. Dialogue transport is missing.** The seven `<action teleport>`
  voyages are not edges (1.4), so Incuna to Brynknot is unroutable, and the
  intro voyage needs a bespoke task. Teleports with `savebed` also move her
  respawn point, a side effect the planner must know about.
- **G7. Keys given by quests or NPCs are invisible.** `access_item_names`
  only knows keys placed in map files. Sera holds "Incuna Western Gate Key"
  and "Incuna Tavern Barn Key", both rewarded by Lost Memories dialogue from
  `incuna/artifacts.art`, but their lock ids (`incuna_western_gate_key`,
  `incuna_tavern_barn`) have no known key item. Her computed access set is
  `{morgeean_ship_key, forgotten_tunnels_kobold_key}`; she also holds the Blue
  Crystal Talisman. 24 of the 58 lock ids in the graph have no known key
  item, among them Underground City `key3`, `force1` and `force_drow`.
- **G8. The checker semantics differ from the server.** The server's
  `check_inv()` recurses into containers and matches `slaying` when `sp` is
  non-zero, the `name` otherwise. `blocked_tile()` requires the object when
  `last_sp` is set and forbids it when not (`check_inv.c:50-85`,
  `map.c:764-790`). The bot keys requirement against exclusion on `sp`
  instead of `last_sp`, and reads only top-level inventory. In content
  v1.14.0 no blocking checker sets `sp 0` explicitly, so the exclusion
  branch never runs. The nine blocking "must not have" checkers in Greyton
  jail are modelled as requirements (conservative). Checkers that match by
  archetype or type, with no `slaying` (three in Rockforge, two in Zechna
  temple), are not modelled at all. Sera carries a key ring; a key moved into
  it would still open doors live but vanish from planning. Whether the client
  sees closed container contents is unverified.
- **G9. Directional locks and granted forces.** The Underground City drow
  passage is one-way by design: a `marker` grants a timed force as she walks
  through, and a force-locked door opens only from one side. Locks are
  undirected tiles and forces are never in her access set, so the passage is
  never offered. That is safe, but the route is lost.
- **G10. No operator block.** The owner wants to "block off a dangerous
  route". The only static block is the hard-coded `KNOWN_LETHAL_TRANSIT`
  (one map), and the only runtime ones are per-trip exclusions.
- **G11. Map routing and savebed selection ignore unlocks.** `route()` and
  savebed walks plan without her access set (1.6, 1.10), so they disagree
  with what `NavigateTask` would do.
- **G12. No visibility.** The dashboard cannot show why a route was chosen,
  what she has unlocked, or what she has learned, and no metric tracks route
  outcomes.

## 4. Design

### 4.1 Principles

1. **The content graph decides what exists; requirements decide what is
   offered; learned costs decide what is chosen; a budget decides whether to
   go at all.** Each of these layers is separately testable.
2. **Fail closed on unknown requirements.** An edge whose requirement cannot
   be evaluated is not offered. The exception is an explicit, bounded probe
   (4.6).
3. **Ordinary player knowledge only.** Unlock state comes from what her
   client sees: inventory, quest journal, server messages, where she ended
   up. Never privileged server state.
4. **Shadow before switch.** The new planner first runs beside the current
   one and logs where they disagree. Behaviour changes only behind a flag the
   owner turns on.
5. **Keep the existing tiers as priors.** The content hazards (ranged,
   over-level, pack, lethal) become prior costs in the same units as learned
   ones, so nothing that works today is lost.

### 4.2 Edge model

`MapEdge` gains fields with defaults, so existing construction keeps working:

```python
@dataclass(slots=True, frozen=True)
class MapEdge:
    ...existing fields...
    transport: str = "walk"        # walk | exit | seam | dialogue | scripted
    requires: Requirement = NONE   # what must hold to use it
    one_way: bool = False          # no usable reverse (derived, informational)
    side_effects: frozenset[str] = frozenset()  # e.g. {"savebed"}
    interaction: tuple = ()        # dialogue: (npc name, response chain)
```

`Requirement` is a small immutable expression: `All`/`Any` of atoms.

| Atom | Meaning | Source |
| --- | --- | --- |
| `Item(lock_id)` | carries an object matching the lock (by slaying or by name, per checker `sp`) | doors, checkers |
| `NotItem(lock_id)` | must not carry it (checker `last_sp 0`) | checkers |
| `Quest(quest, part, state)` | a journal part is completed, or not | interface `state=`, scripts using `qm.completed(...)` |
| `Learned(edge key)` | she has passed it before and nothing since says otherwise | observations |
| `Unknown(reason)` | cannot be evaluated offline | unparsed checks |

Lock tiles keep their place in `MapNode` for local walking. The planner still
handles a locked tile by testing its walkability with the evaluated access set,
so the component machinery does not change. New edge kinds come from new
compilers. Each lives in its own function with its own fixtures:

- **Dialogue teleports.** Parse interface XML for `<action teleport="map x y
  [savebed]">`. The edge's source is the NPC's tile (an approach within one
  tile), and the event `race` attachment gives the NPC (as
  `_dialogue_seller_interfaces` already finds them). The response chain is
  the `destination` path from `hello`. `state=` and `<check>/<ncheck>`
  elements on the path become `Quest` atoms; any other element becomes
  `Unknown`. `savebed` becomes a side effect. In v1.14.0 this yields the
  seven voyages in 1.4.
- **Quest-gated scripted exits.** A small allow-list of recognised script
  patterns, starting with `QuestManager(...).completed("uid")` guarding a
  bounce (`lake_portal.py`), becomes `Quest` atoms on that exit. An exit whose
  script is not recognised keeps today's behaviour; it is not newly blocked.
- **Granted-force passages (later phase).** A `marker` granting force F, on
  the only approach to a door locked by F, becomes one compound one-way edge
  from before the marker to beyond the door. Its requirements are those of
  the marker's tile, the drow key here.

Edge identity, used for learned costs and stable across rebuilds:

- exit: `exit:{source}:{x},{y}>{destination}`
- seam: `seam:{source}>{destination}` (the whole border)
- dialogue: `talk:{source}:{npc}>{destination}`
- map area: `map:{path}`; tile cell: `cell:{path}:{x//8},{y//8}` (8x8 cells,
  so one corner of a map can be costed without per-tile noise)

### 4.3 Unlock state

A new `unlocks.py` module computes an `UnlockState` snapshot. It is immutable
per plan and cached until one of its inputs changes.

- **Inventory**, walked recursively through containers, as `check_inv()`
  does. Container contents are known only once she has opened the container;
  the last seen contents of her own containers are remembered (the key ring
  case) and dropped when the container leaves her inventory.
- **Key aliases** come from map-placed keys (today), plus archetypes and
  artifacts (`*.art`) with a `slaying`, plus quest reward objects
  (`<object arch=...>` in quest XML). This closes G7 for the Incuna keys.
- **Quest journal**: `client.state.quests` (part names and status), mapped to
  part uids through the quest catalog already loaded by `quests.py`.
  `Quest` atoms compare uids. Before the journal has loaded, quest atoms are
  unknown, not false.
- **Learned**: the outcome of her own attempts at an edge (4.4). A
  requirement-related rejection, such as "The portal bounces you away", a
  "prevented from reaching" magic mouth, or arriving somewhere other than the
  edge's arrival, marks the edge `blocked-for-requirement` until a related
  input changes: a quest part completes, or an item matching its lock is
  acquired. A success marks it `passed`, which can satisfy `Unknown` atoms.
  These records are hers alone, stored with the other route records.

`_allow_locked` becomes a thin call into `UnlockState`, so every caller,
including savebed walks and `route()`, uses one access set (G11).

### 4.4 Learned costs

**Units.** Every cost is in *expected seconds*. Travel time is measured in
seconds, and danger is converted to seconds at an exchange rate, so the two
add up. The exchange rate is the owner's main tuning knob (section 6).

**Travel-time prior.** For each edge, estimated tiles from the arrival square
to the next departure (`_segment_steps`) times her measured seconds per tile
(from `movement_ack_timeout`/speed), plus a fixed transition overhead. This
closes G1 even before anything is learned.

**Danger prior.** From content, as today's tiers but graded: over-level
margin, aggro-pack size (rated on every map, not only `world_*`, closing G4),
ranged or caster residents, `KNOWN_LETHAL_TRANSIT`. A prior is a cost in the
same units, so a high prior behaves like today's avoidance but can be
outweighed.

**Observed signals**, one record each, written when they happen:

| Signal | Subject | Magnitude | Trigger (existing hook) |
| --- | --- | --- | --- |
| `death` | map + cell (+ edge if crossing) | 1 | death while a `NavigateTask` is active (decision `death`, post-mortem map, x, y) |
| `recall` | map + cell | 1 | emergency recall or disengage during transit |
| `damage` | map + cell | HP lost / max HP | `hits_between` over the time on that map |
| `blocked` | edge | seconds blocked | `navigation-pack-exit-bypass`, `_exclude_edge` |
| `crossing_failed` | edge | 1 | seam excluded, rejected departure, failed click path |
| `requirement_rejected` | edge | 1 | bounce or rejection message, or a wrong arrival (4.3) |
| `traversal` | edge | seconds taken | arrived across the edge; used for time and as positive evidence |

A death in transit whose post-mortem names a pack counts against the cell,
the map and the edge she was walking toward. This separates "the north-east
of `underground_city_0_0` is deadly" from "the whole map is". Each record
also stores her survivability bucket from `CombatProfile` (the same test
`kit_hazard` uses). Danger records with a materially stronger kit count less
(an owner-tunable factor). The in-progress PR keeps hazards when her kit
gets weaker; this design adopts that rule.

**Aggregation.** For a subject, cost = prior + Σ weight(signal) × magnitude ×
decay(age) × kit factor. A `traversal` success multiplies the subject's
accumulated danger by a recovery factor (proposal: 0.7). The proposed
defaults, all named constants in one module:

| Signal | Weight (s) | Half-life |
| --- | --- | --- |
| death | 1800 (one exchange-rate unit) | 12 h |
| recall | 900 | 12 h |
| damage (per full HP) | 300 | 6 h |
| blocked | 1 × seconds | 30 min |
| crossing_failed | 120 | 30 min |
| requirement_rejected | unavailable until an input changes | none |
| traversal time | measured mean replaces the prior | 24 h |

Contributions below 1 s are dropped. Records older than 30 days are deleted.
"A route that keeps failing gets more expensive until an alternative wins"
follows directly: each death on `underground_city_0_0` adds about 1800 s to
it, and halves away over 12 h.

**Persistence.** A dedicated table in `memory.sqlite3`, in the same
(server, account, character) namespace. It is append-mostly, so a route
death does not rewrite the large `state` blob.

```sql
CREATE TABLE IF NOT EXISTS route_events (
    server TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
    at REAL NOT NULL,            -- wall clock
    subject TEXT NOT NULL,       -- edge/map/cell key from 4.2
    signal TEXT NOT NULL,        -- bounded enum (table above)
    magnitude REAL NOT NULL,
    kit TEXT NOT NULL DEFAULT '',-- survivability bucket, no item names
    content TEXT NOT NULL DEFAULT '' -- content lock revision
);
CREATE INDEX IF NOT EXISTS route_events_by_character
    ON route_events (server, account, character, at);
```

The table is pruned like `chat_log`: 30 days, and at most 20,000 rows per
character, newest kept. Writes are best effort, with the post-mortem's short
lock timeout. Play never waits on telemetry, and a failed write keeps the
event in memory for the next save. On load, the events are folded into an
in-memory `RouteCostModel`. Subjects whose edge no longer exists in the
current content are ignored, and kept until they age out. Existing
`route_hazards` entries are migrated once as synthetic `death` events at
`until - 6 h`. Edge requirement outcomes (`passed`,
`blocked-for-requirement`) are rows with their own signals.

**Privacy.** Only her own observations, in her own namespace. Subjects are
content coordinates. Monster names are not stored; the post-mortem keeps
those. An attacker that is a player is recorded only as the signal, never by
name. No chat, no other characters' positions, no account data. The dashboard
exposes the aggregates through the same loopback-only rules as `/api/state`.

### 4.5 Choosing a route

**Search.** `route_points` already supports per-edge costs. It gains a cost
callback: the edge's travel time, plus the entered map's danger (prior plus
learned), plus the danger of the arrival cell and the departure cell (the
departure being the edge she walks to). That is still a single Dijkstra over
component states, with costs scaled to integers. Within a map, `local_path`
passes per-tile costs (learned cells mapped to the `uint8` grid) so the walk
itself bends away from a deadly corner. The native code needs no change.

**Hard walls versus costs.**

- *Walls* (never offered): a requirement known to be unmet; an
  `Unknown` requirement outside a probe; an operator block (4.7, G10);
  `savebed` side effects unless the trip asks for them; apartments that are
  not the destination (today's rule).
- *Costs*: everything learned and every content prior. Learned danger never
  becomes a wall by itself. This keeps the rule at `navigation.py:1797-1810`.

**Safety against time.** Total = time + λ × danger, with λ from her state:
1.0 normally, higher for the `safety` objective, while depleted, without a
ready recall, or when the destination is optional. With the proposed rate
(one death = 30 min), safety wins every realistic comparison and time breaks
ties. That matches "always choose the safest available route".
`strict_safety` becomes "λ = ∞ for any danger above the prior's 'avoid'
level", which reproduces today's strict economy trips.

**Budget.** The caller gives the trip a danger budget, in the same seconds:

- farm trips: the spot's value, the analyzer's predicted rate × a horizon
  (proposal: 1 h of the objective's rate, converted at a rate the owner sets);
- services and errands: a fixed budget per purpose (town services strict);
- quest steps: an explicit, larger budget, because some quests demand the
  dangerous road.

If the cheapest route's danger exceeds the budget, planning returns
`RouteDeclined(cost, budget, worst subjects)` instead of a route. The caller
picks another destination; the farm circuit treats it like an unreachable
leg. This closes G2. In the incident, the first `underground_city_0_0` death
adds about 1800 s of danger. Together with the prior for its eight-monster
pack, that should exceed the Underground City spot's one-hour value, so the
circuit moves on after one death, not five. That outcome depends on the
weights, which is why phase 5 replays the incident before anything switches.

### 4.6 Exploration risk

- **Untraversed edges** cost only their prior. Priors are pessimistic, never
  optimistic: there is no exploration bonus. A new route is taken only when
  it is the cheapest under the prior, which is how she finds alternatives.
- **First traversal under care.** When the chosen route has an edge or map
  she has never traversed, and its prior danger is above a threshold, the
  trip requires full HP and SP and a ready recall before leaving, and
  records `traversal` or the failure signals on the way. This reuses the
  existing hostile-route expedition rules.
- **Probes for unknown requirements.** An `Unknown` edge, such as a dialogue
  with an unparsed check or a force passage, is offered only when the owner
  has turned probes on, once per edge per day, and only when a known route to
  the same destination exists as fallback. Failure gives
  `requirement_rejected`; success gives `passed`.
- **Side effects.** `savebed` voyages are never probed. They are used only
  when the trip says a respawn move is wanted.

### 4.7 Operator blocks

The owner can block a map, an exit or a seam from the dashboard, with an
optional expiry. A block is a wall for transit; the blocked map stays usable
as a destination unless the owner says otherwise. Blocks are stored as route
events with a `blocked_by_operator` signal, so they share the store and its
audit trail. `KNOWN_LETHAL_TRANSIT` moves to a seeded operator block.
Accepting dashboard writes needs the same loopback and cross-origin
protections as the existing control endpoints.

### 4.8 Metrics and dashboard

Prometheus (bounded labels only, no map labels):

- `route_plans_total{result}`: planned, declined, no_route, shadow_diverged;
- `route_events_total{signal}`;
- `route_plan_cost_seconds{part}` gauge for the active plan: time, danger,
  budget;
- `route_learned_subjects` gauge: subjects with non-zero learned cost.

Dashboard, Navigation section:

- the active route with each edge's time, danger and the subjects that
  dominate it; for a declined trip, why and against what budget;
- **Unlocks**: keys detected (and where: inventory, container, remembered),
  quest gates open or closed, transports available or unavailable and why;
- **Learned**: the 25 highest-cost subjects with their signals, last event
  and when their cost drops below half; operator blocks with an unblock
  control;
- shadow mode: the last 20 divergences between the current and the new plan.

### 4.9 Interaction with the farming analyzer

AGENTS.md puts spot ranking in the locked `atrinik/tools` analyzer and rules
out bot-side spot tables. Route risk is live, personal experience, the same
category as the quarantines and `kit_hazard` filters that
`analyzer_farm_choice` already applies. So:

- `analyzer_farm_choice` asks the planner for each candidate spot's cheapest
  route from her savebed: cost, or declined. It skips declined spots, as it
  skips quarantined ones, within the existing per-call planning budget.
  Results are cached per (savebed, unlock fingerprint, learned-cost
  version).
- The analyzer's ranking is unchanged. Whether route cost should become an
  analyzer input, for example as a per-spot travel penalty, is a
  cross-repository decision (section 6).
- Savebed selection ranks beds by route cost, not tiles. The ship then counts
  for a Brynknot bed serving Asteria spots.
- `_observe_farm_death` keeps blaming the destination for a transit death, as
  the in-progress PR arranges. The new route events add the transit evidence
  the planner uses.

### 4.10 Test strategy

- **Unit tests on synthetic graphs**, the way `test_bot.py` already builds
  small `WorldGraph`s:
  - requirement evaluation: checker `sp` and `last_sp` combinations
    matching `check_inv.c`, keys in containers, artifact and quest-reward
    keys, quest atoms before and after the journal loads, unknown atoms;
  - dialogue edges: compiling an interface fixture, `state=` and `ncheck`
    gating, `savebed` side effects;
  - the cost model: each signal's weight and half-life with an injected
    clock, kit factors, recovery on traversal, pruning, migration of
    `route_hazards`;
  - persistence: round trip, the namespace, the row cap, a locked database
    not stalling play, privacy (a player attacker leaves no name in any row);
  - the planner: the cheapest route wins, a repeated failure flips the choice
    to the alternative, the budget declines, an operator block is a wall,
    `strict_safety` matches today's results on the existing strict-trip
    tests.
- **Content regression tests** against the locked bundle, like
  `test_lost_memories_uses_quest_sam_not_departure_sam`:
  - ship: 3 transitions with the key, more than 20 without;
  - the Underground City entrance inventory (section 2), the lake pocket
    gated on `portal_found`, and the drow passage absent without its unlocks;
  - Incuna to Brynknot routable through Sam only with Lost Memories
    completed;
  - the Old Outpost well bucket only with the orange stone giant key;
  - the Incuna gate keys recognised as keys.
- **Incident replay.** Feed the 2026-11-14 decisions and post-mortems into
  the cost model and `analyzer_farm_choice`. Assert that the Underground City
  spot is declined after the first `underground_city_0_0` death with the
  chosen weights.
- **Performance.** Extend `benchmark_pathfinding.py` with whole-world
  weighted plans. Acceptance: median plan time no worse than 1.5x today's
  breadth-first search on the same routes.
- **Shadow soak.** Run in shadow mode on the live bot for at least 24 h of
  play. Review divergences before turning the flag on.

## 5. Phased plan

Each phase is one small pull request that can ship alone, with tests, and
leaves behaviour unchanged unless it says otherwise.

**P1. `fix(navigation)`: match the server's checker semantics and
container keys.** Require or forbid on `last_sp`, match `slaying` or `name`
on `sp`, model `NotItem`, and walk containers for keys.
*Accept:* unit tests per `check_inv.c` combination; the Greyton jail
checkers become exclusions; no change to routes in the content regression
set except where a forbid-checker opens; `ACCESS_ITEM` removed or used.

**P2. `feat(navigation)`: an unlock state from inventory, artifacts and quest
rewards.** Add `unlocks.py`; `_allow_locked`, savebed walks and `route()`
use it; the dashboard shows unlocks read-only.
*Accept:* Sera's live inventory yields the Incuna gate and barn lock ids;
savebed walks use the ship with the key; existing tests pass.

**P3. `feat(navigation)`: quest-gated and dialogue edges.** Compile
`<action teleport>` voyages and the recognised quest-gated portal script.
`NavigateTask` executes a dialogue edge with the existing dialogue task.
*Accept:* `world_4_85` to `world_0_70` plans through Sam when Lost Memories
is completed and not otherwise; the lake portal is offered only with
`portal_found`; `savebed` voyages are never chosen without an explicit flag;
`LostMemoriesArrivalTask` can be simplified later, but stays in this PR.

**P4. `feat(memory)`: record route events.** Add the `route_events` table,
the recorders from 4.4, pruning and the `route_hazards` migration. The
dashboard lists learned subjects. The planner is unchanged.
*Accept:* every signal has a test from a recorded decision sequence; a
locked database delays a death by at most the post-mortem timeout; no player
names in rows.

**P5. `feat(navigation)`: a cost model and planner in shadow mode.** Add
travel-time and danger priors, aggregation, and a weighted `route_points`
behind a flag. With the flag off, compute both plans and log divergences
(`route-shadow-diverge`) and metrics.
*Accept:* the incident replay meets 4.10; strict trips are identical;
benchmark within budget; 24 h live shadow soak reviewed by the owner.

**P6. `feat(navigation)`: plan with learned costs and budgets.** Turn the
flag on by default after owner approval; add `RouteDeclined`, budgets per
caller, and operator blocks (dashboard control, `KNOWN_LETHAL_TRANSIT`
seeded).
*Accept:* a repeated transit failure switches to the alternative in a
synthetic graph; a sole lethal route declines; the owner can block and
unblock a map live.

**P7. `feat(farm)`: route-aware spot and savebed selection.**
`analyzer_farm_choice` skips declined spots; savebeds rank by route cost.
*Accept:* incident replay picks another spot after one death; savebed
choice uses the ship; analyzer ranking untouched.

**P8. `feat(navigation)`: in-map cell costs.** Learned cells feed
`local_path` through `grid_search` costs.
*Accept:* in a synthetic map, a path bends around a cell with two deaths
while a cheaper path exists; plan latency within budget.

**P9. `feat(navigation)`: probes and granted-force passages.** Bounded
probes of `Unknown` edges and compound one-way edges for marker-granted
forces (the drow passage).
*Accept:* probes stay off by default; a probe failure marks
`requirement_rejected` until an input changes; the drow passage is offered
only with the drow key and in the open direction.

Unlock *acquisition*, such as getting the Underground City keys because they
open a safer route, is out of scope. It needs goal planning beyond routing.

## 6. Open questions for the owner

1. **Exchange rate.** How much extra travel is one expected death worth? The
   proposal is 30 minutes, with λ raised for the safety objective, while
   depleted, and without a ready recall.
2. **Automatic walls.** Should learned danger ever become a wall on its own,
   for example three transit deaths in 6 h with the same kit? Or are walls
   only for unmet requirements and operator blocks, with the budget doing the
   refusing (the proposal)?
3. **Budgets.** Is "one hour of the spot's predicted rate" the right farm
   budget? What fixed budget should errands and quest steps get?
4. **Decay.** Are 12 h half-lives for deaths and recalls, 6 h for damage and
   30 min for blockers right, and a 0.7 recovery per successful traversal?
5. **Operator blocks.** Is a dashboard control acceptable, given it adds a
   write endpoint? Should blocks expire by default?
6. **Analyzer boundary.** Keep route cost as a bot-side filter (the
   proposal), or add a travel-cost input to the `atrinik/tools` analyzer,
   which is a cross-repository change?
7. **Probes and side effects.** May she probe `Unknown` edges at all? Should
   `savebed` voyages be excluded from routing permanently?
8. **Shadow acceptance.** How long a soak, and what divergence review,
   before P6 turns the flag on?
9. **Player attackers.** Deaths to players count as route danger without
   names. Is that acceptable, or should PvP be excluded from route learning?

## 7. Known uncertainties

- The static model reaches the Asteria-side gangway without passing
  Morg'eean's gate. This was not checked live.
- The Underground City lake pocket's isolation is a static result. The live
  map might connect it through something the graph does not model.
- Whether the client receives the contents of closed containers, such as the
  key ring, was not verified.
- Why her deaths cluster in `underground_city_0_0`'s north-east (route
  following, kiting or pursuit) was not established; the post-mortems show
  disengage and retreat steps, not the route.
- The weights in 4.4 are starting points. Only the replay and the shadow
  soak can calibrate them.
