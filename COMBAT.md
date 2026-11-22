# Combat: one-on-one pulls, then storm farming

This note describes how the bot fights aggressive residents today, why it
changed, and what comes next. It covers the farm loop in `tasks.py`
(`FarmTask`) and the pull squares that `navigation.py`
(`FarmCircuitTask._new_child`) computes for it.

## The problem

Sera (character level 28, slash weapons 28, Wizardry 10) trains Wizardry with
magic bullet (see "Spell-only Wizardry training" below). In a live session she
died 7 times in 40 minutes,
each time to an orc pack of 2-3 attackers, archers included, while at full
stats. One orc at a time is easy for her; two or three are not.

The old code let packs in through several gaps:

- `ranged_pull` and the Wizardry target filter refused a target only when
  another hostile stood within 2 tiles of it. Orcs detect from about 4 tiles,
  so a neighbour 3-4 tiles away joined the fight.
- Pull squares were checked for clearance only against the spawns the leg
  targets, not against the other residents of the map.
- After a pull she held a 2-4 tile gap for at most 2.5 s, then walked toward
  the target, often into the rest of its pack.

## One-on-one mode (implemented)

### Lone-pull predicate

`FarmTask._lone_target(client, candidate, fight_square)` decides whether a
candidate may be pulled, by spell or bow. It is lone when no other visible
hostile has either of these inside its own detection radius:

- the candidate's position;
- the square she fights from (her pull or anchor square).

A monster's detection radius comes from `aggressive_detection_ranges`, which
the circuit fills from the authored `aggro_radius` of every aggressive
resident on the map; unknown monsters default to 4. A neighbour whose name
marks it as a ranged attacker (see below) counts with a radius of at least 6.
Authored-peaceful creatures are ignored, since they never join a fight.
`ranged_pull` and the single-target Wizardry filter both use this one
predicate. The Wizardry filter still keeps a monster that is beside her or
already coming at her (an active threat), so she never ignores an attacker.

### Clear pull square as the kite anchor

`_new_child` searches the perimeter of each target spawn's `aggro_radius` for
a pull square. Clearance is now measured against every aggressive resident
of the map. Residents whose names contain "archer", "bowman", "slinger" or
"mage" (`RANGED_RESIDENT_MARKERS`) are given a radius of at least 6, because
they shoot from outside their detection range. The chosen squares are passed
to `FarmTask` as `pull_squares`.

When she pulls, the farm records an anchor. It is the pull square of the
target spawn nearest the target, provided that spawn is within 6 tiles of
the target and its pull square within 7 tiles of her. Both distances are
Chebyshev, with no path check, and apply only on the farm map itself.
Otherwise the anchor is the square she stands on. After the pull,
`melee_kite` prefers steps toward the anchor among steps that keep at least
4 tiles from every other hostile; separation still wins over the anchor.
`low_health_retreat` is unchanged except that, for the engaged target's
anchor, it prefers the step toward it when two steps are otherwise equal.

### Wait, then approach only if alone

After a pull she holds at or near the anchor for up to `PULL_WAIT_SECONDS`
(8 s). She steps back toward the anchor if she is off it, and never takes a
step that brings her closer to the target.

- If the target comes within 2 tiles, she fights it normally.
- If it has not come after 8 s, she approaches it only if it is still lone,
  measured at its current position.
- Otherwise she sets it aside for `PULL_SET_ASIDE_SECONDS` (120 s) and picks
  another target. A set-aside only hides the monster from target choice and
  pull candidates. It still counts as a threat throughout. The set-aside
  ends only when it expires or the monster arrives within 2 tiles of her,
  where a melee attacker must be. Standing within its detection radius of
  her does not end it, and while it stands near her its packmates do not
  become targets.

The old 2.5 s fallback that walked toward the target no longer applies to a
pulled target.

The pull mode does not use combat-text attribution. Combat text names an
attacker only by species, so a packmate's arrow ("orc archer hit you") or a
stale line from an orc killed earlier would look like the pulled orc
attacking. So:

- the wait ends only on arrival or the 8 s timeout;
- the set-aside ends only on expiry or arrival;
- a hit while she has no engagement commits her to the nearest threat that
  is neither set aside nor inside a set-aside monster's reach (its detection
  radius, at least 6 for a ranged monster, measured from that monster);
- if no such threat remains but a set-aside monster is visible, she does not
  commit. Healing comes first; otherwise she retreats with
  `low_health_retreat`, which steps away from the visible threats. On a
  neutral farm that retreat counts only monsters matching a named attacker,
  so when it finds none she falls back to the unseen-contact retreat. Its
  pursued-stand rule can still make her fight a packmate that stays
  adjacent through several retreat steps. With no threats in view at all,
  the existing unseen-contact retreat applies.

A ranged pulled target that shoots her therefore waits out the 8 s, and a
set-aside ranged monster is not fought until it closes to 2 tiles or its
120 s set-aside expires. The safety policy (healing and the flee threshold)
and the retreats still react to its damage.

Accepted limit: while she is unengaged, a monster standing within a
set-aside monster's reach is not fought back, unless it comes adjacent and
the pursued-stand rule fires, until the set-aside expires.

### Analyzer ranking (already shipped)

Pull request https://github.com/atrinik/playtester/pull/77 ranks
Wizardry-to-15 spots by running the farm analyzer at the trained skill's
level with `--max-aggro-pack 1`, so the circuit only picks spots where a lone
pull is possible.

## Spell-only Wizardry training

The server splits kill XP between every skill that damaged the monster, in
proportion to each skill's share of the damage (`award_kill_exp` in
`server/src/server/attack.c`). Each skill's share is of its own capped kill
XP: at Wizardry 10 that cap is 10,000 per kill, reached by almost any monster
of level 10 or higher. The old finisher, where the sword brought a target
down and a spell landed the last hit, therefore fed Slash and left Wizardry
nearly unchanged. Live, Wizardry XP did not move for hours.

