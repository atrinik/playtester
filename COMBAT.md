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
  threat within reach after safety, or keeps its distance. Melee remains only
  as survival: the cornered-retreat breakout, the pursued stand (an attacker
  still adjacent after three retreat steps), travel defence against an
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

- **Mana regeneration.** The server regenerates mana at `gen_sp × 60 + maxsp`.
  `gen_sp` starts at 1 and each worn non-weapon adds its regeneration, which
  item names show as `(mana reg.+0.4)` per point. One point at her 74 max
  mana is +45% regeneration.
- **Pow.** Spell damage is multiplied by `1 + spell_dam_bonus[Pow]`, which is
  flat for 11-15: Pow 13 to 14 adds nothing, and 15 to 16 adds 15%. Pow also
  adds `sp_bonus[Pow] / 2.5` of base max mana.
- **Int.** Int adds `sp_bonus[Int]` of base max mana (also flat for 11-15)
  and no damage. Its value is capped at what the same change to Pow at the
  same stat value would give, so Int never outweighs Pow.
- **Max mana** (`(mana+N)`). Worth its share of her max mana, plus the
  regeneration it adds through `maxsp`.

A percent of regeneration counts as much as a percent of spell damage (8
points), because mana bounds her kill rate. A percent of max mana counts 2.
So one regeneration point (about 358) outscores any single stat. A worn item
is valued against her stats without it, so a worn Pow+1 ring that lifts her
from 12 to 13 is worth nothing next to a candidate that would. Weapons add
no max mana or regeneration on the server, so on a weapon those count for
nothing. Shop stock whose ground name lacks the bonuses is valued from its
examine text, and is never also counted at the generic 40 per stat.

### Greyton's ring stock

From level 30, the upgrade sweep also visits Greyton's general store
(`world_9_67`). Its stock includes a difficulty-50 random-ring square, and
random rings can roll Pow, Int or mana regeneration. The road from Aris
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
  full mana, or one while a cast is still affordable.

Getting the crystal and upgrading it:

- **Opening reward.** At level 18 Gandyld gives her the 50-SP crystal.
- **Recovery.** If the quest has started and she does not carry the crystal,
  she checks the apartment chest, at most once a day.
- **The free upgrade (`ManaCrystalUpgradeTask`).** It runs from level 30
  while "A Better Crystal" is active. Her crystal's capacity is checked
  first. If it is below 100, or she has no crystal, she fights through the
  Old Outpost and applies the alchemists' pool on `old_outpost_a_0201`
  (7,8). The route's strongest residents are level 24 troll elite soldiers.
  Then she reports to Gandyld, which completes the part and starts "A
  Stronger Crystal". A failed trip waits a day before it is tried again.
- **Replacement.** Gandyld's dialogue offers no replacement crystal, but the
  pool's script creates the enhanced crystal whether or not she carries the
  old one. So the pool trip also replaces a lost crystal.
- **Not done: the second upgrade (200 SP).** That pool is in King Rhun's
  vault (`old_outpost_a_0204`), behind a locked door. The key is carried by
  King Rhun, level 30, among level 25-30 guards. The well entrance needs an
  orange stone giant key. The task stops after the first upgrade, and the
  quest policy's vault step is left for a later change.

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

- No change to melee kiting against a target that was not pulled, to the
  pack-split retreat, or to emergency recall.
- No path-level avoidance of archer ranges. The extended radius applies to
  pull-square clearance and the lone-pull check only; patrol avoidance and
  threat detection still use authored radii.
- No AoE behaviour yet. Firestorm already lifts the single-target Wizardry
  filter once she casts it, but pulls still require a lone target.
- No hiding of set-aside monsters from threat handling. A pull set-aside
  hides a monster from target choice only; it stays a threat and is released
  only when it expires or the monster arrives within 2 tiles of her.
