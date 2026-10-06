from __future__ import annotations

import io
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import audit_sarasa_ui_propdigits as audit
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.ttProgram import Program
from test_runtime_regressions import fixture_font
import uharfbuzz as hb


class ContextualIntegrity(unittest.TestCase):
    def font(self):
        path = audit.static_path("SC", "Regular", False, False)
        native_inter = b.SARASA_SOURCE_DIR / "sources" / "Inter" / "Inter-Regular.ttf"
        if not path.exists() or not b.INTER_UPRIGHT.exists() or not native_inter.exists():
            self.skipTest("需要固定来源及已构建的 SC 字体夹具")
        font = TTFont(path, recalcTimestamp=False)
        b.apply_static_propdigits(font)
        b.add_digit_colon_feature(font)
        b.update_static_names(font, "SC", "Regular", 400, False, False)
        b.update_os2_sarasa_metadata(font, "SC")
        self.addCleanup(font.close)
        return font

    def test_mixed_context_and_shared_substitution_survive(self):
        font = self.font(); cmap = font.getBestCmap()
        child = b.ot.Lookup(); child.LookupType = 1; child.LookupFlag = 0
        single = b.ot.SingleSubst(); single.mapping = {cmap[ord("A")]: cmap[ord("B")]}
        child.SubTable = [single]; child.SubTableCount = 1
        child_index = b.append_gsub_lookup(font, child)
        b.append_gsub_feature(font, "zzzz", [child_index])
        rule = b.ot.ChainContextSubst(); rule.Format = 3
        rule.InputCoverage = [b.coverage(font, [cmap[ord("A")]])]; rule.InputGlyphCount = 1
        rule.BacktrackCoverage = [b.coverage(font, [cmap[ord(" ")]])]; rule.BacktrackGlyphCount = 1
        rule.LookAheadCoverage = [b.coverage(font, [cmap[ord("!")]])]; rule.LookAheadGlyphCount = 1
        record = b.ot.SubstLookupRecord(); record.SequenceIndex = 0; record.LookupListIndex = child_index
        rule.SubstLookupRecord = [record]; rule.SubstCount = 1
        parent = next(font["GSUB"].table.LookupList.Lookup[index] for index in b.feature_lookup_indices(font, "GSUB", {"calt"})
                      if font["GSUB"].table.LookupList.Lookup[index].LookupType == 6)
        parent.SubTable.append(rule); parent.SubTableCount += 1
        b.add_digit_colon_feature(font)
        self.assertEqual(b.get_single_substitution_mapping(font, "zzzz"), single.mapping)
        data = io.BytesIO(); font.save(data)
        runtime = hb.Font(hb.Face(data.getvalue()))
        buffer = hb.Buffer(); buffer.add_str(" A!"); buffer.guess_segment_properties()
        hb.shape(runtime, buffer, {"calt": True})
        self.assertEqual(buffer.glyph_infos[1].codepoint, font.getGlyphID(cmap[ord("B")]))
        before = font.getTableData("GSUB")
        b.add_digit_colon_feature(font)
        self.assertEqual(before, font.getTableData("GSUB"))

    def test_missing_non_colon_rule_fails_structure_check(self):
        font = self.font()
        at = font.getBestCmap()[ord("@")]
        self.assertTrue(b.contextual_case_structure_status(font)["ok"])
        for index in b.feature_lookup_indices(font, "GSUB", {"calt"}):
            lookup = font["GSUB"].table.LookupList.Lookup[index]
            if lookup.LookupType != 6:
                continue
            for rule in list(lookup.SubTable):
                if getattr(rule, "Format", None) == 3 and getattr(rule, "SubstCount", 0) and at in rule.InputCoverage[0].glyphs:
                    lookup.SubTable.remove(rule); lookup.SubTableCount -= 1
                    self.assertFalse(b.contextual_case_structure_status(font)["ok"])
                    return
        self.fail("未找到应保留的非冒号上下文规则")

    def test_class_context_keeps_unrelated_members_of_a_shared_class(self):
        font = self.font(); cmap = font.getBestCmap()
        colon = cmap[0x3A]; letter = cmap[ord("A")]; replacement = cmap[ord("B")]
        raised = b.get_single_substitution_mapping(font, "case")[colon]
        child = b.ot.Lookup(); child.LookupType = 1; child.LookupFlag = 0
        mapping = b.ot.SingleSubst(); mapping.mapping = {colon: raised, letter: replacement}
        child.SubTable = [mapping]; child.SubTableCount = 1
        child_index = b.append_gsub_lookup(font, child)
        record = b.ot.SubstLookupRecord(); record.SequenceIndex = 0; record.LookupListIndex = child_index
        rule = b.ot.ChainSubClassRule()
        rule.Backtrack = [1]; rule.BacktrackGlyphCount = 1
        rule.Input = []; rule.InputGlyphCount = 1
        rule.LookAhead = [1]; rule.LookAheadGlyphCount = 1
        rule.SubstLookupRecord = [record]; rule.SubstCount = 1
        rules = b.ot.ChainSubClassSet(); rules.ChainSubClassRule = [rule]; rules.ChainSubClassRuleCount = 1
        subtable = b.ot.ChainContextSubst(); subtable.Format = 2
        subtable.Coverage = b.coverage(font, [colon, letter])
        for name, values in (("InputClassDef", {colon: 1, letter: 1}),
                             ("BacktrackClassDef", {cmap[ord(" ")]: 1}),
                             ("LookAheadClassDef", {cmap[ord("!")]: 1})):
            definition = b.ot.ClassDef(); definition.classDefs = values
            setattr(subtable, name, definition)
        subtable.ChainSubClassSet = [None, rules]; subtable.ChainSubClassSetCount = 2
        parent = b.ot.Lookup(); parent.LookupType = 6; parent.LookupFlag = 0
        parent.SubTable = [subtable]; parent.SubTableCount = 1
        parent_index = b.append_gsub_lookup(font, parent)
        b.merge_gsub_lookup_indices_into_features(font, "calt", [parent_index])
        b.add_digit_colon_feature(font)
        data = io.BytesIO(); font.save(data)
        runtime = hb.Font(hb.Face(data.getvalue()))
        buffer = hb.Buffer(); buffer.add_str(" A!"); buffer.guess_segment_properties()
        hb.shape(runtime, buffer, {"calt": True})
        self.assertEqual(buffer.glyph_infos[1].codepoint, font.getGlyphID(replacement))
        self.assertTrue(b.contextual_case_structure_status(font)["ok"])

    def test_colon_guards_and_width_alternates_are_audited(self):
        font = self.font()
        self.assertGreaterEqual(len(audit.raised_colon_glyph_ids(font)), 2)
        for char in "ķļņŗșț":
            self.assertIn(char + ":2", audit.COLON_TEXTS)
            self.assertIn("1:" + char, audit.COLON_TEXTS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"; font.save(path)
            self.assertTrue(audit.colon_status(path)["ok"])
            self.assertTrue(audit.contextual_calt_status(path, "SC", "Regular", False)["ok"])

    def test_incremental_record_rejects_changed_bytes_and_style(self):
        font = self.font()
        b.install_continuous_vertical_dash_layout(font)
        with tempfile.TemporaryDirectory() as directory, patch.object(b, "OUTPUT_VALIDATION_DIR", Path(directory) / "cache"):
            path = Path(directory) / "candidate.ttf"; font.save(path)
            b.record_verified_output(path, "SC", False, "Regular", False)
            self.assertTrue(b.verified_output_record_status(path, "SC", False, "Regular", False)[0])
            self.assertFalse(b.verified_output_record_status(path, "SC", True, "Regular", False)[0])
            font["GSUB"].table.LookupList.Lookup[0].LookupFlag ^= 2
            font.save(path)
            self.assertFalse(b.verified_output_record_status(path, "SC", False, "Regular", False)[0])

    def test_style_checks_include_angle_flags_and_names(self):
        font = self.font()
        self.assertTrue(b.output_identity_status(font, "SC", False, "Regular", False)["ok"])
        self.assertFalse(b.output_identity_status(font, "SC", True, "Regular", False)["ok"])
        font["post"].italicAngle = -9.4
        self.assertFalse(b.output_identity_status(font, "SC", False, "Regular", False)["ok"])
        b.update_static_names(font, "SC", "Regular", 400, True, False)
        b.update_os2_sarasa_metadata(font, "SC")
        self.assertIn("H outline does not match the fixed Inter upright/italic style",
                      b.output_identity_status(font, "SC", True, "Regular", False)["reasons"])

    def test_copyright_matches_the_pinned_source(self):
        path = b.source_han_vf_path("SC")
        if not path.exists():
            self.skipTest("需要固定 Source Han 来源")
        with TTFont(path, lazy=True) as source:
            self.assertIn(source["name"].getDebugName(0), b.project_copyright("SC"))


class NativeHintReferenceIntegrity(unittest.TestCase):
    def fonts(self):
        source = fixture_font()
        for table in source["cmap"].tables:
            if table.isUnicode():
                table.cmap.update({ord(char): "G" for char in audit.string.ascii_letters + audit.string.digits})
                table.cmap[0x0360] = "acutecomb"
        for glyph in source["glyf"].glyphs.values():
            glyph.program = Program()
            glyph.program.fromBytecode([0xB0, 0, 0x21])
        reference = copy.deepcopy(source)
        default_hinted = copy.deepcopy(source)
        for font in (source, reference, default_hinted):
            self.addCleanup(font.close)
        return source, reference, default_hinted

    def test_equal_outlines_do_not_clear_another_glyphs_instructions(self):
        source, reference, default_hinted = self.fonts()
        original = bytes(reference["glyf"]["G"].program.getBytecode())
        reference["glyf"]["T"].program.fromBytecode([0xB0, 1, 0x21])
        affected, report = audit.apply_native_hint_reference_policy(
            source, reference, default_hinted, {source.getGlyphID("Galt"), source.getGlyphID("T")})
        self.assertEqual(bytes(reference["glyf"]["G"].program.getBytecode()), original)
        self.assertEqual(bytes(reference["glyf"]["Galt"].program.getBytecode()), b"")
        self.assertEqual(bytes(reference["glyf"]["acutecomb"].program.getBytecode()), b"")
        self.assertEqual(bytes(reference["glyf"]["T"].program.getBytecode()), bytes([0xB0, 1, 0x21]))
        self.assertEqual(report["upstream_replaced_instructions_preserved"], 1)
        self.assertIn(0x0360, affected)

    def test_changed_prefix_or_normal_latin_fallback_is_rejected(self):
        source, reference, default_hinted = self.fonts()
        with self.assertRaisesRegex(ValueError, "正常拉丁"):
            audit.apply_native_hint_reference_policy(source, reference, default_hinted, {source.getGlyphID("G")})
        reference["glyf"]["G"].coordinates[0] = (1, 0)
        with self.assertRaisesRegex(ValueError, "顺序或轮廓"):
            audit.apply_native_hint_reference_policy(source, reference, default_hinted, {source.getGlyphID("Galt")})

    def test_missing_policy_evidence_fails_raster_coverage(self):
        self.assertTrue(audit.instruction_raster_coverage_failures([], [
            {"region": "SC", "weight": "ExtraLight", "italic": False, "counts": {}}
        ]))


class HinterIdentity(unittest.TestCase):
    def test_fallback_policy_preserves_latin_and_digit_grid_fitting(self):
        import freetype
        source = b.SARASA_SOURCE_DIR / "sources/Inter/Inter-ExtraLight.ttf"
        if not source.exists():
            self.skipTest("需要固定 Inter 静态来源")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            baseline = work / "baseline.ttf"
            actual = work / "actual.ttf"
            control = work / "control.txt"
            control.write_text(b.ui_hint_control(source), encoding="ascii")
            b.run_ttfautohint([str(source), str(baseline)])
            b.run_ttfautohint([*b.UI_AUTOHINT_OPTIONS, "--control-file", str(control), str(source), str(actual)])
            expected_face = freetype.Face(str(baseline))
            actual_face = freetype.Face(str(actual))
            for ppem in (9, 12, 16):
                expected_face.set_pixel_sizes(0, ppem)
                actual_face.set_pixel_sizes(0, ppem)
                for cp in map(ord, "Haxy0123456789"):
                    self.assertEqual(audit.freetype_render_signature(expected_face, cp),
                                     audit.freetype_render_signature(actual_face, cp),
                                     f"U+{cp:04X} at {ppem}px lost its normal hinting")

    def test_hint_gate_rejects_missing_execution_coverage(self):
        self.assertTrue(audit.hint_execution_coverage_failures([]))
        records = [{"region": region, "weight": weight, "italic": italic,
                    "native_hint_execution": {"complete": True, "glyphs": 1, "loads_checked": 0}}
                   for region in b.REGION_ORDER for weight in audit.EXPECTED_WEIGHTS for italic in (False, True)]
        self.assertTrue(audit.hint_execution_coverage_failures(records))

    def test_replacing_an_executable_in_place_changes_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ttfautohint.exe"; path.write_bytes(b"first executable")
            stamp = path.stat().st_mtime_ns
            with patch.object(b, "selected_ttfautohint", return_value=(path, "external")), patch.object(b, "ttfautohint_version", return_value="same version"):
                first = b.ttfautohint_identity()
                path.write_bytes(b"second executable"); os.utime(path, ns=(stamp, stamp))
                second = b.ttfautohint_identity()
            self.assertNotEqual(first["sha256"], second["sha256"])

    def test_hinter_changes_invalidate_work_and_pass1_but_not_cjk_analysis(self):
        with patch.object(Path, "exists", return_value=True), patch.object(b, "file_sha256", return_value="fixed source"), patch.object(b, "stable_sfnt_fingerprint", return_value="fixed sfnt"), patch.object(b, "ttfautohint_identity") as identity:
            def keys():
                jobs = [(Path("input.ttf"), Path("output.hint.gz"), "Heavy")]
                return (b.static_hint_work_key("Heavy"), b.static_hint_group_cache_key("Heavy", "pass1", jobs), b.static_hint_group_cache_key("Heavy", "fe", jobs))
            identity.return_value = {"sha256": "first"}; first = keys()
            identity.return_value = {"sha256": "second"}; second = keys()
            self.assertNotEqual(first[0], second[0])
            self.assertNotEqual(first[1], second[1])
            self.assertEqual(first[2], second[2])


if __name__ == "__main__":
    unittest.main()
