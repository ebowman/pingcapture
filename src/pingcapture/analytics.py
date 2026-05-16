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
from datetime import UTC, datetime, timedelta

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


# Minimum number of distinct ICMP targets that must fail within a streak for
# the streak to qualify as a real outage on ICMP evidence alone. One or two
# targets failing while TCP succeeds is typically upstream ICMP rate-limiting
# or per-host deprioritisation; failures across three or more independent
# anycast destinations are hard to explain except as actual link loss.
OUTAGE_MIN_DISTINCT_ICMP_TARGETS = 3

# Error-substring fingerprints for DNS resolution failures. If a failure in a
# streak names DNS resolution as the cause, that's independent of any single
# host going dark or of ICMP rate-limiting (resolution uses UDP/53 on the
# nearest resolver), and so counts as evidence of real upstream loss.
_DNS_ERROR_FINGERPRINTS = ("NameLookupError", "Name or service not known",
                           "nodename nor servname", "Temporary failure in name resolution")


def _is_dns_failure(p: PingResult) -> bool:
    err = (p.error or "")
    return any(fp in err for fp in _DNS_ERROR_FINGERPRINTS)


def _streak_is_outage(failures: list[PingResult]) -> bool:
    """The single decision point: does this failure streak count as an outage?

    Everything in the codebase that asks "is this an outage?" routes through
    detect_outages(), which routes through this predicate. Change the rule
    here and every consumer (web /api/summary, bucket_outages, hourly
    materialized buckets in storage, call_quality_pct, report.py) sees
    the new behaviour automatically.

    A streak is an outage when ANY of these hold:
      (a) it contains at least one TCP failure — the historical rule, TCP is
          the user-visible path so a TCP failure is by definition a real
          connectivity event;
      (b) it contains failures across at least
          OUTAGE_MIN_DISTINCT_ICMP_TARGETS distinct ICMP targets — three or
          more independent anycast destinations failing together is
          essentially impossible to explain except as upstream loss;
      (c) it contains a DNS resolution failure — DNS uses a different
          transport and a different upstream path from ICMP echo, so a
          NameLookupError during a failure streak corroborates a real outage.

    Otherwise the streak demotes to flicker (single-target ICMP rate-limiting,
    transient per-host shaping) and does not appear as an outage.
    """
    if any(p.kind == "tcp" for p in failures):
        return True
    if any(_is_dns_failure(p) for p in failures):
        return True
    icmp_targets = {p.target for p in failures if p.kind == "icmp"}
    if len(icmp_targets) >= OUTAGE_MIN_DISTINCT_ICMP_TARGETS:
        return True
    return False


