-- Enable extensions
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- RAW windows
DROP TABLE IF EXISTS imu_windows CASCADE;
CREATE TABLE imu_windows (
  dev_id BIGINT NOT NULL,
  seq BIGINT NOT NULL,
  ts0 TIMESTAMPTZ NOT NULL,
  fs_hz SMALLINT NOT NULL,
  n SMALLINT NOT NULL,
  axes SMALLINT NOT NULL,
  batt SMALLINT NOT NULL,
  ax BYTEA NOT NULL,
  ay BYTEA NOT NULL,
  az BYTEA NOT NULL,
  crc32 INT NOT NULL,
  PRIMARY KEY (dev_id, ts0, seq)      -- <-- включает ts0!
);
SELECT create_hypertable('imu_windows','ts0', chunk_time_interval=>INTERVAL '1 day', if_not_exists=>TRUE);

-- индексы для частых выборок
CREATE INDEX IF NOT EXISTS idx_imu_windows_dev_ts0 ON imu_windows (dev_id, ts0 DESC);
CREATE INDEX IF NOT EXISTS idx_imu_windows_seq      ON imu_windows (dev_id, seq DESC);

ALTER TABLE imu_windows SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'dev_id',
  timescaledb.compress_orderby   = 'ts0, seq'
);
SELECT add_compression_policy('imu_windows', INTERVAL '3 hours');
SELECT add_retention_policy('imu_windows', INTERVAL '30 days');

-- 1s aggregates
DROP TABLE IF EXISTS imu_agg_1s CASCADE;
CREATE TABLE imu_agg_1s (
  dev_id BIGINT NOT NULL,
  ts TIMESTAMPTZ NOT NULL,
  a_rms_mg INT NOT NULL,
  a_peak_mg INT NOT NULL,
  steps INT NOT NULL DEFAULT 0,
  PRIMARY KEY (dev_id, ts)            -- уже ок, включает ts
);
SELECT create_hypertable('imu_agg_1s','ts', chunk_time_interval=>INTERVAL '7 days', if_not_exists=>TRUE);
CREATE INDEX IF NOT EXISTS idx_imu_agg_1s_dev_ts ON imu_agg_1s (dev_id, ts DESC);

ALTER TABLE imu_agg_1s SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'dev_id',
  timescaledb.compress_orderby   = 'ts'
);
SELECT add_compression_policy('imu_agg_1s', INTERVAL '6 hours');
SELECT add_retention_policy('imu_agg_1s', INTERVAL '180 days');

-- per-window features
DROP TABLE IF EXISTS imu_features CASCADE;
CREATE TABLE imu_features (
  dev_id BIGINT NOT NULL,
  seq BIGINT NOT NULL,
  ts0 TIMESTAMPTZ NOT NULL,
  a_rms_mg INT NOT NULL,
  a_peak_mg INT NOT NULL,
  tremor_hz REAL,
  fall_flag BOOLEAN NOT NULL DEFAULT FALSE,
  step_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (dev_id, ts0, seq)      -- <-- включает ts0!
);
SELECT create_hypertable('imu_features','ts0', chunk_time_interval=>INTERVAL '7 days', if_not_exists=>TRUE);
CREATE INDEX IF NOT EXISTS idx_imu_features_dev_ts0 ON imu_features (dev_id, ts0 DESC);
CREATE INDEX IF NOT EXISTS idx_imu_features_seq     ON imu_features (dev_id, seq DESC);

ALTER TABLE imu_features SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'dev_id',
  timescaledb.compress_orderby   = 'ts0, seq'
);
SELECT add_compression_policy('imu_features', INTERVAL '6 hours');
SELECT add_retention_policy('imu_features', INTERVAL '180 days');


DROP MATERIALIZED VIEW IF EXISTS cagg_imu_agg_10s;
CREATE MATERIALIZED VIEW cagg_imu_agg_10s
WITH (timescaledb.continuous) AS
SELECT
  dev_id,
  time_bucket(INTERVAL '10 seconds', ts) AS bucket,
  MAX(a_rms_mg)  AS a_rms_mg_max,
  MAX(a_peak_mg) AS a_peak_mg_max,
  SUM(steps)     AS steps
FROM imu_agg_1s
GROUP BY dev_id, bucket
WITH NO DATA;

-- 1‑минутные агрегаты для исторических графиков
DROP MATERIALIZED VIEW IF EXISTS cagg_imu_agg_1m;
CREATE MATERIALIZED VIEW cagg_imu_agg_1m
WITH (timescaledb.continuous) AS
SELECT
  dev_id,
  time_bucket(INTERVAL '1 minute', ts) AS bucket,
  MAX(a_rms_mg)  AS a_rms_mg_max,
  MAX(a_peak_mg) AS a_peak_mg_max,
  SUM(steps)     AS steps
FROM imu_agg_1s
GROUP BY dev_id, bucket
WITH NO DATA;

-- Политики рефреша непрерывных агрегатов
SELECT add_continuous_aggregate_policy(
  'cagg_imu_agg_10s',
  start_offset     => INTERVAL '1 day',
  end_offset       => INTERVAL '1 minute',
  schedule_interval=> INTERVAL '1 minute'
);
SELECT add_continuous_aggregate_policy(
  'cagg_imu_agg_1m',
  start_offset     => INTERVAL '30 days',
  end_offset       => INTERVAL '2 minutes',
  schedule_interval=> INTERVAL '5 minutes'
);

-- Чтобы сразу заполнить исторические данные после миграции:
CALL refresh_continuous_aggregate('cagg_imu_agg_10s', now() - INTERVAL '1 day', now());
CALL refresh_continuous_aggregate('cagg_imu_agg_1m',  now() - INTERVAL '30 days', now());