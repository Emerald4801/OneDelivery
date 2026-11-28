"""Window actions. Windows only starts the existing worker task, never a second publisher."""
import os
from pathlib import Path
import subprocess
import time

from publish_control import LocalControl


def set_expected(root, config, chapter):
    import publisher_runtime as runtime
    # Prevent changing the target between final verification and a Discord POST.
    with runtime.exclusive(root):
        import json
        current = json.loads((Path(root) / 'config.json').read_text(encoding='utf-8-sig'))
        if (str(current['guild_id']), str(current['channel_id'])) != (str(config['guild_id']), str(config['channel_id'])):
            raise runtime.AppError('Место публикации изменилось. Откройте окно состояния заново.')
        return LocalControl(root, config).set_expected(chapter)


def worker_alive(root, config):
    import json
    from worker_status import destination
    path = destination(root, config) / 'worker_status.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        return (state.get('running') is True and 0 <= time.time() - state.get('heartbeat_at', 0) < 45)
    except (OSError, ValueError, TypeError):
        return False


def wake_worker(root, mode='Manual'):
    if os.name != 'nt':
        raise RuntimeError('Запуск обработчика через Планировщик доступен только в Windows.')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(Path(root) / 'control_task.ps1'), '-Mode', mode],
                                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Не удалось дождаться запуска обработчика в Windows. Команда сохранена.') from None
    if result.returncode:
        raise RuntimeError(f'Планировщик Windows не запустил обработчик (код {result.returncode}). Команда сохранена.')


def request_manual(root, config):
    control = LocalControl(root, config)
    request = control.request_manual()
    try:
        wake_worker(root, 'Manual')
    except RuntimeError as exc:
        current = control.snapshot().get('manual') or {}
        if current.get('id') == request['id'] and current.get('status') in ('running', 'succeeded'):
            return current  # The live worker already accepted it despite a scheduler error.
        if current.get('id') == request['id'] and current.get('status') == 'queued':
            control.update_manual(request['id'], status='failed', finished_at=time.time(), message=str(exc))
        raise
    return request


def set_paused(root, config, paused):
    control = LocalControl(root, config)
    if not paused:
        # Ensure logon startup is enabled; the persisted pause still gates the worker.
        wake_worker(root, 'Resume')
    return control.pause(paused)


def cancel_manual(root, config):
    import publisher_runtime as runtime
    with runtime.exclusive(Path(root)):
        return LocalControl(root, config).cancel_manual()


def open_log(root):
    path = Path(root) / 'logs' / 'ongoing.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    if os.name == 'nt':
        subprocess.Popen(['notepad.exe', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('pause', 'resume', 'manual'))
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    cfg = json.loads((args.root / 'config.json').read_text(encoding='utf-8'))
    if args.action == 'manual':
        request_manual(args.root, cfg)
    else:
        set_paused(args.root, cfg, args.action == 'pause')
    print('Команда сохранена. Результат — в окне состояния.')
