/* ============================================================
 *  WebSocket client — /ws/json with exponential-backoff reconnect.
 * ============================================================ */

import { CONFIG } from './config.js';
import { wsStatusEl, wsStatusText } from './dom.js';

let reconnectDelay = CONFIG.WS_RECONNECT_INIT_MS;

/**
 * Connect to the /ws/json WebSocket and wire up reconnect + frame handling.
 * @param {(frame: object) => void} onFrame - called with each parsed JSON frame
 */
export function connectWebSocket(onFrame) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${location.host}/ws/json`;

  wsStatusEl.classList.remove('connected');
  wsStatusText.textContent = 'Connecting…';

  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('WebSocket creation error:', e);
    scheduleReconnect(onFrame);
    return;
  }

  ws.onopen = () => {
    wsStatusEl.classList.add('connected');
    wsStatusText.textContent = 'Live';
    reconnectDelay = CONFIG.WS_RECONNECT_INIT_MS;
  };

  ws.onmessage = ev => {
    try {
      onFrame(JSON.parse(ev.data));
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
    scheduleReconnect(onFrame);
  };
}

/**
 * Schedule a reconnect attempt with exponential backoff.
 * @param {(frame: object) => void} onFrame - frame handler to re-bind on reconnect
 */
function scheduleReconnect(onFrame) {
  setTimeout(() => connectWebSocket(onFrame), reconnectDelay);
  reconnectDelay = Math.min(
    reconnectDelay * CONFIG.WS_RECONNECT_FACTOR,
    CONFIG.WS_RECONNECT_MAX_MS
  );
}
