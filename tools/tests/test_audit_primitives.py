from __future__ import annotations

import struct
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import build_sarasa_ui_propdigits_sc as build  # noqa: E402
import audit_sarasa_ui_propdigits as audit  # noqa: E402
import package_release  # noqa: E402
import uharfbuzz as hb  # noqa: E402
from fontTools.ttLib import TTFont  # noqa: E402
from fontTools.varLib import builder as var_builder  # noqa: E402


def simple_glyph_data(point_count: int, flags: bytes) -> bytes:
    header = struct.pack(">hhhhh", 1, 0, 0, 0, 0)
    end_points = struct.pack(">H", point_count - 1)
    instructions = struct.pack(">H", 0)
    return header + end_points + instructions + flags


class RawSimpleGlyphFlagTests(unittest.TestCase):
    def test_first_overlap_flag_repeat_is_valid(self) -> None:
        data = simple_glyph_data(
            3,
            bytes(
                [
                    build.glyf_table.flagOverlapSimple
                    | build.glyf_table.flagRepeat,
                    2,
                ]
            ),
        )
        stats = build.parse_raw_simple_glyph_flags(data)
        self.assertEqual(stats["overlap_point_flags"], 3)
        self.assertEqual(stats["invalid_explicit_overlap_flags"], 0)

    def test_later_stored_overlap_flag_is_invalid(self) -> None:
        data = simple_glyph_data(
            2,
            bytes([build.glyf_table.flagOnCurve, build.glyf_table.flagOverlapSimple]),
        )
        stats = build.parse_raw_simple_glyph_flags(data)
        self.assertEqual(stats["overlap_point_flags"], 1)
        self.assertEqual(stats["invalid_explicit_overlap_flags"], 1)

    def test_truncated_repeat_fails_closed(self) -> None:
        data = simple_glyph_data(
            2,
            bytes([build.glyf_table.flagOnCurve | build.glyf_table.flagRepeat]),
        )
        with self.assertRaisesRegex(ValueError, "flag repeat"):
            build.parse_raw_simple_glyph_flags(data)

    def test_result_survives_fonttools_glyph_expansion(self) -> None:
        path = (
            ROOT
            / "fonts"
            / "static"
            / f"SarasaUiPropDigitsSC-TTF-{build.VERSION}"
            / "SarasaUiPropDigitsSC-Regular.ttf"
        )
        if not path.exists():
            self.skipTest("built SC Regular font is not available")
        font = TTFont(path, lazy=False, recalcTimestamp=False)
        try:
            before = build.raw_simple_glyph_flag_stats(font)
            glyf = font["glyf"]
            for glyph_name in font.getGlyphOrder():
                glyf[glyph_name].expand(glyf)
            after = build.raw_simple_glyph_flag_stats(font)
        finally:
            font.close()
        self.assertGreater(before["glyphs_checked"], 0)
        self.assertEqual(after, before)


