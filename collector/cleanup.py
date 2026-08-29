import asyncio
import logging
import os
import time

from db import get_db, RAW_SAMPLE_INTERVAL_S

logger = logging.getLogger("cleanup")

RETENTION_DAYS = 90
MINUTELY_RETENTION_DAYS = int(os.environ.get("MINUTELY_RETENTION_DAYS", "730"))
HOURLY_ROLLUP_DAYS = int(os.environ.get("HOURLY_ROLLUP_DAYS", "365"))
RUN_INTERVAL_S = 24 * 60 * 60
HOUR_S = 3600
DAY_S = 24 * HOUR_S

BATCH_HOURS = 12
BATCH_DAYS = 7


async def compress_old_metrics(db):
    cutoff_ts = int(time.time()) - RETENTION_DAYS * 24 * HOUR_S

    async with db.execute("SELECT MIN(ts) FROM metrics WHERE ts < ?", (cutoff_ts,)) as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        logger.info("No raw data older than %s days, nothing to compress.", RETENTION_DAYS)
        return

    start_hour = (row[0] // HOUR_S) * HOUR_S
    end_hour = (cutoff_ts // HOUR_S) * HOUR_S

    compressed_hours = 0
    deleted_rows = 0
    hours_since_commit = 0

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
        hours_since_commit += 1
        if hours_since_commit >= BATCH_HOURS:
            await db.commit()
            await asyncio.sleep(0)
            hours_since_commit = 0

    await db.commit()

    logger.info(
        "Cleanup done: %s hours compressed, %s raw rows deleted.",
        compressed_hours, deleted_rows,
    )


async def compress_old_hourly(db):
    cutoff_ts = int(time.time()) - HOURLY_ROLLUP_DAYS * DAY_S

    async with db.execute("SELECT MIN(ts_hour) FROM metrics_hourly WHERE ts_hour < ?", (cutoff_ts,)) as cur:
        row = await cur.fetchone()
    if not row or row[0] is None:
        logger.info("No metrics_hourly rows older than %s days, nothing to roll up.", HOURLY_ROLLUP_DAYS)
        return

    start_day = (row[0] // DAY_S) * DAY_S
    end_day = (cutoff_ts // DAY_S) * DAY_S

    compressed_days = 0
    deleted_rows = 0
    days_since_commit = 0

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
        days_since_commit += 1
        if days_since_commit >= BATCH_DAYS:
            await db.commit()
            await asyncio.sleep(0)
            days_since_commit = 0

    await db.commit()

    logger.info(
        "Hourly rollup done: %s days rolled into metrics_daily, %s hourly rows deleted.",
        compressed_days, deleted_rows,
    )


async def cleanup_old_minutely(db):
    cutoff_ts = int(time.time()) - MINUTELY_RETENTION_DAYS * 24 * HOUR_S
    deleted = 0
    while True:
        async with db.execute(
            "SELECT MIN(ts_minute) FROM metrics_minutely WHERE ts_minute < ?", (cutoff_ts,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[0] is None:
            break

        chunk_end = min(cutoff_ts, row[0] + DAY_S)
        cur = await db.execute(
            "DELETE FROM metrics_minutely WHERE ts_minute >= ? AND ts_minute < ?",
            (row[0], chunk_end),
        )
        deleted += cur.rowcount
        await db.commit()
        await asyncio.sleep(0)

    if deleted:
        logger.info("metrics_minutely cleanup: %s rows older than %s days deleted.",
                     deleted, MINUTELY_RETENTION_DAYS)


async def run():
    logger.info(
        "cleanup job starting, retention=%s days (raw), %s days (minutely), interval=%sh",
        RETENTION_DAYS, MINUTELY_RETENTION_DAYS, RUN_INTERVAL_S / 3600,
    )
    db = await get_db()
    try:
        while True:
            try:
                await compress_old_metrics(db)
                await compress_old_hourly(db)
                await cleanup_old_minutely(db)
                await db.execute("PRAGMA optimize;")
            except Exception:  # noqa: BLE001
                logger.exception("Error in cleanup run")
            await asyncio.sleep(RUN_INTERVAL_S)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
