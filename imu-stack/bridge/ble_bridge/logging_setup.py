"""Colored logging setup for the BLE bridge.

Provides the :class:`_Formatter` used across the service, a
:func:`setup_logging` entry point (the composition root calls it once), and a
:func:`get_logger` helper for obtaining child loggers under the ``bridge``
namespace.
"""

from __future__ import annotations

import logging
import sys
import time

_ROOT_NAME = "bridge"


class _Formatter(logging.Formatter):
    """Compact, optionally colored log formatter."""

    LEVEL_TAG = {
        logging.DEBUG:    "DBG",
        logging.INFO:     "INF",
        logging.WARNING:  "WRN",
        logging.ERROR:    "ERR",
        logging.CRITICAL: "CRT",
    }
    LEVEL_COLOR = {
        logging.DEBUG:    "\033[37m",     # white/gray
        logging.INFO:     "\033[36m",     # cyan
        logging.WARNING:  "\033[33m",     # yellow
        logging.ERROR:    "\033[31m",     # red
        logging.CRITICAL: "\033[1;31m",   # bold red
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool = True):
        super().__init__()
        self._color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms = int(record.created * 1000) % 1000
        tag = self.LEVEL_TAG.get(record.levelno, "???")
        name = record.name.split(".")[-1]
        msg = record.getMessage()

        if self._color:
            c = self.LEVEL_COLOR.get(record.levelno, "")
            return f"{ts}.{ms:03d} {c}{tag}{self.RESET} [{name:>4s}] {msg}"
        return f"{ts}.{ms:03d} {tag} [{name:>4s}] {msg}"


def setup_logging(log_level: str) -> logging.Logger:
    """Configure the ``bridge`` root logger and return it."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter(use_color=sys.stdout.isatty()))
    root = logging.getLogger(_ROOT_NAME)
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.DEBUG))
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the ``bridge`` root namespace."""
    return logging.getLogger(_ROOT_NAME).getChild(name)
