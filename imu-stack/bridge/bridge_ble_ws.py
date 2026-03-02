#!/usr/bin/env python3
"""BLE → WebSocket bridge for T-Watch IMU devices.

Discovers T-Watch-IMU devices, connects via BLE, reassembles fragmented
binary frames, verifies CRC32, converts to JSON and broadcasts over WS.
Handles up to MAX_DEVICES simultaneously with automatic reconnection.
"""

import asyncio
import contextlib
import json
import os
import signal
import time
import zlib
from typing import Dict, Optional, Set, Tuple

from bleak import BleakClient, BleakError, BleakScanner
from websockets import serve

# ─── Configuration ──────────────────────────────────────────────────
DEVICE_NAME_SUBSTR = "T-Watch-IMU"
SVC_UUID = "6f2d5d52-0f4d-4b2a-a02b-5a7a5b3a0e11"
TX_UUID  = "f5c8b9d0-3a5d-4d9d-9d67-2d7f1b9e4b22"
RX_UUID  = "e8b6a830-8f6b-4d9c-a71c-6d8c2a3a5f33"

WS_HOST     = os.environ.get("WS_HOST", "0.0.0.0")
WS_PORT     = int(os.environ.get("WS_PORT", "8765"))
MAX_DEVICES = int(os.environ.get("MAX_DEVICES", "4"))
VERBOSE     = os.environ.get("VERBOSE", "1") == "1"

# Protocol
MAGIC_FRAG     = 0xB1F1
MAGIC_FRAME    = 0xB10F
FRAME_HDR_SIZE = 32
FRAG_HDR_SIZE  = 10

# Timing
CONNECT_TIMEOUT    = 12.0
NOTIFY_TIMEOUT     = 8.0     # считаем соединение мёртвым если нет данных
PING_INTERVAL      = 4.0     # как часто слать PING при тишине
RECONNECT_MIN      = 1.0
RECONNECT_MAX      = 30.0
SCAN_PERIOD        = 8.0     # как часто сканировать
SCAN_DURATION      = 5.0     # длительность одного скана
REASM_TTL          = 10.0
REASM_CLEAN_PERIOD = 3.0


def log(tag: str, msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"{ts} [{tag}] {msg}")


def vlog(tag: str, msg: str):
    if VERBOSE:
        log(tag, msg)


