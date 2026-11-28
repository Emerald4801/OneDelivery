"""Explicit diagnostics. Reports use selected fields, never raw config, logs or tokens."""
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
import io
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time
import uuid
from contextlib import redirect_stdout

import app_version
from failure_policy import should_retry


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def chapter(value):
    return value if type(value) is int and value > 0 else None


def choice(value, allowed):
    return value if value in allowed else 'unknown'


def cause(text):
    text = str(text or '').lower()
    for words, label in (
            (('токен', 'ключ'), 'Ключ доступа не принят или недоступен'),
            (('прав', 'permission'), 'Недостаточно прав доступа'),
            (('сертификат', 'защищённое соединение'), 'Не подтверждено защищённое соединение'),
            (('поврежд', 'манифест', 'fingerprint'), 'Не подтверждена целостность данных'),
            (('несколько', 'выбран другой', 'изменился', 'изменил'), 'Конфликт данных или выбранной главы'),
            (('соединени', 'недоступен', 'ответ', 'http', 'частот', 'лимит'), 'Сбой связи или ответ сервиса')):
        if any(word in text for word in words):
            return label
    return 'Причина требует проверки в окне приложения' if text else None


def tasks(root, config):
    if os.name != 'nt':
        return dict(status='unavailable', items=[])
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(Path(root) / 'diagnostic_tasks.ps1')],
            input=json.dumps(dict(root=str(Path(root).resolve()), guild=str(config['guild_id']), channel=str(config['channel_id']))),
            capture_output=True, encoding='utf-8', errors='replace', timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(result.stdout.lstrip('\ufeff'))
        if result.returncode or data.get('ok') is not True or not isinstance(data.get('tasks'), list):
            raise ValueError()
        items = []
        for item in data['tasks']:
            items.append(dict(kind=choice(item.get('kind'), ('worker', 'window')), exists=item.get('exists') is True,
                matches=item.get('matches') is True, enabled=item.get('enabled') is True,
                state=choice(item.get('state'), ('Disabled', 'Queued', 'Ready', 'Running', 'Unknown')),
                last_result=number(item.get('last_result'))))
        if sorted(item['kind'] for item in items) != ['window', 'worker']:
            raise ValueError()
        return dict(status='ok', items=items)
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return dict(status='unavailable', items=[])


def access(directory):
    """Probe only an existing working directory; remove the probe immediately."""
    path = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='onedelivery_probe_', delete=False) as stream:
            path = Path(stream.name)
            stream.write(b'OneDelivery local access check')
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != b'OneDelivery local access check':
            raise OSError()
        path.unlink()
        return 'ok'
    except OSError:
        return 'unavailable'
    finally:
        if path:
            try: path.unlink(missing_ok=True)
            except OSError: pass


def exception_result(error):
    # Exception bodies may contain credentials or signed URLs. Never export str(error).
    result = dict(status='error', retryable=should_retry(error), message=cause(error))
    status = getattr(error, 'status', None)
    code = getattr(error, 'code', None)
    if type(status) is int:
        result['http_status'] = status
        if status in (401, 403): result['message'] = 'Проверьте ключ доступа и права'
    if type(code) is int: result['service_code'] = code
    if result['retryable']: result['message'] = 'Временный сбой связи или сервиса; можно повторить проверку'
    return result


def services(root, config):
    import publisher_runtime as runtime
    from ongoing import noninteractive_token
    from vk_source import VKSource
    result = {}
    api = source = None
    try:
        api = runtime.Discord(noninteractive_token(Path(root)))
        with redirect_stdout(io.StringIO()):
            runtime.check(api, config)  # GET only: never Pipeline.latest/recovery/publish
        result['discord'] = dict(status='ok', message='Подключение и основные права подтверждены')
    except Exception as error:
        result['discord'] = exception_result(error)
    finally:
        if api: api.close()
    try:
        source = VKSource(root, config)
        if source.api_client is not None:
            source.api_client.albums(source.owner, full=False)  # one API page, read-only method
        else:
            source.web_albums(full=False)
        result['vk'] = dict(status='ok', message='Источник альбомов доступен')
    except Exception as error:
        result['vk'] = exception_result(error)
    finally:
        if source: source.close()
    return result


