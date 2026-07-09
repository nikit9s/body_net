"""HTTP routes.

Declares the six API endpoints as an :class:`fastapi.APIRouter`. Handlers
depend on :class:`~imu_api.application.queries.ImuQueryService` via FastAPI
dependency injection and serialize domain results back to plain dicts, so the
response shapes are identical to the original service.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...application.queries import ImuQueryService
from .dependencies import get_service

router = APIRouter()


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


@router.get("/agg/10s")
async def agg_10s(
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    service: ImuQueryService = Depends(get_service),
) -> List[Dict[str, Any]]:
    """Return 10-second aggregate buckets for a device within a range."""
    rows = await service.agg_10s(dev_id, since, until)
    return [r.to_dict() for r in rows]


@router.get("/agg/1m")
async def agg_1m(
    dev_id: int = Query(...),
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    service: ImuQueryService = Depends(get_service),
) -> List[Dict[str, Any]]:
    """Return 1-minute aggregate buckets for a device within a range."""
    rows = await service.agg_1m(dev_id, since, until)
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
