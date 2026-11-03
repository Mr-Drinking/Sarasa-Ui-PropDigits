"""Fetch a published release's fonts into fonts/, verified against its manifest.

Fonts are not tracked in git: a checkout carries reports/release-packages.json,
which names every ZIP of its release with size and SHA-256 and every font with
its SHA-256. This downloads the three all-region packages of that release,
verifies them before use and extracts the 156 fonts with their static
directory documents. Existing files are only accepted when identical.
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
CHUNK = 1024 * 1024
# A release that is not published yet (the release commit's own CI run) exits
# with this code, so CI can tell it from a broken download.
NOT_PUBLISHED = 3


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def aggregate_packages(version: str) -> list[str]:
    return [f"Sarasa-Ui-VF-PropDigits-TTF-{version}.zip", f"SarasaUiPropDigits-TTF-{version}.zip",
            f"SarasaUiPropDigits-TTF-Unhinted-{version}.zip"]


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
            if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                print(f"[fetch] Release 中没有 {target.name}（{url}）：该版本尚未发布", flush=True)
                raise SystemExit(NOT_PUBLISHED)
            print(f"[fetch] {type(error).__name__}: {error}", flush=True)
        finally:
            partial.unlink(missing_ok=True)
        time.sleep(2 ** attempt)
    raise SystemExit(f"无法取得与发布清单一致的 {target.name}")


def place(data: bytes, target: Path, expected: str | None) -> bool:
    """Write a member once; an existing file must already hold the same bytes."""
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None and digest != expected:
        raise SystemExit(f"{target.as_posix()} 的 SHA-256 与发布清单不符")
    if target.exists():
        if target.is_file() and sha256(target) == digest:
            return False
        raise SystemExit(f"{target.as_posix()} 已存在且内容不同；不覆盖现有文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".fetch.tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="从 GitHub Release 取回本检出对应版本的全部字体，按发布清单核验后解压到 fonts/。")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="目标检出（默认本仓库）；复核旧版时指向检出该 tag 的工作树")
    parser.add_argument("--packages", type=Path, help="发布清单（默认取目标检出的 reports/release-packages.json）")
    parser.add_argument("--downloads", type=Path, help="ZIP 下载目录（默认目标检出的 .build-cache/release-downloads/v<版本>）")
    parser.add_argument("--base-url", default=None, help="Release 下载地址前缀（默认 GitHub 上该版本的 tag）")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((args.packages or root / "reports" / "release-packages.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    packages = {item["file"]: item for item in manifest["packages"]}
    fonts = manifest["font_sha256"]
    base = args.base_url or f"https://github.com/{REPOSITORY}/releases/download/v{version}"
    downloads = args.downloads or root / ".build-cache" / "release-downloads" / f"v{version}"
    written = unchanged = 0
    found: set[str] = set()
    for name in aggregate_packages(version):
        item = packages.get(name)
        if item is None:
            raise SystemExit(f"发布清单中没有 {name}")
        archive = downloads / name
        download(f"{base}/{name}", archive, item["bytes"], item["sha256"])
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                relative = PurePosixPath(member.filename)
                if member.is_dir() or relative.is_absolute() or ".." in relative.parts:
                    continue
                if relative.suffix == ".ttf":
                    repository_path = (f"fonts/variable/{relative.name}" if len(relative.parts) == 1
                                       else f"fonts/static/{relative.as_posix()}")
                    expected = fonts.get(repository_path)
                    if expected is None:
                        raise SystemExit(f"{name} 含发布清单未列出的字体：{member.filename}")
                    found.add(repository_path)
                elif len(relative.parts) == 2 and relative.name in ("README.txt", "LICENSE-Sarasa-Gothic.txt"):
                    repository_path, expected = f"fonts/static/{relative.as_posix()}", None
                else:
                    continue
                if place(bundle.read(member), root / repository_path, expected):
                    written += 1
                else:
                    unchanged += 1
    missing = sorted(set(fonts) - found)
    if missing:
        raise SystemExit(f"Release 包缺少 {len(missing)} 个字体，例如 {missing[:3]}")
    print(f"[fetch] v{version}：{len(found)} 个字体与发布清单一致；新写入 {written} 个文件，已有且相同 {unchanged} 个")


if __name__ == "__main__":
    sys.exit(main())
