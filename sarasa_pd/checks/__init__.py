"""产品规则检查、OTS 校验、FreeType 严格执行扫描与 VF/静态一致性检查。"""
from .binary import (
    FT_FAST_PPEMS,
    check_hinted_unhinted_parity,
    check_ots,
    check_vf_static_parity,
    scan_freetype_pedantic,
)
from .rules import CheckResult, check_font_rules

__all__ = [
    "CheckResult",
    "FT_FAST_PPEMS",
    "check_font_rules",
    "check_hinted_unhinted_parity",
    "check_ots",
    "check_vf_static_parity",
    "scan_freetype_pedantic",
]
