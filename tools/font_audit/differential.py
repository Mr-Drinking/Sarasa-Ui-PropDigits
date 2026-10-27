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


@dataclass(frozen=True)
class Context:
    script: str | None = None
    language: str = "und"
    direction: str = "ltr"
    feature: str = "default"

    def arguments(self) -> dict:
        return {"script": self.script, "language": self.language,
                "direction": self.direction,
                "features": {} if self.feature == "default" else {self.feature: True}}


# These strings supplement exhaustive single-codepoint and source-derived
# ligature probes. They are inputs, never a list of expected builder results.
RUNS = (
    "7.", "09:41", "12.5", "T.", "GT", "Go", "LY", "AVATAR", "ToWa",
    "i\u0307", "j\u0301", "į\u0301", "ŞŢşţ", "しす", "とっ", "か\u309a",
    "〳〵", "〴〵", "\u1100\u1161\u11a8", "\u1113\u1161\u11a8",
    "\u1159\u119e\u11f9", "ㄧㄧ", "ㆴㆵㆶㆷㆻ", "〈〉～", "·•‧・",
    "！：；？", "‘’“”", "㊩㊪㊒㊓㊕", "更沿便龍殺契率降讀憐隣鱗麟吏匿燐凞",
    "0123456789", "H@H", "H(H)", "a::::::::::2", "1:", ":2", "1:2",
)
LANGUAGES = ("und", "en", "ro", "mo", "lt", "zh-Hans", "zh-Hant", "zh-HK", "ja", "ko")


def default_contexts() -> tuple[Context, ...]:
    contexts = [Context(direction=direction) for direction in ("ltr", "ttb")]
    contexts.extend(Context(script="Hani", language=language, direction=direction)
                    for language in LANGUAGES[5:] for direction in ("ltr", "ttb"))
    contexts.extend(Context(script="Latn", language=language)
                    for language in LANGUAGES[1:5])
    contexts.extend(Context(script="Hani", direction=direction, feature=feature)
                    for direction, features in (("ltr", ("palt", "halt", "kern")),
                                                ("ttb", ("vpal", "vhal", "vert", "vrt2")))
                    for feature in features)
    return tuple(contexts)


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
        for text in [*(chr(cp) for cp in cps), *runs]:
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
        results.append({**asdict(context), "cases": len(cps) + len(runs),
                        "differences": count, "failures": failed, "explained": dict(explained),
                        "sequence_cases": len(runs), "sequence_failures": sequence_failed,
                        "positioning_failures": positioning_failed, "outline_failures": outlines_failed,
                        "fields": dict(fields), "samples": samples})
    return {"codepoints": len(cps), "sequences": len(runs), "cases": total,
            "differences": differences, "failures": failures, "contexts": results,
            "cmap_missing": sorted(set(right_cmap) - set(left_cmap)),
            "cmap_extra": sorted(set(left_cmap) - set(right_cmap))}
