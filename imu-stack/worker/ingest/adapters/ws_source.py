"""WebSocket-based WindowSource implementation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import websockets

from ..domain.models import ImuWindow
from ..ports import FrameError, WindowItem


class WebSocketWindowSource:
    """WindowSource backed by the BLE-WS bridge.

    Owns the connect/reconnect loop, filters ``type == "imu"`` frames and
    parses each payload into an :class:`ImuWindow`. Yields ``None`` whenever a
    new WS session begins so the consumer can reset per-session counters.
    """

    def __init__(self, ws_url: str, reconnect_s: float, log: logging.Logger) -> None:
        self._ws_url = ws_url
        self._reconnect_s = reconnect_s
        self._log = log

    async def windows(self) -> AsyncIterator[WindowItem]:
        """Yield parsed IMU windows, reconnecting on disconnect."""
        while True:
            self._log.info("connecting to bridge  url=%s", self._ws_url)
            try:
                async with websockets.connect(self._ws_url, max_size=None) as ws:
                    self._log.info("ws connected")
                    yield None  # session-start marker
                    async for msg in ws:
                        try:
                            obj = json.loads(msg)
                            if obj.get("type") != "imu":
                                continue
                            window = ImuWindow.from_payload(obj)
                        except Exception as e:
                            # Per-frame parse error: keep the connection alive and
                            # surface the failure to the consumer for counting.
                            yield FrameError(e)
                            continue
                        yield window
                    self._log.warning("ws stream ended (server closed)")
            except asyncio.CancelledError:
                self._log.info("shutting down")
                break
            except Exception as e:
                self._log.warning(
                    "ws disconnected: %s — reconnecting in %.0fs",
                    e, self._reconnect_s)
                try:
                    await asyncio.sleep(self._reconnect_s)
                except asyncio.CancelledError:
                    self._log.info("shutting down during reconnect wait")
                    break
