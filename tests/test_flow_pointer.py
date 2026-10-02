"""Pointer gesture nodes and the pyautogui-backed primitives.

Two things are pinned here. First, the gesture → primitive mapping: a node that
reports ``clicked: true`` must have asked for the right underlying call, or a
right-click silently behaving as a left-click is invisible until a menu fails
to open. Second, that a failed gesture is reported as failed rather than
swallowed — a gesture returning success while the pointer never moved is the
same silent-wrong-result class this project keeps fixing.
"""

from __future__ import annotations

import sys

from typing import Any

import pytest


from rpa.action.system_automation import (
    MacOSSystemAutomation,
    NoOpSystemAutomation,
    SystemAutomation,
)
from rpa.flow.executor import FlowExecutor
from rpa.flow.schema import Flow


class RecordingAutomation(SystemAutomation):
    """Captures the primitive calls a node makes."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result = True

    def _log(self, name: str, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((name, args, kwargs))
        return self.result

    def activate_app(self, app_name: str) -> bool:
        return self._log("activate_app", app_name)

    def get_frontmost_app(self, app_name: str) -> tuple[bool, str]:
        return self._log("get_frontmost_app", app_name), app_name

    def get_window_rect(self, app_name: str):
        return self._log("get_window_rect", app_name), None, ""

    def click_at(self, x, y, button="left", count=1) -> bool:
        return self._log("click_at", x, y, button=button, count=count)

    def move_to(self, x, y) -> bool:
        return self._log("move_to", x, y)

    def drag_to(self, x1, y1, x2, y2, duration_ms=500) -> bool:
        return self._log("drag_to", x1, y1, x2, y2, duration_ms=duration_ms)

    def scroll_at(self, x, y, clicks) -> bool:
        return self._log("scroll_at", x, y, clicks)

    def set_window_rect(self, app_name: str, rect) -> bool:
        return self._log("set_window_rect", app_name, rect)

    def minimize_window(self, app_name: str) -> bool:
        return self._log("minimize_window", app_name)

    def maximize_window(self, app_name: str) -> bool:
        return self._log("maximize_window", app_name)

    def close_window(self, app_name: str) -> bool:
        return self._log("close_window", app_name)

    def send_keys(self, key_spec: str) -> bool:
        return self._log("send_keys", key_spec)

    def run_applescript(self, script: str, timeout: int = 5):
        return self._log("run_applescript", script), 0, "", ""

    def set_clipboard_text(self, text: str) -> bool:
        return self._log("set_clipboard_text", text)

    def get_clipboard_text(self) -> tuple[bool, str]:
        return self._log("get_clipboard_text"), ""

    def capture_screen(self, rect, output_path, window_id=None):
        return self._log("capture_screen", rect, output_path, window_id=window_id), ""


def _flow(node_type: str, params: dict) -> Flow:
    return Flow(id="f_ptr", name="pointer", graph={
        "version": 1, "entry": "n1", "default_path": "auto", "variables": {},
        "nodes": [{
            "id": "n1", "type": node_type, "name": node_type,
            "position": {"x": 0, "y": 0}, "params": params,
            "retry": {"max": 0, "delay": 0}, "timeout": None, "on_error": "fail",
            "outputs": ["x", "y", "gesture"],
            "disabled": False, "path": None, "target": None,
        }],
        "edges": [],
    })


def _run(node_type: str, params: dict, automation=None):
    auto = automation or RecordingAutomation()
    flow = _flow(node_type, params)
    executor = FlowExecutor(setup_context=lambda ctx: ctx.put_service("automation", auto))
    result = executor.run(flow, "run_ptr")
    return result, auto


@pytest.fixture()
def gui(monkeypatch):
    """A stand-in that records calls instead of driving a real cursor."""
    import types

    calls: list[tuple] = []

    def rec(name):
        def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
        return fn

    module = types.SimpleNamespace(
        FAILSAFE=True, PAUSE=0.1,
        moveTo=rec("moveTo"), click=rec("click"), doubleClick=rec("doubleClick"),
        tripleClick=rec("tripleClick"), dragTo=rec("dragTo"), mouseUp=rec("mouseUp"),
        scroll=rec("scroll"), hotkey=rec("hotkey"), press=rec("press"),
    )
    monkeypatch.setitem(sys.modules, "pyautogui", module)
    auto = MacOSSystemAutomation()
    auto._gui_module = module
    return auto, calls


class TestPointerNodes:
    def test_hover_moves_without_clicking(self):
        result, auto = _run("hover", {"x": 100, "y": 200})
        assert result.status == "ok", result.error
        assert auto.calls == [("move_to", (100, 200), {})]
        assert result.scope["n1"]["moved"] is True

    def test_right_click_sends_the_right_button(self):
        """A right-click that presses left opens nothing and reports success."""
        result, auto = _run("right_click", {"x": 10, "y": 20})
        assert result.status == "ok", result.error
        name, args, kwargs = auto.calls[0]
        assert name == "click_at"
        assert kwargs["button"] == "right"
        assert result.scope["n1"]["clicked"] is True

    def test_double_click_sends_count_two(self):
        result, auto = _run("double_click", {"x": 10, "y": 20})
        assert result.status == "ok", result.error
        assert auto.calls[0][2]["count"] == 2

    def test_drag_passes_both_endpoints(self):
        result, auto = _run("drag", {"x": 1, "y": 2, "x2": 30, "y2": 40})
        assert result.status == "ok", result.error
        name, args, kwargs = auto.calls[0]
        assert name == "drag_to"
        assert args == (1, 2, 30, 40)
        assert result.scope["n1"]["dropped"] is True

    def test_drag_without_destination_is_an_error(self):
        result, _ = _run("drag", {"x": 1, "y": 2})
        assert result.status == "error"

    def test_scroll_passes_clicks_through(self):
        result, auto = _run("scroll", {"x": 5, "y": 6, "clicks": -4})
        assert result.status == "ok", result.error
        assert auto.calls[0] == ("scroll_at", (5, 6, -4), {})

    def test_scroll_zero_is_an_error(self):
        """Zero clicks is almost always a forgotten parameter, not a no-op."""
        result, _ = _run("scroll", {"x": 5, "y": 6, "clicks": 0})
        assert result.status == "error"

    def test_gesture_echoed_in_output(self):
        for node_type in ("hover", "right_click", "double_click"):
            result, _ = _run(node_type, {"x": 1, "y": 1})
            assert result.scope["n1"]["gesture"] == node_type

    def test_resolved_coordinate_is_reported(self):
        result, _ = _run("hover", {"x": 77, "y": 88})
        assert (result.scope["n1"]["x"], result.scope["n1"]["y"]) == (77, 88)
        assert result.scope["n1"]["source"] == "fixed"

    def test_located_x_from_previous_node_is_reused(self):
        auto = RecordingAutomation()
        flow = Flow(id="f2", name="p", graph={
            "version": 1, "entry": "n1", "default_path": "auto", "variables": {},
            "nodes": [
                {"id": "n1", "type": "set_var", "name": "s", "position": {"x": 0, "y": 0},
                 "params": {"name": "located_x", "value": "300"}, "retry": {"max": 0, "delay": 0},
                 "timeout": None, "on_error": "fail", "outputs": ["value"],
                 "disabled": False, "path": None, "target": None},
                {"id": "n1b", "type": "set_var", "name": "s2", "position": {"x": 0, "y": 0},
                 "params": {"name": "located_y", "value": "400"}, "retry": {"max": 0, "delay": 0},
                 "timeout": None, "on_error": "fail", "outputs": ["value"],
                 "disabled": False, "path": None, "target": None},
                {"id": "n2", "type": "hover", "name": "h", "position": {"x": 1, "y": 0},
                 "params": {}, "retry": {"max": 0, "delay": 0}, "timeout": None,
                 "on_error": "fail", "outputs": ["x", "y", "gesture"],
                 "disabled": False, "path": None, "target": None},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n1b"},
                      {"id": "e2", "source": "n1b", "target": "n2"}],
        })
        result = FlowExecutor(setup_context=lambda ctx: ctx.put_service("automation", auto)).run(flow, "r")
        assert result.status == "ok", result.error
        assert auto.calls[0][0] == "move_to"
        assert auto.calls[0][1] == (300, 400)

    def test_no_target_at_all_is_an_error(self):
        result, _ = _run("hover", {})
        assert result.status == "error"


class TestFailureIsReported:
    """A gesture that did nothing must not report success."""

    def test_failed_move_reports_moved_false(self):
        auto = RecordingAutomation()
        auto.result = False
        result, _ = _run("hover", {"x": 1, "y": 1}, automation=auto)
        assert result.status == "ok", result.error
        assert result.scope["n1"]["moved"] is False

    def test_failed_click_reports_clicked_false(self):
        auto = RecordingAutomation()
        auto.result = False
        result, _ = _run("right_click", {"x": 1, "y": 1}, automation=auto)
        assert result.scope["n1"]["clicked"] is False

    def test_failed_drag_reports_dropped_false(self):
        auto = RecordingAutomation()
        auto.result = False
        result, _ = _run("drag", {"x": 1, "y": 1, "x2": 2, "y2": 2}, automation=auto)
        assert result.scope["n1"]["dropped"] is False


class TestPointerPrimitives:
    """The pyautogui-backed mixin, with the GUI library stubbed."""

    def test_failsafe_is_disabled(self, monkeypatch):
        import types
        module = types.SimpleNamespace(
            FAILSAFE=True, PAUSE=0.1, moveTo=lambda *a, **k: None,
            click=lambda *a, **k: None, doubleClick=lambda *a, **k: None,
            tripleClick=lambda *a, **k: None, dragTo=lambda *a, **k: None,
            mouseUp=lambda *a, **k: None, scroll=lambda *a, **k: None,
        )
        monkeypatch.setitem(sys.modules, "pyautogui", module)
        auto = MacOSSystemAutomation()
        auto._gui_module = None
        auto.move_to(1, 1)
        assert module.FAILSAFE is False
        assert module.PAUSE == 0.0

    def test_double_click_uses_double_primitive(self, gui):
        auto, calls = gui
        auto.click_at(5, 6, count=2)
        assert any(c[0] == "doubleClick" for c in calls)
        assert not any(c[0] == "click" for c in calls)

    def test_single_click_uses_click_primitive(self, gui):
        auto, calls = gui
        auto.click_at(5, 6)
        assert [c[0] for c in calls] == ["moveTo", "click"]

    def test_triple_click(self, gui):
        auto, calls = gui
        auto.click_at(5, 6, count=3)
        assert any(c[0] == "tripleClick" for c in calls)

    def test_unknown_button_is_refused(self, gui):
        auto, calls = gui
        assert auto.click_at(1, 1, button="wheel") is False
        assert calls == []

    def test_drag_moves_then_drags(self, gui):
        auto, calls = gui
        auto.drag_to(1, 2, 30, 40, duration_ms=250)
        assert [c[0] for c in calls] == ["moveTo", "dragTo"]
        assert calls[1][2]["duration"] == 0.25

    def test_drag_releases_button_after_failure(self, gui, monkeypatch):
        """A stuck left button wedges the user's whole desktop."""
        auto, calls = gui

        def boom(*args, **kwargs):
            calls.append(("dragTo", args, kwargs))
            raise RuntimeError("target gone")

        import pyautogui
        pyautogui.dragTo = boom
        assert auto.drag_to(1, 2, 3, 4) is False
        assert any(c[0] == "mouseUp" for c in calls)

    def test_scroll_moves_first(self, gui):
        auto, calls = gui
        auto.scroll_at(7, 8, -3)
        assert [c[0] for c in calls] == ["moveTo", "scroll"]
        assert calls[1][1][0] == -3


