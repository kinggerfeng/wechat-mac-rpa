"""macOS permission detection.

The bot needs three TCC grants. Two of them fail *silently* from Python's point of
view — ``screencapture`` returns a desktop-picture image instead of raising, and a
denied Accessibility prompt just means clicks never land — so an operator sees a
bot that "runs" but does nothing. This module is the check that makes that
diagnosable.

The three checks and what they actually gate:

  ``screen_recording``  ``CGPreflightScreenCaptureAccess`` — gates every
                        ``screencapture`` call, i.e. all perception.
  ``accessibility``    ``AXIsProcessTrusted`` — gates ``cliclick`` synthesis
                        (click_at / send_keys), i.e. all action.
  ``automation``       AppleScript ``tell application "System Events"`` — gates
                        app activation and window queries.

Each returns a status plus the exact System Settings pane to open. A "granted"
that is really "prompted and dismissed" is indistinguishable from denied at the
API level, so the status vocabulary is explicit about that: ``granted`` /
``denied`` / ``not_determined`` / ``unavailable``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

#: URL schemes for the three panes, used to deep-link from the desktop app.
PANE_URLS = {
    "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "automation": "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
}

TITLES = {
    "screen_recording": "屏幕录制",
    "accessibility": "辅助功能",
    "automation": "自动化",
}

WHAT_IT_GATES = {
    "screen_recording": "全部截图与感知（perceive / capture 节点）",
    "accessibility": "全部鼠标键盘操作（send_message / click / type_keys 节点）",
    "automation": "应用激活与窗口定位（activate_app / switch_chat 节点）",
}


@dataclass
class PermissionStatus:
    key: str
    status: str  # granted | denied | not_determined | unavailable
    title: str
    detail: str
    gates: str
    pane_url: str
    fix: str

    @property
    def granted(self) -> bool:
        return self.status == "granted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "granted": self.granted,
            "title": self.title,
            "detail": self.detail,
            "gates": self.gates,
            "pane_url": self.pane_url,
            "fix": self.fix,
        }


def _check_screen_recording() -> tuple[str, str]:
    try:
        import Quartz  # type: ignore
    except ImportError:
        return "unavailable", "未安装 pyobjc-framework-Quartz，无法检测屏幕录制权限"
    try:
        # CGPreflightScreenCaptureAccess reports whether a grant exists without
        # triggering the prompt, which is what a check should do.
        #
        # It also cannot be read as a verdict on "the app is permitted". Two
        # measured facts on this machine: a process started from a shell or an
        # IDE reports the *responsible process* rather than the app the operator
        # is looking at, so the value can describe something else entirely; and
        # the entry existing in System Settings only means TCC has seen it once,
        # while the live call flips to false without the list changing. Both
        # were confirmed by starting the same code two different ways and getting
        # different answers, which is why the denial message below names the
        # bundle to grant rather than describing the symptom.
        granted = bool(Quartz.CGPreflightScreenCaptureAccess())
    except AttributeError:
        # Pre-10.15 fallback: absence of the symbol means the OS always allowed it.
        return "granted", "当前 macOS 版本无需显式授予屏幕录制权限"
    except Exception as exc:  # noqa: BLE001
        return "unavailable", f"检测失败: {exc}"
    if granted:
        return "granted", "已授权，可截图"
    return "denied", (
        "未授权：截图只会拿到桌面壁纸，感知会静默失效。"
        "请在 系统设置 → 隐私与安全性 → 屏幕录制 中给 RPAStudio.app 授权后重启应用；"
        "给终端或 Python 解释器授权无效。"
    )


def _check_accessibility() -> tuple[str, str]:
    try:
        import ApplicationServices  # type: ignore
    except ImportError:
        try:
            import Quartz  # type: ignore

            if hasattr(Quartz, "AXIsProcessTrusted"):
                granted = bool(Quartz.AXIsProcessTrusted())
                if granted:
                    return "granted", "已授权，可发送点击与按键"
                return (
                    "denied",
                    "未授权：点击与按键不会生效。请在 系统设置 → 隐私与安全性 → 辅助功能 "
                    "中给 RPAStudio.app 授权后重启应用；给终端或 Python 解释器授权无效。",
                )
            return "unavailable", "Quartz 未暴露 AXIsProcessTrusted"
        except ImportError:
            return "unavailable", "未安装 pyobjc-framework-ApplicationServices"
    try:
        granted = bool(ApplicationServices.AXIsProcessTrusted())
    except Exception as exc:  # noqa: BLE001
        return "unavailable", f"检测失败: {exc}"
    if granted:
        return "granted", "已授权，可发送点击与按键"
    return (
        "denied",
        "未授权：cliclick 点击与按键不会生效。请在 系统设置 → 隐私与安全性 → "
        "辅助功能 中给 RPAStudio.app 授权后重启应用；给终端或 Python 解释器授权无效。",
    )


def _check_automation(deep: bool = False) -> tuple[str, str]:
    """Ask System Events for its own window list.

    This is the only honest test for Automation permission — unlike Accessibility
    and Screen Recording there is no preflight API. It costs a modal consent
    prompt the first time, so it is **deep-only**: the desktop UI polls
    permissions every few seconds and must never make a poll raise a system
    dialog. A shallow check reports the state as not_determined, which is the
    truthful answer — the OS will not tell us without asking, and asking costs
    the user a modal.
    """
    if not deep:
        return "not_determined", "Automation 权限无预检接口，需在设置页主动检测"
    script = 'tell application "System Events" to return name of first process whose frontmost is true'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unavailable", f"osascript 调用失败: {exc}"
    if proc.returncode == 0:
        return "granted", (proc.stdout.strip() or "已授权，可控制其他应用")
    stderr = (proc.stderr or "").strip()
    if "not allowed" in stderr or "-1743" in stderr:
        return "denied", f"未授权：{stderr.splitlines()[0] if stderr else '拒绝自动化控制'}"
    return "not_determined", stderr or "尚未询问，触发一次自动化操作即可弹出授权"


def _check_wechat_running() -> tuple[str, str]:
    script = 'tell application "System Events" to return (exists process "WeChat") or (exists process "微信")'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unavailable", f"osascript 调用失败: {exc}"
    if proc.returncode != 0:
        return "unknown", (proc.stderr or "").strip() or "无法查询"
    running = proc.stdout.strip().lower() == "true"
    return ("granted", "微信正在运行") if running else ("denied", "微信未运行")


def _check_by_key(key: str, deep: bool) -> tuple[str, str]:
    if key == "screen_recording":
        return _check_screen_recording()
    if key == "accessibility":
        return _check_accessibility()
    if key == "automation":
        return _check_automation(deep=deep)
    return "unavailable", f"未知权限 {key!r}"


CHECK_KEYS = ("screen_recording", "accessibility", "automation")


def check_all(include_wechat: bool = True, deep: bool = False) -> dict[str, Any]:
    """Check every permission. ``wechat_running`` is a fact, not a permission.

    ``deep=True`` additionally runs the Automation probe, which may raise a
    system consent dialog. Only the desktop permission page should pass it.
    """
    permissions = []
    for key in CHECK_KEYS:
        try:
            status, detail = _check_by_key(key, deep)
        except Exception as exc:  # noqa: BLE001 - a check must never raise into the UI
            status, detail = "unavailable", f"检测异常: {exc}"
        permissions.append(
            PermissionStatus(
                key=key,
                status=status,
                title=TITLES[key],
                detail=detail,
                gates=WHAT_IT_GATES[key],
                pane_url=PANE_URLS[key],
                fix=_fix_text(key, status),
            ).to_dict()
        )

    wechat = None
    if include_wechat:
        try:
            _, detail = _check_wechat_running()
            wechat = detail
        except Exception:  # noqa: BLE001
            wechat = "unknown"

    # not_determined is not a blocker for a shallow check: the user simply has
    # not been asked yet, and refusing to start on that would be a lie.
    missing = [p["key"] for p in permissions if not p["granted"] and (deep or p["status"] != "not_determined")]
    return {
        "ok": not missing,
        "deep": deep,
        "missing": missing,
        "wechat_running": wechat,
        "permissions": permissions,
        "summary": _summary(permissions),
    }


def _fix_text(key: str, status: str) -> str:
    if status == "granted":
        return "无需处理"
    if status == "unavailable":
        return f"安装依赖后重试：pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices"
    return f"系统设置 → 隐私与安全性 → {TITLES[key]}，勾选本应用后重启本应用"


def _summary(permissions: list[dict[str, Any]]) -> str:
    if not any(not p["granted"] for p in permissions):
        return "全部权限已授予，可以运行自动化流程"
    missing = "、".join(TITLES[p["key"]] for p in permissions if not p["granted"])
    return f"缺少权限：{missing}"


def request_prompt(key: str) -> dict[str, Any]:
    """Trigger the system consent prompt for one permission.

    ``AXIsProcessTrustedWithOptions`` shows the Accessibility dialog; Screen
    Recording has no programmatic prompt, so that one opens the Settings pane.
    """
    if key == "accessibility":
        try:
            import ApplicationServices  # type: ignore

            options = {"AXTrustedCheckOptionPrompt": True}
            granted = bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
            return {"key": key, "prompted": True, "granted": granted}
        except Exception as exc:  # noqa: BLE001
            return {"key": key, "prompted": False, "error": str(exc)}
    if key == "screen_recording":
        try:
            import Quartz  # type: ignore

            Quartz.CGRequestScreenCaptureAccess()
            return {"key": key, "prompted": True, "granted": bool(Quartz.CGPreflightScreenCaptureAccess())}
        except Exception as exc:  # noqa: BLE001
            return {"key": key, "prompted": False, "error": str(exc)}
    return {"key": key, "prompted": False, "error": f"{key} 无编程式提权接口，请手动打开设置面板"}


def open_pane(key: str) -> dict[str, Any]:
    """Open the relevant System Settings pane."""
    url = PANE_URLS.get(key)
    if not url:
        return {"key": key, "opened": False, "error": f"未知权限 {key!r}"}
    try:
        subprocess.run(["open", url], capture_output=True, timeout=8, check=False)
        return {"key": key, "opened": True, "pane_url": url}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"key": key, "opened": False, "error": str(exc)}


def preflight() -> dict[str, Any]:
    """Cheap check for the paths that actually need the screen, no prompts.

    The desktop API calls this before ``/api/bot/start`` so the UI can refuse to
    start rather than starting a bot that will silently do nothing.
    """
    screen, screen_detail = _check_screen_recording()
    access, access_detail = _check_accessibility()
    return {
        "ok": screen == "granted" and access == "granted",
        "screen_recording": {"status": screen, "detail": screen_detail},
        "accessibility": {"status": access, "detail": access_detail},
    }
