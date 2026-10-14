"""Runs routines: fires time/sun/device triggers, executes actions, drives curves."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from .. import models as m
from ..models import Device
from .models import (
    WEEKDAYS, AutoRoutine, CommandAction, CurveRoutine, DelayAction, DeviceTrigger, EventRoutine, FadeAction,
    Routine, RunRoutineAction, SetAction, Target, TimeTrigger, WeatherTrigger,
)
from .auto import AutoRunner
from .timespec import Clock

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger(__name__)

AnyRoutine = EventRoutine | CurveRoutine | AutoRoutine
RoutineAdapter: TypeAdapter[AnyRoutine] = TypeAdapter(Routine)

TICK_SECONDS = 15
# A time trigger still fires if the hub notices it up to this late (e.g. after a hiccup),
# but a hub started hours later doesn't replay the whole day.
TRIGGER_GRACE = timedelta(minutes=3)
# Don't bother devices (and cloud APIs) with changes smaller than this.
MIN_BRIGHTNESS_STEP = 1
MIN_KELVIN_STEP = 50
# How often automatic routines re-evaluate (they also run on every weather update).
AUTO_EVERY = timedelta(minutes=5)


def curve_value(points: list[Any], clock: Clock, now: datetime) -> tuple[dict[str, int], date] | None:
    """Interpolate a curve at `now`. Returns (values, day the curve started) or None if outside it.

    Keyframes are taken in order; a keyframe earlier on the clock than the one before
    it is treated as the next day, so a curve can run 19:00 -> 23:00 -> 06:30.
    """
    for start_day in (now.date(), now.date() - timedelta(days=1)):
        times: list[datetime] = []
        for p in points:
            t = clock.resolve(p.at, start_day)
            while times and t < times[-1]:
                t += timedelta(days=1)
            times.append(t)
        if not times[0] <= now <= times[-1]:
            continue
        values: dict[str, int] = {}
        for attr in ("brightness", "color_temp"):
            keyed = [(t, getattr(p, attr)) for t, p in zip(times, points) if getattr(p, attr) is not None]
            if not keyed:
                continue
            before = [k for k in keyed if k[0] <= now]
            after = [k for k in keyed if k[0] >= now]
            if not before:
                values[attr] = after[0][1]
            elif not after:
                values[attr] = before[-1][1]
            else:
                (t0, v0), (t1, v1) = before[-1], after[0]
                span = (t1 - t0).total_seconds()
                frac = 0 if span == 0 else (now - t0).total_seconds() / span
                values[attr] = round(v0 + (v1 - v0) * frac)
        return values, start_day
    return None


def in_window(clock: Clock, between: tuple[str, str], now: datetime) -> bool:
    for day in (now.date(), now.date() - timedelta(days=1)):
        start = clock.resolve(between[0], day)
        end = clock.resolve(between[1], day)
        if end <= start:
            end += timedelta(days=1)
        if start <= now < end:
            return True
    return False


class RoutineEngine:
    def __init__(self, hub: Hub, path: Path):
        self.hub = hub
        self.path = path
        self.routines: dict[str, AnyRoutine] = {}
        self.last_run: dict[str, str] = {}
        self._fired: set[tuple[str, int, date]] = set()
        self._curve_last: dict[str, datetime] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._weather_prev: dict[tuple[str, int], bool | None] = {}
        self.auto = AutoRunner(self)

    @property
    def clock(self) -> Clock:
        return self.hub.clock

    # ---- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        for raw in json.loads(self.path.read_text()):
            try:
                r = RoutineAdapter.validate_python(raw)
                self.routines[r.id] = r
            except Exception as e:
                log.error("Skipping invalid routine %s: %s", raw.get("id"), e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([r.model_dump(mode="json") for r in self.routines.values()], indent=2))
        tmp.replace(self.path)

    def upsert(self, routine: AnyRoutine) -> None:
        self.routines[routine.id] = routine
        self._curve_last.pop(routine.id, None)
        self.auto.forget(routine.id)
        self.save()

    def delete(self, routine_id: str) -> None:
        self.routines.pop(routine_id, None)
        self.cancel(routine_id)
        self.save()

    # ---- scheduling --------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in [self._task, *self._running.values()]:
            if task:
                task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick(self.clock.now())
            except Exception:
                log.exception("routine tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime) -> None:
        for r in list(self.routines.values()):
            if not r.enabled:
                continue
            if isinstance(r, EventRoutine):
                for i, trig in enumerate(r.triggers):
                    if not isinstance(trig, TimeTrigger):
                        continue
                    fire_at = self.clock.resolve(trig.at, now.date())
                    key = (r.id, i, now.date())
                    if fire_at <= now < fire_at + TRIGGER_GRACE and key not in self._fired:
                        self._fired.add(key)
                        self.trigger(r, f"time {trig.at}")
            elif isinstance(r, AutoRoutine):
                if self.auto.due(r, now, AUTO_EVERY):
                    await self.auto.apply(r, now)
            else:
                last = self._curve_last.get(r.id)
                if last is None or now - last >= timedelta(minutes=r.step_minutes):
                    self._curve_last[r.id] = now
                    await self.apply_curve(r, now)
        # Forget fired markers from previous days.
        self._fired = {k for k in self._fired if k[2] >= now.date() - timedelta(days=1)}

    def on_device_update(self, device: Device, old: dict[str, Any]) -> None:
        for r in list(self.routines.values()):
            if not r.enabled:
                continue
            if isinstance(r, EventRoutine):
                for trig in r.triggers:
                    if (isinstance(trig, DeviceTrigger) and trig.device == device.id
                            and old.get(trig.attribute) != device.state.get(trig.attribute)
                            and (trig.to is None or device.state.get(trig.attribute) == trig.to)):
                        self.trigger(r, f"{device.name} {trig.attribute} -> {device.state.get(trig.attribute)}")
            elif (isinstance(r, CurveRoutine) and device.kind == "light" and not old.get("on") and device.state.get("on")
                  and not self.hub.routine_changed_within(device.id, 10)
                  and target_matches(r.target, device)):
                # Someone switched a light on: put it straight onto the curve instead of
                # full brightness. (Lights a routine turns on, e.g. a wake-up fade, are left alone.)
                self._spawn(self.apply_curve(r, self.clock.now(), only=device, ignore_override=True))

    # ---- running -----------------------------------------------------------

    def on_weather_update(self) -> None:
        """New weather arrived: fire weather triggers that just became true, re-run automatics."""
        metrics = self.hub.weather.metrics() if self.hub.weather else {}
        for r in list(self.routines.values()):
            if not r.enabled:
                continue
            if isinstance(r, EventRoutine):
                for i, trig in enumerate(r.triggers):
                    if isinstance(trig, WeatherTrigger):
                        now_holds = trig.holds(metrics)
                        before = self._weather_prev.get((r.id, i))
                        self._weather_prev[(r.id, i)] = now_holds
                        # Fire on a false -> true edge; the first reading after startup only sets a baseline.
                        if now_holds and before is False:
                            self.trigger(r, f"weather {trig.metric} {trig.op} {trig.value}")
            elif isinstance(r, AutoRoutine):
                self._spawn(self.auto.apply(r, self.clock.now()))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def conditions_met(self, r: EventRoutine, now: datetime) -> bool:
        if WEEKDAYS[now.weekday()] not in r.days:
            return False
        if r.between is not None and not in_window(self.clock, r.between, now):
            return False
        if r.weather:
            # A check we can't make (no weather data) counts as not met.
            metrics = self.hub.weather.metrics() if self.hub.weather else {}
            return all(check.holds(metrics) for check in r.weather)
        return True

    def trigger(self, r: EventRoutine, reason: str) -> None:
        if not self.conditions_met(r, self.clock.now()):
            log.info("routine %s triggered by %s but conditions not met", r.id, reason)
            return
        self.run(r, reason)

    def run(self, r: AnyRoutine, reason: str = "manual") -> None:
        """Start a routine now, ignoring its conditions. Re-running restarts it."""
        self.cancel(r.id)
        log.info("running routine %s (%s)", r.id, reason)
        self.last_run[r.id] = f"{self.clock.now().isoformat(timespec='seconds')} ({reason})"
        if isinstance(r, CurveRoutine):
            coro = self.apply_curve(r, self.clock.now(), ignore_override=True, force=True)
        elif isinstance(r, AutoRoutine):
            coro = self.auto.apply(r, self.clock.now(), force=True)
        else:
            coro = self._run_actions(r)
        task = asyncio.create_task(coro)
        self._running[r.id] = task
        task.add_done_callback(lambda t, rid=r.id: self._running.pop(rid, None) if self._running.get(rid) is t else None)

    def cancel(self, routine_id: str) -> None:
        task = self._running.pop(routine_id, None)
        if task:
            task.cancel()

    async def _run_actions(self, r: EventRoutine, depth: int = 0) -> None:
        for action in r.actions:
            try:
                await self._do(action, depth)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("routine %s: action %s failed", r.id, action.type)

    async def _do(self, action: Any, depth: int) -> None:
        if isinstance(action, SetAction):
            await self._set_many(self.hub.resolve_target(action.target), action.state)
        elif isinstance(action, CommandAction):
            devices = self.hub.resolve_target(action.target)
            await asyncio.gather(*(self.hub.command(d.id, action.command, source="routine") for d in devices),
                                 return_exceptions=True)
        elif isinstance(action, DelayAction):
            await asyncio.sleep(action.minutes * 60)
        elif isinstance(action, FadeAction):
            await self._fade(action)
        elif isinstance(action, RunRoutineAction):
            sub = self.routines.get(action.routine)
            if sub is None or depth > 5:
                log.warning("cannot run sub-routine %s", action.routine)
            elif isinstance(sub, EventRoutine):
                await self._run_actions(sub, depth + 1)
            elif isinstance(sub, AutoRoutine):
                await self.auto.apply(sub, self.clock.now(), force=True)
            else:
                await self.apply_curve(sub, self.clock.now(), ignore_override=True, force=True)

    async def _set_many(self, devices: list[Device], state: dict[str, Any]) -> None:
        jobs = []
        for d in devices:
            changes = d.accepts(state)
            if changes:
                jobs.append(self.hub.set_state(d.id, changes, source="routine"))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                log.warning("routine set_state failed: %s", res)

    async def _fade(self, action: FadeAction) -> None:
        lights = [d for d in self.hub.resolve_target(action.target)
                  if d.supports(m.BRIGHTNESS) or d.supports(m.COLOR_TEMP)]
        if not lights:
            return
        starts: dict[str, dict[str, int]] = {}
        for d in lights:
            s: dict[str, int] = {}
            if action.brightness is not None and d.supports(m.BRIGHTNESS):
                s["brightness"] = d.state.get("brightness", 0) if d.state.get("on") else 1
            if action.color_temp is not None and d.supports(m.COLOR_TEMP):
                s["color_temp"] = d.state.get("color_temp") or action.color_temp
            starts[d.id] = s
        # Step about every 20 s, so a 30-minute fade is ~90 small, unnoticeable changes.
        steps = max(1, min(int(action.minutes * 3), 360))
        interval = action.minutes * 60 / steps
        for i in range(1, steps + 1):
            frac = i / steps
            jobs = []
            for d in lights:
                changes: dict[str, Any] = {}
                for attr, start in starts[d.id].items():
                    end = getattr(action, attr)
                    changes[attr] = round(start + (end - start) * frac)
                if changes:
                    if action.brightness is not None and changes.get("brightness", 1) > 0:
                        changes["on"] = True
                    jobs.append(self.hub.set_state(d.id, changes, source="routine"))
            await asyncio.gather(*jobs, return_exceptions=True)
            if i < steps:
                await asyncio.sleep(interval)
        if action.off_at_end:
            await self._set_many(lights, {"on": False})

    async def apply_curve(self, r: CurveRoutine, now: datetime, only: Device | None = None,
                          ignore_override: bool = False, force: bool = False) -> None:
        result = curve_value(r.points, self.clock, now)
        if result is None:
            return
        values, start_day = result
        if WEEKDAYS[start_day.weekday()] not in r.days and not force:
            return
        devices = [only] if only else self.hub.resolve_target(r.target)
        jobs = []
        for d in devices:
            if d.kind != "light" or not d.available:
                continue
            if r.only_when_on and not d.state.get("on"):
                continue
            if not ignore_override and self.hub.manually_changed_within(d.id, r.manual_override_minutes):
                continue
            changes: dict[str, Any] = {}
            if "brightness" in values and d.supports(m.BRIGHTNESS):
                if abs((d.state.get("brightness") or 0) - values["brightness"]) >= MIN_BRIGHTNESS_STEP:
                    changes["brightness"] = values["brightness"]
            if "color_temp" in values and d.supports(m.COLOR_TEMP):
                if abs((d.state.get("color_temp") or 0) - values["color_temp"]) >= MIN_KELVIN_STEP:
                    changes["color_temp"] = values["color_temp"]
            if changes:
                jobs.append(self.hub.set_state(d.id, changes, source="routine"))
        await asyncio.gather(*jobs, return_exceptions=True)

    def preview(self, r: CurveRoutine, day: date | None = None, step_minutes: int = 10) -> list[dict[str, Any]]:
        """Sample a curve across a day (and into the next morning) for the UI chart."""
        day = day or self.clock.now().date()
        start = datetime.combine(day, datetime.min.time(), self.clock.tz) + timedelta(hours=12)
        out = []
        for i in range(0, 24 * 60 + 1, step_minutes):
            t = start + timedelta(minutes=i)
            res = curve_value(r.points, self.clock, t)
            out.append({"t": t.isoformat(timespec="minutes"), **(res[0] if res else {})})
        return out


def target_matches(target: Target, device: Device) -> bool:
    return (target.all or device.id in target.devices
            or (device.room is not None and device.room in target.rooms)
            or device.kind in target.kinds)
