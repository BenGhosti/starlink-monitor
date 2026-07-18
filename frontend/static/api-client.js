/* api-client.js - data layer between the generic backend and dashboard.js.
 * The backend (api.py) only serves raw, filtered table rows. All display/
 * aggregation logic (range->resolution, bucketing, stats, CSV) lives here.
 * See BACKEND_GUIDE.md for the backend's side of the contract.
 */

const MAX_LIMIT = 50000;

// Every range targets the same point count; resolution (seconds/point) is
// derived dynamically from the span (span / TARGET_POINTS) instead of being
// hardcoded per range.
const TARGET_POINTS = 300;
const RAW_METRICS_INTERVAL_S = 2;      // collector poll interval (metrics_collector.py)
const RAW_SPEEDTEST_INTERVAL_S = 8 * 3600; // speedtest_runner.py interval

// Range key -> duration in seconds (null = "all", resolved dynamically
// from the oldest available data).
const RANGE_SECONDS = {
  '1d': 24 * 3600,
  '7d': 7 * 24 * 3600,
  '14d': 14 * 24 * 3600,
  '1m': 30 * 24 * 3600,
  '3m': 90 * 24 * 3600,
  '6m': 182 * 24 * 3600,
  '12m': 365 * 24 * 3600,
  all: null,
};

let _earliestTsCache = null; // resolved once per page load

async function getEarliestMetricsTs() {
  if (_earliestTsCache != null) return _earliestTsCache;
  for (const [table, tsField] of [['metrics_daily', 'ts_day'], ['metrics_hourly', 'ts_hour'], ['metrics_minutely', 'ts_minute'], ['metrics', 'ts']]) {
    const raw = await apiRaw(table, { order: 'asc', limit: 1 });
    if (raw.data.length) {
      _earliestTsCache = raw.data[0][tsField];
      return _earliestTsCache;
    }
  }
  _earliestTsCache = Math.floor(Date.now() / 1000) - RANGE_SECONDS['1d'];
  return _earliestTsCache;
}

// Returns [from, to, resolution_s] for a range key or explicit window.
// resolution_s targets ~TARGET_POINTS across the span, floored by
// minResolution so short ranges aren't resolved finer than raw data exists.
async function resolveRange(rangeKey, fromTs = null, toTs = null, minResolution = RAW_METRICS_INTERVAL_S) {
  const now = Math.floor(Date.now() / 1000);
  let from, to;
  if (fromTs != null && toTs != null) {
    from = fromTs; to = toTs;
  } else {
    const seconds = rangeKey in RANGE_SECONDS ? RANGE_SECONDS[rangeKey] : RANGE_SECONDS['1d'];
    to = now;
    from = seconds == null ? await getEarliestMetricsTs() : now - seconds;
  }
  const span = Math.max(1, to - from);
  const resolution_s = Math.max(minResolution, Math.ceil(span / TARGET_POINTS));
  return [from, to, resolution_s];
}

// Picks a Chart.js time-axis unit that fits the actual span.
function pickTimeAxisUnit(fromTs, toTs) {
  const span = Math.max(1, toTs - fromTs);
  const HOUR = 3600, DAY = 86400, MONTH = 30 * DAY, YEAR = 365 * DAY;
  if (span <= 6 * HOUR) return { unit: 'minute', tooltipFormat: 'dd.MM. HH:mm:ss', displayFormats: { minute: 'HH:mm' } };
  if (span <= 3 * DAY) return { unit: 'hour', tooltipFormat: 'dd.MM. HH:mm', displayFormats: { hour: 'HH:mm' } };
  if (span <= 60 * DAY) return { unit: 'day', tooltipFormat: 'dd.MM.yyyy', displayFormats: { day: 'dd.MM.' } };
  if (span <= 3 * YEAR) return { unit: 'month', tooltipFormat: 'MM.yyyy', displayFormats: { month: 'MM.yyyy' } };
  return { unit: 'year', tooltipFormat: 'yyyy', displayFormats: { year: 'yyyy' } };
}

// ---------------------------------------------------------------------------
// Generic backend access
// ---------------------------------------------------------------------------

// Session cookie is HttpOnly (see api.py); redirect centrally on 401 instead of per call-site.
function _redirectToLoginOn401(res) {
  if (res.status === 401) {
    window.location.href = '/login';
  }
  return res;
}

