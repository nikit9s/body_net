/* ============================================================
 *  REST API wrappers — /api fetch calls.
 * ============================================================ */

/**
 * Fetch the list of known device IDs from /api/devices.
 * @returns {Promise<number[]>} array of device IDs (empty on failure)
 */
export async function fetchKnownDevices() {
  try {
    const res = await fetch('/api/devices');
    if (!res.ok) return [];
    return await res.json();
  } catch {
    // API might not be ready yet
    return [];
  }
}

/**
 * Fetch aggregate activity data for a device.
 * @param {number} devId - device ID
 * @param {string} resolution - '1m' or '10s'
 * @param {string} since - ISO timestamp for the lookback window start
 * @returns {Promise<Array<object>|null>} aggregate rows, or null on failure
 */
export async function fetchAgg(devId, resolution, since) {
  const endpoint = resolution === '1m' ? '/api/agg/1m' : '/api/agg/10s';
  try {
    const res = await fetch(
      `${endpoint}?dev_id=${devId}&since=${encodeURIComponent(since)}`
    );
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.error('Aggregate chart load error:', err);
    return null;
  }
}
