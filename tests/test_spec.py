"""规格加载、命名推导与码位分类测试。"""
from __future__ import annotations

import unittest

from sarasa_pd.spec import (
    codepoint_category,
    is_ideograph,
    is_korean,
    load_spec,
    opentype_version,
    use_inter_codepoint,
)


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_spec()

    def test_regions_and_weights_matrix(self) -> None:
        self.assertEqual(self.spec.region_order, ("CL", "SC", "TC", "HC", "J", "K"))
        self.assertEqual([w.value for w in self.spec.weights], [200, 300, 400, 600, 700, 900])
        self.assertEqual([w.name for w in self.spec.weights], ["ExtraLight", "Light", "Regular", "SemiBold", "Bold", "Heavy"])
        self.assertEqual(self.spec.public_to_internal_wght[200], 250)
        self.assertEqual(self.spec.public_to_internal_wght[350], 350)
        self.assertEqual(self.spec.public_to_internal_wght[600], 500)
        self.assertEqual(self.spec.weight("Heavy").hcfg, "Heavy")

    def test_propdigits_and_tnum_sets(self) -> None:
        self.assertEqual(len(self.spec.propdigits_codepoints), 37)
        self.assertIn(ord("0"), self.spec.propdigits_codepoints)
        self.assertIn(ord(":"), self.spec.propdigits_codepoints)
        self.assertNotIn(ord("-"), self.spec.propdigits_codepoints)
        self.assertEqual(self.spec.tnum_extra_codepoints, frozenset({0x002D}))

    def test_opentype_version_valid_and_invalid(self) -> None:
        self.assertEqual(opentype_version("1.0.43"), "1.043")
        self.assertEqual(opentype_version("1.0.43.4"), "1.0434")
        for bad in ("1.0.43.0", "1.0.43.10", "2.0.0", "1.0.4"):
            with self.assertRaises(ValueError):
                opentype_version(bad)

    def test_naming_and_filenames(self) -> None:
        self.assertEqual(self.spec.vf_family("SC"), "Sarasa Ui VF PD SC")
        self.assertEqual(self.spec.static_family("SC"), "Sarasa Ui PD SC")
        self.assertEqual(self.spec.variable_filename("SC", False), "Sarasa-Ui-VF-PropDigits-SC[wght].ttf")
        self.assertEqual(self.spec.variable_filename("SC", True), "Sarasa-Ui-VF-PropDigits-SC-Italic[wght].ttf")
        self.assertEqual(self.spec.static_filename("SC", "Regular", True), "SarasaUiPropDigitsSC-Italic.ttf")
        self.assertEqual(self.spec.static_filename("SC", "Heavy", True), "SarasaUiPropDigitsSC-HeavyItalic.ttf")
        self.assertIn("Shanggu Fonts", self.spec.project_copyright("CL"))
        self.assertNotIn("Shanggu Fonts", self.spec.project_copyright("SC"))

    def test_codepoint_routing_rules(self) -> None:
        self.assertTrue(use_inter_codepoint(ord("A")))
        self.assertTrue(use_inter_codepoint(ord("0")))
        self.assertFalse(use_inter_codepoint(0x22EF))
        self.assertFalse(use_inter_codepoint(ord("中")))
        self.assertFalse(use_inter_codepoint(ord("한")))
        self.assertTrue(is_ideograph(ord("㓱")))
        self.assertTrue(is_korean(ord("돌")))
        self.assertEqual(codepoint_category(ord("5")), "digit")
        self.assertEqual(codepoint_category(0x2014), "dash_ellipsis")

    def test_differences_have_unique_ids_and_valid_kinds(self) -> None:
        ids = [d.id for d in self.spec.differences]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 10)
        for d in self.spec.differences:
            self.assertIn(d.kind, {"intentional", "source_vf", "upstream_inherited"})
            self.assertTrue(d.basis.strip())

    def test_unknown_region_and_weight_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.spec.region("XX")
        with self.assertRaises(ValueError):
            self.spec.weight("UltraBlack")


if __name__ == "__main__":
    unittest.main()