async function apiRaw(table, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) qs.set(k, v);
  }
  const res = _redirectToLoginOn401(await fetch(`/api/raw/${table}?${qs.toString()}`, { credentials: 'include' }));
  if (!res.ok) throw new Error(`HTTP ${res.status} on /api/raw/${table}`);
  return res.json();
}

async function apiLatest(table) {
  const res = _redirectToLoginOn401(await fetch(`/api/latest/${table}`, { credentials: 'include' }));
  if (!res.ok) throw new Error(`HTTP ${res.status} on /api/latest/${table}`);
  return res.json();
}

async function apiDeleteTable(table, from_ts, to_ts) {
  const res = _redirectToLoginOn401(await fetch(`/api/admin/${table}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ from_ts, to_ts }),
    credentials: 'include',
  }));
  if (!res.ok) {
    const txt = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${txt.slice(0, 200)}`);
  }
  return res.json();
}

// Timestamp column per table (must match the TABLES allowlist in api.py), used for cursor pagination below.
const TS_FIELDS = {
  metrics: 'ts', metrics_minutely: 'ts_minute', metrics_hourly: 'ts_hour', metrics_daily: 'ts_day',
  events: 'ts', speedtests: 'ts', weather: 'ts',
};

// Fetches ALL rows in [from, to] regardless of the per-request server limit
// (MAX_LIMIT in api.py). Without pagination, long ranges on dense tables
// would silently only return the oldest slice of the window. Pages forward
// by timestamp cursor until a page comes back shorter than the limit.
async function fetchAllRaw(table, { from, to, filter_col, filter_val } = {}) {
  const tsField = TS_FIELDS[table];
  let out = [];
  let cursor = from;
  for (let page = 0; page < 500; page++) { // safety cap against infinite loops
    const params = { from: cursor, to, limit: MAX_LIMIT, order: 'asc' };
    if (filter_col) { params.filter_col = filter_col; params.filter_val = filter_val; }
    const rows = (await apiRaw(table, params)).data;
    if (!rows.length) break;
    out = out.concat(rows);
    if (rows.length < MAX_LIMIT) break; // last page reached
    const lastTs = rows[rows.length - 1][tsField];
    if (lastTs == null || (to != null && lastTs >= to)) break;
    cursor = lastTs + 1; // continue right after the last row seen
  }
  return out;
}

// ---------------------------------------------------------------------------
// Metrics: range -> resolution -> bucket aggregation (client-side, was SQL
// in the backend before). Returns:
// { from, to, resolution_s, data: [{ts, ping_drop_rate, ping_latency_ms, ...}] }
// ---------------------------------------------------------------------------
function avgOf(arr) { return arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null; }

