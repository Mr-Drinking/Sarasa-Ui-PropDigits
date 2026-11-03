"""Explain a differential result only with independently measured sources."""
from __future__ import annotations

from dataclasses import replace
from math import tan, radians

import uharfbuzz as hb
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont

from . import contract as c
from .oracles import CjkUiOracle, inter_owned, in_ranges, IDEOGRAPH_RANGES, HANGUL_RANGES
from .products import InterUiOracle
from .shaping import Glyph, Face
from .outlines import quadratic_source_outline, residual, segment_recording


NATIVE_LANGUAGES = {'CL': 'zh-Hant', 'SC': 'zh-Hans', 'TC': 'zh-Hant', 'HC': 'zh-HK', 'J': 'ja', 'K': 'ko'}
# The OpenType language systems HarfBuzz selects for the audited Latin languages.
LATIN_LANGUAGE_SYSTEMS = {'en': 'ENG ', 'ro': 'ROM ', 'mo': 'MOL ', 'lt': 'LTH ', 'ca': 'CAT '}


def language_systems(font):
    """(script, language) records of a font's GSUB and GPOS."""
    return {(record.ScriptTag, entry.LangSysTag) for tag in ('GSUB', 'GPOS') if tag in font
            for record in font[tag].table.ScriptList.ScriptRecord for entry in record.Script.LangSysRecord}


def feature_tags(font):
    return {record.FeatureTag for tag in ('GSUB', 'GPOS') if tag in font
            for record in font[tag].table.FeatureList.FeatureRecord}


def inter_arguments(text, arguments):
    """Shape Inter as the product routes Cyrillic and Greek: through DFLT.

    Every Cyrillic and Greek language system of the product routes Inter's
    DFLT features: the Far East ones through Sarasa's shareFeatures, the
    defaults by the declared rule. Inter's own cyrl/grek GPOS has only kern,
    so и́ would keep its accent unplaced. A run without a script is read as
    HarfBuzz guesses it (Γ́ in the default context is Greek).
    """
    script = arguments.get('script')
    if script is None:
        buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties()
        script = buffer.script
    return {**arguments, 'script': 'Zyyy'} if script in ('Cyrl', 'Grek') else arguments


def positions(run):
    return [(g.cluster, *g.advance, *g.offset) for g in run]


def ink(run):
    x = y = 0
    result = []
    for glyph in run:
        box = glyph.bounds
        result.append(None if box is None else (x + glyph.offset[0] + box[0], y + glyph.offset[1] + box[1], box[2], box[3]))
        x += glyph.advance[0]; y += glyph.advance[1]
    return result


def near(left, right, tolerance):
    if left is None or right is None:
        return left == right
    return len(left) == len(right) and all(abs(a - b) <= tolerance for a, b in zip(left, right))


def precise_advances(oracle, text, arguments):
    """x advances in 1/64 unit of a 1000-unit em, after the oracle's presets.

    At 1000 units HarfBuzz rounds a 2048-UPM default and each variation
    delta separately; the product rounds Inter's curve once.
    """
    parent = hb.Font(oracle.face)
    parent.scale = (64000, 64000)
    parent.set_variations(oracle.variations)
    font = hb.Font(parent)
    funcs = hb.FontFuncs()
    funcs.set_nominal_glyph_func(lambda _font, cp, _data: oracle.mapping.get(cp, 0))
    font.funcs = funcs
    buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties()
    if arguments.get('script'):
        buffer.script = arguments['script']
    buffer.language = arguments.get('language') or 'und'
    buffer.direction = arguments.get('direction', 'ltr')
    hb.shape(font, buffer, dict(arguments.get('features') or {}))
    return [position.x_advance / 64 for position in buffer.glyph_positions]


