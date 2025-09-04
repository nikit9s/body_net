# IMU Stack (T-Watch → BLE → WebSocket → TimescaleDB)

Полный пайплайн для сбора и анализа данных акселерометра с часов **LilyGO T-Watch 2020**:

* Прошивка на Arduino/ESP-IDF (`firmware/`) — собирает данные IMU, пакует в бинарные кадры и отправляет по BLE.
* Python-bridge (`bridge/bridge_ble_ws.py`) — принимает BLE-нотификации, собирает фрагменты в кадры, проверяет CRC, отдает в WebSocket (`/ws/json`, `/ws/bin`).
* Ingest worker (`worker/imu_ingest_worker.py`) — подписывается на WebSocket, пишет окна/фичи в TimescaleDB.
* TimescaleDB + миграции (`migrations/imu_timescale_migrations.sql`) — таблицы `imu_windows`, `imu_features`, `imu_agg_1s` с compression/retention политиками.
* Nginx + html интерфейс (`web/`) — простая визуализация.

---

## 📂 Структура проекта

```
imu-stack/
├── bridge/                     # Python BLE→WS мост
│   ├── bridge_ble_ws.py
│   └── requirements.txt
├── worker/                     # Ingest worker (WS→TimescaleDB)
│   ├── imu_ingest_worker.py
│   └── requirements.txt
├── firmware/                   # прошивка для LilyGO T-Watch
│   └── twatch_imu_ble.ino
├── migrations/
│   └── imu_timescale_migrations.sql
├── web/                        # статический интерфейс (nginx)
│   └── index.html
├── infra/                      # инфраструктура
│   ├── docker-compose.yml
│   ├── Dockerfile.worker
│   ├── Dockerfile.nginx
│   └── nginx.conf
└── README.md
```

---

## 🚀 Быстрый старт

### 1. Запуск стека

```bash
docker compose up -d --build
```

Поднимутся:

* **db** — TimescaleDB (Postgres 16);
* **worker** — Python-воркер для записи данных;
* **nginx** — фронтенд на [http://localhost:8080](http://localhost:8080).

### 2. Bridge (локально)

Bridge работает на машине с BLE (Linux/macOS).

```bash
cd bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bridge_ble_ws.py
```

Он поднимет WebSocket на `ws://127.0.0.1:8765/ws/json` и начнет слушать T-Watch.

### Множественные устройства

Bridge теперь поддерживает одновременное подключение до **4 устройств T-Watch**:

* Автоматическое обнаружение и подключение новых устройств
* Независимое управление каждым устройством
* Автоматическое переподключение при разрыве связи
* Данные от разных устройств различаются по `dev_id` в JSON/WebSocket потоках

Каждое устройство получает уникальный `dev_id`, который сохраняется в базе данных для идентификации источника данных.

### 3. Worker

Worker работает в контейнере (см. docker-compose). Если нужно локально:

```bash
cd worker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python imu_ingest_worker.py
```

Он подключится к `ws://bridge:8765/ws/json` и запишет данные в TimescaleDB.

### 4. TimescaleDB

Подключение к базе:

```
Host: 127.0.0.1
Port: 5433
User: postgres
Password: postgres
Database: imu
```

Можно использовать TablePlus или psql.

---

## 🗄️ Структура базы данных

### `imu_windows`

Сырые окна данных IMU (по 128 сэмплов).

* `dev_id INT` — идентификатор устройства.
* `seq BIGINT` — порядковый номер окна.
* `ts0 TIMESTAMPTZ` — время первого сэмпла в окне.
* `fs_hz INT` — частота дискретизации (обычно 100 Гц).
* `n INT` — количество сэмплов (обычно 128).
* `axes INT` — битовая маска активных осей.
* `batt INT` — заряд батареи устройства (%).
* `ax BYTEA` — массив int16, n сэмплов по оси X.
* `ay BYTEA` — массив int16, n сэмплов по оси Y.
* `az BYTEA` — массив int16, n сэмплов по оси Z.

**Индексы:**

* `PRIMARY KEY (dev_id, seq)`
* `INDEX ON ts0 DESC`
* Hypertable по `ts0`.

### `imu_features`

Фичи, рассчитанные по окнам.

* `dev_id INT`
* `seq BIGINT`
* `ts0 TIMESTAMPTZ`
* `a_rms FLOAT` — корень среднеквадратичный.
* `a_peak FLOAT` — пиковое ускорение.
* `steps INT` — количество шагов (если алгоритм подсчета подключен).
* `fall BOOLEAN` — флаг падения.

**Индексы:**

* `PRIMARY KEY (dev_id, seq)`
* `INDEX ON ts0 DESC`

### `imu_agg_1s`

Агрегация по секундам.

* `dev_id INT`
* `ts TIMESTAMPTZ`
* `a_rms FLOAT`
* `a_peak FLOAT`
* `steps INT`

**Индексы:**

* `PRIMARY KEY (dev_id, ts)`
* Hypertable по `ts`.

---

## 📑 Миграции

Файл `migrations/imu_timescale_migrations.sql` создаёт таблицы и политики:

* Сжатие `imu_windows` каждые 3 часа, хранение 30 дней.
* Сжатие `imu_agg_1s`, `imu_features` каждые 6 часов, хранение 180 дней.

---

## 🔍 Примеры SQL-запросов

### Распаковать окно

```sql
SELECT i,
       CASE WHEN vax<=32767 THEN vax ELSE vax-65536 END AS ax
FROM generate_series(0,127) i
CROSS JOIN LATERAL (
  SELECT get_byte(ax,2*i) + get_byte(ax,2*i+1)*256 AS vax
) q
FROM imu_windows
WHERE dev_id=1 AND seq=42;
```

### RMS и PEAK по окнам

```sql
WITH u AS (
  SELECT dev_id, seq,
         sqrt(ax*ax + ay*ay + az*az) AS amag
  FROM imu_windows_unpacked
)
SELECT dev_id, seq,
       sqrt(avg(amag*amag)) AS rms,
       max(amag) AS peak
FROM u
GROUP BY dev_id, seq;
```

---

## 🌐 Веб-интерфейс

Фронтенд доступен на [http://localhost:8080](http://localhost:8080).
Можно доработать для графиков (например, Chart.js или Plotly), подключившись к API или напрямую к WS.

---

## 🛠 Отладка

* Логи worker: `docker compose logs -f worker`
* Логи db: `docker compose logs -f db`
* Проверка таблиц: `psql -U postgres -d imu -c "\dt"`

---

