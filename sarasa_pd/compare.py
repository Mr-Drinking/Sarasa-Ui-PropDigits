"""轻量字体对照工具：比较码位/UVS、步进、墨迹、代表语料排版、特性路由与度量表。"""
from __future__ import annotations

from collections import defaultdict
import concurrent.futures
import json
from pathlib import Path
import time
from typing import Any

from fontTools.ttLib import TTFont
import uharfbuzz as hb

from .probes import DEFAULT_PROBES, Probe
from .spec import Spec, codepoint_category, load_spec


def _open_hb_font(path: Path, wght: int | None) -> tuple[hb.Font, int]:
    blob = hb.Blob.from_file_path(str(path))
    face = hb.Face(blob)
    font = hb.Font(face)
    upem = face.upem
    font.scale = (upem, upem)
    if wght is not None:
        font.set_variations({"wght": float(wght)})
    return font, upem


def _extract_uvs(tt: TTFont) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """返回 (全部 (base_cp, selector), 非默认 (base_cp, selector))。"""
    all_pairs: set[tuple[int, int]] = set()
    non_default: set[tuple[int, int]] = set()
    if "cmap" not in tt:
        return all_pairs, non_default
    for sub in tt["cmap"].tables:
        if sub.format != 14:
            continue
        for sel, entries in sub.uvsDict.items():
            for base_cp, glyph_name in entries:
                pair = (base_cp, sel)
                all_pairs.add(pair)
                if glyph_name is not None:
                    non_default.add(pair)
    return all_pairs, non_default


def _extract_routes(tt: TTFont, tag: str) -> dict[str, list[str]]:
    if tag not in tt:
        return {}
    table = tt[tag].table
    if not table.ScriptList or not table.FeatureList:
        return {}
    records = table.FeatureList.FeatureRecord
    out: dict[str, list[str]] = {}
    for srec in table.ScriptList.ScriptRecord:
        script = srec.Script
        systems = [("dflt", script.DefaultLangSys)] if script.DefaultLangSys is not None else []
        systems += [(lrec.LangSysTag.strip(), lrec.LangSys) for lrec in script.LangSysRecord]
        for lang, sys_obj in systems:
            tags = {records[idx].FeatureTag for idx in sys_obj.FeatureIndex}
            if sys_obj.ReqFeatureIndex != 0xFFFF:
                tags.add(records[sys_obj.ReqFeatureIndex].FeatureTag + "*")
            out[f"{srec.ScriptTag.strip()}/{lang}"] = sorted(tags)
    return out


def _extract_metrics_snapshot(tt: TTFont) -> dict[str, Any]:
    head = tt["head"]
    hhea = tt["hhea"]
    os2 = tt["OS/2"]
    post = tt["post"]
    name_tbl = tt["name"]

    def get_en(nid: int) -> str | None:
        rec = name_tbl.getName(nid, 3, 1, 0x0409)
        return rec.toUnicode() if rec is not None else None

    snap: dict[str, Any] = {
        "glyph_count": len(tt.getGlyphOrder()),
        "tables": sorted(t for t in tt.keys() if t != "GlyphOrder"),
        "head": {
            "unitsPerEm": head.unitsPerEm,
            "flags": head.flags,
            "macStyle": head.macStyle,
            "bounds": [head.xMin, head.yMin, head.xMax, head.yMax],
        },
        "hhea": {
            "ascent": hhea.ascent,
            "descent": hhea.descent,
            "lineGap": hhea.lineGap,
        },
        "OS/2": {
            "usWeightClass": os2.usWeightClass,
            "sTypoAscender": os2.sTypoAscender,
            "sTypoDescender": os2.sTypoDescender,
            "sTypoLineGap": os2.sTypoLineGap,
            "usWinAscent": os2.usWinAscent,
            "usWinDescent": os2.usWinDescent,
            "ulCodePageRange1": f"0x{os2.ulCodePageRange1:08X}",
            "bit57_non_plane0": bool(os2.ulUnicodeRange2 & (1 << (57 - 32))),
            "usMaxContext": os2.usMaxContext,
            "achVendID": os2.achVendID,
            "yStrikeoutSize": os2.yStrikeoutSize,
            "bWeight": os2.panose.bWeight,
        },
        "post": {
            "formatType": post.formatType,
            "italicAngle": post.italicAngle,
            "underlinePosition": post.underlinePosition,
            "underlineThickness": post.underlineThickness,
        },
        "name_en": {str(nid): get_en(nid) for nid in (0, 1, 2, 4, 5, 6, 13, 14, 16, 17)},
    }
    if "vhea" in tt:
        vhea = tt["vhea"]
        snap["vhea"] = {"ascent": vhea.ascent, "descent": vhea.descent, "lineGap": vhea.lineGap}
    return snap


