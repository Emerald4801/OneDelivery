"""One-way local status for the tray viewer; never reads or writes credentials."""
import json
import os
from pathlib import Path
import re
import threading
import time
import app_version


def destination(root, config):
    guild, channel = str(config['guild_id']), str(config['channel_id'])
    if not guild.isdigit() or not channel.isdigit():
        raise ValueError('Некорректный ID сервера или канала.')
    return Path(root) / 'state' / guild / channel


def safe_error(error):
    return re.sub(r'https?://\S+', '[адрес скрыт]', str(error))[:1500]


class StatusWriter:
    def __init__(self, root, config):
        self.path = destination(root, config) / 'worker_status.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.data = dict(schema=1, pid=os.getpid(), running=True, started_at=time.time(),
                         guild_id=str(config['guild_id']), channel_id=str(config['channel_id']),
                         phase='Запуск', error=None, action_required=False)
        build = app_version.describe(root)
        self.data['build'] = {key: build[key] for key in ('status', 'source_commit')}
        self.update()
        self.thread = threading.Thread(target=self._heartbeat, name='local-status', daemon=True)
        self.thread.start()

    def update(self, **values):
        with self.lock:
            self.data.update(values)
            self.data['heartbeat_at'] = time.time()
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)

    def _heartbeat(self):
        while not self.stop.wait(10):
            try:
                self.update()
            except OSError:
                pass  # The viewer detects a stale heartbeat; never interrupt a Discord send.

    def close(self, failed=False):
        self.stop.set()
        self.thread.join(timeout=2)
        self.update(running=False, phase='Остановлено с ошибкой' if failed else 'Остановлено')
