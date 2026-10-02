"""Dual-path location: element library first, multimodal vision as the fallback.

The project has two ways to answer "where on screen is the thing I need to
click", and they are not competing implementations — they cover different
ground:

  **Path A — element library (影刀 style).** The target exposes a locatable
  element: a web page has a DOM node, a desktop window has a region that can be
  named, pinned to an OCR anchor, or matched as an image template. Resolution is
  local, costs no network call, and is deterministic once the element is defined.
  Fast, cheap, reproducible.

  **Path B — multimodal operation.** The target *cannot* be captured into
  elements: it renders its UI at runtime, draws to a canvas, remotes its frame
  buffer, or simply has no inspectable structure. There is nothing to capture, so
  the only way to locate anything is to show a vision model a screenshot and ask
  it where the thing is. Costs a model call per action, is non-deterministic, and
  still works when the app is completely opaque.

Neither replaces the other. WeChat is a live demonstration of why: the message
list is *element-free* and is read by multimodal perception (`perceive`), while
the search box, the input area and the back button are perfectly locatable
regions that a flow can name and reuse. A flow that can use both is strictly
stronger than one that picks one.

The abstraction that makes this work: **both paths return a `Located` — a point
on screen plus which path produced it.** Downstream action nodes consume a point
and do not care how it was found, so a flow can switch a single node from
``element`` to ``vision`` without touching anything else, and the trace records
which path ran so a mis-click is diagnosable.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LocateMode(str, Enum):
    """How a target should be located."""

    ELEMENT = "element"
    """Path A only. Fail if the element is missing — never call a model."""

    VISION = "vision"
    """Path B only. Always call the vision model, even if an element exists."""

    AUTO = "auto"
    """Try Path A, fall back to Path B on failure. The default."""

    ELEMENT_STRICT = "element_strict"
    """Path A, and failure is fatal for the run. For a stable element you refuse
    to have guessed by a model."""


@dataclass
class Target:
    """An application the bot can drive, and how to locate things inside it.

    ``match`` is a case-insensitive regex tested against the window title (for
    desktop targets) or the URL (for web targets).
    """

    name: str
    kind: str  # "desktop" | "web"
    match: str = ""
    mode: LocateMode = LocateMode.AUTO
    #: Which window to capture, in priority order. A WeChat install can present
    #: itself as either name depending on the version in use.
    window_titles: list[str] = field(default_factory=list)
    url_pattern: str = ""
    description: str = ""

    def matches_title(self, title: str) -> bool:
        if not self.match:
            return any(t.lower() == title.lower() for t in self.window_titles)
        try:
            return bool(re.search(self.match, title, re.IGNORECASE))
        except re.error:
            return False

    def matches_url(self, url: str) -> bool:
        if self.url_pattern:
            try:
                return bool(re.search(self.url_pattern, url, re.IGNORECASE))
            except re.error:
                return False
        if self.match:
            try:
                return bool(re.search(self.match, url, re.IGNORECASE))
            except re.error:
                return False
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "match": self.match,
            "mode": self.mode.value if isinstance(self.mode, LocateMode) else self.mode,
            "window_titles": self.window_titles,
            "url_pattern": self.url_pattern,
            "description": self.description,
        }


@dataclass
class Located:
    """A resolved point on screen, with provenance.

    ``source`` is the whole point of this class: a flow that clicked the wrong
    thing needs to answer "did the element library lie, or did the model?", and
    the answer has to have been recorded at resolve time.
    """

    x: int
    y: int
    source: str  # "element" | "vision" | "manual" | "fixed"
    confidence: float = 1.0
    label: str = ""
    element_id: str | None = None
    element_name: str | None = None
    mode: str = ""
    elapsed_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "source": self.source,
            "confidence": self.confidence,
            "label": self.label,
            "element_id": self.element_id,
            "element_name": self.element_name,
            "mode": self.mode,
            "elapsed_ms": self.elapsed_ms,
            "detail": self.detail,
        }


#: Built-in targets. The WeChat entry is what the project actually drives today.
BUILTIN_TARGETS: tuple[Target, ...] = (
    Target(
        name="wechat",
        kind="desktop",
        match=r"^(WeChat|微信)$",
        mode=LocateMode.AUTO,
        window_titles=["WeChat", "微信"],
        description="微信 Mac 版。消息区为多模态感知，会话列表项/输入框/返回按钮为元素。",
    ),
    Target(
        name="any_window",
        kind="desktop",
        match=".*",
        mode=LocateMode.VISION,
        window_titles=[],
        description="任意前台窗口，无可用元素结构时只能多模态定位。",
    ),
)


class TargetRegistry:
    """Where targets come from, and how a flow picks one.

    Persisted targets live in the element library's own store so an operator can
    add one from the canvas without a code change — same principle as a node
    type being registered rather than hardcoded.
    """

    def __init__(self, extra: list[Target] | None = None) -> None:
        self._targets: dict[str, Target] = {t.name: t for t in BUILTIN_TARGETS}
        for target in extra or ():
            self._targets[target.name] = target

    def register(self, target: Target) -> Target:
        self._targets[target.name] = target
        return target

    def get(self, name: str) -> Target:
        if name not in self._targets:
            raise KeyError(f"未知目标 {name!r}，已注册：{sorted(self._targets)}")
        return self._targets[name]

    def has(self, name: str) -> bool:
        return name in self._targets

    def list(self) -> list[dict[str, Any]]:
        return [t.to_dict() for t in self._targets.values()]

    def for_title(self, title: str) -> Target | None:
        """Best target for a window title, preferring the most specific match."""
        best: tuple[int, Target] | None = None
        for target in self._targets.values():
            if target.matches_title(title):
                score = len(target.match) if target.match else 0
                if best is None or score > best[0]:
                    best = (score, target)
        return best[1] if best else None

    def for_url(self, url: str) -> Target | None:
        for target in self._targets.values():
            if target.kind == "web" and target.matches_url(url):
                return target
        return None

    def mode_for(self, target_name: str) -> LocateMode:
        return self.get(target_name).mode


_registry: TargetRegistry | None = None


def get_target_registry() -> TargetRegistry:
    global _registry
    if _registry is None:
        _registry = TargetRegistry()
    return _registry


def resolve(
    ctx: Any,
    description: str,
    target: str = "wechat",
    mode: str | LocateMode | None = None,
    element: str | None = None,
    confidence: float = 0.6,
    hint: dict[str, Any] | None = None,
) -> Located:
    """Resolve a screen point for ``description`` through the configured path.

    This is the single entry point both the action nodes and a flow author use.
    ``mode`` overrides the target's configured mode; when omitted, the target's
    ``AUTO`` mode applies, which is element-first with a vision fallback.

    Raises :class:`~src.flow.schema.NodeError` when neither path can produce a
    point — a silent miss would be a click at a default coordinate, which in a
    bot that sends messages is worse than a clean failure.
    """
    from .elements import locate_element
    from .schema import NodeError
    from .vision import locate_by_vision

    started = time.monotonic()
    registry = get_target_registry()
    try:
        resolved = registry.get(target)
    except KeyError as exc:
        raise NodeError(str(exc)) from exc

    effective = LocateMode(mode) if mode else resolved.mode

    if effective in (LocateMode.ELEMENT, LocateMode.AUTO, LocateMode.ELEMENT_STRICT):
        name = element or description
        try:
            found = locate_element(ctx, name)
        except Exception as exc:  # noqa: BLE001 - a miss is a normal outcome
            found = None
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = "元素库未命中"
        if found is not None:
            found.mode = effective.value
            found.elapsed_ms = int((time.monotonic() - started) * 1000)
            return found
        if effective in (LocateMode.ELEMENT, LocateMode.ELEMENT_STRICT):
            raise NodeError(
                f"目标 {target!r} 配置为 {effective.value}，元素 {name!r} 未能定位：{reason}"
            )

    # Path B — multimodal. Reached when mode is VISION, or AUTO fell through.
    located = locate_by_vision(
        ctx,
        description,
        target=target,
        confidence=confidence,
        hint=hint,
        screenshot_path=(ctx.scope.get("screenshot_path") if ctx else None),
    )
    located.mode = effective.value
    located.elapsed_ms = int((time.monotonic() - started) * 1000)
    return located
