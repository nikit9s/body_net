/* ============================================================
 *  Per-device panel — DOM construction, legend toggles, and
 *  the DOM/chart update wiring for a single device.
 * ============================================================ */

import { CHART_COLORS, DATASET_INDEX } from './config.js';
import { deviceGridEl, totalFramesEl } from './dom.js';
import { shortDevId } from './deviceRegistry.js';
import { createChart, updateChartVisibility } from './charts/liveChart.js';

/**
 * Build the DOM panel for a device and attach its chart + element refs.
 * @param {object} state - device state (mutated: state.els, state.chart)
 */
export function buildDevicePanel(state) {
  const panel = document.createElement('div');
  panel.className = 'device-panel';
  panel.id = `panel-${state.id}`;
  const label = shortDevId(state.id);
  panel.innerHTML = `
    <div class="panel-header">
      <div class="panel-id">
        <div class="dev-indicator"></div>
        <span class="dev-label">Device ${label}</span>
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

  initLegendToggles(state, panel);
  state.chart = createChart(state.els.canvas, state.showAxes);
}

/**
 * Wire up the per-axis legend toggle buttons for a panel.
 * @param {object} state - device state
 * @param {HTMLElement} panel - the panel element
 */
export function initLegendToggles(state, panel) {
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

/**
 * Update a device panel's metric DOM from the latest frame.
 * @param {object} state - device state
 * @param {object} meta - frame meta
 * @param {number} n - sample count in this frame
 * @param {number} totalFrameCount - total frames received across all devices
 * @param {() => void} updateActiveCount - active-count refresh callback
 */
export function updateDeviceDOM(state, meta, n, totalFrameCount, updateActiveCount) {
  const { els } = state;
  els.panel.classList.add('active');
  els.status.textContent = 'streaming';
  els.batt.textContent = state.lastBatt ?? '—';
  els.fs.textContent = state.lastFs ?? '—';
  els.peak.textContent = state.lastPeak != null ? state.lastPeak.toLocaleString() : '—';
  els.steps.textContent = state.lastSteps ?? '—';
  els.seq.textContent = meta.seq;
  els.n.textContent = n;
  els.frames.textContent = state.frameCount;
  totalFramesEl.textContent = totalFrameCount;
  updateActiveCount();
}

/**
 * Push the latest frame's series into the device chart.
 * @param {object} state - device state
 * @param {ArrayLike<number>} ax
 * @param {ArrayLike<number>} ay
 * @param {ArrayLike<number>} az
 * @param {Float64Array} magnitudes
 * @param {number} n - sample count
 */
export function updateChart(state, ax, ay, az, magnitudes, n) {
  const chart = state.chart;
  const labels = Array.from({ length: n }, (_, i) => i);

  chart.data.labels = labels;
  chart.data.datasets[DATASET_INDEX.X].data = ax;
  chart.data.datasets[DATASET_INDEX.Y].data = ay;
  chart.data.datasets[DATASET_INDEX.Z].data = az;
  chart.data.datasets[DATASET_INDEX.MAG].data = Array.from(magnitudes);
  chart.update('none');
}
