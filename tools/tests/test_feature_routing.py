from __future__ import annotations

import copy
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import font_layout as layout
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
import uharfbuzz as hb
from test_runtime_regressions import fixture_font
from test_release_runtime_controls import variable_fixture


def table(mapping):
    value = b.ot.GSUB(); value.Version = 0x00010000
    layout.install(value, mapping)
    return value


class FeatureRoutingTests(unittest.TestCase):
    def test_unreferenced_feature_records_do_not_keep_dead_lookups(self):
        font = fixture_font()
        table = font['GSUB'].table
        before = len(table.LookupList.Lookup)
        dead_lookup = copy.deepcopy(table.LookupList.Lookup[0])
        table.LookupList.Lookup.append(dead_lookup); table.LookupList.LookupCount += 1
        feature = b.ot.FeatureRecord(); feature.FeatureTag = 'zzzz'; feature.Feature = b.ot.Feature()
        feature.Feature.FeatureParams = None; feature.Feature.LookupListIndex = [before]; feature.Feature.LookupCount = 1
        table.FeatureList.FeatureRecord.append(feature); table.FeatureList.FeatureCount += 1
        layout.prune_unreferenced_lookups(font)
        self.assertNotIn('zzzz', [record.FeatureTag for record in table.FeatureList.FeatureRecord])
        self.assertTrue(all(lookup is not dead_lookup for lookup in table.LookupList.Lookup))
        self.assertEqual(len(table.LookupList.Lookup), before)

    def test_ideograph_mark_collision_is_repaired_without_changing_combining_marks(self):
        font = fixture_font()
        for cmap in font['cmap'].tables:
            if cmap.isUnicode():
                cmap.cmap[0x3A89] = 'G'
        classes = font['GDEF'].table.GlyphClassDef.classDefs
        classes['G'] = 3; classes['acutecomb'] = 3
        result = b.normalize_ideograph_gdef_classes(font)
        self.assertGreaterEqual(result['ideograph_mark_classes_corrected'], 1)
        self.assertEqual(classes['G'], 1)
        self.assertEqual(classes['acutecomb'], 3)

    def test_extension_substitutions_are_not_mistaken_for_empty_lookups(self):
        font = fixture_font()
        table = font['GSUB'].table
        index = next(i for i, lookup in enumerate(table.LookupList.Lookup) if lookup.LookupType == 1)
        lookup = table.LookupList.Lookup[index]
        expected = dict(lookup.SubTable[0].mapping)
        extension = b.ot.ExtensionSubst(); extension.Format = 1; extension.ExtensionLookupType = 1
        extension.ExtSubTable = lookup.SubTable[0]
        lookup.LookupType = 7; lookup.SubTable = [extension]
        self.assertEqual(b.lookup_single_substitution_mapping(table, index), expected)

    def test_native_tnum_then_presets_keeps_default_comma_and_numr(self):
        font = fixture_font()
        for cmap in font['cmap'].tables:
            if cmap.isUnicode():
                cmap.cmap = {ord(','): 'G', ord(';'): 'T'}
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt;
            feature tnum { sub G by Galt; } tnum;
            feature pnum { sub Galt by G; } pnum;
            feature ss03 { sub G by o; } ss03;
            feature numr { sub G by T; } numr;
        ''')
        b.apply_inter_ui_recipe(font)
        self.assertEqual(font.getBestCmap()[ord(',')], 'G')
        stream = io.BytesIO(); font.save(stream)
        runtime = hb.Font(hb.Face(stream.getvalue()))
        buffer = hb.Buffer(); buffer.add_str(',')
        buffer.script = 'Latn'; buffer.language = 'und'; buffer.direction = 'ltr'
        hb.shape(runtime, buffer, {'numr': True})
        self.assertEqual(buffer.glyph_infos[0].codepoint, font.getGlyphID('T'))

    def test_positional_static_glyph_names_cannot_overwrite_variable_classes(self):
        target = variable_fixture(); reference = fixture_font()
        for font, glyph_class in ((target, 1), (reference, 3)):
            name = b.clone_glyph(font, 'G', 'glyph12345')
            font['GDEF'].table.GlyphClassDef.classDefs[name] = glyph_class
        b.rebuild_gdef_from_reference(target, reference)
        self.assertEqual(target['GDEF'].table.GlyphClassDef.classDefs['glyph12345'], 1)

    def test_language_localization_cannot_be_globally_broadcast(self):
        font = fixture_font()
        with self.assertRaisesRegex(ValueError, 'locl'):
            b.enable_features_for_all_scripts(font, {'locl'})

    def test_common_and_script_specific_kerning_both_survive(self):
        target = fixture_font(); incoming = fixture_font()
        addOpenTypeFeaturesFromString(incoming, '''
            languagesystem DFLT dflt; languagesystem latn dflt;
            feature kern { pos Galt o -15; } kern;
        ''')
        for name in incoming.getGlyphOrder():
            if name != '.notdef':
                b.clone_glyph(target, name, b.prefixed(name))
        for cmap in target['cmap'].tables:
            if cmap.isUnicode():
                cmap.cmap.update({ord('A'): b.prefixed('Galt'), ord('B'): b.prefixed('o')})
        b.import_inter_gdef_classes(target, incoming)
        b.append_layout_features(target, incoming, 'GPOS', {'kern'})
        stream = io.BytesIO(); target.save(stream)
        runtime = hb.Font(hb.Face(stream.getvalue()))
        for script in ('Zyyy', 'Latn', 'Kana'):
            for text, advance in (('GT', 570), ('AB', 585)):
                buffer = hb.Buffer(); buffer.add_str(text)
                buffer.script = script; buffer.language = 'und'; buffer.direction = 'ltr'
                hb.shape(runtime, buffer, {'kern': True})
                self.assertEqual(buffer.glyph_positions[0].x_advance, advance, (script, text))

    def test_romanian_does_not_inherit_chinese_localization(self):
        base = table({('DFLT', 'dflt'): {'ccmp': layout.Feature((0,))},
                      ('latn', 'dflt'): {'ccmp': layout.Feature((0,))},
                      ('latn', 'ZHT '): {'ccmp': layout.Feature((0,)), 'locl': layout.Feature((1,))}})
        incoming = {('DFLT', 'dflt'): {'ccmp': layout.Feature((2,))},
                    ('latn', 'dflt'): {'ccmp': layout.Feature((2,))},
                    ('latn', 'ROM '): {'ccmp': layout.Feature((2,)), 'locl': layout.Feature((3,))},
                    ('latn', 'MOL '): {'ccmp': layout.Feature((2,)), 'locl': layout.Feature((3,))}}
        layout.merge_routes(base, incoming)
        result = layout.routes(base)
        self.assertEqual(result[('DFLT', 'dflt')]['ccmp'].lookups, (0, 2))
        for language in ('ROM ', 'MOL '):
            self.assertEqual(result[('latn', language)]['locl'].lookups, (3,))
        self.assertEqual(result[('latn', 'ZHT ')]['locl'].lookups, (1,))

    def test_explicit_empty_language_is_not_replaced_with_default(self):
        mapping = {('latn', 'dflt'): {'locl': layout.Feature((4,))},
                   ('latn', 'CAT '): {}}
        result = layout.routes(table(mapping))
        self.assertEqual(layout.selected(result, 'latn', 'CAT '), {})

    def test_required_feature_survives_record_deduplication(self):
        mapping = {('DFLT', 'dflt'): {'curs': layout.Feature((9,), required=True)},
                   ('latn', 'dflt'): {'curs': layout.Feature((9,)), 'kern': layout.Feature((3,))}}
        result = layout.routes(table(mapping))
        self.assertTrue(result[('DFLT', 'dflt')]['curs'].required)
        self.assertFalse(result[('latn', 'dflt')]['curs'].required)

    def test_template_selection_uses_language_identity_not_record_number(self):
        target = fixture_font(); reference = fixture_font()
        target['GSUB'].table = table({('latn', 'dflt'): {'ccmp': layout.Feature((4, 5))},
            ('latn', 'MOL '): {'locl': layout.Feature((8,))},
            ('latn', 'ROM '): {'locl': layout.Feature((8,))},
            ('latn', 'ZHT '): {'locl': layout.Feature((2,))}})
        reference['GSUB'].table = table({('latn', 'dflt'): {'ccmp': layout.Feature((99,))},
            ('latn', 'ZHT '): {'locl': layout.Feature((1,))},
            ('latn', 'ROM '): {'locl': layout.Feature((0,))},
            ('latn', 'MOL '): {'locl': layout.Feature((0,))}})
        b.align_layout_feature_template(target, reference, 'GSUB')
        result = layout.routes(target['GSUB'].table)
        self.assertEqual(result[('latn', 'dflt')]['ccmp'].lookups, (4, 5))
        self.assertEqual(result[('latn', 'ROM ')]['locl'].lookups, (8,))
        self.assertEqual(result[('latn', 'MOL ')]['locl'].lookups, (8,))
        self.assertEqual(result[('latn', 'ZHT ')]['locl'].lookups, (2,))


if __name__ == '__main__':
    unittest.main()
