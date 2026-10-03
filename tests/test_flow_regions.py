"""``try`` / ``catch`` / ``finally`` regions.

These exist as regression tests for a defect that made the whole feature inert.
The region was documented as working because the node docstring and the module
docstring both said the validator rewrote ``on_error`` inside the block. No such
rewrite existed anywhere. A flow drawn in the canvas keeps the default
``on_error: fail``, so a body failure ended the run and the ``catch`` node was
never reached — a graph that reads as protected and protects nothing.

The tests below therefore assert the *default* configuration, not a hand-wired
``on_error: branch``, because the default is what a user actually gets.
"""

from __future__ import annotations


import pytest


from apps.engine.control_nodes import REGION_CLOSERS, REGION_OPEN, compute_regions  # noqa: E402
from apps.engine.executor import FlowExecutor  # noqa: E402
from apps.engine.schema import Flow, Node, NodeError  # noqa: E402


def _flow(nodes, edges, entry="a", variables=None):
    return Flow(
        id="flow_region",
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
    return FlowExecutor().run(_flow(nodes, edges), "run_region", **kw)


def _boom_node(node_id="boom", **kw):
    """A node that always raises ``NodeError``, registered for the test."""
    from apps.engine import builtin_nodes
    from apps.engine.registry import NodeSpec, get_node_registry

    class Boom(builtin_nodes.BaseNode):
        def execute(self):
            raise NodeError("窗口太小")

    get_node_registry().register(
        NodeSpec(type="_region_boom", label="炸", category="测试", handler=Boom)
    )
    return _n(node_id, "_region_boom", **kw)


# ── the defect: a region drawn in the canvas protected nothing ─────────────

def test_a_failure_inside_a_region_reaches_the_catch_with_the_default_policy():
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _boom_node(),
            _n("c", "catch", params={"label": "reg", "action": "continue"}),
            _n("d", "set_var", params={"name": "survived", "value": True}),
        ],
        [
            _e("e1", "a", "boom", "body"),
            _e("e2", "boom", "c", "ok"),
            _e("e3", "boom", "c", "error"),
            _e("e4", "c", "d", "exit"),
        ],
    )
    assert result.status == "ok", result.error
    assert result.scope["c"]["caught"] is True
    assert result.scope.get("survived") is True


def test_the_caught_reason_reaches_the_flow():
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _boom_node(),
            _n("c", "catch", params={"label": "reg", "action": "continue"}),
        ],
        [
            _e("e1", "a", "boom", "body"),
            _e("e2", "boom", "c", "ok"),
            _e("e3", "boom", "c", "error"),
        ],
    )
    assert "窗口太小" in result.scope["c"]["error"]
    assert "窗口太小" in result.scope["reg_error"]
    assert result.scope["c"]["error_node"] == "boom"


def test_a_body_that_succeeds_reports_that_nothing_was_caught():
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _n("b", "set_var", params={"name": "fine", "value": 1}),
            _n("c", "catch", params={"label": "reg", "action": "continue"}),
        ],
        [_e("e1", "a", "b", "body"), _e("e2", "b", "c", "ok")],
    )
    assert result.status == "ok", result.error
    assert result.scope["c"]["caught"] is False
    # An empty string, never the literal "None": "no error" and "we do not
    # know" have to look different to whatever reads this next.
    assert result.scope["c"]["error"] == ""


def test_rethrow_puts_the_failure_back_with_its_source():
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _boom_node(),
            _n("c", "catch", params={"label": "reg", "action": "rethrow"}),
        ],
        [
            _e("e1", "a", "boom", "body"),
            _e("e2", "boom", "c", "ok"),
            _e("e3", "boom", "c", "error"),
        ],
    )
    assert result.status == "error"
    assert "重新抛出" in (result.error or "")
    assert "窗口太小" in (result.error or "")
    assert "boom" in (result.error or "")


def test_a_catch_reached_over_a_plain_error_edge_still_learns_the_reason():
    # No `try` in the graph at all — a catch wired straight to a node's error
    # port, with the node explicitly asking to branch. This is the shape the
    # rethrow test uses, and a handler that said "no error" here would be
    # reporting about a failure it just received.
    #
    # ``on_error: branch`` is still required: outside a region it is the only
    # thing that says "route my failure to the error port", and defaulting to
    # "an error edge implies branch" would make an unlabelled error port
    # silently swallow failures.
    result = FlowExecutor().run(
        _flow(
            [
                _boom_node(on_error="branch", outputs=["ok", "error"]),
                _n("c", "catch", params={"label": "reg", "action": "continue"}),
            ],
            [_e("e1", "boom", "c", "error")],
            entry="boom",
        ),
        "run_region_bare",
    )
    assert result.status == "ok", result.error
    assert result.scope["c"]["caught"] is True
    assert "窗口太小" in result.scope["c"]["error"]


