"""Composition root for the HTTP delivery layer.

:func:`create_app` wires the FastAPI application together: it configures CORS,
defines a lifespan that opens and closes the asyncpg pool exactly once, builds
the repository + query service, and includes the router.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ...application.queries import ImuQueryService
from ...config import Settings
from ..timescale_repo import TimescaleReadRepository
from .routes import router


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build and return the configured FastAPI application.

    Args:
        settings: Application settings. Defaults to :meth:`Settings.from_env`.

    Returns:
        A fully wired :class:`fastapi.FastAPI` instance.
    """
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the connection pool on startup and close it on shutdown."""
        pool = await asyncpg.create_pool(
            dsn=cfg.db_dsn,
            min_size=cfg.pool_min_size,
            max_size=cfg.pool_max_size,
        )
        repository = TimescaleReadRepository(pool)
        app.state.pool = pool
        app.state.service = ImuQueryService(repository)
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="IMU API", version="1.0", lifespan=lifespan)

    # CORS: ``allow_origins=["*"]`` with ``allow_credentials=True`` is invalid
    # per the CORS spec, so credentials are disabled when allowing all origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=not cfg.allow_all_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)
    return app
