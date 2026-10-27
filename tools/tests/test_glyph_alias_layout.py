import copy
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
import uharfbuzz as hb
from test_runtime_regressions import fixture_font


def shape(font, text, features=None, direction='ltr'):
    data = b.serialized_font_bytes(font)
    runtime = hb.Font(hb.Face(data))
    buffer = hb.Buffer(); buffer.add_str(text)
    buffer.script = 'Latn'; buffer.language = 'und'; buffer.direction = direction
    hb.shape(runtime, buffer, features or {})
    return [(item.codepoint, pos.x_advance, pos.y_advance, pos.x_offset, pos.y_offset)
            for item, pos in zip(buffer.glyph_infos, buffer.glyph_positions)]


class GlyphAliasLayoutTests(unittest.TestCase):
    def test_hangul_normalization_includes_layout_outputs_and_keeps_zero_advances(self):
        font = fixture_font()
        for cmap in font['cmap'].tables:
            if cmap.isUnicode(): cmap.cmap = {0x1100: 'G', 0x1160: 'T'}
        for name in ('G', 'Galt', 'o'):
            font['hmtx'].metrics[name] = (920, 0)
        font['hmtx'].metrics['T'] = (0, 0)
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt;
            feature ljmo { sub G by Galt; } ljmo;
            feature ccmp { sub Galt T by o; } ccmp;
        ''')
        self.assertEqual(b.normalize_hangul_widths(font), 4)
        for name in ('G', 'Galt', 'o'):
            self.assertEqual(font['hmtx'].metrics[name][0], 1000)
        self.assertEqual(font['hmtx'].metrics['T'][0], 0)
        self.assertEqual(font['glyf']['T'].xMin, -40)

    def test_alias_inherits_single_position_pair_position_and_substitution(self):
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt;
            feature kern { pos G T -30; pos T G -20; } kern;
            feature palt { pos G <10 0 -100 0>; pos T <20 0 -200 0>; } palt;
            feature vert { sub G by Galt; } vert;
            feature dlig { sub G T by o; sub T G by Galt; } dlig;
        ''')
        for cmap in font['cmap'].tables:
            if cmap.isUnicode(): cmap.cmap[ord('A')] = 'G'
        alias = b.clone_cmap_glyph_for_codepoint(font, ord('A'))
        self.assertNotEqual(alias, 'G')
        for text, reference, features, direction in (
            ('AT', 'GT', {'kern': True}, 'ltr'), ('TA', 'TG', {'kern': True}, 'ltr'),
            ('A', 'G', {'palt': True}, 'ltr'), ('A', 'G', {'vert': True}, 'ttb'),
            ('AT', 'GT', {'dlig': True}, 'ltr'), ('TA', 'TG', {'dlig': True}, 'ltr')):
            actual, expected = shape(font, text, features, direction), shape(font, reference, features, direction)
            self.assertEqual([row[1:] for row in actual], [row[1:] for row in expected])
            if features.get('vert') or features.get('dlig'):
                self.assertEqual(actual, expected)

    def test_alias_inherits_mark_attachment_and_class(self):
        font = fixture_font()
        for cmap in font['cmap'].tables:
            if cmap.isUnicode(): cmap.cmap[ord('A')] = 'G'
        alias = b.clone_cmap_glyph_for_codepoint(font, ord('A'))
        self.assertEqual([row[1:] for row in shape(font, 'A\u0301')],
                         [row[1:] for row in shape(font, 'G\u0301')])
        self.assertEqual(font['GDEF'].table.GlyphClassDef.classDefs[alias], 1)

    def test_classical_override_separates_unified_and_compatibility_glyphs(self):
        target = fixture_font(); source = fixture_font()
        unified, compatibility = 0x66F4, 0xF901
        for font, mapping in ((target, {unified: 'G', compatibility: 'G'}),
                              (source, {unified: 'G', compatibility: 'Galt'})):
            for cmap in font['cmap'].tables:
                if cmap.isUnicode(): cmap.cmap = dict(mapping)
        source['glyf']['Galt'].coordinates[1] = (350, 0)
        source['glyf']['Galt'].recalcBounds(source['glyf'])
        self.assertEqual(b.apply_classical_cmap_overrides(target, source, {unified, compatibility}), (2, 2))
        cmap = target.getBestCmap()
        self.assertNotEqual(cmap[unified], cmap[compatibility])
        for cp, name in ((unified, 'G'), (compatibility, 'Galt')):
            self.assertEqual(list(target['glyf'][cmap[cp]].coordinates), list(source['glyf'][name].coordinates))

    def test_reference_alias_alignment_keeps_classical_overrides_apart(self):
        # Sarasa Ui CL maps 礻 to Source Han's ⺭; Shanggu, and the CL static,
        # draw 礻 as its own glyph. Merging by the reference undid the override.
        radical, unified = 0x2EAD, 0x793B
        font, reference = fixture_font(), fixture_font()
        for target, mapping in ((font, {radical: 'G', unified: 'Galt'}), (reference, {radical: 'G', unified: 'G'})):
            for cmap in target['cmap'].tables:
                if cmap.isUnicode(): cmap.cmap = dict(mapping)
        merged = copy.deepcopy(font)
        self.assertTrue(b.align_reference_cmap_alias_mappings(merged, reference, set())["reference_cmap_alias_mappings_aligned"])
        self.assertEqual(merged.getBestCmap()[unified], 'G')
        report = b.align_reference_cmap_alias_mappings(font, reference, set(), {radical: 'uni2EAD', unified: 'uni2EAC'})
        self.assertEqual((report["reference_cmap_alias_mappings_aligned"], report["reference_cmap_alias_groups_kept_apart"]), (0, 1))
        self.assertEqual(font.getBestCmap()[unified], 'Galt')

    def test_pwid_baking_keeps_the_alternate_layout_identity(self):
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt;
            feature pwid { sub G by Galt; } pwid;
            feature vert { sub Galt by T; } vert;
        ''')
        b.bake_single_substitution_feature(font, 'pwid')
        self.assertEqual(font.getBestCmap()[ord('G')], 'Galt')
        self.assertEqual(shape(font, 'G', {'vert': True}, 'ttb')[0][0], font.getGlyphID('T'))


if __name__ == '__main__':
    unittest.main()
