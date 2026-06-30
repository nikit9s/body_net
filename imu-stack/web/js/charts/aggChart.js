/* ============================================================
 *  Aggregate Chart.js chart (RMS / Peak / Steps over time).
 *  Chart is a global provided by the Chart.js CDN <script>.
 * ============================================================ */

import { AGG_COLORS, CHART_STYLE } from '../config.js';

/**
 * Create the aggregate activity chart.
 * @param {HTMLCanvasElement} canvas - target canvas element
 * @returns {Chart} the Chart.js instance
 */
export function createAggChart(canvas) {
  const ctx = canvas.getContext('2d');
  return new Chart(ctx, {
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

/**
 * Format an ISO timestamp as a short ru-RU HH:MM label.
 * @param {string} isoStr - ISO timestamp
 * @returns {string} formatted time, or '' on failure
 */
export function formatTime(isoStr) {
  try {
    return new Date(isoStr).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

/**
 * Populate the aggregate chart with fetched aggregate rows.
 * @param {Chart} aggChart - the Chart.js instance
 * @param {Array<object>} data - aggregate rows from the API
 */
export function updateAggChart(aggChart, data) {
  const labels = data.map(d => formatTime(d.ts));
  aggChart.data.labels = labels;
  aggChart.data.datasets[0].data = data.map(d => d.a_rms_mg_max || 0);
  aggChart.data.datasets[1].data = data.map(d => d.a_peak_mg_max || 0);
  aggChart.data.datasets[2].data = data.map(d => d.steps || 0);
  aggChart.update('none');
}
