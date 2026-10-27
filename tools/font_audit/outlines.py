"""Point-level source comparisons, without product build transformations."""
from __future__ import annotations

import math
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.ttLib import TTFont

from .results import Result


def segment_recording(value: tuple) -> tuple:
    """Normalize implied quadratic points and explicit closing segments.

    HarfBuzz emits one quadratic segment per command; FontTools can emit a
    whole chain with implied on-curve points. Compare the same geometry rather
    than treating the serialization choice as an outline mismatch.
    """
    class Segments(BasePen):
        def __init__(self):
            super().__init__(None)
            self.value = []
            self.start = None

        def _moveTo(self, point):
            self.start = point
            self.value.append(('moveTo', (point,)))

        def _lineTo(self, point):
            self.value.append(('lineTo', (point,)))

        def _qCurveToOne(self, control, end):
            self.value.append(('qCurveTo', (control, end)))

        def _curveToOne(self, a, b, end):
            self.value.append(('curveTo', (a, b, end)))

        def _closePath(self):
            if self.value[-1] == ('lineTo', (self.start,)):
                self.value.pop()
            self.value.append(('closePath', ()))

        def _endPath(self):
            self.value.append(('endPath', ()))

    pen = Segments()
    for operation, points in value:
        getattr(pen, operation)(*points)
    return tuple(pen.value)


def quadratic_source_outline(face, gid, *, italic=False, inter=False) -> tuple:
    """A static source outline as a static build receives it.

    CFF sources pass through AFDKO's published default cu2qu parameters; italic
    sources are sheared 9.4 degrees about em/3 with integer rounding. The
    oracle reads the pinned CFF directly, never a generated build cache.
    """
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    metadata = getattr(face, "metadata", None)
    if not inter and metadata is not None and "CFF " in metadata:
        glyphs = metadata.getGlyphSet()
        pen = TTGlyphPen(glyphs)
        glyphs[metadata.getGlyphName(gid)].draw(Cu2QuPen(pen, 1.0, reverse_direction=True))
        glyph = pen.glyph(); value = RecordingPen(); glyph.draw(value, None)
        value = tuple(value.value)
    elif not inter and metadata is not None and "glyf" in metadata and "fvar" not in metadata:
        # The build shears the stored TrueType points; implied on-curve points
        # follow from them. HarfBuzz draws those midpoints first, some at half
        # units, and rounding them after the shear put a correct CL Heavy
        # italic (Shanggu Heavy) 0.5 units off.
        glyphs = metadata.getGlyphSet(); pen = DecomposingRecordingPen(glyphs)
        glyphs[metadata.getGlyphName(gid)].draw(pen)
        value = tuple(pen.value)
    else:
        value = face.outline(gid)
    if italic and not inter:
        slope = math.tan(math.radians(9.4))
        value = tuple((operation, tuple(None if point is None else
                      (math.floor(point[0] + (point[1] - 1000 / 3) * slope + .5), point[1])
                      for point in points)) for operation, points in value)
    return segment_recording(value)


def recording(glyphs, name: str, *, scale: float = 1, shear: float = 0) -> tuple:
    pen = DecomposingRecordingPen(glyphs)
    glyphs[name].draw(pen)
    return tuple((operation, tuple(None if point is None else
                                  (point[0] * scale + (point[1] * scale - 1000 / 3) * shear, point[1] * scale)
                                  for point in points)) for operation, points in pen.value)


def residual(actual: tuple, expected: tuple, *, allow_translation: bool = False) -> float:
    if len(actual) != len(expected):
        return math.inf
    differences = []
    for (operation, points), (other_operation, other_points) in zip(actual, expected):
        if operation != other_operation or len(points) != len(other_points):
            return math.inf
        for left, right in zip(points, other_points):
            if left is None or right is None:
                if left != right:
                    return math.inf
            else:
                differences.append((left[0] - right[0], left[1] - right[1]))
    if not differences:
        return 0
    if allow_translation:
        # A translation can reflect the explicit Sarasa side-bearing contract;
        # it cannot make a different compatible ideograph pass this comparison.
        # The best uniform translation is the midpoint of the displacement
        # interval. Its largest remaining error is half that interval, not
        # the distance between its two extremes.
        return max((max(value[axis] for value in differences) - min(value[axis] for value in differences)) / 2
                   for axis in (0, 1))
    return max(abs(value) for difference in differences for value in difference)


def compare_outlines(actual_path: Path, expected_path: Path, *, codepoints=None,
                     exclude=frozenset(), actual_weight=None, source_weight=None,
                     source_shear=False, allow_translation=False, tolerance=0) -> dict:
    result, maximum, cache = Result(), 0, {}
    with TTFont(actual_path) as actual, TTFont(expected_path) as expected:
        left, right = actual.getBestCmap(), expected.getBestCmap()
        repertoire = (set(left) & set(right) if codepoints is None else set(codepoints)) - set(exclude)
        glyphs = actual.getGlyphSet(location={"wght": actual_weight} if actual_weight is not None else None)
        source = expected.getGlyphSet(location={"wght": source_weight} if source_weight is not None else None)
        for cp in sorted(repertoire):
            pair = (left.get(cp), right.get(cp))
            if None in pair:
                result.check(False, {"cp": f"U+{cp:04X}", "check": "missing_source_or_target"})
                continue
            if pair not in cache:
                a = recording(glyphs, pair[0], scale=1000 / actual["head"].unitsPerEm)
                b = recording(source, pair[1], scale=1000 / expected["head"].unitsPerEm,
                              shear=math.tan(math.radians(9.4)) if source_shear else 0)
                cache[pair] = residual(a, b, allow_translation=allow_translation)
            error = cache[pair]
            maximum = max(maximum, error)
            result.check(error <= tolerance, {"cp": f"U+{cp:04X}", "residual": error if math.isfinite(error) else "topology"})
    return result.report(glyph_pairs=len(cache), tolerance=tolerance,
                         maximum_residual=maximum if math.isfinite(maximum) else "topology")
