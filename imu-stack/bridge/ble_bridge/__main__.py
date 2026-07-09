"""Composition root for the BLE → WebSocket bridge.

Wires together the concrete Hub, Assembler, and BleakDeviceScanner, injects
them into the Bridge service, starts the WebSocket server, and handles
SIGINT/SIGTERM shutdown. This is the only place where concrete adapters are
constructed.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from .adapters.ble_scanner import BleakDeviceScanner
from .adapters.device_session import BleDeviceSessionFactory
from .adapters.ws_hub import Hub, serve_ws
from .config import Settings
from .domain.reassembler import Assembler
from .application.bridge_service import Bridge
from .logging_setup import get_logger, setup_logging


async def main(settings: Settings) -> None:
    """Build the object graph and run the bridge under a WS server."""
    main_log = get_logger("main")
    main_log.info("starting  ws=%s:%d  max_devices=%d  log_level=%s",
                  settings.ws_host, settings.ws_port, settings.max_devices, settings.log_level)

    hub = Hub()
    asm = Assembler()
    scanner = BleakDeviceScanner(settings.max_devices)
    session_factory = BleDeviceSessionFactory(asm=asm, sink=hub)
    bridge = Bridge(
        scanner=scanner,
        sink=hub,
        session_factory=session_factory,
        asm=asm,
        max_devices=settings.max_devices,
    )

    async with serve_ws(hub, settings.ws_host, settings.ws_port):
        try:
            await bridge.run()
        finally:
            await bridge.shutdown()


def run() -> None:
    """Process entry point: configure logging, set up the loop, run ``main``."""
    settings = Settings.from_env()
    setup_logging(settings.log_level)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(main(settings))
    finally:
        loop.close()


if __name__ == "__main__":
    run()
