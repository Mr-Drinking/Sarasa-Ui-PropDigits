from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
from font_audit import contract as audit
import package_release as package
from test_runtime_regressions import fixture_font


class ReleaseIntegrityRegressions(unittest.TestCase):
    def test_git_source_validation_checks_raw_bytes_not_normalized_status(self):
        if not shutil.which("git"):
            self.skipTest("需要 Git 验证固定来源")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout
            git("init", "-q")
            git("config", "core.autocrlf", "false")
            source = root / "source.mjs"; original = b"export const value = 1;\n"
            source.write_bytes(original)
            git("add", "source.mjs")
            git("-c", "user.name=Source cache test", "-c", "user.email=source@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(root / "no-hooks"),
                "commit", "-qm", "fixture")
            commit = git("rev-parse", "HEAD").decode().strip()
            self.assertEqual(b.verify_git_source_bytes(root, commit)["tracked_files_verified"], 1)
            git("config", "core.autocrlf", "true")
            source.write_bytes(original.replace(b"\n", b"\r\n"))
            with self.assertRaises(RuntimeError):
                b.verify_git_source_bytes(root, commit)
            source.write_bytes(original)
            b.verify_git_source_bytes(root, commit)
            (root / "unexpected.mjs").write_text("unexpected", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                b.verify_git_source_bytes(root, commit)

    def test_files_bound_by_raw_hashes_keep_checkout_line_endings(self):
        # The audit report, the build record and the release packages hash these files' raw bytes,
        # and .gitattributes gives every checkout LF. A CRLF working copy (five audit-contract files
        # on Windows before v1.0.43) binds hashes that a fresh checkout cannot reproduce.
        paths = {audit.ROOT / name for name in audit.audit_contract_manifest()}
        paths.update(Path(b.__file__).with_name(name) for name in b.BUILD_COMPONENT_SHA256)
        paths.update(source for item in package.release_packages() for source, _name in item.files
                     if source.suffix != ".ttf")
        carriage_returns = sorted(Path(os.path.relpath(path, b.ROOT)).as_posix()
                                  for path in paths if b"\r" in path.read_bytes())
        self.assertEqual(carriage_returns, [])

    def test_intermediate_cache_rejects_changed_bytes_inputs_and_missing_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source.ttf"; output = root / "fragment.ttf"
            source.write_bytes(b"source-one"); calls = []
            def produce(path):
                calls.append(True); path.write_bytes(source.read_bytes())
            recipe = {"kind": "fragment", "version": 1}
            self.assertFalse(b.cached_static_artifact(output, {"source": source}, recipe, produce))
            self.assertTrue(b.cached_static_artifact(output, {"source": source}, recipe, produce))
            stamp = output.stat(); output.write_bytes(b"wrong-metr")
            os.utime(output, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertFalse(b.cached_static_artifact(output, {"source": source}, recipe, produce))
            self.assertEqual(output.read_bytes(), source.read_bytes())
            source.write_bytes(b"source-two")
            self.assertFalse(b.cached_static_artifact(output, {"source": source}, recipe, produce))
            output.with_name(output.name + ".cache.json").unlink()
            self.assertFalse(b.cached_static_artifact(output, {"source": source}, recipe, produce))
            self.assertEqual(len(calls), 4)

    def test_failed_cache_producer_keeps_previous_bytes_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "source.ttf"; output = root / "fragment.ttf"
            source.write_bytes(b"source")
            b.cached_static_artifact(output, {"source": source}, {"version": 1}, lambda path: path.write_bytes(b"old"))
            receipt = output.with_name(output.name + ".cache.json"); original = receipt.read_bytes()
            def fail(path):
                path.write_bytes(b"partial")
                raise OSError("interrupted producer")
            with self.assertRaisesRegex(OSError, "interrupted"):
                b.cached_static_artifact(output, {"source": source}, {"version": 2}, fail)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(receipt.read_bytes(), original)

    def test_static_package_rejects_undeclared_files_and_missing_documents(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(b, "STATIC_ROOT", Path(temporary)):
            directory = b.static_dir("SC", False)
            directory.mkdir()
            names = {b.static_output_name("SC", str(stop["name"]), italic)
                     for stop in b.SOURCE_HAN_WEIGHT_STOPS for italic in (False, True)}
            names.update({"README.txt", "LICENSE-Sarasa-Gothic.txt"})
            for name in names:
                (directory / name).write_bytes(b"fixture")
            self.assertEqual(len(package.static_files("SC", False)), 14)
            for extra in ("font.ttf.finalization.tmp", "unreviewed.txt", "nested/README.txt"):
                path = directory / extra
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"unreviewed")
                with self.assertRaisesRegex(ValueError, "清单"):
                    package.static_files("SC", False)
                path.unlink()
            (directory / "LICENSE-Sarasa-Gothic.txt").unlink()
            with self.assertRaisesRegex(ValueError, "清单"):
                package.static_files("SC", False)

    def test_unhinted_entry_writes_license_before_producing_fonts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "LICENSE").write_bytes(b"upstream license")
            with patch.multiple(b, ROOT=root, STATIC_ROOT=root / "fonts/static"), \
                 patch.object(b, "prepare_static_style", return_value=({}, {})):
                b.build_unhinted_fonts(["SC"])
                self.assertEqual((b.static_dir("SC", False) / "LICENSE-Sarasa-Gothic.txt").read_bytes(),
                                 (root / "LICENSE").read_bytes())
                self.assertTrue((b.static_dir("SC", False) / "README.txt").is_file())

    def test_finalization_failure_keeps_whole_group_and_receipts_unchanged(self):
        for variable in (False, True):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                receipts = root / ".cache/receipts"
                receipts.mkdir(parents=True)
                receipt = receipts / "original.json"
                receipt.write_bytes(b"old receipt")
                with patch.multiple(b, ROOT=root, BUILD_CACHE_DIR=root / ".cache",
                                    STATIC_ROOT=root / "fonts/static", VARIABLE_DIR=root / "fonts/variable",
                                    REPORT_DIR=root / "reports", OUTPUT_VALIDATION_DIR=receipts):
                    current = (b.VARIABLE_DIR / b.variable_output_name("SC", False) if variable else
                               b.static_dir("SC", False) / b.static_output_name("SC", "Regular", False))
                    current.parent.mkdir(parents=True)
                    current.write_bytes(b"old font")
                    def fail_after_first(*args):
                        candidate = (b.VARIABLE_DIR / current.name if variable else b.static_dir("SC", False) / current.name)
                        candidate.write_bytes(b"new font")
                        (b.OUTPUT_VALIDATION_DIR / receipt.name).write_bytes(b"new receipt")
                        raise RuntimeError("second font failed")
                    implementation = "_refresh_variable_finalization_outputs" if variable else "_refresh_static_finalization_outputs"
                    public = b.refresh_variable_finalization_outputs if variable else b.refresh_static_finalization_outputs
                    with patch.object(b, 'require_validated_input'), patch.object(b, implementation, side_effect=fail_after_first), self.assertRaisesRegex(RuntimeError, "second font"):
                        public(["SC"])
                    self.assertEqual(current.read_bytes(), b"old font")
                    self.assertEqual(receipt.read_bytes(), b"old receipt")

    def test_external_cache_commits_fonts_and_only_their_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base / "repository"; root.mkdir()
            cache = base / "external-cache"; receipts = cache / "output-validation"
            receipts.mkdir(parents=True)
            unrelated = receipts / "unrelated.json"; unrelated.write_bytes(b"retain")
            with patch.multiple(b, ROOT=root, BUILD_CACHE_DIR=cache, STATIC_ROOT=root / "fonts/static",
                                VARIABLE_DIR=root / "fonts/variable", REPORT_DIR=root / "reports",
                                OUTPUT_VALIDATION_DIR=receipts), \
                 patch.object(b, "verified_output_record_status", return_value=(True, "")):
                with b.staged_build_outputs(["SC"], static_only=True, variable_only=False, unhinted_only=True,
                                           resume_static=False, resume_variable=False):
                    self.assertTrue(b.OUTPUT_STAGING_ROOT.is_relative_to(root))
                    for stop in b.SOURCE_HAN_WEIGHT_STOPS:
                        for italic in (False, True):
                            candidate = b.static_dir("SC", False) / b.static_output_name("SC", str(stop["name"]), italic)
                            candidate.write_bytes(b"font")
                            b.write_json_atomic(b.output_validation_record_path(candidate),
                                                {"region": "SC", "weight": str(stop["name"]), "italic": italic, "hinted": False})
                self.assertEqual(len(list(b.static_dir("SC", False).glob("*.ttf"))), 12)
                self.assertEqual(len(list(receipts.glob("*.json"))), 13)
                self.assertEqual(unrelated.read_bytes(), b"retain")
                source = root / "candidate"; source.write_bytes(b"unauthorized")
                with self.assertRaisesRegex(ValueError, "越界"):
                    b.promote_build_outputs([(source, cache / "unrelated.txt")], root / "backup", validation_root=receipts)

    def test_receipt_promotion_failure_rolls_back_static_and_variable_refreshes(self):
        for variable in (False, True):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary); root = base / "repository"; root.mkdir()
                (root / "LICENSE").write_bytes(b"license")
                receipts = base / "external-cache/receipts"; receipts.mkdir(parents=True)
                with patch.multiple(b, ROOT=root, BUILD_CACHE_DIR=receipts.parent,
                                    STATIC_ROOT=root / "fonts/static", VARIABLE_DIR=root / "fonts/variable",
                                    REPORT_DIR=root / "reports", OUTPUT_VALIDATION_DIR=receipts), \
                     patch.object(b, "verified_output_record_status", return_value=(True, "")):
                    def paths():
                        if variable:
                            return [(b.VARIABLE_DIR / b.variable_output_name("SC", italic), None, italic, False)
                                    for italic in (False, True)]
                        return [(path, str(stop["name"]), italic, hinted) for stop in b.SOURCE_HAN_WEIGHT_STOPS
                                for path, hinted, italic in b.static_weight_output_paths("SC", str(stop["name"]))]
                    before = {}
                    for path, weight, italic, hinted in paths():
                        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"old font")
                        record = b.output_validation_record_path(path)
                        b.write_json_atomic(record, {"region": "SC", "weight": weight, "italic": italic, "hinted": hinted})
                        before[path] = path.read_bytes(); before[record] = record.read_bytes()
                    def refresh(*_args):
                        for path, weight, italic, hinted in paths():
                            path.write_bytes(b"new font")
                            b.write_json_atomic(b.output_validation_record_path(path),
                                                {"region": "SC", "weight": weight, "italic": italic, "hinted": hinted, "new": True})
                        return []
                    original_replace = Path.replace; committed_receipts = []
                    def replace(path, destination):
                        destination = Path(destination)
                        if destination.parent == receipts and path.parent != receipts:
                            committed_receipts.append(path)
                            if len(committed_receipts) == 2:
                                raise OSError("second receipt failed")
                        return original_replace(path, destination)
                    implementation = "_refresh_variable_finalization_outputs" if variable else "_refresh_static_finalization_outputs"
                    public = b.refresh_variable_finalization_outputs if variable else b.refresh_static_finalization_outputs
                    with patch.object(b, 'require_validated_input'), patch.object(b, implementation, side_effect=refresh), patch.object(Path, "replace", replace):
                        with self.assertRaisesRegex(OSError, "second receipt"):
                            public(["SC"])
                    for path, data in before.items():
                        self.assertEqual(path.read_bytes(), data, str(path))

    def test_mutated_cached_metrics_are_rebuilt_before_attestation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); work = root / "work"; work.mkdir()
            raw = root / "raw.ttf"; fixture_font().save(raw)
            fragments = {name: raw for name in ("pass1", "kanji", "hangul", "dash_source_path", "western_dash_source_path")}
            stop = {"name": "Regular", "value": 400}
            def compose(_pass1, _kanji, _hangul, output, *_args):
                fixture_font().save(output)
            def postprocess(*_args):
                return {"static_postprocess_cache_key": b.static_postprocess_cache_key("SC", "Regular", 400, False, False)}
            recorded = []
            with patch.object(b, "STATIC_ROOT", root / "fonts/static"), \
                 patch.object(b, "build_sarasa_static_fragments", return_value=fragments), \
                 patch.object(b, "prepare_static_pass1_derivatives", return_value=(raw, {})), \
                 patch.object(b, "build_sarasa_pass2", side_effect=compose) as rebuild, \
                 patch.object(b, "postprocess_static_font", side_effect=postprocess), \
                 patch.object(b, "record_verified_output", side_effect=lambda path, *args: recorded.append(b.file_sha256(path))):
                b.static_dir("SC", False).mkdir(parents=True)
                b.prepare_static_style("SC", stop, work, False, prepare_hints=False)
                cached = work / "unhinted/SC" / b.static_output_name("SC", "Regular", False)
                original = cached.read_bytes(); stamp = cached.stat()
                with b.TTFont(cached, recalcTimestamp=False) as font:
                    advance, bearing = font["hmtx"].metrics["G"]
                    font["hmtx"].metrics["G"] = (advance + 99, bearing)
                    font.save(cached)
                corrupt_digest = b.file_sha256(cached)
                os.utime(cached, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                b.prepare_static_style("SC", stop, work, False, prepare_hints=False)
                self.assertEqual(rebuild.call_count, 2)
                self.assertNotEqual(recorded[-1], corrupt_digest)
                with b.TTFont(b.static_dir("SC", False) / cached.name) as font:
                    self.assertEqual(font["hmtx"].metrics["G"][0], 600)
                b.prepare_static_style("SC", stop, work, False, prepare_hints=False)
                self.assertEqual(rebuild.call_count, 2)


if __name__ == "__main__":
    unittest.main()
