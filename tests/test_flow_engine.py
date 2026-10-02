"""End-to-end tests for the flow engine.

Run: ``.venv/bin/python -m pytest tests/test_flow_engine.py -v``

These cover the parts that must be right before a flow is allowed near a real
screen: graph validation, branch selection, retry, error routing, cycle safety and
persistence. They use dry-run services so nothing touches the screen.
"""

from __future__ import annotations

import time

import pytest


from rpa.flow.context import FlowScope, evaluate_condition  # noqa: E402
from rpa.flow.executor import BRANCH_KEY, FlowExecutor  # noqa: E402
from rpa.flow.registry import get_node_registry  # noqa: E402
from rpa.flow.schema import Flow, FlowError, NodeError, validate_flow  # noqa: E402
from rpa.flow.seed import build_default_graph  # noqa: E402
from rpa.flow.store import RpaStore  # noqa: E402


# ─────────────────────────────────────────────────────────────── helpers ──

def _flow(nodes, edges, entry="a", variables=None):
    return Flow(
        id="flow_test",
        name="test",
        graph={
            "version": 1,
            "entry": entry,
            "variables": variables or {},
            "nodes": nodes,
            "edges": edges,
        },
    )


def _n(node_id, node_type, x=0, y=0, **kw):
    node = {
        "id": node_id,
        "type": node_type,
        "name": node_type,
        "position": {"x": x, "y": y},
        "params": kw.pop("params", {}),
        "retry": kw.pop("retry", {"max": 0, "delay": 0}),
        "timeout": kw.pop("timeout", None),
        "on_error": kw.pop("on_error", "fail"),
        "outputs": kw.pop("outputs", []),
        "disabled": kw.pop("disabled", False),
        "path": kw.pop("path", None),
        "target": kw.pop("target", None),
    }
    # Silently dropping an unrecognised kwarg would make a test pass against a
    # graph the caller never meant to build.
    assert not kw, f"_n() got unknown keys {sorted(kw)}"
    return node


def _e(edge_id, source, target, port="ok", condition=None):
    return {"id": edge_id, "source": source, "source_port": port, "target": target, "condition": condition}


# ──────────────────────────────────────────────────────────── validation ──

def test_seed_graph_is_valid():
    issues = validate_flow(build_default_graph(), get_node_registry().types())
    assert [i for i in issues if i.severity == "error"] == []
    assert [i for i in issues if i.severity == "warning"] == []


def test_validator_flags_dangling_edge_and_unknown_type():
    graph = {
        "version": 1,
        "entry": "a",
        "nodes": [_n("a", "start"), _n("b", "no_such_type")],
        "edges": [_e("e1", "a", "b"), _e("e2", "b", "ghost")],
    }
    codes = {i.code for i in validate_flow(graph, get_node_registry().types()) if i.severity == "error"}
    assert "unknown_node_type" in codes
    assert "dangling_edge" in codes


def test_validator_flags_bad_condition():
    graph = {
        "version": 1,
        "entry": "a",
        "nodes": [_n("a", "start"), _n("b", "end")],
        "edges": [_e("e1", "a", "b", condition="not a valid condition")],
    }
    codes = {i.code for i in validate_flow(graph, get_node_registry().types())}
    assert "bad_condition" in codes


# ───────────────────────────────────────────────────────────── execution ──

def test_linear_execution_binds_outputs():
    flow = _flow(
        [_n("a", "set_var", params={"name": "x", "value": 41}),
         _n("b", "set_var", params={"name": "y", "expression": "x + 1"})],
        [_e("e1", "a", "b")],
    )
    result = FlowExecutor().run(flow, "run_1")
    assert result.status == "ok", result.error
    assert result.scope["x"] == 41
    assert result.scope["y"] == 42
    assert result.steps == 2


