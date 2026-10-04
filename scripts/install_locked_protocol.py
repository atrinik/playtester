"""Preflight and install the exact Classic protocol wheel from the lock."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compatibility import (  # noqa: E402  (the source tree is the script's package)
    CompatibilityError,
    load_lock,
    preflight_lock,
    protocol_compatibility,
    protocol_requirement,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="preflight and install the protocol dependency selected by the lock"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the preflight result as JSON"
    )
    args = parser.parse_args()

    try:
        lock = load_lock(ROOT / "dependencies.lock.json")
        result = preflight_lock(lock, verify_installed_protocol=False)
    except CompatibilityError as error:
        if args.json:
            print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        else:
            print(f"ERROR lock-availability: {error}", file=sys.stderr)
        return 1

    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                protocol_requirement(lock),
            ],
            check=True,
        )
        result["checks"].append({
            "name": "protocol.compatibility",
            "detail": protocol_compatibility(lock),
        })
    except (CompatibilityError, subprocess.CalledProcessError) as error:
        if isinstance(error, CompatibilityError):
            result["checks"].append({
                "name": "protocol.compatibility",
                "status": "error",
                "detail": str(error),
            })
        result.update(ok=False, error=str(error))
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"ERROR protocol-compatibility: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Verified {len(result['checks'])} locked inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
