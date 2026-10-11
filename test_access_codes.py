"""Private-file and canonical invite-code boundary regression tests."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .access_codes import normalize_access_code, read_access_code


class AccessCodeInputTests(unittest.TestCase):
    def test_canonical_and_outer_ascii_normalization(self):
        self.assertEqual(normalize_access_code(" \t0123456789abcdef\n"),
                         "0123456789ABCDEF")

    def test_invalid_inputs_never_echo_secret(self):
        for value in ("", "A" * 15, "A" * 17, "I" * 16, "O" * 16,
                      "L" * 16, "U" * 16, "0" * 7 + "-" + "A" * 8,
                      "Ａ" * 16, "A" * 8 + " " + "B" * 7,
                      "A" * 15 + "\0", "\u00a0" + "A" * 16):
            with self.assertRaisesRegex(ValueError, "^invalid invite code$"):
                normalize_access_code(value)

    def test_file_private_regular_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "code"
            path.write_text("0123456789abcdef\n")
            path.chmod(0o600)
            self.assertEqual(read_access_code(path), "0123456789ABCDEF")
            path.chmod(0o400)
            self.assertEqual(read_access_code(path), "0123456789ABCDEF")
            path.chmod(0o640)
            with self.assertRaises(ValueError):
                read_access_code(path)
            path.chmod(0o600)
            path.write_bytes(b"A" * 129)
            with self.assertRaises(ValueError):
                read_access_code(path)
            path.write_bytes(b"\xff" * 16)
            with self.assertRaisesRegex(ValueError, "invalid invite code"):
                read_access_code(path)

    def test_symlink_hardlink_and_wrong_owner_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "code"
            path.write_text("0123456789ABCDEF")
            path.chmod(0o600)
            link = Path(root) / "alias"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_access_code(link)
            link.unlink()
            os.link(path, link)
            with self.assertRaises(ValueError):
                read_access_code(path)
            link.unlink()
            with patch("os.getuid", return_value=os.getuid() + 1):
                with self.assertRaises(ValueError):
                    read_access_code(path)

    def test_short_read_and_changed_file_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "code"
            path.write_text("0123456789ABCDEF")
            path.chmod(0o600)
            with patch("os.read", return_value=b"A"):
                with self.assertRaisesRegex(ValueError, "changed"):
                    read_access_code(path)
            real_read = os.read

            def mutate(fd, size):
                value = real_read(fd, size)
                path.write_text("FEDCBA9876543210")
                return value

            with patch("os.read", side_effect=mutate):
                with self.assertRaisesRegex(ValueError, "changed"):
                    read_access_code(path)

    def test_fifo_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fifo"
            os.mkfifo(path, 0o600)
            with self.assertRaises(ValueError):
                read_access_code(path)

    def test_cli_accepts_file_not_raw_code_or_old_password(self):
        from .cli import parser
        import contextlib
        import io
        command = parser()
        args = command.parse_args(["--access-code-file", "/private/code", "observe"])
        self.assertEqual(args.access_code_file, Path("/private/code"))
        for option in ("--access-code", "--join-password"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    command.parse_args([option, "0123456789ABCDEF", "observe"])
        with patch.dict(os.environ, {"ATRINIK_BOT_JOIN_PASSWORD": "0123456789ABCDEF"}):
            self.assertNotIn("0123456789ABCDEF", repr(command.parse_args(["observe"])))
