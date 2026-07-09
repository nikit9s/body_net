#!/usr/bin/env python3
"""BLE → WebSocket bridge for T-Watch IMU devices.

Discovers T-Watch-IMU devices, connects via BLE, reassembles fragmented
binary frames, verifies CRC32, converts to JSON and broadcasts over WS.
Handles up to MAX_DEVICES simultaneously with automatic reconnection.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
import zlib
from typing import Dict, Optional, Set, Tuple

from bleak import BleakClient, BleakError, BleakScanner
from websockets import serve
from websockets.exceptions import ConnectionClosed

# ─── Logging ─────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("LOG_LEVEL", "DEBUG" if os.environ.get("VERBOSE", "1") == "1" else "INFO")

class _Formatter(logging.Formatter):
    LEVEL_TAG = {
        logging.DEBUG:    "DBG",
        logging.INFO:     "INF",
        logging.WARNING:  "WRN",
        logging.ERROR:    "ERR",
        logging.CRITICAL: "CRT",
    }
    LEVEL_COLOR = {
        logging.DEBUG:    "\033[37m",     # white/gray
        logging.INFO:     "\033[36m",     # cyan
        logging.WARNING:  "\033[33m",     # yellow
        logging.ERROR:    "\033[31m",     # red
        logging.CRITICAL: "\033[1;31m",   # bold red
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self._color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms = int(record.created * 1000) % 1000
        tag = self.LEVEL_TAG.get(record.levelno, "???")
        name = record.name.split(".")[-1]
        msg = record.getMessage()

        if self._color:
            c = self.LEVEL_COLOR.get(record.levelno, "")
            return f"{ts}.{ms:03d} {c}{tag}{self.RESET} [{name:>4s}] {msg}"
        return f"{ts}.{ms:03d} {tag} [{name:>4s}] {msg}"


def _setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(use_color=sys.stdout.isatty()))
    root = logging.getLogger("bridge")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.DEBUG))
    return root


_root_log = _setup_logging()

def _log(name: str) -> logging.Logger:
    return _root_log.getChild(name)

# ─── Configuration ───────────────────────────────────────────────────

DEVICE_NAME_SUBSTR = "T-Watch-IMU"
SVC_UUID = "6f2d5d52-0f4d-4b2a-a02b-5a7a5b3a0e11"
TX_UUID  = "f5c8b9d0-3a5d-4d9d-9d67-2d7f1b9e4b22"
RX_UUID  = "e8b6a830-8f6b-4d9c-a71c-6d8c2a3a5f33"

WS_HOST     = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT     = int(os.environ.get("WS_PORT", "8765"))
MAX_DEVICES = int(os.environ.get("MAX_DEVICES", "4"))

# Protocol constants
MAGIC_FRAG     = 0xB1F1
MAGIC_FRAME    = 0xB10F
FRAME_HDR_SIZE = 32
FRAG_HDR_SIZE  = 10   # 2 magic + 1 ver + 1 part_idx + 4 seq + 2 frag_len

# Timing (seconds)
CONNECT_TIMEOUT    = 12.0
NOTIFY_TIMEOUT     = 8.0
PING_INTERVAL      = 4.0
RECONNECT_MIN      = 1.0
RECONNECT_MAX      = 30.0
# Windows are produced every (WIN_N-OVERLAP_N)/FS_HZ ≈ 0.64s while actively
# streaming, so 15s of zero binary fragments (as opposed to zero bytes at
# all — see _last_frame_data) means the watch's IMU pipeline has stalled
# even though the BLE link itself still answers PING with PONG.
FRAME_STALL_TIMEOUT = 15.0
SCAN_PERIOD        = 8.0
SCAN_DURATION      = 5.0
REASM_TTL          = 10.0
REASM_CLEAN_PERIOD = 3.0

# ─── CRC32 ───────────────────────────────────────────────────────────

def crc32_ieee(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF

# ─── WebSocket Hub ───────────────────────────────────────────────────

_ws_log = _log("ws")

class Hub:
    """Manages WS client subscriptions and broadcasts."""

    def __init__(self):
        self._json: Set = set()
        self._bin: Set = set()
        self._lock = asyncio.Lock()
        self._broadcast_count = 0

    async def add(self, ws, kind: str):
        async with self._lock:
            (self._json if kind == "json" else self._bin).add(ws)
            _ws_log.info("client connected  type=%s  json=%d  bin=%d",
                         kind, len(self._json), len(self._bin))

    async def remove(self, ws):
        async with self._lock:
            was_json = ws in self._json
            was_bin = ws in self._bin
            self._json.discard(ws)
            self._bin.discard(ws)
            if was_json or was_bin:
                _ws_log.info("client disconnected  json=%d  bin=%d",
                             len(self._json), len(self._bin))

    async def broadcast_json(self, obj: dict):
        data = json.dumps(obj)
        async with self._lock:
            targets = list(self._json)
        if not targets:
            return
        dead = []
        for ws in targets:
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)
        self._broadcast_count += 1

    async def broadcast_bin(self, data: bytes):
        async with self._lock:
            targets = list(self._bin)
        dead = []
        for ws in targets:
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)

    @property
    def json_client_count(self) -> int:
        return len(self._json)


hub = Hub()

# ─── WS routing ─────────────────────────────────────────────────────

async def _ws_keepalive(ws):
    try:
        async for _ in ws:
            pass
    except ConnectionClosed:
        pass  # client vanished without a clean close handshake — not an error
    finally:
        await hub.remove(ws)


async def ws_router(ws):
    path = getattr(ws, "path", "/").split("?", 1)[0].rstrip("/") or "/"
    if path in ("/ws/json", "/"):
        await hub.add(ws, "json")
        await _ws_keepalive(ws)
    elif path == "/ws/bin":
        await hub.add(ws, "bin")
        await _ws_keepalive(ws)
    else:
        _ws_log.warning("rejected connection to unknown path=%s", path)
        await ws.close(code=1008, reason="use /ws/json or /ws/bin")

# ─── Frame reassembler ──────────────────────────────────────────────

_asm_log = _log("asm")

class _ReasmEntry:
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
    """Reassembles fragments into full frames.

    Shared across all connected devices, so fragments are keyed by
    ``(conn_id, seq)`` where ``conn_id`` is the BLE connection's own address —
    not by ``seq`` alone. ``seq`` is a per-device counter that restarts at 0
    on every connection, so with several devices streaming at the same rate
    it collides constantly across devices; keying purely on ``seq`` (as this
    used to) let one device's continuation fragments get appended into
    another device's in-progress buffer, corrupting the tail of the frame
    (and failing its CRC) while leaving the header — parsed only from the
    first fragment — intact.
    """

    def __init__(self):
        self._map: Dict[Tuple[str, int], _ReasmEntry] = {}
        self._lock = asyncio.Lock()
        self.frames_ok = 0
        self.frames_crc_err = 0
        self.frags_received = 0
        self.frags_dropped = 0

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(REASM_CLEAN_PERIOD)
            now = time.monotonic()
            async with self._lock:
                stale = [k for k, e in self._map.items() if now - e.touched > REASM_TTL]
                for k in stale:
                    del self._map[k]
                if stale:
                    self.frags_dropped += len(stale)
                    _asm_log.debug("gc: expired=%d  pending=%d", len(stale), len(self._map))

    async def feed(self, conn_id: str, frag: bytes) -> Optional[bytes]:
        """Feed a BLE notification from connection ``conn_id``. Returns completed frame or None."""
        if len(frag) < FRAG_HDR_SIZE:
            return None

        magic = int.from_bytes(frag[0:2], "little")
        if magic != MAGIC_FRAG:
            return None

        self.frags_received += 1
        part_idx = frag[3]
        seq      = int.from_bytes(frag[4:8], "little")
        frag_len = int.from_bytes(frag[8:10], "little")
        body     = frag[FRAG_HDR_SIZE:]

        if frag_len != len(body) or frag_len == 0:
            self.frags_dropped += 1
            _asm_log.debug("frag dropped: seq=%d idx=%d (len mismatch or empty)", seq, part_idx)
            return None

        if part_idx == 0:
            return await self._handle_first(conn_id, seq, body)
        return await self._handle_continuation(conn_id, seq, part_idx, body)

    async def _handle_first(self, conn_id: str, seq: int, body: bytes) -> Optional[bytes]:
        if len(body) < FRAME_HDR_SIZE:
            return None
        hdr = body[:FRAME_HDR_SIZE]
        if int.from_bytes(hdr[0:2], "little") != MAGIC_FRAME:
            return None

        parts       = hdr[27]
        payload_len = int.from_bytes(hdr[28:32], "little")
        total       = FRAME_HDR_SIZE + payload_len + 4
        tail        = body[FRAME_HDR_SIZE:]

        async with self._lock:
            entry = _ReasmEntry(total, parts, hdr, tail)
            self._map[(conn_id, seq)] = entry

        if entry.got == total:
            return await self._complete(conn_id, seq)
        return None

    async def _handle_continuation(self, conn_id: str, seq: int, idx: int, body: bytes) -> Optional[bytes]:
        async with self._lock:
            entry = self._map.get((conn_id, seq))
            if entry is None or idx in entry.seen:
                return None
            end = entry.got + len(body)
            if end > len(entry.buf):
                del self._map[(conn_id, seq)]
                self.frags_dropped += 1
                _asm_log.warning("frag overflow: conn=%s seq=%d idx=%d (%d > %d)",
                                 conn_id, seq, idx, end, len(entry.buf))
                return None
            entry.buf[entry.got:end] = body
            entry.got = end
            entry.seen.add(idx)
            entry.touched = time.monotonic()
            if entry.got != len(entry.buf):
                return None
        return await self._complete(conn_id, seq)

    async def _complete(self, conn_id: str, seq: int) -> bytes:
        async with self._lock:
            entry = self._map.pop((conn_id, seq), None)
        if entry is None:
            return None
        self.frames_ok += 1
        return bytes(entry.buf)

    def stats_line(self) -> str:
        return (f"frags_in={self.frags_received}  frames_ok={self.frames_ok}  "
                f"crc_err={self.frames_crc_err}  dropped={self.frags_dropped}  "
                f"pending={len(self._map)}")

# ─── Frame → JSON ───────────────────────────────────────────────────

def frame_to_json(buf: bytes) -> dict:
    if len(buf) < FRAME_HDR_SIZE + 4:
        raise ValueError("frame too small")
    hdr = buf[:FRAME_HDR_SIZE]
    if int.from_bytes(hdr[0:2], "little") != MAGIC_FRAME:
        raise ValueError("bad magic")

    payload_len = int.from_bytes(hdr[28:32], "little")
    n    = int.from_bytes(hdr[22:24], "little")
    axes = hdr[24]
    if axes != 0b111:
        raise ValueError("unsupported axes mask")
    if payload_len != 3 * n * 2:
        raise ValueError("payload_len vs n mismatch")
    if len(buf) != FRAME_HDR_SIZE + payload_len + 4:
        raise ValueError("total size mismatch")

    stored_crc = int.from_bytes(buf[-4:], "little")
    calc_crc   = crc32_ieee(buf[:-4])
    if stored_crc != calc_crc:
        raise ValueError(f"CRC mismatch: 0x{stored_crc:08X} vs 0x{calc_crc:08X}")

    dev_id = int.from_bytes(hdr[4:8], "little")
    seq    = int.from_bytes(hdr[8:12], "little")
    ts0_ns = int.from_bytes(hdr[12:20], "little")
    fs_hz  = int.from_bytes(hdr[20:22], "little")
    batt   = hdr[25]

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

# ─── BLE helpers ─────────────────────────────────────────────────────

def _dev_name(dev, ad) -> str:
    return getattr(ad, "local_name", None) or getattr(dev, "name", None) or ""


async def _write_cmd(cli: BleakClient, data: bytes) -> bool:
    for response in (True, False):
        try:
            await cli.write_gatt_char(RX_UUID, data, response=response)
            return True
        except Exception:
            continue
    return False

# ─── Device Session ──────────────────────────────────────────────────

class DeviceSession:
    """Manages a single T-Watch: connect → handshake → stream → reconnect."""

    def __init__(self, address: str, name: str, asm: Assembler, handshake_lock: asyncio.Semaphore):
        self.address = address
        self.name = name
        self.asm = asm
        # Serializes connect+handshake (TIME/START) across all sessions —
        # multiple devices starting their handshake in the same instant
        # (e.g. right after the bridge restarts and reconnects to several
        # already-known devices at once) overloads the host's BLE stack
        # badly enough that ACKs get lost for everyone. Streaming itself,
        # once a device is past its handshake, stays fully concurrent.
        self._handshake_lock = handshake_lock
        self._handshake_lock_held = False
        self.connected = False
        self._log = _log("dev")
        self._failures = 0
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._disconnect_event = asyncio.Event()
        self._last_data = 0.0
        self._last_frame_data = 0.0
        self._session_frames = 0
        self._cli: Optional[BleakClient] = None
        # Set by _process_notifications when ACK:STOP comes back, so a
        # graceful stop() can confirm the watch actually quiesced before we
        # yank the BLE link — an unconfirmed STOP is exactly what leaves the
        # watch mid-stream for the next bridge run to fight with.
        self._stop_ack = asyncio.Event()
        # Whether ACK:TIME actually came back this connection. When it
        # didn't, the watch's own clock may still hold an offset from a
        # much earlier session (or none at all), so frames get stamped with
        # the bridge's wall clock at receipt instead of the device's ts0_ns
        # — see _process_notifications.
        self._time_synced = False

    @property
    def _addr(self) -> str:
        return self.address[-8:]

    def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            self._log.info("%s  session started  name=%s", self._addr, self.name)

    async def stop(self):
        self._stop.set()
        if self.connected and self._cli is not None:
            self._log.info("%s  graceful shutdown: sending STOP", self._addr)
            self._stop_ack.clear()
            with contextlib.suppress(Exception):
                await _write_cmd(self._cli, b"STOP")
            try:
                await asyncio.wait_for(self._stop_ack.wait(), timeout=1.5)
                self._log.info("%s  STOP acked — watch quiesced before disconnect", self._addr)
            except asyncio.TimeoutError:
                self._log.warning("%s  STOP not acked before shutdown timeout — disconnecting anyway", self._addr)
        self._disconnect_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
            self._task = None

    async def _loop(self):
        while not self._stop.is_set():
            self.connected = False
            self._disconnect_event.clear()
            self._session_frames = 0

            try:
                ok = await self._connect_and_stream()
                if ok:
                    self._failures = 0
                else:
                    self._failures += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error("%s  unexpected error: %s", self._addr, e)
                self._failures += 1

            self.connected = False
            if self._stop.is_set():
                break

            delay = min(RECONNECT_MIN * (2 ** min(self._failures, 5)), RECONNECT_MAX)
            self._log.info("%s  reconnecting in %.0fs  failures=%d", self._addr, delay, self._failures)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

            fresh = await self._rediscover()
            if fresh:
                self.address = fresh.address
                self.name = _dev_name(fresh, None)
                self._log.debug("%s  re-discovered", self._addr)
            else:
                self._log.warning("%s  not found during re-scan", self._addr)
                self._failures += 1

    async def _rediscover(self) -> Optional[object]:
        found = None
        def cb(dev, ad):
            nonlocal found
            name = _dev_name(dev, ad)
            try:
                uuids = {u.lower() for u in (ad.service_uuids or [])}
            except Exception:
                uuids = set()
            if dev.address == self.address:
                found = dev
            elif (SVC_UUID in uuids or DEVICE_NAME_SUBSTR in name) and self.name and self.name in name:
                found = dev

        try:
            scanner = BleakScanner(cb)
            await scanner.start()
            await asyncio.sleep(4.0)
            await scanner.stop()
        except Exception as e:
            self._log.warning("%s  re-scan failed: %s", self._addr, e)
        return found

    async def _connect_and_stream(self) -> bool:
        self._log.info("%s  connecting  name=%s", self._addr, self.name)
        self._disconnect_event.clear()
        self._last_data = time.monotonic()
        self._last_frame_data = time.monotonic()

        def on_disconnect(_cli):
            self._log.info("%s  BLE disconnected (callback)  session_frames=%d",
                           self._addr, self._session_frames)
            self.connected = False
            self._disconnect_event.set()

        await self._handshake_lock.acquire()
        self._handshake_lock_held = True
        try:
            try:
                cli = BleakClient(
                    self.address,
                    timeout=CONNECT_TIMEOUT,
                    disconnected_callback=on_disconnect,
                )
                await cli.connect()
            except (BleakError, asyncio.TimeoutError, OSError) as e:
                self._log.warning("%s  connect failed: %s", self._addr, e)
                return False

            self._cli = cli
            try:
                return await self._run_session(cli)
            finally:
                self._cli = None
                with contextlib.suppress(Exception):
                    await cli.disconnect()
        finally:
            # _run_session releases the lock itself right after the
            # handshake completes, so streaming doesn't hold up other
            # devices' connects; this only catches early-return paths
            # (connect failure, missing characteristics) that never got
            # that far. Guarded by our own flag, not the semaphore's shared
            # state — another session may already have acquired it by now.
            self._release_handshake_lock()

    def _release_handshake_lock(self):
        if self._handshake_lock_held:
            self._handshake_lock_held = False
            self._handshake_lock.release()

    async def _run_session(self, cli: BleakClient) -> bool:
        svcs = cli.services
        want = {TX_UUID.lower(), RX_UUID.lower()}
        have = {ch.uuid.lower() for ch in svcs.characteristics.values()}
        if not want.issubset(have):
            self._log.warning("%s  missing TX/RX characteristics", self._addr)
            return False

        notify_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)

        def on_notify(_ch, data: bytes):
            self._last_data = time.monotonic()
            try:
                notify_q.put_nowait(data)
            except asyncio.QueueFull:
                pass

        await cli.start_notify(TX_UUID, on_notify)

        # Force a clean slate before handshaking: if the watch was already
        # running (e.g. the bridge was restarted while it kept streaming from
        # an earlier session), it can be too busy pushing notifications to
        # process START/TIME promptly, which causes missed ACKs and corrupts
        # in-flight frames. STOP quiesces it regardless of prior state.
        #
        # Retried like TIME/START: a single attempt can itself get lost in
        # the same congestion it's meant to relieve — an already-streaming
        # watch may not service this write for a while, so one shot often
        # isn't enough to actually break the cycle. Once STOP truly lands,
        # the watch falls quiet and the rest of the handshake gets through
        # far more reliably.
        await asyncio.sleep(0.15)
        stopped = False
        for attempt in range(5):
            await _write_cmd(cli, b"STOP")
            ack = await self._wait_text(notify_q, "ACK:STOP", 1.5)
            if ack:
                stopped = True
                break
            self._log.debug("%s  STOP attempt %d/5: no ack", self._addr, attempt + 1)
        if not stopped:
            self._log.warning("%s  STOP never acked — proceeding anyway", self._addr)

        # Handshake: TIME sync — retried like START. A silently-dropped TIME
        # write used to leave the watch on whatever offset it had before
        # (from an earlier session, or none at all), producing frames whose
        # timestamps are consistently off by however long ago that offset
        # was actually set, even though the watch is streaming live right
        # now. Each attempt re-sends the current time, not a stale value.
        await asyncio.sleep(0.15)
        time_synced = False
        for attempt in range(3):
            await _write_cmd(cli, f"TIME:{int(time.time())}".encode())
            ack = await self._wait_text(notify_q, "ACK:TIME", 1.5)
            if ack:
                time_synced = True
                break
            self._log.debug("%s  TIME sync attempt %d/3: no ack", self._addr, attempt + 1)
        self._time_synced = time_synced
        if not time_synced:
            self._log.warning(
                "%s  TIME sync failed after 3 attempts — stamping frames with bridge time instead of device clock",
                self._addr)

        # Handshake: START
        started = False
        for attempt in range(3):
            await _write_cmd(cli, b"START")
            ack = await self._wait_text(notify_q, "ACK:START", 1.5)
            if ack:
                started = True
                break
            self._log.debug("%s  START attempt %d/3: no ack", self._addr, attempt + 1)

        if not started:
            self._log.warning("%s  no ACK:START — listening anyway", self._addr)

        # Handshake is done — let the next queued-up device start connecting
        # instead of waiting for this one's whole streaming session.
        self._release_handshake_lock()

        self.connected = True
        self._failures = 0
        self._log.info("%s  streaming  ws_clients=%d", self._addr, hub.json_client_count)

        processor = asyncio.create_task(self._process_notifications(notify_q))
        pinger = asyncio.create_task(self._ping_loop(cli))
        stats_task = asyncio.create_task(self._stats_loop())

        try:
            await self._disconnect_event.wait()
        finally:
            for t in (processor, pinger, stats_task):
                t.cancel()
                with contextlib.suppress(Exception):
                    await t

        self._log.info("%s  session ended  session_frames=%d  %s",
                       self._addr, self._session_frames, self.asm.stats_line())
        return True

    async def _process_notifications(self, q: asyncio.Queue):
        while True:
            data = await q.get()
            try:
                if len(data) < FRAG_HDR_SIZE:
                    if len(data) <= 64 and all(32 <= b < 127 for b in data):
                        text = data.decode("utf-8", "ignore")
                        self._log.debug("%s  txt: %s", self._addr, text)
                        if "ACK:STOP" in text:
                            self._stop_ack.set()
                    continue

                # Only genuine binary fragments count towards "the IMU stream
                # is alive" — text replies (PONG/ACK/STATE) also arrive on
                # this same notify channel and would otherwise mask a dead
                # stream: the ping watchdog's own PING/PONG keep-alive
                # traffic refreshes _last_data even after the watch stops
                # producing windows, so it never trips on that alone.
                self._last_frame_data = time.monotonic()

                frame = await self.asm.feed(self.address, data)
                if frame is None:
                    continue

                try:
                    obj = frame_to_json(frame)
                    if not self._time_synced:
                        # Device clock is unconfirmed this connection — the
                        # DB only buckets by whole second anyway, so the
                        # bridge's own receipt time is both trustworthy and
                        # precise enough, unlike whatever stale offset the
                        # watch might still be carrying.
                        obj["meta"]["ts0_ns"] = time.time_ns()
                    await hub.broadcast_json(obj)
                    self._session_frames += 1
                except ValueError as e:
                    self.asm.frames_crc_err += 1
                    self._log.warning("%s  frame decode error: %s", self._addr, e)
                    await hub.broadcast_bin(frame)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error("%s  notify handler error: %s", self._addr, e)

    async def _ping_loop(self, cli: BleakClient):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            silence = time.monotonic() - self._last_data

            if silence > NOTIFY_TIMEOUT:
                self._log.warning("%s  no data for %.1fs — forcing disconnect", self._addr, silence)
                self._disconnect_event.set()
                return

            frame_silence = time.monotonic() - self._last_frame_data
            if frame_silence > FRAME_STALL_TIMEOUT:
                self._log.warning(
                    "%s  no IMU frames for %.1fs (link alive, PING/PONG still answering) — forcing disconnect",
                    self._addr, frame_silence)
                self._disconnect_event.set()
                return

            if silence > PING_INTERVAL:
                self._log.debug("%s  silence %.1fs — PING", self._addr, silence)
                ok = await _write_cmd(cli, b"PING")
                if not ok:
                    self._log.warning("%s  PING write failed — forcing disconnect", self._addr)
                    self._disconnect_event.set()
                    return

    async def _stats_loop(self):
        """Periodic summary so you can see the bridge is alive without flooding."""
        interval = 30.0
        while True:
            await asyncio.sleep(interval)
            self._log.info("%s  alive  session_frames=%d  %s",
                           self._addr, self._session_frames, self.asm.stats_line())

    @staticmethod
    async def _wait_text(q: asyncio.Queue, pattern: str, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                data = await asyncio.wait_for(q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if len(data) <= 64 and all(32 <= b < 127 for b in data):
                text = data.decode("utf-8", "ignore")
                if pattern in text:
                    return text

# ─── Bridge (scan + manage sessions) ────────────────────────────────

_bridge_log = _log("scan")

class Bridge:
    def __init__(self):
        self.asm = Assembler()
        self.sessions: Dict[str, DeviceSession] = {}
        self.handshake_lock = asyncio.Semaphore(1)

    async def run(self):
        asyncio.create_task(self.asm.cleanup_loop())

        retry_delay = 2.0
        while True:
            self._cleanup_sessions()

            active = sum(1 for s in self.sessions.values() if s.connected)
            total = len(self.sessions)
            free_slots = MAX_DEVICES - total

            if free_slots <= 0:
                _bridge_log.debug("all slots occupied (%d/%d connected)", active, total)
                await asyncio.sleep(SCAN_PERIOD)
                continue

            try:
                candidates = await self._scan()
                retry_delay = 2.0
            except (OSError, FileNotFoundError) as e:
                _bridge_log.error("bluetooth not available: %s  (retry in %.0fs)", e, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)
                continue
            except Exception as e:
                _bridge_log.error("scan error: %s", e)
                await asyncio.sleep(5.0)
                continue

            if not candidates:
                _bridge_log.debug("no devices found")
                await asyncio.sleep(SCAN_PERIOD)
                continue

            for dev, ad in candidates:
                if len(self.sessions) >= MAX_DEVICES:
                    break
                addr = dev.address
                if addr in self.sessions:
                    continue
                name = _dev_name(dev, ad)
                session = DeviceSession(addr, name, self.asm, self.handshake_lock)
                self.sessions[addr] = session
                session.start()

            await asyncio.sleep(SCAN_PERIOD)

    async def _scan(self) -> list:
        _bridge_log.info("scanning for %s devices…  slots=%d/%d",
                         DEVICE_NAME_SUBSTR, len(self.sessions), MAX_DEVICES)
        seen = {}

        def cb(dev, ad):
            try:
                uuids = {u.lower() for u in (ad.service_uuids or [])}
            except Exception:
                uuids = set()
            name = _dev_name(dev, ad)
            if SVC_UUID in uuids or DEVICE_NAME_SUBSTR in name:
                seen[dev.address] = (dev, ad)

        scanner = BleakScanner(cb)
        await scanner.start()
        await asyncio.sleep(SCAN_DURATION)
        await scanner.stop()

        for addr, (d, ad) in seen.items():
            rssi = getattr(ad, "rssi", None)
            _bridge_log.info("found %s  name=%s  RSSI=%s", addr[-8:], _dev_name(d, ad), rssi)
        if not seen:
            _bridge_log.debug("scan complete — 0 candidates")
        return list(seen.values())

    def _cleanup_sessions(self):
        dead = [addr for addr, s in self.sessions.items()
                if s._task is not None and s._task.done()]
        for addr in dead:
            del self.sessions[addr]
            _bridge_log.debug("cleaned up session %s", addr[-8:])

    async def shutdown(self):
        for s in self.sessions.values():
            await s.stop()
        self.sessions.clear()

# ─── Main ────────────────────────────────────────────────────────────

async def main():
    main_log = _log("main")
    main_log.info("starting  ws=%s:%d  max_devices=%d  log_level=%s",
                  WS_HOST, WS_PORT, MAX_DEVICES, LOG_LEVEL)
    bridge = Bridge()
    async with serve(ws_router, WS_HOST, WS_PORT, max_size=None):
        try:
            await bridge.run()
        finally:
            await bridge.shutdown()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(main())
    # Cancel main() itself rather than calling loop.stop() — stopping the
    # loop directly leaves run_until_complete's future unfinished, so
    # main()'s `finally: await bridge.shutdown()` (which sends STOP to each
    # watch) never runs and every device is left mid-stream for the next run.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, main_task.cancel)
    try:
        loop.run_until_complete(main_task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
