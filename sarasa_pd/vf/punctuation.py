"""Source Han 破折号与省略号布局，以及幂等的中日韩默认标点规则。

横排以及竖排 1em、2em 破折号保留 Source Han 原生字形；竖排 3em 破折号（———、⸻）在
vert/vrt2 中以 MultipleSubst 替换为由原生 3em 竖排字形切出的三段 1em 无缝字形
[top, middle, bottom]，使 head 全局包围盒与 vhea.advanceHeightMax 回到上游水平且正斜体无缝。
"""
from __future__ import annotations

import copy
import math
from typing import Any

from fontTools.misc.fixedTools import otRound
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.TupleVariation import TupleVariation

from ..spec import Spec, load_spec
from .base import _ligature_outputs, _single_sub_mapping
from .layout_utils import Feature, install, routes


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


def _multiple_subtables(lookup: ot.Lookup) -> list[ot.MultipleSubst]:
    if lookup.LookupType == 2:
        return list(lookup.SubTable)
    if lookup.LookupType == 7:
        return [
            st.ExtSubTable
            for st in lookup.SubTable
            if getattr(st, "ExtensionLookupType", None) == 2 and getattr(st, "ExtSubTable", None)
        ]
    return []


def _lookup_single_map(gsub: Any, idx: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for st in _single_subtables(gsub.LookupList.Lookup[idx]):
        out.update(st.mapping or {})
    return out


def _append_gsub_lookup(font: TTFont, lookup: ot.Lookup) -> int:
    gsub = font["GSUB"].table
    idx = len(gsub.LookupList.Lookup)
    gsub.LookupList.Lookup.append(lookup)
    gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)
    return idx


def _make_single_lookup(mapping: dict[str, str]) -> ot.Lookup:
    lk = ot.Lookup()
    lk.LookupType = 1
    lk.LookupFlag = 0
    st = ot.SingleSubst()
    st.mapping = dict(mapping)
    lk.SubTable = [st]
    lk.SubTableCount = 1
    return lk


def _make_multiple_lookup(mapping: dict[str, list[str]]) -> ot.Lookup:
    lk = ot.Lookup()
    lk.LookupType = 2
    lk.LookupFlag = 0
    st = ot.MultipleSubst()
    st.mapping = {k: list(v) for k, v in mapping.items()}
    lk.SubTable = [st]
    lk.SubTableCount = 1
    return lk


def _make_ligature_lookup(rules: dict[str, list[tuple[list[str], str]]]) -> ot.Lookup:
    lk = ot.Lookup()
    lk.LookupType = 4
    lk.LookupFlag = 0
    st = ot.LigatureSubst()
    st.ligatures = {}
    for first, items in rules.items():
        ligs = []
        for comps, out_glyph in sorted(items, key=lambda x: -len(x[0])):
            lig = ot.Ligature()
            lig.Component = list(comps)
            lig.CompCount = len(comps) + 1
            lig.LigGlyph = out_glyph
            ligs.append(lig)
        st.ligatures[first] = ligs
    lk.SubTable = [st]
    lk.SubTableCount = 1
    return lk


def _multiple_sub_mapping(font: TTFont, tag: str) -> dict[str, list[str]]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList or not font["GSUB"].table.LookupList:
        return {}
    table = font["GSUB"].table
    out: dict[str, list[str]] = {}
    for rec in table.FeatureList.FeatureRecord:
        if rec.FeatureTag != tag:
            continue
        for idx in rec.Feature.LookupListIndex or []:
            for st in _multiple_subtables(table.LookupList.Lookup[idx]):
                for src, dst in (st.mapping or {}).items():
                    out[src] = list(dst)
    return out


def _unique_glyph_name(existing: set[str], preferred: str) -> str:
    name = preferred
    suffix = 1
    while name in existing:
        suffix += 1
        name = f"{preferred}.{suffix}"
    existing.add(name)
    return name