def _shape_probe(hb_font: hb.Font, probe: Probe) -> list[dict[str, Any]]:
    buf = hb.Buffer()
    buf.add_str(probe.text)
    buf.direction = probe.direction
    if probe.script:
        buf.script = probe.script
    if probe.language:
        buf.language = probe.language
    buf.guess_segment_properties()
    feats = {k: v for k, v in probe.features}
    hb.shape(hb_font, buf, feats)
    items: list[dict[str, Any]] = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        ext = hb_font.get_glyph_extents(info.codepoint)
        box = (
            [ext.x_bearing, ext.y_bearing, ext.width, ext.height]
            if ext is not None
            else None
        )
        items.append({
            "gid": info.codepoint,
            "cluster": info.cluster,
            "xa": pos.x_advance,
            "ya": pos.y_advance,
            "xo": pos.x_offset,
            "yo": pos.y_offset,
            "box": box,
        })
    return items


def _probe_diff(cand: list[dict[str, Any]], ref: list[dict[str, Any]], tol: int = 1) -> str | None:
    if len(cand) != len(ref):
        return f"字形数不同（候选 {len(cand)} vs 参照 {len(ref)}）"
    reasons: list[str] = []
    for idx, (a, b) in enumerate(zip(cand, ref)):
        if a["cluster"] != b["cluster"]:
            reasons.append(f"#{idx} cluster {a['cluster']}!={b['cluster']}")
            break
        for field in ("xa", "ya", "xo", "yo"):
            if abs(a[field] - b[field]) > tol:
                reasons.append(f"#{idx} {field} {a[field]} vs {b[field]}")
                break
        if reasons:
            break
        ba, bb = a["box"], b["box"]
        if (ba is None) != (bb is None):
            reasons.append(f"#{idx} 墨迹有无不同")
            break
        if ba is not None and bb is not None:
            max_d = max(abs(x - y) for x, y in zip(ba, bb))
            if max_d > max(tol, 2):
                reasons.append(f"#{idx} 墨迹差 {max_d}（{ba} vs {bb}）")
                break
    return "; ".join(reasons) if reasons else None


def _fmt_cp(cp: int) -> str:
    ch = chr(cp) if 0x20 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF) else ""
    return f"U+{cp:04X}({ch})" if ch else f"U+{cp:04X}"