async function fetchMetrics(range) {
  const [from, to, resolution_s] = await resolveRange(range);
  let data;

  if (resolution_s <= 2) {
    const rows = await fetchAllRaw('metrics', { from, to });
    data = rows.map((r) => ({
      ts: r.ts, ping_drop_rate: r.ping_drop_rate, ping_latency_ms: r.ping_latency_ms,
      obstr_fraction: r.obstr_fraction, downlink_bps: r.downlink_bps, uplink_bps: r.uplink_bps,
      state: r.state, snr: r.snr,
      min_latency: r.ping_latency_ms, max_latency: r.ping_latency_ms,
      min_downlink: r.downlink_bps, max_downlink: r.downlink_bps,
      min_uplink: r.uplink_bps, max_uplink: r.uplink_bps,
    }));
  } else if (resolution_s <= 60) {
    const rows = await fetchAllRaw('metrics_minutely', { from, to });
    data = rows.map((r) => ({
      ts: r.ts_minute, ping_drop_rate: r.avg_ping_drop_rate, ping_latency_ms: r.avg_ping_latency_ms,
      obstr_fraction: r.avg_obstr_fraction, downlink_bps: r.avg_downlink_bps, uplink_bps: r.avg_uplink_bps,
      state: null, snr: null,
      min_latency: r.min_ping_latency_ms, max_latency: r.max_ping_latency_ms,
      min_downlink: r.min_downlink_bps, max_downlink: r.max_downlink_bps,
      min_uplink: r.min_uplink_bps, max_uplink: r.max_uplink_bps,
    }));
  } else {
    // Bucket minutely rows into wider buckets; fall back to metrics_hourly
    // before the oldest minutely row, then metrics_daily before the oldest
    // hourly row (see HOURLY_ROLLUP_DAYS in cleanup.py).
    const minuteRows = await fetchAllRaw('metrics_minutely', { from, to });
    const oldestMinute = (await apiRaw('metrics_minutely', { limit: 1, order: 'asc' })).data[0];
    const oldestMinuteTs = oldestMinute ? oldestMinute.ts_minute : Infinity;
    const oldestHour = (await apiRaw('metrics_hourly', { limit: 1, order: 'asc' })).data[0];
    const oldestHourTs = oldestHour ? oldestHour.ts_hour : Infinity;

    const buckets = new Map();
    const bucketOf = (ts) => {
      const bTs = Math.floor(ts / resolution_s) * resolution_s;
      if (!buckets.has(bTs)) {
        buckets.set(bTs, {
          drop: [], latency: [], obstr: [], down: [], up: [],
          minLat: null, maxLat: null, minDown: null, maxDown: null, minUp: null, maxUp: null,
        });
      }
      return buckets.get(bTs);
    };
    const mergeMinMax = (b, key, min, max) => {
      if (min != null) b[`min${key}`] = b[`min${key}`] == null ? min : Math.min(b[`min${key}`], min);
      if (max != null) b[`max${key}`] = b[`max${key}`] == null ? max : Math.max(b[`max${key}`], max);
    };

    for (const r of minuteRows) {
      const b = bucketOf(r.ts_minute);
      if (r.avg_ping_drop_rate != null) b.drop.push(r.avg_ping_drop_rate);
      if (r.avg_ping_latency_ms != null) b.latency.push(r.avg_ping_latency_ms);
      if (r.avg_obstr_fraction != null) b.obstr.push(r.avg_obstr_fraction);
      if (r.avg_downlink_bps != null) b.down.push(r.avg_downlink_bps);
      if (r.avg_uplink_bps != null) b.up.push(r.avg_uplink_bps);
      mergeMinMax(b, 'Lat', r.min_ping_latency_ms, r.max_ping_latency_ms);
      mergeMinMax(b, 'Down', r.min_downlink_bps, r.max_downlink_bps);
      mergeMinMax(b, 'Up', r.min_uplink_bps, r.max_uplink_bps);
    }

    if (from < oldestMinuteTs) {
      const hourRows = await fetchAllRaw('metrics_hourly', { from, to: Math.min(to, oldestMinuteTs) });
      for (const r of hourRows) {
        if (r.ts_hour >= oldestMinuteTs) continue;
        const b = bucketOf(r.ts_hour);
        if (r.avg_ping_drop_rate != null) b.drop.push(r.avg_ping_drop_rate);
        if (r.avg_ping_latency_ms != null) b.latency.push(r.avg_ping_latency_ms);
        if (r.avg_obstr_fraction != null) b.obstr.push(r.avg_obstr_fraction);
        if (r.avg_downlink_bps != null) b.down.push(r.avg_downlink_bps);
        if (r.avg_uplink_bps != null) b.up.push(r.avg_uplink_bps);
        mergeMinMax(b, 'Lat', null, r.max_ping_latency_ms);
      }
    }

    if (from < oldestHourTs) {
      const dayRows = await fetchAllRaw('metrics_daily', { from, to: Math.min(to, oldestHourTs) });
      for (const r of dayRows) {
        if (r.ts_day >= oldestHourTs) continue;
        const b = bucketOf(r.ts_day);
        if (r.avg_ping_drop_rate != null) b.drop.push(r.avg_ping_drop_rate);
        if (r.avg_ping_latency_ms != null) b.latency.push(r.avg_ping_latency_ms);
        if (r.avg_obstr_fraction != null) b.obstr.push(r.avg_obstr_fraction);
        if (r.avg_downlink_bps != null) b.down.push(r.avg_downlink_bps);
        if (r.avg_uplink_bps != null) b.up.push(r.avg_uplink_bps);
        mergeMinMax(b, 'Lat', null, r.max_ping_latency_ms);
      }
    }

    data = Array.from(buckets.entries()).sort((a, b) => a[0] - b[0]).map(([ts, b]) => ({
      ts,
      ping_drop_rate: avgOf(b.drop), ping_latency_ms: avgOf(b.latency), obstr_fraction: avgOf(b.obstr),
      downlink_bps: avgOf(b.down), uplink_bps: avgOf(b.up), state: null, snr: null,
      min_latency: b.minLat, max_latency: b.maxLat,
      min_downlink: b.minDown, max_downlink: b.maxDown,
      min_uplink: b.minUp, max_uplink: b.maxUp,
    }));
  }

  return { from, to, resolution_s, data };
}

