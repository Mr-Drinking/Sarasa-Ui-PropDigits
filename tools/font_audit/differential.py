"""Independent runtime comparisons. No build module or build verdict is used."""
from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
from functools import lru_cache

from fontTools.ttLib import TTFont

from .shaping import Face, geometry_delta, reverse_texts
from . import contract as c
from .contract import sha256


# Latin, Greek and Cyrillic with their combining marks, phonetic and
# historic extensions, punctuation, super/subscripts and currency signs.
WESTERN_RANGES = ((0x0000, 0x052F), (0x1C80, 0x1C8F), (0x1D00, 0x1FFF), (0x2000, 0x20CF),
                  (0x2DE0, 0x2DFF), (0xA640, 0xA69F), (0xFB00, 0xFB06))


def western(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in WESTERN_RANGES)


# CJK ideographs, as Sarasa's make/helpers/unicode-kind.mjs isIdeograph.
IDEOGRAPHS = ((0x2E80, 0x2FFF), (0x3192, 0x319F), (0x31C0, 0x31EF), (0x3400, 0x4DBF),
              (0x4E00, 0x9FFF), (0xF900, 0xFA6F), (0x20000, 0x3FFFF))


def ideograph(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in IDEOGRAPHS)


def _vertical_form_repertoire() -> frozenset[int]:
    """Unicode's vertical presentation forms and the characters they stand for.

    Without vert, HarfBuzz itself replaces these characters by their
    presentation forms (U+2014 by U+FE31) before any lookup runs; the
    fullwidth forms of the ASCII sources are mapped the same way.
    """
    result = {0x2E3A, 0x2E3B}
    for codepoint in (*range(0xFE10, 0xFE1A), *range(0xFE30, 0xFE50)):
        decomposition = unicodedata.decomposition(chr(codepoint))
        if decomposition.startswith("<vertical>"):
            source = int(decomposition.split()[1], 16)
            result.update({codepoint, source})
            if 0x21 <= source <= 0x7E:
                result.add(source + 0xFEE0)
    return frozenset(result)


VERTICAL_FORM_REPERTOIRE = _vertical_form_repertoire()


@dataclass(frozen=True)
class Context:
    script: str | None = None
    language: str = "und"
    direction: str = "ltr"
    # A feature tag turned on, or "-tag" for one a context turns off.
    feature: str = "default"
    # "western" limits a context to the text a Latin, Cyrillic or Greek
    # language system actually shapes, instead of all ~45,000 probes;
    # "ideographs" to the ideographs an optional CJK feature reaches;
    # "vertical-forms" to the characters with a vertical presentation form.
    repertoire: str = "all"

    def arguments(self) -> dict:
        if self.feature == "default":
            features = {}
        elif self.feature.startswith("-"):
            features = {self.feature[1:]: False}
        else:
            features = {self.feature: True}
        return {"script": self.script, "language": self.language,
                "direction": self.direction, "features": features}

    def admits(self, text: str) -> bool:
        if self.repertoire == "ideographs":
            return all(ideograph(ord(char)) for char in text)
        if self.repertoire == "vertical-forms":
            return all(ord(char) in VERTICAL_FORM_REPERTOIRE for char in text)
        return self.repertoire == "all" or all(western(ord(char)) for char in text)


