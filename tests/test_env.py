"""环境检查、锁文件解析与防 PYTHONPATH 阴影测试。"""
from __future__ import annotations

from importlib.machinery import ModuleSpec
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sarasa_pd.env import check_environment, head_modified_timestamp, parse_lockfile, repo_root


class EnvTests(unittest.TestCase):
    def test_current_environment_passes(self) -> None:
        res = check_environment(repo_root() / "requirements.lock")
        self.assertTrue(res.ok, msg=str(res.errors))
        self.assertGreaterEqual(len(res.versions), 10)

    def test_lockfile_rejects_missing_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_lock = Path(tmp) / "bad.lock"
            bad_lock.write_text("fonttools==4.67.0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_lockfile(bad_lock)

    def test_lockfile_rejects_unpinned_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_lock = Path(tmp) / "bad.lock"
            h = "a" * 64
            bad_lock.write_text(f"fonttools>=4.67.0 --hash=sha256:{h}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_lockfile(bad_lock)

    def test_shadow_import_from_outside_site_packages_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_origin = Path(tmp) / "fontTools" / "__init__.py"
            fake_origin.parent.mkdir(parents=True)
            fake_origin.write_text("# shadow\n", encoding="utf-8")
            real_find_spec = __import__("importlib.util", fromlist=["find_spec"]).find_spec

            def fake_find_spec(name: str):
                if name == "fontTools":
                    return ModuleSpec("fontTools", loader=None, origin=str(fake_origin))
                return real_find_spec(name)

            with mock.patch("importlib.util.find_spec", side_effect=fake_find_spec):
                res = check_environment(repo_root() / "requirements.lock")
            self.assertFalse(res.ok)
            self.assertTrue(any("PYTHONPATH" in err or "fontTools" in err for err in res.errors))

    def test_source_date_epoch_override(self) -> None:
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            self.assertEqual(head_modified_timestamp(12345), 1700000000)
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": ""}):
            self.assertEqual(head_modified_timestamp(12345), 12345)


if __name__ == "__main__":
    unittest.main()
