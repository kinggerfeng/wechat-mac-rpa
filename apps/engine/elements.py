"""Element library: named screen regions a flow can click or read by name.

This is the piece the project deliberately did not have before. The existing
pipeline locates everything by OCR and layout inference, which is what makes it
survive a WeChat redesign. That strength is why elements here are **rectangles
relative to a window**, not absolute screen coordinates: a rect saved against a
1760x1280 window still points at the search box after the user drags the window
somewhere else, and after a Retina/non-Retina switch when re-resolved at click
time.

Three kinds:
  ``rect``   — a region of the WeChat window, optionally with an OCR anchor text
  ``ocr``    — resolved by finding ``ocr_text`` via OCR, so the point tracks
               layout changes
  ``image``  — resolved by template matching a stored PNG
"""

from __future__ import annotations

import time
from typing import Any

from .context import FlowContext

#: Windows we consider the element's coordinate space, in order of preference.
WINDOW_TITLES = ("WeChat", "微信")


def window_origin(automation: Any) -> tuple[int, int, int, int] | None:
    """Current on-screen rect of the WeChat window, or ``None``."""
    try:
        ok, rect, _ = automation.get_window_rect("WeChat")
    except Exception:  # noqa: BLE001 - automation impls vary; a miss is not fatal here
        return None
    if not ok or rect is None:
        return None
    return (rect.x, rect.y, rect.width, rect.height)


def locate_element(ctx: FlowContext, name: str) -> "Located | None":
    """Resolve a named element to a ``Located``, or ``None`` if it does not resolve.

    The single Path-A entry point. Mirrors :func:`apps.engine.strategy.resolve`'s
    return type so a caller — or a trace reader — can tell which path produced a
    point without inspecting anything else.
    """
    from .store import get_store
    from .strategy import Located

    element = get_store().find_element(name)
    if element is None:
        return None
    if element.get("kind") == "web":
        return _locate_web(element)
    return _locate_desktop(ctx, element)


def resolve_element_center(ctx: FlowContext, name: str) -> tuple[int, int] | None:
    """Absolute screen coordinates of a named element's centre, or ``None``.

    Resolution order: OCR anchor first (survives a layout shift inside the
    window), then the stored rect scaled onto the window's current geometry.
    """
    located = locate_element(ctx, name)
    return (located.x, located.y) if located else None


def _locate_desktop(ctx: FlowContext, element: dict[str, Any]) -> "Located":
    from .strategy import Located

    kind = element.get("kind", "rect")
    rect = element.get("rect") or {}
    window = window_origin(ctx.service("automation"))
    origin_x, origin_y, window_w, window_h = window if window else (0, 0, 0, 0)

    if kind == "ocr" or (kind == "rect" and element.get("ocr_text")):
        hit = _resolve_by_ocr(ctx, element)
        if hit is not None:
            return Located(
                x=hit[0], y=hit[1], source="element", confidence=0.95,
                label=str(element.get("ocr_text", ""))[:80],
                element_id=element.get("id"), element_name=element.get("name"),
                detail={"resolved_by": "ocr_anchor"},
            )

    if not rect:
        raise ValueError(f"元素 {element.get('name')!r} 没有可用的 rect")

    rel_x = float(rect.get("x", 0))
    rel_y = float(rect.get("y", 0))
    width = float(rect.get("width", 0))
    height = float(rect.get("height", 0))

    if window and rect.get("relative_to") == "window" and window_w:
        # Re-project onto the window's current size.
        base_w = float(rect.get("base_width") or window_w)
        base_h = float(rect.get("base_height") or window_h)
        rel_x = rel_x * (window_w / base_w)
        rel_y = rel_y * (window_h / base_h)
        width = width * (window_w / base_w)
        height = height * (window_h / base_h)

    scale = float(ctx.scope.get("scale_factor") or 1.0) or 1.0
    return Located(
        x=int(origin_x + (rel_x + width / 2) * scale),
        y=int(origin_y + (rel_y + height / 2) * scale),
        source="element",
        confidence=1.0,
        label=element.get("name", ""),
        element_id=element.get("id"),
        element_name=element.get("name"),
        detail={"resolved_by": "rect"},
    )


