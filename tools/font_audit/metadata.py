"""Independent OpenType metadata checks and legal-source verification."""
from __future__ import annotations

from fontTools.otlLib.maxContextCalc import maxCtxFont
from fontTools.ttLib import TTFont

from . import contract as c
from .results import Result


def head_bounds_status(font: TTFont) -> dict:
    boxes = [(glyph.xMin, glyph.yMin, glyph.xMax, glyph.yMax)
             for name in font.getGlyphOrder() for glyph in [font["glyf"][name]]
             if getattr(glyph, "numberOfContours", 0)]
    expected = (min(box[0] for box in boxes), min(box[1] for box in boxes),
                max(box[2] for box in boxes), max(box[3] for box in boxes)) if boxes else (0, 0, 0, 0)
    actual = tuple(getattr(font["head"], field) for field in ("xMin", "yMin", "xMax", "yMax"))
    return {"ok": actual == expected, "actual": actual, "expected": expected}


def referenced_name_ids(font: TTFont) -> set[int]:
    result = set()
    if "fvar" in font:
        result.update(axis.axisNameID for axis in font["fvar"].axes)
        for instance in font["fvar"].instances:
            result.add(instance.subfamilyNameID)
            if instance.postscriptNameID != 0xFFFF:
                result.add(instance.postscriptNameID)
    if "STAT" in font:
        stat = font["STAT"].table
        result.update(axis.AxisNameID for axis in stat.DesignAxisRecord.Axis)
        result.update(value.ValueNameID for value in getattr(stat.AxisValueArray, "AxisValue", []))
        if hasattr(stat, "ElidedFallbackNameID"):
            result.add(stat.ElidedFallbackNameID)
    for tag in ("GSUB", "GPOS"):
        if tag not in font:
            continue
        for record in font[tag].table.FeatureList.FeatureRecord:
            params = record.Feature.FeatureParams
            if params:
                result.update(value for name, value in vars(params).items()
                              if name.endswith("NameID") and isinstance(value, int) and value != 0xFFFF)
    return result


def check_font(path, region: str, italic: bool, weight: str | None, hinted: bool) -> dict:
    result = Result()
    with TTFont(path, recalcBBoxes=False, recalcTimestamp=False) as font:
        os2, head, post = font["OS/2"], font["head"], font["post"]
        expected_weight = 400 if weight is None else c.WEIGHTS[weight]
        names = {record.nameID: record.toUnicode() for record in font["name"].names}
        checks = {
            "units_per_em": head.unitsPerEm == 1000,
            "font_revision": abs(head.fontRevision - float(c.OPENTYPE_VERSION)) <= 1 / 65536,
            "version_name": names.get(5, "").startswith(f"Version {c.OPENTYPE_VERSION}"),
            "region_family": region in names.get(1, "") and "Ui" in names.get(1, ""),
            "family_length": all(len(record.toUnicode()) <= 31 for record in font["name"].names if record.nameID == 1),
            "no_mac_names": all(record.platformID != 1 for record in font["name"].names),
            "name_references": referenced_name_ids(font) <= set(names),
            "weight_class": os2.usWeightClass == expected_weight,
            "italic_flag": bool(os2.fsSelection & 1) == italic and bool(head.macStyle & 2) == italic,
            "italic_angle": abs(post.italicAngle - (-9.4 if italic else 0)) < .01,
            "panose_weight": os2.panose.bWeight == 1 + expected_weight // 100,
            "max_context": os2.usMaxContext >= maxCtxFont(font),
            "head_bounds": head_bounds_status(font)["ok"],
            "vertical_metrics": "vmtx" in font and "vhea" in font,
            "gpos_pair_record_order": pair_record_order(font),
        }
        # Regional code pages and the Regular (VF default instance) vertical
        # padding follow the pinned regional release, not build constants.
        with TTFont(c.reference_path(region, "Regular", False), lazy=True) as release:
            checks["codepage_ranges"] = ((os2.ulCodePageRange1, os2.ulCodePageRange2)
                                         == (release["OS/2"].ulCodePageRange1, release["OS/2"].ulCodePageRange2))
        if weight in (None, "Regular"):
            with TTFont(c.reference_path(region, "Regular", italic, hinted and weight is not None), lazy=True) as release:
                checks["regular_vertical_bounds"] = (head.yMin, head.yMax) == (release["head"].yMin, release["head"].yMax)
        if weight is None:
            axes = [(a.axisTag, a.minValue, a.defaultValue, a.maxValue) for a in font["fvar"].axes]
            checks["variable_axis"] = axes == [("wght", 200, 400, 900)]
            checks["named_weights"] = sorted(i.coordinates["wght"] for i in font["fvar"].instances) == sorted(c.WEIGHTS.values())
            checks["variation_metrics"] = all(tag in font for tag in ("gvar", "HVAR", "MVAR")) and "VVAR" not in font and "VORG" not in font
            checks["strikeout_variation"] = "strs" in {record.ValueTag for record in font["MVAR"].table.ValueRecord}
        else:
            checks["static_post3"] = post.formatType == 3
            checks["no_variable_tables"] = not any(tag in font for tag in ("fvar", "gvar", "HVAR", "VVAR", "MVAR"))
        for name, passed in checks.items():
            result.check(passed, {"check": name})
    return result.report(font=c.display_path(path), region=region, italic=italic, weight=weight, hinted=hinted)


def pair_record_order(font):
    if 'GPOS' not in font:
        return False
    for lookup in font['GPOS'].table.LookupList.Lookup:
        for outer in lookup.SubTable:
            part = getattr(outer, 'ExtSubTable', outer)
            for pair_set in getattr(part, 'PairSet', ()):
                ids = [font.getGlyphID(record.SecondGlyph) for record in pair_set.PairValueRecord]
                if ids != sorted(set(ids)):
                    return False
    return True


def source_han_license_documents_status() -> dict:
    result = Result()
    with TTFont(c.cjk_path("SC"), lazy=True) as source:
        copyright_text = source["name"].getDebugName(0)
    license_path, notice_path, *copies = c.release_license_document_paths()
    original = license_path.read_bytes() if license_path.is_file() else b""
    for path in [license_path, *copies]:
        payload = path.read_bytes() if path.is_file() else b""
        result.check(bool(payload) and payload == original and copyright_text in payload.decode("utf-8"),
                     {"file": c.display_path(path), "check": "source_han_copyright_and_exact_copy"})
    notice = notice_path.read_text("utf-8") if notice_path.is_file() else ""
    result.check(copyright_text.split(", with Reserved Font Name", 1)[0] in notice,
                 {"file": c.display_path(notice_path), "check": "source_han_notice"})
    return {**result.report(), "ok": result.failures == 0, "documents_checked": result.cases,
            "document_manifest": c.manifest(c.release_license_document_paths())}
