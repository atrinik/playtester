"""Regression tests for immutable compatibility input acquisition."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from . import compatibility


class CompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def make_archive(path: Path, root: str, entries: dict[str, bytes | None], *,
                     link: tuple[str, str] | None = None) -> None:
        with tarfile.open(path, "w:gz") as archive:
            top = tarfile.TarInfo(root)
            top.type = tarfile.DIRTYPE
            archive.addfile(top)
            for name, value in entries.items():
                member_name = name if name.startswith("/") else f"{root}/{name}"
                info = tarfile.TarInfo(member_name)
                if value is None:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))
            if link is not None:
                info = tarfile.TarInfo(f"{root}/{link[0]}")
                info.type = tarfile.SYMTYPE
                info.linkname = link[1]
                archive.addfile(info)

    @staticmethod
    def spec(path: Path, root: str, required: dict[str, str], **overrides) -> dict:
        value = {
            "artifact": path.name,
            "url": f"https://example.invalid/{path.name}",
            "sha256": compatibility._sha256_file(path, 1024 * 1024),
            "root": root,
            "max_download_bytes": 1024 * 1024,
            "max_members": 100,
            "max_unpacked_bytes": 1024 * 1024,
            "required_paths": required,
        }
        value.update(overrides)
        return value

    def make_lock(self, source: Path, runtime: Path) -> dict:
        return {
            "schema_version": 2,
            "bundle_version": "test-v1",
            "inputs": {
                "content": {
                    "repository": "atrinik/content",
                    "branch": "main",
                    "revision": "7" * 40,
                    "release": "v1.0.0",
                    "license": "LicenseRef-Atrinik-Content",
                    "disposition": "external",
                    "source": self.spec(
                        source,
                        "source-root",
                        {"tools/world_content_audit.py": "file", "maps": "directory"},
                    ),
                    "classic_runtime": self.spec(
                        runtime,
                        "runtime-root",
                        {"lib/bmaps": "file", "maps": "directory"},
                    ),
                },
                "pathfinding": {
                    "repository": "atrinik/classic",
                    "release": "v5.48.0",
                    "revision": "c" * 40,
                    "archive": "source.tar.gz",
                    "url": "https://example.invalid/source.tar.gz",
                    "sha256": "a" * 64,
                    "module": "atrinik_bot._pathfinding",
                    "license": "GPL-2.0-or-later",
                    "disposition": "external",
                },
                "protocol": {
                    "repository": "atrinik/classic",
                    "release": "v5.48.0",
                    "revision": "c" * 40,
                    "artifact": "protocol.whl",
                    "url": "https://example.invalid/protocol.whl",
                    "sha256": "b" * 64,
                    "distribution": "atrinik-classic-protocol",
                    "license": "GPL-2.0-or-later",
                    "disposition": "external",
                },
            },
        }

    def valid_inputs(self) -> tuple[Path, Path, dict]:
        source = self.root / "source.tar.gz"
        runtime = self.root / "runtime.tar.gz"
        self.make_archive(
            source,
            "source-root",
            {"tools/world_content_audit.py": b"ROOT = 1\n", "maps": None},
        )
        self.make_archive(
            runtime,
            "runtime-root",
            {"lib/bmaps": b"maps\n", "maps": None},
        )
        return source, runtime, self.make_lock(source, runtime)

    def test_repository_lock_binds_current_classic_inputs(self) -> None:
        lock = compatibility.load_lock()
        content = lock["inputs"]["content"]
        self.assertEqual(content["branch"], "main")
        self.assertEqual(content["revision"],
                         "63eb9bb5f02fb9104c2385d5e01c28c3df20b735")
        self.assertEqual(content["release"], "v1.0.0")
        self.assertEqual(
            content["classic_runtime"]["sha256"],
            "e1bffdd1b492472ef594b558c322c655976a95888e7a1d0ab6a56ade2a21b0d5",
        )
        pathfinding = lock["inputs"]["pathfinding"]
        self.assertEqual(pathfinding["repository"], "atrinik/classic")
        self.assertEqual(pathfinding["release"], "v5.48.0")
        self.assertEqual(pathfinding["revision"],
                         "a1af490e841891648015610a3418b0f5ba6daafb")
        protocol = lock["inputs"]["protocol"]
        self.assertEqual(protocol["distribution"], "atrinik-classic-protocol")
        self.assertEqual(protocol["release"], "v5.48.0")
        self.assertEqual(protocol["sha256"],
                         "4a6bdd681ac21e8baa3feebc9398b004c0626d5dcc104c4cd921ec9b2051d25d")

    def test_preflight_verifies_release_assets_and_commit(self) -> None:
        lock = compatibility.load_lock()
        specs = compatibility._release_specs(lock)

        class Response:
            def __init__(self, value: dict) -> None:
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(self.value).encode()

        grouped = {}
        for spec in specs:
            grouped.setdefault(
                (spec["repository"], spec["release"]), []
            ).append(spec)

        def fake_open(request, timeout):
            url = request.full_url
            for (repository, release), values in grouped.items():
                if f"/repos/{repository}/releases/tags/{release}" in url:
                    return Response({
                        "tag_name": release,
                        "assets": [
                            {
                                "name": value["artifact"],
                                "browser_download_url": value["url"],
                                "digest": f"sha256:{value['sha256']}",
                            }
                            for value in values
                        ],
                    })
                if f"/repos/{repository}/commits/{release}" in url:
                    return Response({"sha": values[0]["revision"]})
            raise AssertionError(url)

        result = compatibility.preflight_lock(lock, opener=fake_open)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["checks"]), 4)
        self.assertEqual(
            result["checks"][-1]["artifact"],
            "atrinik_classic_protocol-5.48.0-py3-none-any.whl",
        )

        def missing_asset(request, timeout):
            url = request.full_url
            if "/releases/tags/" in url:
                return Response({"tag_name": "v1.0.0", "assets": []})
            return Response({"sha": specs[0]["revision"]})

        with self.assertRaisesRegex(compatibility.CompatibilityError, "missing asset"):
            compatibility.preflight_lock(lock, opener=missing_asset)

        def missing_repository(request, timeout):
            raise compatibility.urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, None
            )

        with self.assertRaisesRegex(
            compatibility.CompatibilityError,
            "repository atrinik/content or tag v1.0.0 is unavailable",
        ):
            compatibility.preflight_lock(lock, opener=missing_repository)

    def test_lock_validation_rejects_malformed_content_and_digests(self) -> None:
        source, runtime, lock = self.valid_inputs()
        location = self.root / "lock.json"
        for mutation, message in (
            (lambda value: value["inputs"].__setitem__("content", None),
             "content lock is incomplete"),
            (lambda value: value["inputs"]["content"]["source"].__setitem__(
                "sha256", "z" * 64), "content source SHA-256 is invalid"),
        ):
            with self.subTest(message=message):
                candidate = json.loads(json.dumps(lock))
                mutation(candidate)
                location.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(compatibility.CompatibilityError,
                                            message):
                    compatibility.load_lock(location)

    def test_install_is_atomic_and_reusable_offline(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        installed = compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        self.assertEqual(
            (installed["source"] / "tools/world_content_audit.py").read_text(),
            "ROOT = 1\n",
        )
        self.assertTrue((installed["runtime"] / "lib/bmaps").is_file())
        reused = compatibility.install_bundle(cache, lock, offline=True)
        self.assertEqual(reused["root"], installed["root"])
        marker = json.loads((installed["root"] / compatibility.MARKER_NAME).read_text())
        self.assertEqual(marker["content_revision"], "7" * 40)
        self.assertEqual(
            sorted(path.name for path in (cache / "bundles").iterdir()),
            [installed["root"].name],
        )

    def test_atomic_publish_accepts_an_identical_concurrent_winner(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        installed = compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        staging = installed["root"].parent / "bundle-concurrent"
        staging.mkdir()
        reused = compatibility._publish_staging(
            staging, installed["root"], cache, lock)
        self.assertEqual(reused["root"], installed["root"])
        self.assertTrue(staging.exists())

    def test_failed_second_input_leaves_no_bundle(self) -> None:
        source, runtime, lock = self.valid_inputs()
        lock["inputs"]["content"]["classic_runtime"]["sha256"] = "0" * 64
        cache = self.root / "cache"
        with self.assertRaisesRegex(compatibility.CompatibilityError, "SHA-256 mismatch"):
            compatibility.install_bundle(
                cache, lock, source_archive=source, runtime_archive=runtime
            )
        self.assertFalse(compatibility.bundle_root(cache, lock).exists())

    def test_doctor_detects_cached_corruption(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        installed = compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        bmaps = installed["runtime"] / "lib/bmaps"
        bmaps.chmod(0o600)
        bmaps.write_text("corrupt\n")
        result = compatibility.doctor(cache, lock)
        self.assertFalse(result["ok"])
        content = next(item for item in result["checks"]
                       if item["name"] == "content-bundle")
        self.assertEqual(content["status"], "error")
        self.assertIn("corrupt", content["detail"])

    def test_incomplete_marker_fails_closed(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        installed = compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        marker_path = installed["root"] / compatibility.MARKER_NAME
        marker = json.loads(marker_path.read_text())
        marker["inputs"] = {}
        marker_path.write_text(json.dumps(marker))
        with self.assertRaisesRegex(compatibility.CompatibilityError,
                                    "no source tree identity"):
            compatibility.configure_cached_bundle(cache, lock)

    def test_doctor_verifies_all_locked_inputs(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        protocol = lock["inputs"]["protocol"]
        direct = json.dumps({
            "archive_info": {"hash": f"sha256={protocol['sha256']}"},
            "url": protocol["url"],
        })
        distribution = SimpleNamespace(
            version="5.48.0", read_text=lambda name: direct)
        extension = SimpleNamespace(
            __dependency_release__="v5.48.0",
            __dependency_sha256__=lock["inputs"]["pathfinding"]["sha256"],
        )
        with mock.patch.object(compatibility.importlib.metadata, "distribution",
                               return_value=distribution), mock.patch.object(
                                   compatibility.importlib, "import_module",
                                   return_value=extension):
            result = compatibility.doctor(cache, lock)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            [item["name"] for item in result["checks"]],
            ["content-bundle", "content-tooling", "protocol", "pathfinding"],
        )

    def test_doctor_reports_missing_and_incompatible_inputs(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        missing = compatibility.doctor(cache, lock)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["checks"][0]["status"], "error")
        compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        distribution = SimpleNamespace(version="0.0.0", read_text=lambda name: "")
        extension = SimpleNamespace(
            __dependency_release__="v0.0.0", __dependency_sha256__="0" * 64)
        with mock.patch.object(compatibility.importlib.metadata, "distribution",
                               return_value=distribution), mock.patch.object(
                                   compatibility.importlib, "import_module",
                                   return_value=extension):
            incompatible = compatibility.doctor(cache, lock)
        self.assertFalse(incompatible["ok"])
        by_name = {item["name"]: item for item in incompatible["checks"]}
        self.assertIn("expected 5.48.0", by_name["protocol"]["detail"])
        self.assertIn("expected v5.48.0", by_name["pathfinding"]["detail"])

    def test_doctor_remote_diagnostic_reports_preflight_failure(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        with mock.patch.object(
            compatibility,
            "preflight_lock",
            side_effect=compatibility.CompatibilityError(
                "protocol: release atrinik/classic@v5.48.0 is missing asset"
            ),
        ):
            result = compatibility.doctor(cache, lock, verify_remote=True)
        self.assertFalse(result["ok"])
        remote = next(
            item for item in result["checks"] if item["name"] == "lock-availability"
        )
        self.assertEqual(remote["status"], "error")
        self.assertIn("missing asset", remote["detail"])

    def test_configure_uses_cache_without_ambient_checkout(self) -> None:
        source, runtime, lock = self.valid_inputs()
        cache = self.root / "cache"
        installed = compatibility.install_bundle(
            cache, lock, source_archive=source, runtime_archive=runtime
        )
        old_path = list(sys.path)
        old_pycache_prefix = sys.pycache_prefix
        old_runtime = os.environ.pop("ATRINIK_RUNTIME_CONTENT", None)
        try:
            compatibility.configure_cached_bundle(cache, lock)
            self.assertEqual(sys.path[0], str(installed["source"]))
            self.assertEqual(sys.pycache_prefix, str(cache / "bytecode"))
            self.assertEqual(os.environ["ATRINIK_RUNTIME_CONTENT"],
                             str(installed["runtime"]))
        finally:
            sys.path[:] = old_path
            sys.pycache_prefix = old_pycache_prefix
            if old_runtime is None:
                os.environ.pop("ATRINIK_RUNTIME_CONTENT", None)
            else:
                os.environ["ATRINIK_RUNTIME_CONTENT"] = old_runtime

    def test_extraction_rejects_traversal_links_and_wrong_root(self) -> None:
        cases = {
            "traversal": ({"../escape": b"bad"}, None, "unsafe archive path"),
            "absolute": ({"/escape": b"bad"}, None, "unsafe archive path"),
            "link": ({"maps": None}, ("link", "../escape"), "not a regular"),
            "wrong-root": ({"maps": None}, None, "unexpected archive root"),
        }
        for name, (entries, link, message) in cases.items():
            with self.subTest(name=name):
                archive = self.root / f"{name}.tar.gz"
                archive_root = "other" if name == "wrong-root" else "expected"
                self.make_archive(archive, archive_root, entries, link=link)
                spec = self.spec(archive, "expected", {})
                with self.assertRaisesRegex(compatibility.CompatibilityError, message):
                    compatibility._extract_archive(
                        archive, self.root / f"out-{name}", spec
                    )

    def test_extraction_enforces_member_size_count_and_layout(self) -> None:
        archive = self.root / "bounded.tar.gz"
        self.make_archive(archive, "root", {"one": b"1234", "two": b"5678"})
        for override, message in (
            ({"max_members": 2}, "too many members"),
            ({"max_unpacked_bytes": 7}, "configured limit"),
            ({"required_paths": {"missing": "file"}}, "missing required file"),
        ):
            with self.subTest(override=override):
                spec = self.spec(archive, "root", {}, **override)
                with self.assertRaisesRegex(compatibility.CompatibilityError, message):
                    compatibility._extract_archive(
                        archive,
                        self.root / f"bounded-{len(list(self.root.iterdir()))}",
                        spec,
                    )

    def test_offline_mode_refuses_missing_download(self) -> None:
        source, _, lock = self.valid_inputs()
        spec = lock["inputs"]["content"]["source"]
        with self.assertRaisesRegex(compatibility.CompatibilityError,
                                    "offline cache is missing"):
            compatibility._download(spec, self.root / "downloads", offline=True)


if __name__ == "__main__":
    unittest.main()
