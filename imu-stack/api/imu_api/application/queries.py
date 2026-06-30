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

from ..domain.models import AggBucket, Feature, LatestWindow
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

    async def agg_10s(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return 10-second aggregate buckets for a device within a range."""
        return await self._repo.agg_10s(dev_id, since, until)

    async def agg_1m(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return 1-minute aggregate buckets for a device within a range."""
        return await self._repo.agg_1m(dev_id, since, until)

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
