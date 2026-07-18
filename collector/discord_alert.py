"""
discord_alert.py
Helper for Discord embed webhooks with a persistent retry queue.

Why a queue: if the internet itself goes down (not just the Starlink link,
but the home router/modem behind it, or Discord is briefly unreachable), the
webhook POST fails. Without a queue that alert - including the "connection
just went down" information it was meant to carry - is simply lost. Instead,
a failed alert lands in `alert_queue` (SQLite, survives a container
restart) and a background task keeps retrying it until it succeeds.
"""

import asyncio
import json
import os
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger("discord_alert")

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK", "")

COLOR_RED = 0xFF3B5C
COLOR_YELLOW = 0xFFB300
COLOR_GREEN = 0x00FF88

RETRY_INTERVAL_S = 30
MAX_QUEUE_AGE_S = 24 * 3600  # alerts older than 1 day are no longer relevant, drop them
MAX_ATTEMPTS = 200  # ~200 * 30s ~= 100 minutes of retrying before giving up


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_payload(title: str, description: str, color: int, fields: dict | None) -> dict:
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": _now_iso(),
    }
    if fields:
        embed["fields"] = [
            {"name": k, "value": str(v), "inline": True} for k, v in fields.items()
        ]
    return {"embeds": [embed]}


async def _post_to_discord(payload: dict) -> bool:
    """Sends a finished payload to Discord. True on success, False on failure."""
    if not WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK not set, dropping alert.")
        return True  # no point retrying if there's no URL configured at all

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WEBHOOK_URL, json=payload, timeout=10) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    logger.error("Discord webhook failed (%s): %s", resp.status, body)
                    return False
                return True
    except Exception as exc:  # noqa: BLE001 - network errors are the expected case here, not the exception
        logger.warning("Discord webhook unreachable (will retry): %s", exc)
        return False


async def send_alert(title: str, description: str, color: int = COLOR_RED, fields: dict | None = None, db=None):
    """Sends an embed message to the Discord webhook.

    If the direct send fails AND a db connection was passed, the alert is
    queued in `alert_queue` instead of being dropped. Without db (backwards
    compatible), a failure is just logged as before.
    """
    payload = _build_payload(title, description, color, fields)
    success = await _post_to_discord(payload)

    if not success and db is not None:
        await _enqueue(db, payload)


async def _enqueue(db, payload: dict):
    import time
    now = int(time.time())
    await db.execute(
        "INSERT INTO alert_queue (created_ts, payload_json, attempts, last_attempt_ts) VALUES (?, ?, 1, ?)",
        (now, json.dumps(payload, ensure_ascii=False), now),
    )
    await db.commit()
    logger.info("Alert queued for retry (Discord currently unreachable).")


async def retry_queue_loop(db):
    """Background task that periodically retries alerts waiting in the queue.
    Started as its own asyncio task from collect.py, alongside the other
    collector tasks (metrics_collector, ping_watchdog, etc.)."""
    logger.info("Discord retry-queue loop starting, interval %ss", RETRY_INTERVAL_S)
    while True:
        try:
            await _process_queue_once(db)
        except Exception:  # noqa: BLE001
            logger.exception("Error in Discord retry-queue loop")
        await asyncio.sleep(RETRY_INTERVAL_S)


async def _process_queue_once(db):
    import time
    now = int(time.time())

    # Stale alerts (e.g. a disconnect alert from 2 days ago) are no longer
    # worth resending - clean them up instead of retrying forever.
    await db.execute("DELETE FROM alert_queue WHERE created_ts < ?", (now - MAX_QUEUE_AGE_S,))
    await db.execute("DELETE FROM alert_queue WHERE attempts >= ?", (MAX_ATTEMPTS,))
    await db.commit()

    async with db.execute(
        "SELECT id, payload_json, attempts FROM alert_queue ORDER BY created_ts ASC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return

    logger.info("Discord retry queue: %s pending alert(s), attempting delivery...", len(rows))

    for row in rows:
        queue_id, payload_json, attempts = row[0], row[1], row[2]
        payload = json.loads(payload_json)
        success = await _post_to_discord(payload)

        if success:
            await db.execute("DELETE FROM alert_queue WHERE id = ?", (queue_id,))
            await db.commit()
            logger.info("Alert %s resent successfully from the queue.", queue_id)
        else:
            await db.execute(
                "UPDATE alert_queue SET attempts = ?, last_attempt_ts = ? WHERE id = ?",
                (attempts + 1, now, queue_id),
            )
            await db.commit()
            # Discord/internet is probably still down - don't keep trying
            # the rest of the queue this pass, the next loop in
            # RETRY_INTERVAL_S seconds is enough.
            break
