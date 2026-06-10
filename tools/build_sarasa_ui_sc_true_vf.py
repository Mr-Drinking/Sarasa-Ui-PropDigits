from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from fontTools.misc.fixedTools import otRound
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.varLib.instancer import instantiateVariableFont


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "work" / "vf-sources"
OUT_DIR = ROOT / "outputs"

BASE_VF = SRC_DIR / "SourceHanSansSC-VF.ttf"
INTER_UPRIGHT = SRC_DIR / "InterVariable.ttf"
INTER_ITALIC = SRC_DIR / "InterVariable-Italic.woff2"
REFERENCE_SARASA = ROOT / "work" / "sarasa" / "SarasaUiSC-TTF-Unhinted-1.0.39" / "SarasaUiSC-Regular.ttf"

AXIS_LIMIT = {"wght": (250, 400, 900)}
INTER_AXIS_LIMIT = {"opsz": 14, "wght": (250, 400, 900)}
FAMILY = "Sarasa Ui VF PropDigits SC"
PS_FAMILY = "Sarasa-Ui-VF-PropDigits-SC"

SOURCE_HAN_WEIGHT_STOPS = [
    {"name": "ExtraLight", "value": 250, "range_min": 250, "range_max": 299},
    {"name": "Light", "value": 300, "range_min": 300, "range_max": 349},
    {"name": "Normal", "value": 350, "range_min": 350, "range_max": 399},
    {"name": "Regular", "value": 400, "range_min": 400, "range_max": 499, "flags": 0x2},
    {"name": "Medium", "value": 500, "range_min": 500, "range_max": 650},
    {"name": "Bold", "value": 700, "range_min": 650, "range_max": 800},
    {"name": "Heavy", "value": 900, "range_min": 800, "range_max": 900},
]

DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
DIGITS_TF = [f"{name}.tf" for name in DIGITS]
INTER_PREFIX = "inter."


def set_name_record(font: TTFont, name_id: int, value: str) -> None:
    name_table = font["name"]
    records = [n for n in name_table.names if n.nameID == name_id]
    if not records:
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)
        records = [n for n in name_table.names if n.nameID == name_id]
    for record in records:
        record.string = value.encode(record.getEncoding())


def update_names(font: TTFont, italic: bool) -> None:
    subfamily = "Italic" if italic else "Regular"
    full = FAMILY + (" Italic" if italic else "")
    ps = PS_FAMILY + ("-Italic" if italic else "")
    version = "Version 1.0.39-truevf-propdigits"
    replacements = {
        1: FAMILY,
        2: subfamily,
        3: ps + ";1.0.39-truevf-propdigits",
        4: full,
        5: version,
        6: ps,
        16: FAMILY,
        17: subfamily,
        25: ps,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)
    font["name"].names = [
        n
        for n in font["name"].names
        if not (
            n.nameID in {1, 2, 3, 4, 5, 6, 16, 17, 25}
            and n.platformID == 3
            and n.langID not in {0x409}
        )
    ]


def update_style_flags(font: TTFont, italic: bool) -> None:
    font["OS/2"].usWeightClass = 400
    font["OS/2"].fsSelection |= 1 << 6
    font["OS/2"].fsSelection &= ~(1 << 5)
    if italic:
        font["head"].macStyle |= 0b10
        font["OS/2"].fsSelection |= 0b1
        font["OS/2"].fsSelection &= ~(1 << 6)
        font["post"].italicAngle = -9.4
    else:
        font["head"].macStyle &= ~0b10
        font["OS/2"].fsSelection &= ~0b1
        font["post"].italicAngle = 0


def rebuild_stat(font: TTFont, italic: bool) -> None:
    from fontTools.otlLib.builder import buildStatTable

    weight_values = [
        {
            "nominalValue": stop["value"],
            "rangeMinValue": stop["range_min"],
            "rangeMaxValue": stop["range_max"],
            "name": stop["name"],
            "flags": stop.get("flags", 0),
        }
        for stop in SOURCE_HAN_WEIGHT_STOPS
    ]
    axes = [
        {
            "tag": "wght",
            "name": "Weight",
            "values": weight_values,
        },
        {
            "tag": "ital",
            "name": "Italic",
            "values": [
                {
                    "value": 1 if italic else 0,
                    "name": "Italic" if italic else "Roman",
                    "flags": 0x2 if not italic else 0,
                }
            ],
        },
    ]
    buildStatTable(font, axes)