class TestNoOp:
    def test_every_primitive_succeeds(self):
        """The NoOp backend exists so dry runs and tests never touch a screen."""
        noop = NoOpSystemAutomation()
        assert noop.click_at(1, 1, "right", 2) is True
        assert noop.move_to(1, 1) is True
        assert noop.drag_to(1, 1, 2, 2, 100) is True
        assert noop.scroll_at(1, 1, 3) is True

    def test_macos_backend_is_a_system_automation(self):
        assert isinstance(MacOSSystemAutomation(), SystemAutomation)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestKeySpecParsing:
    """``cmd+v`` must reach the platform layer, not an AppleScript template.

    The old implementation interpolated the caller's string straight into
    ``tell process "WeChat" ...``, where ``cmd`` is not a valid identifier —
    osascript returned -2753 and the failure was reported as a success.
    """

    @pytest.mark.parametrize("spec,expected", [
        ("cmd+v", (["command"], "v")),
        ("command+v", (["command"], "v")),
        ("ctrl+shift+t", (["ctrl", "shift"], "t")),
        ("enter", ([], "enter")),
        ("esc", ([], "esc")),
        ("escape", ([], "esc")),
        ("f5", ([], "f5")),
        ("cmd+shift+4", (["command", "shift"], "4")),
        ("a", ([], "a")),
        ("cmd+alt+delete", (["command", "alt"], "delete")),
    ])
    def test_valid_specs(self, spec, expected):
        from rpa.action.system_automation import PointerMixin
        assert PointerMixin.parse_key_spec(spec) == expected

    @pytest.mark.parametrize("bad", ["", "   ", "cmd", "super+v", "cmdx", "cmd+"])
    def test_invalid_specs_are_refused(self, bad):
        from rpa.action.system_automation import PointerMixin
        with pytest.raises(ValueError):
            PointerMixin.parse_key_spec(bad)

    def test_repeated_modifier_is_collapsed(self):
        from rpa.action.system_automation import PointerMixin
        assert PointerMixin.parse_key_spec("cmd+command+a") == (["command"], "a")

    def test_send_keys_uses_hotkey_for_a_combination(self, gui):
        from rpa.action.system_automation import PointerMixin
        auto, calls = gui
        assert auto.send_keys("cmd+v") is True
        assert [c[0] for c in calls] == ["hotkey"]
        assert calls[0][1] == ("command", "v")

    def test_send_keys_uses_press_for_a_bare_key(self, gui):
        auto, calls = gui
        assert auto.send_keys("enter") is True
        assert [c[0] for c in calls] == ["press"]

    def test_unparseable_spec_reports_failure_not_a_keystroke(self, gui):
        """A typo must not turn into literal text typed into a focused field."""
        auto, calls = gui
        assert auto.send_keys("super+v") is False
        assert calls == []

    def test_noop_accepts_every_spec(self):
        assert NoOpSystemAutomation().send_keys("cmd+v") is True


