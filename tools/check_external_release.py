from __future__ import annotations

import argparse
import concurrent.futures
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from python_env_bootstrap import AUDIT_DEPENDENCIES, ensure_project_python, isolated_tool_python

if __name__ == "__main__":
    ensure_project_python(AUDIT_DEPENDENCIES, project_root=Path(__file__).resolve().parents[1], label="release")

from font_audit import contract as audit
import build_sarasa_ui_propdigits_sc as build
from fontTools.ttLib import TTFont


OTS_VERSION = "9.3.0"
OTS_ARCHIVES = {
    "Windows": "3e6c16678a0cb5c2401755bf13d7f7371d09e7f3348468b57da04b0ff23af01f",
    "Linux": "1caab4037806688203efd124b97443694794ca69a3dddd9a42b4833a4f8c4967",
    "Darwin": "b674ab7a57f54799d37c8f6d28c825f8292ec264c18b537241b367d6eef8d928",
}
CHECK_EXPECTED_COUNTS = {
    "opentype/font_version": 156,
    "no_mac_entries": 156,
    "opentype/STAT/ital_axis": 6,
    "name/family_and_style_max_length": 156,
    "smart_dropout": 156,
}
CHECKS = tuple(CHECK_EXPECTED_COUNTS)
# The whole check-universal profile also runs on representative fonts of each
# region, each paired with the pinned Sarasa Ui style it derives from (Heavy has
# none and pairs with Bold). A product FAIL passes only when it is listed here
# and the same check also FAILs on that release font; an ERROR, or a FAIL the
# release does not reproduce, fails the gate. WARN results are recorded.
# The italic VF is left to the per-font checks (STAT italic pairing): its
# interpolation_issues check alone takes about 16 minutes per VF.
UNIVERSAL_SAMPLES = ((None, False, "Regular", False), ("Regular", False, "Regular", False),
                     ("Regular", True, "Regular", True), ("Heavy", False, "Bold", False))
UNIVERSAL_PARALLEL_BATCHES = 3
APPROVED_UPSTREAM_FAILURES = {
    "base_has_width": "U+0488、U+0489（西里尔组合十万、百万符号）在固定 Sarasa Ui 中即为零步进。",
    "case_mapping": "固定 Sarasa Ui 缺少部分字母的大小写对应字形。",
    "file_size": "全字集 CJK 字体远超 FontBakery 为网页字体设定的 9 MB 上限。",
    "opentype/family/underline_thickness": "下划线粗细随字重变化：固定 Sarasa Ui 的 Regular、Italic 为 68，Bold 为 92；本项目 Heavy 为 113，同族检查因此 FAIL。",
}


def save_report(name: str, report: dict) -> None:
    report = build.sanitize_report_data(report)
    build.assert_portable_report_text(json.dumps(report, ensure_ascii=False))
    build.write_json_atomic(build.ROOT / "reports" / name, report)


def font_tables(path: Path) -> list[str]:
    with TTFont(path, lazy=True) as font:
        return sorted(tag for tag in font.keys() if tag != "GlyphOrder")


def ots_executable() -> tuple[Path, str]:
    system = platform.system()
    digest = OTS_ARCHIVES[system]
    archive_name = f"ots-{OTS_VERSION}-{'macOS' if system == 'Darwin' else system}.zip"
    directory = build.ROOT / ".build-cache" / "external-tools"
    archive = build.download_file_checked(
        f"https://github.com/khaledhosny/ots/releases/download/v{OTS_VERSION}/{archive_name}",
        directory / archive_name, digest,
    )
    destination = directory / "ots"
    with zipfile.ZipFile(archive) as zf:
        build.validate_zip_archive(zf, destination)
        for member in zf.infolist():
            if not member.is_dir():
                path = destination / member.filename
                build.verify_or_write_source(path, zf.read(member))
                if system != "Windows":
                    path.chmod((member.external_attr >> 16) & 0o777 or 0o755)
    name = "ots-sanitize.exe" if system == "Windows" else "ots-sanitize"
    matches = list(destination.rglob(name))
    if len(matches) != 1:
        raise RuntimeError("OTS 可执行文件清单不唯一")
    return matches[0], digest


