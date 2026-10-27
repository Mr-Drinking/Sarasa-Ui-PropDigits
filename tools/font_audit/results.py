"""Bounded evidence; successful individual measurements do not bloat JSON."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    cases: int = 0
    failures: int = 0
    samples: list = field(default_factory=list)
    sample_limit: int = 24

    def check(self, passed: bool, detail: dict) -> None:
        self.cases += 1
        if not passed:
            self.failures += 1
            if len(self.samples) < self.sample_limit:
                self.samples.append(detail)

    def report(self, **coverage) -> dict:
        return {"cases": self.cases, "failures": self.failures,
                "samples": self.samples, **coverage}
