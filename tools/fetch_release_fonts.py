"""Fetch a published release's fonts into fonts/, verified against its manifest.

Fonts are not tracked in git: a checkout carries reports/release-packages.json,
which names every ZIP of its release with size and SHA-256 and every font with
its SHA-256. This first asks GitHub whether that release is published and
checks the release's assets against the manifest: all 21 names, sizes and
digests. It then downloads the three all-region packages (with
--all-packages, all 21, each checked member by member), verifies every byte
before use, and only then extracts the 156 fonts with their static directory
documents. Existing files are only accepted when identical.

Exit codes: 0 verified, 3 the release is not published (the release commit's
own CI run), anything else an error. A published release that lacks or
changed an asset is an error, never "not published".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Mr-Drinking/Sarasa-Ui-PropDigits"
API = "https://api.github.com"
CHUNK = 1024 * 1024
# A release that is not published yet (the release commit's own CI run) exits
# with this code, so CI can tell it from a broken download.
NOT_PUBLISHED = 3
PACKAGE_DOCUMENTS = ("LICENSE.txt", "NOTICE.md", "README.md")
STATIC_DOCUMENTS = ("README.txt", "LICENSE-Sarasa-Gothic.txt")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def aggregate_packages(version: str) -> list[str]:
    return [f"Sarasa-Ui-VF-PropDigits-TTF-{version}.zip", f"SarasaUiPropDigits-TTF-{version}.zip",
            f"SarasaUiPropDigits-TTF-Unhinted-{version}.zip"]


def api_request(url: str) -> dict | None:
    """The JSON at url, or None for 404; other failures retry, then stop the run."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sarasa-ui-propdigits-fetch"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            print(f"[fetch] GitHub API {error.code}: {url}", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"[fetch] {type(error).__name__}: {error}", flush=True)
        time.sleep(2 ** attempt)
    raise SystemExit(f"无法查询 Release：{url}")


def published_assets(api: str, version: str) -> dict[str, dict] | None:
    """The published release's assets by name, or None while it is a draft or absent."""
    release = api_request(f"{api}/repos/{REPOSITORY}/releases/tags/v{version}")
    if release is None or release.get("draft"):
        return None
    return {asset["name"]: asset for asset in release.get("assets", [])}


def check_assets(assets: dict[str, dict], packages: dict[str, dict]) -> None:
    """Every manifest package is an uploaded asset of the same size and digest, and nothing else is."""
    problems = []
    for name, item in sorted(packages.items()):
        asset = assets.get(name)
        if asset is None:
            problems.append(f"缺少 {name}")
            continue
        if asset.get("state", "uploaded") != "uploaded" or asset.get("size") != item["bytes"]:
            problems.append(f"{name} 的状态或大小与发布清单不符")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{item['sha256']}":
            problems.append(f"{name} 的 SHA-256 与发布清单不符")
    problems.extend(f"多出未列入发布清单的资产 {name}" for name in sorted(set(assets) - set(packages)))
    if problems:
        raise SystemExit("Release 资产与发布清单不一致：" + "；".join(problems))


