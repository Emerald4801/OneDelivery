"""A narrow, checked update of the installed v9 publisher; no tokens or API calls."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile

from worker_shutdown import application_lock

MARKER = '# VK_CARD_STATUS_UPDATE_1'


class PatchError(RuntimeError):
    pass


def exactly_one(items, description):
    items = list(items)
    if len(items) != 1:
        raise PatchError('Unsupported code layout: ' + description + '. No files were changed.')
    return items[0]


def function(tree, name):
    return exactly_one((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), name)


def replace_node(source, node, text):
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [text.rstrip() + '\n']
    return ''.join(lines)


def patch_publisher(source):
    if MARKER in source:
        compile(source, 'publish_album_v3.py', 'exec')
        return source
    tree = ast.parse(source)
    discord = exactly_one((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Discord'), 'Discord')
    post = function(discord, 'post_message')
    update = exactly_one((n for n in ast.walk(post) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Attribute) and n.func.attr == 'update'
                          and any(k.arg == 'flags' for k in n.keywords)), 'message flags')
    flag = exactly_one((k.value for k in update.keywords if k.arg == 'flags'), 'message flag value')
    if ast.dump(flag) != ast.dump(ast.parse('4096 | 4', mode='eval').body):
        raise PatchError('Message notification flags differ from v9. No files were changed.')
    lines = source.splitlines(keepends=True)
    line = lines[flag.lineno - 1]
    if flag.lineno != flag.end_lineno:
        raise PatchError('Unsupported multiline notification flags.')
    lines[flag.lineno - 1] = line[:flag.col_offset] + '(4096 if files_list(attachment) else 0) | 4' + line[flag.end_col_offset:]
    source = ''.join(lines)
    tree = ast.parse(source)
    formatter = function(tree, 'card_text')
    source = replace_node(source, formatter, '''def card_text(chapter, count, guild, thread=None, *, loading=False):
    text = f'**ГЛАВА {chapter}**\\nСтраниц: {count}'
    if thread:
        label = 'Глава загружается в' if loading else 'Опубликовано в'
        text += f'\\n{label}: <#{thread}>'
    return text
''')
    tree = ast.parse(source)
    publish = function(tree, 'publish')
    assign = exactly_one((n for n in ast.walk(publish) if isinstance(n, ast.Assign)
                          and ast.dump(n.targets[0]) == ast.dump(ast.parse("state['thread_id'] = thread_id").body[0].targets[0])
                          and isinstance(n.value, ast.Name) and n.value.id == 'thread_id'), 'thread assignment')
    lines = source.splitlines(keepends=True)
    lines.insert(assign.end_lineno, '''    api.request('PATCH', f'/channels/{channel}/messages/{card["id"]}',
                {'content': card_text(chapter, len(files), guild, thread_id, loading=True),
                 'allowed_mentions': {'parse': []}, 'embeds': [], 'flags': 4})
''')
    result = MARKER + '\n' + ''.join(lines)
    compile(result, 'publish_album_v3.py', 'exec')
    return result


def patch_pipeline(source):
    if MARKER in source:
        compile(source, 'ongoing.py', 'exec')
        return source
    tree = ast.parse(source)
    pipeline = exactly_one((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Pipeline'), 'Pipeline')
    latest = function(pipeline, 'latest')
    call = exactly_one((n for n in ast.walk(latest) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                        and n.func.value.id == 'publication_state' and n.func.attr == 'finish'
                        and len(n.args) == 1 and isinstance(n.args[0], ast.Name) and n.args[0].id == 'receipt'),
                       'v9 pending receipt recovery')
    names = {n.id for n in ast.walk(latest) if isinstance(n, ast.Name)}
    if not {'number', 'count', 'message', 'receipt'}.issubset(names):
        raise PatchError('Unsupported receipt recovery variable names.')
    lines = source.splitlines(keepends=True)
    indent = lines[call.lineno - 1][:call.col_offset]
    addition = '''final_text = publisher.card_text(str(number), count, self.guild, str(message['id']))
if message.get('content') != final_text:
    self.api.request('PATCH', f'/channels/{self.channel}/messages/{message["id"]}',
                     {'content': final_text, 'allowed_mentions': {'parse': []},
                      'embeds': [], 'flags': 4})
'''
    lines.insert(call.lineno - 1, ''.join(indent + line + '\n' for line in addition.splitlines()))
    result = MARKER + '\n' + ''.join(lines)
    compile(result, 'ongoing.py', 'exec')
    return result


TRANSFORMS = {'publish_album_v3.py': patch_publisher, 'ongoing.py': patch_pipeline}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(target, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + '.update_', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def prepare(root, backup):
    """Validate both files before preparing any change to the installed program."""
    root, backup = Path(root), Path(backup)
    prepared, manifest = {}, {}
    for name, transform in TRANSFORMS.items():
        original = (root / name).read_bytes()
        updated = transform(original.decode('utf-8-sig')).encode('utf-8')
        prepared[name] = (original, updated)
        manifest[name] = {'before': digest(original), 'after': digest(updated)}
    backup.mkdir(parents=True, exist_ok=False)
    (backup / 'original').mkdir(); (backup / 'prepared').mkdir()
    for name, (original, updated) in prepared.items():
        (backup / 'original' / name).write_bytes(original)
        (backup / 'prepared' / name).write_bytes(updated)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def restore(root, backup):
    root, backup = Path(root), Path(backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    for name, hashes in manifest.items():
        if name not in TRANSFORMS:
            raise PatchError('Unknown file in update manifest.')
        data = (backup / 'original' / name).read_bytes()
        if digest(data) != hashes['before']:
            raise PatchError('Backup verification failed: ' + name)
        atomic_write(root / name, data)


def apply(root, backup):
    root, backup = Path(root), Path(backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    # Detect any change since preparation before replacing the first file.
    for name, hashes in manifest.items():
        if name not in TRANSFORMS or digest((root / name).read_bytes()) != hashes['before']:
            raise PatchError('Installed source changed during update: ' + name)
        if digest((backup / 'prepared' / name).read_bytes()) != hashes['after']:
            raise PatchError('Prepared file verification failed: ' + name)
    try:
        for name in manifest:
            atomic_write(root / name, (backup / 'prepared' / name).read_bytes())
    except BaseException:
        restore(root, backup)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'apply', 'restore'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == 'prepare':
            prepare(args.root, args.backup)
        else:
            # Keep both application locks through replacement AND any rollback.
            with application_lock(args.root):
                {'apply': apply, 'restore': restore}[args.mode](args.root, args.backup)
        print('Card update: ' + args.mode + ' completed.')
        return 0
    except Exception as exc:
        print('Card update stopped: ' + str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
