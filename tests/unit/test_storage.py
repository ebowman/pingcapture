"""Storage layer unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pingcapture.storage import MtrHop, MtrRun, Store

from ..conftest import mk_ping


def test_round_trip_ping(store: Store, now: datetime) -> None:
    p = mk_ping(ts=now)
    store.insert_ping(p)
    got = store.pings_between(now - timedelta(seconds=1), now + timedelta(seconds=1))
    assert len(got) == 1
    assert got[0].target == "1.1.1.1"
    assert got[0].success is True
    assert got[0].latency_ms == 12.5


def test_filter_by_kind(store: Store, now: datetime) -> None:
    store.insert_ping(mk_ping(ts=now, kind="icmp"))
    store.insert_ping(mk_ping(ts=now + timedelta(seconds=1), kind="tcp"))
    icmp = store.pings_between(now - timedelta(seconds=1), now + timedelta(seconds=10), kind="icmp")
    tcp = store.pings_between(now - timedelta(seconds=1), now + timedelta(seconds=10), kind="tcp")
    assert len(icmp) == 1 and icmp[0].kind == "icmp"
    assert len(tcp) == 1 and tcp[0].kind == "tcp"


def test_latest_pings_descending(store: Store, now: datetime) -> None:
    for i in range(5):
        store.insert_ping(mk_ping(ts=now + timedelta(seconds=i)))
    latest = store.latest_pings(limit=3)
    assert len(latest) == 3
    # Newest first
    assert latest[0].ts > latest[1].ts > latest[2].ts


def test_failure_round_trip(store: Store, now: datetime) -> None:
    p = mk_ping(ts=now, success=False, error="timeout")
    store.insert_ping(p)
    got = store.pings_between(now - timedelta(seconds=1), now + timedelta(seconds=1))
    assert got[0].success is False
    assert got[0].latency_ms is None
    assert got[0].error == "timeout"


def test_mtr_round_trip(store: Store, now: datetime) -> None:
    run = MtrRun(
        ts=now, target="1.1.1.1", label="CF",
        hops=[
            MtrHop(hop_idx=1, host="gw", ip="10.0.0.1", loss_pct=0.0, sent=10,
                   last_ms=2.1, avg_ms=2.0, best_ms=1.5, worst_ms=3.0, stddev_ms=0.5),
            MtrHop(hop_idx=2, host=None, ip="172.16.0.1", loss_pct=10.0, sent=10,
                   last_ms=8.0, avg_ms=8.5, best_ms=7.0, worst_ms=12.0, stddev_ms=1.5),
        ],
    )
    store.insert_mtr_run(run)
    got = store.mtr_runs_between(now - timedelta(seconds=1), now + timedelta(seconds=1))
    assert len(got) == 1
    assert len(got[0].hops) == 2
    assert got[0].hops[1].loss_pct == 10.0


def test_schema_version_set(tmp_db) -> None:
    s = Store(tmp_db)
    try:
        v = s._conn.execute("PRAGMA user_version").fetchone()[0]
        from pingcapture.storage import SCHEMA_VERSION
        assert v == SCHEMA_VERSION
    finally:
        s.close()


def test_hourly_buckets_materialize_on_insert(store: Store, now: datetime) -> None:
    """Inserting pings should keep the hourly_buckets row for that hour current."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    # 3 successes, 1 failure, all within the same hour.
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=1)))
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=2)))
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=3),
                              success=False, error="no reply"))
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=4)))
    rows = store.hourly_buckets_between(hour, hour + timedelta(hours=1))
    assert len(rows) == 1
    hour_start, samples, failed, longest, count, severity = rows[0]
    assert hour_start == hour
    assert samples == 4
    assert failed == 1
    # One failed probe is not enough to trigger detect_outages' 3-in-a-row rule.
    assert count == 0
    assert severity == "flicker"  # has failure but no outage span


def test_hourly_buckets_updates_in_place(store: Store, now: datetime) -> None:
    """A later insert into an already-materialized hour overwrites, not duplicates."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=1)))
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=2)))
    first = store.hourly_buckets_between(hour, hour + timedelta(hours=1))
    assert first[0][1] == 2  # samples
    store.insert_ping(mk_ping(ts=hour + timedelta(minutes=3)))
    second = store.hourly_buckets_between(hour, hour + timedelta(hours=1))
    assert len(second) == 1  # still one row
    assert second[0][1] == 3  # updated to 3 samples


def test_backfill_hourly_buckets(store: Store, now: datetime) -> None:
    """Backfill rebuilds materialized rows for hours present in ping_results."""
    hour_a = now.replace(minute=0, second=0, microsecond=0)
    hour_b = hour_a + timedelta(hours=1)
    store.insert_ping(mk_ping(ts=hour_a + timedelta(minutes=5)))
    store.insert_ping(mk_ping(ts=hour_b + timedelta(minutes=5)))
    # Wipe the materialized table; raw data stays.
    store._conn.execute("DELETE FROM hourly_buckets")
    assert store.hourly_buckets_between(hour_a, hour_b + timedelta(hours=1)) == []
    n = store.backfill_hourly_buckets()
    assert n == 2
    rows = store.hourly_buckets_between(hour_a, hour_b + timedelta(hours=1))
    assert len(rows) == 2


def test_timezone_naive_input_treated_as_utc(store: Store) -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)  # no tz
    store.insert_ping(mk_ping(ts=naive))
    got = store.pings_between(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert len(got) == 1
    assert got[0].ts.tzinfo is not None
