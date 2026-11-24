"""由 VF 单真相源直接实例化 72 个 unhinted 静态 TTF，并写入静态元数据、STAT 与 post format 3。"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
from pathlib import Path
import time
from typing import Any

from fontTools.otlLib.builder import buildStatTable
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

from ..spec import Spec, WeightSpec, load_spec
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
    os2.recalcAvgCharWidth(font)

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


def instantiate_static_from_vf(
    vf_path: Path,
    out_path: Path,
    *,
    region: str,
    weight_name: str,
    italic: bool,
    spec: Spec | None = None,
) -> dict[str, Any]:
    spec = spec or load_spec()
    r = spec.region(region)
    w = spec.weight(weight_name)
    t0 = time.perf_counter()

    with TTFont(vf_path, recalcTimestamp=False) as vf:
        font = instantiateVariableFont(vf, {"wght": float(w.value)}, inplace=False, optimize=True)
    try:
        for tag in VARIABLE_TABLES:
            if tag in font:
                del font[tag]
        update_static_os2_and_post(font, r.code, w, italic, spec)
        name_rep = update_static_names(font, r.code, w, italic, hinted=False, spec=spec)
        rebuild_static_stat(font, w, italic)
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
    finally:
        font.close()
    os.replace(tmp, out_path)
    return {
        "region": r.code,
        "weight": w.name,
        "weight_value": w.value,
        "italic": italic,
        "filename": out_path.name,
        "glyph_count": glyph_count,
        "size_bytes": out_path.stat().st_size,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        **name_rep,
    }


def _worker_instantiate_style(args: tuple[str, str, list[str], bool, str]) -> list[dict[str, Any]]:
    vf_path_str, region, weight_names, italic, out_dir_str = args
    spec = load_spec()
    vf_path = Path(vf_path_str)
    out_dir = Path(out_dir_str)
    results: list[dict[str, Any]] = []
    # 每个进程对同一 VF 只解析一次磁盘，循环实例化全部目标字重，内存极省且速度快
    with TTFont(vf_path, recalcTimestamp=False) as vf:
        for w_name in weight_names:
            w = spec.weight(w_name)
            t0 = time.perf_counter()
            font = instantiateVariableFont(vf, {"wght": float(w.value)}, inplace=False, optimize=True)
            out_path = out_dir / spec.static_filename(region, w.name, italic)
            try:
                for tag in VARIABLE_TABLES:
                    if tag in font:
                        del font[tag]
                update_static_os2_and_post(font, region, w, italic, spec)
                name_rep = update_static_names(font, region, w, italic, hinted=False, spec=spec)
                rebuild_static_stat(font, w, italic)
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
            finally:
                font.close()
            os.replace(tmp, out_path)
            results.append({
                "region": region,
                "weight": w.name,
                "weight_value": w.value,
                "italic": italic,
                "filename": out_path.name,
                "glyph_count": glyph_count,
                "size_bytes": out_path.stat().st_size,
                "elapsed_sec": round(time.perf_counter() - t0, 2),
                **name_rep,
            })
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
    """并行由 VF 实例化所选地区/风格/字重的 unhinted 静态字体。"""
    spec = spec or load_spec()
    vf_dir = vf_root.resolve() / "variable"
    tasks: list[tuple[str, str, list[str], bool, str]] = []
    for region in regions:
        out_dir = spec.static_dir(out_root.resolve(), region, hinted=False)
        for style in styles:
            italic = style == "italic"
            vf_path = vf_dir / spec.variable_filename(region, italic)
            if not vf_path.is_file():
                raise FileNotFoundError(f"缺少源 VF：{vf_path}")
            tasks.append((str(vf_path), region, list(weights), italic, str(out_dir)))

    t0 = time.perf_counter()
    all_results: list[dict[str, Any]] = []
    if jobs <= 1 or len(tasks) <= 1:
        for item in tasks:
            all_results.extend(_worker_instantiate_style(item))
    else:
        pool = ProcessPoolExecutor(max_workers=min(jobs, len(tasks)))
        try:
            futs = {pool.submit(_worker_instantiate_style, item): item for item in tasks}
            for fut in as_completed(futs):
                batch = fut.result()
                all_results.extend(batch)
                print(f"[unhinted] {batch[0]['region']}{'-It' if batch[0]['italic'] else ''}: {len(batch)} weights done", flush=True)
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
