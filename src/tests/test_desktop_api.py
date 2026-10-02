import sqlite3
import signal
from unittest.mock import Mock

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from python.backend import app as desktop_api


def test_health_and_status_endpoints():
    with TestClient(desktop_api.app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json()["running"] is False
    assert "model" in response.json()


def test_logs_endpoint_returns_last_requested_lines(tmp_path, monkeypatch):
    log_file = tmp_path / "bot.log"
    log_file.write_text("first\nsecond\nthird\n", encoding="utf-8")
    monkeypatch.setattr(desktop_api, "BOT_LOG_FILE", log_file)

    with TestClient(desktop_api.app) as client:
        response = client.get("/api/logs?lines=2")

    assert response.json() == {"logs": ["second", "third"]}


def test_logs_endpoint_rejects_out_of_range_line_count():
    with TestClient(desktop_api.app) as client:
        response = client.get("/api/logs?lines=0")

    assert response.status_code == 422


def test_dashboard_summary_returns_daily_metrics_from_readonly_database(tmp_path, monkeypatch):
    database_path = tmp_path / "cases.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE tick_log (created_at TEXT, should_reply INTEGER, judge_score REAL, skip_reason TEXT)"
    )
    connection.executemany(
        "INSERT INTO tick_log VALUES (?, ?, ?, ?)",
        [
            (desktop_api.date.today().isoformat(), 1, 85.0, None),
            (desktop_api.date.today().isoformat(), 0, 80.0, "duplicate"),
            ("2000-01-01", 1, 100.0, None),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(desktop_api, "CASE_DB_PATH", database_path)

    with TestClient(desktop_api.app) as client:
        response = client.get("/api/dashboard/summary")

    assert response.status_code == 200
    assert response.json()["ticks"] == 2
    assert response.json()["replies"] == 1
    assert response.json()["avg_score"] == 82.5
    assert response.json()["skipped"] == 1
    assert response.json()["skip_rate"] == 50


def test_dashboard_summary_returns_empty_metrics_when_database_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_api, "CASE_DB_PATH", tmp_path / "missing.db")

    with TestClient(desktop_api.app) as client:
        response = client.get("/api/dashboard/summary")

    assert response.json()["ticks"] == 0
    assert response.json()["skip_rate"] == 0


def test_bot_process_manager_starts_and_stops_managed_process(tmp_path, monkeypatch):
    manager = desktop_api.BotProcessManager()
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    monkeypatch.setattr(desktop_api, "BOT_PID_FILE", tmp_path / "bot.pid")
    monkeypatch.setattr(desktop_api, "BOT_LOG_FILE", tmp_path / "logs" / "bot.log")
    monkeypatch.setattr(desktop_api.subprocess, "Popen", Mock(return_value=process))

    # This test is about process lifecycle, not permissions; the machine running
    # the suite may well lack screen recording, and start() correctly refuses
    # without it. The preflight has its own test below.
    started = manager.start(skip_preflight=True)
    stopped = manager.stop()

    assert started == {"status": "started", "pid": 4321}
    assert stopped == {"status": "stopped"}
    process.send_signal.assert_called_once_with(signal.SIGTERM)


def test_start_refuses_without_permissions(tmp_path, monkeypatch):
    """A bot started without the grants would run and silently do nothing, so
    start() must refuse rather than hand back a cheerful 'started'."""
    manager = desktop_api.BotProcessManager()
    process = Mock()
    process.pid = 9999
    process.poll.return_value = None
    monkeypatch.setattr(desktop_api, "BOT_PID_FILE", tmp_path / "bot.pid")
    monkeypatch.setattr(desktop_api, "BOT_LOG_FILE", tmp_path / "logs" / "bot.log")
    monkeypatch.setattr(desktop_api.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        desktop_api,
        "_permission_preflight",
        lambda: {"ok": False, "screen_recording": {"status": "denied"}, "accessibility": {"status": "denied"}},
    )

    with pytest.raises(HTTPException) as exc:
        manager.start()
    assert exc.value.status_code == 409
    assert "权限" in str(exc.value.detail)
    desktop_api.subprocess.Popen.assert_not_called()