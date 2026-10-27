"""Independent audit orchestration; incomplete stages can never publish."""
from __future__ import annotations

import concurrent.futures
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from fontTools.ttLib import TTFont

from . import contract as c
from . import geometry, metadata, metrics, native, products, provenance, raster
from .comparison import Sources
from .differential import compare_pair, default_contexts, source_sequences
from .outlines import compare_outlines
from .oracles import IDEOGRAPH_RANGES, in_ranges, cjk_weight
from .gate import SECTIONS, digest, summarize


@dataclass(frozen=True)
class FontCase:
    region: str
    italic: bool
    weight: str | None = None
    hinted: bool = False

    @property
    def path(self):
        return c.vf_path(self.region, self.italic) if self.weight is None else c.static_path(self.region, self.weight, self.italic, self.hinted)

    @property
    def key(self):
        return c.display_path(self.path)

    @property
    def weights(self):
        return c.AXIS_POINTS if self.weight is None else (c.WEIGHTS[self.weight],)


def fonts(regions=c.REGIONS):
    return [FontCase(region, italic, weight, hinted) for region in regions
            for hinted in (False, True) for weight in c.WEIGHTS for italic in (False, True)] + [
                FontCase(region, italic) for region in regions for italic in (False, True)]


def paired_key(region, italic, weight):
    return f'{c.display_path(c.vf_path(region, italic))}@{weight}'


def plan(regions=c.REGIONS):
    """Expected records and minimum case counts, derived from actual inventories."""
    cases = fonts(regions)
    all_fonts = {font.key: 1 for font in cases}
    variable = {font.key: len(c.AXIS_POINTS) for font in cases if font.weight is None}
    static = {font.key: 1 for font in cases if font.weight is not None}
    paired = {paired_key(region, italic, weight): 1
              for region in regions for italic in (False, True) for weight in c.WEIGHTS.values()}
    result = {name: dict(all_fonts) for name in SECTIONS}
    result['source_provenance'] = {'fixed-sources': sum(map(len, provenance.source_groups().values()))}
    result['licenses'] = {'license-documents': len(c.release_license_document_paths())}
    for name in ('static_outlines', 'raster'):
        result[name] = dict(static)
    for name in ('horizontal_metrics', 'vertical_metrics', 'shaping_default', 'shaping_languages',
                 'shaping_features', 'gsub_sequences', 'gpos_values'):
        result[name] = dict(paired)
    result['classical_outlines'] = {case.key: 1 for case in cases if case.region == 'CL'}
    for name in ('inter_axis_curve', 'cross_engine_metrics', 'style_geometry'):
        result[name] = dict(variable)
    result['hint_execution'] = {case.key: 1 for case in cases if case.hinted}
    for case in cases:
        result['metadata'][case.key] = 21 if case.weight is None else 18 + (case.weight == 'Regular')
        result['digit_features'][case.key] = 114 * len(case.weights)
        result['colon_context'][case.key] = len(products.COLON_TEXTS) * 2 * len(products.WIDTH_FEATURES) * len(case.weights)
        result['inter_positioning'][case.key] = (5 * len(products.KERN_TEXTS) + len(products.MARK_TEXTS)) * len(case.weights)
        result['vertical_joining'][case.key] = 7 * 8 * 3 * len(products.VERTICAL_DASH_RUN_TEXTS) * len(case.weights)
        if case.path.is_file():
            with TTFont(case.path, lazy=True) as font:
                count = font['maxp'].numGlyphs
                result['region_boundaries'][case.key] = len(font.getBestCmap())
                if case.weight is None:
                    result['cross_engine_metrics'][case.key] = count * len(case.weights)
                    composites = sum(font['glyf'][name].isComposite() for name in font.getGlyphOrder())
                    result['style_geometry'][case.key] = composites * len(c.AXIS_POINTS)
                elif case.hinted:
                    regressions = len({font.getGlyphID(font.getBestCmap()[ord(char)]) for char in raster.REGRESSION_TEXT
                                       if ord(char) in font.getBestCmap()})
                    result['hint_execution'][case.key] = (count * 2 + regressions * 193) * len(raster.MODES)
    contexts = default_contexts()
    for region in regions:
        for italic in (False, True):
            path = c.reference_path(region, 'Regular', italic)
            if not path.is_file():
                continue
            with TTFont(path, lazy=True) as font:
                repertoire = set(font.getBestCmap())
            probes = (path, c.cjk_path(region), c.inter_path(italic))
            sequences = sum(all(ord(char) in repertoire for char in text) for text in source_sequences(probes)) if all(path.is_file() for path in probes) else 1
            total = len(repertoire) + sequences
            with TTFont(c.inter_path(italic), lazy=True) as inter:
                inter_repertoire = set(inter.getBestCmap())
            result['inter_axis_curve'][c.display_path(c.vf_path(region, italic))] = 141 * sum(
                cp in inter_repertoire and (0x20 <= cp <= 0x2FF and cp != 0xB7 or 0x370 <= cp <= 0x52F)
                for cp in repertoire)
            for weight in c.WEIGHTS.values():
                key = paired_key(region, italic, weight)
                result['horizontal_metrics'][key] = len(repertoire)
                result['vertical_metrics'][key] = len(repertoire)
                result['shaping_default'][key] = total * sum(ctx.feature == 'default' and ctx.language == 'und' for ctx in contexts)
                result['shaping_languages'][key] = total * sum(ctx.feature == 'default' and ctx.language != 'und' for ctx in contexts)
                result['shaping_features'][key] = total * sum(ctx.feature != 'default' for ctx in contexts) + products.SPACING_CASES
                result['gsub_sequences'][key] = sequences * len(contexts)
                result['gpos_values'][key] = total * len(contexts)
            for weight_name in c.WEIGHTS:
                for hinted in (False, True):
                    path = c.static_path(region, weight_name, italic, hinted)
                    result['static_outlines'][c.display_path(path)] = total * len(contexts) + (
                        len(repertoire) if weight_name == 'Heavy' else 0)
                    result['raster'][c.display_path(path)] = len(repertoire) * len(raster.RASTER_PPEMS)
            if region == 'CL':
                with TTFont(c.SOURCE_ROOT / 'shanggu-1.028/ShangguSansTC-VF.ttf', lazy=True) as shanggu:
                    classical = sum(cp in shanggu.getBestCmap() and in_ranges(cp, IDEOGRAPH_RANGES) for cp in repertoire)
                for case in cases:
                    if case.region == region and case.italic == italic:
                        result['classical_outlines'][case.key] = classical * (len(c.WEIGHTS) if case.weight is None else 1)
    return result


