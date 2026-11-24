"""阶段 2 单元测试：fixes.py、metrics.py、names.py 行为、反例与幂等性。"""
from __future__ import annotations

import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables._c_m_a_p import cmap_format_14
from fontTools.ttLib.tables._f_v_a_r import Axis
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.varLib.varStore import VarStoreInstancer

from sarasa_pd.spec import load_spec
from sarasa_pd.vf.base import apply_public_weight_axis, normalized_wght
from sarasa_pd.vf.fixes import (
    fix_classical_ideograph_fullwidth,
    fix_ideograph_gdef_classes,
    fix_os2_unicode_range_bit57,
    prune_unreachable_glyphs,
)
from sarasa_pd.vf.metrics import (
    normalize_vertical_metrics,
    optimize_vf_gvar,
    rebuild_vf_hvar_from_gvar,
    rebuild_vf_mvar,
    recalculate_head_bounds_and_lsb,
)
from sarasa_pd.vf.names import (
    apply_deterministic_head,
    normalize_nonhinting_raster_metadata,
    rebuild_vf_stat,
    update_vf_names,
    update_vf_style_and_os2,
)


def _rect(x0: int, y0: int, x1: int, y1: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x0, y0))
    pen.lineTo((x1, y0))
    pen.lineTo((x1, y1))
    pen.lineTo((x0, y1))
    pen.closePath()
    return pen.glyph()


def _make_sample_vf(with_sip: bool = True) -> TTFont:
    spec = load_spec()
    glyphs = [".notdef", "han_narrow", "han_mark", "han_uvs", "orphan", "inter.A"]
    cmap = {0x5DC5: "han_narrow", 0x3A89: "han_mark", 0x0041: "inter.A"}
    if with_sip:
        glyphs.append("sip_han")
        cmap[0x28A99] = "sip_han"
    widths = {g: 1000 for g in glyphs}
    widths["han_narrow"] = 920
    widths["inter.A"] = 660

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf({g: _rect(40, 0, widths[g] - 40, 720) for g in glyphs})
    fb.setupHorizontalMetrics({g: (widths[g], 0) for g in glyphs})  # 故意 lsb=0 != xMin=40
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": "Orig", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font

    fvar = newTable("fvar")
    ax = Axis()
    ax.axisTag = "wght"
    ax.minValue, ax.defaultValue, ax.maxValue = (250.0, 400.0, 900.0)
    ax.axisNameID = 256
    fvar.axes = [ax]
    fvar.instances = []
    font["fvar"] = fvar

    avar = newTable("avar")
    avar.segments = {"wght": {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}}
    font["avar"] = avar
    apply_public_weight_axis(font, spec)

    gvar = newTable("gvar")
    gvar.version = 1
    gvar.reserved = 0
    gvar.variations = {
        g: [TupleVariation({"wght": (0.0, 1.0, 1.0)}, [(5, 0)] * 4 + [(0, 0), (40, 0), (0, 0), (0, 0)])]
        for g in glyphs
    }
    font["gvar"] = gvar

    gdef = newTable("GDEF")
    gdef.table = ot.GDEF()
    gdef.table.Version = 0x00010000
    gdef.table.GlyphClassDef = ot.ClassDef()
    gdef.table.GlyphClassDef.classDefs = {"han_mark": 3, "inter.A": 1}
    font["GDEF"] = gdef

    sub14 = cmap_format_14(14)
    sub14.platformID = 0
    sub14.platEncID = 5
    sub14.language = 0
    sub14.cmap = {}
    sub14.uvsDict = {0xE0100: [(0x5DC5, "han_uvs")]}
    font["cmap"].tables.append(sub14)

    vmtx = newTable("vmtx")
    vmtx.metrics = {g: (1000, 100) for g in glyphs}
    font["vmtx"] = vmtx
    return font


