"""子集化保留 cmap format 14 (A1)、对照工具与 CLI 参数校验测试。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import cmap_format_14

from sarasa_pd.__main__ import _parse_csv, build_parser
from sarasa_pd.compare import compare_font_pair, render_markdown
from sarasa_pd.subset import smoke_codepoints, subset_font_preserving_uvs


def _make_rect_glyph(x0: int, y0: int, x1: int, y1: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x0, y0))
    pen.lineTo((x0, y1))
    pen.lineTo((x1, y1))
    pen.lineTo((x1, y0))
    pen.closePath()
    return pen.glyph()


def _build_synthetic_font(path: Path, *, with_uvs: bool = True, zero_width: int = 600) -> Path:
    fb = FontBuilder(1000, isTTF=True)
    glyphs = [".notdef", "zero", "quoteleft", "quoteleft.full", "uni4FAE", "uniFA30", "unused"]
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap({0x0030: "zero", 0x2018: "quoteleft", 0x4FAE: "uni4FAE"})
    fb.setupGlyf({
        ".notdef": _make_rect_glyph(50, 0, 500, 700),
        "zero": _make_rect_glyph(50, 0, zero_width - 50, 700),
        "quoteleft": _make_rect_glyph(40, 450, 220, 720),
        "quoteleft.full": _make_rect_glyph(600, 450, 900, 720),
        "uni4FAE": _make_rect_glyph(100, 0, 900, 800),
        "uniFA30": _make_rect_glyph(120, 10, 880, 790),
        "unused": _make_rect_glyph(0, 0, 100, 100),
    })
    fb.setupHorizontalMetrics({
        ".notdef": (600, 50),
        "zero": (zero_width, 50),
        "quoteleft": (280, 40),
        "quoteleft.full": (1000, 600),
        "uni4FAE": (1000, 100),
        "uniFA30": (1000, 120),
        "unused": (500, 0),
    })
    fb.setupHorizontalHeader(ascent=969, descent=-241)
    fb.setupNameTable({"familyName": "TestFont", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=968, sTypoDescender=-241, usWinAscent=968, usWinDescent=241)
    fb.setupPost()
    font = fb.font
    if with_uvs:
        sub14 = cmap_format_14(14)
        sub14.platformID = 0
        sub14.platEncID = 5
        sub14.language = 0
        sub14.cmap = {}
        sub14.uvsDict = {
            0xFE01: [(0x2018, "quoteleft.full")],
            0xFE00: [(0x4FAE, "uniFA30")],
        }
        font["cmap"].tables.append(sub14)
    font.save(path)
    font.close()
    return path


class SubsetCompareCliTests(unittest.TestCase):
    def test_subset_preserves_cmap_format14_and_uvs_glyphs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = _build_synthetic_font(Path(tmp) / "src.ttf", with_uvs=True)
            out = Path(tmp) / "sub.ttf"
            with TTFont(src) as font:
                subset_font_preserving_uvs(font, {0x0030, 0x2018, 0x4FAE})
                font.save(out)
            with TTFont(out) as sub:
                order = set(sub.getGlyphOrder())
                self.assertIn("quoteleft.full", order)
                self.assertIn("uniFA30", order)
                self.assertNotIn("unused", order)
                f14 = [t for t in sub["cmap"].tables if t.format == 14]
                self.assertEqual(len(f14), 1)
                self.assertIn((0x2018, "quoteleft.full"), f14[0].uvsDict[0xFE01])
                self.assertIn((0x4FAE, "uniFA30"), f14[0].uvsDict[0xFE00])

    def test_compare_detects_missing_uvs_and_advance_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cand = _build_synthetic_font(Path(tmp) / "cand.ttf", with_uvs=False, zero_width=630)
            ref = _build_synthetic_font(Path(tmp) / "ref.ttf", with_uvs=True, zero_width=648)
            res = compare_font_pair(cand, ref, label="synthetic")
            self.assertEqual(res["uvs_format14"]["cand_total"], 0)
            self.assertEqual(res["uvs_format14"]["ref_total"], 2)
            self.assertEqual(res["uvs_format14"]["only_ref_count"], 2)
            self.assertEqual(res["advances"]["digit"]["count"], 1)
            self.assertEqual(res["advances"]["digit"]["max_diff"], 18)
            md = render_markdown({"title": "test", "pair_count": 1, "elapsed_sec": 0.1, "results": [res]})
            self.assertIn("0/2", md)
            self.assertIn("U+0030", md)

    def test_smoke_codepoints_cover_critical_regressions(self) -> None:
        cps = smoke_codepoints()
        self.assertGreater(len(cps), 400)
        self.assertLess(len(cps), 1500)
        for ch in ("0", ":", "-", "<", "巅", "拼", "绿", "㪉", "돌", "뮋", "靇", "龍", "侮"):
            self.assertIn(ord(ch), cps)
        self.assertIn(0xFE01, cps)
        self.assertIn(0xE0100, cps)

    def test_cli_strict_parsing(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["compare", "--reg", "SC"])
        for bad in ("", " , ", "SC,", ",SC", "XX"):
            with self.assertRaises(Exception, msg=bad):
                _parse_csv(bad, ("CL", "SC"), flag="--regions")
        self.assertEqual(_parse_csv("sc,cl,SC", ("CL", "SC"), flag="--regions"), ["SC", "CL"])


if __name__ == "__main__":
    unittest.main()
