from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from hub.routines.engine import RoutineAdapter, curve_value, in_window
from hub.routines.models import CurvePoint
from hub.routines.timespec import Clock, Location, parse

from .conftest import TZ, at

CLOCK = Clock(TZ, Location(40.71, -74.0, "America/New_York"))


def test_parse_time_specs():
    assert parse("07:30") == ("clock", 7, 30)
    assert parse("sunset") == ("sunset", 0, 0)
    assert parse("sunset-45") == ("sunset", -45, 0)
    assert parse("Sunrise + 20") == ("sunrise", 20, 0)
    for bad in ("25:00", "lunchtime", "7pm", "sunset*2"):
        with pytest.raises(ValueError):
            parse(bad)


def test_sun_times_follow_location_and_fall_back_without_one():
    day = at(12).date()
    sunset = CLOCK.resolve("sunset", day)
    assert 18 <= sunset.hour <= 19  # mid-October, New York
    assert (sunset - CLOCK.resolve("sunset-30", day)).total_seconds() == 1800
    no_loc = Clock(TZ)
    assert no_loc.resolve("sunset", day).strftime("%H:%M") == "18:30"


EVENING = [
    CurvePoint(at="19:00", brightness=100, color_temp=4000),
    CurvePoint(at="22:00", brightness=40, color_temp=2200),
    CurvePoint(at="06:00", brightness=10),
]


def test_curve_interpolates_between_keyframes():
    assert curve_value(EVENING, CLOCK, at(19))[0] == {"brightness": 100, "color_temp": 4000}
    assert curve_value(EVENING, CLOCK, at(20, 30))[0] == {"brightness": 70, "color_temp": 3100}
    assert curve_value(EVENING, CLOCK, at(22))[0] == {"brightness": 40, "color_temp": 2200}


def test_curve_wraps_past_midnight():
    values, start_day = curve_value(EVENING, CLOCK, at(2))
    assert start_day == at(2).date().replace(day=13)  # the curve that began the previous evening
    assert values["brightness"] == 25  # halfway from 22:00 (40) to 06:00 (10)
    assert values["color_temp"] == 2200  # last keyframe with a colour temp holds


def test_curve_inactive_outside_its_window():
    assert curve_value(EVENING, CLOCK, at(12)) is None
    assert curve_value(EVENING, CLOCK, at(6, 1)) is None


def test_between_window_wraps():
    assert in_window(CLOCK, ("22:00", "02:00"), at(23))
    assert in_window(CLOCK, ("22:00", "02:00"), at(1))
    assert not in_window(CLOCK, ("22:00", "02:00"), at(12))


def test_routine_validation():
    with pytest.raises(ValidationError):
        RoutineAdapter.validate_python({"kind": "curve", "id": "x", "name": "x", "target": {"all": True},
                                        "points": [{"at": "20:00", "brightness": 50}]})  # one keyframe
    with pytest.raises(ValidationError):
        RoutineAdapter.validate_python({"kind": "event", "id": "x", "name": "x", "actions": [
            {"type": "set", "target": {}, "state": {"on": True}}]})  # empty target
    with pytest.raises(ValidationError):
        RoutineAdapter.validate_python({"kind": "event", "id": "x", "name": "x",
                                        "triggers": [{"type": "time", "at": "noonish"}],
                                        "actions": [{"type": "delay", "minutes": 1}]})


def _event(**kw):
    base = {"kind": "event", "id": "ev", "name": "ev", "triggers": [{"type": "time", "at": "21:00"}],
            "actions": [{"type": "set", "target": {"kinds": ["light"]}, "state": {"on": True, "brightness": 30}}]}
    return RoutineAdapter.validate_python(base | kw)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_time_trigger_fires_once_within_grace(make_hub):
    async def go():
        hub = await make_hub()
        hub.clock.now = lambda: at(21, 1)
        hub.routines.routines["ev"] = _event()
        await hub.routines.tick(at(20, 59))
        await _settle()
        assert not hub.get("demo:kitchen").state["on"]
        await hub.routines.tick(at(21, 1))
        await _settle()
        assert hub.get("demo:kitchen").state == {"on": True, "brightness": 30, "color_temp": 4000}
        # Plugs and thermostats aren't lights, so they weren't touched.
        assert not hub.get("demo:coffee").state["on"]
        hub.get("demo:kitchen").state["on"] = False
        await hub.routines.tick(at(21, 2))
        await _settle()
        assert not hub.get("demo:kitchen").state["on"]  # didn't fire twice
    asyncio.run(go())