def detect_outages(
    pings: Iterable[PingResult],
    *,
    min_consecutive_failures: int = 3,
    gap_tolerance_s: float = 60.0,
) -> list[Outage]:
    """Sliding outage detector.

    A failure streak starts when ``min_consecutive_failures`` probes in a row
    fail (any target, any kind). It ends when a successful probe arrives, OR
    when more than ``gap_tolerance_s`` elapses with no probes (so a system
    sleep doesn't get charged as an outage).

    Whether a streak qualifies as an outage (vs. flicker) is decided by
    ``_streak_is_outage`` — the single source of truth for that distinction.
    See its docstring for the rule.
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
        if not _streak_is_outage(outage_failures):
            in_outage = False
            outage_start = None
            outage_failures = []
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


# Video-call uptime thresholds. ITU/Cisco guidance for acceptable
# interactive voice/video is one-way ≤150 ms latency, ≤30 ms jitter, ≤1 % loss.
# Probes here measure round-trip, so the latency budget doubles to 300 ms.
# Jitter is measured as IQR (p75 - p25) rather than stddev, because per-minute
# probe counts are small (~12) and stddev is too easily moved by a single
# outlier — a real video call would not notice one isolated RTT spike. The
# IQR threshold is loosened from the ITU 30 ms (which targets one-way
# inter-packet variation on managed networks) to 75 ms, since round-trip
# probes over the open internet have naturally higher spread. The loss
# check looks only at TCP probes — ICMP-only loss is upstream rate-limiting,
# not user-visible (see detect_outages for the same rule).
#
# Latency check is *sustained-only*. A single high-RTT probe in an otherwise
# clean minute doesn't reflect call-quality degradation — a real video call's
# jitter buffer absorbs isolated spikes. So we require EITHER (a) the median
# RTT itself to be elevated (more than half the probes were slow) OR (b) at
# least VIDEO_CALL_MIN_SLOW_PROBES probes individually breached the budget.
# Either condition is consistent with sustained network degradation; neither
# fires on one outlier. The p95 threshold no longer drives the decision —
# kept only as a reporting field on the QualityEvent so the rule's history
# stays legible from the data. See pingcapture-9mm.
VIDEO_CALL_WINDOW_S = 60
VIDEO_CALL_MAX_LOSS_PCT = 1.0
VIDEO_CALL_MAX_P95_RTT_MS = 300.0           # reporting-only, no longer triggers
VIDEO_CALL_MAX_P50_RTT_MS = 200.0           # NEW: median (sustained) RTT cap
VIDEO_CALL_MIN_SLOW_PROBES = 3              # NEW: probes > P95 cap to count as sustained
VIDEO_CALL_MAX_JITTER_MS = 75.0


# Quality event reason codes — the lever that explains why a minute fell
# below video-call quality without there being a detected connectivity outage.
QUALITY_REASON_LOSS = "loss"
QUALITY_REASON_LATENCY = "latency"
QUALITY_REASON_JITTER = "jitter"


@dataclass(frozen=True)
class QualityEvent:
    """A 60-second window where call quality fell below threshold without
    there being a detected connectivity outage.

    A user reading the dashboard would otherwise see uptime < 100% with zero
    outages and be confused; quality events explain that gap. The reason
    field is the *first* threshold that triggered (in order: loss, latency,
    jitter) since one event often crosses several at once."""
    start: datetime
    end: datetime
    reason: str               # QUALITY_REASON_*
    worst_metric: float       # the value that tripped the threshold
    threshold: float          # what it was compared against
    samples: int              # probes in the minute
    affected_targets: list[str]


def _classify_minute(
    items: list[PingResult],
) -> tuple[str, float, float] | None:
    """The shared rule: classify a 60s minute as good or as failing one of
    the video-call thresholds. Returns None for a good minute, or
    (reason, worst_metric, threshold) for a bad one.

    Used by BOTH call_quality_pct and quality_events so the two cannot
    drift. The outage-overlap check is the caller's responsibility — it
    needs the outage list, which this helper doesn't take.

    Order of checks matters for *labelling* (loss is reported in preference
    to latency, latency in preference to jitter) but not for the accept/reject
    decision: a minute that trips any check is bad. Ordering is loss first
    because TCP failure is the most user-visible symptom, then latency
    (perceptible mid-call), then jitter (audible as choppiness)."""
    tcp_items = [p for p in items if p.kind == "tcp"]
    if tcp_items:
        tcp_failed = sum(1 for p in tcp_items if not p.success)
        tcp_loss_pct = 100.0 * tcp_failed / len(tcp_items)
        if tcp_loss_pct > VIDEO_CALL_MAX_LOSS_PCT:
            return (QUALITY_REASON_LOSS, tcp_loss_pct, VIDEO_CALL_MAX_LOSS_PCT)
    latencies = sorted(
        p.latency_ms for p in items
        if p.success and p.latency_ms is not None
    )
    if latencies:
        # Sustained-latency check. Either condition is enough on its own.
        p50 = _pct(latencies, 50)
        slow_count = sum(1 for v in latencies if v > VIDEO_CALL_MAX_P95_RTT_MS)
        if p50 > VIDEO_CALL_MAX_P50_RTT_MS:
            # Median elevated → more than half the probes were slow. That's
            # sustained. Report the median itself as the worst metric so the
            # number on the dashboard tells the right story.
            return (QUALITY_REASON_LATENCY, p50, VIDEO_CALL_MAX_P50_RTT_MS)
        if slow_count >= VIDEO_CALL_MIN_SLOW_PROBES:
            # At least N probes individually exceeded the 300ms budget —
            # consistent with sustained congestion across multiple samples,
            # not a single jitter buffer-absorbed spike. Report p95 as the
            # worst metric (still informative; it's what tripped the count).
            p95 = _pct(latencies, 95)
            return (QUALITY_REASON_LATENCY, p95, VIDEO_CALL_MAX_P95_RTT_MS)
        if len(latencies) >= 4:
            iqr = _pct(latencies, 75) - _pct(latencies, 25)
            if iqr > VIDEO_CALL_MAX_JITTER_MS:
                return (QUALITY_REASON_JITTER, iqr, VIDEO_CALL_MAX_JITTER_MS)
    return None


def _bucket_pings_by_minute(
    pings: Iterable[PingResult],
) -> dict[datetime, list[PingResult]]:
    """Floor every probe to its containing minute. Single helper used by
    both call_quality_pct and quality_events."""
    buckets: dict[datetime, list[PingResult]] = {}
    for p in pings:
        key = p.ts.replace(second=0, microsecond=0)
        buckets.setdefault(key, []).append(p)
    return buckets


def _minute_overlaps_outage(
    bucket_start: datetime,
    outages: list[Outage],
    window: timedelta,
) -> bool:
    bucket_end = bucket_start + window
    for o in outages:
        if o.start < bucket_end and o.end > bucket_start:
            return True
    return False


def connectivity_uptime_pct(
    pings: Iterable[PingResult],
    outages: Iterable[Outage],
) -> float:
    """Connectivity uptime: fraction of 60s windows that did NOT overlap a
    detected outage.

    This is the metric that maps to the natural-English reading of
    "uptime" — "was the line up?". By construction it equals 100.0 iff
    the outage list is empty (modulo windows with no probes, which are
    excluded from the denominator the same way the other metrics handle
    capture gaps). That invariant is asserted by a unit test so the
    "0 outages but uptime < 100%" complaint cannot recur.

    Note this is INTENTIONALLY decoupled from call quality. A minute
    with one 1100ms RTT spike is bad for a call but the line was up;
    that minute counts as connectivity-up here, and shows up as a
    quality event in ``quality_events()``. See pingcapture-qnx for
    the architectural rationale.
    """
    window = timedelta(seconds=VIDEO_CALL_WINDOW_S)
    buckets = _bucket_pings_by_minute(pings)
    if not buckets:
        return 100.0
    outage_list = list(outages)
    bad = sum(
        1 for bucket_start in buckets
        if _minute_overlaps_outage(bucket_start, outage_list, window)
    )
    return 100.0 * (len(buckets) - bad) / len(buckets)


def call_quality_pct(
    pings: Iterable[PingResult],
    outages: Iterable[Outage],
) -> float:
    """Call quality: fraction of 60s windows that would have sustained a
    clean video call.

    A window is BAD if any of:
      - it overlaps a detected outage,
      - TCP packet loss in the window exceeds VIDEO_CALL_MAX_LOSS_PCT
        (ICMP-only loss is ignored, matching the outage detector — it
        usually reflects upstream ICMP rate-limiting, not real loss),
      - latency is sustained-high (p50 > VIDEO_CALL_MAX_P50_RTT_MS or
        ≥VIDEO_CALL_MIN_SLOW_PROBES probes exceed VIDEO_CALL_MAX_P95_RTT_MS),
      - inter-quartile RTT spread (p75-p25) exceeds VIDEO_CALL_MAX_JITTER_MS.

    The threshold checks route through ``_classify_minute`` so the rule is
    a single source of truth shared with ``quality_events``.

    Windows with no probes are excluded from the denominator — gaps in
    capture don't count for or against the metric.

    Renamed from video_call_uptime_pct in pingcapture-qnx because the
    name "uptime" invited misreading — see that issue for context.
    """
    window = timedelta(seconds=VIDEO_CALL_WINDOW_S)
    buckets = _bucket_pings_by_minute(pings)
    if not buckets:
        return 100.0
    outage_list = list(outages)
    good = 0
    for bucket_start, items in buckets.items():
        if _minute_overlaps_outage(bucket_start, outage_list, window):
            continue
        if _classify_minute(items) is None:
            good += 1
    return 100.0 * good / len(buckets)


def quality_events(
    pings: Iterable[PingResult],
    outages: Iterable[Outage],
) -> list[QualityEvent]:
    """Minutes that failed the video-call quality rule for reasons OTHER
    than overlapping a detected outage.

    A user reading the dashboard sees outage rows for connectivity loss,
    but the same uptime metric also penalises high latency, jitter, and
    TCP loss inside minutes that *didn't* trigger an outage. Without
    surfacing those, the dashboard can show 'uptime 98.4%, outages 0'
    which reads as a contradiction. Quality events make that gap legible.

    Adjacent minutes with the same reason are NOT merged here — each minute
    is its own event. Coalescing is a presentation concern that should sit
    in the UI, not the analytics layer. (If we need it later, add a
    ``coalesce_quality_events`` helper.)"""
    window = timedelta(seconds=VIDEO_CALL_WINDOW_S)
    buckets = _bucket_pings_by_minute(pings)
    outage_list = list(outages)
    events: list[QualityEvent] = []
    for bucket_start in sorted(buckets):
        items = buckets[bucket_start]
        if _minute_overlaps_outage(bucket_start, outage_list, window):
            continue
        classification = _classify_minute(items)
        if classification is None:
            continue
        reason, worst, threshold = classification
        targets = sorted({p.target for p in items})
        events.append(
            QualityEvent(
                start=bucket_start,
                end=bucket_start + window,
                reason=reason,
                worst_metric=worst,
                threshold=threshold,
                samples=len(items),
                affected_targets=targets,
            )
        )
    return events


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


@dataclass(frozen=True)
class LatencyBucket:
    """One time-bucket of latency samples for a single (target, kind)."""
    ts: datetime
    target: str
    label: str
    kind: str
    samples: int
    failed: int
    p50_ms: float | None
    p95_ms: float | None


def bucket_size_for_window(hours: float) -> int:
    """Pick a sensible bucket size in seconds for a given window width.

    0 means "do not bucket; return raw samples". Past ~1 hour the chart cannot
    show per-probe detail anyway, so we collapse aggressively.
    """
    if hours <= 1.0:
        return 0
    if hours <= 6.0:
        return 30
    if hours <= 24.0:
        return 120
    return 900


def downsample_latency(
    pings: Iterable[PingResult],
    *,
    window_start: datetime,
    bucket_size_s: int,
) -> list[LatencyBucket]:
    """Group pings into time-buckets per (target, kind) with p50/p95/counts.

    Buckets are aligned to ``window_start``. Only successful probes with a
    latency_ms value contribute to the percentile; failures still get counted
    in ``failed`` so the chart can mark gaps.
    """
    if bucket_size_s <= 0:
        return []
    buckets: dict[tuple[int, str, str], list[PingResult]] = {}
    for p in pings:
        idx = int((p.ts - window_start).total_seconds() // bucket_size_s)
        buckets.setdefault((idx, p.target, p.kind), []).append(p)
    out: list[LatencyBucket] = []
    delta = timedelta(seconds=bucket_size_s)
    for (idx, target, kind), items in buckets.items():
        ts = window_start + delta * idx
        latencies = sorted(
            p.latency_ms for p in items
            if p.success and p.latency_ms is not None
        )
        failed = sum(1 for p in items if not p.success)
        out.append(
            LatencyBucket(
                ts=ts,
                target=target,
                label=items[0].label,
                kind=kind,
                samples=len(items),
                failed=failed,
                p50_ms=_pct(latencies, 50) if latencies else None,
                p95_ms=_pct(latencies, 95) if latencies else None,
            )
        )
    out.sort(key=lambda b: (b.target, b.kind, b.ts))
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


# Severity thresholds for the status grid. Each bucket gets the level whose
# threshold its longest outage exceeds. None means no failed probes at all.
SEVERITY_NONE = "none"          # green — zero failures
SEVERITY_NOMINAL = "nominal"    # pale yellow-green — scattered loss, >=99% uptime, no >5s span
SEVERITY_FLICKER = "flicker"    # yellow — noticeable loss (<99% uptime) or 5-15s span
SEVERITY_MINOR = "minor"        # orange — > 15s outage
SEVERITY_MAJOR = "major"        # red — > 30s outage
SEVERITY_SEVERE = "severe"      # dark red — >= 5min outage
SEVERITY_NO_DATA = "no_data"    # grey

# Mixed-kind loss (TCP failed too) uses this floor: <99% combined uptime
# tips a bucket into FLICKER. 1% loss in a 700-probe hour is ~7 dropped
# probes; any more than that pulls the eye in the grid.
NOMINAL_UPTIME_FLOOR = 99.0


@dataclass(frozen=True)
class Bucket:
    start: datetime
    end: datetime
    samples: int
    failed: int
    longest_outage_s: float
    outage_count: int
    severity: str

    @property
    def uptime_pct(self) -> float:
        if self.samples == 0:
            return 0.0
        return 100.0 * (self.samples - self.failed) / self.samples


def _severity_for(
    longest_s: float,
    failed: int,
    samples: int,
    *,
    tcp_failed: int = 0,
    icmp_failed: int | None = None,
    icmp_samples: int | None = None,
) -> str:
    """Classify a bucket. Bigger picture in pingcapture-pyt, pingcapture-ht4,
    and pingcapture-66j.

    TCP is the authoritative signal — TCP failing means user-visible breakage.
    If TCP never failed and no outage was opened, ICMP-only loss is invisible
    to the user (upstream ICMP shaping) and the bucket stays SEVERITY_NONE,
    matching how call_quality_pct treats the same data.
    """
    if samples == 0:
        return SEVERITY_NO_DATA
    if failed == 0:
        return SEVERITY_NONE
    if longest_s >= 300:
        return SEVERITY_SEVERE
    if longest_s > 30:
        return SEVERITY_MAJOR
    if longest_s > 15:
        return SEVERITY_MINOR
    if longest_s > 5:
        return SEVERITY_FLICKER
    # No outage span >5s. Look at the loss pattern.
    if tcp_failed == 0:
        # No TCP failure and no outage opened: ICMP-only shaping is invisible.
        return SEVERITY_NONE
    # TCP failed (but didn't cluster enough to open an outage). Use combined
    # uptime to decide between nominal and flicker.
    uptime = 100.0 * (samples - failed) / samples
    if uptime >= NOMINAL_UPTIME_FLOOR:
        return SEVERITY_NOMINAL
    return SEVERITY_FLICKER


def bucket_outages(
    pings: Iterable[PingResult],
    *,
    window_start: datetime,
    window_end: datetime,
    bucket_size_s: float = 3600.0,
) -> list[Bucket]:
    """Bucket pings into fixed-size windows and classify each by worst outage.

    Buckets are aligned to ``window_start`` and stepped by ``bucket_size_s``.
    Outages are computed per-bucket using the same detector as the rest of the
    system, so a bucket that contains a single 90-second outage is classified
    by *that* outage, not by the bucket's overall uptime percentage.
    """
    if window_end <= window_start:
        return []
    sorted_pings = sorted(
        (p for p in pings if window_start <= p.ts < window_end),
        key=lambda p: p.ts,
    )

    bucket_count = int((window_end - window_start).total_seconds() / bucket_size_s) + 1
    buckets: list[list[PingResult]] = [[] for _ in range(bucket_count)]
    for p in sorted_pings:
        idx = int((p.ts - window_start).total_seconds() / bucket_size_s)
        if 0 <= idx < bucket_count:
            buckets[idx].append(p)

    out: list[Bucket] = []
    delta = timedelta(seconds=bucket_size_s)
    for i, items in enumerate(buckets):
        b_start = window_start + delta * i
        b_end = b_start + delta
        if b_end > window_end:
            b_end = window_end
        outages = detect_outages(items)
        longest = max((o.duration_s for o in outages), default=0.0)
        failed = sum(1 for p in items if not p.success)
        tcp_failed = sum(1 for p in items if p.kind == "tcp" and not p.success)
        icmp_items = [p for p in items if p.kind == "icmp"]
        icmp_failed = sum(1 for p in icmp_items if not p.success)
        out.append(
            Bucket(
                start=b_start,
                end=b_end,
                samples=len(items),
                failed=failed,
                longest_outage_s=longest,
                outage_count=len(outages),
                severity=_severity_for(
                    longest, failed, len(items),
                    tcp_failed=tcp_failed,
                    icmp_failed=icmp_failed,
                    icmp_samples=len(icmp_items),
                ),
            )
        )
    # Trim the final bucket if its window collapsed to nothing.
    while out and out[-1].start >= window_end:
        out.pop()
    return out


def floor_to_hour(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.replace(minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class CalendarRow:
    """One row of the report's calendar grid: a day with 24 hourly cells."""
    date: datetime  # midnight UTC of the day
    cells: list[Bucket]  # length 24, indexed by hour-of-day; missing hours = no_data


