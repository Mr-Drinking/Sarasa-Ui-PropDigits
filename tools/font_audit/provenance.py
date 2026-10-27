"""Bind read-only audit results to source bytes and the executed toolchain."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

import freetype
import py7zr
from py7zr.io import Py7zIO, WriterFactory
import uharfbuzz as hb

from python_env_bootstrap import locked_environment_status
from . import contract as c


ARCHIVES = json.loads(Path(__file__).with_name("source-pins.json").read_text("utf-8"))
SARASA_COMMIT = "61e74fda253a1bec0c6f5b09d32a0944bb96b814"


class HashWriter(Py7zIO):
    def __init__(self):
        self.digest = hashlib.sha256()
        self.length = 0
        self.position = 0

    def write(self, data):
        if self.position != self.length:
            raise OSError("审计散列流不能重写已验证的内容")
        self.digest.update(data)
        self.length += len(data)
        self.position = self.length
        return len(data)

    def read(self, size=None):
        raise OSError("审计散列流不可读取")

    def seek(self, offset, whence=0):
        position = offset + (self.position if whence == 1 else self.length if whence == 2 else 0)
        if position not in (0, self.length):
            raise OSError("审计散列流不支持此偏移")
        self.position = position
        return position

    def flush(self):
        pass

    def size(self):
        return self.length


class HashFactory(WriterFactory):
    def __init__(self):
        self.files = {}

    def create(self, filename):
        if filename in self.files:
            raise ValueError("来源归档包含重复成员")
        writer = HashWriter()
        self.files[filename] = writer
        return writer


def archive_member_hashes(path: Path, basenames: set[str]) -> dict[str, str]:
    """Hash archive payloads directly, without trusting or rewriting a cache."""
    result = {}
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if Path(name).name in basenames]
            for name in members:
                with archive.open(name) as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if Path(name).name in result:
                    raise ValueError("来源归档中的文件名不唯一")
                result[Path(name).name] = digest
    else:
        factory = HashFactory()
        with py7zr.SevenZipFile(path) as archive:
            members = [name for name in archive.getnames() if Path(name).name in basenames]
            archive.extract(targets=members, factory=factory)
        for name, writer in factory.files.items():
            if Path(name).name in result:
                raise ValueError("来源归档中的文件名不唯一")
            result[Path(name).name] = writer.digest.hexdigest()
    if set(result) != basenames:
        raise ValueError("来源归档缺少需要的字体")
    return result


def source_groups() -> dict[str, list[Path]]:
    groups = {
        "Inter-4.1.zip": [c.inter_path(False), c.inter_path(True)],
        "SourceHanSans-VF-2.005R.zip": sorted({c.cjk_path(region) for region in c.REGIONS}),
        "ShangguSansVF_TTFs-1.028.7z": [c.SOURCE_ROOT / "shanggu-1.028/ShangguSansTC-VF.ttf"],
        "ShangguSansTTFs-1.028.7z": [c.SOURCE_ROOT / f"shanggu-1.028/static/ShangguSansTC-{style}.ttf"
                                     for style in ("ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy")],
    }
    for hinted in (False, True):
        variant = "TTF" if hinted else "TTF-Unhinted"
        aggregate = f"SarasaUi-{variant}-{c.VERSION}.7z"
        for region in c.REGIONS:
            archive = aggregate if (c.WORK / "source-archives" / aggregate).is_file() else f"SarasaUi{region}-{variant}-{c.VERSION}.7z"
            groups.setdefault(archive, []).extend(c.reference_path(region, weight, italic, hinted)
                for weight in c.WEIGHTS if weight != "Heavy" for italic in (False, True))
    return groups


def source_manifest() -> dict:
    paths = [path for group in source_groups().values() for path in group]
    paths.extend(c.WORK / "source-archives" / name for name in source_groups())
    # Native source fonts and generated native pass fragments are also bound
    # when present. They are never allowed to stand in for a pinned oracle.
    tracked = subprocess.check_output(['git', '-C', str(c.SARASA_SOURCE), 'ls-files', '-z'])
    paths.extend(c.SARASA_SOURCE / name.decode('utf-8') for name in tracked.split(b'\0') if name)
    return c.manifest(paths)


def verify_git_source() -> dict:
    def git(*arguments, **kwargs):
        return subprocess.check_output(['git', '-C', str(c.SARASA_SOURCE), *arguments], **kwargs)
    if git('rev-parse', 'HEAD').decode().strip() != SARASA_COMMIT:
        raise ValueError('Sarasa 源码提交不符合固定来源')
    records = []
    for entry in git('ls-tree', '-r', '-z', SARASA_COMMIT).split(b'\0'):
        if not entry:
            continue
        metadata, name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('固定 Sarasa 源码中有未支持的对象类型')
        records.append((name, oid))
    hashes = git('hash-object', '--no-filters', '--stdin-paths',
                 input=b'\n'.join(name for name, _oid in records) + b'\n').splitlines()
    if len(hashes) != len(records) or any(actual != expected for actual, (_name, expected) in zip(hashes, records)):
        raise ValueError('Sarasa 源码字节与固定 Git 对象不一致')
    git('diff-index', '--cached', '--quiet', 'HEAD', '--')
    return {'commit': SARASA_COMMIT, 'files_verified': len(records)}


def verify_sources() -> dict:
    checked, archives = 0, {}
    for name, files in source_groups().items():
        archive = c.WORK / "source-archives" / name
        digest = c.sha256(archive)
        if digest != ARCHIVES[name]:
            raise ValueError(f"固定来源归档 SHA-256 不匹配：{name}")
        expected = archive_member_hashes(archive, {path.name for path in files})
        for path in files:
            if c.sha256(path) != expected[path.name]:
                raise ValueError(f"解包来源与固定归档不一致：{path.name}")
            checked += 1
        archives[name] = digest
    return {"cases": checked, "failures": 0, "archives": archives, "git_source": verify_git_source(), "source_manifest": source_manifest()}


def tool_identity() -> dict:
    lock = c.ROOT / "requirements-audit.lock"
    environment = locked_environment_status(lock)
    if environment["mismatches"]:
        raise ValueError("审计环境的版本或已安装文件内容不符合依赖锁")
    from .raster import ttfautohint_identity
    return {"python": platform.python_version(), "platform": sys.platform,
            "harfbuzz": hb.version_string(), "freetype": list(freetype.version()),
            "ttfautohint": ttfautohint_identity(),
            "dependency_lock_sha256": c.sha256(lock), "environment": environment}


def capture() -> dict:
    return {"source_manifest": source_manifest(), "tool_identity": tool_identity()}


def validate_reuse(report: dict, current: dict | None = None) -> None:
    current = capture() if current is None else current
    if not current.get("source_manifest") or not current.get("tool_identity"):
        raise ValueError("缺少来源或工具身份")
    if report.get("provenance") != current:
        raise ValueError("报告的来源或工具出处缺失、过期或与当前环境不同")
