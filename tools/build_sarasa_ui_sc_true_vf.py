from __future__ import annotations

import argparse
import copy
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
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.varLib.models import piecewiseLinearMap
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
            WORK_ROOT / "official-sarasa-ui-sc" / "SarasaUiSC-TTF-Unhinted-1.0.39" / "SarasaUiSC-Regular.ttf",
            WORK_ROOT / "sarasa-original-unhinted" / "SarasaUiSC-Regular.ttf",
            ROOT / "fonts" / "static" / "SarasaUiPropDigitsSC-TTF-1.0.39" / "SarasaUiPropDigitsSC-Regular.ttf",
        ),
    )
)
REFERENCE_SARASA_DIR = REFERENCE_SARASA.parent

VARIABLE_DIR = ROOT / "fonts" / "variable"
STATIC_DIR = ROOT / "fonts" / "static" / "SarasaUiPropDigitsSC-TTF-1.0.39"
REPORT_DIR = ROOT / "reports"

AXIS_LIMIT = {"wght": (250, 400, 900)}
INTER_AXIS_LIMIT = {"opsz": 14, "wght": (250, 400, 900)}
VF_FAMILY = "Sarasa Ui VF PropDigits SC"
VF_PS_FAMILY = "Sarasa-Ui-VF-PropDigits-SC"
STATIC_FAMILY = "Sarasa Ui PropDigits SC"
STATIC_PS_FAMILY = "Sarasa-Ui-PropDigits-SC"
VERSION = "1.0.39-propdigits.3"
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
SOURCE_HAN_FINAL_GSUB_FEATURES = {"locl", "ccmp", "vert", "vrt2", "ljmo", "vjmo", "tjmo", "calt", "hist"}
UPRIGHT_EMPTY_GSUB_FEATURES = {f"cv{i:02d}" for i in range(1, 14)} | {f"ss{i:02d}" for i in range(1, 9)}
ITALIC_EMPTY_GSUB_FEATURES = UPRIGHT_EMPTY_GSUB_FEATURES - {"cv11"}
INTER_GSUB_FEATURES = {
    "aalt",
    "calt",
    "case",
    "ccmp",
    "dlig",
    "dnom",
    "frac",
    "hist",
    "locl",
    "numr",
    "ordn",
    "pnum",
    "salt",
    "sinf",
    "subs",
    "sups",
    "tnum",
    "zero",
    "cv14",
}
FINAL_GSUB_FEATURES = SOURCE_HAN_FINAL_GSUB_FEATURES | INTER_GSUB_FEATURES | UPRIGHT_EMPTY_GSUB_FEATURES | {"pnum", "tnum"}
INTER_GPOS_FEATURES = {"cpsp", "kern", "mark", "mkmk"}
SOURCE_HAN_FORCED_CODEPOINTS = {0x22EF}
REFERENCE_ADVANCE_STOPS = [
    ("ExtraLight", 250),
    ("Light", 300),
    ("Regular", 400),
    ("SemiBold", 600),
    ("Bold", 700),
]
SARASA_VERTICAL_METRICS = {
    "hhea_ascent": 969,
    "hhea_descent": -241,
    "hhea_line_gap": 0,
    "typo_ascent": 968,
    "typo_descent": -241,
    "typo_line_gap": 0,
    "win_ascent": 968,
    "win_descent": 241,
}

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


def empty_gsub_features_for_style(italic: bool) -> set[str]:
    return ITALIC_EMPTY_GSUB_FEATURES if italic else UPRIGHT_EMPTY_GSUB_FEATURES


def reference_style_name(weight_name: str, italic: bool) -> str:
    if italic:
        return "Italic" if weight_name == "Regular" else f"{weight_name}Italic"
    return weight_name


def reference_font_path(weight_name: str, italic: bool) -> Path:
    return REFERENCE_SARASA_DIR / f"SarasaUiSC-{reference_style_name(weight_name, italic)}.ttf"


def open_reference_font(weight_name: str, italic: bool) -> TTFont:
    path = reference_font_path(weight_name, italic)
    if not path.exists():
        raise FileNotFoundError(path)
    return TTFont(path)


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


def is_fe_misc(c: int) -> bool:
    return (
        0x3003 <= c <= 0x3007
        or 0x3012 <= c <= 0x3013
        or 0x3020 <= c <= 0x33FF
        or 0x1AFF0 <= c <= 0x1B12F
        or 0x1F000 <= c <= 0x1F2FF
    )


def is_locale_dependent_fwid_punct(c: int) -> bool:
    return c in {0xFF01, 0xFF08, 0xFF09, 0xFF0C, 0xFF0E, 0xFF1A, 0xFF1B, 0xFF3B, 0xFF3D, 0xFF5B, 0xFF5D, 0xFF1F}


def is_ws(c: int) -> bool:
    return (
        (
            ((0x2000 <= c <= 0x200F) or (0x20A0 <= c < 0x3000))
            and not (0x2E3A <= c <= 0x2E3B)
        )
        or (0xFF01 <= c <= 0xFF5E and not is_locale_dependent_fwid_punct(c))
    )


def source_han_overrides_inter(c: int) -> bool:
    return (
        c in SOURCE_HAN_FORCED_CODEPOINTS
        or is_ideograph(c)
        or is_korean(c)
        or is_enclosed_alphanumerics(c)
        or is_pua(c)
        or (not is_western(c) and not is_ws(c) and not is_fe_misc(c))
    )


def use_inter_codepoint(c: int) -> bool:
    return not source_han_overrides_inter(c)


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
    apply_sarasa_vertical_metrics(font)


def apply_sarasa_vertical_metrics(font: TTFont) -> None:
    hhea = font["hhea"]
    os2 = font["OS/2"]
    hhea.ascent = SARASA_VERTICAL_METRICS["hhea_ascent"]
    hhea.descent = SARASA_VERTICAL_METRICS["hhea_descent"]
    hhea.lineGap = SARASA_VERTICAL_METRICS["hhea_line_gap"]
    os2.sTypoAscender = SARASA_VERTICAL_METRICS["typo_ascent"]
    os2.sTypoDescender = SARASA_VERTICAL_METRICS["typo_descent"]
    os2.sTypoLineGap = SARASA_VERTICAL_METRICS["typo_line_gap"]
    os2.usWinAscent = SARASA_VERTICAL_METRICS["win_ascent"]
    os2.usWinDescent = SARASA_VERTICAL_METRICS["win_descent"]


def sync_sarasa_metadata_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    os2 = font["OS/2"]
    ref_os2 = reference["OS/2"]
    for field in [
        "version",
        "xAvgCharWidth",
        "usWidthClass",
        "fsType",
        "ySubscriptXSize",
        "ySubscriptYSize",
        "ySubscriptXOffset",
        "ySubscriptYOffset",
        "ySuperscriptXSize",
        "ySuperscriptYSize",
        "ySuperscriptXOffset",
        "ySuperscriptYOffset",
        "yStrikeoutSize",
        "yStrikeoutPosition",
        "sFamilyClass",
        "ulUnicodeRange1",
        "ulUnicodeRange2",
        "ulUnicodeRange3",
        "ulUnicodeRange4",
        "achVendID",
        "usFirstCharIndex",
        "usLastCharIndex",
        "ulCodePageRange1",
        "ulCodePageRange2",
        "sxHeight",
        "sCapHeight",
        "usDefaultChar",
        "usBreakChar",
        "usMaxContext",
    ]:
        if hasattr(os2, field) and hasattr(ref_os2, field):
            setattr(os2, field, copy.deepcopy(getattr(ref_os2, field)))
    os2.panose = copy.deepcopy(ref_os2.panose)
    apply_sarasa_vertical_metrics(font)

    head = font["head"]
    ref_head = reference["head"]
    for field in ["fontRevision", "flags", "lowestRecPPEM", "fontDirectionHint", "glyphDataFormat"]:
        if hasattr(head, field) and hasattr(ref_head, field):
            setattr(head, field, copy.deepcopy(getattr(ref_head, field)))

    if "vhea" in font and "vhea" in reference:
        vhea = font["vhea"]
        ref_vhea = reference["vhea"]
        for field in [
            "tableVersion",
            "ascent",
            "descent",
            "lineGap",
            "advanceHeightMax",
            "minTopSideBearing",
            "minBottomSideBearing",
            "yMaxExtent",
            "caretSlopeRise",
            "caretSlopeRun",
            "caretOffset",
            "reserved1",
            "reserved2",
            "reserved3",
            "reserved4",
            "metricDataFormat",
        ]:
            if hasattr(vhea, field) and hasattr(ref_vhea, field):
                setattr(vhea, field, copy.deepcopy(getattr(ref_vhea, field)))
    return {"sarasa_metadata_fields_synced": 1}


def rebuild_gdef_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    reference_gdef = reference.get("GDEF")
    if not reference_gdef or not getattr(reference_gdef.table, "GlyphClassDef", None):
        return {"gdef_classdefs": 0}
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    reference_classes = reference_gdef.table.GlyphClassDef.classDefs
    class_defs: dict[str, int] = {}
    for codepoint, glyph_name in current_cmap.items():
        ref_glyph = reference_cmap.get(codepoint)
        glyph_class = reference_classes.get(ref_glyph) if ref_glyph else None
        if glyph_class is not None:
            class_defs[glyph_name] = glyph_class
    for glyph_name in font.getGlyphOrder():
        if glyph_name in class_defs or glyph_name == ".notdef":
            continue
        if glyph_name in reference_classes:
            class_defs[glyph_name] = reference_classes[glyph_name]

    gdef = newTable("GDEF")
    gdef.table = ot.GDEF()
    gdef.table.Version = 0x00010000
    gdef.table.GlyphClassDef = ot.GlyphClassDef()
    gdef.table.GlyphClassDef.classDefs = class_defs
    gdef.table.AttachList = None
    gdef.table.LigCaretList = None
    gdef.table.MarkAttachClassDef = None
    font["GDEF"] = gdef
    return {"gdef_classdefs": len(class_defs)}


