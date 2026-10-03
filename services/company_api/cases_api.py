"""Cases-domain routes for the desktop API.

Split out of :mod:`services.company_api.app` for one reason: this is the only module
here that opens ``data/cases.db``, and that file is *optional*. A desktop
install that has never run the bot has no cases.db at all, so every route below
has to survive its absence. The legacy admin server never had to think about
this — it owned the data directory and created the file on import.

So a read here answers with the shape it would answer with when the data is
merely empty, plus ``degraded: true``, and a write answers 503. An empty panel
that says "no data yet" is honest; a panel rendering
``sqlite3.OperationalError: no such table`` is indistinguishable from a broken
install, which is this project's number one bug shape: a failure dressed up as
data.

Ported from ``apps/admin_console/admin.py``, which is being retired along with its
server-rendered HTML. The SQL is kept verbatim where it was correct; the
divergences are all commented at the point they happen.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import subprocess  # nosec B404
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

# rpa.badcase lives at the repository root, not next to this module.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from rpa.badcase.case_db import get_db

router = APIRouter(prefix="/api/cases", tags=["cases"])

_logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = _PROJECT_ROOT / "data" / "screenshots"
REPORTS_DIR = _PROJECT_ROOT / "data" / "reports"
WIKI_AUDIT_DIR = _PROJECT_ROOT / "data" / "memory" / "wiki_audit"
WIKI_USERS_DIR = _PROJECT_ROOT / "data" / "memory" / "wiki" / "users"
WIKI_GROUPS_DIR = _PROJECT_ROOT / "data" / "memory" / "wiki" / "groups"
BENCHMARK_SCRIPT = _PROJECT_ROOT / "tools" / "bench" / "generate_benchmark_dashboard.py"

#: Overridable so a packaged app can point at a bundled agent binary; same
#: resolution order the legacy admin server used.
KIMI_BIN = os.environ.get("KIMI_BIN", str(Path.home() / ".local" / "bin" / "kimi"))


# ───────────────────────────────────────────────────────────── database ──

class _DBUnavailable(RuntimeError):
    """cases.db cannot be opened at all — missing file, or an unwritable data/."""


def _connect() -> sqlite3.Connection:
    """Open cases.db, the only database this router ever touches.

    ``row_factory`` is already ``sqlite3.Row`` (set by ``CaseDB._get_conn``),
    and the caller closes the connection.
    """
    try:
        db = get_db()
        path = Path(db.db_path)
        if not path.exists():
            # ``get_db()`` normally creates the file on first use, so a path
            # that is still missing means the instance points somewhere else —
            # and connecting anyway raises "unable to open database file",
            # which never mentions cases.db.
            raise _DBUnavailable(f"cases.db 不存在：{path}")
        return db._get_conn()
    except _DBUnavailable:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _DBUnavailable(f"{type(exc).__name__}: {exc}") from exc


def _connect_for_write() -> sqlite3.Connection:
    """``_connect`` for routes that mutate: no database means no silent success."""
    try:
        return _connect()
    except _DBUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"cases 数据库不可用，写入已放弃：{exc}") from exc


def _select(sql: str, params: Sequence[Any] = ()) -> tuple[list[dict[str, Any]], str | None]:
    """Run a SELECT and return ``(rows, degraded_reason)``.

    A missing table is schema drift, not a bad query: the caller still gets its
    response shape, and the reason travels alongside so an operator can tell
    "empty" from "unreadable" without opening the log.
    """
    try:
        conn = _connect()
    except _DBUnavailable as exc:
        _logger.warning("cases.db 不可读：%s", exc)
        return [], str(exc)
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()], None
    except sqlite3.Error as exc:
        _logger.warning("cases.db 查询失败: %s", exc)
        return [], f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def _execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a write and return the affected row count."""
    conn = _connect_for_write()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor.rowcount
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入 cases.db 失败：{exc}") from exc
    finally:
        conn.close()


def _degraded(payload: dict[str, Any], reason: str | None) -> dict[str, Any]:
    if not reason:
        return payload
    return {**payload, "degraded": True, "degraded_reason": reason}


# ─────────────────────────────────────────────────────────── body models ──

class GroundTruthBody(BaseModel):
    is_badcase: bool
    badcase_type: str = ""
    notes: str = ""


class CodeAuditBody(BaseModel):
    # Kept as a plain str: the whitelist coerces rather than rejects, and the
    # legacy UI posts whatever a stale button held.
    status: str = "pending"
    notes: str = ""


class AnalyzeBody(BaseModel):
    notes: str = ""


class WikiDecisionBody(BaseModel):
    action: str
    new_value: str = ""


# ───────────────────────────────────────────────────────────── dashboard ──

@router.get("/summary")
def summary() -> dict[str, Any]:
    """Today's tick / reply / score / skip counters."""
    return today_summary()


_SUMMARY_SQL = """SELECT COUNT(*) AS total,
                         SUM(CASE WHEN should_reply=1 THEN 1 ELSE 0 END) AS replied,
                         COALESCE(ROUND(AVG(CASE WHEN judge_score>0 THEN judge_score END), 1), 0) AS avg_score,
                         SUM(CASE WHEN skip_reason IS NOT NULL THEN 1 ELSE 0 END) AS skipped
                  FROM tick_log WHERE date(created_at)=?"""


