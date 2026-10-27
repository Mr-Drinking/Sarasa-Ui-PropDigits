"""Small explicit release registry, with independently supplied case plans."""
from __future__ import annotations

import hashlib
import json


SECTIONS = (
    "inventory", "source_provenance", "metadata", "licenses", "region_boundaries",
    "static_outlines", "classical_outlines", "horizontal_metrics", "vertical_metrics",
    "shaping_default", "shaping_languages", "shaping_features", "gsub_sequences", "gpos_values",
    "inter_positioning", "digit_features", "colon_context", "vertical_joining",
    "inter_axis_curve", "cross_engine_metrics", "hint_execution", "raster", "style_geometry",
)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def summarize(records: dict, plan: dict[str, dict[str, int]], *, executed=None) -> dict:
    """A missing, duplicate, empty or undersized record cannot pass a section.

    The caller derives the plan from actual font/source inventories, not from
    counts or completion flags supplied by the report being verified.
    """
    if set(plan) != set(SECTIONS) or set(records) - set(SECTIONS):
        raise ValueError("审计章节与显式注册表不一致")
    executed = set(SECTIONS) if executed is None else set(executed)
    if executed - set(SECTIONS):
        raise ValueError("存在未注册的执行章节")
    sections = {}
    for name in SECTIONS:
        expected = plan[name]
        # A region-restricted run may have no applicable case for a section
        # (e.g. CL sources). Such a section is reported as skipped; a section
        # that was executed must still have a non-empty plan.
        if (not expected and name in executed) or any(type(count) is not int or count <= 0 for count in expected.values()):
            raise ValueError(f"审计计划缺少实际用例：{name}")
        rows = records.get(name, [])
        identities = [row.get("id") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        complete = (len(identities) == len(expected) and len(identities) == len(set(identities))
                    and set(identities) == set(expected))
        failures = 0
        if isinstance(rows, list):
            for row in rows:
                valid = (isinstance(row, dict) and row.get("id") in expected
                         and type(row.get("cases")) is int and row["cases"] >= expected[row["id"]]
                         and type(row.get("failures")) is int and row["failures"] >= 0
                         and isinstance(row.get("samples"), list)
                         and (row["failures"] > 0 or not row["samples"]))
                complete &= valid
                failures += row["failures"] if valid else 1
        else:
            failures = 1
        status = "skipped" if name not in executed else "failed" if failures else "passed" if complete else "incomplete"
        sections[name] = {"status": status, "complete": complete,
                          "records": len(identities), "expected_records": len(expected), "failure_count": failures}
    total = sum(section["failure_count"] for name, section in sections.items() if name in executed)
    complete = len(executed) == len(SECTIONS) and all(section["complete"] for section in sections.values())
    return {"schema_version": 3, "sections": sections, "plan_sha256": digest(plan),
            "total_failures": total, "executed_sections": len(executed),
            "skipped_sections": len(SECTIONS) - len(executed), "complete": complete,
            "passed": complete and total == 0}
