"""VF 基础工具单元测试：gvar 同支撑元组合并、IUP 重优化、路由合并与别名继承守卫。"""
from __future__ import annotations

import copy
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables import otTables as ot

from sarasa_pd.vf.layout_utils import (
    Feature,
    inherit_glyph_aliases,
    install,
    merge_routes,
    remove_empty_lookups,
    routes,
    share_default_with_cyrl_grek,
    sort_pair_sets,
)
from sarasa_pd.vf.variations import merge_same_support_gvar_tuples, reoptimize_gvar_iup


def _rect(x0: int, y0: int, x1: int, y1: int):
    pen = TTGlyphPen(None)
    pen.moveTo((x0, y0))
    pen.lineTo((x0, y1))
    pen.lineTo((x1, y1))
    pen.lineTo((x1, y0))
    pen.closePath()
    return pen.glyph()


def _make_var_font():
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef", "A", "B"])
    fb.setupCharacterMap({0x41: "A", 0x42: "B"})
    fb.setupGlyf({".notdef": _rect(50, 0, 500, 700), "A": _rect(50, 0, 550, 700), "B": _rect(50, 0, 550, 700)})
    fb.setupHorizontalMetrics({".notdef": (600, 50), "A": (600, 50), "B": (600, 50)})
    fb.setupHorizontalHeader(ascent=969, descent=-241)
    fb.setupNameTable({"familyName": "VarTest", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font
    gvar = newTable("gvar")
    gvar.version = 1
    gvar.reserved = 0
    # 4 个真实点 + 4 个幽灵点 = 8 点
    gvar.variations = {
        ".notdef": [],
        "A": [
            TupleVariation({"wght": (0.0, 1.0, 1.0)}, [(10, 0), (10, 0), (20, 0), (20, 0), (0, 0), (30, 0), (0, 0), (0, 0)]),
            TupleVariation({"wght": (0.0, 1.0, 1.0)}, [(5, 0), (5, 0), (5, 0), (5, 0), (0, 0), (10, 0), (0, 0), (0, 0)]),
            # 两个相互抵消的元组，合并后应完全清除
            TupleVariation({"wght": (-1.0, -1.0, 0.0)}, [(4, 0), (4, 0), (4, 0), (4, 0), (0, 0), (0, 0), (0, 0), (0, 0)]),
            TupleVariation({"wght": (-1.0, -1.0, 0.0)}, [(-4, 0), (-4, 0), (-4, 0), (-4, 0), (0, 0), (0, 0), (0, 0), (0, 0)]),
        ],
        "B": [
            # 所有点水平等平移，IUP 容差 0 应可压缩部分点为 None
            TupleVariation({"wght": (0.0, 1.0, 1.0)}, [(12, 0), (12, 0), (12, 0), (12, 0), (0, 0), (0, 0), (0, 0), (0, 0)]),
        ],
    }
    font["gvar"] = gvar
    return font


class VfUtilsTests(unittest.TestCase):
    def test_merge_same_support_gvar_tuples_is_lossless_and_idempotent(self) -> None:
        font = _make_var_font()
        r1 = merge_same_support_gvar_tuples(font)
        self.assertEqual(r1["tuples_before"], 5)
        self.assertEqual(r1["tuples_after"], 2)
        self.assertEqual(r1["zero_tuples_dropped"], 1)
        self.assertEqual(len(font["gvar"].variations["A"]), 1)
        self.assertEqual(
            font["gvar"].variations["A"][0].coordinates,
            [(15, 0), (15, 0), (25, 0), (25, 0), (0, 0), (40, 0), (0, 0), (0, 0)],
        )
        # 幂等性
        r2 = merge_same_support_gvar_tuples(font)
        self.assertEqual(r2["tuples_before"], 2)
        self.assertEqual(r2["tuples_after"], 2)
        self.assertEqual(r2["zero_tuples_dropped"], 0)

    def test_reoptimize_gvar_iup_compresses_and_is_idempotent(self) -> None:
        font = _make_var_font()
        merge_same_support_gvar_tuples(font)
        r1 = reoptimize_gvar_iup(font, tolerance=0.0)
        self.assertGreaterEqual(r1["tuples_optimized"], 1)
        self.assertTrue(any(pt is None for pt in font["gvar"].variations["B"][0].coordinates[:4]))
        r2 = reoptimize_gvar_iup(font, tolerance=0.0)
        self.assertEqual(r2["tuples_optimized"], 0)

    def test_merge_routes_and_cyrl_grek_share_default(self) -> None:
        font = _make_var_font()
        gsub = newTable("GSUB")
        gsub.table = ot.GSUB()
        gsub.table.Version = 0x00010000
        gsub.table.LookupList = ot.LookupList()
        gsub.table.LookupList.Lookup = []
        gsub.table.LookupList.LookupCount = 0
        install(
            gsub.table,
            {
                ("DFLT", "dflt"): {"tnum": Feature((0,)), "vert": Feature((1,))},
                ("hani", "dflt"): {"locl": Feature((2,))},
                ("cyrl", "dflt"): {"vert": Feature((3,))},
            },
        )
        merge_routes(gsub.table, {("latn", "ROM "): {"locl": Feature((4,))}})
        font["GSUB"] = gsub
        # 远东 hani/dflt 应获得 DFLT 的 tnum 与 vert
        r = routes(font["GSUB"].table)
        self.assertIn("tnum", r[("hani", "dflt")])
        self.assertNotIn("tnum", r[("latn", "ROM ")])
        # cyrl/dflt 共享 DFLT 特性，但保留自己的 vert 路由（3 而非 1）
        share_default_with_cyrl_grek(font)
        r2 = routes(font["GSUB"].table)
        self.assertIn("tnum", r2[("cyrl", "dflt")])
        self.assertEqual(r2[("cyrl", "dflt")]["vert"].lookups, (3,))

    def test_sort_pair_sets_orders_by_gid(self) -> None:
        font = _make_var_font()
        gpos = newTable("GPOS")
        gpos.table = ot.GPOS()
        gpos.table.Version = 0x00010000
        sub = ot.PairPos()
        sub.Format = 1
        sub.Coverage = ot.Coverage()
        sub.Coverage.glyphs = ["A"]
        ps = ot.PairSet()
        r_b = ot.PairValueRecord(); r_b.SecondGlyph = "B"
        r_a = ot.PairValueRecord(); r_a.SecondGlyph = "A"
        ps.PairValueRecord = [r_b, r_a]
        ps.PairValueCount = 2
        sub.PairSet = [ps]
        sub.PairSetCount = 1
        lk = ot.Lookup(); lk.LookupType = 2; lk.LookupFlag = 0; lk.SubTable = [sub]; lk.SubTableCount = 1
        gpos.table.LookupList = ot.LookupList(); gpos.table.LookupList.Lookup = [lk]; gpos.table.LookupList.LookupCount = 1
        font["GPOS"] = gpos
        self.assertEqual(sort_pair_sets(font), 1)
        self.assertEqual([r.SecondGlyph for r in ps.PairValueRecord], ["A", "B"])
        self.assertEqual(sort_pair_sets(font), 0)

    def test_inherit_glyph_aliases_preserves_shared_coverage_and_existing_rules(self) -> None:
        font = _make_var_font()
        order = font.getGlyphOrder() + ["A.alias"]
        font.setGlyphOrder(order)
        font["glyf"]["A.alias"] = copy.deepcopy(font["glyf"]["A"])
        font["hmtx"]["A.alias"] = font["hmtx"]["A"]

        gsub = newTable("GSUB")
        gsub.table = ot.GSUB(); gsub.table.Version = 0x00010000
        s1 = ot.SingleSubst(); s1.mapping = {"A": "B", "A.alias": "A.alias"}
        lk = ot.Lookup(); lk.LookupType = 1; lk.LookupFlag = 0; lk.SubTable = [s1]; lk.SubTableCount = 1
        gsub.table.LookupList = ot.LookupList(); gsub.table.LookupList.Lookup = [lk]; gsub.table.LookupList.LookupCount = 1
        gsub.table.FeatureList = ot.FeatureList(); gsub.table.FeatureList.FeatureRecord = []; gsub.table.FeatureList.FeatureCount = 0
        gsub.table.ScriptList = ot.ScriptList(); gsub.table.ScriptList.ScriptRecord = []; gsub.table.ScriptList.ScriptCount = 0
        font["GSUB"] = gsub
        inherit_glyph_aliases(font, {"A": {"A.alias"}})
        # 不得覆盖 A.alias 已有规则（C23.7）
        self.assertEqual(s1.mapping["A.alias"], "A.alias")
        # remove_empty_lookups 无崩
        self.assertEqual(remove_empty_lookups(font), 0)


if __name__ == "__main__":
    unittest.main()
