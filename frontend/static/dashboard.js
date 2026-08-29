/* dashboard.js - Starlink Monitor frontend
 * Angle convention (dome + compass): 0deg = North = up, clockwise.
 * See BACKEND_GUIDE / api-client.js for the data-fetching layer.
 */

// ---------------------------------------------------------------------------
// Colors
// ---------------------------------------------------------------------------
const COLORS = {
  green:  '#56d88f',
  cyan:   '#5ba3d0',
  yellow: '#e0a830',
  red:    '#e0566e',
  grid:   '#26334480',
  text:   '#5a7390',
};

// Known Starlink alert bits (alerts_bitfield), per the standard grpc AlertsDish struct.
const ALERT_BITS = [
  { bit: 0,  label: 'Motors stuck' },
  { bit: 1,  label: 'Thermal shutdown' },
  { bit: 2,  label: 'Unaligned' },
  { bit: 3,  label: 'No thermal shroud' },
  { bit: 4,  label: 'Rain/moisture detected' },
  { bit: 5,  label: 'Thermal throttling' },
  { bit: 6,  label: 'Software update required' },
  { bit: 7,  label: 'Slow ethernet' },
  { bit: 8,  label: 'Excessive motion' },
  { bit: 9,  label: 'ID reset' },
  { bit: 10, label: 'Market access restricted' },
  { bit: 11, label: 'Moving during update' },
];

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------
function fmtNum(v, decimals = 1, suffix = '') {
  return v == null ? '–' : `${v.toFixed(decimals)}${suffix}`;
}

function yesNoSpan(value) {
  if (value === null || value === undefined) return '<span class="dim">–</span>';
  return value ? '<span class="yes">Yes</span>' : '<span class="no">No</span>';
}

function toLocalDatetimeInputValue(date) {
  const p = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(date.getMinutes())}`;
}

function formatUptime(seconds) {
  if (seconds == null) return '–';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

// All timestamps render via toLocaleString without an explicit timeZone,
// so the browser renders in the user's local timezone. Chart.js x-axes get
// ts*1000 (true UTC milliseconds) and the date-fns adapter also formats
// locally (new Date(), no UTC getters) - both stay consistent.
function formatLocalDateTime(ts, opts = {}) {
  return new Date(ts * 1000).toLocaleString('en-GB', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', ...opts,
  });
}

// ---------------------------------------------------------------------------
// Data-fetching functions (fetchMetrics, fetchEvents, fetchSpeedtests,
// fetchDishStatus, fetchWeatherCurrent, fetchStatsSummary, downloadCsv,
// adminDelete) live in api-client.js, which must load before this file.
// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  ranges: { drop: '1d', latency: '1d', throughput: '1d', speedtest: 'all', peak: '1d', traffic: '7d' },
  liveMode: true,
  ws: null,
  wsReconnectTimer: null,
  charts: {},
  dome: { azimuth: null, elevation: null },
};

const MAX_LIVE_POINTS = 43200; // ~24h at 2s interval
const LIVE_TRIM_BATCH = 120;

// ---------------------------------------------------------------------------
// Time axis unit (minute/hour/day/month/year) is derived dynamically from
// the actual span (from/to) - see pickTimeAxisUnit() in api-client.js.
// ---------------------------------------------------------------------------
function applyTimeAxis(chart, fromTs, toTs) {
  const cfg = pickTimeAxisUnit(fromTs, toTs);
  chart.options.scales.x.time.unit = cfg.unit;
  chart.options.scales.x.time.tooltipFormat = cfg.tooltipFormat;
  chart.options.scales.x.time.displayFormats = cfg.displayFormats;
  chart.update('none');
}

function minMaxAfterLabel(unitSuffix, decimals = 1) {
  return (ctx) => {
    const raw = ctx.raw;
    if (!raw || raw._min == null || raw._max == null) return undefined;
    if (Math.abs(raw._max - raw._min) < 10 ** -decimals) return undefined;
    return `Low: ${raw._min.toFixed(decimals)}${unitSuffix} · High: ${raw._max.toFixed(decimals)}${unitSuffix}`;
  };
}

function baseChartOptions(yLabel, opts = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { intersect: false, mode: 'index' },
    scales: {
      x: {
        type: 'time',
        time: { unit: 'hour', tooltipFormat: 'dd.MM. HH:mm:ss', displayFormats: { hour: 'HH:mm', day: 'dd.MM.', month: 'MM.yyyy' } },
        grid: { color: COLORS.grid },
        ticks: { color: COLORS.text, font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
      },
      y: {
        beginAtZero: true,
        min: opts.min !== undefined ? opts.min : 0,
        max: opts.max,
        grid: { color: COLORS.grid },
        ticks: { color: COLORS.text, font: { size: 9 }, callback: opts.tickCallback },
        title: { display: !!yLabel, text: yLabel, color: COLORS.text, font: { size: 9 } },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#0d1520', borderColor: '#1e2a38', borderWidth: 1,
        titleColor: '#c8d6e5', bodyColor: '#8aa8c4',
        callbacks: { label: opts.tooltipCallbacks?.label, afterLabel: opts.tooltipCallbacks?.afterLabel },
      },
    },
  };
}

// ---------------------------------------------------------------------------
// Sky View dome (Canvas 2D)
// ---------------------------------------------------------------------------
const dome = { canvas: null, ctx: null, size: 220 };

function setupDomeCanvas() {
  dome.canvas = document.getElementById('domCanvas');
  if (!dome.canvas) return;
  sizeDomeCanvas();
  window.addEventListener('resize', debounce(sizeDomeCanvas, 200));
}

// Size is read from the actual rendered CSS box (getBoundingClientRect),
// not a hardcoded constant - keeps the canvas buffer sharp and centered
// at any width (e.g. smaller on narrow screens, max-width:100%).
function sizeDomeCanvas() {
  const c = dome.canvas;
  if (!c) return;
  const rect = c.getBoundingClientRect();
  const cssSize = Math.max(1, Math.round(rect.width || 220));
  const dpr = window.devicePixelRatio || 1;
  dome.size = cssSize;
  c.width = cssSize * dpr;
  c.height = cssSize * dpr;
  dome.ctx = c.getContext('2d');
  dome.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawDome();
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// Angle convention (shared by dome + compass):
// 0deg = North = up (12 o'clock), angles increase clockwise.
function polarToXY(cx, cy, radius, azimuthDeg) {
  const rad = (azimuthDeg - 90) * (Math.PI / 180);
  return { x: cx + Math.cos(rad) * radius, y: cy + Math.sin(rad) * radius };
}

function drawDome() {
  const ctx = dome.ctx;
  if (!ctx) return;
  const size = dome.size;
  const cx = size / 2, cy = size / 2;
  const R = size / 2 - Math.max(14, size * 0.08);

  ctx.clearRect(0, 0, size, size);
  ctx.beginPath();
  ctx.arc(cx, cy, R + 2, 0, Math.PI * 2);
  ctx.fillStyle = '#0d1520';
  ctx.fill();

  // Elevation rings (90=zenith/center, 0=horizon/outer edge)
  const elevSteps = [90, 75, 45, 25, 0];
  elevSteps.forEach((el, i) => {
    const r = ((90 - el) / 90) * R;
    ctx.beginPath();
    ctx.arc(cx, cy, r || 0.5, 0, Math.PI * 2);
    ctx.strokeStyle = i === elevSteps.length - 1 ? '#3a5070' : '#26334460';
    ctx.lineWidth = i === elevSteps.length - 1 ? 1.5 : 0.8;
    ctx.stroke();
    if (el > 0 && el < 90) {
      ctx.fillStyle = '#2a3d52';
      ctx.font = `${Math.max(7, size * 0.036)}px JetBrains Mono, monospace`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(`${el}°`, cx + r + 2, cy - 2);
    }
  });

  // Azimuth spokes every 45deg
  for (let az = 0; az < 360; az += 45) {
    const p = polarToXY(cx, cy, R, az);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(p.x, p.y);
    ctx.strokeStyle = '#26334450';
    ctx.lineWidth = 0.8;
    ctx.stroke();
  }

  // Cardinal directions
  const dirLabelR = R + Math.max(10, size * 0.05);
  [['N', 0], ['E', 90], ['S', 180], ['W', 270]].forEach(([label, az]) => {
    const p = polarToXY(cx, cy, dirLabelR, az);
    ctx.fillStyle = '#5ba3d0';
    ctx.font = `bold ${Math.max(9, size * 0.045)}px JetBrains Mono, monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, p.x, p.y);
  });

  // Dish needle
  if (dome.azimuth != null && dome.elevation != null) {
    const r = ((90 - dome.elevation) / 90) * R;
    const p = polarToXY(cx, cy, r, dome.azimuth);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(p.x, p.y);
    ctx.strokeStyle = '#5ba3d099';
    ctx.lineWidth = 1.2;
    ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.beginPath();
    ctx.arc(p.x, p.y, Math.max(3.5, size * 0.022), 0, Math.PI * 2);
    ctx.fillStyle = '#5ba3d0';
    ctx.shadowColor = '#5ba3d0';
    ctx.shadowBlur = 6;
    ctx.fill();
    ctx.shadowBlur = 0;

    ctx.font = `${Math.max(7, size * 0.036)}px JetBrains Mono, monospace`;
    ctx.fillStyle = '#c8d6e5';
    ctx.textAlign = p.x > cx ? 'left' : 'right';
    ctx.textBaseline = 'bottom';
    ctx.fillText(`${dome.azimuth.toFixed(0)}° / ${dome.elevation.toFixed(0)}°`, p.x + (p.x > cx ? 6 : -6), p.y - 4);
  } else {
    ctx.font = `${Math.max(8, size * 0.04)}px JetBrains Mono, monospace`;
    ctx.fillStyle = '#3a5070';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('ZENITH', cx, cy);
  }

  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.strokeStyle = '#3a5070';
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function updateDome(azimuth, elevation) {
  if (azimuth != null) dome.azimuth = azimuth;
  if (elevation != null) dome.elevation = elevation;
  drawDome();
}

