# BodyNet — IMU Pipeline

Full pipeline: **LilyGO T-Watch 2020** accelerometer data via BLE to TimescaleDB with real-time dashboard.

```
[T-Watch 2020] ──BLE──▸ [bridge] ──WS──▸ [worker] ──▸ [TimescaleDB]
                            │                              │
                            ▼                              ▼
                      [Web UI / app.js] ◂──HTTP/WS── [nginx :8080]
                            │                              │
                            ▼                              ▼
                      [FastAPI /api/*] ◂───────────────────┘
```

## Structure

```
imu-stack/
├── firmware/              # Arduino sketch for T-Watch 2020
│   └── twatch_imu_ble.ino
├── bridge/                # BLE → WebSocket bridge (host, requires Bluetooth)
│   ├── bridge_ble_ws.py
│   └── requirements.txt
├── worker/                # WS consumer → TimescaleDB writer
│   ├── imu_ingest_worker.py
│   ├── requirements.txt
│   └── Dockerfile
├── api/                   # FastAPI REST API
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── web/                   # Frontend dashboard
│   ├── index.html
│   └── app.js
├── infra/                 # Nginx reverse proxy
│   ├── nginx.conf
│   └── Dockerfile
├── migrations/
│   └── imu_timescale_migrations.sql
├── docker-compose.yml
├── Makefile
└── .env.example
```

## Quick Start

### 1. Start the stack

```bash
cp .env.example .env
make up
```

This starts:
- **db** — TimescaleDB (Postgres 16), exposed on port 5433
- **worker** — Python ingest worker (WS → DB)
- **api** — FastAPI on internal port 8000
- **nginx** — Frontend + proxy on [http://localhost:8080](http://localhost:8080)

### 2. Run the bridge (on host)

The bridge needs direct access to Bluetooth, so it runs on the host machine:

```bash
cd bridge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bridge_ble_ws.py
```

WebSocket server starts at `ws://127.0.0.1:8765/ws/json`.

Supports up to **4 T-Watch devices** simultaneously with automatic discovery and reconnection.

### 3. Flash the firmware

Open `firmware/twatch_imu_ble.ino` in Arduino IDE / PlatformIO. Requires:
- LilyGoWatch library
- NimBLE-Arduino

Flash to T-Watch 2020. The device advertises as `T-Watch-IMU` and waits for BLE connection.

### 4. Open the dashboard

Navigate to [http://localhost:8080](http://localhost:8080).

## Data Flow

1. **Firmware** — samples BMA423 at 100 Hz, packs 128-sample windows (50% overlap), sends binary frames over BLE with CRC32
2. **Bridge** — receives BLE notifications, reassembles fragmented frames, verifies CRC, broadcasts JSON over WebSocket
3. **Worker** — consumes WebSocket, writes raw windows + computed features (RMS, peak, steps) to TimescaleDB
4. **API** — serves aggregated data (`/agg/10s`, `/agg/1m`, `/features`, `/windows/latest`)
5. **Frontend** — real-time chart via WebSocket, activity heatmap via REST API

## Database

Connection: `postgresql://postgres:postgres@localhost:5433/imu`

| Table | Description |
|-------|-------------|
| `imu_windows` | Raw 128-sample windows (bytea arrays), hypertable on `ts0` |
| `imu_features` | Per-window features: RMS, peak, step count, fall flag |
| `imu_agg_1s` | 1-second aggregates |
| `cagg_imu_agg_10s` | Continuous aggregate — 10s buckets |
| `cagg_imu_agg_1m` | Continuous aggregate — 1min buckets |

Compression after 3–6 hours, retention: 30 days (windows), 180 days (features/aggregates).

## API Endpoints

| Method | Path | Params | Description |
|--------|------|--------|-------------|
| GET | `/health` | — | Health check |
| GET | `/agg/10s` | `dev_id`, `since?`, `until?` | 10-second aggregates |
| GET | `/agg/1m` | `dev_id`, `since?`, `until?` | 1-minute aggregates |
| GET | `/features` | `dev_id`, `since?`, `until?`, `limit?` | Per-window features |
| GET | `/windows/latest` | `dev_id` | Latest raw window metadata |

## Useful Commands

```bash
make up       # Start all services
make down     # Stop and remove volumes
make logs     # Tail logs
make ps       # Show running services
```

## Notes

- On **Linux**, `host.docker.internal` is resolved via `extra_hosts` in docker-compose. If issues persist, replace with your host IP.
- The bridge is intentionally not containerized — it needs host Bluetooth access.
- Units throughout the pipeline are **mG** (milli-g). The firmware converts from BMA423 LSB to mG before transmission.
