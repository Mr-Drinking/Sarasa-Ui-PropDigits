"""产品规则检查、OTS 校验、FreeType 严格执行扫描、hint 位移门与 VF/静态光栅一致性检查。"""
from .binary import (
    FT_FAST_PPEMS,
    HINT_DISP_PPEMS,
    RASTER_PARITY_PPEMS,
    check_hint_displacement,
    check_hinted_unhinted_parity,
    check_ots,
    check_unhinted_raster_parity,
    check_vf_static_parity,
    scan_freetype_pedantic,
)
from .rules import CheckResult, check_font_rules

__all__ = [
    "CheckResult",
    "FT_FAST_PPEMS",
    "HINT_DISP_PPEMS",
    "RASTER_PARITY_PPEMS",
    "check_font_rules",
    "check_hint_displacement",
    "check_hinted_unhinted_parity",
    "check_ots",
    "check_unhinted_raster_parity",
    "check_vf_static_parity",
    "scan_freetype_pedantic",
]