def test_condition_branches_true():
    flow = _flow(
        [_n("a", "set_var", params={"name": "n", "value": 5}),
         _n("c", "condition", params={"expression": "n > 3"})],
        [_e("e1", "a", "c")],
    )
    seen = []
    executor = FlowExecutor(span_hook=lambda s: seen.append((s.node_id, s.status)))
    result = executor.run(flow, "run_2")
    assert result.status == "ok", result.error
    # Each node emits a "running" event then a settled one.
    assert ("c", "running") in seen
    assert ("c", "ok") in seen


def test_branch_key_routes_without_raising():
    """A node returning __branch__ is traced as a success, not an error."""
    flow = _flow(
        [_n("a", "set_var", params={"name": "n", "value": 0}),
         _n("c", "condition", params={"expression": "n > 3"}, outputs=["true", "false"]),
         _n("t", "set_var", params={"name": "taken", "value": "true"}),
         _n("f", "set_var", params={"name": "taken", "value": "false"})],
        [_e("e1", "a", "c"), _e("e2", "c", "t", port="true"), _e("e3", "c", "f", port="false")],
    )
    result = FlowExecutor().run(flow, "run_3")
    assert result.status == "ok", result.error
    assert result.scope["taken"] == "false"
    assert result.steps == 3


def test_edge_condition_selects_next_node():
    flow = _flow(
        [_n("a", "set_var", params={"name": "count", "value": 3}),
         _n("x", "set_var", params={"name": "route", "value": "high"}),
         _n("y", "set_var", params={"name": "route", "value": "low"})],
        [_e("e1", "a", "x", condition="count > 10"),
         _e("e2", "a", "y")],
    )
    result = FlowExecutor().run(flow, "run_4")
    assert result.scope["route"] == "low"


def test_error_routes_to_error_port():
    flow = _flow(
        [_n("a", "file", on_error="branch", params={"mode": "read", "path": "definitely/missing.txt"}),
         _n("ok", "set_var", params={"name": "reached", "value": "ok"})],
        [_e("e1", "a", "ok")],
    )
    result = FlowExecutor().run(flow, "run_5")
    assert result.status == "ok", result.error
    assert result.scope["reached"] == "ok"


def test_error_fails_run_by_default():
    flow = _flow([_n("a", "condition", params={"expression": ""})], [])
    result = FlowExecutor().run(flow, "run_6")
    assert result.status == "error"
    assert result.failed_node == "a"
    assert "expression" in (result.error or "")


def test_missing_file_is_a_normal_return_not_an_error():
    """A file that is not there is an outcome a flow can branch on, not a crash."""
    flow = _flow([_n("a", "file", params={"mode": "read", "path": "definitely/missing.txt"})], [])
    result = FlowExecutor().run(flow, "run_6b")
    assert result.status == "ok", result.error
    assert result.scope["a"]["read"] is False
    assert result.scope["a"]["reason"] == "file not found"


def test_retry_emits_one_span_per_attempt():
    flow = _flow(
        [_n("a", "condition", retry={"max": 2, "delay": 0},
            params={"expression": "1/0"})],
        [],
    )
    # The hook receives the same Span object twice per attempt (started, then
    # settled in place), so snapshot it rather than holding the reference.
    seen = []
    executor = FlowExecutor(span_hook=lambda s: seen.append((s.attempt, s.ended_at is not None, s.status)))
    result = executor.run(flow, "run_7")
    assert result.status == "error"
    settled = [attempt for attempt, done, _ in seen if done]
    assert settled == [1, 2, 3], seen
    assert all(status == "error" for _, done, status in seen if done)


def test_file_node_rejects_path_escape():
    flow = _flow([_n("a", "file", params={"mode": "write", "path": "../../escape.txt", "content": "x"})], [])
    result = FlowExecutor().run(flow, "run_8")
    assert result.status == "error"
    assert "越出项目根目录" in (result.error or "")


def test_max_steps_stops_a_runaway_loop():
    flow = _flow(
        [_n("a", "set_var", params={"name": "n", "value": 0}), _n("b", "set_var", params={"name": "n", "value": 1})],
        [_e("e1", "a", "b"), _e("e2", "b", "a")],
    )
    result = FlowExecutor(max_steps=20).run(flow, "run_9")
    assert result.status == "error"
    assert "最大执行步数" in (result.error or "")


