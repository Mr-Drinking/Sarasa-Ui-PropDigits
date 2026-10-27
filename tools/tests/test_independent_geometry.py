from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from font_audit.comparison import Sources
from font_audit.differential import Context
from font_audit.outlines import quadratic_source_outline, residual, segment_recording
from font_audit.shaping import Face, Glyph


def ideograph_font(path, points):
    """One quadratic contour under U+4E00, its side bearing at xMin."""
    pen = TTGlyphPen(None)
    pen.moveTo(points[0]); pen.qCurveTo(*points[1:]); pen.closePath()
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(['.notdef', 'uni4E00'])
    builder.setupCharacterMap({0x4E00: 'uni4E00'})
    builder.setupGlyf({'.notdef': TTGlyphPen(None).glyph(), 'uni4E00': pen.glyph()})
    builder.setupHorizontalMetrics({'.notdef': (1000, 0), 'uni4E00': (1000, min(x for x, _y in points))})
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    builder.setupNameTable({'familyName': 'Probe', 'styleName': 'Regular'})
    builder.setupOS2(); builder.setupPost(); builder.setupMaxp()
    builder.save(path)
    return builder.font


class IndependentGeometryTests(unittest.TestCase):
    def test_quadratic_chains_and_explicit_closing_lines_are_equivalent(self):
        chain = (('moveTo', ((0, 0),)), ('qCurveTo', ((10, 20), (30, 20), (40, 0))), ('closePath', ()))
        segments = (('moveTo', ((0, 0),)), ('qCurveTo', ((10, 20), (20, 20))),
                    ('qCurveTo', ((30, 20), (40, 0))), ('lineTo', ((0, 0),)), ('closePath', ()))
        self.assertEqual(segment_recording(chain), segment_recording(segments))

    def test_translation_residual_means_error_after_the_best_uniform_shift(self):
        original = (('moveTo', ((0, 0),)), ('lineTo', ((100, 100),)))
        shifted = (('moveTo', ((10, 20),)), ('lineTo', ((114, 120),)))
        self.assertEqual(residual(shifted, original, allow_translation=True), 2)
        self.assertEqual(residual(shifted, original), 20)

    def test_truetype_source_shears_stored_points_before_implied_ones(self):
        # The build shears the stored off-curve points (0, 101) and (1, 102)
        # to -38 and -37, so the implied point between them lies at -37.5.
        # Shearing HarfBuzz's drawn midpoint and rounding it gave -38: a
        # correct CL Heavy italic stood 0.5 units off Shanggu Heavy.
        with tempfile.TemporaryDirectory() as directory:
            source = ideograph_font(Path(directory) / 'source.ttf', [(0, 0), (0, 101), (1, 102), (300, 0)])
            ideograph_font(Path(directory) / 'italic.ttf', [(-55, 0), (-38, 101), (-37, 102), (245, 0)])
            product = segment_recording(Face(Path(directory) / 'italic.ttf').outline(1))
            stored = quadratic_source_outline(SimpleNamespace(metadata=source), 1, italic=True)
            drawn = quadratic_source_outline(SimpleNamespace(metadata=None, outline=Face(Path(directory) / 'source.ttf').outline),
                                             1, italic=True)
        self.assertEqual(residual(product, stored), 0)
        self.assertEqual(residual(product, drawn), 0.5)

    def test_classical_override_keeps_shanggu_own_placement(self):
        # Regular 㓱 took Source Han's side bearing (6 units right of
        # Shanggu) and vertical origin (21 units high). Only Shanggu's own run,
        # same outline at the same coordinates, explains a CL difference.
        with tempfile.TemporaryDirectory() as directory:
            shanggu = ideograph_font(Path(directory) / 'shanggu.ttf', [(22, -81), (22, 821), (885, 821), (885, -81)])
            ideograph_font(Path(directory) / 'shifted.ttf', [(28, -81), (28, 821), (891, 821), (891, -81)])
            own, shifted = Face(Path(directory) / 'shanggu.ttf'), Face(Path(directory) / 'shifted.ttf')
            horizontal = (Glyph(1, 0, (1000, 0), (0, 0), None),)
            vertical = (Glyph(1, 0, (0, -1000), (-500, -880), None),)
            for expected, actual, face, explained in ((horizontal, horizontal, own, True),
                                                       (horizontal, horizontal, shifted, False),
                                                       (vertical, vertical, own, True),
                                                       (vertical, (Glyph(1, 0, (0, -1000), (-500, -859), None),), own, False)):
                sources = SimpleNamespace(shape=lambda _text, _context, face=None, run=expected: run,
                                          classical=SimpleNamespace(metadata=shanggu), italic=False, outline_compatibility={})
                self.assertIs(Sources.classical_static_override(sources, '一', Context(), actual, face), explained)


if __name__ == '__main__':
    unittest.main()