class VfFixesMetricsNamesTests(unittest.TestCase):
    def test_a5_gdef_bit57_and_unreachable_uvs_protection(self) -> None:
        font = _make_sample_vf(with_sip=True)

        # 非 CL 不动 A5
        self.assertEqual(fix_classical_ideograph_fullwidth(font, "SC")["a5_cl_ideographs_fixed"], 0)
        self.assertEqual(font["hmtx"]["han_narrow"][0], 920)

        # CL 修正 A5 且幂等
        r1 = fix_classical_ideograph_fullwidth(font, "CL")
        self.assertGreaterEqual(r1["a5_cl_ideographs_fixed"], 1)
        self.assertEqual(font["hmtx"]["han_narrow"][0], 1000)
        self.assertEqual(font["gvar"].variations["han_narrow"][0].coordinates[-3], (0, 0))
        self.assertEqual(fix_classical_ideograph_fullwidth(font, "CL")["a5_cl_ideographs_fixed"], 0)

        # GDEF 汉字误标为 mark -> base 且幂等
        self.assertEqual(fix_ideograph_gdef_classes(font)["ideograph_gdef_marks_fixed"], 1)
        self.assertEqual(font["GDEF"].table.GlyphClassDef.classDefs["han_mark"], 1)
        self.assertEqual(fix_ideograph_gdef_classes(font)["ideograph_gdef_marks_fixed"], 0)

        # OS/2 bit 57（Non-Plane 0）
        b_rep = fix_os2_unicode_range_bit57(font)
        self.assertTrue(b_rep["os2_bit57_after"])
        self.assertTrue(bool(font["OS/2"].ulUnicodeRange2 & (1 << 25)))

        # 清理孤立字形：删除 orphan，但必须保留 han_uvs
        p_rep = prune_unreachable_glyphs(font)
        self.assertEqual(p_rep["unreachable_glyphs_removed"], 1)
        self.assertNotIn("orphan", font.getGlyphOrder())
        self.assertIn("han_uvs", font.getGlyphOrder())
        self.assertEqual(prune_unreachable_glyphs(font)["unreachable_glyphs_removed"], 0)

    def test_metrics_mvar_hvar_and_bounds(self) -> None:
        spec = load_spec()
        font = _make_sample_vf(with_sip=False)
        normalize_vertical_metrics(font)
        self.assertNotIn("VORG", font)
        self.assertNotIn("VVAR", font)
        # inter.A yMax=720 -> tsb = 880 - 720 = 160
        self.assertEqual(font["vmtx"]["inter.A"], (1000, 160))

        mrep = rebuild_vf_mvar(font, spec)
        self.assertEqual(mrep["mvar_records"], 3)
        store = font["MVAR"].table.VarStore
        by_tag = {r.ValueTag: r.VarIdx for r in font["MVAR"].table.ValueRecord}
        for w in spec.weights:
            loc = {"wght": normalized_wght(font, w.value)}
            inst = VarStoreInstancer(store, font["fvar"].axes, loc)
            self.assertEqual(font["post"].underlineThickness + round(inst[by_tag["unds"]]), w.underline_thickness)
            self.assertEqual(font["post"].underlinePosition + round(inst[by_tag["undo"]]), w.underline_position)

        brep = recalculate_head_bounds_and_lsb(font)
        self.assertGreater(brep["lsb_fixed"], 0)
        self.assertEqual(font["hmtx"]["inter.A"][1], font["glyf"]["inter.A"].xMin)

        optimize_vf_gvar(font, italic=False)
        hrep = rebuild_vf_hvar_from_gvar(font)
        self.assertIn("HVAR", font)
        self.assertGreaterEqual(hrep["hvar_regions"], 1)

    def test_names_stat_raster_and_deterministic_head(self) -> None:
        spec = load_spec()
        font = _make_sample_vf(with_sip=False)
        nrep = update_vf_names(font, "CL", False, spec)
        self.assertIn("ShangguSansTC-VF", nrep["vf_version_string"])
        self.assertIn(spec.shanggu_copyright, font["name"].getDebugName(0))
        self.assertEqual(font["name"].getDebugName(14), spec.license_url)
        self.assertFalse(any(r.platformID == 1 for r in font["name"].names))
        self.assertEqual(len(font["fvar"].instances), 6)

        update_vf_style_and_os2(font, "CL", True, spec)
        self.assertEqual(font["post"].italicAngle, spec.italic_angle)
        self.assertEqual(font["OS/2"].achVendID, spec.vendor_id)
        rebuild_vf_stat(font, True, spec)
        self.assertIn("STAT", font)

        normalize_nonhinting_raster_metadata(font, spec)
        self.assertEqual(font["gasp"].gaspRange, {0xFFFF: 0x000F})
        self.assertEqual(bytes(font["prep"].program.getBytecode()), spec.smart_dropout_prep)

        # 反例：带字形指令时应拒绝覆盖
        font["glyf"]["inter.A"].program = Program()
        font["glyf"]["inter.A"].program.fromBytecode(b"\x00")
        with self.assertRaisesRegex(ValueError, "含有逐字形 hint"):
            normalize_nonhinting_raster_metadata(font, spec)

        hrep = apply_deterministic_head(font, spec)
        self.assertEqual(hrep["head_modified_unix"], spec.head_modified_timestamp)


if __name__ == "__main__":
    unittest.main()