def test_disabled_node_is_skipped():
    flow = _flow(
        [_n("a", "set_var", params={"name": "n", "value": 1}),
         _n("b", "set_var", disabled=True, params={"name": "n", "value": 999}),
         _n("c", "set_var", params={"name": "n", "value": 2})],
        [_e("e1", "a", "b"), _e("e2", "b", "c")],
    )
    result = FlowExecutor().run(flow, "run_10")
    assert result.scope["n"] == 2


def test_invalid_graph_is_rejected_before_execution():
    flow = _flow([_n("a", "nonexistent_type")], [])
    result = FlowExecutor().run(flow, "run_11")
    assert result.status == "error"
    assert "unknown_node_type" in (result.error or "")


def test_timeout_fails_the_node():
    flow = _flow([_n("a", "wait", timeout=0.3, params={"seconds": 3})], [])
    started = time.monotonic()
    result = FlowExecutor().run(flow, "run_12")
    elapsed = time.monotonic() - started
    assert result.status == "error"
    assert "超时" in (result.error or "")
    assert elapsed < 2.5, f"timeout did not cut the node short ({elapsed:.1f}s)"


def test_crash_inside_timeout_thread_is_not_reported_as_success():
    """Regression: a node raising on the timeout thread used to be traced as a
    success with empty output, so a flow whose perception was completely broken
    still reported ``ok``. The exception must cross the thread boundary."""
    from rpa.flow import builtin_nodes
    from rpa.flow.registry import NodeSpec

    registry = get_node_registry()

    class Exploding(builtin_nodes.BaseNode):
        def execute(self):
            raise ModuleNotFoundError("No module named 'AppKit'")

    registry.register(NodeSpec(type="_exploding", label="崩", category="测试", handler=Exploding))

    flow = _flow(
        [_n("a", "_exploding", timeout=5),
         _n("b", "set_var", params={"name": "reached", "value": "yes"})],
        [_e("e1", "a", "b")],
    )
    result = FlowExecutor(registry=registry).run(flow, "run_13")
    assert result.status == "error", f"a crashed node was reported as {result.status!r}"
    assert "AppKit" in (result.error or "")
    assert "reached" not in result.scope, "execution continued past a crashed node"


def test_node_returning_none_is_treated_as_empty_not_crashed():
    flow = _flow([_n("a", "start")], [])
    result = FlowExecutor().run(flow, "run_14")
    assert result.status == "ok"


# ──────────────────────────────────────────────────────────── conditions ──

@pytest.mark.parametrize(
    "expression,scope,expected",
    [
        ("count > 3", {"count": 5}, True),
        ("count > 3", {"count": 1}, False),
        ("name == bob", {"name": "bob"}, True),
        ("name == bob", {"name": "alice"}, False),
        ("flag == true", {"flag": True}, True),
        ("text ~ 微信", {"text": "这是微信消息"}, True),
        ("items.count > 0", {"items": [1, 2]}, True),
        # A bare word on the right that names nothing stays a literal, so
        # quoting stays optional; the left-hand side is a value being read and
        # is not given that leniency.
        ("name == bob and count > 0", {"name": "bob", "count": 1}, True),
        ("name == bob and count > 0", {"name": "bob", "count": 0}, False),
        ("name == bob and count > 0", {"name": "alice", "count": 1}, False),
    ],
)
def test_evaluate_condition(expression, scope, expected):
    assert evaluate_condition(expression, FlowScope(scope)) is expected


def test_evaluate_condition_rejects_undefined_variable():
    """A typo in a guard has to fail loudly, not quietly disable it.

    Under the old comparison-regex evaluator the missing name resolved to
    ``None``, so ``missing.count > 0`` evaluated to ``False`` and the branch it
    guarded simply never ran. A flow that stops working for a reason nothing in
    its trace mentions is the failure mode this check exists to prevent.
    """
    with pytest.raises(FlowError, match="未定义"):
        evaluate_condition("missing.count > 0", FlowScope({"other": 1}))


