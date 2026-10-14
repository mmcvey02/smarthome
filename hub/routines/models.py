"""Routine definitions. Two kinds:

* event  - "when X happens (and conditions hold), do these actions in order".
           Triggers: a clock/sun time, or a device changing state.
           Actions: set state, send a command, fade, wait, run another routine.
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


Trigger = Annotated[Union[TimeTrigger, DeviceTrigger], Field(discriminator="type")]


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


Routine = Annotated[Union[EventRoutine, CurveRoutine], Field(discriminator="kind")]
