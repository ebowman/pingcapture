"""FastAPI app for the local web console."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .. import __version__
from ..analytics import (
    bucket_outages,
    bucket_size_for_window,
    buffer_bloat_score,
    detect_outages,
    downsample_latency,
    floor_to_hour,
    latency_stats,
    mtr_path_changes,
    uptime_pct,
    window,
)
from ..config import Config
from ..i18n import supported_languages
from ..report import ReportInputs, WindowOption, parse_since, render_report
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

    @app.get("/report", response_class=HTMLResponse)
    def report_page(
        lang: str = Query("en"),
        since: str = Query("7d"),
        owner: str = Query(""),
    ) -> HTMLResponse:
        if lang not in supported_languages():
            raise HTTPException(400, f"unsupported lang: {lang}")
        try:
            delta = parse_since(since)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        end = _now()
        start = end - delta
        window_specs = [("24h", "24h"), ("7d", "7d"), ("30d", "30d"), ("90d", "90d")]
        with _store() as store:
            html = render_report(
                cfg=cfg,
                store=store,
                inputs=ReportInputs(
                    start=start,
                    end=end,
                    lang=lang,
                    owner=owner,
                    show_toolbar=True,
                    since_spec=since,
                    window_options=tuple(
                        WindowOption(spec=spec, label=label, active=(spec == since))
                        for spec, label in window_specs
                    ),
                ),
                fmt="html",
            )
        return HTMLResponse(html)

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
        bucket_s = bucket_size_for_window(hours)
        with _store() as store:
            pings = store.pings_between(start, end, kind=kind)
        if bucket_s == 0:
            return JSONResponse(
                {
                    "bucket_s": 0,
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
                    ],
                }
            )
        series = downsample_latency(
            pings, window_start=start, bucket_size_s=bucket_s
        )
        return JSONResponse(
            {
                "bucket_s": bucket_s,
                "series": [
                    {
                        "ts": b.ts.isoformat(),
                        "target": b.target,
                        "label": b.label,
                        "kind": b.kind,
                        "samples": b.samples,
                        "failed": b.failed,
                        "p50_ms": b.p50_ms,
                        "p95_ms": b.p95_ms,
                    }
                    for b in series
                ],
            }
        )

    @app.get("/api/buckets")
    def api_buckets(days: float = 30.0, bucket_hours: float = 1.0) -> JSONResponse:
        # Align the window to the hour boundary so grid columns line up with
        # wall-clock hours. The current (partial) hour is included.
        now = _now()
        end = floor_to_hour(now) + timedelta(hours=1)
        start = floor_to_hour(end - timedelta(days=days))
        if bucket_hours == 1.0:
            buckets = _buckets_from_materialized(start, end)
        else:
            # Non-hourly buckets are a legacy code path; recompute from raw.
            with _store() as store:
                pings = store.pings_between(start, end)
            buckets = bucket_outages(
                pings,
                window_start=start,
                window_end=end,
                bucket_size_s=bucket_hours * 3600.0,
            )
        return JSONResponse(
            {
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "bucket_hours": bucket_hours,
                "buckets": [
                    {
                        "start": b.start.isoformat(),
                        "end": b.end.isoformat(),
                        "samples": b.samples,
                        "failed": b.failed,
                        "longest_outage_s": b.longest_outage_s,
                        "outage_count": b.outage_count,
                        "uptime_pct": b.uptime_pct,
                        "severity": b.severity,
                    }
                    for b in buckets
                ],
            }
        )

    def _buckets_from_materialized(
        start: datetime, end: datetime
    ) -> list:
        from ..analytics import Bucket, SEVERITY_NO_DATA

        with _store() as store:
            rows = store.hourly_buckets_between(start, end)
        by_hour = {row[0]: row for row in rows}
        out: list[Bucket] = []
        cur = start
        one_hour = timedelta(hours=1)
        while cur < end:
            row = by_hour.get(cur)
            if row is None:
                out.append(
                    Bucket(
                        start=cur, end=cur + one_hour,
                        samples=0, failed=0,
                        longest_outage_s=0.0, outage_count=0,
                        severity=SEVERITY_NO_DATA,
                    )
                )
            else:
                _, samples, failed, longest, oc, sev = row
                out.append(
                    Bucket(
                        start=cur, end=cur + one_hour,
                        samples=samples, failed=failed,
                        longest_outage_s=longest, outage_count=oc,
                        severity=sev,
                    )
                )
            cur += one_hour
        return out

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
