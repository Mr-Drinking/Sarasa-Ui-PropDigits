"""对照与产品检查共用的代表性排版语料定义。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Probe:
    name: str
    text: str
    script: str | None = None
    language: str | None = None
    direction: str = "ltr"
    features: tuple[tuple[str, int], ...] = ()


# 覆盖产品核心规则与第四份外部审计关键发现的真实语料
DEFAULT_PROBES: tuple[Probe, ...] = (
    Probe("digits_default", "0123456789 09:41 1,234.56 (10-20)", "Latn", "en"),
    Probe("digits_pnum", "0123456789 09:41 1,234.56", "Latn", "en", features=(("pnum", 1),)),
    Probe("digits_tnum", "0123456789 09:41 10-20", "Latn", "en", features=(("tnum", 1),)),
    Probe("digits_tnum_zero", "0123456789 00:00", "Latn", "en", features=(("tnum", 1), ("zero", 1))),
    Probe("colon_context", "1:2 1: :2 a:2 1:a a:b a::::::::::2", "Latn", "en"),
    Probe("calt_arrows_guard", "x<-1 3<-2 温度<-5℃ -> <-> <=>", "Latn", "en"),
    Probe("frac_guard", "1/2 2026/11/19 1/2/3", "Latn", "en", features=(("frac", 1),)),
    Probe("dlig_guard", "fffi Left:1", "Latn", "en", features=(("dlig", 1),)),
    Probe("sups_context", "H(H) T+1 1:2", "Latn", "en", features=(("sups", 1),)),
    Probe("kana_punct_kern", "です。ます、ク、", "Kana", "ja"),
    Probe("palt_kana", "ぁぉ「テスト」。", "Kana", "ja", features=(("palt", 1),)),
    Probe("vpal_cjk", "一ぁぉ「」。", "Hani", "ja", direction="ttb", features=(("vpal", 1),)),
    Probe("chws_zh", "「中文」，（测试）。", "Hani", "zh-Hans", features=(("chws", 1),)),
    Probe("vchw_zh", "「中文」，（测试）。", "Hani", "zh-Hans", direction="ttb", features=(("vchw", 1),)),
    Probe("cyrl_tnum_mark", "Время 10:11 и́ а̀", "Cyrl", "ru", features=(("tnum", 1),)),
    Probe("grek_tnum_mark", "Ώρα 10:11 Γ́ ʼρ", "Grek", "el", features=(("tnum", 1),)),
    Probe("punct_latn_und", "Loading… A—B A——B ⋯", "Latn", None),
    Probe("punct_hani_und", "中文……继续——检查———结束⸺⸻", "Hani", None),
    Probe("punct_hani_zh_hans", "中文……继续——检查———结束", "Hani", "zh-Hans"),
    Probe("punct_hani_ja", "日本語……—————", "Hani", "ja"),
    Probe("punct_hang_ko", "한국어……—――——", "Hang", "ko"),
    Probe("punct_bopo_und", "ㄅ……ㄆ——ㄇ", "Bopo", None),
    Probe("vert_dash_vert", "甲……乙——丙⸺丁———戊⸻己", "Hani", "zh-Hans", direction="ttb"),
    Probe("vert_dash_vrt2_only", "甲……乙——丙⸺丁︱︱", "Hani", "zh-Hans", direction="ttb", features=(("vert", 0), ("vrt2", 1))),
    Probe("vert_dash_off", "甲——乙︱︱丙", "Hani", "zh-Hans", direction="ttb", features=(("vert", 0), ("vrt2", 0))),
    Probe("old_hangul_lvt_ko", "\u1100\u1188\u11A8", "Hang", "ko"),
    Probe("old_hangul_lvt_und", "\u1100\u1188\u11A8", "Hang", None),
    Probe("old_hangul_lvt_zh", "\u1100\u1188\u11A8", "Hang", "zh-Hans"),
    Probe("cl_classical_chars", "㓱㙦䌙巅拼绿㪉㪘㪣㪰优忧㒶", "Hani", "zh-Hant"),
    Probe("uvs_quote", "\u2018\uFE01\u2019\uFE01", "Hani", "zh-Hans"),
    Probe("uvs_cjk", "\u4FAE\uFE00", "Hani", "ja"),
    Probe("ivs_ja", "\u845B\U000E0100\u845B\U000E0101", "Hani", "ja"),
    Probe("ui_sample_zh", "系统设置：第 12 项（2026-11-24 09:41）", "Hani", "zh-Hans"),
)
