from pathlib import Path
import hashlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_sarasa_ui_propdigits as audit
import build_sarasa_ui_propdigits_sc as build
from fontTools.ttLib.tables.ttProgram import Program
from test_runtime_regressions import fixture_font


class NativeEllipsisHintPolicyTests(unittest.TestCase):
    def test_raster_coverage_requires_the_complete_declared_native_policy(self):
        policy = {"complete": True, "reference_construction": "fixed-upstream-instructions-with-native-none-fallback",
                  "prefix_glyphs_checked": 10, "source_fallback_glyphs": 2,
                  "fallback_classification": "independent-native-policy-instructions",
                  "control_codepoints": [f"U+{cp:04X}" for cp in build.UI_AUTOHINT_UNSCALED_CODEPOINTS],
                  **{key: "a" * 64 for key in ("source_pass1_sha256", "native_policy_source_sha256", "reference_sha256", "original_reference_sha256")}}
        raster = {"region": "SC", "weight": "Regular", "italic": False,
                  "comparison_mode": "official-hinted-native-fallback", "hint_policy_reference": policy}
        self.assertEqual(audit.instruction_raster_coverage_failures([], [raster]), [])
        for key, wrong in (("control_codepoints", ["U+0360"]), ("control_codepoints", []),
                           ("fallback_classification", "debug-text"), ("native_policy_source_sha256", "wrong")):
            changed = {**raster, "hint_policy_reference": {**policy, key: wrong}}
            self.assertTrue(audit.instruction_raster_coverage_failures([], [changed]))

    def test_declared_fallback_requires_empty_instructions(self):
        self.assertTrue(audit.ellipsis_instruction_presence_matches(True, "fullwidth", 0, 0))
        self.assertFalse(audit.ellipsis_instruction_presence_matches(True, "fullwidth", 12, 0))

    def test_hinted_source_cannot_waive_missing_instructions(self):
        self.assertFalse(audit.ellipsis_instruction_presence_matches(True, "fullwidth", 0, 12))
        self.assertTrue(audit.ellipsis_instruction_presence_matches(True, "fullwidth", 12, 12))
        with self.assertRaises(ValueError):
            audit.ellipsis_instruction_presence_matches(True, "fullwidth", 0)

    def test_other_roles_and_unhinted_fonts_keep_their_requirements(self):
        for role in ("proportional", "vertical"):
            self.assertFalse(audit.ellipsis_instruction_presence_matches(True, role, 0, 0))
            self.assertTrue(audit.ellipsis_instruction_presence_matches(True, role, 12, 0))
        for role in ("proportional", "fullwidth", "vertical"):
            self.assertFalse(audit.ellipsis_instruction_presence_matches(False, role, 12))
            self.assertTrue(audit.ellipsis_instruction_presence_matches(False, role, 0))

    def test_expectation_uses_raw_source_output_instead_of_target_or_assumed_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fragments/SC-Regular/pass1.ttf"
            source.parent.mkdir(parents=True)
            font = fixture_font()
            for cmap in font["cmap"].tables:
                if cmap.isUnicode(): cmap.cmap[0x22EF] = "G"
            program = Program(); program.fromBytecode([0x00])
            font["glyf"]["G"].program = program
            font.save(source)
            calls = []
            def native_tool(arguments):
                self.assertEqual(Path(arguments[-2]), source)
                self.assertIn("--control-file", arguments)
                self.assertTrue(all(option in arguments for option in build.UI_AUTOHINT_OPTIONS))
                calls.append(arguments)
                shutil.copyfile(source, arguments[-1])
            audit.native_ellipsis_instruction_expectation.cache_clear()
            with patch.object(build, "static_hint_work_dir", return_value=("key", root)), \
                 patch.object(build, "BUILD_CACHE_DIR", root), \
                 patch.object(build, "ui_hint_control", return_value="1 none dflt\n"), \
                 patch.object(build, "run_ttfautohint", side_effect=native_tool), \
                 patch.object(build, "ttfautohint_identity", return_value={"sha256": "test-tool"}):
                result = audit.native_ellipsis_instruction_expectation("SC", "Regular", False)
            audit.native_ellipsis_instruction_expectation.cache_clear()
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["instruction_bytes"], 1)
            self.assertEqual(result["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
