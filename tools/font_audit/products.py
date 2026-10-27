"""Product invariants whose expected result is not plain Sarasa equality."""
from __future__ import annotations

import io
from pathlib import Path

import freetype
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import RecordingPen
from typing import Any
import uharfbuzz as hb

from . import contract as c
from .results import Result
from .shaping import Face


COLON_LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZķļņŗșțĶĻŅŖȘȚäöüáéğåœæßĢ"
COLON_TEXTS = tuple(dict.fromkeys((
    "09:41", "12:34:56", "a::b::::::2", "1::2::::::a",
    *(left + ":" * count + right
      for left in ("", "0", "1", "a", "A", " ")
      for right in ("", "2", "a", "A", " ") for count in range(1, 17)),
    *(text for char in COLON_LETTERS
      for text in (char + ":2", "1:" + char, char + ":", ":" + char, char + "::::::2")),
)))
WIDTH_FEATURES = ({}, {"pnum": True}, {"tnum": True}, {"zero": True}, {"tnum": True, "zero": True})
KERN_TEXTS = ("7.", "09:41", "12.5", "T.", "LY", "GT", "Go", "AV", "To", "WA", "Ty", "F,")
MARK_TEXTS = ("i\u0307", "j\u0301", "į\u0301", "A\u0304\u0301", "a\u0327\u0301")


def properties(path: Path) -> tuple[bool, tuple[int, ...]]:
    with TTFont(path, lazy=True) as font:
        return bool(font["post"].italicAngle), c.AXIS_POINTS if "fvar" in font else (font["OS/2"].usWeightClass,)


def static_inter_style(weight: int, italic: bool) -> str:
    """The pinned Inter static a static product of this weight is built from (Heavy: Black)."""
    name = next(name for name, value in c.WEIGHTS.items() if value == weight)
    return c.style_name("Black" if name == "Heavy" else name, italic)


def precise_positions(face: Face, text: str, variations: dict, *, script: str, language: str,
                      features: dict) -> list[tuple[int, float, float, float]]:
    """(gid, x advance, x offset, y offset) in 1/64 unit of a 1000-unit em.

    At 1000 HarfBuzz rounds each anchor and each variation delta of a
    2048-UPM face to whole units: Inter's own j́ at 325 lands 1.1 units off
    its curve, which is no measure of a product that follows the curve.
    """
    font = hb.Font(face.face)
    font.scale = (64000, 64000)
    font.set_variations(variations)
    buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties()
    buffer.script = script; buffer.language = language; buffer.direction = "ltr"
    hb.shape(font, buffer, features)
    return [(info.codepoint, position.x_advance / 64, position.x_offset / 64, position.y_offset / 64)
            for info, position in zip(buffer.glyph_infos, buffer.glyph_positions)]


def source_inter(italic: bool, static_style: str | None = None, *, round_coordinates=False) -> Face:
    # HarfBuzz does not decode WOFF2, so decode the original payload without
    # baking any product substitutions or using any builder helper.
    path = c.SARASA_SOURCE / f'sources/Inter/Inter-{static_style}.ttf' if static_style else c.inter_path(italic)
    with TTFont(path) as font:
        font.flavor = None
        if round_coordinates:
            from fontTools.ttLib.scaleUpem import scale_upem
            scale_upem(font, 1000)
        stream = io.BytesIO(); font.save(stream)
    face = Face.__new__(Face)
    face.data = stream.getvalue(); face.face = hb.Face(face.data)
    face.font = hb.Font(face.face); face.font.scale = (1000, 1000); face.variations = {}
    return face