def update_fvar_instances(font: TTFont, italic: bool) -> None:
    name_table = font["name"]
    existing = {
        int(round(instance.coordinates.get("wght", -1))): instance
        for instance in font["fvar"].instances
    }
    instances = []
    for stop in SOURCE_HAN_WEIGHT_STOPS:
        weight_name = stop["name"]
        weight_value = stop["value"]
        instance = existing.get(weight_value) or NamedInstance()
        instance.coordinates = {"wght": float(weight_value)}
        instance.flags = 0
        instance.subfamilyNameID = name_table.addName(weight_name)
        ps_suffix = weight_name + ("Italic" if italic else "")
        instance.postscriptNameID = name_table.addName(f"{PS_FAMILY}-{ps_suffix}")
        instances.append(instance)
    font["fvar"].instances = instances


def reference_unicodes() -> set[int]:
    font = TTFont(REFERENCE_SARASA)
    try:
        return set(font.getBestCmap().keys())
    finally:
        font.close()


def is_western(codepoint: int) -> bool:
    return (codepoint < 0x2000 and codepoint != 0x00B7) or (0x2070 <= codepoint <= 0x218F)


def source_han_unicodes_like_sarasa() -> set[int]:
    # Sarasa's Source Han punctuation/non-kanji path drops western characters
    # and lets the Latin source supply them. This is the part that frees enough
    # glyph slots for Inter instead of hitting the TTF 65535-glyph ceiling.
    return {codepoint for codepoint in reference_unicodes() if not is_western(codepoint)}


def ensure_gvar_keys(font: TTFont) -> None:
    if "gvar" not in font:
        return
    variations = font["gvar"].variations
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in variations:
            variations[glyph_name] = []


def subset_base_like_sarasa(font: TTFont, unicodes: set[int]) -> None:
    ensure_gvar_keys(font)
    options = subset.Options()
    options.layout_features = "*"
    options.name_IDs = "*"
    options.name_legacy = True
    options.name_languages = "*"
    options.notdef_outline = True
    options.recommended_glyphs = True
    options.glyph_names = True
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=unicodes)
    subsetter.subset(font)
    ensure_gvar_keys(font)


def load_base(italic: bool) -> TTFont:
    base = TTFont(BASE_VF)
    base = instantiateVariableFont(base, AXIS_LIMIT, inplace=False, optimize=True)
    subset_base_like_sarasa(base, source_han_unicodes_like_sarasa())
    if italic:
        shear_font(base, 9.4)
    return base


def load_inter(italic: bool) -> TTFont:
    inter = TTFont(INTER_ITALIC if italic else INTER_UPRIGHT)
    inter = instantiateVariableFont(inter, INTER_AXIS_LIMIT, inplace=False, optimize=True)
    scale_upem(inter, 1000)
    return inter


def shear_font(font: TTFont, angle_degrees: float) -> None:
    shear = math.tan(math.radians(angle_degrees))
    glyf = font["glyf"]

    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        glyph.expand(glyf)
        if glyph.isComposite():
            for component in glyph.components:
                component.x = otRound(component.x + component.y * shear)
        elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
            for index, (x, y) in enumerate(glyph.coordinates):
                glyph.coordinates[index] = otRound(x + y * shear), y
        glyph.recalcBounds(glyf)

    if "gvar" in font:
        for variations in font["gvar"].variations.values():
            for variation in variations:
                for index, xy in enumerate(variation.coordinates):
                    if xy is None:
                        continue
                    x, y = xy
                    variation.coordinates[index] = otRound(x + y * shear), y