// The gRPC protocol no longer exposes the per-wedge obstruction map
// (wedges_fraction_obstructed is obsoleted - see starlink_grpc.py), so the
// dome can't draw actual clear/blocked sectors. Show the live obstruction
// fraction instead - the total percentage is still provided by the dish.
function setDomeObstruction(fraction) {
  const el = document.getElementById('domeObstrLabel');
  if (!el) return;
  if (fraction == null) {
    el.textContent = 'Obstruction –';
    el.style.color = '#5a7390';
    return;
  }
  const pct = fraction * 100;
  el.textContent = `Obstruction ${pct.toFixed(2)} %`;
  el.style.color = pct > 0 ? '#e0566e' : '#5a7390';
}

// ---------------------------------------------------------------------------
// Compass (SVG) - same angle convention as the dome
// ---------------------------------------------------------------------------
function setCompass(azimuth, elevation) {
  const needle = document.getElementById('compassNeedle');
  if (!needle) return;
  if (azimuth == null) { needle.style.opacity = '0.2'; return; }
  needle.style.opacity = '1';
  const elFactor = elevation != null ? Math.max(0.3, Math.min(1, elevation / 90)) : 1;
  const len = 60 * elFactor;
  const p = polarToXY(80, 80, len, azimuth);
  needle.setAttribute('x2', p.x.toFixed(1));
  needle.setAttribute('y2', p.y.toFixed(1));
}

