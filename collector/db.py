"""
db.py
Central SQLite setup for Starlink Monitor. Imported by every collector
process (metrics, ping, weather, speedtest) and the cleanup job, so schema
and pragmas live in exactly one place.
"""

import logging

import aiosqlite

DB_PATH = "/data/starlink.db"
logger = logging.getLogger("db")

# Collector poll interval (POLL_INTERVAL_S in metrics_collector.py). Kept
# here so both the minutely aggregator and cleanup.py can compute a raw
# row's traffic bytes as bps * interval / 8.
RAW_SAMPLE_INTERVAL_S = 2

# Tuned for sustained high-frequency writes (2s ticks) with concurrent reads
# from the frontend container. WAL allows readers and the writer to run
# concurrently; cache_size/mmap_size keep the hot (recent) part of the DB in
# RAM; busy_timeout avoids "database is locked" errors on brief write/compress
# overlaps instead of failing immediately.
PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-64000;
PRAGMA mmap_size=268435456;
PRAGMA wal_autocheckpoint=1000;
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ping_drop_rate REAL,
    ping_latency_ms REAL,
    obstr_fraction REAL,
    obstr_valid_s REAL,
    downlink_bps INTEGER,
    uplink_bps INTEGER,
    state TEXT,
    snr REAL,
    uptime_s INTEGER,
    seconds_to_first_nonempty_slot REAL,
    currently_obstructed INTEGER,
    obstruction_duration REAL,
    obstruction_interval REAL,
    direction_azimuth REAL,
    direction_elevation REAL,
    is_snr_above_noise_floor INTEGER,
    gps_ready INTEGER,
    gps_enabled INTEGER,
    gps_sats INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);

-- Static/rarely-changing dish info. Always exactly one row (id=1), kept
-- current by metrics_collector via UPSERT on every tick. Separate from
-- `metrics` so string fields (versions, device ID) don't get duplicated
-- into the high-frequency table tens of thousands of times a day.
CREATE TABLE IF NOT EXISTS dish_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    device_id TEXT,
    hardware_version TEXT,
    software_version TEXT,
    alerts_bitfield INTEGER,
    last_seen_ts INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,
    duration_s REAL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS speedtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    download_mbit REAL,
    upload_mbit REAL,
    latency_ms REAL,
    jitter_ms REAL,
    server TEXT
);
CREATE INDEX IF NOT EXISTS idx_speedtests_ts ON speedtests(ts);

CREATE TABLE IF NOT EXISTS weather (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    temp_c REAL,
    wind_kmh REAL,
    wmo_code INTEGER,
    precipitation_mm REAL,
    visibility_m INTEGER,
    humidity REAL,
    warning TEXT
);
CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(ts);

-- Compressed hourly aggregates for raw data > 90 days old (see cleanup.py).
CREATE TABLE IF NOT EXISTS metrics_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_hour INTEGER NOT NULL UNIQUE,
    avg_ping_drop_rate REAL,
    avg_ping_latency_ms REAL,
    max_ping_latency_ms REAL,
    avg_obstr_fraction REAL,
    avg_downlink_bps INTEGER,
    avg_uplink_bps INTEGER,
    down_bytes INTEGER,
    up_bytes INTEGER,
    sample_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_hourly_ts ON metrics_hourly(ts_hour);

