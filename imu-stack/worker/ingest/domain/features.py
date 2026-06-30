"""Pure feature computation from a raw IMU window."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .dsp import count_steps, hp_filter, safe_magnitude
from .models import ImuWindow, SecondAggregate, WindowFeatures

SMOOTHING_KERNEL  = 5
FALL_THRESHOLD_MG = 3500


def compute_features(window: ImuWindow) -> tuple[WindowFeatures, SecondAggregate]:
    """Compute per-window features and the 1-second aggregate for a window.

    Performs magnitude → high-pass → smoothing, then derives RMS, peak,
    step count and fall flag, plus the 1-second bucket aggregate.
    """
    amag_mg = safe_magnitude(window.ax, window.ay, window.az)
    amag_hp = hp_filter(amag_mg)
    if len(amag_hp) >= SMOOTHING_KERNEL:
        kernel = np.ones(SMOOTHING_KERNEL) / SMOOTHING_KERNEL
        amag_hp = np.convolve(amag_hp, kernel, mode="same")

    a_rms_mg  = int(float(np.sqrt(np.mean(amag_mg ** 2))))
    a_peak_mg = int(float(np.max(amag_mg)))
    steps     = int(count_steps(amag_hp, window.fs_hz))
    fall_flag = a_peak_mg > FALL_THRESHOLD_MG

    features = WindowFeatures(
        a_rms_mg=a_rms_mg,
        a_peak_mg=a_peak_mg,
        steps=steps,
        fall_flag=fall_flag,
    )

    ts_sec = int(window.ts0_ns // 1_000_000_000)
    ts_bucket = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    aggregate = SecondAggregate(
        dev_id=window.dev_id,
        ts=ts_bucket,
        a_rms_mg=a_rms_mg,
        a_peak_mg=a_peak_mg,
        steps=steps,
    )

    return features, aggregate
