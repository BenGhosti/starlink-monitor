"""
weather_state.py
Sehr einfacher In-Process Shared State: weather_poller.py schreibt hier den
letzten Wetter-Snapshot hinein, ping_watchdog.py liest ihn fuer Disconnect-Alerts.
Da beide Coroutinen im selben asyncio-Event-Loop / Prozess laufen (siehe collect.py),
reicht ein einfaches Modul-Level-Dict ohne Locking.
"""

_last_weather: dict = {}


def set_last_weather(temp_c: float | None, wind_kmh: float | None, wmo_code: int | None, warning: str | None):
    global _last_weather
    _last_weather = {
        "temp_c": temp_c,
        "wind_kmh": wind_kmh,
        "wmo_code": wmo_code,
        "warning": warning,
    }


def get_last_weather_summary() -> str:
    if not _last_weather:
        return "keine Daten"
    parts = []
    if _last_weather.get("temp_c") is not None:
        parts.append(f"{_last_weather['temp_c']:.0f}°C")
    if _last_weather.get("wind_kmh") is not None:
        parts.append(f"Wind {_last_weather['wind_kmh']:.0f} km/h")
    if _last_weather.get("warning"):
        parts.append(f"⚡ {_last_weather['warning']}")
    return " · ".join(parts) if parts else "keine Daten"
