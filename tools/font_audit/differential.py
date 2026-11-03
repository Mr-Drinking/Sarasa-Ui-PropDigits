"""Independent runtime comparisons. No build module or build verdict is used."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
from functools import lru_cache

from fontTools.ttLib import TTFont

from .shaping import Face, geometry_delta, reverse_texts
from .contract import sha256


# Latin, Greek and Cyrillic with their combining marks, phonetic and
# historic extensions, punctuation, super/subscripts and currency signs.
WESTERN_RANGES = ((0x0000, 0x052F), (0x1C80, 0x1C8F), (0x1D00, 0x1FFF), (0x2000, 0x20CF),
                  (0x2DE0, 0x2DFF), (0xA640, 0xA69F), (0xFB00, 0xFB06))


def western(codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in WESTERN_RANGES)


@dataclass(frozen=True)
class Context:
    script: str | None = None
    language: str = "und"
    direction: str = "ltr"
    feature: str = "default"
    # "western" limits a context to the text a Latin, Cyrillic or Greek
    # language system actually shapes, instead of all ~45,000 probes.
    repertoire: str = "all"

    def arguments(self) -> dict:
        return {"script": self.script, "language": self.language,
                "direction": self.direction,
                "features": {} if self.feature == "default" else {self.feature: True}}

    def admits(self, text: str) -> bool:
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
)
WESTERN_LANGUAGES = ("en", "ro", "mo", "lt", "ca")
CJK_LANGUAGES = ("zh-Hans", "zh-Hant", "zh-HK", "ja", "ko")
LANGUAGES = ("und", *WESTERN_LANGUAGES, *CJK_LANGUAGES)
# Optional features VF and static must apply alike (single-glyph alternates
# that no default context reaches). Sarasa Ui keeps Inter's ss01-ss08 and
# cv01-cv14 records but drops their alternates, so those shape as default.
# salt is left out as a known defect (README): the VF never calibrates its
# unencoded salt alternates to Inter's curve (Ἱ.salt is 6 units wide of it at
# 900 italic, as in v1.0.43), so VF and static part by up to that much.
OPTIONAL_FEATURES = ("case", "frac", "numr", "dnom", "sups", "subs", "sinf", "ordn", "zero", "tnum", "dlig")


def default_contexts() -> tuple[Context, ...]:
    contexts = [Context(direction=direction) for direction in ("ltr", "ttb")]
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


def paired_contexts() -> tuple[Context, ...]:
    """VF against static: also every optional Inter feature."""
    return default_contexts() + tuple(Context(script="Latn", feature=feature, repertoire="western")
                                      for feature in OPTIONAL_FEATURES)


def context_texts(context: Context, codepoints, runs) -> list[str]:
    """The probes one context measures, in measurement order."""
    return [text for text in (*(chr(cp) for cp in codepoints), *runs) if context.admits(text)]


def source_sequences(paths: tuple[Path, ...]) -> tuple[str, ...]:
    return _source_sequences(tuple((path, sha256(path)) for path in paths))


@lru_cache(maxsize=32)
def _source_sequences(inputs: tuple[tuple[Path, str], ...]) -> tuple[str, ...]:
    texts = set(RUNS)
    for path, _digest in inputs:
        with TTFont(path, lazy=True) as font:
            texts.update(text for text in reverse_texts(font).values() if len(text) > 1)
    return tuple(sorted(texts))


def compare_pair(actual_path: Path, expected_path: Path, *, weight: int | None = None,
                 reference_weight: int | None = None, contexts: Iterable[Context] | None = None,
                 codepoints: Iterable[int] | None = None, sample_limit: int = 32,
                 collect_differences: list | None = None, outlines: bool = False,
                 explanation=None, probe_sources: tuple[Path, ...] | None = None) -> dict:
    """Measure both fonts in HarfBuzz, including runtime vertical origins.

    No discrepancy is waived here. An explicit policy may classify the returned
    differences, but must also verify its replacement oracle.
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
