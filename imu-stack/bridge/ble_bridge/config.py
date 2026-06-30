"""Runtime configuration loaded from the environment.

Only the genuinely env-tunable settings live here. Protocol and timing
constants (which are not meant to be changed at runtime) live in
``ble_bridge.domain.protocol``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable bridge settings sourced from environment variables."""

    ws_host: str
    ws_port: int
    max_devices: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Build a :class:`Settings` instance from the process environment."""
        log_level = os.environ.get(
            "LOG_LEVEL",
            "DEBUG" if os.environ.get("VERBOSE", "1") == "1" else "INFO",
        )
        return cls(
            ws_host=os.environ.get("WS_HOST", "0.0.0.0"),
            ws_port=int(os.environ.get("WS_PORT", "8765")),
            max_devices=int(os.environ.get("MAX_DEVICES", "4")),
            log_level=log_level,
        )
