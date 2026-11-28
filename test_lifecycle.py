"""Startup, persisted settings and native process isolation; no live APIs or tasks."""
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import uuid

APP = Path(__file__).resolve().parent / 'vk_discord_publisher'
sys.path.insert(0, str(APP))
import ongoing
import status_app
from publish_control import LocalControl


CHILD = '''
import json, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import ongoing, status_app
from publish_control import LocalControl
root = pathlib.Path(sys.argv[2])
config = json.loads((root/'config.json').read_text(encoding='utf-8'))
mode = sys.argv[3]
if mode == 'worker':
    raise SystemExit(ongoing.main(['--root',str(root),'--once']))
elif mode == 'file':
    try:
        with ongoing.runtime.exclusive(root): print('entered')
    except ongoing.runtime.AppError: print('busy')
elif mode == 'viewer':
    with status_app.viewer_instance(root) as (first, poll): print(json.dumps(first))
elif mode == 'hold-viewer':
    with status_app.viewer_instance(root) as (first, poll):
        assert first
        (root/'viewer-ready').touch()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if poll() == 'quit':
                print('quit')
                break
            time.sleep(.02)
        else: raise RuntimeError('No quit event')
elif mode == 'manual':
    print(LocalControl(root,config).request_manual()['id'])
'''


class ProfileChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='OneDelivery lifecycle ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(guild_id=str(uuid.uuid4().int), channel_id=str(uuid.uuid4().int))
        ongoing.download.atomic_json(self.root / 'config.json', self.config)
        self.control = LocalControl(self.root, self.config)
        self.control.initialise(paused=True)


@unittest.skipUnless(os.name == 'nt', 'Native Windows locks required')
class WindowsLifecycleChecks(ProfileChecks):
    def command(self, mode):
        return [sys.executable, '-c', CHILD, str(APP), str(self.root), mode]

    def child(self, mode):
        env = dict(os.environ, PYTHONUTF8='1')
        env.pop('DISCORD_BOT_TOKEN', None)
        env.pop('VK_API_TOKEN', None)
        return subprocess.run(self.command(mode), capture_output=True, text=True, encoding='utf-8',
                              env=env, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_second_worker_exits_before_touching_live_status_or_credentials(self):
        directory = self.control.directory
        marker = directory / 'worker_status.json'
        original = b'{"private_test_marker":true}'
        marker.write_bytes(original)
        before = (self.root / 'config.json').read_bytes()
        with ongoing.channel_lock(self.config):
            result = self.child('worker')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('уже запущена', result.stdout)
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual((self.root / 'config.json').read_bytes(), before)
        with ongoing.channel_lock(self.config):
            pass  # clean exit released the duplicate handle without releasing ours

    def test_send_file_lock_blocks_another_process_and_is_released(self):
        with ongoing.runtime.exclusive(self.root):
            result = self.child('file')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'busy')
        result = self.child('file')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'entered')

    def test_second_viewer_signals_existing_instance_then_a_new_instance_can_start(self):
        with status_app.viewer_instance(self.root) as (first, poll):
            self.assertTrue(first)
            result = self.child('viewer')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout))
            self.assertEqual(poll(), 'show')
        result = self.child('viewer')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))

    def test_close_viewer_stops_only_private_viewer_without_changing_control(self):
        before = self.control.path.read_bytes()
        child = subprocess.Popen(self.command('hold-viewer'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=dict(os.environ, PYTHONUTF8='1'), creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / 'viewer-ready').exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    self.fail('Private viewer did not start')
                time.sleep(.02)
            status_app.close_viewer(self.root)
            output, error = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, error)
            self.assertEqual(output.strip(), b'quit')
            self.assertEqual(self.control.path.read_bytes(), before)
        finally:
            if child.poll() is None:
                child.terminate()  # only the helper created by this test
                child.communicate(timeout=5)

    def test_concurrent_manual_requests_coalesce_to_one_durable_job(self):
        children = [subprocess.Popen(self.command('manual'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=dict(os.environ, PYTHONUTF8='1'), creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(3)]
        try:
            ids = []
            for child in children:
                output, error = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, error)
                ids.append(output.decode().strip())
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(self.control.snapshot()['manual']['id'], ids[0])
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)


