"""Install the user-supplied Live2D Cubism Core runtime without redistributing it."""
import os
import sys
import zipfile
from pathlib import Path

DLL = 'Live2DCubismCore.dll'


def runtime_directory():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        # Non-Windows convenience for unit testing only.
        base = str(Path.home() / '.local' / 'share')
    return Path(base) / 'DynamicTeleprompter' / 'Runtime' / 'Live2D'


def install_sdk(source):
    source = Path(source.strip().strip('"')).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f'SDK archive not found: {source}')
    with zipfile.ZipFile(source) as archive:
        matches = [n for n in archive.namelist() if n.replace('\\', '/').endswith('/Core/dll/windows/x86_64/' + DLL)]
        if len(matches) != 1:
            raise ValueError('Expected exactly one Windows x86_64 Cubism Core DLL in the official SDK ZIP')
        info = archive.getinfo(matches[0])
        if info.file_size > 100_000_000:
            raise ValueError('Unreasonably large SDK DLL; refusing to extract')
        blob = archive.read(info)
    folder = runtime_directory()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / DLL
    target.write_bytes(blob)
    return target


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('Usage: py -3 setup_live2d_sdk.py "path-to-official-CubismSdkForNative.zip"')
    try:
        print('Installed licensed Cubism Core for this user:', install_sdk(sys.argv[1]))
        print('The SDK runtime is not included in distributed Dynamic Teleprompter releases.')
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        sys.exit(f'Live2D setup failed: {exc}')
