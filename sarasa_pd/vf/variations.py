"""单轴可变字体曲线重参数化、同支撑 gvar 元组无损合并与斜体 IUP 重优化。"""
from __future__ import annotations

import copy
from typing import Any

from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates, USE_MY_METRICS
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.varLib.iup import iup_delta_optimize
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap, supportScalar
from fontTools.varLib.varStore import VarStoreInstancer


def axis_coordinate(font: TTFont, weight: float) -> float:
    axis = next(a for a in font["fvar"].axes if a.axisTag == "wght")
    val = normalizeValue(weight, (axis.minValue, axis.defaultValue, axis.maxValue))
    if "avar" in font and "wght" in font["avar"].segments:
        val = piecewiseLinearMap(val, font["avar"].segments["wght"])
    return val


def weight_at(font: TTFont, normalized: float) -> float:
    if "avar" in font and "wght" in font["avar"].segments:
        inv = {v: k for k, v in font["avar"].segments["wght"].items()}
        normalized = piecewiseLinearMap(normalized, inv)
    axis = next(a for a in font["fvar"].axes if a.axisTag == "wght")
    span = axis.defaultValue - axis.minValue if normalized < 0 else axis.maxValue - axis.defaultValue
    return axis.defaultValue + normalized * span


class ExactCurve:
    """读取未缩放未取整的原始来源 VF 浮点坐标与步进，避免 scale_upem 二次取整累积误差。"""

    def __init__(self, font: TTFont, scale: float, fixed: dict[str, float] | None = None) -> None:
        self.font = font
        self.scale = scale
        self.fixed = dict(fixed or {})
        self.locations: dict[float, dict[str, float]] = {}
        self.glyphs: dict[str, tuple[GlyphCoordinates, list[tuple[dict[str, tuple[float, float, float]], GlyphCoordinates]]]] = {}
        self.hvar = font["HVAR"].table if "HVAR" in font else None
        self.adv_instancers: dict[float, VarStoreInstancer] = {}

    def location(self, weight: float) -> dict[str, float]:
        if weight not in self.locations:
            loc: dict[str, float] = {}
            for axis in self.font["fvar"].axes:
                raw = {**self.fixed, "wght": weight}.get(axis.axisTag, axis.defaultValue)
                val = normalizeValue(raw, (axis.minValue, axis.defaultValue, axis.maxValue))
                if "avar" in self.font and axis.axisTag in self.font["avar"].segments:
                    val = piecewiseLinearMap(val, self.font["avar"].segments[axis.axisTag])
                loc[axis.axisTag] = val
            self.locations[weight] = loc
        return self.locations[weight]

    def glyph(self, name: str):
        if name not in self.glyphs:
            coords, controls = self.font["glyf"]._getCoordinatesAndControls(name, self.font["hmtx"].metrics, None)
            ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
            tuples = []
            for original in self.font["gvar"].variations.get(name, []):
                var = copy.deepcopy(original)
                var.calcInferredDeltas(coords, ends)
                tuples.append((var.axes, GlyphCoordinates(var.coordinates)))
            self.glyphs[name] = (GlyphCoordinates(coords), tuples)
        return self.glyphs[name]

    def points(self, name: str, weight: float) -> list[tuple[float, float]] | None:
        if name not in self.font["glyf"].glyphs:
            return None
        coords, tuples = self.glyph(name)
        loc = self.location(weight)
        total = GlyphCoordinates(coords)
        for axes, deltas in tuples:
            scalar = supportScalar(loc, axes)
            if scalar:
                total += deltas * scalar
        return [(x * self.scale, y * self.scale) for x, y in list(total)[:-4]]

    def advance_width(self, name: str, weight: float) -> float | None:
        if name not in self.font["hmtx"].metrics:
            return None
        adv = float(self.font["hmtx"].metrics[name][0])
        if self.hvar is not None:
            inst = self.adv_instancers.get(weight)
            if inst is None:
                inst = VarStoreInstancer(self.hvar.VarStore, self.font["fvar"].axes, self.location(weight))
                self.adv_instancers[weight] = inst
            idx = (
                self.hvar.AdvWidthMap.mapping[name]
                if self.hvar.AdvWidthMap is not None
                else self.font.getGlyphID(name)
            )
            adv += float(inst[idx])
        return adv * self.scale


