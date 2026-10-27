"""Reparameterize single-axis glyph curves without discarding avar knots."""
from __future__ import annotations

import copy
import io

import uharfbuzz as hb

from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates, USE_MY_METRICS
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib.models import VariationModel, normalizeValue, piecewiseLinearMap, supportScalar
from fontTools.varLib.varStore import VarStoreInstancer
from fontTools.varLib import builder as var_builder


def coordinate(font, weight):
    axis = font['fvar'].axes[0]
    value = normalizeValue(weight, (axis.minValue, axis.defaultValue, axis.maxValue))
    return piecewiseLinearMap(value, font['avar'].segments['wght']) if 'avar' in font else value


def weight_at(font, normalized):
    if 'avar' in font:
        normalized = piecewiseLinearMap(normalized, {value: key for key, value in font['avar'].segments['wght'].items()})
    axis = font['fvar'].axes[0]
    return axis.defaultValue + normalized * (axis.defaultValue - axis.minValue if normalized < 0 else axis.maxValue - axis.defaultValue)


class ExactCurve:
    """Float glyph points of an original, unscaled and unrounded source VF.

    Instancing and scaling the source to 1000 UPM rounds its default and
    every delta; sampling those rounded tuples drifts from the real curve.
    """

    def __init__(self, font, scale, fixed=None):
        self.font, self.scale, self.fixed = font, scale, dict(fixed or {})
        self.locations, self.glyphs = {}, {}

    def location(self, weight):
        if weight not in self.locations:
            result = {}
            for axis in self.font['fvar'].axes:
                value = normalizeValue({**self.fixed, 'wght': weight}.get(axis.axisTag, axis.defaultValue),
                                       (axis.minValue, axis.defaultValue, axis.maxValue))
                if 'avar' in self.font and axis.axisTag in self.font['avar'].segments:
                    value = piecewiseLinearMap(value, self.font['avar'].segments[axis.axisTag])
                result[axis.axisTag] = value
            self.locations[weight] = result
        return self.locations[weight]

    def glyph(self, name):
        if name not in self.glyphs:
            coords, controls = self.font['glyf']._getCoordinatesAndControls(name, self.font['hmtx'].metrics, None)
            ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
            tuples = []
            for original in self.font['gvar'].variations.get(name, []):
                variation = copy.deepcopy(original)
                variation.calcInferredDeltas(coords, ends)
                tuples.append((variation.axes, GlyphCoordinates(variation.coordinates)))
            self.glyphs[name] = (GlyphCoordinates(coords), tuples)
        return self.glyphs[name]

    def points(self, name, weight):
        """Scaled real points (phantoms excluded), or None for an unknown glyph."""
        if name not in self.font['glyf'].glyphs:
            return None
        coords, tuples = self.glyph(name)
        location = self.location(weight)
        total = GlyphCoordinates(coords)
        for axes, deltas in tuples:
            scalar = supportScalar(location, axes)
            if scalar:
                total += deltas * scalar
        return [(x * self.scale, y * self.scale) for x, y in list(total)[:-4]]


def curve_grid(target, source):
    for font in (target, source):
        if [axis.axisTag for axis in font['fvar'].axes] != ['wght']:
            raise ValueError('曲线重参数化只接受已固定其他轴的 wght 字体')
    weights = set()
    for font in (target, source):
        axis = font['fvar'].axes[0]
        weights.update((axis.minValue, axis.defaultValue, axis.maxValue))
        for normalized in font['avar'].segments['wght'] if 'avar' in font else (-1, 0, 1):
            weights.add(axis.defaultValue + normalized * (axis.defaultValue - axis.minValue if normalized < 0 else axis.maxValue - axis.defaultValue))
    for variations in source['gvar'].variations.values():
        for variation in variations:
            weights.update(weight_at(source, value) for value in variation.axes.get('wght', ()))
    if 'HVAR' in source:
        for region in source['HVAR'].table.VarStore.VarRegionList.Region:
            axis = region.VarRegionAxis[0]
            weights.update(weight_at(source, value) for value in (axis.StartCoord, axis.PeakCoord, axis.EndCoord))
    bounds = target['fvar'].axes[0]
    grid = {floatToFixedToFloat(coordinate(target, weight), 14): weight for weight in sorted(weights)
            if bounds.minValue <= weight <= bounds.maxValue}
    grid[0.0] = bounds.defaultValue
    locations = sorted(grid)
    return [grid[value] for value in locations], [{'wght': value} if value else {} for value in locations]


