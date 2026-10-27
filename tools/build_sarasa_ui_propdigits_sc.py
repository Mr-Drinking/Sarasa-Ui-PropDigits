from __future__ import annotations

import argparse
import asyncio
import copy
import concurrent.futures
import functools
import gzip
import hashlib
import http.client
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
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
import zipfile
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from python_env_bootstrap import BUILD_DEPENDENCIES, ensure_project_python, locked_requirements
import python_env_bootstrap


PYTHON_DEPS = BUILD_DEPENDENCIES


def ensure_python_deps() -> None:
    ensure_project_python(
        PYTHON_DEPS,
        project_root=Path(__file__).resolve().parents[1],
        label="build",
    )


if __name__ == "__main__":
    ensure_python_deps()

from fontTools import subset
from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.otlLib.maxContextCalc import maxCtxFont
from fontTools.pens.basePen import NullPen
from fontTools.pens.boundsPen import BoundsPen, ControlBoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, TTLibError, newTable
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.tables.ttProgram import Program
from fontTools.ttLib.tables import _g_l_y_f as glyf_table
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables._f_v_a_r import NamedInstance
from fontTools.varLib.models import VariationModel, normalizeValue, piecewiseLinearMap, supportScalar
from fontTools.varLib.instancer import AxisLimits, instantiateAvar, instantiateVariableFont
from fontTools.varLib import builder as var_builder
from fontTools.varLib.builder import buildVarDevTable
from fontTools.varLib.varStore import OnlineVarStoreBuilder, VarStoreInstancer
import uharfbuzz as hb
import font_layout
import glyph_aliases
import font_variations
import font_anchor_origins


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
BUILDER_CODE_SHA256 = hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
BUILD_COMPONENT_SHA256 = {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                         for module in (font_layout, glyph_aliases, font_variations, font_anchor_origins, python_env_bootstrap)}
BUILD_LOCK = Path(__file__).resolve().parents[1] / "requirements-build.lock"
BUILD_LOCK_PACKAGES = tuple(locked_requirements(BUILD_LOCK))
BUILD_LOCK_SHA256 = hashlib.sha256(BUILD_LOCK.read_bytes()).hexdigest()
WORK_ROOT = Path(os.environ.get("SARASA_WORK_ROOT", ROOT / ".build-cache" / "work"))


