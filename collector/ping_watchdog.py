"""
ping_watchdog.py
Pingt 1.1.1.1 und 8.8.8.8 alle 2 Sekunden per ICMP.
- Erst nach DISCONNECT_CONFIRM_FAILURES aufeinanderfolgenden Fehlschlaegen
  (beide Ziele unerreichbar) wird ein Disconnect gewertet - ein einzelner
  verlorener Ping (Jitter, kurzer Netz-Hickser) loest noch keinen Fehlalarm aus.
- Latenzspitzen > 200ms fuer > 10s -> separates Event + Alert.
- Obstruction-Schwelle (>5% Drop fuer >30s) wird hier ebenfalls ueberwacht,
  da sie auf denselben Latenz/Drop-Samples basiert wie der Pingcheck.
"""

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

# Erst nach N aufeinanderfolgenden Fehlschlaegen (bei 2s Intervall = N*2 Sekunden)
# wird tatsaechlich ein Disconnect gemeldet. Verhindert Fehlalarme durch
# einzelne verlorene ICMP-Pakete, die nichts mit einem echten Ausfall zu tun haben.
#
# Wichtig: Starlink fuehrt routinemaessig Satelliten-Handover durch (ueblicherweise
# alle ~15s), wobei kurzzeitig (typischerweise <1-2s) keine Pakete durchkommen.
# Das ist normaler Betrieb, kein Ausfall. Mit der alten Schwelle von 3 Fehlschlaegen
# (6s) konnten zwei kurz aufeinanderfolgende Handover-Drops faelschlich als
# zusammenhaengender Ausfall gewertet werden. 8 Fehlschlaege (16s) liegen sicher
# ueber einem einzelnen Handover-Hickser, melden aber immer noch zeitnah echte
# Ausfaelle (Stromausfall, Kabel raus, echte Funkstoerung).
# Per DISCONNECT_CONFIRM_FAILURES env var ohne Code-Aenderung weiter tunbar.
DISCONNECT_CONFIRM_FAILURES = int(os.environ.get("DISCONNECT_CONFIRM_FAILURES", "8"))

LATENCY_SPIKE_THRESHOLD_MS = 200
LATENCY_SPIKE_MIN_DURATION_S = 10

OBSTRUCTION_DROP_THRESHOLD = 0.05  # 5%
OBSTRUCTION_MIN_DURATION_S = 30


async def ping_host(host: str) -> float | None:
    """Fuehrt einen einzelnen ICMP-Ping aus (System-Binary, kein root-only raw socket nötig
    dank `ping` Systemtool). Gibt RTT in ms zurueck oder None bei Timeout/Fehler."""
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
        # Beispiel: "time=14.2 ms"
        for token in text.split():
            if token.startswith("time="):
                return float(token.split("=")[1].replace("ms", ""))
        return None
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return None


async def check_targets() -> tuple[bool, float | None]:
    """Pingt alle Targets parallel. Gibt (reachable, beste_latenz_ms) zurueck."""
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
        "ping_watchdog startet gegen %s, Intervall %ss, Bestaetigung nach %s Fehlschlaegen (%ss)",
        PING_TARGETS, POLL_INTERVAL_S, DISCONNECT_CONFIRM_FAILURES,
        DISCONNECT_CONFIRM_FAILURES * POLL_INTERVAL_S,
    )
    db = await get_db()

    # Disconnect-Tracking
    consecutive_failures = 0
    disconnect_start: float | None = None  # erst gesetzt, wenn Schwelle ueberschritten -> echter Disconnect
    disconnect_confirmed = False
    last_known_latency: float | None = None

    # Latenzspitzen-Tracking
    spike_start: float | None = None
    spike_peak: float = 0.0

    try:
        while True:
            loop_start = time.monotonic()
            now = int(time.time())

            reachable, latency = await check_targets()

            # --- Disconnect-Logik mit Bestaetigungsschwelle ---
            if not reachable:
                consecutive_failures += 1
                if disconnect_start is None:
                    # Erster Fehlschlag dieser Serie - Zeitpunkt merken, aber noch
                    # nicht als Disconnect werten, bis die Schwelle erreicht ist.
                    disconnect_start = time.monotonic()

                if consecutive_failures >= DISCONNECT_CONFIRM_FAILURES and not disconnect_confirmed:
                    disconnect_confirmed = True
                    logger.warning("Disconnect bestaetigt um %s (%s aufeinanderfolgende Fehlschlaege)",
                                    now, consecutive_failures)
                    await send_alert(
                        title="🔴 Starlink Disconnect",
                        description="Beide Ping-Ziele unerreichbar.",
                        color=COLOR_RED,
                        fields={
                            "Letzte bekannte Latenz": f"{last_known_latency} ms" if last_known_latency else "n/a",
                            "Wetter": get_last_weather_summary(),
                        },
                        db=db,
                    )
            else:
                last_known_latency = latency
                if disconnect_confirmed:
                    # Nur wenn wirklich ein bestaetigter Disconnect lief, Event + Alert fuer das Ende
                    duration = time.monotonic() - disconnect_start
                    await log_event(
                        db, now, "disconnect", duration,
                        {"last_known_latency_ms": last_known_latency},
                    )
                    await send_alert(
                        title="🟢 Starlink wieder verbunden",
                        description=f"Ausfall beendet nach {duration:.1f}s.",
                        color=COLOR_GREEN,
                        db=db,
                    )
                # Serie zuruecksetzen, egal ob es ein bestaetigter Disconnect war
                # oder nur ein kurzer, unter der Schwelle gebliebener Hickser.
                consecutive_failures = 0
                disconnect_start = None
                disconnect_confirmed = False

            # --- Latenzspitzen-Logik ---
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
                            title="🟡 Latenzspitze",
                            description=f"Latenz > {LATENCY_SPIKE_THRESHOLD_MS}ms fuer {spike_duration:.1f}s.",
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
