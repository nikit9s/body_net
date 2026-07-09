"""Ports (interfaces) for the application layer.

Defines the :class:`ImuReadRepository` protocol — the boundary the use-case
layer depends on. Concrete adapters (e.g. the TimescaleDB repository) implement
it. This module depends only on the standard library and the domain layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

from .domain.models import AggBucket, Feature, LatestWindow


@runtime_checkable
class ImuReadRepository(Protocol):
    """Read-only repository for IMU telemetry.

    One async method per query exposed by the API. Implementations own all
    storage-specific concerns (SQL, fallbacks, connection management).
    """

    async def list_devices(self) -> List[int]:
        """Return distinct ``dev_id`` values seen in the last 24 hours, ordered."""
        ...

    async def agg_10s(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return 10-second aggregate buckets for a device within a range."""
        ...

    async def agg_1m(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return 1-minute aggregate buckets for a device within a range."""
        ...

    async def features(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
        limit: int,
    ) -> List[Feature]:
        """Return feature rows for a device, newest first, capped at ``limit``."""
        ...

    async def latest_window(self, dev_id: int) -> Optional[LatestWindow]:
        """Return the most recent window for a device, or ``None`` if absent."""
        ...
