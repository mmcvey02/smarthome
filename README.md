# Smart Home Hub

One web page to control every smart device in the house (GE Cync lights, MRCOOL mini-splits,
Roombas and anything you add later), plus routines that follow the rhythm of the day.

- **One dashboard** for all your devices, grouped by room. It works on a phone or a wall tablet, and
  state changes appear live.
- **Lighting curves** dim and warm the lights as the evening goes on, using keyframes such as
  "an hour before sunset: 100 %, 4500 K" through "22:30: 15 %, 2200 K". A light switched on at
  11 pm comes on low and amber, not at full daylight brightness. If you adjust a light by hand,
  the curve leaves that light alone for a while.
- **Routines**: "at 06:30 on weekdays, fade the bedroom lights up over 20 minutes", "at 23:00 set
  the mini-splits to 67 °F and fade all lights out", "when the Roomba starts, turn on the
  hallway light, but only after sunset". Triggers can be a clock time, a sunrise/sunset offset
  or a device changing state.
- **Scenes**: a routine with no trigger is a button you press, such as "Movie night".
- **Pluggable drivers**: adding a new brand means writing one small Python class.

Five starter routines are created the first time it runs (Evening wind-down, Sunrise wake-up,
Bedtime, Vacuum while out, Movie night). Edit or delete them freely.

## Quick start

Requires Python 3.11+. Run it on any always-on machine on your home network, such as a
Raspberry Pi, a NAS or an old laptop.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[all]'          # or just the extras you need: .[cync], .[mrcool], .[roomba]
cp config.example.yaml config.yaml
smarthome                         # then open http://<this-machine>:8080
```

The example config starts with simulated **demo** devices, so you can explore the dashboard
and routines before connecting anything. When your real devices are working, remove the
demo integration.

Options: `smarthome --config path/to/config.yaml --data path/to/data serve --port 8080`.

## Connecting your devices

### GE Cync lights and plugs

Cync has no local API, so the hub talks to the Cync cloud the same way the app does, using the
[`pycync`](https://pypi.org/project/pycync/) library (which Home Assistant also uses).

1. Fill in the `cync` section of `config.yaml` with your Cync email and password, and set
   `enabled: true`.
2. Run `smarthome cync-login`. Cync emails you a code; type it in. A refresh token is saved
   to `data/cync_token.json`, so you only do this once.
3. Restart the hub. Bulbs keep the room names from the Cync app.

### MRCOOL mini-splits

Most MRCOOL DIY and Advantage units use Midea's Wi-Fi module, so they can be controlled
**locally** with [`msmart-ng`](https://github.com/mill1000/midea-msmart), with no cloud round trip.

1. Run `msmart-ng discover`. For each unit it prints `ip`, `id`, `token` and `key`.
   (Units with older firmware print no token or key, and don't need them.)
2. Add each unit under `units:` in the `mrcool` section, with a friendly `name` and `room`.

If `discover` doesn't find a unit, it's on a module that doesn't support local control.

Give each unit a DHCP reservation in your router so its IP address doesn't change.

### Roomba

Roombas are controlled locally over MQTT with [`roombapy`](https://pypi.org/project/roombapy/).

1. With the robot on its dock, hold its **HOME** button until it chimes. Within a few seconds,
   run `roombapy discover`. It prints the IP address, BLID and password.
2. Add the robot under `robots:` in the `roomba` section.

Only one app can talk to a Roomba locally at a time. The iRobot phone app still works through the
cloud.

## Routines

Open the **Routines** tab and choose **+ Lighting curve** or **+ Routine**. The editor has a form
and a **JSON** view for advanced edits. Routines are stored in `data/routines.json`.

**Times** can be `HH:MM` or sun-relative: `sunrise`, `sunset`, `dawn`, `dusk` or `noon`, with an
optional offset in minutes (`sunset-45`, `sunrise+20`). Sun times need `location` set in
`config.yaml`.

**Targets** choose devices by type (all lights), by room, by specific device, or every device.
An action only affects devices that support it: "set brightness 30 %" aimed at a room leaves
the thermostat alone.

### Lighting curves

```json
{
  "kind": "curve", "id": "evening-wind-down", "name": "Evening wind-down",
  "target": {"kinds": ["light"]},
  "points": [
    {"at": "sunset-60", "brightness": 100, "color_temp": 4500},
    {"at": "21:30",     "brightness": 40,  "color_temp": 2500},
    {"at": "22:30",     "brightness": 15,  "color_temp": 2200},
    {"at": "05:30",     "brightness": 10,  "color_temp": 2000}
  ],
  "only_when_on": true,
  "manual_override_minutes": 60,
  "step_minutes": 2
}
```

- Between keyframes, values are interpolated. Keyframes are read in order, so `05:30` after
  `22:30` means the next morning.
- Outside the first and last keyframe, the curve does nothing.
- `only_when_on`: lights that are off stay off. When someone switches one on, it comes on at
  the curve's current level.
- `manual_override_minutes`: after you pick a brightness or colour by hand, the curve leaves
  that light alone for this long.
- The editor draws the curve for the next 24 hours, with a band showing the colour temperature.

### Event routines and scenes

```json
{
  "kind": "event", "id": "sunrise-wake-up", "name": "Sunrise wake-up",
  "days": ["mon", "tue", "wed", "thu", "fri"],
  "triggers": [{"type": "time", "at": "06:30"}],
  "between": null,
  "actions": [
    {"type": "set",  "target": {"rooms": ["Bedroom"]}, "state": {"on": true, "brightness": 1, "color_temp": 2000}},
    {"type": "fade", "target": {"rooms": ["Bedroom"]}, "minutes": 20, "brightness": 70, "color_temp": 4000},
    {"type": "delay", "minutes": 10},
    {"type": "set",  "target": {"devices": ["cync:12345"]}, "state": {"on": true}}
  ]
}
```

| Trigger | Fields |
|---|---|
| `time` | `at`: a clock or sun time |
| `device` | `device`, `attribute`, `to` (optional; leave it out to fire on any change). Example: `{"device": "roomba:ABC", "attribute": "status", "to": "cleaning"}` |

| Action | Fields |
|---|---|
| `set` | `target`, `state`: any of `on`, `brightness` (0–100), `color_temp` (Kelvin), `rgb`, `mode` (auto/cool/heat/dry/fan), `target_temp`, `fan_speed` |
| `fade` | `target`, `minutes`, `brightness` and/or `color_temp`, `off_at_end` |
| `command` | `target`, `command`: `start`, `pause`, `resume`, `stop` or `dock` (vacuums) |
| `delay` | `minutes` |
| `routine` | `routine`: id of another routine to run |

`days` limits which weekdays a routine runs. `between: ["sunset", "23:30"]` limits it to a
time window, which is useful with device triggers. **Run** in the dashboard ignores both.

## Adding a new kind of device

Each brand is an `Integration` subclass. It translates the vendor's API into the hub's common
model: device kinds `light`, `plug`, `climate`, `vacuum`, plus the capabilities and state keys
listed in [`hub/models.py`](hub/models.py). The dashboard and routines then work with the new
device automatically.

```python
# my_drivers/shelly.py
from hub.drivers.base import Integration, register
from hub.models import Device, POWER

