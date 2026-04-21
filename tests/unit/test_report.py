"""Report rendering tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pingcapture.config import Config
from pingcapture.report import ReportInputs, parse_since, render_report
from pingcapture.storage import MtrHop, MtrRun, Store

from ..conftest import mk_ping


def test_parse_since_units() -> None:
    assert parse_since("30m") == timedelta(minutes=30)
    assert parse_since("24h") == timedelta(hours=24)
    assert parse_since("7d") == timedelta(days=7)
    assert parse_since("2w") == timedelta(weeks=2)


def test_parse_since_invalid() -> None:
    with pytest.raises(ValueError):
        parse_since("bogus")
    with pytest.raises(ValueError):
        parse_since("7y")


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("fmt", ["html", "md"])
def test_report_renders_without_missing_keys(store: Store, now: datetime, lang: str, fmt: str) -> None:
    # Some data: a few pings, an outage, an mtr run with a path change.
    for i in range(20):
        store.insert_ping(mk_ping(ts=now + timedelta(seconds=i * 5), success=(i not in (5, 6, 7))))
    store.insert_mtr_run(MtrRun(
        ts=now, target="1.1.1.1", label="CF",
        hops=[MtrHop(hop_idx=1, host=None, ip="10.0.0.1", loss_pct=0, sent=10,
                     last_ms=1, avg_ms=1, best_ms=1, worst_ms=1, stddev_ms=0)],
    ))
    cfg = Config.defaults()
    out = render_report(
        cfg=cfg, store=store,
        inputs=ReportInputs(
            start=now - timedelta(seconds=1),
            end=now + timedelta(seconds=200),
            lang=lang, owner="Test User",
        ),
        fmt=fmt,
    )
    assert "Test User" in out
    if fmt == "html":
        assert "<html" in out
    if lang == "de":
        assert "Berichtszeitraum" in out
    else:
        assert "Reporting period" in out


def test_report_handles_empty_window(store: Store) -> None:
    cfg = Config.defaults()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    out = render_report(
        cfg=cfg, store=store,
        inputs=ReportInputs(start=start, end=end, lang="en"),
        fmt="md",
    )
    assert "No outages" in out
    assert "100.000 %" in out  # uptime defaults to 100% on no data
