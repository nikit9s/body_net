"""ASGI entrypoint.

Exposes ``app`` for ``uvicorn imu_api.asgi:app``. Settings are loaded from the
environment via :meth:`Settings.from_env`.
"""
from __future__ import annotations

from .adapters.http.app import create_app

app = create_app()