// ---------------------------------------------------------------------------
// Traffic (down/up data volume in GB) - same table cascade as fetchMetrics(),
// but sums bytes instead of averaging. Raw table (range='1d' only) computes
// bytes per row as bps * RAW_METRICS_INTERVAL_S / 8; from metrics_minutely
// up, down_bytes/up_bytes are already in the schema (see collector/db.py).
// Bucket width is coarser than the line charts' TARGET_POINTS for readability.
const TRAFFIC_BUCKET_S = {
  '1d': 3600, '7d': 86400, '14d': 86400, '1m': 86400,
  '6m': 7 * 86400, '12m': 30 * 86400,
};

async function fetchTraffic(range) {
  const [from, to] = await resolveRange(range);
  const span = Math.max(1, to - from);
  const bucket_s = TRAFFIC_BUCKET_S[range] || Math.max(30 * 86400, Math.ceil(span / 24));

  const buckets = new Map();
  const addBytes = (ts, downBytes, upBytes) => {
    const bTs = Math.floor(ts / bucket_s) * bucket_s;
    const b = buckets.get(bTs) || { down: 0, up: 0 };
    if (downBytes != null) b.down += downBytes;
    if (upBytes != null) b.up += upBytes;
    buckets.set(bTs, b);
  };

  if (range === '1d') {
    const rows = await fetchAllRaw('metrics', { from, to });
    for (const r of rows) {
      addBytes(
        r.ts,
        r.downlink_bps != null ? (r.downlink_bps * RAW_METRICS_INTERVAL_S) / 8 : null,
        r.uplink_bps != null ? (r.uplink_bps * RAW_METRICS_INTERVAL_S) / 8 : null,
      );
    }
  } else {
    // Same cascade as fetchMetrics(): minutely -> hourly -> daily.
    const oldestMinute = (await apiRaw('metrics_minutely', { limit: 1, order: 'asc' })).data[0];
    const oldestMinuteTs = oldestMinute ? oldestMinute.ts_minute : Infinity;
    const oldestHour = (await apiRaw('metrics_hourly', { limit: 1, order: 'asc' })).data[0];
    const oldestHourTs = oldestHour ? oldestHour.ts_hour : Infinity;

    const minuteRows = await fetchAllRaw('metrics_minutely', { from: Math.max(from, oldestMinuteTs), to });
    for (const r of minuteRows) addBytes(r.ts_minute, r.down_bytes, r.up_bytes);

    if (from < oldestMinuteTs) {
      const hourRows = await fetchAllRaw('metrics_hourly', { from, to: Math.min(to, oldestMinuteTs) });
      for (const r of hourRows) {
        if (r.ts_hour >= oldestMinuteTs) continue;
        addBytes(r.ts_hour, r.down_bytes, r.up_bytes);
      }
    }
    if (from < oldestHourTs) {
      const dayRows = await fetchAllRaw('metrics_daily', { from, to: Math.min(to, oldestHourTs) });
      for (const r of dayRows) {
        if (r.ts_day >= oldestHourTs) continue;
        addBytes(r.ts_day, r.down_bytes, r.up_bytes);
      }
    }
  }

  const data = Array.from(buckets.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([ts, b]) => ({ ts, down_gb: b.down / 1e9, up_gb: b.up / 1e9 }));

  const totals = data.reduce(
    (acc, d) => ({ down_gb: acc.down_gb + d.down_gb, up_gb: acc.up_gb + d.up_gb }),
    { down_gb: 0, up_gb: 0 },
  );

  return { from, to, bucket_s, data, totals };
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
async function fetchEvents(range = '7d', type = null) {
  const [from, to] = await resolveRange(range);
  const rows = await fetchAllRaw('events', {
    from, to,
    filter_col: type ? 'type' : undefined,
    filter_val: type ? type : undefined,
  });
  rows.sort((a, b) => b.ts - a.ts); // newest first (fetchAllRaw returns ascending)
  return rows;
}

// ---------------------------------------------------------------------------
// Speedtests: same target-point-count logic as metrics. Bucket width comes
// from resolveRange(), floored at the speedtest interval itself (8h).
// ---------------------------------------------------------------------------
async function fetchSpeedtests(range = 'all') {
  const [from, to, resolution_s] = await resolveRange(range, null, null, RAW_SPEEDTEST_INTERVAL_S);
  const rows = await fetchAllRaw('speedtests', { from, to });

  if (resolution_s <= RAW_SPEEDTEST_INTERVAL_S) {
    const data = rows.map((r) => ({
      ts: r.ts, download_mbit: r.download_mbit, upload_mbit: r.upload_mbit,
      latency_ms: r.latency_ms, jitter_ms: r.jitter_ms, server: r.server,
      sample_count: 1,
      min_download_mbit: r.download_mbit, max_download_mbit: r.download_mbit,
      min_upload_mbit: r.upload_mbit, max_upload_mbit: r.upload_mbit,
    }));
    return { from, to, resolution_s, data };
  }

  const buckets = new Map();
  for (const r of rows) {
    const bTs = Math.floor(r.ts / resolution_s) * resolution_s;
    if (!buckets.has(bTs)) buckets.set(bTs, { down: [], up: [], lat: [], jit: [] });
    const b = buckets.get(bTs);
    if (r.download_mbit != null) b.down.push(r.download_mbit);
    if (r.upload_mbit != null) b.up.push(r.upload_mbit);
    if (r.latency_ms != null) b.lat.push(r.latency_ms);
    if (r.jitter_ms != null) b.jit.push(r.jitter_ms);
  }
  const data = Array.from(buckets.entries()).sort((a, b) => a[0] - b[0]).map(([ts, b]) => ({
    ts, download_mbit: avgOf(b.down), upload_mbit: avgOf(b.up), latency_ms: avgOf(b.lat), jitter_ms: avgOf(b.jit),
    server: null, sample_count: b.down.length,
    min_download_mbit: b.down.length ? Math.min(...b.down) : null,
    max_download_mbit: b.down.length ? Math.max(...b.down) : null,
    min_upload_mbit: b.up.length ? Math.min(...b.up) : null,
    max_upload_mbit: b.up.length ? Math.max(...b.up) : null,
  }));
  return { from, to, resolution_s, data };
}

// ---------------------------------------------------------------------------
// Dish status, weather, stats summary
// ---------------------------------------------------------------------------
async function fetchDishStatus() {
  const [latest, info] = await Promise.all([apiLatest('metrics'), apiLatest('dish_info')]);
  if ((!latest || !latest.ts) && (!info || !info.id)) return {};
  const result = { ...(latest || {}) };
  if (info && info.id) {
    result.device_id = info.device_id;
    result.hardware_version = info.hardware_version;
    result.software_version = info.software_version;
    result.alerts_bitfield = info.alerts_bitfield;
    result.info_last_seen_ts = info.last_seen_ts;
  }
  return result;
}

async function fetchWeatherCurrent() {
  return apiLatest('weather');
}

async function fetchWeatherWarnings() {
  const raw = await apiRaw('weather', { order: 'desc', limit: 500 });
  return raw.data.filter((r) => r.warning).slice(0, 20);
}

async function fetchStatsSummary() {
  const now = Math.floor(Date.now() / 1000);
  const dayAgo = now - 24 * 3600;

  const [metricsRows, disconnectRows, lastSpeedtestRaw] = await Promise.all([
    fetchAllRaw('metrics', { from: dayAgo, to: now }),
    fetchAllRaw('events', { from: dayAgo, to: now, filter_col: 'type', filter_val: 'disconnect' }),
    apiRaw('speedtests', { order: 'desc', limit: 1 }),
  ]);

  const lat = metricsRows.filter((m) => m.ping_latency_ms != null).map((m) => m.ping_latency_ms);
  const drop = metricsRows.filter((m) => m.ping_drop_rate != null).map((m) => m.ping_drop_rate);
  const downtimeS = disconnectRows.reduce((sum, e) => sum + (e.duration_s || 0), 0);
  let uptimePct = 100.0;
  if (downtimeS) uptimePct = Math.max(0, 100 - (downtimeS / 86400) * 100);

  return {
    avg_latency_ms_24h: avgOf(lat),
    avg_drop_rate_24h: avgOf(drop),
    disconnects_24h: disconnectRows.length,
    uptime_pct_24h: Math.round(uptimePct * 1000) / 1000,
    last_speedtest: lastSpeedtestRaw.data[0] || null,
  };
}

// ---------------------------------------------------------------------------
// CSV export (client-side Blob, was a backend StreamingResponse before)
// ---------------------------------------------------------------------------
function csvEscape(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function downloadCsv(range) {
  const [from, to] = await resolveRange(range);
  const [metricsJson, events, speedtests] = await Promise.all([
    fetchMetrics(range),
    fetchAllRaw('events', { from, to }),
    fetchAllRaw('speedtests', { from, to }),
  ]);

  const rows = [['kind', 'ts', 'field_1', 'field_2', 'field_3', 'field_4', 'field_5', 'field_6']];
  for (const r of metricsJson.data) {
    rows.push(['metric', r.ts, r.ping_drop_rate, r.ping_latency_ms, r.obstr_fraction, r.downlink_bps, r.uplink_bps, r.state]);
  }
  for (const r of events) {
    rows.push(['event', r.ts, r.type, r.duration_s, r.details, '', '']);
  }
  for (const r of speedtests) {
    rows.push(['speedtest', r.ts, r.download_mbit, r.upload_mbit, r.latency_ms, r.jitter_ms, r.server, '']);
  }

  const csv = rows.map((row) => row.map(csvEscape).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `starlink_export_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Peak values (best latency, highest down/upload) from the 2s live samples.
// fetchMetrics() already returns min_latency/max_downlink/max_uplink per
// bucket, so we just take the extremum across buckets instead of re-loading
// the full raw history.
// ---------------------------------------------------------------------------
async function fetchPeakStats(range) {
  const json = await fetchMetrics(range);
  let bestLatencyMs = null, peakDownloadBps = null, peakUploadBps = null;
  for (const d of json.data) {
    // Starlink reports -1 (or other <=0 values) as a "no data" sentinel for
    // latency, not a real measurement - exclude those or "best latency"
    // ends up showing an impossible negative number.
    const lat = d.min_latency ?? d.ping_latency_ms;
    if (lat != null && lat > 0 && (bestLatencyMs == null || lat < bestLatencyMs)) bestLatencyMs = lat;
    const down = d.max_downlink ?? d.downlink_bps;
    if (down != null && (peakDownloadBps == null || down > peakDownloadBps)) peakDownloadBps = down;
    const up = d.max_uplink ?? d.uplink_bps;
    if (up != null && (peakUploadBps == null || up > peakUploadBps)) peakUploadBps = up;
  }
  return {
    from: json.from, to: json.to,
    best_latency_ms: bestLatencyMs,
    peak_download_bps: peakDownloadBps,
    peak_upload_bps: peakUploadBps,
  };
}

// ---------------------------------------------------------------------------
// Admin delete (the metrics+metrics_minutely cascade is UI logic, so it lives here, not in the backend)
// ---------------------------------------------------------------------------
async function adminDelete(target, from_ts, to_ts) {
  if (target === 'metrics') {
    const [a, b] = await Promise.all([
      apiDeleteTable('metrics', from_ts, to_ts),
      apiDeleteTable('metrics_minutely', from_ts, to_ts),
    ]);
    return { deleted: (a.deleted || 0) + (b.deleted || 0) };
  }
  if (target === 'all') {
    const tables = ['events', 'metrics', 'metrics_minutely', 'metrics_hourly', 'metrics_daily', 'speedtests', 'weather'];
    const results = await Promise.all(tables.map((t) => apiDeleteTable(t, from_ts, to_ts)));
    const deleted = {};
    tables.forEach((t, i) => { deleted[t] = results[i].deleted; });
    return { deleted };
  }
  return apiDeleteTable(target, from_ts, to_ts);
}