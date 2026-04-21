"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pingcapture.storage import PingResult, Store


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.sqlite"


@pytest.fixture
def store(tmp_db: Path) -> Store:
    s = Store(tmp_db)
    yield s
    s.close()


def mk_ping(
    *,
    ts: datetime,
    target: str = "1.1.1.1",
    label: str = "Cloudflare",
    kind: str = "icmp",
    success: bool = True,
    latency_ms: float | None = 12.5,
    error: str | None = None,
) -> PingResult:
    if not success and latency_ms == 12.5:
        latency_ms = None
    return PingResult(
        ts=ts, target=target, label=label, kind=kind,
        success=success, latency_ms=latency_ms, error=error,
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def step_seconds():
    return lambda base, n: base + timedelta(seconds=n)
