"""Native Heavy sources and black-box positioning values, independent of build code."""
from __future__ import annotations

import copy
import io
import math
import tempfile
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot

from . import contract as c
from .comparison import Sources
from .products import joined_segment_run_reasons
from .differential import Context, context_texts, default_contexts, source_sequences
from .outlines import quadratic_source_outline, residual, segment_recording
from .oracles import IDEOGRAPH_RANGES, in_ranges
from .results import Result
from .shaping import Face


_PROJECT_REFERENCES: dict[tuple[str, str], Path] = {}
_PROJECT_DIRECTORY: tempfile.TemporaryDirectory | None = None
# The project's one declared routing change from the release: the Cyrillic
# and Greek default languages take the DFLT/dflt features, as Sarasa's
# shareFeatures gives the Far East languages, except vertical substitutions.
SHARED_DEFAULT_SCRIPTS = ("cyrl", "grek")
OWN_VERTICAL_FEATURES = ("vert", "vrt2")


def share_default_language_features(font: TTFont) -> int:
    """Apply the declared rule to a release, one combined record per tag."""
    changed = 0
    for tag in ("GSUB", "GPOS"):
        if tag not in font:
            continue
        table = font[tag].table
        records = table.FeatureList.FeatureRecord
        scripts = {record.ScriptTag: record.Script for record in table.ScriptList.ScriptRecord}
        default = scripts["DFLT"].DefaultLangSys if "DFLT" in scripts else None
        for name in SHARED_DEFAULT_SCRIPTS:
            system = scripts[name].DefaultLangSys if name in scripts else None
            if default is None or system is None:
                continue
            for index in list(default.FeatureIndex):
                feature_tag = records[index].FeatureTag
                own = [i for i in system.FeatureIndex if records[i].FeatureTag == feature_tag]
                if own and feature_tag in OWN_VERTICAL_FEATURES:
                    continue
                lookups = sorted(set(records[index].Feature.LookupListIndex).union(
                    *(records[i].Feature.LookupListIndex for i in own)))
                if len(own) == 1 and lookups == sorted(set(records[own[0]].Feature.LookupListIndex)):
                    continue
                # HarfBuzz applies only the first record of a tag in a
                # language system, so a shared tag becomes one record.
                record = copy.deepcopy(records[index])
                record.Feature.LookupListIndex = lookups
                record.Feature.LookupCount = len(lookups)
                records.append(record)
                system.FeatureIndex = sorted([i for i in system.FeatureIndex if i not in own] + [len(records) - 1])
                system.FeatureCount = len(system.FeatureIndex)
                changed += 1
        table.FeatureList.FeatureCount = len(records)
    return changed


SOURCE_HAN_FACES = {'J': 0, 'CL': 1, 'K': 1, 'SC': 2, 'TC': 3, 'HC': 4}
L_JAMO = ((0x1100, 0x115F), (0xA960, 0xA97F))
V_JAMO = ((0x1160, 0x11A7), (0xD7B0, 0xD7C6))
T_JAMO = ((0x11A8, 0x11FF), (0xD7CB, 0xD7FB))


def source_han_static(region: str, weight: str) -> tuple[Path, int]:
    name = 'Medium' if weight == 'SemiBold' else weight
    return c.SARASA_SOURCE / f'sources/shs/SourceHanSans-{name}.ttc', SOURCE_HAN_FACES[region]


def hangul_ccmp_guards(source: TTFont) -> list[tuple[int, int, frozenset[int]]]:
    """Source Han's old-Hangul ccmp guards, by codepoint.

    In front of each L+V ligature, Source Han's ccmp has a chained rule with
    no substitution for the same pair followed by a trailing consonant, so
    ljmo/vjmo/tjmo compose the whole syllable instead.
    """
    if 'GSUB' not in source:
        return []
    table = source['GSUB'].table
    codepoints: dict[str, int] = {}
    for codepoint, name in sorted(source.getBestCmap().items()):
        codepoints.setdefault(name, codepoint)
    guards = []
    for index in sorted({i for record in table.FeatureList.FeatureRecord if record.FeatureTag == 'ccmp'
                         for i in record.Feature.LookupListIndex}):
        for outer in table.LookupList.Lookup[index].SubTable:
            part = getattr(outer, 'ExtSubTable', outer)
            if (not hasattr(part, 'LookAheadCoverage') or part.Format != 3 or part.SubstLookupRecord
                    or part.BacktrackCoverage or len(part.InputCoverage) != 2 or len(part.LookAheadCoverage) != 1
                    or any(len(coverage.glyphs) != 1 for coverage in part.InputCoverage)):
                continue
            first, second = (codepoints.get(coverage.glyphs[0]) for coverage in part.InputCoverage)
            trailing = frozenset(codepoints.get(name) for name in part.LookAheadCoverage[0].glyphs)
            if (first is not None and second is not None and None not in trailing and in_ranges(first, L_JAMO)
                    and in_ranges(second, V_JAMO) and all(in_ranges(cp, T_JAMO) for cp in trailing)):
                guards.append((first, second, trailing))
    return guards


