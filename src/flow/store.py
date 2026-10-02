"""Persistence for flows, runs, execution spans, elements, recordings and schedules.

Lives in its own database (``data/rpa.db``) rather than ``data/cases.db``. The
cases schema has three known defects in its migration path (see
``src/badcase/case_db.py``), and new engine state must not inherit them or become
un-migratable.

Every public method opens a short-lived connection under a lock. A run can emit
spans from an executor thread while the API serves a read from the event loop, so
the store is written to be safe under that concurrency without a connection pool.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .schema import Flow

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "rpa.db"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS flows (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 1,
    is_active   INTEGER NOT NULL DEFAULT 0,
    graph_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_flows_name ON flows(name);

CREATE TABLE IF NOT EXISTS flow_runs (
    id            TEXT PRIMARY KEY,
    flow_id       TEXT NOT NULL,
    trigger_type  TEXT NOT NULL DEFAULT 'manual',
    trigger_ref   TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'running',
    steps         INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    failed_node   TEXT,
    variables_json TEXT NOT NULL DEFAULT '{}',
    scope_json    TEXT NOT NULL DEFAULT '{}',
    started_at    REAL NOT NULL,
    ended_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_runs_flow ON flow_runs(flow_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_runs_status ON flow_runs(status, started_at DESC);

CREATE TABLE IF NOT EXISTS spans (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    node_type      TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    attempt        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'running',
    started_at     REAL NOT NULL,
    ended_at       REAL,
    duration_ms    INTEGER,
    inputs_json    TEXT NOT NULL DEFAULT '{}',
    outputs_json   TEXT NOT NULL DEFAULT '{}',
    error          TEXT,
    screenshot_path TEXT
);
CREATE INDEX IF NOT EXISTS ix_spans_run ON spans(run_id, id);
CREATE INDEX IF NOT EXISTS ix_spans_error ON spans(status, started_at DESC);

CREATE TABLE IF NOT EXISTS elements (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    rect_json     TEXT NOT NULL DEFAULT '{}',
    anchor_json   TEXT NOT NULL DEFAULT '{}',
    ocr_text      TEXT DEFAULT '',
    image_path    TEXT,
    flow_id       TEXT,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_elements_flow ON elements(flow_id);

CREATE TABLE IF NOT EXISTS recordings (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    events_json TEXT NOT NULL DEFAULT '[]',
    flow_id    TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS schedules (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    flow_id      TEXT NOT NULL,
    cron         TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_status  TEXT,
    last_run_id  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_schedules_enabled ON schedules(enabled);
"""


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


class RpaStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    # -- flows -------------------------------------------------------------

    def save_flow(
        self,
        flow_id: str,
        name: str,
        graph: dict[str, Any],
        description: str = "",
        is_active: bool | None = None,
    ) -> Flow:
        with self.connect() as conn:
            existing = conn.execute("SELECT is_active FROM flows WHERE id=?", (flow_id,)).fetchone()
            active = int(is_active) if is_active is not None else (existing["is_active"] if existing else 0)
            if existing:
                conn.execute(
                    "UPDATE flows SET name=?, description=?, graph_json=?, is_active=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (name, description, json.dumps(graph, ensure_ascii=False), active, flow_id),
                )
            else:
                conn.execute(
                    "INSERT INTO flows (id, name, description, version, is_active, graph_json)"
                    " VALUES (?,?,?,?,?,?)",
                    (flow_id, name, description, int(graph.get("version", 1)), active, json.dumps(graph, ensure_ascii=False)),
                )
        saved = self.get_flow(flow_id)
        assert saved is not None
        return saved

    def get_flow(self, flow_id: str) -> Flow | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
        return Flow.from_row(row) if row else None

    def get_flow_by_name(self, name: str) -> Flow | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM flows WHERE name=?", (name,)).fetchone()
        return Flow.from_row(row) if row else None

    def list_flows(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT f.id, f.name, f.description, f.version, f.is_active, f.created_at, f.updated_at,"
                " (SELECT COUNT(*) FROM flow_runs r WHERE r.flow_id=f.id) AS run_count,"
                " (SELECT r.status FROM flow_runs r WHERE r.flow_id=f.id ORDER BY r.started_at DESC LIMIT 1) AS last_status,"
                " (SELECT r.started_at FROM flow_runs r WHERE r.flow_id=f.id ORDER BY r.started_at DESC LIMIT 1) AS last_run_at"
                " FROM flows f ORDER BY f.updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_flow(self, flow_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM flows WHERE id=?", (flow_id,))
            conn.execute("UPDATE schedules SET enabled=0 WHERE flow_id=?", (flow_id,))
        return cursor.rowcount > 0

    def set_active(self, flow_id: str, is_active: bool) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE flows SET is_active=?, updated_at=datetime('now','localtime') WHERE id=?",
                (int(is_active), flow_id),
            )
        return cursor.rowcount > 0

    # -- runs --------------------------------------------------------------

    def start_run(self, run_id: str, flow_id: str, trigger_type: str, trigger_ref: str, variables: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO flow_runs (id, flow_id, trigger_type, trigger_ref, status, variables_json, started_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, flow_id, trigger_type, trigger_ref, "running", json.dumps(variables, ensure_ascii=False), time.time()),
            )

    def finish_run(self, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE flow_runs SET status=?, steps=?, error=?, failed_node=?, scope_json=?, ended_at=? WHERE id=?",
                (
                    result.get("status", "error"),
                    int(result.get("steps", 0)),
                    result.get("error"),
                    result.get("failed_node"),
                    json.dumps(result.get("scope", {}), ensure_ascii=False, default=str),
                    result.get("ended_at") or time.time(),
                    result.get("run_id"),
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM flow_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        for key in ("variables_json", "scope_json"):
            data[key.removesuffix("_json")] = _load(data.pop(key))
        return data

    def list_runs(self, flow_id: str | None = None, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if flow_id:
            clauses.append("r.flow_id=?")
            params.append(flow_id)
        if status:
            clauses.append("r.status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.*, f.name AS flow_name FROM flow_runs r"
                " LEFT JOIN flows f ON f.id = r.flow_id"
                f" {where} ORDER BY r.started_at DESC LIMIT ?",
                params,
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            for key in ("variables_json", "scope_json"):
                data[key.removesuffix("_json")] = _load(data.pop(key))
            out.append(data)
        return out

    def mark_stale_runs_failed(self, reason: str = "进程重启，运行中断") -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE flow_runs SET status='error', error=?, ended_at=? WHERE status='running'",
                (reason, time.time()),
            )
        return cursor.rowcount

    # -- spans -------------------------------------------------------------

    def insert_span(self, span: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO spans (run_id, node_id, node_type, name, attempt, status, started_at, ended_at,"
                " duration_ms, inputs_json, outputs_json, error, screenshot_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    span.get("run_id", ""),
                    span.get("node_id", ""),
                    span.get("node_type", ""),
                    span.get("name", ""),
                    int(span.get("attempt", 1)),
                    span.get("status", "running"),
                    span.get("started_at", time.time()),
                    span.get("ended_at"),
                    span.get("duration_ms"),
                    json.dumps(span.get("inputs", {}), ensure_ascii=False, default=str),
                    json.dumps(span.get("outputs", {}), ensure_ascii=False, default=str),
                    span.get("error"),
                    span.get("screenshot_path"),
                ),
            )

    def list_spans(self, run_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM spans WHERE run_id=? ORDER BY id LIMIT ?", (run_id, max(1, min(limit, 10000)))
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            for key in ("inputs_json", "outputs_json"):
                data[key.removesuffix("_json")] = _load(data.pop(key))
            out.append(data)
        return out

    def prune_runs(self, keep: int = 500) -> int:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM flow_runs ORDER BY started_at DESC LIMIT -1 OFFSET ?", (keep,)).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM spans WHERE run_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM flow_runs WHERE id IN ({placeholders})", ids)
        return len(ids)

    # -- elements ----------------------------------------------------------

    def save_element(self, element: dict[str, Any]) -> dict[str, Any]:
        element_id = element.get("id") or f"el_{uuid.uuid4().hex[:10]}"
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM elements WHERE id=?", (element_id,)).fetchone()
            payload = (
                element.get("name", ""),
                element.get("kind", "rect"),
                json.dumps(element.get("rect", {}), ensure_ascii=False),
                json.dumps(element.get("anchor", {}), ensure_ascii=False),
                element.get("ocr_text", ""),
                element.get("image_path"),
                element.get("flow_id"),
                json.dumps(element.get("meta", {}), ensure_ascii=False),
            )
            if exists:
                conn.execute(
                    "UPDATE elements SET name=?, kind=?, rect_json=?, anchor_json=?, ocr_text=?,"
                    " image_path=?, flow_id=?, meta_json=?, updated_at=datetime('now','localtime') WHERE id=?",
                    (*payload, element_id),
                )
            else:
                conn.execute(
                    "INSERT INTO elements (id, name, kind, rect_json, anchor_json, ocr_text, image_path, flow_id, meta_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (element_id, *payload),
                )
        saved = self.get_element(element_id)
        assert saved is not None
        return saved

    def get_element(self, element_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
        return _element_row(row) if row else None

    def find_element(self, name: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM elements WHERE name=?", (name,)).fetchone()
        return _element_row(row) if row else None

    def list_elements(self, flow_id: str | None = None) -> list[dict[str, Any]]:
        if flow_id:
            with self.connect() as conn:
                rows = conn.execute("SELECT * FROM elements WHERE flow_id=? ORDER BY name", (flow_id,)).fetchall()
        else:
            with self.connect() as conn:
                rows = conn.execute("SELECT * FROM elements ORDER BY name").fetchall()
        return [_element_row(row) for row in rows]

    def delete_element(self, element_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM elements WHERE id=?", (element_id,))
        return cursor.rowcount > 0

    # -- recordings --------------------------------------------------------

    def save_recording(self, name: str, events: list[dict[str, Any]], flow_id: str | None, duration_ms: int) -> dict[str, Any]:
        recording_id = f"rec_{uuid.uuid4().hex[:10]}"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO recordings (id, name, events_json, flow_id, duration_ms) VALUES (?,?,?,?,?)",
                (recording_id, name, json.dumps(events, ensure_ascii=False), flow_id, duration_ms),
            )
        saved = self.get_recording(recording_id)
        assert saved is not None
        return saved

    def get_recording(self, recording_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["events"] = _load(data.pop("events_json"))
        return data

    def list_recordings(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.id, r.name, r.flow_id, r.duration_ms, r.created_at,"
                " (SELECT COUNT(*) FROM json_each(r.events_json)) AS event_count"
                " FROM recordings r ORDER BY r.created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_recording(self, recording_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
        return cursor.rowcount > 0

    # -- schedules ---------------------------------------------------------

    def save_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        schedule_id = schedule.get("id") or f"sch_{uuid.uuid4().hex[:10]}"
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            payload = (
                schedule.get("name", schedule_id),
                schedule.get("flow_id", ""),
                schedule.get("cron", ""),
                int(schedule.get("enabled", 1)),
            )
            if exists:
                conn.execute(
                    "UPDATE schedules SET name=?, flow_id=?, cron=?, enabled=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (*payload, schedule_id),
                )
            else:
                conn.execute(
                    "INSERT INTO schedules (id, name, flow_id, cron, enabled) VALUES (?,?,?,?,?)",
                    (schedule_id, *payload),
                )
        saved = self.get_schedule(schedule_id)
        assert saved is not None
        return saved

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return dict(row) if row else None

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled=1" if enabled_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, f.name AS flow_name FROM schedules s LEFT JOIN flows f ON f.id=s.flow_id"
                f" {where} ORDER BY s.name"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_schedule(self, schedule_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        return cursor.rowcount > 0

    def mark_schedule_run(self, schedule_id: str, run_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE schedules SET last_run_at=datetime('now','localtime'), last_status=?, last_run_id=? WHERE id=?",
                (status, run_id, schedule_id),
            )


def _element_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["rect"] = _load(data.pop("rect_json"))
    data["anchor"] = _load(data.pop("anchor_json"))
    data["meta"] = _load(data.pop("meta_json"))
    return data


def _load(raw: Any, default: Any = None) -> Any:
    if not raw:
        return {} if default is None else default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {} if default is None else default


_store: RpaStore | None = None
_store_lock = threading.Lock()


def get_store(db_path: str | Path | None = None) -> RpaStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = RpaStore(db_path)
        return _store
