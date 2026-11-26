"""sarasa_pd 统一命令行入口：python -E -s -X utf8 -m sarasa_pd <子命令>。"""
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


def _add_selection_args(parser: argparse.ArgumentParser, *, include_subset: bool = True) -> None:
    parser.add_argument("--regions", default=None, help="逗号分隔地区列表，如 SC,CL（默认全部 6 地区）")
    parser.add_argument("--styles", default=None, help="upright,italic（默认两者）")
    parser.add_argument("--weights", default=None, help="逗号分隔字重列表，如 Regular,Heavy（默认全部 6 字重）")
    if include_subset:
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
    _add_selection_args(p_cmp, include_subset=False)
    p_cmp.add_argument("--baseline", choices=("upstream", "project"), default="upstream", help="对照基准类型：upstream（上游 Sarasa）或 project（历史版本）")
    p_cmp.add_argument("--cand-root", type=Path, default=None, help="候选字体根目录（默认 fonts/）")
    p_cmp.add_argument("--cand-version", default=None, help="候选静态目录版本号（默认 spec.toml 当前版本）")
    p_cmp.add_argument("--ref-root", type=Path, default=None, help="参照字体根目录")
    p_cmp.add_argument("--ref-version", default=None, help="project 基准时的参照版本号（默认 spec.toml previous_version）")
    p_cmp.add_argument("--hinted", action="store_true", help="project 基准时比较 hinted 静态字体")
    p_cmp.add_argument("--cand-file", type=Path, default=None, help="单对比较：候选字体文件")
    p_cmp.add_argument("--ref-file", type=Path, default=None, help="单对比较：参照字体文件")
    p_cmp.add_argument("--cand-wght", type=int, default=None, help="单对比较：候选 VF 的 wght 坐标")
    p_cmp.add_argument("--ref-wght", type=int, default=None, help="单对比较：参照 VF 的 wght 坐标")
    p_cmp.add_argument("--no-vf", action="store_true", help="跳过 VF 对照")
    p_cmp.add_argument("--no-static", action="store_true", help="跳过 unhinted 静态对照")
    p_cmp.add_argument("--jobs", type=int, default=4, help="并行进程数（默认 4）")
    p_cmp.add_argument("--out", type=Path, default=None, help="输出目录（默认 .build-cache/compare/latest）")

    p_build = sub.add_parser("build", allow_abbrev=False, help="构建 VF、unhinted 静态与混合 hinted 静态")
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
    p_pkg.add_argument("--skip-check-gate", action="store_true", help="跳过全量检查通过标记核对（仅限排错）")

    p_smp = sub.add_parser("samples", allow_abbrev=False, help="生成多地区 FreeType 与 Windows 96 DPI GDI/DirectWrite 对比样张")
    _add_selection_args(p_smp, include_subset=False)
    p_smp.add_argument("--cand-root", type=Path, required=True, help="新构建字体根目录")
    p_smp.add_argument("--ref-root", type=Path, default=None, help="对照基准字体根目录（默认 fonts/）")
    p_smp.add_argument("--ref-version", default=None, help="基准静态目录版本号（默认 spec.toml previous_version）")
    p_smp.add_argument("--out", type=Path, default=None, help="样张输出目录（默认 .build-cache/samples）")
    p_smp.add_argument("--windows-only", action="store_true", help="仅生成 Windows GDI 与 DirectWrite 对比样张")

    p_docs = sub.add_parser("docs", allow_abbrev=False, help="按 spec.toml 同步 README 与 docs/ 中的规格表格")
    p_docs.add_argument("--check", action="store_true", help="只核对、不写入，有不一致时返回 1")

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
    ref_version = args.ref_version or spec.previous_version
    if args.baseline == "project":
        ref_root = args.ref_root or (root / "fonts")
        tasks = build_project_tasks(
            cand_root,
            ref_root,
            regions,
            styles,
            weights,
            cand_version=args.cand_version,
            ref_version=ref_version,
            include_vf=not args.no_vf,
            include_static=not args.no_static,
            hinted=args.hinted,
            spec=spec,
        )
    else:
        if args.ref_root is None:
            from .sources import SourceStore

            store = SourceStore(root / ".build-cache" / "work", spec)
            store.ensure_sarasa_reference(regions, hinted=False)
            ref_root = store.ref_dir
        else:
            ref_root = args.ref_root
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
        check_hint_displacement,
        check_hinted_unhinted_parity,
        check_ots,
        check_vf_static_parity,
        scan_freetype_pedantic,
    )
    from .package import write_check_gate
    from .sources import SourceStore

    root = repo_root()
    font_root = (args.root or (root / "fonts")).resolve()
    regions = _parse_csv(args.regions, spec.region_order, flag="--regions")
    styles = _parse_csv(args.styles, ("upright", "italic"), flag="--styles")
    weights = _parse_csv(args.weights, [w.name for w in spec.weights], flag="--weights")

    stages = ("vf", "unhinted", "hinted") if args.stage == "all" else (args.stage,)
    store = SourceStore(root / ".build-cache" / "work", spec)
    # 始终准备并校验固定来源参照，绝不因缺本地参照而静默跳过 cmap/UVS、Inter 字偶距或 hint 位移门检查
    store.ensure_sarasa_reference(regions, hinted=False)
    if "hinted" in stages:
        store.ensure_sarasa_reference(regions, hinted=True)
    vf_sources = store.ensure_vf_sources(regions)
    disp_cache_dir = store.work_root / "hint-disp-cache"

    ots_exe: Path | None = None
    if not args.skip_ots:
        ots_exe = store.ensure_ots(root / ".build-cache" / "external")

    failures: list[str] = []
    checked = 0

    for region in regions:
        for style in styles:
            italic = style == "italic"
            vf_ref_path = store.sarasa_ref_path(region, "Regular", italic, hinted=False)
            inter_ref = vf_sources["inter_italic" if italic else "inter_upright"]
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
                        reference_path=vf_ref_path,
                        inter_vf_path=inter_ref,
                        subset_mode=args.subset,
                        require_references=True,
                        spec=spec,
                    )
                    if not r.ok:
                        failures.extend(f"[VF {vf_path.name}] {e}" for e in r.errors)
                    if ots_exe is not None:
                        o = check_ots(vf_path, ots_exe)
                        if not o.ok:
                            failures.extend(f"[VF {vf_path.name}] {e}" for e in o.errors)
                    print(
                        f"[check] {vf_path.name}: {'PASS' if r.ok else 'FAIL'} "
                        f"(cps={r.metrics.get('codepoints')}, uvs={r.metrics.get('uvs_entries')}, "
                        f"kern_rho={r.metrics.get('mod_apostrophe_rho_kern')})",
                        flush=True,
                    )

            for w_name in weights:
                w = spec.weight(w_name)
                st_ref_path = store.sarasa_ref_path(region, w.name, italic, hinted=False)
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
                            reference_path=st_ref_path,
                            inter_vf_path=inter_ref,
                            subset_mode=args.subset,
                            require_references=True,
                            spec=spec,
                        )
                        if not r.ok:
                            failures.extend(f"[unhinted {un_path.name}] {e}" for e in r.errors)
                        if vf_path.is_file():
                            p = check_vf_static_parity(
                                vf_path,
                                un_path,
                                w.value,
                                region=region,
                                italic=italic,
                                spec=spec,
                                workers=max(1, args.jobs),
                            )
                            if not p.ok:
                                failures.extend(f"[vf-vs-static {un_path.name}] {e}" for e in p.errors)
                        elif args.stage == "all":
                            failures.append(f"[vf-vs-static {un_path.name}] 缺少源 VF：{vf_path}")
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
                            reference_path=st_ref_path,
                            inter_vf_path=inter_ref,
                            subset_mode=args.subset,
                            require_references=True,
                            spec=spec,
                        )
                        if not r.ok:
                            failures.extend(f"[hinted {hi_path.name}] {e}" for e in r.errors)
                        ft = scan_freetype_pedantic(hi_path, mode=args.ft_mode, workers=max(1, args.jobs))
                        if not ft.ok:
                            failures.extend(f"[FT {hi_path.name}] {e}" for e in ft.errors)
                        hi_ref_path = store.sarasa_ref_path(region, w.name, italic, hinted=True)
                        # Heavy 上游无对应字重，优先以同地区同风格的本版 Bold 实测分布为参照（不存在时退回上游 Bold）
                        if w.name == "Heavy":
                            our_bold_path = spec.static_dir(font_root, region, hinted=True) / spec.static_filename(
                                region, "Bold", italic
                            )
                            if our_bold_path.is_file():
                                hi_ref_path = our_bold_path
                        disp = check_hint_displacement(
                            hi_path,
                            hi_ref_path,
                            is_heavy=(w.name == "Heavy"),
                            cache_dir=disp_cache_dir,
                            workers=max(1, args.jobs),
                        )
                        if not disp.ok:
                            failures.extend(f"[hint-disp {hi_path.name}] {e}" for e in disp.errors)
                        if un_path.is_file():
                            par = check_hinted_unhinted_parity(hi_path, un_path)
                            if not par.ok:
                                failures.extend(f"[parity {hi_path.name}] {e}" for e in par.errors)
                        elif args.stage == "all":
                            failures.append(f"[parity {hi_path.name}] 缺少 unhinted 静态：{un_path}")
                        if ots_exe is not None:
                            o = check_ots(hi_path, ots_exe)
                            if not o.ok:
                                failures.extend(f"[hinted {hi_path.name}] {e}" for e in o.errors)
                        ok_hi = r.ok and ft.ok and disp.ok
                        d28 = (disp.metrics.get("hint_disp_20_28_32") or {}).get(28) or {}
                        print(
                            f"[check-hi] {hi_path.name}: {'PASS' if ok_hi else 'FAIL'} "
                            f"(ft={ft.metrics.get('ppem_range')} fails={ft.metrics.get('fail_events')} "
                            f"disp28={d28.get('cand_gt2', '-')}/{d28.get('ref_gt2', '-')})",
                            flush=True,
                        )

    if failures:
        print(f"CHECK FAILED: 检查 {checked} 个字体，发现 {len(failures)} 项错误：", file=sys.stderr)
        for msg in failures[:40]:
            print("  -", msg, file=sys.stderr)
        return 1

    full_matrix = (
        args.stage == "all"
        and args.subset == "full"
        and set(regions) == set(spec.region_order)
        and set(styles) == {"upright", "italic"}
        and set(weights) == {w.name for w in spec.weights}
    )
    if full_matrix:
        gate = write_check_gate(font_root, spec, ft_mode=args.ft_mode, skipped_ots=args.skip_ots)
        print(f"已写入检查通过标记：{gate}")
    print(f"CHECK PASSED: 共 {checked} 个字体全部通过产品规则与二进制检查。")
    return 0


