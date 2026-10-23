import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import updates
from release_info import PRODUCT, REPOSITORY


class UpdateTests(unittest.TestCase):
    def release(self, tag='v0.5.0-alpha99'):
        name = f'{PRODUCT}-{tag}-Windows-x64.zip'
        return {'tag_name': tag, 'prerelease': '-' in tag, 'assets': [{
            'name': name, 'digest': 'sha256:' + 'a' * 64,
            'browser_download_url': f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'}]}

    def archive(self, version='0.5.0-alpha99', extra=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            prefix = f'app/versions/{version}/'
            archive.writestr(prefix + 'CosmereCam.exe', b'MZ-test executable')
            archive.writestr(prefix + 'build.json', json.dumps({
                'product': PRODUCT, 'version': version, 'launcher_protocol': 1}))
            archive.writestr('Cosmere Cam.exe', b'not substituted while running')
            if extra:
                archive.writestr(*extra)
        return output.getvalue()

    def test_version_order_and_preview_channel(self):
        self.assertLess(updates.version_key('v0.5.0-alpha9'), updates.version_key('v0.5.0-alpha17'))
        self.assertIsNotNone(updates.select_release([self.release()]))
        self.assertIsNone(updates.select_release([self.release()], current='0.4.0'))
        self.assertIsNone(updates.select_release([self.release('v0.5.0-alpha16')]))
        self.assertIsNotNone(updates.select_release([self.release('v0.5.0')]))

    def test_requires_expected_asset_repo_and_digest(self):
        for field, value in (('digest', ''), ('browser_download_url', 'https://evil.invalid/a.zip'), ('name', 'source.zip')):
            release = self.release()
            release['assets'][0][field] = value
            self.assertIsNone(updates.select_release([release]))

    def test_atomic_install_and_rollback_leave_launcher_and_user_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'app' / 'versions' / '0.5.0-alpha17').mkdir(parents=True)
            (root / 'app' / 'versions' / '0.5.0-alpha17' / 'CosmereCam.exe').write_bytes(b'old')
            (root / 'Cosmere Cam.exe').write_bytes(b'original launcher')
            (root / 'project.json').write_text('user data')
            updates.atomic_json(root / 'app' / 'current.json', {'version': '0.5.0-alpha17'})
            archive = self.archive()
            release = updates.select_release([self.release()])
            release['sha256'], release['size'] = hashlib.sha256(archive).hexdigest(), len(archive)
            with patch.object(updates.urllib.request, 'urlopen', return_value=io.BytesIO(archive)):
                updates.install_update(release, root)
            self.assertEqual(json.loads((root / 'app/current.json').read_text())['version'], '0.5.0-alpha99')
            self.assertEqual((root / 'Cosmere Cam.exe').read_bytes(), b'original launcher')
            self.assertEqual((root / 'project.json').read_text(), 'user data')
            updates.rollback(root)
            self.assertEqual(json.loads((root / 'app/current.json').read_text())['version'], '0.5.0-alpha17')

    def test_checksum_failure_keeps_current_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Cosmere Cam.exe').write_bytes(b'launcher')
            release = updates.select_release([self.release()])
            with patch.object(updates.urllib.request, 'urlopen', return_value=io.BytesIO(self.archive())):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    updates.install_update(release, root)
            self.assertFalse((root / 'app/current.json').exists())

    def test_traversal_duplicate_and_wrong_identity_are_rejected(self):
        for name in ('../escape.exe', 'app/versions/0.5.0-alpha99/../../escape.exe', 'C:\\escape.exe'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / 'download.zip'
                archive.write_bytes(self.archive(extra=(name, b'bad')))
                with self.assertRaisesRegex(ValueError, 'Unsafe path'):
                    updates.extract_version(archive, root, '0.5.0-alpha99')
                self.assertFalse((root / 'app/versions/0.5.0-alpha99').exists())


if __name__ == '__main__':
    unittest.main()
