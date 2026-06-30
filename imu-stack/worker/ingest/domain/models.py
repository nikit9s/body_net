"""Domain models for the IMU ingest pipeline."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ImuWindow:
    """A single raw IMU window received from the bridge."""

    dev_id: int
    seq: int
    ts0_ns: int
    fs_hz: int
    n: int
    axes: int
    batt: int
    ax: np.ndarray
    ay: np.ndarray
    az: np.ndarray

    @classmethod
    def from_payload(cls, payload: dict) -> "ImuWindow":
        """Parse a WS JSON payload ``{meta, ax, ay, az}`` into an ImuWindow."""
        meta = payload["meta"]
        return cls(
            dev_id=int(meta["dev_id"]),
            seq=int(meta["seq"]),
            ts0_ns=int(meta["ts0_ns"]),
            fs_hz=int(meta["fs_hz"]),
            n=int(meta["n"]),
            axes=int(meta["axes"]),
            batt=int(meta.get("batt", 0)),
            ax=np.asarray(payload["ax"], dtype=np.int16),
            ay=np.asarray(payload["ay"], dtype=np.int16),
            az=np.asarray(payload["az"], dtype=np.int16),
        )


@dataclass(frozen=True)
class WindowFeatures:
    """Per-window computed features."""

    a_rms_mg: int
    a_peak_mg: int
    steps: int
    fall_flag: bool


@dataclass(frozen=True)
class SecondAggregate:
    """1-second aggregate bucket for a window."""

    dev_id: int
    ts: "object"  # datetime, kept loose to avoid importing datetime in signature
    a_rms_mg: int
    a_peak_mg: int
    steps: int
