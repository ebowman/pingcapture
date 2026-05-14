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
    # Minute 0 is clean. Minute 1 has 2 TCP failures in 6 TCP probes
    # (~33% TCP loss) > 1%. ICMP probes (other 6) all succeed.
    good = _good_minute(now, minute=0)
    bad_base = now + timedelta(minutes=1)
    bad = []
    for i in range(12):
        kind = ("icmp", "tcp")[i % 2]
        # i=3 and i=7 are both TCP (odd indices)
        success = i not in (3, 7)
        bad.append(mk_ping(
            ts=bad_base + timedelta(seconds=i * 5),
            success=success, kind=kind, latency_ms=20.0,
        ))
    assert video_call_uptime_pct(good + bad, []) == 50.0


def test_video_uptime_icmp_only_loss_is_good(now: datetime) -> None:
    # Regression: a cluster of ICMP failures with no TCP failures used to
    # flag the minute as 'down', even though the outage detector ignores
    # the same pattern (upstream ICMP rate-limiting, not user-visible loss).
    good = _good_minute(now, minute=0)
    bad_base = now + timedelta(minutes=1)
    bad = []
    for i in range(12):
        kind = ("icmp", "tcp")[i % 2]
        # Kill 3 of the 6 ICMP probes; every TCP probe succeeds.
        success = not (kind == "icmp" and i in (2, 4, 8))
        bad.append(mk_ping(
            ts=bad_base + timedelta(seconds=i * 5),
            success=success, kind=kind, latency_ms=20.0,
        ))
    assert video_call_uptime_pct(good + bad, []) == 100.0


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
    # at 20, top half at 120, so IQR ≈ 100ms (well over the 75ms threshold).
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


def test_icmp_failures_across_three_targets_is_an_outage(now: datetime) -> None:
    """The 2026-05-14 15:08 case: 3 distinct ICMP targets fail in a streak,
    no TCP probe lands in the window. Counts as a real outage under the
    expanded rule — three independent anycast destinations don't all go
    quiet at once except from real upstream loss."""
    pings = [
        mk_ping(ts=now, kind="icmp", target="1.1.1.1"),  # success
        mk_ping(ts=now + timedelta(seconds=5), kind="icmp", target="9.9.9.9", success=False),
        mk_ping(ts=now + timedelta(seconds=10), kind="icmp", target="8.8.8.8", success=False),
        mk_ping(ts=now + timedelta(seconds=15), kind="icmp", target="1.1.1.1", success=False),
        mk_ping(ts=now + timedelta(seconds=20), kind="icmp", target="1.1.1.1"),  # success
    ]
    outages = detect_outages(pings)
    assert len(outages) == 1
    assert sorted(outages[0].affected_targets) == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def test_icmp_failures_across_two_targets_is_flicker(now: datetime) -> None:
    """Two distinct ICMP targets failing while TCP succeeds is below the
    multi-target threshold — still demotes to flicker."""
    pings = [
        mk_ping(ts=now, kind="icmp", target="1.1.1.1"),  # success
        mk_ping(ts=now + timedelta(seconds=5), kind="icmp", target="9.9.9.9", success=False),
        mk_ping(ts=now + timedelta(seconds=10), kind="icmp", target="8.8.8.8", success=False),
        mk_ping(ts=now + timedelta(seconds=15), kind="icmp", target="9.9.9.9", success=False),
        mk_ping(ts=now + timedelta(seconds=20), kind="icmp", target="1.1.1.1"),  # success
    ]
    assert detect_outages(pings) == []


def test_streak_with_dns_failure_is_an_outage(now: datetime) -> None:
    """A DNS resolution failure inside a streak proves the loss isn't just
    ICMP shaping — DNS uses a different upstream path. Streak counts even
    with no TCP failure and only one ICMP target."""
    pings = [
        mk_ping(ts=now, kind="icmp", target="1.1.1.1"),
        mk_ping(ts=now + timedelta(seconds=5), kind="icmp", target="1.1.1.1",
                success=False, error="no reply"),
        mk_ping(ts=now + timedelta(seconds=10), kind="icmp", target="cloudflare.com",
                success=False,
                error="NameLookupError: The name 'cloudflare.com' cannot be resolved"),
        mk_ping(ts=now + timedelta(seconds=15), kind="icmp", target="1.1.1.1",
                success=False, error="no reply"),
        mk_ping(ts=now + timedelta(seconds=20), kind="icmp", target="1.1.1.1"),
    ]
    outages = detect_outages(pings)
    assert len(outages) == 1


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


# ---------------------------------------------------------------------------
# XmR (Wheeler) control charts
# ---------------------------------------------------------------------------

from pingcapture.analytics import (
    XMR_MR_CONSTANT,
    XMR_NPL_CONSTANT,
    xmr_charts,
)


