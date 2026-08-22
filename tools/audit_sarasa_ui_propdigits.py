from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any


AUDIT_DEPS = {
    "freetype": ("freetype-py", "freetype-py==2.5.1", "2.5.1"),
    "numpy": ("numpy", "numpy==2.4.2", "2.4.2"),
}


def ensure_audit_deps() -> None:
    if os.environ.get("SARASA_SKIP_PYTHON_DEPS") == "1":
        return
    needed = []
    for module, (distribution, package_spec, expected_version) in AUDIT_DEPS.items():
        try:
            installed_version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            installed_version = None
        if importlib.util.find_spec(module) is None or installed_version != expected_version:
            needed.append(package_spec)
    if needed:
        print(f"[audit] install Python audit dependencies: {' '.join(needed)}", flush=True)
        subprocess.check_call([sys.executable, "-m", "pip", "install", *needed])


ensure_audit_deps()

import freetype
import numpy as np
import uharfbuzz as hb
from fontTools.misc.fixedTools import otRound
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.perimeterPen import PerimeterPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.teePen import TeePen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.varLib.varStore import VarStoreInstancer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_sarasa_ui_propdigits_sc as b  # noqa: E402

START = time.time()
INTENTIONAL_CPS = set(range(0x30, 0x3A)) | {0x3A}
STATIC_REFERENCE_DASH_EXCEPTIONS = set(b.DASH_CMAP_CODEPOINTS)
EXACT_WEIGHTS = [str(stop["name"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS]
UPSTREAM_EXACT_WEIGHTS = [
    weight
    for weight in EXACT_WEIGHTS
    if str(b.STATIC_STYLE_SOURCES[weight]["sarasa"]) == weight
]
EXPECTED_WEIGHTS = {str(stop["name"]): int(stop["value"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS}
EXPECTED_AXIS = [{"tag": "wght", "min": 200.0, "default": 400.0, "max": 900.0}]
CL_BOUNDARY_ADVANCE_CODEPOINTS = [0x5DC5, 0x62FC, 0x7EFF]
CLASSICAL_OVERRIDE_CACHE: dict[tuple[str, str], set[int]] = {}
FONT_REVISION_TOLERANCE = 1 / 65536
CONTEXTUAL_SPACING_FEATURES = {"chws", "vchw"}
CONTEXTUAL_SPACING_VF_WEIGHTS = [200, 300, 400, 600, 700, 900]
VF_EM_DASH_PROBE_WEIGHTS = [
    ("ExtraLight", 200),
    ("Probe250", 250),
    ("Light", 300),
    ("Probe325", 325),
    ("InterpolationAnchor", 350),
    ("Probe375", 375),
    ("Regular", 400),
    ("Probe500", 500),
    ("SemiBold", 600),
    ("Probe650", 650),
    ("Bold", 700),
    ("Probe800", 800),
    ("Heavy", 900),
]
DEFAULT_RASTER_PPEMS = (9, 12, 16, 20, 24)
CJK_STROKE_WEIGHTS = [(str(stop["name"]), int(stop["value"])) for stop in b.SOURCE_HAN_WEIGHT_STOPS]
CJK_SHEAR_AREA_RELATIVE_TOLERANCE = 0.02
CJK_SHEAR_AREA_ABSOLUTE_TOLERANCE = 2000.0
CJK_SHEAR_POINT_TOLERANCE = 2.0
CJK_ITALIC_ANGLE_DEGREES = 9.4
CJK_MONOTONIC_AREA_RELATIVE_TOLERANCE = 0.012
CJK_MONOTONIC_AREA_ABSOLUTE_TOLERANCE = 750.0
CJK_WEIGHT_SPAN_RELATIVE_MINIMUM = 0.05
CJK_WEIGHT_SPAN_ABSOLUTE_MINIMUM = 1000.0
ASCII_ALPHANUMERIC = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
WEIGHT_HARMONY_WEIGHTS = tuple(range(200, 901, 25))
WEIGHT_HARMONY_NAMED_WEIGHTS = tuple(int(stop["value"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS)
WEIGHT_HARMONY_LATIN_TEXT = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
WEIGHT_HARMONY_STEM_LATIN_TEXT = "BEFHKLMNPRTUVXYZbdefhklmnprtu"
WEIGHT_HARMONY_CJK_SAMPLES = 128
WEIGHT_HARMONY_PPEM = 256


def log(message: str) -> None:
    print(f"[{time.time() - START:8.1f}s] {message}", flush=True)


def display_path(path: Path) -> str:
    return b.portable_report_path(path)


def static_path(region: str, weight: str, italic: bool, hinted: bool) -> Path:
    return b.static_dir(region, hinted) / b.static_output_name(region, weight, italic)


def static_reference_path(region: str, weight: str, italic: bool, hinted: bool) -> Path:
    style = b.static_reference_style_name(weight, italic)
    return b.region_reference_dir(region, hinted) / f"{b.sarasa_region_prefix(region)}-{style}.ttf"


def vf_path(region: str, italic: bool) -> Path:
    return b.VARIABLE_DIR / b.variable_output_name(region, italic)


def names(font: TTFont) -> list[str]:
    out: list[str] = []
    for record in font["name"].names:
        try:
            text = record.toUnicode()
        except Exception:
            continue
        if text not in out:
            out.append(text)
    return out


def name_values(font: TTFont, name_id: int) -> list[str]:
    values: list[str] = []
    for record in font["name"].names:
        if record.nameID != name_id:
            continue
        try:
            value = record.toUnicode()
        except Exception:
            continue
        if value not in values:
            values.append(value)
    return values


def english_name_values(font: TTFont, name_id: int) -> list[str]:
    values: list[str] = []
    for record in font["name"].names:
        if record.nameID != name_id or record.platformID != 3 or record.langID != 0x0409:
            continue
        try:
            value = record.toUnicode()
        except Exception:
            continue
        if value not in values:
            values.append(value)
    return values


def legal_name_status(font: TTFont) -> dict[str, Any]:
    copyright_values = name_values(font, 0)
    source_only = {
        str(name_id): name_values(font, name_id)
        for name_id in sorted(b.SOURCE_ONLY_LEGAL_NAME_IDS)
        if name_values(font, name_id)
    }
    return {
        "ok": copyright_values == [b.PROJECT_COPYRIGHT] and not source_only,
        "copyright": copyright_values,
        "source_only_name_ids": source_only,
    }


def vf_underline_metrics(font: TTFont, weight: int) -> dict[str, Any]:
    result = {
        "weight": weight,
        "thickness": int(font["post"].underlineThickness),
        "position": int(font["post"].underlinePosition),
        "record_tags": [],
    }
    if "MVAR" not in font:
        result["missing_mvar"] = True
        return result
    mvar = font["MVAR"].table
    records = {record.ValueTag: record for record in (mvar.ValueRecord or [])}
    result["record_tags"] = sorted(records)
    normalized = b.vf_mapped_normalized_weight(font, weight)
    location = {} if normalized == 0 else {"wght": normalized}
    instancer = VarStoreInstancer(mvar.VarStore, font["fvar"].axes, location)
    for value_tag, output_key, post_attribute in (
        ("unds", "thickness", "underlineThickness"),
        ("undo", "position", "underlinePosition"),
    ):
        record = records.get(value_tag)
        if record is not None:
            result[output_key] = int(getattr(font["post"], post_attribute) + otRound(instancer[record.VarIdx]))
    return result


def project_name_versions_match(version_strings: list[str]) -> bool:
    expected = f"Version {b.OPENTYPE_VERSION}; project {b.VERSION};"
    return bool(version_strings) and all(value.startswith(expected) for value in version_strings)


def axes(font: TTFont) -> list[dict[str, float | str]]:
    if "fvar" not in font:
        return []
    return [
        {"tag": axis.axisTag, "min": axis.minValue, "default": axis.defaultValue, "max": axis.maxValue}
        for axis in font["fvar"].axes
    ]


def has_glyph_program(font: TTFont) -> bool:
    return "maxp" in font and int(getattr(font["maxp"], "maxSizeOfInstructions", 0)) > 0


def glyph_signature(font: TTFont, glyph_name: str) -> dict[str, Any]:
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    try:
        glyph.expand(glyf)
    except Exception:
        pass
    bbox = tuple(getattr(glyph, attr, None) for attr in ("xMin", "yMin", "xMax", "yMax"))
    if getattr(glyph, "isComposite", lambda: False)():
        shape = ("composite", decomposed_outline_signature(font, glyph_name))
        kind = "composite"
    else:
        coords = getattr(glyph, "coordinates", None)
        coord_tuple = () if coords is None else tuple((int(x), int(y)) for x, y in coords)
        end_pts = tuple(getattr(glyph, "endPtsOfContours", []) or [])
        flags = tuple(int(flag) for flag in (getattr(glyph, "flags", []) or []))
        shape = ("simple", coord_tuple, end_pts, flags)
        kind = "simple"
    program = getattr(glyph, "program", None)
    try:
        instructions = bytes(program.getBytecode()) if program is not None else b""
    except Exception:
        instructions = b""
    return {"kind": kind, "bbox": bbox, "shape": shape, "instructions": instructions}


def decomposed_outline_signature(font: TTFont, glyph_name: str) -> tuple[Any, ...]:
    glyph_set = font.getGlyphSet()
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return tuple((operator, tuple(points)) for operator, points in pen.value)


def classical_override_codepoints(region: str, weight: str) -> set[int]:
    key = (region, weight)
    if key in CLASSICAL_OVERRIDE_CACHE:
        return CLASSICAL_OVERRIDE_CACHE[key]
    if not b.region_config(region)["classical"]:
        CLASSICAL_OVERRIDE_CACHE[key] = set()
        return CLASSICAL_OVERRIDE_CACHE[key]
    source_path = b.classical_static_override_path(region, weight)
    reference_path = static_reference_path(region, weight, False, False)
    if not source_path or not source_path.exists():
        raise FileNotFoundError(source_path)
    if not reference_path.exists():
        raise FileNotFoundError(reference_path)
    source = TTFont(source_path)
    reference = TTFont(reference_path)
    try:
        source_cmap = source.getBestCmap() or {}
        reference_cmap = reference.getBestCmap() or {}
        candidates = {
            codepoint
            for codepoint in set(source_cmap) & set(reference_cmap)
            if b.is_ideograph(codepoint)
        }
        CLASSICAL_OVERRIDE_CACHE[key] = {
            codepoint
            for codepoint in candidates
            if decomposed_outline_signature(source, source_cmap[codepoint])
            != decomposed_outline_signature(reference, reference_cmap[codepoint])
        }
    finally:
        source.close()
        reference.close()
    return CLASSICAL_OVERRIDE_CACHE[key]


def compare_fonts(
    target: TTFont,
    reference: TTFont,
    compare_glyphs: bool,
    skip_codepoints: set[int] | None = None,
    dedicated_feature_codepoints: set[int] | None = None,
) -> dict[str, Any]:
    dedicated_feature_exceptions = set(dedicated_feature_codepoints or ())
    product_exceptions = set(INTENTIONAL_CPS) | dedicated_feature_exceptions
    classical_exceptions = set(skip_codepoints or ())
    target_cmap = target.getBestCmap() or {}
    reference_cmap = reference.getBestCmap() or {}
    counts = {
        "missing_target": 0,
        "missing_reference": 0,
        "h_advance": 0,
        "h_lsb": 0,
        "v_advance": 0,
        "v_side_bearing": 0,
        "bbox": 0,
        "outline_or_flags": 0,
    }
    samples: dict[str, list[Any]] = {key: [] for key in counts}
    observations = {
        "glyph_id_differences": 0,
        "instruction_bytecode_differences": 0,
    }
    observation_samples: dict[str, list[Any]] = {key: [] for key in observations}
    target_hmtx = target["hmtx"].metrics if "hmtx" in target else {}
    reference_hmtx = reference["hmtx"].metrics if "hmtx" in reference else {}
    target_vmtx = target["vmtx"].metrics if "vmtx" in target else {}
    reference_vmtx = reference["vmtx"].metrics if "vmtx" in reference else {}
    all_codepoints = set(target_cmap) | set(reference_cmap)
    metric_codepoints = all_codepoints - product_exceptions
    outline_codepoints = metric_codepoints - classical_exceptions
    instructions_compared = 0
    for cp in sorted(metric_codepoints):
        target_glyph = target_cmap.get(cp)
        reference_glyph = reference_cmap.get(cp)
        if target_glyph is None:
            counts["missing_target"] += 1
            if len(samples["missing_target"]) < 5:
                samples["missing_target"].append(f"U+{cp:04X}")
            continue
        if reference_glyph is None:
            counts["missing_reference"] += 1
            if len(samples["missing_reference"]) < 5:
                samples["missing_reference"].append(f"U+{cp:04X}")
            continue
        target_gid = target.getGlyphID(target_glyph)
        reference_gid = reference.getGlyphID(reference_glyph)
        if target_gid != reference_gid:
            observations["glyph_id_differences"] += 1
            if len(observation_samples["glyph_id_differences"]) < 5:
                observation_samples["glyph_id_differences"].append(
                    [f"U+{cp:04X}", target_gid, reference_gid]
                )
        target_h = target_hmtx.get(target_glyph)
        reference_h = reference_hmtx.get(reference_glyph)
        if target_h is not None and reference_h is not None:
            if target_h[0] != reference_h[0]:
                counts["h_advance"] += 1
                if len(samples["h_advance"]) < 5:
                    samples["h_advance"].append([f"U+{cp:04X}", target_glyph, target_h[0], reference_h[0]])
            if target_h[1] != reference_h[1]:
                counts["h_lsb"] += 1
                if len(samples["h_lsb"]) < 5:
                    samples["h_lsb"].append([f"U+{cp:04X}", target_glyph, target_h[1], reference_h[1]])
        target_v = target_vmtx.get(target_glyph)
        reference_v = reference_vmtx.get(reference_glyph)
        if target_v is not None and reference_v is not None:
            if target_v[0] != reference_v[0]:
                counts["v_advance"] += 1
                if len(samples["v_advance"]) < 5:
                    samples["v_advance"].append([f"U+{cp:04X}", target_glyph, target_v[0], reference_v[0]])
            if target_v[1] != reference_v[1]:
                counts["v_side_bearing"] += 1
                if len(samples["v_side_bearing"]) < 5:
                    samples["v_side_bearing"].append([f"U+{cp:04X}", target_glyph, target_v[1], reference_v[1]])
        if compare_glyphs and cp in outline_codepoints:
            instructions_compared += 1
            target_sig = glyph_signature(target, target_glyph)
            reference_sig = glyph_signature(reference, reference_glyph)
            if target_sig["bbox"] != reference_sig["bbox"]:
                counts["bbox"] += 1
                if len(samples["bbox"]) < 5:
                    samples["bbox"].append([f"U+{cp:04X}", target_glyph, target_sig["bbox"], reference_sig["bbox"]])
            if target_sig["shape"] != reference_sig["shape"]:
                counts["outline_or_flags"] += 1
                if len(samples["outline_or_flags"]) < 5:
                    samples["outline_or_flags"].append(
                        [f"U+{cp:04X}", target_glyph, target_sig["kind"], reference_sig["kind"]]
                    )
            if target_sig["instructions"] != reference_sig["instructions"]:
                observations["instruction_bytecode_differences"] += 1
                if len(observation_samples["instruction_bytecode_differences"]) < 5:
                    observation_samples["instruction_bytecode_differences"].append(
                        [
                            f"U+{cp:04X}",
                            target_glyph,
                            hashlib.sha256(target_sig["instructions"]).hexdigest()[:16],
                            hashlib.sha256(reference_sig["instructions"]).hexdigest()[:16],
                        ]
                    )
    return {
        "counts": counts,
        "coverage": {
            "codepoints_total": len(all_codepoints),
            "codepoints_metrics_compared": len(metric_codepoints),
            "codepoints_outlines_compared": len(outline_codepoints) if compare_glyphs else 0,
            "product_exceptions": len(all_codepoints & product_exceptions),
            "dedicated_feature_exceptions": len(
                all_codepoints & dedicated_feature_exceptions
            ),
            "classical_outline_exceptions": len(all_codepoints & classical_exceptions),
            "instructions_compared": instructions_compared,
        },
        "observations": observations,
        "samples": {key: value for key, value in samples.items() if value},
        "observation_samples": {
            key: value for key, value in observation_samples.items() if value
        },
    }


def freetype_render_glyph_signature(
    face: freetype.Face,
    glyph_id: int,
    load_flags: int = freetype.FT_LOAD_DEFAULT,
) -> tuple[Any, ...]:
    face.load_glyph(glyph_id, load_flags)
    face.glyph.render(freetype.FT_RENDER_MODE_NORMAL)
    slot = face.glyph
    bitmap = slot.bitmap
    return (
        int(slot.bitmap_left),
        int(slot.bitmap_top),
        int(slot.advance.x),
        int(slot.advance.y),
        int(bitmap.width),
        int(bitmap.rows),
        int(bitmap.pixel_mode),
        bytes(bitmap.buffer),
    )


def freetype_render_signature(
    face: freetype.Face,
    codepoint: int,
    load_flags: int = freetype.FT_LOAD_DEFAULT,
) -> tuple[Any, ...]:
    return freetype_render_glyph_signature(
        face,
        face.get_char_index(codepoint),
        load_flags,
    )


def compact_render_signature(signature: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "left": signature[0],
        "top": signature[1],
        "advance": [signature[2], signature[3]],
        "size": [signature[4], signature[5]],
        "pixel_mode": signature[6],
        "bitmap_sha256": hashlib.sha256(signature[7]).hexdigest()[:16],
    }


def freetype_rendered_pixels(
    face: freetype.Face,
    glyph_id: int,
    origin_x: int,
    origin_y: int,
    load_flags: int = freetype.FT_LOAD_DEFAULT,
) -> tuple[set[tuple[int, int]], dict[str, int]]:
    face.load_glyph(glyph_id, load_flags)
    face.glyph.render(freetype.FT_RENDER_MODE_NORMAL)
    slot = face.glyph
    bitmap = slot.bitmap
    width = int(bitmap.width)
    rows = int(bitmap.rows)
    pitch = int(bitmap.pitch)
    stride = abs(pitch)
    data = bytes(bitmap.buffer)
    pixels: set[tuple[int, int]] = set()
    for row in range(rows):
        stored_row = row if pitch >= 0 else rows - 1 - row
        row_data = data[stored_row * stride : (stored_row + 1) * stride]
        for column in range(width):
            if bitmap.pixel_mode == freetype.FT_PIXEL_MODE_MONO:
                occupied = bool(row_data[column // 8] & (0x80 >> (column % 8)))
            else:
                occupied = bool(row_data[column])
            if occupied:
                pixels.add(
                    (
                        origin_x + int(slot.bitmap_left) + column,
                        origin_y + int(slot.bitmap_top) - 1 - row,
                    )
                )
    return pixels, {
        "width": width,
        "rows": rows,
        "left": int(slot.bitmap_left),
        "top": int(slot.bitmap_top),
    }


def pixel_component_count(
    pixels: set[tuple[int, int]],
    *,
    include_diagonals: bool = False,
) -> int:
    remaining = set(pixels)
    count = 0
    neighbors = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    if include_diagonals:
        neighbors.extend([(1, 1), (1, -1), (-1, 1), (-1, -1)])
    while remaining:
        count += 1
        stack = [remaining.pop()]
        while stack:
            x, y = stack.pop()
            for dx, dy in neighbors:
                neighbor = (x + dx, y + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
    return count


def raster_compare_worker(task: dict[str, Any]) -> dict[str, Any]:
    target_path = Path(task["target_path"])
    reference_path = Path(task["reference_path"])
    item: dict[str, Any] = {
        "region": task["region"],
        "weight": task["weight"],
        "italic": task["italic"],
        "hinted": True,
        "target": task["target"],
        "reference": task["reference"],
        "comparison_mode": task["comparison_mode"],
        "ppems": list(task["ppems"]),
        "counts": {
            "raster_mismatch": 0,
            "render_error": 0,
            "propdigits_mapping_error": 0,
            "propdigits_raster_mismatch": 0,
            "propdigits_render_error": 0,
            "classical_render_error": 0,
            "classical_blank_bitmap": 0,
        },
        "mismatches_by_ppem": {},
        "samples": {},
    }
    if not target_path.exists() or not reference_path.exists():
        item["missing"] = True
        return item

    target_font = TTFont(target_path, lazy=True)
    reference_font = TTFont(reference_path, lazy=True)
    propdigits_pairs: list[tuple[int, str, int, int]] = []
    propdigits_mapping_errors: list[list[str]] = []
    try:
        target_cmap = target_font.getBestCmap() or {}
        reference_cmap = reference_font.getBestCmap() or {}
        target_tnum = b.get_single_substitution_mapping(target_font, "tnum")
        reference_pnum = b.get_single_substitution_mapping(reference_font, "pnum")
        reference_tnum = b.get_single_substitution_mapping(reference_font, "tnum")
        project_reference = (
            task["comparison_mode"] == "project-hinted-vs-unhinted-no-hinting"
        )
        for codepoint in range(0x30, 0x3A):
            target_default = target_cmap.get(codepoint)
            target_tabular = target_tnum.get(target_default) if target_default else None
            reference_default = reference_cmap.get(codepoint)
            reference_proportional = (
                reference_default
                if project_reference
                else reference_pnum.get(reference_default)
                if reference_default
                else None
            )
            reference_tabular = (
                reference_tnum.get(reference_default)
                if project_reference
                else reference_tnum.get(reference_default, reference_default)
                if reference_default
                else None
            )
            for variant, target_glyph, reference_glyph in (
                ("pnum", target_default, reference_proportional),
                ("tnum", target_tabular, reference_tabular),
            ):
                if target_glyph is None or reference_glyph is None:
                    propdigits_mapping_errors.append(
                        [f"U+{codepoint:04X}", variant, str(target_glyph), str(reference_glyph)]
                    )
                    continue
                propdigits_pairs.append(
                    (
                        codepoint,
                        variant,
                        target_font.getGlyphID(target_glyph),
                        reference_font.getGlyphID(reference_glyph),
                    )
                )
    finally:
        target_font.close()
        reference_font.close()
    item["counts"]["propdigits_mapping_error"] = len(propdigits_mapping_errors)
    common_codepoints = set(target_cmap) & set(reference_cmap)
    product_exceptions = set(task["product_exception_codepoints"])
    classical_exceptions = set(task["classical_exception_codepoints"])
    codepoints = sorted(common_codepoints - product_exceptions - classical_exceptions)
    classical_codepoints = sorted(common_codepoints & classical_exceptions)
    item["coverage"] = {
        "codepoints_total": len(common_codepoints),
        "codepoints_compared": len(codepoints),
        "product_exceptions": len(common_codepoints & product_exceptions),
        "classical_raster_exceptions": len(common_codepoints & classical_exceptions),
        "classical_raster_smoke_codepoints": len(classical_codepoints),
        "propdigits_raster_pairs": len(propdigits_pairs),
    }
    target_face = freetype.Face(str(target_path))
    reference_face = freetype.Face(str(reference_path))
    load_flags = int(task.get("load_flags", freetype.FT_LOAD_DEFAULT))
    mismatch_samples: list[Any] = []
    error_samples: list[Any] = []
    classical_error_samples: list[Any] = []
    classical_blank_samples: list[Any] = []
    propdigits_mismatch_samples: list[Any] = []
    propdigits_error_samples: list[Any] = []
    for ppem in task["ppems"]:
        target_face.set_pixel_sizes(0, ppem)
        reference_face.set_pixel_sizes(0, ppem)
        ppem_mismatches = 0
        for codepoint in codepoints:
            try:
                target_signature = freetype_render_signature(target_face, codepoint, load_flags)
                reference_signature = freetype_render_signature(reference_face, codepoint, load_flags)
            except Exception as exc:
                item["counts"]["render_error"] += 1
                if len(error_samples) < 8:
                    error_samples.append([f"U+{codepoint:04X}", ppem, type(exc).__name__, str(exc)])
                continue
            if target_signature != reference_signature:
                ppem_mismatches += 1
                item["counts"]["raster_mismatch"] += 1
                if len(mismatch_samples) < 12:
                    mismatch_samples.append(
                        {
                            "codepoint": f"U+{codepoint:04X}",
                            "ppem": ppem,
                            "target": compact_render_signature(target_signature),
                            "reference": compact_render_signature(reference_signature),
                        }
                    )
        item["mismatches_by_ppem"][str(ppem)] = ppem_mismatches
        for codepoint, variant, target_gid, reference_gid in propdigits_pairs:
            try:
                target_signature = freetype_render_glyph_signature(target_face, target_gid, load_flags)
                reference_signature = freetype_render_glyph_signature(reference_face, reference_gid, load_flags)
            except Exception as exc:
                item["counts"]["propdigits_render_error"] += 1
                if len(propdigits_error_samples) < 8:
                    propdigits_error_samples.append(
                        [f"U+{codepoint:04X}", variant, ppem, type(exc).__name__, str(exc)]
                    )
                continue
            if target_signature != reference_signature:
                item["counts"]["propdigits_raster_mismatch"] += 1
                if len(propdigits_mismatch_samples) < 12:
                    propdigits_mismatch_samples.append(
                        {
                            "codepoint": f"U+{codepoint:04X}",
                            "variant": variant,
                            "ppem": ppem,
                            "target": compact_render_signature(target_signature),
                            "reference": compact_render_signature(reference_signature),
                        }
                    )
        for codepoint in classical_codepoints:
            try:
                target_signature = freetype_render_signature(target_face, codepoint)
            except Exception as exc:
                item["counts"]["classical_render_error"] += 1
                if len(classical_error_samples) < 8:
                    classical_error_samples.append(
                        [f"U+{codepoint:04X}", ppem, type(exc).__name__, str(exc)]
                    )
                continue
            if target_signature[4] <= 0 or target_signature[5] <= 0 or not target_signature[7]:
                item["counts"]["classical_blank_bitmap"] += 1
                if len(classical_blank_samples) < 8:
                    classical_blank_samples.append(
                        [f"U+{codepoint:04X}", ppem, compact_render_signature(target_signature)]
                    )
    item["glyphs_per_ppem"] = len(codepoints)
    item["classical_smoke_glyphs_per_ppem"] = len(classical_codepoints)
    item["renders_checked"] = (
        len(codepoints) + len(classical_codepoints) + len(propdigits_pairs)
    ) * len(task["ppems"])
    if propdigits_mapping_errors:
        item["samples"]["propdigits_mapping_error"] = propdigits_mapping_errors[:8]
    if propdigits_mismatch_samples:
        item["samples"]["propdigits_raster_mismatch"] = propdigits_mismatch_samples
    if propdigits_error_samples:
        item["samples"]["propdigits_render_error"] = propdigits_error_samples
    if mismatch_samples:
        item["samples"]["raster_mismatch"] = mismatch_samples
    if error_samples:
        item["samples"]["render_error"] = error_samples
    if classical_error_samples:
        item["samples"]["classical_render_error"] = classical_error_samples
    if classical_blank_samples:
        item["samples"]["classical_blank_bitmap"] = classical_blank_samples
    return item


def audit_static_raster(
    ppems: tuple[int, ...],
    jobs: int,
    regions: list[str] | None = None,
    weights: list[str] | None = None,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    selected_regions = regions or b.REGION_ORDER
    selected_weights = weights or EXACT_WEIGHTS
    for region in selected_regions:
        for weight in selected_weights:
            extension_weight = weight not in UPSTREAM_EXACT_WEIGHTS
            classical_exceptions = (
                set() if extension_weight else classical_override_codepoints(region, weight)
            )
            for italic in (False, True):
                target_path = static_path(region, weight, italic, True)
                reference_path = (
                    static_path(region, weight, italic, False)
                    if extension_weight
                    else static_reference_path(region, weight, italic, True)
                )
                tasks.append(
                    {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "target_path": str(target_path),
                        "reference_path": str(reference_path),
                        "target": display_path(target_path),
                        "reference": display_path(reference_path),
                        "comparison_mode": (
                            "project-hinted-vs-unhinted-no-hinting"
                            if extension_weight
                            else "official-hinted-exact"
                        ),
                        "load_flags": (
                            freetype.FT_LOAD_NO_HINTING
                            if extension_weight
                            else freetype.FT_LOAD_DEFAULT
                        ),
                        "ppems": ppems,
                        "product_exception_codepoints": (
                            INTENTIONAL_CPS
                            if extension_weight
                            else INTENTIONAL_CPS | STATIC_REFERENCE_DASH_EXCEPTIONS
                        ),
                        "classical_exception_codepoints": classical_exceptions,
                    }
                )
    worker_count = max(1, min(jobs, len(tasks)))
    log(
        f"FreeType raster audit start: {len(tasks)} cases, "
        f"ppem={','.join(map(str, ppems))}, jobs={worker_count}"
    )
    if worker_count == 1:
        results = [raster_compare_worker(task) for task in tasks]
    else:
        results = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as executor:
            future_to_task = {executor.submit(raster_compare_worker, task): task for task in tasks}
            for done, future in enumerate(concurrent.futures.as_completed(future_to_task), 1):
                item = future.result()
                results.append(item)
                log(
                    f"FreeType raster {done}/{len(tasks)}: {item['region']} {item['weight']}"
                    f"{' Italic' if item['italic'] else ''}, mismatches="
                    f"{item.get('counts', {}).get('raster_mismatch', 0)}"
                )
    order = {region: index for index, region in enumerate(selected_regions)}
    weight_order = {weight: index for index, weight in enumerate(selected_weights)}
    return sorted(
        results,
        key=lambda item: (order[item["region"]], weight_order[item["weight"]], item["italic"]),
    )


def colon_status(path: Path) -> dict[str, Any]:
    strings = ["09:41", "1:2", "1:a", "a:2", "a:b"]
    shaped = {text: b.shape_glyph_names(path, text, "Latn") for text in strings}
    raw_colon = shaped["a:b"][1] if shaped.get("a:b") and len(shaped["a:b"]) == 3 else None
    raised_0941 = shaped["09:41"][2] if shaped.get("09:41") and len(shaped["09:41"]) == 5 else None
    raised_12 = shaped["1:2"][1] if shaped.get("1:2") and len(shaped["1:2"]) == 3 else None
    non_digit_ok = all(shaped.get(text) and len(shaped[text]) == 3 and shaped[text][1] == raw_colon for text in ["1:a", "a:2", "a:b"])
    return {
        "ok": raw_colon is not None
        and raised_0941 is not None
        and raised_12 is not None
        and raised_0941 != raw_colon
        and raised_12 != raw_colon
        and non_digit_ok,
        "raw_colon": raw_colon,
        "raised_0941": raised_0941,
        "raised_1_2": raised_12,
        "shapes": shaped,
    }


def digit_width_status(
    path: Path,
    variations: dict[str, float] | None = None,
) -> dict[str, Any]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        glyph_order = font.getGlyphOrder()
        feature_tags = layout_feature_tags(font, "GSUB")
    finally:
        font.close()

    def shape(features: dict[str, bool]) -> list[list[Any]]:
        return shape_signature_data(
            data,
            glyph_order,
            "0123456789",
            "latn",
            features,
            variations=variations,
        )

    default = shape({"kern": False, "pnum": False, "tnum": False})
    proportional = shape({"kern": False, "pnum": True, "tnum": False})
    tabular = shape({"kern": False, "pnum": False, "tnum": True})
    default_advances = [item[1] for item in default]
    proportional_advances = [item[1] for item in proportional]
    tabular_advances = [item[1] for item in tabular]
    default_is_proportional = len(set(default_advances)) > 1
    tabular_is_uniform = len(tabular_advances) == 10 and len(set(tabular_advances)) == 1
    default_equals_pnum = default == proportional
    tnum_changes_glyphs = [item[0] for item in tabular] != [item[0] for item in proportional]
    ok = (
        {"pnum", "tnum"} <= feature_tags
        and len(default) == len(proportional) == len(tabular) == 10
        and default_is_proportional
        and tabular_is_uniform
        and default_equals_pnum
        and tnum_changes_glyphs
    )
    return {
        "ok": ok,
        "variations": variations or {},
        "features": sorted(feature_tags & {"pnum", "tnum"}),
        "default_is_proportional": default_is_proportional,
        "tabular_is_uniform": tabular_is_uniform,
        "default_equals_pnum": default_equals_pnum,
        "tnum_changes_glyphs": tnum_changes_glyphs,
        "default_advances": default_advances,
        "pnum_advances": proportional_advances,
        "tnum_advances": tabular_advances,
    }


def digit_width_parity_status(
    variable_status: dict[str, Any],
    static_status: dict[str, Any],
) -> dict[str, Any]:
    fields = ("default_advances", "pnum_advances", "tnum_advances")
    mismatches = {
        field: {
            "variable": variable_status[field],
            "static": static_status[field],
        }
        for field in fields
        if variable_status[field] != static_status[field]
    }
    return {"ok": not mismatches, "mismatches": mismatches}


def shape_signature_data(
    data: bytes,
    glyph_order: list[str],
    text: str,
    script: str,
    features: dict[str, bool],
    direction: str | None = None,
    variations: dict[str, float] | None = None,
    language: str = "ZHS",
) -> list[list[Any]]:
    face = hb.Face(data)
    hb_font = hb.Font(face)
    hb_font.scale = (face.upem, face.upem)
    if variations:
        hb_font.set_variations(variations)
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    buffer.script = script
    buffer.language = language
    if direction:
        buffer.direction = direction
    hb.shape(hb_font, buffer, features)
    return [
        [
            glyph_order[info.codepoint],
            position.x_advance,
            position.y_advance,
            position.x_offset,
            position.y_offset,
        ]
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions)
    ]


def glyph_identity_pattern(signature: list[list[Any]]) -> list[int]:
    classes: dict[str, int] = {}
    pattern = []
    for glyph_name, *_position in signature:
        classes.setdefault(glyph_name, len(classes))
        pattern.append(classes[glyph_name])
    return pattern


def shaped_visual_bounds(
    signature: list[list[Any]],
    glyph_set: Any,
) -> list[tuple[float, float, float, float] | None]:
    cursor_x = 0
    cursor_y = 0
    bounds = []
    for glyph_name, x_advance, y_advance, x_offset, y_offset in signature:
        pen = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(pen)
        if pen.bounds is None:
            bounds.append(None)
        else:
            x_min, y_min, x_max, y_max = pen.bounds
            bounds.append(
                (
                    x_min + cursor_x + x_offset,
                    y_min + cursor_y + y_offset,
                    x_max + cursor_x + x_offset,
                    y_max + cursor_y + y_offset,
                )
            )
        cursor_x += x_advance
        cursor_y += y_advance
    return bounds


def combined_visual_bounds(
    bounds: list[tuple[float, float, float, float] | None],
) -> tuple[int, int, int, int] | None:
    present = [bound for bound in bounds if bound is not None]
    if not present:
        return None
    return (
        otRound(min(bound[0] for bound in present)),
        otRound(min(bound[1] for bound in present)),
        otRound(max(bound[2] for bound in present)),
        otRound(max(bound[3] for bound in present)),
    )


def render_shaped_pixels(
    face: freetype.Face,
    font: TTFont,
    signature: list[list[Any]],
    ppem: int,
) -> set[tuple[int, int]]:
    upem = int(font["head"].unitsPerEm)
    face.set_pixel_sizes(0, ppem)
    cursor_x = 0
    cursor_y = 0
    combined: set[tuple[int, int]] = set()
    for glyph_name, x_advance, y_advance, x_offset, y_offset in signature:
        origin_x = otRound((cursor_x + x_offset) * ppem / upem)
        origin_y = otRound((cursor_y + y_offset) * ppem / upem)
        pixels, _metrics = freetype_rendered_pixels(
            face,
            font.getGlyphID(glyph_name),
            origin_x,
            origin_y,
            freetype.FT_LOAD_NO_HINTING | freetype.FT_LOAD_NO_AUTOHINT,
        )
        combined.update(pixels)
        cursor_x += x_advance
        cursor_y += y_advance
    return combined


EM_DASH_LEGACY_GLYPH_CACHE: dict[tuple[str, int, int], list[str]] = {}


def variable_em_dash_shaping_status(
    data: bytes,
    font: TTFont,
    variations: dict[str, float] | None,
) -> dict[str, Any]:
    glyph_order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet(location=variations)
    upem = int(font["head"].unitsPerEm)
    single_plain = shape_signature_data(
        data,
        glyph_order,
        "—",
        "hani",
        {"ccmp": False, "calt": False, "vert": False, "vrt2": False},
        direction="ltr",
        variations=variations,
    )
    single_default = shape_signature_data(
        data,
        glyph_order,
        "—",
        "hani",
        {},
        direction="ltr",
        variations=variations,
    )
    cases: dict[str, Any] = {}
    definitions = [
        ("default", "ltr", {}, "single"),
        (
            "ccmp",
            "ltr",
            {"ccmp": True, "calt": False, "vert": False, "vrt2": False},
            "single",
        ),
        ("vertical-default", "ttb", {}, "single"),
        (
            "vert",
            "ttb",
            {"ccmp": True, "calt": False, "vert": True, "vrt2": False},
            "single",
        ),
        (
            "vrt2-only",
            "ttb",
            {"ccmp": True, "calt": False, "vert": False, "vrt2": True},
            "same-pair",
        ),
        (
            "vert-plus-vrt2",
            "ttb",
            {"ccmp": True, "calt": False, "vert": True, "vrt2": True},
            "single",
        ),
    ]
    for name, direction, features, glyph_pattern in definitions:
        signature = shape_signature_data(
            data,
            glyph_order,
            "——",
            "hani",
            features,
            direction=direction,
            variations=variations,
        )
        identity_pattern = glyph_identity_pattern(signature)
        expected_pattern = [0] if glyph_pattern == "single" else [0, 0]
        total_x_advance = sum(row[1] for row in signature)
        total_y_advance = sum(row[2] for row in signature)
        expected_advance = (
            (2 * upem, 0) if direction == "ltr" else (0, -2 * upem)
        )
        visual_bounds = shaped_visual_bounds(signature, glyph_set)
        cases[name] = {
            "ok": (
                identity_pattern == expected_pattern
                and (total_x_advance, total_y_advance) == expected_advance
                and all(bound is not None for bound in visual_bounds)
            ),
            "glyph_identity_pattern": identity_pattern,
            "expected_glyph_identity_pattern": expected_pattern,
            "total_x_advance": total_x_advance,
            "total_y_advance": total_y_advance,
            "expected_advance": list(expected_advance),
            "signature": signature,
            "visual_bounds": visual_bounds,
        }
    long_dash_cases: dict[str, Any] = {}
    long_dash_definitions = [
        ("triple-default", "———", "ltr", {}, 3, "single"),
        (
            "triple-ccmp",
            "———",
            "ltr",
            {"ccmp": True, "calt": False, "vert": False, "vrt2": False},
            3,
            "single",
        ),
        ("triple-vertical-default", "———", "ttb", {}, 3, "single"),
        (
            "triple-vert",
            "———",
            "ttb",
            {"ccmp": True, "calt": False, "vert": True, "vrt2": False},
            3,
            "single",
        ),
        (
            "triple-vrt2-only",
            "———",
            "ttb",
            {"ccmp": True, "calt": False, "vert": False, "vrt2": True},
            3,
            "same-triple",
        ),
        (
            "triple-vert-plus-vrt2",
            "———",
            "ttb",
            {"ccmp": True, "calt": False, "vert": True, "vrt2": True},
            3,
            "single",
        ),
    ]
    for encoded_name, text, multiplier in (
        ("encoded-two-em", "⸺", 2),
        ("encoded-three-em", "⸻", 3),
    ):
        long_dash_definitions.extend(
            [
                (f"{encoded_name}-default", text, "ltr", {}, multiplier, "single"),
                (f"{encoded_name}-vertical-default", text, "ttb", {}, multiplier, "single"),
                (
                    f"{encoded_name}-vert",
                    text,
                    "ttb",
                    {"ccmp": True, "calt": False, "vert": True, "vrt2": False},
                    multiplier,
                    "single",
                ),
                (
                    f"{encoded_name}-vrt2-only",
                    text,
                    "ttb",
                    {"ccmp": True, "calt": False, "vert": False, "vrt2": True},
                    multiplier,
                    "single",
                ),
            ]
        )
    for name, text, direction, features, multiplier, glyph_pattern in long_dash_definitions:
        signature = shape_signature_data(
            data,
            glyph_order,
            text,
            "hani",
            features,
            direction=direction,
            variations=variations,
        )
        identity_pattern = glyph_identity_pattern(signature)
        expected_pattern = (
            [0]
            if glyph_pattern == "single"
            else [0, 0, 0]
        )
        expected_advance = (
            (multiplier * upem, 0)
            if direction == "ltr"
            else (0, -multiplier * upem)
        )
        advance = (
            sum(row[1] for row in signature),
            sum(row[2] for row in signature),
        )
        long_dash_cases[name] = {
            "ok": identity_pattern == expected_pattern and advance == expected_advance,
            "glyph_identity_pattern": identity_pattern,
            "expected_glyph_identity_pattern": expected_pattern,
            "advance": list(advance),
            "expected_advance": list(expected_advance),
            "signature": signature,
        }
    def first_glyph(case: dict[str, Any]) -> str | None:
        signature = case.get("signature", [])
        return signature[0][0] if signature else None

    equivalence = {
        "pair_matches_encoded_two_em": (
            first_glyph(cases["default"]) is not None
            and first_glyph(cases["default"])
            == first_glyph(long_dash_cases["encoded-two-em-default"])
        ),
        "vertical_pair_matches_encoded_two_em": (
            first_glyph(cases["vertical-default"]) is not None
            and first_glyph(cases["vertical-default"])
            == first_glyph(long_dash_cases["encoded-two-em-vertical-default"])
        ),
        "triple_matches_encoded_three_em": (
            first_glyph(long_dash_cases["triple-default"]) is not None
            and first_glyph(long_dash_cases["triple-default"])
            == first_glyph(long_dash_cases["encoded-three-em-default"])
        ),
        "vertical_triple_matches_encoded_three_em": (
            first_glyph(long_dash_cases["triple-vertical-default"]) is not None
            and first_glyph(long_dash_cases["triple-vertical-default"])
            == first_glyph(long_dash_cases["encoded-three-em-vertical-default"])
        ),
    }
    structure = b.variable_em_dash_ligature_structure_status(font)
    return {
        "ok": (
            single_plain == single_default
            and structure["ok"]
            and all(case["ok"] for case in cases.values())
            and all(case["ok"] for case in long_dash_cases.values())
            and all(equivalence.values())
        ),
        "mechanism": "Source Han ccmp plus vert",
        "single_unchanged": single_plain == single_default,
        "single_signature": single_default,
        "structure": structure,
        "cases": cases,
        "long_dash_cases": long_dash_cases,
        "long_dash_equivalence": equivalence,
    }


def em_dash_shaping_status(
    path: Path,
    variations: dict[str, float] | None = None,
) -> dict[str, Any]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        if "fvar" in font:
            return variable_em_dash_shaping_status(data, font, variations)
        glyph_order = font.getGlyphOrder()
        glyph_set = font.getGlyphSet(location=variations)
        upem = int(font["head"].unitsPerEm)
        cmap = font.getBestCmap() or {}
        em_dash = cmap.get(0x2014)
        vert_mapping = b.get_single_substitution_mapping(font, "vert")
        vrt2_mapping = b.get_single_substitution_mapping(font, "vrt2")
        em_dash_v = (
            vert_mapping.get(em_dash) or vrt2_mapping.get(em_dash)
            if em_dash
            else None
        )
        stat = path.stat()
        legacy_cache_key = (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
        if legacy_cache_key not in EM_DASH_LEGACY_GLYPH_CACHE:
            EM_DASH_LEGACY_GLYPH_CACHE[legacy_cache_key] = (
                b.legacy_vertical_em_dash_continuation_glyphs(font, em_dash_v)
                if em_dash_v
                else []
            )
        legacy_vertical_continuations = EM_DASH_LEGACY_GLYPH_CACHE[legacy_cache_key]
        single_off = shape_signature_data(
            data,
            glyph_order,
            "—",
            "hani",
            {"calt": False, "vert": False, "vrt2": False},
            direction="ltr",
            variations=variations,
        )
        single_on = shape_signature_data(
            data,
            glyph_order,
            "—",
            "hani",
            {"calt": True, "vert": False, "vrt2": False},
            direction="ltr",
            variations=variations,
        )
        cases = {}
        for name, direction, features in [
            ("default", "ltr", {}),
            ("calt", "ltr", {"calt": True, "vert": False, "vrt2": False}),
            ("vert", "ttb", {"calt": True, "vert": True, "vrt2": False}),
            ("vrt2", "ttb", {"calt": True, "vert": False, "vrt2": True}),
        ]:
            signature = shape_signature_data(
                data,
                glyph_order,
                "——",
                "hani",
                features,
                direction=direction,
                variations=variations,
            )
            visual_bounds = shaped_visual_bounds(signature, glyph_set)
            identity_pattern = glyph_identity_pattern(signature)
            first_outline_matches_single = True
            if direction == "ltr" and len(signature) == 2 and len(single_on) == 1:
                pair_pen = DecomposingRecordingPen(glyph_set)
                single_pen = DecomposingRecordingPen(glyph_set)
                glyph_set[signature[0][0]].draw(pair_pen)
                glyph_set[single_on[0][0]].draw(single_pen)
                first_outline_matches_single = pair_pen.value == single_pen.value
            total_x_advance = sum(row[1] for row in signature)
            total_y_advance = sum(row[2] for row in signature)
            expected_identity_pattern = [0, 1] if direction == "ltr" else [0, 0]
            vertical_placement_delta = (
                signature[1][4] - signature[0][4]
                if direction == "ttb" and len(signature) == 2
                else None
            )
            horizontal_placement_delta = (
                signature[1][3] - signature[0][3]
                if direction == "ttb" and len(signature) == 2
                else None
            )
            expected_vertical_placement = None
            if direction == "ttb":
                records = b.vertical_em_dash_positioning_records(font, variations)[name]
                if records:
                    expected_vertical_placement = (
                        records[0]["x_placement"],
                        records[0]["y_placement"],
                    )
            seam_gap: float | None = None
            if len(visual_bounds) == 2 and all(bound is not None for bound in visual_bounds):
                first = visual_bounds[0]
                second = visual_bounds[1]
                if direction == "ltr":
                    seam_gap = float(second[0] - first[2])
                else:
                    seam_gap = float(first[1] - second[3])
            expected_advance = (
                total_x_advance == 2 * upem and total_y_advance == 0
                if direction == "ltr"
                else total_x_advance == 0 and total_y_advance == -2 * upem
            )
            cases[name] = {
                "ok": (
                    len(signature) == 2
                    and identity_pattern == expected_identity_pattern
                    and first_outline_matches_single
                    and expected_advance
                    and (
                        vertical_placement_delta is not None
                        and vertical_placement_delta > 0
                        and (
                            horizontal_placement_delta,
                            vertical_placement_delta,
                        )
                        == expected_vertical_placement
                        if direction == "ttb"
                        else True
                    )
                    and seam_gap is not None
                    and seam_gap <= 0
                ),
                "glyph_identity_pattern": identity_pattern,
                "expected_glyph_identity_pattern": expected_identity_pattern,
                "first_outline_matches_single": first_outline_matches_single,
                "total_x_advance": total_x_advance,
                "total_y_advance": total_y_advance,
                "vertical_placement_delta": vertical_placement_delta,
                "horizontal_placement_delta": horizontal_placement_delta,
                "expected_vertical_placement": expected_vertical_placement,
                "seam_gap": seam_gap,
                "signature": signature,
                "visual_bounds": visual_bounds,
            }
        return {
            "ok": (
                single_off == single_on
                and all(case["ok"] for case in cases.values())
                and not legacy_vertical_continuations
            ),
            "single_unchanged": single_off == single_on,
            "single_signature": single_on,
            "legacy_vertical_continuations": legacy_vertical_continuations,
            "cases": cases,
        }
    finally:
        font.close()


def upstream_dash_shaping_status(
    path: Path,
    region: str,
    variations: dict[str, float] | None = None,
) -> dict[str, Any]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        glyph_order = font.getGlyphOrder()
        upem = int(font["head"].unitsPerEm)
        structure = b.upstream_dash_structure_status(font, region)

        def shaped(
            text: str,
            language: str,
            direction: str = "ltr",
            features: dict[str, bool] | None = None,
        ) -> list[list[Any]]:
            return shape_signature_data(
                data,
                glyph_order,
                text,
                "Hani",
                features or {},
                direction=direction,
                variations=variations,
                language=language,
            )

        def signature_summary(signature: list[list[Any]]) -> dict[str, Any]:
            return {
                "glyphs": [row[0] for row in signature],
                "glyph_identity_pattern": glyph_identity_pattern(signature),
                "advance": [
                    sum(int(row[1]) for row in signature),
                    sum(int(row[2]) for row in signature),
                ],
            }

        cases: dict[str, Any] = {}
        single_plain = shaped(
            "—",
            "en",
            features={"calt": False, "vert": False, "vrt2": False},
        )
        single_calt = shaped(
            "—",
            "en",
            features={"calt": True, "vert": False, "vrt2": False},
        )
        cases["single-calt-invariant"] = {
            "ok": single_plain == single_calt,
            "plain": signature_summary(single_plain),
            "calt": signature_summary(single_calt),
        }

        languages = {
            "default": "en",
            "zh-Hans": "zh-Hans",
            "zh-Hant": "zh-Hant",
            "zh-HK": "zh-HK",
            "ja": "ja",
            "ko": "ko",
        }
        for label, language in languages.items():
            single = shaped("—", language)
            pair = shaped("——", language)
            triple = shaped("———", language)
            vertical_pair = shaped("——", language, "ttb")
            vertical_triple = shaped("———", language, "ttb")
            encoded_pair = shaped("⸺", language)
            encoded_triple = shaped("⸻", language)
            encoded_vertical_pair = shaped("⸺", language, "ttb")
            encoded_vertical_triple = shaped("⸻", language, "ttb")
            pair_summary = signature_summary(pair)
            triple_summary = signature_summary(triple)
            expected_localized = label != "default" or region == "CL"
            expected_single_advance = (
                None
                if label in {"default", "ko"} and region != "CL"
                else upem
            )
            single_advance = sum(int(row[1]) for row in single)
            localized_ok = (
                len(single) == 1
                and len(pair) == 1
                and len(triple) == 1
                and pair_summary["advance"] == [2 * upem, 0]
                and triple_summary["advance"] == [3 * upem, 0]
            )
            if expected_single_advance is not None:
                localized_ok = localized_ok and single_advance == expected_single_advance
            default_ok = (
                len(single) == len(pair) == len(triple) == 1
                and 0 < single_advance < pair_summary["advance"][0] < triple_summary["advance"][0]
            )
            vertical_ok = (
                len(vertical_pair) == 1
                and len(vertical_triple) == 1
                and signature_summary(vertical_pair)["advance"] == [0, -2 * upem]
                and signature_summary(vertical_triple)["advance"] == [0, -3 * upem]
            ) if expected_localized else True
            equivalence_ok = (
                pair[0][0] == encoded_pair[0][0]
                and triple[0][0] == encoded_triple[0][0]
                and (
                    not expected_localized
                    or (
                        vertical_pair[0][0] == encoded_vertical_pair[0][0]
                        and vertical_triple[0][0] == encoded_vertical_triple[0][0]
                    )
                )
            )
            case_ok = (
                localized_ok if expected_localized else default_ok
            ) and vertical_ok and equivalence_ok
            cases[label] = {
                "ok": case_ok,
                "localized": expected_localized,
                "single": signature_summary(single),
                "pair": pair_summary,
                "triple": triple_summary,
                "vertical_pair": signature_summary(vertical_pair),
                "vertical_triple": signature_summary(vertical_triple),
                "encoded_pair": signature_summary(encoded_pair),
                "encoded_triple": signature_summary(encoded_triple),
                "encoded_vertical_pair": signature_summary(encoded_vertical_pair),
                "encoded_vertical_triple": signature_summary(encoded_vertical_triple),
                "equivalence_ok": equivalence_ok,
            }
        return {
            "ok": structure["ok"] and all(case["ok"] for case in cases.values()),
            "mechanism": "Source Han ccmp/locl/vert-vrt2",
            "structure": structure,
            "cases": cases,
        }
    finally:
        font.close()


def dash_semantic_roles(font: TTFont, region: str) -> dict[str, str]:
    roles = b.upstream_dash_roles(font)
    if region != "CL":
        korean_mapping = b.dash_locl_mappings_by_language(font, roles).get(
            "KOR ",
            {},
        )
        korean_single = korean_mapping.get(roles["proportional"])
        if not korean_single:
            raise ValueError("KOR locl does not expose its single-dash alternate")
        roles["korean_single"] = korean_single
    return roles


def dash_glyph_measurement(
    glyph_set: Any,
    glyph_name: str,
) -> dict[str, Any]:
    glyph = glyph_set[glyph_name]
    area_pen = AreaPen(glyph_set)
    recording_pen = DecomposingRecordingPen(glyph_set)
    glyph.draw(TeePen(area_pen, recording_pen))
    points = sorted(
        (float(point[0]), float(point[1]))
        for command, arguments in recording_pen.value
        if command in {"moveTo", "lineTo", "curveTo", "qCurveTo"}
        for point in arguments
        if point is not None and len(point) == 2
    )
    return {
        "points": points,
        "area": abs(float(area_pen.value)),
        "width": float(getattr(glyph, "width", 0) or 0),
        "lsb": float(getattr(glyph, "lsb", 0) or 0),
        "height": float(getattr(glyph, "height", 0) or 0),
        "tsb": float(getattr(glyph, "tsb", 0) or 0),
    }


def dash_instruction_length(font: TTFont, glyph_name: str) -> int:
    if "glyf" not in font or glyph_name not in font["glyf"].glyphs:
        return 0
    glyph = font["glyf"][glyph_name]
    glyph.expand(font["glyf"])
    program = getattr(glyph, "program", None)
    if program is None:
        return 0
    try:
        return len(program.getBytecode())
    except Exception:
        return 0


def transformed_dash_points(
    points: list[tuple[float, float]],
    italic: bool,
) -> list[tuple[float, float]]:
    shear = math.tan(math.radians(CJK_ITALIC_ANGLE_DEGREES)) if italic else 0.0
    return sorted(
        (float(otRound(x + y * shear)), float(otRound(y)))
        for x, y in points
    )


def point_multiset_residual(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
) -> float:
    if len(first) != len(second):
        return float("inf")
    return max(
        (
            max(abs(first_x - second_x), abs(first_y - second_y))
            for (first_x, first_y), (second_x, second_y) in zip(first, second)
        ),
        default=0.0,
    )


def static_dash_source_path(region: str, weight_name: str) -> Path:
    classical = b.classical_static_override_path(region, weight_name)
    if classical is not None:
        if not classical.exists():
            raise FileNotFoundError(classical)
        return classical
    _work_key, work_dir = b.static_hint_work_dir(weight_name)
    return b.build_shs_ttf(region, weight_name, work_dir)


EXTENSION_RAW_REFERENCE_CACHE: dict[tuple[str, str, bool], Path] = {}


def static_extension_raw_reference_path(
    region: str,
    weight_name: str,
    italic: bool,
) -> Path:
    key = (region, weight_name, italic)
    cached = EXTENSION_RAW_REFERENCE_CACHE.get(key)
    if cached is not None and cached.exists():
        return cached
    if b.static_uses_official_glyph_baseline(weight_name):
        raise ValueError(f"{weight_name} is not a static extension weight")
    weight_value = next(
        int(stop["value"])
        for stop in b.SOURCE_HAN_WEIGHT_STOPS
        if str(stop["name"]) == weight_name
    )
    _work_key, work_dir = b.static_hint_work_dir(weight_name)
    fragments = b.build_sarasa_static_fragments(
        region,
        weight_name,
        weight_value,
        italic,
        work_dir,
    )
    output = (
        work_dir
        / "audit-raw-pass2-v1"
        / region
        / b.static_output_name(region, weight_name, italic)
    )
    if not output.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
        b.build_sarasa_pass2(
            Path(fragments["pass1"]),
            Path(fragments["kanji"]),
            Path(fragments["hangul"]),
            output,
            italic,
            work_dir,
        )
    EXTENSION_RAW_REFERENCE_CACHE[key] = output
    return output


def add_counted_sample(
    item: dict[str, Any],
    key: str,
    sample: Any,
    *,
    limit: int = 16,
) -> None:
    item["counts"][key] += 1
    samples = item["samples"].setdefault(key, [])
    if len(samples) < limit:
        samples.append(sample)


def audit_static_upstream_dash_sources() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    area_curves: dict[tuple[str, bool, bool, str], dict[int, float]] = {}
    weights = [
        (str(stop["name"]), int(stop["value"]))
        for stop in b.SOURCE_HAN_WEIGHT_STOPS
    ]
    total = len(b.REGION_ORDER) * len(weights)
    done = 0
    for region in b.REGION_ORDER:
        for weight_name, weight_value in weights:
            done += 1
            source_path = static_dash_source_path(region, weight_name)
            log(
                f"static upstream dash source {done}/{total}: "
                f"{region} {weight_name}"
            )
            source = TTFont(source_path, recalcBBoxes=False, recalcTimestamp=False)
            try:
                source_roles = dash_semantic_roles(source, region)
                source_glyph_set = source.getGlyphSet()
                source_measurements = {
                    role: dash_glyph_measurement(source_glyph_set, glyph_name)
                    for role, glyph_name in source_roles.items()
                }
                for hinted in (False, True):
                    for italic in (False, True):
                        target_path = static_path(
                            region,
                            weight_name,
                            italic,
                            hinted,
                        )
                        item: dict[str, Any] = {
                            "kind": "source-parity",
                            "region": region,
                            "weight": weight_name,
                            "wght": weight_value,
                            "italic": italic,
                            "hinted": hinted,
                            "target": display_path(target_path),
                            "source": display_path(source_path),
                            "roles_checked": len(source_roles),
                            "counts": {
                                "missing": 0,
                                "role_set": 0,
                                "outline": 0,
                                "h_advance": 0,
                                "h_lsb": 0,
                                "v_advance": 0,
                                "v_tsb": 0,
                                "instructions": 0,
                            },
                            "samples": {},
                        }
                        if not target_path.exists():
                            item["counts"]["missing"] = 1
                            item["samples"]["missing"] = [display_path(target_path)]
                            out.append(item)
                            continue
                        target = TTFont(
                            target_path,
                            recalcBBoxes=False,
                            recalcTimestamp=False,
                        )
                        try:
                            try:
                                target_roles = dash_semantic_roles(target, region)
                            except Exception as error:
                                item["counts"]["role_set"] = 1
                                item["samples"]["role_set"] = [
                                    f"{type(error).__name__}: {error}"
                                ]
                                out.append(item)
                                continue
                            if set(target_roles) != set(source_roles):
                                item["counts"]["role_set"] = 1
                                item["samples"]["role_set"] = [
                                    {
                                        "target": sorted(target_roles),
                                        "source": sorted(source_roles),
                                    }
                                ]
                                out.append(item)
                                continue
                            target_glyph_set = target.getGlyphSet()
                            role_details: dict[str, Any] = {}
                            for role in sorted(source_roles):
                                source_name = source_roles[role]
                                target_name = target_roles[role]
                                source_measurement = source_measurements[role]
                                target_measurement = dash_glyph_measurement(
                                    target_glyph_set,
                                    target_name,
                                )
                                expected_points = transformed_dash_points(
                                    source_measurement["points"],
                                    italic,
                                )
                                outline_residual = point_multiset_residual(
                                    expected_points,
                                    target_measurement["points"],
                                )
                                if outline_residual != 0:
                                    add_counted_sample(
                                        item,
                                        "outline",
                                        {
                                            "role": role,
                                            "target_glyph": target_name,
                                            "source_glyph": source_name,
                                            "residual": outline_residual,
                                            "target_points": target_measurement["points"],
                                            "expected_points": expected_points,
                                        },
                                    )
                                expected_lsb = min(
                                    (point[0] for point in expected_points),
                                    default=source_measurement["lsb"],
                                )
                                metric_expectations = {
                                    "h_advance": (
                                        target_measurement["width"],
                                        source_measurement["width"],
                                    ),
                                    "h_lsb": (
                                        target_measurement["lsb"],
                                        expected_lsb,
                                    ),
                                    "v_advance": (
                                        target_measurement["height"],
                                        source_measurement["height"],
                                    ),
                                    "v_tsb": (
                                        target_measurement["tsb"],
                                        source_measurement["tsb"],
                                    ),
                                }
                                for metric_key, (actual, expected) in metric_expectations.items():
                                    if actual != expected:
                                        add_counted_sample(
                                            item,
                                            metric_key,
                                            {
                                                "role": role,
                                                "actual": actual,
                                                "expected": expected,
                                            },
                                        )
                                instruction_length = dash_instruction_length(
                                    target,
                                    target_name,
                                )
                                if hinted != bool(instruction_length):
                                    add_counted_sample(
                                        item,
                                        "instructions",
                                        {
                                            "role": role,
                                            "bytes": instruction_length,
                                            "expected_hinted": hinted,
                                        },
                                    )
                                role_details[role] = {
                                    "source_glyph": source_name,
                                    "target_glyph": target_name,
                                    "outline_residual": outline_residual,
                                    "area": target_measurement["area"],
                                    "metrics": {
                                        key: [actual, expected]
                                        for key, (actual, expected) in metric_expectations.items()
                                    },
                                    "instruction_bytes": instruction_length,
                                }
                                area_curves.setdefault(
                                    (region, italic, hinted, role),
                                    {},
                                )[weight_value] = target_measurement["area"]
                            item["role_details"] = role_details
                        finally:
                            target.close()
                        out.append(item)
            finally:
                source.close()

    expected_weights = [value for _name, value in weights]
    for (region, italic, hinted, role), curve in sorted(area_curves.items()):
        item = {
            "kind": "weight-curve",
            "region": region,
            "italic": italic,
            "hinted": hinted,
            "role": role,
            "counts": {
                "missing_weight": 0,
                "nonmonotonic_area": 0,
                "frozen_area": 0,
            },
            "samples": {},
            "areas": {str(weight): curve.get(weight) for weight in expected_weights},
        }
        missing_weights = [weight for weight in expected_weights if weight not in curve]
        if missing_weights:
            item["counts"]["missing_weight"] = len(missing_weights)
            item["samples"]["missing_weight"] = missing_weights
        else:
            values = [curve[weight] for weight in expected_weights]
            decreases = [
                {
                    "from": expected_weights[index - 1],
                    "to": expected_weights[index],
                    "from_area": values[index - 1],
                    "to_area": values[index],
                }
                for index in range(1, len(values))
                if values[index] + 0.01 < values[index - 1]
            ]
            if decreases:
                item["counts"]["nonmonotonic_area"] = len(decreases)
                item["samples"]["nonmonotonic_area"] = decreases
            if values[-1] <= values[0] + 1:
                item["counts"]["frozen_area"] = 1
                item["samples"]["frozen_area"] = [values[0], values[-1]]
        out.append(item)
    return out


def vf_dash_reference_font(region: str, italic: bool) -> TTFont:
    source = TTFont(b.source_han_vf_path(region))
    limited = instantiateVariableFont(
        source,
        b.AXIS_LIMIT,
        inplace=False,
        optimize=True,
    )
    source.close()
    override_path = b.classical_vf_override_path(region)
    override: TTFont | None = None
    try:
        if override_path is not None:
            override_source = TTFont(override_path)
            override = instantiateVariableFont(
                override_source,
                b.AXIS_LIMIT,
                inplace=False,
                optimize=True,
            )
            override_source.close()
            b.apply_classical_vf_override(limited, override, region)
        b.apply_public_weight_axis(limited)
        metric_source = limited
        if override is not None:
            b.apply_public_weight_axis(override)
            metric_source = override
        b.preserve_upstream_dash_metric_variations(limited, metric_source)
        b.remove_metric_variation_maps(limited)
    finally:
        if override is not None:
            override.close()
    if italic:
        b.shear_font(limited, CJK_ITALIC_ANGLE_DEGREES)
    return limited


def audit_vf_upstream_dash_sources() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    controls = sorted(b.SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT)
    curve_weights = list(range(200, 901, 25))
    regions = b.variable_regions(b.REGION_ORDER)
    total = len(regions) * 2
    done = 0
    for region in regions:
        for italic in (False, True):
            done += 1
            target_path = vf_path(region, italic)
            log(
                f"VF upstream dash source {done}/{total}: "
                f"{region}{' Italic' if italic else ''}"
            )
            item: dict[str, Any] = {
                "region": region,
                "italic": italic,
                "target": display_path(target_path),
                "controls": controls,
                "curve_step": 25,
                "counts": {
                    "missing": 0,
                    "role_set": 0,
                    "outline": 0,
                    "h_advance": 0,
                    "h_lsb": 0,
                    "v_advance": 0,
                    "v_tsb": 0,
                    "nonmonotonic_area": 0,
                    "frozen_area": 0,
                },
                "samples": {},
            }
            if not target_path.exists():
                item["counts"]["missing"] = 1
                item["samples"]["missing"] = [display_path(target_path)]
                out.append(item)
                continue
            target = TTFont(target_path)
            source = vf_dash_reference_font(region, italic)
            try:
                try:
                    target_roles = dash_semantic_roles(target, region)
                    source_roles = dash_semantic_roles(source, region)
                except Exception as error:
                    item["counts"]["role_set"] = 1
                    item["samples"]["role_set"] = [
                        f"{type(error).__name__}: {error}"
                    ]
                    out.append(item)
                    continue
                if set(target_roles) != set(source_roles):
                    item["counts"]["role_set"] = 1
                    item["samples"]["role_set"] = [
                        {
                            "target": sorted(target_roles),
                            "source": sorted(source_roles),
                        }
                    ]
                    out.append(item)
                    continue
                control_details: dict[str, Any] = {}
                for weight in controls:
                    target_set = target.getGlyphSet(location={"wght": weight})
                    source_set = source.getGlyphSet(location={"wght": weight})
                    role_details: dict[str, Any] = {}
                    for role in sorted(source_roles):
                        target_measurement = dash_glyph_measurement(
                            target_set,
                            target_roles[role],
                        )
                        source_measurement = dash_glyph_measurement(
                            source_set,
                            source_roles[role],
                        )
                        residual = point_multiset_residual(
                            target_measurement["points"],
                            source_measurement["points"],
                        )
                        if residual > 0.01:
                            add_counted_sample(
                                item,
                                "outline",
                                {
                                    "wght": weight,
                                    "role": role,
                                    "residual": residual,
                                },
                            )
                        metrics: dict[str, list[float]] = {}
                        for metric_key, field in (
                            ("h_advance", "width"),
                            ("h_lsb", "lsb"),
                            ("v_advance", "height"),
                            ("v_tsb", "tsb"),
                        ):
                            actual = target_measurement[field]
                            expected = source_measurement[field]
                            metrics[metric_key] = [actual, expected]
                            if abs(actual - expected) > 0.01:
                                add_counted_sample(
                                    item,
                                    metric_key,
                                    {
                                        "wght": weight,
                                        "role": role,
                                        "actual": actual,
                                        "expected": expected,
                                    },
                                )
                        role_details[role] = {
                            "outline_residual": residual,
                            "area": target_measurement["area"],
                            "metrics": metrics,
                        }
                    control_details[str(weight)] = role_details
                item["control_details"] = control_details

                area_curves: dict[str, dict[int, float]] = {
                    role: {} for role in target_roles
                }
                for weight in curve_weights:
                    glyph_set = target.getGlyphSet(location={"wght": weight})
                    for role, glyph_name in target_roles.items():
                        area_curves[role][weight] = dash_glyph_measurement(
                            glyph_set,
                            glyph_name,
                        )["area"]
                item["area_curves"] = {
                    role: {str(weight): curve[weight] for weight in curve_weights}
                    for role, curve in area_curves.items()
                }
                for role, curve in area_curves.items():
                    decreases = [
                        {
                            "role": role,
                            "from": curve_weights[index - 1],
                            "to": curve_weights[index],
                            "from_area": curve[curve_weights[index - 1]],
                            "to_area": curve[curve_weights[index]],
                        }
                        for index in range(1, len(curve_weights))
                        if curve[curve_weights[index]] + 0.01
                        < curve[curve_weights[index - 1]]
                    ]
                    for decrease in decreases:
                        add_counted_sample(item, "nonmonotonic_area", decrease)
                    if curve[900] <= curve[200] + 1:
                        add_counted_sample(
                            item,
                            "frozen_area",
                            {
                                "role": role,
                                "area_200": curve[200],
                                "area_900": curve[900],
                            },
                        )
            finally:
                source.close()
                target.close()
            out.append(item)
    return out


def em_dash_raster_status(
    path: Path,
    variations: dict[str, float] | None = None,
    ppems: tuple[int, ...] = (*DEFAULT_RASTER_PPEMS, 48),
) -> dict[str, Any]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        glyph_order = font.getGlyphOrder()
        upem = int(font["head"].unitsPerEm)
        face = freetype.Face(str(path))
        if variations and "fvar" in font:
            face.set_var_design_coords(
                [
                    float(variations.get(axis.axisTag, axis.defaultValue))
                    for axis in font["fvar"].axes
                ]
            )
        results: dict[str, Any] = {}
        all_ok = True
        case_definitions = [
                ("default", "——", "ltr", {}, (1,), 2),
                (
                    "ccmp",
                    "——",
                    "ltr",
                    {"ccmp": True, "calt": False, "vert": False, "vrt2": False},
                    (1,),
                    2,
                ),
                ("vertical-default", "——", "ttb", {}, (1,), 2),
                (
                    "vert",
                    "——",
                    "ttb",
                    {"ccmp": True, "calt": False, "vert": True, "vrt2": False},
                    (1,),
                    2,
                ),
                (
                    "vrt2-only",
                    "——",
                    "ttb",
                    {"ccmp": True, "calt": False, "vert": False, "vrt2": True},
                    (1, 2),
                    2,
                ),
                (
                    "vert-plus-vrt2",
                    "——",
                    "ttb",
                    {"ccmp": True, "calt": False, "vert": True, "vrt2": True},
                    (1,),
                    2,
                ),
                ("triple-default", "———", "ltr", {}, (1,), 3),
                ("triple-vertical-default", "———", "ttb", {}, (1,), 3),
                (
                    "triple-vrt2-only",
                    "———",
                    "ttb",
                    {"ccmp": True, "calt": False, "vert": False, "vrt2": True},
                    (1, 3),
                    3,
                ),
                ("encoded-two-em-default", "⸺", "ltr", {}, (1,), 2),
                ("encoded-two-em-vertical-default", "⸺", "ttb", {}, (1,), 2),
                ("encoded-three-em-default", "⸻", "ltr", {}, (1,), 3),
                ("encoded-three-em-vertical-default", "⸻", "ttb", {}, (1,), 3),
            ]
        for ppem in ppems:
            face.set_pixel_sizes(0, ppem)
            for (
                name,
                text,
                direction,
                features,
                expected_glyph_counts,
                advance_multiplier,
            ) in case_definitions:
                signature = shape_signature_data(
                    data,
                    glyph_order,
                    text,
                    "hani",
                    features,
                    direction=direction,
                    variations=variations,
                    language="zh-Hans",
                )
                glyph_pixels: list[set[tuple[int, int]]] = []
                bitmap_metrics: list[dict[str, int]] = []
                cursor_x = 0
                cursor_y = 0
                for glyph_name, x_advance, y_advance, x_offset, y_offset in signature:
                    origin_x = otRound((cursor_x + x_offset) * ppem / upem)
                    origin_y = otRound((cursor_y + y_offset) * ppem / upem)
                    pixels, metrics = freetype_rendered_pixels(
                        face,
                        font.getGlyphID(glyph_name),
                        origin_x,
                        origin_y,
                    )
                    glyph_pixels.append(pixels)
                    bitmap_metrics.append(metrics)
                    cursor_x += x_advance
                    cursor_y += y_advance

                combined = set().union(*glyph_pixels) if glyph_pixels else set()
                component_count = pixel_component_count(combined)
                diagonal_component_count = pixel_component_count(
                    combined,
                    include_diagonals=True,
                )
                individual_component_counts = [
                    pixel_component_count(pixels) for pixels in glyph_pixels
                ]
                axis = 0 if direction == "ltr" else 1
                occupied_axis_values = sorted({point[axis] for point in combined})
                gaps = (
                    [
                        value
                        for value in range(occupied_axis_values[0], occupied_axis_values[-1] + 1)
                        if value not in occupied_axis_values
                    ]
                    if occupied_axis_values
                    else []
                )
                thicknesses = [
                    metrics["rows"] if direction == "ltr" else metrics["width"]
                    for metrics in bitmap_metrics
                ]
                thickness_delta = (
                    abs(thicknesses[0] - thicknesses[1]) if len(thicknesses) == 2 else None
                )
                total_advance = (
                    sum(row[1] for row in signature),
                    sum(row[2] for row in signature),
                )
                expected_advance = (
                    (advance_multiplier * upem, 0)
                    if direction == "ltr"
                    else (0, -advance_multiplier * upem)
                )
                case_ok = (
                    len(signature) in expected_glyph_counts
                    and len(glyph_pixels) == len(signature)
                    and all(glyph_pixels)
                    and total_advance == expected_advance
                    and (
                        (
                            not gaps
                            and component_count == 1
                        )
                        if len(signature) == 1
                        else (
                            glyph_identity_pattern(signature)
                            == [0] * len(signature)
                            and all(count == 1 for count in individual_component_counts)
                            and len(
                                {
                                    tuple(sorted(metrics.items()))
                                    for metrics in bitmap_metrics
                                }
                            )
                            == 1
                        )
                    )
                )
                all_ok = all_ok and case_ok
                results[f"{ppem}:{name}"] = {
                    "ok": case_ok,
                    "gap_count": len(gaps),
                    "gap_samples": gaps[:8],
                    "component_count": component_count,
                    "diagonal_component_count": diagonal_component_count,
                    "individual_component_counts": individual_component_counts,
                    "thicknesses": thicknesses,
                    "thickness_delta": thickness_delta,
                    "glyph_count": len(signature),
                    "expected_glyph_counts": list(expected_glyph_counts),
                    "total_advance": list(total_advance),
                    "expected_advance": list(expected_advance),
                    "overlap_pixels": (
                        len(glyph_pixels[0] & glyph_pixels[1])
                        if len(glyph_pixels) == 2
                        else 0
                    ),
                    "bitmap_metrics": bitmap_metrics,
                }
        return {
            "ok": all_ok,
            "variations": variations or {},
            "ppems": list(ppems),
            "cases": results,
        }
    finally:
        font.close()


def shape_semantic_signature_data(
    data: bytes,
    font: TTFont,
    text: str,
    script: str,
    features: dict[str, bool],
    direction: str | None = None,
    compare_outlines: bool = True,
    language: str = "ZHS",
) -> dict[str, Any]:
    face = hb.Face(data)
    hb_font = hb.Font(face)
    hb_font.scale = (face.upem, face.upem)
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.guess_segment_properties()
    buffer.script = script
    buffer.language = language
    if direction:
        buffer.direction = direction
    hb.shape(hb_font, buffer, features)
    glyph_order = font.getGlyphOrder()
    identity_classes: dict[int, int] = {}
    glyphs = []
    for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
        gid = int(info.codepoint)
        identity_classes.setdefault(gid, len(identity_classes))
        glyph_name = glyph_order[gid]
        outline_sha256 = None
        if compare_outlines:
            outline = decomposed_outline_signature(font, glyph_name)
            outline_sha256 = hashlib.sha256(repr(outline).encode("utf-8")).hexdigest()
        glyphs.append(
            {
                "outline_sha256": outline_sha256,
                "hmtx": list(font["hmtx"].metrics[glyph_name]) if "hmtx" in font else None,
                "vmtx": list(font["vmtx"].metrics[glyph_name]) if "vmtx" in font else None,
                "position": [
                    int(position.x_advance),
                    int(position.y_advance),
                    int(position.x_offset),
                    int(position.y_offset),
                ],
            }
        )
    return {
        "outlines_compared": compare_outlines,
        "glyph_identity_pattern": [identity_classes[int(info.codepoint)] for info in buffer.glyph_infos],
        "glyphs": glyphs,
    }


def shape_semantic_signature(
    path: Path,
    text: str,
    script: str,
    features: dict[str, bool],
    direction: str | None = None,
    compare_outlines: bool = True,
    language: str = "ZHS",
) -> dict[str, Any]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        return shape_semantic_signature_data(
            data,
            font,
            text,
            script,
            features,
            direction=direction,
            compare_outlines=compare_outlines,
            language=language,
        )
    finally:
        font.close()


def contextual_spacing_status(path: Path, variations: dict[str, float] | None = None) -> dict[str, Any]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        glyph_order = font.getGlyphOrder()
        feature_tags = layout_feature_tags(font, "GPOS")
        upem = int(font["head"].unitsPerEm)
    finally:
        font.close()

    def shape(text: str, features: dict[str, bool], direction: str) -> list[list[Any]]:
        return shape_signature_data(
            data,
            glyph_order,
            text,
            "hani",
            features,
            direction=direction,
            variations=variations,
        )

    contextual = "（（天地））"
    isolated = "（天地）"
    horizontal_off = shape(contextual, {"chws": False}, "ltr")
    horizontal_on = shape(contextual, {"chws": True}, "ltr")
    vertical_off = shape(contextual, {"vchw": False, "vert": True}, "ttb")
    vertical_on = shape(contextual, {"vchw": True, "vert": True}, "ttb")
    isolated_horizontal_off = shape(isolated, {"chws": False}, "ltr")
    isolated_horizontal_on = shape(isolated, {"chws": True}, "ltr")
    isolated_vertical_off = shape(isolated, {"vchw": False, "vert": True}, "ttb")
    isolated_vertical_on = shape(isolated, {"vchw": True, "vert": True}, "ttb")

    horizontal_reduction = sum(item[1] for item in horizontal_off) - sum(item[1] for item in horizontal_on)
    vertical_reduction = abs(sum(item[2] for item in vertical_off)) - abs(sum(item[2] for item in vertical_on))
    glyphs_stable = [item[0] for item in horizontal_off] == [item[0] for item in horizontal_on]
    vertical_glyphs_stable = [item[0] for item in vertical_off] == [item[0] for item in vertical_on]
    ok = (
        CONTEXTUAL_SPACING_FEATURES <= feature_tags
        and glyphs_stable
        and vertical_glyphs_stable
        and horizontal_reduction == upem
        and vertical_reduction == upem
        and isolated_horizontal_off == isolated_horizontal_on
        and isolated_vertical_off == isolated_vertical_on
    )
    return {
        "ok": ok,
        "variations": variations or {},
        "features": sorted(feature_tags & CONTEXTUAL_SPACING_FEATURES),
        "upem": upem,
        "horizontal_reduction": horizontal_reduction,
        "vertical_reduction": vertical_reduction,
        "isolated_horizontal_unchanged": isolated_horizontal_off == isolated_horizontal_on,
        "isolated_vertical_unchanged": isolated_vertical_off == isolated_vertical_on,
        "horizontal_off": horizontal_off,
        "horizontal_on": horizontal_on,
        "vertical_off": vertical_off,
        "vertical_on": vertical_on,
    }


def value_record_signature(value: Any) -> list[Any] | None:
    if value is None:
        return None
    return [
        getattr(value, "XPlacement", None),
        getattr(value, "YPlacement", None),
        getattr(value, "XAdvance", None),
        getattr(value, "YAdvance", None),
    ]


def gsub_reachable_glyphs(font: TTFont) -> set[str]:
    state = SimpleNamespace(glyphs=set((font.getBestCmap() or {}).values()))
    if "GSUB" in font:
        font["GSUB"].closure_glyphs(state)
    return state.glyphs


def intentional_product_glyph_closure(font: TTFont) -> set[str]:
    cmap = font.getBestCmap() or {}
    state = SimpleNamespace(
        glyphs={cmap[codepoint] for codepoint in INTENTIONAL_CPS if codepoint in cmap}
    )
    if "GSUB" in font:
        font["GSUB"].closure_glyphs(state)
    return state.glyphs


def palt_value_signature(
    font: TTFont,
    excluded_glyphs: set[str] | None = None,
) -> dict[str, Any]:
    if "GPOS" not in font or not font["GPOS"].table.LookupList:
        return {"reachable_entries": [], "unreachable_entries": 0, "excluded_entries": 0}
    table = font["GPOS"].table
    reachable = gsub_reachable_glyphs(font)
    excluded_glyphs = excluded_glyphs or set()
    entries: Counter[str] = Counter()
    unreachable_entries = 0
    excluded_entries = 0
    for lookup_index in b.feature_lookup_indices(font, "GPOS", {"palt"}):
        if lookup_index >= len(table.LookupList.Lookup):
            continue
        lookup = table.LookupList.Lookup[lookup_index]
        for subtable in lookup.SubTable or []:
            if hasattr(subtable, "ExtSubTable"):
                subtable = subtable.ExtSubTable
            if not hasattr(subtable, "Coverage") or not getattr(subtable, "Coverage", None):
                continue
            glyphs = list(subtable.Coverage.glyphs or [])
            if getattr(subtable, "Format", 1) == 2:
                values = list(getattr(subtable, "Value", []) or [])
            else:
                values = [getattr(subtable, "Value", None) for _glyph_name in glyphs]
            for glyph_name, value in zip(glyphs, values):
                if glyph_name in excluded_glyphs:
                    excluded_entries += 1
                    continue
                if glyph_name not in reachable:
                    unreachable_entries += 1
                    continue
                semantic_entry = {
                    "outline_sha256": hashlib.sha256(
                        repr(decomposed_outline_signature(font, glyph_name)).encode("utf-8")
                    ).hexdigest(),
                    "hmtx": list(font["hmtx"].metrics[glyph_name]) if "hmtx" in font else None,
                    "vmtx": list(font["vmtx"].metrics[glyph_name]) if "vmtx" in font else None,
                    "value": value_record_signature(value),
                }
                entries[json.dumps(semantic_entry, sort_keys=True, separators=(",", ":"))] += 1
    return {
        "reachable_entries": [
            {"entry": json.loads(entry), "count": count}
            for entry, count in sorted(entries.items())
        ],
        "unreachable_entries": unreachable_entries,
        "excluded_entries": excluded_entries,
    }


def static_propdigits_shape_status(
    target_path: Path,
    reference_path: Path | None,
) -> dict[str, Any]:
    target_data = target_path.read_bytes()
    target = TTFont(target_path)
    try:
        text = "0123456789"
        target_default = shape_semantic_signature_data(
            target_data,
            target,
            text,
            "latn",
            {"kern": False, "pnum": False, "tnum": False},
        )
        target_pnum = shape_semantic_signature_data(
            target_data,
            target,
            text,
            "latn",
            {"kern": False, "pnum": True, "tnum": False},
        )
        target_tnum = shape_semantic_signature_data(
            target_data,
            target,
            text,
            "latn",
            {"kern": False, "pnum": False, "tnum": True},
        )
    finally:
        target.close()
    if reference_path is None:
        pnum_advances = [glyph["position"][0] for glyph in target_pnum["glyphs"]]
        tnum_advances = [glyph["position"][0] for glyph in target_tnum["glyphs"]]
        checks = {
            "default_not_pnum": target_default != target_pnum,
            "pnum_not_proportional": len(set(pnum_advances)) <= 1,
            "tnum_not_tabular": len(set(tnum_advances)) != 1,
            "pnum_tnum_same": target_pnum == target_tnum,
            "digit_count": len(target_pnum["glyphs"]) != 10 or len(target_tnum["glyphs"]) != 10,
        }
        pairs = {
            "default_not_pnum": (target_default, target_pnum),
            "pnum_not_proportional": (pnum_advances, "more than one advance"),
            "tnum_not_tabular": (tnum_advances, "one shared advance"),
            "pnum_tnum_same": (target_pnum, target_tnum),
            "digit_count": (len(target_pnum["glyphs"]), len(target_tnum["glyphs"])),
        }
    else:
        reference_data = reference_path.read_bytes()
        reference = TTFont(reference_path)
        try:
            reference_pnum = shape_semantic_signature_data(
                reference_data,
                reference,
                text,
                "latn",
                {"kern": False, "pnum": True, "tnum": False},
            )
            reference_tnum = shape_semantic_signature_data(
                reference_data,
                reference,
                text,
                "latn",
                {"kern": False, "pnum": False, "tnum": True},
            )
        finally:
            reference.close()
        checks = {
            "default_not_pnum": target_default != target_pnum,
            "pnum_reference_mismatch": target_pnum != reference_pnum,
            "tnum_reference_mismatch": target_tnum != reference_tnum,
        }
        pairs = {
            "default_not_pnum": (target_default, target_pnum),
            "pnum_reference_mismatch": (target_pnum, reference_pnum),
            "tnum_reference_mismatch": (target_tnum, reference_tnum),
        }
    return {
        "counts": {key: int(value) for key, value in checks.items()},
        "samples": {
            key: sample_pair(*pairs[key])
            for key, failed in checks.items()
            if failed
        },
    }


def audit_static_propdigits_shaping() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = len(b.REGION_ORDER) * 2 * 2 * len(EXACT_WEIGHTS)
    done = 0
    for region in b.REGION_ORDER:
        for hinted in (False, True):
            for italic in (False, True):
                for weight in EXACT_WEIGHTS:
                    done += 1
                    target_path = static_path(region, weight, italic, hinted)
                    extension_weight = weight not in UPSTREAM_EXACT_WEIGHTS
                    reference_path = (
                        None
                        if extension_weight
                        else static_reference_path(region, weight, italic, hinted)
                    )
                    log(
                        f"static PropDigits {done}/{total}: {region} {weight}"
                        f"{' Italic' if italic else ''} {'hinted' if hinted else 'unhinted'}"
                    )
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "reference": display_path(reference_path) if reference_path else None,
                        "reference_mode": (
                            "standalone-extension-semantics"
                            if extension_weight
                            else "official-exact"
                        ),
                        "counts": {
                            "default_not_pnum": 0,
                            "pnum_reference_mismatch": 0,
                            "tnum_reference_mismatch": 0,
                        },
                        "samples": {},
                    }
                    if not target_path.exists() or (
                        reference_path is not None and not reference_path.exists()
                    ):
                        item["missing"] = True
                    else:
                        item.update(static_propdigits_shape_status(target_path, reference_path))
                    out.append(item)
    return out


def audit_static_palt_shaping() -> list[dict[str, Any]]:
    out = []
    regions = list(b.REGION_ORDER)
    total = len(regions) * 2 * 2 * len(EXACT_WEIGHTS)
    done = 0
    for region in regions:
        for hinted in [False, True]:
            for italic in [False, True]:
                for weight in EXACT_WEIGHTS:
                    done += 1
                    target_path = static_path(region, weight, italic, hinted)
                    extension_weight = weight not in UPSTREAM_EXACT_WEIGHTS
                    ref_path = (
                        static_extension_raw_reference_path(region, weight, italic)
                        if extension_weight
                        else b.static_reference_font_path(region, weight, italic)
                    )
                    log(
                        "static palt shaping "
                        f"{done}/{total}: {region} {weight}{' Italic' if italic else ''} "
                        f"{'hinted' if hinted else 'unhinted'}"
                    )
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "reference": display_path(ref_path),
                        "reference_mode": (
                            "sarasa-pass2-extension-source"
                            if extension_weight
                            else "official-sarasa-exact"
                        ),
                        "counts": {"palt_kana_shape": 0, "palt_lookup_values": 0},
                        "observations": {},
                        "samples": {},
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    target_shape = shape_semantic_signature(target_path, "かなカナ", "kana", {"palt": True})
                    reference_shape = shape_semantic_signature(ref_path, "かなカナ", "kana", {"palt": True})
                    if target_shape != reference_shape:
                        item["counts"]["palt_kana_shape"] = 1
                        item["samples"]["palt_kana_shape"] = sample_pair(target_shape, reference_shape)
                    target_font = TTFont(target_path)
                    reference_font = TTFont(ref_path)
                    try:
                        target_values = palt_value_signature(
                            target_font,
                            intentional_product_glyph_closure(target_font),
                        )
                        reference_values = palt_value_signature(
                            reference_font,
                            intentional_product_glyph_closure(reference_font),
                        )
                        item["observations"]["target_unreachable_palt_entries"] = target_values["unreachable_entries"]
                        item["observations"]["reference_unreachable_palt_entries"] = reference_values["unreachable_entries"]
                        item["observations"]["target_excluded_product_palt_entries"] = target_values["excluded_entries"]
                        item["observations"]["reference_excluded_product_palt_entries"] = reference_values["excluded_entries"]
                        if extension_weight:
                            target_entries = Counter(
                                json.dumps(entry["entry"], sort_keys=True, separators=(",", ":"))
                                for entry in target_values["reachable_entries"]
                                for _index in range(int(entry["count"]))
                            )
                            reference_entries = Counter(
                                json.dumps(entry["entry"], sort_keys=True, separators=(",", ":"))
                                for entry in reference_values["reachable_entries"]
                                for _index in range(int(entry["count"]))
                            )
                            unexpected_entries = target_entries - reference_entries
                            item["observations"]["reference_source_only_palt_entries"] = sum(
                                (reference_entries - target_entries).values()
                            )
                            lookup_values_match = not unexpected_entries
                        else:
                            lookup_values_match = (
                                target_values["reachable_entries"]
                                == reference_values["reachable_entries"]
                            )
                        if not lookup_values_match:
                            item["counts"]["palt_lookup_values"] = 1
                            item["samples"]["palt_lookup_values"] = sample_pair(
                                target_values["reachable_entries"],
                                reference_values["reachable_entries"],
                            )
                    finally:
                        target_font.close()
                        reference_font.close()
                    out.append(item)
    return out


def audit_static_em_dash_shaping() -> list[dict[str, Any]]:
    out = []
    weights = [str(stop["name"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS]
    total = len(b.REGION_ORDER) * 2 * 2 * len(weights)
    done = 0
    for region in b.REGION_ORDER:
        for hinted in [False, True]:
            for italic in [False, True]:
                for weight in weights:
                    done += 1
                    target_path = static_path(region, weight, italic, hinted)
                    ref_path = b.static_reference_font_path(region, weight, italic)
                    log(
                        "static em dash shaping "
                        f"{done}/{total}: {region} {weight}{' Italic' if italic else ''} "
                        f"{'hinted' if hinted else 'unhinted'}"
                    )
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "reference": display_path(ref_path),
                        "counts": {
                            "em_dash_upstream_shaping": 0,
                            "em_dash_upstream_raster": 0,
                            "em_dash_structure": 0,
                            "em_dash_single_changed": 0,
                            "em_dash_default_behavior": 0,
                            "em_dash_calt_behavior": 0,
                            "em_dash_vert_behavior": 0,
                            "em_dash_vrt2_behavior": 0,
                            "em_dash_legacy_vertical_glyphs": 0,
                            "em_dash_default_raster": 0,
                            "em_dash_calt_raster": 0,
                            "em_dash_vert_raster": 0,
                            "em_dash_vrt2_raster": 0,
                        },
                        "samples": {},
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    status = upstream_dash_shaping_status(target_path, region)
                    if not status["structure"]["ok"]:
                        item["counts"]["em_dash_structure"] = len(
                            status["structure"]["reasons"]
                        )
                        item["samples"]["em_dash_structure"] = status["structure"]
                    shaping_failures = {
                        name: case
                        for name, case in status["cases"].items()
                        if not case["ok"]
                    }
                    if shaping_failures:
                        item["counts"]["em_dash_upstream_shaping"] = len(
                            shaping_failures
                        )
                        item["samples"]["em_dash_upstream_shaping"] = shaping_failures
                    raster_status = em_dash_raster_status(target_path)
                    raster_failures = {
                        key: value
                        for key, value in raster_status["cases"].items()
                        if not value["ok"]
                    }
                    if raster_failures:
                        item["counts"]["em_dash_upstream_raster"] = len(
                            raster_failures
                        )
                        item["samples"]["em_dash_upstream_raster"] = raster_failures
                    if weight == "Regular" and not hinted:
                        item["sample"] = status
                    out.append(item)
    return out


def vf_em_dash_axis_advance_status(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    face = hb.Face(data)
    hb_font = hb.Font(face)
    hb_font.scale = (face.upem, face.upem)
    expected = 2 * int(face.upem)
    cases = [
        ("default", "ltr", {}, (expected, 0), [0]),
        (
            "ccmp",
            "ltr",
            {"ccmp": True, "calt": False, "vert": False, "vrt2": False},
            (expected, 0),
            [0],
        ),
        ("vertical-default", "ttb", {}, (0, -expected), [0]),
        (
            "vert",
            "ttb",
            {"ccmp": True, "calt": False, "vert": True, "vrt2": False},
            (0, -expected),
            [0],
        ),
        (
            "vrt2-only",
            "ttb",
            {"ccmp": True, "calt": False, "vert": False, "vrt2": True},
            (0, -expected),
            [0, 0],
        ),
        (
            "vert-plus-vrt2",
            "ttb",
            {"ccmp": True, "calt": False, "vert": True, "vrt2": True},
            (0, -expected),
            [0],
        ),
    ]
    failure_counts = {name: 0 for name, *_rest in cases}
    samples: list[dict[str, Any]] = []
    location_count = 0
    for half_step in range(400, 1801):
        weight = half_step / 2
        location_count += 1
        hb_font.set_variations({"wght": weight})
        for name, direction, features, expected_advance, expected_pattern in cases:
            buffer = hb.Buffer()
            buffer.add_str("——")
            buffer.guess_segment_properties()
            buffer.script = "hani"
            buffer.language = "ZHS"
            buffer.direction = direction
            hb.shape(hb_font, buffer, features)
            glyphs = [int(info.codepoint) for info in buffer.glyph_infos]
            classes: dict[int, int] = {}
            pattern = []
            for glyph_id in glyphs:
                classes.setdefault(glyph_id, len(classes))
                pattern.append(classes[glyph_id])
            advance = (
                sum(int(position.x_advance) for position in buffer.glyph_positions),
                sum(int(position.y_advance) for position in buffer.glyph_positions),
            )
            if pattern != expected_pattern or advance != expected_advance:
                failure_counts[name] += 1
                if len(samples) < 32:
                    samples.append(
                        {
                            "wght": weight,
                            "case": name,
                            "glyph_ids": glyphs,
                            "glyph_identity_pattern": pattern,
                            "advance": list(advance),
                            "expected_advance": list(expected_advance),
                        }
                    )
    return {
        "ok": not any(failure_counts.values()),
        "range": [200.0, 900.0],
        "step": 0.5,
        "locations_checked": location_count,
        "shapes_checked": location_count * len(cases),
        "failure_counts": failure_counts,
        "samples": samples,
    }


def vf_static_em_dash_visual_parity_status(
    vf_path: Path,
    static_path: Path,
    weight: int,
    ppems: tuple[int, ...] = (*DEFAULT_RASTER_PPEMS, 48),
) -> dict[str, Any]:
    vf_data = vf_path.read_bytes()
    static_data = static_path.read_bytes()
    vf_font = TTFont(vf_path)
    static_font = TTFont(static_path)
    try:
        vf_order = vf_font.getGlyphOrder()
        static_order = static_font.getGlyphOrder()
        vf_glyph_set = vf_font.getGlyphSet(location={"wght": weight})
        static_glyph_set = static_font.getGlyphSet()
        vf_face = freetype.Face(str(vf_path))
        vf_face.set_var_design_coords(
            [
                float(weight if axis.axisTag == "wght" else axis.defaultValue)
                for axis in vf_font["fvar"].axes
            ]
        )
        static_face = freetype.Face(str(static_path))
        cases: dict[str, Any] = {}
        all_ok = True
        for name, direction in (("horizontal", "ltr"), ("vertical", "ttb")):
            vf_signature = shape_signature_data(
                vf_data,
                vf_order,
                "——",
                "hani",
                {},
                direction=direction,
                variations={"wght": weight},
            )
            static_signature = shape_signature_data(
                static_data,
                static_order,
                "——",
                "hani",
                {},
                direction=direction,
            )
            vf_bounds = combined_visual_bounds(
                shaped_visual_bounds(vf_signature, vf_glyph_set)
            )
            static_bounds = combined_visual_bounds(
                shaped_visual_bounds(static_signature, static_glyph_set)
            )
            vf_advance = (
                sum(row[1] for row in vf_signature),
                sum(row[2] for row in vf_signature),
            )
            static_advance = (
                sum(row[1] for row in static_signature),
                sum(row[2] for row in static_signature),
            )
            raster: dict[str, Any] = {}
            for ppem in ppems:
                vf_pixels = render_shaped_pixels(
                    vf_face,
                    vf_font,
                    vf_signature,
                    ppem,
                )
                static_pixels = render_shaped_pixels(
                    static_face,
                    static_font,
                    static_signature,
                    ppem,
                )
                difference = vf_pixels ^ static_pixels
                vf_pixel_bounds = (
                    (
                        min(x for x, _y in vf_pixels),
                        min(y for _x, y in vf_pixels),
                        max(x for x, _y in vf_pixels),
                        max(y for _x, y in vf_pixels),
                    )
                    if vf_pixels
                    else None
                )
                static_pixel_bounds = (
                    (
                        min(x for x, _y in static_pixels),
                        min(y for _x, y in static_pixels),
                        max(x for x, _y in static_pixels),
                        max(y for _x, y in static_pixels),
                    )
                    if static_pixels
                    else None
                )
                bounds_delta = (
                    [
                        vf_value - static_value
                        for vf_value, static_value in zip(
                            vf_pixel_bounds,
                            static_pixel_bounds,
                        )
                    ]
                    if vf_pixel_bounds and static_pixel_bounds
                    else None
                )
                pixel_count_delta = len(vf_pixels) - len(static_pixels)
                pixel_count_tolerance = max(
                    2,
                    math.ceil(max(len(vf_pixels), len(static_pixels)) * 0.1),
                )
                raster_ok = (
                    bool(vf_pixels)
                    and bool(static_pixels)
                    and bounds_delta is not None
                    and all(abs(delta) <= 1 for delta in bounds_delta)
                    and abs(pixel_count_delta) <= pixel_count_tolerance
                    and pixel_component_count(vf_pixels)
                    == pixel_component_count(static_pixels)
                    == 1
                )
                raster[str(ppem)] = {
                    "ok": raster_ok,
                    "vf_pixels": len(vf_pixels),
                    "static_pixels": len(static_pixels),
                    "pixel_count_delta": pixel_count_delta,
                    "pixel_count_tolerance": pixel_count_tolerance,
                    "vf_bounds": vf_pixel_bounds,
                    "static_bounds": static_pixel_bounds,
                    "bounds_delta": bounds_delta,
                    "vf_component_count": pixel_component_count(vf_pixels),
                    "static_component_count": pixel_component_count(static_pixels),
                    "symmetric_difference_pixels": len(difference),
                    "difference_samples": sorted(difference)[:16],
                }
            exact_visual_ok = (
                len(vf_signature) == 1
                and len(static_signature) == 1
                and vf_bounds == static_bounds
                and vf_advance == static_advance
            )
            exact_raster_ok = all(sample["ok"] for sample in raster.values())
            # Source Han's static TTF and VF release paths have their own
            # whole-glyph translation and quantization differences. Separate
            # source-parity audits prove that each product follows its actual
            # upstream; this cross-product check therefore enforces semantics
            # (one connected glyph and the same 2em advance) and records exact
            # visual/raster parity as a transparent observation.
            semantic_ok = (
                len(vf_signature) == 1
                and len(static_signature) == 1
                and vf_bounds is not None
                and static_bounds is not None
                and vf_advance == static_advance
            )
            case_ok = semantic_ok
            all_ok = all_ok and case_ok
            cases[name] = {
                "ok": case_ok,
                "semantic_ok": semantic_ok,
                "exact_visual_ok": exact_visual_ok,
                "exact_raster_ok": exact_raster_ok,
                "vf_glyph_count": len(vf_signature),
                "static_glyph_count": len(static_signature),
                "vf_bounds": vf_bounds,
                "static_bounds": static_bounds,
                "vf_advance": list(vf_advance),
                "static_advance": list(static_advance),
                "raster": raster,
            }
        return {
            "ok": all_ok,
            "weight": weight,
            "static": display_path(static_path),
            "ppems": list(ppems),
            "cases": cases,
        }
    finally:
        vf_font.close()
        static_font.close()


def audit_vf_em_dash_shaping() -> list[dict[str, Any]]:
    out = []
    regions = b.variable_regions(b.REGION_ORDER)
    weights = VF_EM_DASH_PROBE_WEIGHTS
    total = len(regions) * 2 * len(weights)
    done = 0
    for region in regions:
        for italic in [False, True]:
            target_path = vf_path(region, italic)
            axis_advance_status = (
                b.variable_two_em_dash_axis_status(target_path, region, italic)
                if target_path.exists()
                else None
            )
            for weight_name, weight_value in weights:
                done += 1
                log(
                    f"VF em dash shaping {done}/{total}: {region} {weight_name}"
                    f"{' Italic' if italic else ''}"
                )
                item: dict[str, Any] = {
                    "region": region,
                    "weight": weight_name,
                    "wght": weight_value,
                    "italic": italic,
                    "target": display_path(target_path),
                    "counts": {
                        "em_dash_upstream_shaping": 0,
                        "em_dash_upstream_raster": 0,
                        "em_dash_single_changed": 0,
                        "em_dash_default_behavior": 0,
                        "em_dash_ccmp_behavior": 0,
                        "em_dash_vertical_default_behavior": 0,
                        "em_dash_vert_behavior": 0,
                        "em_dash_vrt2_only_behavior": 0,
                        "em_dash_vert_plus_vrt2_behavior": 0,
                        "em_dash_source_han_long_dash_behavior": 0,
                        "em_dash_structure": 0,
                        "em_dash_default_raster": 0,
                        "em_dash_ccmp_raster": 0,
                        "em_dash_vertical_default_raster": 0,
                        "em_dash_vert_raster": 0,
                        "em_dash_vrt2_only_raster": 0,
                        "em_dash_vert_plus_vrt2_raster": 0,
                        "em_dash_source_han_long_dash_raster": 0,
                        "em_dash_axis_advance": 0,
                        "em_dash_static_semantic_parity": 0,
                    },
                    "samples": {},
                }
                if not target_path.exists():
                    item["missing"] = True
                    out.append(item)
                    continue
                if weight_value == weights[0][1] and axis_advance_status is not None:
                    item["axis_advance_sweep"] = axis_advance_status
                    if not axis_advance_status["ok"]:
                        item["counts"]["em_dash_axis_advance"] = len(
                            axis_advance_status["failure_samples"]
                        )
                        item["samples"]["em_dash_axis_advance"] = axis_advance_status
                status = upstream_dash_shaping_status(
                    target_path,
                    region,
                    {"wght": weight_value},
                )
                if not status["structure"]["ok"]:
                    item["counts"]["em_dash_structure"] = len(
                        status["structure"]["reasons"]
                    )
                    item["samples"]["em_dash_structure"] = status["structure"]
                shaping_failures = {
                    name: case
                    for name, case in status["cases"].items()
                    if not case["ok"]
                }
                if shaping_failures:
                    item["counts"]["em_dash_upstream_shaping"] = len(
                        shaping_failures
                    )
                    item["samples"]["em_dash_upstream_shaping"] = shaping_failures
                raster_status = em_dash_raster_status(target_path, {"wght": weight_value})
                raster_failures = {
                    key: value
                    for key, value in raster_status["cases"].items()
                    if not value["ok"]
                }
                if raster_failures:
                    item["counts"]["em_dash_upstream_raster"] = len(
                        raster_failures
                    )
                    item["samples"]["em_dash_upstream_raster"] = raster_failures
                if weight_name in EXPECTED_WEIGHTS:
                    static_target = static_path(
                        region,
                        weight_name,
                        italic,
                        hinted=False,
                    )
                    item["static_visual_target"] = display_path(static_target)
                    if not static_target.exists():
                        item["counts"]["em_dash_static_semantic_parity"] = 1
                        item["samples"]["em_dash_static_semantic_parity"] = "missing static target"
                    else:
                        parity = vf_static_em_dash_visual_parity_status(
                            target_path,
                            static_target,
                            weight_value,
                        )
                        item["static_source_boundary_parity"] = parity
                        semantic_failures = {
                            name: case
                            for name, case in parity["cases"].items()
                            if not case["semantic_ok"]
                        }
                        parity["source_boundary_observations"] = {
                            "exact_visual_mismatches": sum(
                                not case["exact_visual_ok"]
                                for case in parity["cases"].values()
                            ),
                            "exact_raster_mismatches": sum(
                                not sample["ok"]
                                for case in parity["cases"].values()
                                for sample in case["raster"].values()
                            ),
                            "covered_by": [
                                "static_upstream_dash_sources",
                                "vf_upstream_dash_sources",
                            ],
                        }
                        if semantic_failures:
                            item["counts"]["em_dash_static_semantic_parity"] = len(
                                semantic_failures
                            )
                            item["samples"]["em_dash_static_semantic_parity"] = semantic_failures
                if weight_value in {200, 400, 900}:
                    item["sample"] = status
                out.append(item)
    return out


def layout_feature_tags(font: TTFont, table_tag: str) -> set[str]:
    if table_tag not in font:
        return set()
    table = font[table_tag].table
    if not table.FeatureList:
        return set()
    return {record.FeatureTag for record in table.FeatureList.FeatureRecord}


CV_SS_TAGS = {f"cv{i:02d}" for i in range(1, 14)} | {f"ss{i:02d}" for i in range(1, 9)}


def feature_records(font: TTFont, table_tag: str) -> list[Any]:
    if table_tag not in font:
        return []
    table = font[table_tag].table
    if not table.FeatureList:
        return []
    return list(table.FeatureList.FeatureRecord or [])


def feature_tag_sequence(font: TTFont, table_tag: str) -> list[str]:
    return [record.FeatureTag for record in feature_records(font, table_tag)]


def feature_lookup_signature(font: TTFont, table_tag: str) -> list[list[Any]]:
    return [
        [record.FeatureTag, list(record.Feature.LookupListIndex or [])]
        for record in feature_records(font, table_tag)
    ]


def base_gpos_feature_lookup_signature(font: TTFont) -> list[list[Any]]:
    dash_lookup_indices = {
        item["lookup_index"]
        for records in b.vertical_em_dash_positioning_records(font).values()
        for item in records
    }
    result = []
    for record in feature_records(font, "GPOS"):
        if record.FeatureTag in CONTEXTUAL_SPACING_FEATURES:
            continue
        original = list(record.Feature.LookupListIndex or [])
        base_indices = [index for index in original if index not in dash_lookup_indices]
        if original and not base_indices:
            continue
        result.append([record.FeatureTag, base_indices])
    return result


def vertical_em_dash_feature_lookup_signature(font: TTFont) -> dict[str, list[list[int]]]:
    result = {"vert": [], "vrt2": []}
    for tag, items in b.vertical_em_dash_positioning_records(font).items():
        by_feature: dict[int, list[int]] = {}
        for item in items:
            by_feature.setdefault(item["feature_index"], []).append(item["lookup_index"])
        result[tag] = [sorted(set(indices)) for _feature, indices in sorted(by_feature.items())]
    return result


def contextual_spacing_feature_lookup_signature(font: TTFont) -> dict[str, list[list[int]]]:
    out = {tag: [] for tag in sorted(CONTEXTUAL_SPACING_FEATURES)}
    for record in feature_records(font, "GPOS"):
        if record.FeatureTag in CONTEXTUAL_SPACING_FEATURES:
            out[record.FeatureTag].append(list(record.Feature.LookupListIndex or []))
    return out


def base_gpos_langsys_signatures(font: TTFont) -> list[list[Any]]:
    if "GPOS" not in font:
        return []
    table = font["GPOS"].table
    if not table.FeatureList or not table.ScriptList:
        return []
    records = list(table.FeatureList.FeatureRecord or [])
    dash_lookup_indices = {
        item["lookup_index"]
        for items in b.vertical_em_dash_positioning_records(font).values()
        for item in items
    }
    excluded = {
        index
        for index, record in enumerate(records)
        if record.FeatureTag in CONTEXTUAL_SPACING_FEATURES
        or (
            bool(record.Feature.LookupListIndex)
            and all(
                lookup_index in dash_lookup_indices
                for lookup_index in record.Feature.LookupListIndex
            )
        )
    }

    def one_langsys(script_tag: str, lang_tag: str, langsys: Any) -> list[Any]:
        required_index = int(getattr(langsys, "ReqFeatureIndex", 0xFFFF))
        required = (
            records[required_index].FeatureTag
            if required_index != 0xFFFF
            and required_index not in excluded
            and 0 <= required_index < len(records)
            else None
        )
        tags = [
            records[index].FeatureTag
            for index in list(langsys.FeatureIndex or [])
            if index not in excluded and 0 <= index < len(records)
        ]
        return [script_tag, lang_tag, required, tags]

    signatures = []
    for script_record in table.ScriptList.ScriptRecord or []:
        script = script_record.Script
        if script.DefaultLangSys:
            signatures.append(one_langsys(script_record.ScriptTag, "dflt", script.DefaultLangSys))
        for lang_record in script.LangSysRecord or []:
            signatures.append(
                one_langsys(script_record.ScriptTag, lang_record.LangSysTag, lang_record.LangSys)
            )
    return signatures


def empty_cv_ss_sequence(font: TTFont, table_tag: str) -> list[str]:
    return [
        record.FeatureTag
        for record in feature_records(font, table_tag)
        if record.FeatureTag in CV_SS_TAGS and not list(record.Feature.LookupListIndex or [])
    ]


def langsys_signatures(font: TTFont, table_tag: str, by_tag: bool) -> list[list[Any]]:
    if table_tag not in font:
        return []
    table = font[table_tag].table
    if not table.FeatureList or not table.ScriptList:
        return []
    feature_tags = feature_tag_sequence(font, table_tag)

    def feature_value(index: int) -> int | str:
        if by_tag:
            return feature_tags[index] if 0 <= index < len(feature_tags) else f"#{index}"
        return index

    def req_value(index: int) -> int | str | None:
        if index == 0xFFFF:
            return None
        return feature_value(index)

    def one_langsys(script_tag: str, lang_tag: str, langsys: Any) -> list[Any]:
        indices = list(langsys.FeatureIndex or [])
        return [
            script_tag,
            lang_tag,
            req_value(getattr(langsys, "ReqFeatureIndex", 0xFFFF)),
            [feature_value(index) for index in indices],
        ]

    signatures = []
    for script_record in table.ScriptList.ScriptRecord or []:
        script = script_record.Script
        if script.DefaultLangSys:
            signatures.append(one_langsys(script_record.ScriptTag, "dflt", script.DefaultLangSys))
        for lang_record in script.LangSysRecord or []:
            signatures.append(one_langsys(script_record.ScriptTag, lang_record.LangSysTag, lang_record.LangSys))
    return signatures


def expected_upstream_dash_gsub_template(
    reference: TTFont,
    region: str,
) -> tuple[list[str], list[list[Any]], list[list[Any]]]:
    reference_tags = feature_tag_sequence(reference, "GSUB")
    reference_table = reference["GSUB"].table
    if region == "CL" or not reference_table.ScriptList:
        return (
            reference_tags,
            langsys_signatures(reference, "GSUB", by_tag=False),
            langsys_signatures(reference, "GSUB", by_tag=True),
        )

    missing_dash_languages = {
        lang_record.LangSysTag
        for script_record in reference_table.ScriptList.ScriptRecord
        for lang_record in script_record.Script.LangSysRecord
        if lang_record.LangSysTag in b.CJK_LOCL_LANGUAGES
        and not any(
            reference_tags[index] == "locl"
            for index in list(lang_record.LangSys.FeatureIndex or [])
        )
    }
    if not missing_dash_languages:
        return (
            reference_tags,
            langsys_signatures(reference, "GSUB", by_tag=False),
            langsys_signatures(reference, "GSUB", by_tag=True),
        )

    insert_at = max(
        (index + 1 for index, tag in enumerate(reference_tags) if tag == "locl"),
        default=len(reference_tags),
    )
    mapping_groups = list(
        dict.fromkeys(
            "korean" if language == "KOR " else "standard"
            for language in sorted(missing_dash_languages)
        )
    )
    feature_index_by_group = {
        group: insert_at + offset for offset, group in enumerate(mapping_groups)
    }
    expected_tags = [
        *reference_tags[:insert_at],
        *(["locl"] * len(mapping_groups)),
        *reference_tags[insert_at:],
    ]

    def one_langsys(
        script_tag: str,
        language: str,
        langsys: Any,
        *,
        by_tag: bool,
    ) -> list[Any]:
        indices = [
            index + len(mapping_groups) if index >= insert_at else index
            for index in list(langsys.FeatureIndex or [])
        ]
        if language in missing_dash_languages and not any(
            expected_tags[index] == "locl" for index in indices
        ):
            tags = [expected_tags[index] for index in indices]
            position = max(
                (index + 1 for index, tag in enumerate(tags) if tag == "hist"),
                default=len(indices),
            )
            group = "korean" if language == "KOR " else "standard"
            indices.insert(position, feature_index_by_group[group])
        required = int(getattr(langsys, "ReqFeatureIndex", 0xFFFF))
        if required != 0xFFFF and required >= insert_at:
            required += len(mapping_groups)
        if by_tag:
            required_value: int | str | None = (
                None if required == 0xFFFF else expected_tags[required]
            )
            feature_values: list[int | str] = [
                expected_tags[index] for index in indices
            ]
        else:
            required_value = None if required == 0xFFFF else required
            feature_values = indices
        return [script_tag, language, required_value, feature_values]

    index_signatures: list[list[Any]] = []
    tag_signatures: list[list[Any]] = []
    for script_record in reference_table.ScriptList.ScriptRecord:
        script = script_record.Script
        if script.DefaultLangSys:
            index_signatures.append(
                one_langsys(
                    script_record.ScriptTag,
                    "dflt",
                    script.DefaultLangSys,
                    by_tag=False,
                )
            )
            tag_signatures.append(
                one_langsys(
                    script_record.ScriptTag,
                    "dflt",
                    script.DefaultLangSys,
                    by_tag=True,
                )
            )
        for lang_record in script.LangSysRecord:
            index_signatures.append(
                one_langsys(
                    script_record.ScriptTag,
                    lang_record.LangSysTag,
                    lang_record.LangSys,
                    by_tag=False,
                )
            )
            tag_signatures.append(
                one_langsys(
                    script_record.ScriptTag,
                    lang_record.LangSysTag,
                    lang_record.LangSys,
                    by_tag=True,
                )
            )
    return expected_tags, index_signatures, tag_signatures


def langsys_feature_tags(font: TTFont, table_tag: str, script_tag: str, lang_tag: str) -> list[str]:
    for record in langsys_signatures(font, table_tag, by_tag=True):
        if record[0] == script_tag and record[1] == lang_tag:
            return list(record[3])
    return []


def has_langsys(font: TTFont, table_tag: str, script_tag: str, lang_tag: str) -> bool:
    return any(
        record[0] == script_tag and record[1] == lang_tag
        for record in langsys_signatures(font, table_tag, by_tag=False)
    )


def gpos_subtable_shape(subtable: Any) -> list[Any]:
    shape = [subtable.__class__.__name__, getattr(subtable, "Format", None)]
    if hasattr(subtable, "ExtensionLookupType"):
        ext = getattr(subtable, "ExtSubTable", None)
        shape.extend(
            [
                getattr(subtable, "ExtensionLookupType", None),
                ext.__class__.__name__ if ext else None,
                getattr(ext, "Format", None) if ext else None,
                getattr(ext, "ValueFormat", None) if ext else None,
                getattr(ext, "ValueFormat1", None) if ext else None,
                getattr(ext, "ValueFormat2", None) if ext else None,
            ]
        )
    else:
        shape.extend(
            [
                getattr(subtable, "ValueFormat", None),
                getattr(subtable, "ValueFormat1", None),
                getattr(subtable, "ValueFormat2", None),
            ]
        )
    return shape


def gpos_lookup_signature(font: TTFont) -> list[list[Any]]:
    if "GPOS" not in font:
        return []
    table = font["GPOS"].table
    if not table.LookupList:
        return []
    signatures = []
    for lookup in table.LookupList.Lookup or []:
        signatures.append(
            [
                lookup.LookupType,
                lookup.LookupFlag,
                getattr(lookup, "MarkFilteringSet", None),
                [gpos_subtable_shape(subtable) for subtable in lookup.SubTable or []],
            ]
        )
    return signatures


def sample_pair(target: Any, reference: Any, limit: int = 16) -> dict[str, Any]:
    def trim(value: Any) -> Any:
        if isinstance(value, list) and len(value) > limit:
            return {"count": len(value), "head": value[:limit]}
        return value

    return {"target": trim(target), "reference": trim(reference)}


def audit_static_layout_templates() -> list[dict[str, Any]]:
    out = []
    weights = [str(stop["name"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS]
    total = len(b.REGION_ORDER) * 2 * len(weights) * 2
    done = 0
    for region in b.REGION_ORDER:
        for hinted in [False, True]:
            for weight_name in weights:
                for italic in [False, True]:
                    done += 1
                    target_path = static_path(region, weight_name, italic, hinted)
                    ref_path = b.static_reference_font_path(region, weight_name, italic)
                    log(
                        "static layout template "
                        f"{done}/{total}: {region} {weight_name}{' Italic' if italic else ''} "
                        f"{'hinted' if hinted else 'unhinted'}"
                    )
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight_name,
                        "reference_style": b.static_reference_style_name(weight_name, italic),
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "reference": display_path(ref_path),
                        "counts": {
                            "gsub_feature_record_sequence": 0,
                            "gsub_empty_cv_ss_sequence": 0,
                            "gsub_langsys_index_order": 0,
                            "gsub_langsys_tag_order": 0,
                            "gpos_feature_record_structure": 0,
                            "gpos_langsys_index_order": 0,
                            "gpos_lookup_structure": 0,
                            "cl_required_langsys": 0,
                            "cl_latn_cat_locl": 0,
                        },
                        "samples": {},
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    target = TTFont(target_path)
                    reference = TTFont(ref_path)
                    try:
                        target_gsub_tags = feature_tag_sequence(target, "GSUB")
                        (
                            expected_gsub_tags,
                            expected_gsub_index,
                            expected_gsub_tag,
                        ) = expected_upstream_dash_gsub_template(reference, region)
                        if target_gsub_tags != expected_gsub_tags:
                            item["counts"]["gsub_feature_record_sequence"] = 1
                            item["samples"]["gsub_feature_record_sequence"] = sample_pair(
                                target_gsub_tags, expected_gsub_tags
                            )
                        target_empty = empty_cv_ss_sequence(target, "GSUB")
                        reference_empty = empty_cv_ss_sequence(reference, "GSUB")
                        if target_empty != reference_empty:
                            item["counts"]["gsub_empty_cv_ss_sequence"] = 1
                            item["samples"]["gsub_empty_cv_ss_sequence"] = sample_pair(target_empty, reference_empty)

                        target_gsub_index = langsys_signatures(target, "GSUB", by_tag=False)
                        if target_gsub_index != expected_gsub_index:
                            item["counts"]["gsub_langsys_index_order"] = 1
                            item["samples"]["gsub_langsys_index_order"] = sample_pair(
                                target_gsub_index, expected_gsub_index
                            )
                        target_gsub_tag = langsys_signatures(target, "GSUB", by_tag=True)
                        if target_gsub_tag != expected_gsub_tag:
                            item["counts"]["gsub_langsys_tag_order"] = 1
                            item["samples"]["gsub_langsys_tag_order"] = sample_pair(target_gsub_tag, expected_gsub_tag)

                        target_gpos_feature = base_gpos_feature_lookup_signature(target)
                        reference_gpos_feature = feature_lookup_signature(reference, "GPOS")
                        if target_gpos_feature != reference_gpos_feature:
                            item["counts"]["gpos_feature_record_structure"] = 1
                            item["samples"]["gpos_feature_record_structure"] = sample_pair(
                                target_gpos_feature, reference_gpos_feature
                            )
                        target_gpos_base = base_gpos_langsys_signatures(target)
                        reference_gpos_base = langsys_signatures(reference, "GPOS", by_tag=True)
                        if target_gpos_base != reference_gpos_base:
                            item["counts"]["gpos_langsys_index_order"] = 1
                            item["samples"]["gpos_langsys_index_order"] = sample_pair(
                                target_gpos_base, reference_gpos_base
                            )
                        target_gpos_lookup = gpos_lookup_signature(target)
                        reference_gpos_lookup = gpos_lookup_signature(reference)
                        contextual_features = contextual_spacing_feature_lookup_signature(target)
                        dash_features = vertical_em_dash_feature_lookup_signature(target)
                        base_lookup_count = len(reference_gpos_lookup)
                        expected_contextual_features = {
                            "chws": [[base_lookup_count, base_lookup_count + 2]],
                            "vchw": [[base_lookup_count + 3, base_lookup_count + 5]],
                        }
                        lookup_prefix_ok = target_gpos_lookup[:base_lookup_count] == reference_gpos_lookup
                        contextual_lookup_count_ok = len(target_gpos_lookup) == base_lookup_count + 6
                        contextual_feature_links_ok = contextual_features == expected_contextual_features
                        dash_feature_links_ok = dash_features == {"vert": [], "vrt2": []}
                        if not (
                            lookup_prefix_ok
                            and contextual_lookup_count_ok
                            and contextual_feature_links_ok
                            and dash_feature_links_ok
                        ):
                            item["counts"]["gpos_lookup_structure"] = 1
                            item["samples"]["gpos_lookup_structure"] = sample_pair(
                                {
                                    "base_prefix": target_gpos_lookup[:base_lookup_count],
                                    "appended": target_gpos_lookup[base_lookup_count:],
                                    "contextual_features": contextual_features,
                                    "vertical_em_dash_features": dash_features,
                                },
                                {
                                    "base_prefix": reference_gpos_lookup,
                                    "appended_count": 6,
                                    "contextual_features": expected_contextual_features,
                                    "vertical_em_dash_features": {"vert": [], "vrt2": []},
                                },
                            )

                        if region == "CL":
                            missing_langsys = [
                                [script, lang]
                                for script, lang in (("hang", "KOR "), ("hani", "KOR "), ("kana", "KOR "))
                                if not has_langsys(target, "GSUB", script, lang)
                            ]
                            if missing_langsys:
                                item["counts"]["cl_required_langsys"] = len(missing_langsys)
                                item["samples"]["cl_required_langsys"] = missing_langsys
                            latn_cat = langsys_feature_tags(target, "GSUB", "latn", "CAT ")
                            cat_locl_indices = [
                                index
                                for signature in langsys_signatures(
                                    target,
                                    "GSUB",
                                    by_tag=False,
                                )
                                if signature[0] == "latn" and signature[1] == "CAT "
                                for index in signature[3]
                                if target["GSUB"].table.FeatureList.FeatureRecord[index].FeatureTag
                                == "locl"
                            ]
                            if (
                                "locl" not in latn_cat
                                or not cat_locl_indices
                                or any(
                                    target["GSUB"].table.FeatureList.FeatureRecord[index]
                                    .Feature.LookupListIndex
                                    for index in cat_locl_indices
                                )
                            ):
                                item["counts"]["cl_latn_cat_locl"] = 1
                                item["samples"]["cl_latn_cat_locl"] = {
                                    "tags": latn_cat,
                                    "locl_feature_indices": cat_locl_indices,
                                }
                    finally:
                        target.close()
                        reference.close()
                    out.append(item)
    return out


def referenced_name_ids(font: TTFont) -> set[int]:
    referenced: set[int] = set()
    if "fvar" in font:
        referenced.update(axis.axisNameID for axis in font["fvar"].axes)
        for instance in font["fvar"].instances:
            referenced.add(instance.subfamilyNameID)
            if instance.postscriptNameID != 0xFFFF:
                referenced.add(instance.postscriptNameID)
    if "STAT" in font:
        stat = font["STAT"].table
        referenced.add(stat.ElidedFallbackNameID)
        if stat.DesignAxisRecord:
            referenced.update(axis.AxisNameID for axis in stat.DesignAxisRecord.Axis)
        if stat.AxisValueArray:
            referenced.update(value.ValueNameID for value in stat.AxisValueArray.AxisValue)
    for table_tag in ("GSUB", "GPOS"):
        if table_tag not in font:
            continue
        table = font[table_tag].table
        if not table.FeatureList:
            continue
        for record in table.FeatureList.FeatureRecord:
            params = getattr(record.Feature, "FeatureParams", None)
            if params is None:
                continue
            for field, value in vars(params).items():
                if field.endswith("NameID") and isinstance(value, int) and value > 0:
                    referenced.add(value)
    return referenced


def invalid_variations_ps_prefixes(font: TTFont) -> list[str]:
    records = [record for record in font["name"].names if record.nameID == 25]
    if not records:
        return ["<missing>"]
    return sorted(
        {
            value
            for record in records
            if not (value := record.toUnicode()) or any(character not in ASCII_ALPHANUMERIC for character in value)
        }
    )


def missing_referenced_name_ids(font: TTFont) -> list[int]:
    present = {record.nameID for record in font["name"].names}
    return sorted(referenced_name_ids(font) - present)


def stat_weight_records(font: TTFont) -> list[dict[str, Any]]:
    if "STAT" not in font:
        return []
    stat = font["STAT"].table
    if not stat.DesignAxisRecord or not stat.AxisValueArray:
        return []
    weight_axis_indices = {
        index
        for index, axis in enumerate(stat.DesignAxisRecord.Axis)
        if axis.AxisTag == "wght"
    }
    records: list[dict[str, Any]] = []
    for value in stat.AxisValueArray.AxisValue:
        value_format = int(value.Format)
        nominal: float | None = None
        range_min: float | None = None
        range_max: float | None = None
        if value_format == 4:
            coordinates = {
                int(record.AxisIndex): float(record.Value)
                for record in value.AxisValueRecord
            }
            if not weight_axis_indices & set(coordinates):
                continue
            nominal = coordinates[next(iter(weight_axis_indices & set(coordinates)))]
        elif int(value.AxisIndex) in weight_axis_indices:
            if value_format == 2:
                nominal = float(value.NominalValue)
                range_min = float(value.RangeMinValue)
                range_max = float(value.RangeMaxValue)
            else:
                nominal = float(value.Value)
        else:
            continue
        records.append(
            {
                "format": value_format,
                "name": font["name"].getDebugName(value.ValueNameID),
                "nominal": nominal,
                "range_min": range_min,
                "range_max": range_max,
                "flags": int(value.Flags),
            }
        )
    return sorted(records, key=lambda record: float(record["nominal"]))


def stat_weight_status(font: TTFont, static_weight: str | None = None) -> dict[str, Any]:
    actual = stat_weight_records(font)
    if static_weight is not None:
        stop = next(stop for stop in b.SOURCE_HAN_WEIGHT_STOPS if stop["name"] == static_weight)
        expected = [
            {
                "name": static_weight,
                "nominal": float(stop["value"]),
                "flags": int(stop.get("flags", 0)),
            }
        ]
    else:
        expected = [
            {
                "name": str(stop["name"]),
                "nominal": float(stop["value"]),
                "range_min": float(stop["range_min"]),
                "range_max": float(stop["range_max"]),
                "flags": int(stop.get("flags", 0)),
            }
            for stop in b.SOURCE_HAN_WEIGHT_STOPS
        ]
    actual_comparable = []
    for record in actual:
        comparable = {
            "name": record["name"],
            "nominal": record["nominal"],
            "flags": record["flags"],
        }
        if static_weight is None:
            comparable["range_min"] = record["range_min"]
            comparable["range_max"] = record["range_max"]
        actual_comparable.append(comparable)
    return {"ok": actual_comparable == expected, "expected": expected, "actual": actual}


def vf_axis_mapping_status(font: TTFont, region: str) -> dict[str, Any]:
    source = TTFont(b.source_han_vf_path(region), lazy=True, recalcTimestamp=False)
    try:
        limits = b.AxisLimits(b.AXIS_LIMIT).limitAxesAndPopulateDefaults(source)
        if "avar" in source:
            b.instantiateAvar(source, limits)
        source_axis = b.weight_axis(source)
        source_axis.minValue, source_axis.defaultValue, source_axis.maxValue = b.AXIS_LIMIT["wght"]
        anchors = []
        for public_weight, internal_weight in b.SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT.items():
            actual = b.vf_mapped_normalized_weight(font, public_weight)
            expected = b.normalized_wght(source, internal_weight)
            anchors.append(
                {
                    "public": public_weight,
                    "source_han_internal": internal_weight,
                    "actual_normalized": actual,
                    "expected_normalized": expected,
                    "delta": actual - expected,
                    "ok": abs(actual - expected) <= 1 / 16384,
                }
            )
    finally:
        source.close()
    values = [item["actual_normalized"] for item in anchors]
    monotonic = all(first < second for first, second in zip(values, values[1:]))
    return {
        "ok": monotonic and all(item["ok"] for item in anchors),
        "monotonic": monotonic,
        "anchors": anchors,
    }


def static_style_metadata_status(font: TTFont, weight: str, italic: bool) -> dict[str, Any]:
    if weight == "Regular":
        expected_legacy_style = "Italic" if italic else "Regular"
    elif weight == "Bold":
        expected_legacy_style = "Bold Italic" if italic else "Bold"
    else:
        expected_legacy_style = "Italic" if italic else "Regular"
    os2 = font["OS/2"]
    fs_selection = int(os2.fsSelection)
    mac_style = int(font["head"].macStyle)
    expected = {
        "legacy_subfamily": [expected_legacy_style],
        "fs_italic": italic,
        "fs_bold": weight == "Bold",
        "fs_regular": weight != "Bold" and not italic,
        "mac_italic": italic,
        "mac_bold": weight == "Bold",
    }
    actual = {
        "legacy_subfamily": english_name_values(font, 2),
        "fs_italic": bool(fs_selection & (1 << 0)),
        "fs_bold": bool(fs_selection & (1 << 5)),
        "fs_regular": bool(fs_selection & (1 << 6)),
        "mac_italic": bool(mac_style & 0b10),
        "mac_bold": bool(mac_style & 0b01),
    }
    return {"ok": actual == expected, "expected": expected, "actual": actual}


def post_table_status(font: TTFont) -> dict[str, Any]:
    post = font["post"]
    return {
        "format": float(post.formatType),
        "raw_length": int(font.reader.tables["post"].length),
        "extra_names": len(getattr(post, "extraNames", []) or []),
        "mapping_entries": len(getattr(post, "mapping", {}) or {}),
    }


def audit_metadata() -> dict[str, Any]:
    log("metadata/shaping audit start")
    failures = []
    static_count = 0
    variable_count = 0
    samples = {"static": [], "variable": []}
    for region in b.REGION_ORDER:
        for hinted in [True, False]:
            directory = b.static_dir(region, hinted)
            files = sorted(directory.glob("*.ttf"))
            expected_static_count = 2 * len(EXPECTED_WEIGHTS)
            if len(files) != expected_static_count:
                failures.append({"kind": "static_count", "region": region, "hinted": hinted, "count": len(files)})
            for weight, expected_weight in EXPECTED_WEIGHTS.items():
                for italic in [False, True]:
                    path = static_path(region, weight, italic, hinted)
                    if not path.exists():
                        failures.append({"kind": "missing_static", "file": display_path(path)})
                        continue
                    font = TTFont(path)
                    try:
                        name_hits = [text for text in names(font) if "UI" in text][:5]
                        version_strings = [record.toUnicode() for record in font["name"].names if record.nameID == 5]
                        item = {
                            "file": display_path(path),
                            "weight": font["OS/2"].usWeightClass,
                            "vendor": font["OS/2"].achVendID,
                            "codepage_range_1": int(font["OS/2"].ulCodePageRange1),
                            "head_font_revision": float(font["head"].fontRevision),
                            "name_id_5": version_strings,
                            "legal_names": legal_name_status(font),
                            "mac_name_records": sum(
                                record.platformID == 1 for record in font["name"].names
                            ),
                            "style_metadata": static_style_metadata_status(font, weight, italic),
                            "post": post_table_status(font),
                            "underline": {
                                "thickness": int(font["post"].underlineThickness),
                                "position": int(font["post"].underlinePosition),
                            },
                            "has_fvar": "fvar" in font,
                            "has_gvar": "gvar" in font,
                            "has_stat": "STAT" in font,
                            "stat_weight": stat_weight_status(font, weight),
                            "has_hint_tables": any(tag in font for tag in ("fpgm", "prep", "cvt ")),
                            "has_glyph_program": has_glyph_program(font) if hinted else None,
                            "ots_invalid_explicit_overlap_flags": (
                                b.count_ots_invalid_simple_overlap_flags(font)
                            ),
                            "uppercase_ui_spelling": name_hits,
                            "colon": colon_status(path),
                            "digits": digit_width_status(path),
                            "contextual_spacing": contextual_spacing_status(path),
                        }
                        static_count += 1
                        if len(samples["static"]) < 4:
                            samples["static"].append(item)
                        if item["weight"] != expected_weight:
                            failures.append({"kind": "static_weight", "file": item["file"], "got": item["weight"], "expected": expected_weight})
                        if abs(item["head_font_revision"] - b.FONT_REVISION) > FONT_REVISION_TOLERANCE:
                            failures.append(
                                {
                                    "kind": "static_head_font_revision",
                                    "file": item["file"],
                                    "got": item["head_font_revision"],
                                    "expected": b.FONT_REVISION,
                                }
                            )
                        if not project_name_versions_match(version_strings):
                            failures.append({"kind": "static_name_version", "file": item["file"], "name_id_5": version_strings})
                        if item["vendor"] != b.OS2_VENDOR_ID:
                            failures.append({"kind": "static_vendor", "file": item["file"], "got": item["vendor"]})
                        if item["codepage_range_1"] != b.OS2_CODEPAGE_RANGE_1:
                            failures.append(
                                {
                                    "kind": "static_codepage_range_1",
                                    "file": item["file"],
                                    "got": item["codepage_range_1"],
                                    "expected": b.OS2_CODEPAGE_RANGE_1,
                                    "bit_31": bool(item["codepage_range_1"] & (1 << 31)),
                                }
                            )
                        if not item["legal_names"]["ok"]:
                            failures.append({"kind": "static_legal_names", "file": item["file"], "status": item["legal_names"]})
                        if item["mac_name_records"]:
                            failures.append(
                                {
                                    "kind": "static_mac_name_records",
                                    "file": item["file"],
                                    "count": item["mac_name_records"],
                                }
                            )
                        if not item["style_metadata"]["ok"]:
                            failures.append({"kind": "static_style_metadata", "file": item["file"], "status": item["style_metadata"]})
                        if item["post"] != {"format": 3.0, "raw_length": 32, "extra_names": 0, "mapping_entries": 0}:
                            failures.append({"kind": "static_post_table", "file": item["file"], "status": item["post"]})
                        expected_underline = b.STATIC_UNDERLINE_METRICS[expected_weight]
                        if item["underline"] != {"thickness": expected_underline[0], "position": expected_underline[1]}:
                            failures.append(
                                {
                                    "kind": "static_underline_metrics",
                                    "file": item["file"],
                                    "got": item["underline"],
                                    "expected": {"thickness": expected_underline[0], "position": expected_underline[1]},
                                }
                            )
                        if item["has_fvar"] or item["has_gvar"] or not item["has_stat"]:
                            failures.append({"kind": "static_tables", "file": item["file"], "item": item})
                        if not item["stat_weight"]["ok"]:
                            failures.append(
                                {
                                    "kind": "static_stat_weight",
                                    "file": item["file"],
                                    "status": item["stat_weight"],
                                }
                            )
                        if hinted and (not item["has_hint_tables"] or not item["has_glyph_program"]):
                            failures.append({"kind": "static_missing_hints", "file": item["file"], "has_glyph_program": item["has_glyph_program"]})
                        if (not hinted) and item["has_hint_tables"]:
                            failures.append({"kind": "unhinted_has_hint_tables", "file": item["file"]})
                        if item["ots_invalid_explicit_overlap_flags"]:
                            failures.append(
                                {
                                    "kind": "static_ots_invalid_explicit_overlap_flags",
                                    "file": item["file"],
                                    "count": item["ots_invalid_explicit_overlap_flags"],
                                }
                            )
                        if name_hits:
                            failures.append({"kind": "uppercase_ui_spelling", "file": item["file"], "samples": name_hits})
                        if not item["colon"]["ok"]:
                            failures.append({"kind": "colon_shape", "file": item["file"], "colon": item["colon"]})
                        if not item["digits"]["ok"]:
                            failures.append({"kind": "digit_width_features", "file": item["file"], "digits": item["digits"]})
                        if not item["contextual_spacing"]["ok"]:
                            failures.append(
                                {
                                    "kind": "static_contextual_spacing",
                                    "file": item["file"],
                                    "contextual_spacing": item["contextual_spacing"],
                                }
                            )
                    finally:
                        font.close()
    for region in b.variable_regions(b.REGION_ORDER):
        for italic in [False, True]:
            path = vf_path(region, italic)
            if not path.exists():
                failures.append({"kind": "missing_vf", "file": display_path(path)})
                continue
            font = TTFont(path)
            try:
                name_hits = [text for text in names(font) if "UI" in text][:5]
                version_strings = [record.toUnicode() for record in font["name"].names if record.nameID == 5]
                instance_weights = sorted({int(instance.coordinates.get("wght")) for instance in font["fvar"].instances}) if "fvar" in font else []
                instance_names = (
                    [font["name"].getDebugName(instance.subfamilyNameID) for instance in font["fvar"].instances]
                    if "fvar" in font
                    else []
                )
                expected_instance_names = [
                    str(stop["name"]) + (" Italic" if italic and stop["name"] != "Regular" else "")
                    if stop["name"] != "Regular"
                    else ("Italic" if italic else "Regular")
                    for stop in b.SOURCE_HAN_WEIGHT_STOPS
                ]
                invalid_name_id_25 = invalid_variations_ps_prefixes(font)
                missing_name_ids = missing_referenced_name_ids(font)
                gpos_variation_devices = (
                    b.collect_ot_variation_devices(font["GPOS"].table)
                    if "GPOS" in font
                    else []
                )
                gpos_variation_feature_counts: dict[str, int] = {}
                if "GPOS" in font and font["GPOS"].table.FeatureList:
                    lookup_device_counts = {
                        index: len(b.collect_ot_variation_devices(lookup))
                        for index, lookup in enumerate(font["GPOS"].table.LookupList.Lookup)
                    }
                    for record in font["GPOS"].table.FeatureList.FeatureRecord:
                        count = sum(
                            lookup_device_counts.get(index, 0)
                            for index in set(record.Feature.LookupListIndex or [])
                        )
                        if count:
                            gpos_variation_feature_counts[record.FeatureTag] = max(
                                count,
                                gpos_variation_feature_counts.get(record.FeatureTag, 0),
                            )
                gdef_var_store = (
                    getattr(font["GDEF"].table, "VarStore", None)
                    if "GDEF" in font
                    else None
                )
                invalid_gpos_var_indices = []
                if gdef_var_store:
                    for device in gpos_variation_devices:
                        var_idx = (int(device.StartSize) << 16) | int(device.EndSize)
                        major = var_idx >> 16
                        minor = var_idx & 0xFFFF
                        if (
                            major >= len(gdef_var_store.VarData)
                            or minor >= len(gdef_var_store.VarData[major].Item)
                        ):
                            invalid_gpos_var_indices.append(var_idx)
                gpos_variation_status = {
                    "ok": not gpos_variation_devices
                    or (gdef_var_store is not None and not invalid_gpos_var_indices),
                    "device_count": len(gpos_variation_devices),
                    "feature_device_counts": gpos_variation_feature_counts,
                    "gdef_var_store": gdef_var_store is not None,
                    "invalid_var_indices": sorted(set(invalid_gpos_var_indices)),
                }
                digit_width_instances: dict[str, dict[str, Any]] = {}
                digit_width_static_parity: dict[str, dict[str, Any]] = {}
                for weight_name, weight_value in EXPECTED_WEIGHTS.items():
                    key = str(weight_value)
                    variable_digits = digit_width_status(path, {"wght": weight_value})
                    digit_width_instances[key] = variable_digits
                    static_digit_path = static_path(region, weight_name, italic, False)
                    if not static_digit_path.exists():
                        digit_width_static_parity[key] = {
                            "ok": False,
                            "missing_static": display_path(static_digit_path),
                        }
                        continue
                    digit_width_static_parity[key] = digit_width_parity_status(
                        variable_digits,
                        digit_width_status(static_digit_path),
                    )
                item = {
                    "file": display_path(path),
                    "vendor": font["OS/2"].achVendID,
                    "codepage_range_1": int(font["OS/2"].ulCodePageRange1),
                    "head_font_revision": float(font["head"].fontRevision),
                    "name_id_5": version_strings,
                    "legal_names": legal_name_status(font),
                    "mac_name_records": sum(
                        record.platformID == 1 for record in font["name"].names
                    ),
                    "axes": axes(font),
                    "instances": instance_weights,
                    "instance_names": instance_names,
                    "expected_instance_names": expected_instance_names,
                    "axis_mapping": vf_axis_mapping_status(font, region),
                    "stat_weight": stat_weight_status(font),
                    "has_fvar": "fvar" in font,
                    "has_gvar": "gvar" in font,
                    "has_stat": "STAT" in font,
                    "invalid_name_id_25": invalid_name_id_25,
                    "missing_referenced_name_ids": missing_name_ids,
                    "gpos_variation_store": gpos_variation_status,
                    "uppercase_ui_spelling": name_hits,
                    "colon": colon_status(path),
                    "digit_width_instances": digit_width_instances,
                    "digit_width_static_parity": digit_width_static_parity,
                    "contextual_spacing": contextual_spacing_status(path, {"wght": 400}),
                    "underline_instances": {
                        str(weight): vf_underline_metrics(font, weight)
                        for weight in b.STATIC_UNDERLINE_METRICS
                    },
                }
                variable_count += 1
                if len(samples["variable"]) < 4:
                    samples["variable"].append(item)
                if item["vendor"] != b.OS2_VENDOR_ID:
                    failures.append({"kind": "vf_vendor", "file": item["file"], "got": item["vendor"]})
                if item["codepage_range_1"] != b.OS2_CODEPAGE_RANGE_1:
                    failures.append(
                        {
                            "kind": "vf_codepage_range_1",
                            "file": item["file"],
                            "got": item["codepage_range_1"],
                            "expected": b.OS2_CODEPAGE_RANGE_1,
                        }
                    )
                if not item["legal_names"]["ok"]:
                    failures.append({"kind": "vf_legal_names", "file": item["file"], "status": item["legal_names"]})
                if item["mac_name_records"]:
                    failures.append(
                        {
                            "kind": "vf_mac_name_records",
                            "file": item["file"],
                            "count": item["mac_name_records"],
                        }
                    )
                if abs(item["head_font_revision"] - b.FONT_REVISION) > FONT_REVISION_TOLERANCE:
                    failures.append(
                        {
                            "kind": "vf_head_font_revision",
                            "file": item["file"],
                            "got": item["head_font_revision"],
                            "expected": b.FONT_REVISION,
                        }
                    )
                if not project_name_versions_match(version_strings):
                    failures.append({"kind": "vf_name_version", "file": item["file"], "name_id_5": version_strings})
                if item["axes"] != EXPECTED_AXIS:
                    failures.append({"kind": "vf_axis", "file": item["file"], "got": item["axes"]})
                if item["instances"] != list(EXPECTED_WEIGHTS.values()):
                    failures.append({"kind": "vf_instances", "file": item["file"], "got": item["instances"]})
                if item["instance_names"] != expected_instance_names:
                    failures.append(
                        {
                            "kind": "vf_instance_names",
                            "file": item["file"],
                            "got": item["instance_names"],
                            "expected": expected_instance_names,
                        }
                    )
                if not item["axis_mapping"]["ok"]:
                    failures.append(
                        {
                            "kind": "vf_axis_mapping",
                            "file": item["file"],
                            "status": item["axis_mapping"],
                        }
                    )
                if not item["stat_weight"]["ok"]:
                    failures.append(
                        {
                            "kind": "vf_stat_weight",
                            "file": item["file"],
                            "status": item["stat_weight"],
                        }
                    )
                if not item["has_fvar"] or not item["has_gvar"] or not item["has_stat"]:
                    failures.append({"kind": "vf_tables", "file": item["file"], "item": item})
                if invalid_name_id_25:
                    failures.append({"kind": "vf_name_id_25", "file": item["file"], "values": invalid_name_id_25})
                if missing_name_ids:
                    failures.append({"kind": "vf_missing_referenced_name_ids", "file": item["file"], "name_ids": missing_name_ids})
                if not gpos_variation_status["ok"]:
                    failures.append(
                        {
                            "kind": "vf_gpos_variation_store",
                            "file": item["file"],
                            "status": gpos_variation_status,
                        }
                    )
                for weight, expected_underline in b.STATIC_UNDERLINE_METRICS.items():
                    underline = item["underline_instances"][str(weight)]
                    expected = {
                        "thickness": expected_underline[0],
                        "position": expected_underline[1],
                        "record_tags": ["undo", "unds"],
                    }
                    actual = {
                        "thickness": underline["thickness"],
                        "position": underline["position"],
                        "record_tags": underline["record_tags"],
                    }
                    if actual != expected:
                        failures.append(
                            {
                                "kind": "vf_underline_mvar",
                                "file": item["file"],
                                "weight": weight,
                                "got": actual,
                                "expected": expected,
                            }
                        )
                if name_hits:
                    failures.append({"kind": "uppercase_ui_spelling", "file": item["file"], "samples": name_hits})
                if not item["colon"]["ok"]:
                    failures.append({"kind": "colon_shape", "file": item["file"], "colon": item["colon"]})
                bad_digit_instances = {
                    weight: status
                    for weight, status in item["digit_width_instances"].items()
                    if not status["ok"]
                }
                if bad_digit_instances:
                    failures.append(
                        {
                            "kind": "vf_digit_width_features",
                            "file": item["file"],
                            "instances": bad_digit_instances,
                        }
                    )
                bad_digit_parity = {
                    weight: status
                    for weight, status in item["digit_width_static_parity"].items()
                    if not status["ok"]
                }
                if bad_digit_parity:
                    failures.append(
                        {
                            "kind": "vf_static_digit_width_parity",
                            "file": item["file"],
                            "instances": bad_digit_parity,
                        }
                    )
                if not item["contextual_spacing"]["ok"]:
                    failures.append(
                        {
                            "kind": "vf_contextual_spacing_default",
                            "file": item["file"],
                            "contextual_spacing": item["contextual_spacing"],
                        }
                    )
            finally:
                font.close()
    return {"failures": failures, "static_count": static_count, "variable_count": variable_count, "samples": samples}


def audit_static_exact() -> list[dict[str, Any]]:
    out = []
    total = len(b.REGION_ORDER) * 2 * 2 * len(EXACT_WEIGHTS)
    done = 0
    for region in b.REGION_ORDER:
        for hinted in [False, True]:
            for italic in [False, True]:
                for weight in EXACT_WEIGHTS:
                    done += 1
                    target_path = static_path(region, weight, italic, hinted)
                    extension_weight = weight not in UPSTREAM_EXACT_WEIGHTS
                    ref_path = static_reference_path(region, weight, italic, hinted)
                    log(f"static exact {done}/{total}: {region} {weight}{' Italic' if italic else ''} {'hinted' if hinted else 'unhinted'}")
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "reference": display_path(ref_path),
                        "comparison_mode": (
                            "official-boundary-cmap-only"
                            if extension_weight
                            else "official-exact"
                        ),
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                    else:
                        target = TTFont(target_path)
                        reference = TTFont(ref_path)
                        try:
                            if extension_weight:
                                target_codepoints = set(target.getBestCmap() or {})
                                reference_codepoints = set(reference.getBestCmap() or {})
                                missing_target = sorted(reference_codepoints - target_codepoints)
                                extra_target = sorted(target_codepoints - reference_codepoints)
                                item.update(
                                    {
                                        "counts": {
                                            "missing_target": len(missing_target),
                                            "missing_reference": len(extra_target),
                                        },
                                        "coverage": {
                                            "codepoints_total": len(
                                                target_codepoints | reference_codepoints
                                            ),
                                            "codepoints_boundary_compared": len(
                                                target_codepoints & reference_codepoints
                                            ),
                                        },
                                        "samples": {
                                            **(
                                                {
                                                    "missing_target": [
                                                        f"U+{codepoint:04X}"
                                                        for codepoint in missing_target[:5]
                                                    ]
                                                }
                                                if missing_target
                                                else {}
                                            ),
                                            **(
                                                {
                                                    "missing_reference": [
                                                        f"U+{codepoint:04X}"
                                                        for codepoint in extra_target[:5]
                                                    ]
                                                }
                                                if extra_target
                                                else {}
                                            ),
                                        },
                                    }
                                )
                                out.append(item)
                                continue
                            skip_codepoints = (
                                classical_override_codepoints(region, weight)
                            )
                            if skip_codepoints:
                                item["classical_source_exception"] = {
                                    "reason": f"CL 跟随 Shanggu Sans {b.SHANGGU_TAG} 官方静态 TTF/VF，不再以 Sarasa {b.SARASA_VERSION} 内置旧 subset 为 exact 轮廓基线。",
                                    "codepoints": len(skip_codepoints),
                                }
                            item.update(
                                compare_fonts(
                                    target,
                                    reference,
                                    compare_glyphs=True,
                                    skip_codepoints=skip_codepoints,
                                    dedicated_feature_codepoints=STATIC_REFERENCE_DASH_EXCEPTIONS,
                                )
                            )
                        finally:
                            target.close()
                            reference.close()
                    out.append(item)
    return out


def glyph_set_outline_signature(glyph_set: Any, glyph_name: str) -> tuple[Any, ...]:
    pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    return tuple((operator, tuple(points)) for operator, points in pen.value)


def audit_static_extension_sources() -> list[dict[str, Any]]:
    extension_weights = [
        weight for weight in EXACT_WEIGHTS if weight not in UPSTREAM_EXACT_WEIGHTS
    ]
    out: list[dict[str, Any]] = []
    total = len(b.REGION_ORDER) * 2 * 2 * len(extension_weights)
    done = 0
    excluded = INTENTIONAL_CPS | STATIC_REFERENCE_DASH_EXCEPTIONS
    for region in b.REGION_ORDER:
        for weight in extension_weights:
            for italic in (False, True):
                source_path = static_extension_raw_reference_path(
                    region,
                    weight,
                    italic,
                )
                for hinted in (False, True):
                    done += 1
                    target_path = static_path(region, weight, italic, hinted)
                    log(
                        "static extension source "
                        f"{done}/{total}: {region} {weight}"
                        f"{' Italic' if italic else ''} "
                        f"{'hinted' if hinted else 'unhinted'}"
                    )
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "source": display_path(source_path),
                        "comparison_mode": "sarasa-pass2-extension-source",
                        "counts": {
                            "missing_source": 0,
                            "h_advance": 0,
                            "h_lsb": 0,
                            "v_advance": 0,
                            "v_side_bearing": 0,
                            "outline": 0,
                        },
                        "samples": {},
                    }
                    if not target_path.exists() or not source_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    target = TTFont(target_path)
                    source = TTFont(source_path)
                    try:
                        target_cmap = target.getBestCmap() or {}
                        source_cmap = source.getBestCmap() or {}
                        codepoints = sorted(set(target_cmap) - excluded)
                        target_set = target.getGlyphSet()
                        source_set = source.getGlyphSet()
                        item["coverage"] = {
                            "target_codepoints": len(target_cmap),
                            "source_codepoints": len(source_cmap),
                            "codepoints_compared": len(codepoints),
                            "product_exceptions": len(set(target_cmap) & INTENTIONAL_CPS),
                            "dedicated_dash_exceptions": len(
                                set(target_cmap) & STATIC_REFERENCE_DASH_EXCEPTIONS
                            ),
                        }
                        for codepoint in codepoints:
                            target_name = target_cmap[codepoint]
                            source_name = source_cmap.get(codepoint)
                            if source_name is None:
                                add_counted_sample(
                                    item,
                                    "missing_source",
                                    f"U+{codepoint:04X}",
                                    limit=8,
                                )
                                continue
                            target_h = target["hmtx"].metrics.get(target_name)
                            source_h = source["hmtx"].metrics.get(source_name)
                            if target_h is not None and source_h is not None:
                                for key, index in (("h_advance", 0), ("h_lsb", 1)):
                                    if target_h[index] != source_h[index]:
                                        add_counted_sample(
                                            item,
                                            key,
                                            [
                                                f"U+{codepoint:04X}",
                                                target_h[index],
                                                source_h[index],
                                            ],
                                            limit=8,
                                        )
                            target_v = target["vmtx"].metrics.get(target_name)
                            source_v = source["vmtx"].metrics.get(source_name)
                            if target_v is not None and source_v is not None:
                                for key, index in (
                                    ("v_advance", 0),
                                    ("v_side_bearing", 1),
                                ):
                                    if target_v[index] != source_v[index]:
                                        add_counted_sample(
                                            item,
                                            key,
                                            [
                                                f"U+{codepoint:04X}",
                                                target_v[index],
                                                source_v[index],
                                            ],
                                            limit=8,
                                        )
                            if glyph_set_outline_signature(
                                target_set,
                                target_name,
                            ) != glyph_set_outline_signature(source_set, source_name):
                                add_counted_sample(
                                    item,
                                    "outline",
                                    f"U+{codepoint:04X}",
                                    limit=8,
                                )
                    finally:
                        source.close()
                        target.close()
                    out.append(item)
    return out


def audit_static_cl_source_outlines() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = len(EXPECTED_WEIGHTS) * 2 * 2
    done = 0
    for weight in EXPECTED_WEIGHTS:
        source_path = b.classical_static_override_path("CL", weight)
        reference_path = b.reference_font_path("CL", weight, False)
        has_exact_reference = reference_path.exists()
        exceptions = (
            classical_override_codepoints("CL", weight)
            if has_exact_reference
            else None
        )
        for hinted in (False, True):
            for italic in (False, True):
                done += 1
                target_path = static_path("CL", weight, italic, hinted)
                log(
                    f"CL Shanggu outline {done}/{total}: {weight}"
                    f"{' Italic' if italic else ''} {'hinted' if hinted else 'unhinted'}"
                )
                item: dict[str, Any] = {
                    "region": "CL",
                    "weight": weight,
                    "italic": italic,
                    "hinted": hinted,
                    "target": display_path(target_path),
                    "source": display_path(source_path) if source_path else None,
                    "source_transform": "9.4 degree shear plus whole-glyph translation"
                    if italic
                    else "whole-glyph translation",
                    "coverage_mode": "official_exact_exclusions"
                    if has_exact_reference
                    else "all_shared_source_ideographs",
                    "outline_tolerance_font_units": CJK_SHEAR_POINT_TOLERANCE if italic else 0.001,
                    "counts": {
                        "missing_target": 0,
                        "missing_source": 0,
                        "outline_measurement_error": 0,
                        "source_outline_mismatch": 0,
                        "source_area_mismatch": 0,
                    },
                    "coverage": {
                        "classical_exceptions_expected": len(exceptions or ()),
                        "classical_exceptions_compared": 0,
                    },
                    "samples": {},
                }
                if source_path is None or not source_path.exists() or not target_path.exists():
                    item["missing"] = True
                    out.append(item)
                    continue

                source = TTFont(source_path)
                target = TTFont(target_path)
                mismatch_samples: list[dict[str, Any]] = []
                area_samples: list[dict[str, Any]] = []
                error_samples: list[list[str]] = []
                missing_target_samples: list[str] = []
                missing_source_samples: list[str] = []
                maximum_residual = 0.0
                try:
                    source_cmap = source.getBestCmap() or {}
                    target_cmap = target.getBestCmap() or {}
                    source_set = source.getGlyphSet()
                    target_set = target.getGlyphSet()
                    angle = CJK_ITALIC_ANGLE_DEGREES if italic else 0.0
                    tolerance = CJK_SHEAR_POINT_TOLERANCE if italic else 0.001
                    audit_codepoints = (
                        set(exceptions)
                        if exceptions is not None
                        else {
                            codepoint
                            for codepoint in set(source_cmap) & set(target_cmap)
                            if b.is_ideograph(codepoint)
                        }
                    )
                    item["coverage"]["classical_exceptions_expected"] = len(audit_codepoints)
                    for codepoint in sorted(audit_codepoints):
                        source_glyph = source_cmap.get(codepoint)
                        target_glyph = target_cmap.get(codepoint)
                        if source_glyph is None:
                            item["counts"]["missing_source"] += 1
                            if len(missing_source_samples) < 12:
                                missing_source_samples.append(f"U+{codepoint:04X}")
                            continue
                        if target_glyph is None:
                            item["counts"]["missing_target"] += 1
                            if len(missing_target_samples) < 12:
                                missing_target_samples.append(f"U+{codepoint:04X}")
                            continue
                        try:
                            source_area, source_recording = glyph_area_and_recording(
                                source_set,
                                source_glyph,
                            )
                            target_area, target_recording = glyph_area_and_recording(
                                target_set,
                                target_glyph,
                            )
                        except Exception as error:
                            item["counts"]["outline_measurement_error"] += 1
                            if len(error_samples) < 12:
                                error_samples.append(
                                    [f"U+{codepoint:04X}", type(error).__name__, str(error)]
                                )
                            continue
                        item["coverage"]["classical_exceptions_compared"] += 1
                        reason, residual = transformed_outline_residual(
                            source_recording,
                            target_recording,
                            angle,
                        )
                        if math.isfinite(residual):
                            maximum_residual = max(maximum_residual, residual)
                        if reason is not None or residual > tolerance:
                            item["counts"]["source_outline_mismatch"] += 1
                            if len(mismatch_samples) < 20:
                                mismatch_samples.append(
                                    {
                                        "codepoint": f"U+{codepoint:04X}",
                                        "reason": reason or "point_residual",
                                        "maximum_residual": round(residual, 6)
                                        if math.isfinite(residual)
                                        else None,
                                    }
                                )
                        if exceeds_cjk_area_tolerance(source_area, target_area):
                            item["counts"]["source_area_mismatch"] += 1
                            if len(area_samples) < 20:
                                area_samples.append(
                                    {
                                        "codepoint": f"U+{codepoint:04X}",
                                        "source_area": round(source_area, 3),
                                        "target_area": round(target_area, 3),
                                    }
                                )
                finally:
                    source.close()
                    target.close()
                item["maximum_outline_residual"] = round(maximum_residual, 6)
                if mismatch_samples:
                    item["samples"]["source_outline_mismatch"] = mismatch_samples
                if area_samples:
                    item["samples"]["source_area_mismatch"] = area_samples
                if error_samples:
                    item["samples"]["outline_measurement_error"] = error_samples
                if missing_target_samples:
                    item["samples"]["missing_target"] = missing_target_samples
                if missing_source_samples:
                    item["samples"]["missing_source"] = missing_source_samples
                out.append(item)
    return out


def audit_static_cl_boundaries() -> list[dict[str, Any]]:
    out = []
    total = 2 * 2 * len(EXPECTED_WEIGHTS)
    done = 0
    for hinted in [False, True]:
        for weight_name in EXPECTED_WEIGHTS:
            for italic in [False, True]:
                done += 1
                target_path = static_path("CL", weight_name, italic, hinted)
                ref_path = b.static_reference_font_path("CL", weight_name, italic)
                extension_weight = weight_name not in UPSTREAM_EXACT_WEIGHTS
                advance_ref_path = (
                    static_extension_raw_reference_path(
                        "CL",
                        weight_name,
                        italic,
                    )
                    if extension_weight
                    else ref_path
                )
                log(
                    "CL static boundary "
                    f"{done}/{total}: {weight_name}{' Italic' if italic else ''} "
                    f"{'hinted' if hinted else 'unhinted'}"
                )
                item: dict[str, Any] = {
                    "region": "CL",
                    "weight": weight_name,
                    "reference_style": b.static_reference_style_name(weight_name, italic),
                    "italic": italic,
                    "hinted": hinted,
                    "target": display_path(target_path),
                    "reference": display_path(ref_path),
                    "advance_reference": display_path(advance_ref_path),
                    "advance_reference_mode": (
                        "sarasa-pass2-extension-source"
                        if extension_weight
                        else "official-sarasa-exact"
                    ),
                    "counts": {
                        "extra_cmap": 0,
                        "missing_cmap": 0,
                        "extra_gsub_features": 0,
                        "extra_gpos_features": 0,
                        "advance_mismatch": 0,
                    },
                    "samples": {},
                }
                if (
                    not target_path.exists()
                    or not ref_path.exists()
                    or not advance_ref_path.exists()
                ):
                    item["missing"] = True
                    out.append(item)
                    continue
                target = TTFont(target_path)
                reference = TTFont(ref_path)
                advance_reference = (
                    TTFont(advance_ref_path) if advance_ref_path != ref_path else reference
                )
                try:
                    target_cmap = target.getBestCmap() or {}
                    reference_cmap = reference.getBestCmap() or {}
                    advance_reference_cmap = advance_reference.getBestCmap() or {}
                    extra_cmap = sorted(set(target_cmap) - set(reference_cmap))
                    missing_cmap = sorted(set(reference_cmap) - set(target_cmap))
                    extra_gsub = sorted(layout_feature_tags(target, "GSUB") - layout_feature_tags(reference, "GSUB"))
                    intentional_dash_gpos = {
                        tag
                        for tag, records in b.vertical_em_dash_positioning_records(target).items()
                        if records
                    }
                    extra_gpos = sorted(
                        layout_feature_tags(target, "GPOS")
                        - layout_feature_tags(reference, "GPOS")
                        - CONTEXTUAL_SPACING_FEATURES
                        - intentional_dash_gpos
                    )
                    item["counts"]["extra_cmap"] = len(extra_cmap)
                    item["counts"]["missing_cmap"] = len(missing_cmap)
                    item["counts"]["extra_gsub_features"] = len(extra_gsub)
                    item["counts"]["extra_gpos_features"] = len(extra_gpos)
                    if extra_cmap:
                        item["samples"]["extra_cmap"] = [f"U+{cp:04X}" for cp in extra_cmap[:8]]
                    if missing_cmap:
                        item["samples"]["missing_cmap"] = [f"U+{cp:04X}" for cp in missing_cmap[:8]]
                    if extra_gsub:
                        item["samples"]["extra_gsub_features"] = extra_gsub[:8]
                    if extra_gpos:
                        item["samples"]["extra_gpos_features"] = extra_gpos[:8]
                    advance_samples = []
                    for codepoint in CL_BOUNDARY_ADVANCE_CODEPOINTS:
                        target_glyph = target_cmap.get(codepoint)
                        reference_glyph = advance_reference_cmap.get(codepoint)
                        if not target_glyph or not reference_glyph:
                            continue
                        target_h = target["hmtx"].metrics.get(target_glyph)
                        reference_h = advance_reference["hmtx"].metrics.get(
                            reference_glyph
                        )
                        if target_h and reference_h and target_h[0] != reference_h[0]:
                            item["counts"]["advance_mismatch"] += 1
                            advance_samples.append([f"U+{codepoint:04X}", target_h[0], reference_h[0]])
                    if advance_samples:
                        item["samples"]["advance_mismatch"] = advance_samples
                finally:
                    target.close()
                    if advance_reference is not reference:
                        advance_reference.close()
                    reference.close()
                out.append(item)
    return out


def audit_vf_metrics() -> list[dict[str, Any]]:
    out = []
    weights = list(b.REFERENCE_ADVANCE_STOPS)
    regions = b.variable_regions(b.REGION_ORDER)
    total = len(regions) * 2 * len(weights)
    done = 0
    for region in regions:
        for italic in [False, True]:
            variable_path = vf_path(region, italic)
            variable = TTFont(variable_path) if variable_path.exists() else None
            try:
                for weight_name, weight_value in weights:
                    done += 1
                    ref_path = b.reference_font_path(region, weight_name, italic)
                    log(
                        f"VF exact metrics {done}/{total}: {region} {weight_name}"
                        f"{' Italic' if italic else ''}"
                    )
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight_name,
                        "wght": weight_value,
                        "italic": italic,
                        "target": display_path(variable_path),
                        "reference": display_path(ref_path),
                    }
                    if variable is None or not ref_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    reference = TTFont(ref_path)
                    instance: TTFont | None = None
                    try:
                        instance = instantiateVariableFont(
                            variable,
                            {"wght": weight_value},
                            inplace=False,
                            optimize=True,
                        )
                        skip_codepoints = classical_override_codepoints(region, weight_name)
                        if skip_codepoints:
                            item["classical_source_exception"] = {
                                "reason": f"CL 跟随 Shanggu Sans {b.SHANGGU_TAG} 官方静态 TTF/VF，不再以 Sarasa {b.SARASA_VERSION} 内置旧 subset 为 exact 轮廓基线。",
                                "codepoints": len(skip_codepoints),
                            }
                        item.update(
                            compare_fonts(
                                instance,
                                reference,
                                compare_glyphs=False,
                                skip_codepoints=skip_codepoints,
                                dedicated_feature_codepoints=STATIC_REFERENCE_DASH_EXCEPTIONS,
                            )
                        )
                    finally:
                        if instance is not None:
                            instance.close()
                        reference.close()
                    out.append(item)
            finally:
                if variable is not None:
                    variable.close()
    return out


def compare_source_pairing_codepoints(
    target: TTFont,
    source: TTFont,
    codepoints: list[int],
    *,
    translation_tolerance: float | None = None,
    hmtx_is_failure: bool = True,
    follow_target_cmap_aliases: bool = False,
) -> dict[str, Any]:
    target_cmap = target.getBestCmap() or {}
    source_cmap = source.getBestCmap() or {}
    counts = {"missing": 0, "outline": 0, "hmtx": 0}
    samples: dict[str, list[Any]] = {key: [] for key in counts}
    observations = {"hmtx_differences": 0}
    observation_samples: list[Any] = []
    compared = 0
    maximum_outline_residual = 0.0
    target_set = target.getGlyphSet() if translation_tolerance is not None else None
    source_set = source.getGlyphSet() if translation_tolerance is not None else None
    target_alias_groups: dict[str, list[int]] = {}
    if follow_target_cmap_aliases:
        if translation_tolerance is None:
            raise ValueError("target cmap alias source matching requires translation_tolerance")
        for codepoint in codepoints:
            glyph_name = target_cmap.get(codepoint)
            if glyph_name is not None:
                target_alias_groups.setdefault(glyph_name, []).append(codepoint)
    alias_source_glyphs: dict[str, str] = {}
    for codepoint in codepoints:
        target_glyph = target_cmap.get(codepoint)
        source_glyph = source_cmap.get(codepoint)
        if target_glyph is not None and follow_target_cmap_aliases:
            source_glyph = alias_source_glyphs.get(target_glyph, source_glyph)
            if target_glyph not in alias_source_glyphs:
                alias_group = target_alias_groups.get(target_glyph, [])
                candidate_glyphs = {
                    source_cmap[candidate]
                    for candidate in alias_group
                    if candidate in source_cmap
                }
                if len(candidate_glyphs) == 1:
                    source_glyph = next(iter(candidate_glyphs))
                    alias_source_glyphs[target_glyph] = source_glyph
                    candidate_glyphs = set()
                best: tuple[float, str] | None = None
                for candidate_glyph in candidate_glyphs:
                    try:
                        _target_area, target_recording = glyph_area_and_recording(
                            target_set,
                            target_glyph,
                        )
                        _source_area, source_recording = glyph_area_and_recording(
                            source_set,
                            candidate_glyph,
                        )
                        reason, residual = transformed_outline_residual(
                            source_recording,
                            target_recording,
                            0.0,
                        )
                        score = residual if reason is None else float("inf")
                    except Exception:
                        score = float("inf")
                    candidate = (score, candidate_glyph)
                    if best is None or candidate < best:
                        best = candidate
                if best is not None:
                    source_glyph = best[1]
                    alias_source_glyphs[target_glyph] = source_glyph
        if target_glyph is None or source_glyph is None:
            counts["missing"] += 1
            if len(samples["missing"]) < 8:
                samples["missing"].append(f"U+{codepoint:04X}")
            continue
        compared += 1
        outline_mismatch = False
        accepted_outline_residual: float | None = None
        if translation_tolerance is None:
            outline_mismatch = (
                decomposed_outline_signature(target, target_glyph)
                != decomposed_outline_signature(source, source_glyph)
            )
        else:
            try:
                _target_area, target_recording = glyph_area_and_recording(target_set, target_glyph)
                _source_area, source_recording = glyph_area_and_recording(source_set, source_glyph)
                reason, residual = transformed_outline_residual(
                    source_recording,
                    target_recording,
                    0.0,
                )
                accepted_outline_residual = residual
                outline_mismatch = reason is not None or residual > translation_tolerance
            except Exception:
                outline_mismatch = True
        if outline_mismatch:
            counts["outline"] += 1
            if len(samples["outline"]) < 8:
                samples["outline"].append(f"U+{codepoint:04X}")
        elif accepted_outline_residual is not None and math.isfinite(accepted_outline_residual):
            maximum_outline_residual = max(
                maximum_outline_residual,
                accepted_outline_residual,
            )
        target_hmtx = target["hmtx"].metrics.get(target_glyph)
        source_hmtx = source["hmtx"].metrics.get(source_glyph)
        if target_hmtx != source_hmtx:
            if hmtx_is_failure:
                counts["hmtx"] += 1
                if len(samples["hmtx"]) < 8:
                    samples["hmtx"].append([f"U+{codepoint:04X}", target_hmtx, source_hmtx])
            else:
                observations["hmtx_differences"] += 1
                if len(observation_samples) < 8:
                    observation_samples.append(
                        [f"U+{codepoint:04X}", target_hmtx, source_hmtx]
                    )
    return {
        "compared": compared,
        "counts": counts,
        "samples": {key: value for key, value in samples.items() if value},
        "observations": observations,
        "observation_samples": {"hmtx_differences": observation_samples}
        if observation_samples
        else {},
        **(
            {"maximum_translation_invariant_outline_residual": maximum_outline_residual}
            if translation_tolerance is not None
            else {}
        ),
    }


def cmap_alias_partition_status(
    target: TTFont,
    reference: TTFont,
    codepoints: list[int],
) -> dict[str, Any]:
    target_cmap = target.getBestCmap() or {}
    reference_cmap = reference.getBestCmap() or {}
    shared = sorted(set(codepoints) & set(target_cmap) & set(reference_cmap))

    def memberships(cmap: dict[int, str]) -> dict[int, tuple[int, ...]]:
        groups: dict[str, list[int]] = {}
        for codepoint in shared:
            groups.setdefault(cmap[codepoint], []).append(codepoint)
        return {
            codepoint: tuple(group)
            for group in groups.values()
            for codepoint in group
        }

    target_memberships = memberships(target_cmap)
    reference_memberships = memberships(reference_cmap)
    mismatches = [
        codepoint
        for codepoint in shared
        if target_memberships[codepoint] != reference_memberships[codepoint]
    ]
    return {
        "ok": not mismatches,
        "codepoints_compared": len(shared),
        "mismatches": len(mismatches),
        "samples": [
            {
                "codepoint": f"U+{codepoint:04X}",
                "target_group": [f"U+{value:04X}" for value in target_memberships[codepoint]],
                "reference_group": [f"U+{value:04X}" for value in reference_memberships[codepoint]],
            }
            for codepoint in mismatches[:12]
        ],
    }


def audit_vf_source_pairing_serial(
    selected: tuple[str, bool] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    regions = [selected[0]] if selected else b.variable_regions(b.REGION_ORDER)
    total = 1 if selected else len(regions) * 2
    done = 0
    for region in regions:
        for italic in (False, True):
            if selected and italic != selected[1]:
                continue
            done += 1
            path = vf_path(region, italic)
            log(f"VF source pairing {done}/{total}: {region}{' Italic' if italic else ''}")
            item: dict[str, Any] = {
                "region": region,
                "italic": italic,
                "public_wght": 600,
                "source_han_internal_wght": 500,
                "inter_internal_wght": 600,
                "target": display_path(path),
            }
            target = TTFont(path)
            source_axis = TTFont(b.source_han_vf_path(region))
            inter = b.load_inter(italic)
            source_base: TTFont | None = None
            target_instance: TTFont | None = None
            inter_instance: TTFont | None = None
            source_instance: TTFont | None = None
            limited_source_axis: TTFont | None = None
            reference: TTFont | None = None
            try:
                limited_source_axis = instantiateVariableFont(
                    source_axis,
                    b.AXIS_LIMIT,
                    inplace=False,
                    optimize=True,
                )
                actual_coordinate = b.vf_mapped_normalized_weight(target, 600)
                expected_coordinate = b.normalized_wght(limited_source_axis, 500)
                item["source_han_normalized_coordinate"] = {
                    "actual": actual_coordinate,
                    "expected": expected_coordinate,
                    "delta": actual_coordinate - expected_coordinate,
                    "ok": abs(actual_coordinate - expected_coordinate) <= 1 / 16384,
                }
                limited_source_axis.close()
                limited_source_axis = None
                source_axis.close()
                source_axis = None

                source_base, _source_report = b.load_base(region, italic, set(inter.getBestCmap()))
                target_instance = instantiateVariableFont(target, {"wght": 600}, inplace=False, optimize=True)
                inter_instance = instantiateVariableFont(inter, {"wght": 600}, inplace=False, optimize=True)
                source_instance = instantiateVariableFont(source_base, {"wght": 600}, inplace=False, optimize=True)
                target.close()
                target = None
                inter.close()
                inter = None
                source_base.close()
                source_base = None
                reference = TTFont(b.reference_font_path(region, "SemiBold", italic))
                target_cmap = target_instance.getBestCmap() or {}
                source_cmap = source_instance.getBestCmap() or {}
                inter_cmap = inter_instance.getBestCmap() or {}
                reference_cmap = reference.getBestCmap() or {}
                inter_codepoints = sorted(
                    codepoint
                    for codepoint in target_cmap
                    if codepoint in inter_cmap
                    and b.use_inter_codepoint(codepoint)
                    and codepoint not in INTENTIONAL_CPS
                )
                inter_source_partition_overlap = sorted(set(inter_codepoints) & set(source_cmap))
                cjk_codepoints = sorted(
                    codepoint
                    for codepoint, glyph_name in target_cmap.items()
                    if b.is_ideograph(codepoint)
                    and not glyph_name.startswith(b.INTER_PREFIX)
                    and codepoint in source_cmap
                )
                inter_pairing = compare_source_pairing_codepoints(
                    target_instance,
                    inter_instance,
                    inter_codepoints,
                    translation_tolerance=CJK_SHEAR_POINT_TOLERANCE,
                    hmtx_is_failure=False,
                    follow_target_cmap_aliases=True,
                )
                cjk_pairing = compare_source_pairing_codepoints(
                    target_instance,
                    source_instance,
                    cjk_codepoints,
                    translation_tolerance=CJK_SHEAR_POINT_TOLERANCE,
                    hmtx_is_failure=False,
                    follow_target_cmap_aliases=True,
                )
                alias_codepoints = sorted(
                    (set(target_cmap) & set(reference_cmap))
                    - INTENTIONAL_CPS
                    - STATIC_REFERENCE_DASH_EXCEPTIONS
                )
                sarasa_alias_mapping = cmap_alias_partition_status(
                    target_instance,
                    reference,
                    alias_codepoints,
                )
                item["inter_600"] = inter_pairing
                item["cjk_source_han_500"] = cjk_pairing
                item["sarasa_alias_mapping"] = sarasa_alias_mapping
                item["coverage"] = {
                    "inter_cmap_codepoints": len(inter_codepoints),
                    "propdigits_codepoints_separately_audited": len(
                        set(target_cmap) & set(inter_cmap) & INTENTIONAL_CPS
                    ),
                    "cjk_codepoints": len(cjk_codepoints),
                    "sarasa_alias_codepoints": len(alias_codepoints),
                }
                item["counts"] = {
                    "source_han_coordinate": 0
                    if item["source_han_normalized_coordinate"]["ok"]
                    else 1,
                    "sarasa_alias_mapping": sarasa_alias_mapping["mismatches"],
                    "inter_source_partition_overlap": len(inter_source_partition_overlap),
                    "inter_coverage_empty": 0 if inter_codepoints else 1,
                    "cjk_coverage_empty": 0 if cjk_codepoints else 1,
                    **{
                        f"inter_{key}": value
                        for key, value in inter_pairing["counts"].items()
                    },
                    **{
                        f"cjk_{key}": value
                        for key, value in cjk_pairing["counts"].items()
                    },
                }
                if inter_source_partition_overlap:
                    item.setdefault("samples", {})["inter_source_partition_overlap"] = [
                        f"U+{codepoint:04X}" for codepoint in inter_source_partition_overlap[:12]
                    ]
            finally:
                for font in (
                    source_instance,
                    reference,
                    inter_instance,
                    target_instance,
                    source_base,
                    limited_source_axis,
                    inter,
                    source_axis,
                    target,
                ):
                    if font is not None:
                        font.close()
            out.append(item)
    return out


def audit_vf_source_pairing() -> list[dict[str, Any]]:
    cases = [
        (region, italic)
        for region in b.variable_regions(b.REGION_ORDER)
        for italic in (False, True)
    ]
    worker_count = min(4, os.cpu_count() or 1, len(cases))
    if worker_count == 1:
        return audit_vf_source_pairing_serial()

    results: list[dict[str, Any]] = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(audit_vf_source_pairing_serial, case): case
            for case in cases
        }
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            region, italic = futures[future]
            results.extend(future.result())
            log(
                f"VF source pairing parallel {done}/{len(cases)}: "
                f"{region}{' Italic' if italic else ''}"
            )

    region_order = {region: index for index, region in enumerate(b.REGION_ORDER)}
    return sorted(
        results,
        key=lambda item: (region_order[item["region"]], item["italic"]),
    )


def glyph_area_and_recording(glyph_set: Any, glyph_name: str) -> tuple[float, list[Any]]:
    area_pen = AreaPen(glyph_set)
    recording_pen = DecomposingRecordingPen(glyph_set)
    glyph_set[glyph_name].draw(TeePen(area_pen, recording_pen))
    return abs(float(area_pen.value)), recording_pen.value


def glyph_area_and_stroke_proxy(glyph_set: Any, glyph_name: str) -> tuple[float, float] | None:
    area_pen = AreaPen(glyph_set)
    perimeter_pen = PerimeterPen(glyph_set)
    glyph_set[glyph_name].draw(TeePen(area_pen, perimeter_pen))
    area = abs(float(area_pen.value))
    perimeter = float(perimeter_pen.value)
    if area <= 0 or perimeter <= 0:
        return None
    return area, 2 * area / perimeter


def weight_harmony_cjk_codepoints(cmap: dict[int, str]) -> list[int]:
    start = 0x4E00
    count = 0xA000 - start
    codepoints = [
        start + ((2 * index + 1) * count) // (2 * WEIGHT_HARMONY_CJK_SAMPLES)
        for index in range(WEIGHT_HARMONY_CJK_SAMPLES)
    ]
    return [
        codepoint
        for codepoint in codepoints
        if codepoint in cmap
        and b.is_ideograph(codepoint)
        and not cmap[codepoint].startswith(b.INTER_PREFIX)
    ]


def measure_outline_weight_curve(
    font: TTFont,
    codepoints: list[int],
) -> dict[int, dict[str, float]]:
    cmap = font.getBestCmap() or {}
    glyph_names = [cmap[codepoint] for codepoint in codepoints if codepoint in cmap]
    raw: dict[int, list[tuple[float, float] | None]] = {}
    for weight in WEIGHT_HARMONY_WEIGHTS:
        glyph_set = font.getGlyphSet(location={"wght": weight})
        raw[weight] = [glyph_area_and_stroke_proxy(glyph_set, glyph_name) for glyph_name in glyph_names]

    baseline = raw[400]
    out: dict[int, dict[str, float]] = {}
    for weight, measurements in raw.items():
        area_ratios: list[float] = []
        stroke_ratios: list[float] = []
        areas: list[float] = []
        strokes: list[float] = []
        for current, regular in zip(measurements, baseline):
            if current is None or regular is None:
                continue
            area, stroke = current
            regular_area, regular_stroke = regular
            area_ratios.append(area / regular_area)
            stroke_ratios.append(stroke / regular_stroke)
            areas.append(area)
            strokes.append(stroke)
        out[weight] = {
            "glyphs": len(area_ratios),
            "area_ratio_to_400": float(np.median(area_ratios)),
            "stroke_ratio_to_400": float(np.median(stroke_ratios)),
            "median_area_square_font_units": float(np.median(areas)),
            "median_stroke_font_units": float(np.median(strokes)),
        }
    return out


def weight_harmony_pair(
    latin: dict[int, dict[str, float]],
    cjk: dict[int, dict[str, float]],
    latin_weight: int,
    cjk_weight: int,
) -> dict[str, Any]:
    latin_point = latin[latin_weight]
    cjk_point = cjk[cjk_weight]
    area_gap = latin_point["area_ratio_to_400"] - cjk_point["area_ratio_to_400"]
    stroke_gap = latin_point["stroke_ratio_to_400"] - cjk_point["stroke_ratio_to_400"]
    score = math.hypot(
        math.log(latin_point["area_ratio_to_400"] / cjk_point["area_ratio_to_400"]),
        math.log(latin_point["stroke_ratio_to_400"] / cjk_point["stroke_ratio_to_400"]),
    )
    return {
        "latin_wght": latin_weight,
        "cjk_wght": cjk_weight,
        "latin_area_ratio_to_400": latin_point["area_ratio_to_400"],
        "cjk_area_ratio_to_400": cjk_point["area_ratio_to_400"],
        "area_gap_percentage_points": area_gap * 100,
        "latin_stroke_ratio_to_400": latin_point["stroke_ratio_to_400"],
        "cjk_stroke_ratio_to_400": cjk_point["stroke_ratio_to_400"],
        "stroke_gap_percentage_points": stroke_gap * 100,
        "log_distance": score,
    }


def median_summary(values: list[float]) -> dict[str, float]:
    return {
        "median": float(np.median(values)),
        "minimum": min(values),
        "maximum": max(values),
    }


def summarize_weight_harmony_scheme(
    cases: list[dict[str, Any]],
    scheme_key: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for public_weight in (200, 300, 400, 600, 700, 900):
        points = [case[scheme_key][str(public_weight)] for case in cases]
        out[str(public_weight)] = {
            "area_gap_percentage_points": median_summary(
                [point["area_gap_percentage_points"] for point in points]
            ),
            "stroke_gap_percentage_points": median_summary(
                [point["stroke_gap_percentage_points"] for point in points]
            ),
            "log_distance": median_summary([point["log_distance"] for point in points]),
        }
    return out


def audit_vf_weight_harmony() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    regions = b.variable_regions(b.REGION_ORDER)
    total = len(regions) * 2
    done = 0
    public_weights = (200, 300, 400, 600, 700, 900)
    source_internal_equal_cjk_map = {200: 200, 300: 300, 400: 400, 600: 650, 700: 700, 900: 900}
    for region in regions:
        for italic in (False, True):
            done += 1
            path = vf_path(region, italic)
            log(f"VF weight harmony {done}/{total}: {region}{' Italic' if italic else ''}")
            font = TTFont(path)
            try:
                cmap = font.getBestCmap() or {}
                latin_codepoints = [ord(character) for character in WEIGHT_HARMONY_LATIN_TEXT]
                cjk_codepoints = weight_harmony_cjk_codepoints(cmap)
                latin = measure_outline_weight_curve(font, latin_codepoints)
                cjk = measure_outline_weight_curve(font, cjk_codepoints)
                equal_axis = {
                    str(weight): weight_harmony_pair(latin, cjk, weight, weight)
                    for weight in public_weights
                }
                source_internal_equal = {
                    str(weight): weight_harmony_pair(
                        latin,
                        cjk,
                        weight,
                        source_internal_equal_cjk_map[weight],
                    )
                    for weight in public_weights
                }
                best_cjk: dict[str, Any] = {}
                best_latin: dict[str, Any] = {}
                for public_weight in public_weights:
                    best_cjk[str(public_weight)] = min(
                        (
                            weight_harmony_pair(latin, cjk, public_weight, cjk_weight)
                            for cjk_weight in WEIGHT_HARMONY_WEIGHTS
                        ),
                        key=lambda point: point["log_distance"],
                    )
                    best_latin[str(public_weight)] = min(
                        (
                            weight_harmony_pair(latin, cjk, latin_weight, public_weight)
                            for latin_weight in WEIGHT_HARMONY_WEIGHTS
                        ),
                        key=lambda point: point["log_distance"],
                    )
                cases.append(
                    {
                        "region": region,
                        "italic": italic,
                        "file": display_path(path),
                        "samples": {
                            "latin": len(latin_codepoints),
                            "cjk": len(cjk_codepoints),
                            "cjk_codepoints": [f"U+{codepoint:04X}" for codepoint in cjk_codepoints],
                        },
                        "active_sarasa_mapping": equal_axis,
                        "source_internal_equal": source_internal_equal,
                        "best_cjk_grid_match": best_cjk,
                        "best_latin_grid_match": best_latin,
                        "latin_curve": {str(weight): value for weight, value in latin.items()},
                        "cjk_curve": {str(weight): value for weight, value in cjk.items()},
                    }
                )
            finally:
                font.close()

    best_cjk_summary: dict[str, Any] = {}
    best_latin_summary: dict[str, Any] = {}
    for public_weight in public_weights:
        best_cjk_points = [case["best_cjk_grid_match"][str(public_weight)] for case in cases]
        best_latin_points = [case["best_latin_grid_match"][str(public_weight)] for case in cases]
        best_cjk_summary[str(public_weight)] = {
            "cjk_wght": median_summary([point["cjk_wght"] for point in best_cjk_points]),
            "log_distance": median_summary([point["log_distance"] for point in best_cjk_points]),
        }
        best_latin_summary[str(public_weight)] = {
            "latin_wght": median_summary([point["latin_wght"] for point in best_latin_points]),
            "log_distance": median_summary([point["log_distance"] for point in best_latin_points]),
        }
    return {
        "title": "Sarasa Ui PropDigits 中西文字重协调性审计",
        "method": {
            "weights": list(WEIGHT_HARMONY_WEIGHTS),
            "latin_sample": WEIGHT_HARMONY_LATIN_TEXT,
            "cjk_samples": WEIGHT_HARMONY_CJK_SAMPLES,
            "area_ratio": "每个 glyph 的轮廓有向面积绝对值除以自身 wght=400 面积，再取脚本中位数。",
            "stroke_proxy": "每个 glyph 使用 2×轮廓面积/贝塞尔周长作为有效笔画代理，相对自身 wght=400 后取脚本中位数。",
            "comparison": "gap 为 Latin 相对 400 的增减幅度减去 CJK 相对 400 的增减幅度；正值表示 Latin 相对更重。",
            "normative_status": "设计分析，不预设 pass/fail 阈值。",
        },
        "schemes": {
            "active_sarasa_mapping": {
                "description": "成品的公开同坐标取值；CJK public 200/600 分别对应 Source Han internal 250/500，Inter 始终对应同数字 public 坐标。",
                "summary": summarize_weight_harmony_scheme(cases, "active_sarasa_mapping"),
            },
            "source_internal_equal": {
                "description": "对照方案：让 public 600 的 CJK 取 Source Han internal 600；在当前 avar 下对应 CJK public coordinate 650。",
                "cjk_public_coordinates": {str(key): value for key, value in source_internal_equal_cjk_map.items()},
                "summary": summarize_weight_harmony_scheme(cases, "source_internal_equal"),
            },
        },
        "best_grid_matches": {
            "cjk_for_latin": best_cjk_summary,
            "latin_for_cjk": best_latin_summary,
        },
        "cases": cases,
    }


def curve_nonmonotonic_steps(
    curve: dict[int, dict[str, float]],
    metric: str,
    relative_tolerance: float = 0.003,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    weights = sorted(curve)
    for first_weight, second_weight in zip(weights, weights[1:]):
        first = float(curve[first_weight][metric])
        second = float(curve[second_weight][metric])
        if second + max(abs(first), abs(second), 1e-9) * relative_tolerance < first:
            failures.append(
                {
                    "from_wght": first_weight,
                    "to_wght": second_weight,
                    "from": first,
                    "to": second,
                    "relative_drop": (first - second) / max(abs(first), 1e-9),
                }
            )
    return failures


def inter_axis_outline_status(target: TTFont, inter: TTFont) -> dict[str, Any]:
    target_cmap = target.getBestCmap() or {}
    inter_cmap = inter.getBestCmap() or {}
    expected_codepoints = [ord(character) for character in WEIGHT_HARMONY_LATIN_TEXT]
    missing = [
        codepoint
        for codepoint in expected_codepoints
        if codepoint not in target_cmap or codepoint not in inter_cmap
    ]
    codepoints = [codepoint for codepoint in expected_codepoints if codepoint not in missing]
    mismatches: list[dict[str, Any]] = []
    errors: list[list[Any]] = []
    maximum_residual = 0.0
    compared = 0
    mismatch_count = 0
    for weight in WEIGHT_HARMONY_WEIGHTS:
        target_set = target.getGlyphSet(location={"wght": weight})
        inter_set = inter.getGlyphSet(location={"wght": weight})
        for codepoint in codepoints:
            try:
                _target_area, target_recording = glyph_area_and_recording(
                    target_set,
                    target_cmap[codepoint],
                )
                _inter_area, inter_recording = glyph_area_and_recording(
                    inter_set,
                    inter_cmap[codepoint],
                )
                reason, residual = transformed_outline_residual(
                    inter_recording,
                    target_recording,
                    0.0,
                )
            except Exception as error:
                if len(errors) < 12:
                    errors.append(
                        [f"U+{codepoint:04X}", weight, type(error).__name__, str(error)]
                    )
                continue
            compared += 1
            if math.isfinite(residual):
                maximum_residual = max(maximum_residual, residual)
            if reason is not None or residual > CJK_SHEAR_POINT_TOLERANCE:
                mismatch_count += 1
                if len(mismatches) < 20:
                    mismatches.append(
                        {
                            "codepoint": f"U+{codepoint:04X}",
                            "wght": weight,
                            "reason": reason or "point_residual",
                            "maximum_residual": round(residual, 6)
                            if math.isfinite(residual)
                            else None,
                        }
                    )
    expected_instances = len(codepoints) * len(WEIGHT_HARMONY_WEIGHTS)
    return {
        "weights": list(WEIGHT_HARMONY_WEIGHTS),
        "outline_tolerance_font_units": CJK_SHEAR_POINT_TOLERANCE,
        "coverage": {
            "codepoints": len(codepoints),
            "expected_instances": expected_instances,
            "compared_instances": compared,
        },
        "counts": {
            "missing_codepoints": len(missing),
            "measurement_errors": expected_instances - compared,
            "outline_mismatches": mismatch_count,
        },
        "maximum_translation_invariant_outline_residual": round(maximum_residual, 6),
        "samples": {
            **({"missing_codepoints": [f"U+{codepoint:04X}" for codepoint in missing]} if missing else {}),
            **({"measurement_errors": errors} if errors else {}),
            **({"outline_mismatches": mismatches} if mismatches else {}),
        },
    }


def audit_vf_weight_curve_continuity() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    regions = b.variable_regions(b.REGION_ORDER)
    total = len(regions) * 2
    done = 0
    for region in regions:
        for italic in (False, True):
            done += 1
            path = vf_path(region, italic)
            log(f"VF weight curve {done}/{total}: {region}{' Italic' if italic else ''}")
            item: dict[str, Any] = {
                "region": region,
                "italic": italic,
                "target": display_path(path),
                "weights": list(WEIGHT_HARMONY_WEIGHTS),
                "relative_tolerance": 0.003,
                "counts": {
                    "latin_sample_missing": 0,
                    "cjk_sample_missing": 0,
                    "latin_area_nonmonotonic": 0,
                    "latin_stroke_nonmonotonic": 0,
                    "cjk_area_nonmonotonic": 0,
                    "cjk_stroke_nonmonotonic": 0,
                    "inter_axis_missing": 0,
                    "inter_axis_measurement_error": 0,
                    "inter_axis_outline_mismatch": 0,
                },
                "samples": {},
            }
            if not path.exists():
                item["missing"] = True
                out.append(item)
                continue
            font = TTFont(path)
            inter: TTFont | None = None
            try:
                inter = b.load_inter(italic)
                cmap = font.getBestCmap() or {}
                latin_codepoints = [
                    ord(character) for character in WEIGHT_HARMONY_LATIN_TEXT
                    if ord(character) in cmap
                ]
                cjk_codepoints = weight_harmony_cjk_codepoints(cmap)
                if len(latin_codepoints) != len(WEIGHT_HARMONY_LATIN_TEXT):
                    item["counts"]["latin_sample_missing"] = (
                        len(WEIGHT_HARMONY_LATIN_TEXT) - len(latin_codepoints)
                    )
                if not cjk_codepoints:
                    item["counts"]["cjk_sample_missing"] = WEIGHT_HARMONY_CJK_SAMPLES
                latin_curve = measure_outline_weight_curve(font, latin_codepoints)
                cjk_curve = measure_outline_weight_curve(font, cjk_codepoints)
                for script, curve in (("latin", latin_curve), ("cjk", cjk_curve)):
                    for label, metric in (
                        ("area", "area_ratio_to_400"),
                        ("stroke", "stroke_ratio_to_400"),
                    ):
                        failures = curve_nonmonotonic_steps(curve, metric)
                        key = f"{script}_{label}_nonmonotonic"
                        item["counts"][key] = len(failures)
                        if failures:
                            item["samples"][key] = failures[:12]
                item["coverage"] = {
                    "latin_codepoints": len(latin_codepoints),
                    "cjk_codepoints": len(cjk_codepoints),
                }
                item["named_points"] = {
                    script: {
                        str(weight): curve[weight]
                        for weight in WEIGHT_HARMONY_NAMED_WEIGHTS
                    }
                    for script, curve in (("latin", latin_curve), ("cjk", cjk_curve))
                }
                inter_axis = inter_axis_outline_status(font, inter)
                item["inter_axis_alignment"] = inter_axis
                item["counts"]["inter_axis_missing"] = inter_axis["counts"]["missing_codepoints"]
                item["counts"]["inter_axis_measurement_error"] = inter_axis["counts"]["measurement_errors"]
                item["counts"]["inter_axis_outline_mismatch"] = inter_axis["counts"]["outline_mismatches"]
                for key, values in inter_axis["samples"].items():
                    item["samples"][f"inter_axis_{key}"] = values
            finally:
                if inter is not None:
                    inter.close()
                font.close()
            out.append(item)
    return out


def transformed_outline_residual(
    source_recording: list[Any],
    target_recording: list[Any],
    angle_degrees: float,
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

    shear = math.tan(math.radians(angle_degrees))
    x_offsets = [
        target_point[0] - (source_point[0] + source_point[1] * shear)
        for source_point, target_point in point_pairs
    ]
    y_offsets = [
        target_point[1] - source_point[1]
        for source_point, target_point in point_pairs
    ]
    # Metric synchronization may translate a complete glyph. The midpoint of
    # each observed offset range minimizes the maximum residual; anchoring to
    # one arbitrary point can double the apparent rounding error.
    x_shift = (min(x_offsets) + max(x_offsets)) / 2
    y_shift = (min(y_offsets) + max(y_offsets)) / 2
    maximum = 0.0
    for source_point, target_point in point_pairs:
        expected_x = source_point[0] + source_point[1] * shear + x_shift
        expected_y = source_point[1] + y_shift
        maximum = max(
            maximum,
            abs(expected_x - target_point[0]),
            abs(expected_y - target_point[1]),
        )
    return None, maximum


def sheared_outline_residual(
    upright_recording: list[Any],
    italic_recording: list[Any],
) -> tuple[str | None, float]:
    return transformed_outline_residual(
        upright_recording,
        italic_recording,
        CJK_ITALIC_ANGLE_DEGREES,
    )


def exceeds_cjk_area_tolerance(first: float, second: float) -> bool:
    delta = abs(first - second)
    return delta > max(
        CJK_SHEAR_AREA_ABSOLUTE_TOLERANCE,
        max(first, second) * CJK_SHEAR_AREA_RELATIVE_TOLERANCE,
    )


def audit_vf_cjk_stroke_consistency() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    regions = b.variable_regions(b.REGION_ORDER)
    for region_index, region in enumerate(regions, start=1):
        upright_path = vf_path(region, False)
        italic_path = vf_path(region, True)
        log(f"VF CJK stroke audit {region_index}/{len(regions)}: {region}")
        item: dict[str, Any] = {
            "region": region,
            "target": f"{display_path(upright_path)} + {display_path(italic_path)}",
            "area_tolerance": {
                "relative": CJK_SHEAR_AREA_RELATIVE_TOLERANCE,
                "absolute_square_font_units": CJK_SHEAR_AREA_ABSOLUTE_TOLERANCE,
            },
            "outline_tolerance_font_units": CJK_SHEAR_POINT_TOLERANCE,
            "counts": {
                "cjk_cmap_mismatch": 0,
                "cjk_gvar_missing": 0,
                "cjk_area_measurement_error": 0,
                "italic_cjk_area_mismatch": 0,
                "italic_cjk_outline_mismatch": 0,
                "cjk_weight_nonmonotonic": 0,
                "cjk_weight_span_too_small": 0,
            },
            "samples": {},
        }
        if not upright_path.exists() or not italic_path.exists():
            item["missing"] = True
            out.append(item)
            continue

        upright = TTFont(upright_path)
        italic = TTFont(italic_path)
        try:
            upright_cmap = upright.getBestCmap() or {}
            italic_cmap = italic.getBestCmap() or {}
            upright_codepoints = {
                codepoint
                for codepoint, glyph_name in upright_cmap.items()
                if b.is_ideograph(codepoint) and not glyph_name.startswith(b.INTER_PREFIX)
            }
            italic_codepoints = {
                codepoint
                for codepoint, glyph_name in italic_cmap.items()
                if b.is_ideograph(codepoint) and not glyph_name.startswith(b.INTER_PREFIX)
            }
            cmap_mismatch = sorted(upright_codepoints ^ italic_codepoints)
            item["counts"]["cjk_cmap_mismatch"] = len(cmap_mismatch)
            if cmap_mismatch:
                item["samples"]["cjk_cmap_mismatch"] = [
                    f"U+{codepoint:04X}" for codepoint in cmap_mismatch[:20]
                ]
            codepoints = sorted(upright_codepoints & italic_codepoints)
            item["checked_codepoints"] = len(codepoints)

            missing_gvar: list[list[str]] = []
            for codepoint in codepoints:
                for style, font, cmap in (
                    ("upright", upright, upright_cmap),
                    ("italic", italic, italic_cmap),
                ):
                    glyph_name = cmap[codepoint]
                    if glyph_name not in font["gvar"].variations or not font["gvar"].variations[glyph_name]:
                        item["counts"]["cjk_gvar_missing"] += 1
                        if len(missing_gvar) < 12:
                            missing_gvar.append([f"U+{codepoint:04X}", style, glyph_name])
            if missing_gvar:
                item["samples"]["cjk_gvar_missing"] = missing_gvar

            areas = {
                "upright": {codepoint: [] for codepoint in codepoints},
                "italic": {codepoint: [] for codepoint in codepoints},
            }
            mismatch_samples: list[dict[str, Any]] = []
            outline_mismatch_samples: list[dict[str, Any]] = []
            measurement_errors: list[list[str]] = []
            maximum_relative_delta = 0.0
            maximum_absolute_delta = 0.0
            maximum_outline_residual = 0.0
            mismatch_by_weight: dict[str, int] = {}
            outline_mismatch_by_weight: dict[str, int] = {}
            for weight_name, weight_value in CJK_STROKE_WEIGHTS:
                upright_set = upright.getGlyphSet(location={"wght": weight_value})
                italic_set = italic.getGlyphSet(location={"wght": weight_value})
                weight_mismatches = 0
                weight_outline_mismatches = 0
                for codepoint in codepoints:
                    try:
                        upright_area, upright_recording = glyph_area_and_recording(
                            upright_set,
                            upright_cmap[codepoint],
                        )
                        italic_area, italic_recording = glyph_area_and_recording(
                            italic_set,
                            italic_cmap[codepoint],
                        )
                    except Exception as error:
                        item["counts"]["cjk_area_measurement_error"] += 1
                        if len(measurement_errors) < 12:
                            measurement_errors.append(
                                [f"U+{codepoint:04X}", weight_name, type(error).__name__, str(error)]
                            )
                        continue
                    areas["upright"][codepoint].append(upright_area)
                    areas["italic"][codepoint].append(italic_area)
                    relative_delta = abs(upright_area - italic_area) / max(upright_area, italic_area, 1.0)
                    absolute_delta = abs(upright_area - italic_area)
                    maximum_relative_delta = max(maximum_relative_delta, relative_delta)
                    maximum_absolute_delta = max(maximum_absolute_delta, absolute_delta)
                    if exceeds_cjk_area_tolerance(upright_area, italic_area):
                        item["counts"]["italic_cjk_area_mismatch"] += 1
                        weight_mismatches += 1
                        if len(mismatch_samples) < 20:
                            mismatch_samples.append(
                                {
                                    "codepoint": f"U+{codepoint:04X}",
                                    "weight": weight_name,
                                    "wght": weight_value,
                                    "upright_area": round(upright_area, 3),
                                    "italic_area": round(italic_area, 3),
                                    "relative_delta": round(relative_delta, 6),
                                }
                            )
                    outline_reason, outline_residual = sheared_outline_residual(
                        upright_recording,
                        italic_recording,
                    )
                    if math.isfinite(outline_residual):
                        maximum_outline_residual = max(maximum_outline_residual, outline_residual)
                    if outline_reason is not None or outline_residual > CJK_SHEAR_POINT_TOLERANCE:
                        item["counts"]["italic_cjk_outline_mismatch"] += 1
                        weight_outline_mismatches += 1
                        if len(outline_mismatch_samples) < 20:
                            outline_mismatch_samples.append(
                                {
                                    "codepoint": f"U+{codepoint:04X}",
                                    "weight": weight_name,
                                    "wght": weight_value,
                                    "reason": outline_reason or "point_residual",
                                    "maximum_residual": (
                                        round(outline_residual, 6)
                                        if math.isfinite(outline_residual)
                                        else None
                                    ),
                                }
                            )
                mismatch_by_weight[weight_name] = weight_mismatches
                outline_mismatch_by_weight[weight_name] = weight_outline_mismatches

            if measurement_errors:
                item["samples"]["cjk_area_measurement_error"] = measurement_errors
            if mismatch_samples:
                item["samples"]["italic_cjk_area_mismatch"] = mismatch_samples
            if outline_mismatch_samples:
                item["samples"]["italic_cjk_outline_mismatch"] = outline_mismatch_samples
            item["mismatch_by_weight"] = mismatch_by_weight
            item["outline_mismatch_by_weight"] = outline_mismatch_by_weight
            item["maximum_relative_area_delta"] = round(maximum_relative_delta, 8)
            item["maximum_absolute_area_delta"] = round(maximum_absolute_delta, 3)
            item["maximum_outline_residual"] = round(maximum_outline_residual, 6)

            nonmonotonic_samples: list[dict[str, Any]] = []
            span_samples: list[dict[str, Any]] = []
            expected_count = len(CJK_STROKE_WEIGHTS)
            for style in ("upright", "italic"):
                for codepoint, values in areas[style].items():
                    if len(values) != expected_count:
                        continue
                    for index, (previous, current) in enumerate(zip(values, values[1:]), start=1):
                        if current + max(
                            CJK_MONOTONIC_AREA_ABSOLUTE_TOLERANCE,
                            previous * CJK_MONOTONIC_AREA_RELATIVE_TOLERANCE,
                        ) < previous:
                            item["counts"]["cjk_weight_nonmonotonic"] += 1
                            if len(nonmonotonic_samples) < 20:
                                nonmonotonic_samples.append(
                                    {
                                        "codepoint": f"U+{codepoint:04X}",
                                        "style": style,
                                        "from": CJK_STROKE_WEIGHTS[index - 1][0],
                                        "to": CJK_STROKE_WEIGHTS[index][0],
                                        "from_area": round(previous, 3),
                                        "to_area": round(current, 3),
                                    }
                                )
                    light_area = values[0]
                    heavy_area = values[-1]
                    if heavy_area < light_area + max(
                        CJK_WEIGHT_SPAN_ABSOLUTE_MINIMUM,
                        light_area * CJK_WEIGHT_SPAN_RELATIVE_MINIMUM,
                    ):
                        item["counts"]["cjk_weight_span_too_small"] += 1
                        if len(span_samples) < 20:
                            span_samples.append(
                                {
                                    "codepoint": f"U+{codepoint:04X}",
                                    "style": style,
                                    "extra_light_area": round(light_area, 3),
                                    "heavy_area": round(heavy_area, 3),
                                }
                            )
            if nonmonotonic_samples:
                item["samples"]["cjk_weight_nonmonotonic"] = nonmonotonic_samples
            if span_samples:
                item["samples"]["cjk_weight_span_too_small"] = span_samples
        finally:
            upright.close()
            italic.close()
        out.append(item)
    return out


def audit_vf_contextual_spacing() -> list[dict[str, Any]]:
    out = []
    regions = b.variable_regions(b.REGION_ORDER)
    total = len(regions) * 2 * len(CONTEXTUAL_SPACING_VF_WEIGHTS)
    done = 0
    for region in regions:
        for italic in [False, True]:
            path = vf_path(region, italic)
            for weight in CONTEXTUAL_SPACING_VF_WEIGHTS:
                done += 1
                log(
                    f"VF chws/vchw {done}/{total}: {region} wght={weight}"
                    f"{' Italic' if italic else ''}"
                )
                item: dict[str, Any] = {
                    "region": region,
                    "weight": str(weight),
                    "wght": weight,
                    "italic": italic,
                    "target": display_path(path),
                    "counts": {"contextual_spacing_shape": 0},
                    "samples": {},
                }
                if not path.exists():
                    item["missing"] = True
                else:
                    status = contextual_spacing_status(path, {"wght": weight})
                    if not status["ok"]:
                        item["counts"]["contextual_spacing_shape"] = 1
                        item["samples"]["contextual_spacing_shape"] = status
                    elif weight in {200, 400, 900}:
                        item["sample"] = {
                            "features": status["features"],
                            "horizontal_reduction": status["horizontal_reduction"],
                            "vertical_reduction": status["vertical_reduction"],
                            "isolated_horizontal_unchanged": status["isolated_horizontal_unchanged"],
                            "isolated_vertical_unchanged": status["isolated_vertical_unchanged"],
                        }
                out.append(item)
    return out


def positioning_signature(signature: list[list[Any]]) -> list[list[int]]:
    return [
        [int(row[1]), int(row[2]), int(row[3]), int(row[4])]
        for row in signature
    ]


def positioning_delta(
    enabled: list[list[int]],
    disabled: list[list[int]],
) -> list[list[int]] | None:
    if len(enabled) != len(disabled):
        return None
    return [
        [enabled_value - disabled_value for enabled_value, disabled_value in zip(on, off)]
        for on, off in zip(enabled, disabled)
    ]


def audit_vf_source_gpos_variations() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    weights = [
        (str(stop["name"]), int(stop["value"]))
        for stop in b.SOURCE_HAN_WEIGHT_STOPS
    ]
    total = len(b.variable_regions(b.REGION_ORDER)) * 2 * len(weights)
    done = 0
    cases = [
        (
            "palt",
            "かなカナ",
            "kana",
            "ltr",
            {"palt": True, "vpal": False, "kern": False, "vert": False, "vrt2": False},
            {"palt": False, "vpal": False, "kern": False, "vert": False, "vrt2": False},
        ),
        (
            "vpal",
            "かなカナ",
            "kana",
            "ttb",
            {"palt": False, "vpal": True, "kern": False, "vert": True, "vrt2": False},
            {"palt": False, "vpal": False, "kern": False, "vert": True, "vrt2": False},
        ),
        (
            "kern",
            chr(0xFB00) + chr(0x201E),
            "DFLT",
            "ltr",
            {"palt": False, "vpal": False, "kern": True, "vert": False, "vrt2": False},
            {"palt": False, "vpal": False, "kern": False, "vert": False, "vrt2": False},
        ),
    ]
    for region in b.variable_regions(b.REGION_ORDER):
        source_path = b.source_han_vf_path(region)
        source_data = source_path.read_bytes()
        source_font = TTFont(source_path)
        try:
            source_order = source_font.getGlyphOrder()
            for italic in [False, True]:
                target_path = vf_path(region, italic)
                target_data = target_path.read_bytes() if target_path.exists() else b""
                target_font = TTFont(target_path) if target_path.exists() else None
                try:
                    target_order = target_font.getGlyphOrder() if target_font else []
                    for weight_name, public_weight in weights:
                        done += 1
                        internal_weight = int(
                            b.SOURCE_HAN_PUBLIC_TO_INTERNAL_WGHT[public_weight]
                        )
                        log(
                            "VF source GPOS variation "
                            f"{done}/{total}: {region} "
                            f"{'italic' if italic else 'upright'} {weight_name}"
                        )
                        item: dict[str, Any] = {
                            "region": region,
                            "italic": italic,
                            "weight": weight_name,
                            "public_wght": public_weight,
                            "source_han_wght": internal_weight,
                            "target": display_path(target_path),
                            "source": display_path(source_path),
                            "counts": {
                                "palt_source_variation": 0,
                                "vpal_source_variation": 0,
                                "kern_source_variation": 0,
                            },
                            "samples": {},
                        }
                        if target_font is None:
                            item["missing"] = True
                            out.append(item)
                            continue
                        for feature_tag, text, script, direction, features, disabled_features in cases:
                            target_enabled = positioning_signature(
                                shape_signature_data(
                                    target_data,
                                    target_order,
                                    text,
                                    script,
                                    features,
                                    direction=direction,
                                    variations={"wght": public_weight},
                                )
                            )
                            target_disabled = positioning_signature(
                                shape_signature_data(
                                    target_data,
                                    target_order,
                                    text,
                                    script,
                                    disabled_features,
                                    direction=direction,
                                    variations={"wght": public_weight},
                                )
                            )
                            source_enabled = positioning_signature(
                                shape_signature_data(
                                    source_data,
                                    source_order,
                                    text,
                                    script,
                                    features,
                                    direction=direction,
                                    variations={"wght": internal_weight},
                                )
                            )
                            source_disabled = positioning_signature(
                                shape_signature_data(
                                    source_data,
                                    source_order,
                                    text,
                                    script,
                                    disabled_features,
                                    direction=direction,
                                    variations={"wght": internal_weight},
                                )
                            )
                            target_signature = positioning_delta(
                                target_enabled,
                                target_disabled,
                            )
                            source_signature = positioning_delta(
                                source_enabled,
                                source_disabled,
                            )
                            same_length = (
                                target_signature is not None
                                and source_signature is not None
                                and len(target_signature) == len(source_signature)
                            )
                            maximum_delta = (
                                max(
                                    abs(target_value - source_value)
                                    for target_row, source_row in zip(
                                        target_signature,
                                        source_signature,
                                    )
                                    for target_value, source_value in zip(
                                        target_row,
                                        source_row,
                                    )
                                )
                                if same_length and target_signature and source_signature
                                else None
                            )
                            if not same_length or maximum_delta is None or maximum_delta > 1:
                                key = f"{feature_tag}_source_variation"
                                item["counts"][key] = 1
                                item["samples"][key] = {
                                    "target": target_signature,
                                    "source": source_signature,
                                    "maximum_delta": maximum_delta,
                                }
                        out.append(item)
                finally:
                    if target_font is not None:
                        target_font.close()
        finally:
            source_font.close()
    return out


def nonzero(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bad = []
    for item in items:
        counts = item.get("counts", {})
        if item.get("missing") or any(counts.values()):
            bad.append(
                {
                    "kind": item.get("kind"),
                    "region": item.get("region"),
                    "weight": item.get("weight"),
                    "wght": item.get("wght"),
                    "italic": item.get("italic"),
                    "hinted": item.get("hinted"),
                    "role": item.get("role"),
                    "target": item.get("target"),
                    "source": item.get("source"),
                    "missing": item.get("missing", False),
                    "counts": counts,
                    "coverage": item.get("coverage", {}),
                    "observations": item.get("observations", {}),
                    "classical_source_exception": item.get("classical_source_exception"),
                    "samples": item.get("samples", {}),
                    "observation_samples": item.get("observation_samples", {}),
                }
            )
    return bad


def sum_nested_values(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    keys = {
        key
        for item in items
        for key, value in item.get(field, {}).items()
        if isinstance(value, (int, float))
    }
    return {
        key: int(sum(item.get(field, {}).get(key, 0) for item in items))
        for key in sorted(keys)
    }


def instruction_raster_coverage_failures(
    static_exact: list[dict[str, Any]],
    static_raster: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raster_by_case = {
        (item.get("region"), item.get("weight"), item.get("italic")): item
        for item in static_raster
    }
    failures: list[dict[str, Any]] = []
    for item in static_exact:
        instruction_differences = item.get("observations", {}).get("instruction_bytecode_differences", 0)
        if not item.get("hinted") or not instruction_differences:
            continue
        case = (item.get("region"), item.get("weight"), item.get("italic"))
        raster = raster_by_case.get(case)
        if raster is None or raster.get("missing") or any(raster.get("counts", {}).values()):
            failures.append(
                {
                    "kind": "instruction_differences_without_clean_raster_coverage",
                    "region": case[0],
                    "weight": case[1],
                    "italic": case[2],
                    "instruction_bytecode_differences": instruction_differences,
                    "raster": raster,
                }
            )
    return failures


def classical_outline_coverage_failures(
    static_exact: list[dict[str, Any]],
    cl_source_outlines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_case = {
        (item.get("weight"), item.get("italic"), item.get("hinted")): item
        for item in cl_source_outlines
    }
    failures: list[dict[str, Any]] = []
    for item in static_exact:
        if item.get("region") != "CL":
            continue
        excluded = int(item.get("coverage", {}).get("classical_outline_exceptions", 0))
        if not excluded:
            continue
        case = (item.get("weight"), item.get("italic"), item.get("hinted"))
        source_item = source_by_case.get(case)
        compared = (
            int(source_item.get("coverage", {}).get("classical_exceptions_compared", 0))
            if source_item
            else 0
        )
        if source_item is None or source_item.get("missing") or compared != excluded:
            failures.append(
                {
                    "kind": "classical_outline_exclusion_without_equal_source_coverage",
                    "region": "CL",
                    "weight": case[0],
                    "italic": case[1],
                    "hinted": case[2],
                    "excluded": excluded,
                    "source_compared": compared,
                    "source_missing": source_item is None or source_item.get("missing", False),
                }
            )
    return failures


def vf_source_metric_coverage_failures(
    vf_source_pairing: list[dict[str, Any]],
    vf_metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metric_by_case = {
        (item.get("region"), item.get("italic"), item.get("wght")): item
        for item in vf_metrics
    }
    failures: list[dict[str, Any]] = []
    for item in vf_source_pairing:
        differences = sum(
            int(
                item.get(section, {})
                .get("observations", {})
                .get("hmtx_differences", 0)
            )
            for section in ("inter_600", "cjk_source_han_500")
        )
        if not differences:
            continue
        case = (item.get("region"), item.get("italic"), 600)
        metric_item = metric_by_case.get(case)
        if metric_item is None or metric_item.get("missing") or any(
            metric_item.get("counts", {}).values()
        ):
            failures.append(
                {
                    "kind": "vf_source_hmtx_observation_without_clean_sarasa_metric_alignment",
                    "region": case[0],
                    "italic": case[1],
                    "wght": 600,
                    "source_hmtx_differences": differences,
                    "vf_metric_case": metric_item,
                }
            )
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计 Sarasa Ui PropDigits 发布字体")
    parser.add_argument("--skip-raster", action="store_true", help="跳过 FreeType 栅格审计")
    parser.add_argument("--raster-only", action="store_true", help="只运行 FreeType 栅格审计")
    parser.add_argument(
        "--weight-harmony-only",
        action="store_true",
        help="只运行 VF 中西文字重协调性设计分析",
    )
    parser.add_argument(
        "--raster-jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="栅格审计并发进程数，默认使用本机逻辑 CPU 数",
    )
    parser.add_argument(
        "--raster-ppems",
        default=",".join(map(str, DEFAULT_RASTER_PPEMS)),
        help="逗号分隔的 ppem，默认 9,12,16,20,24",
    )
    parser.add_argument("--raster-regions", help="仅栅格审计指定地区，逗号分隔")
    parser.add_argument("--raster-weights", help="仅栅格审计指定 exact 字重，逗号分隔")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "reports" / "release-audit.json",
        help="JSON 报告输出路径",
    )
    parser.add_argument(
        "--reuse-non-raster-report",
        type=Path,
        help="复用一次 --skip-raster 生成的完整非栅格报告，只补跑栅格审计并合并输出",
    )
    return parser.parse_args()


def parse_csv(value: str | None, allowed: list[str], label: str) -> list[str] | None:
    if not value:
        return None
    selected = [part.strip() for part in value.split(",") if part.strip()]
    invalid = [part for part in selected if part not in allowed]
    if invalid:
        raise ValueError(f"unsupported {label}: {', '.join(invalid)}")
    return selected


def main() -> None:
    args = parse_args()
    if args.weight_harmony_only:
        report = audit_vf_weight_harmony()
        report["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
        report["toolchain"] = {
            "fonttools": importlib.metadata.version("fonttools"),
            "numpy": importlib.metadata.version("numpy"),
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report_text = json.dumps(
            b.sanitize_report_data(report),
            ensure_ascii=False,
            indent=2,
        )
        b.assert_portable_report_text(report_text)
        args.report.write_text(
            report_text + "\n",
            encoding="utf-8",
            newline="\n",
        )
        log(f"wrote weight harmony analysis to {display_path(args.report)}")
        return

    ppems = tuple(int(value.strip()) for value in args.raster_ppems.split(",") if value.strip())
    if not ppems or any(value <= 0 for value in ppems):
        raise ValueError("--raster-ppems must contain positive integers")
    raster_regions = parse_csv(args.raster_regions, b.REGION_ORDER, "regions")
    raster_weights = parse_csv(args.raster_weights, EXACT_WEIGHTS, "weights")

    reused_non_raster_report: str | None = None
    if args.raster_only and args.reuse_non_raster_report:
        raise ValueError("--raster-only and --reuse-non-raster-report are mutually exclusive")
    if args.reuse_non_raster_report:
        checkpoint_path = args.reuse_non_raster_report.resolve()
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        required_sections = {
            "summary",
            "metadata_failures",
            "metadata_samples",
            "static_exact",
            "static_extension_sources",
            "cl_source_outlines",
            "cl_static_boundaries",
            "static_layout_templates",
            "static_propdigits_shaping",
            "static_palt_shaping",
            "static_em_dash_shaping",
            "static_upstream_dash_sources",
            "vf_em_dash_shaping",
            "vf_upstream_dash_sources",
            "vf_metrics",
            "vf_source_pairing",
            "vf_weight_curves",
            "vf_cjk_strokes",
            "vf_contextual_spacing",
            "vf_source_gpos_variations",
        }
        missing_sections = sorted(required_sections - set(checkpoint))
        if missing_sections:
            raise ValueError(
                "non-raster report is incomplete: " + ", ".join(missing_sections)
            )
        checkpoint_summary = checkpoint["summary"]
        if int(checkpoint_summary.get("static_raster_cases", -1)) != 0:
            raise ValueError("non-raster report must be generated with --skip-raster")
        metadata = {
            "failures": checkpoint["metadata_failures"],
            "static_count": int(checkpoint_summary["static_fonts_checked"]),
            "variable_count": int(checkpoint_summary["variable_fonts_checked"]),
            "samples": checkpoint["metadata_samples"],
        }
        static_exact = checkpoint["static_exact"]
        static_extension_sources = checkpoint["static_extension_sources"]
        cl_source_outlines = checkpoint["cl_source_outlines"]
        cl_boundaries = checkpoint["cl_static_boundaries"]
        static_layout_templates = checkpoint["static_layout_templates"]
        static_propdigits_shaping = checkpoint["static_propdigits_shaping"]
        static_palt_shaping = checkpoint["static_palt_shaping"]
        static_em_dash_shaping = checkpoint["static_em_dash_shaping"]
        static_upstream_dash_sources = checkpoint["static_upstream_dash_sources"]
        vf_em_dash_shaping = checkpoint["vf_em_dash_shaping"]
        vf_upstream_dash_sources = checkpoint["vf_upstream_dash_sources"]
        vf_metrics = checkpoint["vf_metrics"]
        vf_source_pairing = checkpoint["vf_source_pairing"]
        vf_weight_curves = checkpoint["vf_weight_curves"]
        vf_cjk_strokes = checkpoint["vf_cjk_strokes"]
        vf_contextual_spacing = checkpoint["vf_contextual_spacing"]
        vf_source_gpos_variations = checkpoint["vf_source_gpos_variations"]
        reused_non_raster_report = display_path(checkpoint_path)
        log(f"reused non-raster report {reused_non_raster_report}")
    elif args.raster_only:
        metadata = {"failures": [], "static_count": 0, "variable_count": 0, "samples": {}}
        static_exact: list[dict[str, Any]] = []
        static_extension_sources: list[dict[str, Any]] = []
        cl_source_outlines: list[dict[str, Any]] = []
        cl_boundaries: list[dict[str, Any]] = []
        static_layout_templates: list[dict[str, Any]] = []
        static_propdigits_shaping: list[dict[str, Any]] = []
        static_palt_shaping: list[dict[str, Any]] = []
        static_em_dash_shaping: list[dict[str, Any]] = []
        static_upstream_dash_sources: list[dict[str, Any]] = []
        vf_em_dash_shaping: list[dict[str, Any]] = []
        vf_upstream_dash_sources: list[dict[str, Any]] = []
        vf_metrics: list[dict[str, Any]] = []
        vf_source_pairing: list[dict[str, Any]] = []
        vf_weight_curves: list[dict[str, Any]] = []
        vf_cjk_strokes: list[dict[str, Any]] = []
        vf_contextual_spacing: list[dict[str, Any]] = []
        vf_source_gpos_variations: list[dict[str, Any]] = []
    else:
        metadata = audit_metadata()
        static_exact = audit_static_exact()
        static_extension_sources = audit_static_extension_sources()
        cl_source_outlines = audit_static_cl_source_outlines()
        cl_boundaries = audit_static_cl_boundaries()
        static_layout_templates = audit_static_layout_templates()
        static_propdigits_shaping = audit_static_propdigits_shaping()
        static_palt_shaping = audit_static_palt_shaping()
        static_em_dash_shaping = audit_static_em_dash_shaping()
        static_upstream_dash_sources = audit_static_upstream_dash_sources()
        vf_em_dash_shaping = audit_vf_em_dash_shaping()
        vf_upstream_dash_sources = audit_vf_upstream_dash_sources()
        vf_metrics = audit_vf_metrics()
        vf_source_pairing = audit_vf_source_pairing()
        vf_weight_curves = audit_vf_weight_curve_continuity()
        vf_cjk_strokes = audit_vf_cjk_stroke_consistency()
        vf_contextual_spacing = audit_vf_contextual_spacing()
        vf_source_gpos_variations = audit_vf_source_gpos_variations()

    static_raster = [] if args.skip_raster else audit_static_raster(
        ppems,
        args.raster_jobs,
        regions=raster_regions,
        weights=raster_weights,
    )
    static_exact_coverage = sum_nested_values(static_exact, "coverage")
    static_exact_observations = sum_nested_values(static_exact, "observations")
    static_raster_coverage = sum_nested_values(static_raster, "coverage")
    coverage_failures = [] if args.raster_only else classical_outline_coverage_failures(
        static_exact,
        cl_source_outlines,
    )
    if not args.raster_only:
        coverage_failures.extend(
            vf_source_metric_coverage_failures(vf_source_pairing, vf_metrics)
        )
    if not args.skip_raster and not args.raster_only:
        coverage_failures.extend(
            instruction_raster_coverage_failures(static_exact, static_raster)
        )
    summary = {
        "audit_coverage_failures": len(coverage_failures),
        "metadata_failures": len(metadata["failures"]),
        "static_exact_failures": len(nonzero(static_exact)),
        "static_extension_source_failures": len(
            nonzero(static_extension_sources)
        ),
        "cl_source_outline_failures": len(nonzero(cl_source_outlines)),
        "static_raster_failures": len(nonzero(static_raster)),
        "cl_static_boundary_failures": len(nonzero(cl_boundaries)),
        "static_layout_template_failures": len(nonzero(static_layout_templates)),
        "static_propdigits_shaping_failures": len(nonzero(static_propdigits_shaping)),
        "static_palt_shaping_failures": len(nonzero(static_palt_shaping)),
        "static_em_dash_shaping_failures": len(nonzero(static_em_dash_shaping)),
        "static_upstream_dash_source_failures": len(
            nonzero(static_upstream_dash_sources)
        ),
        "vf_em_dash_shaping_failures": len(nonzero(vf_em_dash_shaping)),
        "vf_upstream_dash_source_failures": len(
            nonzero(vf_upstream_dash_sources)
        ),
        "vf_metric_failures": len(nonzero(vf_metrics)),
        "vf_source_pairing_failures": len(nonzero(vf_source_pairing)),
        "vf_weight_curve_failures": len(nonzero(vf_weight_curves)),
        "vf_cjk_stroke_failures": len(nonzero(vf_cjk_strokes)),
        "vf_contextual_spacing_failures": len(nonzero(vf_contextual_spacing)),
        "vf_source_gpos_variation_failures": len(nonzero(vf_source_gpos_variations)),
        "static_fonts_checked": metadata["static_count"],
        "variable_fonts_checked": metadata["variable_count"],
        "static_exact_cases": len(static_exact),
        "static_official_exact_outline_cases": sum(
            item.get("comparison_mode") == "official-exact" for item in static_exact
        ),
        "static_extension_boundary_cmap_cases": sum(
            item.get("comparison_mode") == "official-boundary-cmap-only"
            for item in static_exact
        ),
        "static_extension_source_cases": len(static_extension_sources),
        "static_extension_source_codepoints": sum(
            item.get("coverage", {}).get("codepoints_compared", 0)
            for item in static_extension_sources
        ),
        "cl_source_outline_cases": len(cl_source_outlines),
        "cl_source_outline_codepoints": sum(
            item.get("coverage", {}).get("classical_exceptions_compared", 0)
            for item in cl_source_outlines
        ),
        "static_exact_coverage": static_exact_coverage,
        "static_exact_observations": static_exact_observations,
        "static_raster_cases": len(static_raster),
        "static_official_raster_cases": sum(
            item.get("comparison_mode") == "official-hinted-exact"
            for item in static_raster
        ),
        "static_extension_nohint_raster_cases": sum(
            item.get("comparison_mode") == "project-hinted-vs-unhinted-no-hinting"
            for item in static_raster
        ),
        "static_raster_coverage": static_raster_coverage,
        "static_raster_renders": sum(item.get("renders_checked", 0) for item in static_raster),
        "cl_static_boundary_cases": len(cl_boundaries),
        "static_layout_template_cases": len(static_layout_templates),
        "static_propdigits_shaping_cases": len(static_propdigits_shaping),
        "static_palt_shaping_cases": len(static_palt_shaping),
        "static_em_dash_shaping_cases": len(static_em_dash_shaping),
        "static_upstream_dash_source_cases": len(static_upstream_dash_sources),
        "vf_em_dash_shaping_cases": len(vf_em_dash_shaping),
        "vf_upstream_dash_source_cases": len(vf_upstream_dash_sources),
        "vf_metric_cases": len(vf_metrics),
        "vf_source_pairing_cases": len(vf_source_pairing),
        "vf_weight_curve_cases": len(vf_weight_curves),
        "vf_cjk_stroke_cases": len(vf_cjk_strokes),
        "vf_cjk_stroke_codepoints": sum(item.get("checked_codepoints", 0) for item in vf_cjk_strokes),
        "vf_cjk_stroke_weighted_instances": sum(
            item.get("checked_codepoints", 0) * len(CJK_STROKE_WEIGHTS)
            for item in vf_cjk_strokes
        ),
        "vf_contextual_spacing_cases": len(vf_contextual_spacing),
        "vf_source_gpos_variation_cases": len(vf_source_gpos_variations),
    }
    report = {
        "title": "Sarasa Ui PropDigits 多地区发布前审计",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "reused_non_raster_report": reused_non_raster_report,
        "toolchain": {
            "freetype_py": importlib.metadata.version("freetype-py"),
            "freetype": list(freetype.version()),
            "fonttools": importlib.metadata.version("fonttools"),
            "uharfbuzz": importlib.metadata.version("uharfbuzz"),
        },
        "summary": summary,
        "metadata_failures": metadata["failures"],
        "audit_coverage_failures": coverage_failures,
        "non_failing_observation_policy": {
            "glyph_id_differences": "post format 3 不存储 glyph name；只要 cmap、轮廓、metrics 与 layout 审计通过，GID 不要求等同。",
            "instruction_bytecode_differences": (
                "hinted 字体由本项目在修改后的完整字形环境中重新 hint，不复制上游 glyph program；"
                "字节差异必须由同 case 的全码位 FreeType 多 ppem 栅格审计覆盖，否则 audit_coverage_failures 非零。"
            ),
            "heavy_static_reference": (
                "Heavy 900 是本项目扩展字重，上游 Sarasa 没有同名静态成品。Bold 只核验公开 cmap 与 layout 边界；"
                "完整非产品 glyph 的轮廓、hmtx/vmtx 和 palt 逐码位对比同次构建的 Sarasa pass2 Heavy/Black 来源，"
                "栅格阶段再于 FT_LOAD_NO_HINTING 下逐码位比较同字重 hinted/unhinted，确保重新 hint 未改变基础轮廓。"
            ),
            "dash_static_vf_source_boundary": (
                "Source Han/Shanggu 的静态 TTF 与 VF 发布路径在破折号轮廓平移、side bearing 和少量量化上并非 exact；"
                "审计分别要求静态成品与静态源、VF 成品与 VF 源逐角色 exact，并只把跨产品的一字形与 2em advance 语义作为失败项。"
                "跨产品 exact bounds 与 FreeType 位图差异完整保留为观察数据，不用前缀或过滤隐藏。"
            ),
            "vf_source_hmtx_differences": (
                "Inter 与 CJK 轮廓及轴坐标分别按 Inter 600 和 Source Han/Shanggu 500 来源核验，但最终 metrics "
                "以对应 Sarasa Ui 静态模板为准；public 600 的任何源 hmtx 观察项都必须由同地区、同斜体的 "
                "SemiBold exact VF metric 用例零差异覆盖，"
                "否则 audit_coverage_failures 非零。"
            ),
            "vf_source_alias_handling": (
                "来源轮廓比较可沿最终字体的 cmap alias 组寻找对应上游 glyph，但不允许回退到另一套轮廓基线；"
                "同时对除数字和冒号外的完整 cmap alias 分区与同地区官方 Sarasa Ui SemiBold 做严格核验。"
            ),
            "vf_source_gpos_variations": (
                "Source Han 的静态与 VF 发布物可能包含不同的 kern/palt/vpal 定位值；静态成品按 Sarasa 静态参考同步，"
                "VF 的 kern/palt/vpal 则必须在每个公开字重映射点与对应 Source Han VF 内部实例一致；轴裁剪、avar 与 "
                "ItemVariationStore 都以 F2Dot14 保存坐标，因重复定点量化允许单项最多 1 unit，超过即失败。"
            ),
        },
        "static_exact_nonzero": nonzero(static_exact),
        "static_extension_sources_nonzero": nonzero(static_extension_sources),
        "cl_source_outlines_nonzero": nonzero(cl_source_outlines),
        "static_raster_nonzero": nonzero(static_raster),
        "cl_static_boundary_nonzero": nonzero(cl_boundaries),
        "static_layout_template_nonzero": nonzero(static_layout_templates),
        "static_propdigits_shaping_nonzero": nonzero(static_propdigits_shaping),
        "static_palt_shaping_nonzero": nonzero(static_palt_shaping),
        "static_em_dash_shaping_nonzero": nonzero(static_em_dash_shaping),
        "static_upstream_dash_sources_nonzero": nonzero(
            static_upstream_dash_sources
        ),
        "vf_em_dash_shaping_nonzero": nonzero(vf_em_dash_shaping),
        "vf_upstream_dash_sources_nonzero": nonzero(vf_upstream_dash_sources),
        "vf_metrics_nonzero": nonzero(vf_metrics),
        "vf_source_pairing_nonzero": nonzero(vf_source_pairing),
        "vf_weight_curves_nonzero": nonzero(vf_weight_curves),
        "vf_cjk_strokes_nonzero": nonzero(vf_cjk_strokes),
        "vf_contextual_spacing_nonzero": nonzero(vf_contextual_spacing),
        "vf_source_gpos_variations_nonzero": nonzero(vf_source_gpos_variations),
        "static_exact": static_exact,
        "static_extension_sources": static_extension_sources,
        "cl_source_outlines": cl_source_outlines,
        "static_raster": static_raster,
        "cl_static_boundaries": cl_boundaries,
        "static_layout_templates": static_layout_templates,
        "static_propdigits_shaping": static_propdigits_shaping,
        "static_palt_shaping": static_palt_shaping,
        "static_em_dash_shaping": static_em_dash_shaping,
        "static_upstream_dash_sources": static_upstream_dash_sources,
        "vf_em_dash_shaping": vf_em_dash_shaping,
        "vf_upstream_dash_sources": vf_upstream_dash_sources,
        "vf_metrics": vf_metrics,
        "vf_source_pairing": vf_source_pairing,
        "vf_weight_curves": vf_weight_curves,
        "vf_cjk_strokes": vf_cjk_strokes,
        "vf_contextual_spacing": vf_contextual_spacing,
        "vf_source_gpos_variations": vf_source_gpos_variations,
        "metadata_samples": metadata["samples"],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    portable_report = b.sanitize_report_data(report)
    report_text = json.dumps(portable_report, ensure_ascii=False, indent=2)
    b.assert_portable_report_text(report_text)
    args.report.write_text(
        report_text + "\n",
        encoding="utf-8",
        newline="\n",
    )
    log("summary " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    failure_keys = [key for key in summary if key.endswith("_failures")]
    if any(summary[key] for key in failure_keys):
        log(f"audit FAILED; see {display_path(args.report)}")
        raise SystemExit(1)
    log(f"audit PASSED; wrote {display_path(args.report)}")


if __name__ == "__main__":
    main()