- **Every hit is the spell.** While Wizardry trains, `training_finisher`
  casts from the first hit. It has no hand-off window and no per-target cast
  limit, and auto-melee stays off. Safety heals first (on a neutral farm,
  the isolated kite heals only at its flee threshold, as before); after that
  it casts down to the flee threshold.
- **Out of mana, she waits.** When the next cast would dip into the
  healing reserve, she keeps her distance with combat off until mana
  regenerates, rather than let a melee finish hand Slash its share.
- **Never melee a training target.** Whenever the spell cannot be cast
  (hurt below the heal threshold, line blocked), the farm loop keeps her
  distance with combat off; safety and retreat handle her health. Pulls use
  the spell, never a launcher. On the farm's map, travel defence casts at a
  threat within reach after safety, or keeps its distance. A retreat step no
  longer swings at the engaged target. Melee remains only as survival: the
  cornered-retreat breakout, the pursued stand when she cannot cast (see
  "Caster kiting" below), travel defence against an
  adjacent attacker she cannot cast at or step away from, and transit maps'
  last-resort fight when cornered. Accepted limit: that travel-defence
  check reads any recent hit, so an unseen or distant attacker can make her
  fight an adjacent training creature that is not the one hitting her, and
  a kite refused only because the creature's health is not reported yet
  counts as cornered.
- **Pulls wait for a full bar.** Unengaged, with no creature coming at her
  and not under attack, she rests until her mana is full before the next
  pull (`training-rest`), unless she has no food (mana regenerates only
  with food). The circuit's dwell and empty-sweep switches wait out a fresh
  rest, so she does not set off with a partial bar; service detours still
  may. Resting out of combat also ramps her
  regeneration up: with Meditation, the server multiplies it by
  1 + seconds since combat / 10, up to tenfold (`player_do_some_living`).
  Only hits and offensive casts restart that clock.
- **A bar that cannot cast trains the weapon.** Spell-only training needs
  max mana to cover one cast beyond the healing reserve.
- **The analyzer ranks spell kills.** The profile sent for Wizardry training
  is the caster model: the median raw damage of her recent spell hits, taken
  from her own hit text within 3 s of one of her recent casts of the
  planned spell ("You hit X for N (P)
  with ..." gives raw damage N - P); a fixed 1 s cast delay (mana, not the
  cast rate, bounds the kill rate, and the client's `action_time` reflects
  whatever she did last); the spell's mana cost; `regen_sp` (the client
  shows it per second); max mana less the three-heal reserve; and
  `--meditation` when she has that skill. Magic bullet deals weapon magic, which the locked
  analyzer cannot model, so it is passed as magic: understated only against
  the few monsters that resist magic. No monster archetype resists weapon
  magic. Until a cast has been observed, the weapon stands in.

For example, with her live mana (8 SP per cast, 0.3 SP/s, 50 SP usable) and an
assumed 14 damage per cast, the analyzer ranks level 10-11 spots first, at
about 145,000 XP/hour. Mana is the bottleneck: most of each clear is spent resting.
Raising mana regeneration (gear, Meditation) or capacity is what speeds
training up; see "Mana economy" below.

## Mana economy

### Caster gear scoring

While she trains Wizardry, both the farm's `maintain_inventory` and the shop
buyer score gear as a caster (`InventoryPolicy.caster_stats`). The farm
decides this as before. The shop sweep asks the circuit, which applies the
same rule: the build trains Wizardry and the skill is below its milestone.
Otherwise scoring is unchanged and every "+N" counts the same.

A caster bonus is worth only what it adds at her current stats, using the
server's formulas:

- **Mana regeneration.** The server regenerates mana in proportion to
  `gen_sp × 60 + maxsp + 10`. `gen_sp` starts at 1 and each worn non-weapon
  adds its regeneration. Item names show it as `(mana reg.+0.4)` per point,
  but only on untitled rings, amulets and trinkets: a titled item such as
  the Blue Crystal Talisman of Incuna hides its point. So `gen_sp` is read
  back from her `regen_sp` stat, which the server sends as
  `(gen_sp × 60 + maxsp + 10) / 15` tenths before Meditation (at the default
  8 ticks a second); names are only the fallback. A point's worth depends on
  what she already wears: with `gen_sp` 2 and 74 max mana it is +29%.
- **Pow.** Spell damage is multiplied by `1 + spell_dam_bonus[Pow]`, which is
  flat for 11-15: Pow 13 to 14 adds nothing, and 15 to 16 adds 15%. Pow also
  adds `sp_bonus[Pow] / 2.5` of base max mana.
- **Int.** Int adds `sp_bonus[Int]` of base max mana (also flat for 11-15)
  and no damage. Its value is capped at what the same change to Pow at the
  same stat value would give, so Int never outweighs Pow.
- **Max mana** (`(mana+N)`). Worth its share of her max mana, plus the
  regeneration it adds through `maxsp`.
- **Base max mana**, which Int and Pow scale, is the player archetype's 4
  times Wizardry level + 3 (64 at Wizardry 13), not derived from her total,
  which hidden item mana inflates.

A percent of regeneration counts as much as a percent of spell damage (8
points), because mana bounds her kill rate. A percent of max mana counts 2.
So one regeneration point (about 235 at `gen_sp` 2) outscores any single
stat. A worn item is valued against her stats without it, so a worn Pow+1
ring that lifts her from 12 to 13 is worth nothing next to a candidate that
would. Weapons add no max mana or regeneration on the server, so on a
weapon those count for nothing. Shop stock whose ground name lacks the
bonuses is valued from its examine text, and is never also counted at the
generic 40 per stat.

