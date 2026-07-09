"""Fragment → frame reassembler.

A cohesive reassembly unit: pure buffer state plus an ``asyncio.Lock`` to
serialize concurrent fragment feeds. No network IO. Holds the protocol stats
counters.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional, Set, Tuple

from .protocol import (
    FRAG_HDR_SIZE,
    FRAME_HDR_SIZE,
    MAGIC_FRAG,
    MAGIC_FRAME,
    REASM_CLEAN_PERIOD,
    REASM_TTL,
    parse_frag_header,
)
from ..logging_setup import get_logger

_asm_log = get_logger("asm")


class _ReasmEntry:
    """In-progress reassembly state for a single (dev_id, seq) frame."""

    __slots__ = ("buf", "got", "parts", "seen", "touched")

    def __init__(self, total: int, parts: int, hdr: bytes, tail: bytes):
        self.buf = bytearray(total)
        self.buf[:FRAME_HDR_SIZE] = hdr
        self.buf[FRAME_HDR_SIZE:FRAME_HDR_SIZE + len(tail)] = tail
        self.got = FRAME_HDR_SIZE + len(tail)
        self.parts = parts
        self.seen: Set[int] = {0}
        self.touched = time.monotonic()


class Assembler:
    """Reassembles fragmented BLE notifications into complete frames."""

    def __init__(self) -> None:
        self._map: Dict[Tuple[int, int], _ReasmEntry] = {}
        self._seq_dev: Dict[int, int] = {}
        self._lock = asyncio.Lock()
        self.frames_ok = 0
        self.frames_crc_err = 0
        self.frags_received = 0
        self.frags_dropped = 0

    async def cleanup_loop(self) -> None:
        """Periodically drop reassembly entries that have gone stale."""
        while True:
            await asyncio.sleep(REASM_CLEAN_PERIOD)
            now = time.monotonic()
            async with self._lock:
                stale = [k for k, e in self._map.items() if now - e.touched > REASM_TTL]
                for k in stale:
                    self._seq_dev.pop(k[1], None)
                    del self._map[k]
                if stale:
                    self.frags_dropped += len(stale)
                    _asm_log.debug("gc: expired=%d  pending=%d", len(stale), len(self._map))

    async def feed(self, frag: bytes) -> Optional[bytes]:
        """Feed a BLE notification. Returns completed frame or None."""
        if len(frag) < FRAG_HDR_SIZE:
            return None

        header = parse_frag_header(frag)
        if header.magic != MAGIC_FRAG:
            return None

        self.frags_received += 1
        part_idx = header.part_idx
        seq = header.seq
        frag_len = header.frag_len
        body = header.body

        if frag_len != len(body) or frag_len == 0:
            self.frags_dropped += 1
            _asm_log.debug("frag dropped: seq=%d idx=%d (len mismatch or empty)", seq, part_idx)
            return None

        if part_idx == 0:
            return await self._handle_first(seq, body)
        return await self._handle_continuation(seq, part_idx, body)

    async def _handle_first(self, seq: int, body: bytes) -> Optional[bytes]:
        if len(body) < FRAME_HDR_SIZE:
            return None
        hdr = body[:FRAME_HDR_SIZE]
        if int.from_bytes(hdr[0:2], "little") != MAGIC_FRAME:
            return None

        parts = hdr[27]
        payload_len = int.from_bytes(hdr[28:32], "little")
        total = FRAME_HDR_SIZE + payload_len + 4
        tail = body[FRAME_HDR_SIZE:]
        dev_id = int.from_bytes(hdr[4:8], "little")

        async with self._lock:
            entry = _ReasmEntry(total, parts, hdr, tail)
            self._map[(dev_id, seq)] = entry
            self._seq_dev[seq] = dev_id

        # _asm_log.debug("frag[0]: dev=%d seq=%d parts=%d total=%d B", dev_id, seq, parts, total)

        if entry.got == total:
            return await self._complete(dev_id, seq)
        return None

    async def _handle_continuation(self, seq: int, idx: int, body: bytes) -> Optional[bytes]:
        async with self._lock:
            dev_id = self._seq_dev.get(seq)
            if dev_id is None:
                return None
            entry = self._map.get((dev_id, seq))
            if entry is None or idx in entry.seen:
                return None
            end = entry.got + len(body)
            if end > len(entry.buf):
                del self._map[(dev_id, seq)]
                self._seq_dev.pop(seq, None)
                self.frags_dropped += 1
                _asm_log.warning("frag overflow: dev=%d seq=%d idx=%d (%d > %d)",
                                 dev_id, seq, idx, end, len(entry.buf))
                return None
            entry.buf[entry.got:end] = body
            entry.got = end
            entry.seen.add(idx)
            entry.touched = time.monotonic()
            if entry.got != len(entry.buf):
                return None
        return await self._complete(dev_id, seq)

    async def _complete(self, dev_id: int, seq: int) -> Optional[bytes]:
        async with self._lock:
            entry = self._map.pop((dev_id, seq), None)
            self._seq_dev.pop(seq, None)
        if entry is None:
            return None
        self.frames_ok += 1
        # _asm_log.debug("frame complete: dev=%d seq=%d (%d B)  total_ok=%d",
        #                dev_id, seq, len(entry.buf), self.frames_ok)
        return bytes(entry.buf)

    def stats_line(self) -> str:
        """Return a one-line summary of cumulative reassembly statistics."""
        return (f"frags_in={self.frags_received}  frames_ok={self.frames_ok}  "
                f"crc_err={self.frames_crc_err}  dropped={self.frags_dropped}  "
                f"pending={len(self._map)}")
