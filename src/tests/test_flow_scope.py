"""Scope layering and variable-ambiguity reporting.

The behaviour under test is not "does it run" but "does it fail loudly when a
bare ``{{name}}`` could mean two different things". A flat scope that silently
picks the last writer is the failure mode this file exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.flow.context import FlowScope
from src.flow.expr import interpolate
from src.flow.schema import Flow, validate_flow


def _graph(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "entry": nodes[0]["id"],
        "default_path": "auto",
        "variables": {},
        "nodes": nodes,
        "edges": edges or [],
    }


def _ambiguities(graph: dict) -> list:
    return [i for i in validate_flow(graph) if i.code == "ambiguous_variable"]


class TestScopeLayers:
    def test_loop_layer_shadows_global(self):
        scope = FlowScope({"item": "from-node"})
        scope.bind_loop("item", "from-loop")
        assert scope.get("item") == "from-loop"

    def test_global_visible_when_no_loop_binding(self):
        scope = FlowScope({"item": "from-node"})
        assert scope.get("item") == "from-node"

    def test_local_layer_sits_between_loop_and_global(self):
        scope = FlowScope({"v": "global"})
        scope.bind_local("v", "local")
        assert scope.get("v") == "local"
        scope.bind_loop("v", "loop")
        assert scope.get("v") == "loop"

    def test_node_scoped_addressing_disambiguates(self):
        scope = FlowScope()
        scope.bind_node("n1", {"messages": ["a"]})
        scope.bind_node("n2", {"messages": ["b"]})
        assert scope.get("n1.messages") == ["a"]
        assert scope.get("n2.messages") == ["b"]

    def test_flat_binding_is_last_writer_wins(self):
        """The behaviour the ambiguity warning exists to make visible."""
        scope = FlowScope()
        scope.bind("messages", ["first"])
        scope.bind("messages", ["second"])
        assert scope.get("messages") == ["second"]

    def test_clear_loop_restores_global_visibility(self):
        scope = FlowScope({"item": "global"})
        scope.bind_loop("item", "loop")
        scope.clear_loop()
        assert scope.get("item") == "global"

    def test_pop_loop_restores_enclosing_loop(self):
        """A nested loop must not destroy the outer loop's bindings."""
        scope = FlowScope()
        scope.push_loop({"item": "outer"})
        scope.push_loop({"item": "inner"})
        assert scope.get("item") == "inner"
        scope.pop_loop()
        assert scope.get("item") == "outer"
        scope.pop_loop()
        assert not scope.has("item")

    def test_unbalanced_pop_is_a_noop(self):
        scope = FlowScope()
        scope.push_loop({"item": "v"})
        scope.pop_loop()
        scope.pop_loop()  # must not raise
        assert not scope.has("item")

    def test_snapshot_precedence_matches_get(self):
        scope = FlowScope({"v": "global"})
        scope.bind_local("v", "local")
        scope.bind_loop("v", "loop")
        assert scope.snapshot()["v"] == scope.get("v") == "loop"

    def test_export_includes_node_results(self):
        """The run result is a contract; node ids must survive it."""
        scope = FlowScope()
        scope.bind_node("n1", {"messages": ["a"]})
        scope.bind("chat_name", "alex")
        exported = scope.export()
        assert exported["n1"] == {"messages": ["a"]}
        assert exported["chat_name"] == "alex"

    def test_interpolation_reads_node_scoped_reference(self):
        scope = FlowScope()
        scope.bind_node("n_perceive", {"messages": [{"text": "hi"}]})
        assert interpolate("{{n_perceive.messages[0].text}}", scope) == "hi"


