"""面向最终产品契约的规则检查（对应重构计划第 5 节与第四份审计回归条目）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fontTools.misc.fixedTools import otRound
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot
from fontTools.varLib.models import normalizeValue, piecewiseLinearMap
from fontTools.varLib.varStore import VarStoreInstancer
import uharfbuzz as hb

from ..spec import Spec, is_ideograph, load_spec, use_inter_codepoint
from ..vf.base import _single_sub_mapping
from ..vf.layout_utils import routes


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
    a9_repair: bool = False,
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
                if a9_repair and ref_quoteright_rho is None and "quoteright" in m and "rho" in m["quoteright"]:
                    ref_quoteright_rho = m["quoteright"]["rho"]
                subs.append((1, m))
            elif p.Format == 2:
                cov = set(p.Coverage.glyphs)
                c1d = dict(p.ClassDef1.classDefs)
                c2d = dict(p.ClassDef2.classDefs)
                mat = [[_eval_value_xadv(r2.Value1, inst) for r2 in r1.Class2Record] for r1 in p.Class1Record]
                subs.append((2, cov, c1d, c2d, mat))
        if a9_repair and ref_quoteright_rho is not None:
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
            kern_by_w[wv] = _extract_kern_tables(inter, weight=float(wv), opsz=spec.inter_opsz, a9_repair=True)
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
) -> None:
    """检查 GPOS PairSet 排序、A9（ʼρ=-62）以及全量 Inter 字形对字偶距与单次取整步进一致性。"""
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

    # 2) A9 专项回归：ʼρ (U+02BC U+03C1) 字偶距必须为 -62（容差 1 unit），严禁退化为 -21
    if 0x02BC in cmap and 0x03C1 in cmap:
        for w in weights:
            if is_vf:
                hb_font.set_variations({"wght": float(w)})
            base_adv = _shape(hb_font, "\u02bc")[0][1]
            for sc in (None, "grek"):
                paired_adv = _shape(hb_font, "\u02bc\u03c1", script=sc)[0][1]
                kern = paired_adv - base_adv
                if w == 400 or not is_vf:
                    res.metrics["a9_mod_apostrophe_rho_kern"] = kern
                if abs(kern - (-62)) > 1:
                    res.fail(f"A9 回归：wght={w} script={sc or 'auto'} ʼρ 字偶距为 {kern}（期望 -62 ±1）")
                    break
        if is_vf:
            hb_font.set_variations({"wght": 400.0})

    # 3) 推广检查：全部 Inter-Inter 字形对字偶距与 Inter 一致（容差 1 unit），且命名字重步进为单次取整
    if inter_vf_path is None or not inter_vf_path.is_file():
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
        _check_empty_gsub_features(font, res, spec, italic=italic)
        _check_inter_kerning_and_advances(
            font, hb_font, res, spec, is_vf=is_vf, inter_vf_path=inter_vf_path
        )
        _check_names_os2_and_raster(font, res, spec, region=region, italic=italic, is_vf=is_vf, hinted=hinted)
    return res
