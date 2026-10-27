from __future__ import annotations

import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from font_audit import contract as audit, products
import build_sarasa_ui_propdigits_sc as build
import font_layout
import package_release as package
import python_env_bootstrap as bootstrap
import render_visual_checks as visual
from test_runtime_regressions import fixture_font
import freetype
import uharfbuzz as hb
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.ttLib import newTable
from fontTools.ttLib.tables._f_v_a_r import Axis
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont


def variable_fixture():
    font = fixture_font()
    axis = Axis(); axis.axisTag = "wght"; axis.minValue = 200; axis.defaultValue = 400; axis.maxValue = 900; axis.flags = 0; axis.axisNameID = 256
    font["fvar"] = newTable("fvar"); font["fvar"].axes = [axis]; font["fvar"].instances = []
    font["gvar"] = newTable("gvar"); font["gvar"].version = 1; font["gvar"].reserved = 0; font["gvar"].variations = {}
    font["head"].flags |= 2
    return font


def add_metric_tables(font, changed_advance=None):
    model = build.VariationModel([{}, {"wght": 1.0}], axisOrder=["wght"])
    horizontal = [build.horizontal_glyph_metrics(font, name) for name in font.getGlyphOrder()]
    varied = list(horizontal)
    if changed_advance:
        name, delta = changed_advance
        gid = font.getGlyphID(name); advance, lsb, rsb = varied[gid]
        varied[gid] = (advance + delta, lsb, rsb + delta)
    build.build_direct_hvar(font, model, [horizontal, varied], horizontal)
    vertical = [build.vertical_glyph_metrics(font, name) for name in font.getGlyphOrder()]
    build.build_direct_vvar(font, model, [vertical, vertical], vertical)


def font_bytes(font):
    stream = io.BytesIO(); font.save(stream)
    return stream.getvalue()


def langsys_signatures(font, tag):
    return {key: sorted(features) for key, features in font_layout.routes(font[tag].table).items()}


