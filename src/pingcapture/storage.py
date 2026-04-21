"""SQLite append-only storage for ping/TCP results and mtr runs.

Schema versioning via ``PRAGMA user_version``. WAL mode for concurrent reads
while the pinger is writing. All writes go through a single connection per
process; readers (web console, report) open their own connections.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PingResult:
    ts: datetime
    target: str
    label: str
    kind: str  # "icmp" or "tcp"
    success: bool
    latency_ms: float | None
    error: str | None


@dataclass(frozen=True)
class MtrHop:
    hop_idx: int
    host: str | None
    ip: str | None
    loss_pct: float
    sent: int
    last_ms: float | None
    avg_ms: float | None
    best_ms: float | None
    worst_ms: float | None
    stddev_ms: float | None


@dataclass(frozen=True)
class MtrRun:
    ts: datetime
    target: str
    label: str
    hops: list[MtrHop]


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ping_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    target      TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('icmp','tcp')),
    success     INTEGER NOT NULL CHECK (success IN (0,1)),
    latency_ms  REAL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ping_ts        ON ping_results (ts);
CREATE INDEX IF NOT EXISTS idx_ping_target_ts ON ping_results (target, ts);

CREATE TABLE IF NOT EXISTS mtr_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT    NOT NULL,
    target  TEXT    NOT NULL,
    label   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mtr_ts ON mtr_runs (ts);

CREATE TABLE IF NOT EXISTS mtr_hops (
    run_id      INTEGER NOT NULL REFERENCES mtr_runs(id) ON DELETE CASCADE,
    hop_idx     INTEGER NOT NULL,
    host        TEXT,
    ip          TEXT,
    loss_pct    REAL    NOT NULL,
    sent        INTEGER NOT NULL,
    last_ms     REAL,
    avg_ms      REAL,
    best_ms     REAL,
    worst_ms    REAL,
    stddev_ms   REAL,
    PRIMARY KEY (run_id, hop_idx)
);
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _to_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds")


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    """Append-only SQLite store. Single writer; many readers."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            # Future: migrations from `version` -> SCHEMA_VERSION go here.
            raise RuntimeError(
                f"DB schema version {version} not supported by code version {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- writes ---

    def insert_ping(self, r: PingResult) -> None:
        self._conn.execute(
            "INSERT INTO ping_results (ts, target, label, kind, success, latency_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _to_iso(r.ts),
                r.target,
                r.label,
                r.kind,
                1 if r.success else 0,
                r.latency_ms,
                r.error,
            ),
        )

    def insert_mtr_run(self, run: MtrRun) -> int:
        cur = self._conn.execute(
            "INSERT INTO mtr_runs (ts, target, label) VALUES (?, ?, ?)",
            (_to_iso(run.ts), run.target, run.label),
        )
        run_id = int(cur.lastrowid or 0)
        self._conn.executemany(
            "INSERT INTO mtr_hops (run_id, hop_idx, host, ip, loss_pct, sent,"
            " last_ms, avg_ms, best_ms, worst_ms, stddev_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    h.hop_idx,
                    h.host,
                    h.ip,
                    h.loss_pct,
                    h.sent,
                    h.last_ms,
                    h.avg_ms,
                    h.best_ms,
                    h.worst_ms,
                    h.stddev_ms,
                )
                for h in run.hops
            ],
        )
        return run_id

    # --- reads ---

    def pings_between(
        self, start: datetime, end: datetime, kind: str | None = None
    ) -> list[PingResult]:
        sql = (
            "SELECT ts, target, label, kind, success, latency_ms, error"
            " FROM ping_results WHERE ts >= ? AND ts < ?"
        )
        params: list[object] = [_to_iso(start), _to_iso(end)]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY ts"
        return [
            PingResult(
                ts=_from_iso(row["ts"]),
                target=row["target"],
                label=row["label"],
                kind=row["kind"],
                success=bool(row["success"]),
                latency_ms=row["latency_ms"],
                error=row["error"],
            )
            for row in self._conn.execute(sql, params)
        ]

    def latest_pings(self, limit: int = 50) -> list[PingResult]:
        rows = self._conn.execute(
            "SELECT ts, target, label, kind, success, latency_ms, error"
            " FROM ping_results ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return [
            PingResult(
                ts=_from_iso(row["ts"]),
                target=row["target"],
                label=row["label"],
                kind=row["kind"],
                success=bool(row["success"]),
                latency_ms=row["latency_ms"],
                error=row["error"],
            )
            for row in rows
        ]

    def mtr_runs_between(self, start: datetime, end: datetime) -> list[MtrRun]:
        runs = self._conn.execute(
            "SELECT id, ts, target, label FROM mtr_runs"
            " WHERE ts >= ? AND ts < ? ORDER BY ts",
            (_to_iso(start), _to_iso(end)),
        ).fetchall()
        out: list[MtrRun] = []
        for run in runs:
            hops = self._conn.execute(
                "SELECT hop_idx, host, ip, loss_pct, sent, last_ms, avg_ms,"
                " best_ms, worst_ms, stddev_ms"
                " FROM mtr_hops WHERE run_id = ? ORDER BY hop_idx",
                (run["id"],),
            ).fetchall()
            out.append(
                MtrRun(
                    ts=_from_iso(run["ts"]),
                    target=run["target"],
                    label=run["label"],
                    hops=[
                        MtrHop(
                            hop_idx=h["hop_idx"],
                            host=h["host"],
                            ip=h["ip"],
                            loss_pct=h["loss_pct"],
                            sent=h["sent"],
                            last_ms=h["last_ms"],
                            avg_ms=h["avg_ms"],
                            best_ms=h["best_ms"],
                            worst_ms=h["worst_ms"],
                            stddev_ms=h["stddev_ms"],
                        )
                        for h in hops
                    ],
                )
            )
        return out

    def insert_pings_bulk(self, results: Iterable[PingResult]) -> None:
        self._conn.executemany(
            "INSERT INTO ping_results (ts, target, label, kind, success, latency_ms, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    _to_iso(r.ts),
                    r.target,
                    r.label,
                    r.kind,
                    1 if r.success else 0,
                    r.latency_ms,
                    r.error,
                )
                for r in results
            ],
        )


@contextmanager
def open_store(db_path: Path) -> Iterator[Store]:
    store = Store(db_path)
    try:
        yield store
    finally:
        store.close()


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "MtrHop",
    "MtrRun",
    "PingResult",
    "SCHEMA_VERSION",
    "Store",
    "open_store",
    "utc_now",
    "_utc_now_iso",
]