class AuditGateTests(unittest.TestCase):
    def test_observations_never_hide_nonzero_failure_counts(self) -> None:
        observed = {
            "kind": "source-quantization",
            "counts": {"outline": 0},
            "observations": {"fixed_point": 12},
        }
        self.assertEqual(audit.nonzero([observed]), [])
        observed["counts"]["outline"] = 1
        failures = audit.nonzero([observed])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["counts"]["outline"], 1)
        self.assertEqual(failures[0]["observations"]["fixed_point"], 12)

    def test_nonzero_rejects_explicit_false_without_counts(self) -> None:
        self.assertEqual(
            audit.nonzero([{"kind": "contract", "ok": False, "counts": {}}]),
            [
                {
                    "kind": "contract",
                    "region": None,
                    "weight": None,
                    "wght": None,
                    "italic": None,
                    "hinted": None,
                    "role": None,
                    "target": None,
                    "source": None,
                    "missing": False,
                    "ok": False,
                    "counts": {},
                    "coverage": {},
                    "observations": {},
                    "classical_source_exception": None,
                    "samples": {},
                    "observation_samples": {},
                }
            ],
        )

    def test_registered_failure_fails_gate(self) -> None:
        counts = {name: 0 for name in audit.AUDIT_GATE_SECTIONS}
        executed = {name: True for name in audit.AUDIT_GATE_SECTIONS}
        counts["vf_engine_metric"] = 1
        gate = audit.build_audit_gate(counts, executed)
        self.assertEqual(gate["total_failures"], 1)
        self.assertEqual(gate["sections"]["vf_engine_metric"]["status"], "failed")
        self.assertFalse(gate["passed"])

    def test_missing_registry_entry_fails_closed(self) -> None:
        counts = {name: 0 for name in audit.AUDIT_GATE_SECTIONS}
        executed = {name: True for name in audit.AUDIT_GATE_SECTIONS}
        counts.pop("metadata")
        with self.assertRaisesRegex(ValueError, "registry mismatch"):
            audit.build_audit_gate(counts, executed)

    def test_skipped_section_cannot_be_reported_as_passed(self) -> None:
        counts = {name: 0 for name in audit.AUDIT_GATE_SECTIONS}
        executed = {name: True for name in audit.AUDIT_GATE_SECTIONS}
        executed["static_raster"] = False
        gate = audit.build_audit_gate(counts, executed)
        self.assertFalse(gate["complete"])
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["skipped_sections"], 1)


class AuditMetricObservationTests(unittest.TestCase):
    @staticmethod
    def metric_case(
        ordinary_horizontal: int = 1,
        component_horizontal: int = 0,
    ) -> dict[str, object]:
        return {
            "region": "SC",
            "italic": True,
            "wght": 300,
            "counts": {
                "missing_target": 0,
                "missing_reference": 0,
                "h_advance": 0,
                "h_lsb": 5,
                "v_advance": 0,
                "v_side_bearing": 0,
                "bbox": 0,
                "outline_or_flags": 0,
            },
            "samples": {"h_lsb": [["U+0041", "A", 1, 2]]},
            "observations": {},
            "fonttools_side_bearing_differences": {
                "counts": {"horizontal": 5, "vertical": 0},
                "delta_histogram": {"horizontal": {"-1": 5}, "vertical": {}},
                "maximum_absolute_delta": {
                    "horizontal": max(ordinary_horizontal, component_horizontal),
                    "vertical": 0,
                },
                "horizontal_use_my_metrics": {
                    "count": 1 if component_horizontal else 0,
                    "maximum_absolute_delta": component_horizontal,
                },
                "horizontal_without_use_my_metrics_maximum_absolute_delta": (
                    ordinary_horizontal
                ),
                "codepoints_checked": 10,
            },
        }

    @staticmethod
    def engine_case(horizontal_advance: int = 0) -> dict[str, object]:
        return {
            "region": "SC",
            "italic": True,
            "weight": "Light",
            "wght": 300,
            "reference_mode": "project-static-unhinted",
            "counts": {
                "missing_glyph": 0,
                "horizontal_advance": horizontal_advance,
                "vertical_advance": 0,
                "horizontal_side_bearing": 0,
                "vertical_side_bearing": 0,
            },
        }

    def test_named_fonttools_lsb_is_observation_only_with_clean_engine(self) -> None:
        metric = self.metric_case()
        audit.classify_fonttools_vf_metric_observations(
            [metric],
            [self.engine_case()],
        )
        self.assertEqual(metric["counts"]["h_lsb"], 0)
        self.assertEqual(
            metric["observations"][
                "fonttools_static_instance_side_bearing_differences"
            ],
            5,
        )
        self.assertEqual(metric["raw_counts"]["h_lsb"], 5)

    def test_named_fonttools_lsb_stays_failure_when_engine_is_not_clean(self) -> None:
        metric = self.metric_case()
        audit.classify_fonttools_vf_metric_observations(
            [metric],
            [self.engine_case(horizontal_advance=1)],
        )
        self.assertEqual(metric["counts"]["h_lsb"], 5)
        self.assertNotIn("raw_counts", metric)

    def test_named_fonttools_lsb_stays_failure_above_ordinary_limit(self) -> None:
        metric = self.metric_case(ordinary_horizontal=3)
        audit.classify_fonttools_vf_metric_observations(
            [metric],
            [self.engine_case()],
        )
        self.assertEqual(metric["counts"]["h_lsb"], 5)

    def test_use_my_metrics_has_a_separate_bounded_limit(self) -> None:
        metric = self.metric_case(ordinary_horizontal=1, component_horizontal=12)
        audit.classify_fonttools_vf_metric_observations(
            [metric],
            [self.engine_case()],
        )
        self.assertEqual(metric["counts"]["h_lsb"], 0)

        metric = self.metric_case(ordinary_horizontal=1, component_horizontal=17)
        audit.classify_fonttools_vf_metric_observations(
            [metric],
            [self.engine_case()],
        )
        self.assertEqual(metric["counts"]["h_lsb"], 5)

    def test_incomplete_difference_stats_cannot_hide_named_failure(self) -> None:
        metric = self.metric_case()
        metric["fonttools_side_bearing_differences"]["counts"]["horizontal"] = 4
        audit.classify_fonttools_vf_metric_observations(
            [metric],
            [self.engine_case()],
        )
        self.assertEqual(metric["counts"]["h_lsb"], 5)

    def test_anchor350_uses_separate_horizontal_and_vertical_limits(self) -> None:
        counts = {
            "missing_glyph": 0,
            "horizontal_advance": 0,
            "vertical_advance": 0,
            "horizontal_side_bearing": 1,
            "vertical_side_bearing": 1,
        }
        self.assertTrue(
            audit.is_anchor350_side_bearing_quantization_only(
                counts,
                {"horizontal": 2, "vertical": 5},
            )
        )
        self.assertFalse(
            audit.is_anchor350_side_bearing_quantization_only(
                counts,
                {"horizontal": 2, "vertical": 6},
            )
        )
        counts["horizontal_advance"] = 1
        self.assertFalse(
            audit.is_anchor350_side_bearing_quantization_only(
                counts,
                {"horizontal": 1, "vertical": 1},
            )
        )

    def test_source_metric_observation_requires_same_weight_engine_coverage(self) -> None:
        source_pairing = [
            {
                "region": "SC",
                "italic": True,
                "inter_controls": {
                    "350": {
                        "observations": {"hmtx_differences": 12},
                    }
                },
            }
        ]
        engine = self.engine_case()
        engine["weight"] = "Anchor350"
        engine["wght"] = 350
        self.assertEqual(
            audit.vf_source_metric_coverage_failures(source_pairing, [engine]),
            [],
        )
        engine["counts"]["vertical_side_bearing"] = 1
        failures = audit.vf_source_metric_coverage_failures(
            source_pairing,
            [engine],
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["wght"], 350)


