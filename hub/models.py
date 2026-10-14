"""The vendor-neutral device model every driver translates into.

Each driver maps its own API onto these kinds, capabilities and state keys, so
the dashboard and the routine engine never need to know which brand a device is.

State keys by capability:

    power        on: bool
    brightness   brightness: int 0-100
    color_temp   color_temp: int Kelvin (warm ~2000 .. cool ~6500)
    color        rgb: [r, g, b] 0-255
    climate      mode: one of CLIMATE_MODES, target_temp: float (hub unit),
                 current_temp: float, fan_speed: one of FAN_SPEEDS
    vacuum       status: one of VACUUM_STATUSES, battery: int 0-100, bin_full: bool
                 commands: start, stop, pause, resume, dock
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KINDS = ("light", "plug", "climate", "vacuum", "sensor", "other")

POWER = "power"
BRIGHTNESS = "brightness"
COLOR_TEMP = "color_temp"
COLOR = "color"
CLIMATE = "climate"
VACUUM = "vacuum"

CLIMATE_MODES = ("auto", "cool", "heat", "dry", "fan")
FAN_SPEEDS = ("auto", "silent", "low", "medium", "high", "max")
VACUUM_STATUSES = ("docked", "charging", "cleaning", "paused", "returning", "stuck", "idle", "error")
VACUUM_COMMANDS = ("start", "stop", "pause", "resume", "dock")

# Which state keys each capability owns; used to filter routine actions so a
# "set brightness" aimed at a whole room skips the plugs and the thermostat.
CAPABILITY_KEYS = {
    POWER: {"on"},
    BRIGHTNESS: {"brightness"},
    COLOR_TEMP: {"color_temp"},
    COLOR: {"rgb"},
    CLIMATE: {"mode", "target_temp", "fan_speed"},
}


@dataclass
class Device:
    id: str
    name: str
    kind: str
    integration: str
    capabilities: set[str] = field(default_factory=set)
    state: dict[str, Any] = field(default_factory=dict)
    room: str | None = None
    available: bool = True
    # Driver-specific extras shown in the UI (min/max temps, model name, ...).
    attributes: dict[str, Any] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def accepts(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Return only the changes this device has a capability for."""
        allowed: set[str] = set()
        for cap in self.capabilities:
            allowed |= CAPABILITY_KEYS.get(cap, set())
        return {k: v for k, v in changes.items() if k in allowed}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "integration": self.integration,
            "capabilities": sorted(self.capabilities),
            "state": self.state,
            "room": self.room,
            "available": self.available,
            "attributes": self.attributes,
        }


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9
