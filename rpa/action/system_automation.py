#!/usr/bin/env python3
"""L4 Action - 系统级 UI 自动化抽象

将 cliclick、osascript、AppleScript、screencapture、pbcopy/pbpaste 等 macOS 特定调用
封装为统一接口，使 Bot 层和 Action 层可以面向接口编程，便于 mock、测试和跨平台扩展。
"""

import logging
import subprocess  # nosec B404
import time
from abc import ABC, abstractmethod
from typing import Any

from rpa.models.base import Rect

_logger = logging.getLogger("rpa.system_automation")


class SystemAutomation(ABC):
    """系统级 UI 自动化抽象接口。"""

    @abstractmethod
    def activate_app(self, app_name: str) -> bool:
        """激活指定应用并等待其获得焦点。"""
        pass

    @abstractmethod
    def get_frontmost_app(self, app_name: str) -> tuple[bool, str]:
        """检查指定应用是否为当前 frontmost 应用。

        Returns:
            (is_frontmost, info_or_error)
        """
        pass

    @abstractmethod
    def get_window_rect(self, app_name: str) -> tuple[bool, Rect | None, str]:
        """获取指定应用主窗口的位置和大小。

        Returns:
            (success, rect_or_none, error_message)
        """
        pass

    @abstractmethod
    def click_at(self, x: int, y: int, button: str = "left", count: int = 1) -> bool:
        """在屏幕逻辑坐标 (x, y) 处点击。

        Args:
            x, y: 屏幕逻辑坐标（非物理像素）
            button: ``left`` / ``right`` / ``middle``。三键语义在 macOS 与
                Windows 上完全一致，可以直接贯穿到节点层。
            count: 点击次数。``2`` 表示双击。平台差异在于「双击」的实现方式
                （重复左键 vs 专门的 double-click 事件），不改变这里的语义。

        为什么不加 ``hover`` / ``drag`` 参数：拖拽是一串有序事件（移动 →
        按下 → 移动 → 抬起），中途失败需要判断停在哪一步，塞进 click 会让
        签名既长又难跨平台实现。它们是独立方法。
        """
        pass

    @abstractmethod
    def move_to(self, x: int, y: int) -> bool:
        """把指针移动到 (x, y)，不按下任何键。悬停菜单的基础动作。"""
        pass

    @abstractmethod
    def drag_to(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> bool:
        """从 (x1, y1) 按下并拖到 (x2, y2) 后抬起。

        Args:
            duration_ms: 按下到抬起的时长。拖拽被目标应用识别需要时间，
                0 会被当成瞬移，多数控件直接忽略。
        """
        pass

    @abstractmethod
    def scroll_at(self, x: int, y: int, clicks: int) -> bool:
        """在 (x, y) 处滚动滚轮。

        Args:
            clicks: 正数向上滚，负数向下滚。统一成「格数」而不是像素或角度：
            两平台的原生单位不同（Windows 是 WHEEL_DELTA 的 120 倍），
            但「一格」这个语义是一致的，节点层不需要知道平台。
        """
        pass

    @abstractmethod
    def set_window_rect(self, app_name: str, rect: Rect) -> bool:
        """移动并调整窗口到 ``rect``。"""
        pass

    @abstractmethod
    def minimize_window(self, app_name: str) -> bool:
        """最小化主窗口。"""
        pass

    @abstractmethod
    def maximize_window(self, app_name: str) -> bool:
        """最大化主窗口（填充可用工作区）。"""
        pass

    @abstractmethod
    def close_window(self, app_name: str) -> bool:
        """关闭主窗口。**不可撤销**，节点层需显式声明。"""
        pass

    @abstractmethod
    def send_keys(self, key_spec: str) -> bool:
        """发送键盘事件。

        Args:
            key_spec: 平台中立的按键描述，如 ``"cmd+v"``、``"ctrl+shift+t"``、
                      ``"enter"``、``"f5"``、``"escape"``、``"tab"``。

        刻意**不是** AppleScript 片段。旧实现把调用方传的 ``"cmd+v"`` 原样
        嵌进 ``tell process "WeChat" ...``，而 ``cmd`` 在 AppleScript 里不是
        合法标识符——实测返回 ``-2753 变量"cmd"没有定义``。也就是说粘贴功能
        一直是坏的，只是失败被调用方当成了成功。改成中立语法后，macOS 由
        pyautogui 转成真实修饰键，Windows 映射到 Win 键，无需再碰节点层。
        """
        pass

    @abstractmethod
    def run_applescript(self, script: str, timeout: int = 5) -> tuple[int, str, str]:
        """执行 AppleScript。

        Returns:
            (returncode, stdout, stderr)
        """
        pass

    @abstractmethod
    def set_clipboard_text(self, text: str) -> bool:
        """将文本写入系统剪贴板。"""
        pass

    @abstractmethod
    def get_clipboard_text(self) -> tuple[bool, str]:
        """读取系统剪贴板文本。"""
        pass

    @abstractmethod
    def capture_screen(
        self,
        rect: Rect,
        output_path: str,
        window_id: int | None = None,
    ) -> tuple[bool, str]:
        """截取指定屏幕区域或窗口并保存到文件。

        Args:
            rect: 截图区域（window_id 为空时使用）
            output_path: 输出文件路径
            window_id: 可选的窗口 ID，优先使用 screencapture -l

        Returns:
            (success, error_message)
        """
        pass


class PointerMixin:
    """Pointer primitives backed by pyautogui — shared by every platform.

    pyautogui is already a dependency and is itself cross-platform, so the
    click / move / drag / scroll primitives do not need a per-OS
    implementation at all. What *is* per-OS stays in the concrete classes:
    window enumeration, activation and screen capture.

    It also removes a hard dependency that was silently broken: the previous
    macOS path shelled out to ``/opt/homebrew/bin/cliclick`` and returned
    ``False`` on every click when that binary was absent, so the ``click``
    node had never actually clicked anything on a machine without it.
    """

    #: Set by :meth:`_gui`; pyautogui is imported lazily because importing it
    #: probes the display, which is slow and fails in headless test runs.
    _gui_module: Any = None

    def _gui(self) -> Any:
        if self._gui_module is None:
            import pyautogui

            # The corner failsafe aborts a run by raising, which surfaces as an
            # opaque node error with no explanation. Off by default; a flow
            # that wedges the pointer can still be killed from the canvas.
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0.0  # this layer owns its own pacing
            self._gui_module = pyautogui
        return self._gui_module

    def click_at(self, x: int, y: int, button: str = "left", count: int = 1) -> bool:
        if button not in ("left", "right", "middle"):
            _logger.warning("click_at: 未知按键 %r", button)
            return False
        try:
            gui = self._gui()
            gui.moveTo(x, y, duration=0)
            if count == 1:
                gui.click(button=button)
            else:
                gui.doubleClick(button=button) if count == 2 else gui.tripleClick(button=button)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("click_at(%d,%d,%s,%d) 失败: %s", x, y, button, count, e)
            return False

    def move_to(self, x: int, y: int) -> bool:
        try:
            self._gui().moveTo(x, y, duration=0)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("move_to(%d,%d) 失败: %s", x, y, e)
            return False

    def drag_to(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> bool:
        gui = self._gui()
        seconds = max(0.0, int(duration_ms) / 1000.0)
        try:
            # A linear glide over the full duration: instant jumps are treated
            # as teleports and dropped by most list and canvas widgets.
            gui.moveTo(x1, y1, duration=0)
            gui.dragTo(x2, y2, duration=seconds, button="left")
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("drag_to(%d,%d -> %d,%d) 失败: %s", x1, y1, x2, y2, e)
            # Leaving the button down wedges the whole desktop for the user.
            try:
                gui.mouseUp()
            except Exception:  # noqa: BLE001
                pass
            return False

    def scroll_at(self, x: int, y: int, clicks: int) -> bool:
        try:
            gui = self._gui()
            gui.moveTo(x, y, duration=0)
            gui.scroll(clicks)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("scroll_at(%d,%d,%d) 失败: %s", x, y, clicks, e)
            return False

    #: Platform-neutral modifier names → pyautogui's names. pyautogui already
    #: uses the right ones per platform (``command`` on macOS, ``ctrl`` on
    #: Windows), so only the vocabulary needs translating, not the behaviour.
    _MODIFIER_ALIASES = {
        "cmd": "command", "command": "command", "meta": "command", "win": "command",
        "ctrl": "ctrl", "control": "ctrl",
        "alt": "alt", "option": "alt", "opt": "alt",
        "shift": "shift",
    }

    #: Bare key names → pyautogui key names. Covers the keys an RPA flow
    #: actually needs; an unknown name is refused rather than passed through,
    #: because pyautogui's fallback types the literal characters instead.
    _KEY_ALIASES = {
        "enter": "enter", "return": "enter",
        "esc": "esc", "escape": "esc",
        "tab": "tab", "space": "space", "backspace": "backspace", "delete": "delete",
        "up": "up", "down": "down", "left": "left", "right": "right",
        "home": "home", "end": "end", "pageup": "pgup", "pagedown": "pgdn",
        "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5", "f6": "f6",
        "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
    }

    @classmethod
    def parse_key_spec(cls, key_spec: str) -> tuple[list[str], str]:
        """Split ``"cmd+shift+v"`` into ``(["command","shift"], "v")``.

        Raises:
            ValueError: on an empty spec or an unrecognised name. Failing here
            is the point — a mistyped key must not turn into literal text
            being typed into whatever field had focus.
        """
        raw = (key_spec or "").strip()
        if not raw:
            raise ValueError("key_spec 为空")
        parts = [p.strip() for p in raw.split("+") if p.strip()]
        if not parts:
            raise ValueError(f"无法解析按键描述: {key_spec!r}")

        modifiers: list[str] = []
        key = ""
        for part in parts:
            lowered = part.lower()
            if lowered in cls._MODIFIER_ALIASES:
                modifier = cls._MODIFIER_ALIASES[lowered]
                if modifier not in modifiers:
                    modifiers.append(modifier)
                continue
            if lowered in cls._KEY_ALIASES:
                key = cls._KEY_ALIASES[lowered]
                continue
            # A single character is a literal key; anything else is a typo.
            if len(part) == 1:
                key = part
                continue
            raise ValueError(f"无法识别的按键名 {part!r}（来自 {key_spec!r}）")
        if not key:
            raise ValueError(f"按键描述缺少主键: {key_spec!r}")
        return modifiers, key

    def send_keys(self, key_spec: str) -> bool:
        try:
            modifiers, key = self.parse_key_spec(key_spec)
        except ValueError as e:
            _logger.warning("send_keys(%r) 解析失败: %s", key_spec, e)
            return False
        try:
            gui = self._gui()
            if modifiers:
                gui.hotkey(*modifiers, key)
            else:
                gui.press(key)
            return True
        except Exception as e:  # noqa: BLE001
            _logger.warning("send_keys(%r) 失败: %s", key_spec, e)
            return False


class MacOSSystemAutomation(PointerMixin, SystemAutomation):
    """macOS 实现：基于 AppleScript + cliclick + screencapture。"""

    def __init__(self, cliclick_path: str = "/opt/homebrew/bin/cliclick"):
        # Kept for compatibility with callers that pass it, but pointer input
        # no longer goes through it — see PointerMixin. Nothing in this class
        # reads it any more.
        self.cliclick_path = cliclick_path

    def activate_app(self, app_name: str) -> bool:
        script = f'tell application "{app_name}" to activate'
        try:
            rc, _, stderr = self.run_applescript(script, timeout=3)
            if rc != 0:
                _logger.warning("activate_app(%s) 失败: %s", app_name, stderr)
            return rc == 0
        except (subprocess.SubprocessError, OSError) as e:
            _logger.warning("activate_app(%s) 异常: %s", app_name, e)
            return False

    def get_frontmost_app(self, app_name: str) -> tuple[bool, str]:
        script = f'''
            tell application "System Events"
                tell process "{app_name}"
                    set frontmost to true
                    delay 0.3
                end tell
                set frontApp to name of first application process whose frontmost is true
                return frontApp
            end tell
        '''
        try:
            rc, stdout, stderr = self.run_applescript(script, timeout=5)
            if rc != 0:
                return False, stderr
            front_app = stdout.strip()
            return front_app == app_name, front_app
        except Exception as e:
            return False, str(e)

    def get_window_rect(self, app_name: str) -> tuple[bool, Rect | None, str]:
        """通过 AppleScript 获取应用主窗口的位置和大小。"""
        script = f'''
            tell application "System Events"
                tell process "{app_name}"
                    tell window 1
                        set winPos to position
                        set winSize to size
                        return ((item 1 of winPos) as text) & "," & ((item 2 of winPos) as text) & "," & ((item 1 of winSize) as text) & "," & ((item 2 of winSize) as text)
                    end tell
                end tell
            end tell
        '''
        try:
            rc, stdout, stderr = self.run_applescript(script, timeout=5)
            if rc != 0:
                return False, None, stderr
            parts = stdout.strip().split(",")
            if len(parts) != 4:
                return False, None, f"无法解析窗口坐标: {stdout!r}"
            x, y, w, h = map(int, map(float, parts))
            return True, Rect(x=x, y=y, width=w, height=h), ""
        except Exception as e:
            return False, None, str(e)

    def _window_script(self, app_name: str, body: str) -> tuple[bool, str]:
        """Run ``body`` against ``app_name``'s first window.

        Every window operation goes through this so the ``tell`` preamble is
        written once — a typo in the boilerplate would otherwise produce four
        different failures, one per operation.
        """
        if not app_name or '"' in app_name:
            _logger.warning("窗口操作拒绝非法应用名: %r", app_name)
            return False, f"非法应用名: {app_name!r}"
        script = f'''
            tell application "System Events"
                tell process "{app_name}"
                    tell window 1
                        {body}
                    end tell
                end tell
            end tell
        '''
        rc, _, stderr = self.run_applescript(script, timeout=6)
        if rc != 0:
            _logger.warning("窗口操作失败(%s): %s", app_name, stderr.strip()[:200])
            return False, stderr.strip()[:200]
        return True, ""

    def set_window_rect(self, app_name: str, rect: Rect) -> bool:
        ok, _ = self._window_script(
            app_name,
            f"set position to {{{int(rect.x)}, {int(rect.y)}}}\n"
            f"set size to {{{int(rect.width)}, {int(rect.height)}}}",
        )
        return ok

    def minimize_window(self, app_name: str) -> bool:
        # AXMinimized rather than the yellow traffic-light button: it is
        # addressable by name, so it does not depend on the window's chrome.
        return self._window_script(app_name, 'set value of attribute "AXMinimized" to true')[0]

    def maximize_window(self, app_name: str) -> bool:
        """Fill the screen the window is on, minus the menu bar and Dock.

        Reads the screen size through System Events rather than hard-coding a
        display size, so a second monitor does not produce a window larger
        than the screen it lands on.
        """
        return self._window_script(
            app_name,
            "set size to {screen width - 4, screen height - 80}",
        )[0]

    def close_window(self, app_name: str) -> bool:
        return self._window_script(app_name, 'perform action "AXPress" of button 1')[0]

    def run_applescript(self, script: str, timeout: int = 5) -> tuple[int, str, str]:
        try:
            r = subprocess.run(  # nosec
                ["osascript", "-e", script],
                timeout=timeout,
                capture_output=True,
            )
            return (
                r.returncode,
                r.stdout.decode("utf-8", errors="replace"),
                r.stderr.decode("utf-8", errors="replace"),
            )
        except Exception as e:
            return -1, "", str(e)

    def set_clipboard_text(self, text: str) -> bool:
        try:
            subprocess.run(  # nosec
                ["pbcopy"],
                input=text.encode("utf-8"),
                timeout=2,
                capture_output=True,
            )
            return True
        except (subprocess.SubprocessError, OSError) as e:
            _logger.warning("set_clipboard_text 失败: %s", e)
            return False

    def get_clipboard_text(self) -> tuple[bool, str]:
        try:
            r = subprocess.run(  # nosec
                ["pbpaste"],
                timeout=2,
                capture_output=True,
            )
            if r.returncode == 0:
                return True, r.stdout.decode("utf-8", errors="replace")
            return False, r.stderr.decode("utf-8", errors="replace")
        except Exception as e:
            return False, str(e)

    def capture_screen(
        self,
        rect: Rect,
        output_path: str,
        window_id: int | None = None,
    ) -> tuple[bool, str]:
        """使用 screencapture 截取指定区域或窗口。

        对输出路径做基本校验，防止路径注入。
        """
        _SHELL_META = "&;|`$()"
        if not output_path or "/" not in output_path or any(c in output_path for c in _SHELL_META):
            return False, f"非法截图输出路径: {output_path}"
        if window_id:
            cmd = [
                "screencapture",
                "-l", str(window_id),
                "-o",  # 排除窗口阴影
                "-x", output_path,
            ]
        else:
            cmd = [
                "screencapture",
                "-R", f"{rect.x},{rect.y},{rect.width},{rect.height}",
                "-x", output_path,
            ]
        try:
            subprocess.run(cmd, check=True, timeout=5)  # nosec
            return True, ""
        except Exception as e:
            return False, str(e)


class NoOpSystemAutomation(SystemAutomation):
    """空实现，用于测试或禁用 UI 交互的场景。"""

    def __init__(self, window_rect: Rect | None = None):
        self.window_rect = window_rect or Rect(x=0, y=0, width=1200, height=800)

    def activate_app(self, app_name: str) -> bool:
        return True

    def get_frontmost_app(self, app_name: str) -> tuple[bool, str]:
        return True, app_name

    def get_window_rect(self, app_name: str) -> tuple[bool, Rect | None, str]:
        return True, self.window_rect, ""

    def click_at(self, x: int, y: int, button: str = "left", count: int = 1) -> bool:
        return True

    def move_to(self, x: int, y: int) -> bool:
        return True

    def drag_to(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> bool:
        return True

    def send_keys(self, key_spec: str) -> bool:
        return True

    def scroll_at(self, x: int, y: int, clicks: int) -> bool:
        return True

    def set_window_rect(self, app_name: str, rect: Rect) -> bool:
        return True

    def minimize_window(self, app_name: str) -> bool:
        return True

    def maximize_window(self, app_name: str) -> bool:
        return True

    def close_window(self, app_name: str) -> bool:
        return True

    def send_keys(self, key_spec: str) -> bool:
        return True

    def run_applescript(self, script: str, timeout: int = 5) -> tuple[int, str, str]:
        return 0, "", ""

    def set_clipboard_text(self, text: str) -> bool:
        return True

    def get_clipboard_text(self) -> tuple[bool, str]:
        return True, ""

    def capture_screen(
        self,
        rect: Rect,
        output_path: str,
        window_id: int | None = None,
    ) -> tuple[bool, str]:
        return True, ""
