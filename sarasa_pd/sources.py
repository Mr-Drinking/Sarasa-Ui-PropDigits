"""固定来源的下载、SHA-256 校验、安全解包与本地复用。"""
from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from .spec import ArchivePin, Spec, load_spec


class ChecksumMismatch(RuntimeError):
    """文件 SHA-256 与固定期望值不匹配。"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = file_sha256(path)
    if actual.lower() != expected.lower():
        raise ChecksumMismatch(
            f"SHA-256 校验失败：{path.name} 实际 {actual}，期望 {expected}"
        )


def download_checked(
    url: str,
    dest: Path,
    expected_sha256: str,
    *,
    timeout: float = 60.0,
    attempts: int = 3,
) -> Path:
    """下载到临时文件并校验 SHA-256 后原子落位；已有文件若摘要正确则直接复用。"""
    if dest.is_file() and dest.stat().st_size > 0:
        if file_sha256(dest).lower() == expected_sha256.lower():
            return dest
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(attempts):
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{attempt}.part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sarasa-pd"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                digest = hashlib.sha256()
                with tmp.open("wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        out.write(chunk)
            actual = digest.hexdigest()
            if actual.lower() != expected_sha256.lower():
                raise ChecksumMismatch(
                    f"下载校验失败：{dest.name} 实际 {actual}，期望 {expected_sha256}"
                )
            tmp.replace(dest)
            return dest
        except urllib.error.HTTPError as err:
            tmp.unlink(missing_ok=True)
            if 400 <= err.code < 500 and err.code not in {408, 429}:
                raise
            last_error = err
        except (
            urllib.error.URLError,
            http.client.IncompleteRead,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ChecksumMismatch,
        ) as err:
            tmp.unlink(missing_ok=True)
            last_error = err
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def validate_member_paths(names: list[str], destination: Path) -> None:
    root = destination.resolve()
    for raw in names:
        normalized = raw.replace("\\", "/")
        posix = PurePosixPath(normalized)
        win = PureWindowsPath(raw)
        if (
            not normalized
            or posix.is_absolute()
            or win.is_absolute()
            or bool(win.drive)
            or ".." in posix.parts
        ):
            raise RuntimeError(f"归档成员路径不安全：{raw!r}")
        candidate = root.joinpath(*posix.parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as err:
            raise RuntimeError(f"归档成员越界：{raw!r}") from err


def validate_zip_archive(zf: zipfile.ZipFile, destination: Path) -> None:
    infos = zf.infolist()
    validate_member_paths([info.filename for info in infos], destination)
    for info in infos:
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
            raise RuntimeError(f"ZIP 不允许软链接成员：{info.filename!r}")


def verify_or_write_bytes(path: Path, payload: bytes) -> None:
    expected = hashlib.sha256(payload).hexdigest()
    if path.is_file():
        if file_sha256(path) != expected:
            raise ChecksumMismatch(f"缓存文件与固定归档不一致：{path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def extract_zip_member(archive: Path, basename: str, out_path: Path) -> Path:
    with zipfile.ZipFile(archive) as zf:
        validate_zip_archive(zf, out_path.parent)
        matches = [name for name in zf.namelist() if Path(name).name == basename]
        if not matches:
            raise FileNotFoundError(f"归档 {archive.name} 中找不到 {basename}")
        chosen = sorted(matches, key=len)[0]
        verify_or_write_bytes(out_path, zf.read(chosen))
    return out_path


def extract_7z_member(archive: Path, basename: str, out_path: Path) -> Path:
    import py7zr

    with tempfile.TemporaryDirectory(prefix="sarasa-7z-") as tmp_raw:
        tmp_dir = Path(tmp_raw)
        with py7zr.SevenZipFile(archive) as zf:
            names = zf.getnames()
            validate_member_paths(names, tmp_dir)
            matches = [name for name in names if Path(name).name == basename]
            if not matches:
                raise FileNotFoundError(f"归档 {archive.name} 中找不到 {basename}")
            zf.extract(path=tmp_dir, targets=[sorted(matches, key=len)[0]])
        found = list(tmp_dir.rglob(basename))
        if not found:
            raise FileNotFoundError(f"未能从 {archive.name} 解包 {basename}")
        verify_or_write_bytes(out_path, found[0].read_bytes())
    return out_path


def extract_7z_ttf_prefix(archive: Path, out_dir: Path, prefix: str) -> list[Path]:
    import py7zr

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="sarasa-7z-") as tmp_raw:
        tmp_dir = Path(tmp_raw)
        with py7zr.SevenZipFile(archive) as zf:
            validate_member_paths(zf.getnames(), tmp_dir)
            zf.extractall(tmp_dir)
        for path in sorted(tmp_dir.rglob(f"{prefix}*.ttf")):
            dest = out_dir / path.name
            verify_or_write_bytes(dest, path.read_bytes())
            written.append(dest)
    return written


def safe_extract_tar_xz(archive: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-tar-") as tmp_raw:
        staging = Path(tmp_raw)
        with tarfile.open(archive, "r:xz") as tf:
            members = tf.getmembers()
            validate_member_paths([m.name for m in members], staging)
            tf.extractall(staging, members=members, filter="data")
        for member in staging.rglob("*"):
            dest = out_dir / member.relative_to(staging)
            if member.is_file():
                verify_or_write_bytes(dest, member.read_bytes())
                dest.chmod(member.stat().st_mode)


class SourceStore:
    """显式封装固定来源目录与解包，不依赖模块级全局路径。"""

    def __init__(self, work_root: Path, spec: Spec | None = None) -> None:
        self.work_root = work_root.resolve()
        self.spec = spec or load_spec()
        self.archive_dir = self.work_root / "source-archives"
        self.vf_dir = self.work_root / "vf-sources"
        self.ref_dir = self.work_root / f"official-sarasa-ui-{self.spec.sarasa_version}"
        self.node_dir = self.work_root / "node"

    def fetch_pin(self, pin: ArchivePin) -> Path:
        return download_checked(pin.url, self.archive_dir / pin.filename, pin.sha256)

    def ensure_vf_sources(self, regions: list[str] | tuple[str, ...]) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        shs_zip = self.fetch_pin(self.spec.archives["source_han_vf"])
        inter_zip = self.fetch_pin(self.spec.archives["inter"])

        paths["inter_upright"] = extract_zip_member(
            inter_zip, "InterVariable.ttf", self.vf_dir / "InterVariable.ttf"
        )
        paths["inter_italic"] = extract_zip_member(
            inter_zip, "InterVariable-Italic.ttf", self.vf_dir / "InterVariable-Italic.ttf"
        )

        need_classical = False
        for code in regions:
            r = self.spec.region(code)
            paths[f"source_han_{r.code}"] = extract_zip_member(
                shs_zip, r.source_han_vf, self.vf_dir / r.source_han_vf
            )
            if r.classical and r.classical_vf:
                need_classical = True
                paths[f"classical_{r.code}"] = self.vf_dir / r.classical_vf

        if need_classical:
            sg_archive = self.fetch_pin(self.spec.archives["shanggu_vf"])
            for code in regions:
                r = self.spec.region(code)
                if r.classical and r.classical_vf:
                    target = self.vf_dir / r.classical_vf
                    extract_7z_member(sg_archive, target.name, target)
        return paths

    def sarasa_ref_path(self, region: str, weight_name: str, italic: bool, hinted: bool = False) -> Path:
        r = self.spec.region(region)
        kind = "hinted" if hinted else "unhinted"
        return self.ref_dir / r.code / kind / self.spec.sarasa_ref_filename(r.code, weight_name, italic)

    def ensure_sarasa_reference(self, regions: list[str] | tuple[str, ...], hinted: bool = False) -> None:
        kind = "hinted" if hinted else "unhinted"
        variant = "TTF" if hinted else "TTF-Unhinted"
        agg_key = f"sarasa_ui_{kind}_all"
        agg_pin = self.spec.archives[agg_key]
        agg_archive = self.archive_dir / agg_pin.filename
        sha_map = self.spec.sarasa_ui_hinted_sha256 if hinted else self.spec.sarasa_ui_unhinted_sha256

        if agg_archive.is_file():
            verify_sha256(agg_archive, agg_pin.sha256)
            missing_regions = [
                c for c in regions
                if not self.sarasa_ref_path(c, "Regular", False, hinted=hinted).is_file()
            ]
            if missing_regions:
                with tempfile.TemporaryDirectory(prefix="sarasa-ref-") as tmp_raw:
                    tmp_dir = Path(tmp_raw)
                    extract_7z_ttf_prefix(agg_archive, tmp_dir, "SarasaUi")
                    for code in missing_regions:
                        r = self.spec.region(code)
                        dest_dir = self.ref_dir / r.code / kind
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        for path in sorted(tmp_dir.glob(f"{r.sarasa_prefix}-*.ttf")):
                            verify_or_write_bytes(dest_dir / path.name, path.read_bytes())
            return

        for code in regions:
            r = self.spec.region(code)
            if self.sarasa_ref_path(r.code, "Regular", False, hinted=hinted).is_file():
                continue
            name = f"{r.sarasa_prefix}-{variant}-{self.spec.sarasa_version}.7z"
            url = f"https://github.com/be5invis/Sarasa-Gothic/releases/download/{self.spec.source_tags['sarasa']}/{name}"
            archive = download_checked(url, self.archive_dir / name, sha_map[r.code])
            extract_7z_ttf_prefix(archive, self.ref_dir / r.code / kind, f"{r.sarasa_prefix}-")

    def ensure_node(self) -> Path:
        system = platform.system().lower()
        machine = platform.machine().lower()
        arch = "x64" if machine in {"amd64", "x86_64"} else "arm64" if machine in {"arm64", "aarch64"} else None
        if arch is None:
            raise RuntimeError(f"不支持的 Node.js 架构：{platform.machine()}")
        os_key = {"windows": "win", "linux": "linux", "darwin": "darwin"}.get(system)
        if os_key is None:
            raise RuntimeError(f"不支持的操作系统：{platform.system()}")
        ext = "zip" if os_key == "win" else "tar.xz"
        key = f"{os_key}-{arch}-{ext}"
        folder = f"node-{self.spec.node_version}-{os_key}-{arch}"
        exe = self.node_dir / folder / ("node.exe" if os_key == "win" else "bin/node")
        if exe.is_file():
            return exe
        archive_name = f"{folder}.{ext}"
        url = f"https://nodejs.org/dist/{self.spec.node_version}/{archive_name}"
        archive = download_checked(url, self.archive_dir / archive_name, self.spec.node_sha256[key])
        if ext == "zip":
            with zipfile.ZipFile(archive) as zf:
                validate_zip_archive(zf, self.node_dir)
                for member in zf.infolist():
                    if not member.is_dir():
                        verify_or_write_bytes(self.node_dir / member.filename, zf.read(member))
        else:
            safe_extract_tar_xz(archive, self.node_dir)
        return exe

    def ensure_ots(self, external_dir: Path) -> Path:
        system = platform.system()
        if system not in self.spec.ots_sha256:
            raise RuntimeError(f"OTS 不支持当前平台：{system}")
        archive_name = f"ots-{self.spec.ots_version}-{'macOS' if system == 'Darwin' else system}.zip"
        url = f"https://github.com/khaledhosny/ots/releases/download/v{self.spec.ots_version}/{archive_name}"
        archive = download_checked(url, external_dir / archive_name, self.spec.ots_sha256[system])
        dest = external_dir / "ots"
        with zipfile.ZipFile(archive) as zf:
            validate_zip_archive(zf, dest)
            for member in zf.infolist():
                if not member.is_dir():
                    out = dest / member.filename
                    verify_or_write_bytes(out, zf.read(member))
                    if system != "Windows":
                        out.chmod((member.external_attr >> 16) & 0o777 or 0o755)
        name = "ots-sanitize.exe" if system == "Windows" else "ots-sanitize"
        matches = list(dest.rglob(name))
        if len(matches) != 1:
            raise RuntimeError(f"OTS 可执行文件数量异常：{matches}")
        return matches[0]
