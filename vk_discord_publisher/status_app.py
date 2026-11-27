"""Status window and local commands. Only the worker contacts VK/Discord."""
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

from publication_state import PublicationIndex
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
                    state.update(expected=old.get('expected'), last_vk_check=old.get('last_vk_check'), bot_id=scope[3])
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
        self.image_busy = False
        self.current_image = app_icon.load_icon(root)
        self.events = queue.SimpleQueue()
        self.icon = None
        self.last_title = None
        self.tray_ready = False
        identify_taskbar(self.root)
        self.window = tk.Tk()
        self.window.withdraw()
        self.window.title('VK → Discord — состояние')
        self.window.minsize(690, 480)
        self.window.resizable(True, False)
        style = ttk.Style(self.window)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Heading.TLabel', font=('Segoe UI', 18, 'bold'))
        style.configure('Accent.TLabel', font=('Segoe UI', 10, 'bold'), foreground='#2563B8')
        style.configure('Muted.TLabel', foreground='#596273', font=('Segoe UI', 9))
        style.configure('Error.TLabel', foreground='#AF2430')
        body = ttk.Frame(self.window, padding=24)
        body.grid(sticky='nsew')
        self.window.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, columnspan=2, sticky='w')
        self.icon_preview = ttk.Label(header)
        self.icon_preview.grid(row=0, column=0, padx=(0, 12))
        ttk.Label(header, text='VK → Discord', style='Heading.TLabel').grid(row=0, column=1, sticky='w')
        self.apply_icon(self.current_image)
        self.activity = tk.StringVar(value='Чтение состояния…')
        ttk.Label(body, textvariable=self.activity, style='Accent.TLabel').grid(row=1, column=0, columnspan=2, sticky='w', pady=(5, 18))
        fields = [('phase', 'Сейчас'), ('expected', 'Ожидаемая глава'), ('publication', 'Последняя публикация'),
                  ('vk', 'Последняя проверка VK'), ('next', 'Следующая проверка'), ('pause', 'Пауза после публикации'),
                  ('server', 'Сервер'), ('channel', 'Канал'), ('manual', 'Ручная проверка'),
                  ('settings', 'Управление'), ('error', 'Последняя ошибка'), ('action', 'Вмешательство')]
        self.values = {}
        for row, (key, label) in enumerate(fields, 2):
            ttk.Label(body, text=label, style='Muted.TLabel').grid(row=row, column=0, sticky='nw', padx=(0, 20), pady=5)
            value = self.values[key] = tk.StringVar(value='—')
            label = ttk.Label(body, textvariable=value, wraplength=430, justify='left')
            label.grid(row=row, column=1, sticky='nw', pady=5)
            if key == 'error':
                self.error_label = label
        ttk.Separator(body).grid(row=14, column=0, columnspan=2, sticky='ew', pady=(14, 10))
        buttons = ttk.Frame(body)
        buttons.grid(row=15, column=0, columnspan=2, sticky='ew')
        self.manual_button = ttk.Button(buttons, text='Проверить сейчас', command=lambda: self.command('manual'))
        self.manual_button.grid(row=0, column=0, padx=(0, 8))
        self.pause_button = ttk.Button(buttons, text='Приостановить автоматику', command=self.toggle_pause)
        self.pause_button.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text='Открыть журнал', command=self.show_log).grid(row=0, column=2)
        icons = ttk.Frame(body)
        icons.grid(row=16, column=0, columnspan=2, sticky='w', pady=(12, 12))
        ttk.Label(icons, text='Значок приложения', style='Muted.TLabel').grid(row=0, column=0, padx=(0, 12))
        self.choose_icon_button = ttk.Button(icons, text='Выбрать картинку…', command=self.choose_icon)
        self.choose_icon_button.grid(row=0, column=1, padx=(0, 8))
        self.reset_icon_button = ttk.Button(icons, text='Сбросить', command=lambda: self.change_icon(None))
        self.reset_icon_button.grid(row=0, column=2)
        ttk.Label(body, text='Время по Москве. Закрытие окна не останавливает обработчик.',
                  style='Muted.TLabel', wraplength=570).grid(row=17, column=0, columnspan=2, sticky='w')
        self.hide_button = ttk.Button(body, text='Свернуть в трей', command=self.hide)
        self.hide_button.grid(row=18, column=0, columnspan=2, sticky='e', pady=(14, 0))
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
            self.activity.set(state['activity'])
            control = state['control']
            job = control.get('manual') or {}
            job_active = job.get('status') in ('queued', 'running')
            self.manual_button.configure(state='disabled' if self.command_busy or job_active else 'normal')
            self.pause_button.configure(state='disabled' if self.command_busy else 'normal',
                                        text='Возобновить автоматику' if control['paused'] else 'Приостановить автоматику')
            job_text = {'queued': 'Принята, ожидает обработчика.', 'running': 'Выполняется.'}.get(job.get('status'), job.get('message') or 'Не запускалась')
            if job.get('status') == 'running' and job.get('chapter'):
                job_text = f"Глава {job['chapter']}: проверка готовности."
            if job.get('status') == 'failed':
                job_text = 'Ошибка: ' + job_text
            settings = control.get('settings') or {}
            settings_text = 'Ожидает применения обработчиком.' if settings.get('status') == 'queued' else 'Команды применены.'
            mapping = dict(phase=state['phase'], expected=state.get('expected') or '—',
                           publication=publication_text(state.get('last_publication')),
                           vk=date_text(state.get('last_vk_check')),
                           next='Ожидает обработчика' if job.get('status') == 'queued' else date_text(state.get('next_check')),
                           pause=state['cooldown_text'], server=state.get('guild_name') or str(self.config['guild_id']),
                           channel=('#' + state['channel_name']) if state.get('channel_name') else str(self.config['channel_id']),
                           manual=job_text, settings=settings_text, error=state.get('error') or 'Нет',
                           action=('Требуется: проверьте ошибку выше.' if state.get('action_required') else
                                   'Повтор запланирован. Если ошибка сохраняется — проверьте её причину.'
                                   if state.get('error') else 'Не требуется'))
            for key, value in mapping.items():
                self.values[key].set(value)
            self.error_label.configure(style='Error.TLabel' if state.get('error') else 'TLabel')
            title = tooltip(state.get('last_publication'))
            if self.icon and self.tray_ready and title != self.last_title:
                self.icon.title = title
                self.last_title = title
        except (ControlError, OSError, ValueError, TypeError, KeyError) as exc:
            self.activity.set('Статус недоступен')
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
