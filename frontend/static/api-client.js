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
let _earliestTsPromise = null; // de-dupes concurrent callers while resolving

// Shared cache for "oldest row of table X" lookups - fetchMetrics() and
// fetchTraffic() both need the oldest metrics_minutely/metrics_hourly row
// to decide where to fall back to the next tier, and previously each
// re-fetched that separately (and sequentially) on every single call. On a
// high-latency connection (reverse proxy / public domain) those extra
// round trips are the main reason charts feel slow to load.
const _oldestRowCache = new Map();
async function _getOldestRow(table, tsField) {
  if (_oldestRowCache.has(table)) return _oldestRowCache.get(table);
  const raw = await apiRaw(table, { limit: 1, order: 'asc' });
  const row = raw.data[0];
  const ts = row ? row[tsField] : Infinity;
  _oldestRowCache.set(table, ts);
  return ts;
}

// Short-lived memoization for fetchMetrics(range): dashboard.js calls it
// from multiple places on initial load (charts + peak stats) for the same
// range at nearly the same time - without this they'd each independently
// refetch and reprocess the exact same data over the network.
const _metricsMemo = new Map(); // range -> { promise, ts }
const METRICS_MEMO_TTL_MS = 8000;

function _invalidateCaches() {
  _earliestTsCache = null;
  _earliestTsPromise = null;
  _oldestRowCache.clear();
  _metricsMemo.clear();
}

// Priority order matters (daily is checked first): a table earlier in this
// list being non-empty means later tables are irrelevant even if they also
// have rows (e.g. metrics_daily existing means metrics_hourly's oldest row
// was already rolled up and deleted, so it's not the true "earliest" data).
const EARLIEST_TS_TABLES = [
  ['metrics_daily', 'ts_day'], ['metrics_hourly', 'ts_hour'],
  ['metrics_minutely', 'ts_minute'], ['metrics', 'ts'],
];

async function getEarliestMetricsTs() {
  if (_earliestTsCache != null) return _earliestTsCache;
  if (_earliestTsPromise) return _earliestTsPromise; // already in flight - don't fire it again

  _earliestTsPromise = (async () => {
    // Fire all 4 lookups in parallel instead of stopping at the first
    // non-empty result one round trip at a time - on a high-latency
    // connection that's up to 4x slower for no reason, since we need to
    // check every table's presence anyway in the worst case (fresh install).
    const results = await Promise.all(
      EARLIEST_TS_TABLES.map(([table]) => apiRaw(table, { order: 'asc', limit: 1 }))
    );
    for (let i = 0; i < EARLIEST_TS_TABLES.length; i++) {
      const [, tsField] = EARLIEST_TS_TABLES[i];
      if (results[i].data.length) {
        _earliestTsCache = results[i].data[0][tsField];
        return _earliestTsCache;
      }
    }
    _earliestTsCache = Math.floor(Date.now() / 1000) - RANGE_SECONDS['1d'];
    return _earliestTsCache;
  })();

  return _earliestTsPromise;
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
  const cached = _metricsMemo.get(range);
  if (cached && Date.now() - cached.ts < METRICS_MEMO_TTL_MS) return cached.promise;
  const promise = _fetchMetricsUncached(range);
  _metricsMemo.set(range, { promise, ts: Date.now() });
  try {
    return await promise;
  } catch (e) {
    _metricsMemo.delete(range); // don't cache a failed request
    throw e;
  }
}