// ---------------------------------------------------------------------------
// Initialize charts
// ---------------------------------------------------------------------------
function initCharts() {
  state.charts.drop = new Chart(document.getElementById('dropChart'), {
    type: 'line',
    data: { datasets: [{ label: 'Ping Drop %', data: [], borderColor: COLORS.green, backgroundColor: COLORS.green + '40', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.2 }] },
    options: baseChartOptions('Drop Rate (%)', {
      tickCallback: (v) => `${v}%`,
      tooltipCallbacks: { label: (ctx) => `Drop: ${ctx.parsed.y.toFixed(2)} %` },
    }),
  });

  state.charts.latency = new Chart(document.getElementById('latencyChart'), {
    type: 'line',
    data: { datasets: [{ label: 'Latency ms', data: [], borderColor: COLORS.cyan, backgroundColor: COLORS.cyan + '40', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.2 }] },
    options: baseChartOptions('Latency (ms)', {
      tickCallback: (v) => `${v} ms`,
      tooltipCallbacks: {
        label: (ctx) => `Latency: ${ctx.parsed.y.toFixed(0)} ms${ctx.dataset._isAggregated ? ' (avg)' : ''}`,
        afterLabel: minMaxAfterLabel(' ms', 0),
      },
    }),
  });

  const throughputOptions = baseChartOptions('Mbit/s', {
    tickCallback: (v) => `${v}`,
    tooltipCallbacks: {
      label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} Mbit/s${ctx.dataset._isAggregated ? ' (avg)' : ''}`,
      afterLabel: minMaxAfterLabel(' Mbit/s', 1),
    },
  });
  throughputOptions.plugins.legend = { display: true, labels: { color: COLORS.text, font: { size: 9 }, boxWidth: 10 } };
  state.charts.throughput = new Chart(document.getElementById('throughputChart'), {
    type: 'line',
    data: {
      datasets: [
        { label: 'Download Mbit/s', data: [], borderColor: COLORS.green, backgroundColor: COLORS.green + '33', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.2 },
        { label: 'Upload Mbit/s', data: [], borderColor: COLORS.cyan, backgroundColor: COLORS.cyan + '33', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.2 },
      ],
    },
    options: throughputOptions,
  });

  setupDomeCanvas();

  state.charts.speedtest = new Chart(document.getElementById('speedtestChart'), {
    type: 'bar',
    data: {
      datasets: [
        { label: 'Download Mbit/s', data: [], backgroundColor: COLORS.green + 'cc', borderColor: COLORS.green, borderWidth: 1, borderRadius: 2, barPercentage: 0.9, categoryPercentage: 0.7 },
        { label: 'Upload Mbit/s', data: [], backgroundColor: COLORS.cyan + 'cc', borderColor: COLORS.cyan, borderWidth: 1, borderRadius: 2, barPercentage: 0.9, categoryPercentage: 0.7 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'hour', tooltipFormat: 'dd.MM.yyyy HH:mm', displayFormats: { hour: 'HH:mm', day: 'dd.MM.', month: 'MM.yyyy' } },
          grid: { color: COLORS.grid },
          ticks: { color: COLORS.text, font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 14 },
          offset: true,
        },
        y: {
          beginAtZero: true, min: 0,
          grid: { color: COLORS.grid },
          ticks: { color: COLORS.text, font: { size: 9 }, callback: (v) => `${v}` },
          title: { display: true, text: 'Mbit/s', color: COLORS.text, font: { size: 9 } },
        },
      },
      plugins: {
        legend: { display: true, labels: { color: COLORS.text, font: { size: 10 }, boxWidth: 10 } },
        tooltip: {
          backgroundColor: '#0d1520', borderColor: '#1e2a38', borderWidth: 1,
          titleColor: '#c8d6e5', bodyColor: '#8aa8c4',
          callbacks: { label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} Mbit/s` },
        },
      },
    },
  });

  state.charts.traffic = new Chart(document.getElementById('trafficChart'), {
    type: 'bar',
    data: {
      datasets: [
        { label: 'Download GB', data: [], backgroundColor: COLORS.green + 'cc', borderColor: COLORS.green, borderWidth: 1, borderRadius: 2, barPercentage: 0.9, categoryPercentage: 0.7 },
        { label: 'Upload GB', data: [], backgroundColor: COLORS.cyan + 'cc', borderColor: COLORS.cyan, borderWidth: 1, borderRadius: 2, barPercentage: 0.9, categoryPercentage: 0.7 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'day', tooltipFormat: 'dd.MM.yyyy HH:mm', displayFormats: { hour: 'HH:mm', day: 'dd.MM.', month: 'MM.yyyy' } },
          grid: { color: COLORS.grid },
          ticks: { color: COLORS.text, font: { size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 14 },
          offset: true,
        },
        y: {
          beginAtZero: true, min: 0,
          grid: { color: COLORS.grid },
          ticks: { color: COLORS.text, font: { size: 9 }, callback: (v) => `${v} GB` },
          title: { display: true, text: 'GB', color: COLORS.text, font: { size: 9 } },
        },
      },
      plugins: {
        legend: { display: true, labels: { color: COLORS.text, font: { size: 10 }, boxWidth: 10 } },
        tooltip: {
          backgroundColor: '#0d1520', borderColor: '#1e2a38', borderWidth: 1,
          titleColor: '#c8d6e5', bodyColor: '#8aa8c4',
          callbacks: { label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)} GB` },
        },
      },
    },
  });
}

// ---------------------------------------------------------------------------
// Event markers on charts
// ---------------------------------------------------------------------------
function applyEventMarkers(chart, events, typeFilter, color, resolutionS) {
  const dataset = chart.data.datasets[0];
  if (!dataset || !dataset.data.length) return;
  // fetchEvents() returns newest-first - sort ascending to match dataset order
  const eventTimes = events.filter((e) => e.type === typeFilter).map((e) => e.ts * 1000).sort((a, b) => a - b);
  if (!eventTimes.length) {
    dataset.pointBackgroundColor = undefined;
    dataset.pointRadius = 0;
    chart.update('none');
    return;
  }
  const toleranceMs = Math.max(60000, (resolutionS * 1000) / 2 + 30000);
  const bgColors = [], radii = [];
  // Both dataset.data and eventTimes are ascending: a single pointer gives
  // O(n+m) instead of the previous O(n*m) .some() per point (with 43k live
  // points and many events that was the hottest loop in a range switch).
  let ei = 0;
  for (const d of dataset.data) {
    while (ei < eventTimes.length && eventTimes[ei] + toleranceMs < d.x) ei++;
    const hit = ei < eventTimes.length && eventTimes[ei] <= d.x + toleranceMs;
    bgColors.push(hit ? color : 'transparent');
    radii.push(hit ? 4 : 0);
  }
  dataset.pointBackgroundColor = bgColors;
  dataset.pointRadius = radii;
  chart.update();
}

// ---------------------------------------------------------------------------
// Load metrics
// ---------------------------------------------------------------------------
async function loadMetrics(range, target) {
  const json = await fetchMetrics(range);
  const points = json.data;
  const isAgg = json.resolution_s > 2;

  // null (not 0) for missing values so Chart.js draws a gap instead of a
  // misleading zero-line during outages. The dish also reports -1 as a
  // "no data" sentinel for latency - treat it as missing as well.
  const dropData = points.map((p) => ({ x: p.ts * 1000, y: p.ping_drop_rate != null ? p.ping_drop_rate * 100 : null }));
  const latencyData = points.map((p) => ({
    x: p.ts * 1000, y: p.ping_latency_ms != null && p.ping_latency_ms > 0 ? p.ping_latency_ms : null,
    _min: p.min_latency, _max: p.max_latency,
  }));
  const downData = points.map((p) => ({
    x: p.ts * 1000, y: (p.downlink_bps ?? 0) / 1e6,
    _min: p.min_downlink != null ? p.min_downlink / 1e6 : null,
    _max: p.max_downlink != null ? p.max_downlink / 1e6 : null,
  }));
  const upData = points.map((p) => ({
    x: p.ts * 1000, y: (p.uplink_bps ?? 0) / 1e6,
    _min: p.min_uplink != null ? p.min_uplink / 1e6 : null,
    _max: p.max_uplink != null ? p.max_uplink / 1e6 : null,
  }));

  if (target === 'drop' || target === 'both') {
    state.charts.drop.data.datasets[0].data = dropData;
    state.charts.drop.data.datasets[0]._isAggregated = isAgg;
    applyTimeAxis(state.charts.drop, json.from, json.to);
  }
  if (target === 'latency' || target === 'both') {
    state.charts.latency.data.datasets[0].data = latencyData;
    state.charts.latency.data.datasets[0]._isAggregated = isAgg;
    applyTimeAxis(state.charts.latency, json.from, json.to);
  }
  if (target === 'throughput' || target === 'both') {
    state.charts.throughput.data.datasets[0].data = downData;
    state.charts.throughput.data.datasets[1].data = upData;
    state.charts.throughput.data.datasets[0]._isAggregated = isAgg;
    state.charts.throughput.data.datasets[1]._isAggregated = isAgg;
    applyTimeAxis(state.charts.throughput, json.from, json.to);
  }

  try {
    const events = await fetchEvents(range);
    if (target === 'drop' || target === 'both') applyEventMarkers(state.charts.drop, events, 'disconnect', COLORS.red, json.resolution_s);
    if (target === 'latency' || target === 'both') applyEventMarkers(state.charts.latency, events, 'latency_spike', COLORS.red, json.resolution_s);
  } catch (_e) { /* markers are best-effort, not a hard error */ }
}

// ---------------------------------------------------------------------------
// Load speedtests
// ---------------------------------------------------------------------------
// Short label for the bucket width of an aggregated point, e.g. "4-hr avg"
// or "2-day avg" - adapts to the actual resolution instead of always
// saying "daily avg".
function bucketLabel(resolutionS) {
  if (resolutionS < 3600) return `${Math.round(resolutionS / 60)}-min avg`;
  if (resolutionS < 86400) return `${Math.round(resolutionS / 3600)}-hr avg`;
  const days = resolutionS / 86400;
  return `${days >= 1.5 ? Math.round(days) : days.toFixed(1)}-day avg`;
}

async function loadSpeedtests(range = 'all') {
  const json = await fetchSpeedtests(range);
  const rows = json.data;
  const chart = state.charts.speedtest;
  const isAgg = json.resolution_s > RAW_SPEEDTEST_INTERVAL_S;
  const label = bucketLabel(json.resolution_s);

  applyTimeAxis(chart, json.from, json.to);

  // Index rows by timestamp for O(1) tooltip lookup instead of .find() per hover
  const byTs = new Map(rows.map((r) => [r.ts * 1000, r]));
  chart.options.plugins.tooltip.callbacks.label =
    (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} Mbit/s${isAgg ? ` (${label})` : ''}`;
  chart.options.plugins.tooltip.callbacks.afterLabel = !isAgg ? undefined : (ctx) => {
    const r = byTs.get(ctx.raw.x);
    if (!r) return undefined;
    const isDown = ctx.datasetIndex === 0;
    const min = isDown ? r.min_download_mbit : r.min_upload_mbit;
    const max = isDown ? r.max_download_mbit : r.max_upload_mbit;
    if (min == null || max == null) return undefined;
    return `Low: ${min.toFixed(1)} · High: ${max.toFixed(1)} Mbit/s (${r.sample_count} tests)`;
  };

  chart.data.datasets[0].data = rows.map((r) => ({ x: r.ts * 1000, y: r.download_mbit }));
  chart.data.datasets[1].data = rows.map((r) => ({ x: r.ts * 1000, y: r.upload_mbit }));
  chart.update();

  document.getElementById('speedtestCount').textContent =
    isAgg ? `${rows.length} windows (${label})` : `${rows.length} tests`;
}

// ---------------------------------------------------------------------------
// Traffic bar chart (down/up data volume in GB)
// ---------------------------------------------------------------------------
async function loadTraffic(range = '7d') {
  const json = await fetchTraffic(range);
  const rows = json.data;
  const chart = state.charts.traffic;

  applyTimeAxis(chart, json.from, json.to);

  chart.data.datasets[0].data = rows.map((r) => ({ x: r.ts * 1000, y: r.down_gb }));
  chart.data.datasets[1].data = rows.map((r) => ({ x: r.ts * 1000, y: r.up_gb }));
  chart.update();

  const totalEl = document.getElementById('trafficTotal');
  if (totalEl) {
    totalEl.textContent =
      `Total: ${json.totals.down_gb.toFixed(1)} GB ↓ · ${json.totals.up_gb.toFixed(1)} GB ↑`;
  }
}

// ---------------------------------------------------------------------------
// Peak-values tile (best values from 2s live sampling)
// ---------------------------------------------------------------------------
async function loadPeakStats(range = '1d') {
  const s = await fetchPeakStats(range);
  document.getElementById('peakLatency').textContent =
    s.best_latency_ms != null ? `${s.best_latency_ms.toFixed(0)} ms` : '– ms';
  document.getElementById('peakDownload').textContent =
    s.peak_download_bps != null ? `${(s.peak_download_bps / 1e6).toFixed(1)} Mbit/s` : '– Mbit/s';
  document.getElementById('peakUpload').textContent =
    s.peak_upload_bps != null ? `${(s.peak_upload_bps / 1e6).toFixed(1)} Mbit/s` : '– Mbit/s';
}

// ---------------------------------------------------------------------------
// Stat tiles
// ---------------------------------------------------------------------------
function setStatLevel(el, value, warnThresh, errThresh, higherIsBad = true) {
  el.classList.remove('ok', 'warn', 'err', 'blue');
  let level = 'ok';
  if (higherIsBad) {
    if (value >= errThresh) level = 'err';
    else if (value >= warnThresh) level = 'warn';
  } else {
    if (value <= errThresh) level = 'err';
    else if (value <= warnThresh) level = 'warn';
  }
  el.classList.add(level);
}

async function loadSummary() {
  const stats = await fetchStatsSummary();

  const dropPct = (stats.avg_drop_rate_24h ?? 0) * 100;
  const dropEl = document.getElementById('statDrop');
  dropEl.textContent = `${dropPct.toFixed(1)} %`;
  setStatLevel(dropEl, dropPct, 2, 5);

  const latEl = document.getElementById('statLatency');
  latEl.textContent = stats.avg_latency_ms_24h != null ? `${Math.round(stats.avg_latency_ms_24h)} ms` : '– ms';
  latEl.classList.remove('ok', 'warn', 'err');
  latEl.classList.add('blue');

  const uptimeEl = document.getElementById('statUptime');
  if (stats.uptime_pct_24h != null) {
    uptimeEl.textContent = `${stats.uptime_pct_24h.toFixed(2)} %`;
    setStatLevel(uptimeEl, stats.uptime_pct_24h, 99.5, 98, false);
  } else {
    uptimeEl.textContent = '– %';
  }

  const discEl = document.getElementById('statDisconnects');
  discEl.textContent = stats.disconnects_24h ?? 0;
  setStatLevel(discEl, stats.disconnects_24h ?? 0, 1, 3);

  return stats;
}

// ---------------------------------------------------------------------------
// Weather
// ---------------------------------------------------------------------------
const WMO_ICON = {
  0: '☀️', 1: '🌤️', 2: '⛅', 3: '☁️', 45: '🌫️', 48: '🌫️',
  51: '🌦️', 53: '🌦️', 55: '🌧️', 61: '🌧️', 63: '🌧️', 65: '🌧️',
  71: '🌨️', 73: '🌨️', 75: '❄️', 77: '🌨️', 80: '🌦️', 81: '🌧️', 82: '⛈️',
  85: '🌨️', 86: '❄️', 95: '⛈️', 96: '⛈️', 99: '⛈️',
};

async function loadWeather() {
  const w = await fetchWeatherCurrent();
  if (!w || !w.ts) return;
  const icon = WMO_ICON[w.wmo_code] ?? '·';
  document.getElementById('weatherCity').textContent = 'Krefeld';  // adjust to your own location if you fork this
  document.getElementById('weatherMain').textContent =
    `${icon} ${fmtNum(w.temp_c, 0, ' °C')} · Wind ${fmtNum(w.wind_kmh, 0, ' km/h')}`;
  document.getElementById('weatherExtra').textContent =
    `Visibility ${w.visibility_m != null ? (w.visibility_m / 1000).toFixed(0) + ' km' : '–'} · Humidity ${fmtNum(w.humidity, 0, ' %')}`;
  const warnEl = document.getElementById('weatherWarn');
  if (w.warning) { warnEl.textContent = `⚡ ${w.warning}`; warnEl.style.display = ''; }
  else { warnEl.style.display = 'none'; }
}

// ---------------------------------------------------------------------------
// Dish status + device alerts
// ---------------------------------------------------------------------------
function renderAlerts(bitfield) {
  const grid = document.getElementById('alertGrid');
  const summary = document.getElementById('alertSummary');
  if (!grid) return;

  if (bitfield == null) {
    grid.innerHTML = '<div class="event-empty">No alert data available.</div>';
    if (summary) summary.textContent = '';
    return;
  }

  const activeBits = ALERT_BITS.filter((a) => (bitfield & (1 << a.bit)) !== 0);
  grid.innerHTML = ALERT_BITS.map((a) => {
    const active = (bitfield & (1 << a.bit)) !== 0;
    return `<div class="alert-chip ${active ? 'active' : 'inactive'}">
      <span class="dot"></span>
      <span class="alert-chip-label">${a.label}</span>
    </div>`;
  }).join('');

  if (summary) {
    summary.textContent = activeBits.length
      ? `${activeBits.length} active`
      : 'none active';
  }
}

async function loadDishStatus() {
  const d = await fetchDishStatus();
  if (!d || Object.keys(d).length === 0) return;

  const dlEl = document.getElementById('statDownload');
  const ulEl = document.getElementById('statUpload');
  if (d.downlink_bps != null && dlEl) dlEl.textContent = `${(d.downlink_bps / 1e6).toFixed(0)} Mbit/s`;
  if (d.uplink_bps != null && ulEl) ulEl.textContent = `${(d.uplink_bps / 1e6).toFixed(0)} Mbit/s`;

  setCompass(d.direction_azimuth, d.direction_elevation);
  updateDome(d.direction_azimuth, d.direction_elevation);
  setDomeObstruction(d.obstr_fraction);

  document.getElementById('dishAzimuth').textContent = d.direction_azimuth != null ? `${d.direction_azimuth.toFixed(1)} °` : '– °';
  document.getElementById('dishElevation').textContent = d.direction_elevation != null ? `${d.direction_elevation.toFixed(1)} °` : '– °';
  document.getElementById('dishDeviceId').textContent = d.device_id || '–';
  document.getElementById('dishHardware').textContent = d.hardware_version || '–';
  document.getElementById('dishSoftware').textContent = d.software_version || '–';
  document.getElementById('dishUptime').textContent = formatUptime(d.uptime_s);

  const stateEl = document.getElementById('dishState');
  stateEl.textContent = d.state || '–';
  stateEl.style.color = d.state === 'CONNECTED' ? '#56d88f' : (d.state ? '#e0a830' : '#c8d6e5');

  document.getElementById('gpsReady').innerHTML = yesNoSpan(d.gps_ready);
  document.getElementById('gpsEnabled').innerHTML = yesNoSpan(d.gps_enabled);
  document.getElementById('gpsSats').textContent = d.gps_sats ?? '–';
  document.getElementById('snrOk').innerHTML = yesNoSpan(d.is_snr_above_noise_floor);

  document.getElementById('obstrCurrent').innerHTML = yesNoSpan(d.currently_obstructed);
  document.getElementById('obstrFraction').textContent = d.obstr_fraction != null ? `${(d.obstr_fraction * 100).toFixed(2)} %` : '–';
  document.getElementById('obstrDuration').textContent = d.obstruction_duration != null ? `${d.obstruction_duration.toFixed(1)} s` : '–';
  document.getElementById('obstrInterval').textContent = d.obstruction_interval != null ? `${d.obstruction_interval.toFixed(0)} s` : '–';

  renderAlerts(d.alerts_bitfield);
}

// ---------------------------------------------------------------------------
// Event log
// ---------------------------------------------------------------------------
function eventClassAndTag(type) {
  switch (type) {
    case 'disconnect':    return { cls: 'disc', tag: 'tag-err', label: 'Disconnect' };
    case 'latency_spike':  return { cls: 'warn', tag: 'tag-warn', label: 'Latency Spike' };
    case 'obstruction':    return { cls: 'warn', tag: 'tag-warn', label: 'Obstruction' };
    case 'speedtest':      return { cls: 'ok', tag: 'tag-ok', label: 'Speedtest' };
    default:                return { cls: '', tag: 'tag-warn', label: type };
  }
}

function formatEventMessage(ev) {
  let det = {};
  try { det = JSON.parse(ev.details || '{}'); } catch (_e) { /* details optional */ }
  switch (ev.type) {
    case 'disconnect':
      return `Connection lost${ev.duration_s ? ' · ' + ev.duration_s.toFixed(0) + ' s' : ''}${det.last_known_latency_ms ? ' · last latency ' + Math.round(det.last_known_latency_ms) + ' ms' : ''}`;
    case 'latency_spike':
      return `Latency spike${det.peak_ms ? ' · peak ' + Math.round(det.peak_ms) + ' ms' : ''}${ev.duration_s ? ' · ' + ev.duration_s.toFixed(0) + ' s' : ''}`;
    case 'speedtest':
      return `Speedtest · Down ${det.download_mbit?.toFixed(0) ?? '–'} Mbit/s · Up ${det.upload_mbit?.toFixed(0) ?? '–'} Mbit/s · RTT ${det.latency_ms ? Math.round(det.latency_ms) : '–'} ms`;
    default:
      return ev.type;
  }
}

async function loadEvents() {
  const list = document.getElementById('eventList');
  try {
    const events = await fetchEvents('7d');
    if (!events.length) {
      list.innerHTML = '<div class="event-empty">No events in the last 7 days.</div>';
      return;
    }
    list.innerHTML = events.slice(0, 100).map((ev) => {
      const { cls, tag, label } = eventClassAndTag(ev.type);
      return `<div class="event ${cls}">
        <span class="event-time">${formatLocalDateTime(ev.ts, { second: '2-digit' })}</span>
        <span class="event-msg">${formatEventMessage(ev)}</span>
        <span class="event-tag ${tag}">${label}</span>
      </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<div class="event-empty" style="color:#e0566e">Error loading events: ${e.message}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------
async function loadFooter(stats) {
  try {
    const summaryStats = stats || await fetchStatsSummary();
    // Only need the oldest timestamp here, not a full fetchMetrics('all')
    // (which would return only ~TARGET_POINTS anyway due to bucketing).
    const [from] = await resolveRange('all');
    const days = Math.max(1, Math.round((Date.now() / 1000 - from) / 86400));

    let stInfo = '';
    if (summaryStats.last_speedtest) {
      const ago = Math.round((Date.now() / 1000 - summaryStats.last_speedtest.ts) / 3600);
      stInfo = ` · speedtest ${ago}h ago (${summaryStats.last_speedtest.download_mbit.toFixed(0)}↓/${summaryStats.last_speedtest.upload_mbit.toFixed(0)}↑ Mbit/s)`;
    }
    document.getElementById('footerStats').textContent = `Data since ${days} days ago${stInfo}`;
  } catch (e) {
    console.error('Footer:', e);
  }
}

// ---------------------------------------------------------------------------
// WebSocket + live mode
// ---------------------------------------------------------------------------
function setConnStatus(kind, latencyMs) {
  const pill = document.getElementById('connStatus');
  const text = document.getElementById('connText');
  pill.classList.remove('offline', 'reconnecting');
  if (kind === 'online') {
    text.textContent = latencyMs != null ? `Online · ${Math.round(latencyMs)} ms` : 'Online';
  } else if (kind === 'reconnecting') {
    pill.classList.add('reconnecting');
    text.textContent = 'Reconnecting...';
  } else {
    pill.classList.add('offline');
    text.textContent = 'Offline';
  }
}

function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  state.ws = new WebSocket(`${protocol}//${location.host}/ws/live`);
  state.ws.onopen = () => setConnStatus('online');
  state.ws.onmessage = (e) => {
    try { handleLivePoint(JSON.parse(e.data)); } catch (_err) { /* malformed frame, ignore */ }
  };
  state.ws.onclose = () => {
    setConnStatus('reconnecting');
    if (state.wsReconnectTimer) clearTimeout(state.wsReconnectTimer);
    state.wsReconnectTimer = setTimeout(connectWebSocket, 5000);
  };
  state.ws.onerror = () => state.ws.close();
}

function pushLivePoint(dataset, point) {
  dataset.data.push(point);
  if (dataset.data.length > MAX_LIVE_POINTS + LIVE_TRIM_BATCH) {
    dataset.data.splice(0, dataset.data.length - MAX_LIVE_POINTS);
  }
}

function handleLivePoint(point) {
  setConnStatus('online', point.ping_latency_ms);

  if (point.ping_latency_ms != null) {
    document.getElementById('statLatency').textContent = `${Math.round(point.ping_latency_ms)} ms`;
  }
  if (point.ping_drop_rate != null) {
    const el = document.getElementById('statDrop');
    const pct = point.ping_drop_rate * 100;
    el.textContent = `${pct.toFixed(1)} %`;
    setStatLevel(el, pct, 2, 5);
  }
  if (point.downlink_bps != null) {
    document.getElementById('statDownload').textContent = `${(point.downlink_bps / 1e6).toFixed(0)} Mbit/s`;
  }
  if (point.uplink_bps != null) {
    document.getElementById('statUpload').textContent = `${(point.uplink_bps / 1e6).toFixed(0)} Mbit/s`;
  }

  if (state.liveMode) {
    if (state.ranges.drop === '1d') {
      pushLivePoint(state.charts.drop.data.datasets[0], { x: point.ts * 1000, y: point.ping_drop_rate != null ? point.ping_drop_rate * 100 : null });
      state.charts.drop.update('none');
    }
    if (state.ranges.latency === '1d') {
      pushLivePoint(state.charts.latency.data.datasets[0], { x: point.ts * 1000, y: point.ping_latency_ms != null && point.ping_latency_ms > 0 ? point.ping_latency_ms : null });
      state.charts.latency.update('none');
    }
    if (state.ranges.throughput === '1d') {
      pushLivePoint(state.charts.throughput.data.datasets[0], { x: point.ts * 1000, y: (point.downlink_bps ?? 0) / 1e6 });
      pushLivePoint(state.charts.throughput.data.datasets[1], { x: point.ts * 1000, y: (point.uplink_bps ?? 0) / 1e6 });
      state.charts.throughput.update('none');
    }
  }

  // Dish section + dome stay live regardless of chart live mode
  setCompass(point.direction_azimuth, point.direction_elevation);
  if (point.direction_azimuth != null || point.direction_elevation != null) {
    updateDome(point.direction_azimuth, point.direction_elevation);
  }
  if (point.direction_azimuth != null) document.getElementById('dishAzimuth').textContent = `${point.direction_azimuth.toFixed(1)} °`;
  if (point.direction_elevation != null) document.getElementById('dishElevation').textContent = `${point.direction_elevation.toFixed(1)} °`;
  if (point.uptime_s != null) document.getElementById('dishUptime').textContent = formatUptime(point.uptime_s);
  if (point.state) {
    const el = document.getElementById('dishState');
    el.textContent = point.state;
    el.style.color = point.state === 'CONNECTED' ? '#56d88f' : '#e0a830';
  }
  document.getElementById('gpsReady').innerHTML = yesNoSpan(point.gps_ready);
  document.getElementById('gpsEnabled').innerHTML = yesNoSpan(point.gps_enabled);
  if (point.gps_sats != null) document.getElementById('gpsSats').textContent = point.gps_sats;
  document.getElementById('snrOk').innerHTML = yesNoSpan(point.is_snr_above_noise_floor);
  document.getElementById('obstrCurrent').innerHTML = yesNoSpan(point.currently_obstructed);
  if (point.obstr_fraction != null) {
    document.getElementById('obstrFraction').textContent = `${(point.obstr_fraction * 100).toFixed(2)} %`;
    setDomeObstruction(point.obstr_fraction);
  }
  if (point.alerts_bitfield != null) {
    renderAlerts(point.alerts_bitfield);
  }
}

// ---------------------------------------------------------------------------
// Range buttons + live-mode toggle
// ---------------------------------------------------------------------------
function setLiveMode(on) {
  state.liveMode = on;
  const btn = document.getElementById('liveModeBtn');
  if (!btn) return;
  if (on) {
    btn.textContent = '⏸ Pause';
    btn.classList.add('live-active');
  } else {
    btn.textContent = '▶ Live';
    btn.classList.remove('live-active');
  }
}

function initRangeButtons() {
  document.querySelectorAll('.range-btns').forEach((group) => {
    const target = group.dataset.target;
    group.querySelectorAll('.rb').forEach((btn) => {
      btn.addEventListener('click', async () => {
        group.querySelectorAll('.rb').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const range = btn.dataset.range;

        if (range !== '1d' && target !== 'speedtest' && target !== 'peak' && target !== 'traffic') setLiveMode(false);

        if (target === 'drop') state.ranges.drop = range;
        if (target === 'latency') state.ranges.latency = range;
        if (target === 'throughput') state.ranges.throughput = range;
        if (target === 'speedtest') state.ranges.speedtest = range;
        if (target === 'peak') state.ranges.peak = range;
        if (target === 'traffic') state.ranges.traffic = range;

        try {
          if (target === 'speedtest') await loadSpeedtests(range);
          else if (target === 'peak') await loadPeakStats(range);
          else if (target === 'traffic') await loadTraffic(range);
          else await loadMetrics(range, target);
        } catch (e) {
          console.error(`Range switch (${target}=${range}) failed:`, e);
        }
      });
    });
    if (!group.querySelector('.rb.active')) {
      const def = group.querySelector('[data-range="1d"]');
      if (def) def.classList.add('active');
    }
  });

  const liveBtn = document.getElementById('liveModeBtn');
  if (liveBtn) {
    liveBtn.addEventListener('click', () => {
      const nowLive = !state.liveMode;
      setLiveMode(nowLive);
      if (nowLive) {
        if (state.ranges.drop === '1d') loadMetrics('1d', 'drop');
        if (state.ranges.latency === '1d') loadMetrics('1d', 'latency');
        if (state.ranges.throughput === '1d') loadMetrics('1d', 'throughput');
      }
    });
  }
}

// ---------------------------------------------------------------------------
// CSV export
// ---------------------------------------------------------------------------
function initExportButton() {
  const btn = document.getElementById('exportBtn');
  if (btn) {
    btn.addEventListener('click', () => {
      downloadCsv(state.ranges.drop).catch((e) => console.error('CSV export failed:', e));
    });
  }
}

// ---------------------------------------------------------------------------
// Admin panel (data management)
// ---------------------------------------------------------------------------
// Normalizes the different admin-DELETE response shapes into a single
// "rows deleted" count (metrics returns deleted_raw/deleted_minutely
// instead of deleted; all returns a per-table breakdown object).
function totalDeletedFromResponse(data) {
  if (typeof data.deleted === 'number') return data.deleted;
  if (data.deleted && typeof data.deleted === 'object') {
    return Object.values(data.deleted).reduce((a, b) => a + (Number(b) || 0), 0);
  }
  // Fallback for /api/admin/metrics, which returns deleted_raw/deleted_minutely instead of deleted
  const rawKeys = Object.keys(data).filter((k) => k.startsWith('deleted'));
  if (rawKeys.length) {
    return rawKeys.reduce((sum, k) => sum + (Number(data[k]) || 0), 0);
  }
  return 0;
}

function initAdminPanel() {
  const overlay = document.getElementById('adminOverlay');
  const openBtn = document.getElementById('adminBtn');
  const closeBtn = document.getElementById('adminClose');
  const fromInput = document.getElementById('adminFrom');
  const toInput = document.getElementById('adminTo');
  const resultEl = document.getElementById('adminResult');
  if (!overlay) return;

  const closeOverlay = () => overlay.classList.remove('open');
  openBtn.addEventListener('click', () => overlay.classList.add('open'));
  closeBtn.addEventListener('click', closeOverlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeOverlay(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && overlay.classList.contains('open')) closeOverlay(); });

  document.querySelectorAll('.admin-preset').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.admin-preset').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const days = parseInt(btn.dataset.days, 10);
      if (days === 0) {
        fromInput.value = '';
        toInput.value = '';
      } else {
        const cutoff = new Date(Date.now() - days * 86400 * 1000);
        toInput.value = toLocalDatetimeInputValue(cutoff);
        fromInput.value = '';
      }
    });
  });

  function showResult(msg, cls) {
    resultEl.textContent = msg;
    resultEl.className = 'admin-result ' + cls;
    resultEl.style.display = '';
  }

  document.querySelectorAll('.admin-action-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const target = btn.dataset.target;
      const from_ts = fromInput.value ? Math.floor(new Date(fromInput.value).getTime() / 1000) : null;
      const to_ts = toInput.value ? Math.floor(new Date(toInput.value).getTime() / 1000) : null;
      const labelEl = btn.querySelector('span:nth-child(2)');
      const label = labelEl ? labelEl.textContent : target;
      if (!confirm(`Delete "${label}"${from_ts || to_ts ? ' in the selected time range' : ' (EVERYTHING)'}?`)) return;

      const allButtons = document.querySelectorAll('.admin-action-btn');
      allButtons.forEach((b) => { b.disabled = true; });
      showResult('Deleting…', '');

      try {
        const data = await adminDelete(target, from_ts, to_ts);
        const total = totalDeletedFromResponse(data);
        showResult(`✓ ${total.toLocaleString('en-US')} rows deleted.`, 'ok');

        await Promise.allSettled([
          loadMetrics(state.ranges.drop, 'both'),
          loadSpeedtests(state.ranges.speedtest),
          loadTraffic(state.ranges.traffic),
          loadSummary().then(loadFooter),
          loadEvents(),
        ]);
      } catch (e) {
        showResult(`Error: ${e.message}`, 'err');
      } finally {
        allButtons.forEach((b) => { b.disabled = false; });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
async function init() {
  initCharts();
  initRangeButtons();
  initExportButton();
  initAdminPanel();
  setLiveMode(true);

  const results = await Promise.allSettled([
    loadMetrics('1d', 'both'),
    loadSpeedtests(state.ranges.speedtest),
    loadSummary(),
    loadWeather(),
    loadDishStatus(),
    loadEvents(),
    loadPeakStats(state.ranges.peak),
    loadTraffic(state.ranges.traffic),
  ]);

  const summaryResult = results[2];
  await loadFooter(summaryResult.status === 'fulfilled' ? summaryResult.value : null);

  connectWebSocket();

  setInterval(loadSummary, 30000);
  setInterval(loadWeather, 60000);
  setInterval(loadDishStatus, 30000);
  setInterval(loadEvents, 30000);
  setInterval(() => loadPeakStats(state.ranges.peak), 30000);
}

document.addEventListener('DOMContentLoaded', init);
