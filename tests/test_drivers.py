"""Drivers against the real vendor libraries, with the network calls stubbed out."""

from __future__ import annotations

import asyncio

import pytest

from hub.drivers.base import IntegrationError

from .conftest import CONFIG


def test_mrcool_maps_state_both_ways(make_hub, monkeypatch):
    msmart = pytest.importorskip("msmart.device")
    AC = msmart.AirConditioner
    applied = []

    async def noop(self, *a, **kw):
        return None

    async def apply(self):
        applied.append((self.power_state, self.operational_mode, self.target_temperature, self.fan_speed))

    for name in ("get_capabilities", "refresh", "authenticate"):
        monkeypatch.setattr(AC, name, noop)
    monkeypatch.setattr(AC, "apply", apply)

    async def go():
        hub = await make_hub(CONFIG | {"integrations": [{"type": "mrcool", "id": "mrcool", "units": [
            {"name": "Bedroom Mini-Split", "room": "Bedroom", "host": "10.0.0.9", "device_id": 1234,
             "token": "aa", "key": "bb"}]}]})
        device = hub.get("mrcool:1234")
        assert device.room == "Bedroom" and device.kind == "climate"
        await hub.set_state(device.id, {"on": True, "mode": "cool", "target_temp": 68, "fan_speed": "low"})
        power, mode, target_c, fan = applied[-1]
        assert power is True and mode == AC.OperationalMode.COOL and fan == AC.FanSpeed.LOW
        assert target_c == 20.0  # 68 °F
        assert device.state["target_temp"] == 68 and device.state["mode"] == "cool"
    asyncio.run(go())


def test_roomba_maps_mission_phase(make_hub, monkeypatch):
    roombapy = pytest.importorskip("roombapy")
    sent = []

    async def connect(self):
        self._connected = True

    async def send_command(self, command, params=None):
        sent.append(command)

    monkeypatch.setattr(roombapy.RoombaClient, "connect", connect)
    monkeypatch.setattr(roombapy.RoombaClient, "send_command", send_command)

    async def go():
        hub = await make_hub(CONFIG | {"integrations": [{"type": "roomba", "id": "roomba", "robots": [
            {"name": "Rosie", "host": "10.0.0.7", "blid": "ABC", "password": "pw"}]}]})
        device = hub.get("roomba:ABC")
        integ = hub.integrations["roomba"]
        client = integ._clients[device.id]
        client._state.apply({"state": {"reported": {"batPct": 64, "cleanMissionStatus": {"phase": "run"}}}})
        integ._on_message(device, client)
        assert device.state["status"] == "cleaning" and device.state["battery"] == 64
        await hub.command(device.id, "dock")
        assert sent == ["dock"]
        with pytest.raises(IntegrationError):
            await hub.command(device.id, "self_destruct")
    asyncio.run(go())


def test_cync_requires_login_first(make_hub):
    pytest.importorskip("pycync")

    async def go():
        hub = await make_hub(CONFIG | {"integrations": [{"type": "cync", "id": "cync", "username": "a@b.c"}]})
        assert "cync-login" in hub.integrations["cync"].error
    asyncio.run(go())
