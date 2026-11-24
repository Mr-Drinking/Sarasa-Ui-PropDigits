"""规格修正模块：每个函数对应一条明确审计条目，保持幂等且无全局状态。"""
from __future__ import annotations

from typing import Any

from fontTools.otlLib.maxContextCalc import maxCtxFont
from fontTools.ttLib import TTFont

from ..spec import is_ideograph
from .base import center_to_width, set_advance_width, shift_glyph_x
from .layout_utils import substitution_closure


def _has_horizontal_advance_variation(font: TTFont, glyph_name: str) -> bool:
    if "gvar" not in font:
        return False
    for var in font["gvar"].variations.get(glyph_name, []):
        if len(var.coordinates) >= 4:
            adv = var.coordinates[-3]
            if adv is not None and adv != (0, 0):
                return True
    return False


def _freeze_horizontal_advance_variation(font: TTFont, glyph_name: str) -> None:
    if "gvar" not in font:
        return
    for var in font["gvar"].variations.get(glyph_name, []):
        if len(var.coordinates) >= 4:
            for idx in (-4, -3):
                if var.coordinates[idx] is not None:
                    var.coordinates[idx] = (0, 0)


def fix_classical_ideograph_fullwidth(font: TTFont, region: str) -> dict[str, int]:
    """修 A5：CL 地区所有编码汉字各字重步进固定为全宽 1000（upem），墨迹居中并冻结水平步进变化。"""
    if region != "CL":
        return {"a5_cl_ideographs_fixed": 0}
    upem = int(font["head"].unitsPerEm)
    cmap = font.getBestCmap() or {}
    targets = sorted({gname for cp, gname in cmap.items() if is_ideograph(cp)})
    fixed = 0
    for gname in targets:
        adv = font["hmtx"].metrics.get(gname, (upem, 0))[0]
        has_var = _has_horizontal_advance_variation(font, gname)
        if adv != upem or has_var:
            if adv != upem:
                shift_glyph_x(font, gname, (upem - adv) / 2)
                set_advance_width(font, gname, upem)
            _freeze_horizontal_advance_variation(font, gname)
            fixed += 1
    return {"a5_cl_ideographs_fixed": fixed}


def fix_ideograph_gdef_classes(font: TTFont) -> dict[str, int]:
    """修正 CL 㪉㪘㪣㪰 等汉字在 GDEF 中误标为 mark(3) 导致的零步进问题。"""
    if "GDEF" not in font or not getattr(font["GDEF"].table, "GlyphClassDef", None):
        return {"ideograph_gdef_marks_fixed": 0}
    cmap = font.getBestCmap() or {}
    seeds = {gname for cp, gname in cmap.items() if is_ideograph(cp)}
    reachable = substitution_closure(font, seeds)
    class_defs = font["GDEF"].table.GlyphClassDef.classDefs
    fixed = 0
    for gname in reachable:
        if class_defs.get(gname) == 3:
            class_defs[gname] = 1
            fixed += 1
    return {"ideograph_gdef_marks_fixed": fixed}


def fix_os2_unicode_range_bit57(font: TTFont) -> dict[str, bool]:
    """修 A18：当 cmap 包含非 BMP 码位（> U+FFFF）时，将 OS/2 ulUnicodeRange bit 57（Non-Plane 0）置位。"""
    cmap = font.getBestCmap() or {}
    has_non_bmp = any(cp > 0xFFFF for cp in cmap)
    os2 = font["OS/2"]
    bit_mask = 1 << (57 - 32)
    before = bool(os2.ulUnicodeRange2 & bit_mask)
    if has_non_bmp:
        os2.ulUnicodeRange2 |= bit_mask
    else:
        os2.ulUnicodeRange2 &= ~bit_mask
    after = bool(os2.ulUnicodeRange2 & bit_mask)
    return {"os2_bit57_before": before, "os2_bit57_after": after}


