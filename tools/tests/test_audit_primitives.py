from __future__ import annotations

import struct
import json
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
    def test_release_manifest_is_generated_from_the_verified_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            with patch.multiple(build, ROOT=root, STATIC_ROOT=root/'fonts/static', VARIABLE_DIR=root/'fonts/variable'), patch.object(package_release, 'ROOT', root):
                for source, _name in package_release.documentation_files():
                    source.write_text('文档夹具', encoding='utf-8')
                (root/'reports').mkdir()
                for name in ('release-audit.json', 'ots-audit.json', 'fontbakery-audit.json', 'visual-audit.json'):
                    (root/'reports'/name).write_text('{}', encoding='utf-8')
                build.VARIABLE_DIR.mkdir(parents=True)
                for region in build.REGION_ORDER:
                    for italic in (False, True):
                        (build.VARIABLE_DIR/build.variable_output_name(region, italic)).write_bytes(f'VF {region} {italic}'.encode())
                    for hinted in (False, True):
                        directory=build.static_dir(region, hinted); directory.mkdir(parents=True)
                        for document in ("README.txt", "LICENSE-Sarasa-Gothic.txt"):
                            (directory/document).write_text("文档夹具", encoding="utf-8")
                        for stop in build.SOURCE_HAN_WEIGHT_STOPS:
                            for italic in (False, True):
                                name=build.static_output_name(region, str(stop['name']), italic)
                                (directory/name).write_bytes(f'{region} {stop["name"]} {italic} {hinted}'.encode())
                packages=package_release.release_packages()
                results=[package_release.write_package(package, root/'dist') for package in packages]
                package_release.write_release_manifest(packages, results)
                report=json.loads((root/'reports/release-packages.json').read_text(encoding='utf-8'))
                self.assertEqual((report['package_count'], report['unique_fonts'], report['font_occurrences']), (21, 156, 312))
                self.assertEqual(len(report['font_sha256']), 156)
                self.assertTrue(report['verified_before_promotion'])
                self.assertEqual(len(report['audit_report_sha256']), 4)
                with self.assertRaises(ValueError):
                    package_release.write_release_manifest(packages[:-1], results[:-1])

    def test_release_matrix_has_21_documented_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name, patch.object(build, "STATIC_ROOT", Path(tmp_name)):
            for region in build.REGION_ORDER:
                for hinted in (False, True):
                    directory = build.static_dir(region, hinted)
                    directory.mkdir(parents=True)
                    for document in ("README.txt", "LICENSE-Sarasa-Gothic.txt"):
                        (directory/document).write_text("文档夹具", encoding="utf-8")
                    for stop in build.SOURCE_HAN_WEIGHT_STOPS:
                        for italic in (False, True):
                            (directory / build.static_output_name(region, str(stop["name"]), italic)).touch()
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
