/* ============================================================
 *  Per-device live Chart.js line chart (ax/ay/az/|a|).
 *  Chart is a global provided by the Chart.js CDN <script>.
 * ============================================================ */

import { CHART_COLORS, CHART_STYLE, DATASET_INDEX } from '../config.js';

/**
 * Create a per-device live line chart.
 * @param {HTMLCanvasElement} canvas - target canvas element
 * @param {{x:boolean,y:boolean,z:boolean,mag:boolean}} showAxes - initial visibility
 * @returns {Chart} the Chart.js instance
 */
export function createChart(canvas, showAxes) {
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

/**
 * Build a single chart dataset config.
 * @param {string} label - dataset label
 * @param {{line:string,fill:string}} colors - line/fill colors
 * @param {boolean} visible - whether the dataset starts visible
 * @returns {object} Chart.js dataset config
 */
export function makeDataset(label, colors, visible) {
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

/**
 * Sync dataset visibility from a device state's showAxes flags.
 * @param {object} state - device state (with .chart and .showAxes)
 */
export function updateChartVisibility(state) {
  const ds = state.chart.data.datasets;
  ds[DATASET_INDEX.X].hidden = !state.showAxes.x;
  ds[DATASET_INDEX.Y].hidden = !state.showAxes.y;
  ds[DATASET_INDEX.Z].hidden = !state.showAxes.z;
  ds[DATASET_INDEX.MAG].hidden = !state.showAxes.mag;
  state.chart.update('none');
}