def precise_kerning(oracle, text, arguments):
    """Per-glyph kerning of a source in 1/64 unit: kern on minus kern off."""
    on = precise_advances(oracle, text, arguments)
    off = precise_advances(oracle, text, {**arguments, 'features': {**(arguments.get('features') or {}), 'kern': False}})
    return [a - b for a, b in zip(on, off)] if len(on) == len(off) else None


def product_kerning(face, run):
    """Per-glyph kerning of a product: shaped advance minus the glyph's own."""
    return [glyph.advance[0] - face.font.get_glyph_h_advance(glyph.gid) for glyph in run]


def drawn_run(face, run):
    """Replace HarfBuzz integer extents with the drawn outline's ink box.

    HarfBuzz floors and ceils the extents of a scaled face (Inter is 2048
    UPM, measured at 1000) but rounds a 1000-UPM product's to nearest, and
    both follow off-curve control points. Italic Inter VF and static outlines
    often reach the same ink with different control-point extremes, so the
    measurement is the ink itself.
    """
    return tuple(glyph if (box := face.ink(glyph.gid)) is None else replace(glyph, bounds=box) for glyph in run)


class Sources:
    def __init__(self, region, italic, weight, *, variable=True):
        self.region, self.italic, self.weight = region, italic, weight
        self.variable = variable
        weight_name = next((name for name, value in c.WEIGHTS.items() if value == weight), None)
        name = weight_name if not variable else None
        static_inter = c.style_name('Black' if name == 'Heavy' else name, italic) if name else None
        self.inter = InterUiOracle(italic, static_inter); self.inter.set_weight(weight)
        # A paired VF/static comparison also needs the static Inter source of
        # the same weight: the two pinned Inter sources differ in a few glyphs.
        self.inter_static = None
        if variable and weight_name:
            self.inter_static = InterUiOracle(italic, c.style_name('Black' if weight_name == 'Heavy' else weight_name, italic))
            self.inter_static.set_weight(weight)
        # A VF follows Inter's own curve; a static, Inter's static instance as
        # rounded to 1000 units. The rounded VF copy is itself off the curve
        # (ῇ at 200: 2.2 units from a VF that follows the curve).
        if variable:
            self.inter_outline = self.inter
        else:
            self.inter_outline = InterUiOracle(italic, static_inter, round_coordinates=True)
            self.inter_outline.set_weight(weight)
        static_cjk = 'Medium' if name == 'SemiBold' else name
        self.cjk = CjkUiOracle(region, static_weight=static_cjk); self.cjk.set_weight(weight)
        self.classical = CjkUiOracle(region, classical=True, static_weight=static_cjk) if region == 'CL' else None
        if self.classical:
            self.classical.set_weight(weight)
        # Shanggu's VF and static sources draw some ideographs differently
        # (㒶: six contours in the VF, five in the static); a paired CL
        # comparison reads both.
        self.classical_static = None
        if self.classical and variable and weight_name:
            self.classical_static = CjkUiOracle(region, classical=True,
                                                static_weight='Medium' if weight_name == 'SemiBold' else weight_name)
            self.classical_static.set_weight(weight)
        self.box_cache = {}
        self.outline_compatibility = {}
        self.boundary = Face(c.reference_path(region, 'Regular', italic))
        self.localizations = {}
        self.native_routes = {}
        # Sarasa joins language systems exactly. Where Inter has a language
        # the CJK sources lack (latn/ROM, MOL, CAT), the product routes
        # Inter's features alone; a runtime reading a CJK source there falls
        # back to latn/dflt and composes Hangul jamo the product leaves apart.
        cjk_fonts = [self.cjk.metadata] + ([self.classical.metadata] if self.classical else [])
        with TTFont(c.inter_path(italic), lazy=True) as inter_metadata:
            self.inter_only_languages = language_systems(inter_metadata) - set().union(
                *(language_systems(font) for font in cjk_fonts))
        self.cjk_features = set().union(*(feature_tags(font) for font in cjk_fonts))
        self.inter_characters = {}

    def close(self):
        self.cjk.close()
        if self.classical:
            self.classical.close()
        if self.classical_static:
            self.classical_static.close()

    def is_inter(self, text):
        return bool(text) and all(ord(char) in self.inter.mapping
                                 and (inter_owned(ord(char)) or ord(char) not in self.cjk.mapping) for char in text)

    def source(self, text):
        if self.is_inter(text):
            return self.inter
        if self.classical and all(in_ranges(ord(char), IDEOGRAPH_RANGES) for char in text):
            return self.classical
        return self.cjk

    def routes_natively(self, text, context, arguments):
        """Whether the official regional font sets this vertical run as in its own language.

        Source Han routes some vertical forms per language (its Korean VF
        keeps ！：；？ upright for JAN/ZHH/ZHT and rotates the Japanese colon);
        Sarasa Ui K substitutes its own forms for every language. Where the
        official regional font shapes a run like its native language, the
        source is read in that language too.
        """
        native = NATIVE_LANGUAGES[self.region]
        if context.direction != 'ttb' or context.language in ('und', native):
            return False
        key = (text, context, tuple(sorted(arguments['features'].items())))
        if key not in self.native_routes:
            shaped = self.boundary.shape(text, **arguments)
            native_run = self.boundary.shape(text, **{**arguments, 'language': native})
            self.native_routes[key] = [g.gid for g in shaped] == [g.gid for g in native_run]
        return self.native_routes[key]

    def shape(self, text, context, *, feature_overrides=None, face=None):
        face = face or self.source(text)
        arguments = context.arguments()
        if feature_overrides:
            arguments['features'] = {**arguments['features'], **feature_overrides}
        punctuation = c.REGIONAL_PUNCTUATION
        if text and all(ord(char) in punctuation for char in text):
            languages = {'zh-Hans', 'zh-Hant', 'zh-HK', 'ja', 'ko'}
            language = context.language
            if context.script in {'Hani', 'Kana', 'Hang', 'Bopo'} and language not in languages:
                language = NATIVE_LANGUAGES[self.region]
            if text == '⋯':
                language = 'zh-Hans'
            parent = self.classical if self.classical and language in languages - {'ko'} else self.cjk
            face = parent.raw
            # The Cyrillic and Greek default languages take DFLT/dflt's
            # routing by the declared rule, vertical forms included (as in
            # v1.0.43, whose own cyrl/grek vert already gave them).
            default_routed = context.script is None or (context.script in ('Cyrl', 'Grek') and context.language == 'und')
            source_script = None if default_routed and context.direction == 'ttb' else 'Latn'
            arguments = {**arguments, 'script': source_script, 'language': language}
        elif face is not self.inter:
            # Sarasa Ui intentionally narrows Source Han's regional locl
            # repertoire. Read that boundary from the official regional font,
            # not from the current product or numeric lookup positions.
            native = self.routes_natively(text, context, arguments)
            ranges = []
            for index, character in enumerate(text):
                key = (character, context)
                if key not in self.localizations:
                    enabled = self.boundary.shape(character, **{**arguments, 'features': {**arguments['features'], 'locl': True}})
                    disabled = self.boundary.shape(character, **{**arguments, 'features': {**arguments['features'], 'locl': False}})
                    self.localizations[key] = [g.gid for g in enabled] != [g.gid for g in disabled]
                ranges.append((index, index + 1, self.localizations[key]))
            arguments = {**arguments, 'features': {**arguments['features'], 'locl': ranges}}
            if native:
                arguments = {**arguments, 'language': NATIVE_LANGUAGES[self.region]}
        # Western punctuation is the exception: by the documented rule it
        # follows Source Han's runtime, latn/dflt included.
        punctuation_only = bool(text) and all(ord(char) in punctuation for char in text)
        if (face is not self.inter and not punctuation_only and context.script == 'Latn'
                and ('latn', LATIN_LANGUAGE_SYSTEMS.get(context.language)) in self.inter_only_languages):
            arguments = {**arguments, 'features': {tag: False for tag in self.cjk_features}}
        if face is self.inter:
            arguments = inter_arguments(text, arguments)
        run = face.shape(text, **arguments)
        if face is self.inter:
            return run
        result = []
        for glyph in run:
            box = glyph.bounds
            if self.italic and box is not None:
                key = (id(face), glyph.gid)
                if key not in self.box_cache:
                    if getattr(face, 'static_weight', None) is not None:
                        # A static source reaches the static build as AFDKO
                        # cu2qu output, sheared with integer rounding.
                        points = [point for _operation, args in quadratic_source_outline(face, glyph.gid, italic=True)
                                  for point in args if point is not None]
                    else:
                        pen = RecordingPen(); face.font.draw_glyph_with_pen(glyph.gid, pen)
                        points = [(x + (y - 1000 / 3) * tan(radians(9.4)), y)
                                  for operation, args in pen.value for point in args if point is not None
                                  for x, y in [point]]
                    self.box_cache[key] = ((min(x for x, y in points), max(y for x, y in points),
                                            max(x for x, y in points) - min(x for x, y in points),
                                            min(y for x, y in points) - max(y for x, y in points)) if points else box)
                box = self.box_cache[key]
            result.append(replace(glyph, bounds=box))
        return tuple(result)

    def inter_source_difference(self, text, context, actual, reference, expected, actual_face=None, reference_face=None):
        """VF follows Inter VF, static follows Inter static, and their difference
        is exactly the difference between the two pinned Inter sources."""
        static_expected = drawn_run(self.inter_static, self.inter_static.shape(text, **inter_arguments(text, context.arguments())))
        if not (len(actual) == len(reference) == len(expected) == len(static_expected)):
            return False
        for product, source in ((actual, expected), (reference, static_expected)):
            for a, b in zip(product, source):
                if not near(a.advance, b.advance, 1):
                    return False
                if (a.bounds is None) != (b.bounds is None) or (a.bounds is not None and not near(a.bounds[2:], b.bounds[2:], 2)):
                    return False
        if context.direction == 'ltr' and actual_face is not None and reference_face is not None:
            # Each side rounds its own source's kerning once, so VF and static
            # kerning may part by under one unit more than the two sources do,
            # never by two. A glyph's own advance is calibrated to the static
            # at the published weights and is compared elsewhere.
            arguments = inter_arguments(text, context.arguments())
            variable_curve = precise_kerning(self.inter, text, arguments)
            static_curve = precise_kerning(self.inter_static, text, arguments)
            if variable_curve is None or static_curve is None or not (
                    len(variable_curve) == len(static_curve) == len(actual)) or any(
                    abs((a - r) - (x - y)) > 1 for a, r, x, y in zip(
                        product_kerning(actual_face, actual), product_kerning(reference_face, reference),
                        variable_curve, static_curve)):
                return False
        for left, right, source_left, source_right in zip(ink(actual), ink(reference), ink(expected), ink(static_expected)):
            if (left is None) != (right is None) or (source_left is None) != (source_right is None):
                return False
            if left is not None and source_left is not None and not near(
                    (left[0] - right[0], left[1] - right[1]),
                    (source_left[0] - source_right[0], source_left[1] - source_right[1]), 2):
                return False
        return True

    def characters_of(self, face):
        """Glyph ID to the Inter characters a product face maps to it."""
        if id(face) not in self.inter_characters:
            result = {}
            for codepoint in self.inter.mapping:
                gid = face.font.get_nominal_glyph(codepoint)
                if gid:
                    result.setdefault(gid, set()).add(codepoint)
            self.inter_characters[id(face)] = result
        return self.inter_characters[id(face)]

    def inter_glyph_difference(self, context, actual, reference, actual_face, reference_face):
        """VF and static set the same characters' glyphs at the same places,
        and each differs from its counterpart as the two Inter sources draw
        that character on its own. A feature order unlike Inter's (sups before
        its contextual case parentheses in H(H)) changes which glyphs both
        sides pick, never whether they agree; unencoded glyphs have no such
        oracle and stay unexplained."""
        if len(actual) != len(reference) or any(
                a.cluster != b.cluster or a.advance != b.advance or a.offset != b.offset
                for a, b in zip(actual, reference)):
            return False
        single = replace(context, feature='default')
        for a, b in zip(actual, reference):
            shared = self.characters_of(actual_face).get(a.gid, set()) & self.characters_of(reference_face).get(b.gid, set())
            if not shared:
                return False
            text = chr(min(shared))
            left = drawn_run(actual_face, actual_face.shape(text, **single.arguments()))
            right = drawn_run(reference_face, reference_face.shape(text, **single.arguments()))
            if [g.gid for g in left] != [a.gid] or [g.gid for g in right] != [b.gid]:
                return False
            expected = drawn_run(self.inter, self.inter.shape(text, **inter_arguments(text, single.arguments())))
            if len(expected) != 1 or residual(actual_face.outline(a.gid), self.inter_outline.outline(expected[0].gid),
                                              allow_translation=True) > 2:
                return False
            if not self.inter_source_difference(text, single, left, right, expected):
                return False
        return True

    def classical_static_override(self, text, context, actual, actual_face):
        """CL draws these ideographs from the pinned Shanggu 1.028 static where
        Sarasa Ui CL keeps Source Han K or its Shanggu 1.025 subset (㓱 is left
        out of it). The run must be Shanggu's own: the same advances and
        offsets, and the same outlines at the same coordinates, the stored
        points sheared about em/3 for italic. Side bearings or a vertical
        origin borrowed from the official glyph fail."""
        expected = self.shape(text, context, face=self.classical)
        if len(actual) != len(expected) or [g.cluster for g in actual] != [g.cluster for g in expected]:
            return False
        for glyph, source in zip(actual, expected):
            if glyph.advance != source.advance or glyph.offset != source.offset:
                return False
            key = ('classical', id(actual_face), glyph.gid, source.gid)
            if key not in self.outline_compatibility:
                self.outline_compatibility[key] = residual(
                    segment_recording(actual_face.outline(glyph.gid)),
                    quadratic_source_outline(self.classical, source.gid, italic=self.italic)) == 0
            if not self.outline_compatibility[key]:
                return False
        return True

    def classical_source_difference(self, text, context, actual, reference, expected):
        """CL's VF follows Shanggu's VF and its static Shanggu's static, and
        they differ exactly as the two pinned Shanggu sources do. The VF keeps
        the static's horizontal placement (metric alignment), so each axis may
        also simply agree with the static."""
        if self.classical_static is None:
            return False
        static_expected = self.shape(text, context, face=self.classical_static)
        if not (len(actual) == len(reference) == len(expected) == len(static_expected)):
            return False
        for product, source in ((actual, expected), (reference, static_expected)):
            for a, b in zip(product, source):
                if not near(a.advance, b.advance, 1):
                    return False
                if (a.bounds is None) != (b.bounds is None) or (a.bounds is not None and not near(a.bounds[2:], b.bounds[2:], 2)):
                    return False
        for left, right, source_left, source_right in zip(ink(actual), ink(reference), ink(expected), ink(static_expected)):
            if (left is None) != (right is None) or (source_left is None) != (source_right is None):
                return False
            if left is None or source_left is None:
                continue
            for axis in (0, 1):
                if abs(left[axis] - right[axis]) > 2 and abs((left[axis] - right[axis]) - (source_left[axis] - source_right[axis])) > 2:
                    return False
        return True

    def explains(self, text, context, actual, reference, actual_face, reference_face):
        """Return an explicit oracle identifier, or None for an unexplained bug.

        This never accepts a mismatch merely because its codepoint is listed.
        The caller separately checks the exact static source, nominal metrics,
        source outlines, product invariants, and continuous vertical joins.
        """
        punctuation = c.REGIONAL_PUNCTUATION
        if not self.variable and self.classical and text and all(
                in_ranges(ord(char), IDEOGRAPH_RANGES) and ord(char) in self.classical.mapping for char in text):
            return '固定尚古 TC 1.028 静态来源' if self.classical_static_override(text, context, actual, actual_face) else None
        if not self.variable:
            allowed = set(c.INTENTIONAL_CPS) | punctuation
            if self.region == 'CL':
                allowed |= c.CLASSICAL_SPACING_BASES
            if not any(ord(char) in allowed for char in text):
                return None
        if context.direction == 'ttb' and all(char in '—⸺⸻' for char in text):
            count = sum({'—': 1, '⸺': 2, '⸻': 3}[char] for char in text)
            if count > 1 and len(actual) == count and all(g.advance == (0, -1000) for g in actual):
                return '另行逐边核验的 1em 竖排片段'
        # Checked before the run-level source comparison, whose glyphs this
        # case does not share (each glyph is compared on its own below).
        if self.variable and self.inter_static is not None and self.is_inter(text) and self.inter_glyph_difference(
                context, drawn_run(actual_face, actual), drawn_run(reference_face, reference), actual_face, reference_face):
            return 'Inter v4.1 静态与可变来源差异（逐字形）'
        expected = self.shape(text, context)
        if len(actual) != len(expected) or [g.cluster for g in actual] != [g.cluster for g in expected]:
            return None
        source_face = self.source(text)
        if all(ord(char) in punctuation for char in text):
            if all(near(a.advance, b.advance, 1) and near(a.offset, b.offset, 1) and near(a.bounds, b.bounds, 2)
                   for a, b in zip(actual, expected)):
                return '固定 CJK 来源的语言标点'
            return None
        for glyph, upstream in zip(actual, expected):
            key = (id(actual_face), glyph.gid, id(source_face), upstream.gid)
            if key not in self.outline_compatibility:
                a = actual_face.outline(glyph.gid)
                outline_source = self.inter_outline if source_face is self.inter else source_face
                b = outline_source.outline(upstream.gid)
                if self.italic and source_face is not self.inter:
                    slope = tan(radians(9.4))
                    b = tuple((operation, tuple(None if point is None else
                              (point[0] + (point[1] - 1000 / 3) * slope, point[1]) for point in points))
                              for operation, points in b)
                self.outline_compatibility[key] = residual(a, b, allow_translation=True) <= 2
            if not self.outline_compatibility[key]:
                return None
        if self.is_inter(text):
            actual, reference = drawn_run(actual_face, actual), drawn_run(reference_face, reference)
            expected = drawn_run(self.inter, expected)
            if self.variable and self.inter_static is not None and self.inter_source_difference(
                    text, context, actual, reference, expected, actual_face, reference_face):
                return 'Inter v4.1 静态与可变来源差异'
            position_reference = reference if self.variable else expected
            if context.direction != 'ltr':
                valid = (len(actual) == len(position_reference)
                         and all(near(a.advance, b.advance, 0) for a, b in zip(actual, position_reference))
                         and all(a is None and b is None or a is not None and b is not None and near(a[:2], b[:2], 2)
                                 for a, b in zip(ink(actual), ink(position_reference))))
                return 'Inter 轮廓与两端原生竖排位置' if valid else None
            # The product intentionally adopts native Sarasa side bearings.
            # Check those positions against the native run, and shape/width
            # against Inter. Raw Inter's x origin is not the Sarasa origin.
            valid = (len(actual) == len(position_reference)
                     and all(near(a.advance, b.advance, 1)
                             and (a.bounds is None and b.bounds is None or
                                  a.bounds is not None and b.bounds is not None and near(a.bounds[2:], b.bounds[2:], 2))
                             for a, b in zip(actual, expected))
                     and all(a is None and b is None or a is not None and b is not None and near(a[:2], b[:2], 2)
                             for a, b in zip(ink(actual), ink(position_reference))))
            return 'Inter v4.1' if valid else None
        # A nominal shape difference is only explained by the pinned CJK VF;
        # a mismatched localized or vertical form cannot borrow its metrics.
        if not all((a.bounds is None and b.bounds is None) or (a.bounds is not None and b.bounds is not None
                    and near(a.bounds[2:], b.bounds[2:], 2)) for a, b in zip(actual, expected)):
            return None
        if context.feature != 'default':
            disabled = {context.feature: False}
            arguments = {**context.arguments(), 'features': disabled}
            baseline_actual = actual_face.shape(text, **arguments)
            source_face = self.source(text)
            baseline_source = self.shape(text, context, feature_overrides=disabled)
            if len(baseline_actual) != len(actual) or len(baseline_source) != len(expected):
                return None
            sourced = [near(tuple(x - y for x, y in zip((*a.advance, *a.offset), (*a0.advance, *a0.offset))),
                            tuple(x - y for x, y in zip((*s.advance, *s.offset), (*s0.advance, *s0.offset))), 1)
                       for a, a0, s, s0 in zip(actual, baseline_actual, expected, baseline_source)]
            if all(sourced):
                return '固定 CJK 可变来源的实际特性取值'
            # vert/vrt2 select a form whose vertical metrics the product
            # calibrates to the native static, not to the Source Han VF, and
            # each VF origin rounds on its own. Such a glyph is accepted only
            # where it sits exactly as in the native static; the others still
            # need the pinned CJK VF's own value (Sarasa J's rotated colon
            # among ！；？ forms Source Han J routes for other languages).
            if len(actual) == len(reference):
                own, native = drawn_run(actual_face, actual), drawn_run(reference_face, reference)
                placed = [near(a.advance, b.advance, 0) and (a.bounds is None) == (b.bounds is None)
                          and (a.bounds is None or near((a.offset[0] + a.bounds[0], a.offset[1] + a.bounds[1]),
                                                        (b.offset[0] + b.bounds[0], b.offset[1] + b.bounds[1]), 2))
                          for a, b in zip(own, native)]
                if all(source or place for source, place in zip(sourced, placed)):
                    return '固定 CJK 可变来源的实际特性取值'
        if len(actual) != len(reference):
            # A composition the static lacks is the CJK VF's own only if the
            # VF also places every glyph as that source does. Without this,
            # Source Han's Hangul compositions leaking into latn/ROM passed.
            if all(near(a.advance, s.advance, 0) and near(a.offset, s.offset, 1)
                   for a, s in zip(actual, expected)):
                return '固定 CJK 可变来源的组合规则'
            return None
        # Size differences are allowed only when the independently projected
        # source explains them; positions still need to match the static run.
        if any(not near(a.advance, b.advance, 0) for a, b in zip(actual, reference)):
            if all(near(a.advance, source.advance, 0) and near(a.offset, source.offset, 0)
                   for a, source in zip(actual, expected)):
                return '固定 CJK 来源的默认定位与字偶距'
            return None
        # The VF and static reach Source Han as separate quadratic conversions
        # of its VF and static sources: the same ink can have different
        # off-curve extremes (〱 by 5 units, Heavy ﾍ by 49), so a position is
        # the drawn ink.
        drawn_actual, drawn_reference = ink(drawn_run(actual_face, actual)), ink(drawn_run(reference_face, reference))
        if any((a is None) != (b is None) for a, b in zip(drawn_actual, drawn_reference)):
            return None
        if all(a is None or near(a[:2], b[:2], 2) for a, b in zip(drawn_actual, drawn_reference)):
            return '固定 CJK 可变来源的轮廓与整数坐标'
        if source_face is self.classical and self.classical_source_difference(text, context, actual, reference, expected):
            return '固定尚古静态与可变来源差异'
        return None
