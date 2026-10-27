"""Copy layout input identities when a cmap alias needs its own glyph data."""
from __future__ import annotations

import copy
from itertools import product
from math import prod

from fontTools.ttLib.tables import otTables as ot


ALIGNED_COVERAGES = {
    'Coverage': [('Value', 'ValueCount'), ('PairSet', 'PairSetCount'),
                 ('EntryExitRecord', 'EntryExitCount'), ('Substitute', 'GlyphCount'),
                 ('SubRuleSet', 'SubRuleSetCount'), ('ChainSubRuleSet', 'ChainSubRuleSetCount'),
                 ('PosRuleSet', 'PosRuleSetCount'), ('ChainPosRuleSet', 'ChainPosRuleSetCount'),
                 ('AttachPoint', 'GlyphCount'), ('LigGlyph', 'LigGlyphCount')],
    'MarkCoverage': [('MarkArray.MarkRecord', 'MarkCount')],
    'BaseCoverage': [('BaseArray.BaseRecord', 'BaseCount')],
    'LigatureCoverage': [('LigatureArray.LigatureAttach', 'LigatureCount')],
    'Mark1Coverage': [('Mark1Array.MarkRecord', 'MarkCount')],
    'Mark2Coverage': [('Mark2Array.Mark2Record', 'Mark2Count')],
}


def inherit_inputs(font, aliases: dict[str, set[str]]) -> None:
    order = {name: i for i, name in enumerate(font.getGlyphOrder())}
    aliases = {source: set(targets) for source, targets in aliases.items()}
    while True:
        expanded = {source: targets | {name for target in targets for name in aliases.get(target, ())}
                    for source, targets in aliases.items()}
        if any(source in targets for source, targets in expanded.items()):
            raise ValueError('字形别名包含循环')
        if expanded == aliases:
            break
        aliases = expanded
    choices = lambda name: [name, *sorted(aliases.get(name, ()), key=order.__getitem__)]
    seen = set()

    def sequences(names):
        options = [choices(name) for name in names]
        if prod(map(len, options)) > 4096:
            raise ValueError('字形别名的上下文展开超过限制')
        return product(*options)

    def coverage(owner, field):
        cov = getattr(owner, field, None)
        if not isinstance(cov, ot.Coverage):
            return
        original = list(cov.glyphs)
        added = {new: i for i, old in enumerate(original) for new in aliases.get(old, ()) if new not in original}
        if not added:
            return
        indices = {name: i for i, name in enumerate(original)} | added
        names = sorted(indices, key=order.__getitem__)
        for path, count in ALIGNED_COVERAGES[field]:
            parts = path.split('.')
            parent = owner
            for part in parts[:-1]:
                parent = getattr(parent, part, None)
            values = getattr(parent, parts[-1], None) if parent is not None else None
            if isinstance(values, list):
                if len(values) != len(original):
                    raise ValueError(f'布局覆盖与记录数量不一致：{field}/{path}')
                setattr(parent, parts[-1], [copy.deepcopy(values[indices[name]]) for name in names])
                setattr(parent, count, len(names))
        cov.glyphs = names

    def visit(node):
        if node is None or isinstance(node, (str, int, float, bool, bytes)) or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ot.Coverage):
            node.glyphs = sorted({name for old in node.glyphs for name in choices(old)}, key=order.__getitem__)
            return
        for field in ALIGNED_COVERAGES:
            coverage(node, field)
        for field in ('mapping', 'alternates', 'classDefs'):
            mapping = getattr(node, field, None)
            if isinstance(mapping, dict):
                for old, value in list(mapping.items()):
                    for new in aliases.get(old, ()):
                        mapping[new] = new if value == old else copy.deepcopy(value)
        ligatures = getattr(node, 'ligatures', None)
        if isinstance(ligatures, dict):
            expanded = {}
            for first, rules in ligatures.items():
                for rule in rules:
                    for names in sequences([first, *rule.Component]):
                        cloned = copy.deepcopy(rule); cloned.Component = list(names[1:])
                        expanded.setdefault(names[0], []).append(cloned)
            node.ligatures = expanded
        pairs = getattr(node, 'PairValueRecord', None)
        if isinstance(pairs, list):
            expanded = {record.SecondGlyph: record for record in pairs}
            for record in pairs:
                for name in aliases.get(record.SecondGlyph, ()):
                    cloned = copy.deepcopy(record); cloned.SecondGlyph = name; expanded[name] = cloned
            node.PairValueRecord = [expanded[name] for name in sorted(expanded, key=order.__getitem__)]
            node.PairValueCount = len(node.PairValueRecord)
        for field in ('SubRule', 'ChainSubRule', 'PosRule', 'ChainPosRule'):
            rules = getattr(node, field, None)
            if not isinstance(rules, list):
                continue
            expanded = []
            for rule in rules:
                fields = [key for key in ('Input', 'Backtrack', 'LookAhead')
                          if isinstance(getattr(rule, key, None), list)
                          and all(isinstance(value, str) for value in getattr(rule, key))]
                variants = [list(sequences(getattr(rule, key))) for key in fields]
                for variant in product(*variants):
                    cloned = copy.deepcopy(rule)
                    for key, values in zip(fields, variant):
                        setattr(cloned, key, list(values))
                    expanded.append(cloned)
            setattr(node, field, expanded); setattr(node, field + 'Count', len(expanded))
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, (list, tuple)):
            children = list(node)
        else:
            children = [value for name, value in vars(node).items() if not name.startswith('_') and name not in {'reader', 'font'}]
        for child in children:
            visit(child)

    for tag in ('GSUB', 'GPOS', 'GDEF'):
        if tag in font:
            font[tag].ensureDecompiled()
            visit(font[tag].table)
