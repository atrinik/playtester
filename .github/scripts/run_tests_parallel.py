#!/usr/bin/env python3
"""Run atrinik_bot.test_bot across forked workers after one world-graph build.

Building the world graph from the content bundle costs more than every test
together, and ``built_graph()`` caches it only per process. Build it once,
then fork: workers inherit the graph copy-on-write and each runs an
interleaved slice of the suite, kept in original order so class fixtures run
once per worker. Exits nonzero if any worker fails or reports a failure.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()

    from atrinik_bot.compatibility import configure_cached_bundle, default_cache_root, load_lock
    configure_cached_bundle(default_cache_root(), load_lock())
    from atrinik_bot import test_bot

    started = time.monotonic()
    test_bot.built_graph()
    print(f"world graph built once in {time.monotonic() - started:.1f}s", flush=True)

    tests = list(flatten(unittest.defaultTestLoader.loadTestsFromName("atrinik_bot.test_bot")))
    workers = max(1, min(args.workers, len(tests)))
    children: dict[int, tuple[int, int]] = {}
    for index in range(workers):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            mine = unittest.TestSuite(tests[index::workers])
            stream = io.StringIO()
            result = unittest.TextTestRunner(stream=stream, verbosity=1).run(mine)
            report = {"index": index, "ran": result.testsRun, "output": stream.getvalue(),
                      "ok": result.wasSuccessful()}
            with os.fdopen(write_fd, "w") as pipe:
                json.dump(report, pipe)
            os._exit(0)
        os.close(write_fd)
        children[pid] = (index, read_fd)

    ran, ok = 0, True
    for pid, (index, read_fd) in children.items():
        with os.fdopen(read_fd) as pipe:
            payload = pipe.read()
        _, status = os.waitpid(pid, 0)
        if not payload or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            print(f"::error::test worker {index} exited abnormally (status {status})")
            ok = False
            continue
        report = json.loads(payload)
        ran += report["ran"]
        ok = ok and report["ok"]
        print(f"--- worker {index}: {report['ran']} tests, {'OK' if report['ok'] else 'FAILED'}")
        if not report["ok"]:
            print(report["output"])
    print(f"Ran {ran} of {len(tests)} tests across {workers} workers in "
          f"{time.monotonic() - started:.1f}s: {'OK' if ok and ran == len(tests) else 'FAILED'}")
    return 0 if ok and ran == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
