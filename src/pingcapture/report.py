"""Bilingual report generation. Pulls data from a Store, renders via Jinja2."""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from . import __version__
from .analytics import (
    Outage,
    PathChange,
    buffer_bloat_score,
    detect_outages,
    latency_stats,
    mtr_path_changes,
    uptime_pct,
)
from .config import Config
from .i18n import Translator
from .storage import Store


@dataclass(frozen=True)
class ReportInputs:
    start: datetime
    end: datetime
    lang: str = "en"
    owner: str = ""


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
    if fmt not in {"html", "md"}:
        raise ValueError(f"unsupported format: {fmt}")
    t = Translator(inputs.lang)
    pings = store.pings_between(inputs.start, inputs.end)
    mtr_runs = store.mtr_runs_between(inputs.start, inputs.end)
    outages = detect_outages(pings)
    longest = max((o.duration_s for o in outages), default=0.0)
    total_out = sum(o.duration_s for o in outages)

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
        "latency": latency_stats(pings),
        "path_changes": [_path_change_view(c) for c in mtr_path_changes(mtr_runs)],
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


def write_report(out_path: Path, content: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")


__all__ = ["ReportInputs", "parse_since", "render_report", "write_report"]
