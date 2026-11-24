"""固定来源校验与归档安全解包测试。"""
from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from sarasa_pd.sources import (
    ChecksumMismatch,
    file_sha256,
    validate_member_paths,
    validate_zip_archive,
    verify_or_write_bytes,
    verify_sha256,
)


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

    def test_verify_or_write_bytes_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cached.bin"
            verify_or_write_bytes(target, b"payload-1")
            verify_or_write_bytes(target, b"payload-1")
            with self.assertRaises(ChecksumMismatch):
                verify_or_write_bytes(target, b"payload-2")


if __name__ == "__main__":
    unittest.main()
