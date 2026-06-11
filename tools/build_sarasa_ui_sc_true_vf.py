from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fontTools import subset
from fontTools.misc.fixedTools import otRound
from fontTools.ttLib import TTFont
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.varLib.instancer import instantiateVariableFont


ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = ROOT.parent


def first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


SRC_DIR = Path(os.environ.get("VF_SOURCE_DIR", first_existing(WORK_ROOT / "vf-sources", ROOT / "work" / "vf-sources")))
BASE_VF = Path(os.environ.get("SOURCE_HAN_SC_VF", SRC_DIR / "SourceHanSansSC-VF.ttf"))
INTER_UPRIGHT = Path(os.environ.get("INTER_VF", SRC_DIR / "InterVariable.ttf"))
INTER_ITALIC = Path(os.environ.get("INTER_ITALIC_VF", SRC_DIR / "InterVariable-Italic.woff2"))
REFERENCE_SARASA = Path(
    os.environ.get(
        "REFERENCE_SARASA",
        first_existing(
            WORK_ROOT / "sarasa-original-unhinted" / "SarasaUiSC-Regular.ttf",
            ROOT / "fonts" / "static" / "SarasaUiProDigitsSC-TTF-1.0.39" / "SarasaUiProDigitsSC-Regular.ttf",
        ),
    )
)

VARIABLE_DIR = ROOT / "fonts" / "variable"
STATIC_DIR = ROOT / "fonts" / "static" / "SarasaUiProDigitsSC-TTF-1.0.39"
REPORT_DIR = ROOT / "reports"
CHECKSUM_DIR = ROOT / "checksums"

AXIS_LIMIT = {"wght": (250, 400, 900)}
INTER_AXIS_LIMIT = {"opsz": 14, "wght": (250, 400, 900)}
VF_FAMILY = "Sarasa Ui VF PropDigits SC"
VF_PS_FAMILY = "Sarasa-Ui-VF-PropDigits-SC"
STATIC_FAMILY = "Sarasa Ui ProDigits SC"
STATIC_PS_FAMILY = "Sarasa-Ui-ProDigits-SC"
VERSION = "1.0.39-propdigits.2"
INTER_PREFIX = "inter."

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
WIDTH_FEATURES = {"aalt", "pwid", "fwid", "hwid", "twid", "qwid"}
FINAL_GSUB_FEATURES = {"locl", "vert", "vrt2", "ljmo", "vjmo", "tjmo", "tnum", "pnum", "calt"}

# Sarasa make/punct/sanitize-symbols.mjs, in Ui/pwid mode.
SANITIZER_TYPES_PWID = {
    0x00B7: "interpunct",
    0x2018: "ident",
    0x2019: "ident",
    0x201C: "ident",
    0x201D: "ident",
    0x2010: "half",
    0x2025: "ellipsis",
    0x2026: "ellipsis",
    0x2E3A: "stretchDual",
    0x2E3B: "stretchTri",
    0x31B4: "half",
    0x31B5: "half",
    0x31B6: "half",
    0x31B7: "half",
    0x31BB: "half",
}


def prefixed(name: str) -> str:
    return INTER_PREFIX + name


def is_ideograph(c: int) -> bool:
    return (
        0x2E80 <= c <= 0x2FFF
        or 0x3192 <= c <= 0x319F
        or 0x31C0 <= c <= 0x31EF
        or 0x3400 <= c <= 0x4DBF
        or 0x4E00 <= c <= 0x9FFF
        or 0xF900 <= c <= 0xFA6F
        or 0x20000 <= c <= 0x3FFFF
    )


def is_western(c: int) -> bool:
    return (c < 0x2000 and c != 0x00B7) or (0x2070 <= c <= 0x218F)


def is_korean(c: int) -> bool:
    return (
        0x1100 <= c <= 0x11FF
        or 0xAC00 <= c <= 0xD7AF
        or 0x3130 <= c <= 0x318F
        or 0x3200 <= c <= 0x321E
        or 0xFFA1 <= c <= 0xFFDC
        or 0x3260 <= c <= 0x327F
        or 0xA960 <= c <= 0xA97F
        or 0xD7B0 <= c <= 0xD7FF
    )


def is_enclosed_alphanumerics(c: int) -> bool:
    return 0x20DD <= c <= 0x20DE or 0x2460 <= c <= 0x24FF or 0x2776 <= c <= 0x2788


def is_pua(c: int) -> bool:
    return 0xE000 <= c <= 0xF8FF


def use_inter_codepoint(c: int) -> bool:
    return is_western(c) and not is_korean(c) and not is_enclosed_alphanumerics(c) and not is_pua(c)


def set_name_record(font: TTFont, name_id: int, value: str) -> None:
    name_table = font["name"]
    records = [n for n in name_table.names if n.nameID == name_id]
    if not records:
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)
        records = [n for n in name_table.names if n.nameID == name_id]
    for record in records:
        record.string = value.encode(record.getEncoding())


