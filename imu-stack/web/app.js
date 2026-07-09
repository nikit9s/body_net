/* ============================================================
 *  BodyNet IMU Telemetry Dashboard
 *  --------------------------------------------------------
 *  Real-time visualisation for up to MAX_DEVICES BMA423-based
 *  IMU nodes connected via BLE → Bridge → WebSocket pipeline.
 *
 *  Data units throughout the system:  mG  (1 mG = 0.001 g)
 * ============================================================ */

// --------------- Configuration ---------------

const CONFIG = Object.freeze({
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

const CHART_COLORS = Object.freeze({
  X: { line: 'rgba(239, 91, 91, 0.85)', fill: 'rgba(239, 91, 91, 0.06)' },
  Y: { line: 'rgba(45, 212, 160, 0.85)', fill: 'rgba(45, 212, 160, 0.06)' },
  Z: { line: 'rgba(91, 157, 239, 0.85)', fill: 'rgba(91, 157, 239, 0.06)' },
  MAG: { line: 'rgba(210, 210, 230, 0.70)', fill: 'rgba(210, 210, 230, 0.04)' },
});

const CHART_STYLE = Object.freeze({
  gridColor: 'rgba(42, 47, 66, 0.5)',
  tickColor: '#717694',
  tickFont: { family: "'JetBrains Mono', monospace", size: 10 },
  tooltipBg: '#222738',
  tooltipTitle: '#d8dbe8',
  tooltipBody: '#717694',
  tooltipBorder: '#3d4460',
});

// --------------- DOM refs ---------------

const wsStatusEl = document.getElementById('ws-status');
const wsStatusText = document.getElementById('ws-status-text');
const activeCountEl = document.getElementById('active-count');
const totalFramesEl = document.getElementById('total-frames');
const aggDevSelect = document.getElementById('hm-dev');
const aggResSelect = document.getElementById('agg-resolution');
const aggTitleEl = document.getElementById('agg-title');
const aggCanvasEl = document.getElementById('agg-chart');
const tooltipEl = document.getElementById('tooltip');
const deviceGridEl = document.getElementById('device-grid');
const exportSinceEl = document.getElementById('export-since');
const exportUntilEl = document.getElementById('export-until');
const exportResolutionEl = document.getElementById('export-resolution');
const exportDevicesEl = document.getElementById('export-devices');
const exportBtnEl = document.getElementById('export-btn');

// --------------- Per-device state ---------------

const devices = new Map();
const knownDeviceIds = new Set();

let totalFrameCount = 0;

function shortDevId(devId) {
  const hex = (devId >>> 0).toString(16).toUpperCase();
  return hex.length > 4 ? '\u2026' + hex.slice(-4) : hex;
}

// --------------- Friendly device labels (persisted per-browser) ---------------
// dev_id is a firmware-time constant (DEV_ID, see twatch_imu_ble.ino) \u2014 the same
// number is shown on the watch's own screen. These labels are just a local
// display alias on top of it (e.g. "Alice \u2014 left wrist") so panels/dropdowns
// don't only show raw numbers.

const DEVICE_LABELS_KEY = 'bodynet_device_labels';

function loadDeviceLabels() {
  try {
    return JSON.parse(localStorage.getItem(DEVICE_LABELS_KEY) || '{}');
  } catch {
    return {};
  }
}

const deviceLabels = loadDeviceLabels();

function getDeviceLabel(devId) {
  return deviceLabels[devId] || `Device ${shortDevId(devId)}`;
}

function setDeviceLabel(devId, name) {
  const trimmed = (name || '').trim();
  if (trimmed) {
    deviceLabels[devId] = trimmed;
  } else {
    delete deviceLabels[devId];
  }
  localStorage.setItem(DEVICE_LABELS_KEY, JSON.stringify(deviceLabels));
  refreshAllDeviceLabels();
}

function promptRenameDevice(devId) {
  const name = prompt(`Label for device ${devId} (leave empty to reset):`, deviceLabels[devId] || '');
  if (name !== null) setDeviceLabel(devId, name);
}

function refreshAllDeviceLabels() {
  for (const state of devices.values()) {
    if (state.els.label) state.els.label.textContent = getDeviceLabel(state.id);
  }
  for (const opt of aggDevSelect.options) {
    if (!opt.value) continue;
    opt.textContent = `${getDeviceLabel(Number(opt.value))} (${opt.value})`;
  }
  exportDevicesEl.querySelectorAll('label').forEach(lbl => {
    const input = lbl.querySelector('input[type="checkbox"]');
    const text = lbl.querySelector('[data-el="text"]');
    if (input && text) text.textContent = getDeviceLabel(Number(input.value));
  });
}

function getOrCreateDevice(devId) {
  if (devices.has(devId)) return devices.get(devId);
  if (devices.size >= CONFIG.MAX_DEVICES) return null;

  const state = {
    id: devId,
    lastSeq: 0,
    lastBatt: null,
    lastFs: null,
    lastPeak: null,
    lastSteps: null,
    lastUpdateMs: 0,
    frameCount: 0,
    showAxes: { x: false, y: false, z: false, mag: true },
    chart: null,
    els: {},
  };

  buildDevicePanel(state);
  devices.set(devId, state);
  registerDeviceId(devId);
  updateActiveCount();
  return state;
}

// --------------- Device ID registry (for dropdown) ---------------

function registerDeviceId(devId) {
  if (knownDeviceIds.has(devId)) return;
  knownDeviceIds.add(devId);
  addDeviceOption(devId);
}

function addDeviceOption(devId) {
  const existing = aggDevSelect.querySelector(`option[value="${devId}"]`);
  if (existing) return;

  const opt = document.createElement('option');
  opt.value = devId;
  opt.textContent = `${getDeviceLabel(devId)} (${devId})`;
  aggDevSelect.appendChild(opt);

  if (aggDevSelect.options.length === 2) {
    aggDevSelect.selectedIndex = 1;
    refreshAggChart();
  }

  addExportDeviceCheckbox(devId);
}

function addExportDeviceCheckbox(devId) {
  if (exportDevicesEl.querySelector(`input[value="${devId}"]`)) return;
  if (exportDevicesEl.querySelector('.header-stat')) exportDevicesEl.innerHTML = '';

  const label = document.createElement('label');
  label.innerHTML = `<input type="checkbox" value="${devId}" checked /><span data-el="text"></span>`;
  label.querySelector('[data-el="text"]').textContent = getDeviceLabel(devId);
  exportDevicesEl.appendChild(label);
}

async function fetchKnownDevices() {
  try {
    const res = await fetch('/api/devices');
    if (!res.ok) return;
    const ids = await res.json();
    for (const id of ids) {
      registerDeviceId(id);
    }
  } catch {
    // API might not be ready yet
  }
}

// --------------- DOM construction ---------------

function buildDevicePanel(state) {
  const panel = document.createElement('div');
  panel.className = 'device-panel';
  panel.id = `panel-${state.id}`;
  const label = shortDevId(state.id);
  panel.innerHTML = `
    <div class="panel-header">
      <div class="panel-id">
        <div class="dev-indicator"></div>
        <span class="dev-label" data-el="label" title="Double-click to rename">Device ${label}</span>
        <span class="dev-sublabel" data-el="status">waiting</span>
      </div>
      <div class="panel-metrics">
        <div class="panel-metric">
          <div class="pm-label">Batt</div>
          <div class="pm-value"><span data-el="batt">&mdash;</span><span class="pm-unit"> %</span></div>
        </div>
        <div class="panel-metric">
          <div class="pm-label">f<sub>s</sub></div>
          <div class="pm-value"><span data-el="fs">&mdash;</span><span class="pm-unit"> Hz</span></div>
        </div>
        <div class="panel-metric">
          <div class="pm-label">Peak |a|</div>
          <div class="pm-value"><span data-el="peak">&mdash;</span><span class="pm-unit"> mG</span></div>
        </div>
        <div class="panel-metric">
          <div class="pm-label">Steps</div>
          <div class="pm-value"><span data-el="steps">&mdash;</span></div>
        </div>
      </div>
    </div>
    <div class="panel-body">
      <div class="chart-labels">
        <span class="chart-label-left">|a| (mG) vs sample index</span>
        <div class="chart-legend">
          <div class="legend-item" data-axis="x">
            <span class="swatch" style="background:${CHART_COLORS.X.line}"></span>a<sub>x</sub>
          </div>
          <div class="legend-item" data-axis="y">
            <span class="swatch" style="background:${CHART_COLORS.Y.line}"></span>a<sub>y</sub>
          </div>
          <div class="legend-item" data-axis="z">
            <span class="swatch" style="background:${CHART_COLORS.Z.line}"></span>a<sub>z</sub>
          </div>
          <div class="legend-item" data-axis="mag">
            <span class="swatch" style="background:${CHART_COLORS.MAG.line}"></span>|a|
          </div>
        </div>
      </div>
      <div class="chart-wrap">
        <canvas data-el="canvas"></canvas>
      </div>
    </div>
    <div class="panel-footer">
      <span class="pf-item">id: <strong>${state.id}</strong></span>
      <span class="pf-item">seq: <strong data-el="seq">&mdash;</strong></span>
      <span class="pf-item">n: <strong data-el="n">&mdash;</strong></span>
      <span class="pf-item">frames: <strong data-el="frames">0</strong></span>
    </div>
  `;

  deviceGridEl.appendChild(panel);

  state.els.panel = panel;
  panel.querySelectorAll('[data-el]').forEach(el => {
    state.els[el.dataset.el] = el;
  });

  state.els.label.textContent = getDeviceLabel(state.id);
  state.els.label.addEventListener('dblclick', () => promptRenameDevice(state.id));

  initLegendToggles(state, panel);
  state.chart = createChart(state.els.canvas, state.showAxes);
}

function initLegendToggles(state, panel) {
  const items = panel.querySelectorAll('.legend-item');
  items.forEach(item => {
    const axis = item.dataset.axis;
    if (!state.showAxes[axis]) {
      item.classList.add('off');
    }
    item.addEventListener('click', () => {
      state.showAxes[axis] = !state.showAxes[axis];
      item.classList.toggle('off', !state.showAxes[axis]);
      updateChartVisibility(state);
    });
  });
}

// --------------- Chart.js setup ---------------

const DATASET_INDEX = Object.freeze({ X: 0, Y: 1, Z: 2, MAG: 3 });

function createChart(canvas, showAxes) {
  const ctx = canvas.getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        makeDataset('a_x', CHART_COLORS.X, showAxes.x),
        makeDataset('a_y', CHART_COLORS.Y, showAxes.y),
        makeDataset('a_z', CHART_COLORS.Z, showAxes.z),
        makeDataset('|a|', CHART_COLORS.MAG, showAxes.mag),
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: CHART_STYLE.tooltipBg,
          titleColor: CHART_STYLE.tooltipTitle,
          bodyColor: CHART_STYLE.tooltipBody,
          borderColor: CHART_STYLE.tooltipBorder,
          borderWidth: 1,
          cornerRadius: 4,
          padding: 8,
          titleFont: { family: CHART_STYLE.tickFont.family, size: 11 },
          bodyFont: { family: CHART_STYLE.tickFont.family, size: 11 },
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} mG`,
          },
        },
      },
      scales: {
        x: {
          display: true,
          grid: { color: CHART_STYLE.gridColor, lineWidth: 1 },
          ticks: {
            color: CHART_STYLE.tickColor,
            font: CHART_STYLE.tickFont,
            maxTicksLimit: 6,
          },
          title: {
            display: true,
            text: 'sample',
            color: CHART_STYLE.tickColor,
            font: { ...CHART_STYLE.tickFont, size: 10 },
          },
        },
        y: {
          grid: { color: CHART_STYLE.gridColor, lineWidth: 1 },
          ticks: {
            color: CHART_STYLE.tickColor,
            font: CHART_STYLE.tickFont,
          },
          title: {
            display: true,
            text: 'mG',
            color: CHART_STYLE.tickColor,
            font: { ...CHART_STYLE.tickFont, size: 10 },
          },
          min: -10000,
          max: 10000,
        },
      },
    },
  });
}

function makeDataset(label, colors, visible) {
  return {
    label,
    data: [],
    borderColor: colors.line,
    backgroundColor: colors.fill,
    borderWidth: 1.2,
    pointRadius: 0,
    fill: true,
    tension: 0.15,
    hidden: !visible,
  };
}

function updateChartVisibility(state) {
  const ds = state.chart.data.datasets;
  ds[DATASET_INDEX.X].hidden = !state.showAxes.x;
  ds[DATASET_INDEX.Y].hidden = !state.showAxes.y;
  ds[DATASET_INDEX.Z].hidden = !state.showAxes.z;
  ds[DATASET_INDEX.MAG].hidden = !state.showAxes.mag;
  state.chart.update('none');
}

// --------------- Signal processing ---------------

function magnitude(ax, ay, az) {
  return Math.sqrt(ax * ax + ay * ay + az * az);
}

function highPassIIR(x, alpha) {
  const n = x.length;
  const y = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    y[i] = alpha * (y[i - 1] + x[i] - x[i - 1]);
  }
  return y;
}

function countSteps(filteredMagnitude, sampleRateHz) {
  const minSamplesBetweenSteps = Math.max(
    1,
    Math.floor(CONFIG.STEP_REFRACTORY_S * sampleRateHz)
  );
  let count = 0;
  let lastPeakIndex = -minSamplesBetweenSteps;

  for (let i = 1; i < filteredMagnitude.length - 1; i++) {
    const isPeak =
      filteredMagnitude[i] > CONFIG.STEP_PEAK_THRESHOLD_MG &&
      filteredMagnitude[i] >= filteredMagnitude[i - 1] &&
      filteredMagnitude[i] >= filteredMagnitude[i + 1];

    if (isPeak && (i - lastPeakIndex) >= minSamplesBetweenSteps) {
      count++;
      lastPeakIndex = i;
    }
  }
  return count;
}

// --------------- Frame handler ---------------

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

  updateDeviceDOM(state, meta, n);
  updateChart(state, ax, ay, az, magnitudes, n);
}

function updateDeviceDOM(state, meta, n) {
  const { els } = state;
  els.panel.classList.add('active');
  els.status.textContent = 'streaming';
  els.batt.textContent = state.lastBatt ?? '\u2014';
  els.fs.textContent = state.lastFs ?? '\u2014';
  els.peak.textContent = state.lastPeak != null ? state.lastPeak.toLocaleString() : '\u2014';
  els.steps.textContent = state.lastSteps ?? '\u2014';
  els.seq.textContent = meta.seq;
  els.n.textContent = n;
  els.frames.textContent = state.frameCount;
  totalFramesEl.textContent = totalFrameCount;
  updateActiveCount();
}

function updateChart(state, ax, ay, az, magnitudes, n) {
  const chart = state.chart;
  const labels = Array.from({ length: n }, (_, i) => i);

  chart.data.labels = labels;
  chart.data.datasets[DATASET_INDEX.X].data = ax;
  chart.data.datasets[DATASET_INDEX.Y].data = ay;
  chart.data.datasets[DATASET_INDEX.Z].data = az;
  chart.data.datasets[DATASET_INDEX.MAG].data = Array.from(magnitudes);
  chart.update('none');
}

// --------------- Staleness checker ---------------

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

function updateActiveCount() {
  const now = performance.now();
  let active = 0;
  for (const state of devices.values()) {
    if (
      state.lastUpdateMs > 0 &&
      now - state.lastUpdateMs <= CONFIG.DEVICE_STALE_MS
    ) {
      active++;
    }
  }
  activeCountEl.textContent = active;
}

setInterval(checkStaleness, 1000);

// --------------- Aggregate chart ---------------

const AGG_COLORS = Object.freeze({
  rms:   { line: 'rgba(91, 106, 239, 0.9)',  fill: 'rgba(91, 106, 239, 0.10)' },
  peak:  { line: 'rgba(239, 91, 91, 0.7)',   fill: 'rgba(239, 91, 91, 0.05)' },
  steps: { line: 'rgba(45, 212, 160, 0.85)', fill: 'rgba(45, 212, 160, 0.08)' },
});

// Lookback window shown for each aggregate resolution — wider buckets need a
// longer window, otherwise the chart would only ever show a couple of points.
const AGG_LOOKBACK_MS = Object.freeze({
  '10s': 60 * 60 * 1000,               // 1 hour
  '1m':  6 * 60 * 60 * 1000,           // 6 hours
  '5m':  24 * 60 * 60 * 1000,          // 1 day
  '10m': 2 * 24 * 60 * 60 * 1000,      // 2 days
  '15m': 3 * 24 * 60 * 60 * 1000,      // 3 days
  '30m': 7 * 24 * 60 * 60 * 1000,      // 7 days
});

const AGG_LOOKBACK_LABEL = Object.freeze({
  '10s': 'Last Hour',
  '1m':  'Last 6 Hours',
  '5m':  'Last 24 Hours',
  '10m': 'Last 2 Days',
  '15m': 'Last 3 Days',
  '30m': 'Last 7 Days',
});

let aggChart = null;

function createAggChart() {
  const ctx = aggCanvasEl.getContext('2d');
  aggChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        {
          label: 'RMS (mG)',
          data: [],
          borderColor: AGG_COLORS.rms.line,
          backgroundColor: AGG_COLORS.rms.fill,
          borderWidth: 1.5,
          pointRadius: 0,
          fill: true,
          tension: 0.2,
          yAxisID: 'y',
        },
        {
          label: 'Peak (mG)',
          data: [],
          borderColor: AGG_COLORS.peak.line,
          backgroundColor: AGG_COLORS.peak.fill,
          borderWidth: 1,
          pointRadius: 0,
          fill: false,
          tension: 0.2,
          borderDash: [4, 3],
          yAxisID: 'y',
        },
        {
          label: 'Steps',
          data: [],
          borderColor: AGG_COLORS.steps.line,
          backgroundColor: AGG_COLORS.steps.fill,
          borderWidth: 1.5,
          pointRadius: 0,
          fill: true,
          tension: 0.1,
          yAxisID: 'y2',
        },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          align: 'end',
          labels: {
            color: CHART_STYLE.tickColor,
            font: { ...CHART_STYLE.tickFont, size: 10 },
            boxWidth: 12,
            boxHeight: 2,
            padding: 12,
            usePointStyle: false,
          },
        },
        tooltip: {
          backgroundColor: CHART_STYLE.tooltipBg,
          titleColor: CHART_STYLE.tooltipTitle,
          bodyColor: CHART_STYLE.tooltipBody,
          borderColor: CHART_STYLE.tooltipBorder,
          borderWidth: 1,
          cornerRadius: 4,
          padding: 8,
          titleFont: { family: CHART_STYLE.tickFont.family, size: 11 },
          bodyFont: { family: CHART_STYLE.tickFont.family, size: 11 },
        },
      },
      scales: {
        x: {
          display: true,
          grid: { color: CHART_STYLE.gridColor, lineWidth: 1 },
          ticks: {
            color: CHART_STYLE.tickColor,
            font: CHART_STYLE.tickFont,
            maxTicksLimit: 10,
            maxRotation: 0,
          },
        },
        y: {
          position: 'left',
          grid: { color: CHART_STYLE.gridColor, lineWidth: 1 },
          ticks: { color: CHART_STYLE.tickColor, font: CHART_STYLE.tickFont },
          title: {
            display: true,
            text: 'mG',
            color: CHART_STYLE.tickColor,
            font: { ...CHART_STYLE.tickFont, size: 10 },
          },
          min: 0,
          max: 10000,
        },
        y2: {
          position: 'right',
          grid: { drawOnChartArea: false },
          ticks: { color: AGG_COLORS.steps.line, font: CHART_STYLE.tickFont },
          title: {
            display: true,
            text: 'steps',
            color: AGG_COLORS.steps.line,
            font: { ...CHART_STYLE.tickFont, size: 10 },
          },
          beginAtZero: true,
        },
      },
    },
  });
}

function formatTime(isoStr) {
  try {
    return new Date(isoStr).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function getSelectedAggDevId() {
  const val = aggDevSelect.value;
  if (!val) return null;
  const parsed = Number(val);
  return Number.isFinite(parsed) ? parsed : null;
}

async function refreshAggChart() {
  const devId = getSelectedAggDevId();
  if (devId == null) return;

  const resolution = aggResSelect.value;
  const lookbackMs = AGG_LOOKBACK_MS[resolution] ?? CONFIG.HEATMAP_LOOKBACK_MS;
  const since = new Date(Date.now() - lookbackMs).toISOString();
  aggTitleEl.textContent = `Aggregate Activity — ${AGG_LOOKBACK_LABEL[resolution] ?? 'Last Hour'}`;

  try {
    const res = await fetch(
      `/api/agg/${resolution}?dev_id=${devId}&since=${encodeURIComponent(since)}`
    );
    if (!res.ok) return;
    const data = await res.json();

    if (!aggChart) createAggChart();

    const labels = data.map(d => formatTime(d.ts));
    aggChart.data.labels = labels;
    aggChart.data.datasets[0].data = data.map(d => d.a_rms_mg_max || 0);
    aggChart.data.datasets[1].data = data.map(d => d.a_peak_mg_max || 0);
    aggChart.data.datasets[2].data = data.map(d => d.steps || 0);
    aggChart.update('none');
  } catch (err) {
    console.error('Aggregate chart load error:', err);
  }
}

// --------------- Excel export ---------------

function toDatetimeLocalValue(date) {
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function initExportDefaults() {
  const now = new Date();
  const hourAgo = new Date(now.getTime() - 60 * 60 * 1000);
  exportUntilEl.value = toDatetimeLocalValue(now);
  exportSinceEl.value = toDatetimeLocalValue(hourAgo);
}

async function handleExportClick() {
  const sinceVal = exportSinceEl.value;
  const untilVal = exportUntilEl.value;
  if (!sinceVal || !untilVal) {
    alert('Укажите обе даты диапазона.');
    return;
  }

  const since = new Date(sinceVal);
  const until = new Date(untilVal);
  if (since >= until) {
    alert('Дата "From" должна быть раньше даты "To".');
    return;
  }

  const resolution = exportResolutionEl.value;
  const checkedDevIds = Array.from(
    exportDevicesEl.querySelectorAll('input[type="checkbox"]:checked')
  ).map(cb => cb.value);

  const params = new URLSearchParams({
    since: since.toISOString(),
    until: until.toISOString(),
    resolution,
  });
  if (checkedDevIds.length > 0) {
    params.set('dev_ids', checkedDevIds.join(','));
  }

  exportBtnEl.disabled = true;
  exportBtnEl.textContent = 'Экспорт…';
  try {
    const res = await fetch(`/api/export/xlsx?${params.toString()}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert(`Ошибка экспорта: ${body.detail || res.status}`);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `imu_export_${resolution}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    console.error('Export error:', err);
    alert('Не удалось скачать файл экспорта.');
  } finally {
    exportBtnEl.disabled = false;
    exportBtnEl.textContent = 'Скачать .xlsx';
  }
}

// --------------- WebSocket ---------------

let reconnectDelay = CONFIG.WS_RECONNECT_INIT_MS;

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${location.host}/ws/json`;

  wsStatusEl.classList.remove('connected');
  wsStatusText.textContent = 'Connecting\u2026';

  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('WebSocket creation error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    wsStatusEl.classList.add('connected');
    wsStatusText.textContent = 'Live';
    reconnectDelay = CONFIG.WS_RECONNECT_INIT_MS;
  };

  ws.onmessage = ev => {
    try {
      handleFrame(JSON.parse(ev.data));
    } catch (e) {
      console.error('Frame parse error:', e);
    }
  };

  ws.onerror = e => {
    console.error('WebSocket error:', e);
  };

  ws.onclose = () => {
    wsStatusEl.classList.remove('connected');
    wsStatusText.textContent = 'Disconnected';
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  setTimeout(connectWebSocket, reconnectDelay);
  reconnectDelay = Math.min(
    reconnectDelay * CONFIG.WS_RECONNECT_FACTOR,
    CONFIG.WS_RECONNECT_MAX_MS
  );
}

// --------------- Init ---------------

const isFileProtocol = location.protocol === 'file:';

initExportDefaults();
exportBtnEl.addEventListener('click', handleExportClick);

if (isFileProtocol) {
  wsStatusText.textContent = 'Open via http://localhost:8080';
  hmEl.innerHTML =
    '<div class="empty-state">Run <b>make up</b> and open <b>http://localhost:8080</b></div>';
} else {
  connectWebSocket();

  fetchKnownDevices();
  setInterval(fetchKnownDevices, CONFIG.DEVICE_POLL_MS);

  aggDevSelect.addEventListener('change', refreshAggChart);
  aggResSelect.addEventListener('change', refreshAggChart);
  setInterval(refreshAggChart, CONFIG.HEATMAP_REFRESH_MS);
}
