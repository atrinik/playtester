"""Acquire and diagnose the immutable playtester compatibility bundle."""

from __future__ import annotations

import errno
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
from typing import BinaryIO
import urllib.parse
import urllib.request


LOCK_NAME = "dependencies.lock.json"
MARKER_NAME = ".atrinik-playtester-input.json"
BUFFER_SIZE = 1024 * 1024


class CompatibilityError(RuntimeError):
    """A compatibility input is missing, unsafe, or inconsistent."""


def lock_path() -> Path:
    """Return the repository or installed-package compatibility lock."""
    return Path(__file__).with_name(LOCK_NAME)


def load_lock(path: Path | None = None) -> dict:
    """Load and minimally validate the versioned compatibility lock."""
    location = path or lock_path()
    try:
        value = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompatibilityError(f"cannot read compatibility lock {location}: {error}") from error
    if value.get("schema_version") != 2:
        raise CompatibilityError("unsupported compatibility lock schema")
    if not isinstance(value.get("bundle_version"), str):
        raise CompatibilityError("compatibility lock has no bundle version")
    inputs = value.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"content", "pathfinding", "protocol"}:
        raise CompatibilityError("compatibility lock inputs are incomplete")
    content = inputs["content"]
    if not isinstance(content, dict):
        raise CompatibilityError("content lock is incomplete")
    if content.get("branch") != "main":
        raise CompatibilityError("content input must be authored on main")
    for name in ("source", "classic_runtime"):
        artifact = content.get(name)
        required = {
            "artifact", "url", "sha256", "root", "max_download_bytes",
            "max_members", "max_unpacked_bytes", "required_paths",
        }
        if not isinstance(artifact, dict) or not required.issubset(artifact):
            raise CompatibilityError(f"content {name} lock is incomplete")
        try:
            valid_digest = len(artifact["sha256"]) == 64 and int(
                artifact["sha256"], 16) >= 0
        except (TypeError, ValueError):
            valid_digest = False
        if not valid_digest:
            raise CompatibilityError(f"content {name} SHA-256 is invalid")
        parsed = urllib.parse.urlparse(artifact["url"])
        if parsed.scheme != "https" or not parsed.netloc:
            raise CompatibilityError(f"content {name} URL must use HTTPS")
    for name, required in (
        ("pathfinding", {"repository", "release", "archive", "url", "sha256", "module"}),
        ("protocol", {"repository", "release", "artifact", "sha256", "distribution"}),
    ):
        dependency = inputs[name]
        if not isinstance(dependency, dict) or not required.issubset(dependency):
            raise CompatibilityError(f"{name} lock is incomplete")
        try:
            valid_digest = len(dependency["sha256"]) == 64 and int(
                dependency["sha256"], 16) >= 0
        except (TypeError, ValueError):
            valid_digest = False
        if not valid_digest:
            raise CompatibilityError(f"{name} SHA-256 is invalid")
    pathfinding_url = urllib.parse.urlparse(inputs["pathfinding"]["url"])
    if pathfinding_url.scheme != "https" or not pathfinding_url.netloc:
        raise CompatibilityError("pathfinding URL must use HTTPS")
    return value


