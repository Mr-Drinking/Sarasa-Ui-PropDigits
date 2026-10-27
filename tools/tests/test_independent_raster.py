from array import array
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from font_audit import contract, raster
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import newTable
from fontTools.ttLib.tables.ttProgram import Program
from test_runtime_regressions import fixture_font


def looping_font(count=None, cvt=(0,)):
    """G calls an empty function `count` times, far past FreeType's per-glyph loop budget."""
    font = fixture_font()
    font["fpgm"] = newTable("fpgm"); font["fpgm"].program = Program()
    font["fpgm"].program.fromAssembly(["PUSHB[ ]", "0", "FDEF[ ]", "ENDF[ ]"])
    font["cvt "] = newTable("cvt "); font["cvt "].values = array("h", cvt)
    font["maxp"].maxFunctionDefs, font["maxp"].maxStackElements = 1, 8
    glyph = font["glyf"]["G"]; glyph.program = Program()
    glyph.program.fromAssembly(["PUSHW[ ]", str(count), "PUSHB[ ]", "0", "LOOPCALL[ ]"] if count else [])
    return font


class IndependentRasterTests(unittest.TestCase):
    def test_control_file_names_the_declared_fallbacks_and_rejects_dash_decomposition(self):
        font = fixture_font()
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap.update({0x0360: "acutecomb", 0x22EF: "o", 0x2014: "T"})
        self.assertEqual(raster.control_text(font),
                         "".join(f"none dflt @ {font.getGlyphID(name)}\n" for name in sorted(("acutecomb", "o"), key=font.getGlyphID)))
        addOpenTypeFeaturesFromString(font, "languagesystem DFLT dflt; feature ccmp { sub T by T T; } ccmp;")
        with self.assertRaisesRegex(ValueError, "破折号分段"):
            raster.control_text(font)

    def test_hinted_and_unhinted_outlines_must_render_identically(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            hinted, unhinted = root / "hinted.ttf", root / "unhinted.ttf"
            fixture_font().save(hinted)
            fixture_font().save(unhinted)
            counterpart = lambda region, weight, italic, flag: hinted if flag else unhinted  # noqa: E731
            # The fixture has no width alternates, so no width pairs apply.
            with (patch.object(contract, "ROOT", root), patch.object(contract, "static_path", counterpart),
                  patch.object(contract, "INTENTIONAL_CPS", frozenset())):
                baseline = raster.static_raster(hinted, "SC", "Heavy", False, True, ppems=(12, 16))
                self.assertEqual(baseline["failures"], 0, baseline["samples"])
                changed = fixture_font()
                pen = TTGlyphPen(None)
                pen.moveTo((0, 0)); pen.lineTo((400, 0)); pen.lineTo((400, 500)); pen.lineTo((0, 500)); pen.closePath()
                changed["glyf"].glyphs["G"] = pen.glyph()
                changed.save(unhinted)
                result = raster.static_raster(hinted, "SC", "Heavy", False, True, ppems=(12, 16))
            self.assertEqual(result["failures"] - baseline["failures"], 2)
            self.assertIn("U+0047", {sample.get("cp") for sample in result["samples"]})
            self.assertEqual(result["mode"], "hinted-unhinted-outline-identity")

    def test_only_byte_identical_upstream_hint_failures_are_inherited(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = {}
            for name, font in (("product", looping_font(2000)), ("same", looping_font(2000)),
                               ("program", looping_font(2001)), ("cvt", looping_font(2000, cvt=(1,))),
                               ("clean", looping_font())):
                paths[name] = Path(raw) / f"{name}.ttf"; font.save(paths[name])
            def run(reference):
                return raster.execute_hint_programs(paths["product"], reference=reference,
                                                    full_ppems=(12,), regression_ppems=())
            modes = len(raster.MODES)
            self.assertEqual(run(None)["failures"], modes)
            inherited = run(paths["same"])
            self.assertEqual((inherited["failures"], inherited["inherited_upstream"]), (0, modes))
            self.assertIn("execution context too long", inherited["inherited_upstream_samples"][0]["error"])
            # Another program, another CVT, or an upstream glyph that executes cleanly: still failures.
            for name in ("program", "cvt", "clean"):
                result = run(paths[name])
                self.assertEqual((result["failures"], result["inherited_upstream"]), (modes, 0), name)


if __name__ == "__main__":
    unittest.main()
