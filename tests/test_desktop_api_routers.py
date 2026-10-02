"""The desktop API routers.

Why this file exists: ``rpa/backend/`` had **no** test at all. The whole
package is outside ``testpaths``' old default and nothing imported it, so a
change to any of its 22 cases endpoints could break the app while the rest of
the suite stayed green. It was verified once with a throwaway script under
``/tmp``, which does not survive the session.

The tests here build a real ``cases.db`` from the real schema, mount the real
routers, and drive them through ``TestClient``. The one thing they never touch
is the developer's own database: ``get_db`` is redirected at a temp file per
test, because a test that reads the operator's real tick log is a test that
either mutates it or reports on it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rpa.badcase.case_db import CaseDB


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture()
def cases_db(tmp_path: Path) -> Path:
    """A real ``cases.db`` built by the real schema, not a hand-written stub."""
    path = tmp_path / "cases.db"
    CaseDB(path)  # __init__ creates every table
    return path


@pytest.fixture()
def client(cases_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The cases router, pointed at a temp database.

    ``rpa.backend`` is a namespace package with no ``__init__.py``, so it is
    imported by path — the same way uvicorn's ``--app-dir python`` reaches it.
    """
    from rpa.backend import cases_api

    monkeypatch.setattr(cases_api, "get_db", lambda: CaseDB(cases_db))
    app = FastAPI()
    app.include_router(cases_api.router)
    return TestClient(app)