def compare_font_pair(
    cand_path: Path,
    ref_path: Path,
    *,
    label: str,
    cand_wght: int | None = None,
    ref_wght: int | None = None,
    probes: tuple[Probe, ...] = DEFAULT_PROBES,
    sample_limit: int = 12,
) -> dict[str, Any]:
    """对照单个候选字体与参照字体，返回结构化差异字典。"""
    t0 = time.perf_counter()
    with TTFont(cand_path, lazy=True) as cand_tt, TTFont(ref_path, lazy=True) as ref_tt:
        cand_cmap = cand_tt.getBestCmap() or {}
        ref_cmap = ref_tt.getBestCmap() or {}
        cand_uvs, cand_uvs_nd = _extract_uvs(cand_tt)
        ref_uvs, ref_uvs_nd = _extract_uvs(ref_tt)
        cand_routes = {"GSUB": _extract_routes(cand_tt, "GSUB"), "GPOS": _extract_routes(cand_tt, "GPOS")}
        ref_routes = {"GSUB": _extract_routes(ref_tt, "GSUB"), "GPOS": _extract_routes(ref_tt, "GPOS")}
        cand_meta = _extract_metrics_snapshot(cand_tt)
        ref_meta = _extract_metrics_snapshot(ref_tt)

    cand_cps = set(cand_cmap)
    ref_cps = set(ref_cmap)
    only_cand_cps = sorted(cand_cps - ref_cps)
    only_ref_cps = sorted(ref_cps - cand_cps)
    shared_cps = sorted(cand_cps & ref_cps)

    only_cand_uvs = sorted(cand_uvs - ref_uvs)
    only_ref_uvs = sorted(ref_uvs - cand_uvs)

    hb_cand, _ = _open_hb_font(cand_path, cand_wght)
    hb_ref, _ = _open_hb_font(ref_path, ref_wght)

    adv_by_cat: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "max_diff": 0, "samples": []})
    ink_gt2 = {"count": 0, "max_diff": 0, "samples": []}
    ink_1_2 = 0

    for cp in shared_cps:
        gid_a = hb_cand.get_nominal_glyph(cp)
        gid_b = hb_ref.get_nominal_glyph(cp)
        if gid_a is None or gid_b is None:
            continue
        adv_a = hb_cand.get_glyph_h_advance(gid_a)
        adv_b = hb_ref.get_glyph_h_advance(gid_b)
        if adv_a != adv_b:
            cat = codepoint_category(cp)
            bucket = adv_by_cat[cat]
            diff = abs(adv_a - adv_b)
            bucket["count"] += 1
            if diff > bucket["max_diff"]:
                bucket["max_diff"] = diff
            if len(bucket["samples"]) < sample_limit:
                bucket["samples"].append(f"{_fmt_cp(cp)}:{adv_a}->{adv_b}")

        ext_a = hb_cand.get_glyph_extents(gid_a)
        ext_b = hb_ref.get_glyph_extents(gid_b)
        if ext_a is not None and ext_b is not None:
            d = max(
                abs(ext_a.x_bearing - ext_b.x_bearing),
                abs(ext_a.y_bearing - ext_b.y_bearing),
                abs(ext_a.width - ext_b.width),
                abs(ext_a.height - ext_b.height),
            )
            if d > 2:
                ink_gt2["count"] += 1
                if d > ink_gt2["max_diff"]:
                    ink_gt2["max_diff"] = d
                if len(ink_gt2["samples"]) < sample_limit:
                    ink_gt2["samples"].append(f"{_fmt_cp(cp)}:Δ{d}")
            elif d > 0:
                ink_1_2 += 1

    probe_diffs: list[dict[str, str]] = []
    for probe in probes:
        res_a = _shape_probe(hb_cand, probe)
        res_b = _shape_probe(hb_ref, probe)
        reason = _probe_diff(res_a, res_b)
        if reason:
            probe_diffs.append({"probe": probe.name, "text": probe.text, "reason": reason})

    route_diffs: dict[str, dict[str, Any]] = {}
    for tbl in ("GSUB", "GPOS"):
        ra, rb = cand_routes[tbl], ref_routes[tbl]
        only_a = sorted(set(ra) - set(rb))
        only_b = sorted(set(rb) - set(ra))
        changed = {}
        for key in sorted(set(ra) & set(rb)):
            sa, sb = set(ra[key]), set(rb[key])
            if sa != sb:
                changed[key] = {"added": sorted(sa - sb), "removed": sorted(sb - sa)}
        if only_a or only_b or changed:
            route_diffs[tbl] = {"only_cand": only_a, "only_ref": only_b, "changed": changed}

    meta_diffs: dict[str, Any] = {}
    for sec in ("head", "hhea", "OS/2", "post", "vhea"):
        va, vb = cand_meta.get(sec), ref_meta.get(sec)
        if va != vb:
            if isinstance(va, dict) and isinstance(vb, dict):
                sub = {k: [va.get(k), vb.get(k)] for k in sorted(set(va) | set(vb)) if va.get(k) != vb.get(k)}
                if sub:
                    meta_diffs[sec] = sub
            else:
                meta_diffs[sec] = [va, vb]

    return {
        "label": label,
        "cand_name": cand_path.name,
        "ref_name": ref_path.name,
        "cand_wght": cand_wght,
        "ref_wght": ref_wght,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "cmap": {
            "cand_total": len(cand_cps),
            "ref_total": len(ref_cps),
            "only_cand_count": len(only_cand_cps),
            "only_ref_count": len(only_ref_cps),
            "only_cand_samples": [_fmt_cp(c) for c in only_cand_cps[:sample_limit]],
            "only_ref_samples": [_fmt_cp(c) for c in only_ref_cps[:sample_limit]],
        },
        "uvs_format14": {
            "cand_total": len(cand_uvs),
            "cand_non_default": len(cand_uvs_nd),
            "ref_total": len(ref_uvs),
            "ref_non_default": len(ref_uvs_nd),
            "only_cand_count": len(only_cand_uvs),
            "only_ref_count": len(only_ref_uvs),
            "only_ref_samples": [f"U+{b:04X}+U+{s:04X}" for b, s in only_ref_uvs[:sample_limit]],
        },
        "advances": {k: dict(v) for k, v in sorted(adv_by_cat.items())},
        "ink": {"gt2": ink_gt2, "minor_1_to_2": ink_1_2},
        "probes": {"total": len(probes), "diff_count": len(probe_diffs), "diffs": probe_diffs},
        "routes": route_diffs,
        "metadata": meta_diffs,
    }


