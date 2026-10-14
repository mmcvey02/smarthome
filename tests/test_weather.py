from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from hub import hub as hub_module
from hub.api import create_app
from hub.hub import Hub
from hub.routines.auto import decide_climate, dusk_time
from hub.routines.engine import RoutineAdapter
from hub.routines.models import AutoClimate, AutoLighting, WeatherCheck
from hub.weather import WeatherService, detect_location, parse_forecast

from .conftest import CONFIG, TZ, at

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "open_meteo.json").read_text())
WEATHER_CONFIG = CONFIG | {"weather": {"enabled": True}}


def snapshot(**current) -> dict:
    data = parse_forecast(FIXTURE, TZ)
    data["current"].update(current)
    return data


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


# ---- data ------------------------------------------------------------------

def test_parse_open_meteo_response():
    data = parse_forecast(FIXTURE, TZ)
    assert data["current"]["condition"] == "rain"
    assert data["current"]["description"] == "Light rain"
    assert data["current"]["temperature"] == 64.4
    assert data["today"]["high"] == 66.2 and data["today"]["rain_chance"] == 85
    assert data["today"]["sunset"] == "2026-10-14T18:16-04:00"
    assert len(data["days"]) == 5 and len(data["hours"]) == 24


def test_service_refresh_notifies_and_caches(tmp_path, monkeypatch):
    seen = []

    async def fake_fetch(self):
        return FIXTURE

    monkeypatch.setattr(WeatherService, "fetch", fake_fetch)
    svc = WeatherService(40.7, -74.0, TZ, "F", cache_path=tmp_path / "weather.json")
    svc.subscribe(seen.append)
    asyncio.run(svc.refresh())
    assert seen and svc.error is None and not svc.stale
    assert svc.metrics()["rain_chance"] == 85 and svc.metrics()["condition"] == "rain"
    # Last known weather is available straight after a restart.
    assert WeatherService(40.7, -74.0, TZ, "F", cache_path=tmp_path / "weather.json").metrics() == svc.metrics()
    assert svc.params()["temperature_unit"] == "fahrenheit"
    assert WeatherService(0, 0, TZ, "C").params()["temperature_unit"] == "celsius"


def test_service_survives_network_errors(monkeypatch):
    async def broken(self):
        raise OSError("no route to host")

    monkeypatch.setattr(WeatherService, "fetch", broken)
    svc = WeatherService(40.7, -74.0, TZ, "F")
    asyncio.run(svc.refresh())
    assert "no route to host" in svc.error and svc.metrics() == {}


def test_location_detected_from_ip_and_cached(tmp_path, monkeypatch):
    async def fake_detect():
        return {"latitude": 47.6, "longitude": -122.3, "timezone": "America/Los_Angeles",
                "city": "Seattle", "region": "Washington", "country": "United States", "source": "ip"}

    monkeypatch.setattr(hub_module, "detect_location", fake_detect)
    config = {k: v for k, v in CONFIG.items() if k != "location"}
    with TestClient(create_app(Hub(config, tmp_path))) as c:
        status = c.get("/api/status").json()
        assert status["timezone"] == "America/Los_Angeles"
        assert status["location"]["city"] == "Seattle"
    # Next start uses the cached location without looking it up again.
    assert Hub(config, tmp_path).location_info["city"] == "Seattle"


def test_detect_location_reads_geoip_response(monkeypatch):
    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"success": True, "latitude": 40.7, "longitude": -74.0, "city": "New York",
                    "region": "New York", "country": "United States", "timezone": {"id": "America/New_York"}}

    async def fake_get(self, url, **kw):
        return Resp()

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    loc = asyncio.run(detect_location())
    assert loc["timezone"] == "America/New_York" and loc["city"] == "New York"


# ---- checks & triggers -------------------------------------------------------

def test_weather_check_validation_and_matching():
    assert WeatherCheck(metric="temperature", op="above", value=80).holds({"temperature": 85})
    assert not WeatherCheck(metric="temperature", op="below", value=80).holds({"temperature": 85})
    assert WeatherCheck(metric="condition", op="is_not", value="rain").holds({"condition": "clear"})
    assert WeatherCheck(metric="humidity", op="above", value=60).holds({}) is None
    for bad in ({"metric": "condition", "op": "above", "value": 3},
                {"metric": "condition", "op": "is", "value": "sharknado"},
                {"metric": "temperature", "op": "is", "value": "hot"}):
        with pytest.raises(ValidationError):
            WeatherCheck(**bad)