async function _fetchMetricsUncached(range) {
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
    const [minuteRows, oldestMinuteTs, oldestHourTs] = await Promise.all([
      fetchAllRaw('metrics_minutely', { from, to }),
      _getOldestRow('metrics_minutely', 'ts_minute'),
      _getOldestRow('metrics_hourly', 'ts_hour'),
    ]);

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
    const [oldestMinuteTs, oldestHourTs] = await Promise.all([
      _getOldestRow('metrics_minutely', 'ts_minute'),
      _getOldestRow('metrics_hourly', 'ts_hour'),
    ]);

    const minuteFrom = Number.isFinite(oldestMinuteTs) ? Math.max(from, oldestMinuteTs) : from;
    const minuteRows = await fetchAllRaw('metrics_minutely', { from: minuteFrom, to });
    for (const r of minuteRows) addBytes(r.ts_minute, r.down_bytes, r.up_bytes);

    if (Number.isFinite(oldestMinuteTs) && from < oldestMinuteTs) {
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

// Dish states that mean "the link is currently down" (used for ongoing-
// outage detection in fetchStatsSummary). UNKNOWN/null are deliberately
// excluded - some firmware versions report them during normal operation.
const DOWN_STATES = new Set([
  'SEARCHING', 'STOWED', 'BOOTING', 'THERMAL_SHUTDOWN',
  'NO_SIGNAL', 'UPDATE', 'FACTORY_RESET', 'BOOTLOADER',
]);

async function fetchStatsSummary() {
  const now = Math.floor(Date.now() / 1000);
  const dayAgo = now - 24 * 3600;

  // 24h averages come from metrics_minutely (1,440 rows) instead of the raw
  // 2s table (43,200 rows) - called every 30s, the raw variant wasted
  // megabytes of traffic per call. Fresh installs (<1h old) may not have
  // minutely rows yet - fall back to raw for that window.
  const [minuteRows, disconnectRows, lastSpeedtestRaw] = await Promise.all([
    fetchAllRaw('metrics_minutely', { from: dayAgo, to: now }),
    fetchAllRaw('events', { from: dayAgo, to: now, filter_col: 'type', filter_val: 'disconnect' }),
    apiRaw('speedtests', { order: 'desc', limit: 1 }),
  ]);

  let lat, drop, obstr;
  if (minuteRows.length) {
    lat = minuteRows.filter((m) => m.avg_ping_latency_ms != null).map((m) => m.avg_ping_latency_ms);
    drop = minuteRows.filter((m) => m.avg_ping_drop_rate != null).map((m) => m.avg_ping_drop_rate);
    obstr = minuteRows.filter((m) => m.avg_obstr_fraction != null).map((m) => m.avg_obstr_fraction);
  } else {
    const rawRows = await fetchAllRaw('metrics', { from: dayAgo, to: now });
    lat = rawRows.filter((m) => m.ping_latency_ms != null && m.ping_latency_ms > 0).map((m) => m.ping_latency_ms);
    drop = rawRows.filter((m) => m.ping_drop_rate != null).map((m) => m.ping_drop_rate);
    obstr = rawRows.filter((m) => m.obstr_fraction != null).map((m) => m.obstr_fraction);
  }

  // Only the part of each (ended) event inside the 24h window counts - an
  // event whose duration started before the window must not fully count.
  const endedDowntimeS = disconnectRows.reduce(
    (sum, e) => sum + Math.min(e.duration_s || 0, e.ts - dayAgo),
    0,
  );

  // Ongoing outage: no "ended" event exists yet, but the dish state in the
  // newest raw row tells us the link is currently down. Find the last
  // CONNECTED row to know when it went down.
  const [latestRaw, lastConnectedRaw] = await Promise.all([
    apiRaw('metrics', { order: 'desc', limit: 1 }),
    apiRaw('metrics', { filter_col: 'state', filter_val: 'CONNECTED', order: 'desc', limit: 1 }),
  ]);
  const latestState = latestRaw.data[0]?.state;
  let ongoingDowntimeS = 0;
  if (latestState && DOWN_STATES.has(latestState)) {
    const lastConnTs = lastConnectedRaw.data[0]?.ts;
    if (lastConnTs == null) {
      ongoingDowntimeS = 86400; // down for the entire window
    } else {
      ongoingDowntimeS = Math.max(0, Math.min(now - dayAgo, now - lastConnTs));
    }
  }

  // Active hardware alert count from the latest alerts_bitfield (same
  // bit mapping as dashboard.js ALERT_BITS) - feeds the Dish Health Score.
  const bitfield = latestRaw.data[0]?.alerts_bitfield;
  let activeAlerts = 0;
  if (bitfield != null) {
    for (let b = 0; b < 12; b++) {
      if (bitfield & (1 << b)) activeAlerts++;
    }
  }

  const totalDowntimeS = endedDowntimeS + ongoingDowntimeS;
  let uptimePct = 100.0;
  if (totalDowntimeS) uptimePct = Math.max(0, 100 - (totalDowntimeS / 86400) * 100);

  return {
    avg_latency_ms_24h: avgOf(lat),
    avg_drop_rate_24h: avgOf(drop),
    avg_obstr_fraction_24h: avgOf(obstr),
    active_alerts: activeAlerts,
    disconnects_24h: disconnectRows.length + (ongoingDowntimeS > 0 ? 1 : 0),
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

// Export granularity per range: the finest tier whose row count stays
// practical. Raw 2s rows exist for 90 days (cleanup.py), minutely for ~2
// years, hourly/daily beyond. Events + speedtests are always exported raw.
const CSV_TIER_BY_RANGE = {
  '1d': 'raw', '7d': 'minutely', '14d': 'minutely', '1m': 'minutely',
  '3m': 'minutely', '6m': 'hourly', '12m': 'hourly', all: 'hourly',
};

async function _fetchMetricsRowsForCsv(range) {
  const [from, to] = await resolveRange(range);
  const tier = CSV_TIER_BY_RANGE[range] || 'hourly';
  const rows = [];

  if (tier === 'raw') {
    const raw = await fetchAllRaw('metrics', { from, to });
    for (const r of raw) {
      rows.push({ kind: 'metric_raw', ts: r.ts, drop: r.ping_drop_rate, lat: r.ping_latency_ms, obstr: r.obstr_fraction, down: r.downlink_bps, up: r.uplink_bps, state: r.state });
    }
  } else if (tier === 'minutely') {
    const oldestMinuteTs = await _getOldestRow('metrics_minutely', 'ts_minute');
    const minFrom = Number.isFinite(oldestMinuteTs) ? Math.max(from, oldestMinuteTs) : from;
    const min = await fetchAllRaw('metrics_minutely', { from: minFrom, to });
    for (const r of min) {
      rows.push({ kind: 'metric_min', ts: r.ts_minute, drop: r.avg_ping_drop_rate, lat: r.avg_ping_latency_ms, obstr: r.avg_obstr_fraction, down: r.avg_downlink_bps, up: r.avg_uplink_bps });
    }
    if (Number.isFinite(oldestMinuteTs) && from < oldestMinuteTs) {
      const hrs = await fetchAllRaw('metrics_hourly', { from, to: Math.min(to, oldestMinuteTs) });
      for (const r of hrs) {
        if (r.ts_hour >= oldestMinuteTs) continue;
        rows.push({ kind: 'metric_hour', ts: r.ts_hour, drop: r.avg_ping_drop_rate, lat: r.avg_ping_latency_ms, obstr: r.avg_obstr_fraction, down: r.avg_downlink_bps, up: r.avg_uplink_bps });
      }
    }
  } else {
    const oldestHourTs = await _getOldestRow('metrics_hourly', 'ts_hour');
    const hourFrom = Number.isFinite(oldestHourTs) ? Math.max(from, oldestHourTs) : from;
    const hrs = await fetchAllRaw('metrics_hourly', { from: hourFrom, to });
    for (const r of hrs) {
      rows.push({ kind: 'metric_hour', ts: r.ts_hour, drop: r.avg_ping_drop_rate, lat: r.avg_ping_latency_ms, obstr: r.avg_obstr_fraction, down: r.avg_downlink_bps, up: r.avg_uplink_bps });
    }
    if (Number.isFinite(oldestHourTs) && from < oldestHourTs) {
      const days = await fetchAllRaw('metrics_daily', { from, to: Math.min(to, oldestHourTs) });
      for (const r of days) {
        if (r.ts_day >= oldestHourTs) continue;
        rows.push({ kind: 'metric_day', ts: r.ts_day, drop: r.avg_ping_drop_rate, lat: r.avg_ping_latency_ms, obstr: r.avg_obstr_fraction, down: r.avg_downlink_bps, up: r.avg_uplink_bps });
      }
    }
  }

  return rows;
}

async function downloadCsv(range) {
  const [from, to] = await resolveRange(range);
  const [metricRows, events, speedtests] = await Promise.all([
    _fetchMetricsRowsForCsv(range),
    fetchAllRaw('events', { from, to }),
    fetchAllRaw('speedtests', { from, to }),
  ]);

  const rows = [['kind', 'ts', 'field_1', 'field_2', 'field_3', 'field_4', 'field_5', 'field_6']];
  for (const r of metricRows) {
    rows.push([r.kind, r.ts, r.drop, r.lat, r.obstr, r.down, r.up, r.state ?? '']);
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
// Jitter: mean |Δ| between consecutive latency samples, computed at runtime
// (no schema change). True jitter needs the raw 2s samples, so 1d uses those
// exactly. Longer ranges approximate with |Δ| between consecutive
// minute/hour/day averages - the UI labels the basis. Deltas across gaps
// bigger than the entry's tolerance (e.g. an outage) break the chain: that's
// downtime, not jitter.
// ---------------------------------------------------------------------------
const JITTER_TOL_S = { raw: 15, minute: 300, hour: 7200, day: 3 * 86400 };
const JITTER_BASIS_LABEL = { raw: '2s samples', minute: 'minute avg', hour: 'hour avg', day: 'day avg' };

async function fetchJitter(range) {
  const [from, to] = await resolveRange(range);
  const span = Math.max(1, to - from);
  const bucketS = Math.max(120, Math.ceil(span / TARGET_POINTS));
  const buckets = new Map();
  const entries = []; // { ts, lat, basis } chronological

  if (range === '1d') {
    const rows = await fetchAllRaw('metrics', { from, to });
    for (const r of rows) {
      if (r.ping_latency_ms != null && r.ping_latency_ms > 0) {
        entries.push({ ts: r.ts, lat: r.ping_latency_ms, basis: 'raw' });
      }
    }
  } else {
    const oldestMinuteTs = await _getOldestRow('metrics_minutely', 'ts_minute');
    const minFrom = Number.isFinite(oldestMinuteTs) ? Math.max(from, oldestMinuteTs) : from;
    const minRows = await fetchAllRaw('metrics_minutely', { from: minFrom, to });
    for (const r of minRows) {
      if (r.avg_ping_latency_ms != null && r.avg_ping_latency_ms > 0) {
        entries.push({ ts: r.ts_minute, lat: r.avg_ping_latency_ms, basis: 'minute' });
      }
    }
    if (Number.isFinite(oldestMinuteTs) && from < oldestMinuteTs) {
      const oldestHourTs = await _getOldestRow('metrics_hourly', 'ts_hour');
      const hourRows = await fetchAllRaw('metrics_hourly', { from, to: Math.min(to, oldestMinuteTs) });
      for (const r of hourRows) {
        if (r.ts_hour < oldestMinuteTs && r.avg_ping_latency_ms != null && r.avg_ping_latency_ms > 0) {
          entries.push({ ts: r.ts_hour, lat: r.avg_ping_latency_ms, basis: 'hour' });
        }
      }
      if (Number.isFinite(oldestHourTs) && from < oldestHourTs) {
        const dayRows = await fetchAllRaw('metrics_daily', { from, to: Math.min(to, oldestHourTs) });
        for (const r of dayRows) {
          if (r.ts_day < oldestHourTs && r.avg_ping_latency_ms != null && r.avg_ping_latency_ms > 0) {
            entries.push({ ts: r.ts_day, lat: r.avg_ping_latency_ms, basis: 'day' });
          }
        }
      }
    }
  }

  entries.sort((a, b) => a.ts - b.ts);

  let coarsest = 'raw';
  let prev = null;
  for (const e of entries) {
    if (prev != null && e.ts - prev.ts <= JITTER_TOL_S[e.basis]) {
      const bTs = Math.floor(e.ts / bucketS) * bucketS;
      const b = buckets.get(bTs) || { s: 0, n: 0 };
      b.s += Math.abs(e.lat - prev.lat);
      b.n++;
      buckets.set(bTs, b);
    }
    if (JITTER_TOL_S[e.basis] > JITTER_TOL_S[coarsest]) coarsest = e.basis;
    prev = e;
  }

  const data = Array.from(buckets.entries())
    .sort((a, b) => a[0] - b[0])
    .map(([ts, b]) => ({ x: ts * 1000, y: b.n ? b.s / b.n : null }));

  return { from, to, basis: coarsest, basis_label: JITTER_BASIS_LABEL[coarsest], data };
}

// ---------------------------------------------------------------------------
// Weather: warning intervals + correlation insight (runtime, no schema change)
// ---------------------------------------------------------------------------

// Consecutive weather rows with an active warning (≤30min apart) merge into
// one interval, padded ±10min so the band visibly brackets the event.
async function fetchWeatherWarningIntervals(range) {
  const [from, to] = await resolveRange(range);
  const weatherRows = await fetchAllRaw('weather', { from, to });
  const intervals = [];
  let cur = null;
  for (const w of weatherRows) {
    const t = w.ts * 1000;
    if (w.warning) {
      if (!cur) cur = { from: t - 600000, to: t + 600000 };
      else if (t - cur.to <= 1800000) cur.to = t + 600000;
      else { intervals.push(cur); cur = { from: t - 600000, to: t + 600000 }; }
    } else if (cur) {
      intervals.push(cur);
      cur = null;
    }
  }
  if (cur) intervals.push(cur);
  return intervals;
}

// Compares latency/drop while a weather warning was active vs. normal
// conditions. Uses minutely rows for ranges ≤30d, hourly (+daily fallback)
// beyond so an 'all'-range fetch stays cheap.
async function fetchWeatherCorrelation(range) {
  const [from, to] = await resolveRange(range);
  const intervals = await fetchWeatherWarningIntervals(range);
  const useHourly = (to - from) > 30 * 86400;

  const points = [];
  if (useHourly) {
    const oldestHourTs = await _getOldestRow('metrics_hourly', 'ts_hour');
    const hourFrom = Number.isFinite(oldestHourTs) ? Math.max(from, oldestHourTs) : from;
    const hrs = await fetchAllRaw('metrics_hourly', { from: hourFrom, to });
    points.push(...hrs.map((r) => ({ ts: r.ts_hour, lat: r.avg_ping_latency_ms, drop: r.avg_ping_drop_rate })));
    if (Number.isFinite(oldestHourTs) && from < oldestHourTs) {
      const days = await fetchAllRaw('metrics_daily', { from, to: Math.min(to, oldestHourTs) });
      for (const r of days) {
        if (r.ts_day < oldestHourTs) points.push({ ts: r.ts_day, lat: r.avg_ping_latency_ms, drop: r.avg_ping_drop_rate });
      }
    }
  } else {
    const mins = await fetchAllRaw('metrics_minutely', { from, to });
    points.push(...mins.map((r) => ({ ts: r.ts_minute, lat: r.avg_ping_latency_ms, drop: r.avg_ping_drop_rate })));
  }

  const inWarn = (tsMs) => {
    for (const i of intervals) {
      if (tsMs >= i.from && tsMs <= i.to) return true;
    }
    return false;
  };

  const inLat = [], outLat = [], inDrop = [], outDrop = [];
  for (const p of points) {
    const warn = inWarn(p.ts * 1000);
    if (p.lat != null && p.lat > 0) (warn ? inLat : outLat).push(p.lat);
    if (p.drop != null) (warn ? inDrop : outDrop).push(p.drop);
  }

  return {
    hours_warned: intervals.reduce((s, i) => s + (i.to - i.from), 0) / 3600000,
    avg_lat_in: avgOf(inLat),
    avg_lat_out: avgOf(outLat),
    avg_drop_in: avgOf(inDrop),
    avg_drop_out: avgOf(outDrop),
    samples_in: inLat.length,
    samples_out: outLat.length,
  };
}

// ---------------------------------------------------------------------------
// SLA statistics from the events table (runtime, no schema change)
// ---------------------------------------------------------------------------
async function fetchSlaStats() {
  const now = Math.floor(Date.now() / 1000);
  const [from] = await resolveRange('all');
  const events = await fetchAllRaw('events', { from, to: now, filter_col: 'type', filter_val: 'disconnect' });

  const months = new Map(); // 'YYYY-MM' -> downS
  const hourHist = new Array(24).fill(0);
  let longestS = 0, totalDownS = 0;

  for (const e of events) {
    const d = e.duration_s || 0;
    totalDownS += d;
    if (d > longestS) longestS = d;
    const end = new Date(e.ts * 1000);
    const key = `${end.getFullYear()}-${String(end.getMonth() + 1).padStart(2, '0')}`;
    months.set(key, (months.get(key) || 0) + d);
    hourHist[new Date((e.ts - d) * 1000).getHours()]++;
  }

  const nowDate = new Date(now * 1000);
  const nowKey = `${nowDate.getFullYear()}-${String(nowDate.getMonth() + 1).padStart(2, '0')}`;
  const monthRows = [];
  for (const [key, downS] of months) {
    const [y, m] = key.split('-').map(Number);
    const isCurrent = key === nowKey;
    const totalS = isCurrent
      ? Math.max(1, now - new Date(y, m - 1, 1).getTime() / 1000)
      : new Date(y, m, 0).getDate() * 86400;
    monthRows.push({
      month: `${String(m).padStart(2, '0')}.${y}`,
      down_s: downS,
      up_pct: Math.max(0, Math.min(100, 100 - (downS / totalS) * 100)),
    });
  }
  monthRows.sort((a, b) => (a.month < b.month ? -1 : 1));

  const historySpanS = Math.max(1, now - from);
  const mtbfS = (historySpanS - totalDownS) / Math.max(1, events.length);

  return {
    months: monthRows,
    total_outages: events.length,
    longest_s: longestS,
    total_down_s: totalDownS,
    mtbf_s: mtbfS,
    hour_hist: hourHist,
  };
}

// ---------------------------------------------------------------------------
// Admin delete (the metrics+metrics_minutely cascade is UI logic, so it lives here, not in the backend)
// ---------------------------------------------------------------------------
async function adminDelete(target, from_ts, to_ts) {
  let result;
  if (target === 'metrics') {
    const [a, b] = await Promise.all([
      apiDeleteTable('metrics', from_ts, to_ts),
      apiDeleteTable('metrics_minutely', from_ts, to_ts),
    ]);
    result = { deleted: (a.deleted || 0) + (b.deleted || 0) };
  } else if (target === 'all') {
    const tables = ['events', 'metrics', 'metrics_minutely', 'metrics_hourly', 'metrics_daily', 'speedtests', 'weather'];
    const results = await Promise.all(tables.map((t) => apiDeleteTable(t, from_ts, to_ts)));
    const deleted = {};
    tables.forEach((t, i) => { deleted[t] = results[i].deleted; });
    result = { deleted };
  } else {
    result = await apiDeleteTable(target, from_ts, to_ts);
  }
  _invalidateCaches();
  return result;
}
