import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from font_audit.gate import SECTIONS, summarize


class IndependentGateTests(unittest.TestCase):
    def setUp(self):
        self.plan = {name: {"fixture-regular": 10, "fixture-italic": 10} for name in SECTIONS}
        self.records = {name: [{"id": identifier, "cases": 10, "failures": 0, "samples": []}
                               for identifier in entries] for name, entries in self.plan.items()}

    def test_complete_measurements_pass(self):
        self.assertTrue(summarize(self.records, self.plan)["passed"])

    def test_empty_or_wrong_identity_cannot_pass_any_section(self):
        for section in SECTIONS:
            for mutation in ("empty", "duplicate", "wrong_style", "zero", "short"):
                with self.subTest(section=section, mutation=mutation):
                    changed = copy.deepcopy(self.records)
                    if mutation == "empty":
                        changed[section] = []
                    elif mutation == "duplicate":
                        changed[section][1] = changed[section][0]
                    elif mutation == "wrong_style":
                        changed[section][0]["id"] = "unknown-style"
                    else:
                        changed[section][0]["cases"] = 0 if mutation == "zero" else 9
                    self.assertFalse(summarize(changed, self.plan)["passed"])

    def test_skipped_raster_is_partial_and_unregistered_work_is_rejected(self):
        self.records["raster"] = []
        result = summarize(self.records, self.plan, executed=set(SECTIONS) - {"raster"})
        self.assertEqual(result["total_failures"], 0)
        self.assertFalse(result["complete"])
        self.assertFalse(result["passed"])
        self.records["unknown"] = []
        with self.assertRaises(ValueError):
            summarize(self.records, self.plan)

    def test_a_single_recorded_failure_fails_its_section(self):
        self.records["gpos_values"][0]["failures"] = 1
        self.records["gpos_values"][0]["samples"] = [{"text": "LY"}]
        result = summarize(self.records, self.plan)
        self.assertEqual(result["total_failures"], 1)
        self.assertEqual(result["sections"]["gpos_values"]["status"], "failed")
        self.assertFalse(result["passed"])

    def test_failure_without_evidence_or_evidence_without_failure_is_invalid(self):
        for failures, samples in ((0, [{"text": "stale"}]), (-1, [])):
            with self.subTest(failures=failures):
                changed = copy.deepcopy(self.records)
                changed["metadata"][0].update(failures=failures, samples=samples)
                self.assertFalse(summarize(changed, self.plan)["passed"])

    def test_registry_and_plans_cannot_be_reduced(self):
        reduced = {name: entries for name, entries in self.plan.items() if name != "metadata"}
        with self.assertRaises(ValueError):
            summarize(self.records, reduced)
        empty = dict(self.plan, metadata={})
        with self.assertRaises(ValueError):
            summarize(self.records, empty)
        # A region-restricted run may lack a section's cases only if it is skipped.
        result = summarize(self.records, empty, executed=set(SECTIONS) - {"metadata"})
        self.assertEqual(result["sections"]["metadata"]["status"], "skipped")
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
