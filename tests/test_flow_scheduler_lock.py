"""One scheduler per database.

The incident behind this file: three uvicorn processes on one ``rpa.db``, each
with its own scheduler thread, each loading the same enabled ``*/15`` schedule.
One cron entry fired three times an interval. Each fire screenshotted, so the
user got three screen-recording permission prompts an interval and we spent a
while blaming TCC.

The tests below reproduce that shape directly — real processes, real
``flock``, one shared database — because the whole point of the fix is that it
holds *across* processes. A single-process test of a lock is a test of nothing.
"""

from __future__ import annotations
import sys

from pathlib import Path

import subprocess
import time
from datetime import datetime

import pytest


from apps.engine.lease import Lease, read_lease
from apps.engine.store import RpaStore


class TestLease:
    def test_second_holder_is_refused(self, tmp_path):
        path = tmp_path / "s.lock"
        first = Lease(path, port=8768)
        second = Lease(path, port=8769)
        try:
            assert first.acquire() is True
            assert second.acquire() is False
        finally:
            first.release()
            second.release()

    def test_release_hands_it_over(self, tmp_path):
        path = tmp_path / "s.lock"
        first, second = Lease(path), Lease(path)
        try:
            first.acquire()
            first.release()
            assert second.acquire() is True
        finally:
            first.release()
            second.release()

    def test_double_acquire_by_one_lease_is_idempotent(self, tmp_path):
        lease = Lease(tmp_path / "s.lock")
        try:
            assert lease.acquire() is True
            assert lease.acquire() is True
        finally:
            lease.release()

    def test_a_dead_holder_leaves_nothing_to_reap(self, tmp_path):
        """The reason this is ``flock`` and not a PID file.

        A process killed with SIGKILL cannot clean up after itself. With
        ``flock`` the kernel drops the lock and the next process starts; with a
        PID file the next start finds a PID nobody can signal and refuses —
        which is exactly what "just restart it" turns into.
        """
        script = (
            "import sys, time;"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r});"
            "from apps.engine.lease import Lease;"
            f"l = Lease({str(tmp_path / 's.lock')!r});"
            "print('acquired', l.acquire(), flush=True);"
            "time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "acquired True"
            # Held right now.
            assert Lease(tmp_path / "s.lock").acquire() is False
            proc.kill()
            proc.wait(timeout=10)
            # Kernel released it; the file is still on disk and must not block.
            assert (tmp_path / "s.lock").exists()
            survivor = Lease(tmp_path / "s.lock")
            try:
                assert survivor.acquire() is True
            finally:
                survivor.release()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    def test_read_lease_does_not_believe_a_stale_file(self, tmp_path):
        path = tmp_path / "s.lock"
        holder = Lease(path, port=8768)
        holder.acquire()
        info = read_lease(path)
        assert info.held is True
        assert info.pid and info.port == 8768
        holder.release()
        # The file survives the holder. Reporting it as held would send an
        # operator hunting a process that no longer exists.
        assert read_lease(path).held is False
        # ...while still naming who had it.
        assert read_lease(path).port == 8768

    def test_read_lease_of_a_missing_file(self, tmp_path):
        info = read_lease(tmp_path / "never-written.lock")
        assert info.held is False
        assert info.pid is None

    def test_unwritable_path_is_not_reported_as_acquired(self, tmp_path):
        """A lock we cannot take must not look like one we hold, or the caller
        starts a scheduler nobody can see."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        with pytest.raises(OSError):
            Lease(blocker / "s.lock").acquire()


class TestMinuteClaim:
    def test_only_one_claim_per_minute_wins(self, tmp_path):
        store = RpaStore(tmp_path / "rpa.db")
        store.save_schedule({"id": "s1", "name": "n", "flow_id": "f1", "cron": "* * * * *"})
        assert store.claim_schedule_minute("s1", "2026-01-01 10:00") is True
        assert store.claim_schedule_minute("s1", "2026-01-01 10:00") is False
        assert store.claim_schedule_minute("s1", "2026-01-01 10:01") is True

    def test_the_column_exists_on_a_pre_existing_database(self, tmp_path):
        """``CREATE TABLE IF NOT EXISTS`` never adds a column. rpa.db ships in
        the repo and is not recreated between installs, so without an explicit
        add the claim would fail on every existing database."""
        import sqlite3

        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE schedules (id TEXT PRIMARY KEY, name TEXT, flow_id TEXT,"
            " cron TEXT, enabled INTEGER NOT NULL DEFAULT 1, last_run_at TEXT,"
            " last_status TEXT, last_run_id TEXT, created_at TEXT, updated_at TEXT);"
        )
        conn.execute(
            "INSERT INTO schedules (id,name,flow_id,cron) VALUES ('s1','n','f1','* * * * *')"
        )
        conn.commit()
        conn.close()

        store = RpaStore(path)
        assert store.claim_schedule_minute("s1", "2026-01-01 10:00") is True
        assert store.claim_schedule_minute("s1", "2026-01-01 10:00") is False

    def test_two_stores_sharing_a_file_do_not_both_win(self, tmp_path):
        path = tmp_path / "rpa.db"
        a, b = RpaStore(path), RpaStore(path)
        a.save_schedule({"id": "s1", "name": "n", "flow_id": "f1", "cron": "* * * * *"})
        minute = "2026-01-01 10:00"
        assert [a.claim_schedule_minute("s1", minute),
                b.claim_schedule_minute("s1", minute)] == [True, False]

    def test_finishing_a_run_does_not_swallow_the_next_minute(self, tmp_path):
        """``last_run_at`` is when a run *ended*. A run started at 10:00:50 ends
        at 10:01:30, so a de-dupe stamp taken from it would cancel the 10:01
        tick. That is why the claim has its own column."""
        store = RpaStore(tmp_path / "rpa.db")
        store.save_schedule({"id": "s1", "name": "n", "flow_id": "f1", "cron": "* * * * *"})
        assert store.claim_schedule_minute("s1", "2026-01-01 10:00") is True
        store.mark_schedule_run("s1", "run_1", "ok")
        assert store.claim_schedule_minute("s1", "2026-01-01 10:01") is True


#: A flow that exists and does nothing. It has to exist: ``_fire`` returns
#: ``flow_missing`` before it ever reaches ``on_fire``, so a schedule pointing at
#: a phantom flow would measure the wrong thing — and would pass for "fired
#: exactly once" while firing zero times.
_TRIVIAL_GRAPH = {
    "version": 1,
    "entry": "n1",
    "nodes": [
        {"id": "n1", "type": "set_var", "name": "tick",
         "params": {"name": "fired_at", "value": "1"}},
    ],
    "edges": [],
}


def _seed(db: str) -> None:
    store = RpaStore(db)
    store.save_flow("f1", "trivial", dict(_TRIVIAL_GRAPH))
    store.save_schedule(
        {"id": "s1", "name": "every minute", "flow_id": "f1", "cron": "* * * * *"}
    )


_CHILD = """
import sys, time
sys.path.insert(0, {root!r})
from apps.engine.lease import Lease
from apps.engine.scheduler import Scheduler
from apps.engine.store import RpaStore

