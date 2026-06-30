"""Port interfaces decoupling the application layer from concrete IO.

These :class:`typing.Protocol` definitions let sessions and the bridge depend
on abstractions instead of the ``websockets`` Hub or the ``bleak`` scanner.
"""

from __future__ import annotations

from typing import List, Protocol, Tuple, runtime_checkable


@runtime_checkable
class FrameSink(Protocol):
    """Sink that broadcasts decoded frames to connected clients."""

    async def broadcast_json(self, obj: dict) -> None:
        """Broadcast a JSON-serializable object to JSON clients."""
        ...

    async def broadcast_bin(self, data: bytes) -> None:
        """Broadcast raw binary data to binary clients."""
        ...

    @property
    def json_client_count(self) -> int:
        """Number of currently connected JSON clients."""
        ...


@runtime_checkable
class DeviceScanner(Protocol):
    """Discovers candidate BLE devices."""

    async def scan(self) -> List[Tuple[object, object]]:
        """Scan and return a list of ``(device, advertisement)`` tuples."""
        ...


@runtime_checkable
class DeviceSessionHandle(Protocol):
    """A running per-device session, as seen by the orchestrating bridge.

    Exposes only what the bridge needs to manage lifecycle: start the
    connect/reconnect loop, request a stop, observe connectivity, and detect
    when the session's underlying task has finished (for GC).
    """

    @property
    def connected(self) -> bool:
        """Whether the device is currently connected and streaming."""
        ...

    @property
    def done(self) -> bool:
        """Whether the underlying session task has finished."""
        ...

    def start(self) -> None:
        """Start the session's connect/reconnect loop."""
        ...

    async def stop(self) -> None:
        """Signal stop and cancel the session task."""
        ...


@runtime_checkable
class DeviceSessionFactory(Protocol):
    """Creates :class:`DeviceSessionHandle` instances for discovered devices."""

    def create(self, address: str, name: str) -> DeviceSessionHandle:
        """Make a new session for the device at ``address`` named ``name``."""
        ...
