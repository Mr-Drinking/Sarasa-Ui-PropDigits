"""OpenType feature routing by script/language/tag, independent of record order."""
from __future__ import annotations

import copy
from dataclasses import dataclass

from fontTools import subset  # Registers the OpenType lookup closure methods.
from fontTools.ttLib.tables import otTables as ot


@dataclass
class Feature:
    lookups: tuple[int, ...]
    params: object = None
    required: bool = False


def combine(left: Feature, right: Feature) -> Feature:
    if left.params is not None and right.params is not None and left.params != right.params:
        raise ValueError("同一语言的 OpenType 特性参数冲突")
    return Feature(tuple(dict.fromkeys((*left.lookups, *right.lookups))),
                   left.params if left.params is not None else right.params,
                   left.required or right.required)


def routes(table) -> dict[tuple[str, str], dict[str, Feature]]:
    result = {}
    if not table.ScriptList or not table.FeatureList:
        return result
    for script_record in table.ScriptList.ScriptRecord:
        script = script_record.Script
        systems = [("dflt", script.DefaultLangSys)] if script.DefaultLangSys is not None else []
        systems += [(record.LangSysTag, record.LangSys) for record in script.LangSysRecord]
        for language, system in systems:
            features = {}
            indices = list(system.FeatureIndex)
            required = system.ReqFeatureIndex
            if required != 0xFFFF and required not in indices:
                indices.append(required)
            for index in indices:
                record = table.FeatureList.FeatureRecord[index]
                feature = Feature(tuple(record.Feature.LookupListIndex),
                                  record.Feature.FeatureParams, index == required)
                features[record.FeatureTag] = combine(features[record.FeatureTag], feature) if record.FeatureTag in features else feature
            result[(script_record.ScriptTag, language)] = features
    return result


def selected(mapping, script, language):
    selected_script = script if any(key[0] == script for key in mapping) else "DFLT"
    return mapping.get((selected_script, language), mapping.get((selected_script, "dflt"), {}))


def install(table, mapping) -> None:
    if getattr(table, "FeatureVariations", None) is not None:
        raise ValueError("重建特性路由前必须显式处理 FeatureVariations")
    feature_records = []
    bindings = {}
    for key, features in sorted(mapping.items()):
        bindings[key] = []
        for tag, feature in sorted(features.items()):
            index = next((i for i, record in enumerate(feature_records)
                          if record.FeatureTag == tag
                          and tuple(record.Feature.LookupListIndex) == feature.lookups
                          and record.Feature.FeatureParams == feature.params), None)
            if index is None:
                record = ot.FeatureRecord(); record.FeatureTag = tag
                record.Feature = ot.Feature()
                record.Feature.FeatureParams = copy.deepcopy(feature.params)
                record.Feature.LookupListIndex = list(feature.lookups)
                record.Feature.LookupCount = len(feature.lookups)
                index = len(feature_records); feature_records.append(record)
            bindings[key].append((index, feature.required))
    order = sorted(range(len(feature_records)), key=lambda index: feature_records[index].FeatureTag)
    remap = {old: new for new, old in enumerate(order)}
    table.FeatureList = ot.FeatureList()
    table.FeatureList.FeatureRecord = [feature_records[index] for index in order]
    table.FeatureList.FeatureCount = len(order)
    table.ScriptList = ot.ScriptList(); table.ScriptList.ScriptRecord = []
    for script_tag in sorted({script for script, _language in mapping}):
        record = ot.ScriptRecord(); record.ScriptTag = script_tag
        record.Script = ot.Script(); record.Script.DefaultLangSys = None; record.Script.LangSysRecord = []
        for (script, language), references in sorted(bindings.items()):
            if script != script_tag:
                continue
            system = ot.LangSys(); system.LookupOrder = None
            required = [remap[index] for index, flag in references if flag]
            if len(required) > 1:
                raise ValueError("语言系统包含多个必要特性，必须显式合并")
            system.ReqFeatureIndex = required[0] if required else 0xFFFF
            system.FeatureIndex = sorted(remap[index] for index, flag in references if not flag)
            system.FeatureCount = len(system.FeatureIndex)
            if language == "dflt":
                record.Script.DefaultLangSys = system
            else:
                entry = ot.LangSysRecord(); entry.LangSysTag = language; entry.LangSys = system
                record.Script.LangSysRecord.append(entry)
        record.Script.LangSysCount = len(record.Script.LangSysRecord)
        table.ScriptList.ScriptRecord.append(record)
    table.ScriptList.ScriptCount = len(table.ScriptList.ScriptRecord)


