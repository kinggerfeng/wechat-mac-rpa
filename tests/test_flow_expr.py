"""Expression evaluation for conditions and ``{{...}}`` interpolation.

Two regressions are pinned here deliberately.

The first is silent truncation. Conditions used to be matched by a single
regex whose trailing ``(.+?)`` absorbed everything after the first comparison,
so ``count > 5 and enabled`` was evaluated as ``3 > "5 and enabled"`` — the
right-hand side parsed as a bare word, failed to convert to a number, became
``nan``, and every comparison against ``nan`` is False. The guard reported a
result instead of failing, which is the worst possible outcome: a flow that
looks configured and takes the wrong branch.

The second is code execution. Three call sites handed author-supplied text to
:func:`eval` behind a reduced ``__builtins__``, on the reasoning that a closed
namespace is a sandbox. It is not — ``().__class__.__bases__[0].__subclasses__()``
reaches every loaded class regardless. A flow graph is editable through the
API, so that made an expression field an arbitrary-execution primitive.
"""

from __future__ import annotations

import pytest

from apps.engine.context import FlowScope, evaluate_condition
from apps.engine.expr import evaluate, interpolate
from apps.engine.schema import FlowError


@pytest.fixture()
def scope() -> FlowScope:
    return FlowScope(
        {
            "count": 3,
            "empty": 0,
            "name": "alex",
            "flag": True,
            "items": [1, 2, 3],
            "tags": ["a", "b"],
            "meta": {"total": 9, "tags": ["x"]},
            "version": "v1.2.3",
        }
    )


# ───────────────────────────────────────────── the truncation regression ──

@pytest.mark.parametrize(
    "expression,expected",
    [
        # Each of these was True to a reader and False to the old evaluator.
        ("count > 0 and name == 'alex'", True),
        ("count > 5 or name == 'alex'", True),
        ("count > 0 and empty == 0 and flag", True),
        ("count > 5 and flag", False),
        ("(count > 0) and (name == 'alex')", True),
        ("(count > 0 and flag) or (empty == 9)", True),
        ("not flag", False),
        ("not (count > 99)", True),
        ("count > 0 and not flag", False),
    ],
)
def test_combined_conditions_evaluate_every_clause(scope, expression, expected):
    assert evaluate_condition(expression, scope) is expected

def test_whole_expression_is_consumed(scope):
    """A trailing clause can never be dropped without an error.

    This is the direct guard against the regex: whatever follows the first
    comparison has to influence the outcome or fail the evaluation.
    """
    assert evaluate_condition("count > 99 and name == 'alex'", scope) is False
    assert evaluate_condition("count > 0 and name == 'nobody'", scope) is False
    # Both clauses satisfied — if only the first were read this would be False.
    assert evaluate_condition("count > 0 and name == 'alex'", scope) is True


def test_unparseable_expression_never_degrades_to_a_partial_result():
    with pytest.raises(FlowError):
        evaluate_condition("count > 0 and ???", FlowScope({"count": 1}))


# ──────────────────────────────────────────────────────── grammar coverage ──

@pytest.mark.parametrize(
    "expression,expected",
    [
        ("count > 0", True),
        ("count > 99", False),
        ("name == 'alex'", True),
        ("name == 'bob'", False),
        ("name ~ 'le'", True),
        ("tags ~ 'a'", True),
        ("version == 'v1.2.3'", True),
        ("count >= 3 and count <= 3", True),
        ("1 < count < 10", True),
        ("count * 2 + 1 == 7", True),
        ("count % 2 == 1", True),
        ("count // 2 == 1", True),
        ("'yes' if count > 0 else 'no'", "yes"),
        ("len(items) == 3", True),
        ("sorted(items)[0] == 1", True),
        ("items == [1, 2, 3]", True),
        ("meta == {'total': 9, 'tags': ['x']}", True),
        ("items[0] == 1", True),
        ("meta.total == 9", True),
        ("items.count == 3", True),
        ("items.first == 1", True),
        ("items.last == 3", True),
        ("meta.tags.count == 1", True),
        ("'a,b,c'.split(',').count == 3", True),
        ("name.upper() == 'ALEX'", True),
        ("'  x '.strip() == 'x'", True),
        ("flag", True),
    ],
)
def test_expression_grammar(scope, expression, expected):
    assert evaluate(expression, scope) == expected


def test_legacy_contains_survives_next_to_a_tilde_in_data(scope):
    """``~`` is the flow's contains-operator; a ``~`` inside a value is data."""
    assert evaluate("version ~ '1.2'", scope) is True
    assert evaluate("version == 'v1.2.3'", scope) is True


def test_bare_word_comparand_stays_a_literal(scope):
    """Quoting remains optional on the right of a comparison."""
    assert evaluate("name == alex", scope) is True
    assert evaluate("name == bob", scope) is False
    assert evaluate("text ~ 微信", FlowScope({"text": "来自微信"})) is True


def test_undefined_variable_is_an_error_not_a_silent_none():
    with pytest.raises(FlowError, match="未定义"):
        evaluate("nope == 1", FlowScope({"other": 1}))


def test_incompatible_operands_report_the_expression():
    with pytest.raises(FlowError, match="类型不匹配"):
        evaluate("3 in 3", FlowScope({}))


# ─────────────────────────────────────────────────────────── containment ──

