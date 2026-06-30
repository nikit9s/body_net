"""bleak BLE scanner adapter implementing the :class:`DeviceScanner` port."""

from __future__ import annotations

import asyncio
from typing import List, Tuple

from bleak import BleakScanner

from ..domain.protocol import DEVICE_NAME_SUBSTR, SCAN_DURATION, SVC_UUID
from ..logging_setup import get_logger

_bridge_log = get_logger("scan")


def _dev_name(dev, ad) -> str:
    """Best-effort device name from advertisement or device record."""
    return getattr(ad, "local_name", None) or getattr(dev, "name", None) or ""


class BleakDeviceScanner:
    """Discovers T-Watch-IMU devices via :class:`bleak.BleakScanner`.

    Implements the :class:`~ble_bridge.ports.DeviceScanner` port.
    """

    def __init__(self, max_devices: int) -> None:
        self._max_devices = max_devices
        self._session_count = 0

    def set_session_count(self, count: int) -> None:
        """Update the active-session count used only for the scan log line."""
        self._session_count = count

    async def scan(self) -> List[Tuple[object, object]]:
        """Scan for matching devices, returning ``(device, advertisement)`` tuples."""
        _bridge_log.info("scanning for %s devices…  slots=%d/%d",
                         DEVICE_NAME_SUBSTR, self._session_count, self._max_devices)
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
