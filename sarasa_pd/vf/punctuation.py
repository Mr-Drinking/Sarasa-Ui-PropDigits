"""Source Han 破折号与省略号布局，以及幂等的中日韩默认标点规则（退回 Source Han 规则，无竖排分段与 curs）。"""
from __future__ import annotations

import copy
from typing import Any

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot

from ..spec import Spec, load_spec
from .base import _ligature_outputs, _single_sub_mapping
from .layout_utils import Feature, install, routes

REGION_TO_LANG = {
    "CL": "ZHT ",
    "SC": "ZHS ",
    "TC": "ZHT ",
    "HC": "ZHH ",
    "J": "JAN ",
    "K": "KOR ",
}
REGIONAL_PUNCT_SCRIPTS = ("bopo", "hang", "hani", "kana")


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


def resolve_dash_roles(font: TTFont) -> dict[str, str]:
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
    v_single = vert.get(fw) or str(req["vertical_single"])
    v_two = vert.get(fw_two)
    v_three = vert.get(fw_three)
    if not v_single or not v_two or not v_three:
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
    single_lks = _feature_lookups(gsub, {"locl", "vert", "vrt2"})
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
        if idx in single_lks:
            for st in _single_subtables(lk):
                for src in list(st.mapping):
                    if src in punct_sources:
                        del st.mapping[src]


def apply_source_han_punctuation_layout(font: TTFont, region: str, spec: Spec | None = None) -> dict[str, Any]:
    """按 Source Han 原生规则构建破折号与省略号的 ccmp/locl/vert/vrt2，并为本地区 CJK 脚本配置默认标点。"""
    spec = spec or load_spec()
    if "GSUB" not in font:
        return {"punctuation_layout_applied": False}

    dash = resolve_dash_roles(font)
    ellipsis = resolve_ellipsis_roles(font, spec)
    kor_dash = _find_korean_dash_target(font, dash)
    cjk_langs = sorted(spec.features["cjk_locl_languages"])

    dash_sources = {dash["proportional"], dash["fullwidth"], dash["encoded_two"], dash["encoded_three"], dash["vertical_single"]}
    punct_locl_sources = {dash["proportional"], dash["encoded_two"], dash["encoded_three"], ellipsis["proportional"]}
    punct_all_sources = punct_locl_sources | {dash["fullwidth"], dash["fullwidth_two"], dash["fullwidth_three"], ellipsis["fullwidth"]}

    # 清除旧破折号/省略号单替换与连字条目，保证完全幂等
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

    # 3) vert / vrt2：全宽破折号 1/2/3 与比例/全宽省略号 -> 竖排形
    vert_map = {
        dash["fullwidth"]: dash["vertical_single"],
        dash["fullwidth_two"]: dash["vertical_two"],
        dash["fullwidth_three"]: dash["vertical_three"],
        ellipsis["proportional"]: ellipsis["vertical"],
        ellipsis["fullwidth"]: ellipsis["vertical"],
    }
    vert_idx = _append_gsub_lookup(font, _make_single_lookup(vert_map))

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
        # 显式 CJK 语言挂对应标点 locl
        if lang[1] in cjk_langs:
            lidx = kor_locl_idx if lang[1] == "KOR " else std_locl_idx
            feats["locl"] = _add_lookup(feats.get("locl"), lidx, prepend=True)

    # 4b) 为 bopo/hang/hani/kana 四个脚本的默认语言挂本地区标点 locl（DFLT/latn 保持西文默认）
    reg_lang = REGION_TO_LANG[spec.region(region).code]
    reg_lidx = kor_locl_idx if reg_lang == "KOR " else std_locl_idx
    template_key = next((k for k in (("hani", "dflt"), ("latn", "dflt"), ("DFLT", "dflt")) if k in current), None)
    if template_key is None:
        raise ValueError("缺少地区文字系统默认布局模板")

    # 清除非 CJK 脚本默认语言可能遗留的标点-only locl，同时确保四个远东脚本默认语言存在且只挂一次 reg_lidx
    for stag in REGIONAL_PUNCT_SCRIPTS:
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

    # 5) GDEF 中为 2/3 破折号连字标注 ligature (class 2)
    if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None):
        class_defs = font["GDEF"].table.GlyphClassDef.classDefs
        for g in (
            dash["encoded_two"],
            dash["encoded_three"],
            dash["fullwidth_two"],
            dash["fullwidth_three"],
            dash["vertical_two"],
            dash["vertical_three"],
        ):
            class_defs[g] = 2

    return {
        "punctuation_layout_applied": True,
        "default_punctuation_language": reg_lang.strip(),
        "default_punctuation_scripts": list(REGIONAL_PUNCT_SCRIPTS),
        "korean_dash_target": kor_dash,
        "dash_roles": dash,
        "ellipsis_roles": ellipsis,
    }
