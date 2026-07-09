"""Use-case layer.

:class:`ImuQueryService` is a thin orchestration seam over the
:class:`~imu_api.ports.ImuReadRepository` port. The queries are currently pure
pass-throughs, but the layer is kept present so future business rules
(validation, authorization, derived metrics) have a natural home. It imports
only the domain and ports layers — no asyncpg, no FastAPI.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from ..domain.models import AggBucket, ExportRow, Feature, LatestWindow
from ..ports import ImuReadRepository


class ImuQueryService:
    """Application service exposing read use cases for IMU telemetry."""

    def __init__(self, repository: ImuReadRepository) -> None:
        """Store the read repository this service delegates to.

        Args:
            repository: Concrete implementation of the read port.
        """
        self._repo = repository

    async def list_devices(self) -> List[int]:
        """Return distinct device ids active in the last 24 hours, ordered."""
        return await self._repo.list_devices()

    async def agg(
        self,
        resolution: str,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return aggregate buckets for a device within a range, at the given resolution."""
        return await self._repo.agg(resolution, dev_id, since, until)

    async def export_rows(
        self,
        resolution: str,
        since: datetime,
        until: datetime,
        dev_ids: Optional[List[int]],
    ) -> List[ExportRow]:
        """Return per-device RMS/peak rows for the xlsx export."""
        return await self._repo.export_rows(resolution, since, until, dev_ids)

    async def features(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
        limit: int,
    ) -> List[Feature]:
        """Return feature rows for a device, newest first, capped at ``limit``."""
        return await self._repo.features(dev_id, since, until, limit)

    async def latest_window(self, dev_id: int) -> Optional[LatestWindow]:
        """Return the most recent window for a device, or ``None`` if absent."""
        return await self._repo.latest_window(dev_id)
