"""iRobot Roombas, controlled over the local network (the `roombapy` library).

Each robot needs its BLID and password. With the robot on its dock, hold the
HOME button until it chimes, then run `roombapy discover` within a few seconds;
it prints the IP, BLID and password. List the robots in config.yaml:

    - type: roomba
      id: roomba
      robots:
        - name: Roomba
          room: Living Room
          host: 192.168.1.60
          blid: "3145C...."
          password: ":1:1493...."
"""

from __future__ import annotations

import logging
from typing import Any

from .. import models as m
from ..models import Device
from .base import Integration, IntegrationError, register

log = logging.getLogger(__name__)

# roombapy reports cleanMissionStatus.phase; fold it into the hub's statuses.
PHASE_TO_STATUS = {
    "charge": "charging", "run": "cleaning", "new": "cleaning", "resume": "cleaning",
    "hmMidMsn": "returning", "recharge": "charging", "stuck": "stuck", "hmUsrDock": "returning",
    "dock": "returning", "dockend": "docked", "cancelled": "idle", "stop": "paused",
    "pause": "paused", "evac": "docked", "hmPostMsn": "returning", "": "idle",
}


@register
class RoombaIntegration(Integration):
    type = "roomba"
    poll_interval = 0  # the robot pushes state over MQTT

    async def start(self) -> None:
        try:
            from roombapy import RoombaClient
        except ImportError as e:
            raise IntegrationError("roombapy is not installed: pip install 'smarthome-hub[roomba]'") from e
        self._clients: dict[str, Any] = {}

        for robot in self.config.get("robots", []):
            client = RoombaClient(robot["host"], robot["blid"], robot["password"])
            device = Device(
                id=self.device_id(robot["blid"]), name=robot.get("name", "Roomba"), kind="vacuum",
                integration=self.id, capabilities={m.VACUUM}, room=robot.get("room"),
                state={"status": "idle", "battery": None, "bin_full": False}, available=False,
                attributes={"model": "iRobot Roomba", "commands": list(m.VACUUM_COMMANDS)},
            )
            self._clients[device.id] = client
            self.add_device(device)
            client.register_on_message_callback(lambda _msg, d=device, c=client: self._on_message(d, c))
            await client.connect()
            device.available = client.connected
            self._read_state(device, client)

    def _read_state(self, device: Device, client: Any) -> None:
        reported = client.reported
        phase = client.cleanMissionStatus_phase or ""
        device.available = client.connected
        device.state.update(
            status=PHASE_TO_STATUS.get(phase, "idle"),
            battery=reported.get("batPct"),
            bin_full=bool(client.bin_full),
        )
        if client.error_code:
            device.state["status"] = "error"
            device.attributes["error"] = str(client.error_message)
        else:
            device.attributes.pop("error", None)

    def _on_message(self, device: Device, client: Any) -> None:
        self._read_state(device, client)
        self.push_update(device)

    async def set_state(self, device: Device, changes: dict[str, Any]) -> None:
        raise IntegrationError("Use commands (start, dock, ...) to control a Roomba")

    async def command(self, device: Device, command: str, params: dict[str, Any] | None = None) -> None:
        if command not in m.VACUUM_COMMANDS:
            raise IntegrationError(f"Unknown Roomba command {command!r}")
        await self._clients[device.id].send_command(command, params)

    async def stop(self) -> None:
        for client in getattr(self, "_clients", {}).values():
            await client.disconnect()