def collect(root, config, read_status, *, network=False):
    root = Path(root)
    build = app_version.describe(root)
    # Only this projection is exportable. No unfiltered state/config/log attachment.
    report = dict(schema=1, app='OneDelivery', generated_at=time.time(),
        build={key: build[key] for key in ('status', 'source_commit')},
        runtime=dict(python=platform.python_version(), windows=os.name == 'nt'),
        local_access={'application': access(root)}, tasks=tasks(root, config),
        services=dict(status='not_requested'), events=[])
    for key in ('built_at', 'installed_at'):
        report['build'][key] = number(build.get(key))
    report['build']['modified_count'] = len(build['modified'])
    for package in ('requests', 'Pillow', 'gallery-dl', 'pystray'):
        try: report['runtime'][package] = version(package)
        except PackageNotFoundError: report['runtime'][package] = 'not_installed'
    try:
        state = read_status()
        control = state['control']
        recovery = control.get('recovery') or {}
        manual = control.get('manual') or {}
        transfer = state.get('transfer') or {}
        worker_build = state.get('build') or {}
        publication = state.get('last_publication') or {}
        report['publication'] = dict(chapter=chapter(publication.get('chapter')),
            completed_at=number(publication.get('completed_at')),
            origin=choice(publication.get('publication_origin'), ('automatic', 'manual')))
        report['worker'] = dict(running=state.get('running') is True, fresh=state.get('fresh') is True,
            busy=state.get('in_cycle') is True, paused=control.get('paused') is True,
            expected=chapter(state.get('expected')), next_check=number(state.get('next_check')),
            cooldown_until=number(state.get('cooldown_until')),
            build_matches=bool(build['status'] == 'verified' and worker_build.get('status') == 'verified'
                               and worker_build.get('source_commit') == build['source_commit']))
        report['operation'] = dict(manual=choice(manual.get('status'), ('queued', 'running', 'succeeded', 'failed')),
            cancelled=manual.get('cancelled') is True, chapter=chapter(manual.get('chapter')),
            recovery=choice(recovery.get('status'), ('retry', 'blocked')), retry_at=number(recovery.get('retry_at')),
            cause=cause(recovery.get('message') or state.get('error')),
            confirmed=number(transfer.get('confirmed')), total=number(transfer.get('total')))
        for event, value in (('worker_started', state.get('started_at')), ('last_vk_check', state.get('last_vk_check')),
                ('manual_requested', manual.get('requested_at')), ('manual_finished', manual.get('finished_at')),
                ('failure', recovery.get('failed_at')), ('publication_completed', (state.get('last_publication') or {}).get('completed_at'))):
            if number(value) is not None: report['events'].append(dict(event=event, at=number(value)))
        report['events'].sort(key=lambda item: item['at'])
        report['local_access']['state'] = access(root / 'state' / str(config['guild_id']) / str(config['channel_id']))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        report['worker'] = dict(status='unavailable')
    if network:
        report['services'] = services(root, config)
    return report


def save(root, report):
    directory = Path(root) / 'diagnostics'
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    path = directory / ('OneDelivery_' + stamp + '_' + uuid.uuid4().hex[:6] + '.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return path


def display(report):
    """Readable summary; the saved JSON retains precise times and check results."""
    build = report['build']
    lines = ['OneDelivery · диагностика', 'Сборка: ' + (build.get('source_commit') or 'не записана'),
        'Проверка кода: ' + {'verified': 'подтверждена', 'unknown': 'нет сведений', 'modified': 'код изменён', 'invalid': 'ошибка метаданных'}.get(build['status'], 'нет сведений'),
        'Python: ' + report['runtime']['python'], '', 'Локальные данные:']
    for name, status in report['local_access'].items():
        lines.append(('Приложение' if name == 'application' else 'Состояние') + ': ' + ('чтение и запись доступны' if status == 'ok' else 'доступ не подтверждён'))
    worker = report['worker']
    lines += ['', 'Обработчик: ' + ('работает, статус свежий' if worker.get('running') and worker.get('fresh') else 'работа не подтверждена')]
    if 'paused' in worker: lines.append('Автоматика: ' + ('на паузе' if worker['paused'] else 'включена'))
    if 'build_matches' in worker and worker.get('running'):
        lines.append('Сборка обработчика: ' + ('совпадает с окном' if worker['build_matches'] else 'совпадение не подтверждено'))
    operation = report.get('operation', {})
    if operation.get('cause'): lines.append('Причина остановки/повтора: ' + operation['cause'])
    if operation.get('total'):
        lines.append(f"Страницы: подтверждено {operation.get('confirmed') or 0} из {operation['total']}")
    lines += ['', 'Задачи Windows:']
    if report['tasks']['status'] != 'ok': lines.append('Проверка задач недоступна')
    for item in report['tasks']['items']:
        label = 'Обработчик' if item['kind'] == 'worker' else 'Окно'
        text = 'не найдена' if not item['exists'] else 'другое назначение' if not item['matches'] else item['state']
        lines.append(label + ': ' + text)
    lines += ['', 'Связь с сервисами:']
    services_report = report['services']
    if services_report.get('status') == 'not_requested': lines.append('Не проверялась. Нажмите «Проверить связь».')
    for key in ('discord', 'vk'):
        if key in services_report: lines.append(key.upper() + ': ' + services_report[key]['message'])
    lines += ['', 'Отчёт содержит выбранные сведения о состоянии и событиях.', 'Токены, конфигурация, страницы и исходный журнал не прикладываются.']
    return '\n'.join(lines)