def rebuild_vorg_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    if "VORG" not in reference:
        return {"vorg_records": 0}
    reference_vorg = reference["VORG"]
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    reference_vertical = get_single_substitution_mappings(reference, {"vert", "vrt2"})
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    records: dict[str, int] = {}
    for codepoint, glyph_name in current_cmap.items():
        ref_glyph = reference_cmap.get(codepoint)
        if ref_glyph in reference_vorg.VOriginRecords:
            records[glyph_name] = reference_vorg.VOriginRecords[ref_glyph]
        ref_vertical_glyph = reference_vertical.get(ref_glyph) if ref_glyph else None
        current_vertical_glyph = current_vertical.get(glyph_name)
        if (
            ref_vertical_glyph in reference_vorg.VOriginRecords
            and current_vertical_glyph in font.getGlyphOrder()
        ):
            records[current_vertical_glyph] = reference_vorg.VOriginRecords[ref_vertical_glyph]
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in records and glyph_name in reference_vorg.VOriginRecords:
            records[glyph_name] = reference_vorg.VOriginRecords[glyph_name]
    vorg = newTable("VORG")
    vorg.majorVersion = 1
    vorg.minorVersion = 0
    vorg.defaultVertOriginY = reference_vorg.defaultVertOriginY
    vorg.VOriginRecords = records
    font["VORG"] = vorg
    return {"vorg_records": len(records)}


def align_reference_vmtx(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    if "vmtx" not in font or "vmtx" not in reference:
        return {"reference_vmtx_aligned": 0, "reference_vertical_vmtx_aligned": 0}
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    reference_vertical = get_single_substitution_mappings(reference, {"vert", "vrt2"})
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    touched = 0
    vertical_touched = 0
    for codepoint in sorted(set(current_cmap) & set(reference_cmap)):
        if codepoint in skip_codepoints:
            continue
        glyph_name = current_cmap[codepoint]
        ref_glyph = reference_cmap[codepoint]
        if ref_glyph in reference["vmtx"].metrics:
            ref_metrics = copy.deepcopy(reference["vmtx"].metrics[ref_glyph])
            if font["vmtx"].metrics.get(glyph_name) != ref_metrics:
                font["vmtx"].metrics[glyph_name] = ref_metrics
                touched += 1
        ref_vertical_glyph = reference_vertical.get(ref_glyph)
        current_vertical_glyph = current_vertical.get(glyph_name)
        if (
            ref_vertical_glyph in reference["vmtx"].metrics
            and current_vertical_glyph in font["vmtx"].metrics
        ):
            ref_metrics = copy.deepcopy(reference["vmtx"].metrics[ref_vertical_glyph])
            if font["vmtx"].metrics.get(current_vertical_glyph) != ref_metrics:
                font["vmtx"].metrics[current_vertical_glyph] = ref_metrics
                vertical_touched += 1
    return {"reference_vmtx_aligned": touched, "reference_vertical_vmtx_aligned": vertical_touched}


def reference_vmtx_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, tuple[int, int]], ...]]:
    profiles: dict[int, tuple[tuple[int, tuple[int, int]], ...]] = {}
    for codepoint in codepoints:
        metrics = []
        for weight_value, reference in sorted(reference_fonts.items()):
            if "vmtx" not in reference:
                continue
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            if glyph_name and glyph_name in reference["vmtx"].metrics:
                metrics.append((weight_value, tuple(reference["vmtx"].metrics[glyph_name])))
        if metrics:
            profiles[codepoint] = tuple(metrics)
    return profiles


