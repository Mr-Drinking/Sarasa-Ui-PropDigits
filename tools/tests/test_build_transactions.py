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
    def test_all_rollback_paths_are_attempted_after_one_restore_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'ROOT', Path(directory)):
            root = Path(directory)
            pairs = [(root / f'candidate-{name}', root / name) for name in ('a', 'b', 'c')]
            for source, destination in pairs:
                source.write_bytes(b'new'); destination.write_bytes(b'old')
            original = Path.replace
            def replace(path, target):
                if path == pairs[2][0]: raise OSError('promotion failed')
                if path == pairs[1][1] and target == pairs[1][0]: raise OSError('one candidate could not be moved back')
                return original(path, target)
            with patch.object(Path, 'replace', replace), self.assertRaises(BaseExceptionGroup):
                b.promote_build_outputs(pairs, root / 'previous')
            for _source, destination in pairs:
                self.assertEqual(destination.read_bytes(), b'old')

    def test_refresh_rejects_unattested_or_changed_input_before_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(b, ROOT=root, VARIABLE_DIR=root/'fonts/variable', OUTPUT_VALIDATION_DIR=root/'records'):
                path = b.VARIABLE_DIR/b.variable_output_name('SC', False)
                path.parent.mkdir(parents=True); path.write_bytes(b'old bytes')
                with patch.object(b, '_refresh_variable_finalization_outputs') as transform:
                    with self.assertRaisesRegex(ValueError, '校验记录'):
                        b.refresh_variable_finalization_outputs(['SC'], (False,))
                    transform.assert_not_called()
                record = {'file':path.name,'region':'SC','italic':False,'weight':None,'hinted':False,
                          'sha256':b.file_sha256(path),'contract':{'schema':2,'builder_sha256':'a'*64}}
                b.write_json_atomic(b.output_validation_record_path(path), record)
                b.require_validated_input(path, 'SC', False)
                path.write_bytes(b'new bytes')
                with self.assertRaisesRegex(ValueError, '不匹配'):
                    b.require_validated_input(path, 'SC', False)

    def test_partial_build_cannot_overwrite_full_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(b, ROOT=root, VARIABLE_DIR=root/'fonts/variable', STATIC_ROOT=root/'fonts/static', REPORT_DIR=root/'reports'):
                b.REPORT_DIR.mkdir(); b.VARIABLE_DIR.mkdir(parents=True)
                full = b.REPORT_DIR/'Sarasa-Ui-PropDigits-report.json'; full.write_bytes(b'full report')
                inspection = b.REPORT_DIR/'font-inspection.json'; inspection.write_bytes(b'full inspection')
                path = b.VARIABLE_DIR/b.variable_output_name('SC', False); path.write_bytes(b'font')
                report = {'variable_outputs':[{'file':b.portable_report_path(path),'region':'SC'}], 'static_outputs':[]}
                with patch.object(b, 'inspect_font', return_value={'fixture':True}):
                    b.write_reports(report)
                self.assertEqual(full.read_bytes(), b'full report')
                self.assertEqual(inspection.read_bytes(), b'full inspection')
                partial = json.loads((b.REPORT_DIR/'Sarasa-Ui-PropDigits-report.partial-SC.json').read_text(encoding='utf-8'))
                self.assertFalse(partial['complete_font_matrix'])

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
                    b.REPORT_DIR.mkdir(parents=True, exist_ok=True)
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
