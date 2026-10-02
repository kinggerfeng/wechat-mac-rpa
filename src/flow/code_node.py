"""A restricted-Python sandbox for the ``code`` node.

Why this exists
---------------
Some automation genuinely has no node for it: a date format nobody modelled, a
scoring rule a vendor changes monthly, a string that needs parsing three ways at
once. ``run_shell`` covers "call a program", and ``llm``/``expr`` cover "compute a
value", but the in-between — a dozen lines of real Python against data the flow
just fetched — had nowhere to live. Writing it as a script and shelling out is
the obvious answer and it is the wrong one: a flow can be triggered by a webhook
carrying someone else's input, and the webhook is a documented feature, not a
hypothetical.

What "sandbox" means here, precisely
------------------------------------
This is **not** a security boundary against a determined attacker who already
has a Python interpreter on the machine. It is a *blast-radius* boundary against
the two realistic failure modes: an author who writes ``open("../../.ssh/id_rsa")``
by accident, and a webhook caller who gets a variable interpolated into something
they control.

The mechanism is an ``ast`` allow-list — the same one ``expr.py`` uses for
conditions, extended to statements. That choice is inherited deliberately. The
regex-then-``eval`` approach it replaced could not express nesting, and its
failure mode was silence: a guard matched its own prefix and reported success.
An allow-list fails loudly instead, naming the line.

Four escape routes are closed explicitly, because each is a real published
technique and each is cheap to block here:

1. **Imports** are refused outright. There is no safe subset of ``import`` worth
   the argument, so the question does not arise.
2. **Attribute access to underscore names** is refused. ``().__class__`` and
   friends are the substrate of every ``__subclasses__`` walk.
3. **Dunder *string literals*** are refused, closing the ``getattr(x, "__class__")``
   route that rule 2 structurally cannot see.
4. **Reflection builtins** (``getattr``, ``vars``, ``globals``, ``exec`` …) are
   absent from the namespace rather than argument-checked. An absent name raises
   ``NameError`` at the line the author wrote; a checked call fails somewhere less
   obvious.

The namespace is built by *copying in* a curated ``__builtins__`` dict, never by
deleting from the real one — a denylist over CPython's builtins is a list that has
grown every release since 1991.

Known limits, stated rather than glossed over
---------------------------------------------
- **Memory is not bounded.** ``[0] * 10**10`` will exhaust the machine. The
  deadline catches the time, not the allocation.
- **The deadline is a loop, not a wall.** It fires between lines, so a snippet
  blocked inside a single C call is not interrupted. ``time.sleep`` is therefore
  not in the namespace.
- A trace function costs roughly 2-5x on a tight loop. That is the price of a
  deadline that actually fires, and it is paid only by flows that use this node.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import io
import json as _json
import math as _math
import re as _re
import statistics as _statistics
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .files import PathRefused, resolve_under_root

#: Refuse to run a source larger than this. 200k characters is far past anything
#: anyone types into a form field, and the parse is O(n) on a thread that is
#: currently blocking the run.
MAX_SOURCE_CHARS = 200_000

#: Hard cap on captured stdout/stderr. A ``print(blob)`` in a loop can otherwise
#: produce more output than the run has memory for.
MAX_STREAM_CHARS = 100_000

#: The name a snippet binds its result to when it is a bare script rather than
#: a ``main()`` function.
OUTPUT_NAME = "output"


class CodeRefused(ValueError):
    """The source uses a construct this sandbox does not permit.

    Distinct from a runtime failure *inside* the code: refusal happens before a
    single statement runs, so a snippet refused on line 2 never got to do
    anything on line 1.
    """


class CodeTimeout(TimeoutError):
    """The snippet ran past its deadline."""


# ── the namespace ──────────────────────────────────────────────────────────

#: Pure-Python modules. ``json`` deserialises, it does not deserialise-arbitrary-
#: types; ``re`` has no I/O. That — no side effects reachable — is the criterion,
#: not the module list.
_PURE_MODULES: dict[str, Any] = {
    "json": _json,
    "re": _re,
    "math": _math,
    "statistics": _statistics,
}

#: Datetime as functions rather than as a module, so the module's other names
#: (``open``, ``sys``) are not reachable through it.
_DATETIME_NAMES: dict[str, Any] = {
    "date": date,
    "datetime": datetime,
    "timedelta": timedelta,
}

#: Builtins worth having. Reflection, import and I/O names are absent rather
#: than merely unused.
_SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "bytes", "callable", "chr", "dict",
        "divmod", "enumerate", "filter", "float", "format", "frozenset",
        "int", "isinstance", "issubclass", "iter", "len", "list", "map",
        "max", "min", "next", "ord", "pow", "print", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
        "type", "zip",
    )
    if hasattr(builtins, name)
}
_SAFE_BUILTINS.update(_DATETIME_NAMES)

#: Exception classes, so ``try/except`` is usable. Without these an author
#: cannot write the single most ordinary guard there is — ``except ValueError``
#: around a parse — and would have to add a node to the flow instead. They are
#: inert as names: the escape surface of an exception class is its dunder
#: attributes, and those are already refused by :func:`check_source`.
_EXCEPTION_NAMES: tuple[str, ...] = (
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException",
    "Exception", "IndexError", "KeyError", "LookupError",
    "NotImplementedError", "RuntimeError", "StopIteration", "SystemExit",
    "TypeError", "ValueError", "ZeroDivisionError",
)
for _name in _EXCEPTION_NAMES:
    _exc = getattr(builtins, _name, None)
    if _exc is not None:
        _SAFE_BUILTINS[_name] = _exc

#: AST node types a snippet may contain. Anything not listed is refused with its
#: line number. This is deliberately "everything listed", not "everything except
#: the dangerous ones" — the dangerous ones are the ones nobody thought of yet.
_ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset({
    # structure
    ast.Module, ast.Interactive, ast.Expression,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.Return, ast.Pass, ast.Expr,
    ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete,
    ast.For, ast.AsyncFor, ast.While, ast.If, ast.IfExp,
    ast.Break, ast.Continue,
    ast.Try, ast.TryStar, ast.ExceptHandler, ast.Raise, ast.Assert,
    # definitions needed to call anything
    ast.arguments, ast.arg, ast.keyword,
    # data
    ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Starred,
    ast.Name, ast.Constant, ast.Attribute, ast.Subscript, ast.Slice,
    ast.JoinedStr, ast.FormattedValue,
    # operations
    ast.Call, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.USub, ast.UAdd, ast.Invert,
    # contexts
    ast.Load, ast.Store, ast.Del, ast.AugLoad, ast.AugStore,
    # comprehensions
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
})

#: Constructs refused by name, so the message says *why* rather than only
#: reporting an unsupported node type.
_FORBIDDEN_NODES: frozenset[type[ast.AST]] = frozenset({
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Lambda,
    ast.Await, ast.Yield, ast.YieldFrom, ast.ClassDef,
    ast.With, ast.AsyncWith,
})

#: Why each refused construct is refused, surfaced verbatim in the error.
_REFUSAL_REASONS: dict[type[ast.AST], str] = {
    ast.Import: "不允许 import",
    ast.ImportFrom: "不允许 import",
    ast.Global: "不允许 global",
    ast.Nonlocal: "不允许 nonlocal",
    ast.Lambda: "不允许 lambda（请改写为 def）",
    ast.Await: "不允许 await",
    ast.Yield: "不允许生成器",
    ast.YieldFrom: "不允许生成器",
    ast.ClassDef: "不允许定义类（请改写为函数）",
    ast.With: "不允许 with（请改用 fs.*）",
    ast.AsyncWith: "不允许 with（请改用 fs.*）",
}


def _dunder(name: str) -> bool:
    """A dunder — the shape every escape technique walks through."""
    return name.startswith("__") and name.endswith("__")


def check_source(source: str) -> ast.Module:
    """Parse and vet ``source``, returning the tree when it is acceptable.

    Raises :class:`CodeRefused` naming the offending line.
    """
    if not isinstance(source, str):
        raise CodeRefused("代码必须是字符串")
    if not source.strip():
        raise CodeRefused("代码为空")
    if len(source) > MAX_SOURCE_CHARS:
        raise CodeRefused(f"代码过长（{len(source)} 字符，上限 {MAX_SOURCE_CHARS}）")

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise CodeRefused(f"语法错误（第 {exc.lineno} 行）: {exc.msg}") from exc

    for node in ast.walk(tree):
        if type(node) in _FORBIDDEN_NODES:
            reason = _REFUSAL_REASONS.get(type(node), "不允许使用")
            raise CodeRefused(f"第 {node.lineno} 行：{reason}")

        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise CodeRefused(
                f"第 {node.lineno} 行：不允许访问以下划线开头的属性 {node.attr!r}"
            )

        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _dunder(node.value):
            # The getattr route: getattr(x, "__class__") is an attribute access
            # by name, so the AST check above never sees it.
            raise CodeRefused(
                f"第 {node.lineno} 行：不允许使用形如 {node.value!r} 的字符串常量"
            )

        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise CodeRefused(
                f"第 {node.lineno} 行：不允许使用以下划线开头的名字 {node.id!r}"
            )

        if type(node) not in _ALLOWED_NODES:
            raise CodeRefused(
                f"第 {getattr(node, 'lineno', '?')} 行：不支持的语法 {type(node).__name__}"
            )

    return tree


# ── the whitelisted filesystem surface ─────────────────────────────────────

class _SandboxFS:
    """The only way a snippet touches a file.

    Every entry point goes through :func:`resolve_under_root`, so the same
    containment rule that governs the ``file`` nodes governs a line of user code.
    That is the entire reason for a wrapper object instead of injecting ``open``:
    a raw ``open`` would be a different, weaker rule hidden behind the same door.
    """

    def __init__(self, root: Path, writable: bool) -> None:
        self._root = root
        self._writable = writable

    def _resolve(self, path: str) -> Path:
        return resolve_under_root(self._root, path)

    def _rel(self, target: Path) -> str:
        try:
            return str(target.relative_to(self._root))
        except ValueError:  # pragma: no cover - resolve_under_root already checked
            return str(target)

    def _guard_write(self) -> None:
        if not self._writable:
            raise PathRefused("当前流程未授权写文件（readonly=1）")

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self._resolve(path).read_text(encoding=encoding, errors="replace")

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> str:
        self._guard_write()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding=encoding)
        return self._rel(target)

    def append_text(self, path: str, content: str, encoding: str = "utf-8") -> str:
        self._guard_write()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding=encoding) as handle:
            handle.write(str(content))
        return self._rel(target)

    def list_dir(self, path: str = ".") -> list[str]:
        target = self._resolve(path)
        if not target.is_dir():
            raise PathRefused(f"不是目录: {path}")
        return sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def is_dir(self, path: str) -> bool:
        return self._resolve(path).is_dir()

    def size(self, path: str) -> int:
        return self._resolve(path).stat().st_size

    def mkdir(self, path: str) -> str:
        self._guard_write()
        target = self._resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return self._rel(target)


# ── output capture and the deadline ────────────────────────────────────────

class _BoundedStream(io.TextIOBase):
    """A write target that stops growing at ``limit`` and counts the rest.

    Silently truncating would be this project's signature bug: a snippet that
    printed 200k characters and got 100k back would look successful. The
    ``truncated`` flag rides out in the node's output so the run trace says so.
    """

    def __init__(self, limit: int) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._limit = max(0, limit)
        self.truncated = False

    def write(self, text: str) -> int:  # type: ignore[override]
        remaining = self._limit - self._length
        if remaining <= 0:
            self.truncated = True
            return len(text)
        if len(text) > remaining:
            self._parts.append(text[:remaining])
            self._length = self._limit
            self.truncated = True
            return len(text)
        self._parts.append(text)
        self._length += len(text)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self._parts)


class _Deadline:
    """A wall-clock stop, checked between lines.

    ``sys.settrace`` is the only in-process way to interrupt a tight Python loop
    from outside: signals only land between bytecodes, a watchdog thread cannot
    preempt, and a subprocess would mean re-implementing the variable scope. The
    cost is real and worth naming — tracing roughly doubles a hot loop — which is
    why this node is for short snippets, not number crunching.

    Traces are per-thread, and the previous hook is restored in ``exit``: the
    executor runs nodes on worker threads, and a leaked hook would follow the
    thread into whatever it does next.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = max(0.001, float(seconds))
        self._deadline = time.monotonic() + self.seconds
        self._previous: Callable[..., Any] | None = None

    def _hook(self, frame: Any, event: str, arg: Any) -> Any:
        if time.monotonic() > self._deadline:
            raise CodeTimeout(f"执行超时（>{self.seconds:g}s）")
        return self._hook

    def __enter__(self) -> "_Deadline":
        self._previous = sys.gettrace()
        sys.settrace(self._hook)
        return self

    def __exit__(self, *exc: Any) -> None:
        sys.settrace(self._previous)


