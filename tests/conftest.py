from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hub.hub import Hub

TZ = ZoneInfo("America/New_York")
CONFIG = {
    "temperature_unit": "F",
    "location": {"latitude": 40.71, "longitude": -74.0, "timezone": "America/New_York"},
    "integrations": [{"type": "demo", "id": "demo"}],
    "weather": {"enabled": False},  # tests that need weather stub the fetch
}


def at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


@pytest.fixture
def make_hub(tmp_path: Path):
    """Build a Hub with demo devices, started without the background loops."""
    async def _make(config: dict | None = None) -> Hub:
        hub = Hub(config or CONFIG, tmp_path)
        integ_spec = hub.config["integrations"][0]
        from hub.drivers.base import REGISTRY
        integ = REGISTRY[integ_spec["type"]](integ_spec.get("id", integ_spec["type"]),
                                            {k: v for k, v in integ_spec.items() if k not in ("type", "id")}, hub)
        hub.integrations[integ.id] = integ
        await hub._start_integration(integ)
        for t in hub._poll_tasks:
            t.cancel()
        return hub
    return _make
