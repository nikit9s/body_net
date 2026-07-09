"""TimescaleDB read repository.

Implements :class:`~imu_api.ports.ImuReadRepository` over an asyncpg connection
pool. This module owns ALL SQL verbatim, including the
``UndefinedTableError`` fallback from continuous aggregate tables to live
``time_bucket`` queries. It depends on asyncpg, the ports protocol, and the
domain models only.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import asyncpg
from asyncpg.exceptions import UndefinedTableError

from ..domain.models import AggBucket, Feature, LatestWindow


class TimescaleReadRepository:
    """asyncpg-backed implementation of the IMU read repository port."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Store the connection pool used for all queries.

        Args:
            pool: An open asyncpg connection pool.
        """
        self._pool = pool

    async def list_devices(self) -> List[int]:
        """Return all known dev_ids that have sent data in the last 24 hours."""
        q = """
      SELECT DISTINCT dev_id
      FROM imu_agg_1s
      WHERE ts > now() - INTERVAL '24 hours'
      ORDER BY dev_id
    """
        async with self._pool.acquire() as con:
            rows = await con.fetch(q)
        return [r["dev_id"] for r in rows]

    async def agg_10s(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return 10-second buckets, falling back to a live query if needed."""
        q_cagg = """
      SELECT bucket AS ts, a_rms_mg_max, a_peak_mg_max, steps
      FROM cagg_imu_agg_10s
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR bucket >= $2)
                     AND ($3::timestamptz IS NULL OR bucket <= $3)
      ORDER BY bucket
    """
        q_live = """
      SELECT time_bucket(INTERVAL '10 seconds', ts) AS ts,
             MAX(a_rms_mg)  AS a_rms_mg_max,
             MAX(a_peak_mg) AS a_peak_mg_max,
             SUM(steps)     AS steps
      FROM imu_agg_1s
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR ts >= $2)
                     AND ($3::timestamptz IS NULL OR ts <= $3)
      GROUP BY ts
      ORDER BY ts
    """
        async with self._pool.acquire() as con:
            try:
                rows = await con.fetch(q_cagg, dev_id, since, until)
            except UndefinedTableError:
                rows = await con.fetch(q_live, dev_id, since, until)
        return [
            AggBucket(
                ts=r["ts"],
                a_rms_mg_max=r["a_rms_mg_max"],
                a_peak_mg_max=r["a_peak_mg_max"],
                steps=r["steps"],
            )
            for r in rows
        ]

    async def agg_1m(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return 1-minute buckets, falling back to a live query if needed."""
        q_cagg = """
      SELECT bucket AS ts, a_rms_mg_max, a_peak_mg_max, steps
      FROM cagg_imu_agg_1m
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR bucket >= $2)
                     AND ($3::timestamptz IS NULL OR bucket <= $3)
      ORDER BY bucket
    """
        q_live = """
      SELECT time_bucket(INTERVAL '1 minute', ts) AS ts,
             MAX(a_rms_mg)  AS a_rms_mg_max,
             MAX(a_peak_mg) AS a_peak_mg_max,
             SUM(steps)     AS steps
      FROM imu_agg_1s
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR ts >= $2)
                     AND ($3::timestamptz IS NULL OR ts <= $3)
      GROUP BY ts
      ORDER BY ts
    """
        async with self._pool.acquire() as con:
            try:
                rows = await con.fetch(q_cagg, dev_id, since, until)
            except UndefinedTableError:
                rows = await con.fetch(q_live, dev_id, since, until)
        return [
            AggBucket(
                ts=r["ts"],
                a_rms_mg_max=r["a_rms_mg_max"],
                a_peak_mg_max=r["a_peak_mg_max"],
                steps=r["steps"],
            )
            for r in rows
        ]

    async def features(
        self,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
        limit: int,
    ) -> List[Feature]:
        """Return feature rows for a device, newest first, capped at ``limit``."""
        q = """
      SELECT ts0, a_rms_mg, a_peak_mg, step_count, fall_flag
      FROM imu_features
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR ts0 >= $2)
                     AND ($3::timestamptz IS NULL OR ts0 <= $3)
      ORDER BY ts0 DESC
      LIMIT $4
    """
        async with self._pool.acquire() as con:
            rows = await con.fetch(q, dev_id, since, until, limit)
        return [
            Feature(
                ts0=r["ts0"],
                a_rms_mg=r["a_rms_mg"],
                a_peak_mg=r["a_peak_mg"],
                step_count=r["step_count"],
                fall_flag=r["fall_flag"],
            )
            for r in rows
        ]

    async def latest_window(self, dev_id: int) -> Optional[LatestWindow]:
        """Return the most recent window for a device, or ``None`` if absent."""
        q = """
      SELECT seq, ts0, fs_hz, n
      FROM imu_windows
      WHERE dev_id=$1
      ORDER BY ts0 DESC, seq DESC
      LIMIT 1
    """
        async with self._pool.acquire() as con:
            row = await con.fetchrow(q, dev_id)
        if not row:
            return None
        return LatestWindow(
            seq=row["seq"],
            ts0=row["ts0"],
            fs_hz=row["fs_hz"],
            n=row["n"],
        )
