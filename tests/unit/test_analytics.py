"""Analytics unit tests — the core math, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pingcapture.analytics import (
    buffer_bloat_score,
    detect_outages,
    latency_stats,
    mtr_path_changes,
    uptime_pct,
)
from pingcapture.storage import MtrHop, MtrRun

from ..conftest import mk_ping


def _series(now: datetime, pattern: str, *, step_s: int = 5) -> list:
    """Build a ping series. 'O' = success, '.' = fail."""
    return [
        mk_ping(ts=now + timedelta(seconds=i * step_s), success=(c == "O"))
        for i, c in enumerate(pattern)
    ]


def test_uptime_all_success(now: datetime) -> None:
    pings = _series(now, "OOOOOOOO")
    assert uptime_pct(pings) == 100.0


def test_uptime_half(now: datetime) -> None:
    pings = _series(now, "O.O.O.O.")
    assert uptime_pct(pings) == 50.0


def test_uptime_empty_window_is_100() -> None:
    assert uptime_pct([]) == 100.0


def test_outage_threshold_not_met(now: datetime) -> None:
    # Two failures in a row don't count (threshold is 3).
    pings = _series(now, "OO..OO")
    assert detect_outages(pings) == []


def test_outage_detected_at_threshold(now: datetime) -> None:
    pings = _series(now, "OO...OO")
    outages = detect_outages(pings)
    assert len(outages) == 1
    o = outages[0]
    assert o.failed_probes == 3
    # Outage starts at the first failure of the streak (index 2)
    assert o.start == now + timedelta(seconds=10)
    # Outage ends when the next success arrives (index 5)
    assert o.end == now + timedelta(seconds=25)


def test_two_outages_in_one_window(now: datetime) -> None:
    pings = _series(now, "OO...OOOO...OO")
    outages = detect_outages(pings)
    assert len(outages) == 2


def test_long_quiet_gap_does_not_charge_outage(now: datetime, step_seconds) -> None:
    # 3 fails followed by a 5-minute quiet gap — should close on last sample.
    p1 = mk_ping(ts=now, success=False)
    p2 = mk_ping(ts=step_seconds(now, 5), success=False)
    p3 = mk_ping(ts=step_seconds(now, 10), success=False)
    p4 = mk_ping(ts=step_seconds(now, 600), success=True)  # 10 min later
    outages = detect_outages([p1, p2, p3, p4])
    assert len(outages) == 1
    assert outages[0].duration_s == 10  # closed at last sample before gap, not at recovery


def test_outage_affected_targets_sorted(now: datetime) -> None:
    pings = [
        mk_ping(ts=now, target="9.9.9.9", success=False),
        mk_ping(ts=now + timedelta(seconds=5), target="1.1.1.1", success=False),
        mk_ping(ts=now + timedelta(seconds=10), target="8.8.8.8", success=False),
        mk_ping(ts=now + timedelta(seconds=15), target="1.1.1.1", success=True),
    ]
    outages = detect_outages(pings)
    assert outages[0].affected_targets == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def test_latency_stats_basic(now: datetime) -> None:
    pings = [
        mk_ping(ts=now, target="1.1.1.1", latency_ms=10.0),
        mk_ping(ts=now + timedelta(seconds=1), target="1.1.1.1", latency_ms=20.0),
        mk_ping(ts=now + timedelta(seconds=2), target="1.1.1.1", latency_ms=30.0),
        mk_ping(ts=now + timedelta(seconds=3), target="1.1.1.1", success=False),
    ]
    stats = latency_stats(pings)
    assert len(stats) == 1
    s = stats[0]
    assert s.target == "1.1.1.1"
    assert s.samples == 4
    assert s.success_pct == 75.0
    assert s.p50_ms == 20.0
    assert s.max_ms == 30.0


def test_latency_stats_no_successes(now: datetime) -> None:
    pings = [mk_ping(ts=now + timedelta(seconds=i), success=False) for i in range(3)]
    stats = latency_stats(pings)
    assert stats[0].p50_ms is None
    assert stats[0].success_pct == 0.0


def test_mtr_path_changes_detected() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    run1 = MtrRun(ts=base, target="1.1.1.1", label="CF",
                  hops=[MtrHop(hop_idx=3, host=None, ip="10.0.0.5",
                               loss_pct=0, sent=1, last_ms=1, avg_ms=1, best_ms=1,
                               worst_ms=1, stddev_ms=0)])
    run2 = MtrRun(ts=base + timedelta(minutes=15), target="1.1.1.1", label="CF",
                  hops=[MtrHop(hop_idx=3, host=None, ip="10.0.0.6",
                               loss_pct=0, sent=1, last_ms=1, avg_ms=1, best_ms=1,
                               worst_ms=1, stddev_ms=0)])
    changes = mtr_path_changes([run1, run2])
    assert len(changes) == 1
    assert changes[0].old_ip == "10.0.0.5"
    assert changes[0].new_ip == "10.0.0.6"


def test_mtr_no_change_no_event() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hop = MtrHop(hop_idx=1, host=None, ip="10.0.0.1", loss_pct=0, sent=1,
                 last_ms=1, avg_ms=1, best_ms=1, worst_ms=1, stddev_ms=0)
    run1 = MtrRun(ts=base, target="x", label="x", hops=[hop])
    run2 = MtrRun(ts=base + timedelta(minutes=15), target="x", label="x", hops=[hop])
    assert mtr_path_changes([run1, run2]) == []


def test_buffer_bloat_score(now: datetime) -> None:
    pings = [
        mk_ping(ts=now, latency_ms=5.0),
        mk_ping(ts=now + timedelta(seconds=1), latency_ms=5.0),
        mk_ping(ts=now + timedelta(seconds=2), latency_ms=200.0),
        mk_ping(ts=now + timedelta(seconds=3), latency_ms=5.0),
    ]
    score = buffer_bloat_score(pings)
    assert score is not None
    assert score > 50  # high variance
