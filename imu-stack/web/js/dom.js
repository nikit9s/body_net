/* ============================================================
 *  DOM element references — queried once at module load.
 * ============================================================ */

/** Top-level DOM refs used across the dashboard. */
export const wsStatusEl = document.getElementById('ws-status');
export const wsStatusText = document.getElementById('ws-status-text');
export const activeCountEl = document.getElementById('active-count');
export const totalFramesEl = document.getElementById('total-frames');
export const aggDevSelect = document.getElementById('hm-dev');
export const aggResSelect = document.getElementById('agg-resolution');
export const aggTitleEl = document.getElementById('agg-title');
export const aggCanvasEl = document.getElementById('agg-chart');
export const tooltipEl = document.getElementById('tooltip');
export const deviceGridEl = document.getElementById('device-grid');

export const exportSinceEl = document.getElementById('export-since');
export const exportUntilEl = document.getElementById('export-until');
export const exportResolutionEl = document.getElementById('export-resolution');
export const exportDevicesEl = document.getElementById('export-devices');
export const exportBtnEl = document.getElementById('export-btn');
