"""Bilingual report generation. Pulls data from a Store, renders via Jinja2."""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from . import __version__
from .analytics import (
    Outage,
    PathChange,
    bucket_outages,
    buffer_bloat_score,
    detect_outages,
    floor_to_hour,
    latency_stats,
    mtr_path_changes,
    uptime_pct,
)
from .config import Config
from .i18n import Translator
from .storage import Store


@dataclass(frozen=True)
class WindowOption:
    spec: str
    label: str
    active: bool


@dataclass(frozen=True)
class ReportInputs:
    start: datetime
    end: datetime
    lang: str = "en"
    owner: str = ""
    show_toolbar: bool = False
    since_spec: str = ""
    window_options: tuple[WindowOption, ...] = ()


def _format_duration(t: Translator, seconds: float) -> str:
    if seconds < 60:
        return t.t("unit_seconds", n=seconds)
    if seconds < 3600:
        return t.t("unit_minutes", n=seconds / 60)
    if seconds < 86400:
        return t.t("unit_hours", n=seconds / 3600)
    return t.t("unit_days", n=seconds / 86400)


def _fmt_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _outage_view(o: Outage, t: Translator) -> dict[str, object]:
    return {
        "start_str": _fmt_ts(o.start),
        "duration_str": _format_duration(t, o.duration_s),
        "failed_probes": o.failed_probes,
        "affected_targets": o.affected_targets,
    }


def _path_change_view(c: PathChange) -> dict[str, object]:
    return {
        "at_str": _fmt_ts(c.at),
        "target": c.target,
        "hop_idx": c.hop_idx,
        "old_ip": c.old_ip,
        "new_ip": c.new_ip,
    }


def _env() -> Environment:
    return Environment(
        loader=PackageLoader("pingcapture", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def render_report(
    *,
    cfg: Config,
    store: Store,
    inputs: ReportInputs,
    fmt: str = "html",
) -> str:
    """Render the bilingual report. ``fmt`` is retained for the template
    selector but only "html" is supported now that the report is web-served."""
    if fmt != "html":
        raise ValueError(f"unsupported format: {fmt} (only 'html' is supported)")
    t = Translator(inputs.lang)
    pings = store.pings_between(inputs.start, inputs.end)
    mtr_runs = store.mtr_runs_between(inputs.start, inputs.end)
    outages = detect_outages(pings)
    longest = max((o.duration_s for o in outages), default=0.0)
    total_out = sum(o.duration_s for o in outages)
    grid_start = floor_to_hour(inputs.start)
    grid_end = floor_to_hour(inputs.end) + timedelta(hours=1)
    buckets = bucket_outages(
        pings, window_start=grid_start, window_end=grid_end, bucket_size_s=3600.0
    )

    context: dict[str, object] = {
        "lang": inputs.lang,
        "t": t,
        "owner": inputs.owner or getpass.getuser(),
        "version": __version__,
        "start_str": _fmt_ts(inputs.start),
        "end_str": _fmt_ts(inputs.end),
        "duration_str": _format_duration(t, (inputs.end - inputs.start).total_seconds()),
        "generated_at_str": _fmt_ts(datetime.now(UTC)),
        "uptime_pct_value": uptime_pct(pings),
        "outages": [_outage_view(o, t) for o in outages],
        "total_outage_str": _format_duration(t, total_out),
        "longest_outage_str": _format_duration(t, longest),
        "buffer_bloat": buffer_bloat_score(pings),
        "icmp_interval": cfg.icmp_interval_s,
        "icmp_target_count": len(cfg.icmp_targets),
        "tcp_interval": cfg.tcp_interval_s,
        "mtr_interval_min": int(cfg.mtr_interval_s // 60),
        "show_toolbar": inputs.show_toolbar,
        "since_spec": inputs.since_spec,
        "window_options": [
            {"spec": o.spec, "label": o.label, "active": o.active}
            for o in inputs.window_options
        ],
        "latency": latency_stats(pings),
        "path_changes": [_path_change_view(c) for c in mtr_path_changes(mtr_runs)],
        "grid_buckets": [
            {
                "severity": b.severity,
                "start_str": _fmt_ts(b.start),
                "uptime_pct": b.uptime_pct,
                "outage_count": b.outage_count,
                "longest_outage_s": b.longest_outage_s,
                "longest_outage_str": _format_duration(t, b.longest_outage_s) if b.longest_outage_s > 0 else "",
            }
            for b in buckets
        ],
        "grid_start_str": _fmt_ts(grid_start),
        "grid_end_str": _fmt_ts(grid_end),
    }
    template = _env().get_template(f"report.{fmt}.j2")
    return template.render(**context)


def parse_since(spec: str) -> timedelta:
    """Parse spec like '7d', '24h', '90m', '2w'."""
    spec = spec.strip().lower()
    if not spec:
        raise ValueError("empty since spec")
    unit = spec[-1]
    try:
        value = float(spec[:-1])
    except ValueError as e:
        raise ValueError(f"bad since spec: {spec!r}") from e
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    if unit == "w":
        return timedelta(weeks=value)
    raise ValueError(f"unknown unit in since spec: {spec!r}")


__all__ = ["ReportInputs", "WindowOption", "parse_since", "render_report"]
