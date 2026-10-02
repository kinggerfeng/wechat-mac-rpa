"""Tests for the control-flow and system node types added in P1.

These are the node types that reason about the graph rather than about WeChat,
so the tests here are mostly about the ways they can go wrong: a loop that never
ends, a sub-flow that calls itself, a shell command built from a variable that
contains a semicolon.
"""

from __future__ import annotations


import pytest


from rpa.flow.control_nodes import MAX_SUBFLOW_DEPTH  # noqa: E402
from rpa.flow.executor import FlowExecutor  # noqa: E402
from rpa.flow.registry import get_node_registry  # noqa: E402
from rpa.flow.schema import Flow, NodeError, validate_flow  # noqa: E402
from rpa.flow.store import RpaStore  # noqa: E402


def _flow(nodes, edges, entry="a", variables=None):
    return Flow(
        id="flow_ctrl",
        name="test",
        graph={
            "version": 1,
            "entry": entry,
            "variables": variables or {},
            "nodes": nodes,
            "edges": edges,
        },
    )


def _n(node_id, node_type, **kw):
    node = {
        "id": node_id,
        "type": node_type,
        "name": node_type,
        "position": {"x": 0, "y": 0},
        "params": kw.pop("params", {}),
        "retry": {"max": 0, "delay": 0},
        "timeout": None,
        "on_error": kw.pop("on_error", "fail"),
        "outputs": kw.pop("outputs", []),
        "disabled": False,
        "path": None,
        "target": None,
    }
    assert not kw, f"_n() got unknown keys {sorted(kw)}"
    return node


def _e(edge_id, source, target, port="ok"):
    return {"id": edge_id, "source": source, "source_port": port, "target": target, "condition": None}


def _run(nodes, edges, **kw):
    return FlowExecutor().run(_flow(nodes, edges), "run_ctrl", **kw)


# ────────────────────────────────────────────────────────────────── while ──

def test_while_loop_runs_body_then_exits():
    """Three passes, then the exit port is taken."""
    # a(while) → b(inc i) → back to a ; a exit → d
    flow = _flow(
        [
            _n("a", "while", params={"condition": "i < 3", "max_iterations": 10}),
            _n("b", "set_var", params={"name": "i", "operation": "increment", "by": 1}),
            _n("d", "set_var", params={"name": "done", "value": True}),
        ],
        [
            _e("e1", "a", "b", "body"),
            _e("e2", "b", "a", "ok"),   # close the loop on the body's normal port
            _e("e3", "a", "d", "exit"),
        ],
        variables={"i": 0},
    )
    result = FlowExecutor().run(flow, "run_while")
    assert result.status == "ok", result.error
    assert result.scope.get("done") is True
    # The body ran until the condition failed, not once.
    assert result.scope.get("i") == 3


def test_while_stops_at_max_iterations_and_names_the_node():
    """A condition that never goes false must fail loudly, naming the loop."""
    flow = _flow(
        [
            _n("a", "while", params={"condition": "1 == 1", "max_iterations": 5}),
            _n("b", "set_var", params={"name": "touched", "value": True}),
        ],
        [
            _e("e1", "a", "b", "body"),
            _e("e2", "b", "a", "ok"),   # close the loop on the body's normal port
        ],
    )
    result = FlowExecutor().run(flow, "run_infinite")
    assert result.status == "error"
    assert "最大轮数" in (result.error or ""), result.error
    # Five passes of node b, not a runaway.
    assert result.steps < 40, f"loop ran {result.steps} steps"


def test_while_exits_immediately_when_condition_is_false():
    flow = _flow(
        [
            _n("a", "while", params={"condition": "1 == 2", "max_iterations": 5}),
            _n("b", "set_var", params={"name": "body_ran", "value": True}),
            _n("c", "set_var", params={"name": "skipped", "value": True}),
        ],
        [
            _e("e1", "a", "b", "body"),
            _e("e2", "b", "c"),
            _e("e3", "a", "c", "exit"),
        ],
    )
    result = FlowExecutor().run(flow, "run_nowhile")
    assert result.status == "ok", result.error
    assert "body_ran" not in result.scope
    assert result.scope.get("skipped") is True


