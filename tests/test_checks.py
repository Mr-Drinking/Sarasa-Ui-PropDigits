"""检查模块反例测试：确保每条核心产品规则与二进制一致检查在破坏样本时都能可靠失败。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import cmap_format_14
from fontTools.ttLib.tables.ttProgram import Program

from sarasa_pd.checks.binary import check_hinted_unhinted_parity
from sarasa_pd.checks.rules import (
    CheckResult,
    _check_cmap_and_uvs,
    _check_names_os2_and_raster,
)
from sarasa_pd.spec import load_spec
from sarasa_pd.vf.names import normalize_nonhinting_raster_metadata, update_vf_names


def _rect(x0: int, y0: int, x1: int, y1: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()
    return pen.glyph()


def _make_static(with_uvs: bool = True, with_sip: bool = True) -> TTFont:
    glyphs = [".notdef", "A", "han", "han.alt"]
    cmap = {0x0041: "A", 0x5DC5: "han"}
    if with_sip:
        glyphs.append("sip")
        cmap[0x28A99] = "sip"
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf({g: _rect(40, 0, 540, 700) for g in glyphs})
    fb.setupHorizontalMetrics({g: (600, 40) for g in glyphs})
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font
    font["post"].formatType = 3.0
    if with_uvs:
        sub14 = cmap_format_14(14)
        sub14.platformID = 0
        sub14.platEncID = 5
        sub14.language = 0
        sub14.cmap = {}
        sub14.uvsDict = {0xE0100: [(0x5DC5, "han.alt")]}
        font["cmap"].tables.append(sub14)
    return font


class ChecksCounterexampleTests(unittest.TestCase):
    def test_a1_uvs_missing_and_dangling_rejected(self) -> None:
        # 缺少 cmap 14
        f_no14 = _make_static(with_uvs=False)
        res = CheckResult(ok=True)
        _check_cmap_and_uvs(f_no14, res, reference=None, subset_mode="smoke")
        self.assertFalse(res.ok)
        self.assertTrue(any("cmap format 14" in e for e in res.errors))

        # 悬空 UVS 目标字形
        f_bad = _make_static(with_uvs=True)
        sub14 = [t for t in f_bad["cmap"].tables if t.format == 14][0]
        sub14.uvsDict[0xE0100] = [(0x5DC5, "nonexistent_glyph")]
        res2 = CheckResult(ok=True)
        _check_cmap_and_uvs(f_bad, res2, reference=None, subset_mode="smoke")
        self.assertFalse(res2.ok)
        self.assertTrue(any("悬空" in e for e in res2.errors))

    def test_names_os2_bit57_and_raster_counterexamples(self) -> None:
        spec = load_spec()
        font = _make_static(with_uvs=True, with_sip=True)
        update_vf_names(font, "CL", False, spec)
        normalize_nonhinting_raster_metadata(font, spec)
        font["OS/2"].ulUnicodeRange2 |= 1 << 25

        # 正常静态应通过
        ok_res = CheckResult(ok=True)
        _check_names_os2_and_raster(font, ok_res, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertTrue(ok_res.ok, ok_res.errors)

        # 反例 1：清掉 OS/2 bit 57
        font["OS/2"].ulUnicodeRange2 &= ~(1 << 25)
        r1 = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r1, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r1.ok)
        self.assertTrue(any("bit 57" in e for e in r1.errors))
        font["OS/2"].ulUnicodeRange2 |= 1 << 25

        # 反例 2：CL 版权被非 CL 检查时报错（非 CL 不得含尚古版权）
        r2 = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r2, spec, region="SC", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r2.ok)
        self.assertTrue(any("尚古" in e for e in r2.errors))

        # 反例 3：unhinted 意外含字形字节码
        font["glyf"]["A"].program = Program()
        font["glyf"]["A"].program.fromBytecode(b"\x00")
        r3 = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r3, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r3.ok)
        self.assertTrue(any("字形指令" in e for e in r3.errors))

    def test_hinted_unhinted_parity_pass_and_counterexample(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            p_un = tmp / "un.ttf"
            p_hi = tmp / "hi.ttf"
            p_bad = tmp / "bad.ttf"

            fu = _make_static()
            fu.save(p_un)

            # 仅加字形指令不改轮廓 -> 必须通过
            fh = TTFont(p_un)
            fh["glyf"]["A"].program = Program()
            fh["glyf"]["A"].program.fromBytecode(b"\x00")
            fh.save(p_hi)
            self.assertTrue(check_hinted_unhinted_parity(p_hi, p_un).ok)

            # 移动 1 个点 1 unit -> 必须失败
            fb = TTFont(p_hi)
            fb["glyf"]["A"].expand(fb["glyf"])
            x, y = fb["glyf"]["A"].coordinates[0]
            fb["glyf"]["A"].coordinates[0] = (x + 1, y)
            fb.save(p_bad)
            res = check_hinted_unhinted_parity(p_bad, p_un)
            self.assertFalse(res.ok)
            self.assertEqual(res.metrics["outline_diffs"], 1)


if __name__ == "__main__":
    unittest.main()
