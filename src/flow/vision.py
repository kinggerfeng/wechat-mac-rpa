"""Path B — multimodal operation.

The existing ``_QwenAPIClient`` in ``src/perception/smart_pipeline.py`` is a VLM
transport whose *system prompt* happens to be a WeChat OCR engine. The transport
is generic: base64 image in, JSON out. This module reuses that shape and supplies
the prompts that a general-purpose visual operator needs — find this thing, do
that thing, is this true, describe what you see.

Nothing here replaces the element library. This is the path for screens with
nothing to capture: a remote desktop, a game, a canvas-rendered UI, a vendor app
that draws its own widgets. It costs a model call per action and is
non-deterministic, which is exactly why :mod:`src.flow.strategy` tries the
element library first and only falls through to here.

Coordinate handling is the subtle part and the reason this is a module rather
than a prompt. Vision models return coordinates in the image's own pixel space,
and a Retina screenshot is 2x the logical size the click API wants. Every
coordinate crossing this boundary is normalised to logical points, because a click
at 2x lands two inches from where you meant — and in a bot that types and sends,
that is not a recoverable error.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "qwen3.6-flash"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

LOCATE_PROMPT = """你是一个 GUI 视觉定位器。用户会描述他想在屏幕上点哪里。

规则：
1. 只返回一个 JSON，不要任何解释文字。
2. 坐标以图片左上角为原点，单位是图片的像素。
3. 如果界面上有多个符合描述的元素，选择最可能用于该操作的那一个。
4. 如果画面中根本不存在符合描述的元素，把 found 设为 false，不要编造坐标。
5. confidence 反映你的把握：0.9 以上表示位置明确；0.6-0.9 表示有多个候选已择优；低于 0.6 表示不确定。

输出格式：
{"found": true, "x": 123, "y": 456, "confidence": 0.9, "label": "你找到的元素描述", "reason": "为什么是这里"}
"""

ACT_PROMPT = """你是一个 GUI 操作规划器。用户会给出一个操作目标，你要看截图并决定下一步该做什么。

可用的 action：
- "click"：点击某点。必须同时给出 x、y。
- "double_click"：双击某点。
- "right_click"：右键某点。
- "type"：向当前焦点输入文本。必须给出 text；如果需要先点输入框，请在 points 数组里给出该输入框的坐标。
- "scroll"：滚动。direction 为 up/down/left/right，amount 为格数。
- "hotkey"：组合键。给出 keys，例如 "cmd+s"、"cmd+shift+4"、"enter"。
- "wait"：当前画面还没准备好，让系统等一下。
- "done"：目标已经达成。
- "blocked"：做不到。必须在 reason 里说明卡在哪里。

规则：
1. 只返回一个 JSON，不要解释文字。
2. 坐标以图片左上角为原点，单位是图片像素。
3. 一次只做一步。不要试图在一个响应里规划多步。
4. 如果当前画面完全不符合预期（例如停在登录页、弹了错误框），用 "blocked" 并说明。
5. 如果目标已经完成，用 "done"。

输出格式：
{"action": "click", "x": 123, "y": 456, "confidence": 0.9, "reason": "简短说明", "text": "", "keys": "", "direction": "", "amount": 0, "points": []}
"""

DESCRIBE_PROMPT = """你是一个界面结构分析器。描述这张截图里的界面：有哪些可交互区域、输入框、按钮、列表、菜单，它们的位置和大致功能。

只返回一个 JSON：
{"summary": "一句话概括这是什么界面", "regions": [{"label": "区域名", "x": 0, "y": 0, "width": 0, "height": 0, "type": "button|input|list|menu|text|unknown", "description": "这个区域是干什么的"}]}
"""

VERIFY_PROMPT = """你是一个界面校验器。用户会声称界面上存在某个状态。判断截图是否支持这个说法。

