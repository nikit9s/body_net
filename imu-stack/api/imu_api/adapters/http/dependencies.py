"""FastAPI dependency providers.

Exposes :func:`get_service`, which retrieves the per-app
:class:`~imu_api.application.queries.ImuQueryService` constructed during the
lifespan startup and stored on ``app.state``.
"""
from __future__ import annotations

from fastapi import Request

from ...application.queries import ImuQueryService


def get_service(request: Request) -> ImuQueryService:
    """Return the application query service for the current request.

    Args:
        request: The incoming request, used to reach ``app.state``.

    Returns:
        The :class:`ImuQueryService` built at startup.
    """
    return request.app.state.service