class RuntimeControls(unittest.TestCase):

    def test_punctuation_advance_calibration_preserves_other_glyphs_and_native_controls(self):
        target = variable_fixture()
        for table in target["cmap"].tables:
            if table.isUnicode():
                table.cmap.update({ord(char): "G" for char in "…—⸺⸻"})
        source = copy.deepcopy(target)
        source["fvar"].axes[0].minValue = 250
        add_metric_tables(target)
        add_metric_tables(source, ("G", 1))
        region = source["HVAR"].table.VarStore.VarRegionList.Region[0].VarRegionAxis[0]
        region.StartCoord, region.PeakCoord, region.EndCoord = 0.2, 0.4, 0.6
        original = font_bytes(target)
        old_runtime = hb.Font(hb.Face(original))
        protected = {tag: target.getTableData(tag) for tag in ("glyf", "gvar", "hmtx", "GSUB", "GPOS")}
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.ttf"; source.save(source_path)
            with patch.object(build, "source_han_vf_path", return_value=source_path):
                report = build.align_source_punctuation_advances(target, "SC")
                self.assertEqual(report["source_punctuation_advance_glyphs_aligned"], 1)
                first = target.getTableData("HVAR")
                self.assertEqual(build.align_source_punctuation_advances(target, "SC")["source_punctuation_advance_glyphs_aligned"], 0)
                self.assertEqual(target.getTableData("HVAR"), first)
        self.assertEqual({tag: target.getTableData(tag) for tag in protected}, protected)
        changed = hb.Font(hb.Face(font_bytes(target)))
        freetype_font = freetype.Face(io.BytesIO(font_bytes(target)))
        for weight in audit.AXIS_POINTS:
            old_runtime.set_variations({"wght": weight}); changed.set_variations({"wght": weight})
            freetype_font.set_var_design_coords([weight])
            for gid, name in enumerate(target.getGlyphOrder()):
                expected = old_runtime.get_glyph_h_advance(gid) + int(name == "G" and weight == 650)
                self.assertEqual(changed.get_glyph_h_advance(gid), expected, (name, weight))
                freetype_font.load_glyph(gid, freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_HINTING)
                self.assertEqual(freetype_font.glyph.metrics.horiAdvance, expected, (name, weight))

    def test_origin_normalization_preserves_fractional_iup_shape(self):
        font = variable_fixture()
        pen = TTGlyphPen(None); pen.moveTo((0, 0)); pen.qCurveTo((101, 0), (300, 200)); pen.lineTo((300, 700)); pen.lineTo((0, 700)); pen.closePath()
        font["glyf"].glyphs["G"] = pen.glyph()
        coords = [None] * build.gvar_coordinate_count(font, "G")
        coords[0] = (0, 0); coords[2] = (1, 1)
        coords[-4:] = [(64, 0), (64, 0), (0, 0), (0, 0)]
        font["gvar"].variations["G"] = [build.TupleVariation({"wght": (0, 1, 1)}, coords)]
        add_metric_tables(font)
        def points():
            glyphs = font.getGlyphSet(location={"wght": 900})
            recording = DecomposingRecordingPen(glyphs); glyphs["G"].draw(recording)
            return [point for operation, arguments in recording.value for point in arguments if point is not None]
        before = points()
        build.normalize_cross_engine_metrics(font)
        after = points()
        offsets = [(a[0] - b[0], a[1] - b[1]) for a, b in zip(after, before)]
        for axis in (0, 1):
            self.assertLess(max(item[axis] for item in offsets) - min(item[axis] for item in offsets), 1e-8)
        self.assertIsNone(font["gvar"].variations["G"][0].coordinates[1])

    def test_colon_replacement_preserves_native_feature_and_language_records(self):
        path = audit.static_path("SC", "Regular", False, False)
        if not path.exists() or not build.INTER_UPRIGHT.exists():
            self.skipTest("需要固定 Inter 来源和静态字体夹具")
        with TTFont(path) as font:
            before_tags = [record.FeatureTag for record in font["GSUB"].table.FeatureList.FeatureRecord]
            before_languages = langsys_signatures(font, "GSUB")
            build.add_digit_colon_feature(font)
            self.assertEqual(before_tags, [record.FeatureTag for record in font["GSUB"].table.FeatureList.FeatureRecord])
            self.assertEqual(before_languages, langsys_signatures(font, "GSUB"))

    def test_lookup_record_walk_deduplicates_aliases_and_ignores_lazy_font_links(self):
        record = build.ot.SubstLookupRecord(); record.SequenceIndex = 0; record.LookupListIndex = 2
        unrelated = build.ot.SubstLookupRecord(); unrelated.SequenceIndex = 0; unrelated.LookupListIndex = 99
        root = SimpleNamespace(records=[record, record], font=SimpleNamespace(records=[unrelated]))
        self.assertEqual(build.layout_lookup_records(root), [record])

    def test_lookup_reordering_remaps_shared_features_and_records_once(self):
        record = build.ot.SubstLookupRecord(); record.SequenceIndex = 0; record.LookupListIndex = 2
        feature = SimpleNamespace(LookupListIndex=[2])
        first = SimpleNamespace(SubstLookupRecord=[record]); second = SimpleNamespace(SubstLookupRecord=[record]); target = SimpleNamespace()
        gsub = SimpleNamespace(FeatureList=SimpleNamespace(FeatureRecord=[SimpleNamespace(Feature=feature), SimpleNamespace(Feature=feature)]), LookupList=SimpleNamespace(Lookup=[first, second, target]))
        table = SimpleNamespace(table=gsub, ensureDecompiled=lambda: None)
        build.move_gsub_lookups_before({"GSUB": table}, [2], 0)
        self.assertEqual(record.LookupListIndex, 0)
        self.assertEqual(feature.LookupListIndex, [0])
        self.assertIs(gsub.LookupList.Lookup[0], target)

    def test_phantom_only_compensation_is_detected_and_normalized(self):
        font = variable_fixture()
        coords = [(0, 0)] * build.gvar_coordinate_count(font, "G")
        coords[-4:] = [(64, 0), (64, 0), (0, 48), (0, 48)]
        font["gvar"].variations["G"] = [build.TupleVariation({"wght": (0, 1, 1)}, coords)]
        add_metric_tables(font)
        def metrics(data):
            runtime = hb.Font(hb.Face(data)); runtime.set_variations({"wght": 900})
            face = freetype.Face(io.BytesIO(data)); face.set_var_design_coords([900])
            gid = runtime.get_nominal_glyph(ord("G")); extent = runtime.get_glyph_extents(gid)
            face.load_glyph(gid, freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_HINTING)
            return (face.glyph.metrics.horiBearingX, face.glyph.metrics.vertBearingY), (extent.x_bearing, runtime.get_glyph_v_origin(gid)[1] - extent.y_bearing)
        before, expected = metrics(font_bytes(font))
        self.assertEqual((before[0] - expected[0], before[1] - expected[1]), (64, -48))
        report = build.normalize_cross_engine_metrics(font)
        actual, target = metrics(font_bytes(font))
        self.assertEqual(actual, target)
        self.assertEqual(target, expected)
        self.assertEqual(report["runtime_metric_preservation_mismatches"], 0)
        self.assertIn("HVAR", font)
        self.assertNotIn("VVAR", font)

    def test_zero_alternate_uses_the_same_tabular_advance(self):
        font = variable_fixture()
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap.update({cp: "G" if cp == 48 else "T" for cp in range(48, 58)})
        for name, advance in (("G", 500), ("T", 550), ("Galt", 600), ("o", 600), ("acutecomb", 600)):
            font["hmtx"].metrics[name] = (advance, 0)
        font["GDEF"].table.GlyphClassDef.classDefs = {name: 1 for name in font.getGlyphOrder()}
        addOpenTypeFeaturesFromString(font, "languagesystem DFLT dflt; feature tnum { sub G by Galt; sub T by o; } tnum; feature zero { sub Galt by acutecomb; } zero;")
        coords = [(0, 0)] * build.gvar_coordinate_count(font, "acutecomb"); coords[-3] = (-1, 0)
        font["gvar"].variations["acutecomb"] = [build.TupleVariation({"wght": (0, 1, 1)}, coords)]
        add_metric_tables(font, ("acutecomb", -1))
        def widths():
            runtime = hb.Font(hb.Face(font_bytes(font))); runtime.set_variations({"wght": 900})
            buffer = hb.Buffer(); buffer.add_str("0123456789"); buffer.guess_segment_properties()
            hb.shape(runtime, buffer, {"tnum": True, "zero": True})
            return [position.x_advance for position in buffer.glyph_positions]
        self.assertEqual(widths(), [599] + [600] * 9)
        build.align_tabular_alternate_advances(font)
        self.assertEqual(widths(), [600] * 10)

    def test_required_colon_combinations_and_boundaries_are_registered(self):
        self.assertTrue({"1:", ":2", "1:2", "a::::::::::2"} <= set(products.COLON_TEXTS))
        self.assertGreaterEqual(len(products.COLON_TEXTS), 480)
        self.assertIn({"tnum": True}, products.WIDTH_FEATURES)
        self.assertIn({"tnum": True, "zero": True}, products.WIDTH_FEATURES)

    def test_fontbakery_cannot_reuse_the_calling_environment(self):
        with self.assertRaisesRegex(RuntimeError, "独立"):
            bootstrap.isolated_tool_python(Path(sys.prefix), Path("unused.txt"), {})


