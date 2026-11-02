"""The self-updater, end to end against a local web server and stand-in executables."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import http.server
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from hub import updater as updater_mod
from hub.updater import Updater, cleanup_old, restart_command

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stand-in executables are shell scripts")


def fake_exe(path: Path, number: int, broken: bool = False) -> bytes:
    """A tiny program that answers --version like SmartHome.exe does."""
    body = "exit 3" if broken else f"echo '{json.dumps({'number': number, 'sha': f'sha{number}'})}'"
    content = f"#!/bin/sh\n{body}\n".encode()
    path.write_bytes(content)
    path.chmod(0o755)
    return content


@pytest.fixture
def release(tmp_path):
    """A local stand-in for the GitHub release: serves version.json and the new exe."""
    root = tmp_path / "release"
    root.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def publish(number: int, *, broken: bool = False, corrupt: bool = False) -> None:
        content = fake_exe(root / "SmartHome.exe", number, broken)
        (root / "version.json").write_text(json.dumps({
            "number": number, "sha": f"sha{number}", "file": "SmartHome.exe", "size": len(content),
            "sha256": "0" * 64 if corrupt else hashlib.sha256(content).hexdigest(), "notes": "Better things"}))

    publish.url = f"http://127.0.0.1:{server.server_port}/"
    yield publish
    server.shutdown()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """The currently running exe: build 5."""
    app = tmp_path / "app"
    app.mkdir()
    exe = app / "SmartHome.exe"
    fake_exe(exe, 5)
    monkeypatch.setattr(updater_mod, "current_build", lambda: {"number": 5, "sha": "sha5"})
    return exe


def make_updater(hub, exe, url, restarts):
    return Updater(hub, exe, lambda: restarts.append(True), {"url": url})


def test_update_downloads_verifies_swaps_and_restarts(make_hub, release, installed):
    async def go():
        hub = await make_hub()
        restarts = []
        up = make_updater(hub, installed, release.url, restarts)
        release(6)
        await up.check()
        assert up.available and up.latest["notes"] == "Better things"
        assert await up.install()
        assert restarts == [True]
        assert b"\"number\": 6" in installed.read_bytes()  # the new build is in place
        old = installed.with_name("SmartHome.exe.old")
        assert old.exists() and not installed.with_name("SmartHome.exe.new").exists()
        cleanup_old(installed, attempts=1)
        assert not old.exists()
    asyncio.run(go())


def test_corrupted_download_is_rejected(make_hub, release, installed):
    async def go():
        hub = await make_hub()
        restarts = []
        up = make_updater(hub, installed, release.url, restarts)
        release(6, corrupt=True)
        assert not await up.install()
        assert "corrupted" in up.error and restarts == []
        assert b"\"number\": 5" in installed.read_bytes()
        assert not installed.with_name("SmartHome.exe.new").exists()
    asyncio.run(go())


def test_new_version_that_wont_start_is_rejected(make_hub, release, installed):
    async def go():
        hub = await make_hub()
        restarts = []
        up = make_updater(hub, installed, release.url, restarts)
        release(6, broken=True)
        assert not await up.install()
        assert "failed to start" in up.error and restarts == []
        assert b"\"number\": 5" in installed.read_bytes()
    asyncio.run(go())


def test_waits_for_running_routines(make_hub, release, installed):
    async def go():
        hub = await make_hub()
        restarts = []
        up = make_updater(hub, installed, release.url, restarts)
        release(6)
        hub.routines._running["sunrise-wake-up"] = asyncio.get_running_loop().create_future()
        assert not await up.install()
        assert up.status == "ready" and restarts == []  # downloaded, waiting
        hub.routines._running.clear()
        assert await up.install()
        assert restarts == [True]
    asyncio.run(go())


def test_nothing_to_do_when_up_to_date(make_hub, release, installed):
    async def go():
        hub = await make_hub()
        up = make_updater(hub, installed, release.url, [])
        release(5)
        assert not await up.install()
        assert not up.available and up.status == "idle"
        assert not installed.with_name("SmartHome.exe.new").exists()
    asyncio.run(go())


def test_offline_is_reported_not_fatal(make_hub, installed):
    async def go():
        hub = await make_hub()
        up = make_updater(hub, installed, "http://127.0.0.1:9/", [])
        assert not await up.install()
        assert up.status == "error" and "Couldn't check" in up.error
    asyncio.run(go())


def test_auto_setting_persists(make_hub, installed):
    async def go():
        hub = await make_hub()
        up = make_updater(hub, installed, "http://x/", [])
        assert up.auto
        up.set_auto(False)
        assert not make_updater(hub, installed, "http://x/", []).auto
    asyncio.run(go())


def test_restart_command_keeps_options_and_skips_browser():
    cmd = restart_command(Path("C:/Home/SmartHome.exe"), ["--port", "9000", "--after-update"])
    assert cmd[1:] == ["--port", "9000", "--no-browser", "--after-update"]


def test_updates_unavailable_from_source(tmp_path):
    from fastapi.testclient import TestClient

    from hub.api import create_app
    from hub.hub import Hub

    from .conftest import CONFIG
    with TestClient(create_app(Hub(CONFIG, tmp_path))) as c:
        assert c.get("/api/update").json()["supported"] is False
        assert c.post("/api/update/install").status_code == 400
        assert "build" in c.get("/api/status").json()
        assert "style.css?v=" in c.get("/").text


def test_version_flag(capsys, monkeypatch):
    from hub.__main__ import main
    monkeypatch.setattr(sys, "argv", ["smarthome", "--version"])
    main()
    assert json.loads(capsys.readouterr().out)["number"] >= 0
