"""Keep attachment anchors aligned when merged glyph origins are translated."""
from __future__ import annotations

import copy
import io

import uharfbuzz as hb
from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.pens.boundsPen import BoundsPen
from fontTools.varLib.builder import buildVarDevTable
from fontTools.varLib import builder as var_builder
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap
from fontTools.varLib.varStore import VarStoreInstancer

from font_variations import coordinate, curve_grid, weight_at, adjacent_supports


def anchor_slots(font):
    if 'GPOS' not in font:
        return
    for lookup in font['GPOS'].table.LookupList.Lookup:
        for outer in lookup.SubTable:
            part = getattr(outer, 'ExtSubTable', outer)
            for coverage, array, records, field in (
                ('MarkCoverage', 'MarkArray', 'MarkRecord', 'MarkAnchor'),
                ('Mark1Coverage', 'Mark1Array', 'MarkRecord', 'MarkAnchor'),
                ('BaseCoverage', 'BaseArray', 'BaseRecord', 'BaseAnchor'),
                ('Mark2Coverage', 'Mark2Array', 'Mark2Record', 'Mark2Anchor'),
            ):
                if not hasattr(part, coverage):
                    continue
                for name, record in zip(getattr(part, coverage).glyphs, getattr(getattr(part, array), records)):
                    value = getattr(record, field)
                    if isinstance(value, list):
                        for index, anchor in enumerate(value):
                            if anchor is not None:
                                yield name, value, index, anchor
                    elif value is not None:
                        yield name, record, field, value
            if hasattr(part, 'LigatureCoverage'):
                for name, attach in zip(part.LigatureCoverage.glyphs, part.LigatureArray.LigatureAttach):
                    for component in attach.ComponentRecord:
                        for index, anchor in enumerate(component.LigatureAnchor):
                            if anchor is not None:
                                yield name, component.LigatureAnchor, index, anchor
            if hasattr(part, 'EntryExitRecord'):
                for name, record in zip(part.Coverage.glyphs, part.EntryExitRecord):
                    for field in ('EntryAnchor', 'ExitAnchor'):
                        anchor = getattr(record, field)
                        if anchor is not None:
                            yield name, record, field, anchor


def drawn_origin(runtime, gid):
    """Ink (xMin, yMax) as drawn, without HarfBuzz's integer extents."""
    pen = BoundsPen(None)
    runtime.draw_glyph_with_pen(gid, pen)
    return None if pen.bounds is None else (pen.bounds[0], pen.bounds[3])


def anchor_value(anchor, instancer):
    point = []
    for coordinate_field, device_field in (('XCoordinate', 'XDeviceTable'), ('YCoordinate', 'YDeviceTable')):
        value = getattr(anchor, coordinate_field)
        device = getattr(anchor, device_field, None)
        if instancer is not None and device is not None and getattr(device, 'DeltaFormat', None) == 0x8000:
            value += instancer[(device.StartSize << 16) | device.EndSize]
        point.append(value)
    return tuple(point)


def anchor_curves(font, fixed, weights):
    """Every glyph's anchors in a source at each weight, as floats in 1000 units."""
    scale = 1000 / font['head'].unitsPerEm
    store = getattr(font['GDEF'].table, 'VarStore', None) if 'GDEF' in font else None
    axes = font['fvar'].axes if 'fvar' in font else []
    instancers = []
    for weight in weights:
        location = {}
        for axis in axes:
            value = weight if axis.axisTag == 'wght' else fixed.get(axis.axisTag, axis.defaultValue)
            normalized = normalizeValue(value, (axis.minValue, axis.defaultValue, axis.maxValue))
            if 'avar' in font and axis.axisTag in font['avar'].segments:
                normalized = piecewiseLinearMap(normalized, font['avar'].segments[axis.axisTag])
            location[axis.axisTag] = normalized
        instancers.append(VarStoreInstancer(store, axes, location) if store is not None else None)
    curves = {}
    for name, _owner, _field, anchor in anchor_slots(font):
        curves.setdefault(name, []).append([tuple(v * scale for v in anchor_value(anchor, instancer))
                                            for instancer in instancers])
    return curves


