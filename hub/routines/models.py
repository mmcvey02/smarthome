"""Routine definitions. Three kinds:

* event  - "when X happens (and conditions hold), do these actions in order".
           Triggers: a clock/sun time, a device changing state, or the weather
           (it starts raining, it gets above 85). Can also require weather conditions.
           Actions: set state, send a command, fade, wait, run another routine.
* auto   - "automatic": lights come on when it actually gets dark (earlier on cloudy
           evenings, during gloomy days) and mini-splits pick heat / cool / dry / off from
           indoor temperature, outdoor weather and humidity. Uses live weather.
* curve  - a lighting curve through the day: keyframes of brightness and colour
           temperature that the hub interpolates between and keeps applying, so
           lights dim and warm gradually as the evening goes on. Lights that are
           switched on mid-curve come on at the right level straight away.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .timespec import parse as parse_time

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CONDITIONS = ("clear", "partly_cloudy", "cloudy", "fog", "rain", "snow", "storm")


def _check_time(v: str) -> str:
    parse_time(v)
    return v


class Target(BaseModel):
    """Which devices an action applies to. The union of all the selectors."""
    devices: list[str] = []
    rooms: list[str] = []
    kinds: list[str] = []
    all: bool = False

    @model_validator(mode="after")
    def _not_empty(self) -> Target:
        if not (self.devices or self.rooms or self.kinds or self.all):
            raise ValueError("target must name at least one device, room or kind")
        return self


class TimeTrigger(BaseModel):
    type: Literal["time"] = "time"
    at: str

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        return _check_time(v)


class DeviceTrigger(BaseModel):
    type: Literal["device"] = "device"
    device: str
    attribute: str
    to: Any = None  # None = any change


class WeatherCheck(BaseModel):
    """A test against current weather, e.g. temperature above 85, condition is rain."""
    metric: Literal["temperature", "feels_like", "humidity", "cloud_cover", "precipitation", "wind_speed",
                    "uv_index", "rain_chance", "today_high", "today_low", "condition"]
    op: Literal["above", "below", "is", "is_not"] = "above"
    value: float | str

    @model_validator(mode="after")
    def _consistent(self) -> WeatherCheck:
        if self.metric == "condition":
            if self.op not in ("is", "is_not"):
                raise ValueError("condition uses 'is' or 'is_not'")
            if self.value not in CONDITIONS:
                raise ValueError(f"condition must be one of {', '.join(CONDITIONS)}")
        elif self.op not in ("above", "below") or isinstance(self.value, str):
            raise ValueError(f"{self.metric} needs 'above' or 'below' and a number")
        return self

    def holds(self, metrics: dict[str, Any]) -> bool | None:
        """True/False, or None when there's no weather data to judge by."""
        actual = metrics.get(self.metric)
        if actual is None:
            return None
        if self.op == "is":
            return actual == self.value
        if self.op == "is_not":
            return actual != self.value
        return actual > self.value if self.op == "above" else actual < self.value


class WeatherTrigger(WeatherCheck):
    """Fires when the check goes from false to true (e.g. it starts raining)."""
    type: Literal["weather"] = "weather"


Trigger = Annotated[Union[TimeTrigger, DeviceTrigger, WeatherTrigger], Field(discriminator="type")]


class SetAction(BaseModel):
    type: Literal["set"] = "set"
    target: Target
    state: dict[str, Any]


class CommandAction(BaseModel):
    type: Literal["command"] = "command"
    target: Target
    command: str


class FadeAction(BaseModel):
    """Gradually move lights to a brightness / colour temperature."""
    type: Literal["fade"] = "fade"
    target: Target
    minutes: float = Field(gt=0, le=240)
    brightness: int | None = Field(None, ge=0, le=100)
    color_temp: int | None = Field(None, ge=1500, le=9000)
    # Switch off lights at the end (e.g. a fade to 0 at bedtime).
    off_at_end: bool = False

    @model_validator(mode="after")
    def _something(self) -> FadeAction:
        if self.brightness is None and self.color_temp is None:
            raise ValueError("fade needs a brightness and/or color_temp")
        return self


class DelayAction(BaseModel):
    type: Literal["delay"] = "delay"
    minutes: float = Field(gt=0, le=24 * 60)


class RunRoutineAction(BaseModel):
    type: Literal["routine"] = "routine"
    routine: str


