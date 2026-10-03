from __future__ import annotations

from contextlib import ExitStack
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as build
from fontTools.ttLib import TTFont
from test_runtime_regressions import fixture_font
import py7zr


class VersionMetadataRefreshTests(unittest.TestCase):
    def make_source(self, path):
        font = fixture_font()
        font["name"].setName("Probe;0.9.0;unhinted", 3, 3, 1, 0x409)
        font["name"].setName("Version 0.900; project 0.9.0; unhinted", 5, 3, 1, 0x409)
        font["head"].fontRevision = 0.9
        font.save(path)

    def test_only_version_names_and_head_change(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "font.ttf"; self.make_source(path)
            before = build.sfnt_table_hashes(path, {"head", "name"})
            report = build.refresh_version_metadata_font(path, "0.9.0")
            self.assertEqual(build.sfnt_table_hashes(path, {"head", "name"}), before)
            self.assertEqual(report["protected_table_sha256"], before)
            self.assertFalse(report["hint_rerun"])
            with TTFont(path) as font:
                self.assertEqual(font["name"].getDebugName(3), f"Probe;{build.VERSION};unhinted")
                self.assertEqual(font["name"].getDebugName(5), f"Version {build.OPENTYPE_VERSION}; project {build.VERSION}; unhinted")
                self.assertAlmostEqual(font["head"].fontRevision, build.FONT_REVISION, delta=1 / 65536)

    def test_wrong_original_version_leaves_source_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "font.ttf"; self.make_source(path)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                build.refresh_version_metadata_font(path, "0.8.0")
            self.assertEqual(path.read_bytes(), before)

    def test_unexpected_metric_change_cannot_replace_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "font.ttf"; self.make_source(path)
            before = path.read_bytes(); original_save = TTFont.save
            def corrupted_save(font, *args, **kwargs):
                font["hmtx"].metrics["G"] = (601, 0)
                return original_save(font, *args, **kwargs)
            with patch.object(TTFont, "save", corrupted_save):
                with self.assertRaisesRegex(RuntimeError, "head/name"):
                    build.refresh_version_metadata_font(path, "0.9.0")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(Path(folder).glob("*.tmp")))


class AggregateReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.archives = self.root / "archives"
        self.archives.mkdir(); inputs = self.root / "inputs"; inputs.mkdir()
        font = fixture_font()
        font["name"].setName(f"Version {build.SARASA_VERSION}", 5, 3, 1, 0x409)
        data = io.BytesIO(); font.save(data); self.payload = data.getvalue()
        self.names = {f"SarasaUiSC-{build.static_reference_style_name(weight, italic)}.ttf"
                      for weight in build.STATIC_STYLE_SOURCES for italic in (False, True)}
        for name in self.names:
            (inputs / name).write_bytes(self.payload)
        hashes = {}
        for kind, variant in (("hinted", "TTF"), ("unhinted", "TTF-Unhinted")):
            archive = self.archives / f"SarasaUi-{variant}-{build.SARASA_VERSION}.7z"
            with py7zr.SevenZipFile(archive, "w") as stream:
                stream.writeall(inputs, arcname="fonts")
            hashes[kind] = build.file_sha256(archive)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        replacements = {"SOURCE_ARCHIVE_DIR": self.archives, "REFERENCE_ROOT": self.root / "refs",
                        "REFERENCE_SC_LEGACY_ROOT": self.root / "refs/SC", "SARASA_UI_AGGREGATE_ARCHIVE_SHA256": hashes}
        for name in ("REFERENCE_SARASA", "REFERENCE_SARASA_DIR", "REFERENCE_SARASA_HINTED_DIR"):
            replacements[name] = getattr(build, name)
        self.stack.enter_context(patch.multiple(build, **replacements))
        self.stack.enter_context(patch.object(build, "download_file_checked", side_effect=AssertionError("no network expected")))

    def test_verified_aggregate_supplies_only_requested_region(self):
        build.ensure_reference_sarasa(["SC"])
        for hinted in (False, True):
            directory = build.region_reference_dir("SC", hinted)
            self.assertEqual({p.name for p in directory.glob("*.ttf")}, self.names)
            self.assertTrue(all(p.read_bytes() == self.payload for p in directory.glob("*.ttf")))
        self.assertEqual({p.name for p in (self.root / "refs").iterdir()}, {"SC"})

    def test_existing_archive_with_wrong_hash_is_rejected(self):
        path = next(self.archives.glob("*.7z")); path.write_bytes(path.read_bytes() + b"tampered")
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            build.ensure_reference_sarasa(["SC"])

    def test_existing_unpacked_font_is_reverified(self):
        build.ensure_reference_sarasa(["SC"])
        path = build.region_reference_dir("SC", False) / "SarasaUiSC-Regular.ttf"
        path.write_bytes(b"tampered")
        with self.assertRaises(RuntimeError):
            build.ensure_reference_sarasa(["SC"])


if __name__ == "__main__":
    unittest.main()