def _locate_web(element: dict[str, Any]) -> "Located":
    """Path A for the web: resolve a CSS/XPath selector against the active page.

    Web elements do not go through screen coordinates at all — a browser that is
    scrolled or zoomed would make a click land in the wrong place, so the page is
    queried directly and the point is mapped to screen space only at the last
    moment, by the browser itself.
    """
    from .schema import NodeError
    from .strategy import Located

    rect = element.get("rect") or {}
    selector = str(rect.get("selector") or "")
    if not selector:
        raise NodeError(f"web 元素 {element.get('name')!r} 缺少 selector")
    bounds = rect.get("bounds")
    if not bounds:
        raise NodeError(
            f"web 元素 {element.get('name')!r} 没有 bounds，"
            "需要先在目标页面上采集一次（POST /api/elements/capture/web）"
        )
    return Located(
        x=int(bounds.get("x", 0)),
        y=int(bounds.get("y", 0)),
        source="element",
        confidence=1.0,
        label=element.get("name", ""),
        element_id=element.get("id"),
        element_name=element.get("name"),
        detail={"resolved_by": "dom", "selector": selector, "url": rect.get("url", "")},
    )


def _resolve_by_ocr(ctx: FlowContext, element: dict[str, Any]) -> tuple[int, int] | None:
    """Find the element's anchor text on screen and return the point nearest its rect."""
    target = (element.get("ocr_text") or "").strip()
    if not target:
        return None
    try:
        capture = ctx.service("capture")
        ocr = ctx.service("ocr")
    except Exception:  # noqa: BLE001
        return None

    try:
        shot = capture.capture()
        elements = ocr.recognize(shot.image_path)
    except Exception:  # noqa: BLE001
        return None

    window = window_origin(ctx.service("automation"))
    origin_x, origin_y = (window[0], window[1]) if window else (0, 0)
    rect = element.get("rect") or {}
    want_x = float(rect.get("x", 0))
    want_y = float(rect.get("y", 0))

    best = None
    best_distance = float("inf")
    for item in elements:
        if target not in (item.text or ""):
            continue
        cx, cy = item.center.x, item.center.y
        distance = abs(cx - want_x) + abs(cy - want_y)
        if distance < best_distance:
            best_distance = distance
            best = (cx, cy)
    if best is None:
        return None
    return (int(origin_x + best[0]), int(origin_y + best[1]))


def capture_element(
    name: str,
    kind: str,
    rect: dict[str, Any],
    flow_id: str | None = None,
    ocr_text: str = "",
    image_path: str | None = None,
    anchor: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a new element. ``rect`` should be window-relative."""
    from .store import get_store

    payload = dict(rect)
    payload.setdefault("relative_to", "window")
    return get_store().save_element(
        {
            "name": name,
            "kind": kind,
            "rect": payload,
            "anchor": anchor or {},
            "ocr_text": ocr_text,
            "image_path": image_path,
            "flow_id": flow_id,
            "meta": {**(meta or {}), "created_by": "capture", "created_at": time.time()},
        }
    )


def describe_element(element: dict[str, Any]) -> dict[str, Any]:
    """Canvas-facing shape for one element."""
    rect = element.get("rect") or {}
    return {
        "id": element.get("id"),
        "name": element.get("name"),
        "kind": element.get("kind"),
        "rect": {
            "x": rect.get("x", 0),
            "y": rect.get("y", 0),
            "width": rect.get("width", 0),
            "height": rect.get("height", 0),
            "relative_to": rect.get("relative_to", "window"),
        },
        "ocr_text": element.get("ocr_text", ""),
        "image_path": element.get("image_path"),
        "flow_id": element.get("flow_id"),
        "meta": element.get("meta", {}),
        "created_at": element.get("created_at"),
    }
