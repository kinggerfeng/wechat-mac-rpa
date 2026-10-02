"""Parallel branches and the join that waits for them.

The hazard this feature walks into is specific to RPA: the second branch of a
fan-out is the one nobody watches. A mis-wired fork that quietly drops a branch
still reports success, and the run's trace shows a happy path that never
happened. So the tests here are weighted towards the *refusals* — a graph that
cannot work is rejected at save time rather than half-executed at run time.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.flow.executor import FlowExecutor
from src.flow.registry import get_node_registry
from src.flow.schema import Flow, validate_flow

REG = get_node_registry()


def _var(node_id, name, value):
    return {"id": node_id, "type": "set_var", "params": {"name": name, "value": value}}


def _fork_graph(**fork_params):
    params = {"mode": "sequential", "on_error": "fail", **fork_params}
    return {
        "version": 1,
        "entry": "fork",
        "nodes": [
            {"id": "fork", "type": "parallel", "params": params},
            _var("a1", "a", "A"),
            _var("b1", "b", "B"),
            _var("c1", "c", "C"),
            {"id": "barrier", "type": "join", "params": {"label": "all"}},
            _var("after", "done", "yes"),
        ],
        "edges": [
            {"id": "e1", "source": "fork", "source_port": "b1", "target": "a1"},
            {"id": "e2", "source": "fork", "source_port": "b2", "target": "b1"},
            {"id": "e3", "source": "fork", "source_port": "b3", "target": "c1"},
            {"id": "e4", "source": "a1", "source_port": "ok", "target": "barrier"},
            {"id": "e5", "source": "b1", "source_port": "ok", "target": "barrier"},
            {"id": "e6", "source": "c1", "source_port": "ok", "target": "barrier"},
            {"id": "e7", "source": "barrier", "source_port": "ok", "target": "after"},
        ],
    }


def _run(graph, spans=None):
    executor = FlowExecutor(REG)
    if spans is not None:
        executor.span_hook = lambda s: spans.append((s.node_id, s.status))
    return executor.run(Flow(id="f", name="f", graph=graph), "r1")


def _codes(graph):
    return {(i.severity, i.code) for i in validate_flow(graph, REG.types())}


class TestFanOutFanIn:
    def test_three_branches_all_run_and_the_join_fires_once(self):
        spans: list[tuple[str, str]] = []
        result = _run(_fork_graph(), spans)
        assert result.status == "ok", result.error
        assert (result.scope["a"], result.scope["b"], result.scope["c"]) == ("A", "B", "C")
        # The node after the join is the whole point: a fork that executes its
        # branches and then ends the run looks identical to a working one until
        # you notice what did not run.
        assert result.scope["done"] == "yes"

        # The fork's own `ok` closes after the join: one span covering the whole
        # fan-out, which is the only way a trace reader can see how long the
        # fork took rather than inferring it from the gap.
        finished = [n for n, st in spans if st != "running"]
        assert finished == ["a1", "b1", "c1", "barrier", "fork", "after"]

    def test_the_join_runs_once_not_once_per_branch(self):
        spans: list[tuple[str, str]] = []
        _run(_fork_graph(), spans)
        assert sum(1 for n, st in spans if n == "barrier" and st == "ok") == 1

    def test_the_fork_gets_its_own_span(self):
        spans: list[tuple[str, str]] = []
        _run(_fork_graph(), spans)
        # The fork's handler refuses to run, so an executor that did not emit
        # its own span would leave the trace with no sign of it.
        assert ("fork", "running") in spans
        assert ("fork", "ok") in spans

    def test_fork_and_join_bookkeeping_reaches_the_scope(self):
        result = _run(_fork_graph())
        assert result.scope["fork"]["branches"] == 3
        assert result.scope["fork"]["failed_branches"] == 0
        assert result.scope["barrier"]["branches"] == 3

    def test_branches_share_one_scope(self):
        """The reason to join at all: B reads what A wrote."""
        graph = _fork_graph()
        graph["nodes"].append({
            "id": "sum", "type": "llm",
            "params": {"prompt": "unused", "provider": "missing-provider"},
        })
        # Replace the second branch with a read of the first branch's output.
        graph["nodes"][2] = {
            "id": "b1", "type": "set_var",
            "params": {"name": "b", "expression": "'got:' + a"},
        }
        result = _run(graph)
        assert result.status == "ok", result.error
        assert result.scope["b"] == "got:A"

    def test_a_disabled_branch_is_skipped_and_still_joins(self):
        graph = _fork_graph()
        graph["nodes"][2]["disabled"] = True
        result = _run(graph)
        assert result.status == "ok", result.error
        assert result.scope.get("b") is None
        assert result.scope["a"] == "A"
        assert result.scope["done"] == "yes"

    def test_nested_forks_each_join_at_their_own_barrier(self):
        graph = {
            "version": 1,
            "entry": "outer",
            "nodes": [
                {"id": "outer", "type": "parallel", "params": {"mode": "sequential"}},
                {
                    "id": "inner", "type": "parallel", "params": {"mode": "sequential"},
                },
                _var("i1", "i", "I"),
                _var("i2", "inner_b", "I2"),
                {"id": "ibar", "type": "join", "params": {}},
                _var("s1", "s", "S"),
                {"id": "obar", "type": "join", "params": {}},
                _var("last", "finished", "yes"),
            ],
            "edges": [
                {"id": "x1", "source": "outer", "source_port": "b1", "target": "inner"},
                {"id": "x2", "source": "outer", "source_port": "b2", "target": "s1"},
                {"id": "x3", "source": "inner", "source_port": "b1", "target": "i1"},
                {"id": "x4", "source": "inner", "source_port": "b2", "target": "i2"},
                {"id": "x5", "source": "i1", "source_port": "ok", "target": "ibar"},
                {"id": "x6", "source": "i2", "source_port": "ok", "target": "ibar"},
                {"id": "x7", "source": "ibar", "source_port": "ok", "target": "obar"},
                {"id": "x8", "source": "s1", "source_port": "ok", "target": "obar"},
                {"id": "x9", "source": "obar", "source_port": "ok", "target": "last"},
            ],
        }
        assert _codes(graph) == set()
        result = _run(graph)
        assert result.status == "ok", result.error
        assert result.scope["i"] == "I"
        assert result.scope["inner_b"] == "I2"
        assert result.scope["s"] == "S"
        assert result.scope["finished"] == "yes"

    def test_threaded_mode_runs_every_branch(self):
        """``mode: parallel`` on branches that do not touch the screen."""
        graph = _fork_graph(mode="parallel")
        # A loop, not a plain append: CPython would raise here and the whole
        # point of the threaded mode is that the branches really are concurrent.
        seen: list[str] = []
        guard = threading.Lock()

        class _Probe:
            def __init__(self, node_id):
                self.node_id = node_id

            def execute(self):
                with guard:
                    seen.append(self.node_id)
                return {"value": self.node_id}

        from src.flow import builtin_nodes, control_nodes, system_nodes
        from src.flow.registry import NodeRegistry, NodeSpec

        registry = NodeRegistry()
        builtin_nodes.register_all(registry)
        control_nodes.register_all(registry)
        system_nodes.register_all(registry)
        for node in graph["nodes"]:
            if node["id"] in ("a1", "b1", "c1"):
                registry.register(NodeSpec(
                    type="probe", label="探针", category="x",
                    # A class attribute per type, not a closure over a loop
                    # variable: one handler class reused for all three would
                    # report whichever id the loop ended on.
                    handler=type("Probe" + node["id"], (_Probe,), {"node_id": node["id"]}),
                ))
                node["type"] = "probe"
        result = FlowExecutor(registry).run(Flow(id="f", name="f", graph=graph), "r1")
        assert result.status == "ok", result.error
        assert sorted(seen) == ["a1", "b1", "c1"]
        assert result.scope["done"] == "yes"


class TestFailures:
    def test_a_failing_branch_stops_the_run_by_default(self):
        graph = _fork_graph()
        graph["nodes"][2] = {
            "id": "b1", "type": "llm",
            "params": {"prompt": "x", "provider": "does-not-exist"},
        }
        result = _run(graph)
        assert result.status == "error"
        assert "b1" in result.error

    def test_on_error_continue_lets_the_others_finish(self):
        graph = _fork_graph(on_error="continue")
        graph["nodes"][2] = {
            "id": "b1", "type": "llm",
            "params": {"prompt": "x", "provider": "does-not-exist"},
        }
        result = _run(graph)
        assert result.status == "ok", result.error
        assert result.scope["a"] == "A"
        assert result.scope["c"] == "C"
        assert result.scope["done"] == "yes"
        assert result.scope["fork"]["failed_branches"] == 1

    def test_a_branch_that_never_reaches_the_join_is_refused_at_runtime(self):
        graph = _fork_graph()
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e6"]
        result = _run(graph)
        assert result.status == "error"
        assert "join" in result.error

    def test_the_fork_span_records_the_error(self):
        graph = _fork_graph()
        graph["nodes"][2] = {
            "id": "b1", "type": "llm",
            "params": {"prompt": "x", "provider": "does-not-exist"},
        }
        spans: list[tuple[str, str]] = []
        _run(graph, spans)
        fork = [st for n, st in spans if n == "fork"]
        assert "running" in fork and "error" in fork

    def test_a_branch_that_never_reaches_the_join_is_caught_at_save_time(self):
        graph = _fork_graph()
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e6"]
        # Saved as an error, so the executor refuses before the fork runs —
        # which is the point: refusing at save time beats half-running.
        assert ("error", "branch_no_join") in _codes(graph)
        result = _run(graph)
        assert result.status == "error"
        assert "branch_no_join" in (result.error or "")


class TestValidation:
    def test_a_correct_graph_is_clean(self):
        assert _codes(_fork_graph()) == set()

    def test_two_unconditional_edges_from_one_port_is_an_error(self):
        """The silent one. The executor takes the first matching edge and the
        second can never run — and this used to validate clean."""
        graph = {
            "version": 1, "entry": "n1",
            "nodes": [_var("n1", "a", "1"), _var("n2", "b", "2"), _var("n3", "c", "3")],
            "edges": [
                {"id": "e1", "source": "n1", "source_port": "ok", "target": "n2"},
                {"id": "e2", "source": "n1", "source_port": "ok", "target": "n3"},
            ],
        }
        codes = _codes(graph)
        assert ("error", "duplicate_unconditional_edge") in codes
        issue = next(i for i in validate_flow(graph, REG.types())
                     if i.code == "duplicate_unconditional_edge")
        # It has to name the nodes, or the author cannot find the second edge.
        assert "n2" in issue.message and "n3" in issue.message
        assert "parallel" in issue.message

    def test_conditional_edges_from_one_port_are_fine(self):
        graph = {
            "version": 1, "entry": "n1",
            "nodes": [_var("n1", "a", "1"), _var("n2", "b", "2"), _var("n3", "c", "3")],
            "edges": [
                {"id": "e1", "source": "n1", "source_port": "ok", "target": "n2",
                 "condition": "a == 1"},
                {"id": "e2", "source": "n1", "source_port": "ok", "target": "n3"},
            ],
        }
        assert ("error", "duplicate_unconditional_edge") not in _codes(graph)

    def test_two_branches_on_different_ports_are_not_flagged(self):
        """A fork is several *different* ports, which is the whole distinction."""
        assert _codes(_fork_graph()) == set()

    def test_a_lone_branch_is_refused(self):
        graph = {
            "version": 1, "entry": "fork",
            "nodes": [
                {"id": "fork", "type": "parallel", "params": {}},
                _var("a1", "a", "A"),
                {"id": "barrier", "type": "join", "params": {}},
            ],
            "edges": [
                {"id": "e1", "source": "fork", "source_port": "b1", "target": "a1"},
                {"id": "e2", "source": "a1", "source_port": "ok", "target": "barrier"},
            ],
        }
        assert ("error", "parallel_needs_two") in _codes(graph)

    def test_a_branch_with_no_join_is_refused(self):
        graph = _fork_graph()
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e4"]
        assert ("error", "branch_no_join") in _codes(graph)

    def test_branches_converging_on_different_joins_are_refused(self):
        """Two of the three meet at one barrier, the third at another."""
        graph = _fork_graph()
        graph["nodes"].append({"id": "other", "type": "join", "params": {}})
        graph["nodes"].append(_var("b2", "b2", "B2"))
        graph["edges"].append(
            {"id": "e8", "source": "b2", "source_port": "ok", "target": "other"})
        graph["edges"].append(
            {"id": "e9", "source": "b1", "source_port": "ok", "target": "b2"})
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e5"]
        assert ("error", "branches_diverge") in _codes(graph)

    def test_one_branch_reaching_two_joins_is_refused(self):
        """A branch that can arrive at either barrier has no single place to
        wait, so the fork has nothing to join on."""
        graph = _fork_graph()
        graph["nodes"].append({"id": "other", "type": "join", "params": {}})
        graph["nodes"].append(_var("b2", "b2", "B2"))
        graph["edges"].append(
            {"id": "e8", "source": "b2", "source_port": "ok", "target": "other"})
        graph["edges"].append(
            {"id": "e9", "source": "b1", "source_port": "ok", "target": "b2"})
        codes = _codes(graph)
        assert ("error", "branch_ambiguous_join") in codes
        issue = next(i for i in validate_flow(graph, REG.types())
                     if i.code == "branch_ambiguous_join")
        assert "barrier" in issue.message and "other" in issue.message

    def test_a_join_with_no_parallel_upstream_is_refused(self):
        """It would otherwise run as an ordinary node: the flow reports success
        and the author believes they wrote a barrier."""
        graph = {
            "version": 1, "entry": "n1",
            "nodes": [_var("n1", "a", "1"), {"id": "j", "type": "join", "params": {}},
                      _var("n2", "b", "2")],
            "edges": [
                {"id": "e1", "source": "n1", "source_port": "ok", "target": "j"},
                {"id": "e2", "source": "j", "source_port": "ok", "target": "n2"},
            ],
        }
        assert ("error", "join_without_parallel") in _codes(graph)

    def test_a_join_wired_straight_off_a_branch_port_is_refused(self):
        graph = _fork_graph()
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e1"]
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "a1"]
        graph["edges"] = [e for e in graph["edges"] if e["id"] != "e4"]
        graph["edges"].append(
            {"id": "e9", "source": "fork", "source_port": "b1", "target": "barrier"})
        assert ("error", "join_direct_from_parallel") in _codes(graph)

    def test_a_lonely_join_is_refused(self):
        graph = _fork_graph()
        graph["edges"] = [e for e in graph["edges"]
                          if e["id"] not in ("e4", "e5", "e6")]
        assert ("error", "join_no_incoming") in _codes(graph)

    def test_threads_plus_a_screen_touching_branch_is_refused(self):
        """Two threads driving one mouse do not interleave, and the loser
        reports a click that landed on whatever the winner drew."""
        graph = _fork_graph(mode="parallel")
        graph["nodes"][1] = {"id": "a1", "type": "click", "params": {"x": 10, "y": 10}}
        codes = _codes(graph)
        assert ("error", "threaded_ui_branch") in codes
        issue = next(i for i in validate_flow(graph, REG.types())
                     if i.code == "threaded_ui_branch")
        assert "click" in issue.message

    def test_threads_with_only_pure_branches_are_allowed(self):
        graph = _fork_graph(mode="parallel")
        assert _codes(graph) == set()

    def test_sequential_ui_branches_are_allowed(self):
        graph = _fork_graph(mode="sequential")
        graph["nodes"][1] = {
            "id": "a1", "type": "click", "path": "element",
            "params": {"x": 10, "y": 10},
        }
        assert not [c for sev, c in _codes(graph) if sev == "error"]

    def test_the_two_seed_flows_still_validate(self):
        from src.flow.store import get_store

        for row in get_store().list_flows():
            codes = _codes(row.get("graph") or {})
            assert ("error", "duplicate_unconditional_edge") not in codes, row.get("id")
            assert not [c for s, c in codes if s == "error" and c.startswith(
                ("parallel_", "branch_", "branches_", "join_", "threaded_"))], row.get("id")