def pivot_buckets_by_day(buckets: list[Bucket]) -> list[CalendarRow]:
    """Reshape a flat list of hourly buckets into one row per day.

    Each row has exactly 24 cells, indexed 0..23 by UTC hour. Missing hours
    (e.g. partial first/last day) are filled with synthetic no-data buckets so
    the calendar always renders as a clean rectangle. Rows are sorted with the
    most recent day first — that's the order people actually scan a report in.
    """
    by_day: dict[datetime, dict[int, Bucket]] = {}
    for b in buckets:
        ts = b.start if b.start.tzinfo else b.start.replace(tzinfo=UTC)
        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(day, {})[ts.hour] = b
    rows: list[CalendarRow] = []
    for day in sorted(by_day.keys(), reverse=True):
        hours = by_day[day]
        cells: list[Bucket] = []
        for h in range(24):
            if h in hours:
                cells.append(hours[h])
            else:
                slot_start = day.replace(hour=h)
                cells.append(
                    Bucket(
                        start=slot_start,
                        end=slot_start + timedelta(hours=1),
                        samples=0, failed=0,
                        longest_outage_s=0.0, outage_count=0,
                        severity=SEVERITY_NO_DATA,
                    )
                )
        rows.append(CalendarRow(date=day, cells=cells))
    return rows


