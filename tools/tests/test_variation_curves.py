import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import font_variations
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables._f_v_a_r import Axis
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib.models import VariationModel, supportScalar
from fontTools.varLib.instancer import instantiateVariableFont
import uharfbuzz as hb
import freetype
from test_release_runtime_controls import variable_fixture


def runtime(font):
    data = io.BytesIO(); font.save(data)
    return hb.Font(hb.Face(data.getvalue()))


def vertical_tone_font(vertical_target, vertical_metrics, *, variable=False):
    # Positional post names, as in Sarasa releases and project static builds.
    order = ['.notdef', 'uni302E', 'glyph00002', 'glyph00003']
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({0x302E: 'uni302E'})
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        pen.moveTo((-190, 285)); pen.lineTo((0, 285)); pen.lineTo((0, 475)); pen.lineTo((-190, 475)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (920, -190) for name in order})
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    builder.setupVerticalMetrics({'.notdef': (1000, 405), 'uni302E': (0, 428), **vertical_metrics})
    builder.setupVerticalHeader(ascent=500, descent=-500)
    builder.setupNameTable({'familyName': 'Probe', 'styleName': 'Regular'})
    builder.setupOS2(); builder.setupPost(); builder.setupMaxp()
    addOpenTypeFeaturesFromString(builder.font, f'languagesystem DFLT dflt; feature vert {{ sub uni302E by {vertical_target}; }} vert;')
    font = builder.font
    if variable:
        axis = Axis(); axis.axisTag = 'wght'; axis.minValue = 200; axis.defaultValue = 400; axis.maxValue = 900; axis.flags = 0; axis.axisNameID = 256
        font['fvar'] = newTable('fvar'); font['fvar'].axes = [axis]; font['fvar'].instances = []
        font['gvar'] = newTable('gvar'); font['gvar'].version = 1; font['gvar'].reserved = 0; font['gvar'].variations = {}
    return font


