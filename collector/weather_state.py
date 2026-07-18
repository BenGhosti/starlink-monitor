"""
weather_state.py
Minimal in-process shared state: weather_poller.py writes the latest weather
snapshot here, ping_watchdog.py reads it for disconnect alerts. Both
coroutines run in the same asyncio event loop/process (see collect.py), so a
plain module-level dict without locking is sufficient.
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
        return "no data"
    parts = []
    if _last_weather.get("temp_c") is not None:
        parts.append(f"{_last_weather['temp_c']:.0f}°C")
    if _last_weather.get("wind_kmh") is not None:
        parts.append(f"wind {_last_weather['wind_kmh']:.0f} km/h")
    if _last_weather.get("warning"):
        parts.append(f"⚡ {_last_weather['warning']}")
    return " · ".join(parts) if parts else "no data"
