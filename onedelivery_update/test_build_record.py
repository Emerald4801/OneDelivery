"""Code and its installed build identity must change and roll back together."""
import json
import unittest
from unittest.mock import patch

import test_update as fixtures
import update_settings as update
import app_version


class BuildRecordChecks(unittest.TestCase):
    setUp = fixtures.UpdateChecks.setUp
    write_manifest = fixtures.UpdateChecks.write_manifest
    assert_original_files = fixtures.UpdateChecks.assert_original_files

    def prepare(self):
        self.manifest.update(source_commit='a'*40, source_dirty=False, built_at=1000)
        self.write_manifest()
        update.prepare(self.root, self.package, self.backup)

    def test_first_update_records_verified_build_repeat_preserves_it_and_rollback_removes_it(self):
        self.prepare()
        update.apply_files(self.root,self.package,self.backup)
        path = self.root/app_version.RECORD
        original = path.read_bytes()
        self.assertEqual(app_version.describe(self.root)['status'],'verified')
        self.assertFalse(update.prepare(self.root,self.package,self.base/'unused'))
        self.assertEqual(path.read_bytes(),original)
        update.restore_files(self.root,self.backup)
        self.assertFalse(path.exists())
        self.assert_original_files()

    def test_rollback_restores_previous_build_record_exactly(self):
        path = self.root/app_version.RECORD
        path.parent.mkdir()
        original = b'{"schema":1,"source_commit":"old-installation"}\n'
        path.write_bytes(original)
        self.prepare()
        update.apply_files(self.root,self.package,self.backup)
        update.restore_files(self.root,self.backup)
        self.assertEqual(path.read_bytes(),original)
        self.assert_original_files()

    def test_failed_build_record_write_rolls_back_code(self):
        self.prepare()
        actual = update.atomic_write
        def write(path,data):
            if path.name == 'installed_build.json': raise OSError('simulated full disk')
            return actual(path,data)
        with patch.object(update,'atomic_write',side_effect=write):
            with self.assertRaises(OSError): update.apply_files(self.root,self.package,self.backup)
        self.assert_original_files()
        self.assertFalse((self.root/app_version.RECORD).exists())

    def test_changed_record_after_preparation_prevents_code_replacement(self):
        self.prepare()
        path = self.root/app_version.RECORD
        path.parent.mkdir()
        path.write_bytes(b'changed independently')
        with self.assertRaisesRegex(ValueError,'изменились'):
            update.apply_files(self.root,self.package,self.backup)
        self.assert_original_files()

    def test_corrupt_previous_record_backup_is_rejected_before_restoring_code(self):
        path = self.root/app_version.RECORD
        path.parent.mkdir()
        path.write_bytes(b'previous record')
        self.prepare()
        update.apply_files(self.root,self.package,self.backup)
        before = {name:(self.root/name).read_bytes() for name in update.FILES}
        (self.backup/'build_before.json').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'Повреждены'):
            update.restore_files(self.root,self.backup)
        self.assertEqual(before,{name:(self.root/name).read_bytes() for name in update.FILES})


if __name__ == '__main__':
    unittest.main()
