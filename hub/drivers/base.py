"""Base class for integrations (one per vendor account or local device set).

To add a new kind of device, subclass `Integration`, give it a unique `type`,
and decorate it with `@register`. See README "Adding a new device type".
"""

from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import Device

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Integration]] = {}


def register(cls: type[Integration]) -> type[Integration]:
    REGISTRY[cls.type] = cls
    return cls


class IntegrationError(Exception):
    pass


class Integration(abc.ABC):
    #: Name used as `type:` in config.yaml.
    type: ClassVar[str]
    #: Seconds between `poll()` calls; 0 disables polling (push-based drivers).
    poll_interval: ClassVar[float] = 30

    def __init__(self, id: str, config: dict[str, Any], hub: Hub):
        self.id = id
        self.config = config
        self.hub = hub
        self.devices: dict[str, Device] = {}
        self.error: str | None = None

    def device_id(self, native_id: Any) -> str:
        return f"{self.id}:{native_id}"

    def add_device(self, device: Device) -> None:
        self.devices[device.id] = device

    def push_update(self, device: Device) -> None:
        """Tell the hub a device's state changed (from a poll or a push event)."""
        self.hub.device_updated(device)

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect, discover devices and populate `self.devices`."""

    async def stop(self) -> None:
        pass

    async def poll(self) -> None:
        """Refresh device state. Called every `poll_interval` seconds."""

    @abc.abstractmethod
    async def set_state(self, device: Device, changes: dict[str, Any]) -> None:
        """Apply already-validated state changes (see hub.models for keys)."""

    async def command(self, device: Device, command: str, params: dict[str, Any] | None = None) -> None:
        raise IntegrationError(f"{device.name} does not support command {command!r}")
