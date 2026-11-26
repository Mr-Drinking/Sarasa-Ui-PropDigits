"""二进制级检查：OTS 校验、FreeType 严格执行扫描、hint 竖向位移门、hinted/unhinted 与 VF/静态光栅一致性。"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence

import freetype
from fontTools.ttLib import TTFont
from PIL import Image, ImageChops, ImageStat
import uharfbuzz as hb

from ..spec import Spec, is_ideograph, load_spec
from .rules import CheckResult, _check_table_framework_parity

# 日常快速检查采样的字号列表：9–20 px 逐字号全覆 + 24–128 px 每 4 px（共 39 字号，约全量 1/3 耗时）
FT_FAST_PPEMS: tuple[int, ...] = tuple([*range(9, 21), *range(24, 129, 4)])
# VF 与去重叠静态未 hint 光栅对比的默认字号
RASTER_PARITY_PPEMS: tuple[int, ...] = (16, 28, 48)
# hint 竖向点位移门禁的默认字号范围（12–48 px 逐字号全覆，共 37 个字号）
HINT_DISP_PPEMS: tuple[int, ...] = tuple(range(12, 49))
# 常设哨兵字：12–48 px 逐字号校验（非 Heavy 要求全 7 字 ≤ +1.00 px，且「走」≤ +0.70 px 与绝对 ≤ 1.50 px；Heavy 对照本版 Bold 要求全 7 字 ≤ +1.80 px 且「走」绝对 ≤ 1.50 px）
HINT_DISP_SENTINELS: tuple[str, ...] = ("走", "歌", "男", "美", "哥", "虎", "虚")
HINT_DISP_SENTINEL_MARGIN_64: int = 64  # 1.00 px（允许跨源 1 unit 轮廓微差引起的整像素网格对齐跳变）
HINT_DISP_ZOU_MARGIN_64: int = 45  # 0.70 px（全 6 地区 × 5 常规字重实测最大 +0.62 px）
HINT_DISP_ZOU_ABS_MAX_64: int = 96  # 1.50 px


def check_ots(font_path: Path, ots_exe: Path) -> CheckResult:
    res = CheckResult(ok=True)
    proc = subprocess.run([str(ots_exe), str(font_path)], capture_output=True, text=True)
    res.metrics["ots_returncode"] = proc.returncode
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        res.fail(f"OTS 校验失败（rc={proc.returncode}）：{detail[:300]}")
    return res


def _scan_ft_chunk(args: tuple[str, list[int]]) -> tuple[int, list[dict[str, Any]]]:
    font_path, ppems = args
    face = freetype.Face(font_path)
    n = face.num_glyphs
    flags = freetype.FT_LOAD_PEDANTIC | freetype.FT_LOAD_NO_AUTOHINT | freetype.FT_LOAD_TARGET_NORMAL
    fails = 0
    samples: list[dict[str, Any]] = []
    for ppem in ppems:
        face.set_pixel_sizes(0, ppem)
        for gid in range(n):
            try:
                face.load_glyph(gid, flags)
            except freetype.FT_Exception as err:
                fails += 1
                if len(samples) < 8:
                    samples.append({"ppem": ppem, "gid": gid, "err": str(err)})
    return fails, samples


def scan_freetype_pedantic(
    font_path: Path,
    *,
    ppem_start: int = 9,
    ppem_end: int = 128,
    ppems: Sequence[int] | None = None,
    mode: str = "full",
    workers: int = 4,
) -> CheckResult:
    """在灰阶严格模式下扫描全部字形 × 目标字号集合，要求 0 执行中止。

    - mode="full"：扫描 [ppem_start, ppem_end] 全部字号（默认 9–128 px，120 个字号，发版前硬门禁）；
    - mode="fast"：扫描 FT_FAST_PPEMS（9–20 px 逐个 + 24–128 px 每 4 px，39 个字号，日常快速检查）；
    - ppems 显式传入时优先使用该字号序列。
    """
    res = CheckResult(ok=True)
    if ppems is not None:
        target_ppems = [int(p) for p in ppems]
        range_label = f"custom({len(target_ppems)})"
    elif mode == "fast":
        target_ppems = list(FT_FAST_PPEMS)
        range_label = "fast(9-20,24..128:4)"
    elif mode == "full":
        target_ppems = list(range(ppem_start, ppem_end + 1))
        range_label = f"{ppem_start}-{ppem_end}"
    else:
        raise ValueError(f"未知 FreeType 扫描模式：{mode!r}（仅支持 'fast' 或 'full'）")

    total_fails = 0
    samples: list[dict[str, Any]] = []
    if workers <= 1 or len(target_ppems) <= 4:
        total_fails, samples = _scan_ft_chunk((str(font_path), target_ppems))
    else:
        chunks = [target_ppems[i::workers] for i in range(workers)]
        chunks = [c for c in chunks if c]
        with ProcessPoolExecutor(max_workers=min(workers, len(chunks))) as ex:
            for fails, chunk_samples in ex.map(_scan_ft_chunk, [(str(font_path), c) for c in chunks]):
                total_fails += fails
                if len(samples) < 12:
                    samples.extend(chunk_samples[: 12 - len(samples)])
    res.metrics.update({
        "ft_mode": mode,
        "ppem_range": range_label,
        "ppem_count": len(target_ppems),
        "fail_events": total_fails,
        "samples": samples,
    })
    if total_fails > 0:
        res.fail(f"FreeType 严格执行扫描发现 {total_fails} 次失败（{range_label} px），样例：{samples[:4]}")
    return res


def check_hinted_unhinted_parity(hinted_path: Path, unhinted_path: Path) -> CheckResult:
    """验证每一对 hinted 与 unhinted：去指令后轮廓逐点相同、布局表逐字节相同。"""
    res = CheckResult(ok=True)
    with TTFont(hinted_path, lazy=False) as fh, TTFont(unhinted_path, lazy=False) as fu:
        if fh.getGlyphOrder() != fu.getGlyphOrder():
            res.fail("hinted 与 unhinted 字形顺序不一致")
            return res

        coord_diffs = 0
        for gname in fu.getGlyphOrder():
            gh, gu = fh["glyf"][gname], fu["glyf"][gname]
            gh.expand(fh["glyf"])
            gu.expand(fu["glyf"])
            if gh.numberOfContours != gu.numberOfContours:
                coord_diffs += 1
            elif gh.isComposite():
                ch = [(c.glyphName, c.x, c.y, getattr(c, "transform", None)) for c in gh.components]
                cu = [(c.glyphName, c.x, c.y, getattr(c, "transform", None)) for c in gu.components]
                if ch != cu:
                    coord_diffs += 1
            elif gh.numberOfContours > 0:
                if (
                    list(gh.coordinates) != list(gu.coordinates)
                    or list(gh.endPtsOfContours) != list(gu.endPtsOfContours)
                    or [f & 1 for f in gh.flags] != [f & 1 for f in gu.flags]
                ):
                    coord_diffs += 1
        res.metrics["outline_diffs"] = coord_diffs
        if coord_diffs:
            res.fail(f"hinted 与 unhinted 去指令后有 {coord_diffs} 个字形轮廓不一致")

        tbl_diffs: list[str] = []
        for tag in ("GSUB", "GPOS", "GDEF", "cmap"):
            if (tag in fh) != (tag in fu) or (tag in fh and fh.getTableData(tag) != fu.getTableData(tag)):
                tbl_diffs.append(tag)
        res.metrics["layout_table_diffs"] = tbl_diffs
        if tbl_diffs:
            res.fail(f"hinted 与 unhinted 布局表非逐字节相同：{tbl_diffs}")
    return res


def _raster_parity_chunk(
    args: tuple[str, str, float, list[tuple[int, int, str]], tuple[int, ...]]
) -> dict[str, Any]:
    vf_path, st_path, weight, pairs, ppems = args
    f_vf = freetype.Face(vf_path)
    is_mm = bool(f_vf.has_multiple_masters)
    if is_mm:
        f_vf.set_var_design_coords((weight,))
    f_st = freetype.Face(st_path)
    st_flags = freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_NORMAL
    vf_load_flags = freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_TARGET_NORMAL
    ft_outline_overlap = 0x40  # FT_OUTLINE_OVERLAP：对含重叠轮廓的 VF 启用 2x2 超采样消除内部接缝双倍覆盖伪影

    max_px = 0
    max_mean = 0.0
    max_mean_large = 0.0
    max_box_shift = 0
    empty_mismatches = 0
    bad_samples: list[dict[str, Any]] = []

    for ppem in ppems:
        f_vf.set_pixel_sizes(0, ppem)
        f_st.set_pixel_sizes(0, ppem)
        for gid_vf, gid_st, label in pairs:
            if is_mm:
                f_vf.load_glyph(gid_vf, vf_load_flags)
                f_vf.glyph._FT_GlyphSlot.contents.outline.flags |= ft_outline_overlap
                f_vf.glyph.render(freetype.FT_RENDER_MODE_NORMAL)
            else:
                f_vf.load_glyph(gid_vf, st_flags)
            ba = f_vf.glyph.bitmap
            wa, ha = ba.width, ba.rows
            lxa, tya = f_vf.glyph.bitmap_left, f_vf.glyph.bitmap_top
            buf_a = bytes(ba.buffer) if (wa and ha) else b""

            f_st.load_glyph(gid_st, st_flags)
            bb = f_st.glyph.bitmap
            wb, hb_rows = bb.width, bb.rows
            lxb, tyb = f_st.glyph.bitmap_left, f_st.glyph.bitmap_top
            buf_b = bytes(bb.buffer) if (wb and hb_rows) else b""

            if not buf_a and not buf_b:
                continue
            if (not buf_a) != (not buf_b):
                non_empty = buf_a or buf_b
                peak = max(non_empty) if non_empty else 0
                if peak > 64:
                    empty_mismatches += 1
                    max_px = max(max_px, peak)
                    if len(bad_samples) < 8:
                        bad_samples.append({"ppem": ppem, "glyph": label, "reason": "empty_mismatch", "peak": peak})
                continue

            ima = Image.frombytes("L", (wa, ha), buf_a)
            imb = Image.frombytes("L", (wb, hb_rows), buf_b)
            shift = max(abs(lxa - lxb), abs(tya - tyb), abs(wa - wb), abs(ha - hb_rows))
            if shift > max_box_shift:
                max_box_shift = shift

            if lxa == lxb and tya == tyb and wa == wb and ha == hb_rows:
                diff = ImageChops.difference(ima, imb)
                cw, ch = wa, ha
            else:
                x0 = min(lxa, lxb)
                y0 = min(-tya, -tyb)
                x1 = max(lxa + wa, lxb + wb)
                y1 = max(-tya + ha, -tyb + hb_rows)
                cw, ch = x1 - x0, y1 - y0
                ca = Image.new("L", (cw, ch), 0)
                cb = Image.new("L", (cw, ch), 0)
                ca.paste(ima, (lxa - x0, -tya - y0))
                cb.paste(imb, (lxb - x0, -tyb - y0))
                diff = ImageChops.difference(ca, cb)

            mx = int(diff.getextrema()[1])
            mean_abs = float(ImageStat.Stat(diff).mean[0])
            is_large = cw >= 8 and ch >= 8
            if mx > max_px:
                max_px = mx
            if mean_abs > max_mean:
                max_mean = mean_abs
            if is_large and mean_abs > max_mean_large:
                max_mean_large = mean_abs
            if shift > 2 or mean_abs > 18.0 or (is_large and mean_abs > 8.0):
                if len(bad_samples) < 8:
                    bad_samples.append({
                        "ppem": ppem,
                        "glyph": label,
                        "box_shift": shift,
                        "max_px": mx,
                        "mean_abs": round(mean_abs, 2),
                        "canvas": [cw, ch],
                    })

    return {
        "max_px": max_px,
        "max_mean": max_mean,
        "max_mean_large": max_mean_large,
        "max_box_shift": max_box_shift,
        "empty_mismatches": empty_mismatches,
        "bad_samples": bad_samples,
    }


def check_unhinted_raster_parity(
    vf_path: Path,
    static_path: Path,
    weight: int,
    *,
    ppems: Sequence[int] = RASTER_PARITY_PPEMS,
    workers: int = 4,
) -> CheckResult:
    """用 FreeType 无 hint 光栅化逐字形比对 VF 与去重叠静态字体，确认去重叠前后外观一致（仅允许抗锯齿取整微差）。"""
    res = CheckResult(ok=True)
    target_ppems = tuple(int(p) for p in ppems)
    with TTFont(vf_path, lazy=True) as fvf, TTFont(static_path, lazy=True) as fst:
        order_vf = fvf.getGlyphOrder()
        order_st = fst.getGlyphOrder()
        if len(order_vf) == len(order_st) and order_vf == order_st:
            pairs = [(gid, gid, f"gid{gid}") for gid in range(len(order_vf))]
        else:
            cm_vf = fvf.getBestCmap() or {}
            cm_st = fst.getBestCmap() or {}
            if set(cm_vf) != set(cm_st):
                res.fail(f"VF 与静态 cmap 不一致（VF={len(cm_vf)}, static={len(cm_st)}）")
                return res
            pairs = [
                (fvf.getGlyphID(cm_vf[cp]), fst.getGlyphID(cm_st[cp]), f"U+{cp:04X}")
                for cp in sorted(cm_vf)
            ]

    if workers <= 1 or len(pairs) < 256:
        parts = [_raster_parity_chunk((str(vf_path), str(static_path), float(weight), pairs, target_ppems))]
    else:
        n_chunks = min(workers, max(1, len(pairs) // 64))
        chunks = [pairs[i::n_chunks] for i in range(n_chunks)]
        with ProcessPoolExecutor(max_workers=n_chunks) as ex:
            parts = list(
                ex.map(
                    _raster_parity_chunk,
                    [(str(vf_path), str(static_path), float(weight), ch, target_ppems) for ch in chunks],
                )
            )

    max_px = max((p["max_px"] for p in parts), default=0)
    max_mean = max((p["max_mean"] for p in parts), default=0.0)
    max_mean_large = max((p["max_mean_large"] for p in parts), default=0.0)
    max_box_shift = max((p["max_box_shift"] for p in parts), default=0)
    empty_mismatches = sum(p["empty_mismatches"] for p in parts)
    bad_samples: list[dict[str, Any]] = []
    for p in parts:
        if len(bad_samples) < 12:
            bad_samples.extend(p["bad_samples"][: 12 - len(bad_samples)])

    res.metrics.update({
        "raster_ppems": list(target_ppems),
        "raster_glyphs_checked": len(pairs),
        "raster_max_px_diff": max_px,
        "raster_max_mean_abs": round(max_mean, 3),
        "raster_max_mean_large": round(max_mean_large, 3),
        "raster_max_box_shift": max_box_shift,
        "raster_empty_mismatches": empty_mismatches,
        "raster_bad_samples": bad_samples,
    })
    if empty_mismatches > 0 or max_box_shift > 2 or max_mean > 18.0 or max_mean_large > 8.0:
        res.fail(
            f"wght={weight}: VF 与去重叠静态无 hint 光栅外观超出抗锯齿容差"
            f"（empty={empty_mismatches}, box_shift={max_box_shift}, mean={max_mean:.2f}, "
            f"mean_large={max_mean_large:.2f}, max_px={max_px}），样例：{bad_samples[:4]}"
        )
    return res


def check_vf_static_parity(
    vf_path: Path,
    static_path: Path,
    weight: int,
    *,
    region: str | None = None,
    italic: bool | None = None,
    spec: Spec | None = None,
    workers: int = 4,
    raster_ppems: Sequence[int] = RASTER_PARITY_PPEMS,
    check_raster: bool = True,
) -> CheckResult:
    """验证 VF 在命名字重与对应去重叠静态字体步进一致（≤1 unit）、外框粗检（≤15 unit）且无 hint 光栅外观一致；400 字重额外核对表级框架。"""
    res = CheckResult(ok=True)
    vf_hb = hb.Font(hb.Face(vf_path.read_bytes()))
    vf_hb.set_variations({"wght": float(weight)})
    st_hb = hb.Font(hb.Face(static_path.read_bytes()))

    with TTFont(vf_path, lazy=True) as fvf, TTFont(static_path, lazy=True) as fst:
        cm_vf = fvf.getBestCmap() or {}
        cm_st = fst.getBestCmap() or {}
        if set(cm_vf) != set(cm_st):
            res.fail(f"VF 与静态 cmap 码位不一致（VF={len(cm_vf)}, static={len(cm_st)}）")
            return res
        adv_diffs = 0
        max_diff = 0
        samples: list[tuple[str, int, int]] = []
        ext_diffs = 0
        max_ext_diff = 0
        ext_samples: list[tuple[str, int]] = []
        for cp in sorted(cm_vf):
            gid_vf = fvf.getGlyphID(cm_vf[cp])
            gid_st = fst.getGlyphID(cm_st[cp])
            w_vf = vf_hb.get_glyph_h_advance(gid_vf)
            w_st = st_hb.get_glyph_h_advance(gid_st)
            d = abs(w_vf - w_st)
            if d > 0:
                adv_diffs += 1
                max_diff = max(max_diff, d)
                if len(samples) < 8:
                    samples.append((hex(cp), w_vf, w_st))
            ea = vf_hb.get_glyph_extents(gid_vf)
            eb = st_hb.get_glyph_extents(gid_st)
            if (ea is None) != (eb is None):
                ext_diffs += 1
                max_ext_diff = max(max_ext_diff, 9999)
                if len(ext_samples) < 8:
                    ext_samples.append((hex(cp), 9999))
            elif ea is not None and eb is not None:
                ed = max(
                    abs(ea.x_bearing - eb.x_bearing),
                    abs(ea.y_bearing - eb.y_bearing),
                    abs(ea.width - eb.width),
                    abs(ea.height - eb.height),
                )
                if ed > 0:
                    ext_diffs += 1
                    max_ext_diff = max(max_ext_diff, ed)
                    if len(ext_samples) < 8:
                        ext_samples.append((hex(cp), ed))
        res.metrics.update({
            "advance_diffs": adv_diffs,
            "max_advance_diff": max_diff,
            "samples": samples,
            "extent_diffs": ext_diffs,
            "max_extent_diff": max_ext_diff,
            "extent_samples": ext_samples,
        })
        if max_diff > 1:
            res.fail(f"wght={weight}: VF 与静态存在 >1 unit 步进差（max={max_diff}），样例：{samples[:5]}")
        # 去重叠后二次曲线控制框允许最多 ~9 unit 重采样变化，此处保留 15 unit 粗检，精确外观由下方光栅比对守护
        if max_ext_diff > 15:
            res.fail(f"wght={weight}: VF 与静态存在 >15 unit 墨迹外框差（max={max_ext_diff}），样例：{ext_samples[:5]}")
        if weight == 400 and region is not None and italic is not None:
            sp = spec or load_spec()
            _check_table_framework_parity(
                fvf,
                fst,
                res,
                sp,
                region=region,
                italic=italic,
                is_vf=True,
                hinted=False,
                weight_spec=sp.weight("Regular"),
                subset_mode="full",
                mode="vf_vs_regular",
            )

    if check_raster:
        rast = check_unhinted_raster_parity(vf_path, static_path, weight, ppems=raster_ppems, workers=workers)
        res.metrics.update(rast.metrics)
        if not rast.ok:
            for err in rast.errors:
                res.fail(err)
    return res


def _scan_hint_disp_chunk(args: tuple[str, list[int], tuple[int, ...]]) -> dict[int, list[int]]:
    font_path, gids, ppems = args
    face = freetype.Face(font_path)
    hint_flags = freetype.FT_LOAD_NO_AUTOHINT | freetype.FT_LOAD_TARGET_NORMAL | freetype.FT_LOAD_NO_BITMAP
    noh_flags = freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_BITMAP
    out: dict[int, list[int]] = {gid: [0] * len(ppems) for gid in gids}
    for idx, ppem in enumerate(ppems):
        face.set_pixel_sizes(0, ppem)
        for gid in gids:
            face.load_glyph(gid, hint_flags)
            hy = [p[1] for p in face.glyph.outline.points]
            face.load_glyph(gid, noh_flags)
            uy = [p[1] for p in face.glyph.outline.points]
            if hy and len(hy) == len(uy):
                out[gid][idx] = max(abs(a - b) for a, b in zip(hy, uy))
    return out


def _scan_cjk_displacements_64(
    font_path: Path,
    ppems: tuple[int, ...],
    *,
    workers: int = 4,
    cache_dir: Path | None = None,
) -> dict[int, list[int]]:
    """扫描字体中全部 CJK 汉字码位在指定字号下 hint 相对未 hint 的最大竖向点位移（单位：1/64 px 整数）。"""
    st = font_path.stat()
    cache_path: Path | None = None
    if cache_dir is not None:
        p_tag = f"{ppems[0]}-{ppems[-1]}-{len(ppems)}"
        cache_path = cache_dir / f"{font_path.stem}.{st.st_size}.{st.st_mtime_ns}.p{p_tag}.json.gz"
        if cache_path.is_file():
            try:
                with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if cached.get("ppems") == list(ppems):
                    return {int(k): v for k, v in cached["by_cp"].items()}
            except (OSError, ValueError, KeyError):
                pass

    face = freetype.Face(str(font_path))
    cp_to_gid: dict[int, int] = {}
    cp, gid = face.get_first_char()
    while gid:
        if is_ideograph(cp):
            cp_to_gid[cp] = gid
        cp, gid = face.get_next_char(cp, gid)

    unique_gids = sorted(set(cp_to_gid.values()))
    if not unique_gids:
        return {}

    if workers <= 1 or len(unique_gids) < 256:
        gid_map = _scan_hint_disp_chunk((str(font_path), unique_gids, ppems))
    else:
        n_chunks = min(workers * 2, max(1, len(unique_gids) // 64))
        chunks = [unique_gids[i::n_chunks] for i in range(n_chunks)]
        gid_map = {}
        with ProcessPoolExecutor(max_workers=min(workers, n_chunks)) as ex:
            for part in ex.map(_scan_hint_disp_chunk, [(str(font_path), ch, ppems) for ch in chunks]):
                gid_map.update(part)

    by_cp = {cp_k: gid_map[g] for cp_k, g in cp_to_gid.items()}
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"ppems": list(ppems), "by_cp": by_cp}, separators=(",", ":")).encode("utf-8")
        tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
        os.replace(tmp, cache_path)
    return by_cp


def check_hint_displacement(
    hinted_path: Path,
    upstream_hinted_path: Path,
    *,
    ppems: Sequence[int] = HINT_DISP_PPEMS,
    is_heavy: bool = False,
    cache_dir: Path | None = None,
    workers: int = 4,
) -> CheckResult:
    """常设 hint 位移门：逐字号统计 12–48 px 全部 CJK 汉字 hint 相对未 hint 的最大竖向点位移，并与上游同码位比对。"""
    res = CheckResult(ok=True)
    if not upstream_hinted_path.is_file():
        res.fail(f"缺少上游 hinted 参照字体，无法执行 hint 位移门：{upstream_hinted_path}")
        return res

    target_ppems = tuple(int(p) for p in ppems)
    cand_map = _scan_cjk_displacements_64(hinted_path, target_ppems, workers=workers, cache_dir=None)
    ref_map = _scan_cjk_displacements_64(upstream_hinted_path, target_ppems, workers=workers, cache_dir=cache_dir)
    common_cps = sorted(set(cand_map) & set(ref_map))
    if not common_cps:
        res.fail("候选字体与上游 hinted 参照没有共有 CJK 码位，无法执行 hint 位移门")
        return res

    is_small_sample = len(common_cps) < 200
    ppem_stats: dict[int, dict[str, Any]] = {}
    violations: list[str] = []
    worst_gt2_ratio = 0.0
    worst_gt2_ppem = target_ppems[0]
    worst_regressed: list[dict[str, Any]] = []

    for idx, ppem in enumerate(target_ppems):
        cand_gt2 = 0
        ref_gt2 = 0
        cand_max_64 = 0
        cand_2nd_64 = 0
        ref_max_64 = 0
        regressed: list[tuple[int, int, int]] = []
        for cp in common_cps:
            cv = cand_map[cp][idx]
            rv = ref_map[cp][idx]
            if cv > 128:
                cand_gt2 += 1
            if rv > 128:
                ref_gt2 += 1
            if cv >= cand_max_64:
                cand_2nd_64 = cand_max_64
                cand_max_64 = cv
            elif cv > cand_2nd_64:
                cand_2nd_64 = cv
            if rv > ref_max_64:
                ref_max_64 = rv
            # 同码位退化：候选 > 2.0 px 且比参照多偏 > 1.0 px
            if cv > 128 and (cv - rv) > 64:
                regressed.append((cv - rv, cp, cv))

        cand_max = round(cand_max_64 / 64.0, 2)
        cand_2nd = round(cand_2nd_64 / 64.0, 2)
        ref_max = round(ref_max_64 / 64.0, 2)
        ppem_stats[ppem] = {
            "cand_gt2": cand_gt2,
            "ref_gt2": ref_gt2,
            "cand_max": cand_max,
            "cand_2nd": cand_2nd,
            "ref_max": ref_max,
            "regressed": len(regressed),
        }
        ratio = cand_gt2 / max(1, ref_gt2)
        if ratio > worst_gt2_ratio:
            worst_gt2_ratio = ratio
            worst_gt2_ppem = ppem

        if is_small_sample:
            gt2_limit = ref_gt2
            reg_limit = 0
            top2_margin = 1.5
            top1_margin = 1.5
            check_detail = True
        else:
            if is_heavy:
                # 基于全 6 地区 Heavy(0.097) 对照本版 Bold 的实测分布定限
                gt2_limit = max(int(ref_gt2 * 2.5) + 90, ref_gt2 + 310)
                reg_limit = 210
                top2_margin = 2.5
                top1_margin = 3.0
            else:
                # 基于全 6 地区 × 5 常规字重对照上游 1.0.43 的实测分布收紧定限
                gt2_limit = max(int(ref_gt2 * 1.25) + 25, ref_gt2 + 48)
                reg_limit = 35
                top2_margin = 1.5
                top1_margin = 3.0
            check_detail = ppem >= 20

        if cand_gt2 > gt2_limit:
            violations.append(f"{ppem}px(>2px {cand_gt2} vs 参照 {ref_gt2}, 上限 {gt2_limit})")
        if check_detail and len(regressed) > reg_limit:
            violations.append(f"{ppem}px(同码位退化 {len(regressed)} > {reg_limit})")
        if check_detail and (cand_max > ref_max + top1_margin or (not is_small_sample and cand_2nd > ref_max + top2_margin)):
            violations.append(f"{ppem}px(最大/次大位移 {cand_max:.2f}/{cand_2nd:.2f} vs 参照 {ref_max:.2f})")

        if regressed and (cand_gt2 > gt2_limit or (check_detail and len(regressed) > reg_limit)):
            regressed.sort(reverse=True)
            for _diff, cp, cv in regressed[:3]:
                if len(worst_regressed) < 12:
                    worst_regressed.append({
                        "ppem": ppem,
                        "char": chr(cp),
                        "cp": f"U+{cp:04X}",
                        "cand_px": round(cv / 64.0, 2),
                        "ref_px": round(ref_map[cp][idx] / 64.0, 2),
                    })

    res.metrics.update({
        "hint_disp_cjk_count": len(common_cps),
        "hint_disp_ppems": f"{target_ppems[0]}-{target_ppems[-1]}",
        "hint_disp_worst_ppem": worst_gt2_ppem,
        "hint_disp_20_28_32": {
            p: ppem_stats[p] for p in (20, 24, 28, 32) if p in ppem_stats
        },
        "hint_disp_violations": violations[:12],
        "hint_disp_regressed_samples": worst_regressed,
    })

    # 哨兵字逐字号核对：走、歌、男、美、哥、虎、虚
    sentinel_violations: list[dict[str, Any]] = []
    sent_margin_64 = 116 if is_heavy else HINT_DISP_SENTINEL_MARGIN_64
    for ch in HINT_DISP_SENTINELS:
        scp = ord(ch)
        if scp not in cand_map or scp not in ref_map:
            continue
        is_zou = ch == "走"
        for idx, ppem in enumerate(target_ppems):
            cv = cand_map[scp][idx]
            rv = ref_map[scp][idx]
            margin_64 = sent_margin_64 if is_heavy else (HINT_DISP_ZOU_MARGIN_64 if is_zou else sent_margin_64)
            too_much_rel = (cv - rv) > margin_64
            too_much_abs = is_zou and cv > HINT_DISP_ZOU_ABS_MAX_64
            if too_much_rel or too_much_abs:
                sentinel_violations.append({
                    "char": ch,
                    "ppem": ppem,
                    "cand_px": round(cv / 64.0, 2),
                    "ref_px": round(rv / 64.0, 2),
                    "delta_px": round((cv - rv) / 64.0, 2),
                })
    res.metrics["hint_disp_sentinel_violations"] = sentinel_violations[:20]

    if violations:
        res.fail(
            f"CJK hint 竖向位移明显超过参照（共 {len(violations)} 项超限）："
            f"{'；'.join(violations[:6])}；样例：{worst_regressed[:4]}"
        )
    if sentinel_violations:
        sample_txt = "；".join(
            f"{v['char']}@{v['ppem']}px({v['cand_px']} vs 参照 {v['ref_px']})"
            for v in sentinel_violations[:6]
        )
        res.fail(
            f"哨兵字 hint 竖向位移超限（共 {len(sentinel_violations)} 处）：{sample_txt}"
        )
    return res
