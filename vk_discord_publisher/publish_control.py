"""Durable single-slot commands shared by the window and the sole worker."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from worker_status import destination


class ControlError(RuntimeError):
    pass


class LocalControl:
    def __init__(self, root, config):
        self.directory = destination(root, config)
        self.path = self.directory / 'control.json'
        self.scope = [str(config['guild_id']), str(config['channel_id']),
                      config.get('vk_albums_url', 'https://vk.ru/albums-334424')]

    def snapshot(self):
        if not self.path.exists():
            return dict(schema=1, scope=self.scope, paused=True, revision=0, settings=None, manual=None)
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if (state['schema'] != 1 or state['scope'] != self.scope
                    or type(state['paused']) is not bool or type(state['revision']) is not int):
                raise ValueError()
            settings, job = state.get('settings'), state.get('manual')
            if settings is not None and (not isinstance(settings, dict)
                                          or settings.get('status') not in ('queued', 'applied')):
                raise ValueError()
            if job is not None and (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                                    or not job.get('id') or job.get('status') not in ('queued', 'running', 'succeeded', 'failed')
                                    or (job.get('chapter') is not None and (type(job['chapter']) is not int or job['chapter'] <= 0))):
                raise ValueError()
            return state
        except (ValueError, KeyError, TypeError, OSError):
            raise ControlError('Не удалось прочитать команды управления. Файл сохранён для проверки.') from None

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'control.lock').open('a+b') as stream:
            if stream.tell() == 0:
                stream.write(b'0'); stream.flush()
            deadline = time.monotonic() + 3
            while True:
                try:
                    stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ControlError('Управление занято. Повторите действие.') from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _write(self, state):
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                             prefix='control_', suffix='.tmp', delete=False) as stream:
                path = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush(); os.fsync(stream.fileno())
            path.replace(self.path)
        finally:
            if path:
                path.unlink(missing_ok=True)

    def initialise(self, paused=True):
        with self.locked():
            state = self.snapshot()
            if not self.path.exists():
                state['paused'] = bool(paused)
                self._write(state)
            return state

    def pause(self, paused):
        with self.locked():
            state = self.snapshot()
            state['paused'] = bool(paused)
            state['revision'] += 1
            state['settings'] = dict(id=uuid.uuid4().hex, revision=state['revision'],
                                     paused=bool(paused), status='queued', requested_at=time.time())
            self._write(state)
            return state['settings']

    def acknowledge(self, revision):
        with self.locked():
            state = self.snapshot()
            if state['revision'] == revision and state.get('settings'):
                state['settings'].update(status='applied', applied_at=time.time())
                self._write(state)

    def request_manual(self):
        with self.locked():
            state = self.snapshot()
            previous = state.get('manual') or {}
            if previous.get('status') in ('queued', 'running'):
                return previous
            request = dict(id=uuid.uuid4().hex, status='queued', requested_at=time.time(), chapter=None)
            state['manual'] = request
            self._write(state)
            return request

    def update_manual(self, ident, **values):
        with self.locked():
            state = self.snapshot()
            job = state.get('manual') or {}
            if job.get('id') != ident or job.get('status') not in ('queued', 'running'):
                raise ControlError('Ручная проверка уже завершена или заменена.')
            if 'chapter' in values and job.get('chapter') not in (None, values['chapter']):
                raise ControlError('Номер главы ручной проверки изменился.')
            job.update(values)
            self._write(state)
            return job


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--paused', choices=('true', 'false'), default='true')
    parser.add_argument('--set-paused', action='store_true')
    args = parser.parse_args()
    config = json.loads((args.root / 'config.json').read_text(encoding='utf-8'))
    control = LocalControl(args.root, config)
    control.initialise(paused=args.paused == 'true')
    if args.set_paused:
        control.pause(args.paused == 'true')


if __name__ == '__main__':
    main()
