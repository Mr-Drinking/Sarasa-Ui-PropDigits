"""由 VF 单真相源实例化 72 个去重叠 unhinted 静态 TTF，并写入静态元数据、STAT 与 post format 3。

为保证静态 hinted 中斜体 CJK/假名/韩文与正体轮廓点 100% 一一对应而不放宽任何重映射断言，
同一地区与字重的正体与斜体联动构建：先在去重叠前配对同源 9.4° 斜切字形，再对正体严格去重叠，
将去重叠后的正体字形按 9.4° 剪切写入斜体，仅对斜体未配对字形（Inter、竖排 3em 分段、复合字形等）
单独执行严格去重叠。
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Sequence

from fontTools.misc.fixedTools import otRound
from fontTools.otlLib.builder import buildStatTable
from fontTools.ttLib import TTFont
from fontTools.ttLib.removeOverlaps import removeOverlaps
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.varLib.instancer import instantiateVariableFont

from ..spec import Spec, WeightSpec, is_ideograph, is_kana, is_korean, load_spec
from ..vf.fixes import fix_os2_unicode_range_bit57, recalc_os2_max_context
from ..vf.metrics import recalculate_head_bounds_and_lsb
from ..vf.names import (
    apply_deterministic_head,
    drop_foreign_family_names,
    normalize_nonhinting_raster_metadata,
    prune_unreferenced_variable_names,
    remove_mac_name_records,
    set_name_record,
    update_caret_slope,
    update_project_legal_names,
)

VARIABLE_TABLES = ("fvar", "avar", "gvar", "HVAR", "VVAR", "MVAR", "VORG")


def _glyf_stats(font: TTFont) -> tuple[int, int]:
    glyf = font["glyf"]
    composites = 0
    contours = 0
    for gname in font.getGlyphOrder():
        g = glyf[gname]
        if g.isComposite():
            composites += 1
        elif getattr(g, "numberOfContours", 0) > 0:
            contours += int(g.numberOfContours)
    return composites, contours


def _snapshot_vertical_origins(font: TTFont, glyph_names: Sequence[str]) -> dict[str, int]:
    """记录去重叠或剪切前各字形的竖排原点 yOrigin = yMax + tsb，便于边界微变后保持竖排基线不漂移。"""
    if "vmtx" not in font or "glyf" not in font:
        return {}
    glyf = font["glyf"]
    vmtx = font["vmtx"].metrics
    origins: dict[str, int] = {}
    for gname in glyph_names:
        if gname not in vmtx:
            continue
        g = glyf[gname]
        if g.isComposite() or getattr(g, "numberOfContours", 0) > 0:
            g.recalcBounds(glyf)
            if hasattr(g, "yMax"):
                origins[gname] = int(g.yMax) + int(vmtx[gname][1])
    return origins


def _restore_vertical_origins(font: TTFont, origins: dict[str, int]) -> int:
    if not origins or "vmtx" not in font or "glyf" not in font:
        return 0
    glyf = font["glyf"]
    vmtx = font["vmtx"].metrics
    updated = 0
    for gname, v_origin in origins.items():
        if gname not in vmtx:
            continue
        g = glyf[gname]
        if g.isComposite() or getattr(g, "numberOfContours", 0) > 0:
            g.recalcBounds(glyf)
            if hasattr(g, "yMax"):
                adv, old_tsb = vmtx[gname]
                new_tsb = otRound(v_origin - int(g.yMax))
                if new_tsb != old_tsb:
                    vmtx[gname] = (adv, new_tsb)
                    updated += 1
    return updated


def remove_overlaps_strict(font: TTFont, glyph_names: Sequence[str] | None = None) -> dict[str, int]:
    """严格去除 TrueType 静态字形重叠轮廓（ignoreErrors=False，绝不静默跳过报错字形），统一整数化并保持竖排原点不变。"""
    targets = list(font.getGlyphOrder()) if glyph_names is None else list(glyph_names)
    comp_before, cont_before = _glyf_stats(font)
    v_origins = _snapshot_vertical_origins(font, targets)
    if targets:
        removeOverlaps(
            font,
            glyphNames=None if glyph_names is None else targets,
            removeHinting=True,
            ignoreErrors=False,
        )
        glyf = font["glyf"]
        for gname in targets:
            g = glyf[gname]
            g.expand(glyf)
            if not g.isComposite() and g.numberOfContours > 0 and hasattr(g, "coordinates"):
                g.coordinates.toInt(round=otRound)
                g.recalcBounds(glyf)
    tsb_adjusted = _restore_vertical_origins(font, v_origins)
    comp_after, cont_after = _glyf_stats(font)
    return {
        "composites_before": comp_before,
        "composites_after": comp_after,
        "contours_before": cont_before,
        "contours_after": cont_after,
        "vmtx_tsb_adjusted": tsb_adjusted,
    }


def _simple_sig(font: TTFont, gname: str) -> tuple[int, tuple[int, ...], tuple[int, ...]] | None:
    g = font["glyf"][gname]
    g.expand(font["glyf"])
    if g.isComposite() or g.numberOfContours <= 0:
        return None
    return (
        int(g.numberOfContours),
        tuple(int(e) for e in g.endPtsOfContours),
        tuple(int(f) & 1 for f in g.flags),
    )


def _is_sheared_simple_pair(
    up_font: TTFont,
    up_name: str,
    it_font: TTFont,
    it_name: str,
    center: float,
    shear: float,
    *,
    y_tol: float = 1.5,
    x_tol: float = 2.0,
) -> tuple[bool, float]:
    gu = up_font["glyf"][up_name]
    gi = it_font["glyf"][it_name]
    gu.expand(up_font["glyf"])
    gi.expand(it_font["glyf"])
    if gu.isComposite() or gi.isComposite() or gu.numberOfContours <= 0:
        return False, 0.0
    if gu.numberOfContours != gi.numberOfContours:
        return False, 0.0
    if list(gu.endPtsOfContours) != list(gi.endPtsOfContours):
        return False, 0.0
    if [int(f) & 1 for f in gu.flags] != [int(f) & 1 for f in gi.flags]:
        return False, 0.0
    cu, ci = gu.coordinates, gi.coordinates
    if len(cu) != len(ci):
        return False, 0.0
    adv_u = up_font["hmtx"].metrics[up_name][0]
    adv_i = it_font["hmtx"].metrics[it_name][0]
    if abs(int(adv_u) - int(adv_i)) > 1:
        return False, 0.0
    err_sum = 0.0
    for (xu, yu), (xi, yi) in zip(cu, ci):
        dy = abs(float(yi) - float(yu))
        if dy > y_tol:
            return False, 0.0
        dx = abs(float(xi) - (float(xu) + (float(yu) - center) * shear))
        if dx > x_tol:
            return False, 0.0
        err_sum += dy + dx
    return True, err_sum


def _pair_sheared_glyphs(
    up_font: TTFont,
    it_font: TTFont,
    spec: Spec,
) -> dict[str, str]:
    """在去重叠之前配对斜体中由正体按 9.4° 剪切生成的简单字形，返回 {it_glyph_name: up_glyph_name}。

    强断言全部编码 CJK/假名/韩文非空简单字形必须 100% 配对；Inter 真斜体（-10° 独立设计）不会通过
    em/3 9.4° 斜切校验，因此天然保持未配对状态并各自去重叠。
    """
    shear = math.tan(math.radians(abs(spec.italic_angle)))
    center = float(up_font["head"].unitsPerEm) / 3.0
    cu = up_font.getBestCmap() or {}
    ci = it_font.getBestCmap() or {}
    u_order = up_font.getGlyphOrder()
    i_order = it_font.getGlyphOrder()
    u_gid = {gn: idx for idx, gn in enumerate(u_order)}
    i_gid = {gn: idx for idx, gn in enumerate(i_order)}

    paired_it_to_up: dict[str, str] = {}
    used_up: set[str] = set()
    cjk_failures: list[str] = []

    # 1. 编码字形（强断言全部 CJK/假名/韩文简单字形必须配对）
    for cp in sorted(set(cu) & set(ci)):
        un, iname = cu[cp], ci[cp]
        if un == ".notdef" or iname == ".notdef":
            continue
        is_cjk = is_ideograph(cp) or is_kana(cp) or is_korean(cp)
        gu = up_font["glyf"][un]
        gi = it_font["glyf"][iname]
        gu.expand(up_font["glyf"])
        gi.expand(it_font["glyf"])
        if gu.numberOfContours == 0 and gi.numberOfContours == 0:
            continue
        if is_cjk:
            ok, _ = _is_sheared_simple_pair(up_font, un, it_font, iname, center, shear)
            if not ok:
                cjk_failures.append(f"U+{cp:04X}")
                continue
            if iname not in paired_it_to_up:
                paired_it_to_up[iname] = un
                used_up.add(un)
        elif iname not in paired_it_to_up:
            ok, _ = _is_sheared_simple_pair(up_font, un, it_font, iname, center, shear)
            if ok:
                paired_it_to_up[iname] = un
                used_up.add(un)

    if cjk_failures:
        raise RuntimeError(
            f"斜体存在 {len(cjk_failures)} 个 CJK/假名/韩文字形不是正体 9.4° 斜切，无法保持点对应："
            f"{cjk_failures[:8]}"
        )

    # 2. cmap format 14 UVS 变体字形
    u_uvs: dict[tuple[int, int], str] = {}
    i_uvs: dict[tuple[int, int], str] = {}
    for tbl in up_font["cmap"].tables:
        if tbl.format == 14:
            for sel, entries in tbl.uvsDict.items():
                for cp, gn in entries:
                    if gn and gn in u_gid:
                        u_uvs[(sel, cp)] = gn
    for tbl in it_font["cmap"].tables:
        if tbl.format == 14:
            for sel, entries in tbl.uvsDict.items():
                for cp, gn in entries:
                    if gn and gn in i_gid:
                        i_uvs[(sel, cp)] = gn
    for key, iname in i_uvs.items():
        if iname in paired_it_to_up or iname == ".notdef":
            continue
        un = u_uvs.get(key)
        if not un or un == ".notdef":
            continue
        ok, _ = _is_sheared_simple_pair(up_font, un, it_font, iname, center, shear)
        if ok:
            paired_it_to_up[iname] = un
            used_up.add(un)

    # 3. 剩余未编码的斜切同源简单字形（vert/vrt2/locl/Jamo 等）按轮廓拓扑 + 斜切校验配对
    up_by_sig: dict[tuple[int, tuple[int, ...], tuple[int, ...]], list[str]] = {}
    for un in u_order:
        if un == ".notdef":
            continue
        sig = _simple_sig(up_font, un)
        if sig is not None:
            up_by_sig.setdefault(sig, []).append(un)

    for iname in i_order:
        if iname in paired_it_to_up or iname == ".notdef":
            continue
        sig = _simple_sig(it_font, iname)
        if sig is None:
            continue
        cands = up_by_sig.get(sig)
        if not cands:
            continue
        ig = i_gid[iname]
        matches: list[tuple[bool, float, int, int, str]] = []
        for un in cands:
            ok, err_sum = _is_sheared_simple_pair(up_font, un, it_font, iname, center, shear)
            if ok:
                ug = u_gid[un]
                matches.append((un in used_up, err_sum, abs(ug - ig), ug, un))
        if matches:
            matches.sort()
            chosen_un = matches[0][4]
            paired_it_to_up[iname] = chosen_un
            used_up.add(chosen_un)

    return paired_it_to_up


def _apply_sheared_upright_outlines(
    up_font: TTFont,
    it_font: TTFont,
    paired_it_to_up: dict[str, str],
    spec: Spec,
) -> dict[str, int]:
    """将去重叠并整数化后的正体字形按 9.4°（以 em/3 为中心，y 坐标逐点不变）剪切写入斜体。"""
    shear = math.tan(math.radians(abs(spec.italic_angle)))
    center = float(up_font["head"].unitsPerEm) / 3.0
    v_origins = _snapshot_vertical_origins(it_font, list(paired_it_to_up))
    up_glyf = up_font["glyf"]
    it_glyf = it_font["glyf"]

    for it_name, up_name in paired_it_to_up.items():
        gu = up_glyf[up_name]
        gu.expand(up_glyf)
        if gu.isComposite():
            raise RuntimeError(f"已配对正体字形去重叠后意外为复合字形：{up_name}")
        copied = copy.deepcopy(gu)
        if copied.numberOfContours > 0 and hasattr(copied, "coordinates"):
            for idx, (x, y) in enumerate(copied.coordinates):
                yi = otRound(float(y))
                copied.coordinates[idx] = (otRound(float(x) + (float(yi) - center) * shear), yi)
        copied.program = Program()
        it_glyf.glyphs[it_name] = copied
        copied.recalcBounds(it_glyf)
        if hasattr(copied, "xMin"):
            adv = it_font["hmtx"].metrics[it_name][0]
            it_font["hmtx"].metrics[it_name] = (adv, int(copied.xMin))

    tsb_adjusted = _restore_vertical_origins(it_font, v_origins)
    return {"sheared_from_upright": len(paired_it_to_up), "sheared_vmtx_tsb_adjusted": tsb_adjusted}


def _legacy_family(base: str, weight_name: str) -> str:
    return base if weight_name in {"Regular", "Bold"} else f"{base} {weight_name}"


def update_static_names(
    font: TTFont,
    region: str,
    weight: WeightSpec,
    italic: bool,
    hinted: bool,
    spec: Spec,
) -> dict[str, Any]:
    r = spec.region(region)
    typo_fam = spec.static_family(r.code)
    typo_fam_local = spec.static_family_local(r.code)
    legacy_fam = _legacy_family(typo_fam, weight.name)
    legacy_fam_local = _legacy_family(typo_fam_local, weight.name)
    ps_family = spec.static_ps_family(r.code)

    if weight.name == "Bold":
        legacy_style = "Bold Italic" if italic else "Bold"
    else:
        legacy_style = "Italic" if italic else "Regular"

    typo_style = "Italic" if (weight.name == "Regular" and italic) else f"{weight.name}{' Italic' if italic else ''}"
    full = typo_fam if (weight.name == "Regular" and not italic) else f"{typo_fam} {typo_style}"
    full_local = typo_fam_local if (weight.name == "Regular" and not italic) else f"{typo_fam_local} {typo_style}"
    ps_suffix = "Italic" if (weight.name == "Regular" and italic) else f"{weight.name}{'-Italic' if italic else ''}"
    ps = f"{ps_family}-{ps_suffix}"

    src_label = Path(r.source_han_vf).stem
    if r.classical and r.classical_vf:
        src_label += f" + {Path(r.classical_vf).stem}"
    hint_label = "hinted" if hinted else "unhinted"
    ver_str = f"Version {spec.opentype_version()}; project {spec.version}; {src_label} + Inter VF; PD; {hint_label}"

    # 删除可变字体专用 nameID 25
    font["name"].names = [rec for rec in font["name"].names if rec.nameID != 25]

    for nid, val in (
        (1, legacy_fam),
        (2, legacy_style),
        (4, full),
        (5, ver_str),
        (6, ps),
        (16, typo_fam),
        (17, typo_style),
    ):
        set_name_record(font, nid, val)
    font["name"].setName(f"{ps};{spec.version};{hint_label}", 3, 3, 1, 0x0409)
    update_project_legal_names(font, r.code, spec)
    for nid, val in ((1, legacy_fam_local), (2, legacy_style), (4, full_local), (16, typo_fam_local), (17, typo_style)):
        font["name"].setName(val, nid, 3, 1, r.local_lang_id)
    remove_mac_name_records(font)
    drop_foreign_family_names(font, r.code, spec)
    return {"static_family": typo_fam, "postscript_name": ps, "version_string": ver_str}


def rebuild_static_stat(font: TTFont, weight: WeightSpec, italic: bool) -> None:
    axes = [
        {
            "tag": "wght",
            "name": "Weight",
            "values": [{"value": weight.value, "name": weight.name, "flags": weight.flags}],
        },
        {
            "tag": "ital",
            "name": "Italic",
            "values": [
                {
                    "value": 1 if italic else 0,
                    "name": "Italic" if italic else "Roman",
                    **({"linkedValue": 1} if not italic else {}),
                    "flags": 0x2 if not italic else 0,
                }
            ],
        },
    ]
    buildStatTable(font, axes)
    remove_mac_name_records(font)


def update_static_os2_and_post(font: TTFont, region: str, weight: WeightSpec, italic: bool, spec: Spec) -> None:
    r = spec.region(region)
    os2 = font["OS/2"]
    os2.version = max(int(os2.version), 4)
    os2.usWeightClass = weight.value
    os2.achVendID = spec.vendor_id
    os2.ulCodePageRange1 = r.codepage_range1
    os2.ulCodePageRange2 = 0
    os2.panose.bWeight = 1 + weight.value // 100
    os2.yStrikeoutSize = weight.strikeout_size
    os2.xAvgCharWidth = weight.x_avg_char_width_italic if italic else weight.x_avg_char_width_upright

    os2.fsSelection |= 1 << 7  # USE_TYPO_METRICS
    os2.fsSelection &= ~((1 << 0) | (1 << 5) | (1 << 6))
    font["head"].macStyle &= ~0b11
    if italic:
        os2.fsSelection |= 1 << 0
        font["head"].macStyle |= 0b10
        font["post"].italicAngle = spec.italic_angle
    else:
        font["post"].italicAngle = 0.0
    if weight.name == "Bold":
        os2.fsSelection |= 1 << 5
        font["head"].macStyle |= 0b01
    if weight.name != "Bold" and not italic:
        os2.fsSelection |= 1 << 6

    post = font["post"]
    post.underlineThickness = weight.underline_thickness
    post.underlinePosition = weight.underline_position
    post.formatType = 3.0
    post.extraNames = []
    post.mapping = {}
    update_caret_slope(font)


def _finalize_and_save_static(
    font: TTFont,
    region: str,
    weight: WeightSpec,
    italic: bool,
    out_path: Path,
    spec: Spec,
    extra_metrics: dict[str, Any],
    t0: float,
) -> dict[str, Any]:
    for tag in VARIABLE_TABLES:
        if tag in font:
            del font[tag]
    update_static_os2_and_post(font, region, weight, italic, spec)
    name_rep = update_static_names(font, region, weight, italic, hinted=False, spec=spec)
    rebuild_static_stat(font, weight, italic)
    prune_unreferenced_variable_names(font)
    recalculate_head_bounds_and_lsb(font)
    fix_os2_unicode_range_bit57(font)
    recalc_os2_max_context(font)
    normalize_nonhinting_raster_metadata(font, spec)
    apply_deterministic_head(font, spec)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".writing.ttf")
    font.save(tmp, reorderTables=True)
    glyph_count = len(font.getGlyphOrder())
    os.replace(tmp, out_path)
    return {
        "region": region,
        "weight": weight.name,
        "weight_value": weight.value,
        "italic": italic,
        "filename": out_path.name,
        "glyph_count": glyph_count,
        "size_bytes": out_path.stat().st_size,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        **extra_metrics,
        **name_rep,
    }


def _instantiate_pair_from_opened_vfs(
    up_vf: TTFont,
    it_vf: TTFont | None,
    region: str,
    weight: WeightSpec,
    *,
    want_upright: bool,
    want_italic: bool,
    out_dir: Path,
    spec: Spec,
    upright_out_path: Path | None = None,
    italic_out_path: Path | None = None,
) -> list[dict[str, Any]]:
    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    up_font = instantiateVariableFont(up_vf, {"wght": float(weight.value)}, inplace=False, optimize=True)
    it_font = (
        instantiateVariableFont(it_vf, {"wght": float(weight.value)}, inplace=False, optimize=True)
        if (want_italic and it_vf is not None)
        else None
    )
    try:
        paired_it_to_up: dict[str, str] = {}
        if it_font is not None:
            paired_it_to_up = _pair_sheared_glyphs(up_font, it_font, spec)

        up_deov = remove_overlaps_strict(up_font)
        if want_upright:
            out_up = upright_out_path or (out_dir / spec.static_filename(region, weight.name, False))
            results.append(
                _finalize_and_save_static(up_font, region, weight, False, out_up, spec, up_deov, t0)
            )

        if it_font is not None:
            t_it = time.perf_counter()
            shear_info = _apply_sheared_upright_outlines(up_font, it_font, paired_it_to_up, spec)
            unpaired_it = [gn for gn in it_font.getGlyphOrder() if gn not in paired_it_to_up]
            it_deov = remove_overlaps_strict(it_font, glyph_names=unpaired_it)
            it_metrics = {**shear_info, "unpaired_deoverlapped": len(unpaired_it), **it_deov}
            out_it = italic_out_path or (out_dir / spec.static_filename(region, weight.name, True))
            results.append(
                _finalize_and_save_static(it_font, region, weight, True, out_it, spec, it_metrics, t_it)
            )
    finally:
        up_font.close()
        if it_font is not None:
            it_font.close()
    return results


def instantiate_static_from_vf(
    vf_path: Path,
    out_path: Path,
    *,
    region: str,
    weight_name: str,
    italic: bool,
    upright_vf_path: Path | None = None,
    spec: Spec | None = None,
) -> dict[str, Any]:
    spec = spec or load_spec()
    r = spec.region(region)
    w = spec.weight(weight_name)
    if italic:
        companion = upright_vf_path
        if companion is None and vf_path.name == spec.variable_filename(r.code, True):
            cand = vf_path.with_name(spec.variable_filename(r.code, False))
            if cand.is_file():
                companion = cand
        if companion is not None and companion.is_file():
            with TTFont(companion, recalcTimestamp=False) as up_vf, TTFont(vf_path, recalcTimestamp=False) as it_vf:
                batch = _instantiate_pair_from_opened_vfs(
                    up_vf,
                    it_vf,
                    r.code,
                    w,
                    want_upright=False,
                    want_italic=True,
                    out_dir=out_path.parent,
                    spec=spec,
                    italic_out_path=out_path,
                )
                return batch[0]
    t0 = time.perf_counter()
    with TTFont(vf_path, recalcTimestamp=False) as vf:
        font = instantiateVariableFont(vf, {"wght": float(w.value)}, inplace=False, optimize=True)
        try:
            deov = remove_overlaps_strict(font)
            return _finalize_and_save_static(font, r.code, w, italic, out_path, spec, deov, t0)
        finally:
            font.close()


def _worker_instantiate_region(args: tuple[str, str | None, str, list[str], list[str], str]) -> list[dict[str, Any]]:
    up_vf_str, it_vf_str, region, weight_names, styles, out_dir_str = args
    spec = load_spec()
    r = spec.region(region)
    out_dir = Path(out_dir_str)
    want_upright = "upright" in styles
    want_italic = "italic" in styles
    results: list[dict[str, Any]] = []
    # 同一地区正斜体联动：先正体去重叠，再将配对字形按 9.4° 斜切至斜体，保持正斜体点数逐一对应
    with TTFont(Path(up_vf_str), recalcTimestamp=False) as up_vf:
        it_vf = TTFont(Path(it_vf_str), recalcTimestamp=False) if (want_italic and it_vf_str) else None
        try:
            for w_name in weight_names:
                w = spec.weight(w_name)
                results.extend(
                    _instantiate_pair_from_opened_vfs(
                        up_vf,
                        it_vf,
                        r.code,
                        w,
                        want_upright=want_upright,
                        want_italic=want_italic,
                        out_dir=out_dir,
                        spec=spec,
                    )
                )
        finally:
            if it_vf is not None:
                it_vf.close()
    return results


def build_unhinted_statics(
    vf_root: Path,
    out_root: Path,
    regions: list[str],
    styles: list[str],
    weights: list[str],
    *,
    jobs: int = 4,
    spec: Spec | None = None,
) -> dict[str, Any]:
    """按地区并行由 VF 实例化去重叠 unhinted 静态字体（斜体联动同地区正体去重叠后斜切）。"""
    spec = spec or load_spec()
    vf_dir = vf_root.resolve() / "variable"
    want_italic = "italic" in styles
    tasks: list[tuple[str, str | None, str, list[str], list[str], str]] = []
    for region in regions:
        out_dir = spec.static_dir(out_root.resolve(), region, hinted=False)
        up_vf = vf_dir / spec.variable_filename(region, False)
        if not up_vf.is_file():
            raise FileNotFoundError(f"缺少正体源 VF（斜体去重叠联动亦需同地区正体 VF）：{up_vf}")
        it_vf_str: str | None = None
        if want_italic:
            it_vf = vf_dir / spec.variable_filename(region, True)
            if not it_vf.is_file():
                raise FileNotFoundError(f"缺少斜体源 VF：{it_vf}")
            it_vf_str = str(it_vf)
        tasks.append((str(up_vf), it_vf_str, region, list(weights), list(styles), str(out_dir)))

    t0 = time.perf_counter()
    all_results: list[dict[str, Any]] = []
    if jobs <= 1 or len(tasks) <= 1:
        for item in tasks:
            batch = _worker_instantiate_region(item)
            all_results.extend(batch)
            print(f"[unhinted] {item[2]}: {len(batch)} statics done", flush=True)
    else:
        pool = ProcessPoolExecutor(max_workers=min(jobs, len(tasks)))
        try:
            futs = {pool.submit(_worker_instantiate_region, item): item for item in tasks}
            for fut in as_completed(futs):
                batch = fut.result()
                all_results.extend(batch)
                print(f"[unhinted] {batch[0]['region']}: {len(batch)} statics done", flush=True)
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

    w_order = {w.name: i for i, w in enumerate(spec.weights)}
    all_results.sort(key=lambda r: (spec.region_order.index(r["region"]), w_order[r["weight"]], r["italic"]))
    summary = {
        "stage": "unhinted",
        "count": len(all_results),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "outputs": all_results,
    }
    (out_root / "unhinted-build-report.json").write_bytes(
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return summary