class MetricVariationStorageTests(unittest.TestCase):
    def test_direct_calibration_refreshes_var_data_integer_widths(self) -> None:
        var_data = var_builder.buildVarData([0, 1], [[0, 0]], optimize=False)
        self.assertEqual(var_data.NumShorts, 0)
        var_data.Item[0][1] = 128
        fake_font = {
            "VVAR": type(
                "Table",
                (),
                {
                    "table": type(
                        "VVAR",
                        (),
                        {
                            "VarStore": type(
                                "VarStore",
                                (),
                                {"VarData": [var_data]},
                            )()
                        },
                    )()
                },
            )()
        }
        self.assertEqual(build.refresh_metric_var_data_integer_widths(fake_font), 1)
        self.assertEqual(var_data.NumShorts, 2)

    def test_outline_translation_does_not_move_metric_phantoms(self) -> None:
        font = {"gvar": SimpleNamespace(variations={})}
        with (
            patch.object(
                build,
                "glyph_variation_control_coordinates",
                return_value=("simple", [(0.0, 0.0), (10.0, 10.0)], []),
            ),
            patch.object(build, "gvar_coordinate_count", return_value=6),
        ):
            build.add_outline_translation_variation(
                font,
                "probe",
                (-1.0, -0.5, 0.0),
                7,
            )
        coordinates = font["gvar"].variations["probe"][0].coordinates
        self.assertEqual(coordinates[:2], [(7, 0), (7, 0)])
        self.assertEqual(coordinates[-4:], [(0, 0)] * 4)

    def test_gvar_coordinate_count_uses_current_outline(self) -> None:
        stale_variation = SimpleNamespace(coordinates=[None] * 48)
        font = {"gvar": SimpleNamespace(variations={"probe": [stale_variation]})}
        with patch.object(
            build,
            "glyph_variation_control_coordinates",
            return_value=("composite", [(0.0, 0.0)], [None]),
        ):
            self.assertEqual(build.gvar_coordinate_count(font, "probe"), 5)

    def test_default_outline_alignment_processes_components_before_parents(self) -> None:
        class ProbeGlyph:
            def __init__(self, components: list[str] | None = None) -> None:
                self.components = [SimpleNamespace(glyphName=name) for name in components or []]

            def isComposite(self) -> bool:
                return bool(self.components)

        glyf = SimpleNamespace(
            glyphs={
                "base": ProbeGlyph(),
                "parent": ProbeGlyph(["base"]),
                "grandparent": ProbeGlyph(["parent"]),
                "independent": ProbeGlyph(),
            }
        )

        class ProbeFont(dict):
            def getGlyphOrder(self) -> list[str]:
                return ["grandparent", "parent", "base", "independent"]

        font = ProbeFont(glyf=glyf)
        order, max_depth = build.glyph_component_dependency_order(
            font,
            font.getGlyphOrder(),
        )
        self.assertLess(order.index("base"), order.index("parent"))
        self.assertLess(order.index("parent"), order.index("grandparent"))
        self.assertEqual(max_depth, 2)


