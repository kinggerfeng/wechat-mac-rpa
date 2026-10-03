"""The ``code`` node as the executor sees it.

:mod:`apps.engine.code_node` tests the sandbox in isolation. These tests cover the
seam the executor adds: scope exposure, how a failure is reported, and the fact
that a refused snippet never leaves partial side effects behind.

The design decision they lock in: **a snippet that fails fails the node.** It does
not branch. A ``code`` node exists to produce a value, so a failure with no error
edge wired would end the run with nothing bound and the flow reporting success —
the silent-wrong-result bug this project keeps paying for. ``try``/``catch`` is
how a flow says what to do instead.
"""

from __future__ import annotations

from pathlib import Path


import pytest


from apps.engine.executor import FlowExecutor  # noqa: E402
from apps.engine.registry import get_node_registry  # noqa: E402
from apps.engine.schema import Flow, NodeError, validate_flow  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _flow(nodes, edges, entry="a", variables=None):
    return Flow(
        id="flow_code",
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
    return FlowExecutor().run(_flow(nodes, edges), "run_code", **kw)


# ── registration ───────────────────────────────────────────────────────────

def test_the_code_node_is_registered_with_its_declared_ports():
    spec = get_node_registry().get("code")
    assert spec is not None
    assert spec.category == "代码"
    assert "value" in spec.outputs
    assert "ok" in spec.all_ports()
    # No `error` port: the node raises rather than branching, so a port that
    # could never fire would be a lie the canvas draws.
    assert "error" not in spec.all_ports()


def test_the_code_param_is_declared_raw_so_braces_survive():
    spec = get_node_registry().get("code")
    code_param = next(p for p in spec.params if p.name == "code")
    assert code_param.required is True
    assert code_param.raw is True


def test_a_well_formed_code_node_validates():
    issues = validate_flow(
        {
            "version": 1,
            "entry": "a",
            "nodes": [_n("a", "code", params={"code": "output = 1"})],
            "edges": [],
        },
        get_node_registry().types(),
    )
    assert [i for i in issues if i.severity == "error"] == []


# ── the happy path ─────────────────────────────────────────────────────────

def test_a_snippet_binds_its_result_through_the_executor():
    result = _run(
        [_n("a", "code", params={"code": "output = 6 * 7"})],
        [],
    )
    assert result.status == "ok"
    assert result.scope["a"]["value"] == 42
    assert result.scope["a"]["ok"] is True


def test_main_is_called_and_its_return_value_binds():
    result = _run(
        [_n("a", "code", params={"code": "def main():\n    return [1, 2, 3]"})],
        [],
    )
    assert result.scope["a"]["value"] == [1, 2, 3]
    assert result.scope["a"]["value_from"] == "main"


def test_stdout_reaches_the_trace():
    result = _run(
        [_n("a", "code", params={"code": "print('from the snippet')\noutput = 1"})],
        [],
    )
    assert "from the snippet" in result.scope["a"]["stdout"]


def test_a_snippet_sees_an_upstream_node_output_by_dotted_name():
    result = _run(
        [
            _n("a", "set_var", params={"name": "seed", "value": "hello"}),
            _n("b", "code", params={"code": "output = seed.upper()"}),
        ],
        [_e("e1", "a", "b")],
    )
    assert result.scope["b"]["value"] == "HELLO"


# ── scope exposure ─────────────────────────────────────────────────────────

def test_a_blank_inputs_list_exposes_the_whole_top_level_scope():
    result = _run(
        [
            _n("a", "set_var", params={"name": "keep", "value": "yes"}),
            _n("b", "set_var", params={"name": "hide", "value": "no"}),
            _n("c", "code", params={"code": "output = keep + hide"}),
        ],
        [_e("e1", "a", "b"), _e("e2", "b", "c")],
    )
    assert result.scope["c"]["value"] == "yesno"


def test_an_explicit_inputs_list_narrows_what_the_snippet_sees():
    result = _run(
        [
            _n("a", "set_var", params={"name": "keep", "value": "yes"}),
            _n("b", "set_var", params={"name": "hide", "value": "no"}),
            # `hide` was never injected, so the snippet must fail on it rather
            # than quietly succeed on a name it was not given.
            _n("c", "code", params={"code": "output = keep + hide", "inputs": "keep"}),
        ],
        [_e("e1", "a", "b"), _e("e2", "b", "c")],
    )
    assert result.status == "error"
    assert "NameError" in (result.error or "")


def test_a_name_outside_the_scope_is_reported_by_name():
    result = _run(
        [_n("a", "code", params={"code": "output = 1", "inputs": "nonexistent"})],
        [],
    )
    assert result.status == "error"
    assert "nonexistent" in (result.error or "")


# ── refusals fail the run, loudly ──────────────────────────────────────────

def test_a_forbidden_construct_fails_the_run_with_the_line():
    result = _run(
        [_n("a", "code", params={"code": "x = 1\nimport os\noutput = x"})],
        [],
    )
    assert result.status == "error"
    assert "沙箱" in (result.error or "")
    assert "import" in (result.error or "")
    assert "第 2 行" in (result.error or "")


def test_a_refusal_never_runs_the_lines_above_it():
    # The point of vetting before executing: line 1 must not have happened.
    probe = "code_node_pwn_probe.txt"
    result = _run(
        [_n("a", "code", params={
            "code": f"fs.write_text({probe!r}, 'x')\nimport os",
            "writable": True,
        })],
        [],
    )
    assert result.status == "error"
    assert not (PROJECT_ROOT / probe).exists()


def test_a_missing_open_fails_rather_than_binding_an_empty_string():
    result = _run(
        [_n("a", "code", params={"code": "output = open('/etc/hostname').read()"})],
        [],
    )
    assert result.status == "error"
    assert result.scope.get("a") is None


def test_a_class_walk_fails():
    result = _run(
        [_n("a", "code", params={"code": "output = [].__class__.__base__"})],
        [],
    )
    assert result.status == "error"
    assert "下划线" in (result.error or "")


def test_a_dunder_string_literal_fails():
    result = _run(
        [_n("a", "code", params={"code": "output = getattr(1, '__class__')"})],
        [],
    )
    assert result.status == "error"
    assert "字符串常量" in (result.error or "")


# ── runtime failures fail the node, loudly ─────────────────────────────────

def test_a_snippet_that_raises_fails_the_run():
    result = _run(
        [_n("a", "code", params={"code": "output = int('nope')"})],
        [],
    )
    assert result.status == "error"
    assert "ValueError" in (result.error or "")


def test_the_failure_message_carries_the_captured_stdout():
    # The snippet already printed its input; making the author go back and add a
    # print they wrote is the kind of round trip this message exists to avoid.
    result = _run(
        [_n("a", "code", params={"code": "print('context: xyz')\noutput = 1 / 0"})],
        [],
    )
    assert result.status == "error"
    assert "context: xyz" in (result.error or "")
    assert "ZeroDivisionError" in (result.error or "")


def test_a_timeout_fails_the_run_and_says_so():
    result = _run(
        [_n("a", "code", params={"code": "while True:\n    pass", "timeout": 0.4})],
        [],
    )
    assert result.status == "error"
    assert "timeout" in (result.error or "").lower()
    assert "执行超时" in (result.error or "")


def test_a_path_escape_inside_the_snippet_fails_the_node():
    result = _run(
        [_n("a", "code", params={"code": "output = fs.read_text('/etc/hostname')"})],
        [],
    )
    assert result.status == "error"
    assert "根目录" in (result.error or "") or "沙箱" in (result.error or "")


def test_writable_off_blocks_fs_writes():
    probe = "code_node_write_probe.txt"
    result = _run(
        [_n("a", "code", params={"code": f"fs.write_text({probe!r}, 'x')"})],
        [],
    )
    assert result.status == "error"
    assert not (PROJECT_ROOT / probe).exists()


def test_writable_on_lets_fs_write_inside_the_root():
    probe = "code_node_write_ok.txt"
    try:
        result = _run(
            [_n("a", "code", params={
                "code": f"output = fs.write_text({probe!r}, 'ok')",
                "writable": True,
            })],
            [],
        )
        assert result.status == "ok"
        assert (PROJECT_ROOT / probe).read_text(encoding="utf-8") == "ok"
    finally:
        (PROJECT_ROOT / probe).unlink(missing_ok=True)


# ── try/catch is the way to carry on ───────────────────────────────────────

def test_try_catch_handles_a_failing_snippet():
    # This is the whole reason the node raises instead of carrying its own error
    # port: `catch` can name what the flow does when the value is missing, and
    # the region keeps that decision visible in the graph.
    result = _run(
        [
            _n("a", "try", params={"label": "parse"}),
            _n("b", "code", on_error="branch", outputs=["ok", "error"],
               params={"code": "output = int('nope')"}),
            _n("c", "catch", params={"label": "parse", "action": "continue"}),
            _n("d", "set_var", params={"name": "survived", "value": True}),
        ],
        [
            _e("e1", "a", "b", "body"),
            _e("e2", "b", "c", "ok"),
            _e("e3", "b", "c", "error"),
            _e("e4", "c", "d", "exit"),
        ],
    )
    assert result.status == "ok", result.error
    assert result.scope.get("survived") is True


def test_a_snippet_failure_inside_a_catch_region_keeps_the_reason_reachable():
    result = _run(
        [
            _n("a", "try", params={"label": "parse"}),
            _n("b", "code", outputs=["ok", "error"], params={"code": "output = 1 / 0"}),
            _n("c", "catch", params={"label": "parse", "action": "continue"}),
        ],
        [
            _e("e1", "a", "b", "body"),
            _e("e2", "b", "c", "ok"),
            _e("e3", "b", "c", "error"),
        ],
    )
    assert result.status == "ok", result.error
    # The body kept the canvas default `on_error: fail`. A region used to be
    # inert in exactly this configuration: the failure ended the run and `catch`
    # was never reached, so a graph that reads as protected was not.
    assert result.scope["c"]["caught"] is True
    assert result.scope["c"]["error_node"] == "b"
    # The innermost type, not the NodeError wrapper the node is obliged to raise.
    assert result.scope["c"]["error_type"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in result.scope["parse_error"]


def test_a_region_whose_body_succeeds_reports_that_it_caught_nothing():
    # The mirror image, and the one that would have been wrong if the binding
    # were never cleared: a handler that reports `caught` for a body that ran
    # fine sends the flow down the wrong branch.
    result = _run(
        [
            _n("a", "try", params={"label": "parse"}),
            _n("b", "code", params={"code": "output = 42"}),
            _n("c", "catch", params={"label": "parse", "action": "continue"}),
        ],
        [_e("e1", "a", "b", "body"), _e("e2", "b", "c", "ok")],
    )
    assert result.status == "ok", result.error
    assert result.scope["c"]["caught"] is False
    assert result.scope["c"]["error"] == ""
    assert result.scope["parse_error"] == ""


def test_a_region_re_entered_through_a_loop_does_not_report_a_stale_failure():
    # A region inside a loop runs more than once. Without the reset on the way
    # in, the second pass would find the first pass's failure still bound and
    # claim it caught something that did not happen.
    result = _run(
        [
            _n("a", "try", params={"label": "each"}),
            _n("b", "code", outputs=["ok", "error"],
               params={"code": "output = 1 / 0 if n == 1 else n"}),
            _n("c", "catch", params={"label": "each", "action": "continue"}),
        ],
        [_e("e1", "a", "b", "body"), _e("e2", "b", "c", "ok"), _e("e3", "b", "c", "error")],
        variables={"n": 1},
    )
    assert result.status == "ok", result.error
    assert result.scope["c"]["caught"] is True


# ── the code is not interpolated ────────────────────────────────────────────

def test_braces_in_a_snippet_are_not_interpolated():
    # The code is Python; ``{{...}}`` is this project's own reference syntax. A
    # snippet writing a dict literal must not have it eaten as a lookup.
    result = _run(
        [_n("a", "code", params={"code": "output = {'k': 'v'}"})],
        [],
    )
    assert result.status == "ok"
    assert result.scope["a"]["value"] == {"k": "v"}


def test_a_snippet_referencing_an_unknown_name_fails_loudly():
    result = _run(
        [_n("a", "code", params={"code": "output = whatever_undefined"})],
        [],
    )
    assert result.status == "error"
    assert "NameError" in (result.error or "")


def test_the_node_raises_node_error_for_a_refusal():
    from apps.engine.builtin_nodes import CodeNode

    instance = CodeNode({"code": "import os"})
    instance.spec = get_node_registry().get("code")
    instance.ctx = None
    with pytest.raises(NodeError, match="沙箱"):
        instance.execute()
