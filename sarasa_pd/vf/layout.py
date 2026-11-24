"""VF 布局合并：基底特性裁剪、Inter GDEF/GPOS 变化与布局精确导入、数字特性与路由收尾。"""
from __future__ import annotations

import copy
from typing import Any

from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.varLib.builder import buildVarDevTable
from fontTools.varLib.models import VariationModel
from fontTools.varLib.varStore import OnlineVarStoreBuilder, VarStoreInstancer

from ..spec import Spec, load_spec
from .base import glyph_to_unicodes, normalized_wght, weight_axis
from .inter import (
    LoadedInter,
    anchor_slots,
    assert_public_axis_ready,
    collect_ot_variation_devices,
    prefixed,
    variable_value_slots,
)
from .layout_utils import (
    Feature,
    install,
    merge_routes,
    prune_unreferenced_lookups,
    remove_empty_lookups,
    routes,
    share_default_with_cyrl_grek,
    sort_pair_sets,
)
from .variations import adjacent_supports, weight_at


def _denorm(val: float, lo: float, default: float, hi: float) -> float:
    return default + val * (default - lo) if val < 0 else default + val * (hi - default)


def _single_subtables(lookup: ot.Lookup) -> list[ot.SingleSubst]:
    if lookup.LookupType == 1:
        return list(lookup.SubTable)
    if lookup.LookupType == 7:
        return [
            st.ExtSubTable
            for st in lookup.SubTable
            if getattr(st, "ExtensionLookupType", None) == 1 and getattr(st, "ExtSubTable", None)
        ]
    return []


def drop_feature_records(font: TTFont, table_tag: str, tags: set[str]) -> int:
    if table_tag not in font or not tags:
        return 0
    tbl = font[table_tag].table
    if not tbl.FeatureList:
        return 0
    current = routes(tbl)
    removed = 0
    for lang, feats in current.items():
        for tag in list(feats):
            if tag in tags:
                del feats[tag]
                removed += 1
    if removed:
        install(tbl, current)
    return removed


def drop_sarasa_width_features(font: TTFont, spec: Spec) -> dict[str, int]:
    tags = set(spec.features["width_features"])
    return {
        "gsub_width_dropped": drop_feature_records(font, "GSUB", tags),
        "gpos_width_dropped": drop_feature_records(font, "GPOS", tags),
    }


def prune_locl_to_reference(font: TTFont, reference: TTFont, spec: Spec) -> dict[str, int]:
    """保留上游 Sarasa Ui Regular 中出现过的 locl 源码位以及 Source Han 破折号 locl 码位。"""
    ref_rev = glyph_to_unicodes(reference)
    allowed: set[int] = set(spec.dash_locl_codepoints)
    if "GSUB" in reference and reference["GSUB"].table.FeatureList and reference["GSUB"].table.LookupList:
        rgsub = reference["GSUB"].table
        for rec in rgsub.FeatureList.FeatureRecord:
            if rec.FeatureTag != "locl":
                continue
            for idx in rec.Feature.LookupListIndex or []:
                for st in _single_subtables(rgsub.LookupList.Lookup[idx]):
                    for src in st.mapping:
                        allowed.update(ref_rev.get(src, ()))

    if "GSUB" not in font or not font["GSUB"].table.FeatureList or not font["GSUB"].table.LookupList:
        return {"locl_before": 0, "locl_after": 0}
    rev = glyph_to_unicodes(font)
    gsub = font["GSUB"].table
    before = after = 0
    for rec in gsub.FeatureList.FeatureRecord:
        if rec.FeatureTag != "locl":
            continue
        kept: list[int] = []
        for idx in rec.Feature.LookupListIndex or []:
            lk = gsub.LookupList.Lookup[idx]
            subs = _single_subtables(lk)
            if subs:
                non_empty = False
                for st in subs:
                    before += len(st.mapping)
                    st.mapping = {s: d for s, d in st.mapping.items() if rev.get(s, set()) & allowed}
                    after += len(st.mapping)
                    if st.mapping:
                        non_empty = True
                if non_empty:
                    kept.append(idx)
            elif lk.SubTable:
                kept.append(idx)
        rec.Feature.LookupListIndex = kept
        rec.Feature.LookupCount = len(kept)
    return {"locl_before": before, "locl_after": after}


