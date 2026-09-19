from __future__ import annotations

import argparse
import asyncio
import copy
import concurrent.futures
import functools
import gzip
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import math
import os
import platform
import re
import site
import shutil
import stat
import struct
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from python_env_bootstrap import ensure_project_python


PYTHON_DEPS = {
    "fontTools": ("fonttools", "fonttools[woff]==4.63.0", "4.63.0"),
    "uharfbuzz": ("uharfbuzz", "uharfbuzz==0.56.0", "0.56.0"),
    "brotli": ("Brotli", "brotli==1.2.0", "1.2.0"),
    "ttfautohint": ("ttfautohint-py", "ttfautohint-py==0.6.1", "0.6.1"),
    "py7zr": ("py7zr", "py7zr==1.1.3", "1.1.3"),
    "afdko": ("afdko", "afdko==5.0.1", "5.0.1"),
    "chws_tool": ("chws-tool", "chws-tool==1.4.5", "1.4.5"),
    "east_asian_spacing": (
        "east-asian-spacing",
        "east-asian-spacing==1.4.5",
        "1.4.5",
    ),
}


def ensure_python_deps() -> None:
    ensure_project_python(
        PYTHON_DEPS,
        project_root=Path(__file__).resolve().parents[1],
        label="build",
    )


ensure_python_deps()

from fontTools import subset
from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, TTLibError, newTable
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.ttLib.tables import _g_l_y_f as glyf_table
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.varLib.models import VariationModel, normalizeValue, piecewiseLinearMap
from fontTools.varLib.instancer import AxisLimits, instantiateAvar, instantiateVariableFont
from fontTools.varLib import builder as var_builder
from fontTools.varLib.builder import buildVarDevTable
from fontTools.varLib.varStore import OnlineVarStoreBuilder, VarStoreInstancer
import uharfbuzz as hb


def patch_fonttools_overlap_simple_repeat_encoding() -> None:
    def compile_deltas_greedy_ots_safe(self: Any, flags: Any, deltas: Any) -> tuple[bytearray, bytearray, bytearray]:
        compressed_flags = bytearray()
        compressed_xs = bytearray()
        compressed_ys = bytearray()
        last_flag = None
        repeat = 0
        for point_index, (flag, (x, y)) in enumerate(zip(flags, deltas)):
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

            can_extend_overlap_repeat = flag == last_flag and repeat != 255
            if (
                point_index
                and flag & glyf_table.flagOverlapSimple
                and not can_extend_overlap_repeat
            ):
                # OTS permits bit 6 after the first point only when it is
                # represented by the first flag's repeat run.  A coordinate
                # rewrite can make the compressed x/y bits differ and force
                # the later flag to be emitted explicitly; keep the first
                # point's overlap semantics but clear that invalid duplicate.
                flag &= ~glyf_table.flagOverlapSimple

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


SARASA_VERSION = "1.0.40"
VERSION = "1.0.40.4"
FONT_REVISION = 1.0404
OPENTYPE_VERSION = "1.0404"
SARASA_TAG = f"v{SARASA_VERSION}"
SARASA_COMMIT = "4b908c71116a3192f7a9889bd67b1939a891e527"
SARASA_SOURCE_ARCHIVE_SHA256 = "4ef493207030d9bd811695a71b9edeabff498f27b03d515f8f393445afc14ea9"
SARASA_PACKAGE_LOCK_SHA256 = "7a68020fc12728fbf58a34bbc78d1873e957ad1063aaa1f5bdc85800d3896dfe"
SARASA_UI_ARCHIVE_SHA256 = {
    "CL": {
        "hinted": "3ccae6ff23487bf969b23a8cb4b409759a65c86825c180617c1ddc6a57a4cf98",
        "unhinted": "41a604a38f1940471fe6797b0e7fac4f15c1889c00a007c6f8a2b0868a19fd85",
    },
    "SC": {
        "hinted": "bb9891c8be805cd0dae942a07472b3031db2510741b7cde42e9591f74a186f6a",
        "unhinted": "1f0e344e36947317104b686b3bbd7b96ebdc3659e11f1213e62f65e5f772ea80",
    },
    "TC": {
        "hinted": "b1589c9db8ea45cf078855f02493eab845d244065b2f0289954c1554d34d8ade",
        "unhinted": "5a3e143bcc8f2d6cb10999eba4fe04e63667c077aaa03b6d7270ab3ae20e1106",
    },
    "HC": {
        "hinted": "dc884d538afe24f9c27681dc7526bfb7ea19a360d05e3098f06b8786c599e9ca",
        "unhinted": "b0c275ec7a7afc5e60a9b03253ab8d387e4b0c4784b006ac10ec9a3a03371bac",
    },
    "J": {
        "hinted": "9a3a0b23654cb102b5488e7b52d4e08c486a9d123320335e1047fabd8d6b4ff3",
        "unhinted": "b6338812e2a27dedb8e7cb36426f9e35b2da037822c037eb15342d5dcb19523e",
    },
    "K": {
        "hinted": "533e6149df19179ee25c564bc0d62bebe093139310519e7d0fc5db858b9e4637",
        "unhinted": "1be00f7897930658e239afac79e6f5e2555b9c1f887b3654224caf7da042df35",
    },
}
SOURCE_HAN_TAG = "2.005R"
SOURCE_HAN_VF_ARCHIVE_SHA256 = "e5944ea7253878409232f1ffad464e9e93879c0207eaf2960bba327eef89ed81"
INTER_TAG = "v4.1"
INTER_ARCHIVE_SHA256 = "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e"
SHANGGU_TAG = "1.028"
SHANGGU_SANS_TTF_ARCHIVE_NAME = "ShangguSansTTFs.7z"
SHANGGU_SANS_TTF_SHA256 = "a7fc794127270fff06224e2129e28ea917669bb3601296f219ea64fe0266b6f0"
SHANGGU_SANS_VF_ARCHIVE_NAME = "ShangguSansVF_TTFs.7z"
SHANGGU_SANS_VF_SHA256 = "31b207a05332196ff444114d66de1c7b622d3a7244ec15a2485e8d0844cb1984"
NODE_VERSION = "v26.7.0"
NODE_ARCHIVE_SHA256 = {
    ("win", "x64", "zip"): "d3bd72755141ed32bbcd841228ee81897c8a98d50dfa7dae2179399a0a7c90f8",
    ("win", "arm64", "zip"): "be8775204cfceca5a73c30f91bf0de5e85274c01b776dc13f16b91aa251ebb01",
    ("linux", "x64", "tar.xz"): "982aa24dd8be4c889c6a8ab337ddff3b0896645b20f4239356e80552c16277ee",
    ("linux", "arm64", "tar.xz"): "afc7a004018485092ac8985b817b0d5684472bd9472e0b57d2ab88737e50090d",
    ("darwin", "x64", "tar.xz"): "bd19c6b98d923fb049f64b547163b9f7d52ae73f16fdee09ecef9ab248c4d6ff",
    ("darwin", "arm64", "tar.xz"): "595d2f934e081b82961d1a5fd41c6dbd0c5a952d9e8be5b4566ab754426968d2",
}
SOURCE_ARCHIVE_DIR = WORK_ROOT / "source-archives"
NODE_DIR = Path(os.environ.get("SARASA_NODE_DIR", WORK_ROOT / "node"))
REFERENCE_ROOT = Path(
    os.environ.get("REFERENCE_SARASA_ROOT", WORK_ROOT / f"official-sarasa-ui-{SARASA_VERSION}")
)
REFERENCE_SC_LEGACY_ROOT = Path(
    os.environ.get("REFERENCE_SARASA_SC_ROOT", REFERENCE_ROOT / "SC")
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
        WORK_ROOT / f"Sarasa-Gothic-{SARASA_VERSION}",
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
    "Regular": "Regular",
    "SemiBold": "SemiBold",
    "Bold": "Bold",
    "Heavy": "Bold",
}
CHLOROPHYTUM_HINT_STORE_ORDER = "numeric-gid-hcfg-shared-v3"
STATIC_HINT_WORK_VERSION = 5
STATIC_POSTPROCESS_VERSION = 8
STATIC_HINT_RECIPE = {
    "version": STATIC_HINT_WORK_VERSION,
    "fragments": "sarasa-pass1-kanji-hangul",
    "pass1_autohint": "ttfautohint-before-chlorophytum",
    "environment": "all-sarasa-families-all-regions",
    "hint_store_order": CHLOROPHYTUM_HINT_STORE_ORDER,
}
SARASA_HINT_JOBS = int(os.environ.get("SARASA_HINT_JOBS", str(os.cpu_count() or 1)))
SARASA_HINT_PREP_JOBS = int(
    os.environ.get("SARASA_HINT_PREP_JOBS", str(min(4, os.cpu_count() or 1)))
)
SARASA_HINT_FAMILY_ORDER = [
    "Gothic",
    "Ui",
    "Mono",
    "MonoSlab",
    "Term",
    "TermSlab",
    "Fixed",
    "FixedSlab",
]

VARIABLE_DIR = ROOT / "fonts" / "variable"
STATIC_ROOT = ROOT / "fonts" / "static"
STATIC_DIR = STATIC_ROOT / f"SarasaUiPropDigitsSC-TTF-{VERSION}"
STATIC_UNHINTED_DIR = STATIC_ROOT / f"SarasaUiPropDigitsSC-TTF-Unhinted-{VERSION}"
REPORT_DIR = ROOT / "reports"
BUILD_CACHE_DIR = Path(os.environ.get("SARASA_BUILD_CACHE", ROOT / ".build-cache" / "sarasa-ui-propdigits"))
STATIC_HINT_WORK_ROOT = Path(
    os.environ.get("SARASA_HINT_WORK_ROOT", ROOT / ".build-cache" / "hint-work")
)

AXIS_LIMIT = {"wght": (250, 400, 900)}
PUBLIC_AXIS_LIMIT = {"wght": (200, 400, 900)}
INTER_AXIS_LIMIT = {"opsz": 14, "wght": (200, 400, 900)}
VF_FAMILY = "Sarasa Ui VF PropDigits SC"
VF_PS_FAMILY = "Sarasa-Ui-VF-PropDigits-SC"
VF_FAMILY_ZH_HANS = "更纱黑体 Ui VF PropDigits SC"
STATIC_FAMILY = "Sarasa Ui PropDigits SC"
STATIC_PS_FAMILY = "Sarasa-Ui-PropDigits-SC"
STATIC_FAMILY_ZH_HANS = "更纱黑体 Ui PropDigits SC"
INTER_PREFIX = "inter."
OS2_VENDOR_ID = "MRDK"
OS2_CODEPAGE_RANGE_1 = 2147746207
PROJECT_COPYRIGHT = (
    "Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net). "
    "Portions Copyright (c) 2016 The Inter Project Authors. "
    "Portions Copyright (c) 2014-2021 Adobe Systems Incorporated (http://www.adobe.com/), "
    "with Reserved Font Name 'Source'. "
    "Portions Copyright (c) 2012 Google Inc."
)
SHANGGU_COPYRIGHT = "© 2022-2026 Shanggu Fonts."
PROJECT_LICENSE_DESCRIPTION = (
    "This Font Software is licensed under the SIL Open Font License, Version 1.1."
)
PROJECT_LICENSE_URL = "https://openfontlicense.org"
SOURCE_ONLY_LEGAL_NAME_IDS = {7, 8, 9, 10, 11}


def project_copyright(region: str) -> str:
    value = PROJECT_COPYRIGHT
    if check_region(region) == "CL":
        value += " " + SHANGGU_COPYRIGHT
    return value

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
    return STATIC_ROOT / f"SarasaUiPropDigits{region}-{hint_part}-{VERSION}"


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
    {"name": "Light", "value": 300, "range_min": 300, "range_max": 399},
    {"name": "Regular", "value": 400, "range_min": 400, "range_max": 499, "flags": 0x2},
    {"name": "SemiBold", "value": 600, "range_min": 500, "range_max": 649},
    {"name": "Bold", "value": 700, "range_min": 650, "range_max": 799},
    {"name": "Heavy", "value": 900, "range_min": 800, "range_max": 900},
]

STATIC_UNDERLINE_METRICS = {
    200: (37, -185),
    300: (53, -177),
    400: (68, -169),
    600: (84, -161),
    700: (92, -157),
    900: (113, -147),
}

SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT = {
    200: 250,
    300: 300,
    350: 350,
    400: 400,
    600: 500,
    700: 700,
    900: 900,
}
EM_DASH_PROBE_WEIGHTS = (
    200,
    250,
    300,
    325,
    350,
    375,
    400,
    500,
    600,
    650,
    700,
    800,
    900,
)
INTER_OUTLINE_CORRECTION_WEIGHTS = [300, 350, 600, 700]
INTER_OUTLINE_TRANSLATION_TOLERANCE = 2.0
INTER_OUTLINE_AUDIT_WEIGHTS = list(EM_DASH_PROBE_WEIGHTS)
INTER_COMPOSITE_CONTROL_WEIGHTS = list(INTER_OUTLINE_AUDIT_WEIGHTS)
INTER_COMPOSITE_NAMED_CONTROL_WEIGHTS = [200, 300, 350, 400, 600, 700, 900]
INTER_COMPOSITE_VARIATION_WEIGHTS = [
    weight for weight in INTER_COMPOSITE_CONTROL_WEIGHTS if weight != 400
]

STATIC_STYLE_SOURCES = {
    "ExtraLight": {"shs": "ExtraLight", "inter": "ExtraLight", "sarasa": "ExtraLight", "hcfg": "ExtraLight"},
    "Light": {"shs": "Light", "inter": "Light", "sarasa": "Light", "hcfg": "Light"},
    "Regular": {"shs": "Regular", "inter": "Regular", "sarasa": "Regular", "hcfg": "Regular"},
    "SemiBold": {"shs": "Medium", "inter": "SemiBold", "sarasa": "SemiBold", "hcfg": "SemiBold"},
    "Bold": {"shs": "Bold", "inter": "Bold", "sarasa": "Bold", "hcfg": "Bold"},
    "Heavy": {"shs": "Heavy", "inter": "Black", "sarasa": "Bold", "hcfg": "Bold"},
}

DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
DIGITS_TF = [f"{name}.tf" for name in DIGITS]
PROPDIGITS_CODEPOINTS = set(range(0x30, 0x3A)) | {0x3A}
DASH_CMAP_CODEPOINTS = {0x2014, 0x2015, 0x2E3A, 0x2E3B, 0xFE31}
DASH_LOCL_CODEPOINTS = {0x2014, 0x2E3A, 0x2E3B}
ELLIPSIS_CODEPOINT = 0x2026
CJK_ELLIPSIS_CODEPOINT = 0x22EF
CJK_LOCL_LANGUAGES = {"JAN ", "KOR ", "ZHH ", "ZHS ", "ZHT "}
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
VF_METRIC_REFERENCE_STOPS = [*REFERENCE_ADVANCE_STOPS, ("Heavy", 900)]
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


def static_reference_style_name(weight_name: str, italic: bool) -> str:
    style = str(STATIC_STYLE_SOURCES.get(weight_name, {"sarasa": weight_name})["sarasa"])
    if italic:
        return "Italic" if style == "Regular" else f"{style}Italic"
    return style


def static_uses_official_glyph_baseline(weight_name: str) -> bool:
    """Whether Sarasa publishes an exact static style for this project weight."""
    source = STATIC_STYLE_SOURCES.get(weight_name, {"sarasa": weight_name})
    return str(source["sarasa"]) == weight_name


def reference_font_path(region: str, weight_name: str, italic: bool) -> Path:
    return region_reference_dir(region, False) / f"{sarasa_region_prefix(region)}-{reference_style_name(weight_name, italic)}.ttf"


def hinted_reference_font_path(region: str, weight_name: str, italic: bool) -> Path:
    return region_reference_dir(region, True) / f"{sarasa_region_prefix(region)}-{reference_style_name(weight_name, italic)}.ttf"


def static_reference_font_path(region: str, weight_name: str, italic: bool) -> Path:
    return region_reference_dir(region, False) / f"{sarasa_region_prefix(region)}-{static_reference_style_name(weight_name, italic)}.ttf"


def open_reference_font(region: str, weight_name: str, italic: bool) -> TTFont:
    path = reference_font_path(region, weight_name, italic)
    if not path.exists():
        raise FileNotFoundError(path)
    return TTFont(path)


def open_vf_metric_reference_font(
    region: str,
    weight_name: str,
    italic: bool,
) -> TTFont:
    if weight_name != "Heavy":
        return open_reference_font(region, weight_name, italic)
    path = static_dir(region, False) / static_output_name(region, weight_name, italic)
    if not path.exists():
        raise FileNotFoundError(
            f"Heavy VF metric reference requires the static output first: {path}"
        )
    return TTFont(path)


def open_project_static_metric_reference_font(
    region: str,
    weight_name: str,
    italic: bool,
) -> TTFont:
    path = static_dir(region, False) / static_output_name(region, weight_name, italic)
    if not path.exists():
        raise FileNotFoundError(
            f"VF metric reference requires the static output first: {path}"
        )
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


def set_unique_identifier_record(font: TTFont, value: str) -> None:
    font["name"].names = [
        record for record in font["name"].names if record.nameID != 3
    ]
    set_windows_name_record(font, 3, value, 0x0409)


def update_project_legal_names(font: TTFont, region: str) -> None:
    set_name_record(font, 0, project_copyright(region))
    set_name_record(font, 13, PROJECT_LICENSE_DESCRIPTION)
    set_name_record(font, 14, PROJECT_LICENSE_URL)
    font["name"].names = [
        record for record in font["name"].names if record.nameID not in SOURCE_ONLY_LEGAL_NAME_IDS
    ]


def remove_mac_name_records(font: TTFont) -> dict[str, int]:
    if "name" not in font:
        return {"mac_name_records_removed": 0, "mac_name_records_remaining": 0}
    before = len(font["name"].names)
    font["name"].names = [
        record for record in font["name"].names if record.platformID != 1
    ]
    remaining = sum(record.platformID == 1 for record in font["name"].names)
    return {
        "mac_name_records_removed": before - len(font["name"].names),
        "mac_name_records_remaining": remaining,
    }


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
    variations_ps_prefix = "".join(character for character in ps if character.isascii() and character.isalnum())
    source_label = source_han_vf_basename(region) or source_han_static_prefix(region)
    if classical_vf_override_basename(region):
        source_label += " + ShangguSansTC-VF"
    version = f"Version {OPENTYPE_VERSION}; project {VERSION}; {source_label} + Inter VF; PropDigits"
    replacements = {
        1: family,
        2: subfamily,
        4: full,
        5: version,
        6: ps,
        16: family,
        17: subfamily,
        25: variations_ps_prefix,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)
    set_unique_identifier_record(font, ps + f";{VERSION}")
    update_project_legal_names(font, region)
    set_localized_name_records(
        font,
        region,
        {
            1: family_local,
            2: subfamily,
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


def update_static_names(
    font: TTFont,
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    hinted: bool,
) -> None:
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
        4: full,
        5: (
            f"Version {OPENTYPE_VERSION}; project {VERSION}; "
            f"static {source_han_static_prefix(region)} + static Inter; PropDigits; "
            f"{'hinted' if hinted else 'unhinted'}"
        ),
        6: ps,
        16: typographic_family,
        17: typographic_style,
    }
    for name_id, value in replacements.items():
        set_name_record(font, name_id, value)
    set_unique_identifier_record(
        font,
        ps + f";{VERSION};{'hinted' if hinted else 'unhinted'}",
    )
    update_project_legal_names(font, region)
    set_localized_name_records(
        font,
        region,
        {
            1: family_local,
            2: legacy_style,
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
    if weight_name == "Bold":
        os2.fsSelection |= 1 << 5
        font["head"].macStyle |= 0b01
    else:
        font["head"].macStyle &= ~0b01
    if weight_name != "Bold" and not italic:
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
    os2.ulCodePageRange1 = OS2_CODEPAGE_RANGE_1
    os2.ulCodePageRange2 = 0
    apply_sarasa_vertical_metrics(font)
    update_caret_slope(font)


def normalize_static_raster_metadata(font: TTFont, hinted: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "unhinted_max_zones_normalized": False,
        "unhinted_gasp_sentinel_normalized": False,
    }
    if hinted:
        return report
    if "maxp" in font and int(font["maxp"].tableVersion) == 0x00010000:
        if int(font["maxp"].maxZones) != 1:
            font["maxp"].maxZones = 1
            report["unhinted_max_zones_normalized"] = True
    if "gasp" in font and font["gasp"].gaspRange:
        ranges = dict(font["gasp"].gaspRange)
        if 0xFFFF not in ranges:
            final_key = max(ranges)
            final_behavior = ranges.pop(final_key)
            ranges[0xFFFF] = final_behavior
            font["gasp"].gaspRange = dict(sorted(ranges.items()))
            report["unhinted_gasp_sentinel_normalized"] = True
    return report


def vf_mapped_normalized_weight(font: TTFont, weight: float) -> float:
    if "fvar" not in font:
        raise ValueError("VF weight mapping requires fvar")
    axes = [axis for axis in font["fvar"].axes if axis.axisTag == "wght"]
    if len(axes) != 1:
        raise ValueError(f"expected one wght axis, found {len(axes)}")
    axis = axes[0]
    avar_mapping = font["avar"].segments.get("wght", {}) if "avar" in font else {}
    normalized = normalizeValue(weight, (axis.minValue, axis.defaultValue, axis.maxValue))
    if avar_mapping:
        normalized = piecewiseLinearMap(normalized, avar_mapping)
    return normalized


def rebuild_vf_underline_mvar(font: TTFont) -> dict[str, Any]:
    if "fvar" not in font or "post" not in font:
        raise ValueError("VF underline MVAR requires fvar and post tables")

    def normalized_location(weight: int) -> dict[str, float]:
        normalized = vf_mapped_normalized_weight(font, weight)
        return {} if normalized == 0 else {"wght": normalized}

    weights = list(STATIC_UNDERLINE_METRICS)
    controls = {weight: list(STATIC_UNDERLINE_METRICS[weight]) for weight in weights}

    def build_store() -> tuple[Any, list[Any], dict[str, int]]:
        model = VariationModel([normalized_location(weight) for weight in weights], axisOrder=["wght"])
        store_builder = OnlineVarStoreBuilder(["wght"])
        store_builder.setModel(model)
        records = []
        base_values: dict[str, int] = {}
        for value_tag, value_index, attribute in (
            ("unds", 0, "underlineThickness"),
            ("undo", 1, "underlinePosition"),
        ):
            values = [controls[weight][value_index] for weight in weights]
            base_value, variation_index = store_builder.storeMasters(values)
            base_values[attribute] = base_value
            record = ot.MetricsValueRecord()
            record.ValueTag = value_tag
            record.VarIdx = variation_index
            records.append(record)
        store = store_builder.finish()
        for region in store.VarRegionList.Region:
            for axis in region.VarRegionAxis:
                for attribute in ("StartCoord", "PeakCoord", "EndCoord"):
                    setattr(axis, attribute, floatToFixedToFloat(getattr(axis, attribute), 14))
        variation_index_mapping = store.optimize()
        for record in records:
            record.VarIdx = variation_index_mapping[record.VarIdx]
        return store, records, base_values

    calibration_rounds = 0
    while True:
        store, records, base_values = build_store()
        records_by_tag = {record.ValueTag: record for record in records}
        errors: dict[int, tuple[int, int]] = {}
        for weight in weights:
            instancer = VarStoreInstancer(
                store,
                font["fvar"].axes,
                normalized_location(weight),
            )
            actual = (
                base_values["underlineThickness"]
                + otRound(instancer[records_by_tag["unds"].VarIdx]),
                base_values["underlinePosition"]
                + otRound(instancer[records_by_tag["undo"].VarIdx]),
            )
            expected = STATIC_UNDERLINE_METRICS[weight]
            if actual != expected:
                errors[weight] = (expected[0] - actual[0], expected[1] - actual[1])
        if not errors:
            break
        calibration_rounds += 1
        if calibration_rounds > 8:
            raise RuntimeError(f"VF underline MVAR calibration did not converge: {errors}")
        for weight, correction in errors.items():
            controls[weight][0] += correction[0]
            controls[weight][1] += correction[1]

    font["post"].underlineThickness = base_values["underlineThickness"]
    font["post"].underlinePosition = base_values["underlinePosition"]
    if "MVAR" in font:
        del font["MVAR"]
    table = font["MVAR"] = newTable("MVAR")
    mvar = table.table = ot.MVAR()
    mvar.Version = 0x00010000
    mvar.Reserved = 0
    mvar.VarStore = store
    mvar.ValueRecordSize = 8
    mvar.ValueRecordCount = len(records)
    mvar.ValueRecord = sorted(records, key=lambda record: record.ValueTag)
    return {
        "underline_mvar_records": len(records),
        "underline_mvar_weights": weights,
        "underline_mvar_calibration_rounds": calibration_rounds,
        "underline_mvar_control_corrections": {
            str(weight): [
                controls[weight][index] - STATIC_UNDERLINE_METRICS[weight][index]
                for index in range(2)
            ]
            for weight in weights
            if tuple(controls[weight]) != STATIC_UNDERLINE_METRICS[weight]
        },
        "underline_default_thickness": font["post"].underlineThickness,
        "underline_default_position": font["post"].underlinePosition,
    }


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


def update_head_project_revision(font: TTFont) -> dict[str, float]:
    font["head"].fontRevision = FONT_REVISION
    return {"head_font_revision": FONT_REVISION}


def layout_has_feature(font: TTFont, table_tag: str, feature_tag: str) -> bool:
    if table_tag not in font:
        return False
    table = font[table_tag].table
    return bool(
        table.FeatureList
        and any(record.FeatureTag == feature_tag for record in table.FeatureList.FeatureRecord)
    )


def add_noto_contextual_spacing(path: Path) -> dict[str, Any]:
    before = TTFont(path, recalcTimestamp=False)
    try:
        modified = before["head"].modified
        before_records = (
            len(before["GPOS"].table.FeatureList.FeatureRecord)
            if "GPOS" in before and before["GPOS"].table.FeatureList
            else 0
        )
        before_lookups = (
            len(before["GPOS"].table.LookupList.Lookup)
            if "GPOS" in before and before["GPOS"].table.LookupList
            else 0
        )
        had_chws = layout_has_feature(before, "GPOS", "chws")
        had_vchw = layout_has_feature(before, "GPOS", "vchw")
    finally:
        before.close()

    if not (had_chws and had_vchw):
        from chws_tool import add_chws_async

        config_logger = logging.getLogger("config")
        previous_level = config_logger.level
        config_logger.setLevel(logging.ERROR)
        try:
            result = asyncio.run(add_chws_async(path, path))
        finally:
            config_logger.setLevel(previous_level)
        if result is None:
            raise RuntimeError(f"Noto contextual spacing was not applicable to {path}")

    normalized = TTFont(path, recalcTimestamp=False)
    try:
        normalized["head"].modified = modified
        update_head_project_revision(normalized)
        normalized.save(path, reorderTables=True)
    finally:
        normalized.close()

    final = TTFont(path, recalcTimestamp=False)
    try:
        has_chws = layout_has_feature(final, "GPOS", "chws")
        has_vchw = layout_has_feature(final, "GPOS", "vchw")
        if not has_chws or not has_vchw:
            raise RuntimeError(f"Noto contextual spacing features are incomplete in {path}")
        after_records = len(final["GPOS"].table.FeatureList.FeatureRecord)
        after_lookups = len(final["GPOS"].table.LookupList.Lookup)
        modified_preserved = final["head"].modified == modified
    finally:
        final.close()

    return {
        "noto_contextual_spacing": True,
        "noto_contextual_spacing_source": "Noto CJK add-chws delivery step",
        "chws_tool_version": importlib.metadata.version("chws-tool"),
        "east_asian_spacing_version": importlib.metadata.version("east-asian-spacing"),
        "gpos_feature_records_added": after_records - before_records,
        "gpos_lookups_added": after_lookups - before_lookups,
        "chws_present": has_chws,
        "vchw_present": has_vchw,
        "head_modified_preserved": modified_preserved,
    }


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


def parse_raw_simple_glyph_flags(data: bytes) -> dict[str, int] | None:
    """Parse the encoded simple-glyph flag stream without expanding a glyph."""
    if len(data) < 10:
        raise ValueError("truncated glyf header")
    contour_count = struct.unpack(">h", data[:2])[0]
    if contour_count <= 0:
        return None
    end_points_end = 10 + contour_count * 2
    if end_points_end + 2 > len(data):
        raise ValueError("truncated simple-glyph end points")
    point_count = struct.unpack(">H", data[end_points_end - 2 : end_points_end])[0] + 1
    instruction_length = struct.unpack(">H", data[end_points_end : end_points_end + 2])[0]
    offset = end_points_end + 2 + instruction_length
    if offset > len(data):
        raise ValueError("truncated simple-glyph instructions")

    points_read = 0
    stored_flag_count = 0
    overlap_point_flags = 0
    invalid_explicit_overlap_flags = 0
    while points_read < point_count:
        if offset >= len(data):
            raise ValueError("truncated simple-glyph flag stream")
        flag = data[offset]
        offset += 1
        repeat_count = 0
        if flag & glyf_table.flagRepeat:
            if offset >= len(data):
                raise ValueError("truncated simple-glyph flag repeat")
            repeat_count = data[offset]
            offset += 1
        run_length = repeat_count + 1
        if points_read + run_length > point_count:
            raise ValueError("simple-glyph flag repeat exceeds point count")
        if flag & glyf_table.flagOverlapSimple:
            overlap_point_flags += run_length
            if stored_flag_count:
                invalid_explicit_overlap_flags += 1
        points_read += run_length
        stored_flag_count += 1
    return {
        "points": point_count,
        "stored_flags": stored_flag_count,
        "overlap_point_flags": overlap_point_flags,
        "invalid_explicit_overlap_flags": invalid_explicit_overlap_flags,
    }


def raw_glyf_table_data(font: TTFont) -> tuple[bytes, list[int]]:
    reader = getattr(font, "reader", None)
    if reader is None or "glyf" not in reader.tables or "loca" not in reader.tables:
        buffer = BytesIO()
        font.save(buffer, reorderTables=True)
        buffer.seek(0)
        roundtrip = TTFont(buffer, lazy=True, recalcTimestamp=False)
        try:
            return raw_glyf_table_data(roundtrip)
        finally:
            roundtrip.close()

    glyf_data = bytes(reader["glyf"])
    loca_data = bytes(reader["loca"])
    glyph_count = int(font["maxp"].numGlyphs)
    long_loca = int(font["head"].indexToLocFormat) == 1
    entry_size = 4 if long_loca else 2
    expected_size = (glyph_count + 1) * entry_size
    if len(loca_data) < expected_size:
        raise ValueError(
            f"truncated loca table: {len(loca_data)} bytes for {glyph_count} glyphs"
        )
    if long_loca:
        offsets = list(struct.unpack(f">{glyph_count + 1}I", loca_data[:expected_size]))
    else:
        offsets = [
            value * 2
            for value in struct.unpack(f">{glyph_count + 1}H", loca_data[:expected_size])
        ]
    if offsets != sorted(offsets) or offsets[-1] > len(glyf_data):
        raise ValueError("invalid loca offsets")
    return glyf_data, offsets


def raw_simple_glyph_flag_stats(font: TTFont) -> dict[str, int]:
    if "glyf" not in font:
        return {
            "glyphs_checked": 0,
            "overlap_point_flags": 0,
            "invalid_explicit_overlap_flags": 0,
            "malformed_glyphs": 0,
        }
    glyf_data, offsets = raw_glyf_table_data(font)
    stats = {
        "glyphs_checked": 0,
        "overlap_point_flags": 0,
        "invalid_explicit_overlap_flags": 0,
        "malformed_glyphs": 0,
    }
    for start, end in zip(offsets, offsets[1:]):
        if start == end:
            continue
        try:
            glyph_stats = parse_raw_simple_glyph_flags(glyf_data[start:end])
        except ValueError:
            stats["malformed_glyphs"] += 1
            continue
        if glyph_stats is None:
            continue
        stats["glyphs_checked"] += 1
        stats["overlap_point_flags"] += glyph_stats["overlap_point_flags"]
        stats["invalid_explicit_overlap_flags"] += glyph_stats[
            "invalid_explicit_overlap_flags"
        ]
    return stats


def count_simple_glyph_overlap_flags(font: TTFont) -> int:
    return raw_simple_glyph_flag_stats(font)["overlap_point_flags"]


def count_ots_invalid_simple_overlap_flags(font: TTFont) -> int:
    stats = raw_simple_glyph_flag_stats(font)
    if stats["malformed_glyphs"]:
        raise ValueError(
            f"{stats['malformed_glyphs']} malformed raw simple-glyph flag streams"
        )
    return stats["invalid_explicit_overlap_flags"]


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


def sync_static_cmap_bboxes_from_reference(
    font: TTFont,
    reference: TTFont,
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "glyf" not in font or "glyf" not in reference:
        return {
            "post_layout_reference_glyf_bboxes_checked": 0,
            "post_layout_reference_glyf_bboxes_synced": 0,
        }

    current_cmap = font.getBestCmap()
    reference_cmap = reference.getBestCmap()
    checked = 0
    synced = 0
    visited: set[tuple[str, str]] = set()
    for codepoint, reference_glyph_name in reference_cmap.items():
        if codepoint in skip_codepoints or codepoint not in current_cmap:
            continue
        glyph_name = current_cmap[codepoint]
        pair = (glyph_name, reference_glyph_name)
        if pair in visited:
            continue
        visited.add(pair)
        if (
            glyph_name not in font["glyf"].glyphs
            or reference_glyph_name not in reference["glyf"].glyphs
            or glyph_point_structure(font, glyph_name)
            != glyph_point_structure(reference, reference_glyph_name)
        ):
            continue
        checked += 1
        glyph = font["glyf"][glyph_name]
        reference_glyph = reference["glyf"][reference_glyph_name]
        changed = False
        for field in ("xMin", "yMin", "xMax", "yMax"):
            if not hasattr(reference_glyph, field):
                continue
            value = copy.deepcopy(getattr(reference_glyph, field))
            if getattr(glyph, field, None) != value:
                setattr(glyph, field, value)
                changed = True
        if changed:
            synced += 1
    return {
        "post_layout_reference_glyf_bboxes_checked": checked,
        "post_layout_reference_glyf_bboxes_synced": synced,
    }


def sync_static_glyf_from_reference(
    font: TTFont,
    reference: TTFont,
    skip_codepoints: set[int],
) -> dict[str, int]:
    if "glyf" not in font or "glyf" not in reference:
        return {
            "reference_glyf_flags_synced": 0,
            "reference_glyf_bboxes_synced": 0,
            "reference_component_aliases_created": 0,
            "reference_component_aliases_removed": 0,
            "reference_component_names_synced": 0,
        }

    current_cmap = font.getBestCmap()
    reference_cmap = reference.getBestCmap()
    flags_synced = 0
    bboxes_synced = 0
    component_aliases_created = 0
    component_names_synced = 0
    stale_aliases: set[str] = set()
    visited: set[tuple[str, str]] = set()

    def sync_pair(glyph_name: str, reference_glyph_name: str) -> None:
        nonlocal flags_synced, bboxes_synced, component_aliases_created, component_names_synced
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
                if (
                    reference_component_name not in font["glyf"].glyphs
                    and component.glyphName in font["glyf"].glyphs
                    and glyph_point_structure(font, component.glyphName)
                    == glyph_point_structure(reference, reference_component_name)
                ):
                    cloned_name = clone_glyph(font, component.glyphName, reference_component_name)
                    if cloned_name == reference_component_name:
                        component_aliases_created += 1
                if reference_component_name in font["glyf"].glyphs:
                    old_component_name = component.glyphName
                    if old_component_name != reference_component_name:
                        component.glyphName = reference_component_name
                        component_names_synced += 1
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
        "reference_component_aliases_created": component_aliases_created,
        "reference_component_aliases_removed": aliases_removed,
        "reference_component_names_synced": component_names_synced,
    }


def rebuild_gdef_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    reference_gdef = reference.get("GDEF")
    if not reference_gdef or not getattr(reference_gdef.table, "GlyphClassDef", None):
        return {"gdef_classdefs": 0, "gdef_mark_sets": 0}
    existing_var_store = copy.deepcopy(
        getattr(font["GDEF"].table, "VarStore", None)
    ) if "GDEF" in font else None
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

    # Variable lookup flags and filtering-set indices belong to the merged
    # source GDEF. Replacing its sets with auto-named static sets makes them
    # empty; the subsetter then rewrites UseMarkFilteringSet to IgnoreMarks.
    gdef = copy.deepcopy(font["GDEF"] if "fvar" in font and "GDEF" in font else reference_gdef)
    if "fvar" in font and getattr(gdef.table, "GlyphClassDef", None):
        inherited = {name: value for name, value in gdef.table.GlyphClassDef.classDefs.items() if name in glyph_set}
        inherited.update(class_defs)
        class_defs = inherited
    gdef.table.GlyphClassDef.classDefs = class_defs
    mark_sets = getattr(gdef.table, "MarkGlyphSetsDef", None)
    if mark_sets and getattr(mark_sets, "Coverage", None):
        for coverage_table in mark_sets.Coverage:
            coverage_table.glyphs = [glyph_name for glyph_name in coverage_table.glyphs if glyph_name in glyph_set]
        mark_sets.MarkSetCount = len(mark_sets.Coverage)
    if existing_var_store is not None:
        gdef.table.Version = max(int(gdef.table.Version), 0x00010003)
        gdef.table.VarStore = existing_var_store
    font["GDEF"] = gdef
    return {
        "gdef_classdefs": len(class_defs),
        "gdef_mark_sets": mark_sets.MarkSetCount if mark_sets else 0,
        "gdef_var_store_preserved": int(existing_var_store is not None),
    }


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


def restrict_cmap_to_reference(font: TTFont, reference: TTFont, keep_codepoints: set[int] | None = None) -> dict[str, int]:
    if "cmap" not in font:
        return {"reference_cmap_extra_codepoints_removed": 0, "reference_cmap_extra_entries_removed": 0}
    keep = set(reference.getBestCmap().keys())
    if keep_codepoints:
        keep.update(keep_codepoints)
    current = set(font.getBestCmap().keys())
    remove = current - keep
    if not remove:
        return {"reference_cmap_extra_codepoints_removed": 0, "reference_cmap_extra_entries_removed": 0}
    removed = 0
    for cmap_table in font["cmap"].tables:
        if not cmap_table.isUnicode():
            continue
        for codepoint in list(cmap_table.cmap):
            if codepoint in remove:
                del cmap_table.cmap[codepoint]
                removed += 1
    return {"reference_cmap_extra_codepoints_removed": len(remove), "reference_cmap_extra_entries_removed": removed}


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


def single_substitution_mappings_by_feature_record(
    font: TTFont,
    tag: str,
) -> list[tuple[int, dict[str, str]]]:
    if "GSUB" not in font:
        return []
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return []
    records: list[tuple[int, dict[str, str]]] = []
    for feature_index, record in enumerate(gsub.FeatureList.FeatureRecord):
        if record.FeatureTag != tag:
            continue
        mapping: dict[str, str] = {}
        for lookup_index in list(record.Feature.LookupListIndex or []):
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                mapping.update(getattr(subtable, "mapping", {}) or {})
        records.append((feature_index, mapping))
    return records


def ligature_outputs_for_feature(
    font: TTFont,
    tag: str,
    first_glyph: str,
) -> dict[int, str]:
    if "GSUB" not in font:
        return {}
    gsub = font["GSUB"].table
    if not gsub.FeatureList or not gsub.LookupList:
        return {}
    outputs: dict[int, str] = {}
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag != tag:
            continue
        for lookup_index in record.Feature.LookupListIndex or []:
            lookup = gsub.LookupList.Lookup[lookup_index]
            subtables = list(lookup.SubTable) if lookup.LookupType == 4 else [
                subtable.ExtSubTable
                for subtable in lookup.SubTable
                if lookup.LookupType == 7
                and getattr(subtable, "ExtensionLookupType", None) == 4
                and getattr(subtable, "ExtSubTable", None)
            ]
            for subtable in subtables:
                for ligature in list(getattr(subtable, "ligatures", {}).get(first_glyph, []) or []):
                    components = [first_glyph, *list(ligature.Component or [])]
                    if all(component == first_glyph for component in components):
                        outputs[len(components)] = ligature.LigGlyph
    return outputs


def upstream_dash_roles(font: TTFont) -> dict[str, str]:
    cmap = font.getBestCmap() or {}
    proportional = cmap.get(0x2014)
    fullwidth = cmap.get(0x2015)
    encoded_two = cmap.get(0x2E3A)
    encoded_three = cmap.get(0x2E3B)
    vertical_single = cmap.get(0xFE31)
    required = {
        "proportional": proportional,
        "fullwidth": fullwidth,
        "encoded_two": encoded_two,
        "encoded_three": encoded_three,
        "vertical_single": vertical_single,
    }
    missing = [name for name, glyph_name in required.items() if not glyph_name]
    if missing:
        raise ValueError(f"upstream dash glyphs are missing: {missing}")

    fullwidth_ligatures = ligature_outputs_for_feature(font, "ccmp", str(fullwidth))
    fullwidth_two = fullwidth_ligatures.get(2)
    fullwidth_three = fullwidth_ligatures.get(3)
    if not fullwidth_two or not fullwidth_three:
        # Shanggu exposes its already-localized 2em/3em forms directly.
        if proportional == fullwidth:
            fullwidth_two = encoded_two
            fullwidth_three = encoded_three
        else:
            raise ValueError("upstream ccmp lacks fullwidth two/three-em dash ligatures")

    vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    vertical_two = vertical.get(str(fullwidth_two))
    vertical_three = vertical.get(str(fullwidth_three))
    vertical_single = vertical.get(str(fullwidth)) or vertical_single
    if not vertical_two or not vertical_three or not vertical_single:
        raise ValueError("upstream vert/vrt2 lacks localized dash forms")
    return {
        **{name: str(glyph_name) for name, glyph_name in required.items()},
        "fullwidth_two": str(fullwidth_two),
        "fullwidth_three": str(fullwidth_three),
        "vertical_single": str(vertical_single),
        "vertical_two": str(vertical_two),
        "vertical_three": str(vertical_three),
    }


def cjk_ellipsis_roles(font: TTFont) -> dict[str, str]:
    cmap = font.getBestCmap() or {}
    proportional = cmap.get(ELLIPSIS_CODEPOINT)
    fullwidth = cmap.get(CJK_ELLIPSIS_CODEPOINT)
    if not proportional or not fullwidth:
        raise ValueError("font lacks U+2026 or U+22EF ellipsis glyph")
    vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    vertical_target = vertical.get(fullwidth) or vertical.get(proportional)
    if not vertical_target:
        raise ValueError("font lacks a vertical Source Han ellipsis glyph")
    return {
        "proportional": str(proportional),
        "fullwidth": str(fullwidth),
        "vertical": str(vertical_target),
    }


def langsys_single_substitution_mapping(
    font: TTFont,
    language: str,
    tag: str,
) -> dict[str, str]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return {}
    gsub = font["GSUB"].table
    mappings: list[dict[str, str]] = []
    for langsys in langsys_records_for_language(font, language):
        mapping: dict[str, str] = {}
        for feature_index in list(langsys.FeatureIndex or []):
            record = gsub.FeatureList.FeatureRecord[feature_index]
            if record.FeatureTag != tag:
                continue
            for lookup_index in list(record.Feature.LookupListIndex or []):
                lookup = gsub.LookupList.Lookup[lookup_index]
                for subtable in single_substitution_subtables(lookup):
                    mapping.update(getattr(subtable, "mapping", {}) or {})
        if mapping:
            mappings.append(mapping)
    if not mappings:
        return {}
    first = mappings[0]
    if any(mapping != first for mapping in mappings[1:]):
        raise ValueError(f"inconsistent {tag} mappings for language {language}")
    return first


def dash_locl_mappings_by_language(
    font: TTFont,
    roles: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    roles = roles or upstream_dash_roles(font)
    dash_sources = {
        roles["proportional"],
        roles["encoded_two"],
        roles["encoded_three"],
    }
    result: dict[str, dict[str, str]] = {}
    for language in sorted(CJK_LOCL_LANGUAGES):
        mapping = langsys_single_substitution_mapping(font, language, "locl")
        filtered = {
            source_name: target_name
            for source_name, target_name in mapping.items()
            if source_name in dash_sources
        }
        if filtered:
            result[language] = filtered
    return result


def dash_locl_mapping_candidates(
    font: TTFont,
    roles: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    roles = roles or upstream_dash_roles(font)
    sources = {
        roles["proportional"],
        roles["encoded_two"],
        roles["encoded_three"],
    }
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return []
    gsub = font["GSUB"].table
    candidates: list[dict[str, str]] = []
    signatures: set[tuple[tuple[str, str], ...]] = set()
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag != "locl":
            continue
        mapping: dict[str, str] = {}
        for lookup_index in list(record.Feature.LookupListIndex or []):
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                mapping.update(getattr(subtable, "mapping", {}) or {})
        filtered = {
            source_name: target_name
            for source_name, target_name in mapping.items()
            if source_name in sources
        }
        signature = tuple(sorted(filtered.items()))
        if filtered and signature not in signatures:
            signatures.add(signature)
            candidates.append(filtered)
    return candidates


def normalized_source_han_dash_locl_mappings(
    font: TTFont,
    roles: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    roles = roles or upstream_dash_roles(font)
    candidates = dash_locl_mapping_candidates(font, roles)
    korean_target = next(
        (
            mapping[roles["proportional"]]
            for mapping in candidates
            if roles["proportional"] in mapping
            and mapping[roles["proportional"]]
            not in {roles["proportional"], roles["fullwidth"]}
        ),
        None,
    )
    if not korean_target:
        raise ValueError("Source Han dash locl lacks its Korean single-dash alternate")
    standard = {
        roles["proportional"]: roles["fullwidth"],
        roles["encoded_two"]: roles["fullwidth_two"],
        roles["encoded_three"]: roles["fullwidth_three"],
    }
    korean = {
        **standard,
        roles["proportional"]: korean_target,
    }
    return {
        language: dict(korean if language == "KOR " else standard)
        for language in sorted(CJK_LOCL_LANGUAGES)
    }


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
    # Keep Source Han's dash localization alive until the final layout pass.
    # Its KOR form uses an unencoded alternate that would otherwise disappear
    # at the next cmap subset, making an exact upstream reconstruction impossible.
    allowed_unicodes = reference_locl_source_unicodes(region) | DASH_LOCL_CODEPOINTS
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
        return {
            "classical_vf_override_codepoints": 0,
            "classical_vf_override_glyphs": 0,
            "classical_vf_dash_glyphs": 0,
        }
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

    # Shanggu localizes the dash family in the cmap instead of through locl.
    # Copy the same semantic forms into the Source Han K base before public-axis
    # remapping and italic shearing, including the original gvar programs.
    base_dash = upstream_dash_roles(base)
    override_dash = upstream_dash_roles(override)
    dash_pairs = [
        (base_dash["proportional"], override_dash["proportional"]),
        (base_dash["fullwidth"], override_dash["fullwidth"]),
        (base_dash["encoded_two"], override_dash["encoded_two"]),
        (base_dash["encoded_three"], override_dash["encoded_three"]),
        (base_dash["fullwidth_two"], override_dash["fullwidth_two"]),
        (base_dash["fullwidth_three"], override_dash["fullwidth_three"]),
        (base_dash["vertical_single"], override_dash["vertical_single"]),
        (base_dash["vertical_two"], override_dash["vertical_two"]),
        (base_dash["vertical_three"], override_dash["vertical_three"]),
    ]
    for target_name, source_name in dash_pairs:
        copy_external_glyph_data(base, target_name, override, source_name)
    return {
        "classical_vf_override_codepoints": replaced_codepoints,
        "classical_vf_override_glyphs": len(replaced_glyphs),
        "classical_vf_dash_glyphs": len(dash_pairs),
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
    # avar segment coordinates and gvar support coordinates are serialized as
    # F2Dot14. Build correction supports at that same precision so a named
    # public instance still lands exactly on its correction peak after save.
    return floatToFixedToFloat(normalized, 14)


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
    font._sarasa_public_weight_axis_applied = True
    font._sarasa_public_weight_avar_segment = dict(segment)
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
    control = glyph_variation_control_coordinates(font, glyph_name)
    return (0 if control is None else len(control[1])) + 4


def materialize_gvar_variations(font: TTFont) -> dict[str, Any]:
    if "gvar" not in font or "glyf" not in font:
        return {
            "gvar_materialized_glyphs": 0,
            "gvar_raw_point_warning_count": 0,
            "gvar_raw_point_warning_glyphs": [],
            "gvar_coordinate_length_mismatches": 0,
            "gvar_coordinate_length_mismatch_samples": [],
        }

    class WarningCapture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.WARNING)
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    logger = logging.getLogger("fontTools.ttLib.tables.TupleVariation")
    capture = WarningCapture()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(capture)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    materialized: dict[str, list[TupleVariation]] = {}
    warning_glyphs: list[dict[str, Any]] = []
    mismatch_samples: list[dict[str, Any]] = []
    warning_count = 0
    mismatch_count = 0
    try:
        variations = font["gvar"].variations
        for glyph_name in font.getGlyphOrder():
            capture.messages.clear()
            glyph_variations = list(variations.get(glyph_name, []))
            materialized[glyph_name] = glyph_variations
            if capture.messages:
                warning_count += len(capture.messages)
                if len(warning_glyphs) < 32:
                    warning_glyphs.append(
                        {
                            "glyph": glyph_name,
                            "messages": list(capture.messages),
                        }
                    )
            expected_count = gvar_coordinate_count(font, glyph_name)
            bad_lengths = sorted(
                {
                    len(variation.coordinates)
                    for variation in glyph_variations
                    if len(variation.coordinates) != expected_count
                }
            )
            if bad_lengths:
                mismatch_count += 1
                if len(mismatch_samples) < 32:
                    mismatch_samples.append(
                        {
                            "glyph": glyph_name,
                            "expected": expected_count,
                            "actual": bad_lengths,
                        }
                    )
    finally:
        logger.removeHandler(capture)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    # Replacing LazyDict with an ordinary mapping forces every tuple to be
    # recompiled. FontTools then omits any out-of-range raw point numbers it
    # reported while decoding instead of copying their original bytes through.
    font["gvar"].variations = materialized
    return {
        "gvar_materialized_glyphs": len(materialized),
        "gvar_raw_point_warning_count": warning_count,
        "gvar_raw_point_warning_glyphs": warning_glyphs,
        "gvar_coordinate_length_mismatches": mismatch_count,
        "gvar_coordinate_length_mismatch_samples": mismatch_samples,
    }


def raw_gvar_integrity_status(path: Path) -> dict[str, Any]:
    font = TTFont(path, lazy=True, recalcTimestamp=False)
    try:
        report = materialize_gvar_variations(font)
    finally:
        font.close()
    report["ok"] = not (
        report["gvar_raw_point_warning_count"]
        or report["gvar_coordinate_length_mismatches"]
    )
    return report


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


def glyph_variation_control_coordinates(
    font: TTFont,
    glyph_name: str,
) -> tuple[str, list[tuple[float, float]], list[Any]] | None:
    if "glyf" not in font or glyph_name not in font["glyf"].glyphs:
        return None
    glyph = font["glyf"][glyph_name]
    if glyph.isComposite():
        components = list(glyph.components)
        return (
            "composite",
            [(float(component.x), float(component.y)) for component in components],
            [
                tuple(component.transform) if hasattr(component, "transform") else None
                for component in components
            ],
        )
    coordinates = glyph.getCoordinates(font["glyf"])[0]
    if not coordinates:
        return None
    return (
        "simple",
        [(float(x), float(y)) for x, y in coordinates],
        list(glyph.endPtsOfContours),
    )


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


def final_cmap_inter_outline_correction_pairs(
    font: TTFont,
    inter: TTFont,
) -> list[tuple[str, str]]:
    target_cmap = font.getBestCmap() or {}
    inter_cmap = inter.getBestCmap() or {}
    candidates: dict[str, set[str]] = {}
    for codepoint, target_name in target_cmap.items():
        source_name = inter_cmap.get(codepoint)
        if (
            source_name
            and use_inter_codepoint(codepoint)
            and codepoint not in PROPDIGITS_CODEPOINTS
        ):
            candidates.setdefault(target_name, set()).add(source_name)
    pairs = []
    for target_name, source_names in candidates.items():
        if target_name in source_names:
            source_name = target_name
        else:
            source_name = min(source_names)
        pairs.append((source_name, target_name))
    return pairs


def add_inter_outline_correction_variations(
    font: TTFont,
    inter: TTFont,
    *,
    final_cmap: bool = False,
) -> dict[str, int]:
    if "gvar" not in font or "glyf" not in font or "fvar" not in font or "glyf" not in inter or "fvar" not in inter:
        return {"inter_outline_correction_variations_added": 0, "inter_outline_correction_glyphs": 0}
    pairs = (
        final_cmap_inter_outline_correction_pairs(font, inter)
        if final_cmap
        else inter_outline_correction_pairs(font, inter)
    )
    supports = advance_supports(font, INTER_OUTLINE_CORRECTION_WEIGHTS)
    added = 0
    touched_glyphs: set[str] = set()
    for weight_value in INTER_OUTLINE_CORRECTION_WEIGHTS:
        current = instantiateVariableFont(font, {"wght": weight_value}, inplace=False, optimize=True)
        target = instantiateVariableFont(inter, {"wght": weight_value}, inplace=False, optimize=True)
        try:
            support = supports[weight_value]
            for source_name, target_name in pairs:
                current_control = glyph_variation_control_coordinates(current, target_name)
                target_control = glyph_variation_control_coordinates(target, source_name)
                if (
                    current_control is None
                    or target_control is None
                    or current_control[0] != target_control[0]
                    or current_control[2] != target_control[2]
                    or len(current_control[1]) != len(target_control[1])
                ):
                    continue
                current_coordinates = current_control[1]
                target_coordinates = target_control[1]
                deltas = [
                    (
                        otRound(target_x - current_x),
                        otRound(target_y - current_y),
                    )
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


def decomposed_glyph_recording(glyph_set: Any, glyph_name: str) -> list[Any]:
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return pen.value


def translation_invariant_outline_residual(
    source_recording: list[Any],
    target_recording: list[Any],
) -> tuple[str | None, float]:
    if len(source_recording) != len(target_recording):
        return "command_count", float("inf")

    point_pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for (source_command, source_points), (target_command, target_points) in zip(
        source_recording,
        target_recording,
    ):
        if source_command != target_command:
            return "command", float("inf")
        if len(source_points) != len(target_points):
            return "point_count", float("inf")
        for source_point, target_point in zip(source_points, target_points):
            if (source_point is None) != (target_point is None):
                return "implied_point", float("inf")
            if source_point is not None:
                point_pairs.append((source_point, target_point))

    if not point_pairs:
        return None, 0.0
    x_offsets = [target[0] - source[0] for source, target in point_pairs]
    y_offsets = [target[1] - source[1] for source, target in point_pairs]
    x_shift = (min(x_offsets) + max(x_offsets)) / 2
    y_shift = (min(y_offsets) + max(y_offsets)) / 2
    residual = max(
        max(
            abs(source[0] + x_shift - target[0]),
            abs(source[1] + y_shift - target[1]),
        )
        for source, target in point_pairs
    )
    return None, residual


def component_transform(component: Any) -> tuple[float, float, float, float, float, float]:
    _glyph_name, transform = component.getComponentInfo()
    return tuple(float(value) for value in transform)


def inter_composite_corrections_at_weight(
    font: TTFont,
    inter: TTFont,
    pairs: list[tuple[str, str]],
    weight_value: int,
) -> tuple[
    list[tuple[str, list[tuple[int, int]]]],
    dict[str, tuple[int, int, int, int]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    current = instantiateVariableFont(
        font,
        {"wght": weight_value},
        inplace=False,
        optimize=True,
    )
    source = instantiateVariableFont(
        inter,
        {"wght": weight_value},
        inplace=False,
        optimize=True,
    )
    pending: list[tuple[str, list[tuple[int, int]]]] = []
    bounds: dict[str, tuple[int, int, int, int]] = {}
    unsupported: list[dict[str, Any]] = []
    mismatches = 0
    maximum_residual = 0.0
    try:
        current_set = current.getGlyphSet()
        source_set = source.getGlyphSet()
        for source_name, target_name in pairs:
            reason, residual = translation_invariant_outline_residual(
                decomposed_glyph_recording(source_set, source_name),
                decomposed_glyph_recording(current_set, target_name),
            )
            if reason is None and math.isfinite(residual):
                maximum_residual = max(maximum_residual, residual)
            if reason is None and residual <= INTER_OUTLINE_TRANSLATION_TOLERANCE:
                continue

            mismatches += 1
            current_glyph = current["glyf"][target_name]
            source_glyph = source["glyf"][source_name]
            current_glyph.expand(current["glyf"])
            source_glyph.expand(source["glyf"])
            problem: str | None = None
            if not current_glyph.isComposite() or not source_glyph.isComposite():
                problem = "non_composite"
            elif len(current_glyph.components) != len(source_glyph.components):
                problem = "component_count"

            current_bounds = glyph_bbox(current, target_name)
            source_bounds = glyph_bbox(source, source_name)
            if problem is None and (current_bounds is None or source_bounds is None):
                problem = "missing_parent_bounds"

            deltas: list[tuple[int, int]] = []
            if problem is None:
                parent_shift_x = current_bounds[0] - source_bounds[0]
                parent_shift_y = current_bounds[3] - source_bounds[3]
                for component_index, (current_component, source_component) in enumerate(
                    zip(current_glyph.components, source_glyph.components)
                ):
                    current_transform = component_transform(current_component)
                    source_transform = component_transform(source_component)
                    if (
                        current_transform[:4] != (1.0, 0.0, 0.0, 1.0)
                        or source_transform[:4] != (1.0, 0.0, 0.0, 1.0)
                    ):
                        problem = f"non_identity_component_{component_index}"
                        break
                    current_child = current_component.glyphName
                    source_child = source_component.glyphName
                    current_child_bounds = glyph_bbox(current, current_child)
                    source_child_bounds = glyph_bbox(source, source_child)
                    if current_child_bounds is None or source_child_bounds is None:
                        problem = f"missing_child_bounds_{component_index}"
                        break
                    child_reason, child_residual = translation_invariant_outline_residual(
                        decomposed_glyph_recording(current_set, current_child),
                        decomposed_glyph_recording(source_set, source_child),
                    )
                    if (
                        child_reason is not None
                        or child_residual > INTER_OUTLINE_TRANSLATION_TOLERANCE
                    ):
                        problem = f"child_outline_{component_index}"
                        break
                    child_shift_x = current_child_bounds[0] - source_child_bounds[0]
                    child_shift_y = current_child_bounds[3] - source_child_bounds[3]
                    desired_x = source_transform[4] + parent_shift_x - child_shift_x
                    desired_y = source_transform[5] + parent_shift_y - child_shift_y
                    deltas.append(
                        (
                            otRound(desired_x - current_transform[4]),
                            otRound(desired_y - current_transform[5]),
                        )
                    )

            if problem is not None or not any(dx or dy for dx, dy in deltas):
                unsupported.append(
                    {
                        "weight": weight_value,
                        "source_glyph": source_name,
                        "target_glyph": target_name,
                        "reason": problem or "zero_component_delta",
                        "outline_reason": reason,
                        "residual": residual,
                    }
                )
                continue
            pending.append((target_name, deltas))
            bounds[target_name] = current_bounds
    finally:
        current.close()
        source.close()
    return (
        pending,
        bounds,
        {
            "mismatches": mismatches,
            "maximum_residual": maximum_residual,
        },
        unsupported,
    )


def restore_default_inter_composite_metric_bounds(
    font: TTFont,
    targets: dict[str, tuple[int, int, int, int]],
) -> dict[str, int]:
    ordered, maximum_depth = glyph_component_dependency_order(font, targets)
    translations = 0
    maximum_translation = 0
    for glyph_name in ordered:
        expected = targets[glyph_name]
        actual = glyph_bbox(font, glyph_name)
        if actual is None:
            raise RuntimeError(f"missing default Inter composite bounds for {glyph_name}")
        dx = expected[0] - actual[0]
        dy = expected[3] - actual[3]
        if not dx and not dy:
            continue
        glyph = font["glyf"][glyph_name]
        glyph.expand(font["glyf"])
        for component in glyph.components:
            component.x += dx
            component.y += dy
        glyph.recalcBounds(font["glyf"])
        translations += 1
        maximum_translation = max(maximum_translation, abs(dx), abs(dy))

    mismatches = []
    for glyph_name, expected in targets.items():
        actual = glyph_bbox(font, glyph_name)
        if actual is None or actual[0] != expected[0] or actual[3] != expected[3]:
            mismatches.append((glyph_name, expected, actual))
    if mismatches:
        raise RuntimeError(
            "default Inter composite metric-bound restoration failed: "
            + repr(mismatches[:8])
        )
    return {
        "inter_composite_default_bound_targets": len(targets),
        "inter_composite_default_bound_translations": translations,
        "inter_composite_default_bound_maximum_translation": maximum_translation,
        "inter_composite_default_bound_maximum_dependency_depth": maximum_depth,
        "inter_composite_default_metric_bound_mismatches_after": 0,
    }


def add_translation_invariant_inter_composite_corrections(
    font: TTFont,
    inter: TTFont,
) -> tuple[dict[str, Any], dict[int, dict[str, tuple[int, int, int, int]]]]:
    if "gvar" not in font or "glyf" not in font or "fvar" not in font:
        raise ValueError("Inter composite correction requires glyf/gvar/fvar")

    pairs = final_cmap_inter_outline_correction_pairs(font, inter)
    default_weight = int(weight_axis(font).defaultValue)
    if default_weight not in INTER_COMPOSITE_CONTROL_WEIGHTS:
        raise ValueError(f"unsupported Inter composite default weight: {default_weight}")
    supports = advance_supports(font, INTER_COMPOSITE_VARIATION_WEIGHTS)
    bounds_by_weight: dict[int, dict[str, tuple[int, int, int, int]]] = {}
    mismatches_by_weight: dict[int, int] = {}
    maximum_residual_by_weight: dict[int, float] = {}
    touched_glyphs: set[str] = set()

    default_pending, default_bounds, default_status, unsupported = (
        inter_composite_corrections_at_weight(
            font,
            inter,
            pairs,
            default_weight,
        )
    )
    if unsupported:
        raise RuntimeError(
            "unsupported default Inter composite corrections: "
            + repr(unsupported[:8])
        )
    for target_name, deltas in default_pending:
        glyph = font["glyf"][target_name]
        glyph.expand(font["glyf"])
        if len(glyph.components) != len(deltas):
            raise ValueError(f"invalid default component count for {target_name}")
        for component, (dx, dy) in zip(glyph.components, deltas):
            component.x += dx
            component.y += dy
        glyph.recalcBounds(font["glyf"])
        touched_glyphs.add(target_name)
    default_bound_report = restore_default_inter_composite_metric_bounds(
        font,
        default_bounds,
    )
    mismatches_by_weight[default_weight] = default_status["mismatches"]
    maximum_residual_by_weight[default_weight] = default_status[
        "maximum_residual"
    ]

    variations_added = 0
    for weight_value in INTER_COMPOSITE_VARIATION_WEIGHTS:
        pending, bounds, status, unsupported = inter_composite_corrections_at_weight(
            font,
            inter,
            pairs,
            weight_value,
        )
        if unsupported:
            raise RuntimeError(
                "unsupported translation-invariant Inter composite corrections: "
                + repr(unsupported[:8])
            )
        mismatches_by_weight[weight_value] = status["mismatches"]
        maximum_residual_by_weight[weight_value] = status["maximum_residual"]
        if bounds:
            bounds_by_weight[weight_value] = bounds
        for target_name, deltas in pending:
            coordinate_count = gvar_coordinate_count(font, target_name)
            if coordinate_count != len(deltas) + 4:
                raise ValueError(
                    f"invalid composite gvar point count for {target_name}: "
                    f"{coordinate_count} != {len(deltas) + 4}"
                )
            coordinates: list[Any] = list(deltas) + [(0, 0)] * 4
            font["gvar"].variations.setdefault(target_name, []).append(
                TupleVariation({"wght": supports[weight_value]}, coordinates)
            )
            variations_added += 1
            touched_glyphs.add(target_name)

    return (
        {
            "inter_composite_control_weights": list(
                INTER_COMPOSITE_CONTROL_WEIGHTS
            ),
            "inter_composite_pairs_checked_per_weight": len(pairs),
            "inter_composite_mismatches_before_by_weight": mismatches_by_weight,
            "inter_composite_maximum_residual_before_by_weight": (
                maximum_residual_by_weight
            ),
            "inter_composite_default_glyph_corrections": len(default_pending),
            "inter_composite_variations_added": variations_added,
            "inter_composite_glyphs": len(touched_glyphs),
            "inter_composite_unsupported": 0,
            **default_bound_report,
        },
        bounds_by_weight,
    )


def add_uniform_composite_translation_variation(
    font: TTFont,
    glyph_name: str,
    support: tuple[float, float, float],
    dx: int,
    dy: int,
) -> None:
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if not glyph.isComposite():
        raise ValueError(f"expected composite glyph for translation: {glyph_name}")
    coordinates: list[Any] = [(otRound(dx), otRound(dy))] * len(glyph.components)
    coordinates.extend([(0, 0)] * 4)
    if len(coordinates) != gvar_coordinate_count(font, glyph_name):
        raise ValueError(f"invalid composite translation point count for {glyph_name}")
    font["gvar"].variations.setdefault(glyph_name, []).append(
        TupleVariation({"wght": support}, coordinates)
    )


def restore_inter_composite_control_bounds(
    font: TTFont,
    bounds_by_weight: dict[int, dict[str, tuple[int, int, int, int]]],
) -> dict[str, Any]:
    supports = advance_supports(font, list(bounds_by_weight))
    corrections = 0
    maximum_shift = 0
    maximum_depth = 0

    for weight_value, targets in sorted(bounds_by_weight.items()):
        ordered, depth = glyph_component_dependency_order(font, targets)
        maximum_depth = max(maximum_depth, depth)
        current = instantiateVariableFont(
            font,
            {"wght": weight_value},
            inplace=False,
            optimize=True,
        )
        try:
            for glyph_name in ordered:
                expected = targets[glyph_name]
                actual = glyph_bbox(current, glyph_name)
                if actual is None:
                    raise RuntimeError(
                        f"missing corrected composite bounds for {glyph_name} at {weight_value}"
                    )
                dx = expected[0] - actual[0]
                dy = expected[3] - actual[3]
                if not dx and not dy:
                    continue
                add_uniform_composite_translation_variation(
                    font,
                    glyph_name,
                    supports[weight_value],
                    dx,
                    dy,
                )
                instance_glyph = current["glyf"][glyph_name]
                instance_glyph.expand(current["glyf"])
                for component in instance_glyph.components:
                    component.x += dx
                    component.y += dy
                instance_glyph.recalcBounds(current["glyf"])
                corrections += 1
                maximum_shift = max(maximum_shift, abs(dx), abs(dy))
        finally:
            current.close()

    mismatches: list[dict[str, Any]] = []
    outline_dimension_changes = 0
    maximum_outline_dimension_change = 0
    for weight_value, targets in sorted(bounds_by_weight.items()):
        current = instantiateVariableFont(
            font,
            {"wght": weight_value},
            inplace=False,
            optimize=True,
        )
        try:
            for glyph_name, expected in targets.items():
                actual = glyph_bbox(current, glyph_name)
                if actual is None or actual[0] != expected[0] or actual[3] != expected[3]:
                    mismatches.append(
                        {
                            "weight": weight_value,
                            "glyph": glyph_name,
                            "expected": expected,
                            "actual": actual,
                        }
                    )
                    continue
                expected_size = (expected[2] - expected[0], expected[3] - expected[1])
                actual_size = (actual[2] - actual[0], actual[3] - actual[1])
                if actual_size != expected_size:
                    outline_dimension_changes += 1
                    maximum_outline_dimension_change = max(
                        maximum_outline_dimension_change,
                        abs(actual_size[0] - expected_size[0]),
                        abs(actual_size[1] - expected_size[1]),
                    )
        finally:
            current.close()
    if mismatches:
        raise RuntimeError(
            "Inter composite metric-bound restoration failed: "
            + repr(mismatches[:8])
        )
    return {
        "inter_composite_bound_control_weights": sorted(bounds_by_weight),
        "inter_composite_bound_targets": sum(len(targets) for targets in bounds_by_weight.values()),
        "inter_composite_bound_translation_variations_added": corrections,
        "inter_composite_bound_maximum_translation": maximum_shift,
        "inter_composite_bound_maximum_dependency_depth": maximum_depth,
        "inter_composite_metric_bound_mismatches_after": 0,
        "inter_composite_outline_dimension_changes": outline_dimension_changes,
        "inter_composite_maximum_outline_dimension_change": (
            maximum_outline_dimension_change
        ),
    }


def align_inter_composite_harfbuzz_metrics(
    font: TTFont,
    region: str,
    italic: bool,
) -> dict[str, Any]:
    default_weight = int(weight_axis(font).defaultValue)
    supports = advance_supports(font, INTER_COMPOSITE_VARIATION_WEIGHTS)
    glyph_order = font.getGlyphOrder()
    rounds = 0
    corrections = 0
    maximum_translation = 0
    initial_mismatches: dict[int, dict[str, int]] = {}

    while True:
        data = serialized_font_bytes(font)
        face = hb.Face(data)
        hb_font = hb.Font(face)
        hb_font.scale = (face.upem, face.upem)
        pending: dict[tuple[int, str], tuple[int, int]] = {}
        mismatches_by_weight: dict[int, dict[str, int]] = {}
        hard_failures: list[dict[str, Any]] = []
        for weight_name, weight_value in VF_METRIC_REFERENCE_STOPS:
            expected = open_project_static_metric_reference_font(
                region,
                weight_name,
                italic,
            )
            hb_font.set_variations({"wght": weight_value})
            counts = {
                "horizontal_side_bearing": 0,
                "vertical_side_bearing": 0,
            }
            try:
                for codepoint, expected_glyph in (expected.getBestCmap() or {}).items():
                    glyph_id = hb_font.get_nominal_glyph(codepoint)
                    if glyph_id is None:
                        hard_failures.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "reason": "missing_glyph",
                            }
                        )
                        continue
                    target_name = glyph_order[glyph_id]
                    expected_h_advance, expected_lsb = expected["hmtx"].metrics[
                        expected_glyph
                    ]
                    actual_h_advance = hb_font.get_glyph_h_advance(glyph_id)
                    if actual_h_advance != expected_h_advance:
                        hard_failures.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "glyph": target_name,
                                "reason": "horizontal_advance",
                                "actual": actual_h_advance,
                                "expected": expected_h_advance,
                            }
                        )
                        continue
                    extents = hb_font.get_glyph_extents(glyph_id)
                    dx = 0
                    dy = 0
                    if extents is not None and extents.x_bearing != expected_lsb:
                        dx = int(expected_lsb) - int(extents.x_bearing)
                        counts["horizontal_side_bearing"] += 1
                    if "vmtx" in expected and expected_glyph in expected["vmtx"].metrics:
                        expected_v_advance, expected_tsb = expected["vmtx"].metrics[
                            expected_glyph
                        ]
                        actual_v_advance = hb_font.get_glyph_v_advance(glyph_id)
                        if actual_v_advance != -expected_v_advance:
                            hard_failures.append(
                                {
                                    "weight": weight_value,
                                    "codepoint": f"U+{codepoint:04X}",
                                    "glyph": target_name,
                                    "reason": "vertical_advance",
                                    "actual": actual_v_advance,
                                    "expected": -expected_v_advance,
                                }
                            )
                            continue
                        if extents is not None:
                            _origin_x, origin_y = hb_font.get_glyph_v_origin(glyph_id)
                            actual_tsb = int(origin_y) - int(extents.y_bearing)
                            if actual_tsb != expected_tsb:
                                dy = actual_tsb - int(expected_tsb)
                                counts["vertical_side_bearing"] += 1
                    if not dx and not dy:
                        continue
                    glyph = font["glyf"][target_name]
                    glyph.expand(font["glyf"])
                    if not glyph.isComposite():
                        hard_failures.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "glyph": target_name,
                                "reason": "non_composite_side_bearing_mismatch",
                                "delta": (dx, dy),
                            }
                        )
                        continue
                    key = (weight_value, target_name)
                    previous = pending.get(key)
                    if previous is not None and previous != (dx, dy):
                        hard_failures.append(
                            {
                                "weight": weight_value,
                                "glyph": target_name,
                                "reason": "conflicting_alias_metric_targets",
                                "targets": [previous, (dx, dy)],
                            }
                        )
                        continue
                    pending[key] = (dx, dy)
            finally:
                expected.close()
            mismatches_by_weight[weight_value] = counts

        if hard_failures:
            raise RuntimeError(
                "Inter composite HarfBuzz metric alignment found unsupported failures: "
                + repr(hard_failures[:8])
            )
        if rounds == 0:
            initial_mismatches = mismatches_by_weight
        if not pending:
            return {
                "inter_composite_harfbuzz_metric_rounds": rounds,
                "inter_composite_harfbuzz_mismatches_before_by_weight": (
                    initial_mismatches
                ),
                "inter_composite_harfbuzz_translations_added": (
                    corrections
                ),
                "inter_composite_harfbuzz_maximum_translation": (
                    maximum_translation
                ),
                "inter_composite_harfbuzz_metric_mismatches_after": 0,
            }
        rounds += 1
        if rounds > 4:
            raise RuntimeError(
                "Inter composite HarfBuzz metric alignment did not converge: "
                + repr(mismatches_by_weight)
            )
        log_step(
            "VF Inter composite metrics: correction round "
            f"{rounds}, glyphs={len(pending)}"
        )

        default_targets = {
            glyph_name: delta
            for (weight_value, glyph_name), delta in pending.items()
            if weight_value == default_weight
        }
        if default_targets:
            ordered, _depth = glyph_component_dependency_order(
                font,
                default_targets,
            )
            for glyph_name in ordered:
                dx, dy = default_targets[glyph_name]
                glyph = font["glyf"][glyph_name]
                glyph.expand(font["glyf"])
                for component in glyph.components:
                    component.x += dx
                    component.y += dy
                glyph.recalcBounds(font["glyf"])
                corrections += 1
                maximum_translation = max(
                    maximum_translation,
                    abs(dx),
                    abs(dy),
                )
        for (weight_value, glyph_name), (dx, dy) in pending.items():
            if weight_value == default_weight:
                continue
            add_uniform_composite_translation_variation(
                font,
                glyph_name,
                supports[weight_value],
                dx,
                dy,
            )
            corrections += 1
            maximum_translation = max(maximum_translation, abs(dx), abs(dy))


def inter_outline_control_status(font: TTFont, inter: TTFont) -> dict[str, Any]:
    pairs = final_cmap_inter_outline_correction_pairs(font, inter)
    mismatches_by_weight: dict[int, int] = {}
    maximum_residual_by_weight: dict[int, float] = {}
    samples: list[dict[str, Any]] = []
    for weight_value in INTER_OUTLINE_AUDIT_WEIGHTS:
        current = instantiateVariableFont(
            font,
            {"wght": weight_value},
            inplace=False,
            optimize=True,
        )
        source = instantiateVariableFont(
            inter,
            {"wght": weight_value},
            inplace=False,
            optimize=True,
        )
        mismatches = 0
        maximum_residual = 0.0
        try:
            current_set = current.getGlyphSet()
            source_set = source.getGlyphSet()
            for source_name, target_name in pairs:
                reason, residual = translation_invariant_outline_residual(
                    decomposed_glyph_recording(source_set, source_name),
                    decomposed_glyph_recording(current_set, target_name),
                )
                if reason is None and math.isfinite(residual):
                    maximum_residual = max(maximum_residual, residual)
                if reason is None and residual <= INTER_OUTLINE_TRANSLATION_TOLERANCE:
                    continue
                mismatches += 1
                if len(samples) < 16:
                    samples.append(
                        {
                            "weight": weight_value,
                            "source_glyph": source_name,
                            "target_glyph": target_name,
                            "reason": reason,
                            "residual": residual,
                        }
                    )
        finally:
            current.close()
            source.close()
        mismatches_by_weight[weight_value] = mismatches
        maximum_residual_by_weight[weight_value] = maximum_residual
    return {
        "ok": bool(pairs) and not any(mismatches_by_weight.values()),
        "control_weights": list(INTER_OUTLINE_AUDIT_WEIGHTS),
        "pairs_checked_per_weight": len(pairs),
        "mismatches_by_weight": mismatches_by_weight,
        "maximum_translation_invariant_residual_by_weight": (
            maximum_residual_by_weight
        ),
        "failure_samples": samples,
    }


def harfbuzz_named_metric_status(
    path: Path,
    region: str,
    italic: bool,
) -> dict[str, Any]:
    data = path.read_bytes()
    face = hb.Face(data)
    hb_font = hb.Font(face)
    hb_font.scale = (face.upem, face.upem)
    counts_by_weight: dict[int, dict[str, int]] = {}
    samples: list[dict[str, Any]] = []
    compared_by_weight: dict[int, int] = {}
    for weight_name, weight_value in VF_METRIC_REFERENCE_STOPS:
        expected = open_project_static_metric_reference_font(
            region,
            weight_name,
            italic,
        )
        counts = {
            "missing_glyph": 0,
            "horizontal_advance": 0,
            "horizontal_side_bearing": 0,
            "vertical_advance": 0,
            "vertical_side_bearing": 0,
        }
        compared = 0
        hb_font.set_variations({"wght": weight_value})
        expected_path = static_dir(region, False) / static_output_name(region, weight_name, italic)
        expected_hb = hb.Font(hb.Face(expected_path.read_bytes()))
        try:
            for codepoint, expected_glyph in (expected.getBestCmap() or {}).items():
                glyph_id = hb_font.get_nominal_glyph(codepoint)
                if glyph_id is None:
                    counts["missing_glyph"] += 1
                    if len(samples) < 16:
                        samples.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "metric": "missing_glyph",
                            }
                        )
                    continue
                compared += 1
                expected_gid = expected_hb.get_nominal_glyph(codepoint)
                expected_extents = expected_hb.get_glyph_extents(expected_gid)
                expected_h_advance = expected_hb.get_glyph_h_advance(expected_gid)
                expected_lsb = expected_extents.x_bearing if expected_extents is not None else 0
                actual_h_advance = hb_font.get_glyph_h_advance(glyph_id)
                if actual_h_advance != expected_h_advance:
                    counts["horizontal_advance"] += 1
                    if len(samples) < 16:
                        samples.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "metric": "horizontal_advance",
                                "actual": actual_h_advance,
                                "expected": expected_h_advance,
                            }
                        )
                extents = hb_font.get_glyph_extents(glyph_id)
                if extents is not None and extents.x_bearing != expected_lsb:
                    counts["horizontal_side_bearing"] += 1
                    if len(samples) < 16:
                        samples.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "metric": "horizontal_side_bearing",
                                "actual": extents.x_bearing,
                                "expected": expected_lsb,
                            }
                        )
                if "vmtx" not in expected or expected_glyph not in expected["vmtx"].metrics:
                    continue
                expected_v_advance = -expected_hb.get_glyph_v_advance(expected_gid)
                expected_tsb = (
                    expected_hb.get_glyph_v_origin(expected_gid)[1] - expected_extents.y_bearing
                    if expected_extents is not None else 0
                )
                actual_v_advance = hb_font.get_glyph_v_advance(glyph_id)
                if actual_v_advance != -expected_v_advance:
                    counts["vertical_advance"] += 1
                    if len(samples) < 16:
                        samples.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "metric": "vertical_advance",
                                "actual": actual_v_advance,
                                "expected": -expected_v_advance,
                            }
                        )
                if extents is not None:
                    _origin_x, origin_y = hb_font.get_glyph_v_origin(glyph_id)
                    actual_tsb = origin_y - extents.y_bearing
                    if actual_tsb != expected_tsb:
                        counts["vertical_side_bearing"] += 1
                        if len(samples) < 16:
                            samples.append(
                                {
                                    "weight": weight_value,
                                    "codepoint": f"U+{codepoint:04X}",
                                    "metric": "vertical_side_bearing",
                                    "actual": actual_tsb,
                                    "expected": expected_tsb,
                                }
                            )
        finally:
            expected.close()
        counts_by_weight[weight_value] = counts
        compared_by_weight[weight_value] = compared
    return {
        "ok": all(
            not any(counts.values())
            for counts in counts_by_weight.values()
        ),
        "reference_mode": "harfbuzz-both-project-static-unhinted-and-vf",
        "counts_by_weight": counts_by_weight,
        "codepoints_compared_by_weight": compared_by_weight,
        "failure_samples": samples,
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


def add_vertical_advance_tuple_variation(
    font: TTFont,
    glyph_name: str,
    support: tuple[float, float, float],
    delta: int,
) -> None:
    if "gvar" not in font or not delta:
        return
    coordinates: list[Any] = [None] * gvar_coordinate_count(font, glyph_name)
    coordinates[-4:] = [(0, 0), (0, 0), (0, 0), (0, otRound(-delta))]
    font["gvar"].variations.setdefault(glyph_name, []).append(
        TupleVariation({"wght": support}, coordinates)
    )


def clear_glyph_metric_tuple_variations(font: TTFont, glyph_name: str) -> None:
    if "gvar" not in font:
        return
    for variation in font["gvar"].variations.get(glyph_name, []):
        if len(variation.coordinates) < 4:
            continue
        for index in range(len(variation.coordinates) - 4, len(variation.coordinates)):
            if variation.coordinates[index] is not None:
                variation.coordinates[index] = (0, 0)


def preserve_upstream_dash_metric_variations(
    target: TTFont,
    source: TTFont,
) -> dict[str, Any]:
    if "gvar" not in target or "fvar" not in target:
        return {"upstream_dash_metric_variations_preserved": False}
    target_roles = upstream_dash_roles(target)
    source_roles = upstream_dash_roles(source)
    role_pairs = [
        (role, target_roles[role], source_roles[role])
        for role in target_roles
    ]
    control_weights = sorted(SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT)
    default_weight = int(weight_axis(target).defaultValue)
    controls: dict[int, dict[str, tuple[int, int]]] = {}
    for weight in control_weights:
        glyph_set = source.getGlyphSet(location={"wght": weight})
        controls[weight] = {
            role: (
                otRound(glyph_set[source_name].width),
                otRound(getattr(glyph_set[source_name], "height", 0)),
            )
            for role, _target_name, source_name in role_pairs
        }

    if default_weight not in controls:
        raise ValueError(f"dash metric controls lack default weight {default_weight}")
    for role, target_name, _source_name in role_pairs:
        clear_glyph_metric_tuple_variations(target, target_name)
        default_h, default_v = controls[default_weight][role]
        _old_h, lsb = target["hmtx"].metrics[target_name]
        target["hmtx"].metrics[target_name] = (default_h, lsb)
        if "vmtx" in target and target_name in target["vmtx"].metrics:
            _old_v, tsb = target["vmtx"].metrics[target_name]
            target["vmtx"].metrics[target_name] = (default_v, tsb)

    correction_weights = [weight for weight in control_weights if weight != default_weight]
    supports = advance_supports(target, correction_weights)
    horizontal_added = 0
    vertical_added = 0
    for weight in correction_weights:
        support = supports[weight]
        for role, target_name, _source_name in role_pairs:
            default_h, default_v = controls[default_weight][role]
            target_h, target_v = controls[weight][role]
            if target_h != default_h:
                add_advance_tuple_variation(
                    target,
                    target_name,
                    support,
                    target_h - default_h,
                )
                horizontal_added += 1
            if target_v != default_v:
                add_vertical_advance_tuple_variation(
                    target,
                    target_name,
                    support,
                    target_v - default_v,
                )
                vertical_added += 1
    return {
        "upstream_dash_metric_variations_preserved": True,
        "upstream_dash_metric_control_weights": control_weights,
        "upstream_dash_horizontal_metric_variations_added": horizontal_added,
        "upstream_dash_vertical_metric_variations_added": vertical_added,
    }


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


def tabular_digit_alternates(font: TTFont) -> set[str]:
    tnum = get_single_substitution_mapping(font, "tnum")
    tags = {record.FeatureTag for record in font["GSUB"].table.FeatureList.FeatureRecord if record.FeatureTag == "zero" or record.FeatureTag.startswith(("ss", "cv"))}
    mappings = [get_single_substitution_mapping(font, tag) for tag in sorted(tags)]
    def closure(seeds: set[str]) -> set[str]:
        result = set(seeds)
        while True:
            additions = {mapping[name] for mapping in mappings for name in result if name in mapping} - result
            if not additions:
                return result
            result.update(additions)
    cmap = font.getBestCmap()
    digits = closure({cmap[cp] for cp in range(0x30, 0x3A)})
    return closure({tnum[name] for name in digits if name in tnum})


def align_tabular_alternate_advances(font: TTFont) -> dict[str, Any]:
    if "gvar" not in font or "HVAR" not in font:
        return {"tabular_alternate_advances_aligned": 0}
    names = tabular_digit_alternates(font)
    reference = tnum_digit_targets(font)[0x31]
    if reference not in names:
        raise RuntimeError("等宽数字参考字形不在替代字形闭包中")
    curves = []
    for item in font["gvar"].variations.get(reference, []):
        left = item.coordinates[-4] or (0, 0)
        right = item.coordinates[-3] or (0, 0)
        if right[0] != left[0]:
            curves.append((copy.deepcopy(item.axes), right[0] - left[0]))
    hvar = font["HVAR"].table
    order = font.getGlyphOrder()
    indices = dict(hvar.AdvWidthMap.mapping) if hvar.AdvWidthMap is not None else {name: index for index, name in enumerate(order)}
    reference_index = indices[reference]
    advance = font["hmtx"].metrics[reference][0]
    for name in names:
        indices[name] = reference_index
        font["hmtx"].metrics[name] = (advance, font["hmtx"].metrics[name][1])
        if name == reference:
            continue
        items = font["gvar"].variations.setdefault(name, [])
        for item in items:
            item.coordinates[-3] = item.coordinates[-4] or (0, 0)
        items[:] = [item for item in items if any(point not in (None, (0, 0)) for point in item.coordinates)]
        for axes, delta in curves:
            coordinates = [(0, 0)] * gvar_coordinate_count(font, name)
            coordinates[-3] = (delta, 0)
            items.append(TupleVariation(axes, coordinates))
    hvar.AdvWidthMap = var_builder.buildVarIdxMap([indices[name] for name in order], order)
    return {"tabular_alternate_advances_aligned": len(names), "tabular_alternates_share_hvar_advance": True}


def tabular_advance_structure_status(font: TTFont) -> dict[str, Any]:
    names = tabular_digit_alternates(font)
    table = font["HVAR"].table
    indices = {table.AdvWidthMap.mapping[name] if table.AdvWidthMap is not None else font.getGlyphID(name) for name in names}
    advances = {font["hmtx"].metrics[name][0] for name in names}
    return {"ok": len(names) >= 11 and len(indices) == len(advances) == 1, "glyphs": len(names), "advance_curves": len(indices), "default_advances": sorted(advances)}


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


def inter_static_source_path(weight_name: str, italic: bool) -> Path:
    style = str(STATIC_STYLE_SOURCES[weight_name]["inter"])
    if italic:
        style = "Italic" if style == "Regular" else f"{style}Italic"
    return SARASA_SOURCE_DIR / "sources" / "Inter" / f"Inter-{style}.ttf"


def scaled_hmtx_metric(
    metric: tuple[int, int],
    source_upem: int,
    target_upem: int,
) -> tuple[int, int]:
    return (
        otRound(metric[0] * target_upem / source_upem),
        otRound(metric[1] * target_upem / source_upem),
    )


def inter_static_product_hmtx(
    weight_name: str,
    italic: bool,
    target_upem: int,
) -> dict[tuple[str, int], tuple[int, int]]:
    path = inter_static_source_path(weight_name, italic)
    source = TTFont(path)
    try:
        source_upem = int(source["head"].unitsPerEm)
        cmap = source.getBestCmap() or {}
        tnum = tnum_digit_targets(source)
        metrics: dict[tuple[str, int], tuple[int, int]] = {}
        for codepoint in [*range(0x30, 0x3A), 0x3A]:
            proportional_glyph = cmap.get(codepoint)
            tabular_glyph = tnum.get(codepoint)
            if proportional_glyph in source["hmtx"].metrics:
                metrics[("proportional", codepoint)] = scaled_hmtx_metric(
                    source["hmtx"].metrics[proportional_glyph],
                    source_upem,
                    target_upem,
                )
            if tabular_glyph in source["hmtx"].metrics:
                metrics[("tabular", codepoint)] = scaled_hmtx_metric(
                    source["hmtx"].metrics[tabular_glyph],
                    source_upem,
                    target_upem,
                )
        return metrics
    finally:
        source.close()


def product_metric_targets(font: TTFont) -> dict[tuple[str, int], str]:
    cmap = font.getBestCmap() or {}
    targets: dict[tuple[str, int], str] = {}
    for codepoint in [*range(0x30, 0x3A), 0x3A]:
        glyph_name = cmap.get(codepoint)
        if glyph_name in font["hmtx"].metrics:
            targets[("proportional", codepoint)] = glyph_name
    for codepoint, glyph_name in tnum_digit_targets(font).items():
        targets[("tabular", codepoint)] = glyph_name
    return targets


def align_product_metrics_to_inter_static(font: TTFont, italic: bool) -> dict[str, int]:
    if "gvar" not in font or "fvar" not in font:
        return {
            "inter_static_product_default_metrics_aligned": 0,
            "inter_static_product_variations_added": 0,
            "inter_static_product_variation_corrections": 0,
        }
    target_upem = int(font["head"].unitsPerEm)
    source_metrics = {
        int(stop["value"]): inter_static_product_hmtx(
            str(stop["name"]),
            italic,
            target_upem,
        )
        for stop in SOURCE_HAN_WEIGHT_STOPS
    }
    targets = product_metric_targets(font)
    default_weight = int(weight_axis(font).defaultValue)
    default_aligned = 0
    for key, glyph_name in targets.items():
        metric = source_metrics[default_weight].get(key)
        if metric and tuple(font["hmtx"].metrics[glyph_name]) != metric:
            font["hmtx"].metrics[glyph_name] = metric
            default_aligned += 1

    correction_weights = sorted(weight for weight in source_metrics if weight != default_weight)
    supports = advance_supports(font, correction_weights)
    variations_added = 0
    corrections = 0
    for weight in correction_weights:
        instance = instantiateVariableFont(font, {"wght": weight}, inplace=False, optimize=True)
        try:
            pending: dict[str, tuple[int, int]] = {}
            for key, glyph_name in targets.items():
                desired = source_metrics[weight].get(key)
                current = instance["hmtx"].metrics.get(glyph_name)
                if desired is None or current is None:
                    continue
                delta = (desired[0] - current[0], desired[1] - current[1])
                if delta != (0, 0):
                    existing = pending.get(glyph_name)
                    if existing is not None and existing != delta:
                        raise ValueError(f"conflicting product metric correction for {glyph_name}")
                    pending[glyph_name] = delta
        finally:
            instance.close()
        support = supports[weight]
        for glyph_name, (advance_delta, lsb_delta) in pending.items():
            if advance_delta:
                add_advance_tuple_variation(font, glyph_name, support, advance_delta)
                variations_added += 1
            if lsb_delta:
                add_lsb_tuple_variation(font, glyph_name, support, lsb_delta)
                variations_added += 1
            corrections += 1
    return {
        "inter_static_product_default_metrics_aligned": default_aligned,
        "inter_static_product_variations_added": variations_added,
        "inter_static_product_variation_corrections": corrections,
    }


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


def freeze_horizontal_advance_preserve_bearings(
    font: TTFont,
    glyph_name: str,
) -> None:
    if "gvar" not in font:
        return
    for variation in font["gvar"].variations.get(glyph_name, []):
        if len(variation.coordinates) < 4:
            continue
        left_phantom = variation.coordinates[-4]
        variation.coordinates[-3] = (
            copy.deepcopy(left_phantom)
            if left_phantom is not None
            else (0, 0)
        )


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


def materialize_gvar_deltas(font: TTFont) -> dict[str, int]:
    if "gvar" not in font or "glyf" not in font or "hmtx" not in font:
        return {
            "italic_gvar_tuples_materialized": 0,
            "italic_gvar_implied_deltas_materialized": 0,
        }

    glyf = font["glyf"]
    h_metrics = font["hmtx"].metrics
    v_metrics = font["vmtx"].metrics if "vmtx" in font else None
    tuples_materialized = 0
    implied_deltas_materialized = 0
    for glyph_name, variations in font["gvar"].variations.items():
        result = glyf._getCoordinatesAndControls(glyph_name, h_metrics, v_metrics)
        if result is None:
            continue
        coordinates, controls = result
        end_points = (
            controls.endPts
            if controls.numberOfContours >= 1
            else list(range(len(controls.endPts)))
        )
        for variation in variations:
            implied_count = sum(delta is None for delta in variation.coordinates)
            if not implied_count:
                continue
            variation.calcInferredDeltas(coordinates, end_points)
            variation.roundDeltas()
            tuples_materialized += 1
            implied_deltas_materialized += implied_count

    return {
        "italic_gvar_tuples_materialized": tuples_materialized,
        "italic_gvar_implied_deltas_materialized": implied_deltas_materialized,
    }


def shear_font(font: TTFont, angle_degrees: float) -> dict[str, int]:
    report = materialize_gvar_deltas(font)
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
                # The final four gvar coordinates are horizontal and vertical
                # metric phantom points, not outline points. Shearing them would
                # couple vertical metric deltas into horizontal metrics.
                for index, xy in enumerate(variation.coordinates[:-4]):
                    if xy is None:
                        continue
                    x, y = xy
                    variation.coordinates[index] = otRound(x + y * shear), y
    return report


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


def assert_public_axis_ready_for_inter_remap(base: TTFont) -> dict[str, Any]:
    if not getattr(base, "_sarasa_public_weight_axis_applied", False):
        raise RuntimeError(
            "Inter gvar supports must be remapped after apply_public_weight_axis(); "
            "otherwise Inter weights are pinned to Source Han internal coordinates"
        )
    axis = weight_axis(base)
    actual_axis = (axis.minValue, axis.defaultValue, axis.maxValue)
    expected_axis = tuple(PUBLIC_AXIS_LIMIT["wght"])
    if actual_axis != expected_axis:
        raise RuntimeError(f"Inter gvar remap expected public axis {expected_axis}, got {actual_axis}")
    if "avar" not in base or "wght" not in base["avar"].segments:
        raise RuntimeError("Inter gvar remap requires the public Source Han avar mapping")
    segment = base["avar"].segments["wght"]
    expected_segment = getattr(base, "_sarasa_public_weight_avar_segment", None)
    if expected_segment is None:
        raise RuntimeError("Inter gvar remap is missing the public avar runtime contract")
    if len(segment) != len(expected_segment):
        raise RuntimeError(
            "Inter gvar remap public avar was replaced after apply_public_weight_axis()"
        )
    mismatched_anchors = []
    for expected_key, expected_value in expected_segment.items():
        match = next(
            (
                actual_value
                for actual_key, actual_value in segment.items()
                if abs(actual_key - expected_key) <= 1 / 65536
            ),
            None,
        )
        if match is None or abs(match - expected_value) > 1 / 65536:
            mismatched_anchors.append(expected_key)
    if mismatched_anchors:
        raise RuntimeError(
            "Inter gvar remap public avar changed after apply_public_weight_axis(): "
            f"{mismatched_anchors}"
        )
    return {
        "inter_gvar_remap_public_axis_verified": True,
        "inter_gvar_remap_public_axis": list(actual_axis),
        "inter_gvar_remap_public_avar_verified": True,
        "inter_gvar_remap_public_avar_anchors": sorted(SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT),
    }


def remap_inter_gvar_supports(base: TTFont, inter: TTFont) -> dict[str, Any]:
    report = assert_public_axis_ready_for_inter_remap(base)
    if "gvar" not in inter or "fvar" not in inter or "fvar" not in base:
        return report
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
    return report


def load_base(region: str, italic: bool, inter_unicodes: set[int]) -> tuple[TTFont, dict[str, Any]]:
    base = TTFont(source_han_vf_path(region))
    base = instantiateVariableFont(base, AXIS_LIMIT, inplace=False, optimize=True)
    sarasa_report: dict[str, Any] = {}
    override_path = classical_vf_override_path(region)
    override: TTFont | None = None
    try:
        if override_path:
            override = TTFont(override_path)
            override = instantiateVariableFont(override, AXIS_LIMIT, inplace=False, optimize=True)
            sarasa_report.update(apply_classical_vf_override(base, override, region))
        public_axis_report = apply_public_weight_axis(base)
        metric_source = base
        if override is not None:
            apply_public_weight_axis(override)
            metric_source = override
        sarasa_report.update(
            preserve_upstream_dash_metric_variations(base, metric_source)
        )
    finally:
        if override is not None:
            override.close()
    subset_font(base, source_han_unicodes_like_sarasa(region, base, inter_unicodes))
    sarasa_report.update(bake_source_han_pwid_and_sanitize(base))
    sarasa_report.update(public_axis_report)
    sarasa_report["hangul_widths_normalized"] = normalize_hangul_widths(base)
    if italic:
        sarasa_report.update(shear_font(base, 9.4))
    return base, sarasa_report


def load_inter(italic: bool) -> TTFont:
    inter = TTFont(INTER_ITALIC if italic else INTER_UPRIGHT)
    inter = instantiateVariableFont(inter, INTER_AXIS_LIMIT, inplace=False, optimize=True)
    scale_upem(inter, 1000)
    bake_inter_feature_defaults(inter)
    bake_inter_ui_tnum_defaults(inter)
    return inter


def bake_inter_feature_defaults(font: TTFont) -> dict[str, str]:
    # Sarasa bakes these presets by changing cmap identities. This preserves
    # each alternate's kerning/anchors and leaves component glyphs intact.
    effective = {name: name for name in font.getBestCmap().values()}
    for tag in ("ss03", "cv10"):
        substitutions = get_single_substitution_mapping(font, tag)
        effective = {original: substitutions.get(current, current) for original, current in effective.items()}
    effective = {original: target for original, target in effective.items() if original != target}
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap = {codepoint: effective.get(name, name) for codepoint, name in table.cmap.items()}
    return effective


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
    # This must run after load_base() exposes the public axis. The remap keeps
    # Inter wght=W at public W even when Source Han uses a different avar map.
    remap_report = remap_inter_gvar_supports(base, inter)
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
        **remap_report,
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




def import_name_id(
    target_font: TTFont,
    source_font: TTFont,
    source_name_id: int,
    remap: dict[int, int],
) -> tuple[int, int]:
    if source_name_id in remap:
        return remap[source_name_id], 0

    source_records = [record for record in source_font["name"].names if record.nameID == source_name_id]
    if not source_records:
        return source_name_id, 0

    target_name = target_font["name"]
    target_records = [record for record in target_name.names if record.nameID == source_name_id]
    source_signature = {
        (record.platformID, record.platEncID, record.langID, record.toUnicode()) for record in source_records
    }
    target_signature = {
        (record.platformID, record.platEncID, record.langID, record.toUnicode()) for record in target_records
    }
    if target_records and target_signature != source_signature:
        used_ids = {record.nameID for record in target_name.names}
        target_name_id = 256
        while target_name_id in used_ids:
            target_name_id += 1
    else:
        target_name_id = source_name_id

    existing_keys = {
        (record.nameID, record.platformID, record.platEncID, record.langID) for record in target_name.names
    }
    imported = 0
    for source_record in source_records:
        key = (target_name_id, source_record.platformID, source_record.platEncID, source_record.langID)
        if key in existing_keys:
            continue
        record = copy.deepcopy(source_record)
        record.nameID = target_name_id
        target_name.names.append(record)
        existing_keys.add(key)
        imported += 1
    remap[source_name_id] = target_name_id
    return target_name_id, imported


def import_layout_feature_names(
    target_font: TTFont,
    source_font: TTFont,
    feature: Any,
    remap: dict[int, int],
) -> int:
    params = getattr(feature, "FeatureParams", None)
    if params is None:
        return 0
    imported = 0
    for field, value in vars(params).items():
        if not field.endswith("NameID") or not isinstance(value, int) or value < 256:
            continue
        target_name_id, count = import_name_id(target_font, source_font, value, remap)
        setattr(params, field, target_name_id)
        imported += count
    return imported


def import_inter_gpos_variations(base: TTFont, inter: TTFont) -> dict[int, int]:
    """Re-express Inter's positioning in the public axis and merge its store."""
    assert_public_axis_ready_for_inter_remap(base)
    inter["GPOS"].ensureDecompiled()
    devices = collect_ot_variation_devices(inter["GPOS"].table)
    indices = sorted({(device.StartSize << 16) | device.EndSize for device in devices} - {0xFFFFFFFF})
    if not indices:
        return {0xFFFFFFFF: 0xFFFFFFFF}
    store = getattr(inter["GDEF"].table, "VarStore", None) if "GDEF" in inter else None
    if store is None:
        raise ValueError("Inter GPOS 变化引用缺少 GDEF VarStore")
    if [axis.axisTag for axis in inter["fvar"].axes] != ["wght"]:
        raise ValueError("导入 Inter GPOS 前必须固定 opsz，仅保留 wght")
    axis = weight_axis(inter)
    inter_segment = inter["avar"].segments["wght"] if "avar" in inter else {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    base_axis = weight_axis(base)
    base_segment = base["avar"].segments["wght"]
    weights = set(SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT)
    # Include every breakpoint of both coordinate mappings and every source
    # region. Merely moving a tent's three endpoints loses its linear pieces.
    weights.update(denormalize_axis_value(value, axis.minValue, axis.defaultValue, axis.maxValue) for value in inter_segment)
    weights.update(denormalize_axis_value(value, base_axis.minValue, base_axis.defaultValue, base_axis.maxValue) for value in base_segment)
    for region in store.VarRegionList.Region:
        support = region.VarRegionAxis[0]
        for value in (support.StartCoord, support.PeakCoord, support.EndCoord):
            weights.add(denormalize_axis_value(inverse_piecewise_map(value, inter_segment), axis.minValue, axis.defaultValue, axis.maxValue))
    weights = sorted(weight for weight in weights if base_axis.minValue <= weight <= base_axis.maxValue)
    by_location = {
        floatToFixedToFloat(vf_mapped_normalized_weight(base, weight), 14): weight
        for weight in weights
    }
    for weight in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT:
        by_location[floatToFixedToFloat(vf_mapped_normalized_weight(base, weight), 14)] = weight
    locations = sorted(by_location)
    weights = [by_location[value] for value in locations]
    model = VariationModel([{"wght": value} if value else {} for value in locations], axisOrder=["wght"])
    instancers = [VarStoreInstancer(store, inter["fvar"].axes, {"wght": vf_mapped_normalized_weight(inter, weight)}) for weight in weights]
    builder = OnlineVarStoreBuilder(["wght"])
    builder.setModel(model)
    remap = {}
    for index in indices:
        default, new_index = builder.storeMasters([instancer[index] for instancer in instancers], round=otRound)
        if default:
            raise ValueError("Inter GPOS 变化数据在默认坐标不为零")
        remap[index] = new_index
    additional = builder.finish()
    if "GDEF" not in base:
        base["GDEF"] = newTable("GDEF")
        base["GDEF"].table = ot.GDEF()
    gdef = base["GDEF"].table
    existing = getattr(gdef, "VarStore", None)
    if existing is not None:
        merged, merged_indices = append_item_variation_store(existing, additional, [remap[index] for index in indices])
        remap = dict(zip(indices, merged_indices))
    else:
        merged = additional
    gdef.Version = 0x00010003
    gdef.VarStore = merged
    remap[0xFFFFFFFF] = 0xFFFFFFFF
    return remap


def remap_layout_variation_devices(table: Any, mapping: dict[int, int]) -> None:
    transformed = {}
    def visit(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return value
        if id(value) in transformed:
            return transformed[id(value)]
        if isinstance(value, ot.Device) and getattr(value, "DeltaFormat", None) == 0x8000:
            index = mapping[(value.StartSize << 16) | value.EndSize]
            # The store builder uses NO_VARIATION_INDEX for a constant zero
            # delta. A null Device offset expresses that without a reference.
            if index == 0xFFFFFFFF:
                transformed[id(value)] = None
                return None
            value.StartSize, value.EndSize = index >> 16, index & 0xFFFF
            transformed[id(value)] = value
            return value
        transformed[id(value)] = value
        if isinstance(value, list):
            value[:] = [visit(item) for item in value]
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                value[key] = visit(item)
        elif hasattr(value, "__dict__"):
            for key, item in list(vars(value).items()):
                setattr(value, key, visit(item))
        return value
    visit(table)


def append_layout_features(
    base: TTFont,
    inter: TTFont,
    table_tag: str,
    feature_tags: set[str],
) -> dict[str, int]:
    if table_tag not in inter:
        return {
            f"inter_{table_tag.lower()}_features_imported": 0,
            f"inter_{table_tag.lower()}_lookups_imported": 0,
            f"inter_{table_tag.lower()}_feature_names_imported": 0,
        }
    variation_map = import_inter_gpos_variations(base, inter) if table_tag == "GPOS" else {}
    if table_tag not in base:
        base[table_tag] = copy.deepcopy(inter[table_tag])
        rename = {name: prefixed(name) for name in inter.getGlyphOrder() if name != ".notdef"}
        rename_ot_glyph_references(base[table_tag].table, rename)
        for lookup in base[table_tag].table.LookupList.Lookup:
            if lookup.LookupFlag & 0x0010:
                lookup.MarkFilteringSet = base._sarasa_inter_mark_filter_map[lookup.MarkFilteringSet]
        if table_tag == "GPOS":
            remap_layout_variation_devices(base[table_tag].table, variation_map)
        name_id_remap: dict[int, int] = {}
        names_imported = 0
        if base[table_tag].table.FeatureList:
            for record in base[table_tag].table.FeatureList.FeatureRecord:
                names_imported += import_layout_feature_names(base, inter, record.Feature, name_id_remap)
        return {
            f"inter_{table_tag.lower()}_features_imported": len(base[table_tag].table.FeatureList.FeatureRecord)
            if base[table_tag].table.FeatureList
            else 0,
            f"inter_{table_tag.lower()}_lookups_imported": len(base[table_tag].table.LookupList.Lookup)
            if base[table_tag].table.LookupList
            else 0,
            f"inter_{table_tag.lower()}_feature_names_imported": names_imported,
        }

    inter[table_tag].ensureDecompiled()
    source = inter[table_tag].table
    target = base[table_tag].table
    if not source.FeatureList or not source.LookupList:
        return {
            f"inter_{table_tag.lower()}_features_imported": 0,
            f"inter_{table_tag.lower()}_lookups_imported": 0,
            f"inter_{table_tag.lower()}_feature_names_imported": 0,
        }
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
    lookup_indices_set = {index for record in feature_records for index in record.Feature.LookupListIndex}
    pending = list(lookup_indices_set)
    while pending:
        for record in layout_lookup_records(source.LookupList.Lookup[pending.pop()]):
            if record.LookupListIndex not in lookup_indices_set:
                lookup_indices_set.add(record.LookupListIndex)
                pending.append(record.LookupListIndex)
    lookup_indices = sorted(lookup_indices_set)
    lookup_index_map = {index: len(target.LookupList.Lookup) + offset for offset, index in enumerate(lookup_indices)}
    for old_index in lookup_indices:
        lookup = copy.deepcopy(source.LookupList.Lookup[old_index])
        rename_ot_glyph_references(lookup, rename)
        if lookup.LookupFlag & 0x0010:
            lookup.MarkFilteringSet = base._sarasa_inter_mark_filter_map[lookup.MarkFilteringSet]
        for record in layout_lookup_records(lookup):
            record.LookupListIndex = lookup_index_map[record.LookupListIndex]
        if table_tag == "GPOS":
            remap_layout_variation_devices(lookup, variation_map)
        new_index = len(target.LookupList.Lookup)
        target.LookupList.Lookup.append(lookup)
        lookup_index_map[old_index] = new_index
    target.LookupList.LookupCount = len(target.LookupList.Lookup)

    imported_tags: set[str] = set()
    name_id_remap: dict[int, int] = {}
    names_imported = 0
    for source_record in feature_records:
        record = copy.deepcopy(source_record)
        names_imported += import_layout_feature_names(base, inter, record.Feature, name_id_remap)
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
        f"inter_{table_tag.lower()}_feature_names_imported": names_imported,
    }


def layout_lookup_records(value: Any, seen: set[int] | None = None) -> list[Any]:
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return []
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, (ot.SubstLookupRecord, ot.PosLookupRecord)):
        return [value]
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    elif hasattr(value, "__dict__"):
        children = [child for key, child in vars(value).items() if key not in {"reader", "font"}]
    else:
        return []
    return [record for child in children for record in layout_lookup_records(child, seen)]


def import_inter_layout_features(base: TTFont, inter: TTFont) -> dict[str, int]:
    report: dict[str, int] = {}
    report.update(import_inter_gdef_classes(base, inter))
    report.update(append_layout_features(base, inter, "GSUB", INTER_GSUB_FEATURES))
    report.update(append_layout_features(base, inter, "GPOS", INTER_GPOS_FEATURES))
    return report


def import_inter_gdef_classes(base: TTFont, inter: TTFont) -> dict[str, int]:
    if "GDEF" not in inter:
        return {"inter_gdef_mark_sets_imported": 0}
    if "GDEF" not in base:
        base["GDEF"] = newTable("GDEF")
        base["GDEF"].table = ot.GDEF()
        base["GDEF"].table.Version = 0x00010000
    target = base["GDEF"].table
    source = inter["GDEF"].table
    rename = {name: prefixed(name) for name in inter.getGlyphOrder() if name != ".notdef"}
    if getattr(source, "GlyphClassDef", None):
        if not getattr(target, "GlyphClassDef", None):
            target.GlyphClassDef = ot.ClassDef(); target.GlyphClassDef.classDefs = {}
        target.GlyphClassDef.classDefs.update({rename[name]: value for name, value in source.GlyphClassDef.classDefs.items() if name in rename})
    source_sets = getattr(source, "MarkGlyphSetsDef", None)
    mapping = {}
    if source_sets:
        if not getattr(target, "MarkGlyphSetsDef", None):
            target.MarkGlyphSetsDef = ot.MarkGlyphSetsDef()
            target.MarkGlyphSetsDef.MarkSetTableFormat = 1
            target.MarkGlyphSetsDef.Coverage = []
        for index, coverage in enumerate(source_sets.Coverage):
            mapping[index] = len(target.MarkGlyphSetsDef.Coverage)
            cloned = copy.deepcopy(coverage)
            cloned.glyphs = [rename[name] for name in cloned.glyphs if name in rename]
            target.MarkGlyphSetsDef.Coverage.append(cloned)
        target.MarkGlyphSetsDef.MarkSetCount = len(target.MarkGlyphSetsDef.Coverage)
        target.Version = max(target.Version, 0x00010002)
    base._sarasa_inter_mark_filter_map = mapping
    return {"inter_gdef_mark_sets_imported": len(mapping)}


def remove_metric_variation_maps(font: TTFont) -> None:
    for tag in ("HVAR", "VVAR"):
        if tag in font:
            del font[tag]


def metric_variation_control_locations(
    font: TTFont,
) -> tuple[list[int], list[dict[str, float]]]:
    weights = sorted(int(weight) for weight in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT)
    locations: list[dict[str, float]] = []
    for weight in weights:
        normalized = floatToFixedToFloat(vf_mapped_normalized_weight(font, weight), 14)
        locations.append({} if normalized == 0 else {"wght": normalized})
    return weights, locations


def build_direct_hvar(
    font: TTFont,
    model: VariationModel,
    master_metrics: list[list[tuple[int, int, int]]],
    base_metrics: list[tuple[int, int, int]],
) -> dict[str, Any]:
    glyph_order = font.getGlyphOrder()
    supports = model.supports[1:]
    region_list = var_builder.buildVarRegionList(supports, ["wght"])
    var_data_by_metric = {
        metric: var_builder.buildVarData(
            list(range(len(supports))),
            [],
            optimize=False,
        )
        for metric in ("advance", "lsb", "rsb")
    }
    default_master_index = model.reverseMapping[0]
    for glyph_id, glyph_name in enumerate(glyph_order):
        glyph_master_metrics = [metrics[glyph_id] for metrics in master_metrics]
        if base_metrics[glyph_id] != master_metrics[default_master_index][glyph_id]:
            raise ValueError(
                f"HVAR default metric mismatch for {glyph_name}: "
                f"{base_metrics[glyph_id]} != "
                f"{master_metrics[default_master_index][glyph_id]}"
            )
        for metric_index, metric in enumerate(("advance", "lsb", "rsb")):
            values = [values[metric_index] for values in glyph_master_metrics]
            deltas, glyph_supports = model.getDeltasAndSupports(values, round=round)
            if glyph_supports[1:] != supports:
                raise ValueError(f"HVAR support model mismatch for {glyph_name} {metric}")
            var_data_by_metric[metric].addItem(deltas[1:], round=round)

    table = font["HVAR"] = newTable("HVAR")
    hvar = table.table = ot.HVAR()
    hvar.Version = 0x00010000
    hvar.VarStore = var_builder.buildVarStore(
        region_list,
        [var_data_by_metric[metric] for metric in ("advance", "lsb", "rsb")],
    )
    # One direct item per GID avoids shared-map ambiguity during exact-weight
    # calibration and remains compact for this single-axis family.
    hvar.AdvWidthMap = None
    hvar.LsbMap = var_builder.buildVarIdxMap(
        [(1 << 16) | glyph_id for glyph_id in range(len(glyph_order))],
        glyph_order,
    )
    hvar.RsbMap = var_builder.buildVarIdxMap(
        [(2 << 16) | glyph_id for glyph_id in range(len(glyph_order))],
        glyph_order,
    )
    return var_data_by_metric


def horizontal_glyph_metrics(font: TTFont, glyph_name: str) -> tuple[int, int, int]:
    advance, lsb = font["hmtx"].metrics[glyph_name]
    bounds = glyph_bbox(font, glyph_name)
    outline_width = 0 if bounds is None else bounds[2] - bounds[0]
    rsb = int(advance) - int(lsb) - int(outline_width)
    return int(advance), int(lsb), rsb


def translate_default_glyph_outline(font: TTFont, glyph_name: str, dx: int) -> None:
    if not dx:
        return
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    glyph.expand(glyf)
    if glyph.isComposite():
        for component in glyph.components:
            component.x += dx
    else:
        glyph.coordinates.translate((dx, 0))
    glyph.recalcBounds(glyf)


def glyph_component_dependency_order(
    font: TTFont,
    glyph_names: Iterable[str],
) -> tuple[list[str], int]:
    glyf = font["glyf"]
    depths: dict[str, int] = {}
    active: set[str] = set()

    def component_depth(glyph_name: str) -> int:
        if glyph_name in depths:
            return depths[glyph_name]
        if glyph_name in active:
            raise ValueError(f"cyclic composite glyph dependency at {glyph_name}")
        active.add(glyph_name)
        glyph = glyf.glyphs[glyph_name]
        if hasattr(glyph, "expand"):
            glyph.expand(glyf)
        if glyph.isComposite():
            depth = 1 + max(
                (
                    component_depth(component.glyphName)
                    for component in glyph.components
                    if component.glyphName in glyf.glyphs
                ),
                default=-1,
            )
        else:
            depth = 0
        active.remove(glyph_name)
        depths[glyph_name] = depth
        return depth

    glyph_order = font.getGlyphOrder()
    order_index = {glyph_name: index for index, glyph_name in enumerate(glyph_order)}
    requested = list(glyph_names)
    ordered = sorted(
        requested,
        key=lambda glyph_name: (component_depth(glyph_name), order_index[glyph_name]),
    )
    return ordered, max((depths[glyph_name] for glyph_name in requested), default=0)


def align_default_outlines_to_lsb_targets(
    font: TTFont,
    targets: dict[str, int],
) -> tuple[dict[str, int], dict[str, int]]:
    before = control_lsb_xmin_mismatches(font, targets)
    ordered_glyphs, max_component_depth = glyph_component_dependency_order(font, targets)
    translated = 0
    induced_compensations = 0
    for glyph_name in ordered_glyphs:
        bounds = glyph_bbox(font, glyph_name)
        if bounds is None:
            continue
        delta = int(targets[glyph_name]) - int(bounds[0])
        if not delta:
            continue
        translated += 1
        induced_compensations += int(glyph_name not in before)
        translate_default_glyph_outline(font, glyph_name, delta)

    after = control_lsb_xmin_mismatches(font, targets)
    return before, {
        "vf_default_outline_translations": translated,
        "vf_default_component_dependency_compensations": induced_compensations,
        "vf_default_component_max_depth": max_component_depth,
        "vf_default_lsb_xmin_mismatches_after": len(after),
    }


def add_outline_translation_variation(
    font: TTFont,
    glyph_name: str,
    support: tuple[float, float, float],
    dx: int,
) -> None:
    if not dx:
        return
    control = glyph_variation_control_coordinates(font, glyph_name)
    if control is None:
        return
    real_point_count = len(control[1])
    coordinate_count = gvar_coordinate_count(font, glyph_name)
    if coordinate_count < real_point_count + 4:
        raise ValueError(f"invalid gvar point count for {glyph_name}")
    coordinates: list[Any] = [(dx, 0)] * real_point_count
    coordinates.extend([None] * (coordinate_count - real_point_count - 4))
    # HVAR carries the intended advance and side bearings. Moving horizontal
    # phantom points here would move the glyph origin with the outline and
    # cancel the x-bearing correction in shaping engines.
    coordinates.extend([(0, 0), (0, 0), (0, 0), (0, 0)])
    font["gvar"].variations.setdefault(glyph_name, []).append(
        TupleVariation({"wght": support}, coordinates)
    )


def lsb_xmin_mismatches(font: TTFont) -> dict[str, int]:
    mismatches: dict[str, int] = {}
    for glyph_name in font.getGlyphOrder():
        bounds = glyph_bbox(font, glyph_name)
        if bounds is None:
            continue
        lsb = int(font["hmtx"].metrics[glyph_name][1])
        if lsb != int(bounds[0]):
            mismatches[glyph_name] = lsb - int(bounds[0])
    return mismatches


def reference_cmap_metric_targets(
    font: TTFont,
    reference: TTFont,
    table_tag: str,
) -> dict[str, tuple[int, int]]:
    if table_tag not in font or table_tag not in reference:
        return {}
    cmap = font.getBestCmap() or {}
    reference_cmap = reference.getBestCmap() or {}
    targets: dict[str, tuple[int, int]] = {}
    for codepoint in sorted(set(cmap) & set(reference_cmap)):
        glyph_name = cmap[codepoint]
        reference_name = reference_cmap[codepoint]
        if reference_name not in reference[table_tag].metrics:
            continue
        metrics = tuple(int(value) for value in reference[table_tag].metrics[reference_name])
        previous = targets.get(glyph_name)
        if previous is not None and previous != metrics:
            raise ValueError(
                f"conflicting {table_tag} reference metrics for {glyph_name} at "
                f"U+{codepoint:04X}: {previous} != {metrics}"
            )
        targets[glyph_name] = metrics
    return targets


def glyph_vertical_origin(font: TTFont, glyph_name: str) -> int:
    bounds = glyph_bbox(font, glyph_name)
    if "VORG" in font:
        return int(
            font["VORG"].VOriginRecords.get(
                glyph_name,
                font["VORG"].defaultVertOriginY,
            )
        )
    y_max = 0 if bounds is None else int(bounds[3])
    return y_max + int(font["vmtx"].metrics[glyph_name][1])


def set_glyph_vertical_origin(font: TTFont, glyph_name: str, value: int) -> None:
    if "VORG" not in font:
        vorg = newTable("VORG")
        vorg.majorVersion = 1
        vorg.minorVersion = 0
        vorg.defaultVertOriginY = 880
        vorg.VOriginRecords = {}
        font["VORG"] = vorg
    vorg = font["VORG"]
    if int(value) == int(vorg.defaultVertOriginY):
        vorg.VOriginRecords.pop(glyph_name, None)
    else:
        vorg.VOriginRecords[glyph_name] = int(value)


def apply_default_vf_metric_reference(
    font: TTFont,
    reference: TTFont,
) -> dict[str, int]:
    h_targets = reference_cmap_metric_targets(font, reference, "hmtx")
    v_targets = reference_cmap_metric_targets(font, reference, "vmtx")
    hmtx_updated = 0
    vmtx_updated = 0
    vorg_updated = 0
    for glyph_name in font.getGlyphOrder():
        bounds = glyph_bbox(font, glyph_name)
        if bounds is not None:
            target_lsb = h_targets.get(
                glyph_name,
                (font["hmtx"].metrics[glyph_name][0], int(bounds[0])),
            )[1]
            advance = h_targets.get(glyph_name, font["hmtx"].metrics[glyph_name])[0]
            target_hmtx = (int(advance), int(target_lsb))
            if tuple(font["hmtx"].metrics[glyph_name]) != target_hmtx:
                font["hmtx"].metrics[glyph_name] = target_hmtx
                hmtx_updated += 1

        if "vmtx" not in font:
            continue
        if glyph_name in v_targets:
            target_vmtx = v_targets[glyph_name]
        elif bounds is not None:
            advance = int(font["vmtx"].metrics[glyph_name][0])
            target_vmtx = (advance, glyph_vertical_origin(font, glyph_name) - int(bounds[3]))
        else:
            target_vmtx = tuple(font["vmtx"].metrics[glyph_name])
        if tuple(font["vmtx"].metrics[glyph_name]) != target_vmtx:
            font["vmtx"].metrics[glyph_name] = target_vmtx
            vmtx_updated += 1

    origins: dict[str, int] = {}
    for glyph_name, (_advance, target_tsb) in v_targets.items():
        bounds = glyph_bbox(font, glyph_name)
        origins[glyph_name] = (
            glyph_vertical_origin(font, glyph_name)
            if bounds is None
            else int(bounds[3]) + int(target_tsb)
        )
    for glyph_name, origin in origins.items():
        if glyph_vertical_origin(font, glyph_name) != origin:
            set_glyph_vertical_origin(font, glyph_name, origin)
            vorg_updated += 1
    return {
        "vf_default_hmtx_reference_updates": hmtx_updated,
        "vf_default_vmtx_reference_updates": vmtx_updated,
        "vf_default_vorg_reference_updates": vorg_updated,
        "vf_default_reference_glyphs": len(h_targets),
    }


def target_lsb_for_control(
    instance: TTFont,
    reference: TTFont | None,
) -> dict[str, int]:
    targets = {
        glyph_name: int(bounds[0])
        for glyph_name in instance.getGlyphOrder()
        if (bounds := glyph_bbox(instance, glyph_name)) is not None
    }
    if reference is not None:
        targets.update(
            {
                glyph_name: int(metrics[1])
                for glyph_name, metrics in reference_cmap_metric_targets(
                    instance,
                    reference,
                    "hmtx",
                ).items()
            }
        )
    return targets


def control_lsb_xmin_mismatches(
    instance: TTFont,
    targets: dict[str, int],
) -> dict[str, int]:
    mismatches: dict[str, int] = {}
    for glyph_name, target_lsb in targets.items():
        bounds = glyph_bbox(instance, glyph_name)
        if bounds is not None and int(bounds[0]) != int(target_lsb):
            mismatches[glyph_name] = int(target_lsb) - int(bounds[0])
    return mismatches


def align_variable_outlines_to_lsb(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
) -> dict[str, Any]:
    if "fvar" not in font or "gvar" not in font or "glyf" not in font:
        raise ValueError("variable LSB/xMin alignment requires fvar, gvar and glyf")
    default_weight = int(weight_axis(font).defaultValue)
    default_reference = reference_fonts.get(default_weight)
    default_metric_report = (
        apply_default_vf_metric_reference(font, default_reference)
        if default_reference is not None
        else {}
    )
    default_targets = target_lsb_for_control(font, default_reference)
    before, default_outline_report = align_default_outlines_to_lsb_targets(
        font,
        default_targets,
    )
    if default_outline_report["vf_default_lsb_xmin_mismatches_after"]:
        raise RuntimeError(
            "VF default LSB/xMin dependency alignment failed for "
            f"{default_outline_report['vf_default_lsb_xmin_mismatches_after']} glyphs"
        )

    correction_weights = [
        weight
        for weight in sorted(SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT)
        if weight != default_weight
    ]
    supports = advance_supports(font, correction_weights)
    variation_corrections = 0
    rounds = 0
    while True:
        pending: list[tuple[int, str, int]] = []
        control_mismatches: dict[int, int] = {}
        log_step(
            f"VF LSB/xMin: inspect control points after {rounds} correction round(s)"
        )
        for weight in correction_weights:
            instance = instantiateVariableFont(
                font,
                {"wght": weight},
                inplace=False,
                optimize=True,
            )
            try:
                targets = target_lsb_for_control(
                    instance,
                    reference_fonts.get(weight),
                )
                mismatches = control_lsb_xmin_mismatches(instance, targets)
            finally:
                instance.close()
            control_mismatches[weight] = len(mismatches)
            pending.extend((weight, glyph_name, delta) for glyph_name, delta in mismatches.items())
        if not pending:
            break
        rounds += 1
        if rounds > 4:
            raise RuntimeError(
                "VF LSB/xMin control-point alignment did not converge: "
                + json.dumps(control_mismatches, sort_keys=True)
            )
        for weight, glyph_name, delta in pending:
            add_outline_translation_variation(
                font,
                glyph_name,
                supports[weight],
                delta,
            )
        variation_corrections += len(pending)
        log_step(
            f"VF LSB/xMin: applied {len(pending)} corrections in round {rounds}"
        )

    final_default = control_lsb_xmin_mismatches(font, default_targets)
    if final_default:
        raise RuntimeError(
            f"VF default LSB/xMin alignment failed for {len(final_default)} glyphs"
        )
    previous_flags = int(font["head"].flags)
    font["head"].flags = (previous_flags | 0x0002) & ~0x0020
    return {
        "vf_default_lsb_xmin_mismatches_before": len(before),
        "vf_control_lsb_xmin_variation_corrections": variation_corrections,
        "vf_lsb_xmin_alignment_rounds": rounds,
        "vf_head_flags_before_lsb_alignment": previous_flags,
        "vf_head_flags_after_lsb_alignment": int(font["head"].flags),
        **default_outline_report,
        **default_metric_report,
    }


def hvar_metrics_at_weight(
    font: TTFont,
    weight: int,
    base_metrics: list[tuple[int, int, int]] | None = None,
) -> dict[str, list[int]]:
    hvar = font["HVAR"].table
    normalized = vf_mapped_normalized_weight(font, weight)
    location = {} if normalized == 0 else {"wght": normalized}
    instancer = VarStoreInstancer(hvar.VarStore, font["fvar"].axes, location)
    glyph_order = font.getGlyphOrder()
    if base_metrics is None:
        base_metrics = [
            horizontal_glyph_metrics(font, glyph_name) for glyph_name in glyph_order
        ]
    result: dict[str, list[int]] = {}
    for metric_index, metric in enumerate(("advance", "lsb", "rsb")):
        var_data = hvar.VarStore.VarData[metric_index]
        scalars = [instancer._getScalar(index) for index in var_data.VarRegionIndex]
        result[metric] = [
            base_metrics[glyph_id][metric_index]
            + otRound(VarStoreInstancer.interpolateFromDeltasAndScalars(deltas, scalars))
            for glyph_id, deltas in enumerate(var_data.Item)
        ]
    return result


def vertical_glyph_metrics(font: TTFont, glyph_name: str) -> tuple[int, int, int, int]:
    advance, tsb = font["vmtx"].metrics[glyph_name]
    bounds = glyph_bbox(font, glyph_name)
    outline_height = 0 if bounds is None else bounds[3] - bounds[1]
    bsb = int(advance) - int(tsb) - int(outline_height)
    vorg = glyph_vertical_origin(font, glyph_name)
    return int(advance), int(tsb), bsb, vorg


def combined_control_metrics(
    font: TTFont,
    reference: TTFont | None,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int, int]]]:
    h_targets = (
        reference_cmap_metric_targets(font, reference, "hmtx")
        if reference is not None
        else {}
    )
    v_targets = (
        reference_cmap_metric_targets(font, reference, "vmtx")
        if reference is not None
        else {}
    )
    horizontal: list[tuple[int, int, int]] = []
    vertical: list[tuple[int, int, int, int]] = []
    for glyph_name in font.getGlyphOrder():
        bounds = glyph_bbox(font, glyph_name)

        h_advance, stored_lsb = font["hmtx"].metrics[glyph_name]
        lsb = int(stored_lsb) if bounds is None else int(bounds[0])
        if glyph_name in h_targets:
            h_advance, lsb = h_targets[glyph_name]
        outline_width = 0 if bounds is None else int(bounds[2]) - int(bounds[0])
        horizontal.append(
            (
                int(h_advance),
                int(lsb),
                int(h_advance) - int(lsb) - outline_width,
            )
        )

        v_advance, stored_tsb = font["vmtx"].metrics[glyph_name]
        if bounds is None:
            tsb = int(stored_tsb)
            outline_height = 0
            vorg = glyph_vertical_origin(font, glyph_name)
        else:
            tsb = glyph_vertical_origin(font, glyph_name) - int(bounds[3])
            outline_height = int(bounds[3]) - int(bounds[1])
            vorg = int(bounds[3]) + tsb
        if glyph_name in v_targets:
            v_advance, tsb = v_targets[glyph_name]
            if bounds is not None:
                vorg = int(bounds[3]) + int(tsb)
        vertical.append(
            (
                int(v_advance),
                int(tsb),
                int(v_advance) - int(tsb) - outline_height,
                int(vorg),
            )
        )
    return horizontal, vertical


def build_direct_vvar(
    font: TTFont,
    model: VariationModel,
    master_metrics: list[list[tuple[int, int, int, int]]],
    base_metrics: list[tuple[int, int, int, int]],
) -> dict[str, Any]:
    glyph_order = font.getGlyphOrder()
    supports = model.supports[1:]
    region_list = var_builder.buildVarRegionList(supports, ["wght"])
    metrics = ("advance", "tsb", "bsb", "vorg")
    var_data_by_metric = {
        metric: var_builder.buildVarData(
            list(range(len(supports))),
            [],
            optimize=False,
        )
        for metric in metrics
    }
    default_master_index = model.reverseMapping[0]
    for glyph_id, glyph_name in enumerate(glyph_order):
        glyph_master_metrics = [values[glyph_id] for values in master_metrics]
        if base_metrics[glyph_id] != master_metrics[default_master_index][glyph_id]:
            raise ValueError(
                f"VVAR default metric mismatch for {glyph_name}: "
                f"{base_metrics[glyph_id]} != "
                f"{master_metrics[default_master_index][glyph_id]}"
            )
        for metric_index, metric in enumerate(metrics):
            values = [values[metric_index] for values in glyph_master_metrics]
            deltas, glyph_supports = model.getDeltasAndSupports(values, round=round)
            if glyph_supports[1:] != supports:
                raise ValueError(f"VVAR support model mismatch for {glyph_name} {metric}")
            var_data_by_metric[metric].addItem(deltas[1:], round=round)

    table = font["VVAR"] = newTable("VVAR")
    vvar = table.table = ot.VVAR()
    vvar.Version = 0x00010000
    vvar.VarStore = var_builder.buildVarStore(
        region_list,
        [var_data_by_metric[metric] for metric in metrics],
    )
    vvar.AdvHeightMap = None
    vvar.TsbMap = var_builder.buildVarIdxMap(
        [(1 << 16) | glyph_id for glyph_id in range(len(glyph_order))],
        glyph_order,
    )
    vvar.BsbMap = var_builder.buildVarIdxMap(
        [(2 << 16) | glyph_id for glyph_id in range(len(glyph_order))],
        glyph_order,
    )
    vvar.VOrgMap = var_builder.buildVarIdxMap(
        [(3 << 16) | glyph_id for glyph_id in range(len(glyph_order))],
        glyph_order,
    )
    return var_data_by_metric


def vvar_metrics_at_weight(
    font: TTFont,
    weight: int,
    base_metrics: list[tuple[int, int, int, int]] | None = None,
) -> dict[str, list[int]]:
    vvar = font["VVAR"].table
    normalized = vf_mapped_normalized_weight(font, weight)
    location = {} if normalized == 0 else {"wght": normalized}
    instancer = VarStoreInstancer(vvar.VarStore, font["fvar"].axes, location)
    glyph_order = font.getGlyphOrder()
    if base_metrics is None:
        base_metrics = [
            vertical_glyph_metrics(font, glyph_name) for glyph_name in glyph_order
        ]
    result: dict[str, list[int]] = {}
    for metric_index, metric in enumerate(("advance", "tsb", "bsb", "vorg")):
        var_data = vvar.VarStore.VarData[metric_index]
        scalars = [instancer._getScalar(index) for index in var_data.VarRegionIndex]
        result[metric] = [
            base_metrics[glyph_id][metric_index]
            + otRound(VarStoreInstancer.interpolateFromDeltasAndScalars(deltas, scalars))
            for glyph_id, deltas in enumerate(var_data.Item)
        ]
    return result


def optimize_direct_metric_var_store(
    font: TTFont,
    table_tag: str,
    map_attributes: tuple[str, ...],
) -> dict[str, int]:
    table = font[table_tag].table
    glyph_order = font.getGlyphOrder()
    old_var_data_count = len(table.VarStore.VarData)
    old_row_count = sum(len(var_data.Item) for var_data in table.VarStore.VarData)
    mapping = table.VarStore.optimize(use_NO_VARIATION_INDEX=False)
    for major, attribute in enumerate(map_attributes):
        setattr(
            table,
            attribute,
            var_builder.buildVarIdxMap(
                [mapping[(major << 16) | glyph_id] for glyph_id in range(len(glyph_order))],
                glyph_order,
            ),
        )
    return {
        f"{table_tag.lower()}_var_data_before_optimization": old_var_data_count,
        f"{table_tag.lower()}_var_data_after_optimization": len(table.VarStore.VarData),
        f"{table_tag.lower()}_rows_before_optimization": old_row_count,
        f"{table_tag.lower()}_rows_after_optimization": sum(
            len(var_data.Item) for var_data in table.VarStore.VarData
        ),
    }


def serialized_font_bytes(font: TTFont) -> bytes:
    stream = BytesIO()
    recalc_timestamp = font.recalcTimestamp
    font.recalcTimestamp = False
    try:
        font.save(stream, reorderTables=True)
    finally:
        font.recalcTimestamp = recalc_timestamp
    return stream.getvalue()


def refresh_metric_var_data_integer_widths(font: TTFont) -> int:
    refreshed = 0
    for table_tag in ("HVAR", "VVAR"):
        if table_tag not in font:
            continue
        for var_data in font[table_tag].table.VarStore.VarData:
            # Direct calibration mutates Item values after buildVarData chose
            # their int8/int16 columns. Recompute only the storage widths here;
            # reordering columns would invalidate peak_columns below.
            var_data.calculateNumShorts(optimize=False)
            refreshed += 1
    return refreshed


def calibrate_harfbuzz_metric_controls(
    font: TTFont,
    weights: list[int],
    target_horizontal: dict[int, dict[str, list[int]]],
    target_vertical: dict[int, dict[str, list[int]]],
    vvar_data_by_metric: dict[str, Any],
    peak_columns: dict[int, int],
) -> dict[str, Any]:
    default_weight = int(weight_axis(font).defaultValue)
    supports = advance_supports(
        font,
        [weight for weight in weights if weight != default_weight],
    )
    glyph_order = font.getGlyphOrder()
    rounds = 0
    horizontal_corrections = 0
    vertical_corrections = 0
    maximum_horizontal_correction = 0
    maximum_vertical_correction = 0
    initial_mismatches_by_weight: dict[int, dict[str, int]] = {}
    horizontal_correction_history: dict[tuple[int, str], list[int]] = {}
    vertical_correction_history: dict[tuple[int, int], list[int]] = {}
    while True:
        refresh_metric_var_data_integer_widths(font)
        data = serialized_font_bytes(font)
        face = hb.Face(data)
        hb_font = hb.Font(face)
        hb_font.scale = (face.upem, face.upem)
        horizontal_pending: list[tuple[int, str, int]] = []
        vertical_pending: list[tuple[int, int, int]] = []
        mismatches_by_weight: dict[int, tuple[int, int]] = {}
        for weight in weights:
            hb_font.set_variations({"wght": weight})
            horizontal_count = 0
            vertical_count = 0
            for glyph_id, glyph_name in enumerate(glyph_order):
                extents = hb_font.get_glyph_extents(glyph_id)
                if extents is None:
                    continue
                expected_lsb = target_horizontal[weight]["lsb"][glyph_id]
                if int(extents.x_bearing) != int(expected_lsb):
                    horizontal_count += 1
                    horizontal_pending.append(
                        (
                            weight,
                            glyph_name,
                            int(expected_lsb) - int(extents.x_bearing),
                        )
                    )
                _origin_x, origin_y = hb_font.get_glyph_v_origin(glyph_id)
                actual_tsb = int(origin_y) - int(extents.y_bearing)
                expected_tsb = target_vertical[weight]["tsb"][glyph_id]
                if actual_tsb != int(expected_tsb):
                    vertical_count += 1
                    vertical_pending.append(
                        (
                            weight,
                            glyph_id,
                            int(expected_tsb) - actual_tsb,
                        )
                    )
            mismatches_by_weight[weight] = (horizontal_count, vertical_count)
        if rounds == 0:
            initial_mismatches_by_weight = {
                weight: {
                    "horizontal_side_bearing": counts[0],
                    "vertical_side_bearing": counts[1],
                }
                for weight, counts in mismatches_by_weight.items()
            }
        if not horizontal_pending and not vertical_pending:
            return {
                "harfbuzz_metric_calibration_rounds": rounds,
                "harfbuzz_metric_mismatches_before": sum(
                    sum(counts.values())
                    for counts in initial_mismatches_by_weight.values()
                ),
                "harfbuzz_metric_mismatches_before_by_weight": (
                    initial_mismatches_by_weight
                ),
                "harfbuzz_horizontal_side_bearing_corrections": horizontal_corrections,
                "harfbuzz_vertical_side_bearing_corrections": vertical_corrections,
                "harfbuzz_maximum_horizontal_correction": maximum_horizontal_correction,
                "harfbuzz_maximum_vertical_correction": maximum_vertical_correction,
                "harfbuzz_metric_control_mismatches": 0,
            }
        if any(weight == default_weight for weight, _name, _delta in horizontal_pending):
            raise RuntimeError(
                "HarfBuzz default horizontal side bearing disagrees with glyf bounds"
            )
        if any(weight == default_weight for weight, _gid, _delta in vertical_pending):
            raise RuntimeError(
                "HarfBuzz default vertical side bearing disagrees with VORG/glyf bounds"
            )
        rounds += 1
        if rounds > 4:
            oscillation_samples = {
                f"{weight}:{glyph_name}": history
                for (weight, glyph_name), history in horizontal_correction_history.items()
                if len(history) > 1
            }
            raise RuntimeError(
                "HarfBuzz metric control calibration did not converge: "
                + json.dumps(
                    {
                        "mismatches": mismatches_by_weight,
                        "horizontal_correction_history": dict(
                            list(oscillation_samples.items())[:24]
                        ),
                    },
                    sort_keys=True,
                )
            )
        log_step(
            "VF metrics: HarfBuzz correction round "
            f"{rounds}, horizontal={len(horizontal_pending)}, "
            f"vertical={len(vertical_pending)}"
        )
        for weight, glyph_name, correction in horizontal_pending:
            horizontal_correction_history.setdefault(
                (weight, glyph_name),
                [],
            ).append(correction)
            add_outline_translation_variation(
                font,
                glyph_name,
                supports[weight],
                correction,
            )
            maximum_horizontal_correction = max(
                maximum_horizontal_correction,
                abs(correction),
            )
        for weight, glyph_id, correction in vertical_pending:
            vertical_correction_history.setdefault((weight, glyph_id), []).append(
                correction
            )
            vvar_data_by_metric["vorg"].Item[glyph_id][peak_columns[weight]] += correction
            maximum_vertical_correction = max(
                maximum_vertical_correction,
                abs(correction),
            )
        horizontal_corrections += len(horizontal_pending)
        vertical_corrections += len(vertical_pending)


def rebuild_vf_metric_variation_tables(
    font: TTFont,
    reference_fonts: dict[int, TTFont],
) -> dict[str, Any]:
    if "fvar" not in font or "gvar" not in font:
        raise ValueError("VF metric variation rebuild requires fvar and gvar")
    remove_metric_variation_maps(font)
    weights, locations = metric_variation_control_locations(font)
    model = VariationModel(locations, axisOrder=["wght"])
    masters: list[TTFont] = []
    try:
        log_step("VF metrics: instantiate seven control points")
        for weight in weights:
            masters.append(
                instantiateVariableFont(
                    font,
                    {"wght": weight},
                    inplace=False,
                    optimize=True,
                )
            )
        glyph_order = font.getGlyphOrder()
        log_step("VF metrics: scan horizontal and vertical control geometry")
        base_metrics = [horizontal_glyph_metrics(font, name) for name in glyph_order]
        combined_master_metrics = [
            combined_control_metrics(master, reference_fonts.get(weight))
            for weight, master in zip(weights, masters)
        ]
        master_metrics = [metrics[0] for metrics in combined_master_metrics]
        target_metrics = {
            weight: {
                metric: [values[metric_index] for values in metrics]
                for metric_index, metric in enumerate(("advance", "lsb", "rsb"))
            }
            for weight, metrics in zip(weights, master_metrics)
        }
        var_data_by_metric = build_direct_hvar(
            font,
            model,
            master_metrics,
            base_metrics,
        )
        peak_columns = {
            weight: next(
                index
                for index, support in enumerate(model.supports[1:])
                if support["wght"][1]
                == floatToFixedToFloat(vf_mapped_normalized_weight(font, weight), 14)
            )
            for weight in weights
            if weight != int(weight_axis(font).defaultValue)
        }
        calibration_rounds = 0
        calibration_corrections = 0
        log_step("VF metrics: calibrate HVAR control values")
        while True:
            errors: dict[tuple[str, int, int], int] = {}
            for weight in weights:
                if weight == int(weight_axis(font).defaultValue):
                    continue
                actual_metrics = hvar_metrics_at_weight(font, weight, base_metrics)
                for metric in ("advance", "lsb", "rsb"):
                    for glyph_id, (actual_value, expected_value) in enumerate(
                        zip(actual_metrics[metric], target_metrics[weight][metric])
                    ):
                        if actual_value != expected_value:
                            errors[(metric, glyph_id, peak_columns[weight])] = (
                                expected_value - actual_value
                            )
            if not errors:
                break
            calibration_rounds += 1
            calibration_corrections += len(errors)
            if calibration_rounds > 8:
                raise RuntimeError(
                    f"HVAR exact-weight calibration did not converge: {len(errors)} errors"
                )
            for (metric, glyph_id, column), correction in errors.items():
                var_data_by_metric[metric].Item[glyph_id][column] += correction
        vvar_calibration_rounds = 0
        vvar_calibration_corrections = 0
        vvar_optimization_report: dict[str, int] = {}
        engine_metric_report: dict[str, int] = {}
        if "vmtx" in font:
            base_vertical_metrics = [
                vertical_glyph_metrics(font, name) for name in glyph_order
            ]
            master_vertical_metrics = [
                metrics[1] for metrics in combined_master_metrics
            ]
            target_vertical_metrics = {
                weight: {
                    metric: [values[metric_index] for values in metrics]
                    for metric_index, metric in enumerate(
                        ("advance", "tsb", "bsb", "vorg")
                    )
                }
                for weight, metrics in zip(weights, master_vertical_metrics)
            }
            vvar_data_by_metric = build_direct_vvar(
                font,
                model,
                master_vertical_metrics,
                base_vertical_metrics,
            )
            log_step("VF metrics: calibrate VVAR control values")
            while True:
                errors: dict[tuple[str, int, int], int] = {}
                for weight in weights:
                    if weight == int(weight_axis(font).defaultValue):
                        continue
                    actual_metrics = vvar_metrics_at_weight(
                        font,
                        weight,
                        base_vertical_metrics,
                    )
                    for metric in ("advance", "tsb", "bsb", "vorg"):
                        for glyph_id, (actual_value, expected_value) in enumerate(
                            zip(
                                actual_metrics[metric],
                                target_vertical_metrics[weight][metric],
                            )
                        ):
                            if actual_value != expected_value:
                                errors[(metric, glyph_id, peak_columns[weight])] = (
                                    expected_value - actual_value
                                )
                if not errors:
                    break
                vvar_calibration_rounds += 1
                vvar_calibration_corrections += len(errors)
                if vvar_calibration_rounds > 8:
                    raise RuntimeError(
                        "VVAR exact-weight calibration did not converge: "
                        f"{len(errors)} errors"
                    )
                for (metric, glyph_id, column), correction in errors.items():
                    vvar_data_by_metric[metric].Item[glyph_id][column] += correction
            log_step("VF metrics: calibrate serialized HarfBuzz side bearings")
            engine_metric_report = calibrate_harfbuzz_metric_controls(
                font,
                weights,
                target_metrics,
                target_vertical_metrics,
                vvar_data_by_metric,
                peak_columns,
            )
            log_step("VF metrics: optimize HVAR/VVAR stores")
            for var_data in var_data_by_metric.values():
                var_data.optimize()
            hvar_optimization_report = optimize_direct_metric_var_store(
                font,
                "HVAR",
                ("AdvWidthMap", "LsbMap", "RsbMap"),
            )
            for var_data in vvar_data_by_metric.values():
                var_data.optimize()
            vvar_optimization_report = optimize_direct_metric_var_store(
                font,
                "VVAR",
                ("AdvHeightMap", "TsbMap", "BsbMap", "VOrgMap"),
            )
        else:
            for var_data in var_data_by_metric.values():
                var_data.optimize()
            hvar_optimization_report = optimize_direct_metric_var_store(
                font,
                "HVAR",
                ("AdvWidthMap", "LsbMap", "RsbMap"),
            )
        return {
            "metric_variation_control_weights": weights,
            "hvar_explicit_optimized_mapping": True,
            "hvar_calibration_rounds": calibration_rounds,
            "hvar_calibration_corrections": calibration_corrections,
            "hvar_lsb_map": True,
            "hvar_rsb_map": True,
            "vvar_rebuilt": "VVAR" in font,
            "vvar_tsb_map": "VVAR" in font,
            "vvar_bsb_map": "VVAR" in font,
            "vvar_vorg_map": "VVAR" in font,
            "vvar_calibration_rounds": vvar_calibration_rounds,
            "vvar_calibration_corrections": vvar_calibration_corrections,
            "vvar_exact_weight_advance_mismatches": 0,
            **hvar_optimization_report,
            **vvar_optimization_report,
            **engine_metric_report,
        }
    finally:
        for master in masters:
            master.close()


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


def align_layout_feature_template(
    font: TTFont,
    reference: TTFont,
    table_tag: str,
    use_reference_lookup_indices: bool = False,
) -> dict[str, int]:
    key = table_tag.lower()
    if table_tag not in font or table_tag not in reference:
        return {
            f"{key}_feature_records_before_template": 0,
            f"{key}_feature_records_after_template": 0,
            f"{key}_empty_feature_records_added_from_template": 0,
            f"{key}_langsys_after_template": 0,
        }
    table = font[table_tag].table
    ref_table = reference[table_tag].table
    if not table.FeatureList or not ref_table.FeatureList or not ref_table.ScriptList:
        return {
            f"{key}_feature_records_before_template": 0,
            f"{key}_feature_records_after_template": 0,
            f"{key}_empty_feature_records_added_from_template": 0,
            f"{key}_langsys_after_template": 0,
        }

    current_by_tag: dict[str, list[Any]] = {}
    for record in table.FeatureList.FeatureRecord:
        current_by_tag.setdefault(record.FeatureTag, []).append(record)

    old_count = len(table.FeatureList.FeatureRecord)
    ref_to_new: dict[int, int] = {}
    used_by_tag: dict[str, int] = {}
    new_records = []
    empty_added = 0
    for ref_index, ref_record in enumerate(ref_table.FeatureList.FeatureRecord):
        candidates = current_by_tag.get(ref_record.FeatureTag)
        if not list(ref_record.Feature.LookupListIndex or []):
            # Empty FeatureRecords are part of the reference contract.  Do not
            # fill one by reusing the last non-empty record with the same tag;
            # that previously wired latn/CAT to the ROM/MOL locl lookup.
            record = copy.deepcopy(ref_record)
            record.Feature.LookupListIndex = []
            record.Feature.LookupCount = 0
            empty_added += 1
        elif candidates:
            use_index = min(used_by_tag.get(ref_record.FeatureTag, 0), len(candidates) - 1)
            used_by_tag[ref_record.FeatureTag] = used_by_tag.get(ref_record.FeatureTag, 0) + 1
            record = copy.deepcopy(candidates[use_index])
            record.FeatureTag = ref_record.FeatureTag
        else:
            continue
        if use_reference_lookup_indices:
            record.Feature.LookupListIndex = list(ref_record.Feature.LookupListIndex or [])
            record.Feature.LookupCount = len(record.Feature.LookupListIndex)
        ref_to_new[ref_index] = len(new_records)
        new_records.append(record)

    if not new_records:
        return {
            f"{key}_feature_records_before_template": old_count,
            f"{key}_feature_records_after_template": old_count,
            f"{key}_empty_feature_records_added_from_template": 0,
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
        f"{key}_empty_feature_records_added_from_template": empty_added,
        f"{key}_langsys_after_template": langsys_count,
    }


def empty_coverage() -> ot.Coverage:
    coverage = ot.Coverage()
    coverage.glyphs = []
    return coverage


VALUE_RECORD_FIELDS = [
    (0x0001, "XPlacement", 0),
    (0x0002, "YPlacement", 0),
    (0x0004, "XAdvance", 0),
    (0x0008, "YAdvance", 0),
    (0x0010, "XPlaDevice", None),
    (0x0020, "YPlaDevice", None),
    (0x0040, "XAdvDevice", None),
    (0x0080, "YAdvDevice", None),
]


def value_record_for_format(source: Any | None, value_format: int) -> ot.ValueRecord | None:
    if value_format == 0:
        return None
    record = ot.ValueRecord()
    for bit, attr, default in VALUE_RECORD_FIELDS:
        if value_format & bit:
            setattr(record, attr, copy.deepcopy(getattr(source, attr, default)))
    return record


def empty_gpos_subtable(lookup_type: int) -> Any:
    if lookup_type == 1:
        subtable = ot.SinglePos()
        subtable.Format = 1
        subtable.Coverage = empty_coverage()
        subtable.ValueFormat = 0
        subtable.Value = None
        return subtable
    if lookup_type == 2:
        subtable = ot.PairPos()
        subtable.Format = 1
        subtable.Coverage = empty_coverage()
        subtable.ValueFormat1 = 0
        subtable.ValueFormat2 = 0
        subtable.PairSet = []
        subtable.PairSetCount = 0
        return subtable
    if lookup_type == 3:
        subtable = ot.CursivePos()
        subtable.Format = 1
        subtable.Coverage = empty_coverage()
        subtable.EntryExitRecord = []
        subtable.EntryExitCount = 0
        return subtable
    if lookup_type == 4:
        subtable = ot.MarkBasePos()
        subtable.Format = 1
        subtable.MarkCoverage = empty_coverage()
        subtable.BaseCoverage = empty_coverage()
        subtable.ClassCount = 0
        subtable.MarkArray = ot.MarkArray()
        subtable.MarkArray.MarkCount = 0
        subtable.MarkArray.MarkRecord = []
        subtable.BaseArray = ot.BaseArray()
        subtable.BaseArray.BaseCount = 0
        subtable.BaseArray.BaseRecord = []
        return subtable
    if lookup_type == 5:
        subtable = ot.MarkLigPos()
        subtable.Format = 1
        subtable.MarkCoverage = empty_coverage()
        subtable.LigatureCoverage = empty_coverage()
        subtable.ClassCount = 0
        subtable.MarkArray = ot.MarkArray()
        subtable.MarkArray.MarkCount = 0
        subtable.MarkArray.MarkRecord = []
        subtable.LigatureArray = ot.LigatureArray()
        subtable.LigatureArray.LigatureCount = 0
        subtable.LigatureArray.LigatureAttach = []
        return subtable
    if lookup_type == 6:
        subtable = ot.MarkMarkPos()
        subtable.Format = 1
        subtable.Mark1Coverage = empty_coverage()
        subtable.Mark2Coverage = empty_coverage()
        subtable.ClassCount = 0
        subtable.Mark1Array = ot.MarkArray()
        subtable.Mark1Array.MarkCount = 0
        subtable.Mark1Array.MarkRecord = []
        subtable.Mark2Array = ot.Mark2Array()
        subtable.Mark2Array.Mark2Count = 0
        subtable.Mark2Array.Mark2Record = []
        return subtable
    raise ValueError(f"Unsupported empty GPOS lookup type: {lookup_type}")


def align_single_pos_format(subtable: Any, reference: Any) -> Any:
    reference_format = getattr(reference, "Format", getattr(subtable, "Format", 1))
    reference_value_format = getattr(reference, "ValueFormat", getattr(subtable, "ValueFormat", 0))
    glyphs = list(getattr(getattr(subtable, "Coverage", None), "glyphs", []) or [])
    if getattr(subtable, "Format", 1) == 2:
        values = list(getattr(subtable, "Value", []) or [])
        source_value = values[0] if values else None
    else:
        source_value = getattr(subtable, "Value", None)
    subtable.ValueFormat = reference_value_format
    if reference_format == 2:
        subtable.Format = 2
        subtable.Value = [value_record_for_format(source_value, reference_value_format) for _ in glyphs]
        subtable.ValueCount = len(subtable.Value)
    else:
        subtable.Format = 1
        subtable.Value = value_record_for_format(source_value, reference_value_format)
        if hasattr(subtable, "ValueCount"):
            delattr(subtable, "ValueCount")
    return subtable


def empty_class_def() -> ot.ClassDef:
    class_def = ot.ClassDef()
    class_def.classDefs = {}
    return class_def


def build_class2_record(value_format1: int, value_format2: int, value1: Any | None = None, value2: Any | None = None) -> Any:
    record = ot.Class2Record()
    record.Value1 = value_record_for_format(value1, value_format1)
    record.Value2 = value_record_for_format(value2, value_format2)
    return record


def align_pair_pos_format(subtable: Any, reference: Any) -> Any:
    reference_format = getattr(reference, "Format", getattr(subtable, "Format", 1))
    value_format1 = getattr(reference, "ValueFormat1", getattr(subtable, "ValueFormat1", 0))
    value_format2 = getattr(reference, "ValueFormat2", getattr(subtable, "ValueFormat2", 0))
    if reference_format == 2:
        if getattr(subtable, "Format", 1) == 2:
            class1_records = list(getattr(subtable, "Class1Record", []) or [])
            for class1_record in class1_records:
                for class2_record in getattr(class1_record, "Class2Record", []) or []:
                    class2_record.Value1 = value_record_for_format(getattr(class2_record, "Value1", None), value_format1)
                    class2_record.Value2 = value_record_for_format(getattr(class2_record, "Value2", None), value_format2)
            subtable.ValueFormat1 = value_format1
            subtable.ValueFormat2 = value_format2
            return subtable

        first_glyphs = list(getattr(getattr(subtable, "Coverage", None), "glyphs", []) or [])
        second_glyphs: list[str] = []
        pair_values: dict[tuple[str, str], tuple[Any | None, Any | None]] = {}
        for first_glyph, pair_set in zip(first_glyphs, getattr(subtable, "PairSet", []) or []):
            for pair_record in getattr(pair_set, "PairValueRecord", []) or []:
                second_glyph = pair_record.SecondGlyph
                if second_glyph not in second_glyphs:
                    second_glyphs.append(second_glyph)
                pair_values[(first_glyph, second_glyph)] = (
                    getattr(pair_record, "Value1", None),
                    getattr(pair_record, "Value2", None),
                )

        class1 = {glyph: index + 1 for index, glyph in enumerate(first_glyphs)}
        class2 = {glyph: index + 1 for index, glyph in enumerate(second_glyphs)}
        class1_count = len(class1) + 1 if first_glyphs else 0
        class2_count = len(class2) + 1 if second_glyphs else 0
        class1_records = []
        for class1_index in range(class1_count):
            class1_record = ot.Class1Record()
            class2_records = []
            first_glyph = first_glyphs[class1_index - 1] if class1_index else None
            for class2_index in range(class2_count):
                second_glyph = second_glyphs[class2_index - 1] if class2_index else None
                value1, value2 = pair_values.get((first_glyph, second_glyph), (None, None))
                class2_records.append(build_class2_record(value_format1, value_format2, value1, value2))
            class1_record.Class2Record = class2_records
            class1_records.append(class1_record)

        subtable.Format = 2
        subtable.ValueFormat1 = value_format1
        subtable.ValueFormat2 = value_format2
        subtable.ClassDef1 = empty_class_def()
        subtable.ClassDef1.classDefs = class1
        subtable.ClassDef2 = empty_class_def()
        subtable.ClassDef2.classDefs = class2
        subtable.Class1Count = class1_count
        subtable.Class2Count = class2_count
        subtable.Class1Record = class1_records
        if hasattr(subtable, "PairSet"):
            delattr(subtable, "PairSet")
        if hasattr(subtable, "PairSetCount"):
            delattr(subtable, "PairSetCount")
        return subtable

    subtable.Format = 1
    subtable.ValueFormat1 = value_format1
    subtable.ValueFormat2 = value_format2
    if hasattr(subtable, "ClassDef1"):
        delattr(subtable, "ClassDef1")
    if hasattr(subtable, "ClassDef2"):
        delattr(subtable, "ClassDef2")
    if hasattr(subtable, "Class1Count"):
        delattr(subtable, "Class1Count")
    if hasattr(subtable, "Class2Count"):
        delattr(subtable, "Class2Count")
    if hasattr(subtable, "Class1Record"):
        delattr(subtable, "Class1Record")
    if not hasattr(subtable, "PairSet"):
        subtable.PairSet = []
    subtable.PairSetCount = len(subtable.PairSet)
    return subtable


def align_gpos_subtable_to_reference(subtable: Any, reference_subtable: Any, lookup_type: int) -> Any:
    if lookup_type == 9 and hasattr(reference_subtable, "ExtensionLookupType"):
        subtable.Format = getattr(reference_subtable, "Format", 1)
        subtable.ExtensionLookupType = reference_subtable.ExtensionLookupType
        if not hasattr(subtable, "ExtSubTable") or subtable.ExtSubTable is None:
            subtable.ExtSubTable = empty_gpos_subtable(reference_subtable.ExtensionLookupType)
        subtable.ExtSubTable = align_gpos_subtable_to_reference(
            subtable.ExtSubTable,
            reference_subtable.ExtSubTable,
            reference_subtable.ExtensionLookupType,
        )
        return subtable
    if lookup_type == 1:
        return align_single_pos_format(subtable, reference_subtable)
    if lookup_type == 2:
        return align_pair_pos_format(subtable, reference_subtable)
    if hasattr(reference_subtable, "Format"):
        subtable.Format = reference_subtable.Format
    return subtable


def empty_layout_subtable_like(reference_subtable: Any, lookup_type: int) -> Any:
    if lookup_type == 9 and hasattr(reference_subtable, "ExtensionLookupType"):
        extension = ot.ExtensionPos()
        extension.Format = 1
        extension.ExtensionLookupType = reference_subtable.ExtensionLookupType
        extension.ExtSubTable = empty_gpos_subtable(reference_subtable.ExtensionLookupType)
        return align_gpos_subtable_to_reference(extension, reference_subtable, lookup_type)
    return align_gpos_subtable_to_reference(empty_gpos_subtable(lookup_type), reference_subtable, lookup_type)


def align_layout_lookup_structure(font: TTFont, reference: TTFont, table_tag: str) -> dict[str, int]:
    key = table_tag.lower()
    if table_tag not in font or table_tag not in reference:
        return {f"{key}_lookups_before_structure_template": 0, f"{key}_lookups_after_structure_template": 0}
    table = font[table_tag].table
    ref_table = reference[table_tag].table
    if not table.LookupList or not ref_table.LookupList:
        return {f"{key}_lookups_before_structure_template": 0, f"{key}_lookups_after_structure_template": 0}
    before = len(table.LookupList.Lookup)
    ref_lookups = ref_table.LookupList.Lookup
    while len(table.LookupList.Lookup) < len(ref_lookups):
        ref_lookup = ref_lookups[len(table.LookupList.Lookup)]
        lookup = ot.Lookup()
        lookup.LookupType = ref_lookup.LookupType
        lookup.LookupFlag = ref_lookup.LookupFlag
        if hasattr(ref_lookup, "MarkFilteringSet"):
            lookup.MarkFilteringSet = copy.deepcopy(ref_lookup.MarkFilteringSet)
        lookup.SubTable = [
            empty_layout_subtable_like(ref_subtable, ref_lookup.LookupType) for ref_subtable in ref_lookup.SubTable
        ]
        lookup.SubTableCount = len(lookup.SubTable)
        table.LookupList.Lookup.append(lookup)
    if len(table.LookupList.Lookup) > len(ref_lookups):
        table.LookupList.Lookup = table.LookupList.Lookup[: len(ref_lookups)]
    for lookup, ref_lookup in zip(table.LookupList.Lookup, ref_lookups):
        lookup.LookupType = ref_lookup.LookupType
        lookup.LookupFlag = ref_lookup.LookupFlag
        if hasattr(ref_lookup, "MarkFilteringSet"):
            lookup.MarkFilteringSet = copy.deepcopy(ref_lookup.MarkFilteringSet)
        elif hasattr(lookup, "MarkFilteringSet"):
            delattr(lookup, "MarkFilteringSet")
        if len(lookup.SubTable) > len(ref_lookup.SubTable):
            lookup.SubTable = lookup.SubTable[: len(ref_lookup.SubTable)]
        while len(lookup.SubTable) < len(ref_lookup.SubTable):
            ref_subtable = ref_lookup.SubTable[len(lookup.SubTable)]
            lookup.SubTable.append(empty_layout_subtable_like(ref_subtable, ref_lookup.LookupType))
        lookup.SubTable = [
            align_gpos_subtable_to_reference(subtable, ref_subtable, ref_lookup.LookupType)
            for subtable, ref_subtable in zip(lookup.SubTable, ref_lookup.SubTable)
        ]
        lookup.SubTableCount = len(lookup.SubTable)
    table.LookupList.LookupCount = len(table.LookupList.Lookup)
    return {
        f"{key}_lookups_before_structure_template": before,
        f"{key}_lookups_after_structure_template": len(table.LookupList.Lookup),
    }


def feature_lookup_indices(font: TTFont, table_tag: str, feature_tags: set[str]) -> list[int]:
    if table_tag not in font:
        return []
    table = font[table_tag].table
    if not table.FeatureList:
        return []
    indices: list[int] = []
    for record in table.FeatureList.FeatureRecord:
        if record.FeatureTag not in feature_tags:
            continue
        for lookup_index in list(record.Feature.LookupListIndex or []):
            if lookup_index not in indices:
                indices.append(lookup_index)
    return indices


def single_pos_value_map(subtable: Any) -> dict[str, Any]:
    if not hasattr(subtable, "Coverage") or not getattr(subtable, "Coverage", None):
        return {}
    glyphs = list(subtable.Coverage.glyphs or [])
    if getattr(subtable, "Format", 1) == 2:
        return {
            glyph_name: copy.deepcopy(value)
            for glyph_name, value in zip(glyphs, list(getattr(subtable, "Value", []) or []))
        }
    value = copy.deepcopy(getattr(subtable, "Value", None))
    return {glyph_name: copy.deepcopy(value) for glyph_name in glyphs}


def layout_glyph_semantic_key(font: TTFont, glyph_name: str) -> tuple[Any, ...] | None:
    structure = glyph_point_structure(font, glyph_name)
    if structure is None:
        return None
    return (
        structure,
        tuple(font["hmtx"].metrics[glyph_name]) if "hmtx" in font else None,
        tuple(font["vmtx"].metrics[glyph_name]) if "vmtx" in font else None,
    )


def semantic_layout_glyph_map(
    font: TTFont,
    glyphs: list[str],
    reference: TTFont,
    reference_glyphs: list[str],
) -> dict[str, str]:
    reference_buckets: dict[tuple[Any, ...], list[str]] = {}
    for glyph_name in reference_glyphs:
        key = layout_glyph_semantic_key(reference, glyph_name)
        if key is not None:
            reference_buckets.setdefault(key, []).append(glyph_name)

    mapping: dict[str, str] = {}
    for glyph_name in glyphs:
        key = layout_glyph_semantic_key(font, glyph_name)
        candidates = reference_buckets.get(key) if key is not None else None
        if candidates:
            mapping[glyph_name] = candidates.pop(0)
    return mapping


def sync_single_pos_values_from_reference(
    subtable: Any,
    reference_subtable: Any,
    font: TTFont,
    reference: TTFont,
) -> dict[str, int]:
    if hasattr(reference_subtable, "ExtSubTable"):
        if not hasattr(subtable, "ExtSubTable") or subtable.ExtSubTable is None:
            return {"single_pos_values_synced": 0, "single_pos_glyphs_missing_in_reference": 0}
        return sync_single_pos_values_from_reference(
            subtable.ExtSubTable,
            reference_subtable.ExtSubTable,
            font,
            reference,
        )
    if not hasattr(subtable, "Coverage") or not hasattr(reference_subtable, "Coverage"):
        return {"single_pos_values_synced": 0, "single_pos_glyphs_missing_in_reference": 0}
    if getattr(subtable, "Format", 1) != getattr(reference_subtable, "Format", 1) or getattr(
        subtable, "ValueFormat", 0
    ) != getattr(reference_subtable, "ValueFormat", 0):
        subtable = align_single_pos_format(subtable, reference_subtable)

    ref_values = single_pos_value_map(reference_subtable)
    glyphs = list(subtable.Coverage.glyphs or [])
    glyph_map = semantic_layout_glyph_map(
        font,
        glyphs,
        reference,
        list(reference_subtable.Coverage.glyphs or []),
    )
    missing = 0
    synced = 0
    if getattr(subtable, "Format", 1) == 2:
        values = list(getattr(subtable, "Value", []) or [])
        while len(values) < len(glyphs):
            values.append(None)
        for index, glyph_name in enumerate(glyphs):
            reference_glyph_name = glyph_map.get(glyph_name)
            if reference_glyph_name not in ref_values:
                missing += 1
                continue
            values[index] = copy.deepcopy(ref_values[reference_glyph_name])
            synced += 1
        subtable.Value = values[: len(glyphs)]
        subtable.ValueCount = len(subtable.Value)
        subtable.ValueFormat = getattr(reference_subtable, "ValueFormat", getattr(subtable, "ValueFormat", 0))
        return {"single_pos_values_synced": synced, "single_pos_glyphs_missing_in_reference": missing}

    first_value = None
    for glyph_name in glyphs:
        reference_glyph_name = glyph_map.get(glyph_name)
        if reference_glyph_name in ref_values:
            first_value = copy.deepcopy(ref_values[reference_glyph_name])
            synced += 1
        else:
            missing += 1
    if first_value is not None:
        subtable.Value = first_value
        subtable.ValueFormat = getattr(reference_subtable, "ValueFormat", getattr(subtable, "ValueFormat", 0))
    if hasattr(subtable, "ValueCount"):
        delattr(subtable, "ValueCount")
    return {"single_pos_values_synced": synced, "single_pos_glyphs_missing_in_reference": missing}


def sync_gpos_single_pos_feature_values_from_reference(
    font: TTFont,
    reference: TTFont,
    feature_tags: set[str],
) -> dict[str, int]:
    if "GPOS" not in font or "GPOS" not in reference:
        return {
            "gpos_single_pos_feature_lookups_synced": 0,
            "gpos_single_pos_values_synced": 0,
            "gpos_single_pos_glyphs_missing_in_reference": 0,
        }
    table = font["GPOS"].table
    ref_table = reference["GPOS"].table
    if not table.LookupList or not ref_table.LookupList:
        return {
            "gpos_single_pos_feature_lookups_synced": 0,
            "gpos_single_pos_values_synced": 0,
            "gpos_single_pos_glyphs_missing_in_reference": 0,
        }
    target_indices = feature_lookup_indices(font, "GPOS", feature_tags)
    ref_indices = feature_lookup_indices(reference, "GPOS", feature_tags)
    lookup_count = min(len(target_indices), len(ref_indices))
    lookups_synced = 0
    values_synced = 0
    missing = 0
    for target_index, ref_index in zip(target_indices[:lookup_count], ref_indices[:lookup_count]):
        if target_index >= len(table.LookupList.Lookup) or ref_index >= len(ref_table.LookupList.Lookup):
            continue
        lookup = table.LookupList.Lookup[target_index]
        ref_lookup = ref_table.LookupList.Lookup[ref_index]
        if ref_lookup.LookupType not in {1, 9}:
            continue
        lookups_synced += 1
        for subtable, ref_subtable in zip(lookup.SubTable, ref_lookup.SubTable):
            report = sync_single_pos_values_from_reference(
                subtable,
                ref_subtable,
                font,
                reference,
            )
            values_synced += report["single_pos_values_synced"]
            missing += report["single_pos_glyphs_missing_in_reference"]
    return {
        "gpos_single_pos_feature_lookups_synced": lookups_synced,
        "gpos_single_pos_values_synced": values_synced,
        "gpos_single_pos_glyphs_missing_in_reference": missing,
        "gpos_single_pos_feature_lookup_count_mismatch": abs(len(target_indices) - len(ref_indices)),
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


def append_gpos_lookup(font: TTFont, lookup: ot.Lookup) -> int:
    gpos = font["GPOS"].table
    if gpos.LookupList is None:
        gpos.LookupList = ot.LookupList()
        gpos.LookupList.Lookup = []
        gpos.LookupList.LookupCount = 0
    lookup_index = len(gpos.LookupList.Lookup)
    gpos.LookupList.Lookup.append(lookup)
    gpos.LookupList.LookupCount = len(gpos.LookupList.Lookup)
    return lookup_index


def insert_gpos_feature(font: TTFont, tag: str, lookup_indices: list[int]) -> int:
    gpos = font["GPOS"].table
    if gpos.FeatureList is None:
        gpos.FeatureList = ot.FeatureList()
        gpos.FeatureList.FeatureRecord = []
        gpos.FeatureList.FeatureCount = 0
    feature = ot.Feature()
    feature.FeatureParams = None
    feature.LookupListIndex = list(lookup_indices)
    feature.LookupCount = len(lookup_indices)
    record = ot.FeatureRecord()
    record.FeatureTag = tag
    record.Feature = feature

    records = gpos.FeatureList.FeatureRecord
    feature_index = next(
        (index for index, existing in enumerate(records) if existing.FeatureTag > tag),
        len(records),
    )
    if gpos.ScriptList:
        for script_record in gpos.ScriptList.ScriptRecord:
            langsys_list = []
            if script_record.Script.DefaultLangSys:
                langsys_list.append(script_record.Script.DefaultLangSys)
            langsys_list.extend(
                langsys_record.LangSys for langsys_record in script_record.Script.LangSysRecord
            )
            for langsys in langsys_list:
                required = int(getattr(langsys, "ReqFeatureIndex", 0xFFFF))
                if required != 0xFFFF and required >= feature_index:
                    langsys.ReqFeatureIndex = required + 1
                langsys.FeatureIndex = [
                    index + 1 if index >= feature_index else index
                    for index in list(langsys.FeatureIndex or [])
                ]

    records.insert(feature_index, record)
    gpos.FeatureList.FeatureCount = len(records)
    if gpos.ScriptList:
        for script_record in gpos.ScriptList.ScriptRecord:
            langsys_list = []
            if script_record.Script.DefaultLangSys:
                langsys_list.append(script_record.Script.DefaultLangSys)
            langsys_list.extend(
                langsys_record.LangSys for langsys_record in script_record.Script.LangSysRecord
            )
            for langsys in langsys_list:
                indices = list(langsys.FeatureIndex or [])
                if feature_index not in indices:
                    indices.append(feature_index)
                langsys.FeatureIndex = indices
                langsys.FeatureCount = len(langsys.FeatureIndex)
    return feature_index


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


def horizontal_em_dash_continuation_points(
    box: tuple[int, int, int, int],
    advance_width: int,
) -> list[tuple[float, float]]:
    _x_min, y_min, x_max, y_max = box
    half_height = (y_max - y_min) / 2
    return [
        (x_max - advance_width, y_max),
        (x_max - advance_width - half_height, (y_min + y_max) / 2),
        (x_max - advance_width, y_min),
        (x_max, y_min),
        (x_max, y_max),
    ]


def glyph_set_bbox(
    font: TTFont,
    glyph_name: str,
    location: dict[str, float] | None = None,
) -> tuple[int, int, int, int] | None:
    glyph_set = font.getGlyphSet(location=location)
    if glyph_name not in glyph_set:
        return None
    pen = BoundsPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    if pen.bounds is None:
        return None
    return tuple(otRound(value) for value in pen.bounds)


def vertical_origin_y(font: TTFont, glyph_name: str) -> int:
    if "VORG" in font:
        return int(font["VORG"].VOriginRecords.get(glyph_name, font["VORG"].defaultVertOriginY))
    box = glyph_bbox(font, glyph_name)
    if box and "vmtx" in font and glyph_name in font["vmtx"].metrics:
        return int(box[3] + font["vmtx"].metrics[glyph_name][1])
    return int(font["head"].unitsPerEm)


def add_simple_glyph(font: TTFont, glyph_name: str, source_name: str, points: list[tuple[float, float]]) -> None:
    font["glyf"].glyphs[glyph_name] = make_polygon_glyph(points)
    font["glyf"].glyphs[glyph_name].recalcBounds(font["glyf"])
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


def add_fixed_advance_glyph_alias(
    font: TTFont,
    glyph_name: str,
    source_name: str,
    advance_width: int,
) -> None:
    font["glyf"].glyphs[glyph_name] = copy.deepcopy(font["glyf"][source_name])
    source_lsb = int(font["hmtx"].metrics[source_name][1])
    font["hmtx"].metrics[glyph_name] = (int(advance_width), source_lsb)
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        font["vmtx"].metrics[glyph_name] = copy.deepcopy(
            font["vmtx"].metrics[source_name]
        )
    if "gvar" in font:
        font["gvar"].variations[glyph_name] = copy.deepcopy(
            font["gvar"].variations.get(source_name, [])
        )
        freeze_horizontal_advance_preserve_bearings(font, glyph_name)
    order = font.getGlyphOrder()
    if glyph_name not in order:
        order.append(glyph_name)
        font.setGlyphOrder(order)
    if "maxp" in font:
        font["maxp"].numGlyphs = len(font.getGlyphOrder())


def layout_glyph_references(font: TTFont, glyph_names: set[str]) -> set[str]:
    found: set[str] = set()

    def visit(value: Any, seen: set[int]) -> None:
        if isinstance(value, str):
            if value in glyph_names:
                found.add(value)
            return
        if value is None or isinstance(value, (bytes, int, float, bool)):
            return
        value_id = id(value)
        if value_id in seen:
            return
        seen.add(value_id)
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key, seen)
                visit(item, seen)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item, seen)
        elif hasattr(value, "__dict__"):
            for attribute, item in vars(value).items():
                if not attribute.startswith("_"):
                    visit(item, seen)

    for tag in ("GSUB", "GPOS", "GDEF", "MATH", "COLR"):
        if tag in font:
            visit(getattr(font[tag], "table", font[tag]), set())
    return found


def remove_generated_glyphs(font: TTFont, glyph_names: set[str]) -> int:
    removable = set(glyph_names) & set(font.getGlyphOrder())
    if not removable:
        return 0
    cmap_glyphs = set((font.getBestCmap() or {}).values())
    if removable & cmap_glyphs:
        raise ValueError(f"refusing to remove encoded glyphs: {sorted(removable & cmap_glyphs)}")
    layout_references = layout_glyph_references(font, removable)
    if layout_references:
        raise ValueError(
            f"refusing to remove layout-referenced glyphs: {sorted(layout_references)}"
        )
    component_references = (
        {
            component.glyphName
            for glyph_name in font.getGlyphOrder()
            if glyph_name not in removable
            for glyph in [font["glyf"][glyph_name]]
            if glyph.isComposite()
            for component in glyph.components
            if component.glyphName in removable
        }
        if "glyf" in font
        else set()
    )
    if component_references:
        raise ValueError(
            f"refusing to remove component-referenced glyphs: {sorted(component_references)}"
        )
    if "gvar" in font:
        font["gvar"].ensureDecompiled()
    for glyph_name in removable:
        if "gvar" in font:
            font["gvar"].variations.pop(glyph_name, None)
        if "glyf" in font:
            font["glyf"].glyphs.pop(glyph_name, None)
        for tag in ("hmtx", "vmtx"):
            if tag in font:
                font[tag].metrics.pop(glyph_name, None)
        if "VORG" in font:
            font["VORG"].VOriginRecords.pop(glyph_name, None)
        for tag, attributes in {
            "HVAR": ("AdvWidthMap", "LsbMap", "RsbMap"),
            "VVAR": ("AdvHeightMap", "TsbMap", "BsbMap", "VOrgMap"),
        }.items():
            if tag not in font:
                continue
            table = font[tag].table
            for attribute in attributes:
                var_index_map = getattr(table, attribute, None)
                mapping = getattr(var_index_map, "mapping", None)
                if isinstance(mapping, dict):
                    mapping.pop(glyph_name, None)
    font.setGlyphOrder([name for name in font.getGlyphOrder() if name not in removable])
    if "maxp" in font:
        font["maxp"].numGlyphs = len(font.getGlyphOrder())
    return len(removable)


def set_continuation_metrics(
    font: TTFont,
    glyph_name: str,
    source_name: str,
    points: list[tuple[float, float]],
    *,
    horizontal: bool,
) -> None:
    x_min = min(otRound(point[0]) for point in points)
    y_max = max(otRound(point[1]) for point in points)
    source_h_advance = int(font["hmtx"].metrics[source_name][0])
    h_advance = int(font["head"].unitsPerEm) if horizontal else source_h_advance
    font["hmtx"].metrics[glyph_name] = (h_advance, x_min)
    if "vmtx" in font and source_name in font["vmtx"].metrics:
        v_advance = int(font["vmtx"].metrics[source_name][0])
        font["vmtx"].metrics[glyph_name] = (
            v_advance,
            vertical_origin_y(font, source_name) - y_max,
        )


def add_em_dash_continuation_variations(
    font: TTFont,
    em_dash: str,
    em_dash_cont: str,
) -> int:
    if "gvar" not in font or "fvar" not in font:
        return 0
    correction_weights = sorted(
        int(weight)
        for weight in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT
        if int(weight) != int(weight_axis(font).defaultValue)
    )
    supports = advance_supports(font, correction_weights)
    additions = 0
    for weight in correction_weights:
        instance = instantiateVariableFont(font, {"wght": weight}, inplace=False, optimize=True)
        try:
            cases = [
                (
                    em_dash,
                    em_dash_cont,
                    True,
                    horizontal_em_dash_continuation_points,
                )
            ]
            pending: list[tuple[str, list[Any]]] = []
            for source_name, target_name, horizontal, point_builder in cases:
                source_box = glyph_set_bbox(instance, source_name)
                current_points = simple_glyph_coordinates(instance, target_name)
                if not source_box or current_points is None:
                    continue
                source_h_advance = int(instance["hmtx"].metrics[source_name][0])
                source_v_advance = int(instance["vmtx"].metrics[source_name][0])
                pair_cell_advance = int(instance["head"].unitsPerEm)
                target_points = [
                    (otRound(x), otRound(y))
                    for x, y in point_builder(
                        source_box,
                        pair_cell_advance if horizontal else source_v_advance,
                    )
                ]
                if len(current_points) != len(target_points):
                    raise ValueError(f"incompatible em dash continuation topology: {target_name}")
                outline_deltas = [
                    (target_x - current_x, target_y - current_y)
                    for (current_x, current_y), (target_x, target_y) in zip(current_points, target_points)
                ]

                current_h_advance, current_lsb = instance["hmtx"].metrics[target_name]
                target_h_advance = (
                    pair_cell_advance
                    if horizontal
                    else source_h_advance
                )
                current_x_min = min(x for x, _y in current_points)
                target_x_min = min(x for x, _y in target_points)
                target_lsb = target_x_min
                left_phantom_delta = otRound(
                    current_lsb + (target_x_min - current_x_min) - target_lsb
                )
                right_phantom_delta = otRound(
                    left_phantom_delta + target_h_advance - current_h_advance
                )

                current_v_advance, current_tsb = instance["vmtx"].metrics[target_name]
                target_v_advance = source_v_advance
                current_y_max = max(y for _x, y in current_points)
                target_y_max = max(y for _x, y in target_points)
                target_tsb = vertical_origin_y(instance, source_name) - target_y_max
                top_phantom_delta = otRound(
                    target_tsb - current_tsb + target_y_max - current_y_max
                )
                bottom_phantom_delta = otRound(
                    top_phantom_delta - (target_v_advance - current_v_advance)
                )
                coordinates: list[Any] = list(outline_deltas)
                coordinates.extend(
                    [
                        (left_phantom_delta, 0),
                        (right_phantom_delta, 0),
                        (0, top_phantom_delta),
                        (0, bottom_phantom_delta),
                    ]
                )
                if any(delta != (0, 0) for delta in coordinates):
                    pending.append((target_name, coordinates))
        finally:
            instance.close()
        for target_name, coordinates in pending:
            font["gvar"].variations.setdefault(target_name, []).append(
                TupleVariation({"wght": supports[weight]}, coordinates)
            )
            additions += 1
    return additions


def align_em_dash_continuation_variations(font: TTFont) -> dict[str, int]:
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    continuations = em_dash_continuation_glyphs(font) if em_dash else []
    if not em_dash or not continuations:
        return {"continuous_em_dash_variation_corrections": 0}
    em_dash_cont = continuations[0]
    return {
        "continuous_em_dash_variation_corrections": add_em_dash_continuation_variations(
            font,
            em_dash,
            em_dash_cont,
        )
    }


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

    default_location = (
        {"wght": float(weight_axis(font).defaultValue)}
        if "fvar" in font
        else None
    )
    em_dash_box = glyph_set_bbox(font, em_dash, default_location)
    if not em_dash_box:
        return {
            "continuous_em_dash_feature_added": False,
            "continuous_em_dash_vert_mappings": 0,
            "continuous_em_dash_vert_aliases": vert_aliases,
        }
    x_min, _y_min, _x_max, _y_max = em_dash_box
    if x_min <= 0:
        return {
            "continuous_em_dash_feature_added": False,
            "continuous_em_dash_vert_mappings": 0,
            "continuous_em_dash_vert_aliases": vert_aliases,
        }

    pair_cell_advance = int(font["head"].unitsPerEm)

    em_dash_cont = em_dash + ".cont"
    horizontal_points = horizontal_em_dash_continuation_points(
        em_dash_box,
        pair_cell_advance,
    )
    add_simple_glyph(
        font,
        em_dash_cont,
        em_dash,
        horizontal_points,
    )
    set_continuation_metrics(
        font,
        em_dash_cont,
        em_dash,
        horizontal_points,
        horizontal=True,
    )
    variation_additions = add_em_dash_continuation_variations(
        font,
        em_dash,
        em_dash_cont,
    )

    em_dash_start = em_dash + ".pair-start"
    add_fixed_advance_glyph_alias(
        font,
        em_dash_start,
        em_dash,
        pair_cell_advance,
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
    chain.BacktrackCoverage = [coverage(font, [em_dash, em_dash_cont, em_dash_start])]
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

    start_single_sub = ot.SingleSubst()
    start_single_sub.mapping = {em_dash: em_dash_start}
    start_single_lookup = ot.Lookup()
    start_single_lookup.LookupType = 1
    start_single_lookup.LookupFlag = 0
    start_single_lookup.SubTable = [start_single_sub]
    start_single_lookup.SubTableCount = 1
    start_single_index = append_gsub_lookup(font, start_single_lookup)

    start_chain = ot.ChainContextSubst()
    start_chain.Format = 3
    start_chain.BacktrackGlyphCount = 0
    start_chain.BacktrackCoverage = []
    start_chain.InputGlyphCount = 1
    start_chain.InputCoverage = [coverage(font, [em_dash])]
    start_chain.LookAheadGlyphCount = 1
    start_chain.LookAheadCoverage = [coverage(font, [em_dash_cont])]
    start_subst_record = ot.SubstLookupRecord()
    start_subst_record.SequenceIndex = 0
    start_subst_record.LookupListIndex = start_single_index
    start_chain.SubstCount = 1
    start_chain.SubstLookupRecord = [start_subst_record]
    start_chain_lookup = ot.Lookup()
    start_chain_lookup.LookupType = 6
    start_chain_lookup.LookupFlag = 0
    start_chain_lookup.SubTable = [start_chain]
    start_chain_lookup.SubTableCount = 1
    start_chain_index = append_gsub_lookup(font, start_chain_lookup)
    append_gsub_feature(font, "calt", [chain_index, start_chain_index])

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
                        for alias in (em_dash_cont, em_dash_start):
                            if subtable.mapping.get(alias) != subtable.mapping[em_dash]:
                                subtable.mapping[alias] = subtable.mapping[em_dash]
                                vert_mappings += 1

    enable_features_for_all_scripts(font, {"calt", "vert", "vrt2"})
    return {
        "continuous_em_dash_feature_added": True,
        "continuous_em_dash_two_em": True,
        "continuous_em_dash_variations_added": variation_additions,
        "continuous_em_dash_pair_start": em_dash_start,
        "continuous_em_dash_pair_cell_advance": pair_cell_advance,
        "continuous_em_dash_start_chain_lookup": start_chain_index,
        "continuous_em_dash_vert_mappings": vert_mappings,
        "continuous_em_dash_vert_aliases": vert_aliases,
    }


def coverage_has_glyph(coverage_table: Any, glyph_name: str) -> bool:
    return glyph_name in list(getattr(coverage_table, "glyphs", []) or [])


def lookup_single_substitution_mapping(gsub: Any, lookup_index: int) -> dict[str, str]:
    if not gsub.LookupList or lookup_index >= len(gsub.LookupList.Lookup):
        return {}
    lookup = gsub.LookupList.Lookup[lookup_index]
    if lookup.LookupType != 1:
        return {}
    mapping: dict[str, str] = {}
    for subtable in lookup.SubTable:
        if hasattr(subtable, "mapping"):
            mapping.update(subtable.mapping)
    return mapping


def chain_substitution_records(subtable: Any) -> list[Any]:
    records = list(getattr(subtable, "SubstLookupRecord", []) or [])
    for rule_set_attr in ("SubRuleSet", "ChainSubRuleSet", "SubClassSet", "ChainSubClassSet"):
        for rule_set in getattr(subtable, rule_set_attr, []) or []:
            if not rule_set:
                continue
            for rule_list_attr in ("SubRule", "ChainSubRule", "SubClassRule", "ChainSubClassRule"):
                for rule in getattr(rule_set, rule_list_attr, []) or []:
                    records.extend(list(getattr(rule, "SubstLookupRecord", []) or []))
    return records


def is_em_dash_chain_lookup(font: TTFont, lookup_index: int) -> bool:
    if "GSUB" not in font:
        return False
    cmap = font.getBestCmap()
    em_dash = cmap.get(0x2014)
    if not em_dash:
        return False
    gsub = font["GSUB"].table
    if not gsub.LookupList or lookup_index >= len(gsub.LookupList.Lookup):
        return False
    lookup = gsub.LookupList.Lookup[lookup_index]
    if lookup.LookupType != 6:
        return False
    for subtable in lookup.SubTable:
        input_matches = any(coverage_has_glyph(cov, em_dash) for cov in list(getattr(subtable, "InputCoverage", []) or []))
        backtrack_matches = any(
            coverage_has_glyph(cov, em_dash) for cov in list(getattr(subtable, "BacktrackCoverage", []) or [])
        )
        if not input_matches or not backtrack_matches:
            continue
        for subst_record in chain_substitution_records(subtable):
            mapping = lookup_single_substitution_mapping(gsub, subst_record.LookupListIndex)
            target = mapping.get(em_dash)
            if target and target != em_dash:
                return True
    return False


def em_dash_chain_lookup_indices(font: TTFont) -> set[int]:
    if "GSUB" not in font or not font["GSUB"].table.LookupList:
        return set()
    return {
        lookup_index
        for lookup_index in range(len(font["GSUB"].table.LookupList.Lookup))
        if is_em_dash_chain_lookup(font, lookup_index)
    }


def em_dash_continuation_glyphs(font: TTFont) -> list[str]:
    if "GSUB" not in font:
        return []
    cmap = font.getBestCmap()
    em_dash = cmap.get(0x2014)
    if not em_dash:
        return []
    gsub = font["GSUB"].table
    glyphs: list[str] = []
    for lookup_index in sorted(em_dash_chain_lookup_indices(font)):
        lookup = gsub.LookupList.Lookup[lookup_index]
        for subtable in lookup.SubTable:
            for subst_record in chain_substitution_records(subtable):
                mapping = lookup_single_substitution_mapping(gsub, subst_record.LookupListIndex)
                target = mapping.get(em_dash)
                if target and target != em_dash and target not in glyphs:
                    glyphs.append(target)
    return glyphs


def em_dash_pair_start_chain_lookup_indices(font: TTFont) -> set[int]:
    if "GSUB" not in font or not font["GSUB"].table.LookupList:
        return set()
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    continuations = set(em_dash_continuation_glyphs(font)) if em_dash else set()
    if not em_dash or not continuations:
        return set()
    gsub = font["GSUB"].table
    result: set[int] = set()
    for lookup_index, lookup in enumerate(gsub.LookupList.Lookup):
        if lookup.LookupType != 6:
            continue
        for subtable in lookup.SubTable:
            input_matches = any(
                coverage_has_glyph(coverage_table, em_dash)
                for coverage_table in list(
                    getattr(subtable, "InputCoverage", []) or []
                )
            )
            lookahead_matches = any(
                any(
                    coverage_has_glyph(coverage_table, continuation)
                    for continuation in continuations
                )
                for coverage_table in list(
                    getattr(subtable, "LookAheadCoverage", []) or []
                )
            )
            if not input_matches or not lookahead_matches:
                continue
            for subst_record in chain_substitution_records(subtable):
                mapping = lookup_single_substitution_mapping(
                    gsub,
                    subst_record.LookupListIndex,
                )
                target = mapping.get(em_dash)
                if target and target != em_dash and target not in continuations:
                    result.add(lookup_index)
                    break
    return result


def em_dash_pair_start_glyphs(font: TTFont) -> list[str]:
    if "GSUB" not in font:
        return []
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    if not em_dash:
        return []
    gsub = font["GSUB"].table
    glyphs: list[str] = []
    for lookup_index in sorted(em_dash_pair_start_chain_lookup_indices(font)):
        lookup = gsub.LookupList.Lookup[lookup_index]
        for subtable in lookup.SubTable:
            for subst_record in chain_substitution_records(subtable):
                mapping = lookup_single_substitution_mapping(
                    gsub,
                    subst_record.LookupListIndex,
                )
                target = mapping.get(em_dash)
                if target and target != em_dash and target not in glyphs:
                    glyphs.append(target)
    return glyphs


def em_dash_calt_chain_lookup_indices(font: TTFont) -> list[int]:
    return sorted(
        em_dash_chain_lookup_indices(font)
        | em_dash_pair_start_chain_lookup_indices(font)
    )


def ensure_variable_em_dash_pair_start_feature(font: TTFont) -> dict[str, Any]:
    if "fvar" not in font or "gvar" not in font or "GSUB" not in font:
        return {"variable_em_dash_pair_start_feature_added": False}
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    continuations = em_dash_continuation_glyphs(font) if em_dash else []
    starts = em_dash_pair_start_glyphs(font) if em_dash else []
    if not em_dash or len(continuations) != 1 or len(starts) > 1:
        raise ValueError(
            "variable em dash pair start requires one continuation and at most one start alias"
        )

    feature_added = False
    linked_records = 0
    start_chain_index: int | None = None
    if starts:
        em_dash_start = starts[0]
    else:
        em_dash_cont = continuations[0]
        glyph_names = set(font.getGlyphOrder())
        em_dash_start = f"{em_dash}.pair-start"
        suffix = 1
        while em_dash_start in glyph_names:
            em_dash_start = f"{em_dash}.pair-start.{suffix}"
            suffix += 1
        add_fixed_advance_glyph_alias(
            font,
            em_dash_start,
            em_dash,
            int(font["head"].unitsPerEm),
        )

        start_single_sub = ot.SingleSubst()
        start_single_sub.mapping = {em_dash: em_dash_start}
        start_single_lookup = ot.Lookup()
        start_single_lookup.LookupType = 1
        start_single_lookup.LookupFlag = 0
        start_single_lookup.SubTable = [start_single_sub]
        start_single_lookup.SubTableCount = 1
        start_single_index = append_gsub_lookup(font, start_single_lookup)

        start_chain = ot.ChainContextSubst()
        start_chain.Format = 3
        start_chain.BacktrackGlyphCount = 0
        start_chain.BacktrackCoverage = []
        start_chain.InputGlyphCount = 1
        start_chain.InputCoverage = [coverage(font, [em_dash])]
        start_chain.LookAheadGlyphCount = 1
        start_chain.LookAheadCoverage = [coverage(font, [em_dash_cont])]
        start_subst_record = ot.SubstLookupRecord()
        start_subst_record.SequenceIndex = 0
        start_subst_record.LookupListIndex = start_single_index
        start_chain.SubstCount = 1
        start_chain.SubstLookupRecord = [start_subst_record]
        start_chain_lookup = ot.Lookup()
        start_chain_lookup.LookupType = 6
        start_chain_lookup.LookupFlag = 0
        start_chain_lookup.SubTable = [start_chain]
        start_chain_lookup.SubTableCount = 1
        start_chain_index = append_gsub_lookup(font, start_chain_lookup)

        continuation_indices = em_dash_chain_lookup_indices(font)
        feature_list = font["GSUB"].table.FeatureList
        if feature_list:
            for record in feature_list.FeatureRecord:
                if record.FeatureTag != "calt":
                    continue
                indices = list(record.Feature.LookupListIndex or [])
                if not any(index in continuation_indices for index in indices):
                    continue
                if start_chain_index not in indices:
                    indices.append(start_chain_index)
                    record.Feature.LookupListIndex = indices
                    record.Feature.LookupCount = len(indices)
                    linked_records += 1
        if not linked_records:
            append_gsub_feature(font, "calt", [start_chain_index])
            enable_features_for_all_scripts(font, {"calt"})
            linked_records = 1
        feature_added = True

    vert_mappings = 0
    for tag in ("vert", "vrt2"):
        mapping = get_single_substitution_mapping(font, tag)
        vertical_em_dash = mapping.get(em_dash)
        if not vertical_em_dash:
            continue
        for record in font["GSUB"].table.FeatureList.FeatureRecord:
            if record.FeatureTag != tag:
                continue
            for lookup_index in record.Feature.LookupListIndex:
                lookup = font["GSUB"].table.LookupList.Lookup[lookup_index]
                if lookup.LookupType != 1:
                    continue
                for subtable in lookup.SubTable:
                    if (
                        hasattr(subtable, "mapping")
                        and subtable.mapping.get(em_dash) == vertical_em_dash
                        and subtable.mapping.get(em_dash_start) != vertical_em_dash
                    ):
                        subtable.mapping[em_dash_start] = vertical_em_dash
                        vert_mappings += 1

    return {
        "variable_em_dash_pair_start_feature_added": feature_added,
        "variable_em_dash_pair_start_feature_links": linked_records,
        "variable_em_dash_pair_start_chain_lookup": start_chain_index,
        "variable_em_dash_pair_start_vert_mappings": vert_mappings,
        "variable_em_dash_pair_start": em_dash_start,
    }


def ligature_substitution_subtables(lookup: Any) -> list[Any]:
    if lookup.LookupType == 4:
        return list(lookup.SubTable)
    if lookup.LookupType == 7:
        return [
            subtable.ExtSubTable
            for subtable in lookup.SubTable
            if getattr(subtable, "ExtensionLookupType", None) == 4
            and getattr(subtable, "ExtSubTable", None)
        ]
    return []


def remap_unicode_cmap(font: TTFont, codepoint: int, glyph_name: str) -> int:
    changed = 0
    for cmap_table in font["cmap"].tables if "cmap" in font else []:
        if not cmap_table.isUnicode() or codepoint not in cmap_table.cmap:
            continue
        if cmap_table.cmap[codepoint] != glyph_name:
            cmap_table.cmap[codepoint] = glyph_name
            changed += 1
    return changed


def rectangular_corner_indices(font: TTFont, glyph_name: str) -> dict[tuple[int, int], int]:
    glyph = font["glyf"][glyph_name]
    glyph.expand(font["glyf"])
    if glyph.isComposite() or glyph.numberOfContours != 1 or len(glyph.coordinates) != 4:
        raise ValueError(f"dash glyph is not a four-point rectangle: {glyph_name}")
    y_values = sorted({int(y) for _x, y in glyph.coordinates})
    if len(y_values) != 2:
        raise ValueError(f"dash glyph does not have two edges: {glyph_name}")
    result: dict[tuple[int, int], int] = {}
    for y_index, y_value in enumerate(y_values):
        row = sorted(
            (
                (int(x), point_index)
                for point_index, (x, y) in enumerate(glyph.coordinates)
                if int(y) == y_value
            ),
            key=lambda item: item[0],
        )
        if len(row) != 2:
            raise ValueError(f"dash glyph edge is not rectangular: {glyph_name}")
        for x_index, (_x, point_index) in enumerate(row):
            result[(x_index, y_index)] = point_index
    return result


def copy_static_dash_outline(
    target: TTFont,
    target_name: str,
    source: TTFont,
    source_name: str,
    *,
    italic: bool,
) -> None:
    target_indices = rectangular_corner_indices(target, target_name)
    source_indices = rectangular_corner_indices(source, source_name)
    source_glyph = source["glyf"][source_name]
    target_glyph = target["glyf"][target_name]
    shear = math.tan(math.radians(9.4)) if italic else 0.0
    for corner, target_index in target_indices.items():
        source_x, source_y = source_glyph.coordinates[source_indices[corner]]
        target_glyph.coordinates[target_index] = (
            otRound(source_x + source_y * shear),
            int(source_y),
        )
    target_glyph.recalcBounds(target["glyf"])
    source_advance = int(source["hmtx"].metrics[source_name][0])
    target["hmtx"].metrics[target_name] = (source_advance, int(target_glyph.xMin))
    if "vmtx" in target and "vmtx" in source and source_name in source["vmtx"].metrics:
        target["vmtx"].metrics[target_name] = copy.deepcopy(
            source["vmtx"].metrics[source_name]
        )
    if "VORG" in target and "VORG" in source:
        if source_name in source["VORG"].VOriginRecords:
            target["VORG"].VOriginRecords[target_name] = int(
                source["VORG"].VOriginRecords[source_name]
            )
        else:
            target["VORG"].VOriginRecords.pop(target_name, None)


def prepare_static_upstream_dash_glyphs(
    font: TTFont,
    source: TTFont,
    region: str,
    italic: bool,
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, int]]:
    source_roles = upstream_dash_roles(source)
    cmap = font.getBestCmap() or {}
    roles = {
        "proportional": cmap.get(0x2014),
        "fullwidth": cmap.get(0x2015),
        "encoded_two": cmap.get(0x2E3A),
        "encoded_three": cmap.get(0x2E3B),
        "vertical_single": cmap.get(0xFE31),
    }
    missing = [name for name, glyph_name in roles.items() if not glyph_name]
    if missing:
        raise ValueError(f"static dash glyphs are missing: {missing}")
    roles = {name: str(glyph_name) for name, glyph_name in roles.items()}

    if region == "CL":
        roles["fullwidth_two"] = roles["encoded_two"]
        roles["fullwidth_three"] = roles["encoded_three"]
    else:
        roles["fullwidth_two"] = clone_glyph(
            font, roles["encoded_two"], f"{roles['fullwidth']}.two-em"
        )
        roles["fullwidth_three"] = clone_glyph(
            font, roles["encoded_three"], f"{roles['fullwidth']}.three-em"
        )
    roles["vertical_two"] = clone_glyph(
        font, roles["vertical_single"], f"{roles['vertical_single']}.two-em"
    )
    roles["vertical_three"] = clone_glyph(
        font, roles["vertical_single"], f"{roles['vertical_single']}.three-em"
    )
    missing_clones = [
        role
        for role in ("fullwidth_two", "fullwidth_three", "vertical_two", "vertical_three")
        if not roles.get(role)
    ]
    if missing_clones:
        raise ValueError(f"could not clone static dash glyphs: {missing_clones}")
    roles = {name: str(glyph_name) for name, glyph_name in roles.items()}

    copied = 0
    for role in (
        "proportional",
        "fullwidth",
        "encoded_two",
        "encoded_three",
        "vertical_single",
        "fullwidth_two",
        "fullwidth_three",
        "vertical_two",
        "vertical_three",
    ):
        target_name = roles[role]
        source_name = source_roles[role]
        copy_static_dash_outline(
            font,
            target_name,
            source,
            source_name,
            italic=italic,
        )
        copied += 1

    source_to_target = {
        source_roles[role]: roles[role]
        for role in source_roles
        if role in roles
    }
    localized_mappings: dict[str, dict[str, str]] = {}
    localized_clones = 0
    source_localized_mappings = (
        {}
        if region == "CL"
        else normalized_source_han_dash_locl_mappings(source, source_roles)
    )
    for language, source_mapping in source_localized_mappings.items():
        target_mapping: dict[str, str] = {}
        for source_name, source_target in source_mapping.items():
            target_name = source_to_target.get(source_name)
            if not target_name:
                raise ValueError(
                    f"unrecognized static dash locl source for {language}: {source_name}"
                )
            target_target = source_to_target.get(source_target)
            if not target_target:
                target_target = clone_glyph(
                    font,
                    roles["proportional"],
                    f"{roles['proportional']}.locl-{language.strip().lower()}",
                )
                if not target_target:
                    raise ValueError(
                        f"could not clone static dash locl target for {language}"
                    )
                copy_static_dash_outline(
                    font,
                    target_target,
                    source,
                    source_target,
                    italic=italic,
                )
                source_to_target[source_target] = target_target
                localized_clones += 1
                copied += 1
            target_mapping[target_name] = target_target
        localized_mappings[language] = target_mapping

    return roles, localized_mappings, {
        "upstream_dash_static_glyphs_copied": copied,
        "upstream_dash_static_locl_glyphs_cloned": localized_clones,
        "upstream_dash_static_cmap_remaps": 0,
    }


def insert_gsub_feature_record(font: TTFont, tag: str, lookup_indices: list[int]) -> int:
    gsub = font["GSUB"].table
    records = gsub.FeatureList.FeatureRecord
    insert_at = max(
        (index + 1 for index, record in enumerate(records) if record.FeatureTag == tag),
        default=len(records),
    )
    feature = ot.Feature()
    feature.FeatureParams = None
    feature.LookupListIndex = list(lookup_indices)
    feature.LookupCount = len(lookup_indices)
    record = ot.FeatureRecord()
    record.FeatureTag = tag
    record.Feature = feature
    records.insert(insert_at, record)
    gsub.FeatureList.FeatureCount = len(records)
    if gsub.ScriptList:
        for script_record in gsub.ScriptList.ScriptRecord:
            langsys_items = []
            if script_record.Script.DefaultLangSys:
                langsys_items.append(script_record.Script.DefaultLangSys)
            langsys_items.extend(
                langsys_record.LangSys
                for langsys_record in script_record.Script.LangSysRecord
            )
            for langsys in langsys_items:
                required = int(getattr(langsys, "ReqFeatureIndex", 0xFFFF))
                if required != 0xFFFF and required >= insert_at:
                    langsys.ReqFeatureIndex = required + 1
                langsys.FeatureIndex = [
                    index + 1 if index >= insert_at else index
                    for index in list(langsys.FeatureIndex or [])
                ]
                langsys.FeatureCount = len(langsys.FeatureIndex)
    return insert_at


def langsys_records_for_language(font: TTFont, language: str) -> list[Any]:
    if "GSUB" not in font or not font["GSUB"].table.ScriptList:
        return []
    return [
        lang_record.LangSys
        for script_record in font["GSUB"].table.ScriptList.ScriptRecord
        for lang_record in script_record.Script.LangSysRecord
        if lang_record.LangSysTag == language
    ]


def clear_empty_catalan_locl(font: TTFont) -> int:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return 0
    gsub = font["GSUB"].table
    cleared: set[int] = set()
    for langsys in langsys_records_for_language(font, "CAT "):
        for feature_index in list(langsys.FeatureIndex or []):
            record = gsub.FeatureList.FeatureRecord[feature_index]
            if record.FeatureTag != "locl":
                continue
            if record.Feature.LookupListIndex:
                record.Feature.LookupListIndex = []
                record.Feature.LookupCount = 0
                cleared.add(feature_index)
    return len(cleared)


def add_lookup_to_all_features(font: TTFont, tag: str, lookup_index: int) -> int:
    linked = 0
    for record in font["GSUB"].table.FeatureList.FeatureRecord:
        if record.FeatureTag != tag:
            continue
        indices = list(record.Feature.LookupListIndex or [])
        if lookup_index not in indices:
            indices.insert(0, lookup_index)
            record.Feature.LookupListIndex = indices
            record.Feature.LookupCount = len(indices)
            linked += 1
    return linked


def make_single_substitution_lookup(mapping: dict[str, str]) -> ot.Lookup:
    subtable = ot.SingleSubst()
    subtable.mapping = dict(mapping)
    lookup = ot.Lookup()
    lookup.LookupType = 1
    lookup.LookupFlag = 0
    lookup.SubTable = [subtable]
    lookup.SubTableCount = 1
    lookup.MarkFilteringSet = None
    return lookup


def make_ligature_substitution_lookup(
    rules: dict[str, list[tuple[list[str], str]]],
) -> ot.Lookup:
    subtable = ot.LigatureSubst()
    subtable.ligatures = {}
    for first_name, first_rules in rules.items():
        ligatures = []
        for components, output_name in sorted(
            first_rules,
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            ligature = ot.Ligature()
            ligature.Component = list(components)
            ligature.CompCount = len(components) + 1
            ligature.LigGlyph = output_name
            ligatures.append(ligature)
        subtable.ligatures[first_name] = ligatures
    lookup = ot.Lookup()
    lookup.LookupType = 4
    lookup.LookupFlag = 0
    lookup.SubTable = [subtable]
    lookup.SubTableCount = 1
    lookup.MarkFilteringSet = None
    return lookup


def remove_dash_pair_positioning(
    font: TTFont,
    dash_glyphs: set[str],
) -> int:
    if "GPOS" not in font or not font["GPOS"].table.FeatureList:
        return 0
    gpos = font["GPOS"].table
    lookup_indices = {
        int(lookup_index)
        for record in gpos.FeatureList.FeatureRecord
        if record.FeatureTag in {"vert", "vrt2"}
        for lookup_index in list(record.Feature.LookupListIndex or [])
    }
    removed = 0
    for lookup_index in lookup_indices:
        if not gpos.LookupList or lookup_index >= len(gpos.LookupList.Lookup):
            continue
        lookup = gpos.LookupList.Lookup[lookup_index]
        if lookup.LookupType != 2:
            continue
        for subtable in lookup.SubTable:
            if getattr(subtable, "Format", None) != 1:
                continue
            coverage_glyphs = list(getattr(subtable.Coverage, "glyphs", []) or [])
            pair_sets = list(getattr(subtable, "PairSet", []) or [])
            kept_coverage = []
            kept_pair_sets = []
            for first_name, pair_set in zip(coverage_glyphs, pair_sets):
                records = list(pair_set.PairValueRecord or [])
                kept_records = [
                    record
                    for record in records
                    if not (
                        first_name in dash_glyphs
                        and record.SecondGlyph in dash_glyphs
                    )
                ]
                removed += len(records) - len(kept_records)
                if kept_records:
                    pair_set.PairValueRecord = kept_records
                    pair_set.PairValueCount = len(kept_records)
                    kept_coverage.append(first_name)
                    kept_pair_sets.append(pair_set)
            subtable.Coverage.glyphs = glyph_order_sorted(font, kept_coverage)
            pair_by_first = dict(zip(kept_coverage, kept_pair_sets))
            subtable.PairSet = [pair_by_first[name] for name in subtable.Coverage.glyphs]
            subtable.PairSetCount = len(subtable.PairSet)
    return removed


def strip_legacy_dash_layout(
    font: TTFont,
    roles: dict[str, str],
) -> tuple[set[str], dict[str, int]]:
    if "GSUB" not in font or not font["GSUB"].table.FeatureList:
        return set(), {"upstream_dash_legacy_gsub_lookups_removed": 0}
    obsolete_glyphs = set(em_dash_continuation_glyphs(font)) | set(
        em_dash_pair_start_glyphs(font)
    )
    chain_indices = set(em_dash_calt_chain_lookup_indices(font))
    lookup_report = remove_gsub_lookups(font, chain_indices)

    gsub = font["GSUB"].table
    ccmp_sources = {roles["proportional"], roles["fullwidth"]}
    locl_sources = {
        roles["proportional"],
        roles["encoded_two"],
        roles["encoded_three"],
    }
    vertical_sources = {
        roles["proportional"],
        roles["fullwidth"],
        roles["encoded_two"],
        roles["encoded_three"],
        roles["fullwidth_two"],
        roles["fullwidth_three"],
        *obsolete_glyphs,
    }
    removed_single = 0
    removed_ligatures = 0
    visited: set[tuple[str, int]] = set()
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in {"ccmp", "locl", "vert", "vrt2"}:
            continue
        for lookup_index in list(record.Feature.LookupListIndex or []):
            key = (record.FeatureTag, int(lookup_index))
            if key in visited or lookup_index >= len(gsub.LookupList.Lookup):
                continue
            visited.add(key)
            lookup = gsub.LookupList.Lookup[lookup_index]
            if record.FeatureTag == "ccmp":
                for subtable in ligature_substitution_subtables(lookup):
                    for first_name in list(subtable.ligatures):
                        if first_name not in ccmp_sources:
                            continue
                        ligatures = list(subtable.ligatures[first_name] or [])
                        kept = [
                            ligature
                            for ligature in ligatures
                            if not (
                                len(ligature.Component) in {1, 2}
                                and all(
                                    component == first_name
                                    for component in ligature.Component
                                )
                            )
                        ]
                        removed_ligatures += len(ligatures) - len(kept)
                        if kept:
                            subtable.ligatures[first_name] = kept
                        else:
                            del subtable.ligatures[first_name]
                continue
            sources = locl_sources if record.FeatureTag == "locl" else vertical_sources
            for subtable in single_substitution_subtables(lookup):
                mapping = getattr(subtable, "mapping", None)
                if not mapping:
                    continue
                for source_name in list(mapping):
                    if source_name in sources or mapping[source_name] in obsolete_glyphs:
                        del mapping[source_name]
                        removed_single += 1

    dash_glyphs = set(roles.values()) | obsolete_glyphs
    removed_pair_positions = remove_dash_pair_positioning(font, dash_glyphs)
    return obsolete_glyphs, {
        "upstream_dash_legacy_gsub_lookups_removed": int(
            lookup_report.get("gsub_lookups_removed", 0)
        ),
        "upstream_dash_legacy_single_mappings_removed": removed_single,
        "upstream_dash_legacy_ligatures_removed": removed_ligatures,
        "upstream_dash_legacy_pair_positions_removed": removed_pair_positions,
    }


def link_lookup_to_all_gsub_features(font: TTFont, tag: str, lookup_index: int) -> int:
    linked = add_lookup_to_all_features(font, tag, lookup_index)
    if linked:
        return linked
    feature_index = insert_gsub_feature_record(font, tag, [lookup_index])
    if not font["GSUB"].table.ScriptList:
        return 1
    for script_record in font["GSUB"].table.ScriptList.ScriptRecord:
        langsys_items = []
        if script_record.Script.DefaultLangSys:
            langsys_items.append(script_record.Script.DefaultLangSys)
        langsys_items.extend(
            record.LangSys for record in script_record.Script.LangSysRecord
        )
        for langsys in langsys_items:
            indices = sorted(set(list(langsys.FeatureIndex or []) + [feature_index]))
            langsys.FeatureIndex = indices
            langsys.FeatureCount = len(indices)
    return 1


def add_language_specific_locl(
    font: TTFont,
    mappings: dict[str, dict[str, str]],
    *,
    report_prefix: str,
    purpose: str,
) -> dict[str, int]:
    gsub = font["GSUB"].table
    lookup_by_mapping: dict[tuple[tuple[str, str], ...], int] = {}
    feature_mappings: dict[int, tuple[tuple[str, str], ...]] = {}
    missing: dict[tuple[tuple[str, str], ...], list[Any]] = {}
    linked_features: set[int] = set()
    langsys_linked = 0
    for language, mapping in mappings.items():
        records = langsys_records_for_language(font, language)
        if not records:
            raise ValueError(f"missing GSUB LangSys for {purpose} locl language {language}")
        mapping_signature = tuple(sorted(mapping.items()))
        for langsys in records:
            feature_indices = [
                int(feature_index)
                for feature_index in list(langsys.FeatureIndex or [])
                if gsub.FeatureList.FeatureRecord[feature_index].FeatureTag == "locl"
            ]
            if not feature_indices:
                missing.setdefault(mapping_signature, []).append(langsys)
                continue
            feature_index = feature_indices[0]
            previous_mapping = feature_mappings.setdefault(
                feature_index,
                mapping_signature,
            )
            if previous_mapping != mapping_signature:
                raise ValueError(
                    "one GSUB locl FeatureRecord is shared by incompatible "
                    f"{purpose} language mappings"
                )
            linked_features.add(feature_index)
            langsys_linked += 1

    lookups_added = 0
    features_added = 0

    def lookup_for(mapping_signature: tuple[tuple[str, str], ...]) -> int:
        nonlocal lookups_added
        lookup_index = lookup_by_mapping.get(mapping_signature)
        if lookup_index is None:
            lookup_index = append_gsub_lookup(
                font,
                make_single_substitution_lookup(dict(mapping_signature)),
            )
            lookup_by_mapping[mapping_signature] = lookup_index
            lookups_added += 1
        return lookup_index

    for feature_index, mapping_signature in feature_mappings.items():
        feature = gsub.FeatureList.FeatureRecord[feature_index].Feature
        lookup_index = lookup_for(mapping_signature)
        old_lookups = list(feature.LookupListIndex or [])
        feature.LookupListIndex = list(dict.fromkeys([lookup_index, *old_lookups]))
        feature.LookupCount = len(feature.LookupListIndex)

    for mapping_signature, langsys_items in missing.items():
        lookup_index = lookup_for(mapping_signature)
        feature_index = insert_gsub_feature_record(font, "locl", [lookup_index])
        features_added += 1
        for langsys in langsys_items:
            indices = list(langsys.FeatureIndex or [])
            tags = [
                gsub.FeatureList.FeatureRecord[index].FeatureTag
                for index in indices
            ]
            insert_at = max(
                (index + 1 for index, tag in enumerate(tags) if tag == "hist"),
                default=len(indices),
            )
            indices.insert(insert_at, feature_index)
            langsys.FeatureIndex = indices
            langsys.FeatureCount = len(langsys.FeatureIndex)
            langsys_linked += 1
    return {
        f"{report_prefix}_locl_lookups_added": lookups_added,
        f"{report_prefix}_locl_features_added": features_added,
        f"{report_prefix}_locl_existing_features_linked": len(linked_features),
        f"{report_prefix}_locl_langsys_linked": langsys_linked,
    }


def apply_cjk_ellipsis_behavior(font: TTFont) -> dict[str, Any]:
    if "GSUB" not in font or "glyf" not in font:
        return {"cjk_ellipsis_behavior_applied": False}
    current_status = cjk_ellipsis_structure_status(font)
    if current_status["ok"]:
        return {
            "cjk_ellipsis_behavior_applied": False,
            "cjk_ellipsis_already_valid": True,
            "cjk_ellipsis_mechanism": current_status["mechanism"],
            "cjk_ellipsis_roles": current_status["roles"],
        }
    roles = cjk_ellipsis_roles(font)
    localized_mappings: dict[str, dict[str, str]] = {}
    for language in sorted(CJK_LOCL_LANGUAGES):
        existing = langsys_single_substitution_mapping(font, language, "locl")
        if existing.get(roles["proportional"]) != roles["fullwidth"]:
            localized_mappings[language] = {
                roles["proportional"]: roles["fullwidth"],
            }
    report: dict[str, Any] = add_language_specific_locl(
        font,
        localized_mappings,
        report_prefix="cjk_ellipsis",
        purpose="CJK ellipsis",
    )
    for tag in ("vert", "vrt2"):
        vertical_mapping = {
            source_name: roles["vertical"]
            for source_name in (roles["proportional"], roles["fullwidth"])
        }
        lookup_index = append_gsub_lookup(
            font,
            make_single_substitution_lookup(vertical_mapping),
        )
        linked = link_lookup_to_all_gsub_features(font, tag, lookup_index)
        report[f"cjk_ellipsis_{tag}_mappings_added"] = len(vertical_mapping)
        report[f"cjk_ellipsis_{tag}_feature_records_linked"] = linked
    report.update(
        {
            "cjk_ellipsis_behavior_applied": True,
            "cjk_ellipsis_mechanism": "Ui proportional default; CJK locl fullwidth; vert/vrt2 vertical",
            "cjk_ellipsis_roles": roles,
        }
    )
    return report


def apply_default_regional_punctuation(font: TTFont, region: str) -> dict[str, Any]:
    language = {"CL": "ZHT ", "SC": "ZHS ", "TC": "ZHT ", "HC": "ZHH ", "J": "JAN ", "K": "KOR "}.get(region)
    if language is None:
        return {"default_regional_punctuation": False}
    roles = cjk_ellipsis_roles(font)
    mapping = dict(dash_locl_mappings_by_language(font).get(language, {}))
    mapping[roles["proportional"]] = roles["fullwidth"]
    gsub = font["GSUB"].table
    source_indices = {
        lookup
        for langsys in langsys_records_for_language(font, language)
        for index in langsys.FeatureIndex
        if gsub.FeatureList.FeatureRecord[index].FeatureTag == "locl"
        for lookup in gsub.FeatureList.FeatureRecord[index].Feature.LookupListIndex
    }
    lookup_indices = sorted(index for index in source_indices if (
        lookup_single_substitution_mapping(gsub, index)
        and set(lookup_single_substitution_mapping(gsub, index).items()) <= set(mapping.items())
    ))
    covered = {}
    for index in lookup_indices:
        covered.update(lookup_single_substitution_mapping(gsub, index))
    if covered != mapping:
        raise ValueError("缺少可复用的地区标点 locl lookup")
    default_langs = [record.Script.DefaultLangSys for record in gsub.ScriptList.ScriptRecord if record.Script.DefaultLangSys]
    # Repeated finalization must not add duplicate lookups or features.
    matches = []
    for langsys in default_langs:
        for index in langsys.FeatureIndex:
            record = gsub.FeatureList.FeatureRecord[index]
            if record.FeatureTag != "locl":
                continue
            combined = {}
            for lookup in record.Feature.LookupListIndex:
                combined.update(lookup_single_substitution_mapping(gsub, lookup))
            if combined == mapping:
                matches.append(index)
                break
    if len(matches) == len(default_langs):
        for index in set(matches):
            feature = gsub.FeatureList.FeatureRecord[index].Feature
            feature.LookupListIndex = list(lookup_indices)
            feature.LookupCount = len(lookup_indices)
        return {"default_regional_punctuation": True, "default_punctuation_already_valid": True}
    # Reuse the original locl lookups in their ccmp -> locl -> vert order.
    # Appending a new substitution after vert would collapse a vertical 2em
    # dash to the source's 1em default before localization can run.
    feature_index = insert_gsub_feature_record(font, "locl", lookup_indices)
    for record in gsub.ScriptList.ScriptRecord:
        script = record.Script
        if not script.DefaultLangSys:
            continue
        if record.ScriptTag in {"DFLT", "latn"} and not any(lang.LangSysTag == "ENG " for lang in script.LangSysRecord):
            english = ot.LangSysRecord()
            english.LangSysTag = "ENG "
            english.LangSys = copy.deepcopy(script.DefaultLangSys)
            script.LangSysRecord.append(english)
            script.LangSysRecord.sort(key=lambda lang: lang.LangSysTag)
            script.LangSysCount = len(script.LangSysRecord)
        script.DefaultLangSys = copy.deepcopy(script.DefaultLangSys)
        script.DefaultLangSys.FeatureIndex.append(feature_index)
        script.DefaultLangSys.FeatureCount = len(script.DefaultLangSys.FeatureIndex)
    return {"default_regional_punctuation": True, "default_punctuation_language": language.strip()}


def cjk_ellipsis_structure_status(font: TTFont) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        roles = cjk_ellipsis_roles(font)
    except Exception as error:
        return {
            "ok": False,
            "reasons": [f"could not resolve CJK ellipsis roles: {error}"],
        }
    if roles["proportional"] == roles["fullwidth"]:
        reasons.append("U+2026 proportional and U+22EF CJK ellipsis are not distinct")
    locl: dict[str, dict[str, str]] = {}
    for language in sorted(CJK_LOCL_LANGUAGES):
        mapping = langsys_single_substitution_mapping(font, language, "locl")
        actual = mapping.get(roles["proportional"])
        locl[language] = {roles["proportional"]: actual} if actual else {}
        if actual != roles["fullwidth"]:
            reasons.append(
                f"{language} ellipsis locl maps to {actual!r}, "
                f"expected {roles['fullwidth']!r}"
            )
    vertical_records: dict[str, list[int]] = {}
    for tag in ("vert", "vrt2"):
        records = single_substitution_mappings_by_feature_record(font, tag)
        vertical_records[tag] = [feature_index for feature_index, _mapping in records]
        if not records:
            reasons.append(f"no {tag} FeatureRecord is available for CJK ellipsis")
        for feature_index, mapping in records:
            for source_name in (roles["proportional"], roles["fullwidth"]):
                if mapping.get(source_name) != roles["vertical"]:
                    reasons.append(
                        f"{tag} FeatureRecord {feature_index} maps {source_name}->"
                        f"{mapping.get(source_name)!r}, expected {roles['vertical']!r}"
                    )
    upem = int(font["head"].unitsPerEm)
    fullwidth_advance = int(font["hmtx"].metrics[roles["fullwidth"]][0])
    if fullwidth_advance != upem:
        reasons.append(
            f"CJK ellipsis horizontal advance {fullwidth_advance} != {upem}"
        )
    vertical_advance = int(font["vmtx"].metrics[roles["vertical"]][0])
    if vertical_advance != upem:
        reasons.append(
            f"CJK ellipsis vertical advance {vertical_advance} != {upem}"
        )
    return {
        "ok": not reasons,
        "reasons": reasons,
        "mechanism": "Ui proportional default; CJK locl fullwidth; vert/vrt2 vertical",
        "roles": roles,
        "locl": locl,
        "vertical_feature_records": vertical_records,
        "fullwidth_advance": fullwidth_advance,
        "vertical_advance": vertical_advance,
    }


def apply_upstream_dash_behavior(
    font: TTFont,
    region: str,
    *,
    static_source: TTFont | None = None,
    italic: bool = False,
) -> dict[str, Any]:
    if "GSUB" not in font or "glyf" not in font:
        return {"upstream_dash_behavior_applied": False}
    report: dict[str, Any] = {}
    if static_source is not None:
        roles, localized_mappings, static_report = prepare_static_upstream_dash_glyphs(
            font,
            static_source,
            region,
            italic,
        )
        report.update(static_report)
    else:
        roles = upstream_dash_roles(font)
        localized_mappings = (
            {}
            if region == "CL"
            else normalized_source_han_dash_locl_mappings(font, roles)
        )

    _obsolete_glyphs, cleanup_report = strip_legacy_dash_layout(font, roles)
    report.update(cleanup_report)
    if region == "CL":
        cmap_remaps = 0
        for codepoint, glyph_name in (
            (0x2014, roles["fullwidth"]),
            (0x2E3A, roles["fullwidth_two"]),
            (0x2E3B, roles["fullwidth_three"]),
        ):
            cmap_remaps += remap_unicode_cmap(font, codepoint, glyph_name)
        report["upstream_dash_static_cmap_remaps"] = cmap_remaps
        roles["proportional"] = roles["fullwidth"]
        roles["encoded_two"] = roles["fullwidth_two"]
        roles["encoded_three"] = roles["fullwidth_three"]
        localized_mappings = {}
    else:
        missing_languages = CJK_LOCL_LANGUAGES - set(localized_mappings)
        if missing_languages:
            raise ValueError(
                "missing upstream dash locl mappings for: "
                + ", ".join(sorted(missing_languages))
            )

    ccmp_rules: dict[str, list[tuple[list[str], str]]] = {}
    ccmp_rules[roles["proportional"]] = [
        ([roles["proportional"], roles["proportional"]], roles["encoded_three"]),
        ([roles["proportional"]], roles["encoded_two"]),
    ]
    if roles["fullwidth"] != roles["proportional"]:
        ccmp_rules[roles["fullwidth"]] = [
            ([roles["fullwidth"], roles["fullwidth"]], roles["fullwidth_three"]),
            ([roles["fullwidth"]], roles["fullwidth_two"]),
        ]
    ccmp_lookup = append_gsub_lookup(
        font,
        make_ligature_substitution_lookup(ccmp_rules),
    )
    report["upstream_dash_ccmp_feature_records_linked"] = link_lookup_to_all_gsub_features(
        font,
        "ccmp",
        ccmp_lookup,
    )

    report.update(
        add_language_specific_locl(
            font,
            localized_mappings,
            report_prefix="upstream_dash",
            purpose="dash",
        )
    )
    vertical_mapping = {
        roles["fullwidth"]: roles["vertical_single"],
        roles["fullwidth_two"]: roles["vertical_two"],
        roles["fullwidth_three"]: roles["vertical_three"],
    }
    vertical_lookup = append_gsub_lookup(
        font,
        make_single_substitution_lookup(vertical_mapping),
    )
    for tag in ("vert", "vrt2"):
        report[f"upstream_dash_{tag}_feature_records_linked"] = (
            link_lookup_to_all_gsub_features(font, tag, vertical_lookup)
        )
    report.update(apply_cjk_ellipsis_behavior(font))
    report.update(apply_default_regional_punctuation(font, region))

    gdef = font["GDEF"].table if "GDEF" in font else None
    class_defs = getattr(getattr(gdef, "GlyphClassDef", None), "classDefs", None)
    if class_defs is not None:
        for glyph_name in {
            roles["encoded_two"],
            roles["encoded_three"],
            roles["fullwidth_two"],
            roles["fullwidth_three"],
            roles["vertical_two"],
            roles["vertical_three"],
        }:
            class_defs[glyph_name] = 2
    report["upstream_dash_catalan_locl_records_cleared"] = clear_empty_catalan_locl(font)
    report["upstream_dash_behavior_applied"] = True
    report["upstream_dash_mechanism"] = "Source Han ccmp/locl/vert-vrt2"
    report["upstream_dash_weight_dependent_outlines"] = True
    return report


def sync_em_dash_continuation_metrics_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    target_glyphs = em_dash_continuation_glyphs(font)
    reference_glyphs = em_dash_continuation_glyphs(reference)
    hmtx_synced = 0
    vmtx_synced = 0
    for target_glyph, reference_glyph in zip(target_glyphs, reference_glyphs):
        if target_glyph in font["hmtx"].metrics and reference_glyph in reference["hmtx"].metrics:
            if font["hmtx"].metrics[target_glyph] != reference["hmtx"].metrics[reference_glyph]:
                hmtx_synced += 1
            font["hmtx"].metrics[target_glyph] = copy.deepcopy(reference["hmtx"].metrics[reference_glyph])
        if (
            "vmtx" in font
            and "vmtx" in reference
            and target_glyph in font["vmtx"].metrics
            and reference_glyph in reference["vmtx"].metrics
        ):
            if font["vmtx"].metrics[target_glyph] != reference["vmtx"].metrics[reference_glyph]:
                vmtx_synced += 1
            font["vmtx"].metrics[target_glyph] = copy.deepcopy(reference["vmtx"].metrics[reference_glyph])
    return {
        "em_dash_continuation_hmtx_synced": hmtx_synced,
        "em_dash_continuation_vmtx_synced": vmtx_synced,
    }


def pair_position_record(
    font: TTFont,
    lookup_index: int,
    first: str,
    second: str,
) -> tuple[Any, Any] | None:
    if "GPOS" not in font or not font["GPOS"].table.LookupList:
        return None
    lookups = font["GPOS"].table.LookupList.Lookup
    if not (0 <= lookup_index < len(lookups)):
        return None
    lookup = lookups[lookup_index]
    if lookup.LookupType != 2:
        return None
    for subtable in lookup.SubTable:
        if getattr(subtable, "Format", None) != 1 or not getattr(subtable, "Coverage", None):
            continue
        glyphs = list(subtable.Coverage.glyphs or [])
        if first not in glyphs:
            continue
        pair_sets = list(getattr(subtable, "PairSet", []) or [])
        coverage_index = glyphs.index(first)
        if coverage_index >= len(pair_sets):
            continue
        for record in list(pair_sets[coverage_index].PairValueRecord or []):
            if record.SecondGlyph != second:
                continue
            return subtable, record
    return None


def pair_position_placement(
    font: TTFont,
    lookup_index: int,
    first: str,
    second: str,
    location: dict[str, float] | None = None,
) -> tuple[int, int] | None:
    found = pair_position_record(font, lookup_index, first, second)
    if not found:
        return None
    _subtable, record = found
    value = getattr(record, "Value2", None)
    x_placement = int(getattr(value, "XPlacement", 0) or 0)
    y_placement = int(getattr(value, "YPlacement", 0) or 0)
    if location and "fvar" in font and "GDEF" in font:
        var_store = getattr(font["GDEF"].table, "VarStore", None)
        if var_store:
            normalized_location: dict[str, float] = {}
            avar_segments = font["avar"].segments if "avar" in font else {}
            for axis in font["fvar"].axes:
                value_at_location = float(location.get(axis.axisTag, axis.defaultValue))
                normalized = normalizeValue(
                    value_at_location,
                    (axis.minValue, axis.defaultValue, axis.maxValue),
                )
                mapping = avar_segments.get(axis.axisTag, {})
                if mapping:
                    normalized = piecewiseLinearMap(normalized, mapping)
                normalized_location[axis.axisTag] = normalized
            instancer = VarStoreInstancer(
                var_store,
                font["fvar"].axes,
                normalized_location,
            )
            for attribute, base_value in (
                ("XPlaDevice", x_placement),
                ("YPlaDevice", y_placement),
            ):
                device = getattr(value, attribute, None)
                if not device or getattr(device, "DeltaFormat", None) != 0x8000:
                    continue
                var_idx = (int(device.StartSize) << 16) | int(device.EndSize)
                delta = otRound(instancer[var_idx])
                if attribute == "XPlaDevice":
                    x_placement = base_value + delta
                else:
                    y_placement = base_value + delta
    return x_placement, y_placement


def set_pair_position_placement(
    font: TTFont,
    lookup_index: int,
    first: str,
    second: str,
    x_placement: int,
    y_placement: int,
    x_device: Any | None = None,
    y_device: Any | None = None,
) -> bool:
    found = pair_position_record(font, lookup_index, first, second)
    if not found:
        return False
    subtable, record = found
    device_format = (0x0010 if x_device is not None else 0) | (0x0020 if y_device is not None else 0)
    subtable.ValueFormat2 = (
        int(getattr(subtable, "ValueFormat2", 0) or 0) & ~0x0030
    ) | 0x0003 | device_format
    if getattr(record, "Value2", None) is None:
        record.Value2 = ot.ValueRecord()
    record.Value2.XPlacement = int(x_placement)
    record.Value2.YPlacement = int(y_placement)
    record.Value2.XPlaDevice = x_device
    record.Value2.YPlaDevice = y_device
    return True


def glyph_set_points_and_vertical_advance(
    font: TTFont,
    glyph_name: str,
    location: dict[str, float] | None = None,
) -> tuple[list[tuple[float, float]], int]:
    glyph_set = font.getGlyphSet(location=location)
    if glyph_name not in glyph_set:
        return [], 0
    glyph = glyph_set[glyph_name]
    pen = RecordingPen()
    glyph.draw(pen)
    points: list[tuple[float, float]] = []
    for operation, arguments in pen.value:
        if operation not in {"moveTo", "lineTo", "curveTo", "qCurveTo"}:
            continue
        points.extend(
            (float(point[0]), float(point[1]))
            for point in arguments
            if point is not None and len(point) == 2
        )
    return points, int(getattr(glyph, "height", 0) or 0)


def vertical_em_dash_pair_placement_from_points(
    font: TTFont,
    points: list[tuple[float, float]],
    advance_height: int,
) -> tuple[int, int]:
    if not points or not advance_height:
        raise ValueError("missing vertical em dash geometry")
    x_min = min(x for x, _y in points)
    y_min = min(y for _x, y in points)
    x_max = max(x for x, _y in points)
    y_max = max(y for _x, y in points)
    top_x = [x for x, y in points if y == y_max]
    bottom_x = [x for x, y in points if y == y_min]
    if top_x and bottom_x and y_max != y_min:
        top_center = (min(top_x) + max(top_x)) / 2
        bottom_center = (min(bottom_x) + max(bottom_x)) / 2
        slope = (top_center - bottom_center) / (y_max - y_min)
        cap_width = (
            (max(top_x) - min(top_x)) + (max(bottom_x) - min(bottom_x))
        ) / 2
    else:
        italic_angle = float(font["post"].italicAngle) if "post" in font else 0.0
        slope = math.tan(math.radians(-italic_angle))
        cap_width = max(1.0, (x_max - x_min) - abs(slope * (y_max - y_min)))
    y_placement = max(
        1,
        otRound(y_min + advance_height + cap_width / 2 - y_max),
    )
    vertical_step = -advance_height + y_placement
    x_placement = otRound(slope * vertical_step)
    return x_placement, y_placement


def vertical_em_dash_pair_placement(
    font: TTFont,
    glyph_name: str,
    location: dict[str, float] | None = None,
) -> tuple[int, int]:
    points, advance_height = glyph_set_points_and_vertical_advance(
        font,
        glyph_name,
        location,
    )
    if not points or not advance_height:
        raise ValueError(f"missing vertical em dash geometry: {glyph_name}")
    return vertical_em_dash_pair_placement_from_points(
        font,
        points,
        advance_height,
    )


def collect_ot_variation_devices(obj: Any, seen: set[int] | None = None) -> list[Any]:
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (str, int, float, bool, bytes)):
        return []
    obj_id = id(obj)
    if obj_id in seen:
        return []
    seen.add(obj_id)
    if isinstance(obj, ot.Device) and getattr(obj, "DeltaFormat", None) == 0x8000:
        return [obj]
    values: list[Any]
    if isinstance(obj, dict):
        values = [*obj.keys(), *obj.values()]
    elif isinstance(obj, (list, tuple, set)):
        values = list(obj)
    elif hasattr(obj, "__dict__"):
        values = [value for key, value in vars(obj).items() if not key.startswith("_")]
    else:
        values = []
    return [device for value in values for device in collect_ot_variation_devices(value, seen)]


def append_item_variation_store(
    existing_store: Any,
    additional_store: Any,
    var_indices: list[int],
) -> tuple[Any, list[int]]:
    if (
        existing_store.VarRegionList.RegionAxisCount
        != additional_store.VarRegionList.RegionAxisCount
    ):
        raise ValueError("cannot merge GDEF VarStores with different axis counts")

    def region_key(region: Any) -> tuple[tuple[float, float, float], ...]:
        return tuple(
            (float(axis.StartCoord), float(axis.PeakCoord), float(axis.EndCoord))
            for axis in region.VarRegionAxis
        )

    merged = copy.deepcopy(existing_store)
    known_regions = {
        region_key(region): index
        for index, region in enumerate(merged.VarRegionList.Region)
    }
    region_index_map: dict[int, int] = {}
    for old_index, region in enumerate(additional_store.VarRegionList.Region):
        key = region_key(region)
        new_index = known_regions.get(key)
        if new_index is None:
            new_index = len(merged.VarRegionList.Region)
            merged.VarRegionList.Region.append(copy.deepcopy(region))
            known_regions[key] = new_index
        region_index_map[old_index] = new_index
    merged.VarRegionList.RegionCount = len(merged.VarRegionList.Region)

    major_offset = len(merged.VarData)
    for var_data in additional_store.VarData:
        cloned = copy.deepcopy(var_data)
        cloned.VarRegionIndex = [region_index_map[index] for index in cloned.VarRegionIndex]
        cloned.VarRegionCount = len(cloned.VarRegionIndex)
        merged.VarData.append(cloned)
    merged.VarDataCount = len(merged.VarData)

    remapped = []
    for var_idx in var_indices:
        if var_idx == 0xFFFFFFFF:
            remapped.append(var_idx)
        else:
            remapped.append((((var_idx >> 16) + major_offset) << 16) | (var_idx & 0xFFFF))
    return merged, remapped


def vertical_em_dash_variation_data(
    font: TTFont,
    glyph_name: str,
    existing_lookup_indices: set[int],
) -> dict[str, Any]:
    x_placement, y_placement = vertical_em_dash_pair_placement(font, glyph_name)
    if "fvar" not in font:
        return {
            "x_placement": x_placement,
            "y_placement": y_placement,
            "x_device": None,
            "y_device": None,
            "controls": {},
        }

    weights = list(EM_DASH_PROBE_WEIGHTS)
    normalized_locations = [
        {} if vf_mapped_normalized_weight(font, weight) == 0 else {
            "wght": vf_mapped_normalized_weight(font, weight)
        }
        for weight in weights
    ]
    expected_controls = {
        weight: vertical_em_dash_pair_placement(font, glyph_name, {"wght": weight})
        for weight in weights
    }
    controls = {weight: list(expected_controls[weight]) for weight in weights}

    def build_store() -> tuple[Any, int, int, int, int]:
        model = VariationModel(normalized_locations, axisOrder=["wght"])
        store_builder = OnlineVarStoreBuilder(["wght"])
        store_builder.setModel(model)
        base_x, x_var_idx = store_builder.storeMasters(
            [controls[weight][0] for weight in weights]
        )
        base_y, y_var_idx = store_builder.storeMasters(
            [controls[weight][1] for weight in weights]
        )
        store = store_builder.finish()
        for region in store.VarRegionList.Region:
            for axis in region.VarRegionAxis:
                for attribute in ("StartCoord", "PeakCoord", "EndCoord"):
                    setattr(axis, attribute, floatToFixedToFloat(getattr(axis, attribute), 14))
        variation_index_mapping = store.optimize()
        return (
            store,
            int(base_x),
            variation_index_mapping[x_var_idx],
            int(base_y),
            variation_index_mapping[y_var_idx],
        )

    calibration_rounds = 0
    while True:
        store, base_x, x_var_idx, base_y, y_var_idx = build_store()
        errors: dict[int, tuple[int, int]] = {}
        for weight, normalized_location in zip(weights, normalized_locations):
            instancer = VarStoreInstancer(
                store,
                font["fvar"].axes,
                normalized_location,
            )
            actual = (
                base_x + (0 if x_var_idx == 0xFFFFFFFF else otRound(instancer[x_var_idx])),
                base_y + (0 if y_var_idx == 0xFFFFFFFF else otRound(instancer[y_var_idx])),
            )
            expected = expected_controls[weight]
            if actual != expected:
                errors[weight] = (expected[0] - actual[0], expected[1] - actual[1])
        if not errors:
            break
        calibration_rounds += 1
        if calibration_rounds > 8:
            raise RuntimeError(
                f"vertical em dash PairPos calibration did not converge: {errors}"
            )
        for weight, correction in errors.items():
            controls[weight][0] += correction[0]
            controls[weight][1] += correction[1]

    dash_device_ids: set[int] = set()
    for lookup_index in existing_lookup_indices:
        found = pair_position_record(font, lookup_index, glyph_name, glyph_name)
        if not found:
            continue
        value = getattr(found[1], "Value2", None)
        for attribute in ("XPlaDevice", "YPlaDevice"):
            device = getattr(value, attribute, None)
            if device and getattr(device, "DeltaFormat", None) == 0x8000:
                dash_device_ids.add(id(device))
    all_devices = []
    for tag in ("GPOS", "GDEF"):
        if tag in font:
            all_devices.extend(collect_ot_variation_devices(font[tag].table))
    external_devices = [device for device in all_devices if id(device) not in dash_device_ids]
    existing_store = getattr(font["GDEF"].table, "VarStore", None) if "GDEF" in font else None
    if not existing_store and external_devices:
        raise ValueError(
            "GPOS/GDEF VariationIndex records exist without a GDEF VarStore; rebuild required"
        )
    if "GDEF" not in font:
        raise ValueError("variable em dash PairPos requires GDEF")
    if existing_store is not None:
        store, remapped_indices = append_item_variation_store(
            existing_store,
            store,
            [x_var_idx, y_var_idx],
        )
        x_var_idx, y_var_idx = remapped_indices
    gdef = font["GDEF"].table
    gdef.Version = max(int(gdef.Version), 0x00010003)
    gdef.VarStore = store
    return {
        "x_placement": int(base_x),
        "y_placement": int(base_y),
        "x_device": None if x_var_idx == 0xFFFFFFFF else buildVarDevTable(x_var_idx),
        "y_device": None if y_var_idx == 0xFFFFFFFF else buildVarDevTable(y_var_idx),
        "controls": {str(weight): list(expected_controls[weight]) for weight in weights},
        "calibration_rounds": calibration_rounds,
        "control_corrections": {
            str(weight): [
                controls[weight][index] - expected_controls[weight][index]
                for index in range(2)
            ]
            for weight in weights
            if tuple(controls[weight]) != expected_controls[weight]
        },
    }


def vertical_em_dash_positioning_records(
    font: TTFont,
    location: dict[str, float] | None = None,
) -> dict[str, list[dict[str, int]]]:
    result: dict[str, list[dict[str, int]]] = {"vert": [], "vrt2": []}
    if "GPOS" not in font or not font["GPOS"].table.FeatureList:
        return result
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    if not em_dash:
        return result
    vert_mapping = get_single_substitution_mapping(font, "vert")
    vrt2_mapping = get_single_substitution_mapping(font, "vrt2")
    em_dash_v = vert_mapping.get(em_dash) or vrt2_mapping.get(em_dash)
    if not em_dash_v:
        return result
    for feature_index, record in enumerate(font["GPOS"].table.FeatureList.FeatureRecord):
        if record.FeatureTag not in result:
            continue
        for lookup_index in list(record.Feature.LookupListIndex or []):
            placement = pair_position_placement(
                font,
                int(lookup_index),
                em_dash_v,
                em_dash_v,
                location,
            )
            if placement and placement[1]:
                result[record.FeatureTag].append(
                    {
                        "feature_index": feature_index,
                        "lookup_index": int(lookup_index),
                        "x_placement": placement[0],
                        "y_placement": placement[1],
                    }
                )
    return result


def add_vertical_em_dash_positioning(font: TTFont) -> dict[str, Any]:
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    continuations = em_dash_continuation_glyphs(font) if em_dash else []
    if not em_dash or not continuations or "GSUB" not in font or "GPOS" not in font:
        return {"vertical_em_dash_positioning_added": False}
    em_dash_cont = continuations[0]
    em_dash_starts = em_dash_pair_start_glyphs(font)
    vert_mapping = get_single_substitution_mapping(font, "vert")
    vrt2_mapping = get_single_substitution_mapping(font, "vrt2")
    em_dash_v = vert_mapping.get(em_dash) or vrt2_mapping.get(em_dash)
    if not em_dash_v:
        return {"vertical_em_dash_positioning_added": False}

    gsub = font["GSUB"].table
    collapsed_mappings = 0
    for record in gsub.FeatureList.FeatureRecord if gsub.FeatureList else []:
        if record.FeatureTag not in {"vert", "vrt2"}:
            continue
        for lookup_index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in single_substitution_subtables(lookup):
                if subtable.mapping.get(em_dash) == em_dash_v:
                    for alias in (em_dash_cont, *em_dash_starts):
                        if subtable.mapping.get(alias) != em_dash_v:
                            subtable.mapping[alias] = em_dash_v
                            collapsed_mappings += 1

    removed_vertical_calt_mappings = 0
    obsolete_vertical_continuations: set[str] = set()
    for lookup_index in em_dash_chain_lookup_indices(font):
        lookup = gsub.LookupList.Lookup[lookup_index]
        for subtable in lookup.SubTable:
            for subst_record in chain_substitution_records(subtable):
                referenced_lookup = gsub.LookupList.Lookup[subst_record.LookupListIndex]
                for single_subtable in single_substitution_subtables(referenced_lookup):
                    if single_subtable.mapping.get(em_dash) == em_dash_cont:
                        old_vertical_continuation = single_subtable.mapping.get(em_dash_v)
                        if old_vertical_continuation and old_vertical_continuation != em_dash_v:
                            obsolete_vertical_continuations.add(old_vertical_continuation)
                            del single_subtable.mapping[em_dash_v]
                            removed_vertical_calt_mappings += 1
    removed_vertical_coverage_glyphs = 0
    if obsolete_vertical_continuations:
        for lookup_index in em_dash_chain_lookup_indices(font):
            lookup = gsub.LookupList.Lookup[lookup_index]
            for subtable in lookup.SubTable:
                for coverage_table in [
                    *list(getattr(subtable, "BacktrackCoverage", []) or []),
                    *list(getattr(subtable, "InputCoverage", []) or []),
                    *list(getattr(subtable, "LookAheadCoverage", []) or []),
                ]:
                    before = list(getattr(coverage_table, "glyphs", []) or [])
                    after = [
                        glyph
                        for glyph in before
                        if glyph not in obsolete_vertical_continuations
                    ]
                    if after != before:
                        coverage_table.glyphs = after
                        removed_vertical_coverage_glyphs += len(before) - len(after)

    if not glyph_bbox(font, em_dash_v) or "vmtx" not in font:
        return {"vertical_em_dash_positioning_added": False}
    existing = vertical_em_dash_positioning_records(font)
    existing_lookup_indices = {
        item["lookup_index"]
        for records in existing.values()
        for item in records
    }
    variation_data = vertical_em_dash_variation_data(
        font,
        em_dash_v,
        existing_lookup_indices,
    )
    x_placement = int(variation_data["x_placement"])
    y_placement = int(variation_data["y_placement"])
    x_device = variation_data["x_device"]
    y_device = variation_data["y_device"]

    updated_existing_lookups = 0
    for lookup_index in sorted(existing_lookup_indices):
        if set_pair_position_placement(
            font,
            lookup_index,
            em_dash_v,
            em_dash_v,
            x_placement,
            y_placement,
            copy.deepcopy(x_device),
            copy.deepcopy(y_device),
        ):
            updated_existing_lookups += 1
    if updated_existing_lookups:
        existing = vertical_em_dash_positioning_records(font)
    gpos = font["GPOS"].table
    existing_feature_indices = {
        tag: [
            index
            for index, record in enumerate(gpos.FeatureList.FeatureRecord)
            if record.FeatureTag == tag
        ]
        for tag in ("vert", "vrt2")
    }
    missing_feature_indices = {
        tag: [
            index
            for index in existing_feature_indices[tag]
            if not any(
                item["feature_index"] == index
                and item["x_placement"] == x_placement
                and item["y_placement"] == y_placement
                for item in existing[tag]
            )
        ]
        for tag in ("vert", "vrt2")
    }
    missing_tags = {
        tag for tag in ("vert", "vrt2") if not existing_feature_indices[tag]
    }
    needs_lookup = bool(missing_tags) or any(missing_feature_indices.values())
    lookup_index: int | None = next(
        (
            item["lookup_index"]
            for records in existing.values()
            for item in records
            if item["x_placement"] == x_placement
            and item["y_placement"] == y_placement
        ),
        None,
    )
    feature_indices: dict[str, list[int]] = {"vert": [], "vrt2": []}
    if needs_lookup and lookup_index is None:
        subtable = ot.PairPos()
        subtable.Format = 1
        subtable.Coverage = coverage(font, [em_dash_v])
        subtable.ValueFormat1 = 0
        subtable.ValueFormat2 = (
            0x0003
            | (0x0010 if x_device is not None else 0)
            | (0x0020 if y_device is not None else 0)
        )
        pair_value = ot.PairValueRecord()
        pair_value.SecondGlyph = em_dash_v
        pair_value.Value1 = None
        pair_value.Value2 = ot.ValueRecord()
        pair_value.Value2.XPlacement = x_placement
        pair_value.Value2.YPlacement = y_placement
        pair_value.Value2.XPlaDevice = copy.deepcopy(x_device)
        pair_value.Value2.YPlaDevice = copy.deepcopy(y_device)
        pair_set = ot.PairSet()
        pair_set.PairValueRecord = [pair_value]
        pair_set.PairValueCount = 1
        subtable.PairSet = [pair_set]
        subtable.PairSetCount = 1
        lookup = ot.Lookup()
        lookup.LookupType = 2
        lookup.LookupFlag = 0
        lookup.SubTable = [subtable]
        lookup.SubTableCount = 1
        lookup.MarkFilteringSet = None
        lookup_index = append_gpos_lookup(font, lookup)
    if lookup_index is not None:
        for tag in ("vert", "vrt2"):
            for feature_index in missing_feature_indices[tag]:
                feature = gpos.FeatureList.FeatureRecord[feature_index].Feature
                indices = list(feature.LookupListIndex or [])
                if lookup_index not in indices:
                    indices.append(lookup_index)
                    feature.LookupListIndex = indices
                    feature.LookupCount = len(indices)
                    feature_indices[tag].append(feature_index)
            if tag in missing_tags:
                feature_indices[tag].append(insert_gpos_feature(font, tag, [lookup_index]))

    removed_obsolete_glyphs = remove_generated_glyphs(font, obsolete_vertical_continuations)
    return {
        "vertical_em_dash_positioning_added": needs_lookup,
        "vertical_em_dash_positioning_x": x_placement,
        "vertical_em_dash_positioning_y": y_placement,
        "vertical_em_dash_positioning_variations": variation_data["controls"],
        "vertical_em_dash_positioning_calibration_rounds": variation_data.get(
            "calibration_rounds",
            0,
        ),
        "vertical_em_dash_positioning_control_corrections": variation_data.get(
            "control_corrections",
            {},
        ),
        "vertical_em_dash_positioning_updated_lookups": updated_existing_lookups,
        "vertical_em_dash_positioning_lookup": lookup_index,
        "vertical_em_dash_positioning_features": feature_indices,
        "vertical_em_dash_collapsed_mappings": collapsed_mappings,
        "vertical_em_dash_calt_mappings_removed": removed_vertical_calt_mappings,
        "vertical_em_dash_obsolete_coverage_glyphs_removed": removed_vertical_coverage_glyphs,
        "vertical_em_dash_obsolete_glyphs": sorted(obsolete_vertical_continuations),
        "vertical_em_dash_obsolete_glyphs_removed": removed_obsolete_glyphs,
    }


def apply_static_two_em_dash_behavior(font: TTFont) -> dict[str, Any]:
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    if not em_dash or "GSUB" not in font or "glyf" not in font:
        return {"static_two_em_dash_applied": False}
    continuations = em_dash_continuation_glyphs(font)
    if not continuations:
        return {"static_two_em_dash_applied": False}
    em_dash_cont = continuations[0]

    upem = int(font["head"].unitsPerEm)
    base_advance = int(font["hmtx"].metrics[em_dash][0])
    old_cont_advance, old_cont_lsb = font["hmtx"].metrics[em_dash_cont]
    font["hmtx"].metrics[em_dash_cont] = (2 * upem - base_advance, old_cont_lsb)

    return {
        "static_two_em_dash_applied": True,
        "static_two_em_dash_base_advance": base_advance,
        "static_two_em_dash_old_continuation_advance": int(old_cont_advance),
        "static_two_em_dash_continuation_advance": int(font["hmtx"].metrics[em_dash_cont][0]),
    }


def legacy_vertical_em_dash_continuation_glyphs(font: TTFont, em_dash_v: str) -> list[str]:
    if "glyf" not in font or "vmtx" not in font:
        return []
    box = glyph_bbox(font, em_dash_v)
    if not box:
        return []
    x_min, y_min, x_max, _y_max = box
    advance_height = int(font["vmtx"].metrics[em_dash_v][0])
    half_width = (x_max - x_min) / 2
    expected_points = [
        (otRound(x_min), otRound(y_min)),
        (otRound(x_max), otRound(y_min)),
        (otRound(x_max), otRound(y_min + advance_height)),
        (otRound((x_min + x_max) / 2), otRound(y_min + advance_height + half_width)),
        (otRound(x_min), otRound(y_min + advance_height)),
    ]
    expected_box = (
        min(x for x, _y in expected_points),
        min(y for _x, y in expected_points),
        max(x for x, _y in expected_points),
        max(y for _x, y in expected_points),
    )
    encoded = set((font.getBestCmap() or {}).values())
    excluded = encoded | {em_dash_v} | set(em_dash_continuation_glyphs(font))
    matches: list[str] = []
    for glyph_name in font.getGlyphOrder():
        if glyph_name in excluded:
            continue
        glyph = font["glyf"][glyph_name]
        if glyph.isComposite() or getattr(glyph, "numberOfContours", 0) != 1:
            continue
        glyph_box = tuple(
            int(getattr(glyph, attribute, 0))
            for attribute in ("xMin", "yMin", "xMax", "yMax")
        )
        if glyph_box != expected_box:
            continue
        if simple_glyph_coordinates(font, glyph_name) == expected_points:
            matches.append(glyph_name)
    return matches


def variable_em_dash_ligature_structure_status(font: TTFont) -> dict[str, Any]:
    reasons: list[str] = []
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    if not em_dash or "GSUB" not in font:
        return {"ok": False, "reasons": ["missing U+2014 or GSUB"]}
    gsub = font["GSUB"].table
    ccmp_indices = {
        int(index)
        for record in (gsub.FeatureList.FeatureRecord if gsub.FeatureList else [])
        if record.FeatureTag == "ccmp"
        for index in record.Feature.LookupListIndex or []
    }
    pair_rules: set[tuple[int, str]] = set()
    triple_rules: set[tuple[int, str]] = set()
    for lookup_index in ccmp_indices:
        lookup = gsub.LookupList.Lookup[lookup_index]
        for subtable in ligature_substitution_subtables(lookup):
            for ligature in subtable.ligatures.get(em_dash, []) or []:
                components = list(ligature.Component or [])
                if components == [em_dash]:
                    pair_rules.add((lookup_index, ligature.LigGlyph))
                elif components == [em_dash, em_dash]:
                    triple_rules.add((lookup_index, ligature.LigGlyph))
    pair_targets = {target for _index, target in pair_rules}
    if len(pair_targets) != 1:
        reasons.append(f"expected one ccmp em dash pair target, found {sorted(pair_targets)}")
        horizontal_ligature = None
    else:
        horizontal_ligature = next(iter(pair_targets))

    expected_triple = cmap.get(0x2E3B)
    triple_targets = {target for _index, target in triple_rules}
    if expected_triple and triple_targets != {expected_triple}:
        reasons.append(
            f"ccmp em dash triple target {sorted(triple_targets)} != {expected_triple}"
        )

    expected_two_em = cmap.get(0x2E3A)
    ccmp_mapping = get_single_substitution_mapping(font, "ccmp")
    if expected_two_em and ccmp_mapping.get(expected_two_em) != horizontal_ligature:
        reasons.append("ccmp does not send U+2E3A through the two-em ligature")

    vert_mapping = get_single_substitution_mapping(font, "vert")
    vrt2_mapping = get_single_substitution_mapping(font, "vrt2")
    vertical_ligature = (
        vert_mapping.get(horizontal_ligature) if horizontal_ligature else None
    )
    if not vertical_ligature:
        reasons.append("vert does not map the horizontal two-em ligature")
    elif vrt2_mapping.get(horizontal_ligature) != vertical_ligature:
        reasons.append("vrt2 does not share the Source Han long-dash mapping")
    vertical_three_em_ligature = (
        vert_mapping.get(expected_triple) if expected_triple else None
    )
    if not vertical_three_em_ligature:
        reasons.append("vert does not map the horizontal three-em ligature")
    elif vrt2_mapping.get(expected_triple) != vertical_three_em_ligature:
        reasons.append("vrt2 does not share the Source Han three-em mapping")

    upem = int(font["head"].unitsPerEm)
    if horizontal_ligature and int(font["hmtx"].metrics[horizontal_ligature][0]) != 2 * upem:
        reasons.append("horizontal em dash ligature advance is not 2em")
    if (
        vertical_ligature
        and int(font["vmtx"].metrics[vertical_ligature][0]) != 2 * upem
    ):
        reasons.append("vertical em dash ligature advance is not 2em")
    if expected_triple and int(font["hmtx"].metrics[expected_triple][0]) != 3 * upem:
        reasons.append("horizontal three-em dash advance is not 3em")
    if (
        vertical_three_em_ligature
        and int(font["vmtx"].metrics[vertical_three_em_ligature][0]) != 3 * upem
    ):
        reasons.append("vertical three-em dash ligature advance is not 3em")

    continuations = em_dash_continuation_glyphs(font)
    pair_starts = em_dash_pair_start_glyphs(font)
    if continuations or pair_starts:
        reasons.append(
            "obsolete variable calt pair states remain: "
            + repr({"continuations": continuations, "pair_starts": pair_starts})
        )
    pair_positioning = vertical_em_dash_positioning_records(font)
    if any(pair_positioning.values()):
        reasons.append("obsolete variable vertical em dash PairPos remains")

    gdef_classes = (
        font["GDEF"].table.GlyphClassDef.classDefs
        if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None)
        else {}
    )
    for glyph_name in (
        horizontal_ligature,
        vertical_ligature,
        vertical_three_em_ligature,
    ):
        if glyph_name and gdef_classes.get(glyph_name) != 2:
            reasons.append(f"{glyph_name} is not classified as a GDEF ligature")

    topology = {
        horizontal_ligature: (2, 9),
        vertical_ligature: (2, 8),
        vertical_three_em_ligature: (3, 12),
    }
    for glyph_name, (expected_contours, expected_points) in topology.items():
        if not glyph_name or "glyf" not in font:
            continue
        glyph = font["glyf"][glyph_name]
        if glyph.isComposite():
            reasons.append(f"{glyph_name} is unexpectedly composite")
            continue
        coordinates, _end_points, flags = glyph.getCoordinates(font["glyf"])
        if int(glyph.numberOfContours) != expected_contours or len(coordinates) != expected_points:
            reasons.append(
                f"{glyph_name} topology is {glyph.numberOfContours}/{len(coordinates)}, "
                f"expected {expected_contours}/{expected_points}"
            )
        if not len(flags) or not (int(flags[0]) & 0x40):
            reasons.append(f"{glyph_name} lacks OVERLAP_SIMPLE on its first contour")
        if len(font["gvar"].variations.get(glyph_name, [])) != len(EM_DASH_PROBE_WEIGHTS) - 1:
            reasons.append(f"{glyph_name} does not have every em-dash gvar control")

    if pair_rules and min(index for index, _target in pair_rules) != 0:
        reasons.append("em dash ccmp ligature lookup is not first in GSUB")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "mechanism": "Source Han ccmp plus vert",
        "pair_rules": sorted([index, target] for index, target in pair_rules),
        "triple_rules": sorted([index, target] for index, target in triple_rules),
        "horizontal_ligature": horizontal_ligature,
        "vertical_ligature": vertical_ligature,
        "vertical_three_em_ligature": vertical_three_em_ligature,
        "encoded_two_em": expected_two_em,
        "encoded_three_em": expected_triple,
        "obsolete_continuations": continuations,
        "obsolete_pair_starts": pair_starts,
        "obsolete_pair_positioning": pair_positioning,
    }


def two_em_dash_structure_status(font: TTFont, variable: bool) -> dict[str, Any]:
    if variable:
        return variable_em_dash_ligature_structure_status(font)
    reasons: list[str] = []
    cmap = font.getBestCmap() or {}
    em_dash = cmap.get(0x2014)
    continuations = em_dash_continuation_glyphs(font) if em_dash else []
    if not em_dash or not continuations:
        return {"ok": False, "reasons": ["missing horizontal em dash continuation"]}
    em_dash_cont = continuations[0]
    em_dash_starts = em_dash_pair_start_glyphs(font)
    if variable and len(em_dash_starts) != 1:
        reasons.append(
            "variable em dash pair requires exactly one fixed-advance start alias: "
            + repr(em_dash_starts)
        )
    em_dash_start = em_dash_starts[0] if len(em_dash_starts) == 1 else em_dash
    vert_mapping = get_single_substitution_mapping(font, "vert")
    vrt2_mapping = get_single_substitution_mapping(font, "vrt2")
    em_dash_v = vert_mapping.get(em_dash) or vrt2_mapping.get(em_dash)
    if not em_dash_v:
        reasons.append("missing vertical em dash mapping")
    legacy_vertical_continuations = (
        legacy_vertical_em_dash_continuation_glyphs(font, em_dash_v)
        if em_dash_v
        else []
    )
    if legacy_vertical_continuations:
        reasons.append(
            "obsolete unencoded vertical em dash continuation glyphs remain: "
            + ", ".join(legacy_vertical_continuations)
        )
    for tag, mapping in (("vert", vert_mapping), ("vrt2", vrt2_mapping)):
        mapped_base = mapping.get(em_dash)
        aliases = [em_dash_cont, *em_dash_starts]
        if mapped_base and any(mapping.get(alias) != mapped_base for alias in aliases):
            reasons.append(f"{tag} does not collapse all em dash states to one vertical glyph")

    positioning = vertical_em_dash_positioning_records(font)
    expected_pair_placement = (
        vertical_em_dash_pair_placement(font, em_dash_v)
        if em_dash_v
        else None
    )
    pair_placements: dict[str, tuple[int, int] | None] = {
        tag: (
            (records[0]["x_placement"], records[0]["y_placement"])
            if records
            else None
        )
        for tag, records in positioning.items()
    }
    for tag, placement in pair_placements.items():
        if not placement or placement[1] <= 0:
            reasons.append(f"missing positive {tag} em dash pair positioning")
        elif placement != expected_pair_placement or any(
            (record["x_placement"], record["y_placement"]) != expected_pair_placement
            for record in positioning[tag]
        ):
            reasons.append(
                f"incorrect {tag} em dash pair positioning: {positioning[tag]}, "
                f"expected {expected_pair_placement}"
            )

    weights = (
        [int(stop["value"]) for stop in SOURCE_HAN_WEIGHT_STOPS]
        if variable
        else [None]
    )
    width_samples = {}
    for weight in weights:
        instance = (
            instantiateVariableFont(font, {"wght": weight}, inplace=False, optimize=True)
            if weight is not None
            else font
        )
        try:
            horizontal = (
                int(instance["hmtx"].metrics[em_dash_start][0])
                + int(instance["hmtx"].metrics[em_dash_cont][0])
            )
            pair_start_outline_matches = (
                simple_glyph_coordinates(instance, em_dash_start)
                == simple_glyph_coordinates(instance, em_dash)
                and glyph_bbox(instance, em_dash_start)
                == glyph_bbox(instance, em_dash)
            )
            if variable and not pair_start_outline_matches:
                reasons.append(
                    f"em dash pair start outline differs from U+2014 at {weight}"
                )
            vertical = (
                2 * int(instance["vmtx"].metrics[em_dash_v][0])
                if em_dash_v and "vmtx" in instance
                else None
            )
            key = "static" if weight is None else str(weight)
            location = {"wght": weight} if weight is not None else None
            positioning_at_location = vertical_em_dash_positioning_records(font, location)
            pair_placements_at_location: dict[str, tuple[int, int] | None] = {
                tag: (
                    (records[0]["x_placement"], records[0]["y_placement"])
                    if records
                    else None
                )
                for tag, records in positioning_at_location.items()
            }
            expected_at_location = (
                vertical_em_dash_pair_placement(font, em_dash_v, location)
                if em_dash_v
                else None
            )
            for tag, placement in pair_placements_at_location.items():
                if placement != expected_at_location or any(
                    (record["x_placement"], record["y_placement"])
                    != expected_at_location
                    for record in positioning_at_location[tag]
                ):
                    reasons.append(
                        f"incorrect {tag} em dash pair positioning at {key}: "
                        f"{positioning_at_location[tag]}, expected {expected_at_location}"
                    )
            seam_gaps = {}
            if em_dash_v:
                box = glyph_bbox(instance, em_dash_v)
                if box:
                    _x_min, y_min, _x_max, y_max = box
                    advance_height = int(instance["vmtx"].metrics[em_dash_v][0])
                    for tag, placement in pair_placements_at_location.items():
                        seam_gaps[tag] = (
                            y_min - (y_max - advance_height + int(placement[1]))
                            if placement is not None
                            else None
                        )
                        if seam_gaps[tag] is None or seam_gaps[tag] > 0:
                            reasons.append(
                                f"vertical em dash pair at {key}/{tag} has seam gap "
                                f"{seam_gaps[tag]!r}"
                            )
            width_samples[key] = {
                "horizontal": horizontal,
                "vertical": vertical,
                "pair_start_outline_matches": pair_start_outline_matches,
                "vertical_seam_gaps": seam_gaps,
                "pair_placements": pair_placements_at_location,
            }
            expected = 2 * int(instance["head"].unitsPerEm)
            if horizontal != expected:
                reasons.append(f"horizontal em dash pair at {key} is {horizontal}, expected {expected}")
            if vertical != expected:
                reasons.append(f"vertical em dash pair at {key} is {vertical}, expected {expected}")
        finally:
            if weight is not None:
                instance.close()
    return {
        "ok": not reasons,
        "reasons": reasons,
        "widths": width_samples,
        "pair_start_glyphs": em_dash_starts,
        "legacy_vertical_continuations": legacy_vertical_continuations,
    }


def upstream_dash_structure_status(font: TTFont, region: str) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        roles = upstream_dash_roles(font)
    except Exception as error:
        return {
            "ok": False,
            "reasons": [f"could not resolve upstream dash roles: {error}"],
        }

    pair_rules = ligature_outputs_for_feature(
        font,
        "ccmp",
        roles["proportional"],
    )
    if pair_rules.get(2) != roles["encoded_two"]:
        reasons.append("ccmp pair does not produce encoded two-em dash")
    if pair_rules.get(3) != roles["encoded_three"]:
        reasons.append("ccmp triple does not produce encoded three-em dash")
    if roles["fullwidth"] != roles["proportional"]:
        fullwidth_rules = ligature_outputs_for_feature(
            font,
            "ccmp",
            roles["fullwidth"],
        )
        if fullwidth_rules.get(2) != roles["fullwidth_two"]:
            reasons.append("ccmp fullwidth pair does not produce hidden two-em dash")
        if fullwidth_rules.get(3) != roles["fullwidth_three"]:
            reasons.append("ccmp fullwidth triple does not produce hidden three-em dash")

    actual_locl = dash_locl_mappings_by_language(font, roles)
    if region == "CL":
        if roles["proportional"] != roles["fullwidth"]:
            reasons.append("CL U+2014 and U+2015 do not share the Shanggu glyph")
        if actual_locl:
            reasons.append(f"CL unexpectedly localizes dash through locl: {actual_locl!r}")
    else:
        korean_target = None
        for language in sorted(CJK_LOCL_LANGUAGES):
            mapping = actual_locl.get(language, {})
            expected_single = roles["fullwidth"]
            if language == "KOR ":
                expected_single = mapping.get(roles["proportional"])
                korean_target = expected_single
                if not expected_single or expected_single in {
                    roles["proportional"],
                    roles["fullwidth"],
                }:
                    reasons.append("KOR locl lacks Source Han's narrow single-dash alternate")
            expected = {
                roles["proportional"]: expected_single,
                roles["encoded_two"]: roles["fullwidth_two"],
                roles["encoded_three"]: roles["fullwidth_three"],
            }
            if mapping != expected:
                reasons.append(
                    f"{language} dash locl mapping {mapping!r} != {expected!r}"
                )

    vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    expected_vertical = {
        roles["fullwidth"]: roles["vertical_single"],
        roles["fullwidth_two"]: roles["vertical_two"],
        roles["fullwidth_three"]: roles["vertical_three"],
    }
    for source_name, target_name in expected_vertical.items():
        if vertical.get(source_name) != target_name:
            reasons.append(
                f"vert/vrt2 mapping {source_name}->{vertical.get(source_name)!r}, "
                f"expected {target_name}"
            )

    upem = int(font["head"].unitsPerEm)
    expected_metrics = {
        roles["fullwidth_two"]: ("hmtx", 2 * upem),
        roles["fullwidth_three"]: ("hmtx", 3 * upem),
        roles["vertical_two"]: ("vmtx", 2 * upem),
        roles["vertical_three"]: ("vmtx", 3 * upem),
    }
    for glyph_name, (table_tag, expected_advance) in expected_metrics.items():
        actual = int(font[table_tag].metrics[glyph_name][0])
        if actual != expected_advance:
            reasons.append(
                f"{glyph_name} {table_tag} advance {actual} != {expected_advance}"
            )

    obsolete_states = {
        "continuations": em_dash_continuation_glyphs(font),
        "pair_starts": em_dash_pair_start_glyphs(font),
    }
    if any(obsolete_states.values()):
        reasons.append(f"legacy calt dash states remain: {obsolete_states!r}")
    obsolete_positioning = vertical_em_dash_positioning_records(font)
    if any(obsolete_positioning.values()):
        reasons.append("legacy vertical dash PairPos remains")

    catalan_nonempty = 0
    if "GSUB" in font and font["GSUB"].table.FeatureList:
        gsub = font["GSUB"].table
        for langsys in langsys_records_for_language(font, "CAT "):
            for feature_index in list(langsys.FeatureIndex or []):
                record = gsub.FeatureList.FeatureRecord[feature_index]
                if record.FeatureTag == "locl" and record.Feature.LookupListIndex:
                    catalan_nonempty += 1
    if catalan_nonempty:
        reasons.append(f"CAT has {catalan_nonempty} non-empty locl FeatureRecords")

    gdef_classes = (
        font["GDEF"].table.GlyphClassDef.classDefs
        if "GDEF" in font and getattr(font["GDEF"].table, "GlyphClassDef", None)
        else {}
    )
    for glyph_name in {
        roles["encoded_two"],
        roles["encoded_three"],
        roles["fullwidth_two"],
        roles["fullwidth_three"],
        roles["vertical_two"],
        roles["vertical_three"],
    }:
        if gdef_classes.get(glyph_name) != 2:
            reasons.append(f"{glyph_name} is not a GDEF ligature")
    ellipsis_status = cjk_ellipsis_structure_status(font)
    reasons.extend(
        f"ellipsis: {reason}" for reason in ellipsis_status.get("reasons", [])
    )
    return {
        "ok": not reasons,
        "reasons": reasons,
        "mechanism": "Source Han ccmp/locl/vert-vrt2",
        "roles": roles,
        "locl": actual_locl,
        "legacy_states": obsolete_states,
        "legacy_pair_positioning": obsolete_positioning,
        "catalan_nonempty_locl_records": catalan_nonempty,
        "ellipsis": ellipsis_status,
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
    font["GSUB"].ensureDecompiled()
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
        seen_features = set()
        for feature_record in gsub.FeatureList.FeatureRecord:
            if id(feature_record.Feature) in seen_features:
                continue
            seen_features.add(id(feature_record.Feature))
            indices = [
                index_map[index]
                for index in list(feature_record.Feature.LookupListIndex or [])
                if index in index_map
            ]
            feature_record.Feature.LookupListIndex = indices
            feature_record.Feature.LookupCount = len(indices)

    mapped_records = {}
    def remap_records(container: Any) -> None:
        records = list(getattr(container, "SubstLookupRecord", []) or [])
        if records:
            kept_records = []
            for record in records:
                if id(record) not in mapped_records:
                    mapped_records[id(record)] = index_map.get(record.LookupListIndex)
                mapped = mapped_records[id(record)]
                if mapped is None:
                    continue
                record.LookupListIndex = mapped
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


@functools.lru_cache(maxsize=2)
def inter_context_reference(italic: bool) -> TTFont:
    ensure_inter_sources()
    return load_inter(italic)


def inter_layout_glyph_map(font: TTFont, source: TTFont) -> dict[str, str]:
    """Match layout identities through cmap and feature edges, including post 3."""
    names = set(font.getGlyphOrder())
    result = {name: prefixed(name) for name in source.getGlyphOrder() if prefixed(name) in names}
    cmap = font.getBestCmap()
    for cp, name in source.getBestCmap().items():
        if cp in cmap:
            result[name] = cmap[cp]
    tags = {record.FeatureTag for record in source["GSUB"].table.FeatureList.FeatureRecord}
    edges = [(get_single_substitution_mapping(source, tag), get_single_substitution_mapping(font, tag)) for tag in sorted(tags)]
    for _iteration in range(16):
        before = len(result)
        for source_mapping, target_mapping in edges:
            reverse = {target: original for original, target in target_mapping.items()}
            for original, target in source_mapping.items():
                if original in result and result[original] in target_mapping:
                    result.setdefault(target, target_mapping[result[original]])
                if target in result and result[target] in reverse:
                    result.setdefault(original, reverse[result[target]])
        if len(result) == before:
            return result
    raise RuntimeError("Inter 布局身份映射未收敛")


def move_gsub_lookups_before(font: TTFont, moved: list[int], before: int) -> dict[int, int]:
    font["GSUB"].ensureDecompiled()
    gsub = font["GSUB"].table
    order = [index for index in range(len(gsub.LookupList.Lookup)) if index not in moved]
    insertion = order.index(before)
    order[insertion:insertion] = moved
    mapping = {old: new for new, old in enumerate(order)}
    seen_features = set()
    for record in gsub.FeatureList.FeatureRecord:
        if id(record.Feature) in seen_features:
            continue
        seen_features.add(id(record.Feature))
        record.Feature.LookupListIndex = [mapping[index] for index in record.Feature.LookupListIndex]
    for record in layout_lookup_records(gsub.LookupList):
        record.LookupListIndex = mapping[record.LookupListIndex]
    gsub.LookupList.Lookup = [gsub.LookupList.Lookup[index] for index in order]
    return mapping


def add_digit_colon_feature(font: TTFont) -> dict[str, Any]:
    font["GSUB"].ensureDecompiled()
    cmap = font.getBestCmap()
    if 0x3A not in cmap:
        return {"digit_colon_feature_added": False}
    colon = cmap[0x3A]
    italic = bool(font["post"].italicAngle)
    source = inter_context_reference(italic)
    source_colon = source.getBestCmap()[0x3A]
    glyph_map = inter_layout_glyph_map(font, source)
    raised = get_single_substitution_mapping(font, "case").get(colon)
    existing_raised, removed = remove_existing_calt_colon_substitutions(font, colon)
    raised = raised or existing_raised
    if not raised:
        raise RuntimeError("缺少 Inter 原有的上浮冒号字形")
    glyph_map[get_single_substitution_mapping(source, "case")[source_colon]] = raised
    old_indices = digit_colon_calt_lookup_indices(font, colon)
    cleanup = remove_gsub_lookups(font, old_indices)
    # Keep the native feature records and language-system ordering. The new
    # colon lookup below also fills a record whose old lookup was removed.
    single = ot.Lookup(); single.LookupType = 1; single.LookupFlag = 0
    subst = ot.SingleSubst(); subst.mapping = {colon: raised}
    single.SubTable = [subst]; single.SubTableCount = 1
    single_index = append_gsub_lookup(font, single)
    rules = []
    for index in feature_lookup_indices(source, "GSUB", {"calt"}):
        lookup = source["GSUB"].table.LookupList.Lookup[index]
        if lookup.LookupType != 6:
            continue
        for subtable in lookup.SubTable:
            if getattr(subtable, "Format", None) != 3 or subtable.InputGlyphCount != 1 or source_colon not in subtable.InputCoverage[0].glyphs:
                continue
            rule = copy.deepcopy(subtable)
            rule.InputCoverage = [coverage(font, [colon])]
            for attribute in ("BacktrackCoverage", "LookAheadCoverage"):
                setattr(rule, attribute, [coverage(font, sorted({glyph_map[name] for name in cov.glyphs if name in glyph_map})) for cov in getattr(rule, attribute)])
            if any(not cov.glyphs for cov in [*rule.BacktrackCoverage, *rule.LookAheadCoverage]):
                continue
            for record in rule.SubstLookupRecord:
                record.LookupListIndex = single_index
            rules.append(rule)
    if not rules:
        raise RuntimeError("固定 Inter 来源中未找到冒号上下文规则")
    chain = ot.Lookup(); chain.LookupType = 6; chain.LookupFlag = 0
    chain.SubTable = rules; chain.SubTableCount = len(rules)
    chain_index = append_gsub_lookup(font, chain)
    merged = merge_gsub_lookup_indices_into_features(font, "calt", [chain_index])
    if not merged["calt_features_merged"]:
        append_gsub_feature(font, "calt", [chain_index])
        enable_features_for_all_scripts(font, {"calt"})
    # Inter runs calt before width and zero substitutions. Preserve that order
    # so tnum cannot replace colon with colon.tf before its context is matched.
    width_indices = feature_lookup_indices(font, "GSUB", {"tnum", "pnum", "zero"})
    if width_indices:
        indices = move_gsub_lookups_before(font, [single_index, chain_index], min(width_indices))
        single_index, chain_index = indices[single_index], indices[chain_index]
    return {"digit_colon_feature_added": True, "digit_colon_context": "fixed-inter-source-rules", "digit_colon_rules": len(rules), "digit_colon_existing_calt_mappings_removed": removed, "digit_colon_single_lookup_index": single_index, "digit_colon_chain_lookup_index": chain_index, **cleanup}


def build_one_variable(region: str, italic: bool) -> dict[str, Any]:
    region = check_region(region)
    style_label = f"{region} {'italic' if italic else 'upright'}"
    unicodes = reference_unicodes(region)
    log_step(f"variable {style_label}: load sources")
    inter = load_inter(italic)
    base, sarasa_report = load_base(region, italic, set(inter.getBestCmap().keys()))
    reference_fonts: dict[int, TTFont] = {}
    try:
        for weight_name, weight_value in VF_METRIC_REFERENCE_STOPS:
            reference_fonts[weight_value] = open_vf_metric_reference_font(
                region,
                weight_name,
                italic,
            )
        log_step(f"variable {style_label}: merge outlines and layout")
        merge_report = append_inter_glyphs(base, inter, unicodes)
        remove_metric_variation_maps(base)
        feature_drop_report = drop_sarasa_width_features(base)
        locl_report = prune_locl_like_reference(base, region)
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
    skip_metric_codepoints = set(range(0x30, 0x3A)) | {0x3A} | DASH_CMAP_CODEPOINTS
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
        product_metric_report = align_product_metrics_to_inter_static(base, italic)
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
    underline_mvar_report = rebuild_vf_underline_mvar(base)
    rebuild_stat(base, italic)
    font_revision_report = update_head_project_revision(base)
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
        for weight_name, weight_value in VF_METRIC_REFERENCE_STOPS:
            reference_fonts_roundtrip[weight_value] = open_vf_metric_reference_font(
                region,
                weight_name,
                italic,
            )
        target_inter_roundtrip = load_inter(italic)
        try:
            log_step(f"variable {style_label}: roundtrip Inter outlines")
            roundtrip_inter_outline_report = prefix_count_report(
                add_inter_outline_correction_variations(
                    base,
                    target_inter_roundtrip,
                    final_cmap=True,
                ),
                "roundtrip_",
            )
        finally:
            target_inter_roundtrip.close()
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
        roundtrip_product_metric_report = prefix_count_report(
            align_product_metrics_to_inter_static(base, italic),
            "roundtrip_",
        )
        update_head_project_revision(base)
        log_step(f"variable {style_label}: save roundtrip")
        base.save(out_path, reorderTables=True)
    finally:
        for reference_font in reference_fonts_roundtrip.values():
            reference_font.close()

    base.close()
    log_step(f"variable {style_label}: add Noto chws/vchw")
    contextual_spacing_report = add_noto_contextual_spacing(out_path)
    base = TTFont(out_path)
    final_em_dash_ligature_report = apply_upstream_dash_behavior(
        base,
        region,
        italic=italic,
    )
    final_mac_name_report = remove_mac_name_records(base)

    # Recompile every inherited tuple before the final outline and metric
    # calibration.  Recompiling raw upstream gvar data after calibration can
    # change composite interpolation by a few units and invalidate the control
    # points that were just made exact.
    log_step(f"variable {style_label}: materialize inherited gvar")
    inherited_gvar_materialization_report = prefix_count_report(
        materialize_gvar_variations(base),
        "inherited_",
    )
    if inherited_gvar_materialization_report[
        "inherited_gvar_coordinate_length_mismatches"
    ]:
        raise RuntimeError(
            "VF inherited gvar coordinate lengths do not match glyf: "
            + repr(
                inherited_gvar_materialization_report[
                    "inherited_gvar_coordinate_length_mismatch_samples"
                ][:8]
            )
        )
    base.save(out_path, reorderTables=True)
    base.close()
    base = TTFont(out_path)

    final_metric_references: dict[int, TTFont] = {}
    try:
        for weight_name, weight_value in VF_METRIC_REFERENCE_STOPS:
            final_metric_references[weight_value] = (
                open_project_static_metric_reference_font(
                    region,
                    weight_name,
                    italic,
                )
            )
        log_step(f"variable {style_label}: align LSB/xMin control points")
        lsb_xmin_alignment_report = align_variable_outlines_to_lsb(
            base,
            final_metric_references,
        )
        log_step(f"variable {style_label}: rebuild HVAR/VVAR")
        metric_variation_report = rebuild_vf_metric_variation_tables(
            base,
            final_metric_references,
        )
    finally:
        for metric_reference in final_metric_references.values():
            metric_reference.close()

    # Freeze the calibrated metric tuples before the last source-outline pass.
    # That pass changes only relative component positions in composite Inter
    # glyphs. A dependency-ordered follow-up restores xMin/yMax, then uniform
    # whole-glyph translations make serialized HarfBuzz side bearings exact
    # without changing the translation-invariant Inter outline match.
    base.save(out_path, reorderTables=True)
    base.close()
    base = TTFont(out_path)
    log_step(f"variable {style_label}: final Inter outline controls")
    final_inter = load_inter(italic)
    try:
        (
            final_inter_composite_report,
            final_inter_composite_bounds,
        ) = add_translation_invariant_inter_composite_corrections(
            base,
            final_inter,
        )
    finally:
        final_inter.close()

    log_step(f"variable {style_label}: freeze final outline geometry")
    post_metric_gvar_report = prefix_count_report(
        materialize_gvar_variations(base),
        "post_metric_",
    )
    if post_metric_gvar_report["post_metric_gvar_coordinate_length_mismatches"]:
        raise RuntimeError(
            "VF post-metric gvar coordinate lengths do not match glyf: "
            + repr(
                post_metric_gvar_report[
                    "post_metric_gvar_coordinate_length_mismatch_samples"
                ][:8]
            )
        )
    base.save(out_path, reorderTables=True)
    base.close()
    base = TTFont(out_path)

    log_step(f"variable {style_label}: restore Inter composite metric bounds")
    final_inter_bound_report = restore_inter_composite_control_bounds(
        base,
        final_inter_composite_bounds,
    )
    log_step(f"variable {style_label}: align Inter composite HarfBuzz metrics")
    final_inter_engine_metric_report = align_inter_composite_harfbuzz_metrics(
        base,
        region,
        italic,
    )
    log_step(f"variable {style_label}: normalize TrueType vertical origins")
    vorgless_report = normalize_variable_vertical_origin(base)
    vorgless_report.update(add_digit_colon_feature(base))
    vorgless_report.update(align_tabular_alternate_advances(base))
    vorgless_report.update(normalize_cross_engine_metrics(base))
    log_step(f"variable {style_label}: materialize and validate final gvar")
    gvar_finalization_report = materialize_gvar_variations(base)
    if gvar_finalization_report["gvar_coordinate_length_mismatches"]:
        raise RuntimeError(
            "VF gvar coordinate lengths do not match the final glyf table: "
            + repr(
                gvar_finalization_report[
                    "gvar_coordinate_length_mismatch_samples"
                ][:8]
            )
        )
    base.save(out_path, reorderTables=True)
    base.close()
    base = TTFont(out_path)

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
    final_validation_ok, final_validation_report = variable_output_resume_status(
        region,
        italic,
    )
    if not final_validation_ok:
        raise RuntimeError(
            f"variable {style_label} failed final validation: "
            + "; ".join(
                final_validation_report.get("resume_rebuild_reasons", [])
            )
        )

    return {
        "file": portable_report_path(out_path),
        "region": region,
        "source_han_region": source_han_vf_basename(region),
        "axes": axes,
        "instances": instances,
        "glyph_count": glyph_count,
        "final_validation": final_validation_report,
        "default_digit_widths": widths,
        "key_symbol_widths": key_widths,
        **sarasa_report,
        **merge_report,
        **digit_report,
        **inter_outline_report,
        **feature_drop_report,
        **locl_report,
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
        **product_metric_report,
        **vmtx_report,
        **vmtx_variation_report,
        **roundtrip_inter_outline_report,
        **roundtrip_lsb_variation_report,
        **roundtrip_vmtx_variation_report,
        **roundtrip_tnum_target_variation_report,
        **roundtrip_product_metric_report,
        **empty_feature_report,
        **gsub_template_report,
        **gpos_template_report,
        **gsub_lookup_report,
        **gpos_lookup_report,
        **digit_colon_merge_report,
        **gdef_report,
        **vorg_report,
        **metadata_report,
        **underline_mvar_report,
        **font_revision_report,
        **extra_table_report,
        **colon_report,
        **contextual_spacing_report,
        **final_em_dash_ligature_report,
        **final_mac_name_report,
        **inherited_gvar_materialization_report,
        **lsb_xmin_alignment_report,
        **metric_variation_report,
        **final_inter_composite_report,
        **post_metric_gvar_report,
        **final_inter_bound_report,
        **final_inter_engine_metric_report,
        **vorgless_report,
        **gvar_finalization_report,
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


def validate_archive_member_paths(names: list[str], destination: Path) -> None:
    destination = destination.resolve()
    for raw_name in names:
        normalized = raw_name.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        windows_path = PureWindowsPath(raw_name)
        if (
            not normalized
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or ".." in posix_path.parts
        ):
            raise RuntimeError(f"unsafe archive member path: {raw_name!r}")
        candidate = destination.joinpath(*posix_path.parts).resolve()
        try:
            candidate.relative_to(destination)
        except ValueError as error:
            raise RuntimeError(f"archive member escapes destination: {raw_name!r}") from error


def validate_zip_archive(zf: zipfile.ZipFile, destination: Path) -> None:
    infos = zf.infolist()
    validate_archive_member_paths([info.filename for info in infos], destination)
    symlinks = [
        info.filename
        for info in infos
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK
    ]
    if symlinks:
        raise RuntimeError(f"ZIP symlink members are not supported: {symlinks[0]!r}")


def safe_extract_zip_all(zf: zipfile.ZipFile, destination: Path) -> None:
    validate_zip_archive(zf, destination)
    zf.extractall(destination)


def safe_extract_tar_all(tf: tarfile.TarFile, destination: Path) -> None:
    members = tf.getmembers()
    validate_archive_member_paths([member.name for member in members], destination)
    destination = destination.resolve()
    for member in members:
        if not (member.issym() or member.islnk()):
            continue
        member_path = PurePosixPath(member.name.replace("\\", "/"))
        link_path = PurePosixPath(member.linkname.replace("\\", "/"))
        windows_link = PureWindowsPath(member.linkname)
        if link_path.is_absolute() or windows_link.is_absolute() or windows_link.drive:
            raise RuntimeError(
                f"unsafe archive link target: {member.name!r} -> {member.linkname!r}"
            )
        base = destination.joinpath(*member_path.parent.parts) if member.issym() else destination
        target = base.joinpath(*link_path.parts).resolve()
        try:
            target.relative_to(destination)
        except ValueError as error:
            raise RuntimeError(
                f"archive link escapes destination: {member.name!r} -> {member.linkname!r}"
            ) from error
    tf.extractall(destination, members=members, filter="data")


def extract_zip_basename(archive: Path, basename: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        validate_zip_archive(zf, out_path.parent)
        members = [name for name in zf.namelist() if Path(name).name == basename]
        if not members:
            raise FileNotFoundError(f"{basename} not found in {archive}")
        member = sorted(members, key=len)[0]
        log_step(f"extract {basename}")
        payload = zf.read(member)
        verify_or_write_source(out_path, payload)
    return out_path


def extract_zip_first_basename(archive: Path, basenames: list[str], out_dir: Path) -> Path:
    with zipfile.ZipFile(archive) as zf:
        validate_zip_archive(zf, out_dir)
        names = zf.namelist()
        for basename in basenames:
            members = [name for name in names if Path(name).name == basename]
            if not members:
                continue
            out_path = out_dir / basename
            out_path.parent.mkdir(parents=True, exist_ok=True)
            member = sorted(members, key=len)[0]
            verify_or_write_source(out_path, zf.read(member))
            return out_path
    raise FileNotFoundError(f"None of {basenames} found in {archive}")


def extract_7z_ttf_prefix(archive: Path, out_dir: Path, prefix: str) -> None:
    import py7zr

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-extract-") as tmp_name:
        tmp_dir = Path(tmp_name)
        log_step(f"extract {archive.name}")
        with py7zr.SevenZipFile(archive) as zf:
            validate_archive_member_paths(zf.getnames(), tmp_dir)
            zf.extractall(tmp_dir)
        for path in tmp_dir.rglob(f"{prefix}*.ttf"):
            verify_or_write_source(out_dir / path.name, path.read_bytes())


def extract_7z_basename(archive: Path, basename: str, out_path: Path) -> Path:
    import py7zr

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-extract-") as tmp_name:
        tmp_dir = Path(tmp_name)
        log_step(f"extract {basename}")
        with py7zr.SevenZipFile(archive) as zf:
            validate_archive_member_paths(zf.getnames(), tmp_dir)
            members = [name for name in zf.getnames() if Path(name).name == basename]
            if not members:
                raise FileNotFoundError(f"{basename} not found in {archive}")
            zf.extract(path=tmp_dir, targets=[sorted(members, key=len)[0]])
        matches = list(tmp_dir.rglob(basename))
        if not matches:
            raise FileNotFoundError(f"{basename} not extracted from {archive}")
        verify_or_write_source(out_path, matches[0].read_bytes())
    return out_path


def verify_or_write_source(path: Path, payload: bytes) -> None:
    """Compare cached bytes with a member of a freshly verified archive."""
    expected = hashlib.sha256(payload).hexdigest()
    if path.exists():
        if file_sha256(path) != expected:
            raise RuntimeError(f"缓存源 SHA-256 校验失败：{path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".extract.tmp")
    pending.write_bytes(payload)
    pending.replace(path)


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


_NODE_RUNTIME_VERIFIED = False


def ensure_node_runtime() -> None:
    global _NODE_RUNTIME_VERIFIED
    if _NODE_RUNTIME_VERIFIED:
        return
    system, arch, ext = node_platform_archive()
    archive_name = f"node-{NODE_VERSION}-{system}-{arch}.{ext}"
    archive = download_file_checked(
        f"https://nodejs.org/dist/{NODE_VERSION}/{archive_name}",
        SOURCE_ARCHIVE_DIR / archive_name,
        NODE_ARCHIVE_SHA256[(system, arch, ext)],
    )
    NODE_DIR.mkdir(parents=True, exist_ok=True)
    log_step(f"extract {archive_name}")
    if ext == "zip":
        with zipfile.ZipFile(archive) as zf:
            validate_zip_archive(zf, NODE_DIR)
            for member in zf.infolist():
                if not member.is_dir():
                    verify_or_write_source(NODE_DIR / member.filename, zf.read(member))
    else:
        with tempfile.TemporaryDirectory(prefix="sarasa-node-") as tmp_name:
            staging = Path(tmp_name)
            with tarfile.open(archive, "r:xz") as tf:
                safe_extract_tar_all(tf, staging)
            for member in staging.rglob("*"):
                destination = NODE_DIR / member.relative_to(staging)
                if member.is_symlink():
                    target = os.readlink(member)
                    if destination.is_symlink():
                        if os.readlink(destination) != target:
                            raise RuntimeError(f"Node 缓存链接校验失败：{member.name}")
                    elif destination.exists():
                        raise RuntimeError(f"Node 缓存链接类型错误：{member.name}")
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.symlink_to(target)
                elif member.is_file():
                    verify_or_write_source(destination, member.read_bytes())
                    destination.chmod(member.stat().st_mode)
    _NODE_RUNTIME_VERIFIED = True


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
            safe_extract_zip_all(zf, tmp_dir)
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
    source_zip = download_file_checked(
        f"https://github.com/be5invis/Sarasa-Gothic/archive/{SARASA_COMMIT}.zip",
        SOURCE_ARCHIVE_DIR / f"Sarasa-Gothic-{SARASA_COMMIT}.zip",
        SARASA_SOURCE_ARCHIVE_SHA256,
    )
    extract_zip_tree(source_zip, SARASA_SOURCE_DIR)


def ensure_inter_sources() -> None:
    archive = download_file_checked(
        f"https://github.com/rsms/inter/releases/download/{INTER_TAG}/Inter-4.1.zip",
        SOURCE_ARCHIVE_DIR / "Inter-4.1.zip",
        INTER_ARCHIVE_SHA256,
    )
    extract_zip_basename(archive, "InterVariable.ttf", INTER_UPRIGHT)
    italic_basename = "InterVariable-Italic.ttf" if INTER_ITALIC.suffix.lower() == ".ttf" else "InterVariable-Italic.woff2"
    extract_zip_basename(archive, italic_basename, INTER_ITALIC)


def ensure_vf_sources(regions: list[str]) -> None:
    needed_classical_vfs = [
        path for path in (classical_vf_override_path(region) for region in variable_regions(regions)) if path
    ]
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    source_han_zip = download_file_checked(
        f"https://github.com/adobe-fonts/source-han-sans/releases/download/{SOURCE_HAN_TAG}/02_SourceHanSans-VF.zip",
        SOURCE_ARCHIVE_DIR / f"SourceHanSans-VF-{SOURCE_HAN_TAG}.zip",
        SOURCE_HAN_VF_ARCHIVE_SHA256,
    )
    for region in variable_regions(regions):
        target = source_han_vf_path(region)
        extract_zip_basename(source_han_zip, source_han_vf_basename(region) or "", target)
    if needed_classical_vfs:
        shanggu_archive = download_file_checked(
            f"https://github.com/GuiWonder/Shanggu/releases/download/{SHANGGU_TAG}/{SHANGGU_SANS_VF_ARCHIVE_NAME}",
            SOURCE_ARCHIVE_DIR / f"ShangguSansVF_TTFs-{SHANGGU_TAG}.7z",
            SHANGGU_SANS_VF_SHA256,
        )
        for target in needed_classical_vfs:
            extract_7z_basename(shanggu_archive, target.name, target)
    ensure_inter_sources()


def ensure_classical_static_sources(regions: list[str]) -> None:
    needed = [
        path
        for region in regions
        if region_config(region)["classical"]
        for weight_name in STATIC_STYLE_SOURCES
        for path in [classical_static_override_path(region, weight_name)]
        if path
    ]
    if not needed:
        return
    shanggu_archive = download_file_checked(
        f"https://github.com/GuiWonder/Shanggu/releases/download/{SHANGGU_TAG}/{SHANGGU_SANS_TTF_ARCHIVE_NAME}",
        SOURCE_ARCHIVE_DIR / f"ShangguSansTTFs-{SHANGGU_TAG}.7z",
        SHANGGU_SANS_TTF_SHA256,
    )
    for directory in dict.fromkeys(target.parent for target in needed):
        extract_7z_ttf_prefix(shanggu_archive, directory, "ShangguSansTC-")
    for region in regions:
        if not region_config(region)["classical"]:
            continue
        for weight_name, source in STATIC_STYLE_SOURCES.items():
            target = classical_static_override_path(region, weight_name)
            canonical = target.parent / f"ShangguSansTC-{source['shs']}.ttf"
            if target != canonical:
                verify_or_write_source(target, canonical.read_bytes())


def ensure_reference_sarasa(regions: list[str]) -> None:
    global REFERENCE_SARASA, REFERENCE_SARASA_DIR, REFERENCE_SARASA_HINTED_DIR
    for region in regions:
        prefix = sarasa_region_prefix(region)
        hinted_dir = region_reference_dir(region, True)
        unhinted_dir = region_reference_dir(region, False)
        hinted_regular = hinted_dir / f"{prefix}-Regular.ttf"
        unhinted_regular = unhinted_dir / f"{prefix}-Regular.ttf"
        hinted_archive = download_file_checked(
            f"https://github.com/be5invis/Sarasa-Gothic/releases/download/{SARASA_TAG}/{prefix}-TTF-{SARASA_VERSION}.7z",
            SOURCE_ARCHIVE_DIR / f"{prefix}-TTF-{SARASA_VERSION}.7z",
            SARASA_UI_ARCHIVE_SHA256[region]["hinted"],
        )
        unhinted_archive = download_file_checked(
            f"https://github.com/be5invis/Sarasa-Gothic/releases/download/{SARASA_TAG}/{prefix}-TTF-Unhinted-{SARASA_VERSION}.7z",
            SOURCE_ARCHIVE_DIR / f"{prefix}-TTF-Unhinted-{SARASA_VERSION}.7z",
            SARASA_UI_ARCHIVE_SHA256[region]["unhinted"],
        )
        extract_7z_ttf_prefix(hinted_archive, hinted_dir, f"{prefix}-")
        extract_7z_ttf_prefix(unhinted_archive, unhinted_dir, f"{prefix}-")
        for path in [hinted_regular, unhinted_regular]:
            font = TTFont(path, lazy=True)
            try:
                version_name = font_name(font, 5) or ""
            finally:
                font.close()
            if f"Version {SARASA_VERSION}" not in version_name:
                raise RuntimeError(
                    f"Sarasa reference version mismatch at {path}: "
                    f"expected Version {SARASA_VERSION}, got {version_name!r}"
                )
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

    package_json = SARASA_SOURCE_DIR / "package.json"
    package_lock = SARASA_SOURCE_DIR / "package-lock.json"
    if not package_json.exists() or not package_lock.exists():
        raise FileNotFoundError(f"Sarasa Gothic package metadata is missing from {SARASA_SOURCE_DIR}")
    package_version = str(json.loads(package_json.read_text(encoding="utf-8"))["version"])
    if package_version != SARASA_VERSION:
        raise RuntimeError(
            f"Sarasa Gothic source version mismatch at {SARASA_SOURCE_DIR}: "
            f"expected {SARASA_VERSION}, got {package_version}"
        )
    lock_sha256 = file_sha256(package_lock)
    if lock_sha256 != SARASA_PACKAGE_LOCK_SHA256:
        raise RuntimeError(
            f"Sarasa Gothic package-lock mismatch at {package_lock}: "
            f"expected {SARASA_PACKAGE_LOCK_SHA256}, got {lock_sha256}"
        )
    git_dir = SARASA_SOURCE_DIR / ".git"
    if git_dir.exists() and shutil.which("git"):
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=SARASA_SOURCE_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
        actual_commit = result.stdout.strip()
        if actual_commit != SARASA_COMMIT:
            raise RuntimeError(
                f"Sarasa Gothic source commit mismatch at {SARASA_SOURCE_DIR}: "
                f"expected {SARASA_COMMIT}, got {actual_commit}"
            )
        result = subprocess.run(
            ["git", "diff", "--exit-code", "HEAD", "--", "."],
            cwd=SARASA_SOURCE_DIR, capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError("Sarasa 固定提交的源码或字体缓存已被修改")
    else:
        source_zip = download_file_checked(
            f"https://github.com/be5invis/Sarasa-Gothic/archive/{SARASA_COMMIT}.zip",
            SOURCE_ARCHIVE_DIR / f"Sarasa-Gothic-{SARASA_COMMIT}.zip",
            SARASA_SOURCE_ARCHIVE_SHA256,
        )
        with zipfile.ZipFile(source_zip) as archive:
            validate_zip_archive(archive, SARASA_SOURCE_DIR)
            for member in archive.infolist():
                if not member.is_dir():
                    relative = Path(*PurePosixPath(member.filename).parts[1:])
                    verify_or_write_source(SARASA_SOURCE_DIR / relative, archive.read(member))

    npm_marker = SARASA_SOURCE_DIR / "node_modules" / ".sarasa-ui-propdigits-package-lock.sha256"
    marker_value = npm_marker.read_text(encoding="ascii").strip() if npm_marker.exists() else ""
    dependencies_current = SARASA_CHLOROPHYTUM.exists() and marker_value == lock_sha256
    if not dependencies_current and os.environ.get("SARASA_SKIP_CHLOROPHYTUM") != "1":
        log_step("install locked Sarasa Gothic npm dependencies")
        run_checked([npm_executable(), "ci"], cwd=SARASA_SOURCE_DIR, capture_output=False, env=local_runtime_env())
        npm_marker.parent.mkdir(parents=True, exist_ok=True)
        npm_marker.write_text(lock_sha256 + "\n", encoding="ascii")
def ensure_build_sources(static_only: bool, regions: list[str], *, full_hint_group: bool = True) -> None:
    if os.environ.get("SARASA_SKIP_SOURCE_BOOTSTRAP") == "1":
        return
    ensure_reference_sarasa(regions)
    ensure_sarasa_source_tree()
    # Even a single-region hinted build analyzes the same full hint group.
    ensure_classical_static_sources(REGION_ORDER if full_hint_group else regions)
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


def optional_file_sha256(path: Path) -> str | None:
    return file_sha256(path) if path.exists() else None


def stable_sfnt_fingerprint(path: Path) -> str:
    try:
        font = TTFont(path, recalcTimestamp=False)
        try:
            if "head" in font:
                font["head"].created = 0
                font["head"].modified = 0
            buffer = BytesIO()
            font.save(buffer, reorderTables=True)
            return "canonical-sfnt:" + hashlib.sha256(buffer.getvalue()).hexdigest()
        finally:
            font.close()
    except (
        TTLibError,
        OSError,
        EOFError,
        KeyError,
        ValueError,
        AssertionError,
        struct.error,
    ) as error:
        log_step(
            f"cache fingerprint falls back to raw bytes for {path}: "
            f"{type(error).__name__}: {error}"
        )
        return "raw-file:" + file_sha256(path)


def static_hint_recipe_fingerprint() -> str:
    payload = json.dumps(STATIC_HINT_RECIPE, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chlorophytum_package_id() -> dict[str, Any]:
    package_dir = SARASA_SOURCE_DIR / "node_modules" / "@chlorophytum" / "cli"
    package_json = package_dir / "package.json"
    return {
        "startup": optional_file_sha256(SARASA_CHLOROPHYTUM),
        "package": optional_file_sha256(package_json),
    }


def static_hint_group_cache_key(
    weight_name: str,
    group_name: str,
    jobs: list[tuple[Path, Path, str]],
) -> str:
    config_name, config_path = sarasa_hint_config(weight_name)
    payload = {
        "kind": "static-full-group-chlorophytum",
        "version": 2,
        "weight": weight_name,
        "group": group_name,
        "config_name": config_name,
        "config_sha256": file_sha256(config_path),
        "inputs": [stable_sfnt_fingerprint(path) for path, _hint, _weight in jobs],
        "chlorophytum": chlorophytum_package_id(),
        "hint_store_order": CHLOROPHYTUM_HINT_STORE_ORDER,
        "hint_recipe_sha256": static_hint_recipe_fingerprint(),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def static_hint_work_key(weight_name: str) -> str:
    config_name, config_path = sarasa_hint_config(weight_name)
    style_names = [
        sarasa_hint_source_style(weight_name, italic)
        for italic in (False, True)
    ]
    source = STATIC_STYLE_SOURCES[weight_name]
    permanent_inputs = [
        SARASA_SOURCE_DIR / "sources" / "shs" / f"SourceHanSans-{source['shs']}.ttc",
        config_path,
    ]
    for group in sorted({sarasa_hint_latin_group(family) for family in SARASA_HINT_FAMILY_ORDER}):
        for style in style_names:
            permanent_inputs.append(
                SARASA_SOURCE_DIR / "sources" / group / f"{group}-{style}.ttf"
            )
    for italic in (False, True):
        target_style = inter_source_style(weight_name, italic)
        if target_style:
            permanent_inputs.append(
                SARASA_SOURCE_DIR / "sources" / "Inter" / f"Inter-{target_style}.ttf"
            )
        else:
            permanent_inputs.append(INTER_ITALIC if italic else INTER_UPRIGHT)
    classical = classical_static_override_path("CL", weight_name)
    if classical:
        permanent_inputs.append(classical)
    missing = [path for path in permanent_inputs if not path.exists()]
    if missing:
        raise FileNotFoundError(missing[0])
    payload = {
        "kind": "static-hint-work",
        "version": STATIC_HINT_WORK_VERSION,
        "weight": weight_name,
        "config_name": config_name,
        "inputs": [
            {
                "slot": index,
                "name": path.name,
                "sha256": file_sha256(path),
            }
            for index, path in enumerate(permanent_inputs)
        ],
        "ttfautohint_py": importlib.metadata.version("ttfautohint-py"),
        "afdko": importlib.metadata.version("afdko"),
        "sarasa_commit": SARASA_COMMIT,
        "chlorophytum": chlorophytum_package_id(),
        "hint_recipe_sha256": static_hint_recipe_fingerprint(),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def static_hint_work_dir(weight_name: str) -> tuple[str, Path]:
    key = static_hint_work_key(weight_name)
    # Keep the active tree comfortably below legacy Windows MAX_PATH. The
    # manifest stores and verifies the full key; the prefix only names the dir.
    work_dir = STATIC_HINT_WORK_ROOT / f"{weight_name}-{key[:20]}"
    manifest_path = work_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("key") != key:
            raise RuntimeError(f"Static hint work key collision at {work_dir}")
    return key, work_dir


def restore_static_hint_group_cache(
    key: str,
    weight_name: str,
    jobs: list[tuple[Path, Path, str]],
) -> tuple[str, dict[Path, dict[str, Any]]] | None:
    if os.environ.get("SARASA_DISABLE_BUILD_CACHE") == "1":
        return None
    cache_dir = BUILD_CACHE_DIR / "static-hint-groups" / key
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("files") != len(jobs):
        return None
    cached_hashes = manifest.get("sha256")
    cached_reports = manifest.get("reports")
    if not isinstance(cached_hashes, list) or len(cached_hashes) != len(jobs):
        return None
    if not isinstance(cached_reports, list) or len(cached_reports) != len(jobs):
        return None
    reports: dict[Path, dict[str, Any]] = {}
    for index, (_input, hint_path, _weight) in enumerate(jobs):
        cached = cache_dir / f"{index:03d}.hint.gz"
        if not cached.exists() or file_sha256(cached) != cached_hashes[index]:
            return None
        hint_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, hint_path)
        reports[hint_path.resolve()] = {
            **cached_reports[index],
            "chlorophytum_hinted": True,
            "chlorophytum_cache_hit": True,
            "chlorophytum_cache_key": key,
            "chlorophytum_hint_config": sarasa_hint_config(weight_name)[0],
            "chlorophytum_hint_store_order": CHLOROPHYTUM_HINT_STORE_ORDER,
        }
    return key, reports


def store_static_hint_group_cache(
    key: str,
    weight_name: str,
    jobs: list[tuple[Path, Path, str]],
    reports: dict[Path, dict[str, Any]],
) -> None:
    if os.environ.get("SARASA_DISABLE_BUILD_CACHE") == "1":
        return
    cache_dir = BUILD_CACHE_DIR / "static-hint-groups" / key
    pending = cache_dir.with_name(cache_dir.name + ".pending")
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True, exist_ok=True)
    for index, (_input, hint_path, _weight) in enumerate(jobs):
        shutil.copy2(hint_path, pending / f"{index:03d}.hint.gz")
    manifest = {
        "key": key,
        "weight": weight_name,
        "files": len(jobs),
        "created_by": "tools/build_sarasa_ui_propdigits_sc.py",
        "sha256": [file_sha256(pending / f"{index:03d}.hint.gz") for index in range(len(jobs))],
        "reports": [reports[hint.resolve()] for _input, hint, _weight in jobs],
    }
    (pending / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    pending.replace(cache_dir)


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


def chlorophytum_glyph_key(value: str) -> tuple[int, str]:
    suffix = value.rsplit("#", 1)
    if len(suffix) == 2 and suffix[1].isdigit():
        return int(suffix[1]), value
    return 0x7FFFFFFF, value


def sarasa_shared_hint_group_order(config_path: Path) -> tuple[str, ...]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    result: list[str] = []
    for item in config.get("hintOptions", {}).get("passes", []):
        hint_pass = item.get("hintOptions", {}).get("pass", {})
        if hint_pass.get("hintPlugin") != "@chlorophytum/hm-ideograph":
            continue
        group_name = str(hint_pass.get("hintOptions", {}).get("groupName", "Ideograph"))
        if group_name not in result:
            result.append(group_name)
    if not result:
        raise RuntimeError(f"No Chlorophytum ideograph passes found in {config_path}")
    return tuple(result)


def chlorophytum_shared_hint_key(value: str, group_order: tuple[str, ...]) -> int:
    for index, group_name in enumerate(group_order):
        if value.endswith(f"{{{group_name}}}"):
            return index
    return len(group_order)


def normalize_chlorophytum_hint_store(
    path: Path,
    shared_group_order: tuple[str, ...],
) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        source = json.load(handle)
    if not isinstance(source, dict):
        raise TypeError(f"Unsupported Chlorophytum hint store at {path}")

    normalized: dict[str, Any] = {}
    for key in ("glyphs", "glyphHintCacheKeys"):
        values = source.get(key, {})
        if not isinstance(values, dict):
            raise TypeError(f"Unsupported {key} section in {path}")
        normalized[key] = {
            name: values[name]
            for name in sorted(values, key=chlorophytum_glyph_key)
        }
    shared = source.get("sharedHints", {})
    if not isinstance(shared, dict):
        raise TypeError(f"Unsupported sharedHints section in {path}")
    # Shared hint insertion order is semantic: Chlorophytum compiles these
    # models in Map order and assigns function IDs as it goes. Reproduce the
    # hcfg pass order explicitly; alphabetic ordering changes rendered pixels.
    normalized["sharedHints"] = {
        name: shared[name]
        for name in sorted(
            shared,
            key=lambda name: chlorophytum_shared_hint_key(name, shared_group_order),
        )
    }
    known_shared_groups = [
        next(
            group_name
            for group_name in shared_group_order
            if name.endswith(f"{{{group_name}}}")
        )
        for name in normalized["sharedHints"]
        if any(
            name.endswith(f"{{{group_name}}}")
            for group_name in shared_group_order
        )
    ]
    expected_shared_groups = [
        group_name
            for group_name in shared_group_order
        if any(name.endswith(f"{{{group_name}}}") for name in shared)
    ]
    if known_shared_groups != expected_shared_groups:
        raise RuntimeError(
            "Chlorophytum shared hint order does not match the Sarasa hcfg pass order: "
            f"{known_shared_groups!r} != {expected_shared_groups!r}"
        )
    for key, value in source.items():
        if key not in normalized:
            normalized[key] = value

    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    return {
        "chlorophytum_hint_store_order": CHLOROPHYTUM_HINT_STORE_ORDER,
        "chlorophytum_hint_store_glyphs": len(normalized["glyphs"]),
        "chlorophytum_hint_store_shared": len(normalized["sharedHints"]),
        "chlorophytum_hint_store_sha256": file_sha256(path),
    }


def link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def publish_staged_file(source: Path, destination: Path) -> None:
    if not source.exists() or not source.stat().st_size:
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(f"{destination.name}.{os.getpid()}.pending")
    if pending.exists():
        pending.unlink()
    shutil.copy2(source, pending)
    pending.replace(destination)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.{os.getpid()}.pending")
    pending.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pending.replace(path)


def chlorophytum_generate_static_hints(
    jobs: list[tuple[Path, Path, str]],
    tmp_dir: Path,
    hint_jobs: int | None = None,
) -> dict[Path, dict[str, Any]]:
    if not jobs:
        return {}
    tmp_dir = tmp_dir.resolve()
    jobs = [
        (in_path.resolve(), hint_path.resolve(), weight_name)
        for in_path, hint_path, weight_name in jobs
    ]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    config_names = {sarasa_hint_config(weight_name)[0] for _in_path, _hint_path, weight_name in jobs}
    if len(config_names) != 1:
        raise ValueError(f"Chlorophytum batch must use one hcfg, got {sorted(config_names)}")
    config_name = next(iter(config_names))
    config_path = SARASA_SOURCE_DIR / "hcfg" / f"{config_name}.json"
    shared_group_order = sarasa_shared_hint_group_order(config_path)

    reports: dict[Path, dict[str, Any]] = {}
    if os.environ.get("SARASA_SKIP_CHLOROPHYTUM") == "1":
        for _in_path, hint_path, _weight_name in jobs:
            reports[hint_path] = {
                "chlorophytum_hinted": False,
                "chlorophytum_hint_tool": "skipped",
                "chlorophytum_hint_config": config_name,
            }
        return reports
    if not SARASA_CHLOROPHYTUM.exists() or not config_path.exists():
        for _in_path, hint_path, _weight_name in jobs:
            reports[hint_path] = {
                "chlorophytum_hinted": False,
                "chlorophytum_hint_tool": "missing",
                "chlorophytum_hint_config": config_name,
            }
        return reports

    actual_hint_jobs = max(1, int(hint_jobs or SARASA_HINT_JOBS))
    with tempfile.TemporaryDirectory(prefix="sarasa-hint-") as stage_raw:
        stage = Path(stage_raw)
        hint_cmd = [
            node_executable(),
            str(SARASA_CHLOROPHYTUM),
            "hint",
            "-c",
            str(config_path),
            "-h",
            "cache.gz",
            "--jobs",
            str(actual_hint_jobs),
        ]
        staged_outputs: list[Path] = []
        for index, (in_path, _hint_path, _weight_name) in enumerate(jobs):
            staged_input = stage / "i" / f"{index:03d}.ttf"
            staged_output = stage / "h" / f"{index:03d}.gz"
            link_or_copy(in_path, staged_input)
            staged_output.parent.mkdir(parents=True, exist_ok=True)
            hint_cmd.extend([str(staged_input.relative_to(stage)), str(staged_output.relative_to(stage))])
            staged_outputs.append(staged_output)
        verbose = os.environ.get("SARASA_CHLOROPHYTUM_VERBOSE") == "1"
        result = subprocess.run(hint_cmd, cwd=stage, capture_output=not verbose)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
            stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
            raise RuntimeError(stderr or stdout or f"Chlorophytum hint failed with exit code {result.returncode}")
        for staged_output in staged_outputs:
            if not staged_output.exists() or not staged_output.stat().st_size:
                raise FileNotFoundError(staged_output)
        for (_in_path, hint_path, _weight_name), staged_output in zip(jobs, staged_outputs):
            normalize_report = normalize_chlorophytum_hint_store(
                staged_output,
                shared_group_order,
            )
            publish_staged_file(staged_output, hint_path)
            reports[hint_path] = {
                "chlorophytum_hinted": True,
                "chlorophytum_hint_tool": str(SARASA_CHLOROPHYTUM),
                "chlorophytum_hint_config": config_name,
                "chlorophytum_hint_jobs": actual_hint_jobs,
                "chlorophytum_hint_group_size": len(jobs),
                "chlorophytum_hint_cache": "ephemeral-short-path-stage",
                "chlorophytum_cache_hit": False,
                **normalize_report,
            }
    return reports


def chlorophytum_instruct_static_fonts(
    jobs: list[tuple[Path, Path, Path, str]],
) -> dict[Path, dict[str, Any]]:
    if not jobs:
        return {}
    jobs = [
        (in_path.resolve(), hint_path.resolve(), out_path.resolve(), weight_name)
        for in_path, hint_path, out_path, weight_name in jobs
    ]
    config_names = {
        sarasa_hint_config(weight_name)[0]
        for _in_path, _hint_path, _out_path, weight_name in jobs
    }
    if len(config_names) != 1:
        raise ValueError(f"Chlorophytum batch must use one hcfg, got {sorted(config_names)}")
    config_name = next(iter(config_names))
    config_path = SARASA_SOURCE_DIR / "hcfg" / f"{config_name}.json"
    reports: dict[Path, dict[str, Any]] = {}
    if os.environ.get("SARASA_SKIP_CHLOROPHYTUM") == "1" or not SARASA_CHLOROPHYTUM.exists():
        for in_path, _hint_path, out_path, _weight_name in jobs:
            shutil.copy2(in_path, out_path)
            reports[out_path] = {
                "chlorophytum_instructed": False,
                "chlorophytum_instruct_tool": "skipped",
                "chlorophytum_instruct_config": config_name,
            }
        return reports

    with tempfile.TemporaryDirectory(prefix="sarasa-instruct-") as stage_raw:
        stage = Path(stage_raw)
        cmd = [
            node_executable(),
            str(SARASA_CHLOROPHYTUM),
            "instruct",
            "-c",
            str(config_path),
        ]
        staged_outputs: list[Path] = []
        for index, (in_path, hint_path, _out_path, _weight_name) in enumerate(jobs):
            staged_input = stage / "i" / f"{index:03d}.ttf"
            staged_hint = stage / "h" / f"{index:03d}.gz"
            staged_output = stage / "o" / f"{index:03d}.ttf"
            link_or_copy(in_path, staged_input)
            link_or_copy(hint_path, staged_hint)
            staged_output.parent.mkdir(parents=True, exist_ok=True)
            cmd.extend(
                [
                    str(staged_input.relative_to(stage)),
                    str(staged_hint.relative_to(stage)),
                    str(staged_output.relative_to(stage)),
                ]
            )
            staged_outputs.append(staged_output)
        verbose = os.environ.get("SARASA_CHLOROPHYTUM_VERBOSE") == "1"
        result = subprocess.run(cmd, cwd=stage, capture_output=not verbose)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
            stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
            raise RuntimeError(stderr or stdout or f"Chlorophytum instruct failed with exit code {result.returncode}")
        for staged_output in staged_outputs:
            if not staged_output.exists() or not staged_output.stat().st_size:
                raise FileNotFoundError(staged_output)
        for (_in_path, _hint_path, out_path, _weight_name), staged_output in zip(jobs, staged_outputs):
            publish_staged_file(staged_output, out_path)
            reports[out_path] = {
                "chlorophytum_instructed": True,
                "chlorophytum_instruct_tool": str(SARASA_CHLOROPHYTUM),
                "chlorophytum_instruct_config": config_name,
                "chlorophytum_instruct_group_size": len(jobs),
                "chlorophytum_instruct_order": "pass1-hani-hang",
                "chlorophytum_short_path_stage": True,
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


def sarasa_hint_family_flags(family: str) -> dict[str, bool]:
    if family not in SARASA_HINT_FAMILY_ORDER:
        raise ValueError(f"Unsupported Sarasa hint family {family}")
    return {
        "goth": family == "Gothic",
        "mono": family in {"Mono", "MonoSlab", "Term", "TermSlab", "Fixed", "FixedSlab"},
        "pwid": family == "Ui",
        "tnum": family == "Ui",
        "term": family in {"Term", "TermSlab", "Fixed", "FixedSlab"},
    }


def sarasa_hint_latin_group(family: str) -> str:
    return {
        "Gothic": "Inter",
        "Ui": "Inter",
        "Mono": "IosevkaN",
        "MonoSlab": "IosevkaNSlab",
        "Term": "IosevkaNTerm",
        "TermSlab": "IosevkaNTermSlab",
        "Fixed": "IosevkaNFixed",
        "FixedSlab": "IosevkaNFixedSlab",
    }[family]


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


def sarasa_hint_latin_config(family: str) -> dict[str, Any]:
    return sarasa_latin_config() if sarasa_hint_latin_group(family) == "Inter" else {}


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


def sarasa_hint_source_style(weight_name: str, italic: bool) -> str:
    style = str(STATIC_STYLE_SOURCES[weight_name]["sarasa"])
    if italic:
        return "Italic" if style == "Regular" else f"{style}Italic"
    return style


def build_sarasa_hint_latin_source(
    family: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    tmp_dir: Path,
) -> Path:
    group = sarasa_hint_latin_group(family)
    style = sarasa_hint_source_style(weight_name, italic)
    out_dir = tmp_dir / "hint-environment" / f"latin-{group}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{group}-{style}.ttf"
    if out_path.exists():
        return out_path

    if group == "Inter" and family == "Ui":
        source = build_inter_source(weight_name, weight_value, italic, tmp_dir)
    else:
        source = SARASA_SOURCE_DIR / "sources" / group / f"{group}-{style}.ttf"
        if not source.exists():
            raise FileNotFoundError(source)
    run_ttfautohint(["-d", str(source), str(out_path)])
    return out_path


def build_sarasa_hint_environment_pass1(
    family: str,
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    tmp_dir: Path,
    fragments: dict[tuple[str, bool], dict[str, Any]],
) -> Path:
    if family == "Ui":
        return Path(fragments[(region, italic)]["pass1"])

    style = sarasa_hint_source_style(weight_name, italic)
    base_style = str(STATIC_STYLE_SOURCES[weight_name]["sarasa"])
    flags = sarasa_hint_family_flags(family)
    work_dir = tmp_dir / "hint-environment" / "fragments" / f"{family}-{region}-{style}"
    work_dir.mkdir(parents=True, exist_ok=True)
    non_kanji = tmp_dir / "hint-environment" / "non-kanji" / f"{region}-{base_style}.ttf"
    if not non_kanji.exists():
        raise FileNotFoundError(non_kanji)
    latin = build_sarasa_hint_latin_source(family, weight_name, weight_value, italic, tmp_dir)
    punct_args = {
        "family": family,
        "region": region,
        "style": base_style,
        "main": str(non_kanji),
        "lgc": str(latin),
        **flags,
    }
    ws = work_dir / "ws0.ttf"
    as_punct = work_dir / "as0.ttf"
    fe_misc = work_dir / "fe-misc0.ttf"
    if not ws.exists():
        run_sarasa_module(tmp_dir, "make/punct/ws.mjs", {**punct_args, "o": str(ws)})
    if not as_punct.exists():
        run_sarasa_module(tmp_dir, "make/punct/as.mjs", {**punct_args, "o": str(as_punct)})
    if not fe_misc.exists():
        run_sarasa_module(tmp_dir, "make/punct/fe-misc.mjs", {**punct_args, "o": str(fe_misc)})

    pass1 = work_dir / "pass1.ttf"
    if not pass1.exists():
        run_sarasa_module(
            tmp_dir,
            "make/pass1/index.mjs",
            {
                "main": str(latin),
                "as": str(as_punct),
                "ws": str(ws),
                "feMisc": str(fe_misc),
                "o": str(pass1),
                "family": family,
                "subfamily": region,
                "style": style,
                "italize": italic,
                "version": VERSION,
                "latinCfg": sarasa_hint_latin_config(family),
                **flags,
            },
        )
    return pass1


def build_sarasa_hint_environment(
    regions: list[str],
    weight_name: str,
    weight_value: int,
    tmp_dir: Path,
    fragments: dict[tuple[str, bool], dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_regions = list(REGION_ORDER)
    missing = [region for region in expected_regions if (region, False) not in fragments]
    if missing:
        raise RuntimeError(f"Full Sarasa hint environment is missing regions: {', '.join(missing)}")

    items = [
        (family, region, italic)
        for family in SARASA_HINT_FAMILY_ORDER
        for region in expected_regions
        for italic in (False, True)
    ]
    sarasa_module_runner(tmp_dir)
    for region in expected_regions:
        shs_ttf = build_shs_ttf(region, weight_name, tmp_dir)
        non_kanji = (
            tmp_dir
            / "hint-environment"
            / "non-kanji"
            / f"{region}-{STATIC_STYLE_SOURCES[weight_name]['sarasa']}.ttf"
        )
        if not non_kanji.exists():
            non_kanji.parent.mkdir(parents=True, exist_ok=True)
            run_sarasa_module(
                tmp_dir,
                "make/non-kanji/build.mjs",
                {"main": str(shs_ttf), "o": str(non_kanji)},
            )
    for family in SARASA_HINT_FAMILY_ORDER:
        if family != "Ui":
            for italic in (False, True):
                build_sarasa_hint_latin_source(
                    family, weight_name, weight_value, italic, tmp_dir
                )

    build_one = functools.partial(
        build_sarasa_hint_environment_pass1,
        weight_name=weight_name,
        weight_value=weight_value,
        tmp_dir=tmp_dir,
        fragments=fragments,
    )
    paths: dict[tuple[str, str, bool], Path] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, SARASA_HINT_PREP_JOBS)) as executor:
        future_map = {
            executor.submit(build_one, family=family, region=region, italic=italic): (family, region, italic)
            for family, region, italic in items
        }
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            paths[key] = future.result()

    result: list[dict[str, Any]] = []
    for family, region, italic in items:
        source_path = paths[(family, region, italic)]
        hinted_dir = tmp_dir / "hint-environment" / "pass1-hinted"
        hinted_dir.mkdir(parents=True, exist_ok=True)
        style = sarasa_hint_source_style(weight_name, italic)
        prepared_hint = fragments[(region, italic)].get("_pass1_hinted") if family == "Ui" else None
        hinted_path = (
            Path(prepared_hint)
            if prepared_hint
            else hinted_dir / f"{family}-{region}-{style}.ttf"
        )
        hint_path = tmp_dir / "hint-data" / "pass1" / f"{family}-{region}-{style}.hint.gz"
        out_path = tmp_dir / "hinted-environment" / "pass1" / f"{family}-{region}-{style}.ttf"
        result.append(
            {
                "family": family,
                "region": region,
                "italic": italic,
                "source": source_path,
                "hinted": hinted_path,
                "hint": hint_path,
                "output": out_path,
            }
        )
    pending_hint_items = [item for item in result if not Path(item["hinted"]).exists()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, SARASA_HINT_PREP_JOBS)) as executor:
        future_map = {
            executor.submit(hint_static_font, Path(item["source"]), Path(item["hinted"])): item
            for item in pending_hint_items
        }
        for future in concurrent.futures.as_completed(future_map):
            future_map[future]["ttfautohint_report"] = future.result()
    for item in result:
        prepared_report = (
            fragments[(str(item["region"]), bool(item["italic"]))].get("_ttfautohint_report")
            if item["family"] == "Ui"
            else None
        )
        item.setdefault(
            "ttfautohint_report",
            prepared_report or {"hinted": True, "hint_tool": "reused-full-group-preparation"},
        )
    return result


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
    classical_override = (
        build_classical_override_ttf(region, weight_name, tmp_dir)
        if region_config(region)["classical"]
        else None
    )

    kanji = fe_dir / "kanji0.ttf"
    hangul = fe_dir / "hangul0.ttf"
    non_kanji = fe_dir / "non-kanji0.ttf"
    ws = work_dir / "ws0.ttf"
    as_punct = work_dir / "as0.ttf"
    fe_misc = work_dir / "fe-misc0.ttf"
    pass1 = work_dir / "pass1.ttf"

    if not kanji.exists():
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
                "version": VERSION,
                "latinCfg": sarasa_latin_config(),
                **flags,
            },
        )
    return {
        "pass1": pass1,
        "kanji": kanji,
        "hangul": hangul,
        "dash_source_path": classical_override or shs_ttf,
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


def normalize_static_post_table(font: TTFont) -> dict[str, Any]:
    if "post" not in font:
        return {"static_post_format": None, "static_stored_glyph_names": 0}
    previous_format = float(font["post"].formatType)
    font["post"].formatType = 3.0
    font["post"].extraNames = []
    font["post"].mapping = {}
    return {
        "static_post_previous_format": previous_format,
        "static_post_format": 3.0,
        "static_stored_glyph_names": 0,
    }


def static_output_name(region: str, weight_name: str, italic: bool) -> str:
    prefix = static_file_prefix(region)
    if weight_name == "Regular":
        return f"{prefix}-Italic.ttf" if italic else f"{prefix}-Regular.ttf"
    return f"{prefix}-{weight_name}{'Italic' if italic else ''}.ttf"


def static_postprocess_cache_key(
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    hinted: bool,
) -> str:
    payload = {
        "kind": "static-postprocess",
        "version": STATIC_POSTPROCESS_VERSION,
        "project_version": VERSION,
        "region": check_region(region),
        "weight_name": weight_name,
        "weight_value": int(weight_value),
        "italic": bool(italic),
        "hinted": bool(hinted),
        "copyright": project_copyright(region),
        "license_description": PROJECT_LICENSE_DESCRIPTION,
        "license_url": PROJECT_LICENSE_URL,
        "vendor_id": OS2_VENDOR_ID,
        "codepage_range_1": OS2_CODEPAGE_RANGE_1,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def static_weight_output_paths(region: str, weight_name: str) -> list[tuple[Path, bool, bool]]:
    return [
        (static_dir(region, hinted) / static_output_name(region, weight_name, italic), hinted, italic)
        for hinted in [False, True]
        for italic in [False, True]
    ]


def static_weight_resume_status(region: str, stop: dict[str, Any]) -> tuple[bool, list[str]]:
    weight_name = str(stop["name"])
    weight_value = int(stop["value"])
    reasons: list[str] = []
    for path, hinted, italic in static_weight_output_paths(region, weight_name):
        label = f"{path.name} ({'hinted' if hinted else 'unhinted'})"
        if not path.exists():
            reasons.append(f"missing {label}")
            continue
        font: TTFont | None = None
        try:
            font = TTFont(path, lazy=True, recalcTimestamp=False)
            if "OS/2" not in font or font["OS/2"].usWeightClass != weight_value:
                reasons.append(f"{label}: wrong OS/2 weight")
            if "OS/2" not in font or font["OS/2"].achVendID != OS2_VENDOR_ID:
                reasons.append(f"{label}: wrong vendor")
            if "OS/2" not in font or int(font["OS/2"].ulCodePageRange1) != OS2_CODEPAGE_RANGE_1:
                reasons.append(f"{label}: wrong codepage range")
            version_name = font["name"].getDebugName(5) if "name" in font else None
            variant_label = "hinted" if hinted else "unhinted"
            if (
                not version_name
                or f"project {VERSION}" not in version_name
                or not version_name.endswith(f"; {variant_label}")
            ):
                reasons.append(f"{label}: wrong project version")
            unique_ids = {
                record.toUnicode()
                for record in font["name"].names
                if record.nameID == 3
            } if "name" in font else set()
            if len(unique_ids) != 1 or not next(iter(unique_ids), "").endswith(
                f";{variant_label}"
            ):
                reasons.append(f"{label}: wrong unique identifier")
            copyrights = {
                record.toUnicode()
                for record in font["name"].names
                if record.nameID == 0
            } if "name" in font else set()
            license_descriptions = {
                record.toUnicode()
                for record in font["name"].names
                if record.nameID == 13
            } if "name" in font else set()
            license_urls = {
                record.toUnicode()
                for record in font["name"].names
                if record.nameID == 14
            } if "name" in font else set()
            source_only_legal_ids = {
                record.nameID
                for record in font["name"].names
                if record.nameID in SOURCE_ONLY_LEGAL_NAME_IDS
            } if "name" in font else set()
            if (
                copyrights != {project_copyright(region)}
                or license_descriptions != {PROJECT_LICENSE_DESCRIPTION}
                or license_urls != {PROJECT_LICENSE_URL}
                or source_only_legal_ids
            ):
                reasons.append(f"{label}: incomplete legal names")
            mac_name_records = sum(
                record.platformID == 1 for record in font["name"].names
            ) if "name" in font else 0
            if mac_name_records:
                reasons.append(f"{label}: {mac_name_records} Macintosh name records")
            if "post" not in font or float(font["post"].formatType) != 3.0:
                reasons.append(f"{label}: post is not format 3")
            raw_flag_stats = raw_simple_glyph_flag_stats(font)
            if not raw_flag_stats["glyphs_checked"]:
                reasons.append(f"{label}: raw glyf flag audit checked no simple glyphs")
            if raw_flag_stats["malformed_glyphs"]:
                reasons.append(
                    f"{label}: {raw_flag_stats['malformed_glyphs']} malformed raw "
                    "simple-glyph flag streams"
                )
            invalid_overlap_flags = raw_flag_stats["invalid_explicit_overlap_flags"]
            if invalid_overlap_flags:
                reasons.append(
                    f"{label}: {invalid_overlap_flags} OTS-invalid explicit "
                    "OVERLAP_SIMPLE flags"
                )
            if "STAT" not in font or "fvar" in font or "gvar" in font:
                reasons.append(f"{label}: wrong static variation tables")
            hint_tables = any(tag in font for tag in ("fpgm", "prep", "cvt "))
            glyph_programs = int(getattr(font["maxp"], "maxSizeOfInstructions", 0)) > 0
            if hinted and (not hint_tables or not glyph_programs):
                reasons.append(f"{label}: missing hints")
            if not hinted and (hint_tables or glyph_programs):
                reasons.append(f"{label}: unexpected hints")
            if not hinted:
                if "maxp" not in font or int(font["maxp"].maxZones) != 1:
                    reasons.append(f"{label}: unhinted maxp.maxZones is not 1")
                gasp_ranges = dict(font["gasp"].gaspRange) if "gasp" in font else {}
                if not gasp_ranges or max(gasp_ranges) != 0xFFFF:
                    reasons.append(f"{label}: unhinted gasp lacks 0xFFFF sentinel")
            if not layout_has_feature(font, "GPOS", "chws"):
                reasons.append(f"{label}: missing GPOS chws")
            if not layout_has_feature(font, "GPOS", "vchw"):
                reasons.append(f"{label}: missing GPOS vchw")
            dash_status = upstream_dash_structure_status(font, region)
            if not dash_status["ok"]:
                reasons.append(
                    f"{label}: invalid two-em dash behavior: "
                    + "; ".join(dash_status["reasons"])
                )
            if "head" not in font or not math.isclose(
                font["head"].fontRevision,
                FONT_REVISION,
                abs_tol=1 / 65536,
            ):
                reasons.append(f"{label}: wrong head.fontRevision")
        except Exception as error:
            reasons.append(f"{label}: validation failed: {type(error).__name__}: {error}")
        finally:
            if font is not None:
                font.close()
    return not reasons, reasons


def skipped_static_weight_outputs(region: str, stop: dict[str, Any]) -> list[dict[str, Any]]:
    weight_name = str(stop["name"])
    weight_value = int(stop["value"])
    return [
        {
            "file": portable_report_path(path),
            "region": region,
            "weight": weight_name,
            "wght": weight_value,
            "italic": italic,
            "hinted_variant": hinted,
            "rebuilt": False,
            "resume_static_skipped": True,
            "resume_static_verified": True,
        }
        for path, hinted, italic in static_weight_output_paths(region, weight_name)
    ]


def sfnt_table_hashes(
    path: Path,
    excluded_tags: set[str] | None = None,
) -> dict[str, str]:
    excluded = set(excluded_tags or ())
    font = TTFont(path, lazy=True, recalcTimestamp=False)
    try:
        return {
            tag: hashlib.sha256(bytes(font.reader[tag])).hexdigest()
            for tag in sorted(font.reader.tables)
            if tag not in excluded
        }
    finally:
        font.close()


def refresh_static_finalization_font(
    path: Path,
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    hinted: bool,
) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    intentionally_changed = {"head", "maxp", "OS/2", "name", "gasp", "GSUB", "VORG"}
    before = sfnt_table_hashes(path, intentionally_changed)
    tmp_path = path.with_name(path.name + ".finalization.tmp")
    tmp_path.unlink(missing_ok=True)
    font = TTFont(path, lazy=True, recalcBBoxes=False, recalcTimestamp=False)
    font.recalcBBoxes = False
    try:
        update_static_names(
            font,
            region,
            weight_name,
            weight_value,
            italic,
            hinted,
        )
        update_os2_sarasa_metadata(font)
        raster_report = normalize_static_raster_metadata(font, hinted)
        revision_report = update_head_project_revision(font)
        mac_report = remove_mac_name_records(font)
        ellipsis_before = cjk_ellipsis_structure_status(font)
        ellipsis_report = apply_cjk_ellipsis_behavior(font)
        ellipsis_report.update(apply_default_regional_punctuation(font, region))
        vertical_report = normalize_static_vertical_origin(font)
        colon_report = add_digit_colon_feature(font)
        font.save(tmp_path, reorderTables=True)
    finally:
        font.close()
    try:
        after = sfnt_table_hashes(tmp_path, intentionally_changed)
        if before != after:
            changed = sorted(
                tag
                for tag in set(before) | set(after)
                if before.get(tag) != after.get(tag)
            )
            raise RuntimeError(
                "finalization-only refresh changed protected SFNT tables: "
                + ", ".join(changed)
            )
        verified = TTFont(tmp_path, lazy=False, recalcTimestamp=False)
        try:
            ellipsis_after = cjk_ellipsis_structure_status(verified)
            layout_after = upstream_dash_structure_status(verified, region)
        finally:
            verified.close()
        if not ellipsis_after["ok"]:
            raise RuntimeError(
                "finalization-only refresh failed CJK ellipsis validation: "
                + "; ".join(ellipsis_after["reasons"])
            )
        if not layout_after["ok"]:
            raise RuntimeError(
                "finalization-only refresh failed punctuation layout validation: "
                + "; ".join(layout_after["reasons"])
            )
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return {
        "file": portable_report_path(path),
        "region": region,
        "weight": weight_name,
        "wght": weight_value,
        "italic": italic,
        "hinted_variant": hinted,
        "finalization_only_refresh": True,
        "protected_table_count": len(before),
        "cjk_ellipsis_before": ellipsis_before,
        "cjk_ellipsis_after": ellipsis_after,
        "punctuation_layout_after": layout_after,
        **ellipsis_report,
        **raster_report,
        **revision_report,
        **mac_report,
        **vertical_report,
        **colon_report,
    }


def normalize_static_vertical_origin(font: TTFont) -> dict[str, Any]:
    # VORG is defined for CFF/CFF2 only. Some shapers still read a VORG in a
    # TrueType font, overriding the origin derived from its final glyf/vmtx.
    removed = "glyf" in font and "fvar" not in font and "VORG" in font
    if removed:
        del font["VORG"]
    return {"static_vorg_removed": removed, "static_vertical_origin_source": "glyf/vmtx"}


def normalize_variable_vertical_origin(font: TTFont) -> dict[str, Any]:
    if "VORG" not in font:
        return {"variable_vorg_removed": False}
    weights = (200, 250, 300, 325, 350, 375, 400, 500, 600, 650, 700, 800, 900)
    source_hb = hb.Font(hb.Face(serialized_font_bytes(font)))
    order = font.getGlyphOrder()
    expected = {}
    expected_x = {}
    for weight in weights:
        source_hb.set_variations({"wght": weight})
        expected[weight] = [source_hb.get_glyph_v_origin(gid)[1] for gid in range(len(order))]
        expected_x[weight] = [ext.x_bearing if (ext := source_hb.get_glyph_extents(gid)) is not None else None for gid in range(len(order))]
    cleared = 0
    for glyph in font["glyf"].glyphs.values():
        glyph.expand(font["glyf"])
        for component in getattr(glyph, "components", []):
            if component.flags & glyf_table.USE_MY_METRICS:
                component.flags &= ~glyf_table.USE_MY_METRICS
                cleared += 1
    del font["VORG"]
    supports = advance_supports(font, [weight for weight in weights if weight != 400])
    added = 0
    for iteration in range(5):
        current_hb = hb.Font(hb.Face(serialized_font_bytes(font)))
        errors = {}
        for weight in weights:
            current_hb.set_variations({"wght": weight})
            for gid, target in enumerate(expected[weight]):
                difference = target - current_hb.get_glyph_v_origin(gid)[1]
                ext = current_hb.get_glyph_extents(gid)
                dx = ext.x_bearing - expected_x[weight][gid] if ext is not None and expected_x[weight][gid] is not None else 0
                if difference or dx:
                    errors.setdefault(gid, {})[weight] = (dx, difference)
        if not errors:
            return {"variable_vorg_removed": True, "vorgless_metric_rounds": iteration, "vorgless_phantom_corrections": added, "vorgless_use_my_metrics_removed": cleared, "vorgless_control_weights": list(weights), "vorgless_origin_mismatches": 0}
        if any(400 in changes for changes in errors.values()):
            raise RuntimeError("VORG 与默认 glyf/vmtx 原点冲突，不能只修改变化数据")
        if iteration == 4:
            raise RuntimeError(f"无 VORG 的竖排度量校准未收敛：{len(errors)} glyph；" + repr([(order[gid], changes, [component.flags for component in getattr(font['glyf'][order[gid]], 'components', [])]) for gid, changes in list(errors.items())[:8]]))
        # HarfBuzz's TrueType path uses gvar phantom points, not optional VVAR
        # TSB/origin maps. Give composites independent metrics and compensate
        # both phantom pairs equally; real outlines and advances stay intact.
        for gid, changes in errors.items():
            glyph = order[gid]
            for weight, (dx, correction) in changes.items():
                coordinates = [(0, 0)] * gvar_coordinate_count(font, glyph)
                coordinates[-4:] = [(dx, 0), (dx, 0), (0, correction), (0, correction)]
                font["gvar"].variations.setdefault(glyph, []).append(TupleVariation({"wght": supports[weight]}, coordinates))
        added += sum(len(changes) for changes in errors.values())
        log_step(f"VF 无 VORG 竖排校准 {iteration + 1}：{len(errors)} glyph")
    raise AssertionError("unreachable")


def normalize_cross_engine_metrics(font: TTFont) -> dict[str, Any]:
    """Use xMin == LSB with a stationary left phantom, and gvar vertical metrics.

    FreeType ignores horizontal/vertical phantom deltas when HVAR/VVAR exists.
    Translate the horizontal coordinate frame, including component offsets,
    rather than compensating only one engine's phantom-point interpretation.
    VVAR cannot describe varying TrueType origins to FreeType, so vertical
    metrics have one authoritative representation in gvar in the final font.
    """
    if "gvar" not in font:
        return {"cross_engine_metric_normalization": False}
    if "VVAR" not in font and "VORG" not in font and not any(item.coordinates[-4] and item.coordinates[-4][0] for items in font["gvar"].variations.values() for item in items):
        return {"cross_engine_metric_normalization": True, "cross_engine_metrics_already_canonical": True, "vertical_metrics_source": "gvar"}
    weights = (200, 250, 300, 325, 350, 375, 400, 500, 600, 650, 700, 800, 900)
    def snapshot() -> dict[int, list[tuple]]:
        runtime = hb.Font(hb.Face(serialized_font_bytes(font)))
        result = {}
        for weight in weights:
            runtime.set_variations({"wght": weight})
            result[weight] = [
                (runtime.get_glyph_extents(gid), runtime.get_glyph_h_advance(gid), runtime.get_glyph_v_advance(gid), runtime.get_glyph_v_origin(gid))
                for gid in range(len(font.getGlyphOrder()))
            ]
        return result
    expected = snapshot()
    materialized = materialize_gvar_variations(font)
    if materialized["gvar_coordinate_length_mismatches"]:
        raise RuntimeError("跨引擎度量规范化前的 gvar 坐标数量不匹配")
    mismatches = lsb_xmin_mismatches(font)
    if mismatches:
        raise RuntimeError("跨引擎度量规范化要求默认 xMin 与 LSB 一致")
    variations = font["gvar"].variations
    origins = {
        name: [(copy.deepcopy(item.axes), item.coordinates[-4][0]) for item in items if item.coordinates[-4] and item.coordinates[-4][0]]
        for name, items in variations.items()
    }
    translated = compensated = 0
    for name in font.getGlyphOrder():
        glyph = font["glyf"][name]
        items = variations.setdefault(name, [])
        for item in items:
            dx = (item.coordinates[-4] or (0, 0))[0]
            if not dx:
                continue
            if glyph.isComposite():
                item.coordinates[:-4] = [((point or (0, 0))[0] - dx, (point or (0, 0))[1]) for point in item.coordinates[:-4]]
            else:
                # Adding a constant to every explicit delta commutes with IUP.
                # Preserve inferred fractional deltas; materializing them as
                # integer gvar coordinates subtly changes curves (e.g. dots).
                item.coordinates[:-4] = [None if point is None else (point[0] - dx, point[1]) for point in item.coordinates[:-4]]
                start = 0
                for end in getattr(glyph, "endPtsOfContours", []):
                    if all(point is None for point in item.coordinates[start:end + 1]):
                        item.coordinates[start] = (-dx, 0)
                    start = end + 1
            item.coordinates[-4] = (0, 0)
            item.coordinates[-3] = ((item.coordinates[-3] or (0, 0))[0] - dx, 0)
            translated += 1
        if not glyph.isComposite():
            continue
        by_support = {tuple(sorted(item.axes.items())): item for item in items}
        for index, component in enumerate(glyph.components):
            _child, transform = component.getComponentInfo()
            for axes, dx in origins.get(component.glyphName, []):
                key = tuple(sorted(axes.items()))
                if key not in by_support:
                    item = TupleVariation(axes, [(0, 0)] * gvar_coordinate_count(font, name))
                    items.append(item)
                    by_support[key] = item
                item = by_support[key]
                x, y = item.coordinates[index] or (0, 0)
                item.coordinates[index] = (x + otRound(dx * transform[0]), y + otRound(dx * transform[1]))
                compensated += 1
    removed = "VVAR" in font
    if removed:
        del font["VVAR"]
    font["head"].flags |= 2
    actual = snapshot()
    changes = [(weight, font.getGlyphName(gid), before, after) for weight in weights for gid, (before, after) in enumerate(zip(expected[weight], actual[weight])) if before != after]
    if changes:
        raise RuntimeError(f"横向原点规范化改变了已有运行时轮廓边界或度量：{len(changes)}，{changes[:4]!r}")
    return {"cross_engine_metric_normalization": True, "horizontal_origin_tuples_translated": translated, "component_origin_tuples_compensated": compensated, "vertical_metrics_source": "gvar", "vvar_removed_for_shared_vertical_origin": removed, "runtime_metric_preservation_weights": list(weights), "runtime_metric_preservation_glyphs": len(font.getGlyphOrder()), "runtime_metric_preservation_mismatches": 0}



def refresh_variable_finalization_outputs(regions: list[str]) -> list[dict[str, Any]]:
    outputs = []
    for region in regions:
        for italic in (False, True):
            path = VARIABLE_DIR / variable_output_name(region, italic)
            allowed = {"GSUB", "name", "head", "VORG", "VVAR", "HVAR", "hmtx", "glyf", "loca", "gvar"}
            before = sfnt_table_hashes(path, allowed)
            pending = path.with_name(path.name + ".finalization.tmp")
            font = TTFont(path, lazy=True, recalcBBoxes=False, recalcTimestamp=False)
            try:
                geometry_before = {name: glyph_point_structure(font, name) for name in font.getGlyphOrder()}
                report = apply_default_regional_punctuation(font, region)
                report.update(normalize_variable_vertical_origin(font))
                if geometry_before != {name: glyph_point_structure(font, name) for name in font.getGlyphOrder()}:
                    raise RuntimeError("VF 最终化改变了真实轮廓坐标")
                report.update(add_digit_colon_feature(font))
                report.update(align_tabular_alternate_advances(font))
                report.update(normalize_cross_engine_metrics(font))
                update_vf_names(font, region, italic)
                remove_mac_name_records(font)
                update_head_project_revision(font)
                font.save(pending)
            finally:
                font.close()
            try:
                if sfnt_table_hashes(pending, allowed) != before:
                    raise RuntimeError("VF 最终化改变了受保护的轮廓、定位或度量表")
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
            outputs.append({"file": portable_report_path(path), "region": region, "italic": italic, "protected_tables": sorted(before), **report})
    return outputs


def refresh_static_finalization_outputs(regions: list[str]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for region in regions:
        for stop in SOURCE_HAN_WEIGHT_STOPS:
            weight_name = str(stop["name"])
            weight_value = int(stop["value"])
            for path, hinted, italic in static_weight_output_paths(region, weight_name):
                log_step(
                    f"static {region} {weight_name}{' Italic' if italic else ''} "
                    f"{'hinted' if hinted else 'unhinted'}: finalization-only refresh"
                )
                outputs.append(
                    refresh_static_finalization_font(
                        path,
                        region,
                        weight_name,
                        weight_value,
                        italic,
                        hinted,
                    )
                )
    return outputs


def postprocess_static_font(
    path: Path,
    region: str,
    weight_name: str,
    weight_value: int,
    italic: bool,
    hinted: bool,
    dash_source_path: Path,
) -> dict[str, Any]:
    font = TTFont(path, recalcBBoxes=False, recalcTimestamp=False)
    font.recalcBBoxes = False
    uses_official_glyph_baseline = static_uses_official_glyph_baseline(weight_name)
    source_value_reference = (
        None
        if uses_official_glyph_baseline
        else TTFont(path, recalcBBoxes=False, recalcTimestamp=False)
    )
    report: dict[str, Any] = {
        "static_postprocess_version": STATIC_POSTPROCESS_VERSION,
        "static_postprocess_cache_key": static_postprocess_cache_key(
            region,
            weight_name,
            weight_value,
            italic,
            hinted,
        ),
        "static_glyph_data_policy": (
            "official-sarasa-exact"
            if uses_official_glyph_baseline
            else "source-weight-extension"
        ),
    }
    try:
        reference_style = static_reference_style_name(weight_name, italic)
        reference_path = static_reference_font_path(region, weight_name, italic)
        report["static_reference_style"] = reference_style
        report["static_reference_path"] = str(reference_path)
        reference: TTFont | None = None
        if reference_path.exists():
            reference = TTFont(reference_path, recalcBBoxes=False, recalcTimestamp=False)
            try:
                report.update(restrict_cmap_to_reference(font, reference, PROPDIGITS_CODEPOINTS))
                if uses_official_glyph_baseline:
                    report.update(align_reference_advances(font, reference, PROPDIGITS_CODEPOINTS))
                    report.update(align_reference_hmtx_lsb(font, reference, PROPDIGITS_CODEPOINTS))
                    report.update(align_tnum_digit_targets(font, reference))
                    report.update(align_reference_vmtx(font, reference, PROPDIGITS_CODEPOINTS))
                else:
                    report.update(
                        {
                            "reference_advances_aligned": 0,
                            "reference_lsb_aligned": 0,
                            "tnum_digit_target_hmtx_aligned": 0,
                            "reference_vmtx_aligned": 0,
                            "extension_source_metrics_preserved": True,
                        }
                    )
                report.update(rebuild_gdef_from_reference(font, reference))
                if uses_official_glyph_baseline:
                    report.update(rebuild_vorg_from_reference(font, reference))
                else:
                    report["extension_source_vorg_preserved"] = True
                report.update(sync_sarasa_metadata_from_reference(font, reference))
            except Exception:
                reference.close()
                reference = None
                raise
        update_static_names(
            font,
            region,
            weight_name,
            weight_value,
            italic,
            hinted,
        )
        update_os2_sarasa_metadata(font)
        rebuild_static_stat(font, weight_name, weight_value, italic)
        report.update(drop_generated_extra_tables(font, keep_stat=True))
        report.update(apply_static_propdigits(font))
        if reference and uses_official_glyph_baseline:
            report.update(sync_static_glyf_from_reference(font, reference, PROPDIGITS_CODEPOINTS))
        elif reference:
            report["extension_source_glyf_preserved"] = True
        report.update(add_digit_colon_feature(font))
        if reference:
            report.update(align_layout_feature_template(font, reference, "GSUB"))
            report.update(align_layout_lookup_structure(font, reference, "GPOS"))
            report.update(align_layout_feature_template(font, reference, "GPOS", use_reference_lookup_indices=True))
            subset_to_current_cmap(font)
            report.update(align_layout_feature_template(font, reference, "GSUB"))
            report.update(align_layout_lookup_structure(font, reference, "GPOS"))
            report.update(align_layout_feature_template(font, reference, "GPOS", use_reference_lookup_indices=True))
            palt_reference = reference if uses_official_glyph_baseline else source_value_reference
            if palt_reference is None:
                raise RuntimeError("missing extension source palt reference")
            report.update(
                sync_gpos_single_pos_feature_values_from_reference(
                    font,
                    palt_reference,
                    {"palt"},
                )
            )
            report["static_palt_value_source"] = (
                "official-sarasa-exact"
                if uses_official_glyph_baseline
                else "source-weight-extension"
            )
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
        # fontTools' subsetter recalculates ulCodePageRange1; restore Sarasa's
        # explicit metadata only after the final cmap subset has completed.
        update_os2_sarasa_metadata(font)
        report.update(normalize_static_raster_metadata(font, hinted))
        report.update(normalize_static_post_table(font))
        report.update(update_head_project_revision(font))
        report.update(force_recompile_glyf(font))
        font.save(path, reorderTables=True)
    finally:
        if reference:
            reference.close()
        if source_value_reference is not None:
            source_value_reference.close()
        font.close()
    log_step(
        f"static {region} {weight_name}{' Italic' if italic else ''} "
        f"{'hinted' if hinted else 'unhinted'}: add Noto chws/vchw"
    )
    report.update(add_noto_contextual_spacing(path))
    font = TTFont(path, recalcBBoxes=False, recalcTimestamp=False)
    font.recalcBBoxes = False
    post_layout_reference: TTFont | None = None
    try:
        if uses_official_glyph_baseline and reference_path.exists():
            post_layout_reference = TTFont(
                reference_path,
                recalcBBoxes=False,
                recalcTimestamp=False,
            )
            report.update(
                sync_static_cmap_bboxes_from_reference(
                    font,
                    post_layout_reference,
                    PROPDIGITS_CODEPOINTS,
                )
            )
        dash_source = TTFont(
            dash_source_path,
            recalcBBoxes=False,
            recalcTimestamp=False,
        )
        try:
            report.update(
                apply_upstream_dash_behavior(
                    font,
                    region,
                    static_source=dash_source,
                    italic=italic,
                )
            )
        finally:
            dash_source.close()
        report.update(remove_mac_name_records(font))
        report.update(normalize_static_vertical_origin(font))
        report.update(apply_default_regional_punctuation(font, region))
        font.save(path, reorderTables=True)
    finally:
        if post_layout_reference is not None:
            post_layout_reference.close()
        font.close()
    return report


def prepare_static_pass1_derivatives(path: Path) -> dict[str, Any]:
    return {
        "pass1_digit_colon_feature_added": False,
        "pass1_digit_colon_source": "inter-calt",
    }


def prepare_static_style(
    region: str,
    stop: dict[str, Any],
    tmp_dir: Path,
    italic: bool,
    emit_output: bool = True,
    prepare_hints: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    weight_name = str(stop["name"])
    weight_value = int(stop["value"])
    tmp_dir.mkdir(parents=True, exist_ok=True)
    style_label = f"{region} {weight_name}{' Italic' if italic else ''}"
    log_step(f"static {style_label}: build Sarasa fragments")
    fragments = build_sarasa_static_fragments(region, weight_name, weight_value, italic, tmp_dir)
    pass1_derivative_report = prepare_static_pass1_derivatives(fragments["pass1"])

    unhinted_output: dict[str, Any] | None = None
    if emit_output:
        unhinted_tmp = tmp_dir / "unhinted" / region / static_output_name(region, weight_name, italic)
        unhinted_report_path = unhinted_tmp.with_suffix(".report.json")
        unhinted_tmp.parent.mkdir(parents=True, exist_ok=True)
        unhinted_path = static_dir(region, False) / static_output_name(region, weight_name, italic)
        unhinted_report = (
            json.loads(unhinted_report_path.read_text(encoding="utf-8"))
            if unhinted_tmp.exists() and unhinted_report_path.exists()
            else None
        )
        if (
            unhinted_report is not None
            and unhinted_report.get("static_postprocess_cache_key")
            == static_postprocess_cache_key(
                region,
                weight_name,
                weight_value,
                italic,
                False,
            )
        ):
            log_step(f"static {style_label}: reuse completed unhinted output")
        else:
            if unhinted_report is not None:
                log_step(f"static {style_label}: invalidate stale unhinted postprocess cache")
                unhinted_tmp.unlink(missing_ok=True)
                unhinted_report_path.unlink(missing_ok=True)
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
            unhinted_report = postprocess_static_font(
                unhinted_tmp,
                region,
                weight_name,
                weight_value,
                italic,
                False,
                Path(fragments["dash_source_path"]),
            )
            write_json_atomic(unhinted_report_path, unhinted_report)
        shutil.copy2(unhinted_tmp, unhinted_path)
        unhinted_output = {
            "file": portable_report_path(unhinted_path),
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

    if not prepare_hints:
        return {}, unhinted_output

    hinted_work = tmp_dir / "hinted" / region / f"{weight_name}{'Italic' if italic else ''}"
    hinted_work.mkdir(parents=True, exist_ok=True)
    pass1_hinted = hinted_work / "pass1.ttfautohint.ttf"
    pass1_hinted_report = hinted_work / "pass1.ttfautohint.report.json"
    if pass1_hinted.exists() and pass1_hinted_report.exists():
        log_step(f"static {style_label}: reuse ttfautohint pass1")
        hint_report = json.loads(pass1_hinted_report.read_text(encoding="utf-8"))
    else:
        log_step(f"static {style_label}: ttfautohint pass1")
        hint_report = hint_static_font(fragments["pass1"], pass1_hinted)
        write_json_atomic(pass1_hinted_report, hint_report)
    context = {
        "style_label": style_label,
        "region": region,
        "weight_name": weight_name,
        "weight_value": weight_value,
        "italic": italic,
        "emit_output": emit_output,
        "fragments": fragments,
        "pass1_derivative_report": pass1_derivative_report,
        "hint_report": hint_report,
        "pass1_hinted": pass1_hinted,
        "pass1_hint": hinted_work / "pass1.hint.gz",
        "pass1_instructed": hinted_work / "pass1.ttf",
        "hinted_tmp": hinted_work / static_output_name(region, weight_name, italic),
        "hinted_path": static_dir(region, True) / static_output_name(region, weight_name, italic),
    }
    return context, unhinted_output


def build_static_weight_group(
    regions: list[str],
    stop: dict[str, Any],
    tmp_dir: Path,
    hint_jobs: int,
) -> list[dict[str, Any]]:
    weight_name = str(stop["name"])
    output_regions = set(regions)
    contexts: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for region in REGION_ORDER:
        for italic in (False, True):
            context, unhinted_output = prepare_static_style(
                region,
                stop,
                tmp_dir,
                italic,
                emit_output=region in output_regions,
            )
            contexts.append(context)
            if unhinted_output is not None:
                outputs.append(unhinted_output)

    context_map = {
        (str(context["region"]), bool(context["italic"])): context
        for context in contexts
    }
    log_step(
        f"static {weight_name}: prepare full Sarasa pass1 environment "
        f"({len(SARASA_HINT_FAMILY_ORDER) * len(REGION_ORDER) * 2} fonts, "
        f"prep jobs={SARASA_HINT_PREP_JOBS})"
    )
    hint_fragments = {
        key: {
            **value["fragments"],
            "_pass1_hinted": value["pass1_hinted"],
            "_ttfautohint_report": value["hint_report"],
        }
        for key, value in context_map.items()
    }
    pass1_environment = build_sarasa_hint_environment(
        regions,
        weight_name,
        int(stop["value"]),
        tmp_dir,
        hint_fragments,
    )
    pass1_jobs = [
        (Path(item["hinted"]), Path(item["hint"]), weight_name)
        for item in pass1_environment
    ]

    fe_entries: dict[str, dict[str, Any]] = {}
    for region in REGION_ORDER:
        context = next(item for item in contexts if item["region"] == region)
        fragments = context["fragments"]
        fe_work = tmp_dir / "hinted-fe" / region
        fe_work.mkdir(parents=True, exist_ok=True)
        hani_hint = fe_work / "hani.hint.gz"
        hang_hint = fe_work / "hang.hint.gz"
        fe_entries[region] = {
            "kanji": fragments["kanji"],
            "hangul": fragments["hangul"],
            "hani_hint": hani_hint,
            "hang_hint": hang_hint,
            "hani_out": fe_work / "hani.ttf",
            "hang_out": fe_work / "hang.ttf",
        }

    hani_jobs = [
        (Path(fe_entries[region]["kanji"]), Path(fe_entries[region]["hani_hint"]), weight_name)
        for region in REGION_ORDER
    ]
    hang_jobs = [
        (Path(fe_entries[region]["hangul"]), Path(fe_entries[region]["hang_hint"]), weight_name)
        for region in REGION_ORDER
    ]
    fe_jobs = [*hani_jobs, *hang_jobs]
    input_cache_key = static_hint_work_key(weight_name)
    pass1_cache_key = static_hint_group_cache_key(weight_name, "pass1", pass1_jobs)
    pass1_cache_result = restore_static_hint_group_cache(
        pass1_cache_key, weight_name, pass1_jobs
    )
    if pass1_cache_result is None:
        log_step(
            f"static {weight_name}: Chlorophytum full pass1 group ({len(pass1_jobs)} fonts)"
        )
        pass1_hint_reports = chlorophytum_generate_static_hints(
            pass1_jobs,
            tmp_dir / "hint-data" / "pass1",
            hint_jobs=hint_jobs,
        )
        store_static_hint_group_cache(
            pass1_cache_key,
            weight_name,
            pass1_jobs,
            pass1_hint_reports,
        )
    else:
        pass1_cache_key, pass1_hint_reports = pass1_cache_result
        log_step(f"static {weight_name}: cached full pass1 hint group hit")

    fe_cache_key = static_hint_group_cache_key(weight_name, "fe", fe_jobs)
    fe_cache_result = restore_static_hint_group_cache(
        fe_cache_key, weight_name, fe_jobs
    )
    if fe_cache_result is None:
        log_step(
            f"static {weight_name}: Chlorophytum full kanji/hangul group "
            f"({len(fe_jobs)} fonts)"
        )
        fe_hint_reports = chlorophytum_generate_static_hints(
            fe_jobs,
            tmp_dir / "hint-data" / "fe",
            hint_jobs=hint_jobs,
        )
        store_static_hint_group_cache(
            fe_cache_key,
            weight_name,
            fe_jobs,
            fe_hint_reports,
        )
    else:
        fe_cache_key, fe_hint_reports = fe_cache_result
        log_step(f"static {weight_name}: cached full kanji/hangul hint group hit")

    instruct_jobs: list[tuple[Path, Path, Path, str]] = [
        (
            Path(item["hinted"]),
            Path(item["hint"]),
            Path(item["output"]),
            weight_name,
        )
        for item in pass1_environment
    ]
    instruct_jobs.extend(
        (
            fe_entries[region]["kanji"],
            fe_entries[region]["hani_hint"],
            fe_entries[region]["hani_out"],
            weight_name,
        )
        for region in REGION_ORDER
    )
    instruct_jobs.extend(
        (
            fe_entries[region]["hangul"],
            fe_entries[region]["hang_hint"],
            fe_entries[region]["hang_out"],
            weight_name,
        )
        for region in REGION_ORDER
    )
    log_step(f"static {weight_name}: Chlorophytum unified instruct ({len(instruct_jobs)} fonts)")
    instruct_reports = chlorophytum_instruct_static_fonts(instruct_jobs)

    environment_map = {
        (str(item["family"]), str(item["region"]), bool(item["italic"])): item
        for item in pass1_environment
    }

    for context in contexts:
        region = str(context["region"])
        italic = bool(context["italic"])
        style_label = str(context["style_label"])
        entry = fe_entries[region]
        ui_environment = environment_map[("Ui", region, italic)]
        context["pass1_instructed"] = Path(ui_environment["output"])
        if not context["emit_output"]:
            continue
        log_step(f"static {style_label}: compose hinted pass2")
        build_sarasa_pass2(
            context["pass1_instructed"],
            entry["hani_out"],
            entry["hang_out"],
            context["hinted_tmp"],
            italic,
            tmp_dir,
        )
        log_step(f"static {style_label}: postprocess hinted")
        hinted_postprocess = postprocess_static_font(
            context["hinted_tmp"],
            region,
            weight_name,
            int(context["weight_value"]),
            italic,
            True,
            Path(context["fragments"]["dash_source_path"]),
        )
        shutil.copy2(context["hinted_tmp"], context["hinted_path"])
        pass1_report = {
            **pass1_hint_reports[Path(ui_environment["hint"]).resolve()],
            **instruct_reports[context["pass1_instructed"].resolve()],
            "chlorophytum_full_pass1_cache_key": pass1_cache_key,
            "chlorophytum_full_fe_cache_key": fe_cache_key,
            "chlorophytum_full_group_input_cache_key": input_cache_key,
            "chlorophytum_full_pass1_group_size": len(pass1_jobs),
            "chlorophytum_full_fe_group_size": len(fe_jobs),
        }
        fe_report = {
            "hani": {
                **fe_hint_reports[entry["hani_hint"].resolve()],
                **instruct_reports[entry["hani_out"].resolve()],
            },
            "hang": {
                **fe_hint_reports[entry["hang_hint"].resolve()],
                **instruct_reports[entry["hang_out"].resolve()],
            },
        }
        fragments = context["fragments"]
        outputs.append(
            {
                "file": portable_report_path(context["hinted_path"]),
                "region": region,
                "weight": weight_name,
                "wght": int(context["weight_value"]),
                "italic": italic,
                "hinted_variant": True,
                "source_static_build": "sarasa-pass1-kanji-hangul-pass2",
                **{k: v for k, v in fragments.items() if isinstance(v, str)},
                **context["pass1_derivative_report"],
                **context["hint_report"],
                "hinted": True,
                "hint_tool": "ttfautohint-plus-chlorophytum",
                "chlorophytum_hinted": True,
                "pass1_chlorophytum": pass1_report,
                "fe_chlorophytum": fe_report,
                **hinted_postprocess,
            }
        )
    return outputs


def build_unhinted_fonts(regions: list[str], *, only_missing: bool = False) -> list[dict[str, Any]]:
    outputs = []
    for region in regions:
        static_dir(region, False).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sarasa-unhinted-") as tmp_name:
        for stop in SOURCE_HAN_WEIGHT_STOPS:
            for region in regions:
                for italic in (False, True):
                    path = static_dir(region, False) / static_output_name(region, str(stop["name"]), italic)
                    if only_missing and path.exists():
                        continue
                    _context, output = prepare_static_style(
                        region, stop, Path(tmp_name) / str(stop["name"]), italic,
                        prepare_hints=False,
                    )
                    outputs.append(output)
    return outputs


def build_static_fonts(
    regions: list[str],
    resume: bool = False,
    force_weights: set[str] | None = None,
) -> list[dict[str, Any]]:
    force_weights = set(force_weights or ())
    log_step("static: prepare output directories")
    for region in regions:
        expected_names = {
            static_output_name(region, str(stop["name"]), italic)
            for stop in SOURCE_HAN_WEIGHT_STOPS
            for italic in (False, True)
        }
        for out_dir in [static_dir(region, True), static_dir(region, False)]:
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "LICENSE", out_dir / "LICENSE-Sarasa-Gothic.txt")
            for path in out_dir.glob(f"{static_file_prefix(region)}-*.ttf"):
                if not resume or path.name not in expected_names:
                    path.unlink()

    outputs: list[dict[str, Any]] = []
    hint_jobs = SARASA_HINT_JOBS
    log_step(
        f"static: Chlorophytum jobs={hint_jobs}, "
        f"hint environment prep jobs={SARASA_HINT_PREP_JOBS}"
    )
    weight_order = {str(stop["name"]): index for index, stop in enumerate(SOURCE_HAN_WEIGHT_STOPS)}
    region_order = {region: index for index, region in enumerate(regions)}
    with tempfile.TemporaryDirectory(prefix="sarasa-static-") as tmp_dir_raw:
        fallback_tmp_dir = Path(tmp_dir_raw)
        for stop in SOURCE_HAN_WEIGHT_STOPS:
            weight_name = str(stop["name"])
            regions_to_build: list[str] = []
            for region in regions:
                if resume and weight_name not in force_weights:
                    complete, reasons = static_weight_resume_status(region, stop)
                    if complete:
                        log_step(f"static {region} {weight_name}: verified complete; skip")
                        outputs.extend(skipped_static_weight_outputs(region, stop))
                        continue
                    log_step(
                        f"static {region} {weight_name}: resume validation requires rebuild: "
                        + "; ".join(reasons)
                    )
                regions_to_build.append(region)
            if regions_to_build:
                if os.environ.get("SARASA_DISABLE_BUILD_CACHE") == "1":
                    weight_tmp_dir = fallback_tmp_dir / "weights" / weight_name
                else:
                    work_key, weight_tmp_dir = static_hint_work_dir(weight_name)
                    weight_tmp_dir.mkdir(parents=True, exist_ok=True)
                    (weight_tmp_dir / "manifest.json").write_text(
                        json.dumps(
                            {
                                "key": work_key,
                                "weight": weight_name,
                                "version": STATIC_HINT_WORK_VERSION,
                                "hint_recipe_sha256": static_hint_recipe_fingerprint(),
                                "created_by": "tools/build_sarasa_ui_propdigits_sc.py",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                outputs.extend(
                    build_static_weight_group(
                        regions_to_build,
                        stop,
                        weight_tmp_dir,
                        hint_jobs,
                    )
                )
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


def shape_position_signature(
    font_data: bytes,
    glyph_order: list[str],
    text: str,
    features: dict[str, bool],
    direction: str,
    variations: dict[str, float] | None = None,
) -> list[dict[str, int | str]] | None:
    try:
        import uharfbuzz as hb
    except ImportError:
        return None
    face = hb.Face(font_data)
    hb_font = hb.Font(face)
    hb_font.scale = (face.upem, face.upem)
    if variations:
        hb_font.set_variations(variations)
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    buffer.script = "Hani"
    buffer.language = "ZHS"
    buffer.direction = direction
    hb.shape(hb_font, buffer, features)
    return [
        {
            "glyph": glyph_order[info.codepoint],
            "cluster": info.cluster,
            "x_advance": position.x_advance,
            "y_advance": position.y_advance,
            "x_offset": position.x_offset,
            "y_offset": position.y_offset,
        }
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions)
    ]


def contextual_spacing_shape_samples(path: Path, font: TTFont) -> dict[str, Any]:
    font_data = path.read_bytes()
    glyph_order = font.getGlyphOrder()
    text = "（（天地））"
    return {
        "text": text,
        "horizontal_off": shape_position_signature(
            font_data, glyph_order, text, {"chws": False}, "ltr"
        ),
        "horizontal_chws": shape_position_signature(
            font_data, glyph_order, text, {"chws": True}, "ltr"
        ),
        "vertical_off": shape_position_signature(
            font_data, glyph_order, text, {"vchw": False, "vert": True}, "ttb"
        ),
        "vertical_vchw": shape_position_signature(
            font_data, glyph_order, text, {"vchw": True, "vert": True}, "ttb"
        ),
    }


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
            "file": portable_report_path(path),
            "size": path.stat().st_size,
            "names": {
                "family": font_name(font, 1),
                "subfamily": font_name(font, 2),
                "full": font_name(font, 4),
                "version": font_name(font, 5),
                "postscript": font_name(font, 6),
                "typographic_family": font_name(font, 16),
                "typographic_subfamily": font_name(font, 17),
            },
            "head_font_revision": float(font["head"].fontRevision),
            "glyph_count": len(font.getGlyphOrder()),
            "post_format": font["post"].formatType if "post" in font else None,
            "underline_thickness": font["post"].underlineThickness if "post" in font else None,
            "underline_position": font["post"].underlinePosition if "post" in font else None,
            "has_mvar": "MVAR" in font,
            "digit_widths_u0030_to_u0039": digits,
            "key_symbol_widths": key_widths,
            "has_tnum": has_feature(font, "tnum"),
            "has_pnum": has_feature(font, "pnum"),
            "has_digit_colon_calt": has_feature(font, "calt"),
            "has_chws": layout_has_feature(font, "GPOS", "chws"),
            "has_vchw": layout_has_feature(font, "GPOS", "vchw"),
            "contextual_spacing_shapes": contextual_spacing_shape_samples(path, font),
            "has_hints": any(tag in font for tag in ("fpgm", "prep", "cvt ")),
            "glyf_overlap_simple_flags": count_simple_glyph_overlap_flags(font),
            "glyf_ots_invalid_explicit_overlap_flags": (
                count_ots_invalid_simple_overlap_flags(font)
            ),
            "mac_name_records": sum(
                record.platformID == 1 for record in font["name"].names
            ) if "name" in font else 0,
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
    variant = "hinted" if hinted else "unhinted"
    source = "Source Han K 与 Shanggu Sans TC 1.028" if region == "CL" else f"Source Han Sans {region}"
    default = "未提供语言标记时使用对应地区的全宽标点；明确的 Latn/en 保留英文省略号路径。KOR 单破折号保留地区特例。"
    return f"""Sarasa Ui PropDigits {region} {VERSION}（{variant}）

本包包含 200 ExtraLight、300 Light、400 Regular、600 SemiBold、700 Bold、900 Heavy
及对应 Italic，共 12 个静态 TTF。350 仅是 VF 隐藏锚点，不提供静态样式。

来源为 {source} 与 Inter 4.1，按 Sarasa 1.0.40 的 pass1/kanji/hangul/pass2
静态流程构建，不从 VF 实例化。600 配对 Source Han 500 和 Inter 600。
CL 的公开 cmap/layout 限于 Sarasa Ui CL 边界。

默认 ASCII 数字为比例宽；tnum 切换等宽，pnum 恢复比例宽。
冒号复用 Inter 的上下文规则，在 tnum 之前执行；1:2、1:、:2 上浮，
1:a、a:2、a:b 保持原位。tnum 与 zero 可同时启用。{default}
破折号、省略号和竖排沿用对应 Source Han/Shanggu 字形，保持
ccmp → locl → vert/vrt2 顺序。中文双省略号为两个居中 glyph，共 2em；
中文双连、三连破折号分别为 2em、3em。CL 破折号全局保留 Shanggu 全宽形式。

chws/vchw 来自 Noto CJK 交付后处理；是否自动启用取决于实际应用。
静态竖排原点由最终 glyf/vmtx 决定，移除不适用于 TrueType 的 VORG。
post 使用 format 3，不保存虚构 glyph 名称。命名和布局修订不会重新 hint。

请阅读包根目录的中文 README.md、NOTICE.md 和 LICENSE.txt。
完整版本的主审计、OTS、分地区 FontBakery、视觉检查与 21 包校验报告见仓库 reports。
这些字体按 SIL Open Font License 1.1 分发，是修改版字体，不是任何上游的官方发布。
"""



def write_static_readme(regions: list[str], variants: tuple[bool, ...] = (True, False)) -> None:
    for region in regions:
        for hinted in variants:
            directory = static_dir(region, hinted)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "README.txt").write_text(static_readme_text(region, hinted), encoding="utf-8", newline="\n")


def portable_report_path(path: Path) -> str:
    try:
        return Path(os.path.relpath(path, ROOT)).as_posix()
    except ValueError:
        return f"<external>/{path.name}"


def sanitize_report_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_report_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_report_data(item) for item in value]
    if isinstance(value, Path):
        return portable_report_path(value)
    if isinstance(value, str) and Path(value).is_absolute():
        return portable_report_path(Path(value))
    if isinstance(value, str):
        replacements = (
            (str(ROOT.resolve()), "."),
            (ROOT.resolve().as_posix(), "."),
            (str(WORK_ROOT.resolve()), "<work>"),
            (WORK_ROOT.resolve().as_posix(), "<work>"),
            (str(Path.home().resolve()), "~"),
            (Path.home().resolve().as_posix(), "~"),
        )
        for source, replacement in replacements:
            value = value.replace(source, replacement)
        return value
    return value


def assert_portable_report_text(text: str) -> None:
    patterns = (
        r"(?i)(?:(?<![a-z])[a-z]:[\\/]|/users/|\\users\\)[^\"\r\n]*",
        r"(?i)(?<![a-z0-9:])/(?:home/[^/]+|root|mnt/[a-z])/(?:[^\"\r\n]*)",
    )
    leaks = sorted({match for pattern in patterns for match in re.findall(pattern, text)})
    if leaks:
        raise ValueError(f"report contains local absolute paths: {leaks[:8]}")


def write_reports(build_report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    build_text = json.dumps(sanitize_report_data(build_report), ensure_ascii=False, indent=2)
    assert_portable_report_text(build_text)
    (REPORT_DIR / "Sarasa-Ui-PropDigits-report.json").write_text(
        build_text + "\n",
        encoding="utf-8",
        newline="\n",
    )
    legacy_report = REPORT_DIR / "Sarasa-Ui-VF-PropDigits-SC-report.json"
    if legacy_report.exists():
        legacy_report.unlink()

    font_paths = (
        sorted(VARIABLE_DIR.glob("*.ttf"))
        + sorted(STATIC_ROOT.glob(f"SarasaUiPropDigits*-TTF-{VERSION}/*.ttf"))
        + sorted(STATIC_ROOT.glob(f"SarasaUiPropDigits*-TTF-Unhinted-{VERSION}/*.ttf"))
    )
    inspection = {
        "title": "Sarasa Ui VF PropDigits / Sarasa Ui PropDigits 多地区字体检查",
        "note": "由 tools/build_sarasa_ui_propdigits_sc.py 使用 fontTools 生成。",
        "fonts": [inspect_font(path) for path in font_paths],
    }
    inspection_text = json.dumps(
        sanitize_report_data(inspection),
        ensure_ascii=False,
        indent=2,
    )
    assert_portable_report_text(inspection_text)
    (REPORT_DIR / "font-inspection.json").write_text(
        inspection_text + "\n",
        encoding="utf-8",
        newline="\n",
    )


def existing_variable_outputs() -> list[dict[str, Any]]:
    return [
        {"file": portable_report_path(path), "rebuilt": False}
        for path in sorted(VARIABLE_DIR.glob("*.ttf"))
    ]


def variable_two_em_dash_axis_status(
    path: Path,
    region: str,
    italic: bool,
) -> dict[str, Any]:
    try:
        import uharfbuzz as hb
    except ImportError as error:
        raise RuntimeError("variable dash axis validation requires uharfbuzz") from error

    target_data = path.read_bytes()
    target_face = hb.Face(target_data)
    target_font = hb.Font(target_face)
    target_font.scale = (target_face.upem, target_face.upem)
    upem = int(target_face.upem)
    source_path = classical_vf_override_path(region) or source_han_vf_path(region)
    source_face = hb.Face(source_path.read_bytes())
    source_font = hb.Font(source_face)
    source_font.scale = (source_face.upem, source_face.upem)

    def shape(
        hb_font: Any,
        text: str,
        language: str,
        direction: str,
        features: dict[str, bool] | None = None,
    ) -> tuple[list[int], tuple[int, int], list[tuple[int, int, int, int] | None]]:
        buffer = hb.Buffer()
        buffer.add_str(text)
        buffer.script = "Latn" if language == "en" else "Hani"
        buffer.language = language
        buffer.direction = direction
        hb.shape(hb_font, buffer, features or {})
        glyph_ids = [int(info.codepoint) for info in buffer.glyph_infos]
        return (
            glyph_ids,
            (
                sum(int(position.x_advance) for position in buffer.glyph_positions),
                sum(int(position.y_advance) for position in buffer.glyph_positions),
            ),
            [
                None
                if (extent := hb_font.get_glyph_extents(glyph_id)) is None
                else (
                    int(extent.x_bearing),
                    int(extent.y_bearing),
                    int(extent.width),
                    int(extent.height),
                )
                for glyph_id in glyph_ids
            ],
        )

    failures: list[dict[str, Any]] = []

    def fail(weight: float, case: str, actual: Any, expected: Any) -> None:
        if len(failures) < 64:
            failures.append(
                {
                    "wght": weight,
                    "case": case,
                    "actual": actual,
                    "expected": expected,
                }
            )

    cjk_languages = ["zh-Hans", "zh-Hant", "zh-HK", "ja", "ko"]
    locations_checked = 0
    shapes_checked = 0
    previous_default_advances: tuple[int, int] | None = None
    for half_step in range(400, 1801):
        weight = half_step / 2
        locations_checked += 1
        target_font.set_variations({"wght": weight})
        default_pair = shape(target_font, "——", "en", "ltr")
        default_triple = shape(target_font, "———", "en", "ltr")
        english_ellipsis_single = shape(target_font, "…", "en", "ltr")
        english_ellipsis = shape(target_font, "……", "en", "ltr")
        shapes_checked += 4
        if len(default_pair[0]) != 1 or len(default_triple[0]) != 1:
            fail(
                weight,
                "default-ccmp-glyph-count",
                [len(default_pair[0]), len(default_triple[0])],
                [1, 1],
            )
        default_advances = (default_pair[1][0], default_triple[1][0])
        if region == "CL" and default_advances != (2 * upem, 3 * upem):
            fail(weight, "CL-default-advance", default_advances, (2 * upem, 3 * upem))
        if previous_default_advances and any(
            current < previous
            for current, previous in zip(default_advances, previous_default_advances)
        ):
            fail(
                weight,
                "default-advance-monotonicity",
                default_advances,
                previous_default_advances,
            )
        previous_default_advances = default_advances

        english_ellipsis_ok = (
            len(english_ellipsis_single[0]) == 1
            and len(english_ellipsis[0]) == 2
            and english_ellipsis[0][0] == english_ellipsis[0][1]
            and english_ellipsis[0][0] == english_ellipsis_single[0][0]
            and english_ellipsis[1]
            == (
                2 * english_ellipsis_single[1][0],
                2 * english_ellipsis_single[1][1],
            )
            and english_ellipsis[2][0] is not None
            and english_ellipsis[2][1] is not None
        )
        if not english_ellipsis_ok:
            fail(
                weight,
                "ellipsis-en-proportional-pair",
                english_ellipsis,
                "two identical lower proportional glyphs",
            )

        for language in cjk_languages:
            horizontal = shape(target_font, "——", language, "ltr")
            vertical = shape(target_font, "——", language, "ttb")
            vrt2_only = shape(
                target_font,
                "——",
                language,
                "ttb",
                {
                    "ccmp": True,
                    "locl": True,
                    "vert": False,
                    "vrt2": True,
                    "calt": False,
                },
            )
            cjk_ellipsis = shape(target_font, "……", language, "ltr")
            cjk_ellipsis_reference = shape(target_font, "⋯⋯", language, "ltr")
            vertical_ellipsis = shape(target_font, "……", language, "ttb")
            vertical_ellipsis_reference = shape(
                target_font,
                "⋯⋯",
                language,
                "ttb",
            )
            shapes_checked += 7
            if len(horizontal[0]) != 1 or horizontal[1] != (2 * upem, 0):
                fail(
                    weight,
                    f"{language}-horizontal",
                    [horizontal[0], horizontal[1]],
                    ["one glyph", (2 * upem, 0)],
                )
            if len(vertical[0]) != 1 or vertical[1] != (0, -2 * upem):
                fail(
                    weight,
                    f"{language}-vertical",
                    [vertical[0], vertical[1]],
                    ["one glyph", (0, -2 * upem)],
                )
            vrt2_pattern_ok = len(vrt2_only[0]) == 1 or (
                len(vrt2_only[0]) == 2
                and vrt2_only[0][0] == vrt2_only[0][1]
            )
            if not vrt2_pattern_ok or vrt2_only[1] != (0, -2 * upem):
                fail(
                    weight,
                    f"{language}-vrt2-only",
                    [vrt2_only[0], vrt2_only[1]],
                    ["one glyph or Source Han's identical pair", (0, -2 * upem)],
                )
            cjk_ellipsis_ok = (
                cjk_ellipsis == cjk_ellipsis_reference
                and len(cjk_ellipsis[0]) == 2
                and cjk_ellipsis[0][0] == cjk_ellipsis[0][1]
                and cjk_ellipsis[1] == (2 * upem, 0)
            )
            if not cjk_ellipsis_ok:
                fail(
                    weight,
                    f"{language}-ellipsis-horizontal",
                    cjk_ellipsis,
                    cjk_ellipsis_reference,
                )
            if (
                vertical_ellipsis != vertical_ellipsis_reference
                or len(vertical_ellipsis[0]) != 2
                or vertical_ellipsis[0][0] != vertical_ellipsis[0][1]
                or vertical_ellipsis[1] != (0, -2 * upem)
            ):
                fail(
                    weight,
                    f"{language}-ellipsis-vertical",
                    vertical_ellipsis,
                    vertical_ellipsis_reference,
                )
            if english_ellipsis_ok and cjk_ellipsis[2][0] is not None:
                english_y = int(english_ellipsis[2][0][1])
                cjk_y = int(cjk_ellipsis[2][0][1])
                if english_y >= cjk_y:
                    fail(
                        weight,
                        f"{language}-ellipsis-vertical-position",
                        [english_y, cjk_y],
                        "English U+2026 lower than CJK U+2026",
                    )
            else:
                fail(
                    weight,
                    f"{language}-ellipsis-vertical-position",
                    [english_ellipsis[2], cjk_ellipsis[2]],
                    "measurable English and CJK U+2026 extents",
                )

    source_parity: dict[str, Any] = {}
    for public_weight, internal_weight in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT.items():
        target_font.set_variations({"wght": public_weight})
        source_font.set_variations({"wght": internal_weight})
        cases = {
            "default-pair": ("——", "en", "ltr"),
            "default-triple": ("———", "en", "ltr"),
            "zh-Hans-pair": ("——", "zh-Hans", "ltr"),
            "ko-single": ("—", "ko", "ltr"),
            "zh-Hans-vertical-pair": ("——", "zh-Hans", "ttb"),
        }
        weight_report = {}
        for case_name, (text, language, direction) in cases.items():
            target_result = shape(target_font, text, language, direction)
            source_result = shape(source_font, text, language, direction)
            target_signature = (len(target_result[0]), target_result[1])
            source_signature = (len(source_result[0]), source_result[1])
            weight_report[case_name] = {
                "target": [target_signature[0], list(target_signature[1])],
                "source": [source_signature[0], list(source_signature[1])],
            }
            if target_signature != source_signature:
                fail(
                    float(public_weight),
                    f"source-parity-{case_name}",
                    target_signature,
                    source_signature,
                )
        source_parity[str(public_weight)] = weight_report
    return {
        "ok": not failures,
        "locations_checked": locations_checked,
        "shapes_checked": shapes_checked,
        "step": 0.5,
        "source_path": portable_report_path(source_path),
        "source_parity": source_parity,
        "italic_advance_invariant": bool(italic),
        "failure_samples": failures,
        "truncated": len(failures) >= 64,
    }


def variable_output_resume_status(region: str, italic: bool) -> tuple[bool, dict[str, Any]]:
    region = check_region(region)
    path = VARIABLE_DIR / variable_output_name(region, italic)
    details: dict[str, Any] = {
        "file": portable_report_path(path),
        "region": region,
        "italic": italic,
        "rebuilt": False,
        "resume_verified": False,
    }
    if not path.exists():
        details["resume_rebuild_reasons"] = ["missing output"]
        return False, details

    reasons: list[str] = []
    font: TTFont | None = None
    source: TTFont | None = None
    inter_source: TTFont | None = None
    try:
        raw_gvar = raw_gvar_integrity_status(path)
        details["raw_gvar_integrity"] = raw_gvar
        if not raw_gvar["ok"]:
            reasons.append(
                "invalid raw gvar point data: "
                f"warnings={raw_gvar['gvar_raw_point_warning_count']}, "
                "coordinate_length_mismatches="
                f"{raw_gvar['gvar_coordinate_length_mismatches']}"
            )
        # VariationIndex Device records live below lazily decompiled GPOS
        # subtables; a lazy font makes the recursive integrity scan see zero.
        font = TTFont(path, lazy=False, recalcTimestamp=False)
        if "VORG" in font:
            reasons.append("TrueType VF still contains CFF-only VORG")
        if "VVAR" in font:
            reasons.append("TrueType VF vertical origins still depend on conflicting VVAR/phantom paths")
        if "fvar" not in font:
            reasons.append("missing fvar")
        else:
            axes = [axis for axis in font["fvar"].axes if axis.axisTag == "wght"]
            expected_axis = tuple(float(value) for value in PUBLIC_AXIS_LIMIT["wght"])
            actual_axis = (
                (axes[0].minValue, axes[0].defaultValue, axes[0].maxValue)
                if len(axes) == 1
                else None
            )
            if actual_axis != expected_axis:
                reasons.append(f"wght axis {actual_axis!r} != {expected_axis!r}")

            expected_instances = [float(stop["value"]) for stop in SOURCE_HAN_WEIGHT_STOPS]
            actual_instances = [
                instance.coordinates.get("wght") for instance in font["fvar"].instances
            ]
            if actual_instances != expected_instances:
                reasons.append(
                    f"wght instances {actual_instances!r} != {expected_instances!r}"
                )
            actual_instance_names = [
                font["name"].getDebugName(instance.subfamilyNameID)
                for instance in font["fvar"].instances
            ]
            expected_instance_names = [
                stop["name"] + (" Italic" if italic and stop["name"] != "Regular" else "")
                if stop["name"] != "Regular"
                else ("Italic" if italic else "Regular")
                for stop in SOURCE_HAN_WEIGHT_STOPS
            ]
            if actual_instance_names != expected_instance_names:
                reasons.append(
                    f"instance names {actual_instance_names!r} != {expected_instance_names!r}"
                )

        actual_segment = (
            dict(font["avar"].segments.get("wght", {})) if "avar" in font else {}
        )
        source = TTFont(source_han_vf_path(region), lazy=True, recalcTimestamp=False)
        source_limits = AxisLimits(AXIS_LIMIT).limitAxesAndPopulateDefaults(source)
        if "avar" in source:
            instantiateAvar(source, source_limits)
        source_axis = weight_axis(source)
        source_axis.minValue, source_axis.defaultValue, source_axis.maxValue = AXIS_LIMIT["wght"]
        expected_segment = source_han_public_avar_segment(source)
        if len(actual_segment) != len(expected_segment):
            reasons.append(
                f"avar anchor count {len(actual_segment)} != {len(expected_segment)}"
            )
        else:
            for expected_key, expected_value in expected_segment.items():
                match = next(
                    (
                        (actual_key, actual_value)
                        for actual_key, actual_value in actual_segment.items()
                        if abs(actual_key - expected_key) <= 1 / 16384
                    ),
                    None,
                )
                if match is None or abs(match[1] - expected_value) > 1 / 8192:
                    reasons.append(
                        "avar mismatch at public normalized "
                        f"{expected_key:.6f}: {None if match is None else match[1]!r} "
                        f"!= {expected_value:.6f}"
                    )
                    break

        version_name = font["name"].getDebugName(5) if "name" in font else None
        if not version_name or f"project {VERSION}" not in version_name:
            reasons.append(f"nameID 5 does not identify project {VERSION}")
        copyright_name = font["name"].getDebugName(0) if "name" in font else None
        if copyright_name != project_copyright(region):
            reasons.append("nameID 0 is not the complete regional copyright")
        license_description = font["name"].getDebugName(13) if "name" in font else None
        license_url = font["name"].getDebugName(14) if "name" in font else None
        if license_description != PROJECT_LICENSE_DESCRIPTION or license_url != PROJECT_LICENSE_URL:
            reasons.append("nameID 13/14 do not identify the project OFL license")
        unique_ids = {
            record.toUnicode()
            for record in font["name"].names
            if record.nameID == 3
        } if "name" in font else set()
        if len(unique_ids) != 1:
            reasons.append("nameID 3 is not one language-independent unique identifier")
        mac_name_records = sum(
            record.platformID == 1 for record in font["name"].names
        ) if "name" in font else 0
        details["mac_name_records"] = mac_name_records
        if mac_name_records:
            reasons.append(f"{mac_name_records} Macintosh name records remain")
        if "OS/2" not in font or font["OS/2"].achVendID != OS2_VENDOR_ID:
            reasons.append(f"OS/2.achVendID is not {OS2_VENDOR_ID}")
        if "head" not in font or not math.isclose(
            font["head"].fontRevision,
            FONT_REVISION,
            abs_tol=1 / 65536,
        ):
            reasons.append(f"head.fontRevision is not {FONT_REVISION}")
        if "head" in font:
            head_flags = int(font["head"].flags)
            details["head_flags"] = head_flags
            if not head_flags & 0x0002:
                reasons.append("head.flags bit 1 is not set for TrueType VF LSB=xMin")
            if head_flags & 0x0020:
                reasons.append("head.flags bit 5 must be clear in a variable font")
        if "MVAR" not in font:
            reasons.append("missing MVAR")
        if "HVAR" not in font:
            reasons.append("missing HVAR")
        else:
            hvar = font["HVAR"].table
            if hvar.AdvWidthMap is None or hvar.LsbMap is None or hvar.RsbMap is None:
                reasons.append("HVAR lacks complete advance/LSB/RSB mappings")
        if "gvar" in font and any(item.coordinates[-4] and item.coordinates[-4][0] for items in font["gvar"].variations.values() for item in items):
            reasons.append("horizontal phantom origin still varies")
        if "HVAR" in font and not tabular_advance_structure_status(font)["ok"]:
            reasons.append("tabular digit alternatives do not share a single advance curve")
        default_lsb_xmin = lsb_xmin_mismatches(font)
        details["default_lsb_xmin_mismatches"] = len(default_lsb_xmin)
        if default_lsb_xmin:
            reasons.append(
                f"{len(default_lsb_xmin)} default glyphs have hmtx LSB != glyf xMin"
            )
        raw_flag_stats = raw_simple_glyph_flag_stats(font)
        details["raw_simple_glyph_flags"] = raw_flag_stats
        if not raw_flag_stats["glyphs_checked"]:
            reasons.append("raw glyf flag audit checked no simple glyphs")
        if raw_flag_stats["malformed_glyphs"]:
            reasons.append(
                f"{raw_flag_stats['malformed_glyphs']} malformed raw simple-glyph flag streams"
            )
        if raw_flag_stats["invalid_explicit_overlap_flags"]:
            reasons.append(
                f"{raw_flag_stats['invalid_explicit_overlap_flags']} OTS-invalid explicit "
                "OVERLAP_SIMPLE flags"
            )
        if not layout_has_feature(font, "GPOS", "chws"):
            reasons.append("missing GPOS chws")
        if not layout_has_feature(font, "GPOS", "vchw"):
            reasons.append("missing GPOS vchw")
        variation_devices = (
            collect_ot_variation_devices(font["GPOS"].table)
            if "GPOS" in font
            else []
        )
        gdef_var_store = (
            getattr(font["GDEF"].table, "VarStore", None)
            if "GDEF" in font
            else None
        )
        details["gpos_variation_device_count"] = len(variation_devices)
        details["gdef_var_store_present"] = bool(gdef_var_store)
        variation_store_valid = True
        if variation_devices and not gdef_var_store:
            reasons.append("GPOS VariationIndex records exist without a GDEF VarStore")
            variation_store_valid = False
        elif gdef_var_store:
            invalid_var_indices = []
            for device in variation_devices:
                var_idx = (int(device.StartSize) << 16) | int(device.EndSize)
                major = var_idx >> 16
                minor = var_idx & 0xFFFF
                if (
                    major >= len(gdef_var_store.VarData)
                    or minor >= len(gdef_var_store.VarData[major].Item)
                ):
                    invalid_var_indices.append(var_idx)
            if invalid_var_indices:
                variation_store_valid = False
                reasons.append(
                    "GPOS VariationIndex records are outside the GDEF VarStore: "
                    + ", ".join(str(index) for index in sorted(set(invalid_var_indices))[:16])
                )
        if variation_store_valid:
            dash_status = upstream_dash_structure_status(font, region)
            details["two_em_dash"] = dash_status
            if not dash_status["ok"]:
                reasons.append("invalid two-em dash behavior: " + "; ".join(dash_status["reasons"]))
            else:
                dash_axis_status = variable_two_em_dash_axis_status(
                    path,
                    region,
                    italic,
                )
                details["two_em_dash_axis"] = dash_axis_status
                if not dash_axis_status["ok"]:
                    reasons.append(
                        "invalid two-em dash behavior inside the public axis: "
                        + repr(dash_axis_status["failure_samples"][:8])
                    )
        else:
            details["two_em_dash"] = {"ok": False, "skipped": "invalid GDEF VarStore"}

        inter_source = load_inter(italic)
        inter_controls = inter_outline_control_status(font, inter_source)
        details["inter_outline_controls"] = inter_controls
        if not inter_controls["ok"]:
            reasons.append(
                "Inter outline controls are not source-aligned: "
                + repr(inter_controls["mismatches_by_weight"])
            )

        engine_metrics = harfbuzz_named_metric_status(path, region, italic)
        details["harfbuzz_named_metrics"] = engine_metrics
        if not engine_metrics["ok"]:
            reasons.append(
                "HarfBuzz named-instance metrics do not match project static fonts: "
                + repr(engine_metrics["counts_by_weight"])
            )
    except Exception as error:
        reasons.append(f"font validation failed: {type(error).__name__}: {error}")
    finally:
        if inter_source is not None:
            inter_source.close()
        if source is not None:
            source.close()
        if font is not None:
            font.close()

    details["resume_verified"] = not reasons
    if reasons:
        details["resume_rebuild_reasons"] = reasons
    return not reasons, details


def parse_regions(value: str | None) -> list[str]:
    if not value:
        return list(REGION_ORDER)
    result = [check_region(part.strip()) for part in value.replace(";", ",").split(",") if part.strip()]
    if not result:
        return list(REGION_ORDER)
    return list(dict.fromkeys(result))


def parse_static_weights(value: str | None) -> set[str]:
    if not value:
        return set()
    valid = {str(stop["name"]) for stop in SOURCE_HAN_WEIGHT_STOPS}
    requested = {
        part.strip()
        for part in value.replace(";", ",").split(",")
        if part.strip()
    }
    unknown = sorted(requested - valid)
    if unknown:
        raise ValueError(
            "unknown static weights: "
            + ", ".join(unknown)
            + "; expected any of "
            + ", ".join(str(stop["name"]) for stop in SOURCE_HAN_WEIGHT_STOPS)
        )
    return requested


def build_all(
    static_only: bool = False,
    regions: list[str] | None = None,
    resume_variable: bool = False,
    resume_static: bool = False,
    force_static_weights: set[str] | None = None,
    variable_only: bool = False,
    unhinted_only: bool = False,
) -> dict[str, Any]:
    regions = list(REGION_ORDER if regions is None else dict.fromkeys(check_region(region) for region in regions))
    ensure_build_sources(static_only or unhinted_only, regions, full_hint_group=not (variable_only or unhinted_only))
    required_paths = [reference_font_path(region, "Regular", False) for region in regions]
    if not (static_only or unhinted_only):
        required_paths.extend([source_han_vf_path(region) for region in variable_regions(regions)])
        required_paths.extend(
            path for path in (classical_vf_override_path(region) for region in variable_regions(regions)) if path
        )
        required_paths.extend([INTER_UPRIGHT, INTER_ITALIC])
    for path in required_paths:
        if not path.exists():
            raise FileNotFoundError(path)
    force_static_weights = set(force_static_weights or ())
    if variable_only or unhinted_only:
        log_step("static: prepare selected unhinted references without hint analysis")
        static_outputs = build_unhinted_fonts(regions, only_missing=variable_only)
    else:
        log_step("static: build hinted and unhinted")
        static_outputs = build_static_fonts(regions, resume=resume_static, force_weights=force_static_weights)
    if not variable_only:
        write_static_readme(regions, (False,) if unhinted_only else (True, False))
    if static_only or unhinted_only:
        log_step("variable: skipped by --static-only")
        variable_outputs = existing_variable_outputs()
    else:
        # Static outputs are built first because all six published static styles
        # are the final metric authority for the matching VF control points.
        variable_outputs = []
        for region in variable_regions(regions):
            for italic in (False, True):
                style = "italic" if italic else "upright"
                if resume_variable:
                    complete, resume_details = variable_output_resume_status(region, italic)
                    if complete:
                        log_step(f"variable {region} {style}: verified complete; skip")
                        variable_outputs.append(resume_details)
                        continue
                    log_step(
                        f"variable {region} {style}: resume validation requires rebuild: "
                        + "; ".join(resume_details["resume_rebuild_reasons"])
                    )
                log_step(f"variable {region} {style}: build")
                result = build_one_variable(region, italic)
                result["rebuilt"] = True
                variable_outputs.append(result)
    report = {
        "family": "Sarasa Ui PropDigits",
        "version": VERSION,
        "regions": regions,
        "variable_regions": variable_regions(regions),
        "static_regions": regions,
        "static_only": static_only,
        "variable_only": variable_only,
        "unhinted_only": unhinted_only,
        "resume_variable": resume_variable,
        "resume_static": resume_static,
        "force_static_weights": sorted(force_static_weights),
        "build_script": "tools/build_sarasa_ui_propdigits_sc.py",
        "bootstrap_sources": {
            "sarasa_gothic": SARASA_TAG,
            "sarasa_commit": SARASA_COMMIT,
            "sarasa_source_archive_sha256": SARASA_SOURCE_ARCHIVE_SHA256,
            "sarasa_package_lock_sha256": SARASA_PACKAGE_LOCK_SHA256,
            "sarasa_ui_ttf": f"{SARASA_VERSION} hinted/unhinted",
            "source_han_sans": SOURCE_HAN_TAG,
            "source_han_vf_archive_sha256": SOURCE_HAN_VF_ARCHIVE_SHA256,
            "inter": INTER_TAG,
            "inter_archive_sha256": INTER_ARCHIVE_SHA256,
            "node": NODE_VERSION,
            "node_archive_sha256": NODE_ARCHIVE_SHA256[node_platform_archive()],
            "fonttools": importlib.metadata.version("fonttools"),
            "uharfbuzz": importlib.metadata.version("uharfbuzz"),
            "ttfautohint_py": importlib.metadata.version("ttfautohint-py"),
            "py7zr": importlib.metadata.version("py7zr"),
            "afdko": importlib.metadata.version("afdko"),
            "chws_tool": importlib.metadata.version("chws-tool"),
            "east_asian_spacing": importlib.metadata.version("east-asian-spacing"),
            "chlorophytum_jobs": SARASA_HINT_JOBS,
            "hint_environment_prep_jobs": SARASA_HINT_PREP_JOBS,
            "static_hint_recipe_sha256": static_hint_recipe_fingerprint(),
        },
        "source_base_by_region": {
            region: portable_report_path(source_han_vf_path(region))
            for region in variable_regions(regions)
        },
        "classical_vf_override_by_region": {
            region: portable_report_path(path)
            for region in variable_regions(regions)
            for path in [classical_vf_override_path(region)]
            if path
        },
        "source_latin_upright": portable_report_path(INTER_UPRIGHT),
        "source_latin_italic": portable_report_path(INTER_ITALIC),
        "reference_unicode_set_by_region": {
            region: portable_report_path(reference_font_path(region, "Regular", False))
            for region in regions
        },
        "method": (
            "VF 由对应地区的 CJK VF 与 Inter VF 合并而来；SC/TC/HC/J/K 使用对应 "
            "Source Han Sans VF。CL 以 Shanggu Sans "
            f"{SHANGGU_TAG} 的官方 TTF/VF 发布物作为传统旧字形来源：静态 TTF 的 "
            "classical override 直接使用 ShangguSansTC 静态 TTF，VF 以 "
            "SourceHanSansK-VF 为底稿并用 ShangguSansTC-VF 覆盖对应 ideograph 字形。"
            "静态 CL 最终仍按 SarasaUiCL 参考字体裁剪公开 cmap 和 GSUB/GPOS feature，"
            "五个官方同名字重同步非数字 metrics；Heavy 保留 Shanggu Heavy 来源数据，"
            "Bold 只提供布局、命名和 hint 配置边界。"
            "码位归属采用 Sarasa pass1 风格，并按 VF 源文件实际覆盖做兜底：Inter VF 以 Sarasa "
            "的 Inter 设置（ss03 和 cv10）烘焙后用于 Latin 和西文符号覆盖；CJK、"
            "Korean、Jamo 以及 Sarasa Ui 本地化标点优先来自对应地区的 Source Han Sans VF。"
            "在追加 Inter glyph 前，会先应用 Source Han 的 pwid/符号清洗和 Hangul "
            "全角归一。最终 layout 导入对应地区 Sarasa Ui 暴露的 Inter VF GSUB/GPOS 特性，"
            "保留 Sarasa 的空 cv01-cv13/ss01-ss08 标签，并保留 cv14、ccmp、按上游 "
            "Sarasa Ui 覆盖裁剪的 locl、Hangul Jamo 特性、vert/vrt2、tnum/pnum、"
            "中文二字破折号（em dash），以及与 Inter 一致的数字冒号 colon-run calt 规则。"
            "静态与 VF 都在最终模板后重建 Source Han/Shanggu 的 ccmp、地区 locl 和 "
            "vert/vrt2 破折号路径。SC/TC/HC/J/K 的非 CJK 默认路径保留比例长字形，CJK "
            "路径严格使用 1em/2em/3em 字形并保留 KOR 单字特例；CL 跟随 Shanggu 的全局"
            "全宽映射。静态轮廓逐字重来自对应 Source Han/Shanggu 静态源；VF 保留同一上游"
            "的 gvar 与 metric 变化。旧 Sarasa calt continuation、pair-start 和破折号 GPOS "
            "PairPos 全部删除。破折号不向 GPOS/GDEF 追加自定义 VariationIndex。合并后会"
            "保留 U+2026 的非 CJK 下沉比例形式，并以 JAN/KOR/ZHH/ZHS/ZHT locl 恢复"
            "居中 1em 形式；连续两个字符保持两个 glyph 与严格 2em，vert/vrt2 使用现有"
            "竖排字形，不新造轮廓或重新 hint。"
            "同步或重映射 Inter layout FeatureParams 引用的界面名称记录；VF nameID 25 使用"
            "只含 ASCII 字母数字的 Variations PostScript Name Prefix。CJK Italic VF 在剪切前"
            "先于正体坐标空间展开全部 gvar IUP 隐含增量，再剪切基础轮廓和真实轮廓 delta，"
            "四个 metric phantom points 不参与剪切。"
            "对齐对应地区参考 Sarasa Ui 的 cmap alias split 和 alias mapping、GSUB "
            "FeatureRecord 顺序、空 cv/ss FeatureRecord、Script/LangSys 覆盖顺序、GPOS "
            "FeatureRecord lookup index 和 LookupList 结构；五个官方同名字重还对齐非数字 advance "
            "和 LSB、tnum 数字目标 hmtx、垂直指标与 vmtx。Heavy 的这些 glyph 数据保留实际 "
            "Source Han/Shanggu Heavy 和 Inter Black 的 Sarasa pass2 结果。VF 对齐对应公开点的 "
            "非数字 metric 规则及变化、"
            "GDEF、VORG，以及与 Sarasa 兼容的 head/OS/2 metadata；VF 套用静态 GDEF "
            "class/mark 模板时保留 Source Han ItemVariationStore，使 kern/palt/vpal 的 "
            "VariationIndex 继续随轴工作。Source Han 静态与 VF 的 palt/vpal 数值可能不同，"
            "因此五个官方同名字重的静态输出按 Sarasa 静态参考同步，Heavy 按同次 pass2 来源同步，"
            "VF 输出保留 Source Han VF 可变值。VF 在最终 metric 校准前物化全部继承 gvar，"
            "随后直接校正默认 400 的 Inter 基础复合组件，并对 200..900 的另外 12 个探测点中"
            "超出 2 units 的字形追加组件坐标校正；按组件依赖恢复决定运行时 LSB/TSB 的 "
            "xMin/yMax 后，再以不改变相对轮廓的整字平移校准 HarfBuzz 边距。简单字形、组件"
            "数量或变换不匹配均硬失败。六个发布字重再由 HarfBuzz 对项目 unhinted 静态成品逐 cmap"
            "要求 horizontal/vertical advance、LSB、TSB exact。VF 和静态输出都包含 "
            "STAT；静态 STAT 只描述单实例样式，不保留 fvar/gvar 可变表。glyph 总数不强行"
            "补齐到与上游一致：cmap 字形和布局可达的未编码字形会保留，不可达 glyph 数量"
            "差异不视为渲染缺陷。静态 TTF 从对应地区静态 Source Han Sans 和 Inter 源字体出发，"
            "经 Sarasa 的 pass1/kanji/hangul/pass2 片段路径构建，再补上 PropDigits 的数字"
            "和冒号 cmap remap、命名、metadata、layout 模板、GDEF/VORG、与上游兼容的 glyf "
            "flags/bbox/组件结构、静态 post format 3、OTS-compatible glyf repeat "
            "编码与显式后续 OVERLAP_SIMPLE 规范化、palt 取值同步、Source Han/Shanggu "
            "破折号同步和静态 STAT 规则。"
            "全部轮廓、hint、metrics、GSUB 和基础 GPOS 模板处理完成后，再按 Noto CJK "
            "交付流程追加 chws/vchw contextual positioning。静态 post 使用 format 3，"
            "不存储 glyph names，也不改变 glyph order。最终 name table 删除 platform 1 "
            "记录，只保留现代 Windows/Unicode 名称；VF 使用与静态字重一致的下划线"
            "MVAR 曲线，并写入本项目四方 copyright。head.fontRevision "
            f"写为 OpenType fixed 数值 {OPENTYPE_VERSION}，对应本仓库版本 {VERSION}。"
            "hinted 静态套件会对本项目实际生成的片段重新 hint：每个字重固定建立 "
            "Sarasa 上游顺序的 96 个 pass1 与 12 个 FE 输入，全部高层 hint 最后由一次 "
            "统一 instruct 写入 TrueType instructions，再由 pass2 合成最终 TTF。"
            "unhinted 静态套件提供无 TrueType instructions 的正式静态输出。"
            "ExtraLight、Light、Regular、SemiBold、Bold 直接使用上游同名 reference/hint "
            "环境；Heavy 900 使用实际 Heavy/Black 轮廓并沿用 Bold 的辅助边界。"
            "公开字重采用 Sarasa/CSS 口径 200/300/400/600/700/900。CJK 的 public 200 "
            "来自 Source Han ExtraLight 250，public 600 来自 Source Han Medium 500；"
            "Inter 始终钉在同数字公开坐标，因此 public 600 使用 Inter SemiBold 600。"
        ),
        "intentional_differences_from_upstream_sarasa_ui": [
            "默认 ASCII 数字和 ':' 使用比例 glyph；tnum 会恢复等宽 glyph。",
            "公开字重遵循 Sarasa/CSS 口径：200、300、400、600、700、900；CJK 的 public 200/600 分别来自 Source Han ExtraLight 250/Medium 500。",
            "VF 与静态 TTF 都使用与 Inter 一致的上下文冒号 colon-run 行为。",
            "静态与 VF 都使用 Source Han/Shanggu 的 ccmp、地区 locl、vert/vrt2 破折号结构；非 CJK 比例路径、CJK 1em/2em/3em、KOR 单字特例和 CL 全局全宽映射分别跟随对应上游，轮廓与 metrics 随字重变化。",
            "U+2026 在非 CJK 语言下保留下沉比例形式，在中日韩 locl 下切换为居中 1em 形式；连续两个字符严格占 2em，竖排复用上游现有字形。",
            "VF 与静态 TTF 都追加来自 Noto CJK 交付流程的 GPOS chws/vchw；Source Han Sans 2.005R 与 Sarasa 1.0.40 参考成品本身不含这两个 FeatureRecord。",
            "静态 CL 使用 Shanggu Sans 官方发布物作为旧字形轮廓来源；公开 cmap 与 GSUB/GPOS 模板按 SarasaUiCL 边界裁剪，五个官方同名字重的非数字 metrics 同步 SarasaUiCL，Heavy 900 保留 Shanggu Heavy 来源数据。",
            "静态 TTF 与上游一样使用 post format 3，不存储 glyph names；PropDigits 关系只由 cmap/GSUB 表达，不改 glyph order。",
            "拉丁静态 TTF 跟随 Sarasa 静态 Inter 片段路径，拉丁 VF 跟随 Inter VF；不强制两条上游路径的完整 bbox 相同，但 Inter 的 7 个设计控制点、6 个中间探测点和六个发布字重的运行时 metrics 分别严格核验。",
            "Heavy 900 是本项目扩展实例；上游 Sarasa 公开静态系列止于 Bold 700。Bold 只作为 Heavy 的布局、命名和 hint 配置边界，Heavy 的 glyf、hmtx/vmtx、VORG、bbox 与 palt 保留 Source Han/Shanggu Heavy 和 Inter Black 构建结果。",
        ],
        "final_gsub_features": sorted(FINAL_GSUB_FEATURES),
        "variable_outputs": variable_outputs,
        "static_outputs": static_outputs,
    }
    write_reports(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--static-only", action="store_true", help="只重建静态 hinted/unhinted TTF，不重建 VF 输出。")
    modes.add_argument("--variable-only", action="store_true", help="只重建所选地区 VF；缺少静态度量参考时自动准备 unhinted，完全跳过 hint 流程。")
    modes.add_argument("--unhinted-only", action="store_true", help="只按 Sarasa 静态片段路径重建所选地区 unhinted TTF，完全跳过 hint 流程。")
    parser.add_argument(
        "--refresh-static-finalization-only",
        action="store_true",
        help="只刷新现有静态 TTF 的最终 GSUB、命名、法律信息与 unhinted 栅格表；除明确白名单表外逐表保护，不重新 hint。",
    )
    parser.add_argument("--refresh-variable-finalization-only", action="store_true", help="刷新现有 VF 的 GSUB、数字替代字形等宽及跨引擎度量表示；逐表保护 GPOS 等数据，并核对 13 点运行时边界与度量。")
    parser.add_argument(
        "--regions",
        default=",".join(REGION_ORDER),
        help="逗号分隔的输出地区列表，默认 CL,SC,TC,HC,J,K；hinted 分析环境仍固定包含六地区。",
    )
    parser.add_argument(
        "--resume-variable",
        action="store_true",
        help="校验现有 VF 的轴映射、实例、版本、版权、gvar、MVAR、chws/vchw、破折号、省略号、Inter 轮廓控制点与 HarfBuzz 命名字重 metrics；完整时跳过，否则重建。",
    )
    parser.add_argument(
        "--resume-static",
        action="store_true",
        help="逐文件验证静态输出的版本、metadata、hint、layout、破折号与省略号结构；同一地区字重的四个文件全部通过时跳过，否则成组重建。",
    )
    parser.add_argument(
        "--force-static-weights",
        default="",
        help="与 --resume-static 配合，逗号分隔并强制重建指定静态字重；其他完整字重继续跳过。",
    )
    args = parser.parse_args()
    force_static_weights = parse_static_weights(args.force_static_weights)
    if force_static_weights and not args.resume_static:
        parser.error("--force-static-weights requires --resume-static")
    if args.refresh_variable_finalization_only:
        if args.refresh_static_finalization_only or args.static_only or args.variable_only or args.unhinted_only or args.resume_variable or args.resume_static or force_static_weights:
            parser.error("VF 最终化不能与其他构建模式组合")
        outputs = refresh_variable_finalization_outputs(parse_regions(args.regions))
        print(json.dumps({"mode": "variable-finalization-only", "version": VERSION, "outputs": outputs}, ensure_ascii=False, indent=2))
        return
    if args.refresh_static_finalization_only:
        if args.static_only or args.variable_only or args.unhinted_only or args.resume_variable or args.resume_static or force_static_weights:
            parser.error(
                "--refresh-static-finalization-only cannot be combined with build/resume options"
            )
        regions = parse_regions(args.regions)
        outputs = refresh_static_finalization_outputs(regions)
        write_static_readme(regions)
        print(
            json.dumps(
                {
                    "mode": "static-finalization-only",
                    "version": VERSION,
                    "regions": regions,
                    "outputs": outputs,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    report = build_all(
        static_only=args.static_only,
        regions=parse_regions(args.regions),
        resume_variable=args.resume_variable,
        resume_static=args.resume_static,
        force_static_weights=force_static_weights,
        variable_only=args.variable_only,
        unhinted_only=args.unhinted_only,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
