from __future__ import annotations

import argparse
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from python_env_bootstrap import AUDIT_DEPENDENCIES, ensure_project_python

if __name__ == "__main__":
    ensure_project_python(AUDIT_DEPENDENCIES, project_root=Path(__file__).resolve().parents[1], label="audit")

import audit_sarasa_ui_propdigits as audit
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


def main() -> None:
    parser = argparse.ArgumentParser(description="CI 快速检查；不替代完整的 23 节发布审计。")
    parser.add_argument("--regions", default=",".join(build.REGION_ORDER))
    args = parser.parse_args(); regions = build.parse_regions(args.regions)
    results = []; failures = []
    for region in regions:
        paths = [(audit.vf_path(region, italic), italic, None) for italic in (False, True)]
        paths.extend((audit.static_path(region, str(stop["name"]), italic, hinted), italic, str(stop["name"]))
                     for stop in build.SOURCE_HAN_WEIGHT_STOPS for italic in (False, True) for hinted in (False, True))
        for path, italic, weight_name in paths:
            label = audit.display_path(path)
            if not path.is_file():
                failures.append({"file": label, "kind": "missing"}); continue
            data = path.read_bytes(); item = {"file": label, "sha256": build.file_sha256(path)}
            with TTFont(io.BytesIO(data)) as font:
                item["head_bounds"] = audit.head_bounds_status(font)
                item["dash_structure"] = build.upstream_dash_structure_status(font, region)
                item["family_length"] = build.family_name_length_status(font)
                if weight_name is None:
                    item["variable_metadata"] = build.variable_raster_metadata_status(font)
                elif (font["name"].getDebugName(5) or "").endswith("; unhinted"):
                    item["nonhinting_metadata"] = build.nonhinting_raster_metadata_status(font)
                family = (build.vf_family(region) if weight_name is None else build.static_family(region))
                if font["name"].getDebugName(16) != family or f"project {build.VERSION}" not in font["name"].getDebugName(5):
                    failures.append({"file": label, "kind": "family_or_version"})
            weights = tuple(audit.INTER_POSITION_WEIGHTS) if weight_name is None else (None,)
            item["width_features"] = width_feature_status(data, weights)
            # All feature combinations and languages are checked at the default
            # instance here; the full audit covers thirteen variable locations.
            item["vertical_segments"] = audit.vertical_segment_runtime_status(path)
            for kind, status in item.items():
                if isinstance(status, dict) and status.get("ok") is False:
                    failures.append({"file": label, "kind": kind})
            results.append(item)
        print(f"[快速审计] {region}：26 个字体", flush=True)
    report = {"title": "持续集成快速审计", "version": build.VERSION,
              "generated_at": datetime.now(timezone.utc).isoformat(), "release_audit_complete": False,
              "regions": regions, "fonts_checked": len(results), "failures": failures, "results": results}
    output = build.ROOT / "reports" / "quick-audit.json"
    build.write_json_atomic(output, build.sanitize_report_data(report))
    print(f"[快速审计] 检查 {len(results)} 个字体，失败 {len(failures)} 项。", flush=True)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
