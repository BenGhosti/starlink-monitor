import asyncio
import json
import logging
import os
import time

from db import get_db
from discord_alert import send_alert, COLOR_RED, COLOR_YELLOW, COLOR_GREEN
from weather_state import get_last_weather_summary

logger = logging.getLogger("ping_watchdog")

PING_TARGETS = ["1.1.1.1", "8.8.8.8"]
POLL_INTERVAL_S = 2
PING_TIMEOUT_S = 1.5

DISCONNECT_CONFIRM_FAILURES = int(os.environ.get("DISCONNECT_CONFIRM_FAILURES", "8"))

LATENCY_SPIKE_THRESHOLD_MS = 200
LATENCY_SPIKE_MIN_DURATION_S = 10

OBSTRUCTION_DROP_THRESHOLD = 0.05
OBSTRUCTION_MIN_DURATION_S = 30


async def ping_host(host: str) -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(PING_TIMEOUT_S), host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=PING_TIMEOUT_S + 1)
        if proc.returncode != 0:
            return None
        text = stdout.decode(errors="ignore")
        for token in text.split():
            if token.startswith("time="):
                return float(token.split("=")[1].replace("ms", ""))
        return None
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return None


async def check_targets() -> tuple[bool, float | None]:
    results = await asyncio.gather(*(ping_host(h) for h in PING_TARGETS))
    latencies = [r for r in results if r is not None]
    reachable = len(latencies) > 0
    best_latency = min(latencies) if latencies else None
    return reachable, best_latency


async def log_event(db, ts: int, type_: str, duration_s: float | None, details: dict):
    await db.execute(
        "INSERT INTO events (ts, type, duration_s, details) VALUES (?, ?, ?, ?)",
        (ts, type_, duration_s, json.dumps(details, ensure_ascii=False)),
    )
    await db.commit()


async def run():
    logger.info(
        "ping_watchdog starting against %s, interval %ss, confirm after %s failures (%ss)",
        PING_TARGETS, POLL_INTERVAL_S, DISCONNECT_CONFIRM_FAILURES,
        DISCONNECT_CONFIRM_FAILURES * POLL_INTERVAL_S,
    )
    db = await get_db()

    consecutive_failures = 0
    disconnect_start: float | None = None
    disconnect_confirmed = False
    last_known_latency: float | None = None

    spike_start: float | None = None
    spike_peak: float = 0.0

    try:
        while True:
            loop_start = time.monotonic()
            now = int(time.time())

            reachable, latency = await check_targets()

            if not reachable:
                consecutive_failures += 1
                if disconnect_start is None:
                    disconnect_start = time.monotonic()

                if consecutive_failures >= DISCONNECT_CONFIRM_FAILURES and not disconnect_confirmed:
                    disconnect_confirmed = True
                    logger.warning("Disconnect confirmed at %s (%s consecutive failures)",
                                    now, consecutive_failures)
                    await send_alert(
                        title="🔴 Starlink Disconnect",
                        description="Both ping targets unreachable.",
                        color=COLOR_RED,
                        fields={
                            "Last known latency": f"{last_known_latency} ms" if last_known_latency else "n/a",
                            "Weather": get_last_weather_summary(),
                        },
                        db=db,
                    )
            else:
                last_known_latency = latency
                if disconnect_confirmed:
                    duration = time.monotonic() - disconnect_start
                    await log_event(
                        db, now, "disconnect", duration,
                        {"last_known_latency_ms": last_known_latency},
                    )
                    await send_alert(
                        title="🟢 Starlink Reconnected",
                        description=f"Outage ended after {duration:.1f}s.",
                        color=COLOR_GREEN,
                        db=db,
                    )
                consecutive_failures = 0
                disconnect_start = None
                disconnect_confirmed = False

            if reachable and latency is not None and latency > LATENCY_SPIKE_THRESHOLD_MS:
                if spike_start is None:
                    spike_start = time.monotonic()
                    spike_peak = latency
                else:
                    spike_peak = max(spike_peak, latency)
            else:
                if spike_start is not None:
                    spike_duration = time.monotonic() - spike_start
                    if spike_duration >= LATENCY_SPIKE_MIN_DURATION_S:
                        await log_event(
                            db, now, "latency_spike", spike_duration,
                            {"peak_ms": spike_peak},
                        )
                        await send_alert(
                            title="🟡 Latency Spike",
                            description=f"Latency > {LATENCY_SPIKE_THRESHOLD_MS}ms for {spike_duration:.1f}s.",
                            color=COLOR_YELLOW,
                            fields={"Peak": f"{spike_peak:.0f} ms"},
                            db=db,
                        )
                    spike_start = None
                    spike_peak = 0.0

            elapsed = time.monotonic() - loop_start
            await asyncio.sleep(max(0.0, POLL_INTERVAL_S - elapsed))
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
