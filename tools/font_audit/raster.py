"""FreeType execution and pixel measurements, independent of build verdicts."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import freetype
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.ttProgram import Program

from . import contract as c
from .oracles import IDEOGRAPH_RANGES, in_ranges
from .results import Result


MODES = {"normal": (freetype.FT_LOAD_TARGET_NORMAL, freetype.FT_RENDER_MODE_NORMAL),
         "mono": (freetype.FT_LOAD_TARGET_MONO, freetype.FT_RENDER_MODE_MONO),
         "lcd": (freetype.FT_LOAD_TARGET_LCD, freetype.FT_RENDER_MODE_LCD)}
REGRESSION_TEXT = "Haxy0123456789,:;GgÁįķļņŗșț中永更龍龘あㄧ—…⋯⸺⸻"
RASTER_PPEMS = (9, 12, 16, 20, 24)
# The declared Ui pass1 hinting policy (README). The audit applies it to the
# pinned upstream pass1 itself and never reads the product's hint stores.
UI_AUTOHINT_OPTIONS = ("--fallback-script=none", "--fallback-scaling")
UI_AUTOHINT_UNSCALED_CODEPOINTS = (0x0360, 0x22EF)
# Product glyphs that intentionally differ from the regional Sarasa release;
# their shapes and positions are verified against their own source oracles.
DASH_CODEPOINTS = frozenset({0x2014, 0x2015, 0x2E3A, 0x2E3B, 0xFE31})
SAMPLE_LIMIT = 24


def bitmap(face, gid: int, mode="normal") -> tuple:
    flag, render_mode = MODES[mode]
    face.load_glyph(gid, flag | freetype.FT_LOAD_NO_BITMAP)
    face.glyph.render(render_mode)
    value = face.glyph.bitmap
    return (face.glyph.bitmap_left, face.glyph.bitmap_top, value.width, value.rows,
            value.pitch, value.pixel_mode, bytes(value.buffer))


def glyph_program(glyph) -> bytes:
    return glyph.program.getBytecode() if getattr(glyph, "program", None) else b""


class UpstreamHintFailures:
    """Pedantic failures that the pinned upstream release itself produces.

    Sarasa Ui hinted CL keeps a 龍 program that exceeds FreeType's loop budget at
    57-59 px. A failure is inherited only where the product glyph's outline,
    glyph program and CVT are the upstream bytes, and the upstream glyph fails at
    the same size and mode with the same error. fpgm and prep may differ: the
    product adds its own Latin hinting.
    """

    def __init__(self, path, reference):
        self.product, self.reference = TTFont(path, lazy=True), TTFont(reference, lazy=True)
        self.face = freetype.Face(str(reference))
        self.same_cvt = ("cvt " in self.product and "cvt " in self.reference
                         and self.product.getTableData("cvt ") == self.reference.getTableData("cvt "))
        self.codepoints = {}
        for cp, name in sorted(self.product.getBestCmap().items()):
            self.codepoints.setdefault(name, cp)
        self.upstream_cmap = self.reference.getBestCmap()
        self.counterparts = {}

    def counterpart(self, gid: int) -> int | None:
        if gid not in self.counterparts:
            name = self.product.getGlyphOrder()[gid]
            other = self.upstream_cmap.get(self.codepoints.get(name, -1))
            same = False
            if other is not None and self.same_cvt:
                ours, theirs = self.product["glyf"][name], self.reference["glyf"][other]
                same = (glyph_program(ours) == glyph_program(theirs)
                        and ours.getCoordinates(self.product["glyf"]) == theirs.getCoordinates(self.reference["glyf"]))
            self.counterparts[gid] = self.reference.getGlyphID(other) if same else None
        return self.counterparts[gid]

    def fails_alike(self, gid: int, ppem: int, mode: str, error: str) -> bool:
        other = self.counterpart(gid)
        if other is None:
            return False
        self.face.set_pixel_sizes(0, ppem)
        try:
            self.face.load_glyph(other, MODES[mode][0] | freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_PEDANTIC)
        except freetype.FT_Exception as upstream:
            return str(upstream) == error
        return False

    def close(self):
        self.product.close(); self.reference.close()


def execute_hint_programs(path, *, reference=None, full_ppems=(12, 16), regression_ppems=range(6, 201)) -> dict:
    face = freetype.Face(str(path))
    with TTFont(path, lazy=True) as font:
        glyphs = font["maxp"].numGlyphs
        cmap = font.getBestCmap()
        regression = sorted({font.getGlyphID(cmap[ord(char)]) for char in REGRESSION_TEXT if ord(char) in cmap})
    upstream = UpstreamHintFailures(path, reference) if reference is not None else None
    result, inherited, inherited_samples = Result(), 0, []
    try:
        for ppem in sorted(set(full_ppems) | set(regression_ppems)):
            face.set_pixel_sizes(0, ppem)
            selected = range(glyphs) if ppem in full_ppems else regression
            for gid in selected:
                for mode, (flag, _render) in MODES.items():
                    try:
                        face.load_glyph(gid, flag | freetype.FT_LOAD_NO_BITMAP | freetype.FT_LOAD_PEDANTIC)
                    except freetype.FT_Exception as error:
                        detail = {"gid": gid, "ppem": ppem, "mode": mode, "error": str(error)}
                        if upstream is not None and upstream.fails_alike(gid, ppem, mode, str(error)):
                            inherited += 1
                            if len(inherited_samples) < SAMPLE_LIMIT:
                                inherited_samples.append(detail)
                            result.check(True, {})
                        else:
                            result.check(False, detail)
                    else:
                        result.check(True, {})
    finally:
        if upstream is not None:
            upstream.close()
    return result.report(glyphs=glyphs, full_ppems=list(full_ppems), regression_ppems=list(regression_ppems),
                         regression_glyphs=regression, modes=list(MODES),
                         inherited_upstream=inherited, inherited_upstream_samples=inherited_samples)


def compare_pixels(actual_path, expected_path, *, codepoints=None, ppems=(9, 12, 16, 20, 24), mode="normal") -> dict:
    actual, expected = freetype.Face(str(actual_path)), freetype.Face(str(expected_path))
    with TTFont(actual_path, lazy=True) as left, TTFont(expected_path, lazy=True) as right:
        shared = set(left.getBestCmap()) & set(right.getBestCmap())
        cps = sorted(shared if codepoints is None else set(codepoints) & shared)
    result = Result()
    for ppem in ppems:
        actual.set_pixel_sizes(0, ppem); expected.set_pixel_sizes(0, ppem)
        for cp in cps:
            try:
                a = bitmap(actual, actual.get_char_index(cp), mode)
                b = bitmap(expected, expected.get_char_index(cp), mode)
            except freetype.FT_Exception as error:
                result.check(False, {"cp": f"U+{cp:04X}", "ppem": ppem, "error": str(error)})
                continue
            result.check(a == b, {"cp": f"U+{cp:04X}", "ppem": ppem, "mode": mode,
                                 "actual": (*a[:-1], hashlib.sha256(a[-1]).hexdigest()),
                                 "reference": (*b[:-1], hashlib.sha256(b[-1]).hexdigest())})
    return result.report(codepoints=len(cps), ppems=list(ppems), mode=mode)


def render_signature(face, gid: int, flags: int) -> tuple:
    face.load_glyph(gid, flags)
    face.glyph.render(freetype.FT_RENDER_MODE_NORMAL)
    slot = face.glyph
    value = slot.bitmap
    return (int(slot.bitmap_left), int(slot.bitmap_top), int(slot.advance.x), int(slot.advance.y),
            int(value.width), int(value.rows), int(value.pixel_mode), bytes(value.buffer))


def compact(signature: tuple) -> dict:
    return {"left": signature[0], "top": signature[1], "advance": list(signature[2:4]),
            "size": list(signature[4:6]), "bitmap_sha256": hashlib.sha256(signature[7]).hexdigest()[:16]}


def geometry_identity(font: TTFont, gid: int) -> tuple:
    glyph = font["glyf"][font.getGlyphName(gid)]
    if glyph.isComposite():
        return (-1, tuple((font.getGlyphID(component.glyphName), tuple(component.getComponentInfo()[1]))
                          for component in glyph.components))
    if not glyph.numberOfContours:
        return (0,)
    return (glyph.numberOfContours, tuple(glyph.endPtsOfContours), tuple(glyph.coordinates),
            tuple(int(flag) & 1 for flag in glyph.flags))


def bytecode(font: TTFont, gid: int) -> bytes:
    glyph = font["glyf"][font.getGlyphName(gid)]
    return bytes(glyph.program.getBytecode()) if hasattr(glyph, "program") else b""


def ttfautohint_executable() -> Path:
    """The same documented selection as the build: override, PATH, then ttfautohint-py."""
    requested = os.environ.get("TTFAUTOHINT") or shutil.which("ttfautohint")
    if requested:
        path = Path(shutil.which(requested) or requested).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"指定的 ttfautohint 不存在：{path}")
        return path
    import ttfautohint
    return Path(ttfautohint._executable_path()).resolve()


def ttfautohint_identity() -> dict:
    path = ttfautohint_executable()
    version = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    return {"executable": path.name, "sha256": c.sha256(path), "version": version.splitlines()[0] if version else ""}


def hint_work_root() -> Path:
    return Path(os.environ.get("SARASA_HINT_WORK_ROOT", c.ROOT / ".build-cache/hint-work"))


def pass1_candidates(region: str, weight: str, italic: bool) -> list[Path]:
    style = weight + ("Italic" if italic else "")
    return sorted(hint_work_root().glob(f"{weight}-*/fragments/{region}-{style}/pass1.ttf"),
                  key=lambda path: (-path.stat().st_mtime, str(path)))


def prefix_matches(pass1: TTFont, reference: TTFont) -> bool:
    size = len(pass1.getGlyphOrder())
    if len(reference.getGlyphOrder()) < size:
        return False
    return all(geometry_identity(pass1, gid) == geometry_identity(reference, gid) for gid in range(size))


def native_pass1(region: str, weight: str, italic: bool, reference: TTFont) -> Path:
    """Sarasa pass2 keeps the pass1 GID prefix; prove it before using any ID."""
    for path in pass1_candidates(region, weight, italic):
        with TTFont(path, recalcBBoxes=False) as pass1:
            if prefix_matches(pass1, reference):
                return path
    raise FileNotFoundError(f"缺少与固定上游一致的 {region} {weight}{' Italic' if italic else ''} 原生 pass1 片段；"
                            "请先完成静态构建并保留 hint-work 缓存")


def control_text(pass1: TTFont) -> str:
    cmap = pass1.getBestCmap()
    gids = {pass1.getGlyphID(cmap[cp]) for cp in UI_AUTOHINT_UNSCALED_CODEPOINTS if cp in cmap}
    if len(gids) != len(UI_AUTOHINT_UNSCALED_CODEPOINTS):
        raise ValueError("原生 pass1 缺少声明的 fallback 控制码位")
    # The vertical joining segments exist only in the product's derivative.
    # A native pass1 that decomposes dashes would need a new declared policy.
    for lookup in pass1["GSUB"].table.LookupList.Lookup if "GSUB" in pass1 else ():
        for outer in lookup.SubTable:
            part = getattr(outer, "ExtSubTable", outer)
            for source, targets in getattr(part, "mapping", {}).items():
                if isinstance(targets, (list, tuple)) and len(targets) > 1 and source in {cmap.get(cp) for cp in DASH_CODEPOINTS}:
                    raise ValueError("原生 pass1 出现破折号分段替换；需要重新声明栅格参照策略")
    return "".join(f"none dflt @ {gid}\n" for gid in sorted(gids))


CORE_LATIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def apply_policy(source: TTFont, reference: TTFont, default: TTFont, fallback: set[int]) -> tuple[set[int], dict]:
    """Change only independently identified fallback instructions in the release.

    `source` is the native pass1, `default` its default ttfautohint output and
    `fallback` the GIDs whose default program the declared policy removes.
    Equal outlines alone are not identities, so the GID prefix is proven from
    complete point and component geometry before any ID is used.
    """
    size = len(source.getGlyphOrder())
    if len(reference.getGlyphOrder()) < size or len(default.getGlyphOrder()) != size:
        raise ValueError("hint 参照的 pass1 字形清单不一致")
    for gid in range(size):
        identity = geometry_identity(source, gid)
        if identity != geometry_identity(reference, gid) or identity != geometry_identity(default, gid):
            raise ValueError(f"hint 参照的 pass1 字形顺序或轮廓不一致：GID {gid}")
    if not fallback or not set(fallback) <= set(range(size)):
        raise ValueError("ttfautohint 未提供完整有效的 fallback 分类")
    cmap = source.getBestCmap()
    controlled = {source.getGlyphID(cmap[cp]) for cp in UI_AUTOHINT_UNSCALED_CODEPOINTS if cp in cmap}
    candidates = set(fallback) | controlled
    core = {source.getGlyphID(cmap[ord(char)]) for char in CORE_LATIN if ord(char) in cmap}
    if candidates & core:
        raise ValueError("fallback 分类覆盖了正常拉丁字母或数字")
    cleared, preserved = set(), set()
    for gid in sorted(candidates):
        # Chlorophytum replacements in the release differ from the default
        # ttfautohint program and are kept; only ttfautohint output is cleared.
        original = bytecode(reference, gid)
        if original != bytecode(default, gid):
            preserved.add(gid)
            continue
        if original:
            program = Program(); program.fromBytecode([])
            reference["glyf"][reference.getGlyphName(gid)].program = program
            cleared.add(gid)
    parents: dict[int, set[int]] = {}
    for gid, name in enumerate(reference.getGlyphOrder()):
        for component in getattr(reference["glyf"][name], "components", ()):
            parents.setdefault(reference.getGlyphID(component.glyphName), set()).add(gid)
    affected, pending = set(cleared), list(cleared)
    while pending:
        for parent in parents.get(pending.pop(), set()) - affected:
            affected.add(parent); pending.append(parent)
    codepoints = {cp for cp, name in reference.getBestCmap().items() if reference.getGlyphID(name) in affected}
    return codepoints, {"prefix_glyphs_checked": size, "fallback_glyphs": len(fallback),
                        "control_codepoints": [f"U+{cp:04X}" for cp in UI_AUTOHINT_UNSCALED_CODEPOINTS if cp in cmap],
                        "instructions_cleared": len(cleared),
                        "upstream_replaced_instructions_preserved": len(preserved),
                        "affected_codepoints": len(codepoints)}


def policy_reference(region: str, weight: str, italic: bool) -> tuple[bytes, dict]:
    """Apply the declared policy to the pinned release, never to the product.

    ttfautohint runs twice on the proven native pass1: with its default options
    and with the declared Ui options plus control file. A glyph whose default
    program disappears under the policy is a declared fallback. Its upstream
    program is removed only when it equals the default ttfautohint output, so
    Chlorophytum replacements in the release are preserved.
    """
    reference_path = c.reference_path(region, weight, italic, True)
    executable = ttfautohint_executable()
    tool_sha256 = c.sha256(executable)
    with TTFont(reference_path, recalcBBoxes=False, recalcTimestamp=False) as reference:
        pass1_path = native_pass1(region, weight, italic, reference)
        with tempfile.TemporaryDirectory(prefix="raster-reference-") as directory:
            stage = Path(directory)
            default_path, policy_path = stage / "default.ttf", stage / "policy.ttf"
            control_path = stage / "policy.control.txt"
            with TTFont(pass1_path, recalcBBoxes=False) as pass1:
                control = control_text(pass1)
            control_path.write_text(control, encoding="ascii", newline="\n")
            subprocess.run([str(executable), str(pass1_path), str(default_path)], capture_output=True, check=True)
            subprocess.run([str(executable), *UI_AUTOHINT_OPTIONS, "--control-file", str(control_path),
                            str(pass1_path), str(policy_path)], capture_output=True, check=True)
            with (TTFont(pass1_path, recalcBBoxes=False) as source,
                  TTFont(default_path, recalcBBoxes=False) as default,
                  TTFont(policy_path, recalcBBoxes=False) as policy):
                size = len(source.getGlyphOrder())
                if len(default.getGlyphOrder()) != size or len(policy.getGlyphOrder()) != size:
                    raise ValueError("ttfautohint 改变了 pass1 字形清单")
                fallback = set()
                for gid in range(size):
                    if geometry_identity(source, gid) != geometry_identity(policy, gid):
                        raise ValueError(f"声明的 hint 策略改变了 pass1 轮廓：GID {gid}")
                    if bytecode(default, gid) and not bytecode(policy, gid):
                        fallback.add(gid)
                _affected, report = apply_policy(source, reference, default, fallback)
        stream = io.BytesIO()
        reference.save(stream)
    if c.sha256(executable) != tool_sha256:
        raise RuntimeError("构造栅格参照期间 ttfautohint 发生变化")
    data = stream.getvalue()
    return data, {"reference": c.display_path(reference_path), "reference_sha256": c.sha256(reference_path),
                  "pass1": c.display_path(pass1_path), "pass1_sha256": c.sha256(pass1_path),
                  "ttfautohint_sha256": tool_sha256, "options": list(UI_AUTOHINT_OPTIONS), "control": control,
                  **report, "constructed_reference_sha256": hashlib.sha256(data).hexdigest()}


def classical_override_codepoints(region: str, weight: str) -> set[int]:
    """CL ideographs whose pinned Shanggu outline differs from the regional release."""
    if region != "CL":
        return set()
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    name = "Medium" if weight == "SemiBold" else weight
    source_path = c.SOURCE_ROOT / f"shanggu-1.028/static/ShangguSansTC-{name}.ttf"
    with (TTFont(source_path, lazy=True) as source,
          TTFont(c.reference_path(region, weight, False, False), lazy=True) as reference):
        left, right = source.getBestCmap(), reference.getBestCmap()
        glyphs, other = source.getGlyphSet(), reference.getGlyphSet()

        def outline(glyph_set, glyph_name):
            pen = DecomposingRecordingPen(glyph_set); glyph_set[glyph_name].draw(pen)
            return tuple(pen.value)
        return {cp for cp in set(left) & set(right) if in_ranges(cp, IDEOGRAPH_RANGES)
                and outline(glyphs, left[cp]) != outline(other, right[cp])}


def width_pairs(target: TTFont, reference: TTFont, *, release: bool) -> tuple[list, list]:
    """Proportional defaults equal the release pnum forms; tnum equals its default."""
    def mapping(font, tag):
        result = {}
        if "GSUB" not in font:
            return result
        table = font["GSUB"].table
        for record in table.FeatureList.FeatureRecord:
            if record.FeatureTag == tag:
                for index in record.Feature.LookupListIndex:
                    for outer in table.LookupList.Lookup[index].SubTable:
                        part = getattr(outer, "ExtSubTable", outer)
                        result.update({k: v for k, v in getattr(part, "mapping", {}).items() if isinstance(v, str)})
        return result
    target_cmap, reference_cmap = target.getBestCmap(), reference.getBestCmap()
    target_tnum = mapping(target, "tnum")
    reference_pnum, reference_tnum = mapping(reference, "pnum"), mapping(reference, "tnum")
    pairs, errors = [], []
    for cp in sorted(c.INTENTIONAL_CPS):
        default, other = target_cmap.get(cp), reference_cmap.get(cp)
        expected_default = (reference_pnum.get(other) if release else other) if other else None
        expected_tabular = (reference_tnum.get(other, other) if release else reference_tnum.get(other)) if other else None
        for variant, left, right in (("pnum", default, expected_default),
                                     ("tnum", target_tnum.get(default) if default else None, expected_tabular)):
            if left is None or right is None:
                errors.append({"cp": f"U+{cp:04X}", "variant": variant, "check": "missing_width_form"})
            else:
                pairs.append((cp, variant, target.getGlyphID(left), reference.getGlyphID(right)))
    return pairs, errors


def static_raster(path: Path, region: str, weight: str, italic: bool, hinted: bool, *, ppems=RASTER_PPEMS) -> dict:
    """Compare every shared codepoint at each ppem; nothing is skipped silently.

    Hinted regional weights: the product must render exactly like the pinned
    release with the declared fallback policy. Heavy (no release exists) and
    unhinted fonts: the hinted and unhinted products must share outlines and
    metrics, rendered without hinting. Intentional product glyphs receive a
    render check here; their geometry is verified against source oracles in
    other sections.
    """
    release = hinted and weight != "Heavy"
    policy = None
    if release:
        reference_data, policy = policy_reference(region, weight, italic)
        exceptions = set(c.INTENTIONAL_CPS) | DASH_CODEPOINTS | classical_override_codepoints(region, weight)
        flags = freetype.FT_LOAD_DEFAULT
        mode = "official-hinted-native-policy"
        counterpart = c.reference_path(region, weight, italic, True)
    else:
        counterpart = c.static_path(region, weight, italic, not hinted)
        reference_data = counterpart.read_bytes()
        exceptions = set()
        flags = freetype.FT_LOAD_NO_HINTING
        mode = "hinted-unhinted-outline-identity"
    with TTFont(path, lazy=True) as target, TTFont(io.BytesIO(reference_data), lazy=True) as reference:
        target_cmap, reference_cmap = target.getBestCmap(), reference.getBestCmap()
        shared = set(target_cmap) & set(reference_cmap)
        missing = sorted(set(reference_cmap) - set(target_cmap))
        # Width forms are additionally compared as glyph pairs, including tnum.
        pairs, pair_errors = width_pairs(target, reference, release=release)
        compared = sorted(shared - exceptions)
        smoke = sorted(shared & exceptions)
        contoured = {cp for cp in smoke
                     if getattr(target["glyf"][target_cmap[cp]], "numberOfContours", 0) != 0}
    result = Result(sample_limit=SAMPLE_LIMIT)
    for cp in missing:
        result.check(False, {"cp": f"U+{cp:04X}", "check": "missing_from_product"})
    for error in pair_errors:
        result.check(False, error)
    actual, expected = freetype.Face(str(path)), freetype.Face(io.BytesIO(reference_data))
    for ppem in ppems:
        actual.set_pixel_sizes(0, ppem); expected.set_pixel_sizes(0, ppem)
        for cp in compared:
            try:
                left = render_signature(actual, actual.get_char_index(cp), flags)
                right = render_signature(expected, expected.get_char_index(cp), flags)
            except freetype.FT_Exception as error:
                result.check(False, {"cp": f"U+{cp:04X}", "ppem": ppem, "error": str(error)})
                continue
            result.check(left == right, {"cp": f"U+{cp:04X}", "ppem": ppem,
                                         "product": compact(left), "reference": compact(right)})
        for cp, variant, left_gid, right_gid in pairs:
            try:
                left, right = render_signature(actual, left_gid, flags), render_signature(expected, right_gid, flags)
            except freetype.FT_Exception as error:
                result.check(False, {"cp": f"U+{cp:04X}", "variant": variant, "ppem": ppem, "error": str(error)})
                continue
            result.check(left == right, {"cp": f"U+{cp:04X}", "variant": variant, "ppem": ppem,
                                         "product": compact(left), "reference": compact(right)})
        for cp in smoke:
            try:
                signature = render_signature(actual, actual.get_char_index(cp), freetype.FT_LOAD_DEFAULT)
            except freetype.FT_Exception as error:
                result.check(False, {"cp": f"U+{cp:04X}", "ppem": ppem, "check": "render_error", "error": str(error)})
                continue
            blank = signature[4] <= 0 or signature[5] <= 0 or not any(signature[7])
            result.check(not (cp in contoured and blank),
                         {"cp": f"U+{cp:04X}", "ppem": ppem, "check": "blank_render", "product": compact(signature)})
    return result.report(mode=mode, ppems=list(ppems), counterpart=c.display_path(counterpart),
                         codepoints_compared=len(compared), width_pairs=len(pairs),
                         render_checked_codepoints=len(smoke), shared_codepoints=len(shared),
                         policy_reference=policy)
