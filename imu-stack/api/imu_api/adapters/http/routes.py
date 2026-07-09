"""HTTP routes.

Declares the six API endpoints as an :class:`fastapi.APIRouter`. Handlers
depend on :class:`~imu_api.application.queries.ImuQueryService` via FastAPI
dependency injection and serialize domain results back to plain dicts, so the
response shapes are identical to the original service.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ...application.queries import ImuQueryService
from .dependencies import get_service

router = APIRouter()

_AGG_RESOLUTIONS = {"10s", "1m", "5m", "10m", "15m", "30m"}
_EXPORT_RESOLUTIONS = {"1s", "10s", "1m", "5m", "10m", "15m", "30m"}


@router.get("/health")
async def health() -> Dict[str, bool]:
    """Liveness probe."""
    return {"ok": True}


@router.get("/devices")
async def list_devices(
    service: ImuQueryService = Depends(get_service),
) -> List[int]:
    """Return all known dev_ids that have sent data in the last 24 hours."""
    return await service.list_devices()


@router.get("/agg/{resolution}")
async def agg(
    resolution: str,
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    service: ImuQueryService = Depends(get_service),
) -> List[Dict[str, Any]]:
    """Return aggregate buckets for a device within a range.

    ``resolution`` is one of ``10s``, ``1m``, ``5m``, ``10m``, ``15m``, ``30m``.
    """
    if resolution not in _AGG_RESOLUTIONS:
        raise HTTPException(status_code=404, detail=f"unknown resolution '{resolution}'")
    rows = await service.agg(resolution, dev_id, since, until)
    return [r.to_dict() for r in rows]


@router.get("/features")
async def features(
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 1000,
    service: ImuQueryService = Depends(get_service),
) -> List[Dict[str, Any]]:
    """Return feature rows for a device, newest first, capped at ``limit``."""
    rows = await service.features(dev_id, since, until, limit)
    return [r.to_dict() for r in rows]


@router.get("/windows/latest")
async def latest_window(
    dev_id: int = Query(...),
    service: ImuQueryService = Depends(get_service),
) -> Dict[str, Any]:
    """Return the latest window for a device, or 404 if none exist."""
    row = await service.latest_window(dev_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no data")
    return row.to_dict()


@router.get("/export/xlsx")
async def export_xlsx(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    resolution: str = Query("1m"),
    dev_ids: Optional[str] = Query(
        None, description="Comma-separated dev_id list; default = all devices with data in range"
    ),
    tz_offset_min: Optional[int] = Query(
        None,
        description="Client's UTC offset in minutes, JS Date.getTimezoneOffset() convention "
                    "(e.g. -180 for UTC+3): shifts the exported 'time' column to the "
                    "client's local wall-clock time instead of raw UTC",
    ),
    service: ImuQueryService = Depends(get_service),
) -> StreamingResponse:
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

    rows = await service.export_rows(resolution, since, until, ids)
    if not rows:
        raise HTTPException(status_code=404, detail="no data for the given range")

    df = pd.DataFrame([r.to_dict() for r in rows])
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert(None)  # Excel doesn't support tz-aware datetimes
    if tz_offset_min is not None:
        # getTimezoneOffset() is UTC-minus-local, in minutes (negative east of UTC),
        # so local = utc - offset.
        df["ts"] -= pd.Timedelta(minutes=tz_offset_min)

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