def drop_nonfinal_source_han_gsub(font: TTFont, spec: Spec) -> int:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return 0
    allowed = set(spec.features["source_han_final_gsub"])
    present = {rec.FeatureTag for rec in font["GSUB"].table.FeatureList.FeatureRecord}
    return drop_feature_records(font, "GSUB", present - allowed)


def _rename_ot_glyphs(obj: Any, rename: dict[str, str], seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (str, int, float, bool, bytes)) or id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            nk = rename.get(k, k) if isinstance(k, str) else k
            if nk != k:
                del obj[k]
                obj[nk] = v
            if isinstance(v, str) and v in rename:
                obj[nk] = rename[v]
            else:
                _rename_ot_glyphs(obj[nk], rename, seen)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and v in rename:
                obj[i] = rename[v]
            else:
                _rename_ot_glyphs(v, rename, seen)
    elif hasattr(obj, "__dict__"):
        for k, v in vars(obj).items():
            if k.lower().endswith("tag") or k in {"reader", "font"}:
                continue
            if isinstance(v, str) and v in rename:
                setattr(obj, k, rename[v])
            else:
                _rename_ot_glyphs(v, rename, seen)


def _lookup_records(val: Any, seen: set[int] | None = None) -> list[Any]:
    if val is None or isinstance(val, (str, int, float, bool, bytes)):
        return []
    seen = set() if seen is None else seen
    if id(val) in seen:
        return []
    seen.add(id(val))
    if isinstance(val, (ot.SubstLookupRecord, ot.PosLookupRecord)):
        return [val]
    if isinstance(val, dict):
        children = val.values()
    elif isinstance(val, (list, tuple)):
        children = val
    elif hasattr(val, "__dict__"):
        children = [v for k, v in vars(val).items() if k not in {"reader", "font"}]
    else:
        return []
    out: list[Any] = []
    for c in children:
        out.extend(_lookup_records(c, seen))
    return out


def _import_name_id(target: TTFont, source: TTFont, src_id: int, remap: dict[int, int]) -> int:
    if src_id in remap:
        return remap[src_id]
    src_recs = [r for r in source["name"].names if r.nameID == src_id]
    if not src_recs:
        return src_id
    tname = target["name"]
    dst_recs = [r for r in tname.names if r.nameID == src_id]
    src_sig = {(r.platformID, r.platEncID, r.langID, r.toUnicode()) for r in src_recs}
    dst_sig = {(r.platformID, r.platEncID, r.langID, r.toUnicode()) for r in dst_recs}
    if dst_recs and dst_sig != src_sig:
        used = {r.nameID for r in tname.names}
        dst_id = 256
        while dst_id in used:
            dst_id += 1
    else:
        dst_id = src_id
    existing = {(r.nameID, r.platformID, r.platEncID, r.langID) for r in tname.names}
    for r in src_recs:
        key = (dst_id, r.platformID, r.platEncID, r.langID)
        if key not in existing:
            rec = copy.deepcopy(r)
            rec.nameID = dst_id
            tname.names.append(rec)
            existing.add(key)
    remap[src_id] = dst_id
    return dst_id


def _import_feature_param_names(target: TTFont, source: TTFont, feature: Any, remap: dict[int, int]) -> None:
    params = getattr(feature, "FeatureParams", None)
    if params is None:
        return
    for field, val in vars(params).items():
        if field.endswith("NameID") and isinstance(val, int) and val >= 256:
            setattr(params, field, _import_name_id(target, source, val, remap))


