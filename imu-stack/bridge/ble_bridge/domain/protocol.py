"""Pure T-Watch IMU wire protocol.

This module contains only protocol knowledge: constants, the CRC32 check, the
byte-offset frame parsing in :func:`frame_to_json`, and a fragment-header parse
helper. It performs no IO and uses no asyncio or network libraries.
"""

from __future__ import annotations

import zlib
from typing import NamedTuple

# ─── Device / GATT identifiers ──────────────────────────────────────
DEVICE_NAME_SUBSTR = "T-Watch-IMU"
SVC_UUID = "6f2d5d52-0f4d-4b2a-a02b-5a7a5b3a0e11"
TX_UUID = "f5c8b9d0-3a5d-4d9d-9d67-2d7f1b9e4b22"
RX_UUID = "e8b6a830-8f6b-4d9c-a71c-6d8c2a3a5f33"

# ─── Frame protocol constants ───────────────────────────────────────
MAGIC_FRAG = 0xB1F1
MAGIC_FRAME = 0xB10F
FRAME_HDR_SIZE = 32
FRAG_HDR_SIZE = 10   # 2 magic + 1 ver + 1 part_idx + 4 seq + 2 frag_len

# ─── Timing (seconds) ───────────────────────────────────────────────
CONNECT_TIMEOUT = 12.0
NOTIFY_TIMEOUT = 8.0
PING_INTERVAL = 4.0
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
SCAN_PERIOD = 8.0
SCAN_DURATION = 5.0
REASM_TTL = 10.0
REASM_CLEAN_PERIOD = 3.0


class FragHeader(NamedTuple):
    """Parsed fragment header fields."""

    magic: int
    part_idx: int
    seq: int
    frag_len: int
    body: bytes


def crc32_ieee(data: bytes) -> int:
    """Return the IEEE CRC32 of ``data`` as an unsigned 32-bit integer."""
    return zlib.crc32(data) & 0xFFFFFFFF


def parse_frag_header(frag: bytes) -> FragHeader:
    """Parse a fragment header, returning its fields and the trailing body.

    The caller is responsible for validating ``magic`` and ``frag_len``; this
    helper performs only the byte-offset decoding.
    """
    magic = int.from_bytes(frag[0:2], "little")
    part_idx = frag[3]
    seq = int.from_bytes(frag[4:8], "little")
    frag_len = int.from_bytes(frag[8:10], "little")
    body = frag[FRAG_HDR_SIZE:]
    return FragHeader(magic, part_idx, seq, frag_len, body)


def frame_to_json(buf: bytes) -> dict:
    """Convert a complete reassembled binary frame to a JSON-ready dict.

    Raises :class:`ValueError` on any structural or CRC mismatch.
    """
    if len(buf) < FRAME_HDR_SIZE + 4:
        raise ValueError("frame too small")
    hdr = buf[:FRAME_HDR_SIZE]
    if int.from_bytes(hdr[0:2], "little") != MAGIC_FRAME:
        raise ValueError("bad magic")

    payload_len = int.from_bytes(hdr[28:32], "little")
    n = int.from_bytes(hdr[22:24], "little")
    axes = hdr[24]
    if axes != 0b111:
        raise ValueError("unsupported axes mask")
    if payload_len != 3 * n * 2:
        raise ValueError("payload_len vs n mismatch")
    if len(buf) != FRAME_HDR_SIZE + payload_len + 4:
        raise ValueError("total size mismatch")

    stored_crc = int.from_bytes(buf[-4:], "little")
    calc_crc = crc32_ieee(buf[:-4])
    if stored_crc != calc_crc:
        raise ValueError(f"CRC mismatch: 0x{stored_crc:08X} vs 0x{calc_crc:08X}")

    dev_id = int.from_bytes(hdr[4:8], "little")
    seq = int.from_bytes(hdr[8:12], "little")
    ts0_ns = int.from_bytes(hdr[12:20], "little")
    fs_hz = int.from_bytes(hdr[20:22], "little")
    batt = hdr[25]

    mv = memoryview(buf)[FRAME_HDR_SIZE:-4]
    ax, ay, az = [0] * n, [0] * n, [0] * n
    off = 0
    for i in range(n):
        ax[i] = int.from_bytes(mv[off:off + 2], "little", signed=True); off += 2
    for i in range(n):
        ay[i] = int.from_bytes(mv[off:off + 2], "little", signed=True); off += 2
    for i in range(n):
        az[i] = int.from_bytes(mv[off:off + 2], "little", signed=True); off += 2

    return {
        "type": "imu",
        "meta": {
            "dev_id": dev_id, "seq": seq, "ts0_ns": ts0_ns,
            "fs_hz": fs_hz, "n": n, "axes": axes, "batt": batt,
        },
        "ax": ax, "ay": ay, "az": az,
    }
