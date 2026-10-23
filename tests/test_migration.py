import configparser
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from secrets_macos import _rows_with_binary_column
import zen_profile


def read_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read(path)
    return config


def file_contents(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.zen_root = self.root / 'zen'
        self.binary = self.root / 'Zen.app/Contents/MacOS/zen'
        self.staged = self.root / 'staged'
        self.staged.mkdir()
        for name in ('prefs.js', 'places.sqlite', 'cookies.sqlite', 'key4.db'):
            (self.staged / name).write_bytes(('imported-' + name).encode())
        (self.staged / 'compatibility.ini').write_text(
            '[Compatibility]\nLastPlatformDir=' +
            str(self.binary.parents[1] / 'Resources') + '\n')
        (self.staged / '.parentlock').write_bytes(b'ignored-lock')
        self.backup = self.root / 'backup'
        self.process_guard = patch.object(zen_profile, 'zen_running', return_value=False)
        self.process_guard.start()
        self.addCleanup(self.process_guard.stop)

    def existing_profile(self):
        target = self.zen_root / 'Profiles/current'
        target.mkdir(parents=True)
        (target / 'prefs.js').write_bytes(b'original-preferences')
        (target / 'original.sqlite').write_bytes(b'original-data')
        other = self.zen_root / 'Profiles/legacy'
        other.mkdir()
        (other / 'keep.txt').write_bytes(b'unrelated-profile')
        (self.zen_root / 'profiles.ini').write_text(
            '[General]\nStartWithLastProfile=1\nVersion=2\n\n'
            '[Profile0]\nName=Current\nIsRelative=1\nPath=Profiles/current\n\n'
            '[Profile1]\nName=Legacy\nIsRelative=1\nPath=Profiles/legacy\nDefault=1\n\n'
            '[InstallTEST]\nDefault=Profiles/current\nLocked=1\n')
        (self.zen_root / 'installs.ini').write_text(
            '[TEST]\nDefault=Profiles/current\nLocked=1\n')
        return target

    def test_binary_ciphertext_stored_as_sqlite_text_is_read_without_utf8_decoding(self):
        payload = b'v10\xff\xfe\x80\x00binary'
        with sqlite3.connect(':memory:') as database:
            database.row_factory = sqlite3.Row
            database.execute('CREATE TABLE cookies(host_key TEXT, encrypted_value BLOB)')
            database.execute('INSERT INTO cookies VALUES(?, CAST(? AS TEXT))',
                             ('example.invalid', payload))
            self.assertEqual(database.execute('SELECT typeof(encrypted_value) FROM cookies').fetchone()[0], 'text')
            with self.assertRaises(sqlite3.Error):
                database.execute('SELECT encrypted_value FROM cookies').fetchone()
            row = next(iter(_rows_with_binary_column(database, 'cookies', 'encrypted_value')))
            self.assertEqual(row['host_key'], 'example.invalid')
            self.assertEqual(row['encrypted_value'], payload)
            self.assertIsInstance(row['encrypted_value'], bytes)

    def test_replacement_reuses_default_path_and_preserves_other_profiles(self):
        target = self.existing_profile()
        original_profile = file_contents(target)
        original_config = (self.zen_root / 'profiles.ini').read_bytes()
        staged_before = file_contents(self.staged)
        self.assertEqual(zen_profile.resolve_default_profile(self.zen_root, self.binary), target)
        result = zen_profile.install_profile(self.staged, target, self.zen_root,
                                             self.backup, binary=self.binary)
        self.assertEqual(result['profile'], str(target))
        self.assertTrue(result['replaced_existing'])
        self.assertEqual(file_contents(self.backup / 'profil-zen-original'), original_profile)
        self.assertEqual((self.backup / 'profiles.ini').read_bytes(), original_config)
        self.assertEqual(file_contents(self.staged), staged_before)
        self.assertEqual(file_contents(target),
                         {name: value for name, value in staged_before.items() if name != '.parentlock'})
        self.assertEqual((self.zen_root / 'Profiles/legacy/keep.txt').read_bytes(), b'unrelated-profile')
        config = read_config(self.zen_root / 'profiles.ini')
        self.assertEqual([name for name in config.sections() if name.startswith('Profile')],
                         ['Profile0', 'Profile1'])
        self.assertEqual(config['Profile0']['Default'], '1')
        self.assertEqual(config['InstallTEST']['Default'], 'Profiles/current')
        self.assertEqual(read_config(self.zen_root / 'installs.ini')['TEST']['Default'], 'Profiles/current')
        for name in ('profiles.ini', 'installs.ini'):
            for line in (self.zen_root / name).read_text().splitlines():
                if '=' in line:
                    key, value = line.split('=', 1)
                    self.assertEqual(key, key.rstrip())
                    self.assertEqual(value, value.lstrip())

    def test_configuration_failure_restores_profile_and_registry_exactly(self):
        target = self.existing_profile()
        before = file_contents(self.zen_root)
        staged_before = file_contents(self.staged)
        write = zen_profile._atomic_write
        failed = False

        def fail_once(path, data):
            nonlocal failed
            if path == self.zen_root / 'profiles.ini' and not failed:
                failed = True
                raise OSError('Synthetic write failure')
            return write(path, data)

        with patch.object(zen_profile, '_atomic_write', side_effect=fail_once):
            with self.assertRaisesRegex(OSError, 'Synthetic write failure'):
                zen_profile.install_profile(self.staged, target, self.zen_root,
                                            self.backup, binary=self.binary)
        self.assertTrue(failed)
        self.assertEqual(file_contents(self.zen_root), before)
        self.assertEqual(file_contents(self.staged), staged_before)
        self.assertEqual(list(target.parent.glob('.arc-import-*')), [])
        self.assertEqual((self.backup / 'profil-zen-original/original.sqlite').read_bytes(), b'original-data')

    def test_first_install_registers_only_its_default_profile(self):
        target = zen_profile.resolve_default_profile(self.zen_root, self.binary)
        self.assertFalse(self.zen_root.exists())
        result = zen_profile.install_profile(self.staged, target, self.zen_root,
                                             self.backup, binary=self.binary)
        config = read_config(self.zen_root / 'profiles.ini')
        registered = [name for name in config.sections() if name.startswith('Profile')]
        self.assertEqual(registered, ['Profile0'])
        self.assertEqual(config['Profile0']['Default'], '1')
        self.assertEqual(result['registered_profile_count'], 1)
        self.assertFalse(result['replaced_existing'])
        self.assertEqual(zen_profile.resolve_default_profile(self.zen_root, self.binary), target)
        self.assertEqual([path for path in (self.zen_root / 'Profiles').iterdir() if path.is_dir()], [target])


if __name__ == '__main__':
    unittest.main()
