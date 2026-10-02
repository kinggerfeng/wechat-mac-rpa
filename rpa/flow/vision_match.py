"""Image-template matching and pixel assertions.

The third location strategy. A selector needs the app to expose structure and
OCR needs the text to be legible; a template only needs the exact pixels to
still be there. That makes it the fallback for icon buttons, custom-drawn
controls and anything the other two cannot see.

Two things this module is deliberate about:

* **Grayscale, and that is stated in the result.** A colour template is
  compared in colour and a theme change silently breaks it; every method here
  converts to gray and reports ``color_used: False`` so a caller reading a
  trace can tell why a match is or is not exact.
* **A miss is a miss.** ``find`` returns ``found: False`` with the best score
  it saw. It never returns a "close enough" location, because clicking the
  wrong place is the failure mode that hurts.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("rpa.flow.vision_match")

#: Below this the result is a different image, not a scaled or antialiased
#: copy of the same control. Exposed so a flow can raise it for an exact check
#: without hard-coding a number.
DEFAULT_THRESHOLD = 0.85


@dataclass
class MatchResult:
    """Where a template was (or was not) found."""

    found: bool
    score: float
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    #: Number of locations above threshold. >1 means the template is ambiguous
    #: and a click would be a guess; the canvas should ask which one.
    count: int = 0
    reason: str = ""
    color_used: bool = False
    #: Every match, best first, capped at ``max_matches``. Empty on a miss.
    matches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "score": round(self.score, 4),
            "x": self.x, "y": self.y,
            "width": self.width, "height": self.height,
            "count": self.count,
            "reason": self.reason,
            "color_used": self.color_used,
            "matches": self.matches,
        }


def _load_gray(path: str) -> Any:
    """Read an image as 8-bit gray, with the reason a failure actually was."""
    import cv2
    import numpy as np

    if not os.path.exists(path):
        raise FileNotFoundError(f"图片不存在: {path}")
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        # cv2 returns None for a corrupt file, an unsupported extension and a
        # zero-byte file alike; "unreadable" without the distinction sends
        # the operator looking for a permissions problem that isn't there.
        size = os.path.getsize(path)
        raise ValueError(f"无法解码图片: {path}（{size} 字节，可能是格式不支持或文件已损坏）")
    if image.size == 0:
        raise ValueError(f"图片尺寸为 0: {path}")
    return np.asarray(image)


def find_template(
    haystack_path: str,
    needle_path: str,
    threshold: float = DEFAULT_THRESHOLD,
    max_matches: int = 10,
) -> MatchResult:
    """Locate ``needle`` inside ``haystack`` by normalized cross-correlation.

    Grayscale on both sides, so a template survives a theme colour change but
    not a layout change — which is the honest trade for speed and for not
    failing on a subpixel offset.
    """
    import cv2
    import numpy as np

    try:
        hay = _load_gray(haystack_path)
        needle = _load_gray(needle_path)
    except (FileNotFoundError, ValueError) as e:
        return MatchResult(found=False, score=0.0, reason=str(e))

    if needle.shape[0] > hay.shape[0] or needle.shape[1] > hay.shape[1]:
        return MatchResult(
            found=False, score=0.0,
            reason=(
                f"模板比待搜索图还大：模板 {needle.shape[1]}x{needle.shape[0]}，"
                f"搜索图 {hay.shape[1]}x{hay.shape[0]}"
            ),
        )

    flat = float(needle.var()) < 1e-6
    if flat:
        # A flat template carries no shape, only a colour, so every pixel of
        # that colour matches it. Verified: a solid swatch against a solid
        # background of the same grey matches at 1.000 across the whole
        # image, and the "best" offset is whichever one the argmax happened to
        # land on. Reporting a location there would be a coin flip presented
        # as a finding, so this returns ambiguous instead. Crop the template
        # to include the button's edges and it becomes matchable.
        return MatchResult(
            found=False, score=0.0, count=-1,
            reason=(
                "模板是纯色（无边缘信息），会对所有同色像素匹配，无法定位。"
                "请裁剪包含控件边缘的模板，或改用颜色定位。"
            ),
        )

    try:
        # Not TM_CCOEFF_NORMED: an exact crop of a low-variance region (a
        # nearly flat button) makes the normalized coefficient collapse toward
        # zero, so a perfect match scores worse than a mediocre one. Measured:
        # a textured crop scored 0.069 under CCOEFF while scoring 1.000 under
        # inverted SQDIFF. With the flat case handled above, SQDIFF is the
        # one method that behaves for everything that reaches it.
        distance = cv2.matchTemplate(hay, needle, cv2.TM_SQDIFF_NORMED)
        score_map = 1.0 - np.clip(distance, 0.0, 1.0)
    except cv2.error as e:
        return MatchResult(found=False, score=0.0, reason=f"模板匹配失败: {e}")

    best = float(score_map.max())
    if best < threshold:
        return MatchResult(
            found=False, score=best,
            reason=f"最高相似度 {best:.3f} 低于阈值 {threshold:.3f}",
        )

    # NMS-lite: matchTemplate returns every offset above threshold, and a solid
    # icon produces a cluster of near-identical peaks one pixel apart. Without
    # collapsing them, `count` would report hundreds of "matches" for one
    # button and a caller counting them would conclude the image is ambiguous
    # when it is the opposite.
    h, w = needle.shape[:2]
    # Visit candidates best-first so that the ``max_matches`` cut drops the
    # weakest, and so the first survivor is the global best. Iterating in
    # row-major order (what argwhere returns) would keep the top-left-most
    # match instead, which is not the same thing once two controls look alike.
    positions = np.argwhere(score_map >= threshold)
    order = sorted(
        positions,
        key=lambda p: -float(score_map[p[0], p[1]]),
    )
    peaks: list[dict[str, Any]] = []
    for py, px in order:
        py, px = int(py), int(px)
        if any(abs(px - p["x"]) < w // 2 and abs(py - p["y"]) < h // 2 for p in peaks):
            continue
        peaks.append({
            "x": px, "y": py, "w": int(w), "h": int(h),
            "score": round(float(score_map[py, px]), 4),
        })
        if len(peaks) >= max_matches:
            break

    top = peaks[0]
    return MatchResult(
        found=True,
        # The reported score belongs to the reported location. Taking the
        # global max here while x/y came from the best *deduplicated* peak
        # produced a result whose score and position described different
        # places — visible as a template matching itself at 0.9999 while its
        # own match list said 0.9578.
        score=top["score"],
        x=top["x"], y=top["y"],
        width=w, height=h,
        count=len(peaks),
        color_used=False,
        matches=peaks,
    )


def find_color(
    haystack_path: str,
    rgb: tuple[int, int, int],
    threshold: int = 30,
) -> MatchResult:
    """Find the largest blob within ``threshold`` of ``rgb``.

    A single-pixel comparison is useless on a real screen: antialiasing and
    scaling mean the "same" colour is a range. This converts to BGR for
    OpenCV, takes the euclidean distance per pixel and reports the centroid
    plus the blob's bounding box so a caller can click its middle.
    """
    import cv2
    import numpy as np

    try:
        hay = cv2.imread(haystack_path, cv2.IMREAD_COLOR)
    except Exception as e:  # noqa: BLE001
        return MatchResult(found=False, score=0.0, reason=str(e))
    if hay is None or hay.size == 0:
        return MatchResult(found=False, score=0.0, reason=f"无法读取图片: {haystack_path}")

    target = np.array([rgb[2], rgb[1], rgb[0]], dtype=np.int16)  # RGB → BGR
    distance = np.linalg.norm(hay.astype(np.int16) - target, axis=2)
    mask = (distance <= max(0, int(threshold))).astype(np.uint8) * 255

    # A few close pixels are usually an artefact; a real control is a region.
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)

    if count <= 1:
        return MatchResult(
            found=False, score=0.0,
            reason=f"未找到与 RGB{rgb} 相差 ≤{threshold} 的区域",
        )

    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = int(np.argmax(areas)) + 1
    area = int(areas[largest - 1])
    cx, cy = centroids[largest]
    x = int(stats[largest, cv2.CC_STAT_LEFT])
    y = int(stats[largest, cv2.CC_STAT_TOP])
    w = int(stats[largest, cv2.CC_STAT_WIDTH])
    h = int(stats[largest, cv2.CC_STAT_HEIGHT])
    return MatchResult(
        found=True,
        # Coverage relative to the whole image: a large blob is a stronger
        # signal than a lone pixel, and the number is what makes that visible.
        score=float(area) / float(hay.shape[0] * hay.shape[1]),
        x=int(cx), y=int(cy), width=w, height=h,
        count=count - 1,
        reason=f"最大色块 {area} 像素，中心 ({int(cx)},{int(cy)})",
        matches=[{"x": x, "y": y, "w": w, "h": h, "area": area, "score": float(area)}],
    )


def pixel_at(image_path: str, x: int, y: int) -> tuple[int, int, int]:
    """Read one pixel as RGB. Raises rather than returning a sentinel."""
    import cv2

    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片不存在: {image_path}")
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError(f"无法读取图片: {image_path}")
    height, width = image.shape[:2]
    if not (0 <= x < width and 0 <= y < height):
        raise IndexError(f"坐标 ({x},{y}) 超出图片范围 {width}x{height}")
    blue, green, red = (int(v) for v in image[y][x][:3])
    return red, green, blue