MISSING = {'cases': 0, 'failures': 1, 'samples': [{'reason': '缺少成品字体'}]}


def region_boundary(case):
    with TTFont(case.path, lazy=True) as actual, TTFont(c.reference_path(case.region, 'Regular', False), lazy=True) as reference:
        left, right = set(actual.getBestCmap()), set(reference.getBestCmap())
    missing, extra = sorted(right - left), sorted(left - right)
    return {'cases': len(right), 'failures': len(missing) + len(extra),
            'samples': ([{'missing': missing[:24], 'extra': extra[:24]}] if missing or extra else [])}


def vertical_joining(case):
    rows = [products.vertical_segment_runtime_status(case.path,
            {'wght': weight} if case.weight is None else None) for weight in case.weights]
    return {'cases': sum(row['cases'] for row in rows), 'failures': sum(row['failures'] for row in rows),
            'samples': [sample for row in rows for sample in row['samples']][:24], 'weights': list(case.weights)}


def classical_outlines(case):
    boundary = c.reference_path('CL', 'Regular', False)
    variable_source = c.SOURCE_ROOT / 'shanggu-1.028/ShangguSansTC-VF.ttf'
    with TTFont(boundary, lazy=True) as reference, TTFont(variable_source, lazy=True) as source:
        repertoire = {cp for cp in reference.getBestCmap() if cp in source.getBestCmap() and in_ranges(cp, IDEOGRAPH_RANGES)}
        source_weights = {weight: cjk_weight(weight, source) for weight in c.WEIGHTS.values()}
    values = []
    if case.weight is None:
        for weight in c.WEIGHTS.values():
            measured = compare_outlines(case.path, variable_source, codepoints=repertoire,
                         actual_weight=weight, source_weight=source_weights[weight],
                         source_shear=case.italic, allow_translation=True, tolerance=2)
            values.append({'weight': weight, **measured})
    else:
        name = 'Medium' if case.weight == 'SemiBold' else case.weight
        path = c.SOURCE_ROOT / f'shanggu-1.028/static/ShangguSansTC-{name}.ttf'
        measured = compare_outlines(case.path, path, codepoints=repertoire,
                                    source_shear=case.italic, allow_translation=True,
                                    tolerance=1.01 if case.italic else 0)
        values.append({'weight': c.WEIGHTS[case.weight], **measured})
    return {'cases': sum(value['cases'] for value in values), 'failures': sum(value['failures'] for value in values),
            'samples': [sample for value in values for sample in value['samples']][:24], 'weights': values}


