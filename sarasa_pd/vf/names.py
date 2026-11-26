"""VF（及静态共用）的命名、fvar 实例、STAT、OS/2、行高度量、光栅控制与确定性 head 元数据。"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from fontTools.misc.fixedTools import otRound
from fontTools.misc.timeTools import epoch_diff
from fontTools.otlLib.builder import buildStatTable
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.ttLib.tables.ttProgram import Program

from ..env import head_modified_timestamp
from ..spec import Spec, load_spec

LOCALIZED_FAMILY_NAME_IDS = frozenset({1, 2, 4, 16, 17})


def set_name_record(font: TTFont, name_id: int, value: str) -> None:
    name_table = font["name"]
    existing = [r for r in name_table.names if r.nameID == name_id]
    if existing:
        for rec in existing:
            name_table.setName(value, name_id, rec.platformID, rec.platEncID, rec.langID)
    else:
        name_table.setName(value, name_id, 3, 1, 0x0409)


def remove_mac_name_records(font: TTFont) -> dict[str, int]:
    if "name" not in font:
        return {"mac_name_records_removed": 0}
    before = len(font["name"].names)
    font["name"].names = [r for r in font["name"].names if r.platformID != 1]
    return {"mac_name_records_removed": before - len(font["name"].names)}


def update_project_legal_names(font: TTFont, region: str, spec: Spec) -> None:
    set_name_record(font, 0, spec.project_copyright(region))
    set_name_record(font, 13, spec.license_description)
    set_name_record(font, 14, spec.license_url)
    font["name"].names = [
        r for r in font["name"].names if r.nameID not in spec.source_only_legal_name_ids
    ]


def drop_foreign_family_names(font: TTFont, region: str, spec: Spec) -> dict[str, int]:
    keep_langs = {0x0409, spec.region(region).local_lang_id}
    before = len(font["name"].names)
    font["name"].names = [
        r
        for r in font["name"].names
        if r.nameID not in LOCALIZED_FAMILY_NAME_IDS or r.platformID != 3 or r.langID in keep_langs
    ]
    return {"foreign_family_names_removed": before - len(font["name"].names)}


def update_fvar_instances(font: TTFont, region: str, italic: bool, spec: Spec) -> None:
    name_table = font["name"]
    ps_family = spec.vf_ps_family(region)
    instances: list[NamedInstance] = []
    for w in spec.weights:
        sub = ("Italic" if italic else "Regular") if w.name == "Regular" else w.name + (" Italic" if italic else "")
        ps = (
            (f"{ps_family}-Italic" if italic else ps_family)
            if w.name == "Regular"
            else f"{ps_family}-{w.name}{'Italic' if italic else ''}"
        )
        existing = next(
            (
                item
                for item in font["fvar"].instances
                if item.coordinates == {"wght": float(w.value)}
                and item.flags == 0
                and name_table.getDebugName(item.subfamilyNameID) == sub
                and name_table.getDebugName(item.postscriptNameID) == ps
            ),
            None,
        )
        if existing is not None:
            instances.append(existing)
            continue
        inst = NamedInstance()
        inst.coordinates = {"wght": float(w.value)}
        inst.flags = 0
        inst.subfamilyNameID = name_table.addName(sub, platforms=((3, 1, 0x0409),))
        inst.postscriptNameID = name_table.addName(ps, platforms=((3, 1, 0x0409),))
        instances.append(inst)
    font["fvar"].instances = instances


def update_vf_names(font: TTFont, region: str, italic: bool, spec: Spec | None = None) -> dict[str, Any]:
    spec = spec or load_spec()
    r = spec.region(region)
    sub = "Italic" if italic else "Regular"
    family = spec.vf_family(r.code)
    family_local = spec.vf_family_local(r.code)
    ps_family = spec.vf_ps_family(r.code)
    full = f"{family} Italic" if italic else family
    full_local = f"{family_local} Italic" if italic else family_local
    ps = f"{ps_family}-Italic" if italic else ps_family
    var_ps_prefix = "".join(ch for ch in ps if ch.isascii() and ch.isalnum())

    src_label = Path(r.source_han_vf).stem
    if r.classical and r.classical_vf:
        src_label += f" + {Path(r.classical_vf).stem}"
    ver_str = f"Version {spec.opentype_version()}; project {spec.version}; {src_label} + Inter VF; PD"

    for nid, val in (
        (1, family),
        (2, sub),
        (4, full),
        (5, ver_str),
        (6, ps),
        (16, family),
        (17, sub),
        (25, var_ps_prefix),
    ):
        set_name_record(font, nid, val)
    # nameID 3 唯一标识符
    font["name"].setName(f"{ps};{spec.version}", 3, 3, 1, 0x0409)
    update_project_legal_names(font, r.code, spec)
    for nid, val in ((1, family_local), (2, sub), (4, full_local), (16, family_local), (17, sub)):
        font["name"].setName(val, nid, 3, 1, r.local_lang_id)
    if "fvar" in font:
        update_fvar_instances(font, r.code, italic, spec)
    removed_mac = remove_mac_name_records(font)
    removed_foreign = drop_foreign_family_names(font, r.code, spec)
    return {"vf_family": family, "vf_version_string": ver_str, **removed_mac, **removed_foreign}


def rebuild_vf_stat(font: TTFont, italic: bool, spec: Spec | None = None) -> None:
    spec = spec or load_spec()
    weight_values = [
        {
            "nominalValue": w.value,
            "rangeMinValue": w.range_min,
            "rangeMaxValue": w.range_max,
            "name": w.name,
            "flags": w.flags,
        }
        for w in spec.weights
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
                    **({"linkedValue": 1} if not italic else {}),
                    "flags": 0x2 if not italic else 0,
                }
            ],
        },
    ]
    buildStatTable(font, axes)
    remove_mac_name_records(font)


def apply_vertical_metrics(font: TTFont, spec: Spec) -> None:
    vm = spec.vertical_metrics
    hhea = font["hhea"]
    os2 = font["OS/2"]
    hhea.ascent = vm["hhea_ascent"]
    hhea.descent = vm["hhea_descent"]
    hhea.lineGap = vm["hhea_line_gap"]
    os2.sTypoAscender = vm["typo_ascent"]
    os2.sTypoDescender = vm["typo_descent"]
    os2.sTypoLineGap = vm["typo_line_gap"]
    os2.usWinAscent = vm["win_ascent"]
    os2.usWinDescent = vm["win_descent"]


def update_caret_slope(font: TTFont) -> None:
    hhea = font["hhea"]
    angle = float(font["post"].italicAngle)
    hhea.caretSlopeRise = 1000
    if angle:
        hhea.caretSlopeRun = otRound(math.tan(math.radians(-angle)) * hhea.caretSlopeRise)
    else:
        hhea.caretSlopeRun = 0
    hhea.caretOffset = 0


def update_vf_style_and_os2(font: TTFont, region: str, italic: bool, spec: Spec | None = None) -> None:
    spec = spec or load_spec()
    r = spec.region(region)
    os2 = font["OS/2"]
    os2.version = max(int(os2.version), 4)
    os2.usWeightClass = 400
    os2.achVendID = spec.vendor_id
    os2.ulCodePageRange1 = r.codepage_range1
    os2.ulCodePageRange2 = 0
    os2.panose.bWeight = 1 + int(os2.usWeightClass) // 100
    reg_w = spec.weight("Regular")
    os2.yStrikeoutSize = reg_w.strikeout_size

    os2.fsSelection |= 1 << 7  # USE_TYPO_METRICS
    os2.fsSelection &= ~((1 << 0) | (1 << 5) | (1 << 6))
    font["head"].macStyle &= ~0b11
    if italic:
        font["head"].macStyle |= 0b10
        os2.fsSelection |= 1 << 0
        font["post"].italicAngle = spec.italic_angle
    else:
        os2.fsSelection |= 1 << 6
        font["post"].italicAngle = 0.0

    apply_vertical_metrics(font, spec)
    update_caret_slope(font)


def sync_sarasa_os2_fields(font: TTFont, reference: TTFont) -> None:
    """从上游 Sarasa Ui 同风格 Regular 只读参照同步固定排版字段（ulUnicodeRange1-4、xAvgCharWidth、上下标、删除线位置、xHeight/CapHeight、PANOSE、lowestRecPPEM）。"""
    os2, ref = font["OS/2"], reference["OS/2"]
    for field in (
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
        "yStrikeoutPosition",
        "sFamilyClass",
        "ulUnicodeRange1",
        "ulUnicodeRange2",
        "ulUnicodeRange3",
        "ulUnicodeRange4",
        "sxHeight",
        "sCapHeight",
        "usDefaultChar",
        "usBreakChar",
    ):
        if hasattr(ref, field):
            setattr(os2, field, getattr(ref, field))
    if hasattr(ref, "panose"):
        for pf in (
            "bFamilyType",
            "bSerifStyle",
            "bProportion",
            "bContrast",
            "bStrokeVariation",
            "bArmStyle",
            "bLetterForm",
            "bMidline",
            "bXHeight",
        ):
            setattr(os2.panose, pf, getattr(ref.panose, pf))
    if "head" in font and "head" in reference:
        font["head"].lowestRecPPEM = int(reference["head"].lowestRecPPEM)


def normalize_nonhinting_raster_metadata(font: TTFont, spec: Spec | None = None) -> dict[str, Any]:
    """为 VF 与 unhinted 静态设置 smart dropout gasp/prep、head.flags=3（清除 bit 2/3）并复位 maxp；断言无逐字形指令。"""
    spec = spec or load_spec()
    for g in font["glyf"].glyphs.values():
        g.expand(font["glyf"])
        if hasattr(g, "program") and g.program.getBytecode():
            raise ValueError("字体含有逐字形 hint，不能按 nonhinting 覆盖 prep/gasp")
    for tag in ("fpgm", "cvt "):
        if tag in font:
            del font[tag]
    font["gasp"] = newTable("gasp")
    font["gasp"].version = 1
    font["gasp"].gaspRange = {0xFFFF: spec.gasp_range_all}
    font["prep"] = newTable("prep")
    font["prep"].program = Program()
    font["prep"].program.fromBytecode(spec.smart_dropout_prep)
    # 无逐字形指令字体：置位 bit 0（基线 y=0）与 bit 1（lsb 在 x=0），清除 bit 2 与 bit 3（不强制 ppem 取整）
    font["head"].flags = (int(font["head"].flags) & ~((1 << 2) | (1 << 3))) | (1 << 0) | (1 << 1)
    maxp = font["maxp"]
    maxp.maxZones = 1
    maxp.maxTwilightPoints = 0
    maxp.maxStorage = 0
    maxp.maxFunctionDefs = 0
    maxp.maxInstructionDefs = 0
    maxp.maxStackElements = 1
    maxp.maxSizeOfInstructions = 0
    return {"nonhinting_gasp_all": spec.gasp_range_all, "nonhinting_prep": spec.smart_dropout_prep.hex()}


def _referenced_name_ids(font: TTFont) -> set[int]:
    referenced: set[int] = set()
    visited: set[int] = set()

    def visit(val: Any) -> None:
        if val is None or isinstance(val, (str, bytes, int, float, bool)) or id(val) in visited:
            return
        visited.add(id(val))
        if isinstance(val, (list, tuple)):
            for item in val:
                visit(item)
            return
        fields = val if isinstance(val, dict) else getattr(val, "__dict__", {})
        for k, item in fields.items():
            if str(k).endswith("NameID") and isinstance(item, int) and 0 < item < 0xFFFF:
                referenced.add(item)
            visit(item)
        start = fields.get("FirstParamUILabelNameID", 0)
        count = fields.get("NumNamedParameters", 0)
        if count and isinstance(start, int) and isinstance(count, int) and 256 <= start:
            referenced.update(range(start, start + count))

    for tag in ("fvar", "STAT", "GSUB", "GPOS"):
        if tag in font:
            tbl = font[tag]
            if hasattr(tbl, "ensureDecompiled"):
                tbl.ensureDecompiled()
            visit(tbl)
    return referenced


def prune_unreferenced_variable_names(font: TTFont) -> dict[str, int]:
    referenced = _referenced_name_ids(font)
    removed = {r.nameID for r in font["name"].names if r.nameID >= 256 and r.nameID not in referenced}
    font["name"].names = [r for r in font["name"].names if r.nameID not in removed]
    return {"unreferenced_name_ids_removed": len(removed)}


def apply_deterministic_head(font: TTFont, spec: Spec | None = None) -> dict[str, Any]:
    """设置确定性 head.fontRevision、head.lowestRecPPEM 与 head.modified，清除 DSIG/BASE。"""
    spec = spec or load_spec()
    for tag in ("DSIG", "BASE"):
        if tag in font:
            del font[tag]
    font["head"].fontRevision = spec.font_revision()
    font["head"].lowestRecPPEM = spec.lowest_rec_ppem
    unix_ts = head_modified_timestamp(spec.head_modified_timestamp)
    # fontTools.misc.timeTools.epoch_diff 为 1904 - 1970 = -2082844800，故减之得 Mac/OpenType 时间戳
    font["head"].modified = unix_ts - epoch_diff
    return {"head_font_revision": spec.font_revision(), "head_modified_unix": unix_ts}
