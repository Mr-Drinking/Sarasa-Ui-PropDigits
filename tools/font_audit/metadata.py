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


def left_bearing_mismatches(font: TTFont) -> list[str]:
    """Glyphs whose stored xMin is not their left side bearing.

    FreeType, HarfBuzz and the Windows rasterizer place an outline by both,
    so recalculating the bounds of Sarasa's italics (which keep conservative
    ones) without the bearing moves them; a v1.0.43.1 pilot build moved
    20,378 HeavyItalic glyphs 1 unit that way.
    """
    glyf, metrics = font["glyf"], font["hmtx"].metrics
    return [name for name in font.getGlyphOrder()
            if getattr(glyf[name], "numberOfContours", 0) and glyf[name].xMin != metrics[name][1]]


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


def layout_routes(table) -> dict:
    """{(script, language): {tag: lookups}}, as HarfBuzz selects them: the
    first record of a tag in a language system applies, never a fallback."""
    result = {}
    if not table.ScriptList or not table.FeatureList:
        return result
    for record in table.ScriptList.ScriptRecord:
        script = record.Script
        systems = ([("dflt", script.DefaultLangSys)] if script.DefaultLangSys else []) + [
            (entry.LangSysTag.strip(), entry.LangSys) for entry in script.LangSysRecord]
        for language, system in systems:
            indices = list(system.FeatureIndex)
            if system.ReqFeatureIndex != 0xFFFF:
                indices.insert(0, system.ReqFeatureIndex)
            features = {}
            for index in indices:
                feature = table.FeatureList.FeatureRecord[index]
                features.setdefault(feature.FeatureTag, frozenset(feature.Feature.LookupListIndex))
            result[(record.ScriptTag, language)] = features
    return result


def route_structure_differences(font: TTFont, native: TTFont, own_routes=("vert", "vrt2")) -> list[str]:
    """Where the VF routes features unlike the native static, whatever the numbering.

    Each language system carries the same tags; per tag the language systems
    share or separate lookups as in the static. Vertical substitutions may
    share one VF route where the static repeats equal ones per language.
    """
    problems = []
    for tag in ("GSUB", "GPOS"):
        actual, expected = layout_routes(font[tag].table), layout_routes(native[tag].table)
        for key in sorted(set(actual) | set(expected)):
            if set(actual.get(key, {})) != set(expected.get(key, {})) or (key in actual) != (key in expected):
                problems.append(f"{tag} {'/'.join(key)}: {sorted(set(actual.get(key, {})) ^ set(expected.get(key, {})))}")
        for feature in sorted({name for features in expected.values() for name in features} - set(own_routes)):
            def groups(routes):
                shared = {}
                for key, features in routes.items():
                    if feature in features:
                        shared.setdefault(features[feature], set()).add(key)
                return {frozenset(keys) for keys in shared.values()}
            if groups(actual) != groups(expected):
                problems.append(f"{tag} {feature}: 语言分组不同")
    return problems


def gvar_hvar_advance_mismatches(font: TTFont) -> list:
    """Glyphs whose gvar phantom advance and HVAR advance disagree at an axis point.

    HarfBuzz and FreeType add the rounded HVAR delta; an instancer that reads
    gvar takes the phantom points (U+2014 at 700 was 907 there, 908 in HVAR).
    """
    from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
    from fontTools.varLib.models import normalizeValue, piecewiseLinearMap, supportScalar
    from fontTools.varLib.varStore import VarStoreInstancer
    axis = font["fvar"].axes[0]
    segments = font["avar"].segments.get(axis.axisTag) if "avar" in font else None
    locations = {}
    for weight in c.AXIS_POINTS:
        value = normalizeValue(weight, (axis.minValue, axis.defaultValue, axis.maxValue))
        value = piecewiseLinearMap(value, segments) if segments else value
        if value:
            locations[weight] = {axis.axisTag: floatToFixedToFloat(value, 14)}
    hvar = font["HVAR"].table
    instancers = {weight: VarStoreInstancer(hvar.VarStore, font["fvar"].axes, location)
                  for weight, location in locations.items()}
    mapping = hvar.AdvWidthMap.mapping if hvar.AdvWidthMap else None
    glyf, variations, mismatches = font["glyf"], font["gvar"].variations, []
    for gid, name in enumerate(font.getGlyphOrder()):
        glyph = glyf[name]
        points = len(glyph.components) if glyph.isComposite() else (
            len(glyph.getCoordinates(glyf)[0]) if glyph.numberOfContours else 0)
        default = font["hmtx"][name][0]
        for weight, location in locations.items():
            phantom = float(default)
            for variation in variations.get(name, []):
                scalar = supportScalar(location, variation.axes)
                if scalar and len(variation.coordinates) == points + 4:
                    left, right = variation.coordinates[points], variation.coordinates[points + 1]
                    phantom += scalar * ((right[0] if right else 0) - (left[0] if left else 0))
            target = default + otRound(instancers[weight][mapping[name] if mapping else gid])
            if otRound(phantom) != target:
                mismatches.append((name, weight, target, phantom))
    return mismatches


