from __future__ import annotations

import copy
import io
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import audit_sarasa_ui_propdigits as a
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from test_runtime_regressions import fixture_font
from test_release_runtime_controls import variable_fixture


def dash_fixture(italic=False, continuous=True):
    font = fixture_font()
    names = ['dash', 'full', 'two', 'three', 'fulltwo', 'fullthree', 'vertical', 'verticaltwo', 'verticalthree']
    for name in names:
        b.clone_glyph(font, 'G', name)
    for table in font['cmap'].tables:
        if table.isUnicode():
            table.cmap.update({0x2014: 'dash', 0x2015: 'full', 0x2E3A: 'two', 0x2E3B: 'three', 0xFE31: 'vertical'})
    for name, low, high in [('vertical', -48, 808), ('verticaltwo', -548, 1308), ('verticalthree', -1048, 1808)]:
        font['glyf'].glyphs[name] = b.make_polygon_glyph([(479, low), (521, low), (521, high), (479, high)])
        font['vmtx'].metrics[name] = (1000 if name == 'vertical' else 2000 if name == 'verticaltwo' else 3000, 72)
        font['hmtx'].metrics[name] = (1000, 479)
    addOpenTypeFeaturesFromString(font, '''
        languagesystem DFLT dflt;
        languagesystem latn dflt;
        feature ccmp {
            sub dash dash dash by three; sub dash dash by two;
            sub full full full by fullthree; sub full full by fulltwo;
        } ccmp;
        feature vert { sub full by vertical; sub fulltwo by verticaltwo; sub fullthree by verticalthree; } vert;
        feature vrt2 { sub full by vertical; sub fulltwo by verticaltwo; sub fullthree by verticalthree; } vrt2;
    ''')
    if italic:
        b.shear_font(font, 9.4)
    roles = b.upstream_dash_roles(font)
    b.ensure_vertical_presentation_dash_ligatures(font, roles)
    b.create_vertical_dash_segments(font, roles, italic)
    if continuous:
        b.install_continuous_vertical_dash_layout(font)
    b.recalculate_head_bounds(font)
    return font


