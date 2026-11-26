"""阶段 3/5 单元测试：静态实例化元数据（instance.py）与 21 ZIP 打包/核验（package.py）。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from fontTools.fontBuilder import FontBuilder
from fontTools.misc.fixedTools import otRound
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from sarasa_pd.checks.binary import check_vf_static_parity
from sarasa_pd.package import (
    package_release,
    rewrite_readme_for_zip,
    verify_check_gate,
    verify_release_packages,
    write_check_gate,
)
from sarasa_pd.spec import load_spec
from sarasa_pd.static.instance import build_unhinted_statics, instantiate_static_from_vf
from sarasa_pd.vf.base import shear_font
from sarasa_pd.vf.metrics import recalculate_head_bounds_and_lsb
from tests.test_vf_layout_and_fixes import _make_sample_vf


class StaticAndPackageTests(unittest.TestCase):
    def test_instantiate_static_from_vf_sets_metadata_and_drops_var_tables(self) -> None:
        spec = load_spec()
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            vf_path = tmp / "sample-vf.ttf"
            out_path = tmp / "static-heavy.ttf"
            vf = _make_sample_vf(with_sip=True)
            vf.save(vf_path)
            vf.close()

            rep = instantiate_static_from_vf(
                vf_path, out_path, region="CL", weight_name="Heavy", italic=True, spec=spec
            )
            self.assertEqual(rep["weight_value"], 900)
            self.assertIn("ShangguSansTC-VF", rep["version_string"])
            with TTFont(out_path) as st:
                for banned in ("fvar", "avar", "gvar", "HVAR", "VVAR", "MVAR", "VORG"):
                    self.assertNotIn(banned, st)
                self.assertEqual(float(st["post"].formatType), 3.0)
                self.assertEqual(st["OS/2"].usWeightClass, 900)
                self.assertEqual(st["OS/2"].xAvgCharWidth, 686)
                self.assertEqual(st["OS/2"].panose.bWeight, 10)
                self.assertEqual(st["head"].lowestRecPPEM, 3)
                self.assertEqual(st["head"].flags, 3)
                self.assertAlmostEqual(st["post"].italicAngle, spec.italic_angle, places=4)
                self.assertTrue(bool(st["OS/2"].ulUnicodeRange2 & (1 << 25)))
                self.assertFalse(any(r.platformID == 1 for r in st["name"].names))
                self.assertFalse(any(r.nameID == 25 for r in st["name"].names))

    def test_deoverlap_and_upright_shear_italic_pairing(self) -> None:
        import math

        spec = load_spec()

        def _cross_glyph():
            # 十字相交的两个重叠矩形（2 条轮廓）-> 去重叠后必须合并为 1 条轮廓
            pen = TTGlyphPen(None)
            for x0, y0, x1, y1 in ((100, 350, 900, 450), (450, 100, 550, 800)):
                pen.moveTo((x0, y0))
                pen.lineTo((x1, y0))
                pen.lineTo((x1, y1))
                pen.lineTo((x0, y1))
                pen.closePath()
            return pen.glyph()

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            vf_dir = tmp / "variable"
            vf_dir.mkdir(parents=True)

            up_vf = _make_sample_vf(with_sip=True)
            han_gn = up_vf.getBestCmap()[0x5DC5]
            up_vf["glyf"][han_gn] = _cross_glyph()
            up_vf["gvar"].variations[han_gn] = []
            up_vf["hmtx"].metrics[han_gn] = (1000, 100)
            recalculate_head_bounds_and_lsb(up_vf)
            up_vf_path = vf_dir / spec.variable_filename("SC", False)
            up_vf.save(up_vf_path)
            up_vf.close()

            it_vf = TTFont(up_vf_path)
            shear_font(it_vf, abs(spec.italic_angle))
            it_vf_path = vf_dir / spec.variable_filename("SC", True)
            it_vf.save(it_vf_path)
            it_vf.close()

            rep = build_unhinted_statics(
                tmp,
                tmp,
                regions=["SC"],
                styles=["upright", "italic"],
                weights=["Regular"],
                jobs=1,
                spec=spec,
            )
            self.assertEqual(rep["count"], 2)
            out_dir = spec.static_dir(tmp, "SC", hinted=False)
            up_st = out_dir / spec.static_filename("SC", "Regular", False)
            it_st = out_dir / spec.static_filename("SC", "Regular", True)

            with TTFont(up_st) as fu, TTFont(it_st) as fi:
                gu = fu["glyf"][fu.getBestCmap()[0x5DC5]]
                gi = fi["glyf"][fi.getBestCmap()[0x5DC5]]
                gu.expand(fu["glyf"])
                gi.expand(fi["glyf"])
                # 重叠十字已合并为单一轮廓，且正斜体拓扑、flags 与 y 坐标逐点一致
                self.assertEqual(gu.numberOfContours, 1)
                self.assertEqual(gi.numberOfContours, 1)
                self.assertEqual(list(gu.endPtsOfContours), list(gi.endPtsOfContours))
                self.assertEqual([f & 1 for f in gu.flags], [f & 1 for f in gi.flags])
                shear = math.tan(math.radians(abs(spec.italic_angle)))
                center = fu["head"].unitsPerEm / 3.0
                for (xu, yu), (xi, yi) in zip(gu.coordinates, gi.coordinates):
                    self.assertEqual(yi, yu)
                    self.assertEqual(xi, otRound(xu + (yu - center) * shear))

            # 无 hint 光栅核对必须在去重叠后的正斜体上均通过
            p_up = check_vf_static_parity(up_vf_path, up_st, 400, workers=1)
            p_it = check_vf_static_parity(it_vf_path, it_st, 400, workers=1)
            self.assertTrue(p_up.ok, p_up.errors)
            self.assertTrue(p_it.ok, p_it.errors)

    def test_package_21_zips_deterministic_and_verify_detects_tampering(self) -> None:
        spec = load_spec()
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            proj = tmp / "repo"
            fonts = tmp / "fonts"
            dist1 = tmp / "dist1"
            dist2 = tmp / "dist2"
            proj.mkdir()
            (proj / "LICENSE").write_text("OFL 1.1\n", encoding="utf-8")
            (proj / "NOTICE.md").write_text("NOTICE\n", encoding="utf-8")
            (proj / "README.md").write_text(
                "# README\n\n见 [LICENSE](LICENSE)、[规格](docs/规格.md) 与 [requirements.lock](requirements.lock)。\n",
                encoding="utf-8",
            )

            vf_dir = fonts / "variable"
            vf_dir.mkdir(parents=True)
            for region in spec.region_order:
                for italic in (False, True):
                    (vf_dir / spec.variable_filename(region, italic)).write_bytes(f"VF-{region}-{italic}".encode())
                for hinted in (True, False):
                    sdir = spec.static_dir(fonts, region, hinted=hinted)
                    sdir.mkdir(parents=True)
                    for w in spec.weights:
                        for italic in (False, True):
                            (sdir / spec.static_filename(region, w.name, italic)).write_bytes(
                                f"ST-{region}-{w.name}-{italic}-{hinted}".encode()
                            )

            # 无检查标记或仅 fast 档位时默认必须拒绝打包
            with self.assertRaisesRegex(ValueError, "缺少全量检查通过标记"):
                package_release(fonts, dist1, project_root=proj, spec=spec)
            write_check_gate(fonts, spec, ft_mode="fast", skipped_ots=False)
            with self.assertRaisesRegex(ValueError, "ft_mode='full'"):
                package_release(fonts, dist1, project_root=proj, spec=spec)

            # 写入 full + OTS 检查通过标记后正常打包
            write_check_gate(fonts, spec, ft_mode="full", skipped_ots=False)
            m1 = package_release(fonts, dist1, project_root=proj, spec=spec)
            m2 = package_release(fonts, dist2, project_root=proj, spec=spec)
            self.assertEqual(m1["package_count"], 21)
            self.assertEqual((dist1 / "SHA256SUMS.txt").read_bytes(), (dist2 / "SHA256SUMS.txt").read_bytes())

            # 合集静态包根目录有 3 份文档，子目录中绝不含重复文档，且包内 README 链接已转为可用地址
            bundle = dist1 / f"SarasaUiPropDigits-TTF-{spec.version}.zip"
            with zipfile.ZipFile(bundle, "r") as zf:
                names = zf.namelist()
                zip_readme = zf.read("README.md").decode("utf-8")
            self.assertEqual(names[:3], ["LICENSE.txt", "NOTICE.md", "README.md"])
            self.assertEqual(len(names), 3 + 72)
            self.assertFalse(any(n.endswith(("README.txt", "LICENSE-Sarasa-Gothic.txt")) for n in names))
            self.assertIn("[LICENSE](LICENSE.txt)", zip_readme)
            self.assertIn(f"/blob/v{spec.version}/docs/规格.md", zip_readme)
            self.assertIn(f"/blob/v{spec.version}/requirements.lock", zip_readme)
            self.assertIn("(LICENSE.txt)", rewrite_readme_for_zip("[L](LICENSE)", spec.version))

            # 反例：通过检查后篡改任一字体必须触发门禁失败
            sample_vf = vf_dir / spec.variable_filename("SC", False)
            sample_vf.write_bytes(sample_vf.read_bytes() + b"x")
            with self.assertRaisesRegex(ValueError, "发生变化"):
                verify_check_gate(fonts, spec)

            # 反例：篡改某个 ZIP 字节必须触发核验失败
            target_zip = dist1 / f"Sarasa-Ui-VF-PropDigits-SC-TTF-{spec.version}.zip"
            target_zip.write_bytes(target_zip.read_bytes() + b"\x00")
            with self.assertRaisesRegex(ValueError, "摘要不匹配"):
                verify_release_packages(dist1)


if __name__ == "__main__":
    unittest.main()
