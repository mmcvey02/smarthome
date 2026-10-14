"""Command line: `smarthome` (serve the dashboard) and `smarthome cync-login`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(prog="smarthome", description="Smart home hub")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--data", type=Path, default=Path("data"), help="where routines and tokens are stored")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="run the hub and dashboard (default)")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)

    login = sub.add_parser("cync-login", help="sign in to GE Cync once (asks for the emailed code)")
    login.add_argument("--integration", default="cync", help="integration id in config.yaml")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "cync-login":
        from .drivers.cync import interactive_login, token_path
        config = yaml.safe_load(args.config.read_text()) if args.config.exists() else {}
        spec = next((i for i in config.get("integrations", [])
                     if i.get("type") == "cync" and i.get("id", "cync") == args.integration), {})
        username = spec.get("username") or input("Cync email: ")
        password = spec.get("password") or getpass.getpass("Cync password: ")
        args.data.mkdir(parents=True, exist_ok=True)
        out = token_path(args.data, args.integration)
        asyncio.run(interactive_login(username, password, out))
        print(f"Signed in. Token saved to {out}; start the hub with `smarthome`.")
        return

    import uvicorn

    from .api import create_app
    from .hub import Hub

    hub = Hub.from_file(args.config, args.data)
    uvicorn.run(create_app(hub), host=getattr(args, "host", "0.0.0.0"), port=getattr(args, "port", 8080))


if __name__ == "__main__":
    main()
