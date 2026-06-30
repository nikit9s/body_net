import os
from datetime import datetime
from typing import Optional, List

import asyncpg
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from asyncpg.exceptions import UndefinedTableError

DB_DSN = os.getenv('DB_DSN', 'postgresql://postgres:postgres@db:5432/imu')

app = FastAPI(title="IMU API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

async def get_pool():
    if not hasattr(app.state, 'pool'):
        app.state.pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=8)
    return app.state.pool

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/devices")
async def list_devices():
    """Return all known dev_ids that have sent data in the last 24 hours."""
    pool = await get_pool()
    q = """
      SELECT DISTINCT dev_id
      FROM imu_agg_1s
      WHERE ts > now() - INTERVAL '24 hours'
      ORDER BY dev_id
    """
    async with pool.acquire() as con:
        rows = await con.fetch(q)
    return [r["dev_id"] for r in rows]

@app.get("/agg/10s")
async def agg_10s(
    dev_id: int = Query(...),
    since: Optional[datetime] = None,   # <-- было str
    until: Optional[datetime] = None    # <-- было str
):
    pool = await get_pool()
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
    async with pool.acquire() as con:
        try:
            rows = await con.fetch(q_cagg, dev_id, since, until)
        except UndefinedTableError:
            rows = await con.fetch(q_live, dev_id, since, until)
    return [dict(r) for r in rows]

@app.get("/agg/1m")
async def agg_1m(
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None
):
    pool = await get_pool()
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
    async with pool.acquire() as con:
        try:
            rows = await con.fetch(q_cagg, dev_id, since, until)
        except UndefinedTableError:
            rows = await con.fetch(q_live, dev_id, since, until)
    return [dict(r) for r in rows]

@app.get("/features")
async def features(
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 1000
):
    pool = await get_pool()
    q = """
      SELECT ts0, a_rms_mg, a_peak_mg, step_count, fall_flag
      FROM imu_features
      WHERE dev_id=$1 AND ($2::timestamptz IS NULL OR ts0 >= $2)
                     AND ($3::timestamptz IS NULL OR ts0 <= $3)
      ORDER BY ts0 DESC
      LIMIT $4
    """
    async with pool.acquire() as con:
        rows = await con.fetch(q, dev_id, since, until, limit)
    return [dict(r) for r in rows]

@app.get("/windows/latest")
async def latest_window(dev_id: int = Query(...)):
    pool = await get_pool()
    q = """
      SELECT seq, ts0, fs_hz, n
      FROM imu_windows
      WHERE dev_id=$1
      ORDER BY ts0 DESC, seq DESC
      LIMIT 1
    """
    async with pool.acquire() as con:
        row = await con.fetchrow(q, dev_id)
    if not row:
        raise HTTPException(status_code=404, detail="no data")
    return dict(row)