def _xmr_pings(now: datetime, values: list[float], *, bucket_s: int = 300,
               target: str = "1.1.1.1", label: str = "Cloudflare",
               kind: str = "icmp") -> list:
    """One successful ping per bucket, RTT = values[i]. Drives bin medians."""
    return [
        mk_ping(
            ts=now + timedelta(seconds=i * bucket_s),
            target=target, label=label, kind=kind,
            success=True, latency_ms=v,
        )
        for i, v in enumerate(values)
    ]


def test_xmr_in_control_no_signals(now: datetime) -> None:
    # 20 points hovering tightly around 10ms — should be all in-control.
    vals = [10.0, 10.5, 9.8, 10.2, 10.1, 9.9, 10.3, 10.4, 9.7, 10.0,
            10.2, 9.9, 10.1, 10.5, 9.8, 10.3, 10.0, 10.2, 9.9, 10.1]
    charts = xmr_charts(_xmr_pings(now, vals), window_start=now, bucket_size_s=300)
    assert len(charts) == 1
    c = charts[0]
    assert c.target == "1.1.1.1"
    assert c.kind == "icmp"
    assert c.center is not None and 9.5 < c.center < 10.5
    # Limits derived from the moving range itself.
    assert c.unpl == c.center + XMR_NPL_CONSTANT * c.mr_bar
    assert c.lnpl == max(0.0, c.center - XMR_NPL_CONSTANT * c.mr_bar)
    assert c.mr_url == XMR_MR_CONSTANT * c.mr_bar
    assert len(c.points) == len(vals)
    # First point has no moving range to a predecessor.
    assert c.points[0].mr is None
    assert all(p.signals == [] for p in c.points)


def test_xmr_flags_point_outside_limits(now: datetime) -> None:
    # 15 baseline points, then one obvious outlier.
    vals = [10.0] * 15 + [200.0]
    charts = xmr_charts(_xmr_pings(now, vals), window_start=now, bucket_size_s=300)
    assert len(charts) == 1
    sigs = charts[0].points[-1].signals
    assert "outside_limits" in sigs
    # The 200ms jump from a 10ms baseline also blows past the MR URL.
    assert "mr_outside_url" in sigs


def test_xmr_flags_run_of_eight_trigger_only(now: datetime) -> None:
    # 4 above, 4 below, then 10 above — only the *trigger* (the 8th of the
    # run) fires; continuation points do not. This matches standard XmR
    # practice and keeps drifted-process series from carpeting the chart.
    vals = ([12, 8] * 4) + [12] * 10
    charts = xmr_charts(_xmr_pings(now, vals), window_start=now, bucket_size_s=300)
    flagged = [i for i, p in enumerate(charts[0].points) if "run_of_8" in p.signals]
    # Run of 12s starts at index 8; the 8th of that run is index 8+7 = 15.
    assert flagged == [15]


