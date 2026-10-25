"""Explicit launchd/systemd installation. No elevated system-wide services."""

from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

from tracebook.config import Config
from tracebook.model import Diff
from tracebook.platform import atomic_write


def install_service(config: Config, dry_run: bool = True) -> Diff:
    executable = str(Path(sys.executable).parent / "tracebook")
    if sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents/local.tracebook.plist"
        body = plistlib.dumps(
            {
                "Label": "local.tracebook",
                "ProgramArguments": [executable, "--home", str(config.root), "daemon"],
                "RunAtLoad": True,
                "KeepAlive": True,
                "StandardOutPath": str(config.root / "daemon.log"),
                "StandardErrorPath": str(config.root / "daemon.log"),
                "ProcessType": "Background",
                "ThrottleInterval": 10,
            }
        ).decode()
    elif sys.platform.startswith("linux"):
        path = Path.home() / ".config/systemd/user/tracebook.service"
        body = (
            "[Unit]\nDescription=Tracebook local diagnostics\n\n[Service]\nType=simple\nExecStart="
            + shlex.join([executable, "--home", str(config.root), "daemon"])
            + "\nRestart=on-failure\nRestartSec=10\nUMask=0077\n\n[Install]\nWantedBy=default.target\n"
        )
    else:
        return Diff(
            "", "Use tracebook daemon with your user-level process supervisor on this OS.", False
        )
    before = path.read_text() if path.exists() else ""
    if before != body and not dry_run:
        atomic_write(path, body)
    return Diff(str(path), body, before != body)