def expected_names(region: str, italic: bool, weight: str | None, hinted: bool) -> dict[int, str]:
    """English name records under the published naming scheme (README)."""
    variable = weight is None
    base = f"Sarasa Ui {'VF ' if variable else ''}PD {region}"
    name = weight or "Regular"
    typographic = ("Italic" if name == "Regular" else f"{name} Italic") if italic else name
    if name in ("Regular", "Bold"):
        family, style = base, typographic
    else:
        family, style = f"{base} {name}", "Italic" if italic else "Regular"
    if variable:
        family = base
    full = base if typographic == "Regular" else f"{base} {typographic}"
    postscript = base.replace(" ", "-") + ("" if variable and typographic == "Regular"
                                           else "-" + typographic.replace(" ", "-"))
    names = {1: family, 2: style, 4: full, 6: postscript, 16: base, 17: typographic,
             3: f"{postscript};{c.VERSION}" + ("" if variable else f";{'hinted' if hinted else 'unhinted'}")}
    if variable:
        names[25] = base.replace(" ", "") + ("Italic" if italic else "")
    return names


# Regional family names: the pinned release's own words, except K, where the
# release has none and the project adds the Korean name.
REGIONAL_NAMES = {"CL": (0x404, "更紗黑體"), "SC": (0x804, "更纱黑体"), "TC": (0x404, "更紗黑體"),
                  "HC": (0xC04, "更紗黑體"), "J": (0x411, "更紗ゴシック"), "K": (0x412, "사라사 고딕")}


def name_records_status(font: TTFont, region: str, italic: bool, weight: str | None, hinted: bool) -> dict:
    """Every family, style and identifier record, in English and the regional language."""
    english = expected_names(region, italic, weight, hinted)
    language, word = REGIONAL_NAMES[region]
    with TTFont(c.reference_path(region, "Regular", False), lazy=True) as release:
        released = [(record.langID, record.toUnicode().split(" UI ", 1)[0]) for record in release["name"].names
                    if record.nameID == 1 and record.platformID == 3 and record.langID != 0x409]
    if released and released != [(language, word)]:
        return {"ok": False, "differences": {"regional_family": (released, (language, word))}, "extra": [], "version": ""}
    expected = {(identifier, 0x409): text for identifier, text in english.items()}
    for identifier in (1, 2, 4, 16, 17):
        text = english[identifier]
        expected[(identifier, language)] = word + text[len("Sarasa"):] if identifier in (1, 4, 16) else text
    actual = {(record.nameID, record.langID): record.toUnicode() for record in font["name"].names
              if record.nameID in {1, 2, 3, 4, 6, 16, 17, 25}}
    version = next((record.toUnicode() for record in font["name"].names if record.nameID == 5), "")
    version_ok = (version.startswith(f"Version {c.OPENTYPE_VERSION}; project {c.VERSION}; ")
                  and version.endswith("; PD" if weight is None else f"; PD; {'hinted' if hinted else 'unhinted'}"))
    differences = {key: (actual.get(key), text) for key, text in expected.items() if actual.get(key) != text}
    extra = sorted(set(actual) - set(expected))
    return {"ok": not differences and not extra and version_ok,
            "differences": {f"{identifier}/{language:#x}": pair for (identifier, language), pair in differences.items()},
            "extra": extra, "version": version}


def glyph_program_count(font: TTFont) -> int:
    glyf = font["glyf"]
    return sum(bool(getattr(glyf[name], "program", None) and glyf[name].program.getBytecode())
               for name in font.getGlyphOrder())


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
            # nameID 1 alone once passed while 2/4/6/16/17/25 went unchecked,
            # and only the last record of each ID was read (K: the Korean one).
            "name_records": name_records_status(font, region, italic, weight, hinted)["ok"],
            "family_length": all(len(record.toUnicode()) <= 31 for record in font["name"].names if record.nameID == 1),
            "no_mac_names": all(record.platformID != 1 for record in font["name"].names),
            "name_references": referenced_name_ids(font) <= set(names),
            "weight_class": os2.usWeightClass == expected_weight,
            "italic_flag": bool(os2.fsSelection & 1) == italic and bool(head.macStyle & 2) == italic,
            "italic_angle": abs(post.italicAngle - (-9.4 if italic else 0)) < .01,
            "panose_weight": os2.panose.bWeight == 1 + expected_weight // 100,
            "max_context": os2.usMaxContext >= maxCtxFont(font),
            "head_bounds": head_bounds_status(font)["ok"],
            "left_side_bearings": bool(head.flags & 2) and not left_bearing_mismatches(font),
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
            checks["gvar_hvar_advances"] = checks["variation_metrics"] and not gvar_hvar_advance_mismatches(font)
            native_path = c.static_path(region, "Regular", italic, False)
            if native_path.is_file():
                with TTFont(native_path, lazy=True) as native:
                    checks["routes_match_static"] = not route_structure_differences(font, native)
            else:
                checks["routes_match_static"] = False
        else:
            checks["static_post3"] = post.formatType == 3
            checks["no_variable_tables"] = not any(tag in font for tag in ("fvar", "gvar", "HVAR", "VVAR", "MVAR"))
        # A hinted font must really carry hints (Heavy: as many instructed
        # glyphs as the hinted Bold); VF and unhinted fonts carry no glyph
        # programs, only the minimal smart-dropout prep.
        programs = glyph_program_count(font)
        if hinted and weight is not None:
            enough = programs > 0
            if weight == "Heavy":
                bold = c.static_path(region, "Bold", italic, True)
                enough = bold.is_file()
                if enough:
                    with TTFont(bold, lazy=True) as reference:
                        enough = programs >= glyph_program_count(reference)
            checks["hint_programs"] = enough and all(tag in font for tag in ("fpgm", "prep", "cvt "))
        else:
            checks["hint_programs"] = programs == 0 and "fpgm" not in font and "cvt " not in font
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