def _extract_v3_corners(
    font: TTFont, v3_name: str
) -> tuple[
    tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]],
    tuple[int, int, int, int],
]:
    glyf = font["glyf"]
    g = glyf[v3_name]
    g.expand(glyf)
    if g.isComposite() or g.numberOfContours != 1:
        raise ValueError(f"竖排 3em 破折号 {v3_name} 必须是单轮廓简单字形（实际 numberOfContours={g.numberOfContours}）")
    coords = [(int(x), int(y)) for x, y in g.coordinates]
    if len(coords) != 4 or not all(int(f) & 1 for f in g.flags):
        raise ValueError(
            f"竖排 3em 破折号 {v3_name} 必须是 4 个在曲线上的点（实际 {len(coords)} 点，flags={list(g.flags)}）"
        )
    by_y = sorted(range(4), key=lambda i: (coords[i][1], coords[i][0]))
    bot_pair = sorted(by_y[:2], key=lambda i: coords[i][0])
    top_pair = sorted(by_y[2:], key=lambda i: coords[i][0])
    idx_bl, idx_br = bot_pair[0], bot_pair[1]
    idx_tl, idx_tr = top_pair[0], top_pair[1]
    return (coords[idx_tl], coords[idx_tr], coords[idx_br], coords[idx_bl]), (idx_tl, idx_tr, idx_br, idx_bl)


def _materialize_glyph_variations(font: TTFont, gname: str) -> list[tuple[dict[str, tuple[float, float, float]], list[tuple[int, int]]]]:
    if "gvar" not in font:
        return []
    raw_vars = font["gvar"].variations.get(gname, [])
    if not raw_vars:
        return []
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics
    vmtx = font["vmtx"].metrics if "vmtx" in font else None
    coords, controls = glyf._getCoordinatesAndControls(gname, hmtx, vmtx)
    ends = controls.endPts if controls.numberOfContours >= 1 else list(range(len(controls.endPts)))
    out: list[tuple[dict[str, tuple[float, float, float]], list[tuple[int, int]]]] = []
    for var in raw_vars:
        cloned = copy.deepcopy(var)
        if any(pt is None for pt in cloned.coordinates):
            cloned.calcInferredDeltas(coords, ends)
            cloned.roundDeltas()
        pts = [(int(otRound(p[0])), int(otRound(p[1]))) if p is not None else (0, 0) for p in cloned.coordinates]
        out.append((copy.deepcopy(cloned.axes), pts))
    return out