def curve_grid(target: TTFont, source: TTFont) -> tuple[list[float], list[dict[str, float]]]:
    for font in (target, source):
        if [a.axisTag for a in font["fvar"].axes] != ["wght"]:
            raise ValueError("曲线重参数化只接受仅含 wght 轴的字体")
    weights: set[float] = set()
    for font in (target, source):
        axis = font["fvar"].axes[0]
        weights.update((axis.minValue, axis.defaultValue, axis.maxValue))
        seg = font["avar"].segments.get("wght", {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}) if "avar" in font else {-1: -1, 0: 0, 1: 1}
        for norm in seg:
            span = axis.defaultValue - axis.minValue if norm < 0 else axis.maxValue - axis.defaultValue
            weights.add(axis.defaultValue + norm * span)
    for variations in source["gvar"].variations.values():
        for var in variations:
            weights.update(weight_at(source, v) for v in var.axes.get("wght", ()))
    if "HVAR" in source:
        for region in source["HVAR"].table.VarStore.VarRegionList.Region:
            ra = region.VarRegionAxis[0]
            weights.update(weight_at(source, v) for v in (ra.StartCoord, ra.PeakCoord, ra.EndCoord))
    bounds = target["fvar"].axes[0]
    grid = {
        floatToFixedToFloat(axis_coordinate(target, w), 14): w
        for w in sorted(weights)
        if bounds.minValue <= w <= bounds.maxValue
    }
    grid[0.0] = bounds.defaultValue
    locs = sorted(grid)
    return [grid[v] for v in locs], [{"wght": v} if v else {} for v in locs]


def remap_gvar(source: TTFont, target: TTFont, exact: ExactCurve | None = None) -> dict[str, Any]:
    """将 source 的 gvar 重建到 target 的公开 wght/avar 坐标空间，用相邻分段线性支撑实现命名字重单次取整。"""
    weights, locations = curve_grid(target, source)
    supports, master_indices = adjacent_supports(locations)
    source_locations = [{"wght": axis_coordinate(source, w)} for w in weights]
    default_index = next(i for i, loc in enumerate(locations) if not loc)
    hvar = source["HVAR"].table if "HVAR" in source else None
    metric_readers = (
        [VarStoreInstancer(hvar.VarStore, source["fvar"].axes, loc) for loc in source_locations]
        if hvar
        else []
    )
    scalar_cache: dict[tuple, list[float]] = {}
    changed = detached = exact_glyphs = 0

    for gid, name in enumerate(source.getGlyphOrder()):
        if hvar:
            for comp in getattr(source["glyf"][name], "components", ()):
                detached += int(bool(comp.flags & USE_MY_METRICS))
                comp.flags &= ~USE_MY_METRICS
        coords, controls = source["glyf"]._getCoordinatesAndControls(
            name, source["hmtx"].metrics, source["vmtx"].metrics if "vmtx" in source else None
        )
        masters = [GlyphCoordinates([(0, 0)] * len(coords)) for _ in weights]
        ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
        for original in source["gvar"].variations.get(name, []):
            var = copy.deepcopy(original)
            var.calcInferredDeltas(coords, ends)
            deltas = GlyphCoordinates(var.coordinates)
            key = tuple(sorted(var.axes.items()))
            scalars = scalar_cache.setdefault(
                key, [supportScalar(loc, var.axes) for loc in source_locations]
            )
            for master, sc in zip(masters, scalars):
                if sc:
                    master += deltas * sc
        if hvar:
            idx = hvar.AdvWidthMap.mapping[name] if hvar.AdvWidthMap is not None else gid
            for master, reader in zip(masters, metric_readers):
                master[-3] = (master[-4][0] + reader[idx], 0)
        if exact is not None:
            curves = [None if i == default_index else exact.points(name, w) for i, w in enumerate(weights)]
            if any(c is not None and len(c) != len(coords) - 4 for c in curves):
                raise ValueError(f"精确来源与缩放来源的字形点数不一致：{name}")
            base_adv = coords[-3][0] - coords[-4][0]
            for i, (master, w, curve) in enumerate(zip(masters, weights, curves)):
                if i == default_index:
                    continue
                for pt, (x, y) in enumerate(curve or ()):
                    master[pt] = (otRound(x) - coords[pt][0], otRound(y) - coords[pt][1])
                exact_adv = exact.advance_width(name, w)
                if exact_adv is not None:
                    master[-3] = (master[-4][0] + otRound(exact_adv) - base_adv, 0)
            exact_glyphs += int(any(c is not None for c in curves))
        if any(otRound(x) or otRound(y) for x, y in masters[default_index]):
            raise ValueError("重参数化不能改变默认字形")
        variations = []
        for support, m_idx in zip(supports, master_indices):
            values = [(otRound(x), otRound(y)) for x, y in masters[m_idx]]
            if any(x or y for x, y in values):
                variations.append(TupleVariation(support, values))
        source["gvar"].variations[name] = variations
        changed += int(bool(variations))
    return {
        "inter_gvar_reparameterized_glyphs": changed,
        "inter_curve_breakpoints": weights,
        "inter_metric_components_detached": detached,
        "inter_exact_curve_glyphs": exact_glyphs,
    }


