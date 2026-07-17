"""
db.py
Zentrales SQLite-Setup für den Starlink-Monitor.
Wird von allen Collector-Prozessen (metrics, ping, weather, speedtest)
und vom Cleanup-Job importiert, damit Schema & Pragmas an genau einer
Stelle gepflegt werden.
"""

import logging

import aiosqlite

DB_PATH = "/data/starlink.db"
logger = logging.getLogger("db")

# Polling-Takt von metrics_collector.py (POLL_INTERVAL_S dort) - hier zentral
# gepflegt, weil sowohl der Minuten-Aggregator als auch cleanup.py daraus die
# Traffic-Bytes einer Rohdaten-Zeile berechnen (bps * Intervall / 8).
RAW_SAMPLE_INTERVAL_S = 2

# Pragmas fuer dauerhaften Betrieb mit Millionen Zeilen (2s-Takt) und
# gleichzeitigem Lesezugriff des frontend-Containers (WAL erlaubt paralleles
# Lesen waehrend geschrieben wird). cache_size/mmap_size halten den heissen
# Teil der DB (juengste Tage) im RAM, busy_timeout verhindert "database is
# locked"-Fehler bei kurzen Schreib-/Compress-Ueberschneidungen statt sofort
# zu failen.
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

-- Statische/selten wechselnde Geraeteinfo der Dish. Immer genau 1 Zeile (id=1),
-- die der metrics_collector bei jedem Tick per UPSERT aktuell haelt. Getrennt
-- von `metrics`, damit String-Felder (Versionen, Geraete-ID) nicht 43.000x/Tag
-- redundant in der hochfrequenten Tabelle landen.
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

-- Komprimierte Stunden-Aggregate fuer Rohdaten > 90 Tage (siehe cleanup.py)
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

-- Laufend (nicht erst nach 90 Tagen) befuellte Minuten-Aggregate mit
-- min/max/avg fuer alle Hauptfelder. Zweck: bei Zeitraeumen ab 7d soll die
-- UI nicht mehr Rohdaten (2s-Takt) aggregieren muessen (teuer, ungenau beim
-- Hovern), sondern direkt auf fertigen Minuten-Buckets aufbauen, die zusaetzlich
-- Min/Max mitfuehren fuer aussagekraeftige Tooltips ("Tiefstwert/Hoechstwert
-- in dieser Minute"). Wird von metrics_minutely_aggregator.py im Collector
-- kontinuierlich nachgefuehrt (Minute X wird befuellt, sobald X+1 begonnen hat).
-- down_bytes/up_bytes: tatsaechlich uebertragenes Datenvolumen dieser Minute
-- (SUM(bps)*RAW_SAMPLE_INTERVAL_S/8 ueber alle 2s-Samples), fuer die
-- Traffic-Anzeige im Dashboard - exakter als avg_bps*60s, da Luecken
-- (Collector-Downtime) durch sample_count implizit beruecksichtigt sind.
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

-- Dritte Kompressionsstufe fuer sehr lange Historien (Jahre): metrics_hourly
-- ist bereits deutlich kleiner als Rohdaten, wird aber bei Multi-Jahres-
-- Betrieb selbst gross genug, um sich zu lohnen. cleanup.py verdichtet
-- metrics_hourly-Zeilen aelter als HOURLY_ROLLUP_DAYS zu Tages-Buckets hier
-- und loescht danach die verdichteten Stunden-Zeilen. sample_count ist die
-- Summe der zugrunde liegenden Stunden-sample_counts, damit ein gewichteter
-- Durchschnitt ueber mehrere Rollup-Stufen hinweg korrekt bleibt.
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

-- Persistente Retry-Queue fuer Discord-Webhooks. Schlaegt ein Versand fehl
-- (z.B. weil das Internet selbst weg ist, nicht nur die Dish-Verbindung),
-- landet der Alert hier statt verloren zu gehen. Ein Hintergrund-Task in
-- discord_alert.py versucht die Queue periodisch erneut zu versenden, auch
-- ueber einen Container-Neustart hinweg (daher SQLite statt nur In-Memory).
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
    """Legt Pragmas + Schema an. Idempotent, kann von jedem Prozess beim Start aufgerufen werden."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(PRAGMAS)
        await db.executescript(SCHEMA)
        await db.commit()
        await _run_migrations(db)
        await db.commit()


# Erwartete Spalten je Tabelle mit ihrem SQL-Typ, fuer die automatische
# Nachmigration bei bereits existierenden Datenbanken. WICHTIG: CREATE TABLE
# IF NOT EXISTS (siehe SCHEMA oben) legt das Schema nur bei einer komplett
# NEUEN Tabelle an - wurde die Tabelle schon einmal mit einer aelteren
# Code-Version angelegt (z.B. vor Einfuehrung von direction_azimuth/gps_*/
# uptime_s), fehlen diese Spalten in der bestehenden Tabelle fuer immer,
# OHNE dass irgendwo ein Fehler auftritt - Inserts, die diese Spalten nicht
# referenzieren, laufen weiter normal durch, und die fehlenden Werte werden
# stillschweigend nie geschrieben. Das aeussert sich im Frontend als "Daten
# fehlen", ohne dass in den Logs ein offensichtlicher Fehler auftaucht.
# _run_migrations() schliesst diese Luecke: ALTER TABLE ... ADD COLUMN ist in
# SQLite eine billige, sofortige Operation (kein Tabellen-Rewrite noetig).
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
                logger.info("Migration: fuege fehlende Spalte %s.%s (%s) hinzu", table, col_name, col_type)
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


async def get_db():
    """Liefert eine neue Connection mit den richtigen Pragmas (fuer Prozesse, die init_db schon liefen)."""
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(PRAGMAS)
    return db
