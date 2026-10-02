#!/usr/bin/env python3
"""L2 Capture - 窗口截图模块"""

import glob
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import AppKit
import Quartz

from src.action.system_automation import MacOSSystemAutomation, SystemAutomation
from src.models.base import Rect

_logger = logging.getLogger("src.window_capture")


class WindowNotFoundError(Exception):
    """未找到目标窗口时抛出"""
    pass


class WeChatNotReadyError(Exception):
    """微信窗口尺寸异常（未登录/需扫码）时抛出"""
    pass


@dataclass
class CaptureResult:
    """窗口截图结果"""
    image_path: str
    window_rect: Rect
    scale_factor: float  # Retina 屏幕为 2.0，普通屏幕为 1.0


class CaptureValidationError(Exception):
    """截图内容验证失败（截到的不是微信窗口）时抛出"""
    pass


class WindowCapture:
    """查找并截图微信主窗口"""

    def __init__(
        self,
        output_path: Optional[str] = None,
        min_effective_width: int = 800,
        min_effective_height: int = 600,
        automation: SystemAutomation | None = None,
    ):
        if output_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            pid = os.getpid()
            output_path = os.path.join(
                tempfile.gettempdir(), f"wechat_capture_{ts}_{pid}.png"
            )
        self.output_path = output_path
        self.app_names = ['WeChat', '微信']
        self.min_width = 200
        self.min_height = 200
        self.min_effective_width = min_effective_width
        self.min_effective_height = min_effective_height
        self.automation = automation or MacOSSystemAutomation()

    def _find_window(self) -> Optional[tuple]:
        """使用 Quartz 查找微信窗口，返回面积最大的有效窗口 (Rect, window_id) 或 None"""
        # 先尝试 OnScreenOnly（正常情况）
        result = self._find_window_with_options(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        )
        # fallback：如果 OnScreenOnly 找不到（如窗口在另一个 Space / 外接显示器），
        # 则不加 OnScreenOnly 限制，只排除桌面元素
        if result is None:
            result = self._find_window_with_options(
                Quartz.kCGWindowListExcludeDesktopElements
            )
        return result

    def _find_window_with_options(self, options: int) -> Optional[tuple]:
        """使用指定 options 查找微信窗口，返回 (Rect, window_id) 或 None"""
        window_list = Quartz.CGWindowListCopyWindowInfo(
            options, Quartz.kCGNullWindowID
        )

        best_window: Optional[Rect] = None
        best_window_id: Optional[int] = None
        best_area = 0

        for window in window_list:
            owner = window.get(Quartz.kCGWindowOwnerName, '')
            if owner in self.app_names:
                bounds = window.get(Quartz.kCGWindowBounds, {})
                width = int(bounds.get('Width', 0))
                height = int(bounds.get('Height', 0))
                window_id = int(window.get(Quartz.kCGWindowNumber, 0))

                if width > self.min_width and height > self.min_height:
                    area = width * height
                    if area > best_area:
                        best_area = area
                        best_window = Rect(
                            x=int(bounds.get('X', 0)),
                            y=int(bounds.get('Y', 0)),
                            width=width,
                            height=height
                        )
                        best_window_id = window_id
        return (best_window, best_window_id) if best_window else None

    def _is_effective_window(self, rect: Rect) -> bool:
        """判断窗口尺寸是否达到有效主窗口标准"""
        return (
            rect.width >= self.min_effective_width
            and rect.height >= self.min_effective_height
        )

    def _activate_wechat(self) -> bool:
        """尝试激活微信应用。

        Returns:
            bool: 激活是否成功。调用方必须检查返回值——丢弃它会让「激活失败
            但流程继续」变成静默的状态，区域截图随后会截到遮挡物。
        """
        try:
            return bool(self.automation.activate_app("WeChat"))
        except Exception as e:  # noqa: BLE001
            _logger.warning("[WindowCapture] 激活微信异常: %s", e)
            return False

    def _ensure_wechat_foreground(self) -> bool:
        """Make WeChat the frontmost app, and confirm it actually got there.

        ``activate_app`` is a request, not a fact: AppleScript returns 0 for
        "I asked the app to activate" and the frontmost check is a separate
        round trip that can disagree. Region capture reads the *screen*, so it
        silently produces another app's pixels unless frontmost is verified.
        """
        if self._activate_wechat():
            time.sleep(0.3)
        ok, front = self.automation.get_frontmost_app("WeChat")
        if not ok:
            _logger.warning("[WindowCapture] 前台校验失败: frontmost=%r", front)
        return ok

    def _get_scale_factor(self) -> float:
        """获取主屏幕的 Retina 缩放因子"""
        try:
            screen = AppKit.NSScreen.mainScreen()
            if screen is not None:
                return float(screen.backingScaleFactor())
        except Exception as e:
            _logger.warning(f"获取屏幕缩放因子失败: {e}")
        return 1.0

    def _screen_recording_error(self) -> Optional[str]:
        """Why this capture cannot possibly work, or None if it still might.

        ``CGWindowListCopyWindowInfo`` needs no TCC grant, so on a machine that
        was never authorised for screen recording, window discovery and the
        size check both succeed. The failure only surfaces later, at
        ``screencapture`` time, as an opaque non-zero exit — and by then the
        error blames the window size, sending the operator to resize a window
        that was never the problem.

        ``CGPreflightScreenCaptureAccess`` is used to *disprove* only: False
        means this process really cannot capture, which is the direction that
        was verified against ``screencapture``'s actual exit code. True is not
        treated as proof and never short-circuits the normal path.
        """
        try:
            if Quartz.CGPreflightScreenCaptureAccess():
                return None
        except AttributeError:
            return None  # pre-10.15: the OS never gated screen capture
        except Exception as exc:  # noqa: BLE001
            return f"屏幕录制权限检测失败: {exc}"
        return (
            "缺少「屏幕录制」权限，screencapture 取不到画面。"
            "请在「系统设置 → 隐私与安全性 → 录屏与系统录音」中授权本应用，然后重启应用。"
        )

    def _to_screencapture_region(self, rect: Rect) -> str:
        """将 Rect 转换为 screencapture -R 参数格式"""
        return f"{rect.x},{rect.y},{rect.width},{rect.height}"

    def _do_capture(self, rect: Rect, window_id: int) -> None:
        """执行截图命令。

        优先使用 -l <windowid> 只截取指定窗口（不受其他窗口覆盖影响）。

        ``-l`` 对微信是必然失败的——窗口有透明合成层，screencapture 报
        "could not create image from window"，四次实测无一成功。所以区域截图
        不是可有可无的降级，而是**本机唯一能出图的路径**。它读的是屏幕而非
        窗口，因此只有在确认微信确实在前台时才可信：否则截到的是遮挡物，
        而后续 OCR 和视觉动作会基于这张错图全部算错，且不报错。

        这就是为什么降级前必须校验前台，而不是 sleep 一下就当没事发生。
        """
        ok, err = self.automation.capture_screen(
            rect, self.output_path, window_id=window_id if window_id else None
        )
        if ok:
            return

        _logger.info("[WindowCapture] 按窗口截图不可用(%s)，改用区域截图", err)

        if not self._ensure_wechat_foreground():
            raise RuntimeError(
                f"窗口截图不可用（{err}），且无法确认微信处于前台。"
                "区域截图会截到遮挡窗口的内容，已中止以免产生错误截图。"
                "请手动将微信置于前台后重试。"
            )

        fallback_ok, fallback_err = self.automation.capture_screen(
            rect, self.output_path, window_id=None
        )
        if not fallback_ok:
            raise RuntimeError(f"截图失败: window={err}; region={fallback_err}")
        _logger.info("[WindowCapture] 区域截图成功（已确认微信在前台）")

    #: 微信特有的界面词。用来确认「这张图是微信」而不是「这张图有文字」——
    #: 后者对任何应用都成立，等于没有校验。
    _WECHAT_MARKERS = ("搜索", "微信", "通讯录", "聊天信息", "WeChat", "朋友圈")

    def _validate_wechat_screenshot(self, image_path: str) -> bool:
        """验证截图内容确实是微信窗口。

        用项目自带的 :class:`VisionOCREngine`（macOS Vision 框架，零新增依赖）
        读整图，要求命中微信特有词。整图而非裁剪顶部：搜索框在 2x Retina 下
        位于物理像素 x≈166 起，裁剪区域写死像素会在缩放变化时切空，而区域
        截图本身又不保证窗口是原点的。

        OCR 不可用时放行：主 OCR 走 qwen-vl-ocr，验证只是防「截到遮挡物」
        的第二道闸，不该成为单点故障。
        """
        try:
            from src.ocr.vision_ocr import VisionOCREngine
        except Exception as e:  # noqa: BLE001
            _logger.debug("VisionOCREngine 不可用，跳过截图内容验证: %s", e)
            return True

        try:
            elements = VisionOCREngine().recognize(image_path)
        except Exception as e:  # noqa: BLE001
            _logger.warning("截图验证 OCR 失败，跳过验证: %s", e)
            return True

        texts = [
            getattr(el, "text", "") or ""
            for el in elements
            if getattr(el, "text", "")
        ]
        if not texts:
            _logger.warning("截图验证: OCR 未识别到任何文字")
            return False

        hits = [m for m in self._WECHAT_MARKERS if any(m in t for t in texts)]
        if hits:
            _logger.info("截图验证通过，命中微信特征词: %s", hits)
            return True

        _logger.warning(
            "截图验证失败: 识别到 %d 段文字但无微信特征词，样本=%s",
            len(texts), texts[:8],
        )
        return False

    def capture(self) -> CaptureResult:
        """
        查找并截图微信主窗口。

        如果窗口尺寸过小（未登录/浮窗），会先尝试激活微信并等待后重试一次。
        重试后仍无效则抛出 WeChatNotReadyError，提示用户可能需要扫码登录。

        Returns:
            CaptureResult: 包含图片路径和窗口几何信息

        Raises:
            WindowNotFoundError: 未找到任何微信窗口
            WeChatNotReadyError: 窗口尺寸异常，可能需要扫码登录
            CaptureValidationError: 截图内容验证失败
            RuntimeError: 缺少屏幕录制权限
        """
        t_capture_start = time.time()
        # Checked before window discovery on purpose: the grant gates the whole
        # capture, and reporting it first means an unauthorised machine is told
        # what it actually lacks instead of what it should resize.
        blocked = self._screen_recording_error()
        if blocked:
            raise RuntimeError(blocked)

        # 清理旧截图（超过1小时的临时文件，避免 /tmp 无限累积）
        try:
            cutoff = time.time() - 3600
            for old in glob.glob(os.path.join(tempfile.gettempdir(), "wechat_capture_*.png")):
                try:
                    if os.path.getmtime(old) < cutoff:
                        os.remove(old)
                except OSError as e:
                    _logger.warning("cleanup old screenshot failed: %s", e)
        except Exception as e:
            _logger.debug("[WindowCapture] 清理旧截图失败: %s", e)

        # 每次调用生成新的输出路径，避免覆盖旧截图
        # 这是 SmartPerceptionPipeline 像素 diff 正确工作的前提
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        pid = os.getpid()
        self.output_path = os.path.join(
            tempfile.gettempdir(), f"wechat_capture_{ts}_{pid}.png"
        )

        t_find_start = time.time()
        result = self._find_window()
        t_find_ms = (time.time() - t_find_start) * 1000
        if result is None:
            raise WindowNotFoundError("WeChat window not found")

        window_rect, window_id = result

        if not self._is_effective_window(window_rect):
            # 尝试激活微信并等待恢复。激活结果不检查也没关系：下面会重新
            # _find_window 拿几何，激活失败会表现为「窗口依旧太小」并抛出，
            # 而不是被静默跳过。
            self._activate_wechat()
            time.sleep(2.0)
            result = self._find_window()

            if result is None:
                raise WindowNotFoundError("WeChat window not found after activation")

            window_rect, window_id = result

            if not self._is_effective_window(window_rect):
                raise WeChatNotReadyError(
                    f"微信窗口尺寸异常 ({window_rect.width}x{window_rect.height})，"
                    "可能需要扫码登录或主窗口未展开"
                )

        t_screenshot_start = time.time()
        self._do_capture(window_rect, window_id)
        t_screenshot_ms = (time.time() - t_screenshot_start) * 1000

        # 验证截图内容
        t_validate_start = time.time()
        is_valid = self._validate_wechat_screenshot(self.output_path)
        t_validate_ms = (time.time() - t_validate_start) * 1000
        if not is_valid:
            raise CaptureValidationError(
                "截图验证失败：截到的内容不像微信窗口，可能有其他窗口覆盖"
            )

        scale_factor = self._get_scale_factor()
        t_total_ms = (time.time() - t_capture_start) * 1000
        _logger.info(
            f"[Perf][Capture] total={t_total_ms:.0f}ms "
            f"find_window={t_find_ms:.0f}ms screenshot={t_screenshot_ms:.0f}ms "
            f"validate={t_validate_ms:.0f}ms"
        )

        return CaptureResult(
            image_path=self.output_path,
            window_rect=window_rect,
            scale_factor=scale_factor
        )
