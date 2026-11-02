"""Build a single-file SmartHome executable with PyInstaller.

    pip install -e '.[all,exe]'
    python packaging/build_exe.py

The result is dist/SmartHome.exe on Windows (dist/SmartHome elsewhere). PyInstaller
builds for the system it runs on, so build the .exe on Windows. The GitHub Actions
workflow in .github/workflows/build-exe.yml does this automatically.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent


def data(src: str, dest: str) -> str:
    return f"{ROOT / src}{os.pathsep}{dest}"


args = [
    str(ROOT / "packaging" / "launch.py"),
    "--name", "SmartHome",
    "--onefile",
    "--console",  # the window shows the hub is running; closing it stops the hub
    "--noconfirm",
    "--clean",
    "--paths", str(ROOT),  # find the hub package even when it isn't pip-installed
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    "--add-data", data("hub/static", "hub/static"),
    "--add-data", data("config.example.yaml", "."),
    "--collect-submodules", "hub",
    # Imported dynamically (uvicorn picks its protocol/loop modules at runtime).
    "--collect-submodules", "uvicorn",
    "--collect-data", "tzdata",
    "--collect-data", "astral",
]
# Vendor libraries are imported lazily inside the drivers, so PyInstaller can't see them; add the
# ones that are installed.
for module in ("pycync", "aiohttp", "msmart", "roombapy", "aiomqtt", "paho", "orjson", "websockets"):
    try:
        __import__(module)
    except ImportError:
        print(f"note: {module} not installed, leaving it out", file=sys.stderr)
        continue
    args += ["--collect-all", module]

icon = ROOT / "packaging" / "icon.ico"
if icon.exists():
    args += ["--icon", str(icon)]

PyInstaller.__main__.run(args)
