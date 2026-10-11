"""Direct player commands use the current fixed-width wire payloads."""

import struct
import unittest
from unittest.mock import Mock

from . import constants as c
from .client import AtrinikClient, ClientConfig


class DirectCommandWireTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = AtrinikClient(ClientConfig())
        self.frames = []

        async def capture(packet):
            self.frames.append(packet.encode())

        self.client.send = capture
        self.client.record_action = Mock()

    def assert_frame(self, command, payload):
        self.assertEqual(len(self.frames), 1)
        frame = self.frames.pop()
        self.assertEqual(len(frame), 3 + len(payload))
        self.assertEqual(struct.unpack(">H", frame[:2])[0], 1 + len(payload))
        self.assertEqual(frame[2], command)
        self.assertEqual(frame[3:], payload)

    async def test_move_direction_run_and_zero_epoch(self):
        for direction in range(9):
            for run in (False, True):
                with self.subTest(direction=direction, run=run):
                    await self.client.move(direction, run)
                    self.assert_frame(c.S_MOVE,
                                      struct.pack(">BBI", direction, int(run), 0))
                    self.client.record_action.assert_called_with(
                        "retreat" if run else "step", f"direction={direction}")

    async def test_invalid_move_does_not_send_or_record(self):
        for direction in (-1, 9):
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(ValueError, "direction must be 0..8"):
                    await self.client.move(direction)
        self.assertEqual(self.frames, [])
        self.client.record_action.assert_not_called()

    async def test_fire_always_includes_tag_and_zero_epoch(self):
        for direction in (1, 8):
            for tag in (0, 0x12345678, 0xFFFFFFFF):
                with self.subTest(direction=direction, tag=tag):
                    await self.client.fire(direction, tag)
                    self.assert_frame(c.S_FIRE,
                                      struct.pack(">BII", direction, tag, 0))
                    self.client.record_action.assert_called_with(
                        "fire", f"{tag or 'readied item'} direction={direction}")

    async def test_fire_default_uses_zero_tag(self):
        await self.client.fire(3)
        self.assert_frame(c.S_FIRE, b"\x03" + b"\0" * 8)
        self.client.record_action.assert_called_once_with(
            "fire", "readied item direction=3")
