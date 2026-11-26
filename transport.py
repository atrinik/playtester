"""Certificate-pinned QUIC stream transport for the game protocol."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ssl
from dataclasses import dataclass
from typing import Any

from .protocol import ProtocolError


QUIC_ALPN = "atrinik/2"
# Typed-stream v1: ATRN magic, game role, reserved byte, big-endian version.
GAME_STREAM_PREFACE = b"ATRN\x01\x00\x00\x01"
# Public alias retained for callers predating the typed-stream naming.
QUIC_GAME_STREAM_PREFACE = GAME_STREAM_PREFACE


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


def _retrieve(future: asyncio.Future) -> None:
    """Mark a future's outcome as seen, so an exception no one awaits is
    never reported as never retrieved."""
    if not future.cancelled():
        future.exception()


async def _handshake(protocol: Any, timeout: float) -> None:
    """Send the first flight and wait at most ``timeout`` seconds for the
    QUIC handshake (``wait_connected=False`` sends nothing by itself).

    aioquic waits on a shielded waiter future that a closing connection
    later fails with ConnectionError. On a timeout that waiter outlives
    the wait: a done-callback retrieves its outcome, and the caller's
    context exit closes the connection."""
    protocol.transmit()
    wait = asyncio.ensure_future(protocol.wait_connected())
    done, _ = await asyncio.wait({wait}, timeout=timeout)
    if wait in done:
        wait.result()
        return
    waiter = getattr(protocol, "_connected_waiter", None)
    if waiter is not None:
        waiter.add_done_callback(_retrieve)
    wait.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await wait
    raise TimeoutError(f"QUIC handshake gave no answer in {timeout:g} s")


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
        # The handshake is awaited here, under our own timeout, not inside
        # the context: a timeout that cancelled aioquic's shielded
        # handshake waiter left its later ConnectionError unretrieved
        # ("Future exception was never retrieved") on every timed-out
        # reconnect.
        context = connect(host, port, configuration=configuration,
                          wait_connected=False)
        protocol = None
        try:
            protocol = await asyncio.wait_for(context.__aenter__(), timeout)
            await _handshake(protocol, timeout)
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
            writer.write(GAME_STREAM_PREFACE)
            await asyncio.wait_for(writer.drain(), timeout)
            return cls(context, reader, writer)
        except BaseException:
            if protocol is not None:
                await context.__aexit__(None, None, None)
            raise

    async def close(self) -> None:
        self.writer.close()
        await self.context.__aexit__(None, None, None)