# ──────────────────────────────────────────────────────────────── foreach ──

def test_foreach_binds_each_item_and_finishes_on_done():
    flow = _flow(
        [
            _n("a", "foreach", params={"collection": "items", "item_var": "row"}),
            _n("b", "set_var", params={"name": "last", "expression": "row"}),
            _n("c", "set_var", params={"name": "finished", "value": True}),
        ],
        [
            _e("e1", "a", "b", "item"),
            _e("e2", "b", "a", "ok"),   # close the loop on the body's normal port
            _e("e3", "a", "c", "done"),
        ],
        variables={"items": ["x", "y", "z"]},
    )
    result = FlowExecutor().run(flow, "run_foreach")
    assert result.status == "ok", result.error
    assert result.scope.get("last") == "z"
    assert result.scope.get("finished") is True


def test_foreach_on_empty_list_skips_the_body():
    flow = _flow(
        [
            _n("a", "foreach", params={"collection": "items", "item_var": "row"}),
            _n("b", "set_var", params={"name": "ran", "value": True}),
            _n("c", "set_var", params={"name": "finished", "value": True}),
        ],
        [
            _e("e1", "a", "b", "item"),
            _e("e2", "b", "a", "ok"),
            # With no items the loop must leave by `done` and never touch the body.
            _e("e3", "a", "c", "done"),
        ],
        variables={"items": []},
    )
    result = FlowExecutor().run(flow, "run_empty")
    assert result.status == "ok", result.error
    assert "ran" not in result.scope
    assert result.scope.get("finished") is True


def test_foreach_rejects_a_missing_collection_variable():
    flow = _flow(
        [_n("a", "foreach", params={"collection": "nope", "item_var": "row"})],
        [],
    )
    result = FlowExecutor().run(flow, "run_missing")
    assert result.status == "error"
    assert "nope" in (result.error or "")


# ─────────────────────────────────────────────────────────── error regions ──

def test_catch_receives_a_failure_from_a_branching_node():
    """A node inside the region routes its error to catch, not to the run."""
    from rpa.flow import builtin_nodes
    from rpa.flow.registry import NodeSpec

    registry = get_node_registry()

    class Boom(builtin_nodes.BaseNode):
        def execute(self):
            raise NodeError("窗口太小")

    registry.register(NodeSpec(type="_boom", label="炸", category="测试", handler=Boom))

    flow = _flow(
        [
            _n("a", "try", params={"label": "region"}),
            _n("b", "_boom", on_error="branch", outputs=["ok", "error"]),
            _n("c", "catch", params={"label": "region", "action": "continue"}),
            _n("d", "set_var", params={"name": "survived", "value": True}),
        ],
        [
            _e("e1", "a", "b", "body"),
            _e("e2", "b", "c", "ok"),
            _e("e3", "b", "c", "error"),
            _e("e4", "c", "d", "exit"),
        ],
    )
    result = FlowExecutor(registry=registry).run(flow, "run_catch")
    assert result.status == "ok", result.error
    assert result.scope.get("survived") is True


def test_catch_with_rethrow_fails_the_run():
    from rpa.flow import builtin_nodes
    from rpa.flow.registry import NodeSpec

    registry = get_node_registry()

    class Boom2(builtin_nodes.BaseNode):
        def execute(self):
            raise NodeError("还是炸")

    registry.register(NodeSpec(type="_boom2", label="炸2", category="测试", handler=Boom2))

    flow = _flow(
        [
            _n("a", "_boom2", on_error="branch", outputs=["ok", "error"]),
            _n("c", "catch", params={"label": "r", "action": "rethrow"}),
        ],
        [
            _e("e1", "a", "c", "ok"),
            _e("e2", "a", "c", "error"),
        ],
    )
    result = FlowExecutor(registry=registry).run(flow, "run_rethrow")
    assert result.status == "error"
    assert "重新抛出" in (result.error or "")


