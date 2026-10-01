import signal
from unittest.mock import Mock

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


def test_bot_process_manager_starts_and_stops_managed_process(tmp_path, monkeypatch):
    manager = desktop_api.BotProcessManager()
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    monkeypatch.setattr(desktop_api, "BOT_PID_FILE", tmp_path / "bot.pid")
    monkeypatch.setattr(desktop_api, "BOT_LOG_FILE", tmp_path / "logs" / "bot.log")
    monkeypatch.setattr(desktop_api.subprocess, "Popen", Mock(return_value=process))

    started = manager.start()
    stopped = manager.stop()

    assert started == {"status": "started", "pid": 4321}
    assert stopped == {"status": "stopped"}
    process.send_signal.assert_called_once_with(signal.SIGTERM)