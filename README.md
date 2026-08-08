# Atrinik bot

`atrinik_bot` is a Python 3.11+ headless gameplay client and automation engine.
Its world model and safety policy remain Python-owned, while local-grid,
component, and indexed world-graph searches use libatrinik's dependency-light
native pathfinding core through the `_pathfinding` extension.

Build and test the native extension against a sibling libatrinik checkout:

```sh
cmake -S atrinik_bot -B build/atrinik-bot -G Ninja \
  -DATRINIK_PATHFINDING_SOURCE_DIR=/path/to/libatrinik \
  -DBUILD_TESTING=ON
cmake --build build/atrinik-bot
ctest --test-dir build/atrinik-bot --output-on-failure
(cd build/atrinik-bot && \
  PYTHONPATH="$PWD/python:/path/to/content/tools" \
  ATRINIK_RUNTIME_CONTENT=/path/to/collected/content \
  python3 -m unittest -v atrinik_bot.test_bot)
(cd build/atrinik-bot && \
  PYTHONPATH="$PWD/python" \
  python3 -m atrinik_bot.benchmark_pathfinding --repeats 20)
```

Run the Python commands from the build directory as shown so the assembled
package containing the native extension takes precedence over the source-only
package in the repository root.

Without `ATRINIK_PATHFINDING_SOURCE_DIR`, CMake downloads the immutable,
checksum-pinned libatrinik v1.1.5 source from its archived Classic release. The
Python package depends on the checksum-pinned `atrinik-protocol` v1.0.9 wheel
from the archived Classic protocol release rather than copying wire
identifiers. New protocol consumers should use releases from the replacement
`atrinik/protocol` repository once a compatible binding is available there.

World-graph compilation requires the `tools` directory of a compatible Atrinik
content checkout on `PYTHONPATH`. Build its runtime assets with
`python3 tools/build_runtime.py --output build/runtime`, then pass that output
through `--runtime-content` or `ATRINIK_RUNTIME_CONTENT`. To read a server
checkout's current experience table in the dashboard, set
`ATRINIK_SERVER_SOURCE` to the server root; otherwise the bundled early-level
table is used. Store bot memory outside the source tree with `--runtime-state`
or `ATRINIK_BOT_RUNTIME_STATE`.

The operator dashboard intentionally binds only to a loopback address and
rejects cross-origin control requests. Use an authenticated local tunnel when
remote operation is required instead of exposing the HTTP control port.

Runtime databases, state, credentials, logs, caches, and downloaded
dependencies must remain untracked.