def _timeout_seconds(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ── the entry point ────────────────────────────────────────────────────────

def run_code(
    source: str,
    *,
    inputs: dict[str, Any] | None = None,
    root: Path | str = ".",
    writable: bool = False,
    timeout: float = 5.0,
    max_output_chars: int = MAX_STREAM_CHARS,
) -> dict[str, Any]:
    """Vet, run, and report on a snippet.

    A snippet produces its result two ways: by binding ``output``, or by defining
    ``main()`` and returning from it. ``main()`` wins when both are present,
    because a function is the harder shape to get wrong.

    Policy violations raise :class:`CodeRefused` — the flow is misconfigured and
    should fail loudly. A ``ZeroDivisionError`` *inside* a permitted snippet is
    returned as ``ok: False`` instead. Folding both into one shape would make
    "this code is not allowed here" indistinguishable from "your code has a bug"
    three screens later.
    """
    tree = check_source(source)
    root_path = Path(root).resolve()
    budget = _timeout_seconds(timeout, 5.0)

    namespace: dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "__name__": "flow_code",
        "fs": _SandboxFS(root_path, writable),
    }
    namespace.update(_PURE_MODULES)
    for key, value in (inputs or {}).items():
        if not key.isidentifier():
            raise CodeRefused(f"非法输入名: {key!r}")
        namespace[key] = value

    out = _BoundedStream(max_output_chars)
    err = _BoundedStream(max_output_chars)
    started = time.monotonic()
    status = "ok"
    value: Any = None
    error = ""
    error_type = ""
    error_exc: BaseException | None = None
    source_of_value = "none"

    try:
        compiled = compile(tree, "<flow-code>", "exec")
        with redirect_stdout(out), redirect_stderr(err), _Deadline(budget):
            exec(compiled, namespace)  # noqa: S102 - the AST allow-list is the guard
            # ``inspect.isfunction`` rather than ``callable``: an input named
            # ``main`` that arrived as a string would otherwise be invoked.
            if inspect.isfunction(namespace.get("main")):
                value = namespace["main"]()
                source_of_value = "main"
            elif OUTPUT_NAME in namespace:
                value = namespace[OUTPUT_NAME]
                source_of_value = OUTPUT_NAME
    except CodeTimeout as exc:
        status, error, error_type, error_exc = "timeout", str(exc), "CodeTimeout", exc
    except PathRefused as exc:
        status, error, error_type, error_exc = "refused", str(exc), "PathRefused", exc
    except BaseException as exc:  # noqa: BLE001
        # A snippet raising SystemExit must not take the run down with it, and
        # the type is reported so "your code raised ValueError" is visible.
        status = "error"
        error, error_type, error_exc = f"{type(exc).__name__}: {exc}", type(exc).__name__, exc

    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": status == "ok",
        "status": status,
        "value": value,
        "value_from": source_of_value,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "error": error,
        "error_type": error_type,
        # The exception object itself, not just its name. A caller that
        # re-raises can chain it, and the innermost type is the one worth
        # branching on — "ZeroDivisionError", not the "NodeError" the wrapper
        # is obliged to raise.
        "error_exc": error_exc,
        "duration_ms": duration_ms,
        # Explicit rather than implicit: a truncated log that looks complete is
        # the failure this project keeps paying for.
        "truncated": out.truncated or err.truncated,
    }