Protections count 8 per percent everywhere: through the name's generic
"+N", and in the shop examine score when the ground name lacks them. A
caster upgrade takes off a protection item only when it wins clearly, by
the protection's worth once more: one regeneration point does not displace
her "slow +14%" ring, two do. Melee scoring has no such margin.

With both ring slots full, the server keeps whichever worn ring comes first
in inventory order and takes off the other, which may be the better one.
So a ring upgrade first takes off the weakest worn ring explicitly, then
puts the new one on.

### Greyton's ring stock

From level 30, the upgrade sweep also visits Greyton's general store
(`world_9_67`). Its stock includes a difficulty-50 `random_talisman` square
(14,8), which rolls a ring 60% of the time and an amulet 40%. Random rings
can roll Pow, Int or mana regeneration. The road from Aris
passes level 28-29 orc spawns, hence the level gate. Greyton comes last in
the sweep, so a saved cursor from an earlier sweep keeps its meaning. The
sweep policy version moved to 8, so characters at level 18 and above sweep
again.

### Gandyld's mana crystal

A power crystal (`types/power_crystal.c`) does one of two things when
applied, and nothing otherwise:

- at full mana it stores `min(space, sp / 2)`;
- below full mana it gives back `min(missing, stored)`.

`ManaCrystal` examines the crystal once to learn its capacity and a rough
charge ("It has capacity of 100 and is half full."). After that, the mana
change each apply causes keeps the stored amount up to date. An apply that
changes nothing proves the crystal full (when banking) or empty (when
drawing).

- **Banking.** During spell-only training she rests with no threats near.
  On a full bar, with food and room in the crystal, she applies it to bank
  half her mana (`mana-crystal-bank`). The rest then refills the bar, and
  she banks again until the crystal is full. Only then does she pull.
- **Drawing.** In a spell-only fight, when the next cast would dip into the
  healing reserve, she applies the crystal if it holds mana
  (`mana-crystal-draw`). She never applies an empty crystal, a full one at
  full mana, or one while a cast is still affordable. The mana a draw
  starts from is net of casts sent but not yet acknowledged, so a cast's
  cost landing after the apply is not read as an empty crystal.
- **Charge for other code.** `ManaCrystal.known_charge(client)` and
  `has_charge(client)` tell whether the crystal holds mana. Code that counts
  on a mana restorer should use them rather than the crystal's presence.

Getting the crystal and upgrading it:

- **Opening reward.** At level 18 Gandyld gives her the 50-SP crystal.
- **Recovery.** If the quest has started and she does not carry the crystal,
  she checks the apartment chest, at most once a day.
- **The free upgrade (`ManaCrystalUpgradeTask`).** It runs from level 30
  while "A Better Crystal" is active. Her crystal's capacity is checked
  first, by examining it again if an earlier examine went unanswered; a
  crystal of unknown capacity never goes to the pool. If the capacity is
  below 100, or she has no crystal, she fights through the Old Outpost and
  applies the alchemists' pool on `old_outpost_a_0201` (7,8). The route's
  strongest residents are level 24 troll elite soldiers. She then fights
  her way back to Gandyld's map on a defended leg (the plain dialogue route
  finds no way out of the Outpost) and reports, which completes the part
  and starts "A Stronger Crystal".
- **Trip guards.** The trip runs outside the farm circuit, whose death and
  recall handling never sees it. It fails on a death or emergency recall
  recorded after it started, and after 30 minutes. A failed trip waits a
  day before it is tried again.
- **Replacement.** Gandyld's dialogue offers no replacement crystal, but the
  pool's script creates the enhanced crystal whether or not she carries the
  old one. So the pool trip also replaces a lost crystal.
- **Not done: the second upgrade (200 SP).** That pool is in King Rhun's
  vault (`old_outpost_a_0204`), behind a locked door. The key is carried by
  King Rhun, level 30, among level 25-30 guards. The well entrance needs an
  orange stone giant key. The task stops after the first upgrade, and the
  quest policy's vault step is left for a later change.

## Caster kiting (implemented)

Live, the circuit made about 724,000 XP/hour against the analyzer's 2.0
million. Most decisions were retreats, pack splits and two-to-three-tile
melee-kite gaps. Pursued stands swung the sword at full health, so slash hits
landed inside Wizardry kills. And 9 of 10 recent deaths were packs on the
charred treant maps (`world_15_74` to `world_17_74`) and the Strakewood
underground city orcs.

### Aimed bullets, not eight lines

A bolt or bullet is aimed at the selected target when it is launched:
`fire_bolt` and `fire_arch_from_position` (`server/src/server/spell_util.c`)
pass the player's target to `projectile3d_launch`, which turns the heading
straight at it. It then flies in a straight line (only magic missile homes),
about 12 tiles for magic bullet, and stops at walls. Whatever damage a
creature does not absorb flies on into the next creature in its path
(`common_object_projectile_hit`): after a kill, but also after a block or a
hit reduced by protection. So the eight FIRE lines matter only for the
direction byte sent, the nearest one; what she needs is a clear path.

- `aimed_projectile_spell` names the aimed spells (magic bullet, magic
  missile, cause light wounds, the bolts, lightning, meteor, asteroid).
  Cones (firestorm, icestorm, holy word) and swarms keep exact lines.
- `ray_cells` ports the server's planar projectile advance
  (`types/common/projectile3d.c`), cell for cell: the heading's larger axis
  moves one tile per step, and a crossing through a corner touches all three
  cells around it (`boundary_event`).
- `bullet_ray_clear` checks every cell the flight crosses before the target
  cell: an authored wall or door, or another creature, blocks it. On a
  neutral farm the cells beyond the target count too, since leftover damage
  would wake a bystander.