def slice_vertical_three_em_dash(
    font: TTFont,
    v3_name: str,
    spec: Spec,
    *,
    italic: bool = False,
    overlap: int = 16,
) -> tuple[str, str, str]:
    """将原生 3em 竖排破折号按 y=880 与 y=-120 格线切分为三段 1em 重叠无缝字形 [top, middle, bottom]。"""
    corners, (idx_tl, idx_tr, idx_br, idx_bl) = _extract_v3_corners(font, v3_name)
    tl, tr, br, bl = corners
    y_upper_grid = 880
    y_lower_grid = -120
    if min(tl[1], tr[1]) <= y_upper_grid or max(bl[1], br[1]) >= y_lower_grid:
        raise ValueError(f"竖排 3em 破折号 {v3_name} 纵向范围未覆盖三格：{corners}")

    tan_theta = math.tan(math.radians(abs(spec.italic_angle))) if italic else 0.0

    def _edge_x(p_bot: tuple[int, int], p_top: tuple[int, int], y_cut: int) -> int:
        if not italic and p_bot[0] == p_top[0]:
            return p_top[0]
        t = (y_cut - p_bot[1]) / (p_top[1] - p_bot[1])
        return otRound(p_bot[0] + (p_top[0] - p_bot[0]) * t)

    y_top_bot = y_upper_grid - overlap
    y_mid_top = y_upper_grid + overlap
    y_mid_bot = y_lower_grid - overlap
    y_bot_top = y_lower_grid + overlap

    # 三段均对齐标准 1em 竖排格（vOriginY = yMax + tsb = 880，vAdv = 1000）：
    # top 下移 1000 -> [-120-ov, yTop-1000]，tsb = 880 - yMax（400 字重为 72）
    # middle 不移 -> [-120-ov, 880+ov]，tsb = -ov（使 vOriginY 仍为 880，避免接缝下移与斜体错位）
    # bottom 上移 1000 -> [yBot+1000, 880+ov]，tsb = -ov（同上）
    seg_specs = (
        (
            "top",
            [
                (tl[0], tl[1] - 1000),
                (tr[0], tr[1] - 1000),
                (_edge_x(br, tr, y_top_bot), y_top_bot - 1000),
                (_edge_x(bl, tl, y_top_bot), y_top_bot - 1000),
            ],
        ),
        (
            "mid",
            [
                (_edge_x(bl, tl, y_mid_top), y_mid_top),
                (_edge_x(br, tr, y_mid_top), y_mid_top),
                (_edge_x(br, tr, y_mid_bot), y_mid_bot),
                (_edge_x(bl, tl, y_mid_bot), y_mid_bot),
            ],
        ),
        (
            "bot",
            [
                (_edge_x(bl, tl, y_bot_top), y_bot_top + 1000),
                (_edge_x(br, tr, y_bot_top), y_bot_top + 1000),
                (br[0], br[1] + 1000),
                (bl[0], bl[1] + 1000),
            ],
        ),
    )

    mat_vars = _materialize_glyph_variations(font, v3_name)
    glyf = font["glyf"]
    order = font.getGlyphOrder()
    existing = set(order) | set(glyf.glyphs)
    orig_gdef_class = None
    if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None):
        orig_gdef_class = font["GDEF"].table.GlyphClassDef.classDefs.get(v3_name)

    def _cut_delta(d_edge: tuple[int, int]) -> tuple[int, int]:
        return (otRound(d_edge[0] - d_edge[1] * tan_theta), 0)

    created: list[str] = []
    for role, pts in seg_specs:
        seg_name = _unique_glyph_name(existing, f"{v3_name}.seg_{role}")
        pen = TTGlyphPen(None)
        pen.moveTo(pts[0])
        for pt in pts[1:]:
            pen.lineTo(pt)
        pen.closePath()
        glyph = pen.glyph()
        glyf.glyphs[seg_name] = glyph
        glyph.recalcBounds(glyf)

        font["hmtx"].metrics[seg_name] = (1000, int(glyph.xMin))
        if "vmtx" in font:
            tsb = int(y_upper_grid - glyph.yMax)
            font["vmtx"].metrics[seg_name] = (1000, tsb)
        if "gvar" in font:
            seg_vars: list[TupleVariation] = []
            for axes, deltas in mat_vars:
                d_tl, d_tr, d_br, d_bl = deltas[idx_tl], deltas[idx_tr], deltas[idx_br], deltas[idx_bl]
                if role == "top":
                    pts_d = [d_tl, d_tr, _cut_delta(d_tr), _cut_delta(d_tl)]
                elif role == "mid":
                    pts_d = [_cut_delta(d_tl), _cut_delta(d_tr), _cut_delta(d_br), _cut_delta(d_bl)]
                else:
                    pts_d = [_cut_delta(d_bl), _cut_delta(d_br), d_br, d_bl]
                # 4 个 phantom point 增量固定为 0，确保全字重保持 advWidth=1000、advHeight=1000、vOriginY=880
                seg_vars.append(TupleVariation(copy.deepcopy(axes), [*pts_d, (0, 0), (0, 0), (0, 0), (0, 0)]))
            font["gvar"].variations[seg_name] = seg_vars
        if orig_gdef_class is not None:
            font["GDEF"].table.GlyphClassDef.classDefs[seg_name] = orig_gdef_class
        order.append(seg_name)
        created.append(seg_name)

    font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(order)
    return created[0], created[1], created[2]


