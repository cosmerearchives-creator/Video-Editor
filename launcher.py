"""Small stable entry point for the Windows portable distribution."""
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    root = Path(sys.executable).resolve().parent
    try:
        for pointer in ('current.json', 'previous.json'):
            path = root / 'app' / pointer
            if not path.is_file():
                continue
            version = json.loads(path.read_text(encoding='utf-8'))['version']
            if not re.fullmatch(r'\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\d+)?', version):
                continue
            executable = root / 'app' / 'versions' / version / 'CosmereCam.exe'
            if not executable.is_file():
                continue
            env = dict(os.environ, COSMERE_CAM_INSTALL_ROOT=str(root))
            # PyInstaller's process state must not attach the new app to the
            # launcher's temporary one-file extraction directory.
            env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            subprocess.Popen([str(executable), *sys.argv[1:]], cwd=executable.parent, env=env)
            return
        raise RuntimeError('Application files are missing. Extract the entire release ZIP before launching.')
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Cosmere Cam could not start', 0x10)


if __name__ == '__main__':
    main()
