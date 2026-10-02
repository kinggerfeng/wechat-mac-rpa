"""Graph executor.

Walks a flow document from its entry node. Each node runs with its declared
retry/timeout policy, its outputs are bound into the run scope, and the first
outgoing edge whose port and condition match becomes the next node.

Two design choices worth stating, because both are load-bearing for a bot that
drives a real screen:

* **A step budget.** A graph can contain a cycle (that is how the main loop is
  expressed). A runaway cycle would click at the screen forever, so the walker
  enforces ``max_steps`` and fails loudly rather than spinning. The default is
  generous enough for a long unattended run.
* **Traces are emitted per attempt, not per node.** A retried node produces one
  span per attempt so a failure that recovered is still visible in the trace with
  its real duration and error.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .context import FlowContext, FlowScope, evaluate_condition
from .registry import BaseNode, NodeRegistry, get_node_registry
from .schema import Edge, Flow, FlowError, Node, NodeAborted, NodeError, validate_flow

#: Key a node may return to choose its next port without raising, e.g.
#: ``return {"__branch__": "true", "value": ok}`` from a condition node. Chosen
#: over raising because a branch is a successful execution and should be traced
#: as one.
BRANCH_KEY = "__branch__"

#: Hard ceiling on node executions in a single run.
DEFAULT_MAX_STEPS = 100_000

#: How deep ``_walk``-style recursive flows may nest before we call it a cycle loop.
DEFAULT_MAX_VISITS_PER_RUN = 0  # 0 = unlimited


class FlowAborted(RuntimeError):
    """Raised when a run is cancelled mid-walk."""


@dataclass
class Span:
    """One execution of one node. The unit of the execution trace."""

    run_id: str
    node_id: str
    node_type: str
    name: str
    attempt: int
    status: str = "running"  # running | ok | error | skipped
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    duration_ms: int | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    screenshot_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "name": self.name,
            "attempt": self.attempt,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "error": self.error,
            "screenshot_path": self.screenshot_path,
        }


#: ``(span) -> None``. Called for every attempt, synchronously, on the executor
#: thread. The desktop API's SSE broadcaster attaches here.
SpanHook = Callable[[Span], None]


@dataclass
class RunResult:
    run_id: str
    flow_id: str
    status: str  # ok | error | aborted
    steps: int = 0
    error: str | None = None
    failed_node: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    @property
    def duration_ms(self) -> int:
        return int(((self.ended_at or time.time()) - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "flow_id": self.flow_id,
            "status": self.status,
            "steps": self.steps,
            "error": self.error,
            "failed_node": self.failed_node,
            "scope": self.scope,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class FlowExecutor:
    """Executes one flow document, at most once at a time.

    The instance holds the live node objects (they own heavy resources like screen
    handles), so it is not reusable across concurrent runs of the same process.
    :class:`~src.flow.runner.FlowRunner` gives each run its own executor.
    """

    def __init__(
        self,
        registry: NodeRegistry | None = None,
        span_hook: SpanHook | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        validate: bool = True,
        setup_context: Callable[[FlowContext], None] | None = None,
    ) -> None:
        self.registry = registry or get_node_registry()
        self.span_hook = span_hook
        self.max_steps = max_steps
        self.validate = validate
        self.setup_context = setup_context
        self._flow_default_path: str | None = None
        self._abort = threading.Event()
        self.ctx: FlowContext | None = None
        self._live_nodes: list[BaseNode] = []

    def abort(self) -> None:
        self._abort.set()
        if self.ctx:
            self.ctx.request_abort()

    def run(
        self,
        flow: Flow,
        run_id: str,
        variables: dict[str, Any] | None = None,
    ) -> RunResult:
        self._abort.clear()
        graph = flow.graph
        scope = FlowScope({**(graph.get("variables") or {}), **(variables or {})})
        self.ctx = FlowContext(scope=scope, run_id=run_id, flow_id=flow.id)
        # Exposed so a node that runs a nested flow can re-emit that flow's spans
        # into this run's trace. Without it a sub-flow's steps are invisible in
        # the parent's trace panel, which is where an operator looks when a
        # delegated step misbehaves.
        self.ctx.span_sink = self.span_hook
        default_path = graph.get("default_path")
        self._flow_default_path = str(default_path) if default_path else None
        result = RunResult(run_id=run_id, flow_id=flow.id, status="ok")
        if self.setup_context is not None:
            try:
                self.setup_context(self.ctx)
            except Exception as exc:  # noqa: BLE001
                result.status = "error"
                result.error = f"服务初始化失败: {type(exc).__name__}: {exc}"
                result.ended_at = time.time()
                return result

        if self.validate:
            issues = [i for i in validate_flow(graph, self.registry.types()) if i.severity == "error"]
            if issues:
                result.status = "error"
                result.error = "; ".join(f"{i.code}: {i.message}" for i in issues[:5])
                result.ended_at = time.time()
                return result

        try:
            nodes: dict[str, Node] = {}
            for raw in graph.get("nodes", []):
                node = Node.from_dict(raw)
                nodes[node.id] = node
            edges = self._index_edges(graph.get("edges") or [])
            entry = str(graph.get("entry") or "")
            if entry not in nodes:
                raise FlowError(f"入口节点 {entry!r} 不存在")
            self._walk(entry, nodes, edges, result)
        except FlowAborted:
            result.status = "aborted"
        except (FlowError, NodeError) as exc:
            result.status = "error"
            result.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - executor must not leak node crashes
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            result.scope["traceback"] = traceback.format_exc(limit=8)
        finally:
            self._shutdown_nodes()
            result.ended_at = time.time()
            # ``export`` includes the node layer so a consumer of the run result
            # can still address ``{{n_perceive.messages}}``. The canvas and the
            # trace panel only want readable names, so they get the flat view.
            result.scope = scope.export()
        return result

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _index_edges(raw_edges: Iterable[Any]) -> dict[str, list[Edge]]:
        outgoing: dict[str, list[Edge]] = {}
        for raw in raw_edges:
            edge = Edge.from_dict(raw)
            outgoing.setdefault(edge.source, []).append(edge)
        for group in outgoing.values():
            group.sort(key=lambda e: e.id)
        return outgoing

    def _walk(
        self,
        node_id: str,
        nodes: dict[str, Node],
        edges: dict[str, list[Edge]],
        result: RunResult,
    ) -> None:
        assert self.ctx is not None
        current = node_id
        while current:
            if self._abort.is_set():
                raise FlowAborted()
            result.steps += 1
            if result.steps > self.max_steps:
                raise FlowError(f"超过最大执行步数 {self.max_steps}，疑似流程死循环")

            node = nodes.get(current)
            if node is None:
                raise FlowError(f"节点 {current!r} 不存在")

            if node.disabled:
                self._emit_skip(node)
                current = self._next(current, edges, result)
                continue

            try:
                outputs = self._execute(node, result)
            except _Branch as signal:
                current = self._resolve_port(signal.node, signal.port, edges, result)
                continue
            except NodeAborted as exc:
                raise FlowAborted(str(exc)) from exc
            except (NodeError, FlowError, Exception) as exc:  # noqa: BLE001
                current = self._handle_error(node, exc, edges, result)
                if current is _FAIL:
                    return
                continue

            self.ctx.bind_outputs(node, outputs)
            chosen = outputs.get(BRANCH_KEY) if isinstance(outputs, dict) else None
            current = (
                self._resolve_port(node, str(chosen), edges, result)
                if chosen
                else self._next(node.id, edges, result)
            )

    def _execute(self, node: Node, result: RunResult) -> dict[str, Any]:
        assert self.ctx is not None
        spec = self.registry.get(node.type)
        instance = spec.handler(node.params)
        instance.ctx = self.ctx
        # The node's graph id. A loop needs it to key its iteration counter: a
        # handler instance is rebuilt for every attempt, so id(instance) is a
        # different key each pass and a max_iterations cap keyed that way never
        # trips.
        instance.node_id = node.id
        # Design-time declarations chosen while authoring the flow.
        instance.path = node.path or self._flow_default_path
        instance.target = node.target
        self._live_nodes.append(instance)
        self.ctx.count_node(node.id)

        attempts = max(0, node.retry.max) + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self._abort.is_set():
                raise FlowAborted()
            span = Span(
                run_id=result.run_id,
                node_id=node.id,
                node_type=node.type,
                name=node.name or node.type,
                attempt=attempt,
                inputs=self._describe_inputs(instance),
            )
            # Emitted twice on purpose: once as `running` so a consumer can show
            # the node the instant it starts (a bot blocked on a permission prompt
            # produces exactly this one event and then silence), and once at
            # completion with the real status and duration.
            self._emit(span)
            started = time.monotonic()
            try:
                outputs = self._call_with_timeout(instance, node)
                span.status = "ok"
                span.outputs = _summarise(outputs)
                span.screenshot_path = _screenshot_of(outputs)
                return outputs
            except _Branch as signal:
                span.status = "ok"
                span.outputs = {"__branch__": signal.port}
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                span.status = "error"
                span.error = f"{type(exc).__name__}: {exc}"
                span.outputs = {"traceback": traceback.format_exc(limit=6)}
                if attempt < attempts and node.retry.delay:
                    time.sleep(node.retry.delay)
            finally:
                span.ended_at = time.time()
                span.duration_ms = int((time.monotonic() - started) * 1000)
                self._emit(span)
        assert last_error is not None
        raise last_error

    def _call_with_timeout(self, instance: BaseNode, node: Node) -> dict[str, Any]:
        if not node.timeout or node.timeout <= 0:
            return instance.execute() or {}

        box: dict[str, Any] = {}

        def target() -> None:
            # The exception MUST be captured here. An uncaught one kills this
            # thread silently, the worker then looks "finished on time", and the
            # node is reported as a success with empty output — the single worst
            # failure mode in an RPA: the flow reports success while the thing it
            # was supposed to do never happened.
            try:
                box["value"] = instance.execute() or {}
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=target, name=f"flow-node-{node.id}", daemon=True)
        worker.start()
        worker.join(node.timeout)
        if worker.is_alive():
            # The thread is daemonised and will not be joined again; a node that
            # overruns its budget may still be holding the screen, so the run is
            # failed rather than continued.
            raise NodeError(f"节点 {node.id} 超时（>{node.timeout}s）")
        if "error" in box:
            raise box["error"]
        if "value" not in box:
            raise NodeError(f"节点 {node.id} 未返回结果且未报告异常")
        return box["value"]

    def _handle_error(
        self,
        node: Node,
        exc: Exception,
        edges: dict[str, list[Edge]],
        result: RunResult,
    ) -> Any:
        assert self.ctx is not None
        if node.on_error == "ignore":
            self.ctx.scope.bind(f"{node.id}.error", str(exc))
            return self._next(node.id, edges, result)
        if node.on_error == "branch":
            port = self._resolve_port(node, "error", edges, result, missing_ok=True)
            if port:
                return port
        result.status = "error"
        result.error = f"节点 {node.id} ({node.type}) 失败: {exc}"
        result.failed_node = node.id
        return _FAIL

    def _resolve_port(
        self,
        node: Node,
        port: str,
        edges: dict[str, list[Edge]],
        result: RunResult,
        missing_ok: bool = False,
    ) -> Any:
        assert self.ctx is not None
        for edge in edges.get(node.id, ()):
            if edge.source_port != port:
                continue
            if edge.condition and not evaluate_condition(edge.condition, self.ctx.scope):
                continue
            return edge.target
        if missing_ok:
            return ""
        return ""

    def _next(self, node_id: str, edges: dict[str, list[Edge]], result: RunResult) -> str:
        """Pick the next node: a conditional edge first, then the default ``ok``."""
        assert self.ctx is not None
        candidates = edges.get(node_id, [])
        for edge in candidates:
            if edge.source_port != "ok":
                continue
            if edge.condition and evaluate_condition(edge.condition, self.ctx.scope):
                return edge.target
        for edge in candidates:
            if edge.source_port != "ok" or edge.condition:
                continue
            return edge.target
        return ""

    def _describe_inputs(self, instance: BaseNode) -> dict[str, Any]:
        described: dict[str, Any] = {}
        for spec in instance.spec.params:
            if spec.name in instance.params:
                described[spec.name] = instance.params[spec.name]
        return described

    def _emit(self, span: Span) -> None:
        if self.span_hook is not None:
            try:
                self.span_hook(span)
            except Exception:  # noqa: BLE001 - a broken trace sink must not kill a run
                pass

    def _emit_skip(self, node: Node) -> None:
        span = Span(
            run_id="", node_id=node.id, node_type=node.type, name=node.name or node.type,
            attempt=0, status="skipped", ended_at=time.time(), duration_ms=0,
        )
        if self.span_hook is not None:
            try:
                self.span_hook(span)
            except Exception:  # noqa: BLE001
                pass

    def _shutdown_nodes(self) -> None:
        for node in self._live_nodes:
            try:
                node.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._live_nodes.clear()


class _Branch(Exception):
    """Raised by ``branch()`` to hand control to a named port instead of failing."""

    def __init__(self, node: Node, port: str) -> None:
        super().__init__(f"{node.id} -> {port}")
        self.node = node
        self.port = port


class _Fail:
    pass


_FAIL = _Fail()


def branch(node: Node, port: str) -> Any:
    """Unwind to the edge bound to ``port`` on ``node``."""
    raise _Branch(node, port)


def _screenshot_of(outputs: dict[str, Any]) -> str | None:
    if not isinstance(outputs, dict):
        return None
    for key in ("screenshot_path", "screenshot", "image_path"):
        value = outputs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _summarise(outputs: Any, limit: int = 400) -> Any:
    """Keep spans readable: truncate long strings, drop non-serialisable values."""
    if isinstance(outputs, dict):
        return {k: _summarise(v, limit) for k, v in list(outputs.items())[:24]}
    if isinstance(outputs, list):
        return [_summarise(v, limit) for v in outputs[:12]] + ([f"…(+{len(outputs) - 12})"] if len(outputs) > 12 else [])
    if isinstance(outputs, str):
        return outputs if len(outputs) <= limit else outputs[:limit] + f"…(+{len(outputs) - limit})"
    if isinstance(outputs, (int, float, bool, type(None))):
        return outputs
    to_dict = getattr(outputs, "to_dict", None)
    if callable(to_dict):
        return _summarise(to_dict(), limit)
    if hasattr(outputs, "__dict__"):
        return {k: _summarise(v, limit) for k, v in list(vars(outputs).items())[:24]}
    return repr(outputs)[:limit]
