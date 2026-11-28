"""Exercise real v9 cycles and destination transactions; no live services or tasks."""
from contextlib import contextmanager, nullcontext
import copy
import json
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import ongoing
import destination_settings as settings
from publish_control import LocalControl, ControlError
from publisher_controller import Controller
from status_app import StatusReader
from test_ongoing import FakePipeline, FakeSource, snapshot, at

OLD = dict(guild_id='100', channel_id='200')
NEW = dict(guild_id='300', channel_id='400')


class SelectionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = FakeSource()
        self.pipeline = FakePipeline(self.root)
        self.control = LocalControl(self.root, OLD)
        self.control.initialise(paused=False)
        self.worker = ongoing.Watcher(self.root, OLD, self.source, self.pipeline)
        self.controller = Controller(self.worker, self.control, ongoing.next_discovery, logging.getLogger('test'))

    def select(self, number):
        return self.control.set_expected(number)

    def tick(self, offset=0):
        self.controller.tick(at(12) + offset)

    def test_manual_number_overrides_latest_and_survives_restart_and_absence(self):
        self.select(1250)
        self.tick()
        self.assertEqual(self.worker.state['expected'], 1250)
        self.assertEqual(self.control.snapshot()['expected_override']['chapter'], 1250)
        self.worker.save()
        restarted = ongoing.Watcher(self.root, OLD, self.source, self.pipeline)
        controller = Controller(restarted, self.control, ongoing.next_discovery, logging.getLogger('test'))
        controller.tick(at(13))
        self.assertEqual(restarted.state['expected'], 1250)
        self.assertFalse(self.pipeline.sent)

    def test_empty_channel_can_start_at_explicit_chapter(self):
        self.pipeline.latest_number = None
        self.select(1200)
        for offset in (0, 300, 600): self.tick(offset)
        self.assertEqual([s.album.chapter for s in self.pipeline.sent], [1200])
        self.assertIsNone(self.control.snapshot().get('expected_override'))
        self.assertEqual(self.worker.state['expected'], 1201)

    def test_selected_older_gap_is_allowed_but_existing_complete_chapter_is_not_resent(self):
        self.pipeline.latest_number = 1205
        self.select(1200)
        self.pipeline.existing = lambda *args: True
        self.tick()
        self.assertFalse(self.pipeline.sent)
        self.assertIsNotNone(self.control.snapshot().get('expected_override'))
        self.assertEqual(self.worker.state['cycle_result'], 'existing')

    def test_return_to_auto_resets_observation_and_uses_discord_again(self):
        self.select(1200)
        self.tick()
        self.assertIn('observation', self.worker.state)
        self.pipeline.latest_number = 1205
        self.select(None)
        self.tick(1)
        self.assertEqual(self.worker.state['expected'], 1206)
        self.assertNotIn('observation', self.worker.state)

    def test_send_error_does_not_consume_number(self):
        self.select(1200)
        self.pipeline.publish = lambda *a, **k: (_ for _ in ()).throw(ongoing.runtime.UnknownResult('lost response'))
        self.tick(); self.tick(300)
        with self.assertRaises(ongoing.runtime.UnknownResult): self.tick(600)
        self.controller.fail('lost response', at(12) + 900)
        self.assertEqual(self.control.snapshot()['expected_override']['chapter'], 1200)
        self.assertEqual(self.worker.state['override_attempt']['chapter'], 1200)

    def test_successful_manual_check_consumes_number_while_paused(self):
        self.control.pause(True)
        self.select(1200)
        self.control.request_manual()
        for offset in (0, 300, 600): self.tick(offset)
        state = self.control.snapshot()
        self.assertTrue(state['paused'])
        self.assertIsNone(state.get('expected_override'))
        self.assertEqual(state['manual']['status'], 'succeeded')
        self.assertEqual(len(self.pipeline.sent), 1)
        self.assertIsNone(self.worker.state.get('cooldown_until'))

    def test_manual_job_pins_target_and_blocks_selection_changes(self):
        self.select(1200)
        self.control.request_manual()
        self.tick()
        self.assertEqual(self.control.snapshot()['manual']['chapter'], 1200)
        with self.assertRaises(ControlError): self.select(1202)
        with self.assertRaises(ControlError): self.select(None)

    def test_cooldown_gates_auto_but_not_manual_check(self):
        self.select(1200)
        self.worker.publications.summary = lambda *a: dict(last_publication=None, cooldown_until=at(13))
        self.tick()
        self.assertFalse(self.source.calls)
        self.control.request_manual()
        self.tick(1)
        self.assertTrue(self.source.calls)

    def test_maintenance_stops_before_network_and_forbids_commands(self):
        self.control.maintenance(True)
        self.tick()
        self.assertTrue(self.controller.stop_requested)
        self.assertFalse(self.source.calls)
        for action in (lambda: self.select(1200), self.control.request_manual, lambda: self.control.pause(False)):
            with self.assertRaises(ControlError): action()

    def test_late_setting_change_cannot_consume_new_selection(self):
        first = self.select(1200)
        second = self.select(1201)
        self.control.finish_expected(first['id'])
        self.assertEqual(self.control.snapshot()['expected_override'], second)

    def test_negative_fractional_bool_and_zero_are_rejected(self):
        for number in (-1, 0, 1.5, True, '1200'):
            with self.assertRaises(ControlError): self.select(number)

    def test_reader_displays_selection_even_without_worker(self):
        self.select(1250)
        state = StatusReader(self.root, OLD).read()
        self.assertEqual(state['expected'], 1250)

    def test_offline_reset_does_not_show_old_manual_number_as_automatic(self):
        self.select(1250)
        self.control.pause(True)
        self.tick()
        self.select(None)
        restarted = ongoing.Watcher(self.root, OLD, self.source, self.pipeline)
        controller = Controller(restarted, self.control, ongoing.next_discovery, logging.getLogger('test'))
        controller.tick(at(13))
        self.assertIsNone(restarted.state['expected'])

    def test_selection_change_during_download_cancels_before_post(self):
        self.select(1200)
        self.tick(); self.tick(300)
        def download(snap):
            self.select(1201)
            return self.root
        self.pipeline.download = download
        self.tick(600)
        self.assertFalse(self.pipeline.sent)

    def test_completed_override_recovers_before_auto_cooldown(self):
        selection = self.select(1200)
        self.worker.state['override_attempt'] = selection
        self.worker.publications.summary = lambda *a: dict(last_publication=None, cooldown_until=at(13))
        self.pipeline.existing = lambda *a: True
        (self.pipeline.directory / '-334424_2.json').write_text(json.dumps(dict(
            complete=True, chapter='1200', group_plan=[[1, 2]])), encoding='utf-8')
        self.tick()
        self.assertIsNone(self.control.snapshot().get('expected_override'))
        self.assertFalse(self.source.calls)
        self.assertFalse(self.pipeline.sent)