# ─────────────────────────────────────────────────────────────── sub-flows ──

def _child_flow(flow_id, entry_nodes, entry_edges, entry="x"):
    """The graph body only — ``save_flow`` takes the id and name separately."""
    return {
        "version": 1,
        "entry": entry,
        "variables": {},
        "nodes": entry_nodes,
        "edges": entry_edges,
    }


def test_call_flow_runs_the_child_and_returns_its_status(tmp_path, monkeypatch):
    store = RpaStore(tmp_path / "rpa.db")
    store.save_flow(
        "child",
        "子流程",
        _child_flow(
            "child",
            [
                _n("x", "set_var", params={"name": "greeted", "value": "hi"}),
                _n("y", "end", params={"message": "完成"}),
            ],
            [_e("ce1", "x", "y")],
        ),
    )
    # control_nodes imports get_store lazily inside execute(), so the only
    # thing to swap is the module-level singleton it will resolve to.
    monkeypatch.setattr("rpa.flow.store._store", store)

    flow = _flow(
        [
            _n("a", "call_flow", params={"flow_id": "child", "output_prefix": "sub"}),
            _n("b", "end", params={"message": "{{sub_status}}"}),
        ],
        [_e("e1", "a", "b")],
    )
    result = FlowExecutor().run(flow, "run_call")
    assert result.status == "ok", result.error
    assert result.scope.get("sub_status") == "ok"
    assert result.scope.get("sub_result") == "完成"


def test_call_flow_on_a_missing_id_fails_with_the_id(tmp_path, monkeypatch):
    store = RpaStore(tmp_path / "rpa.db")
    # control_nodes imports get_store lazily inside execute(), so the only
    # thing to swap is the module-level singleton it will resolve to.
    monkeypatch.setattr("rpa.flow.store._store", store)

    flow = _flow([_n("a", "call_flow", params={"flow_id": "ghost"})], [])
    result = FlowExecutor().run(flow, "run_ghost")
    assert result.status == "error"
    assert "ghost" in (result.error or "")


def test_recursive_subflow_hits_the_depth_cap_instead_of_crashing(tmp_path, monkeypatch):
    """A flow that calls itself must fail with the cap named, not a stack trace."""
    store = RpaStore(tmp_path / "rpa.db")
    # The child calls the child, by id, forever.
    store.save_flow(
        "looper",
        "自调用",
        _child_flow(
            "looper",
            [_n("x", "call_flow", params={"flow_id": "looper", "output_prefix": "sub"})],
            [],
        ),
    )
    # control_nodes imports get_store lazily inside execute(), so the only
    # thing to swap is the module-level singleton it will resolve to.
    monkeypatch.setattr("rpa.flow.store._store", store)

    flow = _flow([_n("a", "call_flow", params={"flow_id": "looper"})], [])
    result = FlowExecutor().run(flow, "run_recurse")
    assert result.status == "error"
    assert str(MAX_SUBFLOW_DEPTH) in (result.error or ""), result.error


# ──────────────────────────────────────────────────────────── system nodes ──

def test_run_shell_captures_stdout_and_branches_on_exit_code():
    flow = _flow(
        [
            _n("a", "run_shell", params={"command": ["echo", "hi"], "outputs": ["ok", "error"]}),
            _n("b", "set_var", params={"name": "zero", "value": True}),
            _n("c", "set_var", params={"name": "nonzero", "value": True}),
        ],
        [_e("e1", "a", "b", "ok"), _e("e2", "a", "c", "error")],
    )
    result = FlowExecutor().run(flow, "run_shell_ok")
    assert result.status == "ok", result.error
    assert result.scope["a"]["stdout"].strip() == "hi"
    assert result.scope.get("zero") is True
    assert "nonzero" not in result.scope