def substitutes_pair(part, first: str, second: str) -> bool:
    """A context or chained context subtable with a rule substituting exactly first+second."""
    chained = hasattr(part, 'BacktrackCoverage') or hasattr(part, 'ChainSubClassSet') or hasattr(part, 'ChainSubRuleSet')
    if part.Format == 3:
        inputs = part.InputCoverage if chained else part.Coverage
        return (len(inputs) == 2 and first in inputs[0].glyphs and second in inputs[1].glyphs and bool(part.SubstLookupRecord)
                and not (chained and (part.BacktrackCoverage or part.LookAheadCoverage)))
    if first not in part.Coverage.glyphs:
        return False
    if part.Format == 2:
        classes = (part.InputClassDef if chained else part.ClassDef).classDefs
        sets = (part.ChainSubClassSet if chained else part.SubClassSet) or []
        index = classes.get(first, 0)
        rules = []
        if index < len(sets) and sets[index] is not None:
            rules = sets[index].ChainSubClassRule if chained else sets[index].SubClassRule
        return any(list(rule.Input if chained else rule.Class) == [classes.get(second, 0)] and rule.SubstLookupRecord
                   and not (chained and (rule.Backtrack or rule.LookAhead)) for rule in rules or [])
    sets = (part.ChainSubRuleSet if chained else part.SubRuleSet) or []
    index = part.Coverage.glyphs.index(first)
    rules = []
    if index < len(sets) and sets[index] is not None:
        rules = sets[index].ChainSubRule if chained else sets[index].SubRule
    return any(list(rule.Input) == [second] and rule.SubstLookupRecord
               and not (chained and (rule.Backtrack or rule.LookAhead)) for rule in rules or [])


def as_chained(part):
    """The same rules in a chained context subtable, which can share a lookup with the guards."""
    if hasattr(part, 'BacktrackCoverage') or hasattr(part, 'ChainSubClassSet') or hasattr(part, 'ChainSubRuleSet'):
        return part
    chained = ot.ChainContextSubst()
    chained.Format = part.Format
    if part.Format == 3:
        chained.BacktrackCoverage, chained.InputCoverage, chained.LookAheadCoverage = [], list(part.Coverage), []
        chained.BacktrackGlyphCount, chained.InputGlyphCount, chained.LookAheadGlyphCount = 0, len(part.Coverage), 0
        chained.SubstLookupRecord, chained.SubstCount = list(part.SubstLookupRecord), len(part.SubstLookupRecord)
        return chained
    chained.Coverage = part.Coverage
    def rule_of(kind, rule):
        item = kind()
        item.Backtrack, item.LookAhead, item.BacktrackGlyphCount, item.LookAheadGlyphCount = [], [], 0, 0
        item.Input = list(rule.Class if part.Format == 2 else rule.Input)
        item.InputGlyphCount = rule.GlyphCount
        item.SubstLookupRecord, item.SubstCount = list(rule.SubstLookupRecord), len(rule.SubstLookupRecord)
        return item
    if part.Format == 2:
        chained.InputClassDef = part.ClassDef
        chained.BacktrackClassDef, chained.LookAheadClassDef = ot.ClassDef(), ot.ClassDef()
        chained.BacktrackClassDef.classDefs, chained.LookAheadClassDef.classDefs = {}, {}
        chained.ChainSubClassSet = []
        for rule_set in part.SubClassSet:
            target = None
            if rule_set is not None:
                target = ot.ChainSubClassSet()
                target.ChainSubClassRule = [rule_of(ot.ChainSubClassRule, rule) for rule in rule_set.SubClassRule]
                target.ChainSubClassRuleCount = len(target.ChainSubClassRule)
            chained.ChainSubClassSet.append(target)
        chained.ChainSubClassSetCount = len(chained.ChainSubClassSet)
        return chained
    chained.ChainSubRuleSet = []
    for rule_set in part.SubRuleSet:
        target = None
        if rule_set is not None:
            target = ot.ChainSubRuleSet()
            target.ChainSubRule = [rule_of(ot.ChainSubRule, rule) for rule in rule_set.SubRule]
            target.ChainSubRuleCount = len(target.ChainSubRule)
        chained.ChainSubRuleSet.append(target)
    chained.ChainSubRuleSetCount = len(chained.ChainSubRuleSet)
    return chained


