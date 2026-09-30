import asyncio
import json
import logging
import os
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import speedtest

from db import get_db

logger = logging.getLogger("speedtest_runner")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
SCHEDULE_HOURS_BERLIN = [0, 8, 16]
MIN_TEST_DURATION_S = int(os.environ.get("SPEEDTEST_MIN_DURATION_S", "20"))
WARMUP_S = int(os.environ.get("SPEEDTEST_WARMUP_S", "3"))
MAX_TEST_ROUNDS = 40
LATENCY_PROBES = int(os.environ.get("SPEEDTEST_LATENCY_PROBES", "10"))


def _measure_latency_and_jitter(base_url: str) -> tuple[float | None, float | None]:
    """Measure true round-trip latency and jitter against a speedtest server.

    speedtest-cli's own ping value divides 3 samples by 6 (see
    Speedtest.get_best_server), which reports roughly half the real RTT, and it
    provides no jitter at all. We probe latency.txt ourselves: one discarded
    warm-up request, then LATENCY_PROBES measured ones over fresh connections.
    Latency = mean RTT (ms); jitter = mean absolute delta between consecutive
    samples (same definition the dashboard uses for its live jitter).
    """
    if not base_url.startswith(("http://", "https://")):
        return None, None
    samples: list[float] = []
    for i in range(LATENCY_PROBES + 1):
        url = f"{base_url.rstrip('/')}/latency.txt?x={int(time.time() * 1000)}.{i}"
        request = urllib.request.Request(url, headers={"User-Agent": "speedtest-cli/2.1.3"})
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read(9)
        except Exception:  # noqa: BLE001
            continue
        elapsed_ms = (time.perf_counter() - start) * 1000
        if i > 0:  # first probe is warm-up (DNS/connection setup)
            samples.append(elapsed_ms)
    if not samples:
        return None, None
    latency_ms = statistics.fmean(samples)
    deltas = [abs(b - a) for a, b in zip(samples, samples[1:])]
    jitter_ms = statistics.fmean(deltas) if deltas else 0.0
    return round(latency_ms, 1), round(jitter_ms, 1)


def _next_run_time(now_utc: datetime) -> datetime:
    now_berlin = now_utc.astimezone(BERLIN_TZ)
    candidates = []
    for day_offset in (0, 1):
        day = (now_berlin + timedelta(days=day_offset)).date()
        for hour in SCHEDULE_HOURS_BERLIN:
            candidate = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=BERLIN_TZ)
            if candidate >= now_berlin - timedelta(seconds=1):
                candidates.append(candidate)
    target = min(candidates)
    if target < now_berlin:
        target = now_berlin
    return target


def _sustained_measure(st: "speedtest.Speedtest", run_once, bytes_attr: str) -> float:
    run_once()

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
    st = speedtest.Speedtest()
    st.get_best_server()

    # Own RTT/jitter probes before any load is applied
    server_url = os.path.dirname(st.best["url"])
    latency_ms, jitter_ms = _measure_latency_and_jitter(server_url)
    if latency_ms is None:
        latency_ms = st.results.ping  # fallback: speedtest-cli value (roughly half the RTT)

    download_bps = _sustained_measure(st, st.download, "bytes_received")
    upload_bps = _sustained_measure(st, st.upload, "bytes_sent")

    return {
        "download_mbit": download_bps / 1_000_000,
        "upload_mbit": upload_bps / 1_000_000,
        "latency_ms": latency_ms,
        "jitter_ms": jitter_ms,
        "server": st.results.server.get("name", "unknown"),
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
                    "Speedtest: %.1f Mbit/s down, %.1f Mbit/s up, %.0f ms latency, %.1f ms jitter",
                    result["download_mbit"], result["upload_mbit"],
                    result["latency_ms"] or 0, result["jitter_ms"] or 0,
                )
                await insert_speedtest(db, result)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
