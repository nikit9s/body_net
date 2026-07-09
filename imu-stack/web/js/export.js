/* ============================================================
 *  Export-to-Excel panel — device checkboxes, defaults, and the
 *  download handler for /api/export/xlsx.
 * ============================================================ */

import {
  exportSinceEl,
  exportUntilEl,
  exportResolutionEl,
  exportDevicesEl,
  exportBtnEl,
} from './dom.js';
import { fetchExportXlsx } from './api.js';
import { shortDevId, setOnDeviceAdded } from './deviceRegistry.js';

/**
 * Format a Date as a `datetime-local` input value in local time.
 * @param {Date} date
 * @returns {string}
 */
function toDatetimeLocalValue(date) {
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * Prefill the From/To fields with the last hour.
 */
function initExportDefaults() {
  const now = new Date();
  const hourAgo = new Date(now.getTime() - 60 * 60 * 1000);
  exportUntilEl.value = toDatetimeLocalValue(now);
  exportSinceEl.value = toDatetimeLocalValue(hourAgo);
}

/**
 * Add a checkbox for a device to the export panel (idempotent).
 * @param {number} devId
 */
function addExportDeviceCheckbox(devId) {
  if (exportDevicesEl.querySelector(`input[value="${devId}"]`)) return;
  if (exportDevicesEl.querySelector('.header-stat')) exportDevicesEl.innerHTML = '';

  const label = document.createElement('label');
  label.innerHTML = `<input type="checkbox" value="${devId}" checked /> ${shortDevId(devId)}`;
  exportDevicesEl.appendChild(label);
}

/**
 * Handle a click on the "Скачать .xlsx" button: validate inputs, fetch the
 * export, and trigger a browser download of the returned blob.
 */
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
    // Lets the backend shift the exported "time" column back to the
    // browser's local wall-clock instead of leaving it as raw UTC.
    tz_offset_min: since.getTimezoneOffset(),
  });
  if (checkedDevIds.length > 0) {
    params.set('dev_ids', checkedDevIds.join(','));
  }

  exportBtnEl.disabled = true;
  exportBtnEl.textContent = 'Экспорт…';
  try {
    const result = await fetchExportXlsx(params);
    if (!result.ok) {
      alert(`Ошибка экспорта: ${result.error}`);
      return;
    }
    const url = URL.createObjectURL(result.blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `imu_export_${resolution}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } finally {
    exportBtnEl.disabled = false;
    exportBtnEl.textContent = 'Скачать .xlsx';
  }
}

/**
 * Wire up the export panel: defaults, device checkboxes, and the button.
 */
export function initExportPanel() {
  initExportDefaults();
  setOnDeviceAdded(addExportDeviceCheckbox);
  exportBtnEl.addEventListener('click', handleExportClick);
}
