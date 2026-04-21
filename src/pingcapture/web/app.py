"""FastAPI app for the local web console."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .. import __version__
from ..analytics import (
    buffer_bloat_score,
    detect_outages,
    latency_stats,
    mtr_path_changes,
    uptime_pct,
    window,
)
from ..config import Config
from ..storage import Store

STATIC_INDEX = Path(__file__).parent / "static" / "index.html"


def _now() -> datetime:
    return datetime.now(UTC)


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="pingcapture", version=__version__)

    def _store() -> Store:
        # Open per-request — SQLite handles concurrent reads well in WAL mode.
        return Store(cfg.db_path)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if not STATIC_INDEX.exists():
            raise HTTPException(500, "static index missing")
        return HTMLResponse(STATIC_INDEX.read_text(encoding="utf-8"))

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        with _store() as store:
            recent = store.latest_pings(limit=20)
        # Status: DOWN if last 5 probes all failed; DEGRADED if >=2 of last 10 failed; else UP
        last_five = recent[:5]
        last_ten = recent[:10]
        down = bool(last_five) and all(not p.success for p in last_five)
        degraded = sum(1 for p in last_ten if not p.success) >= 2
        status = "DOWN" if down else ("DEGRADED" if degraded else "UP")
        return JSONResponse(
            {
                "status": status,
                "checked_at": _now().isoformat(timespec="seconds"),
                "last_event_at": recent[0].ts.isoformat() if recent else None,
                "version": __version__,
            }
        )

    @app.get("/api/summary")
    def api_summary(hours: float = 24.0) -> JSONResponse:
        start, end = window(_now(), hours=hours)
        with _store() as store:
            pings = store.pings_between(start, end)
            mtr_runs = store.mtr_runs_between(start, end)
        outages = detect_outages(pings)
        return JSONResponse(
            {
                "window_hours": hours,
                "uptime_pct": uptime_pct(pings),
                "outages": [
                    {
                        "start": o.start.isoformat(),
                        "end": o.end.isoformat(),
                        "duration_s": o.duration_s,
                        "failed_probes": o.failed_probes,
                        "affected_targets": o.affected_targets,
                    }
                    for o in outages
                ],
                "buffer_bloat_ms": buffer_bloat_score(pings),
                "latency": [
                    {
                        "target": s.target,
                        "label": s.label,
                        "samples": s.samples,
                        "success_pct": s.success_pct,
                        "p50_ms": s.p50_ms,
                        "p95_ms": s.p95_ms,
                        "p99_ms": s.p99_ms,
                        "max_ms": s.max_ms,
                        "mean_ms": s.mean_ms,
                    }
                    for s in latency_stats(pings)
                ],
                "path_changes": [
                    {
                        "target": c.target,
                        "hop_idx": c.hop_idx,
                        "old_ip": c.old_ip,
                        "new_ip": c.new_ip,
                        "at": c.at.isoformat(),
                    }
                    for c in mtr_path_changes(mtr_runs)
                ],
            }
        )

    @app.get("/api/timeseries")
    def api_timeseries(hours: float = 24.0, kind: str | None = None) -> JSONResponse:
        start, end = window(_now(), hours=hours)
        with _store() as store:
            pings = store.pings_between(start, end, kind=kind)
        return JSONResponse(
            {
                "points": [
                    {
                        "ts": p.ts.isoformat(),
                        "target": p.target,
                        "label": p.label,
                        "kind": p.kind,
                        "success": p.success,
                        "latency_ms": p.latency_ms,
                    }
                    for p in pings
                ]
            }
        )

    @app.get("/api/recent")
    def api_recent(limit: int = 50) -> JSONResponse:
        with _store() as store:
            recent = store.latest_pings(limit=limit)
        return JSONResponse(
            {
                "events": [
                    {
                        "ts": p.ts.isoformat(),
                        "target": p.target,
                        "label": p.label,
                        "kind": p.kind,
                        "success": p.success,
                        "latency_ms": p.latency_ms,
                        "error": p.error,
                    }
                    for p in recent
                ]
            }
        )

    return app


__all__ = ["create_app"]