def guard_hangul_compositions(font: TTFont, guards) -> int:
    """The declared Hangul rule applied to a release: Source Han's guards go
    in front of the ccmp lookup that ligates each pair (Sarasa's static
    pipeline dropped them and splits L·V·T such as U+1100 U+1188 U+11A8)."""
    cmap = font.getBestCmap()
    pairs = [(cmap[first], cmap[second], sorted({cmap[cp] for cp in trailing if cp in cmap}, key=font.getGlyphID))
             for first, second, trailing in guards if first in cmap and second in cmap]
    pairs = [pair for pair in pairs if pair[2]]
    if not pairs or 'GSUB' not in font:
        return 0
    table = font['GSUB'].table
    owners = [index for index in sorted({i for record in table.FeatureList.FeatureRecord if record.FeatureTag == 'ccmp'
                                          for i in record.Feature.LookupListIndex})
              if table.LookupList.Lookup[index].LookupType in (5, 6)
              and all(any(substitutes_pair(part, first, second) for part in table.LookupList.Lookup[index].SubTable)
                      for first, second, _trailing in pairs)]
    if len(owners) != 1:
        raise ValueError(f'上游成品中承载古韩文 L+V 连字的 ccmp lookup 不唯一：{owners}')
    lookup = table.LookupList.Lookup[owners[0]]
    present = {(part.InputCoverage[0].glyphs[0], part.InputCoverage[1].glyphs[0]) for part in lookup.SubTable
               if hasattr(part, 'LookAheadCoverage') and part.Format == 3 and not part.SubstLookupRecord
               and len(part.InputCoverage) == 2}
    guarded = []
    for first, second, trailing in pairs:
        if (first, second) in present:
            continue
        guard = ot.ChainContextSubst()
        guard.Format = 3
        guard.BacktrackCoverage, guard.BacktrackGlyphCount = [], 0
        guard.InputCoverage = []
        for name in (first, second):
            coverage = ot.Coverage(); coverage.glyphs = [name]
            guard.InputCoverage.append(coverage)
        guard.InputGlyphCount = 2
        following = ot.Coverage(); following.glyphs = list(trailing)
        guard.LookAheadCoverage, guard.LookAheadGlyphCount = [following], 1
        guard.SubstLookupRecord, guard.SubstCount = [], 0
        guarded.append(guard)
    if guarded:
        lookup.SubTable = guarded + [as_chained(part) for part in lookup.SubTable]
        lookup.SubTableCount = len(lookup.SubTable)
        lookup.LookupType = 6
    return len(guarded)


def guarded_composition_status(data: bytes, source_path: Path, face_index: int, guards) -> list[dict]:
    """The rewritten release composes each guarded L+V(+T) as Source Han does."""
    fonts = (hb.Font(hb.Face(data)), hb.Font(hb.Face(hb.Blob.from_file_path(str(source_path)), face_index)))
    problems = []
    for first, second, trailing in guards:
        for text in (chr(first) + chr(second), chr(first) + chr(second) + chr(min(trailing))):
            counts = []
            for font in fonts:
                buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties(); buffer.language = 'ko'
                hb.shape(font, buffer, {})
                counts.append(len(buffer.glyph_infos))
            if counts[0] != counts[1]:
                problems.append({'text': ' '.join(f'U+{ord(char):04X}' for char in text), 'glyphs': counts})
    return problems


def project_reference(path: Path, region: str, weight: str) -> Path:
    """The pinned release as this project routes it.

    Upstream stores some PairValueRecords out of SecondGlyph order, so binary
    search engines such as HarfBuzz miss values the font contains (e.g. the
    kana pair とっ at ExtraLight); sorting by glyph ID changes no stored
    value. The two declared layout changes are applied as stated,
    independently of the builder: the Cyrillic and Greek routing
    (share_default_language_features), and Source Han's old-Hangul ccmp
    guards read from the pinned static source of the same region and weight.
    """
    global _PROJECT_DIRECTORY
    key = (str(path), c.sha256(path), region, weight)
    if key not in _PROJECT_REFERENCES:
        if _PROJECT_DIRECTORY is None:
            _PROJECT_DIRECTORY = tempfile.TemporaryDirectory(prefix="sarasa-audit-reference-")
        source_path, face_index = source_han_static(region, weight)
        with TTFont(source_path, fontNumber=face_index, lazy=True) as source:
            guards = hangul_ccmp_guards(source)
        if not guards:
            raise ValueError(f'固定 Source Han 静态来源没有古韩文 ccmp 保护规则：{source_path.name} #{face_index}')
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
            changed += share_default_language_features(font)
            changed += guard_hangul_compositions(font, guards)
            target = Path(_PROJECT_DIRECTORY.name) / f"{len(_PROJECT_REFERENCES)}-{path.name}"
            if changed:
                font.save(target)
            else:
                target = path
        problems = guarded_composition_status(target.read_bytes(), source_path, face_index, guards)
        if problems:
            raise ValueError(f'改写后的上游成品没有按 Source Han 组合古韩文：{problems[:4]}')
        _PROJECT_REFERENCES[key] = target
    return _PROJECT_REFERENCES[key]


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


