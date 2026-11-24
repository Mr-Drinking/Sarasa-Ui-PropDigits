"""Inter VF 载入、1000 UPM 缩放、Sarasa Ui 配方烘焙、gvar 支撑重映射与字形并入。"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from fontTools.misc.fixedTools import otRound
from fontTools.ttLib import TTFont
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap
from fontTools.varLib.varStore import VarStoreInstancer

from ..spec import Spec, load_spec
from .base import _single_sub_mapping, _unicode_cmap_tables, weight_axis
from .variations import ExactCurve, remap_gvar

INTER_PREFIX = "inter."
VALUE_DEVICE_FIELDS = (
    ("XPlacement", "XPlaDevice"),
    ("YPlacement", "YPlaDevice"),
    ("XAdvance", "XAdvDevice"),
    ("YAdvance", "YAdvDevice"),
)


def prefixed(name: str) -> str:
    return name if name == ".notdef" else f"{INTER_PREFIX}{name}"


def anchor_slots(font: TTFont) -> Iterator[tuple[str, Any, Any, Any]]:
    """遍历 GPOS 中所有非空 Anchor 槽位，返回 (glyph_name, owner, field_or_index, anchor)。"""
    if "GPOS" not in font or not font["GPOS"].table.LookupList:
        return
    for lookup in font["GPOS"].table.LookupList.Lookup:
        for outer in lookup.SubTable:
            part = getattr(outer, "ExtSubTable", outer)
            for cov_attr, arr_attr, rec_attr, field in (
                ("MarkCoverage", "MarkArray", "MarkRecord", "MarkAnchor"),
                ("Mark1Coverage", "Mark1Array", "MarkRecord", "MarkAnchor"),
                ("BaseCoverage", "BaseArray", "BaseRecord", "BaseAnchor"),
                ("Mark2Coverage", "Mark2Array", "Mark2Record", "Mark2Anchor"),
            ):
                if not hasattr(part, cov_attr):
                    continue
                for gname, rec in zip(getattr(part, cov_attr).glyphs, getattr(getattr(part, arr_attr), rec_attr)):
                    val = getattr(rec, field)
                    if isinstance(val, list):
                        for idx, anchor in enumerate(val):
                            if anchor is not None:
                                yield gname, val, idx, anchor
                    elif val is not None:
                        yield gname, rec, field, val
            if hasattr(part, "LigatureCoverage"):
                for gname, attach in zip(part.LigatureCoverage.glyphs, part.LigatureArray.LigatureAttach):
                    for comp in attach.ComponentRecord:
                        for idx, anchor in enumerate(comp.LigatureAnchor):
                            if anchor is not None:
                                yield gname, comp.LigatureAnchor, idx, anchor
            if hasattr(part, "EntryExitRecord"):
                for gname, rec in zip(part.Coverage.glyphs, part.EntryExitRecord):
                    for field in ("EntryAnchor", "ExitAnchor"):
                        anchor = getattr(rec, field)
                        if anchor is not None:
                            yield gname, rec, field, anchor


def collect_ot_variation_devices(obj: Any, seen: set[int] | None = None) -> list[Any]:
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (str, int, float, bool, bytes)):
        return []
    oid = id(obj)
    if oid in seen:
        return []
    seen.add(oid)
    if isinstance(obj, ot.Device) and getattr(obj, "DeltaFormat", None) == 0x8000:
        return [obj]
    if isinstance(obj, dict):
        values = [*obj.keys(), *obj.values()]
    elif isinstance(obj, (list, tuple, set)):
        values = list(obj)
    elif hasattr(obj, "__dict__"):
        values = [v for k, v in vars(obj).items() if not k.startswith("_") and k not in {"reader", "font"}]
    else:
        values = []
    out: list[Any] = []
    for v in values:
        out.extend(collect_ot_variation_devices(v, seen))
    return out


def variable_value_slots(table: Any) -> list[tuple[Any, str, str]]:
    slots: list[tuple[Any, str, str]] = []
    seen: set[int] = set()

    def visit(val: Any) -> None:
        if val is None or isinstance(val, (str, int, float, bool, bytes)) or id(val) in seen:
            return
        seen.add(id(val))
        if isinstance(val, ot.ValueRecord):
            for field, dev_field in VALUE_DEVICE_FIELDS:
                dev = getattr(val, dev_field, None)
                if dev is not None and getattr(dev, "DeltaFormat", None) == 0x8000:
                    slots.append((val, field, dev_field))
            return
        if hasattr(val, "ensureDecompiled"):
            val.ensureDecompiled()
        if isinstance(val, dict):
            items = list(val.values())
        elif isinstance(val, (list, tuple)):
            items = list(val)
        elif hasattr(val, "__dict__"):
            items = [v for k, v in vars(val).items() if not k.startswith("_") and k not in {"reader", "font"}]
        else:
            items = []
        for item in items:
            visit(item)

    visit(table)
    return slots


@dataclass
class LoadedInter:
    font: TTFont
    source_path: Path | None
    fixed_axes: dict[str, float]
    original_gpos: tuple[Any, list[Any], dict[float, float], int] | None
    unscaled_gpos_store: tuple[Any, float] | None
    anchor_fractions: dict[int, tuple[Any, float, float]]
    value_fractions: dict[tuple[int, str], tuple[Any, float]]

    def close(self) -> None:
        self.font.close()

    def __enter__(self) -> LoadedInter:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def original_instancers(self, weights: list[float]) -> list[Any] | None:
        if self.original_gpos is None:
            return None
        store, axes, segments, _upem = self.original_gpos
        out = []
        for w in weights:
            loc = {}
            for axis in axes:
                if axis.axisTag == "wght":
                    val = normalizeValue(w, (axis.minValue, axis.defaultValue, axis.maxValue))
                    loc["wght"] = piecewiseLinearMap(val, segments) if segments else val
                else:
                    fixed = self.fixed_axes.get(axis.axisTag, axis.defaultValue)
                    loc[axis.axisTag] = normalizeValue(fixed, (axis.minValue, axis.defaultValue, axis.maxValue))
            out.append(VarStoreInstancer(store, axes, loc))
        return out

    @contextmanager
    def exact_curve(self) -> Iterator[ExactCurve | None]:
        if self.source_path is None:
            yield None
            return
        with TTFont(self.source_path) as orig:
            yield ExactCurve(orig, 1000 / orig["head"].unitsPerEm, self.fixed_axes)


def _capture_original_gpos(inter: TTFont) -> tuple[Any, list[Any], dict[float, float], int] | None:
    store = getattr(inter["GDEF"].table, "VarStore", None) if "GDEF" in inter and "GPOS" in inter else None
    if store is None:
        return None
    for dev in collect_ot_variation_devices(inter["GPOS"].table):
        dev._sarasa_original_varidx = (dev.StartSize << 16) | dev.EndSize
    seg = dict(inter["avar"].segments.get("wght", {})) if "avar" in inter else {}
    return copy.deepcopy(store), copy.deepcopy(inter["fvar"].axes), seg, int(inter["head"].unitsPerEm)


def _feature_lookup_indices(font: TTFont, table_tag: str, tags: set[str]) -> set[int]:
    if table_tag not in font or not font[table_tag].table.FeatureList:
        return set()
    return {
        int(idx)
        for rec in font[table_tag].table.FeatureList.FeatureRecord
        if rec.FeatureTag in tags
        for idx in (rec.Feature.LookupListIndex or [])
    }


def _expand_calt_coverages(font: TTFont, effective: dict[str, str]) -> int:
    """将烘焙 ss03/cv10 产生的新字形原地补入 Inter calt 的上下文覆盖表，保持原生 lookup 顺序（修 A12.5）。"""
    if not effective or "GSUB" not in font or not font["GSUB"].table.LookupList:
        return 0
    gid_map = {name: i for i, name in enumerate(font.getGlyphOrder())}
    gsub = font["GSUB"].table
    expanded = 0
    for idx in _feature_lookup_indices(font, "GSUB", {"calt"}):
        lk = gsub.LookupList.Lookup[idx]
        for outer in lk.SubTable:
            st = getattr(outer, "ExtSubTable", outer)
            if not isinstance(st, ot.ChainContextSubst) or getattr(st, "Format", None) != 3:
                continue
            for attr in ("BacktrackCoverage", "LookAheadCoverage"):
                for cov in getattr(st, attr, []):
                    additions = [effective[g] for g in cov.glyphs if g in effective and effective[g] not in cov.glyphs]
                    if additions:
                        merged = sorted(set(cov.glyphs) | set(additions), key=lambda g: gid_map.get(g, 1 << 30))
                        cov.glyphs = merged
                        expanded += len(additions)
    return expanded


def bake_inter_ui_recipe(font: TTFont, spec: Spec) -> dict[str, int]:
    """按 Sarasa Ui 顺序烘焙 tnum(跳过 U+002D) -> ss03/cv10 -> 37 码位 pnum，不挪动任何 lookup。"""
    tnum_map = _single_sub_mapping(font, "tnum")
    tnum_baked = 0
    for tbl in _unicode_cmap_tables(font):
        for cp, gname in list(tbl.cmap.items()):
            if cp == 0x2D:
                continue
            dst = tnum_map.get(gname)
            if dst and dst in font["glyf"].glyphs:
                tbl.cmap[cp] = dst
                tnum_baked += 1

    # 烘焙 ss03 -> cv10
    presets = [_single_sub_mapping(font, tag) for tag in ("ss03", "cv10")]
    effective = {name: name for name in (font.getBestCmap() or {}).values()}
    for subs in presets:
        effective = {orig: subs.get(cur, cur) for orig, cur in effective.items()}
    effective = {orig: dst for orig, dst in effective.items() if orig != dst}

    if effective and "GSUB" in font and font["GSUB"].table.LookupList:
        for idx in _feature_lookup_indices(font, "GSUB", {"tnum", "pnum"}):
            lk = font["GSUB"].table.LookupList.Lookup[idx]
            for outer in lk.SubTable:
                st = getattr(outer, "ExtSubTable", outer)
                if not isinstance(st, ot.SingleSubst):
                    continue
                updates: dict[str, str] = {}
                for src, dst in list(st.mapping.items()):
                    ns, nd = effective.get(src, src), effective.get(dst, dst)
                    if ns in updates and updates[ns] != nd:
                        raise ValueError("Inter 预设与数字宽度替换存在冲突")
                    updates[ns] = nd
                st.mapping.update(updates)
        calt_expanded = _expand_calt_coverages(font, effective)
    else:
        calt_expanded = 0

    for tbl in _unicode_cmap_tables(font):
        tbl.cmap = {cp: effective.get(gname, gname) for cp, gname in tbl.cmap.items()}

    # 对 37 个码位恢复 pnum 比例形态
    pnum_map = _single_sub_mapping(font, "pnum")
    default_cmap = font.getBestCmap() or {}
    remap = {
        cp: pnum_map[default_cmap[cp]]
        for cp in sorted(spec.propdigits_codepoints)
        if cp in default_cmap and default_cmap[cp] in pnum_map and pnum_map[default_cmap[cp]] in font["glyf"].glyphs
    }
    pnum_restored = 0
    for tbl in _unicode_cmap_tables(font):
        for cp, dst in remap.items():
            if cp in tbl.cmap and tbl.cmap[cp] != dst:
                tbl.cmap[cp] = dst
                pnum_restored += 1

    return {
        "inter_tnum_baked": tnum_baked,
        "inter_preset_remapped": len(effective),
        "inter_calt_coverages_expanded": calt_expanded,
        "inter_propdigits_pnum_restored": pnum_restored,
    }


def repair_inter_ss03_kerning(inter: TTFont) -> int:
    """修复 Inter 4.1 VF 中 (quoteright.ss03, rho) 被 L1 S1 类值冲掉的问题（A9）。

    Inter 4.1 的 Inter-Regular/Inter-Black/InterDisplay-Regular 以及 InterVariable 的
    (quoteright, rho) 均为 -128（1000 UPM 为 -62），无变体偏差；仅因 InterDisplay-Black
    漏掉该例外，varLib 把 (quoteright.ss03, rho) 挪进 S1 并覆写成类值 -43（-21）。
    本函数在缩放到 1000 UPM 前把 (quoteright.ss03, rho) 恢复为同表 (quoteright, rho) 的值。
    """
    if "GPOS" not in inter or not inter["GPOS"].table.LookupList:
        return 0
    gpos = inter["GPOS"].table
    kern_lks = _feature_lookup_indices(inter, "GPOS", {"kern"})
    gid_map = {name: i for i, name in enumerate(inter.getGlyphOrder())}
    fixed = 0
    for idx in sorted(kern_lks):
        lk = gpos.LookupList.Lookup[idx]
        fmt1_subs = [
            st
            for raw in lk.SubTable
            if isinstance((st := getattr(raw, "ExtSubTable", raw)), ot.PairPos) and st.Format == 1
        ]
        if not fmt1_subs:
            continue
        ref_sub = None
        ref_rec = None
        for st in fmt1_subs:
            if "quoteright" not in st.Coverage.glyphs:
                continue
            ps = st.PairSet[st.Coverage.glyphs.index("quoteright")]
            hit = next((r for r in ps.PairValueRecord if r.SecondGlyph == "rho"), None)
            if hit is not None:
                ref_sub, ref_rec = st, hit
                break
        if ref_rec is None:
            continue

        updated = False
        for st in fmt1_subs:
            if "quoteright.ss03" not in st.Coverage.glyphs:
                continue
            ps = st.PairSet[st.Coverage.glyphs.index("quoteright.ss03")]
            hit = next((r for r in ps.PairValueRecord if r.SecondGlyph == "rho"), None)
            if hit is not None:
                hit.Value1 = copy.deepcopy(getattr(ref_rec, "Value1", None))
                ref_v2 = getattr(ref_rec, "Value2", None)
                if ref_v2 is not None or hasattr(hit, "Value2"):
                    hit.Value2 = copy.deepcopy(ref_v2)
                updated = True
                fixed += 1
                break
        if not updated and ref_sub is not None and "quoteright.ss03" in gid_map and "rho" in gid_map:
            new_rec = ot.PairValueRecord()
            new_rec.SecondGlyph = "rho"
            new_rec.Value1 = copy.deepcopy(getattr(ref_rec, "Value1", None))
            ref_v2 = getattr(ref_rec, "Value2", None)
            if ref_v2 is not None:
                new_rec.Value2 = copy.deepcopy(ref_v2)
            if "quoteright.ss03" in ref_sub.Coverage.glyphs:
                ps = ref_sub.PairSet[ref_sub.Coverage.glyphs.index("quoteright.ss03")]
                ps.PairValueRecord.append(new_rec)
                ps.PairValueRecord.sort(key=lambda r: gid_map.get(r.SecondGlyph, 1 << 30))
                ps.PairValueCount = len(ps.PairValueRecord)
            else:
                new_ps = ot.PairSet()
                new_ps.PairValueRecord = [new_rec]
                new_ps.PairValueCount = 1
                pairs = list(zip(ref_sub.Coverage.glyphs, ref_sub.PairSet))
                pairs.append(("quoteright.ss03", new_ps))
                pairs.sort(key=lambda item: gid_map.get(item[0], 1 << 30))
                ref_sub.Coverage.glyphs = [g for g, _ in pairs]
                ref_sub.PairSet = [p for _, p in pairs]
                ref_sub.PairSetCount = len(ref_sub.PairSet)
            fixed += 1
    return fixed


def load_inter(inter_vf: Path, spec: Spec | None = None) -> LoadedInter:
    spec = spec or load_spec()
    with TTFont(inter_vf) as raw:
        original_gpos = _capture_original_gpos(raw)
        inter = instantiateVariableFont(
            raw,
            {"opsz": spec.inter_opsz, "wght": spec.public_limit},
            inplace=False,
            optimize=True,
        )
    repair_inter_ss03_kerning(inter)
    unscaled_store = (
        copy.deepcopy(inter["GDEF"].table.VarStore)
        if "GDEF" in inter and getattr(inter["GDEF"].table, "VarStore", None) is not None
        else None
    )
    scale = 1000 / inter["head"].unitsPerEm
    unscaled_anchors = [
        (anchor, anchor.XCoordinate, anchor.YCoordinate)
        for _g, _o, _f, anchor in anchor_slots(inter)
    ]
    unscaled_values = (
        [(rec, field, getattr(rec, field, 0) or 0) for rec, field, _dev in variable_value_slots(inter["GPOS"].table)]
        if "GPOS" in inter
        else []
    )
    scale_upem(inter, 1000)
    anchor_fractions = {
        id(a): (a, x * scale - a.XCoordinate, y * scale - a.YCoordinate)
        for a, x, y in unscaled_anchors
    }
    value_fractions = {
        (id(rec), field): (rec, val * scale - (getattr(rec, field, 0) or 0))
        for rec, field, val in unscaled_values
    }
    bake_inter_ui_recipe(inter, spec)
    return LoadedInter(
        font=inter,
        source_path=inter_vf,
        fixed_axes={"opsz": spec.inter_opsz},
        original_gpos=original_gpos,
        unscaled_gpos_store=(unscaled_store, scale) if unscaled_store is not None else None,
        anchor_fractions=anchor_fractions,
        value_fractions=value_fractions,
    )


def assert_public_axis_ready(base: TTFont, spec: Spec) -> None:
    if "fvar" not in base or "avar" not in base or "wght" not in base["avar"].segments:
        raise RuntimeError("并入 Inter 前必须先在基底字体上暴露公开 wght 轴与 avar 映射")
    axis = weight_axis(base)
    actual = (axis.minValue, axis.defaultValue, axis.maxValue)
    if actual != spec.public_limit:
        raise RuntimeError(f"基底公开轴期望 {spec.public_limit}，实际 {actual}")


def adopt_inter_notdef(base: TTFont, inter: TTFont) -> dict[str, int]:
    """采用 Inter 缩放到 1000 UPM 后的 .notdef（656 宽方框，不随字重变化）。"""
    src = inter["glyf"][".notdef"]
    src.expand(inter["glyf"])
    if src.isComposite():
        raise ValueError("Inter .notdef 不能是复合字形")
    glyph = copy.deepcopy(src)
    glyph.program = Program()
    glyph.program.fromBytecode(b"")
    base["glyf"].glyphs[".notdef"] = glyph
    glyph.recalcBounds(base["glyf"])
    adv, lsb = inter["hmtx"].metrics[".notdef"]
    if lsb != glyph.xMin:
        lsb = glyph.xMin
    base["hmtx"].metrics[".notdef"] = (otRound(adv), otRound(lsb))
    if "vmtx" in base:
        y_max = getattr(glyph, "yMax", 0)
        base["vmtx"].metrics[".notdef"] = (1000, otRound(880 - y_max))
    if "gvar" in base:
        base["gvar"].variations.pop(".notdef", None)
    return {"notdef_advance": otRound(adv), "notdef_contours": int(glyph.numberOfContours)}


def append_inter_glyphs(
    base: TTFont,
    loaded: LoadedInter,
    allowed_unicodes: set[int],
    spec: Spec | None = None,
) -> dict[str, Any]:
    spec = spec or load_spec()
    assert_public_axis_ready(base, spec)
    inter = loaded.font
    report: dict[str, Any] = {}
    if "gvar" in inter and "fvar" in inter and "fvar" in base:
        with loaded.exact_curve() as exact:
            report.update(remap_gvar(inter, base, exact=exact))

    src_order = inter.getGlyphOrder()
    src_set = set(src_order)
    existing = set(base.getGlyphOrder())
    rename = {name: prefixed(name) for name in src_order if name != ".notdef"}

    base_before = len(base.getGlyphOrder())
    new_order = list(base.getGlyphOrder())
    for src_name in src_order:
        if src_name == ".notdef":
            continue
        dst_name = rename[src_name]
        if dst_name in existing:
            continue
        g = copy.deepcopy(inter["glyf"][src_name])
        g.expand(inter["glyf"])
        if g.isComposite():
            for comp in g.components:
                if comp.glyphName in src_set:
                    comp.glyphName = rename[comp.glyphName]
        base["glyf"].glyphs[dst_name] = g
        base["hmtx"].metrics[dst_name] = copy.deepcopy(inter["hmtx"].metrics.get(src_name, (0, 0)))
        if "vmtx" in base:
            base["vmtx"].metrics[dst_name] = (1000, 0)
        if "gvar" in base and "gvar" in inter:
            base["gvar"].variations[dst_name] = copy.deepcopy(inter["gvar"].variations.get(src_name, []))
        new_order.append(dst_name)
    base.setGlyphOrder(new_order)
    if "maxp" in base:
        base["maxp"].numGlyphs = len(new_order)

    inter_cmap = inter.getBestCmap() or {}
    base_cmap = base.getBestCmap() or {}
    allowed_inter = {cp for cp in allowed_unicodes if cp in inter_cmap and cp not in base_cmap}
    remapped = 0
    for tbl in _unicode_cmap_tables(base):
        for cp, src_name in inter_cmap.items():
            if cp > 0xFFFF and tbl.format in {0, 2, 4, 6}:
                continue
            if cp in allowed_inter and src_name in rename:
                tbl.cmap[cp] = rename[src_name]
                remapped += 1

    report.update(adopt_inter_notdef(base, inter))
    report.update({
        "base_glyphs_before_inter": base_before,
        "appended_inter_glyphs": len(rename),
        "remapped_inter_cmap_entries": remapped,
    })
    return report
