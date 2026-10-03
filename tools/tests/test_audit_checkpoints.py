from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import audit_sarasa_ui_propdigits as audit


class AuditCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.provenance = {"fonts": "font-hash", "sources": "source-hash",
                           "tools": "tool-hash", "contract": "code-hash"}

    def cache(self, provenance=None):
        return audit.AuditCheckpoints(self.directory, provenance or self.provenance)

    def test_completed_metadata_and_license_evidence_survive_resume(self):
        result = {"failures": [], "static_count": 144, "variable_count": 12,
                  "source_han_license_documents": {"ok": True, "documents": {"LICENSE": "sha"}}}
        self.assertEqual(self.cache().run("metadata", lambda: result), result)
        resumed = self.cache()
        with patch.object(audit, "audit_metadata", side_effect=AssertionError("must reuse")):
            self.assertEqual(resumed.run("metadata", audit.audit_metadata), result)
        self.assertEqual(resumed.reused, ["metadata"])

    def test_corrupt_incomplete_or_wrong_section_records_are_recomputed(self):
        cache = self.cache()
        cache.run("static_exact", lambda: [{"counts": {"outline": 0}}])
        path = next(cache.directory.glob("static_exact-*.json"))
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            lambda item: item.update(complete=False),
            lambda item: item.update(section="metadata"),
            lambda item: item.update(provenance_sha256="stale"),
            lambda item: item.update(result=[]),
            lambda item: item.pop("result_sha256"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(original); mutation(invalid)
                path.write_text(json.dumps(invalid), encoding="utf-8")
                calls = []
                result = self.cache().run("static_exact", lambda: calls.append(True) or ["recomputed"])
                self.assertEqual(calls, [True])
                self.assertEqual(result, ["recomputed"])
        path.write_text("{interrupted", encoding="utf-8")
        self.assertEqual(self.cache().run("static_exact", lambda: ["recovered"]), ["recovered"])

    def test_changed_font_source_tool_or_code_never_reuses_results(self):
        self.cache().run("static_exact", lambda: ["old"])
        for key in self.provenance:
            changed = {**self.provenance, key: "changed"}
            resumed = self.cache(changed)
            self.assertEqual(resumed.run("static_exact", lambda: ["new"]), ["new"])
            self.assertEqual(resumed.reused, [])

    def test_changed_raster_scope_never_reuses_results(self):
        cache = self.cache()
        def compute(ppems, regions=None):
            return {"ppems": list(ppems), "regions": regions}
        cache.run("static_raster", compute, (9, 12), regions=["SC"])
        resumed = self.cache()
        result = resumed.run("static_raster", compute, (9, 12, 16, 20, 24), regions=None)
        self.assertEqual(result["regions"], None)
        self.assertEqual(result["ppems"], [9, 12, 16, 20, 24])
        self.assertEqual(resumed.reused, [])

    def test_interrupted_computation_and_write_are_not_complete(self):
        cache = self.cache()
        def interrupted():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            cache.run("static_exact", interrupted)
        self.assertEqual(list(cache.directory.glob("static_exact-*.json")), [])
        with patch.object(audit.b, "write_json_atomic", side_effect=OSError("interrupted write")):
            with self.assertRaises(OSError):
                cache.run("static_exact", lambda: ["finished computation"])
        self.assertEqual(list(cache.directory.glob("static_exact-*.json")), [])
        self.assertEqual(self.cache().run("static_exact", lambda: ["complete"]), ["complete"])

    def test_cached_failures_and_skips_still_fail_the_release_gate(self):
        self.cache().run("static_exact", lambda: [{"counts": {"outline": 1}}])
        result = self.cache().run("static_exact", lambda: [])
        counts = {name: 0 for name in audit.AUDIT_GATE_SECTIONS}
        counts["static_exact"] = len(audit.nonzero(result))
        executed = {name: True for name in audit.AUDIT_GATE_SECTIONS}
        self.assertFalse(audit.build_audit_gate(counts, executed)["passed"])
        counts["static_exact"] = 0
        executed["static_raster"] = False
        self.assertFalse(audit.build_audit_gate(counts, executed)["passed"])
        with self.assertRaisesRegex(ValueError, "unregistered"):
            self.cache().run("invented", lambda: [])

    def test_reference_content_changes_invalidate_file_manifest(self):
        reference = self.directory / "reference.ttf"
        reference.write_bytes(b"original-source")
        before = audit.checkpoint_file_manifest([reference])
        reference.write_bytes(b"modified-source")
        self.assertNotEqual(audit.checkpoint_file_manifest([reference]), before)


if __name__ == "__main__":
    unittest.main()