def lock_digest(lock: dict) -> str:
    """Return the stable identity of a parsed lock."""
    encoded = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def default_cache_root() -> Path:
    """Return a mutable user cache outside the installed source tree."""
    explicit = os.environ.get("ATRINIK_PLAYTESTER_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "atrinik" / "playtester"
    return Path.home() / ".cache" / "atrinik" / "playtester"


def bundle_root(cache: Path, lock: dict) -> Path:
    """Return the immutable generation directory selected by the lock."""
    identity = f"{lock['bundle_version']}-{lock_digest(lock)[:16]}"
    return cache / "bundles" / identity


def _sha256_file(path: Path, maximum: int) -> str:
    if not path.is_file() or path.is_symlink():
        raise CompatibilityError(f"input archive is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(BUFFER_SIZE):
            size += len(chunk)
            if size > maximum:
                raise CompatibilityError(f"input archive exceeds {maximum} bytes: {path}")
            digest.update(chunk)
    return digest.hexdigest()


def _verify_archive(path: Path, spec: dict) -> None:
    actual = _sha256_file(path, int(spec["max_download_bytes"]))
    if actual != spec["sha256"]:
        raise CompatibilityError(
            f"SHA-256 mismatch for {spec['artifact']}: expected {spec['sha256']}, got {actual}"
        )


def _download(spec: dict, downloads: Path, *, offline: bool) -> Path:
    downloads.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = downloads / f"{spec['sha256']}-{spec['artifact']}"
    if destination.exists():
        _verify_archive(destination, spec)
        return destination
    if offline:
        raise CompatibilityError(f"offline cache is missing {spec['artifact']}")

    descriptor, temporary_name = tempfile.mkstemp(prefix="download-", dir=downloads)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    received = 0
    maximum = int(spec["max_download_bytes"])
    try:
        request = urllib.request.Request(
            spec["url"], headers={"User-Agent": "atrinik-playtester/compatibility-v1"}
        )
        with urllib.request.urlopen(request, timeout=30) as response, os.fdopen(
            descriptor, "wb", closefd=True
        ) as output:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or not final.netloc:
                raise CompatibilityError("artifact redirect did not remain on HTTPS")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > maximum:
                raise CompatibilityError(f"download exceeds {maximum} bytes")
            while chunk := response.read(BUFFER_SIZE):
                received += len(chunk)
                if received > maximum:
                    raise CompatibilityError(f"download exceeds {maximum} bytes")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != spec["sha256"]:
            raise CompatibilityError(
                f"SHA-256 mismatch for downloaded {spec['artifact']}"
            )
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            _verify_archive(destination, spec)
        temporary.unlink()
        return destination
    except Exception as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        if isinstance(error, CompatibilityError):
            raise
        raise CompatibilityError(
            f"cannot download {spec['artifact']}: {error}"
        ) from error


def _archive_path(
    spec: dict, downloads: Path, supplied: Path | None, *, offline: bool
) -> Path:
    if supplied is not None:
        supplied = supplied.expanduser().resolve()
        _verify_archive(supplied, spec)
        return supplied
    return _download(spec, downloads, offline=offline)


def _safe_member_path(name: str, expected_root: str) -> Path | None:
    if "\\" in name or name.startswith("/"):
        raise CompatibilityError(f"unsafe archive path: {name!r}")
    pure = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in pure.parts):
        raise CompatibilityError(f"unsafe archive path: {name!r}")
    if not pure.parts or pure.parts[0] != expected_root:
        raise CompatibilityError(f"unexpected archive root: {name!r}")
    relative = pure.parts[1:]
    return Path(*relative) if relative else None


def _copy_member(source: BinaryIO, output: BinaryIO, expected: int) -> None:
    remaining = expected
    while remaining:
        chunk = source.read(min(BUFFER_SIZE, remaining))
        if not chunk:
            raise CompatibilityError("archive member ended before its declared size")
        output.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise CompatibilityError("archive member exceeds its declared size")


def _extract_archive(archive: Path, destination: Path, spec: dict) -> None:
    """Extract a gzip tar after a complete fail-closed metadata pass."""
    destination.mkdir(mode=0o700, parents=True)
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) > int(spec["max_members"]):
                raise CompatibilityError("archive contains too many members")
            total = sum(member.size for member in members if member.isfile())
            if total > int(spec["max_unpacked_bytes"]):
                raise CompatibilityError("archive expands beyond the configured limit")
            seen: set[Path] = set()
            planned: list[tuple[tarfile.TarInfo, Path | None]] = []
            for member in members:
                relative = _safe_member_path(member.name.rstrip("/"), spec["root"])
                if not (member.isdir() or member.isfile()):
                    raise CompatibilityError(f"archive member is not a regular file/directory: {member.name}")
                if relative is not None:
                    if relative in seen:
                        raise CompatibilityError(f"duplicate archive member: {member.name}")
                    seen.add(relative)
                planned.append((member, relative))

            for member, relative in planned:
                if relative is None:
                    continue
                target = destination / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(mode=0o700, exist_ok=True)
                    continue
                source = bundle.extractfile(member)
                if source is None:
                    raise CompatibilityError(f"cannot read archive member: {member.name}")
                with source, target.open("xb") as output:
                    _copy_member(source, output, member.size)
                os.chmod(target, 0o600)
    except (tarfile.TarError, OSError) as error:
        if isinstance(error, CompatibilityError):
            raise
        raise CompatibilityError(f"cannot extract {archive.name}: {error}") from error

    for relative, expected_type in spec["required_paths"].items():
        candidate = destination / relative
        valid = candidate.is_file() if expected_type == "file" else candidate.is_dir()
        if not valid or candidate.is_symlink():
            raise CompatibilityError(
                f"{spec['artifact']} is missing required {expected_type} {relative}"
            )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if path.is_symlink():
            raise CompatibilityError(f"cached input contains a symbolic link: {path}")
        relative = path.relative_to(root).as_posix().encode()
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
            continue
        if not path.is_file():
            raise CompatibilityError(f"cached input is not a regular file: {path}")
        digest.update(b"F\0" + relative + b"\0" + str(path.stat().st_size).encode() + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(BUFFER_SIZE):
                digest.update(chunk)
    return digest.hexdigest()


def _make_tree_readonly(root: Path) -> None:
    """Prevent imports or consumers from changing an immutable generation."""
    paths = sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            raise CompatibilityError(f"cached input contains a symbolic link: {path}")
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(root, 0o555)


def _remove_staging(path: Path) -> None:
    """Remove only an installer-owned unpublished staging directory."""
    def unlock(function, target, _error):
        os.chmod(target, 0o700)
        function(target)

    shutil.rmtree(path, onerror=unlock)


def _publish_staging(staging: Path, destination: Path, cache: Path, lock: dict) -> dict:
    """Publish one generation or accept an identical concurrent winner."""
    try:
        staging.rename(destination)
    except OSError as error:
        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise
        return inspect_bundle(cache, lock, full=True)
    return inspect_bundle(cache, lock, full=True)


def _write_json_atomic(path: Path, value: dict) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix="marker-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def inspect_bundle(cache: Path, lock: dict, *, full: bool) -> dict:
    """Validate and describe the selected installed bundle."""
    root = bundle_root(cache, lock)
    marker_path = root / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompatibilityError(f"compatibility bundle is not installed: {error}") from error
    if marker.get("schema_version") != 1 or marker.get("lock_sha256") != lock_digest(lock):
        raise CompatibilityError("compatibility bundle marker does not match the lock")
    marker_inputs = marker.get("inputs")
    if not isinstance(marker_inputs, dict):
        raise CompatibilityError("compatibility bundle marker is incomplete")
    content = lock["inputs"]["content"]
    for name, directory in (("source", root / "source"), ("classic_runtime", root / "runtime")):
        if directory.is_symlink() or not directory.is_dir():
            raise CompatibilityError(f"cached {name} directory is missing or unsafe")
        for relative, expected_type in content[name]["required_paths"].items():
            candidate = directory / relative
            valid = candidate.is_file() if expected_type == "file" else candidate.is_dir()
            if not valid or candidate.is_symlink():
                raise CompatibilityError(f"cached {name} input is missing {relative}")
        marker_input = marker_inputs.get(name)
        if not isinstance(marker_input, dict) or not isinstance(
                marker_input.get("tree_sha256"), str):
            raise CompatibilityError(
                f"compatibility bundle marker has no {name} tree identity")
        if full and _tree_digest(directory) != marker_input["tree_sha256"]:
            raise CompatibilityError(f"cached {name} input is corrupt")
    return {
        "root": root,
        "source": root / "source",
        "runtime": root / "runtime",
        "marker": marker,
    }


def install_bundle(
    cache: Path,
    lock: dict,
    *,
    source_archive: Path | None = None,
    runtime_archive: Path | None = None,
    offline: bool = False,
) -> dict:
    """Install both content inputs as one immutable atomic cache generation."""
    cache = cache.expanduser().resolve()
    destination = bundle_root(cache, lock)
    if destination.exists():
        return inspect_bundle(cache, lock, full=True)

    content = lock["inputs"]["content"]
    downloads = cache / "downloads"
    source = _archive_path(content["source"], downloads, source_archive, offline=offline)
    runtime = _archive_path(
        content["classic_runtime"], downloads, runtime_archive, offline=offline
    )
    bundles = destination.parent
    bundles.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="bundle-", dir=bundles))
    try:
        _extract_archive(source, staging / "source", content["source"])
        _extract_archive(runtime, staging / "runtime", content["classic_runtime"])
        marker = {
            "schema_version": 1,
            "lock_sha256": lock_digest(lock),
            "bundle_version": lock["bundle_version"],
            "content_revision": content["revision"],
            "content_release": content["release"],
            "inputs": {
                "source": {
                    "artifact": content["source"]["artifact"],
                    "archive_sha256": content["source"]["sha256"],
                    "tree_sha256": _tree_digest(staging / "source"),
                },
                "classic_runtime": {
                    "artifact": content["classic_runtime"]["artifact"],
                    "archive_sha256": content["classic_runtime"]["sha256"],
                    "tree_sha256": _tree_digest(staging / "runtime"),
                },
            },
        }
        _make_tree_readonly(staging / "source")
        _make_tree_readonly(staging / "runtime")
        _write_json_atomic(staging / MARKER_NAME, marker)
        return _publish_staging(staging, destination, cache, lock)
    finally:
        if staging.exists():
            _remove_staging(staging)