def _worker_compare(task: dict[str, Any]) -> dict[str, Any]:
    return compare_font_pair(
        Path(task["cand"]),
        Path(task["ref"]),
        label=task["label"],
        cand_wght=task.get("cand_wght"),
        ref_wght=task.get("ref_wght"),
    )


def run_compare_batch(
    tasks: list[dict[str, Any]],
    *,
    jobs: int = 4,
    out_dir: Path | None = None,
    title: str = "字体对照报告",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    results: list[dict[str, Any]] = []
    if jobs <= 1 or len(tasks) <= 1:
        for task in tasks:
            results.append(_worker_compare(task))
    else:
        executor = concurrent.futures.ProcessPoolExecutor(max_workers=min(jobs, len(tasks)))
        try:
            futures = {executor.submit(_worker_compare, task): task["label"] for task in tasks}
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())
        except KeyboardInterrupt:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    order = {t["label"]: idx for idx, t in enumerate(tasks)}
    results.sort(key=lambda r: order.get(r["label"], 0))
    summary = {
        "title": title,
        "pair_count": len(results),
        "elapsed_sec": round(time.perf_counter() - t0, 2),
        "results": results,
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "compare.json").write_bytes(
            (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        (out_dir / "compare.md").write_bytes(render_markdown(summary).encode("utf-8"))
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# {summary['title']}",
        "",
        f"对照对数：{summary['pair_count']}，总耗时：{summary['elapsed_sec']} 秒。",
        "",
        "| 项 | cmap 差 (+/−) | UVS 差 (候/参) | 步进差 | 墨迹>2 | 语料差 | 路由差异表 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in summary["results"]:
        cm = r["cmap"]
        uvs = r["uvs_format14"]
        adv_total = sum(v["count"] for v in r["advances"].values())
        routes = ",".join(sorted(r["routes"])) or "无"
        lines.append(
            f"| {r['label']} | +{cm['only_cand_count']}/-{cm['only_ref_count']} "
            f"| {uvs['cand_total']}/{uvs['ref_total']} "
            f"| {adv_total} | {r['ink']['gt2']['count']} "
            f"| {r['probes']['diff_count']}/{r['probes']['total']} | {routes} |"
        )

    lines.append("")
    lines.append("## 明细摘要")
    for r in summary["results"]:
        lines.append("")
        lines.append(f"### {r['label']}（{r['cand_name']} vs {r['ref_name']}）")
        uvs = r["uvs_format14"]
        if uvs["only_ref_count"] or uvs["only_cand_count"]:
            lines.append(
                f"- **cmap format 14**：候选 {uvs['cand_total']}（非默认 {uvs['cand_non_default']}）"
                f" vs 参照 {uvs['ref_total']}（非默认 {uvs['ref_non_default']}），"
                f"缺样例：{', '.join(uvs['only_ref_samples'][:6]) or '无'}"
            )
        if r["advances"]:
            parts = [
                f"{cat}={info['count']}(max {info['max_diff']}: {', '.join(info['samples'][:3])})"
                for cat, info in r["advances"].items()
            ]
            lines.append("- **步进差**：" + "；".join(parts))
        ink = r["ink"]["gt2"]
        if ink["count"]:
            lines.append(
                f"- **墨迹差 >2**：{ink['count']}（最大 {ink['max_diff']}，微差 1–2 共 {r['ink']['minor_1_to_2']}），"
                f"样例：{', '.join(ink['samples'][:6])}"
            )
        if r["probes"]["diffs"]:
            lines.append(f"- **语料差异（{r['probes']['diff_count']} 项）**：")
            for item in r["probes"]["diffs"]:
                lines.append(f"  - `{item['probe']}`（`{item['text']}`）：{item['reason']}")
        if r["routes"]:
            for tbl, info in r["routes"].items():
                parts = []
                if info["only_cand"]:
                    parts.append(f"多出脚本/语言 {len(info['only_cand'])}（如 {', '.join(info['only_cand'][:4])}）")
                if info["only_ref"]:
                    parts.append(f"缺少脚本/语言 {len(info['only_ref'])}")
                if info["changed"]:
                    parts.append(f"特性变更 {len(info['changed'])} 处")
                lines.append(f"- **{tbl} 路由**：" + "，".join(parts))
    lines.append("")
    return "\n".join(lines)


def build_upstream_tasks(
    cand_root: Path,
    ref_root: Path,
    regions: list[str],
    styles: list[str],
    weights: list[str],
    *,
    cand_version: str | None = None,
    include_vf: bool = True,
    include_static: bool = True,
    spec: Spec | None = None,
) -> list[dict[str, Any]]:
    """按候选目录与上游 Sarasa Ui 1.0.43 目录构造对照任务。"""
    spec = spec or load_spec()
    ver = cand_version or spec.version
    tasks: list[dict[str, Any]] = []
    for region in regions:
        r = spec.region(region)
        for style in styles:
            italic = style == "italic"
            for weight_name in weights:
                w = spec.weight(weight_name)
                ref_file = ref_root / r.code / "unhinted" / spec.sarasa_ref_filename(r.code, w.name, italic)
                if not ref_file.is_file():
                    continue
                if include_static:
                    static_dir = cand_root / "static" / f"SarasaUiPropDigits{r.code}-TTF-Unhinted-{ver}"
                    cand_file = static_dir / spec.static_filename(r.code, w.name, italic)
                    if cand_file.is_file():
                        tasks.append({
                            "label": f"static:{r.code}-{w.name}{'-Italic' if italic else ''}",
                            "cand": str(cand_file),
                            "ref": str(ref_file),
                        })
                if include_vf:
                    vf_file = cand_root / "variable" / spec.variable_filename(r.code, italic)
                    if vf_file.is_file():
                        tasks.append({
                            "label": f"vf:{r.code}@{w.value}{'-Italic' if italic else ''}",
                            "cand": str(vf_file),
                            "ref": str(ref_file),
                            "cand_wght": w.value,
                        })
    return tasks


def build_project_tasks(
    cand_root: Path,
    ref_root: Path,
    regions: list[str],
    styles: list[str],
    weights: list[str],
    *,
    cand_version: str | None = None,
    ref_version: str = "1.0.43.3",
    include_vf: bool = True,
    include_static: bool = True,
    hinted: bool = False,
    spec: Spec | None = None,
) -> list[dict[str, Any]]:
    """按本项目目录结构构造对照任务（用于新版对照 v1.0.43.3）。"""
    spec = spec or load_spec()
    c_ver = cand_version or spec.version
    variant = "TTF" if hinted else "TTF-Unhinted"
    tasks: list[dict[str, Any]] = []
    for region in regions:
        r = spec.region(region)
        for style in styles:
            italic = style == "italic"
            for weight_name in weights:
                w = spec.weight(weight_name)
                if include_static:
                    cand_file = cand_root / "static" / f"SarasaUiPropDigits{r.code}-{variant}-{c_ver}" / spec.static_filename(r.code, w.name, italic)
                    ref_file = ref_root / "static" / f"SarasaUiPropDigits{r.code}-{variant}-{ref_version}" / spec.static_filename(r.code, w.name, italic)
                    if cand_file.is_file() and ref_file.is_file():
                        tasks.append({
                            "label": f"static{'-hinted' if hinted else ''}:{r.code}-{w.name}{'-Italic' if italic else ''}",
                            "cand": str(cand_file),
                            "ref": str(ref_file),
                        })
                if include_vf:
                    cand_vf = cand_root / "variable" / spec.variable_filename(r.code, italic)
                    ref_vf = ref_root / "variable" / spec.variable_filename(r.code, italic)
                    if cand_vf.is_file() and ref_vf.is_file():
                        tasks.append({
                            "label": f"vf:{r.code}@{w.value}{'-Italic' if italic else ''}",
                            "cand": str(cand_vf),
                            "ref": str(ref_vf),
                            "cand_wght": w.value,
                            "ref_wght": w.value,
                        })
    return tasks