ACTIONS = {
    'metadata': lambda case: metadata.check_font(case.path, case.region, case.italic, case.weight, case.hinted),
    'region_boundaries': region_boundary,
    'digit_features': lambda case: products.width_invariants(case.path),
    'colon_context': lambda case: products.colon_invariants(case.path),
    'inter_positioning': lambda case: products.positioning_invariants(case.path),
    'vertical_joining': vertical_joining,
    'inter_axis_curve': lambda case: products.inter_axis_metrics(case.path),
    'cross_engine_metrics': lambda case: metrics.cross_engine_metrics(case.path),
    'hint_execution': lambda case: raster.execute_hint_programs(
        case.path, reference=None if case.weight == 'Heavy' else c.reference_path(case.region, case.weight, case.italic, True)),
    'classical_outlines': classical_outlines,
    'style_geometry': lambda case: geometry.composite_interpolation(case.path),
    'raster': lambda case: raster.static_raster(case.path, case.region, case.weight, case.italic, case.hinted),
}
SHAPING_SECTIONS = ('shaping_default', 'shaping_languages', 'shaping_features', 'gsub_sequences', 'gpos_values')
METRIC_SECTIONS = ('horizontal_metrics', 'vertical_metrics')
# Expensive units first, so a pool is not left waiting on one long tail.
COST = {'shaping': 0, 'static_outlines': 1, 'raster': 2, 'cross_engine_metrics': 3, 'style_geometry': 3,
        'hint_execution': 4, 'classical_outlines': 4, 'metrics': 5, 'inter_axis_curve': 5}


@dataclass(frozen=True)
class Unit:
    kind: str
    region: str | None = None
    italic: bool = False
    weight: str | None = None
    hinted: bool = False

    @property
    def case(self):
        return FontCase(self.region, self.italic, self.weight, self.hinted)

    @property
    def sections(self):
        return {'shaping': SHAPING_SECTIONS, 'metrics': METRIC_SECTIONS}.get(self.kind, (self.kind,))

    @property
    def name(self):
        if self.kind in ('source_provenance', 'licenses'):
            return self.kind
        if self.kind in ('shaping', 'metrics'):
            return f'{self.kind}:{paired_key(self.region, self.italic, c.WEIGHTS[self.weight])}'
        return f'{self.kind}:{self.case.key}'

    def inputs(self) -> list[Path]:
        if self.kind in ('source_provenance', 'licenses'):
            return c.release_license_document_paths() if self.kind == 'licenses' else []
        if self.kind in ('shaping', 'metrics'):
            return [c.vf_path(self.region, self.italic), c.static_path(self.region, self.weight, self.italic, False)]
        paths = [self.case.path]
        if self.kind == 'hint_execution' and self.weight != 'Heavy':
            paths.append(c.reference_path(self.region, self.weight, self.italic, True))
        if self.kind == 'raster':
            if self.hinted and self.weight != 'Heavy':
                paths.extend(raster.pass1_candidates(self.region, self.weight, self.italic))
            else:
                paths.append(c.static_path(self.region, self.weight, self.italic, not self.hinted))
        return paths


def units(regions, selected):
    result = []
    for case in fonts(regions):
        for section in ('metadata', 'region_boundaries', 'digit_features', 'colon_context', 'inter_positioning',
                        'vertical_joining', 'inter_axis_curve', 'cross_engine_metrics', 'hint_execution',
                        'classical_outlines', 'style_geometry', 'raster', 'static_outlines'):
            if section not in selected:
                continue
            applies = {'inter_axis_curve': case.weight is None, 'cross_engine_metrics': case.weight is None,
                       'style_geometry': case.weight is None, 'hint_execution': case.hinted,
                       'classical_outlines': case.region == 'CL', 'raster': case.weight is not None,
                       'static_outlines': case.weight is not None}.get(section, True)
            if applies:
                result.append(Unit(section, case.region, case.italic, case.weight, case.hinted))
    for region in regions:
        for italic in (False, True):
            for weight in c.WEIGHTS:
                if set(SHAPING_SECTIONS) & selected:
                    result.append(Unit('shaping', region, italic, weight))
                if set(METRIC_SECTIONS) & selected:
                    result.append(Unit('metrics', region, italic, weight))
    result.extend(Unit(kind) for kind in ('source_provenance', 'licenses') if kind in selected)
    return sorted(result, key=lambda unit: (COST.get(unit.kind, 9), unit.name))