- `aimed_fire_direction` gives the direction to send, or None when there is
  no clear shot. The finisher, spell pulls, pursuit casts and `caster_kite`
  use it; pack casts check the same ray through `pack_line`.
- **Casts that do nothing.** Terrain data can miss a wall, and some creatures
  shrug the bullet off. When a finisher cast's feedback times out with no
  target-HP drop and no "You hit" line, it counts against that line (target,
  her square). After `FUTILE_CAST_LIMIT` (2) such casts the line is futile:
  `aimed_fire_direction` refuses it, so she steps to another square.

### Range 4 to 8

While Wizardry trains, `caster_kite` holds the target 4 to 8 tiles away
(`CASTER_MIN_RANGE`, `CASTER_MAX_RANGE`): out of melee, in view, and inside
the bullet's flight.

- She casts whenever the spell is ready and the shot is clear, from wherever
  she stands, the pull square included.
- Between casts (action timer, waiting for HP feedback, out of mana) she
  steps back from a target closer than 4 tiles, preferring squares with a
  clear shot.
- At range with no clear shot she steps to a square that has one, preferring
  one no closer to the target, never inside 4 or beyond 8. This replaces the
  old walk toward the target (`move_to_view`).
- After `CASTER_STALL_SECONDS` (10 s) without a shot she sets the target
  aside with the pull set-aside: hidden from target choice, still a threat,
  and released as soon as it comes within 2 tiles.
- Steps respect the authored walkability, steps already found blocked, and
  occupied tiles. Separation from other hostiles wins, then a clear shot,
  then range, then the pull anchor.

`melee_kite` still serves weapon fights and the old hybrid handoff.

### Pursuit

An attacker still adjacent after one retreat step (`PURSUIT_CAST_STEPS`)
gets the spell: she casts at the most wounded adjacent attacker she has a
clear shot at, with combat off (`pursued-cast`), then keeps retreating until
the next cast is ready. She casts only above the flee threshold the farm uses
for the threats within 4 tiles (55%, or 80% for two, 94% for three or more)
and only when she can afford the cast. Otherwise the melee pursued stand
(three steps, the breakout latch) applies as before. Pursuit and pack casts
start the finisher's cooldown, so it does not cast again straight away.

### Packs

With two or more active threats within 4 tiles, and health above the pack
flee threshold (80% for two, 94% for three or more), `pack_cast` casts
instead of retreating:

- first choice, a shot whose ray passes through two or more pack members,
  aimed at the most wounded front one, so what it does not absorb carries on
  into the next;
- otherwise the most wounded pack member with a clear shot, but only while
  none is within 2 tiles.

Between casts, or when neither applies, the existing pack-split retreat
steps away and keeps her distance.

### Safety

