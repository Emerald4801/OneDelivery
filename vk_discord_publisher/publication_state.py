"""Confirmed publication metadata. No credentials, network calls or message edits."""
import json
import math
from pathlib import Path
import re
import time

COOLDOWN = 72 * 60 * 60
ORIGINS = ('automatic', 'manual')
RECEIPT_NAME = re.compile(r'-?\d+_\d+(?:_\d+)?\.json')


class ReceiptError(ValueError):
    pass


def begin(state, origin):
    if origin not in ORIGINS:
        raise ValueError('Неизвестный способ запуска публикации.')
    # A retry must retain the provenance recorded before the first Discord POST.
    # Legacy pending publications have unknown provenance: do not invent an auto send.
    state.setdefault('publication_origin', 'manual' if state.get('card_id') or state.get('pending_card') else origin)


def finish(state, now=None):
    state['complete'] = True
    state.setdefault('completed_at', time.time() if now is None else now)


class PublicationIndex:
    """Cache parsed receipts; both current and archived receipts preserve the timer."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.cache = {}

    def summary(self, owner=None, bot_id=None):
        paths = list(self.directory.glob('*.json')) + list((self.directory / 'receipts').glob('*.json'))
        kept, completed, legacy, automatic = {}, [], [], []
        for path in paths:
            if not RECEIPT_NAME.fullmatch(path.name):
                continue
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = self.cache.get(path)
            try:
                item = cached[1] if cached and cached[0] == stamp else json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                raise ReceiptError('Не удалось прочитать подтверждение публикации: ' + path.name) from None
            kept[path] = (stamp, item)
            if not isinstance(item, dict):
                raise ReceiptError('Повреждено подтверждение публикации: ' + path.name)
            if not item.get('complete'):
                continue
            if owner is not None and not str(item.get('album', '')).startswith(str(owner) + '_'):
                continue
            if bot_id is not None and str(item.get('bot_id')) != str(bot_id):
                continue
            entry = {k: item.get(k) for k in ('chapter', 'completed_at', 'publication_origin', 'card_id')}
            if 'completed_at' not in item:
                # Display the known chapter without inventing its date or a timer.
                if str(item.get('chapter', '')).isdigit():
                    legacy.append(entry)
                continue
            when = item['completed_at']
            if (isinstance(when, bool) or not isinstance(when, (int, float))
                    or not math.isfinite(when) or when <= 0 or item.get('publication_origin') not in ORIGINS):
                raise ReceiptError('Некорректное время или способ публикации: ' + path.name)
            completed.append(entry)
            if item['publication_origin'] == 'automatic':
                automatic.append(when)
        self.cache = kept
        last = max(completed, key=lambda v: v['completed_at']) if completed else (
            max(legacy, key=lambda v: int(v['chapter'])) if legacy else None)
        all_entries = completed + legacy
        if all_entries and all(str(v.get('card_id', '')).isdigit() for v in all_entries):
            # Serialized sends have increasing Discord card IDs. Recovering an
            # older receipt later must not replace a newer manual chapter in UI.
            last = max(all_entries, key=lambda v: int(v['card_id']))
        return {'last_publication': last,
                'cooldown_until': max(automatic) + COOLDOWN if automatic else None}