def test_a_node_outside_any_region_keeps_failing_despite_an_error_edge():
    # The other half of the contract above, pinned so the convenience is never
    # widened by accident.
    result = FlowExecutor().run(
        _flow(
            [
                _boom_node(outputs=["ok", "error"]),
                _n("c", "catch", params={"label": "reg", "action": "continue"}),
            ],
            [_e("e1", "boom", "c", "error")],
            entry="boom",
        ),
        "run_region_unbranched",
    )
    assert result.status == "error"
    assert "窗口太小" in (result.error or "")


def test_a_trap_whose_handler_is_the_last_node_still_counts_as_handled():
    # ``_next`` returns "" for "the flow ends here", which is a success.
    # Treating that as "the trap did not fire" turned a handled failure into a
    # failed run whenever the handler happened to be the final node.
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _boom_node(),
            _n("c", "catch", params={"label": "reg", "action": "continue"}),
        ],
        [_e("e1", "a", "boom", "body"), _e("e2", "boom", "c", "error")],
    )
    assert result.status == "ok", result.error
    assert result.scope["c"]["caught"] is True


# ── region membership ──────────────────────────────────────────────────────

def _graph_pairs(nodes, edges):
    parsed = {n["id"]: Node.from_dict(n) for n in nodes}
    indexed: dict[str, list] = {}
    for raw in edges:
        from apps.engine.schema import Edge

        edge = Edge.from_dict(raw)
        indexed.setdefault(edge.source, []).append(edge)
    return parsed, indexed


def test_compute_regions_finds_the_body_and_the_closer():
    nodes = [
        _n("a", "try", params={"label": "reg"}),
        _n("b", "set_var"),
        _n("c", "set_var"),
        _n("d", "catch", params={"label": "reg"}),
    ]
    edges = [
        _e("e1", "a", "b", "body"), _e("e2", "b", "c"),
        _e("e3", "c", "d", "ok"),
    ]
    regions = compute_regions(*_graph_pairs(nodes, edges))
    assert len(regions) == 1
    assert regions[0].label == "reg"
    assert regions[0].catch_id == "d"
    assert regions[0].body == frozenset({"b", "c"})


def test_the_try_and_catch_nodes_are_not_their_own_body():
    nodes = [
        _n("a", "try", params={"label": "reg"}),
        _n("b", "set_var"),
        _n("d", "catch", params={"label": "reg"}),
    ]
    edges = [_e("e1", "a", "b", "body"), _e("e2", "b", "d", "ok")]
    regions = compute_regions(*_graph_pairs(nodes, edges))
    # A trap that caught the failure of the node implementing the trap is not
    # a trap; a `catch` that failed would recurse.
    assert regions[0].body == frozenset({"b"})


def test_a_closer_with_a_different_label_is_not_this_region_s_terminator():
    nodes = [
        _n("a", "try", params={"label": "outer"}),
        _n("b", "set_var"),
        _n("d", "catch", params={"label": "inner"}),
    ]
    edges = [_e("e1", "a", "b", "body"), _e("e2", "b", "d", "ok")]
    regions = compute_regions(*_graph_pairs(nodes, edges))
    assert regions[0].catch_id is None
    assert "d" in regions[0].body


def test_a_loop_inside_a_region_terminates_the_walk():
    nodes = [
        _n("a", "try", params={"label": "reg"}),
        _n("b", "set_var"),
        _n("c", "set_var"),
        _n("d", "catch", params={"label": "reg"}),
    ]
    # b → c → b is a back edge: the naive walk would never finish.
    edges = [
        _e("e1", "a", "b", "body"), _e("e2", "b", "c"),
        _e("e3", "c", "b"), _e("e4", "c", "d", "ok"),
    ]
    regions = compute_regions(*_graph_pairs(nodes, edges))
    assert regions[0].body == frozenset({"b", "c"})


