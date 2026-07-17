"""
discord_alert.py
Helper fuer Discord-Embed-Webhooks mit persistenter Retry-Queue.

Warum eine Queue: faellt das Internet selbst aus (nicht nur die Starlink-
Verbindung, sondern z.B. der Heimrouter/das Modem dahinter, oder Discord ist
kurz nicht erreichbar), schlaegt der Webhook-POST fehl. Ohne Queue ist der
Alert dann einfach verloren - inklusive der Information "Verbindung ist
gerade ausgefallen", die ja eigentlich der Punkt des Alerts war. Stattdessen
landet ein fehlgeschlagener Alert in `alert_queue` (SQLite, ueberlebt auch
einen Container-Neustart) und ein Hintergrund-Task versucht ihn periodisch
erneut zu senden, bis es klappt.
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
MAX_QUEUE_AGE_S = 24 * 3600  # Alerts aelter als 1 Tag sind nicht mehr relevant, verwerfen
MAX_ATTEMPTS = 200  # ~200 * 30s ≈ 100 Minuten Dauerversuch, danach als Spam-Schutz aufgeben


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
    """Sendet einen fertigen Payload an Discord. True bei Erfolg, False bei Fehler."""
    if not WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK nicht gesetzt, Alert wird verworfen.")
        return True  # kein Retry sinnvoll, wenn ueberhaupt keine URL konfiguriert ist

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WEBHOOK_URL, json=payload, timeout=10) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    logger.error("Discord-Webhook fehlgeschlagen (%s): %s", resp.status, body)
                    return False
                return True
    except Exception as exc:  # noqa: BLE001 - Netzwerkfehler sind hier der Normalfall, nicht die Ausnahme
        logger.warning("Discord-Webhook nicht erreichbar (wird erneut versucht): %s", exc)
        return False


async def send_alert(title: str, description: str, color: int = COLOR_RED, fields: dict | None = None, db=None):
    """Sendet eine Embed-Nachricht an den Discord-Webhook.

    Schlaegt der direkte Versand fehl UND ist eine db-Connection uebergeben,
    wird der Alert in `alert_queue` zwischengespeichert statt verworfen.
    Ohne db-Parameter (Abwaertskompatibilitaet) wird bei Fehlschlag nur
    geloggt, wie bisher.
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
    logger.info("Alert in Retry-Queue eingereiht (Discord aktuell nicht erreichbar).")


async def retry_queue_loop(db):
    """Hintergrund-Task: versucht periodisch, in der Queue wartende Alerts zu versenden.

    Wird als eigener asyncio-Task aus collect.py gestartet, parallel zu den
    anderen Collector-Tasks (metrics_collector, ping_watchdog, etc.) - siehe
    dortige Task-Liste.
    """
    logger.info("Discord-Retry-Queue-Loop startet, Intervall %ss", RETRY_INTERVAL_S)
    while True:
        try:
            await _process_queue_once(db)
        except Exception:  # noqa: BLE001
            logger.exception("Fehler im Discord-Retry-Queue-Loop")
        await asyncio.sleep(RETRY_INTERVAL_S)


async def _process_queue_once(db):
    import time
    now = int(time.time())

    # Veraltete Alerts (z.B. ein Disconnect-Alert von vor 2 Tagen) sind nicht
    # mehr relevant zum Nachsenden - aufraeumen statt ewig zu versuchen.
    await db.execute("DELETE FROM alert_queue WHERE created_ts < ?", (now - MAX_QUEUE_AGE_S,))
    await db.execute("DELETE FROM alert_queue WHERE attempts >= ?", (MAX_ATTEMPTS,))
    await db.commit()

    async with db.execute(
        "SELECT id, payload_json, attempts FROM alert_queue ORDER BY created_ts ASC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return

    logger.info("Discord-Retry-Queue: %s wartende Alert(s), versuche Versand...", len(rows))

    for row in rows:
        queue_id, payload_json, attempts = row[0], row[1], row[2]
        payload = json.loads(payload_json)
        success = await _post_to_discord(payload)

        if success:
            await db.execute("DELETE FROM alert_queue WHERE id = ?", (queue_id,))
            await db.commit()
            logger.info("Alert aus Queue %s erfolgreich nachgesendet.", queue_id)
        else:
            await db.execute(
                "UPDATE alert_queue SET attempts = ?, last_attempt_ts = ? WHERE id = ?",
                (attempts + 1, now, queue_id),
            )
            await db.commit()
            # Discord vermutlich/Internet noch nicht zurueck - restliche Queue
            # auch nicht weiter versuchen in diesem Durchlauf, naechster Loop
            # in RETRY_INTERVAL_S Sekunden reicht.
            break
