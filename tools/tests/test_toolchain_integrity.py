from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as build
import python_env_bootstrap as bootstrap


class ToolchainIntegrity(unittest.TestCase):
    def test_unhashed_or_unpinned_locks_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "requirements.lock"
            for content in ("leaf==1.0\n", "leaf>=1.0 --hash=sha256:" + "a" * 64 + "\n"):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    bootstrap.locked_requirements(path)

    def test_transitive_version_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "requirements.lock"
            path.write_text("leaf==1.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
            with patch.object(bootstrap.importlib.metadata, "version", return_value="2.0"):
                status = bootstrap.locked_environment_status(path)
            self.assertFalse(status["ok"])
            self.assertEqual(status["mismatches"], ["leaf"])

    def test_missing_or_disabled_chlorophytum_fails_before_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            jobs = [(root / "input.ttf", root / "hint.gz", "Regular")]
            with patch.object(build, "SARASA_CHLOROPHYTUM", root / "missing"), patch.dict(os.environ, {"SARASA_SKIP_CHLOROPHYTUM": "0"}):
                with self.assertRaises(FileNotFoundError):
                    build.chlorophytum_generate_static_hints(jobs, root / "work")
                with self.assertRaises(FileNotFoundError):
                    build.chlorophytum_instruct_static_fonts([(jobs[0][0], jobs[0][1], root / "output.ttf", "Regular")])
            with patch.dict(os.environ, {"SARASA_SKIP_CHLOROPHYTUM": "1"}):
                with self.assertRaises(RuntimeError):
                    build.require_chlorophytum()
            self.assertFalse((root / "output.ttf").exists())

    def test_download_times_out_retries_and_never_publishes_partial_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asset.bin"
            def open_url(_url, **kwargs):
                self.assertEqual(kwargs["timeout"], 60)
                self.assertFalse(path.exists())
                if calls[0] < 2:
                    calls[0] += 1
                    raise TimeoutError("simulated timeout")
                return io.BytesIO(b"complete payload")
            calls = [0]
            with patch.object(build.urllib.request, "urlopen", side_effect=open_url), patch.object(build.time, "sleep"):
                build.download_file("https://example.invalid/asset", path)
            self.assertEqual(path.read_bytes(), b"complete payload")
            self.assertFalse(list(Path(folder).glob("*.part")))

    def test_short_http_body_is_retried_before_promotion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asset.bin"
            first = io.BytesIO(b"short"); first.headers = {"Content-Length": "16"}
            second = io.BytesIO(b"complete"); second.headers = {"Content-Length": "8"}
            with patch.object(build.urllib.request, "urlopen", side_effect=[first, second]), patch.object(build.time, "sleep"):
                build.download_file("https://example.invalid/asset", path)
            self.assertEqual(path.read_bytes(), b"complete")

    def test_dependency_snapshot_detects_same_version_code_changes_and_extra_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "package.json").write_text('{"version":"1.0"}', encoding="utf-8")
            (root / "index.js").write_text("original", encoding="utf-8")
            before = build.npm_dependency_snapshot(root)
            (root / "index.js").write_text("modified", encoding="utf-8")
            self.assertNotEqual(before, build.npm_dependency_snapshot(root))
            (root / "index.js").write_text("original", encoding="utf-8")
            (root / "shadow.js").write_text("added", encoding="utf-8")
            self.assertNotEqual(before, build.npm_dependency_snapshot(root))


if __name__ == "__main__":
    unittest.main()
