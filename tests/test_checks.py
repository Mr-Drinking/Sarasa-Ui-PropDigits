"""检查模块反例测试：确保每条核心产品规则与二进制一致检查在破坏样本时都能可靠失败。"""
from __future__ import annotations

from dataclasses import replace
import array
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
    HINT_DISP_PPEMS,
    check_hint_displacement,
    check_hinted_unhinted_parity,
    check_unhinted_raster_parity,
    check_vf_static_parity,
    scan_freetype_pedantic,
)
from sarasa_pd.checks.rules import (
    CheckResult,
    _check_cmap_and_uvs,
    _check_empty_gsub_features,
    _check_inter_kerning_and_advances,
    _check_names_os2_and_raster,
    _check_table_framework_parity,
)
from sarasa_pd.spec import load_spec
from sarasa_pd.static.instance import rebuild_static_stat, update_static_names, update_static_os2_and_post
from sarasa_pd.vf.fixes import fix_os2_unicode_range_bit57, recalc_os2_max_context
from sarasa_pd.vf.layout import restore_upstream_empty_gsub_features
from sarasa_pd.vf.layout_utils import Feature, install
from sarasa_pd.vf.metrics import recalculate_head_bounds_and_lsb
from sarasa_pd.vf.names import apply_deterministic_head, normalize_nonhinting_raster_metadata, sync_sarasa_os2_fields


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
    def test_uvs_missing_dangling_extra_and_missing_reference_rejected(self) -> None:
        # 缺少 cmap 14
        f_no14 = _make_static(with_uvs=False)
        res = CheckResult(ok=True)
        _check_cmap_and_uvs(f_no14, res, reference=None, subset_mode="smoke", require_reference=False)
        self.assertFalse(res.ok)
        self.assertTrue(any("cmap format 14" in e for e in res.errors))

        # 悬空 UVS 目标字形
        f_bad = _make_static(with_uvs=True)
        sub14 = [t for t in f_bad["cmap"].tables if t.format == 14][0]
        sub14.uvsDict[0xE0100] = [(0x5DC5, "nonexistent_glyph")]
        res2 = CheckResult(ok=True)
        _check_cmap_and_uvs(f_bad, res2, reference=None, subset_mode="smoke", require_reference=False)
        self.assertFalse(res2.ok)
        self.assertTrue(any("悬空" in e for e in res2.errors))

        # 反例：full 模式下缺上游参照绝不得静默放过
        f_ok = _make_static(with_uvs=True)
        res3 = CheckResult(ok=True)
        _check_cmap_and_uvs(f_ok, res3, reference=None, subset_mode="full", require_reference=True)
        self.assertFalse(res3.ok)
        self.assertTrue(any("缺少上游" in e for e in res3.errors))

        # 反例：比上游多出 UVS 或默认/非默认状态不一致必须失败
        f_ref = _make_static(with_uvs=True)
        f_extra = _make_static(with_uvs=True)
        f_extra["cmap"].tables[-1].uvsDict[0xFE00] = [(0x0041, "A")]
        res4 = CheckResult(ok=True)
        _check_cmap_and_uvs(f_extra, res4, reference=f_ref, subset_mode="full", require_reference=True)
        self.assertFalse(res4.ok)
        self.assertTrue(any("多出" in e for e in res4.errors))

        f_kind = _make_static(with_uvs=True)
        f_kind["cmap"].tables[-1].uvsDict[0xE0100] = [(0x5DC5, None)]
        res5 = CheckResult(ok=True)
        _check_cmap_and_uvs(f_kind, res5, reference=f_ref, subset_mode="full", require_reference=True)
        self.assertFalse(res5.ok)
        self.assertTrue(any("默认/非默认" in e for e in res5.errors))

    def test_names_os2_bit57_bounds_and_raster_counterexamples(self) -> None:
        spec = load_spec()
        font = _make_static(with_uvs=True, with_sip=True)
        update_static_os2_and_post(font, "CL", spec.weight("Regular"), False, spec)
        update_static_names(font, "CL", spec.weight("Regular"), False, hinted=False, spec=spec)
        recalculate_head_bounds_and_lsb(font)
        recalc_os2_max_context(font)
        normalize_nonhinting_raster_metadata(font, spec)
        apply_deterministic_head(font, spec)
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

        # 反例 3：OS/2.usMaxContext、ulCodePageRange1、head.yMax、lsb!=xMin 必须全部拦下
        font["OS/2"].usMaxContext = 99
        r_ctx = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r_ctx, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r_ctx.ok)
        self.assertTrue(any("usMaxContext" in e for e in r_ctx.errors))
        recalc_os2_max_context(font)

        orig_cp1 = font["OS/2"].ulCodePageRange1
        font["OS/2"].ulCodePageRange1 = 0x12345678
        r_cp1 = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r_cp1, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r_cp1.ok)
        self.assertTrue(any("ulCodePageRange1" in e for e in r_cp1.errors))
        font["OS/2"].ulCodePageRange1 = orig_cp1

        orig_ymax = font["head"].yMax
        font["head"].yMax = 1808
        r_bbox = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r_bbox, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r_bbox.ok)
        self.assertTrue(any("head 包围盒" in e for e in r_bbox.errors))
        font["head"].yMax = orig_ymax

        adv_a, lsb_a = font["hmtx"]["A"]
        font["hmtx"]["A"] = (adv_a, lsb_a + 10)
        r_lsb = CheckResult(ok=True)
        _check_names_os2_and_raster(font, r_lsb, spec, region="CL", italic=False, is_vf=False, hinted=False)
        self.assertFalse(r_lsb.ok)
        self.assertTrue(any("lsb != xMin" in e for e in r_lsb.errors))
        font["hmtx"]["A"] = (adv_a, lsb_a)

        # 反例 4：unhinted 意外含字形字节码
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

    def test_ss03_rho_kerning_missing_inter_ref_and_unsorted_pairset_counterexamples(self) -> None:
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
            # 1) -62 且有序（显式关闭 Inter 参照）-> 通过；默认要求 Inter 参照时缺参照必须失败
            p_ok = make_kern_font(-62, unsorted=False)
            with TTFont(p_ok) as f_ok:
                r_ok = CheckResult(ok=True)
                _check_inter_kerning_and_advances(
                    f_ok, hb.Font(hb.Face(p_ok.read_bytes())), r_ok, spec, is_vf=False, require_inter_ref=False
                )
                self.assertTrue(r_ok.ok, r_ok.errors)
                self.assertEqual(r_ok.metrics["mod_apostrophe_rho_kern"], -62)

                r_noref = CheckResult(ok=True)
                _check_inter_kerning_and_advances(
                    f_ok, hb.Font(hb.Face(p_ok.read_bytes())), r_noref, spec, is_vf=False, require_inter_ref=True
                )
                self.assertFalse(r_noref.ok)
                self.assertTrue(any("缺少 Inter VF 参照" in e for e in r_noref.errors))

            # 2) 反例：-21（ʼρ 退化）-> 必须失败
            p_bad = make_kern_font(-21, unsorted=False)
            with TTFont(p_bad) as f_bad:
                r_bad = CheckResult(ok=True)
                _check_inter_kerning_and_advances(
                    f_bad, hb.Font(hb.Face(p_bad.read_bytes())), r_bad, spec, is_vf=False, require_inter_ref=False
                )
                self.assertFalse(r_bad.ok)
                self.assertTrue(any("ʼρ 字偶距退化" in e for e in r_bad.errors))

            # 3) 反例：PairSet 未排序 -> 必须失败
            p_uns = make_kern_font(-62, unsorted=True)
            with TTFont(p_uns) as f_uns:
                r_uns = CheckResult(ok=True)
                _check_inter_kerning_and_advances(
                    f_uns, hb.Font(hb.Face(p_uns.read_bytes())), r_uns, spec, is_vf=False, require_inter_ref=False
                )
                self.assertFalse(r_uns.ok)
                self.assertTrue(any("PairSet" in e for e in r_uns.errors))

    def test_vf_static_parity_rejects_extent_and_raster_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            p_a = tmp / "a.ttf"
            p_b = tmp / "b.ttf"
            p_c = tmp / "c.ttf"
            fa = _make_static()
            fa.save(p_a)
            self.assertTrue(check_vf_static_parity(p_a, p_a, 400, workers=1).ok)

            # 反例 1：步进不变但将 A 轮廓高度压低 80 unit -> 外框粗检与无 hint 光栅比对均必须报错
            fb = _make_static()
            fb["glyf"]["A"] = _rect(40, 0, 540, 620)
            fb.save(p_b)
            res = check_vf_static_parity(p_a, p_b, 400, workers=1)
            self.assertFalse(res.ok)
            self.assertEqual(res.metrics["max_advance_diff"], 0)
            self.assertEqual(res.metrics["max_extent_diff"], 80)
            self.assertTrue(any("墨迹外框差" in e for e in res.errors))
            self.assertTrue(any("光栅外观" in e for e in res.errors))

            # 反例 2：外框不变但中间挖空（改变内部像素外观）-> 光栅比对必须拦下
            fc = _make_static()
            pen = TTGlyphPen(None)
            for pts in (
                [(40, 0), (540, 0), (540, 700), (40, 700)],
                [(120, 120), (120, 580), (460, 580), (460, 120)],
            ):
                pen.moveTo(pts[0])
                for pt in pts[1:]:
                    pen.lineTo(pt)
                pen.closePath()
            fc["glyf"]["A"] = pen.glyph()
            fc.save(p_c)
            res_r = check_unhinted_raster_parity(p_a, p_c, 400, workers=1)
            self.assertFalse(res_r.ok)
            self.assertTrue(any("光栅外观" in e for e in res_r.errors))

    def test_hint_displacement_gate_pass_and_counterexample(self) -> None:
        self.assertEqual(HINT_DISP_PPEMS, tuple(range(12, 49)))
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            p_ref = tmp / "ref.ttf"
            p_ok = tmp / "ok.ttf"
            p_bad = tmp / "bad.ttf"

            f_ref = _make_static(with_uvs=False, with_sip=False)
            f_ref.save(p_ref)
            f_ref.save(p_ok)

            # 正常（无异常竖向位移）必须通过
            r_ok = check_hint_displacement(p_ok, p_ref, ppems=(16, 20, 28, 32), workers=1)
            self.assertTrue(r_ok.ok, r_ok.errors)
            self.assertEqual(r_ok.metrics["hint_disp_cjk_count"], 1)

            # 反例：给汉字 U+5DC5 注入 MIAP 指令将点 0 强拉至 cvt[0]=300（28 px 偏移 ~8.4 px）-> 必须报错
            f_bad = TTFont(p_ref)
            cvt = newTable("cvt ")
            cvt.values = array.array("h", [300])
            f_bad["cvt "] = cvt
            han_gn = f_bad.getBestCmap()[0x5DC5]
            f_bad["glyf"][han_gn].expand(f_bad["glyf"])
            prog = Program()
            # SVTCA[0] (Y轴); PUSHB[1] 0 0 (point=0, cvt=0); MIAP[0] (0x3E)
            prog.fromBytecode(bytes([0x00, 0xB1, 0x00, 0x00, 0x3E]))
            f_bad["glyf"][han_gn].program = prog
            f_bad["maxp"].maxStackElements = max(int(f_bad["maxp"].maxStackElements), 16)
            f_bad["maxp"].maxSizeOfInstructions = max(int(f_bad["maxp"].maxSizeOfInstructions), 16)
            f_bad["maxp"].maxZones = 2
            f_bad["head"].flags |= 0x0008
            f_bad.save(p_bad)
            f_bad.close()

            r_bad = check_hint_displacement(p_bad, p_ref, ppems=(16, 20, 28, 32), workers=1)
            self.assertFalse(r_bad.ok)
            self.assertTrue(any("竖向位移明显超过参照" in e for e in r_bad.errors))
            self.assertGreaterEqual(r_bad.metrics["hint_disp_20_28_32"][28]["cand_max"], 8.0)

            # 反例 2：哨兵字「走」(U+8D70) 仅比上游多偏 >0.5 px（如 ~1.0 px，未超 2.0 px 统计阈值）也必须被哨兵门拦下
            p_sref = tmp / "sref.ttf"
            p_sbad = tmp / "sbad.ttf"
            fs = FontBuilder(1000, isTTF=True)
            fs.setupGlyphOrder([".notdef", "zou"])
            fs.setupCharacterMap({0x8D70: "zou"})
            fs.setupGlyf({".notdef": _rect(40, 0, 540, 700), "zou": _rect(100, 0, 900, 700)})
            fs.setupHorizontalMetrics({".notdef": (600, 40), "zou": (1000, 100)})
            fs.setupHorizontalHeader(ascent=880, descent=-120)
            fs.setupNameTable({"familyName": "SentTest", "styleName": "Regular"})
            fs.setupOS2()
            fs.setupPost()
            fs.font["post"].formatType = 3.0
            fs.font.save(p_sref)

            fs_bad = TTFont(p_sref)
            cvt_s = newTable("cvt ")
            cvt_s.values = array.array("h", [36])  # 28 px 下约 1.0 px 位移（>0.5 px 但 <2.0 px）
            fs_bad["cvt "] = cvt_s
            zou_gn = fs_bad.getBestCmap()[0x8D70]
            fs_bad["glyf"][zou_gn].expand(fs_bad["glyf"])
            prog_s = Program()
            prog_s.fromBytecode(bytes([0x00, 0xB1, 0x00, 0x00, 0x3E]))
            fs_bad["glyf"][zou_gn].program = prog_s
            fs_bad["maxp"].maxStackElements = 16
            fs_bad["maxp"].maxSizeOfInstructions = 16
            fs_bad["maxp"].maxZones = 2
            fs_bad["head"].flags |= 0x0008
            fs_bad.save(p_sbad)
            fs_bad.close()

            r_sent = check_hint_displacement(p_sbad, p_sref, ppems=(28,), workers=1)
            self.assertFalse(r_sent.ok)
            self.assertTrue(any("哨兵字" in e and "走@28px" in e for e in r_sent.errors))

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

    def test_table_framework_parity_pass_and_counterexamples(self) -> None:
        spec = load_spec()

        def _make_pair(*, region: str = "SC", weight_name: str = "Regular", italic: bool = False, hinted: bool = False, is_vf: bool = False):
            w = spec.weight(weight_name)
            ref_w = spec.weight(w.sarasa_ref_weight)
            r = spec.region(region)

            def _base_font() -> TTFont:
                f = _make_static(with_uvs=True, with_sip=True)
                fb = FontBuilder(1000, isTTF=True)
                fb.font = f
                fb.setupVerticalHeader(ascent=500, descent=-500)
                fb.setupVerticalMetrics({g: (2000 if g == "han" else 1000, 180) for g in f.getGlyphOrder()})
                f["vhea"].advanceHeightMax = 2000
                f["vhea"].minTopSideBearing = 180
                f["vhea"].minBottomSideBearing = 120
                f["vhea"].yMaxExtent = 880
                f["vhea"].caretSlopeRise = 0
                f["vhea"].caretSlopeRun = 1
                f["vhea"].caretOffset = 0
                f["hhea"].ascent = spec.vertical_metrics["hhea_ascent"]
                f["hhea"].descent = spec.vertical_metrics["hhea_descent"]
                f["hhea"].lineGap = spec.vertical_metrics["hhea_line_gap"]
                f["hhea"].advanceWidthMax = 600
                f["hhea"].minLeftSideBearing = 40
                f["hhea"].minRightSideBearing = 60
                f["hhea"].xMaxExtent = 540
                f["hhea"].caretSlopeRise = 1000
                f["hhea"].caretSlopeRun = 166 if italic else 0
                f["hhea"].caretOffset = 0
                o = f["OS/2"]
                o.version = 4
                o.usWidthClass = 5
                o.fsType = 0
                o.ySubscriptXSize, o.ySubscriptYSize, o.ySubscriptXOffset, o.ySubscriptYOffset = 649, 600, (-12 if italic else 0), 75
                o.ySuperscriptXSize, o.ySuperscriptYSize, o.ySuperscriptXOffset, o.ySuperscriptYOffset = 649, 600, (58 if italic else 0), 350
                o.yStrikeoutPosition = 327
                o.sFamilyClass = 0
                o.ulUnicodeRange1, o.ulUnicodeRange2, o.ulUnicodeRange3, o.ulUnicodeRange4 = (
                    0xF10002FF, 0x69DFFDFF, 0x0000001E, 0x00100000
                )
                o.sTypoAscender = spec.vertical_metrics["typo_ascent"]
                o.sTypoDescender = spec.vertical_metrics["typo_descent"]
                o.sTypoLineGap = spec.vertical_metrics["typo_line_gap"]
                o.usWinAscent = spec.vertical_metrics["win_ascent"]
                o.usWinDescent = spec.vertical_metrics["win_descent"]
                o.sxHeight, o.sCapHeight, o.usDefaultChar, o.usBreakChar = 545, 727, 0, 32
                o.usFirstCharIndex, o.usLastCharIndex = 0, 65535
                f["head"].xMin, f["head"].yMin, f["head"].xMax, f["head"].yMax = 40, 0, 540, 700
                return f

            ref = _base_font()
            update_static_os2_and_post(ref, region, ref_w, italic, spec)
            ref["OS/2"].achVendID = "????"
            ref["OS/2"].usMaxContext = 0
            ref["head"].flags = 15
            ref["head"].lowestRecPPEM = 3
            ref["name"].names = []
            for nid in (0, 1, 2, 3, 4, 5, 6, 16, 17):
                ref["name"].setName(f"ref-{nid}", nid, 3, 1, 0x0409)
            if region != "K":
                for nid in (1, 2, 3, 4, 16, 17):
                    ref["name"].setName(f"ref-loc-{nid}", nid, 3, 1, r.local_lang_id)

            cand = _base_font()
            sync_sarasa_os2_fields(cand, ref)
            fix_os2_unicode_range_bit57(cand)
            update_static_os2_and_post(cand, region, w, italic, spec)
            update_static_names(cand, region, w, italic, hinted=hinted, spec=spec)
            if is_vf:
                cand["name"].setName("SarasaUiVF", 25, 3, 1, 0x0409)
            rebuild_static_stat(cand, w, italic)
            recalc_os2_max_context(cand)
            apply_deterministic_head(cand, spec)
            cand["head"].flags = 15 if (hinted and not is_vf) else 3
            return cand, ref, w

        # 1) 正常样本（Regular 正体/斜体、hinted、Heavy、K 补齐韩语本地化名、VF vs Regular 静态）均必须通过
        for kwargs in (
            dict(region="SC", weight_name="Regular", italic=False, hinted=False),
            dict(region="SC", weight_name="Regular", italic=True, hinted=False),
            dict(region="SC", weight_name="Regular", italic=False, hinted=True),
            dict(region="SC", weight_name="Heavy", italic=True, hinted=False),
            dict(region="SC", weight_name="Regular", italic=True, hinted=False, is_vf=True),
            dict(region="K", weight_name="Regular", italic=False, hinted=False),
        ):
            cand, ref, w = _make_pair(**kwargs)
            res = CheckResult(ok=True)
            _check_table_framework_parity(
                cand,
                ref,
                res,
                spec,
                region=kwargs["region"],
                italic=kwargs["italic"],
                is_vf=kwargs.get("is_vf", False),
                hinted=kwargs["hinted"],
                weight_spec=w,
                mode="upstream",
            )
            self.assertTrue(res.ok, f"kwargs={kwargs}, errors={res.errors}")

        vf_cand, _, w_reg = _make_pair(weight_name="Regular", italic=True, hinted=False, is_vf=True)
        st_reg, _, _ = _make_pair(weight_name="Regular", italic=True, hinted=False, is_vf=False)
        res_vr = CheckResult(ok=True)
        _check_table_framework_parity(
            vf_cand, st_reg, res_vr, spec, region="SC", italic=True, is_vf=True, hinted=False, weight_spec=w_reg, mode="vf_vs_regular"
        )
        self.assertTrue(res_vr.ok, res_vr.errors)

        # 2) 反例测试：任意改动表级字段之一，表级框架对照检查都必须失败
        mutations = [
            ("head.flags(unhinted=15)", lambda f: setattr(f["head"], "flags", 15)),
            ("head.lowestRecPPEM", lambda f: setattr(f["head"], "lowestRecPPEM", 6)),
            ("head.macStyle", lambda f: setattr(f["head"], "macStyle", 1)),
            ("head.yMax", lambda f: setattr(f["head"], "yMax", 1808)),
            ("head.yMin", lambda f: setattr(f["head"], "yMin", -1048)),
            ("hhea.ascent", lambda f: setattr(f["hhea"], "ascent", 900)),
            ("hhea.caretSlopeRun", lambda f: setattr(f["hhea"], "caretSlopeRun", 0)),
            ("vhea.advanceHeightMax", lambda f: setattr(f["vhea"], "advanceHeightMax", 3000)),
            ("vhea.yMaxExtent", lambda f: setattr(f["vhea"], "yMaxExtent", 2928)),
            ("OS/2.ulUnicodeRange1", lambda f: setattr(f["OS/2"], "ulUnicodeRange1", 0x10000002)),
            ("OS/2.ulUnicodeRange2", lambda f: setattr(f["OS/2"], "ulUnicodeRange2", 0x2BDF3C00)),
            ("OS/2.ulUnicodeRange3", lambda f: setattr(f["OS/2"], "ulUnicodeRange3", 0x00000016)),
            ("OS/2.ulUnicodeRange4", lambda f: setattr(f["OS/2"], "ulUnicodeRange4", 0)),
            ("OS/2.ulCodePageRange1", lambda f: setattr(f["OS/2"], "ulCodePageRange1", 0x8002019F)),
            ("OS/2.xAvgCharWidth", lambda f: setattr(f["OS/2"], "xAvgCharWidth", 978)),
            ("OS/2.fsType", lambda f: setattr(f["OS/2"], "fsType", 4)),
            ("OS/2.fsSelection", lambda f: setattr(f["OS/2"], "fsSelection", 0x00C0)),
            ("OS/2.ySubscriptXOffset", lambda f: setattr(f["OS/2"], "ySubscriptXOffset", 0)),
            ("OS/2.ySuperscriptXOffset", lambda f: setattr(f["OS/2"], "ySuperscriptXOffset", 0)),
            ("OS/2.yStrikeoutPosition", lambda f: setattr(f["OS/2"], "yStrikeoutPosition", 300)),
            ("OS/2.panose.bWeight", lambda f: setattr(f["OS/2"].panose, "bWeight", 8)),
            ("post.italicAngle", lambda f: setattr(f["post"], "italicAngle", -10.0)),
            ("post.underlineThickness", lambda f: setattr(f["post"], "underlineThickness", 80)),
            ("name_keys_extra", lambda f: f["name"].setName("x", 12, 3, 1, 0x0409)),
            ("missing_STAT", lambda f: f.__delitem__("STAT")),
        ]
        for label, mutate in mutations:
            cand, ref, w = _make_pair(weight_name="Regular", italic=True, hinted=False)
            mutate(cand)
            r_bad = CheckResult(ok=True)
            _check_table_framework_parity(
                cand, ref, r_bad, spec, region="SC", italic=True, is_vf=False, hinted=False, weight_spec=w, mode="upstream"
            )
            self.assertFalse(r_bad.ok, f"反例 {label} 必须失败却通过了")

        # 3) 反例：hinted 清掉 bit 3（flags=7）也必须失败
        cand_hi, ref_hi, w_hi = _make_pair(weight_name="Regular", italic=False, hinted=True)
        cand_hi["head"].flags = 7
        r_hi = CheckResult(ok=True)
        _check_table_framework_parity(
            cand_hi, ref_hi, r_hi, spec, region="SC", italic=False, is_vf=False, hinted=True, weight_spec=w_hi, mode="upstream"
        )
        self.assertFalse(r_hi.ok)

        # 4) 反例：如果 spec.toml 差异清单遗漏了 nonhinting-head-flags，检查必须失败
        cand_u, ref_u, w_u = _make_pair(weight_name="Regular", italic=False, hinted=False)
        spec_missing_diff = replace(
            spec,
            differences=tuple(d for d in spec.differences if d.id != "nonhinting-head-flags"),
        )
        r_nodiff = CheckResult(ok=True)
        _check_table_framework_parity(
            cand_u, ref_u, r_nodiff, spec_missing_diff, region="SC", italic=False, is_vf=False, hinted=False, weight_spec=w_u, mode="upstream"
        )
        self.assertFalse(r_nodiff.ok)
        self.assertTrue(any("nonhinting-head-flags" in e for e in r_nodiff.errors))


if __name__ == "__main__":
    unittest.main()
