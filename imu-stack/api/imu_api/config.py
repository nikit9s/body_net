"""Application configuration.

Holds the immutable :class:`Settings` value object built from environment
variables. This module has no dependency on any other application layer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass(frozen=True)
class Settings:
    """Immutable application settings.

    Attributes:
        db_dsn: PostgreSQL/TimescaleDB connection string.
        pool_min_size: Minimum size of the asyncpg connection pool.
        pool_max_size: Maximum size of the asyncpg connection pool.
        cors_origins: Allowed CORS origins. ``("*",)`` means allow all.
    """

    db_dsn: str = "postgresql://postgres:postgres@db:5432/imu"
    pool_min_size: int = 1
    pool_max_size: int = 8
    cors_origins: Tuple[str, ...] = ("*",)

    @classmethod
    def from_env(cls) -> "Settings":
        """Build :class:`Settings` from environment variables.

        Reads ``DB_DSN`` (defaulting to the local compose DSN) and, optionally,
        a comma-separated ``CORS_ORIGINS`` list. Pool sizes preserve the
        original ``min_size=1, max_size=8`` behavior.

        Returns:
            A frozen :class:`Settings` instance.
        """
        db_dsn = os.getenv("DB_DSN", "postgresql://postgres:postgres@db:5432/imu")
        raw_origins = os.getenv("CORS_ORIGINS")
        if raw_origins:
            origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())
        else:
            origins = ("*",)
        return cls(
            db_dsn=db_dsn,
            pool_min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "8")),
            cors_origins=origins,
        )

    @property
    def allow_all_origins(self) -> bool:
        """Whether CORS is configured to allow every origin."""
        return tuple(self.cors_origins) == ("*",)
