"""静态字体管线：由 VF 实例化 unhinted 静态，以及混合 Hinting（ttfautohint + Chlorophytum）hinted 静态。"""
from .hint import build_hinted_statics
from .instance import build_unhinted_statics, instantiate_static_from_vf

__all__ = ["build_hinted_statics", "build_unhinted_statics", "instantiate_static_from_vf"]