class VisualControls(unittest.TestCase):
    def test_platform_independent_label_font_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.ttf"; path.touch()
            with patch.object(visual, "LABEL_FONT", None), patch.dict(os.environ, {"SARASA_VISUAL_LABEL_FONT": ""}), patch.object(audit, "static_path", return_value=path):
                self.assertEqual(visual.label_font_path(), path)
            with patch.object(visual, "LABEL_FONT", path):
                self.assertEqual(visual.label_font_path(), path)

    def test_stale_or_incomplete_visual_reports_are_rejected(self):
        path = package.ROOT / "reports" / "visual-audit.json"
        if not path.exists():
            self.skipTest("需要仓库中的已发布视觉记录作为结构夹具")
        # Reuse the published image structure with the current directory
        # naming contract. This fixture never writes a review to the repository.
        text = re.sub(r"(SarasaUiPropDigits[A-Z]+-TTF(?:-Unhinted)?-)\d+(?:\.\d+)+/",
                      lambda match: match.group(1) + build.VERSION + "/", path.read_text(encoding="utf-8"))
        report = json.loads(text)
        manifest = report["input_manifest"]
        report["reviewed_fonts"] = sorted(manifest)
        report["generator_sha256"] = package.package_sha256(package.ROOT / report["generator"])
        # This is a structural fixture, not a new review attestation. Renderer
        # development may have regenerated the example image files in place.
        for item in [*report["images"], *report.get("runtime_screenshots", [])]:
            item["sha256"] = package.package_sha256(package.ROOT / item["file"])
            if item["file"].endswith("-static.png"):
                item["small_size_cases"] = [
                    {"font": case["font"], "weight": case["weight"], "italic": case["italic"], "pixels": pixels, "magnification": 2}
                    for case in item["cases"] if case["hinted"] for pixels in (9, 12, 16)
                ]
            elif item["file"].endswith("-detail.png"):
                region=item["region"]; vf=audit.display_path(audit.vf_path(region, False))
                item["language_cases"]=[{"font":vf,"weight":400,"script":script,"language":language,
                                         "text":"… — —— ⸺ ——— ⸻","pixels":28}
                                        for script,language in (("Latn",None),("Latn","de"),("Cyrl","ru"),("Grek","el"),
                                                                ("Latn","zh-Hans"),("Latn","ko"),("Hani",None))]
                item["width_cases"]=[{"font":vf,"weight":400,"features":features,
                                      "text":"".join(chr(cp) for cp in sorted(audit.INTENTIONAL_CPS)),"pixels":25}
                                     for features in ({},{"pnum":True},{"tnum":True})]
                item["vertical_cases"]=[{"font":audit.display_path(audit.vf_path(region,italic) if variable else audit.static_path(region,"Regular",italic,False)),
                                         "weight":400,"italic":italic,"variable":variable,"script":"Hani","direction":"ttb","text":text,"pixels":34}
                                        for variable in (True,False) for italic in (False,True)
                                        for text in build.VISUAL_VERTICAL_RUN_TEXTS]
        package.validate_visual_report(report, manifest)
        mutations = (
            lambda value: value.update(images=[]),
            lambda value: value.update(reviewed_fonts=[]),
            lambda value: value.update(generator_sha256="incorrect"),
            lambda value: value["images"][0].update(sha256="incorrect"),
            lambda value: value["images"][0].update(reviewed=False),
            lambda value: value["images"][0].update(passed=False),
            lambda value: value["images"][0].update(cases=[]),
            lambda value: value["images"][0].update(observations=[]),
            lambda value: next(item for item in value["images"] if item["file"].endswith("-static.png")).update(small_size_cases=[]),
            lambda value: next(item for item in value["images"] if item["file"].endswith("-detail.png")).update(language_cases=[]),
            lambda value: next(item for item in value["images"] if item["file"].endswith("-detail.png")).update(width_cases=[]),
            lambda value: next(item for item in value["images"] if item["file"].endswith("-detail.png")).update(vertical_cases=[]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                invalid = copy.deepcopy(report); mutate(invalid)
                with self.assertRaises(ValueError):
                    package.validate_visual_report(invalid, manifest)


if __name__ == "__main__":
    unittest.main()