def run_ots(manifest: dict) -> bool:
    executable, archive_digest = ots_executable()
    results = []; failures = []; unexpected = []
    with tempfile.TemporaryDirectory(prefix="sarasa-ots-") as temporary:
        output = Path(temporary) / "sanitized.ttf"
        for index, font in enumerate(audit.release_font_paths(), 1):
            print(f"[OTS {index}/156] {font.name}", flush=True)
            output.unlink(missing_ok=True)
            process = subprocess.run([str(executable), str(font), str(output)], capture_output=True, text=True)
            tables_before = font_tables(font)
            tables_after = font_tables(output) if output.exists() and process.returncode == 0 else []
            item = {"font": audit.display_path(font), "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr, "missing_tables": sorted(set(tables_before) - set(tables_after))}
            results.append(item)
            if process.returncode or item["missing_tables"]:
                failures.append(item)
            if process.stdout.strip() != "File sanitized successfully!" or process.stderr.strip():
                unexpected.append(item)
    unchanged = audit.audit_input_manifest() == manifest
    report = {"title": "OTS 发布检查", "generated_at": datetime.now(timezone.utc).isoformat(), "tool": "OpenType Sanitizer", "version": OTS_VERSION, "archive_sha256": archive_digest, "input_manifest": manifest, "inputs_unchanged": unchanged, "fonts_checked": len(results), "successful": len(results) - len(failures), "failures": failures, "unexpected_messages": unexpected, "results": results}
    if not unchanged:
        report["failures"].append({"reason": "检查期间字体发生变化"})
    save_report("ots-audit.json", report)
    return unchanged and not failures and not unexpected and len(results) == 156


def run_fontbakery(manifest: dict) -> bool:
    python, environment = isolated_tool_python(
        Path(os.environ.get("SARASA_FONTBAKERY_VENV", build.ROOT / ".build-cache" / "fontbakery-venv")),
        build.ROOT / "requirements-fontbakery.lock",
        {"fontbakery": "1.1.0", "freetype-py": "2.3.0", "fonttools": "4.67.0"},
    )
    directory = build.ROOT / ".build-cache" / "fontbakery"
    directory.mkdir(parents=True, exist_ok=True)
    batches = {}; records = {}; totals = Counter(); selected = {check: Counter() for check in CHECKS}
    for region in build.REGION_ORDER:
        fonts = [audit.vf_path(region, italic) for italic in (False, True)]
        fonts.extend(audit.static_path(region, str(stop["name"]), italic, hinted) for stop in build.SOURCE_HAN_WEIGHT_STOPS for italic in (False, True) for hinted in (False, True))
        if len(fonts) != 26:
            raise RuntimeError(f"{region} FontBakery 批次必须恰好包含 26 个字体，实际 {len(fonts)}")
        native = directory / f"{region}.json"
        # A run that dies before writing its JSON must not leave the last
        # run's results to be read in its place.
        native.unlink(missing_ok=True)
        arguments = [str(python), "-m", "fontbakery", "check-universal", "-J", "4", "--json", str(native)]
        for check in CHECKS:
            arguments.extend(["-c", check])
        arguments.extend(audit.display_path(font) for font in fonts)
        print(f"[FontBakery] {region}，26 个字体，-J 4", flush=True)
        with (directory / f"{region}.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(arguments, cwd=build.ROOT, stdout=log, stderr=subprocess.STDOUT)
        if not native.is_file():
            raise RuntimeError(f"FontBakery {region} 批次没有写出 JSON（退出码 {process.returncode}）")
        document = json.loads(native.read_text(encoding="utf-8"))
        counts = {key: value for key, value in document["result"].items() if value}
        totals.update(counts)
        batches[region] = {"fonts": 26, "jobs": 4, "exit_code": process.returncode, "result_counts": counts}
        records[region] = []
        for section in document["sections"]:
            for check in section["checks"]:
                identifier = check["key"][1]
                match = re.fullmatch(r"<FontBakeryCheck:(.+)>", identifier)
                identifier = match.group(1) if match else identifier
                selected[identifier][check["result"]] += 1
                records[region].append({"check": identifier, "font": check.get("filename"), "result": check["result"], "logs": check["logs"]})
    universal = run_fontbakery_universal(python, directory)
    unchanged = audit.audit_input_manifest() == manifest
    expected_results = {name: {"PASS": count} for name, count in CHECK_EXPECTED_COUNTS.items()}
    success = (unchanged and dict(totals) == {"PASS": sum(CHECK_EXPECTED_COUNTS.values())}
               and {name: dict(value) for name, value in selected.items()} == expected_results
               and all(batch["exit_code"] == 0 for batch in batches.values()))
    report = {"title": "FontBakery 分地区发布检查", "generated_at": datetime.now(timezone.utc).isoformat(), "toolchain": environment["versions"], "environment": environment, "input_manifest": manifest, "inputs_unchanged": unchanged, "release_gate": {"fonts": 156, "selected_checks": {name: dict(value) for name, value in selected.items()}, "per_region_batches": batches, "total_result_counts": dict(totals), "all_batches_exit_zero": all(batch["exit_code"] == 0 for batch in batches.values()), "passed": success}, "results": records}
    report["universal"] = universal
    report["approved_upstream_failures"] = universal["approved_upstream_failures"]
    save_report("fontbakery-audit.json", report)
    return success and universal["passed"]


def fontbakery_results(document: dict) -> list[dict]:
    rows = []
    for section in document["sections"]:
        for check in section["checks"]:
            identifier = check["key"][1]
            match = re.fullmatch(r"<FontBakeryCheck:(.+)>", identifier)
            messages = []
            for log in check["logs"]:
                if log["status"] in ("FAIL", "ERROR", "FATAL", "WARN"):
                    message = log["message"]
                    text = message.get("message", str(message)) if isinstance(message, dict) else str(message)
                    messages.append(text[:400])
            rows.append({"check": match.group(1) if match else identifier, "font": check.get("filename"),
                         "result": check["result"], "messages": messages})
    return rows


def run_fontbakery_universal(python: Path, directory: Path) -> dict:
    """check-universal in full on samples and their pinned counterparts."""
    regions = {}
    approvals = []
    unapproved = []
    plans = {}
    for region in build.REGION_ORDER:
        products, counterparts = [], {}
        for weight, italic, release_weight, release_italic in UNIVERSAL_SAMPLES:
            path = audit.vf_path(region, italic) if weight is None else audit.static_path(region, weight, italic, True)
            products.append(path)
            counterparts[audit.display_path(path)] = audit.reference_path(region, release_weight, release_italic, True)
        plans[region] = (products, counterparts)

    def run_batch(region: str, kind: str, fonts: list[Path]) -> dict:
        native = directory / f"universal-{region}-{kind}.json"
        native.unlink(missing_ok=True)
        # Release fonts live outside the repository; pass real paths and
        # report the portable names, in messages too (single_directory and
        # underline_thickness list every file, some as a Python repr).
        names = {str(font.resolve()): audit.display_path(font) for font in fonts}
        # FontBakery's JSON names each font by its file name only.
        by_name = {font.name: audit.display_path(font) for font in fonts}
        if len(by_name) != len(fonts):
            raise RuntimeError(f"FontBakery universal {region} {kind} 批次的文件名不唯一")
        portable = {}
        for real, shown in names.items():
            for old, new in ((real, shown), (str(Path(real).parent), shown.rsplit("/", 1)[0])):
                portable[old] = new
                portable[old.replace("\\", "\\\\")] = new
        arguments = [str(python), "-m", "fontbakery", "check-universal", "-J", "4", "--json", str(native), *names]
        print(f"[FontBakery universal] {region} {kind}，{len(fonts)} 个字体", flush=True)
        with (directory / f"universal-{region}-{kind}.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(arguments, cwd=build.ROOT, stdout=log, stderr=subprocess.STDOUT)
        # Exit 1 means a FAIL or worse among the results; anything else, or no
        # fresh JSON, is a broken run.
        if process.returncode not in (0, 1) or not native.is_file():
            raise RuntimeError(f"FontBakery universal {region} {kind} 运行失败（退出码 {process.returncode}）")
        rows = fontbakery_results(json.loads(native.read_text(encoding="utf-8")))
        for row in rows:
            if row["font"]:
                shown = by_name.get(Path(row["font"]).name)
                if shown is None:
                    raise RuntimeError(f"FontBakery universal 的结果含有本批次之外的字体：{row['font']}")
                row["font"] = shown
        reported = {row["font"] for row in rows if row["font"]}
        if reported != set(by_name.values()):
            raise RuntimeError(f"FontBakery universal {region} {kind} 的结果没有覆盖本批次全部字体")
        # Messages name the fonts by their real paths (single_directory and
        # underline_thickness list every file). This loop sat under the raise
        # above and never ran until v1.0.43.3.
        for row in rows:
            for old, new in sorted(portable.items(), key=lambda item: -len(item[0])):
                row["messages"] = [message.replace(old, new) for message in row["messages"]]
        return {"exit_code": process.returncode, "rows": rows}

    def product_batches(products: list[Path]) -> list[tuple[str, list[Path]]]:
        # The VF and the statics are different families: run as one batch,
        # family checks (single_directory, consistent and typographic family
        # names, VF axis ranges) failed on the mixture, not on either.
        variable = [path for path in products if path.parent == audit.vf_path("SC", False).parent]
        static = [path for path in products if path not in variable]
        return [("product-variable", variable), ("product-static", static)]

    jobs = [(region, kind, fonts) for region, (products, counterparts) in plans.items()
            for kind, fonts in (*product_batches(products), ("release", sorted(set(counterparts.values()))))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=UNIVERSAL_PARALLEL_BATCHES) as pool:
        finished = dict(zip([(region, kind) for region, kind, _fonts in jobs],
                            pool.map(lambda job: run_batch(*job), jobs)))
    for region, (products, counterparts) in plans.items():
        batches = {"product": {"rows": [row for kind in ("product-variable", "product-static")
                                        for row in finished[(region, kind)]["rows"]]},
                   "release": finished[(region, "release")]}
        release_results = {(row["check"], row["font"]): row["result"] for row in batches["release"]["rows"]}
        counts = Counter(row["result"] for row in batches["product"]["rows"])
        for row in batches["product"]["rows"]:
            if row["result"] not in ("FAIL", "ERROR", "FATAL"):
                continue
            if row["font"] is None:
                # A family check runs on the whole batch: its counterpart is
                # the same check on the pinned release batch.
                counterpart = "family:" + ",".join(sorted({audit.display_path(path) for path in counterparts.values()}))
                reproduced = release_results.get((row["check"], None))
            else:
                upstream = counterparts.get(row["font"])
                if upstream is None:
                    raise RuntimeError(f"FontBakery universal 的产品字体没有上游对应字体：{row['font']}")
                counterpart = audit.display_path(upstream)
                reproduced = release_results.get((row["check"], counterpart))
            item = {"region": region, "check": row["check"], "font": row["font"], "result": row["result"],
                    "counterpart": counterpart, "counterpart_result": reproduced, "messages": row["messages"]}
            if row["result"] == "FAIL" and row["check"] in APPROVED_UPSTREAM_FAILURES and reproduced == "FAIL":
                approvals.append({**item, "reason": APPROVED_UPSTREAM_FAILURES[row["check"]]})
            else:
                unapproved.append(item)
        regions[region] = {"product_fonts": [audit.display_path(path) for path in products],
                           "release_fonts": sorted({audit.display_path(path) for path in counterparts.values()}),
                           "product_result_counts": dict(counts),
                           "warnings": sorted({row["check"] for row in batches["product"]["rows"]
                                               if row["result"] == "WARN"})}
    return {"fonts": sum(len(region["product_fonts"]) for region in regions.values()), "regions": regions,
            "approved_upstream_failures": approvals, "unapproved_failures": unapproved, "passed": not unapproved}


def main() -> None:
    parser = argparse.ArgumentParser(description="逐字体运行 OTS，并按地区以 26 字体、-J 4 运行 FontBakery 发布门。")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ots-only", action="store_true", help="只运行 OTS。")
    group.add_argument("--fontbakery-only", action="store_true", help="只运行 FontBakery。")
    args = parser.parse_args()
    manifest = audit.audit_input_manifest()
    if len(manifest) != 156 or any(item.get("missing") for item in manifest.values()):
        parser.error("必须先准备完整的 156 个发布字体")
    success = True
    if not args.fontbakery_only:
        success = run_ots(manifest) and success
    if not args.ots_only:
        success = run_fontbakery(manifest) and success
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
