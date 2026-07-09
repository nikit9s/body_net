/* ============================================================
 *  Device registry — per-device state Map + dropdown wiring +
 *  active-count / staleness helpers.
 * ============================================================ */

import { CONFIG } from './config.js';
import { aggDevSelect, activeCountEl } from './dom.js';
import { buildDevicePanel } from './devicePanel.js';

/** Map of devId -> device state. */
export const devices = new Map();

/** Set of device IDs known to the dropdown. */
export const knownDeviceIds = new Set();

/**
 * Callback fired when the dropdown auto-selects its first real device.
 * Set by the dashboard to trigger an aggregate-chart refresh, avoiding a
 * circular import between the registry and the dashboard orchestrator.
 * @type {() => void}
 */
let onFirstDeviceSelected = () => {};

/**
 * Register the callback invoked when the first device is auto-selected.
 * @param {() => void} fn
 */
export function setOnFirstDeviceSelected(fn) {
  onFirstDeviceSelected = fn;
}

/**
 * Callback fired whenever a new device ID is registered (every device, not
 * just the first). Used by the export panel to add its device checkbox,
 * avoiding a circular import between the registry and that module.
 * @type {(devId: number) => void}
 */
let onDeviceAdded = () => {};

/**
 * Register the callback invoked whenever a new device ID is registered.
 * @param {(devId: number) => void} fn
 */
export function setOnDeviceAdded(fn) {
  onDeviceAdded = fn;
}

/**
 * Short, human-friendly hex label for a device ID (last 4 hex digits).
 * @param {number} devId
 * @returns {string}
 */
export function shortDevId(devId) {
  const hex = (devId >>> 0).toString(16).toUpperCase();
  return hex.length > 4 ? '…' + hex.slice(-4) : hex;
}

/**
 * Get an existing device state, or create one (up to CONFIG.MAX_DEVICES).
 * @param {number} devId
 * @returns {object|null} device state, or null if at capacity
 */
export function getOrCreateDevice(devId) {
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

/**
 * Register a device ID for the dropdown (idempotent).
 * @param {number} devId
 */
export function registerDeviceId(devId) {
  if (knownDeviceIds.has(devId)) return;
  knownDeviceIds.add(devId);
  addDeviceOption(devId);
}

/**
 * Add an <option> for a device to the aggregate-chart dropdown.
 * Auto-selects the first real device and notifies via the callback.
 * @param {number} devId
 */
export function addDeviceOption(devId) {
  const existing = aggDevSelect.querySelector(`option[value="${devId}"]`);
  if (existing) return;

  const opt = document.createElement('option');
  opt.value = devId;
  opt.textContent = `Device ${shortDevId(devId)} (${devId})`;
  aggDevSelect.appendChild(opt);

  if (aggDevSelect.options.length === 2) {
    aggDevSelect.selectedIndex = 1;
    onFirstDeviceSelected();
  }

  onDeviceAdded(devId);
}

/**
 * Update the header "active devices" count based on staleness window.
 */
export function updateActiveCount() {
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
