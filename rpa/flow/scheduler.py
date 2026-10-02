"""Cron parsing and the scheduler loop.

Standard five-field cron (minute hour day month weekday), plus the ``@daily``-style
shorthands. Written by hand rather than pulled from a dependency: the project
already has a full runtime dependency surface, and this is the one place where a
subtle off-by-one would fire a bot at the wrong minute against a live WeChat
window. It is small enough to read.

Day-of-month and day-of-week follow cron's historical OR rule: ``0 9 1 * 0`` means
"the 1st, or any Sunday", not "the 1st if it is a Sunday".
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from .lease import Lease, read_lease
from .store import RpaStore, get_store

SHORTHANDS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 3 * * *",
    "@midnight": "0 3 * * *",
    "@hourly": "0 * * * *",
}

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_FIELD_NAMES = ("minute", "hour", "day", "month", "weekday")
_ALIASES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class CronError(ValueError):
    """Raised for a cron expression the scheduler will not guess about."""


@dataclass
class CronSchedule:
    """A parsed five-field expression.

    ``weekday`` accepts 7 as Sunday as well as 0, because both spellings are in
    wide use and rejecting one of them helps nobody.
    """

    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]
    raw: str = ""

    @classmethod
    def parse(cls, expression: str) -> "CronSchedule":
        text = (expression or "").strip().lower()
        text = SHORTHANDS.get(text, text)
        fields = text.split()
        if len(fields) != 5:
            raise CronError(f"需要 5 段（分 时 日 月 周），收到 {len(fields)} 段：{expression!r}")
        parsed = [_parse_field(part, index) for index, part in enumerate(fields)]
        minute, hour, day, month, weekday = parsed
        if weekday and 7 in weekday:
            weekday = {value % 7 for value in weekday}
        return cls(minutes=minute, hours=hour, days=day, months=month, weekdays=weekday, raw=expression)

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        dom_hit = moment.day in self.days
        dow_hit = ((moment.weekday() + 1) % 7) in self.weekdays
        if self.days == {31} and self.months == {2}:
            dom_hit = False  # cron never fires on a 31st of February
        # cron's OR rule when both day fields are restricted.
        if len(self.days) < 31 and len(self.weekdays) < 7:
            return dom_hit or dow_hit
        return dom_hit and dow_hit

    def next_after(self, moment: datetime, horizon_days: int = 400) -> datetime | None:
        """The next firing strictly after ``moment``, or ``None`` within the horizon."""
        candidate = (moment + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = moment + timedelta(days=horizon_days)
        while candidate <= limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None

    def describe(self) -> str:
        return f"分 {sorted(self.minutes)} 时 {sorted(self.hours)} 日 {sorted(self.days)} 月 {sorted(self.months)} 周 {sorted(self.weekdays)}"


def _parse_field(part: str, index: int) -> set[int]:
    low, high = _FIELD_RANGES[index]
    # Sunday is both 0 and 7 in the wild; accept 7 through parsing and fold it
    # to 0 afterwards, rather than making one of the two spellings an error.
    accept_high = 7 if index == 4 else high
    name = _FIELD_NAMES[index]
    values: set[int] = set()
    for chunk in part.split(","):
        chunk = chunk.strip()
        if not chunk:
            raise CronError(f"{name} 段有空项：{part!r}")
        step = 1
        if "/" in chunk:
            chunk, _, step_text = chunk.partition("/")
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronError(f"{name} 段的步长不是整数：{step_text!r}") from exc
            if step <= 0:
                raise CronError(f"{name} 段的步长必须为正：{step_text!r}")
        if chunk in ("*", "?"):
            start, end = low, accept_high
        elif "-" in chunk.lstrip("-"):
            start_text, _, end_text = chunk.partition("-")
            start, end = _atom(start_text, index), _atom(end_text, index)
        else:
            start = end = _atom(chunk, index)
        if start > end:
            raise CronError(f"{name} 段区间反了：{chunk!r}")
        values.update(value for value in range(start, end + 1, step) if low <= value <= accept_high)
    if not values:
        raise CronError(f"{name} 段没有解析出任何取值：{part!r}")
    return values


def _atom(text: str, index: int) -> int:
    token = text.strip().lower()
    if token in _ALIASES:
        return _ALIASES[token]
    if not re.fullmatch(r"[0-9]+", token):
        raise CronError(f"{_FIELD_NAMES[index]} 段无法解析：{text!r}")
    value = int(token)
    if index == 4 and value == 7:
        return 7  # normalised to 0 by the caller
    low, high = _FIELD_RANGES[index]
    if not low <= value <= high:
        raise CronError(f"{_FIELD_NAMES[index]} 取值 {value} 超出 {low}-{high}")
    return value


# ───────────────────────────────────────────────────────────── scheduler ──

@dataclass
class SchedulerStats:
    ticks: int = 0
    fired: int = 0
    skipped_busy: int = 0
    skipped_claimed: int = 0
    errors: int = 0
    last_error: str = ""
    last_tick_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "fired": self.fired,
            "skipped_busy": self.skipped_busy,
            "skipped_claimed": self.skipped_claimed,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_tick_at": self.last_tick_at,
        }


class Scheduler:
    """Fires flows on a cron, in-process.

    Three decisions worth stating. First, a tick that finds the bot already
    running **skips** rather than queues: a queued WeChat automation would fire
    two flows at one window, and the second would click on whatever the first
    left on screen.

    Second, and third, both about not firing twice. The class docstring used to
    claim the minute de-dupe came from a persisted ``last_run_at``; it came from
    an in-memory dict, which is invisible to every other process on the same
    database. Three dev servers sharing ``rpa.db`` fired one ``*/15`` schedule
    three times an interval. There are now two independent guards — a
    :class:`~rpa.flow.lease.Lease` so at most one process runs a loop at all, and
    a compare-and-set on the schedule row for a process that starts after the
    holder already claimed the minute. See ``src/flow/lease.py`` for why both are
    needed.
    """

    def __init__(
        self,
        store: RpaStore | None = None,
        tick_seconds: int = 20,
        lease: Lease | None = None,
        port: int | None = None,
    ) -> None:
        self.store = store or get_store()
        self.tick_seconds = max(5, tick_seconds)
        self.stats = SchedulerStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._standby_reason = ""
        self.lease = lease or Lease(
            Path(self.store.db_path).with_suffix(".scheduler.lock"), port=port
        )
        self.on_fire: Callable[[str, str], None] | None = None

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not self.lease.acquire():
                # Not an error: another process on this database is doing the
                # job. Recording it keeps the reason visible instead of
                # leaving an operator to wonder why their schedules went quiet.
                self.stats.last_error = (
                    f"调度器已在 PID {read_lease(self.lease.path).pid} 运行，本进程不再重复调度"
                )
                self._standby_reason = self.stats.last_error
                return False
            self._standby_reason = ""
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="flow-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.tick_seconds + 2)
        self._thread = None
        self.lease.release()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        """Everything the settings page needs to explain "is it armed, and by whom".

        ``standby`` is the case that used to be invisible: the process is up,
        the API answers, schedules are enabled — and nothing fires, because
        another process holds the lease.
        """
        holder = self.lease.info() if self.lease.held else read_lease(self.lease.path)
        return {
            "running": self.running,
            "standby": not self.lease.held,
            "standby_reason": self._standby_reason,
            "lease": holder.to_dict(),
            "stats": self.stats.to_dict(),
            "tick_seconds": self.tick_seconds,
        }

    def _loop(self) -> None:
        # Fire immediately on boot for anything whose minute window is now, so a
        # schedule the user just created is not stranded until the next tick.
        self.tick()
        while not self._stop.wait(self.tick_seconds):
            self.tick()

    def tick(self) -> list[dict[str, Any]]:
        """One pass. Returns the schedules that fired."""
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        self.stats.ticks += 1
        self.stats.last_tick_at = time.time()
        fired: list[dict[str, Any]] = []

        for row in self.store.list_schedules(enabled_only=True):
            schedule_id = row["id"]
            if row.get("last_fire_minute") == minute_key:
                # Already claimed — usually by our own previous tick this
                # minute, occasionally by a process that holds no lease but
                # shares the database. Either way it is counted, because an
                # operator asking "why did nothing fire" needs the skip to be
                # visible, not just the fires.
                self.stats.skipped_claimed += 1
                continue
            try:
                cron = CronSchedule.parse(row["cron"])
            except CronError as exc:
                self.stats.errors += 1
                self.stats.last_error = f"计划 {row['name']} 的 cron 非法: {exc}"
                continue
            if not cron.matches(now):
                continue

            # Re-checked in the database, not in memory: the read above is a
            # fast path, this is the one that actually decides.
            if not self.store.claim_schedule_minute(schedule_id, minute_key):
                self.stats.skipped_claimed += 1
                continue
            outcome = self._fire(row, cron)
            fired.append(outcome)

        return fired

    def _fire(self, row: dict[str, Any], cron: CronSchedule) -> dict[str, Any]:
        from .runner import get_run_manager

        schedule_id = row["id"]
        flow_id = row["flow_id"]
        name = row.get("name") or schedule_id
        flow = self.store.get_flow(flow_id)
        if flow is None:
            self.stats.errors += 1
            self.stats.last_error = f"计划 {name} 引用的流程 {flow_id} 不存在"
            self.store.mark_schedule_run(schedule_id, "", "error")
            return {"schedule_id": schedule_id, "status": "error", "reason": "flow_missing"}

        manager = get_run_manager()
        if manager.is_busy():
            self.stats.skipped_busy += 1
            self.store.mark_schedule_run(schedule_id, "", "skipped")
            return {
                "schedule_id": schedule_id,
                "status": "skipped",
                "reason": "微信窗口正被占用，本次不触发",
            }

        result = manager.submit(
            flow,
            trigger_type="schedule",
            trigger_ref=schedule_id,
            variables={},
        )
        status = "started" if result.get("accepted") else "refused"
        if not result.get("accepted"):
            self.stats.errors += 1
            self.stats.last_error = f"计划 {name} 启动被拒: {result.get('message')}"
        else:
            self.stats.fired += 1
            if self.on_fire is not None:
                try:
                    self.on_fire(schedule_id, result.get("run_id", ""))
                except Exception:  # noqa: BLE001
                    pass
        self.store.mark_schedule_run(schedule_id, result.get("run_id", ""), status)
        return {"schedule_id": schedule_id, "status": status, "run_id": result.get("run_id")}

    def describe(self, expression: str) -> dict[str, Any]:
        """Parse without scheduling — the settings page uses this to show a preview."""
        try:
            cron = CronSchedule.parse(expression)
        except CronError as exc:
            return {"valid": False, "error": str(exc)}
        now = datetime.now()
        return {
            "valid": True,
            "raw": expression,
            "fields": cron.describe(),
            "next_runs": [
                moment.isoformat(timespec="minutes")
                for moment in _next_n(cron, now, 3)
            ],
        }


def _next_n(cron: CronSchedule, now: datetime, count: int) -> Iterator[datetime]:
    cursor = now
    for _ in range(count):
        nxt = cron.next_after(cursor)
        if nxt is None:
            return
        yield nxt
        cursor = nxt


_scheduler: Scheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = Scheduler()
        return _scheduler
