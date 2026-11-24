"""--subset smoke 冒烟码位集合，以及完整保留 cmap format 14 (UVS) 的子集化工具。"""
from __future__ import annotations

from collections.abc import Iterable
from fontTools import subset as ft_subset
from fontTools.ttLib import TTFont

from .spec import Spec, load_spec


# 覆盖所有关键回归与常见界面字符的冒烟样本（约 1000 码位以内）
SMOKE_TEXTS: tuple[str, ...] = (
    # ASCII、数字、标点、括号、运算符与代码/日期回归（A2, A4, A12）
    " !\"#$%&'()*+,-./0123456789:;<=>?@",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
    "x<-1 3<-2 温度<-5℃ 2026/11/19 1/2/3 fffi -> <-> <=> 1:2 1: :2 a:2 Left:1 H(H) T+",
    # 扩展拉丁、烘焙 ss03/cv10 与附加符号、常见欧洲字符
    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖ×ØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõö÷øùúûüýþÿ",
    "ĀāĂăĄąĆćĈĉĊċČčĎďĐđĒēĔĕĖėĘęĚěĜĝĞğĠġĢģĤĥĦħĨĩĪīĬĭĮįİıĲĳĴĵĶķĸĹĺĻļĽľĿŀŁł",
    "ŃńŅņŇňŉŊŋŌōŎŏŐőŒœŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŦŧŨũŪūŬŭŮůŰűŲųŴŵŶŷŸŹźŻżŽžſ",
    "ƒƠơƯưǍǎǏǐǑǒǓǔǦǧǴǵȘșȚțȷẞṱṭṷḷḠḁê̌Ê̌ʼρ±−≈≠≤≥‰′″‵‴‶‷·‣⁃‘’“”–—―…⋯⸺⸻︱",
    # 西里尔与希腊（含组合重音与俄/希界面）
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюяВремя",
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρςστυφχψωἹи́а̀Γ́",
    # 中文标点、全角符号与常用界面汉字
    "，。、；：？！「」『』（）【】《》〈〉—…　",
    "一二三四五六七八九十百千万亿零中文简体繁体香港台湾日本韩国排版测试",
    "系统设置通知消息搜索文件编辑查看窗口帮助确定取消保存关闭打开退出返回",
    "默认字体界面显示比例数字等宽标点挤压斜体粗细可变静态预览确认更新安装",
    # CL 尚古差异、A5 全宽修正与间距基字
    "㓱㙦䌙巅拼绿㪉㪘㪣㪰优忧扰抚噘摁㽪团嗨㒶丸備槙醤侮",
    # 假名＋句读（A3）与竖排常用符号
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをんぁぃぅぇぉっゃゅょ",
    "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンァィゥェォッャュョー・",
    "です。ます、ク、〱〲〳",
    # 注音（A10 bopo）
    "ㄅㄆㄇㄈㄉㄊㄋㄌㄍㄎㄏㄐㄑㄒㄓㄔㄕㄖㄗㄘㄙㄧㄨㄩㄚㄛㄜㄝㄞㄟㄠㄡㄢㄣㄤㄥㄦ",
    # 韩文音节、A13 曾失败字形与古韩文 L+V+T
    "가나다라마바사아자차카타파하한글사라사고딕설정확인취소저장닫기",
    "돌들를름뮋뢋뢧뤗릣靇矗聾釁龗龍詎詚詛",
)

# 单独补入不可见字符、组合标记、空格与古韩文字母
SMOKE_EXTRA_CODEPOINTS: tuple[int, ...] = (
    0x00A0,  # NBSP
    0x0300, 0x0301, 0x0302, 0x0308, 0x030C, 0x0323, 0x0360,  # 组合附加符号与 U+0360
    0x0488, 0x0489,
    0x1100, 0x1101, 0x1102, 0x1161, 0x1188, 0x11A8, 0x11AB, 0x11AF,  # 古韩文 L+V+T
    0x2000, 0x2001, 0x2007, 0x2008, 0x2009, 0x200A, 0x200C, 0x200D, 0x2010, 0x2011, 0x2012, 0x2013,
    0x2060,
    0x302A, 0x3099, 0x309A,
    0x31B4, 0x31B5, 0x31B6, 0x31B7, 0x31BB,
    0xFA30,
    0xFE00, 0xFE01, 0xFE0E, 0xFE0F,  # 标准化变体选择符（SVS）
    0xE0100, 0xE0101, 0xE0102,       # 表意文字变体选择符（IVS）
    0xFFA0, 0xFFA1,
    0x28A99,                         # 𨪙
)


def smoke_codepoints(spec: Spec | None = None) -> set[int]:
    spec = spec or load_spec()
    cps: set[int] = set(SMOKE_EXTRA_CODEPOINTS)
    cps.update(spec.propdigits_codepoints)
    cps.update(spec.tnum_extra_codepoints)
    cps.update(spec.tabular_space_codepoints)
    cps.update(spec.dash_cmap_codepoints)
    cps.update(spec.classical_spacing_bases)
    cps.update(spec.classical_fullwidth_check)
    cps.update(spec.ui_autohint_unscaled)
    cps.update(spec.sanitizer_pwid.keys())
    for text in SMOKE_TEXTS:
        for ch in text:
            cps.add(ord(ch))
    return cps


def uvs_selectors_for_codepoints(font: TTFont, unicodes: set[int]) -> tuple[set[int], set[str]]:
    """收集 cmap format 14 中适用于 unicodes 的变体选择符及目标字形名（修 A1）。"""
    selectors: set[int] = set()
    glyphs: set[str] = set()
    if "cmap" not in font:
        return selectors, glyphs
    for subtable in font["cmap"].tables:
        if subtable.format != 14:
            continue
        for selector, pairs in subtable.uvsDict.items():
            matched = False
            for base_cp, glyph_name in pairs:
                if base_cp in unicodes:
                    matched = True
                    if glyph_name is not None:
                        glyphs.add(glyph_name)
            if matched:
                selectors.add(selector)
    return selectors, glyphs


def subset_font_preserving_uvs(
    font: TTFont,
    unicodes: Iterable[int],
    *,
    extra_glyphs: Iterable[str] = (),
    retain_gids: bool = False,
    drop_hints: bool = False,
) -> TTFont:
    """按给定码位子集化字体，显式保留 cmap format 14 的 SVS/IVS 与变体字形（修 A1）。"""
    keep_cps = set(unicodes)
    uvs_selectors, uvs_glyphs = uvs_selectors_for_codepoints(font, keep_cps)

    options = ft_subset.Options()
    options.layout_features = ["*"]
    options.layout_scripts = ["*"]
    options.glyph_names = True
    options.name_IDs = ["*"]
    options.name_legacy = True
    options.name_languages = ["*"]
    options.notdef_glyph = True
    options.notdef_outline = True
    options.recommended_glyphs = True
    options.retain_gids = retain_gids
    options.hinting = not drop_hints
    options.drop_tables = []

    subsetter = ft_subset.Subsetter(options=options)
    subsetter.populate(
        unicodes=keep_cps | uvs_selectors,
        glyphs=set(extra_glyphs) | uvs_glyphs,
    )
    subsetter.subset(font)
    return font
