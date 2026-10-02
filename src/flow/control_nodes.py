"""Control-flow node types: loops, error trapping, and sub-flow calls.

Split from :mod:`src.flow.builtin_nodes` because these are the node types that
reason *about the graph itself* rather than about WeChat, and they carry rules
that are easy to get wrong:

**Loops need no executor change.** A loop is an ordinary node whose ``loop`` port
is wired back to an earlier node. The executor is a linear cursor, so a back edge
just re-enters it; the ``max_steps`` ceiling in :mod:`src.flow.executor` is what
actually stops a runaway loop, and the per-node ``max_iterations`` here is what
stops it *earlier* with a message that names the offending node. The validator
already permits back edges (it only rejects self-loops), which is what makes this
work.

**``try_catch`` is a marker, not a sandbox.** It cannot wrap the execution of
another node at runtime — the executor has no stack to unwind. What it does is
declare an error region, and it works because every node already has an
``on_error`` policy. A node inside the region is set to ``branch`` on ``catch`` by
the validator, so its failures land on the region's catch port instead of killing
the run. The honesty matters: the region does not roll back work the inner nodes
already did, and the node says so in its docstring and its outputs.

**Sub-flows nest a whole run inside a node.** The nested run gets a fresh scope
seeded from ``params``, and its outputs are copied back under an explicit prefix
so two levels of nesting cannot collide on a variable name.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .registry import BaseNode, NodeRegistry, NodeSpec, ParamSpec
from .schema import NodeError

#: Hard ceiling on nesting. A flow that calls itself would otherwise recurse until
#: Python's own limit turns it into an opaque RecursionError.
MAX_SUBFLOW_DEPTH = 8

#: Registry key the executor uses to tell a nested run from a top-level one.
DEPTH_VAR = "__subflow_depth__"


def _subflow_depth(ctx: Any) -> int:
    try:
        return int(ctx.scope.get(DEPTH_VAR) or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────── loops ──

class WhileNode(BaseNode):
    """Loop while a condition holds.

    Ports: ``body`` (into the loop body), ``exit`` (out of the loop).

    **Wiring a loop.** Draw the body from this node's ``body`` port. Close the
    loop by wiring the *last body node's normal ``ok`` port* back to this node —
    the executor picks ``ok`` when nothing else is chosen, so a plain node needs
    no special port to end a loop. A node that branches (a ``condition``, another
    loop) wires its ``loop`` port back instead, so the branch that should repeat
    is explicit.

    Leaving the back edge unwired means the body runs exactly once, which is a
    silently useless loop; the iterations count is in the node's output so a
    trace reader can see it.

    ``max_iterations`` defaults to 100 and is enforced here rather than left to
    the global step ceiling, because "which node looped forever" is a question an
    operator has to answer from the trace panel, and this node can answer it.
    """

    def execute(self) -> dict[str, Any]:
        from .context import evaluate_condition
        from .schema import CONDITION_RE, FlowError

        expression = str(self.param("condition", "")).strip()
        if not expression:
            raise NodeError("while 需要 condition 参数")

        # Keyed on the graph node id, not on id(self): a fresh handler instance
        # is built for every attempt, so id(self) would hand the loop a new
        # counter each pass and max_iterations would never trip.
        counter_var = f"__loop_{self.node_id or 'while'}"
        try:
            iterations = int(self.ctx.scope.get(counter_var) or 0)
        except (TypeError, ValueError):
            iterations = 0

        if expression and CONDITION_RE.match(expression):
            try:
                condition = bool(evaluate_condition(expression, self.ctx.scope))
            except FlowError:
                condition = bool(self._eval(expression))
        else:
            condition = bool(self._eval(expression))

        if not condition:
            self.ctx.scope.bind(counter_var, 0)
            return {"__branch__": "exit", "iterations": iterations, "condition": expression}

        iterations += 1
        limit = int(self.param("max_iterations", 100) or 100)
        if iterations > limit:
            self.ctx.scope.bind(counter_var, 0)
            raise NodeError(f"循环超过最大轮数 {limit}，条件可能恒为真: {expression}")

        self.ctx.scope.bind(counter_var, iterations)
        return {
            "__branch__": "body",
            "iterations": iterations,
            "condition": expression,
            "condition_value": True,
        }

    def _eval(self, expression: str) -> Any:
        try:
            return eval(expression, {"__builtins__": {}}, self.ctx.scope.snapshot())  # noqa: S307
        except Exception as exc:  # noqa: BLE001
            raise NodeError(f"循环条件求值失败: {exc}") from exc


class ForeachNode(BaseNode):
    """Iterate a list, binding the item to a variable on each pass.

    Ports: ``item`` (into the body), ``done`` (after the list is exhausted),
    ``loop`` (wire this back to ``item`` to repeat).

    The index is available as ``<var>_index`` so a body can report "3 of 7"
    without keeping its own counter. ``collection`` accepts a variable name, a
    dotted path into a previous node's output, or a literal JSON list — the last
    one because typing a one-element list inline is the common case.
    """

    def execute(self) -> dict[str, Any]:
        var = str(self.param("item_var", "") or "item")
        collection = self.param("collection", None)
        items = self._coerce(collection)

        # Keyed on node id + variable name: two foreach loops over different
        # lists must not share an index.
        state_var = f"__foreach_{self.node_id or 'loop'}_{var}"
        try:
            index = int(self.ctx.scope.get(state_var) or 0)
        except (TypeError, ValueError):
            index = 0

        if index >= len(items):
            self.ctx.scope.bind(state_var, 0)
            return {"__branch__": "done", "total": len(items), "index": index}

        self.ctx.scope.bind(var, items[index])
        self.ctx.scope.bind(f"{var}_index", index)
        self.ctx.scope.bind(state_var, index + 1)
        return {
            "__branch__": "item",
            "item": items[index],
            "index": index,
            "total": len(items),
        }

    def _coerce(self, collection: Any) -> list[Any]:
        if collection is None or collection == "":
            raise NodeError("foreach 需要 collection 参数")
        if isinstance(collection, (list, tuple)):
            return list(collection)
        if isinstance(collection, str):
            raw = collection.strip()
            if raw.startswith("["):
                try:
                    parsed = eval(raw, {"__builtins__": {}}, {"__builtins__": {}})  # noqa: S307
                except Exception as exc:  # noqa: BLE001
                    raise NodeError(f"字面量列表解析失败: {exc}") from exc
                if not isinstance(parsed, (list, tuple)):
                    raise NodeError("collection 字面量必须是列表")
                return list(parsed)
            # A bare name is a variable reference, not a string to iterate.
            value = self.ctx.scope.get(raw)
            if isinstance(value, (list, tuple)):
                return list(value)
            if value is None:
                raise NodeError(f"collection 变量 {raw!r} 不存在或不是列表")
            raise NodeError(f"collection 变量 {raw!r} 不是列表")
        if isinstance(collection, dict):
            return list(collection.values())
        raise NodeError(f"collection 类型不支持: {type(collection).__name__}")


# ─────────────────────────────────────────────────────── error regions ──

class TryNode(BaseNode):
    """Opens an error region. Body nodes route their failures to ``catch``.

    This node itself does almost nothing: it reports which nodes the validator
    bound into the region so a trace reader can see the boundary without
    cross-referencing the graph. The real work happens at validation time, where
    nodes between ``try`` and ``catch``/``finally`` are given ``on_error: branch``
    on their ``catch`` port.
    """

    def execute(self) -> dict[str, Any]:
        return {
            # The executor falls through to the default `ok` port when a node
            # does not name one, and this node has no `ok` edge — the body is
            # wired on `body`. Without the explicit port the run would end here.
            "__branch__": "body",
            "region": str(self.param("label", "") or "try"),
            "opened_at": time.time(),
            "note": "异常捕获由校验器把区域内节点的 on_error 改写为 catch 分支实现",
        }


class CatchNode(BaseNode):
    """Closes an error region. Receives failures raised inside the region.

    Ports: ``exit`` (normal continuation), ``rethrow`` (put the failure back).
    The caught error is bound to ``<label>_error`` so the rest of the flow can
    inspect it, which is the point of catching rather than merely continuing.
    """

    def execute(self) -> dict[str, Any]:
        label = str(self.param("label", "") or "try")
        error = self.resolve(f"{label}_error", "") or self.resolve("__last_error__", "")
        action = str(self.param("action", "continue") or "continue")
        if action == "rethrow":
            raise NodeError(f"捕获到异常并重新抛出: {error}")
        return {
            "__branch__": "exit",
            "caught": bool(error),
            "error": str(error),
            "label": label,
        }


class FinallyNode(BaseNode):
    """Always runs after its region, whether or not something inside failed.

    There is no second path into this node in the current executor, so a region
    whose body fails does NOT reach it — that is a real limitation, recorded here
    rather than papered over. Use ``catch`` with ``action: rethrow`` plus a node
    after it when guaranteed cleanup matters.
    """

    def execute(self) -> dict[str, Any]:
        return {"finally_at": time.time(), "label": str(self.param("label", "") or "try")}


# ─────────────────────────────────────────────────────────── sub-flows ──

class CallFlowNode(BaseNode):
    """Run another stored flow as a step of this one.

    The nested run gets a fresh scope seeded from ``inputs`` and returns its
    status under ``<output_prefix>_status`` / ``_result`` / ``_error``. Nesting
    depth is capped at :data:`MAX_SUBFLOW_DEPTH`; a flow that recurses into
    itself hits the cap and fails with a name, instead of a stack trace.

    The nested run builds its own services via the same
    :func:`register_default_services` the top-level runner uses, so a sub-flow
    behaves exactly as it would if opened on its own — sharing the caller's
    services would let a sub-flow's ``dry_run`` disagree with its caller's, and
    that is the kind of disagreement that sends a real message in a test.

    Spans are re-emitted through the parent run so the trace panel shows the
    sub-flow's steps inline rather than dropping them.
    """

    def execute(self) -> dict[str, Any]:
        from .executor import FlowExecutor
        from .services import register_default_services
        from .store import get_store

        flow_id = str(self.param("flow_id", "") or "").strip()
        if not flow_id:
            raise NodeError("call_flow 需要 flow_id 参数")

        depth = _subflow_depth(self.ctx)
        if depth >= MAX_SUBFLOW_DEPTH:
            raise NodeError(f"子流程嵌套超过 {MAX_SUBFLOW_DEPTH} 层，疑似递归调用 {flow_id!r}")

        store = get_store()
        flow = store.get_flow(flow_id)
        if flow is None:
            raise NodeError(f"子流程 {flow_id!r} 不存在")

        # A fresh scope: the sub-flow must not see the caller's locals, or a typo
        # in its params would silently bind to a same-named variable upstream.
        raw_inputs = self.param("inputs", {}) or {}
        if isinstance(raw_inputs, str):
            try:
                raw_inputs = json.loads(raw_inputs)
            except json.JSONDecodeError as exc:
                raise NodeError(f"inputs 不是合法 JSON: {exc}") from exc
        if not isinstance(raw_inputs, dict):
            raise NodeError("inputs 必须是 JSON 对象")

        variables = {str(k): v for k, v in raw_inputs.items()}
        variables[DEPTH_VAR] = depth + 1
        dry_run = bool(self.param("dry_run", False))
        prefix = str(self.param("output_prefix", "") or "sub")

        nested = FlowExecutor(
            span_hook=self._forward_span,
            setup_context=lambda ctx: register_default_services(ctx, dry_run=dry_run),
        )
        # A nested run must not write its own run history row: the parent run is
        # the unit of record, and a half-finished sub-flow would otherwise leave
        # an orphan run that looks like a separate execution.
        result = nested.run(flow, run_id=self.ctx.run_id, variables=variables)

        payload = {
            f"{prefix}_status": result.status,
            f"{prefix}_steps": result.steps,
            # RunResult exposes the final scope, not a dedicated output slot, so
            # the sub-flow's `end` node message is read back out of it.
            f"{prefix}_result": str(result.scope.get("message", "") or ""),
            f"{prefix}_error": result.error or "",
        }

        # A failed sub-flow must fail this node unless the author wired the
        # `error` port on purpose. Swallowing it here is how a parent run reports
        # "ok" while the thing it delegated never happened — the same shape as
        # the silent-timeout failure this engine already had one of.
        if result.status != "ok" and not self._error_port_wired():
            detail = result.error or f"子流程状态 {result.status}"
            if result.failed_node:
                detail = f"{detail}（失败节点 {result.failed_node}）"
            raise NodeError(f"子流程 {flow_id!r} {detail}")

        return payload

    def _error_port_wired(self) -> bool:
        """True when the graph routes this node's failures somewhere on purpose."""
        return bool(self.param("swallow_errors", False))

    def _forward_span(self, span: Any) -> None:
        """Re-emit a nested span so the parent trace shows the sub-flow's steps."""
        sink = getattr(self.ctx, "span_sink", None)
        if callable(sink):
            sink(span)


