"""Safe expression evaluation for flow conditions and ``{{...}}`` interpolation.

Why this module exists
----------------------
Conditions used to be matched with a single regular expression, which structurally
cannot express ``and``/``or``/parentheses. Worse, the regex *silently discarded*
anything after the first comparison and returned a boolean computed from the
prefix alone — a flow guarding on ``count > 5 and enabled`` was actually
guarding on ``count > 5``, and it reported success rather than failing.

A regex is the wrong tool for a language with nesting. This module walks a
Python ``ast`` instead and evaluates it against a :class:`FlowScope`.

Safety
------
Nothing is ever ``eval``'d. Every node type is dispatched through an explicit
allow-list; anything unknown — attribute access to private names, imports,
lambdas, comprehensions, subscript assignment — raises :class:`FlowError`
naming the offending construct. Method calls go through a separate allow-list so
``"abc".upper()`` works but ``"".__class__.__mro__`` cannot be reached.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Callable

from .context import LIST_ATTRS, _step
from .schema import FlowError

#: Flow-level attributes on a sequence, resolved through the scope's grammar.
_LIST_ATTRS = LIST_ATTRS

#: Whitelisted free functions. Deliberately small and total: no I/O, no
#: reflection, no imports.
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
}

#: Whitelisted methods. Keyed by receiver type so a ``dict`` cannot reach list
#: methods and vice versa; anything absent is a hard error.
#:
#: Mutating members (append/remove/pop/clear/update/sort/…) are excluded on
#: purpose: an expression should read the scope, never reshape it behind the
#: caller's back. ``str.format`` is excluded for a different reason — its
#: ``{0.attr}`` syntax reaches arbitrary attributes of its argument, which is a
#: template-injection hole that no amount of outer whitelisting would close.
_METHODS: dict[type, frozenset[str]] = {
    str: frozenset(
        {
            "capitalize", "count", "endswith", "find", "index",
            "isalnum", "isalpha", "islower", "isnumeric", "isspace",
            "istitle", "isupper", "join", "lower", "lstrip", "removeprefix",
            "removesuffix", "replace", "rfind", "rindex", "rsplit", "rstrip",
            "split", "splitlines", "startswith", "strip", "title", "upper",
            "zfill",
        }
    ),
    list: frozenset({"count", "index"}),
    tuple: frozenset({"count", "index"}),
    dict: frozenset({"get", "items", "keys", "values"}),
    set: frozenset({"difference", "intersection", "union", "issubset", "issuperset"}),
    int: frozenset({"bit_length"}),
    float: frozenset({"is_integer"}),
}

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

#: The legacy ``a ~ b`` means *b is contained in a* — the operands are the
#: other way round from Python's ``in``. Rewriting the text cannot express that,
#: so ``@`` (matrix multiply) is used as a marker: it is syntactically valid,
#: semantically meaningless, and would be rejected anyway, which makes it a safe
#: place to recover the original intent and swap the operands.
_CONTAINS_MARKER = "@"


def _legacy_contains(left: Any, right: Any) -> bool:
    if left is None:
        return False
    if isinstance(left, str):
        return str(right) in left
    if isinstance(left, (list, tuple, set, dict)):
        return right in left
    return False

_COMPARE: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}


def _stringify(value: Any) -> str:
    """Render a value for text interpolation.

    ``None`` becomes an empty string rather than ``"None"``: a flow that
    interpolates an unset optional should not paste the word "None" into a chat
    message or a filename.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_stringify(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_stringify(v)}" for k, v in value.items())
    return str(value)


def _describe(node: ast.AST) -> str:
    return type(node).__name__


