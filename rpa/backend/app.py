import asyncio
import fcntl
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_PID_FILE = PROJECT_ROOT / "bot.pid"
BOT_LOG_FILE = PROJECT_ROOT / "data" / "logs" / "desktop-bot.log"
CASE_DB_PATH = PROJECT_ROOT / "data" / "cases.db"

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
                # These two used to be hard-coded to None/"unknown" while the UI
                # displayed them as if they were real readings. They are cheap to
                # compute and a dashboard that lies about liveness is worse than
                # one that admits it does not know.
                "last_tick": _last_tick(),
                "model": os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
                "wechat_status": _wechat_status(),
            }

    def start(self, skip_preflight: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return {"status": "already_running", "pid": self._process.pid}
            existing_pid = self._locked_bot_pid()
            if existing_pid is not None:
                return {"status": "already_running", "pid": existing_pid}

            if not skip_preflight:
                check = _permission_preflight()
                if not check.get("ok"):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "缺少 macOS 权限，拒绝启动（否则 bot 会静默失效）",
                            "preflight": check,
                        },
                    )

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


# ── real liveness readings, replacing the two hard-coded placeholders ──

_last_tick_cache: tuple[float, str | None] = (0.0, None)


def _last_tick() -> str | None:
    """Timestamp of the most recent row in ``tick_log``, cached briefly.

    Read on every status poll, and the status endpoint is polled every three
    seconds; a 2s cache keeps that off SQLite without making the reading stale
    enough to mislead. Returns ``None`` when the bot has never ticked — which is
    a real answer, unlike the old unconditional ``None``.
    """
    global _last_tick_cache
    cached_at, cached = _last_tick_cache
    now = time.time()
    if cached is not None and now - cached_at < 2.0:
        return cached
    if not CASE_DB_PATH.exists():
        _last_tick_cache = (now, None)
        return None
    try:
        conn = sqlite3.connect(f"{CASE_DB_PATH.as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT MAX(created_at) FROM tick_log").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    value = row[0] if row and row[0] else None
    _last_tick_cache = (now, value)
    return value


_wechat_cache: tuple[float, str] = (0.0, "")


def _wechat_status() -> str:
    """Whether a usable WeChat window is on screen.

    Uses the same window query the capture path uses, so the answer matches what
    perception will actually find rather than guessing from a process list.
    """
    global _wechat_cache
    now = time.time()
    if _wechat_cache[1] and now - _wechat_cache[0] < 5.0:
        return _wechat_cache[1]
    status = "unknown"
    try:
        import Quartz

        from rpa.models.base import Rect  # noqa: F401  (kept for parity with the capture path)

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        for window in windows or ():
            if window.get("kCGWindowOwnerName") in ("WeChat", "微信"):
                bounds = window.get("kCGWindowBounds", {})
                width = int(bounds.get("Width", 0))
                height = int(bounds.get("Height", 0))
                if width >= 800 and height >= 600:
                    status = "ready"
                else:
                    status = f"window_too_small:{width}x{height}"
                break
        else:
            status = "not_running"
    except ImportError:
        status = "check_unavailable"
    except Exception:  # noqa: BLE001
        status = "check_failed"
    _wechat_cache = (now, status)
    return status


def _permission_preflight() -> dict[str, Any]:
    """Screen recording + accessibility. Cheap and prompt-free, so it is safe to
    run on every start request — which is exactly where it belongs, because the
    alternative is a bot that starts cleanly and then silently does nothing."""
    try:
        from rpa.flow.permissions import preflight

        return preflight()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@asynccontextmanager
async def lifespan(_: FastAPI):
    from rpa.flow.seed import seed
    from rpa.flow.store import get_store

    # First boot writes the shipped flows so the canvas is never empty, and the
    # scheduler starts here rather than on first request so a schedule created
    # last session is already armed when the app comes back up.
    try:
        seed(get_store())
    except Exception:  # noqa: BLE001 - a read-only data dir must not block startup
        pass
    try:
        from rpa.flow.scheduler import get_scheduler

        get_scheduler().start()
    except Exception:  # noqa: BLE001
        pass

    yield

    try:
        from rpa.flow.scheduler import get_scheduler

        get_scheduler().stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        from rpa.flow.runner import get_run_manager

        get_run_manager().abort_all()
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.to_thread(bot_manager.stop)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise


# rpa_api imports rpa.flow at module level, which is a repository-root package.
# uvicorn is started with cwd=PROJECT_ROOT but does not put it on sys.path.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

app = FastAPI(title="WeChat Mac RPA Desktop API", lifespan=lifespan)
_DEV_ORIGINS = [
    # Tauri shell
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    # Vite dev / preview. The preview port moves when 4321 is taken, so the
    # range is matched by regex below rather than enumerated — an origin that
    # is not allowed fails as a network error with no hint about CORS, which
    # is a bad hour to spend debugging.
    "http://localhost:1420",
    "http://127.0.0.1:1420",
]

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_origins=_DEV_ORIGINS,
    # DELETE and PUT are not optional: flow, element and provider editing all
    # issue them, and omitting them fails as a preflight error rather than a
    # 405, which reads like the route is missing.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


from .rpa_api import router as rpa_router  # noqa: E402
from .cases_api import router as cases_router  # noqa: E402
from .record_api import router as record_router  # noqa: E402

app.include_router(rpa_router)
# cases.db belongs to the cases router and nothing else: the flow engine has its
# own rpa.db, and a query that reaches across the two from a flow node is the
# coupling this split exists to prevent.
app.include_router(cases_router)
app.include_router(record_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return bot_manager.status()


@app.get("/api/dashboard/summary")
def dashboard_summary() -> dict[str, int | float | str]:
    # The SQL lives in the cases router, which is the only thing that opens
    # cases.db. This endpoint predates that split and the overview page still
    # calls it, so it stays — as a delegation, not a second copy of the query.
    from .cases_api import today_summary

    return today_summary(CASE_DB_PATH)


@app.post("/api/bot/start")
def start_bot(skip_preflight: bool = False) -> dict[str, Any]:
    return bot_manager.start(skip_preflight=skip_preflight)


@app.post("/api/bot/stop")
def stop_bot() -> dict[str, Any]:
    return bot_manager.stop()


@app.get("/api/logs")
def get_logs(lines: int = Query(200, ge=1, le=1000)) -> dict[str, list[str]]:
    return {"logs": bot_manager.recent_logs(lines)}