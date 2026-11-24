"""阶段 3/5 单元测试：静态实例化元数据（instance.py）与 21 ZIP 打包/核验（package.py）。"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from fontTools.ttLib import TTFont

from sarasa_pd.package import package_release, verify_release_packages
from sarasa_pd.spec import load_spec
from sarasa_pd.static.instance import instantiate_static_from_vf
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
                self.assertEqual(st["OS/2"].panose.bWeight, 10)
                self.assertAlmostEqual(st["post"].italicAngle, spec.italic_angle, places=4)
                self.assertTrue(bool(st["OS/2"].ulUnicodeRange2 & (1 << 25)))
                self.assertFalse(any(r.platformID == 1 for r in st["name"].names))
                self.assertFalse(any(r.nameID == 25 for r in st["name"].names))

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
            (proj / "README.md").write_text("# README\n", encoding="utf-8")

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

            m1 = package_release(fonts, dist1, project_root=proj, spec=spec)
            m2 = package_release(fonts, dist2, project_root=proj, spec=spec)
            self.assertEqual(m1["package_count"], 21)
            self.assertEqual((dist1 / "SHA256SUMS.txt").read_bytes(), (dist2 / "SHA256SUMS.txt").read_bytes())

            # 合集静态包根目录有 3 份文档，子目录中绝不含重复文档
            bundle = dist1 / f"SarasaUiPropDigits-TTF-{spec.version}.zip"
            with zipfile.ZipFile(bundle, "r") as zf:
                names = zf.namelist()
            self.assertEqual(names[:3], ["LICENSE.txt", "NOTICE.md", "README.md"])
            self.assertEqual(len(names), 3 + 72)
            self.assertFalse(any(n.endswith(("README.txt", "LICENSE-Sarasa-Gothic.txt")) for n in names))

            # 反例：篡改某个 ZIP 字节必须触发核验失败
            target_zip = dist1 / f"Sarasa-Ui-VF-PropDigits-SC-TTF-{spec.version}.zip"
            target_zip.write_bytes(target_zip.read_bytes() + b"\x00")
            with self.assertRaisesRegex(ValueError, "摘要不匹配"):
                verify_release_packages(dist1)


if __name__ == "__main__":
    unittest.main()
