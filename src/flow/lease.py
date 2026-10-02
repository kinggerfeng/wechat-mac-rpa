"""One scheduler per database, enforced by the kernel.

The failure this prevents is not hypothetical. Three uvicorn processes were
started against the same ``data/rpa.db`` during development, each loaded the
same enabled schedule, and each ran its own scheduler thread — so one cron
entry fired three times per interval, and each fire took a screenshot. With
screen recording involved that meant three system permission prompts per
interval, which looked like a TCC bug and was not one.

Two guards, and they are not redundant:

* :class:`Lease` — at most one process runs a scheduler loop at all. Cheap,
  covers every code path including ones added later.
* :meth:`RpaStore.claim_schedule_minute` — a compare-and-set on the schedule row
  itself. Covers the cases the lease cannot: a process that starts *after* the
  holder already fired this minute, and a lease lost to a crash mid-minute.

Either alone leaves a hole. Together, firing twice needs both a stolen lease and
a schedule whose claim stamp was rolled back.

Deliberately ``flock`` and not a lockfile containing a PID: the kernel drops
``flock`` when the holder dies, so a killed process leaves nothing to reap. A
PID file written by a process that then gets SIGKILLed blocks the next start
until someone notices and deletes it, which is exactly the failure mode a
"just restart it" instruction runs into.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform guard
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


DEFAULT_LOCK_PATH = Path(__file__).resolve().parents[2] / "data" / "scheduler.lock"


@dataclass
class LeaseInfo:
    """What the lock file says about the current holder.

    Readable without the lock. That is the point: an operator whose schedules
    mysteriously stopped firing needs to see *who* holds it, and a plain
    ``cat`` of the file is enough.
    """

    path: str
    held: bool
    pid: int | None = None
    host: str | None = None
    port: int | None = None
    since: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "held": self.held,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "since": self.since,
            "since_iso": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since))
                if self.since else None
            ),
        }


def read_lease(path: str | Path = DEFAULT_LOCK_PATH) -> LeaseInfo:
    """Describe the current holder without keeping the lease.

    Existence of the file proves nothing — it survives the holder, since the
    kernel releases an ``flock`` when the process dies but the inode stays. So
    this actually tries the lock and reports what it finds. A stale file
    therefore reads as "not held" while still naming the last holder, which is
    what an operator staring at "my schedules stopped firing" needs to see.
    """
    lock_path = Path(path)
    if not lock_path.exists():
        return LeaseInfo(path=str(lock_path), held=False)

    stale = _read_payload(lock_path)
    if fcntl is None:  # pragma: no cover - Windows
        return LeaseInfo(path=str(lock_path), held=True, **stale)

    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return LeaseInfo(path=str(lock_path), held=True, **stale)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Could not take it: a live process has it, whatever the reason the
        # kernel gave. The payload is the best available identification.
        return LeaseInfo(path=str(lock_path), held=True, **stale)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return LeaseInfo(path=str(lock_path), held=False, **stale)


def _read_payload(lock_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        # A truncated file means a holder died mid-write. Naming the last
        # holder is impossible, so say only that the file is there.
        return {}
    since = raw.get("since")
    return {
        "pid": raw.get("pid"),
        "host": raw.get("host"),
        "port": raw.get("port"),
        "since": float(since) if isinstance(since, (int, float)) else None,
    }


def _env_port() -> int | None:
    raw = os.environ.get("RPA_STUDIO_PORT", "").strip()
    return int(raw) if raw.isdigit() else None


class Lease:
    """An exclusive, advisory, per-database claim on being *the* scheduler.

    ``flock`` is advisory, which is the right trade here: every participant is
    our own code and all of it goes through this class. An advisory lock that
    only the participants honour would be useless; one that the kernel also
    enforces against a killed process is exactly what we want.
    """

    def __init__(self, path: str | Path = DEFAULT_LOCK_PATH, port: int | None = None) -> None:
        self.path = Path(path)
        # The port is only a hint for whoever is debugging "which server is
        # this?". Nothing reads it to make a decision, so guessing wrong is
        # harmless and not worth a required argument.
        self.port = port if port is not None else _env_port()
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lease. False if someone else already has it."""
        if self.held:
            return True
        if fcntl is None:  # pragma: no cover - Windows
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            # EWOULDBLOCK/EAGAIN is the ordinary "someone has it". Anything else
            # — a read-only filesystem, a bad path — must not look like success,
            # or the caller starts a scheduler nobody can see.
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            raise
        payload = json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "port": self.port,
            "since": time.time(),
        })
        os.ftruncate(fd, 0)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def info(self) -> LeaseInfo:
        if self.held:
            return LeaseInfo(
                path=str(self.path), held=True, pid=os.getpid(),
                host=socket.gethostname(), port=self.port, since=time.time(),
            )
        return read_lease(self.path)

    def __enter__(self) -> "Lease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