def single_substitution_lookup(table, index) -> dict[str, str] | None:
    mapping = {}
    for subtable in table.LookupList.Lookup[index].SubTable:
        subtable = getattr(subtable, "ExtSubTable", subtable)
        if not isinstance(subtable, ot.SingleSubst):
            return None
        for source, target in subtable.mapping.items():
            mapping.setdefault(source, target)
    return mapping


def contextual_lookups(table, lookups) -> tuple[int, ...]:
    return tuple(index for index in sorted(set(lookups)) if single_substitution_lookup(table, index) is None)


def single_substitution_effect(table, lookups) -> dict[str, str]:
    """The glyph each input becomes through the single substitutions among
    these lookups, in lookup order.

    Other lookup types depend on context and are compared by identity
    (contextual_lookups), not glyph by glyph.
    """
    steps = [mapping for index in sorted(set(lookups))
             if (mapping := single_substitution_lookup(table, index)) is not None]
    effect = {}
    for source in set().union(*steps):
        glyph = source
        for mapping in steps:
            glyph = mapping.get(glyph, glyph)
        if glyph != source:
            effect[source] = glyph
    return effect


def merge_routes(table, incoming) -> None:
    current = routes(table)
    merged = {}
    scripts = {script for script, _ in current} | {script for script, _ in incoming}
    for script in scripts:
        languages = {language for source in (current, incoming) for source_script, language in source
                     if source_script in {script, "DFLT"}}
        for language in languages:
            features = copy.deepcopy(selected(current, script, language))
            for tag, feature in selected(incoming, script, language).items():
                features[tag] = combine(features[tag], feature) if tag in features else copy.deepcopy(feature)
            merged[(script, language)] = features
    install(table, merged)


def substitution_closure(font, initial: set[str]) -> set[str]:
    """Reach glyphs produced from a repertoire, including unencoded forms."""
    glyphs = set(initial)
    subtables = []
    if 'GSUB' in font:
        for lookup in font['GSUB'].table.LookupList.Lookup:
            subtables.extend(part.ExtSubTable if lookup.LookupType == 7 else part for part in lookup.SubTable)
    while True:
        expanded = set(glyphs)
        for part in subtables:
            for field in ('mapping', 'alternates'):
                for source, target in getattr(part, field, {}).items():
                    if source in glyphs:
                        expanded.update([target] if isinstance(target, str) else target)
            for source, rules in getattr(part, 'ligatures', {}).items():
                if source in glyphs:
                    expanded.update(rule.LigGlyph for rule in rules if all(name in glyphs for name in rule.Component))
            if hasattr(part, 'Substitute'):
                expanded.update(target for source, target in zip(part.Coverage.glyphs, part.Substitute) if source in glyphs)
        if 'glyf' in font:
            for name in glyphs:
                glyph = font['glyf'][name]
                if glyph.isComposite():
                    expanded.update(component.glyphName for component in glyph.components)
        if expanded == glyphs:
            return glyphs
        glyphs = expanded