Action = Annotated[
    Union[SetAction, CommandAction, FadeAction, DelayAction, RunRoutineAction], Field(discriminator="type")
]


class RoutineBase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    name: str
    enabled: bool = True
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = list(WEEKDAYS)
    description: str = ""


class EventRoutine(RoutineBase):
    kind: Literal["event"] = "event"
    triggers: list[Trigger] = []  # empty = manual only (a "scene")
    # Only run between these two times, e.g. ["sunset", "23:00"]. Wraps past midnight.
    between: tuple[str, str] | None = None
    # Only run if all of these hold, e.g. [{"metric": "condition", "op": "is_not", "value": "rain"}].
    weather: list[WeatherCheck] = []
    actions: list[Action] = Field(min_length=1)

    @field_validator("between")
    @classmethod
    def _between(cls, v: tuple[str, str] | None) -> tuple[str, str] | None:
        if v:
            _check_time(v[0])
            _check_time(v[1])
        return v


class CurvePoint(BaseModel):
    at: str
    brightness: int | None = Field(None, ge=0, le=100)
    color_temp: int | None = Field(None, ge=1500, le=9000)

    @field_validator("at")
    @classmethod
    def _at(cls, v: str) -> str:
        return _check_time(v)


class CurveRoutine(RoutineBase):
    kind: Literal["curve"] = "curve"
    target: Target
    points: list[CurvePoint] = Field(min_length=2)
    # Leave lights that are off alone; the curve applies when they're switched on.
    only_when_on: bool = True
    # After someone adjusts a light by hand, leave that light alone for this long.
    manual_override_minutes: int = Field(60, ge=0)
    # How often to nudge lights along the curve.
    step_minutes: float = Field(2, ge=0.5, le=60)


class AutoLighting(BaseModel):
    target: Target = Field(default_factory=lambda: Target(kinds=["light"]))
    # Turn lights on when it actually gets dark: at sunset (+ offset), and earlier on cloudy evenings.
    dusk_on: bool = True
    dusk_offset_minutes: int = Field(-15, ge=-180, le=180)
    cloudy_extra_minutes: int = Field(45, ge=0, le=180)
    # Turn lights on during the day when it's dark out (heavy cloud, rain, storms), off when it clears.
    gloomy_day: bool = True
    gloomy_cloud_cover: int = Field(90, ge=0, le=100)
    # Brightness to use when switching on; None leaves it to the light (or a lighting curve).
    brightness: int | None = Field(None, ge=1, le=100)
    # Don't switch lights on after this time (no surprise lights at 2 am).
    latest_on: str = "22:00"

    @field_validator("latest_on")
    @classmethod
    def _t(cls, v: str) -> str:
        return _check_time(v)


class AutoClimate(BaseModel):
    target: Target = Field(default_factory=lambda: Target(kinds=["climate"]))
    # Comfort band in the hub's temperature unit.
    comfort_low: float = 68
    comfort_high: float = 76
    # What to do when it's comfortable inside and mild outside.
    mild_action: Literal["off", "fan", "leave"] = "off"
    # Run "dry" mode when it's comfortable but outdoor humidity is above this %. None disables.
    dehumidify_above: int | None = Field(None, ge=30, le=100)
    # Only adjust units that are already running; never switch one on or off.
    only_when_on: bool = False
    # Leave a unit alone for this long after someone changes it by hand.
    manual_override_minutes: int = Field(120, ge=0)
    # Don't change a unit's mode more often than this (avoids short-cycling).
    min_change_minutes: int = Field(20, ge=5)

    @model_validator(mode="after")
    def _band(self) -> AutoClimate:
        if self.comfort_high <= self.comfort_low:
            raise ValueError("comfort_high must be above comfort_low")
        return self


class AutoRoutine(RoutineBase):
    """Hands-off automation driven by live weather and the sun."""
    kind: Literal["auto"] = "auto"
    lighting: AutoLighting | None = None
    climate: AutoClimate | None = None

    @model_validator(mode="after")
    def _something(self) -> AutoRoutine:
        if self.lighting is None and self.climate is None:
            raise ValueError("turn on automatic lighting and/or climate")
        return self


Routine = Annotated[Union[EventRoutine, CurveRoutine, AutoRoutine], Field(discriminator="kind")]
