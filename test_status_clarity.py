"""Status presentation and observed gallery progress, without real APIs or tasks."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import test_publisher_v3 as gallery
from test_interrupted_resume import InterruptedDiscord
import ongoing
import publish_album_v3 as publisher
from publish_control import LocalControl
import status_app

CFG = dict(guild_id='100', channel_id='200', guild_name='Test server', channel_name='test-channel')


class _StatusProfile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='OneDelivery status ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = LocalControl(self.root, CFG)
        self.control.initialise(paused=False)
        self.now = 10000
        self.raw = dict(schema=1, running=True, heartbeat_at=self.now, guild_id='100', channel_id='200',
                        bot_id='900', phase='Ожидается глава 1201.', expected=1201, next_check=self.now + 300,
                        error=None, action_required=False)

    def view(self):
        ongoing.download.atomic_json(self.control.directory / 'worker_status.json', self.raw)
        state = status_app.StatusReader(self.root, CFG).read(self.now)
        return status_app.status_view(state, CFG, self.now)

class PresentationChecks(_StatusProfile):
    def test_retry_has_a_reason_deadline_and_countdown(self):
        self.raw['error'] = 'Discord недоступен.'
        view = self.view()
        self.assertEqual(view['activity'], 'Ожидание повторной проверки')
        self.assertIn('через 5 мин', view['next'])
        self.assertIn('автоматически', view['action'])
        self.assertEqual(view['activity_style'], 'Warning.TLabel')

    def test_paused_failure_never_promises_an_automatic_retry(self):
        self.control.pause(True)
        self.raw['error'] = 'Ответ на отправку не получен.'
        view = self.view()
        self.assertEqual(view['next'], '—')
        self.assertNotIn('Повтор автоматически', view['action'])
        self.assertIn('Возобновить автоматику', view['action'])

    def test_stale_worker_does_not_promise_a_retry(self):
        self.raw.update(error='offline', heartbeat_at=self.now - 60)
        view = self.view()
        self.assertEqual(view['next'], '—')
        self.assertNotIn('Повтор автоматически', view['action'])
        self.assertIn('обработчика', view['action'])

    def test_failed_manual_job_is_history_after_auto_recovers(self):
        job = self.control.request_manual()
        self.control.update_manual(job['id'], status='failed', finished_at=self.now - 300, message='old network error')
        view = self.view()
        self.assertEqual(view['error'], 'Нет')
        self.assertIn('Предыдущая попытка', view['manual'])
        self.assertIn('old network error', view['manual'])
        self.assertEqual(view['activity'], 'Ожидание следующей проверки')

    def test_in_flight_retry_is_distinct_from_waiting_after_error(self):
        self.raw.update(error='old error', in_cycle=True, next_check=None)
        view = self.view()
        self.assertEqual(view['activity'], 'Выполняется проверка')
        self.assertEqual(view['next'], 'Выполняется сейчас')
        self.assertTrue(view['error'].startswith('Предыдущая попытка:'))
        self.assertEqual(view['error_style'], 'Muted.TLabel')

    def test_saved_six_of_sixteen_is_visible_after_restart_without_new_progress_fields(self):
        ongoing.download.atomic_json(self.control.directory / '-334424_2.json', dict(album='-334424_2',
            chapter='1200', bot_id='900', card_id='1001', schema=3,
            group_plan=[[1, 6], [7, 12], [13, 16]], groups={'1': '1002'}))
        view = self.view()
        self.assertIn('Прогресс сохранён', view['transfer'])
        self.assertIn('подтверждено 6 из 16', view['transfer'])

    def test_active_resume_shows_progress_without_claiming_completion(self):
        self.raw.update(in_cycle=True, transfer=dict(chapter='1200', confirmed=6, total=16,
                                                   resuming=True, stage='sending'))
        view = self.view()
        self.assertEqual(view['activity'], 'Продолжение отправки')
        self.assertIn('подтверждено 6 из 16', view['transfer'])
        self.assertNotIn('завершение подтверждено', view['transfer'])

    def test_all_pages_before_final_edit_are_not_displayed_as_completed(self):
        self.raw.update(in_cycle=True, transfer=dict(chapter='1200', confirmed=16, total=16,
                                                   resuming=True, stage='finalizing'))
        self.assertIn('Ожидается подтверждение завершения', self.view()['transfer'])
        self.raw['transfer']['stage'] = 'complete'
        self.assertIn('завершение подтверждено', self.view()['transfer'])

    def test_fatal_stopped_worker_is_a_visible_error(self):
        self.raw.update(running=False, error='No permission', action_required=True)
        self.assertEqual(self.view()['activity_style'], 'Error.TLabel')

    def test_permissions_are_explained_without_losing_the_missing_right(self):
        text = status_app.friendly_error('Не хватает прав: Create Public Threads, Send Messages in Threads')
        self.assertIn('создание публичных веток', text)
        self.assertIn('отправка сообщений в ветках', text)

    def test_countdown_handles_now_and_days_using_calendar_timestamps(self):
        self.assertEqual(status_app.next_text(self.now, self.now), 'Сейчас')
        self.assertIn('через 3 дн 0 ч', status_app.next_text(self.now + 72 * 3600, self.now))


class ProgressChecks(unittest.TestCase):
    setUp = gallery.PublisherChecks.setUp
    state_path = gallery.PublisherChecks.state_path

    def send(self, observed):
        def progress(value):
            receipt = json.loads(self.state_path().read_text(encoding='utf-8')) if self.state_path().exists() else {}
            observed.append((copy.deepcopy(value), bool(receipt.get('complete'))))
        publisher.publish(self.api, self.config, self.folder, '1199', self.root, progress=progress)

    def test_observer_reports_confirmed_batches_and_completes_only_after_receipt(self):
        self.api = InterruptedDiscord()
        self.api.fail_batch = None
        observed = []
        self.send(observed)
        self.assertEqual([value['confirmed'] for value, _ in observed if value['stage'] == 'sending'], [0, 6, 12])
        self.assertTrue(observed[-1][1])
        self.assertEqual(observed[-1][0]['stage'], 'complete')
        self.assertFalse(any(complete for value, complete in observed if value['stage'] != 'complete'))

    def test_lost_response_keeps_six_until_history_confirms_twelve(self):
        self.api = InterruptedDiscord(accepted=True)
        observed = []
        with self.assertRaises(ongoing.runtime.UnknownResult): self.send(observed)
        self.assertEqual(observed[-1][0]['confirmed'], 6)
        before = sum(event[0] == 'POST' for event in self.api.mutations)
        observed.clear()
        self.send(observed)
        self.assertEqual(observed[0][0]['stage'], 'checking')
        self.assertEqual(observed[0][0]['confirmed'], 6)
        self.assertIn(12, [value['confirmed'] for value, _ in observed])
        self.assertEqual(sum(event[0] == 'POST' for event in self.api.mutations), before)

    def test_observer_failure_never_interrupts_or_duplicates_the_send(self):
        self.api = InterruptedDiscord()
        self.api.fail_batch = None
        def broken(value): raise OSError('status unavailable')
        publisher.publish(self.api, self.config, self.folder, '1199', self.root, progress=broken)
        self.assertTrue(json.loads(self.state_path().read_text(encoding='utf-8'))['complete'])
        self.assertEqual(len(self.api.store['200']), 1)
        self.assertEqual(len(self.api.store['1001']), 2)

    def test_lost_final_edit_keeps_progress_in_confirmation_stage(self):
        self.api = InterruptedDiscord()
        self.api.fail_batch = None
        self.api.fail_at = 'finish'
        observed = []
        with self.assertRaises(ongoing.runtime.UnknownResult): self.send(observed)
        self.assertEqual(observed[-1][0]['stage'], 'finalizing')
        self.assertEqual(observed[-1][0]['confirmed'], 12)
        self.assertFalse(any(value['stage'] == 'complete' for value, _ in observed))
        self.assertFalse(json.loads(self.state_path().read_text(encoding='utf-8')).get('complete'))

    def test_pipeline_passes_progress_to_the_worker_report(self):
        self.api = InterruptedDiscord()
        self.api.fail_batch = None
        observed = []
        class Report:
            def update(self, **values): observed.append(values)
        pipeline = ongoing.Pipeline(self.root, self.config, self.api, report=Report())
        from test_ongoing import snapshot
        snap = snapshot('-334424_310875314', chapter=1199, count=12)
        pipeline.publish(snap, self.folder)
        self.assertEqual(observed[-1]['transfer']['stage'], 'complete')
        self.assertEqual(observed[-1]['transfer']['confirmed'], 12)


@unittest.skipUnless(os.environ.get('ONEDELIVERY_TEST_TK') == '1', 'Native Tk checks run separately with authorized Windows access')
class NativeWindowChecks(_StatusProfile):
    def setUp(self):
        super().setUp()
        self.raw['heartbeat_at'] = time.time()
        self.raw['next_check'] = time.time() + 300
        ongoing.download.atomic_json(self.root / 'config.json', CFG)
        ongoing.download.atomic_json(self.control.directory / 'worker_status.json', self.raw)
        with patch.object(status_app.StatusWindow, 'start_tray'), patch.object(status_app.StatusWindow, 'show'), \
             patch.object(status_app, 'identify_taskbar'):
            self.ui = status_app.StatusWindow(self.root, CFG)
        self.addCleanup(self.close_window)

    def close_window(self):
        for token in self.ui.window.tk.call('after', 'info'):
            self.ui.window.after_cancel(token)
        self.ui.window.destroy()

    def pump(self):
        deadline = time.monotonic() + 5
        while True:
            self.ui.tick()
            self.ui.window.update()
            if not self.ui.command_busy:
                return
            if time.monotonic() > deadline:
                self.fail('Dialog did not finish its local test request')
            time.sleep(.01)

    def test_destination_failure_is_red_persistent_and_can_be_retried_successfully(self):
        import destination_settings
        def listing(root, guild=None):
            return ([dict(id='100', name='Test server'), dict(id='300', name='New server')] if guild is None else
                    [dict(id='200' if guild == '100' else '400', name='test-channel' if guild == '100' else 'news', type=5)])
        new = dict(guild_id='300', channel_id='400', guild_name='New server', channel_name='news')
        def save(*args):
            ongoing.download.atomic_json(self.root / 'config.json', new)
            LocalControl(self.root, new).initialise(paused=True)
            return dict(config=new)
        before = (self.root / 'config.json').read_bytes()
        with patch.object(destination_settings, 'list_destinations', side_effect=listing), \
             patch.object(destination_settings, 'change_destination', side_effect=ongoing.runtime.AppError('Не хватает прав: Create Public Threads')) as change:
            self.ui.choose_destination()
            self.pump()
            dialog = next(widget for widget in self.ui.window.winfo_children() if widget.winfo_class() == 'Toplevel')
            body = dialog.winfo_children()[0]
            server = next(widget for widget in body.winfo_children() if widget.winfo_class() == 'TCombobox' and int(widget.grid_info()['row']) == 1)
            server.current(1)
            server.event_generate('<<ComboboxSelected>>')
            self.ui.window.update()
            self.pump()
            button = next(widget for widget in body.winfo_children() if widget.winfo_class() == 'TButton')
            button.invoke()
            self.pump()
            label = next(widget for widget in body.winfo_children() if widget.winfo_class() == 'TLabel' and int(widget.grid_info()['row']) == 7)
            self.assertIn('Настройки не сохранены', str(dialog.getvar(label.cget('textvariable'))))
            self.assertIn('создание публичных веток', str(dialog.getvar(label.cget('textvariable'))))
            self.assertEqual(label.cget('style'), 'SaveError.TLabel')
            self.assertEqual((self.root / 'config.json').read_bytes(), before)
            self.assertEqual(server.current(), 1)
            self.assertEqual(button.cget('text'), 'Повторить сохранение')
            change.side_effect = save
            button.invoke()
            self.pump()
            self.assertFalse(dialog.winfo_exists())
            self.assertEqual(self.ui.values['server'].get(), 'New server')
            self.assertEqual(self.ui.values['channel'].get(), '#news')
            self.assertEqual(self.ui.value_labels['settings'].cget('style'), 'Success.TLabel')

    def test_main_window_separates_historical_failure_and_current_error(self):
        job = self.control.request_manual()
        self.control.update_manual(job['id'], status='failed', finished_at=time.time(), message='Старая ошибка связи.')
        self.ui.tick()
        self.assertEqual(self.ui.values['error'].get(), 'Нет')
        self.assertIn('Предыдущая попытка', self.ui.values['manual'].get())
        self.assertEqual(self.ui.value_labels['manual'].cget('style'), 'Muted.TLabel')
        self.ui.window.update_idletasks()
        self.assertLessEqual(self.ui.window.winfo_reqheight(), 760)

    def test_waiting_manual_operation_can_retry_or_cancel_without_enabling_auto(self):
        self.control.pause(True)
        job = self.control.request_manual()
        self.control.update_manual(job['id'], status='running', chapter=1200)
        self.control.record_failure('Нет соединения.', retry_at=time.time() + 300, manual_id=job['id'])
        self.ui.tick()
        self.assertEqual(self.ui.manual_button.cget('text'), 'Повторить сейчас')
        self.assertEqual(str(self.ui.manual_button.cget('state')), 'normal')
        self.assertTrue(self.ui.cancel_manual_button.grid_info())
        self.assertIn('Ожидает повторной попытки', self.ui.values['manual'].get())
        self.ui.cancel_manual_button.invoke()
        self.pump()
        self.assertTrue(self.control.snapshot()['paused'])
        self.assertTrue(self.control.snapshot()['manual']['cancelled'])
        self.assertFalse(self.ui.cancel_manual_button.grid_info())
        self.assertIn('отменена', self.ui.values['manual'].get())

    def test_diagnostic_window_is_read_only_and_network_runs_only_after_button(self):
        import diagnostics
        checks = dict(status='unavailable',items=[])
        before = self.control.path.read_bytes()
        with patch.object(diagnostics,'tasks',return_value=checks), \
             patch.object(diagnostics,'services',return_value=dict(discord=dict(status='ok',message='Тест связи'))) as network:
            self.ui.show_diagnostics()
            dialog = self.ui.diagnostic_window
            buttons = next(widget for widget in dialog.winfo_children() if widget.winfo_class() == 'TFrame')
            check, export = buttons.winfo_children()
            def wait_ready():
                deadline = time.monotonic()+5
                while str(export.cget('state')) == 'disabled':
                    self.ui.tick()
                    self.ui.window.update()
                    if time.monotonic()>deadline: self.fail('diagnostic thread did not finish')
                    time.sleep(.01)
            wait_ready()
            network.assert_not_called()
            self.assertIn('Сборка',str(self.ui.version_label.cget('text')))
            check.invoke()
            wait_ready()
            network.assert_called_once()
            export.invoke()
            self.assertEqual(len(list((self.root/'diagnostics').glob('*.json'))),1)
        self.assertEqual(self.control.path.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
