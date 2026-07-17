"""
metrics_minutely_aggregator.py
Laeuft als eigener Collector-Task und komprimiert kontinuierlich die jeweils
letzte vollstaendig abgeschlossene Minute aus `metrics` (Rohdaten, 2s-Takt)
in `metrics_minutely` (1 Zeile pro Minute, mit avg/min/max).

Anders als cleanup.py (der nur EINMAL TAEGLICH alte Rohdaten komprimiert und
loescht) laeuft dieser Aggregator durchgehend im Minutentakt und LOESCHT NICHT
aus `metrics` - die Rohdaten bleiben fuer die 1d-Live-Ansicht (2s-Aufloesung)
weiterhin vollstaendig erhalten. metrics_minutely ist eine zusaetzliche,
parallel gepflegte Aggregat-Tabelle, die die API fuer Zeitraeume >= 7d nutzt,
statt bei jeder Anfrage Millionen Rohzeilen zu GROUP BY aggregieren zu muessen.

Idempotent: ON CONFLICT(ts_minute) DO UPDATE, falls eine Minute aus
irgendeinem Grund erneut verarbeitet wird (z.B. Collector-Neustart).

Backfill bei Start: Ohne explizite Nachbearbeitung wuerden Minuten, die
waehrend einer Collector-Ausfallzeit (Stromausfall, Neustart, Deploy) NICHT
"die letzte abgeschlossene Minute" waren, fuer immer in metrics_minutely
fehlen - obwohl ihre Rohdaten in `metrics` (90 Tage Retention) noch vorhanden
sind. Das wuerde sich als Luecke in der 7d/14d/1m-Ansicht zeigen, obwohl die
Daten technisch noch da waeren. backfill_missing_minutes() schliesst das beim
Start einmalig (und ist danach ueberfluessig, da der Live-Takt jede Minute
zeitnah verarbeitet).
"""

import asyncio
import logging
import time

from db import get_db, RAW_SAMPLE_INTERVAL_S

logger = logging.getLogger("metrics_minutely_aggregator")

MINUTE_S = 60
RUN_INTERVAL_S = 60  # einmal pro Minute pruefen, ob eine neue Minute abgeschlossen ist
BACKFILL_MAX_MINUTES = 90 * 24 * 60  # nie weiter zurueck als die Rohdaten-Retention (90 Tage)


async def aggregate_minute(db, ts_minute_start: int) -> bool:
    """Aggregiert genau eine Minute [ts_minute_start, ts_minute_start+60).
    Gibt True zurueck, wenn Daten gefunden und geschrieben wurden."""
    ts_minute_end = ts_minute_start + MINUTE_S

    async with db.execute(
        """
        SELECT
            AVG(ping_drop_rate),
            AVG(ping_latency_ms), MIN(ping_latency_ms), MAX(ping_latency_ms),
            AVG(obstr_fraction), MAX(obstr_fraction),
            AVG(downlink_bps), MIN(downlink_bps), MAX(downlink_bps),
            AVG(uplink_bps), MIN(uplink_bps), MAX(uplink_bps),
            SUM(downlink_bps), SUM(uplink_bps),
            COUNT(*)
        FROM metrics
        WHERE ts >= ? AND ts < ?
        """,
        (ts_minute_start, ts_minute_end),
    ) as cur:
        row = await cur.fetchone()

    sample_count = row[14] if row else 0
    if not sample_count:
        return False

    # Datenvolumen dieser Minute: SUM(bps) ueber alle 2s-Samples * Intervall / 8
    # (Bit -> Byte). Robuster als avg_bps*60s bei Luecken (Neustart, Ausfall).
    down_bytes = int(row[12] * RAW_SAMPLE_INTERVAL_S / 8) if row[12] is not None else None
    up_bytes = int(row[13] * RAW_SAMPLE_INTERVAL_S / 8) if row[13] is not None else None

    await db.execute(
        """
        INSERT INTO metrics_minutely
            (ts_minute, avg_ping_drop_rate, avg_ping_latency_ms, min_ping_latency_ms, max_ping_latency_ms,
             avg_obstr_fraction, max_obstr_fraction,
             avg_downlink_bps, min_downlink_bps, max_downlink_bps,
             avg_uplink_bps, min_uplink_bps, max_uplink_bps, down_bytes, up_bytes, sample_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ts_minute) DO UPDATE SET
            avg_ping_drop_rate=excluded.avg_ping_drop_rate,
            avg_ping_latency_ms=excluded.avg_ping_latency_ms,
            min_ping_latency_ms=excluded.min_ping_latency_ms,
            max_ping_latency_ms=excluded.max_ping_latency_ms,
            avg_obstr_fraction=excluded.avg_obstr_fraction,
            max_obstr_fraction=excluded.max_obstr_fraction,
            avg_downlink_bps=excluded.avg_downlink_bps,
            min_downlink_bps=excluded.min_downlink_bps,
            max_downlink_bps=excluded.max_downlink_bps,
            avg_uplink_bps=excluded.avg_uplink_bps,
            min_uplink_bps=excluded.min_uplink_bps,
            max_uplink_bps=excluded.max_uplink_bps,
            down_bytes=excluded.down_bytes,
            up_bytes=excluded.up_bytes,
            sample_count=excluded.sample_count
        """,
        (ts_minute_start, row[0], row[1], row[2], row[3], row[4], row[5],
         row[6], row[7], row[8], row[9], row[10], row[11], down_bytes, up_bytes, sample_count),
    )
    await db.commit()
    return True