def test_an_inner_failure_reaches_the_inner_handler_only():
    # Nested regions must not both claim the same node: one failure firing two
    # handlers is two side effects for one error.
    result = _run(
        [
            _n("a", "try", params={"label": "outer"}),
            _n("a2", "try", params={"label": "inner"}),
            _boom_node(),
            _n("ci", "catch", params={"label": "inner", "action": "continue"}),
            _n("d", "set_var", params={"name": "inner_fired", "value": True}),
            _n("co", "catch", params={"label": "outer", "action": "continue"}),
            _n("e", "set_var", params={"name": "outer_fired", "value": True}),
        ],
        [
            _e("e1", "a", "a2", "body"),
            _e("e2", "a2", "boom", "body"),
            _e("e3", "boom", "ci", "error"),
            _e("e4", "ci", "d", "exit"),
            _e("e5", "a2", "co", "ok"),
            _e("e6", "co", "e", "exit"),
        ],
    )
    assert result.status == "ok", result.error
    assert result.scope.get("inner_fired") is True
    assert "outer_fired" not in result.scope


def test_the_finally_node_is_found_as_a_closer():
    nodes = [
        _n("a", "try", params={"label": "reg"}),
        _n("b", "set_var"),
        _n("f", "finally", params={"label": "reg"}),
    ]
    edges = [_e("e1", "a", "b", "body"), _e("e2", "b", "f", "ok")]
    regions = compute_regions(*_graph_pairs(nodes, edges))
    assert regions[0].finally_id == "f"
    assert regions[0].catch_id is None


def test_a_region_with_only_finally_still_traps_the_failure():
    # try/finally with no catch: the terminator is the finally, and a failure
    # that fell through to a dead run would skip the cleanup the author drew.
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _boom_node(),
            _n("f", "finally", params={"label": "reg"}),
            _n("d", "set_var", params={"name": "cleaned", "value": True}),
        ],
        [
            _e("e1", "a", "boom", "body"),
            _e("e2", "boom", "f", "ok"),
            _e("e3", "f", "d"),
        ],
    )
    assert result.status == "ok", result.error
    assert result.scope.get("cleaned") is True


def test_a_flow_with_no_regions_resolves_to_nothing():
    nodes = [_n("a", "set_var"), _n("b", "set_var")]
    edges = [_e("e1", "a", "b")]
    assert compute_regions(*_graph_pairs(nodes, edges)) == []


def test_the_closer_constants_match_the_registered_node_types():
    from apps.engine.registry import get_node_registry

    types = set(get_node_registry().types())
    assert REGION_OPEN in types
    assert set(REGION_CLOSERS) <= types


# ── the binding is cleared on entry ────────────────────────────────────────

def test_try_clears_a_previous_iterations_error():
    # A region inside a loop runs more than once. Without the reset, the second
    # pass would find the first pass's failure still bound and claim it caught
    # something that did not happen.
    result = _run(
        [
            _n("a", "try", params={"label": "reg"}),
            _n("b", "set_var", params={"name": "seen", "value": "@{n}"}),
            _n("c", "catch", params={"label": "reg", "action": "continue"}),
        ],
        [_e("e1", "a", "b", "body"), _e("e2", "b", "c", "ok")],
        variables={"n": 1},
    )
    assert result.status == "ok", result.error
    assert result.scope["reg_error"] == ""
    assert result.scope["c"]["caught"] is False


def test_re_running_the_same_region_twice_does_not_carry_the_first_error():
    nodes = [
        _n("a", "try", params={"label": "reg"}),
        _n("b", "set_var", params={"name": "x", "value": 1}),
        _n("c", "catch", params={"label": "reg", "action": "continue"}),
        _n("d", "loop_back", params={}),  # replaced below; keeps the shape readable
    ]
    del nodes[3]
    flow = _flow(
        [
            _n("a", "try", params={"label": "reg"}),
            _n("b", "set_var", params={"name": "x", "value": 1}),
            _n("c", "catch", params={"label": "reg", "action": "continue"}),
        ],
        [_e("e1", "a", "b", "body"), _e("e2", "b", "c", "ok")],
    )
    result = FlowExecutor().run(flow, "run_region_twice")
    assert result.scope["c"]["caught"] is False
    # Running the same Flow object again must not see the previous run's scope.
    again = FlowExecutor().run(flow, "run_region_twice_2")
    assert again.scope["c"]["caught"] is False


@pytest.mark.parametrize("action", ["continue", "rethrow"])
def test_every_action_is_reachable_and_reports_the_region_label(action: str):
    result = _run(
        [
            _n("a", "try", params={"label": "named"}),
            _boom_node(),
            _n("c", "catch", params={"label": "named", "action": action}),
        ],
        [
            _e("e1", "a", "boom", "body"),
            _e("e2", "boom", "c", "ok"),
            _e("e3", "boom", "c", "error"),
        ],
    )
    if action == "rethrow":
        assert result.status == "error"
    else:
        assert result.status == "ok"
        assert result.scope["c"]["label"] == "named"
