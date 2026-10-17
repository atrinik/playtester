"""Source revision this playtester was built from.

CMake records the release tag, commits since it, and the commit in
``_build_info.json`` on every build (see ``scripts/build_info.cmake``). A
package built without Git metadata reports an unknown revision.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

REPOSITORY_URL = "https://github.com/atrinik/playtester"
BUILD_INFO_PATH = Path(__file__).with_name("_build_info.json")
_TAG = re.compile(r"v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def describe_build(raw: Any) -> dict[str, Any]:
    """Normalize recorded build data into what the dashboard displays."""
    data = raw if isinstance(raw, dict) else {}
    tag = data.get("tag") if isinstance(data.get("tag"), str) else ""
    tag = tag if _TAG.fullmatch(tag) else ""
    distance = data.get("distance")
    distance = (distance if isinstance(distance, int) and
                not isinstance(distance, bool) and distance >= 0 else 0)
    commit = data.get("commit") if isinstance(data.get("commit"), str) else ""
    commit = commit if _COMMIT.fullmatch(commit) else ""
    dirty = data.get("dirty") is True
    short = commit[:7]
    # A release build is exactly its tag; later commits show how far ahead.
    version = (tag + (f"+{distance}" if distance else "")) if tag else ""
    label = " · ".join(part for part in (version or "unreleased", short) if part)
    if not commit:
        label = "unknown build"
    if dirty:
        label += " (modified)"
    return {
        "version": version,
        "tag": tag,
        "distance": distance,
        "commit": commit,
        "short_commit": short,
        "dirty": dirty,
        "label": label,
        "release_url": f"{REPOSITORY_URL}/releases/tag/{tag}" if tag else "",
        "commit_url": f"{REPOSITORY_URL}/commit/{commit}" if commit else "",
    }


@lru_cache(maxsize=1)
def build_info() -> dict[str, Any]:
    try:
        raw = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return describe_build(raw)
