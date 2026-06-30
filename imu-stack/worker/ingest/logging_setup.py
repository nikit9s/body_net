"""Logging configuration for the IMU ingest worker."""

from __future__ import annotations

import logging
import sys


def setup_logging() -> logging.Logger:
    """Create and configure the ``ingest`` logger."""
    log = logging.getLogger("ingest")
    log.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    log.addHandler(h)
    return log
