"""
speedtest_runner.py
Runs Ookla speedtests (via speedtest-cli) on a fixed daily schedule
(SCHEDULE_HOURS_BERLIN, Europe/Berlin, DST-safe) instead of every 8h from
process start.

Sustained measurement: a browser/app-style Ookla test doesn't just grab one
fixed chunk of data - it keeps multiple connections open for ~10-15s per
direction so the link has time to ramp up to its real ceiling, then averages
over the sustained portion (discarding the initial ramp-up). speedtest-cli's
single download()/upload() call targets a fixed, comparatively small volume
that a fast link (Starlink) can finish in 2-3 seconds, understating true
throughput. To match real-world Ookla behavior we:
  1. run one untimed warm-up round to let Starlink/TCP ramp up,
  2. then repeat rounds until MIN_TEST_DURATION_S has elapsed,
  3. average over total bytes / total time across those rounds.

Writes results to `speedtests` plus a matching `events` row (type: speedtest)
for the event log.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import speedtest

from db import get_db

logger = logging.getLogger("speedtest_runner")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
SCHEDULE_HOURS_BERLIN = [0, 8, 16]  # daily, repeats
MIN_TEST_DURATION_S = int(os.environ.get("SPEEDTEST_MIN_DURATION_S", "20"))
WARMUP_S = int(os.environ.get("SPEEDTEST_WARMUP_S", "3"))
MAX_TEST_ROUNDS = 40  # safety cap against infinite loops on very fast links


def _next_run_time(now_utc: datetime) -> datetime:
    """Next SCHEDULE_HOURS_BERLIN slot after `now_utc` (Europe/Berlin, DST-safe)."""
    now_berlin = now_utc.astimezone(BERLIN_TZ)
    candidates = []
    for day_offset in (0, 1):
        day = (now_berlin + timedelta(days=day_offset)).date()
        for hour in SCHEDULE_HOURS_BERLIN:
            candidate = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=BERLIN_TZ)
            if candidate > now_berlin:
                candidates.append(candidate)
    return min(candidates)


def _sustained_measure(st: "speedtest.Speedtest", run_once, bytes_attr: str) -> float:
    """Runs one warm-up round (discarded), then repeats `run_once()` (st.download
    or st.upload) until MIN_TEST_DURATION_S has elapsed, returning sustained
    throughput in bit/s over the sum of the counted rounds."""
    run_once()  # warm-up: lets Starlink and speedtest-cli's connections ramp up

    total_bytes = 0
    total_time = 0.0
    start_all = time.monotonic()
    for _ in range(MAX_TEST_ROUNDS):
        round_start = time.monotonic()
        run_once()
        total_time += time.monotonic() - round_start
        total_bytes += getattr(st.results, bytes_attr)
        if time.monotonic() - start_all >= MIN_TEST_DURATION_S:
            break
    if total_time <= 0:
        return 0.0
    return (total_bytes * 8) / total_time


def _run_speedtest_blocking() -> dict:
    """speedtest-cli is synchronous/blocking, so this runs in an executor."""
    st = speedtest.Speedtest()
    st.get_best_server()

    download_bps = _sustained_measure(st, st.download, "bytes_received")
    upload_bps = _sustained_measure(st, st.upload, "bytes_sent")
    results = st.results.dict()

    return {
        "download_mbit": download_bps / 1_000_000,
        "upload_mbit": upload_bps / 1_000_000,
        "latency_ms": results.get("ping"),
        "jitter_ms": results.get("jitter"),  # not present in every speedtest-cli version
        "server": results.get("server", {}).get("name", "unknown"),
    }


async def run_speedtest() -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _run_speedtest_blocking)
    except Exception:  # noqa: BLE001
        logger.exception("Speedtest failed")
        return None


async def insert_speedtest(db, result: dict):
    ts = int(time.time())
    await db.execute(
        """
        INSERT INTO speedtests (ts, download_mbit, upload_mbit, latency_ms, jitter_ms, server)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            result["download_mbit"],
            result["upload_mbit"],
            result["latency_ms"],
            result["jitter_ms"],
            result["server"],
        ),
    )
    await db.execute(
        "INSERT INTO events (ts, type, duration_s, details) VALUES (?, 'speedtest', NULL, ?)",
        (ts, json.dumps(result, ensure_ascii=False)),
    )
    await db.commit()


async def run():
    logger.info(
        "speedtest_runner starting, schedule %s (Europe/Berlin), min %ss/direction + %ss warm-up",
        SCHEDULE_HOURS_BERLIN, MIN_TEST_DURATION_S, WARMUP_S,
    )
    db = await get_db()

    try:
        while True:
            now = datetime.now(timezone.utc)
            target = _next_run_time(now)
            sleep_s = (target - now).total_seconds()
            logger.info("Next speedtest at %s (in %.0f min)", target.isoformat(), sleep_s / 60)
            await asyncio.sleep(max(0, sleep_s))

            result = await run_speedtest()
            if result is not None:
                logger.info(
                    "Speedtest: %.1f Mbit/s down, %.1f Mbit/s up, %.0f ms",
                    result["download_mbit"], result["upload_mbit"], result["latency_ms"] or 0,
                )
                await insert_speedtest(db, result)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
