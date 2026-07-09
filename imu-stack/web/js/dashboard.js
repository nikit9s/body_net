/* ============================================================
 *  Dashboard orchestration — frame handling, staleness checks,
 *  aggregate refresh, and init() wiring.
 * ============================================================ */

import { CONFIG, AGG_LOOKBACK_MS, AGG_LOOKBACK_LABEL } from './config.js';
import {
  wsStatusText,
  aggDevSelect,
  aggResSelect,
  aggTitleEl,
  aggCanvasEl,
} from './dom.js';
import { magnitude, highPassIIR, countSteps } from './dsp.js';
import { fetchKnownDevices, fetchAgg } from './api.js';
import { connectWebSocket } from './ws.js';
import {
  devices,
  getOrCreateDevice,
  registerDeviceId,
  updateActiveCount,
  setOnFirstDeviceSelected,
} from './deviceRegistry.js';
import { updateDeviceDOM, updateChart } from './devicePanel.js';
import { createAggChart, updateAggChart } from './charts/aggChart.js';
import { initExportPanel } from './export.js';

let totalFrameCount = 0;
let aggChart = null;

/**
 * Handle a single parsed WebSocket frame.
 * @param {object} msg - parsed frame ({type, meta, ax, ay, az})
 */
function handleFrame(msg) {
  if (msg.type !== 'imu') return;

  const { meta, ax, ay, az } = msg;
  const devId = meta.dev_id;

  const state = getOrCreateDevice(devId);
  if (!state) return;

  state.lastSeq = meta.seq;
  state.lastBatt = meta.batt;
  state.lastFs = meta.fs_hz;
  state.lastUpdateMs = performance.now();
  state.frameCount++;
  totalFrameCount++;

  const n = ax.length;
  const magnitudes = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    magnitudes[i] = magnitude(ax[i], ay[i], az[i]);
  }

  const filtered = highPassIIR(magnitudes, CONFIG.HP_FILTER_ALPHA);
  const steps = countSteps(filtered, meta.fs_hz);
  const peak = Math.max(...magnitudes);

  state.lastPeak = Math.floor(peak);
  state.lastSteps = steps;

  updateDeviceDOM(state, meta, n, totalFrameCount, updateActiveCount);
  updateChart(state, ax, ay, az, magnitudes, n);
}

/**
 * Mark devices stale if they have not produced data within DEVICE_STALE_MS.
 */
function checkStaleness() {
  const now = performance.now();
  for (const state of devices.values()) {
    if (
      state.lastUpdateMs > 0 &&
      now - state.lastUpdateMs > CONFIG.DEVICE_STALE_MS
    ) {
      state.els.panel.classList.remove('active');
      state.els.status.textContent = 'stale';
    }
  }
  updateActiveCount();
}

/**
 * Get the currently selected aggregate-chart device ID, or null.
 * @returns {number|null}
 */
function getSelectedAggDevId() {
  const val = aggDevSelect.value;
  if (!val) return null;
  const parsed = Number(val);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Fetch and render the aggregate chart for the selected device/resolution.
 */
async function refreshAggChart() {
  const devId = getSelectedAggDevId();
  if (devId == null) return;

  const resolution = aggResSelect.value;
  const lookbackMs = AGG_LOOKBACK_MS[resolution] ?? CONFIG.HEATMAP_LOOKBACK_MS;
  const since = new Date(Date.now() - lookbackMs).toISOString();
  aggTitleEl.textContent = `Aggregate Activity — ${AGG_LOOKBACK_LABEL[resolution] ?? 'Last Hour'}`;

  const data = await fetchAgg(devId, resolution, since);
  if (data == null) return;

  if (!aggChart) aggChart = createAggChart(aggCanvasEl);

  updateAggChart(aggChart, data);
}

/**
 * Initialise the dashboard: intervals, WebSocket, and event listeners.
 */
export function init() {
  setOnFirstDeviceSelected(refreshAggChart);
  initExportPanel();
  setInterval(checkStaleness, 1000);

  const isFileProtocol = location.protocol === 'file:';

  if (isFileProtocol) {
    wsStatusText.textContent = 'Open via http://localhost:8080';
  } else {
    connectWebSocket(handleFrame);

    fetchKnownDevices().then(ids => {
      for (const id of ids) {
        registerDeviceId(id);
      }
    });
    setInterval(() => {
      fetchKnownDevices().then(ids => {
        for (const id of ids) {
          registerDeviceId(id);
        }
      });
    }, CONFIG.DEVICE_POLL_MS);

    aggDevSelect.addEventListener('change', refreshAggChart);
    aggResSelect.addEventListener('change', refreshAggChart);
    setInterval(refreshAggChart, CONFIG.HEATMAP_REFRESH_MS);
  }
}