def update_vf_names(font: TTFont, italic: bool) -> None:
    subfamily = "Italic" if italic else "Regular"
    full = VF_FAMILY + (" Italic" if italic else "")
    ps = VF_PS_FAMILY + ("-Italic" if italic else "")
    version = f"Version {VERSION}; Source Han Sans SC VF + Inter VF; PropDigits"
    replacements = {
        1: VF_FAMILY,
        2: subfamily,
        3: ps + f";{VERSION}",
        4: full,
        5: version,
        6: ps,
        16: VF_FAMILY,
        17: subfamily,
        25: ps,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)


def legacy_static_family(weight_name: str) -> str:
    if weight_name in {"Regular", "Bold"}:
        return STATIC_FAMILY
    return f"{STATIC_FAMILY} {weight_name}"


def update_static_names(font: TTFont, weight_name: str, weight_value: int, italic: bool) -> None:
    family = legacy_static_family(weight_name)
    if weight_name == "Regular":
        legacy_style = "Italic" if italic else "Regular"
    elif weight_name == "Bold":
        legacy_style = "Bold Italic" if italic else "Bold"
    else:
        legacy_style = "Italic" if italic else "Regular"

    typographic_style = "Italic" if weight_name == "Regular" and italic else weight_name + (" Italic" if italic else "")
    full = STATIC_FAMILY if weight_name == "Regular" and not italic else f"{STATIC_FAMILY} {typographic_style}"
    ps_suffix = "Italic" if weight_name == "Regular" and italic else weight_name + ("-Italic" if italic else "")
    ps = f"{STATIC_PS_FAMILY}-{ps_suffix}"

    replacements = {
        1: family,
        2: legacy_style,
        3: ps + f";{VERSION}",
        4: full,
        5: f"Version {VERSION}; Source Han Sans SC VF + Inter VF; PropDigits",
        6: ps,
        16: STATIC_FAMILY,
        17: typographic_style,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)
    if 25 in {n.nameID for n in font["name"].names}:
        set_name_record(font, 25, ps)

    os2 = font["OS/2"]
    os2.usWeightClass = weight_value
    os2.fsSelection |= 1 << 7
    os2.fsSelection &= ~((1 << 0) | (1 << 5) | (1 << 6))
    if italic:
        os2.fsSelection |= 1 << 0
        font["head"].macStyle |= 0b10
        font["post"].italicAngle = -9.4
    else:
        font["head"].macStyle &= ~0b10
        font["post"].italicAngle = 0
    if weight_value >= 700:
        os2.fsSelection |= 1 << 5
        font["head"].macStyle |= 0b01
    else:
        font["head"].macStyle &= ~0b01
    if weight_name == "Regular" and not italic:
        os2.fsSelection |= 1 << 6


def update_style_flags(font: TTFont, italic: bool) -> None:
    os2 = font["OS/2"]
    os2.usWeightClass = 400
    os2.fsSelection |= 1 << 7
    os2.fsSelection &= ~((1 << 0) | (1 << 5) | (1 << 6))
    font["head"].macStyle &= ~0b11
    if italic:
        font["head"].macStyle |= 0b10
        os2.fsSelection |= 1 << 0
        font["post"].italicAngle = -9.4
    else:
        os2.fsSelection |= 1 << 6
        font["post"].italicAngle = 0


def update_os2_sarasa_metadata(font: TTFont) -> None:
    os2 = font["OS/2"]
    os2.version = max(os2.version, 4)
    os2.achVendID = "????"
    os2.ulCodePageRange1 = 2147746207
    os2.ulCodePageRange2 = 0


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
        {"tag": "wght", "name": "Weight", "values": weight_values},
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
    instances = []
    for stop in SOURCE_HAN_WEIGHT_STOPS:
        weight_name = stop["name"]
        weight_value = stop["value"]
        instance = NamedInstance()
        instance.coordinates = {"wght": float(weight_value)}
        instance.flags = 0
        instance.subfamilyNameID = name_table.addName(weight_name)
        ps_suffix = weight_name + ("Italic" if italic else "")
        instance.postscriptNameID = name_table.addName(f"{VF_PS_FAMILY}-{ps_suffix}")
        instances.append(instance)
    font["fvar"].instances = instances


def reference_unicodes() -> set[int]:
    font = TTFont(REFERENCE_SARASA)
    try:
        return set(font.getBestCmap().keys())
    finally:
        font.close()


def source_han_unicodes_like_sarasa() -> set[int]:
    return {codepoint for codepoint in reference_unicodes() if not use_inter_codepoint(codepoint)}


def ensure_gvar_keys(font: TTFont) -> None:
    if "gvar" not in font:
        return
    variations = font["gvar"].variations
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in variations:
            variations[glyph_name] = []


def subset_font(font: TTFont, unicodes: set[int]) -> None:
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


def subset_to_current_cmap(font: TTFont) -> None:
    subset_font(font, set(font.getBestCmap().keys()))