def _append_var_store(existing: Any, additional: Any, var_indices: list[int]) -> tuple[Any, list[int]]:
    if existing.VarRegionList.RegionAxisCount != additional.VarRegionList.RegionAxisCount:
        raise ValueError("无法合并轴数不同的 GDEF VarStore")

    def rkey(reg: Any) -> tuple[tuple[float, float, float], ...]:
        return tuple((float(a.StartCoord), float(a.PeakCoord), float(a.EndCoord)) for a in reg.VarRegionAxis)

    merged = copy.deepcopy(existing)
    known = {rkey(r): i for i, r in enumerate(merged.VarRegionList.Region)}
    reg_map: dict[int, int] = {}
    for old_i, reg in enumerate(additional.VarRegionList.Region):
        k = rkey(reg)
        new_i = known.get(k)
        if new_i is None:
            new_i = len(merged.VarRegionList.Region)
            merged.VarRegionList.Region.append(copy.deepcopy(reg))
            known[k] = new_i
        reg_map[old_i] = new_i
    merged.VarRegionList.RegionCount = len(merged.VarRegionList.Region)

    major_offset = len(merged.VarData)
    for vd in additional.VarData:
        c = copy.deepcopy(vd)
        c.VarRegionIndex = [reg_map[i] for i in c.VarRegionIndex]
        c.VarRegionCount = len(c.VarRegionIndex)
        merged.VarData.append(c)
    merged.VarDataCount = len(merged.VarData)

    remapped = [
        idx if idx == 0xFFFFFFFF else (((idx >> 16) + major_offset) << 16) | (idx & 0xFFFF)
        for idx in var_indices
    ]
    return merged, remapped


def _remap_variation_devices(table: Any, mapping: dict[int, int]) -> None:
    done: dict[int, Any] = {}

    def visit(val: Any) -> Any:
        if val is None or isinstance(val, (str, int, float, bool, bytes)):
            return val
        if id(val) in done:
            return done[id(val)]
        if isinstance(val, ot.Device) and getattr(val, "DeltaFormat", None) == 0x8000:
            idx = mapping[(val.StartSize << 16) | val.EndSize]
            if idx == 0xFFFFFFFF:
                done[id(val)] = None
                return None
            val.StartSize, val.EndSize = idx >> 16, idx & 0xFFFF
            done[id(val)] = val
            return val
        done[id(val)] = val
        if isinstance(val, list):
            val[:] = [visit(x) for x in val]
        elif isinstance(val, dict):
            for k, x in list(val.items()):
                val[k] = visit(x)
        elif hasattr(val, "__dict__"):
            for k, x in list(vars(val).items()):
                if k not in {"reader", "font"}:
                    setattr(val, k, visit(x))
        return val

    visit(table)


def import_inter_gdef(base: TTFont, inter: TTFont) -> dict[int, int]:
    if "GDEF" not in inter:
        return {}
    if "GDEF" not in base:
        base["GDEF"] = newTable("GDEF")
        base["GDEF"].table = ot.GDEF()
        base["GDEF"].table.Version = 0x00010000
    dst, src = base["GDEF"].table, inter["GDEF"].table
    rename = {n: prefixed(n) for n in inter.getGlyphOrder() if n != ".notdef"}
    if getattr(src, "GlyphClassDef", None):
        if not getattr(dst, "GlyphClassDef", None):
            dst.GlyphClassDef = ot.ClassDef()
            dst.GlyphClassDef.classDefs = {}
        dst.GlyphClassDef.classDefs.update({rename[n]: v for n, v in src.GlyphClassDef.classDefs.items() if n in rename})
    mark_map: dict[int, int] = {}
    src_sets = getattr(src, "MarkGlyphSetsDef", None)
    if src_sets:
        if not getattr(dst, "MarkGlyphSetsDef", None):
            dst.MarkGlyphSetsDef = ot.MarkGlyphSetsDef()
            dst.MarkGlyphSetsDef.MarkSetTableFormat = 1
            dst.MarkGlyphSetsDef.Coverage = []
        for idx, cov in enumerate(src_sets.Coverage):
            mark_map[idx] = len(dst.MarkGlyphSetsDef.Coverage)
            c = copy.deepcopy(cov)
            c.glyphs = [rename[n] for n in c.glyphs if n in rename]
            dst.MarkGlyphSetsDef.Coverage.append(c)
        dst.MarkGlyphSetsDef.MarkSetCount = len(dst.MarkGlyphSetsDef.Coverage)
        dst.Version = max(dst.Version, 0x00010002)
    return mark_map