def resolve_dash_roles(font: TTFont) -> dict[str, Any]:
    cmap = font.getBestCmap() or {}
    req = {
        "proportional": cmap.get(0x2014),
        "fullwidth": cmap.get(0x2015),
        "encoded_two": cmap.get(0x2E3A),
        "encoded_three": cmap.get(0x2E3B),
        "vertical_single": cmap.get(0xFE31),
    }
    missing = [k for k, v in req.items() if not v]
    if missing:
        raise ValueError(f"缺少破折号字形：{missing}")
    fw = str(req["fullwidth"])
    prop = str(req["proportional"])
    fw_ligs = _ligature_outputs(font, "ccmp", fw)
    fw_two = fw_ligs.get(2) or (str(req["encoded_two"]) if prop == fw else None)
    fw_three = fw_ligs.get(3) or (str(req["encoded_three"]) if prop == fw else None)
    if not fw_two or not fw_three:
        raise ValueError("上游 ccmp 缺少全宽双/三破折号连字")

    vert = _single_sub_mapping(font, "vert")
    vert.update(_single_sub_mapping(font, "vrt2"))
    vert_multi = _multiple_sub_mapping(font, "vert")
    vert_multi.update(_multiple_sub_mapping(font, "vrt2"))
    v_single = vert.get(fw) or str(req["vertical_single"])
    v_two = vert.get(fw_two)
    v_three = vert.get(fw_three)
    v_three_segs = vert_multi.get(fw_three)
    if v_three_segs is not None and (
        len(v_three_segs) != 3 or any(g not in font["glyf"].glyphs for g in v_three_segs)
    ):
        v_three_segs = None
    if not v_single or not v_two or (not v_three and not v_three_segs):
        raise ValueError("上游 vert/vrt2 缺少竖排破折号字形")
    return {
        "proportional": prop,
        "fullwidth": fw,
        "encoded_two": str(req["encoded_two"]),
        "encoded_three": str(req["encoded_three"]),
        "fullwidth_two": fw_two,
        "fullwidth_three": fw_three,
        "vertical_single": v_single,
        "vertical_two": v_two,
        "vertical_three": v_three,
        "vertical_three_segments": v_three_segs,
    }


def resolve_ellipsis_roles(font: TTFont, spec: Spec) -> dict[str, str]:
    cmap = font.getBestCmap() or {}
    prop = cmap.get(spec.ellipsis_codepoint)
    fw = cmap.get(spec.cjk_ellipsis_codepoint)
    if not prop or not fw:
        raise ValueError("缺少 U+2026 或 U+22EF 省略号字形")
    vert = _single_sub_mapping(font, "vert")
    vert.update(_single_sub_mapping(font, "vrt2"))
    v_target = vert.get(fw) or vert.get(prop)
    if not v_target:
        raise ValueError("缺少竖排省略号字形")
    return {"proportional": prop, "fullwidth": fw, "vertical": v_target}


def _find_korean_dash_target(font: TTFont, roles: dict[str, str]) -> str:
    """从 Source Han 原始 locl 中提取 KOR 专属单破折号替代形。"""
    gsub = font["GSUB"].table
    prop, fw = roles["proportional"], roles["fullwidth"]
    for rec in gsub.FeatureList.FeatureRecord:
        if rec.FeatureTag != "locl":
            continue
        for idx in rec.Feature.LookupListIndex or []:
            m = _lookup_single_map(gsub, idx)
            target = m.get(prop)
            if target and target not in {prop, fw}:
                return target
    return fw


def _feature_lookups(gsub: Any, tags: set[str]) -> set[int]:
    if not gsub.FeatureList:
        return set()
    return {
        int(idx)
        for rec in gsub.FeatureList.FeatureRecord
        if rec.FeatureTag in tags
        for idx in (rec.Feature.LookupListIndex or [])
    }