class TestWindowNodes:
    """Window management nodes.

    ``close_window`` is the reason this class exists in a test file at all: it
    is the one irreversible action, and its result key has to be exactly
    ``closed`` — a canvas bound to ``close`` would show no output and the
    operator would not know the window had actually gone.
    """

    def test_window_rect_reports_geometry(self):
        from rpa.models.base import Rect

        auto = RecordingAutomation()

        class WithRect(RecordingAutomation):
            def get_window_rect(self, app_name):
                return True, Rect(x=10, y=20, width=800, height=600), ""

        result, _ = _run("window_rect", {"app": "WeChat"}, automation=WithRect())
        assert result.status == "ok", result.error
        out = result.scope["n1"]
        assert (out["x"], out["y"], out["width"], out["height"]) == (10, 20, 800, 600)
        assert out["found"] is True

    def test_window_rect_missing_window_is_not_an_error(self):
        """A closed app is a normal outcome, not a crash — the caller branches."""
        class NoWindow(RecordingAutomation):
            def get_window_rect(self, app_name):
                return False, None, "没有窗口"

        result, _ = _run("window_rect", {}, automation=NoWindow())
        assert result.status == "ok", result.error
        assert result.scope["n1"]["found"] is False
        assert result.scope["n1"]["width"] is None

    def test_set_window_rect_passes_geometry(self):
        result, auto = _run("set_window_rect", {"x": 1, "y": 2, "width": 640, "height": 480})
        assert result.status == "ok", result.error
        name, args, _ = auto.calls[0]
        assert name == "set_window_rect"
        assert (args[1].x, args[1].y, args[1].width, args[1].height) == (1, 2, 640, 480)
        assert result.scope["n1"]["moved"] is True

    @pytest.mark.parametrize("params", [
        {"width": 0, "height": 100},
        {"width": 100, "height": 0},
        {"width": -5, "height": 100},
    ])
    def test_set_window_rect_refuses_a_degenerate_size(self, params):
        """macOS accepts a zero-size window; the operator would never see it."""
        result, auto = _run("set_window_rect", params)
        assert result.status == "error"
        assert auto.calls == []

    @pytest.mark.parametrize("node,method,key", [
        ("minimize_window", "minimize_window", "minimized"),
        ("maximize_window", "maximize_window", "maximized"),
        ("close_window", "close_window", "closed"),
    ])
    def test_single_call_window_actions(self, node, method, key):
        result, auto = _run(node, {"app": "WeChat"})
        assert result.status == "ok", result.error
        assert auto.calls == [(method, ("WeChat",), {})]
        assert result.scope["n1"][key] is True

    @pytest.mark.parametrize("node,key", [
        ("minimize_window", "minimized"),
        ("close_window", "closed"),
    ])
    def test_failure_is_reported_not_swallowed(self, node, key):
        auto = RecordingAutomation()
        auto.result = False
        result, _ = _run(node, {}, automation=auto)
        assert result.scope["n1"][key] is False