def import_inter_gpos_variations(base: TTFont, loaded: LoadedInter, spec: Spec) -> dict[int, int]:
    """将 Inter 未限轴/未缩放 GPOS 定位曲线投影到基底公开轴并合入 GDEF VarStore。"""
    inter = loaded.font
    if "GPOS" not in inter:
        return {0xFFFFFFFF: 0xFFFFFFFF}
    inter["GPOS"].ensureDecompiled()
    devices = collect_ot_variation_devices(inter["GPOS"].table)
    indices = sorted({(d.StartSize << 16) | d.EndSize for d in devices} - {0xFFFFFFFF})
    if not indices:
        return {0xFFFFFFFF: 0xFFFFFFFF}
    assert_public_axis_ready(base, spec)
    store = getattr(inter["GDEF"].table, "VarStore", None) if "GDEF" in inter else None
    if store is None:
        raise ValueError("Inter GPOS 变化引用缺少 GDEF VarStore")
    if [a.axisTag for a in inter["fvar"].axes] != ["wght"]:
        raise ValueError("导入 Inter GPOS 前必须固定 opsz，仅保留 wght")

    iaxis = weight_axis(inter)
    iseg = inter["avar"].segments.get("wght", {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}) if "avar" in inter else {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    baxis = weight_axis(base)
    bseg = base["avar"].segments["wght"]

    weights: set[float] = set(spec.public_to_internal_wght)
    weights.update(_denorm(v, iaxis.minValue, iaxis.defaultValue, iaxis.maxValue) for v in iseg)
    weights.update(_denorm(v, baxis.minValue, baxis.defaultValue, baxis.maxValue) for v in bseg)
    for reg in store.VarRegionList.Region:
        ax = reg.VarRegionAxis[0]
        for v in (ax.StartCoord, ax.PeakCoord, ax.EndCoord):
            weights.add(weight_at(inter, v))
    weights_sorted = sorted(w for w in weights if baxis.minValue <= w <= baxis.maxValue)

    by_loc = {floatToFixedToFloat(normalized_wght(base, w), 14): w for w in weights_sorted}
    for w in spec.public_to_internal_wght:
        by_loc[floatToFixedToFloat(normalized_wght(base, w), 14)] = float(w)
    locs = sorted(by_loc)
    weights_final = [by_loc[v] for v in locs]
    model_locs = [{"wght": v} if v else {} for v in locs]
    model = VariationModel(model_locs, axisOrder=["wght"])

    master_store, scale = loaded.unscaled_gpos_store or (store, 1.0)
    instancers = [VarStoreInstancer(master_store, inter["fvar"].axes, {"wght": normalized_wght(inter, w)}) for w in weights_final]
    orig_insts = loaded.original_instancers(weights_final)
    orig_scale = (1000 / loaded.original_gpos[3]) if orig_insts is not None and loaded.original_gpos else scale

    def sample_curve(dev: Any, idx: int) -> list[float]:
        oidx = getattr(dev, "_sarasa_original_varidx", None)
        if orig_insts is not None and oidx is not None:
            return [inst[oidx] * orig_scale for inst in orig_insts]
        return [inst[idx] * scale for inst in instancers]

    builder = OnlineVarStoreBuilder(["wght"])
    builder.setModel(model)
    remap: dict[int, int] = {}
    for idx in indices:
        default, new_idx = builder.storeMasters([inst[idx] * scale for inst in instancers], round=otRound)
        if default:
            raise ValueError("Inter GPOS 变化数据在默认坐标不为零")
        remap[idx] = new_idx

    supports, master_indices = adjacent_supports(model_locs)
    builder.setSupports(supports)
    synthetic = 0xFF000000
    done_anchors: set[int] = set()
    for _g, _o, _f, anchor in anchor_slots(inter):
        entry = loaded.anchor_fractions.get(id(anchor))
        if entry is None or entry[0] is not anchor or id(anchor) in done_anchors:
            continue
        done_anchors.add(id(anchor))
        for frac, dfield in zip(entry[1:], ("XDeviceTable", "YDeviceTable")):
            dev = getattr(anchor, dfield, None)
            if dev is None or getattr(dev, "DeltaFormat", None) != 0x8000:
                continue
            idx = (dev.StartSize << 16) | dev.EndSize
            if idx == 0xFFFFFFFF:
                continue
            vals = [otRound(frac + d) for d in sample_curve(dev, idx)]
            deltas = [vals[m] for m in master_indices]
            if not any(deltas):
                setattr(anchor, dfield, None)
            else:
                setattr(anchor, dfield, buildVarDevTable(synthetic))
                remap[synthetic] = builder.storeDeltas(deltas)
                synthetic += 1

    for rec, field, dfield in variable_value_slots(inter["GPOS"].table):
        entry = loaded.value_fractions.get((id(rec), field))
        if entry is None or entry[0] is not rec:
            continue
        dev = getattr(rec, dfield)
        idx = (dev.StartSize << 16) | dev.EndSize
        if idx == 0xFFFFFFFF:
            continue
        vals = [otRound(entry[1] + d) for d in sample_curve(dev, idx)]
        deltas = [vals[m] for m in master_indices]
        if not any(deltas):
            setattr(rec, dfield, None)
        else:
            setattr(rec, dfield, buildVarDevTable(synthetic))
            remap[synthetic] = builder.storeDeltas(deltas)
            synthetic += 1

    additional = builder.finish()
    all_keys = sorted(remap)
    if "GDEF" not in base:
        base["GDEF"] = newTable("GDEF")
        base["GDEF"].table = ot.GDEF()
    gdef = base["GDEF"].table
    existing = getattr(gdef, "VarStore", None)
    if existing is not None:
        merged, merged_ids = _append_var_store(existing, additional, [remap[k] for k in all_keys])
        remap = dict(zip(all_keys, merged_ids))
    else:
        merged = additional
    gdef.Version = 0x00010003
    gdef.VarStore = merged
    remap[0xFFFFFFFF] = 0xFFFFFFFF
    return remap


def import_inter_layout_table(
    base: TTFont,
    loaded: LoadedInter,
    table_tag: str,
    feature_tags: set[str],
    mark_filter_map: dict[int, int],
    spec: Spec,
) -> dict[str, int]:
    inter = loaded.font
    if table_tag not in inter:
        return {f"inter_{table_tag.lower()}_lookups": 0}
    var_map = import_inter_gpos_variations(base, loaded, spec) if table_tag == "GPOS" else {}
    inter[table_tag].ensureDecompiled()
    src = inter[table_tag].table
    if not src.FeatureList or not src.LookupList:
        return {f"inter_{table_tag.lower()}_lookups": 0}

    if table_tag not in base:
        base[table_tag] = newTable(table_tag)
        base[table_tag].table = ot.GSUB() if table_tag == "GSUB" else ot.GPOS()
        base[table_tag].table.Version = 0x00010000
        base[table_tag].table.ScriptList = ot.ScriptList()
        base[table_tag].table.ScriptList.ScriptRecord = []
        base[table_tag].table.ScriptList.ScriptCount = 0
        base[table_tag].table.FeatureList = ot.FeatureList()
        base[table_tag].table.FeatureList.FeatureRecord = []
        base[table_tag].table.FeatureList.FeatureCount = 0
        base[table_tag].table.LookupList = ot.LookupList()
        base[table_tag].table.LookupList.Lookup = []
        base[table_tag].table.LookupList.LookupCount = 0

    dst = base[table_tag].table
    rename = {n: prefixed(n) for n in inter.getGlyphOrder() if n != ".notdef"}

    # 按 Inter 原始序号收集全部可达 lookup（包括子 lookup 闭包），严格保持 Inter 原生顺序（修 A12.5）
    f_recs = [r for r in src.FeatureList.FeatureRecord if r.FeatureTag in feature_tags]
    lset = {int(i) for r in f_recs for i in (r.Feature.LookupListIndex or [])}
    pending = list(lset)
    while pending:
        for rec in _lookup_records(src.LookupList.Lookup[pending.pop()]):
            if rec.LookupListIndex not in lset:
                lset.add(rec.LookupListIndex)
                pending.append(rec.LookupListIndex)
    ordered = sorted(lset)
    lmap = {old: len(dst.LookupList.Lookup) + off for off, old in enumerate(ordered)}
    for old in ordered:
        lk = copy.deepcopy(src.LookupList.Lookup[old])
        _rename_ot_glyphs(lk, rename)
        if lk.LookupFlag & 0x0010:
            lk.MarkFilteringSet = mark_filter_map[lk.MarkFilteringSet]
        for rec in _lookup_records(lk):
            rec.LookupListIndex = lmap[rec.LookupListIndex]
        if table_tag == "GPOS":
            _remap_variation_devices(lk, var_map)
        dst.LookupList.Lookup.append(lk)
    dst.LookupList.LookupCount = len(dst.LookupList.Lookup)

    name_remap: dict[int, int] = {}
    incoming: dict[tuple[str, str], dict[str, Feature]] = {}
    for lang, feats in routes(src).items():
        incoming[lang] = {}
        for tag, feat in feats.items():
            if tag not in feature_tags:
                continue
            dummy = ot.Feature()
            dummy.FeatureParams = copy.deepcopy(feat.params)
            _import_feature_param_names(base, inter, dummy, name_remap)
            incoming[lang][tag] = Feature(tuple(lmap[i] for i in feat.lookups), dummy.FeatureParams, feat.required)
    merge_routes(dst, incoming)
    return {f"inter_{table_tag.lower()}_lookups": len(ordered)}


def enable_features_for_all_scripts(font: TTFont, table_tag: str, tags: set[str]) -> None:
    if table_tag not in font or not font[table_tag].table.FeatureList:
        return
    tbl = font[table_tag].table
    current = routes(tbl)
    template = {
        tag: feat
        for feats in current.values()
        for tag, feat in feats.items()
        if tag in tags
    }
    if not template:
        return
    for feats in current.values():
        for tag, feat in template.items():
            if tag not in feats:
                feats[tag] = feat
    install(tbl, current)


def import_inter_layout(base: TTFont, loaded: LoadedInter, spec: Spec) -> dict[str, int]:
    mark_map = import_inter_gdef(base, loaded.font)
    rep: dict[str, int] = {"inter_gdef_mark_sets": len(mark_map)}
    rep.update(import_inter_layout_table(base, loaded, "GSUB", set(spec.features["inter_gsub"]), mark_map, spec))
    rep.update(import_inter_layout_table(base, loaded, "GPOS", set(spec.features["inter_gpos"]), mark_map, spec))
    enable_features_for_all_scripts(base, "GSUB", {"tnum", "pnum", "calt"})
    return rep


def finalize_layout(font: TTFont) -> dict[str, int]:
    """最终布局收尾：cyrl/grek 共享 DFLT、清零空 lookup、删不可达 lookup 并排序 PairSet（幂等）。"""
    rep = share_default_with_cyrl_grek(font)
    rep["empty_lookups_removed"] = remove_empty_lookups(font)
    rep["unreferenced_lookups_removed"] = prune_unreferenced_lookups(font)
    rep["pair_sets_sorted"] = sort_pair_sets(font)
    return rep
