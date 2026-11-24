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

    p_build = sub.add_parser("build", allow_abbrev=False, help="构建 VF、unhinted 静态与 A1 hinted 静态")
    _add_selection_args(p_build)
    p_build.add_argument("--stage", choices=("vf", "unhinted", "hinted", "all"), default="all")
    p_build.add_argument("--jobs", type=int, default=2, help="并行进程数（默认 2）")
    p_build.add_argument("--out", type=Path, default=None, help="输出目录（默认 .build-cache/runs/<时间戳>）")

    p_check = sub.add_parser("check", allow_abbrev=False, help="运行产品规则检查、OTS 与 FreeType 严格执行扫描")
    _add_selection_args(p_check)
    p_check.add_argument("--root", type=Path, default=None, help="待检查字体根目录（默认 fonts/）")
    p_check.add_argument("--stage", choices=("vf", "unhinted", "hinted", "all"), default="all")
    p_check.add_argument("--jobs", type=int, default=4, help="FreeType 扫描并发进程数（默认 4）")
    p_check.add_argument(
        "--ft-mode",
        choices=("fast", "full"),
        default="fast",
        help="hinted FreeType 严格扫描档位：fast 为日常采样（9–20 逐个 + 24–128 每 4 px），full 为发版全量（9–128 逐字号）",
    )
    p_check.add_argument("--skip-ots", action="store_true", help="跳过 OTS 检查（默认启用）")

    p_pkg = sub.add_parser("package", allow_abbrev=False, help="生成 21 个发布 ZIP 与 SHA256SUMS.txt")
    p_pkg.add_argument("--root", type=Path, default=None, help="已检查通过的字体根目录（默认 fonts/）")
    p_pkg.add_argument("--dist", type=Path, default=None, help="发布包输出目录（默认 dist/v<version>）")

    p_smp = sub.add_parser("samples", allow_abbrev=False, help="生成多地区 FreeType 与 Windows 96 DPI GDI/DirectWrite 对比样张")
    _add_selection_args(p_smp)
    p_smp.add_argument("--cand-root", type=Path, required=True, help="新构建字体根目录")
    p_smp.add_argument("--ref-root", type=Path, default=None, help="对照基准字体根目录（默认 fonts/）")
    p_smp.add_argument("--ref-version", default="1.0.43.3", help="基准静态目录版本号（默认 1.0.43.3）")
    p_smp.add_argument("--out", type=Path, default=None, help="样张输出目录（默认 .build-cache/samples）")
    p_smp.add_argument("--windows-only", action="store_true", help="仅生成 Windows GDI 与 DirectWrite 对比样张")

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


def _cmd_build(args: argparse.Namespace, spec: Spec) -> int:
    import time
    from .static.hint import build_hinted_statics
    from .static.instance import build_unhinted_statics
    from .vf.build import build_vfs

    regions = _parse_csv(args.regions, spec.region_order, flag="--regions")
    styles = _parse_csv(args.styles, ("upright", "italic"), flag="--styles")
    weights = _parse_csv(args.weights, [w.name for w in spec.weights], flag="--weights")
    out_root = args.out or (repo_root() / ".build-cache" / "runs" / time.strftime("%Y%m%d-%H%M%S"))
    stages = ("vf", "unhinted", "hinted") if args.stage == "all" else (args.stage,)
    if "vf" in stages:
        summary = build_vfs(
            regions,
            styles,
            out_root,
            subset_mode=args.subset,
            jobs=max(1, args.jobs),
            spec=spec,
        )
        print(f"已完成 {summary['count']} 个 VF 构建，耗时 {summary['elapsed_sec']} 秒，输出：{out_root}")
    if "unhinted" in stages:
        summary = build_unhinted_statics(
            out_root,
            out_root,
            regions,
            styles,
            weights,
            jobs=max(1, args.jobs),
            spec=spec,
        )
        print(f"已完成 {summary['count']} 个 unhinted 静态构建，耗时 {summary['elapsed_sec']} 秒，输出：{out_root}")
    if "hinted" in stages:
        summary = build_hinted_statics(
            out_root,
            out_root,
            regions,
            styles,
            weights,
            jobs=max(1, args.jobs),
            spec=spec,
        )
        print(f"已完成 {summary['count']} 个 hinted 静态构建，耗时 {summary['elapsed_sec']} 秒，输出：{out_root}")
    return 0