def feature_substitution_edges(font, tag):
    """Include child lookups reached through contextual GSUB rules."""
    if 'GSUB' not in font:
        return {}, {}
    table = font['GSUB'].table
    roots = sorted({index for record in table.FeatureList.FeatureRecord if record.FeatureTag == tag
                    for index in record.Feature.LookupListIndex})
    singles, ligatures = {}, {}
    for index in table.LookupList.closure_lookups(roots):
        lookup = table.LookupList.Lookup[index]
        for outer in lookup.SubTable:
            part = getattr(outer, 'ExtSubTable', outer)
            for original, replacement in getattr(part, 'mapping', {}).items():
                if isinstance(replacement, str):
                    singles.setdefault(original, set()).add(replacement)
            for first, rules in getattr(part, 'ligatures', {}).items():
                for rule in rules:
                    ligatures.setdefault((first, *rule.Component), set()).add(rule.LigGlyph)
    # Ambiguous language/style alternatives require more context; never select
    # a glyph merely because its record happened to appear last.
    return ({name: next(iter(values)) for name, values in singles.items() if len(values) == 1},
            {sequence: next(iter(values)) for sequence, values in ligatures.items() if len(values) == 1})


def prune_unreferenced_lookups(font) -> int:
    removed = 0
    for tag in ('GSUB', 'GPOS'):
        if tag not in font:
            continue
        table = font[tag].table
        if getattr(table, 'FeatureVariations', None) is not None:
            raise ValueError('清理 lookup 前必须显式处理 FeatureVariations')
        systems = [system for record in table.ScriptList.ScriptRecord
                   for system in ([record.Script.DefaultLangSys] + [entry.LangSys for entry in record.Script.LangSysRecord])
                   if system is not None]
        active = sorted({index for system in systems
                         for index in [*system.FeatureIndex, system.ReqFeatureIndex] if index != 0xFFFF})
        feature_remap = {old: new for new, old in enumerate(active)}
        table.FeatureList.FeatureRecord = [table.FeatureList.FeatureRecord[index] for index in active]
        table.FeatureList.FeatureCount = len(active)
        for system in systems:
            system.FeatureIndex = [feature_remap[index] for index in system.FeatureIndex]
            if system.ReqFeatureIndex != 0xFFFF:
                system.ReqFeatureIndex = feature_remap[system.ReqFeatureIndex]
        roots = {index for record in table.FeatureList.FeatureRecord for index in record.Feature.LookupListIndex}
        keep = table.LookupList.closure_lookups(sorted(roots))
        remap = {old: new for new, old in enumerate(keep)}
        removed += len(table.LookupList.Lookup) - len(keep)
        table.LookupList.subset_lookups(keep)
        for record in table.FeatureList.FeatureRecord:
            record.Feature.LookupListIndex = [remap[index] for index in record.Feature.LookupListIndex]
            record.Feature.LookupCount = len(record.Feature.LookupListIndex)
    return removed


def sort_pair_sets(font) -> int:
    """Restore the GPOS PairSet binary-search order after glyph reordering.

    PairValueRecords are sorted by the second glyph ID, not by glyph name or
    by their order in an earlier source font. A valid coverage table alone
    does not guarantee that the runtime can find the stored pair value.
    """
    changed = 0
    if 'GPOS' not in font:
        return changed
    for lookup in font['GPOS'].table.LookupList.Lookup:
        for outer in lookup.SubTable:
            part = getattr(outer, 'ExtSubTable', outer)
            if not isinstance(part, ot.PairPos) or part.Format != 1:
                continue
            for pair_set in part.PairSet:
                records = pair_set.PairValueRecord
                if len({record.SecondGlyph for record in records}) != len(records):
                    raise ValueError('GPOS PairSet 含有重复的第二字形')
                ordered = sorted(records, key=lambda record: font.getGlyphID(record.SecondGlyph))
                changed += [r.SecondGlyph for r in records] != [r.SecondGlyph for r in ordered]
                pair_set.PairValueRecord = ordered
                pair_set.PairValueCount = len(ordered)
    return changed