def get_single_substitution_mapping(font: TTFont, tag: str) -> dict[str, str]:
    if "GSUB" not in font:
        return {}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {}
    feature_records = [r for r in gsub.FeatureList.FeatureRecord if r.FeatureTag == tag]
    mapping: dict[str, str] = {}
    for record in feature_records:
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            if lookup.LookupType != 1:
                continue
            for subtable in lookup.SubTable:
                if hasattr(subtable, "mapping"):
                    mapping.update(subtable.mapping)
    return mapping


def copy_glyph_data(font: TTFont, source_name: str, target_name: str) -> None:
    if source_name == target_name or source_name not in font["glyf"].glyphs or target_name not in font["glyf"].glyphs:
        return
    font["glyf"].glyphs[target_name] = copy.deepcopy(font["glyf"][source_name])
    if source_name in font["hmtx"].metrics:
        font["hmtx"].metrics[target_name] = copy.deepcopy(font["hmtx"].metrics[source_name])
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        font["vmtx"].metrics[target_name] = copy.deepcopy(font["vmtx"].metrics[source_name])
    if "gvar" in font:
        font["gvar"].variations[target_name] = copy.deepcopy(font["gvar"].variations.get(source_name, []))


def bake_single_substitution_feature(
    font: TTFont,
    tag: str,
    codepoint_filter: Any | None = None,
) -> int:
    mapping = get_single_substitution_mapping(font, tag)
    if not mapping:
        return 0
    count = 0
    for codepoint, glyph_name in list(font.getBestCmap().items()):
        if codepoint_filter and not codepoint_filter(codepoint):
            continue
        target_name = mapping.get(glyph_name)
        if target_name and target_name in font.getGlyphSet():
            copy_glyph_data(font, target_name, glyph_name)
            count += 1
    return count


def shift_glyph_x(font: TTFont, glyph_name: str, dx: float) -> None:
    dx = otRound(dx)
    if not dx or glyph_name not in font["glyf"].glyphs:
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if glyph.isComposite():
        for component in glyph.components:
            component.x = otRound(component.x + dx)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for index, (x, y) in enumerate(glyph.coordinates):
            glyph.coordinates[index] = otRound(x + dx), y
    glyph.recalcBounds(glyf)


def shift_glyph_y(font: TTFont, glyph_name: str, dy: float) -> None:
    dy = otRound(dy)
    if not dy or glyph_name not in font["glyf"].glyphs:
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if glyph.isComposite():
        for component in glyph.components:
            component.y = otRound(component.y + dy)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for index, (x, y) in enumerate(glyph.coordinates):
            glyph.coordinates[index] = x, otRound(y + dy)
    glyph.recalcBounds(glyf)


def set_advance_width(font: TTFont, glyph_name: str, width: int) -> None:
    _old_width, lsb = font["hmtx"].metrics.get(glyph_name, (width, 0))
    font["hmtx"].metrics[glyph_name] = (otRound(width), lsb)


def freeze_advance_variation(font: TTFont, glyph_name: str) -> None:
    if "gvar" not in font:
        return
    for variation in font["gvar"].variations.get(glyph_name, []):
        if len(variation.coordinates) < 4:
            continue
        for index in range(len(variation.coordinates) - 4, len(variation.coordinates)):
            if variation.coordinates[index] is not None:
                variation.coordinates[index] = (0, 0)


def center_to_width(font: TTFont, glyph_name: str, width: int) -> None:
    old_width = font["hmtx"].metrics.get(glyph_name, (width, 0))[0]
    shift_glyph_x(font, glyph_name, (width - old_width) / 2)
    set_advance_width(font, glyph_name, width)
    freeze_advance_variation(font, glyph_name)


def stretch_to_width(font: TTFont, glyph_name: str, width: int) -> None:
    old_width = font["hmtx"].metrics.get(glyph_name, (width, 0))[0]
    if old_width == width or glyph_name not in font["glyf"].glyphs:
        set_advance_width(font, glyph_name, width)
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    delta = width - old_width
    if glyph.isComposite():
        for component in glyph.components:
            if component.x * 2 >= old_width:
                component.x = otRound(component.x + delta)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for index, (x, y) in enumerate(glyph.coordinates):
            if x * 2 >= old_width:
                glyph.coordinates[index] = otRound(x + delta), y
    glyph.recalcBounds(glyf)
    set_advance_width(font, glyph_name, width)
    freeze_advance_variation(font, glyph_name)


