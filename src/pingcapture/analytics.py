"""Pure analytics over ping/mtr data. No I/O, fully unit-testable.

Outage definition: a contiguous time window during which *every* probe
(ICMP and TCP) that completed within the window failed. We only declare an
outage at the granularity of the rotation interval — single-target ICMP loss
to one host doesn't count, since the next rotation samples a different host.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .storage import MtrRun, PingResult


@dataclass(frozen=True)
class Outage:
    start: datetime
    end: datetime
    duration_s: float
    failed_probes: int
    affected_targets: list[str]


@dataclass(frozen=True)
class LatencyStats:
    target: str
    label: str
    samples: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    mean_ms: float | None
    success_pct: float


@dataclass(frozen=True)
class PathChange:
    target: str
    hop_idx: int
    old_ip: str | None
    new_ip: str | None
    at: datetime


def detect_outages(
    pings: Iterable[PingResult],
    *,
    min_consecutive_failures: int = 3,
    gap_tolerance_s: float = 60.0,
) -> list[Outage]:
    """Sliding outage detector.

    A failure window starts when ``min_consecutive_failures`` probes in a row
    fail (any target, any kind). It ends when a successful probe arrives, OR
    when more than ``gap_tolerance_s`` elapses with no probes (so a system
    sleep doesn't get charged as an outage).
    """
    sorted_pings = sorted(pings, key=lambda p: p.ts)
    if not sorted_pings:
        return []

    outages: list[Outage] = []
    in_outage = False
    streak_failures: list[PingResult] = []
    outage_start: datetime | None = None
    outage_failures: list[PingResult] = []
    last_ts: datetime | None = None

    def _close_outage(end_ts: datetime) -> None:
        nonlocal in_outage, outage_start, outage_failures
        if outage_start is None:
            return
        targets = sorted({p.target for p in outage_failures})
        outages.append(
            Outage(
                start=outage_start,
                end=end_ts,
                duration_s=(end_ts - outage_start).total_seconds(),
                failed_probes=len(outage_failures),
                affected_targets=targets,
            )
        )
        in_outage = False
        outage_start = None
        outage_failures = []

    for p in sorted_pings:
        # Treat a long quiet gap as an end-of-outage / reset.
        if last_ts is not None and (p.ts - last_ts).total_seconds() > gap_tolerance_s:
            if in_outage:
                _close_outage(last_ts)
            streak_failures = []
        last_ts = p.ts

        if p.success:
            if in_outage:
                _close_outage(p.ts)
            streak_failures = []
            continue

        streak_failures.append(p)
        if not in_outage and len(streak_failures) >= min_consecutive_failures:
            in_outage = True
            outage_start = streak_failures[0].ts
            outage_failures = list(streak_failures)
        elif in_outage:
            outage_failures.append(p)

    if in_outage and last_ts is not None:
        _close_outage(last_ts)

    return outages


def uptime_pct(pings: Iterable[PingResult]) -> float:
    total = 0
    ok = 0
    for p in pings:
        total += 1
        if p.success:
            ok += 1
    if total == 0:
        return 100.0
    return 100.0 * ok / total


def latency_stats(pings: Iterable[PingResult]) -> list[LatencyStats]:
    by_target: dict[str, list[PingResult]] = {}
    for p in pings:
        by_target.setdefault(p.target, []).append(p)
    out: list[LatencyStats] = []
    for target, items in sorted(by_target.items()):
        latencies = [p.latency_ms for p in items if p.success and p.latency_ms is not None]
        successes = sum(1 for p in items if p.success)
        success_pct = 100.0 * successes / len(items) if items else 0.0
        label = items[0].label if items else target
        if latencies:
            sorted_lat = sorted(latencies)
            out.append(
                LatencyStats(
                    target=target,
                    label=label,
                    samples=len(items),
                    p50_ms=_pct(sorted_lat, 50),
                    p95_ms=_pct(sorted_lat, 95),
                    p99_ms=_pct(sorted_lat, 99),
                    max_ms=max(sorted_lat),
                    mean_ms=statistics.fmean(sorted_lat),
                    success_pct=success_pct,
                )
            )
        else:
            out.append(
                LatencyStats(
                    target=target,
                    label=label,
                    samples=len(items),
                    p50_ms=None,
                    p95_ms=None,
                    p99_ms=None,
                    max_ms=None,
                    mean_ms=None,
                    success_pct=success_pct,
                )
            )
    return out


def _pct(sorted_values: list[float], pct: int) -> float:
    if not sorted_values:
        raise ValueError("empty")
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def mtr_path_changes(runs: Iterable[MtrRun]) -> list[PathChange]:
    """Detect IP changes per (target, hop_idx) over time."""
    changes: list[PathChange] = []
    last_ip_for: dict[tuple[str, int], str | None] = {}
    for run in sorted(runs, key=lambda r: r.ts):
        for hop in run.hops:
            key = (run.target, hop.hop_idx)
            if key in last_ip_for and last_ip_for[key] != hop.ip:
                changes.append(
                    PathChange(
                        target=run.target,
                        hop_idx=hop.hop_idx,
                        old_ip=last_ip_for[key],
                        new_ip=hop.ip,
                        at=run.ts,
                    )
                )
            last_ip_for[key] = hop.ip
    return changes


def buffer_bloat_score(pings: Iterable[PingResult]) -> float | None:
    """Standard deviation of ICMP latency in ms. High stddev under load
    is a signal of buffer bloat. Returns None if no successful samples."""
    latencies = [
        p.latency_ms for p in pings if p.kind == "icmp" and p.success and p.latency_ms is not None
    ]
    if len(latencies) < 2:
        return None
    return statistics.pstdev(latencies)


def window(now: datetime, *, hours: float | None = None, days: float | None = None) -> tuple[datetime, datetime]:
    delta = timedelta(hours=hours or 0, days=days or 0)
    return (now - delta, now)


__all__ = [
    "LatencyStats",
    "Outage",
    "PathChange",
    "buffer_bloat_score",
    "detect_outages",
    "latency_stats",
    "mtr_path_changes",
    "uptime_pct",
    "window",
]
