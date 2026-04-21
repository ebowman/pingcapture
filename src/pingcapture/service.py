"""launchd service install/uninstall/status helpers (macOS)."""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "ie.boboco.pingcapture"


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return _agents_dir() / f"{LABEL}.plist"


def logs_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "pingcapture"


@dataclass(frozen=True)
class ServiceStatus:
    installed: bool
    loaded: bool
    pid: int | None
    last_exit: int | None


def _bin_path() -> Path:
    """Path to the pingcapture executable in the current Python env."""
    candidate = Path(sys.executable).parent / "pingcapture"
    if candidate.exists():
        return candidate
    # Fallback: try `python -m pingcapture.cli`
    return Path(sys.executable)


def _build_plist() -> dict[str, object]:
    bin_p = _bin_path()
    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    program_args: list[str]
    if bin_p.name == "pingcapture":
        program_args = [str(bin_p), "run"]
    else:
        program_args = [str(bin_p), "-m", "pingcapture.cli", "run"]
    return {
        "Label": LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "pingcapture.out.log"),
        "StandardErrorPath": str(log_dir / "pingcapture.err.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
            ),
        },
        "ProcessType": "Background",
    }


def install() -> Path:
    _agents_dir().mkdir(parents=True, exist_ok=True)
    p = plist_path()
    with p.open("wb") as fh:
        plistlib.dump(_build_plist(), fh)
    # Bootstrap into the current GUI session.
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(p)],
        check=False,
    )
    subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{LABEL}"], check=False)
    return p


def uninstall() -> None:
    p = plist_path()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        check=False,
    )
    if p.exists():
        p.unlink()


def status() -> ServiceStatus:
    p = plist_path()
    installed = p.exists()
    res = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    loaded = res.returncode == 0
    pid: int | None = None
    last_exit: int | None = None
    for line in res.stdout.splitlines():
        ls = line.strip()
        if ls.startswith("pid = "):
            with contextlib.suppress(ValueError):
                pid = int(ls.split("=", 1)[1].strip())
        elif ls.startswith("last exit code = "):
            val = ls.split("=", 1)[1].strip()
            try:
                last_exit = int(val)
            except ValueError:
                last_exit = None
    return ServiceStatus(installed=installed, loaded=loaded, pid=pid, last_exit=last_exit)


__all__ = ["LABEL", "ServiceStatus", "install", "logs_dir", "plist_path", "status", "uninstall"]