# These strings supplement exhaustive single-codepoint and source-derived
# ligature probes. They are inputs, never a list of expected builder results.
RUNS = (
    "7.", "09:41", "12.5", "T.", "GT", "Go", "LY", "AVATAR", "ToWa",
    "i\u0307", "j\u0301", "\u012f\u0301", "\u015e\u0162\u015f\u0163", "\u3057\u3059", "\u3068\u3063", "\u304b\u309a",
    "\u3033\u3035", "\u3034\u3035", "\u1100\u1161\u11a8", "\u1113\u1161\u11a8",
    "\u1159\u119e\u11f9", "\u3127\u3127", "\u31b4\u31b5\u31b6\u31b7\u31bb", "\u3008\u3009\uff5e", "\u00b7\u2022\u2027\u30fb",
    "\uff01\uff1a\uff1b\uff1f", "\u2018\u2019\u201c\u201d", "\u32a9\u32aa\u3292\u3293\u3295",
    "\u66f4\u6cbf\u4fbf\u9f8d\u6bba\u5951\u7387\u964d\u8b80\u6190\u96a3\u9c57\u9e9f\u540f\u533f\u71d0\u51de",
    "0123456789", "H@H", "H(H)", "a::::::::::2", "1:", ":2", "1:2",
    # Inter's contextual arrows are not ligature subtables, so source
    # sequences never reach them; Cyrillic and Greek bases with combining
    # marks exercise mark/mkmk and the soft-dotted rules per language.
    "->", "<-", "<->", "=>", "<=>", "-->",
    "\u0438\u0301", "\u0456\u0301", "\u0457\u0301", "\u0430\u0300\u0301", "\u0393\u0301", "\u03c9\u0342",
    # Old Hangul L+V pairs Source Han's ccmp ligates unless a trailing
    # consonant follows (Sarasa Ui's static drops that guard and splits
    # U+1100 U+1188 U+11A8 into two cells); with and without the T jamo.
    "\u1100\u1188", "\u1100\u1188\u11a8", "\u1100\u1188\u11f0", "\u1100\u1188\ud7cb",
    "\u112d\u119e\u11a8", "\u1132\u119e\u11f0", "\u112f\u11a1\ud7cb", "\u1140\u1175\u11a8",
    "\u1120\u1161\u11f0", "\u1121\u1169\ud7cb", "\u1127\u1165\u11a8", "\u1122\u1173\u11f0",
    "\u112d\u119e", "\u1140\u1175",
    # Shanggu's own dlig ligatures (era names, 株式会社), which the full
    # Shanggu brought into v1.0.43's CL static and Sarasa Ui CL lacks.
    "\u660e\u6cbb", "\u5927\u6b63", "\u662d\u548c", "\u5e73\u6210", "\u4ee4\u548c", "\u682a\u5f0f\u4f1a\u793e",
    # Dash runs, the presentation form U+FE31 among them: up to v1.0.43.2 a
    # ccmp ligature merged two U+FE31 into one glyph, horizontally and in
    # vertical text without vert (where HarfBuzz turns U+2014 into U+FE31).
    "\u2014\u2014", "\u2014\u2014\u2014", "\ufe31\ufe31", "\ufe31\ufe31\ufe31", "\u2e3a\u2e3a", "\u2e3a\u2014",
    # frac and ordn reach nothing in a single character or the runs above.
    "1/2", "3/4", "10/31", "1a", "2o", "No",
)
WESTERN_LANGUAGES = ("en", "ro", "mo", "lt", "ca")
CJK_LANGUAGES = ("zh-Hans", "zh-Hant", "zh-HK", "ja", "ko")
LANGUAGES = ("und", *WESTERN_LANGUAGES, *CJK_LANGUAGES)
# Optional features VF and static must apply alike (single-glyph alternates
# that no default context reaches). Sarasa Ui keeps Inter's ss01-ss08 and
# cv01-cv13 records with their alternates dropped, so those shape as
# default; cv14 still swaps ẞ (U+1E9E) for its alternate, and salt reaches
# unencoded alternates such as Ἱ's (6 units off Inter's curve at 900 italic
# in the VF up to v1.0.43.1).
OPTIONAL_FEATURES = ("case", "frac", "numr", "dnom", "sups", "subs", "sinf", "ordn", "zero", "tnum", "dlig",
                     "salt", "cv14")


def default_contexts() -> tuple[Context, ...]:
    contexts = [Context(direction=direction) for direction in ("ltr", "ttb")]
    # Vertical text with vert off: HarfBuzz's own presentation forms must
    # stay one glyph each, as in Source Han and Sarasa Ui.
    contexts.extend(Context(script=script, language=language, direction="ttb", feature="-vert",
                            repertoire="vertical-forms")
                    for script, language in ((None, "und"), ("Hani", "zh-Hans")))
    contexts.extend(Context(script="Hani", language=language, direction=direction)
                    for language in CJK_LANGUAGES for direction in ("ltr", "ttb"))
    contexts.extend(Context(script="Latn", language=language)
                    for language in WESTERN_LANGUAGES)
    # Each Cyrillic or Greek run reaches its own default language and the Far
    # East languages under that script (v1.0.43's VF lost mark/mkmk there).
    for script, native in (("Cyrl", "ru"), ("Grek", "el")):
        contexts.extend(Context(script=script, language=language, repertoire="western")
                        for language in ("und", native, *CJK_LANGUAGES))
        contexts.append(Context(script=script, direction="ttb", repertoire="western"))
    contexts.extend(Context(script="Hani", direction=direction, feature=feature)
                    for direction, features in (("ltr", ("palt", "halt", "kern")),
                                                ("ttb", ("vpal", "vhal", "vert", "vrt2")))
                    for feature in features)
    return tuple(contexts)


