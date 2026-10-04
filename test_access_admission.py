"""Source-level handshake gates without content or native pathfinding inputs."""

import asyncio
import unittest

from .client import AtrinikClient, ClientConfig
from .model import GameState
from .protocol import Cursor, ProtocolError
from . import constants as c


class AccessAdmissionTests(unittest.TestCase):
    def access_client(self, code=""):
        client = object.__new__(AtrinikClient)
        client.config = ClientConfig(account="a", password="b", access_code=code)
        client.state = GameState(account="a")
        client._setup_sent = client._account_sent = client._access_accepted = False
        client.transport = "quic"
        client.state.phase = "version"
        sent = []

        async def send(packet):
            sent.append(packet.encode())

        client.send = send
        return client, sent

    def test_access_requires_policy_and_result_before_setup(self):
        client, sent = self.access_client("0123456789ABCDEF")
        raw = c.SOCKET_VERSION.to_bytes(4, "big")
        asyncio.run(client._handle_version(Cursor(raw), raw))
        self.assertEqual(sent, [])
        self.assertEqual(client.state.phase, "access-policy")
        asyncio.run(client._handle_access_policy(Cursor(b"\x01\x01"), b"\x01\x01"))
        self.assertEqual(sent, [b"\x00\x12\x17\x01" + b"0123456789ABCDEF"])
        self.assertFalse(client._setup_sent)
        asyncio.run(client._handle_access_result(Cursor(b"\x01\x00"), b"\x01\x00"))
        self.assertEqual(sent[1], b"\x00\x08\x02\x00\x00\x01\x11\x11\x02\x00")
        self.assertTrue(client._setup_sent)
        asyncio.run(client._handle_setup(Cursor(b""), b""))
        self.assertEqual(sent[2], b"\x00\x06\x07\x01a\x00b\x00")

    def test_open_policy_sends_no_code(self):
        client, sent = self.access_client("0123456789ABCDEF")
        raw = c.SOCKET_VERSION.to_bytes(4, "big")
        asyncio.run(client._handle_version(Cursor(raw), raw))
        asyncio.run(client._handle_access_policy(Cursor(b"\x01\x00"), b"\x01\x00"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2], c.S_SETUP)
        self.assertNotIn(b"0123456789ABCDEF", sent[0])

    def test_missing_or_rejected_code_blocks_account_and_setup(self):
        for code in ("", "0123456789ABCDEF"):
            client, sent = self.access_client(code)
            raw = c.SOCKET_VERSION.to_bytes(4, "big")
            asyncio.run(client._handle_version(Cursor(raw), raw))
            if not code:
                with self.assertRaisesRegex(ProtocolError, "access unavailable"):
                    asyncio.run(client._handle_access_policy(Cursor(b"\x01\x01"), b"\x01\x01"))
                self.assertEqual(sent, [])
            else:
                asyncio.run(client._handle_access_policy(Cursor(b"\x01\x01"), b"\x01\x01"))
                with self.assertRaisesRegex(ProtocolError, "access unavailable"):
                    asyncio.run(client._handle_access_result(Cursor(b"\x01\x01"), b"\x01\x01"))
            self.assertFalse(client._setup_sent)
            self.assertFalse(client._account_sent)

    def test_access_phase_lengths_versions_and_plaintext_fail_closed(self):
        for method, phase in (("_handle_access_policy", "access-policy"),
                              ("_handle_access_result", "access-auth")):
            for raw in (b"", b"\x01", b"\x02\x00", b"\x01\x02", b"\x01\x00\x00"):
                client, sent = self.access_client("0123456789ABCDEF")
                client.state.phase = phase
                with self.assertRaises(ProtocolError):
                    asyncio.run(getattr(client, method)(Cursor(raw), raw))
                self.assertEqual(sent, [])
            for bad_phase in ("version", "setup", "playing"):
                client, sent = self.access_client()
                client.state.phase = bad_phase
                with self.assertRaises(ProtocolError):
                    asyncio.run(getattr(client, method)(Cursor(b"\x01\x00"), b"\x01\x00"))
            client, sent = self.access_client()
            client.state.phase = phase
            client.transport = "tcp"
            with self.assertRaises(ProtocolError):
                asyncio.run(getattr(client, method)(Cursor(b"\x01\x00"), b"\x01\x00"))

    def test_old_protocol_and_pre_admission_setup_are_rejected(self):
        client, sent = self.access_client()
        for raw in ((1080).to_bytes(4, "big"), b"", b"\x00" * 5):
            client.state.phase = "version"
            with self.assertRaises(ProtocolError):
                asyncio.run(client._handle_version(Cursor(raw), raw))
        self.assertEqual(sent, [])
        with self.assertRaises(ProtocolError):
            asyncio.run(client._handle_setup(Cursor(b""), b""))
        self.assertFalse(client._account_sent)

    def test_application_packets_before_access_admission_are_rejected(self):
        for phase in ("version", "access-policy", "access-auth"):
            client, sent = self.access_client("0123456789ABCDEF")
            client.state.phase = phase
            for command in (c.C_SETUP, c.C_PLAYER, c.C_CHARACTERS, c.C_ITEM):
                with self.assertRaises(ProtocolError):
                    asyncio.run(client._dispatch(command, b""))
            self.assertEqual(sent, [])
            self.assertFalse(client._account_sent)

    def test_access_code_is_not_in_config_representation(self):
        config = ClientConfig(access_code="0123456789ABCDEF")
        self.assertNotIn(config.access_code, repr(config))

    def test_access_codes_reject_plain_tcp_and_invalid_values(self):
        for code in ("bad\0tail", "x" * 1024, "I" * 16):
            client = AtrinikClient(ClientConfig(
                account="a", password="b", access_code=code,
                transport="quic"))
            with self.assertRaises(ValueError):
                asyncio.run(client.connect())
        client = AtrinikClient(ClientConfig(
            account="a", password="b", access_code="0123456789ABCDEF",
            transport="tcp"))
        with self.assertRaisesRegex(ValueError, "encrypted QUIC"):
            asyncio.run(client.connect())
