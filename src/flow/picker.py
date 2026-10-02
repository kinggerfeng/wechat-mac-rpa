"""Element picking: turn a rectangle the user drew into a stored element.

The picker is the other half of the recorder. A recording gives coordinates the
user happened to click; a picked element gives a rectangle that can be resolved
again later, optionally anchored to the text inside it so it survives the window
moving or resizing.

What a pick produces, in order of preference:

1. **An OCR anchor.** If the drawn rectangle contains text, that text is stored
   and the element is resolved by re-reading it at run time. This is what makes
   an element survive a window move, and it costs nothing because OCR here is
   the on-device Vision framework.
2. **An image template.** If the rectangle has distinctive pixels but no text —
   an icon, a coloured button — the cropped region is stored as a template and
   matched at run time.
3. **A plain rectangle.** The last resort, and the node says so, because a bare
   rect stops working the moment anything moves.

The choice is not made silently. The stored element records which strategy it
uses in ``meta.strategy`` so the canvas can show "OCR 锚点" next to it and the
author knows what they got.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .schema import NodeError

#: Where cropped templates are written. Under the repo's data dir so the file
#: survives a restart and the element is not silently unresolvable tomorrow.
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "data" / "element_templates"

#: A rectangle smaller than this is almost certainly a stray drag, not a target.
_MIN_SIDE = 4


def _ocr_engine() -> Any:
    from src.ocr.vision_ocr import VisionOCREngine  # noqa: PLC0415

    return VisionOCREngine()


def pick_from_rect(
    name: str,
    image_path: str,
    rect: dict[str, Any],
    *,
    flow_id: str | None = None,
    save_template: bool = True,
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    """Build an element from a rectangle drawn on ``image_path``.

    ``rect`` is in the *screenshot's* pixel space: ``x``, ``y``, ``width``,
    ``height``. The stored element is window-relative, which is what
    :mod:`src.flow.elements` resolves against, so the caller passes the window
    origin separately when it knows it.

    Returns the stored element, with ``meta.strategy`` naming which of the three
    strategies was chosen and why.
    """
    from .elements import capture_element

    source = Path(image_path)
    if not source.is_file():
        raise NodeError(f"截图不存在: {image_path}")

    x = int(rect.get("x", 0))
    y = int(rect.get("y", 0))
    width = int(rect.get("width", 0))
    height = int(rect.get("height", 0))
    if width < _MIN_SIDE or height < _MIN_SIDE:
        raise NodeError(
            f"选区太小（{width}×{height}），至少需要 {_MIN_SIDE}×{_MIN_SIDE} 像素"
        )

    ocr_text, ocr_confidence = _text_in_rect(source, x, y, width, height, min_confidence)

    image_template = ""
    if save_template and not ocr_text:
        image_template = _save_template(source, name, x, y, width, height)

    if ocr_text:
        strategy = "ocr_anchor"
        kind = "ocr"
    elif image_template:
        strategy = "image_template"
        kind = "image"
    else:
        strategy = "fixed_rect"
        kind = "rect"

    element = capture_element(
        name=name,
        kind=kind,
        rect={"x": x, "y": y, "width": width, "height": height, "relative_to": "window"},
        flow_id=flow_id,
        ocr_text=ocr_text,
        image_path=image_template or None,
        meta={
            "strategy": strategy,
            "ocr_confidence": round(ocr_confidence, 3),
            "source_image": str(source),
        },
    )
    return element


def _text_in_rect(
    source: Path, x: int, y: int, width: int, height: int, min_confidence: float
) -> tuple[str, float]:
    """The text whose centre falls inside the drawn rectangle.

    Matching on the *centre* rather than full overlap is deliberate: a button's
    label overlaps its own border, and a word that merely crosses the edge of a
    wide selection is not what the user drew around.
    """
    try:
        elements = _ocr_engine().recognize(str(source))
    except Exception:  # noqa: BLE001 - OCR is a bonus, not a requirement
        return "", 0.0

    picked: list[tuple[float, str]] = []
    for item in elements or []:
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        confidence = float(getattr(item, "confidence", 0.0) or 0.0)
        if confidence < min_confidence:
            continue
        center = getattr(item, "center", None)
        if center is None:
            continue
        cx, cy = float(center.x), float(center.y)
        if x <= cx <= x + width and y <= cy <= y + height:
            picked.append((confidence, text))

    if not picked:
        return "", 0.0
    # Highest confidence wins, not longest: a long string with 0.5 confidence is
    # a worse anchor than a short one at 0.99.
    picked.sort(key=lambda item: item[0], reverse=True)
    return picked[0][1], picked[0][0]


def _save_template(source: Path, name: str, x: int, y: int, width: int, height: int) -> str:
    """Crop the rectangle to a PNG and return its path, or "" on any failure."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return ""
    try:
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "element"
        out = TEMPLATE_DIR / f"{safe}_{int(time.time())}.png"
        with Image.open(source) as img:
            img.crop((x, y, x + width, y + height)).save(out)
        return str(out)
    except Exception:  # noqa: BLE001 - a missing template degrades to a rect
        return ""


def list_templates() -> list[dict[str, Any]]:
    """On-disk template files, for the element page to show or clean up."""
    if not TEMPLATE_DIR.is_dir():
        return []
    return [
        {"name": p.name, "path": str(p), "size": p.stat().st_size, "modified": p.stat().st_mtime}
        for p in sorted(TEMPLATE_DIR.glob("*.png"))
    ]
