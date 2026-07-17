"""
cleanup.py
Laeuft einmal taeglich (siehe collect.py) als eigene Task im Collector-Prozess.

Logik:
- Metrics-Rohdaten (alle 2s) aelter als RETENTION_DAYS (90) werden pro
  vollstaendiger Stunde zu einem Aggregat in `metrics_hourly` zusammengefasst
  (avg drop rate, avg/max latency, avg obstruction, avg throughput, sample_count).
- Danach werden genau die komprimierten Rohzeilen aus `metrics` geloescht.
- events, speedtests und weather bleiben unangetastet (deutlich geringeres
  Datenvolumen, lohnt sich nicht zu aggregieren).
- Felder wie Ausrichtung (Azimuth/Elevation), GPS-Status und Geraeteinfo werden
  bewusst NICHT in metrics_hourly uebernommen: das sind Punkt-in-Zeit-Zustaende
  ohne sinnvolle "Durchschnitts"-Bedeutung nach 90 Tagen (die Dish-Ausrichtung
  aendert sich praktisch nie, GPS-Sat-Anzahl ist nur im Live-Betrieb relevant).
  Wer das spaeter braucht, sollte sie separat in dish_info_history o.ae. festhalten.
- metrics_minutely (kontinuierlich von metrics_minutely_aggregator.py befuellt,
  liefert die API-Aggregation fuer 7d-1m Zeitraeume) wird HIER zusaetzlich mit
  einer eigenen, deutlich laengeren Retention (MINUTELY_RETENTION_DAYS, Default
  2 Jahre) aufgeraeumt. Bei 1 Zeile/Minute ist das Volumen ca. Faktor 30 kleiner
  als bei den 2s-Rohdaten, daher lohnt sich eine laengere Aufbewahrung. Aeltere
  Minutendaten werden ersatzlos geloescht (nicht weiter komprimiert), da
  metrics_hourly fuer sehr alte Zeitraeume bereits existiert.
- Wichtige Reihenfolge-Annahme: metrics_minutely_aggregator.py aggregiert JEDE
  Minute zeitnah (plus einmaligen Backfill beim Start, siehe dort), bevor
  compress_old_metrics() hier Rohdaten loescht (erst nach 90 Tagen). Die
  Minuten-Aggregation ist also so gut wie immer fertig, lange bevor die
  zugehoerigen Rohdaten ueberhaupt zum Loeschen anstehen - es sei denn, der
  Collector waere die vollen 90 Tage am Stueck offline (dann gaebe es ohnehin
  keine Rohdaten zum Verlieren).
- Idempotent: bereits aggregierte Stunden werden via UNIQUE(ts_hour) + INSERT OR REPLACE
  nicht doppelt angelegt, falls der Job mehrfach ueber denselben Bereich laeuft.
"""

import asyncio
import logging
import os
import time

from db import get_db, RAW_SAMPLE_INTERVAL_S

logger = logging.getLogger("cleanup")

RETENTION_DAYS = 90
MINUTELY_RETENTION_DAYS = int(os.environ.get("MINUTELY_RETENTION_DAYS", "730"))  # ~2 Jahre
HOURLY_ROLLUP_DAYS = int(os.environ.get("HOURLY_ROLLUP_DAYS", "365"))  # ab hier -> metrics_daily
RUN_INTERVAL_S = 24 * 60 * 60  # einmal taeglich
HOUR_S = 3600
DAY_S = 24 * HOUR_S


