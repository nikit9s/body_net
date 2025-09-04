const devEl = document.getElementById('dev');
const seqEl = document.getElementById('seq');
const battEl = document.getElementById('batt');
const peakEl = document.getElementById('peak');
const stepsEl = document.getElementById('steps');
const hmEl = document.getElementById('hm');

const G_MMPS2 = 9810.0;
const mmps2_to_mg = 1000.0 / G_MMPS2;

const ctx = document.getElementById('chart').getContext('2d');
const chart = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{ label: '|a| (mG)', data: [], borderWidth: 1, pointRadius: 0 }] },
    options: { animation: false, responsive: true, scales: { x: { display: false } } }
});

function magnitude(ax, ay, az) { return Math.sqrt(ax * ax + ay * ay + az * az); }
function hpSimple(x, alpha = 0.98) { const y = new Array(x.length).fill(0); for (let i = 1; i < x.length; i++) y[i] = alpha * (y[i - 1] + x[i] - x[i - 1]); return y; }
function countSteps(a_hp_mg, fs_hz, thr = 200, refr = 0.25) {
    const minI = Math.max(1, Math.floor(refr * fs_hz));
    let cnt = 0, last = -minI; for (let i = 1; i < a_hp_mg.length - 1; i++) if (a_hp_mg[i] > thr && a_hp_mg[i] >= a_hp_mg[i - 1] && a_hp_mg[i] >= a_hp_mg[i + 1]) if (i - last >= minI) { cnt++; last = i; } return cnt;
}

function pushFrame(ax, ay, az, fs) {
    const n = ax.length; const amag_mg = new Array(n);
    for (let i = 0; i < n; i++) amag_mg[i] = magnitude(ax[i], ay[i], az[i]);
    const hp = hpSimple(amag_mg, 0.98); const steps = countSteps(hp, fs);
    const peak = Math.max(...amag_mg.map(v => Math.floor(v)));
    peakEl.textContent = peak; stepsEl.textContent = steps;
    chart.data.labels = [...Array(n).keys()]; chart.data.datasets[0].data = amag_mg; chart.update('none');
}

async function loadHeatmap(dev_id = 1) {
    const since = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    const res = await fetch(`/api/agg/10s?dev_id=${dev_id}&since=${encodeURIComponent(since)}`);
    const data = await res.json();
    hmEl.innerHTML = '';
    const max = Math.max(1, ...data.map(d => d.a_rms_mg_max));
    for (const d of data) {
        const v = d.a_rms_mg_max / max; // 0..1
        const c = Math.floor(240 - 240 * v); // от светло‑серого к насыщенному
        const cell = document.createElement('div');
        cell.className = 'cell';
        cell.title = `${d.ts} | RMS=${d.a_rms_mg_max} mG, steps=${d.steps}`;
        cell.style.background = `hsl(220, 70%, ${90 - 60 * v}%)`;
        hmEl.appendChild(cell);
    }
}

function connectWS() {
    const ws = new WebSocket("ws://localhost:8765/ws/json");
    ws.onmessage = ev => {
        try {
            const obj = JSON.parse(ev.data); if (obj.type !== 'imu') return;
            const { meta, ax, ay, az } = obj;
            devEl.textContent = meta.dev_id; seqEl.textContent = meta.seq; battEl.textContent = meta.batt ?? '-';
            pushFrame(ax, ay, az, meta.fs_hz);
        } catch (e) { console.error(e); }
    };
    ws.onclose = () => setTimeout(connectWS, 1000);
}

connectWS();
loadHeatmap(1);
setInterval(() => loadHeatmap(1), 15_000);