# ─── CRC32 ──────────────────────────────────────────────────────────
def crc32_ieee(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ─── WebSocket Hub ──────────────────────────────────────────────────
class Hub:
    """Manages WS client subscriptions and broadcasts."""

    def __init__(self):
        self._json: Set = set()
        self._bin: Set = set()
        self._lock = asyncio.Lock()

    async def add(self, ws, kind: str):
        async with self._lock:
            (self._json if kind == "json" else self._bin).add(ws)
            log("WS", f"+{kind} client (total json={len(self._json)} bin={len(self._bin)})")

    async def remove(self, ws):
        async with self._lock:
            self._json.discard(ws)
            self._bin.discard(ws)

    async def broadcast_json(self, obj: dict):
        data = json.dumps(obj)
        async with self._lock:
            targets = list(self._json)
        for ws in targets:
            try:
                await ws.send(data)
            except Exception:
                await self.remove(ws)

    async def broadcast_bin(self, data: bytes):
        async with self._lock:
            targets = list(self._bin)
        for ws in targets:
            try:
                await ws.send(data)
            except Exception:
                await self.remove(ws)


hub = Hub()


# ─── WS routing ────────────────────────────────────────────────────
async def _ws_keepalive(ws):
    try:
        async for _ in ws:
            pass
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
        await ws.close(code=1008, reason="use /ws/json or /ws/bin")


# ─── Frame reassembler ─────────────────────────────────────────────
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
    def __init__(self):
        self._map: Dict[Tuple[int, int], _ReasmEntry] = {}
        self._seq_dev: Dict[int, int] = {}
        self._lock = asyncio.Lock()
        self.frames_ok = 0
        self.frames_err = 0

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(REASM_CLEAN_PERIOD)
            now = time.monotonic()
            async with self._lock:
                stale = [k for k, e in self._map.items() if now - e.touched > REASM_TTL]
                for k in stale:
                    self._seq_dev.pop(k[1], None)
                    del self._map[k]
                if stale:
                    vlog("ASM", f"cleaned {len(stale)} stale entries")

    async def feed(self, frag: bytes) -> Optional[bytes]:
        """Feed a BLE notification. Returns completed frame or None."""
        if len(frag) < FRAG_HDR_SIZE:
            return None

        magic = int.from_bytes(frag[0:2], "little")
        if magic != MAGIC_FRAG:
            return None

        part_idx = frag[3]
        seq      = int.from_bytes(frag[4:8], "little")
        frag_len = int.from_bytes(frag[8:10], "little")
        body     = frag[FRAG_HDR_SIZE:]

        if frag_len != len(body) or frag_len == 0:
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

        parts       = hdr[27]
        payload_len = int.from_bytes(hdr[28:32], "little")
        total       = FRAME_HDR_SIZE + payload_len + 4
        tail        = body[FRAME_HDR_SIZE:]
        dev_id      = int.from_bytes(hdr[4:8], "little")

        async with self._lock:
            entry = _ReasmEntry(total, parts, hdr, tail)
            self._map[(dev_id, seq)] = entry
            self._seq_dev[seq] = dev_id

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
                return None
            entry.buf[entry.got:end] = body
            entry.got = end
            entry.seen.add(idx)
            entry.touched = time.monotonic()
            if entry.got != len(entry.buf):
                return None
        return await self._complete(dev_id, seq)

    async def _complete(self, dev_id: int, seq: int) -> bytes:
        async with self._lock:
            entry = self._map.pop((dev_id, seq), None)
            self._seq_dev.pop(seq, None)
        if entry is None:
            return None
        self.frames_ok += 1
        return bytes(entry.buf)


# ─── Frame → JSON ──────────────────────────────────────────────────
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


# ─── BLE helpers ───────────────────────────────────────────────────
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


# ─── Device Session ────────────────────────────────────────────────
class DeviceSession:
    """Manages a single T-Watch: connect → handshake → stream → reconnect."""

    def __init__(self, address: str, name: str, asm: Assembler):
        self.address = address
        self.name = name
        self.asm = asm
        self.connected = False
        self._failures = 0
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._disconnect_event = asyncio.Event()
        self._last_data = 0.0

    def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            log("DEV", f"Session started for {self.address} ({self.name})")

    async def stop(self):
        self._stop.set()
        self._disconnect_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
            self._task = None

    async def _loop(self):
        """Reconnection loop with exponential backoff."""
        while not self._stop.is_set():
            self.connected = False
            self._disconnect_event.clear()

            try:
                ok = await self._connect_and_stream()
                if ok:
                    self._failures = 0
                else:
                    self._failures += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                log("DEV", f"{self.address} unexpected: {e}")
                self._failures += 1

            self.connected = False

            if self._stop.is_set():
                break

            delay = min(RECONNECT_MIN * (2 ** min(self._failures, 5)), RECONNECT_MAX)
            log("DEV", f"{self.address} reconnect in {delay:.0f}s (failures={self._failures})")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

            # Re-discover the device (addresses can change, especially on macOS)
            fresh = await self._rediscover()
            if fresh:
                self.address = fresh.address
                self.name = _dev_name(fresh, None)
            else:
                log("DEV", f"{self.address} not found during re-scan, will retry")
                self._failures += 1

    async def _rediscover(self) -> Optional[object]:
        """Quick scan to find the device again by address or name."""
        found = None
        def cb(dev, ad):
            nonlocal found
            name = _dev_name(dev, ad)
            try:
                uuids = {u.lower() for u in (ad.service_uuids or [])}
            except Exception:
                uuids = set()
            matches_addr = dev.address == self.address
            matches_svc = SVC_UUID in uuids or DEVICE_NAME_SUBSTR in name
            if matches_addr or (matches_svc and DEVICE_NAME_SUBSTR in name and self.name and self.name in name):
                found = dev

        try:
            scanner = BleakScanner(cb)
            await scanner.start()
            await asyncio.sleep(4.0)
            await scanner.stop()
        except Exception as e:
            log("DEV", f"Re-scan error: {e}")
        return found

    async def _connect_and_stream(self) -> bool:
        log("BLE", f"Connecting → {self.address} ({self.name})")

        self._disconnect_event.clear()
        self._last_data = time.monotonic()

        def on_disconnect(_cli):
            log("BLE", f"{self.address} disconnected (callback)")
            self.connected = False
            self._disconnect_event.set()

        try:
            cli = BleakClient(
                self.address,
                timeout=CONNECT_TIMEOUT,
                disconnected_callback=on_disconnect,
            )
            await cli.connect()
        except (BleakError, asyncio.TimeoutError, OSError) as e:
            log("BLE", f"{self.address} connect failed: {e}")
            return False

        try:
            return await self._run_session(cli)
        finally:
            with contextlib.suppress(Exception):
                await cli.disconnect()

    async def _run_session(self, cli: BleakClient) -> bool:
        # Verify characteristics
        svcs = await cli.get_services()
        want = {TX_UUID.lower(), RX_UUID.lower()}
        have = {ch.uuid.lower() for s in svcs for ch in s.characteristics}
        if not want.issubset(have):
            log("BLE", f"{self.address} missing TX/RX characteristics")
            return False

        # Subscribe to notifications
        notify_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)

        def on_notify(_ch, data: bytes):
            self._last_data = time.monotonic()
            try:
                notify_q.put_nowait(data)
            except asyncio.QueueFull:
                pass

        await cli.start_notify(TX_UUID, on_notify)

        # Handshake: TIME sync
        await asyncio.sleep(0.15)
        await _write_cmd(cli, f"TIME:{int(time.time())}".encode())
        ack = await self._wait_text(notify_q, "ACK:TIME", 2.0)
        if ack:
            vlog("BLE", f"{self.address} time synced")
        else:
            vlog("BLE", f"{self.address} no ACK:TIME, continuing")

        # Handshake: START
        started = False
        for attempt in range(3):
            await _write_cmd(cli, b"START")
            ack = await self._wait_text(notify_q, "ACK:START", 1.5)
            if ack:
                started = True
                break
            vlog("BLE", f"{self.address} START attempt {attempt + 1}/3 no ack")

        if not started:
            log("BLE", f"{self.address} no ACK:START — listening anyway")

        self.connected = True
        self._failures = 0
        log("BLE", f"{self.address} streaming active")

        # Process notifications until disconnect
        processor = asyncio.create_task(self._process_notifications(notify_q))
        pinger = asyncio.create_task(self._ping_loop(cli))

        try:
            await self._disconnect_event.wait()
        finally:
            processor.cancel()
            pinger.cancel()
            with contextlib.suppress(Exception):
                await processor
            with contextlib.suppress(Exception):
                await pinger

        log("BLE", f"{self.address} session ended (frames={self.asm.frames_ok})")
        return True

    async def _process_notifications(self, q: asyncio.Queue):
        """Drains notification queue, feeds assembler, broadcasts frames."""
        while True:
            data = await q.get()
            try:
                if len(data) < FRAG_HDR_SIZE:
                    if len(data) <= 64 and all(32 <= b < 127 for b in data):
                        vlog("TXT", data.decode("utf-8", "ignore"))
                    continue

                frame = await self.asm.feed(data)
                if frame is None:
                    continue

                try:
                    obj = frame_to_json(frame)
                    await hub.broadcast_json(obj)
                except Exception as e:
                    vlog("ASM", f"frame_to_json error: {e}")
                    await hub.broadcast_bin(frame)
            except asyncio.CancelledError:
                break
            except Exception as e:
                vlog("DEV", f"notify processing error: {e}")

    async def _ping_loop(self, cli: BleakClient):
        """Sends periodic PINGs when data stops flowing. Triggers disconnect on timeout."""
        while True:
            await asyncio.sleep(PING_INTERVAL)
            silence = time.monotonic() - self._last_data

            if silence > NOTIFY_TIMEOUT:
                log("BLE", f"{self.address} no data for {silence:.1f}s — forcing disconnect")
                self._disconnect_event.set()
                return

            if silence > PING_INTERVAL:
                vlog("BLE", f"{self.address} silence {silence:.1f}s — sending PING")
                ok = await _write_cmd(cli, b"PING")
                if not ok:
                    log("BLE", f"{self.address} PING write failed — forcing disconnect")
                    self._disconnect_event.set()
                    return

    @staticmethod
    async def _wait_text(q: asyncio.Queue, pattern: str, timeout: float) -> Optional[str]:
        """Drain queue looking for a text notification matching pattern."""
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


