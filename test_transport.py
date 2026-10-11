"""Exercise certificate-pinned QUIC against an actual TLS peer."""

import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .protocol import ProtocolError
from .transport import QuicStream


class QuicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-peer")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder()
                       .subject_name(name).issuer_name(name)
                       .public_key(key.public_key()).serial_number(1)
                       .not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(minutes=5))
                       .sign(key, hashes.SHA256()))
        configuration = QuicConfiguration(
            is_client=False, alpn_protocols=["atrinik/2"])
        configuration.certificate = certificate
        configuration.private_key = key
        self.fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        self.streams = 0
        self.prefaces = []
        self.tasks = []

        def stream_handler(reader, writer):
            self.streams += 1

            async def echo():
                try:
                    self.prefaces.append(await reader.readexactly(8))
                    writer.write(await reader.readexactly(5))
                    await writer.drain()
                finally:
                    writer.close()

            self.tasks.append(asyncio.create_task(echo()))

        self.server = await serve(
            "127.0.0.1", 0, configuration=configuration,
            stream_handler=stream_handler)
        self.port = self.server._transport.get_extra_info("sockname")[1]

    async def asyncTearDown(self):
        self.server.close()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_current_classic_alpn_can_exchange_stream_bytes(self):
        stream = await QuicStream.connect(
            "127.0.0.1", self.port, self.fingerprint, 2.0)
        try:
            stream.writer.write(b"probe")
            await stream.writer.drain()
            self.assertEqual(
                await asyncio.wait_for(stream.reader.readexactly(5), 2),
                b"probe")
            self.assertEqual(self.streams, 1)
            self.assertEqual(self.prefaces, [b"ATRN\x01\x00\x00\x01"])
        finally:
            await stream.close()

    async def test_wrong_pin_opens_no_game_stream(self):
        with self.assertRaisesRegex(ProtocolError, "fingerprint mismatch"):
            await QuicStream.connect("127.0.0.1", self.port, "0" * 64, 2.0)
        self.assertEqual(self.streams, 0)
