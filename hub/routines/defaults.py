"""Starter routines written on first run. Edit or delete them from the Routines tab."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .engine import RoutineAdapter
from .models import CurveRoutine, EventRoutine

if TYPE_CHECKING:
    from ..hub import Hub


def default_routines(hub: Hub) -> list[EventRoutine | CurveRoutine]:
    bedtime_temp = 67 if hub.temperature_unit == "F" else 19.5
    raw = [
        {
            "kind": "curve", "id": "evening-wind-down", "name": "Evening wind-down",
            "description": "Lights dim and warm through the evening to support melatonin and sleep. "
                           "Lights switched on late stay low and amber instead of jumping to full.",
            "target": {"kinds": ["light"]},
            "points": [
                {"at": "sunset-60", "brightness": 100, "color_temp": 4500},
                {"at": "20:00", "brightness": 75, "color_temp": 3200},
                {"at": "21:30", "brightness": 40, "color_temp": 2500},
                {"at": "22:30", "brightness": 15, "color_temp": 2200},
                {"at": "05:30", "brightness": 10, "color_temp": 2000},
            ],
        },
        {
            "kind": "event", "id": "sunrise-wake-up", "name": "Sunrise wake-up",
            "description": "A 20-minute artificial sunrise in the bedroom on weekdays.",
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "triggers": [{"type": "time", "at": "06:30"}],
            "actions": [
                {"type": "set", "target": {"rooms": ["Bedroom"], "kinds": []},
                 "state": {"on": True, "brightness": 1, "color_temp": 2000}},
                {"type": "fade", "target": {"rooms": ["Bedroom"]}, "minutes": 20,
                 "brightness": 70, "color_temp": 4000},
            ],
        },
        {
            "kind": "event", "id": "bedtime", "name": "Bedtime",
            "description": "Fade every light out over 15 minutes and cool the house for sleep.",
            "triggers": [{"type": "time", "at": "23:00"}],
            "actions": [
                {"type": "set", "target": {"kinds": ["climate"]}, "state": {"target_temp": bedtime_temp}},
                {"type": "fade", "target": {"kinds": ["light"]}, "minutes": 15, "brightness": 0,
                 "off_at_end": True},
            ],
        },
        {
            "kind": "event", "id": "clean-while-out", "name": "Vacuum while out",
            "enabled": False,
            "days": ["mon", "wed", "fri"],
            "triggers": [{"type": "time", "at": "10:00"}],
            "actions": [{"type": "command", "target": {"kinds": ["vacuum"]}, "command": "start"}],
        },
        {
            "kind": "auto", "id": "automatic", "name": "Automatic lights & climate",
            "enabled": False,
            "description": "Lights come on when it actually gets dark (earlier on cloudy evenings, and on gloomy "
                           "days); mini-splits heat, cool, dry or switch off based on indoor temperature and "
                           "the outdoor weather. Turn on to let the house run itself.",
            "lighting": {"target": {"kinds": ["light"]}},
            "climate": {"target": {"kinds": ["climate"]},
                        "comfort_low": 68 if hub.temperature_unit == "F" else 20,
                        "comfort_high": 76 if hub.temperature_unit == "F" else 24.5},
        },
        {
            "kind": "event", "id": "heat-wave-precool", "name": "Heat wave pre-cool",
            "enabled": False,
            "description": "When it feels hotter than 90° outside, start cooling early so the house never gets hot.",
            "triggers": [{"type": "weather", "metric": "feels_like", "op": "above",
                          "value": 90 if hub.temperature_unit == "F" else 32}],
            "between": ["09:00", "21:00"],
            "actions": [{"type": "set", "target": {"kinds": ["climate"]},
                         "state": {"on": True, "mode": "cool",
                                   "target_temp": 74 if hub.temperature_unit == "F" else 23.5}}],
        },
        {
            "kind": "event", "id": "movie-night", "name": "Movie night",
            "description": "Scene: press Run to dim the living room.",
            "triggers": [],
            "actions": [
                {"type": "set", "target": {"rooms": ["Living Room"]},
                 "state": {"on": True, "brightness": 15, "color_temp": 2200}},
            ],
        },
    ]
    return [RoutineAdapter.validate_python(r) for r in raw]