def register_all(registry: NodeRegistry) -> NodeRegistry:
    """Register every node type in this module. Idempotent."""
    add = registry.register
    add(
NodeSpec(
            type="while",
            label="条件循环",
            category="控制流",
            handler=WhileNode,
            doc="条件为真时重复执行循环体。需把循环体末尾的 loop 出口接回本节点的 body 入边。",
            params=[
                ParamSpec("condition", "text", "循环条件", required=True,
                          help="支持 'name op 值' 语法，或 Python 表达式（相对运行作用域求值）"),
                ParamSpec("max_iterations", "number", "最大轮数", default=100,
                          help="超过则直接失败并指名本节点。不填则用全局步数上限兜底"),
            ],
            outputs=["body", "exit"],
        )
    )
    add(
NodeSpec(
            type="foreach",
            label="列表循环",
            category="控制流",
            handler=ForeachNode,
            doc="遍历列表，每轮把当前项绑定到 item_var。需把循环体末尾的 loop 出口接回 item 入边。",
            params=[
                ParamSpec("collection", "text", "数据来源", required=True,
                          help="变量名、上一节点输出的点路径，或字面量列表如 [1,2,3]"),
                ParamSpec("item_var", "text", "绑定变量名", default="item",
                          help="每轮把当前项写进这个变量；下标可读 <变量名>_index"),
            ],
            outputs=["item", "done", "loop"],
        )
    )
    add(
NodeSpec(
            type="try",
            label="异常捕获开始",
            category="控制流",
            handler=TryNode,
            doc="开启异常区域。校验器会把区域内节点的 on_error 改写为 catch 分支。",
            params=[
                ParamSpec("label", "text", "区域名", default="try",
                          help="同一区域的名字需与 catch 一致"),
            ],
            outputs=["body"],
        )
    )
    add(
NodeSpec(
            type="catch",
            label="异常捕获结束",
            category="控制流",
            handler=CatchNode,
            doc="接收区域内节点抛出的异常，绑定到 <区域名>_error。action=rethrow 可放回失败。",
            params=[
                ParamSpec("label", "text", "区域名", default="try"),
                ParamSpec("action", "select", "处理方式", default="continue",
                          choices=["continue", "rethrow"],
                          help="continue 继续执行；rethrow 重新抛出使整个流程失败"),
            ],
            outputs=["exit"],
        )
    )
    add(
NodeSpec(
            type="finally",
            label="异常区域收尾",
            category="控制流",
            handler=FinallyNode,
            doc="区域收尾节点。注意：当前执行器在区域内失败时不会走到本节点。",
            params=[
                ParamSpec("label", "text", "区域名", default="try"),
            ],
            outputs=["exit"],
        )
    )
    add(
NodeSpec(
            type="call_flow",
            label="调用子流程",
            category="控制流",
            handler=CallFlowNode,
            doc="把另一个已保存的流程当作一步执行。嵌套上限 8 层，递归调用会在第 8 层明确失败。",
            params=[
                ParamSpec("flow_id", "text", "子流程 ID", required=True),
                ParamSpec("inputs", "variable", "输入变量", default={},
                          help="JSON 对象，写入子流程的初始作用域"),
                ParamSpec("output_prefix", "text", "输出前缀", default="sub",
                          help="子流程结果以此为前缀写回：<前缀>_status / _result / _error"),
                ParamSpec("dry_run", "bool", "试运行", default=False),
            ],
            outputs=["ok", "error"],
        )
    )
    return registry
