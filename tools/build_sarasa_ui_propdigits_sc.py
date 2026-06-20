from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import site
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any


PYTHON_DEPS = {
    "fontTools": ("fonttools", "fonttools[woff]==4.63.0", "4.63.0"),
    "uharfbuzz": ("uharfbuzz", "uharfbuzz==0.55.0", "0.55.0"),
    "brotli": ("Brotli", "brotli==1.2.0", "1.2.0"),
    "ttfautohint": ("ttfautohint-py", "ttfautohint-py==0.6.0", "0.6.0"),
    "py7zr": ("py7zr", "py7zr==1.1.0", "1.1.0"),
    "afdko": ("afdko", "afdko==5.0.1", "5.0.1"),
}


def ensure_python_deps() -> None:
    if os.environ.get("SARASA_SKIP_PYTHON_DEPS") == "1":
        return
    needed = []
    for module, (distribution, package_spec, expected_version) in PYTHON_DEPS.items():
        installed_version = None
        try:
            installed_version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            pass
        if importlib.util.find_spec(module) is None or installed_version != expected_version:
            needed.append(package_spec)
    if not needed:
        return
    print(f"[build] install Python build dependencies: {' '.join(needed)}", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", *needed])


ensure_python_deps()

from fontTools import subset
from fontTools.misc.fixedTools import otRound
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.ttLib.tables import _g_l_y_f as glyf_table
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.varLib.models import piecewiseLinearMap
from fontTools.varLib.instancer import instantiateVariableFont


def patch_fonttools_overlap_simple_repeat_encoding() -> None:
    def compile_deltas_greedy_ots_safe(self: Any, flags: Any, deltas: Any) -> tuple[bytearray, bytearray, bytearray]:
        compressed_flags = bytearray()
        compressed_xs = bytearray()
        compressed_ys = bytearray()
        last_flag = None
        repeat = 0
        for flag, (x, y) in zip(flags, deltas):
            if x == 0:
                flag = flag | glyf_table.flagXsame
            elif -255 <= x <= 255:
                flag = flag | glyf_table.flagXShort
                if x > 0:
                    flag = flag | glyf_table.flagXsame
                else:
                    x = -x
                compressed_xs.append(x)
            else:
                compressed_xs.extend(struct.pack(">h", x))
            if y == 0:
                flag = flag | glyf_table.flagYsame
            elif -255 <= y <= 255:
                flag = flag | glyf_table.flagYShort
                if y > 0:
                    flag = flag | glyf_table.flagYsame
                else:
                    y = -y
                compressed_ys.append(y)
            else:
                compressed_ys.extend(struct.pack(">h", y))

            if flag == last_flag and repeat != 255:
                repeat += 1
                if flag & glyf_table.flagOverlapSimple:
                    if repeat == 1:
                        compressed_flags[-1] = flag | glyf_table.flagRepeat
                        compressed_flags.append(repeat)
                    else:
                        compressed_flags[-1] = repeat
                elif repeat == 1:
                    compressed_flags.append(flag)
                else:
                    compressed_flags[-2] = flag | glyf_table.flagRepeat
                    compressed_flags[-1] = repeat
            else:
                repeat = 0
                compressed_flags.append(flag)
            last_flag = flag
        return compressed_flags, compressed_xs, compressed_ys

    glyf_table.Glyph.compileDeltasGreedy = compile_deltas_greedy_ots_safe


patch_fonttools_overlap_simple_repeat_encoding()


ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = Path(os.environ.get("SARASA_WORK_ROOT", ROOT.parent))


def first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def log_step(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[build {timestamp}] {message}", flush=True)


SARASA_VERSION = "1.0.39"
SARASA_TAG = f"v{SARASA_VERSION}"
SOURCE_HAN_TAG = "2.005R"
INTER_TAG = "v4.1"
SHANGGU_TAG = "1.028"
SHANGGU_SANS_TTF_ARCHIVE_NAME = "ShangguSansTTFs.7z"
SHANGGU_SANS_TTF_SHA256 = "a7fc794127270fff06224e2129e28ea917669bb3601296f219ea64fe0266b6f0"
SHANGGU_SANS_VF_ARCHIVE_NAME = "ShangguSansVF_TTFs.7z"
SHANGGU_SANS_VF_SHA256 = "31b207a05332196ff444114d66de1c7b622d3a7244ec15a2485e8d0844cb1984"
NODE_VERSION = "v26.3.0"
SOURCE_ARCHIVE_DIR = WORK_ROOT / "source-archives"
NODE_DIR = Path(os.environ.get("SARASA_NODE_DIR", WORK_ROOT / "node"))
REFERENCE_ROOT = Path(os.environ.get("REFERENCE_SARASA_ROOT", WORK_ROOT / "official-sarasa-ui"))
REFERENCE_SC_LEGACY_ROOT = Path(
    os.environ.get("REFERENCE_SARASA_SC_ROOT", WORK_ROOT / "official-sarasa-ui-sc")
)

SRC_DIR = Path(os.environ.get("VF_SOURCE_DIR", first_existing(WORK_ROOT / "vf-sources", ROOT / "work" / "vf-sources")))
INTER_UPRIGHT = Path(os.environ.get("INTER_VF", SRC_DIR / "InterVariable.ttf"))
INTER_ITALIC = Path(os.environ.get("INTER_ITALIC_VF", SRC_DIR / "InterVariable-Italic.woff2"))

REGION_ORDER = ["CL", "SC", "TC", "HC", "J", "K"]
REGION_CONFIGS: dict[str, dict[str, Any]] = {
    "CL": {
        "source_han_static_prefix": "SourceHanSansK",
        "source_han_vf_basename": "SourceHanSansK-VF.ttf",
        "classical_vf_override_basename": f"shanggu-{SHANGGU_TAG}/ShangguSansTC-VF.ttf",
        "sarasa_prefix": "SarasaUiCL",
        "local_family": "更紗黑體",
        "local_lang_id": 0x0404,
        "classical": True,
    },
    "SC": {
        "source_han_static_prefix": "SourceHanSansSC",
        "source_han_vf_basename": "SourceHanSansSC-VF.ttf",
        "classical_vf_override_basename": None,
        "sarasa_prefix": "SarasaUiSC",
        "local_family": "更纱黑体",
        "local_lang_id": 0x0804,
        "classical": False,
    },
    "TC": {
        "source_han_static_prefix": "SourceHanSansTC",
        "source_han_vf_basename": "SourceHanSansTC-VF.ttf",
        "classical_vf_override_basename": None,
        "sarasa_prefix": "SarasaUiTC",
        "local_family": "更紗黑體",
        "local_lang_id": 0x0404,
        "classical": False,
    },
    "HC": {
        "source_han_static_prefix": "SourceHanSansHC",
        "source_han_vf_basename": "SourceHanSansHC-VF.ttf",
        "classical_vf_override_basename": None,
        "sarasa_prefix": "SarasaUiHC",
        "local_family": "更紗黑體",
        "local_lang_id": 0x0C04,
        "classical": False,
    },
    "J": {
        "source_han_static_prefix": "SourceHanSans",
        "source_han_vf_basename": "SourceHanSans-VF.ttf",
        "classical_vf_override_basename": None,
        "sarasa_prefix": "SarasaUiJ",
        "local_family": "更紗ゴシック",
        "local_lang_id": 0x0411,
        "classical": False,
    },
    "K": {
        "source_han_static_prefix": "SourceHanSansK",
        "source_han_vf_basename": "SourceHanSansK-VF.ttf",
        "classical_vf_override_basename": None,
        "sarasa_prefix": "SarasaUiK",
        "local_family": "사라사 고딕",
        "local_lang_id": 0x0412,
        "classical": False,
    },
}


def check_region(region: str) -> str:
    region = region.upper()
    if region not in REGION_CONFIGS:
        raise ValueError(f"unknown region {region!r}; expected one of {', '.join(REGION_ORDER)}")
    return region


def region_config(region: str) -> dict[str, Any]:
    return REGION_CONFIGS[check_region(region)]


def region_reference_root(region: str) -> Path:
    region = check_region(region)
    if region == "SC" and REFERENCE_SC_LEGACY_ROOT.exists():
        return REFERENCE_SC_LEGACY_ROOT
    return REFERENCE_ROOT / region


def region_reference_dir(region: str, hinted: bool) -> Path:
    return region_reference_root(region) / ("hinted" if hinted else "unhinted")


def sarasa_region_prefix(region: str) -> str:
    return str(region_config(region)["sarasa_prefix"])


def source_han_static_prefix(region: str) -> str:
    return str(region_config(region)["source_han_static_prefix"])


def source_han_vf_basename(region: str) -> str | None:
    value = region_config(region)["source_han_vf_basename"]
    return str(value) if value else None


def classical_vf_override_basename(region: str) -> str | None:
    value = region_config(region)["classical_vf_override_basename"]
    return str(value) if value else None


def source_han_vf_path(region: str) -> Path:
    region = check_region(region)
    env_value = os.environ.get(f"SOURCE_HAN_{region}_VF")
    if region == "SC":
        env_value = env_value or os.environ.get("SOURCE_HAN_SC_VF")
    basename = source_han_vf_basename(region)
    if not basename:
        raise ValueError(f"region {region} has no Source Han Sans VF equivalent")
    return Path(env_value) if env_value else SRC_DIR / basename


def classical_vf_override_path(region: str) -> Path | None:
    region = check_region(region)
    basename = classical_vf_override_basename(region)
    if not basename:
        return None
    env_value = (
        os.environ.get(f"SHANGGU_{region}_VF")
        or os.environ.get(f"CLASSICAL_{region}_VF")
        or os.environ.get("SHANGGU_SANS_TC_VF")
        or os.environ.get("SHANGGU_CLASSICAL_VF")
    )
    return Path(env_value) if env_value else SRC_DIR / basename


def classical_static_override_path(region: str, weight_name: str) -> Path | None:
    region = check_region(region)
    if not region_config(region)["classical"]:
        return None
    source = STATIC_STYLE_SOURCES[weight_name]
    shanggu_weight = str(source["shs"])
    file_name = f"ShangguSansTC-{shanggu_weight}.ttf"
    env_value = (
        os.environ.get(f"SHANGGU_{region}_{weight_name.upper()}_TTF")
        or os.environ.get(f"CLASSICAL_{region}_{weight_name.upper()}_TTF")
    )
    if env_value:
        return Path(env_value)
    env_dir = (
        os.environ.get(f"SHANGGU_{region}_STATIC_DIR")
        or os.environ.get(f"CLASSICAL_{region}_STATIC_DIR")
        or os.environ.get("SHANGGU_SANS_TC_STATIC_DIR")
        or os.environ.get("SHANGGU_CLASSICAL_STATIC_DIR")
    )
    if env_dir:
        return Path(env_dir) / file_name
    return SRC_DIR / f"shanggu-{SHANGGU_TAG}" / "static" / file_name


BASE_VF = source_han_vf_path("SC")
REFERENCE_SARASA = Path(
    os.environ.get(
        "REFERENCE_SARASA",
        first_existing(
            region_reference_dir("SC", False) / "SarasaUiSC-Regular.ttf",
            REFERENCE_SC_LEGACY_ROOT / f"SarasaUiSC-TTF-Unhinted-{SARASA_VERSION}" / "SarasaUiSC-Regular.ttf",
            WORK_ROOT / "sarasa-original-unhinted" / "SarasaUiSC-Regular.ttf",
        ),
    )
)
REFERENCE_SARASA_DIR = REFERENCE_SARASA.parent
REFERENCE_SARASA_HINTED_DIR = Path(
    os.environ.get(
        "REFERENCE_SARASA_HINTED_DIR",
        first_existing(
            region_reference_dir("SC", True),
            REFERENCE_SC_LEGACY_ROOT / f"SarasaUiSC-TTF-{SARASA_VERSION}",
            WORK_ROOT / "sarasa-original" / f"SarasaUiSC-TTF-{SARASA_VERSION}",
            REFERENCE_SARASA_DIR,
        ),
    )
)

SARASA_SOURCE_DIR = Path(
    os.environ.get(
        "SARASA_SOURCE_DIR",
        first_existing(WORK_ROOT / "Sarasa-Gothic", WORK_ROOT / "sarasa-gothic-src", ROOT / "work" / "Sarasa-Gothic"),
    )
)
SARASA_CHLOROPHYTUM = Path(
    os.environ.get(
        "SARASA_CHLOROPHYTUM",
        SARASA_SOURCE_DIR / "node_modules" / "@chlorophytum" / "cli" / "bin" / "_startup",
    )
)
SARASA_HINT_CONFIGS = {
    "ExtraLight": "ExtraLight",
    "Light": "Light",
    "Normal": "Regular",
    "Regular": "Regular",
    "Medium": "SemiBold",
    "Bold": "Bold",
    "Heavy": "Bold",
}
SARASA_HINT_JOBS = int(os.environ.get("SARASA_HINT_JOBS", str(os.cpu_count() or 1)))

VARIABLE_DIR = ROOT / "fonts" / "variable"
STATIC_ROOT = ROOT / "fonts" / "static"
STATIC_DIR = STATIC_ROOT / f"SarasaUiPropDigitsSC-TTF-{SARASA_VERSION}"
STATIC_UNHINTED_DIR = STATIC_ROOT / f"SarasaUiPropDigitsSC-TTF-Unhinted-{SARASA_VERSION}"
REPORT_DIR = ROOT / "reports"
BUILD_CACHE_DIR = Path(os.environ.get("SARASA_BUILD_CACHE", ROOT / ".build-cache" / "sarasa-ui-propdigits"))

AXIS_LIMIT = {"wght": (250, 400, 900)}
PUBLIC_AXIS_LIMIT = {"wght": (200, 400, 900)}
INTER_AXIS_LIMIT = {"opsz": 14, "wght": (200, 400, 900)}
VF_FAMILY = "Sarasa Ui VF PropDigits SC"
VF_PS_FAMILY = "Sarasa-Ui-VF-PropDigits-SC"
VF_FAMILY_ZH_HANS = "更纱黑体 Ui VF PropDigits SC"
STATIC_FAMILY = "Sarasa Ui PropDigits SC"
STATIC_PS_FAMILY = "Sarasa-Ui-PropDigits-SC"
STATIC_FAMILY_ZH_HANS = "更纱黑体 Ui PropDigits SC"
VERSION = "1.0.39.2"
INTER_PREFIX = "inter."
OS2_VENDOR_ID = "MRDK"

def vf_family(region: str) -> str:
    return f"Sarasa Ui VF PropDigits {check_region(region)}"


def vf_ps_family(region: str) -> str:
    return f"Sarasa-Ui-VF-PropDigits-{check_region(region)}"


def vf_family_local(region: str) -> str:
    config = region_config(region)
    return f"{config['local_family']} Ui VF PropDigits {check_region(region)}"


def static_family(region: str) -> str:
    return f"Sarasa Ui PropDigits {check_region(region)}"


def static_ps_family(region: str) -> str:
    return f"Sarasa-Ui-PropDigits-{check_region(region)}"


def static_family_local(region: str) -> str:
    config = region_config(region)
    return f"{config['local_family']} Ui PropDigits {check_region(region)}"


def static_dir(region: str, hinted: bool) -> Path:
    region = check_region(region)
    hint_part = "TTF" if hinted else "TTF-Unhinted"
    return STATIC_ROOT / f"SarasaUiPropDigits{region}-{hint_part}-{SARASA_VERSION}"


def static_file_prefix(region: str) -> str:
    return f"SarasaUiPropDigits{check_region(region)}"


def variable_output_name(region: str, italic: bool) -> str:
    region = check_region(region)
    suffix = f"{region}-Italic[wght].ttf" if italic else f"{region}[wght].ttf"
    return f"Sarasa-Ui-VF-PropDigits-{suffix}"


def variable_regions(regions: list[str]) -> list[str]:
    return [region for region in regions if source_han_vf_basename(region)]


SOURCE_HAN_WEIGHT_STOPS = [
    {"name": "ExtraLight", "value": 200, "range_min": 200, "range_max": 299},
    {"name": "Light", "value": 300, "range_min": 300, "range_max": 349},
    {"name": "Normal", "value": 350, "range_min": 350, "range_max": 399},
    {"name": "Regular", "value": 400, "range_min": 400, "range_max": 499, "flags": 0x2},
    {"name": "Medium", "value": 500, "range_min": 500, "range_max": 650},
    {"name": "Bold", "value": 700, "range_min": 650, "range_max": 800},
    {"name": "Heavy", "value": 900, "range_min": 800, "range_max": 900},
]

SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT = {
    200: 250,
    300: 300,
    350: 350,
    400: 400,
    500: 500,
    700: 700,
    900: 900,
}
INTER_OUTLINE_CORRECTION_WEIGHTS = [300, 350, 500, 700]

STATIC_STYLE_SOURCES = {
    "ExtraLight": {"shs": "ExtraLight", "inter": "ExtraLight", "sarasa": "ExtraLight", "hcfg": "ExtraLight"},
    "Light": {"shs": "Light", "inter": "Light", "sarasa": "Light", "hcfg": "Light"},
    "Normal": {"shs": "Normal", "inter": None, "inter_weight": 350, "sarasa": "Regular", "hcfg": "Regular"},
    "Regular": {"shs": "Regular", "inter": "Regular", "sarasa": "Regular", "hcfg": "Regular"},
    "Medium": {"shs": "Medium", "inter": "Medium", "sarasa": "SemiBold", "hcfg": "SemiBold"},
    "Bold": {"shs": "Bold", "inter": "Bold", "sarasa": "Bold", "hcfg": "Bold"},
    "Heavy": {"shs": "Heavy", "inter": "Black", "sarasa": "Bold", "hcfg": "Bold"},
}

DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
DIGITS_TF = [f"{name}.tf" for name in DIGITS]
PROPDIGITS_CODEPOINTS = set(range(0x30, 0x3A)) | {0x3A}
WIDTH_FEATURES = {"aalt", "pwid", "fwid", "hwid", "twid", "qwid"}
SOURCE_HAN_FINAL_GSUB_FEATURES = {"locl", "ccmp", "vert", "vrt2", "ljmo", "vjmo", "tjmo", "calt", "hist"}
UPRIGHT_EMPTY_GSUB_FEATURES = {f"cv{i:02d}" for i in range(1, 14)} | {f"ss{i:02d}" for i in range(1, 9)}
ITALIC_EMPTY_GSUB_FEATURES = UPRIGHT_EMPTY_GSUB_FEATURES - {"cv11"}
INTER_GSUB_FEATURES = {
    "aalt",
    "calt",
    "case",
    "ccmp",
    "dlig",
    "dnom",
    "frac",
    "hist",
    "locl",
    "numr",
    "ordn",
    "pnum",
    "salt",
    "sinf",
    "subs",
    "sups",
    "tnum",
    "zero",
    "cv14",
}
FINAL_GSUB_FEATURES = SOURCE_HAN_FINAL_GSUB_FEATURES | INTER_GSUB_FEATURES | UPRIGHT_EMPTY_GSUB_FEATURES | {"pnum", "tnum"}
INTER_GPOS_FEATURES = {"cpsp", "kern", "mark", "mkmk"}
SOURCE_HAN_FORCED_CODEPOINTS = {0x22EF}
REFERENCE_ADVANCE_STOPS = [
    ("ExtraLight", 200),
    ("Light", 300),
    ("Regular", 400),
    ("SemiBold", 600),
    ("Bold", 700),
]
SARASA_VERTICAL_METRICS = {
    "hhea_ascent": 969,
    "hhea_descent": -241,
    "hhea_line_gap": 0,
    "typo_ascent": 968,
    "typo_descent": -241,
    "typo_line_gap": 0,
    "win_ascent": 968,
    "win_descent": 241,
}

# Sarasa make/punct/sanitize-symbols.mjs, in Ui/pwid mode.
SANITIZER_TYPES_PWID = {
    0x00B7: "interpunct",
    0x2018: "ident",
    0x2019: "ident",
    0x201C: "ident",
    0x201D: "ident",
    0x2010: "half",
    0x2025: "ellipsis",
    0x2026: "ellipsis",
    0x2E3A: "stretchDual",
    0x2E3B: "stretchTri",
    0x31B4: "half",
    0x31B5: "half",
    0x31B6: "half",
    0x31B7: "half",
    0x31BB: "half",
}


def prefixed(name: str) -> str:
    return INTER_PREFIX + name


def empty_gsub_features_for_style(italic: bool) -> set[str]:
    return ITALIC_EMPTY_GSUB_FEATURES if italic else UPRIGHT_EMPTY_GSUB_FEATURES


def reference_style_name(weight_name: str, italic: bool) -> str:
    if italic:
        return "Italic" if weight_name == "Regular" else f"{weight_name}Italic"
    return weight_name


def reference_font_path(region: str, weight_name: str, italic: bool) -> Path:
    return region_reference_dir(region, False) / f"{sarasa_region_prefix(region)}-{reference_style_name(weight_name, italic)}.ttf"


def hinted_reference_font_path(region: str, weight_name: str, italic: bool) -> Path:
    return region_reference_dir(region, True) / f"{sarasa_region_prefix(region)}-{reference_style_name(weight_name, italic)}.ttf"


def open_reference_font(region: str, weight_name: str, italic: bool) -> TTFont:
    path = reference_font_path(region, weight_name, italic)
    if not path.exists():
        raise FileNotFoundError(path)
    return TTFont(path)


def is_ideograph(c: int) -> bool:
    return (
        0x2E80 <= c <= 0x2FFF
        or 0x3192 <= c <= 0x319F
        or 0x31C0 <= c <= 0x31EF
        or 0x3400 <= c <= 0x4DBF
        or 0x4E00 <= c <= 0x9FFF
        or 0xF900 <= c <= 0xFA6F
        or 0x20000 <= c <= 0x3FFFF
    )


def is_western(c: int) -> bool:
    return (c < 0x2000 and c != 0x00B7) or (0x2070 <= c <= 0x218F)


def is_korean(c: int) -> bool:
    return (
        0x1100 <= c <= 0x11FF
        or 0xAC00 <= c <= 0xD7AF
        or 0x3130 <= c <= 0x318F
        or 0x3200 <= c <= 0x321E
        or 0xFFA1 <= c <= 0xFFDC
        or 0x3260 <= c <= 0x327F
        or 0xA960 <= c <= 0xA97F
        or 0xD7B0 <= c <= 0xD7FF
    )


def is_enclosed_alphanumerics(c: int) -> bool:
    return 0x20DD <= c <= 0x20DE or 0x2460 <= c <= 0x24FF or 0x2776 <= c <= 0x2788


def is_pua(c: int) -> bool:
    return 0xE000 <= c <= 0xF8FF


def is_fe_misc(c: int) -> bool:
    return (
        0x3003 <= c <= 0x3007
        or 0x3012 <= c <= 0x3013
        or 0x3020 <= c <= 0x33FF
        or 0x1AFF0 <= c <= 0x1B12F
        or 0x1F000 <= c <= 0x1F2FF
    )


def is_locale_dependent_fwid_punct(c: int) -> bool:
    return c in {0xFF01, 0xFF08, 0xFF09, 0xFF0C, 0xFF0E, 0xFF1A, 0xFF1B, 0xFF3B, 0xFF3D, 0xFF5B, 0xFF5D, 0xFF1F}


def is_ws(c: int) -> bool:
    return (
        (
            ((0x2000 <= c <= 0x200F) or (0x20A0 <= c < 0x3000))
            and not (0x2E3A <= c <= 0x2E3B)
        )
        or (0xFF01 <= c <= 0xFF5E and not is_locale_dependent_fwid_punct(c))
    )


def source_han_overrides_inter(c: int) -> bool:
    return (
        c in SOURCE_HAN_FORCED_CODEPOINTS
        or is_ideograph(c)
        or is_korean(c)
        or is_enclosed_alphanumerics(c)
        or is_pua(c)
        or (not is_western(c) and not is_ws(c) and not is_fe_misc(c))
    )


def use_inter_codepoint(c: int) -> bool:
    return not source_han_overrides_inter(c)


def set_name_record(font: TTFont, name_id: int, value: str) -> None:
    name_table = font["name"]
    records = [n for n in name_table.names if n.nameID == name_id]
    if not records:
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)
        records = [n for n in name_table.names if n.nameID == name_id]
    for record in records:
        record.string = value.encode(record.getEncoding())


def set_windows_name_record(font: TTFont, name_id: int, value: str, lang_id: int) -> None:
    font["name"].setName(value, name_id, 3, 1, lang_id)


def set_zh_hans_name_records(font: TTFont, replacements: dict[int, str]) -> None:
    for name_id, value in replacements.items():
        set_windows_name_record(font, name_id, value, 0x0804)


def set_localized_name_records(font: TTFont, region: str, replacements: dict[int, str]) -> None:
    lang_id = int(region_config(region)["local_lang_id"])
    for name_id, value in replacements.items():
        set_windows_name_record(font, name_id, value, lang_id)


def update_vf_names(font: TTFont, region: str, italic: bool) -> None:
    subfamily = "Italic" if italic else "Regular"
    family = vf_family(region)
    family_local = vf_family_local(region)
    ps_family = vf_ps_family(region)
    full = family + (" Italic" if italic else "")
    full_local = family_local + (" Italic" if italic else "")
    ps = ps_family + ("-Italic" if italic else "")
    source_label = source_han_vf_basename(region) or source_han_static_prefix(region)
    if classical_vf_override_basename(region):
        source_label += " + ShangguSansTC-VF"
    version = f"Version {VERSION}; {source_label} + Inter VF; PropDigits"
    replacements = {
        1: family,
        2: subfamily,
        3: ps + f";{VERSION}",
        4: full,
        5: version,
        6: ps,
        16: family,
        17: subfamily,
        25: ps,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)
    set_localized_name_records(
        font,
        region,
        {
            1: family_local,
            2: subfamily,
            3: f"{family_local} {subfamily}",
            4: full_local,
            16: family_local,
            17: subfamily,
        },
    )


def legacy_static_family(region: str, weight_name: str) -> str:
    family = static_family(region)
    if weight_name in {"Regular", "Bold"}:
        return family
    return f"{family} {weight_name}"


def legacy_static_family_local(region: str, weight_name: str) -> str:
    family = static_family_local(region)
    if weight_name in {"Regular", "Bold"}:
        return family
    return f"{family} {weight_name}"


def update_static_names(font: TTFont, region: str, weight_name: str, weight_value: int, italic: bool) -> None:
    family = legacy_static_family(region, weight_name)
    family_local = legacy_static_family_local(region, weight_name)
    typographic_family = static_family(region)
    typographic_family_local = static_family_local(region)
    ps_family = static_ps_family(region)
    if weight_name == "Regular":
        legacy_style = "Italic" if italic else "Regular"
    elif weight_name == "Bold":
        legacy_style = "Bold Italic" if italic else "Bold"
    else:
        legacy_style = "Italic" if italic else "Regular"

    typographic_style = "Italic" if weight_name == "Regular" and italic else weight_name + (" Italic" if italic else "")
    full = typographic_family if weight_name == "Regular" and not italic else f"{typographic_family} {typographic_style}"
    full_local = (
        typographic_family_local
        if weight_name == "Regular" and not italic
        else f"{typographic_family_local} {typographic_style}"
    )
    ps_suffix = "Italic" if weight_name == "Regular" and italic else weight_name + ("-Italic" if italic else "")
    ps = f"{ps_family}-{ps_suffix}"

    replacements = {
        1: family,
        2: legacy_style,
        3: ps + f";{VERSION}",
        4: full,
        5: f"Version {VERSION}; static {source_han_static_prefix(region)} + static Inter; PropDigits",
        6: ps,
        16: typographic_family,
        17: typographic_style,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)
    set_localized_name_records(
        font,
        region,
        {
            1: family_local,
            2: legacy_style,
            3: full_local,
            4: full_local,
            16: typographic_family_local,
            17: typographic_style,
        },
    )
    if 25 in {n.nameID for n in font["name"].names}:
        set_name_record(font, 25, ps)

    os2 = font["OS/2"]
    os2.usWeightClass = weight_value
    os2.fsSelection |= 1 << 7
    os2.fsSelection &= ~((1 << 0) | (1 << 5) | (1 << 6))
    if italic:
        os2.fsSelection |= 1 << 0
        font["head"].macStyle |= 0b10
        font["post"].italicAngle = -9.4
    else:
        font["head"].macStyle &= ~0b10
        font["post"].italicAngle = 0
    if weight_value >= 700:
        os2.fsSelection |= 1 << 5
        font["head"].macStyle |= 0b01
    else:
        font["head"].macStyle &= ~0b01
    if weight_value < 700 and not italic:
        os2.fsSelection |= 1 << 6


def update_style_flags(font: TTFont, italic: bool) -> None:
    os2 = font["OS/2"]
    os2.usWeightClass = 400
    os2.fsSelection |= 1 << 7
    os2.fsSelection &= ~((1 << 0) | (1 << 5) | (1 << 6))
    font["head"].macStyle &= ~0b11
    if italic:
        font["head"].macStyle |= 0b10
        os2.fsSelection |= 1 << 0
        font["post"].italicAngle = -9.4
    else:
        os2.fsSelection |= 1 << 6
        font["post"].italicAngle = 0


def update_os2_sarasa_metadata(font: TTFont) -> None:
    os2 = font["OS/2"]
    os2.version = max(os2.version, 4)
    os2.achVendID = OS2_VENDOR_ID
    os2.ulCodePageRange1 = 2147746207
    os2.ulCodePageRange2 = 0
    apply_sarasa_vertical_metrics(font)
    update_caret_slope(font)


def apply_sarasa_vertical_metrics(font: TTFont) -> None:
    hhea = font["hhea"]
    os2 = font["OS/2"]
    hhea.ascent = SARASA_VERTICAL_METRICS["hhea_ascent"]
    hhea.descent = SARASA_VERTICAL_METRICS["hhea_descent"]
    hhea.lineGap = SARASA_VERTICAL_METRICS["hhea_line_gap"]
    os2.sTypoAscender = SARASA_VERTICAL_METRICS["typo_ascent"]
    os2.sTypoDescender = SARASA_VERTICAL_METRICS["typo_descent"]
    os2.sTypoLineGap = SARASA_VERTICAL_METRICS["typo_line_gap"]
    os2.usWinAscent = SARASA_VERTICAL_METRICS["win_ascent"]
    os2.usWinDescent = SARASA_VERTICAL_METRICS["win_descent"]


def update_caret_slope(font: TTFont) -> None:
    hhea = font["hhea"]
    italic_angle = float(font["post"].italicAngle)
    if italic_angle:
        hhea.caretSlopeRise = 1000
        hhea.caretSlopeRun = otRound(math.tan(math.radians(-italic_angle)) * hhea.caretSlopeRise)
    else:
        hhea.caretSlopeRise = 1
        hhea.caretSlopeRun = 0
    hhea.caretOffset = 0


def sync_sarasa_metadata_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    os2 = font["OS/2"]
    ref_os2 = reference["OS/2"]
    for field in [
        "version",
        "xAvgCharWidth",
        "usWidthClass",
        "fsType",
        "ySubscriptXSize",
        "ySubscriptYSize",
        "ySubscriptXOffset",
        "ySubscriptYOffset",
        "ySuperscriptXSize",
        "ySuperscriptYSize",
        "ySuperscriptXOffset",
        "ySuperscriptYOffset",
        "yStrikeoutSize",
        "yStrikeoutPosition",
        "sFamilyClass",
        "ulUnicodeRange1",
        "ulUnicodeRange2",
        "ulUnicodeRange3",
        "ulUnicodeRange4",
        "achVendID",
        "usFirstCharIndex",
        "usLastCharIndex",
        "ulCodePageRange1",
        "ulCodePageRange2",
        "sxHeight",
        "sCapHeight",
        "usDefaultChar",
        "usBreakChar",
        "usMaxContext",
    ]:
        if hasattr(os2, field) and hasattr(ref_os2, field):
            setattr(os2, field, copy.deepcopy(getattr(ref_os2, field)))
    os2.panose = copy.deepcopy(ref_os2.panose)
    apply_sarasa_vertical_metrics(font)

    head = font["head"]
    ref_head = reference["head"]
    for field in ["fontRevision", "flags", "lowestRecPPEM", "fontDirectionHint", "glyphDataFormat"]:
        if hasattr(head, field) and hasattr(ref_head, field):
            setattr(head, field, copy.deepcopy(getattr(ref_head, field)))

    if "vhea" in font and "vhea" in reference:
        vhea = font["vhea"]
        ref_vhea = reference["vhea"]
        for field in [
            "tableVersion",
            "ascent",
            "descent",
            "lineGap",
            "advanceHeightMax",
            "minTopSideBearing",
            "minBottomSideBearing",
            "yMaxExtent",
            "caretSlopeRise",
            "caretSlopeRun",
            "caretOffset",
            "reserved1",
            "reserved2",
            "reserved3",
            "reserved4",
            "metricDataFormat",
        ]:
            if hasattr(vhea, field) and hasattr(ref_vhea, field):
                setattr(vhea, field, copy.deepcopy(getattr(ref_vhea, field)))
    return {"sarasa_metadata_fields_synced": 1}


def glyph_coordinates_match(font: TTFont, glyph_name: str, reference: TTFont, ref_glyph_name: str) -> bool:
    try:
        coordinates, end_pts, flags = font["glyf"][glyph_name].getCoordinates(font["glyf"])
        ref_coordinates, ref_end_pts, ref_flags = reference["glyf"][ref_glyph_name].getCoordinates(reference["glyf"])
    except Exception:
        return False
    return (
        len(coordinates) == len(ref_coordinates)
        and list(end_pts) == list(ref_end_pts)
        and list(flags) == list(ref_flags)
        and all(tuple(coordinates[i]) == tuple(ref_coordinates[i]) for i in range(len(coordinates)))
    )


def sync_hinting_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    if "glyf" not in font or "glyf" not in reference:
        return {"hint_tables_synced": 0, "hint_glyph_programs_synced": 0, "hint_glyph_programs_skipped": 0}

    tables_synced = 0
    for tag in ("fpgm", "prep", "cvt ", "gasp"):
        if tag in reference:
            font[tag] = copy.deepcopy(reference[tag])
            tables_synced += 1
        elif tag in font:
            del font[tag]

    if "maxp" in font and "maxp" in reference:
        for field in (
            "maxZones",
            "maxTwilightPoints",
            "maxStorage",
            "maxFunctionDefs",
            "maxInstructionDefs",
            "maxStackElements",
            "maxSizeOfInstructions",
        ):
            if hasattr(font["maxp"], field) and hasattr(reference["maxp"], field):
                setattr(font["maxp"], field, copy.deepcopy(getattr(reference["maxp"], field)))

    synced = 0
    skipped = 0
    empty_program = Program()
    empty_program.fromBytecode([])
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in reference["glyf"].glyphs:
            skipped += 1
            continue
        if not glyph_coordinates_match(font, glyph_name, reference, glyph_name):
            skipped += 1
            continue
        ref_program = getattr(reference["glyf"][glyph_name], "program", empty_program)
        font["glyf"][glyph_name].program = copy.deepcopy(ref_program)
        synced += 1
    return {
        "hint_tables_synced": tables_synced,
        "hint_glyph_programs_synced": synced,
        "hint_glyph_programs_skipped": skipped,
    }


def count_simple_glyph_overlap_flags(font: TTFont) -> int:
    if "glyf" not in font:
        return 0
    count = 0
    for glyph_name in font.getGlyphOrder():
        glyph = font["glyf"][glyph_name]
        if getattr(glyph, "numberOfContours", 0) > 0 and hasattr(glyph, "flags"):
            count += sum(1 for flag in glyph.flags if flag & 0x40)
    return count


def force_recompile_glyf(font: TTFont) -> dict[str, int]:
    if "glyf" not in font:
        return {"glyf_glyphs_forced_to_recompile": 0}
    glyf = font["glyf"]
    forced = 0
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in glyf.glyphs:
            continue
        glyph = glyf[glyph_name]
        if hasattr(glyph, "data"):
            glyph.expand(glyf)
        if hasattr(glyph, "data"):
            del glyph.data
            forced += 1
    return {"glyf_glyphs_forced_to_recompile": forced}


def glyph_point_structure(font: TTFont, glyph_name: str) -> tuple[Any, ...] | None:
    if "glyf" not in font or glyph_name not in font["glyf"].glyphs:
        return None
    try:
        coords, end_pts, _flags = font["glyf"][glyph_name].getCoordinates(font["glyf"])
    except Exception:
        return None
    return (tuple((int(x), int(y)) for x, y in coords), tuple(int(x) for x in end_pts))


def sync_static_glyf_from_reference(
    font: TTFont,
    reference: TTFont,
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "glyf" not in font or "glyf" not in reference:
        return {
            "reference_glyf_flags_synced": 0,
            "reference_glyf_bboxes_synced": 0,
            "reference_component_aliases_removed": 0,
            "reference_component_names_synced": 0,
        }

    current_cmap = font.getBestCmap()
    reference_cmap = reference.getBestCmap()
    flags_synced = 0
    bboxes_synced = 0
    component_names_synced = 0
    stale_aliases: set[str] = set()
    visited: set[tuple[str, str]] = set()

    def sync_pair(glyph_name: str, reference_glyph_name: str) -> None:
        nonlocal flags_synced, bboxes_synced, component_names_synced
        if (glyph_name, reference_glyph_name) in visited:
            return
        visited.add((glyph_name, reference_glyph_name))
        if glyph_name not in font["glyf"].glyphs or reference_glyph_name not in reference["glyf"].glyphs:
            return
        glyph = font["glyf"][glyph_name]
        reference_glyph = reference["glyf"][reference_glyph_name]

        if glyph_point_structure(font, glyph_name) == glyph_point_structure(reference, reference_glyph_name):
            for field in ("xMin", "yMin", "xMax", "yMax"):
                if hasattr(reference_glyph, field):
                    setattr(glyph, field, copy.deepcopy(getattr(reference_glyph, field)))
            bboxes_synced += 1

            if (
                getattr(glyph, "numberOfContours", 0) > 0
                and getattr(reference_glyph, "numberOfContours", 0) > 0
                and hasattr(glyph, "flags")
                and hasattr(reference_glyph, "flags")
                and len(glyph.flags) == len(reference_glyph.flags)
            ):
                if list(glyph.flags) != list(reference_glyph.flags):
                    glyph.flags[:] = list(reference_glyph.flags)
                    flags_synced += 1

        if glyph.isComposite() and reference_glyph.isComposite():
            components = getattr(glyph, "components", [])
            reference_components = getattr(reference_glyph, "components", [])
            if len(components) != len(reference_components):
                return
            for component, reference_component in zip(components, reference_components):
                reference_component_name = reference_component.glyphName
                if reference_component_name in font["glyf"].glyphs:
                    old_component_name = component.glyphName
                    if old_component_name != reference_component_name:
                        component.glyphName = reference_component_name
                        component_names_synced += 1
                        if old_component_name not in reference["glyf"].glyphs:
                            stale_aliases.add(old_component_name)
                    sync_pair(reference_component_name, reference_component_name)
                else:
                    sync_pair(component.glyphName, reference_component_name)

    for codepoint, reference_glyph_name in reference_cmap.items():
        if codepoint in skip_codepoints or codepoint not in current_cmap:
            continue
        sync_pair(current_cmap[codepoint], reference_glyph_name)

    aliases_removed = remove_glyphs(font, stale_aliases)
    return {
        "reference_glyf_flags_synced": flags_synced,
        "reference_glyf_bboxes_synced": bboxes_synced,
        "reference_component_aliases_removed": aliases_removed,
        "reference_component_names_synced": component_names_synced,
    }


def rebuild_gdef_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    reference_gdef = reference.get("GDEF")
    if not reference_gdef or not getattr(reference_gdef.table, "GlyphClassDef", None):
        return {"gdef_classdefs": 0, "gdef_mark_sets": 0}
    glyph_set = set(font.getGlyphOrder())
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    reference_classes = reference_gdef.table.GlyphClassDef.classDefs
    class_defs: dict[str, int] = {}
    for codepoint, glyph_name in current_cmap.items():
        ref_glyph = reference_cmap.get(codepoint)
        glyph_class = reference_classes.get(ref_glyph) if ref_glyph else None
        if glyph_class is not None:
            class_defs[glyph_name] = glyph_class
    for glyph_name in font.getGlyphOrder():
        if glyph_name in class_defs or glyph_name == ".notdef":
            continue
        if glyph_name in reference_classes:
            class_defs[glyph_name] = reference_classes[glyph_name]
    class_defs = {glyph_name: value for glyph_name, value in class_defs.items() if glyph_name in glyph_set}

    gdef = copy.deepcopy(reference_gdef)
    gdef.table.GlyphClassDef.classDefs = class_defs
    mark_sets = getattr(gdef.table, "MarkGlyphSetsDef", None)
    if mark_sets and getattr(mark_sets, "Coverage", None):
        for coverage_table in mark_sets.Coverage:
            coverage_table.glyphs = [glyph_name for glyph_name in coverage_table.glyphs if glyph_name in glyph_set]
        mark_sets.MarkSetCount = len(mark_sets.Coverage)
    font["GDEF"] = gdef
    return {"gdef_classdefs": len(class_defs), "gdef_mark_sets": mark_sets.MarkSetCount if mark_sets else 0}


def rebuild_vorg_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    if "VORG" not in reference:
        return {"vorg_records": 0}
    reference_vorg = reference["VORG"]
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    reference_vertical = get_single_substitution_mappings(reference, {"vert", "vrt2"})
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    records: dict[str, int] = {}
    for codepoint, glyph_name in current_cmap.items():
        ref_glyph = reference_cmap.get(codepoint)
        if ref_glyph in reference_vorg.VOriginRecords:
            records[glyph_name] = reference_vorg.VOriginRecords[ref_glyph]
        ref_vertical_glyph = reference_vertical.get(ref_glyph) if ref_glyph else None
        current_vertical_glyph = current_vertical.get(glyph_name)
        if (
            ref_vertical_glyph in reference_vorg.VOriginRecords
            and current_vertical_glyph in font.getGlyphOrder()
        ):
            records[current_vertical_glyph] = reference_vorg.VOriginRecords[ref_vertical_glyph]
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in records and glyph_name in reference_vorg.VOriginRecords:
            records[glyph_name] = reference_vorg.VOriginRecords[glyph_name]
    vorg = newTable("VORG")
    vorg.majorVersion = 1
    vorg.minorVersion = 0
    vorg.defaultVertOriginY = reference_vorg.defaultVertOriginY
    vorg.VOriginRecords = records
    font["VORG"] = vorg
    return {"vorg_records": len(records)}


def align_reference_vmtx(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    if "vmtx" not in font or "vmtx" not in reference:
        return {"reference_vmtx_aligned": 0, "reference_vertical_vmtx_aligned": 0}
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    reference_vertical = get_single_substitution_mappings(reference, {"vert", "vrt2"})
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    touched = 0
    vertical_touched = 0
    for codepoint in sorted(set(current_cmap) & set(reference_cmap)):
        if codepoint in skip_codepoints:
            continue
        glyph_name = current_cmap[codepoint]
        ref_glyph = reference_cmap[codepoint]
        if ref_glyph in reference["vmtx"].metrics:
            ref_metrics = copy.deepcopy(reference["vmtx"].metrics[ref_glyph])
            if font["vmtx"].metrics.get(glyph_name) != ref_metrics:
                font["vmtx"].metrics[glyph_name] = ref_metrics
                touched += 1
        ref_vertical_glyph = reference_vertical.get(ref_glyph)
        current_vertical_glyph = current_vertical.get(glyph_name)
        if (
            ref_vertical_glyph in reference["vmtx"].metrics
            and current_vertical_glyph in font["vmtx"].metrics
        ):
            ref_metrics = copy.deepcopy(reference["vmtx"].metrics[ref_vertical_glyph])
            if font["vmtx"].metrics.get(current_vertical_glyph) != ref_metrics:
                font["vmtx"].metrics[current_vertical_glyph] = ref_metrics
                vertical_touched += 1
    return {"reference_vmtx_aligned": touched, "reference_vertical_vmtx_aligned": vertical_touched}


def reference_vmtx_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, tuple[int, int]], ...]]:
    profiles: dict[int, tuple[tuple[int, tuple[int, int]], ...]] = {}
    for codepoint in codepoints:
        metrics = []
        for weight_value, reference in sorted(reference_fonts.items()):
            if "vmtx" not in reference:
                continue
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            if glyph_name and glyph_name in reference["vmtx"].metrics:
                metrics.append((weight_value, tuple(reference["vmtx"].metrics[glyph_name])))
        if metrics:
            profiles[codepoint] = tuple(metrics)
    return profiles


