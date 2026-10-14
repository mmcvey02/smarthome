"""Automatic routines: lighting and climate that respond to live weather and the sun."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .. import models as m
from .models import WEEKDAYS, AutoClimate, AutoLighting, AutoRoutine

if TYPE_CHECKING:
    from .engine import RoutineEngine

log = logging.getLogger(__name__)

GLOOMY_CONDITIONS = ("rain", "storm", "snow", "fog")
# Gloomy-day lights go off again only once cloud cover drops this far below the threshold,
# so a passing cloud bank doesn't flick them on and off.
GLOOMY_HYSTERESIS = 15


def dusk_time(cfg: AutoLighting, sunset: datetime, cloud_cover: float | None) -> datetime:
    """When it gets dark enough for lights: sunset + offset, earlier the cloudier it is."""
    early = (cloud_cover or 0) / 100 * cfg.cloudy_extra_minutes
    return sunset + timedelta(minutes=cfg.dusk_offset_minutes - early)


def is_gloomy(cfg: AutoLighting, weather: dict[str, Any], currently_on: bool) -> bool:
    if weather.get("condition") in GLOOMY_CONDITIONS:
        return True
    cloud = weather.get("cloud_cover")
    if cloud is None:
        return currently_on  # no data: keep doing what we were doing
    return cloud >= (cfg.gloomy_cloud_cover - GLOOMY_HYSTERESIS if currently_on else cfg.gloomy_cloud_cover)


def decide_climate(cfg: AutoClimate, state: dict[str, Any], weather: dict[str, Any],
                   hysteresis: float) -> tuple[dict[str, Any], str]:
    """Pick the mode and setpoint for one unit. Returns (changes, human-readable reason)."""
    indoor, outdoor, humidity = state.get("current_temp"), weather.get("temperature"), weather.get("humidity")
    on, mode = bool(state.get("on")), state.get("mode")
    low, high, h = cfg.comfort_low, cfg.comfort_high, hysteresis
    primary = indoor if indoor is not None else outdoor
    if primary is None:
        return {}, "no indoor or outdoor temperature yet"
    where = "inside" if indoor is not None else "outside"
    cooling, heating = on and mode == "cool", on and mode == "heat"

    want: dict[str, Any]
    if primary >= high + h or (cooling and (primary > high - h or (outdoor is not None and outdoor > high))):
        want, why = {"on": True, "mode": "cool", "target_temp": high}, f"{primary:g}° {where}, cooling to {high:g}°"
    elif primary <= low - h or (heating and (primary < low + h or (outdoor is not None and outdoor < low))):
        want, why = {"on": True, "mode": "heat", "target_temp": low}, f"{primary:g}° {where}, heating to {low:g}°"
    elif cfg.dehumidify_above is not None and humidity is not None and humidity > cfg.dehumidify_above:
        want, why = {"on": True, "mode": "dry"}, f"comfortable but {humidity:g}% humid outside, dehumidifying"
    elif cfg.mild_action == "off":
        want, why = {"on": False}, f"{primary:g}° {where} is comfortable, off"
    elif cfg.mild_action == "fan":
        want, why = {"on": True, "mode": "fan"}, f"{primary:g}° {where} is comfortable, fan only"
    else:
        return {}, f"{primary:g}° {where} is comfortable, leaving as is"

    if cfg.only_when_on:
        if not on:
            return {}, "off (only adjusting units that are running)"
        want.pop("on", None)
        if not want:
            return {}, why
    if want.get("on") is False and not on:
        return {}, why
    changes = {k: v for k, v in want.items() if state.get(k) != v}
    if changes.get("on") is False:
        changes = {"on": False}
    return changes, why


@dataclass
class _AutoState:
    dusk_done: date | None = None
    gloomy_lights: set[str] = field(default_factory=set)
    gloomy_active: bool = False
    climate_changed: dict[str, datetime] = field(default_factory=dict)
    last_applied: datetime | None = None


class AutoRunner:
    def __init__(self, engine: RoutineEngine):
        self.engine = engine
        self._state: dict[str, _AutoState] = {}

    @property
    def hub(self):  # noqa: ANN201
        return self.engine.hub

    def weather(self) -> dict[str, Any]:
        w = self.hub.weather
        return w.metrics() if w else {}

    def due(self, r: AutoRoutine, now: datetime, every: timedelta) -> bool:
        last = self._state.get(r.id, _AutoState()).last_applied
        return last is None or now - last >= every

    def forget(self, routine_id: str) -> None:
        self._state.pop(routine_id, None)

    async def apply(self, r: AutoRoutine, now: datetime, force: bool = False) -> None:
        st = self._state.setdefault(r.id, _AutoState())
        st.last_applied = now
        if WEEKDAYS[now.weekday()] not in r.days and not force:
            return
        weather = self.weather()
        if r.lighting:
            await self._lighting(r.lighting, st, weather, now, force)
        if r.climate:
            await self._climate(r.climate, st, weather, now, force)

    # ---- lighting ----------------------------------------------------------

    def _light_times(self, cfg: AutoLighting, weather: dict[str, Any], now: datetime) -> tuple[datetime, datetime, datetime]:
        clock = self.engine.clock
        dusk = dusk_time(cfg, clock.resolve("sunset", now.date()), weather.get("cloud_cover"))
        day_start = clock.resolve("sunrise+30", now.date())
        latest = clock.resolve(cfg.latest_on, now.date())
        return day_start, dusk, latest

    async def _lighting(self, cfg: AutoLighting, st: _AutoState, weather: dict[str, Any],
                        now: datetime, force: bool) -> None:
        day_start, dusk, latest = self._light_times(cfg, weather, now)
        lights = [d for d in self.hub.resolve_target(cfg.target) if d.supports(m.POWER) and d.available]
        on_state: dict[str, Any] = {"on": True}
        if cfg.brightness is not None:
            on_state["brightness"] = cfg.brightness

        if cfg.dusk_on and dusk <= now < latest and (st.dusk_done != now.date() or force):
            st.dusk_done = now.date()
            st.gloomy_lights.clear()  # evening now; gloomy-day lights simply stay on
            st.gloomy_active = False
            await self.engine._set_many([d for d in lights if not d.state.get("on")], on_state)
            log.info("auto lighting: dusk, lights on")
            return

        if not cfg.gloomy_day or not (day_start <= now < dusk):
            return
        if not weather:
            return
        gloomy = is_gloomy(cfg, weather, st.gloomy_active)
        if gloomy and not st.gloomy_active:
            st.gloomy_active = True
            turn_on = [d for d in lights if not d.state.get("on")]
            st.gloomy_lights = {d.id for d in turn_on}
            await self.engine._set_many(turn_on, on_state)
            log.info("auto lighting: gloomy (%s), %d lights on", weather.get("condition"), len(turn_on))
        elif not gloomy and st.gloomy_active:
            st.gloomy_active = False
            # Only switch off what we switched on, and not if someone has since adjusted it.
            turn_off = [d for d in lights if d.id in st.gloomy_lights and d.state.get("on")
                        and not self.hub.manually_changed_within(d.id, 60)]
            st.gloomy_lights.clear()
            await self.engine._set_many(turn_off, {"on": False})
            log.info("auto lighting: brightened up, %d lights off", len(turn_off))

    # ---- climate -----------------------------------------------------------

    def _hysteresis(self) -> float:
        return 1.0 if self.hub.temperature_unit == "F" else 0.5

    async def _climate(self, cfg: AutoClimate, st: _AutoState, weather: dict[str, Any],
                       now: datetime, force: bool) -> None:
        jobs = []
        for d in self.hub.resolve_target(cfg.target):
            if not d.supports(m.CLIMATE) or not d.available:
                continue
            if not force and self.hub.manually_changed_within(d.id, cfg.manual_override_minutes):
                continue
            changes, why = decide_climate(cfg, d.state, weather, self._hysteresis())
            if not changes:
                continue
            last = st.climate_changed.get(d.id)
            mode_change = "mode" in changes or "on" in changes
            if mode_change and not force and last and now - last < timedelta(minutes=cfg.min_change_minutes):
                continue
            if mode_change:
                st.climate_changed[d.id] = now
            log.info("auto climate %s: %s -> %s", d.name, why, changes)
            jobs.append(self.hub.set_state(d.id, changes, source="routine"))
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("auto climate failed: %s", res)

    # ---- explanation for the UI ----------------------------------------------

    def explain(self, r: AutoRoutine, now: datetime) -> list[str]:
        weather = self.weather()
        lines: list[str] = []
        if not weather:
            lines.append("No weather data yet; using the sun alone.")
        fmt = lambda t: t.strftime("%I:%M %p").lstrip("0")  # noqa: E731
        if r.lighting:
            cfg = r.lighting
            day_start, dusk, latest = self._light_times(cfg, weather, now)
            cloud = weather.get("cloud_cover")
            if cfg.dusk_on:
                extra = f", {round((cloud or 0) / 100 * cfg.cloudy_extra_minutes)} min early for {cloud:g}% cloud" \
                    if cloud else ""
                lines.append(f"Lights on at dusk: {fmt(dusk)} today (sunset {fmt(self.engine.clock.resolve('sunset', now.date()))}"
                             f"{extra}), not after {fmt(latest)}.")
            if cfg.gloomy_day:
                st = self._state.get(r.id)
                active = bool(st and st.gloomy_active)
                if weather:
                    gloomy = is_gloomy(cfg, weather, active)
                    sky = f"{weather.get('condition', '?').replace('_', ' ')}, {cloud if cloud is not None else '?'}% cloud"
                    if not day_start <= now < dusk:
                        verdict = f"applies between {fmt(day_start)} and {fmt(dusk)}"
                    else:
                        verdict = "on now, it's gloomy" if gloomy else "not needed right now"
                    lines.append(f"Gloomy-day lights: {verdict} ({sky}).")
        if r.climate:
            for d in self.hub.resolve_target(r.climate.target):
                if d.supports(m.CLIMATE):
                    changes, why = decide_climate(r.climate, d.state, weather, self._hysteresis())
                    lines.append(f"{d.name}: {why}" + ("" if changes else " (no change needed)") + ".")
        return lines