class TestAmbiguityWarning:
    def test_warns_when_two_nodes_produce_the_same_name(self):
        graph = _graph(
            [
                {"id": "n1", "type": "perceive", "name": "p", "params": {},
                 "outputs": ["chat_name", "messages"]},
                {"id": "n2", "type": "get_unreplied", "name": "u", "params": {},
                 "outputs": ["messages", "count"]},
                {"id": "n3", "type": "log", "name": "l",
                 "params": {"message": "内容 {{messages}}"}, "outputs": []},
            ],
            [{"id": "e1", "source": "n1", "target": "n2"},
             {"id": "e2", "source": "n2", "target": "n3"}],
        )
        found = _ambiguities(graph)
        assert len(found) == 1
        assert found[0].severity == "warning"
        # Must name every candidate, or the fix is guesswork.
        assert "n1" in found[0].message and "n2" in found[0].message
        assert "n3" in found[0].message  # the consumer

    def test_silent_when_qualified_reference_is_used(self):
        graph = _graph(
            [
                {"id": "n1", "type": "perceive", "name": "p", "params": {},
                 "outputs": ["messages"]},
                {"id": "n2", "type": "get_unreplied", "name": "u", "params": {},
                 "outputs": ["messages"]},
                {"id": "n3", "type": "log", "name": "l",
                 "params": {"message": "内容 {{n1.messages}}"}, "outputs": []},
            ],
            [{"id": "e1", "source": "n1", "target": "n2"},
             {"id": "e2", "source": "n2", "target": "n3"}],
        )
        assert _ambiguities(graph) == []

    def test_silent_for_single_producer(self):
        """One producer is legitimate; forcing qualification would be noise."""
        graph = _graph(
            [
                {"id": "n1", "type": "perceive", "name": "p", "params": {},
                 "outputs": ["messages"]},
                {"id": "n2", "type": "log", "name": "l",
                 "params": {"message": "{{messages}}"}, "outputs": []},
            ],
            [{"id": "e1", "source": "n1", "target": "n2"}],
        )
        assert _ambiguities(graph) == []

    def test_edge_condition_reference_is_scanned(self):
        graph = _graph(
            [
                {"id": "n1", "type": "perceive", "name": "p", "params": {},
                 "outputs": ["messages"]},
                {"id": "n2", "type": "get_unreplied", "name": "u", "params": {},
                 "outputs": ["messages"]},
                {"id": "n3", "type": "log", "name": "l", "params": {}, "outputs": []},
            ],
            [{"id": "e1", "source": "n1", "target": "n2"},
             {"id": "e2", "source": "n2", "target": "n3", "condition": "messages.count > 0"}],
        )
        # `messages.count` is an attribute read on a resolved value, not an
        # ambiguous reference, so it must not warn.
        assert _ambiguities(graph) == []

    def test_set_var_and_foreach_are_excluded_as_producers(self):
        """They bind dynamically on purpose; flagging them would be noise."""
        graph = _graph(
            [
                {"id": "n1", "type": "start", "name": "s", "params": {},
                 "outputs": ["item"]},
                {"id": "n2", "type": "foreach", "name": "f", "params": {},
                 "outputs": ["item"]},
                {"id": "n3", "type": "log", "name": "l",
                 "params": {"message": "{{item}}"}, "outputs": []},
            ],
            [{"id": "e1", "source": "n1", "target": "n2"},
             {"id": "e2", "source": "n2", "target": "n3"}],
        )
        assert _ambiguities(graph) == []

    def test_expression_with_dotted_head_is_not_flagged(self):
        graph = _graph(
            [
                {"id": "n1", "type": "perceive", "name": "p", "params": {},
                 "outputs": ["messages"]},
                {"id": "n2", "type": "get_unreplied", "name": "u", "params": {},
                 "outputs": ["messages"]},
                {"id": "n3", "type": "log", "name": "l",
                 "params": {"message": "{{messages.count + 1}}"}, "outputs": []},
            ],
            [{"id": "e1", "source": "n1", "target": "n2"},
             {"id": "e2", "source": "n2", "target": "n3"}],
        )
        # `messages.count` reads an attribute off an already-resolved value; it
        # is not a bare reference, so there is no ambiguity to report. Flagging
        # every dotted read would bury the real warnings.
        assert _ambiguities(graph) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

