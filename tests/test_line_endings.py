"""保证新代码与配置均为 UTF-8 无 BOM、无 NUL、LF 换行。"""
from __future__ import annotations

from pathlib import Path
import unittest

from sarasa_pd.env import repo_root


class LineEndingTests(unittest.TestCase):
    def test_new_files_are_utf8_lf_without_bom_or_nul(self) -> None:
        root = repo_root()
        targets: list[Path] = [
            root / "requirements.txt",
            root / "requirements.lock",
            root / ".github" / "workflows" / "check.yml",
        ]
        targets.extend(sorted((root / "sarasa_pd").rglob("*.py")))
        targets.extend(sorted((root / "sarasa_pd").rglob("*.toml")))
        targets.extend(sorted((root / "tests").rglob("*.py")))
        self.assertGreaterEqual(len(targets), 12)
        for path in targets:
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"存在 UTF-8 BOM：{path}")
            self.assertNotIn(b"\x00", raw, f"存在 NUL 字节：{path}")
            self.assertNotIn(b"\r", raw, f"存在 CR/CRLF 换行：{path}")
            raw.decode("utf-8")


if __name__ == "__main__":
    unittest.main()
