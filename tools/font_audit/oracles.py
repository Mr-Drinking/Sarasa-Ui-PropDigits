"""Read the pinned sources through independent OpenType runtime oracles."""
from __future__ import annotations

from bisect import bisect_right
import io

from fontTools.ttLib import TTFont
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap
import uharfbuzz as hb

from . import contract as c
from .products import InterUiOracle
from .shaping import Face


IDEOGRAPH_RANGES = ((0x2E80, 0x2FFF), (0x3192, 0x319F), (0x31C0, 0x31EF),
                    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFA6F), (0x20000, 0x3FFFF))
HANGUL_RANGES = ((0x1100, 0x11FF), (0xAC00, 0xD7AF), (0x3130, 0x318F),
                 (0x3200, 0x321E), (0x3260, 0x327F), (0xFFA1, 0xFFDC),
                 (0xA960, 0xA97F), (0xD7B0, 0xD7FF))


def in_ranges(cp, ranges):
    return any(first <= cp <= last for first, last in ranges)


def inter_owned(cp):
    # Declarative Sarasa repertoire policy, independent of target glyph names.
    if cp == 0x22EF or in_ranges(cp, IDEOGRAPH_RANGES + HANGUL_RANGES):
        return False
    if in_ranges(cp, ((0x20DD, 0x20DE), (0x2460, 0x24FF), (0x2776, 0x2788), (0xE000, 0xF8FF))):
        return False
    western = (cp < 0x2000 and cp != 0xB7) or 0x2070 <= cp <= 0x218F
    locale_punctuation = {0xFF01, 0xFF08, 0xFF09, 0xFF0C, 0xFF0E, 0xFF1A,
                          0xFF1B, 0xFF3B, 0xFF3D, 0xFF5B, 0xFF5D, 0xFF1F}
    symbols = (0x2000 <= cp <= 0x200F or 0x20A0 <= cp < 0x3000) and cp not in (0x2E3A, 0x2E3B)
    symbols |= 0xFF01 <= cp <= 0xFF5E and cp not in locale_punctuation
    east_asian_misc = in_ranges(cp, ((0x3003, 0x3007), (0x3012, 0x3013), (0x3020, 0x33FF),
                                     (0x1AFF0, 0x1B12F), (0x1F000, 0x1F2FF)))
    return western or symbols or east_asian_misc


def cjk_weight(public_weight, source):
    axis = source['fvar'].axes[0]
    segment = source['avar'].segments['wght'] if 'avar' in source else {-1: -1, 0: 0, 1: 1}
    def mapped(weight):
        return piecewiseLinearMap(normalizeValue(weight, (axis.minValue, axis.defaultValue, axis.maxValue)), segment)
    public_map = {weight: mapped(internal) for weight, internal in c.CJK_WEIGHTS.items()}
    normalized = piecewiseLinearMap(piecewiseLinearMap(public_weight, public_map), {v: k for k, v in segment.items()})
    return axis.defaultValue + normalized * (axis.defaultValue - axis.minValue if normalized < 0 else axis.maxValue - axis.defaultValue)


def single_mapping(font, feature):
    if 'GSUB' not in font:
        return {}
    table = font['GSUB'].table
    mapping = {}
    for record in table.FeatureList.FeatureRecord:
        if record.FeatureTag != feature:
            continue
        for index in record.Feature.LookupListIndex:
            for outer in table.LookupList.Lookup[index].SubTable:
                part = getattr(outer, 'ExtSubTable', outer)
                for original, replacement in getattr(part, 'mapping', {}).items():
                    if isinstance(replacement, str):
                        mapping[original] = replacement
    return mapping


class CjkUiOracle(Face):
    def __init__(self, region, *, classical=False, static_weight=None):
        self.static_weight = static_weight
        if static_weight is not None:
            if classical:
                path = c.SOURCE_ROOT / f'shanggu-1.028/static/ShangguSansTC-{static_weight}.ttf'
                self.metadata = TTFont(path, lazy=True)
            else:
                path = c.SARASA_SOURCE / f'sources/shs/SourceHanSans-{static_weight}.ttc'
                index = {'J': 0, 'CL': 1, 'K': 1, 'SC': 2, 'TC': 3, 'HC': 4}[region]
                self.metadata = TTFont(path, fontNumber=index, lazy=True)
            stream = io.BytesIO(); self.metadata.save(stream)
            self.data = stream.getvalue()
        else:
            path = c.SOURCE_ROOT / 'shanggu-1.028/ShangguSansTC-VF.ttf' if classical else c.cjk_path(region)
            self.metadata = TTFont(path, lazy=True)
            self.data = path.read_bytes()
        self.face = hb.Face(self.data)
        self.parent = hb.Font(self.face); self.parent.scale = (1000, 1000)
        self.font = hb.Font(self.parent); self.variations = {}
        self.raw = Face.__new__(Face)
        self.raw.data = self.data; self.raw.face = self.face; self.raw.font = self.parent
        self.raw.variations = {}; self.raw.outline_cache = {}; self.raw.outline_key_cache = {}
        self.raw.metadata = self.metadata; self.raw.static_weight = static_weight
        substitutions = single_mapping(self.metadata, 'pwid')
        # Sarasa's make/punct/sanitize-symbols.mjs applies pwid only to this
        # repertoire; applying it globally would turn fullwidth Latin into a
        # different source oracle and hide a real layout regression.
        pwid = {0xB7, 0x2010, 0x2018, 0x2019, 0x201C, 0x201D, 0x2025, 0x2026,
                0x31B4, 0x31B5, 0x31B6, 0x31B7, 0x31BB}
        self.mapping = {cp: self.metadata.getGlyphID(substitutions.get(name, name) if cp in pwid else name)
                        for cp, name in self.metadata.getBestCmap().items()}
        funcs = hb.FontFuncs()
        funcs.set_nominal_glyph_func(lambda _font, cp, _data: self.mapping.get(cp, 0))
        self.font.funcs = funcs

    def set_weight(self, weight):
        self.variations = {} if self.static_weight is not None else {'wght': cjk_weight(weight, self.metadata)}
        self.outline_cache = {}; self.outline_key_cache = {}
        self.raw.outline_cache = {}; self.raw.outline_key_cache = {}
        self.parent.set_variations(self.variations); self.font.set_variations(self.variations)

    def close(self):
        self.metadata.close()
