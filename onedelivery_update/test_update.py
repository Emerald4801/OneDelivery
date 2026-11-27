"""Fault injection for file replacement, idle-worker stopping and mode restoration."""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import update_settings as update

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / 'vk_discord_publisher'))
from publish_control import LocalControl


class UpdateChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.package, self.backup = (self.base / name for name in ('root', 'package', 'backup'))
        self.root.mkdir(); (self.package / 'payload').mkdir(parents=True)
        (self.root / '.venv/Scripts').mkdir(parents=True)
        (self.root / '.venv/Scripts/pythonw.exe').touch()
        self.manifest = {'files': {}}
        self.originals = {}
        for i, name in enumerate(update.FILES):
            before = (b'# old\n' + str(i).encode() + b'\n') if i < 5 else None
            after = b'# updated\n' + str(i).encode() + b'\n'
            if before is not None: (self.root / name).write_bytes(before)
            (self.package / 'payload' / name).write_bytes(after)
            self.manifest['files'][name] = dict(before=update.digest(before) if before else None, after=update.digest(after))
            self.originals[name] = before
        self.write_manifest()
        (self.root / 'config.json').write_text('{"guild_id":"100","channel_id":"200"}', encoding='utf-8')
        self.events = []
        self.locked = False
        self.modules = {}
        for name in ('publisher_runtime', 'ongoing', 'publish_control', 'status_app'):
            self.modules[name] = types.SimpleNamespace(__file__=str(self.root / (name + '.py')))
        @contextmanager
        def exclusive(root):
            self.locked = True
            self.events.append('locked')
            try: yield
            finally:
                self.locked = False
                self.events.append('unlocked')
        @contextmanager
        def channel(config):
            self.events.append('channel_locked')
            yield
        self.modules['publisher_runtime'].exclusive = exclusive
        self.modules['ongoing'].channel_lock = channel
        self.modules['publish_control'].LocalControl = LocalControl
        self.modules['status_app'].close_viewer = lambda root: self.events.append('closed_viewer')
        self.snapshot = dict(xml='known-worker-definition', enabled=True, running=True)
        owner = self
        class Tasks:
            def __init__(self, *args): pass
            def call(self, operation, name, **values):
                owner.events.append(operation)
                if operation == 'stop': owner.assertTrue(owner.locked, 'worker stopped without holding its send lock')
                if operation == 'read': return copy.deepcopy(owner.snapshot)
        self.tasks_class = Tasks

    def write_manifest(self):
        (self.package / 'package.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def install(self, **values):
        with patch.object(update, 'Tasks', self.tasks_class), patch.object(update.importlib, 'import_module',
                side_effect=lambda name: self.modules[name]), patch.object(update.subprocess, 'Popen') as popen:
            before = list(sys.path)
            try: update.install(self.root, self.package, **values)
            finally: sys.path[:] = before
            return popen

    def assert_original_files(self):
        for name, original in self.originals.items():
            self.assertEqual((self.root / name).read_bytes() if (self.root / name).exists() else None, original)

    def test_apply_and_explicit_restore_preserve_config_and_all_task_modes(self):
        for enabled in (True, False):
            for running in (True, False):
                with self.subTest(enabled=enabled, running=running):
                    self.setUp()
                    self.snapshot.update(enabled=enabled, running=running)
                    config = (self.root / 'config.json').read_bytes()
                    self.install()
                    self.assertEqual((self.root / 'config.json').read_bytes(), config)
                    self.assertLess(self.events.index('locked'), self.events.index('stop'))
                    self.assertLess(self.events.index('unlocked'), self.events.index('restore'))
                    self.assertEqual('start' in self.events, running)
                    for name in update.FILES:
                        self.assertEqual((self.root / name).read_bytes(), (self.package / 'payload' / name).read_bytes())
                    backup = next((self.root / 'updates').iterdir())
                    self.install(restore=backup)
                    self.assert_original_files()

    def test_unknown_installed_file_and_corrupt_payload_are_rejected_before_stop(self):
        for damage in ('installed', 'package'):
            with self.subTest(damage=damage):
                self.setUp()
                target = self.root if damage == 'installed' else self.package / 'payload'
                (target / update.FILES[0]).write_bytes(b'# unexpected\n')
                with self.assertRaises(ValueError): self.install()
                self.assertNotIn('stop', self.events)

    def test_second_file_replacement_failure_rolls_back_added_and_changed_files(self):
        update.prepare(self.root, self.package, self.backup)
        actual = update.os.replace
        count = 0
        def replace(source, destination):
            nonlocal count
            count += 1
            if count == 2: raise OSError('simulated write failure')
            return actual(source, destination)
        with patch.object(update.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError): update.apply_files(self.root, self.package, self.backup)
        self.assert_original_files()

    def test_changed_source_after_preparation_is_rejected_before_first_replacement(self):
        update.prepare(self.root, self.package, self.backup)
        (self.root / update.FILES[0]).write_bytes(b'# changed after prepare')
        with self.assertRaises(ValueError): update.apply_files(self.root, self.package, self.backup)
        self.assertFalse((self.root / update.FILES[-1]).exists())

    def test_repeat_installation_does_not_stop_worker_or_open_ui(self):
        self.install()
        self.events.clear()
        popen = self.install()
        self.assertNotIn('stop', self.events)
        popen.assert_not_called()

    def test_busy_send_lock_cancels_without_stopping_or_modifying_worker(self):
        @contextmanager
        def busy(factory):
            raise RuntimeError('busy')
            yield
        with patch.object(update, 'wait_lock', busy):
            with self.assertRaises(RuntimeError): self.install()
        self.assertNotIn('stop', self.events)
        self.assert_original_files()

    def test_task_restore_error_reports_installed_code_and_still_reopens_ui(self):
        self.snapshot['running'] = False
        parent = self.tasks_class
        class Failing(parent):
            def call(self, operation, name, **values):
                if operation == 'restore': raise RuntimeError('scheduler restore failed')
                return super().call(operation, name, **values)
        self.tasks_class = Failing
        with patch.object(update.subprocess, 'Popen') as popen:
            with patch.object(update, 'Tasks', self.tasks_class), patch.object(update.importlib, 'import_module',
                    side_effect=lambda name: self.modules[name]):
                before = list(sys.path)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'Код успешно обновлён'):
                        update.install(self.root, self.package)
                finally:
                    sys.path[:] = before
            popen.assert_called_once()


if __name__ == '__main__':
    unittest.main()
