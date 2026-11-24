"""VF 单真相源构建管线：显式有序阶段、前置断言、无全局状态、支持 --subset smoke 与可控并行。"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from fontTools.ttLib import TTFont

from ..env import repo_root
from ..sources import SourceStore
from ..spec import Spec, load_spec
from ..subset import smoke_codepoints
from .base import load_base
from .chws import add_chws_to_file
from .fixes import (
    fix_classical_ideograph_fullwidth,
    fix_ideograph_gdef_classes,
    fix_os2_unicode_range_bit57,
    prune_unreachable_glyphs,
    recalc_os2_max_context,
)
from .inter import LoadedInter, append_inter_glyphs, load_inter
from .layout import (
    drop_nonfinal_source_han_gsub,
    drop_sarasa_width_features,
    finalize_layout,
    import_inter_layout,
    prune_locl_to_reference,
)
from .layout_utils import prune_unreferenced_lookups, remove_empty_lookups
from .metrics import (
    normalize_vertical_metrics,
    optimize_vf_gvar,
    rebuild_vf_hvar_from_gvar,
    rebuild_vf_mvar,
    recalculate_head_bounds_and_lsb,
)
from .names import (
    apply_deterministic_head,
    normalize_nonhinting_raster_metadata,
    prune_unreferenced_variable_names,
    rebuild_vf_stat,
    remove_mac_name_records,
    sync_sarasa_os2_fields,
    update_vf_names,
    update_vf_style_and_os2,
)
from .punctuation import apply_source_han_punctuation_layout


@dataclass
class VFBuildContext:
    region: str
    italic: bool
    subset_mode: str
    work_root: Path
    out_path: Path
    spec: Spec
    source_paths: dict[str, Path] = field(default_factory=dict)
    ref_path: Path | None = None
    allowed_unicodes: set[int] = field(default_factory=set)
    loaded_inter: LoadedInter | None = None
    reference: TTFont | None = None
    font: TTFont | None = None
    report: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        if self.loaded_inter is not None:
            self.loaded_inter.close()
            self.loaded_inter = None
        if self.reference is not None:
            self.reference.close()
            self.reference = None
        if self.font is not None:
            self.font.close()
            self.font = None


def _stage_load_sources(ctx: VFBuildContext) -> None:
    store = SourceStore(ctx.work_root, ctx.spec)
    ctx.source_paths = store.ensure_vf_sources([ctx.region])
    store.ensure_sarasa_reference([ctx.region], hinted=False)
    ctx.ref_path = store.sarasa_ref_path(ctx.region, "Regular", False, hinted=False)
    ctx.reference = TTFont(ctx.ref_path, lazy=True)
    ref_cps = set((ctx.reference.getBestCmap() or {}).keys())
    if ctx.subset_mode == "smoke":
        ctx.allowed_unicodes = ref_cps & smoke_codepoints(ctx.spec)
    else:
        ctx.allowed_unicodes = ref_cps
    inter_key = "inter_italic" if ctx.italic else "inter_upright"
    ctx.loaded_inter = load_inter(ctx.source_paths[inter_key], ctx.spec)
    ctx.report["allowed_unicodes"] = len(ctx.allowed_unicodes)


def _stage_prepare_base(ctx: VFBuildContext) -> None:
    assert ctx.loaded_inter is not None, "前置条件：必须先载入 Inter"
    r = ctx.spec.region(ctx.region)
    shs_path = ctx.source_paths[f"source_han_{r.code}"]
    cl_path = ctx.source_paths.get(f"classical_{r.code}") if r.classical else None
    inter_cps = set((ctx.loaded_inter.font.getBestCmap() or {}).keys())
    font, rep = load_base(
        shs_path,
        r.code,
        ctx.italic,
        ctx.allowed_unicodes,
        inter_cps,
        classical_vf=cl_path,
        spec=ctx.spec,
    )
    ctx.font = font
    ctx.report["base"] = rep


def _stage_merge_inter_glyphs(ctx: VFBuildContext) -> None:
    assert ctx.font is not None and ctx.loaded_inter is not None, "前置条件：缺少基底或 Inter"
    ctx.report["inter_glyphs"] = append_inter_glyphs(ctx.font, ctx.loaded_inter, ctx.allowed_unicodes, ctx.spec)


def _stage_merge_layout(ctx: VFBuildContext) -> None:
    assert ctx.font is not None and ctx.loaded_inter is not None and ctx.reference is not None
    rep: dict[str, Any] = {}
    rep.update(drop_sarasa_width_features(ctx.font, ctx.spec))
    rep.update(prune_locl_to_reference(ctx.font, ctx.reference, ctx.spec))
    rep["source_han_nonfinal_gsub_dropped"] = drop_nonfinal_source_han_gsub(ctx.font, ctx.spec)
    rep.update(import_inter_layout(ctx.font, ctx.loaded_inter, ctx.spec, italic=ctx.italic))
    ctx.report["layout"] = rep
    # Inter 已完全并入，提前释放内存
    ctx.loaded_inter.close()
    ctx.loaded_inter = None


def _stage_apply_punctuation(ctx: VFBuildContext) -> None:
    assert ctx.font is not None
    rep = apply_source_han_punctuation_layout(ctx.font, ctx.region, ctx.spec)
    # 先移除不可达 lookup 再清理孤立字形，防止丢弃的 jp78/fwid 等旧字形叠加 Inter 后超出 65535 GID 上限
    rep["prechws_empty_lookups_removed"] = remove_empty_lookups(ctx.font)
    rep["prechws_unreferenced_lookups_removed"] = prune_unreferenced_lookups(ctx.font)
    rep.update(prune_unreachable_glyphs(ctx.font))
    ctx.report["punctuation"] = rep


def _stage_apply_fixes_and_metadata(ctx: VFBuildContext) -> None:
    assert ctx.font is not None and ctx.reference is not None
    fixes_rep: dict[str, Any] = {}
    fixes_rep.update(fix_classical_ideograph_fullwidth(ctx.font, ctx.region))
    fixes_rep.update(fix_ideograph_gdef_classes(ctx.font))
    fixes_rep.update(fix_os2_unicode_range_bit57(ctx.font))
    ctx.report["fixes"] = fixes_rep

    sync_sarasa_os2_fields(ctx.font, ctx.reference)
    ctx.reference.close()
    ctx.reference = None

    meta_rep: dict[str, Any] = {}
    meta_rep.update(update_vf_names(ctx.font, ctx.region, ctx.italic, ctx.spec))
    update_vf_style_and_os2(ctx.font, ctx.region, ctx.italic, ctx.spec)
    rebuild_vf_stat(ctx.font, ctx.italic, ctx.spec)
    meta_rep.update(normalize_vertical_metrics(ctx.font))
    meta_rep.update(rebuild_vf_mvar(ctx.font, ctx.spec))
    meta_rep.update(normalize_nonhinting_raster_metadata(ctx.font, ctx.spec))
    meta_rep.update(apply_deterministic_head(ctx.font, ctx.spec))
    ctx.report["metadata"] = meta_rep


def _stage_apply_chws(ctx: VFBuildContext) -> None:
    assert ctx.font is not None
    ctx.out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = ctx.out_path.with_suffix(".prechws.ttf")
    ctx.font.save(tmp_path, reorderTables=True)
    ctx.font.close()
    ctx.font = None

    ctx.report["chws"] = add_chws_to_file(tmp_path, ctx.spec)
    ctx.font = TTFont(tmp_path, recalcTimestamp=False)
    # 载入后保留 tmp_path 至最终原子替换前删除
    ctx._tmp_prechws = tmp_path  # type: ignore[attr-defined]


def _stage_finalize_and_save(ctx: VFBuildContext) -> None:
    assert ctx.font is not None
    fin: dict[str, Any] = {}
    fin.update(remove_mac_name_records(ctx.font))
    fin.update(prune_unreferenced_variable_names(ctx.font))
    fin.update(finalize_layout(ctx.font))
    fin.update(fix_ideograph_gdef_classes(ctx.font))
    fin.update(prune_unreachable_glyphs(ctx.font))
    fin.update(recalculate_head_bounds_and_lsb(ctx.font))
    fin.update(optimize_vf_gvar(ctx.font, ctx.italic))
    fin.update(rebuild_vf_hvar_from_gvar(ctx.font))
    fin.update(recalc_os2_max_context(ctx.font))
    fin.update(fix_os2_unicode_range_bit57(ctx.font))
    fin.update(apply_deterministic_head(ctx.font, ctx.spec))
    fin["glyph_count"] = len(ctx.font.getGlyphOrder())
    fin["codepoint_count"] = len(ctx.font.getBestCmap() or {})
    uvs_tables = [t for t in ctx.font["cmap"].tables if t.format == 14]
    fin["uvs_entries"] = sum(len(v) for t in uvs_tables for v in t.uvsDict.values())
    ctx.report["final"] = fin

    tmp_final = ctx.out_path.with_suffix(".writing.ttf")
    ctx.font.save(tmp_final, reorderTables=True)
    ctx.font.close()
    ctx.font = None
    os.replace(tmp_final, ctx.out_path)
    pre_tmp = getattr(ctx, "_tmp_prechws", None)
    if pre_tmp is not None and Path(pre_tmp).exists():
        Path(pre_tmp).unlink()


# 显式阶段顺序表：修改管线只需审视这一张表
VF_STAGES: tuple[tuple[str, Callable[[VFBuildContext], None]], ...] = (
    ("load_sources", _stage_load_sources),
    ("prepare_base", _stage_prepare_base),
    ("merge_inter_glyphs", _stage_merge_inter_glyphs),
    ("merge_layout", _stage_merge_layout),
    ("apply_punctuation", _stage_apply_punctuation),
    ("apply_fixes_and_metadata", _stage_apply_fixes_and_metadata),
    ("apply_chws", _stage_apply_chws),
    ("finalize_and_save", _stage_finalize_and_save),
)


def build_one_vf(
    region: str,
    italic: bool,
    out_dir: Path,
    *,
    work_root: Path | None = None,
    subset_mode: str = "full",
    spec: Spec | None = None,
) -> dict[str, Any]:
    spec = spec or load_spec()
    r = spec.region(region)
    wroot = (work_root or (repo_root() / ".build-cache" / "work")).resolve()
    out_path = out_dir.resolve() / spec.variable_filename(r.code, italic)
    ctx = VFBuildContext(
        region=r.code,
        italic=italic,
        subset_mode=subset_mode,
        work_root=wroot,
        out_path=out_path,
        spec=spec,
    )
    t_all = time.perf_counter()
    stage_timings: dict[str, float] = {}
    try:
        for name, fn in VF_STAGES:
            t0 = time.perf_counter()
            fn(ctx)
            dt = round(time.perf_counter() - t0, 2)
            stage_timings[name] = dt
            print(f"  [{r.code}{'-It' if italic else ''}] {name}: {dt}s", flush=True)
    finally:
        ctx.close()
    return {
        "region": r.code,
        "italic": italic,
        "subset": subset_mode,
        "filename": out_path.name,
        "size_bytes": out_path.stat().st_size,
        "elapsed_sec": round(time.perf_counter() - t_all, 2),
        "stage_timings": stage_timings,
        **ctx.report,
    }


def _worker_build_one(args: tuple[str, bool, str, str, str]) -> dict[str, Any]:
    region, italic, out_dir, work_root, subset_mode = args
    return build_one_vf(region, italic, Path(out_dir), work_root=Path(work_root), subset_mode=subset_mode)


def build_vfs(
    regions: list[str],
    styles: list[str],
    out_root: Path,
    *,
    work_root: Path | None = None,
    subset_mode: str = "full",
    jobs: int = 2,
    spec: Spec | None = None,
) -> dict[str, Any]:
    """并行构建所选地区与正斜体的 VF，支持 Ctrl+C 干净终止（修 C17）。"""
    spec = spec or load_spec()
    wroot = (work_root or (repo_root() / ".build-cache" / "work")).resolve()
    vf_dir = out_root.resolve() / "variable"
    vf_dir.mkdir(parents=True, exist_ok=True)

    # 先单进程预检固定来源，避免多进程竞争解包
    store = SourceStore(wroot, spec)
    store.ensure_vf_sources(regions)
    store.ensure_sarasa_reference(regions, hinted=False)

    tasks = [
        (r, s == "italic", str(vf_dir), str(wroot), subset_mode)
        for r in regions
        for s in styles
    ]
    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    if jobs <= 1 or len(tasks) <= 1:
        for item in tasks:
            res = _worker_build_one(item)
            results.append(res)
            print(f"[vf] {res['filename']} ({res['final']['glyph_count']} glyphs, {res['elapsed_sec']}s)", flush=True)
    else:
        pool = ProcessPoolExecutor(max_workers=min(jobs, len(tasks)))
        try:
            futures = {pool.submit(_worker_build_one, item): item for item in tasks}
            for fut in as_completed(futures):
                res = fut.result()
                results.append(res)
                print(f"[vf] {res['filename']} ({res['final']['glyph_count']} glyphs, {res['elapsed_sec']}s)", flush=True)
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)

    results.sort(key=lambda r: (spec.region_order.index(r["region"]), r["italic"]))
    summary = {
        "stage": "vf",
        "subset": subset_mode,
        "count": len(results),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "outputs": results,
    }
    (out_root / "vf-build-report.json").write_bytes(
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return summary
