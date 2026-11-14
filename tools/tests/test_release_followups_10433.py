"""Regressions for the v1.0.43.3 fixes (the 2026-11-14 external audit)."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b  # noqa: E402
import package_release as package  # noqa: E402
import python_env_bootstrap as bootstrap  # noqa: E402
import uharfbuzz as hb  # noqa: E402
from font_audit import contract as audit, differential, pipeline, products  # noqa: E402
from font_audit.shaping import Face  # noqa: E402
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402
from fontTools.ttLib.tables import otTables as ot  # noqa: E402
from fontTools.ttLib.tables.TupleVariation import TupleVariation  # noqa: E402
from fontTools.ttLib.tables.ttProgram import Program  # noqa: E402
from test_release_runtime_controls import add_metric_tables, variable_fixture  # noqa: E402
from test_runtime_regressions import fixture_font  # noqa: E402
from test_vertical_segments import dash_fixture  # noqa: E402


def reloaded(font):
    stream = io.BytesIO(); font.save(stream); stream.seek(0)
    return TTFont(stream)


class EntriesAndEnvironment(unittest.TestCase):
    def test_a_retired_switch_set_to_zero_passes_and_any_other_value_stops_every_entry(self):
        with patch.dict(os.environ, {"SARASA_SKIP_TTFAUTOHINT": "0", "SARASA_SKIP_CHLOROPHYTUM": ""}):
            self.assertFalse(b.retired_switch_set("SARASA_SKIP_TTFAUTOHINT"))
            b.reject_retired_switches()
        # SARASA_SKIP_TTFAUTOHINT=0 used to pass the entry and stop the hint step.
        with patch.dict(os.environ, {"SARASA_SKIP_TTFAUTOHINT": "1"}):
            for options in (["--refresh-variable-finalization-only"], ["--refresh-static-finalization-only"],
                            ["--refresh-layout-only"], ["--refresh-version-from", "1.0.43.2"]):
                with patch.object(sys, "argv", ["build", *options]), \
                        patch.object(b, "refresh_variable_finalization_outputs") as variable, \
                        patch.object(b, "refresh_static_finalization_outputs") as static, \
                        patch.object(b, "refresh_layout_outputs") as layout, \
                        patch.object(b, "refresh_version_metadata_outputs") as version:
                    with self.assertRaisesRegex(RuntimeError, "SARASA_SKIP_TTFAUTOHINT"):
                        b.main()
                    for entry in (variable, static, layout, version):
                        entry.assert_not_called()

    def test_the_python_pin_is_checked_before_any_environment_is_made(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv"
            with patch.dict(os.environ, {"SARASA_PYTHON_VENV": str(target)}), \
                    patch.object(bootstrap, "PINNED_PYTHON", (3, 12, 0)), \
                    patch.object(bootstrap.venv, "EnvBuilder") as builder:
                with self.assertRaisesRegex(RuntimeError, "3.12.0"):
                    bootstrap.ensure_project_python({}, project_root=Path(directory), label="test")
                builder.assert_not_called()
            self.assertFalse(target.exists())

    def test_an_environment_another_python_made_is_named_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv"
            python = bootstrap.venv_python(target)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
            (target / "pyvenv.cfg").write_text("home = x\nversion = 3.12.4\n", encoding="utf-8")
            self.assertEqual(bootstrap.venv_python_version(target), (3, 12, 4))
            with patch.dict(os.environ, {"SARASA_PYTHON_VENV": str(target)}), \
                    patch.object(bootstrap.subprocess, "call") as call:
                with self.assertRaisesRegex(RuntimeError, "3.12.4"):
                    bootstrap.ensure_project_python({}, project_root=Path(directory), label="test")
                call.assert_not_called()


class LayoutTableFixes(unittest.TestCase):
    def test_dropping_a_feature_renumbers_or_clears_the_required_feature(self):
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, """
            languagesystem DFLT dflt;
            feature aalt { sub G by Galt; } aalt;
            feature ccmp { sub T by o; } ccmp;
            feature salt { sub G by Galt; } salt;
        """, tables=["GSUB"])
        table = font["GSUB"]
        records = table.table.FeatureList.FeatureRecord
        langsys = table.table.ScriptList.ScriptRecord[0].Script.DefaultLangSys
        salt = next(index for index, record in enumerate(records) if record.FeatureTag == "salt")
        langsys.ReqFeatureIndex = salt
        b.drop_feature_records(table, {"aalt"})
        records = table.table.FeatureList.FeatureRecord
        self.assertEqual(records[langsys.ReqFeatureIndex].FeatureTag, "salt")
        b.drop_feature_records(table, {"salt"})
        self.assertEqual(langsys.ReqFeatureIndex, 0xFFFF)

    def test_single_pos_sync_keeps_both_value_formats_and_per_glyph_values(self):
        font, reference = fixture_font(), fixture_font()

        def value(**fields):
            record = ot.ValueRecord()
            for name, number in fields.items():
                setattr(record, name, number)
            return record

        def subtable(fmt, glyphs, value_format, values):
            part = ot.SinglePos(); part.Format = fmt
            part.Coverage = ot.Coverage(); part.Coverage.glyphs = list(glyphs)
            part.ValueFormat = value_format
            part.Value = values if fmt == 2 else values[0]
            if fmt == 2:
                part.ValueCount = len(values)
            return part

        # One shared value cannot hold the reference value for one glyph and
        # the lookup's own for a glyph the reference lacks.
        shared = subtable(1, ["G", "T"], 0x0001, [value(XPlacement=5)])
        source = subtable(1, ["G"], 0x0001, [value(XPlacement=7)])
        report = b.sync_single_pos_values_from_reference(shared, source, font, reference)
        self.assertEqual(report["single_pos_shared_values_expanded"], 1)
        self.assertEqual(shared.Format, 2)
        self.assertEqual([item.XPlacement for item in shared.Value], [7, 5])
        # Different value formats: the fields of both stay.
        wide = subtable(2, ["G", "T"], 0x0001, [value(XPlacement=5), value(XPlacement=6)])
        source = subtable(2, ["G", "T"], 0x0004, [value(XAdvance=10), value(XAdvance=20)])
        b.sync_single_pos_values_from_reference(wide, source, font, reference)
        self.assertEqual([item.XAdvance for item in wide.Value], [10, 20])
        self.assertEqual(wide.ValueFormat, 0x0005)
        # A glyph the reference lacks keeps its own fields.
        own = subtable(2, ["G", "T"], 0x0009, [value(XPlacement=1, YAdvance=2), value(XPlacement=3, YAdvance=4)])
        partial = subtable(2, ["G"], 0x0001, [value(XPlacement=7)])
        b.sync_single_pos_values_from_reference(own, partial, font, reference)
        self.assertEqual(own.ValueFormat, 0x0009)
        self.assertEqual([(item.XPlacement, item.YAdvance) for item in own.Value], [(7, 0), (3, 4)])

    def test_a_legacy_vertical_dash_pair_position_is_found(self):
        font = dash_fixture()
        self.assertEqual(b.vertical_em_dash_positioning_records(font), {"vert": [], "vrt2": []})
        # The legacy rule moved the second dash of a pair.
        addOpenTypeFeaturesFromString(font, "languagesystem DFLT dflt;\n"
                                      "feature vert { pos vertical <0 0 0 0> vertical <0 10 0 0>; } vert;", tables=["GPOS"])
        found = b.vertical_em_dash_positioning_records(reloaded(font))
        self.assertEqual([row["pair"] for row in found["vert"]], [["vertical", "vertical"]])


class RasterAndGlyphData(unittest.TestCase):
    def test_force_recompile_counts_the_glyphs_it_expands(self):
        font = reloaded(fixture_font())
        self.assertEqual(b.force_recompile_glyf(font)["glyf_glyphs_forced_to_recompile"], len(font.getGlyphOrder()))
        self.assertEqual(b.force_recompile_glyf(font)["glyf_glyphs_forced_to_recompile"], 0)

    def test_nonhinting_status_reads_the_glyph_programs_themselves(self):
        font = fixture_font()
        b.normalize_nonhinting_raster_metadata(font)
        self.assertTrue(b.nonhinting_raster_metadata_status(font)["ok"])
        glyph = font["glyf"]["G"]
        glyph.program = Program(); glyph.program.fromBytecode(b"\xb0\x01")
        self.assertFalse(b.nonhinting_raster_metadata_status(font)["ok"])
        self.assertEqual(b.instructed_glyph_count(font), 1)


class VariableSideBearings(unittest.TestCase):
    def test_hvar_side_bearings_follow_an_outline_moved_after_the_metric_tables(self):
        font = variable_fixture()
        add_metric_tables(font)
        # A later step moves G's outline 10 units right at the heaviest weight.
        count = b.gvar_coordinate_count(font, "G")
        font["gvar"].variations["G"] = [TupleVariation({"wght": (0.0, 1.0, 1.0)},
                                                       [(10, 0)] * (count - 4) + [(0, 0)] * 4)]
        font = reloaded(font)
        before = hb.Font(hb.Face(b.serialized_font_bytes(font)))
        before.set_variations({"wght": 900})
        advance = before.get_glyph_h_advance(font.getGlyphID("G"))
        mismatches = b.hvar_side_bearing_mismatches(font)
        self.assertIn("G", {row[0] for row in mismatches})
        report = b.sync_hvar_side_bearings_to_outlines(font)
        self.assertGreater(report["hvar_side_bearing_rows_synced"], 0)
        font = reloaded(font)
        self.assertEqual(b.hvar_side_bearing_mismatches(font), [])
        after = hb.Font(hb.Face(b.serialized_font_bytes(font)))
        after.set_variations({"wght": 900})
        self.assertEqual(after.get_glyph_h_advance(font.getGlyphID("G")), advance)
        self.assertEqual(b.sync_hvar_side_bearings_to_outlines(font), {"hvar_side_bearing_rows_synced": 0})


class VerticalRoutes(unittest.TestCase):
    def test_the_final_vertical_routes_are_compared_by_what_they_do(self):
        font, static = dash_fixture(), dash_fixture()
        self.assertEqual(b.vertical_route_runtime_differences(font, static), [])
        for lookup in static["GSUB"].table.LookupList.Lookup:
            for part in lookup.SubTable:
                mapping = getattr(part, "mapping", None)
                if lookup.LookupType == 1 and mapping and mapping.get("full") == "vertical":
                    mapping["full"] = "G"
        problems = b.vertical_route_runtime_differences(font, static)
        self.assertTrue(any("U+2015" in problem for problem in problems), problems)


class Provenance(unittest.TestCase):
    def record(self, directory, path, builder, components, entry="build"):
        with patch.object(b, "OUTPUT_VALIDATION_DIR", Path(directory) / "records"):
            record_path = b.output_validation_record_path(path)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps({"provenance": {"entry": entry}, "contract": {
                "builder_sha256": builder, "build_components_sha256": components}}), encoding="utf-8")
            return b.output_record_provenance(path)

    def test_only_a_full_build_by_this_code_counts_as_built_by_current_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            current = self.record(directory, path, b.BUILDER_CODE_SHA256, b.BUILD_COMPONENT_SHA256)
            self.assertTrue(current["built_by_current_code"])
            other = self.record(directory, path, "0" * 64, b.BUILD_COMPONENT_SHA256)
            self.assertFalse(other["built_by_current_code"])
            refreshed = self.record(directory, path, b.BUILDER_CODE_SHA256, b.BUILD_COMPONENT_SHA256,
                                    entry="refresh-variable-finalization")
            self.assertFalse(refreshed["built_by_current_code"])

    def test_the_packager_refuses_fonts_a_full_build_of_this_code_did_not_make(self):
        name = "fonts/variable/a.ttf"

        def report(**item):
            return {"version": b.VERSION, "complete_font_matrix": True, "static_outputs": [],
                    "variable_outputs": [{"file": name, **item}]}
        good = {"provenance": {"built_by_current_code": True, "chain": [{"builder_sha256": b.BUILDER_CODE_SHA256}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            path = root / "reports" / "Sarasa-Ui-PropDigits-report.json"
            with patch.object(package, "ROOT", root):
                path.write_text(json.dumps(report(**good)), encoding="utf-8")
                package.validate_build_report({name})
                for item in ({**good, "refreshed_by": ["refresh-static-finalization"]},
                             {"provenance": {"built_by_current_code": True, "chain": [{"builder_sha256": "0" * 64}]}},
                             {"provenance": {"built_by_current_code": False, "chain": [{}]}}):
                    path.write_text(json.dumps(report(**item)), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "完整构建"):
                        package.validate_build_report({name})


class AuditCoverage(unittest.TestCase):
    def test_a_crashed_unit_reports_no_workstation_path(self):
        error = FileNotFoundError(2, "No such file", str(audit.WORK / "vf-sources" / "x.ttf"))
        self.assertNotIn(str(audit.WORK), pipeline.portable_error(error))
        self.assertIn("$SOURCES/x.ttf", pipeline.portable_error(error))
        text = pipeline.portable_error(ValueError(r"read C:\Users\Someone\AppData\Local\Temp\a.ttf failed"))
        self.assertNotIn("Someone", text)

    def test_vert_off_contexts_and_the_new_probes(self):
        context = differential.Context(direction="ttb", feature="-vert", repertoire="vertical-forms")
        self.assertEqual(context.arguments()["features"], {"vert": False})
        for codepoint in (0x2014, 0xFE31, 0x3001, 0xFF01, 0x2E3A):
            self.assertIn(codepoint, differential.VERTICAL_FORM_REPERTOIRE)
        self.assertTrue(any(item.feature == "-vert" for item in differential.default_contexts()))
        for text in ("\ufe31\ufe31", "1/2", "1a"):
            self.assertIn(text, differential.RUNS)
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, "languagesystem DFLT dflt;\nfeature salt { sub G by Galt; } salt;",
                                      tables=["GSUB"])
        self.assertEqual(differential.optional_feature_bases(reloaded(font)), {ord("G")})

    def test_a_joined_run_is_explained_only_by_its_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.ttf"; dash_fixture().save(good)
            face = Face(good)
            run = face.shape("——", direction="ttb", script="Hani")
            self.assertEqual(products.joined_segment_run_reasons(face, run, 2, italic=False, strict=True), [])
            font = dash_fixture()
            segments = b.vertical_dash_segments(font, b.upstream_dash_roles(font))
            glyph = font["glyf"][segments["begin"]]
            reversed_glyph = b.make_polygon_glyph(list(reversed(list(glyph.coordinates))))
            reversed_glyph.recalcBounds(font["glyf"])
            font["glyf"].glyphs[segments["begin"]] = reversed_glyph
            bad = Path(directory) / "bad.ttf"; font.save(bad)
            face = Face(bad)
            run = face.shape("——", direction="ttb", script="Hani")
            self.assertIn("segment outline is counter-clockwise",
                          products.joined_segment_run_reasons(face, run, 2, italic=False, strict=True))

    def test_refresh_field_protection_names_what_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before.ttf"; after = Path(directory) / "after.ttf"
            font = fixture_font(); font.save(before)
            changed = copy.deepcopy(font)
            changed["head"].fontRevision = 9.0
            changed["OS/2"].usWeightClass = 900
            changed["OS/2"].achVendID = "TEST"
            changed.save(after)
            self.assertEqual(b.changed_table_fields(before, after, "head", b.HEAD_REFRESH_FIELDS), [])
            self.assertEqual(b.changed_table_fields(before, after, "OS/2", b.OS2_REFRESH_FIELDS), ["OS/2.usWeightClass"])


if __name__ == "__main__":
    unittest.main()
