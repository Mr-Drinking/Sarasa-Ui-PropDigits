from __future__ import annotations

import io
import copy
import ast
from collections import Counter
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import audit_sarasa_ui_propdigits as audit
from font_audit import contract as audit_contract, metrics as audit_metrics, products as audit_products
import package_release as package
import python_env_bootstrap as bootstrap
import uharfbuzz as hb
from fontTools.fontBuilder import FontBuilder
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable


def fixture_font():
    order = ['.notdef', 'G', 'Galt', 'T', 'o', 'acutecomb']
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({ord('G'): 'G', ord('T'): 'T', ord('o'): 'o', 0x301: 'acutecomb'})
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0)); pen.lineTo((400, 0)); pen.lineTo((400, 700)); pen.lineTo((0, 700)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (0 if name == 'acutecomb' else 600, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupVerticalMetrics({name: (1000, 100) for name in order})
    builder.setupVerticalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'Probe', 'styleName': 'Regular'})
    builder.setupOS2(); builder.setupPost(); builder.setupMaxp()
    addOpenTypeFeaturesFromString(builder.font, '''
        languagesystem DFLT dflt;
        @Original = [G]; @Alternate = [Galt]; @AfterT = [T]; @AfterO = [o];
        feature kern { pos @Original @AfterT -30; pos @Alternate @AfterO -15; } kern;
        markClass acutecomb <anchor 0 0> @TOP;
        feature mark { pos base G <anchor 250 600> mark @TOP; pos base Galt <anchor 350 650> mark @TOP; } mark;
        feature cv10 { sub G by Galt; } cv10;
    ''')
    return builder.font


def hb_font(font):
    stream = io.BytesIO(); font.save(stream)
    return hb.Font(hb.Face(stream.getvalue()))


def positions(font, text, features):
    buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties()
    hb.shape(font, buffer, features)
    return [(p.x_advance, p.y_advance, p.x_offset, p.y_offset) for p in buffer.glyph_positions]


