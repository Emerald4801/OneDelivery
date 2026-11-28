"""Status window, local commands and read-only destination discovery."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time

import app_icon
import control_client
from publish_control import LocalControl, ControlError

from publication_state import PublicationIndex, RECEIPT_NAME
from worker_status import destination, safe_error
from urllib.parse import urlparse

MOSCOW = timezone(timedelta(hours=3))


def date_text(value):
    if not value:
        return '—'
    return datetime.fromtimestamp(value, MOSCOW).strftime('%d.%m.%Y %H:%M')


def publication_text(item):
    if not item:
        return 'Пока нет сведений'
    if not item.get('completed_at'):
        return f"Глава {item['chapter']} · время не записано"
    origin = 'автоматически' if item['publication_origin'] == 'automatic' else 'вручную'
    return f"Глава {item['chapter']} · {date_text(item['completed_at'])} · {origin}"


def friendly_error(text):
    text = safe_error(text)
    for original, translated in (
            ('Send Messages in Threads', 'отправка сообщений в ветках'),
            ('Create Public Threads', 'создание публичных веток'),
            ('Read Message History', 'чтение истории сообщений'),
            ('Send Messages', 'отправка сообщений'), ('Attach Files', 'прикрепление файлов'),
            ('View Channel', 'просмотр канала')):
        text = text.replace(original, translated)
    return text


def next_text(when, now):
    if when is None:
        return '—'
    if when <= now:
        return 'Сейчас'
    seconds = int(when - now + 0.999)
    if seconds < 60:
        left = f'{seconds} с'
    elif seconds < 3600:
        left = f'{seconds // 60} мин {seconds % 60} с'
    elif seconds < 86400:
        left = f'{seconds // 3600} ч {(seconds % 3600) // 60} мин'
    else:
        left = f'{seconds // 86400} дн {(seconds % 86400) // 3600} ч'
    return f'{date_text(when)} МСК · через {left}'


def manual_text(job, recovery=None):
    if not job:
        return 'Не запускалась'
    if job.get('cancelled'):
        return 'Ручная проверка отменена. Прогресс отправки сохранён.'
    if recovery and recovery.get('status') == 'retry' and recovery.get('manual_id') == job.get('id'):
        return 'Ожидает повторной попытки' + (f" · глава {job['chapter']}" if job.get('chapter') else '')
    if job['status'] == 'queued':
        return 'Принята, ожидает обработчика.'
    if job['status'] == 'running':
        return 'Выполняется' + (f" · глава {job['chapter']}" if job.get('chapter') else '')
    when = date_text(job.get('finished_at'))
    if job['status'] == 'failed':
        return f"Предыдущая попытка ({when}) завершилась с ошибкой: {friendly_error(job.get('message') or 'Причина не записана.')}"
    return f"Завершена {when}. {job.get('message') or ''}".strip()


def transfer_text(value):
    if not isinstance(value, dict):
        return 'Нет активной отправки'
    sent, total = value.get('confirmed'), value.get('total')
    if type(sent) is not int or type(total) is not int or not 0 <= sent <= total or total <= 0:
        return 'Сверка отправленных страниц' if value.get('stage') == 'checking' else 'Нет сведений о страницах'
    chapter = value.get('chapter', '—')
    stage = value.get('stage')
    if stage == 'complete':
        return f'Глава {chapter}: все {total} страниц отправлены, завершение подтверждено.'
    if stage == 'finalizing':
        return f'Глава {chapter}: подтверждено {sent} из {total}. Ожидается подтверждение завершения.'
    if stage == 'checking':
        return f'Глава {chapter}: сверка истории Discord. Ранее подтверждено {sent} из {total}.'
    prefix = 'Продолжение' if value.get('resuming') else 'Отправка'
    if stage == 'saved':
        prefix = 'Прогресс сохранён'
    return f'{prefix} · глава {chapter}: подтверждено {sent} из {total} страниц.'


def status_view(state, config, now=None):
    """Presentation only: never schedule a retry or acknowledge a command here."""
    now = time.time() if now is None else now
    control = state['control']
    recovery = control.get('recovery') or {}
    job = control.get('manual') or {}
    active = job.get('status') in ('queued', 'running')
    alive = bool(state.get('running') and state.get('fresh'))
    busy = alive and state.get('in_cycle')
    error = state.get('error')
    when = state.get('next_check')
    retry = alive and error and not state.get('action_required') and not busy and when is not None and when > now
    activity, phase = state['activity'], state['phase']
    action, style = 'Не требуется', 'Accent.TLabel'
    if not alive:
        action = ('Проверьте ошибку и запустите проверку снова.' if error else
                  'Нет работающего обработчика. Нажмите «Проверить сейчас» или возобновите автоматику.')
        if state.get('running'):
            action = 'Нет свежего статуса обработчика. Проверьте журнал и запустите проверку снова.'
        if error and state.get('action_required'):
            activity, style = 'Требуется вмешательство', 'Error.TLabel'
    elif state.get('action_required'):
        activity, style = 'Требуется вмешательство', 'Error.TLabel'
        action = 'Исправьте причину ошибки и нажмите «Проверить сейчас».'
    elif busy:
        transfer = state.get('transfer') or {}
        if transfer.get('stage') in ('preparing', 'checking', 'sending', 'finalizing'):
            activity = 'Продолжение отправки' if transfer.get('resuming') else 'Отправка главы'
        else:
            activity = 'Выполняется проверка'
        action = 'Проверка выполняется. Результат обновится автоматически.'
    elif retry:
        activity, style = ('Ожидание подключения' if recovery.get('stage') == 'startup' else 'Ожидание повторной проверки'), 'Warning.TLabel'
        phase = 'После ошибки назначена повторная попытка.'
        action = f'Повтор автоматически: {next_text(when, now)}. Для попытки сейчас нажмите «Проверить сейчас».'
        if control['paused']:
            action += ' Автоматика остаётся на паузе.'
    elif control['paused'] and not active:
        phase = 'Автоматические проверки приостановлены.'
        action = 'Доступна ручная проверка. Для автоматических проверок нажмите «Возобновить автоматику».'
    elif state.get('cooldown_until') and state['cooldown_until'] > now and not active:
        activity = 'Пауза после публикации'
        action = '72 часа считаются по календарному времени, включая выключение ПК. Ручная проверка доступна.'
    elif when is not None and when > now and not active:
        activity = 'Ожидание следующей проверки'
    if error and not retry and not busy and not control['paused']:
        style = 'Error.TLabel'
    settings = control.get('settings') or {}
    settings_text = 'Ожидает применения обработчиком.' if settings.get('status') == 'queued' else 'Команды применены.'
    expected = (str(state.get('expected') or '—') + ' · вручную до публикации' if control.get('expected_override') else
                str(state.get('expected') or 'Расчёт при проверке') + ' · автоматически')
    return dict(activity=activity, activity_style=style, phase=phase, expected=expected,
        transfer=transfer_text(state.get('transfer')), publication=publication_text(state.get('last_publication')),
        vk=date_text(state.get('last_vk_check')),
        next=('Выполняется сейчас' if busy else 'Ожидает обработчика' if job.get('status') == 'queued' else next_text(when, now)),
        pause=state['cooldown_text'], server=state.get('guild_name') or config.get('guild_name') or str(config['guild_id']),
        channel=('#' + (state.get('channel_name') or config.get('channel_name'))) if
            (state.get('channel_name') or config.get('channel_name')) else str(config['channel_id']),
        manual=manual_text(job, recovery), settings=settings_text,
        error=('Предыдущая попытка: ' if busy and error else '') + friendly_error(error) if error else 'Нет',
        error_style='Muted.TLabel' if busy else 'Error.TLabel' if error else 'TLabel', action=action)


def saved_transfer(directory, owner, bot_id):
    pending = []
    for path in directory.glob(owner + '_*.json'):
        if not RECEIPT_NAME.fullmatch(path.name):
            continue
        item = json.loads(path.read_text(encoding='utf-8'))
        if (item.get('complete') or not (item.get('card_id') or item.get('pending_card'))
                or (bot_id is not None and str(item.get('bot_id')) != str(bot_id))):
            continue
        plan = item.get('group_plan') or []
        if not plan:
            continue
        try:
            confirmed, total = 0, plan[-1][1]
            if type(total) is not int or total <= 0:
                raise ValueError()
            for index, pair in enumerate(plan, 1):
                if str(index) not in (item.get('groups') or {}):
                    break
                confirmed = pair[1]
                if type(confirmed) is not int or not 0 <= confirmed <= total:
                    raise ValueError()
        except (IndexError, KeyError, TypeError, ValueError):
            raise ValueError('Не удалось прочитать сохранённый прогресс отправки.') from None
        pending.append(dict(chapter=item.get('chapter'), confirmed=confirmed, total=total, resuming=True, stage='saved'))
    return pending[0] if len(pending) == 1 else None


def tooltip(item):
    if not item:
        return 'VK → Discord · публикаций пока нет'
    if not item.get('completed_at'):
        return f"VK → Discord · глава {item['chapter']}"[:120]
    when = datetime.fromtimestamp(item['completed_at'], MOSCOW).strftime('%d.%m %H:%M')
    return f"VK → Discord · глава {item['chapter']} · {when}"[:120]


def viewer_key(root):
    return hashlib.sha256(str(root).casefold().encode()).hexdigest()[:24]


def identify_taskbar(root):
    """Identify this UI before Tk creates any window, instead of grouping as Python.

    Microsoft requires SetCurrentProcessExplicitAppUserModelID before presenting UI:
    https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-setcurrentprocessexplicitappusermodelid
    """
    if sys.platform != 'win32':
        return
    import ctypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    setter = shell.SetCurrentProcessExplicitAppUserModelID
    setter.argtypes = [ctypes.c_wchar_p]
    setter.restype = ctypes.c_int32  # HRESULT is 32 bits on both 32-/64-bit Windows.
    app_id = 'Yusif.VKDiscordPublisher.Status.' + viewer_key(root)
    result = setter(app_id)
    if result != 0:
        raise OSError(f'Windows не назначил идентификатор приложения (0x{result & 0xffffffff:08X}).')


def close_viewer(root):
    """Installer-only UI shutdown. Does not signal or stop the publisher."""
    if sys.platform != 'win32':
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    key = viewer_key(root)
    event = kernel.OpenEventW(2, False, 'Local\\VKDiscordStatusQuit_' + key)
    if not event:
        return
    kernel.SetEvent(event)
    kernel.CloseHandle(event)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        handle = kernel.OpenMutexW(0x100000, False, 'Local\\VKDiscordStatus_' + key)
        if not handle:
            return
        kernel.CloseHandle(handle)
        time.sleep(0.1)
    raise OSError('Закройте прежнее окно и значок в трее перед обновлением.')


class StatusReader:
    def __init__(self, root, config):
        self.directory = destination(root, config)
        self.config = config
        self.publications = PublicationIndex(self.directory)
        self.control = LocalControl(root, config)
        path = urlparse(config.get('vk_albums_url', 'https://vk.ru/albums-334424')).path
        match = re.fullmatch(r'/albums(-?\d+)', path)
        if not match:
            raise ValueError('Некорректный адрес списка альбомов.')
        self.owner = match[1]

    def read(self, now=None):
        now = time.time() if now is None else now
        path = self.directory / 'worker_status.json'
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if state and (state.get('schema') != 1
                      or state.get('guild_id') != str(self.config['guild_id'])
                      or state.get('channel_id') != str(self.config['channel_id'])):
            raise ValueError('Статус относится к другой настройке.')
        if not state:
            previous = self.directory / ('watch_' + self.owner + '.json')
            if previous.exists():
                old = json.loads(previous.read_text(encoding='utf-8'))
                scope = old.get('scope', [])
                if len(scope) == 4 and scope[:3] == [self.owner, str(self.config['guild_id']), str(self.config['channel_id'])]:
                    state.update(expected=old.get('expected'), expected_selection=old.get('expected_selection'),
                                 last_vk_check=old.get('last_vk_check'), bot_id=scope[3])
        summary = self.publications.summary(owner=self.owner, bot_id=state.get('bot_id'))
        state.update(summary)
        fresh = 0 <= now - state.get('heartbeat_at', 0) < 45
        if state.get('running') and fresh:
            state['activity'] = 'Автоматика работает'
        elif state.get('running'):
            state['activity'] = 'Нет свежего статуса обработчика'
            state['phase'] = 'Обработчик остановлен или недоступен.'
            state['next_check'] = None
        else:
            state['activity'] = 'Автоматика остановлена'
            state['phase'] = state.get('phase') or 'Нет запущенного обработчика с этой версией статуса.'
            state['next_check'] = None
        control = self.control.snapshot()
        state['control'] = control
        selection = control.get('expected_override')
        if selection:
            state['expected'] = selection['chapter']
        elif state.get('expected_selection'):
            state['expected'] = None  # automatic calculation is due after a selection reset
        state['auto_paused'] = control['paused']
        job = control.get('manual') or {}
        if job.get('status') in ('queued', 'running') and fresh and state.get('running'):
            state['activity'] = 'Ручная проверка · автоматика на паузе' if control['paused'] else 'Ручная проверка'
            if job['status'] == 'queued':
                state['next_check'] = 0
        elif control['paused']:
            state['next_check'] = None
            if fresh and state.get('running'):
                settings = control.get('settings') or {}
                state['activity'] = 'Пауза запрошена' if settings.get('status') == 'queued' else 'Автоматика на паузе'
        deadline = summary['cooldown_until']
        state['cooldown_text'] = date_text(deadline) if deadline and now < deadline else 'Нет'
        state['fresh'] = fresh
        recovery = control.get('recovery') or {}
        if recovery:
            state['error'] = recovery['message']
            state['action_required'] = recovery['status'] == 'blocked'
            if recovery['status'] == 'blocked':
                state['next_check'] = None
            elif not state.get('in_cycle'):
                eligible = (recovery.get('stage') == 'startup' or not control['paused'] or
                    (recovery.get('manual_id') == job.get('id') and job.get('status') in ('queued', 'running')))
                state['next_check'] = recovery['retry_at'] if fresh and state.get('running') and eligible else None
        if not state.get('in_cycle') or not state.get('transfer'):
            pending = saved_transfer(self.directory, self.owner, state.get('bot_id'))
            if pending:
                state['transfer'] = pending
        return state


@contextmanager
def viewer_instance(root):
    """A second shortcut invocation opens the existing window, without a second tray."""
    if sys.platform != 'win32':
        yield True, lambda: False
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    key = viewer_key(root)
    event = kernel.CreateEventW(None, False, False, 'Local\\VKDiscordStatusOpen_' + key)
    if not event:
        raise OSError('Не удалось создать сигнал окна состояния.')
    quit_event = kernel.CreateEventW(None, False, False, 'Local\\VKDiscordStatusQuit_' + key)
    if not quit_event:
        kernel.CloseHandle(event)
        raise OSError('Не удалось создать сигнал закрытия окна.')
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, 'Local\\VKDiscordStatus_' + key)
    duplicate = ctypes.get_last_error() == 183
    if not handle:
        kernel.CloseHandle(event)
        kernel.CloseHandle(quit_event)
        raise OSError('Не удалось открыть окно состояния.')
    try:
        if duplicate:
            kernel.SetEvent(event)
        def poll():
            if kernel.WaitForSingleObject(quit_event, 0) == 0:
                return 'quit'
            return 'show' if kernel.WaitForSingleObject(event, 0) == 0 else None
        yield not duplicate, poll
    finally:
        kernel.CloseHandle(handle)
        kernel.CloseHandle(event)
        kernel.CloseHandle(quit_event)


def icon_image():
    return app_icon.default_icon()


class StatusWindow:
    def __init__(self, root, config, tray=False, poll_open=lambda: False):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.reader = StatusReader(root, config)
        self.root = Path(root)
        self.config, self.poll_open = config, poll_open
        self.command_busy = False
        self.setting_notice = None
        self.image_busy = False
        self.current_image = app_icon.load_icon(root)
        self.events = queue.SimpleQueue()
        self.icon = None
        self.last_title = None
        self.tray_ready = False
        identify_taskbar(self.root)
        self.window = tk.Tk()
        self.window.withdraw()
        self.window.title('OneDelivery — состояние')
        self.window.minsize(690, 480)
        self.window.resizable(True, False)
        style = ttk.Style(self.window)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Heading.TLabel', font=('Segoe UI', 18, 'bold'))
        style.configure('Accent.TLabel', font=('Segoe UI', 10, 'bold'), foreground='#2563B8')
        style.configure('Muted.TLabel', foreground='#596273', font=('Segoe UI', 9))
        style.configure('Error.TLabel', foreground='#AF2430')
        style.configure('SaveError.TLabel', font=('Segoe UI', 10, 'bold'), foreground='#AF2430')
        style.configure('Warning.TLabel', foreground='#9A5A00', font=('Segoe UI', 10, 'bold'))
        style.configure('Success.TLabel', foreground='#176B3A')
        body = ttk.Frame(self.window, padding=20)
        body.grid(sticky='nsew')
        self.window.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, columnspan=2, sticky='w')
        self.icon_preview = ttk.Label(header)
        self.icon_preview.grid(row=0, column=0, padx=(0, 12))
        ttk.Label(header, text='OneDelivery', style='Heading.TLabel').grid(row=0, column=1, sticky='w')
        self.apply_icon(self.current_image)
        self.activity = tk.StringVar(value='Чтение состояния…')
        self.activity_label = ttk.Label(body, textvariable=self.activity, style='Accent.TLabel')
        self.activity_label.grid(row=1, column=0, columnspan=2, sticky='w', pady=(5, 12))
        fields = [('phase', 'Сейчас'), ('transfer', 'Отправка'), ('expected', 'Ожидаемая глава'), ('publication', 'Последняя публикация'),
                  ('vk', 'Последняя проверка VK'), ('next', 'Следующая проверка'), ('pause', 'Пауза после публикации'),
                  ('server', 'Сервер'), ('channel', 'Канал'), ('manual', 'Ручная проверка'),
                  ('settings', 'Управление'), ('error', 'Текущая ошибка'), ('action', 'Что делать')]
        self.values = {}
        self.value_labels = {}
        for row, (key, label) in enumerate(fields, 2):
            ttk.Label(body, text=label, style='Muted.TLabel').grid(row=row, column=0, sticky='nw', padx=(0, 20), pady=3)
            value = self.values[key] = tk.StringVar(value='—')
            label = ttk.Label(body, textvariable=value, wraplength=430, justify='left')
            label.grid(row=row, column=1, sticky='nw', pady=3)
            self.value_labels[key] = label
            if key == 'error':
                self.error_label = label
        footer = 2 + len(fields)
        ttk.Separator(body).grid(row=footer, column=0, columnspan=2, sticky='ew', pady=(12, 8))
        buttons = ttk.Frame(body)
        buttons.grid(row=footer + 1, column=0, columnspan=2, sticky='ew')
        self.manual_button = ttk.Button(buttons, text='Проверить сейчас', command=lambda: self.command('manual'))
        self.manual_button.grid(row=0, column=0, padx=(0, 8))
        self.pause_button = ttk.Button(buttons, text='Приостановить автоматику', command=self.toggle_pause)
        self.pause_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text='Открыть журнал', command=self.show_log).grid(row=0, column=2)
        self.cancel_manual_button = ttk.Button(buttons, text='Отменить ручную проверку', command=lambda: self.command('cancel'))
        self.cancel_manual_button.grid(row=1, column=0, columnspan=3, sticky='w', pady=(8, 0))
        self.cancel_manual_button.grid_remove()
        icons = ttk.Frame(body)
        settings_buttons = ttk.Frame(body)
        settings_buttons.grid(row=footer + 2, column=0, columnspan=2, sticky='w', pady=(10, 0))
        self.destination_button = ttk.Button(settings_buttons, text='Сменить сервер / канал…', command=self.choose_destination)
        self.destination_button.grid(row=0, column=0, padx=(0, 8))
        self.expected_button = ttk.Button(settings_buttons, text='Выбрать главу…', command=self.choose_expected)
        self.expected_button.grid(row=0, column=1, padx=(0, 8))
        self.auto_expected_button = ttk.Button(settings_buttons, text='Вернуть автоматический выбор',
                                              command=lambda: self.change_expected(None))
        self.auto_expected_button.grid(row=0, column=2)
        icons.grid(row=footer + 3, column=0, columnspan=2, sticky='w', pady=(10, 10))
        ttk.Label(icons, text='Значок приложения', style='Muted.TLabel').grid(row=0, column=0, padx=(0, 12))
        self.choose_icon_button = ttk.Button(icons, text='Выбрать картинку…', command=self.choose_icon)
        self.choose_icon_button.grid(row=0, column=1, padx=(0, 8))
        self.reset_icon_button = ttk.Button(icons, text='Сбросить', command=lambda: self.change_icon(None))
        self.reset_icon_button.grid(row=0, column=2)
        ttk.Label(body, text='Время по Москве. Закрытие окна не останавливает обработчик.',
                  style='Muted.TLabel', wraplength=570).grid(row=footer + 4, column=0, columnspan=2, sticky='w')
        self.hide_button = ttk.Button(body, text='Свернуть в трей', command=self.hide)
        self.hide_button.grid(row=footer + 5, column=0, columnspan=2, sticky='e', pady=(10, 0))
        self.window.protocol('WM_DELETE_WINDOW', self.hide)
        self.start_tray()
        self.tick()
        self.window.update_idletasks()
        self.window.minsize(max(690, self.window.winfo_reqwidth()), max(480, self.window.winfo_reqheight()))
        if not tray:
            self.show()
        else:
            # Never leave a hidden viewer if its tray could not start.
            self.window.after(3000, self.check_tray)

    def start_tray(self):
        def run():
            try:
                import pystray
                self.icon = pystray.Icon('VKDiscordPublisherStatus', self.current_image.copy(), tooltip(None), menu=pystray.Menu(
                    pystray.MenuItem('Открыть состояние', lambda *_: self.events.put('show'), default=True),
                    pystray.MenuItem('Закрыть окно и трей', lambda *_: self.events.put('quit'))))
                def ready(icon):
                    icon.visible = True
                    self.events.put('tray_ready')
                self.icon.run(setup=ready)
            except Exception as exc:
                self.events.put(('tray_error', type(exc).__name__))
        threading.Thread(target=run, name='status-tray', daemon=True).start()

    def apply_icon(self, image):
        from PIL import ImageTk, Image
        self.current_image = image.copy()
        self.tk_icon = ImageTk.PhotoImage(image, master=self.window)
        self.window.iconphoto(True, self.tk_icon)
        preview = image.copy()
        preview.thumbnail((40, 40), Image.Resampling.LANCZOS)
        self.preview_image = ImageTk.PhotoImage(preview, master=self.window)
        self.icon_preview.configure(image=self.preview_image)
        if self.icon and self.tray_ready:
            self.icon.icon = image.copy()

    def choose_icon(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.window, title='Значок приложения',
                                         filetypes=[('Картинки', '*.png *.jpg *.jpeg *.webp *.ico'), ('Все файлы', '*.*')])
        if path:
            self.change_icon(path)

    def change_icon(self, path):
        if self.image_busy:
            return
        self.image_busy = True
        self.choose_icon_button.configure(state='disabled')
        self.reset_icon_button.configure(state='disabled')
        def run():
            try:
                image = app_icon.save_icon(path, self.root) if path else app_icon.reset_icon(self.root)
                self.events.put(('icon_changed', image))
            except Exception as exc:
                text = str(exc) if isinstance(exc, app_icon.IconError) else 'Не удалось сохранить значок.'
                self.events.put(('icon_error', text))
        threading.Thread(target=run, name='change-icon', daemon=True).start()

    def toggle_pause(self):
        try:
            paused = LocalControl(self.root, self.config).snapshot()['paused']
            self.command('resume' if paused else 'pause')
        except (ControlError, OSError) as exc:
            self.show_error(str(exc))

    def run_setting(self, task, done=lambda result: None, failed=None):
        if self.command_busy:
            return
        self.command_busy = True
        def run():
            try:
                self.events.put(('setting_done', (done, task())))
            except Exception as exc:
                self.events.put(('setting_failed', (failed, safe_error(exc))))
        threading.Thread(target=run, name='destination-setting', daemon=True).start()

    def choose_expected(self):
        from tkinter import simpledialog
        selection = LocalControl(self.root, self.config).snapshot().get('expected_override')
        number = simpledialog.askinteger('Ожидаемая глава',
            'Искать главу с этим номером до её успешной публикации.\n'
            'После публикации выбор снова станет автоматическим.', parent=self.window,
            initialvalue=(selection or {}).get('chapter') or self.reader.read().get('expected'), minvalue=1)
        if number is not None:
            self.change_expected(number)

    def change_expected(self, number):
        self.run_setting(lambda: control_client.set_expected(self.root, self.config, number))

    def choose_destination(self):
        import destination_settings
        tk, ttk = self.tk, self.ttk
        dialog = tk.Toplevel(self.window)
        dialog.title('Место публикации')
        dialog.transient(self.window)
        dialog.resizable(False, False)
        dialog.protocol('WM_DELETE_WINDOW', lambda: None if self.command_busy else dialog.destroy())
        body = ttk.Frame(dialog, padding=20)
        body.grid(sticky='nsew')
        ttk.Label(body, text='Сервер').grid(row=0, column=0, sticky='w')
        servers = ttk.Combobox(body, state='disabled', width=48)
        servers.grid(row=1, column=0, sticky='ew', pady=(4, 12))
        ttk.Label(body, text='Канал для публикации').grid(row=2, column=0, sticky='w')
        channels = ttk.Combobox(body, state='disabled', width=48)
        channels.grid(row=3, column=0, sticky='ew', pady=(4, 12))
        ttk.Label(body, text='Первая ожидаемая глава (необязательно)').grid(row=4, column=0, sticky='w')
        chapter = ttk.Entry(body, width=14)
        chapter.grid(row=5, column=0, sticky='w', pady=(4, 12))
        ttk.Label(body, text='Пустое поле — автоматический выбор по Discord. В пустом канале\n'
            'ожидается следующая глава после последней доступной в VK.\n'
            'После смены автоматика будет на паузе. Проверка ничего не отправляет.',
            style='Muted.TLabel').grid(row=6, column=0, sticky='w')
        message = tk.StringVar(value='Загрузка серверов бота…')
        message_label = ttk.Label(body, textvariable=message, wraplength=430, justify='left')
        message_label.grid(row=7, column=0, sticky='w', pady=12)
        def set_message(text, failed=False):
            message.set(text)
            message_label.configure(style='SaveError.TLabel' if failed else 'TLabel')
        choices = {'guilds': [], 'channels': []}
        def labels(items, prefix=''):
            return [prefix + item['name'] + (' · ' + item['id'][-6:] if
                sum(other['name'] == item['name'] for other in items) > 1 else '') +
                (' · объявления' if item.get('type') == 5 else '') for item in items]
        def error(text):
            set_message(friendly_error(text), True)
            servers.configure(state='readonly' if choices['guilds'] else 'disabled')
            apply.configure(state='normal' if channels.current() >= 0 else 'disabled')
        def loaded_channels(items):
            choices['channels'] = items
            servers.configure(state='readonly')
            channels.configure(values=labels(items, '#'), state='readonly')
            if items:
                channels.current(next((i for i, item in enumerate(items)
                                       if item['id'] == str(self.config['channel_id'])), 0))
            set_message('Выберите канал.' if items else 'На этом сервере нет текстовых каналов или каналов объявлений.')
            apply.configure(state='normal' if items else 'disabled', text='Сохранить место публикации')
        def select_server(*_):
            if self.command_busy:
                return
            apply.configure(state='disabled')
            servers.configure(state='disabled')
            channels.set('')
            channels.configure(state='disabled')
            set_message('Загрузка каналов…')
            guild = choices['guilds'][servers.current()]['id']
            self.run_setting(lambda: destination_settings.list_destinations(self.root, guild), loaded_channels, error)
        def loaded_servers(items):
            choices['guilds'] = items
            servers.configure(values=labels(items), state='readonly')
            if items:
                servers.current(next((i for i, item in enumerate(items)
                                      if item['id'] == str(self.config['guild_id'])), 0))
                select_server()
            else:
                set_message('Бот пока не добавлен ни на один сервер.')
        def saved(result):
            self.config = result['config']
            self.reader = StatusReader(self.root, self.config)
            self.last_title = None
            self.setting_notice = ('Место публикации сохранено. Автоматика на паузе.', 'Success.TLabel')
            dialog.destroy()
        def save_error(text):
            servers.configure(state='readonly')
            channels.configure(state='readonly')
            error('Настройки не сохранены.\n' + text)
            apply.configure(text='Повторить сохранение')
            self.setting_notice = ('Место публикации не сохранено. Причина показана в окне настройки.', 'Error.TLabel')
        def save():
            value = chapter.get().strip()
            if value and (not value.isascii() or not value.isdigit() or int(value) <= 0):
                save_error('Введите положительный целый номер главы или оставьте поле пустым.')
                return
            guild = choices['guilds'][servers.current()]['id']
            channel = choices['channels'][channels.current()]['id']
            apply.configure(state='disabled')
            servers.configure(state='disabled')
            channels.configure(state='disabled')
            self.setting_notice = None
            set_message('Проверка доступа и безопасная смена назначения…')
            self.run_setting(lambda: destination_settings.change_destination(
                self.root, guild, channel, int(value) if value else None), saved, save_error)
        apply = ttk.Button(body, text='Сохранить место публикации', command=save, state='disabled')
        apply.grid(row=8, column=0, sticky='e')
        servers.bind('<<ComboboxSelected>>', select_server)
        dialog.grab_set()
        self.run_setting(lambda: destination_settings.list_destinations(self.root), loaded_servers, error)

    def command(self, action):
        if self.command_busy:
            return
        self.command_busy = True
        self.manual_button.configure(state='disabled')
        self.pause_button.configure(state='disabled')
        def run():
            try:
                if action == 'manual':
                    control_client.request_manual(self.root, self.config)
                elif action == 'cancel':
                    control_client.cancel_manual(self.root, self.config)
                else:
                    control_client.set_paused(self.root, self.config, action == 'pause')
                self.events.put(('command_done', None))
            except Exception as exc:
                self.events.put(('command_error', safe_error(exc)))
        threading.Thread(target=run, name='local-command', daemon=True).start()

    def show_error(self, message):
        from tkinter import messagebox
        messagebox.showerror('VK → Discord', message, parent=self.window)

    def show_log(self):
        try:
            control_client.open_log(self.root)
        except OSError:
            self.show_error('Не удалось открыть журнал работы.')

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def hide(self):
        if self.tray_ready:
            self.window.withdraw()
        else:
            self.quit()

    def quit(self):
        if self.icon:
            self.icon.stop()
        self.window.destroy()

    def check_tray(self):
        if not self.tray_ready:
            self.show()

    def tick(self):
        command = self.poll_open()
        if command == 'quit':
            self.quit()
            return
        if command == 'show':
            self.show()
        while not self.events.empty():
            event = self.events.get()
            if event == 'quit':
                self.quit()
                return
            if event == 'show':
                self.show()
            elif event == 'tray_ready':
                self.tray_ready = True
                self.apply_icon(self.current_image)
            elif isinstance(event, tuple) and event[0] == 'tray_error':
                self.tray_ready = False
                self.hide_button.configure(text='Закрыть окно')
                self.show()
            elif isinstance(event, tuple) and event[0] in ('command_done', 'command_error'):
                self.command_busy = False
                if event[0] == 'command_error':
                    self.show_error(event[1])
            elif isinstance(event, tuple) and event[0] in ('setting_done', 'setting_failed'):
                self.command_busy = False
                callback, value = event[1]
                if event[0] == 'setting_done':
                    callback(value)
                elif callback:
                    callback(value)
                else:
                    self.show_error(value)
            elif isinstance(event, tuple) and event[0] in ('icon_changed', 'icon_error'):
                self.image_busy = False
                self.choose_icon_button.configure(state='normal')
                self.reset_icon_button.configure(state='normal')
                if event[0] == 'icon_changed':
                    self.apply_icon(event[1])
                else:
                    self.show_error(event[1])
        try:
            state = self.reader.read()
            control = state['control']
            job = control.get('manual') or {}
            job_active = job.get('status') in ('queued', 'running')
            recovery = control.get('recovery') or {}
            manual_waiting = (job_active and recovery.get('status') == 'retry'
                              and recovery.get('manual_id') == job.get('id') and not state.get('in_cycle'))
            for button in (self.destination_button, self.expected_button):
                button.configure(state='disabled' if self.command_busy or job_active else 'normal')
            self.auto_expected_button.configure(state='disabled' if self.command_busy or job_active or
                                                not control.get('expected_override') else 'normal')
            self.manual_button.configure(state='disabled' if self.command_busy or (job_active and not manual_waiting) else 'normal',
                                         text='Повторить сейчас' if manual_waiting else 'Проверить сейчас')
            if manual_waiting:
                self.cancel_manual_button.grid()
                self.cancel_manual_button.configure(state='disabled' if self.command_busy else 'normal')
            else:
                self.cancel_manual_button.grid_remove()
            self.pause_button.configure(state='disabled' if self.command_busy else 'normal',
                                        text='Возобновить автоматику' if control['paused'] else 'Приостановить автоматику')
            mapping = status_view(state, self.config)
            self.activity.set(mapping.pop('activity'))
            self.activity_label.configure(style=mapping.pop('activity_style'))
            self.error_label.configure(style=mapping.pop('error_style'))
            self.value_labels['manual'].configure(style='Muted.TLabel' if job.get('status') in ('failed', 'succeeded') else 'TLabel')
            notice = self.setting_notice
            if notice:
                mapping['settings'] = notice[0]
            self.value_labels['settings'].configure(style=notice[1] if notice else 'TLabel')
            for key, value in mapping.items():
                self.values[key].set(value)
            title = tooltip(state.get('last_publication'))
            if self.icon and self.tray_ready and title != self.last_title:
                self.icon.title = title
                self.last_title = title
        except (ControlError, OSError, ValueError, TypeError, KeyError) as exc:
            self.activity.set('Статус недоступен')
            self.activity_label.configure(style='Error.TLabel')
            self.error_label.configure(style='Error.TLabel')
            self.values['error'].set(safe_error(exc))
            self.values['action'].set('Проверьте доступность рабочей папки и файла состояния.')
        self.window.after(2000, self.tick)

    def run(self):
        self.window.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--tray', action='store_true')
    parser.add_argument('--close-viewer', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.close_viewer:
            close_viewer(root)
            return 0
        config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
        with viewer_instance(root) as (first, poll_open):
            if first:
                StatusWindow(root, config, args.tray, poll_open).run()
        return 0
    except Exception as exc:
        message = 'Не удалось открыть окно состояния: ' + safe_error(exc)
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'VK → Discord', 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
