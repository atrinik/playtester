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
  --analyzer-archive /media/atrinik-tools-farming-analyzer-3.10.0.tar.gz \
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
| `observations_dropped_total` | `reason` | readings the ingestion filter refused: `chat`, `player-link`, `who-list`, `player-name`, `signed-book`, `unmatched-book`, `book-unverified`, `renamed-item`, `examine-interrupted`, `unauthored-item` |

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
  books whose text matches no authored text, renamed items and any line or
  page naming another character she has seen (chat senders, `/who`, players
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
  never the one received. An examine is kept only when the item's shown
  name is an authored name and every line is one of the forms the server's
  examine sends; one soulbound to someone is dropped. Dialogue pages are
  not kept (NPC scripts build them from player data; see the design's
  phase F13). An unattributed line is stored, as `system:<map>`, only when
  it is a fact line (for now "Something blocks your spellcasting."). The
  same text from the same source raises the row's `seen` count.
- **The authored-text index** is scanned from the locked content (maps,
  archetypes, artifacts, materials and the book message file, about 1.3
  seconds) when the `web` or `autoplay` command starts. Without it only
  fact lines are kept.
- **Bounded.** At most 50,000 rows a character, 4,000 characters of text a
  row; when full, the least recently seen rows that no later table refers to
  are evicted first. Writes wait at most 0.25 seconds for a locked database
  and otherwise drop the reading, never the game line.

The dashboard's read-only **Knowledge** tab lists recent readings by source
(`GET /api/knowledge?kind=SOURCE&limit=N`, newest first, up to 1000, under
the same loopback and `Host` rules as `/api/state`).

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
   in the last 10 seconds, health at least 90% and food at least 300; and
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
simulated so far.

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
before any spot whose approach is unknown or expected to cost more than her
whole health bar, keeping the analyzer's order within each objective's ranking. A risky
approach is taken only when nothing else is usable, and then the safest one
first (unreachable counts as 0.5). An apartment savebed is judged from its
recorded return, since the apartment is left only through a script the
analyzer does not follow, and an origin the analyzer rejects is retried once
from the default origins and not passed again. Raw rankings list every
circuit permutation, so each run keeps the top 40 per objective. The Farming
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
and farm the next-ranked spot meanwhile.

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
carries are read from her inventory, including containers she has opened. A map where
she recently died on the way somewhere is avoided for a while when another
route exists. The proposed design for
alternative routes, unlocks such as keys, quests and NPC voyages, and costs
learned from her own experience is in [docs/ROUTING.md](docs/ROUTING.md).

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
