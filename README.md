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

The migrated implementation provides direct Classic TCP and QUIC transports,
account and character setup, a live world model, content-derived navigation,
16 formal quest executors, adaptive farming through level 115, persistent
memory, and a loopback operator dashboard.

The current `autoplay` command completes the introductory flow and then levels;
the all-quest runner remains separate, and Zechna is not yet an automated
completion gate. The roadmap issues track the work needed to turn these pieces
into the single start-to-finish campaign described above.

## Build and test

Requirements are Python 3.11+, CMake 3.21+, Ninja, and a C17 compiler. The
native adapter uses the checksum-pinned Classic libatrinik pathfinding release.

```sh
cmake -S . -B build/playtester -G Ninja -DBUILD_TESTING=ON
cmake --build build/playtester --parallel
ctest --test-dir build/playtester --output-on-failure
```

The complete Python suite additionally needs the `tools` directory and a
collected runtime from the exact content revision recorded in
[`dependencies.lock.json`](dependencies.lock.json):

```sh
python3 /path/to/content/tools/build_runtime.py \
  --source /path/to/content \
  --source-commit 96073eeff1854fc29347fdafd32e622394f24c07 \
  --output build/content-runtime

(cd build/playtester && \
  PYTHONPATH="$PWD/python:/path/to/content/tools" \
  ATRINIK_RUNTIME_CONTENT=/path/to/playtester/build/content-runtime \
  python3 -m unittest -v atrinik_bot.test_bot)
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
ATRINIK_BOT_CHARACTER=Playtester \
ATRINIK_BOT_RUNTIME_STATE=/private/state/playtester.sqlite3 \
ATRINIK_RUNTIME_CONTENT=/path/to/collected/content \
atrinik-playtester autoplay
```

Use `atrinik-playtester --help` for transport, registration, character,
dashboard, navigation, quest, farming, banking, selling, and storage options.
The dashboard binds to loopback and rejects cross-origin control requests; use
an authenticated local tunnel instead of exposing it directly.

## Dependency and content boundaries

The playtester source is independent of its external protocol, pathfinding,
and game-content inputs. Their immutable coordinates and licenses are recorded
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
