"""Run scope for a flow execution: variable binding, condition evaluation, service access.

Nodes receive a :class:`FlowContext` and return a ``dict`` of outputs. The executor
binds those outputs into the context, and later nodes read them by name. Reads are
dotted (``result.chat_name``) and tolerate the shape changes that real perception
output has — a missing key yields ``None`` rather than raising, because a branch
guarded on a missing value is a normal outcome, not a crash.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .schema import CONDITION_RE, FlowError, Node

_MISSING = object()

#: Comparison operators, longest first so ``>=`` wins over ``>``.
_OPERATORS: tuple[tuple[str, Callable[[Any, Any], bool]], ...] = (
    ("==", lambda a, b: _loose_eq(a, b)),
    ("!=", lambda a, b: not _loose_eq(a, b)),
    (">=", lambda a, b: _num(a) >= _num(b)),
    ("<=", lambda a, b: _num(a) <= _num(b)),
    (">", lambda a, b: _num(a) > _num(b)),
    ("<", lambda a, b: _num(a) < _num(b)),
    ("~", lambda a, b: _contains(a, b)),
)


def _loose_eq(left: Any, right: Any) -> bool:
    """Compare numbers across int/float and strings across case.

    ``0`` and ``False`` must not compare equal here: a flow testing
    ``messages.count == 0`` should not match a ``False`` flag.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right) if isinstance(left, (bool, int)) and isinstance(right, (bool, int)) else left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    return left == right


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return float("nan")
    return float("nan")


def _contains(left: Any, right: Any) -> bool:
    if left is None:
        return False
    if isinstance(left, str):
        return str(right) in left
    if isinstance(left, (list, tuple, set)):
        return right in left
    if isinstance(left, dict):
        return right in left
    return False


def parse_literal(raw: str) -> Any:
    """Parse the right-hand side of a condition into a Python value.

    Accepts quoted strings, ``true``/``false``/``null``, numbers, ``[]``/``{}``
    and falls back to the bare word (so a condition can test against a keyword).
    """
    text = raw.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if text == "[]":
        return []
    if text == "{}":
        return {}
    return text


def evaluate_condition(expression: str, scope: "FlowScope") -> bool:
    """Evaluate ``'operand OP literal'`` against ``scope``.

    An unparseable expression is a hard error, not a silent ``True``: a broken
    guard that always fires would run the wrong branch of a live bot.
    """
    match = CONDITION_RE.match(expression)
    if not match:
        raise FlowError(f"无法解析条件表达式: {expression!r}")
    operand, operator, literal_text = match.groups()
    actual = scope.get(operand)
    for symbol, compare in _OPERATORS:
        if operator == symbol:
            return compare(actual, parse_literal(literal_text))
    raise FlowError(f"未知运算符 {operator!r}")


class FlowScope:
    """Flat name -> value scope with dotted reads."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._values: dict[str, Any] = dict(initial or {})
        self._lock = threading.RLock()

    def bind(self, name: str, value: Any) -> None:
        if not name:
            return
        with self._lock:
            self._values[name] = value

    def bind_all(self, values: dict[str, Any]) -> None:
        with self._lock:
            for name, value in values.items():
                if name:
                    self._values[name] = value

    def get(self, dotted: str) -> Any:
        head, _, tail = dotted.partition(".")
        with self._lock:
            value = self._values.get(head, _MISSING)
        if value is _MISSING:
            return None
        if not tail:
            return value
        return _walk(value, tail.split("."))

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._values

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(dict(self._values))


def _walk(value: Any, parts: list[str]) -> Any:
    current = value
    for part in parts:
        if current is None:
            return None
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
            continue
        if isinstance(current, (list, tuple)):
            if part == "count":
                return len(current)
            if part == "first":
                return current[0] if current else None
            if part == "last":
                return current[-1] if current else None
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
            continue
        # Objects expose to_dict() for attribute-style access from expressions.
        to_dict = getattr(current, "to_dict", None)
        if callable(to_dict):
            as_dict = to_dict()
            if isinstance(as_dict, dict):
                if part not in as_dict:
                    return None
                current = as_dict[part]
                continue
        attr = getattr(current, part, None)
        if attr is None and not hasattr(current, part):
            return None
        current = attr
    return current


@dataclass
class FlowContext:
    """Everything a node needs to run.

    ``services`` is the lazy injection point for the heavy subsystems (capture,
    perception, LLM, memory). Building them costs seconds and a screen-capture
    permission prompt, so they are created on first use and shared across the run.
    """

    scope: FlowScope
    run_id: str
    flow_id: str
    services: dict[str, Any] = field(default_factory=dict)
    _service_factories: dict[str, Callable[[], Any]] = field(default_factory=dict)
    _service_lock: threading.Lock = field(default_factory=threading.Lock)
    abort_requested: bool = False
    started_at: float = field(default_factory=time.time)
    node_counts: dict[str, int] = field(default_factory=dict)
    #: Set by the executor. A node running a nested flow re-emits that flow's
    #: spans here so the parent trace shows the delegated steps inline.
    span_sink: Any = None

    def service(self, name: str) -> Any:
        """Get a shared service, constructing it on first request."""
        with self._service_lock:
            if name in self.services:
                return self.services[name]
        factory = self._service_factories.get(name)
        if factory is None:
            raise FlowError(f"未注册的服务: {name!r}")
        instance = factory()
        with self._service_lock:
            self.services.setdefault(name, instance)
            return self.services[name]

    def register_service_factory(self, name: str, factory: Callable[[], Any]) -> None:
        self._service_factories[name] = factory

    def put_service(self, name: str, instance: Any) -> None:
        with self._service_lock:
            self.services[name] = instance

    def bind_outputs(self, node: Node, outputs: dict[str, Any]) -> None:
        """Bind a node's declared outputs, plus the whole result under its id."""
        self.scope.bind(node.id, outputs)
        for name in node.outputs or list(outputs):
            if name in outputs:
                self.scope.bind(name, outputs[name])

    def count_node(self, node_id: str) -> int:
        self.node_counts[node_id] = self.node_counts.get(node_id, 0) + 1
        return self.node_counts[node_id]

    def request_abort(self) -> None:
        self.abort_requested = True

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at
