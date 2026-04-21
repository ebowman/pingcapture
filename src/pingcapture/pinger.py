"""Async ping/TCP probes. Writes results to the storage layer.

ICMP via icmplib (unprivileged on macOS — uses SOCK_DGRAM on darwin).
TCP via asyncio.open_connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from itertools import cycle
from typing import Protocol

from icmplib import async_ping  # type: ignore[import-untyped]

from .config import Config, PingTarget
from .storage import PingResult, utc_now

log = logging.getLogger(__name__)

# A probe function returns the result for a single attempt.
ProbeFn = Callable[[PingTarget], Awaitable[PingResult]]


class ResultSink(Protocol):
    def insert_ping(self, r: PingResult) -> None:
        ...


async def icmp_probe(target: PingTarget, timeout_s: float = 2.0) -> PingResult:
    try:
        host = await async_ping(
            target.host, count=1, timeout=timeout_s, privileged=False
        )
        if host.is_alive and host.avg_rtt > 0:
            return PingResult(
                ts=utc_now(),
                target=target.host,
                label=target.label,
                kind="icmp",
                success=True,
                latency_ms=host.avg_rtt,
                error=None,
            )
        return PingResult(
            ts=utc_now(),
            target=target.host,
            label=target.label,
            kind="icmp",
            success=False,
            latency_ms=None,
            error="no reply" if not host.is_alive else "zero rtt",
        )
    except Exception as e:  # noqa: BLE001 — we want to record any failure
        return PingResult(
            ts=utc_now(),
            target=target.host,
            label=target.label,
            kind="icmp",
            success=False,
            latency_ms=None,
            error=f"{type(e).__name__}: {e}",
        )


async def tcp_probe(target: PingTarget, port: int, timeout_s: float = 3.0) -> PingResult:
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.host, port), timeout=timeout_s
        )
        elapsed_ms = (loop.time() - start) * 1000.0
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        del reader  # silence linters
        return PingResult(
            ts=utc_now(),
            target=target.host,
            label=target.label,
            kind="tcp",
            success=True,
            latency_ms=elapsed_ms,
            error=None,
        )
    except TimeoutError:
        return PingResult(
            ts=utc_now(),
            target=target.host,
            label=target.label,
            kind="tcp",
            success=False,
            latency_ms=None,
            error="timeout",
        )
    except Exception as e:  # noqa: BLE001
        return PingResult(
            ts=utc_now(),
            target=target.host,
            label=target.label,
            kind="tcp",
            success=False,
            latency_ms=None,
            error=f"{type(e).__name__}: {e}",
        )


async def run_rotating_probe(
    targets: list[PingTarget],
    interval_s: float,
    sink: ResultSink,
    probe: ProbeFn,
    *,
    stop: asyncio.Event,
    name: str,
) -> None:
    """Rotate through ``targets`` at ``interval_s`` per probe, recording results."""
    if not targets:
        log.warning("%s: no targets configured, exiting", name)
        return
    targets_cycle = cycle(targets)
    log.info("%s loop started (%d targets, %.1fs interval)", name, len(targets), interval_s)
    while not stop.is_set():
        target = next(targets_cycle)
        try:
            result = await probe(target)
            sink.insert_ping(result)
            if not result.success:
                log.warning("%s fail %s: %s", name, target.host, result.error)
        except Exception as e:  # noqa: BLE001 — never let probe loop die
            log.exception("%s loop error for %s: %s", name, target.host, e)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
    log.info("%s loop stopped", name)


async def run_pinger(cfg: Config, sink: ResultSink, stop: asyncio.Event) -> None:
    """Run ICMP + TCP loops concurrently until ``stop`` is set."""

    async def _icmp(target: PingTarget) -> PingResult:
        return await icmp_probe(target, timeout_s=cfg.icmp_timeout_s)

    async def _tcp(target: PingTarget) -> PingResult:
        return await tcp_probe(target, port=cfg.tcp_port, timeout_s=cfg.tcp_timeout_s)

    tasks = [
        asyncio.create_task(
            run_rotating_probe(
                cfg.icmp_targets, cfg.icmp_interval_s, sink, _icmp, stop=stop, name="icmp"
            )
        ),
        asyncio.create_task(
            run_rotating_probe(
                cfg.tcp_targets, cfg.tcp_interval_s, sink, _tcp, stop=stop, name="tcp"
            )
        ),
    ]
    await asyncio.gather(*tasks)


__all__ = ["icmp_probe", "run_pinger", "run_rotating_probe", "tcp_probe"]
