"""Turns a recorded action list into a flow graph.

A recording is a list of coordinates and text; a flow is a graph with a start, an
end, and a decision at every place the user might have taken a different path.
This module does the mechanical part of that translation and refuses to guess
at the rest.

The one thing it will not do is invent structure. It lays the actions out as a
straight line and marks, as an explicit gap the author must fill, every place
where consecutive actions are far enough apart in time to suggest the user was
reading, waiting, or deciding. Those become ``wait`` nodes with the real gap as
their duration — a literal record of what happened — rather than being smoothed
away. A recording that silently drops its own pauses would replay faster than
the human and hit the wrong screen.

Coordinates become action nodes, not element references: naming an element is
:mod:`apps.engine.elements`' job and needs a deliberate choice by the author.
"""

from __future__ import annotations

import time
from typing import Any

from .recorder import RecordedAction
from .schema import new_id

#: Column spacing between generated nodes. Wide enough that a node card and its
#: label do not overlap at the canvas's default zoom.
_COLUMN = 260

#: Row height, enough for two stacked nodes without the edge labels colliding.
_ROW = 140

#: A gap at least this long becomes an explicit ``wait`` node.
_WAIT_THRESHOLD = 2.0

#: The longest wait to synthesise. A user who walked away for ten minutes did not
#: author a ten-minute sleep, and a flow that carries one is a flow nobody
#: dares to run.
_MAX_SYNTHETIC_WAIT = 60.0


def _node(
    node_id: str,
    node_type: str,
    name: str,
    x: float,
    y: float,
    *,
    params: dict[str, Any] | None = None,
    outputs: list[str] | None = None,
    on_error: str = "fail",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "position": {"x": x, "y": y},
        "params": params or {},
        "retry": {"max": 0, "delay": 0.0},
        "timeout": None,
        "on_error": on_error,
        "outputs": outputs or [],
        "disabled": False,
        # Action nodes are path-aware (a click may resolve through the element
        # library or the vision model), but a recording says nothing about which
        # the author wants, so the field is left unset and the canvas marks it
        # "必选" rather than the recorder guessing and quietly choosing a model.
        "path": None,
        "target": None,
    }


def _label_for(action: RecordedAction) -> str:
    if action.kind == "click":
        return f"点击 ({action.x}, {action.y})"
    if action.kind == "double_click":
        return f"双击 ({action.x}, {action.y})"
    if action.kind == "scroll":
        return f"滚动 {action.scroll_delta:+d}"
    if action.kind == "type_keys":
        preview = action.text if len(action.text) <= 12 else action.text[:12] + "…"
        return f"输入「{preview}」"
    if action.kind == "hotkey":
        return "按键 " + "+".join(action.modifiers or [])
    return action.kind


def actions_to_graph(
    actions: list[RecordedAction],
    *,
    name: str = "录制流程",
    description: str = "",
) -> dict[str, Any]:
    """Build a flow graph from a recording.

    Returns a graph dict ready for :func:`apps.engine.schema.validate_flow`. An
    empty recording still yields a valid one-node graph with a start and an end,
    because an empty canvas with a dangling entry node is not something the
    editor can open.
    """
    stamp = int(time.time())
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    start_id = f"rec_start_{stamp}"
    end_id = f"rec_end_{stamp}"
    nodes.append(_node(start_id, "start", "开始", 0, 0))
    nodes.append(_node(end_id, "end", "结束", _COLUMN * (len(actions) + 2), 0, params={"status": "ok"}))

    previous = start_id
    for index, action in enumerate(actions):
        node_id = f"rec_{stamp}_{index}"
        node_type = action.to_node_type()
        params = action.to_node_params()

        if node_type == "click":
            outputs = ["ok"]
        elif action.kind == "hotkey":
            # A hotkey replays through type_keys, whose "keys" param is what
            # carries a key combination rather than literal text.
            params = {"keys": "+".join(action.modifiers or ["esc"])}
            outputs = []
        else:
            outputs = []

        nodes.append(
            _node(
                node_id,
                node_type,
                _label_for(action),
                _COLUMN * (index + 1),
                0,
                params=params,
                outputs=outputs,
            )
        )
        edges.append(
            {
                "id": f"re_{stamp}_{index}",
                "source": previous,
                "source_port": "ok",
                "target": node_id,
                "condition": None,
            }
        )
        previous = node_id

        # A pause long enough to matter is recorded, not smoothed away: replaying
        # a human's workflow faster than they performed it hits the wrong screen.
        if index + 1 < len(actions):
            gap = actions[index + 1].at - action.at
            if gap >= _WAIT_THRESHOLD:
                wait_id = f"rec_wait_{stamp}_{index}"
                nodes.append(
                    _node(
                        wait_id,
                        "wait",
                        f"等待 {min(gap, _MAX_SYNTHETIC_WAIT):.0f}s",
                        _COLUMN * (index + 1) + _COLUMN / 2,
                        _ROW,
                        params={"seconds": round(min(gap, _MAX_SYNTHETIC_WAIT), 1)},
                    )
                )
                edges.append(
                    {
                        "id": f"rw_{stamp}_{index}",
                        "source": node_id,
                        "source_port": "ok",
                        "target": wait_id,
                        "condition": None,
                    }
                )
                previous = wait_id

    edges.append(
        {
            "id": f"rend_{stamp}",
            "source": previous,
            "source_port": "ok",
            "target": end_id,
            "condition": None,
        }
    )

    summary = f"由录制生成：{len(actions)} 个动作"
    return {
        "version": 1,
        "entry": start_id,
        "default_path": None,
        "name": name,
        "description": description or summary,
        "variables": {},
        "nodes": nodes,
        "edges": edges,
    }


def actions_to_preview(actions: list[RecordedAction]) -> list[dict[str, Any]]:
    """A flat, display-ready list for the recorder panel before it is committed."""
    rows: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        row = action.to_dict()
        row["index"] = index
        row["label"] = _label_for(action)
        row["node_type"] = action.to_node_type()
        row["params"] = action.to_node_params()
        rows.append(row)
    return rows


def new_recording_id() -> str:
    return new_id("rec")
