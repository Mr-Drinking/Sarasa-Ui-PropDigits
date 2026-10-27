"""HarfBuzz measurements with explicit script, language and direction."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
from typing import Mapping

import uharfbuzz as hb
from fontTools.ttLib import TTFont


@dataclass(frozen=True)
class Glyph:
    gid: int
    cluster: int
    advance: tuple[int, int]
    offset: tuple[int, int]
    bounds: tuple[int, int, int, int] | None

    def geometry(self) -> tuple:
        """Glyph IDs belong to a font and must not be compared across fonts."""
        return self.cluster, self.advance, self.offset, self.bounds


class Face:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.face = hb.Face(self.data)
        self.font = hb.Font(self.face)
        self.font.scale = (1000, 1000)
        self.variations: dict[str, float] = {}
        self.outline_cache = {}
        self.outline_key_cache = {}

    def set_weight(self, weight: float | None) -> None:
        self.variations = {} if weight is None else {"wght": weight}
        self.font.set_variations(self.variations)
        self.outline_cache = {}
        self.outline_key_cache = {}

    def outline(self, gid):
        from fontTools.pens.recordingPen import RecordingPen
        cache = getattr(self, 'outline_cache', None)
        if cache is None:
            cache = self.outline_cache = {}
        if gid not in cache:
            pen = RecordingPen(); self.font.draw_glyph_with_pen(gid, pen)
            cache[gid] = tuple((operation, tuple(points)) for operation, points in pen.value)
        return cache[gid]

    def ink(self, gid):
        """HarfBuzz-ordered (x_bearing, y_bearing, width, height) of the drawn ink, or None."""
        from fontTools.pens.boundsPen import BoundsPen
        outline = self.outline(gid)
        cache = getattr(self, 'ink_cache', None)
        if cache is None:
            cache = self.ink_cache = {}
        cached = cache.get(gid)
        # Keyed by the cached outline itself: a new weight draws a new outline.
        if cached is None or cached[0] is not outline:
            pen = BoundsPen(None)
            for operation, args in outline:
                getattr(pen, operation)(*args)
            box = pen.bounds
            cached = cache[gid] = (outline, None if box is None else (box[0], box[3], box[2] - box[0], box[1] - box[3]))
        return cached[1]

    def outline_key(self, gid):
        if not hasattr(self, 'outline_key_cache'):
            self.outline_key_cache = {}
        if gid in self.outline_key_cache:
            return self.outline_key_cache[gid]
        value = self.outline(gid)
        points = [point for _operation, args in value for point in args if point is not None]
        origin = (min(x for x, y in points), min(y for x, y in points)) if points else (0, 0)
        normalized = tuple((operation, tuple(None if point is None else
                           (round(point[0] - origin[0], 4), round(point[1] - origin[1], 4)) for point in args))
                           for operation, args in value)
        self.outline_key_cache[gid] = hashlib.sha256(repr(normalized).encode()).hexdigest()
        return self.outline_key_cache[gid]

    def shape(self, text: str, *, script: str | None = None,
              language: str | None = None, direction: str = "ltr",
              features: Mapping[str, int | bool] | None = None,
              variations: Mapping[str, float] | None = None) -> tuple[Glyph, ...]:
        if variations is not None:
            self.font.set_variations(dict(variations))
            self.outline_cache = {}; self.outline_key_cache = {}
        buffer = hb.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        if script is not None:
            buffer.script = script
        # None means OpenType's default language, never the process locale.
        buffer.language = language or "und"
        buffer.direction = direction
        hb.shape(self.font, buffer, dict(features or {}))
        result = []
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
            extents = self.font.get_glyph_extents(info.codepoint)
            result.append(Glyph(
                info.codepoint, info.cluster,
                (position.x_advance, position.y_advance),
                (position.x_offset, position.y_offset),
                tuple(extents) if extents is not None else None,
            ))
        return tuple(result)


def canonical_outline(font: TTFont, name: str, location: dict | None = None) -> tuple:
    """Decompose components; glyph names and contour start points are not evidence."""
    from fontTools.pens.recordingPen import DecomposingRecordingPen

    glyphs = font.getGlyphSet(location=location)
    pen = DecomposingRecordingPen(glyphs)
    glyphs[name].draw(pen)
    scale = 1000 / font["head"].unitsPerEm
    contours, contour = [], []
    for operation, points in pen.value:
        if operation in {"closePath", "endPath"}:
            if contour:
                contours.append(tuple(sorted(contour)))
                contour = []
        else:
            contour.extend((round(point[0] * scale, 3), round(point[1] * scale, 3))
                           for point in points if point is not None)
    if contour:
        contours.append(tuple(sorted(contour)))
    return tuple(sorted(contours))


def reverse_texts(font: TTFont) -> dict[str, str]:
    """Reach unencoded substitutions, including Old Hangul and kana ligatures."""
    result: dict[str, str] = {}
    for cp, glyph in sorted(font.getBestCmap().items()):
        result.setdefault(glyph, chr(cp))
    if "GSUB" not in font:
        return result
    lookups = font["GSUB"].table.LookupList.Lookup
    for _ in range(len(lookups) + 1):
        before = len(result)
        for lookup in lookups:
            for outer in lookup.SubTable:
                sub = getattr(outer, "ExtSubTable", outer)
                for source, target in getattr(sub, "mapping", {}).items():
                    if source in result:
                        for name in target if isinstance(target, (list, tuple)) else [target]:
                            result.setdefault(name, result[source])
                for source, targets in getattr(sub, "alternates", {}).items():
                    if source in result:
                        for name in targets:
                            result.setdefault(name, result[source])
                for source, ligatures in getattr(sub, "ligatures", {}).items():
                    for ligature in ligatures:
                        sequence = [source, *ligature.Component]
                        if all(name in result for name in sequence):
                            result.setdefault(ligature.LigGlyph, "".join(result[name] for name in sequence))
                coverage = getattr(sub, "Coverage", None)
                for source, target in zip(getattr(coverage, "glyphs", []), getattr(sub, "Substitute", [])):
                    if source in result:
                        result.setdefault(target, result[source])
        if len(result) == before:
            break
    return result


def geometry_delta(actual: tuple[Glyph, ...], expected: tuple[Glyph, ...]) -> dict:
    """Keep positions and outlines separate: a matching box cannot prove routing."""
    if len(actual) != len(expected):
        return {"glyph_count": [len(actual), len(expected)]}
    result = {}
    for field in ("cluster", "advance", "offset", "bounds"):
        left = [getattr(glyph, field) for glyph in actual]
        right = [getattr(glyph, field) for glyph in expected]
        if left != right:
            result[field] = [left, right]
    return result
