"""Analytics unit tests — the core math, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pingcapture.analytics import (
    bucket_size_for_window,
    buffer_bloat_score,
    detect_outages,
    downsample_latency,
    latency_stats,
    mtr_path_changes,
    uptime_pct,
    video_call_uptime_pct,
)
from pingcapture.storage import MtrHop, MtrRun

from ..conftest import mk_ping


def _series(now: datetime, pattern: str, *, step_s: int = 5) -> list:
    """Build a ping series. 'O' = success, '.' = fail.

    Probe kinds alternate icmp/tcp/icmp/tcp so that any failure streak >= 2
    contains both kinds — matching the production rotation and satisfying the
    detector's TCP-in-streak rule.
    """
    kinds = ("icmp", "tcp")
    return [
        mk_ping(ts=now + timedelta(seconds=i * step_s),
                success=(c == "O"), kind=kinds[i % 2])
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


# --- video-call uptime ---------------------------------------------------


def _good_minute(now: datetime, *, minute: int) -> list:
    """One minute (60s) of all-successful, low-latency probes — a 'good' window."""
    base = now + timedelta(minutes=minute)
    kinds = ("icmp", "tcp")
    return [
        mk_ping(ts=base + timedelta(seconds=i * 5), success=True,
                kind=kinds[i % 2], latency_ms=20.0)
        for i in range(12)
    ]


def test_video_uptime_all_clear(now: datetime) -> None:
    pings = _good_minute(now, minute=0) + _good_minute(now, minute=1)
    assert video_call_uptime_pct(pings, []) == 100.0


def test_video_uptime_excludes_outage_window(now: datetime) -> None:
    # Two minutes; the second overlaps a detected outage.
    pings = _good_minute(now, minute=0) + _good_minute(now, minute=1)
    from pingcapture.analytics import Outage
    outage = Outage(
        start=now + timedelta(minutes=1, seconds=10),
        end=now + timedelta(minutes=1, seconds=40),
        duration_s=30.0,
        failed_probes=6,
        affected_targets=["1.1.1.1"],
    )
    assert video_call_uptime_pct(pings, [outage]) == 50.0


def test_video_uptime_high_loss_window_is_bad(now: datetime) -> None:
    # Minute 0 is clean. Minute 1 has 2 failures in 12 probes (~16% loss) > 1%.
    good = _good_minute(now, minute=0)
    bad_base = now + timedelta(minutes=1)
    bad = []
    for i in range(12):
        kind = ("icmp", "tcp")[i % 2]
        success = i not in (3, 7)
        bad.append(mk_ping(
            ts=bad_base + timedelta(seconds=i * 5),
            success=success, kind=kind, latency_ms=20.0,
        ))
    assert video_call_uptime_pct(good + bad, []) == 50.0


def test_video_uptime_high_p95_latency_is_bad(now: datetime) -> None:
    # Minute 0 is clean. Minute 1 has 5/12 samples at 500ms so p95 lands
    # well above the 300ms threshold (a single outlier wouldn't — that case
    # is exercised by test_video_uptime_single_rtt_outlier_is_good).
    good = _good_minute(now, minute=0)
    bad_base = now + timedelta(minutes=1)
    kinds = ("icmp", "tcp")
    bad = [
        mk_ping(ts=bad_base + timedelta(seconds=i * 5),
                success=True, kind=kinds[i % 2],
                latency_ms=20.0 if i < 7 else 500.0)
        for i in range(12)
    ]
    assert video_call_uptime_pct(good + bad, []) == 50.0


def test_video_uptime_high_jitter_is_bad(now: datetime) -> None:
    # Minute 0 is clean. Minute 1 alternates 20/120ms — bottom half clusters
    # at 20, top half at 120, so IQR ≈ 100ms (well over the 30ms threshold).
    # Each sample is still under the 300ms p95 cap.
    good = _good_minute(now, minute=0)
    bad_base = now + timedelta(minutes=1)
    kinds = ("icmp", "tcp")
    bad = [
        mk_ping(ts=bad_base + timedelta(seconds=i * 5),
                success=True, kind=kinds[i % 2],
                latency_ms=20.0 if i % 2 == 0 else 120.0)
        for i in range(12)
    ]
    assert video_call_uptime_pct(good + bad, []) == 50.0


def test_video_uptime_single_rtt_outlier_is_good(now: datetime) -> None:
    # Regression: a single 200ms spike among 11 fast probes used to trip the
    # stddev-based jitter check, even though a real video call wouldn't notice.
    # With IQR-based jitter, the outlier sits outside the middle 50%, so the
    # window stays GOOD.
    base = now
    kinds = ("icmp", "tcp")
    pings = [
        mk_ping(ts=base + timedelta(seconds=i * 5),
                success=True, kind=kinds[i % 2],
                latency_ms=200.0 if i == 5 else 20.0)
        for i in range(12)
    ]
    assert video_call_uptime_pct(pings, []) == 100.0


def test_video_uptime_empty_input_is_100() -> None:
    assert video_call_uptime_pct([], []) == 100.0


def test_video_uptime_ignores_empty_minutes(now: datetime) -> None:
    # Capture has a gap (minute 5..10) — denominator should be 2, not 12.
    pings = _good_minute(now, minute=0) + _good_minute(now, minute=15)
    assert video_call_uptime_pct(pings, []) == 100.0


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
    p1 = mk_ping(ts=now, success=False, kind="icmp")
    p2 = mk_ping(ts=step_seconds(now, 5), success=False, kind="tcp")
    p3 = mk_ping(ts=step_seconds(now, 10), success=False, kind="icmp")
    p4 = mk_ping(ts=step_seconds(now, 600), success=True)  # 10 min later
    outages = detect_outages([p1, p2, p3, p4])
    assert len(outages) == 1
    assert outages[0].duration_s == 10  # closed at last sample before gap, not at recovery


def test_outage_affected_targets_sorted(now: datetime) -> None:
    # Mix kinds so the streak contains a TCP failure (required for outage).
    pings = [
        mk_ping(ts=now, target="9.9.9.9", success=False, kind="icmp"),
        mk_ping(ts=now + timedelta(seconds=5), target="1.1.1.1", success=False, kind="tcp"),
        mk_ping(ts=now + timedelta(seconds=10), target="8.8.8.8", success=False, kind="icmp"),
        mk_ping(ts=now + timedelta(seconds=15), target="1.1.1.1", success=True),
    ]
    outages = detect_outages(pings)
    assert outages[0].affected_targets == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def test_icmp_only_streak_is_not_an_outage(now: datetime) -> None:
    """Real-world ICMP shaping: 5 ICMP failures, no TCP probe in window."""
    pings = [
        mk_ping(ts=now, kind="icmp"),  # success
        mk_ping(ts=now + timedelta(seconds=5), kind="icmp", success=False),
        mk_ping(ts=now + timedelta(seconds=10), kind="icmp", success=False),
        mk_ping(ts=now + timedelta(seconds=15), kind="icmp", success=False),
        mk_ping(ts=now + timedelta(seconds=20), kind="icmp", success=False),
        mk_ping(ts=now + timedelta(seconds=25), kind="icmp"),  # success
    ]
    assert detect_outages(pings) == []


def test_streak_with_one_tcp_failure_is_an_outage(now: datetime) -> None:
    pings = [
        mk_ping(ts=now, kind="icmp"),  # success
        mk_ping(ts=now + timedelta(seconds=5), kind="icmp", success=False),
        mk_ping(ts=now + timedelta(seconds=10), kind="tcp", success=False),
        mk_ping(ts=now + timedelta(seconds=15), kind="icmp", success=False),
        mk_ping(ts=now + timedelta(seconds=20), kind="icmp"),  # success
    ]
    outages = detect_outages(pings)
    assert len(outages) == 1
    assert outages[0].failed_probes == 3


def test_streak_with_only_tcp_failures_is_an_outage(now: datetime) -> None:
    """TCP-only failure means real services are breaking — still an outage."""
    pings = [
        mk_ping(ts=now, kind="tcp"),  # success
        mk_ping(ts=now + timedelta(seconds=5), kind="tcp", success=False),
        mk_ping(ts=now + timedelta(seconds=10), kind="tcp", success=False),
        mk_ping(ts=now + timedelta(seconds=15), kind="tcp", success=False),
        mk_ping(ts=now + timedelta(seconds=20), kind="tcp"),  # success
    ]
    outages = detect_outages(pings)
    assert len(outages) == 1
    assert outages[0].failed_probes == 3


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


def test_bucket_size_for_window() -> None:
    assert bucket_size_for_window(0.5) == 0
    assert bucket_size_for_window(1.0) == 0
    assert bucket_size_for_window(6.0) == 30
    assert bucket_size_for_window(24.0) == 120
    assert bucket_size_for_window(168.0) == 900


def test_downsample_latency_groups_by_target(now: datetime) -> None:
    # 60s of pings, 30s buckets => 2 buckets per target.
    pings = []
    for i in range(60):
        pings.append(mk_ping(ts=now + timedelta(seconds=i),
                             target="a", label="A", latency_ms=10.0 + i))
        pings.append(mk_ping(ts=now + timedelta(seconds=i),
                             target="b", label="B", latency_ms=20.0))
    out = downsample_latency(pings, window_start=now, bucket_size_s=30)
    assert len(out) == 4  # 2 targets x 2 buckets
    a_buckets = [b for b in out if b.target == "a"]
    assert len(a_buckets) == 2
    assert a_buckets[0].samples == 30
    # p50 of latencies 10..39 is between 24 and 25
    assert 24.0 <= a_buckets[0].p50_ms <= 25.0
    b_buckets = [b for b in out if b.target == "b"]
    assert b_buckets[0].p50_ms == 20.0


def test_downsample_latency_counts_failures(now: datetime) -> None:
    pings = [
        mk_ping(ts=now, success=True, latency_ms=10.0),
        mk_ping(ts=now + timedelta(seconds=10), success=False, error="x"),
        mk_ping(ts=now + timedelta(seconds=20), success=True, latency_ms=12.0),
    ]
    out = downsample_latency(pings, window_start=now, bucket_size_s=60)
    assert len(out) == 1
    assert out[0].samples == 3
    assert out[0].failed == 1
    assert out[0].p50_ms == 11.0


def test_downsample_latency_zero_bucket_returns_empty(now: datetime) -> None:
    pings = [mk_ping(ts=now, latency_ms=10.0)]
    assert downsample_latency(pings, window_start=now, bucket_size_s=0) == []