def test_run_shell_branches_to_error_on_failure():
    flow = _flow(
        [
            _n("a", "run_shell", params={"command": ["false"], "outputs": ["ok", "error"]}),
            _n("b", "set_var", params={"name": "failed", "value": True}),
        ],
        [_e("e1", "a", "b", "error")],
    )
    result = FlowExecutor().run(flow, "run_shell_fail")
    assert result.status == "ok", result.error
    assert result.scope["a"]["exit_code"] != 0
    assert result.scope.get("failed") is True


def test_run_shell_does_not_interpret_a_variable_as_a_shell_command():
    """A filename with a shell metacharacter must stay one argument."""
    marker = "should-not-exist"
    flow = _flow(
        [
            _n(
                "a",
                "run_shell",
                params={"command": ["echo", f"safe; touch {marker}"], "outputs": ["ok", "error"]},
            ),
        ],
        [],
    )
    result = FlowExecutor().run(flow, "run_shell_inject")
    assert result.status == "ok", result.error
    assert f"safe; touch {marker}" in result.scope["a"]["stdout"]


def test_run_shell_reports_a_missing_binary_by_name():
    flow = _flow([_n("a", "run_shell", params={"command": ["definitely-not-a-real-binary-xyz"]})], [])
    result = FlowExecutor().run(flow, "run_shell_missing")
    assert result.status == "error"
    assert "definitely-not-a-real-binary-xyz" in (result.error or "")


def test_run_shell_timeout_is_reported(tmp_path):
    flow = _flow(
        [_n("a", "run_shell", params={"command": ["sleep", "5"], "timeout": 0.3})],
        [],
    )
    result = FlowExecutor().run(flow, "run_shell_timeout")
    assert result.status == "error"
    assert "超时" in (result.error or "")


def test_excel_reports_a_missing_file_clearly():
    flow = _flow([_n("a", "excel", params={"path": "/nope/missing.xlsx"})], [])
    result = FlowExecutor().run(flow, "run_excel_missing")
    assert result.status == "error"
    assert "不存在" in (result.error or "")


def test_excel_reads_and_writes_a_workbook(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = "hello"
    wb.save(book)

    flow = _flow([_n("a", "excel", params={"path": str(book), "cell": "A1"})], [])
    result = FlowExecutor().run(flow, "run_excel_read")
    assert result.status == "ok", result.error
    assert result.scope["a"]["value"] == "hello"

    write = _flow(
        [
            _n("a", "excel", params={"path": str(book), "mode": "write", "cell": "B2", "value": 42}),
            _n("b", "excel", params={"path": str(book), "cell": "B2"}),
        ],
        [_e("e1", "a", "b")],
    )
    written = FlowExecutor().run(write, "run_excel_write")
    assert written.status == "ok", written.error
    assert written.scope["b"]["value"] == 42


# ───────────────────────────────────────────────────────────── validation ──

def test_new_node_types_are_registered_and_validated():
    registry = get_node_registry()
    for type_name in ("while", "foreach", "try", "catch", "finally", "call_flow", "run_shell", "excel"):
        assert registry.has(type_name), f"{type_name} 未注册"
    # A back edge is what makes a loop a loop; the validator must permit it.
    issues = validate_flow(
        {
            "version": 1,
            "entry": "a",
            "nodes": [
                _n("a", "while", params={"condition": "1 == 1"}),
                _n("b", "set_var", params={"name": "x", "value": 1}),
            ],
            "edges": [_e("e1", "a", "b", "body"), _e("e2", "b", "a", "loop")],
        },
        registry.types(),
    )
    assert [i for i in issues if i.severity == "error"] == []


def test_self_loop_is_still_rejected():
    registry = get_node_registry()
    issues = validate_flow(
        {
            "version": 1,
            "entry": "a",
            "nodes": [_n("a", "while", params={"condition": "1 == 1"})],
            "edges": [_e("e1", "a", "a", "loop")],
        },
        registry.types(),
    )
    assert any(i.code == "self_loop" for i in issues)


def test_while_without_a_condition_is_reported_at_runtime():
    flow = _flow([_n("a", "while", params={})], [])
    result = FlowExecutor().run(flow, "run_nocond")
    assert result.status == "error"
    assert "condition" in (result.error or "")