def _cmd_package(args: argparse.Namespace, spec: Spec) -> int:
    from .package import package_release

    root = repo_root()
    font_root = (args.root or (root / "fonts")).resolve()
    dist_dir = (args.dist or (root / "dist" / f"v{spec.version}")).resolve()
    manifest = package_release(
        font_root,
        dist_dir,
        project_root=root,
        require_check_gate=not args.skip_check_gate,
        spec=spec,
    )
    print(f"已打包并核验 {manifest['package_count']} 个发布 ZIP（耗时 {manifest['elapsed_sec']}s），输出：{dist_dir}")
    return 0


def _cmd_samples(args: argparse.Namespace, spec: Spec) -> int:
    import os
    from .samples import (
        render_region_comparison_sheet,
        render_windows_dwrite_comparison,
        render_windows_gdi_comparison,
    )

    if args.windows_only and os.name != "nt":
        print("ERROR: --windows-only 仅支持在 Windows 平台运行。", file=sys.stderr)
        return 1

    root = repo_root()
    cand_root = args.cand_root.resolve()
    ref_root = (args.ref_root or (root / "fonts")).resolve()
    ref_version = args.ref_version or spec.previous_version
    out_dir = (args.out or (root / ".build-cache" / "samples")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    regions = _parse_csv(args.regions, spec.region_order, flag="--regions")
    styles = _parse_csv(args.styles, ("upright", "italic"), flag="--styles")
    weights = _parse_csv(args.weights, [w.name for w in spec.weights], flag="--weights")

    def _ref_static(region: str, weight_name: str, italic: bool, hinted: bool) -> Path:
        d = spec.static_dir(ref_root, region, hinted=hinted, version=ref_version)
        return d / spec.static_filename(region, weight_name, italic)

    def _cand_static(region: str, weight_name: str, italic: bool, hinted: bool) -> Path:
        d = spec.static_dir(cand_root, region, hinted=hinted)
        return d / spec.static_filename(region, weight_name, italic)

    generated: list[Path] = []
    if not args.windows_only:
        for region in regions:
            for style in styles:
                italic = style == "italic"
                style_suf = "-Italic" if italic else ""
                for w_name in weights:
                    rows: list[tuple[str, Path, bool]] = []
                    c_hi = _cand_static(region, w_name, italic, hinted=True)
                    c_un = _cand_static(region, w_name, italic, hinted=False)
                    r_hi = _ref_static(region, w_name, italic, hinted=True)
                    tag = f"{region}-{w_name}{style_suf}"
                    if c_hi.is_file():
                        rows.append((f"New-{tag}(hi)", c_hi, True))
                    if r_hi.is_file():
                        rows.append((f"v{ref_version}-{tag}(hi)", r_hi, True))
                    if c_un.is_file():
                        rows.append((f"New-{tag}(un)", c_un, False))
                    if rows:
                        out = out_dir / f"ft-{tag}.png"
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
                win_pairs.append((f"v{ref_version}-SC-{tag}", r_hi, w.value, italic))
        if win_pairs:
            generated.append(render_windows_gdi_comparison(out_dir / "win-gdi-96dpi-SC.png", win_pairs))
            generated.append(render_windows_dwrite_comparison(out_dir / "win-dwrite-96dpi-SC.png", win_pairs))

    print(f"已生成 {len(generated)} 张验收样张：{out_dir}")
    for p in generated:
        print("  -", p.name)
    return 0


def _cmd_docs(args: argparse.Namespace, spec: Spec) -> int:
    from .docs_gen import check_doc_files, doc_paths, sync_doc_files

    paths = doc_paths(repo_root())
    if args.check:
        problems = check_doc_files(paths, spec)
        for msg in problems:
            print(f"ERROR: {msg}", file=sys.stderr)
        if problems:
            return 1
        print(f"OK: {len(paths)} 份文档中的规格表与 spec.toml 一致。")
        return 0
    try:
        changed = sync_doc_files(paths, spec)
    except ValueError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if changed:
        print("已同步：" + "、".join(p.name for p in changed))
    else:
        print("规格表已是最新，没有改动。")
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
    if args.command == "docs":
        return _cmd_docs(args, spec)
    raise SystemExit(f"未知子命令：{args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