class TestLLMNode:
    """The generic llm node — parameter shaping, not model behaviour.

    Every test patches the client, so none of them touch the network. What is
    being pinned is the node's contract: what it sends, and what it reports
    back when the model returns nothing.
    """

    @staticmethod
    def _flow(params: dict) -> "Flow":
        return Flow(
            id="flow_llm",
            name="llm",
            graph={
                "version": 1,
                "entry": "n1",
                "variables": {},
                "nodes": [{
                    "id": "n1", "type": "llm", "name": "llm",
                    "position": {"x": 0, "y": 0},
                    "params": params,
                    "retry": {"max": 0, "delay": 0},
                    "timeout": None, "on_error": "fail",
                    "outputs": ["text", "empty", "model"],
                    "disabled": False, "path": None, "target": None,
                }],
                "edges": [],
            },
        )

    @staticmethod
    def _run(params: dict, reply: str = "hello", variables: dict | None = None):
        import src.llm.openclaw_client as oc
        from src.flow.executor import FlowExecutor

        seen: dict = {}

        class FakeClient:
            model = "fake-model"

            def chat(self, messages, temperature=None, max_tokens=None, timeout=None):
                seen.update(messages=messages, temperature=temperature,
                            max_tokens=max_tokens, timeout=timeout)
                return reply

        original = oc.OpenClawClient
        oc.OpenClawClient = lambda **kw: (seen.setdefault("init", kw), FakeClient())[1]
        try:
            result = FlowExecutor().run(TestLLMNode._flow(params), "run_llm", variables=variables)
        finally:
            oc.OpenClawClient = original
        return result, seen

    def test_prompt_reaches_the_model(self):
        result, seen = self._run({"prompt": "总结这段话"})
        assert result.status == "ok", result.error
        assert seen["messages"][-1] == {"role": "user", "content": "总结这段话"}

    def test_system_prompt_is_prepended(self):
        result, seen = self._run({"prompt": "p", "system": "你是翻译"})
        assert result.status == "ok", result.error
        assert seen["messages"][0] == {"role": "system", "content": "你是翻译"}

    def test_blank_system_prompt_is_omitted(self):
        """An empty system turn changes provider behaviour; it must not be sent."""
        result, seen = self._run({"prompt": "p", "system": "  "})
        assert result.status == "ok", result.error
        assert all(m["role"] == "user" for m in seen["messages"])

    def test_temperature_omitted_when_unset(self):
        result, seen = self._run({"prompt": "p"})
        assert result.status == "ok", result.error
        assert seen["temperature"] is None

    def test_temperature_passes_through_when_set(self):
        result, seen = self._run({"prompt": "p", "temperature": 0.2})
        assert result.status == "ok", result.error
        assert seen["temperature"] == 0.2

    def test_prompt_interpolates_from_scope(self):
        result, seen = self._run(
            {"prompt": "总结：{{n_perceive.text}}"},
            variables={"n_perceive": {"text": "你好"}},
        )
        assert result.status == "ok", result.error
        assert seen["messages"][-1]["content"] == "总结：你好"

    def test_empty_reply_sets_empty_flag(self):
        result, _ = self._run({"prompt": "p"}, reply="   ")
        assert result.status == "ok", result.error
        assert result.scope["n1"]["empty"] is True
        assert result.scope["n1"]["text"] == "   "

    def test_missing_prompt_is_an_error(self):
        result, _ = self._run({"prompt": "  "})
        assert result.status == "error"


class TestTemplateNode:
    """Jinja2 rendering, including the ways it must refuse to work."""

    @staticmethod
    def _run(template: str, variables: dict | None = None):
        from src.flow.executor import FlowExecutor

        flow = Flow(
            id="flow_tpl", name="tpl",
            graph={
                "version": 1, "entry": "n1", "variables": {},
                "nodes": [{
                    "id": "n1", "type": "template", "name": "tpl",
                    "position": {"x": 0, "y": 0},
                    "params": {"template": template},
                    "retry": {"max": 0, "delay": 0}, "timeout": None,
                    "on_error": "fail",
                    "outputs": ["text", "empty", "length"],
                    "disabled": False, "path": None, "target": None,
                }],
                "edges": [],
            },
        )
        return FlowExecutor().run(flow, "run_tpl", variables=variables or {})

    def test_simple_interpolation(self):
        r = self._run("你好 {{name}}", {"name": "alex"})
        assert r.status == "ok", r.error
        assert r.scope["n1"]["text"] == "你好 alex"

    def test_loop_over_a_list(self):
        r = self._run(
            "{% for i in items %}[{{ i }}]{% endfor %}",
            {"items": ["a", "b", "c"]},
        )
        assert r.status == "ok", r.error
        assert r.scope["n1"]["text"] == "[a][b][c]"

    def test_conditional(self):
        r = self._run("{% if ok %}Y{% else %}N{% endif %}", {"ok": True})
        assert r.status == "ok", r.error
        assert r.scope["n1"]["text"] == "Y"

    def test_filter(self):
        r = self._run("{{ name | upper }}", {"name": "alex"})
        assert r.status == "ok", r.error
        assert r.scope["n1"]["text"] == "ALEX"

    def test_missing_variable_is_an_error(self):
        """StrictUndefined: a blank where a name should be is a wrong message."""
        r = self._run("你好 {{nope}}")
        assert r.status == "error"
        assert "nope" in r.error

    def test_sandbox_blocks_attribute_escape(self):
        """The scope must not be walkable from inside a template."""
        r = self._run("{{ ''.__class__.__mro__ }}")
        assert r.status == "error"

    def test_empty_template_is_an_error(self):
        r = self._run("   ")
        assert r.status == "error"

    def test_blank_render_sets_empty(self):
        r = self._run("{% if false %}x{% endif %}")
        assert r.status == "ok", r.error
        assert r.scope["n1"]["empty"] is True
