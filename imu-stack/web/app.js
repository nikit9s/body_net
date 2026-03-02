const devEl = document.getElementById('dev');
const seqEl = document.getElementById('seq');
const battEl = document.getElementById('batt');
const peakEl = document.getElementById('peak');
const stepsEl = document.getElementById('steps');
const fsEl = document.getElementById('fs');
const hmEl = document.getElementById('hm');
const statusEl = document.getElementById('status');
const statusText = document.getElementById('status-text');
const hmDevSelect = document.getElementById('hm-dev');
const tooltip = document.getElementById('tooltip');

const MAX_POINTS = 256;

const ctx = document.getElementById('chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: '|a| (mG)',
      data: [],
      borderColor: 'rgba(108, 122, 255, 0.9)',
      backgroundColor: 'rgba(108, 122, 255, 0.08)',
      borderWidth: 1.5,
      pointRadius: 0,
      fill: true,
      tension: 0.2,
    }],
  },
  options: {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    interaction: { intersect: false, mode: 'index' },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#232733',
        titleColor: '#e4e6f0',
        bodyColor: '#8b8fa3',
        borderColor: '#2d3145',
        borderWidth: 1,
        cornerRadius: 8,
        padding: 10,
      },
    },
    scales: {
      x: {
        display: false,
      },
      y: {
        grid: { color: 'rgba(45, 49, 69, 0.5)', lineWidth: 1 },
        ticks: { color: '#8b8fa3', font: { size: 11 } },
        title: { display: true, text: 'mG', color: '#8b8fa3', font: { size: 11 } },
      },
    },
  },
});

function magnitude(ax, ay, az) {
  return Math.sqrt(ax * ax + ay * ay + az * az);
}

function hpSimple(x, alpha = 0.98) {
  const y = new Array(x.length).fill(0);
  for (let i = 1; i < x.length; i++) {
    y[i] = alpha * (y[i - 1] + x[i] - x[i - 1]);
  }
  return y;
}

function countSteps(a_hp_mg, fs_hz, thr = 200, refr = 0.25) {
  const minI = Math.max(1, Math.floor(refr * fs_hz));
  let cnt = 0, last = -minI;
  for (let i = 1; i < a_hp_mg.length - 1; i++) {
    if (a_hp_mg[i] > thr && a_hp_mg[i] >= a_hp_mg[i - 1] && a_hp_mg[i] >= a_hp_mg[i + 1]) {
      if (i - last >= minI) {
        cnt++;
        last = i;
      }
    }
  }
  return cnt;
}

function pushFrame(ax, ay, az, fs) {
  const n = ax.length;
  const amag_mg = new Array(n);
  for (let i = 0; i < n; i++) {
    amag_mg[i] = magnitude(ax[i], ay[i], az[i]);
  }

  const hp = hpSimple(amag_mg, 0.98);
  const steps = countSteps(hp, fs);
  const peak = Math.max(...amag_mg.map(v => Math.floor(v)));

  peakEl.textContent = peak.toLocaleString();
  stepsEl.textContent = steps;
  fsEl.textContent = fs;

  chart.data.labels = [...Array(n).keys()];
  chart.data.datasets[0].data = amag_mg;
  chart.update('none');
}

// --- Heatmap ---

function formatTime(isoStr) {
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return ''; }
}

async function loadHeatmap(devId) {
  const since = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  try {
    const res = await fetch(`/api/agg/10s?dev_id=${devId}&since=${encodeURIComponent(since)}`);
    if (!res.ok) {
      hmEl.innerHTML = '<div class="empty-state"><div class="icon">—</div>No data available</div>';
      return;
    }
    const data = await res.json();

    if (!data.length) {
      hmEl.innerHTML = '<div class="empty-state"><div class="icon">—</div>No data for the last hour</div>';
      return;
    }

    hmEl.innerHTML = '';
    const maxRms = Math.max(1, ...data.map(d => d.a_rms_mg_max || 0));

    for (const d of data) {
      const v = (d.a_rms_mg_max || 0) / maxRms;
      const cell = document.createElement('div');
      cell.className = 'hm-cell';

      const hue = 240 - v * 180;
      const sat = 60 + v * 30;
      const lgt = 20 + v * 35;
      cell.style.background = `hsl(${hue}, ${sat}%, ${lgt}%)`;

      cell.addEventListener('mouseenter', (e) => {
        tooltip.textContent = `${formatTime(d.ts)} — RMS: ${d.a_rms_mg_max || 0} mG, Steps: ${d.steps || 0}`;
        tooltip.style.display = 'block';
        tooltip.style.left = e.clientX + 12 + 'px';
        tooltip.style.top = e.clientY - 30 + 'px';
      });
      cell.addEventListener('mousemove', (e) => {
        tooltip.style.left = e.clientX + 12 + 'px';
        tooltip.style.top = e.clientY - 30 + 'px';
      });
      cell.addEventListener('mouseleave', () => {
        tooltip.style.display = 'none';
      });

      hmEl.appendChild(cell);
    }
  } catch (err) {
    console.error('Heatmap load error:', err);
    hmEl.innerHTML = '<div class="empty-state"><div class="icon">—</div>Failed to load data</div>';
  }
}

// --- WebSocket ---

let reconnectDelay = 1000;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${location.host}/ws/json`;

  statusEl.classList.remove('connected');
  statusText.textContent = 'Connecting…';

  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    console.error('WS create error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    statusEl.classList.add('connected');
    statusText.textContent = 'Live';
    reconnectDelay = 1000;
  };

  ws.onmessage = (ev) => {
    try {
      const obj = JSON.parse(ev.data);
      if (obj.type !== 'imu') return;
      const { meta, ax, ay, az } = obj;
      devEl.textContent = meta.dev_id;
      seqEl.textContent = meta.seq;
      battEl.textContent = meta.batt ?? '—';
      pushFrame(ax, ay, az, meta.fs_hz);
    } catch (e) {
      console.error('WS message error:', e);
    }
  };

  ws.onerror = (e) => {
    console.error('WS error:', e);
  };

  ws.onclose = () => {
    statusEl.classList.remove('connected');
    statusText.textContent = 'Disconnected';
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  setTimeout(connectWS, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
}

// --- Init ---

connectWS();

const selectedDev = () => parseInt(hmDevSelect.value, 10) || 1;
loadHeatmap(selectedDev());
setInterval(() => loadHeatmap(selectedDev()), 15_000);
hmDevSelect.addEventListener('change', () => loadHeatmap(selectedDev()));