def today_summary(db_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The same counters, against an explicit database file.

    ``app.py`` owns ``CASE_DB_PATH`` and has served ``/api/dashboard/summary``
    from it since before this router existed. That endpoint is a documented
    contract with its own tests, and pointing it here means it must keep
    honouring that path — hence the parameter rather than a second copy of the
    SQL. Two copies of this query is exactly the kind of drift that produces a
    dashboard and a panel disagreeing about the same day.
    """
    today = date.today().isoformat()
    if db_path is None:
        rows, reason = _select(_SUMMARY_SQL, (today,))
    else:
        path = Path(db_path)
        if not path.exists():
            return _degraded(
                {"date": today, "ticks": 0, "replies": 0, "avg_score": 0,
                 "skipped": 0, "skip_rate": 0},
                f"cases.db 不存在：{path}",
            )
        try:
            # Read-only: the dashboard must not be able to create or migrate
            # the database it is only supposed to be reporting on.
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(_SUMMARY_SQL, (today,)).fetchone()
                rows, reason = ([dict(row)] if row else []), None
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return _degraded(
                {"date": today, "ticks": 0, "replies": 0, "avg_score": 0,
                 "skipped": 0, "skip_rate": 0},
                f"{type(exc).__name__}: {exc}",
            )

    row = rows[0] if rows else {}
    total = row.get("total") or 0
    skipped = row.get("skipped") or 0
    return _degraded(
        {
            "date": today,
            "ticks": total,
            "replies": row.get("replied") or 0,
            "avg_score": row.get("avg_score") or 0,
            "skipped": skipped,
            "skip_rate": round(skipped * 100 / max(total, 1)),
        },
        reason,
    )


# ────────────────────────────────────────────────────────────── tick log ──

#: Verbatim from admin.py's list query: the columns the tick table renders, and
#: nothing else, because a tick row carries whole prompts and tool-call blobs.
TICK_LIST_COLUMNS = (
    "id, session_id, tick_id, chat_name, messages_count, new_messages_count, "
    "should_reply, send_success, skip_reason, judge_score, human_is_badcase, "
    "human_badcase_type, replies_sent_json, raw_response, duration_ms, created_at"
)

#: filter -> (list predicate, matching COUNT query)
_TICK_FILTERS: dict[str, tuple[str, str]] = {
    "all": ("", "SELECT COUNT(*) AS n FROM tick_log"),
    "replied": (
        "WHERE should_reply=1",
        "SELECT COUNT(*) AS n FROM tick_log WHERE should_reply=1",
    ),
    "skipped": (
        "WHERE skip_reason IS NOT NULL",
        "SELECT COUNT(*) AS n FROM tick_log WHERE skip_reason IS NOT NULL",
    ),
}


@router.get("/ticks")
def list_ticks(
    filter: str = "all",
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    """One page of the tick log.

    page/size are clamped rather than rejected: a stale pager in the UI must not
    be able to turn a numeric typo into an error the operator has to read.
    """
    if filter not in _TICK_FILTERS:
        raise HTTPException(status_code=400, detail=f"非法 filter: {filter!r}，可选 all / replied / skipped")
    page = max(1, page)
    size = min(200, max(1, size))
    where, count_sql = _TICK_FILTERS[filter]
    rows, reason = _select(
        f"SELECT {TICK_LIST_COLUMNS} FROM tick_log {where} "
        "ORDER BY created_at DESC, tick_id DESC LIMIT ? OFFSET ?",
        (size, (page - 1) * size),
    )
    counts, count_reason = _select(count_sql)
    return _degraded(
        {
            "total": counts[0]["n"] if counts else 0,
            "page": page,
            "size": size,
            "filter": filter,
            "rows": rows,
        },
        reason or count_reason,
    )


@router.get("/ticks/{tick_pk}")
def tick_detail(tick_pk: int) -> dict[str, Any]:
    """The full ``SELECT *`` row behind the tick detail view."""
    rows, reason = _select("SELECT * FROM tick_log WHERE id=?", (tick_pk,))
    if not rows:
        if reason:
            # Degraded, not 404: "the database is unreadable" and "this tick
            # does not exist" are different answers and must not look alike.
            return _degraded({"id": tick_pk}, reason)
        raise HTTPException(status_code=404, detail=f"tick {tick_pk} 不存在")
    return _degraded(rows[0], reason)


@router.post("/ticks/{tick_pk}/gt")
def save_ground_truth(tick_pk: int, body: GroundTruthBody) -> dict[str, Any]:
    """Record the human verdict for one tick.

    The existence check is the point: the legacy UPDATE reported success on
    zero matched rows, so labelling a tick id that was never written answered
    "saved" and lost the label. A wrong ``saved: true`` is worse than a 404.
    """
    conn = _connect_for_write()
    try:
        exists = conn.execute("SELECT 1 FROM tick_log WHERE id=?", (tick_pk,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"tick {tick_pk} 不存在")
        conn.execute(
            """UPDATE tick_log SET
                human_is_badcase=?, human_badcase_type=?, human_notes=?,
                human_labeled_at=datetime('now','localtime')
                WHERE id=?""",
            (1 if body.is_badcase else 0, body.badcase_type, body.notes, tick_pk),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入 cases.db 失败：{exc}") from exc
    finally:
        conn.close()
    return {"saved": True}


@router.delete("/ticks/{tick_pk}/gt")
def clear_ground_truth(tick_pk: int) -> dict[str, Any]:
    """Withdraw a human verdict, returning the tick to "unlabelled".

    The columns are set to NULL rather than to a "normal" verdict because
    "the human decided this reply was fine" and "nobody looked" are different
    facts, and the judge-vs-human page counts them differently. A reviewer who
    mis-clicks needs a way back to unlabelled, and without this the only
    options are to leave a wrong label or to edit cases.db by hand.

    Idempotent: clearing an already-unlabelled tick is a 200, not a 404, because
    the requested end state is already true and failing would make a retry look
    like an error.
    """
    conn = _connect_for_write()
    try:
        exists = conn.execute("SELECT 1 FROM tick_log WHERE id=?", (tick_pk,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"tick {tick_pk} 不存在")
        conn.execute(
            """UPDATE tick_log SET
                human_is_badcase=NULL, human_badcase_type=NULL, human_notes=NULL,
                human_labeled_at=NULL
                WHERE id=?""",
            (tick_pk,),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入 cases.db 失败：{exc}") from exc
    finally:
        conn.close()
    return {"cleared": True, "id": tick_pk}


# ──────────────────────────────────────────────────── ground truth / GT ──

@router.get("/gt")
def ground_truth_rows(limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """Ticks the judge may have got wrong, newest first."""
    rows, reason = _select(
        """SELECT id, session_id, tick_id, chat_name, judge_score, judge_is_badcase,
                  human_is_badcase, human_badcase_type, raw_response
           FROM tick_log
           WHERE judge_score > 0
             AND (human_is_badcase IS NULL OR human_is_badcase != judge_is_badcase)
           ORDER BY created_at DESC, tick_id DESC LIMIT ?""",
        (limit,),
    )
    for row in rows:
        # Computed here rather than in SQL: an unlabelled tick is a *candidate*
        # for labelling, not a disagreement, and the legacy list rendered the
        # two identically.
        human = row["human_is_badcase"]
        row["disagree"] = human is not None and bool(human) != bool(row["judge_is_badcase"])
    return _degraded({"rows": rows}, reason)


# ────────────────────────────────────────────────────────────── reviews ──

@router.get("/reviews")
def review_rows(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Committed badcase drafts, newest first.

    The legacy list page linked each row to ``/review/{draft_id}``, a route that
    never existed — the link was a 404. Only the list is reproduced; a detail
    view belongs to a page that has an actual use case.
    """
    rows, reason = _select(
        "SELECT id, draft_id, chat_name, status, badcase_type, severity, confidence, "
        "overall_score, judge_reason FROM cases ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return _degraded({"rows": rows}, reason)


# ─────────────────────────────────────────────────────────── screenshots ──

def _screenshot_name(path_value: str | None) -> str:
    """The stored column holds a full path; the bytes are served by basename."""
    return Path(path_value).name if path_value else ""


def _image_exists(path_value: str | None) -> bool:
    name = _screenshot_name(path_value)
    return bool(name) and _safe_path(SCREENSHOTS_DIR, name) is not None


def _safe_path(base: Path, name: str) -> Path | None:
    """Only a bare filename directly inside ``base``.

    The regex *is* the defence: without it a name like ``../../.ssh/id_rsa``
    resolves to a real file and this becomes a file server for the whole home
    directory.
    """
    if not name or not re.fullmatch(r"[a-zA-Z0-9_.\-]+", name):
        return None
    # lgtm[py/path-injection] name is regex-restricted to a bare filename above
    target = base / name
    return target if target.is_file() else None


@router.get("/screenshots")
def list_screenshots(page: int = 1, size: int = 50) -> dict[str, Any]:
    """Ticks that recorded a screenshot path, newest first."""
    page = max(1, page)
    size = min(200, max(1, size))
    where = "WHERE screenshot_path IS NOT NULL AND screenshot_path != ''"
    rows, reason = _select(
        "SELECT tick_id, session_id, chat_name, screenshot_path, created_at "
        f"FROM tick_log {where} ORDER BY created_at DESC, tick_id DESC LIMIT ? OFFSET ?",
        (size, (page - 1) * size),
    )
    counts, count_reason = _select(f"SELECT COUNT(*) AS n FROM tick_log {where}")
    for row in rows:
        row["has_image"] = _image_exists(row["screenshot_path"])
    return _degraded(
        {
            "total": counts[0]["n"] if counts else 0,
            "page": page,
            "size": size,
            "rows": rows,
        },
        reason or count_reason,
    )


@router.get("/screenshots/{tick_id}")
def screenshot_detail(tick_id: int) -> dict[str, Any]:
    """One tick's screenshot plus the surrounding reply and judge verdict.

    ``tick_id`` recurs across sessions, so the newest row wins — the legacy page
    took whichever row SQLite happened to return first.
    """
    rows, reason = _select(
        "SELECT tick_id, session_id, chat_name, screenshot_path, created_at, "
        "replies_sent_json, raw_response, judge_score, skip_reason "
        "FROM tick_log WHERE tick_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (tick_id,),
    )
    if not rows:
        if reason:
            return _degraded({"tick_id": tick_id}, reason)
        raise HTTPException(status_code=404, detail=f"tick {tick_id} 没有截图记录")
    row = rows[0]
    return _degraded(
        {
            "tick_id": row["tick_id"],
            "session_id": row["session_id"] or "",
            "chat_name": row["chat_name"] or "",
            "screenshot_path": row["screenshot_path"],
            "created_at": row["created_at"],
            "has_image": _image_exists(row["screenshot_path"]),
            # What the bot actually said, falling back to the raw model output
            # when nothing was sent — a skip page that shows a blank reply
            # tells the reader nothing about why.
            "reply": (row["replies_sent_json"] or "") or row["raw_response"],
            "judge_score": row["judge_score"],
            "skip_reason": row["skip_reason"],
        },
        reason,
    )


@router.get("/screenshot-image/{filename}")
def serve_screenshot_image(filename: str) -> FileResponse:
    """Stream the PNG itself — this is an ``<img>`` src, not a JSON payload."""
    for base in (SCREENSHOTS_DIR, Path(tempfile.gettempdir())):
        target = _safe_path(base, filename)
        if target is not None:
            # lgtm[py/path-injection] target came from _safe_path
            return FileResponse(str(target), media_type="image/png")
    raise HTTPException(status_code=404, detail=f"截图 {filename!r} 不存在")


# ──────────────────────────────────────────────────────────── benchmarks ──

#: key, title, file. Titles ported from the legacy nav so the panel headings do
#: not change for an operator who has been reading them for months.
_BENCHMARK_REPORTS: tuple[tuple[str, str, Path], ...] = (
    ("judge", "Judge Quality Benchmark", REPORTS_DIR / "benchmark_judge.html"),
    ("reply", "Bot 回复质量 Benchmark", REPORTS_DIR / "benchmark_reply.html"),
)


@router.get("/benchmarks")
def benchmarks() -> dict[str, Any]:
    """Whole report documents, injected client-side."""
    reports: list[dict[str, Any]] = []
    for key, title, path in _BENCHMARK_REPORTS:
        try:
            raw = path.read_text(encoding="utf-8")
            # Milliseconds: the field is consumed by ``new Date(modified)``,
            # which would render 1970 for a POSIX second count.
            modified = int(path.stat().st_mtime * 1000)
        except OSError as exc:
            _logger.info("benchmark 报告 %s 缺失或不可读：%s", path.name, exc)
            # Omit rather than ship an empty iframe: a missing report and an
            # empty report look identical in the UI.
            continue
        reports.append({"key": key, "title": title, "html": raw, "modified": modified})
    return {
        "reports": reports,
        # The refresh button is only meaningful when the generator is shipped
        # alongside; a packaged build without tools/ must not offer it.
        "refreshable": BENCHMARK_SCRIPT.is_file(),
    }


@router.post("/benchmarks/refresh")
def refresh_benchmarks() -> dict[str, Any]:
    """Regenerate both reports. Never raises: the caller is a button."""
    try:
        proc = subprocess.run(  # nosec B603
            [sys.executable, str(BENCHMARK_SCRIPT)],
            timeout=60,
            capture_output=True,
            cwd=str(_PROJECT_ROOT),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("刷新 benchmark 失败: %s", exc)
        return {"success": False, "error": "刷新失败，请查看服务端日志"}
    if proc.returncode != 0:
        # The legacy version discarded the return code and always answered
        # success, so a failed regeneration looked like a finished one. The
        # stderr tail is what makes the difference debuggable.
        tail = proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        _logger.warning("benchmark 脚本退出码 %s: %s", proc.returncode, tail)
        return {"success": False, "error": f"刷新失败（退出码 {proc.returncode}），请查看服务端日志"}
    return {"success": True}


# ────────────────────────────────────────────────────────── experiments ──

@router.get("/experiments")
def list_experiments() -> dict[str, Any]:
    rows, reason = _select("SELECT * FROM experiments ORDER BY id DESC")
    return _degraded({"experiments": rows}, reason)


@router.get("/experiments/{exp_id}")
def experiment_detail(exp_id: int) -> dict[str, Any]:
    """A/B comparison: both arms side by side, with the prompt each one saw."""
    rows, reason = _select("SELECT * FROM experiments WHERE id=?", (exp_id,))
    if not rows:
        if reason:
            return _degraded({"experiment": None, "results": [], "totals": None}, reason)
        raise HTTPException(status_code=404, detail=f"实验 {exp_id} 不存在")
    experiment = rows[0]

    result_rows, result_reason = _select(
        """
        SELECT c.tick_id,
               MAX(CASE WHEN c.config_name='control' THEN c.judge_score END) as c_score,
               MAX(CASE WHEN c.config_name='control' THEN c.judge_is_badcase END) as c_bc,
               MAX(CASE WHEN c.config_name='control' THEN c.bot_reply END) as c_reply,
               MAX(CASE WHEN c.config_name='control' THEN c.judge_dimensions_json END) as c_dims,
               MAX(CASE WHEN c.config_name='control' THEN c.judge_reason END) as c_reason,
               MAX(CASE WHEN c.config_name!='control' THEN c.judge_score END) as e_score,
               MAX(CASE WHEN c.config_name!='control' THEN c.judge_is_badcase END) as e_bc,
               MAX(CASE WHEN c.config_name!='control' THEN c.bot_reply END) as e_reply,
               MAX(CASE WHEN c.config_name!='control' THEN c.judge_dimensions_json END) as e_dims,
               MAX(CASE WHEN c.config_name!='control' THEN c.judge_reason END) as e_reason
        FROM experiment_results c
        WHERE c.experiment_id=?
        GROUP BY c.tick_id ORDER BY c.tick_id
        """,
        (exp_id,),
    )
    tick_ids = [row["tick_id"] for row in result_rows]
    context_rows, context_reason = _select(
        "SELECT tick_id, chat_name, created_at, system_prompt, user_prompt, tool_calls_json "
        "FROM tick_log WHERE tick_id IN (" + ",".join("?" * len(tick_ids)) + ")",  # nosec B608
        tick_ids,
    ) if tick_ids else ([], None)
    context_by_tick = {row["tick_id"]: row for row in context_rows}

    results: list[dict[str, Any]] = []
    for row in result_rows:
        ctx = context_by_tick.get(row["tick_id"])
        if ctx is None:
            # A tick id with no tick_log entry has no chat, no timestamp and no
            # prompts to show — the legacy page rendered a row of dashes for it.
            # Dropping it is honest; filling the gaps with nulls is not.
            continue
        results.append({
            **row,
            "chat_name": ctx["chat_name"],
            "created_at": ctx["created_at"],
            "context": {
                "system_prompt": ctx["system_prompt"],
                "user_prompt": ctx["user_prompt"],
                "tool_calls_json": ctx["tool_calls_json"],
            },
        })

    def _mean(field: str) -> float | None:
        # ``None`` rather than NaN: JSON has no NaN, and FastAPI would emit the
        # bare token ``NaN``, which the client renders as the literal string.
        values = [row[field] for row in results if row[field] is not None]
        return sum(values) / len(values) if values else None

    totals = {
        "count": len(results),
        "c_avg": _mean("c_score"),
        "e_avg": _mean("e_score"),
        "c_badcase": sum(1 for row in results if row["c_bc"]),
        "e_badcase": sum(1 for row in results if row["e_bc"]),
    }
    return _degraded(
        {"experiment": experiment, "results": results, "totals": totals},
        reason or result_reason or context_reason,
    )


# ───────────────────────────────────────────────────────────── code audit ──

#: Statuses the save endpoint accepts. ``ai_analyzing`` and ``failed`` are
#: deliberately absent: only the analyze endpoint may set those, so a stale
#: client cannot pin an issue in a state no human left it in.
CODE_AUDIT_STATUSES: tuple[str, ...] = (
    "pending", "todo", "rethink", "fixed", "wontfix", "deferred",
)

#: The audit findings, carried over verbatim from admin.py. ``_audit_issue``
#: maps them onto the wire shape rather than rewriting the list, so the text an
#: auditor reads stays the text that was written.
CODE_AUDIT_ISSUES: list[dict[str, Any]] = [
    {
        "key": "api-timestamp-missing",
        "severity": "P0",
        "title": "API 路径时间戳系统性缺失",
        "file": "src/perception/smart_pipeline.py",
        "lines": "48-142, 893-904",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/perception/smart_pipeline.py#L893",
        "problem": "System Prompt 的 messages 格式只有 sender/text/type，未要求 API 返回时间戳；解析代码固定用 strptime('%Y-%m-%d %H:%M:%S')，但截图中的时间格式是'昨天 21:58'、'11:34'等，完全不匹配。",
        "impact": "所有 API 路径消息 create_time 100% fallback 到 int(time.time())。同 tick 内多条消息时间戳完全相同，导致历史窗口'最近10分钟'cutoff失效、already_handled去重误判、LLM时间推理维度失效。Tick 409已证实此症状。",
        "fix": "1) System Prompt 增加 timestamp 字段要求；2) 解析逻辑支持'昨天 HH:MM'、'HH:MM'、'YYYY-MM-DD HH:MM'多种格式；3) 使用相对时间转换（昨天=今天日期-1天+HH:MM）。"
    },
    {
        "key": "layout-timestamp-bug",
        "severity": "P0",
        "title": "layout_parser 聊天列表时间戳检测逻辑完全错误",
        "file": "src/layout/layout_parser.py",
        "lines": "354",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/layout/layout_parser.py#L354",
        "problem": "e.text in TIMESTAMP_PATTERNS 永远为False（列表元素是正则串）；e.text[1]==':' 对'11:34'判断第二个字符'1'；e.text[2:].isdigit() 对'11:34'得到':34'.isdigit()。三个条件全部永远为False。",
        "impact": "ChatListItem.timestamp 永远为空。当前无下游直接消费此字段，但这是一个彻底失效的功能——未来任何人基于时间戳做排序/判断都会失败。",
        "fix": "改为正则匹配：any(re.match(p, e.text) for p in TIMESTAMP_PATTERNS)"
    },
    {
        "key": "judge-weight-mismatch",
        "severity": "P0",
        "title": "judge_worker 维度权重表与 Prompt 模板不一致",
        "file": "src/badcase/judge_worker.py",
        "lines": "602-611, 215",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/badcase/judge_worker.py#L602",
        "problem": "Prompt中回复必要性20%/简洁度15%，代码中15%/10%；代码多出一个'工具调用正确性'10%维度，Prompt中完全没有。",
        "impact": "Judge LLM按Prompt打分，代码用另一套权重算总分。'工具调用正确性'缺失时fallback到50分，每个case被系统性扣5分，borderline case可能错误判为badcase。",
        "fix": "统一权重表：代码DIM_WEIGHTS与Prompt模板完全一致，或将'工具调用正确性'合并到'信息准确性'中。"
    },
    {
        "key": "weflow-mode-check",
        "severity": "P1",
        "title": "WeFlow 模式判断 hasattr 永远为 True",
        "file": "src/session/global_store.py",
        "lines": "390",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/session/global_store.py#L390",
        "problem": "hasattr(messages[0], 'local_id') 对任何 ChatMessage 永远为True（dataclass定义了该字段）。",
        "impact": "当前_weflow_mode='ocr'，不会触发此分支。但如果未来启用WeFlow持续模式，OCR消息会错误进入_merge_tick_weflow。",
        "fix": "messages[0].local_id is not None"
    },
    {
        "key": "timestamp-extract-inconsistent",
        "severity": "P1",
        "title": "_format_message_line 与 _msg_ts 时间戳提取逻辑不一致",
        "file": "src/reply/generator.py",
        "lines": "772-784, 916-921",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/reply/generator.py#L772",
        "problem": "_format_message_line对SELF消息不优先用reply_time；_msg_ts对SELF消息优先reply_time。两者fallback路径也不同（timestamp解析 vs time.time()）。",
        "impact": "Bot自己发的消息在prompt中不显示时间标签（因为create_time为空），但会被正确纳入历史窗口。显示与选择逻辑不一致，未来维护者容易困惑。",
        "fix": "统一两个函数的时间戳提取优先级：SELF→reply_time→create_time→timestamp解析→time.time()；OTHER→create_time→timestamp解析→time.time()"
    },
    {
        "key": "bot-self-msg-no-create-time",
        "severity": "P1",
        "title": "Bot 自身消息不设置 create_time",
        "file": "src/bot/wechat_bot.py",
        "lines": "416-420",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/bot/wechat_bot.py#L416",
        "problem": "发送成功后创建ChatMessage时只设置了reply_time，没有设置create_time。",
        "impact": "结合P1-2，Bot消息在prompt中永远不显示时间标签。LLM无法判断Bot消息的发送时间，只能依赖消息顺序推断。",
        "fix": "ChatMessage(..., create_time=int(time.time()), reply_time=time.time())"
    },
    {
        "key": "already-handled-mislabel",
        "severity": "P2",
        "title": "already_handled 可能错误标记连续消息",
        "file": "src/reply/generator.py",
        "lines": "962-975",
        "github_url": "https://github.com/wq19901103wq/wechat-mac-rpa/blob/main/src/reply/generator.py#L962",
        "problem": "如果用户连续发3条消息，Bot只回复了第3条，第1/2条也会因为'reply_time > ts'被标记为'⚠️(可跳过)'。",
        "impact": "这是提示性标记不强制跳过，但可能误导LLM跳过需要单独回复的消息。属于设计缺陷。",
        "fix": "更精确匹配：检查Bot回复的上一条消息是否与当前未读消息内容对应，而非仅比较时间。"
    },
]


def _raw_issue(key: str) -> dict[str, Any]:
    """The stored finding, exactly as written."""
    raw = next((item for item in CODE_AUDIT_ISSUES if item["key"] == key), None)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"审计项 {key!r} 不存在")
    return raw


def _audit_issue(key: str) -> dict[str, Any]:
    """The wire shape for one finding."""
    raw = _raw_issue(key)
    # The findings store a line *range* as free text ("48-142, 893-904"); the
    # wire shape wants a single anchor, so the first number is it.
    first_line = re.search(r"\d+", str(raw.get("lines") or ""))
    return {
        "key": raw["key"],
        "severity": raw.get("severity") or "",
        "title": raw.get("title") or "",
        "category": raw.get("category"),
        "file": raw.get("file"),
        "line": int(first_line.group()) if first_line else None,
        "description": raw.get("problem") or None,
        # The legacy detail page also rendered these three; the contract type
        # has no room for them, and dropping the fix suggestion would make the
        # panel useless for the one job it exists to do.
        "impact": raw.get("impact"),
        "fix": raw.get("fix"),
        "github_url": raw.get("github_url"),
        "lines": raw.get("lines"),
    }


def _ensure_code_audit_migration() -> None:
    """Migrate the pre-``status`` ``code_audit`` table (``checked`` INTEGER).

    The guard fires only on the old shape, so a migrated database pays one
    PRAGMA and no write.
    """
    try:
        conn = _connect()
    except _DBUnavailable as exc:
        _logger.warning("code_audit 迁移跳过：%s", exc)
        return
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(code_audit)").fetchall()}
        if "checked" in columns and "status" not in columns:
            conn.execute("ALTER TABLE code_audit ADD COLUMN status TEXT DEFAULT 'pending'")
            conn.execute("UPDATE code_audit SET status = CASE WHEN checked = 1 THEN 'fixed' ELSE 'pending' END")
            conn.commit()
            _logger.info("code_audit 表已从 checked 迁移到 status")
    except sqlite3.Error as exc:
        conn.rollback()
        _logger.warning("code_audit 迁移检查失败: %s", exc)
    finally:
        conn.close()


@router.get("/code-audit")
def code_audit_list() -> dict[str, Any]:
    """The findings, plus whatever state has been recorded for each."""
    _ensure_code_audit_migration()
    rows, reason = _select("SELECT issue_key, status, notes, ai_proposal FROM code_audit")
    recorded = {
        row["issue_key"]: {
            "status": row["status"] or "pending",
            "notes": row["notes"] or "",
            "ai_proposal": row["ai_proposal"] or "",
        }
        for row in rows
    }
    # Every known finding gets an entry, defaulted where nothing was recorded:
    # the legacy page applied that default in JavaScript, which meant a client
    # reading ``states[key].status`` had to guard the lookup itself. Rows for
    # keys that are no longer findings are dropped rather than surfaced.
    states = {
        item["key"]: recorded.get(item["key"], {"status": "pending", "notes": "", "ai_proposal": ""})
        for item in CODE_AUDIT_ISSUES
    }
    return _degraded(
        {
            "issues": [_audit_issue(item["key"]) for item in CODE_AUDIT_ISSUES],
            "states": states,
        },
        reason,
    )


@router.get("/code-audit/{key}")
def code_audit_detail(key: str) -> dict[str, Any]:
    """One finding, its state, and every analysis round recorded against it."""
    issue = _audit_issue(key)
    _ensure_code_audit_migration()
    rows, reason = _select(
        "SELECT issue_key, status, notes, ai_proposal FROM code_audit WHERE issue_key=?",
        (key,),
    )
    rounds, round_reason = _select(
        "SELECT round_num, user_notes, ai_proposal, created_at FROM code_audit_round "
        "WHERE issue_key=? ORDER BY round_num DESC",
        (key,),
    )
    row = rows[0] if rows else {}
    return _degraded(
        {
            "issue": issue,
            "status": row.get("status") or "pending",
            "notes": row.get("notes") or "",
            "ai_proposal": row.get("ai_proposal") or "",
            "rounds": [
                {
                    "round": item["round_num"],
                    "notes": item["user_notes"],
                    "proposal": item["ai_proposal"],
                    "created_at": item["created_at"],
                }
                for item in rounds
            ],
        },
        reason or round_reason,
    )


@router.post("/code-audit/{key}")
def save_code_audit(key: str, body: CodeAuditBody) -> dict[str, Any]:
    """Set the status and notes. An unknown status is coerced, not rejected —
    the legacy UI had no way to send a bad one and a 400 would just break it."""
    _audit_issue(key)
    status = body.status if body.status in CODE_AUDIT_STATUSES else "pending"
    _execute(
        """INSERT INTO code_audit (issue_key, status, notes, updated_at)
           VALUES (?, ?, ?, datetime('now','localtime'))
           ON CONFLICT(issue_key) DO UPDATE SET
           status=excluded.status, notes=excluded.notes, updated_at=excluded.updated_at""",
        (key, status, body.notes),
    )
    return {"saved": True, "status": status}


async def _analyze_with_kimi(issue: dict[str, Any], notes: str, timeout: int = 300) -> dict[str, Any]:
    """Ask the coding agent for a fix proposal. Never raises."""
    prompt = f"""你是一个代码审计专家。请分析以下代码问题并给出修复方案。

## 问题信息
- 标题: {issue['title']}
- 文件: {issue['file']}
- 行号: {issue.get('lines')}
- 级别: {issue['severity']}

## 问题描述
{issue['problem']}

## 影响
{issue['impact']}

## 用户反馈/要求
{notes if notes else '请分析问题根因并给出具体修复方案。'}

## 要求
1. 请先读取相关源码文件进行分析
2. 给出具体的修复方案（包含代码 diff）
3. 不要直接修改任何文件，只给出方案
4. 用中文回复
"""
    cmd = [KIMI_BIN, "--quiet", "--yolo", "-p", prompt, "-w", str(_PROJECT_ROOT)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(_PROJECT_ROOT),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip() or f"Kimi 退出码 {proc.returncode}"
            _logger.warning("Kimi 分析失败: %s", err)
            return {"success": False, "error": "Kimi 分析失败，请查看服务端日志"}
        return {"success": True, "reply": stdout.decode("utf-8", errors="replace")}
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("终止 Kimi 子进程失败: %s", exc)
        return {"success": False, "error": f"分析超时（>{timeout}秒）"}
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Kimi 分析异常: %s", exc)
        return {"success": False, "error": "分析异常，请查看服务端日志"}


@router.post("/code-audit/{key}/analyze")
async def analyze_code_audit(key: str, body: AnalyzeBody) -> dict[str, Any]:
    """Run one analysis round for a finding.

    The round row and the cleared notes are written *before* the model runs, on
    purpose: an analysis takes minutes, and if the operator switches tabs
    mid-flight they must still see the requirement they typed. Waiting until
    the end would show an empty panel for the whole wait.
    """
    issue = _raw_issue(key)
    notes = body.notes

    conn = _connect_for_write()
    try:
        conn.execute(
            """INSERT INTO code_audit (issue_key, severity, status, notes, updated_at)
               VALUES (?, ?, 'ai_analyzing', ?, datetime('now','localtime'))
               ON CONFLICT(issue_key) DO UPDATE SET
               status='ai_analyzing', notes=excluded.notes, updated_at=excluded.updated_at""",
            (key, issue["severity"], notes),
        )
        max_round = conn.execute(
            "SELECT MAX(round_num) FROM code_audit_round WHERE issue_key=?", (key,)
        ).fetchone()[0] or 0
        new_round = max_round + 1
        conn.execute(
            "INSERT INTO code_audit_round (issue_key, round_num, user_notes, ai_proposal) "
            "VALUES (?, ?, ?, ?)",
            (key, new_round, notes, ""),
        )
        # The requirement now lives in the round; clear it so the input box is
        # empty and ready for the next round.
        conn.execute("UPDATE code_audit SET notes='' WHERE issue_key=?", (key,))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail=f"写入 cases.db 失败：{exc}") from exc
    finally:
        conn.close()

    # 60000 is the legacy call site's value and it is very likely a unit slip
    # (the parameter is seconds), but changing a timeout is a decision about how
    # long an operator waits, not a bug fix — kept verbatim on purpose.
    result = await _analyze_with_kimi(issue, notes, timeout=60000)

    if result["success"]:
        _execute(
            "UPDATE code_audit_round SET ai_proposal=? WHERE issue_key=? AND round_num=?",
            (result.get("reply", ""), key, new_round),
        )
        _execute(
            """INSERT INTO code_audit (issue_key, severity, status, notes, ai_proposal, updated_at)
               VALUES (?, ?, 'rethink', ?, ?, datetime('now','localtime'))
               ON CONFLICT(issue_key) DO UPDATE SET
               status='rethink', notes=excluded.notes, ai_proposal=excluded.ai_proposal,
               updated_at=excluded.updated_at""",
            (key, issue["severity"], notes, result.get("reply", "")),
        )
    else:
        err_msg = "分析失败: " + result.get("error", "未知错误")
        _execute(
            "UPDATE code_audit_round SET ai_proposal=? WHERE issue_key=? AND round_num=?",
            (err_msg, key, new_round),
        )
        _execute(
            "UPDATE code_audit SET status='failed', ai_proposal=? WHERE issue_key=?",
            (err_msg, key),
        )
    return {
        "success": result["success"],
        "reply": result.get("reply", ""),
        "error": result.get("error", ""),
        "round": new_round,
    }


@router.post("/code-audit/{key}/execute")
def execute_code_audit(key: str) -> dict[str, Any]:
    """Claim the finding: pending -> todo."""
    _audit_issue(key)
    _execute(
        "UPDATE code_audit SET status='todo', updated_at=datetime('now','localtime') WHERE issue_key=?",
        (key,),
    )
    return {"success": True}


@router.post("/code-audit/{key}/reject")
def reject_code_audit(key: str) -> dict[str, Any]:
    """Send the finding back: any status -> pending."""
    _audit_issue(key)
    _execute(
        "UPDATE code_audit SET status='pending', updated_at=datetime('now','localtime') WHERE issue_key=?",
        (key,),
    )
    return {"success": True}


# ───────────────────────────────────────────────────────────── wiki review ──

WIKI_ACTIONS: tuple[str, ...] = ("delete", "fix", "mark", "skip")


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Loud, not silent: a corrupt review.json shown as "0 待确认" reads as
        # "there is nothing to review", which is a false statement about work.
        raise HTTPException(status_code=500, detail=f"{path.name} 读取失败：{exc}") from exc


def _load_review_items() -> list[dict[str, Any]]:
    return _load_json(WIKI_AUDIT_DIR / "review.json") or []


def _load_decisions() -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in _load_json(WIKI_AUDIT_DIR / "decisions.json") or []}


def _wiki_line_context(name: str, is_group: bool, needle: str, span: int = 2) -> str:
    """The offending line plus ``span`` lines either side, line-numbered."""
    base = WIKI_GROUPS_DIR if is_group else WIKI_USERS_DIR
    path = base / f"{name}.md"
    if not path.exists():
        return "(wiki 文件不存在)"
    lines = path.read_text(encoding="utf-8").split("\n")
    index = -1
    for position, line in enumerate(lines):
        if needle and needle[:30] in line:
            index = position
            break
    if index < 0:
        return "(行未找到，可能已被清洗)"
    low, high = max(0, index - span), min(len(lines), index + span + 1)
    return "\n".join(
        f"{'▶' if position == index else ' '} {position + 1:3d} | {html.escape(lines[position])}"
        for position in range(low, high)
    )


@router.get("/wiki-review")
def wiki_review_list() -> dict[str, Any]:
    """Audit items from ``review.json`` with whatever has been decided."""
    items = _load_review_items()
    decisions = _load_decisions()
    # Counted over the items, not as ``len(items) - len(decisions)``: a
    # decisions.json carrying an id that is no longer under review would
    # otherwise drive the pending count negative.
    pending = sum(1 for item in items if item.get("id") not in decisions)
    return {"items": items, "decisions": decisions, "pending": pending}


@router.get("/wiki-review/{item_id}")
def wiki_review_detail(item_id: str) -> dict[str, Any]:
    """One audit item, its decision, and the wiki line it refers to."""
    item = next((entry for entry in _load_review_items() if entry.get("id") == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"审核条目 {item_id!r} 不存在")
    needle = item.get("line") or item.get("wiki_excerpt") or item.get("fact") or ""
    return {
        "item": item,
        "decision": _load_decisions().get(item_id),
        "context": _wiki_line_context(item.get("wiki", ""), bool(item.get("is_group", False)), needle),
    }


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write through a sibling temp file, then ``os.replace``.

    ``os.replace`` is atomic within one filesystem, so a crash mid-write leaves
    the previous decisions.json intact. Truncating it in place would leave a
    half-written file — losing every decision ever made.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入 {path.name} 失败：{exc}") from exc


@router.post("/wiki-review/{item_id}")
def save_wiki_review(item_id: str, body: WikiDecisionBody) -> dict[str, Any]:
    """Record a decision. Re-deciding an item replaces its previous decision."""
    if body.action not in WIKI_ACTIONS:
        raise HTTPException(status_code=400, detail=f"非法 action: {body.action!r}")
    item = next((entry for entry in _load_review_items() if entry.get("id") == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"审核条目 {item_id!r} 不存在")
    decision = {
        "id": item_id,
        "wiki": item.get("wiki", ""),
        "is_group": item.get("is_group", False),
        "line": item.get("line") or item.get("wiki_excerpt") or item.get("fact", ""),
        "action": body.action,
        "new_value": body.new_value,
        "decided_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = WIKI_AUDIT_DIR / "decisions.json"
    existing = [entry for entry in (_load_json(path) or []) if entry.get("id") != item_id]
    existing.append(decision)
    _write_json_atomic(path, existing)
    return {"success": True}