# Severity ordering for "worst severity" aggregation. Higher = worse.
_SEVERITY_RANK = {
    SEVERITY_NO_DATA: -1,  # don't let absent data dominate the aggregate
    SEVERITY_NONE: 0,
    SEVERITY_NOMINAL: 1,
    SEVERITY_FLICKER: 2,
    SEVERITY_MINOR: 3,
    SEVERITY_MAJOR: 4,
    SEVERITY_SEVERE: 5,
}


@dataclass(frozen=True)
class HourSummary:
    """Aggregated severity for a single hour-of-day across the whole window."""
    hour: int  # 0..23
    days_observed: int
    days_with_failure: int
    worst_severity: str
    total_outage_s: float


def summarize_by_hour_of_day(buckets: list[Bucket]) -> list[HourSummary]:
    """Collapse hourly buckets into 24 hour-of-day summaries.

    For each hour 0..23, returns the worst severity seen at that hour over the
    window, plus simple counts. Useful for surfacing time-of-day patterns the
    eye misses in a long calendar grid.
    """
    by_hour: dict[int, list[Bucket]] = {h: [] for h in range(24)}
    for b in buckets:
        ts = b.start if b.start.tzinfo else b.start.replace(tzinfo=UTC)
        by_hour[ts.hour].append(b)
    out: list[HourSummary] = []
    for hour in range(24):
        items = by_hour[hour]
        observed = [b for b in items if b.severity != SEVERITY_NO_DATA]
        with_fail = [b for b in observed if b.failed > 0]
        if observed:
            worst = max(observed, key=lambda b: _SEVERITY_RANK[b.severity]).severity
        else:
            worst = SEVERITY_NO_DATA
        out.append(
            HourSummary(
                hour=hour,
                days_observed=len(observed),
                days_with_failure=len(with_fail),
                worst_severity=worst,
                total_outage_s=sum(b.longest_outage_s for b in observed),
            )
        )
    return out


# ---------------------------------------------------------------------------
# XmR / process behaviour charts (Donald Wheeler, "Understanding Variation").
#
# An XmR chart pairs a chart of *individual* values with a chart of the
# *moving range* between successive values. The natural process limits are
# derived from the moving range itself — no assumption of normality, no
# arbitrary thresholds. Constants below are Wheeler's:
#
#   UNPL/LNPL for individuals  = mean ± 2.660 · MR-bar
#   URL for moving range       = 3.268 · MR-bar
#
# 2.660 = 3 / d2 with d2 = 1.128 for subgroup size n=2 (the moving range of
# consecutive points). 3.268 = D4 for n=2.
#
# Wheeler's "Western Electric / Detection Rules" for signals of special cause:
#   1. A single point outside the natural process limits.
#   2. Eight successive points on the same side of the centre line ("run").
#   3. Three of four successive points beyond 2-sigma (two-thirds of the way
#      from the centre to a limit) on the same side.
#   4. Six successive points steadily increasing or steadily decreasing
#      ("trend"). Strict monotonic — equal values break the run.
# ---------------------------------------------------------------------------

XMR_NPL_CONSTANT = 2.660    # 3 / d2 for n=2
XMR_MR_CONSTANT = 3.268     # D4 for n=2


@dataclass(frozen=True)
class XmRPoint:
    """One individual observation in an XmR chart."""
    ts: datetime
    value: float           # the individual (e.g. median RTT for the bin)
    samples: int           # number of probes that produced the value
    mr: float | None       # moving range to the previous point; None for first
    signals: list[str]     # which Wheeler rules fired AT this point


