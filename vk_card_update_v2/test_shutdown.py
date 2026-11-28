"""Offline Windows integration tests using private profiles and real app locks."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import publisher_patch as update
import worker_shutdown as shutdown
from test_card_update import APP, BASE_PUBLISHER, v9_pipeline

HERE = Path(__file__).resolve().parent


class WaitChecks(unittest.TestCase):
    def test_wait_retries_busy_lock_and_stops_when_released(self):
        ticks = [0.0]
        attempts = []
        @contextmanager
        def lock(root):
            attempts.append(ticks[0])
            if len(attempts) < 3:
                raise shutdown.WorkerBusy('still exiting')
            yield
        self.assertTrue(shutdown.wait_until_free(None, 1, lock=lock,
                        clock=lambda: ticks[0], sleep=lambda n: ticks.__setitem__(0, ticks[0] + n)))
        self.assertEqual(attempts, [0, 0.25, 0.5])

    def test_unexpected_access_error_is_not_treated_as_busy_or_retried(self):
        @contextmanager
        def lock(root):
            raise PermissionError('cannot read configuration')
            yield
        with self.assertRaises(PermissionError):
            shutdown.wait_until_free(None, 1, lock=lock)


@unittest.skipUnless(os.name == 'nt', 'Native Windows application locks required')
class WindowsUpdateChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='vk_card_update_v2_')
        cls.base = Path(cls.temporary.name).resolve()
        cls.venv = cls.base / 'test_venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages', str(cls.venv)],
                       check=True, capture_output=True, timeout=45)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.root = self.base / ('Тест с пробелами ' + uuid.uuid4().hex[:8])
        self.root.mkdir()
        # Use one disposable venv, linked into each isolated installation.
        # A directory junction is not needed: copying a no-pip venv is small.
        shutil.copytree(self.venv, self.root / '.venv')
        self.python = self.root / '.venv' / 'Scripts' / 'python.exe'
        for path in APP.glob('*.py'):
            shutil.copyfile(path, self.root / path.name)
        (self.root / 'publish_album_v3.py').write_text(BASE_PUBLISHER, encoding='utf-8')
        (self.root / 'ongoing.py').write_text(v9_pipeline(), encoding='utf-8')
        config = {'guild_id': str(uuid.uuid4().int), 'channel_id': str(uuid.uuid4().int)}
        (self.root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        (self.root / 'status_app.py').write_text('raise SystemExit(0)\n', encoding='utf-8')
        self.history = self.root / 'publication-history-test.json'
        self.history.write_text('{"sent": [1199, 1200]}', encoding='utf-8')
        self.before = {name: (self.root / name).read_bytes() for name in update.TRANSFORMS}
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()  # only our own private test helpers
            child.communicate(timeout=10)
        self.assertEqual(self.history.read_text(encoding='utf-8'), '{"sent": [1199, 1200]}')

    def run_python(self, *args, timeout=50):
        env = dict(os.environ, PYTHONUTF8='1')
        return subprocess.run([str(self.python), *map(str, args)], cwd=HERE, env=env,
                              capture_output=True, text=True, encoding='utf-8', timeout=timeout)

    def holder(self, release_delay=None, file_lock_only=False):
        ready, stop = self.root / 'holder_ready', self.root / 'stop_requested'
        code = '''import json, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import worker_shutdown
root = pathlib.Path(sys.argv[2])
ready, stop = root/'holder_ready', root/'stop_requested'
delay = None if sys.argv[3] == 'never' else float(sys.argv[3])
if sys.argv[4] == 'file':
    sys.path.insert(0, str(root))
    import publisher_runtime
    lock = publisher_runtime.exclusive(root)
else:
    lock = worker_shutdown.application_lock(root)
with lock:
    ready.write_text('ready')
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        if delay is not None and stop.exists():
            time.sleep(delay)
            break
        time.sleep(.05)
'''
        child = subprocess.Popen([str(self.python), '-c', code, str(HERE), str(self.root),
                                  'never' if release_delay is None else str(release_delay),
                                  'file' if file_lock_only else 'both'],
                                 env=dict(os.environ, PYTHONUTF8='1'),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(child)
        limit = time.monotonic() + 10
        while not ready.exists():
            if child.poll() is not None:
                self.fail('Test lock holder failed: ' + repr(child.communicate()))
            if time.monotonic() > limit:
                self.fail('Test lock holder did not become ready')
            time.sleep(.05)
        return stop

    def run_installer(self, state='Running', enabled=True, expect=True, stop=None):
        # Booleans are literal PowerShell expressions, paths are passed as quoted literals.
        quote = lambda p: "'" + str(p).replace("'", "''") + "'"
        command = ('& ' + quote(HERE / 'test_installer_windows.ps1') + ' -TestRoot ' + quote(self.root)
                   + ' -InitialState ' + state + ' -InitiallyEnabled $' + str(enabled).lower()
                   + ' -ExpectSuccess $' + str(expect).lower())
        if stop:
            command += ' -StopSignal ' + quote(stop)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                                capture_output=True, timeout=55)
        output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('Isolated Windows installer test passed:', output)
        return output

    def test_installer_restores_each_previous_task_mode(self):
        for state in ('Running', 'Ready'):
            for enabled in (True, False):
                with self.subTest(state=state, enabled=enabled):
                    self.run_installer(state, enabled)
                    # Repeated installation also exercises the idempotent patch.

    def test_installer_waits_even_when_scheduler_already_reports_stopped(self):
        stop = self.holder(release_delay=3)
        started = time.monotonic()
        output = self.run_installer(stop=stop)
        self.assertGreaterEqual(time.monotonic() - started, 3)
        self.assertIn('Publisher locks released.', output)

    def test_busy_worker_cancels_without_source_changes_or_tracebacks(self):
        self.holder()
        output = self.run_installer(expect=False)
        self.assertIn('still busy', output)
        self.assertNotIn('Traceback', output)
        self.assertNotIn('CategoryInfo', output)
        self.assertNotIn('manually started', output)
        for name, data in self.before.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_file_lock_alone_blocks_apply_after_preparation(self):
        backup = self.root / 'backup'
        update.prepare(self.root, backup)
        self.holder(file_lock_only=True)
        result = self.run_python(HERE / 'publisher_patch.py', 'apply', '--root', self.root, '--backup', backup)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stdout + result.stderr)
        for name, data in self.before.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def test_transaction_holds_real_locks_through_both_replacements(self):
        backup = self.root / 'backup'
        update.prepare(self.root, backup)
        code = '''import pathlib, subprocess, sys
sys.path.insert(0, sys.argv[1])
import publisher_patch as patch
helper_path, target_root = sys.argv[1:3]
write = patch.atomic_write
calls = []
def guarded_write(target, data):
    result = subprocess.run([sys.executable, str(pathlib.Path(helper_path)/'worker_shutdown.py'),
                             '--root', target_root, '--timeout', '0'], capture_output=True)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    calls.append(target.name)
    write(target, data)
patch.atomic_write = guarded_write
root, backup = sys.argv[2:4]
sys.argv = ['patch', 'apply', '--root', root, '--backup', backup]
result = patch.main()
assert calls == ['publish_album_v3.py', 'ongoing.py'], calls
raise SystemExit(result)
'''
        result = self.run_python('-c', code, HERE, self.root, backup)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
