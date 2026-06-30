"""WebSocket adapter: the :class:`Hub` (a :class:`~ble_bridge.ports.FrameSink`)
plus connection routing, keepalive, and a helper to start the server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Set

from websockets import serve

from ..logging_setup import get_logger

_ws_log = get_logger("ws")


class Hub:
    """Manages WS client subscriptions and broadcasts.

    Implements the :class:`~ble_bridge.ports.FrameSink` port.
    """

    def __init__(self) -> None:
        self._json: Set = set()
        self._bin: Set = set()
        self._lock = asyncio.Lock()
        self._broadcast_count = 0

    async def add(self, ws, kind: str) -> None:
        """Register a client websocket under the given ``kind`` ('json'/'bin')."""
        async with self._lock:
            (self._json if kind == "json" else self._bin).add(ws)
            _ws_log.info("client connected  type=%s  json=%d  bin=%d",
                         kind, len(self._json), len(self._bin))

    async def remove(self, ws) -> None:
        """Unregister a client websocket from both subscription sets."""
        async with self._lock:
            was_json = ws in self._json
            was_bin = ws in self._bin
            self._json.discard(ws)
            self._bin.discard(ws)
            if was_json or was_bin:
                _ws_log.info("client disconnected  json=%d  bin=%d",
                             len(self._json), len(self._bin))

    async def broadcast_json(self, obj: dict) -> None:
        """Broadcast a JSON-serializable object to all JSON clients."""
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

    async def broadcast_bin(self, data: bytes) -> None:
        """Broadcast raw binary data to all binary clients."""
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
        """Number of currently connected JSON clients."""
        return len(self._json)


async def _ws_keepalive(hub: Hub, ws) -> None:
    """Consume inbound messages until the client disconnects, then deregister."""
    try:
        async for _ in ws:
            pass
    finally:
        await hub.remove(ws)


def make_ws_router(hub: Hub):
    """Build a websockets connection handler bound to ``hub``."""

    async def ws_router(ws) -> None:
        path = getattr(ws, "path", "/").split("?", 1)[0].rstrip("/") or "/"
        if path in ("/ws/json", "/"):
            await hub.add(ws, "json")
            await _ws_keepalive(hub, ws)
        elif path == "/ws/bin":
            await hub.add(ws, "bin")
            await _ws_keepalive(hub, ws)
        else:
            _ws_log.warning("rejected connection to unknown path=%s", path)
            await ws.close(code=1008, reason="use /ws/json or /ws/bin")

    return ws_router


def serve_ws(hub: Hub, host: str, port: int):
    """Return the ``websockets.serve`` async context manager for ``hub``."""
    return serve(make_ws_router(hub), host, port, max_size=None)
