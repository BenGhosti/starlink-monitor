"""
metrics_minutely_aggregator.py
Standalone collector task that continuously rolls up each just-completed
minute of `metrics` (raw, 2s ticks) into `metrics_minutely` (one row per
minute, with avg/min/max).

Unlike cleanup.py (which compresses+deletes old raw data once a day), this
runs every minute and never deletes from `metrics` - raw data stays fully
intact for the 1d live view (2s resolution). metrics_minutely is an
additional, parallel aggregate table the API uses for ranges >= 7d instead
of GROUP BY-aggregating millions of raw rows on every request.

Idempotent: ON CONFLICT(ts_minute) DO UPDATE in case a minute gets
reprocessed (e.g. collector restart).

Backfill on start: without it, minutes that weren't "the last completed
minute" during a collector outage would be missing from metrics_minutely
forever, even though their raw data (90-day retention) still exists. That
would show as a gap in the 7d/14d/1m view despite the data technically
being there. backfill_missing_minutes() closes that gap once at startup.
"""

import asyncio
import logging
import time

from db import get_db, RAW_SAMPLE_INTERVAL_S

logger = logging.getLogger("metrics_minutely_aggregator")

MINUTE_S = 60
RUN_INTERVAL_S = 60  # check once a minute whether a new minute has completed
BACKFILL_MAX_MINUTES = 90 * 24 * 60  # never further back than the raw-data retention (90 days)


async def aggregate_minute(db, ts_minute_start: int) -> bool:
    """Aggregates exactly one minute [ts_minute_start, ts_minute_start+60).
    Returns True if data was found and written."""
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

    # Data volume for this minute: SUM(bps) across all 2s samples * interval / 8
    # (bit -> byte). More robust than avg_bps*60s across gaps (restarts, outages).
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
    """Finds completed minutes that have raw data in `metrics` but are
    missing from `metrics_minutely`, and aggregates them. Capped at
    BACKFILL_MAX_MINUTES so a huge/unexpected gap doesn't block for hours.
    Returns the number of minutes backfilled."""
    now = int(time.time())
    current_minute_start = (now // MINUTE_S) * MINUTE_S

    async with db.execute("SELECT MIN(ts) FROM metrics") as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        return 0  # no raw data yet

    earliest_minute = (row[0] // MINUTE_S) * MINUTE_S
    oldest_allowed = current_minute_start - BACKFILL_MAX_MINUTES * MINUTE_S
    earliest_minute = max(earliest_minute, oldest_allowed)

    async with db.execute("SELECT ts_minute FROM metrics_minutely") as cur:
        existing = {r[0] async for r in cur}

    backfilled = 0
    ts_minute = earliest_minute
    # current_minute_start itself isn't complete yet, hence < not <=
    while ts_minute < current_minute_start:
        if ts_minute not in existing:
            try:
                wrote = await aggregate_minute(db, ts_minute)
                if wrote:
                    backfilled += 1
            except Exception:  # noqa: BLE001
                logger.exception("Backfill failed for minute %s", ts_minute)
        ts_minute += MINUTE_S

    if backfilled:
        logger.info("Backfill done: %s missing minutes aggregated.", backfilled)
    return backfilled


async def run():
    logger.info("metrics_minutely_aggregator starting, interval %ss", RUN_INTERVAL_S)
    db = await get_db()

    last_aggregated_minute: int | None = None

    try:
        await backfill_missing_minutes(db)

        while True:
            now = int(time.time())
            current_minute_start = (now // MINUTE_S) * MINUTE_S
            # Only aggregate the PREVIOUS minute - the current one isn't
            # complete yet (more data points are still coming in).
            target_minute = current_minute_start - MINUTE_S

            if target_minute != last_aggregated_minute:
                try:
                    wrote = await aggregate_minute(db, target_minute)
                    if wrote:
                        last_aggregated_minute = target_minute
                except Exception:  # noqa: BLE001
                    logger.exception("Error aggregating minute %s", target_minute)

            await asyncio.sleep(RUN_INTERVAL_S)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
