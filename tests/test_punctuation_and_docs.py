"""标点布局幂等性（punctuation.py）与规格文档同步校验（docs_gen.py）单元测试。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot

from sarasa_pd.docs_gen import check_doc_files, doc_paths, render_spec_block, sync_doc_files, sync_doc_text
from sarasa_pd.spec import load_spec
from sarasa_pd.vf.layout_utils import Feature, install, routes
from sarasa_pd.vf.punctuation import apply_source_han_punctuation_layout


def _rect():
    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((500, 0))
    pen.lineTo((500, 700))
    pen.lineTo((0, 700))
    pen.closePath()
    return pen.glyph()


def _make_punct_font() -> TTFont:
    glyphs = [
        ".notdef", "dash_prop", "dash_fw", "dash_kor",
        "dash_enc2", "dash_enc3", "dash_fw2", "dash_fw3",
        "dash_v1", "dash_v2", "dash_v3",
        "ell_prop", "ell_fw", "ell_v",
    ]
    cmap = {
        0x2014: "dash_prop",
        0x2015: "dash_fw",
        0x2E3A: "dash_enc2",
        0x2E3B: "dash_enc3",
        0xFE31: "dash_v1",
        0x2026: "ell_prop",
        0x22EF: "ell_fw",
    }
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf({g: _rect() for g in glyphs})
    fb.setupHorizontalMetrics({g: (1000, 0) for g in glyphs})
    fb.setupHorizontalHeader(ascent=880, descent=-120)
    fb.setupNameTable({"familyName": "PunctTest", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font

    # 构造初始 ccmp、locl（含 KOR 专属单破折号）、vert
    lk_ccmp = ot.Lookup(); lk_ccmp.LookupType = 4; lk_ccmp.LookupFlag = 0
    st_lig = ot.LigatureSubst()
    l2 = ot.Ligature(); l2.Component = ["dash_fw"]; l2.CompCount = 2; l2.LigGlyph = "dash_fw2"
    l3 = ot.Ligature(); l3.Component = ["dash_fw", "dash_fw"]; l3.CompCount = 3; l3.LigGlyph = "dash_fw3"
    st_lig.ligatures = {"dash_fw": [l3, l2]}
    lk_ccmp.SubTable = [st_lig]; lk_ccmp.SubTableCount = 1

    lk_kor = ot.Lookup(); lk_kor.LookupType = 1; lk_kor.LookupFlag = 0
    st_kor = ot.SingleSubst(); st_kor.mapping = {"dash_prop": "dash_kor"}
    lk_kor.SubTable = [st_kor]; lk_kor.SubTableCount = 1

    lk_vert = ot.Lookup(); lk_vert.LookupType = 1; lk_vert.LookupFlag = 0
    st_v = ot.SingleSubst()
    st_v.mapping = {"dash_fw": "dash_v1", "dash_fw2": "dash_v2", "dash_fw3": "dash_v3", "ell_fw": "ell_v"}
    lk_vert.SubTable = [st_v]; lk_vert.SubTableCount = 1

    gsub = newTable("GSUB")
    gsub.table = ot.GSUB(); gsub.table.Version = 0x00010000
    gsub.table.LookupList = ot.LookupList()
    gsub.table.LookupList.Lookup = [lk_ccmp, lk_kor, lk_vert]
    gsub.table.LookupList.LookupCount = 3
    install(
        gsub.table,
        {
            ("DFLT", "dflt"): {"ccmp": Feature((0,)), "vert": Feature((2,)), "vrt2": Feature((2,))},
            ("latn", "dflt"): {"ccmp": Feature((0,)), "vert": Feature((2,))},
            ("hani", "dflt"): {"ccmp": Feature((0,)), "vert": Feature((2,)), "vrt2": Feature((2,))},
            ("hani", "ZHS "): {"ccmp": Feature((0,)), "vert": Feature((2,))},
            ("hang", "KOR "): {"ccmp": Feature((0,)), "locl": Feature((1,)), "vert": Feature((2,))},
        },
    )
    font["GSUB"] = gsub
    return font


class PunctuationAndDocsTests(unittest.TestCase):
    def test_punctuation_layout_routes_bopo_keeps_fe31_unligated_and_is_idempotent(self) -> None:
        spec = load_spec()
        font = _make_punct_font()
        r1 = apply_source_han_punctuation_layout(font, "SC", spec)
        self.assertTrue(r1["punctuation_layout_applied"])
        self.assertEqual(r1["korean_dash_target"], "dash_kor")

        rt1 = routes(font["GSUB"].table)
        for sc in ("bopo", "hang", "hani", "kana"):
            self.assertIn((sc, "dflt"), rt1)
            self.assertIn("locl", rt1[(sc, "dflt")])
        self.assertNotIn("locl", rt1[("DFLT", "dflt")])
        self.assertNotIn("locl", rt1[("latn", "dflt")])

        # U+FE31（dash_v1）绝不得出现在任何 ccmp 连字里
        for lk in font["GSUB"].table.LookupList.Lookup:
            if lk.LookupType == 4:
                for st in lk.SubTable:
                    self.assertNotIn("dash_v1", st.ligatures)

        # 幂等性：再执行一次后，活跃路由映射语义保持一致，且单/多语言 locl 不堆积重复目标
        from sarasa_pd.vf.layout_utils import prune_unreferenced_lookups, remove_empty_lookups
        remove_empty_lookups(font)
        prune_unreferenced_lookups(font)
        count1 = len(font["GSUB"].table.LookupList.Lookup)

        apply_source_han_punctuation_layout(font, "SC", spec)
        remove_empty_lookups(font)
        prune_unreferenced_lookups(font)
        count2 = len(font["GSUB"].table.LookupList.Lookup)
        self.assertEqual(count1, count2)

    def test_docs_gen_sync_and_drift_detection(self) -> None:
        spec = load_spec()
        # weights 是 BEGIN 紧跟 END 的空块（表格尚未生成），也必须被识别并填上
        template = "# 规格\n\n<!-- BEGIN SPEC:regions -->\n旧表\n<!-- END SPEC:regions -->\n\n<!-- BEGIN SPEC:weights -->\n<!-- END SPEC:weights -->\n"
        synced = sync_doc_text(template, spec)
        self.assertIn("ShangguSansTC-VF", synced)
        self.assertIn("Heavy.json", synced)
        self.assertEqual(sync_doc_text(synced, spec), synced)
        with tempfile.TemporaryDirectory() as raw_tmp:
            p = Path(raw_tmp) / "spec.md"
            p.write_bytes(synced.encode("utf-8"))
            self.assertEqual(check_doc_files([p], spec), [])
            p.write_bytes(synced.replace("Heavy.json", "Bold.json").encode("utf-8"))
            problems = check_doc_files([p], spec)
            self.assertEqual(len(problems), 1)
            self.assertIn("（weights）", problems[0])
            # 未生成的空块与过期的表一样要报出来，不能当作一致放过
            p.write_bytes(template.encode("utf-8"))
            self.assertIn("（regions、weights）", check_doc_files([p], spec)[0])
            # 缺 END、名称不配对、标记不在行首、未知名称：核对报错，同步拒绝改写
            for bad in (
                "<!-- BEGIN SPEC:regions -->\n旧表\n",
                "<!-- BEGIN SPEC:regions -->\n<!-- END SPEC:weights -->\n",
                " <!-- BEGIN SPEC:regions -->\n<!-- END SPEC:regions -->\n",
                "<!-- BEGIN SPEC:nope -->\n<!-- END SPEC:nope -->\n",
            ):
                p.write_bytes(bad.encode("utf-8"))
                self.assertEqual(len(check_doc_files([p], spec)), 1, bad)
                with self.assertRaises(ValueError, msg=bad):
                    sync_doc_text(bad, spec)
            self.assertEqual(check_doc_files([Path(raw_tmp) / "missing.md"], spec), ["missing.md：文件不存在"])

    def test_sync_doc_files_writes_lf_and_refuses_bad_markers(self) -> None:
        spec = load_spec()
        with tempfile.TemporaryDirectory() as raw_tmp:
            good = Path(raw_tmp) / "good.md"
            plain = Path(raw_tmp) / "plain.md"
            good.write_bytes("# 文档\n\n<!-- BEGIN SPEC:sources -->\n<!-- END SPEC:sources -->\n".encode("utf-8"))
            plain.write_bytes("# 没有表格\n".encode("utf-8"))
            self.assertEqual(sync_doc_files([good, plain], spec), [good])
            raw = good.read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertIn(render_spec_block("sources", spec), raw.decode("utf-8"))
            self.assertEqual(check_doc_files([good, plain], spec), [])
            self.assertEqual(sync_doc_files([good, plain], spec), [])
            # 任一文件标记有误时整批都不写
            bad = Path(raw_tmp) / "bad.md"
            bad.write_bytes("<!-- BEGIN SPEC:regions -->\n".encode("utf-8"))
            plain.write_bytes("# 没有表格\n\n<!-- BEGIN SPEC:regions -->\n<!-- END SPEC:regions -->\n".encode("utf-8"))
            with self.assertRaises(ValueError):
                sync_doc_files([plain, bad], spec)
            self.assertNotIn(render_spec_block("regions", spec), plain.read_text(encoding="utf-8"))

    def test_repo_docs_are_synced_with_spec(self) -> None:
        from sarasa_pd.env import repo_root

        # 只要求 README 在、规格表与 spec.toml 一致；docs/ 下有几份、叫什么都不限。
        self.assertEqual(check_doc_files(doc_paths(repo_root()), load_spec()), [])


if __name__ == "__main__":
    unittest.main()
