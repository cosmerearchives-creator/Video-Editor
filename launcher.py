"""Small stable entry point for the Windows portable distribution."""
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


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
            with tempfile.TemporaryDirectory(prefix='cosmere-launch-') as temp:
                ready = Path(temp) / 'ready'
                env['COSMERE_CAM_READY_FILE'] = str(ready)
                child = subprocess.Popen([str(executable), *sys.argv[1:]], cwd=executable.parent, env=env)
                deadline = time.monotonic() + 30
                while child.poll() is None and not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                if ready.exists() or child.poll() is None:
                    return
            # A version that exits before showing its control window may have
            # failed during startup. Restore the last installed version once.
            if pointer == 'previous.json':
                raise RuntimeError('Both installed versions failed to start. Re-extract the release ZIP.')
            previous = root / 'app' / 'previous.json'
            if previous.exists():
                current = root / 'app' / 'current.json'
                staged = current.with_suffix('.tmp')
                staged.write_bytes(previous.read_bytes())
                os.replace(staged, current)
        raise RuntimeError('Application files are missing. Extract the entire release ZIP before launching.')
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Cosmere Cam could not start', 0x10)


if __name__ == '__main__':
    main()
