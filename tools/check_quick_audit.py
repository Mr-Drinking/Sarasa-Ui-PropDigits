from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from python_env_bootstrap import AUDIT_DEPENDENCIES, ensure_project_python

if __name__ == "__main__":
    ensure_project_python(AUDIT_DEPENDENCIES, project_root=Path(__file__).resolve().parents[1], label="audit")

from font_audit import contract as audit
from font_audit.metadata import head_bounds_status
from font_audit.products import vertical_segment_runtime_status
import build_sarasa_ui_propdigits_sc as build
import uharfbuzz as hb
from fontTools.ttLib import TTFont


def width_feature_status(data: bytes, weights: tuple[int | None, ...]) -> dict:
    runtime = hb.Font(hb.Face(data)); failures = []; cases = 0
    def shape(text, features):
        buffer = hb.Buffer(); buffer.add_str(text); buffer.script = "Latn"; buffer.direction = "ltr"
        hb.shape(runtime, buffer, {"kern": False, **features})
        return [(item.codepoint, pos.x_advance) for item, pos in zip(buffer.glyph_infos, buffer.glyph_positions)]
    for weight in weights:
        if weight is not None: runtime.set_variations({"wght": weight})
        for codepoint in sorted(audit.INTENTIONAL_CPS):
            plain = shape(chr(codepoint), {}); proportional = shape(chr(codepoint), {"pnum": True})
            cases += 1
            if plain != proportional or any(gid == 0 for gid, advance in plain):
                failures.append({"weight": weight, "codepoint": f"U+{codepoint:04X}", "kind": "default_proportional"})
        for features in ({"tnum": True}, {"tnum": True, "zero": True}):
            result = shape("0123456789", features); cases += 1
            if len(result) != 10 or len({advance for gid, advance in result}) != 1 or any(gid == 0 for gid, advance in result):
                failures.append({"weight": weight, "features": features, "kind": "strict_tabular", "result": result})
        for features in ({}, {"tnum": True}, {"tnum": True, "zero": True}):
            for text in ("1:2", "1:", ":2"):
                cases += 1
                if shape(text, {**features, "calt": True}) == shape(text, {**features, "calt": False}):
                    failures.append({"weight": weight, "features": features, "text": text, "kind": "colon_context"})
    return {"ok": not failures, "cases": cases, "failures": failures}


def font_status(task: tuple[Path, str, str | None]) -> tuple[dict | None, list[dict]]:
    """Check one font; runs in a worker process when there is more than one job."""
    path, region, weight_name = task
    label = audit.display_path(path)
    if not path.is_file():
        return None, [{"file": label, "kind": "missing"}]
    failures = []
    data = path.read_bytes(); item = {"file": label, "sha256": build.file_sha256(path)}
    with TTFont(io.BytesIO(data)) as font:
        item["head_bounds"] = head_bounds_status(font)
        item["dash_structure"] = build.upstream_dash_structure_status(font, region)
        item["family_length"] = build.family_name_length_status(font)
        if weight_name is None:
            item["variable_metadata"] = build.variable_raster_metadata_status(font)
        elif (font["name"].getDebugName(5) or "").endswith("; unhinted"):
            item["nonhinting_metadata"] = build.nonhinting_raster_metadata_status(font)
        family = (build.vf_family(region) if weight_name is None else build.static_family(region))
        if font["name"].getDebugName(16) != family or f"project {build.VERSION}" not in font["name"].getDebugName(5):
            failures.append({"file": label, "kind": "family_or_version"})
    weights = tuple(audit.AXIS_POINTS) if weight_name is None else (None,)
    item["width_features"] = width_feature_status(data, weights)
    # All feature combinations and languages are checked at the default
    # instance here; the full audit covers thirteen variable locations.
    item["vertical_segments"] = vertical_segment_runtime_status(path)
    for kind, status in item.items():
        if isinstance(status, dict) and status.get("ok") is False:
            failures.append({"file": label, "kind": kind})
    return item, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="CI 快速检查；不替代完整的 23 节发布审计。")
    parser.add_argument("--regions", default=",".join(build.REGION_ORDER))
    parser.add_argument("--jobs", type=int, default=min(10, os.cpu_count() or 1), help="并行进程数。")
    args = parser.parse_args(); regions = build.parse_regions(args.regions)
    tasks = []
    for region in regions:
        tasks.extend((audit.vf_path(region, italic), region, None) for italic in (False, True))
        tasks.extend((audit.static_path(region, str(stop["name"]), italic, hinted), region, str(stop["name"]))
                     for stop in build.SOURCE_HAN_WEIGHT_STOPS for italic in (False, True) for hinted in (False, True))
    results = []; failures = []

    def collect(statuses) -> None:
        # Statuses arrive in task order, so the report keeps its order whatever the job count.
        for index, (item, found) in enumerate(statuses):
            if item is not None:
                results.append(item)
            failures.extend(found)
            region = tasks[index][1]
            if index + 1 == len(tasks) or tasks[index + 1][1] != region:
                print(f"[快速审计] {region}：26 个字体", flush=True)

    workers = max(1, min(args.jobs, len(tasks)))
    if workers == 1:
        collect(map(font_status, tasks))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            collect(pool.map(font_status, tasks))
    report = {"title": "持续集成快速审计", "version": build.VERSION,
              "generated_at": datetime.now(timezone.utc).isoformat(), "release_audit_complete": False,
              "regions": regions, "fonts_checked": len(results), "failures": failures, "results": results}
    output = build.ROOT / "reports" / "quick-audit.json"
    build.write_json_atomic(output, build.sanitize_report_data(report))
    print(f"[快速审计] 检查 {len(results)} 个字体，失败 {len(failures)} 项。", flush=True)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