db, lock, marker, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sched = Scheduler(store=RpaStore(db), tick_seconds=3600,
                  lease=Lease(lock, port=port), port=port)

# Counting through a file rather than through tick()'s return value on purpose:
# start() fires once on boot by design, so a manual tick() afterwards is
# legitimately a no-op and asserting on its return races the boot thread.
def _fired(schedule_id, run_id):
    with open(marker, "a") as fh:
        fh.write(schedule_id + "\\n")

sched.on_fire = _fired
started = sched.start()
time.sleep(0.8)
import json
print(json.dumps({{"started": started, "status": sched.status()}}), flush=True)
sched.stop()
"""


class TestThreeProcesses:
    """The incident, reproduced: N processes, one database, one schedule."""

    @staticmethod
    def _run(tmp_path, count: int, tag: str = "s"):
        import json

        db = str(tmp_path / "rpa.db")
        lock = str(tmp_path / "s.lock")
        marker = str(tmp_path / f"{tag}.marker")
        # The parent owns setup. Three children each writing the same row is a
        # race in the test, not in the code under test.
        _seed(db)
        script = _CHILD.format(root=str(Path(__file__).resolve().parents[1]))
        procs = [
            subprocess.Popen([sys.executable, "-c", script, db, lock, marker, str(8700 + i)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for i in range(count)
        ]
        reports = []
        try:
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=90)
                assert proc.returncode == 0, stderr
                reports.append(json.loads(stdout))
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
        lines = Path(marker).read_text().split() if Path(marker).exists() else []
        return reports, lines

    def test_only_one_process_arms_and_the_schedule_fires_once(self, tmp_path):
        reports, fired = self._run(tmp_path, 3)
        assert len(reports) == 3

        started = [r["started"] for r in reports]
        assert started.count(True) == 1, started
        assert started.count(False) == 2, started

        # The observable consequence, measured where it actually shows up.
        assert fired == ["s1"], fired

        for report in reports:
            if report["started"]:
                continue
            status = report["status"]
            assert status["standby"] is True
            assert status["running"] is False
            # The refusal has to name a PID, or all the operator sees is a
            # process that is up, serving, and quietly not scheduling. The
            # lease is deliberately not asserted as still-held here: by the
            # time a standby reports, the holder may legitimately have exited,
            # and pinning that would make the test a race rather than a
            # contract. ``TestLease`` covers refusal-while-held directly.

    def test_two_processes_also_agree(self, tmp_path):
        reports, fired = self._run(tmp_path, 2, tag="two")
        assert [r["started"] for r in reports].count(True) == 1
        assert fired == ["s1"], fired

    def test_the_lease_is_released_so_a_restart_can_arm(self, tmp_path):
        import json

        db, lock = str(tmp_path / "rpa.db"), str(tmp_path / "s.lock")
        marker = str(tmp_path / "restart.marker")
        _seed(db)
        script = _CHILD.format(root=str(Path(__file__).resolve().parents[1]))
        run = lambda port: subprocess.run(  # noqa: E731
            [sys.executable, "-c", script, db, lock, marker, str(port)],
            capture_output=True, text=True, timeout=90)
        first = run(8700)
        assert first.returncode == 0, first.stderr
        assert json.loads(first.stdout)["started"] is True
        # The holder exited. Its file is still on disk and must not block.
        assert read_lease(lock).held is False
        second = run(8701)
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["started"] is True


class TestSchedulerUsesTheClaim:
    def test_tick_fires_once_across_two_schedulers_on_one_store(self, tmp_path):
        from apps.engine.scheduler import Scheduler

        path = tmp_path / "rpa.db"
        _seed(str(path))
        store, other = RpaStore(path), RpaStore(path)
        # Separate lock files on purpose: both schedulers legitimately hold a
        # lease of their own, which is exactly the case the DB claim exists for
        # — a process that starts after the holder already fired this minute.
        a = Scheduler(store=store, tick_seconds=3600, lease=Lease(tmp_path / "a.lock"))
        b = Scheduler(store=other, tick_seconds=3600, lease=Lease(tmp_path / "b.lock"))

        first = a.tick()
        second = b.tick()

        assert len(first) == 1
        assert first[0]["status"] in ("started", "refused")
        assert second == []
        assert b.stats.skipped_claimed == 1
        assert store.get_schedule("s1")["last_fire_minute"] == datetime.now().strftime("%Y-%m-%d %H:%M")

    def test_the_fast_path_skip_is_counted_too(self, tmp_path):
        """A standby's stats have to show the skip, or "why did nothing fire"
        is answered by a counter that stayed at zero."""
        from apps.engine.scheduler import Scheduler

        path = tmp_path / "rpa.db"
        _seed(str(path))
        sched = Scheduler(store=RpaStore(path), tick_seconds=3600,
                          lease=Lease(tmp_path / "a.lock"))
        assert len(sched.tick()) == 1
        assert sched.tick() == []
        assert sched.stats.fired == 1
        assert sched.stats.skipped_claimed == 1

    def test_standby_reason_is_empty_for_the_holder(self, tmp_path):
        from apps.engine.scheduler import Scheduler

        store = RpaStore(tmp_path / "rpa.db")
        sched = Scheduler(store=store, tick_seconds=5, lease=Lease(tmp_path / "s.lock"))
        try:
            assert sched.start() is True
            status = sched.status()
            assert status["running"] is True
            assert status["standby"] is False
            assert status["standby_reason"] == ""
        finally:
            sched.stop()
        time.sleep(0.05)
        assert sched.status()["running"] is False
