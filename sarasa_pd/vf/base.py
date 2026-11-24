"""VF 基底准备：Source Han VF 限轴、CL 尚古覆盖、公开轴、保留 UVS 子集化、pwid 规整、韩文归一与斜体剪切。"""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables import otTables as ot
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap, supportScalar

from ..spec import Spec, is_ideograph, is_korean, load_spec, use_inter_codepoint
from ..subset import subset_font_preserving_uvs
from .layout_utils import inherit_glyph_aliases, substitution_closure


def glyph_to_unicodes(font: TTFont) -> dict[str, set[int]]:
    rev: dict[str, set[int]] = {}
    for cp, gname in (font.getBestCmap() or {}).items():
        rev.setdefault(gname, set()).add(cp)
    return rev


def _unicode_cmap_tables(font: TTFont):
    """返回普通 Unicode cmap 子表（显式排除 format 14 UVS 子表）。"""
    return [t for t in font["cmap"].tables if t.isUnicode() and t.format != 14]


def _language_system_tags(font: TTFont) -> dict[str, dict[str, set[str]]]:
    saved: dict[str, dict[str, set[str]]] = {}
    for tag in ("GSUB", "GPOS"):
        if tag not in font or not font[tag].table.ScriptList:
            continue
        by_script: dict[str, set[str]] = {}
        for srec in font[tag].table.ScriptList.ScriptRecord:
            langs = {lrec.LangSysTag for lrec in srec.Script.LangSysRecord}
            if langs:
                by_script[srec.ScriptTag] = langs
        if by_script:
            saved[tag] = by_script
    return saved


def _restore_language_systems(font: TTFont, saved: dict[str, dict[str, set[str]]]) -> None:
    """补回 fontTools Subsetter 删除的冗余语言系统，保持按 (script, lang) 精确合并语义。"""
    for tag, by_script in saved.items():
        if tag not in font or not font[tag].table.ScriptList:
            continue
        for srec in font[tag].table.ScriptList.ScriptRecord:
            expected = by_script.get(srec.ScriptTag)
            if not expected or srec.Script.DefaultLangSys is None:
                continue
            present = {lrec.LangSysTag for lrec in srec.Script.LangSysRecord}
            missing = sorted(expected - present)
            if not missing:
                continue
            for ltag in missing:
                rec = ot.LangSysRecord()
                rec.LangSysTag = ltag
                rec.LangSys = copy.deepcopy(srec.Script.DefaultLangSys)
                srec.Script.LangSysRecord.append(rec)
            srec.Script.LangSysRecord.sort(key=lambda r: r.LangSysTag)
            srec.Script.LangSysCount = len(srec.Script.LangSysRecord)


def subset_preserving_routes_and_uvs(font: TTFont, unicodes: set[int]) -> None:
    saved = _language_system_tags(font)
    subset_font_preserving_uvs(font, unicodes)
    _restore_language_systems(font, saved)


def weight_axis(font: TTFont):
    return next(a for a in font["fvar"].axes if a.axisTag == "wght")


def normalize_axis(value: float, lo: float, default: float, hi: float) -> float:
    if value == default:
        return 0.0
    return (value - default) / (default - lo) if value < default else (value - default) / (hi - default)


def normalized_wght(font: TTFont, value: float) -> float:
    axis = weight_axis(font)
    norm = normalize_axis(value, axis.minValue, axis.defaultValue, axis.maxValue)
    if "avar" in font and "wght" in font["avar"].segments:
        norm = piecewiseLinearMap(norm, font["avar"].segments["wght"])
    return floatToFixedToFloat(norm, 14)


