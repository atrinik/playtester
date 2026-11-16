"""The owner-only key that guards the operator-assist dashboard route.

Kept free of game imports: the ``operator-assist`` CLI reads it without the
content bundle that the bot itself needs.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

# The header carrying the key, and where the key lives.
KEY_HEADER = "X-Playtester-Operator-Key"
KEY_FILENAME = "operator-assist.key"
DEFAULT_STATE_DIRECTORY = Path("/var/lib/atrinik-playtester")


class OperatorKeyError(RuntimeError):
    """The operator key file is missing, unreadable or not owner-only."""


def operator_key_path(runtime_state: str = "") -> Path:
    """``ATRINIK_BOT_OPERATOR_KEY_FILE``, else ``operator-assist.key`` next to
    the runtime state (``ATRINIK_BOT_RUNTIME_STATE``), else in
    ``/var/lib/atrinik-playtester``."""
    explicit = os.getenv("ATRINIK_BOT_OPERATOR_KEY_FILE", "")
    if explicit:
        return Path(explicit)
    state = runtime_state or os.getenv("ATRINIK_BOT_RUNTIME_STATE", "")
    return (Path(state).parent if state else DEFAULT_STATE_DIRECTORY) / KEY_FILENAME


def read_operator_key(path: Path) -> str:
    """The key, refusing a file other users could read or replace."""
    try:
        with open(path, "rb") as handle:
            info = os.fstat(handle.fileno())
            raw = handle.read(256)
    except OSError as error:
        raise OperatorKeyError(f"cannot read the operator key {path}: "
                               f"{error.strerror or error}") from error
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OperatorKeyError(
            f"the operator key {path} must be owned by this user with mode 0600")
    key = raw.decode("ascii", "replace").strip()
    if len(key) < 32:
        raise OperatorKeyError(f"the operator key {path} is too short")
    return key


def ensure_operator_key(path: Path) -> str:
    """Create the key (mode 0600) unless it exists; return it."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_operator_key(path)
    except OSError as error:
        raise OperatorKeyError(f"cannot create the operator key {path}: "
                               f"{error.strerror or error}") from error
    with os.fdopen(descriptor, "w") as handle:
        handle.write(secrets.token_hex(32) + "\n")
    return read_operator_key(path)


def key_matches(expected: str, offered: str) -> bool:
    """Constant-time comparison; an unset key never matches."""
    return bool(expected) and hmac.compare_digest(
        expected.encode("utf-8"), str(offered or "").encode("utf-8"))
