"""TimescaleDB FeatureRepository implementation (asyncpg)."""

from __future__ import annotations

import asyncpg

from ..domain.models import ImuWindow, SecondAggregate, WindowFeatures


class TimescaleFeatureRepository:
    """FeatureRepository backed by TimescaleDB via an asyncpg pool.

    Holds the exact INSERT statements for raw windows, per-window features and
    the 1-second aggregate, each acquired on a pooled connection.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str) -> "TimescaleFeatureRepository":
        """Create the repository with a freshly-created connection pool."""
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=6)
        return cls(pool)

    async def close(self) -> None:
        """Close the underlying connection pool."""
        await self._pool.close()

    async def persist(
        self,
        window: ImuWindow,
        features: WindowFeatures,
        aggregate: SecondAggregate,
    ) -> None:
        """Persist the raw window, its features and the 1-second aggregate."""
        async with self._pool.acquire() as conn:
            await self._insert_window(conn, window)
            await self._insert_features(conn, window, features)
            await self._upsert_aggregate(conn, aggregate)

    async def _insert_window(
        self, conn: asyncpg.Connection, window: ImuWindow
    ) -> None:
        await conn.execute(
            """
            INSERT INTO imu_windows(dev_id, seq, ts0, fs_hz, n, axes, batt, ax, ay, az, crc32)
            VALUES($1, $2, to_timestamp($3 / 1e9), $4, $5, $6, $7,
                   $8::bytea, $9::bytea, $10::bytea, $11)
            ON CONFLICT (dev_id, ts0, seq) DO NOTHING
            """,
            window.dev_id, window.seq, window.ts0_ns, window.fs_hz, window.n,
            window.axes, window.batt,
            memoryview(window.ax).tobytes(),
            memoryview(window.ay).tobytes(),
            memoryview(window.az).tobytes(),
            0,
        )

    async def _insert_features(
        self,
        conn: asyncpg.Connection,
        window: ImuWindow,
        features: WindowFeatures,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO imu_features(dev_id, seq, ts0, a_rms_mg, a_peak_mg, fall_flag, step_count)
            VALUES($1, $2, to_timestamp($3 / 1e9), $4, $5, $6, $7)
            ON CONFLICT (dev_id, ts0, seq) DO UPDATE
              SET a_rms_mg   = EXCLUDED.a_rms_mg,
                  a_peak_mg  = EXCLUDED.a_peak_mg,
                  fall_flag  = EXCLUDED.fall_flag,
                  step_count = EXCLUDED.step_count
            """,
            window.dev_id, window.seq, window.ts0_ns,
            features.a_rms_mg, features.a_peak_mg, features.fall_flag,
            features.steps,
        )

    async def _upsert_aggregate(
        self, conn: asyncpg.Connection, aggregate: SecondAggregate
    ) -> None:
        await conn.execute(
            """
            INSERT INTO imu_agg_1s(dev_id, ts, a_rms_mg, a_peak_mg, steps)
            VALUES($1, $2, $3, $4, $5)
            ON CONFLICT (dev_id, ts) DO UPDATE
              SET a_rms_mg  = GREATEST(imu_agg_1s.a_rms_mg, EXCLUDED.a_rms_mg),
                  a_peak_mg = GREATEST(imu_agg_1s.a_peak_mg, EXCLUDED.a_peak_mg),
                  steps     = imu_agg_1s.steps + EXCLUDED.steps
            """,
            aggregate.dev_id, aggregate.ts, aggregate.a_rms_mg,
            aggregate.a_peak_mg, aggregate.steps,
        )
