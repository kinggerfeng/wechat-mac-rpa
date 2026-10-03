"""Visual RPA flow engine.

A flow is a graph of typed nodes; :mod:`~apps.engine.executor` walks it, the desktop
canvas edits it, and both agree on :mod:`~apps.engine.schema`.

    from apps.engine import get_node_registry, get_store, get_run_manager

    registry = get_node_registry()          # node type catalogue for the canvas
    store = get_store()                     # data/rpa.db
    get_run_manager().submit(flow)          # execute, returns a run id
"""

from .context import FlowContext, FlowScope, evaluate_condition
from .executor import BRANCH_KEY, FlowAborted, FlowExecutor, RunResult, Span
from .registry import BaseNode, NodeRegistry, NodeSpec, ParamSpec, get_node_registry
from .schema import (
    SCHEMA_VERSION,
    Edge,
    Flow,
    FlowError,
    Node,
    NodeAborted,
    NodeError,
    Position,
    Retry,
    ValidationIssue,
    new_id,
    validate_flow,
)
from .store import RpaStore, get_store, new_run_id

__all__ = [
    "BRANCH_KEY",
    "SCHEMA_VERSION",
    "BaseNode",
    "Edge",
    "Flow",
    "FlowAborted",
    "FlowContext",
    "FlowError",
    "FlowExecutor",
    "FlowScope",
    "Node",
    "NodeAborted",
    "NodeError",
    "NodeRegistry",
    "NodeSpec",
    "ParamSpec",
    "Position",
    "Retry",
    "RpaStore",
    "RunResult",
    "Span",
    "ValidationIssue",
    "evaluate_condition",
    "get_node_registry",
    "get_run_manager",
    "get_store",
    "new_id",
    "new_run_id",
    "validate_flow",
]


def get_run_manager():
    """Lazily import and return the process-wide run manager."""
    from .runner import get_run_manager as _get

    return _get()