def test_evaluate_condition_rejects_garbage():
    with pytest.raises(FlowError):
        evaluate_condition("this is not an expression", FlowScope({}))


def test_scope_dotted_reads():
    scope = FlowScope({"result": {"chat_name": "群A", "items": [1, 2, 3]}})
    assert scope.get("result.chat_name") == "群A"
    assert scope.get("result.items.count") == 3
    assert scope.get("result.items.first") == 1
    assert scope.get("result.nope") is None
    assert scope.get("absent") is None


# ────────────────────────────────────────────────────────── persistence ──

def test_store_roundtrip(tmp_path):
    store = RpaStore(tmp_path / "rpa.db")
    graph = build_default_graph()
    saved = store.save_flow("f1", "测试流程", graph, description="d")
    assert saved.id == "f1" and len(saved.nodes) == len(graph["nodes"])

    loaded = store.get_flow("f1")
    assert loaded is not None and loaded.name == "测试流程"
    assert store.get_flow_by_name("测试流程").id == "f1"
    assert [f["id"] for f in store.list_flows()] == ["f1"]

    store.start_run("r1", "f1", "manual", "", {"a": 1})
    store.insert_span({"run_id": "r1", "node_id": "a", "node_type": "start", "status": "ok",
                       "started_at": time.time(), "ended_at": time.time(), "duration_ms": 1,
                       "inputs": {}, "outputs": {"ok": True}})
    store.finish_run({"run_id": "r1", "flow_id": "f1", "status": "ok", "steps": 3, "scope": {"a": 1},
                      "ended_at": time.time()})

    run = store.get_run("r1")
    assert run["status"] == "ok" and run["steps"] == 3
    assert store.list_spans("r1")[0]["node_id"] == "a"
    assert store.list_runs(flow_id="f1")[0]["flow_name"] == "测试流程"
    assert store.delete_flow("f1") is True


def test_store_element_roundtrip(tmp_path):
    store = RpaStore(tmp_path / "rpa.db")
    element = store.save_element({"name": "搜索框", "kind": "ocr", "rect": {"x": 10, "y": 20, "width": 30, "height": 8}, "ocr_text": "搜索"})
    assert element["name"] == "搜索框"
    assert store.find_element("搜索框")["ocr_text"] == "搜索"
    assert store.find_element("nope") is None
    assert len(store.list_elements()) == 1
    assert store.delete_element(element["id"]) is True


def test_store_marks_stale_runs_failed(tmp_path):
    store = RpaStore(tmp_path / "rpa.db")
    store.save_flow("f1", "n", {"version": 1, "entry": "a", "nodes": [], "edges": []})
    store.start_run("r1", "f1", "manual", "", {})
    assert store.mark_stale_runs_failed() == 1
    assert store.get_run("r1")["status"] == "error"


# ────────────────────────────────────────── dual-path locate strategy ──

def test_target_registry_matches_window_title():
    from rpa.flow.strategy import LocateMode, TargetRegistry, Target

    registry = TargetRegistry()
    wechat = registry.for_title("WeChat")
    assert wechat is not None and wechat.name == "wechat"
    assert wechat.mode is LocateMode.AUTO
    # An unlisted window falls back to the catch-all target, which is vision-only.
    other = registry.for_title("Xcode")
    assert other is not None and other.mode is LocateMode.VISION

    # A more specific target wins over the catch-all.
    registry.register(Target(name="xcode", kind="desktop", match=r"^Xcode$", mode=LocateMode.ELEMENT))
    assert registry.for_title("Xcode").name == "xcode"


