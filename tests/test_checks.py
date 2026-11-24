"""检查模块反例测试：确保每条核心产品规则与二进制一致检查在破坏样本时都能可靠失败。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._c_m_a_p import cmap_format_14
from fontTools.ttLib.tables.ttProgram import Program
import uharfbuzz as hb

from sarasa_pd.checks.binary import (
    FT_FAST_PPEMS,
    check_hinted_unhinted_parity,
    scan_freetype_pedantic,
)
from sarasa_pd.checks.rules import (
    CheckResult,
    _check_cmap_and_uvs,
    _check_empty_gsub_features,
    _check_inter_kerning_and_advances,
    _check_names_os2_and_raster,
)
from sarasa_pd.spec import load_spec
from sarasa_pd.vf.layout import restore_upstream_empty_gsub_features
from sarasa_pd.vf.layout_utils import Feature, install
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

    def test_a9_kerning_and_unsorted_pairset_counterexamples(self) -> None:
        spec = load_spec()

        def make_kern_font(kern_val: int, unsorted: bool = False) -> Path:
            fb = FontBuilder(1000, isTTF=True)
            fb.setupGlyphOrder([".notdef", "uni02BC", "rho", "alpha"])
            fb.setupCharacterMap({0x02BC: "uni02BC", 0x03C1: "rho", 0x03B1: "alpha"})
            fb.setupGlyf({g: _rect(40, 0, 260, 700) for g in (".notdef", "uni02BC", "rho", "alpha")})
            fb.setupHorizontalMetrics({".notdef": (600, 40), "uni02BC": (299, 40), "rho": (612, 40), "alpha": (600, 40)})
            fb.setupHorizontalHeader(ascent=880, descent=-120)
            fb.setupNameTable({"familyName": "KernTest", "styleName": "Regular"})
            fb.setupOS2()
            fb.setupPost()
            font = fb.font
            font["post"].formatType = 3.0

            gpos = newTable("GPOS")
            tbl = ot.GPOS()
            tbl.Version = 0x00010000
            sub = ot.PairPos()
            sub.Format = 1
            sub.ValueFormat1 = 0x0004
            sub.ValueFormat2 = 0
            sub.Coverage = ot.Coverage()
            sub.Coverage.glyphs = ["uni02BC"]
            r_rho = ot.PairValueRecord()
            r_rho.SecondGlyph = "rho"
            r_rho.Value1 = ot.ValueRecord()
            r_rho.Value1.XAdvance = kern_val
            r_alpha = ot.PairValueRecord()
            r_alpha.SecondGlyph = "alpha"
            r_alpha.Value1 = ot.ValueRecord()
            r_alpha.Value1.XAdvance = -10
            ps = ot.PairSet()
            # alpha GID=3 > rho GID=2；unsorted=True 时故意倒序
            ps.PairValueRecord = [r_alpha, r_rho] if unsorted else [r_rho, r_alpha]
            ps.PairValueCount = 2
            sub.PairSet = [ps]
            sub.PairSetCount = 1
            lk = ot.Lookup()
            lk.LookupType = 2
            lk.LookupFlag = 0
            lk.SubTable = [sub]
            lk.SubTableCount = 1
            tbl.LookupList = ot.LookupList()
            tbl.LookupList.Lookup = [lk]
            tbl.LookupList.LookupCount = 1
            install(tbl, {("DFLT", "dflt"): {"kern": Feature((0,))}, ("grek", "dflt"): {"kern": Feature((0,))}})
            gpos.table = tbl
            font["GPOS"] = gpos
            out = tmp / f"kern_{kern_val}_{int(unsorted)}.ttf"
            font.save(out)
            return out

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            # 1) -62 且有序 -> 通过
            p_ok = make_kern_font(-62, unsorted=False)
            with TTFont(p_ok) as f_ok:
                r_ok = CheckResult(ok=True)
                _check_inter_kerning_and_advances(f_ok, hb.Font(hb.Face(p_ok.read_bytes())), r_ok, spec, is_vf=False)
                self.assertTrue(r_ok.ok, r_ok.errors)
                self.assertEqual(r_ok.metrics["a9_mod_apostrophe_rho_kern"], -62)

            # 2) 反例：-21（A9 退化）-> 必须失败
            p_bad = make_kern_font(-21, unsorted=False)
            with TTFont(p_bad) as f_bad:
                r_bad = CheckResult(ok=True)
                _check_inter_kerning_and_advances(f_bad, hb.Font(hb.Face(p_bad.read_bytes())), r_bad, spec, is_vf=False)
                self.assertFalse(r_bad.ok)
                self.assertTrue(any("A9 回归" in e for e in r_bad.errors))

            # 3) 反例：PairSet 未排序 -> 必须失败
            p_uns = make_kern_font(-62, unsorted=True)
            with TTFont(p_uns) as f_uns:
                r_uns = CheckResult(ok=True)
                _check_inter_kerning_and_advances(f_uns, hb.Font(hb.Face(p_uns.read_bytes())), r_uns, spec, is_vf=False)
                self.assertFalse(r_uns.ok)
                self.assertTrue(any("PairSet" in e for e in r_uns.errors))

    def test_empty_gsub_features_check_upright_and_italic(self) -> None:
        spec = load_spec()
        font = _make_static()
        gsub = newTable("GSUB")
        gsub.table = ot.GSUB()
        gsub.table.Version = 0x00010000
        gsub.table.LookupList = ot.LookupList()
        gsub.table.LookupList.Lookup = []
        gsub.table.LookupList.LookupCount = 0
        install(gsub.table, {("DFLT", "dflt"): {"ccmp": Feature(())}, ("latn", "dflt"): {"ccmp": Feature(())}})
        font["GSUB"] = gsub

        # 未补空特性前必须失败
        r_miss = CheckResult(ok=True)
        _check_empty_gsub_features(font, r_miss, spec, italic=False)
        self.assertFalse(r_miss.ok)

        # 斜体补齐（20 个，不含 cv11）-> 斜体检查通过、正体检查报缺 cv11
        restore_upstream_empty_gsub_features(font, spec, italic=True)
        r_it = CheckResult(ok=True)
        _check_empty_gsub_features(font, r_it, spec, italic=True)
        self.assertTrue(r_it.ok, r_it.errors)
        r_up_fail = CheckResult(ok=True)
        _check_empty_gsub_features(font, r_up_fail, spec, italic=False)
        self.assertFalse(r_up_fail.ok)
        self.assertTrue(any("cv11" in e for e in r_up_fail.errors))

        # 正体补齐（含 cv11）-> 正体检查通过
        restore_upstream_empty_gsub_features(font, spec, italic=False)
        r_up = CheckResult(ok=True)
        _check_empty_gsub_features(font, r_up, spec, italic=False)
        self.assertTrue(r_up.ok, r_up.errors)

    def test_ft_fast_ppems_and_scan_mode(self) -> None:
        self.assertEqual(len(FT_FAST_PPEMS), 39)
        self.assertEqual(FT_FAST_PPEMS[:12], tuple(range(9, 21)))
        self.assertEqual(FT_FAST_PPEMS[-1], 128)
        with tempfile.TemporaryDirectory() as raw_tmp:
            p = Path(raw_tmp) / "static.ttf"
            _make_static().save(p)
            res = scan_freetype_pedantic(p, mode="fast", workers=1)
            self.assertTrue(res.ok, res.errors)
            self.assertEqual(res.metrics["ppem_count"], 39)
            self.assertEqual(res.metrics["ft_mode"], "fast")
            with self.assertRaises(ValueError):
                scan_freetype_pedantic(p, mode="invalid", workers=1)


if __name__ == "__main__":
    unittest.main()
