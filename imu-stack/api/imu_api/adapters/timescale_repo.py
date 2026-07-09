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

from ..domain.models import AggBucket, ExportRow, Feature, LatestWindow

# Resolutions backed by a materialized continuous aggregate (fast path).
# Anything else falls back to live bucketing over imu_agg_1s.
_AGG_CONTINUOUS_VIEWS = {
    "10s": "cagg_imu_agg_10s",
    "1m":  "cagg_imu_agg_1m",
}

# Bucket width used for the live fallback / for resolutions with no continuous aggregate.
_AGG_BUCKET_INTERVALS = {
    "10s": "10 seconds",
    "1m":  "1 minute",
    "5m":  "5 minutes",
    "10m": "10 minutes",
    "15m": "15 minutes",
    "30m": "30 minutes",
}

# Table + time column to read from for export resolutions backed directly by a
# table (raw per-second, or a materialized continuous aggregate). Resolutions
# without an entry here (5m/10m/15m/30m) are live-bucketed from imu_agg_1s.
_EXPORT_SOURCES = {
    "1s":  ("imu_agg_1s",       "ts",     "a_rms_mg",     "a_peak_mg"),
    "10s": ("cagg_imu_agg_10s", "bucket", "a_rms_mg_max", "a_peak_mg_max"),
    "1m":  ("cagg_imu_agg_1m",  "bucket", "a_rms_mg_max", "a_peak_mg_max"),
}


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

    async def agg(
        self,
        resolution: str,
        dev_id: int,
        since: Optional[datetime],
        until: Optional[datetime],
    ) -> List[AggBucket]:
        """Return aggregate buckets for a device, falling back to a live query if needed."""
        # GROUP BY/ORDER BY reference the bucket by ordinal (1), not by the
        # "ts" alias: Postgres resolves an ambiguous GROUP BY name in favor of
        # the input column (imu_agg_1s.ts, per-second) over the output alias,
        # which would otherwise group by the raw second instead of the bucket.
        q_live = f"""
      SELECT time_bucket(INTERVAL '{_AGG_BUCKET_INTERVALS[resolution]}', ts) AS ts,
             MAX(a_rms_mg)  AS a_rms_mg_max,
             MAX(a_peak_mg) AS a_peak_mg_max,
             SUM(steps)     AS steps
      FROM imu_agg_1s
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR ts >= $2)
                     AND ($3::timestamptz IS NULL OR ts <= $3)
      GROUP BY 1
      ORDER BY 1
    """
        async with self._pool.acquire() as con:
            view = _AGG_CONTINUOUS_VIEWS.get(resolution)
            if view:
                q_cagg = f"""
      SELECT bucket AS ts, a_rms_mg_max, a_peak_mg_max, steps
      FROM {view}
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR bucket >= $2)
                     AND ($3::timestamptz IS NULL OR bucket <= $3)
      ORDER BY bucket
    """
                try:
                    rows = await con.fetch(q_cagg, dev_id, since, until)
                except UndefinedTableError:
                    rows = await con.fetch(q_live, dev_id, since, until)
            else:
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

    async def export_rows(
        self,
        resolution: str,
        since: datetime,
        until: datetime,
        dev_ids: Optional[List[int]],
    ) -> List[ExportRow]:
        """Return per-device RMS/peak rows for the xlsx export."""
        dev_filter = "AND dev_id = ANY($3::bigint[])" if dev_ids else ""
        params = [since, until] + ([dev_ids] if dev_ids else [])

        if resolution in _EXPORT_SOURCES:
            table, time_col, rms_col, peak_col = _EXPORT_SOURCES[resolution]
            q = f"""
      SELECT dev_id, {time_col} AS ts, {rms_col} AS a_rms_mg, {peak_col} AS a_peak_mg
      FROM {table}
      WHERE {time_col} >= $1 AND {time_col} <= $2 {dev_filter}
      ORDER BY {time_col}
    """
        else:
            # GROUP BY/ORDER BY the bucket by ordinal (2) — see comment in agg().
            q = f"""
      SELECT dev_id, time_bucket(INTERVAL '{_AGG_BUCKET_INTERVALS[resolution]}', ts) AS ts,
             MAX(a_rms_mg)  AS a_rms_mg,
             MAX(a_peak_mg) AS a_peak_mg
      FROM imu_agg_1s
      WHERE ts >= $1 AND ts <= $2 {dev_filter}
      GROUP BY dev_id, 2
      ORDER BY 2
    """

        async with self._pool.acquire() as con:
            try:
                rows = await con.fetch(q, *params)
            except UndefinedTableError:
                rows = []
        return [
            ExportRow(
                dev_id=r["dev_id"],
                ts=r["ts"],
                a_rms_mg=r["a_rms_mg"],
                a_peak_mg=r["a_peak_mg"],
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
