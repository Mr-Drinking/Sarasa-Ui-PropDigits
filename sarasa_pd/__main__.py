"""sarasa_pd 统一命令行入口：python -I -X utf8 -m sarasa_pd <子命令>。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from .compare import (
    build_project_tasks,
    build_upstream_tasks,
    compare_font_pair,
    render_markdown,
    run_compare_batch,
)
from .env import check_environment, repo_root
from .spec import Spec, load_spec


def _parse_csv(raw: str | None, valid: Sequence[str], *, flag: str, case_insensitive: bool = True) -> list[str]:
    if raw is None:
        return list(valid)
    items = [part.strip() for part in raw.split(",")]
    if not raw.strip() or any(not item for item in items):
        raise argparse.ArgumentTypeError(f"{flag} 不允许空值：{raw!r}")
    lookup = {v.lower(): v for v in valid} if case_insensitive else {v: v for v in valid}
    resolved: list[str] = []
    for item in items:
        key = item.lower() if case_insensitive else item
        if key not in lookup:
            raise argparse.ArgumentTypeError(f"{flag} 包含未知项 {item!r}，可选：{', '.join(valid)}")
        canonical = lookup[key]
        if canonical not in resolved:
            resolved.append(canonical)
    return resolved


def _add_selection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--regions", default=None, help="逗号分隔地区列表，如 SC,CL（默认全部 6 地区）")
    parser.add_argument("--styles", default=None, help="upright,italic（默认两者）")
    parser.add_argument("--weights", default=None, help="逗号分隔字重列表，如 Regular,Heavy（默认全部 6 字重）")
    parser.add_argument("--subset", choices=("full", "smoke"), default="full", help="full 全字集或 smoke 千字冒烟")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sarasa_pd",
        description="Sarasa Ui PropDigits 构建、检查、对照与打包工具",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_env = sub.add_parser("env", allow_abbrev=False, help="检查 Python 版本、依赖锁与导入路径隔离")
    p_env.add_argument("--lock", type=Path, default=None, help="指定锁文件路径（默认仓库根目录 requirements.lock）")

    p_cmp = sub.add_parser("compare", allow_abbrev=False, help="对照两套字体或单个字体对，输出 Markdown 摘要与 JSON 明细")
    _add_selection_args(p_cmp)
    p_cmp.add_argument("--baseline", choices=("upstream", "project"), default="upstream", help="对照基准类型：upstream（上游 Sarasa）或 project（如 v1.0.43.3）")
    p_cmp.add_argument("--cand-root", type=Path, default=None, help="候选字体根目录（默认 fonts/）")
    p_cmp.add_argument("--cand-version", default=None, help="候选静态目录版本号（如 1.0.43.3）")
    p_cmp.add_argument("--ref-root", type=Path, default=None, help="参照字体根目录")
    p_cmp.add_argument("--ref-version", default="1.0.43.3", help="project 基准时的参照版本号（默认 1.0.43.3）")
    p_cmp.add_argument("--hinted", action="store_true", help="project 基准时比较 hinted 静态字体")
    p_cmp.add_argument("--cand-file", type=Path, default=None, help="单对比较：候选字体文件")
    p_cmp.add_argument("--ref-file", type=Path, default=None, help="单对比较：参照字体文件")
    p_cmp.add_argument("--cand-wght", type=int, default=None, help="单对比较：候选 VF 的 wght 坐标")
    p_cmp.add_argument("--ref-wght", type=int, default=None, help="单对比较：参照 VF 的 wght 坐标")
    p_cmp.add_argument("--no-vf", action="store_true", help="跳过 VF 对照")
    p_cmp.add_argument("--no-static", action="store_true", help="跳过 unhinted 静态对照")
    p_cmp.add_argument("--jobs", type=int, default=4, help="并行进程数（默认 4）")
    p_cmp.add_argument("--out", type=Path, default=None, help="输出目录（默认 .build-cache/compare/latest）")

    p_build = sub.add_parser("build", allow_abbrev=False, help="构建 VF、unhinted 静态与 A1 hinted 静态（阶段 2–4 实现）")
    _add_selection_args(p_build)
    p_build.add_argument("--stage", choices=("vf", "unhinted", "hinted", "all"), default="all")
    p_build.add_argument("--out", type=Path, default=None)

    p_check = sub.add_parser("check", allow_abbrev=False, help="运行产品规则检查、OTS 与 FreeType 严格执行扫描（阶段 2–4 实现）")
    _add_selection_args(p_check)
    p_check.add_argument("--root", type=Path, default=None)

    p_pkg = sub.add_parser("package", allow_abbrev=False, help="生成 21 个发布 ZIP 与 SHA256SUMS.txt（阶段 5 实现）")
    p_pkg.add_argument("--root", type=Path, default=None)
    p_pkg.add_argument("--dist", type=Path, default=None)

    return parser


def _cmd_env(args: argparse.Namespace) -> int:
    res = check_environment(args.lock)
    print(f"Python: {res.python_version} (isolated={res.isolated})")
    print(f"Locked packages verified: {len(res.versions)}")
    if not res.ok:
        for err in res.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print("OK: environment matches lockfile without shadow imports.")
    return 0


def _cmd_compare(args: argparse.Namespace, spec: Spec) -> int:
    root = repo_root()
    out_dir = args.out or (root / ".build-cache" / "compare" / "latest")

    if args.cand_file or args.ref_file:
        if not (args.cand_file and args.ref_file):
            raise SystemExit("单对比较必须同时提供 --cand-file 和 --ref-file")
        item = compare_font_pair(
            args.cand_file,
            args.ref_file,
            label=f"{args.cand_file.stem} vs {args.ref_file.stem}",
            cand_wght=args.cand_wght,
            ref_wght=args.ref_wght,
        )
        summary = {"title": "单对字体对照报告", "pair_count": 1, "elapsed_sec": item["elapsed_sec"], "results": [item]}
        out_dir.mkdir(parents=True, exist_ok=True)
        import json
        (out_dir / "compare.json").write_bytes((json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        md = render_markdown(summary)
        (out_dir / "compare.md").write_bytes(md.encode("utf-8"))
        print(md)
        return 0

    regions = _parse_csv(args.regions, spec.region_order, flag="--regions")
    styles = _parse_csv(args.styles, ("upright", "italic"), flag="--styles")
    weight_names = [w.name for w in spec.weights]
    weights = _parse_csv(args.weights, weight_names, flag="--weights")

    cand_root = args.cand_root or (root / "fonts")
    if args.baseline == "project":
        ref_root = args.ref_root or (root / "fonts")
        tasks = build_project_tasks(
            cand_root,
            ref_root,
            regions,
            styles,
            weights,
            cand_version=args.cand_version,
            ref_version=args.ref_version,
            include_vf=not args.no_vf,
            include_static=not args.no_static,
            hinted=args.hinted,
            spec=spec,
        )
    else:
        ref_root = args.ref_root or (root / ".build-cache" / "work" / f"official-sarasa-ui-{spec.sarasa_version}")
        tasks = build_upstream_tasks(
            cand_root,
            ref_root,
            regions,
            styles,
            weights,
            cand_version=args.cand_version,
            include_vf=not args.no_vf,
            include_static=not args.no_static,
            spec=spec,
        )
    if not tasks:
        raise SystemExit(f"在 {cand_root} 与 {ref_root} 中未找到可对照的字体对")

    summary = run_compare_batch(
        tasks,
        jobs=max(1, args.jobs),
        out_dir=out_dir,
        title=f"对照报告（{cand_root.name} vs {ref_root.name}）",
    )
    print(f"已完成 {summary['pair_count']} 对字体对照，耗时 {summary['elapsed_sec']} 秒。")
    print(f"摘要：{out_dir / 'compare.md'}")
    print(f"明细：{out_dir / 'compare.json'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentTypeError as err:
        parser.error(str(err))
    spec = load_spec()

    if args.command == "env":
        return _cmd_env(args)
    if args.command == "compare":
        try:
            return _cmd_compare(args, spec)
        except argparse.ArgumentTypeError as err:
            parser.error(str(err))
    raise SystemExit(f"子命令 {args.command!r} 尚未实现（将在后续阶段接入）")


if __name__ == "__main__":
    raise SystemExit(main())
