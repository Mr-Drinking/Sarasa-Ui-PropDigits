from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_sarasa_ui_propdigits_sc as b
from test_runtime_regressions import fixture_font


class BuildTransactionTests(unittest.TestCase):
    def test_promotion_preserves_concurrently_updated_unrelated_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cache=root/'.cache';cache.mkdir()
            receipts=cache/'validation';receipts.mkdir()
            other=receipts/'unrelated.json';other.write_text('old')
            with patch.multiple(b,ROOT=root,BUILD_CACHE_DIR=cache,STATIC_ROOT=root/'fonts/static',
                                VARIABLE_DIR=root/'fonts/variable',REPORT_DIR=root/'reports',OUTPUT_VALIDATION_DIR=receipts), \
                 patch.object(b,'verified_output_record_status',return_value=(True,'')):
                with b.staged_build_outputs(['SC'],static_only=True,variable_only=False,unhinted_only=True,
                                           resume_static=False,resume_variable=False):
                    other.write_text('updated by another build')
                    b.REPORT_DIR.mkdir(parents=True)
                    for stop in b.SOURCE_HAN_WEIGHT_STOPS:
                        for italic in (False,True):
                            path=b.static_dir('SC',False)/b.static_output_name('SC',str(stop['name']),italic)
                            path.write_bytes(b'font fixture')
                            b.write_json_atomic(b.output_validation_record_path(path),{
                                'region':'SC','weight':str(stop['name']),'italic':italic,'hinted':False})
                self.assertEqual(other.read_text(),'updated by another build')

    def test_failed_promotion_restores_every_previous_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'ROOT', Path(directory)):
            root = Path(directory)
            pairs = [(root / 'candidate-a.ttf', root / 'a.ttf'), (root / 'candidate-b.ttf', root / 'b.ttf')]
            for source, destination in pairs:
                source.write_bytes(b'new'); destination.write_bytes(b'old')
            original = Path.replace
            def replace(path, target):
                if path == pairs[1][0]:
                    raise OSError('simulated publication failure')
                return original(path, target)
            with patch.object(Path, 'replace', replace), self.assertRaisesRegex(OSError, 'publication failure'):
                b.promote_build_outputs(pairs, root / 'previous')
            for source, destination in pairs:
                self.assertEqual(destination.read_bytes(), b'old')
                self.assertEqual(source.read_bytes(), b'new')

    def test_build_failure_keeps_outputs_and_retains_reviewable_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root / '.cache'; cache.mkdir()
            with patch.multiple(b, ROOT=root, BUILD_CACHE_DIR=cache,
                                STATIC_ROOT=root/'fonts/static', VARIABLE_DIR=root/'fonts/variable',
                                REPORT_DIR=root/'reports', OUTPUT_VALIDATION_DIR=cache/'validation'):
                current = b.static_dir('SC', False); current.mkdir(parents=True)
                sentinel = current / 'existing.ttf'; sentinel.write_bytes(b'previous')
                with self.assertRaisesRegex(RuntimeError, 'build failed'):
                    with b.staged_build_outputs(['SC'], static_only=True, variable_only=False,
                                               unhinted_only=True, resume_static=False, resume_variable=False) as transaction:
                        candidate = b.static_dir('SC', False) / 'new.ttf'
                        candidate.write_bytes(b'candidate')
                        raise RuntimeError('build failed')
                self.assertEqual(sentinel.read_bytes(), b'previous')
                self.assertEqual(candidate.read_bytes(), b'candidate')
                self.assertEqual(json.loads((transaction/'status.json').read_text())['status'], 'failed')
                self.assertEqual(b.STATIC_ROOT, root/'fonts/static')

    def test_reference_rejects_stale_version_even_with_an_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'reference.ttf'; font = fixture_font()
            font['name'].setName('Version 0; project obsolete; unhinted', 5, 3, 1, 0x409)
            font.save(path)
            with patch.object(b, 'verified_output_record_status', return_value=(True, '')), \
                 patch.object(b, 'output_identity_status', return_value={'reasons': []}), \
                 patch.object(b, 'upstream_dash_structure_status', return_value={'reasons': []}), \
                 patch.object(b, 'contextual_case_structure_status', return_value={'ok': True}):
                valid, reasons = b.unhinted_reference_status(path, 'SC', 'Regular', False)
            self.assertFalse(valid)
            self.assertIn('wrong reference version', reasons)


if __name__ == '__main__':
    unittest.main()