@register
class ShellyIntegration(Integration):
    type = "shelly"          # used as `type:` in config.yaml
    poll_interval = 15       # seconds; 0 if the device pushes updates

    async def start(self):
        for relay in self.config["relays"]:
            self.add_device(Device(
                id=self.device_id(relay["host"]), name=relay["name"], kind="plug",
                integration=self.id, capabilities={POWER}, room=relay.get("room"),
                state={"on": False}))
        await self.poll()

    async def poll(self):
        for device in self.devices.values():
            ...                  # read the real state into device.state
            self.push_update(device)

    async def set_state(self, device, changes):
        ...                      # send changes["on"] to the device
        device.state.update(changes)
        self.push_update(device)
```

Then list the module under `plugins:` and add an integration entry in `config.yaml`:

```yaml
plugins: [my_drivers.shelly]
integrations:
  - type: shelly
    id: shelly
    relays: [{host: 192.168.1.80, name: Porch Light, room: Outside}]
```

To ship a driver with the hub instead, put it in `hub/drivers/` and import it in
`hub/drivers/__init__.py`. `hub/drivers/demo.py` is a complete minimal example.

## Running it permanently (Raspberry Pi / Linux)

```ini
# /etc/systemd/system/smarthome.service
[Unit]
Description=Smart Home Hub
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/smarthome
ExecStart=/home/pi/smarthome/.venv/bin/smarthome
Restart=always

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now smarthome`

## Security

The dashboard can control physical devices, so:

- Set `password:` in `config.yaml` if anyone else uses your Wi-Fi.
- Don't expose port 8080 to the internet. For access away from home, use a VPN such as
  Tailscale or WireGuard.
- `config.yaml` and `data/` hold credentials and are git-ignored. Keep them that way.

## API

The dashboard is a client of a small JSON API, which you can also script against:

| | |
|---|---|
| `GET /api/status` | time, sun times, integration health, rooms |
| `GET /api/devices` | all devices |
| `POST /api/devices/{id}/state` | `{"on": true, "brightness": 40}` |
| `POST /api/devices/{id}/command` | `{"command": "start"}` |
| `PATCH /api/devices/{id}` | `{"name": "...", "room": "...", "hidden": false}` |
| `POST /api/rooms/{room}/state` | apply state to every device in a room that supports it |
| `GET/POST /api/routines`, `PUT/DELETE /api/routines/{id}` | manage routines |
| `POST /api/routines/{id}/run`, `/stop` | run now / cancel a running routine |
| `POST /api/curve/preview` | sample a curve over 24 hours |
| `WS /api/ws` | live device updates |

Interactive docs are at `/docs`.

## Development

```bash
pip install -e '.[all,dev]'
pytest
```

Layout: `hub/models.py` (common device model), `hub/drivers/` (one file per brand),
`hub/routines/` (time parsing, routine schema, engine, starter routines), `hub/hub.py` (device
registry and integration lifecycle), `hub/api.py` (HTTP/WebSocket), `hub/static/` (dashboard, plain
JS, no build step).
