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
pull is possible. Otherwise her single-target kit ranks with
`--max-aggro-pack 2` ([Safer spot choice](#safer-spot-choice-2026-11-23)).

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
  spots on the road map; so does an emergency recall on the way with three
  or more attackers at once (`ROUTE_PACK_ATTACKERS`), and no other recall
  on the way (see [Safer spot choice](#safer-spot-choice-2026-11-23)).
  Live, 22 such approach failures at the underground city had not counted. Records
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
    (`map_damage` above 0); toward kit hazards it counts only with three
    or more attackers at once.
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
  healing resumes alongside the retreat. Below the flee threshold, heals
  that keep losing start the
  [heals-losing disengage](#heals-losing-on-the-farm-2026-11-23).

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
which supersedes it; depletion is the only case left, and an unaffordable
cure no longer holds it for good (the
[owner decision of 2026-11-23](#an-unaffordable-cure-is-never-a-blocker-owner-decision-2026-11-23)):

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
  from any Burnt Forest map. From combat level 18
  (`SWAMP_TRANSIT_LEVEL`), the level the farm circuit's own return does
  (`farm-return-threat-fallback`), that ordinary walk may also cross
  hazard maps when no hazard-free route exists
  (`navigation-threat-fallback`): pack-hazard maps, and with them learned
  ranged and over-level maps; only `KNOWN_LETHAL_TRANSIT` stays closed.
  Once, when such a walk is created, its plan is checked: if it takes
  that fallback, the walk heals below 85% and, below 70%, stops and steps
  away from what hits her, as the recall shopping and cure trips do. If
  she stands on a map without hostile spawns she first rests there, with
  her usual healing and defence, until her HP and SP are full, for at
  most `HAZARD_REST_SECONDS` (600 s), and records
  `recovery-home-hazard-rest`. On a map with hostile spawns she goes at
  once, since staying is worse. A replan during the walk never rests
  again; the next walk, after `HOME_RETRY_SECONDS`, is checked afresh.
  Already home (a death during the rest respawns her there), she does
  not rest. The rest, its record and the home-unreachable streak are the
  recovery task's own memory: a rebuilt task (autoplay's handover, Stop
  and Start, a restart) may rest and record again, and starts a new
  streak.
  The cure trip never crosses a known hazard while she is depleted, and
  waits instead. The walk home cannot: errands, the cure among them,
  start only from her savebed, so waiting off it would leave her uncured
  and unrested forever.
  Live, from 2026-11-18 (v1.81 to v1.87), recovery began at the Asteria
  sage (`world_4_47`), where her recall trip had taken a careful route
  out, and every way back to her Clearhaven savebed (`world_7_79`)
  crossed pack-hazard maps (`world_0_50` and `world_0_51` from the sage,
  `world_5_50` and `world_5_51` from `world_6_44_1`, then the Brynknot
  ship): 3,052 walks home failed, one every two minutes, until v1.88.0
  stopped suspending farming for recall. A failed walk retries after
  `HOME_RETRY_SECONDS`; the third in a row records
  `recovery-home-unreachable` once. Errands start only from her savebed,
  and the cure and funding trips stay strict;
- depleted: the strict temple cure when she can pay (a depletion potion
  first); otherwise the cure-funding path (identify-all, junk sale,
  deposit, the apartment chest); and once none of it is left to start,
  the [cure-funding farm](#an-unaffordable-cure-is-never-a-blocker-owner-decision-2026-11-23);
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
  her the gold. Critical while she is parked, a warning while she farms
  to fund the cure (see the
  [owner decision of 2026-11-23](#an-unaffordable-cure-is-never-a-blocker-owner-decision-2026-11-23));
- `cure-unreachable`: three strict temple trips in a row refused for want
  of a safe route from her savebed. Each refusal is a recorded decision
  (`cure-route-refused`, reset by `cure-route-cleared` or
  `farming-resumed`), so the count survives Stop and restarts;
- `home-unreachable`: three walks home in a row failed, with the latest
  error, while she is off her savebed. She keeps retrying, which is
  harmless: a learned hazard expires, but a content hazard such as a pack
  map lifts only when her level changes (below level 18 the walk never
  crosses one). Reaching her savebed (a respawn, say) clears it at once;
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
- **Without a recall, safety comes from not engaging, or starting
  optional trips, when out of heals.** The recall at 35 % used to pre-empt
  the worst case from level 18. Live, on 2026-11-22, with no heals and no
  recall, she took the mana-crystal-upgrade errand into the Old Outpost
  and sat at "critical health 15% and no usable heal" for 45 s until she
  died among troll guards, a stone giant master guard and a lava golem.
  So, from level 18, while she has neither a usable heal (an item, or a
  healing spell she has the SP for) nor a ready recall
  (`out_of_heals_and_recall`) and is below `UNPREPARED_HEALTH` (90 %,
  `unprepared_for_trips`):
  - **No new pull.** The farm fights only what is beside her or already
    coming at her, with its defence as ever. Otherwise she rests where she
    stands, combat off, recorded once as `unprepared-rest`
    (`FarmTask._unprepared_rest`). The circuit's dwell and empty-sweep
    leg switches wait meanwhile, so she makes no such unprepared walk to
    another map; a leg that stops being available on its schedule (at
    nightfall, say) still moves her.
  - **No optional trip.** The farm circuit starts no ship-key trip, spell
    purchase, or recall, utility or upgrade shopping (checked with the
    farm's own safety policy), and autoplay starts no mana-crystal detour
    (its start, recovery or upgrade), whether a circuit runs or not: each
    is postponed, not rested for (`_start_mana_crystal_detours`). Autoplay
    still starts the farm circuit, where the farm's rest keeps its
    defence. Essential trips are not delayed: temple food, the recovery
    phase and its cure, and the circuit's food, bank, identify, storage
    and savebed trips. The one optional trip the hold leaves alone is the
    [healing-potion restock](#healing-potion-restock), on its own rules.
  - **At most ten minutes.** The farm's rest and the circuit's optional
    trips share one hold, owned by the circuit (`UnpreparedHold`), so a
    rebuilt farm does not restart it. It lasts at most
    `UNPREPARED_REST_LIMIT_SECONDS` (600 s) of checks without a gap: then
    she goes on as before, recorded once as `unprepared-rest-timeout`,
    since damage over time can outpace regeneration. A gap of more than
    `UNPREPARED_GAP_SECONDS` (10 s) without a check, such as an essential
    trip, starts the hold afresh, and so does being prepared again. A
    farm rest checked within the last 3 s, with no circuit trip running,
    is an explained wait, so the dashboard's stuck alert stays quiet
    meanwhile, and only then.

  Each rest otherwise ends by itself: health regenerates passively, her
  SP buys a heal again, or a recall becomes ready. Below level 18, and
  with a heal or a ready recall, nothing changes. The routing danger weight already
  plans with the safety λ while she has no ready recall (ROUTING 4.5).
  Follow-up, not part of this change:
  preferring the analyzer's safety objective while she has no working
  recall (switching it re-ranks spots each time a wand's charge is
  learned). The in-fight escape on a walk is
  [below](#critical-escape-on-a-walk-2026-11-22), and so is the
  [healing-potion restock](#healing-potion-restock).
- **Recall alerts use fresh quotes only.** A recall quote older than a
  day (`RECALL_SHOP_INTERVAL`) is no price: it neither gates recall
  shopping nor raises `recall-unfunded`. When an alternative's quote never
  arrives, the cheapest price she could not pay is still recorded. A
  recall spell costing more SP than her whole bar is not a recall source.

## An unaffordable cure is never a blocker (owner decision, 2026-11-23)

Live, on 2026-11-23 (v1.92.0), depletion 7 held farming. The cure cost
2,865 copper, she had 2,245 with her bank balance, nothing she carried
would sell, and the chest trip that would have funded it failed
("storage container contents did not load"). The `cure-unfunded` alert
went critical and she parked at her savebed for good: the same deadlock
as the recall one above. The owner's decision: an unaffordable cure must
not block her either.

- **Farming funds it.** When the recovery phase, at her savebed, finds
  depletion due, the cure short of her wallet and known bank balance (the
  funding path's own measure, `_cure_shortfall`, with no margin), and no
  funding trip to start (identify-all, junk sale, deposit and the chest
  trip each found nothing or failed and wait their retry), it records
  `cure-funding-farm` and ends. While that runs (`cure_funding_farm`),
  `farming_unsafe_reason` lets her farm. An unknown bank balance never
  starts it: the bank visit comes first.
- **A careful farm.** It follows the analyzer's safety ranking alone,
  never the chosen objective, by the ordinary rules (quarantine, route
  deaths, kit hazard), except that when every usable spot is hazardous for
  her kit it does not farm the least dangerous one but waits (the
  2026-11-17 spiral farmed exactly that fallback). It takes only a spot
  with a safe approach (never `risky_approach`) whose analyzer danger is
  under half her maximum health (`FUNDING_FARM_DANGER_SHARE`) and under
  every ranked spot rated hazardous for her kit (the kit hazard's own
  three-failure rule), whether or not that spot is quarantined or behind
  a route death: the spot her kit just failed on still says what is too
  much. With none left, it waits. Single failures never cap anything;
  they only order the rest: the spot with the fewest recent failures
  comes first (`recent_kit_failures`: her failures in the kit window,
  24 h, with the kit hazard's kit matching, any cause, emergency recalls
  on the way included, each counted for the maps of its spot's record and
  for the map it happened on). Then the safest, then the least dangerous
  by the analyzer's own danger, not the safety view's order. That score
  is her maximum health over a spot's danger, capped at 1, so every spot
  under her health ties, and the analyzer orders the ties by its balanced
  score. The safety view is therefore kept 250 deep
  (`FarmAnalyzer.SAFETY_TOP`; the other views keep 40).

  The funding farm skips the depleted strict town check
  (`town_safely_reachable`), which is statically unsatisfiable in the
  east: every way back from the eastern candidate maps crosses a content
  pack-hazard map (`_known_pack_hazard_maps`), so the check only walked
  her down the list one spot per tick before falling back to the first.
  Her strict town trips from the farm fail for the same reason; she goes
  home for them (below).

  Live, on 2026-11-23 (v1.96.0, level 36, 322 health), 126 of the 199
  ranked spots tied at 1, and the first 40 were all Burnt Forest
  permutations (danger 71 to 126). Each shared a map with the circuits
  where she had three or more pack and out-damaged failures that night,
  so the kit hazard rejected all 40 (the 04:08 death quarantine and the
  route deaths had expired), and she waited for a spot ("no farm spot is
  usable yet"). Equally safe spots with far lower danger, the Underground
  City's outer maps, Hemlock Cave and Eld Woods Island (danger 36 to 66),
  ranked 60th to 125th. In a replay of that state with her keys, the cap
  is 49.1 (the least dangerous hazardous Burnt Forest circuit); the orc
  pack recall at the Underground City entrance at 02:43 puts Hemlock Cave
  (35.8) ahead of the entrance (35.7), and four route recalls on
  `world_15_74` put it (31.2) behind both, so she farms
  `hemlock_cave_0102` (danger 35.8, monsters level 29 to 32). Every other
  rule still applies, so a spot that just killed her, or one hazardous
  for her kit, is never taken. The deeper view also reaches the depleted
  fallback outside the funding farm (parked, or farming by hand): safety
  spots ranked 41st to 250th are tried, in rank order and with the strict
  town check, before the chosen objective. The analyzer should break
  those ties by danger itself; until it does, this keeps the funding farm
  off the deadliest tied spots.

  It makes no optional trip (savebed rebinding, upgrade, spell, utility
  and recall shopping, the ship key) and no mana-crystal detour. The one
  exception is the [healing-potion restock](#healing-potion-restock),
  which otherwise waits while depletion is due: a depleted farm without
  heals is the 2026-11-17 death spiral. On the funding farm it buys only
  up to `HEAL_RESTOCK_LOW` (2), the least that makes her prepared, and
  takes only a strict route, deferred (`heal-restock-deferred`) when
  there is none. The unprepared hold and the circuit's maintenance trips
  (food, bank, identify, storage, junk sale, the chest) run as ever.
- **Home for errands.** A strict town trip that cannot start from the
  funding farm's spot (a bank deposit, junk sale, identify-all or the
  apartment chest refused for want of a strict route, or a deferred heal
  restock) sends her home (`cure-funding-farm-home`,
  `send_cure_funding_farm_home`). Meanwhile `cure_funding_farm` is false,
  so farming is unsafe ("home for errands from the cure-funding farm"):
  autoplay hands over to recovery as usual, which walks her home and runs
  its funding errands (identify-all, junk sale, deposit, the chest's
  funding trip) from her savebed. With none left to start and no trip
  under way, the same farm resumes (`cure-funding-farm-resumed`), its
  start and deaths kept. The heal restock and the apartment storage trip
  are not recovery errands, so on resuming the strict-route retries of
  the trips a break is for (heal restock, sale, deposit, identify-all,
  storage; `CURE_FUNDING_FARM_HOME_TRIPS`) are cleared: the circuit's
  first maintenance ticks run them from her savebed, where strict routes
  exist, before she walks out. Longer backoffs, such as a full chest's
  6 h or a failed trip's, are kept. A break lasts at most
  `CURE_FUNDING_FARM_HOME_SECONDS` (30 min), and another starts no sooner
  than that after one began: a refusal meanwhile keeps the trip's own
  retry. A break that completed no town errand (sale, deposit,
  identify-all, storage, or a heal restock that bought something) since
  the break before is
  fruitless; after `CURE_FUNDING_FARM_HOME_FRUITLESS` (2) fruitless
  breaks in a row she stops going home for the rest of that funding farm
  (`cure-funding-farm-home-given-up`) and farms on, with the warning
  `funding-farm-town-unreachable`. A new funding farm starts afresh. The
  break state is in memory only. Its `cure-unfunded` alert is a warning.
  Live, on 2026-11-23, no town service was strictly reachable from
  `hemlock_cave_0102`: from the farm she could never sell, bank or
  restock heals.
- **Until she can pay.** Affordability is read live on every tick: as
  soon as the coins she carries, or they and her known bank balance, pay
  for the cure, farming is unsafe again,
  autoplay hands over to recovery as usual (essential trips and a farm
  fight finish first) and recovery takes her to the temple. Autoplay
  records `cure-funding-farm-funded`, but the farm and its deaths stay
  until the cure: should she drop short again before the temple, she
  farms on with the same count. It ends (`cure-funding-farm-ended`) once
  depletion is no longer due (the client's own depletion reset ends it
  too, even while autoplay is stopped), or
  `CURE_FUNDING_FARM_KEEP_SECONDS` (24 h) after it was funded without a
  cure. Only then is a later shortfall a fresh decision.
- **No death loop.** A death while funding is no reason to stop: depletion
  is already due, and the fee grows only with her level (65 copper a
  level, `_depletion_service_cost`), not with the points. After
  `CURE_FUNDING_FARM_DEATHS` (3) deaths since the start she stops,
  autoplay records `cure-funding-farm-backoff`, and recovery parks her
  with the critical alert again. Only after
  `CURE_FUNDING_FARM_BACKOFF_SECONDS` (24 h) may another funding farm
  start, with its deaths counted afresh.
- **Persisted, so it survives Stop and restarts.** Its state is her
  character memory (`GameState.cure_funding_farm_*`: start, deaths
  counted and up to when, when it was funded, backoff end), not the bounded
  decision history, from which a long farm's start and deaths would
  scroll out. Deaths are counted from the `death` decisions and folded
  into the persisted count every autoplay tick. The `cure-funding-farm`,
  `cure-funding-farm-funded`, `cure-funding-farm-ended` and
  `cure-funding-farm-backoff` decisions are its audit trail.
- **Alerts.** While she farms for it, `cure-unfunded` is a warning naming
  both amounts and her deaths while funding; it is critical again while
  no farm spot is usable and she waits for one (an explained wait, so no
  `stuck` alert), and while she parks after the backoff (with the time to the next try). Operator assist is
  unchanged and still offered in the text.

## Healing-potion restock

Live, on 2026-11-22, Sera died at critical health with no usable heal: no
trip bought healing items, and being unprepared only made her rest. The
farm circuit now restocks them (`FarmCircuitTask._needs_heal_restock`),
so she is rarely without a heal:

- **When.** From level 18 (`RECALL_REQUIRED_LEVEL`, where being out of
  heals and recall starts to hold her), when she carries fewer than
  `HEAL_RESTOCK_LOW` (2) usable healing consumables (`heal_consumables`):
  identified, paid, neither cursed nor damned potions and balms named like
  the heals her safety policy applies (`healing_patterns`), and scrolls
  only while she has both literacy and wizardry spells, which reading one
  needs (`types/scroll.c`). Her safety policy uses the same test
  (`usable_heal_item`) for its heals, so she is "unprepared" exactly when
  the restock sees no heal: an unreadable scroll, an unpaid, cursed or
  unidentified potion or a spellbook is no heal to either. Healing spells,
  rods and wands are not counted, on purpose: the restock is the backup
  for when her SP or charges run out. Not with depletion due (the recovery phase
  comes first), except on the cure-funding farm, not above the food trips' load limit (80 % of her carry
  weight), and only with the money for one item plus the 1,000 copper
  reserve (`HEAL_RESTOCK_RESERVE`): with a healing quote from the last day
  (`heal_restock_quote`, vendor quote `heal-restock`), its price plus the
  reserve; without one, 1,250 copper to go and look.
- **Where.** The nearest merchant that may sell her one
  (`heal_restock_vendors`): authored healing stock she can use (the
  Asteria merchant's scroll of minor healing) before stock rolled from
  `random_potion` (balms of minor or greater healing), a route over its
  budget after every other, as recall shopping ranks its vendors. When
  no route without a threat fallback reaches one, as from the Burnt
  Forest and Brynknot (Asteria lies behind the crocodile road), the same
  ranking runs over routes that need the hostile-transit fallback, which
  recall and utility shopping's trips may also take; a merchant no route
  reaches at all is never chosen. Out of heals and recall
  (`out_of_heals_and_recall`), such a hostile-road trip departs only at
  full HP and SP, checked once before it sets out (no re-wait when its
  route is re-planned). Until then the farm pulls nothing new
  (`FarmTask.rest_for_departure`, combat off once per rest; whatever is
  beside her or coming at her is still fought), the trip is re-planned
  every 30 s, and after 10 minutes without getting full she backs off for
  two hours. A rebuilt circuit keeps the 30 s re-plan pause but starts
  the ten-minute wait afresh (a recovery phase in between would otherwise
  count against it). The wait ends without a backoff once the restock is
  no longer due, and is an explained wait for the dashboard's stuck
  alert. After a fight during the rest, combat off is sent once more.
  The walk keeps the trip's safety policy (heal at 85 %, flee at 70 %).
- **What.** `BuyHealingStockTask` asks the price of every healing choice
  she can use and buys up to `HEAL_RESTOCK_TARGET` (5) of the one with
  the most heal per copper (`heal_estimate`: the server's minor and
  greater healing rolls at the item's level), as many as the reserve
  leaves. Heal per copper favours cheap minor heals over a bigger greater
  heal; that is intended, more uses for the money. The merchant sells at
  most what it stocks, so what arrives is counted: the purchase record
  says that many, and the bank reservation for the rest is given back.
  The cheapest price is recorded. A merchant answer that she cannot carry
  them ends it at once.
- **An optional trip, not an essential one.** It never crosses an owner
  block, keeps the errand budget and planning with the safety λ, and is
  dropped when farming turns unsafe (`DROPPED_AT_HANDOVER`). Being without
  a heal is when trips are most dangerous, so making it essential (care
  crossings and route overrides) would send her through the worst routes
  exactly when she can least afford a fight.
- **But the unprepared hold does not hold it.** It is the cure for being
  unprepared, so it is the one optional trip that may start while the
  hold holds, on stricter rules: only by a strict route (`strict_safety`,
  no hostile-transit fallback), and only from its flee threshold (70 %
  HP) up; below that the farm's unprepared rest regenerates her first.
  With no strict route she tries again after 10 minutes
  (`heal-restock-deferred`), not a day. Once the hold's ten minutes run
  out, it goes like any optional trip, ahead of the others. Prepared (a
  ready recall, SP for a healing spell, or 90 % HP), it is an ordinary
  optional trip. Never while depletion is due, except on the
  [cure-funding farm](#an-unaffordable-cure-is-never-a-blocker-owner-decision-2026-11-23).
- **No loops.** A merchant with no healing stock she can use, stock that
  ran short of the low mark, or no merchant at all backs her off for a
  day; a failed trip (she could pay for none, a route declined, a
  dialogue that did not answer) for two hours. The backoff and its reason
  are kept in her persisted state (`heal_restock_note`,
  `heal_restock_retry_until`), recorded once as `heal-restock-backoff`,
  and shown on the dashboard as the `heal-restock-unavailable` info
  alert while she still lacks heals. A purchase that reaches the low mark
  clears it, and the next restock waits at least two hours
  (`HEAL_RESTOCK_MIN_INTERVAL`), without an alert: at high level one fight
  can use all five, and restocking at once would empty the merchant's
  stock in back-to-back trips. A strict route over its budget is
  declined like any optional trip's and counts as a failed trip (two
  hours).

## Critical escape on a walk (2026-11-22)

The death that led to the unprepared rest above, read from her post-mortem
and journal (v1.87.0): starving (food 0, nothing to eat, "no temple priest
is reachable"), with 2 of 82 SP and no recall, she was on the
mana-crystal-upgrade errand's defended walk (`NavigateThenTask`, safety
thresholds 85 % heal and 70 % flee). From 23:30:37 UTC she logged
"critical health ... and no usable heal" every 2 s while she went on
through the Old Outpost from 69 % to 1 %, hit about every 15 s. Two gaps:

- Starving with nothing to eat makes resting futile, so the walk's
  low-health hold let her walk on (`starving_without_food`). That also
  skipped its retreat: she walked on among the troll guards instead of
  away from them.
- Her last 12 s, at 7 % down to 1 %, she stood in a doorway of
  `old_outpost_0202` (17, 9) choosing two diagonal retreat steps in turn
  every half second (`navigation-retreat-choice`, directions 4 and 6),
  neither of which landed. `_retreat_step` replaced a step in flight with
  a new one before `movement_ack_timeout` (at least 1.5 s), so neither was
  ever learned as blocked.

Now, on a defended walk:

- **A retreat step waits for its answer.** While one is in flight,
  `NavigateThenTask._retreat_step` (and `_unseen_attacker_retreat`, which
  shares its bookkeeping) waits for it, as the farm's
  `retreat_ack_pending` does. A step still where she sent it at the
  acknowledgement timeout is learned as blocked, and the next choice is
  another square. Once she stands anywhere else (pushed, teleported, a
  map change) the step is forgotten, not learned. A learned square is
  tried again after `RETREAT_BLOCKED_SECONDS` (30 s), and forgotten on
  another map. This is a fix for every caller of the walk's retreat: its
  own defence (`_defend_while_navigating`, on its own walk and as the farm
  circuit's transit legs, route-death hold and failed-route escape, and the
  recovery phase's defence call it) and the circuit's failed-route escape,
  which calls `_retreat_step` directly.
- **Critical escape.** Only on a walk's own tick (`own_walk`), and not on
  a farm circuit's transit leg (a walk whose task is a `FarmTask`), which
  keeps its defence unchanged. While she has neither a usable heal nor a
  ready recall (`out_of_heals_and_recall`), from the safety's flee
  threshold, with an attacker within 4 tiles on her (a hit in the last
  3 s, or one within 2 tiles), she runs, starving or not: combat off, no
  pull, recorded once per escape as `navigation-critical-escape`. Before
  it come only the safety's heal and food (`SafetyPolicy.enforce`; a heal
  or meal just sent holds the escape for that tick) and the transit
  hold-fire release of an engagement. The step must take her farther
  from the nearest attacker (`gain_only`); among those, the usual retreat
  ranking chooses, open continuation first. An equal-distance step does
  not count: in a pocket it shuffled her between two squares beside a
  troll that stood still, combat off, never fighting.
- **No re-engaging until recovered.** The escape holds until she is above
  the safety's heal threshold, or has a heal or a ready recall again, and
  ends when the walk starts again. Until then she keeps running from an
  attacker on her, and closes no gap to a creature more than 2 tiles off.
- **Cornered keeps its defence.** With no step that gains distance the
  escape does not act, and the walk's defence decides (the cornered fight
  among them), against the closest attacker rather than a committed one
  farther off. The safety's low-health hold, whose equal-distance steps
  would shuffle her beside the attacker with combat off, stands aside
  meanwhile.

Accepted limits:

- once no attacker is on her, the walk's hold and route go on as before.
  Starving with nothing to eat she does not rest, so the route can lead
  her back past the attacker she escaped; giving up an errand to go for
  food is not part of this change;
- the escape sees only creatures within 4 tiles: a ranged attacker
  farther off, or one she cannot see, does not start it.

The farm map's own retreat (`FarmTask.low_health_retreat`, the cornered
breakout and the pursued stand) is unchanged here; an earlier attempt to
change them, during the review of
[pull request 117](https://github.com/atrinik/playtester/pull/117), spawned
edge cases and was reverted. Only the narrow
[heals-losing disengage](#heals-losing-on-the-farm-2026-11-23) and the
[caster and pocket rules](#casters-across-a-seam-2026-11-23) below touch
it.

## Heals losing on the farm (2026-11-23)

Read from her post-mortem, decisions and journal (v1.88.0, Burnt Forest
circuit, `world_18_73`): charred treants, level 37 and slow
(`treant_evil`, speed 0.048), took her from 89% to 19% in about 10 s,
with three of them in view (`farm-pack-split`) and two at a time beside
her late on. The circuit's adapted margins (flee 89%, heal 98%) put the
whole fight below the flee threshold, where the farm heals first and
retreats only on ticks without a heal. For the last 28 s she cast minor
healing (12-26 a cast) every ~2 s and held between 19% and 39% (62-128
of 322), while three `pursued-stand`s at 38-39%, above the 35% stand
floor, turned her back into melee. `healing-outpaced` had fired once, at
90%; below the flee threshold it does nothing. Her retreat steps mostly
gained a tile (`separation=2`), but each heal and stand in between let
the treants close again; with two of them on either side the last steps
could not gain (`separation=1`). One 66-damage burst
from two treants then killed her at 52 HP; depletion went from 0 to 7.

Now, on the farm map:

- **Heals losing** (`heals_losing`, over `OUT_HEALED_WINDOW_SECONDS`,
  12 s, from the first heal in it on): three heals with the hits since the
  first at least what the later ones restored, or four heals that never
  lifted her above `OUT_HEALED_CEILING` (50%, from her vitals samples; no
  samples, no stall). It counts only from the flee threshold down and
  under hostile contact. A fight her heals win lifts her above the ceiling
  and restores more than she takes, so it never counts.
- **Disengage once** (`FarmTask.out_healed`, recorded once as
  `out-healed-disengage` with the kinds that hit her in the window). It
  holds `OUT_HEALED_HOLD_SECONDS` (8 s) past her last hostile contact
  (a hit or miss within the last 10 s, so up to about 18 s after the last
  one), and ends at once above the heal threshold. While it holds,
  with an active attacker within 2 tiles, the farm's own retreat
  (`low_health_retreat`, `gain_only`) runs before the next heal: only a
  step that takes her farther from the nearest threat counts (the gain
  rule of the walk's critical escape), taken as soon as the previous one is
  answered rather than at the fighting retreat's 2 s cadence, with combat
  off. These steps leave the pursuit count alone. With no such step it
  does nothing, touches no latch, and healing and the usual retreat
  decide, the cornered breakout among them. With no attacker within
  2 tiles she heals as usual. Running never shuts healing out: a step
  unanswered for `OUT_HEALED_ACK_WAIT_SECONDS` (0.75 s), or a usable heal
  with none landed for `OUT_HEALED_HEAL_GAP_SECONDS` (2.5 s) and none in
  flight ([heals in melee](#heals-in-melee-2026-11-23)), lets that
  tick's heal go first, whatever the attacker's distance.
- **No stand while she runs.** The gain-only retreat runs only while she
  has a usable heal (an item, or the SP for the spell). While the
  disengage holds, she has a usable heal and a gain-only retreat found a
  gaining step within `OUT_HEALED_GAIN_SECONDS` (2 s;
  `out_healed_running`), every retreat on the farm map makes no pursued
  stand or pursued cast, a stand already latched ends, and combat stays
  off on each retreat step (`emergency-disengage`). Otherwise (no heal,
  or no way out) the ordinary retreat, its pursued stand and cornered
  breakout among them, defends her as before.
- **Out of her league** (`OUT_OF_LEAGUE_SECONDS`, 10 minutes, kept on the
  client so a rebuilt farm task still knows, and cleared for another
  character): those kinds are neither pulled nor engaged, even within the
  3 tiles where she otherwise fights anything; an engagement with one is
  dropped rather than walked back to. Only one of that kind while that
  kind is hitting her (hostile contact, its name in the hits) is fought.
  A kind matches exactly, by name or visual name, ignoring case,
  underscores and word order: a mark for "rat" is not one for "pirate"
  or "giant rat". Hits are matched by the name in their message: a
  ranged or spell hit may name its missile instead, so a marked ranged
  caster may never count as hitting her and is then not fought.

Accepted limits:

- nothing here feeds the failure review or the farm adaptations: a
  disengage she survives is not a failure, and the review rebuilds the
  farm task. A death still counts as before;
- on a circuit whose only target is that kind, she pulls nothing there
  for those 10 minutes;
- a burst from full health that ends the fight before three heals is not
  seen; the existing flee threshold and recall handle it.

## Casters across a seam (2026-11-23)

Read from her post-mortem, decisions and journal (v1.98.0, the
Underground City circuit, `underground_city_1_3`): she died at (7, 1) to
three attackers at once, with 565 damage taken in about a minute. The orc
sorcerer did 69% of it (magic bullet 259, its own cleave 132), and she has
no protection against magic. She never fought it: she swung at the orcs
beside it. The facts:

- Orc sorcerers spawn on `underground_city_1_2`, tiled to the north of
  1_3. The room at 1_3 (0-7, 0-1) runs on into 1_2's room at (0-6,
  16-23), where they stand. The farm read caster kinds only from its own
  map's authored list, and 1_3 has none. It also left every creature off
  its own map out of its threats and targets.
- The sorcerer's magic bullet is an aimed projectile. Its hits name the
  missile ("magic bullet hit you"), not the caster. The fight record
  counted no ranged attacker (`ranged_attackers` 0).
- She fought on the room side of the doorway at (8, 1), in the
  sorcerers' line. The corridor side is behind the wall.
- Her retreat steps were clamped to 1_3, so the pocket by the seam was a
  dead end. A pursued stand turned her on three adjacent attackers while
  she was disengaging (`emergency-disengage`, `out-healed-disengage`
  soon after).

Now, on the farm map:

- **Casters across a seam count.** A caster kind is an authored caster
  (`can_cast_spell`) of the farm map or of any map tiled to its sides or
  corners (`area_caster_identities`, through `terrain.tile_directions`).
  This is used for target choice, the stationary-caster orbit and the
  fight record's ranged count (`fight_positions.ranged_attackers`).
- **Caster first** (`caster_first`). A caster is fought before any melee
  attacker, the engaged one included, when it is beside her (across a
  seam too, where it then counts as a threat). It is also fought first
  when it stands within `CASTER_FIRST_REACH` (2) on the farm map and its
  name is in a recent hit or miss, provided the step toward it puts no
  more other hostiles beside her than she has now. A missile's hit
  ("magic bullet hit you") names no caster and switches no target, so
  she does not leave the orc beside her for a caster past the pack. The
  same rank comes first when she picks one attacker among several: the
  first contact she commits to, the pursued stand, the cornered
  breakout's blocker. A breakout or stand latched on a melee attacker
  switches to a caster that comes beside her.
- **Breaking a caster's line** (`break_caster_sight`). One step that
  puts an authored wall between her and the casters she is not going
  for (`wall_covers`). The line is the server's projectile flight
  (`cover_cells`, from the caster), across seams. A wall there also takes
  away the sight a monster needs to cast at all (`monster_cast_spell`,
  `server/src/types/monster.c`). Doors do not count, because orcs open
  them. Its gates:
  - an aimed spell hit in the last `COVER_HIT_SECONDS` (12 s) that is
    newer than the one her last cover step answered, so an old hit never
    starts another;
  - `caster_first` has no caster for her;
  - her engaged or selected target is no caster she covers from: if she
    is going for it, she goes for it. Cover from her own target alternated
    with the approach to it, step by step (review of
    [pull request 131](https://github.com/atrinik/playtester/pull/131));
  - at most one threat near her (the pack-split retreat's count, within
    4 tiles): with two, that retreat decides, since cover steps between
    its steps only alternated with them;
  - above the tick's flee threshold, with no heal in flight and none due
    (a usable heal at or below the heal threshold), so it never pre-empts
    a heal (`cover_allowed`), and not while she runs from losing heals.
    A safety that tracks its sent heal (`heal_in_flight`, its retry time)
    answers for that; otherwise a heal sent within the heal cooldown is
    in flight.

  The square must have strictly fewer clear lines from the visible
  casters than hers, and no more attackers beside it. Among those squares
  she takes first the one with the fewest lines, then the one fewer melee
  attackers can reach (`terrain.features`), then the one farthest from
  the nearest other hostile. The step is recorded as `caster-cover`, and
  its square is learned as blocked if it never lands. For
  `COVER_HOLD_SECONDS` (4 s) afterwards she approaches and kites no melee
  target: it comes to her. A caster she is going for she still
  approaches, orbits or kites. In the incident pocket she steps
  through the doorway (8, 1) to the corridor side, which neither sorcerer
  can see.

  Bolts that reflect off walls (lightning, negative energy bolt) do not
  follow the straight line past a wall. For them cover rests on the sight
  check alone, which the same wall takes away.
- **A seam pocket is no dead end.** The low-health retreat may step onto
  a map tiled to the farm map, but only when every step on the farm map
  is a dead end (an escape depth under 2) or there is none. The rules:
  - The square must be walkable and unoccupied on that map.
  - From it she must be able to get at least 2 squares away from the
    seam edge (`seam_escape`), since running along the seam is no escape.
  - The step must gain distance from the nearest threat.
  - The square must have no more hostiles within 4 tiles than hers.
  - No second seam step follows within `SEAM_STEP_COOLDOWN_SECONDS`
    (20 s): off the farm map the circuit's walk brings her straight back.
  - The decision is `retreat-choice ... seam=1`.
- **No stand into a losing pack.** A pursued stand that is due, against
  two or more attackers beside her while she is losing, gives way to a
  step that sheds the nearest of them (`pursued-stand-declined`). Losing
  means the retreat's own emergency disengage (at or below the flee
  threshold, short of mana for a heal, or healing outpaced) or heals
  that no longer keep up (`healing_outpaced`). This is the gain-only rule
  of [pull request 120](https://github.com/atrinik/playtester/pull/120)
  and [pull request 124](https://github.com/atrinik/playtester/pull/124):
  an equal-distance step does not count, since it
  shuffled her between two squares beside the attackers. With no gaining
  step she stands as before, caster first, so the declined stand cannot
  ping-pong. The pursuit count goes on, and the stand floor, the
  out-healed rules and the emergency recall are unchanged.

Accepted limits:

- after a seam step she is off the farm map. The circuit's walk
  (`NavigateThenTask`) then takes her back to it, and its own defence,
  not the farm's, handles the attackers that follow;
- cover is judged from the authored walls and the casters in view. A
  caster out of view, a summoned or unauthored one, or a door an orc has
  closed again is not modelled. A spell hit with no caster in view
  starts no cover step;
- the failure review still counts a missile name ("magic bullet") as an
  attacker of its own. Out-of-league marks still match only the hits'
  names, as noted above;
- the farm still neither pulls nor walks to creatures across a seam. Only
  an adjacent caster there is fought. One two squares across a seam is
  not, and the cover step treats it like any other caster in view;
- cover steps are judged at their square. She does not plan a path to
  cover, and two casters may leave no square with fewer lines.

## Heals in melee (2026-11-23)

Read from her post-mortem and journal (Underground City circuit,
`underground_city_1_3`, 19:13:27 UTC): in melee with three orcs, about
8 of some 25 minor-healing casts landed and her SP stayed at 82. Each
auto-swing sets her action timer to her weapon speed (2.25 s;
`pl->action_attack`, `server/src/types/player.c`), and
`socket_command_fire` (`server/src/socket/request.c`) returns without a
word while that timer runs. The safety policy healed every
`heal_cooldown` (1.15 s) with combat on, so most casts arrived while the
timer ran, and it counted each one as a heal. Applying an item
(`socket_command_item_apply`, `player_apply`) checks no such timer:
potions and scrolls always went through.

Now (`SafetyPolicy.enforce`, `_heal`):

- **Casts wait for the timer.** A healing spell, rod or wand (all sent as
  FIRE) first turns combat off, repeated on each tick of the wait since a
  hit turns it back on server side, and is sent only once her action
  timer has run out (`effective_action_time`), plus one server tick
  (`HEAL_TIMER_MARGIN`, 125 ms) since the client's countdown can end
  just before the server's, as the emergency recall does. The wait is at
  most one swing. It is not the heal's tick (`_heal_action` marks only a
  tick that sent a heal or readied its device): callers retreat and step
  away meanwhile, which no timer gates, but while `heal_waiting` holds,
  `low_health_retreat` makes no stand, cornered breakout or pursued cast,
  and the walk's defence holds even when she is starving with nothing to
  eat (which otherwise skips the safety hold), since each fight would
  restart the timer. A rod or wand not yet readied is
  applied once (it readies it; applying it again would unready it), then
  fired by its tag. A potion or scroll is applied at once, with combat
  left as it is.
- **Combat comes back with the cast.** Right after the fire, combat goes
  back on as it was (with its force flag): the fire set her timer before
  the combat packet arrives, so the next swing follows the cast's
  recovery and she fights back between heals as before. A heal no longer
  needed or usable during the wait (her health over the threshold, no SP)
  also turns it back on; a recall waiting for the timer (`combat_allowed`)
  keeps it off. What to restore is kept on the client, so whichever safety
  policy runs next restores it, the engine's idle one after a task ended
  mid-wait among them. Any other combat-off (a Stop, an operator's task
  change, an autoplay handover, a retreat) drops it: whoever turned combat
  off owns it (the farm turns it on again whenever it fights), and a new
  login starts without one.
- **A heal counts once it lands** (`heal_landed`). The server answers
  every heal it runs, spell, potion, scroll, rod or wand, with "The spell
  heals you for N hp!" (or "The healing spell fails!", `cast_heal`); a
  potion or scroll is also counted when its quantity falls. Only then
  does the cooldown, and the out-healed heal gap, count from it
  (`_last_heal`). A refusal with a word (no mana, a blocked square, a
  device that cannot fire now) waits out the cooldown.
- **Refused devices give way.** A healing rod or wand that "whines for a
  while, but nothing happens" or "is impossible to handle for you" is
  backed off for `RECALL_DEVICE_RETRY_SECONDS` (20 s, the recall's own
  back-off, shared with it), and a wand that "says poof" is known empty
  (`wand_known_empty`; the name in the message must be the wand's). Neither counts as a usable heal meanwhile, so the
  next heal in her pack, her spell among them, heals instead of the
  combat-off wait repeating on a device that cannot fire. A cast unanswered
  for `heal_confirm_seconds` (1 s) was dropped: it is recorded as
  `heal-unconfirmed` and sent again at the next allowed moment, without
  waiting out a cooldown.

`healing_outpaced` and `heals_losing` (the
[heals-losing disengage](#heals-losing-on-the-farm-2026-11-23)) already
count only the server's heal messages, that is landed heals. The walk's
[critical escape](#critical-escape-on-a-walk-2026-11-22) runs only with
no usable heal, and reads `_heal_action` only to leave the tick of a heal
to it. Accepted limit: casts and swings alternate, so in melee a heal
lands about every swing plus one cast (about 3.3 s with a 2.25 s weapon),
not every 1.15 s; a fight that needs more is for the disengage and the
recall.

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

## Safer spot choice (2026-11-23)

At 19:13:27 UTC she died at the Underground City circuit of
`underground_city_1_3`, `2_2`, `2_3` and `3_2`, the XP view's first spot
(v1.98.0, level 37, 330 health). The analyzer rated it danger 115.6,
safety 0.95, pack 3, with casters only from `3_2`. Of the 565 damage in
her last minute, an orc sorcerer's magic bullet did 259: three sorcerers
spawn on `underground_city_1_2`, seamlessly north of `1_3`, about seven
tiles from the doorway she fought in. The analyzer scores a spot from its
own maps alone and judges its pack per map, so it never saw them. Its
profile allowed any pack (`max_aggro_pack` 0), and the orc recall at the
entrance `underground_city_0_0` at 02:43 (three orcs at once) had not
counted toward the spot's kit hazard. Three changes:

- **Seam exposure** (`WorldGraph.seam_exposure`,
  `farm_analyzer.seam_adjusted`, `navigation.seam_ranking`). Bot-side,
  from her own world graph and the authored spawns, until the analyzer
  models it (the fix belongs there; see below). A neighbour is a map tiled
  to one of the spot's maps, side or diagonal, outside the spot; the
  circuit never clears it. At each of the spot's spawns, the pack counts
  every aggressive spawn within its own detection radius, the analyzer's
  rule now across every seam, the circuit's own interior seams included
  (the analyzer judges each map alone), plus each neighbour caster within
  `SEAM_REACH_TILES` (10) of her pull square (the spawn's detection
  radius from it). Distances ignore walls, as the analyzer's do. Counting
  every melee spawn that near too would put the open Burnt Forest at a
  pack of three to eight and leave almost no overworld spot under the cap
  below. Every ranked spot (each objective, the cure-funding farm's
  safety view, the farm-score shadow and the dashboard's Farming tab,
  farm picker and manual farm task alike) keeps the analyzer's order and
  gets: `max_aggro_pack` the larger of the two packs; seam casters in
  `danger_flags` (`seam caster×N`), with `seam_casters`,
  `seam_aggressive_spawns` (neighbour spawns within 10 tiles) and
  `seam_maps`; and a spot safety of at most her maximum health over the
  spot's danger per monster times that pack, plus 20 per seam caster (the
  analyzer's caster bonus). A spot whose pack, seam included, exceeds
  the ranked profile's `max_aggro_pack` is left out, as the analyzer
  leaves out its own. That circuit: pack 4, three seam casters (on `1_2`
  and beyond), spot safety 0.63. `1_3` alone: pack 3, two seam casters.
- **A risky spot comes after a safe one.** `farm_analyzer.risky_spot`: a
  spot whose own safety (`spot_safety_score`, seam exposure included) is
  under 1 expects one encounter to cost more than her whole health bar.
  Autoplay farms spots that are neither that nor a risky approach first,
  in rank order, as for approaches; the risky ones after them, the safest
  first, and on a safety tie (one approach capping them all) by their own
  safety. The cure-funding farm never takes a risky spot, and the
  every-spot-hazardous fallback takes the least dangerous spot that is
  not risky before any that is. The funding farm's danger cap still reads
  every hazardous spot of the raw ranking, those seam exposure leaves out
  included.
- **A pack she can hold.** Outside Wizardry training (pack 1, unchanged),
  `combat_profile` now ranks for `max_aggro_pack`
  `SINGLE_TARGET_MAX_AGGRO_PACK` (2): she fights one target at a time and
  casts no area spell. The analyzer drops a map whose pack exceeds it
  before it builds circuits, so `2_3`, `3_2`, `1_4` and the other
  Underground City maps of pack 3 or more leave every circuit.
- **Pack recalls on the way count** toward the spot's kit hazard
  (`hazard_failure`): an emergency recall on the way with
  `ROUTE_PACK_ATTACKERS` (3) or more individual attackers at once
  (`peak_individuals`). The hit text names no individual, so three orc
  warriors are one name. Within any 8 s:
  - one name's lines within one server tick (`SWING_SECONDS`, 0.125 s)
    are one swing, since the server sends a line per attack type of a
    swing (`server/src/server/attack.c`, the `NROFATTACKS` loop; a ghoul,
    wraith or lost soul has two or three types), unless two lines name the
    same type;
  - each name counts as many attackers as its most swings less than
    `BURST_SECONDS` (1 s) apart, at least one. One monster swings far less
    often: an orc's `weapon_speed` 0.035 a tick at 8 ticks a second is a
    swing about every 3.6 s;
  - only names of authored monster kinds count (`WorldGraph.is_monster`:
    a monster archetype, or a monster placed on a map). A hit named for a
    spell projectile ("magic bullet", or a firestorm's "fire", which a
    cone sends every tick it lives), an effect or a trap is its caster's
    or nobody's: one sorcerer and its bullet are one;
  - the names are summed.

  It is an estimate: same-named attackers that never hit within a second
  of each other, or that swing in one tick with the same attack types,
  count as one, and a caster seen only through its spell does not count. Each
  kit failure now keeps it; one stored before reads it from the review of
  the same time, or that review's distinct names when it predates this. The 02:43 recall and the 19:13 death make two;
  `KIT_HAZARD_FAILURES` stays 3. Route deaths counted already. Recalls
  with fewer attackers, and the route-death map rule, are unchanged.

A replay of her live state, with the locked analyzer (v3.10.0) and
content (v1.14.0) at her profile, from her savebed `world_7_79` and from
the default origins: allowing any pack, as live, the death circuit is still
the XP view's first spot, now risky (pack 4, safety 0.62), and every
non-risky spot is a Burnt Forest circuit hazardous for her kit; so she
would farm the least exposed Underground City circuit instead
(`0_4 + 1_2 + 1_3 + 1_4`, sorcerers inside it). Allowing any pack is not
enough, and a cap of 3 is not either (she would farm the risky
`1_2 + 1_3 + 2_2 + 3_2`), so the cap stays 2. Ranked at pack 2, no
Underground City circuit is left, and seam exposure removes `1_3` alone
(pack 3), `world_4_73`, `world_7_71` and two Hemlock Cave maps from the
safety view. Her XP view is then all Burnt Forest circuits; with her kit
failures there (rebuilt from the dashboard's self-improvement summary)
every one is hazardous, so she farms the least dangerous of them,
`world_17_72 + world_17_73 + world_17_74 + world_17_75` (danger 74.9,
pack 1, no casters). Without those failures she would farm the XP view's
first, `world_17_72 + world_17_73 + world_18_73 + world_19_73` (pack 2).

Accepted limits: distances ignore walls and line of sight, so a seam
caster behind rock counts; neighbour melee spawns farther than their
detection radius do not join the pack, although orcs pursue about nine
tiles; vertical links are not neighbours.

The fix belongs in the analyzer (atrinik/tools `farming_analyzer`):
judge a map's and a circuit's pack, and its casters, in one coordinate
frame with its tiled neighbours, the neighbours' spawns counted as
uncleared, so its danger, pack filter and safety score agree with this.
Then this post-adjustment goes.

## Packs on a walk: cornered and outclassed (2026-11-24)

Live (v1.101.0, the Old Outpost on the mana-crystal errand) Sera at level
37 stood 14 minutes among the Outpost's level 12-24 residents without
fighting: four level 16 lava golems in the exit room, then boxed in at
(16, 6) by walls on five sides, two level 17 stone giant master guards
and a lava golem. Two hostiles within 4 tiles made the walk's defence
(`NavigateThenTask._defend_while_navigating`, which the farm circuit's
legs also use) split the pack: clear, combat off and a retreat step,
twice a second. With no retreat square it did nothing else, so the fight
below it was never reached; 100 of the 102 attacks on her missed.

- **Outclassed creatures are no pack.** A hostile whose authored level
  (the world graph's, for this map and the maps tiled to it) is more than
  `OUTCLASSED_LEVEL_GAP` (10) below hers counts toward neither the
  pack split (two within 4 tiles) nor the pack avoidance and pack pull
  (two within 6). She still fights it, one at a time, and it still counts
  as a threat for retreats, halos and the critical escape. A creature of
  unknown level always counts. The protections against dangerous packs
  stand: creatures 4 or more levels above her are avoided as before, and
  two within the gap still split.
- **Cornered, she fights.** When a pack split finds no retreat square
  (`_retreat_step` with `probe`, which sends nothing), she fights the
  closest attacker (`_cornered_split`, recorded once per target as
  `navigation-cornered-fight`), as the farm's cornered breakout does: one
  beside her, or one farther off only while she is being hit (else she
  holds, combat off, and lets them come). The
  attacker she fights stays her target until it dies or is more than 4
  tiles off, so a square opening mid-fight, or her step toward one
  farther off, does not turn her back into a combat-off retreat. Off her
  side it stays her target only while no other attacker is beside her and
  she is still being hit. The safety's low-health hold and the critical
  escape still come first.
- **No resent hold.** A pack split that repeats the last tick's (same
  square, same creatures within 4 tiles) sends neither its clear nor
  combat off again, unless combat was turned back on meanwhile; a retreat
  step in flight is waited for, as before.

Accepted limits: the level gap reads authored levels only, not her
equipment or theirs; a retreat step that keeps her as close to the
nearest attacker (still beside it) counts as a way out, so against a
pack within the gap she may still step sideways with combat off rather
than fight (retreat scoring is not changed here); the farm map's own
pack split (`farm-pack-split` in `FarmTask`) has no authored levels and
still counts every active threat, with its cornered breakout unchanged.

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