def bake_source_han_pwid_and_sanitize(font: TTFont) -> dict[str, int]:
    pwid_count = bake_single_substitution_feature(font, "pwid", lambda cp: cp in SANITIZER_TYPES_PWID)
    cmap = font.getBestCmap()
    touched = 0
    for codepoint, sanitizer in SANITIZER_TYPES_PWID.items():
        glyph_name = cmap.get(codepoint)
        if not glyph_name:
            continue
        if sanitizer in {"ident", "ellipsis"}:
            pass
        elif sanitizer in {"interpunct", "half"}:
            center_to_width(font, glyph_name, font["head"].unitsPerEm // 2)
        elif sanitizer == "stretchDual":
            stretch_to_width(font, glyph_name, font["head"].unitsPerEm * 2)
        elif sanitizer == "stretchTri":
            stretch_to_width(font, glyph_name, font["head"].unitsPerEm * 3)
        touched += 1
    return {"source_han_pwid_baked": pwid_count, "source_han_symbols_sanitized": touched}


def normalize_hangul_widths(font: TTFont) -> int:
    cmap = font.getBestCmap()
    touched: set[str] = set()
    em = font["head"].unitsPerEm
    for codepoint, glyph_name in cmap.items():
        if not is_korean(codepoint) or glyph_name in touched:
            continue
        old_width = font["hmtx"].metrics.get(glyph_name, (em, 0))[0]
        target_width = max(em, math.ceil(old_width / em) * em) if old_width > 0 else em
        shift_glyph_x(font, glyph_name, (target_width - old_width) / 2)
        set_advance_width(font, glyph_name, target_width)
        freeze_advance_variation(font, glyph_name)
        touched.add(glyph_name)
    return len(touched)


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


def load_base(italic: bool) -> tuple[TTFont, dict[str, int]]:
    base = TTFont(BASE_VF)
    base = instantiateVariableFont(base, AXIS_LIMIT, inplace=False, optimize=True)
    subset_font(base, source_han_unicodes_like_sarasa())
    sarasa_report = bake_source_han_pwid_and_sanitize(base)
    sarasa_report["hangul_widths_normalized"] = normalize_hangul_widths(base)
    if italic:
        shear_font(base, 9.4)
    return base, sarasa_report


def load_inter(italic: bool) -> TTFont:
    inter = TTFont(INTER_ITALIC if italic else INTER_UPRIGHT)
    inter = instantiateVariableFont(inter, INTER_AXIS_LIMIT, inplace=False, optimize=True)
    scale_upem(inter, 1000)
    bake_single_substitution_feature(inter, "ss03")
    bake_single_substitution_feature(inter, "cv10")
    return inter


def append_inter_glyphs(base: TTFont, inter: TTFont, allowed_unicodes: set[int]) -> dict[str, Any]:
    remap_inter_gvar_supports(base, inter)
    source_order = inter.getGlyphOrder()
    source_names = set(source_order)
    existing = set(base.getGlyphOrder())
    rename = {name: prefixed(name) for name in source_order if name != ".notdef"}

    base_order_before = len(base.getGlyphOrder())
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
        base["hmtx"].metrics[target_name] = copy.deepcopy(inter["hmtx"].metrics.get(source_name, (0, 0)))
        if "vmtx" in base:
            base["vmtx"].metrics[target_name] = (1000, 0)
        if "gvar" in base and "gvar" in inter:
            base["gvar"].variations[target_name] = copy.deepcopy(inter["gvar"].variations.get(source_name, []))
        new_order.append(target_name)

    base.setGlyphOrder(new_order)
    if "maxp" in base:
        base["maxp"].numGlyphs = len(new_order)

    remapped_cmap = 0
    allowed_inter_unicodes = {cp for cp in allowed_unicodes if use_inter_codepoint(cp)}
    inter_cmap = inter.getBestCmap()
    for cmap_table in base["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint, source_name in inter_cmap.items():
            if codepoint > 0xFFFF and cmap_table.format in {0, 2, 4, 6}:
                continue
            if codepoint in allowed_inter_unicodes and source_name in rename:
                cmap_table.cmap[codepoint] = rename[source_name]
                remapped_cmap += 1

    return {
        "base_subset_glyphs_before_inter": base_order_before,
        "appended_inter_glyphs": len(rename),
        "remapped_inter_cmap_entries": remapped_cmap,
    }


def remove_metric_variation_maps(font: TTFont) -> None:
    for tag in ("HVAR", "VVAR"):
        if tag in font:
            del font[tag]


def drop_feature_records(table: Any, tags: set[str]) -> int:
    if not table or not table.table or not table.table.FeatureList:
        return 0
    root = table.table
    old_records = root.FeatureList.FeatureRecord
    keep_records = [record for record in old_records if record.FeatureTag not in tags]
    if len(keep_records) == len(old_records):
        return 0
    remap: dict[int, int] = {}
    next_index = 0
    for old_index, record in enumerate(old_records):
        if record.FeatureTag not in tags:
            remap[old_index] = next_index
            next_index += 1
    root.FeatureList.FeatureRecord = keep_records
    root.FeatureList.FeatureCount = len(keep_records)
    if root.ScriptList:
        for script_record in root.ScriptList.ScriptRecord:
            langsys_list = []
            if script_record.Script.DefaultLangSys:
                langsys_list.append(script_record.Script.DefaultLangSys)
            langsys_list.extend(record.LangSys for record in script_record.Script.LangSysRecord)
            for langsys in langsys_list:
                old_indices = list(langsys.FeatureIndex or [])
                langsys.FeatureIndex = [remap[i] for i in old_indices if i in remap]
                langsys.FeatureCount = len(langsys.FeatureIndex)
    return len(old_records) - len(keep_records)


def drop_sarasa_width_features(font: TTFont) -> dict[str, int]:
    return {
        "gsub_width_features_dropped": drop_feature_records(font["GSUB"], WIDTH_FEATURES) if "GSUB" in font else 0,
        "gpos_width_features_dropped": drop_feature_records(font["GPOS"], WIDTH_FEATURES) if "GPOS" in font else 0,
    }


def drop_nonfinal_gsub_features(font: TTFont) -> int:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return 0
    tags = {record.FeatureTag for record in font["GSUB"].table.FeatureList.FeatureRecord}
    return drop_feature_records(font["GSUB"], tags - FINAL_GSUB_FEATURES)


def append_single_sub_feature(font: TTFont, tag: str, mapping: dict[str, str]) -> bool:
    mapping = {src: dst for src, dst in mapping.items() if src in font.getGlyphSet() and dst in font.getGlyphSet()}
    if not mapping or "GSUB" not in font:
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
    gsub.FeatureList.FeatureRecord.append(record)
    gsub.FeatureList.FeatureCount = len(gsub.FeatureList.FeatureRecord)
    return True


def glyph_order_sorted(font: TTFont, glyphs: list[str]) -> list[str]:
    order = {glyph_name: index for index, glyph_name in enumerate(font.getGlyphOrder())}
    return sorted(glyphs, key=lambda glyph_name: order.get(glyph_name, 10**9))


def coverage(font: TTFont, glyphs: list[str]) -> ot.Coverage:
    cov = ot.Coverage()
    cov.glyphs = glyph_order_sorted(font, glyphs)
    return cov


def append_gsub_lookup(font: TTFont, lookup: ot.Lookup) -> int:
    gsub = font["GSUB"].table
    if gsub.LookupList is None:
        gsub.LookupList = ot.LookupList()
        gsub.LookupList.Lookup = []
        gsub.LookupList.LookupCount = 0
    lookup_index = len(gsub.LookupList.Lookup)
    gsub.LookupList.Lookup.append(lookup)
    gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)
    return lookup_index


def append_gsub_feature(font: TTFont, tag: str, lookup_indices: list[int]) -> int:
    gsub = font["GSUB"].table
    if gsub.FeatureList is None:
        gsub.FeatureList = ot.FeatureList()
        gsub.FeatureList.FeatureRecord = []
        gsub.FeatureList.FeatureCount = 0
    feature = ot.Feature()
    feature.FeatureParams = None
    feature.LookupListIndex = lookup_indices
    feature.LookupCount = len(lookup_indices)
    record = ot.FeatureRecord()
    record.FeatureTag = tag
    record.Feature = feature
    feature_index = len(gsub.FeatureList.FeatureRecord)
    gsub.FeatureList.FeatureRecord.append(record)
    gsub.FeatureList.FeatureCount = len(gsub.FeatureList.FeatureRecord)
    return feature_index


def enable_features_for_all_scripts(font: TTFont, tags: set[str]) -> None:
    if "GSUB" not in font:
        return
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.ScriptList:
        return
    indices = [i for i, record in enumerate(gsub.FeatureList.FeatureRecord) if record.FeatureTag in tags]
    if not indices:
        return
    for script_record in gsub.ScriptList.ScriptRecord:
        langsys_list = []
        if script_record.Script.DefaultLangSys:
            langsys_list.append(script_record.Script.DefaultLangSys)
        langsys_list.extend(record.LangSys for record in script_record.Script.LangSysRecord)
        for langsys in langsys_list:
            feature_indices = list(langsys.FeatureIndex or [])
            for index in indices:
                if index not in feature_indices:
                    feature_indices.append(index)
            langsys.FeatureIndex = feature_indices
            langsys.FeatureCount = len(feature_indices)


def collect_prefixed_inter_feature_mapping(inter: TTFont, tag: str) -> dict[str, str]:
    return {prefixed(src): prefixed(dst) for src, dst in get_single_substitution_mapping(inter, tag).items()}


def add_digit_width_features(font: TTFont, inter: TTFont) -> dict[str, Any]:
    tnum = collect_prefixed_inter_feature_mapping(inter, "tnum")
    pnum = collect_prefixed_inter_feature_mapping(inter, "pnum")
    if not pnum:
        pnum = {dst: src for src, dst in tnum.items()}
    added_tnum = append_single_sub_feature(font, "tnum", tnum)
    added_pnum = append_single_sub_feature(font, "pnum", pnum)
    enable_features_for_all_scripts(font, {"tnum", "pnum"})
    return {
        "tnum_feature_added": added_tnum,
        "pnum_feature_added": added_pnum,
        "tnum_mappings": len(tnum),
        "pnum_mappings": len(pnum),
    }


def glyph_bbox(font: TTFont, glyph_name: str) -> tuple[int, int, int, int] | None:
    if glyph_name not in font["glyf"].glyphs:
        return None
    glyph = font["glyf"][glyph_name]
    glyph.recalcBounds(font["glyf"])
    if not hasattr(glyph, "xMin"):
        return None
    return glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax


def add_digit_colon_feature(font: TTFont) -> dict[str, Any]:
    glyphs = font.getGlyphSet()
    colon = prefixed("colon")
    raised = prefixed("colon.digitsep")
    digit_names = [prefixed(name) for name in DIGITS]
    tabular_names = [prefixed(name) for name in DIGITS_TF]
    if colon not in glyphs or not all(name in glyphs for name in digit_names + tabular_names):
        return {"digit_colon_feature_added": False, "digit_colon_raise": 0}

    order = font.getGlyphOrder()
    if raised not in order:
        font["glyf"].glyphs[raised] = copy.deepcopy(font["glyf"][colon])
        font["hmtx"].metrics[raised] = copy.deepcopy(font["hmtx"].metrics[colon])
        if "vmtx" in font and colon in font["vmtx"].metrics:
            font["vmtx"].metrics[raised] = copy.deepcopy(font["vmtx"].metrics[colon])
        if "gvar" in font:
            font["gvar"].variations[raised] = copy.deepcopy(font["gvar"].variations.get(colon, []))
        order.append(raised)
        font.setGlyphOrder(order)

    digit_boxes = [glyph_bbox(font, name) for name in digit_names if glyph_bbox(font, name)]
    colon_box = glyph_bbox(font, colon)
    if digit_boxes and colon_box:
        digit_y_min = min(box[1] for box in digit_boxes if box)
        digit_y_max = max(box[3] for box in digit_boxes if box)
        digit_center = (digit_y_min + digit_y_max) / 2
        colon_center = (colon_box[1] + colon_box[3]) / 2
        raise_amount = otRound(digit_center - colon_center)
    else:
        raise_amount = 105
    shift_glyph_y(font, raised, raise_amount)

    single_sub = ot.SingleSubst()
    single_sub.mapping = {colon: raised}
    single_lookup = ot.Lookup()
    single_lookup.LookupType = 1
    single_lookup.LookupFlag = 0
    single_lookup.SubTable = [single_sub]
    single_lookup.SubTableCount = 1
    single_index = append_gsub_lookup(font, single_lookup)

    chain = ot.ChainContextSubst()
    chain.Format = 3
    chain.BacktrackGlyphCount = 1
    chain.BacktrackCoverage = [coverage(font, digit_names + tabular_names)]
    chain.InputGlyphCount = 1
    chain.InputCoverage = [coverage(font, [colon])]
    chain.LookAheadGlyphCount = 1
    chain.LookAheadCoverage = [coverage(font, digit_names + tabular_names)]
    subst_record = ot.SubstLookupRecord()
    subst_record.SequenceIndex = 0
    subst_record.LookupListIndex = single_index
    chain.SubstCount = 1
    chain.SubstLookupRecord = [subst_record]

    chain_lookup = ot.Lookup()
    chain_lookup.LookupType = 6
    chain_lookup.LookupFlag = 0
    chain_lookup.SubTable = [chain]
    chain_lookup.SubTableCount = 1
    chain_index = append_gsub_lookup(font, chain_lookup)
    append_gsub_feature(font, "calt", [chain_index])
    enable_features_for_all_scripts(font, {"calt"})
    return {"digit_colon_feature_added": True, "digit_colon_raise": raise_amount}


def build_one_variable(italic: bool) -> dict[str, Any]:
    unicodes = reference_unicodes()
    base, sarasa_report = load_base(italic)
    inter = load_inter(italic)
    try:
        merge_report = append_inter_glyphs(base, inter, unicodes)
        digit_report = add_digit_width_features(base, inter)
    finally:
        inter.close()

    remove_metric_variation_maps(base)
    feature_drop_report = drop_sarasa_width_features(base)
    nonfinal_features_dropped = drop_nonfinal_gsub_features(base)
    subset_to_current_cmap(base)
    colon_report = add_digit_colon_feature(base)
    subset_to_current_cmap(base)
    update_vf_names(base, italic)
    update_fvar_instances(base, italic)
    update_style_flags(base, italic)
    update_os2_sarasa_metadata(base)
    rebuild_stat(base, italic)
    if "DSIG" in base:
        del base["DSIG"]

    VARIABLE_DIR.mkdir(parents=True, exist_ok=True)
    out_name = "Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf" if italic else "Sarasa-Ui-VF-PropDigits-SC[wght].ttf"
    out_path = VARIABLE_DIR / out_name
    base.save(out_path, reorderTables=True)

    cmap = base.getBestCmap()
    widths = {f"U+{cp:04X}": base["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A)}
    key_widths = {
        f"U+{cp:04X}": base["hmtx"].metrics[cmap[cp]][0]
        for cp in [0x00B7, 0x2018, 0x2019, 0x201C, 0x201D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2025, 0x2026, 0x2E3A, 0x2E3B, 0x31B4, 0x3131, 0xAC00, 0x1100]
        if cp in cmap
    }
    axes = [(a.axisTag, a.minValue, a.defaultValue, a.maxValue) for a in base["fvar"].axes]
    instances = [base["name"].getDebugName(i.subfamilyNameID) for i in base["fvar"].instances]
    glyph_count = len(base.getGlyphOrder())
    base.close()

    return {
        "file": str(out_path.relative_to(ROOT)),
        "axes": axes,
        "instances": instances,
        "glyph_count": glyph_count,
        "default_digit_widths": widths,
        "key_symbol_widths": key_widths,
        **sarasa_report,
        **merge_report,
        **digit_report,
        **feature_drop_report,
        "nonfinal_gsub_features_dropped": nonfinal_features_dropped,
        **colon_report,
    }


def remove_variable_tables(font: TTFont) -> None:
    for tag in ("fvar", "gvar", "avar", "HVAR", "VVAR", "MVAR"):
        if tag in font:
            del font[tag]


def hint_static_font(in_path: Path, out_path: Path) -> dict[str, Any]:
    if os.environ.get("SARASA_SKIP_TTFAUTOHINT") == "1":
        shutil.copy2(in_path, out_path)
        return {"hinted": False, "hint_tool": "skipped"}
    exe = os.environ.get("TTFAUTOHINT")
    if exe:
        result = subprocess.run([exe, "--no-info", str(in_path), str(out_path)], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return {"hinted": True, "hint_tool": exe}
    try:
        import ttfautohint

        result = ttfautohint.run(["--no-info", str(in_path), str(out_path)], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return {"hinted": True, "hint_tool": "ttfautohint-py"}
    except ImportError:
        shutil.copy2(in_path, out_path)
        return {"hinted": False, "hint_tool": "missing"}


def static_output_name(weight_name: str, italic: bool) -> str:
    if weight_name == "Regular":
        return "SarasaUiProDigitsSC-Italic.ttf" if italic else "SarasaUiProDigitsSC-Regular.ttf"
    return f"SarasaUiProDigitsSC-{weight_name}{'Italic' if italic else ''}.ttf"


def build_static_fonts() -> list[dict[str, Any]]:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for path in STATIC_DIR.glob("SarasaUiProDigitsSC-*.ttf"):
        path.unlink()

    outputs: list[dict[str, Any]] = []
    sources = [
        (False, VARIABLE_DIR / "Sarasa-Ui-VF-PropDigits-SC[wght].ttf"),
        (True, VARIABLE_DIR / "Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf"),
    ]
    with tempfile.TemporaryDirectory() as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        for italic, vf_path in sources:
            for stop in SOURCE_HAN_WEIGHT_STOPS:
                weight_name = stop["name"]
                weight_value = int(stop["value"])
                font = TTFont(vf_path)
                font = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
                remove_variable_tables(font)
                update_static_names(font, weight_name, weight_value, italic)
                update_os2_sarasa_metadata(font)
                if "DSIG" in font:
                    del font["DSIG"]
                tmp_path = tmp_dir / static_output_name(weight_name, italic)
                final_path = STATIC_DIR / static_output_name(weight_name, italic)
                font.save(tmp_path, reorderTables=True)
                font.close()
                hint_report = hint_static_font(tmp_path, final_path)
                outputs.append(
                    {
                        "file": str(final_path.relative_to(ROOT)),
                        "weight": weight_name,
                        "wght": weight_value,
                        "italic": italic,
                        **hint_report,
                    }
                )
    return outputs


def font_name(font: TTFont, name_id: int) -> str | None:
    name = font["name"].getName(name_id, 3, 1, 0x409) or font["name"].getName(name_id, 1, 0, 0)
    return name.toUnicode() if name else None


def has_feature(font: TTFont, tag: str) -> bool:
    return "GSUB" in font and font["GSUB"].table.FeatureList and any(
        record.FeatureTag == tag for record in font["GSUB"].table.FeatureList.FeatureRecord
    )


def inspect_font(path: Path) -> dict[str, Any]:
    font = TTFont(path)
    try:
        cmap = font.getBestCmap()
        digits = [font["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A) if cp in cmap]
        key_cps = [0x00B7, 0x2018, 0x2019, 0x201C, 0x201D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2025, 0x2026, 0x2E3A, 0x2E3B, 0x31B4, 0x3131, 0xAC00, 0x1100]
        key_widths = {f"U+{cp:04X}": font["hmtx"].metrics[cmap[cp]][0] for cp in key_cps if cp in cmap}
        axes = []
        instances = []
        if "fvar" in font:
            axes = [
                {"tag": axis.axisTag, "min": axis.minValue, "default": axis.defaultValue, "max": axis.maxValue}
                for axis in font["fvar"].axes
            ]
            instances = [
                {
                    "name": font["name"].getDebugName(instance.subfamilyNameID),
                    "coordinates": instance.coordinates,
                    "postscript": font["name"].getDebugName(instance.postscriptNameID)
                    if instance.postscriptNameID != 0xFFFF
                    else None,
                }
                for instance in font["fvar"].instances
            ]
        return {
            "file": str(path.relative_to(ROOT)),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
            "names": {
                "family": font_name(font, 1),
                "subfamily": font_name(font, 2),
                "full": font_name(font, 4),
                "postscript": font_name(font, 6),
                "typographic_family": font_name(font, 16),
                "typographic_subfamily": font_name(font, 17),
            },
            "glyph_count": len(font.getGlyphOrder()),
            "digit_widths_u0030_to_u0039": digits,
            "key_symbol_widths": key_widths,
            "has_tnum": has_feature(font, "tnum"),
            "has_pnum": has_feature(font, "pnum"),
            "has_digit_colon_calt": has_feature(font, "calt"),
            "has_hints": any(tag in font for tag in ("fpgm", "prep", "cvt ")),
            "fvar_axes": axes,
            "fvar_instances": instances,
            "fsSelection": font["OS/2"].fsSelection,
            "vendor": font["OS/2"].achVendID,
            "codepage_range_1": font["OS/2"].ulCodePageRange1,
            "codepage_range_2": font["OS/2"].ulCodePageRange2,
        }
    finally:
        font.close()


def write_static_readme() -> None:
    text = """Sarasa Ui ProDigits SC TTF 1.0.39

This directory contains static TrueType instances generated from the corrected
Sarasa Ui VF PropDigits SC build.

Weights:

- ExtraLight 250
- Light 300
- Normal 350
- Regular 400
- Medium 500
- Bold 700
- Heavy 900

Each weight has an upright and Italic file. ASCII digits are proportional by
default; OpenType tnum restores tabular digits, and pnum maps tabular digits
back to proportional digits. The contextual digit-colon rule raises ':' only
when it appears between digits.

Static instances are passed through ttfautohint when the tool is available.
These fonts are modified derivatives and are not official Sarasa Gothic,
Source Han Sans, or Inter releases.
"""
    (STATIC_DIR / "README.txt").write_text(text, encoding="utf-8")


def write_reports(build_report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    build_text = json.dumps(build_report, ensure_ascii=False, indent=2)
    (REPORT_DIR / "Sarasa-Ui-VF-PropDigits-SC-report.json").write_text(build_text, encoding="utf-8")

    font_paths = sorted(VARIABLE_DIR.glob("*.ttf")) + sorted(STATIC_DIR.glob("SarasaUiProDigitsSC-*.ttf"))
    inspection = {
        "title": "Sarasa Ui VF PropDigits SC / Sarasa Ui ProDigits SC font inspection",
        "note": "Generated by tools/build_sarasa_ui_sc_true_vf.py using fontTools.",
        "fonts": [inspect_font(path) for path in font_paths],
    }
    (REPORT_DIR / "font-inspection.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")


def write_checksums() -> None:
    CHECKSUM_DIR.mkdir(parents=True, exist_ok=True)
    include_roots = [ROOT / "README.md", ROOT / "NOTICE.md", ROOT / "LICENSE", VARIABLE_DIR, STATIC_DIR, REPORT_DIR]
    paths: list[Path] = []
    for entry in include_roots:
        if entry.is_file():
            paths.append(entry)
        elif entry.is_dir():
            paths.extend(path for path in entry.rglob("*") if path.is_file())
    lines = []
    for path in sorted(paths, key=lambda p: str(p.relative_to(ROOT)).replace("\\", "/")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        lines.append(f"{digest}  {rel}")
    (CHECKSUM_DIR / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_all() -> dict[str, Any]:
    for path in [BASE_VF, INTER_UPRIGHT, INTER_ITALIC, REFERENCE_SARASA]:
        if not path.exists():
            raise FileNotFoundError(path)
    variable_outputs = [build_one_variable(False), build_one_variable(True)]
    static_outputs = build_static_fonts()
    write_static_readme()
    report = {
        "family": VF_FAMILY,
        "version": VERSION,
        "source_base": str(BASE_VF),
        "source_latin_upright": str(INTER_UPRIGHT),
        "source_latin_italic": str(INTER_ITALIC),
        "reference_unicode_set": str(REFERENCE_SARASA),
        "method": (
            "Source Han Sans SC VF is kept for Sarasa Ui punctuation, symbols, CJK, "
            "and Korean codepoints; Inter VF is baked with Sarasa's Inter settings "
            "(ss03 and cv10) and used only for Sarasa's Latin-owned codepoints. "
            "Source Han pwid/symbol sanitization and Hangul full-width normalization "
            "are applied before Inter glyphs are appended. The final GSUB set keeps "
            "locl, Hangul Jamo features, vert/vrt2, tnum/pnum, and the digit-colon "
            "calt rule; Source Han's original ccmp is omitted to stay below the "
            "TrueType glyph limit."
        ),
        "intentional_differences_from_upstream_sarasa_ui": [
            "Default ASCII digits are proportional; tnum restores tabular digits.",
            "Weight instances follow Source Han Sans stops: 250, 300, 350, 400, 500, 700, 900.",
            "A contextual calt rule raises colon only between digits.",
        ],
        "variable_outputs": variable_outputs,
        "static_outputs": static_outputs,
    }
    write_reports(report)
    write_checksums()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checksums-only", action="store_true")
    args = parser.parse_args()
    if args.checksums_only:
        write_checksums()
        return
    report = build_all()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
