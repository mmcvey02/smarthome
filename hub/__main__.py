"""Command line: `smarthome` (serve the dashboard) and `smarthome cync-login`.

Also the entry point of the Windows SmartHome.exe: double-clicking it creates
config.yaml and a data folder next to the exe on first run, starts the hub and
opens the dashboard in the browser.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import yaml

FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller exe
# Keep config and data beside the exe, wherever the user put it. From source, use the current folder.
HOME = Path(sys.executable).parent if FROZEN else Path.cwd()
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

FALLBACK_CONFIG = """\
temperature_unit: F
location: auto
integrations:
  - type: demo
    id: demo
"""


def ensure_config(path: Path) -> bool:
    """Write a starter config on first run. Returns True if one was created."""
    if path.exists():
        return False
    example = BUNDLE / "config.example.yaml"
    path.write_text(example.read_text(encoding="utf-8") if example.exists() else FALLBACK_CONFIG, encoding="utf-8")
    return True


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_browser_when_ready(port: int) -> None:
    def wait_then_open() -> None:
        for _ in range(60):
            if port_in_use(port):
                webbrowser.open(f"http://localhost:{port}/")
                return
            time.sleep(0.5)
    threading.Thread(target=wait_then_open, daemon=True).start()


def wait_for_port(port: int, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while port_in_use(port) and time.monotonic() < deadline:
        time.sleep(0.5)


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app
    from .hub import Hub
    from .updater import Updater, current_build, restart_command, spawn

    if args.after_update:
        # Just updated: the previous version may still be shutting down.
        wait_for_port(args.port)
        print(f"Updated to build {current_build()['number']}.\n")
    if port_in_use(args.port):
        print(f"The hub already seems to be running; opening http://localhost:{args.port}/")
        webbrowser.open(f"http://localhost:{args.port}/")
        return
    if ensure_config(args.config):
        print(f"Created {args.config.resolve()} with demo devices.\n"
              "Edit it to add your Cync, MRCOOL and Roomba devices, then restart.\n")

    hub = Hub.from_file(args.config, args.data)
    config = uvicorn.Config(create_app(hub), host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)

    restart = False

    def request_restart() -> None:
        nonlocal restart
        restart = True
        server.should_exit = True

    # Only the Windows exe is published. A custom SMARTHOME_UPDATE_URL (used by the build's
    # self-test) means the caller serves a matching build, so allow it on any platform.
    updatable = FROZEN and (sys.platform == "win32" or bool(os.environ.get("SMARTHOME_UPDATE_URL")))
    hub.updater = Updater(hub, Path(sys.executable) if updatable else None, request_restart,
                          hub.config.get("updates") or {})

    print(f"Smart Home Hub is running: http://localhost:{args.port}/")
    print(f"Other devices on your network can use http://{local_ip()}:{args.port}/")
    if FROZEN:
        print("Keep this window open while you use it; close it to stop the hub.\n")
    if not args.no_browser:
        open_browser_when_ready(args.port)

    if sys.platform == "win32":
        # The Roomba (MQTT) client needs the selector event loop; Windows defaults to proactor.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    asyncio.run(server.serve())
    if restart:
        # The new exe is in place and the port is free: start it, then let this process end.
        spawn(restart_command(Path(sys.executable), sys.argv[1:]))


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packets are sent; just picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return "this-computer"


def cync_login(args: argparse.Namespace) -> None:
    from .drivers.cync import interactive_login, token_path

    config = yaml.safe_load(args.config.read_text()) if args.config.exists() else {}
    spec = next((i for i in (config or {}).get("integrations", [])
                 if i.get("type") == "cync" and i.get("id", "cync") == args.integration), {})
    username = spec.get("username") or input("Cync email: ")
    password = spec.get("password") or getpass.getpass("Cync password: ")
    args.data.mkdir(parents=True, exist_ok=True)
    out = token_path(args.data, args.integration)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    asyncio.run(interactive_login(username, password, out))
    print(f"Signed in. Token saved to {out}. Start the hub again to see your Cync devices.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="smarthome", description="Smart home hub")
    parser.add_argument("--config", type=Path, default=HOME / "config.yaml")
    parser.add_argument("--data", type=Path, default=HOME / "data", help="where routines and tokens are stored")
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="run the hub and dashboard (default)")
    login = sub.add_parser("cync-login", help="sign in to GE Cync once (asks for the emailed code)")
    login.add_argument("--integration", default="cync", help="integration id in config.yaml")
    for p in (parser, serve_p):  # so `smarthome --port 9000` works without typing `serve`
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=8080)
        p.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
        p.add_argument("--after-update", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="print the build number and exit")

    args = parser.parse_args()
    if args.version:
        import json

        from .updater import current_build
        print(json.dumps(current_build()))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        if args.cmd == "cync-login":
            cync_login(args)
        else:
            serve(args)
    except KeyboardInterrupt:
        pass
    except Exception:
        if not FROZEN:
            raise
        # Double-clicked exe: keep the window open so the error can be read.
        logging.exception("The hub stopped because of an error")
        input("\nPress Enter to close this window...")


if __name__ == "__main__":
    main()
