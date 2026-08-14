from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


AUDIT_DEPS = {
    "freetype": ("freetype-py", "freetype-py==2.5.1", "2.5.1"),
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
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_sarasa_ui_propdigits_sc as b  # noqa: E402

START = time.time()
INTENTIONAL_CPS = set(range(0x30, 0x3A)) | {0x3A}
EXACT_WEIGHTS = ["ExtraLight", "Light", "Regular", "Bold"]
EXPECTED_WEIGHTS = {str(stop["name"]): int(stop["value"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS}
EXPECTED_AXIS = [{"tag": "wght", "min": 200.0, "default": 400.0, "max": 900.0}]
CL_BOUNDARY_ADVANCE_CODEPOINTS = [0x5DC5, 0x62FC, 0x7EFF]
CLASSICAL_SKIP_CACHE: dict[tuple[str, str], set[int]] = {}
FONT_REVISION_TOLERANCE = 1 / 65536
CONTEXTUAL_SPACING_FEATURES = {"chws", "vchw"}
CONTEXTUAL_SPACING_VF_WEIGHTS = [200, 300, 400, 700, 900]
DEFAULT_RASTER_PPEMS = (9, 12, 16, 20, 24)


def log(message: str) -> None:
    print(f"[{time.time() - START:8.1f}s] {message}", flush=True)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def static_path(region: str, weight: str, italic: bool, hinted: bool) -> Path:
    return b.static_dir(region, hinted) / b.static_output_name(region, weight, italic)


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


def glyph_program_count(font: TTFont) -> int:
    if "glyf" not in font:
        return 0
    glyf = font["glyf"]
    count = 0
    for glyph_name in font.getGlyphOrder():
        glyph = glyf[glyph_name]
        try:
            glyph.expand(glyf)
        except Exception:
            pass
        program = getattr(glyph, "program", None)
        if program is None:
            continue
        try:
            if len(program.getBytecode()) > 0:
                count += 1
        except Exception:
            pass
    return count


def glyph_signature(font: TTFont, glyph_name: str) -> dict[str, Any]:
    glyf = font["glyf"]
    glyph = glyf[glyph_name]
    try:
        glyph.expand(glyf)
    except Exception:
        pass
    bbox = tuple(getattr(glyph, attr, None) for attr in ("xMin", "yMin", "xMax", "yMax"))
    if getattr(glyph, "isComposite", lambda: False)():
        components = []
        for component in getattr(glyph, "components", []) or []:
            transform = getattr(component, "transform", None)
            components.append(
                (
                    getattr(component, "glyphName", None),
                    getattr(component, "x", None),
                    getattr(component, "y", None),
                    getattr(component, "flags", None),
                    tuple(transform) if transform is not None else None,
                )
            )
        shape = ("composite", tuple(components))
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


def classical_skip_codepoints(region: str, weight: str) -> set[int]:
    key = (region, weight)
    if key in CLASSICAL_SKIP_CACHE:
        return CLASSICAL_SKIP_CACHE[key]
    if not b.region_config(region)["classical"]:
        CLASSICAL_SKIP_CACHE[key] = set()
        return CLASSICAL_SKIP_CACHE[key]
    path = b.classical_static_override_path(region, weight)
    if not path or not path.exists():
        CLASSICAL_SKIP_CACHE[key] = set()
        return CLASSICAL_SKIP_CACHE[key]
    font = TTFont(path)
    try:
        cmap = font.getBestCmap() or {}
        CLASSICAL_SKIP_CACHE[key] = {cp for cp in cmap if b.source_han_overrides_inter(cp)}
    finally:
        font.close()
    return CLASSICAL_SKIP_CACHE[key]


def compare_fonts(
    target: TTFont,
    reference: TTFont,
    compare_glyphs: bool,
    skip_codepoints: set[int] | None = None,
) -> dict[str, Any]:
    intentional_cps = set(INTENTIONAL_CPS)
    if skip_codepoints:
        intentional_cps.update(skip_codepoints)
    target_cmap = target.getBestCmap() or {}
    reference_cmap = reference.getBestCmap() or {}
    counts = {
        "missing_target": 0,
        "missing_reference": 0,
        "glyph_name": 0,
        "h_advance": 0,
        "h_lsb": 0,
        "v_advance": 0,
        "v_side_bearing": 0,
        "bbox": 0,
        "outline_or_flags": 0,
        "diagnostic_instructions": 0,
        "skipped_intentional": 0,
    }
    samples: dict[str, list[Any]] = {key: [] for key in counts}
    target_hmtx = target["hmtx"].metrics if "hmtx" in target else {}
    reference_hmtx = reference["hmtx"].metrics if "hmtx" in reference else {}
    target_vmtx = target["vmtx"].metrics if "vmtx" in target else {}
    reference_vmtx = reference["vmtx"].metrics if "vmtx" in reference else {}
    all_codepoints = set(target_cmap) | set(reference_cmap)
    counts["skipped_intentional"] = len(all_codepoints & intentional_cps)
    for cp in sorted(all_codepoints - intentional_cps):
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
        if target_glyph != reference_glyph:
            counts["glyph_name"] += 1
            if len(samples["glyph_name"]) < 5:
                samples["glyph_name"].append([f"U+{cp:04X}", target_glyph, reference_glyph])
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
        if compare_glyphs:
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
                counts["diagnostic_instructions"] += 1
                if len(samples["diagnostic_instructions"]) < 5:
                    samples["diagnostic_instructions"].append(
                        [
                            f"U+{cp:04X}",
                            target_glyph,
                            hashlib.sha256(target_sig["instructions"]).hexdigest()[:16],
                            hashlib.sha256(reference_sig["instructions"]).hexdigest()[:16],
                        ]
                    )
    return {"counts": counts, "samples": {key: value for key, value in samples.items() if value}}


def freetype_render_signature(face: freetype.Face, codepoint: int) -> tuple[Any, ...]:
    face.load_char(codepoint, freetype.FT_LOAD_DEFAULT)
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


def compact_render_signature(signature: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "left": signature[0],
        "top": signature[1],
        "advance": [signature[2], signature[3]],
        "size": [signature[4], signature[5]],
        "pixel_mode": signature[6],
        "bitmap_sha256": hashlib.sha256(signature[7]).hexdigest()[:16],
    }


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
        "ppems": list(task["ppems"]),
        "counts": {"raster_mismatch": 0, "render_error": 0},
        "mismatches_by_ppem": {},
        "samples": {},
    }
    if not target_path.exists() or not reference_path.exists():
        item["missing"] = True
        return item

    target_font = TTFont(target_path, lazy=True)
    reference_font = TTFont(reference_path, lazy=True)
    try:
        target_cmap = set((target_font.getBestCmap() or {}).keys())
        reference_cmap = set((reference_font.getBestCmap() or {}).keys())
    finally:
        target_font.close()
        reference_font.close()
    codepoints = sorted((target_cmap & reference_cmap) - set(task["skip_codepoints"]))
    target_face = freetype.Face(str(target_path))
    reference_face = freetype.Face(str(reference_path))
    mismatch_samples: list[Any] = []
    error_samples: list[Any] = []
    for ppem in task["ppems"]:
        target_face.set_pixel_sizes(0, ppem)
        reference_face.set_pixel_sizes(0, ppem)
        ppem_mismatches = 0
        for codepoint in codepoints:
            try:
                target_signature = freetype_render_signature(target_face, codepoint)
                reference_signature = freetype_render_signature(reference_face, codepoint)
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
    item["glyphs_per_ppem"] = len(codepoints)
    item["renders_checked"] = len(codepoints) * len(task["ppems"])
    if mismatch_samples:
        item["samples"]["raster_mismatch"] = mismatch_samples
    if error_samples:
        item["samples"]["render_error"] = error_samples
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
            skip_codepoints = INTENTIONAL_CPS | classical_skip_codepoints(region, weight)
            for italic in (False, True):
                target_path = static_path(region, weight, italic, True)
                reference_path = b.hinted_reference_font_path(region, weight, italic)
                tasks.append(
                    {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "target_path": str(target_path),
                        "reference_path": str(reference_path),
                        "target": display_path(target_path),
                        "reference": display_path(reference_path),
                        "ppems": ppems,
                        "skip_codepoints": skip_codepoints,
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


def shape_signature_data(
    data: bytes,
    glyph_order: list[str],
    text: str,
    script: str,
    features: dict[str, bool],
    direction: str | None = None,
    variations: dict[str, float] | None = None,
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
    buffer.language = "ZHS"
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


def shape_signature(
    path: Path,
    text: str,
    script: str,
    features: dict[str, bool],
    direction: str | None = None,
    variations: dict[str, float] | None = None,
) -> list[list[Any]]:
    data = path.read_bytes()
    font = TTFont(path)
    try:
        return shape_signature_data(
            data,
            font.getGlyphOrder(),
            text,
            script,
            features,
            direction=direction,
            variations=variations,
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


def single_pos_value_signature(subtable: Any) -> dict[str, list[Any] | None]:
    if hasattr(subtable, "ExtSubTable"):
        return single_pos_value_signature(subtable.ExtSubTable)
    if not hasattr(subtable, "Coverage") or not getattr(subtable, "Coverage", None):
        return {}
    glyphs = list(subtable.Coverage.glyphs or [])
    if getattr(subtable, "Format", 1) == 2:
        return {
            glyph_name: value_record_signature(value)
            for glyph_name, value in zip(glyphs, list(getattr(subtable, "Value", []) or []))
        }
    value = value_record_signature(getattr(subtable, "Value", None))
    return {glyph_name: value for glyph_name in glyphs}


def palt_value_signature(font: TTFont) -> dict[str, list[Any] | None]:
    if "GPOS" not in font or not font["GPOS"].table.LookupList:
        return {}
    table = font["GPOS"].table
    values: dict[str, list[Any] | None] = {}
    for lookup_index in b.feature_lookup_indices(font, "GPOS", {"palt"}):
        if lookup_index >= len(table.LookupList.Lookup):
            continue
        lookup = table.LookupList.Lookup[lookup_index]
        for subtable_index, subtable in enumerate(lookup.SubTable or []):
            for glyph_name, value in single_pos_value_signature(subtable).items():
                values[f"{lookup_index}:{subtable_index}:{glyph_name}"] = value
    return values


def audit_static_palt_shaping() -> list[dict[str, Any]]:
    out = []
    regions = ["SC", "TC", "HC", "J", "K"]
    total = len(regions) * 2 * 2 * len(EXACT_WEIGHTS)
    done = 0
    for region in regions:
        for hinted in [False, True]:
            for italic in [False, True]:
                for weight in EXACT_WEIGHTS:
                    done += 1
                    target_path = static_path(region, weight, italic, hinted)
                    ref_path = b.static_reference_font_path(region, weight, italic)
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
                        "counts": {"palt_kana_shape": 0, "palt_lookup_values": 0},
                        "samples": {},
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    target_shape = shape_signature(target_path, "かなカナ", "kana", {"palt": True})
                    reference_shape = shape_signature(ref_path, "かなカナ", "kana", {"palt": True})
                    if target_shape != reference_shape:
                        item["counts"]["palt_kana_shape"] = 1
                        item["samples"]["palt_kana_shape"] = sample_pair(target_shape, reference_shape)
                    target_font = TTFont(target_path)
                    reference_font = TTFont(ref_path)
                    try:
                        target_values = palt_value_signature(target_font)
                        reference_values = palt_value_signature(reference_font)
                        mismatches = []
                        for key, target_value in target_values.items():
                            reference_value = reference_values.get(key)
                            if target_value != reference_value:
                                mismatches.append([key, target_value, reference_value])
                        if mismatches:
                            item["counts"]["palt_lookup_values"] = len(mismatches)
                            item["samples"]["palt_lookup_values"] = mismatches[:8]
                    finally:
                        target_font.close()
                        reference_font.close()
                    out.append(item)
    return out


def audit_static_em_dash_shaping() -> list[dict[str, Any]]:
    out = []
    weights = [str(stop["name"]) for stop in b.SOURCE_HAN_WEIGHT_STOPS]
    feature_cases = [
        ["calt", {"calt": True}],
        ["vert", {"calt": True, "vert": True}],
        ["vrt2", {"calt": True, "vrt2": True}],
    ]
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
                        "counts": {f"em_dash_{name}_shape": 0 for name, _features in feature_cases},
                        "samples": {},
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                        out.append(item)
                        continue
                    for name, features in feature_cases:
                        target_shape = shape_signature(target_path, "——", "hani", features)
                        reference_shape = shape_signature(ref_path, "——", "hani", features)
                        if target_shape != reference_shape:
                            key = f"em_dash_{name}_shape"
                            item["counts"][key] = 1
                            item["samples"][key] = sample_pair(target_shape, reference_shape)
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
    return [
        [record.FeatureTag, list(record.Feature.LookupListIndex or [])]
        for record in feature_records(font, "GPOS")
        if record.FeatureTag not in CONTEXTUAL_SPACING_FEATURES
    ]


def contextual_spacing_feature_lookup_signature(font: TTFont) -> dict[str, list[list[int]]]:
    out = {tag: [] for tag in sorted(CONTEXTUAL_SPACING_FEATURES)}
    for record in feature_records(font, "GPOS"):
        if record.FeatureTag in CONTEXTUAL_SPACING_FEATURES:
            out[record.FeatureTag].append(list(record.Feature.LookupListIndex or []))
    return out


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
                        reference_gsub_tags = feature_tag_sequence(reference, "GSUB")
                        if target_gsub_tags != reference_gsub_tags:
                            item["counts"]["gsub_feature_record_sequence"] = 1
                            item["samples"]["gsub_feature_record_sequence"] = sample_pair(
                                target_gsub_tags, reference_gsub_tags
                            )
                        target_empty = empty_cv_ss_sequence(target, "GSUB")
                        reference_empty = empty_cv_ss_sequence(reference, "GSUB")
                        if target_empty != reference_empty:
                            item["counts"]["gsub_empty_cv_ss_sequence"] = 1
                            item["samples"]["gsub_empty_cv_ss_sequence"] = sample_pair(target_empty, reference_empty)

                        target_gsub_index = langsys_signatures(target, "GSUB", by_tag=False)
                        reference_gsub_index = langsys_signatures(reference, "GSUB", by_tag=False)
                        if target_gsub_index != reference_gsub_index:
                            item["counts"]["gsub_langsys_index_order"] = 1
                            item["samples"]["gsub_langsys_index_order"] = sample_pair(
                                target_gsub_index, reference_gsub_index
                            )
                        target_gsub_tag = langsys_signatures(target, "GSUB", by_tag=True)
                        reference_gsub_tag = langsys_signatures(reference, "GSUB", by_tag=True)
                        if target_gsub_tag != reference_gsub_tag:
                            item["counts"]["gsub_langsys_tag_order"] = 1
                            item["samples"]["gsub_langsys_tag_order"] = sample_pair(target_gsub_tag, reference_gsub_tag)

                        target_gpos_feature = base_gpos_feature_lookup_signature(target)
                        reference_gpos_feature = feature_lookup_signature(reference, "GPOS")
                        if target_gpos_feature != reference_gpos_feature:
                            item["counts"]["gpos_feature_record_structure"] = 1
                            item["samples"]["gpos_feature_record_structure"] = sample_pair(
                                target_gpos_feature, reference_gpos_feature
                            )
                        target_gpos_tags = langsys_signatures(target, "GPOS", by_tag=True)
                        target_gpos_base = [
                            [script, lang, req if req not in CONTEXTUAL_SPACING_FEATURES else None,
                             [tag for tag in tags if tag not in CONTEXTUAL_SPACING_FEATURES]]
                            for script, lang, req, tags in target_gpos_tags
                        ]
                        reference_gpos_base = langsys_signatures(reference, "GPOS", by_tag=True)
                        if target_gpos_base != reference_gpos_base:
                            item["counts"]["gpos_langsys_index_order"] = 1
                            item["samples"]["gpos_langsys_index_order"] = sample_pair(
                                target_gpos_base, reference_gpos_base
                            )
                        target_gpos_lookup = gpos_lookup_signature(target)
                        reference_gpos_lookup = gpos_lookup_signature(reference)
                        contextual_features = contextual_spacing_feature_lookup_signature(target)
                        base_lookup_count = len(reference_gpos_lookup)
                        expected_contextual_features = {
                            "chws": [[base_lookup_count, base_lookup_count + 2]],
                            "vchw": [[base_lookup_count + 3, base_lookup_count + 5]],
                        }
                        lookup_prefix_ok = target_gpos_lookup[:base_lookup_count] == reference_gpos_lookup
                        contextual_lookup_count_ok = len(target_gpos_lookup) == base_lookup_count + 6
                        contextual_feature_links_ok = contextual_features == expected_contextual_features
                        if not (
                            lookup_prefix_ok
                            and contextual_lookup_count_ok
                            and contextual_feature_links_ok
                        ):
                            item["counts"]["gpos_lookup_structure"] = 1
                            item["samples"]["gpos_lookup_structure"] = sample_pair(
                                {
                                    "base_prefix": target_gpos_lookup[:base_lookup_count],
                                    "appended": target_gpos_lookup[base_lookup_count:],
                                    "contextual_features": contextual_features,
                                },
                                {
                                    "base_prefix": reference_gpos_lookup,
                                    "appended_count": 6,
                                    "contextual_features": expected_contextual_features,
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
                            if "locl" not in latn_cat:
                                item["counts"]["cl_latn_cat_locl"] = 1
                                item["samples"]["cl_latn_cat_locl"] = latn_cat
                    finally:
                        target.close()
                        reference.close()
                    out.append(item)
    return out


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
            if len(files) != 14:
                failures.append({"kind": "static_count", "region": region, "hinted": hinted, "count": len(files)})
            for weight, expected_weight in EXPECTED_WEIGHTS.items():
                for italic in [False, True]:
                    path = static_path(region, weight, italic, hinted)
                    if not path.exists():
                        failures.append({"kind": "missing_static", "file": str(path.relative_to(ROOT))})
                        continue
                    font = TTFont(path)
                    try:
                        name_hits = [text for text in names(font) if "UI" in text][:5]
                        version_strings = [record.toUnicode() for record in font["name"].names if record.nameID == 5]
                        item = {
                            "file": str(path.relative_to(ROOT)),
                            "weight": font["OS/2"].usWeightClass,
                            "vendor": font["OS/2"].achVendID,
                            "head_font_revision": float(font["head"].fontRevision),
                            "name_id_5": version_strings,
                            "has_fvar": "fvar" in font,
                            "has_gvar": "gvar" in font,
                            "has_stat": "STAT" in font,
                            "has_hint_tables": any(tag in font for tag in ("fpgm", "prep", "cvt ")),
                            "glyph_program_count": glyph_program_count(font) if hinted else None,
                            "uppercase_UI_names": name_hits,
                            "colon": colon_status(path),
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
                        if item["has_fvar"] or item["has_gvar"] or not item["has_stat"]:
                            failures.append({"kind": "static_tables", "file": item["file"], "item": item})
                        if hinted and (not item["has_hint_tables"] or not item["glyph_program_count"]):
                            failures.append({"kind": "static_missing_hints", "file": item["file"], "programs": item["glyph_program_count"]})
                        if (not hinted) and item["has_hint_tables"]:
                            failures.append({"kind": "unhinted_has_hint_tables", "file": item["file"]})
                        if name_hits:
                            failures.append({"kind": "uppercase_UI_name", "file": item["file"], "samples": name_hits})
                        if not item["colon"]["ok"]:
                            failures.append({"kind": "colon_shape", "file": item["file"], "colon": item["colon"]})
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
                failures.append({"kind": "missing_vf", "file": str(path.relative_to(ROOT))})
                continue
            font = TTFont(path)
            try:
                name_hits = [text for text in names(font) if "UI" in text][:5]
                version_strings = [record.toUnicode() for record in font["name"].names if record.nameID == 5]
                instance_weights = sorted({int(instance.coordinates.get("wght")) for instance in font["fvar"].instances}) if "fvar" in font else []
                item = {
                    "file": str(path.relative_to(ROOT)),
                    "vendor": font["OS/2"].achVendID,
                    "head_font_revision": float(font["head"].fontRevision),
                    "name_id_5": version_strings,
                    "axes": axes(font),
                    "instances": instance_weights,
                    "has_fvar": "fvar" in font,
                    "has_gvar": "gvar" in font,
                    "has_stat": "STAT" in font,
                    "uppercase_UI_names": name_hits,
                    "colon": colon_status(path),
                    "contextual_spacing": contextual_spacing_status(path, {"wght": 400}),
                }
                variable_count += 1
                if len(samples["variable"]) < 4:
                    samples["variable"].append(item)
                if item["vendor"] != b.OS2_VENDOR_ID:
                    failures.append({"kind": "vf_vendor", "file": item["file"], "got": item["vendor"]})
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
                if not item["has_fvar"] or not item["has_gvar"] or not item["has_stat"]:
                    failures.append({"kind": "vf_tables", "file": item["file"], "item": item})
                if name_hits:
                    failures.append({"kind": "uppercase_UI_name", "file": item["file"], "samples": name_hits})
                if not item["colon"]["ok"]:
                    failures.append({"kind": "colon_shape", "file": item["file"], "colon": item["colon"]})
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
                    ref_path = (b.hinted_reference_font_path if hinted else b.reference_font_path)(region, weight, italic)
                    log(f"static exact {done}/{total}: {region} {weight}{' Italic' if italic else ''} {'hinted' if hinted else 'unhinted'}")
                    item: dict[str, Any] = {
                        "region": region,
                        "weight": weight,
                        "italic": italic,
                        "hinted": hinted,
                        "target": display_path(target_path),
                        "reference": display_path(ref_path),
                    }
                    if not target_path.exists() or not ref_path.exists():
                        item["missing"] = True
                    else:
                        target = TTFont(target_path)
                        reference = TTFont(ref_path)
                        try:
                            skip_codepoints = classical_skip_codepoints(region, weight)
                            if skip_codepoints:
                                item["intentional_skip"] = {
                                    "reason": f"CL 跟随 Shanggu Sans {b.SHANGGU_TAG} 官方静态 TTF/VF，不再以 Sarasa {b.SARASA_VERSION} 内置旧 subset 为 exact 轮廓基线。",
                                    "codepoints": len(skip_codepoints),
                                }
                            item.update(compare_fonts(target, reference, compare_glyphs=True, skip_codepoints=skip_codepoints))
                        finally:
                            target.close()
                            reference.close()
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
                    "counts": {
                        "extra_cmap": 0,
                        "missing_cmap": 0,
                        "extra_gsub_features": 0,
                        "extra_gpos_features": 0,
                        "advance_mismatch": 0,
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
                    target_cmap = target.getBestCmap() or {}
                    reference_cmap = reference.getBestCmap() or {}
                    extra_cmap = sorted(set(target_cmap) - set(reference_cmap))
                    missing_cmap = sorted(set(reference_cmap) - set(target_cmap))
                    extra_gsub = sorted(layout_feature_tags(target, "GSUB") - layout_feature_tags(reference, "GSUB"))
                    extra_gpos = sorted(
                        layout_feature_tags(target, "GPOS")
                        - layout_feature_tags(reference, "GPOS")
                        - CONTEXTUAL_SPACING_FEATURES
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
                        reference_glyph = reference_cmap.get(codepoint)
                        if not target_glyph or not reference_glyph:
                            continue
                        target_h = target["hmtx"].metrics.get(target_glyph)
                        reference_h = reference["hmtx"].metrics.get(reference_glyph)
                        if target_h and reference_h and target_h[0] != reference_h[0]:
                            item["counts"]["advance_mismatch"] += 1
                            advance_samples.append([f"U+{codepoint:04X}", target_h[0], reference_h[0]])
                    if advance_samples:
                        item["samples"]["advance_mismatch"] = advance_samples
                finally:
                    target.close()
                    reference.close()
                out.append(item)
    return out


def audit_vf_metrics() -> list[dict[str, Any]]:
    out = []
    weights = [("Light", 300), ("Bold", 700)]
    regions = b.variable_regions(b.REGION_ORDER)
    total = len(regions) * 2 * len(weights)
    done = 0
    for region in regions:
        for italic in [False, True]:
            variable_path = vf_path(region, italic)
            for weight_name, weight_value in weights:
                done += 1
                ref_path = b.reference_font_path(region, weight_name, italic)
                log(f"VF exact metrics {done}/{total}: {region} {weight_name}{' Italic' if italic else ''}")
                item: dict[str, Any] = {
                    "region": region,
                    "weight": weight_name,
                    "wght": weight_value,
                    "italic": italic,
                    "target": display_path(variable_path),
                    "reference": display_path(ref_path),
                }
                if not variable_path.exists() or not ref_path.exists():
                    item["missing"] = True
                else:
                    variable = TTFont(variable_path)
                    reference = TTFont(ref_path)
                    try:
                        instance = instantiateVariableFont(variable, {"wght": weight_value}, inplace=False, optimize=True)
                        try:
                            skip_codepoints = classical_skip_codepoints(region, weight_name)
                            if skip_codepoints:
                                item["intentional_skip"] = {
                                    "reason": f"CL 跟随 Shanggu Sans {b.SHANGGU_TAG} 官方静态 TTF/VF，不再以 Sarasa {b.SARASA_VERSION} 内置旧 subset 为 exact 轮廓基线。",
                                    "codepoints": len(skip_codepoints),
                                }
                            item.update(compare_fonts(instance, reference, compare_glyphs=False, skip_codepoints=skip_codepoints))
                        finally:
                            instance.close()
                    finally:
                        variable.close()
                        reference.close()
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


def nonzero(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bad = []
    for item in items:
        counts = item.get("counts", {})
        failing_counts = {
            key: value
            for key, value in counts.items()
            if key != "skipped_intentional" and not key.startswith("diagnostic_")
        }
        if item.get("missing") or any(failing_counts.values()):
            bad.append(
                {
                    "region": item.get("region"),
                    "weight": item.get("weight"),
                    "wght": item.get("wght"),
                    "italic": item.get("italic"),
                    "hinted": item.get("hinted"),
                    "target": item.get("target"),
                    "missing": item.get("missing", False),
                    "counts": counts,
                    "intentional_skip": item.get("intentional_skip"),
                    "samples": item.get("samples", {}),
                }
            )
    return bad


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审计 Sarasa Ui PropDigits 发布字体")
    parser.add_argument("--skip-raster", action="store_true", help="跳过 FreeType 栅格审计")
    parser.add_argument("--raster-only", action="store_true", help="只运行 FreeType 栅格审计")
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
    ppems = tuple(int(value.strip()) for value in args.raster_ppems.split(",") if value.strip())
    if not ppems or any(value <= 0 for value in ppems):
        raise ValueError("--raster-ppems must contain positive integers")
    raster_regions = parse_csv(args.raster_regions, b.REGION_ORDER, "regions")
    raster_weights = parse_csv(args.raster_weights, EXACT_WEIGHTS, "weights")

    if args.raster_only:
        metadata = {"failures": [], "static_count": 0, "variable_count": 0, "samples": {}}
        static_exact: list[dict[str, Any]] = []
        cl_boundaries: list[dict[str, Any]] = []
        static_layout_templates: list[dict[str, Any]] = []
        static_palt_shaping: list[dict[str, Any]] = []
        static_em_dash_shaping: list[dict[str, Any]] = []
        vf_metrics: list[dict[str, Any]] = []
        vf_contextual_spacing: list[dict[str, Any]] = []
    else:
        metadata = audit_metadata()
        static_exact = audit_static_exact()
        cl_boundaries = audit_static_cl_boundaries()
        static_layout_templates = audit_static_layout_templates()
        static_palt_shaping = audit_static_palt_shaping()
        static_em_dash_shaping = audit_static_em_dash_shaping()
        vf_metrics = audit_vf_metrics()
        vf_contextual_spacing = audit_vf_contextual_spacing()

    static_raster = [] if args.skip_raster else audit_static_raster(
        ppems,
        args.raster_jobs,
        regions=raster_regions,
        weights=raster_weights,
    )
    summary = {
        "metadata_failures": len(metadata["failures"]),
        "static_exact_failures": len(nonzero(static_exact)),
        "static_raster_failures": len(nonzero(static_raster)),
        "cl_static_boundary_failures": len(nonzero(cl_boundaries)),
        "static_layout_template_failures": len(nonzero(static_layout_templates)),
        "static_palt_shaping_failures": len(nonzero(static_palt_shaping)),
        "static_em_dash_shaping_failures": len(nonzero(static_em_dash_shaping)),
        "vf_metric_failures": len(nonzero(vf_metrics)),
        "vf_contextual_spacing_failures": len(nonzero(vf_contextual_spacing)),
        "static_fonts_checked": metadata["static_count"],
        "variable_fonts_checked": metadata["variable_count"],
        "static_exact_cases": len(static_exact),
        "static_raster_cases": len(static_raster),
        "static_raster_renders": sum(item.get("renders_checked", 0) for item in static_raster),
        "cl_static_boundary_cases": len(cl_boundaries),
        "static_layout_template_cases": len(static_layout_templates),
        "static_palt_shaping_cases": len(static_palt_shaping),
        "static_em_dash_shaping_cases": len(static_em_dash_shaping),
        "vf_metric_cases": len(vf_metrics),
        "vf_contextual_spacing_cases": len(vf_contextual_spacing),
    }
    report = {
        "title": "Sarasa Ui PropDigits 多地区发布前审计",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "toolchain": {
            "freetype_py": importlib.metadata.version("freetype-py"),
            "freetype": list(freetype.version()),
            "fonttools": importlib.metadata.version("fonttools"),
            "uharfbuzz": importlib.metadata.version("uharfbuzz"),
        },
        "summary": summary,
        "metadata_failures": metadata["failures"],
        "static_exact_nonzero": nonzero(static_exact),
        "static_raster_nonzero": nonzero(static_raster),
        "cl_static_boundary_nonzero": nonzero(cl_boundaries),
        "static_layout_template_nonzero": nonzero(static_layout_templates),
        "static_palt_shaping_nonzero": nonzero(static_palt_shaping),
        "static_em_dash_shaping_nonzero": nonzero(static_em_dash_shaping),
        "vf_metrics_nonzero": nonzero(vf_metrics),
        "vf_contextual_spacing_nonzero": nonzero(vf_contextual_spacing),
        "static_exact": static_exact,
        "static_raster": static_raster,
        "cl_static_boundaries": cl_boundaries,
        "static_layout_templates": static_layout_templates,
        "static_palt_shaping": static_palt_shaping,
        "static_em_dash_shaping": static_em_dash_shaping,
        "vf_metrics": vf_metrics,
        "vf_contextual_spacing": vf_contextual_spacing,
        "metadata_samples": metadata["samples"],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("summary " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    failure_keys = [key for key in summary if key.endswith("_failures")]
    if any(summary[key] for key in failure_keys):
        log(f"audit FAILED; see {display_path(args.report)}")
        raise SystemExit(1)
    log(f"audit PASSED; wrote {display_path(args.report)}")


if __name__ == "__main__":
    main()
