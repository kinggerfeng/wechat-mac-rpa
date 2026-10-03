"""The three confirmed ``cases.db`` defects, reproduced before they were fixed.

Each of these had been written and read for months without ever being executed
against a real database, and each failed the same way: a statement raised, an
``except`` logged a warning, and the process exited 0. So the tests here do not
mock anything — they build a database in the *old* shape, open it with the real
:class:`CaseDB`, and then run the real statements that were broken.

Defect 1: the ``tick_log`` rebuild declared 29 columns and supplied 35 values.
Defect 2: ``experiment_results`` had no ``system_prompt``/``user_prompt``, both
           of which ``run_experiment.py`` inserts and ``admin.py`` selects.
Defect 3: ``migrate_benchmarks_to_db.py`` wrote ``benchmark_*_cases`` while
           ``case_db.py`` creates and reads ``bench_*_cases``.
"""

from __future__ import annotations

from pathlib import Path

import sqlite3

import pytest


from rpa.badcase.case_db import CaseDB

#: tick_log as it looked before session_id existed and tick_id was UNIQUE.
OLD_TICK_LOG = """
CREATE TABLE tick_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id INTEGER NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    skip_reason TEXT,
    chat_name TEXT,
    is_group INTEGER DEFAULT 0,
    screenshot_path TEXT,
    messages_count INTEGER,
    new_messages_count INTEGER DEFAULT 0,
    system_prompt TEXT,
    user_prompt TEXT,
    raw_response TEXT,
    tool_calls_json TEXT,
    should_reply INTEGER DEFAULT 0,
    replies_sent_json TEXT,
    send_success INTEGER DEFAULT 0,
    send_duration_ms INTEGER,
    judge_score REAL,
    judge_is_badcase INTEGER,
    judge_dimensions_json TEXT,
    human_is_badcase INTEGER,
    human_badcase_type TEXT,
    human_notes TEXT,
    tokens_estimated INTEGER DEFAULT 0,
    duration_ms INTEGER,
    judge_badcase_type TEXT,
    judge_reason TEXT
)
"""


def make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_TICK_LOG)
    conn.execute(
        "INSERT INTO tick_log (tick_id, chat_name, system_prompt, user_prompt,"
        " judge_score, raw_response) VALUES (?,?,?,?,?,?)",
        (101, "旧会话", "系统提示", "用户提示", 88.0, "回复正文"),
    )
    conn.execute(
        "INSERT INTO tick_log (tick_id, chat_name, judge_score)"
        " VALUES (?,?,?)", (102, "第二个", 50.0),
    )
    conn.commit()
    conn.close()
    return path


