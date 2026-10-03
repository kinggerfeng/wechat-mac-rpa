"""``wait_for`` — the condition is about the screen *now*.

Two things this file locks down:

* **A wait must not be satisfiable by stale data.** The node used to read
  ``scope["_wait_text"]``, a key nothing ever writes, so the text condition was
  a dead path that still reported ``文本 '…'`` as if it had checked something.
  It now OCRs the live window. The tests here prove the text condition actually
  reads through the OCR service and that scope data cannot satisfy it.
* **Timeout is a port, not a crash.** Every timeout path has to reach the node
  wired to the ``timeout`` port, so the walk is asserted through the executor
  rather than by calling the node directly.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


from apps.engine.context import FlowContext, FlowScope
from apps.engine.registry import BaseNode, get_node_registry
from apps.engine.schema import NodeAborted, NodeError

cv2 = pytest.importorskip("cv2")
numpy = pytest.importorskip("numpy")


class _FakeOCR:
    """Stands in for ``VisionOCREngine``. ``script`` is a list of texts handed
    out one per ``recognize`` call, so a test can make the text show up on the
    third poll and nowhere else."""

    def __init__(self, script=None, text="", fail=False):
        self.script = list(script or [])
        self.text = text
        self.fail = fail
        self.seen: list[str] = []

    def recognize(self, image_path):
        self.seen.append(image_path)
        if self.fail:
            raise RuntimeError("Vision framework unavailable")
        if self.script:
            return [SimpleNamespace(text=self.script.pop(0), confidence=1.0)]
        return [SimpleNamespace(text=self.text, confidence=1.0)]


def _scene(tmp_path, label: str = "") -> str:
    """A bordered badge with a mark inside it.

    The border and the mark are not decoration: a crop of the flat fill alone
    has near-zero variance, and ``find_template`` reports that as ambiguous
    rather than returning a position. A template test that crops only the
    interior would then be asserting the ambiguity path by accident.
    """
    image = numpy.full((120, 200, 3), 250, numpy.uint8)
    cv2.rectangle(image, (60, 40), (140, 80), (40, 150, 220), -1)   # BGR fill
    cv2.rectangle(image, (60, 40), (140, 80), (30, 30, 30), 2)     # border
    cv2.line(image, (78, 50), (122, 70), (250, 250, 250), 3)      # mark
    path = str(tmp_path / f"scene{label}.png")
    cv2.imwrite(path, image)
    return path


def _node(params, services=None, scope=None) -> BaseNode:
    instance = get_node_registry().get("wait_for").handler(params)
    instance.ctx = FlowContext(scope=scope or FlowScope({}), run_id="r1", flow_id="f1")
    for name, value in (services or {}).items():
        instance.ctx.services[name] = value
    return instance


class TestTextCondition:
    def test_missing_text_is_an_error_not_a_pass(self):
        instance = _node({"text": "", "timeout": 0.2})
        with pytest.raises(NodeError, match="以下之一"):
            instance.execute()

    def test_text_is_read_through_ocr(self, tmp_path):
        scene = _scene(tmp_path)
        ocr = _FakeOCR(text="对方 消息内容")
        instance = _node({"text": "消息内容", "haystack": scene, "timeout": 1},
                         services={"ocr": ocr})
        out = instance.execute()
        assert out["appeared"] is True
        assert out["matched"] is True
        assert out["attempts"] == 1
        assert out["timed_out"] is False
        assert out["__branch__"] == "ok"
        assert ocr.seen == [scene]

    def test_scope_data_cannot_satisfy_the_wait(self, tmp_path):
        """The regression the dead path enabled: a key in scope that looks like
        an OCR result must not count as the text being on screen."""
        scene = _scene(tmp_path)
        ocr = _FakeOCR(text="完全不同的内容")
        scope = FlowScope({"_wait_text": "等待文本ZZZ", "messages": ["等待文本ZZZ"]})
        instance = _node({"text": "等待文本ZZZ", "haystack": scene, "timeout": 0.2},
                         services={"ocr": ocr}, scope=scope)
        out = instance.execute()
        assert out["appeared"] is False
        assert out["timed_out"] is True
        # And it really did read the screen instead of short-circuiting.
        assert ocr.seen and set(ocr.seen) == {scene}

    def test_text_can_appear_late(self, tmp_path):
        scene = _scene(tmp_path)
        ocr = _FakeOCR(script=["", "", "终于出现了"])
        instance = _node({"text": "终于出现了", "haystack": scene,
                          "timeout": 2, "interval": 0.05},
                         services={"ocr": ocr})
        out = instance.execute()
        assert out["appeared"] is True
        assert out["attempts"] == 3
        assert len(ocr.seen) == 3
        assert out["timed_out"] is False

    def test_ocr_failure_is_a_failed_poll_not_a_pass(self, tmp_path):
        scene = _scene(tmp_path)
        instance = _node({"text": "任何文字", "haystack": scene, "timeout": 0.15},
                         services={"ocr": _FakeOCR(fail=True)})
        out = instance.execute()
        assert out["appeared"] is False
        assert "OCR 失败" in out["reason"]


class TestVanish:
    def test_vanish_succeeds_once_the_text_is_gone(self, tmp_path):
        scene = _scene(tmp_path)
        ocr = _FakeOCR(script=["加载中", "加载中", ""])
        instance = _node({"text": "加载中", "state": "vanish", "haystack": scene,
                          "timeout": 2, "interval": 0.05},
                         services={"ocr": ocr})
        out = instance.execute()
        assert out["appeared"] is True
        assert out["matched"] is False      # the needle is indeed absent
        assert out["state"] == "vanish"
        assert out["timed_out"] is False

    def test_vanish_times_out_when_it_stays(self, tmp_path):
        scene = _scene(tmp_path)
        instance = _node({"text": "加载中", "state": "vanish", "haystack": scene,
                          "timeout": 0.15, "interval": 0.05},
                         services={"ocr": _FakeOCR(text="加载中")})
        out = instance.execute()
        assert out["appeared"] is False
        assert out["matched"] is True
        # Never previously reported, and wrong: running out of time is a
        # timeout whichever direction the wait was pointing.
        assert out["timed_out"] is True
        assert out["__branch__"] == "timeout"

    def test_invalid_state_is_rejected(self, tmp_path):
        instance = _node({"text": "x", "state": "flicker",
                          "haystack": _scene(tmp_path), "timeout": 0.2},
                         services={"ocr": _FakeOCR()})
        with pytest.raises(NodeError, match="appear"):
            instance.execute()


class TestPixelConditions:
    def test_image_condition_finds_a_template(self, tmp_path):
        scene = _scene(tmp_path)
        needle = str(tmp_path / "needle.png")
        cv2.imwrite(needle, numpy.ascontiguousarray(
            cv2.imread(scene)[38:83, 58:143]))
        instance = _node({"image": needle, "haystack": scene, "timeout": 1})
        out = instance.execute()
        assert out["appeared"] is True
        assert out["condition"] == "image"

    def test_image_condition_times_out_when_absent(self, tmp_path):
        scene = _scene(tmp_path)
        other = str(tmp_path / "other.png")
        cv2.imwrite(other, numpy.full((120, 200, 3), 90, numpy.uint8))
        needle = str(tmp_path / "absent.png")
        cv2.imwrite(needle, numpy.ascontiguousarray(
            cv2.imread(scene)[38:83, 58:143]))
        instance = _node({"image": needle, "haystack": other,
                          "timeout": 0.2, "interval": 0.05})
        out = instance.execute()
        assert out["appeared"] is False
        assert out["timed_out"] is True

    def test_color_condition_matches(self, tmp_path):
        scene = _scene(tmp_path)
        instance = _node({"color": "220,150,40", "haystack": scene,
                          "timeout": 1, "tolerance": 20})
        out = instance.execute()
        assert out["appeared"] is True

    def test_malformed_color_is_a_failed_poll_with_a_reason(self, tmp_path):
        instance = _node({"color": "255,0", "haystack": _scene(tmp_path), "timeout": 0.1})
        out = instance.execute()
        assert out["appeared"] is False
        assert "三个分量" in out["reason"]

    def test_haystack_falls_back_to_a_fresh_capture(self, tmp_path):
        scene = _scene(tmp_path)
        calls: list[int] = []

        class _Capture:
            def capture(self):
                calls.append(1)
                return SimpleNamespace(image_path=scene)

        ocr = _FakeOCR(text="现截的图")
        instance = _node({"text": "现截的图", "timeout": 1},
                         services={"ocr": ocr, "capture": _Capture()})
        out = instance.execute()
        assert out["appeared"] is True
        assert calls == [1]
        assert ocr.seen == [scene]

    def test_haystack_prefers_the_most_recent_capture_over_a_new_one(self, tmp_path):
        pinned = _scene(tmp_path, "_pinned")
        fresh = _scene(tmp_path, "_fresh")

        class _Capture:
            def capture(self):
                raise AssertionError("wait_for 不该在已有截图时再截一次")

        ocr = _FakeOCR(text="来自 pinned")
        scope = FlowScope({"screenshot_path": pinned})
        instance = _node({"text": "来自 pinned", "timeout": 1},
                         services={"ocr": ocr, "capture": _Capture()}, scope=scope)
        assert instance.execute()["appeared"] is True
        assert ocr.seen == [pinned]

    def test_first_poll_capture_failure_is_fatal_not_a_timeout(self):
        """A screen that cannot be read even once is a setup problem. Reporting
        it as ``timed_out`` would blame the timeout for it."""

        class _Capture:
            def capture(self):
                raise RuntimeError("screencapture: could not create image from window")

        instance = _node({"text": "x", "timeout": 0.1}, services={"capture": _Capture()})
        with pytest.raises(NodeError, match="截图失败"):
            instance.execute()

    def test_later_poll_capture_failure_counts_as_not_yet(self, tmp_path):
        """An app still launching cannot be screenshotted for the first few
        hundred milliseconds; that must not abort the wait."""

        scene = _scene(tmp_path)

        class _Capture:
            calls = 0

            def capture(self):
                type(self).calls += 1
                if type(self).calls in (2, 3):
                    raise RuntimeError("window not on screen yet")
                return SimpleNamespace(image_path=scene)

        # OCR only runs on the polls that managed to read the screen, so the
        # script has one entry per successful capture, not per attempt.
        ocr = _FakeOCR(script=["启动中", "启动中", "启动完成"])
        instance = _node({"text": "启动完成", "timeout": 2, "interval": 0.05},
                         services={"ocr": ocr, "capture": _Capture()})
        out = instance.execute()
        assert out["appeared"] is True
        # Attempts 2 and 3 could not even read the screen; neither may end the
        # wait, so the node polled a third time and only then saw the text.
        assert _Capture.calls == 5
        assert out["attempts"] == 5
        assert len(ocr.seen) == 3


class TestWindowCondition:
    def test_window_present_then_absent(self):
        calls: list[int] = []

        class _Auto:
            def get_window_rect(self, app):
                calls.append(1)
                return (len(calls) >= 3, None, None if len(calls) >= 3 else "未找到窗口")

        instance = _node({"window": "WeChat", "timeout": 2, "interval": 0.05},
                         services={"automation": _Auto()})
        out = instance.execute()
        assert out["appeared"] is True
        assert out["condition"] == "window"
        assert len(calls) == 3


class TestGuards:
    def test_negative_timeout_is_rejected(self):
        with pytest.raises(NodeError, match="不能为负"):
            _node({"text": "x", "timeout": -1}).execute()

    def test_abort_stops_the_poll(self, tmp_path):
        scene = _scene(tmp_path)
        instance = _node({"text": "永远不会出现", "haystack": scene,
                          "timeout": 30, "interval": 0.05},
                         services={"ocr": _FakeOCR(text="别的东西")})
        instance.ctx.request_abort()
        with pytest.raises(NodeAborted):
            instance.execute()

    def test_elapsed_is_reported_not_invented(self, tmp_path):
        scene = _scene(tmp_path)
        started = time.time()
        instance = _node({"text": "不会命中", "haystack": scene,
                          "timeout": 0.3, "interval": 0.05},
                         services={"ocr": _FakeOCR()})
        out = instance.execute()
        assert 0.25 <= out["elapsed"] <= time.time() - started + 0.1


class TestThroughTheExecutor:
    """The node returning ``__branch__`` is not the same as the walk that
    follows it. These assert the edge itself: a ``timeout`` port the executor
    never resolves looks exactly like a working node in a unit test."""

    @staticmethod
    def _graph(tmp_path):
        return {
            "version": 1,
            "entry": "wait",
            "nodes": [
                {"id": "wait", "type": "wait_for",
                 "params": {"text": "永远不会出现", "haystack": _scene(tmp_path),
                            "timeout": 0.2, "interval": 0.05}},
                {"id": "hit", "type": "set_var", "params": {"name": "took", "value": "ok"}},
                {"id": "miss", "type": "set_var", "params": {"name": "took", "value": "timeout"}},
            ],
            "edges": [
                {"id": "e1", "source": "wait", "source_port": "ok", "target": "hit"},
                {"id": "e2", "source": "wait", "source_port": "timeout", "target": "miss"},
            ],
        }

    @staticmethod
    def _run(graph, ocr):
        from apps.engine.executor import FlowExecutor
        from apps.engine.schema import Flow

        executor = FlowExecutor(get_node_registry())
        executor.setup_context = lambda ctx: ctx.services.__setitem__("ocr", ocr)
        return executor.run(Flow(id="f", name="f", graph=graph), "r1")

    def test_timeout_port_reaches_its_target(self, tmp_path):
        result = self._run(self._graph(tmp_path), _FakeOCR(text="别的东西"))
        assert result.status == "ok", result.error
        assert result.scope["took"] == "timeout"
        assert result.scope["appeared"] is False
        assert result.scope["timed_out"] is True
        assert result.failed_node is None

    def test_ok_port_reaches_its_target_when_the_text_shows_up(self, tmp_path):
        result = self._run(self._graph(tmp_path), _FakeOCR(text="永远不会出现了"))
        assert result.status == "ok", result.error
        assert result.scope["took"] == "ok"
        assert result.scope["appeared"] is True

    def test_each_node_emits_one_span_pair(self, tmp_path):
        """Two spans per node — ``running`` then the final status — so a
        duplicate entry in a trace is not a node that ran twice."""
        graph = self._graph(tmp_path)
        spans: list[tuple[str, str]] = []
        from apps.engine.executor import FlowExecutor
        from apps.engine.schema import Flow

        executor = FlowExecutor(get_node_registry())
        executor.setup_context = lambda ctx: ctx.services.__setitem__(
            "ocr", _FakeOCR(text="别的东西"))
        executor.span_hook = lambda s: spans.append((s.node_id, s.status))
        executor.run(Flow(id="f", name="f", graph=graph), "r1")

        assert [n for n, _ in spans].count("wait") == 2
        assert dict(spans)["wait"] == "ok"
        assert [n for n, _ in spans if n == "miss"] == ["miss", "miss"]
        assert "hit" not in [n for n, _ in spans]
