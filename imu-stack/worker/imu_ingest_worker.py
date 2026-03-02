#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# IMU ingest worker:
# - connects to ws://127.0.0.1:8765/ws/json (your bridge_ble_ws.py)
# - receives JSON windows {meta, ax, ay, az}
# - writes RAW window into TimescaleDB (imu_windows)
# - computes features (RMS/PEAK/steps) and writes into imu_features
# - aggregates per-second metrics into imu_agg_1s
#
# ENV:
#   DB_DSN="postgresql://user:pass@localhost:5432/imu"
#   WS_URL="ws://127.0.0.1:8765/ws/json"

import os
import asyncio
import json
import signal
from datetime import datetime, timezone

import numpy as np
import asyncpg
import websockets

DB_DSN = os.getenv("DB_DSN", "postgresql://postgres:postgres@localhost:5432/imu")
WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8765/ws/json")

STEP_THR_MG = 200.0
REFRACTORY_SEC = 0.25

async def ensure_conn():
    return await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=6)

def safe_magnitude(ax: np.ndarray, ay: np.ndarray, az: np.ndarray) -> np.ndarray:
    """Compute |a| safely: cast to int32 to avoid int16 overflow on square."""
    ax32 = ax.astype(np.int32)
    ay32 = ay.astype(np.int32)
    az32 = az.astype(np.int32)
    return np.sqrt(ax32*ax32 + ay32*ay32 + az32*az32).astype(np.float32)

def hp_filter_simple(x: np.ndarray, alpha: float = 0.98) -> np.ndarray:
    """Simple 1-pole high-pass: y[n] = alpha*(y[n-1] + x[n] - x[n-1]).
    alpha ≈ exp(-2π*fc/fs)."""
    y = np.zeros_like(x, dtype=np.float32)
    if len(x) == 0:
        return y
    prev_x = x[0]
    for i in range(1, len(x)):
        y[i] = alpha * (y[i-1] + x[i] - prev_x)
        prev_x = x[i]
    return y

def count_steps(a_hp_mg: np.ndarray, fs_hz: int) -> int:
    thr = STEP_THR_MG
    min_i = max(1, int(REFRACTORY_SEC * fs_hz))
    cnt = 0
    last_peak = -min_i
    for i in range(1, len(a_hp_mg)-1):
        if a_hp_mg[i] > thr and a_hp_mg[i] >= a_hp_mg[i-1] and a_hp_mg[i] >= a_hp_mg[i+1]:
            if i - last_peak >= min_i:
                cnt += 1
                last_peak = i
    return cnt

async def insert_raw_and_features(conn, payload: dict):
    meta = payload["meta"]
    dev_id = int(meta["dev_id"])
    seq = int(meta["seq"])
    ts0_ns = int(meta["ts0_ns"])
    fs_hz = int(meta["fs_hz"])
    n = int(meta["n"])
    axes = int(meta["axes"])
    batt = int(meta.get("batt", 0))

    ax = np.asarray(payload["ax"], dtype=np.int16)
    ay = np.asarray(payload["ay"], dtype=np.int16)
    az = np.asarray(payload["az"], dtype=np.int16)

    # For raw window CRC we don't have it in JSON (already verified by bridge), store 0
    crc32 = 0

    # 1) RAW window insert
    await conn.execute(
        """
        INSERT INTO imu_windows(dev_id, seq, ts0, fs_hz, n, axes, batt, ax, ay, az, crc32)
        VALUES($1,$2, to_timestamp($3/1e9), $4, $5, $6, $7,
               $8::bytea, $9::bytea, $10::bytea, $11)
        ON CONFLICT (dev_id, ts0, seq) DO NOTHING
        """,
        dev_id, seq, ts0_ns, fs_hz, n, axes, batt,
        memoryview(ax).tobytes(),
        memoryview(ay).tobytes(),
        memoryview(az).tobytes(),
        crc32
    )

    # 2) Features — firmware already sends values in mG
    amag_mg = safe_magnitude(ax, ay, az)

    # quick high-pass to remove gravity for step detection
    amag_hp = hp_filter_simple(amag_mg, alpha=0.98)
    # smooth a bit
    if len(amag_hp) >= 5:
        amag_hp = np.convolve(amag_hp, np.ones(5)/5.0, mode='same')

    a_rms_mg = int(float(np.sqrt(np.mean(amag_mg**2))))
    a_peak_mg = int(float(np.max(amag_mg)))
    steps = int(count_steps(amag_hp, fs_hz))

    await conn.execute(
        """
        INSERT INTO imu_features(dev_id, seq, ts0, a_rms_mg, a_peak_mg, fall_flag, step_count)
        VALUES($1,$2, to_timestamp($3/1e9), $4, $5, $6, $7)
        ON CONFLICT (dev_id, ts0, seq) DO UPDATE
        SET a_rms_mg=EXCLUDED.a_rms_mg,
            a_peak_mg=EXCLUDED.a_peak_mg,
            fall_flag=EXCLUDED.fall_flag,
            step_count=EXCLUDED.step_count
        """,
        dev_id, seq, ts0_ns, a_rms_mg, a_peak_mg, bool(a_peak_mg > 3500), steps
    )

    # 3) 1-sec aggregate (bucket by floor(ts0))
    ts_sec = int(ts0_ns // 1_000_000_000)
    ts_bucket = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    await conn.execute(
        """
        INSERT INTO imu_agg_1s(dev_id, ts, a_rms_mg, a_peak_mg, steps)
        VALUES($1, $2, $3, $4, $5)
        ON CONFLICT (dev_id, ts) DO UPDATE
        SET a_rms_mg = GREATEST(imu_agg_1s.a_rms_mg, EXCLUDED.a_rms_mg),
            a_peak_mg = GREATEST(imu_agg_1s.a_peak_mg, EXCLUDED.a_peak_mg),
            steps     = imu_agg_1s.steps + EXCLUDED.steps
        """,
        dev_id, ts_bucket, a_rms_mg, a_peak_mg, steps
    )

async def run():
    pool = await ensure_conn()
    print(f"[ingest] connecting WS → {WS_URL}")
    # reconnect loop
    while True:
        try:
            async with websockets.connect(WS_URL, max_size=None) as ws:
                print("[ingest] WS connected")
                async for msg in ws:
                    try:
                        obj = json.loads(msg)
                        if obj.get("type") != "imu":
                            continue
                        async with pool.acquire() as conn:
                            await insert_raw_and_features(conn, obj)
                    except Exception as e:
                        print("[ingest] error:", e)
        except Exception as e:
            print("[ingest] WS error, retrying in 2s:", e)
            await asyncio.sleep(2.0)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, loop.stop)
        except NotImplementedError:
            pass
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