def shaping_unit(region, italic, weight_name):
    variable, static = c.vf_path(region, italic), c.static_path(region, weight_name, italic, False)
    weight = c.WEIGHTS[weight_name]
    source = Sources(region, italic, weight)
    try:
        measured = compare_pair(variable, static, weight=weight, outlines=True,
                                explanation=source.explains, sample_limit=12,
                                probe_sources=(c.reference_path(region, 'Regular', italic), c.cjk_path(region), c.inter_path(italic)))
    finally:
        source.close()
    spacing = products.spacing_parity(variable, static, weight)
    groups = {
        'shaping_default': [row for row in measured['contexts'] if row['feature'] == 'default' and row['language'] == 'und'],
        'shaping_languages': [row for row in measured['contexts'] if row['feature'] == 'default' and row['language'] != 'und'],
        'shaping_features': [row for row in measured['contexts'] if row['feature'] != 'default'],
        'gsub_sequences': measured['contexts'],
        'gpos_values': measured['contexts'],
    }
    records = {}
    for section, rows in groups.items():
        case_field = 'sequence_cases' if section == 'gsub_sequences' else 'cases'
        failure_field = 'sequence_failures' if section == 'gsub_sequences' else 'positioning_failures' if section == 'gpos_values' else 'failures'
        failures = sum(row[failure_field] for row in rows)
        samples = [sample for row in rows for sample in row['samples']
                   if (section != 'gsub_sequences' or len(sample['text']) > 1)
                   and (section != 'gpos_values' or set(sample['difference']) & {'advance', 'offset'})]
        record = {'cases': sum(row[case_field] for row in rows), 'failures': failures,
                  'codepoints': measured['codepoints'], 'sequences': measured['sequences'],
                  'contexts': [{k: v for k, v in row.items() if k != 'samples'} for row in rows]}
        if section == 'shaping_features':
            record['cases'] += spacing['cases']
            record['failures'] += spacing['failures']
            samples = samples + spacing['samples']
            record['contextual_spacing'] = {k: v for k, v in spacing.items() if k != 'samples'}
        record['samples'] = samples[:24] if record['failures'] else []
        records[section] = record
    return records


def metrics_unit(region, italic, weight_name):
    variable, static = c.vf_path(region, italic), c.static_path(region, weight_name, italic, False)
    weight = c.WEIGHTS[weight_name]
    return metrics.compare_nominal(metrics.nominal_metrics(variable, weight), metrics.nominal_metrics(static))


def static_outline_unit(case):
    if case.weight == 'Heavy':
        return native.heavy_sources(case.path, case.region, case.italic)
    reference = c.reference_path(case.region, case.weight, case.italic, case.hinted)
    if not reference.is_file():
        return {'cases': 0, 'failures': 1, 'samples': [{'reason': '缺少固定静态来源'}]}
    source = Sources(case.region, case.italic, c.WEIGHTS[case.weight], variable=False)
    try:
        # Probe sequences come from the release itself; positions from the
        # release after the OpenType-mandated PairSet ordering.
        report = compare_pair(case.path, native.sorted_pair_reference(reference), outlines=True, explanation=source.explains,
                              probe_sources=(reference, c.cjk_path(case.region), c.inter_path(case.italic)), sample_limit=12)
    finally:
        source.close()
    return {'cases': report['cases'], 'failures': report['failures'],
            'samples': [sample for row in report['contexts'] for sample in row['samples']][:24],
            'codepoints': report['codepoints'], 'sequences': report['sequences'],
            'contexts': [{k: v for k, v in row.items() if k != 'samples'} for row in report['contexts']]}


def execute(unit: Unit) -> dict[str, dict]:
    """Measure one independent unit; returns {section: record without id}."""
    if unit.kind == 'source_provenance':
        return {'source_provenance': {**provenance.verify_sources(), 'samples': []}}
    if unit.kind == 'licenses':
        return {'licenses': metadata.source_han_license_documents_status()}
    if any(not path.is_file() for path in unit.inputs()[:2 if unit.kind in ('shaping', 'metrics') else 1]):
        return {section: dict(MISSING) for section in unit.sections}
    if unit.kind == 'shaping':
        return shaping_unit(unit.region, unit.italic, unit.weight)
    if unit.kind == 'metrics':
        return metrics_unit(unit.region, unit.italic, unit.weight)
    if unit.kind == 'static_outlines':
        return {'static_outlines': static_outline_unit(unit.case)}
    return {unit.kind: ACTIONS[unit.kind](unit.case)}


def unit_key(unit: Unit, measurement_contract: str, provenance_digest: str) -> str:
    inputs = {c.display_path(path): c.sha256(path) if path.is_file() else None for path in unit.inputs()}
    return digest({'schema': 1, 'unit': unit.name, 'sections': list(unit.sections), 'inputs': inputs,
                   'contract': measurement_contract, 'provenance': provenance_digest})