# ─── Bridge (scan + manage sessions) ───────────────────────────────
class Bridge:
    def __init__(self):
        self.asm = Assembler()
        self.sessions: Dict[str, DeviceSession] = {}

    async def run(self):
        asyncio.create_task(self.asm.cleanup_loop())

        retry_delay = 2.0
        while True:
            # Clean up finished sessions
            self._cleanup_sessions()

            active = sum(1 for s in self.sessions.values() if s.connected)
            total = len(self.sessions)
            free_slots = MAX_DEVICES - total

            if free_slots <= 0:
                await asyncio.sleep(SCAN_PERIOD)
                continue

            try:
                candidates = await self._scan()
                retry_delay = 2.0
            except (OSError, FileNotFoundError) as e:
                log("BLE", f"Bluetooth not available: {e}")
                log("BLE", f"Retrying in {retry_delay:.0f}s (on macOS — run bridge on host)")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60.0)
                continue
            except Exception as e:
                log("BLE", f"Scan error: {e}")
                await asyncio.sleep(5.0)
                continue

            if not candidates:
                vlog("SCAN", "No devices found")
                await asyncio.sleep(SCAN_PERIOD)
                continue

            for dev, ad in candidates:
                if len(self.sessions) >= MAX_DEVICES:
                    break
                addr = dev.address
                if addr in self.sessions:
                    continue
                name = _dev_name(dev, ad)
                session = DeviceSession(addr, name, self.asm)
                self.sessions[addr] = session
                session.start()
                log("SCAN", f"New device: {addr} ({name})")

            await asyncio.sleep(SCAN_PERIOD)

    async def _scan(self) -> list:
        log("SCAN", "Searching for T-Watch-IMU devices…")
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
            log("SCAN", f"Found {addr} ({_dev_name(d, ad)}) RSSI={rssi}")
        return list(seen.values())

    def _cleanup_sessions(self):
        dead = [addr for addr, s in self.sessions.items()
                if s._task is not None and s._task.done()]
        for addr in dead:
            del self.sessions[addr]
            vlog("SCAN", f"Cleaned up session for {addr}")

    async def shutdown(self):
        for s in self.sessions.values():
            await s.stop()
        self.sessions.clear()


# ─── Main ──────────────────────────────────────────────────────────
async def main():
    log("MAIN", f"WS server: ws://{WS_HOST}:{WS_PORT}/ws/json | /ws/bin")
    bridge = Bridge()
    async with serve(ws_router, WS_HOST, WS_PORT, max_size=None):
        try:
            await bridge.run()
        finally:
            await bridge.shutdown()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