async def backfill_missing_minutes(db) -> int:
    """Findet abgeschlossene Minuten, die Rohdaten in `metrics` haben, aber
    noch nicht in `metrics_minutely` stehen, und aggregiert sie nach.
    Begrenzt auf BACKFILL_MAX_MINUTES, um bei einer riesigen/unerwarteten
    Luecke (z.B. sehr alte Test-DB) nicht stundenlang zu blockieren.
    Gibt die Anzahl nachgeholter Minuten zurueck."""
    now = int(time.time())
    current_minute_start = (now // MINUTE_S) * MINUTE_S

    async with db.execute("SELECT MIN(ts) FROM metrics") as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        return 0  # noch gar keine Rohdaten vorhanden

    earliest_minute = (row[0] // MINUTE_S) * MINUTE_S
    oldest_allowed = current_minute_start - BACKFILL_MAX_MINUTES * MINUTE_S
    earliest_minute = max(earliest_minute, oldest_allowed)

    async with db.execute("SELECT ts_minute FROM metrics_minutely") as cur:
        existing = {r[0] async for r in cur}

    backfilled = 0
    ts_minute = earliest_minute
    # current_minute_start selbst ist noch nicht abgeschlossen, daher < statt <=
    while ts_minute < current_minute_start:
        if ts_minute not in existing:
            try:
                wrote = await aggregate_minute(db, ts_minute)
                if wrote:
                    backfilled += 1
            except Exception:  # noqa: BLE001
                logger.exception("Backfill fehlgeschlagen fuer Minute %s", ts_minute)
        ts_minute += MINUTE_S

    if backfilled:
        logger.info("Backfill abgeschlossen: %s fehlende Minuten nachtraeglich aggregiert.", backfilled)
    return backfilled


async def run():
    logger.info("metrics_minutely_aggregator startet, Intervall %ss", RUN_INTERVAL_S)
    db = await get_db()

    last_aggregated_minute: int | None = None

    try:
        await backfill_missing_minutes(db)

        while True:
            now = int(time.time())
            current_minute_start = (now // MINUTE_S) * MINUTE_S
            # Nur die VORHERIGE Minute aggregieren, da die aktuelle Minute
            # noch nicht abgeschlossen ist (es kommen noch Datenpunkte rein).
            target_minute = current_minute_start - MINUTE_S

            if target_minute != last_aggregated_minute:
                try:
                    wrote = await aggregate_minute(db, target_minute)
                    if wrote:
                        last_aggregated_minute = target_minute
                except Exception:  # noqa: BLE001
                    logger.exception("Fehler beim Aggregieren von Minute %s", target_minute)

            await asyncio.sleep(RUN_INTERVAL_S)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
