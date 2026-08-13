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

## Install and acquire compatible inputs

Build and install the wheel from the repository root in any clean Python 3.11+
environment. The wheel declares the checksum-pinned protocol binding and always
builds the checksum-pinned pathfinding archive selected by the compatibility
lock; it does not bundle or relicense either dependency.

```sh
python3 -m venv /path/to/playtester-environment
/path/to/playtester-environment/bin/pip install .
```

Install the exact content compiler/catalog and Classic runtime selected by
[`dependencies.lock.json`](dependencies.lock.json). The default cache is
`$XDG_CACHE_HOME/atrinik/playtester` or `~/.cache/atrinik/playtester` and can be
overridden with `--cache` or `ATRINIK_PLAYTESTER_CACHE`.

```sh
/path/to/playtester-environment/bin/atrinik-playtester dependencies
/path/to/playtester-environment/bin/atrinik-playtester doctor
```

`dependencies` downloads the source and Classic-runtime release archives over
HTTPS, verifies their immutable SHA-256 coordinates, validates their bounded
layouts, and installs both as one atomic cache generation. To seed a machine
from already downloaded files without network access:

```sh
atrinik-playtester dependencies \
  --source-archive /media/atrinik-content-2.14.0.tar.gz \
  --runtime-archive /media/atrinik-content-2.14.0-classic-runtime.tar.gz \
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

## Dependency and content boundaries

The playtester source is independent of its external protocol, pathfinding,
content-tooling, and game-runtime inputs. Their immutable coordinates and licenses are recorded
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
