"""Tests for the status-grid bucketing + severity classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pingcapture.analytics import (
    SEVERITY_FLICKER,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    SEVERITY_NO_DATA,
    SEVERITY_NONE,
    SEVERITY_SEVERE,
    bucket_outages,
    floor_to_hour,
    pivot_buckets_by_day,
    summarize_by_hour_of_day,
)

from ..conftest import mk_ping


def _hour(start: datetime, idx: int) -> datetime:
    return start + timedelta(hours=idx)


def test_no_data_bucket_is_grey() -> None:
    start = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    buckets = bucket_outages([], window_start=start, window_end=end, bucket_size_s=3600)
    assert len(buckets) == 2
    assert all(b.severity == SEVERITY_NO_DATA for b in buckets)
    assert all(b.samples == 0 for b in buckets)


def test_clean_hour_is_green() -> None:
    start = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    pings = [mk_ping(ts=start + timedelta(seconds=i * 5)) for i in range(720)]
    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    assert len(buckets) == 1
    assert buckets[0].severity == SEVERITY_NONE
    assert buckets[0].failed == 0
    assert buckets[0].uptime_pct == 100.0


def test_classification_thresholds() -> None:
    """Each bucket gets the severity matching its longest outage."""
    start = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=5)
    pings: list = []

    def fill_hour(hour_idx: int, fail_seconds: float) -> None:
        """Drop one outage of `fail_seconds` into hour `hour_idx`. Probes
        every 5 s, so e.g. 3 consecutive failures = 15 s outage."""
        h_start = _hour(start, hour_idx)
        # 720 probes per hour at 5s
        n_fail = max(0, int(fail_seconds // 5))
        # Need >=3 consecutive failures to trip the detector.
        for i in range(720):
            ts = h_start + timedelta(seconds=i * 5)
            ok = not (10 <= i < 10 + n_fail)
            pings.append(mk_ping(ts=ts, success=ok))

    # N consecutive failed probes at 5s spacing span N*5 seconds
    # (outage ends at the next successful probe, 5s after the last fail).
    fill_hour(0, 5)        # 1 failed probe — single fail, doesn't reach 3-failure outage threshold
    fill_hour(1, 20)       # 4 fails → 20s outage → minor (>15s, ≤30s)
    fill_hour(2, 35)       # 7 fails → 35s outage → major (>30s, <5min)
    fill_hour(3, 310)      # 62 fails → 310s outage → severe (≥5min)
    fill_hour(4, 0)        # clean hour → green

    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    assert [b.severity for b in buckets] == [
        SEVERITY_FLICKER,   # 1 fail, no outage detected, but failed probes present
        SEVERITY_MINOR,
        SEVERITY_MAJOR,
        SEVERITY_SEVERE,
        SEVERITY_NONE,
    ]
    assert buckets[1].outage_count == 1
    assert buckets[2].longest_outage_s == 35.0
    assert buckets[3].longest_outage_s == 310.0


def test_yellow_for_short_outage() -> None:
    """A 15s outage (3 consecutive fails) crosses the 5s threshold but not 15s,
    so it should be classified as a flicker."""
    start = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    pings = []
    for i in range(720):
        ok = not (100 <= i < 103)  # 3 fails → 15s outage (boundary case)
        pings.append(mk_ping(ts=start + timedelta(seconds=i * 5), success=ok))
    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    assert buckets[0].severity == SEVERITY_FLICKER
    assert buckets[0].longest_outage_s == 15.0


def test_pings_outside_window_excluded() -> None:
    start = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    pings = [
        mk_ping(ts=start - timedelta(minutes=5)),  # before
        mk_ping(ts=start + timedelta(minutes=5)),  # in
        mk_ping(ts=end + timedelta(minutes=5)),    # after
    ]
    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    assert buckets[0].samples == 1


def test_bucket_count_matches_window() -> None:
    start = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    # Exactly 24 hours
    end = start + timedelta(hours=24)
    buckets = bucket_outages([], window_start=start, window_end=end, bucket_size_s=3600)
    assert len(buckets) == 24


def test_floor_to_hour() -> None:
    ts = datetime(2026, 4, 26, 13, 47, 23, 500_000, tzinfo=UTC)
    assert floor_to_hour(ts) == datetime(2026, 4, 26, 13, 0, 0, tzinfo=UTC)


def test_floor_to_hour_naive_treated_as_utc() -> None:
    ts = datetime(2026, 4, 26, 13, 47, 23)
    floored = floor_to_hour(ts)
    assert floored.tzinfo is not None
    assert floored.hour == 13
    assert floored.minute == 0


# --- calendar pivot ---

def test_pivot_groups_by_day_with_24_columns() -> None:
    start = datetime(2026, 4, 25, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=2)
    pings = [mk_ping(ts=start + timedelta(seconds=i * 5)) for i in range(720 * 48)]
    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    rows = pivot_buckets_by_day(buckets)
    assert len(rows) == 2
    for row in rows:
        assert len(row.cells) == 24
    # Most recent day first
    assert rows[0].date > rows[1].date


def test_pivot_fills_missing_hours_with_no_data() -> None:
    """A partial day (e.g. only the second half captured) still gets 24 cells —
    the missing hours show up as no_data, so the report renders as a clean
    rectangle instead of jagged."""
    start = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)  # noon
    end = start + timedelta(hours=4)
    pings = [mk_ping(ts=start + timedelta(seconds=i * 5)) for i in range(720 * 4)]
    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    rows = pivot_buckets_by_day(buckets)
    assert len(rows) == 1
    row = rows[0]
    assert len(row.cells) == 24
    # Hours 0..11 should be no_data, 12..15 green, 16..23 no_data.
    assert all(c.severity == SEVERITY_NO_DATA for c in row.cells[:12])
    assert all(c.severity == SEVERITY_NONE for c in row.cells[12:16])
    assert all(c.severity == SEVERITY_NO_DATA for c in row.cells[16:])


def test_pivot_empty_input_is_empty() -> None:
    assert pivot_buckets_by_day([]) == []


# --- hour-of-day summary ---

def test_hour_of_day_aggregates_worst_severity() -> None:
    """If hour 14 is severe on one day and minor on another, the summary
    should report severe — that's the pattern worth flagging."""
    start = datetime(2026, 4, 25, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=2)
    pings = []
    for day_idx in range(2):
        day_start = start + timedelta(days=day_idx)
        for i in range(720 * 24):
            ts = day_start + timedelta(seconds=i * 5)
            # Day 0: 14:00 has a severe outage; Day 1: 14:00 has only a minor one.
            in_2pm = ts.hour == 14
            offset_in_hour = (ts - day_start.replace(hour=14)).total_seconds() if in_2pm else 0
            day0_severe = day_idx == 0 and in_2pm and 0 <= offset_in_hour < 310
            day1_minor = day_idx == 1 and in_2pm and 0 <= offset_in_hour < 20
            if day0_severe or day1_minor:
                pings.append(mk_ping(ts=ts, success=False))
            else:
                pings.append(mk_ping(ts=ts, success=True))
    buckets = bucket_outages(pings, window_start=start, window_end=end, bucket_size_s=3600)
    summary = summarize_by_hour_of_day(buckets)
    assert len(summary) == 24
    h14 = summary[14]
    assert h14.hour == 14
    assert h14.worst_severity == SEVERITY_SEVERE
    assert h14.days_observed == 2
    assert h14.days_with_failure == 2
    # A clean hour should report green.
    assert summary[3].worst_severity == SEVERITY_NONE
    assert summary[3].days_with_failure == 0


def test_hour_of_day_no_data_when_nothing_observed() -> None:
    summary = summarize_by_hour_of_day([])
    assert len(summary) == 24
    assert all(h.worst_severity == SEVERITY_NO_DATA for h in summary)
    assert all(h.days_observed == 0 for h in summary)
