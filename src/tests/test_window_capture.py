#!/usr/bin/env python3
"""Tests for L2 WindowCapture module."""

import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.action.system_automation import SystemAutomation
from src.capture.window_capture import CaptureResult, WindowCapture, WindowNotFoundError
from src.models.base import Rect


class MockCaptureAutomation(SystemAutomation):
    """可编程 SystemAutomation mock，用于 WindowCapture 测试。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.capture_success = True
        self.capture_error = ""

    def _log(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def activate_app(self, app_name: str) -> bool:
        self._log("activate_app", app_name)
        return True

    def get_frontmost_app(self, app_name: str) -> tuple[bool, str]:
        self._log("get_frontmost_app", app_name)
        return True, app_name

    def get_window_rect(self, app_name: str) -> tuple[bool, Rect | None, str]:
        self._log("get_window_rect", app_name)
        return True, Rect(x=0, y=0, width=800, height=600), ""

    def click_at(self, x: int, y: int, button: str = "left", count: int = 1) -> bool:
        self._log("click_at", x, y, button=button, count=count)
        return True

    def move_to(self, x: int, y: int) -> bool:
        self._log("move_to", x, y)
        return True

    def drag_to(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> bool:
        self._log("drag_to", x1, y1, x2, y2, duration_ms=duration_ms)
        return True

    def scroll_at(self, x: int, y: int, clicks: int) -> bool:
        self._log("scroll_at", x, y, clicks)
        return True

    def set_window_rect(self, app_name: str, rect) -> bool:
        self._log("set_window_rect", app_name, rect)
        return True

    def minimize_window(self, app_name: str) -> bool:
        self._log("minimize_window", app_name)
        return True

    def maximize_window(self, app_name: str) -> bool:
        self._log("maximize_window", app_name)
        return True

    def close_window(self, app_name: str) -> bool:
        self._log("close_window", app_name)
        return True

    def send_keys(self, key_spec: str) -> bool:
        self._log("send_keys", key_spec)
        return True

    def run_applescript(self, script: str, timeout: int = 5) -> tuple[int, str, str]:
        self._log("run_applescript", script, timeout=timeout)
        return 0, "", ""

    def set_clipboard_text(self, text: str) -> bool:
        self._log("set_clipboard_text", text)
        return True

    def get_clipboard_text(self) -> tuple[bool, str]:
        self._log("get_clipboard_text")
        return True, ""

    def capture_screen(
        self,
        rect: Rect,
        output_path: str,
        window_id: int | None = None,
    ) -> tuple[bool, str]:
        self._log("capture_screen", rect, output_path, window_id=window_id)
        return self.capture_success, self.capture_error


class TestWindowCapture(unittest.TestCase):
    """Test WindowCapture with mocked Quartz and SystemAutomation."""

    def setUp(self):
        self.automation = MockCaptureAutomation()
        self.capture = WindowCapture(automation=self.automation)

    def _make_mock_window(self, owner, x, y, width, height, window_id=1):
        """Helper to build a Quartz window info dict."""
        return {
            'kCGWindowOwnerName': owner,
            'kCGWindowBounds': {
                'X': x,
                'Y': y,
                'Width': width,
                'Height': height,
            },
            'kCGWindowNumber': window_id,
        }

    def test_validate_skips_when_ocr_engine_import_fails(self):
        """Validation is a second gate, not a single point of failure.

        The primary OCR runs through qwen-vl-ocr, so an unavailable local
        engine must not block capture.
        """
        original_import = __import__

        def fail_ocr(name, *args, **kwargs):
            if name == "src.ocr.vision_ocr":
                raise ImportError("broken optional dependency")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_ocr):
            self.assertTrue(self.capture._validate_wechat_screenshot("unused.png"))

    def test_validate_rejects_image_without_wechat_markers(self):
        """Text alone is not proof of WeChat — any app has text.

        The old fallback was ``bool(left_text)``, which passed on whatever was
        on screen; a marker whitelist is what makes the check mean something.
        """
        elements = [MagicMock(text=t) for t in ("Safari", "Bookmarks", "Reading List")]
        with patch("src.ocr.vision_ocr.VisionOCREngine") as mock_engine:
            mock_engine.return_value.recognize.return_value = elements
            self.assertFalse(self.capture._validate_wechat_screenshot("any.png"))

    def test_validate_accepts_image_with_wechat_marker(self):
        elements = [
            MagicMock(text="Q. 搜索"),
            MagicMock(text="公众号"),
            MagicMock(text="深圳大件事"),
        ]
        with patch("src.ocr.vision_ocr.VisionOCREngine") as mock_engine:
            mock_engine.return_value.recognize.return_value = elements
            self.assertTrue(self.capture._validate_wechat_screenshot("wechat.png"))

    def test_validate_rejects_blank_image(self):
        """No text at all means the pixels are not a usable WeChat window."""
        with patch("src.ocr.vision_ocr.VisionOCREngine") as mock_engine:
            mock_engine.return_value.recognize.return_value = []
            self.assertFalse(self.capture._validate_wechat_screenshot("blank.png"))

    def test_window_capture_failure_falls_back_to_region(self):
        rect = Rect(x=100, y=200, width=1200, height=900)
        self.automation.capture_screen = MagicMock(
            side_effect=[(False, "window capture failed"), (True, "")]
        )

        with patch("src.capture.window_capture.time.sleep") as mock_sleep:
            self.capture._do_capture(rect, window_id=47)

        self.automation.capture_screen.assert_has_calls([
            call(rect, self.capture.output_path, window_id=47),
            call(rect, self.capture.output_path, window_id=None),
        ])
        self.assertIn(
            ("activate_app", ("WeChat",), {}),
            self.automation.calls,
        )
        # Fallback is only trusted after frontmost is confirmed, so the
        # verification round trip is part of the contract, not an extra.
        self.assertIn(("get_frontmost_app", ("WeChat",), {}), self.automation.calls)
        self.assertTrue(mock_sleep.called)

    def test_region_capture_refused_when_wechat_not_foreground(self):
        """A region capture while another app is frontmost yields the wrong pixels.

        The old code activated WeChat, slept a fixed 0.5s and captured anyway,
        so an activation that silently did nothing produced a screenshot of
        whatever was on top — reported as success. Refusing is the whole point.
        """
        rect = Rect(x=100, y=200, width=1200, height=900)
        self.automation.capture_screen = MagicMock(
            side_effect=[(False, "could not create image from window")]
        )
        self.automation.activate_app = MagicMock(return_value=False)
        self.automation.get_frontmost_app = MagicMock(return_value=(False, "Safari"))

        with patch("src.capture.window_capture.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self.capture._do_capture(rect, window_id=47)

        self.assertIn("前台", str(ctx.exception))
        # The region attempt must never happen: it is the one that lies.
        self.automation.capture_screen.assert_called_once()
        self.assertEqual(
            self.automation.capture_screen.call_args.kwargs.get("window_id"), 47
        )

    @patch.object(WindowCapture, '_validate_wechat_screenshot', return_value=True)
    @patch('src.capture.window_capture.Quartz')
    @patch('src.capture.window_capture.AppKit')
    def test_capture_success_wechat_en(self, mock_appkit, mock_quartz, mock_validate):
        """Successful capture of English-named WeChat window."""
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            self._make_mock_window('Safari', 0, 0, 1200, 800),
            self._make_mock_window('WeChat', 100, 200, 1200, 900),
        ]
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 2
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.kCGWindowOwnerName = 'kCGWindowOwnerName'
        mock_quartz.kCGWindowBounds = 'kCGWindowBounds'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'

        mock_screen = MagicMock()
        mock_screen.backingScaleFactor.return_value = 1.0
        mock_appkit.NSScreen.mainScreen.return_value = mock_screen

        result = self.capture.capture()

        self.assertIsInstance(result, CaptureResult)
        self.assertEqual(result.image_path, self.capture.output_path)
        self.assertEqual(result.window_rect, Rect(x=100, y=200, width=1200, height=900))
        self.assertEqual(result.scale_factor, 1.0)

        capture_calls = [c for c in self.automation.calls if c[0] == "capture_screen"]
        self.assertEqual(len(capture_calls), 1)
        _, args, kwargs = capture_calls[0]
        self.assertEqual(args[0], Rect(x=100, y=200, width=1200, height=900))
        self.assertEqual(args[1], self.capture.output_path)
        self.assertEqual(kwargs.get("window_id"), 1)

    @patch.object(WindowCapture, '_validate_wechat_screenshot', return_value=True)
    @patch('src.capture.window_capture.Quartz')
    @patch('src.capture.window_capture.AppKit')
    def test_capture_success_wechat_cn(self, mock_appkit, mock_quartz, mock_validate):
        """Successful capture of Chinese-named WeChat window."""
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            self._make_mock_window('微信', 50, 100, 1760, 1280),
        ]
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 2
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.kCGWindowOwnerName = 'kCGWindowOwnerName'
        mock_quartz.kCGWindowBounds = 'kCGWindowBounds'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'

        mock_screen = MagicMock()
        mock_screen.backingScaleFactor.return_value = 2.0
        mock_appkit.NSScreen.mainScreen.return_value = mock_screen

        result = self.capture.capture()

        self.assertIsInstance(result, CaptureResult)
        self.assertEqual(result.window_rect, Rect(x=50, y=100, width=1760, height=1280))
        self.assertEqual(result.scale_factor, 2.0)

    @patch('src.capture.window_capture.Quartz')
    def test_window_not_found_no_wechat(self, mock_quartz):
        """Raise WindowNotFoundError when no WeChat window exists."""
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            self._make_mock_window('Safari', 0, 0, 1200, 800),
            self._make_mock_window('Finder', 0, 0, 800, 600),
        ]
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 2
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.kCGWindowOwnerName = 'kCGWindowOwnerName'
        mock_quartz.kCGWindowBounds = 'kCGWindowBounds'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'

        with self.assertRaises(WindowNotFoundError):
            self.capture.capture()

    @patch('src.capture.window_capture.Quartz')
    def test_window_not_found_too_small(self, mock_quartz):
        """Raise WindowNotFoundError when WeChat window is too small."""
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            self._make_mock_window('WeChat', 0, 0, 199, 199),
        ]
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 2
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.kCGWindowOwnerName = 'kCGWindowOwnerName'
        mock_quartz.kCGWindowBounds = 'kCGWindowBounds'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'

        with self.assertRaises(WindowNotFoundError):
            self.capture.capture()

    @patch.object(WindowCapture, '_validate_wechat_screenshot', return_value=True)
    @patch('src.capture.window_capture.Quartz')
    @patch('src.capture.window_capture.AppKit')
    def test_capture_uses_largest_matching_window(self, mock_appkit, mock_quartz, mock_validate):
        """When multiple WeChat windows exist, the largest one is selected."""
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            self._make_mock_window('WeChat', 10, 20, 1200, 900, window_id=1),
            self._make_mock_window('WeChat', 30, 40, 1400, 1000, window_id=2),
        ]
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 2
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.kCGWindowOwnerName = 'kCGWindowOwnerName'
        mock_quartz.kCGWindowBounds = 'kCGWindowBounds'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'

        mock_screen = MagicMock()
        mock_screen.backingScaleFactor.return_value = 1.0
        mock_appkit.NSScreen.mainScreen.return_value = mock_screen

        result = self.capture.capture()

        # Largest match wins (1400x1000 > 1200x900)
        self.assertEqual(result.window_rect, Rect(x=30, y=40, width=1400, height=1000))

    @patch('src.capture.window_capture.Quartz')
    @patch('src.capture.window_capture.AppKit')
    def test_subprocess_failure_raises(self, mock_appkit, mock_quartz):
        """If capture fails, a RuntimeError is raised."""
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            self._make_mock_window('WeChat', 100, 200, 1200, 900),
        ]
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 2
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.kCGWindowOwnerName = 'kCGWindowOwnerName'
        mock_quartz.kCGWindowBounds = 'kCGWindowBounds'
        mock_quartz.kCGWindowNumber = 'kCGWindowNumber'

        mock_screen = MagicMock()
        mock_screen.backingScaleFactor.return_value = 1.0
        mock_appkit.NSScreen.mainScreen.return_value = mock_screen

        self.automation.capture_success = False
        self.automation.capture_error = "mock capture failure"

        with self.assertRaises(RuntimeError):
            self.capture.capture()


if __name__ == '__main__':
    unittest.main()
