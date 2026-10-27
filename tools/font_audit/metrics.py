"""Measure runtime metrics, including unencoded glyphs and vertical origins."""
from __future__ import annotations

import io

import freetype
import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont

from .contract import AXIS_POINTS


def cross_engine_metrics(path, *, weights=AXIS_POINTS, sample_limit=24) -> dict:
    data = path.read_bytes()
    runtime = hb.Font(hb.Face(data))
    with TTFont(io.BytesIO(data), lazy=True) as metadata:
        variable = "fvar" in metadata
        glyph_count = metadata["maxp"].numGlyphs
        weights = tuple(weights) if variable else (metadata["OS/2"].usWeightClass,)
    face = freetype.Face(io.BytesIO(data))
    failures, samples, cases = 0, [], 0
    rounding_count, rounding_samples, maximum_unrounded_bearing_difference = 0, [], 0
    face.set_pixel_sizes(0, face.units_per_EM)
    flags = freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_BITMAP
    for weight in weights:
        if variable:
            runtime.set_variations({"wght": weight})
            face.set_var_design_coords([weight])
        for gid in range(glyph_count):
            face.load_glyph(gid, flags)
            metric = face.glyph.metrics
            extent = runtime.get_glyph_extents(gid)
            vertical_origin = runtime.get_glyph_v_origin(gid)
            expected = (runtime.get_glyph_h_advance(gid), -runtime.get_glyph_v_advance(gid),
                        extent.x_bearing if extent else 0,
                        vertical_origin[1] - extent.y_bearing if extent and vertical_origin else 0)
            actual = (metric.horiAdvance, metric.vertAdvance, metric.horiBearingX, metric.vertBearingY)
            # Empty glyphs have no meaningful ink side bearings. Advances are
            # nevertheless required to agree exactly, including GDEF marks.
            compared = 4 if extent and (extent.width or extent.height) else 2
            cases += 1
            if actual[:compared] == expected[:compared]:
                continue
            differences = [abs(a - b) for a, b in zip(actual[:compared], expected[:compared])]
            # NO_SCALE exposes variable outlines as rounded integer font units.
            # Recheck a one-unit bearing difference with FreeType's documented
            # unrounded-unit path. Advances and digit uniformity stay exact.
            quantized = compared == 4 and differences[:2] == [0, 0] and max(differences[2:]) <= 1
            if quantized:
                face.load_glyph(gid, freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_BITMAP)
                precise = (face.glyph.metrics.horiBearingX / 64, face.glyph.metrics.vertBearingY / 64)
                distance = max(abs(a - b) for a, b in zip(precise, expected[2:]))
                maximum_unrounded_bearing_difference = max(maximum_unrounded_bearing_difference, distance)
                # Keep this different FreeType loading path as raw evidence,
                # not as an extra rounding of the design-unit comparison.
                # In particular composite vertical metrics are calculated at
                # different stages in the two FreeType loading paths.
            if quantized:
                rounding_count += 1
                if len(rounding_samples) < sample_limit:
                    rounding_samples.append({"weight": weight, "gid": gid, "freetype_integer": actual[2:],
                                             "freetype_unrounded": precise, "harfbuzz_integer": expected[2:]})
            else:
                failures += 1
                if len(samples) < sample_limit:
                    samples.append({"weight": weight, "gid": gid,
                                    "freetype": actual, "harfbuzz": expected})
    return {"cases": cases, "glyphs": glyph_count, "weights": list(weights),
            "failures": failures, "samples": samples,
            "integer_bearing_observations": {"count": rounding_count, "samples": rounding_samples,
                                             "maximum_unrounded_difference": maximum_unrounded_bearing_difference},
            "policy": {"advance_tolerance": 0, "integer_bearing_tolerance": 1,
                       "freetype_reference": "https://freetype.org/freetype2/docs/reference/ft2-glyph_retrieval.html#ft_glyphslotrec"}}


def vertical_position(value: tuple) -> tuple:
    """(vertical advance, ink top below the vertical origin) of a nominal_metrics row."""
    _h_advance, v_advance, origin, extents, _ink_x, ink_top = value
    top = ink_top if ink_top is not None else (extents[1] if extents else 0)
    return v_advance, origin[1] - top


def compare_nominal(actual: dict[int, tuple], expected: dict[int, tuple], *, sample_limit=24) -> dict[str, dict]:
    """Horizontal (advance, ink x) and vertical (advance, ink below the origin) per codepoint.

    Advances are exact. Positions are the drawn ink, within one unit of
    rounding: a VF and its static may reach the same ink with different
    off-curve extremes (italic Inter, Ƴ's hook), which HarfBuzz extents and
    a TrueType vertical origin (control yMax + TSB) follow.
    """
    rows = {name: {"cases": 0, "failures": 0, "samples": []} for name in ("horizontal_metrics", "vertical_metrics")}
    for cp, reference in expected.items():
        measured = actual.get(cp)
        for section, row in rows.items():
            row["cases"] += 1
            if measured is None:
                left, right, failed = None, reference, True
            elif section == "horizontal_metrics":
                left, right = (measured[0], measured[4]), (reference[0], reference[4])
                failed = (left[0] != right[0] or (left[1] is None) != (right[1] is None)
                          or (left[1] is not None and abs(left[1] - right[1]) > 1))
            else:
                left, right = vertical_position(measured), vertical_position(reference)
                failed = (left[0] != right[0] or (measured[5] is None) != (reference[5] is None)
                          or abs(left[1] - right[1]) > 1)
            if failed:
                row["failures"] += 1
                if len(row["samples"]) < sample_limit:
                    row["samples"].append({"cp": f"U+{cp:04X}", "variable": left, "static": right})
    return rows


def nominal_metrics(path, weight=None) -> dict[int, tuple]:
    """Full cmap metrics as consumed by HarfBuzz; never compare raw vmtx to HB."""
    with TTFont(path, lazy=True) as font:
        codepoints = sorted(font.getBestCmap())
    runtime = hb.Font(hb.Face(path.read_bytes()))
    if weight is not None:
        runtime.set_variations({"wght": weight})
    result = {}
    for cp in codepoints:
        gid = runtime.get_nominal_glyph(cp)
        extent = runtime.get_glyph_extents(gid)
        ink = BoundsPen(None)
        runtime.draw_glyph_with_pen(gid, ink)
        result[cp] = (runtime.get_glyph_h_advance(gid), runtime.get_glyph_v_advance(gid),
                      runtime.get_glyph_v_origin(gid), tuple(extent) if extent else None,
                      ink.bounds[0] if ink.bounds else None, ink.bounds[3] if ink.bounds else None)
    return result
