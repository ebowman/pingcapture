"""Configuration loading and defaults."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    return Path(
        os.environ.get(
            "PINGCAPTURE_DATA_DIR",
            Path.home() / "Library" / "Application Support" / "pingcapture",
        )
    )


def default_config_path() -> Path:
    return Path(
        os.environ.get(
            "PINGCAPTURE_CONFIG",
            Path.home() / ".config" / "pingcapture" / "config.toml",
        )
    )


@dataclass(frozen=True)
class PingTarget:
    host: str
    label: str


@dataclass(frozen=True)
class Config:
    db_path: Path
    icmp_targets: list[PingTarget]
    icmp_interval_s: float
    icmp_timeout_s: float
    tcp_targets: list[PingTarget]
    tcp_port: int
    tcp_interval_s: float
    tcp_timeout_s: float
    mtr_targets: list[PingTarget]
    mtr_interval_s: float
    mtr_cycles: int
    web_host: str
    web_port: int

    @staticmethod
    def defaults(data_dir: Path | None = None) -> Config:
        data = data_dir or default_data_dir()
        return Config(
            db_path=data / "pingcapture.sqlite",
            icmp_targets=[
                PingTarget("1.1.1.1", "Cloudflare"),
                PingTarget("8.8.8.8", "Google"),
                PingTarget("9.9.9.9", "Quad9"),
                PingTarget("cloudflare.com", "cloudflare.com (DNS sanity)"),
            ],
            icmp_interval_s=5.0,
            icmp_timeout_s=2.0,
            tcp_targets=[
                PingTarget("1.1.1.1", "Cloudflare"),
                PingTarget("8.8.8.8", "Google"),
            ],
            tcp_port=443,
            tcp_interval_s=30.0,
            tcp_timeout_s=3.0,
            mtr_targets=[
                PingTarget("1.1.1.1", "Cloudflare"),
                PingTarget("8.8.8.8", "Google"),
            ],
            mtr_interval_s=900.0,
            mtr_cycles=10,
            web_host="127.0.0.1",
            web_port=8765,
        )

    @staticmethod
    def load(path: Path | None = None) -> Config:
        cfg_path = path or default_config_path()
        defaults = Config.defaults()
        if not cfg_path.exists():
            return defaults
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
        return Config(
            db_path=Path(data.get("db_path", defaults.db_path)),
            icmp_targets=_parse_targets(data.get("icmp_targets"), defaults.icmp_targets),
            icmp_interval_s=float(data.get("icmp_interval_s", defaults.icmp_interval_s)),
            icmp_timeout_s=float(data.get("icmp_timeout_s", defaults.icmp_timeout_s)),
            tcp_targets=_parse_targets(data.get("tcp_targets"), defaults.tcp_targets),
            tcp_port=int(data.get("tcp_port", defaults.tcp_port)),
            tcp_interval_s=float(data.get("tcp_interval_s", defaults.tcp_interval_s)),
            tcp_timeout_s=float(data.get("tcp_timeout_s", defaults.tcp_timeout_s)),
            mtr_targets=_parse_targets(data.get("mtr_targets"), defaults.mtr_targets),
            mtr_interval_s=float(data.get("mtr_interval_s", defaults.mtr_interval_s)),
            mtr_cycles=int(data.get("mtr_cycles", defaults.mtr_cycles)),
            web_host=str(data.get("web_host", defaults.web_host)),
            web_port=int(data.get("web_port", defaults.web_port)),
        )


def _parse_targets(
    raw: list[dict[str, str]] | None, fallback: list[PingTarget]
) -> list[PingTarget]:
    if not raw:
        return fallback
    return [PingTarget(host=t["host"], label=t.get("label", t["host"])) for t in raw]


def port_is_free(host: str, port: int) -> bool:
    """Return True if (host, port) can be bound right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def pick_free_port(host: str, start: int, *, attempts: int = 50) -> int | None:
    """Return the first free port at-or-after ``start``, or None if none found.

    Scans ``start, start+1, ..., start+attempts-1``. The 50-port window is
    enough to skip past a small cluster of common dev servers without wandering
    far from the documented default.
    """
    for offset in range(attempts):
        candidate = start + offset
        if candidate > 65535:
            return None
        if port_is_free(host, candidate):
            return candidate
    return None


DEFAULT_CONFIG_TOML = """\
# pingcapture configuration. All fields optional; defaults are sensible.

# db_path = "~/Library/Application Support/pingcapture/pingcapture.sqlite"

icmp_interval_s = 5.0
icmp_timeout_s  = 2.0
tcp_interval_s  = 30.0
tcp_timeout_s   = 3.0
tcp_port        = 443
mtr_interval_s  = 900.0
mtr_cycles      = 10

web_host = "127.0.0.1"
# Local-only dashboard port. Change if 8765 conflicts.
# Pick anything 1024-65535 that's not in use. Common collisions:
# 3000/8000/8080 (dev servers), 5000 (macOS AirPlay receiver).
web_port = 8765

[[icmp_targets]]
host  = "1.1.1.1"
label = "Cloudflare"

[[icmp_targets]]
host  = "8.8.8.8"
label = "Google"

[[icmp_targets]]
host  = "9.9.9.9"
label = "Quad9"

[[icmp_targets]]
host  = "cloudflare.com"
label = "cloudflare.com (DNS sanity)"

[[tcp_targets]]
host  = "1.1.1.1"
label = "Cloudflare"

[[tcp_targets]]
host  = "8.8.8.8"
label = "Google"

[[mtr_targets]]
host  = "1.1.1.1"
label = "Cloudflare"

[[mtr_targets]]
host  = "8.8.8.8"
label = "Google"
"""
