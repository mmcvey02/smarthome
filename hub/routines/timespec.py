"""Clock times and sun-relative times, e.g. "21:30", "sunset", "sunset-30", "sunrise+45".

Sun-relative offsets are in minutes. Sun times need `location` in config.yaml;
without it they fall back to fixed times so routines still run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

log = logging.getLogger(__name__)

SUN_EVENTS = ("dawn", "sunrise", "noon", "sunset", "dusk")
FALLBACK = {"dawn": time(6, 0), "sunrise": time(6, 30), "noon": time(12, 0),
            "sunset": time(18, 30), "dusk": time(19, 0)}
_RE = re.compile(r"^\s*(?:(?P<h>\d{1,2}):(?P<m>\d{2})|(?P<sun>[a-z]+)\s*(?:(?P<sign>[+-])\s*(?P<off>\d+))?)\s*$")


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    timezone: str


def parse(spec: str) -> tuple[str, int, int]:
    """Return ("clock", hour, minute) or (sun_event, offset_minutes, 0). Raises ValueError."""
    match = _RE.match(str(spec).lower())
    if not match:
        raise ValueError(f"Can't read time {spec!r}; use HH:MM or e.g. sunset-30")
    if match["h"] is not None:
        h, mnt = int(match["h"]), int(match["m"])
        if h > 23 or mnt > 59:
            raise ValueError(f"Invalid clock time {spec!r}")
        return ("clock", h, mnt)
    if match["sun"] not in SUN_EVENTS:
        raise ValueError(f"Unknown sun event {match['sun']!r}; use one of {', '.join(SUN_EVENTS)}")
    offset = int(match["off"] or 0) * (-1 if match["sign"] == "-" else 1)
    return (match["sun"], offset, 0)


class Clock:
    """Resolves time specs to datetimes for a given day, in the home's timezone."""

    def __init__(self, tz: tzinfo, location: Location | None = None):
        self.tz = tz
        self.location = location
        self._sun_cache: dict[date, dict[str, datetime]] = {}

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def sun_times(self, day: date) -> dict[str, datetime]:
        if day not in self._sun_cache:
            times: dict[str, datetime] = {}
            if self.location:
                from astral import LocationInfo
                from astral.sun import sun
                info = LocationInfo(latitude=self.location.latitude, longitude=self.location.longitude,
                                    timezone=self.location.timezone)
                try:
                    times = sun(info.observer, date=day, tzinfo=self.tz)
                except ValueError as e:  # polar day/night
                    log.warning("No sun times for %s: %s", day, e)
            for event in SUN_EVENTS:
                times.setdefault(event, datetime.combine(day, FALLBACK[event], self.tz))
            self._sun_cache = {day: {k: times[k] for k in SUN_EVENTS}} | (
                self._sun_cache if len(self._sun_cache) < 8 else {})
        return self._sun_cache[day]

    def resolve(self, spec: str, day: date) -> datetime:
        kind, a, b = parse(spec)
        if kind == "clock":
            return datetime.combine(day, time(a, b), self.tz)
        return self.sun_times(day)[kind] + timedelta(minutes=a)
