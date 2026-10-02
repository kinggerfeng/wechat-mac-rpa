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

import base64
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .schema import Flow
from .version import check as check_engine_compat
from .version import node_fingerprint, stamp

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "rpa.db"


def _registry_specs() -> dict[str, Any]:
    """The registered node types and the parameters each one accepts.

    Imported lazily: the registry builds itself on first use and pulls in the
    node modules, so importing it at module scope would make every reader of
    the store pay for the whole engine.
    """
    from .registry import get_node_registry

    return {
        spec.type: [p.name for p in spec.params]
        for spec in get_node_registry().list_specs()
    }

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
    -- Which engine accepted this flow. Columns rather than only a key inside
    -- graph_json, because the flow list answers "which of these are stale?"
    -- without loading every graph. Nullable: rows written before versioning
    -- have neither, which is UNSTAMPED, not broken.
    engine_version    TEXT,
    node_fingerprint  TEXT,
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

-- LLM 网关配置。存在 DB 而不是 .env，是因为换 provider 是运营动作，不该
-- 需要改文件重启进程——同一个产品要能对接客户自己的网关。api_key 走
-- obfuscate 而非明文：它挡不住有文件读权限的人，但能挡住 grep 日志、导出
-- 配置、以及截图粘进工单。
CREATE TABLE IF NOT EXISTS llm_providers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    base_url     TEXT NOT NULL,
    api_key      TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    temperature  REAL,
    max_tokens   INTEGER,
    timeout      REAL,
    is_default   INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT '',
    last_ok_at   TEXT,
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_llm_providers_default ON llm_providers(is_default);
"""

#: Columns added after the tables above were first shipped. ``rpa.db`` lives in
#: the repo and is not recreated between installs, so ``CREATE TABLE IF NOT
#: EXISTS`` silently leaves an older database without them.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # The minute a schedule last claimed, as ``YYYY-MM-DD HH:MM``. Distinct from
    # ``last_run_at``, which records when a run *finished*: a run started at
    # 10:00:50 ends at 10:01:30, and reusing last_run_at for the de-dupe stamp
    # would let that finishing time swallow the whole 10:01 tick.
    ("schedules", "last_fire_minute", "TEXT"),
    ("flows", "engine_version", "TEXT"),
    ("flows", "node_fingerprint", "TEXT"),
)


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
                self._ensure_wal(conn)
                conn.execute("PRAGMA synchronous=NORMAL")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _ensure_wal(conn: sqlite3.Connection) -> None:
        """Switch the journal to WAL, but only if it is not already.

        ``PRAGMA journal_mode=WAL`` takes a brief exclusive lock and returns
        SQLITE_BUSY immediately rather than honouring ``busy_timeout``, so
        running it on every connection meant two processes starting against the
        same fresh database killed each other with ``database is locked``. The
        read below is free, and a database already in WAL never needs the write.
        """
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            return
        for attempt in range(5):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                # The loser of a concurrent conversion sees BUSY but finds the
                # mode already set by the winner; anything else is real.
                mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if mode == "wal":
                    return
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # ``CREATE TABLE IF NOT EXISTS`` cannot add a column to a database
            # that already exists, so schema growth needs its own step. Kept
            # idempotent and inline rather than as a versioned migration chain:
            # this file is the only writer of rpa.db, and every addition so far
            # is a nullable column with a default.
            for table, column, decl in _ADDED_COLUMNS:
                cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column in cols:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                except sqlite3.OperationalError as exc:
                    # Another process opened the same database at the same
                    # moment, saw the column missing too, and added it first.
                    # Whichever loses the race has to accept the winner's column
                    # — the alternative is refusing to start because a
                    # concurrent uvicorn got there a millisecond earlier.
                    if "duplicate column" not in str(exc).lower():
                        raise

    # -- flows -------------------------------------------------------------

    def save_flow(
        self,
        flow_id: str,
        name: str,
        graph: dict[str, Any],
        description: str = "",
        is_active: bool | None = None,
    ) -> Flow:
        # Stamped here rather than in the editor: the store is the one place
        # every write passes through, so a flow saved from any surface carries
        # the engine that accepted it. A node's parameters or the registry
        # changing later then shows up as drift rather than as a silent
        # behaviour difference.
        stamp(graph, node_fingerprint(_registry_specs()))
        payload = json.dumps(graph, ensure_ascii=False)
        provenance = (graph["engine_version"], graph["node_fingerprint"])
        with self.connect() as conn:
            existing = conn.execute("SELECT is_active FROM flows WHERE id=?", (flow_id,)).fetchone()
            active = int(is_active) if is_active is not None else (existing["is_active"] if existing else 0)
            if existing:
                conn.execute(
                    "UPDATE flows SET name=?, description=?, graph_json=?, is_active=?,"
                    " engine_version=?, node_fingerprint=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (name, description, payload, active, *provenance, flow_id),
                )
            else:
                conn.execute(
                    "INSERT INTO flows (id, name, description, version, is_active, graph_json,"
                    " engine_version, node_fingerprint) VALUES (?,?,?,?,?,?,?,?)",
                    (flow_id, name, description, int(graph.get("version", 1)), active,
                     payload, *provenance),
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
                " f.engine_version, f.node_fingerprint,"
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

    def claim_schedule_minute(self, schedule_id: str, minute: str) -> bool:
        """Atomically claim ``minute`` for this schedule. True if we won.

        This is the cross-process de-dupe. An in-memory set is not enough: a
        second process on the same database has its own, and a restart inside the
        same minute starts with an empty one. Both cases fire the same schedule
        twice — which for a WeChat flow means the second run clicks whatever the
        first left on screen.

        The compare and the write are one statement so SQLite serialises them.
        Reading ``last_fire_minute`` first and writing it after would leave a
        window wide enough for two ticks to both decide they were first.
        """
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE schedules SET last_fire_minute=?"
                " WHERE id=? AND (last_fire_minute IS NULL OR last_fire_minute<>?)",
                (minute, schedule_id, minute),
            )
            return cursor.rowcount > 0

    # -- llm providers ----------------------------------------------------

    #: Obfuscation only. Reversible so a provider row can be used to build a
    #: client without a second copy of the key, and deliberately *not* framed
    #: as security: anything able to read rpa.db can read the key. It exists so
    #: the key does not show up in `sqlite3` dumps, log greps, config exports
    #: and screenshots pasted into tickets.
    _KEY_SALT = b"rpa-studio-llm-provider-v1"

    @classmethod
    def _obfuscate(cls, raw: str) -> str:
        if not raw:
            return ""
        return base64.urlsafe_b64encode(bytes(
            b ^ cls._KEY_SALT[i % len(cls._KEY_SALT)]
            for i, b in enumerate(raw.encode("utf-8"))
        )).decode("ascii")

    @classmethod
    def _deobfuscate(cls, stored: str) -> str:
        if not stored:
            return ""
        try:
            # urlsafe_b64encode drops the '=' padding; without restoring it the
            # decode raises or truncates, and the key comes back as garbage
            # that then fails at request time instead of here.
            padded = stored + "=" * (-len(stored) % 4)
            data = base64.urlsafe_b64decode(padded.encode("ascii"))
            # XOR is its own inverse, so decoding is the second half of the
            # same transform — base64 is only the transport, not the cipher.
            return bytes(
                b ^ cls._KEY_SALT[i % len(cls._KEY_SALT)]
                for i, b in enumerate(data)
            ).decode("utf-8")
        except Exception:  # noqa: BLE001
            return ""

    def save_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        """Insert or update a provider row.

        ``api_key`` is stored obfuscated and never returned in clear to the UI
        — listing returns a mask so the settings page can show "configured"
        without re-exposing it. The mask is what lets an edit save the form
        without the browser ever holding the real key.
        """
        pid = provider.get("id") or f"llmp_{uuid.uuid4().hex[:10]}"
        api_key = provider.get("api_key", "")
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM llm_providers WHERE id=?", (pid,)).fetchone()
            # An empty api_key on update means "keep the stored one" — the UI
            # submits a mask, and writing the mask through would break the row.
            if api_key:
                api_key = self._obfuscate(api_key)
            elif existing:
                api_key = existing["api_key"]

            payload = (
                provider.get("name", pid),
                provider.get("base_url", ""),
                api_key,
                provider.get("model", ""),
                provider.get("temperature"),
                provider.get("max_tokens"),
                provider.get("timeout"),
                int(provider.get("enabled", 1)),
                provider.get("note", ""),
            )
            is_default = int(provider.get("is_default", 0))
            if existing:
                conn.execute(
                    "UPDATE llm_providers SET name=?, base_url=?, api_key=?, model=?,"
                    " temperature=?, max_tokens=?, timeout=?, enabled=?, note=?, is_default=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (*payload, is_default, pid),
                )
            else:
                conn.execute(
                    "INSERT INTO llm_providers (name, base_url, api_key, model, temperature,"
                    " max_tokens, timeout, enabled, note, is_default, id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (*payload, is_default, pid),
                )
            # Demote the others *after* the row carries its own flag, or the
            # new default ends up cleared along with everyone else.
            if is_default:
                conn.execute("UPDATE llm_providers SET is_default=0 WHERE id<>?", (pid,))
        # Return the masked row, not the clear one: a caller that does
        # `store.save_provider({**saved, "name": ...})` would otherwise feed a
        # clear key back in and obfuscate it a second time.
        return next(
            (p for p in self.list_providers() if p["id"] == pid), {}
        )

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        """Provider with a **clear** api_key, for building a client."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM llm_providers WHERE id=?", (provider_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["api_key"] = self._deobfuscate(data.get("api_key", ""))
        return data

    def list_providers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Providers for the settings UI, with the key masked."""
        where = "WHERE enabled=1" if enabled_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM llm_providers {where} ORDER BY is_default DESC, name"
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            key = self._deobfuscate(data.get("api_key", ""))
            data["api_key_set"] = bool(key)
            data["api_key"] = _mask_key(key)
            out.append(data)
        return out

    def delete_provider(self, provider_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM llm_providers WHERE id=?", (provider_id,))
        return cursor.rowcount > 0

    def mark_provider_result(self, provider_id: str, ok: bool, error: str = "") -> None:
        with self.connect() as conn:
            if ok:
                conn.execute(
                    "UPDATE llm_providers SET last_ok_at=datetime('now','localtime'),"
                    " last_error='' WHERE id=?",
                    (provider_id,),
                )
            else:
                conn.execute(
                    "UPDATE llm_providers SET last_error=? WHERE id=?",
                    (str(error)[:500], provider_id),
                )

    def default_provider(self) -> dict[str, Any] | None:
        """The provider a flow gets when none is named.

        Falls back to the first enabled row so a single-provider install
        needs no default flag, and returns None (not an error) when the table
        is empty — an install with no LLM configured is legitimate.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM llm_providers WHERE enabled=1 ORDER BY is_default DESC, name LIMIT 1"
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["api_key"] = self._deobfuscate(data.get("api_key", ""))
        return data


def _element_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["rect"] = _load(data.pop("rect_json"))
    data["anchor"] = _load(data.pop("anchor_json"))
    data["meta"] = _load(data.pop("meta_json"))
    return data


def _mask_key(key: str) -> str:
    """What the settings UI shows in place of a stored key.

    A real mask rather than a boolean, so a form round-trip can send it back
    unchanged without the browser ever having held the key. ``save_provider``
    treats any non-empty value as "replace", so the UI must omit this field
    on edit rather than echo it.
    """
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


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
