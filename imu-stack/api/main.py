import io
import os
from datetime import datetime, timedelta
from typing import Optional, List

import asyncpg
import pandas as pd
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

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

@app.get("/agg/{resolution}")
async def agg(
    resolution: str,
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
):
    if resolution not in _AGG_BUCKET_INTERVALS:
        raise HTTPException(status_code=404, detail=f"unknown resolution '{resolution}'")

    pool = await get_pool()
    q_live = f"""
      SELECT time_bucket(INTERVAL '{_AGG_BUCKET_INTERVALS[resolution]}', ts) AS ts,
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

# Table + time column to read from for export resolutions backed directly by a
# table (raw per-second, or a materialized continuous aggregate). Resolutions
# without an entry here (5m/10m/15m/30m) are live-bucketed from imu_agg_1s.
_EXPORT_SOURCES = {
    "1s":  ("imu_agg_1s",       "ts",     "a_rms_mg",     "a_peak_mg"),
    "10s": ("cagg_imu_agg_10s", "bucket", "a_rms_mg_max", "a_peak_mg_max"),
    "1m":  ("cagg_imu_agg_1m",  "bucket", "a_rms_mg_max", "a_peak_mg_max"),
}

_EXPORT_RESOLUTIONS = set(_EXPORT_SOURCES) | set(_AGG_BUCKET_INTERVALS)

@app.get("/export/xlsx")
async def export_xlsx(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    resolution: str = Query("1m"),
    dev_ids: Optional[str] = Query(None, description="Comma-separated dev_id list; default = all devices with data in range"),
):
    """Export peak/RMS acceleration as an .xlsx workbook: time as rows, one column per device.

    Sheet "RMS_mG" and sheet "Peak_mG" each have a `time` column followed by
    `dev_<id>` columns — pick a wider table (mG) per acceleration metric.
    """
    if resolution not in _EXPORT_RESOLUTIONS:
        raise HTTPException(status_code=404, detail=f"unknown resolution '{resolution}'")

    until = until or datetime.utcnow()
    since = since or (until - timedelta(hours=1))

    ids: Optional[List[int]] = None
    if dev_ids:
        ids = [int(x) for x in dev_ids.split(",") if x.strip()]

    dev_filter = "AND dev_id = ANY($3::bigint[])" if ids else ""
    params = [since, until] + ([ids] if ids else [])

    if resolution in _EXPORT_SOURCES:
        table, time_col, rms_col, peak_col = _EXPORT_SOURCES[resolution]
        q = f"""
          SELECT dev_id, {time_col} AS ts, {rms_col} AS a_rms_mg, {peak_col} AS a_peak_mg
          FROM {table}
          WHERE {time_col} >= $1 AND {time_col} <= $2 {dev_filter}
          ORDER BY {time_col}
        """
    else:
        q = f"""
          SELECT dev_id, time_bucket(INTERVAL '{_AGG_BUCKET_INTERVALS[resolution]}', ts) AS ts,
                 MAX(a_rms_mg)  AS a_rms_mg,
                 MAX(a_peak_mg) AS a_peak_mg
          FROM imu_agg_1s
          WHERE ts >= $1 AND ts <= $2 {dev_filter}
          GROUP BY dev_id, ts
          ORDER BY ts
        """

    pool = await get_pool()
    async with pool.acquire() as con:
        try:
            rows = await con.fetch(q, *params)
        except UndefinedTableError:
            raise HTTPException(
                status_code=404,
                detail=f"data source for resolution '{resolution}' not available yet (continuous aggregate not created/refreshed)",
            )

    if not rows:
        raise HTTPException(status_code=404, detail="no data for the given range")

    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert(None)  # Excel doesn't support tz-aware datetimes

    def pivot(value_col: str) -> pd.DataFrame:
        wide = df.pivot(index="ts", columns="dev_id", values=value_col).sort_index()
        wide.columns = [f"dev_{c}" for c in wide.columns]
        return wide

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pivot("a_rms_mg").to_excel(writer, sheet_name="RMS_mG", index_label="time")
        pivot("a_peak_mg").to_excel(writer, sheet_name="Peak_mG", index_label="time")
    buf.seek(0)

    filename = f"imu_export_{resolution}_{since:%Y%m%d%H%M}_{until:%Y%m%d%H%M}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )