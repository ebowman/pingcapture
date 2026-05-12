"""BDD step definitions for the user-facing scenarios."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from pingcapture.analytics import detect_outages, uptime_pct
from pingcapture.config import Config
from pingcapture.i18n import Translator
from pingcapture.report import ReportInputs, render_report
from pingcapture.storage import PingResult, Store

FEATURES = Path(__file__).resolve().parents[1] / "features"

scenarios(str(FEATURES / "outage_detection.feature"))
scenarios(str(FEATURES / "uptime.feature"))
scenarios(str(FEATURES / "report.feature"))


def _ping(ts: datetime, ok: bool, kind: str = "icmp") -> PingResult:
    return PingResult(
        ts=ts, target="1.1.1.1", label="Cloudflare", kind=kind,
        success=ok, latency_ms=10.0 if ok else None, error=None if ok else "fail",
    )


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "now": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "cursor": datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        "pings": [],
        "outages": [],
        "uptime": None,
        "report": None,
        "lang": "en",
    }


# --- Givens ---

@given(parsers.parse('a probe stream "{pattern}" at {step:d}-second intervals'))
def stream(ctx: dict[str, Any], pattern: str, step: int) -> None:
    # Alternate icmp/tcp so any failure streak >=2 contains a TCP failure
    # (required for detect_outages to consider it user-visible).
    for c in pattern:
        idx = len(ctx["pings"])
        kind = "icmp" if idx % 2 == 0 else "tcp"
        ctx["pings"].append(_ping(ctx["cursor"], c == "O", kind=kind))
        ctx["cursor"] += timedelta(seconds=step)


@given(parsers.parse('then a long quiet period of {seconds:d} seconds'))
def quiet(ctx: dict[str, Any], seconds: int) -> None:
    ctx["cursor"] += timedelta(seconds=seconds)


@given(parsers.parse('then a probe stream "{pattern}" at {step:d}-second intervals'))
def stream_then(ctx: dict[str, Any], pattern: str, step: int) -> None:
    stream(ctx, pattern, step)


@given("no probes")
def no_probes(ctx: dict[str, Any]) -> None:
    ctx["pings"] = []


@given("a seeded database with normal traffic and one outage")
def seeded_db(ctx: dict[str, Any], tmp_path: Path) -> None:
    db = tmp_path / "report.sqlite"
    store = Store(db)
    base = ctx["now"]
    for i in range(60):
        ok = not (10 <= i < 14)  # one 4-failure outage in the middle
        store.insert_ping(_ping(base + timedelta(seconds=i * 5), ok))
    store.close()
    ctx["db_path"] = db


# --- Whens ---

@when("I detect outages")
def when_detect(ctx: dict[str, Any]) -> None:
    ctx["outages"] = detect_outages(ctx["pings"])


@when("I compute uptime")
def when_uptime(ctx: dict[str, Any]) -> None:
    ctx["uptime"] = uptime_pct(ctx["pings"])


@when(parsers.parse("I render a report in {lang}"))
def when_render(ctx: dict[str, Any], lang: str) -> None:
    ctx["lang"] = lang
    cfg = Config.defaults()
    store = Store(ctx["db_path"])
    try:
        ctx["report"] = render_report(
            cfg=cfg, store=store,
            inputs=ReportInputs(
                start=ctx["now"] - timedelta(seconds=1),
                end=ctx["now"] + timedelta(seconds=60 * 5 + 1),
                lang=lang, owner="Eric",
            ),
        )
    finally:
        store.close()


# --- Thens ---

@then(parsers.parse("I see {n:d} outage"))
@then(parsers.parse("I see {n:d} outages"))
def then_count(ctx: dict[str, Any], n: int) -> None:
    assert len(ctx["outages"]) == n, f"got {len(ctx['outages'])}: {ctx['outages']}"


@then(parsers.parse("the outage duration is {seconds:d} seconds"))
def then_duration(ctx: dict[str, Any], seconds: int) -> None:
    assert len(ctx["outages"]) == 1
    assert ctx["outages"][0].duration_s == seconds


@then(parsers.parse("the uptime is {pct:f} percent"))
def then_uptime(ctx: dict[str, Any], pct: float) -> None:
    assert ctx["uptime"] == pytest.approx(pct, abs=0.01)


@then("the report contains the localized title")
def then_title(ctx: dict[str, Any]) -> None:
    expected = Translator(ctx["lang"]).t("report_title")
    assert expected in ctx["report"], f"missing title {expected!r} in report"