@pytest.mark.parametrize(
    "payload",
    [
        "__import__('os').system('echo pwned')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "[x for x in items]",
        "(lambda: 1)()",
        "name.upper.__self__",
        "items.remove",
        "items.append",
        "items.pop",
        "meta.clear",
        "tags.add",
        "version.format",
        "'{0.__class__}'.format(items)",
        "name._private",
    ],
)
def test_expressions_cannot_reach_outside_the_scope(scope, payload):
    """Every one of these ran under the old ``eval`` implementation.

    The sandbox claim was that a reduced ``__builtins__`` closed the namespace.
    It did not: subclass traversal alone reached 225 loaded classes, which is
    enough to find an import. The evaluator now walks an allow-listed ``ast``.
    """
    with pytest.raises(FlowError):
        evaluate(payload, scope)


def test_read_only_methods_still_work(scope):
    assert evaluate("items.count > 0", scope) is True
    assert evaluate("meta.get('total') == 9", scope) is True
    assert evaluate("sorted(items) == [1, 2, 3]", scope) is True


# ────────────────────────────────────────────────────────── interpolation ──

@pytest.mark.parametrize(
    "template,expected",
    [
        ("共 {{count}} 条", "共 3 条"),
        ("你好 {{name}}", "你好 alex"),
        ("{{len(items)}} 项", "3 项"),
        ("{{meta.total}}", "9"),
        ("{{version}}", "v1.2.3"),
        ("{{count}} / {{name}}", "3 / alex"),
        ("{{items[0]}}", "1"),
        ("{{items.count}}", "3"),
        ("{{name.upper()}}", "ALEX"),
        ("{{count if count > 0 else 0}}", "3"),
        ("没有变量", "没有变量"),
        ("", ""),
    ],
)
def test_interpolation(scope, template, expected):
    assert interpolate(template, scope) == expected


def test_interpolation_reports_an_unresolvable_reference(scope):
    with pytest.raises(FlowError):
        interpolate("{{missing}}", scope)


def test_interpolation_can_stay_permissive_while_a_template_is_drafted(scope):
    assert interpolate("{{missing}}", scope, strict=False) == "{{missing}}"


def test_interpolation_renders_none_as_empty():
    """An unset optional must not paste the literal word "None" into a message."""
    assert interpolate("[{{maybe}}]", FlowScope({"maybe": None})) == "[]"


def test_interpolation_leaves_plain_text_untouched(scope):
    for text in ("plain", "", "100%", "a { b } c", "{{unclosed"):
        assert interpolate(text, scope, strict=False) == text


# ─────────────────────────────────────────────────────────── node wiring ──

def test_set_var_evaluates_expressions_without_eval():
    """The variable node shared the same sandbox; it now shares the evaluator."""
    from apps.engine.executor import FlowExecutor
    from apps.engine.schema import Flow

    def _node(node_id, node_type, **params):
        return {
            "id": node_id,
            "type": node_type,
            "name": node_type,
            "position": {"x": 0, "y": 0},
            "params": params,
            "retry": {"max": 0, "delay": 0},
            "timeout": None,
            "on_error": "fail",
            "outputs": [],
            "disabled": False,
            "path": None,
            "target": None,
        }

    def _edge(edge_id, source, target, port="ok"):
        return {"id": edge_id, "source": source, "source_port": port, "target": target}

    flow = Flow(
        id="expr_setvar",
        name="test",
        graph={
            "version": 1,
            "entry": "a",
            "variables": {},
            "nodes": [
                _node("a", "set_var", name="seed", value=3),
                _node("b", "set_var", name="doubled", expression="seed * 2"),
                _node("c", "set_var", name="nope", expression="__import__('os')"),
            ],
            "edges": [_edge("e1", "a", "b"), _edge("e2", "b", "c")],
        },
    )
    result = FlowExecutor().run(flow, "expr_setvar")

    # Reached c, so the arithmetic expression evaluated rather than being
    # rejected as unsupported, and c failed naming the call it refused.
    assert result.status == "error"
    assert result.scope.get("doubled") == 6
    assert "不允许调用函数" in (result.error or "") and "__import__" in (result.error or "")


def test_condition_node_evaluates_a_compound_expression():
    from apps.engine.executor import FlowExecutor
    from apps.engine.schema import Flow

    def _node(node_id, node_type, **params):
        return {
            "id": node_id,
            "type": node_type,
            "name": node_type,
            "position": {"x": 0, "y": 0},
            "params": params,
            "retry": {"max": 0, "delay": 0},
            "timeout": None,
            "on_error": "fail",
            "outputs": [],
            "disabled": False,
            "path": None,
            "target": None,
        }

    flow = Flow(
        id="expr_cond",
        name="test",
        graph={
            "version": 1,
            "entry": "c",
            "variables": {"items": [1, 2]},
            "nodes": [
                _node(
                    "c",
                    "condition",
                    expression="count > 0 and flag and len(items) == 2",
                ),
            ],
            "edges": [],
        },
    )
    # ``count`` and ``flag`` are never bound, so the first clause cannot hold.
    # The point is that it is now *decided* rather than silently ignored.
    result = FlowExecutor().run(flow, "expr_cond", variables={"count": 0, "flag": True})
    assert result.status == "ok"
    assert result.scope.get("expression") == "count > 0 and flag and len(items) == 2"
    assert result.scope.get("value") is False

    ok_run = FlowExecutor().run(flow, "expr_cond2", variables={"count": 5, "flag": True})
    assert ok_run.scope.get("value") is True
