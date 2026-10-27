"""Native Heavy sources and black-box positioning values, independent of build code."""
from __future__ import annotations

import copy
import io
import math
import tempfile
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont

from . import contract as c
from .comparison import Sources
from .differential import Context, default_contexts, source_sequences
from .outlines import quadratic_source_outline, residual, segment_recording
from .oracles import IDEOGRAPH_RANGES, in_ranges
from .results import Result
from .shaping import Face


_SORTED_REFERENCES: dict[tuple[str, str], Path] = {}
_SORTED_DIRECTORY: tempfile.TemporaryDirectory | None = None


def sorted_pair_reference(path: Path) -> Path:
    """The pinned release with GPOS PairSets in the order OpenType requires.

    Upstream stores some PairValueRecords out of SecondGlyph order, so binary
    search engines such as HarfBuzz miss values the font contains (e.g. the
    kana pair とっ at ExtraLight). Sorting by glyph ID changes no stored value.
    """
    global _SORTED_DIRECTORY
    key = (str(path), c.sha256(path))
    if key not in _SORTED_REFERENCES:
        if _SORTED_DIRECTORY is None:
            _SORTED_DIRECTORY = tempfile.TemporaryDirectory(prefix="sarasa-audit-reference-")
        with TTFont(path, recalcBBoxes=False, recalcTimestamp=False) as font:
            changed = 0
            for lookup in font["GPOS"].table.LookupList.Lookup if "GPOS" in font else ():
                for outer in lookup.SubTable:
                    part = getattr(outer, "ExtSubTable", outer)
                    if getattr(part, "Format", None) != 1 or not hasattr(part, "PairSet"):
                        continue
                    for pair_set in part.PairSet:
                        ordered = sorted(pair_set.PairValueRecord, key=lambda record: font.getGlyphID(record.SecondGlyph))
                        changed += ordered != pair_set.PairValueRecord
                        pair_set.PairValueRecord = ordered
            target = Path(_SORTED_DIRECTORY.name) / f"{len(_SORTED_REFERENCES)}-{path.name}"
            if changed:
                font.save(target)
            else:
                target = path
        _SORTED_REFERENCES[key] = target
    return _SORTED_REFERENCES[key]