def static_contexts() -> tuple[Context, ...]:
    """Static against the release: also the optional CJK features on ideographs.

    CL's full Shanggu brought its own dlig (era names, 株式会社) and aalt into
    v1.0.43's static; the static keeps only what Sarasa Ui CL's features
    reach. The VF carries no CJK source's dlig or aalt (README), so these are
    not paired against it.
    """
    return default_contexts() + tuple(Context(script="Hani", feature=feature, repertoire="ideographs")
                                      for feature in ("dlig", "aalt"))


def paired_contexts() -> tuple[Context, ...]:
    """VF against static: also every optional Inter feature."""
    return default_contexts() + tuple(Context(script="Latn", feature=feature, repertoire="western")
                                      for feature in OPTIONAL_FEATURES)


def context_texts(context: Context, codepoints, runs) -> list[str]:
    """The probes one context measures, in measurement order."""
    return [text for text in (*(chr(cp) for cp in codepoints), *runs) if context.admits(text)]


def source_sequences(paths: tuple[Path, ...]) -> tuple[str, ...]:
    inter = tuple(c.inter_path(italic) for italic in (False, True))
    return _source_sequences(tuple((path, sha256(path)) for path in paths),
                             tuple((path, sha256(path)) for path in inter))


# Marks above and below a base an optional Inter feature replaces: such an
# alternate carries its own anchors (up to v1.0.43.2 the VF's salt forms of
# U+1E6D and U+0163 took their marks 3 units off the static at 300 italic).
MARK_PROBES = ("\u0301", "\u0308", "\u0323")


def optional_feature_bases(font: TTFont) -> set[int]:
    """Encoded letters a single substitution of an optional feature replaces."""
    reverse: dict[str, int] = {}
    for codepoint, name in sorted(font.getBestCmap().items()):
        reverse.setdefault(name, codepoint)
    result: set[int] = set()
    if "GSUB" not in font:
        return result
    table = font["GSUB"].table
    for record in table.FeatureList.FeatureRecord:
        if record.FeatureTag not in OPTIONAL_FEATURES:
            continue
        for index in record.Feature.LookupListIndex:
            for subtable in table.LookupList.Lookup[index].SubTable:
                part = getattr(subtable, "ExtSubTable", subtable)
                for source in getattr(part, "mapping", None) or {}:
                    codepoint = reverse.get(source)
                    if (codepoint is not None and western(codepoint)
                            and unicodedata.category(chr(codepoint)).startswith("L")):
                        result.add(codepoint)
    return result


@lru_cache(maxsize=32)
def _source_sequences(inputs: tuple[tuple[Path, str], ...], inter: tuple[tuple[Path, str], ...]) -> tuple[str, ...]:
    texts = set(RUNS)
    for path, _digest in inputs:
        with TTFont(path, lazy=True) as font:
            texts.update(text for text in reverse_texts(font).values() if len(text) > 1)
    for path, _digest in inter:
        with TTFont(path, lazy=True) as font:
            texts.update(chr(codepoint) + mark for codepoint in optional_feature_bases(font) for mark in MARK_PROBES)
    return tuple(sorted(texts))


# Characters Sarasa Ui does not encode: each shapes as the missing-glyph box.
MISSING_PROBES = (0x0149, 0x01C4, 0x0378, 0x03A2)


def notdef_parity(actual_path: Path, expected_path: Path, *, weight: int | None = None,
                  reference_weight: int | None = None) -> dict:
    """The .notdef box every unmapped character shows, horizontally and vertically.

    No cmap probe reaches GID 0: up to v1.0.43.1 the VF kept Source Han's
    1000-unit box while the static and Sarasa Ui use Inter's 656-unit one.
    """
    with TTFont(actual_path, lazy=True) as left_font, TTFont(expected_path, lazy=True) as right_font:
        mapped = set(left_font.getBestCmap()) | set(right_font.getBestCmap())
    absent = [cp for cp in MISSING_PROBES if cp not in mapped]
    if not absent:
        return {"cases": 1, "failures": 1, "samples": [{"reason": "没有两边都不编码的探测码位"}]}
    actual, expected = Face(actual_path), Face(expected_path)
    actual.set_weight(weight)
    expected.set_weight(reference_weight)
    cases = failures = 0
    samples = []
    # Inside a run too; only the box itself is compared (the letters around
    # it differ between VF and static, and between weights, by design).
    for text in (chr(absent[0]), "A" + chr(absent[0]) + "B"):
        for direction in ("ltr", "ttb"):
            left = tuple(g for g in actual.shape(text, direction=direction) if g.gid == 0)
            right = tuple(g for g in expected.shape(text, direction=direction) if g.gid == 0)
            if not left or not right:
                delta = {"notdef": "unmapped probe did not reach GID 0"}
            else:
                delta = geometry_delta(left, right)
                if actual.outline_key(0) != expected.outline_key(0):
                    delta["outline"] = True
            cases += 1
            if delta:
                failures += 1
                samples.append({"text": " ".join(f"U+{ord(char):04X}" for char in text), "direction": direction,
                                "difference": delta})
    return {"cases": cases, "failures": failures, "samples": samples}