只返回一个 JSON：
{"holds": true, "confidence": 0.9, "evidence": "支持或反驳这个说法的画面证据"}
"""


@dataclass
class VisionAction:
    """One action the model decided on, in logical screen points."""

    action: str
    x: int | None = None
    y: int | None = None
    confidence: float = 0.0
    reason: str = ""
    text: str = ""
    keys: str = ""
    direction: str = ""
    amount: int = 0
    points: list[dict[str, int]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], scale: float) -> "VisionAction":
        """Parse a model reply, converting image pixels to logical points."""
        scale = scale if scale and scale > 0 else 1.0

        def point(value: Any) -> int | None:
            try:
                return int(round(float(value) / scale))
            except (TypeError, ValueError):
                return None

        raw_points = payload.get("points")
        points: list[dict[str, int]] = []
        if isinstance(raw_points, list):
            for item in raw_points:
                if isinstance(item, dict):
                    px, py = point(item.get("x")), point(item.get("y"))
                    if px is not None and py is not None:
                        points.append({"x": px, "y": py})

        return cls(
            action=str(payload.get("action") or "blocked").lower(),
            x=point(payload.get("x")),
            y=point(payload.get("y")),
            confidence=_as_float(payload.get("confidence"), 0.0),
            reason=str(payload.get("reason") or ""),
            text=str(payload.get("text") or ""),
            keys=str(payload.get("keys") or ""),
            direction=str(payload.get("direction") or ""),
            amount=int(payload.get("amount") or 0),
            points=points,
            raw=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "x": self.x,
            "y": self.y,
            "confidence": self.confidence,
            "reason": self.reason,
            "text": self.text,
            "keys": self.keys,
            "direction": self.direction,
            "amount": self.amount,
            "points": self.points,
        }


class VisionClient:
    """A VLM call with an image, returning parsed JSON.

    Deliberately separate from ``src/utils/qwen_client.py``: that one is the text
    and tool-calling client the reply generator uses, and routing a full-resolution
    screenshot through it would put megabytes of base64 into a conversation
    history that is also used for token accounting.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        self.model = model or os.environ.get("VISION_MODEL") or DEFAULT_MODEL
        self.base_url = base_url or os.environ.get("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY 未设置，多模态定位不可用")
        import httpx
        from openai import OpenAI

        # Proxy off for the same reason smart_pipeline does it: the workspace
        # endpoint hangs behind a local proxy.
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=httpx.Client(transport=httpx.HTTPTransport(proxy=None), timeout=timeout),
        )
        self.calls = 0
        self.total_ms = 0

    def ask(self, image_path: str, prompt: str, max_tokens: int = 2048) -> dict[str, Any]:
        with open(image_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("utf-8")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }
        ]
        started = time.monotonic()
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        self.calls += 1
        self.total_ms += int((time.monotonic() - started) * 1000)
        content = response.choices[0].message.content or ""
        from src.utils.json_extractor import extract_json

        return extract_json(content) or {"_raw": content}


_client: VisionClient | None = None
_client_lock_error: str | None = None


def get_vision_client() -> VisionClient:
    """Process-wide client. Failure is cached as a message so a misconfigured key
    does not cost a failed TLS handshake on every single locate call."""
    global _client, _client_lock_error
    if _client is not None:
        return _client
    if _client_lock_error is not None:
        raise RuntimeError(_client_lock_error)
    try:
        _client = VisionClient()
    except Exception as exc:  # noqa: BLE001
        _client_lock_error = f"视觉模型客户端不可用: {exc}"
        raise RuntimeError(_client_lock_error) from exc
    return _client


def reset_vision_client() -> None:
    """Drop the cached client. The settings page calls this after a key change."""
    global _client, _client_lock_error
    _client = None
    _client_lock_error = None


# ───────────────────────────────────────────────────────────── operations ──

def _screenshot(ctx: Any, screenshot_path: str | None = None) -> tuple[str, float, tuple[int, int, int, int] | None]:
    """Get an image and the scale that maps its pixels to logical screen points.

    ``screenshot_path`` is reused when the caller already has one — recapturing
    costs a window activation and a sleep, and between two vision calls in the same
    node the screen is unlikely to have changed.
    """
    path = screenshot_path or ctx.scope.get("screenshot_path")
    scale = _as_float(ctx.scope.get("scale_factor"), 1.0) or 1.0
    origin: tuple[int, int, int, int] | None = None
    if not path:
        from .elements import window_origin

        shot = ctx.service("capture").capture()
        path = shot.image_path
        scale = shot.scale_factor
        rect = shot.window_rect
        origin = (rect.x, rect.y, rect.width, rect.height)
    return str(path), scale, origin