class InterUiOracle(Face):
    """Execute the documented preset order in HarfBuzz, not in builder code.

    A sub-font changes only nominal character-to-glyph lookup. Every outline,
    variation, positioning table and GSUB operation still comes from the pinned
    original Inter face. This also avoids copying the builder's layout rewrites
    into the expected result.
    """
    def __init__(self, italic: bool, static_style: str | None = None, *, round_coordinates=False):
        source = source_inter(italic, static_style, round_coordinates=round_coordinates)
        self.data, self.face, self.parent = source.data, source.face, source.font
        path = c.SARASA_SOURCE / f'sources/Inter/Inter-{static_style}.ttf' if static_style else c.inter_path(italic)
        with TTFont(path, lazy=True) as metadata:
            codepoints = sorted(metadata.getBestCmap())
        self.mapping = {cp: self.parent.get_nominal_glyph(cp) for cp in codepoints}
        self.font = hb.Font(self.parent)
        funcs = hb.FontFuncs()
        funcs.set_nominal_glyph_func(lambda _font, cp, _data: self.mapping.get(cp, 0))
        # Sarasa's native pass1 establishes the same 1em vertical cell for
        # every Inter glyph. This is a declared metric projection, independent
        # of any candidate font's vmtx, VORG or glyph bounds.
        funcs.set_glyph_v_advance_func(lambda _font, _gid, _data: -1000)
        funcs.set_glyph_v_origin_func(lambda _font, gid, _data:
                                     (True, self.parent.get_glyph_h_advance(gid) // 2, 880))
        self.font.funcs = funcs
        self.variations = {}
        self.set_weight(400)
        disabled = {tag: False for tag in ("ccmp", "locl", "liga", "clig", "rlig", "calt", "rclt", "frac",
                                           "numr", "dnom", "zero", "ss03", "cv10", "pnum", "tnum")}
        for tag in ("tnum", "ss03", "cv10", "pnum"):
            changed = {}
            for cp in codepoints:
                if (tag == "tnum" and cp == 0x2D) or (tag == "pnum" and cp not in c.INTENTIONAL_CPS):
                    continue
                shaped = self.shape(chr(cp), script="Latn", features={**disabled, tag: True})
                if len(shaped) == 1 and shaped[0].gid:
                    changed[cp] = shaped[0].gid
            self.mapping.update(changed)

    def set_weight(self, weight: float | None) -> None:
        self.variations = {"wght": weight if weight is not None else 400, "opsz": 14}
        self.outline_cache = {}; self.outline_key_cache = {}
        self.parent.set_variations(self.variations)
        self.font.set_variations(self.variations)


def inter_axis_metrics(path: Path, *, weights=range(200, 901, 5)) -> dict:
    """The 350 neighborhood is checked against Inter, never a self-instance."""
    italic, _ = properties(path)
    source, actual = InterUiOracle(italic), Face(path)
    with TTFont(path, lazy=True) as font:
        # Basic Latin, Latin extensions, Greek and Cyrillic are Inter-owned.
        # CJK override characters and line-separator controls are excluded by
        # this explicit repertoire, rather than by observing target mismatches.
        codepoints = sorted(cp for cp in font.getBestCmap() if cp in source.mapping
                            and (0x20 <= cp <= 0x2FF and cp != 0xB7 or 0x370 <= cp <= 0x52F))
    result = Result()
    maximum_error = 0
    errors_by_weight = {}
    for weight in weights:
        source.set_weight(weight); actual.set_weight(weight)
        for cp in codepoints:
            left = actual.font.get_glyph_h_advance(actual.font.get_nominal_glyph(cp))
            right = source.font.get_glyph_h_advance(source.mapping[cp])
            error = abs(left - right)
            maximum_error = max(maximum_error, error)
            errors_by_weight[weight] = max(errors_by_weight.get(weight, 0), error)
            result.check(error <= 1, {"weight": weight, "cp": f"U+{cp:04X}", "actual": left, "Inter": right})
    return result.report(weights=list(weights), codepoints=len(codepoints), source="Inter v4.1",
                         maximum_error=maximum_error, maximum_error_by_weight=errors_by_weight)


def width_invariants(path: Path) -> dict:
    italic, weights = properties(path)
    actual, source = Face(path), source_inter(italic)
    result = Result()
    ft = freetype.Face(io.BytesIO(path.read_bytes()))
    for weight in weights:
        actual.set_weight(weight)
        source.font.set_variations({"wght": weight, "opsz": 14})
        if len(weights) > 1:
            ft.set_var_design_coords([weight])
        for cp in sorted(c.INTENTIONAL_CPS):
            for tabular in (False, True):
                features = {"kern": False, "calt": False, "tnum": tabular, "pnum": not tabular}
                target = actual.shape(chr(cp), script="Latn", features=features)
                expected = source.shape(chr(cp), script="Latn", features=features)
                result.check(len(target) == len(expected) == 1 and target[0].gid != 0
                             and abs(target[0].advance[0] - expected[0].advance[0]) <= 1,
                             {"weight": weight, "cp": f"U+{cp:04X}", "tabular": tabular,
                              "actual": [g.advance for g in target], "source": [g.advance for g in expected]})
            plain = actual.shape(chr(cp), script="Latn", features={"kern": False, "calt": False})
            proportional = actual.shape(chr(cp), script="Latn", features={"kern": False, "calt": False, "pnum": True})
            result.check(plain == proportional, {"weight": weight, "cp": f"U+{cp:04X}", "check": "default_equals_pnum"})
        for features in ({"tnum": True}, {"tnum": True, "zero": True}, {"tnum": True, "zero": True, "ss01": True}):
            run = actual.shape("0123456789", script="Latn", features={"kern": False, **features})
            advances = [glyph.advance[0] for glyph in run]
            ft_advances = []
            for glyph in run:
                ft.load_glyph(glyph.gid, freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_HINTING)
                ft_advances.append(ft.glyph.metrics.horiAdvance)
            result.check(len(run) == 10 and len(set(advances)) == 1 and advances == ft_advances,
                         {"weight": weight, "features": features, "harfbuzz": advances, "freetype": ft_advances})
    return result.report(weights=list(weights), codepoints=len(c.INTENTIONAL_CPS))


def colon_invariants(path: Path) -> dict:
    italic, weights = properties(path)
    with TTFont(path, lazy=True) as font:
        variable = "fvar" in font
    # A VF follows Inter's curve; a static, the same-weight Inter static it is
    # built from. SemiBold's raised colon tops out at 628.01 on the curve but at
    # 627.93 in Inter-SemiBold, whose dot and component offset each round to
    # the product's 627.
    actual = Face(path)
    source = source_inter(italic, None if variable else static_inter_style(weights[0], italic))
    result = Result()
    for weight in weights:
        actual.set_weight(weight)
        source.font.set_variations({"wght": weight, "opsz": 14})
        source.outline_cache = {}; source.outline_key_cache = {}
        for width in WIDTH_FEATURES:
            for enabled in (False, True):
                features = {"kern": False, "calt": enabled, **width}
                for text in COLON_TEXTS:
                    left = actual.shape(text, script="Latn", language="en", features=features)
                    right = source.shape(text, script="Latn", language="en", features=features)
                    def signature(run, face):
                        # Drawn ink: HarfBuzz floors and ceils 2048-UPM Inter's
                        # extents at 1000 but rounds the product's (the italic
                        # tabular colon at 300: 615 against 613, its ink 0.6 apart).
                        return [(g.advance[0], g.offset[1] + box[1]) for g in run
                                if g.cluster < len(text) and text[g.cluster] == ":" and (box := face.ink(g.gid))]
                    a, b = signature(left, actual), signature(right, source)
                    passed = len(a) == len(b) == text.count(":") and all(abs(x - y) <= 1 for av, bv in zip(a, b) for x, y in zip(av, bv))
                    result.check(passed, {"weight": weight, "text": text, "features": features, "actual": a, "source": b})
    return result.report(weights=list(weights), texts=len(COLON_TEXTS), feature_combinations=2 * len(WIDTH_FEATURES))


def positioning_invariants(path: Path, *, weights=None) -> dict:
    italic, font_weights = properties(path)
    weights = font_weights if weights is None else tuple(weights)
    actual, source = Face(path), source_inter(italic)
    result = Result()
    for weight in weights:
        actual.set_weight(weight)
        source.font.set_variations({"wght": weight, "opsz": 14})
        source.outline_cache = {}; source.outline_key_cache = {}
        for script in ("DFLT", "Latn", "Cyrl", "Grek", "Hani"):
            for text in KERN_TEXTS:
                deltas = []
                for face, presets in ((actual, {}), (source, {"cv10": True})):
                    off = face.shape(text, script=script, features={"kern": False, "calt": False, **presets})
                    on = face.shape(text, script=script, features={"kern": True, "calt": False, **presets})
                    deltas.append([(a.advance[0] - b.advance[0], a.offset[0] - b.offset[0]) for a, b in zip(on, off)])
                a, b = deltas
                result.check(len(a) == len(b) == len(text) and all(abs(x - y) <= 1 for av, bv in zip(a, b) for x, y in zip(av, bv)),
                             {"weight": weight, "script": script, "text": text, "actual": a, "source": b})
        for text in MARK_TEXTS:
            values = []
            for face, variations in ((actual, {"wght": weight}), (source, {"wght": weight, "opsz": 14})):
                run = precise_positions(face, text, variations, script="Latn", language="lt",
                                        features={"kern": False, "cv10": True})
                pen, ink = 0.0, []
                for gid, advance, x_offset, y_offset in run:
                    # Drawn ink: HarfBuzz floors and ceils the extents of
                    # 2048-UPM Inter measured at 1000 (i̇ at 325: 3 units).
                    box = face.ink(gid)
                    if box:
                        ink.append((pen + x_offset + box[0], y_offset + box[1]))
                    pen += advance
                values.append([(x - ink[0][0], y - ink[0][1]) for x, y in ink] if ink else [])
            a, b = values
            result.check(bool(a) and len(a) == len(b) and all(abs(x - y) <= 2 for av, bv in zip(a, b) for x, y in zip(av, bv)),
                         {"weight": weight, "text": text, "check": "attached_mark_ink", "actual": a, "source": b})
    return result.report(weights=list(weights), scripts=5, kerning_texts=len(KERN_TEXTS), mark_texts=len(MARK_TEXTS))


SPACING_OPENING = "「『（【〔〈《｛［〘〖〝"
SPACING_CLOSING = "」』）】〕〉》｝］〙〗〟"
SPACING_MIDDLE = "、，。．：；！？・"
SPACING_TEXTS = tuple(left + right for left in SPACING_OPENING + SPACING_CLOSING + SPACING_MIDDLE
                      for right in SPACING_OPENING + SPACING_CLOSING + SPACING_MIDDLE)
SPACING_LANGUAGES = ("und", "zh-Hans", "zh-Hant", "zh-HK", "ja", "ko")
SPACING_FEATURES = (("chws", "ltr"), ("vchw", "ttb"))
SPACING_CASES = len(SPACING_TEXTS) * len(SPACING_LANGUAGES) * len(SPACING_FEATURES) + len(SPACING_FEATURES)


def spacing_parity(variable_path: Path, static_path: Path, weight: int) -> dict:
    """chws/vchw are product additions with no upstream oracle.

    The VF must apply exactly the static weight's contextual adjustments, and
    each feature must actually tighten adjacent fullwidth punctuation.
    """
    variable, static = Face(variable_path), Face(static_path)
    variable.set_weight(weight)
    result = Result()
    for feature, direction in SPACING_FEATURES:
        tightened = 0
        for language in SPACING_LANGUAGES:
            for text in SPACING_TEXTS:
                effects = []
                for face in (variable, static):
                    arguments = {"script": "Hani", "language": language, "direction": direction}
                    off = face.shape(text, **arguments, features={feature: False})
                    on = face.shape(text, **arguments, features={feature: True})
                    effects.append([tuple(x - y for x, y in zip((*a.advance, *a.offset), (*b.advance, *b.offset)))
                                    for a, b in zip(on, off)] if [g.gid for g in on] == [g.gid for g in off] else None)
                a, b = effects
                passed = (a is not None and b is not None and len(a) == len(b)
                          and all(abs(x - y) <= 1 for left, right in zip(a, b) for x, y in zip(left, right)))
                tightened += bool(b) and any(any(value) for value in b)
                result.check(passed, {"feature": feature, "language": language, "text": text,
                                      "variable": a, "static": b})
        result.check(tightened > 0, {"feature": feature, "check": "feature_has_effect"})
    return result.report(weight=weight, texts=len(SPACING_TEXTS), languages=list(SPACING_LANGUAGES))


VERTICAL_DASH_RUN_TEXTS = tuple("—" * count for count in (*range(2, 17), 32, 64, 257)) + (
    "⸺", "⸻", "⸺⸺", "⸺—", "—⸻", "⸻⸺—", "⸺⸻⸺",
)

def vertical_segment_runtime_status(path: Path, variations: dict[str, float] | None = None) -> dict[str, Any]:
    data = path.read_bytes()
    runtime = hb.Font(hb.Face(data))
    if variations: runtime.set_variations(variations)
    em = runtime.face.upem
    failures = []; cases = 0
    with TTFont(io.BytesIO(data)) as font:
        point_cache = {}
        for script in ("Latn", "Cyrl", "Grek", "Hani", "Kana", "Hang", "Bopo"):
            for language in (None, "en", "de", "zh-Hans", "zh-Hant", "zh-HK", "ja", "ko"):
                for features in ({}, {"vert": True, "vrt2": False}, {"vert": False, "vrt2": True}):
                    for text in VERTICAL_DASH_RUN_TEXTS:
                        count = sum({"—": 1, "⸺": 2, "⸻": 3}[character] for character in text)
                        buf = hb.Buffer(); buf.add_str(text); buf.script = script; buf.direction = "ttb"
                        buf.language = language or "und"
                        hb.shape(runtime, buf, features)
                        reasons = []
                        if len(buf.glyph_infos) != count:
                            reasons.append("wrong segment count")
                        previous_bottom = None
                        for index, (info, pos) in enumerate(zip(buf.glyph_infos, buf.glyph_positions)):
                            if (pos.x_advance, pos.y_advance) != (0, -em):
                                reasons.append("segment advance is not 1em")
                            extent = runtime.get_glyph_extents(info.codepoint)
                            if not extent or extent.y_bearing + pos.y_offset > 0 or extent.y_bearing + extent.height + pos.y_offset < -em:
                                reasons.append("ink exceeds its own vertical cell")
                            name = font.getGlyphName(info.codepoint)
                            if name not in point_cache:
                                pen = RecordingPen(); runtime.draw_glyph_with_pen(info.codepoint, pen)
                                points = [point for command, args in pen.value if command in {"moveTo", "lineTo"} for point in args]
                                if len(points) > 1 and points[0] == points[-1]:
                                    points.pop()
                                point_cache[name] = points
                            points = [(x + pos.x_offset, y + pos.y_offset - index * em) for x, y in point_cache[name]]
                            if len(points) != 4:
                                reasons.append("segment is not the required quadrilateral")
                                continue
                            high = max(y for x, y in points); low = min(y for x, y in points)
                            top = sorted((x, y) for x, y in points if abs(y - high) < 1e-6)
                            bottom = sorted((x, y) for x, y in points if abs(y - low) < 1e-6)
                            if previous_bottom is not None:
                                if not font["post"].italicAngle or text in {"——", "———", "⸺", "⸻"}:
                                    if top != previous_bottom:
                                        reasons.append("adjacent ink edges do not meet exactly")
                                elif (len(top) != 2 or len(previous_bottom) != 2
                                      or top[0] != previous_bottom[0]
                                      or min(top[1][0], previous_bottom[1][0]) <= top[0][0]
                                      or abs(top[1][0] - previous_bottom[1][0]) > 1):
                                    # Repeated italic middles retain the source's
                                    # integer-rounded width (at most 1 unit of
                                    # taper). Their left edges coincide exactly;
                                    # the joining cross-sections must overlap.
                                    reasons.append("continuous run has an ink gap or unexpected edge-width change")
                            previous_bottom = bottom
                        cases += 1
                        if reasons:
                            failures.append({"script": script, "language": language, "features": features,
                                             "text": text, "reasons": reasons})
    return {"ok": not failures, "cases": cases, "failures": len(failures), "samples": failures[:24]}
