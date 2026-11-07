"""Skips for tests that need the built or released fonts.

Fonts are not in git. The release commit's own CI run has no fonts yet and
may skip exactly these tests; every other skip (a missing pinned source, a
missing tool) fails CI. The reason prefix is how CI tells them apart.
"""
from __future__ import annotations

from pathlib import Path

RELEASE_FONT_SKIP = "需要成品字体："


def require_release_fonts(test, *paths) -> None:
    missing = [Path(path) for path in paths if not Path(path).is_file()]
    if missing:
        test.skipTest(RELEASE_FONT_SKIP + "、".join(path.name for path in missing[:3]))
