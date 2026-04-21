"""mtr JSON parser tests."""

from __future__ import annotations

import json

from pingcapture.config import PingTarget
from pingcapture.mtr import parse_mtr_json


def test_parse_typical_output() -> None:
    payload = json.dumps({
        "report": {
            "mtr": {"src": "host", "dst": "1.1.1.1"},
            "hubs": [
                {"count": 1, "host": "router (10.0.0.1)", "Loss%": 0.0, "Snt": 10,
                 "Last": 1.2, "Avg": 1.5, "Best": 1.0, "Wrst": 2.0, "StDev": 0.3},
                {"count": 2, "host": "??? (172.16.0.1)", "Loss%": 50.0, "Snt": 10,
                 "Last": 8.0, "Avg": 9.0, "Best": 7.0, "Wrst": 14.0, "StDev": 2.5},
                {"count": 3, "host": "1.1.1.1", "Loss%": 0.0, "Snt": 10,
                 "Last": 12.0, "Avg": 12.5, "Best": 11.0, "Wrst": 14.0, "StDev": 1.0},
            ],
        }
    })
    run = parse_mtr_json(payload, target=PingTarget("1.1.1.1", "CF"))
    assert len(run.hops) == 3
    assert run.hops[0].ip == "10.0.0.1"
    assert run.hops[0].host == "router"
    assert run.hops[1].loss_pct == 50.0
    assert run.hops[2].ip == "1.1.1.1"


def test_parse_handles_missing_hops() -> None:
    payload = json.dumps({"report": {"hubs": []}})
    run = parse_mtr_json(payload, target=PingTarget("x", "x"))
    assert run.hops == []
