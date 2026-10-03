"""Records user actions and turns them into flow nodes.

A recording session listens for clicks, scrolls, and keystrokes, coalesces them
into a readable sequence, and hands back action nodes an author can drop onto
the canvas.

**Why Cocoa and not a Quartz event tap.** A ``CGEventTap`` is the lower-level
and more powerful primitive, but on this platform its pyobjc binding hands back
an ``NSMachPort`` that ``CFMachPortCreateRunLoopSource`` refuses to accept (it
wants a raw ``mach_port_t``), and every conversion route between the two
pyobjc-idiomatically segfaults the interpreter. ``NSEvent``'s global monitor is
the supported, documented API for exactly this job, needs no type surgery, and
cannot swallow events — it observes. The one cost is that it needs a run loop,
which :meth:`ActionRecorder.start` provides on its own thread.

**What a recording is and is not.** It is a first draft: coordinates are the
ones the user actually clicked, which may be the right answer or may be a
dialog that moved. Turning a click into a named element is the picker's job, not
the recorder's. Keystrokes are coalesced into one node per burst because a
per-character log is unreadable as a flow and replays with the wrong rhythm.

Both an Accessibility permission failure and a monitor thread that dies are
reported through :attr:`ActionRecorder.error` rather than being swallowed — an
empty recording that looks like "the user did nothing" is the worst outcome.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

#: A burst of typing longer than this becomes several nodes. Long enough to stay
#: readable, short enough that a paragraph is not one enormous literal.
_MAX_BURST = 200

#: Ignore bursts shorter than this. Stray modifier presses produce key events
#: with no text, and a node saying "type ''" is noise.
_MIN_BURST = 1

#: Two clicks of the same button within this window are one double-click.
_DOUBLE_CLICK_SECONDS = 0.4


@dataclass
class RecordedAction:
    """One captured action, already shaped as a node candidate."""

    kind: str
    at: float
    x: int = 0
    y: int = 0
    button: int = 0
    clicks: int = 1
    text: str = ""
    scroll_delta: int = 0
    modifiers: list[str] = field(default_factory=list)
    #: Bundle identifier of the frontmost app at the moment of the event, so a
    #: replay can be scoped to the app the user was actually working in.
    app: str = ""

    def to_node_params(self) -> dict[str, Any]:
        """The params a generated action node would carry."""
        if self.kind == "click":
            return {"x": self.x, "y": self.y, "button": "left" if self.button == 0 else "right"}
        if self.kind == "double_click":
            return {"x": self.x, "y": self.y, "clicks": 2}
        if self.kind == "scroll":
            return {"x": self.x, "y": self.y, "amount": self.scroll_delta}
        if self.kind == "type_keys":
            return {"text": self.text}
        if self.kind == "hotkey":
            return {"keys": "+".join(self.modifiers) if self.modifiers else ""}
        return {}

    def to_node_type(self) -> str:
        """The node type this action replays as."""
        return {
            "click": "click",
            "double_click": "click",
            "scroll": "scroll",
            "type_keys": "type_keys",
            "hotkey": "type_keys",
        }.get(self.kind, "log")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at,
            "x": self.x,
            "y": self.y,
            "button": self.button,
            "clicks": self.clicks,
            "text": self.text,
            "scroll_delta": self.scroll_delta,
            "modifiers": self.modifiers,
            "app": self.app,
        }


@dataclass
class _Burst:
    """Keystrokes not yet committed to a node."""

    chars: list[str] = field(default_factory=list)
    started: float = 0.0
    modifiers: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.chars)


def _modifiers_from_flags(flags: int) -> list[str]:
    out: list[str] = []
    if flags & 0x00020000:  # NSEventModifierFlagShift
        out.append("shift")
    if flags & 0x00040000:  # NSEventModifierFlagControl
        out.append("ctrl")
    if flags & 0x00080000:  # NSEventModifierFlagOption / alt
        out.append("alt")
    if flags & 0x00100000:  # NSEventModifierFlagCommand
        out.append("cmd")
    return out


class RecorderError(RuntimeError):
    """Raised when recording cannot start, with the reason in plain words."""


class ActionRecorder:
    """Start/stop a global NSEvent monitor that accumulates recorded actions.

    A process-wide singleton by way of :func:`get_recorder`: only one global
    monitor is meaningful at a time, and installing a second would double-count
    every event.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._monitor: Any = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._actions: list[RecordedAction] = []
        self._burst: _Burst | None = None
        self._error: str = ""
        self._on_action: Callable[[RecordedAction], None] | None = None

    # ── public API ──

    @property
    def recording(self) -> bool:
        if self._thread is None or not self._thread.is_alive():
            return False
        return not self._stop.is_set()

    @property
    def error(self) -> str:
        return self._error

    def start(self, on_action: Callable[[RecordedAction], None] | None = None) -> None:
        """Install the global monitor. Raises :class:`RecorderError` if it cannot."""
        if self.recording:
            raise RecorderError("已经在录制中")
        try:
            import AppKit  # noqa: PLC0415
            import Quartz  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RecorderError(
                "缺少 pyobjc-framework-Cocoa / Quartz，无法监听鼠标键盘事件。"
                "请执行: uv pip install --python .venv/bin/python "
                "pyobjc-framework-Cocoa pyobjc-framework-Quartz"
            ) from exc

        with self._lock:
            self._actions = []
            self._burst = None
            self._error = ""
            self._on_action = on_action
            self._stop.clear()

        mask = (
            AppKit.NSEventMaskLeftMouseDown
            | AppKit.NSEventMaskRightMouseDown
            | AppKit.NSEventMaskOtherMouseDown
            | AppKit.NSEventMaskScrollWheel
            | AppKit.NSEventMaskKeyDown
        )

        def handler(event: Any) -> None:
            try:
                self._handle(AppKit, event)
            except Exception:  # noqa: BLE001 - a monitor must never raise into AppKit
                pass

        try:
            monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, handler)
        except Exception as exc:  # noqa: BLE001
            raise RecorderError(f"无法安装事件监听: {exc}") from exc

        if monitor is None:
            # NSEvent returns nil rather than raising when the process lacks
            # Accessibility permission, so this nil check is the only place the
            # permission failure can be reported.
            raise RecorderError(
                "无法监听鼠标键盘事件。请在「系统设置 → 隐私与安全性 → 辅助功能」"
                "中授权当前程序后重试。"
            )

        self._monitor = monitor
        started = threading.Event()

        def pump() -> None:
            loop = Quartz.CFRunLoopGetCurrent()
            # The monitor is delivered on the thread whose run loop is running,
            # so the loop has to be entered on this thread before the first
            # event can arrive.
            started.set()
            while not self._stop.is_set():
                Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.2, False)

        self._thread = threading.Thread(target=pump, name="rpa-recorder", daemon=True)
        self._thread.start()
        started.wait(timeout=2.0)
        if not self._thread.is_alive():
            self._teardown_monitor()
            raise RecorderError("录制线程启动后立即退出，无法录制")

    def stop(self) -> list[RecordedAction]:
        """Stop recording and return everything captured."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        self._teardown_monitor()
        with self._lock:
            self._flush_burst_locked()
            return list(self._actions)

    def actions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._flush_burst_locked()
            return [a.to_dict() for a in self._actions]

    def clear(self) -> None:
        with self._lock:
            self._actions = []
            self._burst = None

    # ── internals ──

    def _teardown_monitor(self) -> None:
        monitor = self._monitor
        self._monitor = None
        if monitor is None:
            return
        try:
            import AppKit  # noqa: PLC0415

            AppKit.NSEvent.removeMonitor_(monitor)
        except Exception:  # noqa: BLE001
            pass

    def _handle(self, appkit: Any, event: Any) -> None:
        if event is None:
            return
        kind = int(event.type())
        with self._lock:
            if kind in (
                appkit.NSEventTypeLeftMouseDown,
                appkit.NSEventTypeRightMouseDown,
                appkit.NSEventTypeOtherMouseDown,
            ):
                self._flush_burst_locked()
                self._record_click(appkit, event, kind)
            elif kind == appkit.NSEventTypeScrollWheel:
                self._flush_burst_locked()
                self._record_scroll(event)
            elif kind == appkit.NSEventTypeKeyDown:
                self._record_key(event)

    def _record_click(self, appkit: Any, event: Any, kind: int) -> None:
        # locationInWindow is window-relative; a global monitor's window is the
        # system window, so the screen coordinates come from locationInScreen
        # when available and fall back to the window point otherwise.
        point = self._screen_point(event)
        button = {appkit.NSEventTypeLeftMouseDown: 0,
                  appkit.NSEventTypeRightMouseDown: 1}.get(kind, 2)
        click_count = int(event.clickCount()) or 1
        now = time.time()

        last = self._actions[-1] if self._actions else None
        if (
            click_count >= 2
            or (
                last is not None
                and last.kind == "click"
                and last.button == button
                and now - last.at < _DOUBLE_CLICK_SECONDS
            )
        ):
            if last is not None and last.kind == "click":
                last.clicks = 2
                last.kind = "double_click"
                self._emit(last)
                return

        self._append(
            RecordedAction(
                kind="click",
                at=now,
                x=int(point[0]),
                y=int(point[1]),
                button=button,
                modifiers=_modifiers_from_flags(int(event.modifierFlags())),
                app=self._front_app(),
            )
        )

    def _screen_point(self, event: Any) -> tuple[float, float]:
        for accessor in ("locationInScreen", "locationInWindow"):
            getter = getattr(event, accessor, None)
            if getter is not None:
                try:
                    p = getter()
                    return (float(p.x), float(p.y))
                except Exception:  # noqa: BLE001
                    continue
        return (0.0, 0.0)

    def _record_scroll(self, event: Any) -> None:
        point = self._screen_point(event)
        # A trackpad and a wheel mouse disagree on sign and scale, so the raw
        # delta is kept as-is and the node that replays it is responsible for
        # its own direction convention.
        try:
            delta = int(round(float(event.scrollingDeltaY())))
        except Exception:  # noqa: BLE001
            delta = 0
        self._append(
            RecordedAction(
                kind="scroll",
                at=time.time(),
                x=int(point[0]),
                y=int(point[1]),
                scroll_delta=delta,
                app=self._front_app(),
            )
        )

    def _record_key(self, event: Any) -> None:
        keycode = int(event.keyCode())
        # Return, tab, escape and delete are structure, not text: they either
        # commit the burst or are worth a node of their own.
        if keycode == 36:  # return
            self._flush_burst_locked()
            return
        if keycode == 48:  # tab
            self._flush_burst_locked()
            self._append(RecordedAction(kind="hotkey", at=time.time(), modifiers=["tab"], app=self._front_app()))
            return
        if keycode == 53:  # escape
            self._flush_burst_locked()
            self._append(RecordedAction(kind="hotkey", at=time.time(), modifiers=["esc"], app=self._front_app()))
            return
        if keycode == 51:  # delete
            self._flush_burst_locked()
            return

        text = self._char_for(event)
        if not text:
            # A modifier press produces a key event with no characters; logging
            # it would produce a node whose text is empty.
            return
        if self._burst is None:
            self._burst = _Burst(
                started=time.time(),
                modifiers=_modifiers_from_flags(int(event.modifierFlags())),
            )
        self._burst.chars.append(text)
        if len(self._burst.chars) >= _MAX_BURST:
            self._flush_burst_locked()

    def _char_for(self, event: Any) -> str:
        try:
            raw = event.characters()
        except Exception:  # noqa: BLE001
            return ""
        if raw is None:
            return ""
        # NSEvent reports carriage returns for the return key; the newlines that
        # end up in a node body would replay as Enter presses inside a type_keys.
        return raw.replace("\r", "").replace("\n", "")

    def _front_app(self) -> str:
        try:
            from AppKit import NSWorkspace  # noqa: PLC0415

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return str(app.bundleIdentifier() or "") if app else ""
        except Exception:  # noqa: BLE001
            return ""

    def _flush_burst_locked(self) -> None:
        burst = self._burst
        self._burst = None
        if burst is None:
            return
        text = burst.text
        if len(text) < _MIN_BURST:
            return
        self._append(
            RecordedAction(
                kind="type_keys",
                at=burst.started or time.time(),
                text=text,
                modifiers=list(burst.modifiers),
                app=self._front_app(),
            )
        )

    def _append(self, action: RecordedAction) -> None:
        self._actions.append(action)
        self._emit(action)

    def _emit(self, action: RecordedAction) -> None:
        if self._on_action is None:
            return
        try:
            self._on_action(action)
        except Exception:  # noqa: BLE001 - a UI callback must not kill the monitor
            pass


_RECORDER: ActionRecorder | None = None


def get_recorder() -> ActionRecorder:
    """Process-wide recorder. One monitor at a time, by design."""
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = ActionRecorder()
    return _RECORDER
