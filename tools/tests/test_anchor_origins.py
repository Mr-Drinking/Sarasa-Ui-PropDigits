import copy
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as build
import font_anchor_origins
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.misc.fixedTools import floatToFixedToFloat, otRound
from fontTools.ttLib import newTable
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.varLib.varStore import VarStoreInstancer
import uharfbuzz as hb
from test_release_runtime_controls import variable_fixture
from test_runtime_regressions import fixture_font


def attached_ink(font):
    stream = io.BytesIO(); font.save(stream)
    runtime = hb.Font(hb.Face(stream.getvalue()))
    buffer = hb.Buffer(); buffer.add_str('G\u0301'); buffer.script = 'Latn'; buffer.language = 'und'; buffer.direction = 'ltr'
    hb.shape(runtime, buffer, {'kern': False})
    pen = 0; positions = []
    for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
        extent = runtime.get_glyph_extents(info.codepoint)
        positions.append((pen + position.x_offset + extent.x_bearing, position.y_offset + extent.y_bearing))
        pen += position.x_advance
    return tuple((x - positions[0][0], y - positions[0][1]) for x, y in positions)


class AnchorOriginTests(unittest.TestCase):
    def test_translated_base_keeps_the_original_relative_mark_position(self):
        source = variable_fixture()
        addOpenTypeFeaturesFromString(source, '''
            languagesystem DFLT dflt;
            markClass acutecomb <anchor 100 0> @TOP;
            feature mark { pos base G <anchor 300 700> mark @TOP; } mark;
        ''')
        target = copy.deepcopy(source)
        build.shift_glyph_x(target, 'G', 10)
        target['hmtx'].metrics['G'] = (600, 10)
        self.assertNotEqual(attached_ink(source), attached_ink(target))
        result = font_anchor_origins.align(target, source, {'G': 'G', 'acutecomb': 'acutecomb'},
                                           [200, 400, 900], build.append_item_variation_store)
        self.assertGreater(result['inter_anchor_origins_aligned'], 0)
        self.assertEqual(attached_ink(source), attached_ink(target))

    def test_origin_change_is_measured_against_the_exact_source(self):
        # The product's anchors and outlines follow Inter's original curve.
        # The 1000-UPM copy rounds U+0304 off it (a unit in HarfBuzz extents
        # at 700); measured against that copy, a mark that never moved shifts.
        exact = variable_fixture()
        addOpenTypeFeaturesFromString(exact, '''
            languagesystem DFLT dflt;
            markClass acutecomb <anchor 100 0> @TOP;
            feature mark { pos base G <anchor 300 700> mark @TOP; } mark;
        ''')
        rounded = copy.deepcopy(exact)
        build.shift_glyph_x(rounded, 'acutecomb', 1)
        advance, lsb = rounded['hmtx'].metrics['acutecomb']
        rounded['hmtx'].metrics['acutecomb'] = (advance, lsb + 1)
        target, measured_against_copy = copy.deepcopy(exact), copy.deepcopy(exact)
        before = attached_ink(target)
        result = font_anchor_origins.align(target, rounded, {'G': 'G', 'acutecomb': 'acutecomb'},
                                           [200, 400, 900], build.append_item_variation_store, exact=(exact, {}))
        self.assertEqual(result['inter_anchor_origins_aligned'], 0)
        self.assertEqual(attached_ink(target), before)
        font_anchor_origins.align(measured_against_copy, rounded, {'G': 'G', 'acutecomb': 'acutecomb'},
                                  [200, 400, 900], build.append_item_variation_store)
        self.assertNotEqual(attached_ink(measured_against_copy), before)

    def test_only_a_translation_moves_an_anchor_and_it_rounds_once(self):
        # 83 at 2048 units is 40.53 at 1000, so the scaled mark rounds right.
        # Moved a unit left, its ink sits 0.53 left of the source's: rounding
        # that the native static's own anchors never follow, so an anchor at
        # 500 stays (following it gave 499, as U+030C on ê at 600). Moved three
        # units left, an anchor follows from the source's 268.07 and rounds
        # once to 266; rounding the move on its own and adding it gave 265.
        def aligned(dx, mark_x):
            exact = variable_fixture(); scale_upem(exact, 2048)
            addOpenTypeFeaturesFromString(exact, '''
                languagesystem DFLT dflt;
                markClass acutecomb <anchor %d 0> @TOP;
                feature mark { pos base G <anchor 614 1433> mark @TOP; } mark;
            ''' % mark_x)

            def move_ink(font, shift):
                build.shift_glyph_x(font, 'acutecomb', shift)
                advance, lsb = font['hmtx'].metrics['acutecomb']
                font['hmtx'].metrics['acutecomb'] = (advance, lsb + shift)

            move_ink(exact, 83 - min(x for x, _y in exact['glyf']['acutecomb'].coordinates))
            target = copy.deepcopy(exact); scale_upem(target, 1000)
            source = copy.deepcopy(target)
            move_ink(target, dx)
            runtimes = []
            for font in (target, exact):
                stream = io.BytesIO(); font.save(stream)
                runtimes.append(hb.Font(hb.Face(stream.getvalue()))); runtimes[-1].scale = (1000, 1000)
            move = (font_anchor_origins.drawn_origin(runtimes[0], target.getGlyphID('acutecomb'))[0]
                    - font_anchor_origins.drawn_origin(runtimes[1], exact.getGlyphID('acutecomb'))[0])
            font_anchor_origins.align(target, source, {'G': 'G', 'acutecomb': 'acutecomb'},
                                      [200, 400, 900], build.append_item_variation_store, exact=(exact, {}))
            mark = next(anchor for name, _owner, _field, anchor in font_anchor_origins.anchor_slots(target)
                        if name == 'acutecomb')
            return round(move, 3), mark.XCoordinate, getattr(mark, 'XDeviceTable', None)

        self.assertEqual(aligned(-1, 1024), (-0.527, 500, None))
        self.assertEqual(aligned(-3, 549), (-2.527, 266, None))

    def test_variable_anchors_round_once_at_every_sampled_weight(self):
        # Inter's 2048-unit anchors lose a fraction when scaled to 1000 UPM;
        # rounding their deltas again on top left anchors up to 1.4 units off
        # the original curve. G's anchor reaches 325.68 at 900.
        def misses(keep_fractions):
            inter = variable_fixture(); scale_upem(inter, 2048)
            addOpenTypeFeaturesFromString(inter, '''
                languagesystem DFLT dflt;
                markClass acutecomb <anchor (wght=200:203 wght=400:207 wght=900:262) 0> @TOP;
                feature mark { pos base G <anchor (wght=200:599 wght=400:615 wght=900:667) 1433> mark @TOP; } mark;
            ''')
            original = copy.deepcopy(inter)
            build.scale_inter_upem(inter)
            if not keep_fractions:
                inter._sarasa_anchor_fractions = {}
            base = fixture_font()
            base['fvar'] = copy.deepcopy(inter['fvar']); base['fvar'].axes[0].minValue = 250
            base['gvar'] = newTable('gvar'); base['gvar'].version = 1; base['gvar'].reserved = 0; base['gvar'].variations = {}
            build.apply_public_weight_axis(base)
            remap = build.import_inter_gpos_variations(base, inter)
            found = []
            for weight in build.INTER_COMPOSITE_CONTROL_WEIGHTS:
                product = VarStoreInstancer(base['GDEF'].table.VarStore, base['fvar'].axes,
                                            {'wght': floatToFixedToFloat(build.vf_mapped_normalized_weight(base, weight), 14)})
                exact = VarStoreInstancer(original['GDEF'].table.VarStore, original['fvar'].axes,
                                          {'wght': build.vf_mapped_normalized_weight(original, weight)})
                for (_name, _owner, _field, anchor), (_same, _o, _f, source) in zip(
                        font_anchor_origins.anchor_slots(inter), font_anchor_origins.anchor_slots(original)):
                    for coordinate, field in (('XCoordinate', 'XDeviceTable'), ('YCoordinate', 'YDeviceTable')):
                        device, source_device = getattr(anchor, field, None), getattr(source, field, None)
                        value = getattr(anchor, coordinate)
                        if device is not None:
                            value += product[remap[(device.StartSize << 16) | device.EndSize]]
                        expected = getattr(source, coordinate)
                        if source_device is not None:
                            expected += exact[(source_device.StartSize << 16) | source_device.EndSize]
                        if value != otRound(expected * 1000 / 2048):
                            found.append((weight, coordinate, value, expected * 1000 / 2048))
            return found

        self.assertEqual(misses(True), [])
        self.assertIn((900, 'XCoordinate', 325, 667 * 1000 / 2048), misses(False))

    def test_variable_kerning_rounds_once_at_every_sampled_weight(self):
        # Kerning loses the same fraction to scale_upem, and on the variation
        # model's own tents a control weight also sums its neighbors' rows:
        # Sarasa's ToWa at 700 kerned o-W by -49.5 where both Inter sources
        # round to -50, one unit off the Bold static.
        def misses(keep_fractions):
            inter = variable_fixture(); scale_upem(inter, 2048)
            addOpenTypeFeaturesFromString(inter, '''
                languagesystem DFLT dflt;
                feature kern { pos T o (wght=200:-87 wght=400:-101 wght=900:-131); } kern;
            ''')
            original = copy.deepcopy(inter)
            build.scale_inter_upem(inter)
            if not keep_fractions:
                inter._sarasa_value_fractions = {}
            pairs = list(zip(build.variable_value_slots(inter['GPOS'].table), build.variable_value_slots(original['GPOS'].table)))
            base = fixture_font()
            base['fvar'] = copy.deepcopy(inter['fvar']); base['fvar'].axes[0].minValue = 250
            base['gvar'] = newTable('gvar'); base['gvar'].version = 1; base['gvar'].reserved = 0; base['gvar'].variations = {}
            build.apply_public_weight_axis(base)
            remap = build.import_inter_gpos_variations(base, inter)
            found = []
            for weight in build.INTER_COMPOSITE_CONTROL_WEIGHTS:
                product = VarStoreInstancer(base['GDEF'].table.VarStore, base['fvar'].axes,
                                            {'wght': floatToFixedToFloat(build.vf_mapped_normalized_weight(base, weight), 14)})
                exact = VarStoreInstancer(original['GDEF'].table.VarStore, original['fvar'].axes,
                                          {'wght': build.vf_mapped_normalized_weight(original, weight)})
                for (record, field, device_field), (source, _field, source_device_field) in pairs:
                    device, source_device = getattr(record, device_field), getattr(source, source_device_field)
                    value = getattr(record, field, 0) or 0
                    if device is not None:
                        value += product[remap[(device.StartSize << 16) | device.EndSize]]
                    expected = (getattr(source, field, 0) or 0) + exact[(source_device.StartSize << 16) | source_device.EndSize]
                    if value != otRound(expected * 1000 / 2048):
                        found.append((weight, field, value, expected * 1000 / 2048))
            return found

        self.assertEqual(misses(True), [])
        self.assertTrue(any(value != round(value) for _weight, _field, value, _expected in misses(False)))


if __name__ == '__main__':
    unittest.main()