class VerticalSegmentTests(unittest.TestCase):
    def test_vertical_joining_is_not_mistaken_for_legacy_calt(self):
        font = dash_fixture()
        self.assertEqual(b.em_dash_continuation_glyphs(font), [])
        self.assertEqual(b.em_dash_pair_start_glyphs(font), [])
        indices = b.install_continuous_vertical_dash_layout(font)['vertical_join_substitution_lookups']
        b.append_gsub_feature(font, 'calt', indices)
        self.assertEqual(b.em_dash_chain_lookup_indices(font), {indices[0]})
        self.assertEqual(b.em_dash_pair_start_chain_lookup_indices(font), {indices[1]})
        segments = b.vertical_dash_segments(font, b.upstream_dash_roles(font))
        self.assertEqual(b.em_dash_continuation_glyphs(font), [segments['end2']])
        self.assertEqual(b.em_dash_pair_start_glyphs(font), [segments['begin']])

    def test_hint_control_covers_unencoded_vertical_segments(self):
        font = dash_fixture()
        segments = b.vertical_dash_segments(font, b.upstream_dash_roles(font))
        expected = {font.getGlyphID(name) for name in segments.values()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pass1.project.ttf'
            font.save(path)
            actual = {int(line.split('@')[1]) for line in b.ui_hint_control(path).splitlines()}
        self.assertEqual(actual, expected)
        self.assertNotIn(font.getGlyphID('G'), actual)

    def test_variable_segments_reproduce_native_controls_and_keep_shared_edges(self):
        references = {}
        widths = {200: 34, 300: 38, 400: 42, 600: 44, 700: 46, 900: 50}
        for weight, width in widths.items():
            reference = dash_fixture()
            for name in b.vertical_dash_segments(reference, b.upstream_dash_roles(reference)).values():
                glyph = reference['glyf'][name]
                glyph.flags[0] |= b.glyf_table.flagOverlapSimple
                glyph.coordinates[:] = [(500 - width//2 if x < 500 else 500 + width//2, y) for x,y in glyph.coordinates]
                glyph.recalcBounds(reference['glyf'])
                reference['hmtx'].metrics[name]=(1000,glyph.xMin)
            references[weight]=reference
        font=copy.deepcopy(references[400]);variable=variable_fixture()
        font['fvar']=copy.deepcopy(variable['fvar'])
        font['gvar']=b.newTable('gvar');font['gvar'].version=1;font['gvar'].reserved=0
        font['gvar'].variations={name:[] for name in font.getGlyphOrder()}
        b.align_vertical_dash_geometry_to_static(font,references)
        b.install_continuous_vertical_dash_layout(font)
        for name in b.vertical_dash_segments(font,b.upstream_dash_roles(font)).values():
            self.assertTrue(font['glyf'][name].flags[0] & b.glyf_table.flagOverlapSimple)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'variable-segments.ttf';font.save(path)
            runtime=a.hb.Font(a.hb.Face(path.read_bytes()))
            name=b.vertical_dash_segments(font,b.upstream_dash_roles(font))['begin']
            for weight,width in widths.items():
                runtime.set_variations({'wght':weight})
                self.assertEqual(runtime.get_glyph_extents(font.getGlyphID(name)).width,width)
            for weight in a.INTER_POSITION_WEIGHTS:
                self.assertTrue(a.vertical_segment_runtime_status(path,{'wght':weight})['ok'],weight)

    def test_metric_calibration_includes_unencoded_segments_after_post3(self):
        target = dash_fixture(True)
        source = copy.deepcopy(target)
        b.normalize_static_post_table(source)
        data=io.BytesIO(); source.save(data); data.seek(0)
        with b.TTFont(data) as reference:
            target_names=b.vertical_dash_segments(target,b.upstream_dash_roles(target))
            reference_names=b.vertical_dash_segments(reference,b.upstream_dash_roles(reference))
            for tag in ('hmtx','vmtx'):
                for name in target_names.values():
                    advance,bearing=target[tag].metrics[name]
                    target[tag].metrics[name]=(advance,bearing+8)
                metrics=b.reference_cmap_metric_targets(target,reference,tag)
                for role,name in target_names.items():
                    self.assertEqual(metrics[name],reference[tag].metrics[reference_names[role]])

    def test_both_styles_have_1em_ink_cells_exact_joins_and_real_head_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            for italic in (False, True):
                font = dash_fixture(italic)
                path = Path(directory) / f'{italic}.ttf'; font.save(path)
                status = a.vertical_segment_runtime_status(path)
                self.assertTrue(status['ok'], status)
                self.assertEqual(status['cases'], 4200)
                self.assertEqual(font['head'].yMax, 880)
                self.assertEqual(font['head'].yMin, -120)
                self.assertTrue(a.head_bounds_status(font)['ok'])

    def test_runtime_rejects_an_actual_vertical_origin_error(self):
        font = dash_fixture()
        segment = b.vertical_dash_segments(font, b.upstream_dash_roles(font))['begin']
        advance, bearing = font['vmtx'].metrics[segment]
        font['vmtx'].metrics[segment] = (advance, bearing + 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wrong-origin.ttf'; font.save(path)
            status = a.vertical_segment_runtime_status(path)
            self.assertFalse(status['ok'])
            self.assertTrue(any('ink exceeds its own vertical cell' in row['reasons'] for row in status['samples']))

    def test_pass2_relinking_preserves_prepared_outlines_and_programs(self):
        font = dash_fixture()
        before = {name: copy.deepcopy(b.glyph_point_structure(font, name)) for name in font.getGlyphOrder()}
        multiple = {index for index, lookup in enumerate(font['GSUB'].table.LookupList.Lookup) if lookup.LookupType == 2}
        for record in font['GSUB'].table.FeatureList.FeatureRecord:
            record.Feature.LookupListIndex = [index for index in record.Feature.LookupListIndex if index not in multiple]
            record.Feature.LookupCount = len(record.Feature.LookupListIndex)
        self.assertIsNone(b.vertical_dash_segments(font, b.upstream_dash_roles(font)))
        b.restore_prepared_vertical_segments(font)
        self.assertIsNotNone(b.vertical_dash_segments(font, b.upstream_dash_roles(font)))
        self.assertEqual(before, {name: b.glyph_point_structure(font, name) for name in font.getGlyphOrder()})

    def test_missing_encoded_long_dash_rule_cannot_pass_runtime(self):
        font = dash_fixture()
        source = font.getBestCmap()[0x2E3B]
        for lookup in font['GSUB'].table.LookupList.Lookup:
            if lookup.LookupType == 2:
                for part in lookup.SubTable:
                    part.mapping.pop(source, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing-rule.ttf'; font.save(path)
            self.assertFalse(a.vertical_segment_runtime_status(path)['ok'])

    def test_old_grouped_layout_fails_four_dash_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.ttf'
            dash_fixture(continuous=False).save(path)
            with patch.object(a, 'VERTICAL_DASH_RUN_TEXTS', ('——', '———')):
                self.assertTrue(a.vertical_segment_runtime_status(path)['ok'])
            with patch.object(a, 'VERTICAL_DASH_RUN_TEXTS', ('————',)):
                self.assertFalse(a.vertical_segment_runtime_status(path)['ok'])

    def test_joining_is_idempotent_and_keeps_all_existing_outlines(self):
        for italic in (False, True):
            with self.subTest(italic=italic):
                font = dash_fixture(italic, continuous=False)
                outlines = {name: b.glyph_point_structure(font, name) for name in font.getGlyphOrder()}
                b.install_continuous_vertical_dash_layout(font)
                tables = {tag: font.getTableData(tag) for tag in ('GSUB', 'GPOS')}
                b.install_continuous_vertical_dash_layout(font)
                self.assertEqual(tables, {tag: font.getTableData(tag) for tag in tables})
                self.assertEqual(outlines, {name: b.glyph_point_structure(font, name) for name in font.getGlyphOrder()})

    def test_lazy_variable_store_preservation_compares_records_not_container_identity(self):
        path = a.vf_path('CL', False)
        if not path.is_file():
            self.skipTest('需要已构建的 CL VF 夹具')
        with b.TTFont(path, lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
            result = b.install_continuous_vertical_dash_layout(font)
            self.assertTrue(result['existing_layout_variations_preserved'])
            before = {tag: font.getTableData(tag) for tag in ('GSUB', 'GPOS', 'GDEF')}
            b.install_continuous_vertical_dash_layout(font)
            self.assertEqual(before, {tag: font.getTableData(tag) for tag in before})

    def test_long_runs_are_not_implemented_as_a_finite_ligature_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'long.ttf'
            dash_fixture(True).save(path)
            with patch.object(a, 'VERTICAL_DASH_RUN_TEXTS', ('—' * 1024,)):
                self.assertTrue(a.vertical_segment_runtime_status(path)['ok'])


if __name__ == '__main__':
    unittest.main()
