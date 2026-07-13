import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_REQUEST_TIMEOUT = 5.0

# WMO weather codes -> short human text.
# https://open-meteo.com/en/docs (weather_code)
_WMO_CONDITIONS: dict[int, str] = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}

# (start_hour, end_hour) inclusive, used to bucket the 48h hourly series into
# the parts of the day a runner actually cares about.
_WINDOWS = {
    "morning": (6, 9),
    "midday": (11, 14),
    "evening": (17, 20),
}

_CACHE_TTL_SECONDS = 30 * 60
_forecast_cache: dict[tuple[float, float], tuple[float, dict]] = {}


def _condition(code: int | None) -> str:
    if code is None:
        return "unknown"
    return _WMO_CONDITIONS.get(code, "unknown")


async def geocode(query: str) -> dict[str, Any] | None:
    """Resolve a free-text place name to its top match. Returns
    {name, country, latitude, longitude, timezone} or None if not found or
    the API is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(_GEOCODE_URL, params={"name": query, "count": 1})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Geocoding request failed for query=%r", query)
        return None

    results = data.get("results") or []
    if not results:
        return None

    top = results[0]
    return {
        "name": top["name"],
        "country": top.get("country"),
        "latitude": top["latitude"],
        "longitude": top["longitude"],
        "timezone": top.get("timezone"),
    }


def _bucket_hourly(hourly: dict[str, list]) -> dict[str, dict[str, dict]]:
    """Group the hourly series by local date, then by time-of-day window,
    averaging temp/humidity and taking the max precipitation probability
    per window."""
    times = hourly.get("time") or []
    buckets: dict[str, dict[str, list[dict]]] = {}

    for i, ts in enumerate(times):
        day, hour = ts[:10], int(ts[11:13])
        window = next((name for name, (start, end) in _WINDOWS.items() if start <= hour <= end), None)
        if window is None:
            continue

        entry = {
            "temp": hourly["temperature_2m"][i],
            "feels_like": hourly["apparent_temperature"][i],
            "humidity": hourly["relative_humidity_2m"][i],
            "precipitation_probability": hourly["precipitation_probability"][i],
        }
        buckets.setdefault(day, {}).setdefault(window, []).append(entry)

    result: dict[str, dict[str, dict]] = {}
    for day, windows in buckets.items():
        result[day] = {}
        for window, entries in windows.items():
            n = len(entries)
            result[day][window] = {
                "temp": round(sum(e["temp"] for e in entries) / n, 1),
                "feels_like": round(sum(e["feels_like"] for e in entries) / n, 1),
                "humidity": round(sum(e["humidity"] for e in entries) / n),
                "precipitation_probability": max(e["precipitation_probability"] for e in entries),
            }
    return result


def _build_daily(daily: dict[str, list]) -> list[dict]:
    days = daily.get("time") or []
    return [
        {
            "date": days[i],
            "temp_min": daily["temperature_2m_min"][i],
            "temp_max": daily["temperature_2m_max"][i],
            "feels_like_min": daily["apparent_temperature_min"][i],
            "feels_like_max": daily["apparent_temperature_max"][i],
            "precipitation_probability": daily["precipitation_probability_max"][i],
            "wind_speed_max": daily["windspeed_10m_max"][i],
            "condition": _condition(daily["weather_code"][i]),
        }
        for i in range(len(days))
    ]


async def get_forecast(lat: float, lon: float, tz: str, days: int = 3) -> dict[str, Any] | None:
    """Returns {"daily": [...], "windows": {date: {morning/midday/evening: {...}}}}
    or None on failure (network error, timeout, bad response) — callers must
    treat this as "weather unavailable right now", not crash."""
    cache_key = (round(lat, 2), round(lon, 2))
    cached = _forecast_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz or "UTC",
        "forecast_days": days,
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "apparent_temperature_max",
                "apparent_temperature_min",
                "precipitation_probability_max",
                "windspeed_10m_max",
                "weather_code",
            ]
        ),
        "hourly": ",".join(
            ["temperature_2m", "apparent_temperature", "relative_humidity_2m", "precipitation_probability"]
        ),
    }

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(_FORECAST_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("Forecast request failed for lat=%s lon=%s", lat, lon)
        return None

    result = {
        "daily": _build_daily(data.get("daily") or {}),
        "windows": _bucket_hourly(data.get("hourly") or {}),
    }
    _forecast_cache[cache_key] = (time.monotonic(), result)
    return result