def test_xmr_flags_trend_up_six(now: datetime) -> None:
    # 6 strictly-increasing points after a flat baseline.
    vals = [10.0] * 10 + [11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    charts = xmr_charts(_xmr_pings(now, vals), window_start=now, bucket_size_s=300)
    last = charts[0].points[-1]
    assert "trend_up_6" in last.signals


def test_xmr_lnpl_clamped_at_zero(now: datetime) -> None:
    # Very noisy series whose mean - 2.66*MR-bar would be negative; the
    # LNPL should clamp at zero (RTT can't be negative).
    vals = [1.0, 50.0] * 10  # alternating big/small
    charts = xmr_charts(_xmr_pings(now, vals), window_start=now, bucket_size_s=300)
    assert charts[0].lnpl == 0.0


def test_xmr_filters_by_kind(now: datetime) -> None:
    # ICMP + TCP probes; default kind=icmp keeps only ICMP.
    icmp = _xmr_pings(now, [10.0] * 12, kind="icmp", target="a", label="A")
    tcp = _xmr_pings(now, [20.0] * 12, kind="tcp", target="a", label="A")
    icmp_only = xmr_charts(icmp + tcp, window_start=now, bucket_size_s=300, kind="icmp")
    assert len(icmp_only) == 1
    assert icmp_only[0].kind == "icmp"
    assert 9.5 < icmp_only[0].center < 10.5


def test_xmr_skips_targets_below_min_points(now: datetime) -> None:
    # Only 5 bins — below the default min_points=10.
    out = xmr_charts(_xmr_pings(now, [10.0] * 5), window_start=now, bucket_size_s=300)
    assert out == []


def test_xmr_one_chart_per_target(now: datetime) -> None:
    a = _xmr_pings(now, [10.0] * 12, target="a", label="A")
    b = _xmr_pings(now, [50.0] * 12, target="b", label="B")
    charts = xmr_charts(a + b, window_start=now, bucket_size_s=300)
    assert {c.target for c in charts} == {"a", "b"}
    by_target = {c.target: c for c in charts}
    assert by_target["a"].center is not None and 9.5 < by_target["a"].center < 10.5
    assert by_target["b"].center is not None and 49.5 < by_target["b"].center < 50.5


def test_xmr_excludes_failed_probes(now: datetime) -> None:
    # 12 successful probes + a scattered failure — failures don't enter the
    # individuals series, but the chart still builds from the 12 successes.
    pings = _xmr_pings(now, [10.0] * 12)
    pings.append(mk_ping(
        ts=now + timedelta(seconds=6 * 300), success=False, error="timeout",
        latency_ms=None,
    ))
    charts = xmr_charts(pings, window_start=now, bucket_size_s=300)
    assert len(charts) == 1
    # 12 distinct buckets, one point each.
    assert len(charts[0].points) == 12


# ---------------------------------------------------------------------------
# Quality events — minutes failing the video-call rule for reasons other
# than an overlapping connectivity outage.
# ---------------------------------------------------------------------------

from pingcapture.analytics import (
    QUALITY_REASON_JITTER,
    QUALITY_REASON_LATENCY,
    QUALITY_REASON_LOSS,
    quality_events,
)


def test_quality_events_empty_when_no_pings() -> None:
    assert quality_events([], []) == []


def test_quality_events_high_p95_latency_fires(now: datetime) -> None:
    # Twelve probes in one minute, p95 > 300ms. No connectivity outage.
    pings = [
        mk_ping(ts=now + timedelta(seconds=i), kind="icmp", latency_ms=400.0)
        for i in range(12)
    ]
    events = quality_events(pings, [])
    assert len(events) == 1
    assert events[0].reason == QUALITY_REASON_LATENCY
    assert events[0].worst_metric >= 300.0


def test_quality_events_high_jitter_fires(now: datetime) -> None:
    # Latencies span 10 / 20 / 90 / 200 ms — IQR easily > 75ms, p95 still
    # under 300. Must trip the jitter check, not the latency check.
    rtts = [10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0, 90.0, 110.0, 150.0, 200.0]
    pings = [
        mk_ping(ts=now + timedelta(seconds=i), kind="icmp", latency_ms=r)
        for i, r in enumerate(rtts)
    ]
    events = quality_events(pings, [])
    assert len(events) == 1
    assert events[0].reason == QUALITY_REASON_JITTER


def test_quality_events_tcp_loss_fires(now: datetime) -> None:
    # 10 TCP probes, 3 fail = 30% > 1% threshold. ICMP all fine, so no
    # outage opens (and we pass no outages anyway).
    pings = []
    for i in range(10):
        pings.append(mk_ping(
            ts=now + timedelta(seconds=i * 5), kind="tcp",
            success=(i not in (3, 4, 5)),
        ))
    events = quality_events(pings, [])
    assert len(events) == 1
    assert events[0].reason == QUALITY_REASON_LOSS


def test_quality_events_skipped_when_minute_overlaps_outage(now: datetime) -> None:
    """Minutes inside a connectivity outage are accounted for by the outage
    row — they must not also become quality events (double-counting)."""
    from pingcapture.analytics import Outage

    pings = [
        mk_ping(ts=now + timedelta(seconds=i), kind="icmp", latency_ms=400.0)
        for i in range(12)
    ]
    outage = Outage(
        start=now,
        end=now + timedelta(seconds=30),
        duration_s=30.0,
        failed_probes=3,
        affected_targets=["1.1.1.1"],
    )
    events = quality_events(pings, [outage])
    assert events == []


def test_quality_events_good_minute_produces_nothing(now: datetime) -> None:
    pings = [
        mk_ping(ts=now + timedelta(seconds=i), kind="icmp", latency_ms=15.0)
        for i in range(12)
    ]
    assert quality_events(pings, []) == []


def test_quality_and_uptime_agree(now: datetime) -> None:
    """The whole point of refactoring _classify_minute: video_call_uptime_pct
    and quality_events must produce a consistent picture. If uptime < 100%
    and no outages were detected, the gap must equal len(quality_events) / N
    where N is the total minute count."""
    # Two minutes total: one good, one with a 400ms latency spike.
    base = now.replace(second=0, microsecond=0)
    pings = []
    for i in range(12):
        pings.append(mk_ping(ts=base + timedelta(seconds=i), kind="icmp", latency_ms=15.0))
    for i in range(12):
        pings.append(mk_ping(ts=base + timedelta(minutes=1, seconds=i),
                             kind="icmp", latency_ms=400.0))
    events = quality_events(pings, [])
    uptime = video_call_uptime_pct(pings, [])
    assert len(events) == 1
    # 1 good out of 2 minutes -> 50%.
    assert uptime == 50.0


def test_quality_event_reason_priority_loss_over_latency(now: datetime) -> None:
    """When a minute trips both TCP loss and high latency, the reason field
    should report loss (the more user-visible symptom)."""
    pings = []
    for i in range(10):
        pings.append(mk_ping(
            ts=now + timedelta(seconds=i * 5),
            kind="tcp", success=(i % 5 != 0),  # 20% loss
            latency_ms=400.0 if i % 5 != 0 else None,
        ))
    events = quality_events(pings, [])
    assert len(events) == 1
    assert events[0].reason == QUALITY_REASON_LOSS