-- Minute aggregates, filled continuously (not just after 90 days) with
-- min/max/avg for the main fields, so ranges >= 7d can build charts
-- straight from ready-made buckets instead of aggregating 2s raw data on
-- every request. down_bytes/up_bytes hold the minute's actual transferred
-- volume (SUM(bps) * RAW_SAMPLE_INTERVAL_S / 8), used by the traffic chart -
-- more accurate than avg_bps*60s since gaps are reflected via sample_count.
CREATE TABLE IF NOT EXISTS metrics_minutely (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_minute INTEGER NOT NULL UNIQUE,
    avg_ping_drop_rate REAL,
    avg_ping_latency_ms REAL,
    min_ping_latency_ms REAL,
    max_ping_latency_ms REAL,
    avg_obstr_fraction REAL,
    max_obstr_fraction REAL,
    avg_downlink_bps REAL,
    min_downlink_bps REAL,
    max_downlink_bps REAL,
    avg_uplink_bps REAL,
    min_uplink_bps REAL,
    max_uplink_bps REAL,
    down_bytes INTEGER,
    up_bytes INTEGER,
    sample_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_minutely_ts ON metrics_minutely(ts_minute);

-- Third compression tier for multi-year histories: cleanup.py rolls up
-- metrics_hourly rows older than HOURLY_ROLLUP_DAYS into daily buckets here
-- and deletes the source hourly rows. sample_count is the sum of the
-- underlying hourly sample_counts, so weighted averages stay correct across
-- rollup stages.
CREATE TABLE IF NOT EXISTS metrics_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_day INTEGER NOT NULL UNIQUE,
    avg_ping_drop_rate REAL,
    avg_ping_latency_ms REAL,
    max_ping_latency_ms REAL,
    avg_obstr_fraction REAL,
    avg_downlink_bps INTEGER,
    avg_uplink_bps INTEGER,
    down_bytes INTEGER,
    up_bytes INTEGER,
    sample_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_daily_ts ON metrics_daily(ts_day);

-- Persistent retry queue for Discord webhooks. If a send fails (e.g. the
-- internet uplink itself is down, not just the dish), the alert lands here
-- instead of being lost; discord_alert.py retries it in the background,
-- surviving container restarts (hence SQLite, not just in-memory).
CREATE TABLE IF NOT EXISTS alert_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alert_queue_created ON alert_queue(created_ts);
"""


async def init_db():
    """Applies pragmas + schema. Idempotent; safe to call from any process at startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(PRAGMAS)
        await db.executescript(SCHEMA)
        await db.commit()
        await _run_migrations(db)
        await db.commit()


# Expected columns per table, for auto-migrating pre-existing databases.
# CREATE TABLE IF NOT EXISTS only applies the full schema to a brand-new
# table; on a table created by an older code version, missing columns would
# otherwise stay missing forever with no visible error (inserts that don't
# reference them just silently never populate them). ALTER TABLE ... ADD
# COLUMN is a cheap, instant operation in SQLite (no table rewrite).
EXPECTED_COLUMNS = {
    "metrics": {
        "uptime_s": "INTEGER",
        "seconds_to_first_nonempty_slot": "REAL",
        "currently_obstructed": "INTEGER",
        "obstruction_duration": "REAL",
        "obstruction_interval": "REAL",
        "direction_azimuth": "REAL",
        "direction_elevation": "REAL",
        "is_snr_above_noise_floor": "INTEGER",
        "gps_ready": "INTEGER",
        "gps_enabled": "INTEGER",
        "gps_sats": "INTEGER",
        "alerts_bitfield": "INTEGER",
    },
    "dish_info": {
        "alerts_bitfield": "INTEGER",
    },
    "metrics_minutely": {
        "down_bytes": "INTEGER",
        "up_bytes": "INTEGER",
    },
    "metrics_hourly": {
        "down_bytes": "INTEGER",
        "up_bytes": "INTEGER",
    },
    "metrics_daily": {
        "down_bytes": "INTEGER",
        "up_bytes": "INTEGER",
    },
}


async def _run_migrations(db):
    for table, columns in EXPECTED_COLUMNS.items():
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row[1] async for row in cur}  # row[1] = column name

        for col_name, col_type in columns.items():
            if col_name not in existing:
                logger.info("Migration: adding missing column %s.%s (%s)", table, col_name, col_type)
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


async def get_db():
    """New connection with the correct pragmas (for processes where init_db() already ran)."""
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(PRAGMAS)
    return db
