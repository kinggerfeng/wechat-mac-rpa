"""Image-template matching and colour location.

Every test builds its own image rather than shipping a fixture: a checked-in
PNG is opaque about what it contains, and the two bugs this file exists for
(a flat template matching the whole screen, a low-variance template scoring
*worse* the closer it got) are only visible from the construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

cv2 = pytest.importorskip("cv2")
numpy = pytest.importorskip("numpy")

from src.flow import vision_match


def _scene(tmp_path) -> str:
    """A light background with one bordered blue button and one green disc.

    Channels are written BGR because OpenCV treats a written array as BGR;
    getting that backwards produced a "passing" test in an earlier draft that
    asserted against colours nobody actually drew.
    """
    hay = numpy.full((200, 300, 3), 240, numpy.uint8)
    cv2.rectangle(hay, (100, 80), (140, 110), (220, 120, 20), -1)   # RGB(20,120,220)
    cv2.rectangle(hay, (98, 78), (142, 112), (40, 40, 40), 2)        # border
    cv2.circle(hay, (220, 150), 20, (60, 200, 30), -1)
    cv2.circle(hay, (220, 150), 18, (30, 220, 40), -1)
    path = str(tmp_path / "scene.png")
    cv2.imwrite(path, hay)
    return path


def _crop(src: str, dst: str, box) -> str:
    image = cv2.imread(src)
    x1, y1, x2, y2 = box
    cv2.imwrite(dst, numpy.ascontiguousarray(image[y1:y2, x1:x2]))
    return dst


class TestFindTemplate:
    def test_exact_crop_is_found(self, tmp_path):
        scene = _scene(tmp_path)
        needle = _crop(scene, str(tmp_path / "n.png"), (98, 78, 143, 113))
        result = vision_match.find_template(scene, needle)
        assert result.found is True
        assert result.score > 0.99
        # One pixel of slack for the off-by-one in a slice's end index.
        assert abs(result.x - 98) <= 1
        assert abs(result.y - 78) <= 1
        assert result.width == 45 and result.height == 35

    def test_flat_template_is_refused_as_ambiguous(self, tmp_path):
        """A solid swatch matches every pixel of that colour.

        Under SQDIFF a flat template against a flat background scored 1.000
        across the whole image, and the "best" offset was wherever the argmax
        happened to land. Returning that as a location would be a coin flip
        dressed up as a result.
        """
        scene = _scene(tmp_path)
        flat = str(tmp_path / "flat.png")
        cv2.imwrite(flat, numpy.full((20, 20), 138, numpy.uint8))
        result = vision_match.find_template(scene, flat)
        assert result.found is False
        assert result.count == -1
        assert "纯色" in result.reason
        assert result.x is None

    def test_absent_region_does_not_match(self, tmp_path):
        """The blank top-left corner is real content, just not the button."""
        scene = _scene(tmp_path)
        blank = _crop(scene, str(tmp_path / "b.png"), (0, 0, 20, 20))
        result = vision_match.find_template(scene, blank)
        assert result.found is False
        assert result.x is None

    def test_low_variance_template_scores_by_similarity_not_coefficient(self, tmp_path):
        """A near-flat but textured crop must score higher the closer it gets.

        This is why TM_CCOEFF_NORMED is not used: a near-flat window makes the
        normalized coefficient collapse, and a *perfect* match measured 0.069
        while a worse one could score higher. The comparison is against a
        genuinely different region — a shifted crop of the same blank
        background is just as uniform and also scores 1.0.
        """
        scene = _scene(tmp_path)
        exact = _crop(scene, str(tmp_path / "e.png"), (98, 78, 143, 113))
        elsewhere = _crop(scene, str(tmp_path / "o.png"), (150, 20, 195, 55))
        near = vision_match.find_template(scene, exact)
        far = vision_match.find_template(scene, elsewhere)
        assert near.score > far.score
        # And the reported score is the one at the reported location.
        assert near.score == near.matches[0]["score"]

    def test_threshold_is_honoured(self, tmp_path):
        scene = _scene(tmp_path)
        needle = _crop(scene, str(tmp_path / "n.png"), (98, 78, 143, 113))
        strict = vision_match.find_template(scene, needle, threshold=1.01)
        assert strict.found is False
        assert "阈值" in strict.reason

    def test_oversized_template_is_reported_not_crashed(self, tmp_path):
        scene = _scene(tmp_path)
        big = str(tmp_path / "big.png")
        cv2.imwrite(big, numpy.zeros((500, 500), numpy.uint8))
        result = vision_match.find_template(scene, big)
        assert result.found is False
        assert "比待搜索图还大" in result.reason

    def test_missing_files_return_a_reason(self, tmp_path):
        result = vision_match.find_template(
            str(tmp_path / "nope.png"), str(tmp_path / "also-nope.png"))
        assert result.found is False
        assert "不存在" in result.reason

    def test_corrupt_file_says_so(self, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not a png at all")
        result = vision_match.find_template(str(bad), str(bad))
        assert result.found is False
        assert "无法解码" in result.reason

    def test_result_is_serialisable(self, tmp_path):
        scene = _scene(tmp_path)
        needle = _crop(scene, str(tmp_path / "n.png"), (98, 78, 143, 113))
        payload = vision_match.find_template(scene, needle).to_dict()
        assert set(payload) >= {"found", "score", "x", "y", "count", "matches"}


class TestFindColor:
    def test_button_colour_is_located(self, tmp_path):
        scene = _scene(tmp_path)
        result = vision_match.find_color(scene, (20, 120, 220), threshold=40)
        assert result.found is True
        assert abs(result.x - 120) <= 2
        assert abs(result.y - 95) <= 2
        assert result.width > 0 and result.height > 0

    def test_absent_colour_is_reported(self, tmp_path):
        result = vision_match.find_color(_scene(tmp_path), (0, 0, 255), threshold=10)
        assert result.found is False
        assert "未找到" in result.reason

    def test_tolerance_widens_the_match(self, tmp_path):
        scene = _scene(tmp_path)
        near = vision_match.find_color(scene, (28, 126, 226), threshold=12)
        assert near.found is True

    def test_unreadable_image_is_reported(self, tmp_path):
        result = vision_match.find_color(str(tmp_path / "nope.png"), (1, 2, 3))
        assert result.found is False


class TestPixelAt:
    def test_reads_rgb_not_bgr(self, tmp_path):
        """The channel order is the whole point of this function."""
        assert vision_match.pixel_at(_scene(tmp_path), 120, 95) == (20, 120, 220)

    def test_out_of_bounds_raises(self, tmp_path):
        with pytest.raises(IndexError):
            vision_match.pixel_at(_scene(tmp_path), 999, 999)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            vision_match.pixel_at(str(tmp_path / "nope.png"), 0, 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestImageNodes:
    """The three pixel nodes, driven through the executor.

    The fallback to the run's last screenshot is the part worth pinning: it is
    what makes ``find_image`` usable without repeating the screenshot path on
    every node, and it is silent when it picks the wrong frame.
    """

    @staticmethod
    def _flow(node_type: str, params: dict, variables: dict | None = None) -> "Flow":
        from src.flow.schema import Flow

        return Flow(id="f_img", name="img", graph={
            "version": 1, "entry": "n1", "default_path": "auto",
            "variables": variables or {},
            "nodes": [{
                "id": "n1", "type": node_type, "name": node_type,
                "position": {"x": 0, "y": 0}, "params": params,
                "retry": {"max": 0, "delay": 0}, "timeout": None, "on_error": "fail",
                "outputs": ["found"], "disabled": False, "path": None, "target": None,
            }],
            "edges": [],
        })

    @staticmethod
    def _run(node_type: str, params: dict, variables: dict | None = None):
        from src.flow.executor import FlowExecutor

        return FlowExecutor().run(
            TestImageNodes._flow(node_type, params, variables), "run_img")

    def test_find_image_locates_a_bordered_button(self, tmp_path):
        scene = _scene(tmp_path)
        needle = _crop(scene, str(tmp_path / "n.png"), (98, 78, 143, 113))
        result = self._run("find_image", {"image": needle, "haystack": scene})
        assert result.status == "ok", result.error
        assert result.scope["n1"]["found"] is True
        assert abs(result.scope["n1"]["x"] - 98) <= 1

    def test_find_image_falls_back_to_the_run_screenshot(self, tmp_path):
        scene = _scene(tmp_path)
        needle = _crop(scene, str(tmp_path / "n.png"), (98, 78, 143, 113))
        result = self._run("find_image", {"image": needle}, {"screenshot_path": scene})
        assert result.status == "ok", result.error
        assert result.scope["n1"]["found"] is True

    def test_find_image_without_any_image_says_what_to_do(self, tmp_path):
        result = self._run("find_image", {"image": str(tmp_path / "n.png")})
        assert result.status == "error"
        assert "capture" in result.error

    def test_find_color_uses_the_last_screenshot(self, tmp_path):
        result = self._run("find_color", {"rgb": "20,120,220", "threshold": 40},
                           {"screenshot_path": _scene(tmp_path)})
        assert result.status == "ok", result.error
        assert result.scope["n1"]["found"] is True

    @pytest.mark.parametrize("bad", ["", "1,2", "1,2,3,4", "a,b,c", "300,0,0"])
    def test_bad_rgb_is_refused_with_a_reason(self, tmp_path, bad):
        result = self._run("find_color", {"rgb": bad, "haystack": _scene(tmp_path)})
        assert result.status == "error"

    def test_assert_pixel_accepts_a_matching_colour(self, tmp_path):
        result = self._run("assert_pixel",
                           {"x": 120, "y": 95, "rgb": "20,120,220", "haystack": _scene(tmp_path)})
        assert result.status == "ok", result.error
        out = result.scope["n1"]
        assert out["matched"] is True
        assert (out["r"], out["g"], out["b"]) == (20, 120, 220)

    def test_assert_pixel_rejects_a_different_colour(self, tmp_path):
        result = self._run("assert_pixel",
                           {"x": 120, "y": 95, "rgb": "255,0,0", "tolerance": 5,
                            "haystack": _scene(tmp_path)})
        out = result.scope["n1"]
        assert out["matched"] is False
        assert "超过容忍" in out["reason"]

    def test_assert_pixel_without_expectation_is_a_read(self, tmp_path):
        result = self._run("assert_pixel", {"x": 120, "y": 95, "haystack": _scene(tmp_path)})
        out = result.scope["n1"]
        assert out["matched"] is True
        assert "仅读取" in out["reason"]

    def test_assert_pixel_out_of_bounds_is_a_false_not_a_crash(self, tmp_path):
        """A caller branching on `matched` must not have to catch an error."""
        result = self._run("assert_pixel", {"x": 9999, "y": 9999,
                                            "haystack": _scene(tmp_path)})
        assert result.status == "ok", result.error
        assert result.scope["n1"]["matched"] is False
        assert "超出" in result.scope["n1"]["reason"]
