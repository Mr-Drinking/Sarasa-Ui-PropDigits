from __future__ import annotations

import copy
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
import font_layout
import uharfbuzz as hb
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from test_runtime_regressions import fixture_font


def positioning(values, *, format=2):
    table = b.ot.SinglePos()
    table.Format = format
    table.Coverage = b.ot.Coverage()
    table.Coverage.glyphs = [f"g{i}" for i in range(len(values))]
    table.ValueFormat = 0x000A
    records = []
    for y, advance in values:
        record = b.ot.ValueRecord()
        record.YPlacement = y
        record.YAdvance = advance
        records.append(record)
    table.Value = records if format == 2 else records[0]
    if format == 2:
        table.ValueCount = len(records)
    return table


class SinglePositionValueTests(unittest.TestCase):
    def test_reordered_pair_records_remain_searchable_in_harfbuzz(self):
        font = fixture_font()
        addOpenTypeFeaturesFromString(font, '''
            languagesystem DFLT dflt;
            feature kern { pos G T -14; pos G o -25; pos G Galt -35; } kern;
        ''')
        pair_set = font['GPOS'].table.LookupList.Lookup[0].SubTable[0].PairSet[0]
        pair_set.PairValueRecord.reverse()

        def advance():
            stream = io.BytesIO(); font.save(stream)
            runtime = hb.Font(hb.Face(stream.getvalue()))
            buffer = hb.Buffer(); buffer.add_str('Go'); buffer.guess_segment_properties()
            buffer.language = 'und'; hb.shape(runtime, buffer)
            return buffer.glyph_positions[0].x_advance

        width = font['hmtx'].metrics['G'][0]
        self.assertEqual(advance(), width)
        self.assertEqual(font_layout.sort_pair_sets(font), 1)
        self.assertEqual(advance(), width - 25)
        self.assertEqual(font_layout.sort_pair_sets(font), 0)

    def test_class_pairs_survive_a_format_one_template_with_fewer_fields(self):
        source = b.ot.PairPos()
        source.Format = 2
        source.Coverage = b.ot.Coverage(); source.Coverage.glyphs = ['seven']
        source.ClassDef1 = b.empty_class_def(); source.ClassDef1.classDefs = {'seven': 1}
        source.ClassDef2 = b.empty_class_def(); source.ClassDef2.classDefs = {'eight': 1}
        source.Class1Count = source.Class2Count = 2
        source.ValueFormat1 = 6; source.ValueFormat2 = 0
        source.Class1Record = []
        for index in range(2):
            row = b.ot.Class1Record()
            row.Class2Record = []
            for column in range(2):
                value = b.ot.ValueRecord(); value.XAdvance = -14 if index == column == 1 else 0
                value.YPlacement = 7 if index == column == 1 else 0
                row.Class2Record.append(b.build_class2_record(6, 0, value))
            source.Class1Record.append(row)
        reference = b.ot.PairPos(); reference.Format = 1
        reference.ValueFormat1 = 4; reference.ValueFormat2 = 0
        target = b.align_pair_pos_format(source, reference)
        self.assertEqual(target.Format, 2)
        value = target.Class1Record[1].Class2Record[1].Value1
        self.assertEqual((value.XAdvance, value.YPlacement), (-14, 7))
        self.assertEqual(target.ClassDef2.classDefs['eight'], 1)

    def test_format_two_keeps_each_glyph_value(self):
        source = positioning([(250, -1000), (550, -200), (0, -58)])
        target = b.align_single_pos_format(copy.deepcopy(source), source)
        self.assertEqual([(r.YPlacement, r.YAdvance) for r in target.Value],
                         [(250, -1000), (550, -200), (0, -58)])

    def test_heterogeneous_values_cannot_be_collapsed_to_format_one(self):
        source = positioning([(0, -58), (0, -246), (0, -222)])
        reference = positioning([(0, -87)], format=1)
        target = b.align_single_pos_format(source, reference)
        self.assertEqual(target.Format, 2)
        self.assertEqual([r.YAdvance for r in target.Value], [-58, -246, -222])

    def test_shared_value_can_be_expanded_without_changing_it(self):
        source = positioning([(550, -200)] * 3, format=1)
        target = b.align_single_pos_format(source, positioning([(0, 0)] * 3))
        self.assertEqual([(r.YPlacement, r.YAdvance) for r in target.Value],
                         [(550, -200)] * 3)

    def test_template_cannot_discard_a_value_field(self):
        source = positioning([(550, -200)])
        reference = positioning([(0, 0)])
        reference.ValueFormat = 2
        target = b.align_single_pos_format(source, reference)
        self.assertEqual(target.Value[0].YAdvance, -200)

    def test_missing_per_glyph_values_are_rejected(self):
        source = positioning([(0, -200), (0, -100)])
        source.Value.pop()
        with self.assertRaises(ValueError):
            b.align_single_pos_format(source, source)


if __name__ == "__main__":
    unittest.main()
