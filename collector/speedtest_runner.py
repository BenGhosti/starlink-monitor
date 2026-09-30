import asyncio
import json
import logging
import os
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import speedtest

from db import get_db
from discord_alert import send_alert, COLOR_YELLOW

logger = logging.getLogger("speedtest_runner")

# Own opener without system proxy discovery: urllib's default opener consults
# OS proxy settings on every call, which can add seconds of overhead per
# request on some systems (measured on Windows) and serves no purpose here.
_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BERLIN_TZ = ZoneInfo("Europe/Berlin")
SCHEDULE_HOURS_BERLIN = [0, 8, 16]
MIN_TEST_DURATION_S = int(os.environ.get("SPEEDTEST_MIN_DURATION_S", "20"))
MAX_TEST_ROUNDS = 100
LATENCY_PROBES = int(os.environ.get("SPEEDTEST_LATENCY_PROBES", "10"))
SERVER_ID = os.environ.get("SPEEDTEST_SERVER_ID", "").strip()
SERVER_URL = os.environ.get("SPEEDTEST_SERVER_URL", "").strip()
SERVER_LAT = os.environ.get("SPEEDTEST_SERVER_LAT", "").strip() or "51.3388"
SERVER_LON = os.environ.get("SPEEDTEST_SERVER_LON", "").strip() or "6.5853"

CANDIDATE_PROBES = 2
CANDIDATE_LIMIT = 10
CANDIDATE_WORKERS = 10
CANDIDATE_TIMEOUT_S = 1.5
PROBE_FAIL_FAST = 3

# Upload rounds use the speedtest.net per-request sizes but fewer repetitions
# than the default (8 instead of 17). Per-request size must stay large: small
# upload files do not amortize TCP ramp-up, measured upload collapses (~2x low
# in A/B comparisons). Download rounds need no override - the downloader
# already caps each round at config['length']['download'] (10 s).
UPLOAD_ROUND_SIZES = [524288, 1048576, 7340032]
UPLOAD_ROUND_COUNTS = 8


def _measure_latency_and_jitter(
    base_url: str, probes: int | None = None, timeout: float = 5.0
) -> tuple[float | None, float | None]:
    """Measure true round-trip latency and jitter against a speedtest server.

    speedtest-cli's own ping value divides 3 samples by 6 (see
    Speedtest.get_best_server), which reports roughly half the real RTT, and it
    provides no jitter at all. We probe latency.txt ourselves: one discarded
    warm-up request, then `probes` measured ones over fresh connections.
    Latency = mean RTT (ms); jitter = mean absolute delta between consecutive
    samples (same definition the dashboard uses for its live jitter).
    """
    if not base_url.startswith(("http://", "https://")):
        return None, None
    probe_count = probes if probes is not None else LATENCY_PROBES
    samples: list[float] = []
    consecutive_failures = 0
    for i in range(probe_count + 1):
        url = f"{base_url.rstrip('/')}/latency.txt?x={int(time.time() * 1000)}.{i}"
        request = urllib.request.Request(url, headers={"User-Agent": "speedtest-cli/2.1.3"})
        start = time.perf_counter()
        try:
            with _HTTP_OPENER.open(request, timeout=timeout) as response:
                response.read(9)
        except Exception:  # noqa: BLE001
            consecutive_failures += 1
            if consecutive_failures >= PROBE_FAIL_FAST and not samples:
                return None, None  # unreachable - skip the remaining timeouts
            continue
        consecutive_failures = 0
        elapsed_ms = (time.perf_counter() - start) * 1000
        if i > 0:  # first probe is warm-up (DNS/connection setup)
            samples.append(elapsed_ms)
    if not samples:
        return None, None
    latency_ms = statistics.fmean(samples)
    deltas = [abs(b - a) for a, b in zip(samples, samples[1:])]
    jitter_ms = statistics.fmean(deltas) if deltas else 0.0
    return round(latency_ms, 1), round(jitter_ms, 1)


JS_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.speedtest.net/",
}


def _pinned_server() -> dict | None:
    """Server entry for a manually pinned speedtest server, if configured."""
    if not SERVER_URL:
        return None
    host = urllib.parse.urlsplit(SERVER_URL).netloc or SERVER_URL
    return {
        "id": SERVER_ID or "pinned",
        "name": host,
        "sponsor": "pinned",
        "cc": "",
        "url": SERVER_URL,
        "d": 0,
    }


def _force_server(st: "speedtest.Speedtest", server: dict) -> None:
    """Use `server` without speedtest-cli's redundant latency round-trips.

    We measure RTT/jitter ourselves, so the three probe requests
    get_best_server would fire are pointless (and they slow down dead-server
    detection).
    """
    st.results.server = server
    st.results.ping = 0.0
    st._best.update(server)


def _region_candidates() -> list[dict]:
    """Nearby speedtest.net servers around SERVER_LAT/SERVER_LON.

    Uses the speedtest.net JS API instead of speedtest-cli's server list,
    because the latter is geo-filtered by the client IP and Starlink CGNAT can
    point it at a completely different region.
    """
    url = (
        "https://www.speedtest.net/api/js/servers?engine=js"
        f"&limit=20&lat={SERVER_LAT}&lon={SERVER_LON}"
    )
    try:
        request = urllib.request.Request(url, headers=JS_API_HEADERS)
        with _HTTP_OPENER.open(request, timeout=15) as response:
            servers = json.load(response)
    except Exception:  # noqa: BLE001
        logger.exception("speedtest.net server lookup failed")
        return []

    candidates = []
    for server in servers[:CANDIDATE_LIMIT]:
        server_url = str(server.get("url", ""))
        if not server_url.startswith(("http://", "https://")):
            continue
        candidates.append({
            "id": str(server.get("id", "")),
            "name": server.get("name") or server.get("sponsor") or str(server.get("id", "")),
            "sponsor": server.get("sponsor") or "",
            "cc": server.get("cc") or "",
            "url": server_url,
            "d": 0,
        })
    return candidates


