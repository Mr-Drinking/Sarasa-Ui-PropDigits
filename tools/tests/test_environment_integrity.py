import base64
import hashlib
from importlib.metadata import FileHash, PackagePath
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import python_env_bootstrap as bootstrap
import build_sarasa_ui_propdigits_sc as build


class EnvironmentIntegrityTests(unittest.TestCase):
    def test_unchanged_version_does_not_hide_changed_package_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); payload = root / 'leaf.py'; payload.write_bytes(b'correct')
            lock = root / 'requirements.lock'
            lock.write_text('leaf==1.0 --hash=sha256:' + 'a' * 64 + '\n', encoding='utf-8')
            entry = PackagePath('leaf.py')
            entry.hash = FileHash('sha256=' + base64.urlsafe_b64encode(hashlib.sha256(b'correct').digest()).rstrip(b'=').decode())
            dist = SimpleNamespace(files=[entry], locate_file=lambda path: root / path)
            with patch.object(bootstrap.importlib.metadata, 'version', return_value='1.0'), \
                 patch.object(bootstrap.importlib.metadata, 'distribution', return_value=dist):
                self.assertTrue(bootstrap.locked_environment_status(lock)['ok'])
                stamp = payload.stat(); payload.write_bytes(b'changed')
                os.utime(payload, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                result = bootstrap.locked_environment_status(lock)
                self.assertFalse(result['ok'])
                self.assertEqual(result['mismatches'], ['leaf'])

    def test_broken_target_venv_cannot_reexec_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'broken'
            with patch.dict(os.environ, {'SARASA_SKIP_PYTHON_DEPS': '0', 'SARASA_PYTHON_VENV': str(target),
                                        'SARASA_PYTHON_REEXEC_TARGET': str(target)}), \
                 patch.object(bootstrap.subprocess, 'call', side_effect=AssertionError('must not restart')):
                with self.assertRaisesRegex(RuntimeError, '重复启动'):
                    bootstrap.ensure_project_python({}, project_root=Path(directory), label='test')

    def test_private_tool_precedes_path_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory); executable = scripts / 'otc2otf.exe'; executable.touch()
            with patch.dict(os.environ, {'OTC2OTF': ''}), \
                 patch.object(build, 'python_script_dirs', return_value=[scripts]), \
                 patch.object(build.platform, 'system', return_value='Windows'), \
                 patch.object(build.shutil, 'which', return_value='unrelated/otc2otf.exe'):
                self.assertEqual(build.tool_executable('OTC2OTF', 'otc2otf'), str(executable))

    def test_output_record_binds_dependency_versions(self):
        with patch.object(build, 'locked_requirements', return_value={'leaf': '1.0'}):
            with patch.object(build.importlib.metadata, 'version', return_value='1.0'):
                first = build.output_validation_contract(False)
            with patch.object(build.importlib.metadata, 'version', return_value='2.0'):
                second = build.output_validation_contract(False)
        self.assertNotEqual(first, second)
        self.assertIn('font_layout.py', first['build_components_sha256'])


if __name__ == '__main__':
    unittest.main()
