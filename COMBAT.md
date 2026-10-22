# Combat: one-on-one pulls, then storm farming

This note describes how the bot fights aggressive residents today, why it
changed, and what comes next. It covers the farm loop in `tasks.py`
(`FarmTask`) and the pull squares that `navigation.py`
(`FarmCircuitTask._new_child`) computes for it.

## The problem

Sera (character level 28, slash weapons 28, Wizardry 10) trains Wizardry with
magic bullet as a finisher. In a live session she died 7 times in 40 minutes,
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
resident on the map; unknown monsters default to 4. Authored-peaceful
creatures are ignored, since they never join a fight. `ranged_pull` and the
single-target Wizardry filter both use this one predicate.

### Clear pull square as the kite anchor

`_new_child` searches the perimeter of each target spawn's `aggro_radius` for
a pull square. Clearance is now measured against every aggressive resident
of the map. Residents whose names contain "archer", "bowman", "slinger" or
"mage" (`RANGED_RESIDENT_MARKERS`) are given a radius of at least 6, because
they shoot from outside their detection range. The chosen squares are passed
to `FarmTask` as `pull_squares`.

When she pulls, the farm records an anchor: the pull square of the nearest
target spawn if she can reach it, otherwise the square she stands on. After
the pull, `melee_kite` prefers steps toward the anchor. `low_health_retreat`
is unchanged except that it prefers the step toward the anchor when two
steps are otherwise equal.

### Wait, then approach only if alone

After a pull she holds at or near the anchor for up to `PULL_WAIT_SECONDS`
(8 s). She steps back toward the anchor if she is off it, and never takes a
step that brings her closer to the target.

- If the target comes within 2 tiles, she fights it normally.
- If it has not come when the wait ends, she approaches it only if it is
  still lone, measured at its current position.
- Otherwise she sets it aside for `PULL_SET_ASIDE_SECONDS` (120 s), using
  the farm's existing unreachable-target list, and picks another target.

The old 2.5 s fallback that walked toward the target no longer applies to a
pulled target.

### Analyzer ranking (already shipped)

Pull request https://github.com/atrinik/playtester/pull/77 ranks
Wizardry-to-15 spots by running the farm analyzer at the trained skill's
level with `--max-aggro-pack 1`, so the circuit only picks spots where a lone
pull is possible.

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
  pull-square clearance only; patrol avoidance still uses authored radii.
- No AoE behaviour yet. Firestorm already lifts the single-target Wizardry
  filter once she casts it, but pulls still require a lone target.
- No new set-aside mechanism. The farm's unreachable-target list hides a
  set-aside monster from target choice and threat counts alike, so if it
  later attacks her, only the existing unseen-contact retreat responds.
