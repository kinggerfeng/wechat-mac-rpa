import asyncio
import fcntl
import os
import signal
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_PID_FILE = PROJECT_ROOT / "bot.pid"
BOT_LOG_FILE = PROJECT_ROOT / "data" / "logs" / "desktop-bot.log"

try:
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
except OSError:
    pass


class BotProcessManager:
    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def _locked_bot_pid(self) -> int | None:
        try:
            with BOT_PID_FILE.open("a+") as pid_file:
                try:
                    fcntl.flock(pid_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pid_file.seek(0)
                    value = pid_file.read().strip()
                    return int(value) if value.isdigit() else None
                else:
                    fcntl.flock(pid_file, fcntl.LOCK_UN)
        except OSError:
            return None
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is not None:
                self._process = None
            pid = self._locked_bot_pid()
            return {
                "running": pid is not None,
                "pid": pid,
                "last_tick": None,
                "model": os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
                "wechat_status": "unknown",
            }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {"status": "already_running", "pid": self._process.pid}
            existing_pid = self._locked_bot_pid()
            if existing_pid is not None:
                return {"status": "already_running", "pid": existing_pid}

            BOT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            with BOT_LOG_FILE.open("ab") as log_file:
                try:
                    process = subprocess.Popen(
                        [sys.executable, str(PROJECT_ROOT / "run_bot.py")],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as exc:
                    raise HTTPException(status_code=500, detail=f"无法启动 Bot: {exc}") from exc
            self._process = process
            return {"status": "started", "pid": process.pid}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._process = None
                if self._locked_bot_pid() is not None:
                    raise HTTPException(status_code=409, detail="Bot 由其他进程启动，桌面端无法停止它")
                return {"status": "not_running"}

            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            self._process = None
            return {"status": "stopped"}

    def recent_logs(self, lines: int) -> list[str]:
        try:
            return BOT_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except FileNotFoundError:
            return []


bot_manager = BotProcessManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    try:
        await asyncio.to_thread(bot_manager.stop)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise


app = FastAPI(title="WeChat Mac RPA Desktop API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return bot_manager.status()


@app.post("/api/bot/start")
def start_bot() -> dict[str, Any]:
    return bot_manager.start()


@app.post("/api/bot/stop")
def stop_bot() -> dict[str, Any]:
    return bot_manager.stop()


@app.get("/api/logs")
def get_logs(lines: int = Query(200, ge=1, le=1000)) -> dict[str, list[str]]:
    return {"logs": bot_manager.recent_logs(lines)}