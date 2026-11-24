"""调用 chws_tool 为 VF 添加 Noto 上下文全宽间距（chws / vchw），斜体按剪切前正体墨迹测算。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib.metadata
import logging
import math
from pathlib import Path
from typing import Any, Iterator

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

from ..spec import Spec, load_spec


def _has_gpos_feature(font: TTFont, tag: str) -> bool:
    if "GPOS" not in font or not font["GPOS"].table.FeatureList:
        return False
    return any(r.FeatureTag == tag for r in font["GPOS"].table.FeatureList.FeatureRecord)


@contextmanager
def upright_ink_measurement() -> Iterator[None]:
    """在斜体上以 em/3 反向剪切恢复正体墨迹，避免 east-asian-spacing 误判标点落半。"""
    from east_asian_spacing import font as spacing_font

    original = spacing_font.Font.glyph_bounds

    def glyph_bounds(self: Any, glyph: Any) -> Any:
        ttfont = self.ttfont
        angle = float(ttfont["post"].italicAngle) if ttfont is not None and "post" in ttfont else 0.0
        if not angle:
            return original(self, glyph)
        slope = math.tan(math.radians(-angle))
        center = ttfont["head"].unitsPerEm / 3
        glyph_set = self.ttglyphset
        pen = BoundsPen(glyph_set)
        glyph_set[self.glyph_name(glyph)].draw(TransformPen(pen, (1, 0, -slope, 1, slope * center, 0)))
        return pen.bounds

    spacing_font.Font.glyph_bounds = glyph_bounds
    try:
        yield
    finally:
        spacing_font.Font.glyph_bounds = original


def add_chws_to_file(path: Path, spec: Spec | None = None) -> dict[str, Any]:
    """为磁盘上的 VF 添加 chws / vchw（幂等；若已存在则不重复运行，且保留固定时间戳与版本）。"""
    spec = spec or load_spec()
    with TTFont(path, recalcTimestamp=False) as before:
        modified = before["head"].modified
        before_feats = (
            len(before["GPOS"].table.FeatureList.FeatureRecord)
            if "GPOS" in before and before["GPOS"].table.FeatureList
            else 0
        )
        before_lookups = (
            len(before["GPOS"].table.LookupList.Lookup)
            if "GPOS" in before and before["GPOS"].table.LookupList
            else 0
        )
        had_both = _has_gpos_feature(before, "chws") and _has_gpos_feature(before, "vchw")

    if not had_both:
        from chws_tool import add_chws_async

        config_logger = logging.getLogger("config")
        prev_level = config_logger.level
        config_logger.setLevel(logging.ERROR)
        try:
            with upright_ink_measurement():
                result = asyncio.run(add_chws_async(path, path))
        finally:
            config_logger.setLevel(prev_level)
        if result is None:
            raise RuntimeError(f"chws_tool 未能向字体添加 chws/vchw：{path}")

        with TTFont(path, recalcTimestamp=False) as norm:
            norm["head"].modified = modified
            norm["head"].fontRevision = spec.font_revision()
            norm.save(path, reorderTables=True)

    with TTFont(path, recalcTimestamp=False) as after:
        has_chws = _has_gpos_feature(after, "chws")
        has_vchw = _has_gpos_feature(after, "vchw")
        if not (has_chws and has_vchw):
            raise RuntimeError(f"添加后仍缺少 chws 或 vchw：{path}")
        after_feats = len(after["GPOS"].table.FeatureList.FeatureRecord)
        after_lookups = len(after["GPOS"].table.LookupList.Lookup)

    return {
        "chws_present": has_chws,
        "vchw_present": has_vchw,
        "chws_tool_version": importlib.metadata.version("chws-tool"),
        "gpos_features_added": after_feats - before_feats,
        "gpos_lookups_added": after_lookups - before_lookups,
    }
