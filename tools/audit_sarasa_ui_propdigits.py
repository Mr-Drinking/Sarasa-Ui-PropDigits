"""Independent release audit.

The trunk is differential: the VF equals the static font, and each static font
equals the pinned Sarasa Ui release, across the whole cmap, source-derived
sequences, scripts, languages, directions and features. A difference passes
only when an explicit oracle (Inter, Source Han, Shanggu) independently
reproduces it. Product rules that have no upstream counterpart are checked
against their own sources. No build module or build verdict is imported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from python_env_bootstrap import AUDIT_DEPENDENCIES, ensure_project_python  # noqa: E402

if __name__ == "__main__":
    ensure_project_python(AUDIT_DEPENDENCIES, project_root=ROOT, label="audit")

from font_audit import contract  # noqa: E402
from font_audit.gate import SECTIONS  # noqa: E402

RELEASE_REPORT = ROOT / "reports" / "release-audit.json"
PARTIAL_DIR = ROOT / ".build-cache" / "release-audit-partial"
CHECKPOINTS = ROOT / ".build-cache" / "release-audit-checkpoints" / "v3"
WORKSTATION_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|/Users/|/home/")


def parse_list(value: str | None, allowed, label: str) -> list[str]:
    if value is None:
        return list(allowed)
    items = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(items) - set(allowed))
    if not items or unknown:
        raise SystemExit(f"未知或为空的{label}：{unknown or value}")
    return [item for item in allowed if item in items]


def write_report(path: Path, report: dict) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if WORKSTATION_PATH.search(text):
        raise ValueError("审计报告不能包含本机绝对路径")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="独立发布审计：23 节显式门槛；局部运行只生成 PARTIAL 报告。")
    parser.add_argument("--regions", help="逗号分隔的地区，默认六地区。")
    parser.add_argument("--sections", help="逗号分隔的章节，默认全部 23 节：" + ",".join(SECTIONS))
    parser.add_argument("--jobs", type=int, default=min(10, os.cpu_count() or 1), help="并行进程数。")
    parser.add_argument("--checkpoint-dir", type=Path, default=CHECKPOINTS,
                        help="检查点目录；键绑定输入字体、审计程序、来源与工具身份。")
    parser.add_argument("--no-checkpoints", action="store_true", help="不读取也不写入检查点。")
    parser.add_argument("--report", type=Path, help="报告路径；完整范围默认写入 reports/release-audit.json。")
    args = parser.parse_args()
    regions = parse_list(args.regions, contract.REGIONS, "地区")
    sections = parse_list(args.sections, SECTIONS, "章节")
    complete_scope = set(regions) == set(contract.REGIONS) and set(sections) == set(SECTIONS)
    if args.report is not None:
        output = args.report.resolve()
        if output == RELEASE_REPORT.resolve() and not complete_scope:
            parser.error("局部审计不能覆盖发布审计报告")
    elif complete_scope:
        output = RELEASE_REPORT
    else:
        label = "-".join(regions) + ("" if set(sections) == set(SECTIONS) else f"-{len(sections)}sections")
        output = PARTIAL_DIR / f"release-audit-{label}.json"

    from font_audit import pipeline

    report = pipeline.run(regions=regions, selected=sections, jobs=args.jobs,
                          checkpoint_dir=None if args.no_checkpoints else args.checkpoint_dir,
                          log=lambda message: print(message, flush=True))
    write_report(output, report)
    gate = report["gate"]
    for name in SECTIONS:
        section = gate["sections"][name]
        print(f"{name:22s} {section['status']:10s} 记录 {section['records']}/{section['expected_records']}"
              f" 失败 {section['failure_count']}", flush=True)
    state = "PASSED" if gate["passed"] else "PARTIAL" if not complete_scope else "FAILED"
    try:
        shown = contract.display_path(output)
    except ValueError:
        shown = output.name
    print(f"[审计] {state}：失败 {gate['total_failures']}，执行 {gate['executed_sections']}/{len(SECTIONS)} 节；"
          f"报告 {shown}", flush=True)
    executed = [gate["sections"][name] for name in sections]
    clean = gate["total_failures"] == 0 and all(section["complete"] for section in executed)
    raise SystemExit(0 if (gate["passed"] if complete_scope else clean) else 1)


if __name__ == "__main__":
    main()