def first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def log_step(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[build {timestamp}] {message}", flush=True)


SARASA_VERSION = "1.0.43"
VERSION = "1.0.43"
FONT_REVISION = 1.043
OPENTYPE_VERSION = "1.043"
VISUAL_VERTICAL_RUN_TEXTS = ("甲……乙——丙⸺丁", "甲……乙———丙⸻丁",
                           "甲————乙⸺⸻丁", "甲————————————————乙")
SARASA_TAG = f"v{SARASA_VERSION}"
SARASA_COMMIT = "61e74fda253a1bec0c6f5b09d32a0944bb96b814"
# Generated GitHub archives are not stable source identities. Git is the normal
# transport; an archive fallback is permitted only with an explicitly pinned hash.
SARASA_SOURCE_ARCHIVE_SHA256: str | None = None
SARASA_PACKAGE_LOCK_SHA256 = "616537089de133ea22f98176666408edfdc98fcca2b8babd03d775f799481658"
SARASA_UI_ARCHIVE_SHA256 = {
    "CL": {
        "hinted": "2a7eb5b7422fa2a372e098d1e6e399213609cc40b9d510e1e8f8dc19667bce79",
        "unhinted": "30dbf87fe1a975c5caa2cc4ff40df01bd91eac075ee2ccaf62af871044c07aa4"
    },
    "SC": {
        "hinted": "3dc0f534b2cfa5f98b58a574948d3e3f9eb140600bd738f259c3d077acb2de66",
        "unhinted": "1b55231dc382ac78933ba54fda493c1c14b7f4203d4b2df536de397e345b685d"
    },
    "TC": {
        "hinted": "bcdea1645e70abf36215a871895e15f49064ab80d1890e28d761521e59b15ee8",
        "unhinted": "10c34d46b38e18ca8afcce951f2ba84e6d72646521517315057ab13139c21cb7"
    },
    "HC": {
        "hinted": "303ba44f8a2ce221c55ebe48129ae4def010627b770c7636c16654407cde669c",
        "unhinted": "abd648ea42ff1537d9046061bdf9c29a4b24f3f0231054793bdecc0dc8fe5ac9"
    },
    "J": {
        "hinted": "e9fc510c63d958ac8b4a25a24fd6f7d1eb18440a3b8dfb501222e99daeee2203",
        "unhinted": "ac0594f969748d34418c0aae472f3cdb8bb002423312120a270966b6457f2e65"
    },
    "K": {
        "hinted": "017e47e1ddcf8ced80cef8d0d873371cc4a1baa09c7f45511f9928d64648770c",
        "unhinted": "e0c90a24f8a4d32345040f6721cd26748a65f30f0f746c787d3e9f34854dd360"
    }
}
SARASA_UI_AGGREGATE_ARCHIVE_SHA256 = {
    "hinted": "980a4fc33c95f1a65494612f508d76ad1892c9d20e76078692013b22c153bf30",
    "unhinted": "c17173820f91ecd774ed484eb6ea7689fc14fbef0f6740e9d7a23e265dd83354"
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
NODE_VERSION = "v26.12.1"
NODE_ARCHIVE_SHA256 = {
    ("win", "x64", "zip"): "b3efd5c70cba3308ed46e7e699a9b8aac075bbc6b027fcb39c84c31acc25e2b4",
    ("win", "arm64", "zip"): "34567581ee63967b37afb6b7a0cc63a2d550a43d5d838adb75982d7e356f2e2c",
    ("linux", "x64", "tar.xz"): "6170b2d474e1c626f43cff3adc118df9fe3b573258a4fc3d850c8dbc6b079bbd",
    ("linux", "arm64", "tar.xz"): "42aa04922591ee5d4dbc7740d452621acf28508239daa512f5ee8c47ee224fdb",
    ("darwin", "x64", "tar.xz"): "69316aaafb4dfcb25ac7b9a460f5c60928a37580284fde410144d0e6c8672d0f",
    ("darwin", "arm64", "tar.xz"): "18830bf448fe187c650dc28c98aa8c968ed1d40b6dd032c081e70a384a1bf660",
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
STATIC_HINT_WORK_VERSION = 6
STATIC_POSTPROCESS_VERSION = 19
STATIC_HINT_RECIPE = {
    "version": STATIC_HINT_WORK_VERSION,
    "fragments": "sarasa-pass1-kanji-hangul",
    "pass1_autohint": "ttfautohint-before-chlorophytum",
    "environment": "all-sarasa-families-all-regions",
    "hint_store_order": CHLOROPHYTUM_HINT_STORE_ORDER,
}
# `latn` plus fallback scaling also scales the ordinary Latin style, stripping
# its grid fitting. Keep that style hinted; only uncovered glyphs use identity
# scaling. U+0360 can generate an empty action CALL at larger Heavy sizes and
# is assigned to the same unhinted fallback through ttfautohint's native control.
# U+22EF retains its raw native fallback classification after layout additions.
# Vertical segment joins must stay on their shared geometric cell boundaries.
UI_AUTOHINT_OPTIONS = ("--fallback-script=none", "--fallback-scaling")
UI_AUTOHINT_UNSCALED_CODEPOINTS = (0x0360, 0x22EF)
UI_AUTOHINT_UNSCALED_VERTICAL_SEGMENTS = True
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
OUTPUT_STAGING_ROOT: Path | None = None
BUILD_CACHE_DIR = Path(os.environ.get("SARASA_BUILD_CACHE", ROOT / ".build-cache" / "sarasa-ui-propdigits"))
OUTPUT_VALIDATION_DIR = BUILD_CACHE_DIR / "output-validation"
STATIC_HINT_WORK_ROOT = Path(
    os.environ.get("SARASA_HINT_WORK_ROOT", ROOT / ".build-cache" / "hint-work")
)

AXIS_LIMIT = {"wght": (250, 400, 900)}
PUBLIC_AXIS_LIMIT = {"wght": (200, 400, 900)}
INTER_AXIS_LIMIT = {"opsz": 14, "wght": (200, 400, 900)}
VF_FAMILY = "Sarasa Ui VF PD SC"
VF_PS_FAMILY = "Sarasa-Ui-VF-PD-SC"
VF_FAMILY_ZH_HANS = "更纱黑体 Ui VF PD SC"
STATIC_FAMILY = "Sarasa Ui PD SC"
STATIC_PS_FAMILY = "Sarasa-Ui-PD-SC"
STATIC_FAMILY_ZH_HANS = "更纱黑体 Ui PD SC"
INTER_PREFIX = "inter."
OS2_VENDOR_ID = "MRDK"
OS2_CODEPAGE_RANGE_1_BY_REGION = {
    "SC": 0x8004019F,
    "J": 0x8002019F,
    "TC": 0x8010019F,
    "HC": 0x8010019F,
    "CL": 0x8010019F,
    "K": 0x8028019F,
}
PROJECT_COPYRIGHT = (
    "Copyright (c) 2015-2025, Renzhi Li (aka. Belleve Invis, belleve@typeof.net). "
    "Portions Copyright (c) 2016 The Inter Project Authors. "
    "Portions © 2014-2025 Adobe (http://www.adobe.com/), "
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
    return f"Sarasa Ui VF PD {check_region(region)}"


def vf_ps_family(region: str) -> str:
    return f"Sarasa-Ui-VF-PD-{check_region(region)}"


def vf_family_local(region: str) -> str:
    config = region_config(region)
    return f"{config['local_family']} Ui VF PD {check_region(region)}"


def static_family(region: str) -> str:
    return f"Sarasa Ui PD {check_region(region)}"


def static_ps_family(region: str) -> str:
    return f"Sarasa-Ui-PD-{check_region(region)}"


def static_family_local(region: str) -> str:
    config = region_config(region)
    return f"{config['local_family']} Ui PD {check_region(region)}"


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
INTER_OUTLINE_TRANSLATION_TOLERANCE = 2.0
INTER_OUTLINE_AUDIT_WEIGHTS = list(EM_DASH_PROBE_WEIGHTS)
INTER_COMPOSITE_CONTROL_WEIGHTS = list(INTER_OUTLINE_AUDIT_WEIGHTS)
INTER_COMPOSITE_NAMED_CONTROL_WEIGHTS = [200, 300, 350, 400, 600, 700, 900]
INTER_COMPOSITE_VARIATION_WEIGHTS = [
    weight for weight in INTER_COMPOSITE_CONTROL_WEIGHTS if weight != 400
]
# Every control weight is corrected on a tent bounded by its neighbors. With
# corrections at 300/350/600/700 only, the 700 tent reached 900 and left half
# its correction at 800 (Ҽ 0.9 units off Inter's curve).
INTER_OUTLINE_CORRECTION_WEIGHTS = list(INTER_COMPOSITE_VARIATION_WEIGHTS)

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
PROPDIGITS_CODEPOINTS = set(range(0x30, 0x3A)) | {
    0x20, 0xA0, 0x2008, 0x2C, 0x2E, 0x3A, 0x3B,
    0x28, 0x29, 0x5B, 0x5D, 0x7B, 0x7D,
    0x2A, 0x2B, 0x3C, 0x3D, 0x3E, 0x7E,
    0xB1, 0xD7, 0xF7, 0x2212, 0x2248, 0x2260, 0x2264, 0x2265,
}
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
    valid, reasons = unhinted_reference_status(path, region, weight_name, italic)
    if not valid:
        raise RuntimeError(f"VF 静态度量参考未通过版本、哈希与布局校验：{path.name}；{reasons}")
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
    version = f"Version {OPENTYPE_VERSION}; project {VERSION}; {source_label} + Inter VF; PD"
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
    if "fvar" in font:
        update_fvar_instances(font, region, italic)


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
            f"static {source_han_static_prefix(region)} + static Inter; PD; "
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


def update_os2_sarasa_metadata(font: TTFont, region: str) -> None:
    os2 = font["OS/2"]
    os2.version = max(os2.version, 4)
    os2.achVendID = OS2_VENDOR_ID
    os2.ulCodePageRange1 = OS2_CODEPAGE_RANGE_1_BY_REGION[check_region(region)]
    os2.ulCodePageRange2 = 0
    os2.panose.bWeight = 1 + int(os2.usWeightClass) // 100
    if int(os2.usWeightClass) in STATIC_UNDERLINE_METRICS:
        os2.yStrikeoutSize = STATIC_UNDERLINE_METRICS[int(os2.usWeightClass)][0]
    apply_sarasa_vertical_metrics(font)
    update_caret_slope(font)


def normalize_static_raster_metadata(font: TTFont, hinted: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "unhinted_max_zones_normalized": False,
        "unhinted_gasp_sentinel_normalized": False,
    }
    if hinted:
        return report
    report["unhinted_max_zones_normalized"] = int(font["maxp"].maxZones) != 1
    report["unhinted_gasp_sentinel_normalized"] = "gasp" not in font or 0xFFFF not in font["gasp"].gaspRange
    report.update(normalize_nonhinting_raster_metadata(font))
    return report


def family_name_length_status(font: TTFont) -> dict[str, Any]:
    failures = []
    lengths = []
    for record in font["name"].names:
        if record.nameID != 1:
            continue
        value = record.toUnicode()
        family = re.sub(r"(?:\s+(?:Regular|Italic|Bold))+$", "", value)
        lengths.append(len(family))
        if len(family) > 31:
            failures.append({"name_id": 1, "value": value, "length": len(family)})
    if "fvar" in font and "STAT" in font:
        stat_table = font["STAT"].table
        axes = getattr(getattr(stat_table, "DesignAxisRecord", None), "Axis", ())
        weight_indices = {index for index, axis in enumerate(axes) if axis.AxisTag == "wght"}
        weight_names = set()
        for value in getattr(getattr(stat_table, "AxisValueArray", None), "AxisValue", ()):
            indices = {getattr(value, "AxisIndex", -1)}
            indices.update(item.AxisIndex for item in getattr(value, "AxisValueRecord", ()))
            if indices & weight_indices:
                label = font["name"].getDebugName(value.ValueNameID)
                if label:
                    weight_names.add(label)
        for record in font["name"].names:
            if record.nameID != 16:
                continue
            for weight in weight_names:
                value = record.toUnicode() + " " + weight
                lengths.append(len(value))
                if len(value) > 31:
                    failures.append({"name_id": 16, "value": value, "length": len(value)})
    return {"ok": not failures, "limit": 31, "maximum_length": max(lengths, default=0), "failures": failures}


SMART_DROPOUT_PREP = bytes.fromhex("B8 01 FF 85 B0 04 8D")


def referenced_variable_name_ids(font: TTFont) -> set[int]:
    referenced: set[int] = set()
    visited: set[int] = set()
    def visit(value: Any) -> None:
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return
        if id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
            return
        fields = value if isinstance(value, dict) else getattr(value, "__dict__", {})
        for key, item in fields.items():
            if str(key).endswith("NameID") and isinstance(item, int) and 0 < item < 0xFFFF:
                referenced.add(item)
            visit(item)
        start = fields.get("FirstParamUILabelNameID", 0)
        count = fields.get("NumNamedParameters", 0)
        if count:
            if not isinstance(start, int) or not isinstance(count, int) or start < 256 or start + count > 0xFFFF:
                raise ValueError("FeatureParams 的名称范围无效")
            referenced.update(range(start, start + count))
    for tag in ("fvar", "STAT", "GSUB", "GPOS"):
        if tag in font:
            table = font[tag]
            if hasattr(table, "ensureDecompiled"):
                table.ensureDecompiled()
            visit(table)
    return referenced


def prune_unreferenced_variable_names(font: TTFont) -> dict[str, Any]:
    referenced = referenced_variable_name_ids(font)
    removed = {record.nameID for record in font["name"].names if record.nameID >= 256 and record.nameID not in referenced}
    font["name"].names = [record for record in font["name"].names if record.nameID not in removed]
    return {"unreferenced_variable_name_ids_removed": sorted(removed), "referenced_variable_name_ids": sorted(referenced)}


def normalize_nonhinting_raster_metadata(font: TTFont) -> dict[str, Any]:
    if "glyf" not in font:
        raise ValueError("nonhinting 光栅控制要求 TrueType 轮廓")
    for glyph in font["glyf"].glyphs.values():
        glyph.expand(font["glyf"])
        if hasattr(glyph, "program") and glyph.program.getBytecode():
            raise ValueError("字体含有逐字形 hint，不能按 nonhinting 覆盖其准备程序")
    for tag in ("fpgm", "cvt "):
        if tag in font:
            del font[tag]
    font["gasp"] = newTable("gasp")
    font["gasp"].version = 1
    font["gasp"].gaspRange = {0xFFFF: 0x000F}
    font["prep"] = newTable("prep")
    font["prep"].program = Program()
    font["prep"].program.fromBytecode(SMART_DROPOUT_PREP)
    maxp = font["maxp"]
    maxp.maxZones = 1
    maxp.maxTwilightPoints = 0
    maxp.maxStorage = 0
    maxp.maxFunctionDefs = 0
    maxp.maxInstructionDefs = 0
    maxp.maxStackElements = 1
    maxp.maxSizeOfInstructions = 0
    return {"nonhinting_gasp": {"65535": 15}, "nonhinting_smart_dropout_prep": SMART_DROPOUT_PREP.hex(),
            "nonhinting_glyph_hint_programs": 0}


def normalize_variable_raster_metadata(font: TTFont) -> dict[str, Any]:
    if "fvar" not in font:
        raise ValueError("仅对 VF 调用可变字体光栅元数据入口")
    return {key.replace("nonhinting_", "vf_", 1): value for key, value in normalize_nonhinting_raster_metadata(font).items()}


def has_grid_fitting_tables(font: TTFont) -> bool:
    return (any(tag in font for tag in ("fpgm", "cvt "))
            or ("prep" in font and bytes(font["prep"].program.getBytecode()) != SMART_DROPOUT_PREP))


def nonhinting_raster_metadata_status(font: TTFont) -> dict[str, Any]:
    reasons = []
    if "prep" not in font or bytes(font["prep"].program.getBytecode()) != SMART_DROPOUT_PREP:
        reasons.append("nonhinting prep must contain only the smart dropout program")
    if "gasp" not in font or font["gasp"].gaspRange != {0xFFFF: 0x000F}:
        reasons.append("nonhinting gasp must cover all sizes with 0x000F")
    if has_grid_fitting_tables(font) or font["maxp"].maxSizeOfInstructions != 0:
        reasons.append("nonhinting font contains grid-fitting instructions")
    maxp = font["maxp"]
    if (maxp.maxZones != 1 or maxp.maxStackElements < 1 or maxp.maxTwilightPoints != 0
            or maxp.maxStorage != 0 or maxp.maxFunctionDefs != 0 or maxp.maxInstructionDefs != 0):
        reasons.append("nonhinting maxp does not match its preparation program")
    return {"ok": not reasons, "reasons": reasons}


def variable_raster_metadata_status(font: TTFont) -> dict[str, Any]:
    reasons = []
    if "gasp" not in font or font["gasp"].gaspRange != {0xFFFF: 0x000F}:
        reasons.append("VF gasp must cover all sizes with 0x000F")
    if "prep" not in font or bytes(font["prep"].program.getBytecode()) != SMART_DROPOUT_PREP:
        reasons.append("VF prep must contain only the minimal smart dropout program")
    maxp = font["maxp"]
    if (maxp.maxZones != 1 or maxp.maxStackElements < 1 or maxp.maxTwilightPoints != 0
            or maxp.maxStorage != 0 or maxp.maxFunctionDefs != 0 or maxp.maxInstructionDefs != 0):
        reasons.append("VF maxp does not match the nonhinting preparation program")
    existing = {record.nameID for record in font["name"].names}
    referenced = referenced_variable_name_ids(font)
    orphaned = {name_id for name_id in existing if name_id >= 256} - referenced
    if orphaned:
        reasons.append("VF contains unreferenced custom name records")
    if referenced - existing:
        reasons.append("VF references missing name records")
    return {"ok": not reasons, "reasons": reasons, "unreferenced_name_ids": sorted(orphaned), "missing_name_ids": sorted(referenced - existing)}


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
    strikeout = ot.MetricsValueRecord()
    strikeout.ValueTag = "strs"
    strikeout.VarIdx = next(record.VarIdx for record in records if record.ValueTag == "unds")
    records.append(strikeout)
    font["OS/2"].yStrikeoutSize = base_values["underlineThickness"]
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


@contextmanager
def upright_ink_measurement():
    """Measure an italic font's punctuation ink as the upright shape it is sheared from.

    east-asian-spacing classifies punctuation by which half of the em its ink
    falls in. Sheared ink crosses the middle depending on weight: SC italic ？
    counted as left-ink at Light but not at Regular, which one VF cannot
    follow. Sarasa shears about em/3, so the upright shape is recovered there.
    """
    from east_asian_spacing import font as spacing_font
    from fontTools.pens.transformPen import TransformPen

    original = spacing_font.Font.glyph_bounds

    def glyph_bounds(self, glyph):
        ttfont = self.ttfont
        angle = ttfont["post"].italicAngle if ttfont is not None and "post" in ttfont else 0
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
            with upright_ink_measurement():
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


def reference_outline_divergences(font: TTFont, reference: TTFont, skip_codepoints: set[int]) -> set[int]:
    """Code points whose glyph is not the reference glyph.

    Reference metrics place the reference's own outline. CL draws some
    ideographs from Shanggu 1.028 where Sarasa Ui CL keeps Source Han K (its
    Shanggu 1.025 subset leaves 㓱 out); Source Han's side bearings and
    vertical origin put Shanggu's 㓱 6 units right and 21 units high in
    Regular. Such glyphs keep their source placement, as Sarasa keeps
    Shanggu's for the subset it overrides.
    """
    current, reference_cmap = font.getBestCmap(), reference.getBestCmap()
    return {cp for cp in set(current) & set(reference_cmap) if cp not in skip_codepoints
            and glyph_point_structure(font, current[cp]) != glyph_point_structure(reference, reference_cmap[cp])}


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


def normalize_ideograph_gdef_classes(font: TTFont) -> dict[str, int]:
    # A CJK ideograph is a spacing base even when an inherited positional-name
    # collision labelled it as a mark. HarfBuzz otherwise zeros its advance.
    if "GDEF" not in font or not font["GDEF"].table.GlyphClassDef:
        return {"ideograph_mark_classes_corrected": 0}
    seeds = {name for cp, name in font.getBestCmap().items() if is_ideograph(cp)}
    reachable = font_layout.substitution_closure(font, seeds)
    classes = font["GDEF"].table.GlyphClassDef.classDefs
    corrected = 0
    for name in reachable:
        if classes.get(name) == 3:
            classes[name] = 1
            corrected += 1
    return {"ideograph_mark_classes_corrected": corrected}


def rebuild_gdef_from_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    if "fvar" in font:
        # The merged variable sources own these glyph identities and classes.
        # Static post names such as glyph12345 are positional, not identities.
        gdef = font.get("GDEF")
        classes = getattr(getattr(gdef.table, "GlyphClassDef", None), "classDefs", {}) if gdef else {}
        mark_sets = getattr(gdef.table, "MarkGlyphSetsDef", None) if gdef else None
        return {"gdef_classdefs": len(classes),
                "gdef_mark_sets": len(mark_sets.Coverage) if mark_sets else 0,
                "gdef_var_store_preserved": int(gdef is not None and getattr(gdef.table, "VarStore", None) is not None)}
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
                clone_name = clone_glyph(font, target_name, preferred_name, inherit_layout=True)
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
    current_vertical = get_single_substitution_mappings(font, {"vert", "vrt2"})
    variations_added = 0
    corrections = 0
    for weight_value in correction_weights:
        reference = reference_fonts[weight_value]
        if "vmtx" not in reference:
            continue
        # Static post names such as glyph04590 are positional. The Heavy
        # reference is a project build with its own glyph order, so each
        # reference resolves its own vertical forms.
        reference_vertical = get_single_substitution_mappings(reference, {"vert", "vrt2"})
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
        subfamily = ("Italic" if italic else "Regular") if weight_name == "Regular" else weight_name + (" Italic" if italic else "")
        postscript = (f"{ps_family}-Italic" if italic else ps_family) if weight_name == "Regular" else f"{ps_family}-{weight_name}{'Italic' if italic else ''}"
        existing = next((item for item in font["fvar"].instances
                         if item.coordinates == {"wght": float(weight_value)} and item.flags == 0
                         and name_table.getDebugName(item.subfamilyNameID) == subfamily
                         and name_table.getDebugName(item.postscriptNameID) == postscript), None)
        if existing is not None:
            # STAT may refer to a different record with the same text. Keep
            # the existing fvar references, not merely any matching name ID.
            instances.append(existing)
            continue
        instance = NamedInstance()
        instance.coordinates = {"wght": float(weight_value)}
        instance.flags = 0
        instance.subfamilyNameID = name_table.addName(subfamily)
        instance.postscriptNameID = name_table.addName(postscript)
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
    flush_layout_aliases(font)
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


def clone_glyph(font: TTFont, source_name: str, preferred_name: str, *, inherit_layout: bool = False) -> str | None:
    if source_name not in font["glyf"].glyphs or source_name not in font["hmtx"].metrics:
        return None
    old_order = font.getGlyphOrder()
    metric_maps = []
    for tag, fields in (("HVAR", ("AdvWidthMap", "LsbMap", "RsbMap")),
                        ("VVAR", ("AdvHeightMap", "TsbMap", "BsbMap", "VOrgMap"))):
        if tag not in font:
            continue
        for field in fields:
            mapping = getattr(font[tag].table, field, None)
            if mapping is None and field in {"AdvWidthMap", "AdvHeightMap"}:
                mapping = var_builder.buildVarIdxMap(list(range(len(old_order))), old_order)
                setattr(font[tag].table, field, mapping)
            if mapping is not None:
                metric_maps.append((mapping.mapping, mapping.mapping[source_name]))
    existing = set(old_order) | set(font["glyf"].glyphs)
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
    for mapping, index in metric_maps:
        mapping[new_name] = index
    if "maxp" in font:
        font["maxp"].numGlyphs = len(order)
    if inherit_layout:
        pending = getattr(font, "_pending_layout_aliases", {})
        pending.setdefault(source_name, set()).add(new_name)
        font._pending_layout_aliases = pending
    return new_name


def flush_layout_aliases(font: TTFont) -> None:
    pending = getattr(font, "_pending_layout_aliases", {})
    if pending:
        glyph_aliases.inherit_inputs(font, pending)
        font._pending_layout_aliases = {}


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


def apply_classical_cmap_overrides(base: TTFont, override: TTFont, codepoints: set[int]) -> tuple[int, int]:
    base_cmap, source_cmap = base.getBestCmap(), override.getBestCmap()
    groups: dict[str, dict[str, set[int]]] = {}
    all_users = glyph_to_unicodes(base)
    for codepoint in sorted(codepoints & set(base_cmap) & set(source_cmap)):
        if is_ideograph(codepoint):
            groups.setdefault(base_cmap[codepoint], {}).setdefault(source_cmap[codepoint], set()).add(codepoint)
    changed = 0; glyphs = 0
    for original, replacements in groups.items():
        replaced_codepoints = set().union(*replacements.values())
        keep_original = not (all_users[original] - replaced_codepoints)
        for source_name, users in sorted(replacements.items(), key=lambda item: min(item[1])):
            target_name = original if keep_original else clone_glyph(
                base, original, f"{original}.classical.u{min(users):04X}", inherit_layout=True,
            )
            keep_original = False
            if target_name is None:
                raise ValueError("无法分离尚古覆盖的 cmap 别名")
            copy_external_glyph_data(base, target_name, override, source_name)
            for table in base["cmap"].tables:
                if table.isUnicode():
                    for codepoint in users:
                        if codepoint in table.cmap:
                            table.cmap[codepoint] = target_name
            changed += len(users); glyphs += 1
    flush_layout_aliases(base)
    return changed, glyphs


def apply_classical_vf_override(base: TTFont, override: TTFont, region: str) -> dict[str, int]:
    if not region_config(region)["classical"]:
        return {
            "classical_vf_override_codepoints": 0,
            "classical_vf_override_glyphs": 0,
            "classical_vf_dash_glyphs": 0,
        }
    reference_cps = reference_unicodes(region)
    replaced_codepoints, replaced_glyphs = apply_classical_cmap_overrides(base, override, reference_cps)
    # Later alias alignment must not merge what Shanggu draws apart.
    base._sarasa_classical_cmap = {cp: name for cp, name in override.getBestCmap().items()
                                   if cp in reference_cps and is_ideograph(cp)}

    # Keep Source Han's Western and Korean forms. Only the CJK fullwidth and
    # vertical forms come from Shanggu, before axis remapping and shearing.
    base_dash = upstream_dash_roles(base)
    override_dash = upstream_dash_roles(override)
    dash_pairs = [
        (base_dash["fullwidth"], override_dash["fullwidth"]),
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
        "classical_vf_override_glyphs": replaced_glyphs,
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

    new_name = clone_glyph(font, glyph_name, new_name, inherit_layout=True)
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


def align_reference_cmap_alias_mappings(
    font: TTFont,
    reference: TTFont,
    skip_codepoints: set[int],
    separate: dict[int, str] | None = None,
) -> dict[str, int]:
    """Share a glyph where the reference shares one.

    ``separate`` maps codepoints to the glyphs a classical override gave them.
    Codepoints it gives different glyphs stay apart: Sarasa Ui CL shares
    Source Han's ⺭ for 礻, while Shanggu, and the static, draw each its own.
    """
    reference_groups: dict[str, set[int]] = {}
    for codepoint, glyph_name in reference.getBestCmap().items():
        if codepoint not in skip_codepoints:
            reference_groups.setdefault(glyph_name, set()).add(codepoint)

    remapped = 0
    kept_apart = 0
    current_cmap = font.getBestCmap()
    for ref_glyph, codepoints in reference_groups.items():
        shared = sorted(cp for cp in codepoints if cp in current_cmap)
        if len(shared) <= 1:
            continue
        if separate and len({separate[cp] for cp in shared if cp in separate}) > 1:
            kept_apart += 1
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
    return {"reference_cmap_alias_mappings_aligned": remapped, "reference_cmap_alias_groups_kept_apart": kept_apart}


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
    with inter_exact_curve(inter) as exact:
        for weight_value in INTER_OUTLINE_CORRECTION_WEIGHTS:
            # Compare the product's own unrounded curve with Inter's. An
            # instance rounds every value: two rounded values a unit apart
            # can be 0.2 apart in fact, and such a spurious unit, added once
            # per pass, moved Ą's ogonek 2 units at 700.
            own = font_variations.ExactCurve(font, 1.0)
            target = instantiateVariableFont(inter, {"wght": weight_value}, inplace=False, optimize=True)
            try:
                support = supports[weight_value]
                for source_name, target_name in pairs:
                    current_control = glyph_variation_control_coordinates(font, target_name)
                    target_control = glyph_variation_control_coordinates(target, source_name)
                    if (
                        current_control is None
                        or target_control is None
                        or current_control[0] != target_control[0]
                        or current_control[2] != target_control[2]
                        or len(current_control[1]) != len(target_control[1])
                    ):
                        continue
                    current_coordinates = own.points(target_name, weight_value)
                    target_coordinates = target_control[1]
                    curve = exact.points(source_name, weight_value) if exact is not None else None
                    if curve is not None and len(curve) == len(target_coordinates):
                        # The rounded Inter copy drifts from its own curve; aim at
                        # the original Inter point.
                        target_coordinates = curve
                    # A point within half a unit of the curve is exact.
                    deltas = [
                        tuple(otRound(miss) if abs(miss) > 0.5 else 0
                              for miss in (target_x - current_x, target_y - current_y))
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


class ComponentInkPen(NullPen):
    """Each component's transform and ink, drawn while its composite is drawn.

    Drawn there, a child sits at its own points as in engines. Drawn on its
    own it is shifted to its left side bearing: an origin still carried by
    phantom points (metric alignment before normalize_cross_engine_metrics)
    put ḩ's h two units from where the composite places it.
    """

    def __init__(self, glyph_set: Any):
        self.glyph_set = glyph_set
        self.layout: list[tuple[Any, Any]] = []

    def addComponent(self, glyph_name: str, transformation: Any) -> None:
        pen = BoundsPen(self.glyph_set)
        self.glyph_set[glyph_name].draw(pen)
        self.layout.append((transformation, pen.bounds))


def component_layout(glyph_set: Any, glyph_name: str, scale: float = 1.0) -> list[tuple[tuple[float, float], Any]]:
    """Each component's (offset, child ink box) as the composite places them at the glyph set's location."""
    pen = ComponentInkPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return [((transform[4] * scale, transform[5] * scale), None if box is None else tuple(value * scale for value in box))
            for transform, box in pen.layout]


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
    source = exact_inter_instance(inter, weight_value)
    pending: list[tuple[str, list[tuple[int, int]]]] = []
    bounds: dict[str, tuple[int, int, int, int]] = {}
    unsupported: list[dict[str, Any]] = []
    mismatches = 0
    maximum_residual = 0.0
    original_path = getattr(inter, "_sarasa_exact_source", None)
    original = TTFont(original_path) if original_path else None
    try:
        current_set = current.getGlyphSet()
        source_set = source.getGlyphSet()
        # Component offsets are compared unrounded: the product's own curve at
        # this weight against Inter's original curve. Rounded instances and
        # child control boxes left Ś's acute 3 units off at italic 200, below
        # any outline-residual threshold.
        unrounded = font.getGlyphSet(location={"wght": weight_value})
        if original is not None:
            fixed = {tag: value for tag, value in INTER_AXIS_LIMIT.items() if not isinstance(value, tuple)}
            exact_set, exact_scale = original.getGlyphSet(location={**fixed, "wght": weight_value}), 1000 / original["head"].unitsPerEm
        else:
            exact_set, exact_scale = source_set, 1.0
        for source_name, target_name in pairs:
            reason, residual = translation_invariant_outline_residual(
                decomposed_glyph_recording(source_set, source_name),
                decomposed_glyph_recording(current_set, target_name),
            )
            if reason is None and math.isfinite(residual):
                maximum_residual = max(maximum_residual, residual)
            current_glyph = current["glyf"][target_name]
            source_glyph = source["glyf"][source_name]
            current_glyph.expand(current["glyf"])
            source_glyph.expand(source["glyf"])
            if reason is None and residual <= INTER_OUTLINE_TRANSLATION_TOLERANCE and not (
                    current_glyph.isComposite() and source_glyph.isComposite()):
                continue
            problem: str | None = None
            if not current_glyph.isComposite() or not source_glyph.isComposite():
                problem = "non_composite"
            elif len(current_glyph.components) != len(source_glyph.components):
                problem = "component_count"
            elif any(component_transform(component)[:4] != (1.0, 0.0, 0.0, 1.0)
                     for component in [*current_glyph.components, *source_glyph.components]):
                problem = "non_identity_component"
            current_bounds = glyph_bbox(current, target_name)
            if problem is None and current_bounds is None:
                problem = "missing_parent_bounds"
            deltas: list[tuple[int, int]] = []
            if problem is None:
                placed = component_layout(unrounded, target_name)
                expected = component_layout(exact_set, source_name, exact_scale)
                inked = [(offset, box) for offset, box in placed if box is not None]
                if len(placed) != len(expected):
                    problem = "component_count"
                elif inked:
                    # Each child's ink centre goes where Inter puts its own
                    # child's. Children are aligned to the native static one by
                    # one; Inter's offsets alone keep each child's rounding (½
                    # two units narrow at 600). The whole glyph keeps the ink
                    # xMin/yMax metric alignment gave it: Ś's base S, or the
                    # dot that bounds i on the left.
                    boxes = [box for _offset, box in placed]
                    wanted = [want if box is None or inter_box is None else
                              (want[0] + (inter_box[0] + inter_box[2] - box[0] - box[2]) / 2,
                               want[1] + (inter_box[1] + inter_box[3] - box[1] - box[3]) / 2)
                              for box, (want, inter_box) in zip(boxes, expected)]
                    shift_x = (min(offset[0] + box[0] for offset, box in inked)
                               - min(want[0] + box[0] for want, box in zip(wanted, boxes) if box is not None))
                    shift_y = (max(offset[1] + box[3] for offset, box in inked)
                               - max(want[1] + box[3] for want, box in zip(wanted, boxes) if box is not None))
                    # An integer offset within half a unit of its place is exact.
                    deltas = [tuple(otRound(miss) if abs(miss) > 0.5 else 0
                                    for miss in (want_x + shift_x - have_x, want_y + shift_y - have_y))
                              for ((have_x, have_y), _box), (want_x, want_y) in zip(placed, wanted)]
            if problem is None and not any(dx or dy for dx, dy in deltas):
                continue
            if problem is not None and reason is None and residual <= INTER_OUTLINE_TRANSLATION_TOLERANCE:
                continue
            mismatches += 1
            if problem is not None:
                unsupported.append(
                    {
                        "weight": weight_value,
                        "source_glyph": source_name,
                        "target_glyph": target_name,
                        "reason": problem,
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
        if original is not None:
            original.close()
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
            expected_glyphs = expected.getGlyphSet()
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
                    native = reference_ink_offsets(expected, expected_glyphs, expected_glyph)
                    ink = BoundsPen(None)
                    if extents is not None:
                        hb_font.draw_glyph_with_pen(glyph_id, ink)
                    if extents is not None and native is not None and ink_positioned_codepoint(codepoint, italic):
                        # The glyph keeps the native static's drawn ink, not
                        # its control-point xMin; allow its own outline rounding.
                        offset = native[2] - ink.bounds[0] if ink.bounds else 0
                        if abs(offset) > 1:
                            dx = otRound(offset)
                            counts["horizontal_side_bearing"] += 1
                    elif extents is not None and extents.x_bearing != expected_lsb:
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
                        if extents is not None and native is not None and ink.bounds:
                            # The native ink below the vertical origin, as
                            # reference_cmap_metric_targets calibrates it.
                            _origin_x, origin_y = hb_font.get_glyph_v_origin(glyph_id)
                            offset = origin_y - ink.bounds[3] - (expected_tsb + native[1])
                            if abs(offset) > 1:
                                dy = otRound(offset)
                                counts["vertical_side_bearing"] += 1
                        elif extents is not None:
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
        source = exact_inter_instance(inter, weight_value)
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
                actual_side, expected_side = (extents.x_bearing if extents is not None else None), expected_lsb
                mismatch = extents is not None and extents.x_bearing != expected_lsb
                if ink_positioned_codepoint(codepoint, italic):
                    # These glyphs keep the native ink position; their own
                    # control-point xMin may differ (see reference targets).
                    actual_ink, expected_ink = BoundsPen(None), BoundsPen(None)
                    hb_font.draw_glyph_with_pen(glyph_id, actual_ink)
                    expected_hb.draw_glyph_with_pen(expected_gid, expected_ink)
                    actual_side = actual_ink.bounds[0] if actual_ink.bounds else None
                    expected_side = expected_ink.bounds[0] if expected_ink.bounds else None
                    mismatch = (actual_side is None) != (expected_side is None) or (
                        actual_side is not None and abs(actual_side - expected_side) > 1)
                if mismatch:
                    counts["horizontal_side_bearing"] += 1
                    if len(samples) < 16:
                        samples.append(
                            {
                                "weight": weight_value,
                                "codepoint": f"U+{codepoint:04X}",
                                "metric": "horizontal_side_bearing",
                                "actual": actual_side,
                                "expected": expected_side,
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
                    # The ink below the vertical origin; the control-point
                    # yMax may differ (see reference_cmap_metric_targets).
                    actual_ink, expected_ink = BoundsPen(None), BoundsPen(None)
                    hb_font.draw_glyph_with_pen(glyph_id, actual_ink)
                    expected_hb.draw_glyph_with_pen(expected_gid, expected_ink)
                    _origin_x, origin_y = hb_font.get_glyph_v_origin(glyph_id)
                    actual_tsb = origin_y - extents.y_bearing
                    mismatch = actual_tsb != expected_tsb
                    if actual_ink.bounds and expected_ink.bounds:
                        actual_tsb = origin_y - actual_ink.bounds[3]
                        expected_tsb = expected_hb.get_glyph_v_origin(expected_gid)[1] - expected_ink.bounds[3]
                        mismatch = abs(actual_tsb - expected_tsb) > 1
                    if mismatch:
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
    selected_roles: set[str] | None = None,
) -> dict[str, Any]:
    if "gvar" not in target or "fvar" not in target:
        return {"upstream_dash_metric_variations_preserved": False}
    target_roles = upstream_dash_roles(target)
    source_roles = upstream_dash_roles(source)
    role_pairs = [
        (role, target_roles[role], source_roles[role])
        for role in target_roles if selected_roles is None or role in selected_roles
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
    for codepoint in sorted(PROPDIGITS_CODEPOINTS):
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


def preserve_inter_advance_curves(font: TTFont, region: str, italic: bool) -> dict[str, Any]:
    # Outline instancing and side-bearing calibration must not become a second
    # source of Inter's advance curve. Read the original 2048-em HVAR here and
    # rescale its runtime metrics only once, retaining native named controls.
    with TTFont(INTER_ITALIC if italic else INTER_UPRIGHT) as raw:
        source = instantiateVariableFont(raw, {"opsz": 14}, inplace=False, optimize=True)
    try:
        source.flavor = None
        apply_inter_ui_recipe(source)
        cmap = font.getBestCmap()
        owned = {cp for cp in source.getBestCmap() if cp in cmap and use_inter_codepoint(cp)}
        pairs = {cmap[cp]: source.getBestCmap()[cp] for cp in owned}
        graph = inter_layout_glyph_map(font, source, codepoints=owned)
        owned_source_glyphs = font_layout.substitution_closure(source, {source.getBestCmap()[cp] for cp in owned})
        for source_name, target_name in graph.items():
            if source_name in owned_source_glyphs:
                pairs.setdefault(target_name, source_name)
        native_advances = {}
        for stop in SOURCE_HAN_WEIGHT_STOPS:
            with open_project_static_metric_reference_font(region, str(stop["name"]), italic) as reference:
                reference_cmap = reference.getBestCmap()
                values = {cmap[cp]: reference["hmtx"].metrics[reference_cmap[cp]][0]
                          for cp in owned if cp in reference_cmap}
                reference_graph = inter_layout_glyph_map(reference, source, codepoints=owned)
                for target_name, source_name in pairs.items():
                    if source_name in reference_graph:
                        values.setdefault(target_name, reference["hmtx"].metrics[reference_graph[source_name]][0])
                native_advances[int(stop["value"])] = values
        store, names, weights = font_variations.advance_curve_store(
            font, source, pairs, {int(stop["value"]) for stop in SOURCE_HAN_WEIGHT_STOPS}, native_advances,
            INTER_COMPOSITE_CONTROL_WEIGHTS)
        hvar = font["HVAR"].table
        order = font.getGlyphOrder()
        mapping = dict(hvar.AdvWidthMap.mapping) if hvar.AdvWidthMap is not None else dict(zip(order, range(len(order))))
        hvar.VarStore, new_indices = append_item_variation_store(hvar.VarStore, store, list(range(len(names))))
        mapping.update(zip(names, new_indices))
        hvar.AdvWidthMap = var_builder.buildVarIdxMap([mapping[name] for name in order], order)
        return {"inter_advance_curves_preserved": len(names), "inter_advance_curve_weights": weights}
    finally:
        source.close()


def align_inter_attachment_origins(font: TTFont, italic: bool) -> dict[str, Any]:
    with load_inter(italic) as source:
        owned = {cp for cp in source.getBestCmap() if cp in font.getBestCmap() and use_inter_codepoint(cp)}
        owned_names = font_layout.substitution_closure(source, {source.getBestCmap()[cp] for cp in owned})
        mapping = {original: target for original, target in inter_layout_glyph_map(font, source, codepoints=owned).items()
                   if original in owned_names}
        with TTFont(source._sarasa_exact_source) as original:
            fixed = {tag: value for tag, value in INTER_AXIS_LIMIT.items() if not isinstance(value, tuple)}
            return font_anchor_origins.align(font, source, mapping, INTER_COMPOSITE_CONTROL_WEIGHTS,
                                             append_item_variation_store, exact=(original, fixed))


def tabular_advance_structure_status(font: TTFont) -> dict[str, Any]:
    names = tabular_digit_alternates(font)
    table = font["HVAR"].table
    indices = {table.AdvWidthMap.mapping[name] if table.AdvWidthMap is not None else font.getGlyphID(name) for name in names}
    advances = {font["hmtx"].metrics[name][0] for name in names}
    return {"ok": len(names) >= 11 and len(indices) == len(advances) == 1, "glyphs": len(names), "advance_curves": len(indices), "default_advances": sorted(advances)}


def reference_digit_hmtx(reference: TTFont) -> dict[int, tuple[int, int]]:
    cmap = reference.getBestCmap()
    metrics: dict[int, tuple[int, int]] = {}
    for codepoint in sorted(PROPDIGITS_CODEPOINTS):
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
        for codepoint in sorted(PROPDIGITS_CODEPOINTS):
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
    for codepoint in sorted(PROPDIGITS_CODEPOINTS):
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
    flush_layout_aliases(font)
    mapping = get_single_substitution_mapping(font, tag)
    if not mapping:
        return 0
    count = 0
    replacements = {}
    for codepoint, glyph_name in list(font.getBestCmap().items()):
        if codepoint_filter and not codepoint_filter(codepoint):
            continue
        target_name = mapping.get(glyph_name)
        if target_name and target_name in font.getGlyphSet():
            replacements[codepoint] = target_name
            count += 1
    for table in font["cmap"].tables:
        if table.isUnicode():
            for codepoint, name in replacements.items():
                if codepoint in table.cmap:
                    table.cmap[codepoint] = name
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
        # dx is whole; an instanced default outline stays in floats until
        # glyf compiles it, and italic shearing reads the true points.
        for index, (x, y) in enumerate(glyph.coordinates):
            glyph.coordinates[index] = x + dx, y
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
            glyph.coordinates[index] = x, y + dy
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
    flush_layout_aliases(font)
    return {"source_han_pwid_baked": pwid_count, "source_han_symbols_sanitized": touched}


def normalize_hangul_widths(font: TTFont) -> int:
    flush_layout_aliases(font)
    glyphs = font_layout.substitution_closure(font, {
        name for codepoint, name in font.getBestCmap().items() if is_korean(codepoint)
    })
    em = font["head"].unitsPerEm
    for glyph_name in glyphs:
        old_width = font["hmtx"].metrics.get(glyph_name, (em, 0))[0]
        if old_width > 0:
            target_width = math.ceil(old_width / em) * em
            shift_glyph_x(font, glyph_name, (target_width - old_width) / 2)
            set_advance_width(font, glyph_name, target_width)
            freeze_advance_variation(font, glyph_name)
        else:
            shift_glyph_x(font, glyph_name, -0.04 * em)
    return len(glyphs)


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
    glyf = font["glyf"]
    # Instancing to a new default keeps the default outline in floats until
    # glyf is compiled, while its deltas already start from the rounded
    # points. Shearing the floats left 䊴 a unit off at 200. Each sheared
    # default x rounds once from the true point; the variations then absorb
    # that rounding relative to the points their deltas start from.
    true_defaults: dict[str, list[tuple[float, float]]] = {}
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        glyph.expand(glyf)
        if glyph.isComposite():
            true_defaults[glyph_name] = [(component.x, component.y) for component in glyph.components]
            for component in glyph.components:
                component.x, component.y = otRound(component.x), otRound(component.y)
        elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
            true_defaults[glyph_name] = list(glyph.coordinates)
            glyph.coordinates.toInt(round=otRound)
    report = materialize_gvar_deltas(font)
    shear = math.tan(math.radians(angle_degrees))
    center = font["head"].unitsPerEm / 3
    # What rounding left on each sheared default x; the variations absorb it.
    default_errors: dict[str, list[float]] = {}
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        glyph.expand(glyf)
        errors: list[float] = []
        if glyph.isComposite():
            for component, (true_x, true_y) in zip(glyph.components, true_defaults[glyph_name]):
                # Children receive the same affine transform. Conjugate their
                # component matrices instead of applying the center twice.
                matrix = getattr(component, "transform", [[1, 0], [0, 1]])
                (xx, xy), (yx, yy) = matrix
                transformed = [[xx + shear * xy, xy],
                               [yx + shear * (yy - xx) - shear * shear * xy, yy - shear * xy]]
                offset = center * shear * (transformed[0][0] - 1)
                exact = component.x + component.y * shear + offset
                component.x = otRound(true_x + true_y * shear + offset)
                errors.append(component.x - exact)
                component.y = otRound(component.y + center * shear * xy)
                if transformed != [[1, 0], [0, 1]]:
                    component.transform = transformed
        elif glyph.numberOfContours > 0 and hasattr(glyph, "coordinates"):
            for index, ((x, y), (true_x, true_y)) in enumerate(zip(glyph.coordinates, true_defaults[glyph_name])):
                glyph.coordinates[index] = otRound(true_x + (true_y - center) * shear), y
                errors.append(glyph.coordinates[index][0] - (x + (y - center) * shear))
        default_errors[glyph_name] = errors
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        glyph.recalcBounds(glyf)
        if hasattr(glyph, "xMin"):
            font["hmtx"].metrics[glyph_name] = (font["hmtx"].metrics[glyph_name][0], glyph.xMin)
    if "gvar" in font:
        for glyph_name, variations in font["gvar"].variations.items():
            shear_variation_deltas(variations, default_errors.get(glyph_name, []), shear)
    return report


def shear_variation_deltas(variations: list[TupleVariation], default_errors: list[float], shear: float) -> None:
    """Shear gvar x deltas so the outline at every tuple's peak rounds once.

    Each tuple's sheared delta rounded on its own adds up where tuples
    overlap: Source Han's 夜 sat 1.9 units off its sheared curve at 200. A
    tuple is taken after every tuple that is non-zero at its peak and absorbs
    what the rounded default and those tuples left there; tuples sharing a
    support are rounded together. The final four gvar coordinates are metric
    phantom points: shearing them would couple vertical metric deltas into
    horizontal metrics.
    """
    count = len(variations)
    exact = [[None if xy is None else xy[0] + xy[1] * shear for xy in variation.coordinates[:-4]]
             for variation in variations]
    peaks = [{axis: peak for axis, (_start, peak, _end) in variation.axes.items()} for variation in variations]
    scalars = [[supportScalar(peaks[j], variations[k].axes) for k in range(count)] for j in range(count)]
    groups: list[list[int]] = []
    by_support: dict[tuple, int] = {}
    for index, variation in enumerate(variations):
        key = tuple(sorted(variation.axes.items()))
        if key not in by_support:
            by_support[key] = len(groups)
            groups.append([])
        groups[by_support[key]].append(index)
    pending = list(range(len(groups)))
    done: list[int] = []
    rounded: list[list[int | None]] = [[None] * len(row) for row in exact]
    while pending:
        # A peak no pending tuple reaches; otherwise (overlapping tents) the
        # one nearest the default.
        ready = [group for group in pending
                 if not any(scalars[groups[group][0]][k] for other in pending if other != group for k in groups[other])]
        group = ready[0] if ready else min(pending, key=lambda g: max(abs(v) for v in peaks[groups[g][0]].values()))
        pending.remove(group)
        members = groups[group]
        reached = [(k, scalars[members[0]][k]) for k in done if scalars[members[0]][k]]
        for index in range(len(exact[members[0]])):
            if any(exact[member][index] is None for member in members):
                continue
            total = sum(exact[member][index] for member in members)
            if index < len(default_errors):
                total -= default_errors[index]
            for k, scalar in reached:
                if exact[k][index] is not None and rounded[k][index] is not None:
                    total += scalar * (exact[k][index] - rounded[k][index])
            for member in members[:-1]:
                rounded[member][index] = otRound(exact[member][index])
                total -= rounded[member][index]
            rounded[members[-1]][index] = otRound(total)
        done.extend(members)
    for variation, row in zip(variations, rounded):
        for index, value in enumerate(row):
            if value is not None:
                variation.coordinates[index] = value, variation.coordinates[index][1]


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
    with inter_exact_curve(inter) as exact:
        report.update(font_variations.remap_gvar(inter, base, exact=exact))
    return report


def load_base(region: str, italic: bool, inter_unicodes: set[int]) -> tuple[TTFont, dict[str, Any]]:
    # Italic shears every delta, so it needs them all explicit and exact: IUP
    # optimization would let inferred points stray up to half a unit first.
    base = TTFont(source_han_vf_path(region))
    base = instantiateVariableFont(base, AXIS_LIMIT, inplace=False, optimize=not italic)
    sarasa_report: dict[str, Any] = {}
    override_path = classical_vf_override_path(region)
    override: TTFont | None = None
    try:
        if override_path:
            override = TTFont(override_path)
            override = instantiateVariableFont(override, AXIS_LIMIT, inplace=False, optimize=not italic)
            sarasa_report.update(apply_classical_vf_override(base, override, region))
        public_axis_report = apply_public_weight_axis(base)
        sarasa_report.update(preserve_upstream_dash_metric_variations(base, base))
        if override is not None:
            apply_public_weight_axis(override)
            sarasa_report.update(preserve_upstream_dash_metric_variations(
                base, override, {"fullwidth", "fullwidth_two", "fullwidth_three",
                                 "vertical_single", "vertical_two", "vertical_three"}))
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
    path = INTER_ITALIC if italic else INTER_UPRIGHT
    inter = TTFont(path)
    inter = instantiateVariableFont(inter, INTER_AXIS_LIMIT, inplace=False, optimize=True)
    scale_inter_upem(inter)
    apply_inter_ui_recipe(inter)
    # Instancing and scale_upem round the default and every delta. Variable
    # controls follow the original curve; the rounded copy is only the default.
    inter._sarasa_exact_source = path
    return inter


def scale_inter_upem(inter: TTFont) -> None:
    """scale_upem to 1000, keeping what it rounds off variable positioning."""
    # scale_upem rounds every delta in place without renumbering VarIdx; keep
    # the unscaled positioning store so its curve is sampled before rounding.
    unscaled_store = copy.deepcopy(inter["GDEF"].table.VarStore) if "GDEF" in inter and getattr(inter["GDEF"].table, "VarStore", None) else None
    scale = 1000 / inter["head"].unitsPerEm
    unscaled_anchors = [(anchor, anchor.XCoordinate, anchor.YCoordinate)
                        for _name, _owner, _field, anchor in font_anchor_origins.anchor_slots(inter)]
    unscaled_values = ([(record, field, getattr(record, field, 0) or 0) for record, field, _device in variable_value_slots(inter["GPOS"].table)]
                       if "GPOS" in inter else [])
    scale_upem(inter, 1000)
    inter._sarasa_unscaled_gpos_store = (unscaled_store, scale) if unscaled_store is not None else None
    # What scale_upem rounded off each anchor's default (kept with the anchor
    # object it belongs to), so variable anchors can round once.
    inter._sarasa_anchor_fractions = {id(anchor): (anchor, x * scale - anchor.XCoordinate, y * scale - anchor.YCoordinate)
                                      for anchor, x, y in unscaled_anchors}
    # Variable kerning and other value records lose the same fraction.
    inter._sarasa_value_fractions = {(id(record), field): (record, value * scale - (getattr(record, field, 0) or 0))
                                     for record, field, value in unscaled_values}


def exact_inter_instance(inter: TTFont, weight: int) -> TTFont:
    """Inter at one weight, instanced from the original and rounded once to 1000 UPM.

    Instancing the scaled copy would sum its separately rounded default and
    deltas, up to 1.5 units off the curve the VF now follows.
    """
    path = getattr(inter, "_sarasa_exact_source", None)
    if path is None:
        return instantiateVariableFont(inter, {"wght": weight}, inplace=False, optimize=True)
    original = TTFont(path)
    try:
        fixed = {tag: value for tag, value in INTER_AXIS_LIMIT.items() if not isinstance(value, tuple)}
        instance = instantiateVariableFont(original, {**fixed, "wght": weight}, inplace=False, optimize=True)
    finally:
        original.close()
    scale_upem(instance, 1000)
    return instance


@contextmanager
def inter_exact_curve(inter: TTFont):
    path = getattr(inter, "_sarasa_exact_source", None)
    if path is None:
        yield None
        return
    original = TTFont(path)
    try:
        fixed = {tag: value for tag, value in INTER_AXIS_LIMIT.items() if not isinstance(value, tuple)}
        yield font_variations.ExactCurve(original, 1000 / original["head"].unitsPerEm, fixed)
    finally:
        original.close()


def apply_inter_ui_recipe(font: TTFont) -> None:
    # Sarasa pass1 bakes tnum before ss03/cv10. Only afterwards does this
    # project's documented proportional-default exception select pnum.
    bake_inter_ui_tnum_defaults(font)
    bake_inter_feature_defaults(font)
    apply_static_propdigits(font)


def bake_inter_feature_defaults(font: TTFont) -> dict[str, str]:
    # Sarasa bakes these presets by changing cmap identities. This preserves
    # each alternate's kerning/anchors and leaves component glyphs intact.
    presets = [get_single_substitution_mapping(font, tag) for tag in ("ss03", "cv10")]
    effective = {name: name for name in font.getBestCmap().values()}
    for substitutions in presets:
        effective = {original: substitutions.get(current, current) for original, current in effective.items()}
    effective = {original: target for original, target in effective.items() if original != target}
    # Width features must reach the baked identities, too. In Inter, ss03
    # changes comma/semicolon but their tnum inputs use the original names;
    # pnum must return to the baked proportional forms instead of undoing them.
    for index in feature_lookup_indices(font, "GSUB", {"tnum", "pnum"}):
        lookup = font["GSUB"].table.LookupList.Lookup[index]
        for subtable in lookup.SubTable:
            part = subtable.ExtSubTable if lookup.LookupType == 7 and subtable.ExtensionLookupType == 1 else subtable
            if lookup.LookupType != 1 and not (lookup.LookupType == 7 and subtable.ExtensionLookupType == 1):
                continue
            updates: dict[str, str] = {}
            for source, target in list(part.mapping.items()):
                source, target = effective.get(source, source), effective.get(target, target)
                if source in updates and updates[source] != target:
                    raise ValueError("Inter 预设与数字宽度替换存在冲突")
                updates[source] = target
            part.mapping.update(updates)
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap = {codepoint: effective.get(name, name) for codepoint, name in table.cmap.items()}
    return effective


def bake_inter_ui_tnum_defaults(font: TTFont) -> int:
    mapping = get_single_substitution_mapping(font, "tnum")
    if not mapping:
        return 0
    touched = 0
    skip = {0x2D}
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
    inter["GPOS"].ensureDecompiled()
    devices = collect_ot_variation_devices(inter["GPOS"].table)
    indices = sorted({(device.StartSize << 16) | device.EndSize for device in devices} - {0xFFFFFFFF})
    if not indices:
        return {0xFFFFFFFF: 0xFFFFFFFF}
    assert_public_axis_ready_for_inter_remap(base)
    store = getattr(inter["GDEF"].table, "VarStore", None) if "GDEF" in inter else None
    if store is None:
        raise ValueError("Inter GPOS 变化引用缺少 GDEF VarStore")
    if [axis.axisTag for axis in inter["fvar"].axes] != ["wght"]:
        raise ValueError("导入 Inter GPOS 前必须固定 opsz，仅保留 wght")
    axis = weight_axis(inter)
    inter_segment = inter["avar"].segments["wght"] if "avar" in inter else {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    base_axis = weight_axis(base)
    base_segment = base["avar"].segments["wght"]
    weights = set(SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT) | set(INTER_COMPOSITE_CONTROL_WEIGHTS)
    # Include every breakpoint of both coordinate mappings, every source
    # region and every control weight the outlines are corrected at; between
    # them the public axis is linear, Inter's curve need not be (Ā́ at 250).
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
    # Sample the store before scale_upem rounded each delta to 1000 UPM.
    master_store, scale = getattr(inter, "_sarasa_unscaled_gpos_store", None) or (store, 1.0)
    instancers = [VarStoreInstancer(master_store, inter["fvar"].axes, {"wght": vf_mapped_normalized_weight(inter, weight)}) for weight in weights]
    builder = OnlineVarStoreBuilder(["wght"])
    builder.setModel(model)
    remap = {}
    for index in indices:
        default, new_index = builder.storeMasters([instancer[index] * scale for instancer in instancers], round=otRound)
        if default:
            raise ValueError("Inter GPOS 变化数据在默认坐标不为零")
        remap[index] = new_index
    # A variable anchor rounds once: its scaled default lost a fraction that
    # separately rounded deltas add again, up to 1.4 units off Inter's curve
    # (Ā́ at 250). Each such anchor gets its own row of whole values on
    # neighbor-bounded tents, exact at every sampled weight.
    fractions = getattr(inter, "_sarasa_anchor_fractions", {})
    supports, master_indices = font_variations.adjacent_supports([{"wght": value} if value else {} for value in locations])
    builder.setSupports(supports)
    synthetic = 0xFF000000
    if indices[-1] >= synthetic:
        raise ValueError("Inter GPOS 变化索引超出锚点专用范围")
    rounded_once: set[int] = set()
    for _name, _owner, _field, anchor in font_anchor_origins.anchor_slots(inter):
        entry = fractions.get(id(anchor))
        if entry is None or entry[0] is not anchor or id(anchor) in rounded_once:
            continue
        rounded_once.add(id(anchor))
        for fraction, field in zip(entry[1:], ("XDeviceTable", "YDeviceTable")):
            device = getattr(anchor, field, None)
            if device is None or getattr(device, "DeltaFormat", None) != 0x8000:
                continue
            index = (device.StartSize << 16) | device.EndSize
            if index == 0xFFFFFFFF:
                continue
            values = [otRound(fraction + instancer[index] * scale) for instancer in instancers]
            deltas = [values[master] for master in master_indices]
            if not any(deltas):
                setattr(anchor, field, None)
                continue
            setattr(anchor, field, buildVarDevTable(synthetic))
            remap[synthetic] = builder.storeDeltas(deltas)
            synthetic += 1
    # Kerning rounds once the same way. On the model's own tents a control
    # weight also sums fractions of its neighbors' rows: ToWa at 700 kerned
    # o-W by -49.5 where Inter has -49.72 and the Bold static -50.
    value_fractions = getattr(inter, "_sarasa_value_fractions", {})
    for record, field, device_field in variable_value_slots(inter["GPOS"].table):
        entry = value_fractions.get((id(record), field))
        if entry is None or entry[0] is not record:
            continue
        device = getattr(record, device_field)
        index = (device.StartSize << 16) | device.EndSize
        if index == 0xFFFFFFFF:
            continue
        values = [otRound(entry[1] + instancer[index] * scale) for instancer in instancers]
        deltas = [values[master] for master in master_indices]
        if not any(deltas):
            setattr(record, device_field, None)
            continue
        setattr(record, device_field, buildVarDevTable(synthetic))
        remap[synthetic] = builder.storeDeltas(deltas)
        synthetic += 1
    additional = builder.finish()
    indices = sorted(remap)
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
    incoming = {}
    for language, features in font_layout.routes(source).items():
        incoming[language] = {}
        for tag, feature in features.items():
            if tag not in feature_tags:
                continue
            copied = ot.Feature()
            copied.FeatureParams = copy.deepcopy(feature.params)
            names_imported += import_layout_feature_names(base, inter, copied, name_id_remap)
            incoming[language][tag] = font_layout.Feature(
                tuple(lookup_index_map[index] for index in feature.lookups),
                copied.FeatureParams, feature.required,
            )
            imported_tags.add(tag)
    font_layout.merge_routes(target, incoming)
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
    # Metric curves must preserve every break in the actual glyph curves,
    # including Inter's avar knots, rather than connecting only named weights.
    weights = set(SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT)
    for variations in font["gvar"].variations.values():
        for variation in variations:
            for value in variation.axes.get("wght", ()):
                weight = font_variations.weight_at(font, value)
                weights.add(round(weight) if abs(weight - round(weight)) < .04 else round(weight, 5))
    weights = sorted(weights)
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
    supports, master_indices = font_variations.adjacent_supports(
        model.origLocations, [support["wght"][1] for support in model.supports[1:]])
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
            deltas = [values[index] - values[default_master_index] for index in master_indices]
            var_data_by_metric[metric].addItem(deltas, round=otRound)

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


def glyph_ink_offsets(glyph_set: Any, glyph_name: str) -> tuple[float, float, float] | None:
    """(ink xMin - control xMin, control yMax - ink yMax, ink xMin as drawn); None without ink.

    The drawn ink xMin is where engines render the glyph. In Sarasa's italic
    statics it can sit a unit right of hmtx LSB and the stale glyph header
    (K Light Italic ᄁ: LSB 256, points from 257).
    """
    ink, control = BoundsPen(glyph_set), ControlBoundsPen(glyph_set)
    glyph_set[glyph_name].draw(ink)
    glyph_set[glyph_name].draw(control)
    if ink.bounds is None:
        return None
    return ink.bounds[0] - control.bounds[0], control.bounds[3] - ink.bounds[3], ink.bounds[0]


REFERENCE_INK_OFFSETS: dict[tuple[str, int, int, str], tuple[float, float, float] | None] = {}


def reference_ink_offsets(reference: TTFont, glyph_set: Any, glyph_name: str) -> tuple[float, float, float] | None:
    # Native static references stay unchanged throughout a VF build.
    path = getattr(getattr(getattr(reference, "reader", None), "file", None), "name", None)
    if not isinstance(path, str) or not os.path.isfile(path):
        return glyph_ink_offsets(glyph_set, glyph_name)
    stat = os.stat(path)
    key = (path, stat.st_size, stat.st_mtime_ns, glyph_name)
    if key not in REFERENCE_INK_OFFSETS:
        REFERENCE_INK_OFFSETS[key] = glyph_ink_offsets(glyph_set, glyph_name)
    return REFERENCE_INK_OFFSETS[key]


def ink_positioned_codepoint(codepoint: int, italic: bool) -> bool:
    """Whether the horizontal position follows the native ink, not its control box.

    Sheared italic outlines and Inter's sources reach the same ink as the
    native static through different off-curve x extremes. So can a Source Han
    glyph the builder does not reserve for Source Han (〱〲〳, 5 units): the
    VF and static are separate quadratic conversions of Source Han's VF and
    static sources. Only glyphs Source Han overrides keep the control box.
    """
    return italic or use_inter_codepoint(codepoint)


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
    sources: dict[str, str] = {}
    ink_sources: dict[str, str] = {}
    italic = "post" in reference and bool(reference["post"].italicAngle)
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
        sources[glyph_name] = reference_name
        if ink_positioned_codepoint(codepoint, italic):
            ink_sources[glyph_name] = reference_name
    if 'GSUB' in font and 'GSUB' in reference:
        # Metric calibration must follow the same Inter glyph identities that
        # layout can reach. Nominal-only calibration leaves zero.slash and
        # at.case at their original origins after their base glyph was moved.
        owned = {cp for cp in cmap if use_inter_codepoint(cp)}
        layout_mapping = inter_layout_glyph_map(reference, font, codepoints=owned)
        encoded = set(cmap.values())
        for glyph_name, reference_name in layout_mapping.items():
            if glyph_name in encoded or reference_name not in reference[table_tag].metrics:
                continue
            targets[glyph_name] = tuple(int(value) for value in reference[table_tag].metrics[reference_name])
            sources[glyph_name] = reference_name
            ink_sources[glyph_name] = reference_name
    if table_tag == "hmtx" and ink_sources:
        # VF and static outlines can reach the same ink with different
        # off-curve extremes (italic; Inter's italic sources). Equal LSBs
        # (control-point xMin) then move the ink, by up to 18 units for '@'
        # and 8 for italic U+2030; place the native ink instead, where the
        # static draws it rather than where its LSB says.
        glyphs, reference_glyphs = font.getGlyphSet(), reference.getGlyphSet()
        for glyph_name, reference_name in ink_sources.items():
            own = glyph_ink_offsets(glyphs, glyph_name)
            native = reference_ink_offsets(reference, reference_glyphs, reference_name)
            if own is not None and native is not None:
                advance, _lsb = targets[glyph_name]
                targets[glyph_name] = (advance, otRound(native[2] - own[0]))
    if table_tag == "vmtx" and sources:
        # A TrueType vertical origin is the control-point yMax plus TSB, so
        # equal TSBs move the ink as well when one outline reaches its top
        # through an off-curve point: Inter's VF and static Ƴ/Ƙ hooks (up to
        # 9 units), Source Han's Heavy ﾍ conversion (50). Keep the native
        # static's ink below the origin, for every glyph.
        glyphs, reference_glyphs = font.getGlyphSet(), reference.getGlyphSet()
        for glyph_name, reference_name in sources.items():
            own = glyph_ink_offsets(glyphs, glyph_name)
            native = reference_ink_offsets(reference, reference_glyphs, reference_name)
            if own is not None and native is not None:
                advance, tsb = targets[glyph_name]
                targets[glyph_name] = (advance, otRound(tsb + native[1] - own[1]))
    if DASH_CMAP_CODEPOINTS <= set(cmap) and DASH_CMAP_CODEPOINTS <= set(reference_cmap):
        target_segments = vertical_dash_segments(font, upstream_dash_roles(font))
        reference_segments = vertical_dash_segments(reference, upstream_dash_roles(reference))
        if bool(target_segments) != bool(reference_segments):
            raise ValueError("VF 与静态度量参考的竖排分段覆盖不一致")
        if target_segments and reference_segments:
            # These glyphs have no cmap entries. They must join the same named
            # control-point calibration as encoded glyphs, especially italic
            # Source Han/VF whole-glyph translations.
            for role, glyph_name in target_segments.items():
                targets[glyph_name] = tuple(int(value) for value in
                    reference[table_tag].metrics[reference_segments[role]])
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
    protected_glyphs: set[str] | None = None,
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
                mismatches = {name: delta for name, delta in control_lsb_xmin_mismatches(instance, targets).items()
                              if name not in (protected_glyphs or set())}
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
    supports, master_indices = font_variations.adjacent_supports(
        model.origLocations, [support["wght"][1] for support in model.supports[1:]])
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
            deltas = [values[index] - values[default_master_index] for index in master_indices]
            var_data_by_metric[metric].addItem(deltas, round=otRound)

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
    flush_layout_aliases(font)
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
    advance_corrections = 0
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
        advance_pending: list[tuple[str, int, int, int]] = []
        mismatches_by_weight: dict[int, tuple[int, int]] = {}
        for weight in weights:
            hb_font.set_variations({"wght": weight})
            horizontal_count = 0
            vertical_count = 0
            for glyph_id, glyph_name in enumerate(glyph_order):
                for tag, expected_advance, actual_advance in (
                    ("HVAR", target_horizontal[weight]["advance"][glyph_id], hb_font.get_glyph_h_advance(glyph_id)),
                    ("VVAR", target_vertical[weight]["advance"][glyph_id], -hb_font.get_glyph_v_advance(glyph_id)),
                ):
                    if actual_advance != expected_advance:
                        advance_pending.append((tag, weight, glyph_id, expected_advance - actual_advance))
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
        if not horizontal_pending and not vertical_pending and not advance_pending:
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
                "harfbuzz_advance_corrections": advance_corrections,
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
        if any(weight == default_weight for _tag, weight, _gid, _delta in advance_pending):
            raise RuntimeError("HarfBuzz 默认步进与原生参考不一致")
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
                        "vertical_pending": [
                            {"weight": weight, "glyph": glyph_order[gid], "correction": correction,
                             "history": vertical_correction_history.get((weight, gid), [])}
                            for weight, gid, correction in vertical_pending[:24]
                        ],
                        "advance_pending": advance_pending[:24],
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
            f"vertical={len(vertical_pending)}, advance={len(advance_pending)}"
        )
        for tag, weight, glyph_id, correction in advance_pending:
            # These stores have one direct row per glyph and have not yet been
            # optimized. Calibrate the serialized runtime, including its own
            # normalization/rounding, instead of a FontTools-only estimate.
            font[tag].table.VarStore.VarData[0].Item[glyph_id][peak_columns[weight]] += correction
        advance_corrections += len(advance_pending)
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
        log_step(f"VF metrics: instantiate {len(weights)} control points")
        combined_master_metrics = []
        for weight in weights:
            master = instantiateVariableFont(
                font, {"wght": weight}, inplace=False, optimize=True,
            )
            masters.append(master)
            combined_master_metrics.append(combined_control_metrics(master, reference_fonts.get(weight)))
            # Retain only numeric measurements. Holding thirteen expanded CJK
            # TTFont objects needlessly multiplied the peak memory footprint.
            master.close()
            masters.pop()
        glyph_order = font.getGlyphOrder()
        log_step("VF metrics: scan horizontal and vertical control geometry")
        base_metrics = [horizontal_glyph_metrics(font, name) for name in glyph_order]
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
            # Only native static controls are independent metric targets.
            # An intermediate FontTools instance rounds IUP geometry before
            # measuring it; forcing HarfBuzz to that rounded copy can oscillate
            # by one unit and deform an otherwise correct Inter curve. Retain
            # every curve knot in HVAR/VVAR, and calibrate side bearings against
            # the six actual native references. The release audit compares
            # intermediate positions directly with Inter and across engines.
            reference_weights = sorted(reference_fonts)
            log_step("VF metrics: calibrate HarfBuzz against native reference weights")
            engine_metric_report = calibrate_harfbuzz_metric_controls(
                font,
                reference_weights,
                target_metrics,
                target_vertical_metrics,
                vvar_data_by_metric,
                peak_columns,
            )
            engine_metric_report["harfbuzz_metric_reference_weights"] = reference_weights
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
        return {f"{key}_feature_records_before_template": 0,
                f"{key}_feature_records_after_template": 0,
                f"{key}_empty_feature_records_added_from_template": 0,
                f"{key}_langsys_after_template": 0}
    table, template = font[table_tag].table, reference[table_tag].table
    before = len(table.FeatureList.FeatureRecord) if table.FeatureList else 0
    actual, required = font_layout.routes(table), font_layout.routes(template)
    selected = {}; empty = 0
    for (script, language), features in required.items():
        current = font_layout.selected(actual, script, language)
        selected[(script, language)] = {}
        for tag, expected in features.items():
            if use_reference_lookup_indices or not expected.lookups:
                chosen = expected
                empty += not bool(expected.lookups)
            elif tag in current:
                chosen = current[tag]
            else:
                continue
            selected[(script, language)][tag] = font_layout.Feature(
                chosen.lookups, chosen.params, expected.required,
            )
    font_layout.install(table, selected)
    return {f"{key}_feature_records_before_template": before,
            f"{key}_feature_records_after_template": len(table.FeatureList.FeatureRecord),
            f"{key}_empty_feature_records_added_from_template": empty,
            f"{key}_langsys_after_template": len(selected)}


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
    # A template can add fields or expand a shared value; it must never erase
    # per-glyph positioning or a field that is present in the actual lookup.
    value_format = getattr(subtable, "ValueFormat", 0) | getattr(reference, "ValueFormat", 0)
    glyphs = list(getattr(getattr(subtable, "Coverage", None), "glyphs", []) or [])
    source_format = getattr(subtable, "Format", 1)
    if source_format == 2:
        values = list(getattr(subtable, "Value", []) or [])
        if len(values) != len(glyphs):
            raise ValueError("GPOS SinglePos 格式 2 的覆盖与取值数量不一致")
    else:
        values = [getattr(subtable, "Value", None)] * len(glyphs)
    subtable.ValueFormat = value_format
    if source_format == 2 or reference_format == 2:
        subtable.Format = 2
        subtable.Value = [value_record_for_format(value, value_format) for value in values]
        subtable.ValueCount = len(subtable.Value)
    else:
        subtable.Format = 1
        subtable.Value = value_record_for_format(getattr(subtable, "Value", None), value_format)
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
    value_format1 = getattr(reference, "ValueFormat1", 0) | getattr(subtable, "ValueFormat1", 0)
    value_format2 = getattr(reference, "ValueFormat2", 0) | getattr(subtable, "ValueFormat2", 0)
    # Class positioning cannot be turned into a glyph-pair table by dropping
    # Class1Record. Keep its semantics even when a different weight uses a
    # different serialization format in the regional reference.
    if getattr(subtable, "Format", 1) == 2:
        reference_format = 2
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
    if "locl" in tags:
        raise ValueError("locl 必须按语言合并，不能全局广播")
    table = font[table_tag].table
    if not table.FeatureList or not table.ScriptList:
        return
    records = table.FeatureList.FeatureRecord
    scripts = {record.ScriptTag: record.Script for record in table.ScriptList.ScriptRecord}
    default = scripts.get("DFLT")
    def indices(system, tag):
        if system is None:
            return []
        values = list(system.FeatureIndex)
        if system.ReqFeatureIndex != 0xFFFF:
            values.append(system.ReqFeatureIndex)
        return [index for index in values if records[index].FeatureTag == tag]
    for tag in tags:
        fallback = indices(default.DefaultLangSys if default else None, tag)
        if not fallback:
            candidates = [i for i, record in enumerate(records) if record.FeatureTag == tag]
            if not candidates:
                continue
            first = records[candidates[0]].Feature
            if any(records[i].Feature != first for i in candidates[1:]):
                raise ValueError(f"{tag} 存在不同语言的规则，缺少明确的默认路由")
            fallback = candidates[:1]
        for script in scripts.values():
            preferred = indices(script.DefaultLangSys, tag) or fallback
            systems = ([script.DefaultLangSys] if script.DefaultLangSys else [])
            systems += [record.LangSys for record in script.LangSysRecord]
            for system in systems:
                if not indices(system, tag):
                    system.FeatureIndex = sorted(set(system.FeatureIndex) | set(preferred))
                    system.FeatureCount = len(system.FeatureIndex)


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


def vertical_dash_segment_points(
    points: list[tuple[float, float]], origin: float, em: int, italic: bool,
) -> dict[str, list[tuple[int, int]]]:
    """Each segment stays inside its own vertical cell; joins share exact points."""
    if len(points) != 4:
        raise ValueError("竖排破折号来源必须是四角轮廓")
    shear = math.tan(math.radians(9.4)) if italic else 0.0
    left = min(x - shear * y for x, y in points)
    right = max(x - shear * y for x, y in points)
    top = min(origin, max(y for _x, y in points))
    bottom = max(origin - em, min(y for _x, y in points))
    result = {}
    for role, upper, lower, cell in (
        ("begin", top, origin - em, 0),
        ("middle", origin, origin - em, 1),
        ("end2", origin, bottom, 1),
        ("end3", origin, bottom, 2),
    ):
        result[role] = [(otRound(x + shear * (y - cell * em)), otRound(y))
                        for x, y in ((left, lower), (right, lower), (right, upper), (left, upper))]
    return result


def dash_multiple_substitutions(font: TTFont) -> dict[str, list[str]]:
    result = {}
    for index in feature_lookup_indices(font, "GSUB", {"vert", "vrt2"}):
        lookup = font["GSUB"].table.LookupList.Lookup[index]
        for subtable in lookup.SubTable:
            part = subtable.ExtSubTable if lookup.LookupType == 7 and subtable.ExtensionLookupType == 2 else subtable
            if lookup.LookupType == 2 or (lookup.LookupType == 7 and subtable.ExtensionLookupType == 2):
                result.update(getattr(part, "mapping", {}))
    return result


def vertical_dash_segments(font: TTFont, roles: dict[str, str]) -> dict[str, str] | None:
    mappings = dash_multiple_substitutions(font)
    two = mappings.get(roles["vertical_two"])
    three = mappings.get(roles["vertical_three"])
    if two and three and len(two) == 2 and len(three) == 3 and two[0] == three[0]:
        return {"begin": two[0], "middle": three[1], "end2": two[1], "end3": three[2]}
    return None


def create_vertical_dash_segments(font: TTFont, roles: dict[str, str], italic: bool) -> dict[str, Any]:
    existing = vertical_dash_segments(font, roles)
    if existing:
        return {"vertical_dash_segments": existing, "vertical_dash_segments_reused": True}
    source_name = roles["vertical_single"]
    em = int(font["head"].unitsPerEm)
    variable = "fvar" in font and "gvar" in font
    weights = list(EM_DASH_PROBE_WEIGHTS) if variable else [int(font["OS/2"].usWeightClass)]
    default_weight = int(weight_axis(font).defaultValue) if variable else weights[0]
    data = serialized_font_bytes(font)
    runtime = hb.Font(hb.Face(data))
    source_gid = font.getGlyphID(source_name)
    samples: dict[int, dict[str, list[tuple[int, int]]]] = {}
    origins = {}
    for weight in weights:
        runtime.set_variations({"wght": weight})
        origin = int(runtime.get_glyph_v_origin(source_gid)[1])
        glyph_set = font.getGlyphSet(location={"wght": weight} if variable else None)
        pen = RecordingPen()
        glyph_set[source_name].draw(pen)
        if any(operator not in {"moveTo", "lineTo", "closePath"} for operator, _values in pen.value):
            raise ValueError("竖排破折号来源包含意外的曲线或组件")
        points = [point for operator, values in pen.value if operator in {"moveTo", "lineTo"} for point in values]
        samples[weight] = vertical_dash_segment_points(points, origin, em, italic)
        origins[weight] = origin
    names = {}
    order = font.getGlyphOrder()
    for role in ("begin", "middle", "end2", "end3"):
        name = f"pd.vertical-dash.{role}"
        if name in order:
            raise ValueError("发现没有完整布局引用的竖排分段字形")
        names[role] = name
        glyph = make_polygon_glyph(samples[default_weight][role])
        font["glyf"].glyphs[name] = glyph
        glyph.recalcBounds(font["glyf"])
        font["hmtx"].metrics[name] = (em, glyph.xMin)
        font["vmtx"].metrics[name] = (em, origins[default_weight] - glyph.yMax)
        if "VORG" in font:
            font["VORG"].VOriginRecords[name] = origins[default_weight]
        order.append(name)
    font.setGlyphOrder(order)
    if variable:
        correction_weights = [weight for weight in weights if weight != default_weight]
        supports = advance_supports(font, correction_weights)
        for role, name in names.items():
            coordinates = {weight: samples[weight][role] + [(0, 0), (em, 0), (0, origins[weight]), (0, origins[weight] - em)] for weight in weights}
            baseline = coordinates[default_weight]
            # Independent neighbor-bounded tents reproduce each integer master
            # exactly. Rounding residuals in a broad overlapping VariationModel
            # made narrow rectangles wider and let their joins diverge.
            font["gvar"].variations[name] = [TupleVariation({"wght": supports[weight]},
                [(x - base_x, y - base_y) for (x, y), (base_x, base_y) in zip(coordinates[weight], baseline)])
                for weight in correction_weights]
    # Keep the existing single-substitution role identities as harmless 1em
    # intermediates. Their old 2em/3em outlines must not inflate head bounds.
    for role in ("vertical_two", "vertical_three"):
        target = roles[role]
        font["glyf"].glyphs[target] = copy.deepcopy(font["glyf"][source_name])
        font["hmtx"].metrics[target] = copy.deepcopy(font["hmtx"].metrics[source_name])
        font["vmtx"].metrics[target] = copy.deepcopy(font["vmtx"].metrics[source_name])
        if variable:
            font["gvar"].variations[target] = copy.deepcopy(font["gvar"].variations.get(source_name, []))
        if "VORG" in font:
            font["VORG"].VOriginRecords[target] = origins[default_weight]
    install_vertical_dash_segment_layout(font, roles, names)
    font["maxp"].numGlyphs = len(font.getGlyphOrder())
    return {"vertical_dash_segments": names, "vertical_dash_segment_weights": weights,
            "vertical_dash_segment_advance": em, "vertical_dash_long_outlines_replaced": 2}


def install_vertical_dash_segment_layout(font: TTFont, roles: dict[str, str], names: dict[str, str]) -> None:
    multiple = ot.MultipleSubst()
    multiple.mapping = {roles[key]: [names["begin"], names["end2"]]
                        for key in ("encoded_two", "fullwidth_two", "vertical_two")}
    multiple.mapping.update({roles[key]: [names["begin"], names["middle"], names["end3"]]
                             for key in ("encoded_three", "fullwidth_three", "vertical_three")})
    lookup = ot.Lookup(); lookup.LookupType = 2; lookup.LookupFlag = 0
    lookup.SubTable = [multiple]; lookup.SubTableCount = 1
    index = append_gsub_lookup(font, lookup)
    for tag in ("vert", "vrt2"):
        link_lookup_to_all_gsub_features(font, tag, index)
    gdef = font["GDEF"].table if "GDEF" in font else None
    classes = getattr(getattr(gdef, "GlyphClassDef", None), "classDefs", None)
    if classes is not None:
        classes.update({name: 1 for name in names.values()})


def cursive_anchor_signature(anchor: Any, store: Any) -> Any:
    if anchor is None:
        return None
    result = [int(anchor.XCoordinate), int(anchor.YCoordinate)]
    for field in ("XDeviceTable", "YDeviceTable"):
        device = getattr(anchor, field, None)
        if device is None:
            result.append(())
        elif device.DeltaFormat != 0x8000:
            result.append((device.DeltaFormat, device.StartSize, device.EndSize, tuple(device.DeltaValue)))
        else:
            if store is None:
                raise ValueError("连接锚点缺少 GDEF 变化存储")
            data = store.VarData[device.StartSize]
            deltas = {}
            for region, delta in zip(data.VarRegionIndex, data.Item[device.EndSize]):
                key = tuple((axis.StartCoord, axis.PeakCoord, axis.EndCoord)
                            for axis in store.VarRegionList.Region[region].VarRegionAxis)
                deltas[key] = deltas.get(key, 0) + delta
            result.append(tuple(sorted((key, value) for key, value in deltas.items() if value)))
    return tuple(result)


def install_continuous_vertical_dash_layout(font: TTFont) -> dict[str, Any]:
    """Join any number of existing segments without changing outlines or hint."""
    roles = upstream_dash_roles(font)
    names = vertical_dash_segments(font, roles)
    if not names:
        raise ValueError("缺少竖排分段，不能安装连续连接规则")
    if "GPOS" not in font:
        font["GPOS"] = newTable("GPOS"); font["GPOS"].table = ot.GPOS()
        table = font["GPOS"].table; table.Version = 0x00010000
        table.ScriptList = copy.deepcopy(font["GSUB"].table.ScriptList)
        table.FeatureList = ot.FeatureList(); table.FeatureList.FeatureRecord = []; table.FeatureList.FeatureCount = 0
        table.LookupList = ot.LookupList(); table.LookupList.Lookup = []; table.LookupList.LookupCount = 0
        for script in table.ScriptList.ScriptRecord:
            for langsys in ([script.Script.DefaultLangSys] if script.Script.DefaultLangSys else []) + [row.LangSys for row in script.Script.LangSysRecord]:
                langsys.ReqFeatureIndex = 0xFFFF; langsys.FeatureIndex = []; langsys.FeatureCount = 0
    singles = {roles[key] for key in ("proportional", "fullwidth", "vertical_single", "korean_single") if key in roles}
    for mapping in dash_locl_mappings_by_language(font, roles).values():
        if roles["proportional"] in mapping:
            singles.add(mapping[roles["proportional"]])
    segments = set(names.values())
    members = segments | singles
    original_gpos = {
        index: copy.deepcopy(lookup) for index, lookup in enumerate(font["GPOS"].table.LookupList.Lookup)
        if not (lookup.LookupType == 3 and len(lookup.SubTable) == 1
                and set(lookup.SubTable[0].Coverage.glyphs) == segments)
    }
    original_store = copy.deepcopy(getattr(font["GDEF"].table, "VarStore", None)) if "GDEF" in font else None
    lookups = font["GSUB"].table.LookupList.Lookup

    def context(mapping, backtrack=(), lookahead=()):
        inputs = set(mapping)
        expected = ([set(backtrack)] if backtrack else [], [inputs], [set(lookahead)] if lookahead else [])
        for index, existing in enumerate(lookups):
            if existing.LookupType != 6 or len(existing.SubTable) != 1:
                continue
            part = existing.SubTable[0]
            if getattr(part, "Format", None) != 3 or part.SubstCount != 1:
                continue
            actual = tuple([set(cov.glyphs) for cov in getattr(part, field)]
                           for field in ("BacktrackCoverage", "InputCoverage", "LookAheadCoverage"))
            if actual == expected and part.SubstLookupRecord[0].SequenceIndex == 0:
                child = lookups[part.SubstLookupRecord[0].LookupListIndex]
                current = {key: value for table in child.SubTable for key, value in getattr(table, "mapping", {}).items()}
                if child.LookupType == 1 and current == mapping:
                    return index
        substitution = append_gsub_lookup(font, make_single_substitution_lookup(mapping))
        part = ot.ChainContextSubst(); part.Format = 3
        part.BacktrackCoverage = [coverage(font, backtrack)] if backtrack else []
        part.InputCoverage = [coverage(font, inputs)]
        part.LookAheadCoverage = [coverage(font, lookahead)] if lookahead else []
        part.BacktrackGlyphCount = len(part.BacktrackCoverage); part.InputGlyphCount = 1
        part.LookAheadGlyphCount = len(part.LookAheadCoverage)
        record = ot.SubstLookupRecord(); record.SequenceIndex = 0; record.LookupListIndex = substitution
        part.SubstLookupRecord = [record]; part.SubstCount = 1
        lookup = ot.Lookup(); lookup.LookupType = 6; lookup.LookupFlag = 0
        lookup.SubTable = [part]; lookup.SubTableCount = 1
        return append_gsub_lookup(font, lookup)

    indices = [
        context({names["begin"]: names["middle"], **{name: names["end2"] for name in singles}}, backtrack=members),
        context({name: names["begin"] for name in singles}, lookahead=members),
        context({names["end2"]: names["middle"], names["end3"]: names["middle"]}, lookahead=members),
    ]
    for tag in ("vert", "vrt2"):
        for index in indices:
            link_lookup_to_all_gsub_features(font, tag, index)

    # Contour-point anchors alone do not follow gvar when an engine has no
    # ppem. Carry the actual point deltas in GDEF, preserving Inter's store.
    variable = "gvar" in font and "fvar" in font
    store_builder = OnlineVarStoreBuilder([axis.axisTag for axis in font["fvar"].axes]) if variable else None
    devices = []
    subtable = ot.CursivePos(); subtable.Format = 1; subtable.Coverage = coverage(font, segments)
    subtable.EntryExitRecord = []
    for name in subtable.Coverage.glyphs:
        coordinates, controls = font["glyf"]._getCoordinatesAndControls(name, font["hmtx"].metrics, font["vmtx"].metrics)
        outline = coordinates[:-4]
        top = min((i for i, point in enumerate(outline) if point[1] == max(y for x, y in outline)), key=lambda i: outline[i][0])
        bottom = min((i for i, point in enumerate(outline) if point[1] == min(y for x, y in outline)), key=lambda i: outline[i][0])
        variations = copy.deepcopy(font["gvar"].variations.get(name, [])) if variable else []
        for variation in variations:
            variation.calcInferredDeltas(coordinates, controls.endPts)
        if store_builder is not None:
            store_builder.setSupports([variation.axes for variation in variations])
        def anchor(point):
            result = ot.Anchor(); result.Format = 1
            result.XCoordinate, result.YCoordinate = outline[point]
            if variations:
                result.Format = 3; result.XDeviceTable = None; result.YDeviceTable = None
                for coordinate, field in enumerate(("XDeviceTable", "YDeviceTable")):
                    deltas = [variation.coordinates[point][coordinate] for variation in variations]
                    if any(deltas):
                        device = buildVarDevTable(store_builder.storeDeltas(deltas, round=otRound))
                        setattr(result, field, device); devices.append(device)
            return result
        record = ot.EntryExitRecord()
        record.EntryAnchor = None if name == names["begin"] else anchor(top)
        record.ExitAnchor = anchor(bottom) if name in {names["begin"], names["middle"]} else None
        subtable.EntryExitRecord.append(record)
    subtable.EntryExitCount = len(subtable.EntryExitRecord)
    additional = store_builder.finish() if devices else None
    gpos = font["GPOS"].table
    existing_indices = [index for index, item in enumerate(gpos.LookupList.Lookup)
                        if item.LookupType == 3 and len(item.SubTable) == 1
                        and set(item.SubTable[0].Coverage.glyphs) == segments]
    if len(existing_indices) > 1:
        raise ValueError("竖排连接定位记录重复")
    existing_store = getattr(font["GDEF"].table, "VarStore", None) if "GDEF" in font else None
    def signature(part, store):
        return [(name, cursive_anchor_signature(record.EntryAnchor, store),
                 cursive_anchor_signature(record.ExitAnchor, store))
                for name, record in zip(part.Coverage.glyphs, part.EntryExitRecord)]
    unchanged = bool(existing_indices and signature(gpos.LookupList.Lookup[existing_indices[0]].SubTable[0], existing_store)
                     == signature(subtable, additional))
    if devices and not unchanged:
        if "GDEF" not in font:
            font["GDEF"] = newTable("GDEF"); font["GDEF"].table = ot.GDEF()
            for field in ("GlyphClassDef", "AttachList", "LigCaretList", "MarkAttachClassDef", "MarkGlyphSetsDef"):
                setattr(font["GDEF"].table, field, None)
        gdef = font["GDEF"].table
        existing = getattr(gdef, "VarStore", None)
        values = [(device.StartSize << 16) | device.EndSize for device in devices]
        if existing is not None:
            additional, values = append_item_variation_store(existing, additional, values)
        gdef.Version = 0x00010003; gdef.VarStore = additional
        for device, value in zip(devices, values):
            device.StartSize, device.EndSize = value >> 16, value & 0xFFFF
    lookup = ot.Lookup(); lookup.LookupType = 3; lookup.LookupFlag = 0
    lookup.SubTable = [subtable]; lookup.SubTableCount = 1
    if existing_indices:
        index = existing_indices[0]
        if not unchanged:
            gpos.LookupList.Lookup[index] = lookup
    else:
        index = append_gpos_lookup(font, lookup)
    feature_indices = [i for i, record in enumerate(gpos.FeatureList.FeatureRecord) if record.FeatureTag == "curs"]
    feature_index = feature_indices[0] if feature_indices else insert_gpos_feature(font, "curs", [index])
    feature = gpos.FeatureList.FeatureRecord[feature_index].Feature
    if index not in feature.LookupListIndex:
        feature.LookupListIndex.append(index); feature.LookupCount = len(feature.LookupListIndex)
    # Joining is required positioning, including engines which do not enable
    # optional vertical kerning. Existing required features retain their rules.
    for script in gpos.ScriptList.ScriptRecord:
        for langsys in ([script.Script.DefaultLangSys] if script.Script.DefaultLangSys else []) + [row.LangSys for row in script.Script.LangSysRecord]:
            if langsys.ReqFeatureIndex == 0xFFFF:
                langsys.ReqFeatureIndex = feature_index
            elif langsys.ReqFeatureIndex != feature_index:
                required = gpos.FeatureList.FeatureRecord[langsys.ReqFeatureIndex].Feature
                if index not in required.LookupListIndex:
                    required.LookupListIndex.append(index); required.LookupCount = len(required.LookupListIndex)
    if any(lookup != gpos.LookupList.Lookup[index] for index, lookup in original_gpos.items()):
        raise RuntimeError("连续竖排连接修改了已有的非连接 GPOS lookup")
    if original_store is not None:
        current = font["GDEF"].table.VarStore
        if (list(current.VarData[:len(original_store.VarData)]) != list(original_store.VarData)
                or list(current.VarRegionList.Region[:len(original_store.VarRegionList.Region)]) != list(original_store.VarRegionList.Region)):
            raise RuntimeError("连续竖排连接修改了已有的 GPOS 变化数据")
    return {"continuous_vertical_dash_joining": True, "vertical_join_substitution_lookups": indices,
            "vertical_join_positioning_lookup": index, "vertical_join_variable_anchors": len(devices),
            "existing_positioning_lookups_preserved": len(original_gpos), "existing_layout_variations_preserved": True}


def recalculate_head_bounds(font: TTFont) -> dict[str, Any]:
    boxes = []
    for name in font.getGlyphOrder():
        glyph = font["glyf"][name]
        if getattr(glyph, "numberOfContours", 0):
            # Native Sarasa keeps conservative bounds for rounded italic
            # coordinates. Changing xMin without its metric phantom origin
            # shifts the rendered glyph even when all points are unchanged.
            # Outline producers update their own bounds; head only aggregates
            # them, calculating missing bounds for newly constructed glyphs.
            if not all(hasattr(glyph, field) for field in ("xMin", "yMin", "xMax", "yMax")):
                glyph.recalcBounds(font["glyf"])
            boxes.append((glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax))
    if not boxes:
        raise ValueError("字体没有可计算包围盒的轮廓")
    bounds = (min(box[0] for box in boxes), min(box[1] for box in boxes),
              max(box[2] for box in boxes), max(box[3] for box in boxes))
    for attribute, value in zip(("xMin", "yMin", "xMax", "yMax"), bounds):
        setattr(font["head"], attribute, value)
    return {"head_bounds_recalculated": list(bounds), "head_bounds_glyphs_checked": len(font.getGlyphOrder())}


def continuous_vertical_dash_structure_status(font: TTFont) -> dict[str, Any]:
    try:
        tags = ("GSUB", "GPOS", "GDEF")
        before = {tag: font.getTableData(tag) for tag in tags if tag in font}
        probe = TTFont(); probe.setGlyphOrder(font.getGlyphOrder())
        for tag in tags:
            if tag in font:
                probe[tag] = copy.deepcopy(font[tag])
        for tag in ("cmap", "glyf", "hmtx", "vmtx", "head", "fvar", "gvar"):
            if tag in font:
                probe[tag] = font[tag]
        install_continuous_vertical_dash_layout(probe)
        after = {tag: probe.getTableData(tag) for tag in tags if tag in probe}
        return {"ok": before == after, "reason": "" if before == after else "连续竖排连接规则、变化锚点或必要特性引用不完整"}
    except (KeyError, ValueError, AttributeError) as error:
        return {"ok": False, "reason": str(error)}


def align_vertical_dash_geometry_to_static(font: TTFont, reference_fonts: dict[int, TTFont]) -> dict[str, Any]:
    """Build the new segment family from native static control-point geometry."""
    if "fvar" not in font or "gvar" not in font:
        raise ValueError("竖排分段轴校准要求 VF")
    names = vertical_dash_segments(font, upstream_dash_roles(font))
    controls = [int(stop["value"]) for stop in SOURCE_HAN_WEIGHT_STOPS]
    if not names or not set(controls) <= set(reference_fonts):
        raise ValueError("竖排分段缺少完整的原生静态控制点")
    em = int(font["head"].unitsPerEm)
    runtime = hb.Font(hb.Face(serialized_font_bytes(font)))
    locations = {}
    for weight in EM_DASH_PROBE_WEIGHTS:
        runtime.set_variations({"wght": weight})
        locations[weight] = float(runtime.get_var_coords_normalized()[0])
    control_points = {}; control_origins = {}
    for weight in controls:
        reference = reference_fonts[weight]
        reference_names = vertical_dash_segments(reference, upstream_dash_roles(reference))
        if not reference_names:
            raise ValueError("静态控制点缺少竖排分段")
        control_points[weight] = {}; control_origins[weight] = {}
        for role, reference_name in reference_names.items():
            if reference["vmtx"].metrics[reference_name][0] != em:
                raise ValueError("静态竖排分段不是 1em")
            corners = rectangular_corner_indices(reference, reference_name)
            glyph = reference["glyf"][reference_name]
            control_points[weight][role] = [tuple(glyph.coordinates[corners[corner]])
                                           for corner in ((0, 0), (1, 0), (1, 1), (0, 1))]
            control_origins[weight][role] = glyph_vertical_origin(reference, reference_name)
    default_weight = int(weight_axis(font).defaultValue)
    ordered = sorted((coordinate, weight) for weight, coordinate in locations.items())
    supports = {weight: (ordered[index - 1][0] if index else -1.0, coordinate,
                        ordered[index + 1][0] if index + 1 < len(ordered) else 1.0)
                for index, (coordinate, weight) in enumerate(ordered) if weight != default_weight}
    for role, name in names.items():
        coordinates = {}
        for weight, location in locations.items():
            points = [tuple(otRound(piecewiseLinearMap(location,
                       {locations[control]: control_points[control][role][point][axis] for control in controls}))
                       for axis in (0, 1)) for point in range(4)]
            origin = otRound(piecewiseLinearMap(location,
                             {locations[control]: control_origins[control][role] for control in controls}))
            coordinates[weight] = points + [(0, 0), (em, 0), (0, origin), (0, origin - em)]
        baseline = coordinates[default_weight]
        glyph = make_polygon_glyph(baseline[:4])
        default_reference = reference_fonts[default_weight]
        default_names = vertical_dash_segments(default_reference, upstream_dash_roles(default_reference))
        source_flags = default_reference["glyf"][default_names[role]].flags
        glyph.flags[0] |= int(source_flags[0]) & glyf_table.flagOverlapSimple
        glyph.recalcBounds(font["glyf"])
        font["glyf"].glyphs[name] = glyph
        font["hmtx"].metrics[name] = (em, glyph.xMin)
        font["vmtx"].metrics[name] = (em, baseline[-2][1] - glyph.yMax)
        if "VORG" in font:
            set_glyph_vertical_origin(font, name, baseline[-2][1])
        font["gvar"].variations[name] = [TupleVariation({"wght": support},
            [(x - bx, y - by) for (x, y), (bx, by) in zip(coordinates[weight], baseline)])
            for weight, support in supports.items()]
    return {"vertical_segment_native_controls": controls,
            "vertical_segment_runtime_controls": list(EM_DASH_PROBE_WEIGHTS),
            "vertical_segment_glyphs": list(names.values())}


def finalize_variable_vertical_segments(font: TTFont, reference_fonts: dict[int, TTFont]) -> dict[str, Any]:
    """Update only the four segment glyphs and their HVAR side-bearing rows."""
    report = align_vertical_dash_geometry_to_static(font, reference_fonts)
    names = report["vertical_segment_glyphs"]
    variations = font["gvar"].variations
    supports = [variation.axes for variation in variations[names[0]]]
    rows = []
    for name in names:
        if [variation.axes for variation in variations[name]] != supports:
            raise ValueError("竖排片段没有共享轴控制区间")
        rows.extend([
            [int(variation.coordinates[0][0]) for variation in variations[name]],
            [-int(variation.coordinates[2][0]) for variation in variations[name]],
        ])
    extra = var_builder.buildVarStore(
        var_builder.buildVarRegionList(supports, ["wght"]),
        [var_builder.buildVarData(list(range(len(supports))), rows, optimize=False)],
    )
    hvar = font["HVAR"].table
    hvar.VarStore, indices = append_item_variation_store(hvar.VarStore, extra, list(range(len(rows))))
    for index, name in enumerate(names):
        hvar.LsbMap.mapping[name] = indices[2 * index]
        hvar.RsbMap.mapping[name] = indices[2 * index + 1]
    old_maps = {attribute: dict(getattr(hvar, attribute).mapping)
                for attribute in ("AdvWidthMap", "LsbMap", "RsbMap")}
    remap = hvar.VarStore.optimize(use_NO_VARIATION_INDEX=False)
    for attribute, mapping in old_maps.items():
        setattr(hvar, attribute, var_builder.buildVarIdxMap(
            [remap[mapping[name]] for name in font.getGlyphOrder()], font.getGlyphOrder()))
    report.update(recalculate_head_bounds(font))
    report["vertical_segment_hvar_rows_updated"] = len(rows)
    return report


def align_source_punctuation_advances(font: TTFont, region: str) -> dict[str, Any]:
    """Retain Source Han advances at intermediate public-axis control points."""
    weights = (200, 250, 300, 325, 350, 375, 400, 500, 600, 650, 700, 800, 900)
    runtime = hb.Font(hb.Face(serialized_font_bytes(font)))
    source_path = source_han_vf_path(region)
    source = hb.Font(hb.Face(source_path.read_bytes()))
    if runtime.face.upem != source.face.upem:
        raise ValueError("标点度量来源与成品的 UPM 不一致")

    def single_glyph(shaper: Any, text: str, language: str) -> int:
        buffer = hb.Buffer(); buffer.add_str(text)
        buffer.script = "Latn"; buffer.language = language; buffer.direction = "ltr"
        hb.shape(shaper, buffer)
        if len(buffer.glyph_infos) != 1:
            raise ValueError("横排标点来源没有返回单一字形")
        return buffer.glyph_infos[0].codepoint

    glyph_sources: dict[str, set[int]] = {}
    for language in ("en", "ko", "zh-Hans", "zh-Hant", "zh-HK", "ja"):
        for text in ("…", "—", "⸺", "⸻"):
            name = font.getGlyphName(single_glyph(runtime, text, language))
            glyph_sources.setdefault(name, set()).add(single_glyph(source, text, language))
    normalized = {}; expected = {}; actual = {}
    with TTFont(source_path, lazy=True) as metadata:
        axis = weight_axis(metadata)
        avar = metadata["avar"].segments["wght"] if "avar" in metadata else {-1: -1, 0: 0, 1: 1}
        mapped = {public: piecewiseLinearMap(normalize_axis_value(internal, axis.minValue, axis.defaultValue, axis.maxValue), avar)
                  for public, internal in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT.items()}
        for weight in weights:
            internal = SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT.get(weight)
            if internal is None:
                coordinate = piecewiseLinearMap(piecewiseLinearMap(weight, mapped), {value: key for key, value in avar.items()})
                internal = denormalize_axis_value(coordinate, axis.minValue, axis.defaultValue, axis.maxValue)
            source.set_variations({"wght": internal}); runtime.set_variations({"wght": weight})
            normalized[weight] = runtime.get_var_coords_normalized()[0]
            expected[weight] = {}; actual[weight] = {}
            for name, source_gids in glyph_sources.items():
                values = {source.get_glyph_h_advance(gid) for gid in source_gids}
                if len(values) != 1:
                    raise ValueError("不同语言的来源宽度不能合并到同一标点字形")
                expected[weight][name] = values.pop()
                actual[weight][name] = runtime.get_glyph_h_advance(font.getGlyphID(name))
    changed = [name for name in glyph_sources if any(expected[w][name] != actual[w][name] for w in weights)]
    report = {"source_punctuation_advance_control_weights": list(weights),
              "source_punctuation_advance_glyphs_aligned": len(changed)}
    if not changed:
        return report
    # Published controls are already tied to native static output. This final
    # correction must not silently replace that independently checked contract.
    for weight in SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT:
        if any(expected[weight][name] != actual[weight][name] for name in changed):
            raise ValueError("Source Han 标点宽度与既有公开字重控制点冲突")
    supports = []; peaks = []
    for index, weight in enumerate(weights):
        peak = normalized[weight]
        if peak:
            supports.append({"wght": (normalized[weights[max(0, index - 1)]], peak,
                                        normalized[weights[min(index + 1, len(weights) - 1)]])})
            peaks.append(weight)
    hvar = font["HVAR"].table
    order = font.getGlyphOrder()
    if hvar.AdvWidthMap is None:
        hvar.AdvWidthMap = var_builder.buildVarIdxMap(list(range(len(order))), order)
    if hvar.LsbMap is None or hvar.RsbMap is None:
        raise ValueError("标点校准需要重建后的完整 HVAR 侧边距映射")
    old_region_count = len(hvar.VarStore.VarRegionList.Region)
    regions = copy.deepcopy(hvar.VarStore.VarRegionList)
    regions.Region.extend(var_builder.buildVarRegionList(supports, ["wght"]).Region)
    regions.RegionCount = len(regions.Region)

    def coefficients(index: int) -> list[int]:
        result = [0] * regions.RegionCount
        if index != 0xFFFFFFFF:
            data = hvar.VarStore.VarData[index >> 16]
            for column, delta in zip(data.VarRegionIndex, data.Item[index & 0xFFFF]):
                result[column] += delta
        return result

    rows = []
    for name in changed:
        base = font["hmtx"].metrics[name][0]
        if expected[400][name] != base:
            raise ValueError("标点默认宽度与 hmtx 不一致")
        advance = [0] * old_region_count + [expected[weight][name] - base for weight in peaks]
        old_advance = coefficients(hvar.AdvWidthMap.mapping[name])
        old_rsb = coefficients(hvar.RsbMap.mapping[name])
        # RSB receives exactly the advance-curve difference. LSB and all
        # outline data remain unchanged, at every coordinate between controls.
        rows.extend([advance, [r + a - o for r, a, o in zip(old_rsb, advance, old_advance)]])
    extra = var_builder.buildVarStore(regions, [var_builder.buildVarData(list(range(regions.RegionCount)), rows, optimize=False)])
    hvar.VarStore, indices = append_item_variation_store(hvar.VarStore, extra, list(range(len(rows))))
    for index, name in enumerate(changed):
        hvar.AdvWidthMap.mapping[name] = indices[2 * index]
        hvar.RsbMap.mapping[name] = indices[2 * index + 1]
    remap = hvar.VarStore.optimize(use_NO_VARIATION_INDEX=False)
    for attribute in ("AdvWidthMap", "LsbMap", "RsbMap"):
        previous = getattr(hvar, attribute).mapping
        setattr(hvar, attribute, var_builder.buildVarIdxMap([remap[previous[name]] for name in order], order))
    checked = hb.Font(hb.Face(serialized_font_bytes(font)))
    for weight in weights:
        checked.set_variations({"wght": weight})
        if any(checked.get_glyph_h_advance(font.getGlyphID(name)) != expected[weight][name] for name in changed):
            raise RuntimeError("Source Han 标点宽度控制点校准失败")
    return report


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
    mapping: dict[str, str] = {}
    for subtable in single_substitution_subtables(lookup):
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
        for lookup_index in calt_referenced_substitution_lookups(font)
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
    for lookup_index in sorted(calt_referenced_substitution_lookups(font)):
        lookup = gsub.LookupList.Lookup[lookup_index]
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
            otRound(source_x + (source_y - source["head"].unitsPerEm / 3) * shear),
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
    western_source: TTFont | None = None,
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

    if region == "CL" and western_source is None:
        raise ValueError("CL 静态破折号必须同时提供 Source Han K 与 Shanggu 来源")
    western_source = western_source or source
    western_roles = upstream_dash_roles(western_source)
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
        western = role in {"proportional", "encoded_two", "encoded_three"}
        outline_source = western_source if western else source
        source_name = (western_roles if western else source_roles)[role]
        copy_static_dash_outline(
            font,
            target_name,
            outline_source,
            source_name,
            italic=italic,
        )
        copied += 1

    source_to_target = {
        western_roles[role]: roles[role]
        for role in western_roles
        if role in roles
    }
    localized_mappings: dict[str, dict[str, str]] = {}
    localized_clones = 0
    source_localized_mappings = normalized_source_han_dash_locl_mappings(western_source, western_roles)
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
                    western_source,
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
    ccmp_sources = {roles["proportional"], roles["fullwidth"], roles["vertical_single"]}
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
    if linked or has_feature(font, tag):
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
    regional_features = set()
    explicit_features = {index for record in gsub.ScriptList.ScriptRecord
                         for entry in record.Script.LangSysRecord for index in entry.LangSys.FeatureIndex}
    for index, record in enumerate(gsub.FeatureList.FeatureRecord):
        if record.FeatureTag != "locl":
            continue
        combined = {}
        for lookup in record.Feature.LookupListIndex:
            combined.update(lookup_single_substitution_mapping(gsub, lookup))
        reusable_default = (index not in explicit_features and combined
                            and set(combined.items()) <= set(mapping.items()))
        if combined == mapping or reusable_default:
            regional_features.add(index)
    removed_english = 0
    for record in gsub.ScriptList.ScriptRecord:
        script = record.Script
        if script.DefaultLangSys is not None:
            script.DefaultLangSys = copy.deepcopy(script.DefaultLangSys)
            default = script.DefaultLangSys
            punctuation_sources = {roles["proportional"], *upstream_dash_roles(font).values()}
            def punctuation_only(index: int) -> bool:
                feature_record = gsub.FeatureList.FeatureRecord[index]
                if feature_record.FeatureTag != "locl":
                    return False
                entries = {}
                for lookup in feature_record.Feature.LookupListIndex:
                    entries.update(lookup_single_substitution_mapping(gsub, lookup))
                return bool(entries) and set(entries) <= punctuation_sources
            default.FeatureIndex = [index for index in default.FeatureIndex
                                    if index not in regional_features and not punctuation_only(index)]
            default.FeatureCount = len(default.FeatureIndex)
            # The former implementation added ENG as an exception to its
            # incorrect default routing. An equivalent default needs no alias.
            if record.ScriptTag in {"DFLT", "latn"}:
                kept = []
                for entry in script.LangSysRecord:
                    if (entry.LangSysTag == "ENG "
                            and entry.LangSys.ReqFeatureIndex == default.ReqFeatureIndex
                            and entry.LangSys.FeatureIndex == default.FeatureIndex):
                        removed_english += 1
                    else:
                        kept.append(entry)
                script.LangSysRecord = kept
                script.LangSysCount = len(kept)
    if regional_features:
        feature_index = min(regional_features)
        feature = gsub.FeatureList.FeatureRecord[feature_index].Feature
        feature.LookupListIndex = list(lookup_indices)
        feature.LookupCount = len(lookup_indices)
    else:
        feature_index = insert_gsub_feature_record(font, "locl", lookup_indices)
    scripts = {record.ScriptTag: record for record in gsub.ScriptList.ScriptRecord}
    template = scripts.get("hani") or scripts.get("latn") or scripts.get("DFLT")
    if template is None or template.Script.DefaultLangSys is None:
        raise ValueError("缺少地区文字系统的默认布局模板")
    for tag in ("bopo", "hang", "hani", "kana"):
        if tag not in scripts:
            record = ot.ScriptRecord()
            record.ScriptTag = tag
            record.Script = copy.deepcopy(template.Script)
            gsub.ScriptList.ScriptRecord.append(record)
            scripts[tag] = record
        script = scripts[tag].Script
        if script.DefaultLangSys is None:
            script.DefaultLangSys = copy.deepcopy(template.Script.DefaultLangSys)
        default = script.DefaultLangSys
        default.FeatureIndex = [index for index in default.FeatureIndex if index != feature_index] + [feature_index]
        default.FeatureCount = len(default.FeatureIndex)
    gsub.ScriptList.ScriptRecord.sort(key=lambda record: record.ScriptTag)
    gsub.ScriptList.ScriptCount = len(gsub.ScriptList.ScriptRecord)
    return {"default_regional_punctuation": True, "default_punctuation_language": language.strip(),
            "default_punctuation_scripts": ["bopo", "hang", "hani", "kana"],
            "redundant_english_langsys_removed": removed_english}


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


def ensure_vertical_presentation_dash_ligatures(font: TTFont, roles: dict[str, str]) -> dict[str, Any]:
    existing = ligature_outputs_for_feature(font, "ccmp", roles["vertical_single"])
    if existing.get(2) == roles["vertical_two"] and existing.get(3) == roles["vertical_three"]:
        return {"vertical_presentation_dash_ligatures_added": False}
    rules = {roles["vertical_single"]: [
        ([roles["vertical_single"], roles["vertical_single"]], roles["vertical_three"]),
        ([roles["vertical_single"]], roles["vertical_two"]),
    ]}
    added = make_ligature_substitution_lookup(rules).SubTable[0].ligatures
    for index in feature_lookup_indices(font, "GSUB", {"ccmp"}):
        for subtable in ligature_substitution_subtables(font["GSUB"].table.LookupList.Lookup[index]):
            if roles["proportional"] in subtable.ligatures:
                subtable.ligatures.update(added)
                return {"vertical_presentation_dash_ligatures_added": True}
    raise ValueError("缺少能在竖排分段之前执行的破折号 ccmp lookup")


def apply_upstream_dash_behavior(
    font: TTFont,
    region: str,
    *,
    static_source: TTFont | None = None,
    western_source: TTFont | None = None,
    italic: bool = False,
    prepared_layout: tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]] | None = None,
) -> dict[str, Any]:
    if "GSUB" not in font or "glyf" not in font:
        return {"upstream_dash_behavior_applied": False}
    try:
        existing_roles = upstream_dash_roles(font)
        existing_segments = vertical_dash_segments(font, existing_roles)
    except ValueError:
        existing_segments = None
    if prepared_layout is not None:
        existing_roles, prepared_mappings, existing_segments = prepared_layout
        required = set(existing_roles.values()) | set(existing_segments.values())
        required.update(target for mapping in prepared_mappings.values() for target in mapping.values())
        if not required <= set(font.getGlyphOrder()):
            raise ValueError("静态布局处理丢失了 hint 前已准备的破折号字形")
    report: dict[str, Any] = {}
    if existing_segments:
        # Restore final routing after template alignment while retaining every
        # prepared outline and instruction byte. Only lookups are rebuilt.
        roles = existing_roles
        localized_mappings = (prepared_mappings if prepared_layout is not None
                              else normalized_source_han_dash_locl_mappings(font, roles))
        old_indices = {index for index, lookup in enumerate(font["GSUB"].table.LookupList.Lookup)
                       if lookup.LookupType == 2 and any(
                           any(name in set(existing_segments.values()) for name in sequence)
                           for part in lookup.SubTable for sequence in part.mapping.values())}
        remove_gsub_lookups(font, old_indices)
        report["upstream_dash_existing_segments_preserved"] = True
    elif static_source is not None:
        roles, localized_mappings, static_report = prepare_static_upstream_dash_glyphs(
            font,
            static_source,
            region,
            italic,
            western_source,
        )
        report.update(static_report)
    else:
        roles = upstream_dash_roles(font)
        localized_mappings = normalized_source_han_dash_locl_mappings(font, roles)

    _obsolete_glyphs, cleanup_report = strip_legacy_dash_layout(font, roles)
    report.update(cleanup_report)
    missing_languages = CJK_LOCL_LANGUAGES - set(localized_mappings)
    if missing_languages:
        raise ValueError("missing upstream dash locl mappings for: " + ", ".join(sorted(missing_languages)))

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
    # HarfBuzz can apply Unicode vertical presentation fallback before GSUB
    # when only vrt2 is enabled. Its FE31 input must join by the same rule.
    ccmp_rules[roles["vertical_single"]] = [
        ([roles["vertical_single"], roles["vertical_single"]], roles["vertical_three"]),
        ([roles["vertical_single"]], roles["vertical_two"]),
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
    if existing_segments:
        install_vertical_dash_segment_layout(font, roles, existing_segments)
        report["vertical_dash_segments"] = existing_segments
    else:
        report.update(create_vertical_dash_segments(font, roles, italic))
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


VALUE_DEVICE_FIELDS = (("XPlacement", "XPlaDevice"), ("YPlacement", "YPlaDevice"),
                       ("XAdvance", "XAdvDevice"), ("YAdvance", "YAdvDevice"))


def variable_value_slots(table: Any) -> list[tuple[Any, str, str]]:
    """Each (ValueRecord, value field, device field) whose value has a variation index."""
    slots: list[tuple[Any, str, str]] = []
    seen: set[int] = set()

    def visit(value: Any) -> None:
        if value is None or isinstance(value, (str, int, float, bool, bytes)) or id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, ot.ValueRecord):
            for field, device_field in VALUE_DEVICE_FIELDS:
                device = getattr(value, device_field, None)
                if device is not None and getattr(device, "DeltaFormat", None) == 0x8000:
                    slots.append((value, field, device_field))
            return
        if hasattr(value, "ensureDecompiled"):
            value.ensureDecompiled()
        if isinstance(value, dict):
            items = list(value.values())
        elif isinstance(value, (list, tuple)):
            items = list(value)
        elif hasattr(value, "__dict__"):
            items = [item for key, item in vars(value).items() if not key.startswith("_") and key not in {"reader", "font"}]
        else:
            items = []
        for item in items:
            visit(item)

    visit(table)
    return slots


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
    if roles["proportional"] == roles["fullwidth"]:
        reasons.append("Western and CJK single dash must have distinct glyphs")
    for language in sorted(CJK_LOCL_LANGUAGES):
        mapping = actual_locl.get(language, {})
        expected_single = roles["fullwidth"]
        if language == "KOR ":
            expected_single = mapping.get(roles["proportional"])
            if not expected_single or expected_single in {roles["proportional"], roles["fullwidth"]}:
                reasons.append("KOR locl lacks Source Han's narrow single-dash alternate")
        expected = {
            roles["proportional"]: expected_single,
            roles["encoded_two"]: roles["fullwidth_two"],
            roles["encoded_three"]: roles["fullwidth_three"],
        }
        if mapping != expected:
            reasons.append(f"{language} dash locl mapping {mapping!r} != {expected!r}")

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
        roles["vertical_two"]: ("vmtx", upem),
        roles["vertical_three"]: ("vmtx", upem),
    }
    for glyph_name, (table_tag, expected_advance) in expected_metrics.items():
        actual = int(font[table_tag].metrics[glyph_name][0])
        if actual != expected_advance:
            reasons.append(
                f"{glyph_name} {table_tag} advance {actual} != {expected_advance}"
            )
    segments = vertical_dash_segments(font, roles)
    mappings = dash_multiple_substitutions(font)
    if not segments or len(set(segments.values())) != 4:
        reasons.append("missing complete 1em vertical dash segment topology")
    else:
        for role in ("encoded_two", "fullwidth_two", "vertical_two"):
            if mappings.get(roles[role]) != [segments["begin"], segments["end2"]]:
                reasons.append(f"{role} is not split into the two expected vertical segments")
        for role in ("encoded_three", "fullwidth_three", "vertical_three"):
            if mappings.get(roles[role]) != [segments["begin"], segments["middle"], segments["end3"]]:
                reasons.append(f"{role} is not split into the three expected vertical segments")
        for name in segments.values():
            if font["vmtx"].metrics[name][0] != upem:
                reasons.append(f"{name} vertical advance is not 1em")

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
        "vertical_segments": segments,
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


def inter_layout_glyph_map(font: TTFont, source: TTFont, *, codepoints: set[int] | None = None) -> dict[str, str]:
    """Match layout identities through cmap and feature edges, including post 3."""
    names = set(font.getGlyphOrder())
    result = {name: prefixed(name) for name in source.getGlyphOrder() if prefixed(name) in names}
    cmap = font.getBestCmap()
    for cp, name in source.getBestCmap().items():
        if cp in cmap and (codepoints is None or cp in codepoints):
            result[name] = cmap[cp]
    tags = {record.FeatureTag for record in source["GSUB"].table.FeatureList.FeatureRecord}
    edges = [(font_layout.feature_substitution_edges(source, tag), font_layout.feature_substitution_edges(font, tag)) for tag in sorted(tags)]
    for _iteration in range(16):
        before = len(result)
        for (source_mapping, source_ligatures), (target_mapping, target_ligatures) in edges:
            reverse = {target: original for original, target in target_mapping.items()}
            for original, target in source_mapping.items():
                if original in result and result[original] in target_mapping:
                    result.setdefault(target, target_mapping[result[original]])
                if target in result and result[target] in reverse:
                    result.setdefault(original, reverse[result[target]])
            for sequence, replacement in source_ligatures.items():
                if all(name in result for name in sequence):
                    target_name = target_ligatures.get(tuple(result[name] for name in sequence))
                    if target_name is not None:
                        result.setdefault(replacement, target_name)
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


def link_complete_inter_calt(font: TTFont, source: TTFont, glyph_map: dict[str, str]) -> dict[str, int]:
    """Keep the imported Inter feature active after regional template matching."""
    def ligatures(lookup: Any) -> set[tuple[str, ...]]:
        return {(first, *item.Component, item.LigGlyph)
                for table in ligature_substitution_subtables(lookup)
                for first, items in table.ligatures.items() for item in items}

    # Sarasa deliberately keeps CJK-owned dash forms. Those Source Han
    # identities must not be treated as Inter inputs to contextual arrows.
    eligible = {name for cp, name in source.getBestCmap().items()
                if cp in font.getBestCmap() and use_inter_codepoint(cp)}
    single_edges = [get_single_substitution_mapping(source, tag)
                    for tag in {record.FeatureTag for record in source["GSUB"].table.FeatureList.FeatureRecord}]
    while True:
        expanded = eligible | {target for mapping in single_edges for original, target in mapping.items() if original in eligible}
        if expanded == eligible:
            break
        eligible = expanded
    expected = set()
    for index in feature_lookup_indices(source, "GSUB", {"calt"}):
        for rule in ligatures(source["GSUB"].table.LookupList.Lookup[index]):
            if all(name in eligible for name in rule[:-1]) and all(name in glyph_map for name in rule):
                expected.add(tuple(glyph_map[name] for name in rule))
    if not expected:
        return {"inter_calt_ligatures_verified": 0, "inter_calt_lookups_linked": 0}
    table = font["GSUB"].table
    eligible_targets = {glyph_map[name] for name in eligible if name in glyph_map}
    def inter_context(lookup: Any) -> bool:
        if lookup.LookupType not in {5, 6, 7}:
            return False
        inputs = set()
        for raw in lookup.SubTable:
            part = getattr(raw, "ExtSubTable", raw)
            coverages = getattr(part, "InputCoverage", None)
            first = coverages[0] if coverages else getattr(part, "Coverage", None)
            if isinstance(first, list):
                first = first[0] if first else None
            if first is not None:
                inputs.update(first.glyphs)
        return bool(inputs) and inputs <= eligible_targets
    selected = set(); present = set()
    for record in table.FeatureList.FeatureRecord:
        if record.FeatureTag != "calt":
            continue
        rules = set().union(*(ligatures(table.LookupList.Lookup[index]) for index in record.Feature.LookupListIndex))
        if rules & expected:
            selected.update(index for index in record.Feature.LookupListIndex
                            if ligatures(table.LookupList.Lookup[index]) & expected
                            or inter_context(table.LookupList.Lookup[index]))
            present.update(rules)
    if expected - present:
        raise ValueError("缺少固定 Inter 的 calt 连字规则，不能签发完整布局校验记录")
    # Source Han and Inter may have separate calt records with identical
    # consumers. A template choosing the Source Han record must still retain
    # the complete imported Inter lookup family, including contextual arrows.
    merge_gsub_lookup_indices_into_features(font, "calt", sorted(selected))
    for record in table.FeatureList.FeatureRecord:
        if record.FeatureTag == "calt":
            record.Feature.LookupListIndex = sorted(set(record.Feature.LookupListIndex))
            record.Feature.LookupCount = len(record.Feature.LookupListIndex)
    return {"inter_calt_ligatures_verified": len(expected), "inter_calt_lookups_linked": len(selected)}


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
    # Inter keeps colon, contextual case and their guards in one lookup.
    # Project that complete rule family; deleting a lookup merely because one
    # of its inputs is colon also deletes the other contextual substitutions.
    source_rules = []
    source_mapping = {}
    for index in feature_lookup_indices(source, "GSUB", {"calt"}):
        lookup = source["GSUB"].table.LookupList.Lookup[index]
        if lookup.LookupType != 6:
            continue
        for subtable in lookup.SubTable:
            if getattr(subtable, "Format", None) != 3 or subtable.InputGlyphCount != 1:
                continue
            source_rules.append(subtable)
            for record in subtable.SubstLookupRecord:
                child = source["GSUB"].table.LookupList.Lookup[record.LookupListIndex]
                if child.LookupType != 1 or record.SequenceIndex != 0:
                    raise RuntimeError("固定 Inter 的上下文大小写规则结构发生变化")
                for part in child.SubTable:
                    source_mapping.update(part.mapping)
    substitutions = {
        glyph_map[original]: glyph_map[target]
        for original, target in source_mapping.items()
        if original in glyph_map and target in glyph_map
    }
    if colon not in substitutions:
        raise RuntimeError("缺少 Inter 原有的上浮冒号字形")
    cleanup = remove_inter_contextual_case_rules(font, substitutions)
    single = ot.Lookup(); single.LookupType = 1; single.LookupFlag = 0
    subst = ot.SingleSubst(); subst.mapping = substitutions
    single.SubTable = [subst]; single.SubTableCount = 1
    single_index = append_gsub_lookup(font, single)
    rules = []
    defaults = {}
    for tag in ("ss03", "cv10"):
        mapping = get_single_substitution_mapping(source, tag)
        defaults = {original: mapping.get(target, target) for original, target in defaults.items()}
        defaults.update({original: target for original, target in mapping.items() if original not in defaults})
    for subtable in source_rules:
        rule = copy.deepcopy(subtable)
        inputs = {glyph_map[name] for name in subtable.InputCoverage[0].glyphs if name in glyph_map}
        inputs &= substitutions.keys()
        if not inputs:
            continue
        rule.InputCoverage = [coverage(font, inputs)]
        for attribute in ("BacktrackCoverage", "LookAheadCoverage"):
            coverages = []
            for cov in getattr(subtable, attribute):
                # Baking ss03/cv10 changes nominal glyphs, not their letter
                # context. Preserve the guards for both original and baked IDs.
                names = set(cov.glyphs) | {defaults[name] for name in cov.glyphs if name in defaults}
                coverages.append(coverage(font, {glyph_map[name] for name in names if name in glyph_map}))
            setattr(rule, attribute, coverages)
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
    inter_calt_report = link_complete_inter_calt(font, source, glyph_map)
    # Remove only genuinely unreferenced lookup objects. Feature records and
    # language-system indices stay intact, including native empty records.
    gsub = font["GSUB"].table
    roots = {index for record in gsub.FeatureList.FeatureRecord for index in record.Feature.LookupListIndex}
    retained = set(gsub.LookupList.closure_lookups(sorted(roots)))
    remove_gsub_lookups(font, set(range(len(gsub.LookupList.Lookup))) - retained)
    return {"digit_colon_feature_added": True, "digit_colon_context": "complete-inter-contextual-case", "digit_colon_rules": sum(colon in rule.InputCoverage[0].glyphs for rule in rules), "inter_contextual_case_rules": len(rules), "inter_contextual_non_colon_rules": sum(any(name != colon for name in rule.InputCoverage[0].glyphs) for rule in rules), **cleanup, **inter_calt_report}


def expanded_chain_context_rules(font: TTFont, subtable: Any) -> list[Any]:
    """Expand context formats 1/2 without losing class-zero complements/order."""
    if subtable.Format == 3:
        return [subtable]
    result = []
    universe = set(font.getGlyphOrder())
    def make(first: set[str], rule: Any, back: list[set[str]], inputs: list[set[str]], ahead: list[set[str]]) -> None:
        if not first:
            return
        item = ot.ChainContextSubst(); item.Format = 3
        item.InputCoverage = [coverage(font, names) for names in [first, *inputs]]
        item.BacktrackCoverage = [coverage(font, names) for names in back]
        item.LookAheadCoverage = [coverage(font, names) for names in ahead]
        item.InputGlyphCount = len(item.InputCoverage)
        item.BacktrackGlyphCount = len(item.BacktrackCoverage)
        item.LookAheadGlyphCount = len(item.LookAheadCoverage)
        item.SubstLookupRecord = copy.deepcopy(rule.SubstLookupRecord)
        item.SubstCount = len(item.SubstLookupRecord)
        result.append(item)
    if subtable.Format == 1:
        for first, rules in zip(subtable.Coverage.glyphs, subtable.ChainSubRuleSet):
            if rules is not None:
                for rule in rules.ChainSubRule:
                    make({first}, rule, [{name} for name in rule.Backtrack], [{name} for name in rule.Input], [{name} for name in rule.LookAhead])
    elif subtable.Format == 2:
        classes = {}
        def members(definition: Any, number: int) -> set[str]:
            key = (id(definition), number)
            if key not in classes:
                classes[key] = {name for name in universe if definition.classDefs.get(name, 0) == number}
            return classes[key]
        initial = set(subtable.Coverage.glyphs)
        for number, rules in enumerate(subtable.ChainSubClassSet):
            if rules is not None:
                first = initial & members(subtable.InputClassDef, number)
                for rule in rules.ChainSubClassRule:
                    make(first, rule, [members(subtable.BacktrackClassDef, value) for value in rule.Backtrack],
                         [members(subtable.InputClassDef, value) for value in rule.Input],
                         [members(subtable.LookAheadClassDef, value) for value in rule.LookAhead])
    else:
        raise ValueError(f"unsupported chained-context format {subtable.Format}")
    return result


def remove_inter_contextual_case_rules(font: TTFont, substitutions: dict[str, str]) -> dict[str, int]:
    """Replace owned case/colon rules without deleting mixed or shared rules."""
    gsub = font["GSUB"].table
    roots = [gsub.LookupList.Lookup[index] for index in feature_lookup_indices(font, "GSUB", {"calt"})]
    removed = 0
    for lookup in roots:
        if lookup.LookupType != 6 or lookup.LookupFlag != 0:
            continue
        kept = []
        changed = False
        for subtable in lookup.SubTable:
            remaining = []
            subtable_changed = False
            for rule in expanded_chain_context_rules(font, subtable):
                inputs = set(rule.InputCoverage[0].glyphs)
                owned = inputs & substitutions.keys() if rule.InputGlyphCount == 1 else set()
                for record in rule.SubstLookupRecord:
                    child = gsub.LookupList.Lookup[record.LookupListIndex]
                    mapping = {}
                    for part in single_substitution_subtables(child):
                        if hasattr(part, "mapping"):
                            mapping.update(part.mapping)
                    owned = {name for name in owned if record.SequenceIndex == 0 and mapping.get(name) == substitutions[name]}
                if owned:
                    removed += 1
                    subtable_changed = True
                unowned = inputs - owned
                if unowned:
                    if owned:
                        rule = copy.deepcopy(rule)
                        rule.InputCoverage[0] = coverage(font, unowned)
                    remaining.append(rule)
            kept.extend(remaining if subtable_changed else [subtable])
            changed = changed or subtable_changed
        if not changed:
            continue
        index = gsub.LookupList.Lookup.index(lookup)
        replacement = None
        if kept:
            clone = copy.deepcopy(lookup)
            clone.SubTable = copy.deepcopy(kept)
            clone.SubTableCount = len(kept)
            replacement = append_gsub_lookup(font, clone)
            changed = move_gsub_lookups_before(font, [replacement], index)
            index, replacement = changed[index], changed[replacement]
        for record in gsub.FeatureList.FeatureRecord:
            if record.FeatureTag == "calt":
                record.Feature.LookupListIndex = [replacement if item == index else item for item in record.Feature.LookupListIndex if item != index or replacement is not None]
                record.Feature.LookupCount = len(record.Feature.LookupListIndex)
    return {"inter_contextual_case_rules_replaced": removed}


def source_dash_locl_by_language(font: TTFont, roles: dict[str, str]) -> dict[str, dict[str, str]]:
    """Dash-only locl mapping reached from each CJK language system."""
    sources = {roles["proportional"], roles["encoded_two"], roles["encoded_three"]}
    gsub = font["GSUB"].table
    result: dict[str, dict[str, str]] = {}
    for language in sorted(CJK_LOCL_LANGUAGES):
        mappings = []
        for langsys in langsys_records_for_language(font, language):
            mapping: dict[str, str] = {}
            for feature_index in list(langsys.FeatureIndex or []):
                record = gsub.FeatureList.FeatureRecord[feature_index]
                if record.FeatureTag != "locl":
                    continue
                for lookup_index in list(record.Feature.LookupListIndex or []):
                    for subtable in single_substitution_subtables(gsub.LookupList.Lookup[lookup_index]):
                        mapping.update({source: target for source, target in (getattr(subtable, "mapping", {}) or {}).items()
                                        if source in sources})
            if mapping:
                mappings.append(mapping)
        if mappings:
            if any(mapping != mappings[0] for mapping in mappings[1:]):
                raise ValueError(f"inconsistent Source Han dash locl for language {language}")
            result[language] = mappings[0]
    return result


VERTICAL_ROUTE_FEATURES = ("vert", "vrt2")
# Different vertical forms of one character differ by hundreds of units (an
# upright corner colon, a rotated one); one form's VF and static outlines
# stay within a few units of each other.
VERTICAL_FORM_BOX_TOLERANCE = 50


def unify_reference_vertical_routes(font: TTFont, reference: TTFont) -> dict[str, int]:
    """Route vertical forms for each language as the native static does.

    The layout template keeps the VF's own Source Han lookups per language,
    but Source Han routes some vertical forms per language: its Korean VF
    leaves ！：；？ upright for JAN/ZHH/ZHT and rotates the Japanese colon,
    while Sarasa K substitutes the same forms for every language; Sarasa J
    rotates the colon (Source Han J's JAN lookup) and substitutes ！；？
    (its KOR/ZHS lookup) for every language. Languages the native static
    routes identically share the VF route, or the union of two VF routes,
    whose forms agree with it, preferring the script default.
    """
    if "GSUB" not in font or "GSUB" not in reference:
        return {"vertical_routes_unified": 0}
    table, template = font["GSUB"].table, reference["GSUB"].table
    actual, expected = font_layout.routes(table), font_layout.routes(template)
    cmap, reference_cmap = font.getBestCmap() or {}, reference.getBestCmap() or {}
    codepoints = sorted(set(cmap) & set(reference_cmap))
    reverse: dict[str, int] = {}
    reference_reverse: dict[str, int] = {}
    for codepoint in sorted(cmap):
        reverse.setdefault(cmap[codepoint], codepoint)
    for codepoint in sorted(reference_cmap):
        reference_reverse.setdefault(reference_cmap[codepoint], codepoint)
    glyph_sets = (font.getGlyphSet(), reference.getGlyphSet())
    boxes: dict[tuple[int, str], tuple[float, ...] | None] = {}

    def box(side: int, name: str) -> tuple[float, ...] | None:
        if (side, name) not in boxes:
            pen = BoundsPen(glyph_sets[side])
            glyph_sets[side][name].draw(pen)
            boxes[side, name] = pen.bounds
        return boxes[side, name]

    def same(name: str, reference_name: str) -> bool:
        # An encoded result names the same glyph in both fonts; positional
        # names of unencoded forms do not, so compare what they draw.
        if name in reverse and reverse[name] == reference_reverse.get(reference_name):
            return True
        own, native = box(0, name), box(1, reference_name)
        return own is not None and native is not None and all(
            abs(left - right) <= VERTICAL_FORM_BOX_TOLERANCE for left, right in zip(own, native))

    unified = 0
    mismatches: list[str] = []
    for tag in VERTICAL_ROUTE_FEATURES:
        # Any script's route is a candidate: these single substitutions do
        # not depend on the script, and Source Han's TC VF routes ！：；？ to
        # their vertical forms only for KOR/ZHS, never in DFLT. A union keeps
        # lookup order, so Source Han J's JAN colon precedes its KOR forms.
        routes = sorted({features[tag].lookups for features in actual.values() if tag in features})
        unions = {tuple(sorted(set(left) | set(right))) for index, left in enumerate(routes) for right in routes[index + 1:]}
        effects = {lookups: font_layout.single_substitution_effect(table, lookups) for lookups in [*routes, *sorted(unions)]}
        for script in sorted({script for script, _language in expected}):
            classes: dict[tuple, tuple[dict[str, str], list[str]]] = {}
            for (route_script, language), features in sorted(expected.items()):
                if route_script != script or tag not in features:
                    continue
                lookups = features[tag].lookups
                effect = font_layout.single_substitution_effect(template, lookups)
                key = (tuple(sorted(effect.items())), font_layout.contextual_lookups(template, lookups))
                classes.setdefault(key, (effect, []))[1].append(language)
            for wanted, languages in classes.values():
                members = {language: actual[(script, language)][tag] for language in languages
                           if tag in actual.get((script, language), {})}
                if not members:
                    continue
                # A shared route must not change any member's contextual
                # lookups (the continuous dash runs are routed separately).
                contexts = {font_layout.contextual_lookups(table, feature.lookups) for feature in members.values()}
                eligible = [lookups for lookups in effects
                            if len(contexts) == 1 and font_layout.contextual_lookups(table, lookups) in contexts]
                if not eligible:
                    continue
                considered = [cp for cp in codepoints if reference_cmap[cp] in wanted
                              or any(cmap[cp] in effects[lookups] for lookups in eligible)]
                agreement = {lookups: [same(effects[lookups].get(cmap[cp], cmap[cp]),
                                            wanted.get(reference_cmap[cp], reference_cmap[cp])) for cp in considered]
                             for lookups in eligible}
                usage = {lookups: sum(feature.lookups == lookups for feature in members.values()) for lookups in eligible}
                default = members["dflt"].lookups if "dflt" in members else None
                best = min(eligible, key=lambda lookups: (
                    -sum(agreement[lookups]), lookups != default, -usage[lookups], len(lookups), lookups))
                for language, feature in members.items():
                    if feature.lookups != best:
                        actual[(script, language)][tag] = font_layout.Feature(best, feature.params, feature.required)
                        unified += 1
                # Only forms some VF route reaches depend on this choice;
                # forms no route changes keep their own identities.
                differing = [f"U+{cp:04X}" for index, cp in enumerate(considered)
                             if not agreement[best][index] and any(agreement[lookups][index] for lookups in eligible)]
                if differing:
                    mismatches.append(f"{script}/{'/'.join(sorted(members))}/{tag}: {differing[:6]}")
    if unified:
        font_layout.install(table, actual)
    if mismatches:
        raise ValueError("VF 竖排替换与原生静态参考不一致：" + "; ".join(mismatches[:8]))
    return {"vertical_routes_unified": unified}


def align_variable_layout_to_reference(font: TTFont, reference: TTFont) -> dict[str, int]:
    # calt is already attached before subsetting. Keep those live feature
    # references; old numeric lookup IDs are invalid after renumbering.
    # Sarasa routes no locl for a region's own languages (K: JAN/KOR), yet the
    # final dash pass derives every language's dash form, including the KOR
    # single-dash alternate, from Source Han's locl. Carry exactly the dash
    # mappings the template drops; that pass then strips and rebuilds them.
    try:
        roles = upstream_dash_roles(font)
    except ValueError:
        roles = None
    before = source_dash_locl_by_language(font, roles) if roles else {}
    report = {
        **align_layout_feature_template(font, reference, "GSUB"),
        **align_layout_feature_template(font, reference, "GPOS"),
    }
    report.update(unify_reference_vertical_routes(font, reference))
    after = source_dash_locl_by_language(font, roles) if roles else {}
    carried = {language: mapping for language, mapping in before.items() if after.get(language) != mapping}
    report["source_dash_locl_languages_carried"] = len(carried)
    if carried:
        report.update(add_language_specific_locl(font, carried, report_prefix="source_dash_carried",
                                                 purpose="Source Han dash"))
    return report


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
    skip_metric_codepoints = PROPDIGITS_CODEPOINTS | DASH_CMAP_CODEPOINTS
    try:
        alias_report = split_reference_cmap_aliases(base, reference)
        alias_mapping_report = align_reference_cmap_alias_mappings(
            base, reference, skip_metric_codepoints, getattr(base, "_sarasa_classical_cmap", None))
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
        layout_template_report = align_variable_layout_to_reference(base, reference)
        gdef_report = rebuild_gdef_from_reference(base, reference)
        vorg_report = rebuild_vorg_from_reference(base, reference)
        metadata_report = sync_sarasa_metadata_from_reference(base, reference)
    finally:
        for reference_font in reference_fonts.values():
            reference_font.close()
    update_vf_names(base, region, italic)
    update_fvar_instances(base, region, italic)
    update_style_flags(base, italic)
    update_os2_sarasa_metadata(base, region)
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
        final_em_dash_ligature_report.update(align_vertical_dash_geometry_to_static(base, final_metric_references))
        lsb_xmin_alignment_report = align_variable_outlines_to_lsb(
            base,
            final_metric_references,
            protected_glyphs=set(final_em_dash_ligature_report["vertical_segment_glyphs"]),
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
    final_inter_bound_report.update(preserve_inter_advance_curves(base, region, italic))
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
    vorgless_report.update(align_source_punctuation_advances(base, region))
    vorgless_report.update(align_inter_attachment_origins(base, italic))
    vorgless_report.update(align_tabular_alternate_advances(base))
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
    vorgless_report.update(normalize_variable_raster_metadata(base))
    vorgless_report.update(prune_unreferenced_variable_names(base))
    vorgless_report.update(recalculate_head_bounds(base))
    vorgless_report.update(install_continuous_vertical_dash_layout(base))
    vorgless_report.update(normalize_ideograph_gdef_classes(base))
    vorgless_report["gpos_pair_sets_sorted"] = font_layout.sort_pair_sets(base)
    vorgless_report["unreferenced_layout_lookups_removed"] = font_layout.prune_unreferenced_lookups(base)
    base["OS/2"].usMaxContext = maxCtxFont(base)
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
        verify_record=False,
    )
    if not final_validation_ok:
        raise RuntimeError(
            f"variable {style_label} failed final validation: "
            + "; ".join(
                final_validation_report.get("resume_rebuild_reasons", [])
            )
        )
    record_verified_output(out_path, region, italic)

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
        **layout_template_report,
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
    names = [command_name]
    if platform.system().lower() == "windows":
        names.extend([f"{command_name}.exe", f"{command_name}.cmd", f"{command_name}.bat"])
    directories = python_script_dirs()
    for directory in directories[:1]:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    found = shutil.which(command_name)
    if found:
        return found
    for directory in directories[1:]:
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
    for attempt in range(3):
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
        os.close(descriptor)
        tmp = Path(temporary)
        try:
            log_step(f"download {url} (attempt {attempt + 1}/3)")
            with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as handle:
                expected_length = getattr(response, "headers", {}).get("Content-Length")
                size = 0
                read = getattr(response, "read1", response.read)
                while chunk := read(64 * 1024):
                    handle.write(chunk)
                    size += len(chunk)
                if expected_length is not None and size != int(expected_length):
                    raise http.client.IncompleteRead(b"", int(expected_length) - size)
            tmp.replace(path)
            return path
        except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in {408, 429, 500, 502, 503, 504}:
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
        finally:
            tmp.unlink(missing_ok=True)
    raise RuntimeError("download retry loop exhausted")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file_checked(url: str, path: Path, sha256: str) -> Path:
    if path.exists() and path.stat().st_size and file_sha256(path).lower() != sha256.lower():
        path.unlink()
    for attempt in range(3):
        result = download_file(url, path)
        actual = file_sha256(result)
        if actual.lower() == sha256.lower():
            return result
        result.unlink(missing_ok=True)
        if attempt < 2:
            log_step(f"retry checksum-mismatched download: {path.name}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"sha256 mismatch for {path.name}: expected {sha256}, got {actual}")


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
                "-c",
                "core.autocrlf=false",
                "clone",
                "--config",
                "core.autocrlf=false",
                "--config",
                "core.eol=lf",
                "--filter=blob:none",
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
    if not SARASA_SOURCE_ARCHIVE_SHA256:
        raise FileNotFoundError("需要 Git 获取固定上游提交；当前版本没有配置可校验的源码归档后备")
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


def ensure_reference_sarasa(regions: list[str], *, hinted_styles: tuple[bool, ...] = (False, True)) -> None:
    global REFERENCE_SARASA, REFERENCE_SARASA_DIR, REFERENCE_SARASA_HINTED_DIR
    # A previously downloaded all-region archive is also a fixed-hash source.
    # Cold regional builds still download only their small regional archives.
    with tempfile.TemporaryDirectory(prefix="sarasa-reference-") as temporary:
        aggregates: dict[bool, Path] = {}
        for hinted in hinted_styles:
            kind = "hinted" if hinted else "unhinted"
            variant = "TTF" if hinted else "TTF-Unhinted"
            name = f"SarasaUi-{variant}-{SARASA_VERSION}.7z"
            archive = SOURCE_ARCHIVE_DIR / name
            if archive.is_file():
                if file_sha256(archive) != SARASA_UI_AGGREGATE_ARCHIVE_SHA256[kind]:
                    raise RuntimeError(f"Sarasa 全地区缓存归档 SHA-256 不匹配：{name}")
                destination = Path(temporary) / kind
                extract_7z_ttf_prefix(archive, destination, "SarasaUi")
                aggregates[hinted] = destination
        for region in regions:
            prefix = sarasa_region_prefix(region)
            for hinted in hinted_styles:
                kind = "hinted" if hinted else "unhinted"
                variant = "TTF" if hinted else "TTF-Unhinted"
                destination = region_reference_dir(region, hinted)
                if hinted in aggregates:
                    expected = {f"{prefix}-{static_reference_style_name(weight, italic)}.ttf"
                                for weight in STATIC_STYLE_SOURCES for italic in (False, True)}
                    found = {path.name: path for path in aggregates[hinted].glob(f"{prefix}-*.ttf")}
                    if set(found) != expected:
                        raise RuntimeError(f"Sarasa 全地区归档中的 {region} 字体清单不完整")
                    for name, source in found.items():
                        verify_or_write_source(destination / name, source.read_bytes())
                else:
                    name = f"{prefix}-{variant}-{SARASA_VERSION}.7z"
                    archive = download_file_checked(
                        f"https://github.com/be5invis/Sarasa-Gothic/releases/download/{SARASA_TAG}/{name}",
                        SOURCE_ARCHIVE_DIR / name, SARASA_UI_ARCHIVE_SHA256[region][kind],
                    )
                    extract_7z_ttf_prefix(archive, destination, f"{prefix}-")
                path = destination / f"{prefix}-Regular.ttf"
                with TTFont(path, lazy=True) as font:
                    version_name = font_name(font, 5) or ""
                if f"Version {SARASA_VERSION}" not in version_name:
                    raise RuntimeError(f"Sarasa reference version mismatch at {path}: {version_name!r}")
    REFERENCE_SARASA = reference_font_path("SC", "Regular", False)
    REFERENCE_SARASA_DIR = REFERENCE_SARASA.parent
    REFERENCE_SARASA_HINTED_DIR = region_reference_dir("SC", True)


def npm_dependency_snapshot(root: Path) -> dict[str, dict[str, str]]:
    snapshot = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise RuntimeError("node_modules 含有越界链接")
            snapshot[relative] = {"kind": "link", "target": os.readlink(path)}
        elif path.is_file():
            snapshot[relative] = {"kind": "file", "sha256": file_sha256(path)}
    return snapshot


def ensure_locked_npm_dependencies(lock_sha256: str) -> None:
    root = SARASA_SOURCE_DIR / "node_modules"
    receipt = BUILD_CACHE_DIR / "npm-verification" / f"{SARASA_COMMIT}-{lock_sha256}.json"
    expected_identity = {"source_commit": SARASA_COMMIT, "lock_sha256": lock_sha256,
                         "node_archive_sha256": NODE_ARCHIVE_SHA256[node_platform_archive()]}
    if receipt.is_file():
        recorded = json.loads(receipt.read_text(encoding="utf-8"))
        if recorded.get("identity") != expected_identity or not recorded.get("files"):
            raise RuntimeError("npm 校验记录与固定来源不一致")
        if npm_dependency_snapshot(root) != recorded["files"]:
            raise RuntimeError("node_modules 内容已改变，拒绝复用未通过校验的依赖")
        return
    # npm ci checks every package's lockfile integrity; the pinned Node archive
    # supplies npm. No dependency in this lock has an install script, and the
    # project's script only checks executable presence, handled explicitly here.
    log_step("install and verify locked npm dependencies without lifecycle scripts")
    run_checked([npm_executable(), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=SARASA_SOURCE_DIR, capture_output=False, env=local_runtime_env())
    if file_sha256(SARASA_SOURCE_DIR / "package-lock.json") != lock_sha256:
        raise RuntimeError("npm 安装改变了固定 lockfile")
    lock = json.loads((SARASA_SOURCE_DIR / "package-lock.json").read_text(encoding="utf-8"))
    for relative, package in lock["packages"].items():
        if not relative:
            continue
        installed = SARASA_SOURCE_DIR / relative / "package.json"
        if not installed.is_file():
            if package.get("optional") or package.get("os") or package.get("cpu"):
                continue
            raise RuntimeError(f"npm 缺少锁定依赖：{relative}")
        if str(json.loads(installed.read_text(encoding="utf-8"))["version"]) != str(package["version"]):
            raise RuntimeError(f"npm 依赖版本不匹配：{relative}")
    snapshot = npm_dependency_snapshot(root)
    if not snapshot or not SARASA_CHLOROPHYTUM.is_file():
        raise RuntimeError("npm 未生成完整的 Chlorophytum 工具链")
    write_json_atomic(receipt, {"identity": expected_identity, "files": snapshot})


def verify_git_source_bytes(directory: Path, expected_commit: str) -> dict[str, Any]:
    def git(*arguments, input=None):
        return subprocess.run(["git", *arguments], cwd=directory, input=input,
                              capture_output=True, check=True).stdout
    if git("rev-parse", "HEAD").decode().strip() != expected_commit:
        raise RuntimeError("上游源码提交不匹配")
    records = []
    for entry in git("ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0"):
        if not entry:
            continue
        info, name = entry.split(b"\t", 1)
        mode, kind, digest = info.split()
        relative = name.decode("utf-8")
        path = directory / relative
        if (kind != b"blob" or mode not in {b"100644", b"100755"} or b"\n" in name
                or not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve())):
            raise RuntimeError(f"上游源码文件类型或路径无效：{relative}")
        records.append((name, digest))
    hashes = git("hash-object", "--no-filters", "--stdin-paths",
                 input=b"\n".join(name for name, _digest in records) + b"\n").splitlines()
    if len(hashes) != len(records):
        raise RuntimeError("上游源码内容校验覆盖不完整")
    changed = [name.decode("utf-8") for (name, expected), actual in zip(records, hashes) if expected != actual]
    if changed or git("ls-files", "--others", "--exclude-standard", "-z"):
        raise RuntimeError(f"上游源码字节与固定提交不符或含额外文件：{changed[:8]}")
    git("diff-index", "--cached", "--quiet", "HEAD", "--")
    return {"commit": expected_commit, "tracked_files_verified": len(records), "raw_git_object_bytes": True}


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
        verify_git_source_bytes(SARASA_SOURCE_DIR, SARASA_COMMIT)
    else:
        if not SARASA_SOURCE_ARCHIVE_SHA256:
            raise RuntimeError("上游源码必须是已固定提交的 Git checkout；不能信任无校验来源的目录")
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

    ensure_locked_npm_dependencies(lock_sha256)
def ensure_build_sources(static_only: bool, regions: list[str], *, full_hint_group: bool = True) -> None:
    if os.environ.get("SARASA_SKIP_SOURCE_BOOTSTRAP") == "1":
        return
    ensure_reference_sarasa(regions)
    ensure_sarasa_source_tree()
    # Even a single-region hinted build analyzes the same full hint group.
    ensure_classical_static_sources(REGION_ORDER if full_hint_group else regions)
    if not static_only:
        ensure_vf_sources(regions)


def selected_ttfautohint() -> tuple[Path, str]:
    requested = os.environ.get("TTFAUTOHINT")
    executable = requested or shutil.which("ttfautohint")
    if executable:
        path = Path(shutil.which(executable) or executable).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"指定的 ttfautohint 不存在：{path}")
        return path, "external"
    try:
        import ttfautohint
    except ImportError:
        raise FileNotFoundError("ttfautohint executable or Python module is required")
    return Path(ttfautohint._executable_path()).resolve(), "ttfautohint-py"


@functools.lru_cache(maxsize=16)
def ttfautohint_version(path: str, digest: str) -> str:
    result = subprocess.run([path, "--version"], capture_output=True, text=True, check=True)
    version = result.stdout.strip().splitlines()
    if not version:
        raise RuntimeError("ttfautohint 没有返回版本信息")
    return version[0]


def ttfautohint_identity() -> dict[str, str]:
    path, provider = selected_ttfautohint()
    # Hash actual executable bytes on every call, including replacements at
    # the same path. The Python package version does not identify an override.
    digest = file_sha256(path)
    return {"provider": provider, "executable": path.name, "sha256": digest,
            "version": ttfautohint_version(str(path), digest)}


def run_ttfautohint(args: list[str]) -> str:
    path, provider = selected_ttfautohint()
    run_checked([str(path), *args])
    return path.name if provider == "external" else provider


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


def static_hint_recipe_fingerprint(weight_name: str | None = None) -> str:
    recipe = dict(STATIC_HINT_RECIPE)
    if weight_name is not None:
        recipe["ui_pass1_geometry"] = "source-han-western-cl-shanggu-cjk-1em-vertical-segments-v1"
        recipe["ui_pass1_ttfautohint_options"] = list(UI_AUTOHINT_OPTIONS)
        recipe["ui_pass1_unscaled_codepoints"] = list(UI_AUTOHINT_UNSCALED_CODEPOINTS)
        recipe["ui_pass1_unscaled_vertical_segments"] = UI_AUTOHINT_UNSCALED_VERTICAL_SEGMENTS
    payload = json.dumps(recipe, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chlorophytum_package_id() -> dict[str, Any]:
    package_dir = SARASA_SOURCE_DIR / "node_modules" / "@chlorophytum" / "cli"
    package_json = package_dir / "package.json"
    return {
        "startup": optional_file_sha256(SARASA_CHLOROPHYTUM),
        "package": optional_file_sha256(package_json),
        "locked_dependency_files": hashlib.sha256(json.dumps(
            {name: value for name, value in npm_dependency_snapshot(SARASA_SOURCE_DIR / "node_modules").items()
             if name != ".package-lock.json"},
            sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest(),
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
        "hint_recipe_sha256": static_hint_recipe_fingerprint(weight_name if group_name == "pass1" else None),
    }
    if group_name == "pass1":
        payload["ttfautohint"] = ttfautohint_identity()
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
        "ttfautohint": ttfautohint_identity(),
        "afdko": importlib.metadata.version("afdko"),
        "sarasa_commit": SARASA_COMMIT,
        "chlorophytum": chlorophytum_package_id(),
        "hint_recipe_sha256": static_hint_recipe_fingerprint(weight_name),
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


def ui_hint_control(in_path: Path) -> str:
    with TTFont(in_path, lazy=True) as font:
        cmap = font.getBestCmap() or {}
        glyph_ids = {font.getGlyphID(cmap[cp]) for cp in UI_AUTOHINT_UNSCALED_CODEPOINTS if cp in cmap}
        if UI_AUTOHINT_UNSCALED_VERTICAL_SEGMENTS and dash_multiple_substitutions(font):
            segments = vertical_dash_segments(font, upstream_dash_roles(font))
            if segments:
                glyph_ids.update(font.getGlyphID(name) for name in segments.values())
    return "".join(f"none dflt @ {gid}\n" for gid in sorted(glyph_ids))


def hint_static_font(in_path: Path, out_path: Path, weight_name: str | None = None) -> dict[str, Any]:
    if os.environ.get("SARASA_SKIP_TTFAUTOHINT") == "1":
        shutil.copy2(in_path, out_path)
        return {"hinted": False, "hint_tool": "skipped"}
    options = list(UI_AUTOHINT_OPTIONS) if weight_name is not None else []
    control = ui_hint_control(in_path) if weight_name is not None else ""
    control_args = []
    if control:
        control_path = out_path.with_suffix(".control.txt")
        control_path.write_text(control, encoding="ascii", newline="\n")
        control_args = ["--control-file", str(control_path)]
    identity = ttfautohint_identity()
    tool = run_ttfautohint([*options, *control_args, str(in_path), str(out_path)])
    if ttfautohint_identity() != identity:
        raise RuntimeError("ttfautohint 在生成指令期间发生变化")
    execution = native_hint_execution_status(out_path, full_ppems=tuple(range(6, 201)), regression_ppems=()) if weight_name is not None else None
    if execution is not None and not execution["ok"]:
        raise RuntimeError(f"ttfautohint 生成了无效指令：{out_path.name}；{execution}")
    return {"hinted": True, "hint_tool": tool, "hint_tool_identity": identity,
            "hint_options": options, "input_sha256": file_sha256(in_path), "output_sha256": file_sha256(out_path),
            "hint_control": control, "hint_control_sha256": hashlib.sha256(control.encode("ascii")).hexdigest(),
            "native_hint_execution": execution}


def native_hint_execution_status(path: Path, *, full_ppems: tuple[int, ...] = (12, 16), regression_ppems: tuple[int, ...] = tuple(range(6, 201))) -> dict[str, Any]:
    import freetype

    face = freetype.Face(BytesIO(path.read_bytes()))
    modes = {"normal": freetype.FT_LOAD_TARGET_NORMAL, "mono": freetype.FT_LOAD_TARGET_MONO, "lcd": freetype.FT_LOAD_TARGET_LCD}
    regressions = {0, *[face.get_char_index(cp) for cp in (0x0360, 0x3125, 0x30, 0x3A, 0x40, 0x2D)]}
    expected = len(modes) * (face.num_glyphs * len(full_ppems) + len(regressions) * len(regression_ppems))
    checked = 0; errors = []
    for mode, mode_flags in modes.items():
        for sizes, glyphs in ((full_ppems, range(face.num_glyphs)), (regression_ppems, sorted(regressions))):
            for ppem in sizes:
                face.set_pixel_sizes(0, ppem)
                for gid in glyphs:
                    checked += 1
                    try:
                        face.load_glyph(gid, freetype.FT_LOAD_PEDANTIC | freetype.FT_LOAD_NO_AUTOHINT | mode_flags)
                    except freetype.FT_Exception as error:
                        errors.append({"mode": mode, "ppem": ppem, "gid": gid, "error": str(error)})
                        if len(errors) >= 16:
                            break
                if len(errors) >= 16:
                    break
            if len(errors) >= 16:
                break
        if len(errors) >= 16:
            break
    return {"ok": not errors and checked == expected, "complete": checked == expected,
            "FreeType": list(freetype.version()), "glyphs": face.num_glyphs,
            "full_glyph_ppems": list(full_ppems), "regression_ppems": list(regression_ppems),
            "regression_glyphs": sorted(regressions), "modes": list(modes),
            "loads_expected": expected, "loads_checked": checked, "failures": len(errors), "samples": errors}


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


def require_chlorophytum() -> None:
    if os.environ.get("SARASA_SKIP_CHLOROPHYTUM") == "1":
        raise RuntimeError("hinted 构建不能跳过 Chlorophytum；无 hint 成品请使用 --unhinted-only")
    if not SARASA_CHLOROPHYTUM.is_file():
        raise FileNotFoundError("缺少 Chlorophytum，不能生成完整 hinted 字体")


def chlorophytum_generate_static_hints(
    jobs: list[tuple[Path, Path, str]],
    tmp_dir: Path,
    hint_jobs: int | None = None,
) -> dict[Path, dict[str, Any]]:
    if not jobs:
        return {}
    require_chlorophytum()
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
    if not config_path.is_file():
        raise FileNotFoundError("缺少 Chlorophytum 字重配置")

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
    require_chlorophytum()
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
    if not config_path.is_file():
        raise FileNotFoundError("缺少 Chlorophytum 字重配置")

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
    content = "\n".join([
        'import { pathToFileURL } from "node:url";',
        "const recipe = process.argv[2];",
        "const args = JSON.parse(process.argv[3]);",
        "const mod = await import(pathToFileURL(recipe).href);",
        "await mod.default(args);",
        "",
    ])
    if not runner.is_file() or runner.read_text(encoding="utf-8") != content:
        runner.write_text(content, encoding="utf-8", newline="\n")
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


def cached_static_artifact(output: Path, inputs: dict[str, Path], recipe: dict[str, Any], producer: Any) -> bool:
    """Reuse only content-bound artifacts; never attest to preexisting bytes."""
    payload = {"schema": 1, "file": output.name, "recipe": recipe,
               "inputs": {name: file_sha256(path) for name, path in sorted(inputs.items())}}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    receipt = output.with_name(output.name + ".cache.json")
    try:
        saved = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if output.is_file() and saved == {"key": key, "sha256": file_sha256(output), "size": output.stat().st_size}:
        return True
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".artifact-", dir=output.parent) as temporary:
        pending = Path(temporary) / output.name
        producer(pending)
        if not pending.is_file() or not pending.stat().st_size:
            raise RuntimeError(f"静态缓存生成失败：{output.name}")
        value = {"key": key, "sha256": file_sha256(pending), "size": pending.stat().st_size}
        pending.replace(output)
        write_json_atomic(receipt, value)
    return False


def run_cached_sarasa_module(tmp_dir: Path, recipe: str, args: dict[str, Any]) -> None:
    input_names = {"main", "lgc", "as", "ws", "feMisc", "kanji", "hangul", "classicalOverride"}
    inputs = {name: Path(value) for name, value in args.items() if name in input_names and value is not None}
    inputs["module"] = SARASA_SOURCE_DIR / recipe
    parameters = {name: value for name, value in args.items() if name not in input_names | {"o"}}
    cached_static_artifact(Path(args["o"]), inputs,
                           {"module": recipe, "parameters": parameters, "sarasa_commit": SARASA_COMMIT,
                            "npm_lock_sha256": SARASA_PACKAGE_LOCK_SHA256, "node": NODE_VERSION},
                           lambda output: run_sarasa_module(tmp_dir, recipe, {**args, "o": str(output)}))


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
    source_ttc = SARASA_SOURCE_DIR / "sources" / "shs" / f"SourceHanSans-{shs_weight}.ttc"
    if not source_ttc.exists():
        raise FileNotFoundError(source_ttc)
    shs_prefix = source_han_static_prefix(region)
    extractor, converter = otc2otf_executable(), otf2ttf_executable()
    inputs = {"source": source_ttc,
              "extractor": Path(shutil.which(extractor) or extractor),
              "converter": Path(shutil.which(converter) or converter)}
    def produce(output):
        with tempfile.TemporaryDirectory(prefix="sarasa-shs-") as temporary:
            stage = Path(temporary)
            copied_ttc = stage / source_ttc.name
            link_or_copy(source_ttc, copied_ttc)
            run_checked([extractor, str(copied_ttc)], cwd=stage)
            expected_otf = stage / f"{shs_prefix}-{shs_weight}.otf"
            if not expected_otf.is_file():
                raise FileNotFoundError(expected_otf)
            run_checked([converter, "-o", str(output), str(expected_otf)])
    cached_static_artifact(out_ttf, inputs,
                           {"kind": "source-han-static", "region": region, "weight": shs_weight,
                            "afdko": importlib.metadata.version("afdko")}, produce)
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
        def instantiate(output):
            inter = TTFont(INTER_ITALIC if italic else INTER_UPRIGHT)
            inter = instantiateVariableFont(inter, {"opsz": 14, "wght": weight_value}, inplace=False, optimize=True)
            try:
                remove_variable_tables(inter)
                inter.flavor = None
                inter.save(output, reorderTables=True)
            finally:
                inter.close()
        cached_static_artifact(raw_path, {"source": INTER_ITALIC if italic else INTER_UPRIGHT},
                               {"kind": "inter-static-instance", "wght": weight_value, "opsz": 14,
                                "fonttools": importlib.metadata.version("fonttools")}, instantiate)
    cached_static_artifact(out_path, {"source": raw_path}, {"kind": "dehint", "tool": ttfautohint_identity()},
                           lambda output: run_ttfautohint(["-d", str(raw_path), str(output)]))
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
    if group == "Inter" and family == "Ui":
        source = build_inter_source(weight_name, weight_value, italic, tmp_dir)
    else:
        source = SARASA_SOURCE_DIR / "sources" / group / f"{group}-{style}.ttf"
        if not source.exists():
            raise FileNotFoundError(source)
    cached_static_artifact(out_path, {"source": source}, {"kind": "dehint", "tool": ttfautohint_identity()},
                           lambda output: run_ttfautohint(["-d", str(source), str(output)]))
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
    run_cached_sarasa_module(tmp_dir, "make/punct/ws.mjs", {**punct_args, "o": str(ws)})
    run_cached_sarasa_module(tmp_dir, "make/punct/as.mjs", {**punct_args, "o": str(as_punct)})
    run_cached_sarasa_module(tmp_dir, "make/punct/fe-misc.mjs", {**punct_args, "o": str(fe_misc)})

    pass1 = work_dir / "pass1.ttf"
    run_cached_sarasa_module(
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
        non_kanji.parent.mkdir(parents=True, exist_ok=True)
        run_cached_sarasa_module(
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
    pending_hint_items = [item for item in result if item["family"] != "Ui"]
    def prepare_environment_hint(item):
        source = Path(item["source"]); output = Path(item["hinted"])
        generated = {}
        cached = cached_static_artifact(
            output, {"source": source}, {"kind": "sarasa-non-ui-autohint", "tool": ttfautohint_identity(),
                                        "hint_recipe": static_hint_recipe_fingerprint()},
            lambda pending: generated.update(hint_static_font(source, pending)))
        return generated or {"hinted": True, "hint_tool": "verified-full-group-preparation",
                             "input_sha256": file_sha256(source), "output_sha256": file_sha256(output),
                             "cache_reused": cached}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, SARASA_HINT_PREP_JOBS)) as executor:
        future_map = {
            executor.submit(prepare_environment_hint, item): item
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

    run_cached_sarasa_module(
        tmp_dir,
        "make/kanji/build.mjs",
        {"main": str(shs_ttf), "classicalOverride": str(classical_override) if classical_override else None, "o": str(kanji)},
    )
    run_cached_sarasa_module(tmp_dir, "make/hangul/build.mjs", {"main": str(shs_ttf), "o": str(hangul)})
    run_cached_sarasa_module(tmp_dir, "make/non-kanji/build.mjs", {"main": str(shs_ttf), "o": str(non_kanji)})

    punct_args = {
        "family": "Ui",
        "region": region,
        "style": style_name,
        "main": str(non_kanji),
        "lgc": str(inter_ttf),
        **flags,
    }
    run_cached_sarasa_module(tmp_dir, "make/punct/ws.mjs", {**punct_args, "o": str(ws)})
    run_cached_sarasa_module(tmp_dir, "make/punct/as.mjs", {**punct_args, "o": str(as_punct)})
    run_cached_sarasa_module(tmp_dir, "make/punct/fe-misc.mjs", {**punct_args, "o": str(fe_misc)})

    run_cached_sarasa_module(
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
        "western_dash_source_path": shs_ttf,
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
    # Sarasa's final simplifySingleSub replaces each vert/vrt2 feature with
    # a SingleSubst and leaves our prepared MultipleSubst unreferenced. Relink
    # the exact native glyphs before subsetting; never redraw them after hinting.
    with TTFont(out_path, recalcBBoxes=False, recalcTimestamp=False) as font:
        restore_prepared_vertical_segments(font)
        font.save(out_path, reorderTables=True)


def restore_prepared_vertical_segments(font: TTFont) -> None:
    candidates = []
    try:
        roles = upstream_dash_roles(font)
    except ValueError:
        return  # Raw upstream/reference pass1 has no project derivatives.
    expected_keys = {roles[key] for key in ("encoded_two", "fullwidth_two", "vertical_two",
                                           "encoded_three", "fullwidth_three", "vertical_three")}
    for index, lookup in enumerate(font["GSUB"].table.LookupList.Lookup):
        if lookup.LookupType != 2:
            continue
        mapping = {key: value for part in lookup.SubTable for key, value in part.mapping.items()}
        if set(mapping) == expected_keys:
            two, three = mapping[roles["vertical_two"]], mapping[roles["vertical_three"]]
            if len(two) == 2 and len(three) == 3 and two[0] == three[0]:
                candidates.append(index)
    if not candidates:
        return
    if len(candidates) != 1:
        raise ValueError("pass2 中竖排分段布局来源不唯一")
    for tag in ("vert", "vrt2"):
        link_lookup_to_all_gsub_features(font, tag, candidates[0])
    ensure_vertical_presentation_dash_ligatures(font, roles)


def apply_static_propdigits(font: TTFont) -> dict[str, int]:
    pnum = get_single_substitution_mapping(font, "pnum")
    if not pnum or "cmap" not in font:
        return {"static_propdigit_cmap_remaps": 0}
    default_cmap = font.getBestCmap()
    remap: dict[int, str] = {}
    for codepoint in sorted(PROPDIGITS_CODEPOINTS):
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
        "codepage_range_1": OS2_CODEPAGE_RANGE_1_BY_REGION[region],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def static_weight_output_paths(region: str, weight_name: str) -> list[tuple[Path, bool, bool]]:
    return [
        (static_dir(region, hinted) / static_output_name(region, weight_name, italic), hinted, italic)
        for hinted in [False, True]
        for italic in [False, True]
    ]


def outline_style_control(font: TTFont) -> tuple[float, float]:
    name = font.getBestCmap().get(0x48)
    if name is None:
        raise ValueError("missing H outline style control")
    points = font["glyf"][name].getCoordinates(font["glyf"])[0]
    bottom, top = min(y for x, y in points), max(y for x, y in points)
    if top <= bottom:
        raise ValueError("empty H outline style control")
    lower = [x for x, y in points if y == bottom]
    upper = [x for x, y in points if y == top]
    slope = (sum(upper) / len(upper) - sum(lower) / len(lower)) / (top - bottom)
    return slope, top - bottom


@functools.lru_cache(maxsize=12)
def source_outline_style_control(weight_name: str, italic: bool) -> tuple[float, float]:
    style = inter_source_style(weight_name, italic)
    if style is None:
        raise ValueError("missing fixed Inter static style")
    with TTFont(SARASA_SOURCE_DIR / "sources" / "Inter" / f"Inter-{style}.ttf", lazy=True) as source:
        return outline_style_control(source)


def output_identity_status(font: TTFont, region: str, italic: bool, weight_name: str | None, hinted: bool = False) -> dict[str, Any]:
    expected = TTFont()
    for tag in ("name", "OS/2", "head", "post", "hhea"):
        expected[tag] = copy.deepcopy(font[tag])
    if weight_name is None:
        update_vf_names(expected, region, italic)
        update_style_flags(expected, italic)
    else:
        stop = next(stop for stop in SOURCE_HAN_WEIGHT_STOPS if stop["name"] == weight_name)
        update_static_names(expected, region, weight_name, int(stop["value"]), italic, hinted)
    update_caret_slope(expected)
    identifiers = {1, 2, 3, 4, 6, 16, 17, 25}
    def names(value: TTFont) -> dict[tuple[int, int, int, int], str]:
        return {(r.nameID, r.platformID, r.platEncID, r.langID): r.toUnicode()
                for r in value["name"].names if r.nameID in identifiers}
    failures = []
    if names(font) != names(expected):
        failures.append("regional family/style/unique/PostScript names do not match the requested output")
    for tag, attribute, mask in (("OS/2", "fsSelection", 0xE1), ("head", "macStyle", 3)):
        if (int(getattr(font[tag], attribute)) & mask) != (int(getattr(expected[tag], attribute)) & mask):
            failures.append(f"{tag}.{attribute} does not match the requested style")
    if font["OS/2"].usWeightClass != expected["OS/2"].usWeightClass:
        failures.append("OS/2 weight does not match the requested style")
    if not math.isclose(font["post"].italicAngle, expected["post"].italicAngle, abs_tol=1 / 65536):
        failures.append("post.italicAngle does not match the requested style")
    for attribute in ("caretSlopeRise", "caretSlopeRun", "caretOffset"):
        if getattr(font["hhea"], attribute) != getattr(expected["hhea"], attribute):
            failures.append(f"hhea.{attribute} does not match the requested style")
    actual_slope, actual_height = outline_style_control(font)
    expected_slope, source_height = source_outline_style_control(weight_name or "Regular", italic)
    if abs(actual_slope - expected_slope) > 2 / actual_height + 2 / source_height:
        failures.append("H outline does not match the fixed Inter upright/italic style")
    if not family_name_length_status(font)["ok"]:
        failures.append("family and weight names exceed the Windows 31-character limit")
    if weight_name is None:
        failures.extend(variable_raster_metadata_status(font)["reasons"])
    return {"ok": not failures, "reasons": failures}


def contextual_case_structure_status(font: TTFont) -> dict[str, Any]:
    if "GSUB" not in font or not layout_has_feature(font, "GSUB", "calt"):
        return {"ok": False, "reason": "missing calt"}
    font["GSUB"].ensureDecompiled()
    before = font.getTableData("GSUB")
    probe = TTFont()
    probe.setGlyphOrder(font.getGlyphOrder())
    for tag in ("GSUB", "cmap", "post"):
        probe[tag] = copy.deepcopy(font[tag])
    report = add_digit_colon_feature(probe)
    return {"ok": before == probe.getTableData("GSUB"),
            "expected_rules": report["inter_contextual_case_rules"],
            "expected_colon_rules": report["digit_colon_rules"],
            "reason": "" if before == probe.getTableData("GSUB") else "incomplete or stale Inter contextual-case rules"}


def output_validation_record_path(path: Path) -> Path:
    path = canonical_output_path(path)
    relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
    key = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return OUTPUT_VALIDATION_DIR / f"{key}.json"


def output_validation_contract(hinted: bool) -> dict[str, Any]:
    # A record attests to exact output bytes validated under this implementation.
    # A changed tool invalidates hinted outputs even when its path is unchanged.
    dependencies = {name: importlib.metadata.version(name) for name in BUILD_LOCK_PACKAGES}
    return {"schema": 3, "builder_sha256": BUILDER_CODE_SHA256,
            "build_components_sha256": BUILD_COMPONENT_SHA256,
            "python": list(sys.version_info[:3]), "dependencies": dependencies,
            "dependency_lock_sha256": BUILD_LOCK_SHA256,
            "ttfautohint": ttfautohint_identity() if hinted else None}


def record_verified_output(path: Path, region: str, italic: bool, weight_name: str | None = None, hinted: bool = False) -> None:
    with TTFont(path, recalcTimestamp=False) as font:
        identity = output_identity_status(font, region, italic, weight_name, hinted)
        layout = contextual_case_structure_status(font)
        joining = continuous_vertical_dash_structure_status(font)
        if not identity["ok"] or not layout["ok"] or not joining["ok"]:
            raise RuntimeError(f"不能记录不完整的增量成品：{path.name}；{identity}；{layout}；{joining}")
    if hinted:
        execution = native_hint_execution_status(path, full_ppems=())
        if not execution["ok"]:
            raise RuntimeError(f"不能记录指令执行失败的增量成品：{path.name}；{execution}")
    write_json_atomic(output_validation_record_path(path), {
        "file": path.name, "region": region, "italic": italic, "weight": weight_name,
        "hinted": hinted, "sha256": file_sha256(path), "contract": output_validation_contract(hinted),
    })


def verified_output_record_status(path: Path, region: str, italic: bool, weight_name: str | None = None, hinted: bool = False) -> tuple[bool, str]:
    record_path = output_validation_record_path(path)
    if not record_path.is_file():
        return False, "missing validated output record"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False, "invalid validated output record"
    expected = {"file": path.name, "region": region, "italic": italic, "weight": weight_name,
                "hinted": hinted, "sha256": file_sha256(path), "contract": output_validation_contract(hinted)}
    return (True, "") if record == expected else (False, "output bytes, style or build/tool contract changed")


def require_validated_input(path: Path, region: str, italic: bool,
                            weight_name: str | None = None, hinted: bool = False) -> None:
    """A refresh may transform previously attested bytes, never unknown input."""
    try:
        record = json.loads(output_validation_record_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"最终化来源缺少有效校验记录：{path.name}") from error
    expected = {"file": path.name, "region": region, "italic": italic,
                "weight": weight_name, "hinted": hinted, "sha256": file_sha256(path)}
    contract = record.get("contract", {})
    if (any(record.get(key) != value for key, value in expected.items())
            or contract.get("schema") not in {2, 3}
            or re.fullmatch(r"[a-f0-9]{64}", str(contract.get("builder_sha256", ""))) is None):
        raise ValueError(f"最终化来源字节或身份不匹配已有校验记录：{path.name}")


def unhinted_reference_status(path: Path, region: str, weight_name: str, italic: bool) -> tuple[bool, list[str]]:
    if not path.is_file():
        return False, ["missing unhinted reference"]
    valid, reason = verified_output_record_status(path, region, italic, weight_name, False)
    reasons = [] if valid else [reason]
    try:
        with TTFont(path, recalcTimestamp=False) as font:
            reasons.extend(output_identity_status(font, region, italic, weight_name)["reasons"])
            if f"project {VERSION}" not in (font["name"].getDebugName(5) or ""):
                reasons.append("wrong reference version")
            if "fvar" in font or "fpgm" in font or "cvt " in font:
                reasons.append("reference must be native unhinted static")
            reasons.extend(upstream_dash_structure_status(font, region)["reasons"])
            if not contextual_case_structure_status(font)["ok"]:
                reasons.append("incomplete contextual rules")
    except (ValueError, KeyError, TTLibError) as error:
        reasons.append(f"invalid reference: {error}")
    return not reasons, reasons


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
            identity = output_identity_status(font, region, italic, weight_name, hinted)
            reasons.extend(f"{label}: {reason}" for reason in identity["reasons"])
            valid_record, record_reason = verified_output_record_status(path, region, italic, weight_name, hinted)
            if not valid_record:
                reasons.append(f"{label}: {record_reason}")
            context_status = contextual_case_structure_status(font)
            if not context_status["ok"]:
                reasons.append(f"{label}: {context_status['reason']}")
            if "OS/2" not in font or font["OS/2"].usWeightClass != weight_value:
                reasons.append(f"{label}: wrong OS/2 weight")
            if "OS/2" not in font or font["OS/2"].achVendID != OS2_VENDOR_ID:
                reasons.append(f"{label}: wrong vendor")
            if "OS/2" not in font or int(font["OS/2"].ulCodePageRange1) != OS2_CODEPAGE_RANGE_1_BY_REGION[region]:
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
            hint_tables = has_grid_fitting_tables(font)
            glyph_programs = int(getattr(font["maxp"], "maxSizeOfInstructions", 0)) > 0
            if hinted and (not hint_tables or not glyph_programs):
                reasons.append(f"{label}: missing hints")
            if not hinted and (hint_tables or glyph_programs):
                reasons.append(f"{label}: unexpected hints")
            if not hinted:
                reasons.extend(f"{label}: {reason}" for reason in nonhinting_raster_metadata_status(font)["reasons"])
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


def refresh_version_metadata_font(path: Path, source_version: str) -> dict[str, Any]:
    """Change only version-bearing name records and head.fontRevision."""
    before_sha256 = file_sha256(path)
    protected = sfnt_table_hashes(path, {"head", "name"})
    pending = path.with_name(path.name + ".version.tmp")
    try:
        with TTFont(path, lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
            if f"project {source_version};" not in (font_name(font, 5) or ""):
                raise ValueError("版本刷新输入不属于指定的原项目版本")
            changed = set()
            for record in list(font["name"].names):
                if record.nameID not in {3, 5}:
                    continue
                original = record.toUnicode()
                value = re.sub(r"(?<![0-9.])" + re.escape(source_version) + r"(?![0-9.])", VERSION, original)
                if record.nameID == 5:
                    value = re.sub(r"^Version [0-9.]+;", f"Version {OPENTYPE_VERSION};", value)
                if value != original:
                    font["name"].setName(value, record.nameID, record.platformID, record.platEncID, record.langID)
                    changed.add(record.nameID)
            if changed != {3, 5}:
                raise ValueError("未找到完整的项目版本名称记录")
            font["head"].fontRevision = FONT_REVISION
            font.save(pending, reorderTables=True)
        if sfnt_table_hashes(pending, {"head", "name"}) != protected:
            raise RuntimeError("版本刷新改变了 head/name 之外的表，拒绝替换")
        pending.replace(path)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return {"from_version": source_version, "to_version": VERSION,
            "previous_sha256": before_sha256, "sha256": file_sha256(path),
            "changed_name_ids": sorted(changed), "protected_table_sha256": protected,
            "hint_rerun": False}


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
    intentionally_changed = {"head", "maxp", "OS/2", "name", "gasp", "GSUB", "GPOS", "GDEF", "VORG"}
    if not hinted:
        intentionally_changed.add("prep")
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
        update_os2_sarasa_metadata(font, region)
        raster_report = normalize_static_raster_metadata(font, hinted)
        revision_report = update_head_project_revision(font)
        mac_report = remove_mac_name_records(font)
        ellipsis_before = cjk_ellipsis_structure_status(font)
        ellipsis_report = apply_cjk_ellipsis_behavior(font)
        ellipsis_report.update(apply_default_regional_punctuation(font, region))
        vertical_report = normalize_static_vertical_origin(font)
        colon_report = add_digit_colon_feature(font)
        colon_report.update(install_continuous_vertical_dash_layout(font))
        colon_report.update(normalize_ideograph_gdef_classes(font))
        colon_report["gpos_pair_sets_sorted"] = font_layout.sort_pair_sets(font)
        colon_report["unreferenced_layout_lookups_removed"] = font_layout.prune_unreferenced_lookups(font)
        font["OS/2"].usMaxContext = maxCtxFont(font)
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
        record_verified_output(path, region, italic, weight_name, hinted)
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
    def snapshot(data: bytes) -> dict[int, list[tuple]]:
        runtime = hb.Font(hb.Face(data))
        result = {}
        for weight in weights:
            runtime.set_variations({"wght": weight})
            result[weight] = [
                (runtime.get_glyph_extents(gid), runtime.get_glyph_h_advance(gid), runtime.get_glyph_v_advance(gid), runtime.get_glyph_v_origin(gid))
                for gid in range(len(font.getGlyphOrder()))
            ]
        return result
    original_data = serialized_font_bytes(font)
    expected = snapshot(original_data)
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
    normalized_data = serialized_font_bytes(font)
    actual = snapshot(normalized_data)
    changes = [(weight, font.getGlyphName(gid), before, after) for weight in weights for gid, (before, after) in enumerate(zip(expected[weight], actual[weight])) if before != after]
    rounding_observations = []
    unexpected = []
    before_runtime, after_runtime = hb.Font(hb.Face(original_data)), hb.Font(hb.Face(normalized_data))
    precise_before = precise_after = None
    precise_sets = {}
    for weight, name, before, after in changes:
        first, second = before[0], after[0]
        eligible = (before[1:] == after[1:] and first is not None and second is not None
                    and abs(first.x_bearing - second.x_bearing) <= 1 and abs(first.y_bearing - second.y_bearing) <= 1
                    and abs(first.width - second.width) <= 1 and abs(first.height - second.height) <= 1)
        drawings = []
        if eligible:
            for runtime in (before_runtime, after_runtime):
                runtime.set_variations({"wght": weight})
                pen = RecordingPen(); runtime.draw_glyph_with_pen(font.getGlyphID(name), pen)
                drawings.append(pen.value)
            eligible = ([(command, len(points)) for command, points in drawings[0]]
                        == [(command, len(points)) for command, points in drawings[1]])
        if eligible:
            pairs = [(a, b) for (_, left), (_, right) in zip(*drawings)
                     for left_point, right_point in zip(left, right)
                     for a, b in zip(left_point, right_point)]
            maximum_coordinate = max((max(abs(a), abs(b)) for a, b in pairs), default=1)
            # HarfBuzz draw coordinates are float32. Moving the phantom origin
            # can change the final floating addition by one ULP at an exact .5
            # boundary. Prove that tiny drawing difference directly; a whole
            # font-unit geometric change is never accepted by this check.
            ulp = 2 ** (math.floor(math.log2(max(maximum_coordinate, 1))) - 23)
            residual = max((abs(a - b) for a, b in pairs), default=0)
            bound = min(1 / 1024, ulp * max(2, 2 * len(variations.get(name, []))))
            eligible = residual <= bound
            geometry_residual = 0.0
            if eligible and (residual > ulp or first.x_bearing != second.x_bearing or first.y_bearing != second.y_bearing):
                # Reassociation across multiple active tuples can accumulate
                # more than one float32 ULP. In that case require a separate
                # double-precision, translation-invariant outline proof.
                if precise_before is None:
                    precise_before = TTFont(BytesIO(original_data))
                    precise_after = TTFont(BytesIO(normalized_data))
                if weight not in precise_sets:
                    precise_sets[weight] = (precise_before.getGlyphSet(location={"wght": weight}),
                                             precise_after.getGlyphSet(location={"wght": weight}))
                records = []
                for glyphs in precise_sets[weight]:
                    pen = DecomposingRecordingPen(glyphs); glyphs[name].draw(pen)
                    records.append(pen.value)
                eligible = ([(op, len(args)) for op, args in records[0]]
                            == [(op, len(args)) for op, args in records[1]])
                if eligible:
                    deltas = [(a[0] - b[0], a[1] - b[1]) for (_, aa), (_, bb) in zip(*records)
                              for a, b in zip(aa, bb) if a is not None and b is not None]
                    geometry_residual = max((max(point[axis] for point in deltas) - min(point[axis] for point in deltas)
                                             for axis in (0, 1)), default=0) if deltas else 0
                    eligible = geometry_residual < 1e-7
        if eligible:
            rounding_observations.append({"weight": weight, "glyph": name,
                                          "maximum_draw_difference": residual, "float32_ulp": ulp,
                                          "float32_accumulation_bound": bound,
                                          "normalized_outline_residual": geometry_residual,
                                          "before_extent": tuple(first), "after_extent": tuple(second)})
        else:
            unexpected.append((weight, name, before, after))
    if precise_before is not None:
        precise_before.close(); precise_after.close()
    if unexpected:
        raise RuntimeError(f"横向原点规范化改变了已有运行时轮廓边界或度量：{len(unexpected)}，{unexpected[:4]!r}")
    return {"cross_engine_metric_normalization": True, "horizontal_origin_tuples_translated": translated, "component_origin_tuples_compensated": compensated, "vertical_metrics_source": "gvar", "vvar_removed_for_shared_vertical_origin": removed, "runtime_metric_preservation_weights": list(weights), "runtime_metric_preservation_glyphs": len(font.getGlyphOrder()), "runtime_metric_preservation_mismatches": 0, "runtime_extent_rounding_observations": rounding_observations}



def _refresh_variable_finalization_outputs(regions: list[str], italic_styles: tuple[bool, ...] = (False, True)) -> list[dict[str, Any]]:
    outputs = []
    for region in regions:
        for italic in italic_styles:
            path = VARIABLE_DIR / variable_output_name(region, italic)
            allowed = {"GSUB", "GPOS", "GDEF", "name", "head", "OS/2", "MVAR", "VORG", "VVAR", "HVAR", "hmtx", "vmtx", "glyf", "loca", "gvar", "prep", "gasp", "maxp", "fpgm", "cvt "}
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
                references = {}
                try:
                    for stop in SOURCE_HAN_WEIGHT_STOPS:
                        references[int(stop["value"])] = open_project_static_metric_reference_font(region, str(stop["name"]), italic)
                    report.update(finalize_variable_vertical_segments(font, references))
                finally:
                    for reference in references.values():
                        reference.close()
                report.update(align_source_punctuation_advances(font, region))
                update_vf_names(font, region, italic)
                update_os2_sarasa_metadata(font, region)
                remove_mac_name_records(font)
                update_head_project_revision(font)
                report.update(normalize_variable_raster_metadata(font))
                report.update(prune_unreferenced_variable_names(font))
                report.update(install_continuous_vertical_dash_layout(font))
                report.update(normalize_ideograph_gdef_classes(font))
                report["unreferenced_layout_lookups_removed"] = font_layout.prune_unreferenced_lookups(font)
                font["OS/2"].usMaxContext = maxCtxFont(font)
                report.update(rebuild_vf_underline_mvar(font))
                font.save(pending)
            finally:
                font.close()
            try:
                if sfnt_table_hashes(pending, allowed) != before:
                    raise RuntimeError("VF 最终化改变了受保护的轮廓、定位或度量表")
                pending.replace(path)
                record_verified_output(path, region, italic)
            finally:
                pending.unlink(missing_ok=True)
            outputs.append({"file": portable_report_path(path), "region": region, "italic": italic, "protected_tables": sorted(before), **report})
    return outputs


def _refresh_static_finalization_outputs(regions: list[str]) -> list[dict[str, Any]]:
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


def refresh_variable_finalization_outputs(regions: list[str], italic_styles: tuple[bool, ...] = (False, True)) -> list[dict[str, Any]]:
    for region in regions:
        for italic in italic_styles:
            require_validated_input(VARIABLE_DIR / variable_output_name(region, italic), region, italic)
    with staged_build_outputs(regions, static_only=False, variable_only=True, unhinted_only=False,
                              resume_static=False, resume_variable=True, include_static=False,
                              italic_styles=italic_styles):
        return _refresh_variable_finalization_outputs(regions, italic_styles)


def refresh_static_finalization_outputs(regions: list[str]) -> list[dict[str, Any]]:
    for region in regions:
        for stop in SOURCE_HAN_WEIGHT_STOPS:
            for path, hinted, italic in static_weight_output_paths(region, str(stop["name"])):
                require_validated_input(path, region, italic, str(stop["name"]), hinted)
    with staged_build_outputs(regions, static_only=True, variable_only=False, unhinted_only=False,
                              resume_static=True, resume_variable=False):
        outputs = _refresh_static_finalization_outputs(regions)
        write_static_readme(regions)
        return outputs


def refresh_layout_outputs(regions: list[str]) -> dict[str, Any]:
    """Atomically refresh layout with byte-for-byte outline/hint preservation."""
    from font_audit.contract import AXIS_POINTS
    from font_audit.products import vertical_segment_runtime_status

    regions = parse_regions(",".join(regions))
    original_report = json.loads((REPORT_DIR / "Sarasa-Ui-PropDigits-report.json").read_text(encoding="utf-8"))
    ots = json.loads((REPORT_DIR / "ots-audit.json").read_text(encoding="utf-8"))
    if original_report.get("version") != VERSION:
        raise ValueError("布局刷新只能处理当前项目版本")
    inputs = [(static_dir(region, hinted) / static_output_name(region, str(stop["name"]), italic),
               region, str(stop["name"]), italic, hinted)
              for region in regions for stop in SOURCE_HAN_WEIGHT_STOPS for italic in (False, True) for hinted in (False, True)]
    inputs.extend((VARIABLE_DIR / variable_output_name(region, italic), region, None, italic, False)
                  for region in regions for italic in (False, True))
    inputs.sort(key=lambda item: item[2] is not None)
    verified = {}
    for path, region, weight, italic, hinted in inputs:
        digest = file_sha256(path)
        original = ots.get("input_manifest", {}).get(portable_report_path(path), {})
        try:
            receipt = json.loads(output_validation_record_path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {}
        receipt_matches = all(receipt.get(key) == value for key, value in
                              {"file": path.name, "sha256": digest, "region": region, "weight": weight,
                               "italic": italic, "hinted": hinted}.items())
        if digest != original.get("sha256") and not receipt_matches:
            raise ValueError(f"布局刷新来源没有匹配的成品哈希：{path.name}")
        verified[path] = digest
    report = copy.deepcopy(original_report)
    report["source_lockfile_normalization"] = {
        "previous_sha256": report["bootstrap_sources"].get("sarasa_package_lock_sha256"),
        "canonical_git_blob_sha256": SARASA_PACKAGE_LOCK_SHA256,
        "policy": "使用固定 Git 提交的原始 LF 字节；依赖版本不变",
    }
    report["bootstrap_sources"]["sarasa_package_lock_sha256"] = SARASA_PACKAGE_LOCK_SHA256
    updated = {}
    with staged_build_outputs(regions, static_only=False, variable_only=False, unhinted_only=False,
                              resume_static=True, resume_variable=True):
        for original_path, region, weight, italic, hinted in inputs:
            path = (VARIABLE_DIR / original_path.name if weight is None else static_dir(region, hinted) / original_path.name)
            if file_sha256(path) != verified[original_path]:
                raise RuntimeError("布局刷新暂存期间输入字体发生变化")
            protected = sfnt_table_hashes(path, {"head", "GSUB", "GPOS", "GDEF"})
            pending = path.with_name(path.name + ".finalization.tmp")
            log_step(f"layout-only {len(updated) + 1}/{len(inputs)}: {original_path.name}")
            try:
                with TTFont(path, lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
                    original_head = font.reader["head"]
                    details = install_continuous_vertical_dash_layout(font)
                    font.save(pending, reorderTables=True)
                with TTFont(pending, lazy=True) as candidate:
                    candidate_head = candidate.reader["head"]
                if original_head[:8] + original_head[12:] != candidate_head[:8] + candidate_head[12:]:
                    raise RuntimeError("布局刷新改变了 head 校验和之外的字段")
                if protected != sfnt_table_hashes(pending, {"head", "GSUB", "GPOS", "GDEF"}):
                    raise RuntimeError("布局刷新改变了受保护的轮廓、度量或 hint 表")
                weights = AXIS_POINTS if weight is None else (None,)
                runtime_cases = 0
                for axis_weight in weights:
                    status = vertical_segment_runtime_status(pending, {"wght": axis_weight} if axis_weight else None)
                    runtime_cases += status["cases"]
                    if not status["ok"]:
                        raise RuntimeError(f"连续竖排连接没有通过运行时校验：{original_path.name}；{status['samples'][:2]}")
                pending.replace(path)
                record_verified_output(path, region, italic, weight, hinted)
                updated[portable_report_path(path)] = {
                    **details, "layout_only_refresh": True, "hint_rerun": False,
                    "previous_sha256": verified[original_path], "sha256": file_sha256(path),
                    "protected_table_sha256": protected, "continuous_vertical_runtime_cases": runtime_cases,
                    "head_fields_preserved": True,
                }
            finally:
                pending.unlink(missing_ok=True)
        for field in ("static_outputs", "variable_outputs"):
            for item in report[field]:
                if item["file"] in updated:
                    item.update(updated[item["file"]])
        report["layout_refresh"] = {"fonts": len(updated), "version": VERSION, "hint_rerun": False,
                                    "protected_tables_verified": True}
        report["method"] = production_method_description()
        write_static_readme(regions)
        write_reports(report)
        if any(file_sha256(path) != digest for path, digest in verified.items()):
            raise RuntimeError("布局刷新期间原成品发生变化，拒绝覆盖")
    return report


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
    prepared_roles = upstream_dash_roles(font)
    prepared_segments = vertical_dash_segments(font, prepared_roles)
    if not prepared_segments:
        raise ValueError("静态成品缺少 hint 前准备并经 pass2 保留的竖排分段")
    prepared_layout = (prepared_roles, normalized_source_han_dash_locl_mappings(font, prepared_roles), prepared_segments)
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
    preserved_product_codepoints = PROPDIGITS_CODEPOINTS | DASH_CMAP_CODEPOINTS
    try:
        reference_style = static_reference_style_name(weight_name, italic)
        reference_path = static_reference_font_path(region, weight_name, italic)
        report["static_reference_style"] = reference_style
        report["static_reference_path"] = str(reference_path)
        reference: TTFont | None = None
        if reference_path.exists():
            reference = TTFont(reference_path, recalcBBoxes=False, recalcTimestamp=False)
            try:
                report.update(restrict_cmap_to_reference(font, reference, preserved_product_codepoints))
                if uses_official_glyph_baseline:
                    metric_codepoints_kept = set(preserved_product_codepoints)
                    if region_config(region)["classical"]:
                        classical = reference_outline_divergences(font, reference, preserved_product_codepoints)
                        report["classical_source_placement_kept"] = len(classical)
                        metric_codepoints_kept |= classical
                    report.update(align_reference_advances(font, reference, metric_codepoints_kept))
                    report.update(align_reference_hmtx_lsb(font, reference, metric_codepoints_kept))
                    report.update(align_tnum_digit_targets(font, reference))
                    report.update(align_reference_vmtx(font, reference, metric_codepoints_kept))
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
        update_os2_sarasa_metadata(font, region)
        rebuild_static_stat(font, weight_name, weight_value, italic)
        report.update(drop_generated_extra_tables(font, keep_stat=True))
        report.update(apply_static_propdigits(font))
        if reference and uses_official_glyph_baseline:
            report.update(sync_static_glyf_from_reference(font, reference, preserved_product_codepoints))
        elif reference:
            report["extension_source_glyf_preserved"] = True
        report.update(add_digit_colon_feature(font))
        if reference:
            report.update(align_layout_feature_template(font, reference, "GSUB"))
            report.update(apply_upstream_dash_behavior(font, region, italic=italic, prepared_layout=prepared_layout))
            report.update(align_layout_feature_template(font, reference, "GPOS"))
            subset_to_current_cmap(font)
            report.update(align_layout_feature_template(font, reference, "GSUB"))
            report.update(apply_upstream_dash_behavior(font, region, italic=italic, prepared_layout=prepared_layout))
            report.update(align_layout_feature_template(font, reference, "GPOS"))
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
        update_os2_sarasa_metadata(font, region)
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
                    preserved_product_codepoints,
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
        report.update(add_digit_colon_feature(font))
        report.update(recalculate_head_bounds(font))
        report.update(install_continuous_vertical_dash_layout(font))
        report.update(normalize_ideograph_gdef_classes(font))
        report["gpos_pair_sets_sorted"] = font_layout.sort_pair_sets(font)
        report["unreferenced_layout_lookups_removed"] = font_layout.prune_unreferenced_lookups(font)
        font["OS/2"].usMaxContext = maxCtxFont(font)
        font.save(path, reorderTables=True)
    finally:
        if post_layout_reference is not None:
            post_layout_reference.close()
        font.close()
    return report


def prepare_static_pass1_derivatives(path: Path, region: str, italic: bool, dash_source_path: Path,
                                    western_source_path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    derivative = path.with_name("pass1.project.ttf")
    font = TTFont(path, recalcBBoxes=False, recalcTimestamp=False)
    with TTFont(dash_source_path, recalcBBoxes=False, recalcTimestamp=False) as source:
        with TTFont(western_source_path or dash_source_path, recalcBBoxes=False, recalcTimestamp=False) as western:
            report = apply_upstream_dash_behavior(font, region, static_source=source, western_source=western, italic=italic)
    report.update(recalculate_head_bounds(font))
    font.save(derivative, reorderTables=True)
    font.close()
    return derivative, {"pass1_project_geometry_prepared_before_hinting": True, **report}


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
    fragments["pass1"], pass1_derivative_report = prepare_static_pass1_derivatives(
        fragments["pass1"], region, italic, Path(fragments["dash_source_path"]),
        Path(fragments["western_dash_source_path"]))

    unhinted_output: dict[str, Any] | None = None
    if emit_output:
        unhinted_tmp = tmp_dir / "unhinted" / region / static_output_name(region, weight_name, italic)
        unhinted_report_path = unhinted_tmp.with_suffix(".report.json")
        unhinted_tmp.parent.mkdir(parents=True, exist_ok=True)
        unhinted_path = static_dir(region, False) / static_output_name(region, weight_name, italic)
        try:
            unhinted_report = json.loads(unhinted_report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unhinted_report = None
        cache_inputs = {name: file_sha256(Path(fragments[name])) for name in
                        ("pass1", "kanji", "hangul", "dash_source_path", "western_dash_source_path")}
        if (
            isinstance(unhinted_report, dict)
            and unhinted_tmp.is_file()
            and unhinted_report.get("cache_binding") == {
                "schema": 1, "builder_sha256": BUILDER_CODE_SHA256,
                "inputs": cache_inputs, "output_sha256": file_sha256(unhinted_tmp),
                "output_size": unhinted_tmp.stat().st_size,
            }
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
            unhinted_report["cache_binding"] = {
                "schema": 1, "builder_sha256": BUILDER_CODE_SHA256, "inputs": cache_inputs,
                "output_sha256": file_sha256(unhinted_tmp), "output_size": unhinted_tmp.stat().st_size,
            }
            write_json_atomic(unhinted_report_path, unhinted_report)
        shutil.copy2(unhinted_tmp, unhinted_path)
        record_verified_output(unhinted_path, region, italic, weight_name, False)
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
    try:
        cached_hint = json.loads(pass1_hinted_report.read_text(encoding="utf-8")) if pass1_hinted_report.exists() else {}
    except (ValueError, OSError):
        cached_hint = {}
    if not isinstance(cached_hint, dict):
        cached_hint = {}
    expected_options = list(UI_AUTOHINT_OPTIONS)
    expected_control = ui_hint_control(fragments["pass1"])
    reusable_hint = (
        pass1_hinted.is_file() and cached_hint.get("hint_tool_identity") == ttfautohint_identity()
        and cached_hint.get("hint_options") == expected_options
        and cached_hint.get("hint_control") == expected_control
        and cached_hint.get("hint_control_sha256") == hashlib.sha256(expected_control.encode("ascii")).hexdigest()
        and cached_hint.get("input_sha256") == file_sha256(fragments["pass1"])
        and cached_hint.get("output_sha256") == file_sha256(pass1_hinted)
    )
    if reusable_hint:
        log_step(f"static {style_label}: reuse ttfautohint pass1")
        hint_report = cached_hint
    else:
        log_step(f"static {style_label}: ttfautohint pass1")
        hint_report = hint_static_font(fragments["pass1"], pass1_hinted, weight_name)
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
        record_verified_output(Path(context["hinted_path"]), region, italic, weight_name, True)
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
    write_static_readme(regions, (False,))
    with tempfile.TemporaryDirectory(prefix="sarasa-unhinted-") as tmp_name:
        for stop in SOURCE_HAN_WEIGHT_STOPS:
            for region in regions:
                for italic in (False, True):
                    path = static_dir(region, False) / static_output_name(region, str(stop["name"]), italic)
                    if only_missing:
                        valid, reasons = unhinted_reference_status(path, region, str(stop["name"]), italic)
                        if valid:
                            continue
                        log_step(f"unhinted {region} {stop['name']}: rebuild invalid reference: {'; '.join(reasons)}")
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
            if any(path.name not in expected_names for path in out_dir.glob("*.ttf")):
                raise RuntimeError("输出目录包含非本次构建的字体；请先检查目录")

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
                                "hint_recipe_sha256": static_hint_recipe_fingerprint(weight_name),
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
            "has_hints": has_grid_fitting_tables(font),
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
    default = "拉丁、西里尔、希腊文字使用 Source Han 的西文语言路由；hani/kana/hang/bopo 的默认语言采用地区 CJK 形式。KOR 单破折号保留比例形态。"
    return f"""Sarasa Ui PD {region} {VERSION}（{variant}）

本包包含 200 ExtraLight、300 Light、400 Regular、600 SemiBold、700 Bold、900 Heavy
及对应 Italic，共 12 个静态 TTF。350 仅是 VF 隐藏锚点，不提供静态样式。

来源为 {source} 与 Inter 4.1，按 Sarasa {SARASA_VERSION} 的 pass1/kanji/hangul/pass2
静态流程构建，不从 VF 实例化。600 配对 Source Han 500 和 Inter 600。
CL 的公开 cmap/layout 限于 Sarasa Ui CL 边界。

37 个数字、空格、标点和运算符默认使用 Inter 比例形态；tnum 恢复 tabular，pnum 不改变默认宽度。
冒号复用 Inter 的上下文规则，在 tnum 之前执行；1:2、1:、:2 上浮，
1:a、a:2、a:b 保持原位。tnum 与 zero 可同时启用。{default}
破折号、省略号和竖排沿用对应 Source Han/Shanggu 字形，保持
ccmp → locl → vert/vrt2 顺序。中文双省略号为两个居中 glyph，共 2em；
中文双连、三连破折号分别为 2em、3em；竖排拆为连续的 1em 片段，墨迹不越出各自 advance。
CL 中文破折号使用 Shanggu，西文使用 Source Han K。分段在 hint 前生成，最终 head 按全部字形重算。

chws/vchw 来自 Noto CJK 交付后处理；是否自动启用取决于实际应用。
静态竖排原点由最终 glyf/vmtx 决定，移除不适用于 TrueType 的 VORG。
post 使用 format 3，不保存虚构 glyph 名称。命名和布局修订不会重新 hint。
unhinted 仅保留启用 smart dropout 的最小 prep，不含逐字形 hint、fpgm 或 cvt。
家族名缩写为 PD，字重名称不缩写；Windows win 度量仍为上游 968/241，GDI 可能裁切部分超界字形。

请阅读包根目录的中文 README.md、NOTICE.md 和 LICENSE.txt。
完整版本的主审计、OTS、分地区 FontBakery、视觉检查与 21 包校验报告见仓库 reports。
这些字体按 SIL Open Font License 1.1 分发，是修改版字体，不是任何上游的官方发布。
"""



def write_static_readme(regions: list[str], variants: tuple[bool, ...] = (True, False)) -> None:
    for region in regions:
        for hinted in variants:
            directory = static_dir(region, hinted)
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "LICENSE", directory / "LICENSE-Sarasa-Gothic.txt")
            (directory / "README.txt").write_text(static_readme_text(region, hinted), encoding="utf-8", newline="\n")


def canonical_output_path(path: Path) -> Path:
    path = path.resolve()
    if OUTPUT_STAGING_ROOT is not None and path.is_relative_to(OUTPUT_STAGING_ROOT.resolve()):
        return ROOT / path.relative_to(OUTPUT_STAGING_ROOT.resolve())
    return path


def portable_report_path(path: Path) -> str:
    try:
        return Path(os.path.relpath(canonical_output_path(path), ROOT)).as_posix()
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


def production_method_description() -> str:
    return (
        f"固定 Sarasa Gothic {SARASA_VERSION}、Source Han Sans 2.005R、Inter 4.1、Shanggu Sans 1.028 "
        "与 Node.js 26.7.0；归档和已解包来源均按固定 SHA-256 核验。"
        "公开字重为 200/300/400/600/700/900，350 仅为隐藏 avar 锚点。"
        "CJK public 200/600 分别使用内部 250/500，Inter 使用公开数字坐标；"
        "先应用 Source Han public axis，再重映射 Inter 轮廓和 GPOS 变化。"
        "SC/TC/HC/J/K 各用对应地区 Source Han；CL 使用 K 基底与 Shanggu TC 覆盖，"
        "公开 cmap/layout 按 Sarasa Ui CL 边界裁剪。"
        "静态字体走原生 pass1/kanji/hangul/pass2，绝不由 VF 实例化。"
        "每字重建立同序的 96 个 pass1 与 12 个 CJK 输入，统一 instruct 后由 pass2 合成。"
        "Ui pass1 使用 --fallback-script=none --fallback-scaling；原生控制文件将 U+0360 "
        "归入相同 fallback，正常拉丁字母和数字保留原有 hint。生成后检查全部 Ui pass1 字形在 "
        "6–200 px、灰阶/单色/LCD 下的严格指令执行。实际 hinter 执行文件、版本、选项、"
        "控制文件和来源哈希参与相关缓存校验；布局、名称或版权刷新保护轮廓与 hint。"
        "静态 post 保持 format 3，竖排原点来自最终 glyf/vmtx，移除 TrueType 中的 VORG。"
        "VF 直接合并 CJK 与 Inter，合并 GDEF 变化存储、可变定位和 mark filtering sets。"
        "Italic CJK 先物化 IUP 隐含增量再剪切，metric phantom points 不剪切；"
        "用 13 个轴点检查复合字形。最终保留 HVAR，横向原点补偿转为轮廓与组件变化，"
        "移除 VVAR/VORG，竖排统一使用 gvar，并保持既有运行时轮廓边界和度量。"
        "37 个数字、空格、标点与运算符默认比例宽，tnum 恢复 tabular，pnum 不改变默认宽度；zero 及可达替代数字共享严格等宽变化曲线。"
        "ss03/cv10 保留替代字形自己的定位；完整恢复固定 Inter 的上下文大小写规则与抑制规则，"
        "冒号规则在数字宽度替换前执行，兼容字符串边界、长串冒号和烘焙后的字母身份。"
        "西文文字系统按 Source Han 的语言路由；hani/kana/hang/bopo 默认采用地区标点，双省略号严格 2em。"
        "CL 中文破折号使用 Shanggu，西文使用 Source Han K。竖排长破折号拆为连续的 1em 片段，"
        "轮廓在 hint 前生成，最终重算全部字形的 head 包围盒。保留 Source Han/Shanggu 的 "
        "ccmp→locl→vert/vrt2 及 KOR 单破折号特例。最后追加 Noto CJK 交付后处理的 chws/vchw。"
        "命名、STAT、版权、版本和下划线度量按项目统一。发布审计使用明确的 23 节注册表，"
        "包含全码位、两端真实 HarfBuzz、跨引擎、布局、地区边界、指令和独立栅格参照。"
        "栅格参照从固定上游与独立默认 ttfautohint 分类构造，仅应用声明的 fallback 策略；"
        "原始上游像素差异另行记录。最终还需 OTS、独立环境下的分地区 FontBakery、"
        "逐图视觉验收与 21 包校验，任何覆盖缺失不得声明完整通过。"
    )


def production_intentional_differences() -> list[str]:
    return [
        "37 个数字、空格、标点和运算符默认比例宽，tnum 恢复 tabular，pnum 不改变默认宽度；tnum+zero 数字在各引擎内严格等宽。",
        "公开 200/600 分别配对 CJK 250/500 与 Inter 200/600，350 仅为隐藏锚点；Heavy 900 是正式扩展样式。",
        "完整的 Inter 上下文规则与字母抑制规则在烘焙 ss03/cv10 后继续有效，并保留替代字形自己的定位。",
        "西文文字系统采用 Source Han 的语言路由；hani/kana/hang/bopo 默认采用地区 CJK 形式。CL 中文破折号使用 Shanggu，西文使用 Source Han K；CJK 双省略号严格 2em。",
        "竖排连续破折号以 1em 片段接续，每段墨迹不越界，连接边严格一致；静态分段轮廓在 hint 前生成，最终 head 包围盒重新计算。",
        "CL 使用 Source Han K 和 Shanggu TC 来源，公开 cmap/layout 按 Sarasa Ui CL 边界裁剪。",
        "Ui 原生 hint 对未分类字形及 U+0360 使用明确的 fallback 策略，修复无效指令；正常拉丁字母和数字的 hint 保持有效。",
        "VF 使用 HVAR 与 gvar 的统一度量表示，移除 VVAR/VORG；静态竖排使用最终 glyf/vmtx，post 保持 format 3。",
        "追加 Noto CJK 交付流程的 chws/vchw，是否自动启用由实际引擎和应用决定。",
        "静态与 VF 分别沿用固定静态源和可变源；记录源轮廓差异，并在公开字重以两端真实 HarfBuzz 对齐横竖度量。",
    ]


def write_reports(build_report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    declared = {item["file"] for field in ("static_outputs", "variable_outputs") for item in build_report.get(field, [])}
    expected = {f"fonts/variable/{variable_output_name(region, italic)}"
                for region in REGION_ORDER for italic in (False, True)}
    expected.update(f"fonts/static/{static_dir(region, hinted).name}/{static_output_name(region, str(stop['name']), italic)}"
                    for region in REGION_ORDER for hinted in (False, True)
                    for stop in SOURCE_HAN_WEIGHT_STOPS for italic in (False, True))
    complete = declared == expected
    scope = sorted({str(item["region"]) for field in ("static_outputs", "variable_outputs")
                    for item in build_report.get(field, []) if "region" in item})
    suffix = "" if complete else ".partial-" + ("-".join(scope) or "selection")
    report = {**build_report, "complete_font_matrix": complete, "declared_font_count": len(declared)}
    report = sanitize_report_data(report)
    assert_portable_report_text(json.dumps(report, ensure_ascii=False))
    write_json_atomic(REPORT_DIR / f"Sarasa-Ui-PropDigits-report{suffix}.json", report)
    font_paths = (sorted(VARIABLE_DIR.glob("*.ttf"))
                  + sorted(STATIC_ROOT.glob(f"SarasaUiPropDigits*-TTF-{VERSION}/*.ttf"))
                  + sorted(STATIC_ROOT.glob(f"SarasaUiPropDigits*-TTF-Unhinted-{VERSION}/*.ttf")))
    inspection = {"title": "Sarasa Ui PropDigits 字体检查", "complete_font_matrix": complete,
                  "fonts": [inspect_font(path) for path in font_paths if portable_report_path(path) in declared]}
    inspection = sanitize_report_data(inspection)
    assert_portable_report_text(json.dumps(inspection, ensure_ascii=False))
    write_json_atomic(REPORT_DIR / f"font-inspection{suffix}.json", inspection)


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
    source_path = source_han_vf_path(region)
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
            if len(vertical[0]) != 2 or vertical[1] != (0, -2 * upem):
                fail(
                    weight,
                    f"{language}-vertical",
                    [vertical[0], vertical[1]],
                    ["two 1em segments", (0, -2 * upem)],
                )
            vrt2_pattern_ok = len(vrt2_only[0]) == 2 and vrt2_only == vertical
            if not vrt2_pattern_ok or vrt2_only[1] != (0, -2 * upem):
                fail(
                    weight,
                    f"{language}-vrt2-only",
                    [vrt2_only[0], vrt2_only[1]],
                    ["the same two 1em segments as vert", (0, -2 * upem)],
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
            expected_signature = ((2, source_signature[1]) if direction == "ttb" else source_signature)
            if target_signature != expected_signature:
                fail(
                    float(public_weight),
                    f"source-parity-{case_name}",
                    target_signature,
                    expected_signature,
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


def variable_output_resume_status(region: str, italic: bool, *, verify_record: bool = True) -> tuple[bool, dict[str, Any]]:
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
        identity = output_identity_status(font, region, italic, None)
        details["output_identity"] = identity
        reasons.extend(identity["reasons"])
        if verify_record:
            valid_record, record_reason = verified_output_record_status(path, region, italic)
            if not valid_record:
                reasons.append(record_reason)
        context_status = contextual_case_structure_status(font)
        details["contextual_case_structure"] = context_status
        if not context_status["ok"]:
            reasons.append(context_status["reason"])
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


def _build_all_in_workspace(
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
        "method": production_method_description(),
        "intentional_differences_from_upstream_sarasa_ui": production_intentional_differences(),
        "final_gsub_features": sorted(FINAL_GSUB_FEATURES),
        "variable_outputs": variable_outputs,
        "static_outputs": static_outputs,
    }
    write_reports(report)
    return report


def promote_build_outputs(pairs: list[tuple[Path, Path]], backup_root: Path, *,
                          validation_root: Path | None = None,
                          validation_backup_root: Path | None = None) -> None:
    """Promote completed files/directories, rolling back every rename on error."""
    completed: list[tuple[Path, Path, Path, bool]] = []
    try:
        for index, (source, destination) in enumerate(pairs):
            is_receipt = (validation_root is not None
                          and destination.resolve().parent == validation_root.resolve()
                          and re.fullmatch(r"[0-9a-f]{64}\.json", destination.name) is not None)
            if not destination.resolve().is_relative_to(ROOT.resolve()) and not is_receipt:
                raise ValueError("构建成品目的路径越界")
            if not source.exists():
                raise FileNotFoundError(source)
            backup = (validation_backup_root if is_receipt and validation_backup_root is not None
                      else backup_root) / str(index)
            backup.parent.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            existed = destination.exists()
            if existed:
                destination.replace(backup)
            completed.append((source, destination, backup, existed))
            source.replace(destination)
    except BaseException as original_error:
        rollback_errors = []
        for source, destination, backup, existed in reversed(completed):
            try:
                if destination.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    destination.replace(source)
            except OSError as error:
                rollback_errors.append(error)
            try:
                if existed and backup.exists():
                    backup.replace(destination)
            except OSError as error:
                rollback_errors.append(error)
        if rollback_errors:
            raise BaseExceptionGroup("构建失败；已尝试全部回滚，部分路径需要恢复", [original_error, *rollback_errors])
        raise


def recycle_completed_transaction(transaction: Path) -> None:
    allowed = (ROOT / ".build-cache" / "transactions").resolve()
    source = transaction.resolve()
    if source.parent != allowed or transaction.is_symlink() or transaction.is_junction():
        raise ValueError("事务清理路径不在已验证的暂存目录内")
    state = json.loads((source / "status.json").read_text(encoding="utf-8"))
    if state.get("status") != "promoted":
        raise ValueError("只能清理已经完成提交的事务")
    if os.name == "nt":
        script = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                  "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
                  "$env:SARASA_COMPLETED_TRANSACTION, "
                  "[Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
                  "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, "SARASA_COMPLETED_TRANSACTION": str(source)}, check=True)
    else:
        trash = (Path.home() / ".Trash" if sys.platform == "darwin" else
                 Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "Trash")
        directory = trash if sys.platform == "darwin" else trash / "files"
        directory.mkdir(parents=True, exist_ok=True)
        name = source.name + "-" + uuid.uuid4().hex[:8]
        if sys.platform != "darwin":
            info = trash / "info"; info.mkdir(parents=True, exist_ok=True)
            (info / (name + ".trashinfo")).write_text(
                "[Trash Info]\nPath=" + urllib.parse.quote(str(source))
                + "\nDeletionDate=" + datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n", encoding="utf-8")
        shutil.move(str(source), str(directory / name))


@contextmanager
def staged_build_outputs(regions: list[str], *, static_only: bool, variable_only: bool,
                         unhinted_only: bool, resume_static: bool, resume_variable: bool,
                         include_static: bool = True, italic_styles: tuple[bool, ...] = (False, True)):
    global STATIC_ROOT, VARIABLE_DIR, REPORT_DIR, OUTPUT_STAGING_ROOT, OUTPUT_VALIDATION_DIR
    saved = (STATIC_ROOT, VARIABLE_DIR, REPORT_DIR, OUTPUT_STAGING_ROOT, OUTPUT_VALIDATION_DIR)
    # Font replacements stay on the repository's filesystem even when the
    # build cache is on another volume. Receipts use their own local stage.
    transaction_root = ROOT / ".build-cache" / "transactions"
    transaction_root.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix=f"build-{VERSION}-", dir=transaction_root))
    stage = transaction / "candidate"
    variants = (False,) if variable_only or unhinted_only else (False, True)
    destinations = [static_dir(region, hinted) for region in regions for hinted in variants] if include_static else []
    seed_static = resume_static or variable_only
    try:
        for destination in destinations:
            candidate = stage / destination.relative_to(ROOT)
            candidate.mkdir(parents=True, exist_ok=True)
            if seed_static and destination.exists():
                shutil.copytree(destination, candidate, dirs_exist_ok=True)
        if resume_variable and not (static_only or unhinted_only):
            for region in regions:
                for italic in italic_styles:
                    destination = VARIABLE_DIR / variable_output_name(region, italic)
                    if destination.is_file():
                        candidate = stage / destination.relative_to(ROOT)
                        candidate.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(destination, candidate)
        validation = transaction / "validation"
        validation.mkdir(parents=True, exist_ok=True)
        if OUTPUT_VALIDATION_DIR.exists():
            shutil.copytree(OUTPUT_VALIDATION_DIR, validation, dirs_exist_ok=True)
        STATIC_ROOT = stage / "fonts" / "static" if include_static else saved[0]
        VARIABLE_DIR = stage / "fonts" / "variable"
        REPORT_DIR = stage / "reports"
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_STAGING_ROOT = stage
        OUTPUT_VALIDATION_DIR = validation
        write_json_atomic(transaction / "status.json", {"version": VERSION, "status": "building"})
        yield transaction
        pairs = [(stage / destination.relative_to(ROOT), destination) for destination in destinations]
        if not (static_only or unhinted_only):
            pairs.extend((VARIABLE_DIR / variable_output_name(region, italic),
                          saved[1] / variable_output_name(region, italic))
                         for region in regions for italic in italic_styles)
        # Each candidate must carry an attestation to its current bytes. The
        # main release audit remains a separate, mandatory gate after building.
        candidate_receipts: set[Path] = set()
        for source, _destination in pairs:
            if source.is_dir() and len(list(source.glob("*.ttf"))) != 2 * len(SOURCE_HAN_WEIGHT_STOPS):
                raise RuntimeError("暂存地区目录没有完整的十二个静态字体")
            for font_path in (source.glob("*.ttf") if source.is_dir() else (source,)):
                record_path = output_validation_record_path(font_path)
                if not record_path.is_file():
                    raise RuntimeError(f"构建成品缺少校验记录：{font_path.name}")
                candidate_receipts.add(record_path)
                record = json.loads(record_path.read_text(encoding="utf-8"))
                valid, reason = verified_output_record_status(font_path, record["region"], record["italic"],
                                                              record["weight"], record["hinted"])
                if not valid:
                    raise RuntimeError(f"暂存成品已改变：{font_path.name}；{reason}")
        pairs.extend((path, saved[2] / path.name) for path in REPORT_DIR.iterdir() if path.is_file())
        # Another region/format may finish while this transaction is building.
        # Its fresh receipt must not be overwritten by our initial snapshot.
        saved[4].parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".output-validation-", dir=saved[4].parent) as receipt_stage_raw:
            receipt_stage = Path(receipt_stage_raw)
            for path in sorted(candidate_receipts):
                local_candidate = receipt_stage / path.name
                shutil.copy2(path, local_candidate)
                pairs.append((local_candidate, saved[4] / path.name))
            promote_build_outputs(pairs, transaction / "previous", validation_root=saved[4],
                                  validation_backup_root=receipt_stage / "previous")
        write_json_atomic(transaction / "status.json", {"version": VERSION, "status": "promoted"})
        try:
            recycle_completed_transaction(transaction)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            log_step(f"成品已提交，暂存目录待清理：{transaction.name}；{type(error).__name__}")
    except BaseException:
        write_json_atomic(transaction / "status.json", {"version": VERSION, "status": "failed", "candidate_retained": True})
        raise
    finally:
        STATIC_ROOT, VARIABLE_DIR, REPORT_DIR, OUTPUT_STAGING_ROOT, OUTPUT_VALIDATION_DIR = saved


def build_all(static_only: bool = False, regions: list[str] | None = None,
              resume_variable: bool = False, resume_static: bool = False,
              force_static_weights: set[str] | None = None,
              variable_only: bool = False, unhinted_only: bool = False) -> dict[str, Any]:
    regions = parse_regions(",".join(regions)) if regions is not None else list(REGION_ORDER)
    BUILD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with staged_build_outputs(regions, static_only=static_only, variable_only=variable_only,
                              unhinted_only=unhinted_only, resume_static=resume_static,
                              resume_variable=resume_variable):
        return _build_all_in_workspace(static_only, regions, resume_variable, resume_static,
                                       force_static_weights, variable_only, unhinted_only)


def refresh_version_metadata_outputs(source_version: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", source_version) or source_version == VERSION:
        raise ValueError("必须指定不同于当前版本的原项目版本号")
    original = json.loads((REPORT_DIR / "Sarasa-Ui-PropDigits-report.json").read_text(encoding="utf-8"))
    ots = json.loads((REPORT_DIR / "ots-audit.json").read_text(encoding="utf-8"))
    if original.get("version") != source_version or ots.get("successful") != 156 or ots.get("failures") or ots.get("unexpected_messages"):
        raise ValueError("原版本构建记录或完整 OTS 输入记录不符")
    outputs = [*original["static_outputs"], *original["variable_outputs"]]
    manifest = ots["input_manifest"]
    if len(outputs) != 156 or len(manifest) != 156 or {item["file"] for item in outputs} != set(manifest):
        raise ValueError("版本刷新要求同一组完整的 156 个字体")
    for relative, expected in manifest.items():
        path = ROOT / relative
        if not path.resolve().is_relative_to((ROOT / "fonts").resolve()):
            raise ValueError("原字体路径越界")
        if not path.is_file() or path.stat().st_size != expected["size"] or file_sha256(path) != expected["sha256"]:
            raise ValueError(f"原字体已改变：{relative}")
    previous_bootstrap = copy.deepcopy(original["bootstrap_sources"])
    report = copy.deepcopy(original)
    report.update(version=VERSION, method=production_method_description())
    report["bootstrap_sources"].update(
        sarasa_gothic=SARASA_TAG, sarasa_commit=SARASA_COMMIT,
        sarasa_package_lock_sha256=SARASA_PACKAGE_LOCK_SHA256,
        sarasa_ui_ttf=f"{SARASA_VERSION} hinted/unhinted",
    )
    report["reference_unicode_set_by_region"] = {
        region: portable_report_path(reference_font_path(region, "Regular", False)) for region in REGION_ORDER}
    report["version_metadata_refresh"] = {"from_version": source_version, "to_version": VERSION,
        "original_bootstrap_sources": previous_bootstrap, "fonts": 156,
        "only_head_and_name_changed": True, "hint_rerun": False}
    BUILD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with staged_build_outputs(list(REGION_ORDER), static_only=False, variable_only=False,
                              unhinted_only=False, resume_static=False, resume_variable=True):
        for kind in ("static_outputs", "variable_outputs"):
            for item in report[kind]:
                region = str(item["region"])
                if kind == "variable_outputs":
                    styles = {variable_output_name(region, italic): italic for italic in (False, True)}
                    italic = styles[Path(item["file"]).name]
                else:
                    italic = bool(item["italic"])
                weight = str(item["weight"]) if kind == "static_outputs" else None
                hinted = bool(item["hinted_variant"]) if weight is not None else False
                source = ROOT / item["file"]
                if file_sha256(source) != manifest[item["file"]]["sha256"]:
                    raise ValueError("版本刷新期间原字体发生变化")
                destination = (static_dir(region, hinted) / static_output_name(region, weight, italic)
                               if weight is not None else VARIABLE_DIR / variable_output_name(region, italic))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                log_step(f"version metadata {source_version} -> {VERSION}: {source.name}")
                item["version_metadata_refresh"] = refresh_version_metadata_font(destination, source_version)
                item["file"] = portable_report_path(destination)
                item["head_font_revision"] = FONT_REVISION
                item["rebuilt"] = False
                record_verified_output(destination, region, italic, weight, hinted)
        for region in REGION_ORDER:
            for hinted in (False, True):
                shutil.copy2(ROOT / "LICENSE", static_dir(region, hinted) / "LICENSE-Sarasa-Gothic.txt")
        write_static_readme(list(REGION_ORDER))
        write_reports(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--static-only", action="store_true", help="只重建静态 hinted/unhinted TTF，不重建 VF 输出。")
    modes.add_argument("--variable-only", action="store_true", help="只重建所选地区 VF；缺少静态度量参考时自动准备 unhinted，完全跳过 hint 流程。")
    modes.add_argument("--unhinted-only", action="store_true", help="只按 Sarasa 静态片段路径重建所选地区 unhinted TTF，完全跳过 hint 流程。")
    modes.add_argument("--refresh-layout-only", action="store_true", help="整组刷新现有静态与 VF 的连续竖排连接；逐表保护轮廓、度量及 hint，并验证长串运行时后提交。")
    parser.add_argument("--refresh-version-from", help="将有完整 OTS 输入记录的旧版本整组迁移到当前版本，只改 head/name；不重画轮廓、不重新 hint，仍须重新通过发布审计。")
    parser.add_argument(
        "--refresh-static-finalization-only",
        action="store_true",
        help="只刷新现有静态 TTF 的最终 GSUB、命名、法律信息与 unhinted 栅格表；除明确白名单表外逐表保护，不重新 hint。",
    )
    parser.add_argument("--refresh-variable-finalization-only", action="store_true", help="刷新现有 VF 的 GSUB、数字等宽、标点宽度、跨引擎度量及四个竖排片段的原生静态控制点；保护 GPOS 等表，并核对 13 点运行时边界与度量。")
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
    if args.refresh_layout_only:
        if args.refresh_version_from or args.refresh_static_finalization_only or args.refresh_variable_finalization_only or args.resume_static or args.resume_variable or force_static_weights:
            parser.error("布局刷新不能与其他构建模式组合")
        report = refresh_layout_outputs(parse_regions(args.regions))
        print(json.dumps({"version": VERSION, "mode": "layout-only", **report["layout_refresh"]}, ensure_ascii=False))
        return
    if args.refresh_version_from:
        if (args.static_only or args.variable_only or args.unhinted_only or args.resume_static
                or args.resume_variable or args.refresh_static_finalization_only
                or args.refresh_variable_finalization_only or force_static_weights
                or set(parse_regions(args.regions)) != set(REGION_ORDER)):
            parser.error("版本元数据刷新必须独立处理完整六地区")
        refresh_version_metadata_outputs(args.refresh_version_from)
        print(json.dumps({"version": VERSION, "fonts": 156, "mode": "version-metadata-only", "hint_rerun": False}, ensure_ascii=False))
        return
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
