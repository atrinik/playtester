"""Bounded invite-code input; secrets remain outside argv and mutable state."""

from __future__ import annotations

import os
import stat
from pathlib import Path

ALPHABET = frozenset("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def normalize_access_code(value: str) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("invalid invite code")
    code = value.strip(" \t\r\n\v\f").upper()
    if len(code) != 16 or any(ch not in ALPHABET for ch in code):
        raise ValueError("invalid invite code")
    return code


def read_access_code(path: Path) -> str:
    """Read a no-follow current-owner private regular file, at most128 bytes."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)
                or before.st_nlink != 1 or before.st_size > 128):
            raise ValueError("invite-code file must be owner-only and regular")
        raw = os.read(fd, 129)
        after = os.fstat(fd)
        if (len(raw) > 128 or len(raw) != before.st_size
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("invite-code file changed while reading")
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError("invalid invite code") from None
        return normalize_access_code(value)
    finally:
        os.close(fd)