def align(target, source, glyph_map, weights, merge_store, exact=None):
    """glyph_map maps source identities to target identities in this merge.

    Call once after the final outline translations. Existing anchor variations
    are retained; only the glyph's measured change of origin is added.

    exact = (original source font, fixed axis values): the anchors follow
    the original curve, so the change of origin is measured against it. A
    copy scaled and rounded to 1000 UPM puts U+0304 0.4 units off at 700,
    and its integer extents then a whole unit off.

    Only a translation moves an anchor. Ink within a unit of the source is
    the native static's rounding, whose own anchors stay put: following it
    put U+030C on ê more than two units off the SemiBold static. A larger
    move is followed and rounds once, from the source anchor that the target
    anchor rounds at the given weights; rounding the move on its own and
    adding it to the already rounded anchor drifts a further unit.
    """
    pairs = {target_name: source_name for source_name, target_name in glyph_map.items()}
    slots = [entry for entry in anchor_slots(target) if entry[0] in pairs]
    if not slots:
        return {'inter_anchor_origins_aligned': 0}
    gdef = target['GDEF'].table
    previous_store = getattr(gdef, 'VarStore', None)
    control_weights = list(weights)
    grid_weights, locations = curve_grid(target, source)
    grid = {location.get('wght', 0): weight for weight, location in zip(grid_weights, locations)}
    for weight in control_weights:
        grid[floatToFixedToFloat(coordinate(target, weight), 14)] = weight
    if previous_store:
        for region in previous_store.VarRegionList.Region:
            axis = region.VarRegionAxis[0]
            for value in (axis.StartCoord, axis.PeakCoord, axis.EndCoord):
                grid.setdefault(value, weight_at(target, value))
    values = sorted(grid)
    weights = [grid[value] for value in values]
    locations = [{'wght': value} if value else {} for value in values]
    supports, master_indices = adjacent_supports(locations)
    default_index = values.index(0)
    runtimes = []
    measured, fixed = exact if exact is not None else (source, {})
    for font in (target, measured):
        stream = io.BytesIO(); flavor = font.flavor; font.flavor = None
        try:
            font.save(stream)
        finally:
            font.flavor = flavor
        runtime = hb.Font(hb.Face(stream.getvalue())); runtime.scale = (1000, 1000)
        runtimes.append(runtime)
    moves = {name: [] for name in {entry[0] for entry in slots}}
    for weight in weights:
        runtimes[0].set_variations({'wght': weight})
        runtimes[1].set_variations({**fixed, 'wght': weight})
        for name in moves:
            a = drawn_origin(runtimes[0], target.getGlyphID(name))
            b = drawn_origin(runtimes[1], measured.getGlyphID(pairs[name]))
            moves[name].append((a[0] - b[0], a[1] - b[1]) if a and b else (0.0, 0.0))
    curves = anchor_curves(measured, fixed, weights)
    current = [VarStoreInstancer(previous_store, target['fvar'].axes, location) if previous_store else None
               for location in locations]
    controls = [weights.index(weight) for weight in control_weights]
    original_regions = copy.deepcopy(previous_store.VarRegionList.Region) if previous_store else []
    region_count = len(original_regions)
    rows = []
    pending = []
    for name, owner, field, original in slots:
        move = moves[name]
        here = [anchor_value(original, instancer) for instancer in current]

        def distance(curve):
            return max(abs(here[i][axis] - curve[i][axis]) for i in controls for axis in (0, 1))

        curve = min(curves.get(pairs[name], []), key=distance, default=None)
        matched = curve is not None and distance(curve) <= 1
        shifts = []
        for i in range(len(weights)):
            shift = []
            for axis in (0, 1):
                if abs(move[i][axis]) < 1:
                    shift.append(0)
                elif matched:
                    shift.append(otRound(curve[i][axis] + move[i][axis] - here[i][axis]))
                else:
                    # No source anchor this one rounds: keep its value, follow the ink.
                    shift.append(otRound(move[i][axis]))
            shifts.append(tuple(shift))
        if not any(x or y for x, y in shifts):
            continue
        if original.Format == 2:
            raise ValueError('变更轮廓原点前必须显式处理轮廓点索引锚点')
        anchor = copy.deepcopy(original)
        for axis, coordinate_field, device_field in ((0, 'XCoordinate', 'XDeviceTable'), (1, 'YCoordinate', 'YDeviceTable')):
            device = getattr(original, device_field, None)
            if device is not None and device.DeltaFormat != 0x8000:
                raise ValueError('不能覆盖已有的字号设备定位表')
            index = (device.StartSize << 16) | device.EndSize if device else None
            # Preserve existing integer coefficients instead of resampling and
            # rounding the source anchor's curve a second time.
            row = [0] * (region_count + len(supports))
            if index is not None:
                data = previous_store.VarData[index >> 16]
                for region, delta in zip(data.VarRegionIndex, data.Item[index & 0xFFFF]):
                    row[region] += delta
            default = shifts[default_index][axis]
            for column, master_index in enumerate(master_indices):
                row[region_count + column] = shifts[master_index][axis] - default
            setattr(anchor, coordinate_field, getattr(original, coordinate_field) + default)
            setattr(anchor, device_field, buildVarDevTable(len(rows)) if any(row) else None)
            rows.append(row)
        anchor.Format = 3
        if isinstance(owner, list):
            owner[field] = anchor
        else:
            setattr(owner, field, anchor)
        pending.append(anchor)
    regions = var_builder.buildVarRegionList(supports, ['wght'])
    regions.Region = original_regions + regions.Region
    regions.RegionCount = len(regions.Region)
    additional = var_builder.buildVarStore(regions, [var_builder.buildVarData(
        list(range(len(regions.Region))), rows, optimize=False)])
    devices = [device for anchor in pending for field in ('XDeviceTable', 'YDeviceTable')
               for device in [getattr(anchor, field)] if device is not None]
    indices = [(device.StartSize << 16) | device.EndSize for device in devices]
    if devices:
        merged, remapped = merge_store(previous_store, additional, indices) if previous_store else (additional, indices)
        gdef.VarStore = merged; gdef.Version = 0x00010003
        for device, index in zip(devices, remapped):
            device.StartSize, device.EndSize = index >> 16, index & 0xFFFF
    return {'inter_anchor_origins_aligned': len(pending), 'inter_anchor_origin_weights': weights}
