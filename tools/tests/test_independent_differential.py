from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from font_audit.differential import Context, compare_pair
from font_audit.native import effects_match, routing_drops_positioning
from font_audit.provenance import archive_member_hashes, validate_reuse
from font_audit.shaping import Face, reverse_texts
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.ttGlyphPen import TTGlyphPen
import py7zr
from test_runtime_regressions import fixture_font


class IndependentDifferentialTests(unittest.TestCase):
    def test_equal_bounds_do_not_hide_a_wrong_outline(self):
        source = fixture_font(); actual = copy.deepcopy(source)
        pen = TTGlyphPen(None)
        for x0, y0, x1, y1 in ((0, 0, 400, 700), (100, 100, 200, 200)):
            pen.moveTo((x0, y0)); pen.lineTo((x1, y0)); pen.lineTo((x1, y1)); pen.lineTo((x0, y1)); pen.closePath()
        actual['glyf'].glyphs['G'] = pen.glyph()
        result = self.compare(source, actual, contexts=[Context()], codepoints=[ord('G')], outlines=True)
        self.assertGreater(result['differences'], 0)
        self.assertIn('outline', result['contexts'][0]['fields'])

    def compare(self, expected, actual, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            reference, candidate = Path(folder) / "reference.ttf", Path(folder) / "candidate.ttf"
            expected.save(reference); actual.save(candidate)
            return compare_pair(candidate, reference, **kwargs)

    def test_single_position_values_are_compared_not_only_structure(self):
        source = fixture_font()
        addOpenTypeFeaturesFromString(source, """
            languagesystem DFLT dflt;
            feature vpal { pos G <0 10 0 -100>; pos T <0 40 0 -250>; } vpal;
        """)
        actual = copy.deepcopy(source)
        lookup = actual["GPOS"].table.LookupList.Lookup[0]
        self.assertEqual(lookup.SubTable[0].Format, 2)
        values = lookup.SubTable[0].Value
        values[1] = copy.deepcopy(values[0])
        result = self.compare(source, actual, contexts=[Context(direction="ttb", feature="vpal")])
        self.assertGreater(result["differences"], 0)
        self.assertIn("advance", result["contexts"][0]["fields"])

    def test_default_script_kern_route_is_exercised(self):
        source = fixture_font()
        addOpenTypeFeaturesFromString(source, """
            languagesystem DFLT dflt;
            languagesystem latn dflt;
            feature kern { pos G T -30; } kern;
        """)
        actual = copy.deepcopy(source)
        for record in actual["GPOS"].table.ScriptList.ScriptRecord:
            if record.ScriptTag == "DFLT":
                record.Script.DefaultLangSys.FeatureIndex = []
                record.Script.DefaultLangSys.FeatureCount = 0
        result = self.compare(source, actual, contexts=[Context(script="DFLT")])
        self.assertGreater(result["differences"], 0)
        self.assertTrue(any(sample["text"] == "GT" for sample in result["contexts"][0]["samples"]))

    def test_unencoded_ligature_probes_are_derived_from_source(self):
        source = fixture_font()
        addOpenTypeFeaturesFromString(source, """
            languagesystem DFLT dflt;
            feature ccmp { sub G T o by Galt; } ccmp;
        """)
        # Galt is also encoded in some fixtures; remove that direct shortcut.
        for table in source["cmap"].tables:
            if table.isUnicode():
                table.cmap = {cp: name for cp, name in table.cmap.items() if name != "Galt"}
        self.assertEqual(reverse_texts(source)["Galt"], "GTo")
        actual = copy.deepcopy(source)
        actual["GSUB"].table.FeatureList.FeatureRecord[0].Feature.LookupListIndex = []
        actual["GSUB"].table.FeatureList.FeatureRecord[0].Feature.LookupCount = 0
        result = self.compare(source, actual, contexts=[Context()])
        self.assertTrue(any(sample["text"] == "GTo" for sample in result["contexts"][0]["samples"]))

    def test_drawn_boxes_remove_scaled_extent_rounding(self):
        # A 2048-UPM source measured at 1000 rounds its extents outward; the
        # 1000-UPM product rounds to nearest. The drawn outline box does not.
        from fontTools.ttLib.scaleUpem import scale_upem
        from font_audit.comparison import drawn_run
        fonts = []
        for upem, (x0, x1, top) in ((2048, (241, 605, 1433)), (1000, (118, 295, 700))):
            font = fixture_font()
            if upem != 1000:
                scale_upem(font, upem)
            pen = TTGlyphPen(None)
            pen.moveTo((x0, 0)); pen.lineTo((x1, 0)); pen.lineTo((x1, top)); pen.lineTo((x0, top)); pen.closePath()
            font['glyf'].glyphs['G'] = pen.glyph()
            font['hmtx'].metrics['G'] = (font['hmtx'].metrics['G'][0], x0)
            fonts.append(font)
        with tempfile.TemporaryDirectory() as folder:
            faces = []
            for index, font in enumerate(fonts):
                path = Path(folder) / f"{index}.ttf"; font.save(path); faces.append(Face(path))
            runs = [face.shape("G") for face in faces]
            integer = [run[0].bounds[2] for run in runs]
            drawn = [drawn_run(face, run)[0].bounds[2] for face, run in zip(faces, runs)]
        self.assertGreaterEqual(abs(integer[0] - integer[1]), 2)
        self.assertLess(abs(drawn[0] - drawn[1]), 1)

    def test_reuse_requires_source_and_tool_identity(self):
        current = {"source_manifest": {"upstream.ttf": "pinned"}, "tool_identity": {"harfbuzz": "fixed"}}
        validate_reuse({"provenance": current}, current)
        for field in current:
            changed = copy.deepcopy(current); changed[field] = {}
            with self.assertRaises(ValueError):
                validate_reuse({"provenance": changed}, current)
        with self.assertRaises(ValueError):
            validate_reuse({}, current)

    def test_archive_hashes_are_computed_without_writing_extracted_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); payload = b"pinned font payload"
            (root / "source.ttf").write_bytes(payload)
            with py7zr.SevenZipFile(root / "source.7z", "w") as archive:
                archive.write(root / "source.ttf", "sub/font.ttf")
            before = sorted(path.name for path in root.iterdir())
            self.assertEqual(archive_member_hashes(root / "source.7z", {"font.ttf"}),
                             {"font.ttf": hashlib.sha256(payload).hexdigest()})
            self.assertEqual(sorted(path.name for path in root.iterdir()), before)

    def test_heavy_drops_positioning_only_where_the_bold_release_does(self):
        # J vertical ！：；？ under vpal: Sarasa's ！？ forms carry no vpal, ： keeps Source Han's.
        none = (0, 0, 0, 0)
        heavy_source = [(0, 120, 0, 67), (0, 500, 0, 260), none, (0, 93, 0, 31)]
        bold_source = [(0, 118, 0, 66), (0, 500, 0, 260), none, (0, 91, 0, 30)]
        release = [none, (0, 500, 0, 260), none, none]
        product = [none, (0, 500, 0, 260), none, none]
        self.assertTrue(routing_drops_positioning(product, heavy_source, release, bold_source))
        # A whole unpositioned run, as for kana kern under latn/ROM.
        self.assertTrue(routing_drops_positioning([none] * 4, heavy_source, [none] * 4, bold_source))
        # The colon's vpal is routed by the release, so Heavy must keep it.
        self.assertFalse(routing_drops_positioning([none] * 4, heavy_source, release, bold_source))
        # A value of its own where the release drops one is still a difference.
        self.assertFalse(routing_drops_positioning([(0, 60, 0, 30), *product[1:]], heavy_source, release, bold_source))
        # Without a Bold source effect that the release removes, nothing shows Sarasa's routing.
        self.assertFalse(routing_drops_positioning(product, heavy_source, release, [none, (0, 500, 0, 260), none, none]))
        self.assertFalse(routing_drops_positioning(product[:3], heavy_source, release, bold_source))
        self.assertTrue(effects_match((1, 0, 2, -2), none, True))
        self.assertFalse(effects_match((1, 0, 0, 0), none, False))


if __name__ == "__main__":
    unittest.main()
