"""Regressions for the v1.0.43.2 fixes: build, audit, release fetch and packaging."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b  # noqa: E402
import fetch_release_fonts as fetch  # noqa: E402
import package_release as package  # noqa: E402
import python_env_bootstrap as bootstrap  # noqa: E402
from font_audit import contract as audit, native, pipeline  # noqa: E402
from font_audit.differential import notdef_parity  # noqa: E402
from font_audit.gate import SECTIONS  # noqa: E402
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString  # noqa: E402
from fontTools.fontBuilder import FontBuilder  # noqa: E402
from fontTools.pens.boundsPen import BoundsPen  # noqa: E402
from fontTools.pens.ttGlyphPen import TTGlyphPen  # noqa: E402
from fontTools.ttLib import TTFont, newTable  # noqa: E402
from fontTools.ttLib.tables import otTables as ot  # noqa: E402
from fontTools.ttLib.tables._f_v_a_r import Axis  # noqa: E402
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphComponent  # noqa: E402
from fontTools.ttLib.tables.TupleVariation import TupleVariation  # noqa: E402


def rectangles(*boxes):
    pen = TTGlyphPen(None)
    for x0, y0, x1, y1 in boxes:
        pen.moveTo((x0, y0)); pen.lineTo((x0, y1)); pen.lineTo((x1, y1)); pen.lineTo((x1, y0)); pen.closePath()
    return pen.glyph()


def fixture(order, cmap, shapes, advances=None, fea=None, vertical=False):
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    builder.setupGlyf({name: rectangles(*shapes.get(name, ())) for name in order})
    glyf = builder.font["glyf"]
    builder.setupHorizontalMetrics({name: ((advances or {}).get(name, 500), getattr(glyf[name], "xMin", 0))
                                    for name in order})
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    if vertical:
        builder.setupVerticalMetrics({name: (1000, 880 - getattr(glyf[name], "yMax", 880)) for name in order})
        builder.setupVerticalHeader(ascent=500, descent=-500)
    builder.setupNameTable({"familyName": "Fixture", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    if fea:
        addOpenTypeFeaturesFromString(builder.font, fea)
    # Compile and read back, so layout tables look as they do in a real font.
    stream = io.BytesIO()
    builder.font.save(stream)
    stream.seek(0)
    return TTFont(stream)


def saved(font, path):
    font.save(path)
    return path


HANGUL_ORDER = [".notdef", "L", "V", "T", "LV"]
HANGUL_CMAP = {0x1100: "L", 0x1188: "V", 0x11A8: "T"}
HANGUL_SHAPES = {"L": [(50, 0, 450, 700)], "V": [(500, 0, 900, 700)], "T": [(100, 0, 900, 200)],
                 "LV": [(50, 0, 900, 700)], ".notdef": [(50, 0, 450, 700)]}
HANGUL_SOURCE = """
languagesystem DFLT dflt;
languagesystem hang dflt;
lookup LIGATE { sub L V by LV; } LIGATE;
feature ccmp {
    ignore sub L' V' T;
    sub L' lookup LIGATE V';
} ccmp;
"""
HANGUL_RELEASE = """
languagesystem DFLT dflt;
languagesystem hang dflt;
lookup LIGATE { sub L V by LV; } LIGATE;
feature ccmp { sub L' lookup LIGATE V'; } ccmp;
"""


def coverage(*names):
    table = ot.Coverage()
    table.glyphs = list(names)
    return table


def chained_rule(lookahead, records):
    rule = ot.ChainContextSubst()
    rule.Format = 3
    rule.BacktrackCoverage, rule.BacktrackGlyphCount = [], 0
    rule.InputCoverage, rule.InputGlyphCount = [coverage("L"), coverage("V")], 2
    rule.LookAheadCoverage = [coverage(*lookahead)] if lookahead else []
    rule.LookAheadGlyphCount = len(rule.LookAheadCoverage)
    rule.SubstLookupRecord, rule.SubstCount = records, len(records)
    return rule


def hangul_fonts():
    """Source Han's guarded ccmp, and Sarasa's plain context lookup without the guard."""
    source = fixture(HANGUL_ORDER, HANGUL_CMAP, HANGUL_SHAPES, fea=HANGUL_SOURCE)
    # Source Han writes each rule as its own format 3 subtable, the guard
    # (no substitution, T jamo lookahead) first; feaLib would merge them.
    gsub = source["GSUB"].table
    ccmp = next(record for record in gsub.FeatureList.FeatureRecord if record.FeatureTag == "ccmp")
    lookup = gsub.LookupList.Lookup[ccmp.Feature.LookupListIndex[0]]
    ligate = ot.SubstLookupRecord(); ligate.SequenceIndex = 0
    ligate.LookupListIndex = next(index for index, item in enumerate(gsub.LookupList.Lookup) if item.LookupType == 4)
    lookup.SubTable = [chained_rule(["T"], []), chained_rule([], [ligate])]
    lookup.SubTableCount = 2
    stream = io.BytesIO(); source.save(stream); stream.seek(0); source = TTFont(stream)
    release = fixture(HANGUL_ORDER, HANGUL_CMAP, HANGUL_SHAPES, fea=HANGUL_RELEASE)
    # Without backtrack or lookahead feaLib writes a plain context lookup
    # (type 5), as Sarasa's pipeline does; the guard needs a chained one.
    gsub = release["GSUB"].table
    ccmp = next(record for record in gsub.FeatureList.FeatureRecord if record.FeatureTag == "ccmp")
    assert gsub.LookupList.Lookup[ccmp.Feature.LookupListIndex[0]].LookupType == 5
    return source, release


class VersionAndReportPaths(unittest.TestCase):
    def test_revision_digits_stay_unambiguous(self):
        self.assertEqual(b.opentype_version("1.0.43"), "1.043")
        self.assertEqual(b.opentype_version("1.0.43.2"), "1.0432")
        for version in ("1.0.43.10", "1.0.4.31", "1.0.43.0", "2.0.43"):
            with self.assertRaises(ValueError):
                b.opentype_version(version)

    def test_paths_outside_the_repository_name_their_root(self):
        # A relative path out of the repository spelled ../../Users/<name>/...,
        # which the portability check rejected only after hours of building.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, work, cache = base / "repo", base / "Users" / "name" / "work", base / "cache"
            for path in (root, work, cache):
                path.mkdir(parents=True)
            with patch.multiple(b, ROOT=root, WORK_ROOT=work, BUILD_CACHE_DIR=cache, OUTPUT_STAGING_ROOT=None):
                self.assertEqual(b.portable_report_path(work / "sources" / "a.ttf"), "<work>/sources/a.ttf")
                self.assertEqual(b.portable_report_path(cache / "b.json"), "<cache>/b.json")
                self.assertEqual(b.portable_report_path(root / "fonts" / "c.ttf"), "fonts/c.ttf")
                report = json.dumps({"source": b.portable_report_path(work / "sources" / "a.ttf")})
                b.assert_portable_report_text(report)


class RetiredSwitchesAndTools(unittest.TestCase):
    def test_switches_that_skipped_verification_are_refused(self):
        for name in ("SARASA_SKIP_SOURCE_BOOTSTRAP", "SARASA_SKIP_PYTHON_DEPS"):
            with patch.dict(os.environ, {name: "1"}):
                with self.assertRaisesRegex(RuntimeError, name):
                    b.reject_retired_switches()
            with patch.dict(os.environ, {name: "0"}):
                b.reject_retired_switches()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SARASA_SKIP_PYTHON_DEPS": "1"}):
            with self.assertRaisesRegex(RuntimeError, "SARASA_SKIP_PYTHON_DEPS"):
                bootstrap.ensure_project_python({}, project_root=Path(directory), label="test")

    def test_a_ttfautohint_on_path_is_never_picked_up(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTFAUTOHINT", None)
            with patch.object(b.shutil, "which", return_value=sys.executable):
                _path, provider = b.selected_ttfautohint()
        self.assertEqual(provider, "ttfautohint-py")

    def test_options_a_mode_would_ignore_are_refused(self):
        for options in (["--static-only", "--resume-variable"], ["--variable-only", "--resume-static"],
                        ["--unhinted-only", "--resume-variable"], ["--unhinted-only", "--resume-static"]):
            with patch.object(sys, "argv", ["build", *options]), patch.object(b, "build_all") as build_all:
                with self.assertRaises(SystemExit):
                    b.main()
                build_all.assert_not_called()


class OutputRecordProvenance(unittest.TestCase):
    def test_refreshed_records_resume_and_report_their_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            font = root / "fonts" / "variable" / "probe.ttf"
            font.parent.mkdir(parents=True)
            font.write_bytes(b"refreshed bytes")
            with patch.multiple(b, ROOT=root, OUTPUT_VALIDATION_DIR=root / "records", OUTPUT_STAGING_ROOT=None):
                (root / "records").mkdir()
                built = {"file": font.name, "region": "SC", "italic": False, "weight": None, "hinted": False,
                         "sha256": "0" * 64, "contract": {"schema": 3, "builder_sha256": "a" * 64},
                         "provenance": {"entry": "build"}}
                record = {"file": font.name, "region": "SC", "italic": False, "weight": None, "hinted": False,
                          "sha256": b.file_sha256(font), "contract": b.output_validation_contract(False),
                          "provenance": {"entry": "refresh-variable-finalization", "source_record": built}}
                b.output_validation_record_path(font).write_text(json.dumps(record), encoding="utf-8")
                self.assertEqual(b.verified_output_record_status(font, "SC", False), (True, ""))
                provenance = b.output_record_provenance(font)
        self.assertEqual(provenance["entry"], "refresh-variable-finalization")
        self.assertFalse(provenance["built_by_current_code"])
        self.assertEqual([item["entry"] for item in provenance["chain"]], ["refresh-variable-finalization", "build"])
        self.assertEqual(provenance["chain"][1]["builder_sha256"], "a" * 64)


    def test_a_refresh_signs_only_fully_validated_bytes(self):
        # The first v1.0.43.2 refresh signed six VFs after the light checks;
        # their υ sat 1.1 units off the static fonts at 700.
        steps = ["apply_default_regional_punctuation", "normalize_variable_vertical_origin", "add_digit_colon_feature",
                 "align_tabular_alternate_advances", "normalize_cross_engine_metrics", "finalize_variable_vertical_segments",
                 "align_source_punctuation_advances", "align_unencoded_inter_ink", "sync_gvar_advances_to_hvar",
                 "update_vf_names", "update_os2_sarasa_metadata", "remove_mac_name_records", "update_head_project_revision",
                 "normalize_variable_raster_metadata", "prune_unreferenced_variable_names", "finalize_layout_routes",
                 "verify_variable_routes_match_static", "normalize_ideograph_gdef_classes", "rebuild_vf_underline_mvar",
                 "sfnt_table_hashes"]
        for validated in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                variable = Path(directory)
                font_path = variable / b.variable_output_name("SC", False)
                saved(fixture([".notdef", "A"], {0x41: "A"}, {"A": [(10, 0, 490, 700)]}), font_path)
                verdict = (validated, {"resume_rebuild_reasons": [] if validated else ["HarfBuzz named-instance metrics"]})
                with patch.multiple(b, VARIABLE_DIR=variable, **{name: MagicMock(return_value={}) for name in steps}), \
                        patch.multiple(b, glyph_point_structure=MagicMock(return_value=0), maxCtxFont=MagicMock(return_value=0),
                                       open_project_static_metric_reference_font=MagicMock(), load_inter=MagicMock(),
                                       variable_output_resume_status=MagicMock(return_value=verdict),
                                       record_verified_output=MagicMock()), \
                        patch.object(b.font_layout, "prune_unreferenced_lookups", return_value=0):
                    if validated:
                        outputs = b._refresh_variable_finalization_outputs(["SC"], (False,))
                        self.assertEqual(outputs[0]["final_validation"], verdict[1])
                        b.record_verified_output.assert_called_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "完整核验"):
                            b._refresh_variable_finalization_outputs(["SC"], (False,))
                        b.record_verified_output.assert_not_called()
                    b.variable_output_resume_status.assert_called_once_with("SC", False, verify_record=False)
                self.assertEqual(sorted(item.name for item in variable.iterdir()), [font_path.name])

    def test_a_static_only_report_inspects_the_published_vfs_it_declares(self):
        # A static-only build stages no VF: its inspection read none and stayed
        # partial although the build report listed all twelve.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / ".build-cache" / "transactions" / "build" / "candidate"
            published = root / "fonts" / "variable" / b.variable_output_name("SC", False)
            published.parent.mkdir(parents=True)
            published.write_bytes(b"font")
            (stage / "fonts" / "variable").mkdir(parents=True)
            inspected = []
            with patch.multiple(b, ROOT=root, OUTPUT_STAGING_ROOT=stage, VARIABLE_DIR=stage / "fonts" / "variable",
                                STATIC_ROOT=stage / "fonts" / "static", REPORT_DIR=stage / "reports"), \
                    patch.object(b, "inspect_font", side_effect=lambda font: inspected.append(font) or {}), \
                    patch.object(b, "output_record_provenance", return_value={"entry": "build"}):
                b.write_reports({"variable_outputs": [{"file": "fonts/variable/" + published.name, "region": "SC"}],
                                 "static_outputs": []})
        self.assertEqual(inspected, [published])


class HangulCcmpGuards(unittest.TestCase):
    def test_build_restores_source_han_guards_in_a_plain_context_lookup(self):
        source, release = hangul_fonts()
        with tempfile.TemporaryDirectory() as directory:
            source_path = saved(source, Path(directory) / "source.ttf")
            before = saved(release, Path(directory) / "before.ttf")
            status = b.hangul_ccmp_guard_runtime_status(before, source_path)
            self.assertFalse(status["ok"])
            self.assertEqual(status["samples"][0]["glyphs"], 2)
            font = TTFont(before)
            with TTFont(source_path) as guarded:
                self.assertEqual(b.source_han_hangul_ccmp_guards(guarded), [(0x1100, 0x1188, (0x11A8,))])
                report = b.restore_source_han_hangul_ccmp_guards(font, guarded)
                self.assertEqual(report["hangul_ccmp_guards_restored"], 1)
                # Idempotent: a second pass finds the guard already in place.
                self.assertEqual(b.restore_source_han_hangul_ccmp_guards(font, guarded)["hangul_ccmp_guards_restored"], 0)
            after = saved(font, Path(directory) / "after.ttf")
            status = b.hangul_ccmp_guard_runtime_status(after, source_path)
            self.assertTrue(status["ok"], status["samples"])
            with TTFont(after) as reloaded:
                lookups = reloaded["GSUB"].table.LookupList.Lookup
                self.assertIn(6, {lookup.LookupType for lookup in lookups})

    def test_audit_rewrites_the_release_by_the_same_declared_rule(self):
        source, release = hangul_fonts()
        with tempfile.TemporaryDirectory() as directory:
            source_path = saved(source, Path(directory) / "source.ttf")
            guards = native.hangul_ccmp_guards(TTFont(source_path))
            self.assertEqual(guards, [(0x1100, 0x1188, frozenset({0x11A8}))])
            raw = saved(release, Path(directory) / "release.ttf")
            self.assertTrue(native.guarded_composition_status(raw.read_bytes(), source_path, 0, guards))
            font = TTFont(raw)
            self.assertEqual(native.guard_hangul_compositions(font, guards), 1)
            self.assertEqual(native.guard_hangul_compositions(font, guards), 0)
            rewritten = saved(font, Path(directory) / "rewritten.ttf")
            self.assertEqual(native.guarded_composition_status(rewritten.read_bytes(), source_path, 0, guards), [])


class VariableGlyphIdentity(unittest.TestCase):
    def test_vf_takes_the_static_notdef(self):
        # Up to v1.0.43.1 every missing character took Source Han's 1000-unit
        # box in the VF and Inter's 656-unit box in the static.
        base = fixture([".notdef", "A"], {0x41: "A"}, {".notdef": [(100, -100, 900, 800)], "A": [(10, 0, 490, 700)]},
                       advances={".notdef": 1000}, vertical=True)
        reference = fixture([".notdef", "A"], {0x41: "A"},
                            {".notdef": [(160, -203, 496, 906), (250, 156, 410, 546)], "A": [(10, 0, 490, 700)]},
                            advances={".notdef": 656}, vertical=True)
        base["gvar"] = newTable("gvar")
        base["gvar"].variations = {".notdef": [TupleVariation({"wght": (0.0, 1.0, 1.0)}, [(1, 1)] * 8)]}
        report = b.adopt_reference_notdef(base, reference)
        self.assertEqual(report, {"notdef_advance": 656, "notdef_contours": 2})
        self.assertEqual(base["hmtx"][".notdef"], reference["hmtx"][".notdef"])
        self.assertEqual(base["vmtx"][".notdef"], reference["vmtx"][".notdef"])
        self.assertNotIn(".notdef", base["gvar"].variations)
        self.assertEqual(base["glyf"][".notdef"].program.getBytecode(), b"")

    def test_vf_vorg_never_follows_positional_names(self):
        order = [".notdef", "uni4E00", "glyph00002", "glyph00003"]
        shapes = {name: [(100, 0, 900, 800)] for name in order}
        cmap = {0x4E00: "uni4E00"}
        font = fixture(order, cmap, shapes, fea="feature salt { sub uni4E00 by glyph00003; } salt;")
        reference = fixture(order, cmap, shapes, fea="feature salt { sub uni4E00 by glyph00002; } salt;")
        reference["VORG"] = newTable("VORG")
        reference["VORG"].majorVersion, reference["VORG"].minorVersion = 1, 0
        reference["VORG"].defaultVertOriginY = 880
        reference["VORG"].VOriginRecords = {"glyph00002": 777, "glyph00003": 555}
        b.rebuild_vorg_from_reference(font, reference, same_pipeline=False)
        # glyph00003 stands for the reference's glyph00002 (salt of 一), not for
        # the reference glyph that happens to share its name.
        self.assertEqual(font["VORG"].VOriginRecords, {"glyph00003": 777})
        b.rebuild_vorg_from_reference(font, reference, same_pipeline=True)
        self.assertEqual(font["VORG"].VOriginRecords, {"glyph00002": 777, "glyph00003": 555})

    def test_final_inter_corrections_pair_unencoded_alternates(self):
        shapes = {"A": [(10, 0, 490, 700)], "A.salt": [(10, 0, 240, 700), (260, 0, 490, 700)]}
        inter = fixture([".notdef", "A", "A.salt"], {0x41: "A"}, shapes,
                        fea="feature salt { sub A by A.salt; } salt;")
        product = fixture([".notdef", "A", "glyph00002", "glyph00003"], {0x41: "A"},
                          {"A": shapes["A"], "glyph00002": shapes["A.salt"], "glyph00003": [(0, 0, 100, 100)]},
                          fea="feature salt { sub A by glyph00002; } salt; feature case { sub A by glyph00003; } case;")
        pairs = b.final_inter_outline_correction_pairs(product, inter)
        self.assertIn(("A", "A"), pairs)
        self.assertIn(("A.salt", "glyph00002"), pairs)
        self.assertNotIn("glyph00003", {target for _source, target in pairs})


def shared_alternate_font(scale=None):
    """u.1 (an alternate only salt reaches) and υ, an encoded composite built on it, as in Inter."""
    font = fixture([".notdef", "upsilon", "u.1"], {0x03C5: "upsilon"},
                   {".notdef": [(50, 0, 450, 700)], "u.1": [(100, 0, 400, 500)]})
    glyf = font["glyf"]
    component = GlyphComponent()
    component.glyphName, component.x, component.y, component.flags = "u.1", 0, 0, 0
    if scale is not None:
        component.transform = [[scale, 0], [0, scale]]
    upsilon = Glyph()
    upsilon.numberOfContours, upsilon.components = -1, [component]
    glyf["upsilon"] = upsilon
    upsilon.recalcBounds(glyf)
    font["hmtx"]["upsilon"] = (500, upsilon.xMin)
    axis = Axis()
    axis.axisTag, axis.minValue, axis.defaultValue, axis.maxValue, axis.axisNameID = "wght", 200, 400, 900, 256
    font["fvar"] = newTable("fvar")
    font["fvar"].axes, font["fvar"].instances = [axis], []
    font["gvar"] = newTable("gvar")
    font["gvar"].variations = {name: [] for name in font.getGlyphOrder()}
    return font


def ink_left(font, glyph_name, weight):
    glyphs = font.getGlyphSet(location={"wght": weight})
    pen = BoundsPen(glyphs)
    glyphs[glyph_name].draw(pen)
    return pen.bounds[0]


class UnencodedInkAlignment(unittest.TestCase):
    @staticmethod
    def inter_curve(font, _inter, weights):
        # Inter's curve puts u.1 three units right of the product at every weight.
        return {weight: {"u.1": 103 - ink_left(font, "u.1", weight)} for weight in weights}

    def test_a_shared_alternate_moves_alone(self):
        # Inter builds υ on u.1: the first v1.0.43.2 refresh moved u.1 toward
        # Inter's curve and took υ and 19 other encoded glyphs 1.1 units off
        # the static metrics at 700.
        font = shared_alternate_font()
        with patch.object(b, "unencoded_inter_ink_misses", side_effect=self.inter_curve):
            report = b.align_unencoded_inter_ink(font, None)
        self.assertEqual(report["unencoded_inter_ink_translations"], len(b.INTER_OUTLINE_CORRECTION_WEIGHTS))
        self.assertEqual(report["unencoded_inter_ink_compensated_components"], len(b.INTER_OUTLINE_CORRECTION_WEIGHTS))
        for weight in (200, 300, 700, 750, 900):
            self.assertAlmostEqual(ink_left(font, "u.1", weight), 103, places=6)
            self.assertAlmostEqual(ink_left(font, "upsilon", weight), 100, places=6)
        self.assertEqual(ink_left(font, "u.1", 400), 100)

    def test_a_transformed_user_cannot_be_compensated(self):
        font = shared_alternate_font(scale=0.5)
        with patch.object(b, "unencoded_inter_ink_misses", side_effect=self.inter_curve):
            with self.assertRaisesRegex(ValueError, "带变换的组件"):
                b.align_unencoded_inter_ink(font, None)


class AuditMissingGlyph(unittest.TestCase):
    def test_notdef_parity_measures_the_missing_glyph_box(self):
        with tempfile.TemporaryDirectory() as directory:
            fonts = {}
            for name, advance in (("wide", 1000), ("narrow", 656), ("same", 656)):
                fonts[name] = saved(fixture([".notdef", "A", "B"], {0x41: "A", 0x42: "B"},
                                            {".notdef": [(100, 0, 500, 700)], "A": [(10, 0, 490, 700)], "B": [(10, 0, 490, 700)]},
                                            advances={".notdef": advance}, vertical=True), Path(directory) / f"{name}.ttf")
            different = notdef_parity(fonts["wide"], fonts["narrow"])
            self.assertEqual((different["cases"], different["failures"]), (4, 4))
            same = notdef_parity(fonts["narrow"], fonts["same"])
            self.assertEqual((same["cases"], same["failures"]), (4, 0))


class AuditScope(unittest.TestCase):
    def test_a_section_without_cases_in_scope_is_skipped_not_executed(self):
        unit = pipeline.Unit("metadata", "SC", False, "Regular", False)
        plan = {name: {} for name in SECTIONS}
        plan["metadata"] = {unit.name.split(":", 1)[1]: 1}
        with (patch.object(pipeline, "plan", return_value=plan),
              patch.object(pipeline, "units", return_value=[unit]),
              patch.object(pipeline, "execute", lambda _unit: {"metadata": {"cases": 1, "failures": 0, "samples": []}}),
              patch.object(pipeline.provenance, "capture", return_value={}),
              patch.object(audit, "audit_input_manifest", return_value={}),
              patch.object(audit, "audit_contract_manifest", return_value={}),
              patch.object(audit, "measurement_contract_manifest", return_value={})):
            report = pipeline.run(regions=("SC",), selected={"metadata", "classical_outlines"}, jobs=1, log=lambda _: None)
        self.assertEqual(report["gate"]["sections"]["classical_outlines"]["status"], "skipped")
        self.assertEqual(report["gate"]["sections"]["metadata"]["status"], "passed")


def release_manifest(version="9.9.9.9"):
    return {"version": version, "package_count": 2,
            "documentation_sha256": {"LICENSE.txt": "1" * 64, "NOTICE.md": "2" * 64, "README.md": "3" * 64},
            "font_sha256": {f"fonts/variable/Sarasa-Ui-VF-PropDigits-SC[wght].ttf": "4" * 64,
                            f"fonts/static/SarasaUiPropDigitsSC-TTF-{version}/SarasaUiPropDigitsSC-Regular.ttf": "5" * 64},
            "packages": [{"file": f"Sarasa-Ui-VF-PropDigits-SC-TTF-{version}.zip", "bytes": 10, "sha256": "6" * 64, "members": 4},
                         {"file": f"SarasaUiPropDigitsSC-TTF-{version}.zip", "bytes": 20, "sha256": "7" * 64, "members": 6}]}


class FetchRelease(unittest.TestCase):
    def test_an_unpublished_release_exits_three_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports" / "release-packages.json").write_text(json.dumps(release_manifest()), encoding="utf-8")
            for release in (None, {"draft": True, "assets": []}):
                with patch.object(fetch, "api_request", return_value=release), \
                     patch.object(sys, "argv", ["fetch", "--root", str(root)]), patch.object(fetch, "download") as download:
                    with self.assertRaises(SystemExit) as raised:
                        fetch.main()
                    download.assert_not_called()
                self.assertEqual(raised.exception.code, fetch.NOT_PUBLISHED)

    def test_a_published_release_must_carry_every_asset_as_recorded(self):
        packages = {item["file"]: item for item in release_manifest()["packages"]}
        exact = {name: {"name": name, "size": item["bytes"], "digest": "sha256:" + item["sha256"], "state": "uploaded"}
                 for name, item in packages.items()}
        fetch.check_assets(exact, packages)
        name = next(iter(packages))
        for assets in ({key: value for key, value in exact.items() if key != name},
                       {**exact, name: {**exact[name], "size": 1}},
                       {**exact, name: {**exact[name], "digest": "sha256:" + "0" * 64}},
                       {**exact, "extra.zip": {"name": "extra.zip", "size": 1}}):
            with self.assertRaises(SystemExit) as raised:
                fetch.check_assets(assets, packages)
            self.assertNotEqual(raised.exception.code, fetch.NOT_PUBLISHED)

    def test_package_members_are_checked_against_the_manifest(self):
        version = "9.9.9.9"
        manifest = release_manifest(version)
        name = f"SarasaUiPropDigitsSC-TTF-{version}.zip"
        members = fetch.package_members(name, version, manifest)
        self.assertEqual(set(members), {"LICENSE.txt", "NOTICE.md", "README.md", "SarasaUiPropDigitsSC-Regular.ttf",
                                        "README.txt", "LICENSE-Sarasa-Gothic.txt"})
        self.assertEqual(set(fetch.package_members(f"Sarasa-Ui-VF-PropDigits-SC-TTF-{version}.zip", version, manifest)),
                         {"LICENSE.txt", "NOTICE.md", "README.md", "Sarasa-Ui-VF-PropDigits-SC[wght].ttf"})
        contents = {"LICENSE.txt": b"license", "NOTICE.md": b"notice", "README.md": b"readme",
                    "SarasaUiPropDigitsSC-Regular.ttf": b"font", "README.txt": b"static readme",
                    "LICENSE-Sarasa-Gothic.txt": b"static license"}
        import hashlib
        manifest["documentation_sha256"] = {key: hashlib.sha256(contents[key]).hexdigest()
                                            for key in ("LICENSE.txt", "NOTICE.md", "README.md")}
        manifest["font_sha256"][f"fonts/static/SarasaUiPropDigitsSC-TTF-{version}/SarasaUiPropDigitsSC-Regular.ttf"] = \
            hashlib.sha256(b"font").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / name
            with zipfile.ZipFile(archive, "w") as bundle:
                for member, data in contents.items():
                    bundle.writestr(member, data)
            fetch.verify_package_members(archive, name, version, manifest, {"members": 6}, {})
            with self.assertRaises(SystemExit):
                fetch.verify_package_members(archive, name, version, manifest, {"members": 6},
                                             {(f"SarasaUiPropDigitsSC-TTF-{version}", "README.txt"): "0" * 64})


class PackageManifestChecks(unittest.TestCase):
    def test_verify_only_compares_archives_with_the_release_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            manifest = {"version": b.VERSION, "packages": [{"file": "a.zip", "bytes": 1, "sha256": "0" * 64, "members": 4}]}
            (root / "reports" / "release-packages.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(package, "ROOT", root):
                with self.assertRaisesRegex(ValueError, "发布清单"):
                    package.verify_release_manifest([], [{"file": "a.zip", "bytes": 2, "sha256": "0" * 64, "members": 4}])
                manifest["version"] = "0.0.0"
                (root / "reports" / "release-packages.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "发布清单属于"):
                    package.verify_release_manifest([], [])

    def test_fontbakery_approvals_name_their_pinned_counterpart(self):
        import check_external_release as external
        regions = {}
        for region in b.REGION_ORDER:
            regions[region] = {"product_fonts": [audit.display_path(audit.vf_path(region, italic) if weight is None
                                                                    else audit.static_path(region, weight, italic, True))
                                                 for weight, italic, _w, _i in external.UNIVERSAL_SAMPLES]}
        font = audit.display_path(audit.static_path("SC", "Heavy", False, True))
        approval = {"region": "SC", "check": "file_size", "font": font, "result": "FAIL", "counterpart_result": "FAIL",
                    "counterpart": audit.display_path(audit.reference_path("SC", "Bold", False, True))}
        def report(items):
            return {"universal": {"passed": True, "unapproved_failures": [], "regions": regions,
                                  "approved_upstream_failures": items}, "approved_upstream_failures": items}
        package.validate_fontbakery_universal(report([approval]))
        for counterpart in (None, audit.display_path(audit.reference_path("SC", "Regular", False, True))):
            with self.assertRaisesRegex(ValueError, "对应的固定上游"):
                package.validate_fontbakery_universal(report([{**approval, "counterpart": counterpart}]))


if __name__ == "__main__":
    unittest.main()
