#!/usr/bin/env python3
"""IMU ingest worker.

Connects to the BLE-WS bridge, receives JSON windows {meta, ax, ay, az},
writes raw windows into TimescaleDB (imu_windows), computes per-window
features (RMS, peak, steps) and maintains 1-second aggregates.

ENV:
  DB_DSN  — PostgreSQL DSN  (default: postgresql://postgres:postgres@localhost:5432/imu)
  WS_URL  — Bridge WS URL   (default: ws://127.0.0.1:8765/ws/json)
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import asyncpg
import numpy as np
import websockets

# ─── Configuration ───────────────────────────────────────────────────

DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:postgres@localhost:5432/imu")
WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8765/ws/json")

STEP_THRESHOLD_MG = 200.0
STEP_REFRACTORY_S = 0.25
HP_FILTER_ALPHA   = 0.98
SMOOTHING_KERNEL  = 5
FALL_THRESHOLD_MG = 3500

WS_RECONNECT_S = 2.0

# ─── Logging ─────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    log = logging.getLogger("ingest")
    log.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    log.addHandler(h)
    return log

log = _setup_logging()

# ─── Signal processing ──────────────────────────────────────────────

def safe_magnitude(ax: np.ndarray, ay: np.ndarray, az: np.ndarray) -> np.ndarray:
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

# ─── Database ────────────────────────────────────────────────────────

async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=6)


async def insert_raw_and_features(conn: asyncpg.Connection, payload: dict):
    meta = payload["meta"]
    dev_id = int(meta["dev_id"])
    seq    = int(meta["seq"])
    ts0_ns = int(meta["ts0_ns"])
    fs_hz  = int(meta["fs_hz"])
    n      = int(meta["n"])
    axes   = int(meta["axes"])
    batt   = int(meta.get("batt", 0))

    ax = np.asarray(payload["ax"], dtype=np.int16)
    ay = np.asarray(payload["ay"], dtype=np.int16)
    az = np.asarray(payload["az"], dtype=np.int16)

    await conn.execute(
        """
        INSERT INTO imu_windows(dev_id, seq, ts0, fs_hz, n, axes, batt, ax, ay, az, crc32)
        VALUES($1, $2, to_timestamp($3 / 1e9), $4, $5, $6, $7,
               $8::bytea, $9::bytea, $10::bytea, $11)
        ON CONFLICT (dev_id, ts0, seq) DO NOTHING
        """,
        dev_id, seq, ts0_ns, fs_hz, n, axes, batt,
        memoryview(ax).tobytes(),
        memoryview(ay).tobytes(),
        memoryview(az).tobytes(),
        0,
    )

    amag_mg = safe_magnitude(ax, ay, az)
    amag_hp = hp_filter(amag_mg)
    if len(amag_hp) >= SMOOTHING_KERNEL:
        kernel = np.ones(SMOOTHING_KERNEL) / SMOOTHING_KERNEL
        amag_hp = np.convolve(amag_hp, kernel, mode="same")

    a_rms_mg  = int(float(np.sqrt(np.mean(amag_mg ** 2))))
    a_peak_mg = int(float(np.max(amag_mg)))
    steps     = int(count_steps(amag_hp, fs_hz))
    fall_flag = a_peak_mg > FALL_THRESHOLD_MG

    await conn.execute(
        """
        INSERT INTO imu_features(dev_id, seq, ts0, a_rms_mg, a_peak_mg, fall_flag, step_count)
        VALUES($1, $2, to_timestamp($3 / 1e9), $4, $5, $6, $7)
        ON CONFLICT (dev_id, ts0, seq) DO UPDATE
          SET a_rms_mg   = EXCLUDED.a_rms_mg,
              a_peak_mg  = EXCLUDED.a_peak_mg,
              fall_flag  = EXCLUDED.fall_flag,
              step_count = EXCLUDED.step_count
        """,
        dev_id, seq, ts0_ns, a_rms_mg, a_peak_mg, fall_flag, steps,
    )

    ts_sec = int(ts0_ns // 1_000_000_000)
    ts_bucket = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    await conn.execute(
        """
        INSERT INTO imu_agg_1s(dev_id, ts, a_rms_mg, a_peak_mg, steps)
        VALUES($1, $2, $3, $4, $5)
        ON CONFLICT (dev_id, ts) DO UPDATE
          SET a_rms_mg  = GREATEST(imu_agg_1s.a_rms_mg, EXCLUDED.a_rms_mg),
              a_peak_mg = GREATEST(imu_agg_1s.a_peak_mg, EXCLUDED.a_peak_mg),
              steps     = imu_agg_1s.steps + EXCLUDED.steps
        """,
        dev_id, ts_bucket, a_rms_mg, a_peak_mg, steps,
    )

# ─── Main loop ───────────────────────────────────────────────────────

async def run():
    pool = await create_pool()
    log.info("db pool created  dsn=%s", DB_DSN.split("@")[-1])

    frames_total = 0
    errors_total = 0

    while True:
        log.info("connecting to bridge  url=%s", WS_URL)
        try:
            async with websockets.connect(WS_URL, max_size=None) as ws:
                log.info("ws connected")
                session_frames = 0
                session_start = time.monotonic()

                async for msg in ws:
                    try:
                        obj = json.loads(msg)
                        if obj.get("type") != "imu":
                            continue
                        async with pool.acquire() as conn:
                            await insert_raw_and_features(conn, obj)
                        frames_total += 1
                        session_frames += 1
                        if session_frames % 100 == 0:
                            elapsed = time.monotonic() - session_start
                            rate = session_frames / elapsed if elapsed > 0 else 0
                            log.info("progress  session=%d  total=%d  rate=%.1f frames/s  errors=%d",
                                     session_frames, frames_total, rate, errors_total)
                    except Exception as e:
                        errors_total += 1
                        log.error("frame processing error: %s", e)

                log.warning("ws stream ended (server closed)")
        except asyncio.CancelledError:
            log.info("shutting down")
            break
        except Exception as e:
            log.warning("ws disconnected: %s — reconnecting in %.0fs", e, WS_RECONNECT_S)
            try:
                await asyncio.sleep(WS_RECONNECT_S)
            except asyncio.CancelledError:
                log.info("shutting down during reconnect wait")
                break

    await pool.close()
    log.info("stopped  total_frames=%d  total_errors=%d", frames_total, errors_total)


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(run())

    def _shutdown():
        log.info("signal received, cancelling…")
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(task)
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
