from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_sarasa_ui_propdigits_sc as build  # noqa: E402


ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REQUIRED_DOCUMENTS = ("LICENSE.txt", "NOTICE.md", "README.md")
EXPECTED_EXTERNAL_ATTR = 0o100644 << 16
SOURCE_DIGEST_CACHE: dict[tuple[Path, int, int], str] = {}


@dataclass(frozen=True)
class Package:
    filename: str
    files: tuple[tuple[Path, str], ...]


def documentation_files() -> tuple[tuple[Path, str], ...]:
    return (
        (ROOT / "LICENSE", "LICENSE.txt"),
        (ROOT / "NOTICE.md", "NOTICE.md"),
        (ROOT / "README.md", "README.md"),
    )


def directory_files(directory: Path, prefix: str = "") -> tuple[tuple[Path, str], ...]:
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    return tuple(
        (path, f"{prefix}{path.relative_to(directory).as_posix()}")
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


def variable_files(region: str | None = None) -> tuple[tuple[Path, str], ...]:
    regions = [region] if region else list(build.REGION_ORDER)
    files = []
    for current_region in regions:
        for italic in (False, True):
            path = build.VARIABLE_DIR / build.variable_output_name(current_region, italic)
            files.append((path, path.name))
    return tuple(files)


def static_files(region: str, hinted: bool, prefix: str = "") -> tuple[tuple[Path, str], ...]:
    directory = build.static_dir(region, hinted)
    files = directory_files(directory, prefix)
    expected = {
        build.static_output_name(region, str(stop["name"]), italic)
        for stop in build.SOURCE_HAN_WEIGHT_STOPS
        for italic in (False, True)
    }
    actual = {
        path.relative_to(directory).as_posix()
        for path, _name in files
        if path.suffix.lower() == ".ttf"
    }
    if actual != expected:
        raise ValueError(
            f"静态字体清单不完整：{directory.name}；"
            f"缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}"
        )
    return files


def release_packages() -> list[Package]:
    version = build.VERSION
    docs = documentation_files()
    packages: list[Package] = []
    for region in build.REGION_ORDER:
        packages.append(
            Package(
                f"Sarasa-Ui-VF-PropDigits-{region}-TTF-{version}.zip",
                docs + variable_files(region),
            )
        )
        hinted_dir = build.static_dir(region, True)
        unhinted_dir = build.static_dir(region, False)
        packages.append(
            Package(
                f"SarasaUiPropDigits{region}-TTF-{version}.zip",
                docs + static_files(region, True),
            )
        )
        packages.append(
            Package(
                f"SarasaUiPropDigits{region}-TTF-Unhinted-{version}.zip",
                docs + static_files(region, False),
            )
        )

    packages.append(
        Package(
            f"Sarasa-Ui-VF-PropDigits-TTF-{version}.zip",
            docs + variable_files(),
        )
    )
    for hinted in (True, False):
        variant = "TTF" if hinted else "TTF-Unhinted"
        files = list(docs)
        for region in build.REGION_ORDER:
            directory = build.static_dir(region, hinted)
            files.extend(static_files(region, hinted, f"{directory.name}/"))
        packages.append(
            Package(
                f"SarasaUiPropDigits-{variant}-{version}.zip",
                tuple(files),
            )
        )
    if len(packages) != 21:
        raise AssertionError(f"expected 21 release packages, got {len(packages)}")
    return packages


def zip_info(archive_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_name, ZIP_TIMESTAMP)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def package_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_sha256(path: Path) -> str:
    stat = path.stat()
    key = (path.resolve(), stat.st_size, stat.st_mtime_ns)
    if key not in SOURCE_DIGEST_CACHE:
        SOURCE_DIGEST_CACHE[key] = package_sha256(path)
    return SOURCE_DIGEST_CACHE[key]


def archive_member_sha256(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package_sources(package: Package) -> None:
    names = [archive_name for _path, archive_name in package.files]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"duplicate ZIP members in {package.filename}: {duplicates}")
    missing_documents = sorted(set(REQUIRED_DOCUMENTS) - set(names))
    if missing_documents:
        raise ValueError(
            f"{package.filename} lacks required documents: {missing_documents}"
        )
    if not any(path.suffix.lower() == ".ttf" for path, _name in package.files):
        raise ValueError(f"{package.filename} 不包含字体")
    for path, archive_name in package.files:
        if not path.is_file():
            raise FileNotFoundError(path)
        build.validate_archive_member_paths([archive_name], ROOT)


def validate_visual_report(visual: dict, manifest: dict) -> None:
    import audit_sarasa_ui_propdigits as audit

    if visual.get("complete") is not True or visual.get("passed") is not True or visual.get("review_required") is not False:
        raise ValueError("视觉检查尚未完整通过")
    if visual.get("input_manifest") != manifest:
        raise ValueError("视觉报告的字体 SHA-256 不匹配")
    if visual.get("fonts_reviewed") != 156 or sorted(visual.get("reviewed_fonts", [])) != sorted(manifest):
        raise ValueError("视觉报告缺少准确的 156 个已审阅字体清单")
    if sorted(visual.get("regions", [])) != sorted(build.REGION_ORDER):
        raise ValueError("视觉报告没有覆盖全部六地区")
    generator = "tools/render_visual_checks.py"
    if visual.get("generator") != generator or visual.get("generator_sha256") != package_sha256(ROOT / generator):
        raise ValueError("视觉生成器 SHA-256 不匹配")

    def verify_image(item: dict, *, expected_png: bool = False) -> None:
        name = item.get("file", "")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or not name.startswith("assets/checks/"):
            raise ValueError("视觉图片路径无效")
        path = ROOT / relative
        if not path.resolve().is_relative_to((ROOT / "assets" / "checks").resolve()) or not path.is_file():
            raise ValueError("视觉图片不存在或位于检查目录之外")
        if item.get("reviewed") is not True or item.get("passed") is not True:
            raise ValueError("视觉图片尚未审阅通过")
        if item.get("sha256") != package_sha256(path):
            raise ValueError("视觉图片 SHA-256 不匹配")
        with path.open("rb") as stream:
            header = stream.read(8)
            if header != b"\x89PNG\r\n\x1a\n" and (expected_png or not header.startswith(b"\xff\xd8\xff")):
                raise ValueError("视觉图片格式无效")

    images = visual.get("images", [])
    expected_names = {f"assets/checks/{region}-{kind}.png" for region in build.REGION_ORDER for kind in ("static", "variable", "detail")}
    if len(images) != 18 or {item.get("file") for item in images} != expected_names:
        raise ValueError("视觉报告必须准确包含六地区的 18 张样张")
    reviewed = set()
    for item in images:
        verify_image(item, expected_png=True)
        if not item.get("observations") or not all(isinstance(value, str) and value.strip() for value in item["observations"]):
            raise ValueError("视觉图片缺少具体审阅记录")
        region = item.get("region")
        if region not in build.REGION_ORDER or not item["file"].startswith(f"assets/checks/{region}-"):
            raise ValueError("视觉图片地区不匹配")
        cases = item.get("cases", [])
        if item["file"].endswith("-static.png"):
            expected = {(audit.display_path(audit.static_path(region, str(stop["name"]), italic, hinted)), int(stop["value"]), italic, hinted) for stop in build.SOURCE_HAN_WEIGHT_STOPS for italic in (False, True) for hinted in (False, True)}
            actual = [(case.get("font"), case.get("weight"), case.get("italic"), case.get("hinted")) for case in cases]
        elif item["file"].endswith("-variable.png"):
            expected = {(audit.display_path(audit.vf_path(region, italic)), weight, italic, None) for weight in audit.INTER_POSITION_WEIGHTS for italic in (False, True)}
            actual = [(case.get("font"), case.get("weight"), case.get("italic"), case.get("hinted")) for case in cases]
        else:
            expected = {(audit.display_path(audit.vf_path(region, False)), weight) for weight in (200, 400, 600, 900)}
            actual = [(case.get("font"), case.get("weight")) for case in cases]
        if len(actual) != len(expected) or set(actual) != expected or any(not isinstance(case.get("pixels"), int) or case["pixels"] <= 0 for case in cases):
            raise ValueError("视觉图片的字体、字重或样式覆盖不完整")
        reviewed.update(case[0] for case in actual)
    if reviewed != set(manifest):
        raise ValueError("视觉图片没有覆盖全部成品")
    for screenshot in visual.get("runtime_screenshots", []):
        verify_image(screenshot)


def validate_release_audits(packages: list[Package]) -> None:
    import audit_sarasa_ui_propdigits as audit

    paths = {path for package in packages for path, _name in package.files if path.suffix.lower() == ".ttf"}
    if paths != set(audit.release_font_paths()) or len(paths) != 156:
        raise ValueError("发布字体清单必须完整包含 12 个 VF 和 144 个静态 TTF")
    manifest = audit.audit_input_manifest()
    reports = {}
    for name in ("release-audit", "ots-audit", "fontbakery-audit", "visual-audit"):
        report_path = ROOT / "reports" / f"{name}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("input_manifest") != manifest:
            raise ValueError(f"{name} 未覆盖当前全部字体，或字体 SHA-256 已改变")
        build.assert_portable_report_text(json.dumps(report, ensure_ascii=False))
        reports[name] = report
    main_report = reports["release-audit"]
    gate = main_report.get("gate", {})
    expected_sections = set(audit.AUDIT_GATE_SECTIONS)
    if (
        gate.get("complete") is not True
        or gate.get("passed") is not True
        or gate.get("total_failures") != 0
        or gate.get("skipped_sections") != 0
        or gate.get("executed_sections") != len(expected_sections)
        or set(gate.get("sections", {})) != expected_sections
        or any(item != {"status": "passed", "failure_count": 0} for item in gate["sections"].values())
        or main_report.get("audit_contract") != audit.audit_contract_manifest()
    ):
        raise ValueError("当前构建与审计代码没有完整通过主审计")
    ots = reports["ots-audit"]
    if ots.get("fonts_checked") != 156 or ots.get("successful") != 156 or ots.get("failures") or ots.get("unexpected_messages"):
        raise ValueError("OTS 尚未完整通过 156 个字体")
    fb = reports["fontbakery-audit"].get("release_gate", {})
    if fb.get("total_result_counts") != {"PASS": 318} or fb.get("all_batches_exit_zero") is not True:
        raise ValueError("FontBakery 发布门尚未取得 318 PASS")
    if fb.get("selected_checks") != {"opentype/font_version": {"PASS": 156}, "no_mac_entries": {"PASS": 156}, "opentype/STAT/ital_axis": {"PASS": 6}}:
        raise ValueError("FontBakery 的逐字体及正斜体配对检查覆盖不完整")
    batches = fb.get("per_region_batches", {})
    if set(batches) != set(build.REGION_ORDER) or any(batch.get("fonts") != 26 or batch.get("jobs") != 4 for batch in batches.values()):
        raise ValueError("FontBakery 必须按六地区、每批 26 字体、-J 4 运行")
    environment = reports["fontbakery-audit"].get("environment", {})
    if environment.get("versions", {}).get("freetype-py") != "2.3.0" or environment.get("pip_check") != {"returncode": 0, "passed": True}:
        raise ValueError("FontBakery 独立环境尚未通过固定依赖与 pip check 检查")
    validate_visual_report(reports["visual-audit"], manifest)


def write_package(package: Package, output_dir: Path) -> dict[str, object]:
    validate_package_sources(package)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / package.filename
    pending = output.with_suffix(output.suffix + ".tmp")
    pending.unlink(missing_ok=True)
    with zipfile.ZipFile(
        pending,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for source, archive_name in sorted(package.files, key=lambda item: item[1]):
            archive.writestr(
                zip_info(archive_name),
                source.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    pending.replace(output)
    verify_package(output, package)
    return {
        "file": output.name,
        "bytes": output.stat().st_size,
        "sha256": package_sha256(output),
        "members": len(package.files),
    }


def verify_package(path: Path, package: Package) -> None:
    expected = sorted(archive_name for _source, archive_name in package.files)
    sources = {archive_name: source for source, archive_name in package.files}
    with zipfile.ZipFile(path) as archive:
        actual = archive.namelist()
        if actual != expected:
            raise ValueError(f"ZIP member order/content mismatch in {path}")
        if archive.testzip() is not None:
            raise ValueError(f"ZIP CRC validation failed in {path}")
        for info in archive.infolist():
            if info.date_time != ZIP_TIMESTAMP:
                raise ValueError(f"non-reproducible ZIP timestamp in {path}: {info.filename}")
            if info.external_attr != EXPECTED_EXTERNAL_ATTR:
                raise ValueError(f"unexpected ZIP permissions in {path}: {info.filename}")
            if info.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError(f"unexpected ZIP compression in {path}: {info.filename}")
            source = sources[info.filename]
            if info.file_size != source.stat().st_size:
                raise ValueError(f"ZIP member size mismatch in {path}: {info.filename}")
            if archive_member_sha256(archive, info.filename) != source_sha256(source):
                raise ValueError(f"ZIP member SHA-256 mismatch in {path}: {info.filename}")
        missing_documents = sorted(set(REQUIRED_DOCUMENTS) - set(actual))
        if missing_documents:
            raise ValueError(f"{path} lacks required documents: {missing_documents}")


def write_checksums(output_dir: Path, results: list[dict[str, object]]) -> Path:
    path = output_dir / "SHA256SUMS.txt"
    lines = [f"{item['sha256']}  {item['file']}" for item in sorted(results, key=lambda item: str(item["file"]))]
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成并核验 21 个可复现 Release ZIP。")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist" / f"v{build.VERSION}",
        help="ZIP 输出目录。",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只核验输出目录中已有的 21 个 ZIP。",
    )
    args = parser.parse_args()
    packages = release_packages()
    for package in packages:
        validate_package_sources(package)
    validate_release_audits(packages)
    results: list[dict[str, object]] = []
    for index, package in enumerate(packages, start=1):
        output = args.output_dir / package.filename
        print(f"[package {index:02d}/21] {package.filename}", flush=True)
        if args.verify_only:
            validate_package_sources(package)
            verify_package(output, package)
            results.append(
                {
                    "file": output.name,
                    "bytes": output.stat().st_size,
                    "sha256": package_sha256(output),
                    "members": len(package.files),
                }
            )
        else:
            results.append(write_package(package, args.output_dir))
    expected_archives = {package.filename for package in packages}
    actual_archives = {path.name for path in args.output_dir.glob("*.zip")}
    if actual_archives != expected_archives:
        raise ValueError("输出目录中的 ZIP 清单必须恰好等于本版 21 个发布包")
    # Bind the completed archives to the same audited fonts even if a source
    # file changed while the packages were being compressed.
    validate_release_audits(packages)
    checksums = write_checksums(args.output_dir, results)
    print(f"[package] wrote {checksums}", flush=True)


if __name__ == "__main__":
    main()
