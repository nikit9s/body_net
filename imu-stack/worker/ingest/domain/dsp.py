"""Pure digital signal-processing primitives (no IO)."""

from __future__ import annotations

import numpy as np

STEP_THRESHOLD_MG = 200.0
STEP_REFRACTORY_S = 0.25
HP_FILTER_ALPHA   = 0.98


def safe_magnitude(ax: np.ndarray, ay: np.ndarray, az: np.ndarray) -> np.ndarray:
    """Vector magnitude of the three axes, computed in int32 to avoid overflow."""
    ax32 = ax.astype(np.int32)
    ay32 = ay.astype(np.int32)
    az32 = az.astype(np.int32)
    return np.sqrt(ax32 * ax32 + ay32 * ay32 + az32 * az32).astype(np.float32)


def hp_filter(x: np.ndarray, alpha: float = HP_FILTER_ALPHA) -> np.ndarray:
    """Single-pole IIR high-pass: y[n] = alpha * (y[n-1] + x[n] - x[n-1])."""
    y = np.zeros_like(x, dtype=np.float32)
    if len(x) == 0:
        return y
    prev_x = x[0]
    for i in range(1, len(x)):
        y[i] = alpha * (y[i - 1] + x[i] - prev_x)
        prev_x = x[i]
    return y


def count_steps(a_hp_mg: np.ndarray, fs_hz: int) -> int:
    """Count peaks above threshold with a refractory gap between steps."""
    min_gap = max(1, int(STEP_REFRACTORY_S * fs_hz))
    cnt = 0
    last_peak = -min_gap
    for i in range(1, len(a_hp_mg) - 1):
        is_peak = (a_hp_mg[i] > STEP_THRESHOLD_MG
                   and a_hp_mg[i] >= a_hp_mg[i - 1]
                   and a_hp_mg[i] >= a_hp_mg[i + 1])
        if is_peak and i - last_peak >= min_gap:
            cnt += 1
            last_peak = i
    return cnt
