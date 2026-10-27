import copy
import math
from pathlib import Path
import sys
import unittest
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
from fontTools.misc.fixedTools import otRound
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib.models import supportScalar
from test_release_runtime_controls import variable_fixture
from test_runtime_regressions import fixture_font


def points(font, name):
    glyphs = font.getGlyphSet()
    pen = DecomposingRecordingPen(glyphs)
    glyphs[name].draw(pen)
    return [point for _operation, values in pen.value for point in values]


class NativeGeometryTests(unittest.TestCase):
    def test_metrics_include_unencoded_feature_alternates(self):
        actual, reference = fixture_font(), fixture_font()
        for font in (actual, reference):
            for table in font['cmap'].tables:
                if table.isUnicode():
                    table.cmap = {cp: name for cp, name in table.cmap.items() if name != 'Galt'}
            addOpenTypeFeaturesFromString(font, '''
                languagesystem DFLT dflt;
                feature zero { sub G by Galt; } zero;
            ''')
        reference['hmtx'].metrics['Galt'] = (655, 18)
        self.assertEqual(b.reference_cmap_metric_targets(actual, reference, 'hmtx')['Galt'], (655, 18))

    def test_reference_metrics_place_only_the_reference_glyph(self):
        # Sarasa Ui CL draws 㓱 from Source Han K, the product from Shanggu
        # 1.028; Source Han's side bearing and vertical origin put Shanggu's
        # outline 6 units right and 21 units high. A glyph takes the
        # reference's metrics only when it is the reference's glyph.
        font, reference = fixture_font(), fixture_font()
        pen = TTGlyphPen(None)
        pen.moveTo((20, 0)); pen.lineTo((380, 0)); pen.lineTo((380, 650)); pen.lineTo((20, 650)); pen.closePath()
        reference['glyf']['T'] = pen.glyph()
        for name in ('G', 'T'):
            reference['hmtx'].metrics[name] = (600, 37)
            reference['vmtx'].metrics[name] = (1000, 150)
        kept = b.reference_outline_divergences(font, reference, set())
        self.assertEqual(kept, {ord('T')})
        b.align_reference_hmtx_lsb(font, reference, kept)
        b.align_reference_vmtx(font, reference, kept)
        self.assertEqual((font['hmtx'].metrics['G'], font['vmtx'].metrics['G']), ((600, 37), (1000, 150)))
        self.assertEqual((font['hmtx'].metrics['T'], font['vmtx'].metrics['T']), ((600, 0), (1000, 100)))

    def test_italic_uses_native_center_for_unencoded_glyphs_too(self):
        font = fixture_font()
        alias = b.clone_glyph(font, 'G', 'vertical.unencoded')
        source = points(font, alias)
        b.shear_font(font, 9.4)
        slope = math.tan(math.radians(9.4))
        expected = [(math.floor(x + (y - 1000 / 3) * slope + .5), y) for x, y in source]
        self.assertEqual(points(font, alias), expected)
        self.assertEqual(font['hmtx'].metrics[alias][1], min(x for x, y in expected))

    def test_italic_variations_round_once_where_tuples_overlap(self):
        # Every sheared delta rounded on its own left Source Han's 夜 1.9
        # units off its sheared curve at 200. Here two tuples meet at -0.5:
        # 0.5 of the first plus the second, each 0.497 short after rounding.
        font = variable_fixture()
        source = list(font['glyf']['G'].coordinates)
        lift = [(0, 3)] * len(source) + [(0, 0)] * 4
        supports = [(-1, -1, 0), (-1, -0.5, 0)]
        font['gvar'].variations['G'] = [TupleVariation({'wght': support}, list(lift)) for support in supports]
        b.shear_font(font, 9.4)
        slope, center = math.tan(math.radians(9.4)), 1000 / 3
        sheared, variations = list(font['glyf']['G'].coordinates), font['gvar'].variations['G']
        independent = []
        for location in (-1.0, -0.5):
            scalars = [supportScalar({'wght': location}, {'wght': support}) for support in supports]
            for index, (x, y) in enumerate(source):
                truth = x + (y + 3 * sum(scalars) - center) * slope
                actual = sheared[index][0] + sum(scalar * variation.coordinates[index][0]
                                                 for scalar, variation in zip(scalars, variations))
                self.assertLessEqual(abs(actual - truth), 0.5, (location, index))
                independent.append(abs(otRound(x + (y - center) * slope) + sum(scalars) * otRound(3 * slope) - truth))
        self.assertGreater(max(independent), 0.5)
        self.assertEqual([variation.coordinates[-4:] for variation in variations], [[(0, 0)] * 4] * 2)

    def test_italic_shear_starts_from_the_points_its_deltas_start_from(self):
        # Instancing to a new default leaves the default outline in floats
        # (955.46, 762.48) while its deltas already start from (955, 762):
        # shearing the floats left Source Han's 䊴 a unit off at 200.
        font = variable_fixture()
        glyph = font['glyf']['G']
        rounded = list(glyph.coordinates)
        glyph.coordinates = type(glyph.coordinates)([(x + .46, y + .48) for x, y in rounded])
        font['gvar'].variations['G'] = [TupleVariation({'wght': (-1, -1, 0)}, [(-5, -12)] * len(rounded) + [(0, 0)] * 4)]
        b.shear_font(font, 9.4)
        slope, center = math.tan(math.radians(9.4)), 1000 / 3
        sheared = list(font['glyf']['G'].coordinates)
        delta = font['gvar'].variations['G'][0].coordinates
        for index, (x, y) in enumerate(rounded):
            light = sheared[index][0] + delta[index][0]
            self.assertLessEqual(abs(light - (x - 5 + (y - 12 - center) * slope)), 0.5, index)
            self.assertLessEqual(abs(sheared[index][0] - (x + .46 + (y + .48 - center) * slope)), 0.5, index)
            self.assertEqual(sheared[index][1], y)

    def test_italic_punctuation_ink_is_measured_upright(self):
        # A sheared ？ crosses the middle of the em at Regular but not at
        # Light; measured upright, every weight and the VF classify alike.
        import tempfile
        from east_asian_spacing.font import Font
        font = fixture_font()
        font['post'].italicAngle = -9.4
        b.shear_font(font, 9.4)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'italic.ttf'; font.save(path)
            loaded = Font.load(path)
            sheared = loaded.glyph_bounds(font.getGlyphID('G'))
            with b.upright_ink_measurement():
                upright = loaded.glyph_bounds(font.getGlyphID('G'))
        source = points(fixture_font(), 'G')
        self.assertAlmostEqual(upright[0], min(x for x, _y in source), delta=1)
        self.assertAlmostEqual(upright[2], max(x for x, _y in source), delta=1)
        self.assertNotAlmostEqual(sheared[2], upright[2], delta=1)

    def test_scaled_components_receive_the_affine_transform_once(self):
        font = fixture_font()
        pen = TTGlyphPen(font.getGlyphSet())
        pen.addComponent('G', (.8, 0, 0, 1.2, 10, 20))
        font['glyf'].glyphs['component'] = pen.glyph()
        font['hmtx'].metrics['component'] = (600, 10)
        font['vmtx'].metrics['component'] = (1000, 100)
        font.setGlyphOrder(font.getGlyphOrder() + ['component'])
        source = points(font, 'component')
        b.shear_font(font, 9.4)
        expected = [(x + (y - 1000 / 3) * math.tan(math.radians(9.4)), y) for x, y in source]
        for actual, wanted in zip(points(font, 'component'), expected):
            self.assertLessEqual(abs(actual[0] - wanted[0]), 1)
            self.assertLessEqual(abs(actual[1] - wanted[1]), 1)

    def test_static_dash_uses_the_same_native_center(self):
        source = fixture_font(); target = copy.deepcopy(source)
        original = points(source, 'G')
        b.copy_static_dash_outline(target, 'G', source, 'G', italic=True)
        expected = [(math.floor(x + (y - 1000 / 3) * math.tan(math.radians(9.4)) + .5), y)
                    for x, y in original]
        self.assertEqual(points(target, 'G'), expected)


if __name__ == '__main__':
    unittest.main()
