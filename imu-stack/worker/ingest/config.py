"""Configuration settings for the IMU ingest worker."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration, sourced from the environment.

    ENV:
      DB_DSN  — PostgreSQL DSN  (default: postgresql://postgres:postgres@localhost:5432/imu)
      WS_URL  — Bridge WS URL   (default: ws://127.0.0.1:8765/ws/json)
    """

    db_dsn: str
    ws_url: str
    ws_reconnect_s: float = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables, applying defaults."""
        return cls(
            db_dsn=os.getenv(
                "DB_DSN", "postgresql://postgres:postgres@localhost:5432/imu"
            ),
            ws_url=os.getenv("WS_URL", "ws://127.0.0.1:8765/ws/json"),
            ws_reconnect_s=2.0,
        )
