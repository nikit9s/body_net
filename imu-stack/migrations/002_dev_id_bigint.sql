-- Migration: dev_id INT → BIGINT
--
-- Firmware uses uint32 for dev_id (up to 4 294 967 295),
-- which exceeds PostgreSQL INT range (max 2 147 483 647).
--
-- Run this on an existing database:
--   docker compose exec db psql -U postgres -d imu -f /migrations/002_dev_id_bigint.sql

-- Drop continuous aggregates first (they depend on imu_agg_1s)
DROP MATERIALIZED VIEW IF EXISTS cagg_imu_agg_1m CASCADE;
DROP MATERIALIZED VIEW IF EXISTS cagg_imu_agg_10s CASCADE;

-- Alter base tables
ALTER TABLE imu_windows  ALTER COLUMN dev_id TYPE BIGINT;
ALTER TABLE imu_agg_1s   ALTER COLUMN dev_id TYPE BIGINT;
ALTER TABLE imu_features  ALTER COLUMN dev_id TYPE BIGINT;

-- Recreate continuous aggregates
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

CALL refresh_continuous_aggregate('cagg_imu_agg_10s', now() - INTERVAL '1 day', now());
CALL refresh_continuous_aggregate('cagg_imu_agg_1m',  now() - INTERVAL '30 days', now());