def heavy_metrics(path, region, italic):
    """Heavy's cmap advances and upright ink positions, from its own sources.

    Heavy has no release, and the outline comparison allows translation. The
    rule each codepoint follows is read where a release exists (Bold, and
    Regular for weight-independent widths): the release keeps the source's
    advance and position, rounds a Hangul advance up to whole ems (centring
    the ink), or uses one width at every weight (Sarasa's half-width
    symbols). Heavy must apply that rule to Source Han Heavy, Inter Black
    and Shanggu Heavy (CL ideographs keep Shanggu's own position). The
    declared differences are checked by their own oracles. On a sheared
    curve a source's control-point box is not its ink, so italic positions
    compare the drawn ink of each source as the static build receives it
    (CJK sources sheared about em/3, Inter's own italic): the outline check
    allows any translation and left italic positions unchecked until v1.0.43.3.
    """
    declared = set(c.INTENTIONAL_CPS) | set(c.REGIONAL_PUNCTUATION) | (set(c.CLASSICAL_SPACING_BASES) if region == 'CL' else set())
    releases = {weight: Face(c.reference_path(region, weight, italic)) for weight in ('Regular', 'Bold')}
    product = Face(path)
    bold, heavy = Sources(region, italic, 700, variable=False), Sources(region, italic, 900, variable=False)
    result = Result()
    rules = {}

    def single(run, face=None):
        if len(run) != 1:
            return None
        ink = face.ink(run[0].gid) if face is not None else run[0].bounds
        return run[0].advance[0], None if ink is None else ink[0]

    def sheared_source(sources, text, value):
        """An italic source run's advance with its drawn, sheared ink left edge."""
        if value is None or not italic:
            return value
        run = sources.shape(text, Context())
        face = sources.source(text)
        if face is sources.inter:
            ink = face.ink(run[0].gid)
            return value[0], None if ink is None else ink[0]
        pen = BoundsPen(None)
        for operation, points in quadratic_source_outline(face, run[0].gid, italic=True):
            getattr(pen, operation)(*points)
        return value[0], None if pen.bounds is None else pen.bounds[0]
    try:
        with TTFont(path, lazy=True) as font:
            repertoire = sorted(cp for cp in font.getBestCmap() if cp not in declared)
        for cp in repertoire:
            text = chr(cp)
            released = single(releases['Bold'].shape(text, language='und'), releases['Bold'])
            regular = single(releases['Regular'].shape(text, language='und'))
            bold_source = sheared_source(bold, text, single(bold.shape(text, Context())))
            heavy_source = sheared_source(heavy, text, single(heavy.shape(text, Context())))
            actual = single(product.shape(text, language='und'), product)
            if None in (released, regular, bold_source, heavy_source, actual):
                # A combining mark shaped alone gains a dotted circle (U+302E
                # in Hangul). Such runs are compared glyph by glyph by the
                # outline and positioning checks; only a run that is multi-glyph
                # on one side alone is a failure here.
                both = released is None and actual is None
                rules['multi_glyph'] = rules.get('multi_glyph', 0) + both
                if not both:
                    result.check(False, {'cp': f'U+{cp:04X}', 'check': 'heavy_metrics_single_glyph'})
                continue
            shift = None
            if region == 'CL' and heavy.source(text) is heavy.classical:
                rule, expected, shift = 'shanggu', heavy_source[0], 0
            elif released[0] == bold_source[0]:
                rule, expected = 'source', heavy_source[0]
                if released[1] is not None and bold_source[1] is not None:
                    shift = released[1] - bold_source[1]
            elif bold_source[0] > 0 and released[0] == -(-bold_source[0] // 1000) * 1000:
                rule, expected = 'whole_em', -(-heavy_source[0] // 1000) * 1000
                if released[1] is not None and bold_source[1] is not None:
                    shift = ((released[1] - bold_source[1]) - (released[0] - bold_source[0]) / 2
                             + (expected - heavy_source[0]) / 2)
            elif released[0] == regular[0]:
                rule, expected = 'constant', released[0]
            else:
                result.check(False, {'cp': f'U+{cp:04X}', 'check': 'heavy_metrics_rule',
                                     'release': released[0], 'source': bold_source[0]})
                continue
            rules[rule] = rules.get(rule, 0) + 1
            result.check(actual[0] == expected, {'cp': f'U+{cp:04X}', 'check': 'heavy_advance', 'rule': rule,
                                                 'actual': actual[0], 'expected': expected})
            if shift is not None and actual[1] is not None and heavy_source[1] is not None:
                miss = (actual[1] - heavy_source[1]) - shift
                result.check(abs(miss) <= 2, {'cp': f'U+{cp:04X}', 'check': 'heavy_ink_left', 'rule': rule,
                                              'miss': round(miss, 2)})
    finally:
        bold.close()
        heavy.close()
    return {**result.report(), 'rules': rules}


def heavy_sources(path, region, italic, *, contexts=None, codepoints=None):
    """Verify Heavy without inventing a Sarasa Heavy release.

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
    # when it equals the pinned Bold release, as the project routes it,
    # exactly; its positioning effect is then compared with that release.
    # Outlines and the positioning of runs Source Han Heavy shapes the same
    # way come from Inter Black and Source Han Heavy; only such structurally
    # different runs borrow the Bold release's positioning effect.
    routing = Face(project_reference(c.reference_path(region, 'Bold', italic), region, 'Bold')); routing_unpositioned = without_positioning(routing)
    with TTFont(path, lazy=True) as font:
        repertoire = sorted(font.getBestCmap() if codepoints is None else codepoints)
    repertoire_set = set(repertoire)
    probes = (c.reference_path(region, 'Regular', italic), c.cjk_path(region), c.inter_path(italic))
    sequences = [text for text in source_sequences(probes) if all(ord(char) in repertoire_set for char in text)]
    outlines, positioning, reached = Result(), Result(), Result()
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
            for text in context_texts(context, repertoire, sequences):
                args = context.arguments()
                run = actual.shape(text, **args)
                source_run = source.shape(text, context)
                a = positioning_effect(run, unpositioned.shape(text, **args))
                b = positioning_effect(source_run, baseline.shape(text, context))
                # Glyphs only GSUB reaches (vertical, localized and ligature
                # forms) had no outline comparison at all: every glyph of a
                # run that matches its source glyph for glyph is drawn as the
                # source draws it, like the cmap glyphs above.
                if (len(run) == len(source_run) and all(g.gid for g in source_run)
                        and not all(ord(char) in c.REGIONAL_PUNCTUATION for char in text)):
                    face = source.source(text)
                    for glyph, origin in zip(run, source_run):
                        key = (glyph.gid, id(face), origin.gid)
                        if key in cache:
                            continue
                        left = segment_recording(actual.outline(glyph.gid))
                        right = quadratic_source_outline(face, origin.gid, italic=italic, inter=face is source.inter)
                        cache[key] = residual(left, right, allow_translation=True)
                        # Sarasa's punctuation modules move vertical forms;
                        # sheared about em/3, the moved points round to half
                        # units (CL Heavy Italic: ！：；？ 0.5 off the source).
                        reached.check(cache[key] <= (2 if face is source.inter else 0.5 if italic else 0),
                                      {'text': text, 'context': vars(context), 'check': 'gsub_reached_outline',
                                       'residual': cache[key] if math.isfinite(cache[key]) else 'topology'})
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
                if (context.direction == 'ttb' and not context.feature.startswith('-')
                        and all(ord(char) in c.VERTICAL_DASH_SEGMENTS for char in text)):
                    segments = sum(c.VERTICAL_DASH_SEGMENTS[ord(char)] for char in text)
                    if segments > 1:
                        # Italic runs joined by context take curs offsets
                        # (one shear width per cell); the joined geometry is
                        # what must hold, not zero positioning.
                        valid = not joined_segment_run_reasons(actual, run, segments, italic=italic,
                                                               strict=text in {'——', '———', '⸺', '⸻'})
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
    return {'cases': outlines.cases + reached.cases + positioning.cases,
            'failures': outlines.failures + reached.failures + positioning.failures,
            'samples': (outlines.samples + reached.samples + positioning.samples)[:24],
            'outline_comparison': outlines.report(), 'gsub_reached_outlines': reached.report(),
            'positioning_comparison': positioning.report(contexts=context_rows),
            'codepoints': len(repertoire), 'sequences': len(sequences)}