class PositioningRegressionTests(unittest.TestCase):
    def test_audit_top_level_definitions_have_unique_names(self):
        for path in [Path(audit.__file__), *sorted(Path(audit_contract.__file__).parent.glob('*.py'))]:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            names = Counter(node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            self.assertEqual({name: count for name, count in names.items() if count > 1}, {}, path.name)

    def test_vorg_removal_preserves_origins_and_real_composite_outlines(self):
        font = fixture_font()
        axis = b.weight_axis(b.load_inter(False)) if b.INTER_UPRIGHT.exists() else None
        if axis is None:
            self.skipTest('需先准备固定 Inter 来源')
        font['fvar'] = newTable('fvar'); font['fvar'].axes = [copy.deepcopy(axis)]; font['fvar'].instances = []
        font['gvar'] = newTable('gvar'); font['gvar'].version = 1; font['gvar'].reserved = 0; font['gvar'].variations = {}
        pen = TTGlyphPen(font.getGlyphSet()); pen.addComponent('G', (1, 0, 0, 1, 0, 0))
        composite = pen.glyph(); composite.components[0].flags |= b.glyf_table.USE_MY_METRICS
        font['glyf'].glyphs['Galt'] = composite
        coordinates = [(0, 0)] * b.gvar_coordinate_count(font, 'G')
        coordinates[-2:] = [(0, 10), (0, 10)]
        font['gvar'].variations['G'] = [b.TupleVariation({'wght': (0, 1, 1)}, coordinates)]
        vorg = font['VORG'] = newTable('VORG'); vorg.majorVersion = 1; vorg.minorVersion = 0; vorg.defaultVertOriginY = 800; vorg.VOriginRecords = {}
        before = {name: b.glyph_point_structure(font, name) for name in font.getGlyphOrder()}
        report = b.normalize_variable_vertical_origin(font)
        self.assertNotIn('VORG', font)
        self.assertEqual(report['vorgless_origin_mismatches'], 0)
        self.assertEqual(before, {name: b.glyph_point_structure(font, name) for name in font.getGlyphOrder()})

    def test_shared_variation_devices_are_remapped_once(self):
        device = b.ot.Device(); device.DeltaFormat = 0x8000
        device.StartSize = 0; device.EndSize = 1
        value = SimpleNamespace(first=device, second=device)
        b.remap_layout_variation_devices(value, {1: 0x10002})
        self.assertIs(value.first, value.second)
        self.assertEqual((value.first.StartSize, value.first.EndSize), (1, 2))

    def test_baking_keeps_alternate_kerning_and_mark_anchors(self):
        font = fixture_font()
        source = hb_font(font)
        b.bake_inter_feature_defaults(font)
        target = hb_font(font)
        for text in ('GT', 'Go', 'GG', 'oG', 'G\u0301'):
            with self.subTest(text=text):
                self.assertEqual(positions(target, text, {'cv10': False}), positions(source, text, {'cv10': True}))

    @staticmethod
    def curved_g(font, control_x, lsb):
        # One quadratic edge: control point at control_x, ink extreme at
        # (control_x + 100) / 2 when both on-curve ends sit at x = 100.
        pen = TTGlyphPen(None)
        pen.moveTo((100, 0)); pen.lineTo((300, 0)); pen.lineTo((300, 700)); pen.lineTo((100, 700))
        pen.qCurveTo((control_x, 350), (100, 0)); pen.closePath()
        font['glyf'].glyphs['G'] = pen.glyph()
        font['hmtx'].metrics['G'] = (600, lsb)

    @staticmethod
    def straight_g(font, x):
        pen = TTGlyphPen(None)
        pen.moveTo((x, 0)); pen.lineTo((300, 0)); pen.lineTo((300, 700)); pen.lineTo((x, 700)); pen.closePath()
        font['glyf'].glyphs['G'] = pen.glyph()
        font['hmtx'].metrics['G'] = (600, x)

    def test_nominal_audit_compares_ink_not_control_box(self):
        curved, straight, shifted = fixture_font(), fixture_font(), fixture_font()
        self.curved_g(curved, 60, 60)      # control xMin 60, ink 80
        self.straight_g(straight, 80)      # control xMin 80, ink 80
        self.straight_g(shifted, 83)       # ink moved by three units
        with tempfile.TemporaryDirectory() as raw:
            paths = {}
            for name, font in (('curved', curved), ('straight', straight), ('shifted', shifted)):
                paths[name] = Path(raw) / f'{name}.ttf'; font.save(paths[name])
            same_ink = audit_metrics.compare_nominal(audit_metrics.nominal_metrics(paths['curved']),
                                                     audit_metrics.nominal_metrics(paths['straight']))
            moved_ink = audit_metrics.compare_nominal(audit_metrics.nominal_metrics(paths['shifted']),
                                                      audit_metrics.nominal_metrics(paths['straight']))
        self.assertEqual(same_ink['horizontal_metrics']['failures'], 0)
        self.assertEqual(moved_ink['horizontal_metrics']['failures'], 1)

    def test_inter_lsb_targets_place_the_native_ink(self):
        # The native static reaches ink 75 with its control box; the product
        # outline has a control point 20 units outside its ink.
        product, native = fixture_font(), fixture_font()
        self.curved_g(product, 60, 60)
        self.straight_g(native, 75)
        targets = b.reference_cmap_metric_targets(product, native, 'hmtx')
        self.assertEqual(targets['G'], (600, 55))
        self.assertEqual(targets['T'], tuple(native['hmtx'].metrics['T']))

    def test_lsb_targets_follow_the_drawn_native_ink(self):
        # Sarasa's italic statics can keep hmtx LSB and the glyph header a
        # unit left of the points engines draw (K Light Italic ᄁ: 256, 257).
        product, native = fixture_font(), fixture_font()
        self.straight_g(product, 80)
        self.straight_g(native, 257)
        native['glyf'].glyphs['G'].xMin = 256
        native['hmtx'].metrics['G'] = (600, 256)
        targets = b.reference_cmap_metric_targets(product, native, 'hmtx')
        self.assertEqual(targets['G'], (600, 257))

    @staticmethod
    def top_t(font, control_y, tsb):
        # A quadratic top edge from (400, 700) to (0, 700): its ink peaks at
        # 700 + (control_y - 700) / 2, below a control point at control_y.
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0)); pen.lineTo((400, 0)); pen.lineTo((400, 700))
        if control_y == 700:
            pen.lineTo((0, 700))
        else:
            pen.qCurveTo((200, control_y), (0, 700))
        pen.closePath()
        font['glyf'].glyphs['T'] = pen.glyph()
        font['vmtx'].metrics['T'] = (1000, tsb)

    @staticmethod
    def flat_t(font, top, tsb):
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0)); pen.lineTo((400, 0)); pen.lineTo((400, top)); pen.lineTo((0, top)); pen.closePath()
        font['glyf'].glyphs['T'] = pen.glyph()
        font['vmtx'].metrics['T'] = (1000, tsb)

    def test_vmtx_targets_keep_the_native_ink_below_the_origin(self):
        # The native ink tops out at 730, 150 below its origin. The product
        # reaches 730 through a control point at 760, so an equal TSB would
        # lift its origin, and its ink, by 30 units.
        product, native = fixture_font(), fixture_font()
        self.top_t(product, 760, 150)
        self.flat_t(native, 730, 150)
        targets = b.reference_cmap_metric_targets(product, native, 'vmtx')
        self.assertEqual(targets['T'], (1000, 120))
        self.assertEqual(targets['o'], tuple(native['vmtx'].metrics['o']))

    def test_nominal_audit_compares_vertical_ink_not_control_box(self):
        curved, flat, lowered = fixture_font(), fixture_font(), fixture_font()
        self.top_t(curved, 760, 120)       # origin 880, ink top 730
        self.flat_t(flat, 730, 150)        # origin 880, ink top 730
        self.flat_t(lowered, 730, 153)     # ink three units further below
        with tempfile.TemporaryDirectory() as raw:
            paths = {}
            for name, font in (('curved', curved), ('flat', flat), ('lowered', lowered)):
                paths[name] = Path(raw) / f'{name}.ttf'; font.save(paths[name])
            same_ink = audit_metrics.compare_nominal(audit_metrics.nominal_metrics(paths['curved']),
                                                     audit_metrics.nominal_metrics(paths['flat']))
            moved_ink = audit_metrics.compare_nominal(audit_metrics.nominal_metrics(paths['lowered']),
                                                      audit_metrics.nominal_metrics(paths['flat']))
        self.assertEqual(same_ink['vertical_metrics']['failures'], 0)
        self.assertEqual(moved_ink['vertical_metrics']['failures'], 1)

    def test_runtime_audit_detects_static_vorg_conflict(self):
        canonical = fixture_font()
        runtime = hb_font(canonical)
        static = fixture_font()
        vorg = static['VORG'] = newTable('VORG')
        vorg.majorVersion = 1; vorg.minorVersion = 0
        vorg.defaultVertOriginY = 900; vorg.VOriginRecords = {}
        with tempfile.TemporaryDirectory() as raw:
            canonical_path, static_path = Path(raw) / 'canonical.ttf', Path(raw) / 'static.ttf'
            canonical.save(canonical_path); static.save(static_path)
            expected = audit_metrics.nominal_metrics(canonical_path)
            result = audit_metrics.compare_nominal(audit_metrics.nominal_metrics(static_path), expected)
            self.assertGreater(result['vertical_metrics']['failures'], 0)
            before = static.getTableData('glyf')
            b.normalize_static_vertical_origin(static)
            self.assertEqual(before, static.getTableData('glyf'))
            static.save(static_path)
            result = audit_metrics.compare_nominal(audit_metrics.nominal_metrics(static_path), expected)
            self.assertEqual([row['failures'] for row in result.values()], [0, 0])

    def test_ordered_presets_follow_the_selected_glyph_identity(self):
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt;
            feature ss03 { sub G by T; } ss03;
            feature cv10 { sub G by Galt; } cv10;
        ''')
        outlines = font.getTableData('glyf')
        b.bake_inter_feature_defaults(font)
        self.assertEqual(font.getBestCmap()[ord('G')], 'T')
        self.assertEqual(font.getTableData('glyf'), outlines)

    def test_real_inter_merge_keeps_variations_and_mark_filtering_sets(self):
        if not b.INTER_UPRIGHT.exists():
            self.skipTest('需先准备固定 Inter 来源')
        inter = b.load_inter(False)
        base = fixture_font()
        for table in base['cmap'].tables:
            table.cmap = {}
        # Leave existing lookups to exercise index-offset remapping, but remove
        # their feature routes so the fixture has no unrelated active kerning.
        base['GPOS'].table.FeatureList.FeatureRecord = []
        base['GPOS'].table.FeatureList.FeatureCount = 0
        for script in base['GPOS'].table.ScriptList.ScriptRecord:
            script.Script.DefaultLangSys.FeatureIndex = []
            script.Script.DefaultLangSys.FeatureCount = 0
        base['fvar'] = copy.deepcopy(inter['fvar'])
        base['fvar'].axes[0].minValue = 250
        base['gvar'] = newTable('gvar')
        base['gvar'].version = 1; base['gvar'].reserved = 0; base['gvar'].variations = {}
        sets = b.ot.MarkGlyphSetsDef(); sets.MarkSetTableFormat = 1; sets.Coverage = []
        for name in ('G', 'T'):
            coverage = b.ot.Coverage(); coverage.glyphs = [name]; sets.Coverage.append(coverage)
        sets.MarkSetCount = 2; base['GDEF'].table.MarkGlyphSetsDef = sets; base['GDEF'].table.Version = 0x00010002
        b.apply_public_weight_axis(base)
        b.append_inter_glyphs(base, inter, set(inter.getBestCmap()))
        b.import_inter_layout_features(base, inter)
        b.subset_to_current_cmap(base)
        b.rebuild_gdef_from_reference(base, fixture_font())
        b.subset_to_current_cmap(base)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'merged.ttf'
            base.save(path)
            # This fixture merges layout only; curve and origin alignment for
            # intermediate weights is exercised by the release audit on real VFs.
            result = audit_products.positioning_invariants(path, weights=(200, 400, 900))
        self.assertEqual(result['failures'], 0, result['samples'][:4])


class SourceAndEnvironmentRegressionTests(unittest.TestCase):
    def test_packaging_rejects_stale_audit_hashes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); (root / 'reports').mkdir()
            fonts = [root / f'font-{i}.ttf' for i in range(156)]
            manifest = {str(path): {'sha256': 'current', 'size': 1} for path in fonts}
            (root / 'reports/release-audit.json').write_text('{"input_manifest": {}}', encoding='utf-8')
            packages = [package.Package('probe.zip', tuple((path, path.name) for path in fonts))]
            with patch.object(package, 'ROOT', root), patch.object(audit_contract, 'release_font_paths', return_value=fonts), patch.object(audit_contract, 'audit_input_manifest', return_value=manifest):
                with self.assertRaisesRegex(ValueError, 'SHA-256'):
                    package.validate_release_audits(packages)

    def test_cached_extracted_zip_member_must_match_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); archive = root / 'source.zip'; output = root / 'font.ttf'
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('folder/font.ttf', b'authentic')
            b.extract_zip_basename(archive, 'font.ttf', output)
            output.write_bytes(b'corrupted')
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                b.extract_zip_basename(archive, 'font.ttf', output)

    def test_explicit_venv_and_inherited_marker_cannot_select_current_environment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); target = root / 'private'
            executable = bootstrap.venv_python(target)
            executable.parent.mkdir(parents=True); executable.touch()
            owner, method = (bootstrap.subprocess, 'call') if os.name == 'nt' else (bootstrap.os, 'execve')
            with patch.dict(os.environ, {'SARASA_PYTHON_VENV': str(target), 'SARASA_PYTHON_ENV_ACTIVE': '1', 'SARASA_SKIP_PYTHON_DEPS': '0'}), patch.object(bootstrap, 'missing_dependencies', return_value=[]), patch.object(owner, method, side_effect=RuntimeError('switched')) as switch, patch.object(bootstrap.subprocess, 'check_call') as install:
                with self.assertRaisesRegex(RuntimeError, 'switched'):
                    bootstrap.ensure_project_python({}, project_root=root, label='test')
                actual = switch.call_args.args[0][0] if os.name == 'nt' else switch.call_args.args[0]
                self.assertEqual(Path(actual).resolve(), executable.resolve())
                install.assert_not_called()

    def test_partial_region_prepares_full_hint_sources_before_cache_key(self):
        with patch.object(b, 'ensure_reference_sarasa'), patch.object(b, 'ensure_sarasa_source_tree'), patch.object(b, 'ensure_classical_static_sources') as classical, patch.dict(os.environ, {'SARASA_SKIP_SOURCE_BOOTSTRAP': '0'}):
            b.ensure_build_sources(True, ['SC'])
            classical.assert_called_once_with(b.REGION_ORDER)
            classical.reset_mock()
            b.ensure_build_sources(True, ['SC', 'TC'], full_hint_group=False)
            classical.assert_called_once_with(['SC', 'TC'])

    def test_document_only_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); files = []
            for name in package.REQUIRED_DOCUMENTS:
                path = root / name; path.write_text('document', encoding='utf-8'); files.append((path, name))
            with self.assertRaisesRegex(ValueError, '不包含字体'):
                package.validate_package_sources(package.Package('empty.zip', tuple(files)))

    def test_empty_static_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(b, 'static_dir', return_value=Path(raw)):
            (Path(raw) / 'README.txt').write_text('document', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '清单不完整'):
                package.static_files('SC', False)


if __name__ == '__main__':
    unittest.main()
