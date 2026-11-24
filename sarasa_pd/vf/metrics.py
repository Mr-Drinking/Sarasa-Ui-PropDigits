"""VF 竖排度量、gvar 优化、HVAR（由 gvar 直接生成）、MVAR 与 head 边界重算。"""
from __future__ import annotations

from typing import Any

from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.varLib.hvar import add_HVAR
from fontTools.varLib.models import VariationModel
from fontTools.varLib.varStore import OnlineVarStoreBuilder, VarStoreInstancer

from ..spec import Spec, load_spec
from .base import normalized_wght
from .inter import INTER_PREFIX
from .variations import merge_same_support_gvar_tuples, reoptimize_gvar_iup


def normalize_vertical_metrics(font: TTFont) -> dict[str, Any]:
    """清理 VORG/VVAR/旧 HVAR，并为 Inter 字形与 .notdef 写入 TrueType vmtx 竖排原点（y=880，步进 1000）。"""
    removed = [tag for tag in ("VORG", "VVAR", "HVAR") if tag in font]
    for tag in removed:
        del font[tag]

    updated_inter = 0
    if "vmtx" in font and "glyf" in font:
        glyf = font["glyf"]
        vmtx = font["vmtx"].metrics
        for gname in font.getGlyphOrder():
            if not (gname == ".notdef" or gname.startswith(INTER_PREFIX)) and gname in vmtx:
                continue
            g = glyf[gname]
            if g.isComposite() or getattr(g, "numberOfContours", 0) > 0:
                g.recalcBounds(glyf)
                tsb = otRound(880 - getattr(g, "yMax", 0))
            else:
                tsb = 0
            vmtx[gname] = (1000, tsb)
            updated_inter += 1
    return {"metric_tables_cleared": removed, "inter_vmtx_normalized": updated_inter}


def rebuild_vf_mvar(font: TTFont, spec: Spec | None = None) -> dict[str, Any]:
    """按 6 档命名字重的下划线/删除线度量构建精确 MVAR（unds、undo、strs）。"""
    spec = spec or load_spec()
    if "fvar" not in font or "post" not in font:
        raise ValueError("构建 MVAR 需要 fvar 与 post 表")

    weights = [w.value for w in spec.weights]
    expected_map = {w.value: (w.underline_thickness, w.underline_position) for w in spec.weights}
    controls = {w: list(expected_map[w]) for w in weights}

    def norm_loc(w: int) -> dict[str, float]:
        n = normalized_wght(font, w)
        return {} if n == 0.0 else {"wght": n}

    def build_once() -> tuple[Any, list[Any], dict[str, int]]:
        model = VariationModel([norm_loc(w) for w in weights], axisOrder=["wght"])
        builder = OnlineVarStoreBuilder(["wght"])
        builder.setModel(model)
        records = []
        bases: dict[str, int] = {}
        for tag, idx, attr in (("unds", 0, "underlineThickness"), ("undo", 1, "underlinePosition")):
            vals = [controls[w][idx] for w in weights]
            base_val, var_idx = builder.storeMasters(vals)
            bases[attr] = base_val
            rec = ot.MetricsValueRecord()
            rec.ValueTag = tag
            rec.VarIdx = var_idx
            records.append(rec)
        store = builder.finish()
        for region in store.VarRegionList.Region:
            for ax in region.VarRegionAxis:
                for attr in ("StartCoord", "PeakCoord", "EndCoord"):
                    setattr(ax, attr, floatToFixedToFloat(getattr(ax, attr), 14))
        remap = store.optimize()
        for rec in records:
            rec.VarIdx = remap[rec.VarIdx]
        return store, records, bases

    rounds = 0
    while True:
        store, records, bases = build_once()
        by_tag = {r.ValueTag: r for r in records}
        errs: dict[int, tuple[int, int]] = {}
        for w in weights:
            inst = VarStoreInstancer(store, font["fvar"].axes, norm_loc(w))
            actual = (
                bases["underlineThickness"] + otRound(inst[by_tag["unds"].VarIdx]),
                bases["underlinePosition"] + otRound(inst[by_tag["undo"].VarIdx]),
            )
            exp = expected_map[w]
            if actual != exp:
                errs[w] = (exp[0] - actual[0], exp[1] - actual[1])
        if not errs:
            break
        rounds += 1
        if rounds > 8:
            raise RuntimeError(f"MVAR 校准未收敛：{errs}")
        for w, (dt, dp) in errs.items():
            controls[w][0] += dt
            controls[w][1] += dp

    font["post"].underlineThickness = bases["underlineThickness"]
    font["post"].underlinePosition = bases["underlinePosition"]
    font["OS/2"].yStrikeoutSize = bases["underlineThickness"]

    strikeout = ot.MetricsValueRecord()
    strikeout.ValueTag = "strs"
    strikeout.VarIdx = next(r.VarIdx for r in records if r.ValueTag == "unds")
    records.append(strikeout)

    tbl = font["MVAR"] = newTable("MVAR")
    mvar = tbl.table = ot.MVAR()
    mvar.Version = 0x00010000
    mvar.Reserved = 0
    mvar.VarStore = store
    mvar.ValueRecordSize = 8
    mvar.ValueRecordCount = len(records)
    mvar.ValueRecord = sorted(records, key=lambda r: r.ValueTag)
    return {
        "mvar_records": len(records),
        "mvar_calibration_rounds": rounds,
        "underline_thickness_default": bases["underlineThickness"],
        "underline_position_default": bases["underlinePosition"],
    }


