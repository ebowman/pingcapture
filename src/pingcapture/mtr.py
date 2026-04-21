"""mtr scheduler. Shells out to ``mtr --json`` and parses the result."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from typing import Any

from .config import Config, PingTarget
from .storage import MtrHop, MtrRun, Store, utc_now

log = logging.getLogger(__name__)


def mtr_available() -> bool:
    return shutil.which("mtr") is not None


async def run_mtr_once(target: PingTarget, cycles: int = 10, timeout_s: float = 60.0) -> MtrRun:
    """Run a single mtr report. Raises RuntimeError on failure."""
    cmd = [
        "mtr",
        "--report",
        "--report-cycles",
        str(cycles),
        "--json",
        "--no-dns",
        "--show-ips",
        target.host,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"mtr timeout after {timeout_s}s for {target.host}") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"mtr exited {proc.returncode} for {target.host}: {stderr.decode(errors='replace')}"
        )
    return parse_mtr_json(stdout.decode(), target=target)


def parse_mtr_json(payload: str, *, target: PingTarget) -> MtrRun:
    """Parse mtr's JSON output into an MtrRun."""
    data: dict[str, Any] = json.loads(payload)
    raw_hubs = data.get("report", {}).get("hubs", [])
    hops: list[MtrHop] = []
    for hub in raw_hubs:
        # mtr --show-ips puts IP in 'host' as 'name (ip)' OR just IP.
        host = hub.get("host")
        ip: str | None = None
        if host and "(" in host and host.endswith(")"):
            name, ip_part = host.rsplit("(", 1)
            host = name.strip() or None
            ip = ip_part.rstrip(")").strip()
        elif host and host.replace(".", "").replace(":", "").isalnum():
            ip = host  # bare IP / hex
        hops.append(
            MtrHop(
                hop_idx=int(hub.get("count", len(hops) + 1)),
                host=host,
                ip=ip,
                loss_pct=float(hub.get("Loss%", 0.0)),
                sent=int(hub.get("Snt", 0)),
                last_ms=_opt_float(hub.get("Last")),
                avg_ms=_opt_float(hub.get("Avg")),
                best_ms=_opt_float(hub.get("Best")),
                worst_ms=_opt_float(hub.get("Wrst")),
                stddev_ms=_opt_float(hub.get("StDev")),
            )
        )
    return MtrRun(ts=utc_now(), target=target.host, label=target.label, hops=hops)


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


_SETUID_HINT = (
    "mtr-packet cannot open raw sockets. On macOS run:\n"
    "    sudo chown root:wheel $(which mtr-packet) && sudo chmod u+s $(which mtr-packet)\n"
    "Then restart pingcapture. mtr scheduler will exit until this is fixed."
)


async def run_mtr_scheduler(cfg: Config, store: Store, stop: asyncio.Event) -> None:
    if not mtr_available():
        log.warning("mtr not installed, skipping mtr scheduler")
        return
    if not cfg.mtr_targets:
        log.warning("mtr scheduler: no targets configured, exiting")
        return
    log.info(
        "mtr scheduler started (%d targets, %.0fs interval, %d cycles)",
        len(cfg.mtr_targets),
        cfg.mtr_interval_s,
        cfg.mtr_cycles,
    )
    # Run immediately on start, then on the configured interval.
    while not stop.is_set():
        for target in cfg.mtr_targets:
            if stop.is_set():
                break
            try:
                run = await run_mtr_once(target, cycles=cfg.mtr_cycles)
                store.insert_mtr_run(run)
                log.info("mtr ok %s (%d hops)", target.host, len(run.hops))
            except RuntimeError as e:
                msg = str(e)
                if "Failure to open IPv4 sockets" in msg or "Failure to start mtr-packet" in msg:
                    log.error("mtr scheduler exiting: %s", _SETUID_HINT)
                    return
                log.warning("mtr failed for %s: %s", target.host, e)
            except Exception as e:  # noqa: BLE001
                log.warning("mtr failed for %s: %s", target.host, e)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=cfg.mtr_interval_s)
    log.info("mtr scheduler stopped")


__all__ = ["mtr_available", "parse_mtr_json", "run_mtr_once", "run_mtr_scheduler"]
