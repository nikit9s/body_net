/* ============================================================
 *  BodyNet IMU Telemetry Dashboard — configuration
 *  --------------------------------------------------------
 *  Frozen config objects shared across the dashboard modules.
 *
 *  Data units throughout the system:  mG  (1 mG = 0.001 g)
 * ============================================================ */

export const CONFIG = Object.freeze({
  MAX_DEVICES: 4,

  /** High-pass filter coefficient for step detection (IIR, single-pole) */
  HP_FILTER_ALPHA: 0.98,

  /** Peak threshold for step counting (mG above high-passed baseline) */
  STEP_PEAK_THRESHOLD_MG: 200,

  /** Minimum refractory period between steps (seconds) */
  STEP_REFRACTORY_S: 0.25,

  /** Heatmap auto-refresh interval (ms) */
  HEATMAP_REFRESH_MS: 15_000,

  /** Heatmap lookback window (ms) — 1 hour */
  HEATMAP_LOOKBACK_MS: 60 * 60 * 1000,

  /** How often to poll /api/devices for known device IDs (ms) */
  DEVICE_POLL_MS: 10_000,

  /** WebSocket reconnect — initial delay (ms) */
  WS_RECONNECT_INIT_MS: 1000,

  /** WebSocket reconnect — maximum delay (ms) */
  WS_RECONNECT_MAX_MS: 10_000,

  /** WebSocket reconnect — backoff multiplier */
  WS_RECONNECT_FACTOR: 1.5,

  /** Staleness timeout: device is "inactive" after this many ms without data */
  DEVICE_STALE_MS: 5000,
});

export const CHART_COLORS = Object.freeze({
  X: { line: 'rgba(239, 91, 91, 0.85)', fill: 'rgba(239, 91, 91, 0.06)' },
  Y: { line: 'rgba(45, 212, 160, 0.85)', fill: 'rgba(45, 212, 160, 0.06)' },
  Z: { line: 'rgba(91, 157, 239, 0.85)', fill: 'rgba(91, 157, 239, 0.06)' },
  MAG: { line: 'rgba(210, 210, 230, 0.70)', fill: 'rgba(210, 210, 230, 0.04)' },
});

export const CHART_STYLE = Object.freeze({
  gridColor: 'rgba(42, 47, 66, 0.5)',
  tickColor: '#717694',
  tickFont: { family: "'JetBrains Mono', monospace", size: 10 },
  tooltipBg: '#222738',
  tooltipTitle: '#d8dbe8',
  tooltipBody: '#717694',
  tooltipBorder: '#3d4460',
});

export const AGG_COLORS = Object.freeze({
  rms:   { line: 'rgba(91, 106, 239, 0.9)',  fill: 'rgba(91, 106, 239, 0.10)' },
  peak:  { line: 'rgba(239, 91, 91, 0.7)',   fill: 'rgba(239, 91, 91, 0.05)' },
  steps: { line: 'rgba(45, 212, 160, 0.85)', fill: 'rgba(45, 212, 160, 0.08)' },
});

/**
 * Lookback window shown for each aggregate resolution — wider buckets need a
 * longer window, otherwise the chart would only ever show a couple of points.
 */
export const AGG_LOOKBACK_MS = Object.freeze({
  '10s': 60 * 60 * 1000,               // 1 hour
  '1m':  6 * 60 * 60 * 1000,           // 6 hours
  '5m':  24 * 60 * 60 * 1000,          // 1 day
  '10m': 2 * 24 * 60 * 60 * 1000,      // 2 days
  '15m': 3 * 24 * 60 * 60 * 1000,      // 3 days
  '30m': 7 * 24 * 60 * 60 * 1000,      // 7 days
});

export const AGG_LOOKBACK_LABEL = Object.freeze({
  '10s': 'Last Hour',
  '1m':  'Last 6 Hours',
  '5m':  'Last 24 Hours',
  '10m': 'Last 2 Days',
  '15m': 'Last 3 Days',
  '30m': 'Last 7 Days',
});

export const DATASET_INDEX = Object.freeze({ X: 0, Y: 1, Z: 2, MAG: 3 });
