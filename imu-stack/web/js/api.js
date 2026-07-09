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
 * @param {string} resolution - '10s' | '1m' | '5m' | '10m' | '15m' | '30m'
 * @param {string} since - ISO timestamp for the lookback window start
 * @returns {Promise<Array<object>|null>} aggregate rows, or null on failure
 */
export async function fetchAgg(devId, resolution, since) {
  try {
    const res = await fetch(
      `/api/agg/${resolution}?dev_id=${devId}&since=${encodeURIComponent(since)}`
    );
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    console.error('Aggregate chart load error:', err);
    return null;
  }
}

/**
 * Download an .xlsx export as a Blob via /api/export/xlsx.
 * @param {URLSearchParams} params - since/until/resolution/dev_ids query params
 * @returns {Promise<{ok: true, blob: Blob}|{ok: false, error: string}>}
 */
export async function fetchExportXlsx(params) {
  try {
    const res = await fetch(`/api/export/xlsx?${params.toString()}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return { ok: false, error: body.detail || String(res.status) };
    }
    return { ok: true, blob: await res.blob() };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}
