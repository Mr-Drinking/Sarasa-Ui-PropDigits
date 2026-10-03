import copy
import sys
import unittest
import io
from pathlib import Path
import uharfbuzz as hb
from fontTools.ttLib import TTFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from test_runtime_regressions import fixture_font


class LayoutTemplateConsumerTests(unittest.TestCase):
    def test_inter_arrows_remain_enabled_when_a_regional_calt_record_is_selected(self):
        if not b.INTER_UPRIGHT.is_file():
            self.skipTest('需要固定 Inter 来源')
        with TTFont(b.INTER_UPRIGHT) as font:
            table = font['GSUB'].table
            original_indices = {index for index, record in enumerate(table.FeatureList.FeatureRecord) if record.FeatureTag == 'calt'}
            partial = copy.deepcopy(table.FeatureList.FeatureRecord[min(original_indices)])
            partial.Feature.LookupListIndex = sorted(b.digit_colon_calt_lookup_indices(font, font.getBestCmap()[0x3A]))
            partial.Feature.LookupCount = len(partial.Feature.LookupListIndex)
            partial_index = len(table.FeatureList.FeatureRecord)
            table.FeatureList.FeatureRecord.append(partial); table.FeatureList.FeatureCount += 1
            for record in table.ScriptList.ScriptRecord:
                script = record.Script
                systems = ([script.DefaultLangSys] if script.DefaultLangSys else []) + [entry.LangSys for entry in script.LangSysRecord]
                for system in systems:
                    system.FeatureIndex = [partial_index if index in original_indices else index for index in system.FeatureIndex]
            def count(text, enabled):
                runtime = hb.Font(hb.Face(b.serialized_font_bytes(font)))
                buffer = hb.Buffer(); buffer.add_str(text); buffer.script = 'Latn'; buffer.language = 'en'; buffer.direction = 'ltr'
                hb.shape(runtime, buffer, {'calt': enabled})
                return len(buffer.glyph_infos)
            self.assertEqual(count('->', True), 2)
            b.add_digit_colon_feature(font)
            for text in ('<--', '->', '<->', '<==>'):
                self.assertEqual(count(text, True), 1)
                self.assertEqual(count(text, False), len(text))
            before = font.getTableData('GSUB')
            b.add_digit_colon_feature(font)
            self.assertEqual(font.getTableData('GSUB'), before)
            for record in table.FeatureList.FeatureRecord:
                if record.FeatureTag == 'calt':
                    record.Feature.LookupListIndex = [index for index in record.Feature.LookupListIndex if table.LookupList.Lookup[index].LookupType != 4]
                    record.Feature.LookupCount = len(record.Feature.LookupListIndex)
            with self.assertRaisesRegex(ValueError, 'calt 连字规则'):
                b.add_digit_colon_feature(font)

    def test_variable_layout_retains_inter_context_after_subset_renumbering(self):
        if not b.INTER_UPRIGHT.is_file():
            self.skipTest('需要固定 Inter 来源')
        with TTFont(b.INTER_UPRIGHT) as reference, TTFont(b.INTER_UPRIGHT) as target:
            b.add_digit_colon_feature(target)
            b.subset_to_current_cmap(target)
            b.align_variable_layout_to_reference(target, reference)
            def runtime(font):
                stream = io.BytesIO(); font.save(stream)
                return hb.Font(hb.Face(stream.getvalue()))
            expected = runtime(reference); actual = runtime(target)
            for weight in (200, 400, 650, 900):
                expected.set_variations({'wght': weight}); actual.set_variations({'wght': weight})
                for text in ('1:', ':2', '1:2', 'a::::::::::2', 'ķ:2', '1:ț', 'A@B', 'A-B', '<--', '->', '<->', '<==>'):
                    for features in ({'calt': True}, {'calt': True, 'tnum': True},
                                     {'calt': True, 'tnum': True, 'zero': True}):
                        results = []
                        for font, shaper in ((reference, expected), (target, actual)):
                            buffer = hb.Buffer(); buffer.add_str(text); buffer.guess_segment_properties()
                            hb.shape(shaper, buffer, features)
                            results.append([(font.getGlyphName(info.codepoint), pos.x_advance,
                                             pos.x_offset, pos.y_offset)
                                            for info, pos in zip(buffer.glyph_infos, buffer.glyph_positions)])
                        self.assertEqual(*results, (weight, text, features))

    def test_added_feature_does_not_shift_romanian_into_chinese_rules(self):
        reference = fixture_font()
        addOpenTypeFeaturesFromString(reference, '''
            languagesystem DFLT dflt;
            languagesystem latn dflt;
            languagesystem latn ROM;
            languagesystem latn ZHS;
            feature locl {
                script latn;
                language ROM exclude_dflt; sub G by Galt;
                language ZHS exclude_dflt; sub T by o;
            } locl;
        ''')
        target = copy.deepcopy(reference)
        index = b.append_gsub_lookup(target, b.make_single_substitution_lookup({'G': 'o'}))
        record = b.ot.FeatureRecord(); record.FeatureTag = 'locl'
        record.Feature = b.ot.Feature(); record.Feature.FeatureParams = None
        record.Feature.LookupListIndex = [index]; record.Feature.LookupCount = 1
        table = target['GSUB'].table
        table.FeatureList.FeatureRecord.insert(0, record)
        table.FeatureList.FeatureCount += 1
        for script in table.ScriptList.ScriptRecord:
            systems = ([script.Script.DefaultLangSys] if script.Script.DefaultLangSys else [])
            systems += [entry.LangSys for entry in script.Script.LangSysRecord]
            for system in systems:
                system.FeatureIndex = [index + 1 for index in system.FeatureIndex]
                if system.ReqFeatureIndex != 0xFFFF: system.ReqFeatureIndex += 1
            if script.ScriptTag == 'DFLT':
                script.Script.DefaultLangSys.FeatureIndex = [0]
                script.Script.DefaultLangSys.FeatureCount = 1
        b.align_layout_feature_template(target, reference, 'GSUB')
        self.assertEqual(b.langsys_single_substitution_mapping(target, 'ROM ', 'locl'), {'G': 'Galt'})
        self.assertEqual(b.langsys_single_substitution_mapping(target, 'ZHS ', 'locl'), {'T': 'o'})


if __name__ == '__main__':
    unittest.main()
