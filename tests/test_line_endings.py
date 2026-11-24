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
            root / "README.md",
            root / "NOTICE.md",
            root / ".github" / "workflows" / "check.yml",
            root / "hint" / "package.json",
            root / "hint" / "package-lock.json",
        ]
        targets.extend(sorted((root / "docs").glob("*.md")))
        targets.extend(sorted((root / "hint" / "hcfg").glob("*.json")))
        targets.extend(sorted((root / "sarasa_pd").rglob("*.py")))
        targets.extend(sorted((root / "sarasa_pd").rglob("*.toml")))
        targets.extend(sorted((root / "tests").rglob("*.py")))
        self.assertGreaterEqual(len(targets), 30)
        for path in targets:
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"存在 UTF-8 BOM：{path}")
            self.assertNotIn(b"\x00", raw, f"存在 NUL 字节：{path}")
            self.assertNotIn(b"\r", raw, f"存在 CR/CRLF 换行：{path}")
            text = raw.decode("utf-8")
            if path.suffix == ".md":
                for forbidden in ("D:\\Sarasa", "d:\\Sarasa", "D:/Sarasa", "d:/Sarasa", "ShenZehou"):
                    self.assertNotIn(forbidden, text, f"文档含本机路径 {forbidden!r}：{path}")


if __name__ == "__main__":
    unittest.main()
