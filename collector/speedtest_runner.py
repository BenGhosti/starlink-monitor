"""
speedtest_runner.py
Fuehrt Speedtests via speedtest-cli (Ookla) zu festen Uhrzeiten aus:
00:00, 08:00 und 16:00 Uhr Europe/Berlin (wiederholt sich taeglich - siehe
SCHEDULE_HOURS_BERLIN), statt einfach alle 8h ab Programmstart.

Sustained-Messung: speedtest-cli laedt pro Aufruf nur ein FESTES Datenvolumen
(dimensioniert von speedtest.net fuer "durchschnittliche" Leitungen mit
Zieldauer ~10s). Bei sehr schnellen Verbindungen (Starlink) ist dieses Volumen
in 2-3 Sekunden durchgeladen -> kurze, ungenaue Messung statt einer echten
"mehrere Sekunden"-Messung wie beim offiziellen Ookla-Client (der bei
schnellen Leitungen automatisch mehr Volumen nachlaedt). Wir wiederholen die
Messrunden daher, bis insgesamt mindestens MIN_TEST_DURATION_S vergangen sind,
und mitteln ueber die Summe aller Runden (Gesamt-Bytes / Gesamt-Zeit).

Schreibt Ergebnis in `speedtests` UND zusaetzlich ein Event in `events`
(Typ: speedtest) fuer die Anzeige im Event-Log.
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
SCHEDULE_HOURS_BERLIN = [0, 8, 16]  # taeglich, wiederholt sich
MIN_TEST_DURATION_S = int(os.environ.get("SPEEDTEST_MIN_DURATION_S", "10"))
MAX_TEST_ROUNDS = 20  # Sicherheitsobergrenze gegen Endlosschleifen bei sehr schnellen Leitungen


def _next_run_time(now_utc: datetime) -> datetime:
    """Naechster Termin aus SCHEDULE_HOURS_BERLIN nach `now_utc` (Europe/Berlin, DST-sicher)."""
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
    """Wiederholt `run_once()` (st.download oder st.upload), bis insgesamt
    MIN_TEST_DURATION_S Sekunden vergangen sind, und gibt den sustained
    Durchsatz in bit/s ueber die Summe aller Runden zurueck."""
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
    """speedtest-cli ist synchron/blocking - wird daher in run_in_executor ausgefuehrt."""
    st = speedtest.Speedtest()
    st.get_best_server()

    download_bps = _sustained_measure(st, st.download, "bytes_received")
    upload_bps = _sustained_measure(st, st.upload, "bytes_sent")
    results = st.results.dict()

    return {
        "download_mbit": download_bps / 1_000_000,
        "upload_mbit": upload_bps / 1_000_000,
        "latency_ms": results.get("ping"),
        "jitter_ms": results.get("jitter"),  # nicht in allen speedtest-cli Versionen vorhanden
        "server": results.get("server", {}).get("name", "unknown"),
    }


async def run_speedtest() -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _run_speedtest_blocking)
    except Exception:  # noqa: BLE001
        logger.exception("Speedtest fehlgeschlagen")
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
        "speedtest_runner startet, Zeitplan %s Uhr Europe/Berlin (min. %ss/Richtung)",
        SCHEDULE_HOURS_BERLIN, MIN_TEST_DURATION_S,
    )
    db = await get_db()

    try:
        while True:
            now = datetime.now(timezone.utc)
            target = _next_run_time(now)
            sleep_s = (target - now).total_seconds()
            logger.info("Naechster Speedtest um %s (in %.0f min)", target.isoformat(), sleep_s / 60)
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
