"""Run the real PowerShell 5.1 JSON bridge with substituted scheduler commands."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / 'vk_discord_publisher/task_bridge.ps1'


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell is required')
class BridgeChecks(unittest.TestCase):
    def execute(self, operation, foreign=False):
        with tempfile.TemporaryDirectory(prefix='OneDelivery ') as temporary:
            root = Path(temporary) / 'Папка с пробелами 🐸'
            root.mkdir()
            arguments = '"' + str(root / 'ongoing.py') + '"'
            xml = ('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                '<RegistrationInfo><URI>\\Old</URI></RegistrationInfo><Actions><Exec><Command>' +
                escape(str(root / '.venv/Scripts/pythonw.exe')) + '</Command><Arguments>' + escape(arguments) +
                '</Arguments><WorkingDirectory>' + escape(str(root)) + '</WorkingDirectory></Exec></Actions></Task>')
            request = dict(root=str(root), name='VKDiscordPublisher-100-200', operation=operation,
                           xml=xml, enabled=False)
            command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(HERE / 'test_task_bridge.ps1'), '-Bridge', str(BRIDGE), '-Root', str(root), '-Operation', operation]
            if foreign: command.append('-Foreign')
            result = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), encoding='utf-8',
                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            try: body = json.loads(result.stdout.lstrip('\ufeff'))
            except ValueError: self.fail('Invalid bridge JSON: ' + result.stdout + result.stderr)
            return result.returncode, body

    def test_every_operation_uses_only_the_substituted_scheduler(self):
        for operation in ('read', 'disable', 'start', 'stop', 'remove', 'restore'):
            with self.subTest(operation=operation):
                code, result = self.execute(operation)
                self.assertEqual(code, 0, result)
                self.assertTrue(result['ok'])
                if operation == 'read': self.assertIn('xml', result['result'])

    def test_foreign_task_is_rejected_before_mutation(self):
        code, result = self.execute('remove', foreign=True)
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertIn('different installation', result['error'])


if __name__ == '__main__':
    unittest.main()