def columns_of(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


def ddl_of(path: Path, table: str) -> str:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


class TestTickLogMigration:
    def test_it_actually_runs(self, tmp_path):
        """The whole point: the statement that used to raise now completes."""
        db = make_old_db(tmp_path / "cases.db")
        CaseDB(db)  # the migration runs inside __init__
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM tick_log").fetchone()[0] == 2
        conn.close()

    def test_data_survives(self, tmp_path):
        db = make_old_db(tmp_path / "cases.db")
        CaseDB(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tick_log WHERE tick_id=101 AND chat_name='旧会话'").fetchone()
        assert row["system_prompt"] == "系统提示"
        assert row["user_prompt"] == "用户提示"
        assert row["raw_response"] == "回复正文"
        assert row["judge_score"] == 88.0
        conn.close()

    def test_the_unique_constraint_on_tick_id_is_gone(self, tmp_path):
        """The point of the rebuild. Two ticks may share a tick_id now — a
        re-run within a minute produces one per session."""
        db = make_old_db(tmp_path / "cases.db")
        assert "UNIQUE" in ddl_of(db, "tick_log")
        CaseDB(db)
        ddl = ddl_of(db, "tick_log")
        assert "session_id" in ddl
        assert "UNIQUE" not in ddl
        # The insert the old schema could not accept.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO tick_log (session_id, tick_id, chat_name) VALUES (?,?,?)",
            ("s9", 101, "同 tick 的第二次"),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM tick_log").fetchone()[0] == 3
        conn.close()

    def test_every_current_column_exists_afterwards(self, tmp_path):
        """A rebuild that quietly dropped a column would lose data and still
        report success."""
        db = make_old_db(tmp_path / "cases.db")
        CaseDB(db)
        after = set(columns_of(db, "tick_log"))
        for column in ("session_id", "tool_results_json", "llm_messages_json",
                       "iterate_count", "feedback_issues", "judge_raw_response",
                       "session_input_messages_json", "think_tool_called"):
            assert column in after, column

    def test_new_columns_get_their_declared_defaults(self, tmp_path):
        db = make_old_db(tmp_path / "cases.db")
        CaseDB(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tick_log WHERE chat_name='旧会话'").fetchone()
        assert row["session_id"] == ""
        assert row["tool_results_json"] == "[]"
        assert row["feedback_issues"] == "[]"
        assert row["iterate_count"] == 0
        conn.close()

    def test_a_stale_scratch_table_does_not_wedge_it(self, tmp_path):
        """The old failure left ``tick_log_new`` behind, and on every later run
        ``CREATE TABLE IF NOT EXISTS`` skipped it — so the migration was
        permanently stuck. Starting from that exact state must still work."""
        db = make_old_db(tmp_path / "cases.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE tick_log_new (id INTEGER PRIMARY KEY, wrong TEXT)")
        conn.commit()
        conn.close()

        CaseDB(db)
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM tick_log").fetchone()[0] == 2
        conn.close()

    def test_running_twice_is_a_no_op(self, tmp_path):
        db = make_old_db(tmp_path / "cases.db")
        CaseDB(db)
        first = columns_of(db, "tick_log")
        CaseDB(db)
        assert columns_of(db, "tick_log") == first
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM tick_log").fetchone()[0] == 2
        conn.close()

    def test_a_column_missing_from_the_addition_list_is_an_error_not_data_loss(self, tmp_path,
                                                                              monkeypatch):
        """A column the *intended* schema declares, that the old table lacks and
        that nobody put in the mapping, must stop the rebuild loudly.

        Without the guard the rebuild would simply not include the column, and
        the rebuild would report success having dropped it.
        """
        import rpa.badcase.case_db as case_db

        db = make_old_db(tmp_path / "cases.db")
        monkeypatch.setattr(
            case_db, "_SCHEMA_TICK_LOG_DDL",
            case_db._SCHEMA_TICK_LOG_DDL.replace(
                "judge_reason TEXT,", "judge_reason TEXT,\n    a_brand_new_column TEXT,"),
        )
        instance = CaseDB.__new__(CaseDB)
        connection = sqlite3.connect(db)
        try:
            with pytest.raises(RuntimeError, match="a_brand_new_column"):
                instance._migrate_tick_log(connection)
        finally:
            connection.close()
        # The original table is untouched.
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM tick_log").fetchone()[0] == 2
        conn.close()

    def test_the_mapping_does_not_name_columns_the_schema_dropped(self):
        """The guard's other direction. A mapping entry for a column the schema
        no longer has would add it back on every rebuild — a column that cannot
        be filled by anything and should have been retired."""
        import rpa.badcase.case_db as case_db

        instance = CaseDB.__new__(CaseDB)
        intended = set(instance._columns_in_create(case_db._SCHEMA_TICK_LOG_DDL))
        mapped = {c for c, _, _ in CaseDB._TICK_LOG_ADDED}
        assert not mapped - intended, sorted(mapped - intended)


class TestExperimentResultsColumns:
    def test_the_insert_run_experiment_performs_now_works(self, tmp_path):
        """The exact statement from tools/bench/run_experiment.py."""
        db = tmp_path / "cases.db"
        CaseDB(db)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO experiments (name) VALUES ('e1')")
        exp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""INSERT OR REPLACE INTO experiment_results
            (experiment_id, tick_id, config_name, bot_reply,
             judge_is_badcase, judge_score, judge_dimensions_json, judge_reason,
             system_prompt, user_prompt)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (exp_id, 1, "control", "回复", 0, 80.0, "{}", "ok", "SP", "UP"))
        conn.commit()
        conn.close()

    def test_the_select_admin_performs_now_works(self, tmp_path):
        """The exact per-tick comparison query from apps/admin_console/admin.py."""
        db = tmp_path / "cases.db"
        CaseDB(db)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO experiments (name) VALUES ('e1')")
        exp_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""INSERT OR REPLACE INTO experiment_results
            (experiment_id, tick_id, config_name, bot_reply,
             judge_is_badcase, judge_score, judge_dimensions_json, judge_reason,
             system_prompt, user_prompt)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (exp_id, 1, "control", "回复", 0, 80.0, "{}", "ok", "SP", "UP"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT c.tick_id,
                   MAX(CASE WHEN c.config_name='control' THEN c.bot_reply END) as c_reply,
                   MAX(CASE WHEN c.config_name='control' THEN c.system_prompt END) as c_sp,
                   MAX(CASE WHEN c.config_name='control' THEN c.user_prompt END) as c_up
            FROM experiment_results c WHERE c.experiment_id=?
            GROUP BY c.tick_id ORDER BY c.tick_id
        """, (exp_id,)).fetchall()
        conn.close()
        assert rows[0]["c_sp"] == "SP"
        assert rows[0]["c_up"] == "UP"

    def test_an_existing_database_gains_the_columns(self, tmp_path):
        db = tmp_path / "cases.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE experiment_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER, tick_id INTEGER NOT NULL,
                config_name TEXT NOT NULL, bot_reply TEXT,
                judge_is_badcase INTEGER DEFAULT 0, judge_score REAL DEFAULT 0,
                judge_dimensions_json TEXT, judge_reason TEXT,
                UNIQUE(experiment_id, tick_id, config_name)
            );
            INSERT INTO experiment_results (experiment_id, tick_id, config_name, bot_reply)
            VALUES (1, 1, 'control', '旧数据');
        """)
        conn.commit()
        conn.close()

        CaseDB(db)
        assert "system_prompt" in columns_of(db, "experiment_results")
        conn = sqlite3.connect(db)
        assert conn.execute(
            "SELECT bot_reply FROM experiment_results").fetchone()[0] == "旧数据"
        conn.close()


class TestBenchmarkTableNames:
    def test_the_migration_targets_tables_that_exist(self, tmp_path):
        db = tmp_path / "cases.db"
        CaseDB(db)
        for table in ("bench_tool_cases", "bench_reply_cases", "bench_search_cases"):
            assert ddl_of(db, table), table

    def test_the_names_the_migration_writes_are_the_names_that_exist(self):
        """The defect in one assertion: the script's INSERT targets and the
        schema's CREATE targets have to be the same strings."""
        script = Path(__file__).resolve().parents[1] / "tools" / "bench" / "migrate_benchmarks_to_db.py"
        text = script.read_text(encoding="utf-8")
        for table in ("bench_tool_cases", "bench_reply_cases", "bench_search_cases"):
            assert f"INTO {table}" in text, table
            assert f"INTO benchmark_{table}" not in text, table

    def test_every_column_the_migration_inserts_exists(self, tmp_path):
        import re

        db = tmp_path / "cases.db"
        CaseDB(db)
        script = Path(__file__).resolve().parents[1] / "tools" / "bench" / "migrate_benchmarks_to_db.py"
        text = script.read_text(encoding="utf-8")
        for match in re.finditer(
            r"INSERT OR REPLACE INTO (bench_\w+)\s*\n\s*\(([^)]*)\)", text
        ):
            table, column_list = match.group(1), match.group(2)
            have = set(columns_of(db, table))
            for column in (c.strip() for c in column_list.split(",")):
                assert column in have, f"{table}.{column}"

    def test_the_columns_the_migration_writes_survive_an_existing_database(self, tmp_path):
        db = tmp_path / "cases.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE bench_tool_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_name TEXT UNIQUE NOT NULL,
                user_message TEXT NOT NULL, should_call_memory INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL, notes TEXT, enabled INTEGER DEFAULT 1
            );
        """)
        conn.commit()
        conn.close()
        CaseDB(db)
        assert "evaluation_mode" in columns_of(db, "bench_tool_cases")
