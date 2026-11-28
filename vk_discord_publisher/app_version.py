"""Identify installed bytes without requiring Git or reading user configuration."""
import hashlib
import json
import math
from pathlib import Path
import re

RECORD = Path('updates') / 'installed_build.json'


def describe(root):
    root = Path(root)
    result = dict(status='unknown', label='Сборка не записана', source_commit=None,
                  built_at=None, installed_at=None, modified=[])
    path = root / RECORD
    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data.get('schema') != 1 or not re.fullmatch(r'[0-9a-f]{40}', data.get('source_commit', ''))
                or not isinstance(data.get('files'), dict) or not data['files']):
            raise ValueError()
        for name, digest in data['files'].items():
            if (not re.fullmatch(r'[A-Za-z0-9_-]+\.(py|ps1|cmd|lock|txt)', name)
                    or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise ValueError()
            file = root / name
            if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                result['modified'].append(name)
        result.update(source_commit=data['source_commit'], status='verified')
        for key in ('built_at', 'installed_at'):
            value = data.get(key)
            result[key] = value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799 else None
        result['label'] = 'Сборка ' + data['source_commit'][:8]
        if result['modified'] or data.get('source_dirty'):
            result.update(status='modified', label=result['label'] + ' · есть изменения кода')
    except (ValueError, TypeError, AttributeError, OSError):
        result.update(status='invalid', label='Не удалось проверить сборку')
    return result
