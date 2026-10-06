from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import audit_sarasa_ui_propdigits as a
import package_release as package


class AuditCaseCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = json.loads((ROOT / "reports/release-audit.json").read_text(encoding="utf-8"))
        cls.baseline["audit_contract"] = a.audit_contract_manifest()
        cls.baseline["gate"] = a.recompute_audit_gate(cls.baseline)
        if not cls.baseline["gate"]["passed"]:
            raise AssertionError({name: item for name, item in cls.baseline["gate"]["sections"].items()
                                  if item["status"] != "passed"})

    def reject(self, report):
        self.assertFalse(a.recompute_audit_gate(report)["passed"])
        # Retain the old true flags and complete summary, as in the independent audit.
        with self.assertRaises(ValueError):
            package.validate_main_audit_report(report)

    def test_complete_case_evidence_is_accepted(self):
        package.validate_main_audit_report(self.baseline)

    def test_each_empty_section_fails_audit_and_packager(self):
        for section, field in a.AUDIT_RESULT_FIELDS.items():
            with self.subTest(section=section):
                report = copy.deepcopy(self.baseline)
                report[field] = []
                self.reject(report)
        report = copy.deepcopy(self.baseline)
        report["metadata"] = {}
        self.reject(report)

    def test_missing_duplicate_or_wrong_style_cases_are_not_complete(self):
        for section, field in a.AUDIT_RESULT_FIELDS.items():
            for mutation in ("missing", "duplicate", "wrong_region"):
                with self.subTest(section=section, mutation=mutation):
                    report = copy.deepcopy(self.baseline)
                    rows = report[field]
                    if mutation == "missing":
                        rows.pop()
                    elif mutation == "duplicate":
                        rows[-1] = copy.deepcopy(rows[0])
                    else:
                        rows[0]["region"] = "invalid-region"
                    self.reject(report)

    def test_zero_codepoint_or_nested_axis_coverage_is_rejected(self):
        mutations = (
            ("static_exact", "coverage", {}),
            ("static_raster", "renders_checked", 0),
            ("static_upstream_dash_sources", "role_details", {}),
            ("vf_ellipsis_shaping", "control_status", {}),
            ("vf_upstream_dash_sources", "control_details", {}),
            ("vf_source_pairing", "inter_controls", {}),
            ("vf_weight_curves", "weights", []),
            ("vf_cjk_strokes", "checked_codepoints", 0),
            ("static_layout_templates", "tables_checked", []),
            ("static_propdigits_shaping", "checked_tests", []),
            ("static_palt_shaping", "checked_tests", []),
            ("static_em_dash_shaping", "case_coverage", {}),
            ("vf_em_dash_shaping", "case_coverage", {}),
            ("vf_contextual_spacing", "spacing_evidence", {}),
            ("vf_source_gpos_variations", "checked_features", []),
        )
        for field, key, value in mutations:
            with self.subTest(field=field, key=key):
                report = copy.deepcopy(self.baseline)
                report[field][0][key] = value
                self.reject(report)

    def test_counts_and_execution_flags_without_measurements_cannot_pass(self):
        gate = a.build_audit_gate({name: 0 for name in a.AUDIT_GATE_SECTIONS},
                                  {name: True for name in a.AUDIT_GATE_SECTIONS})
        self.assertFalse(gate["complete"])
        self.assertFalse(gate["passed"])


if __name__ == "__main__":
    unittest.main()