def _seed_ticks(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.executemany(
        """INSERT INTO tick_log
           (session_id, tick_id, chat_name, created_at, should_reply,
            judge_score, skip_reason, human_is_badcase, human_badcase_type,
            human_notes, human_labeled_at, judge_is_badcase, raw_response,
            new_messages_count, messages_count, duration_ms, replies_sent_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    conn.close()


TODAY = "2026-10-02 10:00:00"


# ── shape of the envelope ──────────────────────────────────────────────────

def test_summary_counts_today_only(client: TestClient, cases_db: Path):
    _seed_ticks(cases_db, [
        ("s1", 1, "张三", TODAY, 1, 85.0, None, None, None, None, None, 0,
         "raw-1", 2, 3, 120, '["收到"]'),
        ("s1", 2, "张三", TODAY, 0, 80.0, "duplicate", None, None, None, None, 0,
         "raw-2", 0, 3, 90, "[]"),
        ("s1", 3, "旧会话", "2000-01-01 00:00:00", 1, 100.0, None, None, None, None,
         None, 0, "raw-3", 1, 1, 50, "[]"),
    ])
    body = client.get("/api/cases/summary").json()
    assert body["ticks"] == 2              # the 2000 row is a different day
    assert body["replies"] == 1
    assert body["avg_score"] == 82.5
    assert body["skipped"] == 1
    assert body["skip_rate"] == 50
    assert body.get("degraded") is not True


# ── tick list ──────────────────────────────────────────────────────────────

def test_ticks_defaults_to_all_and_orders_newest_first(client: TestClient, cases_db: Path):
    _seed_ticks(cases_db, [
        ("s1", 1, "A", "2026-10-02 09:00:00", 0, None, None, None, None, None, None,
         0, "", 0, 0, 1, "[]"),
        ("s1", 2, "B", "2026-10-02 11:00:00", 0, None, None, None, None, None, None,
         0, "", 0, 0, 1, "[]"),
    ])
    body = client.get("/api/cases/ticks").json()
    assert body["filter"] == "all"
    assert [r["chat_name"] for r in body["rows"]] == ["B", "A"]
    assert body["total"] == 2


@pytest.mark.parametrize(
    "query,expected_total",
    # `replied` is "the bot decided to reply" (should_reply=1), not "a message
    # actually went out" — replies_sent_json is deliberately not consulted.
    [("filter=all", 3), ("filter=replied", 1), ("filter=skipped", 1)],
)
def test_each_filter_selects_a_different_set(client: TestClient, cases_db: Path, query, expected_total):
    _seed_ticks(cases_db, [
        ("s1", 1, "A", TODAY, 1, 80.0, None, None, None, None, None, 0, "", 0, 0, 1, "[]"),
        ("s1", 2, "B", TODAY, 0, 75.0, "duplicate", None, None, None, None, 0, "", 0, 0, 1, "[]"),
        ("s1", 3, "C", TODAY, 0, 70.0, None, None, None, None, None, 0, "", 0, 0, 1, "[]"),
    ])
    body = client.get(f"/api/cases/ticks?{query}").json()
    assert body["total"] == expected_total


def test_an_unknown_filter_is_rejected_rather_than_silently_all(client: TestClient):
    response = client.get("/api/cases/ticks?filter=bogus")
    assert response.status_code == 400
    assert "filter" in response.json()["detail"]


def test_paging_is_clamped_not_rejected(client: TestClient, cases_db: Path):
    _seed_ticks(cases_db, [
        ("s1", i, "A", TODAY, 0, None, None, None, None, None, None, 0, "", 0, 0, 1, "[]")
        for i in range(1, 4)
    ])
    body = client.get("/api/cases/ticks?page=0&size=9999").json()
    assert body["page"] == 1
    assert body["size"] <= 200


# ── tick detail and ground truth ───────────────────────────────────────────

def test_a_missing_tick_is_404_not_an_empty_row(client: TestClient):
    assert client.get("/api/cases/ticks/4242").status_code == 404


def test_ground_truth_save_then_read_back(client: TestClient, cases_db: Path):
    _seed_ticks(cases_db, [
        ("s1", 1, "A", TODAY, 0, 60.0, None, None, None, None, None, 0, "", 0, 0, 1, "[]"),
    ])
    saved = client.post("/api/cases/ticks/1/gt", json={
        "is_badcase": True, "badcase_type": "hallucination", "notes": "编造了不存在的链接",
    })
    assert saved.status_code == 200
    assert saved.json()["saved"] is True

    row = client.get("/api/cases/ticks/1").json()
    assert row["human_is_badcase"] == 1
    assert row["human_badcase_type"] == "hallucination"
    assert row["human_notes"] == "编造了不存在的链接"
    assert row["human_labeled_at"]


def test_ground_truth_on_a_missing_tick_does_not_report_saved(client: TestClient):
    # The legacy handler UPDATEd without checking and answered `saved: true`
    # for a row it never wrote. A wrong "saved" is worse than a 404.
    response = client.post("/api/cases/ticks/999/gt", json={"is_badcase": False})
    assert response.status_code == 404
    assert "saved" not in response.json()


def test_clearing_a_label_returns_the_tick_to_unlabelled(client: TestClient, cases_db: Path):
    _seed_ticks(cases_db, [
        ("s1", 1, "A", TODAY, 0, 60.0, None, 1, "hallucination", "note", TODAY, 0,
         "", 0, 0, 1, "[]"),
    ])
    response = client.request("DELETE", "/api/cases/ticks/1/gt")
    assert response.status_code == 200
    assert response.json()["cleared"] is True

    row = client.get("/api/cases/ticks/1").json()
    # "Nobody looked" and "the human decided it was fine" are different facts,
    # and the judge-vs-human page counts them differently.
    assert row["human_is_badcase"] is None
    assert row["human_badcase_type"] is None
    assert row["human_notes"] is None
    assert row["human_labeled_at"] is None


def test_clearing_twice_is_idempotent(client: TestClient, cases_db: Path):
    _seed_ticks(cases_db, [
        ("s1", 1, "A", TODAY, 0, 60.0, None, None, None, None, None, 0, "", 0, 0, 1, "[]"),
    ])
    assert client.request("DELETE", "/api/cases/ticks/1/gt").status_code == 200
    # The requested end state is already true; a 404 would make a retry look
    # like a failure.
    assert client.request("DELETE", "/api/cases/ticks/1/gt").status_code == 200


def test_clearing_a_missing_tick_is_404(client: TestClient):
    assert client.request("DELETE", "/api/cases/ticks/999/gt").status_code == 404


def test_gt_separates_a_disagreement_from_an_unlabelled_tick(client: TestClient, cases_db: Path):
    """Three ticks that the old list rendered identically.

    A tick the human already agreed with is not a candidate and is dropped by
    SQL; an unlabelled tick *is* a candidate but is not yet a disagreement. Only
    the middle row is a disagreement, and only that row may set ``disagree``.
    """
    _seed_ticks(cases_db, [
        # judge says bad, human already agreed — nothing left to do
        ("s1", 1, "A", TODAY, 0, 40.0, None, 1, "x", None, None, 1, "", 0, 0, 1, "[]"),
        # judge says fine, human says bad — the real disagreement
        ("s1", 2, "B", TODAY, 0, 90.0, None, 1, "y", None, None, 0, "", 0, 0, 1, "[]"),
        # nobody looked — a candidate, but not yet a disagreement
        ("s1", 3, "C", TODAY, 0, 90.0, None, None, None, None, None, 0, "", 0, 0, 1, "[]"),
        # never judged at all — outside the panel entirely
        ("s1", 4, "D", TODAY, 0, 0.0, None, 1, "z", None, None, 0, "", 0, 0, 1, "[]"),
    ])
    rows = {r["tick_id"]: r for r in client.get("/api/cases/gt").json()["rows"]}
    assert 1 not in rows, "an already-agreed tick must not be offered for relabelling"
    assert 4 not in rows, "a tick the judge never scored is not a disagreement"
    assert rows[2]["disagree"] is True
    assert rows[3]["disagree"] is False


# ── screenshot image serving ───────────────────────────────────────────────

def test_a_traversing_filename_is_refused(client: TestClient):
    for name in ("..%2F..%2Fetc%2Fpasswd", "../../etc/passwd", "a/b.png"):
        response = client.get(f"/api/cases/screenshot-image/{name}")
        assert response.status_code in (400, 404), name


def test_an_absent_image_is_404_json_not_a_broken_stream(client: TestClient):
    response = client.get("/api/cases/screenshot-image/nope.png")
    assert response.status_code == 404
    assert "error" in response.json() or "detail" in response.json()


# ── experiments ────────────────────────────────────────────────────────────

def test_an_unknown_experiment_is_404(client: TestClient):
    assert client.get("/api/cases/experiments/999").status_code == 404


def test_experiment_totals_are_null_not_nan_when_there_is_nothing_to_average(
    client: TestClient, cases_db: Path
):
    conn = sqlite3.connect(cases_db)
    # An experiment row with no results at all: every mean divides by zero.
    conn.execute(
        "INSERT INTO experiments (name, created_at, n_samples) VALUES ('空实验', ?, 0)",
        (TODAY,),
    )
    conn.commit()
    conn.close()
    body = client.get("/api/cases/experiments/1").json()
    assert body["totals"]["count"] == 0
    assert body["totals"]["c_avg"] is None
    assert body["totals"]["e_avg"] is None
    # JSON has no NaN literal; a NaN reaching the client is a rendering bug.
    assert "NaN" not in json.dumps(body)


# ── code audit ─────────────────────────────────────────────────────────────

def test_the_issue_catalogue_is_served_and_complete(client: TestClient):
    body = client.get("/api/cases/code-audit").json()
    assert body["issues"], "the static issue list must never be empty"
    for issue in body["issues"]:
        assert issue["key"] and issue["title"]


def test_every_issue_gets_a_state_even_without_a_row(client: TestClient):
    body = client.get("/api/cases/code-audit").json()
    for issue in body["issues"]:
        state = body["states"][issue["key"]]
        assert state["status"] in {
            "pending", "todo", "rethink", "fixed", "wontfix", "deferred",
            "ai_analyzing", "failed",
        }


def test_an_unknown_issue_key_is_404_everywhere(client: TestClient):
    key = "no-such-key"
    assert client.get(f"/api/cases/code-audit/{key}").status_code == 404
    assert client.post(f"/api/cases/code-audit/{key}", json={"status": "todo"}).status_code == 404
    assert client.post(f"/api/cases/code-audit/{key}/execute").status_code == 404
    assert client.post(f"/api/cases/code-audit/{key}/reject").status_code == 404


def test_status_saves_and_illegal_statuses_coerce(client: TestClient):
    key = client.get("/api/cases/code-audit").json()["issues"][0]["key"]
    assert client.post(f"/api/cases/code-audit/{key}",
                       json={"status": "fixed", "notes": "n"}).json()["status"] == "fixed"
    assert client.post(f"/api/cases/code-audit/{key}",
                       json={"status": "garbage"}).json()["status"] == "pending"
    assert client.post(f"/api/cases/code-audit/{key}/execute").status_code == 200
    assert client.get(f"/api/cases/code-audit/{key}").json()["status"] == "todo"
    assert client.post(f"/api/cases/code-audit/{key}/reject").status_code == 200
    assert client.get(f"/api/cases/code-audit/{key}").json()["status"] == "pending"


# ── wiki review ────────────────────────────────────────────────────────────

def test_wiki_review_is_empty_not_broken_when_the_file_is_absent(client: TestClient):
    body = client.get("/api/cases/wiki-review").json()
    assert body["items"] == []
    assert body["decisions"] == {}
    assert body["pending"] == 0


def test_a_corrupt_review_file_is_500_not_an_empty_worklist(client: TestClient, tmp_path, monkeypatch):
    """The failure this endpoint must never commit.

    ``review.json`` unreadable shown as "0 待确认" states, falsely, that there
    is no outstanding work. Absence is a real answer and comes back 200 with an
    empty list; corruption is not an answer at all.
    """
    from rpa.backend import cases_api

    audit = tmp_path / "wiki_audit"
    audit.mkdir()
    (audit / "review.json").write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(cases_api, "WIKI_AUDIT_DIR", audit)

    response = client.get("/api/cases/wiki-review")
    assert response.status_code == 500
    assert "review.json" in response.json()["detail"]


def test_an_unknown_wiki_item_is_404(client: TestClient):
    assert client.get("/api/cases/wiki-review/nope").status_code == 404
    assert client.post("/api/cases/wiki-review/nope", json={"action": "mark"}).status_code == 404


def test_an_illegal_wiki_action_is_rejected(client: TestClient, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    response = client.post("/api/cases/wiki-review/whatever", json={"action": "detonate"})
    assert response.status_code == 400


# ── benchmarks (disk-backed, so no degraded envelope) ──────────────────────

def test_a_missing_benchmark_report_is_omitted_not_shipped_empty(
    client: TestClient, tmp_path, monkeypatch
):
    """A missing report and an empty report render identically in an iframe.

    So an absent one is left out of the list entirely rather than shipped with
    empty ``html``, which would draw a blank frame the operator reads as
    "benchmark ran and found nothing".
    """
    from rpa.backend import cases_api

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "benchmark_judge.html").write_text("<h1>judge</h1>", encoding="utf-8")
    # `_BENCHMARK_REPORTS` bakes the paths in at import time, so redirecting
    # REPORTS_DIR alone would leave the real data/ directory in play.
    monkeypatch.setattr(cases_api, "_BENCHMARK_REPORTS", (
        ("judge", "Judge Quality Benchmark", reports / "benchmark_judge.html"),
        ("reply", "Bot 回复质量 Benchmark", reports / "benchmark_reply.html"),
    ))

    body = client.get("/api/cases/benchmarks").json()
    assert [r["key"] for r in body["reports"]] == ["judge"]
    assert body["reports"][0]["html"] == "<h1>judge</h1>"
    # Milliseconds, not a POSIX second count — the client does `new Date(ms)`.
    assert body["reports"][0]["modified"] > 1_000_000_000_000


# ── the degraded envelope ──────────────────────────────────────────────────
def _vanished_db(path: Path) -> CaseDB:
    """A ``CaseDB`` whose file has been removed underneath it.

    ``CaseDB.__init__`` creates the database, so pointing it at a fresh path
    produces a *healthy empty* store — which is exactly the healthy empty
    response, and so can never reach the degraded branch. Deleting the file
    afterwards is what ``_connect`` tests for. The instance is built once and
    reused, because rebuilding it inside the ``get_db`` replacement would
    recreate the file on every call.
    """
    db = CaseDB(path)
    Path(db.db_path).unlink()
    return db


#: Reads that go through ``_select`` and so can be degraded by an unreadable
#: store. Two cases endpoints are absent on purpose: ``/api/cases/benchmarks``
#: and ``/api/cases/wiki-review`` read report and review JSON off disk and
#: never open cases.db, so there is nothing for the database to degrade. Each
#: has its own handling, pinned by its own test.
_DB_BACKED_READS = (
    "/api/cases/summary", "/api/cases/ticks", "/api/cases/gt",
    "/api/cases/reviews", "/api/cases/screenshots",
    "/api/cases/code-audit", "/api/cases/experiments",
)


def test_a_missing_database_answers_200_with_degraded_not_500(tmp_path, monkeypatch):
    """An unreadable store and an empty one must not look the same.

    The code-audit list is the sharpest case: a payload of the full static issue
    list with every state reset to ``pending`` reads as a perfectly healthy
    worklist. ``degraded`` is the only thing telling the two apart, which is why
    the frontend types carry it on every cases response.
    """
    from rpa.backend import cases_api

    gone = _vanished_db(tmp_path / "cases.db")
    monkeypatch.setattr(cases_api, "get_db", lambda: gone)
    app = FastAPI()
    app.include_router(cases_api.router)
    broken = TestClient(app)

    for path in _DB_BACKED_READS:
        response = broken.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"
        body = response.json()
        assert body.get("degraded") is True, f"{path} did not flag degraded"
        assert body.get("degraded_reason"), f"{path} gave no reason"


def test_a_missing_database_and_an_empty_one_are_distinguishable(tmp_path, monkeypatch):
    """The pair the previous test leans on: identical payload, one flag apart.

    Without this, a reader could not tell whether the degraded assertions prove
    anything — an endpoint that set the flag unconditionally would pass them
    just as well.
    """
    from rpa.backend import cases_api

    live = CaseDB(tmp_path / "live" / "cases.db")
    monkeypatch.setattr(cases_api, "get_db", lambda: live)
    app = FastAPI()
    app.include_router(cases_api.router)
    healthy = TestClient(app)

    for path in _DB_BACKED_READS:
        body = healthy.get(path).json()
        assert body.get("degraded") is None, f"{path} flagged a healthy database"


def test_a_write_against_a_missing_database_is_503_not_a_silent_success(
    tmp_path, monkeypatch
):
    from rpa.backend import cases_api

    gone = _vanished_db(tmp_path / "cases.db")
    monkeypatch.setattr(cases_api, "get_db", lambda: gone)
    app = FastAPI()
    app.include_router(cases_api.router)
    broken = TestClient(app)

    response = broken.post("/api/cases/ticks/1/gt", json={"is_badcase": False})
    assert response.status_code == 503
    assert "saved" not in response.json()


# ── the router is mounted by the app the desktop actually runs ─────────────

def test_the_desktop_app_mounts_the_cases_router():
    """A router that exists but is not mounted is a 404 for every page.

    Worth one test: the mount is a side effect of a module-level import, which
    is exactly the kind of wiring a rename or a reorder silently drops.
    """
    from rpa.backend.app import app as desktop_app

    # `app.routes` holds one lazily-resolved `_IncludedRouter` per mount under
    # this FastAPI, and it has no `.path` — the OpenAPI schema is the only
    # enumeration that actually sees through the mounts.
    paths = set(desktop_app.openapi()["paths"])
    for path in ("/api/cases/summary", "/api/cases/ticks", "/api/cases/code-audit"):
        assert path in paths, f"{path} is not mounted on the desktop app"


def test_the_desktop_app_still_serves_dashboard_summary_from_case_db_path(tmp_path, monkeypatch):
    """Regression: delegating the summary to the cases router dropped the path.

    ``app.py`` owns ``CASE_DB_PATH`` and its own tests monkeypatch it. Routing
    the query through a function that opens the default store instead returns
    the developer's real numbers — or zeros — and the assertion is what caught it.
    """
    from rpa.backend import app as desktop_module

    database = tmp_path / "cases.db"
    conn = sqlite3.connect(database)
    conn.execute(
        "CREATE TABLE tick_log (created_at TEXT, should_reply INTEGER, judge_score REAL, skip_reason TEXT)"
    )
    conn.executemany("INSERT INTO tick_log VALUES (?,?,?,?)", [
        (TODAY, 1, 85.0, None),
        (TODAY, 0, 80.0, "duplicate"),
        ("2000-01-01", 1, 100.0, None),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setattr(desktop_module, "CASE_DB_PATH", database)

    body = TestClient(desktop_module.app).get("/api/dashboard/summary").json()
    assert body["ticks"] == 2
    assert body["avg_score"] == 82.5