def _cmd_check(args: argparse.Namespace, spec: Spec) -> int:
    from .checks import (
        check_font_rules,
        check_hinted_unhinted_parity,
        check_ots,
        check_vf_static_parity,
        scan_freetype_pedantic,
    )
    from .sources import SourceStore

    root = repo_root()
    font_root = (args.root or (root / "fonts")).resolve()
    regions = _parse_csv(args.regions, spec.region_order, flag="--regions")
    styles = _parse_csv(args.styles, ("upright", "italic"), flag="--styles")
    weights = _parse_csv(args.weights, [w.name for w in spec.weights], flag="--weights")

    store = SourceStore(root / ".build-cache" / "work", spec)
    ots_exe: Path | None = None
    if not args.skip_ots:
        ots_exe = next((root / ".build-cache").rglob("ots-sanitize.exe" if sys.platform == "win32" else "ots-sanitize"), None)
        if ots_exe is None:
            ots_exe = store.ensure_ots(root / ".build-cache" / "external")

    failures: list[str] = []
    checked = 0
    stages = ("vf", "unhinted", "hinted") if args.stage == "all" else (args.stage,)

    for region in regions:
        ref_path = store.sarasa_ref_path(region, "Regular", False, hinted=False)
        for style in styles:
            italic = style == "italic"
            inter_vf_path = store.vf_dir / ("InterVariable-Italic.ttf" if italic else "InterVariable.ttf")
            inter_ref = inter_vf_path if inter_vf_path.is_file() else None
            vf_path = font_root / "variable" / spec.variable_filename(region, italic)
            if "vf" in stages:
                if not vf_path.is_file():
                    failures.append(f"缺少 VF：{vf_path}")
                else:
                    checked += 1
                    r = check_font_rules(
                        vf_path,
                        region=region,
                        italic=italic,
                        is_vf=True,
                        reference_path=ref_path if ref_path.is_file() else None,
                        inter_vf_path=inter_ref,
                        subset_mode=args.subset,
                        spec=spec,
                    )
                    if not r.ok:
                        failures.extend(f"[VF {vf_path.name}] {e}" for e in r.errors)
                    if ots_exe is not None:
                        o = check_ots(vf_path, ots_exe)
                        if not o.ok:
                            failures.extend(f"[VF {vf_path.name}] {e}" for e in o.errors)
                    print(f"[check] {vf_path.name}: {'PASS' if r.ok else 'FAIL'} (cps={r.metrics.get('codepoints')}, uvs={r.metrics.get('uvs_entries')}, a9={r.metrics.get('a9_mod_apostrophe_rho_kern')})", flush=True)

            for w_name in weights:
                w = spec.weight(w_name)
                un_path = spec.static_dir(font_root, region, hinted=False) / spec.static_filename(region, w.name, italic)
                hi_path = spec.static_dir(font_root, region, hinted=True) / spec.static_filename(region, w.name, italic)

                if "unhinted" in stages:
                    if not un_path.is_file():
                        failures.append(f"缺少 unhinted 静态：{un_path}")
                    else:
                        checked += 1
                        r = check_font_rules(
                            un_path,
                            region=region,
                            italic=italic,
                            is_vf=False,
                            hinted=False,
                            reference_path=ref_path if ref_path.is_file() else None,
                            inter_vf_path=inter_ref,
                            subset_mode=args.subset,
                            spec=spec,
                        )
                        if not r.ok:
                            failures.extend(f"[unhinted {un_path.name}] {e}" for e in r.errors)
                        if vf_path.is_file():
                            p = check_vf_static_parity(vf_path, un_path, w.value)
                            if not p.ok:
                                failures.extend(f"[vf-vs-static {un_path.name}] {e}" for e in p.errors)
                        if ots_exe is not None:
                            o = check_ots(un_path, ots_exe)
                            if not o.ok:
                                failures.extend(f"[unhinted {un_path.name}] {e}" for e in o.errors)

                if "hinted" in stages:
                    if not hi_path.is_file():
                        failures.append(f"缺少 hinted 静态：{hi_path}")
                    else:
                        checked += 1
                        r = check_font_rules(
                            hi_path,
                            region=region,
                            italic=italic,
                            is_vf=False,
                            hinted=True,
                            reference_path=ref_path if ref_path.is_file() else None,
                            inter_vf_path=inter_ref,
                            subset_mode=args.subset,
                            spec=spec,
                        )
                        if not r.ok:
                            failures.extend(f"[hinted {hi_path.name}] {e}" for e in r.errors)
                        ft = scan_freetype_pedantic(hi_path, mode=args.ft_mode, workers=max(1, args.jobs))
                        if not ft.ok:
                            failures.extend(f"[FT {hi_path.name}] {e}" for e in ft.errors)
                        if un_path.is_file():
                            par = check_hinted_unhinted_parity(hi_path, un_path)
                            if not par.ok:
                                failures.extend(f"[parity {hi_path.name}] {e}" for e in par.errors)
                        if ots_exe is not None:
                            o = check_ots(hi_path, ots_exe)
                            if not o.ok:
                                failures.extend(f"[hinted {hi_path.name}] {e}" for e in o.errors)
                        ok_hi = r.ok and ft.ok
                        print(f"[check-hi] {hi_path.name}: {'PASS' if ok_hi else 'FAIL'} (ft={ft.metrics.get('ppem_range')} fails={ft.metrics.get('fail_events')})", flush=True)

    if failures:
        print(f"CHECK FAILED: 检查 {checked} 个字体，发现 {len(failures)} 项错误：", file=sys.stderr)
        for msg in failures[:40]:
            print("  -", msg, file=sys.stderr)
        return 1
    print(f"CHECK PASSED: 共 {checked} 个字体全部通过产品规则与二进制检查。")
    return 0


