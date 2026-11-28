"""Checked delta updater. Stops only the matched worker while its send lock is held."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
FILES = ('failure_policy.py', 'ongoing.py', 'publisher_controller.py', 'publish_control.py', 'control_client.py',
         'status_app.py', 'destination_settings.py', 'task_bridge.ps1', 'requirements-ui.lock',
         'publisher_runtime.py', 'publish_album_v3.py', 'vk_api.py', 'vk_source.py', 'download_album.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(root, package, backup, *, files=FILES):
    root, package, backup = Path(root), Path(package), Path(backup)
    manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
    if set(manifest['files']) != set(files):
        raise ValueError('Неподдерживаемый состав обновления.')
    records, pending = {}, False
    for name in files:
        spec = manifest['files'][name]
        updated = (package / 'payload' / name).read_bytes()
        if digest(updated) != spec['after']:
            raise ValueError('Повреждён файл обновления: ' + name)
        if name.endswith('.py'):
            compile(updated, name, 'exec')
        path = root / name
        original = path.read_bytes() if path.exists() else None
        before = digest(original) if original is not None else None
        if before not in (spec['before'], spec['after'], *spec.get('compatible_before', [])):
            raise ValueError('Установленный файл отличается от проверенной v9: ' + name)
        pending |= before != spec['after']
        records[name] = dict(before=before, after=spec['after'])
    if not pending:
        return False
    backup.mkdir(parents=True, exist_ok=False)
    (backup / 'original').mkdir()
    for name, record in records.items():
        if record['before'] is not None:
            atomic_write(backup / 'original' / name, (root / name).read_bytes())
    atomic_write(backup / 'manifest.json', json.dumps(records, indent=2).encode('utf-8'))
    return True


def restore_files(root, backup, *, files=FILES):
    records = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if set(records) != set(files):
        raise ValueError('Неподдерживаемая резервная копия.')
    originals = {}
    for name, record in records.items():
        data = (backup / 'original' / name).read_bytes() if record['before'] is not None else None
        if data is not None and digest(data) != record['before']:
            raise ValueError('Повреждена резервная копия: ' + name)
        originals[name] = data
    for name, data in originals.items():
        if data is None:
            (root / name).unlink(missing_ok=True)
        else:
            atomic_write(root / name, data)


def apply_files(root, package, backup, *, files=FILES):
    records = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if set(records) != set(files):
        raise ValueError('Неподдерживаемая резервная копия.')
    for name, record in records.items():
        path = root / name
        current = digest(path.read_bytes()) if path.exists() else None
        if current != record['before']:
            raise ValueError('Файл изменился после подготовки: ' + name)
        if digest((package / 'payload' / name).read_bytes()) != record['after']:
            raise ValueError('Подготовленный файл изменился: ' + name)
    try:
        for name in records:
            atomic_write(root / name, (package / 'payload' / name).read_bytes())
    except BaseException:
        restore_files(root, backup, files=files)
        raise


class Tasks:
    def __init__(self, root, package):
        self.root, self.package = root, package

    def call(self, operation, name, **values):
        process = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(self.package / 'payload' / 'task_bridge.ps1')],
            input=json.dumps(dict(operation=operation, name=name, root=str(self.root), **values)),
            capture_output=True, encoding='utf-8', timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            result = json.loads(process.stdout.lstrip('\ufeff'))
        except ValueError:
            raise RuntimeError('Планировщик Windows не ответил. Изменения остановлены.') from None
        if process.returncode or not result.get('ok'):
            raise RuntimeError('Планировщик: ' + result.get('error', 'неизвестная ошибка'))
        return result.get('result')


@contextmanager
def wait_lock(factory, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        context = factory()
        try:
            context.__enter__()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Обработчик занят. Обновление отменено до замены файлов.') from None
            time.sleep(0.25)
    try:
        yield
    finally:
        context.__exit__(None, None, None)


def install(root, package=HERE, restore=None, *, files=FILES):
    root, package = Path(root).resolve(), Path(package).resolve()
    if root == package or not (root / '.venv' / 'Scripts' / 'pythonw.exe').is_file():
        raise ValueError('Установленная OneDelivery v9 не найдена.')
    config_bytes = (root / 'config.json').read_bytes()
    config = json.loads(config_bytes.decode('utf-8-sig'))
    if not all(str(config.get(key, '')).isdigit() for key in ('guild_id', 'channel_id')):
        raise ValueError('Некорректное место публикации.')
    # Import only the known installed application. These imports do not contact services.
    sys.path.insert(0, str(root))
    runtime = importlib.import_module('publisher_runtime')
    ongoing = importlib.import_module('ongoing')
    control_module = importlib.import_module('publish_control')
    viewer = importlib.import_module('status_app')
    for module in (runtime, ongoing, control_module, viewer):
        if Path(module.__file__).resolve().parent != root:
            raise ValueError('Загружен модуль из другой установки.')
    tasks = Tasks(root, package)
    name = 'VKDiscordPublisher-' + str(config['guild_id']) + '-' + str(config['channel_id'])
    backup = Path(restore).resolve() if restore else root / 'updates' / ('settings_' + str(time.time_ns()))
    settings_guard = control_module.LocalControl(root, dict(guild_id='0', channel_id='0'))
    settings_guard.directory = root
    with settings_guard.locked():
        snapshot = tasks.call('read', name)
        if snapshot is None:
            raise ValueError('Задача этой установки не найдена.')
        if not restore and not prepare(root, package, backup, files=files):
            print('Это обновление уже установлено. Файлы и режим работы не менялись.')
            return
        if not restore:
            atomic_write(backup / 'task.json', json.dumps(snapshot, ensure_ascii=False, indent=2).encode('utf-8'))
        viewer_closed = False
        touched = False
        files_applied = False
        try:
            # Holding publisher.lock makes Stop-ScheduledTask safe: no send can be active or start.
            with wait_lock(lambda: runtime.exclusive(root)):
                touched = True
                tasks.call('disable', name)
                tasks.call('stop', name)
                with wait_lock(lambda: ongoing.channel_lock(config)):
                    if (root / 'config.json').read_bytes() != config_bytes:
                        raise ValueError('Настройка изменилась во время подготовки.')
                    viewer.close_viewer(root)
                    viewer_closed = True
                    if restore:
                        restore_files(root, backup, files=files)
                    else:
                        apply_files(root, package, backup, files=files)
                    files_applied = True
        except Exception as exc:
            raise RuntimeError(str(exc) + '\nРезервная копия: ' + str(backup)) from exc
        finally:
            recovery_errors = []
            try:
                if touched:
                    tasks.call('restore', name, xml=snapshot['xml'], enabled=snapshot['enabled'])
                    if snapshot['running']:
                        tasks.call('start', name)
                        if not snapshot['enabled']:
                            tasks.call('disable', name)
            except Exception as exc:
                recovery_errors.append('Не удалось восстановить режим обработчика: ' + str(exc))
            if viewer_closed:
                try:
                    subprocess.Popen([str(root / '.venv' / 'Scripts' / 'pythonw.exe'),
                        str(root / 'status_app.py')], cwd=root, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except OSError as exc:
                    recovery_errors.append('Не удалось открыть окно: ' + str(exc))
            if recovery_errors:
                result = ('Код успешно восстановлен.' if restore else 'Код успешно обновлён.') if files_applied else 'Обновление не завершено.'
                raise RuntimeError(result + '\n' + '\n'.join(recovery_errors) + '\nРезервная копия: ' + str(backup))
    print(('Прежний код восстановлен.' if restore else 'Обновление OneDelivery установлено.') + '\nРезервная копия: ' + str(backup))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'VKDiscordPublisher')
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    try:
        install(args.root, restore=args.restore)
        return 0
    except Exception as exc:
        print('Обновление остановлено: ' + str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