class _Evaluator:
    def __init__(self, scope: Any) -> None:
        self.scope = scope

    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, "_eval_" + _describe(node), None)
        if method is None:
            raise FlowError(f"表达式不支持 {_describe(node)}")
        return method(node)

    # ── leaves ────────────────────────────────────────────────────────────
    def _eval_Expression(self, node: ast.Expression) -> Any:
        return self.eval(node.body)

    def _eval_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _eval_Name(self, node: ast.Name) -> Any:
        name = node.id
        if name == "true":
            return True
        if name == "false":
            return False
        if name in ("null", "none", "nil"):
            return None
        if not self.scope.has(name):
            # An unknown name is an authoring error, not an empty string: a
            # condition silently comparing against None takes the wrong branch.
            raise FlowError(f"表达式引用了未定义的变量: {name!r}")
        return self.scope.get(name)

    # ── containers ────────────────────────────────────────────────────────
    def _eval_List(self, node: ast.List) -> list[Any]:
        return [self.eval(e) for e in node.elts]

    def _eval_Tuple(self, node: ast.Tuple) -> tuple[Any, ...]:
        return tuple(self.eval(e) for e in node.elts)

    def _eval_Set(self, node: ast.Set) -> set[Any]:
        return {self.eval(e) for e in node.elts}

    def _eval_Dict(self, node: ast.Dict) -> dict[Any, Any]:
        out: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values):
            if key is None:  # {**other}
                out.update(self.eval(value))
            else:
                out[self.eval(key)] = self.eval(value)
        return out

    # ── access ────────────────────────────────────────────────────────────
    def _eval_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr.startswith("_"):
            raise FlowError(f"表达式禁止访问私有属性: {node.attr!r}")

        obj = self.eval(node.value)

        # A sequence carries flow-level attributes — count is its length, first
        # and last its ends — and that reading must win over list.count, which is
        # a method taking an argument. Handled by the scope so both the dotted
        # reader and the evaluator agree on one grammar.
        if isinstance(obj, (list, tuple)) and node.attr in _LIST_ATTRS:
            return _step(obj, node.attr)

        if isinstance(obj, dict):
            # A mapping is nested in a scope as ``d.k``; a dotted read is a key
            # lookup first and a method only when the name is allow-listed.
            if node.attr in _METHODS.get(dict, frozenset()):
                return getattr(obj, node.attr)
            try:
                return obj[node.attr]
            except KeyError as exc:
                raise FlowError(f"字典没有键 {node.attr!r}") from exc

        allowed = _METHODS.get(type(obj))
        if allowed is not None and node.attr not in allowed:
            raise FlowError(f"表达式不允许访问 {type(obj).__name__}.{node.attr}")
        return getattr(obj, node.attr)

    def _eval_Subscript(self, node: ast.Subscript) -> Any:
        target = self.eval(node.value)
        index = self.eval(node.slice)
        try:
            return target[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise FlowError(f"下标访问失败: {type(exc).__name__}") from exc

    # ── operators ─────────────────────────────────────────────────────────
    def _eval_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.eval(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.eval(value)
            if result:
                return result
        return result

    def _eval_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.eval(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise FlowError("表达式不支持该一元运算符")

    def _eval_BinOp(self, node: ast.BinOp) -> Any:
        if isinstance(node.op, ast.MatMult):
            # See _CONTAINS_MARKER. ``a ~ b`` already reads b-inside-a, and the
            # marker keeps that operand order, so no swap is needed here. The
            # right side goes through _operand so ``text ~ 微信`` keeps treating
            # the bare word as a literal.
            return _legacy_contains(self.eval(node.left), self._operand(node.right))
        handler = _BINOPS.get(type(node.op))
        if handler is None:
            raise FlowError(f"表达式不支持运算符 {_describe(node.op)}")
        return handler(self.eval(node.left), self.eval(node.right))

    def _eval_Compare(self, node: ast.Compare) -> Any:
        """Chained comparison: ``1 < x < 10`` evaluates left to right."""
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            handler = _COMPARE.get(type(op))
            if handler is None:
                raise FlowError(f"表达式不支持比较运算符 {_describe(op)}")
            right = self._operand(comparator)
            if not handler(left, right):
                return False
            left = right
        return True

    def _operand(self, node: ast.AST) -> Any:
        """Evaluate the right-hand side of a comparison.

        A bare word that names nothing in the scope is read as a literal, so
        ``name == bob`` and ``text ~ 微信`` keep working without quotes — the
        grammar every existing flow was authored against. The fallback is
        bounded to comparands: on the left an unknown name still raises, because
        that side is a value being read rather than something compared against.
        """
        if (
            isinstance(node, ast.Name)
            and node.id not in ("true", "false", "null", "none", "nil")
            and hasattr(self.scope, "has")
            and not self.scope.has(node.id)
        ):
            return node.id
        return self.eval(node)

    def _eval_IfExp(self, node: ast.IfExp) -> Any:
        return self.eval(node.body) if self.eval(node.test) else self.eval(node.orelse)

    # ── calls ─────────────────────────────────────────────────────────────
    def _eval_Call(self, node: ast.Call) -> Any:
        if node.keywords:
            names = {k.arg for k in node.keywords}
            if names - {"sep", "fill", "start", "end", "reverse", "round"}:
                raise FlowError("表达式函数不支持关键字参数")
        args = [self.eval(a) for a in node.args]
        kwargs = {k.arg: self.eval(k.value) for k in node.keywords if k.arg}

        if isinstance(node.func, ast.Name):
            fn = _FUNCTIONS.get(node.func.id)
            if fn is None:
                raise FlowError(f"表达式不允许调用函数 {node.func.id!r}")
            return fn(*args, **kwargs)

        if isinstance(node.func, ast.Attribute):
            receiver = self.eval(node.func.value)
            allowed = _METHODS.get(type(receiver), frozenset())
            if node.func.attr not in allowed:
                raise FlowError(
                    f"表达式不允许对 {type(receiver).__name__} 调用 {node.func.attr!r}"
                )
            return getattr(receiver, node.func.attr)(*args, **kwargs)

        raise FlowError("表达式不支持该调用形式")


#: The legacy ``a ~ 'sub'`` contains-operator. Python reads ``~`` as unary
#: bitwise-not, so ``a ~ b`` is a syntax error there and the raw text is never
#: evaluated. The lookbehind keeps the operator anchored to operand position, so
#: a ``~`` embedded in a quoted value is only rewritten on the retry below.
#: Operand order is preserved: ``a ~ b`` already means "b is inside a".
_LEGACY_CONTAINS_RE = re.compile(r"(?<=[\s\)])\s*~\s*")


def _parse(text: str) -> ast.Expression:
    try:
        return ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise FlowError(f"无法解析表达式: {text!r} ({exc.msg})") from exc


def evaluate(expression: str, scope: Any) -> Any:
    """Evaluate ``expression`` against ``scope``.

    The whole string is consumed: there is no regex that can match a prefix and
    leave a suffix behind, so a malformed condition raises instead of quietly
    running a different one.
    """
    text = (expression or "").strip()
    if not text:
        raise FlowError("表达式为空")

    evaluator = _Evaluator(scope)
    try:
        return evaluator.eval(_parse(text))
    except FlowError:
        if "~" not in text:
            raise
    except TypeError as exc:
        # A well-formed expression over incompatible operands (``3 in 3``) is
        # still an authoring error, and must read as one rather than escaping
        # as a raw TypeError from deep inside the evaluator.
        raise FlowError(f"表达式类型不匹配: {text!r} ({exc})") from exc
    # Retried only after the literal reading failed, so a ``~`` inside a quoted
    # string is never rewritten.
    try:
        return evaluator.eval(_parse(_LEGACY_CONTAINS_RE.sub(f" {_CONTAINS_MARKER} ", text)))
    except TypeError as exc:
        raise FlowError(f"表达式类型不匹配: {text!r} ({exc})") from exc


def evaluate_condition(expression: str, scope: Any) -> bool:
    """Evaluate a condition to a strict boolean."""
    return bool(evaluate(expression, scope))


#: ``{{ path.to.value }}`` — dotted path or a full expression inside.
INTERPOLATION_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def interpolate(template: str, scope: Any, *, strict: bool = True) -> str:
    """Substitute every ``{{...}}`` occurrence in ``template``.

    With ``strict=False`` an unresolvable expression is left verbatim, which is
    what a partially authored template needs while it is being written.
    """
    if not isinstance(template, str) or "{{" not in template:
        return template

    def replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if not expr:
            return match.group(0)
        try:
            return _stringify(evaluate(expr, scope))
        except FlowError:
            if strict:
                raise
            return match.group(0)

    return INTERPOLATION_RE.sub(replace, template)


def has_interpolation(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value