def compare_pair(actual_path: Path, expected_path: Path, *, weight: int | None = None,
                 reference_weight: int | None = None, contexts: Iterable[Context] | None = None,
                 codepoints: Iterable[int] | None = None, sample_limit: int = 32,
                 collect_differences: list | None = None, outlines: bool = False,
                 explanation=None, probe_sources: tuple[Path, ...] | None = None,
                 drawn_positions: bool = False) -> dict:
    """Measure both fonts in HarfBuzz, including runtime vertical origins.

    No discrepancy is waived here. An explicit policy may classify the returned
    differences, but must also verify its replacement oracle.

    drawn_positions also compares where each glyph's outline is drawn. A
    static font's HarfBuzz extents come from hmtx and the stored glyph box
    and the outline comparison ignores translation, so an outline moved as
    a whole under unchanged stored bounds went unnoticed (a rasterizer draws
    it moved).
    """
    with TTFont(actual_path, lazy=True) as actual_font, TTFont(expected_path, lazy=True) as reference_font:
        left_cmap, right_cmap = actual_font.getBestCmap(), reference_font.getBestCmap()
        shared = set(left_cmap) & set(right_cmap)
        cps = sorted(shared if codepoints is None else set(codepoints) & shared)
        runs = source_sequences(probe_sources or (expected_path,))
        runs = [text for text in runs if all(ord(char) in shared for char in text)]
    actual, expected = Face(actual_path), Face(expected_path)
    actual.set_weight(weight)
    expected.set_weight(reference_weight)
    results, total, differences, failures = [], 0, 0, 0
    for context in contexts or default_contexts():
        arguments = context.arguments()
        fields, samples, count, failed, explained = Counter(), [], 0, 0, Counter()
        sequence_failed = positioning_failed = outlines_failed = 0
        texts = context_texts(context, cps, runs)
        for text in texts:
            left, right = actual.shape(text, **arguments), expected.shape(text, **arguments)
            delta = geometry_delta(left, right)
            if outlines and len(left) == len(right):
                if [actual.outline_key(g.gid) for g in left] != [expected.outline_key(g.gid) for g in right]:
                    delta['outline'] = True
            if drawn_positions and len(left) == len(right):
                for a, b in zip(left, right):
                    ink_a, ink_b = actual.ink(a.gid), expected.ink(b.gid)
                    if (ink_a is None) != (ink_b is None) or (ink_a is not None and any(
                            abs(x - y) > 0.5 for x, y in zip(ink_a[:2], ink_b[:2]))):
                        delta['drawn_position'] = True
                        break
            total += 1
            if delta:
                count += 1
                fields.update(delta.keys())
                item = {"text": text, "context": asdict(context), "difference": delta}
                reason = explanation(text, context, left, right, actual, expected) if explanation else None
                if reason:
                    explained[reason] += 1
                else:
                    failed += 1
                    sequence_failed += int(len(text) > 1)
                    positioning_failed += int(bool(set(delta) & {'advance', 'offset'}))
                    outlines_failed += int(bool(set(delta) & {'outline', 'glyph_count', 'cluster', 'bounds'}))
                if not reason and len(samples) < sample_limit:
                    samples.append(item)
                if collect_differences is not None:
                    collect_differences.append(item)
        differences += count
        failures += failed
        results.append({**asdict(context), "cases": len(texts),
                        "differences": count, "failures": failed, "explained": dict(explained),
                        "sequence_cases": sum(len(text) > 1 for text in texts), "sequence_failures": sequence_failed,
                        "positioning_failures": positioning_failed, "outline_failures": outlines_failed,
                        "fields": dict(fields), "samples": samples})
    return {"codepoints": len(cps), "sequences": len(runs), "cases": total,
            "differences": differences, "failures": failures, "contexts": results,
            "cmap_missing": sorted(set(right_cmap) - set(left_cmap)),
            "cmap_extra": sorted(set(left_cmap) - set(right_cmap))}
