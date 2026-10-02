"""Lazy service construction for flow runs.

Every heavy subsystem is built on first use and shared for the life of a run.
Two reasons it is not done at import time:

* Constructing a capture or perception pipeline prompts for the macOS screen
  recording permission. A flow that is only being *validated* in the canvas must
  not trigger that prompt.
* Perception, LLM and memory each cost seconds to construct; a 20-node flow that
  constructs them per node would spend a minute on setup.

The ``NoOp`` automation seam is used whenever ``dry_run`` is set, so a flow can
be stepped through in the canvas without touching the screen.
"""

from __future__ import annotations

import os
from typing import Any

from .context import FlowContext

_SERVICES_REGISTERED = False


def register_default_services(ctx: FlowContext, dry_run: bool = False) -> None:
    """Attach factories for every built-in service to ``ctx``."""
    global _SERVICES_REGISTERED
    if not _SERVICES_REGISTERED:
        _load_env()
        _SERVICES_REGISTERED = True

    ctx.register_service_factory("automation", lambda: _build_automation(dry_run))
    ctx.register_service_factory("profile", _build_profile)
    ctx.register_service_factory("perception", lambda: _build_perception(ctx, dry_run))
    ctx.register_service_factory("capture", lambda: _build_capture(ctx, dry_run))
    # Local macOS Vision OCR. elements.py resolves an element's ocr_text anchor
    # through this service, so without the registration every OCR-anchored
    # element raised "未注册的服务: 'ocr'" and silently fell back to a fixed rect
    # — the element appeared to work until the window moved. Vision runs on
    # device, so unlike the VLM path it costs nothing per call.
    ctx.register_service_factory("ocr", _build_ocr)
    ctx.register_service_factory("sender", _build_sender)
    ctx.register_service_factory("session", _build_session)
    ctx.register_service_factory("memory", _build_memory)
    ctx.register_service_factory("generator", lambda: _build_generator(ctx, dry_run))
    ctx.register_service_factory("policy", _build_policy)
    ctx.register_service_factory("clicker", lambda: _build_clicker(ctx, dry_run))
    ctx.register_service_factory("logger", _build_logger)
    ctx.register_service_factory("tools", _build_tools)
    ctx.register_service_factory("case_db", _build_case_db)


def _load_env() -> None:
    """Read .env once, matching run_bot.py's hand-rolled parser."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass


def _build_automation(dry_run: bool) -> Any:
    if dry_run:
        from src.action.system_automation import NoOpSystemAutomation

        return NoOpSystemAutomation()
    from src.action.system_automation import MacOSSystemAutomation

    return MacOSSystemAutomation()


def _build_profile() -> Any:
    from src.layout.profile import PROFILE_WECHAT_MAC_1760X1280

    return PROFILE_WECHAT_MAC_1760X1280


class StubCapture:
    """Stands in for :class:`WindowCapture` during a dry run.

    A dry run must not touch the screen: capturing activates WeChat and prompts
    for screen recording, which is exactly what a dry run is meant to avoid. The
    stub still produces a real file so downstream nodes that read ``image_path``
    have something to open, and still reports the layout profile's nominal
    geometry so window-relative element maths has plausible numbers.
    """

    def __init__(self, profile: Any) -> None:
        from src.models.base import Rect

        self._profile = profile
        self._rect = Rect(0, 0, profile.window_width, profile.window_height)

    def capture(self) -> Any:
        import time
        from types import SimpleNamespace

        path = f"/tmp/wechat-dryrun-{int(time.time() * 1000)}.png"
        try:
            from PIL import Image

            Image.new("RGB", (self._rect.width, self._rect.height), (240, 240, 240)).save(path)
        except Exception:  # noqa: BLE001 - the path is enough for a dry run
            pass
        return SimpleNamespace(image_path=path, window_rect=self._rect, scale_factor=2.0)

    def recognize(self, image_path: str) -> list[Any]:
        return []


def _build_capture(ctx: FlowContext | None = None, dry_run: bool = False) -> Any:
    if dry_run:
        return StubCapture(_build_profile())
    from src.capture.window_capture import WindowCapture

    return WindowCapture()


def _build_ocr() -> Any:
    """On-device OCR via the macOS Vision framework.

    Kept separate from the VLM in ``_build_perception`` on purpose: an element's
    anchor text is re-read on every single resolve, and a model call per click
    would make the element library the most expensive part of a flow.
    """
    from src.ocr.vision_ocr import VisionOCREngine

    return VisionOCREngine()


def _build_perception(ctx: FlowContext, dry_run: bool) -> Any:
    if dry_run:
        from src.perception.vision_pipeline import VisionPipeline

        return VisionPipeline(ctx.service("profile"))
    from src.perception.smart_pipeline import SmartPerceptionPipeline

    return SmartPerceptionPipeline(ctx.service("profile"))


def _build_sender() -> Any:
    from src.action.message_sender import WeChatMessageSender

    return WeChatMessageSender(silent_mode=True)


def _build_session() -> Any:
    from src.session.global_store import GlobalStore

    return GlobalStore()


def _build_memory() -> Any:
    from src.memory.engine import MemoryEngine

    return MemoryEngine(llm_client=_optional_llm())


def _build_generator(ctx: FlowContext, dry_run: bool) -> Any:
    from src.reply.generator import ReplyGenerator

    if dry_run:
        return ReplyGenerator(llm_client=None, memory_engine=None)
    return ReplyGenerator(
        llm_client=_required_llm(),
        memory_engine=ctx.service("memory"),
        tool_registry=ctx.service("tools"),
    )


def _build_policy() -> Any:
    from src.reply.policy import ReplyPolicy

    return ReplyPolicy()


def _build_clicker(ctx: FlowContext, dry_run: bool) -> Any:
    from src.action.chat_list_clicker import ChatListClicker

    # Window geometry is whatever `capture`/`perceive` last bound; before the first
    # one runs, fall back to the layout profile's nominal size so a flow that
    # starts with switch_chat still has something to click against.
    profile = ctx.service("profile")
    rect = ctx.scope.get("window_rect")
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        window_rect = (rect[0], rect[1], rect[2], rect[3])
    else:
        window_rect = (0, 0, profile.window_width, profile.window_height)
    scale = ctx.scope.get("scale_factor")
    scale_factor = float(scale) if isinstance(scale, (int, float)) else 2.0
    return ChatListClicker(window_rect=window_rect, scale_factor=scale_factor, automation=ctx.service("automation"))


def _build_logger() -> Any:
    from src.logging.bot_logger import get_logger

    return get_logger()


def _build_tools() -> Any:
    from src.tools import get_registry, register_builtin_tools

    registry = get_registry()
    register_builtin_tools(registry)
    return registry


def _build_case_db() -> Any:
    from src.badcase.case_db import get_db

    return get_db()


def _optional_llm() -> Any:
    try:
        from src.utils.qwen_client import QwenClient

        return QwenClient()
    except Exception:  # noqa: BLE001 - memory works without an LLM, it just skips updates
        return None


def _required_llm() -> Any:
    from src.utils.qwen_client import QwenClient

    return QwenClient()
