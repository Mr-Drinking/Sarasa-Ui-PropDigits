import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_release as package


def fixture(root):
    files = []
    for name in ('LICENSE.txt', 'NOTICE.md', 'README.md', 'font.ttf'):
        path = root / name; path.write_bytes(name.encode('ascii')); files.append((path, name))
    return package.Package('fixture.zip', tuple(files))


class PackageReadOnlyTests(unittest.TestCase):
    def test_failed_zip_validation_preserves_the_previous_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); spec = fixture(root)
            old = root / spec.filename; old.write_bytes(b'previous release')
            with patch.object(package, 'verify_package', side_effect=ValueError('bad ZIP')):
                with self.assertRaisesRegex(ValueError, 'bad ZIP'):
                    package.write_package(spec, root)
            self.assertEqual(old.read_bytes(), b'previous release')

    def test_verify_only_never_writes_checksums_or_release_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); spec = fixture(root)
            package.write_package(spec, root)
            sums = root / 'SHA256SUMS.txt'; sums.write_bytes(b'existing checksums')
            with patch.object(package, 'release_packages', return_value=[spec]), \
                 patch.object(package, 'validate_release_audits'), \
                 patch.object(package, 'write_checksums') as write_sums, \
                 patch.object(package, 'write_release_manifest') as write_manifest, \
                 patch.object(sys, 'argv', ['package', '--verify-only', '--output-dir', str(root)]):
                package.main()
                write_sums.assert_not_called(); write_manifest.assert_not_called()
            self.assertEqual(sums.read_bytes(), b'existing checksums')

    def test_visual_observations_must_be_a_list(self):
        report = json.loads((package.ROOT / 'reports/visual-audit.json').read_text(encoding='utf-8'))
        # Structural fixture: the renderer and its sheets may have changed since the review.
        report['generator_sha256'] = package.package_sha256(package.ROOT / report['generator'])
        for image in report['images']:
            image['sha256'] = package.package_sha256(package.ROOT / image['file'])
        report['images'][0]['observations'] = '字符串不是逐项审阅记录'
        with self.assertRaisesRegex(ValueError, '审阅记录'):
            package.validate_visual_report(report, report['input_manifest'])


if __name__ == '__main__':
    unittest.main()
