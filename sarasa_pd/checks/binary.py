"""二进制级检查：OTS 校验、FreeType 严格执行扫描、hinted/unhinted 与 VF/静态一致性。"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import subprocess
from typing import Any, Sequence

import freetype
from fontTools.ttLib import TTFont
import uharfbuzz as hb

from .rules import CheckResult

# 日常快速检查采样的字号列表：9–20 px 逐字号全覆 + 24–128 px 每 4 px（共 39 字号，约全量 1/3 耗时）
FT_FAST_PPEMS: tuple[int, ...] = tuple([*range(9, 21), *range(24, 129, 4)])


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
    """在灰阶严格模式下扫描全部字形 × 目标字号集合，要求 0 执行中止（A13）。

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


def check_vf_static_parity(vf_path: Path, static_path: Path, weight: int) -> CheckResult:
    """验证 VF 在命名字重与对应静态字体逐字形选字与步进一致（允许临界 1 unit 取整）。"""
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
        res.metrics.update({"advance_diffs": adv_diffs, "max_advance_diff": max_diff, "samples": samples})
        if max_diff > 1:
            res.fail(f"wght={weight}: VF 与静态存在 >1 unit 步进差（max={max_diff}），样例：{samples[:5]}")
    return res
