"""Switch the sole destination, retaining each channel's state and task definitions."""
from contextlib import contextmanager, ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import download_album
import publisher_runtime as runtime
from publish_control import LocalControl, ControlError
from publication_state import RECEIPT_NAME


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def settings_lock(root):
    # A separate lock, since the worker may still own publisher.lock while exiting.
    lock_config = dict(guild_id='0', channel_id='0')
    guard = LocalControl(root, lock_config)
    guard.directory = Path(root)  # root/control.lock, not a synthetic destination in state/
    with guard.locked():
        yield


class Scheduler:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def call(self, operation, name, **values):
        request = dict(operation=operation, name=name, root=str(self.root), **values)
        try:
            process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(self.root / 'task_bridge.ps1')], input=json.dumps(request),
                encoding='utf-8', capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = json.loads(process.stdout.lstrip('\ufeff'))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise runtime.AppError('Планировщик Windows не ответил. Проверьте состояние настройки.') from None
        if not result.get('ok') or process.returncode:
            raise runtime.AppError('Ошибка Планировщика: ' + result.get('error', 'неизвестная ошибка'))
        return result.get('result')


def task_names(config):
    suffix = str(config['guild_id']) + '-' + str(config['channel_id'])
    return ['VKDiscordPublisher-' + suffix, 'VKDiscordPublisher-Status-' + suffix]


def ensure_no_pending(control):
    if (control.snapshot().get('manual') or {}).get('status') in ('queued', 'running'):
        raise ControlError('Сначала дождитесь завершения ручной проверки.')
    for path in control.directory.glob('*.json'):
        if RECEIPT_NAME.fullmatch(path.name):
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if not receipt.get('complete') and (receipt.get('card_id') or receipt.get('pending_card')):
                raise ControlError('Есть незавершённая публикация в этом канале. Сначала завершите её.')


def wait_for_shutdown(root, config, timeout=30):
    from ongoing import channel_lock
    deadline = time.monotonic() + timeout
    while True:
        try:
            with channel_lock(config), runtime.exclusive(root):
                return
        except runtime.AppError:
            if time.monotonic() >= deadline:
                raise runtime.AppError('Обработчик ещё занят. Смена отменена; прежнее назначение сохранено.') from None
            time.sleep(0.25)


def verify_destination(root, config):
    # check() issues only GET requests. Do not use Pipeline.latest(): it may recover/edit a card.
    from ongoing import noninteractive_token
    api = runtime.Discord(noninteractive_token(Path(root)))
    try:
        with redirect_stdout(io.StringIO()):
            runtime.check(api, config)
        return api.destination
    finally:
        api.close()


def list_destinations(root, guild=None):
    from ongoing import noninteractive_token
    api = runtime.Discord(noninteractive_token(Path(root)))
    try:
        if guild is not None:
            if not str(guild).isdigit():
                raise ValueError('Некорректный сервер.')
            channels = api.request('GET', f'/guilds/{guild}/channels')
            return [dict(id=str(c['id']), name=c['name']) for c in
                    sorted(channels, key=lambda c: (c.get('position', 0), int(c['id']))) if c['type'] == 0]
        found, after = [], None
        while True:
            batch = api.request('GET', '/users/@me/guilds', params=dict(limit=200, **({'after': after} if after else {})))
            found.extend(dict(id=str(g['id']), name=g['name']) for g in batch)
            if len(batch) < 200:
                return sorted(found, key=lambda g: (g['name'].casefold(), g['id']))
            next_after = str(max(int(g['id']) for g in batch))
            if next_after == after:
                raise runtime.AppError('Discord не завершил список серверов.')
            after = next_after
    finally:
        api.close()