@dataclass(frozen=True)
class XmRChart:
    """A complete XmR chart for one (target, kind) pair."""
    target: str
    label: str
    kind: str
    bucket_size_s: int
    center: float | None         # mean of individuals
    unpl: float | None           # upper natural process limit
    lnpl: float | None           # lower natural process limit (clamped at 0)
    mr_bar: float | None         # mean moving range
    mr_url: float | None         # URL for the MR chart (3.268 · MR-bar)
    points: list[XmRPoint]


def _xmr_bucket_pings(
    pings: Iterable[PingResult],
    *,
    window_start: datetime,
    bucket_size_s: int,
) -> dict[tuple[str, str], list[tuple[datetime, float, int]]]:
    """Group successful pings into (target, kind) -> sorted [(ts, p50, n), ...].

    The "individual" we chart is the median of successful RTTs in the bucket.
    Failed probes count toward ``samples`` only via the caller's bookkeeping;
    here we just want a stable, robust per-bucket value. A bucket with no
    successful probes is omitted from the series (treated as a gap, not zero).
    """
    if bucket_size_s <= 0:
        raise ValueError("bucket_size_s must be > 0")
    grouped: dict[tuple[int, str, str], list[PingResult]] = {}
    labels: dict[str, str] = {}
    for p in pings:
        if p.latency_ms is None or not p.success:
            continue
        idx = int((p.ts - window_start).total_seconds() // bucket_size_s)
        grouped.setdefault((idx, p.target, p.kind), []).append(p)
        labels.setdefault(p.target, p.label)
    out: dict[tuple[str, str], list[tuple[datetime, float, int]]] = {}
    delta = timedelta(seconds=bucket_size_s)
    for (idx, target, kind), items in grouped.items():
        latencies = sorted(p.latency_ms for p in items)
        median = _pct(latencies, 50)
        ts = window_start + delta * idx
        out.setdefault((target, kind), []).append((ts, median, len(latencies)))
    for series in out.values():
        series.sort(key=lambda t: t[0])
    return out


def _xmr_signals(values: list[float], center: float, unpl: float, lnpl: float) -> list[list[str]]:
    """Compute per-point Wheeler signal labels. Pure function over the series.

    Returns a list of the same length as ``values``; each element is the list
    of rule names firing at that point (typically empty).
    """
    n = len(values)
    out: list[list[str]] = [[] for _ in range(n)]
    if n == 0:
        return out
    # Rule 1: point outside the natural process limits.
    for i, v in enumerate(values):
        if v > unpl or v < lnpl:
            out[i].append("outside_limits")
    # Rule 2: 8 in a row on the same side of the centre line. Mark only the
    # *trigger* (the 8th point where the run becomes detectable), not every
    # continuation. Marking continuations produced a "carpet" of hundreds of
    # red dots when the process had drifted, drowning out acute outliers and
    # making the chart look broken. Standard XmR practice flags the trigger.
    side = [0] * n
    for i, v in enumerate(values):
        if v > center:
            side[i] = 1
        elif v < center:
            side[i] = -1
        # equal to centre breaks the run
    run = 0
    last_side = 0
    for i, s in enumerate(side):
        if s != 0 and s == last_side:
            run += 1
        elif s != 0:
            run = 1
            last_side = s
        else:
            run = 0
            last_side = 0
        if run == 8:
            out[i].append("run_of_8")
    # Rule 3: three of four beyond the 2/3 line (two-sigma) on the same side.
    # 2/3 line = centre + (2/3) * (UNPL - centre)  — works symmetrically.
    upper_2s = center + (2.0 / 3.0) * (unpl - center)
    lower_2s = center - (2.0 / 3.0) * (center - lnpl)
    for i in range(3, n):
        window_ = values[i - 3 : i + 1]
        above = sum(1 for v in window_ if v > upper_2s)
        below = sum(1 for v in window_ if v < lower_2s)
        if above >= 3 or below >= 3:
            out[i].append("two_sigma_3of4")
    # Rule 4: six points steadily increasing or steadily decreasing.
    for i in range(5, n):
        win = values[i - 5 : i + 1]
        if all(win[j] < win[j + 1] for j in range(5)):
            out[i].append("trend_up_6")
        elif all(win[j] > win[j + 1] for j in range(5)):
            out[i].append("trend_down_6")
    return out


def xmr_charts(
    pings: Iterable[PingResult],
    *,
    window_start: datetime,
    bucket_size_s: int,
    kind: str = "icmp",
    min_points: int = 10,
) -> list[XmRChart]:
    """Build one XmR chart per (target, kind) above ``min_points``.

    ``kind`` filters which probe kind enters the chart; XmR is per-process,
    and mixing ICMP and TCP RTT into one chart would conflate two processes.
    Default is ICMP because the user asked for ICMP control charts.

    Limits are computed from the values *visible in the window*. This is a
    pragmatic compromise: Wheeler prefers limits anchored to a stable baseline
    period. Future enhancement: accept an explicit baseline window.
    """
    grouped = _xmr_bucket_pings(
        (p for p in pings if p.kind == kind),
        window_start=window_start,
        bucket_size_s=bucket_size_s,
    )
    label_lookup = {p.target: p.label for p in pings if p.kind == kind}
    out: list[XmRChart] = []
    for (target, k), series in grouped.items():
        if len(series) < min_points:
            continue
        values = [v for _, v, _ in series]
        ts_list = [t for t, _, _ in series]
        samples_list = [n for _, _, n in series]
        moving_ranges = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
        mr_bar = statistics.fmean(moving_ranges) if moving_ranges else 0.0
        center = statistics.fmean(values)
        unpl = center + XMR_NPL_CONSTANT * mr_bar
        # RTT can't go negative; Wheeler clamps the lower limit at the
        # physical floor when one exists.
        lnpl = max(0.0, center - XMR_NPL_CONSTANT * mr_bar)
        mr_url = XMR_MR_CONSTANT * mr_bar
        per_point_signals = _xmr_signals(values, center, unpl, lnpl)
        points: list[XmRPoint] = []
        for i, ((ts, v, n), sigs) in enumerate(zip(series, per_point_signals)):
            mr_i = moving_ranges[i - 1] if i > 0 else None
            # MR chart signal: range beyond URL means an unusually large
            # jump from the previous point — append as its own rule.
            if mr_i is not None and mr_i > mr_url:
                sigs = [*sigs, "mr_outside_url"]
            points.append(
                XmRPoint(
                    ts=ts,
                    value=v,
                    samples=n,
                    mr=mr_i,
                    signals=sigs,
                )
            )
        out.append(
            XmRChart(
                target=target,
                label=label_lookup.get(target, target),
                kind=k,
                bucket_size_s=bucket_size_s,
                center=center,
                unpl=unpl,
                lnpl=lnpl,
                mr_bar=mr_bar,
                mr_url=mr_url,
                points=points,
            )
        )
    out.sort(key=lambda c: (c.kind, c.label))
    return out


__all__ = [
    "Bucket",
    "CalendarRow",
    "HourSummary",
    "LatencyBucket",
    "LatencyStats",
    "Outage",
    "PathChange",
    "QUALITY_REASON_JITTER",
    "QUALITY_REASON_LATENCY",
    "QUALITY_REASON_LOSS",
    "QualityEvent",
    "SEVERITY_FLICKER",
    "SEVERITY_MAJOR",
    "SEVERITY_MINOR",
    "SEVERITY_NOMINAL",
    "SEVERITY_NONE",
    "SEVERITY_NO_DATA",
    "SEVERITY_SEVERE",
    "XMR_MR_CONSTANT",
    "XMR_NPL_CONSTANT",
    "XmRChart",
    "XmRPoint",
    "bucket_outages",
    "bucket_size_for_window",
    "buffer_bloat_score",
    "call_quality_pct",
    "connectivity_uptime_pct",
    "detect_outages",
    "downsample_latency",
    "floor_to_hour",
    "latency_stats",
    "mtr_path_changes",
    "pivot_buckets_by_day",
    "quality_events",
    "summarize_by_hour_of_day",
    "uptime_pct",
    "window",
    "xmr_charts",
]