class CjkEllipsisTests(unittest.TestCase):
    def test_static_and_vf_use_cjk_locl_without_touching_font_data(self) -> None:
        sources = (
            build.static_dir("SC", False)
            / build.static_output_name("SC", "Regular", False),
            build.VARIABLE_DIR / build.variable_output_name("SC", False),
        )
        for source in sources:
            if not source.exists():
                self.skipTest("built SC static/VF fonts are not available")
        with tempfile.TemporaryDirectory() as tmp_name:
            for source in sources:
                with self.subTest(source=source.name):
                    target = Path(tmp_name) / source.name
                    shutil.copy2(source, target)
                    before = build.sfnt_table_hashes(target, {"head", "GSUB"})
                    font = TTFont(target, recalcTimestamp=False)
                    try:
                        build.apply_cjk_ellipsis_behavior(font)
                        font.save(target, reorderTables=True)
                    finally:
                        font.close()
                    after = build.sfnt_table_hashes(target, {"head", "GSUB"})
                    self.assertEqual(after, before)

                    font = TTFont(target)
                    try:
                        self.assertTrue(build.cjk_ellipsis_structure_status(font)["ok"])
                        cmap = font.getBestCmap()
                        proportional_gid = font.getGlyphID(
                            cmap[build.ELLIPSIS_CODEPOINT]
                        )
                        fullwidth_gid = font.getGlyphID(
                            cmap[build.CJK_ELLIPSIS_CODEPOINT]
                        )
                        upem = int(font["head"].unitsPerEm)
                    finally:
                        font.close()

                    data = target.read_bytes()
                    face = hb.Face(data)
                    hb_font = hb.Font(face)
                    hb_font.scale = (face.upem, face.upem)
                    hb_font.set_variations({"wght": 400})

                    def shape(
                        language: str,
                        direction: str,
                    ) -> tuple[list[int], int, int, list[object]]:
                        buffer = hb.Buffer()
                        buffer.add_str("\u2026\u2026")
                        buffer.script = "Latn" if language == "en" else "Hani"
                        buffer.language = language
                        buffer.direction = direction
                        hb.shape(hb_font, buffer)
                        glyphs = [info.codepoint for info in buffer.glyph_infos]
                        return (
                            glyphs,
                            sum(pos.x_advance for pos in buffer.glyph_positions),
                            sum(pos.y_advance for pos in buffer.glyph_positions),
                            [hb_font.get_glyph_extents(glyph) for glyph in glyphs],
                        )

                    english = shape("en", "ltr")
                    self.assertEqual(english[0], [proportional_gid, proportional_gid])
                    self.assertIsNotNone(english[3][0])
                    for language in ("zh-Hans", "zh-Hant", "zh-HK", "ja", "ko"):
                        with self.subTest(source=source.name, language=language):
                            cjk = shape(language, "ltr")
                            vertical = shape(language, "ttb")
                            self.assertEqual(cjk[:3], ([fullwidth_gid, fullwidth_gid], 2 * upem, 0))
                            self.assertIsNotNone(cjk[3][0])
                            self.assertLess(
                                english[3][0].y_bearing,
                                cjk[3][0].y_bearing,
                            )
                            self.assertEqual(len(set(vertical[0])), 1)
                            self.assertEqual(vertical[1:3], (0, -2 * upem))


