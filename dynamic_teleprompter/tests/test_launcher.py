import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class LauncherTests(unittest.TestCase):
    def test_failed_new_version_restores_and_launches_previous(self):
        source = Path(__file__).parents[2] / 'launcher.py'
        spec = importlib.util.spec_from_file_location('portable_launcher', source)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in ('0.5.0-alpha17', '0.5.0-alpha18'):
                folder = root / 'app' / 'versions' / version
                folder.mkdir(parents=True)
                (folder / 'CosmereCam.exe').write_bytes(b'fake')
            (root / 'app/current.json').write_text(json.dumps({'version': '0.5.0-alpha18'}))
            (root / 'app/previous.json').write_text(json.dumps({'version': '0.5.0-alpha17'}))
            started = []

            def start(command, cwd, env):
                started.append(command[0])
                if len(started) == 1:
                    return SimpleNamespace(poll=lambda: 1)
                Path(env['COSMERE_CAM_READY_FILE']).write_text('ready')
                self.assertEqual(env['PYINSTALLER_RESET_ENVIRONMENT'], '1')
                return SimpleNamespace(poll=lambda: None)

            with patch.object(launcher.sys, 'executable', str(root / 'Cosmere Cam.exe')), \
                    patch.object(launcher.sys, 'argv', ['Cosmere Cam.exe']), \
                    patch.object(launcher.subprocess, 'Popen', side_effect=start):
                launcher.main()
            self.assertEqual(len(started), 2)
            self.assertIn('alpha17', started[1])
            self.assertEqual(json.loads((root / 'app/current.json').read_text())['version'], '0.5.0-alpha17')


if __name__ == '__main__':
    unittest.main()