def locate_by_vision(
    ctx: Any,
    description: str,
    target: str = "wechat",
    confidence: float = 0.6,
    hint: dict[str, Any] | None = None,
    screenshot_path: str | None = None,
) -> Any:
    """Path B: ask the model where ``description`` is, return a ``Located``."""
    from .schema import NodeError
    from .strategy import Located

    path, scale, origin = _screenshot(ctx, screenshot_path)
    prompt = LOCATE_PROMPT + f"\n\n需要定位的元素：{description}"
    payload = get_vision_client().ask(path, prompt)
    if not payload or payload.get("found") is not True:
        reason = (payload or {}).get("reason") or "模型未在画面中找到该元素"
        raise NodeError(f"多模态定位失败：{reason}")

    action = VisionAction.from_payload(payload, scale)
    if action.x is None or action.y is None:
        raise NodeError("多模态定位返回了空坐标")
    if action.confidence < confidence:
        raise NodeError(
            f"多模态定位置信度 {action.confidence:.2f} 低于阈值 {confidence:.2f}，拒绝点击"
        )
    offset_x, offset_y = (origin[0], origin[1]) if origin else (0, 0)
    return Located(
        x=action.x + offset_x,
        y=action.y + offset_y,
        source="vision",
        confidence=action.confidence,
        label=str((payload.get("label") or description)[:120]),
        detail={"reason": action.reason, "model_scale": scale, **(hint or {})},
    )


def plan_action(
    ctx: Any,
    goal: str,
    target: str = "wechat",
    screenshot_path: str | None = None,
    extra_context: str = "",
) -> VisionAction:
    """Path B: decide the next single action for ``goal``."""
    path, scale, _ = _screenshot(ctx, screenshot_path)
    prompt = ACT_PROMPT
    if extra_context:
        prompt += f"\n\n补充上下文：{extra_context}"
    prompt += f"\n\n操作目标：{goal}"
    payload = get_vision_client().ask(path, prompt)
    if not payload:
        raise RuntimeError("视觉模型返回空结果")
    return VisionAction.from_payload(payload, scale)


def describe_screen(ctx: Any, screenshot_path: str | None = None) -> dict[str, Any]:
    """Path B: describe the interface as a set of named, located regions."""
    path, scale, origin = _screenshot(ctx, screenshot_path)
    payload = get_vision_client().ask(path, DESCRIBE_PROMPT, max_tokens=4096)
    regions = []
    offset_x, offset_y = (origin[0], origin[1]) if origin else (0, 0)
    for item in payload.get("regions") or []:
        if not isinstance(item, dict):
            continue
        try:
            regions.append({
                "label": str(item.get("label", "")),
                "x": int(float(item.get("x", 0)) / scale) + offset_x,
                "y": int(float(item.get("y", 0)) / scale) + offset_y,
                "width": int(float(item.get("width", 0)) / scale),
                "height": int(float(item.get("height", 0)) / scale),
                "type": str(item.get("type", "unknown")),
                "description": str(item.get("description", "")),
            })
        except (TypeError, ValueError):
            continue
    return {"summary": str(payload.get("summary") or ""), "regions": regions}


def verify(ctx: Any, claim: str, screenshot_path: str | None = None) -> dict[str, Any]:
    """Path B: check a claim about the current screen."""
    path, _, _ = _screenshot(ctx, screenshot_path)
    payload = get_vision_client().ask(path, VERIFY_PROMPT + f"\n\n需要判断的说法：{claim}")
    return {
        "claim": claim,
        "holds": bool(payload.get("holds")),
        "confidence": _as_float(payload.get("confidence"), 0.0),
        "evidence": str(payload.get("evidence") or ""),
    }


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
