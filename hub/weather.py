"""Public weather for the home's location, from Open-Meteo (free, no API key).

Also finds the home's location from its public IP address when config.yaml
doesn't give one, so sunrise/sunset and weather work with zero setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOIP_URLS = ("https://ipwho.is/", "https://ipapi.co/json/")

CURRENT_FIELDS = ("temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,"
                  "weather_code,cloud_cover,wind_speed_10m,uv_index")
HOURLY_FIELDS = "temperature_2m,precipitation_probability,weather_code"
DAILY_FIELDS = ("weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
                "precipitation_probability_max,uv_index_max")

# WMO weather codes -> (description, category). Categories are what routines match on.
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear", "clear"), 1: ("Mostly clear", "clear"), 2: ("Partly cloudy", "partly_cloudy"),
    3: ("Overcast", "cloudy"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"), 81: ("Rain showers", "rain"), 82: ("Violent rain showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm with hail", "storm"), 99: ("Thunderstorm with hail", "storm"),
}
CATEGORIES = ("clear", "partly_cloudy", "cloudy", "fog", "rain", "snow", "storm")

# Values routines can test. Keys of `Weather.metrics()`.
METRICS = {
    "temperature": "Outdoor temperature", "feels_like": "Feels-like temperature",
    "humidity": "Humidity %", "cloud_cover": "Cloud cover %", "precipitation": "Precipitation now",
    "wind_speed": "Wind speed", "uv_index": "UV index", "rain_chance": "Today's rain chance %",
    "today_high": "Today's high", "today_low": "Today's low", "condition": "Condition",
}


def describe(code: int | None) -> tuple[str, str]:
    return WMO_CODES.get(code if code is not None else -1, ("Unknown", "cloudy"))


def parse_forecast(data: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    """Turn an Open-Meteo response into the hub's weather snapshot."""
    cur = data.get("current", {})
    daily = data.get("daily", {})
    hourly = data.get("hourly", {})

    def local(ts: str | None) -> str | None:
        return datetime.fromisoformat(ts).replace(tzinfo=tz).isoformat(timespec="minutes") if ts else None

    desc, category = describe(cur.get("weather_code"))
    days = []
    for i, day in enumerate(daily.get("time", [])):
        d_desc, d_cat = describe(daily["weather_code"][i])
        days.append({
            "date": day, "condition": d_cat, "description": d_desc,
            "high": daily["temperature_2m_max"][i], "low": daily["temperature_2m_min"][i],
            "rain_chance": daily.get("precipitation_probability_max", [None] * (i + 1))[i],
            "uv_index_max": daily.get("uv_index_max", [None] * (i + 1))[i],
            "sunrise": local(daily["sunrise"][i]), "sunset": local(daily["sunset"][i]),
        })
    hours = [
        {"time": local(t), "temperature": hourly["temperature_2m"][i],
         "rain_chance": hourly.get("precipitation_probability", [None] * (i + 1))[i],
         "condition": describe(hourly.get("weather_code", [None] * (i + 1))[i])[1]}
        for i, t in enumerate(hourly.get("time", []))
    ]
    return {
        "updated": local(cur.get("time")),
        "current": {
            "temperature": cur.get("temperature_2m"), "feels_like": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"), "cloud_cover": cur.get("cloud_cover"),
            "precipitation": cur.get("precipitation"), "wind_speed": cur.get("wind_speed_10m"),
            "uv_index": cur.get("uv_index"), "is_day": bool(cur.get("is_day", 1)),
            "condition": category, "description": desc, "code": cur.get("weather_code"),
        },
        "today": days[0] if days else None,
        "days": days,
        "hours": hours,
    }


class WeatherService:
    def __init__(self, latitude: float, longitude: float, tz: ZoneInfo, unit: str,
                 refresh_minutes: float = 15, cache_path: Path | None = None):
        self.latitude = latitude
        self.longitude = longitude
        self.tz = tz
        self.unit = unit
        self.refresh_minutes = refresh_minutes
        self.cache_path = cache_path
        self.data: dict[str, Any] | None = None
        self.error: str | None = None
        self.fetched_at: float | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._task: asyncio.Task[None] | None = None
        if cache_path and cache_path.exists():
            try:  # show the last known weather straight away after a restart
                self.data = json.loads(cache_path.read_text())
            except ValueError:
                pass

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def metrics(self) -> dict[str, Any]:
        """Flat values for routine triggers/conditions. Empty until the first fetch."""
        if not self.data:
            return {}
        cur, today = self.data["current"], self.data.get("today") or {}
        return {
            **{k: cur.get(k) for k in ("temperature", "feels_like", "humidity", "cloud_cover",
                                       "precipitation", "wind_speed", "uv_index", "condition")},
            "rain_chance": today.get("rain_chance"),
            "today_high": today.get("high"),
            "today_low": today.get("low"),
        }

    @property
    def stale(self) -> bool:
        """True when we haven't had fresh data for a while (e.g. internet is down)."""
        return self.fetched_at is None or time.monotonic() - self.fetched_at > self.refresh_minutes * 60 * 4

    def params(self) -> dict[str, Any]:
        imperial = self.unit == "F"
        return {
            "latitude": self.latitude, "longitude": self.longitude, "timezone": self.tz.key,
            "current": CURRENT_FIELDS, "hourly": HOURLY_FIELDS, "daily": DAILY_FIELDS,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
            "precipitation_unit": "inch" if imperial else "mm",
            "forecast_days": 5, "forecast_hours": 24,
        }

    async def fetch(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(FORECAST_URL, params=self.params())
            res.raise_for_status()
            return res.json()

    async def refresh(self) -> None:
        try:
            self.update(parse_forecast(await self.fetch(), self.tz))
        except Exception as e:
            self.error = f"Couldn't fetch weather: {e}"
            log.warning(self.error)

    def update(self, data: dict[str, Any]) -> None:
        self.data = data
        self.error = None
        self.fetched_at = time.monotonic()
        if self.cache_path:
            self.cache_path.write_text(json.dumps(data))
        for listener in list(self._listeners):
            try:
                listener(data)
            except Exception:
                log.exception("weather listener failed")

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_minutes * 60 if self.error is None else 120)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


async def detect_location() -> dict[str, Any]:
    """Approximate location (city level) from this network's public IP address."""
    errors = []
    async with httpx.AsyncClient(timeout=10) as client:
        for url in GEOIP_URLS:
            try:
                res = await client.get(url)
                res.raise_for_status()
                d = res.json()
                tz = d.get("timezone")
                tz = tz.get("id") if isinstance(tz, dict) else tz
                if d.get("latitude") is None or not tz:
                    raise ValueError("no coordinates in response")
                return {"latitude": float(d["latitude"]), "longitude": float(d["longitude"]), "timezone": tz,
                        "city": d.get("city"), "region": d.get("region"),
                        "country": d.get("country") or d.get("country_name"), "source": "ip"}
            except Exception as e:
                errors.append(f"{url}: {e}")
    raise RuntimeError("; ".join(errors))
