from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from hub.api import create_app
from hub.drivers.cync import cync_to_kelvin, kelvin_to_cync
from hub.hub import Hub

from .conftest import CONFIG


def client(tmp_path, **extra) -> TestClient:
    return TestClient(create_app(Hub(CONFIG | extra, tmp_path)))


def test_devices_and_control(tmp_path):
    with client(tmp_path) as c:
        devices = c.get("/api/devices").json()
        assert {d["kind"] for d in devices} == {"light", "plug", "climate", "vacuum"}
        r = c.post("/api/devices/demo:bedroom_ac/state", json={"on": True, "mode": "heat", "target_temp": 99})
        assert r.status_code == 200
        assert r.json()["state"]["target_temp"] == 86  # clamped to the unit's range
        assert c.post("/api/devices/demo:coffee/state", json={"brightness": 50}).status_code == 400
        assert c.post("/api/devices/demo:bedroom_ac/state", json={"mode": "turbo"}).status_code == 400
        assert c.post("/api/devices/nope/state", json={"on": True}).status_code == 404
        assert c.post("/api/devices/demo:roomba/command", json={"command": "dock"}).json()["state"]["status"] == "returning"


def test_room_control_and_device_overrides(tmp_path):
    with client(tmp_path) as c:
        assert c.post("/api/rooms/Living Room/state", json={"on": True}).json()["updated"] == 3
        c.patch("/api/devices/demo:coffee", json={"name": "Espresso", "room": "Office"})
    with client(tmp_path) as c:  # survives a restart
        coffee = c.get("/api/devices/demo:coffee").json()
        assert (coffee["name"], coffee["room"]) == ("Espresso", "Office")
        assert "Office" in c.get("/api/status").json()["rooms"]


def test_routine_crud(tmp_path):
    with client(tmp_path) as c:
        defaults = {r["id"] for r in c.get("/api/routines").json()}
        assert "evening-wind-down" in defaults
        body = {"kind": "event", "id": "lights-out", "name": "Lights out",
                "triggers": [{"type": "time", "at": "sunset+120"}],
                "actions": [{"type": "set", "target": {"all": True}, "state": {"on": False}}]}
        assert c.post("/api/routines", json=body).status_code == 200
        assert c.post("/api/routines", json=body).status_code == 409
        assert c.put("/api/routines/lights-out", json=body | {"enabled": False}).json()["enabled"] is False
        bad = c.post("/api/routines", json=body | {"id": "bad", "triggers": [{"type": "time", "at": "soon"}]})
        assert bad.status_code == 422
        assert c.post("/api/routines/lights-out/run").status_code == 200
        assert c.delete("/api/routines/lights-out").status_code == 200
        assert c.post("/api/routines/lights-out/run").status_code == 404


def test_curve_preview(tmp_path):
    with client(tmp_path) as c:
        samples = c.post("/api/curve/preview", json={"points": [
            {"at": "20:00", "brightness": 80}, {"at": "23:00", "brightness": 20}]}).json()
        by_time = {s["t"][11:16]: s.get("brightness") for s in samples}
        assert by_time["12:00"] is None
        assert by_time["21:30"] == 50


def test_password_protects_api(tmp_path):
    with client(tmp_path, password="hunter2") as c:
        assert c.get("/api/devices").status_code == 401
        good = base64.b64encode(b"me:hunter2").decode()
        assert c.get("/api/devices", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_cync_colour_temp_scale():
    assert kelvin_to_cync(7000) == 1
    assert kelvin_to_cync(2000) == 100
    assert kelvin_to_cync(1500) == 100  # clamped
    for k in (2200, 2700, 4000, 6500):
        assert abs(cync_to_kelvin(kelvin_to_cync(k)) - k) <= 60
    assert cync_to_kelvin(0) is None  # RGB mode