def _run_unit(unit: Unit) -> tuple[dict, float]:
    started = time.monotonic()
    try:
        value = execute(unit)
    except Exception as error:  # noqa: BLE001 - a crashed measurement is a failure, not a pass
        value = {section: {'cases': 0, 'failures': 1, 'error': True,
                           'samples': [{'error': f'{type(error).__name__}: {error}'[:500]}]}
                 for section in unit.sections}
    return value, time.monotonic() - started


def load_checkpoint(path: Path | None, unit: Unit, key: str) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        stored = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None
    if stored.get('key') != key or stored.get('unit') != unit.name or set(stored.get('records', {})) != set(unit.sections):
        return None
    return stored['records']


def store_checkpoint(path: Path | None, unit: Unit, key: str, records: dict, seconds: float) -> None:
    if path is None:
        return
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'unit': unit.name, 'key': key, 'seconds': seconds, 'records': records},
                                    ensure_ascii=False), 'utf-8')
    temporary.replace(path)


def run(*, regions=c.REGIONS, selected=None, jobs=None, checkpoint_dir=None, log=print):
    """Execute every applicable unit; unexecuted sections stay visibly partial."""
    selected = set(SECTIONS if selected is None else selected)
    if selected - set(SECTIONS):
        raise ValueError('未知的审计章节')
    regions = tuple(regions)
    started = time.monotonic()
    log('[审计] 记录输入、审计程序、来源与工具身份')
    inputs, contract, identity = c.audit_input_manifest(), c.audit_contract_manifest(), provenance.capture()
    measurement = digest(c.measurement_contract_manifest())
    identity_digest = digest(identity)
    full_plan = plan(regions)
    records = {name: [] for name in SECTIONS}
    executed = set(selected)
    if 'inventory' in selected:
        for case in fonts(regions):
            records['inventory'].append({'id': case.key, 'cases': 1, 'failures': int(not case.path.is_file()),
                                         'samples': [] if case.path.is_file() else [{'reason': '缺少成品字体'}]})
    work = units(regions, selected)
    directory = Path(checkpoint_dir) if checkpoint_dir else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
    pending, results = [], {}
    for unit in work:
        key = unit_key(unit, measurement, identity_digest)
        path = directory / f'{key[:40]}.json' if directory else None
        stored = load_checkpoint(path, unit, key)
        if stored is not None:
            results[unit] = stored
        else:
            pending.append((unit, key, path))
    log(f'[审计] 共 {len(work)} 个单元；复用 {len(results)} 个检查点，待执行 {len(pending)} 个')
    workers = max(1, min(jobs or os.cpu_count() or 1, len(pending) or 1))

    def finished(index, unit, key, path, value, seconds):
        results[unit] = value
        failures = sum(record.get('failures', 0) for record in value.values())
        crashed = any(record.get('error') for record in value.values())
        log(f'[审计 {index}/{len(pending)}] {unit.name} 用时 {seconds:.0f}s 失败 {failures}' + (' 出错' if crashed else ''))
        if not crashed:
            store_checkpoint(path, unit, key, value, seconds)

    if workers == 1:
        for index, (unit, key, path) in enumerate(pending, 1):
            finished(index, unit, key, path, *_run_unit(unit))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_unit, unit): (unit, key, path) for unit, key, path in pending}
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                finished(index, *futures[future], *future.result())
    for unit in work:
        for section, record in results[unit].items():
            identifier = (unit.name.split(':', 1)[1] if ':' in unit.name else
                          'fixed-sources' if section == 'source_provenance' else 'license-documents')
            records[section].append({'id': identifier, **record})
    if c.audit_input_manifest() != inputs or c.audit_contract_manifest() != contract:
        raise ValueError('审计期间字体或审计程序发生变化')
    result = {'schema': 3, 'version': c.VERSION, 'input_manifest': inputs, 'audit_contract': contract,
              'provenance': identity, 'regions': list(regions), 'sections': sorted(selected),
              'complete_scope': set(regions) == set(c.REGIONS) and selected == set(SECTIONS),
              'records': records, 'elapsed_seconds': round(time.monotonic() - started)}
    result['gate'] = summarize(records, full_plan, executed=executed)
    return result


def run_core(*, regions=c.REGIONS, selected=None, skip_raster=False, log=print):
    """Sequential compatibility entry for small probes and tests."""
    chosen = set(selected or SECTIONS)
    if skip_raster:
        chosen -= {'raster', 'hint_execution'}
    return run(regions=regions, selected=chosen, jobs=1, log=log)
