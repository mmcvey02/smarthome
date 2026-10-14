"""Simulated devices, so the dashboard and routines can be tried with no hardware."""

from __future__ import annotations

import asyncio
from typing import Any

from .. import models as m
from ..models import Device
from .base import Integration, IntegrationError, register

DEFAULT_DEVICES = [
    {"id": "living_lamp", "name": "Living Room Lamp", "kind": "light", "room": "Living Room", "color": True},
    {"id": "living_ceiling", "name": "Living Room Ceiling", "kind": "light", "room": "Living Room"},
    {"id": "bedroom_lamp", "name": "Bedside Lamp", "kind": "light", "room": "Bedroom", "color": True},
    {"id": "kitchen", "name": "Kitchen Lights", "kind": "light", "room": "Kitchen"},
    {"id": "coffee", "name": "Coffee Maker", "kind": "plug", "room": "Kitchen"},
    {"id": "bedroom_ac", "name": "Bedroom Mini-Split", "kind": "climate", "room": "Bedroom"},
    {"id": "living_ac", "name": "Living Room Mini-Split", "kind": "climate", "room": "Living Room"},
    {"id": "roomba", "name": "Roomba", "kind": "vacuum", "room": "Living Room"},
]


@register
class DemoIntegration(Integration):
    type = "demo"
    poll_interval = 5

    async def start(self) -> None:
        unit = self.hub.temperature_unit
        for spec in self.config.get("devices", DEFAULT_DEVICES):
            kind = spec["kind"]
            caps: set[str] = set()
            state: dict[str, Any] = {}
            attrs: dict[str, Any] = {"model": "Simulated"}
            if kind == "light":
                caps = {m.POWER, m.BRIGHTNESS, m.COLOR_TEMP}
                state = {"on": False, "brightness": 80, "color_temp": 4000}
                if spec.get("color"):
                    caps.add(m.COLOR)
                    state["rgb"] = [255, 180, 120]
            elif kind == "plug":
                caps = {m.POWER}
                state = {"on": False}
            elif kind == "climate":
                caps = {m.POWER, m.CLIMATE}
                cur = 72.0 if unit == "F" else 22.0
                state = {"on": False, "mode": "cool", "target_temp": cur, "current_temp": cur, "fan_speed": "auto"}
                attrs.update(min_temp=60 if unit == "F" else 16, max_temp=86 if unit == "F" else 30,
                             modes=list(m.CLIMATE_MODES), fan_speeds=list(m.FAN_SPEEDS))
            elif kind == "vacuum":
                caps = {m.VACUUM}
                state = {"status": "docked", "battery": 100, "bin_full": False}
                attrs["commands"] = list(m.VACUUM_COMMANDS)
            self.add_device(Device(
                id=self.device_id(spec["id"]), name=spec["name"], kind=kind, integration=self.id,
                capabilities=caps, state=state, room=spec.get("room"), attributes=attrs,
            ))

    async def set_state(self, device: Device, changes: dict[str, Any]) -> None:
        await asyncio.sleep(0)  # pretend there's I/O
        device.state.update(changes)
        self.push_update(device)

    async def command(self, device: Device, command: str, params: dict[str, Any] | None = None) -> None:
        if device.kind != "vacuum" or command not in m.VACUUM_COMMANDS:
            raise IntegrationError(f"{device.name} does not support {command!r}")
        device.state["status"] = {
            "start": "cleaning", "resume": "cleaning", "pause": "paused",
            "stop": "idle", "dock": "returning",
        }[command]
        self.push_update(device)

    async def poll(self) -> None:
        # Drift the simulation a little so the dashboard looks alive.
        for d in self.devices.values():
            changed = False
            if d.kind == "climate" and d.state["on"] and d.state["mode"] in ("cool", "heat"):
                diff = d.state["target_temp"] - d.state["current_temp"]
                if abs(diff) >= 0.25:
                    d.state["current_temp"] = round(d.state["current_temp"] + (0.5 if diff > 0 else -0.5), 1)
                    changed = True
            elif d.kind == "vacuum":
                s = d.state
                if s["status"] == "cleaning":
                    s["battery"] = max(0, s["battery"] - 2)
                    if s["battery"] <= 15:
                        s["status"] = "returning"
                    changed = True
                elif s["status"] == "returning":
                    s["status"] = "charging"
                    changed = True
                elif s["status"] in ("charging", "docked") and s["battery"] < 100:
                    s["battery"] = min(100, s["battery"] + 5)
                    s["status"] = "charging" if s["battery"] < 100 else "docked"
                    changed = True
            if changed:
                self.push_update(d)
