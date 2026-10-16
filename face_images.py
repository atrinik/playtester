"""Serve Classic face images from the verified runtime content bundle.

The dashboard renders the live map and inventory with the same images the
graphical client uses. ``lib/atrinik.0`` concatenates every face as
``IMAGE <number> <size> <name>\\n`` followed by ``size`` PNG bytes, and
``lib/animations`` lists the frames of each animation. Observers want a calm
static view, so an animation resolves to its first visible frame.
"""
from __future__ import annotations

import mmap
import re
import threading
from pathlib import Path

FACE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}")
# Padding frames that draw nothing; never choose them as a static image.
INVISIBLE_FRAMES = frozenset({"trans.101", "blank.111"})


class FaceImages:
    """Lazily indexed, read-only face image lookup."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self._lock = threading.Lock()
        self._index: dict[str, tuple[int, int]] | None = None
        self._animations: dict[str, str] | None = None
        self._data: mmap.mmap | None = None

    def _load(self) -> None:
        if self._index is not None:
            return
        with self._lock:
            if self._index is not None:
                return
            index: dict[str, tuple[int, int]] = {}
            path = self.runtime_root / "lib" / "atrinik.0"
            with path.open("rb") as stream:
                data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            position = 0
            while position < len(data):
                end = data.find(b"\n", position)
                if end < 0:
                    break
                parts = data[position:end].split()
                if len(parts) != 4 or parts[0] != b"IMAGE":
                    raise ValueError(f"malformed face image header at byte {position}")
                size = int(parts[2])
                index[parts[3].decode("utf-8", "replace")] = (end + 1, size)
                position = end + 1 + size
            self._animations = self._load_animations()
            self._data = data
            self._index = index

    def _load_animations(self) -> dict[str, str]:
        first_visible: dict[str, str] = {}
        path = self.runtime_root / "lib" / "animations"
        if not path.exists():
            return first_visible
        name = None
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("anim "):
                name = line[5:].strip()
            elif line == "mina":
                name = None
            elif (name and line and not line.startswith("facings ")
                  and line not in INVISIBLE_FRAMES and name not in first_visible):
                first_visible[name] = line
        return first_visible

    def resolve(self, name: str) -> str:
        """Return the face name to draw for a face or animation name."""
        if not FACE_NAME.fullmatch(name or ""):
            return ""
        self._load()
        if name in self._index:
            return name
        return self._animations.get(name, "") if name in self._animations else ""

    def png(self, name: str) -> bytes | None:
        face = self.resolve(name)
        if not face:
            return None
        offset, size = self._index[face]
        return bytes(self._data[offset:offset + size])