def recalc_os2_max_context(font: TTFont) -> dict[str, int]:
    """按最终 GSUB/GPOS 表重算 OS/2.usMaxContext。"""
    before = int(font["OS/2"].usMaxContext)
    after = int(maxCtxFont(font))
    font["OS/2"].usMaxContext = after
    return {"os2_max_context_before": before, "os2_max_context_after": after}


def _collect_ot_glyphs(obj: Any, valid: set[str], out: set[str], seen: set[int]) -> None:
    if obj is None or isinstance(obj, (int, float, bool, bytes)):
        return
    if isinstance(obj, str):
        if obj in valid:
            out.add(obj)
        return
    oid = id(obj)
    if oid in seen:
        return
    seen.add(oid)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k in valid:
                out.add(k)
            _collect_ot_glyphs(v, valid, out, seen)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_ot_glyphs(v, valid, out, seen)
    elif hasattr(obj, "__dict__"):
        for k, v in vars(obj).items():
            if k.lower().endswith("tag") or k in {"reader", "font"}:
                continue
            _collect_ot_glyphs(v, valid, out, seen)


def reachable_glyphs(font: TTFont) -> set[str]:
    """计算完整可达字形集合：.notdef + 普通 cmap + cmap 14 UVS 目标 + GSUB/GPOS 引用 + 复合字形组件闭包。"""
    order = font.getGlyphOrder()
    valid = set(order)
    keep: set[str] = {".notdef"} if ".notdef" in valid else set()

    if "cmap" in font:
        for tbl in font["cmap"].tables:
            if tbl.format == 14:
                for entries in tbl.uvsDict.values():
                    for _cp, gname in entries:
                        if gname and gname in valid:
                            keep.add(gname)
            else:
                for gname in tbl.cmap.values():
                    if gname in valid:
                        keep.add(gname)

    # 先对 GSUB 做替换闭包
    keep = substitution_closure(font, keep)

    # 收集 GPOS/GSUB 中所有仍被活规则引用的字形（保守留存）
    seen: set[int] = set()
    for tag in ("GSUB", "GPOS"):
        if tag in font:
            _collect_ot_glyphs(font[tag].table, valid, keep, seen)

    # 复合字形组件闭包
    if "glyf" in font:
        glyf = font["glyf"]
        pending = list(keep)
        while pending:
            gname = pending.pop()
            if gname not in glyf.glyphs:
                continue
            g = glyf[gname]
            if g.isComposite():
                for comp in getattr(g, "components", []):
                    if comp.glyphName in valid and comp.glyphName not in keep:
                        keep.add(comp.glyphName)
                        pending.append(comp.glyphName)
    return keep


def prune_unreachable_glyphs(font: TTFont) -> dict[str, int]:
    """清理彻底不可达的孤立字形，同时严格保护 cmap 14 UVS 与 GDEF/glyf/hmtx/vmtx/gvar 一致性。"""
    order = font.getGlyphOrder()
    keep = reachable_glyphs(font)
    removable = [g for g in order if g not in keep]
    if not removable:
        return {"unreachable_glyphs_removed": 0}
    rem_set = set(removable)
    new_order = [g for g in order if g not in rem_set]
    # 必须先解包并只保留 keep 中的 gvar 条目，再修改 glyf 与 glyphOrder（防 LazyDict 读取已删 glyf 崩溃）
    for tag in ("glyf", "hmtx", "vmtx", "gvar", "GDEF"):
        if tag in font:
            _ = font[tag]
    if "gvar" in font:
        gvars = font["gvar"].variations
        font["gvar"].variations = {g: gvars[g] for g in new_order if g in gvars}
    if "glyf" in font:
        for g in removable:
            font["glyf"].glyphs.pop(g, None)
    for tag in ("hmtx", "vmtx"):
        if tag in font:
            for g in removable:
                font[tag].metrics.pop(g, None)
    if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None):
        class_defs = font["GDEF"].table.GlyphClassDef.classDefs
        for g in removable:
            class_defs.pop(g, None)
    font.setGlyphOrder(new_order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(new_order)
    return {"unreachable_glyphs_removed": len(removable)}
