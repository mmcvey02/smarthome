"""The hub: owns integrations, the combined device list, and the routine engine."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from . import drivers  # noqa: F401  (registers built-in integrations)
from . import models as m
from .drivers.base import REGISTRY, Integration, IntegrationError
from .models import Device
from .routines.engine import RoutineEngine, target_matches
from .routines.models import Target
from .routines.timespec import Clock, Location
from .weather import WeatherService, detect_location

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, config: dict[str, Any], data_dir: Path):
        self.config = config
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.temperature_unit: str = str(config.get("temperature_unit", "F")).upper()

        # `location:` may be coordinates, "auto" (or missing) to look it up from the
        # public IP address at startup, or "off" to use neither location nor weather.
        loc = config.get("location", "auto")
        self.location_info: dict[str, Any] = {}
        self.weather: WeatherService | None = None
        self.clock = Clock(ZoneInfo(config.get("timezone") or "UTC"))
        if isinstance(loc, dict) and "latitude" in loc and "longitude" in loc:
            self._set_location({**loc, "source": "config"})
        elif loc == "auto" or loc is None:
            cached = data_dir / "location.json"
            if cached.exists():
                self._set_location(json.loads(cached.read_text()))

        self.integrations: dict[str, Integration] = {}
        self.devices: dict[str, Device] = {}
        self._overrides_path = data_dir / "devices.json"
        self.overrides: dict[str, dict[str, Any]] = (
            json.loads(self._overrides_path.read_text()) if self._overrides_path.exists() else {})
        self._last_state: dict[str, dict[str, Any]] = {}
        self._manual_at: dict[str, float] = {}
        self._routine_at: dict[str, float] = {}
        self._listeners: list[Callable[[Device], None]] = []
        self._poll_tasks: list[asyncio.Task[None]] = []
        self.routines = RoutineEngine(self, data_dir / "routines.json")

    def _set_location(self, loc: dict[str, Any]) -> None:
        tz = ZoneInfo(loc.get("timezone") or self.config.get("timezone") or "UTC")
        self.location_info = {k: loc.get(k) for k in ("latitude", "longitude", "city", "region", "country", "source")}
        self.location_info["timezone"] = tz.key
        self.clock = Clock(tz, Location(float(loc["latitude"]), float(loc["longitude"]), tz.key))
        wcfg = self.config.get("weather") or {}
        if wcfg.get("enabled", True):
            self.weather = WeatherService(float(loc["latitude"]), float(loc["longitude"]), tz,
                                          self.temperature_unit, float(wcfg.get("refresh_minutes", 15)),
                                          cache_path=self.data_dir / "weather.json")
            self.weather.subscribe(lambda _data: self.routines.on_weather_update())

    async def _detect_location(self) -> None:
        try:
            loc = await detect_location()
        except Exception as e:
            log.warning("Couldn't detect location from IP (%s). Set `location:` in config.yaml "
                        "for sunrise/sunset and weather.", e)
            return
        (self.data_dir / "location.json").write_text(json.dumps(loc))
        self._set_location(loc)
        log.info("Location detected as %s, %s (%s)", loc.get("city"), loc.get("region"), loc["timezone"])

    @classmethod
    def from_file(cls, path: Path, data_dir: Path) -> Hub:
        config = yaml.safe_load(path.read_text()) if path.exists() else {}
        if not config:
            log.warning("No config at %s; starting with demo devices only", path)
            config = {"integrations": [{"type": "demo", "id": "demo"}]}
        return cls(config, data_dir)

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Third-party drivers: any importable module that uses @register.
        for module in self.config.get("plugins", []):
            try:
                importlib.import_module(module)
            except Exception:
                log.exception("Could not load plugin %s", module)

        for spec in self.config.get("integrations", []):
            spec = dict(spec)
            type_ = spec.pop("type", None)
            iid = spec.pop("id", type_)
            if spec.pop("enabled", True) is False:
                continue
            cls = REGISTRY.get(type_)
            if cls is None:
                log.error("Unknown integration type %r (known: %s)", type_, ", ".join(sorted(REGISTRY)))
                continue
            self.integrations[iid] = cls(iid, spec, self)

        if self.clock.location is None and self.config.get("location", "auto") in ("auto", None):
            await self._detect_location()
        if self.weather:
            self.weather.start()

        await asyncio.gather(*(self._start_integration(i) for i in self.integrations.values()))
        self.routines.load()
        if not self.routines.path.exists():
            from .routines.defaults import default_routines
            for r in default_routines(self):
                self.routines.routines[r.id] = r
            self.routines.save()
        self.routines.start()

    async def _start_integration(self, integ: Integration) -> None:
        try:
            await integ.start()
        except Exception as e:
            integ.error = str(e) or type(e).__name__
            log.error("Integration %s failed to start: %s", integ.id, integ.error)
            return
        for device in integ.devices.values():
            self._apply_overrides(device)
            self.devices[device.id] = device
            self._last_state[device.id] = copy.deepcopy(device.state)
        if integ.poll_interval:
            self._poll_tasks.append(asyncio.create_task(self._poll_loop(integ)))
        log.info("Integration %s: %d devices", integ.id, len(integ.devices))

    async def _poll_loop(self, integ: Integration) -> None:
        while True:
            await asyncio.sleep(integ.poll_interval)
            try:
                await integ.poll()
                integ.error = None
            except Exception as e:
                integ.error = f"poll failed: {e}"
                log.warning("Integration %s %s", integ.id, integ.error)

    async def stop(self) -> None:
        await self.routines.stop()
        if self.weather:
            await self.weather.stop()
        for t in self._poll_tasks:
            t.cancel()
        for integ in self.integrations.values():
            try:
                await integ.stop()
            except Exception:
                log.exception("stopping %s", integ.id)

    # ---- devices -----------------------------------------------------------

    def _apply_overrides(self, device: Device) -> None:
        o = self.overrides.get(device.id, {})
        if o.get("name"):
            device.name = o["name"]
        if "room" in o:
            device.room = o["room"] or None
        device.attributes["hidden"] = bool(o.get("hidden"))

    def update_device_info(self, device_id: str, **fields: Any) -> Device:
        device = self.get(device_id)
        o = self.overrides.setdefault(device_id, {})
        for k in ("name", "room", "hidden"):
            if k in fields and fields[k] is not None:
                o[k] = fields[k]
        self._overrides_path.write_text(json.dumps(self.overrides, indent=2))
        self._apply_overrides(device)
        self._notify(device)
        return device

    def get(self, device_id: str) -> Device:
        try:
            return self.devices[device_id]
        except KeyError:
            raise KeyError(f"No device {device_id!r}") from None

    def rooms(self) -> list[str]:
        return sorted({d.room for d in self.devices.values() if d.room})

    def resolve_target(self, target: Target) -> list[Device]:
        return [d for d in self.devices.values() if target_matches(target, d)]

    def manually_changed_within(self, device_id: str, minutes: float) -> bool:
        at = self._manual_at.get(device_id)
        return at is not None and time.monotonic() - at < minutes * 60

    def routine_changed_within(self, device_id: str, seconds: float) -> bool:
        at = self._routine_at.get(device_id)
        return at is not None and time.monotonic() - at < seconds

    async def set_state(self, device_id: str, changes: dict[str, Any], source: str = "user") -> Device:
        device = self.get(device_id)
        integ = self.integrations[device.integration]
        changes = self._validate(device, changes)
        if not changes:
            return device
        if source != "user":
            self._routine_at[device_id] = time.monotonic()
        elif set(changes) - {"on"}:
            # A hand-picked brightness/colour pauses curves for this light; a plain on/off doesn't.
            self._manual_at[device_id] = time.monotonic()
        await integ.set_state(device, changes)
        return device

    async def command(self, device_id: str, command: str, params: dict[str, Any] | None = None,
                      source: str = "user") -> Device:
        device = self.get(device_id)
        await self.integrations[device.integration].command(device, command, params)
        return device

    def _validate(self, device: Device, changes: dict[str, Any]) -> dict[str, Any]:
        accepted = device.accepts(changes)
        unknown = set(changes) - set(accepted)
        if unknown:
            raise IntegrationError(f"{device.name} can't set {', '.join(sorted(unknown))}")
        if "brightness" in accepted:
            accepted["brightness"] = int(m.clamp(int(accepted["brightness"]), 0, 100))
        if "color_temp" in accepted:
            accepted["color_temp"] = int(m.clamp(int(accepted["color_temp"]), 1500, 9000))
        if "mode" in accepted and accepted["mode"] not in m.CLIMATE_MODES:
            raise IntegrationError(f"Unknown mode {accepted['mode']!r}")
        if "fan_speed" in accepted and accepted["fan_speed"] not in m.FAN_SPEEDS:
            raise IntegrationError(f"Unknown fan speed {accepted['fan_speed']!r}")
        if "target_temp" in accepted:
            lo = device.attributes.get("min_temp", -100)
            hi = device.attributes.get("max_temp", 200)
            accepted["target_temp"] = m.clamp(float(accepted["target_temp"]), lo, hi)
        if "on" in accepted:
            accepted["on"] = bool(accepted["on"])
        return accepted

    # ---- change notification -----------------------------------------------

    def subscribe(self, listener: Callable[[Device], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def device_updated(self, device: Device) -> None:
        """Called by integrations whenever a device's state may have changed."""
        old = self._last_state.get(device.id, {})
        if old == device.state:
            return
        self._last_state[device.id] = copy.deepcopy(device.state)
        self.routines.on_device_update(device, old)
        self._notify(device)

    def _notify(self, device: Device) -> None:
        for listener in list(self._listeners):
            try:
                listener(device)
            except Exception:
                log.exception("listener failed")