def test_weather_trigger_fires_on_change_not_at_startup(make_hub):
    async def go():
        hub = await make_hub(WEATHER_CONFIG)
        hub.clock.now = lambda: at(14)
        hub.routines.routines["rain"] = RoutineAdapter.validate_python({
            "kind": "event", "id": "rain", "name": "Rain",
            "triggers": [{"type": "weather", "metric": "condition", "op": "is", "value": "rain"}],
            "actions": [{"type": "set", "target": {"devices": ["demo:living_lamp"]}, "state": {"on": True}}]})
        hub.weather.update(snapshot(condition="rain"))  # raining when the hub starts: baseline only
        await _settle()
        assert not hub.get("demo:living_lamp").state["on"]
        hub.weather.update(snapshot(condition="cloudy"))
        hub.weather.update(snapshot(condition="rain"))  # starts raining
        await _settle()
        assert hub.get("demo:living_lamp").state["on"]
    asyncio.run(go())


def test_weather_conditions_gate_routines(make_hub):
    async def go():
        hub = await make_hub(WEATHER_CONFIG)
        r = RoutineAdapter.validate_python({
            "kind": "event", "id": "v", "name": "v", "triggers": [{"type": "time", "at": "10:00"}],
            "weather": [{"metric": "condition", "op": "is_not", "value": "rain"}],
            "actions": [{"type": "command", "target": {"kinds": ["vacuum"]}, "command": "start"}]})
        assert not hub.routines.conditions_met(r, at(10))  # no data yet
        hub.weather.update(snapshot(condition="rain"))
        assert not hub.routines.conditions_met(r, at(10))
        hub.weather.update(snapshot(condition="clear"))
        assert hub.routines.conditions_met(r, at(10))
    asyncio.run(go())


# ---- automatic climate -----------------------------------------------------

CLIMATE = AutoClimate(comfort_low=68, comfort_high=76, dehumidify_above=65)


@pytest.mark.parametrize("indoor,outdoor,humidity,state,expected", [
    (80, 90, 40, {"on": False, "mode": "cool"}, {"on": True, "target_temp": 76}),
    (63, 40, 40, {"on": False, "mode": "cool"}, {"on": True, "mode": "heat", "target_temp": 68}),
    (72, 70, 80, {"on": False, "mode": "cool"}, {"on": True, "mode": "dry"}),
    (72, 70, 40, {"on": True, "mode": "cool"}, {"on": False}),
    # Holding temperature on a hot day: keep cooling even though it's at the setpoint inside.
    (76, 90, 40, {"on": True, "mode": "cool", "target_temp": 76}, {}),
    # Inside a hair over the band: hysteresis means no change yet.
    (76.5, 70, 40, {"on": False, "mode": "cool"}, {"on": False}),
])
def test_decide_climate(indoor, outdoor, humidity, state, expected):
    state = {"target_temp": 72, **state, "current_temp": indoor}
    changes, why = decide_climate(CLIMATE, state, {"temperature": outdoor, "humidity": humidity}, 1.0)
    if expected == {"on": False} and not state["on"]:
        assert changes == {}
    else:
        assert changes == expected, why


def test_decide_climate_only_adjusts_running_units():
    cfg = AutoClimate(only_when_on=True)
    off = {"on": False, "mode": "cool", "current_temp": 85, "target_temp": 72}
    assert decide_climate(cfg, off, {"temperature": 95}, 1.0)[0] == {}
    running = {"on": True, "mode": "fan", "current_temp": 72, "target_temp": 72}
    assert decide_climate(cfg, running, {"temperature": 72, "humidity": 40}, 1.0)[0] == {}  # never switches off
    assert decide_climate(cfg, {**running, "current_temp": 85}, {"temperature": 95}, 1.0)[0] == \
        {"mode": "cool", "target_temp": 76}


def test_auto_climate_respects_manual_changes_and_short_cycling(make_hub):
    async def go():
        hub = await make_hub(WEATHER_CONFIG)
        r = RoutineAdapter.validate_python({"kind": "auto", "id": "a", "name": "a", "climate": {}})
        hub.weather.update(snapshot(temperature=95, humidity=40))
        ac = hub.get("demo:bedroom_ac")
        ac.state["current_temp"] = 82
        await hub.routines.auto.apply(r, at(14))
        assert ac.state["on"] and ac.state["mode"] == "cool" and ac.state["target_temp"] == 76
        # Cools off fast; a mode change 5 min later would short-cycle the compressor, so wait.
        ac.state["current_temp"] = 70
        hub.weather.update(snapshot(temperature=70, humidity=40))
        await hub.routines.auto.apply(r, at(14, 5))
        assert ac.state["on"]
        await hub.routines.auto.apply(r, at(14, 25))
        assert not ac.state["on"]
        # Someone turns it back on by hand: leave it alone.
        await hub.set_state(ac.id, {"on": True, "mode": "cool", "target_temp": 70})
        await hub.routines.auto.apply(r, at(15))
        assert ac.state["on"]
    asyncio.run(go())


