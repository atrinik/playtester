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

The owner has also decided (2026-11-14):

- **Exchange rate.** One expected death costs the same as 30 minutes of extra
  travel.
- **No automatic walls.** Learned danger never becomes a hard wall on its
  own. It can stop a trip only through that trip's danger budget (4.5).
- **Owner blocks.** The dashboard gets a control to block a route or a map.
  Every block expires after a duration she chooses, from 1 hour to 7 days.
  The dashboard shows it, and she can remove it early (4.7).
- **Probes are bounded and safe-only.** Routes with unknown requirements may
  be probed only at full health, only where the approach is low-danger, at
  most a few a day, stopping at the first failure (4.6).

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
  NPC. Seven exist in content, three of them with `savebed`. The graph
  models none of them:
  - Incuna to Brynknot: Sam's Lost Memories interface with
    `state="completed"` teleports to the ocean map `world_0_83` (8,17), whose
    stairs lead below deck and out at Brynknot `world_0_70`. From
    `world_0_83` the graph already routes to `world_0_70` (2 transitions);
    only the dialogue hop is missing, so Incuna `world_4_85` to Brynknot is
    unroutable today, and so is the way back.
  - Brynknot back to Incuna: Sam on `world_0_70`
    (`interfaces/strakewood_island/brynknot/sam_goodberry.xml`) teleports to
    the ocean map `world_2_87` (5,6). Only that first hop is dialogue:
    `world_2_87` has static stairs at (7,7) down to
    `ship_lower_deck_to_incuna`, whose stairs lead to `world_4_85`.
  - Three teleports also move her respawn point (`savebed`): the two
    Deserted Island voyages (`interfaces/ocean/sam_goodberry.xml`,
    `interfaces/quests/escaping_deserted_island/quest.xml`) and Lost
    Memories' `the_journey` (`interfaces/quests/lost_memories/quest.xml:939`).
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
  (`native/pathfinding_module.c:601-625`, `684`; `grid_search` chooses the
  same way at `379`). Overflow is reported as
  `"cost overflow"` (`test_pathfinding.py:104-110`). `grid_search` accepts a
  per-tile `uint8` cost grid (`pathfinding.py:50-62`), which `local_path`
  does not use yet. Learned costs therefore need no native change.
- **Tile walks.** `_segment_steps` and `_route_walk`
  (`navigation.py:4344-4391`) estimate the tiles walked along a planned route.
  Only savebed selection uses them.
- **Search states.** A `route_points` state is (map, `min(component)`). Its
  position is the arrival square of the first edge that reached it, in
  breadth-first order (`navigation.py:1374`, `1413-1419`). Later edges
  into the same component reuse the state and keep that position, so
  onward transitions are resolved from wherever she first arrived, not from
  the arrival the final route actually uses. With unit costs this only
  affects which seam crossing is picked. With travel-time costs it matters
  (4.5).

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

