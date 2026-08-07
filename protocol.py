"""Binary encoding, framing, and defensive packet decoding."""

from __future__ import annotations

import asyncio
import struct
import zlib
from dataclasses import dataclass


class ProtocolError(RuntimeError):
    pass


class Cursor:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise ProtocolError(
                f"packet underrun at {self.pos}: need {size}, have {self.remaining}"
            )
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def unpack(self, fmt: str):
        size = struct.calcsize(">" + fmt)
        values = struct.unpack(">" + fmt, self.take(size))
        return values[0] if len(values) == 1 else values

    def u8(self) -> int: return self.unpack("B")
    def i8(self) -> int: return self.unpack("b")
    def u16(self) -> int: return self.unpack("H")
    def i16(self) -> int: return self.unpack("h")
    def u32(self) -> int: return self.unpack("I")
    def i32(self) -> int: return self.unpack("i")
    def u64(self) -> int: return self.unpack("Q")
    def i64(self) -> int: return self.unpack("q")
    def f32(self) -> float: return self.unpack("f")
    def f64(self) -> float: return self.unpack("d")

    def cstring(self) -> str:
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise ProtocolError(f"unterminated string at {self.pos}")
        raw = self.data[self.pos:end]
        self.pos = end + 1
        return raw.decode("utf-8", "replace")


class Packet:
    def __init__(self, packet_type: int):
        self.packet_type = packet_type
        self.data = bytearray()

    def add(self, fmt: str, value) -> "Packet":
        self.data.extend(struct.pack(">" + fmt, value))
        return self

    def u8(self, value: int) -> "Packet": return self.add("B", value)
    def u16(self, value: int) -> "Packet": return self.add("H", value)
    def u32(self, value: int) -> "Packet": return self.add("I", value)
    def string(self, value: str) -> "Packet":
        self.data.extend(value.encode("utf-8"))
        self.data.append(0)
        return self

    def encode(self) -> bytes:
        body = bytes((self.packet_type,)) + self.data
        if len(body) > 0xFFFF:
            raise ProtocolError("client packet exceeds 65535 bytes")
        return struct.pack(">H", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    first = (await reader.readexactly(1))[0]
    if first & 0x80:
        rest = await reader.readexactly(2)
        size = ((first & 0x7F) << 16) | (rest[0] << 8) | rest[1]
    else:
        second = (await reader.readexactly(1))[0]
        size = (first << 8) | second
    if size == 0:
        raise ProtocolError("empty server frame")
    return await reader.readexactly(size)


def decompress_frame(frame: bytes, compressed_type: int) -> bytes:
    if frame[0] != compressed_type:
        return frame
    cur = Cursor(frame[1:])
    original_type = cur.u8()
    expected = cur.u32()
    payload = zlib.decompress(cur.take(cur.remaining))
    if len(payload) != expected:
        raise ProtocolError(
            f"compressed packet length mismatch: {len(payload)} != {expected}"
        )
    return bytes((original_type,)) + payload


@dataclass(slots=True)
class Event:
    kind: str
    data: object = None