def _select_region_server() -> dict | None:
    """Probe nearby candidates (in parallel) with real RTT, keep the fastest."""
    candidates = _region_candidates()
    if not candidates:
        return None

    def probe(server: dict) -> tuple[dict, float | None, float | None]:
        latency_ms, jitter_ms = _measure_latency_and_jitter(
            os.path.dirname(server["url"]),
            probes=CANDIDATE_PROBES,
            timeout=CANDIDATE_TIMEOUT_S,
        )
        return server, latency_ms, jitter_ms

    best: dict | None = None
    best_latency: float | None = None
    with ThreadPoolExecutor(max_workers=CANDIDATE_WORKERS) as pool:
        for server, latency_ms, jitter_ms in pool.map(probe, candidates):
            if latency_ms is None:
                continue
            logger.info(
                "Candidate %s (%s): %.1f ms, jitter %.1f ms",
                server["id"], server["name"], latency_ms, jitter_ms,
            )
            if best_latency is None or latency_ms < best_latency:
                best, best_latency = server, latency_ms
    if best is not None:
        logger.info(
            "Selected speedtest server: %s (%s) at %.1f ms",
            best["id"], best["name"], best_latency,
        )
    return best


_selected_server: dict | None = None


def _configure_best_server(st: "speedtest.Speedtest", force_reselect: bool = False) -> None:
    """Select the speedtest server.

    Priority: manual pin (SPEEDTEST_SERVER_URL) > cached auto pick > fresh
    region-aware auto pick > speedtest-cli's own geo selection as last resort.
    """
    global _selected_server

    pinned = _pinned_server()
    if pinned:
        _force_server(st, pinned)
        logger.info("Using pinned speedtest server: %s (%s)", pinned["id"], pinned["url"])
        return

    if SERVER_ID:
        try:
            matches = [s for lst in st.get_servers([SERVER_ID]).values() for s in lst]
            if matches:
                _force_server(st, matches[0])
                return
            logger.warning(
                "SPEEDTEST_SERVER_ID=%s not found - continuing with auto selection",
                SERVER_ID,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "SPEEDTEST_SERVER_ID=%s lookup failed - continuing with auto selection",
                SERVER_ID,
            )

    if _selected_server is not None and not force_reselect:
        _force_server(st, _selected_server)
        return

    server = _select_region_server()
    if server is not None:
        _selected_server = server
        _force_server(st, server)
        return

    logger.warning("Region-aware selection unavailable - using speedtest-cli auto selection")
    st.get_best_server()


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


def _apply_round_config(st: "speedtest.Speedtest") -> None:
    """Use smaller upload rounds than the default for a stable run duration."""
    st.config["sizes"]["upload"] = list(UPLOAD_ROUND_SIZES)
    st.config["counts"]["upload"] = UPLOAD_ROUND_COUNTS
    st.config["upload_max"] = len(UPLOAD_ROUND_SIZES) * UPLOAD_ROUND_COUNTS


def _run_speedtest_blocking() -> dict:
    st = speedtest.Speedtest()
    _apply_round_config(st)
    _configure_best_server(st)

    # Own RTT/jitter probes before any load is applied
    server_url = os.path.dirname(st.best["url"])
    latency_ms, jitter_ms = _measure_latency_and_jitter(server_url)

    if latency_ms is None and _pinned_server() is None:
        # Auto-selected server died mid-flight: drop it and pick another once
        logger.warning("Selected server unreachable (%s) - selecting a new one", server_url)
        _configure_best_server(st, force_reselect=True)
        server_url = os.path.dirname(st.best["url"])
        latency_ms, jitter_ms = _measure_latency_and_jitter(server_url)

    if latency_ms is None:
        raise RuntimeError(f"Speedtest server unreachable: {server_url}")

    download_bps = _sustained_measure(st, st.download, "bytes_received")
    upload_bps = _sustained_measure(st, st.upload, "bytes_sent")

    if download_bps <= 0 and upload_bps <= 0:
        raise RuntimeError("Speedtest measured 0 Mbit/s in both directions - not storing")

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
        "speedtest_runner starting, schedule %s (Europe/Berlin), %ss measurement window per direction",
        SCHEDULE_HOURS_BERLIN, MIN_TEST_DURATION_S,
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
            if result is None:
                await send_alert(
                    title="🟡 Speedtest failed",
                    description="No measurement stored (server unreachable or zero throughput).",
                    color=COLOR_YELLOW,
                    db=db,
                )
                continue
            logger.info(
                "Speedtest @ %s: %.1f Mbit/s down, %.1f Mbit/s up, %.0f ms latency, %.1f ms jitter",
                result["server"], result["download_mbit"], result["upload_mbit"],
                result["latency_ms"] or 0, result["jitter_ms"] or 0,
            )
            await insert_speedtest(db, result)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