class VariationCurveTests(unittest.TestCase):
    def test_origin_change_at_float32_half_boundary_preserves_the_drawn_shape(self):
        # Two-glyph OFL fixture retains the failing source curve and copyright.
        # Its maximum x crosses 522.4999389648438 -> 522.5 at weight 325.
        font = TTFont(recalcBBoxes=False, recalcTimestamp=False)
        font.importXML(Path(__file__).parent / 'fixtures/origin-rounding.ttx')
        self.addCleanup(font.close)
        report = b.normalize_cross_engine_metrics(font)
        observations = report['runtime_extent_rounding_observations']
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['weight'], 325)
        self.assertLessEqual(observations[0]['maximum_draw_difference'], observations[0]['float32_ulp'])
        self.assertLess(observations[0]['maximum_draw_difference'], .001)
        self.assertEqual(report['runtime_metric_preservation_mismatches'], 0)

    def test_runtime_calibration_checks_advances_as_well_as_side_bearings(self):
        font = variable_fixture()
        horizontal = [b.horizontal_glyph_metrics(font, name) for name in font.getGlyphOrder()]
        vertical = [b.vertical_glyph_metrics(font, name) for name in font.getGlyphOrder()]
        low = list(horizontal); low[1] = (520, 0, 120)
        model = VariationModel([{}, {'wght': -1}], axisOrder=['wght'])
        b.build_direct_hvar(font, model, [horizontal, low], horizontal)
        vdata = b.build_direct_vvar(font, model, [vertical, vertical], vertical)
        desired = list(low); desired[1] = (519, 0, 119)
        htargets = {weight: {name: [row[index] for row in values] for index, name in enumerate(('advance', 'lsb', 'rsb'))}
                    for weight, values in ((200, desired), (400, horizontal))}
        vtargets = {weight: {name: [row[index] for row in vertical] for index, name in enumerate(('advance', 'tsb', 'bsb', 'vorg'))}
                    for weight in (200, 400)}
        result = b.calibrate_harfbuzz_metric_controls(font, [200, 400], htargets, vtargets, vdata, {200: 0})
        self.assertEqual(result['harfbuzz_advance_corrections'], 1)
        actual = runtime(font); actual.set_variations({'wght': 200})
        self.assertEqual(actual.get_glyph_h_advance(1), 519)

    def test_direct_advance_curves_keep_native_controls_and_intermediate_source_values(self):
        source = variable_fixture()
        source['avar'] = newTable('avar'); source['avar'].segments = {'wght': {-1: -1, 0: 0, 1: 1}}
        base = [b.horizontal_glyph_metrics(source, name) for name in source.getGlyphOrder()]
        low, high = list(base), list(base)
        low[1] = (520, 0, 120); high[1] = (700, 0, 300)
        model = VariationModel([{}, {'wght': -1}, {'wght': 1}], axisOrder=['wght'])
        b.build_direct_hvar(source, model, [base, low, high], base)
        target = copy.deepcopy(source)
        target['avar'].segments = {'wght': {-1: -1, -.5: -.7, -.25: -.45, 0: 0, 1: 1}}
        store, names, weights = font_variations.advance_curve_store(target, source, {'G': 'G'}, {200, 400, 900})
        hvar = target['HVAR'].table
        hvar.VarStore, added = b.append_item_variation_store(hvar.VarStore, store, [0])
        mapping = {name: gid for gid, name in enumerate(target.getGlyphOrder())}
        mapping.update(zip(names, added))
        hvar.AdvWidthMap = b.var_builder.buildVarIdxMap([mapping[name] for name in target.getGlyphOrder()], target.getGlyphOrder())
        expected, actual = runtime(source), runtime(target)
        stream = io.BytesIO(); target.save(stream)
        face = freetype.Face(io.BytesIO(stream.getvalue()))
        gid = target.getGlyphID('G')
        for weight in range(200, 901):
            expected.set_variations({'wght': weight}); actual.set_variations({'wght': weight})
            face.set_var_design_coords([weight])
            face.load_glyph(gid, freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_HINTING)
            self.assertLessEqual(abs(actual.get_glyph_h_advance(gid) - expected.get_glyph_h_advance(gid)), 1, weight)
            self.assertEqual(face.glyph.metrics.horiAdvance, actual.get_glyph_h_advance(gid), weight)
        self.assertIn(350, weights)

    def test_source_advance_curve_survives_a_different_avar_at_hidden_and_probe_weights(self):
        source = variable_fixture()
        source['avar'] = newTable('avar'); source['avar'].segments = {'wght': {-1: -1, 0: 0, .8: .76, 1: 1}}
        target = copy.deepcopy(source)
        target['avar'].segments = {'wght': {-1: -1, -.5: -.7, -.25: -.45, 0: 0, 1: 1}}
        points = len(source['glyf']['G'].coordinates) + 4
        low = [(0, 0)] * points; low[-3] = (-80, 0)
        high = [(0, 0)] * points; high[-3] = (100, 0)
        source['gvar'].variations['G'] = [TupleVariation({'wght': (-1, -1, 0)}, low),
                                        TupleVariation({'wght': (0, 1, 1)}, high)]
        original = runtime(source)
        report = font_variations.remap_gvar(source, target)
        source['avar'] = copy.deepcopy(target['avar'])
        actual = runtime(source); gid = source.getGlyphID('G')
        for weight in range(200, 901, 5):
            original.set_variations({'wght': weight}); actual.set_variations({'wght': weight})
            self.assertLessEqual(abs(actual.get_glyph_h_advance(gid) - original.get_glyph_h_advance(gid)), 1, weight)
        with instantiateVariableFont(source, {'wght': 350}) as instance:
            self.assertEqual(instance['hmtx'].metrics['G'][0], 580)
        self.assertTrue(any(abs(weight - 800) < .05 for weight in report['inter_curve_breakpoints']))
        weights, _locations = b.metric_variation_control_locations(source)
        self.assertIn(350, weights); self.assertIn(800, weights)

    def test_vertical_form_metrics_resolve_in_each_reference_glyph_order(self):
        # glyph00002 is the vertical tone mark in Regular but an unrelated
        # full-width glyph in the Heavy project build, whose glyph order differs.
        regular = vertical_tone_font('glyph00002', {'glyph00002': (0, 428), 'glyph00003': (1000, 33)})
        heavy = vertical_tone_font('glyph00003', {'glyph00002': (1000, 33), 'glyph00003': (0, 405)})
        font = vertical_tone_font('glyph00002', {'glyph00002': (0, 428), 'glyph00003': (1000, 33)}, variable=True)
        report = b.align_reference_vmtx_variations(font, {400: regular, 900: heavy}, set())
        self.assertEqual(report['reference_vmtx_variation_corrections'], 1)
        self.assertEqual(b.glyph_vmtx_at_weight(font, 900)['glyph00002'], (0, 405))
        self.assertEqual(b.glyph_vmtx_at_weight(font, 400)['glyph00002'], (0, 428))

    def test_exact_curve_keeps_every_control_within_half_a_unit(self):
        # A 2048-UPM source scaled to 1000 UPM rounds its default and deltas;
        # sampling that rounded copy drifts from the real curve at controls.
        original = variable_fixture()
        scale_upem(original, 2048)
        original['avar'] = newTable('avar'); original['avar'].segments = {'wght': {-1: -1, 0: 0, .6: .55, 1: 1}}
        points = len(original['glyf']['G'].coordinates) + 4
        low = [(37 + 11 * index, 5 * index) for index in range(points - 4)] + [(0, 0)] * 4
        high = [(113 - 7 * index, -9 * index) for index in range(points - 4)] + [(0, 0)] * 4
        middle = [(-23 + 3 * index, 13) for index in range(points - 4)] + [(0, 0)] * 4
        original['gvar'].variations['G'] = [TupleVariation({'wght': (-1, -1, 0)}, low),
                                            TupleVariation({'wght': (0, 1, 1)}, high),
                                            TupleVariation({'wght': (0, .55, 1)}, middle)]
        stream = io.BytesIO(); original.save(stream); stream.seek(0)
        original = TTFont(stream)
        target = variable_fixture()
        target['avar'] = newTable('avar'); target['avar'].segments = {'wght': {-1: -1, -.5: -.59, -.25: -.18, 0: 0, .4: .28, .6: .64, 1: 1}}
        exact = font_variations.ExactCurve(original, 1000 / 2048)

        def worst(use_exact):
            rounded = copy.deepcopy(original); scale_upem(rounded, 1000)
            font_variations.remap_gvar(rounded, target, exact=exact if use_exact else None)
            rounded['avar'] = copy.deepcopy(target['avar'])
            coords, controls = rounded['glyf']._getCoordinatesAndControls('G', rounded['hmtx'].metrics, None)
            errors = []
            for weight in (200, 300, 350, 400, 600, 700, 900):
                location = {'wght': font_variations.coordinate(rounded, weight)}
                total = GlyphCoordinates(coords)
                for item in rounded['gvar'].variations['G']:
                    scalar = supportScalar(location, item.axes)
                    if scalar:
                        variation = copy.deepcopy(item); variation.calcInferredDeltas(coords, controls.endPts)
                        total += GlyphCoordinates(variation.coordinates) * scalar
                truth = exact.points('G', weight)
                errors.append(max(max(abs(x - u), abs(y - v)) for (x, y), (u, v) in zip(list(total)[:-4], truth)))
            return max(errors)

        self.assertLessEqual(worst(True), .5)
        self.assertGreater(worst(False), .5)

    def test_outline_corrections_compare_the_unrounded_product_curve(self):
        # At 700 the product's left edge reaches 3.6 and Inter's original
        # curve 3.3: rounded they are a unit apart (4 and 3), in fact 0.3.
        def corrections(inter_delta):
            product = variable_fixture()
            coordinates = [(6, 0), (0, 0), (0, 0), (6, 0)] + [(0, 0)] * 4
            product['gvar'].variations['G'] = [TupleVariation({'wght': (0, 1, 1)}, coordinates)]
            original = variable_fixture()
            scale_upem(original, 2000)
            coordinates = [(inter_delta, 0), (0, 0), (0, 0), (inter_delta, 0)] + [(0, 0)] * 4
            original['gvar'].variations['G'] = [TupleVariation({'wght': (0, 1, 1)}, coordinates)]
            with tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'original.ttf'; original.save(path)
                inter = TTFont(path); scale_upem(inter, 1000)
                inter._sarasa_exact_source = path
                report = b.add_inter_outline_correction_variations(product, inter)
                inter.close()
            coords, _controls = product['glyf']._getCoordinatesAndControls('G', product['hmtx'].metrics, None)
            lefts = {}
            for weight in b.INTER_COMPOSITE_CONTROL_WEIGHTS:
                location = {'wght': font_variations.coordinate(product, weight)}
                total = GlyphCoordinates(coords)
                for item in product['gvar'].variations['G']:
                    total += GlyphCoordinates(item.coordinates) * supportScalar(location, item.axes)
                lefts[weight] = total[0][0] - coords[0][0]
            return report['inter_outline_correction_variations_added'], lefts

        added, lefts = corrections(11)
        self.assertEqual(added, 0)
        self.assertAlmostEqual(lefts[700], 3.6)
        # Inter at 4.2 at 700: a real 0.6 unit miss there, 0.8 at 800 and a
        # unit at 900. Each control weight is corrected on its own.
        added, lefts = corrections(14)
        self.assertEqual(added, 3)
        self.assertAlmostEqual(lefts[700], 4.6, places=3)     # the support peak is F2Dot14
        for weight, left in lefts.items():
            self.assertLessEqual(abs(left - 7 * max(0, weight - 400) / 500), 0.5 + 1e-3, weight)

    def test_composite_offsets_follow_the_unrounded_inter_curve(self):
        # At 700 the product's acute sits at 303.6 and Inter's at 305.4: the
        # base keeps its place and the acute follows Inter, 1.8 units over.
        def with_composite(font, offset, delta):
            pen = TTGlyphPen(font.getGlyphSet()); pen.addComponent('G', (1, 0, 0, 1, 0, 0))
            pen.addComponent('acutecomb', (1, 0, 0, 1, offset, 0))
            font['glyf'].glyphs['Gacute'] = pen.glyph(); font.setGlyphOrder(font.getGlyphOrder() + ['Gacute'])
            font['hmtx'].metrics['Gacute'] = font['hmtx'].metrics['G']
            font['vmtx'].metrics['Gacute'] = font['vmtx'].metrics['G']
            font['gvar'].variations['Gacute'] = [TupleVariation({'wght': (0, 1, 1)}, [(0, 0), (delta, 0)] + [(0, 0)] * 4)]
            for name in font.getGlyphOrder():
                font['gvar'].variations.setdefault(name, [])
            return font

        product = with_composite(variable_fixture(), 300, 6)
        original = variable_fixture()
        scale_upem(original, 2000)
        with_composite(original, 600, 18)
        with tempfile.TemporaryDirectory() as raw, patch.dict(b.INTER_AXIS_LIMIT, {'wght': b.INTER_AXIS_LIMIT['wght']}, clear=True):
            path = Path(raw) / 'original.ttf'; original.save(path)
            inter = TTFont(path); scale_upem(inter, 1000)
            inter._sarasa_exact_source = path
            pending, _bounds, _status, unsupported = b.inter_composite_corrections_at_weight(
                product, inter, [('Gacute', 'Gacute')], 700)
            inter.close()
        self.assertEqual(unsupported, [])
        self.assertEqual(pending, [('Gacute', [(0, 0), (2, 0)])])

    def test_composite_children_keep_inter_spacing_after_their_own_alignment(self):
        # Each child rounds its own alignment: at 700 the product's G sits
        # 0.6 left of Inter's and its acute 0.6 right. Inter's offsets alone
        # would keep the glyph 1.2 units wider (½ was two units narrow).
        def with_composite(font, offset, shifts):
            for name, shift in shifts.items():
                count = len(font['glyf'][name].coordinates)
                font['gvar'].variations[name] = [TupleVariation({'wght': (0, 1, 1)}, [(shift, 0)] * count + [(0, 0)] * 4)]
            pen = TTGlyphPen(font.getGlyphSet()); pen.addComponent('G', (1, 0, 0, 1, 0, 0))
            pen.addComponent('acutecomb', (1, 0, 0, 1, offset, 0))
            font['glyf'].glyphs['Gacute'] = pen.glyph(); font.setGlyphOrder(font.getGlyphOrder() + ['Gacute'])
            font['hmtx'].metrics['Gacute'] = font['hmtx'].metrics['G']
            font['vmtx'].metrics['Gacute'] = font['vmtx'].metrics['G']
            for name in font.getGlyphOrder():
                font['gvar'].variations.setdefault(name, [])
            return font

        product = with_composite(variable_fixture(), 300, {'G': -1, 'acutecomb': 1})
        original = variable_fixture()
        scale_upem(original, 2000)
        with_composite(original, 600, {})
        with tempfile.TemporaryDirectory() as raw, patch.dict(b.INTER_AXIS_LIMIT, {'wght': b.INTER_AXIS_LIMIT['wght']}, clear=True):
            path = Path(raw) / 'original.ttf'; original.save(path)
            inter = TTFont(path); scale_upem(inter, 1000)
            inter._sarasa_exact_source = path
            pending, _bounds, _status, unsupported = b.inter_composite_corrections_at_weight(
                product, inter, [('Gacute', 'Gacute')], 700)
            inter.close()
        self.assertEqual(unsupported, [])
        # G keeps the left edge its alignment gave the glyph; the acute moves.
        self.assertEqual(pending, [('Gacute', [(0, 0), (-1, 0)])])

    def test_component_ink_ignores_an_origin_carried_by_phantom_points(self):
        # Before normalize_cross_engine_metrics a child's alignment can still
        # live in its phantom points. Drawn alone it shifts; inside the
        # composite it does not, and corrections measured alone moved ḩ's h
        # the wrong way at 300.
        font = variable_fixture()
        pen = TTGlyphPen(font.getGlyphSet()); pen.addComponent('G', (1, 0, 0, 1, 0, 0))
        pen.addComponent('acutecomb', (1, 0, 0, 1, 300, 0))
        font['glyf'].glyphs['Gacute'] = pen.glyph(); font.setGlyphOrder(font.getGlyphOrder() + ['Gacute'])
        font['hmtx'].metrics['Gacute'] = font['hmtx'].metrics['G']
        font['vmtx'].metrics['Gacute'] = font['vmtx'].metrics['G']
        count = len(font['glyf']['G'].coordinates)
        font['gvar'].variations['G'] = [TupleVariation({'wght': (0, 1, 1)}, [None] * count + [(5, 0), (5, 0), (0, 0), (0, 0)])]
        for name in font.getGlyphOrder():
            font['gvar'].variations.setdefault(name, [])
        glyph_set = font.getGlyphSet(location={'wght': 900})
        alone = BoundsPen(glyph_set); glyph_set['G'].draw(alone)
        placed = b.component_layout(glyph_set, 'Gacute')
        self.assertEqual(alone.bounds[0], font['glyf']['G'].xMin - 5)
        self.assertEqual(placed[0], ((0, 0), (font['glyf']['G'].xMin, font['glyf']['G'].yMin,
                                             font['glyf']['G'].xMax, font['glyf']['G'].yMax)))

    def test_hvar_only_advance_data_is_carried_into_phantom_points(self):
        source = variable_fixture(); target = copy.deepcopy(source)
        base = [b.horizontal_glyph_metrics(source, name) for name in source.getGlyphOrder()]
        low = list(base); low[1] = (520, 0, 120)
        model = VariationModel([{}, {'wght': -1}], axisOrder=['wght'])
        b.build_direct_hvar(source, model, [base, low], base)
        original = runtime(source)
        font_variations.remap_gvar(source, target)
        del source['HVAR']
        actual = runtime(source)
        for weight in (200, 250, 300, 325, 350, 375, 400):
            original.set_variations({'wght': weight}); actual.set_variations({'wght': weight})
            self.assertEqual(actual.get_glyph_h_advance(1), original.get_glyph_h_advance(1))


if __name__ == '__main__':
    unittest.main()
