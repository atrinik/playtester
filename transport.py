"""TCP and certificate-pinned QUIC stream transports for the game protocol."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from dataclasses import dataclass
from typing import Any

from .protocol import ProtocolError


QUIC_ALPN = "atrinik/1"


def normalize_certificate_sha256(value: str) -> str:
    """Return a canonical SHA-256 certificate fingerprint."""
    fingerprint = value.replace(":", "").strip().lower()
    if (len(fingerprint) != hashlib.sha256().digest_size * 2 or
            any(ch not in "0123456789abcdef" for ch in fingerprint)):
        raise ValueError(
            "QUIC certificate SHA-256 must contain exactly 64 hexadecimal "
            "characters"
        )
    return fingerprint


@dataclass(slots=True)
class QuicStream:
    """Own an aioquic connection context and its game stream."""

    context: Any
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    @classmethod
    async def connect(cls, host: str, port: int, certificate_sha256: str,
                      timeout: float) -> "QuicStream":
        expected = normalize_certificate_sha256(certificate_sha256)
        try:
            from aioquic.asyncio.client import connect
            from aioquic.quic.configuration import QuicConfiguration
            from cryptography.hazmat.primitives import hashes
        except ImportError as exc:
            raise RuntimeError(
                "QUIC transport requires aioquic; install the bot package "
                "dependencies first"
            ) from exc

        # Atrinik identifies private servers by an explicitly pinned
        # certificate fingerprint, not by a public CA or DNS name. aioquic
        # still verifies the TLS CertificateVerify signature during the
        # handshake; the exact certificate identity is checked below before
        # any game stream is opened or credentials are sent.
        configuration = QuicConfiguration(
            alpn_protocols=[QUIC_ALPN],
            is_client=True,
            verify_mode=ssl.CERT_NONE,
        )
        context = connect(host, port, configuration=configuration)
        protocol = None
        try:
            protocol = await asyncio.wait_for(context.__aenter__(), timeout)
            certificate = protocol._quic.tls._peer_certificate
            if certificate is None:
                raise ProtocolError("QUIC peer did not provide a certificate")
            actual = certificate.fingerprint(hashes.SHA256()).hex()
            if actual != expected:
                raise ProtocolError(
                    "QUIC certificate fingerprint mismatch "
                    f"(expected {expected}, got {actual})"
                )
            reader, writer = await protocol.create_stream()
            return cls(context, reader, writer)
        except BaseException:
            if protocol is not None:
                await context.__aexit__(None, None, None)
            raise

    async def close(self) -> None:
        self.writer.close()
        await self.context.__aexit__(None, None, None)