**PR #94** (`fix(navigation): stop route deaths from looping through one
transit map`, branch `fix/sera-route-death-loop`, open at `0b4f787` when
this was written) adds a farm-selection rule. Each route death is kept in
`farm_adaptations` `kit_failures` with its transit map and the damage taken
there (`map_damage`). `route_death_maps` makes such a map a hazard for 6 h
(`ROUTE_DEATH_HAZARD_SECONDS`). `route_enters_death_map` then skips, as a
hard skip, every spot that cannot be reached by an exact `route_points`
search avoiding those maps. A death stops counting once her kit is improved
(`CombatProfile.no_survivability_gain`), which is a binary rule. The PR also
lets recall work while travelling. It does not change `_observe_farm_death`.

#94 is being revised. Its kit rule becomes "a survivability gain and no
material loss", and deaths on her respawn map or on chokepoints fall back to
a cost instead of the skip. Check `origin/fix/sera-route-death-loop` again
before implementing 4.9, in case it moves further. Section 4.9 says how this
design wraps and then replaces it.

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
- **The Brynknot entrances lead in, but cannot be reached from Brynknot.**
  `world_2_69` (0,6) and the sewers `lab` (11,12) are both in the graph. Both
  arrive at (23,13) in `underground_city_0_1`, in an 11-tile walled pocket
  (21-23, 11-14, with Dunsor's corpse). The pocket is not a dead end. Its
  east column is on the map border: the coordinate seam crosses at (24,13)
  into `underground_city_1_1` (0,13), a 322-tile component. From the pocket,
  `route_points` reaches `0_4` through `1_1 > 1_2 > 1_3 > 1_4 > 0_4` with no
  lock and without `0_0`. The green lake portal (22,12) only gates the other
  way out of the pocket: its script, `scripts/lake_portal.py`, bounces the
  player unless Portal of Llwyfen's `portal_found` part is complete, and the
  graph treats it as an ordinary exit.
  So the Brynknot side is still not a way in statically, but for a different
  reason: neither entrance is reachable from Brynknot's main component.
  `world_2_69` (0,6) is reached only from (0,7), a one-tile component ringed
  by water that is itself the lake portal's arrival. The `lab` is entered
  only from `world_3_66_-2` (23,12), and no static route reaches that map
  from Brynknot. The live sewers may connect in a way the graph does not
  model (section 7).
- **Other entrances need unlocks she lacks.** With every lock open, the
  drow entrance `world_4_67_-1` (7,17) to `underground_city_5_2_-1` reaches
  `0_4` in 14 transitions without `0_0`. The minimum access is
  `underground_city_key_drow`, `underground_city_force_drow` and
  `underground_city_key1`. The drow key is not a quest reward. It is a
  `quest_container` carried by the level-56 drow annihilator that spawns at
  (4,17) in `underground_city_5_2_-1`, and its description calls it a one-way
  passage. The force is granted by a `marker` as she walks through, and the
  door is "not possible to open from this side". The yellow portal from
  `world_6_67_-1` and the Bone Cairn plane give lower-level entrances.
- **The `1_x` column is the alternative inside, and an over-level hazard
  blocks it.** From `0_0`, with `0_0` and `0_2` penalized, the planner would
  switch to `0_0 > 1_0 > 1_1 > ... > 1_4 > 0_4`. From the Brynknot pocket, the
  same column avoids `0_0` entirely. Either way it crosses `1_1`, which holds
  level-49 ghouls and shadow ghouls. At level 34 the over-level margin is 7,
  so anything at 41 or above is avoided before learned penalties are weighed.

The conclusion is the most important input to this design. **For this
incident, a better graph alone would not have saved her.** With her unlocks,
from where she could start, every route the planner would take crosses
`underground_city_0_0`. The content does have alternatives: the drow
passage, and the Brynknot pocket into the `1_x` column. But she cannot reach
them, and the column is over her level. What was missing was a route cost
that the trip and the spot selection could refuse. "Only route is lethal"
should mean "do not go", not "go anyway, cheapest". The design keeps "a
cost, never a wall" for choosing between routes, and adds a **budget** that a
trip's cheapest route must fit.

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
  branch never runs. Nine blocking "must not have" checkers
  (`last_grace 1`, `last_sp 0`) are modelled as requirements instead, which
  is conservative: six in Greyton jail (`jail_force`) and three on
  `world_13_62` (`berri_lur_stench`). Three checkers in Rockforge and two in
  the Zechna temple have no `slaying`, `race` or `hp`. Those match any object
  she carries, so they are effectively open, and leaving them unmodelled is
  right in practice.
  The bot also ignores `last_heal`, which makes a checker consume the
  matching object (`decrease_ob`) each time she passes. Five checkers do
  this: `underground_city_5_2_-1` (8,3), `underground_city_5_3_-1` (4,12),
  `hierro_lair` (10,4), `creation_0801` (11,18) and `world_12_65` (5,19). A
  single-use pass is treated as permanent.
  Sera carries a key ring; a key moved into it would still open doors live
  but vanish from planning. Whether the client sees closed container
  contents is unverified.
- **G9. Directional locks and granted forces.** The Underground City drow
  passage is one-way by design: a `marker` grants a timed force as she walks
  through, and a force-locked door opens only from one side. Locks are
  undirected tiles and forces are never in her access set, so the passage is
  never offered. That is safe, but the route is lost.
- **G10. No owner block.** The owner wants to "block off a dangerous
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
   owner turns on. New edge kinds that act in the live world (dialogue
   voyages, quest-gated exits) also stay behind their own flag until a
   supervised run. Owner blocks are the only exception: the owner sets them
   herself (P3).
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
| `Consumes(lock_id)` | carries it, and passing uses one up (checker `last_heal 1`) | checkers |
| `Quest(quest, part, state)` | a journal part is completed, or not | interface `state=`, scripts using `qm.completed(...)` |
| `Learned(edge key)` | she has passed it before and nothing since says otherwise | observations |
| `Unknown(reason)` | cannot be evaluated offline | unparsed checks |

A `Consumes` atom is satisfied only by an item she carries now, counted by
quantity along the route. A `Learned` "passed" record never satisfies it
again: she passed by spending the item.

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
  seven voyages in 1.4, three of them with `savebed`.
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
- **Learned**: the outcome of her own attempts at an edge (4.4).
  - A rejection marks the edge `blocked-for-requirement`. It needs message
    evidence: a server line matched against the edge's authored text, such
    as the lake portal's "The portal bounces you away" or a magic mouth's
    "prevented from reaching", within a few seconds of the attempt.
  - A wrong arrival with no such message is not a requirement outcome. It is
    a `crossing_failed` cost, so a flaky crossing can never close an edge.
  - A rejection lasts until a related input changes (a quest part completes,
    or an item matching its lock is acquired), and at most 24 h. The time
    limit matters for edges that have no `Item` or `Quest` atom: otherwise
    one false rejection would close them for good.
  - A success marks the edge `passed`, which can satisfy `Unknown` atoms but
    never `Consumes` atoms.
  - These records are hers alone, in their own table (4.4).

`_allow_locked` becomes a thin call into `UnlockState`, so every caller,
including savebed walks and `route()`, uses one access set (G11).

### 4.4 Learned costs

**Units.** Every cost is in *expected seconds*. Travel time is measured in
seconds, and danger is converted to seconds at an exchange rate, so the two
add up. The owner set the exchange rate: one expected death = 1800 s
(30 min of travel).

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

A death in transit always counts against the map and the edge she was
walking toward. It counts against a cell only when the death square is on
the planned local path, or within 3 tiles of it. A death further away
happened in flight or pursuit, and charging that cell would teach the
planner nothing about the route. The incident's `underground_city_0_0`
deaths at x 19-21, y 1-4 were off the route from the stairs at (1,1) to the
south seam, so they would be map-level only. That separates "this corner of
the route is deadly" from "the whole map is", without inventing corners.

Each danger record also stores her survivability description from
`CombatProfile`. The kit rule is #94's, so the two never disagree: a record
stops counting once she has a survivability gain and no material loss since
it was made (the revised #94 predicate). A weaker kit keeps it counting.
There is no partial discount.

**Aggregation.** For a subject, cost = prior + Σ weight(signal) × magnitude ×
decay(age) × kit factor. A `traversal` success multiplies the subject's
accumulated danger by a recovery factor (proposal: 0.7). The proposed
defaults, all named constants in one module:

| Signal | Weight (s) | Half-life |
| --- | --- | --- |
| death | 1800 (the owner's rate) | 6 h |
| recall | 900 | 6 h |
| damage (per full HP) | 300 | 6 h |
| blocked | 1 × seconds | 30 min |
| crossing_failed | 120 | 30 min |
| requirement_rejected | edge unavailable until an input changes, at most 24 h | none |
| traversal time | measured mean replaces the prior | 24 h |

The 6 h death half-life matches #94's 6 h window: a death is worth 1800 s at
once, 900 s after 6 h and 450 s after 12 h. Contributions below 1 s are
dropped. Danger rows older than 7 days are deleted. "A route that keeps
failing gets more expensive until an alternative wins" follows directly:
each death on `underground_city_0_0` adds 1800 s to it, and two in an hour
add about 3400 s.

**Cold start.** Every edge starts untraversed, which would put every trip
under the first-traversal care rule (4.6) on day one. Two measures avoid
that. First, P5 seeds map-level `traversal` counts from the persisted
decision ledger and map arrivals she already has (`decisions`, 2,000
records). Second, the care rule ramps in: for the first 7 days after route
recording starts, it applies only to edges whose prior is at the avoid
level.

**Persistence.** Four tables in `memory.sqlite3`, all in the same
(server, account, character) namespace. Each grows in its own bounded way,
and a route death never rewrites the large `state` blob.

```sql
-- Rare danger and failure signals, one row per event.
CREATE TABLE IF NOT EXISTS route_events (
    server TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
    at REAL NOT NULL,             -- wall clock
    subject TEXT NOT NULL,        -- edge/map/cell key from 4.2
    signal TEXT NOT NULL,         -- death | recall | damage | blocked | crossing_failed
    magnitude REAL NOT NULL,
    kit TEXT NOT NULL DEFAULT '', -- survivability description, no item names
    content TEXT NOT NULL DEFAULT '' -- content lock revision
);
-- Successful traversals, aggregated: one row per subject, upserted.
CREATE TABLE IF NOT EXISTS route_traversals (
    server TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
    subject TEXT NOT NULL,
    count INTEGER NOT NULL, mean_seconds REAL NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY (server, account, character, subject)
);
-- Owner blocks (4.7).
CREATE TABLE IF NOT EXISTS route_blocks (
    server TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
    subject TEXT NOT NULL, created_at REAL NOT NULL, until REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (server, account, character, subject)
);
-- Requirement outcomes per edge (4.3).
CREATE TABLE IF NOT EXISTS route_requirements (
    server TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
    edge TEXT NOT NULL, outcome TEXT NOT NULL, -- passed | rejected
    at REAL NOT NULL, until REAL NOT NULL,
    inputs TEXT NOT NULL,         -- fingerprint of the related quest/item state
    evidence TEXT NOT NULL DEFAULT '', -- the matched server line, truncated
    PRIMARY KEY (server, account, character, edge)
);
```

- `route_traversals` is updated in place with an upsert (`count + 1`, a
  running mean), so it grows with the number of distinct subjects she has
  crossed, not with time. Rows idle for 30 days are deleted.
- `route_events` is capped per signal, not as a whole, so a flood of
  `blocked` rows can never evict the rare ones. Proposed caps per character:
  death 1,000; recall 1,000; damage 5,000; blocked 2,000;
  crossing_failed 2,000. Plus the 7-day age limit.
- `route_blocks` rows are removed when `until` passes, or when the owner
  removes them.
- `route_requirements` rows are removed when `until` passes (24 h for a
  rejection, 30 days for a pass), or when `inputs` no longer matches.

Writes are best effort, with the post-mortem's short lock timeout. Play
never waits on telemetry, and a failed write keeps the event in memory for
the next save. On load, the tables are folded into an in-memory
`RouteCostModel`. Subjects whose edge no longer exists in the current
content are ignored until they age out. Existing `route_hazards` entries are
migrated once as synthetic `death` events at `until - 6 h`.

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

**The state approximation.** States stay (map, `min(component)`) with the
first arrival's position (1.6). The walking time charged to an edge is
measured from that first arrival, not from the arrival the cheapest route
really uses. Two consequences, and how they are handled:

- **Error bound.** For each map transit, the time error is at most the walk
  between two squares of the same component: its walking diameter (`d`,
  computed once per component from the cached `grid_reachable` result).
  Most world maps are 24x24, so `d` is usually under about 46 steps. A
  route's error is at most the sum of `d` over the maps it crosses. Danger
  terms are per map, edge and cell, so the approximation never changes
  them; only the time term is affected. That is the term the owner's rate
  makes small: 46 steps is well under a minute, against 1800 s for a death.
- **When it matters.** It can pick the wrong one of two routes that differ
  by less than the error bound in time and have equal danger. P6's shadow
  mode logs every divergence with both routes' exact walked tiles
  (`_route_walk`), so the frequency is measured, not guessed.
- **The exact alternative.** If shadow data shows the approximation
  matters, P9 changes the state to (map, component, arrival square), with
  arrival-to-departure distances cached per component. Those distances
  depend only on static content and the access set, so they are computed
  once per graph build in the existing parse worker pool.

**Performance plan.** The extra cost is the travel-time term for each
expanded edge.

- It comes from `_segment_steps`, which runs `local_path`. Calls are
  memoized per (map, arrival, departure, access set). The content is static,
  so the memo survives across plans and is cleared with `_component_cache`.
- The danger term is a dictionary lookup in the `RouteCostModel`.
- `benchmark_pathfinding.py` gains whole-world weighted plans over a fixed
  set of real trips, including the incident's and the ship's. The gate is a
  median weighted plan time no worse than 1.5x today's breadth-first plan on
  the same trips, measured with a warm memo, plus a reported cold-memo
  figure.
- If the gate fails, the fallback is to precompute the memo for the maps
  the analyzer's spots and town services use, at graph build time.

**Hard walls versus costs.**

- *Walls* (never offered): a requirement known to be unmet; an
  `Unknown` requirement outside a probe; an owner block (4.7, G10);
  `savebed` side effects unless the trip asks for them; apartments that are
  not the destination (today's rule).
- *Costs*: everything learned and every content prior. Learned danger never
  becomes a wall by itself; only a trip's budget can stop it (owner
  decision). This keeps the rule at `navigation.py:1797-1810`.

**Safety against time.** Total = time + λ × danger, with λ from her state:
1.0 normally, higher for the `safety` objective, while depleted, without a
ready recall, or when the destination is optional. With the owner's rate
(one death = 30 min), safety wins every realistic comparison and time breaks
ties. That matches "always choose the safest available route".

`strict_safety` stays an exclusion, not a huge cost. Its avoided set is
today's (ranged at any level, over-level, pack, `KNOWN_LETHAL_TRANSIT`,
learned) with "learned" redefined as every map whose learned danger is at
least `STRICT_LEARNED_AVOID_SECONDS` (900 s, half a death). Learned danger
is part of "above avoid", as today, where `_plan_strict` already avoids
learned hazards. Building it as an exclusion keeps it exact and fast, and
it cannot be outweighed by a long detour.

**Budget.** The caller gives each trip a danger budget, in the same
seconds. If the cheapest route's danger is at or above it, planning returns
`RouteDeclined(cost, budget, worst subjects)` instead of a route. This closes
G2, and it is the only way learned danger can stop a trip (owner decision).

- **Farm trips.** Budget = `FARM_ROUTE_BUDGET_SECONDS` × advantage, where
  `FARM_ROUTE_BUDGET_SECONDS = 1800` and advantage = 1 − r_next / r_spot.
  `r_spot` and `r_next` are the analyzer's predicted rates for the farmed
  objective, for this spot and for the next-ranked usable spot (ranking
  order, before any route check). So a trip may risk at most one death's
  worth, scaled by how much better the spot is than the next one. Two
  equally rated spots get a budget of 0, so the one with less transit danger
  wins. The danger counted is transit
  only: the analyzer already rated the spot itself, so the destination's own
  maps are excluded.
- **Quest steps** get an explicit, larger budget per policy, because some
  quests require the dangerous road.
- **Optional errands** (upgrade sweeps, utility shopping, junk sale) get a
  fixed budget per purpose (open question 1).
- **Essential trips** are food when hungry or starving, a cure or the
  depletion service, the savebed move, and a bank trip that pays for one of
  these. They are never declined into stranding. When their budget declines
  them, they take the cheapest route anyway, with care: they wait to be at
  full HP and SP where it is safe to rest, need a ready recall when she has
  one, and use transit hold-fire. A decision `essential-route-override` is
  recorded and the dashboard shows it. Owner blocks still apply. If the only
  route crosses an owner block, the trip fails as today and an alert is
  raised (open question 3).

The incident with numbers: with transit priors set to zero (their values
are calibrated in shadow mode), the first `underground_city_0_0` death adds
1800 s of transit danger to every route to `0_4`. The farm budget is
1800 × advantage, and advantage is below 1 whenever another usable spot has
a positive rate. Even a spot rated 99% better than the next gets 1782 s, so
one death declines it. When nothing else is usable (r_next = 0), the budget
is exactly 1800, and "at or above" declines it too. For a spot rated 20%
better, the budget is 360 s, and the death keeps it declined until
1800 × 0.5^(t / 6 h) < 360, which is t > 6 × log2(5) ≈ 13.9 h. So the
circuit moves on after one death, not five. The analyzer's rates for the
Underground City spot were not in the live state when it was read, so this
uses the bound, not the actual ratio.

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
  with an unparsed check or a force passage, may be probed within the
  owner's bounds:
  - only at full HP and SP;
  - only where the approach to the edge is low-danger: every map and cell on
    the route to its departure is below `PROBE_MAX_DANGER_SECONDS`
    (proposal: 300 s, a sixth of a death);
  - at most `PROBES_PER_DAY = 3` a day, and once per edge per day;
  - stopping for the day at the first failure;
  - only when a known route to the same destination exists as fallback.
  Failure gives `requirement_rejected` (with its 24 h limit); success gives
  `passed`.
- **Side effects.** `savebed` voyages are never probed. They are used only
  when the trip says a respawn move is wanted.

### 4.7 Owner blocks

The owner can block a map, an exit or a seam from the dashboard. Each block
has a duration she picks, from 1 hour to 7 days. The dashboard lists every
active block with its expiry and a control to remove it early. A block is a
wall for transit for every trip, essential trips included. The blocked map
stays usable as a destination unless she says otherwise. Blocks live in
`route_blocks` (4.4) and are deleted when they expire.
`KNOWN_LETHAL_TRANSIT` (one map) stays a code constant until the owner
replaces it with a block of her own; seeding a block would need an expiry
she did not choose. The control accepts writes only with the same loopback
and cross-origin protections as the existing control endpoints.

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
  and when their cost drops below half;
- **Blocks**: owner blocks with their expiry, a remove control, and the
  block form (subject and a 1 h to 7 d duration);
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
  cross-repository decision (open question 2).
- Savebed selection ranks beds by route cost, not tiles. The ship then counts
  for a Brynknot bed serving Asteria spots.
- `_observe_farm_death` already blames the destination for a transit death
  (`navigation.py:4813-4821`). #94 keeps that. The new route events add the
  transit evidence the planner uses.

**Relation to #94.** P8 first wraps #94's rule, then replaces it. At any time
exactly one of them decides, so no death counts twice:

- With the learned-cost flag off, #94's `route_enters_death_map` decides, as
  merged. P5 also records each of its route deaths as a `death` route event,
  so the history is there when the flag turns on.
- With the flag on, the budget decides, and `route_enters_death_map` is not
  called. Its 6 h hard skip is covered by the death cost: 1800 s at once,
  which declines a spot with any usable alternative (4.5). Its kit rule is
  the same predicate (4.4). Its revised fallback for deaths on the respawn
  map or a chokepoint, "fall back to a cost", is what the budget does for
  every map.
- Once the flag has been on for the owner's acceptance period, a later PR
  removes `route_enters_death_map` and `ROUTE_DEATH_HAZARD_SECONDS`.

### 4.10 Test strategy

- **Unit tests on synthetic graphs**, the way `test_bot.py` already builds
  small `WorldGraph`s:
  - requirement evaluation: checker `sp`, `last_sp` and `last_heal`
    combinations matching `check_inv.c`; `Consumes` counted by quantity and
    never satisfied by a `passed` record; keys in containers; artifact and
    quest-reward keys; quest atoms before and after the journal loads;
    unknown atoms;
  - learned requirement outcomes: a rejection needs message evidence, a
    wrong arrival alone is only a cost, and a rejection expires after 24 h or
    when its inputs change;
  - dialogue edges: compiling an interface fixture, `state=` and `ncheck`
    gating, `savebed` side effects;
  - the cost model: each signal's weight and half-life with an injected
    clock, the #94 kit predicate, cell attribution only near the planned
    path, recovery on traversal, per-signal caps, the traversal upsert,
    migration of `route_hazards`, cold-start seeding;
  - persistence: round trip, the namespace, a locked database not stalling
    play, block and requirement expiry, privacy (a player attacker leaves no
    name in any row);
  - the planner: the cheapest route wins, a repeated failure flips the choice
    to the alternative, the budget declines, an essential trip overrides a
    decline with care, an owner block is a wall, and `strict_safety`
    matches today's results on the existing strict-trip tests.
- **Content regression tests** against the locked bundle, like
  `test_lost_memories_uses_quest_sam_not_departure_sam`:
  - ship: 3 transitions with the key, more than 20 without;
  - Underground City, as in section 2: with her access set, every route
    from Brynknot to `0_4` enters `0_0`. The `world_2_69` (0,6) and `lab`
    arrivals land in the `0_1` pocket, which reaches `0_4` by the seam into
    `1_1` without a lock. Brynknot's main component reaches neither arrival.
    The lake portal exit is offered only with `portal_found`. The drow
    passage is absent without its unlocks;
  - Incuna `world_4_85` to Brynknot `world_0_70` routable through Sam only
    with Lost Memories completed, and only with the dialogue flag on;
    Brynknot to Incuna uses one dialogue hop, then the `world_2_87` stairs;
  - the Old Outpost well bucket only with the orange stone giant key;
  - the Incuna gate keys recognised as keys;
  - the five `last_heal` checkers compile as `Consumes`.
- **Incident replay.** Feed the 2026-11-14 decisions and post-mortems into
  the cost model and, in shadow mode, `analyzer_farm_choice`. Assert that the
  shadow decision would decline the Underground City spot after the first
  `underground_city_0_0` death.
- **Performance.** The benchmark gate in 4.5.
- **Live soak.** At least 24 h of live play in shadow mode, reviewed with
  the owner. This is the gate for turning the learned-cost flag on (P7), not
  for merging the shadow code.

## 5. Phased plan

Each phase is one small pull request that can ship alone, with tests, and
leaves behaviour unchanged unless it says otherwise. The order is chosen so
that each phase's acceptance needs only what earlier phases built.

**P1. `fix(navigation)`: match the server's checker semantics.** Require or
forbid on `last_sp`, match `slaying` or `name` on `sp`, model `NotItem` and
`Consumes` (`last_heal`), and walk containers for keys.
*Accept:* unit tests per `check_inv.c` combination; the six Greyton jail and
three `world_13_62` checkers become exclusions; the five `last_heal`
checkers compile as `Consumes`; no change to routes in the content
regression set except where a forbid-checker opens; `ACCESS_ITEM` removed or
used.

**P2. `feat(navigation)`: an unlock state from inventory, artifacts and quest
rewards.** Add `unlocks.py`; `_allow_locked`, savebed walks and `route()`
use it; the dashboard shows unlocks read-only.
*Accept:* Sera's live inventory yields the Incuna gate and barn lock ids;
savebed walks use the ship with the key; existing tests pass.

**P3. `feat(dashboard)`: owner route and map blocks.** The `route_blocks`
table, the dashboard form (subject, 1 h to 7 d), the list with expiry and
early removal, and the planner treating active blocks as transit walls for
every trip. Behaviour changes only when the owner sets a block.
*Accept:* a block on a synthetic route forces the alternative; with no
alternative, the trip fails with a block reason and an alert; a block
expires on time and can be removed early; writes are refused off loopback
or cross-origin.

**P4. `feat(navigation)`: quest-gated and dialogue edges, behind a flag.**
Compile `<action teleport>` voyages and the recognised quest-gated portal
script. `NavigateTask` executes a dialogue edge with the existing dialogue
task. Flag `dialogue_edges`, default off; the owner turns it on after one
supervised voyage.
*Accept:* with the flag on, `world_4_85` to `world_0_70` plans through Sam
when Lost Memories is completed and not otherwise; the lake portal is
offered only with `portal_found`; `savebed` voyages are never chosen without
an explicit trip flag; with the flag off, routes are unchanged.
`LostMemoriesArrivalTask` stays in this PR.

**P5. `feat(memory)`: record route events.** The `route_events`,
`route_traversals` and `route_requirements` tables, the recorders from 4.4,
per-signal caps, cold-start seeding, the `route_hazards` migration, and #94's
route deaths mirrored as events. The dashboard lists learned subjects. The
planner is unchanged.
*Accept:* every signal has a test from a recorded decision sequence; a
locked database delays a death by at most the post-mortem timeout; no player
names in rows; caps hold under a synthetic flood of `blocked` rows without
evicting deaths.

**P6. `feat(navigation)`: a cost model, budgets and shadow planning.** Travel
time and danger priors, aggregation, weighted `route_points`, budgets with
the essential-trip override, and the shadow farm decision. With the flag
off, the live plan and farm choice are today's. The new ones are computed
alongside and logged (`route-shadow-diverge`, `route-shadow-decline`) with
metrics.
*Accept:* the incident replay's shadow decision declines the Underground
City spot after one death; strict trips are identical; the benchmark gate
in 4.5 passes.

**P7. `feat(navigation)`: plan with learned costs.** Turn the flag on: live
plans use learned costs and budgets, and declines and essential overrides
take effect for navigation.
*Gate:* a 24 h live shadow soak from P6, reviewed with the owner, and her
approval. *Accept:* a repeated transit failure switches to the alternative
in a synthetic graph; a sole lethal route declines an optional trip; an
essential trip on the same route proceeds with care.

**P8. `feat(farm)`: route-aware spot and savebed selection.**
`analyzer_farm_choice` skips declined spots, and savebeds rank by route
cost. With the flag on, this replaces #94's `route_enters_death_map`; with
it off, #94 decides (4.9).
*Accept:* the incident replay picks another spot after one death, live
rather than in shadow; savebed choice uses the ship; analyzer ranking
untouched; no death is counted by both rules.

**P9. `feat(navigation)`: in-map cell costs and exact arrival states.**
Learned cells feed `local_path` through `grid_search` costs. If P6's shadow
data shows the first-arrival approximation changes choices, states carry
arrival squares (4.5).
*Accept:* in a synthetic map, a path bends around a cell with two on-path
deaths while a cheaper path exists; off-path deaths do not bend it; the
benchmark gate holds.

**P10. `feat(navigation)`: probes and granted-force passages.** Probes of
`Unknown` edges within the owner's bounds, and compound one-way edges for
marker-granted forces (the drow passage).
*Accept:* a probe runs only at full HP and SP, on a low-danger approach, at
most 3 a day, and stops for the day at the first failure; a failure marks
`requirement_rejected` for at most 24 h; the drow passage is offered only
with the drow key and in the open direction.

Unlock *acquisition*, such as getting the Underground City keys because they
open a safer route, is out of scope. It needs goal planning beyond routing.

## 6. Open questions for the owner

Settled on 2026-11-14 and folded in above: the exchange rate (30 min per
death), no automatic walls, owner blocks with a 1 h to 7 d expiry, and
bounded, safe-only probes.

1. **Errand and quest budgets.** What fixed danger budget should optional
   errands and quest steps get? The farm budget is defined (4.5).
2. **Analyzer boundary.** Keep route cost as a bot-side filter (the
   proposal), or add a travel-cost input to the `atrinik/tools` analyzer,
   which is a cross-repository change?
3. **Blocks against essential trips.** If her only route to food, a cure or
   her savebed crosses one of your blocks, should she fail and alert (the
   proposal) or cross it with care?
4. **Decay.** Are a 6 h half-life for deaths and recalls (matching #94),
   6 h for damage, 30 min for blockers and a 0.7 recovery per successful
   traversal right?
5. **Probe limits.** Is 3 probes a day "a few", and is 300 s (a sixth of a
   death) the right ceiling for a "low-danger" approach?
6. **`savebed` voyages.** Should the three voyages that move her respawn
   point be excluded from routing permanently?
7. **Soak review.** Beyond 24 h of shadow play, what divergence review do you
   want before the flag turns on (P7)?
8. **Player attackers.** Deaths to players count as route danger without
   names. Is that acceptable, or should PvP be excluded from route learning?

## 7. Known uncertainties

- The static model reaches the Asteria-side gangway without passing
  Morg'eean's gate. This was not checked live.
- The static graph finds no route from Brynknot to the `world_2_69` (0,6)
  islet or to `world_3_66_-2`, the only way into the sewers `lab`. The live
  sewers might connect through something the graph does not model, which
  would open the Brynknot pocket and the `1_x` column as a way in.
- Whether the client receives the contents of closed containers, such as the
  key ring, was not verified.
- Why her deaths cluster in `underground_city_0_0`'s north-east was not
  established. They are off the stairs-to-seam route, so flight or pursuit
  is likely, and the post-mortems show disengage and retreat steps.
- The weights in 4.4, the transit priors and the farm budget constant are
  starting points. The replay and the shadow soak calibrate them.
- #94 is still under revision. 4.9 assumes its revised kit rule and
  fallback; check `origin/fix/sera-route-death-loop` before P5 and P8.
