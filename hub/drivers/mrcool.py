"""MRCOOL mini-splits, controlled over the local network (the `msmart-ng` library).

MRCOOL DIY / Advantage units use Midea's Wi-Fi module and LAN protocol, so they
can be driven locally with no cloud round-trip. Find each unit's id, token and
key with `msmart-ng discover` (see README), then list them in config.yaml:

    - type: mrcool
      id: mrcool
      units:
        - name: Bedroom Mini-Split
          room: Bedroom
          host: 192.168.1.50
          device_id: 147334558165565
          token: "..."      # only for V3 firmware
          key: "..."
"""

from __future__ import annotations

import logging
from typing import Any

from .. import models as m
from ..models import Device
from .base import Integration, IntegrationError, register

log = logging.getLogger(__name__)

MODE_TO_MIDEA = {"auto": "AUTO", "cool": "COOL", "heat": "HEAT", "dry": "DRY", "fan": "FAN_ONLY"}
MIDEA_TO_MODE = {v: k for k, v in MODE_TO_MIDEA.items()} | {"SMART_DRY": "dry"}
FAN_TO_MIDEA = {"auto": "AUTO", "silent": "SILENT", "low": "LOW", "medium": "MEDIUM", "high": "HIGH", "max": "MAX"}
MIDEA_TO_FAN = {v: k for k, v in FAN_TO_MIDEA.items()}


@register
class MrCoolIntegration(Integration):
    type = "mrcool"
    poll_interval = 30

    async def start(self) -> None:
        try:
            from msmart.device import AirConditioner
        except ImportError as e:
            raise IntegrationError("msmart-ng is not installed: pip install 'smarthome-hub[mrcool]'") from e
        self._AC = AirConditioner
        self._units: dict[str, Any] = {}

        for unit in self.config.get("units", []):
            ac = AirConditioner(ip=unit["host"], device_id=int(unit["device_id"]), port=int(unit.get("port", 6444)))
            if unit.get("token") and unit.get("key"):
                await ac.authenticate(unit["token"], unit["key"])
            await ac.get_capabilities()
            await ac.refresh()

            to_hub = self._to_hub
            device = Device(
                id=self.device_id(unit["device_id"]), name=unit.get("name", f"Mini-split {unit['host']}"),
                kind="climate", integration=self.id, capabilities={m.POWER, m.CLIMATE}, room=unit.get("room"),
                attributes={
                    "model": "MRCOOL",
                    "min_temp": round(to_hub(ac.min_target_temperature)),
                    "max_temp": round(to_hub(ac.max_target_temperature)),
                    "modes": [MIDEA_TO_MODE[x.name] for x in ac.supported_operation_modes if x.name in MIDEA_TO_MODE],
                    "fan_speeds": [MIDEA_TO_FAN[x.name] for x in ac.supported_fan_speeds if x.name in MIDEA_TO_FAN],
                },
            )
            self._units[device.id] = ac
            self._read_state(device, ac)
            self.add_device(device)

    def _to_hub(self, celsius: float | None) -> float | None:
        if celsius is None:
            return None
        return round(m.c_to_f(celsius)) if self.hub.temperature_unit == "F" else celsius

    def _from_hub(self, value: float) -> float:
        c = m.f_to_c(value) if self.hub.temperature_unit == "F" else value
        return round(c * 2) / 2  # units accept 0.5 °C steps

    def _read_state(self, device: Device, ac: Any) -> None:
        device.available = bool(ac.online)
        fan = ac.fan_speed
        device.state.update(
            on=bool(ac.power_state),
            mode=MIDEA_TO_MODE.get(ac.operational_mode.name, "auto"),
            target_temp=self._to_hub(ac.target_temperature),
            current_temp=self._to_hub(ac.indoor_temperature),
            fan_speed=MIDEA_TO_FAN.get(getattr(fan, "name", ""), "auto"),
        )

    async def poll(self) -> None:
        for device_id, ac in self._units.items():
            try:
                await ac.refresh()
            except Exception as e:  # unit unplugged, Wi-Fi blip...
                log.warning("refresh failed for %s: %s", device_id, e)
            device = self.devices[device_id]
            self._read_state(device, ac)
            self.push_update(device)

    async def set_state(self, device: Device, changes: dict[str, Any]) -> None:
        ac = self._units[device.id]
        if "on" in changes:
            ac.power_state = bool(changes["on"])
        if "mode" in changes:
            ac.operational_mode = self._AC.OperationalMode[MODE_TO_MIDEA[changes["mode"]]]
        if "target_temp" in changes:
            ac.target_temperature = self._from_hub(float(changes["target_temp"]))
        if "fan_speed" in changes:
            ac.fan_speed = self._AC.FanSpeed[FAN_TO_MIDEA[changes["fan_speed"]]]
        await ac.apply()
        self._read_state(device, ac)
        self.push_update(device)
