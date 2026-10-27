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
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from test_runtime_regressions import fixture_font


def dash_font(locl_languages):
    # Source Han's dash roles; KOR localizes the em dash to its own alternate.
    order = ['.notdef', 'emdash', 'uni2015', 'uni2E3A', 'uni2E3B', 'uniFE31', 'two', 'three', 'vtwo', 'vthree', 'emdash.kor']
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({0x2014: 'emdash', 0x2015: 'uni2015', 0x2E3A: 'uni2E3A', 0x2E3B: 'uni2E3B', 0xFE31: 'uniFE31'})
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 300)); pen.lineTo((1000, 300)); pen.lineTo((1000, 350)); pen.lineTo((0, 350)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (1000, 0) for name in order})
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    builder.setupNameTable({'familyName': 'Probe', 'styleName': 'Regular'})
    builder.setupOS2(); builder.setupPost(); builder.setupMaxp()
    locl = ''.join(f'language {language} exclude_dflt; sub emdash by {"emdash.kor" if language == "KOR" else "uni2015"}; '
                   'sub uni2E3A by two; sub uni2E3B by three; ' for language in locl_languages)
    addOpenTypeFeaturesFromString(builder.font, 'languagesystem DFLT dflt; languagesystem hani dflt; '
                                  + ''.join(f'languagesystem hani {language}; ' for language in ('JAN', 'KOR', 'ZHH', 'ZHS', 'ZHT'))
                                  + f'''
        feature ccmp {{ sub uni2015 uni2015 uni2015 by three; sub uni2015 uni2015 by two; }} ccmp;
        feature locl {{ script hani; {locl} }} locl;
        feature vert {{ sub uni2015 by uniFE31; sub two by vtwo; sub three by vthree; }} vert;
    ''')
    return builder.font


def vertical_punctuation_font(vert):
    # Sarasa K substitutes ！ and ： for every language; Source Han's Korean
    # VF routes them per language and rotates the Japanese colon instead.
    order = ['.notdef', 'uni3001', 'uniFE11', 'uniFF01', 'uniFE15', 'uniFF1A', 'uniFE13', 'colon.jan', 'colon.bad']
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({0x3001: 'uni3001', 0xFE11: 'uniFE11', 0xFF01: 'uniFF01',
                               0xFE15: 'uniFE15', 0xFF1A: 'uniFF1A', 0xFE13: 'uniFE13'})
    glyphs = {}
    for index, name in enumerate(order):
        # Each form draws apart from the others, as an upright and a rotated colon do.
        pen = TTGlyphPen(None)
        pen.moveTo((10 + 100 * index, 100)); pen.lineTo((60 + 100 * index, 100))
        pen.lineTo((60 + 100 * index, 700)); pen.lineTo((10 + 100 * index, 700)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (1000, 10 + 100 * index) for index, name in enumerate(order)})
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    builder.setupNameTable({'familyName': 'Probe', 'styleName': 'Regular'})
    builder.setupOS2(); builder.setupPost(); builder.setupMaxp()
    addOpenTypeFeaturesFromString(builder.font, 'languagesystem DFLT dflt; languagesystem hani dflt; '
                                  'languagesystem hani JAN; languagesystem hani ZHT; ' + vert)
    return builder.font


def vertical_glyphs(font, text, language):
    stream = io.BytesIO(); font.save(stream)
    runtime = hb.Font(hb.Face(stream.getvalue()))
    buffer = hb.Buffer(); buffer.add_str(text); buffer.script = 'Hani'; buffer.language = language; buffer.direction = 'ttb'
    hb.shape(runtime, buffer, {})
    return [font.getGlyphName(info.codepoint) for info in buffer.glyph_infos]


SOURCE_HAN_VERTICAL = '''
    lookup common { sub uni3001 by uniFE11; } common;
    lookup punctuation { sub uniFF01 by uniFE15; sub uniFF1A by uniFE13; } punctuation;
    lookup jan { sub uniFF1A by colon.jan; } jan;
    feature vert {
        script DFLT; language dflt; lookup common; lookup punctuation;
        script hani; language dflt; lookup common; lookup punctuation;
        language JAN exclude_dflt; lookup common; lookup jan;
        language ZHT exclude_dflt; lookup common;
    } vert;
'''


