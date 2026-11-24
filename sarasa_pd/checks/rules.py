"""面向最终产品契约的规则检查（对应重构计划第 5 节与第四份审计回归条目）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
import uharfbuzz as hb

from ..spec import Spec, is_ideograph, load_spec


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
) -> None:
    cmap = font.getBestCmap() or {}
    order = set(font.getGlyphOrder())
    res.metrics["codepoints"] = len(cmap)

    if reference is not None:
        ref_cmap = reference.getBestCmap() or {}
        if subset_mode == "full":
            missing = sorted(set(ref_cmap) - set(cmap))
            extra = sorted(set(cmap) - set(ref_cmap))
            if missing:
                res.fail(f"cmap 缺少上游码位 {len(missing)} 个，样例：{[hex(c) for c in missing[:6]]}")
            if extra:
                res.fail(f"cmap 多出上游外码位 {len(extra)} 个，样例：{[hex(c) for c in extra[:6]]}")

    # A1：cmap format 14 存在且目标字形均在 glyf 中可达
    sub14 = [t for t in font["cmap"].tables if t.format == 14]
    if not sub14:
        res.fail("缺少 cmap format 14 (UVS) 子表（A1 回归）")
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
        res.fail("cmap format 14 为空（A1 回归）")
    if dangling:
        res.fail(f"cmap format 14 存在 {dangling} 个悬空目标字形（A1/C23 回归）")

    if reference is not None and subset_mode == "full":
        ref14 = [t for t in reference["cmap"].tables if t.format == 14]
        if ref14:
            ref_pairs = {(sel, cp) for t in ref14 for sel, es in t.uvsDict.items() for cp, _ in es}
            cur_pairs = {(sel, cp) for t in sub14 for sel, es in t.uvsDict.items() for cp, _ in es}
            miss_uvs = ref_pairs - cur_pairs
            if miss_uvs:
                res.fail(f"cmap format 14 相比上游缺失 {len(miss_uvs)} 条 UVS 映射")


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

    # 1) 冒号在数字间（默认 / tnum / pnum / tnum+zero 全部组合下）应上浮
    for feats, expected_colon in (
        ({}, case_colon),
        ({"pnum": True}, case_colon),
        ({"tnum": True}, tnum_case_colon),
        ({"tnum": True, "zero": True}, tnum_case_colon),
    ):
        seq = gids("1:2", feats)
        if len(seq) != 3 or seq[1] != expected_colon:
            res.fail(f"数字间冒号未上浮（feats={feats}, actual_gid={seq[1] if len(seq) >= 2 else None}, expected={expected_colon}）")

    # 2) A12.5：Left:1 开 dlig 时冒号不得误上浮
    left1 = gids("Left:1", {"dlig": True})
    if len(left1) >= 5 and left1[4] != base_colon:
        res.fail(f"A12.5 回归：Left:1 开 dlig 时冒号误上浮（gid={left1[4]} != base {base_colon}）")
    # 顺带确认 Left:1 开 tnum+dlig 也是基础等宽冒号而非上浮冒号
    left1_tnum = gids("Left:1", {"dlig": True, "tnum": True})
    if len(left1_tnum) >= 5 and left1_tnum[4] != tnum_colon:
        res.fail(f"A12.5 回归：Left:1 开 dlig+tnum 时冒号误变（gid={left1_tnum[4]} != tnum {tnum_colon}）")

    # 3) A2：x<-1 / 3<-2 / 温度<-5℃ 不得变左箭头连字
    if len(gids("x<-1")) != 4 or len(gids("3<-2")) != 4:
        res.fail(f"A2 回归：x<-1 或 3<-2 误触发箭头连字 -> {gids('x<-1')}, {gids('3<-2')}")
    # 4) 箭头本身在独立位置应生效
    if len(gids("->")) != 1:
        res.fail(f"箭头连字 -> 未生效 -> {gids('->')}")
    # 5) A4：2026/11/19 开 frac 不得误变分数斜杠
    date_plain = gids("2026/11/19")
    date_frac = gids("2026/11/19", {"frac": True})
    if date_frac != date_plain:
        res.fail(f"A4 回归：2026/11/19 开 frac 被破坏 -> {date_frac}")
    # 6) H(H) 开 sups/subs 走 Inter 原生 .case 括号（用户 11-24 确认）
    lparen_case = gids("(", {"case": True})[0]
    rparen_case = gids(")", {"case": True})[0]
    for ftag in ("sups", "subs"):
        seq = gids("H(H)", {ftag: True})
        if len(seq) != 4 or seq[1] != lparen_case or seq[3] != rparen_case:
            res.fail(f"A12.5 回归：H(H) 开 {ftag} 括号未走 .case（got {seq}, expected parens {lparen_case}/{rparen_case}）")


def _check_chws_and_punctuation(font: TTFont, hb_font: hb.Font, res: CheckResult, region: str) -> None:
    order = font.getGlyphOrder()
    # chws / vchw 存在并产生挤压
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

    # 标点与语言路由：latn/dflt 为比例省略号，hani/bopo/hang/kana 默认为全宽省略号
    latn_ell = order[_shape(hb_font, "…", script="latn")[0][0]]
    for sc in ("hani", "kana", "hang", "bopo"):
        cjk_ell = order[_shape(hb_font, "…", script=sc)[0][0]]
        if cjk_ell == latn_ell:
            res.fail(f"{sc}/dflt 省略号未切到全宽形（仍为 {latn_ell}）")

    # 连续 U+FE31 不得被 ccmp 吞成单字形
    fe31 = _shape(hb_font, "\uFE31\uFE31", script="hani")
    if len(fe31) != 2:
        res.fail(f"U+FE31 连续破折号被 ccmp 误合并（len={len(fe31)}）")

    # cyrl/grek 支持 tnum
    for sc, txt in (("cyrl", "Время 0123"), ("grek", "αβ 0123")):
        ws = [x[1] for x in _shape(hb_font, txt, features={"tnum": True}, script=sc)[-4:]]
        if len(set(ws)) != 1:
            res.fail(f"{sc} 脚本下 tnum 数字未等宽：{ws}")

    # 古韩文 L+V+T 塑形不崩且步进合理
    old_hangul = _shape(hb_font, "\u1100\u1161\u11A8", script="hang")
    if not old_hangul or sum(x[1] for x in old_hangul) <= 0:
        res.fail(f"古韩文 L+V+T 塑形异常：{old_hangul}")

    # CL 专属检查：A5 汉字全宽 1000 & 㪉㪘㪣㪰 非 mark
    if region == "CL":
        cmap = font.getBestCmap() or {}
        hmtx = font["hmtx"].metrics
        bad_a5 = [
            hex(cp)
            for cp, gn in cmap.items()
            if is_ideograph(cp) and hmtx.get(gn, (0, 0))[0] != 1000
        ]
        if bad_a5:
            res.fail(f"A5 回归：CL 存在 {len(bad_a5)} 个非 1000 宽汉字，样例：{bad_a5[:6]}")
        for cp in (0x3A89, 0x3A98, 0x3AA3, 0x3AB0):
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
    if any(r.platformID == 1 for r in name.names):
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

    # OS/2 bit 57（Non-Plane 0）
    cmap = font.getBestCmap() or {}
    if any(cp > 0xFFFF for cp in cmap):
        if not (font["OS/2"].ulUnicodeRange2 & (1 << 25)):
            res.fail("A18 回归：含非 BMP 码位但 OS/2 ulUnicodeRange bit 57 未置位")

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


def check_font_rules(
    font_path: Path,
    *,
    region: str,
    italic: bool,
    is_vf: bool,
    hinted: bool = False,
    reference_path: Path | None = None,
    subset_mode: str = "full",
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
            _check_cmap_and_uvs(font, res, reference=ref, subset_mode=subset_mode)
        finally:
            if ref is not None:
                ref.close()
        _check_propdigits_and_tnum(font, hb_font, res, spec, is_vf)
        _check_inter_calt_and_guards(font, hb_font, res)
        _check_chws_and_punctuation(font, hb_font, res, region)
        _check_names_os2_and_raster(font, res, spec, region=region, italic=italic, is_vf=is_vf, hinted=hinted)
    return res