def _strip_existing_punct_rules(font: TTFont, punct_sources: set[str], dash_sources: set[str]) -> None:
    """仅清除 ccmp/locl/vert/vrt2 中针对这组标点的旧规则，确保重跑幂等且不误伤其他特性。"""
    gsub = font["GSUB"].table
    ccmp_lks = _feature_lookups(gsub, {"ccmp"})
    subst_lks = _feature_lookups(gsub, {"locl", "vert", "vrt2"})
    for idx, lk in enumerate(gsub.LookupList.Lookup):
        if idx in ccmp_lks:
            if lk.LookupType == 4:
                for st in lk.SubTable:
                    for src in list(st.ligatures):
                        if src in dash_sources:
                            del st.ligatures[src]
            elif lk.LookupType == 7:
                for st in lk.SubTable:
                    if getattr(st, "ExtensionLookupType", None) == 4 and getattr(st, "ExtSubTable", None):
                        for src in list(st.ExtSubTable.ligatures):
                            if src in dash_sources:
                                del st.ExtSubTable.ligatures[src]
        if idx in subst_lks:
            for st in _single_subtables(lk):
                for src in list(st.mapping):
                    if src in punct_sources:
                        del st.mapping[src]
            for st in _multiple_subtables(lk):
                for src in list(st.mapping):
                    if src in punct_sources:
                        del st.mapping[src]


