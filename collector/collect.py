"""
collect.py
Einstiegspunkt des `collector`-Containers.
Startet metrics_collector, ping_watchdog, weather_poller, speedtest_runner
und den cleanup-Job als parallele asyncio-Tasks in einem Prozess.

Stuerzt ein Task ab, werden die anderen weiterhin geloggt (kein globaler Crash),
aber der Prozess beendet sich, damit Docker (restart: always) ihn neu startet -
so vermeiden wir einen "halb-toten" Container mit nur 2 von 5 laufenden Tasks.
"""

import asyncio
import logging
import sys

from db import init_db, get_db
import metrics_collector
import ping_watchdog
import weather_poller
import speedtest_runner
import cleanup
import discord_alert
import metrics_minutely_aggregator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("collect")


async def main():
    logger.info("Starlink-Monitor Collector startet...")
    await init_db()
    logger.info("DB-Schema initialisiert.")

    alert_queue_db = await get_db()

    tasks = [
        asyncio.create_task(metrics_collector.run(), name="metrics_collector"),
        asyncio.create_task(ping_watchdog.run(), name="ping_watchdog"),
        asyncio.create_task(weather_poller.run(), name="weather_poller"),
        asyncio.create_task(speedtest_runner.run(), name="speedtest_runner"),
        asyncio.create_task(cleanup.run(), name="cleanup"),
        asyncio.create_task(discord_alert.retry_queue_loop(alert_queue_db), name="discord_retry_queue"),
        asyncio.create_task(metrics_minutely_aggregator.run(), name="metrics_minutely_aggregator"),
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    for task in done:
        exc = task.exception()
        if exc:
            logger.error("Task '%s' ist abgestuerzt: %s", task.get_name(), exc, exc_info=exc)

    for task in pending:
        task.cancel()

    logger.error("Mindestens ein Collector-Task ist beendet - Prozess wird beendet (Docker restartet).")
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
