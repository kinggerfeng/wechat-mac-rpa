"""Tests for the action recorder and the recording-to-graph translation.

The recorder itself cannot be exercised end to end here: a global NSEvent
monitor only fires for input a human generates, and synthesising one does not
go through the same path. What is tested is everything downstream of that —
the translation into a graph, and the error paths, which are the parts that can
be wrong without anyone noticing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.flow.recorder import ActionRecorder, RecorderError, RecordedAction  # noqa: E402
from src.flow.recording_graph import (  # noqa: E402
    _MAX_SYNTHETIC_WAIT,
    _WAIT_THRESHOLD,
    actions_to_graph,
    actions_to_preview,
)
from src.flow.registry import get_node_registry  # noqa: E402
from src.flow.schema import validate_flow  # noqa: E402


def _act(kind, at=0.0, **kw):
    return RecordedAction(kind=kind, at=at, **kw)


# ─────────────────────────────────────────────────────── graph translation ──

def test_empty_recording_still_produces_an_openable_graph():
    """A graph with no actions must still have an entry, or the editor cannot open it."""
    graph = actions_to_graph([])
    assert graph["entry"]
    assert len(graph["nodes"]) == 2
    issues = validate_flow(graph, get_node_registry().types())
    assert [i for i in issues if i.severity == "error"] == [], issues


def test_clicks_become_click_nodes_in_order():
    actions = [
        _act("click", at=0.0, x=10, y=20),
        _act("click", at=0.5, x=30, y=40),
        _act("click", at=0.9, x=50, y=60),
    ]
    graph = actions_to_graph(actions)
    types = [n["type"] for n in graph["nodes"]]
    assert sorted(types) == ["click", "click", "click", "end", "start"]
    # The action nodes form the chain between start and end.
    chain = [
        n["type"]
        for n in sorted(
            (n for n in graph["nodes"] if n["type"] in ("start", "click", "end")),
            key=lambda n: n["position"]["x"],
        )
    ]
    assert chain == ["start", "click", "click", "click", "end"]
    assert [n["params"]["x"] for n in graph["nodes"] if n["type"] == "click"] == [10, 30, 50]
    issues = validate_flow(graph, get_node_registry().types())
    assert [i for i in issues if i.severity == "error"] == [], issues


def test_generated_graph_is_actually_runnable():
    """The strongest check: the produced graph executes to completion."""
    from src.flow.executor import FlowExecutor
    from src.flow.schema import Flow

    actions = [_act("log_ok", at=0.0)]  # unknown kind -> log node, safe to run
    graph = actions_to_graph(actions)
    flow = Flow(id="rec", name="rec", graph=graph)
    result = FlowExecutor(validate=False).run(flow, "run_rec")
    assert result.status == "ok", result.error


def test_a_pause_becomes_an_explicit_wait_node():
    """Replaying faster than the human would hit the wrong screen."""
    actions = [
        _act("click", at=0.0, x=1, y=1),
        _act("click", at=30.0, x=2, y=2),
    ]
    graph = actions_to_graph(actions)
    waits = [n for n in graph["nodes"] if n["type"] == "wait"]
    assert len(waits) == 1
    assert waits[0]["params"]["seconds"] == pytest.approx(30.0, abs=0.1)
    # And it is actually wired between the two clicks.
    wait_id = waits[0]["id"]
    assert any(e["target"] == wait_id for e in graph["edges"])
    assert any(e["source"] == wait_id for e in graph["edges"])


def test_a_short_gap_does_not_become_a_wait():
    actions = [
        _act("click", at=0.0, x=1, y=1),
        _act("click", at=_WAIT_THRESHOLD - 0.5, x=2, y=2),
    ]
    graph = actions_to_graph(actions)
    assert [n for n in graph["nodes"] if n["type"] == "wait"] == []


def test_a_very_long_pause_is_capped():
    """A flow nobody dares to run is not a faithful recording."""
    actions = [
        _act("click", at=0.0, x=1, y=1),
        _act("click", at=3600.0, x=2, y=2),
    ]
    graph = actions_to_graph(actions)
    waits = [n for n in graph["nodes"] if n["type"] == "wait"]
    assert len(waits) == 1
    assert waits[0]["params"]["seconds"] <= _MAX_SYNTHETIC_WAIT


def test_recorded_clicks_carry_no_path_so_the_author_must_choose():
    """The recorder must not silently pick a path — auto costs a model call."""
    graph = actions_to_graph([_act("click", at=0.0, x=5, y=6)])
    clicks = [n for n in graph["nodes"] if n["type"] == "click"]
    assert clicks
    for node in clicks:
        assert node["path"] is None, "recorder must not choose a locate path"


def test_typing_becomes_one_node_per_burst():
    actions = [_act("type_keys", at=0.0, text="你好")]
    graph = actions_to_graph(actions)
    typed = [n for n in graph["nodes"] if n["type"] == "type_keys"]
    assert len(typed) == 1
    assert typed[0]["params"]["text"] == "你好"


def test_hotkey_carries_keys_not_text():
    actions = [_act("hotkey", at=0.0, modifiers=["cmd", "tab"])]
    graph = actions_to_graph(actions)
    typed = [n for n in graph["nodes"] if n["type"] == "type_keys"]
    assert len(typed) == 1
    assert typed[0]["params"]["keys"] == "cmd+tab"


def test_scroll_keeps_its_signed_delta():
    graph = actions_to_graph([_act("scroll", at=0.0, scroll_delta=-3)])
    scrolls = [n for n in graph["nodes"] if n["type"] == "scroll"]
    assert len(scrolls) == 1
    assert scrolls[0]["params"]["amount"] == -3


def test_double_click_is_distinguished_from_two_clicks():
    graph = actions_to_graph([_act("double_click", at=0.0, x=9, y=9, clicks=2)])
    nodes = [n for n in graph["nodes"] if n["type"] == "click"]
    assert len(nodes) == 1
    assert nodes[0]["params"]["clicks"] == 2


def test_preview_rows_carry_everything_the_panel_needs():
    actions = [_act("click", at=0.0, x=1, y=2), _act("type_keys", at=0.4, text="hi")]
    rows = actions_to_preview(actions)
    assert [r["index"] for r in rows] == [0, 1]
    assert rows[0]["label"] == "点击 (1, 2)"
    assert rows[1]["node_type"] == "type_keys"
    assert rows[1]["params"] == {"text": "hi"}


def test_every_node_id_in_a_graph_is_unique():
    """A duplicate id makes the canvas render two nodes on top of each other."""
    actions = [_act("click", at=float(i)) for i in range(20)]
    graph = actions_to_graph(actions)
    ids = [n["id"] for n in graph["nodes"]]
    assert len(ids) == len(set(ids))
    edge_ids = [e["id"] for e in graph["edges"]]
    assert len(edge_ids) == len(set(edge_ids))


def test_nodes_are_laid_out_left_to_right_without_overlap():
    actions = [_act("click", at=float(i)) for i in range(5)]
    graph = actions_to_graph(actions)
    xs = sorted(n["position"]["x"] for n in graph["nodes"] if n["type"] == "click")
    assert xs == sorted(xs)
    assert len(set(xs)) == len(xs), "two nodes share a column and would overlap"


# ────────────────────────────────────────────────────────── the recorder ──

def test_recorder_reports_not_recording_before_start():
    assert ActionRecorder().recording is False


def test_recorder_starts_and_stops_cleanly_or_says_why_not():
    """Either it works, or it explains itself. It must never fail silently —
    an empty recording that looks like "the user did nothing" is the worst
    possible outcome."""
    recorder = ActionRecorder()
    try:
        recorder.start()
    except RecorderError as exc:
        # No Accessibility permission: the message must name the fix.
        assert "辅助功能" in str(exc)
        return
    try:
        time.sleep(0.2)
        assert recorder.recording is True
    finally:
        actions = recorder.stop()
    assert recorder.recording is False
    assert isinstance(actions, list)
    assert recorder.error == "", f"recording reported an error: {recorder.error}"


def test_starting_twice_is_refused():
    recorder = ActionRecorder()
    try:
        recorder.start()
    except RecorderError:
        return
    try:
        with pytest.raises(RecorderError, match="已经在录制"):
            recorder.start()
    finally:
        recorder.stop()


def test_clear_empties_the_buffer():
    recorder = ActionRecorder()
    recorder._actions.append(_act("click", at=0.0, x=1, y=1))
    assert recorder.actions()
    recorder.clear()
    assert recorder.actions() == []