def apply_public_weight_axis(font: TTFont, spec: Spec) -> dict[str, Any]:
    """将 Source Han 内部轴 (250–900) 重映射到公开轴 (200–900，含 350 锚点)。"""
    axis = weight_axis(font)
    src_seg = dict(font["avar"].segments.get("wght", {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0})) if "avar" in font else {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    pub_min, pub_def, pub_max = spec.public_limit
    segment: dict[float, float] = {}
    for pub_w, src_w in spec.public_to_internal_wght.items():
        pub_n = normalize_axis(pub_w, pub_min, pub_def, pub_max)
        src_n = normalize_axis(src_w, axis.minValue, axis.defaultValue, axis.maxValue)
        segment[pub_n] = piecewiseLinearMap(src_n, src_seg)
    segment = dict(sorted(segment.items()))
    axis.minValue, axis.defaultValue, axis.maxValue = pub_min, pub_def, pub_max
    if "avar" not in font:
        font["avar"] = newTable("avar")
        font["avar"].segments = {}
    font["avar"].segments["wght"] = segment
    return {"public_wght_axis": list(spec.public_limit), "public_avar_anchors": sorted(spec.public_to_internal_wght)}


def clone_glyph(font: TTFont, src_name: str, preferred: str, pending_aliases: dict[str, set[str]] | None = None) -> str:
    order = font.getGlyphOrder()
    existing = set(order) | set(font["glyf"].glyphs)
    base_name = preferred if preferred and preferred not in {".notdef", src_name} else f"{src_name}.clone"
    new_name = base_name
    suffix = 1
    while new_name in existing:
        suffix += 1
        new_name = f"{base_name}.{suffix}"
    font["glyf"].glyphs[new_name] = copy.deepcopy(font["glyf"][src_name])
    font["hmtx"].metrics[new_name] = copy.deepcopy(font["hmtx"].metrics[src_name])
    if "vmtx" in font and src_name in font["vmtx"].metrics:
        font["vmtx"].metrics[new_name] = copy.deepcopy(font["vmtx"].metrics[src_name])
    if "gvar" in font:
        font["gvar"].variations[new_name] = copy.deepcopy(font["gvar"].variations.get(src_name, []))
    if "VORG" in font and src_name in font["VORG"].VOriginRecords:
        font["VORG"].VOriginRecords[new_name] = copy.deepcopy(font["VORG"].VOriginRecords[src_name])
    order.append(new_name)
    font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(order)
    if pending_aliases is not None:
        pending_aliases.setdefault(src_name, set()).add(new_name)
    return new_name


def copy_external_glyph_data(target: TTFont, dst_name: str, source: TTFont, src_name: str) -> None:
    if src_name not in source["glyf"].glyphs or dst_name not in target["glyf"].glyphs:
        return
    target["glyf"].glyphs[dst_name] = copy.deepcopy(source["glyf"][src_name])
    if src_name in source["hmtx"].metrics:
        target["hmtx"].metrics[dst_name] = copy.deepcopy(source["hmtx"].metrics[src_name])
    if "vmtx" in target and "vmtx" in source and src_name in source["vmtx"].metrics:
        target["vmtx"].metrics[dst_name] = copy.deepcopy(source["vmtx"].metrics[src_name])
    if "gvar" in target:
        target["gvar"].variations[dst_name] = copy.deepcopy(source["gvar"].variations.get(src_name, [])) if "gvar" in source else []
    if "VORG" in target and "VORG" in source:
        if src_name in source["VORG"].VOriginRecords:
            target["VORG"].VOriginRecords[dst_name] = copy.deepcopy(source["VORG"].VOriginRecords[src_name])
        else:
            target["VORG"].VOriginRecords.pop(dst_name, None)


def assert_compatible_axis_space(base: TTFont, override: TTFont) -> None:
    """校验尚古 VF 与基底 Source Han K VF 具有相同轴范围与 avar 映射（修 C23.5）。"""
    ba, oa = weight_axis(base), weight_axis(override)
    if (ba.minValue, ba.defaultValue, ba.maxValue) != (oa.minValue, oa.defaultValue, oa.maxValue):
        raise ValueError(
            f"尚古 VF 与基底 wght 轴不一致：{(oa.minValue, oa.defaultValue, oa.maxValue)} != {(ba.minValue, ba.defaultValue, ba.maxValue)}"
        )
    bseg = base["avar"].segments.get("wght", {}) if "avar" in base else {}
    oseg = override["avar"].segments.get("wght", {}) if "avar" in override else {}
    if set(bseg) != set(oseg) or any(abs(bseg[k] - oseg[k]) > 1 / 16384 for k in bseg):
        raise ValueError(f"尚古 VF 与基底 avar 映射不一致：{oseg} != {bseg}")


def _single_sub_mapping(font: TTFont, tag: str) -> dict[str, str]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList or not font["GSUB"].table.LookupList:
        return {}
    table = font["GSUB"].table
    out: dict[str, str] = {}
    for rec in table.FeatureList.FeatureRecord:
        if rec.FeatureTag != tag:
            continue
        for idx in rec.Feature.LookupListIndex:
            lk = table.LookupList.Lookup[idx]
            for part in lk.SubTable:
                part = getattr(part, "ExtSubTable", part)
                if isinstance(part, ot.SingleSubst):
                    out.update(part.mapping)
    return out


def _ligature_outputs(font: TTFont, tag: str, first_glyph: str) -> dict[int, str]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList or not font["GSUB"].table.LookupList:
        return {}
    gsub = font["GSUB"].table
    out: dict[int, str] = {}
    for rec in gsub.FeatureList.FeatureRecord:
        if rec.FeatureTag != tag:
            continue
        for idx in rec.Feature.LookupListIndex or []:
            lk = gsub.LookupList.Lookup[idx]
            for part in lk.SubTable:
                st = getattr(part, "ExtSubTable", part)
                if not isinstance(st, ot.LigatureSubst):
                    continue
                for lig in st.ligatures.get(first_glyph, []):
                    comps = [first_glyph, *list(lig.Component or [])]
                    if all(c == first_glyph for c in comps):
                        out[len(comps)] = lig.LigGlyph
    return out


def _dash_cjk_roles(font: TTFont) -> dict[str, str]:
    """解析 Source Han / 尚古 VF 中 CJK 全宽与竖排破折号字形角色。"""
    cmap = font.getBestCmap() or {}
    vert = _single_sub_mapping(font, "vert")
    vert.update(_single_sub_mapping(font, "vrt2"))
    prop = cmap[0x2014]
    fw = cmap[0x2015]
    two_enc = cmap[0x2E3A]
    three_enc = cmap[0x2E3B]
    fw_ligs = _ligature_outputs(font, "ccmp", fw)
    fw_two = fw_ligs.get(2, two_enc if prop == fw else two_enc)
    fw_three = fw_ligs.get(3, three_enc if prop == fw else three_enc)
    return {
        "fullwidth": fw,
        "fullwidth_two": fw_two,
        "fullwidth_three": fw_three,
        "vertical_single": vert.get(fw, cmap.get(0xFE31, fw)),
        "vertical_two": vert.get(fw_two, fw_two),
        "vertical_three": vert.get(fw_three, fw_three),
    }


def apply_classical_override(base: TTFont, override: TTFont, allowed_cps: set[int]) -> dict[str, int]:
    assert_compatible_axis_space(base, override)
    base_cmap = base.getBestCmap() or {}
    src_cmap = override.getBestCmap() or {}
    all_users = glyph_to_unicodes(base)
    groups: dict[str, dict[str, set[int]]] = {}
    for cp in sorted(allowed_cps & set(base_cmap) & set(src_cmap)):
        if is_ideograph(cp):
            groups.setdefault(base_cmap[cp], {}).setdefault(src_cmap[cp], set()).add(cp)

    pending_aliases: dict[str, set[str]] = {}
    changed_cps = changed_glyphs = 0
    for original, replacements in groups.items():
        replaced = set().union(*replacements.values())
        keep_original = not (all_users[original] - replaced)
        for src_name, users in sorted(replacements.items(), key=lambda item: min(item[1])):
            dst_name = original if keep_original else clone_glyph(
                base, original, f"{original}.cl.u{min(users):04X}", pending_aliases
            )
            keep_original = False
            copy_external_glyph_data(base, dst_name, override, src_name)
            for tbl in _unicode_cmap_tables(base):
                for cp in users:
                    if cp in tbl.cmap:
                        tbl.cmap[cp] = dst_name
            changed_cps += len(users)
            changed_glyphs += 1
    if pending_aliases:
        inherit_glyph_aliases(base, pending_aliases)

    base_dash = _dash_cjk_roles(base)
    ov_dash = _dash_cjk_roles(override)
    for role in ("fullwidth", "fullwidth_two", "fullwidth_three", "vertical_single", "vertical_two", "vertical_three"):
        copy_external_glyph_data(base, base_dash[role], override, ov_dash[role])
    return {"classical_codepoints": changed_cps, "classical_glyphs": changed_glyphs}


def shift_glyph_x(font: TTFont, glyph_name: str, dx: float) -> None:
    dx_int = otRound(dx)
    if not dx_int or glyph_name not in font["glyf"].glyphs:
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if glyph.isComposite():
        for comp in glyph.components:
            comp.x = otRound(comp.x + dx_int)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for idx, (x, y) in enumerate(glyph.coordinates):
            glyph.coordinates[idx] = x + dx_int, y
    glyph.recalcBounds(glyf)


def set_advance_width(font: TTFont, glyph_name: str, width: int) -> None:
    _old, lsb = font["hmtx"].metrics.get(glyph_name, (width, 0))
    if glyph_name in font["glyf"].glyphs:
        g = font["glyf"][glyph_name]
        if g.isComposite() or getattr(g, "numberOfContours", 0) > 0:
            g.recalcBounds(font["glyf"])
            lsb = getattr(g, "xMin", lsb)
    font["hmtx"].metrics[glyph_name] = (otRound(width), lsb)


def freeze_advance_variation(font: TTFont, glyph_name: str) -> None:
    if "gvar" not in font:
        return
    for var in font["gvar"].variations.get(glyph_name, []):
        if len(var.coordinates) < 4:
            continue
        for idx in range(len(var.coordinates) - 4, len(var.coordinates)):
            if var.coordinates[idx] is not None:
                var.coordinates[idx] = (0, 0)


def center_to_width(font: TTFont, glyph_name: str, width: int) -> None:
    old_width = font["hmtx"].metrics.get(glyph_name, (width, 0))[0]
    shift_glyph_x(font, glyph_name, (width - old_width) / 2)
    set_advance_width(font, glyph_name, width)
    freeze_advance_variation(font, glyph_name)


def stretch_to_width(font: TTFont, glyph_name: str, width: int) -> None:
    old_width = font["hmtx"].metrics.get(glyph_name, (width, 0))[0]
    if old_width == width or glyph_name not in font["glyf"].glyphs:
        set_advance_width(font, glyph_name, width)
        freeze_advance_variation(font, glyph_name)
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    delta = width - old_width
    if glyph.isComposite():
        for comp in glyph.components:
            if comp.x * 2 >= old_width:
                comp.x = otRound(comp.x + delta)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for idx, (x, y) in enumerate(glyph.coordinates):
            if x * 2 >= old_width:
                glyph.coordinates[idx] = otRound(x + delta), y
    glyph.recalcBounds(glyf)
    set_advance_width(font, glyph_name, width)
    freeze_advance_variation(font, glyph_name)


def bake_pwid_and_sanitize(font: TTFont, spec: Spec) -> dict[str, int]:
    """沿用 Sarasa Ui/pwid 语义烘焙比例标点并规整符号宽度。"""
    pending: dict[str, set[str]] = {}
    cmap = font.getBestCmap() or {}
    rev = glyph_to_unicodes(font)

    def isolate(cp: int) -> None:
        gname = cmap.get(cp)
        if gname and len(rev.get(gname, ())) > 1:
            new_name = clone_glyph(font, gname, f"{gname}.u{cp:04X}", pending)
            for tbl in _unicode_cmap_tables(font):
                if tbl.cmap.get(cp) == gname:
                    tbl.cmap[cp] = new_name
            rev[gname].discard(cp)
            rev[new_name] = {cp}
            cmap[cp] = new_name

    # 隔离强制保留 Source Han 形态的码位（如 U+22EF）以及片假名中点 U+30FB（上游将其归入 kana 而非 punct，
    # 必须保持 1000 全宽，而 SC/TC/HC 中与 U+00B7 共享 periodcentered 的 U+2022/U+2027 则跟随 U+00B7 一起半宽居中；
    # J/K/CL 中 U+2022/U+2027 原生就是独立字形 bullet/uni2027，不会被 U+00B7 带动）。
    for cp in (spec.source_han_forced_codepoints - set(spec.sanitizer_pwid)) | {0x30FB}:
        isolate(cp)
    if pending:
        inherit_glyph_aliases(font, pending)
        pending.clear()

    pwid = _single_sub_mapping(font, "pwid")
    baked = 0
    # 烘焙 pwid 改写普通 cmap 之前，先把 cmap format 14 中依赖这些码位的默认变体 (cp, None)
    # 显式固化为原字形名（例如 U+2018+U+FE01 全宽引号不能随 U+2018 一起变成比例引号）
    uvs_tables = [t for t in font["cmap"].tables if t.format == 14]
    for cp in spec.sanitizer_pwid:
        gname = cmap.get(cp)
        target = pwid.get(gname) if gname else None
        if target and target in font["glyf"].glyphs:
            if target != gname:
                for uvs_tbl in uvs_tables:
                    for sel, entries in uvs_tbl.uvsDict.items():
                        for idx, (base_cp, vglyph) in enumerate(entries):
                            if base_cp == cp and vglyph is None:
                                entries[idx] = (base_cp, gname)
            for tbl in _unicode_cmap_tables(font):
                if cp in tbl.cmap:
                    tbl.cmap[cp] = target
            baked += 1

    cmap = font.getBestCmap() or {}
    rev = glyph_to_unicodes(font)
    interpunct_shared = {0x00B7, 0x2022, 0x2027}
    for cp in spec.sanitizer_pwid:
        if cp == 0x00B7:
            gname = cmap.get(cp)
            if gname:
                for other_cp in sorted(set(rev.get(gname, ())) - interpunct_shared):
                    isolate(other_cp)
        else:
            isolate(cp)
    if pending:
        inherit_glyph_aliases(font, pending)

    upem = font["head"].unitsPerEm
    touched = 0
    cmap = font.getBestCmap() or {}
    for cp, mode in spec.sanitizer_pwid.items():
        gname = cmap.get(cp)
        if not gname:
            continue
        if mode in {"interpunct", "half"}:
            center_to_width(font, gname, upem // 2)
        elif mode == "stretchDual":
            stretch_to_width(font, gname, upem * 2)
        elif mode == "stretchTri":
            stretch_to_width(font, gname, upem * 3)
        touched += 1
    return {"pwid_baked": baked, "symbols_sanitized": touched}


def normalize_hangul_widths(font: TTFont) -> int:
    """将韩文字形（含 GSUB 闭包）的正步进向上取整至整 em 并居中，零步进左移 0.04em；隔离与非韩文共享的字形（如 U+FFA0）。"""
    cmap = font.getBestCmap() or {}
    rev = glyph_to_unicodes(font)
    pending: dict[str, set[str]] = {}
    for cp, gname in list(cmap.items()):
        if not is_korean(cp):
            continue
        non_korean_users = [u for u in rev.get(gname, ()) if not is_korean(u)]
        if non_korean_users:
            for ncp in sorted(non_korean_users):
                cloned = clone_glyph(font, gname, f"{gname}.u{ncp:04X}", pending)
                for tbl in _unicode_cmap_tables(font):
                    if tbl.cmap.get(ncp) == gname:
                        tbl.cmap[ncp] = cloned
                rev[gname].discard(ncp)
                rev[cloned] = {ncp}
                cmap[ncp] = cloned
    if pending:
        inherit_glyph_aliases(font, pending)

    cmap = font.getBestCmap() or {}
    seeds = {name for cp, name in cmap.items() if is_korean(cp)}
    glyphs = substitution_closure(font, seeds)
    em = font["head"].unitsPerEm
    for gname in glyphs:
        old_w = font["hmtx"].metrics.get(gname, (em, 0))[0]
        if old_w > 0:
            target_w = math.ceil(old_w / em) * em
            center_to_width(font, gname, target_w)
        else:
            shift_glyph_x(font, gname, -0.04 * em)
    return len(glyphs)


def _materialize_gvar_deltas(font: TTFont) -> int:
    if "gvar" not in font:
        return 0
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics
    vmtx = font["vmtx"].metrics if "vmtx" in font else None
    count = 0
    for gname, variations in font["gvar"].variations.items():
        coords, controls = glyf._getCoordinatesAndControls(gname, hmtx, vmtx)
        ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
        for var in variations:
            if any(pt is None for pt in var.coordinates):
                var.calcInferredDeltas(coords, ends)
                var.roundDeltas()
                count += 1
    return count


def _shear_variation_deltas(variations: list[TupleVariation], default_errors: list[float], shear: float) -> None:
    count = len(variations)
    exact = [[None if xy is None else xy[0] + xy[1] * shear for xy in v.coordinates[:-4]] for v in variations]
    peaks = [{a: p for a, (_s, p, _e) in v.axes.items()} for v in variations]
    scalars = [[supportScalar(peaks[j], variations[k].axes) for k in range(count)] for j in range(count)]
    groups: list[list[int]] = []
    by_support: dict[tuple, int] = {}
    for idx, var in enumerate(variations):
        key = tuple(sorted(var.axes.items()))
        if key not in by_support:
            by_support[key] = len(groups)
            groups.append([])
        groups[by_support[key]].append(idx)
    pending = list(range(len(groups)))
    done: list[int] = []
    rounded: list[list[int | None]] = [[None] * len(row) for row in exact]
    while pending:
        ready = [g for g in pending if not any(scalars[groups[g][0]][k] for o in pending if o != g for k in groups[o])]
        g = ready[0] if ready else min(pending, key=lambda x: max(abs(v) for v in peaks[groups[x][0]].values()))
        pending.remove(g)
        members = groups[g]
        reached = [(k, scalars[members[0]][k]) for k in done if scalars[members[0]][k]]
        for idx in range(len(exact[members[0]])):
            if any(exact[m][idx] is None for m in members):
                continue
            total = sum(exact[m][idx] for m in members) - (default_errors[idx] if idx < len(default_errors) else 0.0)
            for k, sc in reached:
                if exact[k][idx] is not None and rounded[k][idx] is not None:
                    total += sc * (exact[k][idx] - rounded[k][idx])
            for m in members[:-1]:
                rounded[m][idx] = otRound(exact[m][idx])
                total -= rounded[m][idx]
            rounded[members[-1]][idx] = otRound(total)
        done.extend(members)
    for var, row in zip(variations, rounded):
        for idx, val in enumerate(row):
            if val is not None:
                var.coordinates[idx] = val, var.coordinates[idx][1]


def shear_font(font: TTFont, angle_degrees: float) -> dict[str, int]:
    """物化 IUP 增量并以 em/3 为中心倾斜轮廓及 gvar 增量，不剪切 metric phantom points。"""
    glyf = font["glyf"]
    true_defaults: dict[str, list[tuple[float, float]]] = {}
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        g.expand(glyf)
        if g.isComposite():
            true_defaults[gname] = [(c.x, c.y) for c in g.components]
            for c in g.components:
                c.x, c.y = otRound(c.x), otRound(c.y)
        elif g.numberOfContours > 0 and hasattr(g, "coordinates"):
            true_defaults[gname] = list(g.coordinates)
            g.coordinates.toInt(round=otRound)
    materialized = _materialize_gvar_deltas(font)
    shear = math.tan(math.radians(angle_degrees))
    center = font["head"].unitsPerEm / 3
    default_errors: dict[str, list[float]] = {}
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        g.expand(glyf)
        errs: list[float] = []
        if g.isComposite():
            for comp, (tx, ty) in zip(g.components, true_defaults[gname]):
                (xx, xy), (yx, yy) = getattr(comp, "transform", [[1, 0], [0, 1]])
                tr = [[xx + shear * xy, xy], [yx + shear * (yy - xx) - shear * shear * xy, yy - shear * xy]]
                off = center * shear * (tr[0][0] - 1)
                exact = comp.x + comp.y * shear + off
                comp.x = otRound(tx + ty * shear + off)
                errs.append(comp.x - exact)
                comp.y = otRound(comp.y + center * shear * xy)
                if tr != [[1, 0], [0, 1]]:
                    comp.transform = tr
        elif g.numberOfContours > 0 and hasattr(g, "coordinates"):
            for idx, ((x, y), (tx, ty)) in enumerate(zip(g.coordinates, true_defaults[gname])):
                g.coordinates[idx] = otRound(tx + (ty - center) * shear), y
                errs.append(g.coordinates[idx][0] - (x + (y - center) * shear))
        default_errors[gname] = errs
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        g.recalcBounds(glyf)
        if hasattr(g, "xMin"):
            font["hmtx"].metrics[gname] = (font["hmtx"].metrics[gname][0], g.xMin)
    if "gvar" in font:
        for gname, variations in font["gvar"].variations.items():
            _shear_variation_deltas(variations, default_errors.get(gname, []), shear)
    return {"italic_tuples_materialized": materialized}


def load_base(
    source_han_vf: Path,
    region: str,
    italic: bool,
    allowed_unicodes: set[int],
    inter_unicodes: set[int],
    *,
    classical_vf: Path | None = None,
    spec: Spec | None = None,
) -> tuple[TTFont, dict[str, Any]]:
    spec = spec or load_spec()
    report: dict[str, Any] = {}
    forced = spec.source_han_forced_codepoints

    base = TTFont(source_han_vf)
    base_cps = set((base.getBestCmap() or {}).keys())
    keep_cps = {
        cp for cp in allowed_unicodes
        if cp in base_cps and not (cp in inter_unicodes and use_inter_codepoint(cp, forced))
    }
    # 先子集化再原地限轴，大幅减少无关字形的 gvar 计算与内存峰值
    subset_preserving_routes_and_uvs(base, keep_cps)
    instantiateVariableFont(base, {"wght": spec.source_han_limit}, inplace=True, optimize=not italic)

    if classical_vf is not None:
        ov = TTFont(classical_vf)
        try:
            ov_cps = set((ov.getBestCmap() or {}).keys())
            ov_keep = ({cp for cp in keep_cps if is_ideograph(cp)} | set(spec.dash_cmap_codepoints)) & ov_cps
            subset_preserving_routes_and_uvs(ov, ov_keep)
            instantiateVariableFont(ov, {"wght": spec.source_han_limit}, inplace=True, optimize=not italic)
            report.update(apply_classical_override(base, ov, keep_cps))
        finally:
            ov.close()

    report.update(apply_public_weight_axis(base, spec))
    report.update(bake_pwid_and_sanitize(base, spec))
    report["hangul_widths_normalized"] = normalize_hangul_widths(base)
    if italic:
        report.update(shear_font(base, abs(spec.italic_angle)))
    return base, report