def reference_vertical_vmtx_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, tuple[int, int]], ...]]:
    vertical_maps = {
        weight_value: get_single_substitution_mappings(reference, {"vert", "vrt2"})
        for weight_value, reference in reference_fonts.items()
    }
    profiles: dict[int, tuple[tuple[int, tuple[int, int]], ...]] = {}
    for codepoint in codepoints:
        metrics = []
        for weight_value, reference in sorted(reference_fonts.items()):
            if "vmtx" not in reference:
                continue
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            vertical_glyph = vertical_maps[weight_value].get(glyph_name) if glyph_name else None
            if vertical_glyph and vertical_glyph in reference["vmtx"].metrics:
                metrics.append((weight_value, tuple(reference["vmtx"].metrics[vertical_glyph])))
        if metrics:
            profiles[codepoint] = tuple(metrics)
    return profiles


def split_reference_vmtx_profiles(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    profiles = reference_vmtx_profiles(reference_fonts, set(font.getBestCmap()) - skip_codepoints)
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        relevant = {codepoint for codepoint in codepoints if codepoint in profiles}
        if len(relevant) <= 1:
            continue
        by_profile: dict[tuple[tuple[int, tuple[int, int]], ...], set[int]] = {}
        for codepoint in relevant:
            by_profile.setdefault(profiles[codepoint], set()).add(codepoint)
        if len(by_profile) <= 1:
            continue
        keep_profile, _keep_codepoints = max(by_profile.items(), key=lambda item: (len(item[1]), -min(item[1])))
        for profile, cps in by_profile.items():
            if profile == keep_profile:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_vmtx_profile_groups_split": split_groups, "reference_vmtx_profile_glyphs_cloned": cloned_glyphs}


def split_reference_vertical_vmtx_aliases(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "GSUB" not in font or "vmtx" not in font or "glyf" not in font:
        return {
            "reference_vertical_vmtx_alias_groups_split": 0,
            "reference_vertical_vmtx_alias_glyphs_cloned": 0,
            "reference_vertical_vmtx_alias_mappings_updated": 0,
        }

    cmap = font.getBestCmap()
    relevant_codepoints = set(cmap) - skip_codepoints
    encoded_profiles = reference_vmtx_profiles(reference_fonts, relevant_codepoints)
    vertical_profiles = reference_vertical_vmtx_profiles(reference_fonts, relevant_codepoints)
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    if not vertical_profiles or not current_vertical:
        return {
            "reference_vertical_vmtx_alias_groups_split": 0,
            "reference_vertical_vmtx_alias_glyphs_cloned": 0,
            "reference_vertical_vmtx_alias_mappings_updated": 0,
        }

    reference_400 = reference_fonts.get(400) or next(iter(reference_fonts.values()))
    reference_400_cmap = reference_400.getBestCmap()
    reference_400_vertical = get_single_substitution_mappings(reference_400, {"vert", "vrt2"})
    encoded_profiles_by_glyph: dict[str, set[tuple[tuple[int, tuple[int, int]], ...]]] = {}
    for codepoint, glyph_name in cmap.items():
        profile = encoded_profiles.get(codepoint)
        if profile:
            encoded_profiles_by_glyph.setdefault(glyph_name, set()).add(profile)

    roles_by_target: dict[str, list[tuple[int, str, tuple[tuple[int, tuple[int, int]], ...], str | None]]] = {}
    for codepoint in sorted(relevant_codepoints):
        source_name = cmap.get(codepoint)
        target_name = current_vertical.get(source_name) if source_name else None
        profile = vertical_profiles.get(codepoint)
        if not source_name or not target_name or not profile or target_name not in font["glyf"].glyphs:
            continue
        ref_source = reference_400_cmap.get(codepoint)
        ref_target = reference_400_vertical.get(ref_source) if ref_source else None
        roles_by_target.setdefault(target_name, []).append((codepoint, source_name, profile, ref_target))

    replacements: dict[tuple[str, str], str] = {}
    split_groups = 0
    cloned_glyphs = 0
    for target_name, roles in roles_by_target.items():
        encoded_for_target = encoded_profiles_by_glyph.get(target_name, set())
        profiles_to_clone: set[tuple[tuple[int, tuple[int, int]], ...]] = set()
        if encoded_for_target:
            profiles_to_clone = {profile for _cp, _source, profile, _ref_target in roles if profile not in encoded_for_target}
        else:
            roles_by_profile: dict[tuple[tuple[int, tuple[int, int]], ...], list[tuple[int, str, str | None]]] = {}
            for codepoint, source_name, profile, ref_target in roles:
                roles_by_profile.setdefault(profile, []).append((codepoint, source_name, ref_target))
            if len(roles_by_profile) > 1:
                keep_profile = max(
                    roles_by_profile.items(),
                    key=lambda item: (len(item[1]), -min(codepoint for codepoint, _source, _ref_target in item[1])),
                )[0]
                profiles_to_clone = {profile for profile in roles_by_profile if profile != keep_profile}

        clones_by_profile: dict[tuple[tuple[int, tuple[int, int]], ...], str] = {}
        for codepoint, source_name, profile, ref_target in roles:
            if profile not in profiles_to_clone:
                continue
            if profile not in clones_by_profile:
                preferred_name = ref_target or f"{target_name}.v{codepoint:04X}"
                clone_name = clone_glyph(font, target_name, preferred_name)
                if not clone_name:
                    continue
                clones_by_profile[profile] = clone_name
                cloned_glyphs += 1
            replacements[(source_name, target_name)] = clones_by_profile[profile]
        if clones_by_profile:
            split_groups += 1

    updated = update_single_substitution_mappings(font, {"vert", "vrt2"}, replacements)
    return {
        "reference_vertical_vmtx_alias_groups_split": split_groups,
        "reference_vertical_vmtx_alias_glyphs_cloned": cloned_glyphs,
        "reference_vertical_vmtx_alias_mappings_updated": updated,
    }


def glyph_vmtx_at_weight(font: TTFont, weight_value: int) -> dict[str, tuple[int, int]]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        if "vmtx" not in instance:
            return {}
        return {glyph_name: tuple(metrics) for glyph_name, metrics in instance["vmtx"].metrics.items()}
    finally:
        instance.close()


def add_vmtx_tuple_variation(
    font: TTFont,
    glyph_name: str,
    support: tuple[float, float, float],
    advance_delta: int,
    tsb_delta: int,
) -> None:
    if "gvar" not in font or (not advance_delta and not tsb_delta):
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    top_delta = otRound(tsb_delta)
    bottom_delta = otRound(tsb_delta - advance_delta)
    coordinates[-4:] = [(0, 0), (0, 0), (0, top_delta), (0, bottom_delta)]
    font["gvar"].variations.setdefault(glyph_name, []).append(TupleVariation({"wght": support}, coordinates))


def align_reference_vmtx_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font or "vmtx" not in font:
        return {"reference_vmtx_variations_added": 0, "reference_vmtx_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    reference_vertical = get_single_substitution_mappings(reference_fonts[400], {"vert", "vrt2"})
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        if "vmtx" not in reference:
            continue
        reference_cmap = reference.getBestCmap()
        current_cmap = font.getBestCmap()
        current_metrics = glyph_vmtx_at_weight(font, weight_value)
        glyph_deltas: dict[str, tuple[int, int]] = {}

        def queue_delta(glyph_name: str, target_metrics: tuple[int, int]) -> None:
            nonlocal corrections
            current = current_metrics.get(glyph_name)
            if current is None:
                return
            advance_delta = target_metrics[0] - current[0]
            tsb_delta = target_metrics[1] - current[1]
            if not advance_delta and not tsb_delta:
                return
            glyph_deltas[glyph_name] = (advance_delta, tsb_delta)
            corrections += 1

        for codepoint in sorted(set(current_cmap) & set(reference_cmap)):
            if codepoint in skip_codepoints:
                continue
            glyph_name = current_cmap[codepoint]
            ref_glyph = reference_cmap[codepoint]
            if ref_glyph in reference["vmtx"].metrics:
                queue_delta(glyph_name, tuple(reference["vmtx"].metrics[ref_glyph]))

            ref_vertical_glyph = reference_vertical.get(ref_glyph)
            current_vertical_glyph = current_vertical.get(glyph_name)
            if ref_vertical_glyph in reference["vmtx"].metrics and current_vertical_glyph in font["vmtx"].metrics:
                queue_delta(current_vertical_glyph, tuple(reference["vmtx"].metrics[ref_vertical_glyph]))

        support = supports[weight_value]
        for glyph_name, (advance_delta, tsb_delta) in glyph_deltas.items():
            add_vmtx_tuple_variation(font, glyph_name, support, advance_delta, tsb_delta)
            variations_added += 1
    return {
        "reference_vmtx_variations_added": variations_added,
        "reference_vmtx_variation_corrections": corrections,
    }


def drop_generated_extra_tables(font: TTFont, keep_stat: bool) -> dict[str, int]:
    dropped = 0
    for tag in ("BASE",):
        if tag in font:
            del font[tag]
            dropped += 1
    if not keep_stat and "STAT" in font:
        del font["STAT"]
        dropped += 1
    return {"extra_tables_dropped": dropped}


def rebuild_stat(font: TTFont, italic: bool) -> None:
    from fontTools.otlLib.builder import buildStatTable

    weight_values = [
        {
            "nominalValue": stop["value"],
            "rangeMinValue": stop["range_min"],
            "rangeMaxValue": stop["range_max"],
            "name": stop["name"],
            "flags": stop.get("flags", 0),
        }
        for stop in SOURCE_HAN_WEIGHT_STOPS
    ]
    axes = [
        {"tag": "wght", "name": "Weight", "values": weight_values},
        {
            "tag": "ital",
            "name": "Italic",
            "values": [
                {
                    "value": 1 if italic else 0,
                    "name": "Italic" if italic else "Roman",
                    **({"linkedValue": 1} if not italic else {}),
                    "flags": 0x2 if not italic else 0,
                }
            ],
        },
    ]
    buildStatTable(font, axes)


def rebuild_static_stat(font: TTFont, weight_name: str, weight_value: int, italic: bool) -> None:
    from fontTools.otlLib.builder import buildStatTable

    stop = next((item for item in SOURCE_HAN_WEIGHT_STOPS if item["name"] == weight_name), None)
    axes = [
        {
            "tag": "wght",
            "name": "Weight",
            "values": [
                {
                    "value": weight_value,
                    "name": weight_name,
                    "flags": stop.get("flags", 0) if stop else 0,
                }
            ],
        },
        {
            "tag": "ital",
            "name": "Italic",
            "values": [
                {
                    "value": 1 if italic else 0,
                    "name": "Italic" if italic else "Roman",
                    **({"linkedValue": 1} if not italic else {}),
                    "flags": 0x2 if not italic else 0,
                }
            ],
        },
    ]
    buildStatTable(font, axes)


def update_fvar_instances(font: TTFont, region: str, italic: bool) -> None:
    name_table = font["name"]
    instances = []
    ps_family = vf_ps_family(region)
    for stop in SOURCE_HAN_WEIGHT_STOPS:
        weight_name = stop["name"]
        weight_value = stop["value"]
        instance = NamedInstance()
        instance.coordinates = {"wght": float(weight_value)}
        instance.flags = 0
        if weight_name == "Regular":
            instance.subfamilyNameID = name_table.addName("Italic" if italic else "Regular")
            instance.postscriptNameID = name_table.addName(f"{ps_family}-Italic" if italic else ps_family)
        else:
            instance.subfamilyNameID = name_table.addName(weight_name + (" Italic" if italic else ""))
            instance.postscriptNameID = name_table.addName(
                f"{ps_family}-{weight_name}{'Italic' if italic else ''}"
            )
        instances.append(instance)
    font["fvar"].instances = instances


def reference_unicodes(region: str) -> set[int]:
    font = TTFont(reference_font_path(region, "Regular", False))
    try:
        return set(font.getBestCmap().keys())
    finally:
        font.close()


def source_han_unicodes_like_sarasa(region: str, base: TTFont, inter_unicodes: set[int]) -> set[int]:
    base_unicodes = set(base.getBestCmap().keys())
    unicodes: set[int] = set()
    for codepoint in reference_unicodes(region):
        if codepoint not in base_unicodes:
            continue
        if source_han_overrides_inter(codepoint) or codepoint not in inter_unicodes:
            unicodes.add(codepoint)
    return unicodes


def ensure_gvar_keys(font: TTFont) -> None:
    if "gvar" not in font:
        return
    variations = font["gvar"].variations
    for glyph_name in font.getGlyphOrder():
        if glyph_name not in variations:
            variations[glyph_name] = []


def subset_font(font: TTFont, unicodes: set[int]) -> None:
    ensure_gvar_keys(font)
    options = subset.Options()
    options.layout_features = "*"
    options.name_IDs = "*"
    options.name_legacy = True
    options.name_languages = "*"
    options.notdef_outline = True
    options.recommended_glyphs = True
    options.glyph_names = True
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(unicodes=unicodes)
    subsetter.subset(font)
    ensure_gvar_keys(font)


def subset_to_current_cmap(font: TTFont) -> None:
    subset_font(font, set(font.getBestCmap().keys()))


def get_single_substitution_mapping(font: TTFont, tag: str) -> dict[str, str]:
    if "GSUB" not in font:
        return {}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {}
    feature_records = [r for r in gsub.FeatureList.FeatureRecord if r.FeatureTag == tag]
    mapping: dict[str, str] = {}
    for record in feature_records:
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if hasattr(subtable, "mapping"):
                    mapping.update(subtable.mapping)
    return mapping


def get_single_substitution_mappings(font: TTFont, tags: set[str]) -> dict[str, str]:
    if "GSUB" not in font:
        return {}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {}
    mapping: dict[str, str] = {}
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in tags:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if hasattr(subtable, "mapping"):
                    mapping.update(subtable.mapping)
    return mapping


def update_single_substitution_mappings(font: TTFont, tags: set[str], replacements: dict[tuple[str, str], str]) -> int:
    if "GSUB" not in font or not replacements:
        return 0
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return 0
    updated = 0
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in tags:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if not hasattr(subtable, "mapping"):
                    continue
                for source_name, target_name in list(subtable.mapping.items()):
                    new_target = replacements.get((source_name, target_name))
                    if new_target and new_target != target_name:
                        subtable.mapping[source_name] = new_target
                        updated += 1
    return updated


def remove_vertical_long_dash_ligature_mappings(font: TTFont) -> dict[str, int]:
    if "GSUB" not in font or "hmtx" not in font or "vmtx" not in font:
        return {"vertical_long_dash_ligature_mappings_removed": 0}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {"vertical_long_dash_ligature_mappings_removed": 0}
    removed = 0
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in {"vert", "vrt2"}:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if not hasattr(subtable, "mapping"):
                    continue
                for source_name, target_name in list(subtable.mapping.items()):
                    source_width = font["hmtx"].metrics.get(source_name, (0, 0))[0]
                    target_height = font["vmtx"].metrics.get(target_name, (0, 0))[0]
                    if source_width > font["head"].unitsPerEm and target_height > font["head"].unitsPerEm:
                        del subtable.mapping[source_name]
                        removed += 1
    return {"vertical_long_dash_ligature_mappings_removed": removed}


def single_substitution_subtables(lookup: ot.Lookup) -> list[Any]:
    if lookup.LookupType == 1:
        return list(lookup.SubTable)
    if lookup.LookupType == 7:
        subtables = []
        for subtable in lookup.SubTable:
            if getattr(subtable, "ExtensionLookupType", None) == 1 and getattr(subtable, "ExtSubTable", None):
                subtables.append(subtable.ExtSubTable)
        return subtables
    return []


def glyph_to_unicodes(font: TTFont) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    for codepoint, glyph_name in font.getBestCmap().items():
        result.setdefault(glyph_name, set()).add(codepoint)
    return result


def reference_locl_source_unicodes(region: str) -> set[int]:
    font = TTFont(reference_font_path(region, "Regular", False))
    try:
        reverse = glyph_to_unicodes(font)
        unicodes: set[int] = set()
        for source_name in get_single_substitution_mapping(font, "locl"):
            unicodes.update(reverse.get(source_name, set()))
        return unicodes
    finally:
        font.close()


def prune_locl_like_reference(font: TTFont, region: str) -> dict[str, int]:
    allowed_unicodes = reference_locl_source_unicodes(region)
    reverse = glyph_to_unicodes(font)
    before = 0
    after = 0
    emptied_lookups = 0

    if "GSUB" not in font:
        return {
            "reference_locl_codepoints": len(allowed_unicodes),
            "locl_mappings_before_prune": 0,
            "locl_mappings_after_prune": 0,
            "locl_lookups_emptied": 0,
        }

    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {
            "reference_locl_codepoints": len(allowed_unicodes),
            "locl_mappings_before_prune": 0,
            "locl_mappings_after_prune": 0,
            "locl_lookups_emptied": 0,
        }

    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag != "locl":
            continue
        kept_indices = []
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            lookup_has_mappings = False
            single_subtables = single_substitution_subtables(lookup)
            if single_subtables:
                for subtable in single_subtables:
                    before += len(subtable.mapping)
                    subtable.mapping = {
                        source: target
                        for source, target in subtable.mapping.items()
                        if reverse.get(source, set()) & allowed_unicodes
                    }
                    after += len(subtable.mapping)
                    if subtable.mapping:
                        lookup_has_mappings = True
            elif lookup.LookupType != 7:
                lookup_has_mappings = True
            if lookup_has_mappings:
                kept_indices.append(lookup_index)
            else:
                emptied_lookups += 1
        record.Feature.LookupListIndex = kept_indices
        record.Feature.LookupCount = len(kept_indices)

    return {
        "reference_locl_codepoints": len(allowed_unicodes),
        "locl_mappings_before_prune": before,
        "locl_mappings_after_prune": after,
        "locl_lookups_emptied": emptied_lookups,
    }


def copy_glyph_data(font: TTFont, source_name: str, target_name: str) -> None:
    if source_name == target_name or source_name not in font["glyf"].glyphs or target_name not in font["glyf"].glyphs:
        return
    font["glyf"].glyphs[target_name] = copy.deepcopy(font["glyf"][source_name])
    if source_name in font["hmtx"].metrics:
        font["hmtx"].metrics[target_name] = copy.deepcopy(font["hmtx"].metrics[source_name])
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        font["vmtx"].metrics[target_name] = copy.deepcopy(font["vmtx"].metrics[source_name])
    if "gvar" in font:
        font["gvar"].variations[target_name] = copy.deepcopy(font["gvar"].variations.get(source_name, []))


def clone_glyph(font: TTFont, source_name: str, preferred_name: str) -> str | None:
    if source_name not in font["glyf"].glyphs or source_name not in font["hmtx"].metrics:
        return None
    existing = set(font.getGlyphOrder()) | set(font["glyf"].glyphs)
    base_name = preferred_name if preferred_name and preferred_name not in {".notdef", source_name} else f"{source_name}.clone"
    new_name = base_name
    suffix = 1
    while new_name in existing:
        suffix += 1
        new_name = f"{base_name}.{suffix}"
    font["glyf"].glyphs[new_name] = copy.deepcopy(font["glyf"][source_name])
    font["hmtx"].metrics[new_name] = copy.deepcopy(font["hmtx"].metrics[source_name])
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        font["vmtx"].metrics[new_name] = copy.deepcopy(font["vmtx"].metrics[source_name])
    if "gvar" in font:
        font["gvar"].variations[new_name] = copy.deepcopy(font["gvar"].variations.get(source_name, []))
    if "VORG" in font and source_name in font["VORG"].VOriginRecords:
        font["VORG"].VOriginRecords[new_name] = copy.deepcopy(font["VORG"].VOriginRecords[source_name])
    order = font.getGlyphOrder()
    order.append(new_name)
    font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(order)
    return new_name


def copy_external_glyph_data(target: TTFont, target_name: str, source: TTFont, source_name: str) -> None:
    if source_name not in source["glyf"].glyphs or target_name not in target["glyf"].glyphs:
        return
    target["glyf"].glyphs[target_name] = copy.deepcopy(source["glyf"][source_name])
    if source_name in source["hmtx"].metrics:
        target["hmtx"].metrics[target_name] = copy.deepcopy(source["hmtx"].metrics[source_name])
    if "vmtx" in target and "vmtx" in source and source_name in source["vmtx"].metrics:
        target["vmtx"].metrics[target_name] = copy.deepcopy(source["vmtx"].metrics[source_name])
    if "gvar" in target:
        target["gvar"].variations[target_name] = (
            copy.deepcopy(source["gvar"].variations.get(source_name, [])) if "gvar" in source else []
        )
    if "VORG" in target and "VORG" in source:
        if source_name in source["VORG"].VOriginRecords:
            target["VORG"].VOriginRecords[target_name] = copy.deepcopy(source["VORG"].VOriginRecords[source_name])
        else:
            target["VORG"].VOriginRecords.pop(target_name, None)


def apply_classical_vf_override(base: TTFont, override: TTFont, region: str) -> dict[str, int]:
    if not region_config(region)["classical"]:
        return {"classical_vf_override_codepoints": 0, "classical_vf_override_glyphs": 0}
    base_cmap = base.getBestCmap() or {}
    override_cmap = override.getBestCmap() or {}
    reference_cps = reference_unicodes(region)
    replaced_glyphs: set[str] = set()
    replaced_codepoints = 0
    for codepoint in sorted(reference_cps & set(base_cmap) & set(override_cmap)):
        if not is_ideograph(codepoint):
            continue
        target_name = base_cmap[codepoint]
        source_name = override_cmap[codepoint]
        copy_external_glyph_data(base, target_name, override, source_name)
        replaced_glyphs.add(target_name)
        replaced_codepoints += 1
    return {
        "classical_vf_override_codepoints": replaced_codepoints,
        "classical_vf_override_glyphs": len(replaced_glyphs),
    }


def clone_cmap_glyph_for_codepoint(font: TTFont, codepoint: int) -> str | None:
    cmap = font.getBestCmap()
    glyph_name = cmap.get(codepoint)
    if not glyph_name:
        return None
    if sum(1 for glyph in cmap.values() if glyph == glyph_name) <= 1:
        return glyph_name

    order = font.getGlyphOrder()
    new_name = f"{glyph_name}.u{codepoint:04X}"
    suffix = 1
    while new_name in font.getGlyphSet():
        suffix += 1
        new_name = f"{glyph_name}.u{codepoint:04X}.{suffix}"

    new_name = clone_glyph(font, glyph_name, new_name)
    if not new_name:
        return None
    for cmap_table in font["cmap"].tables:
        if cmap_table.isUnicode() and cmap_table.cmap.get(codepoint) == glyph_name:
            cmap_table.cmap[codepoint] = new_name
    return new_name


def clone_cmap_glyph_for_codepoints(font: TTFont, codepoints: set[int]) -> str | None:
    if not codepoints:
        return None
    cmap = font.getBestCmap()
    first = min(codepoints)
    old_name = cmap.get(first)
    if not old_name:
        return None
    new_name = clone_cmap_glyph_for_codepoint(font, first)
    if not new_name or new_name == old_name:
        return new_name
    for cmap_table in font["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint in codepoints:
            if cmap_table.cmap.get(codepoint) == old_name:
                cmap_table.cmap[codepoint] = new_name
    return new_name


def split_reference_cmap_aliases(font: TTFont, reference: TTFont) -> dict[str, int]:
    reference_cmap = reference.getBestCmap()
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        if len(codepoints) <= 1:
            continue
        ref_groups: dict[str, set[int]] = {}
        for codepoint in codepoints:
            ref_glyph = reference_cmap.get(codepoint)
            if ref_glyph:
                ref_groups.setdefault(ref_glyph, set()).add(codepoint)
        if len(ref_groups) <= 1:
            continue

        current_width = font["hmtx"].metrics.get(glyph_name, (None, None))[0]

        def group_score(item: tuple[str, set[int]]) -> tuple[int, int, int]:
            _ref_glyph, cps = item
            widths = {
                reference["hmtx"].metrics[reference_cmap[cp]][0]
                for cp in cps
                if cp in reference_cmap and reference_cmap[cp] in reference["hmtx"].metrics
            }
            return (1 if current_width in widths else 0, len(cps), -min(cps))

        keep_ref_glyph, _keep_codepoints = max(ref_groups.items(), key=group_score)
        for ref_glyph, cps in ref_groups.items():
            if ref_glyph == keep_ref_glyph:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_alias_groups_split": split_groups, "reference_alias_glyphs_cloned": cloned_glyphs}


def align_reference_cmap_alias_mappings(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    reference_groups: dict[str, set[int]] = {}
    for codepoint, glyph_name in reference.getBestCmap().items():
        if codepoint not in skip_codepoints:
            reference_groups.setdefault(glyph_name, set()).add(codepoint)

    remapped = 0
    current_cmap = font.getBestCmap()
    for ref_glyph, codepoints in reference_groups.items():
        shared = sorted(cp for cp in codepoints if cp in current_cmap)
        if len(shared) <= 1:
            continue
        canonical_cp = next((cp for cp in shared if current_cmap[cp] == ref_glyph), shared[0])
        canonical_glyph = current_cmap[canonical_cp]
        for codepoint in shared:
            old_glyph = current_cmap.get(codepoint)
            if old_glyph == canonical_glyph:
                continue
            for cmap_table in font["cmap"].tables:
                if cmap_table.isUnicode() and cmap_table.cmap.get(codepoint) == old_glyph:
                    cmap_table.cmap[codepoint] = canonical_glyph
                    remapped += 1
    return {"reference_cmap_alias_mappings_aligned": remapped}


def align_reference_advances(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    reference_cmap = reference.getBestCmap()
    touched = 0
    cloned = 0
    for codepoint in sorted(set(font.getBestCmap()) & set(reference_cmap)):
        if codepoint in skip_codepoints:
            continue
        cmap = font.getBestCmap()
        glyph_name = cmap.get(codepoint)
        ref_glyph = reference_cmap.get(codepoint)
        if not glyph_name or not ref_glyph:
            continue
        ref_width = reference["hmtx"].metrics[ref_glyph][0]
        current_width = font["hmtx"].metrics.get(glyph_name, (ref_width, 0))[0]
        if current_width == ref_width:
            continue

        shared_codepoints = glyph_to_unicodes(font).get(glyph_name, set())
        if len(shared_codepoints) > 1:
            shared_widths = {
                reference["hmtx"].metrics[reference_cmap[cp]][0]
                for cp in shared_codepoints
                if cp not in skip_codepoints and cp in reference_cmap and reference_cmap[cp] in reference["hmtx"].metrics
            }
            if (shared_codepoints & skip_codepoints) or any(width != ref_width for width in shared_widths):
                new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
                if new_name and new_name != glyph_name:
                    glyph_name = new_name
                    cloned += 1
        set_advance_width(font, glyph_name, ref_width)
        freeze_advance_variation(font, glyph_name)
        touched += 1
    return {"reference_advances_aligned": touched, "reference_advance_glyphs_cloned": cloned}


def weight_axis(font: TTFont) -> Any:
    return next(axis for axis in font["fvar"].axes if axis.axisTag == "wght")


def normalize_axis_value(value: float, min_value: float, default_value: float, max_value: float) -> float:
    if value == default_value:
        return 0.0
    if value < default_value:
        return (value - default_value) / (default_value - min_value)
    return (value - default_value) / (max_value - default_value)


def denormalize_axis_value(normalized: float, min_value: float, default_value: float, max_value: float) -> float:
    if normalized == 0.0:
        return default_value
    if normalized < 0.0:
        return default_value + normalized * (default_value - min_value)
    return default_value + normalized * (max_value - default_value)


def normalized_wght(font: TTFont, value: int) -> float:
    axis = weight_axis(font)
    normalized = normalize_axis_value(value, axis.minValue, axis.defaultValue, axis.maxValue)
    if "avar" in font and "wght" in font["avar"].segments:
        normalized = piecewiseLinearMap(normalized, font["avar"].segments["wght"])
    return normalized


def source_han_public_avar_segment(font: TTFont) -> dict[float, float]:
    source_axis = weight_axis(font)
    source_segment = {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    if "avar" in font and "wght" in font["avar"].segments:
        source_segment = dict(font["avar"].segments["wght"])
    public_min, public_default, public_max = PUBLIC_AXIS_LIMIT["wght"]
    segment: dict[float, float] = {}
    for public_value, source_value in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT.items():
        public_normalized = normalize_axis_value(public_value, public_min, public_default, public_max)
        source_normalized = normalize_axis_value(
            source_value,
            source_axis.minValue,
            source_axis.defaultValue,
            source_axis.maxValue,
        )
        segment[public_normalized] = piecewiseLinearMap(source_normalized, source_segment)
    return dict(sorted(segment.items()))


def apply_public_weight_axis(font: TTFont) -> dict[str, Any]:
    if "fvar" not in font:
        return {"public_wght_axis_applied": False}
    source_axis = weight_axis(font)
    source_axis_limit = [source_axis.minValue, source_axis.defaultValue, source_axis.maxValue]
    segment = source_han_public_avar_segment(font)
    public_min, public_default, public_max = PUBLIC_AXIS_LIMIT["wght"]
    source_axis.minValue = public_min
    source_axis.defaultValue = public_default
    source_axis.maxValue = public_max
    if "avar" not in font:
        font["avar"] = newTable("avar")
        font["avar"].segments = {}
    font["avar"].segments["wght"] = segment
    return {
        "public_wght_axis_applied": True,
        "public_wght_axis": [public_min, public_default, public_max],
        "source_han_internal_wght_axis": source_axis_limit,
        "source_han_public_to_internal_wght": SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT,
    }


def reference_width_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, int], ...]]:
    profiles: dict[int, tuple[tuple[int, int], ...]] = {}
    for codepoint in codepoints:
        widths = []
        for weight_value, reference in sorted(reference_fonts.items()):
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            if glyph_name and glyph_name in reference["hmtx"].metrics:
                widths.append((weight_value, reference["hmtx"].metrics[glyph_name][0]))
        if widths:
            profiles[codepoint] = tuple(widths)
    return profiles


def reference_lsb_profiles(
    reference_fonts: dict[int, TTFont],
    codepoints: set[int],
) -> dict[int, tuple[tuple[int, int], ...]]:
    profiles: dict[int, tuple[tuple[int, int], ...]] = {}
    for codepoint in codepoints:
        lsbs = []
        for weight_value, reference in sorted(reference_fonts.items()):
            cmap = reference.getBestCmap()
            glyph_name = cmap.get(codepoint)
            if glyph_name and glyph_name in reference["hmtx"].metrics:
                lsbs.append((weight_value, reference["hmtx"].metrics[glyph_name][1]))
        if lsbs:
            profiles[codepoint] = tuple(lsbs)
    return profiles


def split_reference_advance_profiles(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    profiles = reference_width_profiles(reference_fonts, set(font.getBestCmap()) - skip_codepoints)
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        relevant = {codepoint for codepoint in codepoints if codepoint in profiles}
        if len(relevant) <= 1:
            continue
        by_profile: dict[tuple[tuple[int, int], ...], set[int]] = {}
        for codepoint in relevant:
            by_profile.setdefault(profiles[codepoint], set()).add(codepoint)
        if len(by_profile) <= 1:
            continue
        keep_profile, _keep_codepoints = max(by_profile.items(), key=lambda item: (len(item[1]), -min(item[1])))
        for profile, cps in by_profile.items():
            if profile == keep_profile:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_advance_profile_groups_split": split_groups, "reference_advance_profile_glyphs_cloned": cloned_glyphs}


def split_reference_lsb_profiles(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    profiles = reference_lsb_profiles(reference_fonts, set(font.getBestCmap()) - skip_codepoints)
    split_groups = 0
    cloned_glyphs = 0
    for glyph_name, codepoints in list(glyph_to_unicodes(font).items()):
        relevant = {codepoint for codepoint in codepoints if codepoint in profiles}
        if len(relevant) <= 1:
            continue
        by_profile: dict[tuple[tuple[int, int], ...], set[int]] = {}
        for codepoint in relevant:
            by_profile.setdefault(profiles[codepoint], set()).add(codepoint)
        if len(by_profile) <= 1:
            continue
        keep_profile, _keep_codepoints = max(by_profile.items(), key=lambda item: (len(item[1]), -min(item[1])))
        for profile, cps in by_profile.items():
            if profile == keep_profile:
                continue
            if clone_cmap_glyph_for_codepoints(font, cps):
                cloned_glyphs += 1
        split_groups += 1
    return {"reference_lsb_profile_groups_split": split_groups, "reference_lsb_profile_glyphs_cloned": cloned_glyphs}


def gvar_coordinate_count(font: TTFont, glyph_name: str) -> int:
    variations = font["gvar"].variations.get(glyph_name, [])
    if variations:
        return len(variations[0].coordinates)
    return len(font["glyf"][glyph_name].getCoordinates(font["glyf"])[0]) + 4


def simple_glyph_coordinates(font: TTFont, glyph_name: str) -> list[tuple[int, int]] | None:
    if "glyf" not in font or glyph_name not in font["glyf"].glyphs:
        return None
    glyph = font["glyf"][glyph_name]
    if glyph.isComposite():
        return None
    coordinates = glyph.getCoordinates(font["glyf"])[0]
    if not coordinates:
        return None
    return [(x, y) for x, y in coordinates]


def inter_outline_correction_pairs(font: TTFont, inter: TTFont) -> list[tuple[str, str]]:
    glyphs = set(font.getGlyphOrder())
    pairs: list[tuple[str, str]] = []
    for source_name in inter.getGlyphOrder():
        if source_name == ".notdef":
            continue
        prefixed_name = prefixed(source_name)
        if prefixed_name in glyphs:
            pairs.append((source_name, prefixed_name))
        elif source_name in glyphs:
            pairs.append((source_name, source_name))
    return pairs


def add_inter_outline_correction_variations(font: TTFont, inter: TTFont) -> dict[str, int]:
    if "gvar" not in font or "glyf" not in font or "fvar" not in font or "glyf" not in inter or "fvar" not in inter:
        return {"inter_outline_correction_variations_added": 0, "inter_outline_correction_glyphs": 0}
    pairs = inter_outline_correction_pairs(font, inter)
    supports = advance_supports(font, INTER_OUTLINE_CORRECTION_WEIGHTS)
    added = 0
    touched_glyphs: set[str] = set()
    for weight_value in INTER_OUTLINE_CORRECTION_WEIGHTS:
        current = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
        target = instantiateVariableFont(inter, {"wght": weight_value}, inplace=False, optimize=True)
        try:
            support = supports[weight_value]
            for source_name, target_name in pairs:
                current_coordinates = simple_glyph_coordinates(current, target_name)
                target_coordinates = simple_glyph_coordinates(target, source_name)
                if (
                    current_coordinates is None
                    or target_coordinates is None
                    or len(current_coordinates) != len(target_coordinates)
                ):
                    continue
                deltas = [
                    (otRound(target_x - current_x), otRound(target_y - current_y))
                    for (current_x, current_y), (target_x, target_y) in zip(current_coordinates, target_coordinates)
                ]
                if not any(dx or dy for dx, dy in deltas):
                    continue
                coordinates: list[Any] = list(deltas)
                coordinates.extend([(0, 0)] * (gvar_coordinate_count(font, target_name) - len(coordinates)))
                font["gvar"].variations.setdefault(target_name, []).append(TupleVariation({"wght": support}, coordinates))
                added += 1
                touched_glyphs.add(target_name)
        finally:
            current.close()
            target.close()
    return {
        "inter_outline_correction_variations_added": added,
        "inter_outline_correction_glyphs": len(touched_glyphs),
    }


def add_advance_tuple_variation(font: TTFont, glyph_name: str, support: tuple[float, float, float], delta: int) -> None:
    if "gvar" not in font or not delta:
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    coordinates[-4:] = [(0, 0), (otRound(delta), 0), (0, 0), (0, 0)]
    font["gvar"].variations.setdefault(glyph_name, []).append(TupleVariation({"wght": support}, coordinates))


def add_lsb_tuple_variation(font: TTFont, glyph_name: str, support: tuple[float, float, float], delta: int) -> None:
    if "gvar" not in font or not delta:
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    phantom_delta = otRound(-delta)
    coordinates[-4:] = [(phantom_delta, 0), (phantom_delta, 0), (0, 0), (0, 0)]
    font["gvar"].variations.setdefault(glyph_name, []).append(TupleVariation({"wght": support}, coordinates))


def advance_supports(font: TTFont, weights: list[int]) -> dict[int, tuple[float, float, float]]:
    normalized = {weight: normalized_wght(font, weight) for weight in weights}
    supports: dict[int, tuple[float, float, float]] = {}
    negative = sorted((weight, value) for weight, value in normalized.items() if value < 0)
    positive = sorted((weight, value) for weight, value in normalized.items() if value > 0)
    for index, (weight, value) in enumerate(negative):
        start = -1.0 if index == 0 else negative[index - 1][1]
        end = 0.0 if index == len(negative) - 1 else negative[index + 1][1]
        supports[weight] = (start, value, end)
    for index, (weight, value) in enumerate(positive):
        start = 0.0 if index == 0 else positive[index - 1][1]
        end = 1.0 if index == len(positive) - 1 else positive[index + 1][1]
        supports[weight] = (start, value, end)
    return supports


def cmap_widths_at_weight(font: TTFont, weight_value: int) -> dict[int, int]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        cmap = instance.getBestCmap()
        return {
            codepoint: instance["hmtx"].metrics[glyph_name][0]
            for codepoint, glyph_name in cmap.items()
            if glyph_name in instance["hmtx"].metrics
        }
    finally:
        instance.close()


def cmap_hmtx_at_weight(font: TTFont, weight_value: int) -> dict[int, tuple[int, int]]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        cmap = instance.getBestCmap()
        return {
            codepoint: tuple(instance["hmtx"].metrics[glyph_name])
            for codepoint, glyph_name in cmap.items()
            if glyph_name in instance["hmtx"].metrics
        }
    finally:
        instance.close()


def align_reference_hmtx_lsb(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> dict[str, int]:
    reference_cmap = reference.getBestCmap()
    current_cmap = font.getBestCmap()
    touched = 0
    cloned = 0
    targets: dict[str, int] = {}
    for codepoint in sorted(set(current_cmap) & set(reference_cmap)):
        if codepoint in skip_codepoints:
            continue
        glyph_name = current_cmap[codepoint]
        ref_glyph = reference_cmap[codepoint]
        if ref_glyph not in reference["hmtx"].metrics:
            continue
        target_lsb = reference["hmtx"].metrics[ref_glyph][1]
        existing = targets.get(glyph_name)
        if existing is not None and existing != target_lsb:
            new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
            if new_name and new_name != glyph_name:
                glyph_name = new_name
                cloned += 1
        targets[glyph_name] = target_lsb
    for glyph_name, target_lsb in targets.items():
        advance_width, lsb = font["hmtx"].metrics[glyph_name]
        if lsb != target_lsb:
            font["hmtx"].metrics[glyph_name] = (advance_width, target_lsb)
            touched += 1
    return {"reference_lsb_aligned": touched, "reference_lsb_glyphs_cloned": cloned}


def align_reference_advance_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {"reference_advance_variations_added": 0, "reference_advance_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        reference_cmap = reference.getBestCmap()
        current_widths = cmap_widths_at_weight(font, weight_value)
        glyph_deltas: dict[str, int] = {}
        cmap = font.getBestCmap()
        for codepoint in sorted(set(cmap) & set(reference_cmap)):
            if codepoint in skip_codepoints:
                continue
            glyph_name = cmap[codepoint]
            ref_glyph = reference_cmap[codepoint]
            if ref_glyph not in reference["hmtx"].metrics:
                continue
            target_width = reference["hmtx"].metrics[ref_glyph][0]
            current_width = current_widths.get(codepoint)
            if current_width is None:
                continue
            delta = target_width - current_width
            if not delta:
                continue
            existing_delta = glyph_deltas.get(glyph_name)
            if existing_delta is not None and existing_delta != delta:
                new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
                if new_name and new_name != glyph_name:
                    glyph_name = new_name
            glyph_deltas[glyph_name] = delta
            corrections += 1
        support = supports[weight_value]
        for glyph_name, delta in glyph_deltas.items():
            add_advance_tuple_variation(font, glyph_name, support, delta)
            variations_added += 1
    return {
        "reference_advance_variations_added": variations_added,
        "reference_advance_variation_corrections": corrections,
    }


def align_reference_lsb_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {"reference_lsb_variations_added": 0, "reference_lsb_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        reference_cmap = reference.getBestCmap()
        current_metrics = cmap_hmtx_at_weight(font, weight_value)
        glyph_deltas: dict[str, int] = {}
        cmap = font.getBestCmap()
        for codepoint in sorted(set(cmap) & set(reference_cmap)):
            if codepoint in skip_codepoints:
                continue
            glyph_name = cmap[codepoint]
            ref_glyph = reference_cmap[codepoint]
            if ref_glyph not in reference["hmtx"].metrics:
                continue
            current = current_metrics.get(codepoint)
            if current is None:
                continue
            target_lsb = reference["hmtx"].metrics[ref_glyph][1]
            delta = target_lsb - current[1]
            if not delta:
                continue
            existing_delta = glyph_deltas.get(glyph_name)
            if existing_delta is not None and existing_delta != delta:
                new_name = clone_cmap_glyph_for_codepoint(font, codepoint)
                if new_name and new_name != glyph_name:
                    glyph_name = new_name
            glyph_deltas[glyph_name] = delta
            corrections += 1
        support = supports[weight_value]
        for glyph_name, delta in glyph_deltas.items():
            add_lsb_tuple_variation(font, glyph_name, support, delta)
            variations_added += 1
    return {
        "reference_lsb_variations_added": variations_added,
        "reference_lsb_variation_corrections": corrections,
    }


def tnum_digit_targets(font: TTFont) -> dict[int, str]:
    if "hmtx" not in font:
        return {}
    mapping = get_single_substitution_mapping(font, "tnum")
    cmap = font.getBestCmap()
    targets: dict[int, str] = {}
    for codepoint in [*range(0x30, 0x3A), 0x3A]:
        source_glyph = cmap.get(codepoint)
        target_glyph = mapping.get(source_glyph) if source_glyph else None
        if not target_glyph:
            if 0x30 <= codepoint <= 0x39:
                target_glyph = mapping.get(DIGITS[codepoint - 0x30])
            elif codepoint == 0x3A:
                target_glyph = mapping.get("colon")
        if target_glyph in font["hmtx"].metrics:
            targets[codepoint] = target_glyph
    return targets


def reference_digit_hmtx(reference: TTFont) -> dict[int, tuple[int, int]]:
    cmap = reference.getBestCmap()
    metrics: dict[int, tuple[int, int]] = {}
    for codepoint in [*range(0x30, 0x3A), 0x3A]:
        glyph_name = cmap.get(codepoint)
        if glyph_name in reference["hmtx"].metrics:
            metrics[codepoint] = tuple(reference["hmtx"].metrics[glyph_name])
    return metrics


def align_tnum_digit_targets(font: TTFont, reference: TTFont) -> dict[str, int]:
    targets = tnum_digit_targets(font)
    reference_metrics = reference_digit_hmtx(reference)
    touched = 0
    for codepoint, target_glyph in targets.items():
        metrics = reference_metrics.get(codepoint)
        if metrics and tuple(font["hmtx"].metrics[target_glyph]) != metrics:
            font["hmtx"].metrics[target_glyph] = metrics
            touched += 1
    return {"tnum_digit_target_hmtx_aligned": touched}


def tnum_digit_target_hmtx_at_weight(font: TTFont, weight_value: int) -> dict[int, tuple[int, int]]:
    instance = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
    try:
        targets = tnum_digit_targets(instance)
        return {codepoint: tuple(instance["hmtx"].metrics[glyph_name]) for codepoint, glyph_name in targets.items()}
    finally:
        instance.close()


def align_tnum_digit_target_variations(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {"tnum_digit_target_variations_added": 0, "tnum_digit_target_variation_corrections": 0}
    correction_weights = [weight for weight in sorted(reference_fonts) if weight != 400]
    supports = advance_supports(font, correction_weights)
    targets = tnum_digit_targets(font)
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference_metrics = reference_digit_hmtx(reference_fonts[weight_value])
        current_metrics = tnum_digit_target_hmtx_at_weight(font, weight_value)
        support = supports[weight_value]
        for codepoint, target_glyph in targets.items():
            target_metrics = reference_metrics.get(codepoint)
            current = current_metrics.get(codepoint)
            if not target_metrics or current is None:
                continue
            advance_delta = target_metrics[0] - current[0]
            lsb_delta = target_metrics[1] - current[1]
            if advance_delta:
                add_advance_tuple_variation(font, target_glyph, support, advance_delta)
                variations_added += 1
            if lsb_delta:
                add_lsb_tuple_variation(font, target_glyph, support, lsb_delta)
                variations_added += 1
            if advance_delta or lsb_delta:
                corrections += 1
    return {
        "tnum_digit_target_variations_added": variations_added,
        "tnum_digit_target_variation_corrections": corrections,
    }


def sum_count_reports(*reports: dict[str, int]) -> dict[str, int]:
    total: dict[str, int] = {}
    for report in reports:
        for key, value in report.items():
            total[key] = total.get(key, 0) + value
    return total


def prefix_count_report(report: dict[str, int], prefix: str) -> dict[str, int]:
    return {f"{prefix}{key}": value for key, value in report.items()}


def bake_single_substitution_feature(
    font: TTFont,
    tag: str,
    codepoint_filter: Any | None = None,
) -> int:
    mapping = get_single_substitution_mapping(font, tag)
    if not mapping:
        return 0
    count = 0
    for codepoint, glyph_name in list(font.getBestCmap().items()):
        if codepoint_filter and not codepoint_filter(codepoint):
            continue
        target_name = mapping.get(glyph_name)
        if target_name and target_name in font.getGlyphSet():
            copy_glyph_data(font, target_name, glyph_name)
            count += 1
    return count


def shift_glyph_x(font: TTFont, glyph_name: str, dx: float) -> None:
    dx = otRound(dx)
    if not dx or glyph_name not in font["glyf"].glyphs:
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if glyph.isComposite():
        for component in glyph.components:
            component.x = otRound(component.x + dx)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for index, (x, y) in enumerate(glyph.coordinates):
            glyph.coordinates[index] = otRound(x + dx), y
    glyph.recalcBounds(glyf)


def shift_glyph_y(font: TTFont, glyph_name: str, dy: float) -> None:
    dy = otRound(dy)
    if not dy or glyph_name not in font["glyf"].glyphs:
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if glyph.isComposite():
        for component in glyph.components:
            component.y = otRound(component.y + dy)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for index, (x, y) in enumerate(glyph.coordinates):
            glyph.coordinates[index] = x, otRound(y + dy)
    glyph.recalcBounds(glyf)


def glyph_x_min(font: TTFont, glyph_name: str, fallback: int = 0) -> int:
    if glyph_name not in font["glyf"].glyphs:
        return fallback
    glyph = font["glyf"][glyph_name]
    if glyph.isComposite() or getattr(glyph, "numberOfContours", 0) > 0:
        glyph.recalcBounds(font["glyf"])
        return getattr(glyph, "xMin", fallback)
    return fallback


def sync_hmtx_lsb_to_glyph_bounds(font: TTFont) -> dict[str, int]:
    if "hmtx" not in font or "glyf" not in font:
        return {"hmtx_lsb_synced": 0}
    touched = 0
    for glyph_name, (advance_width, lsb) in list(font["hmtx"].metrics.items()):
        if glyph_name not in font["glyf"].glyphs:
            continue
        new_lsb = glyph_x_min(font, glyph_name, lsb)
        if new_lsb != lsb:
            font["hmtx"].metrics[glyph_name] = (advance_width, new_lsb)
            touched += 1
    return {"hmtx_lsb_synced": touched}


def set_advance_width(font: TTFont, glyph_name: str, width: int) -> None:
    _old_width, lsb = font["hmtx"].metrics.get(glyph_name, (width, 0))
    font["hmtx"].metrics[glyph_name] = (otRound(width), glyph_x_min(font, glyph_name, lsb))


def freeze_advance_variation(font: TTFont, glyph_name: str) -> None:
    if "gvar" not in font:
        return
    for variation in font["gvar"].variations.get(glyph_name, []):
        if len(variation.coordinates) < 4:
            continue
        for index in range(len(variation.coordinates) - 4, len(variation.coordinates)):
            if variation.coordinates[index] is not None:
                variation.coordinates[index] = (0, 0)


def center_to_width(font: TTFont, glyph_name: str, width: int) -> None:
    old_width = font["hmtx"].metrics.get(glyph_name, (width, 0))[0]
    shift_glyph_x(font, glyph_name, (width - old_width) / 2)
    set_advance_width(font, glyph_name, width)
    freeze_advance_variation(font, glyph_name)


def stretch_to_width(font: TTFont, glyph_name: str, width: int) -> None:
    old_width = font["hmtx"].metrics.get(glyph_name, (width, 0))[0]
    if old_width == width or glyph_name not in font["glyf"].glyphs:
        set_advance_width(font, glyph_name, width)
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    delta = width - old_width
    if glyph.isComposite():
        for component in glyph.components:
            if component.x * 2 >= old_width:
                component.x = otRound(component.x + delta)
    elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
        for index, (x, y) in enumerate(glyph.coordinates):
            if x * 2 >= old_width:
                glyph.coordinates[index] = otRound(x + delta), y
    glyph.recalcBounds(glyf)
    set_advance_width(font, glyph_name, width)
    freeze_advance_variation(font, glyph_name)


def bake_source_han_pwid_and_sanitize(font: TTFont) -> dict[str, int]:
    for codepoint in SOURCE_HAN_FORCED_CODEPOINTS - set(SANITIZER_TYPES_PWID):
        clone_cmap_glyph_for_codepoint(font, codepoint)
    pwid_count = bake_single_substitution_feature(font, "pwid", lambda cp: cp in SANITIZER_TYPES_PWID)
    cmap = font.getBestCmap()
    touched = 0
    for codepoint, sanitizer in SANITIZER_TYPES_PWID.items():
        clone_cmap_glyph_for_codepoint(font, codepoint)
    cmap = font.getBestCmap()
    for codepoint, sanitizer in SANITIZER_TYPES_PWID.items():
        glyph_name = cmap.get(codepoint)
        if not glyph_name:
            continue
        if sanitizer in {"ident", "ellipsis"}:
            pass
        elif sanitizer in {"interpunct", "half"}:
            center_to_width(font, glyph_name, font["head"].unitsPerEm // 2)
        elif sanitizer == "stretchDual":
            stretch_to_width(font, glyph_name, font["head"].unitsPerEm * 2)
        elif sanitizer == "stretchTri":
            stretch_to_width(font, glyph_name, font["head"].unitsPerEm * 3)
        touched += 1
    return {"source_han_pwid_baked": pwid_count, "source_han_symbols_sanitized": touched}


def normalize_hangul_widths(font: TTFont) -> int:
    cmap = font.getBestCmap()
    touched: set[str] = set()
    em = font["head"].unitsPerEm
    for codepoint, glyph_name in cmap.items():
        if not is_korean(codepoint) or glyph_name in touched:
            continue
        old_width = font["hmtx"].metrics.get(glyph_name, (em, 0))[0]
        target_width = max(em, math.ceil(old_width / em) * em) if old_width > 0 else em
        shift_glyph_x(font, glyph_name, (target_width - old_width) / 2)
        set_advance_width(font, glyph_name, target_width)
        freeze_advance_variation(font, glyph_name)
        touched.add(glyph_name)
    return len(touched)


def shear_font(font: TTFont, angle_degrees: float) -> None:
    shear = math.tan(math.radians(angle_degrees))
    glyf = font["glyf"]
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        glyph.expand(glyf)
        if glyph.isComposite():
            for component in glyph.components:
                component.x = otRound(component.x + component.y * shear)
        elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
            for index, (x, y) in enumerate(glyph.coordinates):
                glyph.coordinates[index] = otRound(x + y * shear), y
        glyph.recalcBounds(glyf)
    if "gvar" in font:
        for variations in font["gvar"].variations.values():
            for variation in variations:
                for index, xy in enumerate(variation.coordinates):
                    if xy is None:
                        continue
                    x, y = xy
                    variation.coordinates[index] = otRound(x + y * shear), y


def piecewise_map(value: float, segment: dict[float, float]) -> float:
    items = sorted(segment.items())
    if value <= items[0][0]:
        return items[0][1]
    if value >= items[-1][0]:
        return items[-1][1]
    for (x0, y0), (x1, y1) in zip(items, items[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y0
            return y0 + (value - x0) * (y1 - y0) / (x1 - x0)
    return value


def inverse_piecewise_map(value: float, segment: dict[float, float]) -> float:
    items = sorted(segment.items(), key=lambda item: item[1])
    if value <= items[0][1]:
        return items[0][0]
    if value >= items[-1][1]:
        return items[-1][0]
    for (x0, y0), (x1, y1) in zip(items, items[1:]):
        if y0 <= value <= y1:
            if y1 == y0:
                return x0
            return x0 + (value - y0) * (x1 - x0) / (y1 - y0)
    return value


def remap_inter_gvar_supports(base: TTFont, inter: TTFont) -> None:
    if "gvar" not in inter or "fvar" not in inter or "fvar" not in base:
        return
    inter_axis = weight_axis(inter)
    base_axis = weight_axis(base)
    inter_segment = {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    base_segment = {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    if "avar" in inter and "wght" in inter["avar"].segments:
        inter_segment = inter["avar"].segments["wght"]
    if "avar" in base and "wght" in base["avar"].segments:
        base_segment = base["avar"].segments["wght"]
    for variations in inter["gvar"].variations.values():
        for variation in variations:
            support = variation.axes.get("wght")
            if not support:
                continue
            variation.axes["wght"] = tuple(
                piecewise_map(
                    normalize_axis_value(
                        denormalize_axis_value(
                            inverse_piecewise_map(value, inter_segment),
                            inter_axis.minValue,
                            inter_axis.defaultValue,
                            inter_axis.maxValue,
                        ),
                        base_axis.minValue,
                        base_axis.defaultValue,
                        base_axis.maxValue,
                    ),
                    base_segment,
                )
                for value in support
            )


def load_base(region: str, italic: bool, inter_unicodes: set[int]) -> tuple[TTFont, dict[str, Any]]:
    base = TTFont(source_han_vf_path(region))
    base = instantiateVariableFont(base, AXIS_LIMIT, inplace=False, optimize=True)
    sarasa_report: dict[str, Any] = {}
    override_path = classical_vf_override_path(region)
    if override_path:
        override = TTFont(override_path)
        try:
            override = instantiateVariableFont(override, AXIS_LIMIT, inplace=False, optimize=True)
            sarasa_report.update(apply_classical_vf_override(base, override, region))
        finally:
            override.close()
    public_axis_report = apply_public_weight_axis(base)
    subset_font(base, source_han_unicodes_like_sarasa(region, base, inter_unicodes))
    sarasa_report.update(bake_source_han_pwid_and_sanitize(base))
    sarasa_report.update(public_axis_report)
    sarasa_report["hangul_widths_normalized"] = normalize_hangul_widths(base)
    if italic:
        shear_font(base, 9.4)
    return base, sarasa_report


def load_inter(italic: bool) -> TTFont:
    inter = TTFont(INTER_ITALIC if italic else INTER_UPRIGHT)
    inter = instantiateVariableFont(inter, INTER_AXIS_LIMIT, inplace=False, optimize=True)
    scale_upem(inter, 1000)
    bake_single_substitution_feature(inter, "ss03")
    bake_single_substitution_feature(inter, "cv10")
    bake_inter_ui_tnum_defaults(inter)
    return inter


def bake_inter_ui_tnum_defaults(font: TTFont) -> int:
    mapping = get_single_substitution_mapping(font, "tnum")
    if not mapping:
        return 0
    touched = 0
    skip = set(range(0x30, 0x3A)) | {0x2D, 0x3A}
    for cmap_table in font["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint, glyph_name in list(cmap_table.cmap.items()):
            if codepoint in skip:
                continue
            target = mapping.get(glyph_name)
            if target and target in font.getGlyphSet():
                cmap_table.cmap[codepoint] = target
                touched += 1
    return touched


def append_inter_glyphs(base: TTFont, inter: TTFont, allowed_unicodes: set[int]) -> dict[str, Any]:
    remap_inter_gvar_supports(base, inter)
    source_order = inter.getGlyphOrder()
    source_names = set(source_order)
    existing = set(base.getGlyphOrder())
    rename = {name: prefixed(name) for name in source_order if name != ".notdef"}

    base_order_before = len(base.getGlyphOrder())
    new_order = base.getGlyphOrder()
    for source_name in source_order:
        if source_name == ".notdef":
            continue
        target_name = rename[source_name]
        if target_name in existing:
            continue
        glyph = copy.deepcopy(inter["glyf"][source_name])
        glyph.expand(inter["glyf"])
        if glyph.isComposite():
            for component in glyph.components:
                if component.glyphName in source_names:
                    component.glyphName = rename[component.glyphName]
        base["glyf"].glyphs[target_name] = glyph
        base["hmtx"].metrics[target_name] = copy.deepcopy(inter["hmtx"].metrics.get(source_name, (0, 0)))
        if "vmtx" in base:
            base["vmtx"].metrics[target_name] = (1000, 0)
        if "gvar" in base and "gvar" in inter:
            base["gvar"].variations[target_name] = copy.deepcopy(inter["gvar"].variations.get(source_name, []))
        new_order.append(target_name)

    base.setGlyphOrder(new_order)
    if "maxp" in base:
        base["maxp"].numGlyphs = len(new_order)

    remapped_cmap = 0
    inter_cmap = inter.getBestCmap()
    base_cmap = base.getBestCmap()
    allowed_inter_unicodes = {cp for cp in allowed_unicodes if cp in inter_cmap and cp not in base_cmap}
    for cmap_table in base["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint, source_name in inter_cmap.items():
            if codepoint > 0xFFFF and cmap_table.format in {0, 2, 4, 6}:
                continue
            if codepoint in allowed_inter_unicodes and source_name in rename:
                cmap_table.cmap[codepoint] = rename[source_name]
                remapped_cmap += 1

    return {
        "base_subset_glyphs_before_inter": base_order_before,
        "appended_inter_glyphs": len(rename),
        "remapped_inter_cmap_entries": remapped_cmap,
    }


def rename_ot_glyph_references(obj: Any, rename: dict[str, str], seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if isinstance(obj, str) or obj is None or isinstance(obj, (int, float, bool, bytes)):
        return
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            new_key = rename.get(key, key) if isinstance(key, str) else key
            if new_key != key:
                del obj[key]
                obj[new_key] = value
            if isinstance(value, str) and value in rename:
                obj[new_key] = rename[value]
            else:
                rename_ot_glyph_references(obj[new_key], rename, seen)
        return
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            if isinstance(value, str) and value in rename:
                obj[index] = rename[value]
            else:
                rename_ot_glyph_references(value, rename, seen)
        return
    if isinstance(obj, tuple):
        return
    if hasattr(obj, "__dict__"):
        for key, value in vars(obj).items():
            if key.lower().endswith("tag"):
                continue
            if isinstance(value, str) and value in rename:
                setattr(obj, key, rename[value])
            else:
                rename_ot_glyph_references(value, rename, seen)


def apply_glyph_rename_map(font: TTFont, rename: dict[str, str]) -> None:
    rename = {old: new for old, new in rename.items() if old != new}
    if not rename:
        return

    font.setGlyphOrder([rename.get(glyph_name, glyph_name) for glyph_name in font.getGlyphOrder()])
    if "glyf" in font:
        glyf = font["glyf"]
        glyf.glyphs = {rename.get(glyph_name, glyph_name): glyph for glyph_name, glyph in glyf.glyphs.items()}
        for glyph in glyf.glyphs.values():
            if glyph.isComposite():
                for component in getattr(glyph, "components", []):
                    component.glyphName = rename.get(component.glyphName, component.glyphName)
    for table_tag in ("hmtx", "vmtx"):
        if table_tag in font:
            metrics = font[table_tag].metrics
            font[table_tag].metrics = {
                rename.get(glyph_name, glyph_name): value for glyph_name, value in metrics.items()
            }
    if "cmap" in font:
        for cmap_table in font["cmap"].tables:
            cmap_table.cmap = {
                codepoint: rename.get(glyph_name, glyph_name) for codepoint, glyph_name in cmap_table.cmap.items()
            }
    if "gvar" in font:
        font["gvar"].variations = {
            rename.get(glyph_name, glyph_name): value for glyph_name, value in font["gvar"].variations.items()
        }
    if "VORG" in font:
        records = font["VORG"].VOriginRecords
        font["VORG"].VOriginRecords = {
            rename.get(glyph_name, glyph_name): value for glyph_name, value in records.items()
        }
    for table_tag in ("GDEF", "GSUB", "GPOS", "BASE", "JSTF", "MATH", "COLR"):
        if table_tag in font:
            rename_ot_glyph_references(font[table_tag].table, rename)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(font.getGlyphOrder())


def rename_glyphs(font: TTFont, rename: dict[str, str]) -> int:
    glyph_set = set(font.getGlyphOrder())
    rename = {old: new for old, new in rename.items() if old != new and old in glyph_set}
    if not rename:
        return 0
    targets = set(rename.values())
    collisions = targets & (glyph_set - set(rename))
    if collisions:
        raise ValueError(f"Cannot rename glyphs onto existing glyphs: {sorted(collisions)[:8]}")

    glyph_set = set(font.getGlyphOrder())
    temporary: dict[str, str] = {}
    for index, old in enumerate(rename):
        candidate = f"zzTmpRename{index:05d}"
        while candidate in glyph_set or candidate in targets:
            index += 1
            candidate = f"zzTmpRename{index:05d}"
        temporary[old] = candidate
        glyph_set.add(candidate)
    apply_glyph_rename_map(font, temporary)
    apply_glyph_rename_map(font, {temporary[old]: new for old, new in rename.items()})
    return len(rename)


def collect_ot_glyph_references(obj: Any, glyphs: set[str], out: set[str], seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (int, float, bool, bytes)):
        return
    if isinstance(obj, str):
        if obj in glyphs:
            out.add(obj)
        return
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key in glyphs:
                out.add(key)
            collect_ot_glyph_references(value, glyphs, out, seen)
        return
    if isinstance(obj, (list, tuple)):
        for value in obj:
            collect_ot_glyph_references(value, glyphs, out, seen)
        return
    if hasattr(obj, "__dict__"):
        for key, value in vars(obj).items():
            if key.lower().endswith("tag"):
                continue
            collect_ot_glyph_references(value, glyphs, out, seen)


def referenced_glyphs(font: TTFont) -> set[str]:
    glyphs = set(font.getGlyphOrder())
    refs: set[str] = set()
    if "cmap" in font:
        for cmap_table in font["cmap"].tables:
            refs.update(glyph for glyph in cmap_table.cmap.values() if glyph in glyphs)
    if "glyf" in font:
        for glyph in font["glyf"].glyphs.values():
            if glyph.isComposite():
                refs.update(component.glyphName for component in getattr(glyph, "components", []) if component.glyphName in glyphs)
    for table_tag in ("GDEF", "GSUB", "GPOS", "BASE", "JSTF", "MATH", "COLR"):
        if table_tag in font:
            collect_ot_glyph_references(font[table_tag].table, glyphs, refs)
    return refs


def remove_glyphs(font: TTFont, glyph_names: set[str]) -> int:
    glyph_names = {name for name in glyph_names if name != ".notdef" and name in set(font.getGlyphOrder())}
    if not glyph_names:
        return 0
    refs = referenced_glyphs(font)
    removable = glyph_names - refs
    if not removable:
        return 0
    font.setGlyphOrder([glyph_name for glyph_name in font.getGlyphOrder() if glyph_name not in removable])
    if "glyf" in font:
        for glyph_name in removable:
            font["glyf"].glyphs.pop(glyph_name, None)
    for table_tag in ("hmtx", "vmtx"):
        if table_tag in font:
            for glyph_name in removable:
                font[table_tag].metrics.pop(glyph_name, None)
    if "gvar" in font:
        for glyph_name in removable:
            font["gvar"].variations.pop(glyph_name, None)
    if "VORG" in font:
        for glyph_name in removable:
            font["VORG"].VOriginRecords.pop(glyph_name, None)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(font.getGlyphOrder())
    return len(removable)


def strip_ot_variation_devices(obj: Any, seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (str, int, float, bool, bytes)):
        return
    obj_id = id(obj)
    if obj_id in seen:
        return
    seen.add(obj_id)
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
                obj[key] = None
            else:
                strip_ot_variation_devices(value, seen)
        return
    if isinstance(obj, list):
        for index, value in enumerate(obj):
            if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
                obj[index] = None
            else:
                strip_ot_variation_devices(value, seen)
        return
    if isinstance(obj, tuple):
        for value in obj:
            strip_ot_variation_devices(value, seen)
        return
    if hasattr(obj, "__dict__"):
        for key, value in vars(obj).items():
            if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
                setattr(obj, key, None)
            else:
                strip_ot_variation_devices(value, seen)


def append_layout_features(
    base: TTFont,
    inter: TTFont,
    table_tag: str,
    feature_tags: set[str],
) -> dict[str, int]:
    if table_tag not in inter:
        return {f"inter_{table_tag.lower()}_features_imported": 0, f"inter_{table_tag.lower()}_lookups_imported": 0}
    if table_tag not in base:
        base[table_tag] = copy.deepcopy(inter[table_tag])
        rename = {name: prefixed(name) for name in inter.getGlyphOrder() if name != ".notdef"}
        rename_ot_glyph_references(base[table_tag].table, rename)
        return {
            f"inter_{table_tag.lower()}_features_imported": len(base[table_tag].table.FeatureList.FeatureRecord)
            if base[table_tag].table.FeatureList
            else 0,
            f"inter_{table_tag.lower()}_lookups_imported": len(base[table_tag].table.LookupList.Lookup)
            if base[table_tag].table.LookupList
            else 0,
        }

    source = inter[table_tag].table
    target = base[table_tag].table
    if not source.FeatureList or not source.LookupList:
        return {f"inter_{table_tag.lower()}_features_imported": 0, f"inter_{table_tag.lower()}_lookups_imported": 0}
    if target.LookupList is None:
        target.LookupList = ot.LookupList()
        target.LookupList.Lookup = []
        target.LookupList.LookupCount = 0
    if target.FeatureList is None:
        target.FeatureList = ot.FeatureList()
        target.FeatureList.FeatureRecord = []
        target.FeatureList.FeatureCount = 0

    rename = {name: prefixed(name) for name in inter.getGlyphOrder() if name != ".notdef"}
    feature_records = [record for record in source.FeatureList.FeatureRecord if record.FeatureTag in feature_tags]
    lookup_indices = sorted({index for record in feature_records for index in record.Feature.LookupListIndex})
    lookup_index_map: dict[int, int] = {}
    for old_index in lookup_indices:
        lookup = copy.deepcopy(source.LookupList.Lookup[old_index])
        rename_ot_glyph_references(lookup, rename)
        if table_tag == "GPOS":
            strip_ot_variation_devices(lookup)
        new_index = len(target.LookupList.Lookup)
        target.LookupList.Lookup.append(lookup)
        lookup_index_map[old_index] = new_index
    target.LookupList.LookupCount = len(target.LookupList.Lookup)

    imported_tags: set[str] = set()
    for source_record in feature_records:
        record = copy.deepcopy(source_record)
        record.Feature.LookupListIndex = [lookup_index_map[index] for index in source_record.Feature.LookupListIndex if index in lookup_index_map]
        record.Feature.LookupCount = len(record.Feature.LookupListIndex)
        if not record.Feature.LookupListIndex:
            continue
        target.FeatureList.FeatureRecord.append(record)
        imported_tags.add(record.FeatureTag)
    target.FeatureList.FeatureCount = len(target.FeatureList.FeatureRecord)
    enable_features_for_all_scripts(base, imported_tags, table_tag)
    return {
        f"inter_{table_tag.lower()}_features_imported": len(imported_tags),
        f"inter_{table_tag.lower()}_lookups_imported": len(lookup_index_map),
    }


def import_inter_layout_features(base: TTFont, inter: TTFont) -> dict[str, int]:
    report: dict[str, int] = {}
    report.update(append_layout_features(base, inter, "GSUB", INTER_GSUB_FEATURES))
    report.update(append_layout_features(base, inter, "GPOS", INTER_GPOS_FEATURES))
    return report


def remove_metric_variation_maps(font: TTFont) -> None:
    for tag in ("HVAR", "VVAR"):
        if tag in font:
            del font[tag]


def drop_feature_records(table: Any, tags: set[str]) -> int:
    if not table or not table.table or not table.table.FeatureList:
        return 0
    root = table.table
    old_records = root.FeatureList.FeatureRecord
    keep_records = [record for record in old_records if record.FeatureTag not in tags]
    if len(keep_records) == len(old_records):
        return 0
    remap: dict[int, int] = {}
    next_index = 0
    for old_index, record in enumerate(old_records):
        if record.FeatureTag not in tags:
            remap[old_index] = next_index
            next_index += 1
    root.FeatureList.FeatureRecord = keep_records
    root.FeatureList.FeatureCount = len(keep_records)
    if root.ScriptList:
        for script_record in root.ScriptList.ScriptRecord:
            langsys_list = []
            if script_record.Script.DefaultLangSys:
                langsys_list.append(script_record.Script.DefaultLangSys)
            langsys_list.extend(record.LangSys for record in script_record.Script.LangSysRecord)
            for langsys in langsys_list:
                old_indices = list(langsys.FeatureIndex or [])
                langsys.FeatureIndex = [remap[i] for i in old_indices if i in remap]
                langsys.FeatureCount = len(langsys.FeatureIndex)
    return len(old_records) - len(keep_records)


def drop_sarasa_width_features(font: TTFont) -> dict[str, int]:
    return {
        "gsub_width_features_dropped": drop_feature_records(font["GSUB"], WIDTH_FEATURES) if "GSUB" in font else 0,
        "gpos_width_features_dropped": drop_feature_records(font["GPOS"], WIDTH_FEATURES) if "GPOS" in font else 0,
    }


def drop_nonfinal_gsub_features(font: TTFont, allowed_features: set[str] = FINAL_GSUB_FEATURES) -> int:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return 0
    tags = {record.FeatureTag for record in font["GSUB"].table.FeatureList.FeatureRecord}
    return drop_feature_records(font["GSUB"], tags - allowed_features)


def align_layout_feature_template(font: TTFont, reference: TTFont, table_tag: str) -> dict[str, int]:
    key = table_tag.lower()
    if table_tag not in font or table_tag not in reference:
        return {
            f"{key}_feature_records_before_template": 0,
            f"{key}_feature_records_after_template": 0,
            f"{key}_langsys_after_template": 0,
        }
    table = font[table_tag].table
    ref_table = reference[table_tag].table
    if not table.FeatureList or not ref_table.FeatureList or not ref_table.ScriptList:
        return {
            f"{key}_feature_records_before_template": 0,
            f"{key}_feature_records_after_template": 0,
            f"{key}_langsys_after_template": 0,
        }

    current_by_tag: dict[str, list[Any]] = {}
    for record in table.FeatureList.FeatureRecord:
        current_by_tag.setdefault(record.FeatureTag, []).append(record)

    old_count = len(table.FeatureList.FeatureRecord)
    ref_to_new: dict[int, int] = {}
    used_by_tag: dict[str, int] = {}
    new_records = []
    for ref_index, ref_record in enumerate(ref_table.FeatureList.FeatureRecord):
        candidates = current_by_tag.get(ref_record.FeatureTag)
        if not candidates:
            continue
        use_index = min(used_by_tag.get(ref_record.FeatureTag, 0), len(candidates) - 1)
        used_by_tag[ref_record.FeatureTag] = used_by_tag.get(ref_record.FeatureTag, 0) + 1
        record = copy.deepcopy(candidates[use_index])
        record.FeatureTag = ref_record.FeatureTag
        ref_to_new[ref_index] = len(new_records)
        new_records.append(record)

    if not new_records:
        return {
            f"{key}_feature_records_before_template": old_count,
            f"{key}_feature_records_after_template": old_count,
            f"{key}_langsys_after_template": 0,
        }

    def remap_langsys(langsys: Any) -> Any | None:
        new_langsys = copy.deepcopy(langsys)
        indices = [ref_to_new[index] for index in list(langsys.FeatureIndex or []) if index in ref_to_new]
        if not indices:
            return None
        new_langsys.FeatureIndex = indices
        new_langsys.FeatureCount = len(indices)
        if getattr(new_langsys, "ReqFeatureIndex", 0xFFFF) != 0xFFFF:
            new_langsys.ReqFeatureIndex = ref_to_new.get(new_langsys.ReqFeatureIndex, 0xFFFF)
        return new_langsys

    new_script_list = ot.ScriptList()
    new_script_list.ScriptRecord = []
    langsys_count = 0
    for ref_script_record in ref_table.ScriptList.ScriptRecord:
        script = ot.Script()
        script.DefaultLangSys = None
        script.LangSysRecord = []
        if ref_script_record.Script.DefaultLangSys:
            script.DefaultLangSys = remap_langsys(ref_script_record.Script.DefaultLangSys)
            if script.DefaultLangSys:
                langsys_count += 1
        for ref_lang_record in ref_script_record.Script.LangSysRecord:
            langsys = remap_langsys(ref_lang_record.LangSys)
            if not langsys:
                continue
            lang_record = ot.LangSysRecord()
            lang_record.LangSysTag = ref_lang_record.LangSysTag
            lang_record.LangSys = langsys
            script.LangSysRecord.append(lang_record)
            langsys_count += 1
        if not script.DefaultLangSys and not script.LangSysRecord:
            continue
        script.LangSysCount = len(script.LangSysRecord)
        script_record = ot.ScriptRecord()
        script_record.ScriptTag = ref_script_record.ScriptTag
        script_record.Script = script
        new_script_list.ScriptRecord.append(script_record)

    new_script_list.ScriptCount = len(new_script_list.ScriptRecord)
    table.FeatureList.FeatureRecord = new_records
    table.FeatureList.FeatureCount = len(new_records)
    table.ScriptList = new_script_list
    return {
        f"{key}_feature_records_before_template": old_count,
        f"{key}_feature_records_after_template": len(new_records),
        f"{key}_langsys_after_template": langsys_count,
    }


def pad_lookup_list_to_reference_count(font: TTFont, reference: TTFont, table_tag: str) -> dict[str, int]:
    key = table_tag.lower()
    if table_tag not in font or table_tag not in reference:
        return {f"{key}_lookups_before_padding": 0, f"{key}_lookups_after_padding": 0}
    table = font[table_tag].table
    ref_table = reference[table_tag].table
    if not table.LookupList or not ref_table.LookupList or not table.LookupList.Lookup:
        return {f"{key}_lookups_before_padding": 0, f"{key}_lookups_after_padding": 0}
    before = len(table.LookupList.Lookup)
    target = len(ref_table.LookupList.Lookup)
    while len(table.LookupList.Lookup) < target:
        table.LookupList.Lookup.append(copy.deepcopy(table.LookupList.Lookup[-1]))
    table.LookupList.LookupCount = len(table.LookupList.Lookup)
    return {f"{key}_lookups_before_padding": before, f"{key}_lookups_after_padding": len(table.LookupList.Lookup)}


def append_single_sub_feature(font: TTFont, tag: str, mapping: dict[str, str]) -> bool:
    mapping = {src: dst for src, dst in mapping.items() if src in font.getGlyphSet() and dst in font.getGlyphSet()}
    if not mapping or "GSUB" not in font:
        return False

    gsub = font["GSUB"].table
    if gsub.LookupList is None:
        gsub.LookupList = ot.LookupList()
        gsub.LookupList.Lookup = []
        gsub.LookupList.LookupCount = 0
    if gsub.FeatureList is None:
        gsub.FeatureList = ot.FeatureList()
        gsub.FeatureList.FeatureRecord = []
        gsub.FeatureList.FeatureCount = 0

    subtable = ot.SingleSubst()
    subtable.mapping = mapping

    lookup = ot.Lookup()
    lookup.LookupType = 1
    lookup.LookupFlag = 0
    lookup.SubTable = [subtable]
    lookup.SubTableCount = 1

    lookup_index = len(gsub.LookupList.Lookup)
    gsub.LookupList.Lookup.append(lookup)
    gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)

    feature = ot.Feature()
    feature.FeatureParams = None
    feature.LookupListIndex = [lookup_index]
    feature.LookupCount = 1

    record = ot.FeatureRecord()
    record.FeatureTag = tag
    record.Feature = feature
    gsub.FeatureList.FeatureRecord.append(record)
    gsub.FeatureList.FeatureCount = len(gsub.FeatureList.FeatureRecord)
    return True


def glyph_order_sorted(font: TTFont, glyphs: list[str]) -> list[str]:
    order = {glyph_name: index for index, glyph_name in enumerate(font.getGlyphOrder())}
    return sorted(glyphs, key=lambda glyph_name: order.get(glyph_name, 10**9))


def coverage(font: TTFont, glyphs: list[str]) -> ot.Coverage:
    cov = ot.Coverage()
    cov.glyphs = glyph_order_sorted(font, glyphs)
    return cov


def append_gsub_lookup(font: TTFont, lookup: ot.Lookup) -> int:
    gsub = font["GSUB"].table
    if gsub.LookupList is None:
        gsub.LookupList = ot.LookupList()
        gsub.LookupList.Lookup = []
        gsub.LookupList.LookupCount = 0
    lookup_index = len(gsub.LookupList.Lookup)
    gsub.LookupList.Lookup.append(lookup)
    gsub.LookupList.LookupCount = len(gsub.LookupList.Lookup)
    return lookup_index


def append_gsub_feature(font: TTFont, tag: str, lookup_indices: list[int]) -> int:
    gsub = font["GSUB"].table
    if gsub.FeatureList is None:
        gsub.FeatureList = ot.FeatureList()
        gsub.FeatureList.FeatureRecord = []
        gsub.FeatureList.FeatureCount = 0
    feature = ot.Feature()
    feature.FeatureParams = None
    feature.LookupListIndex = lookup_indices
    feature.LookupCount = len(lookup_indices)
    record = ot.FeatureRecord()
    record.FeatureTag = tag
    record.Feature = feature
    feature_index = len(gsub.FeatureList.FeatureRecord)
    gsub.FeatureList.FeatureRecord.append(record)
    gsub.FeatureList.FeatureCount = len(gsub.FeatureList.FeatureRecord)
    return feature_index


def enable_features_for_all_scripts(font: TTFont, tags: set[str], table_tag: str = "GSUB") -> None:
    if table_tag not in font:
        return
    table = font[table_tag].table
    if not table.FeatureList or not table.ScriptList:
        return
    indices = [i for i, record in enumerate(table.FeatureList.FeatureRecord) if record.FeatureTag in tags]
    if not indices:
        return
    for script_record in table.ScriptList.ScriptRecord:
        langsys_list = []
        if script_record.Script.DefaultLangSys:
            langsys_list.append(script_record.Script.DefaultLangSys)
        langsys_list.extend(record.LangSys for record in script_record.Script.LangSysRecord)
        for langsys in langsys_list:
            feature_indices = list(langsys.FeatureIndex or [])
            for index in indices:
                if index not in feature_indices:
                    feature_indices.append(index)
            langsys.FeatureIndex = feature_indices
            langsys.FeatureCount = len(feature_indices)


def merge_gsub_lookup_indices_into_features(font: TTFont, tag: str, lookup_indices: list[int]) -> dict[str, int]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return {f"{tag}_features_merged": 0, f"{tag}_feature_lookup_links_added": 0}
    merged = 0
    added = 0
    for record in font["GSUB"].table.FeatureList.FeatureRecord:
        if record.FeatureTag != tag:
            continue
        indices = list(record.Feature.LookupListIndex or [])
        before = len(indices)
        for lookup_index in lookup_indices:
            if lookup_index not in indices:
                indices.append(lookup_index)
        record.Feature.LookupListIndex = indices
        record.Feature.LookupCount = len(indices)
        merged += 1
        added += len(indices) - before
    return {f"{tag}_features_merged": merged, f"{tag}_feature_lookup_links_added": added}


def ensure_empty_gsub_features(font: TTFont, tags: set[str]) -> dict[str, int]:
    added = 0
    for tag in sorted(tags):
        if not has_feature(font, tag):
            append_gsub_feature(font, tag, [])
            added += 1
    enable_features_for_all_scripts(font, tags)
    return {"empty_gsub_features_added": added}


def collect_prefixed_inter_feature_mapping(inter: TTFont, tag: str) -> dict[str, str]:
    return {prefixed(src): prefixed(dst) for src, dst in get_single_substitution_mapping(inter, tag).items()}


def add_digit_width_features(font: TTFont, inter: TTFont) -> dict[str, Any]:
    tnum = collect_prefixed_inter_feature_mapping(inter, "tnum")
    pnum = collect_prefixed_inter_feature_mapping(inter, "pnum")
    if not pnum:
        pnum = {dst: src for src, dst in tnum.items()}
    enable_features_for_all_scripts(font, {"tnum", "pnum"})
    return {
        "tnum_feature_added": has_feature(font, "tnum"),
        "pnum_feature_added": has_feature(font, "pnum"),
        "tnum_mappings": len(tnum),
        "pnum_mappings": len(pnum),
    }


def make_polygon_glyph(points: list[tuple[float, float]]) -> Any:
    pen = TTGlyphPen(None)
    pen.moveTo((otRound(points[0][0]), otRound(points[0][1])))
    for x, y in points[1:]:
        pen.lineTo((otRound(x), otRound(y)))
    pen.closePath()
    return pen.glyph()


def add_simple_glyph(font: TTFont, glyph_name: str, source_name: str, points: list[tuple[float, float]]) -> None:
    font["glyf"].glyphs[glyph_name] = make_polygon_glyph(points)
    font["hmtx"].metrics[glyph_name] = copy.deepcopy(font["hmtx"].metrics[source_name])
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        font["vmtx"].metrics[glyph_name] = copy.deepcopy(font["vmtx"].metrics[source_name])
    if "gvar" in font:
        font["gvar"].variations[glyph_name] = []
    order = font.getGlyphOrder()
    if glyph_name not in order:
        order.append(glyph_name)
        font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(font.getGlyphOrder())


def glyph_bbox(font: TTFont, glyph_name: str) -> tuple[int, int, int, int] | None:
    if glyph_name not in font["glyf"].glyphs:
        return None
    glyph = font["glyf"][glyph_name]
    glyph.recalcBounds(font["glyf"])
    if not hasattr(glyph, "xMin"):
        return None
    return glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax


def add_vert_alias(font: TTFont, source_codepoint: int, target_codepoint: int) -> int:
    if "GSUB" not in font:
        return 0
    cmap = font.getBestCmap()
    source_glyph = cmap.get(source_codepoint)
    target_glyph = cmap.get(target_codepoint)
    if not source_glyph or not target_glyph:
        return 0

    added = 0
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return 0
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in {"vert", "vrt2"}:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            if lookup.LookupType != 1:
                continue
            for subtable in lookup.SubTable:
                if not hasattr(subtable, "mapping"):
                    continue
                target_substitution = subtable.mapping.get(target_glyph)
                if target_substitution and subtable.mapping.get(source_glyph) != target_substitution:
                    subtable.mapping[source_glyph] = target_substitution
                    added += 1
    return added


def add_continuous_em_dash_feature(font: TTFont) -> dict[str, Any]:
    cmap = font.getBestCmap()
    em_dash = cmap.get(0x2014)
    if not em_dash or em_dash not in font["glyf"].glyphs:
        return {"continuous_em_dash_feature_added": False, "continuous_em_dash_vert_mappings": 0}
    vert_aliases = add_vert_alias(font, 0x2014, 0x2015)

    em_dash_box = glyph_bbox(font, em_dash)
    if not em_dash_box:
        return {
            "continuous_em_dash_feature_added": False,
            "continuous_em_dash_vert_mappings": 0,
            "continuous_em_dash_vert_aliases": vert_aliases,
        }
    x_min, y_min, x_max, y_max = em_dash_box
    if x_min <= 0:
        return {
            "continuous_em_dash_feature_added": False,
            "continuous_em_dash_vert_mappings": 0,
            "continuous_em_dash_vert_aliases": vert_aliases,
        }

    vert_mapping = get_single_substitution_mapping(font, "vert")
    vrt2_mapping = get_single_substitution_mapping(font, "vrt2")
    em_dash_v = vert_mapping.get(em_dash) or vrt2_mapping.get(em_dash) or em_dash
    if em_dash_v not in font["glyf"].glyphs:
        em_dash_v = em_dash
    em_dash_v_box = glyph_bbox(font, em_dash_v)
    if not em_dash_v_box:
        em_dash_v_box = em_dash_box

    advance_width = font["hmtx"].metrics.get(em_dash, (font["head"].unitsPerEm, 0))[0]
    x_min_v, y_min_v, x_max_v, _y_max_v = em_dash_v_box
    advance_height = font["vmtx"].metrics.get(em_dash_v, (font["head"].unitsPerEm, 0))[0] if "vmtx" in font else font["head"].unitsPerEm

    em_dash_cont = em_dash + ".cont"
    em_dash_v_cont = em_dash_v + ".cont"
    half_height = (y_max - y_min) / 2
    add_simple_glyph(
        font,
        em_dash_cont,
        em_dash,
        [
            (x_max - advance_width, y_max),
            (x_max - advance_width - half_height, (y_min + y_max) / 2),
            (x_max - advance_width, y_min),
            (x_max, y_min),
            (x_max, y_max),
        ],
    )
    add_simple_glyph(
        font,
        em_dash_v_cont,
        em_dash_v,
        [
            (x_min_v, y_min_v),
            (x_max_v, y_min_v),
            (x_max_v, y_min_v + advance_height),
            ((x_min_v + x_max_v) / 2, y_min_v + advance_height + (x_max_v - x_min_v) / 2),
            (x_min_v, y_min_v + advance_height),
        ],
    )

    single_sub = ot.SingleSubst()
    single_sub.mapping = {em_dash: em_dash_cont}
    single_lookup = ot.Lookup()
    single_lookup.LookupType = 1
    single_lookup.LookupFlag = 0
    single_lookup.SubTable = [single_sub]
    single_lookup.SubTableCount = 1
    single_index = append_gsub_lookup(font, single_lookup)

    chain = ot.ChainContextSubst()
    chain.Format = 3
    chain.BacktrackGlyphCount = 1
    chain.BacktrackCoverage = [coverage(font, [em_dash, em_dash_cont])]
    chain.InputGlyphCount = 1
    chain.InputCoverage = [coverage(font, [em_dash])]
    chain.LookAheadGlyphCount = 0
    chain.LookAheadCoverage = []
    subst_record = ot.SubstLookupRecord()
    subst_record.SequenceIndex = 0
    subst_record.LookupListIndex = single_index
    chain.SubstCount = 1
    chain.SubstLookupRecord = [subst_record]

    chain_lookup = ot.Lookup()
    chain_lookup.LookupType = 6
    chain_lookup.LookupFlag = 0
    chain_lookup.SubTable = [chain]
    chain_lookup.SubTableCount = 1
    chain_index = append_gsub_lookup(font, chain_lookup)
    append_gsub_feature(font, "calt", [chain_index])

    vert_mappings = 0
    for tag in ("vert", "vrt2"):
        if "GSUB" not in font:
            continue
        gsub = font["GSUB"].table
        if not gsub.FeatureList or not gsub.LookupList:
            continue
        for record in gsub.FeatureList.FeatureRecord:
            if record.FeatureTag != tag:
                continue
            for lookup_index in record.Feature.LookupListIndex:
                lookup = gsub.LookupList.Lookup[lookup_index]
                if lookup.LookupType != 1:
                    continue
                for subtable in lookup.SubTable:
                    if hasattr(subtable, "mapping") and em_dash in subtable.mapping:
                        subtable.mapping[em_dash_cont] = em_dash_v_cont
                        vert_mappings += 1

    enable_features_for_all_scripts(font, {"calt", "vert", "vrt2"})
    return {
        "continuous_em_dash_feature_added": True,
        "continuous_em_dash_vert_mappings": vert_mappings,
        "continuous_em_dash_vert_aliases": vert_aliases,
    }


def tnum_digit_glyphs(font: TTFont, digit_names: list[str]) -> list[str]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return []
    result: list[str] = []
    lookup_list = font["GSUB"].table.LookupList.Lookup if font["GSUB"].table.LookupList else []
    for feature_record in font["GSUB"].table.FeatureList.FeatureRecord:
        if feature_record.FeatureTag != "tnum":
            continue
        for lookup_index in feature_record.Feature.LookupListIndex:
            if lookup_index >= len(lookup_list):
                continue
            lookup = lookup_list[lookup_index]
            if lookup.LookupType != 1:
                continue
            for subtable in lookup.SubTable:
                if not hasattr(subtable, "mapping"):
                    continue
                for digit_name in digit_names:
                    target = subtable.mapping.get(digit_name)
                    if target:
                        result.append(target)
    return result


def calt_referenced_substitution_lookups(font: TTFont) -> set[int]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList or not font["GSUB"].table.LookupList:
        return set()
    lookup_list = font["GSUB"].table.LookupList.Lookup
    stack: list[int] = []
    seen: set[int] = set()
    for feature_record in font["GSUB"].table.FeatureList.FeatureRecord:
        if feature_record.FeatureTag == "calt":
            stack.extend(feature_record.Feature.LookupListIndex)
    while stack:
        lookup_index = stack.pop()
        if lookup_index in seen or lookup_index >= len(lookup_list):
            continue
        seen.add(lookup_index)
        lookup = lookup_list[lookup_index]
        for subtable in lookup.SubTable:
            for record in getattr(subtable, "SubstLookupRecord", []) or []:
                stack.append(record.LookupListIndex)
            for rule_set_attr in ("SubRuleSet", "ChainSubRuleSet", "SubClassSet", "ChainSubClassSet"):
                for rule_set in getattr(subtable, rule_set_attr, []) or []:
                    if not rule_set:
                        continue
                    for rule_list_attr in ("SubRule", "ChainSubRule", "SubClassRule", "ChainSubClassRule"):
                        for rule in getattr(rule_set, rule_list_attr, []) or []:
                            for record in getattr(rule, "SubstLookupRecord", []) or []:
                                stack.append(record.LookupListIndex)
    return seen


def remove_existing_calt_colon_substitutions(font: TTFont, colon: str) -> tuple[str | None, int]:
    if "GSUB" not in font or not font["GSUB"].table.LookupList:
        return None, 0
    lookup_list = font["GSUB"].table.LookupList.Lookup
    raised = None
    removed = 0
    for lookup_index in sorted(calt_referenced_substitution_lookups(font)):
        if lookup_index >= len(lookup_list):
            continue
        lookup = lookup_list[lookup_index]
        if lookup.LookupType != 1:
            continue
        for subtable in lookup.SubTable:
            if not hasattr(subtable, "mapping") or colon not in subtable.mapping:
                continue
            raised = raised or subtable.mapping[colon]
            del subtable.mapping[colon]
            removed += 1
    return raised, removed


def coverage_contains_glyph(cov: Any, glyph_name: str) -> bool:
    return glyph_name in (getattr(cov, "glyphs", []) or [])


def digit_colon_calt_lookup_indices(font: TTFont, colon: str) -> set[int]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList or not font["GSUB"].table.LookupList:
        return set()
    lookup_list = font["GSUB"].table.LookupList.Lookup
    result: set[int] = set()
    for feature_record in font["GSUB"].table.FeatureList.FeatureRecord:
        if feature_record.FeatureTag != "calt":
            continue
        for lookup_index in list(feature_record.Feature.LookupListIndex or []):
            if lookup_index >= len(lookup_list):
                continue
            lookup = lookup_list[lookup_index]
            if lookup.LookupType != 6:
                continue
            target_indices: set[int] = set()
            for subtable in lookup.SubTable:
                input_coverages = getattr(subtable, "InputCoverage", []) or []
                if not any(coverage_contains_glyph(cov, colon) for cov in input_coverages):
                    continue
                for record in getattr(subtable, "SubstLookupRecord", []) or []:
                    target_index = record.LookupListIndex
                    if target_index < len(lookup_list) and lookup_list[target_index].LookupType == 1:
                        target_indices.add(target_index)
            if target_indices:
                result.add(lookup_index)
                result.update(target_indices)
    return result


def remove_gsub_lookups(font: TTFont, remove_indices: set[int]) -> dict[str, int]:
    if "GSUB" not in font or not font["GSUB"].table.LookupList or not remove_indices:
        return {"gsub_lookups_removed": 0}
    gsub = font["GSUB"].table
    old_lookups = gsub.LookupList.Lookup
    remove_indices = {index for index in remove_indices if 0 <= index < len(old_lookups)}
    if not remove_indices:
        return {"gsub_lookups_removed": 0}

    index_map: dict[int, int] = {}
    new_lookups = []
    for old_index, lookup in enumerate(old_lookups):
        if old_index in remove_indices:
            continue
        index_map[old_index] = len(new_lookups)
        new_lookups.append(lookup)

    if gsub.FeatureList:
        for feature_record in gsub.FeatureList.FeatureRecord:
            indices = [
                index_map[index]
                for index in list(feature_record.Feature.LookupListIndex or [])
                if index in index_map
            ]
            feature_record.Feature.LookupListIndex = indices
            feature_record.Feature.LookupCount = len(indices)

    def remap_records(container: Any) -> None:
        records = list(getattr(container, "SubstLookupRecord", []) or [])
        if records:
            kept_records = []
            for record in records:
                if record.LookupListIndex not in index_map:
                    continue
                record.LookupListIndex = index_map[record.LookupListIndex]
                kept_records.append(record)
            container.SubstLookupRecord = kept_records
            if hasattr(container, "SubstCount"):
                container.SubstCount = len(kept_records)
        for rule_set_attr in ("SubRuleSet", "ChainSubRuleSet", "SubClassSet", "ChainSubClassSet"):
            for rule_set in getattr(container, rule_set_attr, []) or []:
                if not rule_set:
                    continue
                for rule_list_attr in ("SubRule", "ChainSubRule", "SubClassRule", "ChainSubClassRule"):
                    for rule in getattr(rule_set, rule_list_attr, []) or []:
                        remap_records(rule)

    for lookup in new_lookups:
        for subtable in lookup.SubTable:
            remap_records(subtable)

    gsub.LookupList.Lookup = new_lookups
    gsub.LookupList.LookupCount = len(new_lookups)
    return {"gsub_lookups_removed": len(remove_indices)}


def drop_empty_feature_records(font: TTFont, table_tag: str, tag: str) -> dict[str, int]:
    if table_tag not in font or not font[table_tag].table.FeatureList:
        return {f"{tag}_empty_feature_records_removed": 0}
    table = font[table_tag].table
    old_records = table.FeatureList.FeatureRecord
    index_map: dict[int, int] = {}
    new_records = []
    removed = 0
    for old_index, record in enumerate(old_records):
        if record.FeatureTag == tag and not list(record.Feature.LookupListIndex or []):
            removed += 1
            continue
        index_map[old_index] = len(new_records)
        new_records.append(record)
    if not removed:
        return {f"{tag}_empty_feature_records_removed": 0}
    table.FeatureList.FeatureRecord = new_records
    table.FeatureList.FeatureCount = len(new_records)
    if table.ScriptList:
        for script_record in table.ScriptList.ScriptRecord:
            langsys_list = []
            if script_record.Script.DefaultLangSys:
                langsys_list.append(script_record.Script.DefaultLangSys)
            langsys_list.extend(record.LangSys for record in script_record.Script.LangSysRecord)
            for langsys in langsys_list:
                indices = [
                    index_map[index]
                    for index in list(langsys.FeatureIndex or [])
                    if index in index_map
                ]
                langsys.FeatureIndex = indices
                langsys.FeatureCount = len(indices)
                if getattr(langsys, "ReqFeatureIndex", 0xFFFF) != 0xFFFF:
                    langsys.ReqFeatureIndex = index_map.get(langsys.ReqFeatureIndex, 0xFFFF)
    return {f"{tag}_empty_feature_records_removed": removed}


def ensure_raised_colon_glyph(font: TTFont, colon: str, raised: str | None) -> tuple[str, int]:
    glyphs = font.getGlyphSet()
    if raised and raised in glyphs:
        return raised, 0
    raised = f"{colon}.digitsep"
    order = font.getGlyphOrder()
    if raised in order:
        return raised, 0
    font["glyf"].glyphs[raised] = copy.deepcopy(font["glyf"][colon])
    font["hmtx"].metrics[raised] = copy.deepcopy(font["hmtx"].metrics[colon])
    if "vmtx" in font and colon in font["vmtx"].metrics:
        font["vmtx"].metrics[raised] = copy.deepcopy(font["vmtx"].metrics[colon])
    if "gvar" in font:
        font["gvar"].variations[raised] = copy.deepcopy(font["gvar"].variations.get(colon, []))
    digit_names = [font.getBestCmap()[cp] for cp in range(0x30, 0x3A) if cp in font.getBestCmap()]
    digit_boxes = [glyph_bbox(font, name) for name in digit_names if glyph_bbox(font, name)]
    colon_box = glyph_bbox(font, colon)
    if digit_boxes and colon_box:
        digit_y_min = min(box[1] for box in digit_boxes if box)
        digit_y_max = max(box[3] for box in digit_boxes if box)
        digit_center = (digit_y_min + digit_y_max) / 2
        colon_center = (colon_box[1] + colon_box[3]) / 2
        shift_glyph_y(font, raised, otRound(digit_center - colon_center))
    else:
        shift_glyph_y(font, raised, 105)
    order.append(raised)
    font.setGlyphOrder(order)
    return raised, 1


def add_digit_colon_feature(font: TTFont) -> dict[str, Any]:
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    if 0x3A not in cmap or not all(cp in cmap for cp in range(0x30, 0x3A)):
        return {"digit_colon_feature_added": False, "digit_colon_raise": 0}
    colon = cmap[0x3A]
    digit_names = [cmap[cp] for cp in range(0x30, 0x3A)]
    tabular_names = tnum_digit_glyphs(font, digit_names)
    digit_names = [name for name in dict.fromkeys([*digit_names, *tabular_names]) if name in glyphs]
    if colon not in glyphs or not digit_names:
        return {"digit_colon_feature_added": False, "digit_colon_raise": 0}

    existing_raised, removed = remove_existing_calt_colon_substitutions(font, colon)
    old_colon_lookup_indices = digit_colon_calt_lookup_indices(font, colon)
    lookup_cleanup_report = remove_gsub_lookups(font, old_colon_lookup_indices)
    empty_feature_report = drop_empty_feature_records(font, "GSUB", "calt")
    raised, raised_created = ensure_raised_colon_glyph(font, colon, existing_raised)
    glyphs = font.getGlyphSet()
    colonish = [name for name in dict.fromkeys([colon, raised]) if name in glyphs]
    single_sub = ot.SingleSubst()
    single_sub.mapping = {colon: raised}
    single_lookup = ot.Lookup()
    single_lookup.LookupType = 1
    single_lookup.LookupFlag = 0
    single_lookup.SubTable = [single_sub]
    single_lookup.SubTableCount = 1
    single_index = append_gsub_lookup(font, single_lookup)

    chain_subtables = []

    def add_chain_rule(backtrack: list[list[str]], lookahead: list[list[str]]) -> None:
        subst_record = ot.SubstLookupRecord()
        subst_record.SequenceIndex = 0
        subst_record.LookupListIndex = single_index
        chain = ot.ChainContextSubst()
        chain.Format = 3
        chain.BacktrackGlyphCount = len(backtrack)
        chain.BacktrackCoverage = [coverage(font, names) for names in backtrack]
        chain.InputGlyphCount = 1
        chain.InputCoverage = [coverage(font, [colon])]
        chain.LookAheadGlyphCount = len(lookahead)
        chain.LookAheadCoverage = [coverage(font, names) for names in lookahead]
        chain.SubstCount = 1
        chain.SubstLookupRecord = [subst_record]
        chain_subtables.append(chain)

    # Inter's calt raises a single colon only between digits. Colon runs are
    # then propagated to the right once the first colon has been raised.
    add_chain_rule([digit_names], [digit_names + colonish])
    add_chain_rule([[raised]], [])
    # Inter also raises colon runs of length three or more before a digit.
    for run_tail_len in range(2, 9):
        add_chain_rule([], [colonish] * run_tail_len + [digit_names])

    chain_lookup = ot.Lookup()
    chain_lookup.LookupType = 6
    chain_lookup.LookupFlag = 0
    chain_lookup.SubTable = chain_subtables
    chain_lookup.SubTableCount = len(chain_subtables)
    chain_index = append_gsub_lookup(font, chain_lookup)
    merge_report = merge_gsub_lookup_indices_into_features(font, "calt", [chain_index])
    feature_added = False
    if merge_report["calt_features_merged"] == 0:
        append_gsub_feature(font, "calt", [chain_index])
        feature_added = True
    enable_features_for_all_scripts(font, {"calt"})
    return {
        "digit_colon_feature_added": True,
        "digit_colon_calt_feature_added": feature_added,
        **merge_report,
        "digit_colon_existing_calt_lookups_removed": len(old_colon_lookup_indices),
        **lookup_cleanup_report,
        **empty_feature_report,
        "digit_colon_existing_calt_mappings_removed": removed,
        "digit_colon_raised_glyph_created": raised_created,
        "digit_colon_context": "inter-compatible-colon-runs",
        "digit_colon_single_lookup_index": single_index,
        "digit_colon_chain_lookup_index": chain_index,
    }


def build_one_variable(region: str, italic: bool) -> dict[str, Any]:
    region = check_region(region)
    style_label = f"{region} {'italic' if italic else 'upright'}"
    unicodes = reference_unicodes(region)
    log_step(f"variable {style_label}: load sources")
    inter = load_inter(italic)
    base, sarasa_report = load_base(region, italic, set(inter.getBestCmap().keys()))
    reference_fonts: dict[int, TTFont] = {}
    try:
        for weight_name, weight_value in REFERENCE_ADVANCE_STOPS:
            reference_fonts[weight_value] = open_reference_font(region, weight_name, italic)
        log_step(f"variable {style_label}: merge outlines and layout")
        merge_report = append_inter_glyphs(base, inter, unicodes)
        remove_metric_variation_maps(base)
        feature_drop_report = drop_sarasa_width_features(base)
        locl_report = prune_locl_like_reference(base, region)
        em_dash_report = add_continuous_em_dash_feature(base)
        long_dash_report = remove_vertical_long_dash_ligature_mappings(base)
        source_nonfinal_features_dropped = drop_nonfinal_gsub_features(base, SOURCE_HAN_FINAL_GSUB_FEATURES)
        inter_layout_report = import_inter_layout_features(base, inter)
        digit_report = add_digit_width_features(base, inter)
        target_inter = load_inter(italic)
        try:
            inter_outline_report = add_inter_outline_correction_variations(base, target_inter)
        finally:
            target_inter.close()
    finally:
        inter.close()

    subset_to_current_cmap(base)
    colon_report = add_digit_colon_feature(base)
    reference = reference_fonts[400]
    skip_metric_codepoints = set(range(0x30, 0x3A)) | {0x3A}
    try:
        alias_report = split_reference_cmap_aliases(base, reference)
        alias_mapping_report = align_reference_cmap_alias_mappings(base, reference, skip_metric_codepoints)
        profile_report = split_reference_advance_profiles(base, reference_fonts, skip_metric_codepoints)
        lsb_profile_report = split_reference_lsb_profiles(base, reference_fonts, skip_metric_codepoints)
        vmtx_profile_report = split_reference_vmtx_profiles(base, reference_fonts, skip_metric_codepoints)
        vertical_vmtx_alias_report = split_reference_vertical_vmtx_aliases(base, reference_fonts, skip_metric_codepoints)
        advance_report = align_reference_advances(base, reference, skip_metric_codepoints)
        lsb_align_report = align_reference_hmtx_lsb(base, reference, skip_metric_codepoints)
        log_step(f"variable {style_label}: align exact-weight advances")
        advance_variation_report = align_reference_advance_variations(base, reference_fonts, skip_metric_codepoints)
        log_step(f"variable {style_label}: align exact-weight LSB")
        lsb_variation_report = sum_count_reports(
            align_reference_lsb_variations(base, reference_fonts, skip_metric_codepoints),
            align_reference_lsb_variations(base, reference_fonts, skip_metric_codepoints),
        )
        tnum_target_report = align_tnum_digit_targets(base, reference)
        log_step(f"variable {style_label}: align tnum exact-weight metrics")
        tnum_target_variation_report = align_tnum_digit_target_variations(base, reference_fonts)
        vmtx_report = align_reference_vmtx(base, reference, skip_metric_codepoints)
        log_step(f"variable {style_label}: align exact-weight vmtx")
        vmtx_variation_report = sum_count_reports(
            align_reference_vmtx_variations(base, reference_fonts, skip_metric_codepoints),
            align_reference_vmtx_variations(base, reference_fonts, skip_metric_codepoints),
        )
        subset_to_current_cmap(base)
        empty_feature_report = ensure_empty_gsub_features(base, empty_gsub_features_for_style(italic))
        gsub_template_report = align_layout_feature_template(base, reference, "GSUB")
        gpos_template_report = align_layout_feature_template(base, reference, "GPOS")
        gsub_lookup_report = pad_lookup_list_to_reference_count(base, reference, "GSUB")
        gpos_lookup_report = pad_lookup_list_to_reference_count(base, reference, "GPOS")
        digit_colon_merge_report = merge_gsub_lookup_indices_into_features(
            base,
            "calt",
            [colon_report["digit_colon_chain_lookup_index"]] if colon_report.get("digit_colon_feature_added") else [],
        )
        gdef_report = rebuild_gdef_from_reference(base, reference)
        vorg_report = rebuild_vorg_from_reference(base, reference)
        metadata_report = sync_sarasa_metadata_from_reference(base, reference)
    finally:
        for reference_font in reference_fonts.values():
            reference_font.close()
    update_vf_names(base, region, italic)
    update_fvar_instances(base, region, italic)
    update_style_flags(base, italic)
    update_os2_sarasa_metadata(base)
    rebuild_stat(base, italic)
    extra_table_report = drop_generated_extra_tables(base, keep_stat=True)
    if "DSIG" in base:
        del base["DSIG"]

    VARIABLE_DIR.mkdir(parents=True, exist_ok=True)
    out_name = variable_output_name(region, italic)
    out_path = VARIABLE_DIR / out_name
    log_step(f"variable {style_label}: save")
    base.save(out_path, reorderTables=True)
    base.close()

    base = TTFont(out_path)
    reference_fonts_roundtrip: dict[int, TTFont] = {}
    try:
        for weight_name, weight_value in REFERENCE_ADVANCE_STOPS:
            reference_fonts_roundtrip[weight_value] = open_reference_font(region, weight_name, italic)
        log_step(f"variable {style_label}: roundtrip LSB")
        roundtrip_lsb_variation_report = prefix_count_report(
            align_reference_lsb_variations(base, reference_fonts_roundtrip, skip_metric_codepoints),
            "roundtrip_",
        )
        log_step(f"variable {style_label}: roundtrip vmtx")
        roundtrip_vmtx_variation_report = prefix_count_report(
            align_reference_vmtx_variations(base, reference_fonts_roundtrip, skip_metric_codepoints),
            "roundtrip_",
        )
        log_step(f"variable {style_label}: roundtrip tnum")
        roundtrip_tnum_target_variation_report = prefix_count_report(
            align_tnum_digit_target_variations(base, reference_fonts_roundtrip),
            "roundtrip_",
        )
        log_step(f"variable {style_label}: save roundtrip")
        base.save(out_path, reorderTables=True)
    finally:
        for reference_font in reference_fonts_roundtrip.values():
            reference_font.close()

    cmap = base.getBestCmap()
    widths = {f"U+{cp:04X}": base["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A)}
    key_widths = {
        f"U+{cp:04X}": base["hmtx"].metrics[cmap[cp]][0]
        for cp in [0x00B7, 0x2018, 0x2019, 0x201C, 0x201D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2025, 0x2026, 0x22EF, 0x2E3A, 0x2E3B, 0x31B4, 0x3131, 0xAC00, 0x1100]
        if cp in cmap
    }
    axes = [(a.axisTag, a.minValue, a.defaultValue, a.maxValue) for a in base["fvar"].axes]
    instances = [base["name"].getDebugName(i.subfamilyNameID) for i in base["fvar"].instances]
    glyph_count = len(base.getGlyphOrder())
    base.close()

    return {
        "file": str(out_path.relative_to(ROOT)),
        "region": region,
        "source_han_region": source_han_vf_basename(region),
        "axes": axes,
        "instances": instances,
        "glyph_count": glyph_count,
        "default_digit_widths": widths,
        "key_symbol_widths": key_widths,
        **sarasa_report,
        **merge_report,
        **digit_report,
        **inter_outline_report,
        **feature_drop_report,
        **locl_report,
        **em_dash_report,
        **long_dash_report,
        "nonfinal_gsub_features_dropped": source_nonfinal_features_dropped,
        **inter_layout_report,
        **alias_report,
        **alias_mapping_report,
        **profile_report,
        **lsb_profile_report,
        **vmtx_profile_report,
        **vertical_vmtx_alias_report,
        **advance_report,
        **lsb_align_report,
        **advance_variation_report,
        **lsb_variation_report,
        **tnum_target_report,
        **tnum_target_variation_report,
        **vmtx_report,
        **vmtx_variation_report,
        **roundtrip_lsb_variation_report,
        **roundtrip_vmtx_variation_report,
        **roundtrip_tnum_target_variation_report,
        **empty_feature_report,
        **gsub_template_report,
        **gpos_template_report,
        **gsub_lookup_report,
        **gpos_lookup_report,
        **digit_colon_merge_report,
        **gdef_report,
        **vorg_report,
        **metadata_report,
        **extra_table_report,
        **colon_report,
    }


def remove_variable_tables(font: TTFont) -> None:
    for tag in ("fvar", "gvar", "avar", "HVAR", "VVAR", "MVAR", "STAT", "BASE"):
        if tag in font:
            del font[tag]


def python_script_dirs() -> list[Path]:
    candidates: list[Path] = []
    py_version_dir = f"Python{sys.version_info.major}{sys.version_info.minor}"
    for value in [
        sysconfig.get_path("scripts"),
        Path(site.USER_BASE) / ("Scripts" if platform.system().lower() == "windows" else "bin"),
        Path(site.getuserbase()) / py_version_dir / ("Scripts" if platform.system().lower() == "windows" else "bin"),
        Path(os.environ.get("APPDATA", "")) / "Python" / py_version_dir / "Scripts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / py_version_dir / "Scripts",
    ]:
        if not value:
            continue
        path = Path(value)
        if path not in candidates:
            candidates.append(path)
    return candidates


def tool_executable(env_name: str, command_name: str) -> str:
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    found = shutil.which(command_name)
    if found:
        return found
    names = [command_name]
    if platform.system().lower() == "windows":
        names.extend([f"{command_name}.exe", f"{command_name}.cmd", f"{command_name}.bat"])
    for directory in python_script_dirs():
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return command_name


def run_checked(
    cmd: list[str],
    cwd: Path | None = None,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(cmd, cwd=cwd, capture_output=capture_output, env=env)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
        stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
        raise RuntimeError(stderr or stdout or f"{cmd[0]} failed with exit code {result.returncode}")


def download_file(url: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        return path
    tmp = path.with_name(path.name + ".tmp")
    log_step(f"download {url}")
    with urllib.request.urlopen(url) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle, 1024 * 1024)
    tmp.replace(path)
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file_checked(url: str, path: Path, sha256: str) -> Path:
    if path.exists() and path.stat().st_size and file_sha256(path).lower() != sha256.lower():
        path.unlink()
    result = download_file(url, path)
    actual = file_sha256(result)
    if actual.lower() != sha256.lower():
        result.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for {result}: expected {sha256}, got {actual}")
    return result


def extract_zip_basename(archive: Path, basename: str, out_path: Path) -> Path:
    if out_path.exists():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        members = [name for name in zf.namelist() if Path(name).name == basename]
        if not members:
            raise FileNotFoundError(f"{basename} not found in {archive}")
        member = sorted(members, key=len)[0]
        log_step(f"extract {basename}")
        with zf.open(member) as src, out_path.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    return out_path


def extract_zip_first_basename(archive: Path, basenames: list[str], out_dir: Path) -> Path:
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        for basename in basenames:
            members = [name for name in names if Path(name).name == basename]
            if not members:
                continue
            out_path = out_dir / basename
            if not out_path.exists():
                out_path.parent.mkdir(parents=True, exist_ok=True)
                member = sorted(members, key=len)[0]
                log_step(f"extract {basename}")
                with zf.open(member) as src, out_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            return out_path
    raise FileNotFoundError(f"None of {basenames} found in {archive}")


def extract_7z_ttf_prefix(archive: Path, out_dir: Path, prefix: str) -> None:
    if any(out_dir.glob(f"{prefix}*.ttf")):
        return
    import py7zr

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-extract-") as tmp_name:
        tmp_dir = Path(tmp_name)
        log_step(f"extract {archive.name}")
        with py7zr.SevenZipFile(archive) as zf:
            zf.extractall(tmp_dir)
        for path in tmp_dir.rglob(f"{prefix}*.ttf"):
            shutil.copy2(path, out_dir / path.name)


def extract_7z_basename(archive: Path, basename: str, out_path: Path) -> Path:
    if out_path.exists():
        return out_path
    import py7zr

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-extract-") as tmp_name:
        tmp_dir = Path(tmp_name)
        log_step(f"extract {basename}")
        with py7zr.SevenZipFile(archive) as zf:
            members = [name for name in zf.getnames() if Path(name).name == basename]
            if not members:
                raise FileNotFoundError(f"{basename} not found in {archive}")
            zf.extract(path=tmp_dir, targets=[sorted(members, key=len)[0]])
        matches = list(tmp_dir.rglob(basename))
        if not matches:
            raise FileNotFoundError(f"{basename} not extracted from {archive}")
        shutil.copy2(matches[0], out_path)
    return out_path


def node_platform_archive() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported Node.js architecture: {platform.machine()}")
    if system == "windows":
        return "win", arch, "zip"
    if system == "linux":
        return "linux", arch, "tar.xz"
    if system == "darwin":
        return "darwin", arch, "tar.xz"
    raise RuntimeError(f"Unsupported Node.js platform: {platform.system()}")


def bundled_node_bin_dir() -> Path:
    system, arch, ext = node_platform_archive()
    folder = f"node-{NODE_VERSION}-{system}-{arch}"
    if ext == "zip":
        return NODE_DIR / folder
    return NODE_DIR / folder / "bin"


def bundled_node_executable() -> Path:
    bin_dir = bundled_node_bin_dir()
    return bin_dir / ("node.exe" if platform.system().lower() == "windows" else "node")


def bundled_npm_executable() -> Path:
    bin_dir = bundled_node_bin_dir()
    return bin_dir / ("npm.cmd" if platform.system().lower() == "windows" else "npm")


def ensure_node_runtime() -> None:
    if bundled_node_executable().exists() and bundled_npm_executable().exists():
        return
    system, arch, ext = node_platform_archive()
    archive_name = f"node-{NODE_VERSION}-{system}-{arch}.{ext}"
    archive = download_file(
        f"https://nodejs.org/dist/{NODE_VERSION}/{archive_name}",
        SOURCE_ARCHIVE_DIR / archive_name,
    )
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    log_step(f"extract {archive_name}")
    if ext == "zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(NODE_DIR)
    else:
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(NODE_DIR)


def local_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = str(bundled_node_bin_dir())
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def npm_executable() -> str:
    env_value = os.environ.get("NPM")
    if env_value:
        return env_value
    ensure_node_runtime()
    return str(bundled_npm_executable())


def extract_zip_tree(archive: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-source-") as tmp_name:
        tmp_dir = Path(tmp_name)
        log_step(f"extract {archive.name}")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp_dir)
        roots = [path for path in tmp_dir.iterdir() if path.is_dir()]
        source_root = roots[0] if len(roots) == 1 else tmp_dir
        shutil.copytree(source_root, out_dir, dirs_exist_ok=True)


def bootstrap_sarasa_source_tree() -> None:
    if shutil.which("git"):
        run_checked(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                SARASA_TAG,
                "https://github.com/be5invis/Sarasa-Gothic.git",
                str(SARASA_SOURCE_DIR),
            ],
            capture_output=False,
        )
        return
    source_zip = download_file(
        f"https://github.com/be5invis/Sarasa-Gothic/archive/refs/tags/{SARASA_TAG}.zip",
        SOURCE_ARCHIVE_DIR / f"Sarasa-Gothic-{SARASA_TAG}.zip",
    )
    extract_zip_tree(source_zip, SARASA_SOURCE_DIR)


def ensure_vf_sources(regions: list[str]) -> None:
    global INTER_ITALIC
    needed_vfs = [source_han_vf_path(region) for region in variable_regions(regions)]
    needed_classical_vfs = [
        path for path in (classical_vf_override_path(region) for region in variable_regions(regions)) if path
    ]
    if (
        all(path.exists() for path in needed_vfs)
        and all(path.exists() for path in needed_classical_vfs)
        and INTER_UPRIGHT.exists()
        and INTER_ITALIC.exists()
    ):
        return
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    source_han_zip = download_file(
        f"https://github.com/adobe-fonts/source-han-sans/releases/download/{SOURCE_HAN_TAG}/02_SourceHanSans-VF.zip",
        SOURCE_ARCHIVE_DIR / f"SourceHanSans-VF-{SOURCE_HAN_TAG}.zip",
    )
    inter_zip = download_file(
        f"https://github.com/rsms/inter/releases/download/{INTER_TAG}/Inter-4.1.zip",
        SOURCE_ARCHIVE_DIR / "Inter-4.1.zip",
    )
    for region in variable_regions(regions):
        target = source_han_vf_path(region)
        if not target.exists():
            extract_zip_basename(source_han_zip, source_han_vf_basename(region) or "", target)
    if any(not path.exists() for path in needed_classical_vfs):
        shanggu_archive = download_file_checked(
            f"https://github.com/GuiWonder/Shanggu/releases/download/{SHANGGU_TAG}/{SHANGGU_SANS_VF_ARCHIVE_NAME}",
            SOURCE_ARCHIVE_DIR / f"ShangguSansVF_TTFs-{SHANGGU_TAG}.7z",
            SHANGGU_SANS_VF_SHA256,
        )
        for target in needed_classical_vfs:
            if not target.exists():
                extract_7z_basename(shanggu_archive, target.name, target)
    extract_zip_basename(inter_zip, "InterVariable.ttf", INTER_UPRIGHT)
    if not INTER_ITALIC.exists():
        italic = extract_zip_first_basename(
            inter_zip,
            ["InterVariable-Italic.woff2", "InterVariable-Italic.ttf"],
            SRC_DIR,
        )
        INTER_ITALIC = italic


def ensure_classical_static_sources(regions: list[str]) -> None:
    needed = [
        path
        for region in regions
        if region_config(region)["classical"]
        for weight_name in STATIC_STYLE_SOURCES
        for path in [classical_static_override_path(region, weight_name)]
        if path
    ]
    if not needed or all(path.exists() for path in needed):
        return
    shanggu_archive = download_file_checked(
        f"https://github.com/GuiWonder/Shanggu/releases/download/{SHANGGU_TAG}/{SHANGGU_SANS_TTF_ARCHIVE_NAME}",
        SOURCE_ARCHIVE_DIR / f"ShangguSansTTFs-{SHANGGU_TAG}.7z",
        SHANGGU_SANS_TTF_SHA256,
    )
    for target in needed:
        if not target.exists():
            extract_7z_basename(shanggu_archive, target.name, target)


def ensure_reference_sarasa(regions: list[str]) -> None:
    global REFERENCE_SARASA, REFERENCE_SARASA_DIR, REFERENCE_SARASA_HINTED_DIR
    for region in regions:
        prefix = sarasa_region_prefix(region)
        hinted_dir = region_reference_dir(region, True)
        unhinted_dir = region_reference_dir(region, False)
        hinted_regular = hinted_dir / f"{prefix}-Regular.ttf"
        unhinted_regular = unhinted_dir / f"{prefix}-Regular.ttf"
        if not (hinted_regular.exists() and unhinted_regular.exists()):
            hinted_archive = download_file(
                f"https://github.com/be5invis/Sarasa-Gothic/releases/download/{SARASA_TAG}/{prefix}-TTF-{SARASA_VERSION}.7z",
                SOURCE_ARCHIVE_DIR / f"{prefix}-TTF-{SARASA_VERSION}.7z",
            )
            unhinted_archive = download_file(
                f"https://github.com/be5invis/Sarasa-Gothic/releases/download/{SARASA_TAG}/{prefix}-TTF-Unhinted-{SARASA_VERSION}.7z",
                SOURCE_ARCHIVE_DIR / f"{prefix}-TTF-Unhinted-{SARASA_VERSION}.7z",
            )
            extract_7z_ttf_prefix(hinted_archive, hinted_dir, f"{prefix}-")
            extract_7z_ttf_prefix(unhinted_archive, unhinted_dir, f"{prefix}-")
    REFERENCE_SARASA = reference_font_path("SC", "Regular", False)
    REFERENCE_SARASA_DIR = REFERENCE_SARASA.parent
    REFERENCE_SARASA_HINTED_DIR = region_reference_dir("SC", True)


def ensure_sarasa_source_tree() -> None:
    required = [
        SARASA_SOURCE_DIR / "sources" / "shs" / "SourceHanSans-Regular.ttc",
        SARASA_SOURCE_DIR / "sources" / "Inter" / "Inter-Regular.ttf",
        SARASA_SOURCE_DIR / "hcfg" / "Regular.json",
    ]
    if not all(path.exists() for path in required):
        if not SARASA_SOURCE_DIR.exists() or not any(SARASA_SOURCE_DIR.iterdir()):
            bootstrap_sarasa_source_tree()
        if not all(path.exists() for path in required):
            raise FileNotFoundError(
                f"Sarasa Gothic source tree at {SARASA_SOURCE_DIR} is incomplete; "
                f"expected {required[0]}, {required[1]}, and {required[2]}"
            )
    if not SARASA_CHLOROPHYTUM.exists() and os.environ.get("SARASA_SKIP_CHLOROPHYTUM") != "1":
        log_step("install Sarasa Gothic npm dependencies")
        run_checked([npm_executable(), "install"], cwd=SARASA_SOURCE_DIR, capture_output=False, env=local_runtime_env())


def ensure_build_sources(static_only: bool, regions: list[str]) -> None:
    if os.environ.get("SARASA_SKIP_SOURCE_BOOTSTRAP") == "1":
        return
    ensure_reference_sarasa(regions)
    ensure_sarasa_source_tree()
    ensure_classical_static_sources(regions)
    if not static_only:
        ensure_vf_sources(regions)


def run_ttfautohint(args: list[str]) -> str:
    exe = os.environ.get("TTFAUTOHINT") or shutil.which("ttfautohint")
    if exe:
        run_checked([exe, *args])
        return exe
    try:
        import ttfautohint

        result = ttfautohint.run(args, capture_output=True)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
            stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
            raise RuntimeError(stderr or stdout or f"ttfautohint-py failed with exit code {result.returncode}")
        return "ttfautohint-py"
    except ImportError:
        raise FileNotFoundError("ttfautohint executable or Python module is required")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def optional_file_sha256(path: Path) -> str | None:
    return file_sha256(path) if path.exists() else None


def stable_sfnt_sha256(path: Path) -> str:
    try:
        font = TTFont(path, recalcTimestamp=False)
        try:
            if "head" in font:
                font["head"].created = 0
                font["head"].modified = 0
            buffer = BytesIO()
            font.save(buffer, reorderTables=True)
            return hashlib.sha256(buffer.getvalue()).hexdigest()
        finally:
            font.close()
    except Exception:
        return file_sha256(path)


def chlorophytum_package_id() -> dict[str, Any]:
    package_dir = SARASA_SOURCE_DIR / "node_modules" / "@chlorophytum" / "cli"
    package_json = package_dir / "package.json"
    return {
        "startup": optional_file_sha256(SARASA_CHLOROPHYTUM),
        "package": optional_file_sha256(package_json),
    }


def static_fe_cache_key(weight_name: str, kanji: Path, hangul: Path) -> str:
    config_name, config_path = sarasa_hint_config(weight_name)
    payload = {
        "kind": "static-fe-chlorophytum",
        "version": 2,
        "weight": weight_name,
        "config_name": config_name,
        "config_sha256": file_sha256(config_path),
        "kanji_sha256": stable_sfnt_sha256(kanji),
        "hangul_sha256": stable_sfnt_sha256(hangul),
        "chlorophytum": chlorophytum_package_id(),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def restore_static_fe_cache(weight_name: str, kanji: Path, hangul: Path, hani_out: Path, hang_out: Path) -> dict[str, Any] | None:
    if os.environ.get("SARASA_DISABLE_BUILD_CACHE") == "1":
        return None
    key = static_fe_cache_key(weight_name, kanji, hangul)
    cache_dir = BUILD_CACHE_DIR / "static-fe" / key
    cached_hani = cache_dir / "hani.ttf"
    cached_hang = cache_dir / "hang.ttf"
    manifest = cache_dir / "manifest.json"
    if not cached_hani.exists() or not cached_hang.exists() or not manifest.exists():
        return None
    hani_out.parent.mkdir(parents=True, exist_ok=True)
    hang_out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cached_hani, hani_out)
    shutil.copy2(cached_hang, hang_out)
    _config_name, config_path = sarasa_hint_config(weight_name)
    return {
        "hani": {
            "chlorophytum_hinted": True,
            "chlorophytum_cache_hit": True,
            "chlorophytum_cache_key": key,
            "chlorophytum_hint_config": config_path.stem,
        },
        "hang": {
            "chlorophytum_hinted": True,
            "chlorophytum_cache_hit": True,
            "chlorophytum_cache_key": key,
            "chlorophytum_hint_config": config_path.stem,
        },
    }


def store_static_fe_cache(
    weight_name: str,
    kanji: Path,
    hangul: Path,
    hani_out: Path,
    hang_out: Path,
    report: dict[str, Any],
) -> None:
    if os.environ.get("SARASA_DISABLE_BUILD_CACHE") == "1":
        return
    key = static_fe_cache_key(weight_name, kanji, hangul)
    cache_dir = BUILD_CACHE_DIR / "static-fe" / key
    cache_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(hani_out, cache_dir / "hani.ttf")
    shutil.copy2(hang_out, cache_dir / "hang.ttf")
    manifest = {
        "key": key,
        "weight": weight_name,
        "created_by": "tools/build_sarasa_ui_propdigits_sc.py",
        "report": report,
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def hint_static_font(in_path: Path, out_path: Path) -> dict[str, Any]:
    if os.environ.get("SARASA_SKIP_TTFAUTOHINT") == "1":
        shutil.copy2(in_path, out_path)
        return {"hinted": False, "hint_tool": "skipped"}
    return {"hinted": True, "hint_tool": run_ttfautohint([str(in_path), str(out_path)])}


def node_executable() -> str:
    for env_name in ("SARASA_NODE", "NODE"):
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
    ensure_node_runtime()
    return str(bundled_node_executable())


def sarasa_hint_config(weight_name: str) -> tuple[str, Path]:
    config_name = SARASA_HINT_CONFIGS.get(weight_name, weight_name)
    return config_name, SARASA_SOURCE_DIR / "hcfg" / f"{config_name}.json"


def chlorophytum_hint_static_font(
    in_path: Path,
    out_path: Path,
    weight_name: str,
    tmp_dir: Path,
    hint_jobs: int | None = None,
) -> dict[str, Any]:
    return chlorophytum_hint_static_fonts([(in_path, out_path, weight_name)], tmp_dir, hint_jobs=hint_jobs)[out_path]


def chlorophytum_hint_static_fonts(
    jobs: list[tuple[Path, Path, str]],
    tmp_dir: Path,
    hint_jobs: int | None = None,
) -> dict[Path, dict[str, Any]]:
    if not jobs:
        return {}
    tmp_dir.mkdir(parents=True, exist_ok=True)
    config_names = {sarasa_hint_config(weight_name)[0] for _in_path, _out_path, weight_name in jobs}
    if len(config_names) != 1:
        raise ValueError(f"Chlorophytum batch must use one hcfg, got {sorted(config_names)}")
    config_name = next(iter(config_names))
    config_path = SARASA_SOURCE_DIR / "hcfg" / f"{config_name}.json"

    reports: dict[Path, dict[str, Any]] = {}
    if os.environ.get("SARASA_SKIP_CHLOROPHYTUM") == "1":
        for in_path, out_path, _weight_name in jobs:
            shutil.copy2(in_path, out_path)
            reports[out_path] = {
                "chlorophytum_hinted": False,
                "chlorophytum_hint_tool": "skipped",
                "chlorophytum_hint_config": config_name,
            }
        return reports
    if not SARASA_CHLOROPHYTUM.exists() or not config_path.exists():
        for in_path, out_path, _weight_name in jobs:
            shutil.copy2(in_path, out_path)
            reports[out_path] = {
                "chlorophytum_hinted": False,
                "chlorophytum_hint_tool": "missing",
                "chlorophytum_hint_config": config_name,
            }
        return reports

    actual_hint_jobs = max(1, int(hint_jobs or SARASA_HINT_JOBS))
    cache_path = tmp_dir / f"{config_name}.hc.gz"
    node = node_executable()
    hint_cmd = [
        node,
        str(SARASA_CHLOROPHYTUM),
        "hint",
        "-c",
        str(config_path),
        "-h",
        str(cache_path),
        "--jobs",
        str(actual_hint_jobs),
    ]
    hint_paths: dict[Path, Path] = {}
    for in_path, _out_path, _weight_name in jobs:
        hint_path = tmp_dir / f"{in_path.stem}.hint.gz"
        hint_paths[in_path] = hint_path
        hint_cmd.extend([str(in_path), str(hint_path)])
    verbose = os.environ.get("SARASA_CHLOROPHYTUM_VERBOSE") == "1"
    result = subprocess.run(hint_cmd, cwd=SARASA_SOURCE_DIR, capture_output=not verbose)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
        stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
        raise RuntimeError(stderr or stdout or f"Chlorophytum hint failed with exit code {result.returncode}")

    instruct_cmd = [
        node,
        str(SARASA_CHLOROPHYTUM),
        "instruct",
        "-c",
        str(config_path),
    ]
    for in_path, out_path, _weight_name in jobs:
        instruct_cmd.extend([str(in_path), str(hint_paths[in_path]), str(out_path)])
    result = subprocess.run(instruct_cmd, cwd=SARASA_SOURCE_DIR, capture_output=not verbose)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
        stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
        raise RuntimeError(stderr or stdout or f"Chlorophytum instruct failed with exit code {result.returncode}")

    for _in_path, out_path, _weight_name in jobs:
        reports[out_path] = {
            "chlorophytum_hinted": True,
            "chlorophytum_hint_tool": str(SARASA_CHLOROPHYTUM),
            "chlorophytum_hint_config": config_name,
            "chlorophytum_hint_jobs": actual_hint_jobs,
            "chlorophytum_hint_group_size": len(jobs),
            "chlorophytum_hint_cache": str(cache_path),
        }
    return reports


def sarasa_style_name(weight_name: str, italic: bool) -> str:
    style = str(STATIC_STYLE_SOURCES[weight_name]["sarasa"])
    if not italic:
        return style
    if style == "Regular":
        return "Italic"
    return f"{style}Italic"


def sarasa_ui_flags() -> dict[str, bool]:
    return {
        "goth": False,
        "mono": False,
        "pwid": True,
        "tnum": True,
        "term": False,
    }


def sarasa_latin_config() -> dict[str, Any]:
    return {
        "bakeFeatures": [{"tag": "ss03"}, {"tag": "cv10"}],
        "dropFeatures": [
            "cv01",
            "cv02",
            "cv03",
            "cv04",
            "cv05",
            "cv06",
            "cv07",
            "cv08",
            "cv09",
            "cv10",
            "cv11",
            "cv12",
            "cv13",
            "ss01",
            "ss02",
            "ss03",
            "ss04",
            "ss05",
            "ss06",
            "ss07",
            "ss08",
        ],
    }


def sarasa_module_runner(tmp_dir: Path) -> Path:
    runner = tmp_dir / "run-sarasa-module.mjs"
    if not runner.exists():
        runner.write_text(
            "\n".join(
                [
                    'import { pathToFileURL } from "node:url";',
                    "const recipe = process.argv[2];",
                    "const args = JSON.parse(process.argv[3]);",
                    "const mod = await import(pathToFileURL(recipe).href);",
                    "await mod.default(args);",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return runner


def run_sarasa_module(tmp_dir: Path, recipe: str, args: dict[str, Any]) -> None:
    runner = sarasa_module_runner(tmp_dir)
    cmd = [
        node_executable(),
        str(runner),
        str(SARASA_SOURCE_DIR / recipe),
        json.dumps(args, ensure_ascii=False),
    ]
    run_checked(cmd, cwd=SARASA_SOURCE_DIR)


def otc2otf_executable() -> str:
    return tool_executable("OTC2OTF", "otc2otf")


def otf2ttf_executable() -> str:
    return tool_executable("OTF2TTF", "otf2ttf")


def build_shs_ttf(region: str, weight_name: str, tmp_dir: Path) -> Path:
    region = check_region(region)
    source = STATIC_STYLE_SOURCES[weight_name]
    shs_weight = str(source["shs"])
    out_dir = tmp_dir / "shs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_ttf = out_dir / f"{region}-{shs_weight}.ttf"
    if out_ttf.exists():
        return out_ttf

    source_ttc = SARASA_SOURCE_DIR / "sources" / "shs" / f"SourceHanSans-{shs_weight}.ttc"
    if not source_ttc.exists():
        raise FileNotFoundError(source_ttc)
    extract_dir = tmp_dir / "shs-extract" / shs_weight
    extract_dir.mkdir(parents=True, exist_ok=True)
    copied_ttc = extract_dir / source_ttc.name
    if not copied_ttc.exists():
        shutil.copy2(source_ttc, copied_ttc)
    shs_prefix = source_han_static_prefix(region)
    expected_otf = extract_dir / f"{shs_prefix}-{shs_weight}.otf"
    if not expected_otf.exists():
        run_checked([otc2otf_executable(), str(copied_ttc)], cwd=extract_dir)
    if not expected_otf.exists():
        candidates = list(extract_dir.rglob(f"{shs_prefix}-{shs_weight}.otf"))
        if candidates:
            expected_otf = candidates[0]
    if not expected_otf.exists():
        raise FileNotFoundError(expected_otf)
    if out_ttf.exists():
        out_ttf.unlink()
    run_checked([otf2ttf_executable(), "-o", str(out_ttf), str(expected_otf)])
    return out_ttf


def build_classical_override_ttf(region: str, weight_name: str, tmp_dir: Path) -> Path:
    override_ttf = classical_static_override_path(region, weight_name)
    if not override_ttf:
        raise ValueError(f"region {region} has no classical static override")
    if not override_ttf.exists():
        ensure_classical_static_sources([region])
    if not override_ttf.exists():
        raise FileNotFoundError(override_ttf)
    return override_ttf


def inter_source_style(weight_name: str, italic: bool) -> str | None:
    source = STATIC_STYLE_SOURCES[weight_name]
    inter_style = source.get("inter")
    if inter_style is None:
        return None
    inter_style = str(inter_style)
    if italic:
        if inter_style == "Regular":
            return "Italic"
        return f"{inter_style}Italic"
    return inter_style


def build_inter_source(weight_name: str, weight_value: int, italic: bool, tmp_dir: Path) -> Path:
    out_dir = tmp_dir / "inter"
    out_dir.mkdir(parents=True, exist_ok=True)
    style = inter_source_style(weight_name, italic)
    if style:
        raw_source = SARASA_SOURCE_DIR / "sources" / "Inter" / f"Inter-{style}.ttf"
        if not raw_source.exists():
            raise FileNotFoundError(raw_source)
        raw_path = raw_source
        out_path = out_dir / f"Inter-{style}.dehint.ttf"
    else:
        suffix = "Italic" if italic else ""
        raw_path = out_dir / f"Inter-{weight_name}{suffix}.vf-instance.ttf"
        out_path = out_dir / f"Inter-{weight_name}{suffix}.dehint.ttf"
        if not raw_path.exists():
            inter = TTFont(INTER_ITALIC if italic else INTER_UPRIGHT)
            inter = instantiateVariableFont(inter, {"opsz": 14, "wght": weight_value}, inplace=False, optimize=True)
            try:
                remove_variable_tables(inter)
                inter.flavor = None
                inter.save(raw_path, reorderTables=True)
            finally:
                inter.close()
    if not out_path.exists():
        run_ttfautohint(["-d", str(raw_path), str(out_path)])
    return out_path


def build_sarasa_static_fragments(
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    tmp_dir: Path,
) -> dict[str, Any]:
    region = check_region(region)
    suffix = f"{weight_name}{'Italic' if italic else ''}"
    fe_dir = tmp_dir / "fragments" / f"{region}-{weight_name}-fe"
    work_dir = tmp_dir / "fragments" / f"{region}-{suffix}"
    fe_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    shs_ttf = build_shs_ttf(region, weight_name, tmp_dir)
    inter_ttf = build_inter_source(weight_name, weight_value, italic, tmp_dir)
    style_name = sarasa_style_name(weight_name, italic)
    flags = sarasa_ui_flags()

    kanji = fe_dir / "kanji0.ttf"
    hangul = fe_dir / "hangul0.ttf"
    non_kanji = fe_dir / "non-kanji0.ttf"
    ws = work_dir / "ws0.ttf"
    as_punct = work_dir / "as0.ttf"
    fe_misc = work_dir / "fe-misc0.ttf"
    pass1 = work_dir / "pass1.ttf"

    if not kanji.exists():
        classical_override = build_classical_override_ttf(region, weight_name, tmp_dir) if region_config(region)["classical"] else None
        run_sarasa_module(
            tmp_dir,
            "make/kanji/build.mjs",
            {"main": str(shs_ttf), "classicalOverride": str(classical_override) if classical_override else None, "o": str(kanji)},
        )
    if not hangul.exists():
        run_sarasa_module(tmp_dir, "make/hangul/build.mjs", {"main": str(shs_ttf), "o": str(hangul)})
    if not non_kanji.exists():
        run_sarasa_module(tmp_dir, "make/non-kanji/build.mjs", {"main": str(shs_ttf), "o": str(non_kanji)})

    punct_args = {
        "family": "Ui",
        "region": region,
        "style": style_name,
        "main": str(non_kanji),
        "lgc": str(inter_ttf),
        **flags,
    }
    if not ws.exists():
        run_sarasa_module(tmp_dir, "make/punct/ws.mjs", {**punct_args, "o": str(ws)})
    if not as_punct.exists():
        run_sarasa_module(tmp_dir, "make/punct/as.mjs", {**punct_args, "o": str(as_punct)})
    if not fe_misc.exists():
        run_sarasa_module(tmp_dir, "make/punct/fe-misc.mjs", {**punct_args, "o": str(fe_misc)})

    if not pass1.exists():
        run_sarasa_module(
            tmp_dir,
            "make/pass1/index.mjs",
            {
                "main": str(inter_ttf),
                "as": str(as_punct),
                "ws": str(ws),
                "feMisc": str(fe_misc),
                "o": str(pass1),
                "family": "Ui",
                "subfamily": region,
                "style": style_name,
                "italize": italic,
                "version": "1.0.39",
                "latinCfg": sarasa_latin_config(),
                **flags,
            },
        )
    return {
        "pass1": pass1,
        "kanji": kanji,
        "hangul": hangul,
        "region": region,
        "sarasa_static_style": style_name,
        "sarasa_source_han_style": str(STATIC_STYLE_SOURCES[weight_name]["shs"]),
        "sarasa_source_han_region": source_han_static_prefix(region),
        "sarasa_inter_style": inter_source_style(weight_name, italic) or f"VF-{weight_value}{'Italic' if italic else ''}",
    }


def build_sarasa_pass2(
    pass1: Path,
    kanji: Path,
    hangul: Path,
    out_path: Path,
    italic: bool,
    tmp_dir: Path,
) -> None:
    run_sarasa_module(
        tmp_dir,
        "make/pass2/index.mjs",
        {
            "main": str(pass1),
            "kanji": str(kanji),
            "hangul": str(hangul),
            "o": str(out_path),
            "italize": italic,
        },
    )


def apply_static_propdigits(font: TTFont) -> dict[str, int]:
    pnum = get_single_substitution_mapping(font, "pnum")
    if not pnum or "cmap" not in font:
        return {"static_propdigit_cmap_remaps": 0}
    default_cmap = font.getBestCmap()
    remap: dict[int, str] = {}
    for codepoint in [*range(0x30, 0x3A), 0x3A]:
        glyph_name = default_cmap.get(codepoint)
        target = pnum.get(glyph_name or "")
        if target and target in font.getGlyphSet():
            remap[codepoint] = target
    touched = 0
    for cmap_table in font["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint, target in remap.items():
            if cmap_table.cmap.get(codepoint) != target:
                cmap_table.cmap[codepoint] = target
                touched += 1
    return {"static_propdigit_cmap_remaps": touched}


def apply_static_digit_glyph_names(font: TTFont) -> dict[str, int]:
    if "glyf" not in font or "hmtx" not in font:
        return {"static_digit_glyphs_renamed": 0, "static_post_format2_names": 0}

    cmap = font.getBestCmap()
    tnum = get_single_substitution_mapping(font, "tnum")
    glyph_set = set(font.getGlyphOrder())
    order = font.getGlyphOrder()
    rename: dict[str, str] = {}
    for offset, digit_name in enumerate(DIGITS):
        codepoint = 0x30 + offset
        proportional = cmap.get(codepoint)
        if not proportional or proportional not in glyph_set:
            continue
        tabular = tnum.get(proportional) or tnum.get(digit_name)
        proportional_name = f"glyph{order.index(proportional):05d}"
        rename[proportional] = proportional_name
        if tabular and tabular in glyph_set:
            rename[tabular] = digit_name

    renamed = rename_glyphs(font, rename)
    post_format2 = 0
    if "post" in font:
        post = font["post"]
        if getattr(post, "formatType", None) != 2.0:
            post_format2 = 1
        post.formatType = 2.0
        post.extraNames = []
        post.mapping = {}
    return {"static_digit_glyphs_renamed": renamed, "static_post_format2_names": post_format2}


def static_output_name(region: str, weight_name: str, italic: bool) -> str:
    prefix = static_file_prefix(region)
    if weight_name == "Regular":
        return f"{prefix}-Italic.ttf" if italic else f"{prefix}-Regular.ttf"
    return f"{prefix}-{weight_name}{'Italic' if italic else ''}.ttf"


def static_weight_output_paths(region: str, weight_name: str) -> list[tuple[Path, bool, bool]]:
    return [
        (static_dir(region, hinted) / static_output_name(region, weight_name, italic), hinted, italic)
        for hinted in [False, True]
        for italic in [False, True]
    ]


def static_weight_complete(region: str, weight_name: str) -> bool:
    return all(path.exists() for path, _hinted, _italic in static_weight_output_paths(region, weight_name))


def skipped_static_weight_outputs(region: str, stop: dict[str, Any]) -> list[dict[str, Any]]:
    weight_name = str(stop["name"])
    weight_value = int(stop["value"])
    return [
        {
            "file": str(path.relative_to(ROOT)),
            "region": region,
            "weight": weight_name,
            "wght": weight_value,
            "italic": italic,
            "hinted_variant": hinted,
            "rebuilt": False,
            "resume_static_skipped": True,
        }
        for path, hinted, italic in static_weight_output_paths(region, weight_name)
    ]


def postprocess_static_font(
    path: Path,
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    hinted: bool,
) -> dict[str, Any]:
    font = TTFont(path, recalcBBoxes=False, recalcTimestamp=False)
    font.recalcBBoxes = False
    report: dict[str, Any] = {}
    try:
        reference_path = reference_font_path(region, weight_name, italic)
        reference: TTFont | None = None
        if reference_path.exists():
            reference = TTFont(reference_path, recalcBBoxes=False, recalcTimestamp=False)
            try:
                report.update(align_reference_hmtx_lsb(font, reference, PROPDIGITS_CODEPOINTS))
                report.update(align_tnum_digit_targets(font, reference))
                report.update(align_reference_vmtx(font, reference, PROPDIGITS_CODEPOINTS))
                report.update(rebuild_gdef_from_reference(font, reference))
                report.update(rebuild_vorg_from_reference(font, reference))
                report.update(sync_sarasa_metadata_from_reference(font, reference))
            except Exception:
                reference.close()
                reference = None
                raise
        update_static_names(font, region, weight_name, weight_value, italic)
        update_os2_sarasa_metadata(font)
        rebuild_static_stat(font, weight_name, weight_value, italic)
        report.update(drop_generated_extra_tables(font, keep_stat=True))
        report.update(apply_static_propdigits(font))
        report.update(apply_static_digit_glyph_names(font))
        if reference:
            report.update(sync_static_glyf_from_reference(font, reference, PROPDIGITS_CODEPOINTS))
        report.update(add_digit_colon_feature(font))
        if hinted:
            report.update(
                {
                    "hint_tables_synced": 0,
                    "hint_glyph_programs_synced": 0,
                    "hint_glyph_programs_skipped": 0,
                    "hint_reference_sync": "skipped-rehinted-output",
                }
            )
        report["digit_colon_source"] = "inter-compatible-calt"
        if "DSIG" in font:
            del font["DSIG"]
        report.update(force_recompile_glyf(font))
        font.save(path, reorderTables=True)
    finally:
        if reference:
            reference.close()
        font.close()
    return report


def prepare_static_pass1_derivatives(path: Path) -> dict[str, Any]:
    return {
        "pass1_digit_colon_feature_added": False,
        "pass1_digit_colon_source": "inter-calt",
    }


def build_static_weight(
    region: str,
    stop: dict[str, Any],
    tmp_dir: Path,
    hint_jobs: int,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    weight_name = str(stop["name"])
    weight_value = int(stop["value"])
    hinted_fe_entry: dict[str, Any] | None = None
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for italic in [False, True]:
        style_label = f"{region} {weight_name}{' Italic' if italic else ''}"
        log_step(f"static {style_label}: build Sarasa fragments")
        fragments = build_sarasa_static_fragments(region, weight_name, weight_value, italic, tmp_dir)
        pass1_derivative_report = prepare_static_pass1_derivatives(fragments["pass1"])

        unhinted_tmp = tmp_dir / "unhinted" / region / static_output_name(region, weight_name, italic)
        unhinted_tmp.parent.mkdir(parents=True, exist_ok=True)
        unhinted_path = static_dir(region, False) / static_output_name(region, weight_name, italic)
        log_step(f"static {style_label}: compose unhinted pass2")
        build_sarasa_pass2(
            fragments["pass1"],
            fragments["kanji"],
            fragments["hangul"],
            unhinted_tmp,
            italic,
            tmp_dir,
        )
        log_step(f"static {style_label}: postprocess unhinted")
        unhinted_report = postprocess_static_font(unhinted_tmp, region, weight_name, weight_value, italic, False)
        shutil.copy2(unhinted_tmp, unhinted_path)
        outputs.append(
            {
                "file": str(unhinted_path.relative_to(ROOT)),
                "region": region,
                "weight": weight_name,
                "wght": weight_value,
                "italic": italic,
                "hinted_variant": False,
                "source_static_build": "sarasa-pass1-kanji-hangul-pass2",
                "hinted": False,
                "hint_tool": "unhinted",
                "chlorophytum_hinted": False,
                **{k: v for k, v in fragments.items() if isinstance(v, str)},
                **pass1_derivative_report,
                **unhinted_report,
            }
        )

        hinted_work = tmp_dir / "hinted" / region / f"{weight_name}{'Italic' if italic else ''}"
        hinted_work.mkdir(parents=True, exist_ok=True)
        pass1_hinted = hinted_work / "pass1.ttfautohint.ttf"
        pass1_instructed = hinted_work / "pass1.ttf"
        hinted_tmp = hinted_work / static_output_name(region, weight_name, italic)
        hinted_path = static_dir(region, True) / static_output_name(region, weight_name, italic)

        log_step(f"static {style_label}: ttfautohint pass1")
        hint_report = hint_static_font(fragments["pass1"], pass1_hinted)
        log_step(f"static {style_label}: Chlorophytum pass1")
        pass1_chlorophytum = chlorophytum_hint_static_fonts(
            [(pass1_hinted, pass1_instructed, weight_name)],
            hinted_work / "pass1-hints",
            hint_jobs=hint_jobs,
        )[pass1_instructed]

        if hinted_fe_entry is None:
            fe_work = tmp_dir / "hinted-fe" / region / weight_name
            fe_work.mkdir(parents=True, exist_ok=True)
            hani_instructed = fe_work / "hani.ttf"
            hang_instructed = fe_work / "hang.ttf"
            log_step(f"static {style_label}: restore cached kanji/hangul")
            fe_chlorophytum_report = restore_static_fe_cache(
                weight_name,
                fragments["kanji"],
                fragments["hangul"],
                hani_instructed,
                hang_instructed,
            )
            if fe_chlorophytum_report is None:
                log_step(f"static {style_label}: Chlorophytum kanji/hangul")
                fe_chlorophytum = chlorophytum_hint_static_fonts(
                    [
                        (fragments["kanji"], hani_instructed, weight_name),
                        (fragments["hangul"], hang_instructed, weight_name),
                    ],
                    fe_work / "hints",
                    hint_jobs=hint_jobs,
                )
                fe_chlorophytum_report = {
                    "hani": fe_chlorophytum[hani_instructed],
                    "hang": fe_chlorophytum[hang_instructed],
                }
                store_static_fe_cache(
                    weight_name,
                    fragments["kanji"],
                    fragments["hangul"],
                    hani_instructed,
                    hang_instructed,
                    fe_chlorophytum_report,
                )
            else:
                log_step(f"static {style_label}: cached kanji/hangul hit")
            hinted_fe_entry = {
                "hani": hani_instructed,
                "hang": hang_instructed,
                "report": fe_chlorophytum_report,
            }
        else:
            log_step(f"static {style_label}: reuse Chlorophytum kanji/hangul")

        hani_instructed = hinted_fe_entry["hani"]
        hang_instructed = hinted_fe_entry["hang"]
        fe_chlorophytum_report = hinted_fe_entry["report"]
        log_step(f"static {style_label}: compose hinted pass2")
        build_sarasa_pass2(pass1_instructed, hani_instructed, hang_instructed, hinted_tmp, italic, tmp_dir)
        log_step(f"static {style_label}: postprocess hinted")
        hinted_postprocess = postprocess_static_font(hinted_tmp, region, weight_name, weight_value, italic, True)
        shutil.copy2(hinted_tmp, hinted_path)
        outputs.append(
            {
                "file": str(hinted_path.relative_to(ROOT)),
                "region": region,
                "weight": weight_name,
                "wght": weight_value,
                "italic": italic,
                "hinted_variant": True,
                "source_static_build": "sarasa-pass1-kanji-hangul-pass2",
                **{k: v for k, v in fragments.items() if isinstance(v, str)},
                **pass1_derivative_report,
                **hint_report,
                "hinted": True,
                "hint_tool": "ttfautohint-plus-chlorophytum",
                "chlorophytum_hinted": True,
                "pass1_chlorophytum": pass1_chlorophytum,
                "fe_chlorophytum": fe_chlorophytum_report,
                **hinted_postprocess,
            }
        )
    return outputs


def build_static_fonts(regions: list[str], resume: bool = False) -> list[dict[str, Any]]:
    log_step("static: prepare output directories")
    for region in regions:
        for out_dir in [static_dir(region, True), static_dir(region, False)]:
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "LICENSE", out_dir / "LICENSE-Sarasa-Gothic.txt")
            if not resume:
                for path in out_dir.glob(f"{static_file_prefix(region)}-*.ttf"):
                    path.unlink()

    outputs: list[dict[str, Any]] = []
    hint_jobs = SARASA_HINT_JOBS
    log_step(f"static: Chlorophytum jobs={hint_jobs}")
    weight_order = {str(stop["name"]): index for index, stop in enumerate(SOURCE_HAN_WEIGHT_STOPS)}
    region_order = {region: index for index, region in enumerate(regions)}
    with tempfile.TemporaryDirectory() as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        for region in regions:
            region_tmp = tmp_dir / "weights" / region
            stops_to_build: list[dict[str, Any]] = []
            for stop in SOURCE_HAN_WEIGHT_STOPS:
                weight_name = str(stop["name"])
                if resume and static_weight_complete(region, weight_name):
                    log_step(f"static {region} {weight_name}: skip existing complete weight")
                    outputs.extend(skipped_static_weight_outputs(region, stop))
                else:
                    stops_to_build.append(stop)
            for stop in stops_to_build:
                outputs.extend(build_static_weight(region, stop, region_tmp / str(stop["name"]), hint_jobs))
    outputs.sort(
        key=lambda item: (
            region_order.get(str(item.get("region")), 999),
            weight_order.get(str(item.get("weight")), 999),
            bool(item.get("italic")),
            bool(item.get("hinted_variant")),
        )
    )
    return outputs


def font_name(font: TTFont, name_id: int) -> str | None:
    name = font["name"].getName(name_id, 3, 1, 0x409) or font["name"].getName(name_id, 1, 0, 0)
    return name.toUnicode() if name else None


def has_feature(font: TTFont, tag: str) -> bool:
    return "GSUB" in font and font["GSUB"].table.FeatureList and any(
        record.FeatureTag == tag for record in font["GSUB"].table.FeatureList.FeatureRecord
    )


def layout_table_summary(font: TTFont, table_tag: str) -> dict[str, Any]:
    if table_tag not in font:
        return {"present": False}
    table = font[table_tag].table
    feature_records = table.FeatureList.FeatureRecord if table.FeatureList else []
    lookup_count = len(table.LookupList.Lookup) if table.LookupList else 0
    scripts = []
    langsys_count = 0
    if table.ScriptList:
        for script_record in table.ScriptList.ScriptRecord:
            langs = [record.LangSysTag for record in script_record.Script.LangSysRecord]
            langsys_count += len(langs)
            if script_record.Script.DefaultLangSys:
                langsys_count += 1
            scripts.append(
                {
                    "tag": script_record.ScriptTag,
                    "has_default": script_record.Script.DefaultLangSys is not None,
                    "langs": langs,
                }
            )
    return {
        "present": True,
        "feature_records": len(feature_records),
        "unique_features": sorted({record.FeatureTag for record in feature_records}),
        "lookups": lookup_count,
        "scripts": scripts,
        "langsys": langsys_count,
    }


def shape_glyph_names(path: Path, text: str, script: str | None = None, language: str | None = None) -> list[str] | None:
    try:
        import uharfbuzz as hb
    except ImportError:
        return None
    data = path.read_bytes()
    face = hb.Face(data)
    hb_font = hb.Font(face)
    hb_font.scale = (face.upem, face.upem)
    buffer = hb.Buffer()
    buffer.add_str(text)
    if script:
        buffer.script = script
    if language:
        buffer.language = language
    buffer.guess_segment_properties()
    hb.shape(hb_font, buffer, {"calt": True})
    font = TTFont(path)
    try:
        glyph_order = font.getGlyphOrder()
        return [glyph_order[info.codepoint] for info in buffer.glyph_infos]
    finally:
        font.close()


def lsb_mismatch_count(font: TTFont) -> int | None:
    if "hmtx" not in font or "glyf" not in font:
        return None
    mismatches = 0
    for glyph_name, (_advance_width, lsb) in font["hmtx"].metrics.items():
        if glyph_name not in font["glyf"].glyphs:
            continue
        if glyph_x_min(font, glyph_name, lsb) != lsb:
            mismatches += 1
    return mismatches


def inspect_font(path: Path) -> dict[str, Any]:
    font = TTFont(path)
    try:
        cmap = font.getBestCmap()
        digits = [font["hmtx"].metrics[cmap[cp]][0] for cp in range(0x30, 0x3A) if cp in cmap]
        key_cps = [0x00B7, 0x2018, 0x2019, 0x201C, 0x201D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2025, 0x2026, 0x22EF, 0x2E3A, 0x2E3B, 0x31B4, 0x3131, 0xAC00, 0x1100]
        key_widths = {f"U+{cp:04X}": font["hmtx"].metrics[cmap[cp]][0] for cp in key_cps if cp in cmap}
        axes = []
        instances = []
        if "fvar" in font:
            axes = [
                {"tag": axis.axisTag, "min": axis.minValue, "default": axis.defaultValue, "max": axis.maxValue}
                for axis in font["fvar"].axes
            ]
            instances = [
                {
                    "name": font["name"].getDebugName(instance.subfamilyNameID),
                    "coordinates": instance.coordinates,
                    "postscript": font["name"].getDebugName(instance.postscriptNameID)
                    if instance.postscriptNameID != 0xFFFF
                    else None,
                }
                for instance in font["fvar"].instances
            ]
        return {
            "file": str(path.relative_to(ROOT)),
            "size": path.stat().st_size,
            "names": {
                "family": font_name(font, 1),
                "subfamily": font_name(font, 2),
                "full": font_name(font, 4),
                "postscript": font_name(font, 6),
                "typographic_family": font_name(font, 16),
                "typographic_subfamily": font_name(font, 17),
            },
            "glyph_count": len(font.getGlyphOrder()),
            "post_format": font["post"].formatType if "post" in font else None,
            "digit_widths_u0030_to_u0039": digits,
            "key_symbol_widths": key_widths,
            "has_tnum": has_feature(font, "tnum"),
            "has_pnum": has_feature(font, "pnum"),
            "has_digit_colon_calt": has_feature(font, "calt"),
            "has_hints": any(tag in font for tag in ("fpgm", "prep", "cvt ")),
            "glyf_overlap_simple_flags": count_simple_glyph_overlap_flags(font),
            "shape_1_colon_2": {
                "default": shape_glyph_names(path, "1:2"),
                "latn": shape_glyph_names(path, "1:2", "Latn"),
                "hani_zhs": shape_glyph_names(path, "1:2", "Hani", "ZHS"),
            },
            "tables": {
                "BASE": "BASE" in font,
                "GDEF": "GDEF" in font,
                "STAT": "STAT" in font,
                "VORG": "VORG" in font,
                "fvar": "fvar" in font,
                "gvar": "gvar" in font,
            },
            "layout": {
                "GSUB": layout_table_summary(font, "GSUB"),
                "GPOS": layout_table_summary(font, "GPOS"),
            },
            "fvar_axes": axes,
            "fvar_instances": instances,
            "usWeightClass": font["OS/2"].usWeightClass,
            "fsSelection": font["OS/2"].fsSelection,
            "vendor": font["OS/2"].achVendID,
            "codepage_range_1": font["OS/2"].ulCodePageRange1,
            "codepage_range_2": font["OS/2"].ulCodePageRange2,
        }
    finally:
        font.close()


def static_readme_text(region: str, hinted: bool) -> str:
    family = static_family(region)
    family_local = static_family_local(region)
    shs_prefix = source_han_static_prefix(region)
    title = f"{family} TTF {SARASA_VERSION}" if hinted else f"{family} TTF Unhinted {SARASA_VERSION}"
    cl_note = (
        f"CL 地区的传统旧字形覆盖跟随 Shanggu Sans {SHANGGU_TAG} 官方 TTF：\n"
        "汉字底稿先取 SourceHanSansK，再用 ShangguSansTC 静态 TTF 覆盖。"
        if region == "CL"
        else f"{region} 地区沿用 Sarasa 上游路径：CJK 底稿来自 {shs_prefix}。"
    )
    hint_note = (
        "hinted 套件会对本项目实际生成的静态片段重新 hint：pass1 先经过\n"
        "ttfautohint，随后 pass1/kanji/hangul 片段用 Sarasa 上游 Chlorophytum\n"
        "hcfg 流程写入 TrueType instructions，最后由 pass2 合成最终 TTF。\n"
        "Normal、Medium、Heavy 这类项目扩展字重也按当前轮廓重新生成 hint，\n"
        "不会冒充官方 Sarasa 已发布静态字重。静态 PropDigits 会把 ':' remap\n"
        "到已有的 pnum glyph，移除旧的冒号上下文替换，再追加与 Inter 一致的\n"
        "colon-run calt 规则。"
        if hinted
        else "unhinted 套件同样沿用上游 Sarasa 的静态片段构建路径，但直接用\n"
        "未 hint 的 pass1/kanji/hangul 片段进入 pass2。它会跳过\n"
        "ttfautohint 和 Chlorophytum，提供正式的无 TrueType instructions\n"
        "静态输出。"
    )
    return f"""{title}

本目录包含静态 TrueType 字体。这些字体从静态 {shs_prefix} 和
Inter 源字体出发，经 Sarasa 的 pass1/kanji/hangul/pass2 构建路径生成，
然后补上 PropDigits 派生行为。

{cl_note}

字重：

- ExtraLight 200
- Light 300
- Normal 350
- Regular 400
- Medium 500
- Bold 700
- Heavy 900

公开字重采用 Sarasa/CSS 口径：ExtraLight 是 200。CJK 轮廓来源仍是
Source Han Sans 的 ExtraLight 口径 250；VF 通过轴映射让 public
wght=200 对应 Source Han 内部 wght=250，而 Inter 对应 wght=200。

每个字重都包含正体和 Italic 文件。ASCII 数字默认使用比例宽度；
OpenType tnum 会恢复等宽数字，pnum 会把等宽数字切回比例数字。
静态 TTF 与 VF 使用一致的、与 Inter 兼容的 calt 冒号行为：
1:2 会上浮 ':'，1:a 和 a:2 不会上浮，1::2 等连续冒号上下文遵循
Inter 的 colon-run 规则。

name 表包含地区本地化显示名，例如：
{family_local} ExtraLight.
OS/2.achVendID 使用本派生项目的 MRDK，不继承上游 Sarasa Ui 的
???? 占位值。
{hint_note}
静态 TTF 保留静态 STAT 表，供现代应用识别 weight/italic 样式；这不会让
静态 TTF 变成可变字体。GSUB/GPOS 的 FeatureRecord 顺序、Script/LangSys
覆盖和基础 lookup 结构按对应样式的上游 Sarasa Ui {region} 静态字体套模板。
对于 exact 静态样式，非数字/非冒号码位会保留上游 simple glyph flags、
glyf bbox 和组合字形组件名。静态 TTF 使用 post format 2，让默认比例数字
remap 到 U+0030..U+0039 后，相关 glyph names 仍能稳定保留。最终写出 glyf
时保留上游 OVERLAP_SIMPLE 语义，并用 OTS 可接受的 repeat 编码保存重复
overlap flags，而不是清除 bit 6。unhinted 套件中的 OTS maxZones/gasp 警告
继承自上游 unhinted 基线，返回码为 0。
glyph 总数不强行补齐到与上游一致；cmap 字形和布局可达的未编码字形会保留，
不可达 glyph 数量差异视为构建产物。
这些字体是修改派生版，不是 Sarasa Gothic、Source Han Sans 或 Inter 的官方发布。
"""


def write_static_readme(regions: list[str]) -> None:
    for region in regions:
        hinted_dir = static_dir(region, True)
        unhinted_dir = static_dir(region, False)
        hinted_dir.mkdir(parents=True, exist_ok=True)
        unhinted_dir.mkdir(parents=True, exist_ok=True)
        (hinted_dir / "README.txt").write_text(static_readme_text(region, True), encoding="utf-8")
        (unhinted_dir / "README.txt").write_text(static_readme_text(region, False), encoding="utf-8")


def write_reports(build_report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    build_text = json.dumps(build_report, ensure_ascii=False, indent=2)
    (REPORT_DIR / "Sarasa-Ui-PropDigits-report.json").write_text(build_text, encoding="utf-8")
    legacy_report = REPORT_DIR / "Sarasa-Ui-VF-PropDigits-SC-report.json"
    if legacy_report.exists():
        legacy_report.unlink()

    font_paths = (
        sorted(VARIABLE_DIR.glob("*.ttf"))
        + sorted(STATIC_ROOT.glob(f"SarasaUiPropDigits*-TTF-{SARASA_VERSION}/*.ttf"))
        + sorted(STATIC_ROOT.glob(f"SarasaUiPropDigits*-TTF-Unhinted-{SARASA_VERSION}/*.ttf"))
    )
    inspection = {
        "title": "Sarasa Ui VF PropDigits / Sarasa Ui PropDigits 多地区字体检查",
        "note": "由 tools/build_sarasa_ui_propdigits_sc.py 使用 fontTools 生成。",
        "fonts": [inspect_font(path) for path in font_paths],
    }
    (REPORT_DIR / "font-inspection.json").write_text(json.dumps(inspection, ensure_ascii=False, indent=2), encoding="utf-8")


def existing_variable_outputs() -> list[dict[str, Any]]:
    return [
        {"file": str(path.relative_to(ROOT)), "rebuilt": False}
        for path in sorted(VARIABLE_DIR.glob("*.ttf"))
    ]


def parse_regions(value: str | None) -> list[str]:
    if not value:
        return list(REGION_ORDER)
    result = [check_region(part.strip()) for part in value.replace(";", ",").split(",") if part.strip()]
    if not result:
        return list(REGION_ORDER)
    return list(dict.fromkeys(result))


def build_all(
    static_only: bool = False,
    regions: list[str] | None = None,
    resume_static: bool = False,
) -> dict[str, Any]:
    regions = list(REGION_ORDER if regions is None else dict.fromkeys(check_region(region) for region in regions))
    ensure_build_sources(static_only, regions)
    required_paths = [reference_font_path(region, "Regular", False) for region in regions]
    if not static_only:
        required_paths.extend([source_han_vf_path(region) for region in variable_regions(regions)])
        required_paths.extend(
            path for path in (classical_vf_override_path(region) for region in variable_regions(regions)) if path
        )
        required_paths.extend([INTER_UPRIGHT, INTER_ITALIC])
    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(path)
    if static_only:
        log_step("variable: skipped by --static-only")
        variable_outputs = existing_variable_outputs()
    else:
        variable_outputs = []
        for region in variable_regions(regions):
            log_step(f"variable {region} upright: build")
            variable_outputs.append(build_one_variable(region, False))
            log_step(f"variable {region} italic: build")
            variable_outputs.append(build_one_variable(region, True))
    log_step("static: build hinted and unhinted")
    static_outputs = build_static_fonts(regions, resume=resume_static)
    write_static_readme(regions)
    report = {
        "family": "Sarasa Ui PropDigits",
        "version": VERSION,
        "regions": regions,
        "variable_regions": variable_regions(regions),
        "static_regions": regions,
        "static_only": static_only,
        "resume_static": resume_static,
        "build_script": "tools/build_sarasa_ui_propdigits_sc.py",
        "bootstrap_sources": {
            "sarasa_gothic": SARASA_TAG,
            "sarasa_ui_ttf": f"{SARASA_VERSION} hinted/unhinted",
            "source_han_sans": SOURCE_HAN_TAG,
            "inter": INTER_TAG,
            "node": NODE_VERSION,
        },
        "source_base_by_region": {
            region: str(source_han_vf_path(region)) for region in variable_regions(regions)
        },
        "classical_vf_override_by_region": {
            region: str(path)
            for region in variable_regions(regions)
            for path in [classical_vf_override_path(region)]
            if path
        },
        "source_latin_upright": str(INTER_UPRIGHT),
        "source_latin_italic": str(INTER_ITALIC),
        "reference_unicode_set_by_region": {
            region: str(reference_font_path(region, "Regular", False)) for region in regions
        },
        "method": (
            "VF 由对应地区的 CJK VF 与 Inter VF 合并而来；SC/TC/HC/J/K 使用对应 "
            "Source Han Sans VF。CL 以 Shanggu Sans "
            f"{SHANGGU_TAG} 的官方 TTF/VF 发布物作为传统旧字形来源：静态 TTF 的 "
            "classical override 直接使用 ShangguSansTC 静态 TTF，VF 以 "
            "SourceHanSansK-VF 为底稿并用 ShangguSansTC-VF 覆盖对应 ideograph 字形。"
            "码位归属采用 Sarasa pass1 风格，并按 VF 源文件实际覆盖做兜底：Inter VF 以 Sarasa "
            "的 Inter 设置（ss03 和 cv10）烘焙后用于 Latin 和西文符号覆盖；CJK、"
            "Korean、Jamo 以及 Sarasa Ui 本地化标点优先来自对应地区的 Source Han Sans VF。"
            "在追加 Inter glyph 前，会先应用 Source Han 的 pwid/符号清洗和 Hangul "
            "全角归一。最终 layout 导入对应地区 Sarasa Ui 暴露的 Inter VF GSUB/GPOS 特性，"
            "保留 Sarasa 的空 cv01-cv13/ss01-ss08 标签，并保留 cv14、ccmp、按上游 "
            "Sarasa Ui 覆盖裁剪的 locl、Hangul Jamo 特性、vert/vrt2、tnum/pnum、"
            "连续 em dash，以及与 Inter 一致的数字冒号 colon-run calt 规则。合并后会"
            "对齐对应地区参考 Sarasa Ui 的 cmap alias split 和 alias mapping、GSUB/GPOS "
            "FeatureRecord 顺序、Script/LangSys 覆盖、基础 lookup 结构、非数字 advance "
            "和 LSB 的权重轴规则、tnum 数字目标 hmtx、垂直指标、vmtx 默认值和变化、"
            "GDEF、VORG，以及与 Sarasa 兼容的 head/OS/2 metadata。VF 和静态输出都包含 "
            "STAT；静态 STAT 只描述单实例样式，不保留 fvar/gvar 可变表。glyph 总数不强行"
            "补齐到与上游一致：cmap 字形和布局可达的未编码字形会保留，不可达 glyph 数量"
            "差异不视为渲染缺陷。静态 TTF 从对应地区静态 Source Han Sans 和 Inter 源字体出发，"
            "经 Sarasa 的 pass1/kanji/hangul/pass2 片段路径构建，再补上 PropDigits 的数字"
            "和冒号 cmap remap、命名、metadata、layout、GDEF/VORG、与上游兼容的 glyf "
            "flags/bbox/组件名、静态 post format 2 glyph names、OTS-compatible glyf repeat "
            "编码和静态 STAT 规则。hinted 静态套件会对本项目实际生成的片段重新 hint："
            "pass1 先经过 ttfautohint，随后 pass1/kanji/hangul 片段用 Sarasa 上游 "
            "Chlorophytum hcfg 写入 TrueType instructions，最后由 pass2 合成最终 TTF。"
            "unhinted 静态套件提供无 TrueType instructions 的正式静态输出。"
            "Normal、Medium、Heavy 分别使用上游 Regular、SemiBold、Bold 作为对齐参考，"
            "因为上游 Sarasa 没有发布对应的静态输出样式。"
            "公开字重采用 Sarasa/CSS 口径：ExtraLight 为 200，Light/Regular/Bold/Heavy "
            "分别为 300/400/700/900；CJK 轮廓来源仍是 Source Han Sans 的 ExtraLight "
            "250，VF 通过 avar 轴映射让 public wght=200 对应 Source Han 内部 wght=250。"
        ),
        "intentional_differences_from_upstream_sarasa_ui": [
            "默认 ASCII 数字和 ':' 使用比例 glyph；tnum 会恢复等宽 glyph。",
            "公开字重遵循 Sarasa/CSS 口径：200、300、350、400、500、700、900；CJK 内部仍使用 Source Han ExtraLight 250 作为 public 200 的来源。",
            "VF 与静态 TTF 都使用与 Inter 一致的上下文冒号 colon-run 行为。",
            "静态 TTF 使用 post format 2，以便 PropDigits cmap remap 后仍保留审计稳定的 glyph names；VF 保持既有 post/GID 模型。",
        ],
        "final_gsub_features": sorted(FINAL_GSUB_FEATURES),
        "variable_outputs": variable_outputs,
        "static_outputs": static_outputs,
    }
    write_reports(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-only", action="store_true", help="只重建静态 hinted/unhinted TTF，不重建 VF 输出。")
    parser.add_argument(
        "--regions",
        default=",".join(REGION_ORDER),
        help="逗号分隔的地区列表，默认 CL,SC,TC,HC,J,K。",
    )
    parser.add_argument(
        "--resume-static",
        action="store_true",
        help="保留现有静态输出，只重建缺失的完整字重；同一字重的 hinted/unhinted 正体与 Italic 都存在时跳过。",
    )
    args = parser.parse_args()
    report = build_all(static_only=args.static_only, regions=parse_regions(args.regions), resume_static=args.resume_static)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
