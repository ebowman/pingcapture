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