def test_locate_prefers_element_and_records_provenance(tmp_path, monkeypatch):
    """Path A must win under auto, and the trace must say so."""
    from rpa.flow import elements, strategy
    from rpa.flow.strategy import LocateMode, Located

    called = []

    def fake_locate_element(ctx, name):
        called.append(name)
        return Located(x=100, y=200, source="element", element_name=name)

    def boom(*a, **k):
        raise AssertionError("vision must not be called when the element resolves")

    monkeypatch.setattr(elements, "locate_element", fake_locate_element)
    monkeypatch.setattr("rpa.flow.vision.locate_by_vision", boom)

    result = strategy.resolve(None, description="搜索框", target="wechat", mode="auto", element="搜索框")
    assert called == ["搜索框"]
    assert (result.x, result.y) == (100, 200)
    assert result.source == "element"
    assert result.mode == "auto"


def test_locate_falls_back_to_vision_and_records_provenance(monkeypatch):
    from rpa.flow import elements, strategy
    from rpa.flow.strategy import Located

    monkeypatch.setattr(elements, "locate_element", lambda ctx, name: None)
    monkeypatch.setattr(
        "rpa.flow.vision.locate_by_vision",
        lambda ctx, description, **kw: Located(x=7, y=9, source="vision", confidence=0.8, label=description),
    )
    result = strategy.resolve(None, description="那个蓝色的按钮", target="wechat", mode="auto")
    assert result.source == "vision"
    assert (result.x, result.y) == (7, 9)


def test_element_strict_never_falls_back_to_a_model(monkeypatch):
    from rpa.flow import elements, strategy
    from rpa.flow.schema import NodeError

    monkeypatch.setattr(elements, "locate_element", lambda ctx, name: None)
    monkeypatch.setattr(
        "rpa.flow.vision.locate_by_vision",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")),
    )
    with pytest.raises(NodeError) as exc:
        strategy.resolve(None, description="x", target="wechat", mode="element_strict")
    assert "element_strict" in str(exc.value)


def test_vision_mode_ignores_the_element_library(monkeypatch):
    from rpa.flow import elements, strategy
    from rpa.flow.strategy import Located

    monkeypatch.setattr(elements, "locate_element", lambda ctx, name: Located(x=1, y=1, source="element"))
    monkeypatch.setattr(
        "rpa.flow.vision.locate_by_vision",
        lambda ctx, description, **kw: Located(x=500, y=600, source="vision"),
    )
    result = strategy.resolve(None, description="x", target="any_window", mode="vision", element="有元素也不用")
    assert result.source == "vision" and result.x == 500


def test_unknown_target_is_an_error_not_a_silent_fallback():
    from rpa.flow.schema import NodeError
    from rpa.flow.strategy import resolve

    with pytest.raises(NodeError):
        resolve(None, description="x", target="不存在的应用")


# ──────────────────────────────────────────── vision coordinate handling ──

def test_vision_action_scales_retina_coordinates():
    """A 2x screenshot must be halved before clicking, or the click lands 2x off."""
    from rpa.flow.vision import VisionAction

    action = VisionAction.from_payload(
        {"action": "click", "x": 400, "y": 600, "confidence": 0.9}, scale=2.0
    )
    assert (action.x, action.y) == (200, 300)


def test_vision_action_defaults_to_scale_one_when_unknown():
    from rpa.flow.vision import VisionAction

    for scale in (0, None, 1.0):
        action = VisionAction.from_payload({"action": "click", "x": 120, "y": 240}, scale=scale)
        assert (action.x, action.y) == (120, 240)


def test_vision_action_handles_missing_and_junk_coordinates():
    from rpa.flow.vision import VisionAction

    action = VisionAction.from_payload({"action": "click", "x": "左下角", "y": None}, scale=1.0)
    assert action.x is None and action.y is None
    junk = VisionAction.from_payload({"action": "scroll", "points": [{"x": "a"}, {"x": 10, "y": 20}]}, scale=1.0)
    assert junk.points == [{"x": 10, "y": 20}]


# ─────────────────────────────────────── design-time path declaration ──

