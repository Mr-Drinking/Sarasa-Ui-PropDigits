"""OpenType GSUB/GPOS 路由管理、PairSet 排序、不可达 lookup 清理与字形别名继承。"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from itertools import product
from math import prod
from typing import Any

from fontTools import subset as _ft_subset  # noqa: F401  # 注册 lookup 闭包方法
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot

FAR_EAST_SCRIPTS = frozenset({"hani", "kana", "bopo", "hang"})
FAR_EAST_LANGUAGES = frozenset({"JAN", "KOR", "ZHS", "ZHT", "ZHH"})
SHARED_DEFAULT_SCRIPTS = frozenset({"cyrl", "grek"})
OWN_VERTICAL_FEATURES = frozenset({"vert", "vrt2"})

ALIGNED_COVERAGES = {
    "Coverage": [
        ("Value", "ValueCount"),
        ("PairSet", "PairSetCount"),
        ("EntryExitRecord", "EntryExitCount"),
        ("Substitute", "GlyphCount"),
        ("SubRuleSet", "SubRuleSetCount"),
        ("ChainSubRuleSet", "ChainSubRuleSetCount"),
        ("PosRuleSet", "PosRuleSetCount"),
        ("ChainPosRuleSet", "ChainPosRuleSetCount"),
        ("AttachPoint", "GlyphCount"),
        ("LigGlyph", "LigGlyphCount"),
    ],
    "MarkCoverage": [("MarkArray.MarkRecord", "MarkCount")],
    "BaseCoverage": [("BaseArray.BaseRecord", "BaseCount")],
    "LigatureCoverage": [("LigatureArray.LigatureAttach", "LigatureCount")],
    "Mark1Coverage": [("Mark1Array.MarkRecord", "MarkCount")],
    "Mark2Coverage": [("Mark2Array.Mark2Record", "Mark2Count")],
}


@dataclass
class Feature:
    lookups: tuple[int, ...]
    params: object = None
    required: bool = False


def combine(left: Feature, right: Feature) -> Feature:
    if left.params is not None and right.params is not None and left.params != right.params:
        raise ValueError("同一语言的 OpenType 特性参数冲突")
    return Feature(
        tuple(dict.fromkeys((*left.lookups, *right.lookups))),
        left.params if left.params is not None else right.params,
        left.required or right.required,
    )


def routes(table: Any) -> dict[tuple[str, str], dict[str, Feature]]:
    result: dict[tuple[str, str], dict[str, Feature]] = {}
    if not table.ScriptList or not table.FeatureList:
        return result
    for srec in table.ScriptList.ScriptRecord:
        script = srec.Script
        systems = [("dflt", script.DefaultLangSys)] if script.DefaultLangSys is not None else []
        systems += [(rec.LangSysTag, rec.LangSys) for rec in script.LangSysRecord]
        for language, system in systems:
            features: dict[str, Feature] = {}
            indices = list(system.FeatureIndex)
            required = system.ReqFeatureIndex
            if required != 0xFFFF and required not in indices:
                indices.append(required)
            for idx in indices:
                rec = table.FeatureList.FeatureRecord[idx]
                feat = Feature(tuple(rec.Feature.LookupListIndex), rec.Feature.FeatureParams, idx == required)
                features[rec.FeatureTag] = combine(features[rec.FeatureTag], feat) if rec.FeatureTag in features else feat
            result[(srec.ScriptTag, language)] = features
    return result


def install(table: Any, mapping: dict[tuple[str, str], dict[str, Feature]]) -> None:
    if getattr(table, "FeatureVariations", None) is not None:
        raise ValueError("重建特性路由前必须显式处理 FeatureVariations")
    feature_records: list[ot.FeatureRecord] = []
    bindings: dict[tuple[str, str], list[tuple[int, bool]]] = {}
    for key, features in sorted(mapping.items()):
        bindings[key] = []
        for tag, feat in sorted(features.items()):
            index = next(
                (
                    i
                    for i, rec in enumerate(feature_records)
                    if rec.FeatureTag == tag
                    and tuple(rec.Feature.LookupListIndex) == feat.lookups
                    and rec.Feature.FeatureParams == feat.params
                ),
                None,
            )
            if index is None:
                rec = ot.FeatureRecord()
                rec.FeatureTag = tag
                rec.Feature = ot.Feature()
                rec.Feature.FeatureParams = copy.deepcopy(feat.params)
                rec.Feature.LookupListIndex = list(feat.lookups)
                rec.Feature.LookupCount = len(feat.lookups)
                index = len(feature_records)
                feature_records.append(rec)
            bindings[key].append((index, feat.required))

    order = sorted(range(len(feature_records)), key=lambda idx: feature_records[idx].FeatureTag)
    remap = {old: new for new, old in enumerate(order)}
    table.FeatureList = ot.FeatureList()
    table.FeatureList.FeatureRecord = [feature_records[idx] for idx in order]
    table.FeatureList.FeatureCount = len(order)
    table.ScriptList = ot.ScriptList()
    table.ScriptList.ScriptRecord = []
    for script_tag in sorted({script for script, _ in mapping}):
        srec = ot.ScriptRecord()
        srec.ScriptTag = script_tag
        srec.Script = ot.Script()
        srec.Script.DefaultLangSys = None
        srec.Script.LangSysRecord = []
        for (script, language), refs in sorted(bindings.items()):
            if script != script_tag:
                continue
            sys_obj = ot.LangSys()
            sys_obj.LookupOrder = None
            req = [remap[idx] for idx, is_req in refs if is_req]
            if len(req) > 1:
                raise ValueError("语言系统包含多个必要特性，必须显式合并")
            sys_obj.ReqFeatureIndex = req[0] if req else 0xFFFF
            sys_obj.FeatureIndex = sorted(remap[idx] for idx, is_req in refs if not is_req)
            sys_obj.FeatureCount = len(sys_obj.FeatureIndex)
            if language == "dflt":
                srec.Script.DefaultLangSys = sys_obj
            else:
                entry = ot.LangSysRecord()
                entry.LangSysTag = language
                entry.LangSys = sys_obj
                srec.Script.LangSysRecord.append(entry)
        srec.Script.LangSysCount = len(srec.Script.LangSysRecord)
        table.ScriptList.ScriptRecord.append(srec)
    table.ScriptList.ScriptCount = len(table.ScriptList.ScriptRecord)


def far_east_receives_default(script: str, language: str) -> bool:
    if language == "dflt":
        return script in FAR_EAST_SCRIPTS
    return script in FAR_EAST_SCRIPTS or language.strip() in FAR_EAST_LANGUAGES


def shared_default_receives(script: str, language: str) -> bool:
    return script in SHARED_DEFAULT_SCRIPTS and language == "dflt"


def share_default(mapping: dict[tuple[str, str], dict[str, Feature]], receives: Any, keep: frozenset[str] = frozenset()) -> int:
    default = mapping.get(("DFLT", "dflt"))
    changed = 0
    if not default:
        return changed
    for key, features in mapping.items():
        if key == ("DFLT", "dflt") or not receives(*key):
            continue
        for tag, feat in default.items():
            own = features.get(tag)
            if own is not None and tag in keep:
                continue
            shared = (
                Feature(feat.lookups, feat.params)
                if own is None
                else combine(own, Feature(feat.lookups, feat.params))
            )
            if own is None or shared.lookups != own.lookups or shared.params is not own.params:
                features[tag] = copy.deepcopy(shared)
                changed += 1
    return changed


def merge_routes(table: Any, incoming: dict[tuple[str, str], dict[str, Feature]]) -> None:
    """按 Sarasa 语义精确合并语言系统，并把 DFLT/dflt 特性共享给远东脚本/语言。"""
    current = routes(table)
    merged: dict[tuple[str, str], dict[str, Feature]] = {}
    for key in set(current) | set(incoming):
        features = copy.deepcopy(current.get(key, {}))
        for tag, feat in incoming.get(key, {}).items():
            features[tag] = combine(features[tag], feat) if tag in features else copy.deepcopy(feat)
        merged[key] = features
    share_default(merged, far_east_receives_default)
    install(table, merged)


def share_default_with_cyrl_grek(font: TTFont) -> dict[str, int]:
    report: dict[str, int] = {}
    for tag in ("GSUB", "GPOS"):
        if tag not in font:
            continue
        table = font[tag].table
        mapping = routes(table)
        changed = share_default(mapping, shared_default_receives, OWN_VERTICAL_FEATURES)
        if changed:
            install(table, mapping)
        report[f"{tag.lower()}_cyrl_grek_shared"] = changed
    return report


def uvs_target_glyphs(font: TTFont) -> set[str]:
    """收集 cmap format 14 引用的全部非默认变体字形名（修 A1/C23）。"""
    glyphs: set[str] = set()
    if "cmap" not in font:
        return glyphs
    for sub in font["cmap"].tables:
        if sub.format != 14:
            continue
        for pairs in sub.uvsDict.values():
            for _cp, gname in pairs:
                if gname is not None:
                    glyphs.add(gname)
    return glyphs


def substitution_closure(font: TTFont, initial: set[str]) -> set[str]:
    glyphs = set(initial)
    subtables = []
    if "GSUB" in font and font["GSUB"].table.LookupList:
        for lookup in font["GSUB"].table.LookupList.Lookup:
            subtables.extend(getattr(part, "ExtSubTable", part) for part in lookup.SubTable)
    while True:
        expanded = set(glyphs)
        for part in subtables:
            for field in ("mapping", "alternates"):
                for src, dst in getattr(part, field, {}).items():
                    if src in glyphs:
                        expanded.update([dst] if isinstance(dst, str) else dst)
            for src, rules in getattr(part, "ligatures", {}).items():
                if src in glyphs:
                    expanded.update(r.LigGlyph for r in rules if all(n in glyphs for n in r.Component))
            if hasattr(part, "Substitute") and hasattr(part, "Coverage"):
                expanded.update(dst for src, dst in zip(part.Coverage.glyphs, part.Substitute) if src in glyphs)
        if "glyf" in font:
            for name in list(glyphs):
                if name in font["glyf"].glyphs:
                    g = font["glyf"][name]
                    if g.isComposite():
                        expanded.update(comp.glyphName for comp in g.components)
        if expanded == glyphs:
            return glyphs
        glyphs = expanded


def _subtable_empty(part: Any) -> bool:
    for field in ("mapping", "alternates", "ligatures"):
        val = getattr(part, field, None)
        if val is not None:
            return not val
    for field in (
        "SubRuleSet", "SubClassSet", "ChainSubRuleSet", "ChainSubClassSet",
        "PosRuleSet", "PosClassSet", "ChainPosRuleSet", "ChainPosClassSet",
    ):
        val = getattr(part, field, None)
        if val is not None:
            return not any(val)
    for field in ("Coverage", "MarkCoverage", "Mark1Coverage", "InputCoverage"):
        val = getattr(part, field, None)
        if isinstance(val, list):
            return not val or any(not cov.glyphs for cov in val)
        if val is not None:
            return not val.glyphs
    return False


def _subtable_unreachable(part: Any, reachable: set[str]) -> bool:
    needed = []
    for field in ("BacktrackCoverage", "InputCoverage", "LookAheadCoverage"):
        needed.extend(cov.glyphs for cov in getattr(part, field, None) or [])
    for field in ("Coverage", "MarkCoverage", "Mark1Coverage"):
        val = getattr(part, field, None)
        if val is not None:
            needed.extend(cov.glyphs for cov in (val if isinstance(val, list) else [val]))
    for field in ("mapping", "alternates", "ligatures"):
        val = getattr(part, field, None)
        if val is not None:
            needed.append(list(val))
    return any(not reachable.intersection(glyphs) for glyphs in needed)


def remove_empty_lookups(font: TTFont) -> int:
    """移除无法从 cmap + UVS + GSUB 闭包触达的空/死 lookup 引用。"""
    removed = 0
    cmap = font.getBestCmap() if "cmap" in font else None
    seeds = (set(cmap.values()) | uvs_target_glyphs(font) | {font.getGlyphOrder()[0]}) if cmap else None
    reachable = substitution_closure(font, seeds) if seeds else None
    for tag in ("GSUB", "GPOS"):
        if tag not in font or not font[tag].table.FeatureList or not font[tag].table.LookupList:
            continue
        table = font[tag].table
        dead = {
            idx
            for idx, lookup in enumerate(table.LookupList.Lookup)
            if all(
                _subtable_empty(part) or (reachable is not None and _subtable_unreachable(part, reachable))
                for part in (getattr(p, "ExtSubTable", p) for p in lookup.SubTable)
            )
        }
        for rec in table.FeatureList.FeatureRecord:
            kept = [idx for idx in rec.Feature.LookupListIndex if idx not in dead]
            removed += len(rec.Feature.LookupListIndex) - len(kept)
            rec.Feature.LookupListIndex = kept
            rec.Feature.LookupCount = len(kept)
    return removed


def prune_unreferenced_lookups(font: TTFont) -> int:
    removed = 0
    for tag in ("GSUB", "GPOS"):
        if tag not in font or not font[tag].table.ScriptList or not font[tag].table.FeatureList:
            continue
        table = font[tag].table
        if getattr(table, "FeatureVariations", None) is not None:
            raise ValueError("清理 lookup 前必须显式处理 FeatureVariations")
        systems = [
            sys_obj
            for srec in table.ScriptList.ScriptRecord
            for sys_obj in ([srec.Script.DefaultLangSys] + [e.LangSys for e in srec.Script.LangSysRecord])
            if sys_obj is not None
        ]
        active = sorted({
            idx for sys_obj in systems for idx in [*sys_obj.FeatureIndex, sys_obj.ReqFeatureIndex] if idx != 0xFFFF
        })
        fremap = {old: new for new, old in enumerate(active)}
        table.FeatureList.FeatureRecord = [table.FeatureList.FeatureRecord[i] for i in active]
        table.FeatureList.FeatureCount = len(active)
        for sys_obj in systems:
            sys_obj.FeatureIndex = [fremap[i] for i in sys_obj.FeatureIndex]
            if sys_obj.ReqFeatureIndex != 0xFFFF:
                sys_obj.ReqFeatureIndex = fremap[sys_obj.ReqFeatureIndex]
        roots = {idx for rec in table.FeatureList.FeatureRecord for idx in rec.Feature.LookupListIndex}
        keep = table.LookupList.closure_lookups(sorted(roots))
        lremap = {old: new for new, old in enumerate(keep)}
        removed += len(table.LookupList.Lookup) - len(keep)
        table.LookupList.subset_lookups(keep)
        for rec in table.FeatureList.FeatureRecord:
            rec.Feature.LookupListIndex = [lremap[i] for i in rec.Feature.LookupListIndex]
            rec.Feature.LookupCount = len(rec.Feature.LookupListIndex)
    return removed


def sort_pair_sets(font: TTFont) -> int:
    """按第二字形 GID 升序排列所有 GPOS Format 1 PairSet（修复上游漏读字偶距）。"""
    changed = 0
    if "GPOS" not in font or not font["GPOS"].table.LookupList:
        return changed
    for lookup in font["GPOS"].table.LookupList.Lookup:
        for outer in lookup.SubTable:
            part = getattr(outer, "ExtSubTable", outer)
            if not isinstance(part, ot.PairPos) or part.Format != 1:
                continue
            for pair_set in part.PairSet:
                records = pair_set.PairValueRecord
                if len({r.SecondGlyph for r in records}) != len(records):
                    raise ValueError("GPOS PairSet 含有重复的第二字形")
                ordered = sorted(records, key=lambda r: font.getGlyphID(r.SecondGlyph))
                changed += int([r.SecondGlyph for r in records] != [r.SecondGlyph for r in ordered])
                pair_set.PairValueRecord = ordered
                pair_set.PairValueCount = len(ordered)
    return changed


def inherit_glyph_aliases(font: TTFont, aliases: dict[str, set[str]]) -> None:
    """拆分 cmap 别名后，同步继承 GSUB/GPOS/GDEF 中的布局输入与记录（带 C23.7 守卫）。"""
    order = {name: i for i, name in enumerate(font.getGlyphOrder())}
    expanded = {src: set(dsts) for src, dsts in aliases.items()}
    while True:
        nxt = {src: dsts | {n for d in dsts for n in expanded.get(d, ())} for src, dsts in expanded.items()}
        if any(src in dsts for src, dsts in nxt.items()):
            raise ValueError("字形别名包含循环")
        if nxt == expanded:
            break
        expanded = nxt
    aliases = expanded

    def choices(name: str) -> list[str]:
        return [name, *sorted(aliases.get(name, ()), key=order.__getitem__)]

    def seq_variants(names: list[str]):
        opts = [choices(n) for n in names]
        if prod(map(len, opts)) > 4096:
            raise ValueError("字形别名的上下文展开超过 4096 上限")
        return list(product(*opts))

    seen: set[int] = set()

    def expand_aligned(owner: Any, field: str) -> None:
        cov = getattr(owner, field, None)
        if not isinstance(cov, ot.Coverage):
            return
        original = list(cov.glyphs)
        added = {new: i for i, old in enumerate(original) for new in aliases.get(old, ()) if new not in original}
        if not added:
            return
        # 若多个子表共享同一 Coverage，先独立深拷贝再修改，避免平行数组失配（C23.7）
        cov = copy.deepcopy(cov)
        setattr(owner, field, cov)
        indices = {name: i for i, name in enumerate(original)} | added
        names = sorted(indices, key=order.__getitem__)
        for path, count_attr in ALIGNED_COVERAGES[field]:
            parts = path.split(".")
            parent = owner
            for p in parts[:-1]:
                parent = getattr(parent, p, None)
            values = getattr(parent, parts[-1], None) if parent is not None else None
            if isinstance(values, list):
                if len(values) != len(original):
                    raise ValueError(f"布局覆盖与记录数量不一致：{field}/{path}")
                setattr(parent, parts[-1], [copy.deepcopy(values[indices[n]]) for n in names])
                setattr(parent, count_attr, len(names))
        cov.glyphs = names

    def visit(node: Any) -> None:
        if node is None or isinstance(node, (str, int, float, bool, bytes)) or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ot.Coverage):
            node.glyphs = sorted({n for old in node.glyphs for n in choices(old)}, key=order.__getitem__)
            return
        for field in ALIGNED_COVERAGES:
            expand_aligned(node, field)
        for field in ("mapping", "alternates", "classDefs"):
            mapping = getattr(node, field, None)
            if isinstance(mapping, dict):
                for old, val in list(mapping.items()):
                    for new in aliases.get(old, ()):
                        if new in mapping:
                            continue  # 不覆盖别名已有规则（C23.7）
                        if isinstance(val, list):
                            mapping[new] = [new if item == old else item for item in val]
                        else:
                            mapping[new] = new if val == old else copy.deepcopy(val)
        ligs = getattr(node, "ligatures", None)
        if isinstance(ligs, dict):
            new_ligs: dict[str, list[Any]] = {}
            for first, rules in ligs.items():
                for rule in rules:
                    for names in seq_variants([first, *rule.Component]):
                        cloned = copy.deepcopy(rule)
                        cloned.Component = list(names[1:])
                        new_ligs.setdefault(names[0], []).append(cloned)
            node.ligatures = new_ligs
        pairs = getattr(node, "PairValueRecord", None)
        if isinstance(pairs, list):
            by_sec = {r.SecondGlyph: r for r in pairs}
            for r in pairs:
                for name in aliases.get(r.SecondGlyph, ()):
                    if name not in by_sec:
                        cloned = copy.deepcopy(r)
                        cloned.SecondGlyph = name
                        by_sec[name] = cloned
            node.PairValueRecord = [by_sec[n] for n in sorted(by_sec, key=order.__getitem__)]
            node.PairValueCount = len(node.PairValueRecord)
        for field in ("SubRule", "ChainSubRule", "PosRule", "ChainPosRule"):
            rules = getattr(node, field, None)
            if not isinstance(rules, list):
                continue
            out_rules = []
            for rule in rules:
                rfields = [
                    k for k in ("Input", "Backtrack", "LookAhead")
                    if isinstance(getattr(rule, k, None), list) and all(isinstance(v, str) for v in getattr(rule, k))
                ]
                var_lists = [seq_variants(getattr(rule, k)) for k in rfields]
                if prod(map(len, var_lists)) > 4096:
                    raise ValueError("链式规则展开总数超过 4096 上限")
                for combo in product(*var_lists):
                    cloned = copy.deepcopy(rule)
                    for k, vals in zip(rfields, combo):
                        setattr(cloned, k, list(vals))
                    out_rules.append(cloned)
            setattr(node, field, out_rules)
            setattr(node, field + "Count", len(out_rules))
        children = (
            list(node.values())
            if isinstance(node, dict)
            else list(node)
            if isinstance(node, (list, tuple))
            else [v for k, v in vars(node).items() if not k.startswith("_") and k not in {"reader", "font"}]
        )
        for child in children:
            visit(child)

    for tag in ("GSUB", "GPOS", "GDEF"):
        if tag in font:
            font[tag].ensureDecompiled()
            visit(font[tag].table)