class FakeScheduler:
    def __init__(self, *, enabled=True, running=True):
        self.tasks = {name: dict(xml='xml-' + name, enabled=enabled, running=running if i == 0 else True)
                      for i, name in enumerate(settings.task_names(OLD))}
        self.calls = []
        self.fail_on = None

    def call(self, operation, name, **values):
        self.calls.append((operation, name))
        if self.fail_on == (operation, name):
            self.fail_on = None
            raise ongoing.runtime.AppError('injected scheduler failure')
        if operation == 'read': return copy.deepcopy(self.tasks.get(name))
        if operation == 'disable': self.tasks[name]['enabled'] = False
        if operation == 'restore': self.tasks[name] = dict(**values, running=self.tasks.get(name, {}).get('running', False))
        if operation == 'remove': self.tasks.pop(name, None)
        if operation == 'start': self.tasks[name].update(running=True, enabled=True)


class DestinationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(OLD, vk_mode='api', max_file_mib=8)
        self.config_bytes = json.dumps(self.config).encode('utf-8')
        (self.root / 'config.json').write_bytes(self.config_bytes)
        self.old_control = LocalControl(self.root, OLD)
        self.old_control.initialise(paused=False)
        self.scheduler = FakeScheduler()
        self.original_tasks = copy.deepcopy(self.scheduler.tasks)
        self.verifications = []

    def verify(self, root, config):
        self.verifications.append(config)
        return dict(guild_name='New server', channel_name='chapters')

    def wait(self, root, config):
        self.assertTrue(self.old_control.snapshot()['maintenance'])
        self.scheduler.tasks[settings.task_names(OLD)[0]]['running'] = False

    def change(self, chapter=None, **values):
        return settings.change_destination(self.root, '300', '400', chapter,
            scheduler=self.scheduler, verify=values.get('verify', self.verify),
            wait=values.get('wait', self.wait), channel_guard=values.get('guard', lambda cfg: nullcontext()))

    def assert_rolled_back(self):
        self.assertEqual((self.root / 'config.json').read_bytes(), self.config_bytes)
        self.assertEqual(self.scheduler.tasks, self.original_tasks)
        self.assertFalse(self.old_control.snapshot().get('maintenance'))

    def test_switch_updates_two_tasks_preserves_state_and_leaves_auto_paused(self):
        history = self.old_control.directory / '-334424_2.json'
        history.write_text('{"complete":true}', encoding='utf-8')
        result = self.change(1201)
        self.assertEqual(result['config']['channel_id'], '400')
        self.assertEqual(result['config']['vk_mode'], 'api')
        self.assertEqual(set(self.scheduler.tasks), set(settings.task_names(NEW)))
        new_control = LocalControl(self.root, NEW).snapshot()
        self.assertTrue(new_control['paused'])
        self.assertEqual(new_control['expected_override']['chapter'], 1201)
        self.assertEqual(history.read_text(), '{"complete":true}')
        self.assertFalse(self.old_control.snapshot().get('maintenance'))
        self.assertFalse(any(operation == 'start' for operation, _ in self.scheduler.calls))

    def test_all_old_worker_modes_preserve_task_enabled_state(self):
        for enabled in (True, False):
            for running in (True, False):
                with self.subTest(enabled=enabled, running=running):
                    self.setUp()
                    self.scheduler = FakeScheduler(enabled=enabled, running=running)
                    self.change()
                    self.assertEqual(self.scheduler.tasks[settings.task_names(NEW)[0]]['enabled'], enabled)
                    self.assertTrue(LocalControl(self.root, NEW).snapshot()['paused'])

    def test_failed_access_verification_changes_nothing(self):
        def reject(*args): raise ongoing.runtime.ApiError(403)
        with self.assertRaises(ongoing.runtime.ApiError): self.change(verify=reject)
        self.assertEqual(self.scheduler.calls, [])
        self.assert_rolled_back()

    def test_busy_worker_cancels_and_restores_task_mode(self):
        def busy(*args): raise ongoing.runtime.AppError('busy')
        with self.assertRaises(ongoing.runtime.AppError): self.change(wait=busy)
        self.assert_rolled_back()

    def test_new_task_collision_is_rejected_before_disabling_old(self):
        name = settings.task_names(NEW)[0]
        self.scheduler.tasks[name] = dict(xml='foreign', enabled=True, running=True)
        with self.assertRaises(ongoing.runtime.AppError): self.change()
        self.assertFalse(any(op == 'disable' for op, _ in self.scheduler.calls))
        self.assertEqual(self.scheduler.tasks[name]['xml'], 'foreign')

    def test_registration_and_removal_failures_restore_config_tasks_and_target_control(self):
        for operation, name in [('restore', settings.task_names(NEW)[1]),
                                ('remove', settings.task_names(OLD)[1])]:
            with self.subTest(operation=operation):
                self.setUp()
                target = LocalControl(self.root, NEW)
                target.initialise(paused=False)
                target.set_expected(1300)
                before = target.path.read_bytes()
                self.scheduler.fail_on = (operation, name)
                with self.assertRaises(ongoing.runtime.AppError): self.change(1200)
                self.assert_rolled_back()
                self.assertEqual(target.path.read_bytes(), before)

    def test_manual_job_or_partial_send_blocks_switch(self):
        self.old_control.request_manual()
        with self.assertRaises(ControlError): self.change()
        job = self.old_control.snapshot()['manual']
        self.old_control.update_manual(job['id'], status='failed')
        (self.old_control.directory / '-334424_2.json').write_text(
            '{"card_id":"123","complete":false}', encoding='utf-8')
        with self.assertRaises(ControlError): self.change()
        self.assert_rolled_back()

    def test_duplicate_destination_lock_cancels_switch(self):
        @contextmanager
        def guard(config):
            if config['channel_id'] == '400': raise ongoing.runtime.AppError('duplicate')
            yield
        with self.assertRaises(ongoing.runtime.AppError): self.change(guard=guard)
        self.assert_rolled_back()

    def test_incomplete_rollback_never_restarts_worker(self):
        original_call = self.scheduler.call
        def failing(operation, name, **values):
            if operation == 'restore' and name in (settings.task_names(NEW)[1], settings.task_names(OLD)[0]):
                self.scheduler.calls.append((operation, name))
                raise ongoing.runtime.AppError('restore failed')
            return original_call(operation, name, **values)
        self.scheduler.call = failing
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'Не удалось полностью восстановить'):
            self.change()
        self.assertFalse(any(op == 'start' for op, _ in self.scheduler.calls))
        self.assertTrue(self.old_control.snapshot().get('maintenance'))

    def test_same_destination_and_invalid_ids_are_rejected(self):
        for guild, channel in [('100', '200'), ('0', '4'), ('abc', '4'), ('3', '../4')]:
            with self.assertRaises(ValueError):
                settings.change_destination(self.root, guild, channel, scheduler=self.scheduler, verify=self.verify)
        self.assertEqual((self.root / 'config.json').read_bytes(), self.config_bytes)

    def test_verification_uses_only_read_requests(self):
        calls = []
        class API:
            bot_id = None
            destination = dict(guild_name='Server', channel_name='channel')
            def __init__(self, token): pass
            def close(self): pass
            def request(self, method, route, *a, **kw):
                calls.append(method)
                if route == '/users/@me': return dict(bot=True, id='900', username='OneDelivery')
                if route == '/channels/400': return dict(id='400', guild_id='300', type=0, name='channel')
                if route == '/guilds/300': return dict(id='300', name='Server', owner_id='900')
                if route.endswith('/roles'): return []
                if '/members/' in route: return dict(roles=[])
                return []
        with patch.object(ongoing, 'noninteractive_token', return_value='test'), patch.object(settings.runtime, 'Discord', API):
            settings.verify_destination(self.root, NEW)
        self.assertTrue(calls)
        self.assertEqual(set(calls), {'GET'})


if __name__ == '__main__':
    unittest.main()
