"""阶段 2 单元测试：VF 基底准备（base.py）与 Inter 烘焙/并入（inter.py）。"""
from __future__ import annotations

import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables._c_m_a_p import cmap_format_14
from fontTools.ttLib.tables._f_v_a_r import Axis

from sarasa_pd.spec import load_spec
from sarasa_pd.vf.base import (
    apply_public_weight_axis,
    assert_compatible_axis_space,
    bake_pwid_and_sanitize,
    normalize_hangul_widths,
    normalized_wght,
    shear_font,
    stretch_to_width,
)
from sarasa_pd.vf.inter import (
    LoadedInter,
    append_inter_glyphs,
    bake_inter_ui_recipe,
)


def _rect(x0: int, y0: int, x1: int, y1: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()
    return pen.glyph()


def _make_vf(
    glyphs: list[str],
    cmap: dict[int, str],
    widths: dict[str, int],
    *,
    axis_range: tuple[float, float, float] = (250.0, 400.0, 900.0),
    avar_seg: dict[float, float] | None = None,
    uvs: dict[int, list[tuple[int, str | None]]] | None = None,
) -> TTFont:
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf({g: _rect(0, 0, max(100, widths.get(g, 500) - 50), 700) for g in glyphs})
    fb.setupHorizontalMetrics({g: (widths.get(g, 500), 0) for g in glyphs})
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": "Syn", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font

    fvar = newTable("fvar")
    ax = Axis()
    ax.axisTag = "wght"
    ax.minValue, ax.defaultValue, ax.maxValue = axis_range
    ax.axisNameID = 256
    fvar.axes = [ax]
    fvar.instances = []
    font["fvar"] = fvar

    avar = newTable("avar")
    avar.segments = {"wght": dict(avar_seg or {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0})}
    font["avar"] = avar

    gvar = newTable("gvar")
    gvar.version = 1
    gvar.reserved = 0
    gvar.variations = {}
    for g in glyphs:
        npts = len(font["glyf"][g].coordinates) if font["glyf"][g].numberOfContours > 0 else 0
        gvar.variations[g] = [
            TupleVariation({"wght": (0.0, 1.0, 1.0)}, [(10, 0)] * npts + [(0, 0), (20, 0), (0, 0), (0, 0)])
        ]
    font["gvar"] = gvar

    if uvs:
        sub14 = cmap_format_14(14)
        sub14.platformID = 0
        sub14.platEncID = 5
        sub14.language = 0
        sub14.cmap = {}
        sub14.uvsDict = uvs
        font["cmap"].tables.append(sub14)
    return font


def _add_single_gsub(font: TTFont, tag_mappings: list[tuple[str, dict[str, str]]]) -> None:
    gsub = newTable("GSUB")
    tbl = ot.GSUB()
    tbl.Version = 0x00010000
    tbl.LookupList = ot.LookupList()
    tbl.LookupList.Lookup = []
    tbl.FeatureList = ot.FeatureList()
    tbl.FeatureList.FeatureRecord = []

    for tag, mapping in tag_mappings:
        lk = ot.Lookup()
        lk.LookupType = 1
        lk.LookupFlag = 0
        st = ot.SingleSubst()
        st.mapping = dict(mapping)
        lk.SubTable = [st]
        lk.SubTableCount = 1
        idx = len(tbl.LookupList.Lookup)
        tbl.LookupList.Lookup.append(lk)

        frec = ot.FeatureRecord()
        frec.FeatureTag = tag
        frec.Feature = ot.Feature()
        frec.Feature.FeatureParams = None
        frec.Feature.LookupListIndex = [idx]
        frec.Feature.LookupCount = 1
        tbl.FeatureList.FeatureRecord.append(frec)

    tbl.LookupList.LookupCount = len(tbl.LookupList.Lookup)
    tbl.FeatureList.FeatureCount = len(tbl.FeatureList.FeatureRecord)

    srec = ot.ScriptRecord()
    srec.ScriptTag = "DFLT"
    srec.Script = ot.Script()
    srec.Script.DefaultLangSys = ot.LangSys()
    srec.Script.DefaultLangSys.ReqFeatureIndex = 0xFFFF
    srec.Script.DefaultLangSys.FeatureIndex = list(range(len(tag_mappings)))
    srec.Script.DefaultLangSys.FeatureCount = len(tag_mappings)
    srec.Script.LangSysRecord = []
    srec.Script.LangSysCount = 0
    tbl.ScriptList = ot.ScriptList()
    tbl.ScriptList.ScriptRecord = [srec]
    tbl.ScriptList.ScriptCount = 1
    gsub.table = tbl
    font["GSUB"] = gsub


class VfBaseInterTests(unittest.TestCase):
    def test_public_weight_axis_and_compatibility_check(self) -> None:
        spec = load_spec()
        base = _make_vf([".notdef", "A"], {0x41: "A"}, {".notdef": 500, "A": 600})
        ov_ok = _make_vf([".notdef", "A"], {0x41: "A"}, {".notdef": 500, "A": 600})
        ov_bad = _make_vf([".notdef", "A"], {0x41: "A"}, {".notdef": 500, "A": 600}, axis_range=(200.0, 400.0, 900.0))

        assert_compatible_axis_space(base, ov_ok)
        with self.assertRaisesRegex(ValueError, "wght 轴不一致"):
            assert_compatible_axis_space(base, ov_bad)

        report = apply_public_weight_axis(base, spec)
        self.assertEqual(report["public_wght_axis"], [200.0, 400.0, 900.0])
        self.assertIn(350, report["public_avar_anchors"])
        self.assertEqual(normalized_wght(base, 200), -1.0)
        self.assertEqual(normalized_wght(base, 400), 0.0)
        self.assertEqual(normalized_wght(base, 900), 1.0)
        self.assertLess(-1.0, normalized_wght(base, 350))
        self.assertLess(normalized_wght(base, 350), 0.0)

    def test_pwid_sanitize_stretch_and_hangul_widths(self) -> None:
        spec = load_spec()
        font = _make_vf(
            [".notdef", "middot", "middot.pwid", "twoem", "han", "jamo0", "uvs_alt"],
            {0x00B7: "middot", 0x2E3A: "twoem", 0xAC00: "han", 0x1161: "jamo0"},
            {".notdef": 500, "middot": 1000, "middot.pwid": 400, "twoem": 1000, "han": 920, "jamo0": 0, "uvs_alt": 1000},
            uvs={0xFE00: [(0xAC00, "uvs_alt")]},
        )
        _add_single_gsub(font, [("pwid", {"middot": "middot.pwid"})])

        rep = bake_pwid_and_sanitize(font, spec)
        self.assertEqual(rep["pwid_baked"], 1)
        self.assertEqual(font.getBestCmap()[0x00B7], "middot.pwid")
        self.assertEqual(font["hmtx"].metrics["middot.pwid"][0], 500)
        self.assertEqual(font["gvar"].variations["middot.pwid"][0].coordinates[-3], (0, 0))

        stretch_to_width(font, "twoem", 2000)
        self.assertEqual(font["hmtx"].metrics["twoem"][0], 2000)
        self.assertEqual(font["gvar"].variations["twoem"][0].coordinates[-3], (0, 0))

        count = normalize_hangul_widths(font)
        self.assertGreaterEqual(count, 2)
        self.assertEqual(font["hmtx"].metrics["han"][0], 1000)
        self.assertEqual(font["gvar"].variations["han"][0].coordinates[-3], (0, 0))
        self.assertEqual(font["glyf"]["jamo0"].xMin, -40)

    def test_pwid_preserves_default_uvs_and_hangul_isolates_ffa0(self) -> None:
        spec = load_spec()
        font = _make_vf(
            [".notdef", "quoteleft", "quoteleft.pwid", "uni1160"],
            {0x2018: "quoteleft", 0x3164: "uni1160", 0xFFA0: "uni1160"},
            {".notdef": 500, "quoteleft": 1000, "quoteleft.pwid": 279, "uni1160": 920},
            uvs={0xFE01: [(0x2018, None)]},
        )
        _add_single_gsub(font, [("pwid", {"quoteleft": "quoteleft.pwid"})])

        bake_pwid_and_sanitize(font, spec)
        self.assertEqual(font.getBestCmap()[0x2018], "quoteleft.pwid")
        sub14 = [t for t in font["cmap"].tables if t.format == 14][0]
        # U+2018+U+FE01 必须固化为原 1000 宽 quoteleft，不随普通 cmap 变窄
        self.assertEqual(sub14.uvsDict[0xFE01], [(0x2018, "quoteleft")])

        normalize_hangul_widths(font)
        cmap = font.getBestCmap()
        self.assertEqual(font["hmtx"].metrics[cmap[0x3164]][0], 1000)
        self.assertEqual(font["hmtx"].metrics[cmap[0xFFA0]][0], 920)

    def test_interpunct_matches_upstream_sc_vs_jkcl_sharing(self) -> None:
        spec = load_spec()
        # SC/TC/HC 模式：0x00B7/0x2022/0x2027/0x30FB 共享 periodcentered，结果前三者 500、0x30FB 保持 1000
        sc = _make_vf(
            [".notdef", "periodcentered"],
            {0x00B7: "periodcentered", 0x2022: "periodcentered", 0x2027: "periodcentered", 0x30FB: "periodcentered"},
            {".notdef": 500, "periodcentered": 1000},
        )
        bake_pwid_and_sanitize(sc, spec)
        cm_sc = sc.getBestCmap()
        self.assertEqual(sc["hmtx"].metrics[cm_sc[0x00B7]][0], 500)
        self.assertEqual(sc["hmtx"].metrics[cm_sc[0x2022]][0], 500)
        self.assertEqual(sc["hmtx"].metrics[cm_sc[0x2027]][0], 500)
        self.assertEqual(sc["hmtx"].metrics[cm_sc[0x30FB]][0], 1000)

        # J/K/CL 模式：0x2022(bullet=340) 与 0x2027(uni2027=1000) 为独立字形，不得被误改成 500
        cl = _make_vf(
            [".notdef", "periodcentered", "bullet", "uni2027"],
            {0x00B7: "periodcentered", 0x2022: "bullet", 0x2027: "uni2027", 0x30FB: "uni2027"},
            {".notdef": 500, "periodcentered": 529, "bullet": 340, "uni2027": 1000},
        )
        bake_pwid_and_sanitize(cl, spec)
        cm_cl = cl.getBestCmap()
        self.assertEqual(cl["hmtx"].metrics[cm_cl[0x00B7]][0], 500)
        self.assertEqual(cl["hmtx"].metrics[cm_cl[0x2022]][0], 340)
        self.assertEqual(cl["hmtx"].metrics[cm_cl[0x2027]][0], 1000)
        self.assertEqual(cl["hmtx"].metrics[cm_cl[0x30FB]][0], 1000)

    def test_shear_font_preserves_phantom_points(self) -> None:
        font = _make_vf([".notdef", "A"], {0x41: "A"}, {".notdef": 500, "A": 600})
        before_phantom = list(font["gvar"].variations["A"][0].coordinates[-4:])
        shear_font(font, 9.4)
        after_phantom = list(font["gvar"].variations["A"][0].coordinates[-4:])
        self.assertEqual(after_phantom, before_phantom)
        coords = list(font["glyf"]["A"].coordinates)
        self.assertGreater(coords[2][0], coords[1][0])

    def test_inter_recipe_expands_calt_and_append_preserves_uvs(self) -> None:
        spec = load_spec()
        inter = _make_vf(
            [".notdef", "G", "G.1", "zero", "zero.tf", "hyphen", "hyphen.tf", "colon", "colon.tf", "colon.case", "A"],
            {0x47: "G", 0x30: "zero", 0x2D: "hyphen", 0x3A: "colon", 0x41: "A"},
            {
                ".notdef": 656, "G": 700, "G.1": 700, "zero": 610, "zero.tf": 650,
                "hyphen": 400, "hyphen.tf": 650, "colon": 300, "colon.tf": 650, "colon.case": 300, "A": 680,
            },
            axis_range=(200.0, 400.0, 900.0),
        )
        _add_single_gsub(
            inter,
            [
                ("tnum", {"zero": "zero.tf", "hyphen": "hyphen.tf", "colon": "colon.tf"}),
                ("pnum", {"zero.tf": "zero", "hyphen.tf": "hyphen", "colon.tf": "colon"}),
                ("cv10", {"G": "G.1"}),
            ],
        )
        gsub = inter["GSUB"].table
        calt_lk = ot.Lookup()
        calt_lk.LookupType = 6
        calt_lk.LookupFlag = 0
        st = ot.ChainContextSubst()
        st.Format = 3
        b_cov = ot.Coverage(); b_cov.glyphs = ["G"]
        i_cov = ot.Coverage(); i_cov.glyphs = ["colon"]
        a_cov = ot.Coverage(); a_cov.glyphs = ["zero"]
        st.BacktrackCoverage = [b_cov]
        st.BacktrackGlyphCount = 1
        st.InputCoverage = [i_cov]
        st.InputGlyphCount = 1
        st.LookAheadCoverage = [a_cov]
        st.LookAheadGlyphCount = 1
        st.SubstLookupRecord = []
        st.SubstCount = 0
        calt_lk.SubTable = [st]
        calt_lk.SubTableCount = 1
        calt_idx = len(gsub.LookupList.Lookup)
        gsub.LookupList.Lookup.append(calt_lk)
        gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)
        frec = ot.FeatureRecord()
        frec.FeatureTag = "calt"
        frec.Feature = ot.Feature()
        frec.Feature.FeatureParams = None
        frec.Feature.LookupListIndex = [calt_idx]
        frec.Feature.LookupCount = 1
        gsub.FeatureList.FeatureRecord.append(frec)
        gsub.FeatureList.FeatureCount = len(gsub.FeatureList.FeatureRecord)

        recipe_rep = bake_inter_ui_recipe(inter, spec)
        self.assertEqual(recipe_rep["inter_calt_coverages_expanded"], 1)
        self.assertIn("G.1", st.BacktrackCoverage[0].glyphs)
        self.assertIn("G", st.BacktrackCoverage[0].glyphs)
        cmap = inter.getBestCmap()
        self.assertEqual(cmap[0x47], "G.1")
        self.assertEqual(cmap[0x30], "zero")
        self.assertEqual(cmap[0x3A], "colon")
        self.assertEqual(cmap[0x2D], "hyphen")

        base = _make_vf(
            [".notdef", "uni4E00", "uni4E00.alt"],
            {0x4E00: "uni4E00"},
            {".notdef": 1000, "uni4E00": 1000, "uni4E00.alt": 1000},
            uvs={0xE0100: [(0x4E00, "uni4E00.alt")]},
        )
        loaded = LoadedInter(
            font=inter,
            source_path=None,
            fixed_axes={"opsz": 14.0},
            original_gpos=None,
            unscaled_gpos_store=None,
            anchor_fractions={},
            value_fractions={},
        )
        with self.assertRaisesRegex(RuntimeError, "公开轴期望"):
            append_inter_glyphs(base, loaded, {0x4E00, 0x30, 0x47, 0x3A}, spec)

        apply_public_weight_axis(base, spec)
        rep = append_inter_glyphs(base, loaded, {0x4E00, 0x30, 0x47, 0x3A}, spec)
        self.assertEqual(rep["notdef_advance"], 656)
        self.assertEqual(base.getBestCmap()[0x47], "inter.G.1")
        self.assertEqual(base.getBestCmap()[0x30], "inter.zero")
        sub14 = [t for t in base["cmap"].tables if t.format == 14]
        self.assertEqual(len(sub14), 1)
        self.assertEqual(sub14[0].uvsDict, {0xE0100: [(0x4E00, "uni4E00.alt")]})


if __name__ == "__main__":
    unittest.main()
