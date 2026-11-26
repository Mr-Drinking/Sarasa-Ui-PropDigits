"""混合 Hinting 单元测试：增量缓存合并、.hint.gz 键序规范化、斜体点数核对与重映射、cvt 补零与 hinted 元数据。"""
from __future__ import annotations

import array
import json
from pathlib import Path
import tempfile
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

from sarasa_pd.spec import load_spec
from sarasa_pd.static.hint import (
    _load_gzip_json,
    _save_gzip_json_atomic,
    canonicalize_hint_data,
    finalize_hinted_ttf,
    remap_upright_hints_to_italic,
)


def _rect(x0: int, y0: int, x1: int, y1: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()
    return pen.glyph()


def _make_cjk_static(path: Path, *, extra_point: bool = False, extra_jamo_point: bool = False) -> Path:
    fb = FontBuilder(1000, isTTF=True)
    glyphs = [".notdef", "A", "uni4E00", "uniAC00", "uni1100"]
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap({0x0041: "A", 0x4E00: "uni4E00", 0xAC00: "uniAC00", 0x1100: "uni1100"})
    pen = TTGlyphPen(None)
    pen.moveTo((100, 400))
    pen.lineTo((900, 400))
    if extra_point:
        pen.lineTo((900, 450))
    pen.lineTo((900, 500))
    pen.lineTo((100, 500))
    pen.closePath()
    jpen = TTGlyphPen(None)
    jpen.moveTo((100, 200))
    jpen.lineTo((800, 200))
    if extra_jamo_point:
        jpen.lineTo((800, 500))
    jpen.lineTo((800, 700))
    jpen.lineTo((100, 700))
    jpen.closePath()
    fb.setupGlyf({
        ".notdef": _rect(50, 0, 500, 700),
        "A": _rect(50, 0, 550, 700),
        "uni4E00": pen.glyph(),
        "uniAC00": _rect(100, 100, 900, 800),
        "uni1100": jpen.glyph(),
    })
    fb.setupHorizontalMetrics({g: (1000, 50) for g in glyphs})
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": "HintTest", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font
    cvt = newTable("cvt ")
    cvt.values = array.array("h", [10, 20, 30])
    font["cvt "] = cvt
    font.save(path)
    font.close()
    return path


class HintUtilsTests(unittest.TestCase):
    def test_canonicalize_hint_data_produces_identical_order(self) -> None:
        a = {
            "glyphs": {"uni4E00#10": {"h": 2}, ".gid2#2": {"h": 1}, "uniAC00#3": {"h": 3}},
            "glyphHintCacheKeys": {"uniAC00#3": "k3", "uni4E00#10": "k2", ".gid2#2": "k1"},
            "sharedHints": {"z": 1, "a": 2},
        }
        b = {
            "glyphs": {".gid2#2": {"h": 1}, "uniAC00#3": {"h": 3}, "uni4E00#10": {"h": 2}},
            "glyphHintCacheKeys": {".gid2#2": "k1", "uniAC00#3": "k3", "uni4E00#10": "k2"},
            "sharedHints": {"a": 2, "z": 1},
        }
        ca = canonicalize_hint_data(a)
        cb = canonicalize_hint_data(b)
        self.assertEqual(list(ca["glyphs"].keys()), [".gid2#2", "uniAC00#3", "uni4E00#10"])
        self.assertEqual(json.dumps(ca, separators=(",", ":")), json.dumps(cb, separators=(",", ":")))

    def test_remap_upright_hints_to_italic_pass_and_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            up = _make_cjk_static(tmp / "up.ttf", extra_point=False)
            it_ok = _make_cjk_static(tmp / "it_ok.ttf", extra_point=False)
            it_bad = _make_cjk_static(tmp / "it_bad.ttf", extra_point=True)
            it_bad_jamo = _make_cjk_static(tmp / "it_bad_jamo.ttf", extra_jamo_point=True)

            up_hgz = tmp / "up.hint.gz"
            _save_gzip_json_atomic(
                up_hgz,
                {
                    "glyphs": {"uni4E00#2": {"h": 1}, "uniAC00#3": {"h": 2}},
                    "glyphHintCacheKeys": {"uni4E00#2": "k1", "uniAC00#3": "k2"},
                },
            )
            it_hgz = tmp / "it.hint.gz"
            rep = remap_upright_hints_to_italic(up, up_hgz, it_ok, it_hgz)
            self.assertEqual(rep["italic_hints_mapped"], 2)
            self.assertEqual(rep["italic_hints_skipped"], 0)
            loaded = _load_gzip_json(it_hgz)
            self.assertEqual(list(loaded["glyphs"]), ["uni4E00#2", "uniAC00#3"])

            # 反例：正斜体 CJK 或韩文字母点数不一致时必须拒绝复用
            with self.assertRaisesRegex(RuntimeError, "轮廓点数或纵坐标不一致"):
                remap_upright_hints_to_italic(up, up_hgz, it_bad, tmp / "bad.hint.gz")
            with self.assertRaisesRegex(RuntimeError, "轮廓点数或纵坐标不一致"):
                remap_upright_hints_to_italic(up, up_hgz, it_bad_jamo, tmp / "bad_jamo.hint.gz")

    def test_remap_matches_shifted_unencoded_glyph_and_blocks_false_same_gid(self) -> None:
        import math
        tan_s = math.tan(math.radians(9.4))

        def _make_shifted(path: Path, *, italic: bool, dy_bad: int = 0) -> Path:
            fb = FontBuilder(1000, isTTF=True)
            glyphs = [".notdef", "A", "uni4E00", "vert_alt", "other_glyph"] if not italic else [
                ".notdef", "A", "uni4E00", "other_glyph", "vert_alt"
            ]
            fb.setupGlyphOrder(glyphs)
            fb.setupCharacterMap({0x0041: "A", 0x4E00: "uni4E00"})

            def maybe_shear(x0: int, y0: int, x1: int, y1: int, *, dy: int = 0):
                pen = TTGlyphPen(None)
                pts = [(x0, y0), (x1, y0), (x1, y1 + dy), (x0, y1)]
                if italic:
                    pts = [(round(x + (y - 1000 / 3) * tan_s), y) for x, y in pts]
                pen.moveTo(pts[0])
                for pt in pts[1:]:
                    pen.lineTo(pt)
                pen.closePath()
                return pen.glyph()

            fb.setupGlyf({
                ".notdef": _rect(50, 0, 500, 700),
                "A": _rect(50, 0, 550, 700),
                "uni4E00": maybe_shear(100, 400, 900, 500),
                "vert_alt": maybe_shear(420, 120, 580, 820, dy=dy_bad if italic else 0),
                "other_glyph": _rect(10, 50, 200, 250) if not italic else _rect(10, 15, 300, 315),
            })
            fb.setupHorizontalMetrics({g: (1000, 50) for g in glyphs})
            fb.setupHorizontalHeader(ascent=880, descent=-120)
            fb.setupNameTable({"familyName": "HintTest", "styleName": "Italic" if italic else "Regular"})
            fb.setupOS2()
            fb.setupPost()
            fb.font["post"].formatType = 3.0
            fb.font.save(path)
            fb.font.close()
            return path

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            up = _make_shifted(tmp / "up3.ttf", italic=False)
            it = _make_shifted(tmp / "it3.ttf", italic=True, dy_bad=0)
            it_dy1 = _make_shifted(tmp / "it3_dy1.ttf", italic=True, dy_bad=1)
            up_hgz = tmp / "up3.hint.gz"
            # post format 3 下 Chlorophytum（ot-builder）对编码字形用 uniXXXX#gid、未编码用 .gidN#N：
            # 未编码字形 vert_alt 在正体 GID=3，在斜体位移到 GID=4；
            # 而正体 GID=4（other_glyph）与斜体 GID=3（不同形状）绝不能被错配。
            _save_gzip_json_atomic(
                up_hgz,
                {
                    "glyphs": {"uni4E00#2": {"h": 1}, ".gid3#3": {"h": 2}, ".gid4#4": {"h": 3}},
                    "glyphHintCacheKeys": {"uni4E00#2": "k1", ".gid3#3": "k2", ".gid4#4": "k3"},
                },
            )
            it_hgz = tmp / "it3.hint.gz"
            # 默认严禁静默跳过无法匹配的条目
            with self.assertRaisesRegex(RuntimeError, "无法精确匹配"):
                remap_upright_hints_to_italic(up, up_hgz, it, it_hgz)
            rep = remap_upright_hints_to_italic(up, up_hgz, it, it_hgz, allow_skipped=True)
            self.assertEqual(rep["italic_hints_mapped"], 2)
            self.assertEqual(rep["italic_hints_skipped"], 1)
            loaded = _load_gzip_json(it_hgz)
            self.assertEqual(loaded["glyphs"], {"uni4E00#2": {"h": 1}, ".gid4#4": {"h": 2}})

            # 反例：严格零容差下，即便纵坐标只差 1 unit 也绝不静默匹配
            rep_bad = remap_upright_hints_to_italic(up, up_hgz, it_dy1, tmp / "it3_dy1.hint.gz", allow_skipped=True)
            self.assertEqual(rep_bad["italic_hints_mapped"], 1)
            self.assertEqual(rep_bad["italic_hints_skipped"], 2)

    def test_finalize_hinted_ttf_pads_cvt_and_sets_hinted_metadata(self) -> None:
        spec = load_spec()
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            src = _make_cjk_static(tmp / "src.ttf")
            dst = tmp / "dst.ttf"
            rep = finalize_hinted_ttf(
                src, dst, region="CL", weight=spec.weight("Heavy"), italic=False, spec=spec, cvt_min_len=8192
            )
            self.assertEqual(rep["cvt_before"], 3)
            self.assertEqual(rep["cvt_after"], 8192)
            with TTFont(dst) as f:
                self.assertEqual(len(f["cvt "].values), 8192)
                self.assertEqual(list(f["cvt "].values[:3]), [10, 20, 30])
                self.assertEqual(list(f["cvt "].values[3:10]), [0] * 7)
                self.assertEqual(float(f["post"].formatType), 3.0)
                self.assertEqual(int(f["head"].flags), 15)
                self.assertEqual(int(f["head"].lowestRecPPEM), 3)
                self.assertEqual(int(f["OS/2"].xAvgCharWidth), 684)
                ver = f["name"].getDebugName(5) or ""
                self.assertIn("hinted", ver)
                self.assertIn("ShangguSansTC-VF", ver)
                self.assertNotIn("shanggu-1.028/", ver)


if __name__ == "__main__":
    unittest.main()
