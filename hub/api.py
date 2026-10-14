"""HTTP + WebSocket API, and the static dashboard."""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .drivers.base import IntegrationError
from .hub import Hub
from .models import Device
from .routines.engine import RoutineAdapter
from .routines.models import CurveRoutine

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class DeviceInfo(BaseModel):
    name: str | None = None
    room: str | None = None
    hidden: bool | None = None


class CommandBody(BaseModel):
    command: str
    params: dict[str, Any] | None = None


def create_app(hub: Hub) -> FastAPI:
    sockets: set[WebSocket] = set()

    def broadcast(device: Device) -> None:
        msg = {"type": "device", "device": device.to_dict()}
        for ws in list(sockets):
            asyncio.create_task(_send(ws, msg))

    async def _send(ws: WebSocket, msg: dict[str, Any]) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            sockets.discard(ws)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        unsubscribe = hub.subscribe(broadcast)
        yield
        unsubscribe()
        await hub.stop()

    app = FastAPI(title="Smart Home Hub", lifespan=lifespan)

    password = hub.config.get("password")

    def authorized(header: str) -> bool:
        if not password:
            return True
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        return secrets.compare_digest(given.encode(), str(password).encode())

    if password:
        # Optional HTTP Basic auth (any username) so other people on the network can't
        # flip your lights. Browsers prompt once and reuse it for the WebSocket too.
        @app.middleware("http")
        async def basic_auth(request: Request, call_next: Any) -> Any:
            if not authorized(request.headers.get("authorization", "")):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Smart Home"'})
            return await call_next(request)

    def device_or_404(device_id: str) -> Device:
        try:
            return hub.get(device_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None

    async def guarded(coro: Any) -> Any:
        try:
            return await coro
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        except IntegrationError as e:
            raise HTTPException(400, str(e)) from None
        except Exception as e:
            log.exception("device call failed")
            raise HTTPException(502, f"Device didn't respond: {e}") from None

    # ---- overview ----------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        sun = hub.clock.sun_times(hub.clock.now().date())
        return {
            "now": hub.clock.now().isoformat(timespec="seconds"),
            "timezone": hub.clock.tz.key,
            "temperature_unit": hub.temperature_unit,
            "location_configured": hub.clock.location is not None,
            "sun": {k: v.isoformat(timespec="minutes") for k, v in sun.items()},
            "integrations": [
                {"id": i.id, "type": i.type, "devices": len(i.devices), "error": i.error}
                for i in hub.integrations.values()
            ],
            "rooms": hub.rooms(),
        }

    # ---- devices -----------------------------------------------------------

    @app.get("/api/devices")
    def list_devices() -> list[dict[str, Any]]:
        return [d.to_dict() for d in hub.devices.values()]

    @app.get("/api/devices/{device_id}")
    def get_device(device_id: str) -> dict[str, Any]:
        return device_or_404(device_id).to_dict()

    @app.post("/api/devices/{device_id}/state")
    async def set_state(device_id: str, changes: dict[str, Any] = Body(...)) -> dict[str, Any]:
        device = await guarded(hub.set_state(device_id, changes))
        return device.to_dict()

    @app.post("/api/devices/{device_id}/command")
    async def command(device_id: str, body: CommandBody) -> dict[str, Any]:
        device = await guarded(hub.command(device_id, body.command, body.params))
        return device.to_dict()

    @app.patch("/api/devices/{device_id}")
    async def update_device(device_id: str, info: DeviceInfo) -> dict[str, Any]:
        device_or_404(device_id)
        return hub.update_device_info(device_id, **info.model_dump()).to_dict()

    @app.post("/api/rooms/{room}/state")
    async def set_room(room: str, changes: dict[str, Any] = Body(...)) -> dict[str, Any]:
        devices = [d for d in hub.devices.values() if d.room == room and d.accepts(changes)]
        results = await asyncio.gather(*(hub.set_state(d.id, d.accepts(changes)) for d in devices),
                                       return_exceptions=True)
        errors = [str(r) for r in results if isinstance(r, Exception)]
        return {"updated": len(devices) - len(errors), "errors": errors}

    # ---- routines ----------------------------------------------------------

    def routine_dict(r: Any) -> dict[str, Any]:
        return {**r.model_dump(mode="json"), "last_run": hub.routines.last_run.get(r.id),
                "running": r.id in hub.routines._running}

    def parse_routine(raw: dict[str, Any]) -> Any:
        try:
            return RoutineAdapter.validate_python(raw)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None

    @app.get("/api/routines")
    def list_routines() -> list[dict[str, Any]]:
        return [routine_dict(r) for r in hub.routines.routines.values()]

    @app.post("/api/routines")
    async def create_routine(raw: dict[str, Any] = Body(...)) -> dict[str, Any]:
        r = parse_routine(raw)
        if r.id in hub.routines.routines:
            raise HTTPException(409, f"A routine with id {r.id!r} already exists")
        hub.routines.upsert(r)
        return routine_dict(r)

    @app.put("/api/routines/{routine_id}")
    async def replace_routine(routine_id: str, raw: dict[str, Any] = Body(...)) -> dict[str, Any]:
        r = parse_routine({**raw, "id": routine_id})
        hub.routines.upsert(r)
        return routine_dict(r)

    @app.delete("/api/routines/{routine_id}")
    async def delete_routine(routine_id: str) -> dict[str, bool]:
        if routine_id not in hub.routines.routines:
            raise HTTPException(404, "No such routine")
        hub.routines.delete(routine_id)
        return {"ok": True}

    @app.post("/api/routines/{routine_id}/run")
    async def run_routine(routine_id: str) -> dict[str, bool]:
        r = hub.routines.routines.get(routine_id)
        if r is None:
            raise HTTPException(404, "No such routine")
        hub.routines.run(r)
        return {"ok": True}

    @app.post("/api/routines/{routine_id}/stop")
    async def stop_routine(routine_id: str) -> dict[str, bool]:
        hub.routines.cancel(routine_id)
        return {"ok": True}

    @app.post("/api/curve/preview")
    def preview_curve(raw: dict[str, Any] = Body(...)) -> list[dict[str, Any]]:
        r = parse_routine({"id": "preview", "name": "preview", "target": {"all": True}, **raw, "kind": "curve"})
        assert isinstance(r, CurveRoutine)
        return hub.routines.preview(r)

    # ---- live updates ------------------------------------------------------

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        if not authorized(websocket.headers.get("authorization", "")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        sockets.add(websocket)
        try:
            while True:
                await websocket.receive_text()  # keepalive pings from the page
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(websocket)

    # ---- dashboard ---------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