async def compress_old_metrics(db):
    cutoff_ts = int(time.time()) - RETENTION_DAYS * 24 * HOUR_S

    # Aelteste vorhandene Rohdaten-Stunde ermitteln, um nicht ueber Jahre leere Stunden zu iterieren
    async with db.execute("SELECT MIN(ts) FROM metrics WHERE ts < ?", (cutoff_ts,)) as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        logger.info("Keine Rohdaten aelter als %s Tage, nichts zu komprimieren.", RETENTION_DAYS)
        return

    start_hour = (row[0] // HOUR_S) * HOUR_S
    end_hour = (cutoff_ts // HOUR_S) * HOUR_S  # letzte VOLLSTAENDIGE Stunde vor dem Cutoff

    compressed_hours = 0
    deleted_rows = 0

    ts_hour = start_hour
    while ts_hour < end_hour:
        next_hour = ts_hour + HOUR_S

        async with db.execute(
            """
            SELECT
                AVG(ping_drop_rate), AVG(ping_latency_ms), MAX(ping_latency_ms),
                AVG(obstr_fraction), AVG(downlink_bps), AVG(uplink_bps),
                SUM(downlink_bps), SUM(uplink_bps), COUNT(*)
            FROM metrics
            WHERE ts >= ? AND ts < ?
            """,
            (ts_hour, next_hour),
        ) as cur:
            agg = await cur.fetchone()

        sample_count = agg[8] if agg else 0
        if sample_count and sample_count > 0:
            down_bytes = int(agg[6] * RAW_SAMPLE_INTERVAL_S / 8) if agg[6] is not None else None
            up_bytes = int(agg[7] * RAW_SAMPLE_INTERVAL_S / 8) if agg[7] is not None else None
            await db.execute(
                """
                INSERT INTO metrics_hourly
                    (ts_hour, avg_ping_drop_rate, avg_ping_latency_ms, max_ping_latency_ms,
                     avg_obstr_fraction, avg_downlink_bps, avg_uplink_bps, down_bytes, up_bytes, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts_hour) DO UPDATE SET
                    avg_ping_drop_rate=excluded.avg_ping_drop_rate,
                    avg_ping_latency_ms=excluded.avg_ping_latency_ms,
                    max_ping_latency_ms=excluded.max_ping_latency_ms,
                    avg_obstr_fraction=excluded.avg_obstr_fraction,
                    avg_downlink_bps=excluded.avg_downlink_bps,
                    avg_uplink_bps=excluded.avg_uplink_bps,
                    down_bytes=excluded.down_bytes,
                    up_bytes=excluded.up_bytes,
                    sample_count=excluded.sample_count
                """,
                (ts_hour, agg[0], agg[1], agg[2], agg[3], agg[4], agg[5], down_bytes, up_bytes, sample_count),
            )

            cursor = await db.execute(
                "DELETE FROM metrics WHERE ts >= ? AND ts < ?", (ts_hour, next_hour)
            )
            deleted_rows += cursor.rowcount
            compressed_hours += 1

        ts_hour = next_hour

    await db.commit()
    # Hinweis: PRAGMA incremental_vacuum/VACUUM bewusst NICHT hier ausgefuehrt -
    # VACUUM blockiert die gesamte DB exklusiv und wuerde metrics_collector/ping_watchdog
    # fuer die Dauer des Vacuums (bei 15 Mio+ Zeilen potenziell Minuten) lahmlegen.
    # WAL-Checkpointing erledigt Platzfreigabe inkrementell im Hintergrund.

    logger.info(
        "Cleanup abgeschlossen: %s Stunden komprimiert, %s Rohzeilen geloescht.",
        compressed_hours, deleted_rows,
    )


async def compress_old_hourly(db):
    """Dritte Kompressionsstufe: metrics_hourly-Zeilen aelter als
    HOURLY_ROLLUP_DAYS (Default 1 Jahr) werden pro vollstaendigem Kalendertag
    (UTC) zu metrics_daily verdichtet, danach werden die Quell-Stunden
    geloescht. sample_count wird als SUM() der Stunden-sample_counts
    uebernommen (echte Anzahl zugrunde liegender 2s-Messungen), nicht als
    Zeilenzahl - damit bleibt die Gewichtung ueber Rollup-Stufen hinweg exakt.
    """
    cutoff_ts = int(time.time()) - HOURLY_ROLLUP_DAYS * DAY_S

    async with db.execute("SELECT MIN(ts_hour) FROM metrics_hourly WHERE ts_hour < ?", (cutoff_ts,)) as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        logger.info("Keine metrics_hourly-Zeilen aelter als %s Tage, nichts zu Tages-Buckets zu verdichten.", HOURLY_ROLLUP_DAYS)
        return

    start_day = (row[0] // DAY_S) * DAY_S
    end_day = (cutoff_ts // DAY_S) * DAY_S  # letzter VOLLSTAENDIGER Tag vor dem Cutoff

    compressed_days = 0
    deleted_rows = 0

    ts_day = start_day
    while ts_day < end_day:
        next_day = ts_day + DAY_S

        async with db.execute(
            """
            SELECT
                AVG(avg_ping_drop_rate),
                SUM(avg_ping_latency_ms * sample_count) / NULLIF(SUM(sample_count), 0),
                MAX(max_ping_latency_ms),
                AVG(avg_obstr_fraction),
                SUM(avg_downlink_bps * sample_count) / NULLIF(SUM(sample_count), 0),
                SUM(avg_uplink_bps * sample_count) / NULLIF(SUM(sample_count), 0),
                SUM(down_bytes), SUM(up_bytes),
                SUM(sample_count)
            FROM metrics_hourly
            WHERE ts_hour >= ? AND ts_hour < ?
            """,
            (ts_day, next_day),
        ) as cur:
            agg = await cur.fetchone()

        sample_count = agg[8] if agg else 0
        if sample_count and sample_count > 0:
            await db.execute(
                """
                INSERT INTO metrics_daily
                    (ts_day, avg_ping_drop_rate, avg_ping_latency_ms, max_ping_latency_ms,
                     avg_obstr_fraction, avg_downlink_bps, avg_uplink_bps, down_bytes, up_bytes, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ts_day) DO UPDATE SET
                    avg_ping_drop_rate=excluded.avg_ping_drop_rate,
                    avg_ping_latency_ms=excluded.avg_ping_latency_ms,
                    max_ping_latency_ms=excluded.max_ping_latency_ms,
                    avg_obstr_fraction=excluded.avg_obstr_fraction,
                    avg_downlink_bps=excluded.avg_downlink_bps,
                    avg_uplink_bps=excluded.avg_uplink_bps,
                    down_bytes=excluded.down_bytes,
                    up_bytes=excluded.up_bytes,
                    sample_count=excluded.sample_count
                """,
                (ts_day, agg[0], agg[1], agg[2], agg[3], agg[4], agg[5], agg[6], agg[7], sample_count),
            )

            cursor = await db.execute(
                "DELETE FROM metrics_hourly WHERE ts_hour >= ? AND ts_hour < ?", (ts_day, next_day)
            )
            deleted_rows += cursor.rowcount
            compressed_days += 1

        ts_day = next_day

    await db.commit()

    logger.info(
        "Hourly-Rollup abgeschlossen: %s Tage zu metrics_daily verdichtet, %s Stunden-Zeilen geloescht.",
        compressed_days, deleted_rows,
    )


async def cleanup_old_minutely(db):
    """Loescht metrics_minutely-Zeilen aelter als MINUTELY_RETENTION_DAYS.
    Keine Komprimierung (anders als bei compress_old_metrics) - fuer sehr
    alte Zeitraeume existiert bereits metrics_hourly."""
    cutoff_ts = int(time.time()) - MINUTELY_RETENTION_DAYS * 24 * HOUR_S
    cursor = await db.execute("DELETE FROM metrics_minutely WHERE ts_minute < ?", (cutoff_ts,))
    await db.commit()
    if cursor.rowcount:
        logger.info("metrics_minutely Cleanup: %s Zeilen aelter als %s Tage geloescht.",
                     cursor.rowcount, MINUTELY_RETENTION_DAYS)


async def run():
    logger.info(
        "cleanup-Job startet, Retention=%s Tage (Rohdaten), %s Tage (minutely), Intervall=%sh",
        RETENTION_DAYS, MINUTELY_RETENTION_DAYS, RUN_INTERVAL_S / 3600,
    )
    db = await get_db()
    try:
        while True:
            try:
                await compress_old_metrics(db)
                await compress_old_hourly(db)
                await cleanup_old_minutely(db)
                # Aktualisiert SQLite's Query-Planner-Statistiken (leichtgewichtig,
                # kein Tabellen-Rewrite wie VACUUM) - nach jedem Kompressions-
                # Lauf sinnvoll, weil sich die Zeilenverteilung deutlich verschiebt.
                await db.execute("PRAGMA optimize;")
            except Exception:  # noqa: BLE001
                logger.exception("Fehler im Cleanup-Lauf")
            await asyncio.sleep(RUN_INTERVAL_S)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