class AuditSummaryTests(unittest.TestCase):
    def test_reused_report_rejects_changed_font_or_contract(self) -> None:
        inputs = {"fonts/a.ttf": {"size": 1, "sha256": "a"}}
        contract = {"tools/audit.py": "b"}
        checkpoint = {
            "input_manifest": inputs,
            "audit_contract": contract,
        }
        audit.validate_reused_audit_provenance(checkpoint, inputs, contract)
        with self.assertRaisesRegex(ValueError, "156 fonts"):
            audit.validate_reused_audit_provenance(
                checkpoint,
                {"fonts/a.ttf": {"size": 2, "sha256": "c"}},
                contract,
            )
        with self.assertRaisesRegex(ValueError, "current scripts"):
            audit.validate_reused_audit_provenance(
                checkpoint,
                inputs,
                {"tools/audit.py": "changed"},
            )

    def test_sum_nested_values_returns_all_numeric_totals(self) -> None:
        self.assertEqual(
            audit.sum_nested_values(
                [
                    {"counts": {"a": 2, "b": 3}},
                    {"counts": {"a": 5, "ignored": "text"}},
                ],
                "counts",
            ),
            {"a": 7, "b": 3},
        )

    def test_portable_report_gate_rejects_windows_and_posix_homes(self) -> None:
        for value in (
            r'{"path":"C:\\Users\\name\\font.ttf"}',
            '{"path":"/home/name/font.ttf"}',
            '{"path":"/Users/name/font.ttf"}',
            '{"path":"/mnt/c/Users/name/font.ttf"}',
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "local absolute paths"):
                    build.assert_portable_report_text(value)


class ArchiveSafetyTests(unittest.TestCase):
    def test_parent_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            with self.assertRaisesRegex(RuntimeError, "unsafe archive member"):
                build.validate_archive_member_paths(["../outside.ttf"], Path(tmp_name))

    def test_windows_absolute_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            with self.assertRaisesRegex(RuntimeError, "unsafe archive member"):
                build.validate_archive_member_paths(
                    [r"C:\\Users\\name\\font.ttf"],
                    Path(tmp_name),
                )


class ReleasePackageTests(unittest.TestCase):
    def test_release_matrix_has_21_documented_packages(self) -> None:
        packages = package_release.release_packages()
        self.assertEqual(len(packages), 21)
        self.assertEqual(len({package.filename for package in packages}), 21)
        for package in packages:
            names = {name for _path, name in package.files}
            self.assertTrue(set(package_release.REQUIRED_DOCUMENTS) <= names)

    def test_package_verification_compares_member_sha256_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            files = []
            for name in package_release.REQUIRED_DOCUMENTS:
                source = root / name
                source.write_text("alpha", encoding="ascii")
                files.append((source, name))
            package = package_release.Package("probe.zip", tuple(files))
            font = root / "probe.ttf"
            font.write_bytes(b"font-fixture")
            package = package_release.Package("probe.zip", tuple(files) + ((font, font.name),))
            package_release.write_package(package, root)
            files[0][0].write_text("omega", encoding="ascii")
            package_release.SOURCE_DIGEST_CACHE.clear()
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                package_release.verify_package(root / package.filename, package)


if __name__ == "__main__":
    unittest.main()