def adjacent_supports(locations: list[dict[str, float]], peak_order: list[float] | None = None):
    coords = [loc.get("wght", 0.0) for loc in locations]
    if any(set(loc) - {"wght"} for loc in locations) or len(coords) != len(set(coords)) or 0.0 not in coords:
        raise ValueError("度量控制点必须是互不重复且包含默认值的单 wght 轴坐标")
    ordered = sorted(coords)
    peaks = peak_order if peak_order is not None else [v for v in ordered if v]
    supports = []
    for peak in peaks:
        idx = ordered.index(peak)
        supports.append({"wght": (ordered[max(idx - 1, 0)], peak, ordered[min(idx + 1, len(ordered) - 1)])})
    return supports, [coords.index(p) for p in peaks]


def merge_same_support_gvar_tuples(font: TTFont) -> dict[str, int]:
    """无损合并 gvar 中同一支撑的多个 TupleVariation 并移除全零元组。"""
    if "gvar" not in font or "glyf" not in font:
        return {"tuples_before": 0, "tuples_after": 0, "zero_tuples_dropped": 0}
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics
    vmtx = font["vmtx"].metrics if "vmtx" in font else None
    before = after = dropped_zero = 0

    for gname, variations in font["gvar"].variations.items():
        if not variations:
            continue
        before += len(variations)
        groups: dict[tuple, list[TupleVariation]] = {}
        for var in variations:
            key = tuple(sorted((a, tuple(floatToFixedToFloat(v, 14) for v in rng)) for a, rng in var.axes.items()))
            groups.setdefault(key, []).append(var)

        coords_controls = None
        merged_list: list[TupleVariation] = []
        for _key, group in groups.items():
            if len(group) == 1:
                v0 = group[0]
                if any(pt is not None and (pt[0] or pt[1]) for pt in v0.coordinates):
                    merged_list.append(v0)
                else:
                    dropped_zero += 1
                continue
            # 多元组同支撑：先物化 IUP 隐含点再精确相加
            if coords_controls is None:
                coords_controls = glyf._getCoordinatesAndControls(gname, hmtx, vmtx)
            coords, controls = coords_controls
            ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
            acc_x = [0] * len(coords)
            acc_y = [0] * len(coords)
            for var in group:
                vcopy = copy.deepcopy(var)
                if any(pt is None for pt in vcopy.coordinates):
                    vcopy.calcInferredDeltas(coords, ends)
                    vcopy.roundDeltas()
                for i, pt in enumerate(vcopy.coordinates):
                    if pt is not None:
                        acc_x[i] += otRound(pt[0])
                        acc_y[i] += otRound(pt[1])
            summed = [(x, y) for x, y in zip(acc_x, acc_y)]
            if any(x or y for x, y in summed):
                merged_list.append(TupleVariation(copy.deepcopy(group[0].axes), summed))
            else:
                dropped_zero += 1

        font["gvar"].variations[gname] = merged_list
        after += len(merged_list)

    return {"tuples_before": before, "tuples_after": after, "zero_tuples_dropped": dropped_zero}


def reoptimize_gvar_iup(font: TTFont, *, tolerance: float = 0.0) -> dict[str, int]:
    """对简单字形的 gvar 元组做 IUP 压缩（斜体容差 0 可显著减小体积）。"""
    if "gvar" not in font or "glyf" not in font:
        return {"glyphs_optimized": 0, "tuples_optimized": 0}
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics
    vmtx = font["vmtx"].metrics if "vmtx" in font else None
    g_count = t_count = 0

    for gname, variations in font["gvar"].variations.items():
        if not variations or gname not in glyf.glyphs:
            continue
        glyph = glyf[gname]
        if glyph.numberOfContours <= 0:
            continue
        coords, _controls = glyf._getCoordinatesAndControls(gname, hmtx, vmtx)
        ends = list(glyph.endPtsOfContours)
        orig_pts = list(coords)
        touched = False
        for var in variations:
            if any(pt is None for pt in var.coordinates):
                continue
            deltas = list(var.coordinates)
            if not any(x or y for x, y in deltas):
                continue
            opt = iup_delta_optimize(deltas, orig_pts, ends, tolerance=tolerance)
            if any(pt is None for pt in opt):
                var.coordinates = opt
                t_count += 1
                touched = True
        if touched:
            g_count += 1
    return {"glyphs_optimized": g_count, "tuples_optimized": t_count}
