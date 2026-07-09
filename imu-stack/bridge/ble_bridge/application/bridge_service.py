"""Bridge orchestration: scan loop and session lifecycle management.

Depends on injected :class:`~ble_bridge.ports.DeviceScanner`,
:class:`~ble_bridge.ports.DeviceSessionFactory`, and
:class:`~ble_bridge.ports.FrameSink`. Holds no module globals and never
references an infrastructure library (``bleak``) — sessions are produced
through the injected factory port.
"""

from __future__ import annotations

import asyncio
from typing import Dict

from ..domain.protocol import SCAN_PERIOD
from ..domain.reassembler import Assembler
from ..logging_setup import get_logger
from ..ports import DeviceScanner, DeviceSessionFactory, DeviceSessionHandle, FrameSink

_bridge_log = get_logger("scan")


def _dev_name(dev, ad) -> str:
    """Best-effort device name from advertisement or device record."""
    return getattr(ad, "local_name", None) or getattr(dev, "name", None) or ""


class Bridge:
    """Scans for devices and manages up to ``max_devices`` sessions."""

    def __init__(
        self,
        scanner: DeviceScanner,
        sink: FrameSink,
        session_factory: DeviceSessionFactory,
        asm: Assembler,
        max_devices: int,
    ) -> None:
        self.scanner = scanner
        self.sink = sink
        self.session_factory = session_factory
        self.asm = asm
        self.max_devices = max_devices
        self.sessions: Dict[str, DeviceSessionHandle] = {}

    async def run(self) -> None:
        """Run the scan/manage loop forever."""
        asyncio.create_task(self.asm.cleanup_loop())

        retry_delay = 2.0
        while True:
            self._cleanup_sessions()

            active = sum(1 for s in self.sessions.values() if s.connected)
            total = len(self.sessions)
            free_slots = self.max_devices - total

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
                if len(self.sessions) >= self.max_devices:
                    break
                addr = dev.address
                if addr in self.sessions:
                    continue
                name = _dev_name(dev, ad)
                session = self.session_factory.create(addr, name)
                self.sessions[addr] = session
                session.start()

            await asyncio.sleep(SCAN_PERIOD)

    async def _scan(self) -> list:
        # Keep the scan log line's slot count consistent with prior behavior.
        set_count = getattr(self.scanner, "set_session_count", None)
        if callable(set_count):
            set_count(len(self.sessions))
        return await self.scanner.scan()

    def _cleanup_sessions(self) -> None:
        dead = [addr for addr, s in self.sessions.items() if s.done]
        for addr in dead:
            del self.sessions[addr]
            _bridge_log.debug("cleaned up session %s", addr[-8:])

    async def shutdown(self) -> None:
        """Stop all sessions and clear the registry."""
        for s in self.sessions.values():
            await s.stop()
        self.sessions.clear()
