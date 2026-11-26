# Atrinik playtester

Atrinik playtester is an autonomous, headless end-to-end player for Atrinik.
It connects through ordinary player protocols, observes live game state, and
runs navigation, quest, combat, equipment, economy, and survival policies
without privileged server access. The one exception is the owner-triggered
[operator assist](#operator-assist), which autoplay never uses.

The product goal is one reproducible unattended campaign from a new account
and character through a versioned full-game completion contract. The initial
golden path is tracked by [issue #1](https://github.com/atrinik/playtester/issues/1):
complete all 16 formal quests, reach level 115, defeat Zechna, verify every
outcome from authoritative game state, and emit a machine-readable result.

## Current capability

The migrated implementation provides pinned, encrypted Classic QUIC transport,
account and character setup, a live world model, content-derived navigation,
16 formal quest executors, adaptive farming through level 115, persistent
memory, and a loopback operator dashboard.

The current `autoplay` command completes the introductory flow and then levels;
the all-quest runner remains separate, and Zechna is not yet an automated
completion gate. The roadmap issues track the work needed to turn these pieces
into the single start-to-finish campaign described above.

Design notes: [COMBAT.md](COMBAT.md) describes how she fights, and the
proposal [docs/LEARNING_AND_FEEDBACK.md](docs/LEARNING_AND_FEEDBACK.md)
covers learning from doors, books and fights, and an owner-approved
playtester feedback queue.

## Install and acquire compatible inputs

Build and install the wheel from the repository root in any clean Python 3.11+
environment. The protocol wheel and native pathfinding archive are selected by
the compatibility lock; the project does not bundle or relicense either
dependency. Preflight and install the protocol wheel from that lock before
building the package:

```sh
python3 -m venv /path/to/playtester-environment
/path/to/playtester-environment/bin/python scripts/install_locked_protocol.py
/path/to/playtester-environment/bin/pip install .
```

The installer verifies the locked repository, release tag, exact commit, asset,
download URL, and SHA-256 digest before invoking pip. The
`atrinik-playtester preflight` command repeats that remote availability check,
while `doctor --preflight` reports the same check alongside local cache and
installed-input diagnostics. When `GITHUB_TOKEN` is set, these GitHub API
requests are authenticated (the token is sent only to `api.github.com`), which
avoids the anonymous rate limit that shared CI runners hit.

For a coordinated unreleased Classic protocol change, retain the immutable lock
and point only the development process at that checkout's generated bindings:

```sh
export ATRINIK_PLAYTESTER_PROTOCOL_SOURCE=/path/to/classic/protocol/generated/python
```

The source directory must contain `atrinik_protocol`. Playtester requires the
protocol revision 1085 and rejects an older installed wheel. The immutable
`v5.106.0` lock identifies the matching released wire-1085 wheel, so normal
installation and `doctor` require no source override. Do not use the override
for a released run; it validates the exact generated-source module origin
rather than relabeling an installed artifact.

Install the exact content compiler/catalog and Classic runtime selected by
[`dependencies.lock.json`](dependencies.lock.json). The default cache is
`$XDG_CACHE_HOME/atrinik/playtester` or `~/.cache/atrinik/playtester` and can be
overridden with `--cache` or `ATRINIK_PLAYTESTER_CACHE`.

```sh
/path/to/playtester-environment/bin/atrinik-playtester dependencies
/path/to/playtester-environment/bin/atrinik-playtester doctor
/path/to/playtester-environment/bin/atrinik-playtester preflight
```

`dependencies` downloads the content source and Classic-runtime archives, the
farming analyzer release asset, and the Classic server source archive over
HTTPS. It verifies their immutable SHA-256 coordinates, validates their bounded
layouts, and installs all four as one atomic cache generation. To seed a machine
from already downloaded files without network access:

```sh
atrinik-playtester dependencies \
  --source-archive /media/atrinik-content-1.14.0.tar.gz \
  --runtime-archive /media/atrinik-content-1.14.0-classic-runtime.tar.gz \
  --analyzer-archive /media/atrinik-tools-farming-analyzer-3.11.0.tar.gz \
  --server-archive /media/atrinik-classic-server-5.99.0.tar.gz \
  --offline
```

After one successful installation, `atrinik-playtester dependencies --offline`
verifies and reuses the cache. `doctor` performs a full tree-integrity check and
diagnoses missing, mismatched, or corrupt content, protocol, and pathfinding
inputs. Gameplay commands discover the verified content tooling and runtime
from that cache; no aggregate repository or ambient sibling checkout is needed.

## Build and test

Requirements are Python 3.11+, CMake 3.21+, Ninja, and a C17 compiler. The
native adapter uses the checksum-pinned Classic libatrinik pathfinding release.
Run `scripts/install_locked_protocol.py` with the Python environment that will
build the package before configuring CMake. Install the required `aioquic`
transport dependency in that environment for the live TLS regression tests.

```sh
cmake -S . -B build/playtester -G Ninja -DBUILD_TESTING=ON
cmake --build build/playtester --parallel
ctest --test-dir build/playtester --output-on-failure
```

The complete Python suite uses the same installed bundle. Build the package,
acquire it once, configure the verified paths through the package API, and run
the regression suite:

```sh
PYTHONPATH=build/playtester/python python3 -m atrinik_bot dependencies
PYTHONPATH=build/playtester/python python3 -m atrinik_bot doctor
PYTHONPATH=build/playtester/python python3 - <<'PY'
import unittest
from atrinik_bot.compatibility import (
    configure_cached_bundle,
    default_cache_root,
    load_lock,
)

configure_cached_bundle(default_cache_root(), load_lock())
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromName("atrinik_bot.test_bot")
)
raise SystemExit(not result.wasSuccessful())
PY
```

Run commands from the build directory so the assembled `atrinik_bot` package,
including its native extension, takes precedence over the source files at the
repository root.

## Run

Install the package or invoke the assembled package. Credentials and mutable
state belong outside the source tree and should be supplied through environment
variables or a private launcher:

```sh
ATRINIK_BOT_ACCOUNT=playtester-account \
ATRINIK_BOT_PASSWORD='replace-me' \
ATRINIK_BOT_CERTIFICATE_SHA256='replace-with-server-certificate-sha256' \
ATRINIK_BOT_CHARACTER=Playtester \
ATRINIK_BOT_RUNTIME_STATE=/private/state/playtester.sqlite3 \
atrinik-playtester autoplay
```

`--runtime-content` remains available as an explicit runtime override for
diagnostics, but ordinary runs use the locked cached runtime by default.

Use `atrinik-playtester --help` for transport, registration, character,
dashboard, navigation, quest, farming, banking, selling, and storage options.
The dashboard binds to loopback and rejects cross-origin control requests; use
an authenticated local tunnel instead of exposing it directly. Every response
carries a Content-Security-Policy (`default-src 'self'; script-src 'self';
object-src 'none'; frame-ancestors 'none'; base-uri 'none'`): the page loads
nothing from elsewhere, runs no inline script, and sets element styles
through CSSOM rather than inline `style` attributes, which the policy refuses.

The dashboard's Stop outlives a restart. It is stored in the runtime state
database for the configured `--character`, and the `autoplay` command then
starts idle (survival only) until Start autoplay clears it, so a service
manager that restarts the bot does not undo the owner's Stop. Other
dashboard tasks (quests, navigation, farming and so on) do not clear it, so
after one of them a restart still starts idle. Without `--character`
nothing is stored: the bot logs a warning once, and the dashboard's last
task says the Stop is not kept across a restart.

Classic 5.119.0 (https://github.com/atrinik/classic/pull/655) removed
starvation: food never drains and food 0 does nothing. The wire protocol did
not change, so `--food-obsolete` (`ATRINIK_BOT_FOOD_OBSOLETE`) tells her
which rules apply (`hunger.py`):

- `on`, the default, for the live server: every hunger behaviour is off.
  She makes no food or temple trips and no food restock, never eats, and
  rests and banks mana at food 0. The starving and idle-food alerts stay
  silent, and a low food stat neither makes a trip give way nor stops an
  operator assist. A food trip under way when food turns out obsolete
  ends. Carried food is junk, dropped between pulls on her farm map when
  that is open country or a dungeon (never a town, a `no_harm` or unique
  map, a map with a shop floor or mat, a savebed, merchant or priest, or
  her apartment), on a quiet square: no target and no hit for 10 seconds
  (`obsolete-food-drop`). Food never triggers a junk sale or holds back cure
  funding. A drop failed only while the same stack is still
  carried; it is then dropped again after 1, then 2 minutes, and after
  three drops kept, with `obsolete-food-kept` saying why. Fresh loot of a
  name already dropped starts afresh. Junk food is only ordinary server-typed
  food: quest food (any item a formal quest's objectives name), named food
  (a capital letter or a possessive, such as Gimli's lembas), food whose
  name has a reserve word (quest, key, talisman, artifact, unique,
  exceptional, ring, amulet, spellbook, formula, recipe, mana crystal),
  locked food, flesh and drink are kept. Unreadable quest data keeps all
  food (one warning).
- `off`: the hunger rules of an older server, unchanged.
- `auto`: `off` until her food has stayed unchanged for 30 minutes in which
  she moved and gained experience; the window starts again whenever food
  changes and, at food 0, whenever she loses health to something other than
  an attack (the old server's starvation). It never turns on while she is
  hungry with no food on her, and one fall of food keeps `off` for the run.
- An unknown `ATRINIK_BOT_FOOD_OBSOLETE` value is logged as an error and
  means `auto`.

`atrinik-playtester --version`, the startup log line, and the dashboard header
name the build: its release tag (with `+N` when the commit is N commits past
it) and short commit, linked to GitHub. CMake records them from Git on every
build; a tree without Git metadata reports an unknown build.

## Prometheus metrics

The dashboard serves `GET /metrics` in the Prometheus text format 0.0.4
(`text/plain; version=0.0.4; charset=utf-8`), so a scraper can record what the
bot does over hours for analysis. The endpoint has the same access rules as
`/api/state`: it listens only on loopback, rejects a non-loopback `Host`
header, and needs no other credential. It is read-only and holds no secrets.
It never includes the account, password, invite code or certificate. Scrape it
from the same machine or through the same authenticated tunnel as the
dashboard. Deploying Prometheus or Grafana (scrape configuration and
dashboards) is out of scope here.

Every series is named `atrinik_playtester_*` and carries `character` (in lower
case) and `server` labels. Counters are kept in memory and start again from zero when
the process restarts, which `rate()` and `increase()` handle. Every other
label comes from a bounded set: server-named monsters (at most 300), skills,
attacks, damage types, spells, potions, action kinds (at most 200) and task
types each have a fixed cap. Values past a cap are counted as `other`. Chat and
other player-typed lines are never parsed into labels; a PvP kill's victim is
the one character name that can appear, within the monster cap.

Gauges, read from live state on each scrape:

- `connected` (1 while playing), `hp`, `maxhp`, `sp`, `maxsp`, `food`, `level`, `experience`,
  `regen_hp`, `regen_sp`, `depletion_points`;
- `skill_level{skill}`, `skill_experience{skill}`;
- `money_carried_copper`, `money_banked_copper` (only once a balance is
  known);
- `farm_predicted_xp_per_hour{objective}`: the farming analyzer's estimate for
  the spot autoplay farms (its usable pick, not always the first-ranked);
- `task_info{task}` and `phase_info{phase}`: 1 for the running top-level task
  type and autoplay phase;
- `operator_assisted`: 1 once an [operator assist](#operator-assist) has
  given her coins, so progress from then on is not all earned in play;
  `operator_assist_copper` (copper given in total),
  `operator_assist_last_timestamp_seconds` (when the last run whose coin
  left the operator ended) and
  `operator_assist_running` (1 while one holds her still).

Counters:

| Metric | Labels | Counts |
| --- | --- | --- |
| `kills_total` | `monster` | "You killed …" lines |
| `experience_gained_total` | `skill` | experience from "You got N exp in skill …" |
| `damage_dealt_total`, `hits_dealt_total` | `attack` | "You hit … for N with …"; the server sends one line per attack type of a swing, so hits count damaging lines, not swings |
| `damage_taken_total`, `hits_taken_total` | `type` | "… hit you for N … damage", one damaging line per attack type |
| `spells_cast_total` | `spell` | spell casts ordered (including any the server refuses) |
| `mana_spent_total` | | each ordered cast's listed spell cost |
| `heals_total` | `source` | her own healing spells on herself (`spell`) and healing potions applied (`potion`) |
| `healed_hp_total` | | health restored by her healing spells on herself |
| `potions_used_total` | `potion` | potions applied |
| `deaths_total` | `cause` | deaths, by the post-mortem cause |
| `rests_total` | | rests started to regain spell points |
| `actions_total` | `kind` | every recorded action and decision, such as `retreat`, `melee-kite` or `farm-pack-split` |
| `purchases_total`, `sales_total` | | shop purchases and sales |
| `money_spent_copper_total` | `reason` | money paid in shops (`purchase`) |
| `money_gained_copper_total` | `source` | money received for sales (`sale`) |
| `item_moves_total` | `direction` | item moves ordered: `store`, `retrieve`, `loot` (corpse moves and `/take` commands), `pickup`, `drop` |
| `chat_lines_total` | `direction`, `channel` | player chat heard and answered |
| `connections_total`, `reconnects_total`, `disconnects_total` | | game server connections; a failed attempt also counts as a disconnect |
| `tasks_started_total` | `task` | top-level tasks started |
| `tasks_finished_total` | `task`, `status` | top-level tasks that completed, failed or were stopped from the dashboard (stopping counts as failed); a task replaced by another is not counted |
| `state_seconds_total` | `state` | seconds `fighting`, `resting`, `travelling`, `shopping` (shops, temple, NPC dialogue), `busy`, `idle` or `disconnected` (not playing) |
| `observations_total` | `source` | [observations](#observation-ledger) stored, by source kind; a repeat of a stored reading counts again |
| `facts_total` | `kind`, `status` | [facts](#facts) first stored, by kind (`tile-property`, `key-name`, ...) and status (`candidate`, `confirmed`) |
| `fight_positions_total` | `feature`, `outcome` | [fights recorded by terrain](#fights-by-terrain): `corridor`, `doorway`, `room`, `open` or `unknown`, and `won`, `retreated`, `recalled` or `died` |
| `observations_dropped_total` | `reason` | readings the ingestion filter refused: `chat`, `player-link`, `who-list`, `player-name`, `signed-book`, `unmatched-book`, `book-unverified`, `renamed-item`, `examine-interrupted`, `unauthored-item`, `examine-unverified`, `book-examine` |

The time in each state follows the latest recorded action that marks one. For
example, a target or a kite step means fighting, and a step or route decision
means travelling. Actions that mark none, such as applying an item, keep the
current state. A task that has not yet done anything that marks a state is
`busy`; with no task running, she is `idle`.

## Observation ledger

She keeps what she reads or is told in the `observations` table of the
runtime state database (`observations.py`, `memory.py`; the design is
[docs/LEARNING_AND_FEEDBACK.md](docs/LEARNING_AND_FEEDBACK.md) 4.2 and
4.3.1, phase F1). Recording changes nothing she does.

- **Only her own observations.** Player chat channels, lines carrying a
  player-name link, `/who` roster lines, books a player signed, unsigned
  books whose text matches no authored text, renamed items and any line
  naming another character she has seen (chat senders, `/who`, players
  in view) are never stored. Her own name becomes `{activator.name}`, as in
  authored text. Other players' names are held only in memory, to
  filter with, so after a restart only players seen since then are known.
- **Only exact matches with authored content.** Sign, door and
  magic-mouth text must equal the authored message of an object on or next
  to her square (`timing` when her own bump, apply or step in the previous
  1.5 seconds reached it, else `content-match`; a broadcast she did not
  cause, with another player in view, is not hers). A fixed server phrase
  ("You open the door with the key.", "You turn the lever.", "Nothing is
  written on it.") is kept only when an authored door, lever or sign of
  that name stands where her one bump or apply in the window reached.
  Books are kept when their text is authored, under the authored title,
  never the one received, and as the normalized authored form, never the
  received spacing. Examines of books are not kept (a player can name a
  book anything); an examine is kept only when the item's shown
  name is an authored name and every line is one of the forms the server's
  examine sends; one soulbound to someone is dropped. Dialogue pages are
  not kept (NPC scripts build them from player data; see the design's
  phase F13). An unattributed line is stored, as `system:<map>`, only when
  it is a fact line (the server's no-magic and no-harm refusals, such as
  "Something blocks your spellcasting."). The same text from the same
  source raises the row's `seen` count.
- **The authored-text index** is scanned from the locked content (maps,
  archetypes, artifacts, materials and the book message file, about 1.3
  seconds) when the `web` or `autoplay` command starts. Without it only
  fact lines are kept.
- **Bounded.** At most 50,000 rows a character, 4,000 characters of text a
  row; when full, the least recently seen rows that no later table refers to
  are evicted first. Writes wait at most 0.25 seconds for a locked database
  and otherwise drop the reading, never the game line.

The dashboard's read-only **Knowledge** tab lists her facts and recent
readings by kind and source (`GET
/api/knowledge?kind=SOURCE&limit=N&fact_kind=KIND&fact_limit=N`, newest
first, up to 1000 each, under the same loopback and `Host` rules as
`/api/state`).

### Facts

Fixed parsers (`facts.py`, the design's 4.3.2, phase F2) read facts out of
each stored observation into the `facts` table: key names a lock asks for,
key holders engraved on a door, lock colours, NPCs that mention a key,
passwords (an empty value when the text promises one and gives none),
directions, trap warnings, creature weaknesses and immunities, quest-gate
hints, quest-part places, doors that say they do not open, and lock texts
that name nothing. They are regular expressions over the stored text and
the authored names; no model, network or chat is involved, and nothing
but a kept observation is read. Each fact cites its observation
(`facts.observation_id`), which is then never evicted. Every parsed fact
is a `candidate`: a hint, never an unlock. One row per kind, subject and
value; a repeat refreshes it. At most 20,000 a character, the least
recently updated candidates evicted first.

**Tile facts.** When the server refuses her magic ("Something blocks your
spellcasting.", "Powerful countermagic cancels all spellcasting here!") in
answer to her own recent cast, the square of the cast (or the whole map)
is kept as a confirmed `tile-property: no_magic` fact, besides the one-hour
mark idle survival already sets; "Powerful countermagic cancels all
harmful magic here!" is kept as `no_harm` and changes nothing. A fact is
kept only when the refusal certainly belongs to the cast: no step was in
flight when she cast (sent after a map update last moved her), and for a
square she still stands where she cast,
for a map she is still on the cast's map. Anything else (a potion drunk
after an exit, a cast in the same tick as a step) is kept as nothing.
Tile facts lapse after 7 days unless a refusal refreshes them, and count
only under the content revision they were seen with (the locked
`atrinik/content` revision, in the fact's `inputs`).

A remembered fact never blocks anything by itself; idle survival's
one-hour mark still alone stops casts, the emergency recall included.
With `--persist-tile-facts` (or `ATRINIK_BOT_PERSIST_TILE_FACTS=1`), a
remembered no-magic square or map only reorders healing: a potion or
other non-magical heal comes before a spell, scroll, rod or wand there
(`tile-fact-no-magic`, once a square). With no other heal she casts
anyway, at any health: a refused cast costs nothing, and that cast tests
the fact. Off (the default), healing is unchanged and a
`tile-fact-no-magic-shadow` decision records once a square where she
would have put another heal first.

A fact is refreshed only by another no-magic refusal attributed to its
square or map; nothing refutes it, since a cast the server drops without
a word (her action timer, a device it refuses first) looks the same as
one that worked. So with the flag on, a stale fact can make her drink a
potion instead of casting on that square until it expires, at most 7 days
after the last refusal (or at once, when the content revision changes).

## Equipment

She works out what each piece of gear really adds when worn
(`equipment.py`; the design is
[docs/LEARNING_AND_FEEDBACK.md](docs/LEARNING_AND_FEEDBACK.md) 4.8, phases
F3 and F4). One scorer, `GearScorer`, decides what she wears (the farm's
equip loop), what she buys (shop upgrades) and which spare gear is surplus
to sell:

- **By style.** Her build picks the profile: caster while Wizardry trains,
  bow while she trains an archery skill, melee otherwise. Melee values
  damage per second (damage × `LEVEL_DAMAGE` of her weapon skill × the Str
  bonus, over the weapon's delay) and WC; a caster values Int, Pow, mana and
  regeneration by what they add at her stats (COMBAT.md, "Caster gear
  scoring") and melee damage at a quarter; an archer values a launcher's
  arrow WC and damage in full. All value AC, protections, hit points and
  Str, Dex and Con at the server's table values.
- **Speed.** A `(speed+N)` item and the armour speed cap count by how much
  faster or slower she really is, from the server's formula (`living.c`):
  0.85, plus the Dex bonus and N/30 per item, clamped to the lowest worn
  armour speed cap, plus (1 + N/3)/10 per item after the cap. +0.1 speed
  is worth +3 AC (owner decision, 2026-11-26), and speed is weighted up by
  half while she has no working recall (a change in that
  counts only once it has held for 2 minutes). Once she carries more than
  half her weight limit, an item's weight costs the speed it would take off
  her (encumbrance starts at 65%), but only where the decision changes
  what she carries: a purchase adds the stock's weight and a sale sheds a
  spare's; swapping between carried items costs nothing. An empty slot
  always takes armour that is a net gain apart from speed, whatever its
  speed cap. A shield gives nothing under a two-handed weapon or launcher
  (the server's two-handed flag), and a two-handed one is charged the worn
  shield it switches off.
- **Surplus** is spare gear that no style she can field would put on:
  her current build's, melee always, caster once she knows a spell, bow
  once an archery skill is above level 1.
- **Girdles and belts** are worn like any other slot (the belt slot).
- **Fallback.** Two items are compared on the effective scale only when
  both have examine data; otherwise both use the old name-based score
  (`gear_score` plus the archetype's base score).

- **The "+N" rules follow the Classic server.** "+N" adds N to each of AC,
  block and absorb the item has a base value for and, unless it is a
  launcher, to WC and damage likewise; a base of zero gets nothing, so a
  `silk cape +2` (capes have no AC) adds no AC. A launcher's "+N" goes to
  each arrow it fires, and its protections do not count. A light adds
  nothing. Protections, attacks and a weapon's damage scale with condition.
- **Examining carried gear.** On a quiet farm tick (no threat in view, the
  recall-wand rule) she examines each identified gear item she carries
  once a session, what she wears first: one at a time, at least 2 seconds
  apart, waiting up to 5 seconds for each. Recall-wand and mana-crystal
  examines wait for its answer and it for theirs, so normally only one
  examine is in flight. The walk out of a farm, waiting for her wands'
  charges, abandons a waiting gear examine instead; the gear examiner then
  pauses for 7 seconds, so its late answer, read by nothing, comes before
  her next gear examine is asked. The observation ledger may then pair that late
  answer with the wand's examine; the row still carries the item's name
  from its own line, and the wand's answer may go unrecorded. Examine shows the base stats
  without the "+N"; until an item is examined its archetype's base stats
  are used.
- **Shadow logging.** At most every 30 seconds she compares, per gear
  family, what the old name score and `GearScorer` would put on. Each
  change in where they disagree is a `gear-shadow` decision with both
  items and both scores.

The dashboard's read-only **Equipment** tab lists each gear family she
carries: what she wears and the best candidate, with both scores, the
effective stats, per-stat differences and notes such as "+2 on a cape adds
no AC", and the recent shadow log.

## Combat builds per spot

She fields one of four builds on her farm spot (`builds.py`; COMBAT.md,
"Combat builds per spot"): melee, AoE caster, single-target caster or bow,
each with its requirements, a FarmTask routine, a gear style and an
encounter style. The operator picks one for the farmed spot or for every
spot on the Farming tab (kept in her character memory); on auto she fields
the safest build she can by the analyzer's encounter rating of each
fieldable style, ranked by XP. A build she cannot field falls back with a
"Build gap" alert, and a "get this build" errand learns the spell it needs
from a spell seller (first Firestorm from Dunowen) once she can pay it
beyond her cure reserve; until then the dashboard shows what she needs. A
build change makes one apartment trip to swap gear sets, and the Farming
tab suggests the best owned set for the spot's build.

## Inventory and home storage

She carries only her active set (`storage.py`, `FarmCircuitTask`): worn
gear and her active build's best gear, heals, recall (one recall wand),
identify devices, the mana crystal, quest items, keys, money and other
consumables. The rest goes to her apartment chest, and some is sold:

- **Sets per build.** `GearScorer` ranks everything she owns once per build
  (melee, caster, bow): the best item per slot (two rings) is that build's
  set. The other builds' sets and other spare gear go home. A storage trip
  also takes back out her active build's best stored items that beat what
  she wears by the equip loop's margin.
- **The index.** The chest's contents are not in the dashboard state and
  are sent only while she has it open, so her character memory keeps
  what it holds (`GameState.apartment_storage`): each item she deposits or
  retrieves, replaced by the real contents whenever she opens the chest.
  Each stored gear row names the sets it belongs to (`melee:weapon`,
  `caster:ring`), for a later build change.
- **Trips.** A storage trip leaves at once for a valuable, else once 8
  items wait or she carries 65% of her weight limit (where encumbrance
  slows her), at most once per 30 minutes. An item the chest refuses
  for god-given gear ("You can't store god-given items ...") is not
  offered again; a full chest or any other refusal only backs off. What
  she deposited is not taken back out that session until why it went
  home changes (identified under a new name, or a level requirement she
  now meets). God-given refusals include "First remove all god-given
  items from this container!". A junk sale waits for 8 items, a cure shortfall or 85% of her
  limit. The dashboard, CLI and mana-crystal chest visits update the
  index too, without set labels (the circuit's next visit adds them).
- **Containers.** The key ring stays. A bag of holding is known by its "of
  ... holding" title or its examine ("It decreases the weight of items
  inside by N%"); bags rank by the server's multiplier (minor 0.85 down to
  weightless 0.30). She carries her best bag only while what she carries
  would weigh more than half her limit without it (it goes home again
  below 40%), and puts gems and the ammunition her build does not shoot
  in it, never coins, heals, unknown items or a valuable-looking gem
  (magical, or quality 90 or more), which stays where storage and cure
  funding see it. The server's rules (`types/container.c`,
  `types/player.c`): applying an unready container readies it, applying
  a readied closed one opens it, and applying the open one closes and
  unreadies it; only a readied container takes a move into it, and a
  readied or open one takes every pickup. So she readies the bag on a
  quiet farm tick, fills it, then opens and closes it to unready it. The
  unready step runs on every tick, so a fight or a trip that starts while
  it is readied unreadies it before any loot is picked up; a bag seen open
  is closed with one apply. Each apply waits up to 3 seconds for the
  server's flag change. If the bag will not unready after a few applies,
  she gives it up and the dashboard warns `bag-readied` ("Bag left
  readied: pickups go into it"). The bag is fetched from home once she
  needs it (above 50%), before the 65% storage trigger, since the bag is
  what keeps her below it. Other containers go home; a duplicate plain
  container (same name, empty weight) is sold.
- **Sold.** The worst duplicates (gear the equip loop would never wear
  that is no build's best), cursed or damned potions, spare recall wands
  known to be empty, identified books and worthless-plus items. Never a
  ring, an amulet, anything unidentified, or the best item of any build.
- **Recall wands.** She keeps one: one not known empty, else the most
  charges, else the highest level. Only that wand is recharged, once it
  is empty, with a scroll of recharge (recall shopping buys one); none is
  read or bought while it works. A spare that may still
  hold charges goes home.
- **Identify-all.** Unknown gear, carried or stored, sends her to the
  cheapest smith she can reach once her wallet pays him plus a 1,000
  copper reserve. The fee is flat, so she goes for three or more unknown
  pieces, any unknown ring, amulet or wand, within an hour of any rise of
  at least one gold coin in her carried wallet (loot, a sale, a
  withdrawal, or the owner dropping gold, which she picks up like loot:
  they cannot be told apart), or once
  the first unknown piece has waited 6 hours. The smiths: Kulgar (world_8_79, 1,450), John Markor (world_5_58,
  2,200) or Conbran Strongfist (world_0_69, 3,200); a smith she cannot
  reach is passed over for 6 hours. Stored unknown gear is taken out of
  the chest first, and the next storage trip puts it back; if that fetch
  fails, she identifies what she carries and the fetch waits. Unknown gear
  stays carried while identify-all is affordable, and goes home while it
  is not. She never wears an unidentified item. While she cannot pay, the
  dashboard shows `identify-unfunded` ("Identify needs N copper ...; drop
  gold to help"): the owner may drop gold at her feet (operator assist).
  While depleted for the temple the note is not shown: the cure comes
  first, and only the cure's batch rule buys an identify-all.

## Fights by terrain

She records where she stood in each fight (`terrain.py`, `memory.py`; the
design is [docs/LEARNING_AND_FEEDBACK.md](docs/LEARNING_AND_FEEDBACK.md)
4.7, phase F10). Recording changes nothing she does.

- **A fight** starts at the first combat line of hers in a while (a hit or
  miss on her, a block, dodge or parry, her own hit or kill) and ends 10
  seconds after the last one, when she leaves the map (crossing a tile
  seam to the next map keeps it), or 2 seconds after her death (hits until
  then still count, as in the failure review); a fight longer than 15
  minutes is cut there. It ends `died` on her death, `recalled` after her
  emergency recall, `won` when she killed something, and `retreated`
  otherwise. Damage over time from her own poisoning or disease ("poison
  hit you for ...", or a symptom under its disease's name, "flu hit you
  for ...", from the authored diseases in the locked content) names no
  attacker and starts no fight.
- **The square** is read when the fight starts, and again where she stands
  when a second attacker hits her within 8 seconds. Its `reach` is how
  many of her 8 neighbours a walking monster could stand on, from the
  authored walkability she plans with (a door counts; the server lets a
  monster hit a diagonal neighbour past a wall corner; off the map counts
  only toward a tiled neighbour). Its feature is
  `corridor` (reach 2, opposite each other), `doorway` (a door on or next
  to her, or a one-wide gap into a room), `open` (reach 7 or 8 and at least
  20 standable squares within 2), `room` (anything else), or `unknown` off
  the world graph.
- **Counts only.** Peak attackers (the most distinct attackers within 8
  seconds, counted exactly as the failure review counts packs), ranged
  monsters in view and how many a wall or door covers (on the cells an
  aimed bullet crosses, as `bullet_ray_clear` tests them), creatures and players in view, kills, damage taken and
  seconds. No attacker's or player's name is stored.
- **Bounded.** The recorder reads only new server lines each engine step
  and looks at the creatures in view only during a fight; a fight keeps at
  most 2,000 hits. A row waits at most 0.25 seconds for a locked database
  and is otherwise dropped; at most 20,000 rows a character, oldest
  evicted first. An error in the recorder forgets the fight and never
  reaches the game loop; it is logged once, then counted every 10 minutes
  until a step works again.

The read-only **Terrain** panel on the dashboard's Farming tab lists her
recent fights and the rates learned per feature, peak attackers and
ranged monsters in view: fights, damage a second and deaths, each day's
fights weighted with a 7-day half-life (`GET /api/terrain?limit=N`, up to
200, under the same loopback and `Host` rules as `/api/state`).

## Feedback drafts

She collects what confuses her as deduplicated drafts in a local queue
(`feedback.py`, `feedback_producers.py`; the design is
[docs/LEARNING_AND_FEEDBACK.md](docs/LEARNING_AND_FEEDBACK.md) 4.10, phases
F5 and F6). Nothing leaves the machine until the owner approves an item with
the CLI on CT130: the game process and the queue's code open no socket and
never see the GitHub credential. Only the separate filer (`feedback file`,
its own systemd unit) files approved items, as issues signed by a small
GitHub App; see "Approval and filing" below.

- **Producers.** "+N with no effect": an examined item of hers whose name
  shows "+N" on a type and base that gain nothing from it (a `silk cape +2`),
  one aggregate draft for `atrinik/classic` listing the archetype families
  she met. Book experience at low Int: an authored book she read at Int 10
  or lower, where the server's `book_exp_mod` is negative, one draft for
  `atrinik/classic`. Typos (a small list of common slips such as "a the",
  "the the" and "recieve") in authored door, gate, lever, sign and
  magic-mouth text, one draft a source for `atrinik/content`. They read
  only her stored observations, her own stats and skills and her own gear,
  never chat; the typo producer also reads the stored lock and sign texts
  once a login.
- **Deduplication.** Each draft has a fingerprint, the sha256 of its kind,
  repository, subject and claim, shown as `pt-` and 12 hex digits. The same
  fingerprint never makes a second draft, across restarts too; a new
  occurrence raises its count, and a draft's body is rendered again from its
  evidence each time; its day count is the number of distinct dates. At
  most 500 drafts a character: a new draft evicts the one with the fewest
  occurrences, then the least recently seen. Drafts carry dates, never times
  of day, and name a map only by its authored path, never a player's own
  map.
- **Privacy.** A draft holds only authored text and her own state. Game
  text appears only in a fenced code block longer than any backtick run in
  it, with HTML comment markers and invisible characters removed, so a
  `@name`, `#123`, link or copied fingerprint line in it does nothing;
  names are inline code. An excerpt line naming another character she has
  seen (in view, in `/who`, or any chat sender her memory keeps) is
  dropped. A draft whose title or body would contain her account,
  character or the server's address, the password, the invite code, her
  party, the operator character or another character's name is not stored
  at all. Every reader checks stored items again against the names known
  now: one that names a private term is withheld, shown with no text, and
  stays withheld; it can only be rejected.

The dashboard's read-only **Feedback** tab lists the items, filtered by
kind and repository, with the exact body that would be filed (the Markdown
copy), the issue once filed, the filer's error and an item's possible
duplicates (`GET /api/feedback?kind=KIND&repo=OWNER/NAME&status=STATUS&limit=N`).
It has no write route: approving, rejecting and editing are CLI-only (owner
decision, 2026-11-19). Items filed without their labels or type, waiting on
a duplicate review, or failed raise dashboard alerts. On CT130:

```sh
# With the service's ATRINIK_BOT_ACCOUNT, ATRINIK_BOT_CHARACTER and
# ATRINIK_BOT_RUNTIME_STATE, and its --host and --port:
atrinik-playtester --host HOST --port 1728 feedback list
atrinik-playtester --host HOST --port 1728 feedback show pt-4f1c2a9b0d3e
```

`--host`, `--port`, `--account` and `--character` select her rows as the
bot stores them; `list` takes `--kind`, `--repo` and `--status`, and
`list --json` and `show --json` print JSON. The command opens the database
read-only, whether or not the bot is running, and needs no network and no
game inputs. It knows fewer names than the running bot (none it saw in
play), so it shows an item's text only once the running bot has screened
it since it last changed; otherwise it prints "(not yet screened by the
running bot)" and the metadata. Run it as the bot's service user: SQLite
may create the database's `-wal` and `-shm` files even for a read-only
reader when the bot is stopped, and files created by another user can then
stop the bot from opening its database.

### Approval and filing

The owner decides on CT130, as the bot's service user (with its
environment, so the CLI knows the account, character and the other private
terms; for example `sudo -u playtester` and the release's full path, as for
operator assist below):

```sh
atrinik-playtester --host HOST --port 1728 feedback approve 12
atrinik-playtester --host HOST --port 1728 feedback reject 13
atrinik-playtester --host HOST --port 1728 feedback reject 14 --duplicate https://github.com/atrinik/content/issues/9
atrinik-playtester --host HOST --port 1728 feedback approve 14 --file-anyway
atrinik-playtester --host HOST --port 1728 feedback edit 12 --title "..." --labels bug,maps --type Bug
atrinik-playtester --host HOST --port 1728 feedback edit 12 --body-file body.md
atrinik-playtester --host HOST --port 1728 feedback show 12 --markdown
```

- `approve` takes only an item that is not withheld and that the running
  bot has screened with her full private terms since it last changed (the
  bot screens approved and edited items every minute); it prints the
  Markdown copy. `--file-anyway` files an item the filer held in
  `needs-review` despite its possible duplicates. Approving a `failed` item
  is the retry.
- `reject` ends an item; `--duplicate URL` marks it a duplicate of an
  existing issue. A withheld item can only be rejected.
- `edit` changes the title, body (`-` reads standard input), labels, type
  (`default` chooses by kind), repository or target (`new-issue`, or an
  issue URL in the item's repository to post it as a comment). The body
  keeps its fingerprint footer, is screened before it is stored, and the
  item becomes a draft again, to be screened by the bot and approved again.

The filer, `atrinik-playtester feedback file`, files approved items one at
a time: it refuses an item that is withheld or that the running bot has not
screened since its approval (checked again when it claims the item, so one
withheld or edited meanwhile is never filed), screens it again itself, checks GitHub for an
issue with the same fingerprint or the same map in its title (holding a
match in `needs-review`), creates the issue with the item's labels, the
`playtester` label and the type (`Bug`, `Feature` or `Task`), reads it
back, and marks it `filed`, or `filed-unlabelled` when GitHub dropped a
label or the type. A `comment:` target is posted as a comment instead. It
files at most 5 items an hour and 20 a day, 60 s apart, honours GitHub's
`Retry-After` and secondary limits with a back-off from 10 minutes, and
stops while GitHub's remaining core quota is under 50. It never follows a
redirect, and an item an earlier run already filed goes to `needs-review`.
The commands run only as the user that owns her memory database. Every request is a row in her memory's
`feedback_filings` audit table. Without the App key (or with `--dry-run`)
it sends nothing, leaves the items approved and prints their Markdown
copies.

The key is read only by the filer, from its systemd credential
(`$CREDENTIALS_DIRECTORY/feedback-app.pem`) or `--app-key-file`, which
must be readable by nobody else. The filer signs a short-lived RS256 JWT
with it (the `cryptography` package, already required by `aioquic`), mints
an installation token for the one repository it files to with
`issues: write`, revokes it when done, and never prints, logs or stores
the key, its path, the JWT or the token. One-time setup before live filing:

1. Create a GitHub App owned by the `atrinik` organization with repository
   permissions Issues: read and write and Metadata: read-only, nothing
   else, no webhook; install it on `atrinik/content`, `atrinik/classic` and
   `atrinik/playtester` only. Note its App ID and generate a private key.
2. On CT130, place the key as `/etc/atrinik-playtester/feedback-app.pem`,
   `root:root`, mode `0400`, so that only systemd can read it and hand it
   to the filer's unit.
3. Create the `playtester` label in each of the three repositories (the
   filer never creates labels; without it items are filed
   `filed-unlabelled`).
4. Add the filer's units next to the bot's service, with the bot's user
   and runtime state. Give the filer its own environment file with only
   what it needs: `ATRINIK_BOT_ACCOUNT` and `ATRINIK_BOT_CHARACTER` (they
   select her rows), `ATRINIK_BOT_RUNTIME_STATE`, and for its own screen
   `ATRINIK_BOT_PARTY`, `ATRINIK_BOT_OPERATOR_CHARACTER` and
   `ATRINIK_BOT_CERTIFICATE_SHA256`. It does not need the game password or
   invite code: it never logs in, and the running bot's screen with every
   private term, which the filer requires after each approval, covers them.

   ```ini
   # atrinik-playtester-feedback-file.service
   [Service]
   Type=oneshot
   # The bot's service user, and the bot service's own environment file
   # (account, character, runtime state and the other private terms).
   User=playtester
   EnvironmentFile=/etc/atrinik-playtester/feedback-filer.env
   Environment=ATRINIK_PLAYTESTER_FEEDBACK_APP_ID=APP_ID
   LoadCredential=feedback-app.pem:/etc/atrinik-playtester/feedback-app.pem
   ExecStart=/opt/atrinik-playtester/current/venv/bin/atrinik-playtester --host HOST --port 1728 feedback file
   NoNewPrivileges=yes
   LimitCORE=0
   PrivateTmp=yes
   ProtectHome=yes
   ProtectSystem=strict
   # The directory of ATRINIK_BOT_RUNTIME_STATE (her memory and its lock).
   ReadWritePaths=STATE_DIRECTORY

   # atrinik-playtester-feedback-file.timer
   [Timer]
   OnBootSec=10min
   OnUnitActiveSec=15min
   [Install]
   WantedBy=timers.target
   ```

   The game's own unit must not have the `LoadCredential=` line or the key
   path in its environment.

## Operator assist

The owner can have an operator character hand the played character gold.
This is the one, narrow exception to playing only through ordinary player
capabilities (owner decision, 2026-11-16, when Sera was 1,827 copper short
of the temple's remove-depletion fee). Nothing automatic ever starts it:
autoplay, tasks, the client and the task engine hold no reference to it.

It is disabled until `ATRINIK_BOT_OPERATOR_CHARACTER` (or
`--operator-character`) names another character on the same account. Grant
that character exactly the two commands it needs, from a game master's
session:

```text
/cmd_permission <name> add tp
/cmd_permission <name> add create
```

Do not give `[OP]` or `*` to any character this bot logs into: those grant
every command, including `/console`, a server Python console.

When enabled, the bot creates an owner-only key at start-up, if none exists:
`operator-assist.key` (mode 0600, owned by the service user) next to its
runtime state (`ATRINIK_BOT_RUNTIME_STATE`), else in
`/var/lib/atrinik-playtester`, or at `ATRINIK_BOT_OPERATOR_KEY_FILE`.
`POST /api/operator-assist` requires that key in the
`X-Playtester-Operator-Key` header (compared in constant time) and a
`Content-Type: application/json` body; reaching the dashboard is not enough.
Requests without the right key are refused with 403, write no audit record,
and are logged at most once a minute. As defence in depth, the lab's
reverse proxy should deny `/api/operator-assist` outright.

With the bot running (`web` or `autoplay`), the owner stops autoplay from
the dashboard and runs, on the same machine, as the bot's service user so
the command can read the key (for example `sudo -u playtester`):

```sh
sudo -u playtester /opt/atrinik-playtester/current/venv/bin/atrinik-playtester \
  operator-assist --give-gold 1 --to Sera --web-port 8765 --then-autoplay
```

The full path is the release installed by the home-lab role. That role puts
no `atrinik-playtester` on sudo's secure PATH.

Give `--key-file` when the key is elsewhere. The command posts to the
running bot's loopback dashboard, prints the audit record, and exits 0 only
when the coin reached her. The running bot then:

1. refuses unless the feature is enabled, `--to` names the character it
   plays, the amount is 1 gold coin (the cap), the last run that reached
   `/create` was more than 24 hours ago, and she is idle at a safe square:
   logged in, no task or idle food trip, no target or combat, nothing hit her
   in the last 10 seconds, health at least 90% and food at least 300 (not
   checked where food is obsolete, see Run); and
   she can carry the coin (the server refuses a pickup unless what she
   carries plus the coin stays under her weight limit, which depletion
   lowers with Str: on 2026-11-18 Sera carried 40.6 kg against 25 kg, so
   shed weight first);
2. holds her still (`TaskEngine.operator_hold`: no task, idle survival or
   inventory upkeep acts; dashboard tasks other than Stop and manual
   actions are refused) and cancels her queued movement;
3. logs the operator character in on a second connection. The Classic server
   lets one account play several characters at once; it only replaces a
   second session of the same character (`player_login`). She stays logged
   in throughout. If the owner is playing the operator character at that
   moment, that session is disconnected, and after the run the operator
   character stays saved on her square;
4. as the operator, sends `/tp Sera` (onto her exact square), waits until
   the operator is on her square and her sub-layer (a drop lands on the
   dropper's sub-layer, and `/tp` does not set it), then sends
   `/create goldcoin` (one gold coin, 10,000 copper, into the operator's
   inventory), drops exactly that coin and logs out;
5. has her pick the coin up from her square, then releases the hold. The
   ground window sends no item types, so the coin underfoot is the item
   named `gold coin`.

Each step has a timeout, and before every order and on every poll it checks
that holding her is still safe (no target or combat, no hit on her, health
at least 90%), because the hold also suspends her idle healing. Any failure
aborts the run: the operator is logged out, the hold is released, and the
record says which step failed and where the coin is (`none`,
`operator-inventory`, `ground` on her recorded square and sub-layer,
`character`, or `unknown`). After an abort past `/create` it looks once
more, a second later, at the operator's inventory and her square; a coin
neither accounts for, such as one whose `/create` went unanswered, is
`unknown`. If her own connection drops meanwhile, her normal reconnect
restores her and the pickup waits for her. A run that failed before
`/create`, or whose `/create` the server refused (no permission, or
`'/create' is not a valid command.`), does not count toward the daily
limit; a run whose counted record cannot be saved never sends `/create`.

Every run and refusal while enabled is an audit record in her memory
database (`memory.sqlite3`, kind `operator_assist`) and in the decision log
(`operator-assist`). The list keeps 200 records, trimming only refusals and
runs that never reached `/create`, so the daily limit and the assisted mark
can never be pushed out. Gold counts as given once the coin left the
operator (it reached her square or her pack) or may have (`unknown`). From then on, the dashboard shows
an `Operator-assisted since …` header notice and the coins given next to her
wealth, and the metrics above mark her as assisted: progress after it is not
counted as earned.

## Farming analyzer

Autoplay and the dashboard rank farm spots with the locked `atrinik/tools`
farming analyzer instead of hard-coded level bands. The dependency installer
caches its release asset and the Classic server source whose experience tables
it reads. During `web` and `autoplay`, a background task checks the character
every 30 seconds. When the killing weapon school's level, the player doll's DAM,
WC, weapon speed, movement speed, max HP, AC or protections change materially, or the last
ranking is six hours old, it starts one `nice` analyzer process with
`--ranking all --json` (about 10 to 30 seconds) while the bot keeps playing.
Runs are at least ten minutes apart, failures back off for fifteen minutes, and
a failed run keeps the previous ranking. Only melee weapon schools are
simulated so far. She fights one target at a time and casts no area spell,
so the profile passes `--max-aggro-pack 2` (1 while Wizardry trains): the
analyzer drops a map whose likely simultaneous aggro is larger.

Since analyzer v3.11.0 the bot also runs the analyzer's encounter model, to
warn only (owner decision, 2026-11-25; COMBAT.md, "Encounter warnings").
The ranking run above is the one she chooses by and passes no
`--combat-style`: with a style the analyzer lowers `spot_safety_score`,
`safety_score`, `score` and the views' order to its encounter safety. Once
a ranking is in, a separate encounter run passes her combat style
(`--combat-style melee` with a weapon, `ranged-kite` while she trains
Wizardry with spells only, which she casts from range once the spell fits
beyond her healing reserve; the fielded build's style when she fields
another, see "Combat builds per spot"),
her measured healing (`--healing-per-second`, below) and her displayed
speed (`--character-speed`), and its encounter fields (`encounter_safety`,
`expected_pack`, `worst_pack`, `encounter_reasons` and the rest) are copied
onto the ranked spots with the same path and map paths. Her ranking never
waits for it, and a ranking that falls due stops it first. It does not run until one heal has landed: with no healing
every pull would rate as unhealed. A new ranking, another style or a 15%
change in her healing re-runs only the encounter run; a failed one keeps the
ranking, shows on the Farming tab and is retried after 15 minutes. Each run
takes about 15 to 30 seconds. Her healing is the median of her landed
heal-spell casts over a weapon swing plus a one-second cast (an upper bound:
casts also wait out other actions), within what her SP regeneration pays at
her cheapest heal spell's cost.

The farm circuit records a `farm-spot-encounter` decision with the style,
encounter safety, expected and bad pull's packs and reasons whenever her
spot's rating changes, the spot she starts on included, and the Farming tab
shows the same. When her spot's encounter safety is under 0.8 or its bad
pull over 6, a `farm-spot-encounter-warning` decision names it and its first
reasons, and the dashboard raises a `farm-encounter` warning alert while
autoplay farms that circuit and the spot still warns.

The analyzer scores a spot's pack from its own maps alone, so the bot folds
tiled neighbours in itself (`seam_ranking`, COMBAT.md "Safer
spot choice"): from its world graph, a neighbour map's aggressive spawns
within their detection radius, and its casters within 10 tiles, of where
she pulls raise the spot's pack, as do the circuit's own maps' spawns
across their shared seams; it flags `seam caster×N` and lowers the spot's
own safety, and a spot whose pack then exceeds the cap is left out. The
Farming tab, the farm picker and manual farm tasks use that same ranking:
a spot seam exposure leaves out is not offered in the picker (an explicit
`farm_circuit` task with its own legs still works), and the Farming tab
marks a risky spot and its seam exposure under its approach.

A spot is only as safe as the way there. Once her savebed is known and exists
in the locked content, each run passes it as `--approach-origin MAP
--approach-origins-only`, and a savebed move triggers a new run. The analyzer
then charges the most dangerous pack crossed between the savebed and the spot
against her live maximum HP (`--max-health`), and reports the spot's
`safety_score` as the lower of the spot's own safety and that approach safety.
Otherwise the analyzer's default origins apply. A spot the analyzer finds no
path to is capped at an approach safety of 0.5
(`--unreachable-approach-safety`), because its walk does not model ships,
scripts or keys. Autoplay farms spots with a known approach safety of 100%
and a spot safety of 100% (seam exposure included) before any spot whose
approach is unknown or either is expected to cost more than her whole
health bar, keeping the analyzer's order within each objective's ranking. A risky
approach is taken only when nothing else is usable, and then the safest one
first (unreachable counts as 0.5). An apartment savebed is judged from its
recorded return, since the apartment is left only through a script the
analyzer does not follow, and an origin the analyzer rejects is retried once
from the default origins and not passed again. Raw rankings list every
circuit permutation, so each run keeps the top 40 per objective with at most
three circuits of one area, except the safety view, which keeps 250: its
score is capped at 1 for every spot whose danger stays under her health, so
its first 40 can all be permutations of one high-XP area (see COMBAT.md, the
cure-funding farm). Among safe spots within 80% of the first-ranked one's
rate, the nearest from where she stands (fewest map transitions) goes first;
deep in a dungeon, far from both her savebed and the spot, she walks home
first and chooses again (COMBAT.md, the Old Outpost stall). The Farming
tab shows each spot's approach: its safety, worst map and danger, or that it
is unknown, and marks the spot autoplay farms with its rank.

The dashboard's **Farming** tab shows the run status, the simulated profile and
the top spots for the preferred objective: XP, money or loot per hour,
balanced, or safety. The choice is saved per character and needs no new run,
because one run ranks every objective. The rates are model estimates for
comparing spots, not live measurements.

Autoplay farms the preferred objective's #1 usable spot. The exception is
depletion: while the live depletion force is present and her depletion count
is at the temple-service threshold (3), it farms the **safety** ranking
instead, until the depletion is removed. If no safety spot is usable, it keeps
the preferred objective's spot, because the circuit removes depletion before
any farming whenever an item or the temple fee allows, and farming is what pays
that fee. The Farming tab then shows the ranking she actually farms, and notes
the depletion override; the objective toggle keeps showing your choice.

There are no level bands or curated spots: the analyzer result's map paths become the
circuit's legs (a multi-map circuit rotates through every map), and its listed
monsters become the targets. Spots whose residents include aggressive spawns
get cautious recovery margins (flee at 80% HP, heal at 95%) and isolated pulls
from squares outside other residents' detection radii; other spots use 75%
and 90%. Until the first ranking exists, autoplay holds position safely.
The Tasks tab's **Farm** picker lists the same ranked spots.

A death or emergency recall at the spot does not make autoplay give it up.
Instead it reviews the last minute of combat messages and decisions to find
out why, and adapts:

- **pack**: two or more monsters were hitting at once;
- **resources**: spell points ran short of the next heal;
- **damage-type:TYPE**: one non-physical damage type dealt most of the damage;
- **out-damaged**: a single attacker simply hit harder than she could recover;
- **route**: the failure happened on the way there, not at the spot.

Each failure raises the spot's adaptation level (up to 4). Each level raises the
flee threshold by 3% and the heal threshold by 2%, capped at 90% and 98%. The
lessons are persisted per character, and the dashboard's Farming tab shows
them, together with improvement goals aggregated across spots (area spells,
protection types, offence, healing). Only when the spot keeps failing at the
maximum level (three failures there within an hour) does autoplay pause it for an hour
and farm the next-ranked spot meanwhile; the pause covers every map of the
spot's area when that area is small (the Burnt Forest's 25 maps), and kit
hazards count failures across the whole area. A death or recall just
across the spot's seam counts for the spot, and a spot whose every
resident kind is out of her league is left for an hour (COMBAT.md,
"Learning from deaths").

A spot is still quarantined for six hours, and autoplay moves to the
next-ranked spot, after:

- ten minutes there without any character XP;
- a route there that the world graph cannot plan.

Explicitly configured circuits keep the old behaviour: a death or emergency
recall there quarantines the spot.

Before farming a circuit, autoplay moves the character's savebed (the "bed to
reality" a death respawns at) to the usable bed with the shortest walk to the
circuit's maps, when that saves at least 20 tiles over the current one. Inn
and tavern beds work for anyone; apartment and house beds only in the starter
apartment. The walk is estimated along the world graph's route from the bed,
and the search spreads its routes over ticks so it never stalls combat. A bed
that cannot be reached or applied is skipped for the next-nearest one. The
dashboard's Farming tab shows where she respawns.

When the analyzer gets a spot wrong, the fix belongs in the analyzer.

## Route planning

Navigation plans over a world graph compiled from the locked content: tile
seams, exits, apartments, locked doors and inventory checkers. Checkers
follow the server: one requires or forbids a matching object, matched by its
`slaying` or its name, and some use the object up as she passes. The keys she
carries are read from her inventory and from containers her client currently
lists: a key in a closed key ring counts once she opens it. Keys that no
map places, such as quest rewards and NPC gifts, are recognised from their
key archetypes and artifacts. A generic key name shared by several locks,
such as "strange key", opens none of them in planning. Every plan, savebed choice and map-level route uses
this one unlock state, so a keyed shortcut such as Morg'eean's ship counts
only while she carries its key. The Tasks tab's **Unlocks** section shows the
keys she carries, where, how many gated tiles each opens, and any ambiguous
key names. A map where
she recently died on the way somewhere is avoided for a while when another
route exists.

The owner can block a map, an exit or a tile seam from the Tasks tab's
**Owner blocks** section, for 1 hour to 7 days; the list shows each block's
expiry and removes it early. A block is a wall for transit on every trip; a
blocked map stays usable as a destination. Blocks are stored per character
in the runtime state database (`route_blocks`) and are written only through
`POST /api/route-blocks`: loopback only, a loopback `Host`, no foreign
`Origin`, and `application/json` only, which an HTML form cannot send
cross-site. Only an essential trip (food while she is hungry or starving,
never where food is obsolete, a
cure, the depletion service, a savebed move, a trip that funds one of them,
and the recovery phase's walk home) crosses a block, and
only when every route does; a block never sends a trip through a hazard
the planner otherwise avoids. It waits at a safe place (her savebed, or a
map without hostile spawns, with no hostile in view; on a map
with hostile spawns she walks to her savebed first) for full HP and SP, or
HP and SP that have stopped rising for a minute, and a ready recall when she
owns one she could ready, then holds fire until the block is behind her.
That wait has no limit and raises a dashboard alert after 10 minutes, and
a non-food trip gives way to a food trip once she is hungry. A food trip,
hungry or starving, waits only for a ready recall, since waiting to
regenerate burns food. A working recall is never a blocker (owner
decision, 2026-11-22): with only empty or unusable recall sources she
does not wait for one, and a food trip then crosses with hold-fire alone.
Each crossing is recorded (`essential-route-override`, plus
`essential-care-food` or `essential-care-starving` for a food trip, which is
also alerted) and listed under **Care crossings**.

On every trip she records what the way cost her, in the runtime state
database (`route_events`, `route_traversals` and `route_requirements`, per
character): deaths, emergency recalls and disengages, and the damage she
took on each map she crossed, each against the map and, when it happened
within 3 tiles of her planned path, its 8x8 cell; exits held by a pack and
crossings that failed, against the exit or seam; and each exit, seam and map
she crossed, with the time it took. Subjects use the same `map:`, `exit:`
and `seam:` names as owner blocks, plus `cell:`. Deaths to other players and
their hits are not recorded, nor is damage on a PvP map, and no row holds a
player or creature name. A respawn is never taken for a crossing,
and a recall only when it lands right beside the next map's arrival square.
Writes are best effort: they wait at most a quarter second for a locked
database, keep a failed write's rows in memory for the next attempt, and
never hold up play. Each signal keeps its own cap (deaths 1,000, damage
5,000), so a flood of one never evicts another, and rows older than 7 days
are deleted. Her route deaths and learned hazards from before this were
migrated once, and her decision ledger's map visits seed the crossing
counts. The Tasks tab's **Learned routes** section lists the 25 subjects
with the highest provisional cost (one death costs 30 minutes of travel,
halving every 6 hours), their signals, the last event and when the cost
falls below half. It is read-only.

Those events become learned route costs (`route_costs.py`), in seconds:
each crossing's travel time (her measured crossing times once she has
them) plus the danger of the map it enters, one death counting 30 minutes
and halving every 6 hours, a recall 15 minutes and a full health bar of
damage 5. A death stops counting once her survivability (health, armour,
protections, level) has genuinely improved since, and each safe crossing
cuts a map's or exit's recall, damage and held-exit cost by 30 %, never its
deaths. Today's hazard tiers become graded content costs that steer the
route but never stop a trip. Behind the owner's `learned_route_costs` flag
(`--learned-route-costs` or `ATRINIK_BOT_LEARNED_ROUTE_COSTS=1`), off by
default, a trip takes the cheapest route by those costs; an errand whose
cheapest route carries one expected death or more of learned danger (a
death counts in full until it is 6 hours old) is declined and retried later, while an essential trip goes anyway with the
owner-block care rules, waiting at most 10 minutes
(`route-budget-override`, `route-strict-override`, then
`essential-care-timeout`). With the flag off, plans and farm choices are
exactly as before, and the learned plans run in the shadow: at most
20 ms a task-engine step, at most one trip every 15 seconds and the farm score
every 5 minutes. Where they differ she records `route-shadow-diverge` or
`route-shadow-decline`, keeps every comparison for 7 days in the runtime
state database (`route_shadow`), and the Tasks tab's **Shadow routes**
section lists the counts per trip kind and the last 20 differences. The
`route-shadow` command (`--hours`, `--json`) prints the same report from
her memory database, read-only, for the owner's divergence report before
any flip. The farm score in the shadow is the analyzer's rate over an hour
minus the learned danger on the spot's route and maps; one fresh death on
the way moves her to any spot rated within half of the top one, and a spot
pauses at 3000 s (4500 s behind a chokepoint).

NPC voyages and quest-gated exits wait behind the owner's `dialogue_edges`
flag (`--dialogue-edges` or `ATRINIK_BOT_DIALOGUE_EDGES=1`), off by default:
the owner turns it on after one supervised voyage. With it off, plans are
exactly as before. With it on, the planner adds the content's dialogue
teleports, such as Sam Goodberry's voyages between Incuna and Brynknot, and
closes the Underground City lake portal until her journal shows Portal of
Llwyfen's "A Strange Portal" done. A voyage is offered only when its quest
state holds in her journal (Sam's Incuna voyage needs Lost Memories
completed; an unloaded journal offers none), when its dialogue only talks
and teleports (no quest step, purchase, enemy or typed text), and when its
arrival has a known way back to where she boarded or to her savebed. The
three voyages that move her savebed are never offered. She takes one by
walking beside the NPC and following its authored pages exactly: a page
that differs, a voyage that does not leave or lands elsewhere is refused
for a day or until her keys or journal change, and is never clicked
through. An owner block can name a voyage as
`talk:SOURCE:NPC>DESTINATION`, and her route events record each voyage
she takes, or fails to start, under that name. The Tasks tab's **Voyages** section shows
the flag, each voyage and why it is or is not offered, and the quest-gated
exits. The design for alternative routes, unlocks such as keys, quests and
NPC voyages, and costs learned from her own experience is in
[docs/ROUTING.md](docs/ROUTING.md).

Route rankings that plan many trips at once (the temple priests, the food,
heal and recall merchants) run on a planner worker thread against a
snapshot of her state, so her connection, safety and combat never wait for
them. An event-loop stall over a second is logged, recorded as
`event-loop-stall` with what was running, and raised as the dashboard's
`loop-stall` warning; see section 1.12 of [docs/ROUTING.md](docs/ROUTING.md).

She can watch the owner play, behind the `watch_owner` flag
(`--watch-owner` or `ATRINIK_BOT_WATCH_OWNER=1`), off by default; with it
off nothing is recorded (the client still decodes the map's damage
numbers into a small in-memory ring and keeps the operator character's
party health). With it on, and only while the operator
character (`ATRINIK_BOT_OPERATOR_CHARACTER`, exact name) is on her map and
she is not fighting herself, each of its fights becomes one
`owner_observations` row in her memory: where it stood and the square's
terrain feature, the monster kinds it hit in order, the most monsters next
to it at once, the damage numbers on its square and its targets, whether
it stepped after a ranged hit, and its health before and after (only while
it shares her party). A fight while another player is in her view is
dropped whole, and no player's name is stored. The Farming tab's
**Watching the owner** section lists them, read-only; nothing she does
depends on them yet. The design, including the later following and rule
phases, is section 4.14 of
[docs/LEARNING_AND_FEEDBACK.md](docs/LEARNING_AND_FEEDBACK.md).

## Dependency and content boundaries

The playtester source is independent of its external protocol, pathfinding,
content-tooling, farming-analyzer, and game-runtime inputs. Their immutable coordinates and licenses are recorded
in [`dependencies.lock.json`](dependencies.lock.json) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). In particular, a wheel or
binary linked with GPL libatrinik pathfinding is not an MIT-only artifact and
must satisfy the GPL's corresponding-source and notice requirements.

## License and provenance

The playtester source is distributed under the [MIT License](LICENSE). The
imported `atrinik_bot` project code was generated and iteratively developed by
OpenAI Codex under Zoey Rose's direct supervision and steering, then reviewed
and accepted by her. Zoey directs its MIT release and grants all rights she
holds in that code. The precise development record, retained history mapping,
and excluded material are recorded in [`PROVENANCE.md`](PROVENANCE.md).

## Protected servers

The playtester uses `atrinik/2` QUIC with a typed game-stream preface.
The locked Classic protocol receives an authenticated admission-policy announcement before setup
or account login. Open servers need no code. Protected servers take one
16-character invite code from an owner-only regular file using
`--access-code-file /private/path`; the code never belongs in command arguments,
environment variables, dashboard state or checkpoints. The server certificate
pin is required for both modes. Account credentials remain separate. The old
join-password option and setup subtype are removed; old peers fail version
negotiation. Codes grant ordinary admission and never operator capabilities.

The MAP decoder follows the coordinated Classic wire contract, including
framed linked depths, ordered continuations and Q5.11/timed-light payloads. It
retains every advertised depth from -6 through +6 atomically; the depth-zero
view remains the navigation compatibility surface, while target selection uses
the authoritative linked-depth actor count. Colored lighting and timed
interpolation are consumed for framing rather than rendered.