def piecewise_map(value: float, segment: dict[float, float]) -> float:
    items = sorted(segment.items())
    if value <= items[0][0]:
        return items[0][1]
    if value >= items[-1][0]:
        return items[-1][1]
    for (x0, y0), (x1, y1) in zip(items, items[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y0
            return y0 + (value - x0) * (y1 - y0) / (x1 - x0)
    return value


def inverse_piecewise_map(value: float, segment: dict[float, float]) -> float:
    items = sorted(segment.items(), key=lambda item: item[1])
    if value <= items[0][1]:
        return items[0][0]
    if value >= items[-1][1]:
        return items[-1][0]
    for (x0, y0), (x1, y1) in zip(items, items[1:]):
        if y0 <= value <= y1:
            if y1 == y0:
                return x0
            return x0 + (value - y0) * (x1 - x0) / (y1 - y0)
    return value


def remap_inter_gvar_supports(base: TTFont, inter: TTFont) -> None:
    if "gvar" not in inter or "avar" not in inter or "avar" not in base:
        return
    inter_segment = inter["avar"].segments.get("wght")
    base_segment = base["avar"].segments.get("wght")
    if not inter_segment or not base_segment:
        return
    for variations in inter["gvar"].variations.values():
        for variation in variations:
            support = variation.axes.get("wght")
            if not support:
                continue
            variation.axes["wght"] = tuple(
                piecewise_map(inverse_piecewise_map(value, inter_segment), base_segment)
                for value in support
            )


def prefixed(name: str) -> str:
    return INTER_PREFIX + name


def append_inter_glyphs(base: TTFont, inter: TTFont, allowed_unicodes: set[int]) -> dict[str, object]:
    remap_inter_gvar_supports(base, inter)
    source_order = inter.getGlyphOrder()
    source_names = set(source_order)
    existing = set(base.getGlyphOrder())
    rename = {name: prefixed(name) for name in source_order if name != ".notdef"}

    new_order = base.getGlyphOrder()
    for source_name in source_order:
        if source_name == ".notdef":
            continue
        target_name = rename[source_name]
        if target_name in existing:
            continue
        glyph = copy.deepcopy(inter["glyf"][source_name])
        glyph.expand(inter["glyf"])
        if glyph.isComposite():
            for component in glyph.components:
                if component.glyphName in source_names:
                    component.glyphName = rename[component.glyphName]
        base["glyf"].glyphs[target_name] = glyph
        base["hmtx"].metrics[target_name] = copy.deepcopy(
            inter["hmtx"].metrics.get(source_name, (0, 0))
        )
        if "vmtx" in base:
            base["vmtx"].metrics[target_name] = (1000, 0)
        if "gvar" in base and "gvar" in inter:
            base["gvar"].variations[target_name] = copy.deepcopy(
                inter["gvar"].variations.get(source_name, [])
            )
        new_order.append(target_name)

    base.setGlyphOrder(new_order)
    if "maxp" in base:
        base["maxp"].numGlyphs = len(new_order)

    remapped_cmap = 0
    inter_cmap = inter.getBestCmap()
    for cmap_table in base["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint, source_name in inter_cmap.items():
            if codepoint > 0xFFFF and cmap_table.format in {0, 2, 4, 6}:
                continue
            if codepoint in allowed_unicodes and source_name in rename:
                cmap_table.cmap[codepoint] = rename[source_name]
                remapped_cmap += 1

    return {
        "base_subset_glyphs_before_inter": len(new_order) - len(rename),
        "appended_inter_glyphs": len(rename),
        "remapped_inter_cmap_entries": remapped_cmap,
    }


def remove_metric_variation_maps(font: TTFont) -> None:
    # gvar phantom points provide advances for the replaced Inter glyphs. Source Han
    # CJK glyphs are fixed-width, so HVAR/VVAR are not needed and can otherwise carry
    # stale VarIdxMap entries after we append prefixed Inter glyphs.
    for tag in ("HVAR", "VVAR"):
        if tag in font:
            del font[tag]


def add_digit_features(font: TTFont) -> bool:
    glyphs = set(font.getGlyphOrder())
    digit_names = [prefixed(name) for name in DIGITS]
    tabular_names = [prefixed(name) for name in DIGITS_TF]
    if not all(g in glyphs for g in digit_names + tabular_names):
        return False

    if "GSUB" not in font:
        return False

    gsub = font["GSUB"].table
    if gsub.LookupList is None:
        gsub.LookupList = ot.LookupList()
        gsub.LookupList.Lookup = []
        gsub.LookupList.LookupCount = 0
    if gsub.FeatureList is None:
        gsub.FeatureList = ot.FeatureList()
        gsub.FeatureList.FeatureRecord = []
        gsub.FeatureList.FeatureCount = 0

    def append_single_sub_feature(tag: str, mapping: dict[str, str]) -> int:
        subtable = ot.SingleSubst()
        subtable.mapping = mapping

        lookup = ot.Lookup()
        lookup.LookupType = 1
        lookup.LookupFlag = 0
        lookup.SubTable = [subtable]
        lookup.SubTableCount = 1

        lookup_index = len(gsub.LookupList.Lookup)
        gsub.LookupList.Lookup.append(lookup)
        gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)

        feature = ot.Feature()
        feature.FeatureParams = None
        feature.LookupListIndex = [lookup_index]
        feature.LookupCount = 1

        record = ot.FeatureRecord()
        record.FeatureTag = tag
        record.Feature = feature
        feature_index = len(gsub.FeatureList.FeatureRecord)
        gsub.FeatureList.FeatureRecord.append(record)
        gsub.FeatureList.FeatureCount = len(gsub.FeatureList.FeatureRecord)
        return feature_index

    new_feature_indices = [
        append_single_sub_feature("tnum", dict(zip(digit_names, tabular_names))),
        append_single_sub_feature("pnum", dict(zip(tabular_names, digit_names))),
    ]

    if gsub.ScriptList:
        for script_record in gsub.ScriptList.ScriptRecord:
            languages = []
            if script_record.Script.DefaultLangSys:
                languages.append(script_record.Script.DefaultLangSys)
            languages.extend(record.LangSys for record in script_record.Script.LangSysRecord)
            for langsys in languages:
                existing = list(langsys.FeatureIndex or [])
                for feature_index in new_feature_indices:
                    if feature_index not in existing:
                        existing.append(feature_index)
                langsys.FeatureIndex = existing
                langsys.FeatureCount = len(existing)
    return True


def build_one(italic: bool) -> dict[str, object]:
    unicodes = reference_unicodes()
    base = load_base(italic)
    inter = load_inter(italic)
    try:
        merge_report = append_inter_glyphs(base, inter, unicodes)
    finally:
        inter.close()

    remove_metric_variation_maps(base)
    digit_features_added = add_digit_features(base)
    update_names(base, italic)
    update_fvar_instances(base, italic)
    update_style_flags(base, italic)
    rebuild_stat(base, italic)
    if "DSIG" in base:
        del base["DSIG"]

    out_name = (
        "Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf"
        if italic
        else "Sarasa-Ui-VF-PropDigits-SC[wght].ttf"
    )
    out_path = OUT_DIR / out_name
    base.save(out_path, reorderTables=True)

    cmap = base.getBestCmap()
    widths = {chr(cp): base["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A)}
    axes = [(a.axisTag, a.minValue, a.defaultValue, a.maxValue) for a in base["fvar"].axes]
    glyph_count = len(base.getGlyphOrder())
    base.close()

    return {
        "file": str(out_path),
        "axes": axes,
        "glyph_count": glyph_count,
        "default_digit_widths": widths,
        "digit_features_added": digit_features_added,
        **merge_report,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = [build_one(False), build_one(True)]
    report = {
        "family": FAMILY,
        "source_base": str(BASE_VF),
        "source_latin_upright": str(INTER_UPRIGHT),
        "source_latin_italic": str(INTER_ITALIC),
        "method": "Source Han Sans SC VF is subset with Sarasa's original western-drop strategy, then scaled Inter Variable glyphs are appended with prefixed glyph names. This follows Sarasa's glyph-limit approach of dropping Source Han western glyphs and letting the Latin source provide them.",
        "outputs": outputs,
    }
    report_path = OUT_DIR / "Sarasa-Ui-VF-PropDigits-SC-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
