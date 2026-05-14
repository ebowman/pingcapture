"""Web API smoke tests via httpx + FastAPI TestClient."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from pingcapture.config import Config
from pingcapture.storage import Store
from pingcapture.web.app import create_app

from ..conftest import mk_ping


def _seed(store: Store, now: datetime) -> None:
    for i in range(20):
        store.insert_ping(mk_ping(ts=now + timedelta(seconds=i * 5), success=(i % 7 != 0)))


def test_status_endpoint(tmp_db, now) -> None:
    s = Store(tmp_db)
    _seed(s, now)
    s.close()
    cfg = Config.defaults()
    cfg = type(cfg)(
        **{**cfg.__dict__, "db_path": tmp_db}
    )
    client = TestClient(create_app(cfg))
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"UP", "DEGRADED", "DOWN"}
    assert "version" in body


def test_summary_endpoint(tmp_db, now) -> None:
    s = Store(tmp_db)
    _seed(s, now)
    s.close()
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/summary?hours=24")
    assert r.status_code == 200
    body = r.json()
    assert "uptime_pct" in body
    assert "outages" in body
    assert "latency" in body
    # Quality events must always be returned so the dashboard can keep its
    # 'connectivity outages + quality events' picture consistent.
    assert "quality_events" in body
    assert isinstance(body["quality_events"], list)


def test_index_serves_html(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/")
    assert r.status_code == 200
    assert "pingcapture" in r.text
    assert "<html" in r.text


def test_report_page_en(tmp_db, now) -> None:
    s = Store(tmp_db)
    _seed(s, now)
    s.close()
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/report?lang=en&since=24h")
    assert r.status_code == 200
    assert "Reporting period" in r.text
    assert 'class="toolbar"' in r.text
    assert "?lang=de&since=24h" in r.text
    assert "@media print" in r.text


def test_report_page_de(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/report?lang=de&since=7d")
    assert r.status_code == 200
    assert "Berichtszeitraum" in r.text


def test_report_page_rejects_bad_lang(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/report?lang=fr")
    assert r.status_code == 400


def test_report_page_rejects_bad_since(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/report?since=bogus")
    assert r.status_code == 400


def test_buckets_endpoint(tmp_db, now) -> None:
    s = Store(tmp_db)
    _seed(s, now)
    s.close()
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/buckets?days=1&bucket_hours=1")
    assert r.status_code == 200
    body = r.json()
    assert "buckets" in body
    assert body["bucket_hours"] == 1.0
    assert all("severity" in b for b in body["buckets"])


def test_timeseries_raw_under_one_hour(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/timeseries?hours=1")
    assert r.status_code == 200
    body = r.json()
    assert body["bucket_s"] == 0
    assert "points" in body
    assert "series" not in body


def test_timeseries_downsampled_above_one_hour(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/timeseries?hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body["bucket_s"] == 120
    assert "series" in body
    assert "points" not in body


def _seed_xmr(store: Store, *, count: int = 15, step_s: int = 300) -> None:
    """Seed enough ICMP probes (one per 5-min bin) for xmr_charts to return one chart.

    The XmR endpoint uses real wall-clock now() for its window, so the seed
    must be anchored to now() rather than the test fixture's frozen datetime.
    """
    from datetime import UTC, datetime as _dt
    base = _dt.now(UTC) - timedelta(seconds=step_s * (count - 1))
    for i in range(count):
        store.insert_ping(mk_ping(
            ts=base + timedelta(seconds=i * step_s),
            kind="icmp", success=True, latency_ms=10.0 + (i % 3) * 0.3,
        ))


def test_xmr_page_serves_html(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/xmr")
    assert r.status_code == 200
    assert "XmR" in r.text


def test_xmr_endpoint_returns_chart(tmp_db) -> None:
    s = Store(tmp_db)
    _seed_xmr(s)
    s.close()
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/xmr?hours=24&bucket_s=300&kind=icmp")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "icmp"
    assert body["bucket_s"] == 300
    assert len(body["charts"]) == 1
    chart = body["charts"][0]
    assert chart["center"] is not None
    assert chart["unpl"] > chart["center"] > chart["lnpl"]
    assert len(chart["points"]) >= 10
    # Every point has the expected shape.
    for p in chart["points"]:
        assert {"ts", "value", "samples", "mr", "signals"} <= p.keys()


def test_xmr_endpoint_rejects_bad_kind(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/xmr?kind=udp")
    assert r.status_code == 400


def test_xmr_endpoint_rejects_zero_bucket(tmp_db) -> None:
    cfg = Config.defaults()
    cfg = type(cfg)(**{**cfg.__dict__, "db_path": tmp_db})
    client = TestClient(create_app(cfg))
    r = client.get("/api/xmr?bucket_s=0")
    assert r.status_code == 400