def configure_cached_bundle(cache: Path, lock: dict) -> dict:
    """Expose verified cached tools/runtime to the current process."""
    installed = inspect_bundle(cache.expanduser().resolve(), lock, full=True)
    bytecode = cache.expanduser().resolve() / "bytecode"
    bytecode.mkdir(mode=0o700, parents=True, exist_ok=True)
    sys.pycache_prefix = str(bytecode)
    source = str(installed["source"])
    if source not in sys.path:
        sys.path.insert(0, source)
    os.environ.setdefault("ATRINIK_RUNTIME_CONTENT", str(installed["runtime"]))
    return installed


def doctor(cache: Path, lock: dict) -> dict:
    """Diagnose every locked input without mutating cache or source state."""
    checks: list[dict[str, str]] = []

    def check(name: str, operation) -> None:
        try:
            detail = str(operation())
        except Exception as error:  # Report every independent diagnosis.
            checks.append({"name": name, "status": "error", "detail": str(error)})
        else:
            checks.append({"name": name, "status": "ok", "detail": detail})

    installed: dict | None = None

    def inspect() -> str:
        nonlocal installed
        installed = inspect_bundle(cache.expanduser().resolve(), lock, full=True)
        return str(installed["root"])

    check("content-bundle", inspect)

    def content_tools() -> str:
        if installed is None:
            raise CompatibilityError("content bundle is unavailable")
        module = installed["source"] / "tools" / "world_content_audit.py"
        if not module.is_file():
            raise CompatibilityError("content audit tooling is missing")
        return str(module)

    check("content-tooling", content_tools)

    protocol = lock["inputs"]["protocol"]

    def protocol_version() -> str:
        distribution = importlib.metadata.distribution(protocol["distribution"])
        actual = distribution.version
        expected = protocol["release"].removeprefix("v")
        if actual != expected:
            raise CompatibilityError(f"expected {expected}, found {actual}")
        try:
            direct = json.loads(distribution.read_text("direct_url.json") or "")
            archive_info = direct["archive_info"]
            artifact_hash = archive_info.get("hash")
            if artifact_hash is None:
                artifact_hash = "sha256=" + archive_info["hashes"]["sha256"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise CompatibilityError(
                "installed protocol has no verifiable direct-artifact record"
            ) from error
        expected_url = (
            f"https://github.com/{protocol['repository']}/releases/download/"
            f"{protocol['release']}/{protocol['artifact']}"
        )
        if direct.get("url") != expected_url:
            raise CompatibilityError("installed protocol artifact URL does not match the lock")
        if artifact_hash != f"sha256={protocol['sha256']}":
            raise CompatibilityError("installed protocol artifact digest does not match the lock")
        return f"{actual} sha256:{protocol['sha256']}"

    check("protocol", protocol_version)
    pathfinding = lock["inputs"]["pathfinding"]

    def pathfinding_version() -> str:
        module = importlib.import_module(pathfinding["module"])
        actual = getattr(module, "__dependency_release__", "")
        if actual != pathfinding["release"]:
            raise CompatibilityError(
                f"expected {pathfinding['release']}, found {actual or 'unidentified build'}"
            )
        actual_sha256 = getattr(module, "__dependency_sha256__", "")
        if actual_sha256 != pathfinding["sha256"]:
            raise CompatibilityError("pathfinding source digest does not match the lock")
        return f"{actual} sha256:{actual_sha256}"

    check("pathfinding", pathfinding_version)
    return {
        "ok": all(item["status"] == "ok" for item in checks),
        "lock_sha256": lock_digest(lock),
        "content_revision": lock["inputs"]["content"]["revision"],
        "checks": checks,
    }