- **Recall-wand charges.** The client sees a wand's charges only in its
  examine text ("It has N charges left.", `types/player.c`). On a quiet farm
  tick she examines each identified recall wand of unknown charge. After she
  fires one, its count is forgotten and read again, since a fire can fizzle
  without spending a charge. "The wand says poof." (`types/wand.c`) after a
  fire marks it empty, and the same emergency tries the next recall source
  at once. An empty wand is no longer an emergency candidate, and recall
  shopping treats it as no device. When the last recall source is known
  empty, the recall shop check is reset so a replacement is bought, once
  she can afford it, without waiting out its daily interval. Recharging
  needs the recharge spell, which she does not have. She farms on without
  a working recall
  ([owner decision, 2026-11-22](#a-working-recall-is-never-a-blocker-owner-decision-2026-11-22)).
- **Hazardous for her kit.** Each farm failure review now stores the
  analyzer's combat profile she failed with. A spot with three pack or
  out-damaged failures, or approach deaths, in 24 hours (`KIT_HAZARD_FAILURES`), each with the
  survivability as hers now or better, is hazardous for her kit. The test
  (`CombatProfile.no_survivability_gain`) is one-sided: a failure stops
  counting only once she has genuinely improved on it, with maximum health
  more than 10% up, armour class more than 2 up, total protections more
  than 5 up or character level more than 2 up, and none of them down by
  more than that. Weaker or equal, it still
  counts: live, depletion dropped Sera's maximum health from 322 to 244,
  and a symmetric test had stopped counting the deaths she had at full
  strength. Skill level and mana move constantly while she trains and are
  ignored. The circuit leaves such a spot at once (`farm-kit-hazard`), and
  `analyzer_farm_choice` skips it until her survivability improves. If every
  usable spot is hazardous, she farms the least dangerous one rather than
  idle. No map names are listed; maps qualify by their record. Live, the
  treant maps had eight such deaths in 90 minutes, `world_16_74` alone five;
  the underground city had two, so it qualifies on a third.
  The hazard is judged per area, not per circuit: the analyzer ranks
  several overlapping circuits for one area (two for the underground city,
  three for the burnt forest), and live she moved to a sibling circuit after
  each one was marked, dying about three times an hour. A candidate spot
  counts the failures of every adaptation record whose spot shares a map
  with it (each record keeps its spot's maps), plus its own record's. A
  death on the way to a spot (`route`) counts for that spot too, not for
  spots on the road map; an emergency recall on the way does not. Live, 22
  such approach failures at the underground city had not counted. Records
  saved before maps were kept count for their own spot, and a single-map
  spot's also for spots sharing that map.
- **Route deaths mark the map she died on.** A death on the way to a spot
  also keeps the transit map and the damage taken on it. For six hours
  (`ROUTE_DEATH_HAZARD_SECONDS`, as long as navigation penalises that map),
  `route_death_blocked_maps` blocks that map and every map that she can
  reach from her savebed only through it. `analyzer_farm_choice` then skips
  any spot with a leg on a blocked map, whichever spot she was heading to,
  and the hazard and depletion fallbacks skip it too. Reach comes from the
  planner's own walkable-component expansion (`WorldGraph.reachable_maps`),
  judged from the savebed, where every death returns her. Without a known
  savebed, it is judged from where she stands, keyed by her component. The
  coarse map graph is not enough: it reaches the underground city's
  circuits around the entrance through seams she cannot walk.
  - A route emergency recall counts like a death when that map hurt her
    (`map_damage` above 0); it still does not count toward kit hazards.
    Live, on 2026-11-16, three recalls on the underground city entrance
    taught only a route cost, and then she died there.
  - Exceptions: a recall or death on a map that did not hurt her, a
    death with a kit she has since improved on, a death on her savebed
    map, and a death on a chokepoint block nothing. A chokepoint is a map
    cutting off more than half of what she reaches from the savebed
    (`ROUTE_DEATH_CHOKEPOINT_SHARE`). These keep only the planner's
    penalised cost. Blocking the town map next to her respawn would block
    all 40 ranked spots. When hazards that each stay under the share
    together cut off more than it, the largest cuts become cost-only, one at
    a time, until the rest are under it, so a small dead end stays blocked
    beside them. Repeated deaths
    on such a map remain possible until the routing design's care and cap
    rules land (docs/ROUTING.md, https://github.com/atrinik/playtester/pull/95).
  - Without a known savebed (or with an apartment savebed whose return is
    not recorded), reach is judged from where she stands. While she stands
    on a death map, only the death maps themselves are blocked, so a spot
    elsewhere sends her off it.
  - Each expansion covers the whole world (a few tenths of a second on the
    event loop). Results are cached, and at most one runs every
    `ROUTE_DEATH_SEARCH_INTERVAL_SECONDS` across all callers. None runs
    while she is being hit, while the circuit finishes a fight or loot, or
    for the dashboard and Prometheus, which read cached results only. Hits
    from before her last death do not count as contact, and the combat
    mode flag does not either: the server turns it on with every hit and
    keeps it through a death. The circuit also turns combat mode off once
    after each death.
  - While a route death is still being judged, nothing is usable. The
    circuit keeps its plan, and stops (`farm-spots-exhausted`) only once its
    own spot is judged blocked and no other spot is usable. Meanwhile a
    plan whose leg is on, or whose route from where she stands crosses, a
    counting route-death map is held in transit (`farm-route-death-hold`),
    after the transit recall, healing and the low-health retreat have run:
    she defends herself but does not walk on. While held, a corpse or loot
    from that defence does not hold off the search. From
    `ROUTE_DEATH_VERDICT_WAIT_SECONDS` (30 s) after the newest failure the
    search runs despite contact or an unfinished fight
    (`route_death_overdue`), still rate limited, so the hold lasts at most
    about that long plus a few searches; until a search completes the
    verdict stays undecided and nothing behind the death map is chosen. A
    decided verdict against her circuit keeps her held while a fight delays
    the switch. Only an undecided overdue verdict is searched mid-fight,
    and no town route is planned for it. While held, only her own
    engagement or hostile contact in this life is a fight: a target the
    server selected that has stopped attacking (a guard still in view) is
    not. Live, on 2026-11-16,
    she respawned and walked back towards the entrance that had just
    killed her for three minutes while the verdict stayed undecided. A
    pause after repeated failures rebuilds the farm task as well, so the
    fatal fight's engagement does not read as a fight still being
    finished.
  - Walks between a usable circuit's legs are not routed around blocked
    maps: learned route hazards only cost the planner more there. Every leg
    is reachable from the savebed without entering one, so a penalised
    route enters none unless the server's one-way links leave no other way.
  - Live, on 2026-11-14, Sera died six times in 13 minutes at the
    underground city entrance on the way to two circuits that do not farm
    it.
- **Recall on the way.** Off the farm map (except within
  `TRANSIT_RECALL_BED_RADIUS` tiles of her savebed, where recall leads
  anyway; the rest of an outdoor savebed map is ordinary road), the
  circuit asks the farm task's emergency recall
  first. It uses the same thresholds, device readiness, charge and
  in-flight handling and the same `emergency-recall` decision, before
  healing and the low-health retreat. Threats count as on the farm
  (`transit_threat_count`): the engaged target, a creature hitting her, or
  one within its detection radius. A recall in flight is never followed by
  another for 30 s, even on another map. Clearing it on the map change had
  fired a second recall, and with it a second failure review, as soon as
  the first one landed her in town. Live, she had fled at 8-16 hit points
  with a seven-charge recall wand.
- **A recall counts once it starts.** The server drops a fire without a
  word while her previous action is still recovering
  (`socket_command_fire`'s `action_attack` check), so no recall is fired
  before her action timer has run out. While it waits she is in recall
  mode (`recall_waiting`, checked by `combat_allowed` at every combat
  entry point): combat mode is kept off (a hit turns it back on server
  side), and no fight branch, cornered breakout, pursued stand, transit
  defence or healing spell runs, since each would restart the timer. She
  still steps away and drinks potions, which leave it alone, and the
  readied device stays readied however long the wait. The fire is noted
  where she stands, so a "Something blocks" or countermagic answer marks
  that square or map. A fire counts only when the server answers "You feel a force
  starting to build up inside you." (`cast_wor`) within
  `RECALL_CONFIRM_SECONDS` (3 s); only then is `emergency-recall` recorded
  and the 30 s guard held. An unanswered or refused fire is recorded as
  `emergency-recall-dropped` and retried on the next eligible tick, which
  also waits for the timer the last fire set: a lagging answer costs at
  most an extra charge. "Powerful countermagic cancels all spellcasting
  here!" (`MAP_NOMAGIC`) marks the whole map as blocking magic, so she
  stops firing anywhere on it; "Something blocks…" marks the square. A rod
  that "whines for a while" (out of charge) or is "impossible to handle"
  gives way to the next source for `RECALL_DEVICE_RETRY_SECONDS`. Live, on
  2026-11-16, three recalls on the underground city entrance, exactly 30 s
  apart, never started, and she died there. A level-17 wand's force takes
  about 9 s to land (72 server ticks); it is not cancelled by moving or by
  damage. The decision is recorded where she is when the answer arrives:
  retreating across a seam in between, it is recorded on the neighbouring
  map, and the failure review judges that map.
- **Healing outpaced.** From the first heal in the last 8 s on, when the
  hits taken are at least what the later heals restored (her health at the
  latest heal is no higher than at the first), healing has stopped keeping
  up (`healing_outpaced`). The hits that pushed her under the heal threshold
  came before the first heal and do not count. She then stops chaining
  heals: from the heal threshold down she retreats with combat off
  (`healing-outpaced`, 4 s at a time), and an emergency recall fires at the
  heal threshold instead of the flee threshold. At the flee threshold
  healing resumes alongside the retreat.

- **No casts where magic is blocked.** On a no-magic square the server
  answers every spell with "Something blocks your spellcasting."
  (`cast_spell`, `server/spell_util.c`, which checks the caster's own
  square). The client does not see square flags, so that answer is the
  signal. The client records the square of each spell, scroll, rod or wand
  use when it is sent, and `spell_blocked_here` attributes the answer to
  the square of the last use before it, so a step taken before the answer
  arrives does not mark the wrong square. Word of Recall's own check
  answers "Something blocks your spell." and counts too. The square is
  remembered for an hour, shared by every safety policy. There, safety
  heals only with potions and plain items, never a spell, scroll, rod or
  wand (`spell-blocked`; a read scroll turns to dust even so), and a
  blocked healing spell is no reason to hold still and wait for mana. Once
  she moves off the square, healing casts resume. Live, idle on her
  no-magic savebed square in Clearhaven, Sera sent about 11,641
  minor-healing casts while starving, and 135 of her last 150 messages were
  that answer.

## Stop: no tasks except survival

The dashboard's Stop (`{"type": "stop"}`) ends the running task: no
farming, questing, navigation or errands. It does not leave her unattended.
A connected Atrinik character keeps getting hungry, so with no task the
engine still runs a narrow survival guard (`TaskEngine._protect_idle`):

- the idle safety policy heals her and eats carried food below its food
  threshold (`idle-hunger`, "ate ..."). Only a rise in her food stat counts
  as a meal: after three applications without one, it stops applying what
  she carries for a minute and treats her as having no food. Only this
  idle policy does that; task safety policies keep applying food;
- once she is hungry with nothing to eat (`hungry`, the test autoplay
  uses), `IdleSurvival` walks to the nearest reachable temple priest for the
  free food service, the trip autoplay uses (`idle-hunger`, then
  `idle-hunger-fed`), and she stays idle where she ate;
- when no priest is reachable or the trip fails, it records
  `idle-hunger-failed`, passes that priest over for an hour, raises the
  dashboard's critical `idle-food` alert, and retries only after 10, 20,
  40, then every 60 minutes. Eating, a later trip, a task, or her food
  stat back at 180 or more clears the alert. It never loops.

Stop also ends a running idle food trip, and so do the manual dashboard
actions move, dialog, apply and apply on the ground, which would otherwise
fight it. Each records `idle-survival-hold`, trip or not. No trip starts
for the next 10 minutes (`IdleSurvival.RETRY_SECONDS`), so Stop during
autoplay's own temple-food phase does not send her off again at once.
Repeated actions cannot stretch that: one hunger episode is held at most
10 minutes from its first hold. The hold ends, or is not applied, once her
food is below 100: food falls about a point every 4.3 s, and at zero the
server drains her health one-for-one (`types/player.c`). After the hold, a
still-hungry character goes again: starving is worse than overriding the
operator. Starting any task hands her back to it; a trip in flight is
dropped. Live,
on 2026-11-14, Stop left Sera idle, without food or money, on her savebed in
Clearhaven; she starved to death 22 times, about every 50 minutes, and her
depletion rose from 41 to 67.

## No farming while unsafe (owner decision, 2026-11-17)

Live, on 2026-11-17, Sera died ten times in under three hours, eight of
them on the Burnt Forest circuits and the roads to them. Depletion rose
from 68 to 92 and her maximum health fell from 197 to 134. Both her recall
wands were empty, and she had 1,038 copper against a 2,865 cure. Every
safeguard above held as designed and none stopped her. The kit hazard
marked every Burnt Forest circuit, but with every usable spot hazardous
she farmed the least dangerous one. Pauses quarantined one leg map at a
time, so she moved to a sibling circuit. The temple threshold only changed
which objective she farmed, because farming was what paid the fee.

`farming_unsafe_reason` held farming in two cases. The second, a missing
working recall, was removed by the
[owner decision of 2026-11-22](#a-working-recall-is-never-a-blocker-owner-decision-2026-11-22),
which supersedes it; depletion is the only case left:

- depletion at the temple-service threshold (`DEPLETION_SERVICE_THRESHOLD`,
  3 points, shown as `conditions.depletion_threshold`, with the live
  depletion force). The circuit already treated that as the point where
  the lowered stats make her spots deadly. The fee does not grow with the
  points, and every death adds one, so a higher limit would only allow
  more deaths. Maximum-health loss cannot serve as the limit: the client
  has no undepleted baseline, and level and gear move it too. The kit
  comparison is relative between failures, and its fallback is what kept
  her farming;
- (superseded on 2026-11-22) from level 18, owning recall sources none of
  which works.

While it holds, autoplay runs a recovery phase (`RecoveryTask`)
instead of a farm circuit, whether or not the analyzer ranks a spot.
When the switch is due, the circuit's discretionary trips end at once
(upgrade sweep, spell purchase, utility and recall shopping, savebed
rebinding). The exceptions are a shop trip still settling a purchase and,
while she carries unpaid stock, the one trip paying for it: those finish
paying first.
Essential trips finish first (food, bank, identify, storage, the cure and
its funding), and so does a fight on the farm map, under the farm's own
combat and emergency recall. Meanwhile the farm pulls no new target
(`FarmTask.hold_new_pulls`): it finishes the engaged one and fights what
comes at her; if farming turns safe again before the switch, the hold
lifts. The wait lasts at most `HANDOVER_SECONDS` (120 s), or
`HANDOVER_UNPAID_SECONDS` (300 s) while she still carries unpaid stock,
so a payment is not cut off. A fight on the road is no reason to wait,
since waiting would walk her on along the farm route. Each tick the
recovery phase:

- while she is hit, fires the emergency recall by the farm's own rule,
  then heals, takes the low-health retreat, or defends where she stands.
  A walk or errand under way defends itself. There is no farm route to
  resume;
- heals and eats. Hunger with nothing to eat is autoplay's temple food
  trip, as before;
- off her savebed with nothing running, walks home on a strict route.
  Where none exists it takes the ordinary route, which only costs learned
  hazards more, and records `recovery-home-unsafe-route`. Recovery
  usually starts on the farm, and live no strictly safe route led home
  from any Burnt Forest map. Errands start only from her savebed, and the
  cure and funding trips stay strict;
- depleted: the strict temple cure when she can pay (a depletion potion
  first); otherwise the cure-funding path (identify-all, junk sale,
  deposit, the apartment chest);
- otherwise waits at her savebed.

Recovery never shops for recall: no recall is bought with the cure's
money, and recall shopping is the farm circuit's opportunistic trip.

It never walks a farm route, rebinds her savebed or makes a discretionary
purchase. Its trips are the farm circuit's own, with their retry
deadlines (`FarmCircuitTask._tick_running_service`). Once nothing is
unsafe and nothing is under way, it records `farming-resumed`, and
autoplay starts a circuit again.

The dashboard reads its alerts from live state on every poll, so each one
clears as soon as its condition does:

- `farming-suspended` (warning), with the live reason, in any autoplay
  phase;
- `cure-unfunded`: depleted, short of the fee, and no funding trip under
  way (one that is merely due does not hide it). Operator assist can give
  her the gold;
- `cure-unreachable`: three strict temple trips in a row refused for want
  of a safe route from her savebed. Each refusal is a recorded decision
  (`cure-route-refused`, reset by `cure-route-cleared` or
  `farming-resumed`), so the count survives Stop and restarts;
The recall alerts are no longer recovery blockers; see the
[owner decision of 2026-11-22](#a-working-recall-is-never-a-blocker-owner-decision-2026-11-22).

The `stuck` alert is left out only while she waits at her savebed on one
of these blockers. A cure walk or other errand that stalls still raises
it.

Before a farm circuit walks out, it examines every recall wand of unknown
charge and waits for the answers (or the 5 s timeout). The empty mark is
kept by item tag, and live, each life brought new tags for the same two
wands (most likely her checkpoint relogs), so she walked back out and
fired them again. Nobody sells recall charges: recharging needs the
recharge spell, so an empty wand is replaced, never refilled.

## A working recall is never a blocker (owner decision, 2026-11-22)

Live, on 2026-11-22 (v1.87.0), both of Sera's recall wands were empty.
From level 18 that suspended farming, and the recovery phase shopped for
a replacement: the cheapest recall the merchant had cost 9,595 copper
("recall shopping failed: merchant stock costs 95s 95c, wallet 24s 13c,
reserve 10s"). With farming suspended she could not earn it, and she
cycled errands for about three days without XP. The `recall-unfunded`
alert never fired, because it compared her wallet with the fixed 1,250
copper recall shopping needs to start, not with the real price.

The owner's decision: "A working emergency recall should never be a
blocker. They should be used when possible and needed but not relied on
(eventually she can find a reusable horn or rod etc)." It supersedes the
recall parts of the 2026-11-17 decision above and of the care-crossing
rules ([ROUTING 4.5](docs/ROUTING.md)):

- **Farming goes on without one.** A missing working recall is not a
  reason in `farming_unsafe_reason`; depletion at the temple threshold
  still is. The emergency recall is unchanged: when she owns a working
  source and needs it, she fires it.
- **Recall is bought opportunistically.** From level 18 the farm circuit's
  recall shopping runs between fights, like its other maintenance trips,
  only while she can afford it: with a recall quote from the last day
  (`recall_quote`, the merchant's cheapest recall stock, kept in her
  vendor quotes as `recall-source`), only with that price plus the 1,000
  copper reserve the purchase keeps (`RECALL_SHOP_RESERVE`); without one,
  with the 1,250 copper minimum to go and look. A quote older than a day
  no longer holds her back, since merchant stock changes. At the merchant
  she tries each recall choice in turn and buys the first she can pay for
  (`BuyDialogueStockTask(try_alternatives=True)`); when none is
  affordable the cheapest quote is recorded. The recovery phase never
  shops for recall, and recall shopping is not an essential trip, so it
  never crosses an owner block.
- **Care crossings do not wait for a recall she cannot ready.** An
  essential trip's owner-block crossing, and a food trip's, wait for a
  ready recall only when she owns one she could ready
  (`care_recall_ready`): known-empty wands, unidentified or cursed
  devices, and a recall spell costing more than her whole mana bar do not
  count. Without one she crosses with the rest of the care rule: full HP
  and SP and hold-fire, or for a food trip hold-fire alone. The override
  says which (`no working recall`, `hold-fire only`).
- **Alerts.** In any autoplay phase, from level 18 and while no recall
  source of hers works (`recall_alerts`), none of them critical:
  - `recall-unfunded` (warning): the quoted recall plus the reserve costs
    more than she has; the text names both amounts and says she farms on;
  - `recall-unavailable` (info): the last recall shopping failed or bought
    nothing, or getting the ship key it needs failed, within a day. The
    reason is kept in her persisted state (`recall_unavailable`), so it
    survives Stop and restarts, and is cleared once recall works again (or
    below level 18);
  - `recall-missing` (info): otherwise.
- **Safety without a recall** stays with the existing rules: healing, the
  low-health retreat and flee thresholds, the pack-split retreat, and the
  routing danger weight, which already plans with the safety λ while she
  has no ready recall (ROUTING 4.5). Preferring the analyzer's safety
  objective, or tighter retreat thresholds, while she has no working
  recall is a possible follow-up, not part of this change: switching the
  objective re-ranks spots each time a wand's charge is learned.

## Operator assist: a held, idle square (owner decision, 2026-11-16)

Sera needed 2,865 copper for the temple's remove-depletion prayer and had
1,038 banked. The owner's answer is the owner-only `operator-assist` command
([README](README.md#operator-assist)): an operator character on her account
drops one gold coin on her square, at most once a day, and she picks it up.
It never runs from autoplay or a task, and while it runs nothing of hers
fights or moves:

- it refuses unless she is idle at a safe square: no task, no idle food
  trip, no target, combat off, no hit on her in the last 10 seconds, health
  at least 90% and food at least 300. Stop autoplay first;
- during the run `TaskEngine.operator_hold` skips every engine tick: no
  task, no idle survival (#97) and no idle safety policy, so the food-trip
  guard cannot walk her off the square the coin lands on. The hold usually
  lasts seconds; the step timeouts bound it to under three minutes (a
  food loss of about 40), and it is always released, also when a step
  fails or the run is cancelled;
- the run aborts, and releases her at once, if she gets a target, combat
  turns on, something hits her or her health falls below 90%;
- dashboard task requests other than Stop, and manual actions, are refused
  while it holds her.

The money still pays for the depletion removal through the ordinary temple
service. Her dashboard and metrics keep an `operator-assisted` marker from
then on, so later progress is not counted as earned.

## Approach danger: known limitations

Since analyzer v3.10.0, autoplay prefers spots whose approach from her
savebed is known and fully safe (see the README's "Farming analyzer").

- **Navigation does not follow the analyzer's approach.** The analyzer
  charges the maps on its own least-dangerous path (`approach_maps`), but
  `NavigateTask` plans its own shortest route around known hazards. Checked
  on the real graph from her savebed on `world_7_79` (16, 3) to the ten
  Burnt Forest circuits she now prefers: every route crosses 1 to 6 surface
  maps outside `approach_maps`. Most are harmless (`world_7_78` has no spawns;
  `world_15_75` and `world_15_76` are weaker than the charged maps), but some
  are stronger than the charged worst map. For example, the route to the
  `world_17_73` circuits crosses `world_16_73` (spot danger 81) and
  `world_15_73` (47) instead of the charged `world_16_74` (56). All of these
  stay within one health bar at 197 HP, so the preference still holds there,
  but in general the approach safety describes a path she may not walk.
  Following `approach_maps` in navigation is not built yet.
- **Bed rebinding can re-rank in a loop.** Autoplay moves her savebed toward
  the chosen circuit, the move changes the approach origin, and the new
  origin starts a new analysis that may prefer another circuit, whose bed
  the next trip moves to. Runs stay at least ten minutes apart, and an
  entrance pack like the Underground City's is charged from every origin,
  but the loop is not otherwise damped.

## Next phase: storm farming (owner decisions, 2026-10-22)

At Wizardry 15 the single-target restriction should give way to area spells.

- **Purchase.** Buy both firestorm and icestorm from Dunowen in the Wizards'
  Tower (Daffodil, `world_17_2_2` at (5,9)), 5 gold each.
- **Element choice.** Cast whichever spell the monsters are not immune to.
  Ice golems are cold-immune and fire-weak; lava golems are the reverse.
  Firestorm is the default.
- **Play style.** Pure caster: rest to full mana between packs.
- **Spell facts.** A 90° cone about 5 tiles deep, aimed at the exact selected
  target. 13 SP at Wizardry 15, about 1 s to cast, about 11-13 damage per
  monster at Wizardry 15 with Pow 13.
- **Analyzer AoE profile.** Inputs: `--damage-per-attack`, `--attack-delay`,
  `--mana-cost`, `--mana-regen`, `--max-mana`, `--aoe-radius`,
  `--attack-type` and `--simulate-multiple-enemies`.
- **Candidate area.** The Old Outpost. `old_outpost_0101` has 10 ice golems
  (L12) and 11 lava golems (L13); deeper maps go to L27-30.

Open questions:

- The route to Lyondale.
- Storm damage against golem HP: how many casts a pack needs, and whether
  she can afford them from one mana bar.
- Whether one cone can hit the same monster more than once.

## Non-goals

- No change to melee kiting against a target that was not pulled. While
  Wizardry trains, the pack-split retreat and emergency recall changed as
  described under "Caster kiting".
- No path-level avoidance of archer ranges. The extended radius applies to
  pull-square clearance and the lone-pull check only; patrol avoidance and
  threat detection still use authored radii.
- No AoE behaviour yet. Firestorm already lifts the single-target Wizardry
  filter once she casts it, but pulls still require a lone target.
- No hiding of set-aside monsters from threat handling. A pull set-aside
  hides a monster from target choice only; it stays a threat and is released
  only when it expires or the monster arrives within 2 tiles of her.
