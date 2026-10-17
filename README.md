# Atrinik playtester

Atrinik playtester is an autonomous, headless end-to-end player for Atrinik.
It connects through ordinary player protocols, observes live game state, and
runs navigation, quest, combat, equipment, economy, and survival policies
without privileged server access.

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
installed-input diagnostics.

For a coordinated unreleased Classic protocol change, retain the immutable lock
and point only the development process at that checkout's generated bindings:

```sh
export ATRINIK_PLAYTESTER_PROTOCOL_SOURCE=/path/to/classic/protocol/generated/python
```

The source directory must contain `atrinik_protocol`. Playtester requires the
protocol revision 1084 and rejects an older installed wheel. The immutable
`v5.90.0` lock identifies the matching released wire-1084 wheel, so normal
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

`dependencies` downloads the source and Classic-runtime release archives over
HTTPS, verifies their immutable SHA-256 coordinates, validates their bounded
layouts, and installs both as one atomic cache generation. To seed a machine
from already downloaded files without network access:

```sh
atrinik-playtester dependencies \
  --source-archive /media/atrinik-content-1.14.0.tar.gz \
  --runtime-archive /media/atrinik-content-1.14.0-classic-runtime.tar.gz \
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
an authenticated local tunnel instead of exposing it directly.

`atrinik-playtester --version`, the startup log line, and the dashboard header
name the build: its release tag (with `+N` when the commit is N commits past
it) and short commit, linked to GitHub. CMake records them from Git on every
build; a tree without Git metadata reports an unknown build.

## Farming analyzer

Autoplay and the dashboard rank farm spots with the locked `atrinik/tools`
farming analyzer instead of hard-coded level bands. The dependency installer
caches its release asset and the Classic server source whose experience tables
it reads. During `web` and `autoplay`, a background task checks the character
every 30 seconds. When the killing weapon school's level, the player doll's DAM,
WC, weapon speed, max HP, AC or protections change materially, or the last
ranking is six hours old, it starts one `nice` analyzer process with
`--ranking all --json` (about 10 to 30 seconds) while the bot keeps playing.
Runs are at least ten minutes apart, failures back off for fifteen minutes, and
a failed run keeps the previous ranking. Only melee weapon schools are
simulated so far.

The dashboard's **Farming** tab shows the run status, the simulated profile and
the top spots for the preferred objective: XP, money or loot per hour,
balanced, or safety. The choice is saved per character and needs no new run,
because one run ranks every objective. The rates are model estimates for
comparing spots, not live measurements.

Autoplay always farms the preferred objective's #1 spot. There are no
level bands or curated spots: the analyzer result's map paths become the
circuit's legs (a multi-map circuit rotates through every map), and its listed
monsters become the targets. Spots whose residents include aggressive spawns
get cautious recovery margins (flee at 80% HP, heal at 95%) and isolated pulls
from squares outside other residents' detection radii; other spots use 75%
and 90%. Until the first ranking exists, autoplay holds position safely.
The Tasks tab's **Farm** picker lists the same ranked spots.

The only exceptions are live failures. A spot is quarantined for six hours,
and autoplay moves to the next-ranked spot, after:

- a death there (including on the way there);
- an emergency recall there;
- ten minutes there without any character XP;
- a route there that the world graph cannot plan.

When the analyzer gets a spot wrong, the fix belongs in the analyzer.

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