def download(url: str, target: Path, size: int, digest: str) -> None:
    """Only bytes of the declared size and digest ever reach the target path."""
    if target.is_file() and target.stat().st_size == size and sha256(target) == digest:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", suffix=".part", dir=target.parent)
        os.close(descriptor)
        partial = Path(temporary)
        try:
            print(f"[fetch] {url}（第 {attempt + 1}/3 次）", flush=True)
            with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as stream:
                while chunk := response.read(CHUNK):
                    stream.write(chunk)
            if partial.stat().st_size == size and sha256(partial) == digest:
                partial.replace(target)
                return
            print(f"[fetch] {target.name} 的大小或 SHA-256 与发布清单不符", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            # The release was confirmed published: a missing asset is an error.
            print(f"[fetch] {type(error).__name__}: {error}", flush=True)
        finally:
            partial.unlink(missing_ok=True)
        time.sleep(2 ** attempt)
    raise SystemExit(f"无法取得与发布清单一致的 {target.name}")


def package_members(name: str, version: str, manifest: dict) -> dict[str, str | tuple[str, str]]:
    """Each member of a release package with its manifest SHA-256.

    A static directory's README.txt and license copy are not in the manifest;
    they map to (directory, document) and must be the same bytes in every
    package that carries that directory.
    """
    members: dict[str, str | tuple[str, str]] = dict(manifest["documentation_sha256"])
    variable: dict[str, str] = {}
    static: dict[str, dict[str, str]] = {}
    for path, digest in manifest["font_sha256"].items():
        parts = PurePosixPath(path).parts
        if parts[:2] == ("fonts", "variable") and len(parts) == 3:
            variable[parts[2]] = digest
        elif parts[:2] == ("fonts", "static") and len(parts) == 4:
            static.setdefault(parts[2], {})[parts[3]] = digest
        else:
            raise SystemExit(f"发布清单中的字体路径无效：{path}")
    if name == f"Sarasa-Ui-VF-PropDigits-TTF-{version}.zip":
        members.update(variable)
    elif name.startswith("Sarasa-Ui-VF-PropDigits-") and name.endswith(f"-TTF-{version}.zip"):
        region = name.removeprefix("Sarasa-Ui-VF-PropDigits-").removesuffix(f"-TTF-{version}.zip")
        members.update({file: digest for file, digest in variable.items()
                        if file in (f"Sarasa-Ui-VF-PropDigits-{region}[wght].ttf",
                                    f"Sarasa-Ui-VF-PropDigits-{region}-Italic[wght].ttf")})
    elif name in (f"SarasaUiPropDigits-TTF-{version}.zip", f"SarasaUiPropDigits-TTF-Unhinted-{version}.zip"):
        unhinted = "-Unhinted-" in name
        for directory, files in static.items():
            if ("-Unhinted-" in directory) == unhinted:
                members.update({f"{directory}/{file}": digest for file, digest in files.items()})
                members.update({f"{directory}/{document}": (directory, document) for document in STATIC_DOCUMENTS})
    else:
        directory = name.removesuffix(".zip")
        if directory not in static:
            raise SystemExit(f"发布清单中没有 {name} 对应的静态目录")
        members.update(static[directory])
        members.update({document: (directory, document) for document in STATIC_DOCUMENTS})
    return members


def verify_package_members(archive: Path, name: str, version: str, manifest: dict, item: dict,
                           documents: dict[tuple[str, str], str]) -> None:
    """A package holds exactly its manifest members, each with its manifest bytes."""
    expected = package_members(name, version, manifest)
    with zipfile.ZipFile(archive) as bundle:
        names = [member.filename for member in bundle.infolist() if not member.is_dir()]
        if sorted(names) != sorted(expected) or len(names) != item.get("members", len(names)):
            raise SystemExit(f"{name} 的成员与发布清单不符：缺少 {sorted(set(expected) - set(names))[:3]}，"
                             f"多出 {sorted(set(names) - set(expected))[:3]}")
        if bundle.testzip() is not None:
            raise SystemExit(f"{name} 的 CRC 校验失败")
        for member in names:
            digest = member_sha256(bundle, member)
            wanted = expected[member]
            if isinstance(wanted, tuple):
                wanted = documents.setdefault(wanted, digest)
            if digest != wanted:
                raise SystemExit(f"{name} 中 {member} 的 SHA-256 与发布清单或其他包中的同一文件不一致")


def member_sha256(bundle: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    """SHA-256 of a ZIP member, read in chunks."""
    hasher = hashlib.sha256()
    with bundle.open(member) as stream:
        while chunk := stream.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="从 GitHub Release 取回本检出对应版本的全部字体，按发布清单核验后解压到 fonts/。")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="目标检出（默认本仓库）；复核旧版时指向检出该 tag 的工作树")
    parser.add_argument("--packages", type=Path, help="发布清单（默认取目标检出的 reports/release-packages.json）")
    parser.add_argument("--downloads", type=Path, help="ZIP 下载目录（默认目标检出的 .build-cache/release-downloads/v<版本>）")
    parser.add_argument("--base-url", default=None, help="Release 下载地址前缀（默认 GitHub 上该版本的 tag）")
    parser.add_argument("--api-url", default=API, help="GitHub API 地址（默认 https://api.github.com）")
    parser.add_argument("--all-packages", action="store_true",
                        help="下载并逐成员核验全部 21 个包，而不只是解压字体所需的三个全地区包")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((args.packages or root / "reports" / "release-packages.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    packages = {item["file"]: item for item in manifest["packages"]}
    if len(packages) != manifest.get("package_count", len(packages)):
        raise SystemExit("发布清单的包数与记录不符")
    fonts = manifest["font_sha256"]
    assets = published_assets(args.api_url.rstrip("/"), version)
    if assets is None:
        print(f"[fetch] v{version} 的 Release 尚未发布（不存在或仍是草稿）", flush=True)
        raise SystemExit(NOT_PUBLISHED)
    check_assets(assets, packages)
    print(f"[fetch] v{version}：Release 的 {len(assets)} 个资产与发布清单的名称、大小和摘要一致", flush=True)
    base = args.base_url or f"https://github.com/{REPOSITORY}/releases/download/v{version}"
    downloads = args.downloads or root / ".build-cache" / "release-downloads" / f"v{version}"
    selected = sorted(packages) if args.all_packages else aggregate_packages(version)
    for name in selected:
        if name not in packages:
            raise SystemExit(f"发布清单中没有 {name}")
    # Every selected package is complete and verified before anything is extracted.
    for name in selected:
        download(f"{base}/{name}", downloads / name, packages[name]["bytes"], packages[name]["sha256"])
    if args.all_packages:
        documents: dict[tuple[str, str], str] = {}
        for name in selected:
            verify_package_members(downloads / name, name, version, manifest, packages[name], documents)
        print(f"[fetch] 21 个包的成员与发布清单逐项一致", flush=True)
    written = unchanged = 0
    found: set[str] = set()
    # Members are read in chunks, twice: every one is verified before any is
    # written, and checked again while it is written. Holding them all in
    # memory between the two steps took 3.3 GB for the three packages.
    pending: list[tuple[str, str, str, Path]] = []
    for name in aggregate_packages(version):
        with zipfile.ZipFile(downloads / name) as bundle:
            for member in bundle.infolist():
                relative = PurePosixPath(member.filename)
                if member.is_dir() or relative.is_absolute() or ".." in relative.parts:
                    continue
                digest = member_sha256(bundle, member)
                if relative.suffix == ".ttf":
                    repository_path = (f"fonts/variable/{relative.name}" if len(relative.parts) == 1
                                       else f"fonts/static/{relative.as_posix()}")
                    expected = fonts.get(repository_path)
                    if expected is None:
                        raise SystemExit(f"{name} 含发布清单未列出的字体：{member.filename}")
                    if digest != expected:
                        raise SystemExit(f"{repository_path} 的 SHA-256 与发布清单不符")
                    found.add(repository_path)
                elif len(relative.parts) == 2 and relative.name in STATIC_DOCUMENTS:
                    repository_path = f"fonts/static/{relative.as_posix()}"
                else:
                    continue
                target = root / repository_path
                if target.exists() and (not target.is_file() or sha256(target) != digest):
                    raise SystemExit(f"{target.relative_to(root).as_posix()} 已存在且内容不同；不覆盖现有文件")
                pending.append((name, member.filename, digest, target))
    missing = sorted(set(fonts) - found)
    if missing:
        raise SystemExit(f"Release 包缺少 {len(missing)} 个字体，例如 {missing[:3]}")
    for name, member_name, digest, target in pending:
        if target.exists():
            unchanged += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".fetch.tmp")
        with zipfile.ZipFile(downloads / name) as bundle, bundle.open(member_name) as source, temporary.open("wb") as output:
            hasher = hashlib.sha256()
            while chunk := source.read(1 << 20):
                hasher.update(chunk)
                output.write(chunk)
        if hasher.hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            raise SystemExit(f"{target.relative_to(root).as_posix()} 写入时内容与核验时不同")
        temporary.replace(target)
        written += 1
    print(f"[fetch] v{version}：{len(found)} 个字体与发布清单一致；新写入 {written} 个文件，已有且相同 {unchanged} 个")


if __name__ == "__main__":
    sys.exit(main())
