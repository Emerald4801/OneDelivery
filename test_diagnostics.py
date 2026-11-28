"""Diagnostics must explain installed state without exporting secrets or publishing."""
import hashlib
import json
from pathlib import Path
import tempfile
import types
import subprocess
import unittest
from unittest.mock import Mock, patch

import test_ongoing  # canonical import path
import app_version
import diagnostics
import ongoing
import publisher_runtime as runtime
import vk_source

CFG = dict(guild_id='100', channel_id='200')
COMMIT = 'a' * 40


class DiagnosticChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'state/100/200').mkdir(parents=True)
        self.state = dict(control=dict(paused=True, manual=dict(status='running', chapter=1200),
            recovery=dict(status='retry', retry_at=2000, message='Нет соединения', failed_at=1000)),
            running=True, fresh=True, started_at=100, expected=1200,
            transfer=dict(confirmed=6,total=16), next_check=2000)

    def collect(self, **options):
        with patch.object(diagnostics, 'tasks', return_value=dict(status='unavailable', items=[])):
            return diagnostics.collect(self.root, CFG, lambda: self.state, **options)

    def record(self):
        path = self.root / 'app_version.py'
        path.write_bytes(b'# installed code\n')
        data = dict(schema=1, source_commit=COMMIT, built_at=900, installed_at=950,
                    files={path.name:hashlib.sha256(path.read_bytes()).hexdigest()})
        record = self.root / app_version.RECORD
        record.parent.mkdir(exist_ok=True)
        record.write_text(json.dumps(data), encoding='utf-8')
        return record

    def test_build_is_verified_against_installed_bytes_and_detects_modification_or_loss(self):
        self.record()
        self.assertEqual(app_version.describe(self.root)['status'], 'verified')
        (self.root/'app_version.py').write_bytes(b'# edited\n')
        self.assertEqual(app_version.describe(self.root)['modified'], ['app_version.py'])
        (self.root/'app_version.py').unlink()
        self.assertEqual(app_version.describe(self.root)['status'], 'modified')

    def test_missing_metadata_does_not_guess_version_from_repository(self):
        self.assertEqual(app_version.describe(self.root)['status'], 'unknown')
        self.record().write_text('{broken', encoding='utf-8')
        self.assertEqual(app_version.describe(self.root)['status'], 'invalid')

    def test_build_metadata_cannot_follow_a_path_outside_application(self):
        record = self.record()
        data = json.loads(record.read_text())
        data['files'] = {'../token.dpapi':'0'*64}
        record.write_text(json.dumps(data))
        self.assertEqual(app_version.describe(self.root)['status'], 'invalid')

    def test_dirty_source_is_identified_as_modified_even_if_payload_matches(self):
        record = self.record()
        data = json.loads(record.read_text())
        data['source_dirty'] = True
        record.write_text(json.dumps(data))
        self.assertEqual(app_version.describe(self.root)['status'], 'modified')

    def test_local_diagnostics_never_reads_tokens_or_contacts_services(self):
        for name in ('config.json','token.dpapi','vk_api_token.dpapi'):
            (self.root/name).write_bytes(b'opaque-test-secret')
        with patch.object(diagnostics, 'services', side_effect=AssertionError('network forbidden')):
            report = self.collect()
        self.assertEqual(report['services']['status'], 'not_requested')
        self.assertTrue(report['worker']['paused'])
        self.assertEqual(report['operation']['confirmed'], 6)
        self.assertEqual(report['operation']['retry_at'], 2000)
        self.assertEqual(report['events'][-1]['event'], 'failure')
        self.assertFalse(list(self.root.rglob('onedelivery_probe_*')))

    def test_report_projects_only_allowed_fields_including_error_text(self):
        secret = 'ArbitraryUnrecognizedSecret12345'
        self.state.update(token=secret, phase=secret, unknown=secret)
        self.state['control']['recovery']['message'] = 'HTTP https://example.invalid/?token='+secret
        self.state['control']['manual']['id'] = secret
        report = self.collect()
        path = diagnostics.save(self.root, report)
        self.assertNotIn(secret, path.read_text(encoding='utf-8'))
        self.assertNotIn('https://', path.read_text(encoding='utf-8'))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), report)
        self.assertNotIn(secret, diagnostics.display(report))

    def test_corrupt_state_still_allows_a_local_diagnostic_report(self):
        with patch.object(diagnostics, 'tasks', return_value=dict(status='unavailable', items=[])):
            report = diagnostics.collect(self.root, CFG, Mock(side_effect=ValueError('secret')))
        self.assertEqual(report['worker']['status'], 'unavailable')
        self.assertTrue(diagnostics.save(self.root, report).is_file())

    def test_unwritable_working_directory_is_reported_without_leaving_probe(self):
        with patch.object(diagnostics.tempfile, 'NamedTemporaryFile', side_effect=PermissionError('private')):
            report = self.collect()
        self.assertEqual(report['local_access']['application'], 'unavailable')
        self.assertNotIn('private', json.dumps(report))

    def test_task_adapter_filters_fields_and_never_exports_raw_task_xml(self):
        result = dict(ok=True, tasks=[dict(kind='worker',exists=True,matches=True,enabled=True,state='Running',xml='secret'),
                                      dict(kind='window',exists=False,secret='private')])
        with patch.object(diagnostics.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps(result))) as run:
            report = diagnostics.tasks(self.root, CFG)
        self.assertEqual(report['status'], 'ok')
        self.assertNotIn('secret', json.dumps(report))
        self.assertIn('diagnostic_tasks.ps1', ' '.join(run.call_args.args[0]))

    def test_task_timeout_and_malformed_response_are_explicitly_unavailable(self):
        for result in ('broken', '{}'):
            with patch.object(diagnostics.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0,stdout=result)):
                self.assertEqual(diagnostics.tasks(self.root, CFG)['status'], 'unavailable')
        with patch.object(diagnostics.subprocess, 'run', side_effect=diagnostics.subprocess.TimeoutExpired('test',20)):
            self.assertEqual(diagnostics.tasks(self.root, CFG)['status'], 'unavailable')

    def test_real_powershell_task_reader_uses_only_mocked_read_commands(self):
        script_path = str(Path(diagnostics.__file__).with_name('diagnostic_tasks.ps1')).replace("'", "''")
        root = str(self.root).replace("'", "''")
        script = """
function Get-ScheduledTask {
    param($TaskName,$TaskPath)
    $script = if ($TaskName -like '*-Status-*') { 'status_app.py' } else { 'ongoing.py' }
    $argument = '"' + (Join-Path $global:mockRoot $script) + '"'
    if ($script -eq 'status_app.py') { $argument += ' --tray' }
    [pscustomobject]@{State='Running'; Settings=[pscustomobject]@{Enabled=$true}; Actions=@([pscustomobject]@{
        Execute=(Join-Path $global:mockRoot '.venv\\Scripts\\pythonw.exe'); Arguments=$argument; WorkingDirectory=$global:mockRoot})}
}
function Get-ScheduledTaskInfo { param($TaskName,$TaskPath); [pscustomobject]@{LastTaskResult=0} }
function Register-ScheduledTask { throw 'Task mutation forbidden' }
function Start-ScheduledTask { throw 'Task mutation forbidden' }
function Stop-ScheduledTask { throw 'Task mutation forbidden' }
""" + "\n$global:mockRoot = '" + root + "'\n& '" + script_path + "'"
        result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-Command',script],
            input=json.dumps(dict(root=str(self.root),guild='100',channel='200')),
            capture_output=True,encoding='utf-8',errors='replace',timeout=20,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout.lstrip('\ufeff'))
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['tasks']),2)
        self.assertTrue(all(item['matches'] for item in data['tasks']))

    def test_explicit_network_check_uses_only_read_apis_and_closes_clients(self):
        api, source = Mock(), Mock()
        with patch.object(runtime,'Discord',return_value=api), patch.object(ongoing,'noninteractive_token',return_value='test-only'), \
             patch.object(runtime,'check') as check, patch.object(vk_source,'VKSource',return_value=source):
            result = diagnostics.services(self.root, CFG)
        check.assert_called_once_with(api, CFG)
        source.api_client.albums.assert_called_once_with(source.owner, full=False)
        source.albums.assert_not_called()
        api.close.assert_called_once()
        source.close.assert_called_once()
        self.assertEqual(result['discord']['status'], 'ok')
        self.assertEqual(result['vk']['status'], 'ok')

    def test_service_failures_do_not_export_exception_bodies_or_skip_other_service(self):
        api, source = Mock(), Mock()
        source.api_client.albums.side_effect = RuntimeError('VK_SECRET')
        with patch.object(runtime,'Discord',return_value=api), patch.object(ongoing,'noninteractive_token',return_value='test-only'), \
             patch.object(runtime,'check',side_effect=runtime.ApiError(403)), patch.object(vk_source,'VKSource',return_value=source):
            result = diagnostics.services(self.root, CFG)
        self.assertEqual(result['discord']['http_status'],403)
        self.assertEqual(result['vk']['status'],'error')
        self.assertNotIn('VK_SECRET',json.dumps(result))
        source.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