def apply_source_han_punctuation_layout(
    font: TTFont,
    region: str,
    spec: Spec | None = None,
    *,
    italic: bool = False,
) -> dict[str, Any]:
    """按 Source Han 规则构建破折号与省略号的 ccmp/locl/vert/vrt2，并为本地区 CJK 脚本配置默认标点。"""
    spec = spec or load_spec()
    if "GSUB" not in font:
        return {"punctuation_layout_applied": False}

    dash = resolve_dash_roles(font)
    ellipsis = resolve_ellipsis_roles(font, spec)
    kor_dash = _find_korean_dash_target(font, dash)
    cjk_langs = sorted(spec.features["cjk_locl_languages"])
    regional_scripts = sorted(spec.features["far_east_scripts"])

    # 若尚未切分竖排 3em 破折号，则由原生 3em 竖排单字形切出三段 1em 无缝重叠字形；重跑时直接复用已切分三段
    v3_segs = dash.get("vertical_three_segments")
    if v3_segs is None:
        v3_name = dash["vertical_three"]
        v3_segs = list(slice_vertical_three_em_dash(font, v3_name, spec, italic=italic))
        dash["vertical_three_segments"] = v3_segs

    dash_sources = {dash["proportional"], dash["fullwidth"], dash["encoded_two"], dash["encoded_three"], dash["vertical_single"]}
    punct_locl_sources = {dash["proportional"], dash["encoded_two"], dash["encoded_three"], ellipsis["proportional"]}
    punct_all_sources = punct_locl_sources | {dash["fullwidth"], dash["fullwidth_two"], dash["fullwidth_three"], ellipsis["fullwidth"]}

    # 清除旧破折号/省略号单替换、多替换与连字条目，保证完全幂等
    _strip_existing_punct_rules(font, punct_all_sources, dash_sources)

    # 1) ccmp：仅连字 proportional 与 fullwidth（绝不连字 U+FE31）
    ccmp_rules: dict[str, list[tuple[list[str], str]]] = {
        dash["proportional"]: [
            ([dash["proportional"], dash["proportional"]], dash["encoded_three"]),
            ([dash["proportional"]], dash["encoded_two"]),
        ]
    }
    if dash["fullwidth"] != dash["proportional"]:
        ccmp_rules[dash["fullwidth"]] = [
            ([dash["fullwidth"], dash["fullwidth"]], dash["fullwidth_three"]),
            ([dash["fullwidth"]], dash["fullwidth_two"]),
        ]
    ccmp_idx = _append_gsub_lookup(font, _make_ligature_lookup(ccmp_rules))

    # 2) 各 CJK 语言的标点 locl（标准形与 KOR 形共用 2 个干净 lookup，绝不重复）
    std_locl = {
        dash["proportional"]: dash["fullwidth"],
        dash["encoded_two"]: dash["fullwidth_two"],
        dash["encoded_three"]: dash["fullwidth_three"],
        ellipsis["proportional"]: ellipsis["fullwidth"],
    }
    kor_locl = {**std_locl, dash["proportional"]: kor_dash}
    std_locl_idx = _append_gsub_lookup(font, _make_single_lookup(std_locl))
    kor_locl_idx = std_locl_idx if kor_locl == std_locl else _append_gsub_lookup(font, _make_single_lookup(kor_locl))

    # 3) vert / vrt2：1em/2em 破折号与省略号走单替换；3em 破折号由 MultipleSubst 替换为切出的三段 1em 无缝字形
    vert_map = {
        dash["fullwidth"]: dash["vertical_single"],
        dash["fullwidth_two"]: dash["vertical_two"],
        ellipsis["proportional"]: ellipsis["vertical"],
        ellipsis["fullwidth"]: ellipsis["vertical"],
    }
    vert_idx = _append_gsub_lookup(font, _make_single_lookup(vert_map))
    vert_multi_idx = _append_gsub_lookup(
        font,
        _make_multiple_lookup({dash["fullwidth_three"]: list(v3_segs)}),
    )

    # 4) 按 routes 统一干净装配，保证完全幂等且不制造重复 lookup
    gsub = font["GSUB"].table
    current = routes(gsub)

    def _add_lookup(feat: Feature | None, idx: int, prepend: bool = False) -> Feature:
        old = list(feat.lookups) if feat else []
        merged = [idx, *old] if prepend else [*old, idx]
        return Feature(tuple(dict.fromkeys(merged)), feat.params if feat else None, feat.required if feat else False)

    # 4a) 全部语言挂 ccmp、vert、vrt2（若已有该特性则追加）
    for lang, feats in current.items():
        if "ccmp" in feats:
            feats["ccmp"] = _add_lookup(feats["ccmp"], ccmp_idx)
        for vtag in ("vert", "vrt2"):
            if vtag in feats:
                feats[vtag] = _add_lookup(feats[vtag], vert_idx)
                feats[vtag] = _add_lookup(feats[vtag], vert_multi_idx)
        # 显式 CJK 语言挂对应标点 locl
        if lang[1] in cjk_langs:
            lidx = kor_locl_idx if lang[1] == "KOR " else std_locl_idx
            feats["locl"] = _add_lookup(feats.get("locl"), lidx, prepend=True)

    # 4b) 为远东脚本（spec.features["far_east_scripts"]）默认语言挂本地区标点 locl（DFLT/latn 保持西文默认）
    reg_lang = spec.region(region).default_ot_lang
    reg_lidx = kor_locl_idx if reg_lang == "KOR " else std_locl_idx
    template_key = next((k for k in (("hani", "dflt"), ("latn", "dflt"), ("DFLT", "dflt")) if k in current), None)
    if template_key is None:
        raise ValueError("缺少地区文字系统默认布局模板")

    for stag in regional_scripts:
        key = (stag, "dflt")
        if key not in current:
            current[key] = copy.deepcopy(current[template_key])
        current[key]["locl"] = _add_lookup(current[key].get("locl"), reg_lidx, prepend=True)

    # 清理 DFLT/latn 下等价于默认语言的冗余 ENG 语言系统
    for stag in ("DFLT", "latn"):
        if (stag, "dflt") in current and (stag, "ENG ") in current:
            if current[(stag, "ENG ")] == current[(stag, "dflt")]:
                del current[(stag, "ENG ")]

    install(gsub, current)

    # 5) GDEF 中为保留的 2/3 破折号连字标注 ligature (class 2)
    if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None):
        class_defs = font["GDEF"].table.GlyphClassDef.classDefs
        for g in (
            dash["encoded_two"],
            dash["encoded_three"],
            dash["fullwidth_two"],
            dash["fullwidth_three"],
            dash["vertical_two"],
        ):
            class_defs[g] = 2

    return {
        "punctuation_layout_applied": True,
        "default_punctuation_language": reg_lang.strip(),
        "default_punctuation_scripts": regional_scripts,
        "korean_dash_target": kor_dash,
        "dash_roles": dash,
        "ellipsis_roles": ellipsis,
    }