# ---- automatic lighting ----------------------------------------------------

def test_dusk_comes_earlier_when_cloudy():
    cfg = AutoLighting(dusk_offset_minutes=-15, cloudy_extra_minutes=40)
    sunset = at(18, 16)
    assert dusk_time(cfg, sunset, 0) == at(18, 1)
    assert dusk_time(cfg, sunset, 100) == at(17, 21)


def test_auto_lights_on_at_dusk_once(make_hub):
    async def go():
        hub = await make_hub(WEATHER_CONFIG)
        r = RoutineAdapter.validate_python({"kind": "auto", "id": "a", "name": "a",
                                            "lighting": {"gloomy_day": False, "brightness": 60}})
        hub.weather.update(snapshot(cloud_cover=100, condition="cloudy"))
        sunset = hub.clock.resolve("sunset", at(12).date())
        before = sunset.replace(second=0) - timedelta(minutes=65)
        await hub.routines.auto.apply(r, before)
        assert not hub.get("demo:kitchen").state["on"]
        await hub.routines.auto.apply(r, sunset - timedelta(minutes=55))  # overcast: early
        assert hub.get("demo:kitchen").state["on"] and hub.get("demo:kitchen").state["brightness"] == 60
        assert not hub.get("demo:coffee").state["on"]  # plugs aren't lights
        await hub.set_state("demo:kitchen", {"on": False})
        await hub.routines.auto.apply(r, sunset)
        assert not hub.get("demo:kitchen").state["on"]  # someone turned it off; don't fight them
    asyncio.run(go())


def test_auto_lights_never_after_latest_on(make_hub):
    async def go():
        hub = await make_hub(WEATHER_CONFIG)
        r = RoutineAdapter.validate_python({"kind": "auto", "id": "a", "name": "a", "lighting": {"latest_on": "22:00"}})
        await hub.routines.auto.apply(r, at(23, 30))
        assert not hub.get("demo:kitchen").state["on"]
    asyncio.run(go())


def test_gloomy_day_lights_on_then_off(make_hub):
    async def go():
        hub = await make_hub(WEATHER_CONFIG)
        r = RoutineAdapter.validate_python({"kind": "auto", "id": "a", "name": "a", "lighting": {}})
        await hub.set_state("demo:kitchen", {"on": True}, source="routine")  # already on for other reasons
        hub.weather.update(snapshot(condition="storm", cloud_cover=100))
        await hub.routines.auto.apply(r, at(13))
        assert hub.get("demo:living_ceiling").state["on"]
        hub.weather.update(snapshot(condition="cloudy", cloud_cover=80))  # within hysteresis: stay on
        await hub.routines.auto.apply(r, at(13, 30))
        assert hub.get("demo:living_ceiling").state["on"]
        hub.weather.update(snapshot(condition="partly_cloudy", cloud_cover=40))
        await hub.routines.auto.apply(r, at(14))
        assert not hub.get("demo:living_ceiling").state["on"]
        assert hub.get("demo:kitchen").state["on"]  # we didn't turn it on, so we don't turn it off
    asyncio.run(go())


def test_auto_routine_validation():
    with pytest.raises(ValidationError):
        RoutineAdapter.validate_python({"kind": "auto", "id": "a", "name": "a"})
    with pytest.raises(ValidationError):
        RoutineAdapter.validate_python({"kind": "auto", "id": "a", "name": "a",
                                        "climate": {"comfort_low": 75, "comfort_high": 70}})


def test_weather_and_explain_api(tmp_path, monkeypatch):
    async def fake_fetch(self):
        return FIXTURE

    monkeypatch.setattr(WeatherService, "fetch", fake_fetch)
    with TestClient(create_app(Hub(WEATHER_CONFIG, tmp_path))) as c:
        c.post("/api/weather/refresh")
        w = c.get("/api/weather").json()
        assert w["data"]["current"]["description"] == "Light rain" and w["metrics"]["humidity"] == 71
        lines = c.post("/api/auto/explain", json={"lighting": {}, "climate": {}}).json()
        assert any("dusk" in line for line in lines)
        assert any("Mini-Split" in line for line in lines)
        auto = next(r for r in c.get("/api/routines").json() if r["kind"] == "auto")
        assert auto["explain"]