def test_node_path_roundtrips():
    from rpa.flow.schema import Node

    node = Node.from_dict({"id": "a", "type": "locate", "path": "vision", "target": "wechat"})
    assert node.path == "vision" and node.target == "wechat"
    assert Node.from_dict(node.to_dict()).path == "vision"
    assert Node.from_dict({"id": "a", "type": "locate"}).path is None


def test_invalid_path_is_rejected_at_parse_time():
    from rpa.flow.schema import FlowError, Node

    with pytest.raises(FlowError) as exc:
        Node.from_dict({"id": "a", "type": "locate", "path": "魔法"})
    assert "invalid path" in str(exc.value)


def test_flow_default_path_is_inherited_by_nodes_that_declare_nothing():
    graph = {
        "version": 1, "entry": "a", "default_path": "element",
        "nodes": [_n("a", "locate", params={"element": "搜索框"})], "edges": [],
    }
    codes = {i.code for i in validate_flow(graph, get_node_registry().types())}
    assert "no_path_declared" not in codes


def test_path_aware_node_without_a_path_warns():
    graph = {
        "version": 1, "entry": "a",
        "nodes": [_n("a", "locate", params={"element": "x"})], "edges": [],
    }
    issues = validate_flow(graph, get_node_registry().types())
    assert any(i.code == "no_path_declared" for i in issues)


def test_auto_path_warns_about_the_model_call_it_can_cost():
    graph = {
        "version": 1, "entry": "a", "default_path": "element",
        "nodes": [_n("a", "locate", path="auto", params={"element": "x"})], "edges": [],
    }
    issues = validate_flow(graph, get_node_registry().types())
    assert any(i.code == "auto_path_costs_a_model_call" for i in issues)


def test_path_on_an_unaware_type_warns_that_it_is_ignored():
    graph = {
        "version": 1, "entry": "a", "default_path": "element",
        "nodes": [_n("a", "wait", path="vision", params={"seconds": 0})], "edges": [],
    }
    issues = validate_flow(graph, get_node_registry().types())
    assert any(i.code == "path_on_unaware_type" for i in issues)


def test_registry_marks_which_types_can_declare_a_path():
    specs = {s["type"]: s for group in get_node_registry().catalogue() for s in group["nodes"]}
    assert specs["locate"]["path_aware"] is True
    assert specs["locate"]["path_choices"] == ["element", "element_strict", "vision", "auto"]
    assert specs["wait"]["path_aware"] is False
    assert specs["wait"]["path_choices"] == []


def test_node_path_field_beats_a_legacy_mode_param(monkeypatch):
    """The field is the declaration the canvas shows, so it must win."""
    from rpa.flow import builtin_nodes
    from rpa.flow.registry import NodeSpec

    seen: dict[str, object] = {}

    class Spy(builtin_nodes.BaseNode):
        def execute(self):
            seen["path"] = self.locate_mode()
            seen["target"] = self.target_name()
            return {"__branch__": "ok"}

    registry = get_node_registry()
    registry.register(NodeSpec(type="_spy", label="探针", category="测试", handler=Spy))

    flow = _flow(
        [_n("a", "_spy", path="vision", target="any_window", params={"mode": "element", "target": "wechat"})],
        [],
    )
    assert FlowExecutor(registry=registry).run(flow, "run_path").status == "ok"
    assert seen == {"path": "vision", "target": "any_window"}


def test_flow_default_path_reaches_the_node_when_it_declares_none(monkeypatch):
    from rpa.flow import builtin_nodes
    from rpa.flow.registry import NodeSpec

    seen: dict[str, object] = {}

    class Spy(builtin_nodes.BaseNode):
        def execute(self):
            seen["path"] = self.locate_mode()
            return {"__branch__": "ok"}

    registry = get_node_registry()
    registry.register(NodeSpec(type="_spy2", label="探针2", category="测试", handler=Spy))

    flow = _flow([_n("a", "_spy2", params={})], [])
    flow.graph["default_path"] = "element_strict"
    assert FlowExecutor(registry=registry).run(flow, "run_path2").status == "ok"
    assert seen["path"] == "element_strict"
