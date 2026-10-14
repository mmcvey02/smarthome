"""GE Cync lights and plugs, via the cloud API (the `pycync` library).

Cync accounts require an emailed two-factor code on first login, so run
`smarthome cync-login` once; it stores a refresh token in data/cync_token.json
and the hub signs in silently from then on.

config.yaml:
    - type: cync
      id: cync
      username: you@example.com
      password: "..."
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .. import models as m
from ..models import Device
from .base import Integration, IntegrationError, register

log = logging.getLogger(__name__)

# Cync expresses white temperature as 1 (coolest) .. 100 (warmest).
CYNC_KELVIN_COOL = 7000
CYNC_KELVIN_WARM = 2000


def kelvin_to_cync(kelvin: int) -> int:
    k = m.clamp(kelvin, CYNC_KELVIN_WARM, CYNC_KELVIN_COOL)
    return round(1 + (CYNC_KELVIN_COOL - k) / (CYNC_KELVIN_COOL - CYNC_KELVIN_WARM) * 99)


def cync_to_kelvin(value: int) -> int | None:
    if not 1 <= value <= 100:
        return None  # bulb is in RGB / effect mode
    return round(CYNC_KELVIN_COOL - (value - 1) / 99 * (CYNC_KELVIN_COOL - CYNC_KELVIN_WARM))


def token_path(hub_data_dir: Path, integration_id: str) -> Path:
    return hub_data_dir / f"{integration_id}_token.json"


def save_user(path: Path, user: Any) -> None:
    path.write_text(json.dumps({
        "access_token": user.access_token, "refresh_token": user.refresh_token,
        "authorize": user.authorize, "user_id": user.user_id, "expires_at": user.expires_at,
    }))
    path.chmod(0o600)


@register
class CyncIntegration(Integration):
    type = "cync"
    poll_interval = 60  # state is pushed over TCP; polling is just a safety net

    async def start(self) -> None:
        try:
            import aiohttp
            from pycync import Auth, Cync, User
            from pycync.devices import CyncLight, CyncPlug
        except ImportError as e:
            raise IntegrationError("pycync is not installed: pip install 'smarthome-hub[cync]'") from e

        self._token_file = token_path(self.hub.data_dir, self.id)
        if not self._token_file.exists():
            raise IntegrationError("Not signed in. Run `smarthome cync-login` once to enter the emailed code.")
        saved = json.loads(self._token_file.read_text())
        user = User(saved["access_token"], saved["refresh_token"], saved["authorize"],
                    saved["user_id"], expires_at=saved["expires_at"])

        self._session = aiohttp.ClientSession()
        self._auth = Auth(self._session, user=user,
                          username=self.config.get("username"), password=self.config.get("password"))
        if user.expires_at - time.time() < 3600:
            await self._auth.async_refresh_user_token()
            save_user(self._token_file, self._auth.user)

        self._cync = await Cync.create(self._auth)
        self._native: dict[str, Any] = {}

        rooms: dict[str, str] = {}
        for home in self._cync.get_homes():
            for room in home.rooms:
                for dev in room.devices + [d for g in room.groups for d in g.devices]:
                    rooms[str(dev.device_id)] = room.name

        for dev in self._cync.get_devices():
            if self.device_id(dev.device_id) in self._native:
                continue  # listed under both a room and a group
            if isinstance(dev, CyncLight):
                kind, caps = "light", {m.POWER}
                if dev.supports_capability(_cap("DIMMING")):
                    caps.add(m.BRIGHTNESS)
                if dev.supports_capability(_cap("CCT_COLOR")):
                    caps.add(m.COLOR_TEMP)
                if dev.supports_capability(_cap("RGB_COLOR")):
                    caps.add(m.COLOR)
            elif isinstance(dev, CyncPlug):
                kind, caps = "plug", {m.POWER}
            else:
                continue
            device = Device(
                id=self.device_id(dev.device_id), name=dev.name, kind=kind, integration=self.id,
                capabilities=caps, room=rooms.get(str(dev.device_id)), attributes={"model": "GE Cync"},
            )
            self._native[device.id] = dev
            self._read_state(device, dev)
            self.add_device(device)

        self._cync.set_update_callback(self._on_update)
        self._cync.update_device_states()

    def _read_state(self, device: Device, dev: Any) -> None:
        device.available = bool(getattr(dev, "is_online", True))
        device.state["on"] = bool(dev.is_on)
        if device.supports(m.BRIGHTNESS):
            device.state["brightness"] = dev.brightness
        if device.supports(m.COLOR_TEMP):
            device.state["color_temp"] = cync_to_kelvin(dev.color_temp)
        if device.supports(m.COLOR):
            device.state["rgb"] = list(dev.rgb)

    def _on_update(self, updated: dict[Any, Any]) -> None:
        for dev in updated.values():
            device = self.devices.get(self.device_id(dev.device_id))
            if device:
                self._read_state(device, dev)
                self.push_update(device)

    async def poll(self) -> None:
        if self._auth.user.expires_at - time.time() < 3600:
            await self._auth.async_refresh_user_token()
            save_user(self._token_file, self._auth.user)
        self._cync.update_device_states()

    async def set_state(self, device: Device, changes: dict[str, Any]) -> None:
        dev = self._native[device.id]
        if changes.get("on") is False:
            await dev.turn_off()
            return
        if "on" in changes and len(changes) == 1:
            await dev.turn_on()
            return
        # Brightness / colour changes also switch the bulb on, matching the Cync app.
        if "brightness" in changes:
            await dev.set_brightness(int(m.clamp(changes["brightness"], 0, 100)))
        elif changes.get("on"):
            await dev.turn_on()
        if "color_temp" in changes:
            await dev.set_color_temp(kelvin_to_cync(int(changes["color_temp"])))
        if "rgb" in changes:
            await dev.set_rgb(tuple(int(m.clamp(c, 0, 255)) for c in changes["rgb"]))

    async def stop(self) -> None:
        if getattr(self, "_cync", None):
            await self._cync.shut_down()
        if getattr(self, "_session", None):
            await self._session.close()


def _cap(name: str) -> Any:
    from pycync.devices.capabilities import CyncCapability
    return CyncCapability[name]


async def interactive_login(username: str, password: str, out: Path) -> None:
    """Sign in, prompting for the emailed 2FA code, and store the token."""
    import aiohttp
    from pycync import Auth
    from pycync.exceptions import TwoFactorRequiredError

    async with aiohttp.ClientSession() as session:
        auth = Auth(session, username=username, password=password)
        try:
            user = await auth.login()
        except TwoFactorRequiredError:
            code = input("Cync emailed you a verification code. Enter it: ").strip()
            user = await auth.login(code)
        save_user(out, user)
