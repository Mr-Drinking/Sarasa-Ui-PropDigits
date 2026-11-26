"""面向最终产品契约的规则检查：覆盖码位/UVS、比例数字、上下文保护、标点路由、字偶距与元数据。"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any

from fontTools.misc.fixedTools import otRound
from fontTools.otlLib.maxContextCalc import maxCtxFont
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap
from fontTools.varLib.varStore import VarStoreInstancer
import uharfbuzz as hb

from ..spec import Spec, WeightSpec, is_ideograph, load_spec, use_inter_codepoint
from ..vf.base import _single_sub_mapping
from ..vf.layout_utils import routes

PANOSE_FIELDS: tuple[str, ...] = (
    "bFamilyType",
    "bSerifStyle",
    "bWeight",
    "bProportion",
    "bContrast",
    "bStrokeVariation",
    "bArmStyle",
    "bLetterForm",
    "bMidline",
    "bXHeight",
)

OS2_SCALAR_FIELDS: tuple[str, ...] = (
    "version",
    "xAvgCharWidth",
    "usWeightClass",
    "usWidthClass",
    "fsType",
    "ySubscriptXSize",
    "ySubscriptYSize",
    "ySubscriptXOffset",
    "ySubscriptYOffset",
    "ySuperscriptXSize",
    "ySuperscriptYSize",
    "ySuperscriptXOffset",
    "ySuperscriptYOffset",
    "yStrikeoutSize",
    "yStrikeoutPosition",
    "sFamilyClass",
    "ulUnicodeRange1",
    "ulUnicodeRange2",
    "ulUnicodeRange3",
    "ulUnicodeRange4",
    "achVendID",
    "fsSelection",
    "usFirstCharIndex",
    "usLastCharIndex",
    "sTypoAscender",
    "sTypoDescender",
    "sTypoLineGap",
    "usWinAscent",
    "usWinDescent",
    "ulCodePageRange1",
    "ulCodePageRange2",
    "sxHeight",
    "sCapHeight",
    "usDefaultChar",
    "usBreakChar",
    "usMaxContext",
)


@dataclass
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)


def _shape(
    hb_font: hb.Font,
    text: str,
    *,
    features: dict[str, bool] | None = None,
    script: str | None = None,
    language: str | None = None,
    direction: str = "ltr",
) -> list[tuple[int, int, int, int, int]]:
    buf = hb.Buffer()
    buf.add_str(text)
    buf.direction = direction
    if script:
        buf.script = script
    if language:
        buf.language = language
    buf.guess_segment_properties()
    hb.shape(hb_font, buf, features or {})
    return [
        (info.codepoint, pos.x_advance, pos.y_advance, pos.x_offset, pos.y_offset)
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions)
    ]


def _check_cmap_and_uvs(
    font: TTFont,
    res: CheckResult,
    *,
    reference: TTFont | None,
    subset_mode: str,
    require_reference: bool = True,
    strict_uvs_kind: bool = True,
) -> None:
    cmap = font.getBestCmap() or {}
    order = set(font.getGlyphOrder())
    res.metrics["codepoints"] = len(cmap)

    if reference is None:
        if require_reference and subset_mode == "full":
            res.fail("缺少上游 Sarasa Ui 参照字体，无法核对 cmap 与 UVS 一致性")
    else:
        ref_cmap = reference.getBestCmap() or {}
        if subset_mode == "full":
            missing = sorted(set(ref_cmap) - set(cmap))
            extra = sorted(set(cmap) - set(ref_cmap))
            if missing:
                res.fail(f"cmap 缺少上游码位 {len(missing)} 个，样例：{[hex(c) for c in missing[:6]]}")
            if extra:
                res.fail(f"cmap 多出上游外码位 {len(extra)} 个，样例：{[hex(c) for c in extra[:6]]}")

    # cmap format 14 存在且目标字形均在 glyf 中可达
    sub14 = [t for t in font["cmap"].tables if t.format == 14]
    if not sub14:
        res.fail("缺少 cmap format 14 (UVS) 子表")
        return
    uvs_count = 0
    dangling = 0
    for tbl in sub14:
        for _sel, entries in tbl.uvsDict.items():
            for _cp, gname in entries:
                uvs_count += 1
                if gname is not None and gname not in order:
                    dangling += 1
    res.metrics["uvs_entries"] = uvs_count
    if uvs_count == 0:
        res.fail("cmap format 14 为空")
    if dangling:
        res.fail(f"cmap format 14 存在 {dangling} 个悬空目标字形")

    if reference is not None and subset_mode == "full":
        ref14 = [t for t in reference["cmap"].tables if t.format == 14]
        if ref14:
            ref_map = {(sel, cp): (g is not None) for t in ref14 for sel, es in t.uvsDict.items() for cp, g in es}
            cur_map = {(sel, cp): (g is not None) for t in sub14 for sel, es in t.uvsDict.items() for cp, g in es}
            miss_uvs = sorted(set(ref_map) - set(cur_map))
            extra_uvs = sorted(set(cur_map) - set(ref_map))
            if miss_uvs:
                res.fail(f"cmap format 14 相比上游缺失 {len(miss_uvs)} 条 UVS 映射，样例：{miss_uvs[:5]}")
            if extra_uvs:
                res.fail(f"cmap format 14 相比上游多出 {len(extra_uvs)} 条 UVS 映射，样例：{extra_uvs[:5]}")
            if strict_uvs_kind:
                kind_diffs = sorted(k for k in (set(ref_map) & set(cur_map)) if ref_map[k] != cur_map[k])
                if kind_diffs:
                    res.fail(f"cmap format 14 有 {len(kind_diffs)} 条默认/非默认映射状态与上游不符，样例：{kind_diffs[:5]}")


def _check_propdigits_and_tnum(font: TTFont, hb_font: hb.Font, res: CheckResult, spec: Spec, is_vf: bool) -> None:
    cmap = font.getBestCmap() or {}
    weights = list(range(200, 901, 25)) if is_vf else [int(font["OS/2"].usWeightClass)]

    for w in weights:
        if is_vf:
            hb_font.set_variations({"wght": float(w)})
        # 1) 默认数字应为比例宽（1 与 0 宽度不同）
        def_digits = [_shape(hb_font, str(d))[0][1] for d in range(10)]
        if len(set(def_digits)) <= 1:
            res.fail(f"wght={w}: 默认数字未呈现比例宽：{def_digits}")
            break
        # 2) 37 个码位默认与开启 pnum 宽度完全相同
        for cp in sorted(spec.propdigits_codepoints):
            if cp not in cmap:
                continue
            ch = chr(cp)
            w_def = _shape(hb_font, ch)[0][1]
            w_pnum = _shape(hb_font, ch, features={"pnum": True})[0][1]
            if w_def != w_pnum:
                res.fail(f"wght={w}: 比例码位 U+{cp:04X} 默认宽 {w_def} != pnum 宽 {w_pnum}")
                break
        # 3) tnum 模式下 0-9 严格等宽，且 U+2007 与 tnum+zero 保持同一等宽
        tnum_digits = [_shape(hb_font, str(d), features={"tnum": True})[0][1] for d in range(10)]
        if len(set(tnum_digits)) != 1:
            res.fail(f"wght={w}: tnum 数字未严格等宽：{tnum_digits}")
            break
        tab_w = tnum_digits[0]
        if 0x2007 in cmap:
            fig_w = _shape(hb_font, "\u2007")[0][1]
            if fig_w != tab_w:
                res.fail(f"wght={w}: U+2007 宽 {fig_w} != tnum 数字宽 {tab_w}")
                break
        z_gid_plain = _shape(hb_font, "0", features={"tnum": True})[0][0]
        z_gid_slash, z_w_slash = _shape(hb_font, "0", features={"tnum": True, "zero": True})[0][:2]
        if z_gid_slash == z_gid_plain or z_w_slash != tab_w:
            res.fail(f"wght={w}: tnum+zero 异常（gid={z_gid_plain}->{z_gid_slash}, w={z_w_slash} vs {tab_w}）")
            break
        # 4) U+002D 默认与 tnum 不同（开启 tnum 变宽）
        h_def = _shape(hb_font, "-")[0][1]
        h_tnum = _shape(hb_font, "-", features={"tnum": True})[0][1]
        if h_def == h_tnum:
            res.fail(f"wght={w}: U+002D 开启 tnum 未切换等宽（{h_def}）")
            break

    if is_vf:
        hb_font.set_variations({"wght": 400.0})


def _check_inter_calt_and_guards(font: TTFont, hb_font: hb.Font, res: CheckResult) -> None:
    def gids(text: str, feats: dict[str, bool] | None = None) -> list[int]:
        return [g[0] for g in _shape(hb_font, text, features=feats)]

    # 不依赖 post 字形名（post format 3 无名称表），直接以 GID 与几何 y_bearing 精确核对
    base_colon = gids(":")[0]
    case_colon = gids(":", {"case": True})[0]
    tnum_colon = gids(":", {"tnum": True})[0]
    tnum_case_colon = gids(":", {"case": True, "tnum": True})[0]
    if case_colon == base_colon:
        res.fail("字体缺少冒号的 case 上浮替代形")
        return
    ext_base = hb_font.get_glyph_extents(base_colon)
    ext_case = hb_font.get_glyph_extents(case_colon)
    if ext_base is not None and ext_case is not None and ext_case.y_bearing <= ext_base.y_bearing:
        res.fail(f"冒号 case 形未实际高于基础冒号（y_bearing {ext_case.y_bearing} <= {ext_base.y_bearing}）")

    # 1) 冒号在数字间（默认 / tnum / pnum / tnum+zero 全部组合下）及数字邻接（1:、:2）应上浮
    for feats, expected_colon in (
        ({}, case_colon),
        ({"pnum": True}, case_colon),
        ({"tnum": True}, tnum_case_colon),
        ({"tnum": True, "zero": True}, tnum_case_colon),
    ):
        seq = gids("1:2", feats)
        if len(seq) != 3 or seq[1] != expected_colon:
            res.fail(f"数字间冒号未上浮（feats={feats}, actual_gid={seq[1] if len(seq) >= 2 else None}, expected={expected_colon}）")
    seq_after_digit = gids("1:")
    if len(seq_after_digit) != 2 or seq_after_digit[1] != case_colon:
        res.fail(f"1: 冒号未上浮（got {seq_after_digit}, expected colon={case_colon}）")
    seq_before_digit = gids(":2")
    if len(seq_before_digit) != 2 or seq_before_digit[0] != case_colon:
        res.fail(f":2 冒号未上浮（got {seq_before_digit}, expected colon={case_colon}）")

    # 2) 非纯数字上下文（1:a、a:2、a:b 及 Left:1 开 dlig）冒号不得误上浮
    for sample_txt in ("1:a", "a:2", "a:b"):
        seq = gids(sample_txt)
        if len(seq) != 3 or seq[1] != base_colon:
            res.fail(f"{sample_txt} 冒号不应上浮（actual_gid={seq[1] if len(seq) >= 2 else None}, expected={base_colon}）")
    left1 = gids("Left:1", {"dlig": True})
    if len(left1) >= 5 and left1[4] != base_colon:
        res.fail(f"Left:1 开 dlig 时冒号误上浮（gid={left1[4]} != base {base_colon}）")
    left1_tnum = gids("Left:1", {"dlig": True, "tnum": True})
    if len(left1_tnum) >= 5 and left1_tnum[4] != tnum_colon:
        res.fail(f"Left:1 开 dlig+tnum 时冒号误变（gid={left1_tnum[4]} != tnum {tnum_colon}）")

    # 3) 代码比较运算符 x<-1 / 3<-2 不得误触发左箭头连字
    if len(gids("x<-1")) != 4 or len(gids("3<-2")) != 4:
        res.fail(f"x<-1 或 3<-2 误触发箭头连字 -> {gids('x<-1')}, {gids('3<-2')}")
    # 4) 独立箭头连字 -> 应生效
    if len(gids("->")) != 1:
        res.fail(f"箭头连字 -> 未生效 -> {gids('->')}")
    # 5) 日期 2026/11/19 开 frac 不得误变分数斜杠
    date_plain = gids("2026/11/19")
    date_frac = gids("2026/11/19", {"frac": True})
    if date_frac != date_plain:
        res.fail(f"2026/11/19 开 frac 被破坏 -> {date_frac}")
    # 6) H(H) 开 sups/subs 保持 Inter 原生 .case 括号
    lparen_case = gids("(", {"case": True})[0]
    rparen_case = gids(")", {"case": True})[0]
    for ftag in ("sups", "subs"):
        seq = gids("H(H)", {ftag: True})
        if len(seq) != 4 or seq[1] != lparen_case or seq[3] != rparen_case:
            res.fail(f"H(H) 开 {ftag} 括号未走 .case（got {seq}, expected parens {lparen_case}/{rparen_case}）")


def _check_chws_and_punctuation(
    font: TTFont,
    hb_font: hb.Font,
    res: CheckResult,
    region: str,
    spec: Spec,
    *,
    italic: bool = False,
    is_vf: bool = False,
) -> None:
    order = font.getGlyphOrder()
    cmap = font.getBestCmap() or {}
    # 1) chws / vchw 存在且均实际产生挤压
    for tag in ("chws", "vchw"):
        has_tag = "GPOS" in font and font["GPOS"].table.FeatureList and any(
            r.FeatureTag == tag for r in font["GPOS"].table.FeatureList.FeatureRecord
        )
        if not has_tag:
            res.fail(f"GPOS 缺少 {tag} 特性")
    adv_off = sum(x[1] for x in _shape(hb_font, "「「", features={"chws": False}, script="hani"))
    adv_on = sum(x[1] for x in _shape(hb_font, "「「", features={"chws": True}, script="hani"))
    if adv_on >= adv_off:
        res.fail(f"chws 未对「「生效（off={adv_off}, on={adv_on}）")
    vadv_off = sum(abs(x[2]) for x in _shape(hb_font, "「「", features={"vchw": False}, script="hani", direction="ttb"))
    vadv_on = sum(abs(x[2]) for x in _shape(hb_font, "「「", features={"vchw": True}, script="hani", direction="ttb"))
    if vadv_on >= vadv_off:
        res.fail(f"vchw 未对竖排「「生效（off={vadv_off}, on={vadv_on}）")

    # 2) 标点与语言路由（对应 docs/规格.md §4.1）
    latn_ell_gid, latn_ell_w = _shape(hb_font, "…", script="latn")[0][:2]
    latn_ell = order[latn_ell_gid]
    cjk_ref_ell_gid = font.getGlyphID(cmap[spec.cjk_ellipsis_codepoint]) if spec.cjk_ellipsis_codepoint in cmap else -1
    if latn_ell_gid == cjk_ref_ell_gid:
        res.fail(f"latn/dflt 省略号不应使用全宽居中省略号（gid={latn_ell_gid}）")
    ext_latn_ell = hb_font.get_glyph_extents(latn_ell_gid)
    ext_cjk_ell = hb_font.get_glyph_extents(cjk_ref_ell_gid) if cjk_ref_ell_gid >= 0 else None
    if ext_latn_ell is not None and ext_cjk_ell is not None and ext_latn_ell.y_bearing >= ext_cjk_ell.y_bearing:
        res.fail(f"latn/dflt 省略号应位于基线下沉，实际 y_bearing={ext_latn_ell.y_bearing} >= cjk {ext_cjk_ell.y_bearing}")
    for sc in ("cyrl", "grek"):
        sc_ell = order[_shape(hb_font, "…", script=sc)[0][0]]
        if sc_ell != latn_ell:
            res.fail(f"{sc}/dflt 省略号应为西文形 {latn_ell}，实际 {sc_ell}")
    for sc in sorted(spec.features["far_east_scripts"]):
        cjk_ell_gid, cjk_ell_w = _shape(hb_font, "…", script=sc)[0][:2]
        if order[cjk_ell_gid] == latn_ell or cjk_ell_w != 1000:
            res.fail(f"{sc}/dflt 省略号未切到全宽形（glyph={order[cjk_ell_gid]}, w={cjk_ell_w}）")

    # 破折号路由：西文脚本保持比例形；显式 CJK 语言切全宽/2em/3em；远东脚本默认随地区
    latn_dash_w = _shape(hb_font, "—", script="latn")[0][1]
    if latn_dash_w >= 1000:
        res.fail(f"latn/dflt 单破折号应为比例宽，实际 {latn_dash_w}")
    for lang in ("zh-Hans", "zh-Hant", "zh-HK", "ja"):
        d1 = _shape(hb_font, "—", script="hani", language=lang)
        d2 = _shape(hb_font, "——", script="hani", language=lang)
        d3 = _shape(hb_font, "———", script="hani", language=lang)
        if len(d1) != 1 or d1[0][1] != 1000:
            res.fail(f"{lang} 单破折号应为 1000 全宽，实际 {d1}")
        if len(d2) != 1 or d2[0][1] != 2000:
            res.fail(f"{lang} 双破折号应合为 2em（2000），实际 {d2}")
        if len(d3) != 1 or d3[0][1] != 3000:
            res.fail(f"{lang} 三破折号应合为 3em（3000），实际 {d3}")
    ko_d1 = _shape(hb_font, "—", script="hang", language="ko")
    ko_d2 = _shape(hb_font, "——", script="hang", language="ko")
    if len(ko_d2) != 1 or ko_d2[0][1] != 2000:
        res.fail(f"ko 双破折号应合为 2em（2000），实际 {ko_d2}")
    expected_fe_d1_gid = ko_d1[0][0] if spec.region(region).default_ot_lang == "KOR " else _shape(hb_font, "—", script="hani", language="zh-Hans")[0][0]
    for sc in sorted(spec.features["far_east_scripts"]):
        fe_d1 = _shape(hb_font, "—", script=sc)
        fe_d2 = _shape(hb_font, "——", script=sc)
        if len(fe_d1) != 1 or fe_d1[0][0] != expected_fe_d1_gid:
            res.fail(f"{sc}/dflt 单破折号未按地区路由（got {fe_d1}, expected gid={expected_fe_d1_gid}）")
        if len(fe_d2) != 1 or fe_d2[0][1] != 2000:
            res.fail(f"{sc}/dflt 双破折号未合为 2em（got {fe_d2}）")

    # 竖排破折号：2em 保持原生单字形 -2000；3em（———、⸻）切成三段 1em 无缝重叠字形；4em 为 3em 三段 + 1em
    v1 = _shape(hb_font, "—", script="hani", language="zh-Hans", direction="ttb")
    v2 = _shape(hb_font, "——", script="hani", language="zh-Hans", direction="ttb")
    if len(v1) != 1 or v1[0][2] != -1000:
        res.fail(f"竖排单破折号应为 1 字形 -1000，实际 {v1}")
    if len(v2) != 1 or v2[0][2] != -2000:
        res.fail(f"竖排双破折号应为 1 字形 -2000，实际 {v2}")

    tan_t = math.tan(math.radians(abs(spec.italic_angle))) if italic else 0.0
    test_weights = (200, 400, 900) if is_vf else (int(font["OS/2"].usWeightClass),)
    v3_gids_default: list[int] | None = None
    try:
        for w in test_weights:
            if is_vf:
                hb_font.set_variations({"wght": float(w)})
            for vtxt in ("———", "\u2E3B"):
                v3 = _shape(hb_font, vtxt, script="hani", language="zh-Hans", direction="ttb")
                if len(v3) != 3 or any(x[2] != -1000 for x in v3) or len({x[0] for x in v3}) != 3:
                    res.fail(f"wght={w} 竖排 3em 破折号 {vtxt!r} 应为 3 个独立 1em 段（总步进 -3000），实际 {v3}")
                    continue
                if vtxt == "———" and (not is_vf or w == 400):
                    v3_gids_default = [x[0] for x in v3]
                boxes: list[tuple[int, int, int, int]] = []
                pen_y = 0
                missing_ext = False
                for gid, _xa, ya, xo, yo in v3:
                    ext = hb_font.get_glyph_extents(gid)
                    if ext is None:
                        res.fail(f"wght={w} 竖排 3em 段 gid={gid} 缺少墨迹外框")
                        missing_ext = True
                        break
                    x_left = xo + ext.x_bearing
                    x_right = x_left + ext.width
                    y_top = pen_y + yo + ext.y_bearing
                    y_bot = y_top + ext.height
                    boxes.append((x_left, y_bot, x_right, y_top))
                    pen_y += ya
                if missing_ext or len(boxes) != 3:
                    continue
                # 顶部/底部边距应与原生 3em 一致（±6 unit 内，400 为 -72 / -2928，900 为 -75 / -2925）
                if abs(boxes[0][3] - (-72)) > 6 or abs(boxes[2][1] - (-2928)) > 6:
                    res.fail(f"wght={w} 竖排 3em {vtxt!r} 首尾边距异常：top={boxes[0][3]}, bot={boxes[2][1]}")
                # 相邻两段必须墨迹重叠（理论 32 unit，要求 >=16）且横向连续
                stroke_ws: list[float] = []
                for idx in range(3):
                    xl, yb, xr, yt = boxes[idx]
                    stroke_ws.append((xr - xl) - (yt - yb) * tan_t)
                for idx in range(2):
                    xl0, yb0, xr0, yt0 = boxes[idx]
                    xl1, yb1, xr1, yt1 = boxes[idx + 1]
                    ov = yt1 - yb0
                    if ov < 16 or ov > 64:
                        res.fail(f"wght={w} 竖排 3em {vtxt!r} 第 {idx}->{idx + 1} 接缝未重叠（overlap={ov}，box0={boxes[idx]} box1={boxes[idx + 1]}）")
                    if not italic:
                        if abs(xl0 - xl1) > 1 or abs(xr0 - xr1) > 1:
                            res.fail(f"wght={w} 正体竖排 3em {vtxt!r} 第 {idx}->{idx + 1} 横向错位：[{xl0},{xr0}] vs [{xl1},{xr1}]")
                    else:
                        exp_dxl = (yb0 - yb1) * tan_t
                        exp_dxr = (yt0 - yt1) * tan_t
                        if abs((xl0 - xl1) - exp_dxl) > 3.0 or abs((xr0 - xr1) - exp_dxr) > 3.0:
                            res.fail(
                                f"wght={w} 斜体竖排 3em {vtxt!r} 第 {idx}->{idx + 1} 斜边不连续："
                                f"dxl={xl0 - xl1} (exp {exp_dxl:.1f}), dxr={xr0 - xr1} (exp {exp_dxr:.1f})"
                            )
                if max(stroke_ws) - min(stroke_ws) > 3.0 or min(stroke_ws) < 20.0:
                    res.fail(f"wght={w} 竖排 3em {vtxt!r} 三段笔画宽度不一致：{stroke_ws}")
    finally:
        if is_vf:
            hb_font.set_variations({"wght": 400.0})

    v4 = _shape(hb_font, "————", script="hani", language="zh-Hans", direction="ttb")
    if (
        len(v4) != 4
        or sum(x[2] for x in v4) != -4000
        or (v3_gids_default is not None and [x[0] for x in v4[:3]] != v3_gids_default)
        or (len(v1) == 1 and v4[3][0] != v1[0][0])
    ):
        res.fail(f"竖排四破折号 ———— 应与 Source Han 一致（3em 三段 + 1em，总步进 -4000），实际 {v4}")

    # 连续 U+FE31 不得被 ccmp 吞成单字形
    fe31 = _shape(hb_font, "\uFE31\uFE31", script="hani")
    if len(fe31) != 2:
        res.fail(f"U+FE31 连续破折号被 ccmp 误合并（len={len(fe31)}）")

    # 3) 西里尔、希腊默认语言共享 DFLT：tnum、tnum+zero、frac、mark/mkmk
    gpos_routes = routes(font["GPOS"].table) if "GPOS" in font else {}
    for sc, txt, mark_sample in (("cyrl", "Время 0123", "и\u0301"), ("grek", "αβ 0123", "Γ\u0301")):
        ws = [x[1] for x in _shape(hb_font, txt, features={"tnum": True}, script=sc)[-4:]]
        if len(set(ws)) != 1:
            res.fail(f"{sc} 默认语言下 tnum 数字未等宽：{ws}")
        tab_w = ws[0]
        z_plain = _shape(hb_font, "0", features={"tnum": True}, script=sc)[0][0]
        z_slash, z_slash_w = _shape(hb_font, "0", features={"tnum": True, "zero": True}, script=sc)[0][:2]
        if z_slash == z_plain or z_slash_w != tab_w:
            res.fail(f"{sc} 默认语言下 tnum+zero 异常（gid={z_plain}->{z_slash}, w={z_slash_w} vs {tab_w}）")
        f_off = [x[0] for x in _shape(hb_font, "1/2", features={"frac": False}, script=sc)]
        f_on = [x[0] for x in _shape(hb_font, "1/2", features={"frac": True}, script=sc)]
        if f_on == f_off:
            res.fail(f"{sc} 默认语言下 frac 未对 1/2 生效")
        m_off = _shape(hb_font, mark_sample, features={"mark": False}, script=sc)
        m_on = _shape(hb_font, mark_sample, features={"mark": True}, script=sc)
        if len(m_on) >= 2 and len(m_off) >= 2 and m_on[1][3:5] == m_off[1][3:5]:
            res.fail(f"{sc} 默认语言下 mark 组合重音未定位生效（{mark_sample!r}: off={m_off}, on={m_on}）")
        sc_gpos = gpos_routes.get((sc, "dflt"), {})
        for req_tag in ("mark", "mkmk"):
            if req_tag not in sc_gpos:
                res.fail(f"GPOS {sc}/dflt 缺少 {req_tag} 特性")

    # 4) 古韩文 L+V+T 合成保护（使用不会预组合的 U+1100 U+1188 U+11A8）
    if {0x1100, 0x1188, 0x11A8} <= set(cmap):
        lvt = "\u1100\u1188\u11A8"
        for lang in ("ko", None, "en"):
            shaped = _shape(hb_font, lvt, script="hang", language=lang)
            total = sum(x[1] for x in shaped)
            if total != 1000:
                res.fail(f"古韩文 L+V+T 在 lang={lang!r} 下应合为一格（步进 1000），实际 {total}：{shaped}")
        for lang in ("zh-Hans", "ja"):
            shaped = _shape(hb_font, lvt, script="hang", language=lang)
            total = sum(x[1] for x in shaped)
            if total != 3000:
                res.fail(f"古韩文 L+V+T 在 lang={lang!r} 下应各占一格（步进 3000），实际 {total}：{shaped}")

    # 5) CL 专属检查：全汉字 1000 宽 & 间距基字非 mark
    if region == "CL":
        hmtx = font["hmtx"].metrics
        bad_fw = [
            hex(cp)
            for cp, gn in cmap.items()
            if is_ideograph(cp) and hmtx.get(gn, (0, 0))[0] != 1000
        ]
        if bad_fw:
            res.fail(f"CL 存在 {len(bad_fw)} 个非 1000 宽汉字，样例：{bad_fw[:6]}")
        for cp in sorted(spec.classical_spacing_bases):
            if cp in cmap:
                adv = _shape(hb_font, chr(cp), script="hani")[0][1]
                if adv != 1000:
                    res.fail(f"CL 间距基字 U+{cp:04X} 塑形步进 {adv} != 1000")


def _check_names_os2_and_raster(
    font: TTFont,
    res: CheckResult,
    spec: Spec,
    *,
    region: str,
    italic: bool,
    is_vf: bool,
    hinted: bool,
) -> None:
    name = font["name"]
    r = spec.region(region)
    if any(rec.platformID == 1 for rec in name.names):
        res.fail("name 表包含残留 Mac (platformID=1) 记录")
    cpr = name.getDebugName(0) or ""
    if spec.copyright not in cpr:
        res.fail("nameID 0 缺少项目版权说明")
    if region == "CL" and spec.shanggu_copyright not in cpr:
        res.fail("CL nameID 0 缺少尚古版权说明")
    if region != "CL" and spec.shanggu_copyright in cpr:
        res.fail(f"非 CL 地区 {region} 不应包含尚古版权")
    if (name.getDebugName(13) or "") != spec.license_description:
        res.fail("nameID 13 许可证正文不匹配")
    if (name.getDebugName(14) or "") != spec.license_url:
        res.fail("nameID 14 许可证链接不匹配")
    ver = name.getDebugName(5) or ""
    if f"Version {spec.opentype_version()}" not in ver:
        res.fail(f"nameID 5 版本前缀异常：{ver!r}")
    if region == "CL" and "Shanggu" not in ver:
        res.fail(f"CL nameID 5 未注明尚古来源：{ver!r}")

    def _get_name(nid: int, lang_id: int) -> str | None:
        rec = name.getName(nid, 3, 1, lang_id)
        return rec.toUnicode() if rec is not None else None

    # 校验英文与本地区命名记录（nameID 1, 2, 4, 6, 16, 17）
    if is_vf:
        exp_fam = spec.vf_family(r.code)
        exp_fam_loc = spec.vf_family_local(r.code)
        exp_sub = "Italic" if italic else "Regular"
        exp_full = f"{exp_fam} Italic" if italic else exp_fam
        exp_full_loc = f"{exp_fam_loc} Italic" if italic else exp_fam_loc
        exp_ps = f"{spec.vf_ps_family(r.code)}-Italic" if italic else spec.vf_ps_family(r.code)
        exp_en = {1: exp_fam, 2: exp_sub, 4: exp_full, 6: exp_ps, 16: exp_fam, 17: exp_sub}
        exp_loc = {1: exp_fam_loc, 2: exp_sub, 4: exp_full_loc, 16: exp_fam_loc, 17: exp_sub}
    else:
        w_val = int(font["OS/2"].usWeightClass)
        w_spec = next((w for w in spec.weights if w.value == w_val), None)
        if w_spec is None:
            res.fail(f"静态字体 OS/2.usWeightClass={w_val} 不在规格字重表中")
            exp_en = {}
            exp_loc = {}
        else:
            typo_fam = spec.static_family(r.code)
            typo_fam_loc = spec.static_family_local(r.code)
            leg_fam = typo_fam if w_spec.name in {"Regular", "Bold"} else f"{typo_fam} {w_spec.name}"
            leg_fam_loc = typo_fam_loc if w_spec.name in {"Regular", "Bold"} else f"{typo_fam_loc} {w_spec.name}"
            leg_sub = ("Bold Italic" if italic else "Bold") if w_spec.name == "Bold" else ("Italic" if italic else "Regular")
            typo_sub = "Italic" if (w_spec.name == "Regular" and italic) else f"{w_spec.name}{' Italic' if italic else ''}"
            full = typo_fam if (w_spec.name == "Regular" and not italic) else f"{typo_fam} {typo_sub}"
            full_loc = typo_fam_loc if (w_spec.name == "Regular" and not italic) else f"{typo_fam_loc} {typo_sub}"
            ps_suf = "Italic" if (w_spec.name == "Regular" and italic) else f"{w_spec.name}{'-Italic' if italic else ''}"
            ps = f"{spec.static_ps_family(r.code)}-{ps_suf}"
            exp_en = {1: leg_fam, 2: leg_sub, 4: full, 6: ps, 16: typo_fam, 17: typo_sub}
            exp_loc = {1: leg_fam_loc, 2: leg_sub, 4: full_loc, 16: typo_fam_loc, 17: typo_sub}
    for nid, expected in exp_en.items():
        actual = _get_name(nid, 0x0409)
        if actual != expected:
            res.fail(f"英文 nameID {nid} 不匹配：实际 {actual!r}，期望 {expected!r}")
    for nid, expected in exp_loc.items():
        actual = _get_name(nid, r.local_lang_id)
        if actual != expected:
            res.fail(f"本地化(0x{r.local_lang_id:04X}) nameID {nid} 不匹配：实际 {actual!r}，期望 {expected!r}")

    # OS/2 bit 57（Non-Plane 0）、ulCodePageRange1 与 usMaxContext
    cmap = font.getBestCmap() or {}
    os2 = font["OS/2"]
    if any(cp > 0xFFFF for cp in cmap):
        if not (os2.ulUnicodeRange2 & (1 << 25)):
            res.fail("含非 BMP 码位但 OS/2 ulUnicodeRange bit 57 未置位")
    if int(os2.ulCodePageRange1) != r.codepage_range1:
        res.fail(f"OS/2.ulCodePageRange1 实际 0x{os2.ulCodePageRange1:08X} != 期望 0x{r.codepage_range1:08X}")
    expected_max_ctx = int(maxCtxFont(font))
    if int(os2.usMaxContext) != expected_max_ctx:
        res.fail(f"OS/2.usMaxContext 实际 {os2.usMaxContext} != 重算值 {expected_max_ctx}")

    # head.flags、lowestRecPPEM、全局包围盒上下界、vhea.advanceHeightMax 及全字形 lsb == xMin
    head = font["head"]
    expected_flags = 15 if (not is_vf and hinted) else 3
    if int(head.flags) != expected_flags:
        res.fail(f"head.flags 实际 {head.flags} != 期望 {expected_flags}（hinted={hinted}, is_vf={is_vf}）")
    if int(head.lowestRecPPEM) != spec.lowest_rec_ppem:
        res.fail(f"head.lowestRecPPEM 实际 {head.lowestRecPPEM} != 期望 {spec.lowest_rec_ppem}")
    if int(head.yMax) > 1350 or int(head.yMin) < -560:
        res.fail(f"head 包围盒上下界超出预期范围（yMin={head.yMin}, yMax={head.yMax}，期望在 [-560, 1350] 内）")
    if "vhea" in font and int(font["vhea"].advanceHeightMax) != 2000:
        res.fail(f"vhea.advanceHeightMax 实际 {font['vhea'].advanceHeightMax} != 期望 2000")
    w_for_xavg = spec.weight("Regular") if is_vf else next((w for w in spec.weights if w.value == int(os2.usWeightClass)), None)
    if w_for_xavg is not None:
        exp_xavg = w_for_xavg.x_avg_char_width_italic if italic else w_for_xavg.x_avg_char_width_upright
        if int(os2.xAvgCharWidth) != exp_xavg:
            res.fail(f"OS/2.xAvgCharWidth 实际 {os2.xAvgCharWidth} != 期望 {exp_xavg}")
    glyf = font["glyf"]
    hmtx = font["hmtx"].metrics
    lsb_bad: list[str] = []
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        if (g.isComposite() or getattr(g, "numberOfContours", 0) > 0) and hasattr(g, "xMin"):
            lsb = hmtx[gname][1]
            if lsb != g.xMin:
                lsb_bad.append(f"{gname}({lsb}!={g.xMin})")
                if len(lsb_bad) >= 6:
                    break
    if lsb_bad:
        res.fail(f"存在非空字形 lsb != xMin，样例：{lsb_bad}")

    # 光栅元数据与 VF/静态表结构
    if is_vf:
        for req in ("fvar", "avar", "gvar", "HVAR", "MVAR", "STAT"):
            if req not in font:
                res.fail(f"VF 缺少必备表 {req}")
        for banned in ("VORG", "VVAR", "fpgm", "cvt "):
            if banned in font:
                res.fail(f"VF 不应包含表 {banned}")
    else:
        for banned in ("fvar", "avar", "gvar", "HVAR", "VVAR", "MVAR", "VORG"):
            if banned in font:
                res.fail(f"静态字体不应包含表 {banned}")
        if float(font["post"].formatType) != 3.0:
            res.fail(f"静态字体 post.formatType={font['post'].formatType} != 3.0")

    if is_vf or not hinted:
        if "fpgm" in font or "cvt " in font:
            res.fail("VF/unhinted 不应带 fpgm 或 cvt 表")
        if "prep" not in font or bytes(font["prep"].program.getBytecode()) != spec.smart_dropout_prep:
            res.fail("VF/unhinted 缺少 smart dropout prep")
        if "gasp" not in font or font["gasp"].gaspRange != {0xFFFF: spec.gasp_range_all}:
            res.fail("VF/unhinted gasp 表不匹配")
        inst_cnt = sum(
            1
            for g in font["glyf"].glyphs.values()
            if hasattr(g, "program") and g.program and g.program.getBytecode()
        )
        if inst_cnt:
            res.fail(f"VF/unhinted 存在 {inst_cnt} 个带字形指令的字形")


def _check_empty_gsub_features(font: TTFont, res: CheckResult, spec: Spec, *, italic: bool) -> None:
    """确认按上游 Sarasa Ui 保留的空 GSUB 特性记录（ss01–ss08、cv01–cv13，斜体不含 cv11）仍在位。"""
    if "GSUB" not in font or not font["GSUB"].table.ScriptList:
        res.fail("缺少 GSUB 表，无法校验上游空特性记录")
        return
    expected = set(spec.features["italic_empty_gsub" if italic else "upright_empty_gsub"])
    r = routes(font["GSUB"].table)
    for key in (("DFLT", "dflt"), ("latn", "dflt")):
        feats = r.get(key)
        if feats is None:
            continue
        missing = sorted(expected - set(feats))
        if missing:
            res.fail(f"GSUB {key[0]}/{key[1]} 缺少上游保留空特性 {len(missing)} 个：{missing[:6]}")


def _eval_value_xadv(vrec: Any, inst: VarStoreInstancer | None) -> float:
    if vrec is None:
        return 0.0
    x = float(getattr(vrec, "XAdvance", 0) or 0)
    dev = getattr(vrec, "XAdvDevice", None)
    if dev is not None and inst is not None and getattr(dev, "DeltaFormat", None) == 0x8000:
        x += float(inst[(dev.StartSize << 16) | dev.EndSize])
    return x


def _make_var_instancer(font: TTFont, weight: float, opsz: float | None = None) -> VarStoreInstancer | None:
    if "GDEF" not in font or getattr(font["GDEF"].table, "VarStore", None) is None or "fvar" not in font:
        return None
    loc: dict[str, float] = {}
    avar_segs = font["avar"].segments if "avar" in font else {}
    for axis in font["fvar"].axes:
        if axis.axisTag == "wght":
            n = normalizeValue(weight, (axis.minValue, axis.defaultValue, axis.maxValue))
            seg = avar_segs.get("wght")
            loc["wght"] = piecewiseLinearMap(n, seg) if seg else n
        elif axis.axisTag == "opsz" and opsz is not None:
            n = normalizeValue(opsz, (axis.minValue, axis.defaultValue, axis.maxValue))
            seg = avar_segs.get("opsz")
            loc["opsz"] = piecewiseLinearMap(n, seg) if seg else n
        else:
            loc[axis.axisTag] = 0.0
    return VarStoreInstancer(font["GDEF"].table.VarStore, font["fvar"].axes, loc)


def _extract_kern_tables(
    font: TTFont,
    *,
    weight: float | None = None,
    opsz: float | None = None,
    repair_ss03_rho: bool = False,
) -> list[list[tuple[Any, ...]]]:
    if "GPOS" not in font or not font["GPOS"].table.FeatureList or not font["GPOS"].table.LookupList:
        return []
    gpos = font["GPOS"].table
    kern_lks = sorted({
        int(idx)
        for rec in gpos.FeatureList.FeatureRecord
        if rec.FeatureTag == "kern"
        for idx in (rec.Feature.LookupListIndex or [])
    })
    inst = _make_var_instancer(font, weight, opsz) if weight is not None else None
    lookups: list[list[tuple[Any, ...]]] = []
    for li in kern_lks:
        lk = gpos.LookupList.Lookup[li]
        subs: list[tuple[Any, ...]] = []
        ref_quoteright_rho: float | None = None
        for raw in lk.SubTable:
            p = getattr(raw, "ExtSubTable", raw)
            if not isinstance(p, ot.PairPos):
                continue
            if p.Format == 1:
                m: dict[str, dict[str, float]] = {}
                for g1, ps in zip(p.Coverage.glyphs, p.PairSet):
                    row = {r.SecondGlyph: _eval_value_xadv(r.Value1, inst) for r in ps.PairValueRecord}
                    m[g1] = row
                if repair_ss03_rho and ref_quoteright_rho is None and "quoteright" in m and "rho" in m["quoteright"]:
                    ref_quoteright_rho = m["quoteright"]["rho"]
                subs.append((1, m))
            elif p.Format == 2:
                cov = set(p.Coverage.glyphs)
                c1d = dict(p.ClassDef1.classDefs)
                c2d = dict(p.ClassDef2.classDefs)
                mat = [[_eval_value_xadv(r2.Value1, inst) for r2 in r1.Class2Record] for r1 in p.Class1Record]
                subs.append((2, cov, c1d, c2d, mat))
        if repair_ss03_rho and ref_quoteright_rho is not None:
            patched = False
            for sub in subs:
                if sub[0] == 1 and "quoteright.ss03" in sub[1] and "rho" in sub[1]["quoteright.ss03"]:
                    sub[1]["quoteright.ss03"]["rho"] = ref_quoteright_rho
                    patched = True
                    break
            if not patched:
                for sub in subs:
                    if sub[0] == 1:
                        sub[1].setdefault("quoteright.ss03", {})["rho"] = ref_quoteright_rho
                        break
        lookups.append(subs)
    return lookups


def _eval_kern(lookups: list[list[tuple[Any, ...]]], g1: str, g2: str) -> float:
    total = 0.0
    for subs in lookups:
        for sub in subs:
            if sub[0] == 1:
                row = sub[1].get(g1)
                if row is not None and g2 in row:
                    total += row[g2]
                    break
            else:
                _fmt, cov, c1d, c2d, mat = sub
                if g1 in cov:
                    total += mat[c1d.get(g1, 0)][c2d.get(g2, 0)]
                    break
    return total


_INTER_REF_CACHE: dict[tuple[str, float], dict[str, Any]] = {}


def _load_inter_ref(inter_vf_path: Path, spec: Spec) -> dict[str, Any]:
    key = (str(inter_vf_path.resolve()), float(spec.inter_opsz))
    cached = _INTER_REF_CACHE.get(key)
    if cached is not None:
        return cached
    with TTFont(inter_vf_path) as inter:
        scale = 1000.0 / float(inter["head"].unitsPerEm)
        cm = dict(inter.getBestCmap() or {})
        tnum = _single_sub_mapping(inter, "tnum")
        ss03 = _single_sub_mapping(inter, "ss03")
        cv10 = _single_sub_mapping(inter, "cv10")
        pnum = _single_sub_mapping(inter, "pnum")
        eff_cmap: dict[int, str] = {}
        for cp, g in cm.items():
            if cp != 0x2D and g in tnum:
                g = tnum[g]
            g = ss03.get(g, g)
            g = cv10.get(g, g)
            if cp in spec.propdigits_codepoints and g in pnum:
                g = pnum[g]
            eff_cmap[cp] = g

        hvar = inter["HVAR"].table if "HVAR" in inter else None
        axes = inter["fvar"].axes
        avar_segs = inter["avar"].segments if "avar" in inter else {}
        hmtx = {g: float(m[0]) for g, m in inter["hmtx"].metrics.items()}
        glyph_ids = {g: i for i, g in enumerate(inter.getGlyphOrder())}

        kern_by_w: dict[int, list[list[tuple[Any, ...]]]] = {}
        adv_by_w: dict[int, dict[str, float]] = {}
        for wspec in spec.weights:
            wv = wspec.value
            kern_by_w[wv] = _extract_kern_tables(inter, weight=float(wv), opsz=spec.inter_opsz, repair_ss03_rho=True)
            adv_map: dict[str, float] = {}
            if hvar is not None:
                loc: dict[str, float] = {}
                for ax in axes:
                    if ax.axisTag == "wght":
                        n = normalizeValue(float(wv), (ax.minValue, ax.defaultValue, ax.maxValue))
                        seg = avar_segs.get("wght")
                        loc["wght"] = piecewiseLinearMap(n, seg) if seg else n
                    elif ax.axisTag == "opsz":
                        n = normalizeValue(spec.inter_opsz, (ax.minValue, ax.defaultValue, ax.maxValue))
                        seg = avar_segs.get("opsz")
                        loc["opsz"] = piecewiseLinearMap(n, seg) if seg else n
                    else:
                        loc[ax.axisTag] = 0.0
                inst = VarStoreInstancer(hvar.VarStore, axes, loc)
                for g in set(eff_cmap.values()):
                    idx = hvar.AdvWidthMap.mapping[g] if hvar.AdvWidthMap is not None else glyph_ids[g]
                    adv_map[g] = (hmtx[g] + float(inst[idx])) * scale
            else:
                for g in set(eff_cmap.values()):
                    adv_map[g] = hmtx[g] * scale
            adv_by_w[wv] = adv_map

    data = {"scale": scale, "eff_cmap": eff_cmap, "kern_by_w": kern_by_w, "adv_by_w": adv_by_w}
    _INTER_REF_CACHE[key] = data
    return data


def _check_inter_kerning_and_advances(
    font: TTFont,
    hb_font: hb.Font,
    res: CheckResult,
    spec: Spec,
    *,
    is_vf: bool,
    inter_vf_path: Path | None = None,
    require_inter_ref: bool = True,
) -> None:
    """检查 GPOS PairSet 排序、ʼρ=-62 专项回归，以及全量 Inter 字形对字偶距与单次取整步进一致性。"""
    cmap = font.getBestCmap() or {}
    weights = [w.value for w in spec.weights] if is_vf else [int(font["OS/2"].usWeightClass)]

    # 1) 所有 GPOS Format 1 PairSet 必须按第二字形 GID 严格升序（防 HarfBuzz 二分漏读）
    if "GPOS" in font and font["GPOS"].table.LookupList:
        unsorted_sets = 0
        for lk in font["GPOS"].table.LookupList.Lookup:
            for raw in lk.SubTable:
                st = getattr(raw, "ExtSubTable", raw)
                if not isinstance(st, ot.PairPos) or st.Format != 1:
                    continue
                for ps in st.PairSet:
                    gids = [font.getGlyphID(r.SecondGlyph) for r in ps.PairValueRecord]
                    if any(a >= b for a, b in zip(gids, gids[1:])):
                        unsorted_sets += 1
        if unsorted_sets:
            res.fail(f"GPOS 存在 {unsorted_sets} 个未按 GID 严格升序排列的 Format 1 PairSet")

    # 2) ʼρ (U+02BC U+03C1) 专项回归：字偶距必须为 -62（容差 1 unit），严禁退化为 -21
    if 0x02BC in cmap and 0x03C1 in cmap:
        for w in weights:
            if is_vf:
                hb_font.set_variations({"wght": float(w)})
            base_adv = _shape(hb_font, "\u02bc")[0][1]
            for sc in (None, "grek"):
                paired_adv = _shape(hb_font, "\u02bc\u03c1", script=sc)[0][1]
                kern = paired_adv - base_adv
                if w == 400 or not is_vf:
                    res.metrics["mod_apostrophe_rho_kern"] = kern
                if abs(kern - (-62)) > 1:
                    res.fail(f"wght={w} script={sc or 'auto'} ʼρ 字偶距退化为 {kern}（期望 -62 ±1）")
                    break
        if is_vf:
            hb_font.set_variations({"wght": 400.0})

    # 3) 推广检查：全部 Inter-Inter 字形对字偶距与 Inter 一致（容差 1 unit），且命名字重步进为单次取整
    if inter_vf_path is None or not inter_vf_path.is_file():
        if require_inter_ref:
            res.fail(f"缺少 Inter VF 参照文件，无法核对全量字偶距与单次取整步进：{inter_vf_path}")
        return
    ref = _load_inter_ref(inter_vf_path, spec)
    eff_cmap: dict[int, str] = ref["eff_cmap"]
    scale: float = ref["scale"]
    forced = spec.source_han_forced_codepoints

    inter_to_prod: dict[str, str] = {}
    prod_to_inter: dict[str, str] = {}
    inter_cps: list[int] = []
    for cp in sorted(cmap):
        if cp in eff_cmap and use_inter_codepoint(cp, forced):
            ig, pg = eff_cmap[cp], cmap[cp]
            inter_to_prod.setdefault(ig, pg)
            prod_to_inter.setdefault(pg, ig)
            inter_cps.append(cp)

    kern_failed = False
    adv_failed = False
    for w in weights:
        if w not in ref["kern_by_w"]:
            continue
        if not kern_failed:
            inter_lks = ref["kern_by_w"][w]
            prod_lks = _extract_kern_tables(font, weight=float(w) if is_vf else None)

            # 收集待测对集合：双方所有 Format 1 对 + 双方所有 Format 2 (class1, class2) 代表对
            candidate_pairs: set[tuple[str, str]] = set()
            for subs in inter_lks:
                for sub in subs:
                    if sub[0] == 1:
                        for ig1, row in sub[1].items():
                            if ig1 in inter_to_prod:
                                for ig2 in row:
                                    if ig2 in inter_to_prod:
                                        candidate_pairs.add((ig1, ig2))
                    else:
                        _fmt, cov, c1d, c2d, _mat = sub
                        c1_rep: dict[int, str] = {}
                        c2_rep: dict[int, str] = {}
                        for ig in inter_to_prod:
                            if ig in cov:
                                c1_rep.setdefault(c1d.get(ig, 0), ig)
                            c2_rep.setdefault(c2d.get(ig, 0), ig)
                        for ig1 in c1_rep.values():
                            for ig2 in c2_rep.values():
                                candidate_pairs.add((ig1, ig2))
            for subs in prod_lks:
                for sub in subs:
                    if sub[0] == 1:
                        for pg1, row in sub[1].items():
                            if pg1 in prod_to_inter:
                                ig1 = prod_to_inter[pg1]
                                for pg2 in row:
                                    if pg2 in prod_to_inter:
                                        candidate_pairs.add((ig1, prod_to_inter[pg2]))
                    else:
                        _fmt, cov, c1d, c2d, _mat = sub
                        c1_rep = {}
                        c2_rep = {}
                        for pg, ig in prod_to_inter.items():
                            if pg in cov:
                                c1_rep.setdefault(c1d.get(pg, 0), ig)
                            c2_rep.setdefault(c2d.get(pg, 0), ig)
                        for ig1 in c1_rep.values():
                            for ig2 in c2_rep.values():
                                candidate_pairs.add((ig1, ig2))

            bad_pairs: list[tuple[str, str, float, float]] = []
            for ig1, ig2 in sorted(candidate_pairs):
                pg1, pg2 = inter_to_prod[ig1], inter_to_prod[ig2]
                exp_k = _eval_kern(inter_lks, ig1, ig2) * scale
                act_k = _eval_kern(prod_lks, pg1, pg2)
                if abs(act_k - exp_k) > 1.01:
                    bad_pairs.append((ig1, ig2, round(exp_k, 2), round(act_k, 2)))
            if bad_pairs:
                res.fail(
                    f"wght={w}: {len(bad_pairs)} 对 Inter 字形字偶距偏离 Inter 超过 1 unit，样例：{bad_pairs[:5]}"
                )
                kern_failed = True

        # 单次取整步进校验：每个 Inter 编码字形在命名字重下与未缩放精确步进偏差不得超过 1.0 unit
        if not adv_failed:
            adv_map = ref["adv_by_w"][w]
            if is_vf:
                hb_font.set_variations({"wght": float(w)})
            bad_advs: list[tuple[str, str, int, float]] = []
            for cp in inter_cps:
                ig, pg = eff_cmap[cp], cmap[cp]
                exact_adv = adv_map[ig]
                prod_adv = (
                    hb_font.get_glyph_h_advance(font.getGlyphID(pg))
                    if is_vf
                    else font["hmtx"].metrics[pg][0]
                )
                if abs(prod_adv - exact_adv) > 1.0:
                    bad_advs.append((hex(cp), ig, prod_adv, round(exact_adv, 3)))
            if bad_advs:
                res.fail(
                    f"wght={w}: {len(bad_advs)} 个 Inter 字形步进出现二次取整偏差（>1.0 unit），样例：{bad_advs[:5]}"
                )
                adv_failed = True
        if kern_failed and adv_failed:
            break
    if is_vf:
        hb_font.set_variations({"wght": 400.0})


def extract_table_framework(font: TTFont) -> dict[str, Any]:
    """提取用于表级框架对照的完整字段快照（head、hhea、vhea、OS/2 全部字段、post 与标准 name 记录键）。"""
    head = font["head"]
    hhea = font["hhea"]
    os2 = font["OS/2"]
    post = font["post"]

    os2_dict: dict[str, Any] = {}
    for f in OS2_SCALAR_FIELDS:
        if hasattr(os2, f):
            val = getattr(os2, f)
            os2_dict[f] = str(val) if f == "achVendID" else int(val)
    if hasattr(os2, "panose"):
        for pf in PANOSE_FIELDS:
            os2_dict[f"panose.{pf}"] = int(getattr(os2.panose, pf))

    snap: dict[str, Any] = {
        "head": {
            "unitsPerEm": int(head.unitsPerEm),
            "flags": int(head.flags),
            "lowestRecPPEM": int(head.lowestRecPPEM),
            "macStyle": int(head.macStyle),
            "xMin": int(head.xMin),
            "yMin": int(head.yMin),
            "xMax": int(head.xMax),
            "yMax": int(head.yMax),
        },
        "hhea": {
            "ascent": int(hhea.ascent),
            "descent": int(hhea.descent),
            "lineGap": int(hhea.lineGap),
            "advanceWidthMax": int(hhea.advanceWidthMax),
            "minLeftSideBearing": int(hhea.minLeftSideBearing),
            "minRightSideBearing": int(hhea.minRightSideBearing),
            "xMaxExtent": int(hhea.xMaxExtent),
            "caretSlopeRise": int(hhea.caretSlopeRise),
            "caretSlopeRun": int(hhea.caretSlopeRun),
            "caretOffset": int(hhea.caretOffset),
        },
        "OS/2": os2_dict,
        "post": {
            "formatType": float(post.formatType),
            "italicAngle": round(float(post.italicAngle), 4),
            "underlinePosition": int(post.underlinePosition),
            "underlineThickness": int(post.underlineThickness),
            "isFixedPitch": int(post.isFixedPitch),
        },
        "name_keys": sorted(
            (int(r.platformID), int(r.platEncID), int(r.langID), int(r.nameID))
            for r in font["name"].names
            if int(r.nameID) < 256
        ),
        "has_STAT": "STAT" in font,
    }
    if "vhea" in font:
        vhea = font["vhea"]
        snap["vhea"] = {
            "tableVersion": int(vhea.tableVersion),
            "ascent": int(vhea.ascent),
            "descent": int(vhea.descent),
            "lineGap": int(vhea.lineGap),
            "advanceHeightMax": int(vhea.advanceHeightMax),
            "minTopSideBearing": int(vhea.minTopSideBearing),
            "minBottomSideBearing": int(vhea.minBottomSideBearing),
            "yMaxExtent": int(vhea.yMaxExtent),
            "caretSlopeRise": int(vhea.caretSlopeRise),
            "caretSlopeRun": int(vhea.caretSlopeRun),
            "caretOffset": int(vhea.caretOffset),
        }
    return snap


def _check_table_framework_parity(
    font: TTFont,
    reference: TTFont,
    res: CheckResult,
    spec: Spec,
    *,
    region: str,
    italic: bool,
    is_vf: bool,
    hinted: bool,
    weight_spec: WeightSpec,
    subset_mode: str = "full",
    mode: str = "upstream",
) -> None:
    """表级框架逐字段对照：任何未登记在 spec.toml 差异清单里的字段偏差必须失败。

    - mode="upstream"：对照同地区、同字重、同风格上游 Sarasa Ui（Heavy 对照 Bold 并按规格换算）；
    - mode="vf_vs_regular"：对照 VF 默认实例与 Regular unhinted 静态。
    """
    cur = extract_table_framework(font)
    ref = extract_table_framework(reference)
    diff_ids = {d.id for d in spec.differences}
    is_heavy = (mode == "upstream") and (weight_spec.name == "Heavy")
    has_non_bmp = any(cp > 0xFFFF for cp in (font.getBestCmap() or {}))

    def _require_diff_id(diff_id: str, label: str) -> bool:
        if diff_id not in diff_ids:
            res.fail(f"表级差异 {label} 未在 spec.toml 差异清单中声明（缺少 {diff_id!r}）")
            return False
        return True

    smoke_skip_fields = {
        ("head", "xMin"),
        ("head", "yMin"),
        ("head", "xMax"),
        ("head", "yMax"),
        ("hhea", "advanceWidthMax"),
        ("hhea", "minLeftSideBearing"),
        ("hhea", "minRightSideBearing"),
        ("hhea", "xMaxExtent"),
        ("vhea", "minTopSideBearing"),
        ("vhea", "minBottomSideBearing"),
        ("vhea", "yMaxExtent"),
        ("OS/2", "usFirstCharIndex"),
        ("OS/2", "usLastCharIndex"),
    }
    geom_tol_fields = {
        ("head", "xMin"),
        ("head", "xMax"),
        ("hhea", "minLeftSideBearing"),
        ("hhea", "minRightSideBearing"),
        ("hhea", "xMaxExtent"),
        ("vhea", "minTopSideBearing"),
        ("vhea", "minBottomSideBearing"),
    }
    tight_bbox_fields = {("head", "yMin"), ("head", "yMax"), ("vhea", "yMaxExtent")}

    for sec in ("head", "hhea", "vhea", "OS/2", "post"):
        if (sec in cur) != (sec in ref):
            res.fail(f"表级框架对照({mode})：表 {sec} 存在性不一致（cand={sec in cur}, ref={sec in ref}）")
            continue
        if sec not in cur:
            continue
        for fld in sorted(set(cur[sec]) | set(ref[sec])):
            key = (sec, fld)
            if subset_mode == "smoke" and key in smoke_skip_fields:
                continue
            a = cur[sec].get(fld)
            b = ref[sec].get(fld)
            tag = f"{sec}.{fld}"

            if mode == "vf_vs_regular":
                if a != b:
                    # 静态实例化后严格去重叠（cu2qu）允许个别极值控制框产生 ≤10 unit 微差，其余表级字段须逐值完全相等
                    if key in (geom_tol_fields | tight_bbox_fields) and abs(int(a) - int(b)) <= 10:
                        continue
                    res.fail(f"VF vs Regular 静态表级不一致：{tag} VF={a!r} != Regular={b!r}")
                continue

            # mode == "upstream"
            if key == ("head", "flags"):
                if not is_vf and hinted:
                    if a != b:
                        res.fail(f"表级框架对照 {tag}：hinted 实际 {a} != 上游 {b}")
                else:
                    _require_diff_id("nonhinting-head-flags", tag)
                    if a != 3:
                        res.fail(f"表级框架对照 {tag}：非 hinted/VF 实际 {a} != 期望 3（上游 {b}）")
                continue

            if key in tight_bbox_fields:
                if a != b:
                    _require_diff_id("heavy-weight" if is_heavy else "vf-single-source-metrics", tag)
                    if abs(int(a) - int(b)) > 10:
                        res.fail(f"表级框架对照 {tag} 偏离上游超过 10 unit：实际 {a} vs 上游 {b}")
                continue

            if key in geom_tol_fields:
                if a != b:
                    _require_diff_id("heavy-weight" if is_heavy else "vf-single-source-metrics", tag)
                    limit = 200 if is_heavy else 40
                    if abs(int(a) - int(b)) > limit:
                        res.fail(f"表级框架对照 {tag} 偏离上游过大：实际 {a} vs 上游 {b}（允许 ±{limit}）")
                continue

            if key == ("hhea", "caretSlopeRise"):
                if a != b:
                    if (
                        cur["hhea"].get("caretSlopeRun") == 0
                        and ref["hhea"].get("caretSlopeRun") == 0
                        and {a, b} == {1, 1000}
                    ):
                        continue
                    res.fail(f"表级框架对照 {tag} 不一致：实际 {a} != 上游 {b}")
                continue

            if key == ("OS/2", "achVendID"):
                _require_diff_id("os2-metadata-fixes", tag)
                if a != spec.vendor_id:
                    res.fail(f"表级框架对照 {tag}：实际 {a!r} != 规格 {spec.vendor_id!r}")
                continue

            if key == ("OS/2", "usMaxContext"):
                _require_diff_id("os2-metadata-fixes", tag)
                exp_ctx = int(maxCtxFont(font))
                if a != exp_ctx:
                    res.fail(f"表级框架对照 {tag}：实际 {a} != 重算值 {exp_ctx}")
                continue

            if key == ("OS/2", "ulUnicodeRange2"):
                exp_ur2 = (int(b) | (1 << 25)) if has_non_bmp else int(b)
                if exp_ur2 != int(b):
                    _require_diff_id("os2-metadata-fixes", tag)
                if int(a) != exp_ur2:
                    res.fail(f"表级框架对照 {tag}：实际 0x{int(a):08X} != 期望 0x{exp_ur2:08X}")
                continue

            if key == ("OS/2", "xAvgCharWidth"):
                exp_xavg = weight_spec.x_avg_char_width_italic if italic else weight_spec.x_avg_char_width_upright
                if exp_xavg != b:
                    _require_diff_id("heavy-weight", tag)
                if a != exp_xavg:
                    res.fail(f"表级框架对照 {tag}：实际 {a} != 期望 {exp_xavg}（上游 {b}）")
                continue

            if is_heavy and key in {
                ("head", "macStyle"),
                ("OS/2", "usWeightClass"),
                ("OS/2", "fsSelection"),
                ("OS/2", "panose.bWeight"),
                ("OS/2", "yStrikeoutSize"),
                ("post", "underlineThickness"),
                ("post", "underlinePosition"),
            }:
                _require_diff_id("heavy-weight", tag)
                heavy_expected = {
                    ("head", "macStyle"): 0b10 if italic else 0,
                    ("OS/2", "usWeightClass"): 900,
                    ("OS/2", "fsSelection"): (1 << 7) | ((1 << 0) if italic else (1 << 6)),
                    ("OS/2", "panose.bWeight"): 10,
                    ("OS/2", "yStrikeoutSize"): weight_spec.strikeout_size,
                    ("post", "underlineThickness"): weight_spec.underline_thickness,
                    ("post", "underlinePosition"): weight_spec.underline_position,
                }[key]
                if a != heavy_expected:
                    res.fail(f"表级框架对照(Heavy) {tag}：实际 {a!r} != 规格 {heavy_expected!r}")
                continue

            if a != b:
                if fld.startswith("ulUnicodeRange") or fld.startswith("ulCodePageRange"):
                    res.fail(f"表级框架对照 {tag} 不一致：实际 0x{int(a):08X} != 上游 0x{int(b):08X}")
                else:
                    res.fail(f"表级框架对照 {tag} 不一致：实际 {a!r} != 上游 {b!r}")

    # name 记录键（nameID < 256）
    cur_keys = set(cur["name_keys"])
    ref_keys = set(ref["name_keys"])
    if mode == "vf_vs_regular":
        _require_diff_id("os2-metadata-fixes", "name_keys(nameID 25)")
        exp_keys = ref_keys | {(3, 1, 0x0409, 25)}
    else:
        _require_diff_id("os2-metadata-fixes", "name_keys")
        local_lang = spec.region(region).local_lang_id
        exp_keys = (ref_keys - {(3, 1, local_lang, 3)}) | {(3, 1, 0x0409, 13), (3, 1, 0x0409, 14)}
        for nid in (1, 2, 4, 16, 17):
            exp_keys.add((3, 1, local_lang, nid))
        if is_vf:
            exp_keys.add((3, 1, 0x0409, 25))
    if cur_keys != exp_keys:
        extra_k = sorted(cur_keys - exp_keys)
        miss_k = sorted(exp_keys - cur_keys)
        res.fail(f"表级框架对照({mode}) name 记录键不匹配：多出 {extra_k}，缺少 {miss_k}")

    # STAT 表存在性
    if not cur["has_STAT"]:
        res.fail(f"表级框架对照({mode})：缺少 STAT 表")
    elif not ref["has_STAT"]:
        _require_diff_id("os2-metadata-fixes", "STAT")


def check_font_rules(
    font_path: Path,
    *,
    region: str,
    italic: bool,
    is_vf: bool,
    hinted: bool = False,
    reference_path: Path | None = None,
    inter_vf_path: Path | None = None,
    subset_mode: str = "full",
    require_references: bool = True,
    spec: Spec | None = None,
) -> CheckResult:
    """对单个 VF 或静态 TTF 执行全部产品规则检查。"""
    spec = spec or load_spec()
    res = CheckResult(ok=True)
    blob = font_path.read_bytes()
    hb_font = hb.Font(hb.Face(blob))
    if is_vf:
        hb_font.set_variations({"wght": 400.0})

    with TTFont(font_path, recalcTimestamp=False) as font:
        ref = TTFont(reference_path, lazy=True) if reference_path is not None and reference_path.is_file() else None
        try:
            _check_cmap_and_uvs(
                font,
                res,
                reference=ref,
                subset_mode=subset_mode,
                require_reference=require_references,
                strict_uvs_kind=not spec.region(region).classical,
            )
            _check_propdigits_and_tnum(font, hb_font, res, spec, is_vf)
            _check_inter_calt_and_guards(font, hb_font, res)
            _check_chws_and_punctuation(font, hb_font, res, region, spec, italic=italic, is_vf=is_vf)
            _check_empty_gsub_features(font, res, spec, italic=italic)
            _check_inter_kerning_and_advances(
                font,
                hb_font,
                res,
                spec,
                is_vf=is_vf,
                inter_vf_path=inter_vf_path,
                require_inter_ref=require_references,
            )
            _check_names_os2_and_raster(font, res, spec, region=region, italic=italic, is_vf=is_vf, hinted=hinted)
            if ref is not None:
                if is_vf:
                    w_spec = spec.weight("Regular")
                else:
                    w_val = int(font["OS/2"].usWeightClass)
                    w_spec = next((w for w in spec.weights if w.value == w_val), spec.weight("Regular"))
                _check_table_framework_parity(
                    font,
                    ref,
                    res,
                    spec,
                    region=region,
                    italic=italic,
                    is_vf=is_vf,
                    hinted=hinted,
                    weight_spec=w_spec,
                    subset_mode=subset_mode,
                    mode="upstream",
                )
        finally:
            if ref is not None:
                ref.close()
    return res