def split_reference_vmtx_profiles(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    profiles = reference_vmtx_profiles(reference_fonts, set(font.getBestCmap()) - skip_codepoints)
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        relevant = {codepoint for codepoint in codepoints if codepoint in profiles}
        if len(relevant) <= 1:
            continue
        by_profile: dict[tuple[tuple[int, tuple[int, int]], ...], set[int]] = {}
        for codepoint in relevant:
            by_profile.setdefault(profiles[codepoint], set()).add(codepoint)
        if len(by_profile) <= 1:
            continue
        keep_profile, _keep_codepoints = max(by_profile.items(), key=lambda item: (len(item[1]), -min(item[1])))
        for profile, cps in by_profile.items():
            if profile == keep_profile:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_vmtx_profile_groups_split": split_groups, "reference_vmtx_profile_glyphs_cloned": cloned_glyphs}


def glyph_vmtx_at_weight(font: TTFont, weight_value: int) -> dict[str, tuple[int, int]]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        if "vmtx" not in instance:
            return {}
        return {glyph_name: tuple(metrics) for glyph_name, metrics in instance["vmtx"].metrics.items()}
    finally:
        instance.close()


def add_vmtx_tuple_variation(
    font: TTFont,
    glyph_name: str,
    support: tuple[float, float, float],
    advance_delta: int,
    tsb_delta: int,
) -> None:
    if "gvar" not in font or (not advance_delta and not tsb_delta):
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    top_delta = otRound(tsb_delta)
    bottom_delta = otRound(tsb_delta - advance_delta)
    coordinates[-4:] = [(0, 0), (0, 0), (0, top_delta), (0, bottom_delta)]
    font["gvar"].variations.setdefault(glyph_name, []).append(TupleVariation({"wght": support}, coordinates))


def align_reference_vmtx_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font or "vmtx" not in font:
        return {"reference_vmtx_variations_added": 0, "reference_vmtx_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    reference_vertical = get_single_substitution_mappings(reference_fonts[400], {"vert", "vrt2"})
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        if "vmtx" not in reference:
            continue
        reference_cmap = reference.getBestCmap()
        current_cmap = font.getBestCmap()
        current_metrics = glyph_vmtx_at_weight(font, weight_value)
        glyph_deltas: dict[str, tuple[int, int]] = {}

        def queue_delta(glyph_name: str, target_metrics: tuple[int, int]) -> None:
            nonlocal corrections
            current = current_metrics.get(glyph_name)
            if current is None:
                return
            advance_delta = target_metrics[0] - current[0]
            tsb_delta = target_metrics[1] - current[1]
            if not advance_delta and not tsb_delta:
                return
            glyph_deltas[glyph_name] = (advance_delta, tsb_delta)
            corrections += 1

        for codepoint in sorted(set(current_cmap) & set(reference_cmap)):
            if codepoint in skip_codepoints:
                continue
            glyph_name = current_cmap[codepoint]
            ref_glyph = reference_cmap[codepoint]
            if ref_glyph in reference["vmtx"].metrics:
                queue_delta(glyph_name, tuple(reference["vmtx"].metrics[ref_glyph]))

            ref_vertical_glyph = reference_vertical.get(ref_glyph)
            current_vertical_glyph = current_vertical.get(glyph_name)
            if ref_vertical_glyph in reference["vmtx"].metrics and current_vertical_glyph in font["vmtx"].metrics:
                queue_delta(current_vertical_glyph, tuple(reference["vmtx"].metrics[ref_vertical_glyph]))

        support = supports[weight_value]
        for glyph_name, (advance_delta, tsb_delta) in glyph_deltas.items():
            add_vmtx_tuple_variation(font, glyph_name, support, advance_delta, tsb_delta)
            variations_added += 1
    return {
        "reference_vmtx_variations_added": variations_added,
        "reference_vmtx_variation_corrections": corrections,
    }


def drop_generated_extra_tables(font: TTFont, keep_stat: bool) -> dict[str, int]:
    dropped = 0
    for tag in ("BASE",):
        if tag in font:
            del font[tag]
            dropped += 1
    if not keep_stat and "STAT" in font:
        del font["STAT"]
        dropped += 1
    return {"extra_tables_dropped": dropped}


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


def rebuild_static_stat(font: TTFont, weight_name: str, weight_value: int, italic: bool) -> None:
    from fontTools.otlLib.builder import buildStatTable

    stop = next((item for item in SOURCE_HAN_WEIGHT_STOPS if item["name"] == weight_name), None)
    axes = [
        {
            "tag": "wght",
            "name": "Weight",
            "values": [
                {
                    "value": weight_value,
                    "name": weight_name,
                    "flags": stop.get("flags", 0) if stop else 0,
                }
            ],
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


def source_han_unicodes_like_sarasa(base: TTFont, inter_unicodes: set[int]) -> set[int]:
    base_unicodes = set(base.getBestCmap().keys())
    unicodes: set[int] = set()
    for codepoint in reference_unicodes():
        if codepoint not in base_unicodes:
            continue
        if source_han_overrides_inter(codepoint) or codepoint not in inter_unicodes:
            unicodes.add(codepoint)
    return unicodes


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
            for subtable in single_substitution_subtables(lookup):
                if hasattr(subtable, "mapping"):
                    mapping.update(subtable.mapping)
    return mapping


def get_single_substitution_mappings(font: TTFont, tags: set[str]) -> dict[str, str]:
    if "GSUB" not in font:
        return {}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {}
    mapping: dict[str, str] = {}
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in tags:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if hasattr(subtable, "mapping"):
                    mapping.update(subtable.mapping)
    return mapping


def remove_vertical_long_dash_ligature_mappings(font: TTFont) -> dict[str, int]:
    if "GSUB" not in font or "hmtx" not in font or "vmtx" not in font:
        return {"vertical_long_dash_ligature_mappings_removed": 0}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {"vertical_long_dash_ligature_mappings_removed": 0}
    removed = 0
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in {"vert", "vrt2"}:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if not hasattr(subtable, "mapping"):
                    continue
                for source_name, target_name in list(subtable.mapping.items()):
                    source_width = font["hmtx"].metrics.get(source_name, (0, 0))[0]
                    target_height = font["vmtx"].metrics.get(target_name, (0, 0))[0]
                    if source_width > font["head"].unitsPerEm and target_height > font["head"].unitsPerEm:
                        del subtable.mapping[source_name]
                        removed += 1
    return {"vertical_long_dash_ligature_mappings_removed": removed}


def single_substitution_subtables(lookup: ot.Lookup) -> list[Any]:
    if lookup.LookupType == 1:
        return list(lookup.SubTable)
    if lookup.LookupType == 7:
        subtables = []
        for subtable in lookup.SubTable:
            if getattr(subtable, "ExtensionLookupType", None) == 1 and getattr(subtable, "ExtSubTable", None):
                subtables.append(subtable.ExtSubTable)
        return subtables
    return []


def glyph_to_unicodes(font: TTFont) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for codepoint, glyph_name in font.getBestCmap().items():
        result.setdefault(glyph_name, set()).add(codepoint)
    return result


def reference_locl_source_unicodes() -> set[int]:
    font = TTFont(REFERENCE_SARASA)
    try:
        reverse = glyph_to_unicodes(font)
        unicodes: set[int] = set()
        for source_name in get_single_substitution_mapping(font, "locl"):
            unicodes.update(reverse.get(source_name, set()))
        return unicodes
    finally:
        font.close()


def prune_locl_like_reference(font: TTFont) -> dict[str, int]:
    allowed_unicodes = reference_locl_source_unicodes()
    reverse = glyph_to_unicodes(font)
    before = 0
    after = 0
    emptied_lookups = 0

    if "GSUB" not in font:
        return {
            "reference_locl_codepoints": len(allowed_unicodes),
            "locl_mappings_before_prune": 0,
            "locl_mappings_after_prune": 0,
            "locl_lookups_emptied": 0,
        }

    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {
            "reference_locl_codepoints": len(allowed_unicodes),
            "locl_mappings_before_prune": 0,
            "locl_mappings_after_prune": 0,
            "locl_lookups_emptied": 0,
        }

    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag != "locl":
            continue
        kept_indices = []
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            lookup_has_mappings = False
            single_subtables = single_substitution_subtables(lookup)
            if single_subtables:
                for subtable in single_subtables:
                    before += len(subtable.mapping)
                    subtable.mapping = {
                        source: target
                        for source, target in subtable.mapping.items()
                        if reverse.get(source, set()) & allowed_unicodes
                    }
                    after += len(subtable.mapping)
                    if subtable.mapping:
                        lookup_has_mappings = True
            elif lookup.LookupType != 7:
                lookup_has_mappings = True
            if lookup_has_mappings:
                kept_indices.append(lookup_index)
            else:
                emptied_lookups += 1
        record.Feature.LookupListIndex = kept_indices
        record.Feature.LookupCount = len(kept_indices)

    return {
        "reference_locl_codepoints": len(allowed_unicodes),
        "locl_mappings_before_prune": before,
        "locl_mappings_after_prune": after,
        "locl_lookups_emptied": emptied_lookups,
    }


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


def clone_cmap_glyph_for_codepoint(font: TTFont, codepoint: int) -> str | None:
    cmap = font.getBestCmap()
    glyph_name = cmap.get(codepoint)
    if not glyph_name:
        return None
    if sum(1 for glyph in cmap.values() if glyph == glyph_name) <= 1:
        return glyph_name

    order = font.getGlyphOrder()
    new_name = f"{glyph_name}.u{codepoint:04X}"
    suffix = 1
    while new_name in font.getGlyphSet():
        suffix += 1
        new_name = f"{glyph_name}.u{codepoint:04X}.{suffix}"

    font["glyf"].glyphs[new_name] = copy.deepcopy(font["glyf"][glyph_name])
    font["hmtx"].metrics[new_name] = copy.deepcopy(font["hmtx"].metrics[glyph_name])
    if "vmtx" in font and glyph_name in font["vmtx"].metrics:
        font["vmtx"].metrics[new_name] = copy.deepcopy(font["vmtx"].metrics[glyph_name])
    if "gvar" in font:
        font["gvar"].variations[new_name] = copy.deepcopy(font["gvar"].variations.get(glyph_name, []))
    order.append(new_name)
    font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(order)
    for cmap_table in font["cmap"].tables:
        if cmap_table.isUnicode() and cmap_table.cmap.get(codepoint) == glyph_name:
            cmap_table.cmap[codepoint] = new_name
    return new_name


def clone_cmap_glyph_for_codepoints(font: TTFont, codepoints: set[int]) -> str | None:
    if not codepoints:
        return None
    cmap = font.getBestCmap()
    first = min(codepoints)
    old_name = cmap.get(first)
    if not old_name:
        return None
    new_name = clone_cmap_glyph_for_codepoint(font, first)
    if not new_name or new_name == old_name:
        return new_name
    for cmap_table in font["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint in codepoints:
            if cmap_table.cmap.get(codepoint) == old_name:
                cmap_table.cmap[codepoint] = new_name
    return new_name


def split_reference_cmap_aliases(font: TTFont, reference: TTFont) -> dict[str, int]:
    reference_cmap = reference.getBestCmap()
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        if len(codepoints) <= 1:
            continue
        ref_groups: dict[str, set[int]] = {}
        for codepoint in codepoints:
            ref_glyph = reference_cmap.get(codepoint)
            if ref_glyph:
                ref_groups.setdefault(ref_glyph, set()).add(codepoint)
        if len(ref_groups) <= 1:
            continue

        current_width = font["hmtx"].metrics.get(glyph_name, (None, None))[0]

        def group_score(item: tuple[str, set[int]]) -> tuple[int, int, int]:
            _ref_glyph, cps = item
            widths = {
                reference["hmtx"].metrics[reference_cmap[cp]][0]
                for cp in cps
                if cp in reference_cmap and reference_cmap[cp] in reference["hmtx"].metrics
            }
            return (1 if current_width in widths else 0, len(cps), -min(cps))

        keep_ref_glyph, _keep_codepoints = max(ref_groups.items(), key=group_score)
        for ref_glyph, cps in ref_groups.items():
            if ref_glyph == keep_ref_glyph:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_alias_groups_split": split_groups, "reference_alias_glyphs_cloned": cloned_glyphs}


def align_reference_cmap_alias_mappings(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    reference_groups: dict[str, set[int]] = {}
    for codepoint, glyph_name in reference.getBestCmap().items():
        if codepoint not in skip_codepoints:
            reference_groups.setdefault(glyph_name, set()).add(codepoint)

    remapped = 0
    current_cmap = font.getBestCmap()
    for ref_glyph, codepoints in reference_groups.items():
        shared = sorted(cp for cp in codepoints if cp in current_cmap)
        if len(shared) <= 1:
            continue
        canonical_cp = next((cp for cp in shared if current_cmap[cp] == ref_glyph), shared[0])
        canonical_glyph = current_cmap[canonical_cp]
        for codepoint in shared:
            old_glyph = current_cmap.get(codepoint)
            if old_glyph == canonical_glyph:
                continue
            for cmap_table in font["cmap"].tables:
                if cmap_table.isUnicode() and cmap_table.cmap.get(codepoint) == old_glyph:
                    cmap_table.cmap[codepoint] = canonical_glyph
                    remapped += 1
    return {"reference_cmap_alias_mappings_aligned": remapped}


def align_reference_advances(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    reference_cmap = reference.getBestCmap()
    touched = 0
    cloned = 0
    for codepoint in sorted(set(font.getBestCmap()) & set(reference_cmap)):
        if codepoint in skip_codepoints:
            continue
        cmap = font.getBestCmap()
        glyph_name = cmap.get(codepoint)
        ref_glyph = reference_cmap.get(codepoint)
        if not glyph_name or not ref_glyph:
            continue
        ref_width = reference["hmtx"].metrics[ref_glyph][0]
        current_width = font["hmtx"].metrics.get(glyph_name, (ref_width, 0))[0]
        if current_width == ref_width:
            continue

        shared_codepoints = glyph_to_unicodes(font).get(glyph_name, set())
        if len(shared_codepoints) > 1:
            shared_widths = {
                reference["hmtx"].metrics[reference_cmap[cp]][0]
                for cp in shared_codepoints
                if cp not in skip_codepoints and cp in reference_cmap and reference_cmap[cp] in reference["hmtx"].metrics
            }
            if (shared_codepoints & skip_codepoints) or any(width != ref_width for width in shared_widths):
                new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
                if new_name and new_name != glyph_name:
                    glyph_name = new_name
                    cloned += 1
        set_advance_width(font, glyph_name, ref_width)
        freeze_advance_variation(font, glyph_name)
        touched += 1
    return {"reference_advances_aligned": touched, "reference_advance_glyphs_cloned": cloned}


def normalized_wght(font: TTFont, value: int) -> float:
    axis = next(axis for axis in font["fvar"].axes if axis.axisTag == "wght")
    if value == axis.defaultValue:
        normalized = 0.0
    if value < axis.defaultValue:
        normalized = (value - axis.defaultValue) / (axis.defaultValue - axis.minValue)
    elif value > axis.defaultValue:
        normalized = (value - axis.defaultValue) / (axis.maxValue - axis.defaultValue)
    if "avar" in font and "wght" in font["avar"].segments:
        normalized = piecewiseLinearMap(normalized, font["avar"].segments["wght"])
    return normalized


def reference_width_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, int], ...]]:
    profiles: dict[int, tuple[tuple[int, int], ...]] = {}
    for codepoint in codepoints:
        widths = []
        for weight_value, reference in sorted(reference_fonts.items()):
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            if glyph_name and glyph_name in reference["hmtx"].metrics:
                widths.append((weight_value, reference["hmtx"].metrics[glyph_name][0]))
        if widths:
            profiles[codepoint] = tuple(widths)
    return profiles


def reference_lsb_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, int], ...]]:
    profiles: dict[int, tuple[tuple[int, int], ...]] = {}
    for codepoint in codepoints:
        lsbs = []
        for weight_value, reference in sorted(reference_fonts.items()):
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            if glyph_name and glyph_name in reference["hmtx"].metrics:
                lsbs.append((weight_value, reference["hmtx"].metrics[glyph_name][1]))
        if lsbs:
            profiles[codepoint] = tuple(lsbs)
    return profiles


def split_reference_advance_profiles(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    profiles = reference_width_profiles(reference_fonts, set(font.getBestCmap()) - skip_codepoints)
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        relevant = {codepoint for codepoint in codepoints if codepoint in profiles}
        if len(relevant) <= 1:
            continue
        by_profile: dict[tuple[tuple[int, int], ...], set[int]] = {}
        for codepoint in relevant:
            by_profile.setdefault(profiles[codepoint], set()).add(codepoint)
        if len(by_profile) <= 1:
            continue
        keep_profile, _keep_codepoints = max(by_profile.items(), key=lambda item: (len(item[1]), -min(item[1])))
        for profile, cps in by_profile.items():
            if profile == keep_profile:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_advance_profile_groups_split": split_groups, "reference_advance_profile_glyphs_cloned": cloned_glyphs}


def split_reference_lsb_profiles(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    profiles = reference_lsb_profiles(reference_fonts, set(font.getBestCmap()) - skip_codepoints)
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        relevant = {codepoint for codepoint in codepoints if codepoint in profiles}
        if len(relevant) <= 1:
            continue
        by_profile: dict[tuple[tuple[int, int], ...], set[int]] = {}
        for codepoint in relevant:
            by_profile.setdefault(profiles[codepoint], set()).add(codepoint)
        if len(by_profile) <= 1:
            continue
        keep_profile, _keep_codepoints = max(by_profile.items(), key=lambda item: (len(item[1]), -min(item[1])))
        for profile, cps in by_profile.items():
            if profile == keep_profile:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_lsb_profile_groups_split": split_groups, "reference_lsb_profile_glyphs_cloned": cloned_glyphs}


def gvar_coordinate_count(font: TTFont, glyph_name: str) -> int:
    variations = font["gvar"].variations.get(glyph_name, [])
    if variations:
        return len(variations[0].coordinates)
    return len(font["glyf"][glyph_name].getCoordinates(font["glyf"])[0]) + 4


def add_advance_tuple_variation(font: TTFont, glyph_name: str, support: tuple[float, float, float], delta: int) -> None:
    if "gvar" not in font or not delta:
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    coordinates[-4:] = [(0, 0), (otRound(delta), 0), (0, 0), (0, 0)]
    font["gvar"].variations.setdefault(glyph_name, []).append(TupleVariation({"wght": support}, coordinates))


def add_lsb_tuple_variation(font: TTFont, glyph_name: str, support: tuple[float, float, float], delta: int) -> None:
    if "gvar" not in font or not delta:
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    phantom_delta = otRound(-delta)
    coordinates[-4:] = [(phantom_delta, 0), (phantom_delta, 0), (0, 0), (0, 0)]
    font["gvar"].variations.setdefault(glyph_name, []).append(TupleVariation({"wght": support}, coordinates))


def advance_supports(font: TTFont, weights: list[int]) -> dict[int, tuple[float, float, float]]:
    normalized = {weight: normalized_wght(font, weight) for weight in weights}
    supports: dict[int, tuple[float, float, float]] = {}
    negative = sorted((weight, value) for weight, value in normalized.items() if value < 0)
    positive = sorted((weight, value) for weight, value in normalized.items() if value > 0)
    for index, (weight, value) in enumerate(negative):
        start = -1.0 if index == 0 else negative[index - 1][1]
        end = 0.0 if index == len(negative) - 1 else negative[index + 1][1]
        supports[weight] = (start, value, end)
    for index, (weight, value) in enumerate(positive):
        start = 0.0 if index == 0 else positive[index - 1][1]
        end = 1.0 if index == len(positive) - 1 else positive[index + 1][1]
        supports[weight] = (start, value, end)
    return supports


def cmap_widths_at_weight(font: TTFont, weight_value: int) -> dict[int, int]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        cmap = instance.getBestCmap()
        return {
            codepoint: instance["hmtx"].metrics[glyph_name][0]
            for codepoint, glyph_name in cmap.items()
            if glyph_name in instance["hmtx"].metrics
        }
    finally:
        instance.close()


def cmap_hmtx_at_weight(font: TTFont, weight_value: int) -> dict[int, tuple[int, int]]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        cmap = instance.getBestCmap()
        return {
            codepoint: tuple(instance["hmtx"].metrics[glyph_name])
            for codepoint, glyph_name in cmap.items()
            if glyph_name in instance["hmtx"].metrics
        }
    finally:
        instance.close()


def align_reference_hmtx_lsb(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    touched = 0
    cloned = 0
    targets: dict[str, int] = {}
    for codepoint in sorted(set(current_cmap) & set(reference_cmap)):
        if codepoint in skip_codepoints:
            continue
        glyph_name = current_cmap[codepoint]
        ref_glyph = reference_cmap[codepoint]
        if ref_glyph not in reference["hmtx"].metrics:
            continue
        target_lsb = reference["hmtx"].metrics[ref_glyph][1]
        existing = targets.get(glyph_name)
        if existing is not None and existing != target_lsb:
            new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
            if new_name and new_name != glyph_name:
                glyph_name = new_name
                cloned += 1
        targets[glyph_name] = target_lsb
    for glyph_name, target_lsb in targets.items():
        advance_width, lsb = font["hmtx"].metrics[glyph_name]
        if lsb != target_lsb:
            font["hmtx"].metrics[glyph_name] = (advance_width, target_lsb)
            touched += 1
    return {"reference_lsb_aligned": touched, "reference_lsb_glyphs_cloned": cloned}


def align_reference_advance_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {"reference_advance_variations_added": 0, "reference_advance_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        reference_cmap = reference.getBestCmap()
        current_widths = cmap_widths_at_weight(font, weight_value)
        glyph_deltas: dict[str, int] = {}
        cmap = font.getBestCmap()
        for codepoint in sorted(set(cmap) & set(reference_cmap)):
            if codepoint in skip_codepoints:
                continue
            glyph_name = cmap[codepoint]
            ref_glyph = reference_cmap[codepoint]
            if ref_glyph not in reference["hmtx"].metrics:
                continue
            target_width = reference["hmtx"].metrics[ref_glyph][0]
            current_width = current_widths.get(codepoint)
            if current_width is None:
                continue
            delta = target_width - current_width
            if not delta:
                continue
            existing_delta = glyph_deltas.get(glyph_name)
            if existing_delta is not None and existing_delta != delta:
                new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
                if new_name and new_name != glyph_name:
                    glyph_name = new_name
            glyph_deltas[glyph_name] = delta
            corrections += 1
        support = supports[weight_value]
        for glyph_name, delta in glyph_deltas.items():
            add_advance_tuple_variation(font, glyph_name, support, delta)
            variations_added += 1
    return {
        "reference_advance_variations_added": variations_added,
        "reference_advance_variation_corrections": corrections,
    }


def align_reference_lsb_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {"reference_lsb_variations_added": 0, "reference_lsb_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        reference_cmap = reference.getBestCmap()
        current_metrics = cmap_hmtx_at_weight(font, weight_value)
        glyph_deltas: dict[str, int] = {}
        cmap = font.getBestCmap()
        for codepoint in sorted(set(cmap) & set(reference_cmap)):
            if codepoint in skip_codepoints:
                continue
            glyph_name = cmap[codepoint]
            ref_glyph = reference_cmap[codepoint]
            if ref_glyph not in reference["hmtx"].metrics:
                continue
            current = current_metrics.get(codepoint)
            if current is None:
                continue
            target_lsb = reference["hmtx"].metrics[ref_glyph][1]
            delta = target_lsb - current[1]
            if not delta:
                continue
            existing_delta = glyph_deltas.get(glyph_name)
            if existing_delta is not None and existing_delta != delta:
                new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
                if new_name and new_name != glyph_name:
                    glyph_name = new_name
            glyph_deltas[glyph_name] = delta
            corrections += 1
        support = supports[weight_value]
        for glyph_name, delta in glyph_deltas.items():
            add_lsb_tuple_variation(font, glyph_name, support, delta)
            variations_added += 1
    return {
        "reference_lsb_variations_added": variations_added,
        "reference_lsb_variation_corrections": corrections,
    }


def tnum_digit_targets(font: TTFont) -> dict[int, str]:
    if "hmtx" not in font:
        return {}
    mapping = get_single_substitution_mapping(font, "tnum")
    cmap = font.getBestCmap()
    targets: dict[int, str] = {}
    for codepoint in range(0x30, 0x3A):
        source_glyph = cmap.get(codepoint)
        target_glyph = mapping.get(source_glyph) if source_glyph else None
        if not target_glyph:
            target_glyph = mapping.get(DIGITS[codepoint - 0x30])
        if target_glyph in font["hmtx"].metrics:
            targets[codepoint] = target_glyph
    return targets


def reference_digit_hmtx(reference: TTFont) -> dict[int, tuple[int, int]]:
    cmap = reference.getBestCmap()
    metrics: dict[int, tuple[int, int]] = {}
    for codepoint in range(0x30, 0x3A):
        glyph_name = cmap.get(codepoint)
        if glyph_name in reference["hmtx"].metrics:
            metrics[codepoint] = tuple(reference["hmtx"].metrics[glyph_name])
    return metrics


def align_tnum_digit_targets(font: TTFont, reference: TTFont) -> dict[str, int]:
    targets = tnum_digit_targets(font)
    reference_metrics = reference_digit_hmtx(reference)
    touched = 0
    for codepoint, target_glyph in targets.items():
        metrics = reference_metrics.get(codepoint)
        if metrics and tuple(font["hmtx"].metrics[target_glyph]) != metrics:
            font["hmtx"].metrics[target_glyph] = metrics
            touched += 1
    return {"tnum_digit_target_hmtx_aligned": touched}


def tnum_digit_target_hmtx_at_weight(font: TTFont, weight_value: int) -> dict[int, tuple[int, int]]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        targets = tnum_digit_targets(instance)
        return {codepoint: tuple(instance["hmtx"].metrics[glyph_name]) for codepoint, glyph_name in targets.items()}
    finally:
        instance.close()


def align_tnum_digit_target_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {"tnum_digit_target_variations_added": 0, "tnum_digit_target_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    targets = tnum_digit_targets(font)
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference_metrics = reference_digit_hmtx(reference_fonts[weight_value])
        current_metrics = tnum_digit_target_hmtx_at_weight(font, weight_value)
        support = supports[weight_value]
        for codepoint, target_glyph in targets.items():
            target_metrics = reference_metrics.get(codepoint)
            current = current_metrics.get(codepoint)
            if not target_metrics or current is None:
                continue
            advance_delta = target_metrics[0] - current[0]
            lsb_delta = target_metrics[1] - current[1]
            if advance_delta:
                add_advance_tuple_variation(font, target_glyph, support, advance_delta)
                variations_added += 1
            if lsb_delta:
                add_lsb_tuple_variation(font, target_glyph, support, lsb_delta)
                variations_added += 1
            if advance_delta or lsb_delta:
                corrections += 1
    return {
        "tnum_digit_target_variations_added": variations_added,
        "tnum_digit_target_variation_corrections": corrections,
    }


def sum_count_reports(*reports: dict[str, int]) -> dict[str, int]:
    total: dict[str, int] = {}
    for report in reports:
        for key, value in report.items():
            total[key] = total.get(key, 0) + value
    return total


def prefix_count_report(report: dict[str, int], prefix: str) -> dict[str, int]:
    return {f"{prefix}{key}": value for key, value in report.items()}


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


def glyph_x_min(font: TTFont, glyph_name: str, fallback: int = 0) -> int:
    if glyph_name not in font["glyf"].glyphs:
        return fallback
    glyph = font["glyf"][glyph_name]
    if glyph.isComposite() or getattr(glyph, "numberOfContours", 0) > 0:
        glyph.recalcBounds(font["glyf"])
        return getattr(glyph, "xMin", fallback)
    return fallback


def sync_hmtx_lsb_to_glyph_bounds(font: TTFont) -> dict[str, int]:
    if "hmtx" not in font or "glyf" not in font:
        return {"hmtx_lsb_synced": 0}
    touched = 0
    for glyph_name, (advance_width, lsb) in list(font["hmtx"].metrics.items()):
        if glyph_name not in font["glyf"].glyphs:
            continue
        new_lsb = glyph_x_min(font, glyph_name, lsb)
        if new_lsb != lsb:
            font["hmtx"].metrics[glyph_name] = (advance_width, new_lsb)
            touched += 1
    return {"hmtx_lsb_synced": touched}


def set_advance_width(font: TTFont, glyph_name: str, width: int) -> None:
    _old_width, lsb = font["hmtx"].metrics.get(glyph_name, (width, 0))
    font["hmtx"].metrics[glyph_name] = (otRound(width), glyph_x_min(font, glyph_name, lsb))


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
    for codepoint in SOURCE_HAN_FORCED_CODEPOINTS - set(SANITIZER_TYPES_PWID):
        clone_cmap_glyph_for_codepoint(font, codepoint)
    pwid_count = bake_single_substitution_feature(font, "pwid", lambda cp: cp in SANITIZER_TYPES_PWID)
    cmap = font.getBestCmap()
    touched = 0
    for codepoint, sanitizer in SANITIZER_TYPES_PWID.items():
        clone_cmap_glyph_for_codepoint(font, codepoint)
    cmap = font.getBestCmap()
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


def load_base(italic: bool, inter_unicodes: set[int]) -> tuple[TTFont, dict[str, int]]:
    base = TTFont(BASE_VF)
    base = instantiateVariableFont(base, AXIS_LIMIT, inplace=False, optimize=True)
    subset_font(base, source_han_unicodes_like_sarasa(base, inter_unicodes))
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
    bake_inter_ui_tnum_defaults(inter)
    return inter


def bake_inter_ui_tnum_defaults(font: TTFont) -> int:
    mapping = get_single_substitution_mapping(font, "tnum")
    if not mapping:
        return 0
    touched = 0
    skip = set(range(0x30, 0x3A)) | {0x2D, 0x3A}
    for cmap_table in font["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint, glyph_name in list(cmap_table.cmap.items()):
            if codepoint in skip:
                continue
            target = mapping.get(glyph_name)
            if target and target in font.getGlyphSet():
                cmap_table.cmap[codepoint] = target
                touched += 1
    return touched


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
    inter_cmap = inter.getBestCmap()
    base_cmap = base.getBestCmap()
    allowed_inter_unicodes = {cp for cp in allowed_unicodes if cp in inter_cmap and cp not in base_cmap}
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


def rename_ot_glyph_references(obj: Any, rename: dict[str, str], seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if isinstance(obj, str) or obj is None or isinstance(obj, (int, float, bool, bytes)):
        return
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            new_key = rename.get(key, key) if isinstance(key, str) else key
            if new_key != key:
                del obj[key]
                obj[new_key] = value
            if isinstance(value, str) and value in rename:
                obj[new_key] = rename[value]
            else:
                rename_ot_glyph_references(obj[new_key], rename, seen)
        return
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            if isinstance(value, str) and value in rename:
                obj[index] = rename[value]
            else:
                rename_ot_glyph_references(value, rename, seen)
        return
    if isinstance(obj, tuple):
        return
    if hasattr(obj, "__dict__"):
        for key, value in vars(obj).items():
            if isinstance(value, str) and value in rename:
                setattr(obj, key, rename[value])
            else:
                rename_ot_glyph_references(value, rename, seen)


def strip_ot_variation_devices(obj: Any, seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (str, int, float, bool, bytes)):
        return
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
                obj[key] = None
            else:
                strip_ot_variation_devices(value, seen)
        return
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
                obj[index] = None
            else:
                strip_ot_variation_devices(value, seen)
        return
    if isinstance(obj, tuple):
        for value in obj:
            strip_ot_variation_devices(value, seen)
        return
    if hasattr(obj, "__dict__"):
        for key, value in vars(obj).items():
            if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
                setattr(obj, key, None)
            else:
                strip_ot_variation_devices(value, seen)


def append_layout_features(
    base: TTFont,
    inter: TTFont,
    table_tag: str,
    feature_tags: set[str],
) -> dict[str, int]:
    if table_tag not in inter:
        return {f"inter_{table_tag.lower()}_features_imported": 0, f"inter_{table_tag.lower()}_lookups_imported": 0}
    if table_tag not in base:
        base[table_tag] = copy.deepcopy(inter[table_tag])
        rename = {name: prefixed(name) for name in inter.getGlyphOrder() if name != ".notdef"}
        rename_ot_glyph_references(base[table_tag].table, rename)
        return {
            f"inter_{table_tag.lower()}_features_imported": len(base[table_tag].table.FeatureList.FeatureRecord)
            if base[table_tag].table.FeatureList
            else 0,
            f"inter_{table_tag.lower()}_lookups_imported": len(base[table_tag].table.LookupList.Lookup)
            if base[table_tag].table.LookupList
            else 0,
        }

    source = inter[table_tag].table
    target = base[table_tag].table
    if not source.FeatureList or not source.LookupList:
        return {f"inter_{table_tag.lower()}_features_imported": 0, f"inter_{table_tag.lower()}_lookups_imported": 0}
    if target.LookupList is None:
        target.LookupList = ot.LookupList()
        target.LookupList.Lookup = []
        target.LookupList.LookupCount = 0
    if target.FeatureList is None:
        target.FeatureList = ot.FeatureList()
        target.FeatureList.FeatureRecord = []
        target.FeatureList.FeatureCount = 0

    rename = {name: prefixed(name) for name in inter.getGlyphOrder() if name != ".notdef"}
    feature_records = [record for record in source.FeatureList.FeatureRecord if record.FeatureTag in feature_tags]
    lookup_indices = sorted({index for record in feature_records for index in record.Feature.LookupListIndex})
    lookup_index_map: dict[int, int] = {}
    for old_index in lookup_indices:
        lookup = copy.deepcopy(source.LookupList.Lookup[old_index])
        rename_ot_glyph_references(lookup, rename)
        if table_tag == "GPOS":
            strip_ot_variation_devices(lookup)
        new_index = len(target.LookupList.Lookup)
        target.LookupList.Lookup.append(lookup)
        lookup_index_map[old_index] = new_index
    target.LookupList.LookupCount = len(target.LookupList.Lookup)

    imported_tags: set[str] = set()
    for source_record in feature_records:
        record = copy.deepcopy(source_record)
        record.Feature.LookupListIndex = [lookup_index_map[index] for index in source_record.Feature.LookupListIndex if index in lookup_index_map]
        record.Feature.LookupCount = len(record.Feature.LookupListIndex)
        if not record.Feature.LookupListIndex:
            continue
        target.FeatureList.FeatureRecord.append(record)
        imported_tags.add(record.FeatureTag)
    target.FeatureList.FeatureCount = len(target.FeatureList.FeatureRecord)
    enable_features_for_all_scripts(base, imported_tags, table_tag)
    return {
        f"inter_{table_tag.lower()}_features_imported": len(imported_tags),
        f"inter_{table_tag.lower()}_lookups_imported": len(lookup_index_map),
    }


def import_inter_layout_features(base: TTFont, inter: TTFont) -> dict[str, int]:
    report: dict[str, int] = {}
    report.update(append_layout_features(base, inter, "GSUB", INTER_GSUB_FEATURES))
    report.update(append_layout_features(base, inter, "GPOS", INTER_GPOS_FEATURES))
    return report


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


def drop_nonfinal_gsub_features(font: TTFont, allowed_features: set[str] = FINAL_GSUB_FEATURES) -> int:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return 0
    tags = {record.FeatureTag for record in font["GSUB"].table.FeatureList.FeatureRecord}
    return drop_feature_records(font["GSUB"], tags - allowed_features)


def align_layout_feature_template(font: TTFont, reference: TTFont, table_tag: str) -> dict[str, int]:
    key = table_tag.lower()
    if table_tag not in font or table_tag not in reference:
        return {
            f"{key}_feature_records_before_template": 0,
            f"{key}_feature_records_after_template": 0,
            f"{key}_langsys_after_template": 0,
        }
    table = font[table_tag].table
    ref_table = reference[table_tag].table
    if not table.FeatureList or not ref_table.FeatureList or not ref_table.ScriptList:
        return {
            f"{key}_feature_records_before_template": 0,
            f"{key}_feature_records_after_template": 0,
            f"{key}_langsys_after_template": 0,
        }

    current_by_tag: dict[str, list[Any]] = {}
    for record in table.FeatureList.FeatureRecord:
        current_by_tag.setdefault(record.FeatureTag, []).append(record)

    old_count = len(table.FeatureList.FeatureRecord)
    ref_to_new: dict[int, int] = {}
    used_by_tag: dict[str, int] = {}
    new_records = []
    for ref_index, ref_record in enumerate(ref_table.FeatureList.FeatureRecord):
        candidates = current_by_tag.get(ref_record.FeatureTag)
        if not candidates:
            continue
        use_index = min(used_by_tag.get(ref_record.FeatureTag, 0), len(candidates) - 1)
        used_by_tag[ref_record.FeatureTag] = used_by_tag.get(ref_record.FeatureTag, 0) + 1
        record = copy.deepcopy(candidates[use_index])
        record.FeatureTag = ref_record.FeatureTag
        ref_to_new[ref_index] = len(new_records)
        new_records.append(record)

    if not new_records:
        return {
            f"{key}_feature_records_before_template": old_count,
            f"{key}_feature_records_after_template": old_count,
            f"{key}_langsys_after_template": 0,
        }

    def remap_langsys(langsys: Any) -> Any | None:
        new_langsys = copy.deepcopy(langsys)
        indices = [ref_to_new[index] for index in list(langsys.FeatureIndex or []) if index in ref_to_new]
        if not indices:
            return None
        new_langsys.FeatureIndex = indices
        new_langsys.FeatureCount = len(indices)
        if getattr(new_langsys, "ReqFeatureIndex", 0xFFFF) != 0xFFFF:
            new_langsys.ReqFeatureIndex = ref_to_new.get(new_langsys.ReqFeatureIndex, 0xFFFF)
        return new_langsys

    new_script_list = ot.ScriptList()
    new_script_list.ScriptRecord = []
    langsys_count = 0
    for ref_script_record in ref_table.ScriptList.ScriptRecord:
        script = ot.Script()
        script.DefaultLangSys = None
        script.LangSysRecord = []
        if ref_script_record.Script.DefaultLangSys:
            script.DefaultLangSys = remap_langsys(ref_script_record.Script.DefaultLangSys)
            if script.DefaultLangSys:
                langsys_count += 1
        for ref_lang_record in ref_script_record.Script.LangSysRecord:
            langsys = remap_langsys(ref_lang_record.LangSys)
            if not langsys:
                continue
            lang_record = ot.LangSysRecord()
            lang_record.LangSysTag = ref_lang_record.LangSysTag
            lang_record.LangSys = langsys
            script.LangSysRecord.append(lang_record)
            langsys_count += 1
        if not script.DefaultLangSys and not script.LangSysRecord:
            continue
        script.LangSysCount = len(script.LangSysRecord)
        script_record = ot.ScriptRecord()
        script_record.ScriptTag = ref_script_record.ScriptTag
        script_record.Script = script
        new_script_list.ScriptRecord.append(script_record)

    new_script_list.ScriptCount = len(new_script_list.ScriptRecord)
    table.FeatureList.FeatureRecord = new_records
    table.FeatureList.FeatureCount = len(new_records)
    table.ScriptList = new_script_list
    return {
        f"{key}_feature_records_before_template": old_count,
        f"{key}_feature_records_after_template": len(new_records),
        f"{key}_langsys_after_template": langsys_count,
    }


def pad_lookup_list_to_reference_count(font: TTFont, reference: TTFont, table_tag: str) -> dict[str, int]:
    key = table_tag.lower()
    if table_tag not in font or table_tag not in reference:
        return {f"{key}_lookups_before_padding": 0, f"{key}_lookups_after_padding": 0}
    table = font[table_tag].table
    ref_table = reference[table_tag].table
    if not table.LookupList or not ref_table.LookupList or not table.LookupList.Lookup:
        return {f"{key}_lookups_before_padding": 0, f"{key}_lookups_after_padding": 0}
    before = len(table.LookupList.Lookup)
    target = len(ref_table.LookupList.Lookup)
    while len(table.LookupList.Lookup) < target:
        table.LookupList.Lookup.append(copy.deepcopy(table.LookupList.Lookup[-1]))
    table.LookupList.LookupCount = len(table.LookupList.Lookup)
    return {f"{key}_lookups_before_padding": before, f"{key}_lookups_after_padding": len(table.LookupList.Lookup)}


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


def enable_features_for_all_scripts(font: TTFont, tags: set[str], table_tag: str = "GSUB") -> None:
    if table_tag not in font:
        return
    table = font[table_tag].table
    if not table.FeatureList or not table.ScriptList:
        return
    indices = [i for i, record in enumerate(table.FeatureList.FeatureRecord) if record.FeatureTag in tags]
    if not indices:
        return
    for script_record in table.ScriptList.ScriptRecord:
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


def ensure_empty_gsub_features(font: TTFont, tags: set[str]) -> dict[str, int]:
    added = 0
    for tag in sorted(tags):
        if not has_feature(font, tag):
            append_gsub_feature(font, tag, [])
            added += 1
    enable_features_for_all_scripts(font, tags)
    return {"empty_gsub_features_added": added}


def collect_prefixed_inter_feature_mapping(inter: TTFont, tag: str) -> dict[str, str]:
    return {prefixed(src): prefixed(dst) for src, dst in get_single_substitution_mapping(inter, tag).items()}


def add_digit_width_features(font: TTFont, inter: TTFont) -> dict[str, Any]:
    tnum = collect_prefixed_inter_feature_mapping(inter, "tnum")
    pnum = collect_prefixed_inter_feature_mapping(inter, "pnum")
    if not pnum:
        pnum = {dst: src for src, dst in tnum.items()}
    enable_features_for_all_scripts(font, {"tnum", "pnum"})
    return {
        "tnum_feature_added": has_feature(font, "tnum"),
        "pnum_feature_added": has_feature(font, "pnum"),
        "tnum_mappings": len(tnum),
        "pnum_mappings": len(pnum),
    }


def make_polygon_glyph(points: list[tuple[float, float]]) -> Any:
    pen = TTGlyphPen(None)
    pen.moveTo((otRound(points[0][0]), otRound(points[0][1])))
    for x, y in points[1:]:
        pen.lineTo((otRound(x), otRound(y)))
    pen.closePath()
    return pen.glyph()


def add_simple_glyph(font: TTFont, glyph_name: str, source_name: str, points: list[tuple[float, float]]) -> None:
    font["glyf"].glyphs[glyph_name] = make_polygon_glyph(points)
    font["hmtx"].metrics[glyph_name] = copy.deepcopy(font["hmtx"].metrics[source_name])
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        font["vmtx"].metrics[glyph_name] = copy.deepcopy(font["vmtx"].metrics[source_name])
    if "gvar" in font:
        font["gvar"].variations[glyph_name] = []
    order = font.getGlyphOrder()
    if glyph_name not in order:
        order.append(glyph_name)
        font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(font.getGlyphOrder())


def glyph_bbox(font: TTFont, glyph_name: str) -> tuple[int, int, int, int] | None:
    if glyph_name not in font["glyf"].glyphs:
        return None
    glyph = font["glyf"][glyph_name]
    glyph.recalcBounds(font["glyf"])
    if not hasattr(glyph, "xMin"):
        return None
    return glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax


def add_vert_alias(font: TTFont, source_codepoint: int, target_codepoint: int) -> int:
    if "GSUB" not in font:
        return 0
    cmap = font.getBestCmap()
    source_glyph = cmap.get(source_codepoint)
    target_glyph = cmap.get(target_codepoint)
    if not source_glyph or not target_glyph:
        return 0

    added = 0
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return 0
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in {"vert", "vrt2"}:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            if lookup.LookupType != 1:
                continue
            for subtable in lookup.SubTable:
                if not hasattr(subtable, "mapping"):
                    continue
                target_substitution = subtable.mapping.get(target_glyph)
                if target_substitution and subtable.mapping.get(source_glyph) != target_substitution:
                    subtable.mapping[source_glyph] = target_substitution
                    added += 1
    return added


def add_continuous_em_dash_feature(font: TTFont) -> dict[str, Any]:
    cmap = font.getBestCmap()
    em_dash = cmap.get(0x2014)
    if not em_dash or em_dash not in font["glyf"].glyphs:
        return {"continuous_em_dash_feature_added": False, "continuous_em_dash_vert_mappings": 0}
    vert_aliases = add_vert_alias(font, 0x2014, 0x2015)

    em_dash_box = glyph_bbox(font, em_dash)
    if not em_dash_box:
        return {
            "continuous_em_dash_feature_added": False,
            "continuous_em_dash_vert_mappings": 0,
            "continuous_em_dash_vert_aliases": vert_aliases,
        }
    x_min, y_min, x_max, y_max = em_dash_box
    if x_min <= 0:
        return {
            "continuous_em_dash_feature_added": False,
            "continuous_em_dash_vert_mappings": 0,
            "continuous_em_dash_vert_aliases": vert_aliases,
        }

    vert_mapping = get_single_substitution_mapping(font, "vert")
    vrt2_mapping = get_single_substitution_mapping(font, "vrt2")
    em_dash_v = vert_mapping.get(em_dash) or vrt2_mapping.get(em_dash) or em_dash
    if em_dash_v not in font["glyf"].glyphs:
        em_dash_v = em_dash
    em_dash_v_box = glyph_bbox(font, em_dash_v)
    if not em_dash_v_box:
        em_dash_v_box = em_dash_box

    advance_width = font["hmtx"].metrics.get(em_dash, (font["head"].unitsPerEm, 0))[0]
    x_min_v, y_min_v, x_max_v, _y_max_v = em_dash_v_box
    advance_height = font["vmtx"].metrics.get(em_dash_v, (font["head"].unitsPerEm, 0))[0] if "vmtx" in font else font["head"].unitsPerEm

    em_dash_cont = em_dash + ".cont"
    em_dash_v_cont = em_dash_v + ".cont"
    half_height = (y_max - y_min) / 2
    add_simple_glyph(
        font,
        em_dash_cont,
        em_dash,
        [
            (x_max - advance_width, y_max),
            (x_max - advance_width - half_height, (y_min + y_max) / 2),
            (x_max - advance_width, y_min),
            (x_max, y_min),
            (x_max, y_max),
        ],
    )
    add_simple_glyph(
        font,
        em_dash_v_cont,
        em_dash_v,
        [
            (x_min_v, y_min_v),
            (x_max_v, y_min_v),
            (x_max_v, y_min_v + advance_height),
            ((x_min_v + x_max_v) / 2, y_min_v + advance_height + (x_max_v - x_min_v) / 2),
            (x_min_v, y_min_v + advance_height),
        ],
    )

    single_sub = ot.SingleSubst()
    single_sub.mapping = {em_dash: em_dash_cont}
    single_lookup = ot.Lookup()
    single_lookup.LookupType = 1
    single_lookup.LookupFlag = 0
    single_lookup.SubTable = [single_sub]
    single_lookup.SubTableCount = 1
    single_index = append_gsub_lookup(font, single_lookup)

    chain = ot.ChainContextSubst()
    chain.Format = 3
    chain.BacktrackGlyphCount = 1
    chain.BacktrackCoverage = [coverage(font, [em_dash, em_dash_cont])]
    chain.InputGlyphCount = 1
    chain.InputCoverage = [coverage(font, [em_dash])]
    chain.LookAheadGlyphCount = 0
    chain.LookAheadCoverage = []
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

    vert_mappings = 0
    for tag in ("vert", "vrt2"):
        if "GSUB" not in font:
            continue
        gsub = font["GSUB"].table
        if not gsub.FeatureList or not gsub.LookupList:
            continue
        for record in gsub.FeatureList.FeatureRecord:
            if record.FeatureTag != tag:
                continue
            for lookup_index in record.Feature.LookupListIndex:
                lookup = gsub.LookupList.Lookup[lookup_index]
                if lookup.LookupType != 1:
                    continue
                for subtable in lookup.SubTable:
                    if hasattr(subtable, "mapping") and em_dash in subtable.mapping:
                        subtable.mapping[em_dash_cont] = em_dash_v_cont
                        vert_mappings += 1

    enable_features_for_all_scripts(font, {"calt", "vert", "vrt2"})
    return {
        "continuous_em_dash_feature_added": True,
        "continuous_em_dash_vert_mappings": vert_mappings,
        "continuous_em_dash_vert_aliases": vert_aliases,
    }


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
    inter = load_inter(italic)
    base, sarasa_report = load_base(italic, set(inter.getBestCmap().keys()))
    reference_fonts: dict[int, TTFont] = {}
    try:
        for weight_name, weight_value in REFERENCE_ADVANCE_STOPS:
            reference_fonts[weight_value] = open_reference_font(weight_name, italic)
        merge_report = append_inter_glyphs(base, inter, unicodes)
        remove_metric_variation_maps(base)
        feature_drop_report = drop_sarasa_width_features(base)
        locl_report = prune_locl_like_reference(base)
        em_dash_report = add_continuous_em_dash_feature(base)
        long_dash_report = remove_vertical_long_dash_ligature_mappings(base)
        source_nonfinal_features_dropped = drop_nonfinal_gsub_features(base, SOURCE_HAN_FINAL_GSUB_FEATURES)
        inter_layout_report = import_inter_layout_features(base, inter)
        digit_report = add_digit_width_features(base, inter)
    finally:
        inter.close()

    subset_to_current_cmap(base)
    colon_report = add_digit_colon_feature(base)
    reference = reference_fonts[400]
    skip_metric_codepoints = set(range(0x30, 0x3A)) | {0x3A}
    try:
        alias_report = split_reference_cmap_aliases(base, reference)
        alias_mapping_report = align_reference_cmap_alias_mappings(base, reference, skip_metric_codepoints)
        profile_report = split_reference_advance_profiles(base, reference_fonts, skip_metric_codepoints)
        lsb_profile_report = split_reference_lsb_profiles(base, reference_fonts, skip_metric_codepoints)
        vmtx_profile_report = split_reference_vmtx_profiles(base, reference_fonts, skip_metric_codepoints)
        advance_report = align_reference_advances(base, reference, skip_metric_codepoints)
        lsb_align_report = align_reference_hmtx_lsb(base, reference, skip_metric_codepoints)
        advance_variation_report = align_reference_advance_variations(base, reference_fonts, skip_metric_codepoints)
        lsb_variation_report = sum_count_reports(
            align_reference_lsb_variations(base, reference_fonts, skip_metric_codepoints),
            align_reference_lsb_variations(base, reference_fonts, skip_metric_codepoints),
        )
        tnum_target_report = align_tnum_digit_targets(base, reference)
        tnum_target_variation_report = align_tnum_digit_target_variations(base, reference_fonts)
        vmtx_report = align_reference_vmtx(base, reference, skip_metric_codepoints)
        vmtx_variation_report = sum_count_reports(
            align_reference_vmtx_variations(base, reference_fonts, skip_metric_codepoints),
            align_reference_vmtx_variations(base, reference_fonts, skip_metric_codepoints),
        )
        subset_to_current_cmap(base)
        empty_feature_report = ensure_empty_gsub_features(base, empty_gsub_features_for_style(italic))
        gsub_template_report = align_layout_feature_template(base, reference, "GSUB")
        gpos_template_report = align_layout_feature_template(base, reference, "GPOS")
        gsub_lookup_report = pad_lookup_list_to_reference_count(base, reference, "GSUB")
        gpos_lookup_report = pad_lookup_list_to_reference_count(base, reference, "GPOS")
        gdef_report = rebuild_gdef_from_reference(base, reference)
        vorg_report = rebuild_vorg_from_reference(base, reference)
        metadata_report = sync_sarasa_metadata_from_reference(base, reference)
    finally:
        for reference_font in reference_fonts.values():
            reference_font.close()
    update_vf_names(base, italic)
    update_fvar_instances(base, italic)
    update_style_flags(base, italic)
    update_os2_sarasa_metadata(base)
    rebuild_stat(base, italic)
    extra_table_report = drop_generated_extra_tables(base, keep_stat=True)
    if "DSIG" in base:
        del base["DSIG"]

    VARIABLE_DIR.mkdir(parents=True, exist_ok=True)
    out_name = "Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf" if italic else "Sarasa-Ui-VF-PropDigits-SC[wght].ttf"
    out_path = VARIABLE_DIR / out_name
    base.save(out_path, reorderTables=True)
    base.close()

    base = TTFont(out_path)
    reference_fonts_roundtrip: dict[int, TTFont] = {}
    try:
        for weight_name, weight_value in REFERENCE_ADVANCE_STOPS:
            reference_fonts_roundtrip[weight_value] = open_reference_font(weight_name, italic)
        roundtrip_lsb_variation_report = prefix_count_report(
            align_reference_lsb_variations(base, reference_fonts_roundtrip, skip_metric_codepoints),
            "roundtrip_",
        )
        roundtrip_vmtx_variation_report = prefix_count_report(
            align_reference_vmtx_variations(base, reference_fonts_roundtrip, skip_metric_codepoints),
            "roundtrip_",
        )
        roundtrip_tnum_target_variation_report = prefix_count_report(
            align_tnum_digit_target_variations(base, reference_fonts_roundtrip),
            "roundtrip_",
        )
        base.save(out_path, reorderTables=True)
    finally:
        for reference_font in reference_fonts_roundtrip.values():
            reference_font.close()

    cmap = base.getBestCmap()
    widths = {f"U+{cp:04X}": base["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A)}
    key_widths = {
        f"U+{cp:04X}": base["hmtx"].metrics[cmap[cp]][0]
        for cp in [0x00B7, 0x2018, 0x2019, 0x201C, 0x201D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2025, 0x2026, 0x22EF, 0x2E3A, 0x2E3B, 0x31B4, 0x3131, 0xAC00, 0x1100]
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
        **locl_report,
        **em_dash_report,
        **long_dash_report,
        "nonfinal_gsub_features_dropped": source_nonfinal_features_dropped,
        **inter_layout_report,
        **alias_report,
        **alias_mapping_report,
        **profile_report,
        **lsb_profile_report,
        **vmtx_profile_report,
        **advance_report,
        **lsb_align_report,
        **advance_variation_report,
        **lsb_variation_report,
        **tnum_target_report,
        **tnum_target_variation_report,
        **vmtx_report,
        **vmtx_variation_report,
        **roundtrip_lsb_variation_report,
        **roundtrip_vmtx_variation_report,
        **roundtrip_tnum_target_variation_report,
        **empty_feature_report,
        **gsub_template_report,
        **gpos_template_report,
        **gsub_lookup_report,
        **gpos_lookup_report,
        **gdef_report,
        **vorg_report,
        **metadata_report,
        **extra_table_report,
        **colon_report,
    }


def remove_variable_tables(font: TTFont) -> None:
    for tag in ("fvar", "gvar", "avar", "HVAR", "VVAR", "MVAR", "STAT", "BASE"):
        if tag in font:
            del font[tag]


def hint_static_font(in_path: Path, out_path: Path) -> dict[str, Any]:
    if os.environ.get("SARASA_SKIP_TTFAUTOHINT") == "1":
        shutil.copy2(in_path, out_path)
        return {"hinted": False, "hint_tool": "skipped"}
    exe = os.environ.get("TTFAUTOHINT")
    if exe:
        result = subprocess.run([exe, str(in_path), str(out_path)], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return {"hinted": True, "hint_tool": exe}
    try:
        import ttfautohint

        result = ttfautohint.run([str(in_path), str(out_path)], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return {"hinted": True, "hint_tool": "ttfautohint-py"}
    except ImportError:
        shutil.copy2(in_path, out_path)
        return {"hinted": False, "hint_tool": "missing"}


def static_output_name(weight_name: str, italic: bool) -> str:
    if weight_name == "Regular":
        return "SarasaUiPropDigitsSC-Italic.ttf" if italic else "SarasaUiPropDigitsSC-Regular.ttf"
    return f"SarasaUiPropDigitsSC-{weight_name}{'Italic' if italic else ''}.ttf"


def postprocess_static_font(path: Path, weight_name: str, weight_value: int, italic: bool) -> dict[str, Any]:
    font = TTFont(path)
    report: dict[str, Any] = {}
    try:
        reference_path = reference_font_path(weight_name, italic)
        if reference_path.exists():
            reference = TTFont(reference_path)
            try:
                report.update(align_reference_hmtx_lsb(font, reference, set(range(0x30, 0x3A)) | {0x3A}))
                report.update(align_tnum_digit_targets(font, reference))
                report.update(align_reference_vmtx(font, reference, set(range(0x30, 0x3A)) | {0x3A}))
                report.update(rebuild_gdef_from_reference(font, reference))
                report.update(rebuild_vorg_from_reference(font, reference))
                report.update(sync_sarasa_metadata_from_reference(font, reference))
            finally:
                reference.close()
        update_static_names(font, weight_name, weight_value, italic)
        update_os2_sarasa_metadata(font)
        rebuild_static_stat(font, weight_name, weight_value, italic)
        report.update(drop_generated_extra_tables(font, keep_stat=True))
        if "DSIG" in font:
            del font["DSIG"]
        font.save(path, reorderTables=True)
    finally:
        font.close()
    return report


def build_static_fonts() -> list[dict[str, Any]]:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", STATIC_DIR / "LICENSE-Sarasa-Gothic.txt")
    for path in STATIC_DIR.glob("SarasaUiPropDigitsSC-*.ttf"):
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
                rebuild_static_stat(font, weight_name, weight_value, italic)
                sync_hmtx_lsb_to_glyph_bounds(font)
                if "DSIG" in font:
                    del font["DSIG"]
                tmp_path = tmp_dir / static_output_name(weight_name, italic)
                final_path = STATIC_DIR / static_output_name(weight_name, italic)
                font.save(tmp_path, reorderTables=True)
                font.close()
                hint_report = hint_static_font(tmp_path, final_path)
                postprocess_report = postprocess_static_font(final_path, weight_name, weight_value, italic)
                outputs.append(
                    {
                        "file": str(final_path.relative_to(ROOT)),
                        "weight": weight_name,
                        "wght": weight_value,
                        "italic": italic,
                        **hint_report,
                        **postprocess_report,
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


def layout_table_summary(font: TTFont, table_tag: str) -> dict[str, Any]:
    if table_tag not in font:
        return {"present": False}
    table = font[table_tag].table
    feature_records = table.FeatureList.FeatureRecord if table.FeatureList else []
    lookup_count = len(table.LookupList.Lookup) if table.LookupList else 0
    scripts = []
    langsys_count = 0
    if table.ScriptList:
        for script_record in table.ScriptList.ScriptRecord:
            langs = [record.LangSysTag for record in script_record.Script.LangSysRecord]
            langsys_count += len(langs)
            if script_record.Script.DefaultLangSys:
                langsys_count += 1
            scripts.append(
                {
                    "tag": script_record.ScriptTag,
                    "has_default": script_record.Script.DefaultLangSys is not None,
                    "langs": langs,
                }
            )
    return {
        "present": True,
        "feature_records": len(feature_records),
        "unique_features": sorted({record.FeatureTag for record in feature_records}),
        "lookups": lookup_count,
        "scripts": scripts,
        "langsys": langsys_count,
    }


def lsb_mismatch_count(font: TTFont) -> int | None:
    if "hmtx" not in font or "glyf" not in font:
        return None
    mismatches = 0
    for glyph_name, (_advance_width, lsb) in font["hmtx"].metrics.items():
        if glyph_name not in font["glyf"].glyphs:
            continue
        if glyph_x_min(font, glyph_name, lsb) != lsb:
            mismatches += 1
    return mismatches


def inspect_font(path: Path) -> dict[str, Any]:
    font = TTFont(path)
    try:
        cmap = font.getBestCmap()
        digits = [font["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A) if cp in cmap]
        key_cps = [0x00B7, 0x2018, 0x2019, 0x201C, 0x201D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2025, 0x2026, 0x22EF, 0x2E3A, 0x2E3B, 0x31B4, 0x3131, 0xAC00, 0x1100]
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
            "tables": {
                "BASE": "BASE" in font,
                "GDEF": "GDEF" in font,
                "STAT": "STAT" in font,
                "VORG": "VORG" in font,
                "fvar": "fvar" in font,
                "gvar": "gvar" in font,
            },
            "layout": {
                "GSUB": layout_table_summary(font, "GSUB"),
                "GPOS": layout_table_summary(font, "GPOS"),
            },
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
    text = """Sarasa Ui PropDigits SC TTF 1.0.39

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
They keep a static STAT table for modern weight/italic style recognition; this
does not make the static TTFs variable fonts.
GSUB/GPOS FeatureRecord order, Script/LangSys coverage, and lookup counts are
templated from the corresponding upstream Sarasa Ui SC static font for each
style.
Glyph counts are not padded to match upstream; cmap glyphs and layout-reachable
unencoded glyphs are preserved, while unreachable glyph count differences are
left as build artifacts.
These fonts are modified derivatives and are not official Sarasa Gothic,
Source Han Sans, or Inter releases.
"""
    (STATIC_DIR / "README.txt").write_text(text, encoding="utf-8")


def write_reports(build_report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    build_text = json.dumps(build_report, ensure_ascii=False, indent=2)
    (REPORT_DIR / "Sarasa-Ui-VF-PropDigits-SC-report.json").write_text(build_text, encoding="utf-8")

    font_paths = sorted(VARIABLE_DIR.glob("*.ttf")) + sorted(STATIC_DIR.glob("SarasaUiPropDigitsSC-*.ttf"))
    inspection = {
        "title": "Sarasa Ui VF PropDigits SC / Sarasa Ui PropDigits SC font inspection",
        "note": "Generated by tools/build_sarasa_ui_sc_true_vf.py using fontTools.",
        "fonts": [inspect_font(path) for path in font_paths],
    }
    (REPORT_DIR / "font-inspection.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")


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
            "Source Han Sans SC VF and Inter VF are merged through Sarasa pass1-style "
            "codepoint ownership with VF-availability fallback: Inter VF is baked with "
            "Sarasa's Inter settings (ss03 and cv10) for Latin and western-symbol "
            "coverage, while Source Han Sans SC VF is preferred for CJK, Korean, "
            "Jamo, and localized Sarasa Ui punctuation when that VF source covers the "
            "codepoint. Source Han pwid/symbol sanitization and Hangul full-width "
            "normalization are applied before Inter glyphs are appended. The final "
            "layout imports the Inter VF GSUB/GPOS features that Sarasa UI SC exposes, "
            "keeps Sarasa's empty cv01-cv13/ss01-ss08 tags, preserves cv14, ccmp, "
            "locl pruned to upstream Sarasa UI coverage, Hangul Jamo features, "
            "vert/vrt2, tnum/pnum, continuous em dash, and the digit-colon calt rule. "
            "Reference Sarasa UI SC cmap alias splits and alias mappings, GSUB/GPOS "
            "FeatureRecord order, Script/LangSys coverage, lookup counts, non-digit "
            "advances and LSB values across the weight axis, tnum digit target hmtx, "
            "vertical metrics, vmtx defaults and variations, GDEF, VORG, and "
            "Sarasa-compatible head/OS/2 metadata are aligned after the merge. "
            "VF and static outputs both include STAT; static STAT only describes the "
            "single instance's style and does not preserve variable fvar/gvar tables. "
            "Glyph counts are not padded to match upstream: cmap glyphs and "
            "layout-reachable unencoded glyphs are kept, while unreachable glyph count "
            "differences are not treated as rendering defects. "
            "Static TTF outputs use the same final ttfautohint input/output invocation "
            "shape as upstream Sarasa."
        ),
        "intentional_differences_from_upstream_sarasa_ui": [
            "Default ASCII digits are proportional; tnum restores tabular digits.",
            "Weight instances follow Source Han Sans stops: 250, 300, 350, 400, 500, 700, 900.",
            "A contextual calt rule raises colon only between digits.",
        ],
        "final_gsub_features": sorted(FINAL_GSUB_FEATURES),
        "variable_outputs": variable_outputs,
        "static_outputs": static_outputs,
    }
    write_reports(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    report = build_all()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
