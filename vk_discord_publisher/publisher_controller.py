"""One worker handles automatic cycles and durable one-shot manual requests."""
import time
from contextlib import nullcontext


class Controller:
    def __init__(self, worker, control, next_discovery, log, report=None, exclusive=nullcontext):
        self.worker, self.control = worker, control
        self.next_discovery, self.log, self.report = next_discovery, log, report
        self.control.initialise()
        self.next_auto = 0
        self.next_manual = 0
        self.active_manual = None
        self.revision = None
        self.last_clock = None
        self.error = None
        self.exclusive = exclusive

    def reset_observation(self):
        for key in ('observation', 'candidate'):
            self.worker.state.pop(key, None)
        self.worker.state['known'] = []
        self.worker.state['next_discovery'] = 0

    def fail(self, text, retry_at):
        self.error = text
        self.worker.state['error'] = text
        self.worker.status('Проверка завершилась с ошибкой.')
        self.reset_observation()
        if self.active_manual:
            job = self.control.snapshot().get('manual') or {}
            if job.get('id') == self.active_manual and job.get('status') in ('queued', 'running'):
                self.control.update_manual(self.active_manual, status='failed', finished_at=time.time(), message=text)
            self.active_manual = None
            self.next_auto = self.next_discovery(time.time())
        else:
            self.next_auto = retry_at
        self.publish_status()

    def publish_status(self):
        state = self.control.snapshot()
        job = state.get('manual') or {}
        manual_active = job.get('status') in ('queued', 'running')
        next_check = self.next_manual if manual_active and job.get('status') == 'running' else (
            0 if manual_active else None if state['paused'] else self.next_auto)
        self.worker.state['next_check'] = next_check
        self.worker.save()
        if self.report:
            self.report.update(phase=self.worker.state.get('stage'), expected=self.worker.state.get('expected'),
                               last_vk_check=self.worker.state.get('last_vk_check'), next_check=next_check,
                               auto_paused=state['paused'], error=self.error, action_required=False,
                               manual=job or None, **{k: self.worker.state.get(k) for k in ('cooldown_until', 'last_publication')})

    def tick(self, now):
        """Call at most once a second. Only a due cycle can contact external services."""
        state = self.control.snapshot()
        if self.last_clock is not None and now < self.last_clock - 60:
            self.reset_observation()
            self.next_manual = self.next_auto = 0
        self.last_clock = now
        changed = self.revision != state['revision']
        if changed:
            self.revision = state['revision']
            self.control.acknowledge(self.revision)
            self.log.info('Автоматика: %s.', 'ручная пауза' if state['paused'] else 'возобновлена')
            if not self.active_manual:
                self.reset_observation()
            self.next_auto = 0
        job = state.get('manual') or {}
        if job.get('status') in ('queued', 'running'):
            if self.active_manual != job['id']:
                self.active_manual = job['id']
                self.reset_observation()
                self.next_manual = 0
                self.control.update_manual(job['id'], status='running', started_at=job.get('started_at') or now)
                self.log.info('Ручная проверка принята.')
            if now < self.next_manual:
                if changed:
                    self.publish_status()
                return False
            def pin(number):
                self.control.update_manual(job['id'], chapter=number)
            with self.exclusive():
                self.next_manual = self.worker.cycle(now, origin='manual', target=job.get('chapter'), on_expected=pin)
            self.error = None
            self.worker.state.pop('error', None)
            self.worker.state['last_successful_check'] = time.time()
            result = self.worker.state.get('cycle_result')
            if result in ('not_found', 'published', 'existing'):
                message = self.worker.state.get('stage', 'Проверка завершена.')
                self.control.update_manual(job['id'], status='succeeded', finished_at=time.time(), message=message)
                self.log.info('Ручная проверка завершена: %s', message)
                self.active_manual = None
                self.next_auto = self.next_discovery(time.time())
                self.reset_observation()
                self.worker.status('Автоматика на ручной паузе.' if self.control.snapshot()['paused'] else
                                   'Ожидание следующей автоматической проверки.')
            self.publish_status()
            return True
        if state['paused']:
            if changed:
                self.worker.status('Автоматика на ручной паузе. Ручная проверка доступна.')
                self.publish_status()
            return False
        if now < self.next_auto:
            return False
        with self.exclusive():
            self.next_auto = self.worker.cycle(now, can_publish=lambda: not self.control.snapshot()['paused'])
        self.error = None
        self.worker.state.pop('error', None)
        self.worker.state['last_successful_check'] = time.time()
        self.publish_status()
        return True
