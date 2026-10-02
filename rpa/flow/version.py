"""Which engine wrote this flow, and will it still run?

Every RPA product accumulates this problem. The one that hurt most is not a
missing feature but a quiet behaviour change: a node keeps its name and its
parameters, the flow editor shows no warning, and the run behaves differently
than it did last month. The failure surfaces weeks later, in someone else's
data, with nothing to compare against.

An independent review of a comparable commercial tool found exactly this —
frequent releases, command behaviour shifting between versions, existing flows
requiring patches to keep working. With 74 nodes here it is a matter of when.

Two identifiers, because they answer different questions:

``ENGINE_VERSION``
    A manual contract. Bumped when a behaviour change is *intended* and
    existing flows are *expected* to need attention. This is the number an
    operator reads.

``node_fingerprint``
    Derived from what is actually registered — the node types and the
    parameters each one accepts. It changes when a signature changes, whether
    or not anyone remembered to bump ``ENGINE_VERSION``. A mismatch here is a
    *warning*, never a refusal: a renamed parameter should not stop a
    production run at 3am on a Sunday.

Nothing here refuses to run. A flow saved before this existed carries neither
field, and reporting every one of those as incompatible would be a lie — they
are simply unstamped, and ``UNSTAMPED`` is a third answer beside ``MATCH`` and
``MISMATCH``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

#: The engine contract. Bump on a deliberate behaviour change.
ENGINE_VERSION = "1"

Compatibility = Literal["MATCH", "MISMATCH", "UNSTAMPED", "FUTURE"]

STATUS_TEXT: dict[str, str] = {
    "MATCH": "与当前引擎一致",
    "MISMATCH": "由不同版本的引擎保存，节点行为可能已变化",
    "UNSTAMPED": "保存于引入版本标记之前，无法判断来源版本",
    "FUTURE": "由更新版本的引擎保存，当前引擎可能不认识其中的节点",
}


def node_fingerprint(specs: dict[str, Any]) -> str:
    """A stable digest of what the registry offers.

    Derived rather than declared on purpose: a hand-maintained number is a
    number someone forgets. This changes whenever a node type is added or
    removed, and whenever a node's accepted parameters change — which are the
    two changes that can make an old flow behave differently.

    Deliberately insensitive to descriptions, labels and ordering, so that
    editing help text does not make every flow in the database look stale.
    """
    shape: dict[str, list[str]] = {}
    for name, spec in sorted(specs.items()):
        params = spec if isinstance(spec, (list, tuple, set)) else getattr(spec, "params", ())
        shape[name] = sorted(str(p) for p in params)
    payload = json.dumps(shape, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def stamp(graph: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    """Return the graph with the provenance this engine is able to attest to.

    Mutates in place, because the callers already treat the graph as theirs and
    a copy would be surprising at the save boundary.
    """
    graph["engine_version"] = ENGINE_VERSION
    graph["node_fingerprint"] = fingerprint
    return graph


def check(graph: dict[str, Any], fingerprint: str) -> tuple[Compatibility, str]:
    """Compare a stored graph against the running engine.

    Returns a status and a human-readable reason. Never raises: this is
    reporting, not enforcement, and an unusable flow is still more useful than
    a flow that refuses to start.
    """
    stamped_engine = graph.get("engine_version")
    stamped_nodes = graph.get("node_fingerprint")

    if not stamped_engine and not stamped_nodes:
        return "UNSTAMPED", STATUS_TEXT["UNSTAMPED"]

    if _version_tuple(stamped_engine) > _version_tuple(ENGINE_VERSION):
        return "FUTURE", (
            f"{STATUS_TEXT['FUTURE']}"
            f"（流程保存于 {stamped_engine}，当前 {ENGINE_VERSION}）"
        )

    if stamped_nodes and stamped_nodes != fingerprint:
        return "MISMATCH", (
            f"{STATUS_TEXT['MISMATCH']}"
            f"（保存时 {stamped_nodes}，当前 {fingerprint}）"
        )

    return "MATCH", STATUS_TEXT["MATCH"]


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(value).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)
