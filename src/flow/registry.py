"""Node type registry.

Mirrors ``src/tools/tool_registry.py`` so the codebase has one registration idiom,
but the payload is a class rather than a callable: a node needs a declared
parameter schema (the canvas renders a form from it) and a declared port list (the
canvas draws sockets from it), which a bare callable cannot express.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Type

from .context import FlowContext
from .schema import BASE_PORTS, FlowError


@dataclass
class ParamSpec:
    """One configurable parameter on a node type.

    ``kind`` drives which control the canvas renders: ``text``/``textarea``/
    ``number``/``select``/``bool``/``variable``.
    """

    name: str
    kind: str = "text"
    label: str = ""
    default: Any = None
    choices: list[Any] = field(default_factory=list)
    help: str = ""
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "label": self.label or self.name,
            "default": self.default,
            "choices": self.choices,
            "help": self.help,
            "required": self.required,
        }


@dataclass
class NodeSpec:
    """Everything the canvas and the validator need to know about a node type."""

    type: str
    label: str
    category: str
    handler: Type["BaseNode"]
    params: list[ParamSpec] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    doc: str = ""
    #: Node types that must be on screen for this one to work (e.g. ``perceive``
    #: needs the WeChat window). Rendered as a precondition badge in the canvas.
    requires: list[str] = field(default_factory=list)
    singleton: bool = False

    def all_ports(self) -> list[str]:
        return list(dict.fromkeys([*BASE_PORTS, *self.outputs]))

    def to_dict(self) -> dict[str, Any]:
        from .schema import PATH_CHOICES, PATH_AWARE_TYPES

        aware = self.type in PATH_AWARE_TYPES
        return {
            "type": self.type,
            "label": self.label,
            "category": self.category,
            "params": [p.to_dict() for p in self.params],
            "outputs": self.all_ports(),
            "doc": self.doc,
            "requires": self.requires,
            "singleton": self.singleton,
            # The canvas uses this to render a path selector on the node itself,
            # outside the parameter form, and to badge the node in the graph.
            "path_aware": aware,
            "path_choices": list(PATH_CHOICES) if aware else [],
        }


class BaseNode:
    """Subclass and implement :meth:`execute`.

    The base class resolves ``params`` entries that the user left blank from the
    run scope, so a node can declare ``chat_name`` as a param and the canvas user
    can leave it empty to inherit the value a previous node bound.

    ``path`` and ``target`` are injected by the executor from the node's
    first-class graph fields. They are design-time values chosen while authoring
    the flow, so a node reads them as configuration, not as something it decides
    for itself.
    """

    spec: NodeSpec

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params or {}
        self.path: str | None = None
        self.target: str | None = None
        #: Set by the executor before each attempt. A loop keys its iteration
        #: counter on this; the instance itself is rebuilt every attempt.
        self.node_id: str = ""

    def param(self, name: str, default: Any = None) -> Any:
        """A declared parameter, with ``{{...}}`` references resolved.

        Interpolation lives here rather than in each node so that every one of
        them supports it. It used to be applied at ten hand-written call sites
        in ``builtin_nodes`` and nowhere at all in ``control_nodes`` or
        ``system_nodes``, which meant a flow author who typed ``{{chat_name}}``
        into an ``excel`` node's path got a literal file-not-found at run time
        with nothing in the graph to explain it.
        """
        if name in self.params and self.params[name] not in (None, ""):
            value = self.params[name]
            if isinstance(value, str) and self.ctx is not None:
                from .expr import interpolate

                return interpolate(value, self.ctx.scope)
            return value
        for spec in self.spec.params:
            if spec.name == name:
                if spec.default is not None:
                    return spec.default
                break
        return default

    def resolve(self, name: str, default: Any = None) -> Any:
        """Param value, falling back to the run scope under the same name."""
        value = self.param(name, _UNSET)
        if value is not _UNSET:
            return value
        return self.ctx.scope.get(name) if self.ctx else default

    def target_name(self, default: str = "wechat") -> str:
        """The app this node drives: its own ``target``, else the param, else the default."""
        return self.target or str(self.param("target", "") or default)

    def locate_mode(self, default: str = "auto") -> str:
        """The path chosen at design time for this node.

        Precedence: the node's own ``path`` field, then a legacy ``mode`` param,
        then ``default``. The field wins because it is the declaration the canvas
        shows; the param is kept so a graph authored before the field existed
        still runs with the behaviour its author chose.
        """
        if self.path:
            return self.path
        return str(self.param("mode", "") or default)

    ctx: FlowContext

    def execute(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def shutdown(self) -> None:
        """Release heavy resources when the run ends. Default is a no-op."""


_UNSET = object()


class NodeRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, NodeSpec] = {}
        self._lock = threading.RLock()

    def register(self, spec: NodeSpec) -> NodeSpec:
        # The handler reads its own spec for param defaults, so a spec registered
        # directly (not via the decorator) must be reflected onto the class.
        spec.handler.spec = spec
        with self._lock:
            self._specs[spec.type] = spec
        return spec

    def decorator(self, type: str, label: str, category: str, **kwargs: Any) -> Callable[[Type[BaseNode]], Type[BaseNode]]:
        def wrap(cls: Type[BaseNode]) -> Type[BaseNode]:
            params = kwargs.pop("params", None)
            if params is None:
                params = _params_from_dataclass(cls)
            cls.spec = NodeSpec(
                type=type,
                label=label,
                category=category,
                handler=cls,
                params=list(params),
                outputs=list(kwargs.pop("outputs", ())),
                doc=kwargs.pop("doc", (cls.__doc__ or "").strip()),
                requires=list(kwargs.pop("requires", ())),
                singleton=bool(kwargs.pop("singleton", False)),
                **kwargs,
            )
            return cls

        return wrap

    def get(self, type: str) -> NodeSpec:
        with self._lock:
            if type not in self._specs:
                raise FlowError(f"未注册的节点类型: {type!r}")
            return self._specs[type]

    def has(self, type: str) -> bool:
        with self._lock:
            return type in self._specs

    def types(self) -> list[str]:
        with self._lock:
            return sorted(self._specs)

    def list_specs(self) -> list[NodeSpec]:
        with self._lock:
            return [self._specs[t] for t in sorted(self._specs)]

    def catalogue(self) -> list[dict[str, Any]]:
        """Palette payload for the canvas, grouped by category."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for spec in self.list_specs():
            grouped.setdefault(spec.category, []).append(spec.to_dict())
        return [{"category": name, "nodes": items} for name, items in sorted(grouped.items())]


def _params_from_dataclass(cls: type) -> list[ParamSpec]:
    """Read a node's ``__node_params__`` if it declares one."""
    declared = getattr(cls, "__node_params__", None)
    if declared:
        return list(declared)
    return []


_registry: NodeRegistry | None = None
_registry_lock = threading.Lock()


def get_node_registry() -> NodeRegistry:
    """Process-wide registry, loaded with the built-in node types on first call."""
    global _registry
    with _registry_lock:
        if _registry is None:
            from . import builtin_nodes, control_nodes, system_nodes

            _registry = NodeRegistry()
            builtin_nodes.register_all(_registry)
            # Control flow (loops, error regions, sub-flows) and the two
            # out-of-graph node types live in their own modules because their
            # rules are load-bearing enough to deserve a file each.
            control_nodes.register_all(_registry)
            system_nodes.register_all(_registry)
        return _registry
