"""Domain value/result types.

These lightweight dataclasses describe the shape of data returned by the read
repository. They depend on nothing app-specific (only the standard library).
Routes may serialize them back to plain dicts to preserve the original JSON
response shapes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class AggBucket:
    """A single aggregated time bucket from a cagg or live ``time_bucket`` query.

    Mirrors the columns ``ts, a_rms_mg_max, a_peak_mg_max, steps``.
    """

    ts: datetime
    a_rms_mg_max: Optional[float]
    a_peak_mg_max: Optional[float]
    steps: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        """Return the bucket as a plain dict matching the original response."""
        return {
            "ts": self.ts,
            "a_rms_mg_max": self.a_rms_mg_max,
            "a_peak_mg_max": self.a_peak_mg_max,
            "steps": self.steps,
        }


@dataclass(frozen=True)
class Feature:
    """A computed feature row from ``imu_features``.

    Mirrors the columns ``ts0, a_rms_mg, a_peak_mg, step_count, fall_flag``.
    """

    ts0: datetime
    a_rms_mg: Optional[float]
    a_peak_mg: Optional[float]
    step_count: Optional[int]
    fall_flag: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        """Return the feature as a plain dict matching the original response."""
        return {
            "ts0": self.ts0,
            "a_rms_mg": self.a_rms_mg,
            "a_peak_mg": self.a_peak_mg,
            "step_count": self.step_count,
            "fall_flag": self.fall_flag,
        }


@dataclass(frozen=True)
class LatestWindow:
    """The latest window row from ``imu_windows``.

    Mirrors the columns ``seq, ts0, fs_hz, n``.
    """

    seq: int
    ts0: datetime
    fs_hz: Optional[float]
    n: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        """Return the window as a plain dict matching the original response."""
        return {
            "seq": self.seq,
            "ts0": self.ts0,
            "fs_hz": self.fs_hz,
            "n": self.n,
        }