def change_destination(root, guild, channel, chapter=None, *, scheduler=None,
                       verify=verify_destination, wait=wait_for_shutdown, channel_guard=None):
    """Prepare/commit while stopped; restore config, target control and tasks on any error."""
    root = Path(root).resolve()
    if not str(guild).isdigit() or not str(channel).isdigit() or int(guild) <= 0 or int(channel) <= 0:
        raise ValueError('Выберите сервер и текстовый канал.')
    if chapter is not None and (type(chapter) is not int or chapter <= 0):
        raise ValueError('Введите положительный целый номер главы.')
    if channel_guard is None:
        from ongoing import channel_lock
        channel_guard = channel_lock
    scheduler = scheduler or Scheduler(root)
    with settings_lock(root):
        config_path = root / 'config.json'
        original = config_path.read_bytes()
        old = json.loads(original.decode('utf-8-sig'))
        new = dict(old, guild_id=str(guild), channel_id=str(channel))
        if task_names(old) == task_names(new):
            raise ValueError('Этот сервер и канал уже выбраны.')
        names = task_names(old)
        new_names = task_names(new)
        details = verify(root, new)  # before modifying anything
        new.update(details)
        snapshots = [scheduler.call('read', name) for name in names]
        if not all(snapshots):
            raise runtime.AppError('Не найдены обе задачи этой установки. Настройки не менялись.')
        if any(scheduler.call('read', name) is not None for name in new_names):
            raise runtime.AppError('Для нового канала уже есть задачи Windows. Настройки не менялись.')
        old_control, new_control = LocalControl(root, old), LocalControl(root, new)
        ensure_no_pending(old_control)
        ensure_no_pending(new_control)
        backup = root / 'updates' / ('destination_' + str(time.time_ns()))
        backup.mkdir(parents=True)
        atomic_bytes(backup / 'config.json', original)
        download_album.atomic_json(backup / 'tasks.json', dict(zip(names, snapshots)))
        target_original = new_control.path.read_bytes() if new_control.path.exists() else None
        if target_original is not None:
            atomic_bytes(backup / 'target_control.json', target_original)
        created, removed, committed = [], [], False
        rollback_complete = False
        maintenance = False
        try:
            old_control.maintenance(True)
            maintenance = True
            for name in names:
                scheduler.call('disable', name)
            wait(root, old)
            deadline = time.monotonic() + 10
            while (scheduler.call('read', names[0]) or {}).get('running'):
                if time.monotonic() >= deadline:
                    raise runtime.AppError('Планировщик ещё завершает обработчик. Смена отменена.')
                time.sleep(0.25)
            with channel_guard(old), channel_guard(new), runtime.exclusive(root):
                if config_path.read_bytes() != original:
                    raise runtime.AppError('Настройки изменились во время переключения. Повторите действие.')
                ensure_no_pending(old_control)
                ensure_no_pending(new_control)
                new_control.initialise()
                new_control.maintenance(False)
                new_control.pause(True)
                new_control.set_expected(chapter)
                for name, snapshot in zip(new_names, snapshots):
                    created.append(name)  # also cover ambiguous registration failures
                    scheduler.call('restore', name, xml=snapshot['xml'], enabled=snapshot['enabled'])
                download_album.atomic_json(config_path, new)
                for name in names:
                    removed.append(name)
                    scheduler.call('remove', name)
                committed = True
            # The UI stays open; Manual/Resume starts the listener on explicit user action.
        except Exception as failure:
            # No new publisher can send: its target control is paused and has no active job.
            errors = []
            for name in reversed(created):
                try:
                    scheduler.call('remove', name)
                except Exception as exc:
                    errors.append(str(exc))
            try:
                atomic_bytes(config_path, original)
                if target_original is None:
                    new_control.path.unlink(missing_ok=True)
                else:
                    atomic_bytes(new_control.path, target_original)
            except Exception as exc:
                errors.append(str(exc))
            for name, snapshot in zip(names, snapshots):
                try:
                    scheduler.call('restore', name, xml=snapshot['xml'], enabled=snapshot['enabled'])
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                # Do not restart a task against a configuration whose rollback is uncertain.
                for control in (old_control, new_control):
                    try:
                        control.maintenance(True)
                    except Exception:
                        pass
                raise runtime.AppError('Не удалось полностью восстановить настройку. Резервная копия: ' + str(backup)) from failure
            rollback_complete = True
            raise
        finally:
            if maintenance and (committed or rollback_complete):
                old_control.maintenance(False)
            if not committed and rollback_complete:
                for name, snapshot in zip(names[:1], snapshots[:1]):
                    if snapshot['running']:
                        scheduler.call('start', name)
                        if not snapshot['enabled']:
                            scheduler.call('disable', name)
        return dict(config=new, **details, backup=str(backup), paused=True)
