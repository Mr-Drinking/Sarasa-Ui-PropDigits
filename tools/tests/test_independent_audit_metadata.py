from __future__ import annotations

import io
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as build
import audit_sarasa_ui_propdigits as audit
import package_release as package
from fontTools.ttLib import TTFont
import uharfbuzz as hb
from test_runtime_regressions import fixture_font
from test_release_runtime_controls import variable_fixture


class IndependentMetadataTests(unittest.TestCase):
    def test_baked_inter_presets_preserve_comma_semicolon_width_features(self):
        if not build.INTER_UPRIGHT.is_file():
            self.skipTest("需要固定 Inter 来源")
        with TTFont(build.INTER_UPRIGHT) as font:
            source = build.serialized_font_bytes(font)
            build.bake_inter_feature_defaults(font)
            baked = build.serialized_font_bytes(font)

        def shape(data, weight, features):
            runtime = hb.Font(hb.Face(data))
            runtime.set_variations({"wght": weight})
            buffer = hb.Buffer()
            buffer.add_str(",;G")
            buffer.script = "Latn"
            buffer.direction = "ltr"
            hb.shape(runtime, buffer, {"kern": False, "calt": False, **features})
            return [(info.codepoint, pos.x_advance, pos.x_offset, pos.y_offset)
                    for info, pos in zip(buffer.glyph_infos, buffer.glyph_positions)]

        for weight in audit.INTER_POSITION_WEIGHTS:
            for width in ({}, {"tnum": True}, {"pnum": True}, {"tnum": True, "pnum": True}):
                with self.subTest(weight=weight, features=width):
                    self.assertEqual(
                        shape(source, weight, {"ss03": True, "cv10": True, **width}),
                        shape(baked, weight, {"ss03": False, "cv10": False, **width}),
                    )

    def test_head_recalculation_preserves_native_glyph_bounds_and_origins(self):
        font = fixture_font()
        font.recalcBBoxes = False
        glyph = font["glyf"]["G"]
        glyph.xMin = -1
        glyph.xMax = 401
        font["head"].xMin = 0
        font["head"].xMax = 0
        before = {tag: font.getTableData(tag) for tag in ("glyf", "hmtx", "vmtx")}
        build.recalculate_head_bounds(font)
        self.assertEqual((font["head"].xMin, font["head"].xMax), (-1, 401))
        self.assertEqual({tag: font.getTableData(tag) for tag in before}, before)
        self.assertEqual((glyph.xMin, glyph.xMax), (-1, 401))

    def test_license_documents_follow_upstream_and_reject_stale_copies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_path = root / "upstream.ttf"
            source = fixture_font()
            source["name"].names = [record for record in source["name"].names if record.nameID != 0]
            # A different fixture year proves that the oracle is the upstream
            # record, rather than the current project's copyright constant.
            copyright_text = "© 2014-2031 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'."
            source["name"].setName(copyright_text, 0, 3, 1, 0x409)
            source.save(source_path)
            license_bytes = ("Portions " + copyright_text + "\nLicense fixture\n").encode("utf-8")
            notice = copyright_text.split(", with Reserved Font Name", 1)[0]
            with patch.multiple(build, ROOT=root, STATIC_ROOT=root / "fonts/static"), patch.object(audit, "ROOT", root), patch.object(build, "source_han_vf_path", return_value=source_path):
                copies = [build.static_dir(region, hinted) / "LICENSE-Sarasa-Gothic.txt"
                          for region in build.REGION_ORDER for hinted in (True, False)]
                for path in copies:
                    path.parent.mkdir(parents=True, exist_ok=True)

                def reset_documents():
                    (root / "LICENSE").write_bytes(license_bytes)
                    (root / "NOTICE.md").write_text(notice, encoding="utf-8")
                    for path in copies:
                        path.write_bytes(license_bytes)

                reset_documents()
                result = audit.source_han_license_documents_status()
                self.assertTrue(result["ok"])
                self.assertEqual(result["documents_checked"], 14)
                package.validate_license_documentation({"source_han_license_documents": result})
                for path in [root / "LICENSE", *copies]:
                    path.write_bytes(license_bytes.replace(b"2031", b"2021"))
                with self.assertRaisesRegex(ValueError, "报告已过期"):
                    package.validate_license_documentation({"source_han_license_documents": result})
                self.assertIn("source_han_license_copyright", {item["kind"] for item in audit.source_han_license_documents_status()["failures"]})
                reset_documents()
                copies[0].unlink()
                self.assertIn("source_han_license_copy_missing", {item["kind"] for item in audit.source_han_license_documents_status()["failures"]})
                reset_documents()
                copies[0].write_bytes(license_bytes + b"stale")
                self.assertIn("source_han_license_copy_contents", {item["kind"] for item in audit.source_han_license_documents_status()["failures"]})
                reset_documents()
                (root / "NOTICE.md").write_text(notice.replace("2031", "2021"), encoding="utf-8")
                self.assertIn("source_han_notice_copyright", {item["kind"] for item in audit.source_han_license_documents_status()["failures"]})

    def test_repeated_variable_naming_keeps_fvar_and_stat_references(self):
        for italic in (False, True):
            font = variable_fixture()
            font["name"].setName("Weight", 256, 3, 1, 0x409)
            build.update_vf_names(font, "SC", italic)
            build.rebuild_stat(font, italic)
            build.remove_mac_name_records(font)
            build.prune_unreferenced_variable_names(font)
            before = {tag: font.getTableData(tag) for tag in ("fvar", "STAT", "name")}
            build.update_vf_names(font, "SC", italic)
            build.remove_mac_name_records(font)
            build.prune_unreferenced_variable_names(font)
            self.assertEqual({tag: font.getTableData(tag) for tag in before}, before)
            self.assertEqual(len(font["fvar"].instances), 6)

    def test_oversized_regular_bounds_fail_even_when_the_font_reports_them_truthfully(self):
        source_path = build.reference_font_path("SC", "Regular", False)
        if not source_path.is_file():
            self.skipTest("需要固定 Sarasa 来源")
        with TTFont(source_path) as font:
            self.assertTrue(audit.regular_vertical_bounds_status(font, source_path)["ok"])
            # A long vertical outline plus a correctly updated head still
            # violates the upstream default-instance padding requirement.
            from fontTools.pens.ttGlyphPen import TTGlyphPen
            pen = TTGlyphPen(None)
            top = font["head"].yMax + font["head"].unitsPerEm
            pen.moveTo((100, 0)); pen.lineTo((120, 0))
            pen.lineTo((120, top)); pen.lineTo((100, top)); pen.closePath()
            font["glyf"].glyphs[font.getBestCmap()[0xFE31]] = pen.glyph()
            build.recalculate_head_bounds(font)
            self.assertTrue(audit.head_bounds_status(font)["ok"])
            self.assertFalse(audit.regular_vertical_bounds_status(font, source_path)["ok"])
        self.assertFalse(audit.regular_vertical_bounds_status(fixture_font(), source_path.with_name("missing.ttf"))["ok"])

    def test_default_width_gate_rejects_a_tabular_space(self):
        if not build.INTER_UPRIGHT.is_file():
            self.skipTest("需要固定 Inter 来源")
        with TTFont(build.INTER_UPRIGHT) as font:
            self.assertTrue(audit.proportional_defaults_status(build.serialized_font_bytes(font), (400,))["ok"])
            space = font.getBestCmap()[0x20]
            tabular = build.get_single_substitution_mapping(font, "tnum")[space]
            build.remap_unicode_cmap(font, 0x20, tabular)
            result = audit.proportional_defaults_status(build.serialized_font_bytes(font), (400,))
            self.assertFalse(result["ok"])
            self.assertIn("U+0020", {failure["codepoint"] for failure in result["failures"]})

    def test_nonhinting_static_dropout_is_metadata_only(self):
        font = fixture_font()
        before = font.getTableData("glyf")
        build.normalize_static_raster_metadata(font, False)
        self.assertEqual(before, font.getTableData("glyf"))
        self.assertFalse(build.has_grid_fitting_tables(font))
        self.assertTrue(build.nonhinting_raster_metadata_status(font)["ok"])
        font["prep"].program.fromBytecode(build.SMART_DROPOUT_PREP + b"\xB0\x00\x21")
        self.assertTrue(build.has_grid_fitting_tables(font))
        self.assertFalse(build.nonhinting_raster_metadata_status(font)["ok"])

    def test_validation_record_identifies_the_loaded_code(self):
        expected = build.BUILDER_CODE_SHA256
        with patch.object(Path, "read_text", side_effect=AssertionError("must not attest to edited code on disk")):
            self.assertEqual(build.output_validation_contract(False)["builder_sha256"], expected)

    def test_regional_codepages_match_independent_upstream(self):
        for region in build.REGION_ORDER:
            path = build.reference_font_path(region, "Regular", False)
            if not path.exists():
                self.skipTest("需要固定上游地区字体")
            font = fixture_font()
            build.update_os2_sarasa_metadata(font, region)
            with TTFont(path, lazy=True) as source:
                self.assertEqual(font["OS/2"].ulCodePageRange1, source["OS/2"].ulCodePageRange1, region)
                self.assertEqual(audit.expected_codepage_range_1(region), source["OS/2"].ulCodePageRange1)

    def test_all_static_family_names_fit_without_shortening_weight_names(self):
        for region in build.REGION_ORDER:
            for stop in build.SOURCE_HAN_WEIGHT_STOPS:
                for italic in (False, True):
                    font = fixture_font()
                    build.update_static_names(font, region, str(stop["name"]), int(stop["value"]), italic, False)
                    self.assertTrue(build.family_name_length_status(font)["ok"])
                    self.assertEqual(font["name"].getDebugName(16), f"Sarasa Ui PD {region}")
                    if stop["name"] != "Regular":
                        self.assertIn(str(stop["name"]), font["name"].getDebugName(4))
                    self.assertIn("PropDigits", build.static_output_name(region, str(stop["name"]), italic))

    def test_variable_names_dropout_and_orphan_pruning_survive_roundtrip(self):
        font = variable_fixture()
        font["name"].setName("Weight", 256, 3, 1, 0x409)
        font["name"].setName("SourceHanSansSCVF-unused", 700, 3, 1, 0x409)
        build.update_vf_names(font, "SC", False)
        build.rebuild_stat(font, False)
        build.normalize_variable_raster_metadata(font)
        removed = build.prune_unreferenced_variable_names(font)
        self.assertIn(700, removed["unreferenced_variable_name_ids_removed"])
        self.assertNotIn(256, removed["unreferenced_variable_name_ids_removed"])
        for instance in font["fvar"].instances:
            self.assertIn("PD", font["name"].getDebugName(instance.postscriptNameID))
        self.assertTrue(build.family_name_length_status(font)["ok"])
        data = io.BytesIO(); font.save(data); data.seek(0)
        with TTFont(data) as actual:
            self.assertTrue(build.variable_raster_metadata_status(actual)["ok"])
            self.assertEqual(actual["prep"].program.getBytecode(), bytes.fromhex("B8 01 FF 85 B0 04 8D"))
            self.assertEqual(actual["gasp"].gaspRange, {0xFFFF: 0x000F})

    def test_family_limit_is_not_derived_from_the_current_family_constant(self):
        font = fixture_font()
        font["name"].setName("A" * 32, 1, 3, 1, 0x409)
        self.assertFalse(build.family_name_length_status(font)["ok"])

    def test_imports_never_bootstrap_or_restart_python(self):
        script = '''
import sys
sys.path.insert(0, "tools")
import python_env_bootstrap as bootstrap
def forbidden(*args, **kwargs):
    raise AssertionError("import attempted dependency preparation")
bootstrap.ensure_project_python = forbidden
bootstrap.isolated_tool_python = forbidden
import build_sarasa_ui_propdigits_sc
import audit_sarasa_ui_propdigits
import render_visual_checks
import check_external_release
import package_release
import check_quick_audit
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=build.ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_37_defaults_restore_inter_proportional_identities(self):
        if not build.INTER_UPRIGHT.exists():
            self.skipTest("需要固定 Inter 来源")
        with TTFont(build.INTER_UPRIGHT) as font:
            expected = {cp: font.getBestCmap()[cp] for cp in build.PROPDIGITS_CODEPOINTS}
            self.assertEqual(len(expected), 37)
            tnum = build.get_single_substitution_mapping(font, "tnum")
            for table in font["cmap"].tables:
                if table.isUnicode():
                    for cp, name in expected.items():
                        table.cmap[cp] = tnum[name]
            build.apply_static_propdigits(font)
            self.assertEqual({cp: font.getBestCmap()[cp] for cp in expected}, expected)
            build.bake_inter_ui_tnum_defaults(font)
            self.assertEqual({cp: font.getBestCmap()[cp] for cp in expected}, expected)
            font.flavor = None
            data = io.BytesIO(); font.save(data)
            runtime = hb.Font(hb.Face(data.getvalue()))
            def shape(cp, features):
                buffer = hb.Buffer(); buffer.add_codepoints([cp]); buffer.direction = "ltr"; buffer.script = "Latn"
                hb.shape(runtime, buffer, {"calt": False, "kern": False, **features})
                return [(item.codepoint, pos.x_advance) for item, pos in zip(buffer.glyph_infos, buffer.glyph_positions)]
            for cp, name in expected.items():
                self.assertEqual(shape(cp, {}), shape(cp, {"pnum": True}), f"U+{cp:04X}")
                self.assertEqual(shape(cp, {"tnum": True})[0][0], font.getGlyphID(tnum[name]), f"U+{cp:04X}")


if __name__ == "__main__":
    unittest.main()
