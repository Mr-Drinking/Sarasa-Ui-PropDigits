"""Independent interpolation and italic geometry measurements."""
from __future__ import annotations

import math

from fontTools.ttLib import TTFont

from . import contract as c
from .outlines import recording, residual, segment_recording
from .results import Result
from .shaping import Face


def composite_interpolation(path, *, weights=c.AXIS_POINTS):
    """Compare two independent gvar/IUP evaluators for every composite glyph."""
    result = Result()
    maximum = 0
    runtime = Face(path)
    with TTFont(path) as font:
        names = [name for name in font.getGlyphOrder() if font['glyf'][name].isComposite()]
        for weight in weights:
            runtime.set_weight(weight)
            # Both evaluators must receive the same F2Dot14 coordinate.
            # Comparing HarfBuzz's quantized coordinate with an unquantized
            # Python float conflates axis conversion with IUP interpolation.
            location = dict(zip((axis.axisTag for axis in font['fvar'].axes),
                                runtime.font.get_var_coords_normalized()))
            glyphs = font.getGlyphSet(location=location, normalized=True)
            for name in names:
                actual = segment_recording(runtime.outline(font.getGlyphID(name)))
                expected = segment_recording(recording(glyphs, name))
                error = residual(actual, expected)
                maximum = max(maximum, error)
                result.check(error <= 1 / 512, {'weight': weight, 'gid': font.getGlyphID(name),
                             'residual': error if math.isfinite(error) else 'topology'})
    return result.report(weights=list(weights), composite_glyphs=len(names),
                         maximum_residual=maximum if math.isfinite(maximum) else 'topology',
                         coordinate_tolerance=1 / 512)
