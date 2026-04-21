"""Pinger loop tests with mocked probes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from pingcapture.config import PingTarget
from pingcapture.pinger import run_rotating_probe
from pingcapture.storage import PingResult


class FakeSink:
    def __init__(self) -> None:
        self.results: list[PingResult] = []

    def insert_ping(self, r: PingResult) -> None:
        self.results.append(r)


@pytest.mark.asyncio
async def test_rotation_cycles_targets() -> None:
    sink = FakeSink()
    targets = [PingTarget("a", "A"), PingTarget("b", "B"), PingTarget("c", "C")]
    stop = asyncio.Event()

    async def probe(t: PingTarget) -> PingResult:
        return PingResult(
            ts=datetime.now(UTC), target=t.host, label=t.label,
            kind="icmp", success=True, latency_ms=1.0, error=None,
        )

    async def stopper() -> None:
        # Allow ~6 ticks at 0.01s
        await asyncio.sleep(0.07)
        stop.set()

    await asyncio.gather(
        run_rotating_probe(targets, 0.01, sink, probe, stop=stop, name="test"),
        stopper(),
    )

    assert len(sink.results) >= 3
    # First three should rotate a, b, c
    assert [r.target for r in sink.results[:3]] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_loop_survives_probe_exceptions() -> None:
    sink = FakeSink()
    stop = asyncio.Event()
    calls: dict[str, int] = {"n": 0}

    async def flaky(t: PingTarget) -> PingResult:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return PingResult(
            ts=datetime.now(UTC), target=t.host, label=t.label,
            kind="icmp", success=True, latency_ms=1.0, error=None,
        )

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        run_rotating_probe([PingTarget("a", "A")], 0.01, sink, flaky, stop=stop, name="test"),
        stopper(),
    )
    assert calls["n"] >= 3  # kept going past the exception
    assert any(r.success for r in sink.results)


@pytest.mark.asyncio
async def test_empty_targets_exits_clean() -> None:
    sink = FakeSink()
    stop = asyncio.Event()

    async def probe(t: PingTarget) -> Any:
        raise AssertionError("should not be called")

    await run_rotating_probe([], 0.01, sink, probe, stop=stop, name="test")
    assert sink.results == []
