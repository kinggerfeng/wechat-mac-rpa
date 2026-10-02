"""Flow graph schema: the contract shared by the executor, the HTTP API and the canvas.

A flow is a directed graph. The executor walks it; the canvas edits it; both agree
on this module and nothing else.

Graph document (JSON, persisted in `flows.graph_json`):

    {
      "version": 1,
      "name": "微信自动回复主循环",
      "description": "...",
      "entry": "n_start",
      "variables": {"interval": 5.0},
      "nodes": [
        {
          "id": "n_perceive",
          "type": "perceive",
          "name": "感知微信窗口",
          "position": {"x": 120, "y": 80},
          "params": {"mode": "auto"},
          "retry": {"max": 1, "delay": 2.0},
          "timeout": 30,
          "on_error": "fail",
          "outputs": ["result", "screenshot"]
        }
      ],
      "edges": [
        {"id": "e1", "source": "n_start", "source_port": "ok",
         "target": "n_perceive", "condition": null}
      ]
    }
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = 1

NODE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

#: Values accepted by ``Node.on_error``.
ON_ERROR_CHOICES = ("fail", "branch", "ignore")

#: The dual-path declaration, chosen when the flow is configured.
#:
#: ``auto`` exists but is a deliberate choice, not a default: it means "try the
#: element library, and if the element is genuinely missing, ask a vision model
#: instead". It costs a model call and gives up determinism, so a flow author
#: should reach for it only for nodes that are known to be element-free.
PATH_CHOICES = ("element", "element_strict", "vision", "auto")

#: Node types that can declare a path. Everything else ignores it — a `wait` node
#: has no opinion about how its neighbours locate things.
PATH_AWARE_TYPES = ("locate", "click", "vlm_act", "vlm_describe", "vlm_verify", "switch_chat")

#: Port every node type is required to expose.
BASE_PORTS = ("ok",)


class FlowError(ValueError):
    """Raised when a graph document is structurally invalid."""


class NodeError(RuntimeError):
    """Raised by a node implementation when it fails in a way the graph can handle.

    Nodes should raise this (not bare ``Exception``) when the failure is expected
    at runtime — a WeChat window that is not open is a branch, not a crash.
    """


class NodeAborted(RuntimeError):
    """Raised by a node when a stop request arrived while it was working.

    Distinct from :class:`NodeError` so the executor reports ``aborted`` rather
    than ``error``: a user pressing stop is not a run failure.
    """


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Position:
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, raw: Any) -> "Position":
        if not isinstance(raw, dict):
            return cls()
        return cls(x=_as_float(raw.get("x"), 0.0), y=_as_float(raw.get("y"), 0.0))


@dataclass
class Retry:
    max: int = 0
    delay: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"max": self.max, "delay": self.delay}

    @classmethod
    def from_dict(cls, raw: Any) -> "Retry":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            max=max(0, int(_as_float(raw.get("max"), 0))),
            delay=max(0.0, _as_float(raw.get("delay"), 0.0)),
        )


@dataclass
class Node:
    """One step in the graph.

    ``inputs`` are not stored on the node: the executor derives what a node can
    see from the variables its predecessors bound. The canvas shows that binding
    as a read-only hint on the node.

    ``path`` is the dual-path declaration and is deliberately a **first-class
    field, not a param**. Which of the two location strategies a node uses is a
    design-time decision made while authoring the flow, and it has to be visible
    on the node itself — in the canvas, in the palette, in the trace. Burying it
    in ``params`` next to a confidence threshold would make the single most
    consequential choice in an RPA flow look like a tuning knob, and would let a
    run quietly differ from the design.
    """

    id: str
    type: str
    name: str = ""
    position: Position = field(default_factory=Position)
    params: dict[str, Any] = field(default_factory=dict)
    retry: Retry = field(default_factory=Retry)
    timeout: float | None = None
    on_error: str = "fail"
    outputs: list[str] = field(default_factory=list)
    disabled: bool = False
    #: One of :data:`PATH_CHOICES`, or ``None`` to inherit the flow default.
    path: str | None = None
    #: Target application this node drives, e.g. ``"wechat"``. Also design-time.
    target: str | None = None

    def effective_path(self, flow_default: str | None = None) -> str | None:
        return self.path or flow_default

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name or self.type,
            "position": self.position.to_dict(),
            "params": self.params,
            "retry": self.retry.to_dict(),
            "timeout": self.timeout,
            "on_error": self.on_error,
            "outputs": self.outputs,
            "disabled": self.disabled,
            "path": self.path,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Node":
        if not isinstance(raw, dict):
            raise FlowError("node must be an object")
        node_id = str(raw.get("id") or "").strip()
        if not NODE_ID_RE.match(node_id):
            raise FlowError(f"invalid node id: {node_id!r}")
        node_type = str(raw.get("type") or "").strip()
        if not node_type:
            raise FlowError(f"node {node_id!r} has no type")
        on_error = str(raw.get("on_error") or "fail")
        if on_error not in ON_ERROR_CHOICES:
            raise FlowError(f"node {node_id!r} has invalid on_error: {on_error!r}")
        outputs = raw.get("outputs")
        path = raw.get("path")
        if path is not None and str(path) not in PATH_CHOICES:
            raise FlowError(
                f"node {node_id!r} has invalid path {path!r}; expected one of {list(PATH_CHOICES)}"
            )
        return cls(
            id=node_id,
            type=node_type,
            name=str(raw.get("name") or node_type),
            position=Position.from_dict(raw.get("position")),
            params=raw.get("params") if isinstance(raw.get("params"), dict) else {},
            retry=Retry.from_dict(raw.get("retry")),
            timeout=_as_optional_float(raw.get("timeout")),
            on_error=on_error,
            outputs=[str(p) for p in outputs] if isinstance(outputs, list) else [],
            disabled=bool(raw.get("disabled", False)),
            path=str(path) if path else None,
            target=str(raw["target"]) if raw.get("target") else None,
        )


@dataclass
class Edge:
    """A transition. ``source_port`` selects the branch; ``condition`` guards it.

    ``condition`` is a small expression evaluated against the run scope, of the
    form ``name op literal`` (e.g. ``result.count > 0``). ``None`` means
    unconditional. The first outgoing edge whose condition holds wins; edges with
    no matching port are ignored, so a node with no satisfied edge simply ends
    that branch.
    """

    id: str
    source: str
    target: str
    source_port: str = "ok"
    condition: str | None = None
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_port": self.source_port,
            "target": self.target,
            "condition": self.condition,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Edge":
        if not isinstance(raw, dict):
            raise FlowError("edge must be an object")
        edge_id = str(raw.get("id") or "").strip()
        if not edge_id:
            raise FlowError("edge has no id")
        source = str(raw.get("source") or "")
        target = str(raw.get("target") or "")
        if not source or not target:
            raise FlowError(f"edge {edge_id!r} must have source and target")
        condition = raw.get("condition")
        return cls(
            id=edge_id,
            source=source,
            target=target,
            source_port=str(raw.get("source_port") or "ok"),
            condition=str(condition) if condition else None,
            label=str(raw.get("label") or ""),
        )


@dataclass
class Flow:
    id: str
    name: str
    graph: dict[str, Any]
    description: str = ""
    version: int = 1
    is_active: bool = False
    created_at: str = ""
    updated_at: str = ""

    @property
    def nodes(self) -> list[Node]:
        return [Node.from_dict(n) for n in self.graph.get("nodes", [])]

    @property
    def edges(self) -> list[Edge]:
        return [Edge.from_dict(e) for e in self.graph.get("edges", [])]

    @property
    def entry(self) -> str:
        return str(self.graph.get("entry") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "graph": self.graph,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Flow":
        import json

        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            version=row["version"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            graph=json.loads(row["graph_json"]),
        )


class ValidationIssue:
    """One problem found by :func:`validate_flow`."""

    __slots__ = ("severity", "code", "message", "node_id", "edge_id")

    def __init__(
        self,
        severity: str,
        code: str,
        message: str,
        node_id: str | None = None,
        edge_id: str | None = None,
    ) -> None:
        self.severity = severity  # "error" | "warning"
        self.code = code
        self.message = message
        self.node_id = node_id
        self.edge_id = edge_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.severity} {self.code} {self.message}>"


def validate_flow(graph: dict[str, Any], known_types: Iterable[str] | None = None) -> list[ValidationIssue]:
    """Structurally check a graph document.

    ``known_types`` is the set of registered node types. When supplied, unknown
    types are reported — the canvas needs that to flag a node whose type was
    removed from the registry after it was placed.

    Returns a list of issues. An empty list means the graph is structurally sound;
    it does not mean the graph is semantically correct.
    """
    issues: list[ValidationIssue] = []
    if not isinstance(graph, dict):
        return [ValidationIssue("error", "not_an_object", "流程文档必须是对象")]

    version = graph.get("version")
    if version != SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                "error",
                "version_mismatch",
                f"不支持的流程版本 {version!r}，当前引擎为 {SCHEMA_VERSION}",
            )
        )

    raw_nodes = graph.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return issues + [ValidationIssue("error", "no_nodes", "流程至少需要一个节点")]

    raw_edges = graph.get("edges")
    if raw_edges is not None and not isinstance(raw_edges, list):
        issues.append(ValidationIssue("error", "edges_not_a_list", "edges 必须是数组"))
        raw_edges = []
    raw_edges = raw_edges or []

    known = set(known_types) if known_types is not None else None
    parsed: dict[str, Node] = {}
    for index, raw in enumerate(raw_nodes):
        try:
            node = Node.from_dict(raw)
        except FlowError as exc:
            issues.append(ValidationIssue("error", "bad_node", f"节点[{index}]: {exc}"))
            continue
        if node.id in parsed:
            issues.append(
                ValidationIssue("error", "duplicate_node_id", f"节点 id 重复: {node.id}", node_id=node.id)
            )
            continue
        if known is not None and node.type not in known:
            issues.append(
                ValidationIssue(
                    "error",
                    "unknown_node_type",
                    f"未知节点类型 {node.type!r}（注册表中不存在）",
                    node_id=node.id,
                )
            )
        parsed[node.id] = node

    entry = str(graph.get("entry") or "")
    if not entry:
        issues.append(ValidationIssue("error", "no_entry", "流程未设置入口节点 entry"))
    elif entry not in parsed:
        issues.append(
            ValidationIssue("error", "entry_not_found", f"入口节点 {entry!r} 不存在", node_id=entry)
        )

    targets: dict[str, int] = {}
    seen_edge_ids: set[str] = set()
    valid_edges: dict[str, Edge] = {}
    for index, raw in enumerate(raw_edges):
        try:
            edge = Edge.from_dict(raw)
        except FlowError as exc:
            issues.append(ValidationIssue("error", "bad_edge", f"连线[{index}]: {exc}"))
            continue
        if edge.id in seen_edge_ids:
            issues.append(
                ValidationIssue("error", "duplicate_edge_id", f"连线 id 重复: {edge.id}", edge_id=edge.id)
            )
            continue
        seen_edge_ids.add(edge.id)
        valid_edges[edge.id] = edge
        for role, node_id in (("source", edge.source), ("target", edge.target)):
            if node_id not in parsed:
                issues.append(
                    ValidationIssue(
                        "error",
                        "dangling_edge",
                        f"连线 {edge.id} 的 {role} 节点 {node_id!r} 不存在",
                        node_id=node_id,
                        edge_id=edge.id,
                    )
                )
        if edge.source in parsed and edge.target == edge.source:
            issues.append(
                ValidationIssue("error", "self_loop", f"连线 {edge.id} 指向自身", edge_id=edge.id)
            )
        targets[edge.target] = targets.get(edge.target, 0) + 1
        if edge.condition:
            _validate_condition(edge, issues)

    reachable = _reachable(entry, parsed, valid_edges)
    for node_id in parsed:
        if node_id not in reachable:
            issues.append(
                ValidationIssue("warning", "unreachable_node", f"节点 {node_id} 从入口不可达", node_id=node_id)
            )

    for node_id in parsed:
        if node_id != entry and targets.get(node_id, 0) == 0 and not parsed[node_id].disabled:
            issues.append(
                ValidationIssue("warning", "no_incoming_edge", f"节点 {node_id} 没有任何入边", node_id=node_id)
            )

    issues.extend(_validate_paths(parsed, graph.get("default_path")))
    issues.extend(_validate_branching(parsed, valid_edges))
    _validate_variable_ambiguity(graph, list(parsed.values()), issues)

    return issues


#: Node types that move the mouse, type, or read the screen. Two of these running
#: at once do not interleave politely — they fight over one cursor and one
#: screen, and the loser reports a click that landed on whatever the winner
#: drew. So a threaded branch containing one is refused at validation time
#: rather than discovered as a mystery mis-click.
UI_BOUND_TYPES = frozenset({
    "click", "double_click", "right_click", "hover", "drag", "scroll",
    "type_keys", "send_keys", "vlm_act", "capture", "perceive", "find_image",
    "find_color", "assert_pixel", "wait_for", "activate", "activate_app",
    "window_rect", "set_window_rect", "minimize_window", "maximize_window",
    "close_window", "pick", "vlm_locate",
})

#: The branch ports a ``parallel`` node exposes. Fixed count on purpose: the
#: canvas renders ports from the node spec, so a variable number would need a
#: second mechanism to draw. Unused ports simply have no edge.
BRANCH_PORTS = ("b1", "b2", "b3", "b4")


def _validate_branching(nodes: dict[str, Node], edges: dict[str, Edge]) -> list[ValidationIssue]:
    """Check fan-out and fan-in.

    Two rules earn their keep here, and both are about a branch that *looks*
    connected but cannot be:

    1. **Two unconditional edges out of the same port.** The executor is a
       cursor that takes the first matching edge; the second is dead. That used
       to validate clean and report success, which is how someone wiring
       parallel branches by hand ends up with one of the two silently missing.
    2. **Branches that do not meet at one join.** A barrier needs a single
       point to wait at. Branches converging on different joins, or on none,
       cannot be joined, and saying so at save time beats a run that hangs or
       half-fires.
    """
    issues: list[ValidationIssue] = []

    by_source: dict[tuple[str, str], list[Edge]] = {}
    for edge in sorted(edges.values(), key=lambda e: e.id):
        if edge.condition:
            continue
        by_source.setdefault((edge.source, edge.source_port), []).append(edge)
    for (source, port), group in sorted(by_source.items()):
        if len(group) < 2:
            continue
        targets = "、".join(e.target for e in group)
        hint = "改用 parallel 节点的分支出边" if port == "ok" else "给其中几条加条件"
        issues.append(ValidationIssue(
            "error",
            "duplicate_unconditional_edge",
            f"节点 {source} 的 {port} 端口有多条无条件出边（{targets}），"
            f"执行器只会走第一条，其余永不执行；{hint}",
            node_id=source,
            edge_id=group[1].id,
        ))

    for node in nodes.values():
        if node.type == "parallel":
            issues.extend(_validate_parallel_node(node, nodes, edges))
        elif node.type == "join":
            issues.extend(_validate_join_node(node, nodes, edges))
    return issues


def _parallel_entries(node: Node, edges: dict[str, Edge]) -> list[str]:
    return [e.target for e in sorted(edges.values(), key=lambda e: e.id)
            if e.source == node.id and e.source_port in BRANCH_PORTS]


def _reaches_join(start: str, nodes: dict[str, Node], edges: dict[str, Edge]) -> set[str]:
    """Joins that can end a walk entering at ``start``.

    A nested ``parallel`` resolves to *its own* barrier and the walk carries on
    from there, so the inner fork's join must not be reported as the outer
    branch's join — otherwise every nested fan-out looks like branches that
    converge on two different barriers. Recursion with a memo, and a visiting
    set so a cycle terminates instead of recursing until Python gives up.
    """
    memo: dict[str, set[str] | None] = {}
    visiting: set[str] = set()
    return _barriers(start, nodes, edges, memo, visiting) or set()


def _barriers(
    start: str,
    nodes: dict[str, Node],
    edges: dict[str, Edge],
    memo: dict[str, set[str] | None],
    visiting: set[str],
) -> set[str] | None:
    if start in memo:
        return memo[start]
    if start in visiting:
        # A cycle cannot terminate on a join, or it would never have exited.
        return None
    node = nodes.get(start)
    if node is None:
        return None
    if node.type == "join":
        memo[start] = {start}
        return memo[start]
    visiting.add(start)
    try:
        if node.type == "parallel":
            result = _barriers_after_fork(node, nodes, edges, memo, visiting)
        else:
            result = None
            for edge in edges.values():
                if edge.source != start:
                    continue
                onward = _barriers(edge.target, nodes, edges, memo, visiting)
                if onward:
                    result = set(onward) if result is None else result | onward
    finally:
        visiting.discard(start)
    memo[start] = result
    return result


def _barriers_after_fork(
    node: Node,
    nodes: dict[str, Node],
    edges: dict[str, Edge],
    memo: dict[str, set[str] | None],
    visiting: set[str],
) -> set[str] | None:
    """Where a walk that entered a fork carries on once the fork has joined."""
    entries = _parallel_entries(node, edges)
    if len(entries) < 2:
        return None
    inner: set[str] = set()
    for entry in entries:
        found = _barriers(entry, nodes, edges, memo, visiting)
        if not found:
            return None
        inner |= found
    if len(inner) != 1:
        # The fork's own branches disagree; whoever validates that reports it.
        # Returning every candidate here would double-report the same defect.
        return None
    barrier = next(iter(inner))
    onward: set[str] = set()
    for edge in edges.values():
        if edge.source != barrier:
            continue
        found = _barriers(edge.target, nodes, edges, memo, visiting)
        if found:
            onward |= found
    return onward or None


def _validate_parallel_node(node: Node, nodes: dict[str, Node], edges: dict[str, Edge]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    entries = _parallel_entries(node, edges)
    if len(entries) < 2:
        issues.append(ValidationIssue(
            "error", "parallel_needs_two",
            f"parallel 节点 {node.id} 只连了 {len(entries)} 条分支，分支少于 2 条没有意义",
            node_id=node.id,
        ))
        return issues

    threaded = str(node.params.get("mode") or "sequential") == "parallel"
    for entry in entries:
        joins = _reaches_join(entry, nodes, edges)
        if not joins:
            issues.append(ValidationIssue(
                "error", "branch_no_join",
                f"parallel 节点 {node.id} 的分支 {entry} 走不到任何 join 节点，"
                f"汇合点缺失，执行时会一直走到分支尽头然后丢弃结果",
                node_id=node.id,
            ))
            continue
        if len(joins) > 1:
            issues.append(ValidationIssue(
                "error", "branch_ambiguous_join",
                f"parallel 节点 {node.id} 的分支 {entry} 可以到达多个 join"
                f"（{'、'.join(sorted(joins))}），一个汇合点只能等一处",
                node_id=node.id,
            ))
            continue
        if threaded:
            offenders = _ui_bound_in_branch(entry, nodes, edges)
            if offenders:
                issues.append(ValidationIssue(
                    "error", "threaded_ui_branch",
                    f"parallel 节点 {node.id} 设为并行执行，但分支 {entry} 里含操作屏幕的节点"
                    f"（{'、'.join(offenders)}）。两个线程会争抢同一个鼠标和同一块屏幕，"
                    f"后执行的那个会点到先执行的那个留下的东西上；"
                    f"请改成顺序执行，或把这些节点移出并行分支",
                    node_id=node.id,
                ))
    if entries and not issues:
        # A set of sets is unhashable, so compare the sorted tuples.
        signatures = {tuple(sorted(_reaches_join(e, nodes, edges))) for e in entries}
        if len(signatures) > 1:
            only = "；".join(
                f"{e} → {'、'.join(sorted(_reaches_join(e, nodes, edges)))}" for e in entries
            )
            issues.append(ValidationIssue(
                "error", "branches_diverge",
                f"parallel 节点 {node.id} 的分支没有汇聚到同一个 join 节点（{only}）",
                node_id=node.id,
            ))
    return issues


def _validate_join_node(node: Node, nodes: dict[str, Node], edges: dict[str, Edge]) -> list[ValidationIssue]:
    """A join reached without a fork is a node that does nothing.

    It would pass through as an ordinary node, so the flow would run and report
    success while the author believed they had written a barrier. That is worse
    than refusing to save.
    """
    incoming = [e for e in edges.values() if e.target == node.id]
    if not incoming:
        return [ValidationIssue(
            "error", "join_no_incoming",
            f"join 节点 {node.id} 没有任何入边", node_id=node.id,
        )]
    for edge in incoming:
        source = nodes.get(edge.source)
        if source is None or source.type != "parallel":
            continue
        # A direct parallel -> join edge means the branch never ran: the fork
        # went straight to the barrier.
        if edge.source_port in BRANCH_PORTS:
            issues = [ValidationIssue(
                "error", "join_direct_from_parallel",
                f"join 节点 {node.id} 直接连在 parallel 的分支出边上，"
                f"这条分支没有任何节点可执行",
                node_id=node.id, edge_id=edge.id,
            )]
            return issues
    if not _reaches_any_parallel(node.id, nodes, edges):
        return [ValidationIssue(
            "error", "join_without_parallel",
            f"join 节点 {node.id} 只能由 parallel 的分支进入；"
            f"没有任何 parallel 分支能走到它，它会当作普通节点空转",
            node_id=node.id,
        )]
    return []


def _reaches_any_parallel(start: str, nodes: dict[str, Node], edges: dict[str, Edge]) -> bool:
    """Whether ``start`` is downstream of a ``parallel`` node."""
    reverse: dict[str, list[str]] = {}
    for edge in edges.values():
        reverse.setdefault(edge.target, []).append(edge.source)
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for prev in reverse.get(current, ()):
            if prev in seen:
                continue
            node = nodes.get(prev)
            if node is not None and node.type == "parallel":
                return True
            seen.add(prev)
            stack.append(prev)
    return False


def _ui_bound_in_branch(start: str, nodes: dict[str, Node], edges: dict[str, Edge]) -> list[str]:
    """Node types in a branch that touch the screen, up to the join."""
    outgoing: dict[str, list[str]] = {}
    for edge in edges.values():
        outgoing.setdefault(edge.source, []).append(edge.target)
    found: list[str] = []
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        node = nodes.get(current)
        if node is not None and node.type == "join":
            continue
        if node is not None and node.type in UI_BOUND_TYPES and node.type not in found:
            found.append(node.type)
        for nxt in outgoing.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return sorted(found)


def _validate_paths(nodes: dict[str, Node], default_path: Any) -> list[ValidationIssue]:
    """Check the dual-path declarations.

    The point of these rules is that the path a flow will take must be knowable
    from the graph alone. A path-aware node with no declared path and no flow
    default falls through to the target's own configuration at run time, which
    means the design does not fully determine behaviour — worth a warning,
    because that is exactly the situation where a flow that "worked yesterday"
    starts costing a model call per click after someone edits a target.
    """
    issues: list[ValidationIssue] = []
    if default_path is not None and str(default_path) not in PATH_CHOICES:
        issues.append(
            ValidationIssue("error", "bad_default_path", f"流程默认路径 {default_path!r} 非法")
        )
    resolved_default = str(default_path) if default_path and str(default_path) in PATH_CHOICES else None

    for node in nodes.values():
        if node.path and node.type not in PATH_AWARE_TYPES:
            issues.append(
                ValidationIssue(
                    "warning",
                    "path_on_unaware_type",
                    f"节点 {node.id} 类型 {node.type} 不支持路径选择，path 会被忽略",
                    node_id=node.id,
                )
            )
            continue
        if node.type not in PATH_AWARE_TYPES:
            continue
        if not node.path and not resolved_default:
            issues.append(
                ValidationIssue(
                    "warning",
                    "no_path_declared",
                    f"节点 {node.id} 未选定路径，将回落到目标应用的默认配置（运行期才决定）",
                    node_id=node.id,
                )
            )
        if node.path == "auto":
            issues.append(
                ValidationIssue(
                    "warning",
                    "auto_path_costs_a_model_call",
                    f"节点 {node.id} 使用 auto：元素缺失时会调用视觉模型，耗时与成本不可控",
                    node_id=node.id,
                )
            )
    return issues


def _safe_edge(raw: Any) -> bool:
    try:
        Edge.from_dict(raw)
        return True
    except FlowError:
        return False


def _reachable(entry: str, nodes: dict[str, Node], edges: dict[str, Edge]) -> set[str]:
    """Nodes reachable from ``entry``, following edges by source."""
    outgoing: dict[str, list[str]] = {}
    for edge in edges.values():
        outgoing.setdefault(edge.source, []).append(edge.target)
    seen: set[str] = set()
    stack = [entry]
    while stack:
        current = stack.pop()
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        stack.extend(outgoing.get(current, ()))
    return seen


#: ``operand OP literal`` where OP is one of these.
#:
#: Retained for backwards compatibility only. It must not be used to decide
#: whether a condition is well formed: its trailing ``(.+?)`` swallows whatever
#: follows the first comparison, so ``count > 5 and enabled`` matched cleanly
#: while comparing ``count`` against the literal string ``"5 and enabled"``.
#: Validation now parses the condition instead.
CONDITION_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.\[\]]*)\s*(==|!=|>=|<=|>|<|~)\s*(.+?)\s*$")

#: ``~`` is the flow-level "contains" operator and is not valid Python; the
#: marker is syntactically valid, so validation only has to substitute it.
_TILDE_MARKER_RE = re.compile(r"(?<=[\s\)])\s*~\s*")


def is_valid_expression(text: str) -> bool:
    """Whether ``text`` parses as an expression the evaluator can run.

    Deliberately depends on :mod:`ast` rather than :mod:`src.flow.expr`: the
    evaluator imports ``FlowError`` from this module, so importing it back here
    would close a cycle. Parsing is the whole check — the evaluator rejects
    anything unsupported at run time with a message naming the construct.
    """
    import ast

    for candidate in (text, _TILDE_MARKER_RE.sub(" @ ", text)):
        try:
            ast.parse(candidate, mode="eval")
            return True
        except SyntaxError:
            continue
    return False


def _validate_condition(edge: Edge, issues: list[ValidationIssue]) -> None:
    if not is_valid_expression(edge.condition or ""):
        issues.append(
            ValidationIssue(
                "error",
                "bad_condition",
                f"连线 {edge.id} 的条件 {edge.condition!r} 无法解析，应为可求值的表达式",
                edge_id=edge.id,
            )
        )


#: ``{{ ... }}`` occurrences in any text a node parameter or edge carries.
_INTERPOLATION_SCAN_RE = re.compile(r"\{\{(.*?)\}\}", re.S)

#: Names a flow may bind at runtime that never appear in a node's declared
#: ``outputs``: the start node's inputs, ``set_var`` targets and loop variables.
#: Excluded from the ambiguity check because they are *supposed* to share the
#: flat namespace — that is what ``set_var`` and ``foreach`` are for.
_DYNAMIC_BINDERS = frozenset({"set_var", "foreach", "call_flow", "start"})


def _referenced_names(text: Any) -> set[str]:
    """Bare variable heads referenced by ``{{...}}`` in ``text``.

    A dotted reference is ignored: ``{{n_perceive.messages}}`` names one node
    explicitly and is exactly the disambiguated form this check is steering
    authors toward.
    """
    if not isinstance(text, str) or "{{" not in text:
        return set()
    names: set[str] = set()
    for body in _INTERPOLATION_SCAN_RE.findall(text):
        expr = body.strip()
        if not expr or "." in expr:
            continue
        # Only a plain head counts; `{{a + b}}` is an expression, not a
        # reference to a variable literally named "a + b".
        if expr.isidentifier():
            names.add(expr)
    return names


def _validate_variable_ambiguity(
    graph: dict[str, Any], nodes: list[Node], issues: list[ValidationIssue]
) -> None:
    """Warn when a bare ``{{name}}`` could mean more than one node's output.

    Node outputs are bound both flat and node-scoped. The flat binding is
    last-writer-wins, so a flow whose ``perceive`` and ``get_unreplied`` both
    emit ``messages`` and which reads ``{{messages}}`` gets whichever ran last
    — correct until an edge is re-ordered, then silently wrong.

    A warning, not an error: a flow with one producer is legitimate and should
    not be forced to write ``{{n_perceive.messages}}`` everywhere. The message
    names every candidate so the fix is obvious.
    """
    producers: dict[str, list[str]] = {}
    for node in nodes:
        if node.type in _DYNAMIC_BINDERS:
            continue
        for out in node.outputs or []:
            producers.setdefault(out, []).append(node.id)

    ambiguous = {name: ids for name, ids in producers.items() if len(ids) > 1}

    used: dict[str, list[str]] = {}
    for node in nodes:
        for value in (node.params or {}).values():
            for name in _referenced_names(value):
                used.setdefault(name, []).append(node.id)
    for edge in graph.get("edges") or []:
        for name in _referenced_names(edge.get("condition")):
            used.setdefault(name, []).append(edge.id)

    for name, consumers in used.items():
        candidates = ambiguous.get(name)
        if not candidates:
            continue
        where = "、".join(sorted(set(consumers)))
        issues.append(
            ValidationIssue(
                "warning",
                "ambiguous_variable",
                f"变量 {{{{{name}}}}} 由多个节点产出（{'、'.join(candidates)}），"
                f"裸引用取最后执行的那个；建议写成 {{{{{candidates[0]}.{name}}}}} 明确来源。"
                f"引用位置：{where}",
            )
        )


def _as_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _as_optional_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
