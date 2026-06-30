"""Single-device session: connect → handshake → stream → reconnect.

The session owns its ``bleak`` client (its transport detail) but talks to the
outside world only through the injected :class:`~ble_bridge.ports.FrameSink`
and :class:`~ble_bridge.domain.reassembler.Assembler`. It never references a
module-global hub.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Optional

from bleak import BleakClient, BleakError, BleakScanner

from ..domain.protocol import (
    CONNECT_TIMEOUT,
    DEVICE_NAME_SUBSTR,
    FRAG_HDR_SIZE,
    NOTIFY_TIMEOUT,
    PING_INTERVAL,
    RECONNECT_MAX,
    RECONNECT_MIN,
    RX_UUID,
    SVC_UUID,
    TX_UUID,
    frame_to_json,
)
from ..domain.reassembler import Assembler
from ..logging_setup import get_logger
from ..ports import DeviceSessionHandle, FrameSink


def _dev_name(dev, ad) -> str:
    """Best-effort device name from advertisement or device record."""
    return getattr(ad, "local_name", None) or getattr(dev, "name", None) or ""


async def _write_cmd(cli: BleakClient, data: bytes) -> bool:
    """Write a command to the RX characteristic, trying with/without response."""
    for response in (True, False):
        try:
            await cli.write_gatt_char(RX_UUID, data, response=response)
            return True
        except Exception:
            continue
    return False


class DeviceSession:
    """Manages a single T-Watch: connect → handshake → stream → reconnect."""

    def __init__(self, address: str, name: str, asm: Assembler, sink: FrameSink) -> None:
        self.address = address
        self.name = name
        self.asm = asm
        self.sink = sink
        self.connected = False
        self._log = get_logger("dev")
        self._failures = 0
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._disconnect_event = asyncio.Event()
        self._last_data = 0.0
        self._session_frames = 0

    @property
    def _addr(self) -> str:
        return self.address[-8:]

    def start(self) -> None:
        """Start the session's connect/reconnect loop task."""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
            self._log.info("%s  session started  name=%s", self._addr, self.name)

    async def stop(self) -> None:
        """Signal stop and cancel the session task."""
        self._stop.set()
        self._disconnect_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
            self._task = None

    @property
    def done(self) -> bool:
        """Whether the underlying session task has finished."""
        return self._task is not None and self._task.done()

    @property
    def is_finished(self) -> bool:
        """Alias of :attr:`done` for the :class:`DeviceSessionHandle` port."""
        return self.done

    async def _loop(self) -> None:
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

        def on_disconnect(_cli):
            self._log.info("%s  BLE disconnected (callback)  session_frames=%d",
                           self._addr, self._session_frames)
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
            self._log.warning("%s  connect failed: %s", self._addr, e)
            return False

        try:
            return await self._run_session(cli)
        finally:
            with contextlib.suppress(Exception):
                await cli.disconnect()

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

        # Handshake: TIME sync
        await asyncio.sleep(0.15)
        await _write_cmd(cli, f"TIME:{int(time.time())}".encode())
        ack = await self._wait_text(notify_q, "ACK:TIME", 2.0)
        self._log.debug("%s  TIME sync: %s", self._addr, "ok" if ack else "no ack")

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

        self.connected = True
        self._failures = 0
        self._log.info("%s  streaming  ws_clients=%d", self._addr, self.sink.json_client_count)

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

    async def _process_notifications(self, q: asyncio.Queue) -> None:
        while True:
            data = await q.get()
            try:
                if len(data) < FRAG_HDR_SIZE:
                    if len(data) <= 64 and all(32 <= b < 127 for b in data):
                        self._log.debug("%s  txt: %s", self._addr, data.decode("utf-8", "ignore"))
                    continue

                frame = await self.asm.feed(data)
                if frame is None:
                    continue

                try:
                    obj = frame_to_json(frame)
                    await self.sink.broadcast_json(obj)
                    self._session_frames += 1
                except ValueError as e:
                    self.asm.frames_crc_err += 1
                    self._log.warning("%s  frame decode error: %s", self._addr, e)
                    await self.sink.broadcast_bin(frame)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error("%s  notify handler error: %s", self._addr, e)

    async def _ping_loop(self, cli: BleakClient) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            silence = time.monotonic() - self._last_data

            if silence > NOTIFY_TIMEOUT:
                self._log.warning("%s  no data for %.1fs — forcing disconnect", self._addr, silence)
                self._disconnect_event.set()
                return

            if silence > PING_INTERVAL:
                self._log.debug("%s  silence %.1fs — PING", self._addr, silence)
                ok = await _write_cmd(cli, b"PING")
                if not ok:
                    self._log.warning("%s  PING write failed — forcing disconnect", self._addr)
                    self._disconnect_event.set()
                    return

    async def _stats_loop(self) -> None:
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


class BleDeviceSessionFactory:
    """Creates :class:`DeviceSession` instances wired with shared dependencies.

    Implements the :class:`~ble_bridge.ports.DeviceSessionFactory` port. Closes
    over the injected :class:`~ble_bridge.ports.FrameSink` and
    :class:`~ble_bridge.domain.reassembler.Assembler` so the orchestrating
    bridge never needs to know about ``bleak`` or how a session is built.
    """

    def __init__(self, asm: Assembler, sink: FrameSink) -> None:
        self.asm = asm
        self.sink = sink

    def create(self, address: str, name: str) -> DeviceSessionHandle:
        """Make a new BLE session for ``address`` named ``name``."""
        return DeviceSession(address, name, self.asm, self.sink)
