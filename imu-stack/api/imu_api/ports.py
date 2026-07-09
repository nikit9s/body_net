"""Ports (interfaces) for the application layer.

Defines the :class:`ImuReadRepository` protocol — the boundary the use-case
layer depends on. Concrete adapters (e.g. the TimescaleDB repository) implement
it. This module depends only on the standard library and the domain layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

from .domain.models import AggBucket, ExportRow, Feature, LatestWindow


@runtime_checkable
class ImuReadRepository(Protocol):
    """Read-only repository for IMU telemetry.

    One async method per query exposed by the API. Implementations own all
    storage-specific concerns (SQL, fallbacks, connection management).
    """

    async def list_devices(self) -> List[int]:
        """Return distinct ``dev_id`` values seen in the last 24 hours, ordered."""
        ...

    async def agg(
        self,
        resolution: str,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return aggregate buckets for a device within a range.

        ``resolution`` is one of ``10s``, ``1m``, ``5m``, ``10m``, ``15m``,
        ``30m``. Callers must validate ``resolution`` before calling.
        """
        ...

    async def export_rows(
        self,
        resolution: str,
        since: datetime,
        until: datetime,
        dev_ids: Optional[List[int]],
    ) -> List[ExportRow]:
        """Return per-device RMS/peak rows for the xlsx export.

        ``resolution`` is one of ``1s``, ``10s``, ``1m``, ``5m``, ``10m``,
        ``15m``, ``30m``. When ``dev_ids`` is ``None``, all devices with data
        in range are included.
        """
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
