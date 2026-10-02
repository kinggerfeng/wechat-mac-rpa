"""Tests for element picking.

The three strategies — OCR anchor, image template, fixed rectangle — exist
because a rectangle alone stops working the moment the window moves. Each is
tested on a synthetic image so the choice is observable rather than dependent on
whatever happens to be on screen.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.flow import picker  # noqa: E402
from src.flow.schema import NodeError  # noqa: E402

pytest.importorskip("PIL")


@pytest.fixture()
def shot(tmp_path):
    """A small image with a block of colour in the middle and a border."""
    from PIL import Image, ImageDraw

    path = tmp_path / "shot.png"
    img = Image.new("RGB", (400, 300), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    draw.rectangle([100, 100, 200, 150], fill=(40, 90, 160))
    draw.rectangle([10, 10, 390, 290], outline=(200, 200, 200))
    img.save(path)
    return str(path)


def _no_ocr(monkeypatch):
    """Force the OCR path to find nothing, so the template/rect paths are reachable."""
    monkeypatch.setattr(picker, "_text_in_rect", lambda *a, **k: ("", 0.0))


def test_ocr_text_in_rect_becomes_the_anchor(monkeypatch, tmp_path, shot):
    seen = {}

    def fake(source, x, y, w, h, min_conf):
        seen.update(source=str(source), rect=(x, y, w, h), conf=min_conf)
        return "搜索", 0.93

    monkeypatch.setattr(picker, "_text_in_rect", fake)
    monkeypatch.setattr(picker, "TEMPLATE_DIR", tmp_path / "tpl")
    monkeypatch.setattr(
        "src.flow.elements.capture_element",
        lambda **kw: {"id": "e1", "name": kw["name"], "kind": kw["kind"],
                      "ocr_text": kw["ocr_text"], "meta": kw["meta"]},
    )

    element = picker.pick_from_rect("搜索框", shot, {"x": 10, "y": 20, "width": 30, "height": 40})

    assert element["kind"] == "ocr"
    assert element["ocr_text"] == "搜索"
    assert element["meta"]["strategy"] == "ocr_anchor"
    assert element["meta"]["ocr_confidence"] == 0.93
    assert seen["rect"] == (10, 20, 30, 40)


def test_a_rectangle_with_no_text_falls_back_to_a_template(monkeypatch, tmp_path, shot):
    _no_ocr(monkeypatch)
    monkeypatch.setattr(picker, "TEMPLATE_DIR", tmp_path / "tpl")
    monkeypatch.setattr(
        "src.flow.elements.capture_element",
        lambda **kw: {"id": "e2", "kind": kw["kind"], "image_path": kw["image_path"],
                      "meta": kw["meta"]},
    )

    element = picker.pick_from_rect("图标按钮", shot, {"x": 100, "y": 100, "width": 60, "height": 30})

    assert element["kind"] == "image"
    assert element["meta"]["strategy"] == "image_template"
    template = Path(element["image_path"])
    assert template.is_file(), "template PNG was not written"
    assert template.parent == tmp_path / "tpl"


def test_with_neither_text_nor_template_it_says_it_is_a_plain_rect(monkeypatch, shot):
    _no_ocr(monkeypatch)
    monkeypatch.setattr(picker, "_save_template", lambda *a, **k: "")
    monkeypatch.setattr(
        "src.flow.elements.capture_element",
        lambda **kw: {"id": "e3", "kind": kw["kind"], "meta": kw["meta"]},
    )

    element = picker.pick_from_rect("未知", shot, {"x": 10, "y": 10, "width": 50, "height": 50})
    assert element["kind"] == "rect"
    assert element["meta"]["strategy"] == "fixed_rect"


def test_a_stray_drag_is_rejected_rather_than_stored(monkeypatch, shot):
    with pytest.raises(NodeError, match="选区太小"):
        picker.pick_from_rect("误拖", shot, {"x": 10, "y": 10, "width": 1, "height": 1})


def test_a_missing_screenshot_is_reported_with_its_path():
    with pytest.raises(NodeError, match="截图不存在"):
        picker.pick_from_rect("x", "/nope/shot.png", {"x": 0, "y": 0, "width": 10, "height": 10})


def test_ocr_picking_the_centre_not_a_straddling_word(monkeypatch, tmp_path):
    """A word that only crosses the edge of the selection is not the anchor."""
    class _Centre:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class _Item:
        def __init__(self, text, cx, cy, conf):
            self.text, self.center, self.confidence = text, _Centre(cx, cy), conf

    monkeypatch.setattr(
        picker, "_ocr_engine",
        lambda: type("E", (), {"recognize": staticmethod(
            lambda _p: [
                _Item("在里面", 150, 120, 0.9),   # centre inside
                _Item("只是路过", 195, 120, 0.9),  # text overlaps, centre outside
            ]
        )})(),
    )
    img = tmp_path / "s.png"
    from PIL import Image

    Image.new("RGB", (300, 200), "white").save(img)

    text, conf = picker._text_in_rect(img, 100, 100, 100, 50, 0.5)
    assert text == "在里面"
    assert conf == 0.9


def test_low_confidence_text_is_ignored(monkeypatch, tmp_path):
    class _Centre:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class _Item:
        def __init__(self, text, cx, cy, conf):
            self.text, self.center, self.confidence = text, _Centre(cx, cy), conf

    monkeypatch.setattr(
        picker, "_ocr_engine",
        lambda: type("E", (), {"recognize": staticmethod(
            lambda _p: [_Item("模糊", 150, 120, 0.2)]
        )})(),
    )
    img = tmp_path / "s.png"
    from PIL import Image

    Image.new("RGB", (300, 200), "white").save(img)
    assert picker._text_in_rect(img, 100, 100, 100, 50, 0.5) == ("", 0.0)


def test_ocr_failure_degrades_instead_of_raising(monkeypatch, tmp_path):
    """OCR is a bonus; a broken engine must not stop someone storing an element."""
    def boom():
        raise RuntimeError("Vision framework unavailable")

    monkeypatch.setattr(picker, "_ocr_engine", boom)
    img = tmp_path / "s.png"
    from PIL import Image

    Image.new("RGB", (300, 200), "white").save(img)
    assert picker._text_in_rect(img, 0, 0, 10, 10, 0.5) == ("", 0.0)


def test_template_names_are_sanitised(monkeypatch, tmp_path, shot):
    """An element named 搜索/框 must not try to write a nested path."""
    _no_ocr(monkeypatch)
    monkeypatch.setattr(picker, "TEMPLATE_DIR", tmp_path / "tpl")
    monkeypatch.setattr(
        "src.flow.elements.capture_element",
        lambda **kw: {"id": "e4", "image_path": kw["image_path"], "meta": kw["meta"]},
    )
    element = picker.pick_from_rect("搜索/框:1", shot, {"x": 100, "y": 100, "width": 20, "height": 20})
    path = Path(element["image_path"])
    assert path.parent == tmp_path / "tpl"
    assert "/" not in path.name