def without_positioning(original):
    """Disable every GPOS feature while retaining GSUB, cmap presets and variations.

    The table itself stays: without any GPOS HarfBuzz applies its own fallback
    mark positioning from glyph extents, which would be measured as if it were
    the font's positioning.
    """
    with TTFont(io.BytesIO(original.data)) as font:
        if 'GPOS' in font:
            table = font['GPOS'].table
            table.FeatureList.FeatureRecord = []; table.FeatureList.FeatureCount = 0
            for record in table.ScriptList.ScriptRecord:
                for system in [record.Script.DefaultLangSys, *(entry.LangSys for entry in record.Script.LangSysRecord)]:
                    if system is not None:
                        system.FeatureIndex = []; system.FeatureCount = 0; system.ReqFeatureIndex = 0xFFFF
            if getattr(table, 'FeatureVariations', None) is not None:
                table.FeatureVariations = None
        stream = io.BytesIO(); font.save(stream)
    result = copy.copy(original)
    result.data = stream.getvalue(); result.face = hb.Face(result.data)
    result.parent = hb.Font(result.face); result.parent.scale = (1000, 1000)
    result.parent.set_variations(original.variations)
    if hasattr(original, 'mapping'):
        result.font = hb.Font(result.parent)
        funcs = hb.FontFuncs()
        funcs.set_nominal_glyph_func(lambda _font, cp, _data: result.mapping.get(cp, 0))
        if not hasattr(original, 'metadata'):  # Inter's declared native vertical cell
            funcs.set_glyph_v_advance_func(lambda _font, _gid, _data: -1000)
            funcs.set_glyph_v_origin_func(lambda _font, gid, _data:
                                         (True, result.parent.get_glyph_h_advance(gid) // 2, 880))
        result.font.funcs = funcs
        result.font.set_variations(original.variations)
    else:
        result.font = result.parent
    result.outline_cache = {}; result.outline_key_cache = {}; result.ink_cache = {}
    if hasattr(original, 'raw'):
        result.raw = copy.copy(original.raw)
        result.raw.data = result.data; result.raw.face = result.face; result.raw.font = result.parent
        result.raw.outline_cache = {}; result.raw.outline_key_cache = {}; result.raw.ink_cache = {}
    return result


def positioning_effect(enabled, disabled):
    # GPOS cannot change the glyph sequence. Reject a broken control face
    # instead of zipping unequal lists and losing the tail of the comparison.
    if [(g.gid, g.cluster) for g in enabled] != [(g.gid, g.cluster) for g in disabled]:
        raise ValueError('移除 GPOS 后 GSUB 字形序列发生变化')
    return [tuple(a - b for a, b in zip((*left.advance, *left.offset), (*right.advance, *right.offset)))
            for left, right in zip(enabled, disabled)]


def effects_match(actual, expected, inter):
    return all(abs(x - y) <= (2 if inter and field >= 2 else 1 if inter else 0)
               for field, (x, y) in enumerate(zip(actual, expected)))


def routing_drops_positioning(actual, expected, released, source_bold, *, inter=False):
    """Whether a Heavy run differs from Source Han Heavy only where Sarasa routes no positioning.

    Sarasa may route none where Source Han positions, for a whole run (kana
    kern under latn/ROM) or for some of its glyphs (J's vertical ！？ take
    Sarasa's vertical forms without vpal while ： keeps it). Every glyph must
    match Source Han Heavy or be unpositioned like the pinned Bold release, and
    at least one of those must be a glyph whose Bold source effect the release
    removes.
    """
    if not len(actual) == len(expected) == len(released) == len(source_bold):
        return False
    dropped = [not any(value) and not any(gone) for value, gone in zip(actual, released)]
    return (all(drop or effects_match(value, wanted, inter) for value, wanted, drop in zip(actual, expected, dropped))
            and any(drop and any(kept) for drop, kept in zip(dropped, source_bold)))


def heavy_sources(path, region, italic, *, contexts=None, codepoints=None):
    """Verify Heavy without inventing a Sarasa Heavy release or borrowing Bold.

    The source is Inter Black plus Source Han Heavy CFF (Shanggu Heavy for CL).
    Every cmap outline is compared; positioning is measured with and without
    GPOS on both ends, so per-glyph Format 2 values cannot collapse unnoticed.
    """
    source = Sources(region, italic, 900, variable=False)
    baseline = copy.copy(source)
    baseline.inter = without_positioning(source.inter)
    baseline.cjk = without_positioning(source.cjk)
    baseline.classical = without_positioning(source.classical) if source.classical else None
    baseline.box_cache = {}
    actual = Face(path); unpositioned = without_positioning(actual)
    # Sarasa Ui narrows Source Han's language-system routing (for example, no
    # Hangul or kana composition under latn/ROM). That routing is the same for
    # every weight, so a structural difference from Source Han is accepted only
    # when it equals the pinned Bold release exactly; its positioning effect is
    # then compared with that release instead.
    routing = Face(c.reference_path(region, 'Bold', italic)); routing_unpositioned = without_positioning(routing)
    with TTFont(path, lazy=True) as font:
        repertoire = sorted(font.getBestCmap() if codepoints is None else codepoints)
    repertoire_set = set(repertoire)
    probes = (c.reference_path(region, 'Regular', italic), c.cjk_path(region), c.inter_path(italic))
    sequences = [text for text in source_sequences(probes) if all(ord(char) in repertoire_set for char in text)]
    outlines, positioning = Result(), Result()
    cache = {}
    bold = bold_baseline = None
    try:
        for cp in repertoire:
            text = chr(cp); face = source.source(text)
            observed = actual.shape(text, language='und')
            expected = source.shape(text, Context())
            valid = len(observed) == len(expected) and all(g.gid for g in expected)
            error = 0
            if valid:
                for a, b in zip(observed, expected):
                    key = (a.gid, id(face), b.gid)
                    if key not in cache:
                        left = segment_recording(actual.outline(a.gid))
                        right = quadratic_source_outline(face, b.gid, italic=italic, inter=face is source.inter)
                        cache[key] = residual(left, right, allow_translation=True)
                    error = max(error, cache[key])
                valid = error <= (2 if face is source.inter else 0)
            outlines.check(valid, {'cp': f'U+{cp:04X}', 'residual': error if math.isfinite(error) else 'topology',
                                  'source_glyphs': [g.gid for g in expected]})
        context_rows = []
        for context in contexts or default_contexts():
            count, failures = positioning.cases, positioning.failures
            for text in [*(chr(cp) for cp in repertoire), *sequences]:
                args = context.arguments()
                run = actual.shape(text, **args)
                a = positioning_effect(run, unpositioned.shape(text, **args))
                b = positioning_effect(source.shape(text, context), baseline.shape(text, context))
                if len(a) != len(b):
                    released = routing.shape(text, **args)
                    if [g.cluster for g in released] == [g.cluster for g in run]:
                        b = positioning_effect(released, routing_unpositioned.shape(text, **args))
                if region != 'CL' and all(in_ranges(ord(char), IDEOGRAPH_RANGES) for char in text):
                    # Pinned Sarasa make/kanji/build.mjs explicitly drops OTL
                    # before selecting ideographs. CL merges Shanggu after
                    # this step and therefore must keep its own source rules.
                    b = [(0, 0, 0, 0)] * len(b)
                inter = source.is_inter(text)
                valid = len(a) == len(b) and all(effects_match(left, right, inter) for left, right in zip(a, b))
                segments = 0
                if context.direction == 'ttb' and all(char in '—⸺⸻' for char in text):
                    segments = sum({'—': 1, '⸺': 2, '⸻': 3}[char] for char in text)
                    if segments > 1:
                        valid = len(a) == segments and all(value == (0, 0, 0, 0) for value in a + b)
                # Joined dash segments carry no positioning, whatever the routing.
                if not valid and len(a) == len(b) and not (segments > 1 and any(any(value) for value in a)):
                    if bold is None:
                        bold = Sources(region, italic, 700, variable=False)
                        bold_baseline = copy.copy(bold); bold_baseline.box_cache = {}
                        bold_baseline.inter = without_positioning(bold.inter); bold_baseline.cjk = without_positioning(bold.cjk)
                        bold_baseline.classical = without_positioning(bold.classical) if bold.classical else None
                    released = positioning_effect(routing.shape(text, **args), routing_unpositioned.shape(text, **args))
                    source_bold = positioning_effect(bold.shape(text, context), bold_baseline.shape(text, context))
                    valid = routing_drops_positioning(a, b, released, source_bold, inter=inter)
                positioning.check(valid, {'text': text, 'context': vars(context), 'actual': a, 'source': b})
            context_rows.append({**vars(context), 'cases': positioning.cases - count,
                                 'failures': positioning.failures - failures})
    finally:
        source.close()
        if bold is not None:
            bold.close()
    return {'cases': outlines.cases + positioning.cases, 'failures': outlines.failures + positioning.failures,
            'samples': (outlines.samples + positioning.samples)[:24],
            'outline_comparison': outlines.report(), 'positioning_comparison': positioning.report(contexts=context_rows),
            'codepoints': len(repertoire), 'sequences': len(sequences)}
