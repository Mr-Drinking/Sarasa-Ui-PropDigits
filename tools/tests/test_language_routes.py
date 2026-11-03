from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import font_layout as layout
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from test_release_runtime_controls import add_metric_tables, variable_fixture
from test_runtime_regressions import fixture_font


def table(kind, mapping):
    value = getattr(b.ot, kind)(); value.Version = 0x00010000
    layout.install(value, {key: {tag: layout.Feature(lookups) for tag, lookups in features.items()}
                           for key, features in mapping.items()})
    return value


def lookups(value, script, language, tag):
    feature = layout.routes(value).get((script, language), {}).get(tag)
    return None if feature is None else sorted(feature.lookups)


class LanguageRoutes(unittest.TestCase):
    def test_merge_joins_languages_exactly_then_shares_dflt_with_far_east(self):
        # Source Han routes every language; Inter only DFLT, cyrl/dflt (kern) and latn.
        base = table("GPOS", {("DFLT", "dflt"): {"kern": (0,)}, ("cyrl", "dflt"): {"kern": (0,)},
                              ("cyrl", "ZHS "): {"kern": (0,)}, ("latn", "dflt"): {"kern": (0,)},
                              ("hani", "dflt"): {"kern": (0,)}})
        incoming = {("DFLT", "dflt"): {"kern": layout.Feature((10,)), "mark": layout.Feature((11,)),
                                       "mkmk": layout.Feature((12,))},
                    ("cyrl", "dflt"): {"kern": layout.Feature((10,))},
                    ("latn", "dflt"): {"kern": layout.Feature((10,)), "mark": layout.Feature((11,))},
                    ("latn", "ROM "): {"kern": layout.Feature((10,)), "locl": layout.Feature((13,))}}
        layout.merge_routes(base, incoming)
        # A Far East language under Cyrillic gets Inter's DFLT marks (the VF lacked them).
        self.assertEqual(lookups(base, "cyrl", "ZHS ", "mark"), [11])
        self.assertEqual(lookups(base, "cyrl", "ZHS ", "mkmk"), [12])
        self.assertEqual(lookups(base, "cyrl", "ZHS ", "kern"), [0, 10])
        # cyrl/dflt keeps the two sources' own routes, as upstream does.
        self.assertEqual(lookups(base, "cyrl", "dflt", "kern"), [0, 10])
        self.assertIsNone(lookups(base, "cyrl", "dflt", "mark"))
        # Romanian takes nothing from Source Han's latn/dflt.
        self.assertEqual(lookups(base, "latn", "ROM ", "kern"), [10])
        self.assertEqual(lookups(base, "latn", "ROM ", "locl"), [13])
        self.assertEqual(lookups(base, "hani", "dflt", "mark"), [11])

    def test_subsetting_keeps_language_systems_equal_to_their_default(self):
        from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt; languagesystem cyrl dflt; languagesystem cyrl ZHS;
            feature kern { pos G T -30; } kern;
        ''')
        b.subset_font(font, set(font.getBestCmap()))
        systems = {(record.ScriptTag, entry.LangSysTag) for record in font["GPOS"].table.ScriptList.ScriptRecord
                   for entry in record.Script.LangSysRecord}
        # fontTools drops cyrl/ZHS as equal to cyrl/dflt; the merge needs it.
        self.assertIn(("cyrl", "ZHS "), systems)
        self.assertEqual(lookups(font["GPOS"].table, "cyrl", "ZHS ", "kern"), lookups(font["GPOS"].table, "cyrl", "dflt", "kern"))

    def test_cyrillic_and_greek_defaults_share_dflt_but_keep_vertical_routes(self):
        gsub = table("GSUB", {("DFLT", "dflt"): {"ccmp": (1, 2), "tnum": (3,), "vert": (4,)},
                              ("cyrl", "dflt"): {"ccmp": (1,), "vert": (5,)},
                              ("grek", "dflt"): {"vert": (6,)},
                              ("latn", "ROM "): {"ccmp": (2,)}})
        gpos = table("GPOS", {("DFLT", "dflt"): {"kern": (0,), "mark": (1,)},
                              ("cyrl", "dflt"): {"kern": (0,)}, ("grek", "dflt"): {"kern": (0,)}})
        font = {"GSUB": SimpleNamespace(table=gsub), "GPOS": SimpleNamespace(table=gpos)}
        report = layout.share_default_language_features(font)
        self.assertEqual(lookups(gsub, "cyrl", "dflt", "ccmp"), [1, 2])
        self.assertEqual(lookups(gsub, "cyrl", "dflt", "tnum"), [3])
        self.assertEqual(lookups(gsub, "cyrl", "dflt", "vert"), [5])
        self.assertEqual(lookups(gsub, "grek", "dflt", "vert"), [6])
        self.assertEqual(lookups(gpos, "grek", "dflt", "mark"), [1])
        self.assertEqual(lookups(gsub, "latn", "ROM ", "ccmp"), [2])
        self.assertTrue(all(report.values()))
        self.assertEqual(set(layout.share_default_language_features(font).values()), {0})

    def test_structure_comparison_ignores_numbering_but_not_routing(self):
        template = table("GPOS", {("DFLT", "dflt"): {"kern": (1, 2), "mark": (3,)},
                                  ("cyrl", "ZHS "): {"kern": (1, 2), "mark": (3,)},
                                  ("latn", "ROM "): {"kern": (2,)}})
        renumbered = table("GPOS", {("DFLT", "dflt"): {"kern": (7, 8), "mark": (9,)},
                                    ("cyrl", "ZHS "): {"kern": (7, 8), "mark": (9,)},
                                    ("latn", "ROM "): {"kern": (8,)}})
        self.assertEqual(layout.structure_differences(renumbered, template), [])
        # The v1.0.43 VF: no mark under cyrl/ZHS, Source Han kerning in latn/ROM.
        released = table("GPOS", {("DFLT", "dflt"): {"kern": (7, 8), "mark": (9,)},
                                  ("cyrl", "ZHS "): {"kern": (7, 8)},
                                  ("latn", "ROM "): {"kern": (7, 8)}})
        problems = layout.structure_differences(released, template)
        self.assertTrue(any(problem.startswith("cyrl/ZHS") and "mark" in problem for problem in problems))
        self.assertTrue(any(problem.startswith("kern:") for problem in problems))

    def test_empty_lookups_lose_feature_references(self):
        font = fixture_font()
        gsub = font["GSUB"].table
        empty = b.ot.Lookup(); empty.LookupType = 1; empty.LookupFlag = 0
        subtable = b.ot.SingleSubst(); subtable.Format = 1; subtable.mapping = {}
        empty.SubTable = [subtable]; empty.SubTableCount = 1
        gsub.LookupList.Lookup.append(empty); gsub.LookupList.LookupCount += 1
        index = len(gsub.LookupList.Lookup) - 1
        record = gsub.FeatureList.FeatureRecord[0]
        record.Feature.LookupListIndex.append(index); record.Feature.LookupCount += 1
        self.assertEqual(layout.remove_empty_lookups(font), 1)
        self.assertNotIn(index, record.Feature.LookupListIndex)
        layout.prune_unreferenced_lookups(font)
        self.assertNotIn(empty, gsub.LookupList.Lookup)
        self.assertEqual(layout.remove_empty_lookups(font), 0)

    def test_lookups_no_text_can_reach_lose_feature_references(self):
        # As Source Han's calt rule on the unencoded GID 1 in the VF: once o
        # leaves the cmap no text reaches it, while T still does.
        font = fixture_font()
        for subtable in font["cmap"].tables:
            subtable.cmap.pop(ord("o"), None)
        gsub = font["GSUB"].table
        record = gsub.FeatureList.FeatureRecord[0]
        indices = []
        for mapping in ({"o": "G"}, {"T": "G"}):
            lookup = b.ot.Lookup(); lookup.LookupType = 1; lookup.LookupFlag = 0
            subtable = b.ot.SingleSubst(); subtable.Format = 1; subtable.mapping = mapping
            lookup.SubTable = [subtable]; lookup.SubTableCount = 1
            gsub.LookupList.Lookup.append(lookup); gsub.LookupList.LookupCount += 1
            indices.append(len(gsub.LookupList.Lookup) - 1)
            record.Feature.LookupListIndex.append(indices[-1]); record.Feature.LookupCount += 1
        self.assertEqual(layout.remove_empty_lookups(font), 1)
        self.assertNotIn(indices[0], record.Feature.LookupListIndex)
        self.assertIn(indices[1], record.Feature.LookupListIndex)

    def test_inter_only_features_keep_only_lookups_the_release_applies(self):
        # As Shanggu 1.028's aalt and dlig in the CL statics: a lookup starting
        # at a glyph the release's feature never touches loses its reference.
        font, reference = fixture_font(), fixture_font()
        addOpenTypeFeaturesFromString(reference, "languagesystem DFLT dflt; feature dlig { sub G by Galt; } dlig;")
        addOpenTypeFeaturesFromString(font, """languagesystem DFLT dflt;
            lookup Latin { sub G by Galt; } Latin; lookup Other { sub T by o; } Other;
            feature dlig { lookup Latin; lookup Other; } dlig;""")
        self.assertEqual(b.prune_features_like_reference(font, reference),
                         {"release_aligned_lookup_references_removed": 1})
        self.assertEqual(lookups(font["GSUB"].table, "DFLT", "dflt", "dlig"), [0])
        self.assertEqual(b.prune_features_like_reference(font, reference),
                         {"release_aligned_lookup_references_removed": 0})

    def test_variable_family_names_keep_english_and_the_regional_language(self):
        # The CL VF starts from Source Han K, whose Korean records became copies
        # of the English names; K keeps its own Korean names.
        for region, kept in (("CL", {0x409, 0x404}), ("K", {0x409, 0x412})):
            font = fixture_font()
            for lang_id in (0x404, 0x412):
                font["name"].setName("x", 1, 3, 1, lang_id)
            font["name"].setName("x", 0, 3, 1, 0x412)
            b.drop_foreign_family_names(font, region)
            self.assertEqual({r.langID for r in font["name"].names if r.nameID == 1 and r.platformID == 3}, kept)
            self.assertTrue(any(r.nameID == 0 and r.langID == 0x412 for r in font["name"].names))

    def test_inter_oracle_reads_cyrillic_and_greek_through_dflt(self):
        # Heavy compared Γ́ in the default context with Inter's own grek/dflt,
        # which has no mark positioning; the product routes DFLT there.
        from font_audit import comparison
        self.assertEqual(comparison.inter_arguments("Γ́", {"script": None})["script"], "Zyyy")
        self.assertEqual(comparison.inter_arguments("и́", {"script": "Cyrl"})["script"], "Zyyy")
        self.assertIsNone(comparison.inter_arguments("ab", {"script": None})["script"])
        self.assertEqual(comparison.inter_arguments("и", {"script": "Latn"})["script"], "Latn")

    def test_variable_template_alignment_rejects_a_missing_feature(self):
        target, reference = fixture_font(), fixture_font()
        target["GPOS"].table = table("GPOS", {("DFLT", "dflt"): {"kern": (0,), "mark": (1,)},
                                              ("cyrl", "ZHS "): {"kern": (0,)}})
        reference["GPOS"].table = table("GPOS", {("DFLT", "dflt"): {"kern": (0,), "mark": (1,)},
                                                 ("cyrl", "ZHS "): {"kern": (0,), "mark": (1,)}})
        with self.assertRaisesRegex(ValueError, "cyrl/ZHS/mark"):
            b.align_layout_feature_template(target, reference, "GPOS", strict=True)
        report = b.align_layout_feature_template(target, reference, "GPOS")
        self.assertEqual(report["gpos_template_features_unavailable"], ["cyrl/ZHS/mark"])

    def test_gvar_phantom_advances_follow_hvar_at_every_control(self):
        font = variable_fixture()
        add_metric_tables(font, changed_advance=("G", 37))
        self.assertTrue(b.gvar_hvar_advance_mismatches(font))
        report = b.sync_gvar_advances_to_hvar(font)
        self.assertGreater(report["gvar_phantom_advances_synced_to_hvar"], 0)
        self.assertEqual(b.gvar_hvar_advance_mismatches(font), [])
        self.assertEqual(b.sync_gvar_advances_to_hvar(font)["gvar_phantom_advances_synced_to_hvar"], 0)

    def test_gvar_sync_lands_on_hvar_when_the_phantom_ends_in_half(self):
        font = variable_fixture()
        add_metric_tables(font)
        coordinates = [None] * b.gvar_coordinate_count(font, "G")
        coordinates[-4:] = [(0, 0), (1, 0), (0, 0), (0, 0)]
        font["gvar"].variations.setdefault("G", []).append(b.TupleVariation({"wght": (0.0, 1.0, 1.0)}, coordinates))
        # At 650 the phantom advance is the default plus 0.5, which rounds up.
        self.assertIn(("G", 650), {(name, weight) for name, weight, *_ in b.gvar_hvar_advance_mismatches(font)})
        b.sync_gvar_advances_to_hvar(font)
        self.assertEqual(b.gvar_hvar_advance_mismatches(font), [])

    def test_heavy_derives_xavgcharwidth_and_vertical_extremes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = fixture_font(); source["head"].unitsPerEm = 2048; source["OS/2"].xAvgCharWidth = 1402
            path = Path(folder) / "Inter-Black.ttf"; source.save(path)
            font = fixture_font()
            with patch.object(b, "inter_static_source_path", return_value=path):
                self.assertEqual(b.derive_static_weight_metadata(font, "Heavy", False), {"derived_xavgcharwidth": 684})
                font["OS/2"].xAvgCharWidth = 685
                with self.assertRaisesRegex(ValueError, "xAvgCharWidth"):
                    b.derive_static_weight_metadata(font, "Bold", False)
        font["vhea"].minTopSideBearing = -1
        self.assertEqual(b.recalculate_derived_vertical_extremes(font, "Bold"), {})
        self.assertEqual(font["vhea"].minTopSideBearing, -1)
        # Conservative stored bounds, as Sarasa's italics keep, stay untouched.
        font["glyf"]["G"].xMin = -1
        # Every fixture glyph spans 0..700 in a 1000 cell with top bearing 100.
        self.assertEqual(b.recalculate_derived_vertical_extremes(font, "Heavy"),
                         {"derived_vhea_extremes": [1000, 100, 200, 800]})
        self.assertEqual(font["glyf"]["G"].xMin, -1)


class ToolchainOverrides(unittest.TestCase):
    def test_checksum_mismatch_never_reaches_the_cache_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "asset.bin"
            with patch.object(b.urllib.request, "urlopen", side_effect=lambda *_a, **_k: io.BytesIO(b"tampered")):
                with self.assertRaises(b.ChecksumMismatch):
                    b.download_file("https://example.invalid/asset", path, "0" * 64)
            self.assertFalse(path.exists())
            self.assertFalse(list(Path(folder).glob("*.part")))
            expected = hashlib.sha256(b"payload").hexdigest()
            with patch.object(b.urllib.request, "urlopen", side_effect=[io.BytesIO(b"tampered"), io.BytesIO(b"payload")]), \
                 patch.object(b.time, "sleep"):
                b.download_file_checked("https://example.invalid/asset", path, expected)
            self.assertEqual(path.read_bytes(), b"payload")

    def test_unpinned_node_and_skipped_hint_are_rejected(self):
        for name in ("SARASA_NODE", "NODE", "NPM"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "elsewhere"}):
                with self.assertRaisesRegex(RuntimeError, name):
                    b.node_executable()
                with self.assertRaisesRegex(RuntimeError, name):
                    b.npm_executable()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"SARASA_SKIP_TTFAUTOHINT": "1"}):
            source = Path(folder) / "in.ttf"; source.write_bytes(b"font")
            with self.assertRaisesRegex(RuntimeError, "--unhinted-only"):
                b.hint_static_font(source, Path(folder) / "out.ttf", "Regular")
            self.assertFalse((Path(folder) / "out.ttf").exists())

    def test_report_text_follows_the_pinned_constants(self):
        text = b.production_method_description()
        self.assertIn(f"Node.js {b.NODE_VERSION.removeprefix('v')}", text)
        for codepoint in b.UI_AUTOHINT_UNSCALED_CODEPOINTS:
            self.assertIn(f"U+{codepoint:04X}", text)
        self.assertIn("竖排连接片段", text)


if __name__ == "__main__":
    unittest.main()