def _cmd_package(args: argparse.Namespace, spec: Spec) -> int:
    from .package import package_release

    root = repo_root()
    font_root = (args.root or (root / "fonts")).resolve()
    dist_dir = (args.dist or (root / "dist" / f"v{spec.version}")).resolve()
    manifest = package_release(font_root, dist_dir, project_root=root, spec=spec)
    print(f"已打包并核验 {manifest['package_count']} 个发布 ZIP（耗时 {manifest['elapsed_sec']}s），输出：{dist_dir}")
    return 0


def _cmd_samples(args: argparse.Namespace, spec: Spec) -> int:
    import os
    from .samples import (
        render_region_comparison_sheet,
        render_windows_dwrite_comparison,
        render_windows_gdi_comparison,
    )

    root = repo_root()
    cand_root = args.cand_root.resolve()
    ref_root = (args.ref_root or (root / "fonts")).resolve()
    out_dir = (args.out or (root / ".build-cache" / "samples")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    regions = _parse_csv(args.regions, spec.region_order, flag="--regions")
    weights = _parse_csv(args.weights, [w.name for w in spec.weights], flag="--weights")

    def _ref_static(region: str, weight_name: str, italic: bool, hinted: bool) -> Path:
        d = spec.static_dir(ref_root, region, hinted=hinted, version=args.ref_version)
        return d / spec.static_filename(region, weight_name, italic)

    def _cand_static(region: str, weight_name: str, italic: bool, hinted: bool) -> Path:
        d = spec.static_dir(cand_root, region, hinted=hinted)
        return d / spec.static_filename(region, weight_name, italic)

    generated: list[Path] = []
    if not args.windows_only:
        for region in regions:
            for w_name in weights:
                rows: list[tuple[str, Path, bool]] = []
                c_hi = _cand_static(region, w_name, False, hinted=True)
                c_un = _cand_static(region, w_name, False, hinted=False)
                r_hi = _ref_static(region, w_name, False, hinted=True)
                if c_hi.is_file():
                    rows.append((f"New-{region}-{w_name}(hi)", c_hi, True))
                if r_hi.is_file():
                    rows.append((f"v{args.ref_version}-{region}-{w_name}(hi)", r_hi, True))
                if c_un.is_file():
                    rows.append((f"New-{region}-{w_name}(un)", c_un, False))
                if rows:
                    out = out_dir / f"ft-{region}-{w_name}.png"
                    render_region_comparison_sheet(out, region=region, rows=rows)
                    generated.append(out)

    if os.name == "nt":
        win_pairs: list[tuple[str, Path, int, bool]] = []
        for w_name, italic in (("Regular", False), ("Regular", True), ("Bold", False), ("Heavy", False)):
            w = spec.weight(w_name)
            c_hi = _cand_static("SC", w_name, italic, hinted=True)
            c_un = _cand_static("SC", w_name, italic, hinted=False)
            r_hi = _ref_static("SC", w_name, italic, hinted=True)
            c_path = c_hi if c_hi.is_file() else c_un
            tag = "It" if italic else w_name
            if c_path.is_file():
                win_pairs.append((f"New-SC-{tag}", c_path, w.value, italic))
            if r_hi.is_file():
                win_pairs.append((f"v{args.ref_version}-SC-{tag}", r_hi, w.value, italic))
        if win_pairs:
            generated.append(render_windows_gdi_comparison(out_dir / "win-gdi-96dpi-SC.png", win_pairs))
            generated.append(render_windows_dwrite_comparison(out_dir / "win-dwrite-96dpi-SC.png", win_pairs))

    print(f"已生成 {len(generated)} 张验收样张：{out_dir}")
    for p in generated:
        print("  -", p.name)
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
    if args.command == "build":
        try:
            return _cmd_build(args, spec)
        except argparse.ArgumentTypeError as err:
            parser.error(str(err))
    if args.command == "check":
        try:
            return _cmd_check(args, spec)
        except argparse.ArgumentTypeError as err:
            parser.error(str(err))
    if args.command == "package":
        return _cmd_package(args, spec)
    if args.command == "samples":
        try:
            return _cmd_samples(args, spec)
        except argparse.ArgumentTypeError as err:
            parser.error(str(err))
    raise SystemExit(f"未知子命令：{args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