class LayoutTemplateConsumerTests(unittest.TestCase):
    def test_vertical_routes_follow_the_native_static_for_every_language(self):
        reference = vertical_punctuation_font(
            'feature vert { sub uni3001 by uniFE11; sub uniFF01 by uniFE15; sub uniFF1A by uniFE13; } vert;')
        target = vertical_punctuation_font(SOURCE_HAN_VERTICAL)
        self.assertEqual(vertical_glyphs(target, '！：', 'ja'), ['uniFF01', 'colon.jan'])
        report = b.unify_reference_vertical_routes(target, reference)
        self.assertEqual(report['vertical_routes_unified'], 2)
        for language in ('und', 'ja', 'zh-Hant'):
            with self.subTest(language=language):
                self.assertEqual(vertical_glyphs(target, '！：、', language), ['uniFE15', 'uniFE13', 'uniFE11'])

    def test_vertical_routes_may_come_from_another_script(self):
        # Source Han's TC VF substitutes ！ only for KOR/ZHS, never in DFLT.
        reference = vertical_punctuation_font(
            'feature vert { sub uni3001 by uniFE11; sub uniFF01 by uniFE15; sub uniFF1A by uniFE13; } vert;')
        target = vertical_punctuation_font(SOURCE_HAN_VERTICAL.replace(
            'script DFLT; language dflt; lookup common; lookup punctuation;', 'script DFLT; language dflt; lookup common;'))
        stream = io.BytesIO(); target.save(stream)
        runtime = hb.Font(hb.Face(stream.getvalue()))
        buffer = hb.Buffer(); buffer.add_str('！'); buffer.direction = 'ttb'; buffer.script = 'Zyyy'
        hb.shape(runtime, buffer, {})
        self.assertEqual([target.getGlyphName(info.codepoint) for info in buffer.glyph_infos], ['uniFF01'])
        b.unify_reference_vertical_routes(target, reference)
        stream = io.BytesIO(); target.save(stream)
        runtime = hb.Font(hb.Face(stream.getvalue()))
        buffer = hb.Buffer(); buffer.add_str('！'); buffer.direction = 'ttb'; buffer.script = 'Zyyy'
        hb.shape(runtime, buffer, {})
        self.assertEqual([target.getGlyphName(info.codepoint) for info in buffer.glyph_infos], ['uniFE15'])

    def test_vertical_routes_combine_two_source_routes(self):
        # Sarasa J rotates the colon (Source Han J's JAN lookup) and takes ！
        # from its KOR lookup, for every language; no single route does both.
        reference = vertical_punctuation_font(
            'feature vert { sub uni3001 by uniFE11; sub uniFF01 by uniFE15; sub uniFF1A by colon.jan; } vert;')
        target = vertical_punctuation_font('''
            lookup common { sub uni3001 by uniFE11; } common;
            lookup jan { sub uniFF1A by colon.jan; } jan;
            lookup kor { sub uniFF01 by uniFE15; sub uniFF1A by uniFE13; } kor;
            feature vert {
                script DFLT; language dflt; lookup common; lookup jan;
                script hani; language dflt; lookup common; lookup jan;
                language JAN exclude_dflt; lookup common; lookup jan;
                language ZHT exclude_dflt; lookup common; lookup kor;
            } vert;
        ''')
        b.unify_reference_vertical_routes(target, reference)
        for language in ('und', 'ja', 'zh-Hant'):
            with self.subTest(language=language):
                self.assertEqual(vertical_glyphs(target, '！：、', language), ['uniFE15', 'colon.jan', 'uniFE11'])

    def test_vertical_routes_reject_forms_no_route_combination_reaches(self):
        # Each route reaches one native form and breaks the other, and the
        # earlier lookup wins either combination.
        reference = vertical_punctuation_font(
            'feature vert { sub uni3001 by uniFE11; sub uniFF01 by uniFE15; sub uniFF1A by uniFE13; } vert;')
        target = vertical_punctuation_font('''
            lookup first { sub uniFF01 by uniFE15; sub uniFF1A by colon.bad; } first;
            lookup second { sub uniFF01 by uniFE11; sub uniFF1A by uniFE13; } second;
            lookup common { sub uni3001 by uniFE11; } common;
            feature vert {
                script DFLT; language dflt; lookup common; lookup first;
                script hani; language dflt; lookup common; lookup first;
                language JAN exclude_dflt; lookup common; lookup second;
                language ZHT exclude_dflt; lookup common; lookup first;
            } vert;
        ''')
        with self.assertRaisesRegex(ValueError, 'U\\+FF1A'):
            b.unify_reference_vertical_routes(target, reference)

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

    def test_variable_template_carries_source_han_dash_forms_it_does_not_route(self):
        # Sarasa Ui K, J and CL route no locl for JAN/KOR. The final dash pass
        # still needs Source Han's KOR single-dash alternate from the VF.
        reference = dash_font(['ZHH', 'ZHS', 'ZHT'])
        routed_only = dash_font(['JAN', 'KOR', 'ZHH', 'ZHS', 'ZHT'])
        b.align_layout_feature_template(routed_only, reference, 'GSUB')
        with self.assertRaisesRegex(ValueError, 'Korean single-dash alternate'):
            b.normalized_source_han_dash_locl_mappings(routed_only)
        target = dash_font(['JAN', 'KOR', 'ZHH', 'ZHS', 'ZHT'])
        report = b.align_variable_layout_to_reference(target, reference)
        self.assertEqual(report['source_dash_locl_languages_carried'], 2)
        self.assertEqual(b.normalized_source_han_dash_locl_mappings(target)['KOR ']['emdash'], 'emdash.kor')
        carried = b.source_dash_locl_by_language(target, b.upstream_dash_roles(target))
        self.assertEqual(carried['JAN '], {'emdash': 'uni2015', 'uni2E3A': 'two', 'uni2E3B': 'three'})
        self.assertEqual(carried['KOR ']['emdash'], 'emdash.kor')

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