def test_time_trigger_skips_other_days_and_stale_times(make_hub):
    async def go():
        hub = await make_hub()
        hub.clock.now = lambda: at(21, 1)
        hub.routines.routines["ev"] = _event(days=["sat", "sun"])  # Oct 14 2026 is a Wednesday
        await hub.routines.tick(at(21, 1))
        hub.routines.routines["late"] = _event(id="late")
        await hub.routines.tick(at(23, 0))  # hours after 21:00: don't replay
        await _settle()
        assert not hub.get("demo:kitchen").state["on"]
    asyncio.run(go())


def test_device_trigger(make_hub):
    async def go():
        hub = await make_hub()
        hub.clock.now = lambda: at(12)
        hub.routines.routines["ev"] = _event(
            triggers=[{"type": "device", "device": "demo:roomba", "attribute": "status", "to": "cleaning"}],
            actions=[{"type": "set", "target": {"devices": ["demo:coffee"]}, "state": {"on": True}}])
        await hub.command("demo:roomba", "start")
        await _settle()
        assert hub.get("demo:coffee").state["on"]
    asyncio.run(go())


def _curve(**kw):
    base = {"kind": "curve", "id": "cv", "name": "cv", "target": {"kinds": ["light"]},
            "points": [p.model_dump() for p in EVENING]}
    return RoutineAdapter.validate_python(base | kw)


def test_curve_only_adjusts_lights_that_are_on(make_hub):
    async def go():
        hub = await make_hub()
        await hub.set_state("demo:kitchen", {"on": True}, source="routine")
        await hub.routines.apply_curve(_curve(), at(20, 30))
        assert hub.get("demo:kitchen").state["brightness"] == 70
        assert hub.get("demo:bedroom_lamp").state == {"on": False, "brightness": 80, "color_temp": 4000,
                                                      "rgb": [255, 180, 120]}
    asyncio.run(go())


def test_curve_respects_manual_override(make_hub):
    async def go():
        hub = await make_hub()
        await hub.set_state("demo:kitchen", {"on": True, "brightness": 95})  # by hand
        await hub.routines.apply_curve(_curve(), at(20, 30))
        assert hub.get("demo:kitchen").state["brightness"] == 95
        await hub.routines.apply_curve(_curve(manual_override_minutes=0), at(20, 30))
        assert hub.get("demo:kitchen").state["brightness"] == 70
    asyncio.run(go())


def test_light_switched_on_mid_curve_jumps_to_curve(make_hub):
    async def go():
        hub = await make_hub()
        hub.clock.now = lambda: at(22)
        hub.routines.routines["cv"] = _curve()
        await hub.set_state("demo:living_ceiling", {"on": True})  # someone flips the switch at 22:00
        await _settle()
        assert hub.get("demo:living_ceiling").state["brightness"] == 40
        assert hub.get("demo:living_ceiling").state["color_temp"] == 2200
    asyncio.run(go())


def test_fade_reaches_target_and_turns_off(make_hub):
    async def go():
        hub = await make_hub()
        await hub.set_state("demo:kitchen", {"on": True, "brightness": 90}, source="routine")
        r = _event(triggers=[], actions=[{"type": "fade", "target": {"devices": ["demo:kitchen"]},
                                          "minutes": 0.01, "brightness": 20, "color_temp": 2200}])
        await hub.routines._run_actions(r)
        assert hub.get("demo:kitchen").state["brightness"] == 20
        assert hub.get("demo:kitchen").state["color_temp"] == 2200
        r = _event(triggers=[], actions=[{"type": "fade", "target": {"devices": ["demo:kitchen"]},
                                          "minutes": 0.01, "brightness": 0, "off_at_end": True}])
        await hub.routines._run_actions(r)
        assert hub.get("demo:kitchen").state["on"] is False
    asyncio.run(go())


def test_routines_persist(make_hub, tmp_path):
    async def go():
        hub = await make_hub()
        hub.routines.upsert(_curve())
        hub.routines.upsert(_event())
        again = await make_hub()
        again.routines.load()
        assert set(again.routines.routines) == {"cv", "ev"}
        assert again.routines.routines["cv"] == hub.routines.routines["cv"]
    asyncio.run(go())
