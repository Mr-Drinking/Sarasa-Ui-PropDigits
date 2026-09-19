from __future__ import annotations

import argparse
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

import audit_sarasa_ui_propdigits as audit
import build_sarasa_ui_propdigits_sc as build
from python_env_bootstrap import isolated_tool_python
from fontTools.ttLib import TTFont


OTS_VERSION = "9.3.0"
OTS_ARCHIVES = {
    "Windows": "3e6c16678a0cb5c2401755bf13d7f7371d09e7f3348468b57da04b0ff23af01f",
    "Linux": "1caab4037806688203efd124b97443694794ca69a3dddd9a42b4833a4f8c4967",
    "Darwin": "b674ab7a57f54799d37c8f6d28c825f8292ec264c18b537241b367d6eef8d928",
}
CHECKS = ("opentype/font_version", "no_mac_entries", "opentype/STAT/ital_axis")


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
        build.ROOT / "requirements-fontbakery.txt",
        {"fontbakery": "1.1.0", "freetype-py": "2.3.0", "fonttools": "4.63.0"},
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
        arguments = [str(python), "-m", "fontbakery", "check-universal", "-J", "4", "--json", str(native)]
        for check in CHECKS:
            arguments.extend(["-c", check])
        arguments.extend(audit.display_path(font) for font in fonts)
        print(f"[FontBakery] {region}，26 个字体，-J 4", flush=True)
        with (directory / f"{region}.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(arguments, cwd=build.ROOT, stdout=log, stderr=subprocess.STDOUT)
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
    unchanged = audit.audit_input_manifest() == manifest
    success = unchanged and dict(totals) == {"PASS": 318} and all(batch["exit_code"] == 0 for batch in batches.values())
    report = {"title": "FontBakery 分地区发布检查", "generated_at": datetime.now(timezone.utc).isoformat(), "toolchain": environment["versions"], "environment": environment, "input_manifest": manifest, "inputs_unchanged": unchanged, "release_gate": {"fonts": 156, "selected_checks": {name: dict(value) for name, value in selected.items()}, "per_region_batches": batches, "total_result_counts": dict(totals), "all_batches_exit_zero": all(batch["exit_code"] == 0 for batch in batches.values()), "passed": success}, "results": records}
    save_report("fontbakery-audit.json", report)
    return success


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