def remap_gvar(source, target, exact=None):
    """Rebuild source gvar on the target axis.

    With ``exact`` (an ExactCurve of the original source), every non-default
    control takes the real curve value rather than the rounded source tuples.
    Deltas are rounded inside the hierarchical model so each control keeps at
    most half a unit of error; rounding afterwards accumulates over the
    overlapping supports.
    """
    weights, locations = curve_grid(target, source)
    model = VariationModel(locations, axisOrder=['wght'])
    source_locations = [{'wght': coordinate(source, weight)} for weight in weights]
    default_index = next(index for index, location in enumerate(locations) if not location)
    hvar = source['HVAR'].table if 'HVAR' in source else None
    metric_readers = [VarStoreInstancer(hvar.VarStore, source['fvar'].axes, location) for location in source_locations] if hvar else []
    scalar_cache = {}
    changed = detached = exact_glyphs = 0
    for gid, name in enumerate(source.getGlyphOrder()):
        if hvar:
            for component in getattr(source['glyf'][name], 'components', ()):
                detached += bool(component.flags & USE_MY_METRICS)
                component.flags &= ~USE_MY_METRICS
        coords, controls = source['glyf']._getCoordinatesAndControls(name, source['hmtx'].metrics,
                                                                    source['vmtx'].metrics if 'vmtx' in source else None)
        masters = [GlyphCoordinates([(0, 0)] * len(coords)) for _ in weights]
        ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
        for original in source['gvar'].variations.get(name, []):
            variation = copy.deepcopy(original)
            variation.calcInferredDeltas(coords, ends)
            deltas = GlyphCoordinates(variation.coordinates)
            key = tuple(sorted(variation.axes.items()))
            scalars = scalar_cache.setdefault(key, [supportScalar(location, variation.axes) for location in source_locations])
            for master, scalar in zip(masters, scalars):
                if scalar:
                    master += deltas * scalar
        if hvar:
            index = hvar.AdvWidthMap.mapping[name] if hvar.AdvWidthMap is not None else gid
            for master, reader in zip(masters, metric_readers):
                master[-3] = (master[-4][0] + reader[index], 0)
        if exact is not None:
            curves = [None if index == default_index else exact.points(name, weight)
                      for index, weight in enumerate(weights)]
            if any(curve is not None and len(curve) != len(coords) - 4 for curve in curves):
                raise ValueError(f'精确来源与缩放来源的字形结构不一致：{name}')
            for master, curve in zip(masters, curves):
                for point, (x, y) in enumerate(curve or ()):
                    master[point] = (x - coords[point][0], y - coords[point][1])
            exact_glyphs += any(curve is not None for curve in curves)
        deltas, supports = model.getDeltasAndSupports(masters, round=round)
        if any(x or y for x, y in deltas[0]):
            raise ValueError('重参数化不能改变默认字形')
        variations = []
        for delta, support in zip(deltas[1:], supports[1:]):
            values = [(otRound(x), otRound(y)) for x, y in delta]
            if any(x or y for x, y in values):
                variations.append(TupleVariation(support, values))
        source['gvar'].variations[name] = variations
        changed += bool(variations)
    return {'inter_gvar_reparameterized_glyphs': changed, 'inter_curve_breakpoints': weights,
            'inter_metric_components_detached': detached, 'inter_exact_curve_glyphs': exact_glyphs}


def adjacent_supports(locations, peak_order=None):
    coordinates = [location.get('wght', 0) for location in locations]
    if any(set(location) - {'wght'} for location in locations) or len(coordinates) != len(set(coordinates)) or 0 not in coordinates:
        raise ValueError('度量控制点必须是互不重复且包含默认值的单 wght 轴坐标')
    ordered = sorted(coordinates)
    peaks = peak_order if peak_order is not None else [value for value in ordered if value]
    supports = []
    for peak in peaks:
        index = ordered.index(peak)
        supports.append({'wght': (ordered[max(index - 1, 0)], peak, ordered[min(index + 1, len(ordered) - 1)])})
    return supports, [coordinates.index(peak) for peak in peaks]


def advance_curve_store(target, source, pairs, native_weights, native_advances=None, control_weights=()):
    """Preserve source advance knots without resampling rounded glyph bounds.

    Local adjacent tents avoid accumulating rounding error in a hierarchical
    metric model. Native named controls retain their actual advance; other
    controls are measured directly from the original source HVAR.
    Returns a store and glyph order for the caller to append to HVAR.
    """
    weights, locations = curve_grid(target, source)
    grid = {location.get('wght', 0): weight for weight, location in zip(weights, locations)}
    for weight in set(native_weights) | set(control_weights):
        grid[floatToFixedToFloat(coordinate(target, weight), 14)] = weight
    normalized = sorted(grid)
    weights = [grid[value] for value in normalized]
    supports, _indices = adjacent_supports([{'wght': value} if value else {} for value in normalized])
    runtimes = []
    for font in (target, source):
        stream = io.BytesIO(); font.save(stream)
        runtime = hb.Font(hb.Face(stream.getvalue())); runtime.scale = (1000, 1000)
        runtimes.append(runtime)
    actual, expected = runtimes
    target_names = sorted(pairs, key=target.getGlyphID)
    masters = {name: [] for name in target_names}
    for weight in weights:
        actual.set_variations({'wght': weight}); expected.set_variations({'wght': weight})
        for name in target_names:
            value = ((native_advances or {}).get(weight, {}).get(name, actual.get_glyph_h_advance(target.getGlyphID(name))) if weight in native_weights
                     else expected.get_glyph_h_advance(source.getGlyphID(pairs[name])))
            masters[name].append(value)
    default_index = normalized.index(0)
    items = []
    for name in target_names:
        default = target['hmtx'].metrics[name][0]
        if masters[name][default_index] != default:
            raise ValueError('默认字重的 advance 来源不一致')
        deltas = [value - default for index, value in enumerate(masters[name]) if index != default_index]
        items.append(deltas)
        # Keep the TrueType phantom advance and HVAR on the same curve. Neither
        # real points nor the left phantom (glyph origin) are moved here.
        variations = target['gvar'].variations.setdefault(name, [])
        for variation in variations:
            variation.coordinates[-3] = variation.coordinates[-4] or (0, 0)
        coordinates, _ = target['glyf']._getCoordinatesAndControls(
            name, target['hmtx'].metrics, target['vmtx'].metrics if 'vmtx' in target else None)
        for delta, support in zip(deltas, supports):
            if delta:
                values = [None] * len(coordinates)
                values[-4:] = [(0, 0), (delta, 0), (0, 0), (0, 0)]
                variations.append(TupleVariation(support, values))
    store = var_builder.buildVarStore(var_builder.buildVarRegionList(supports, ['wght']),
                                     [var_builder.buildVarData(list(range(len(supports))), items, optimize=False)])
    return store, target_names, weights