def optimize_vf_gvar(font: TTFont, italic: bool) -> dict[str, int]:
    """合并同支撑 gvar 元组，并对斜体执行容差 0 的 IUP 重优化。"""
    rep = merge_same_support_gvar_tuples(font)
    if italic:
        rep.update(reoptimize_gvar_iup(font, tolerance=0.0))
    return rep


def rebuild_vf_hvar_from_gvar(font: TTFont) -> dict[str, Any]:
    """直接由最终 gvar 幽灵点生成 HVAR，确保 VF 运行时与实例化静态步进同源一致。"""
    if "fvar" not in font or "gvar" not in font:
        raise ValueError("构建 HVAR 需要 fvar 与 gvar 表")
    # 确保所有字形都在 gvar 中有条目
    variations = font["gvar"].variations
    for gname in font.getGlyphOrder():
        if gname not in variations:
            variations[gname] = []
    add_HVAR(font)
    hvar = font["HVAR"].table
    store = hvar.VarStore
    return {
        "hvar_regions": len(store.VarRegionList.Region) if store else 0,
        "hvar_subtables": len(store.VarData) if store else 0,
    }


def recalculate_head_bounds_and_lsb(font: TTFont) -> dict[str, Any]:
    """重算所有轮廓边界，对齐非空字形 hmtx.lsb == xMin，并汇总更新 head 包围盒。"""
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics
    lsb_fixed = 0
    boxes: list[tuple[int, int, int, int]] = []
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        if g.isComposite() or getattr(g, "numberOfContours", 0) > 0:
            g.recalcBounds(glyf)
            adv, lsb = hmtx[gname]
            if hasattr(g, "xMin") and lsb != g.xMin:
                hmtx[gname] = (adv, g.xMin)
                lsb_fixed += 1
            if all(hasattr(g, attr) for attr in ("xMin", "yMin", "xMax", "yMax")):
                boxes.append((g.xMin, g.yMin, g.xMax, g.yMax))
    if not boxes:
        raise ValueError("字体没有可计算包围盒的轮廓")
    bounds = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    font["head"].xMin, font["head"].yMin, font["head"].xMax, font["head"].yMax = bounds
    return {"head_bounds": list(bounds), "lsb_fixed": lsb_fixed}
