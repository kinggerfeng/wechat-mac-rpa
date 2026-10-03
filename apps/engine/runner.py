"""Run manager: owns the lifecycle of flow runs.

Responsibilities that do not belong in the executor:

* **Persistence** — a run row is written before the first node, spans are streamed
  to SQLite as they complete, and the run row is finalised at the end.
* **Live trace fan-out** — subscribers (the desktop UI's SSE stream) get each span
  as it is emitted, not after the run finishes. A 5-second polling loop produces
  very little output; a bot that is stuck on a permission prompt produces one span
  and then silence, and the difference matters when diagnosing.
* **Concurrency** — one run per flow at a time, enforced by a lock keyed on flow
  id. Two runs of the same flow would fight over the WeChat window.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any, Callable, Iterator

from .executor import FlowExecutor, RunResult, Span
from .schema import Flow
from .services import register_default_services
from .store import RpaStore, get_store, new_run_id

#: Spans buffered per subscriber before the slowest one starts dropping.
SUBSCRIBER_QUEUE = 2000


class Subscriber:
    """An SSE consumer. ``queue`` is bounded; a slow reader loses old spans."""

    def __init__(self) -> None:
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=SUBSCRIBER_QUEUE)
        self.dropped = 0

    def put(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def stream(self) -> Iterator[dict[str, Any]]:
        while True:
            yield self.queue.get()


class RunManager:
    def __init__(self, store: RpaStore | None = None) -> None:
        self.store = store or get_store()
        self._running: dict[str, str] = {}  # flow_id -> run_id
        self._executors: dict[str, FlowExecutor] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.RLock()
        self._persisted_runs: set[str] = set()
        self.store.mark_stale_runs_failed()

    # -- introspection -----------------------------------------------------

    def is_running(self, flow_id: str) -> bool:
        with self._lock:
            return flow_id in self._running

    def active_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"flow_id": flow_id, "run_id": run_id, "started_at": self.store.get_run(run_id)["started_at"]}
                for flow_id, run_id in self._running.items()
                if self.store.get_run(run_id) is not None
            ]

    def is_busy(self) -> bool:
        """True when any run holds the screen. The bot start button uses this."""
        with self._lock:
            return bool(self._running)

    # -- submission --------------------------------------------------------

    def submit(
        self,
        flow: Flow,
        trigger_type: str = "manual",
        trigger_ref: str = "",
        variables: dict[str, Any] | None = None,
        dry_run: bool = False,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Start a run. Returns immediately with a run id unless ``wait``.

        Refuses when the same flow already has a live run, and refuses when a
        *different* flow holds the screen: both would drive the same WeChat window.
        """
        with self._lock:
            if flow.id in self._running:
                return {
                    "accepted": False,
                    "reason": "already_running",
                    "run_id": self._running[flow.id],
                    "message": f"流程 {flow.name} 已有运行中的实例",
                }
            holder = next((fid for fid in self._running if fid != flow.id), None)
            if holder is not None:
                other = self.store.get_flow(holder)
                return {
                    "accepted": False,
                    "reason": "screen_busy",
                    "run_id": self._running[holder],
                    "message": f"流程 {other.name if other else holder} 正在占用微信窗口，请先停止",
                }
            run_id = new_run_id()
            self._running[flow.id] = run_id

        self.store.start_run(run_id, flow.id, trigger_type, trigger_ref, variables or {})
        self._publish({"type": "run.started", "run_id": run_id, "flow_id": flow.id, "flow_name": flow.name})

        def target() -> None:
            self._execute(flow, run_id, variables or {}, dry_run)

        thread = threading.Thread(target=target, name=f"flow-run-{run_id}", daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        thread.start()

        if wait:
            thread.join()
            return {"accepted": True, "run_id": run_id, "result": self.store.get_run(run_id)}
        return {"accepted": True, "run_id": run_id, "status": "running"}

    def _execute(self, flow: Flow, run_id: str, variables: dict[str, Any], dry_run: bool) -> None:
        executor = FlowExecutor(
            span_hook=lambda span: self._on_span(run_id, span),
            setup_context=lambda ctx: register_default_services(ctx, dry_run=dry_run),
        )
        with self._lock:
            self._executors[run_id] = executor
        try:
            result = executor.run(flow, run_id, variables)
            self.store.finish_run(result.to_dict())
            self._publish({
                "type": "run.finished",
                "run_id": run_id,
                "flow_id": flow.id,
                "status": result.status,
                "steps": result.steps,
                "error": result.error,
                "duration_ms": result.duration_ms,
            })
        except Exception as exc:  # noqa: BLE001 - a crashed run must still be recorded
            self.store.finish_run({
                "run_id": run_id,
                "flow_id": flow.id,
                "status": "error",
                "steps": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "scope": {"traceback": traceback.format_exc(limit=10)},
                "ended_at": time.time(),
            })
            self._publish({"type": "run.finished", "run_id": run_id, "flow_id": flow.id, "status": "error", "error": str(exc)})
        finally:
            with self._lock:
                self._running.pop(flow.id, None)
                self._executors.pop(run_id, None)
                self._threads.pop(run_id, None)

    # -- abort -------------------------------------------------------------

    def abort(self, flow_id: str) -> dict[str, Any]:
        with self._lock:
            run_id = self._running.get(flow_id)
            executor = self._executors.get(run_id or "")
        if not run_id or executor is None:
            return {"aborted": False, "reason": "not_running"}
        executor.abort()
        return {"aborted": True, "run_id": run_id}

    def abort_all(self) -> list[str]:
        with self._lock:
            run_ids = list(self._executors)
        for run_id in run_ids:
            executor = self._executors.get(run_id)
            if executor:
                executor.abort()
        return run_ids

    # -- trace fan-out -----------------------------------------------------

    def _on_span(self, run_id: str, span: Span) -> None:
        payload = span.to_dict()
        payload["type"] = "span"
        # The hook fires twice per attempt: once when the node starts and once
        # when it settles. Only the settled one is a fact worth a row; both go to
        # subscribers so the UI can show a node the moment it starts.
        if span.ended_at is not None:
            self.store.insert_span(payload)
            payload = {**payload, "phase": "finished"}
        else:
            payload = {**payload, "phase": "started"}
        self._publish(payload)

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber()
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(event)

    def backfill(self, run_id: str) -> list[dict[str, Any]]:
        return [{"type": "span", **span} for span in self.store.list_spans(run_id)]


_manager: RunManager | None = None
_manager_lock = threading.Lock()


def get_run_manager() -> RunManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = RunManager()
        return _manager
