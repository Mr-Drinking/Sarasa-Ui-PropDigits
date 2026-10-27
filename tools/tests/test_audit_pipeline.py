import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from font_audit import contract, pipeline
from font_audit.gate import SECTIONS


class AuditPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.font = self.root / "fonts/probe.ttf"
        self.font.parent.mkdir()
        self.font.write_bytes(b"first")
        patcher = patch.object(contract, "ROOT", self.root)
        patcher.start(); self.addCleanup(patcher.stop)
        self.unit = pipeline.Unit("metadata", "SC", False, "Regular", False)
        inputs = patch.object(pipeline.Unit, "inputs", lambda unit: [self.font])
        inputs.start(); self.addCleanup(inputs.stop)

    def test_checkpoint_identity_binds_inputs_code_and_provenance(self):
        key = pipeline.unit_key(self.unit, "code", "sources")
        self.assertEqual(key, pipeline.unit_key(self.unit, "code", "sources"))
        self.assertNotEqual(key, pipeline.unit_key(self.unit, "changed code", "sources"))
        self.assertNotEqual(key, pipeline.unit_key(self.unit, "code", "changed sources"))
        self.font.write_bytes(b"second")
        self.assertNotEqual(key, pipeline.unit_key(self.unit, "code", "sources"))

    def test_stale_corrupt_or_foreign_checkpoints_are_recomputed(self):
        path = self.root / "checkpoint.json"
        records = {"metadata": {"cases": 1, "failures": 0, "samples": []}}
        pipeline.store_checkpoint(path, self.unit, "key", records, 1.0)
        self.assertEqual(pipeline.load_checkpoint(path, self.unit, "key"), records)
        self.assertIsNone(pipeline.load_checkpoint(path, self.unit, "other key"))
        other = pipeline.Unit("metadata", "TC", False, "Regular", False)
        self.assertIsNone(pipeline.load_checkpoint(path, other, "key"))
        path.write_text(json.dumps({"unit": self.unit.name, "key": "key", "records": {}}), "utf-8")
        self.assertIsNone(pipeline.load_checkpoint(path, self.unit, "key"))
        path.write_text("{truncated", "utf-8")
        self.assertIsNone(pipeline.load_checkpoint(path, self.unit, "key"))

    def test_resumed_run_reuses_measurements_and_still_applies_the_gate(self):
        plan = {name: {} for name in SECTIONS}
        plan["metadata"] = {self.unit.name.split(":", 1)[1]: 1}
        calls = []

        def execute(unit):
            calls.append(unit.name)
            return {"metadata": {"cases": 1, "failures": 1, "samples": [{"check": "probe"}]}}

        checkpoints = self.root / "checkpoints"
        with (patch.object(pipeline, "plan", return_value=plan),
              patch.object(pipeline, "units", return_value=[self.unit]),
              patch.object(pipeline, "execute", execute),
              patch.object(pipeline.provenance, "capture", return_value={"source_manifest": {"a": 1}, "tool_identity": {"b": 2}}),
              patch.object(contract, "audit_input_manifest", return_value={}),
              patch.object(contract, "audit_contract_manifest", return_value={}),
              patch.object(contract, "measurement_contract_manifest", return_value={"code": "1"})):
            first = pipeline.run(regions=("SC",), selected={"metadata"}, jobs=1, checkpoint_dir=checkpoints, log=lambda _: None)
            second = pipeline.run(regions=("SC",), selected={"metadata"}, jobs=1, checkpoint_dir=checkpoints, log=lambda _: None)
        self.assertEqual(calls, [self.unit.name])
        self.assertEqual(first["records"], second["records"])
        for report in (first, second):
            self.assertEqual(report["gate"]["total_failures"], 1)
            self.assertFalse(report["gate"]["passed"])
            self.assertFalse(report["complete_scope"])


if __name__ == "__main__":
    unittest.main()
