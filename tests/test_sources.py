"""固定来源校验与归档安全解包测试。"""
from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from sarasa_pd.sources import (
    ChecksumMismatch,
    file_sha256,
    safe_extract_tar_xz,
    validate_7z_archive,
    validate_member_paths,
    validate_tar_archive,
    validate_zip_archive,
    verify_or_write_bytes,
    verify_sha256,
)


def _make_tar_xz(path: Path, entries: list[tuple[str, bytes | str, int]]) -> Path:
    with tarfile.open(path, "w:xz") as tf:
        for name, content, kind in entries:
            ti = tarfile.TarInfo(name=name)
            if kind == tarfile.SYMTYPE:
                ti.type = tarfile.SYMTYPE
                ti.linkname = str(content)
                tf.addfile(ti)
            elif kind == tarfile.LNKTYPE:
                ti.type = tarfile.LNKTYPE
                ti.linkname = str(content)
                tf.addfile(ti)
            else:
                assert isinstance(content, bytes)
                ti.size = len(content)
                ti.mode = 0o755
                tf.addfile(ti, io.BytesIO(content))
    return path


class SourcesTests(unittest.TestCase):
    def test_sha256_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"sarasa-pd")
            digest = file_sha256(p)
            verify_sha256(p, digest)
            with self.assertRaises(ChecksumMismatch):
                verify_sha256(p, "0" * 64)

    def test_rejects_unsafe_archive_member_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            for bad in ("../evil.ttf", "a/../../evil.ttf", "/etc/passwd", "C:\\Windows\\evil.ttf", ""):
                with self.assertRaises(RuntimeError, msg=bad):
                    validate_member_paths([bad], dest)
            validate_member_paths(["dir/ok.ttf", "README.md"], dest)

    def test_rejects_zip_symlink_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zpath = Path(tmp) / "sym.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                info = zipfile.ZipInfo("link.ttf")
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, "../target.ttf")
            with zipfile.ZipFile(zpath) as zf:
                with self.assertRaises(RuntimeError):
                    validate_zip_archive(zf, Path(tmp) / "out")

    def test_rejects_7z_symlink_member(self) -> None:
        class _Fake7z:
            def __init__(self, infos):
                self._infos = infos

            def getnames(self):
                return [i.filename for i in self._infos]

            def list(self):
                return self._infos

        with tempfile.TemporaryDirectory() as tmp:
            ok_7z = _Fake7z([SimpleNamespace(filename="a.ttf", is_symlink=False, st_mode=stat.S_IFREG | 0o644)])
            self.assertEqual(validate_7z_archive(ok_7z, Path(tmp)), ["a.ttf"])

            bad_7z = _Fake7z([SimpleNamespace(filename="link.ttf", is_symlink=True, st_mode=stat.S_IFLNK | 0o777)])
            with self.assertRaisesRegex(RuntimeError, "7z 不允许软链接"):
                validate_7z_archive(bad_7z, Path(tmp))

    def test_tar_xz_validation_and_relative_symlink_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            # 1) 合法包内相对软链接（模拟 Node.js bin/npm -> ../lib/node_modules/npm/bin/npm-cli.js）
            good_tar = _make_tar_xz(
                tmp / "node-ok.tar.xz",
                [
                    ("node/lib/node_modules/npm/bin/npm-cli.js", b"console.log('ok');\n", tarfile.REGTYPE),
                    ("node/bin/npm", "../lib/node_modules/npm/bin/npm-cli.js", tarfile.SYMTYPE),
                ],
            )
            with tarfile.open(good_tar, "r:xz") as tf:
                validate_tar_archive(tf.getmembers(), tmp / "out")

            # 2) 反例：越界软链接、绝对目标软链接、硬链接必须被拒绝
            for idx, bad_entry in enumerate((
                ("node/bin/npm", "../../../etc/passwd", tarfile.SYMTYPE),
                ("node/bin/npm", "/etc/passwd", tarfile.SYMTYPE),
                ("node/bin/hard", "node/lib/file", tarfile.LNKTYPE),
            )):
                bad_tar = _make_tar_xz(tmp / f"bad-{idx}.tar.xz", [bad_entry])
                with tarfile.open(bad_tar, "r:xz") as tf:
                    with self.assertRaises(RuntimeError, msg=str(bad_entry)):
                        validate_tar_archive(tf.getmembers(), tmp / f"bad-out-{idx}")

            # 3) 实际解包：POSIX（及支持创建软链接的环境）必须原样保留相对软链接而不复制成普通文件
            out_dir = tmp / "extracted"
            probe_link = tmp / "probe_sym"
            can_symlink = True
            try:
                probe_link.symlink_to("target")
            except OSError:
                can_symlink = False
            if can_symlink:
                safe_extract_tar_xz(good_tar, out_dir)
                npm_path = out_dir / "node" / "bin" / "npm"
                self.assertTrue(npm_path.is_symlink())
                self.assertEqual(os.readlink(npm_path).replace("\\", "/"), "../lib/node_modules/npm/bin/npm-cli.js")
                self.assertTrue(npm_path.resolve().is_file())
            else:
                # 无创建软链接权限的 Windows 环境，用 mock 核对不会走解引用复制路径
                created_links: list[tuple[Path, str]] = []
                real_extractall = tarfile.TarFile.extractall

                def _fake_extractall(self_tf, path=".", members=None, **kwargs):
                    root = Path(path)
                    for m in members or []:
                        p = root / m.name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        if m.issym():
                            p.write_text(m.linkname, encoding="utf-8")
                        elif m.isfile():
                            p.write_bytes(self_tf.extractfile(m).read())

                orig_is_symlink = Path.is_symlink

                def _fake_is_symlink(self_p: Path) -> bool:
                    if self_p.name == "npm" and "sarasa-tar-" in str(self_p):
                        return True
                    return orig_is_symlink(self_p)

                def _fake_readlink(p: Path) -> str:
                    return Path(p).read_text(encoding="utf-8")

                def _fake_symlink_to(self_p: Path, target: str) -> None:
                    created_links.append((self_p, target))

                with (
                    mock.patch.object(tarfile.TarFile, "extractall", _fake_extractall),
                    mock.patch.object(Path, "is_symlink", _fake_is_symlink),
                    mock.patch("os.readlink", side_effect=_fake_readlink),
                    mock.patch.object(Path, "symlink_to", _fake_symlink_to),
                ):
                    safe_extract_tar_xz(good_tar, out_dir)
                self.assertEqual(len(created_links), 1)
                self.assertEqual(created_links[0][0].name, "npm")
                self.assertEqual(created_links[0][1], "../lib/node_modules/npm/bin/npm-cli.js")

    def test_verify_or_write_bytes_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cached.bin"
            verify_or_write_bytes(target, b"payload-1")
            verify_or_write_bytes(target, b"payload-1")
            with self.assertRaises(ChecksumMismatch):
                verify_or_write_bytes(target, b"payload-2")


if __name__ == "__main__":
    unittest.main()
