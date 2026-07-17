"""
weather_poller.py
Pollt Open-Meteo (kostenlos, kein Key) fuer Krefeld alle 10 Minuten.
Schreibt in Tabelle `weather` und sendet bei aktiver Unwetterwarnung
(Gewitter, Sturm Bft 7+) einen Discord-Alert.
"""

import asyncio
import logging
import time

import aiohttp

from db import get_db
from discord_alert import send_alert, COLOR_YELLOW
from weather_state import set_last_weather

logger = logging.getLogger("weather_poller")

LAT = 51.3388
LON = 6.5853
POLL_INTERVAL_S = 10 * 60

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,wind_speed_10m,weather_code,precipitation,relative_humidity_2m,visibility"
    "&timezone=Europe%2FBerlin"
)

# WMO-Codes die wir als "Unwetter" werten (Gewitter, starker Schneefall/Hagel etc.)
THUNDERSTORM_CODES = {95, 96, 99}
HEAVY_SNOW_CODES = {75, 86}
WIND_WARNING_KMH = 50  # entspricht etwa Bft 7 (Sturm)

WMO_DESCRIPTIONS = {
    0: "Klar", 1: "Ueberwiegend klar", 2: "Teilweise bewoelkt", 3: "Bedeckt",
    45: "Nebel", 48: "Reifnebel",
    51: "Leichter Niesel", 53: "Niesel", 55: "Starker Niesel",
    61: "Leichter Regen", 63: "Regen", 65: "Starker Regen",
    71: "Leichter Schneefall", 73: "Schneefall", 75: "Starker Schneefall",
    77: "Schneegriesel", 80: "Regenschauer", 81: "Regenschauer", 82: "Heftige Regenschauer",
    85: "Schneeschauer", 86: "Starke Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Schweres Gewitter mit Hagel",
}


def evaluate_warning(wmo_code: int, wind_kmh: float) -> str | None:
    if wmo_code in THUNDERSTORM_CODES:
        return f"Gewitter ({WMO_DESCRIPTIONS.get(wmo_code, 'Unwetter')})"
    if wmo_code in HEAVY_SNOW_CODES:
        return f"Starker Schneefall ({WMO_DESCRIPTIONS.get(wmo_code, 'Unwetter')})"
    if wind_kmh >= WIND_WARNING_KMH:
        return f"Sturmwarnung (Wind {wind_kmh:.0f} km/h)"
    return None


async def fetch_weather() -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(OPEN_METEO_URL, timeout=15) as resp:
                if resp.status != 200:
                    logger.warning("Open-Meteo HTTP %s", resp.status)
                    return None
                data = await resp.json()
                current = data.get("current", {})
                return {
                    "temp_c": current.get("temperature_2m"),
                    "wind_kmh": current.get("wind_speed_10m"),
                    "wmo_code": current.get("weather_code"),
                    "precipitation_mm": current.get("precipitation"),
                    "visibility_m": current.get("visibility"),
                    "humidity": current.get("relative_humidity_2m"),
                }
    except Exception:  # noqa: BLE001
        logger.exception("Fehler beim Abruf der Open-Meteo API")
        return None


async def insert_weather(db, row: dict, warning: str | None):
    await db.execute(
        """
        INSERT INTO weather
            (ts, temp_c, wind_kmh, wmo_code, precipitation_mm, visibility_m, humidity, warning)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(time.time()),
            row["temp_c"],
            row["wind_kmh"],
            row["wmo_code"],
            row["precipitation_mm"],
            row["visibility_m"],
            row["humidity"],
            warning,
        ),
    )
    await db.commit()


async def run():
    logger.info("weather_poller startet fuer Krefeld (%s, %s), Intervall %ss", LAT, LON, POLL_INTERVAL_S)
    db = await get_db()

    last_warning_sent: str | None = None

    try:
        while True:
            row = await fetch_weather()
            if row is not None:
                warning = None
                if row["wmo_code"] is not None and row["wind_kmh"] is not None:
                    warning = evaluate_warning(row["wmo_code"], row["wind_kmh"])

                await insert_weather(db, row, warning)
                set_last_weather(row["temp_c"], row["wind_kmh"], row["wmo_code"], warning)

                # Nur bei neuer/wechselnder Warnung alarmieren, nicht jede 10 Minuten erneut
                if warning and warning != last_warning_sent:
                    await send_alert(
                        title="⚡ Unwetterwarnung",
                        description=warning,
                        color=COLOR_YELLOW,
                        fields={
                            "Temperatur": f"{row['temp_c']}°C" if row["temp_c"] is not None else "n/a",
                            "Wind": f"{row['wind_kmh']} km/h" if row["wind_kmh"] is not None else "n/a",
                        },
                        db=db,
                    )
                last_warning_sent = warning

            await asyncio.sleep(POLL_INTERVAL_S)
    finally:
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
