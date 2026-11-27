"""Full runtime refresh must retain data and remain independent of old transfer folders."""
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_full_package as builder
import full_install
from full_package import FULL_FILES, PRESERVED_FILES
import test_update as fixtures
import update_settings as update

PROJECT = Path(__file__).resolve().parent.parent
APP = PROJECT / 'vk_discord_publisher'


class BundleChecks(unittest.TestCase):
    def test_archive_contains_complete_local_import_closure_and_no_user_data(self):
        for name in FULL_FILES:
            if not name.endswith('.py'):
                continue
            tree = ast.parse((APP / name).read_text(encoding='utf-8-sig'))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split('.')[0])
            for module in imports:
                if (APP / (module + '.py')).is_file():
                    self.assertIn(module + '.py', FULL_FILES, (name, module))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copyfile(PROJECT / 'INSTALL_ONEDELIVERY.cmd', root / 'INSTALL_ONEDELIVERY.cmd')
            with patch.object(builder, 'PROJECT', root), patch.object(builder.subprocess, 'check_output', return_value=b'test-build\n'), \
                    contextlib.redirect_stdout(io.StringIO()):
                archive_path = builder.build()
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(set(archive.namelist()), full_install.ARCHIVE_FILES)
                self.assertFalse(any(name in archive.namelist() for name in PRESERVED_FILES))
                self.assertIsNone(archive.testzip())
            with full_install.unpack(archive_path) as package:
                manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['kind'], 'full-runtime')
                self.assertEqual(set(manifest['files']), set(FULL_FILES))
                for name in FULL_FILES:
                    self.assertEqual(update.digest((package / 'payload' / name).read_bytes()), manifest['files'][name]['after'])

    def test_archive_rejects_extra_or_traversal_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'invalid.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('../token.dpapi', 'test-data')
            with self.assertRaises(ValueError):
                with full_install.unpack(archive):
                    self.fail('An unrecognized archive was accepted')


class FullRefreshChecks(unittest.TestCase):
    def setUp(self):
        fixtures.UpdateChecks.setUp(self)
        self.manifest['kind'] = 'full-runtime'
        for name in FULL_FILES:
            if name in self.manifest['files']:
                continue
            before, after = b'# previous complete runtime\n', b'# new complete runtime\n'
            (self.root / name).write_bytes(before)
            (self.package / 'payload' / name).write_bytes(after)
            self.manifest['files'][name] = dict(before=update.digest(before), after=update.digest(after))
            self.originals[name] = before
        self.write_manifest()
        for name, value in {'token.dpapi': b'opaque-discord-dpapi-test',
                            'vk_api_token.dpapi': b'opaque-vk-dpapi-test',
                            'state/100/200/control.json': b'{"paused":true}',
                            'state/100/200/receipt.json': b'{"complete":true}',
                            'ui/tray_icon.png': b'custom-icon-test',
                            'downloads/retained.bin': b'download-test',
                            'logs/ongoing.log': b'log-test'}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.data_before = {name: (self.root / name).read_bytes() for name in
            ('config.json', 'token.dpapi', 'vk_api_token.dpapi', 'state/100/200/control.json',
             'state/100/200/receipt.json', 'ui/tray_icon.png', 'downloads/retained.bin', 'logs/ongoing.log',
             '.venv/Scripts/pythonw.exe')}
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    write_manifest = fixtures.UpdateChecks.write_manifest
    assert_original_files = fixtures.UpdateChecks.assert_original_files

    def refresh(self, restore=None):
        with patch.object(update, 'Tasks', self.tasks_class), patch.object(update.importlib, 'import_module',
                side_effect=lambda name: self.modules[name]), patch.object(update.subprocess, 'Popen'):
            original_path = list(update.sys.path)
            try:
                full_install.refresh(self.root, self.package, restore)
            finally:
                update.sys.path[:] = original_path

    def assert_data_preserved(self):
        for name, data in self.data_before.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_full_refresh_repeat_and_rollback_retain_tokens_config_history_icon_and_venv(self):
        self.refresh()
        for name in FULL_FILES:
            self.assertEqual((self.root / name).read_bytes(), (self.package / 'payload' / name).read_bytes())
        self.assert_data_preserved()
        self.events.clear()
        self.refresh()
        self.assertNotIn('stop', self.events)
        backup = next((self.root / 'updates').iterdir())
        self.refresh(restore=backup)
        self.assert_original_files()
        self.assert_data_preserved()

    def test_missing_saved_token_stops_before_task_or_file_changes(self):
        (self.root / 'vk_api_token.dpapi').unlink()
        with self.assertRaisesRegex(ValueError, 'токены'):
            self.refresh()
        self.assertEqual(self.events, [])
        self.assert_original_files()

    def test_partial_file_failure_restores_full_runtime_without_touching_data(self):
        replace = update.os.replace
        count = 0
        def failing(source, target):
            nonlocal count
            if Path(target).parent == self.root:
                count += 1
                if count == 17:
                    raise OSError('simulated full replacement failure')
            return replace(source, target)
        with patch.object(update.os, 'replace', side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.refresh()
        self.assert_original_files()
        self.assert_data_preserved()

    def test_data_file_cannot_be_added_to_full_manifest(self):
        self.manifest['files']['token.dpapi'] = dict(before=None, after='invalid')
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.refresh()
        self.assertEqual(self.events, [])
        self.assert_data_preserved()


if __name__ == '__main__':
    unittest.main()