class StartupChecks(ProfileChecks):
    def boot(self, check=None, *, once=True, stop_after_tick=False, source_override=None, sleep_action=None):
        api = types.SimpleNamespace(bot_id='900', close=lambda: None)
        source = source_override or types.SimpleNamespace(owner='-334424', close=lambda: None)
        log = logging.Logger('isolated-bootstrap')
        log.addHandler(logging.NullHandler())
        original_tick = ongoing.Controller.tick
        clock = [time.time()]
        def sleep(seconds):
            clock[0] += seconds
            if sleep_action:
                sleep_action()
        def tick(controller, now):
            result = original_tick(controller, now)
            controller.stop_requested = True
            return result
        with patch.object(ongoing, 'logger', return_value=log), \
             patch.object(ongoing, 'verify_dependencies'), \
             patch.object(ongoing, 'noninteractive_token', return_value='test-only') as token, \
             patch.object(ongoing.publisher, 'Discord', return_value=api), \
             patch.object(ongoing.runtime, 'check', side_effect=check) as checked, \
             patch.object(ongoing, 'VKSource', return_value=source), \
             patch.object(ongoing.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(ongoing.time, 'sleep', side_effect=sleep) as slept, \
             patch.object(ongoing.Controller, 'tick', tick if stop_after_tick else original_tick):
            code = ongoing.main(['--root', str(self.root)] + (['--once'] if once else []))
            return code, checked.call_count, slept.call_count, token.call_count

    def test_paused_start_and_restart_keep_manual_number_and_do_not_publish(self):
        selection = self.control.set_expected(1250)
        with patch.object(ongoing.Pipeline, 'latest', side_effect=AssertionError('Unexpected Discord history')):
            for _ in range(2):
                code, checks, _, _ = self.boot()
                self.assertEqual(code, 0)
                self.assertEqual(checks, 1)
                self.assertTrue(self.control.snapshot()['paused'])
                self.assertEqual(self.control.snapshot()['expected_override'], selection)
                state = status_app.StatusReader(self.root, self.config).read()
                self.assertEqual(state['expected'], 1250)
                self.assertFalse(state['running'])

    def test_start_without_network_retries_then_enters_paused_controller(self):
        result = self.boot([ongoing.runtime.TransientError('offline'), None], once=False, stop_after_tick=True)
        self.assertEqual(result[:3], (0, 2, 60))
        self.assertTrue(self.control.snapshot()['paused'])

    def test_permission_failure_fails_queued_job_and_requires_intervention(self):
        self.control.request_manual()
        code, checks, sleeps, _ = self.boot(ongoing.runtime.ApiError(403))
        self.assertEqual((code, checks, sleeps), (1, 1, 0))
        self.assertEqual(self.control.snapshot()['manual']['status'], 'failed')
        state = status_app.StatusReader(self.root, self.config).read()
        self.assertTrue(state['action_required'])
        self.assertFalse(state['running'])

    def test_maintenance_start_exits_before_reading_credentials(self):
        self.control.maintenance(True)
        code, checks, sleeps, tokens = self.boot()
        self.assertEqual((code, checks, sleeps, tokens), (0, 0, 0, 0))

    def test_reopened_viewer_reads_changed_config_and_its_own_control(self):
        new = dict(guild_id='300', channel_id='400', guild_name='New server', channel_name='new-channel')
        LocalControl(self.root, new).initialise(paused=True)
        LocalControl(self.root, new).set_expected(1250)
        ongoing.download.atomic_json(self.root / 'config.json', new)
        observed = []
        class Window:
            def __init__(window, root, config, tray, poll):
                observed.append((config, status_app.StatusReader(root, config).read()))
            def run(window): pass
        with patch.object(status_app, 'StatusWindow', Window):
            self.assertEqual(status_app.main(['--root', str(self.root)]), 0)
        self.assertEqual(observed[0][0], new)
        self.assertEqual(observed[0][1]['expected'], 1250)
        self.assertTrue(observed[0][1]['control']['paused'])


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell required')
class TaskStartupChecks(ProfileChecks):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory(prefix='OneDelivery task environment ')
        cls.venv = Path(cls.base.name) / 'venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(cls.venv)],
                       check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        super().setUp()
        shutil.copytree(self.venv, self.root / '.venv')
        for name in ('control_task.ps1', 'task.ps1', 'publish_control.py', 'worker_status.py'):
            (self.root / name).write_bytes((APP / name).read_bytes())

    def execute(self, mode, *, running=True, enabled=True, foreign=False, preview=False):
        wrapper = APP.parent / 'onedelivery_update' / 'test_control_task.ps1'
        command = ['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File', str(wrapper),
                   '-Root', str(self.root), '-Mode', mode, '-State', 'Running' if running else 'Ready',
                   '-Enabled', str(int(enabled))]
        if foreign: command.append('-Foreign')
        if preview: command.append('-Preview')
        result = subprocess.run(command, encoding='utf-8', errors='replace',
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        try: body = json.loads(result.stdout.lstrip('\ufeff'))
        except ValueError: self.fail('Invalid PowerShell response: ' + result.stdout + result.stderr)
        return result.returncode, body

    def test_manual_on_running_enabled_worker_keeps_auto_mode_and_job(self):
        self.control.pause(False)
        job = self.control.request_manual()
        code, result = self.execute('Manual')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['calls'], ['enable'])
        self.assertFalse(self.control.snapshot()['paused'])
        self.assertEqual(self.control.snapshot()['manual']['id'], job['id'])

    def test_manual_restart_pauses_auto_before_enabling_or_starting(self):
        for running, enabled in ((False, True), (False, False), (True, False)):
            with self.subTest(running=running, enabled=enabled):
                self.control.pause(False)
                code, result = self.execute('Manual', running=running, enabled=enabled)
                self.assertEqual(code, 0, result)
                self.assertTrue(self.control.snapshot()['paused'])
                self.assertEqual(result['calls'], ['enable'] if running else ['enable', 'start'])

    def test_resume_only_wakes_the_expected_worker_without_resetting_control(self):
        before = self.control.path.read_bytes()
        for running in (False, True):
            code, result = self.execute('Resume', running=running, enabled=False)
            self.assertEqual(code, 0, result)
            self.assertEqual(result['calls'], ['enable'] if running else ['enable', 'start'])
            self.assertEqual(self.control.path.read_bytes(), before)

    def test_foreign_task_is_rejected_before_control_or_scheduler_changes(self):
        before = self.control.path.read_bytes()
        code, result = self.execute('Manual', running=False, foreign=True)
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['calls'], [])
        self.assertEqual(self.control.path.read_bytes(), before)

    @unittest.skipUnless(os.environ.get('ONEDELIVERY_TEST_NATIVE_TASK_PREVIEW') == '1',
                         'Native CIM preview is run separately with authorized Windows access')
    def test_real_task_definition_has_restart_and_duplicate_protection_without_registration(self):
        code, result = self.execute('Resume', preview=True)
        self.assertEqual(code, 0, result)
        self.assertTrue(result['ok'])
        self.assertEqual(result['calls'], [])
        task = result['definition']
        self.assertEqual(task['Execute'], str(self.root / '.venv' / 'Scripts' / 'pythonw.exe'))
        self.assertEqual(task['Arguments'], '"' + str(self.root / 'ongoing.py') + '"')
        self.assertEqual(task['RestartCount'], 999)
        self.assertEqual(task['RestartInterval'], 'PT1M')
        self.assertEqual(task['ExecutionTimeLimit'], 'PT0S')
        self.assertEqual(task['MultipleInstances'], 2)  # CIM enum IgnoreNew


if __name__ == '__main__':
    unittest.main()
