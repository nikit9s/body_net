/* ============================================================
 *  Signal processing — pure DSP helpers (no DOM, no state).
 * ============================================================ */

import { CONFIG } from './config.js';

/**
 * Euclidean magnitude of a 3-axis acceleration sample.
 * @param {number} ax
 * @param {number} ay
 * @param {number} az
 * @returns {number} sqrt(ax^2 + ay^2 + az^2)
 */
export function magnitude(ax, ay, az) {
  return Math.sqrt(ax * ax + ay * ay + az * az);
}

/**
 * Single-pole high-pass IIR filter.
 * @param {ArrayLike<number>} x - input signal
 * @param {number} alpha - filter coefficient
 * @returns {Float64Array} high-passed signal
 */
export function highPassIIR(x, alpha) {
  const n = x.length;
  const y = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    y[i] = alpha * (y[i - 1] + x[i] - x[i - 1]);
  }
  return y;
}

/**
 * Count steps from a high-passed magnitude signal using peak detection
 * with a refractory period.
 * @param {ArrayLike<number>} filteredMagnitude - high-passed magnitude signal
 * @param {number} sampleRateHz - sampling rate in Hz
 * @returns {number} number of detected steps
 */
export function countSteps(filteredMagnitude, sampleRateHz) {
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
