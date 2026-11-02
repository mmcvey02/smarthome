"""Self-updating for the Windows SmartHome.exe.

Every CI build publishes SmartHome.exe plus a small version.json (build number,
commit, SHA-256, size) on the `exe-latest` GitHub release. The running exe checks
version.json every few hours; when a newer build is out it:

1. downloads it next to itself as SmartHome.exe.new and checks size + SHA-256,
2. runs `SmartHome.exe.new --version` to make sure it actually starts,
3. waits until no routine is mid-run (so a wake-up fade isn't cut short),
4. renames itself to SmartHome.exe.old (Windows allows renaming a running exe),
   moves the new file into place, shuts the hub down and starts the new exe.

The new exe deletes SmartHome.exe.old once the old process has exited. Config, data
and routines live beside the exe and are untouched. Running from source, updates are
off (use git pull).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger(__name__)

DEFAULT_URL = "https://github.com/mmcvey02/smarthome/releases/download/exe-latest/"
FIRST_CHECK_SECONDS = 60
RETRY_IDLE_SECONDS = 60


def current_build() -> dict[str, Any]:
    """Build info baked into the exe by packaging/build_exe.py; number 0 when running from source."""
    try:
        from ._build import BUILD  # type: ignore[import-not-found]
        return dict(BUILD)
    except ImportError:
        return {"number": 0, "sha": "source", "date": None}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, hub: Hub, exe_path: Path | None, request_restart: Callable[[], None],
                 config: dict[str, Any] | None = None):
        config = config or {}
        self.hub = hub
        self.exe_path = exe_path  # None when not running as an updatable exe
        self.request_restart = request_restart
        self.url = os.environ.get("SMARTHOME_UPDATE_URL") or config.get("url") or DEFAULT_URL
        if not self.url.endswith("/"):
            self.url += "/"
        self.check_hours = float(config.get("check_hours", 6))
        self._settings_path = hub.data_dir / "updates.json"
        saved = self._load_settings()
        self.auto: bool = saved.get("auto", config.get("auto", True))
        self.current = current_build()
        self.latest: dict[str, Any] | None = None
        self.status = "idle" if self.supported else "unsupported"
        self.error: str | None = None
        self.last_check: str | None = None
        self.pending: Path | None = None  # verified download waiting to be installed
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ---- settings ------------------------------------------------------------

    @property
    def supported(self) -> bool:
        return self.exe_path is not None

    def _load_settings(self) -> dict[str, Any]:
        try:
            return json.loads(self._settings_path.read_text())
        except (OSError, ValueError):
            return {}

    def set_auto(self, auto: bool) -> None:
        self.auto = auto
        self._settings_path.write_text(json.dumps({"auto": auto}))

    @property
    def available(self) -> bool:
        return bool(self.latest) and int(self.latest.get("number", 0)) > int(self.current.get("number", 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported, "auto": self.auto, "status": self.status, "error": self.error,
            "current": self.current, "latest": self.latest, "available": self.available,
            "last_check": self.last_check, "check_hours": self.check_hours,
        }

    # ---- checking & downloading ---------------------------------------------

    async def _get(self, client: httpx.AsyncClient, name: str) -> httpx.Response:
        res = await client.get(self.url + name, params={"t": int(time.time())})  # skip stale caches
        res.raise_for_status()
        return res

    async def check(self) -> dict[str, Any] | None:
        self.status, self.error = "checking", None
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                info = (await self._get(client, "version.json")).json()
            if not {"number", "sha256", "size", "file"} <= set(info):
                raise UpdateError("version.json is missing fields")
            self.latest = info
            self.status = "idle"
        except Exception as e:
            self.status, self.error = "error", f"Couldn't check for updates: {e}"
            log.warning(self.error)
        self.last_check = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self.latest

    async def download(self) -> Path:
        assert self.exe_path and self.latest
        info = self.latest
        new = self.exe_path.with_name(self.exe_path.name + ".new")
        self.status = "downloading"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as client:
            async with client.stream("GET", self.url + info["file"]) as res:
                res.raise_for_status()
                with new.open("wb") as f:
                    async for chunk in res.aiter_bytes(1 << 16):
                        f.write(chunk)
        if new.stat().st_size != int(info["size"]) or sha256_of(new) != info["sha256"]:
            new.unlink(missing_ok=True)
            raise UpdateError("the download was incomplete or corrupted; will retry later")
        if os.name != "nt":
            new.chmod(0o755)
        await asyncio.to_thread(self._verify_runs, new, info)
        return new

    @staticmethod
    def _verify_runs(new: Path, info: dict[str, Any]) -> None:
        """Start the new exe with --version: proves it isn't broken before we replace ourselves."""
        try:
            out = subprocess.run([str(new), "--version"], capture_output=True, text=True, timeout=120,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env())
            reported = json.loads(out.stdout.strip().splitlines()[-1])
        except Exception as e:
            new.unlink(missing_ok=True)
            raise UpdateError(f"the new version failed to start ({e})") from e
        if int(reported.get("number", -1)) != int(info["number"]):
            new.unlink(missing_ok=True)
            raise UpdateError("the new version reported an unexpected build number")

    # ---- installing ----------------------------------------------------------

    def busy(self) -> bool:
        """True while a routine is mid-run; restarting then would cut it short."""
        return bool(self.hub.routines._running)

    def swap(self, new: Path) -> None:
        """Put the new exe in place of the running one."""
        assert self.exe_path
        old = self.exe_path.with_name(self.exe_path.name + ".old")
        if old.exists():
            old.unlink()  # left over from an earlier update
        self.exe_path.rename(old)
        try:
            new.rename(self.exe_path)
        except OSError:
            old.rename(self.exe_path)  # put things back
            raise

    async def install(self, force: bool = False) -> bool:
        """Download (if needed) and switch to the latest build. Returns True if a restart was requested."""
        if not self.supported:
            raise UpdateError("Updates only work in the Windows SmartHome.exe. From source, use git pull.")
        async with self._lock:
            try:
                if self.latest is None or not self.pending:
                    await self.check()
                    if self.status == "error":
                        return False
                if not self.available:
                    self.status = "idle"
                    return False
                if self.pending is None or not self.pending.exists():
                    self.pending = await self.download()
                if self.busy() and not force:
                    self.status = "ready"  # installs once routines finish
                    return False
                self.status = "installing"
                self.swap(self.pending)
                self.pending = None
                log.info("Updating to build %s; restarting", self.latest["number"])
                self.request_restart()
                return True
            except Exception as e:
                self.status, self.error = "error", f"Update failed: {e}"
                log.warning(self.error)
                return False

    # ---- background loop -----------------------------------------------------

    def start(self) -> None:
        if self.supported:
            self._task = asyncio.create_task(self._loop())
            asyncio.create_task(asyncio.to_thread(cleanup_old, self.exe_path))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK_SECONDS)
        while True:
            if self.auto:
                await self.install()
            else:
                await self.check()
            if self.status == "ready":
                delay = RETRY_IDLE_SECONDS  # downloaded, waiting for routines to finish
            elif self.status == "error":
                delay = 30 * 60  # offline or a bad download: try again in a while
            else:
                delay = self.check_hours * 3600
            await asyncio.sleep(delay)


def cleanup_old(exe_path: Path | None, attempts: int = 30) -> None:
    """Delete SmartHome.exe.old once the previous process has let go of it."""
    if exe_path is None:
        return
    old = exe_path.with_name(exe_path.name + ".old")
    for _ in range(attempts):
        if not old.exists():
            return
        try:
            old.unlink()
            return
        except OSError:
            time.sleep(2)


def child_env() -> dict[str, str]:
    """Environment for launching another copy of the exe.

    Without the reset, the new copy inherits PyInstaller's runtime variables and tries to use
    this process's temporary unpack folder, which vanishes when this process exits.
    """
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def restart_command(exe_path: Path, argv: list[str]) -> list[str]:
    """Command line for the new exe: same options, no new browser tab, wait for the port."""
    args = [a for a in argv if a not in ("--after-update", "--no-browser")]
    return [str(exe_path), *args, "--no-browser", "--after-update"]


def spawn(cmd: list[str]) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, env=child_env())
