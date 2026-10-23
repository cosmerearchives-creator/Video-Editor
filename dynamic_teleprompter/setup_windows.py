"""Interactive, user-level first-time setup. Safe to run repeatedly."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from setup_live2d_sdk import runtime_directory, install_sdk, DLL

HERE = Path(__file__).resolve().parent
REQUIRED = [('PySide6', 'PySide6>=6.6,<7'), ('spellchecker', 'pyspellchecker>=0.8,<1'),
            ('vosk', 'vosk>=0.3.45,<1'), ('sounddevice', 'sounddevice>=0.4.7,<1'),
            ('numpy', 'numpy>=2.2,<3')]
OPTIONAL = [('cv2', 'opencv-python>=4.10,<5', 'webcam face tracking'),
            ('pedalboard', 'pedalboard>=0.9.20,<1', 'VST3 plugin hosting'),
            ('pygrabber', 'pygrabber>=0.2,<1', 'Windows webcam friendly names (optional)')]


def ask(question, default=False):
    if not sys.stdin.isatty():
        return default
    suffix = '[Y/n]' if default else '[y/N]'
    return input(f'{question} {suffix}: ').strip().lower() in (('','y','yes') if default else ('y','yes'))


def pip_install(specs):
    if not specs:
        return True
    print('Installing Python packages:', ', '.join(specs), flush=True)
    return subprocess.call([sys.executable, '-m', 'pip', 'install', *specs]) == 0


def main():
    print('Dynamic Teleprompter v0.5.0-alpha13.2 - Unified Setup')
    print('Python:', sys.version.split()[0], '| Executable:', sys.executable)
    missing = [spec for module, spec in REQUIRED if importlib.util.find_spec(module) is None]
    if missing and not pip_install(missing):
        print('ERROR: Required Python packages could not be installed.')
        return 1
    print('Required Python dependencies: available.')
    for module, spec, label in OPTIONAL:
        if importlib.util.find_spec(module) is not None:
            print(f'{label}: already installed.')
        elif ask(f'Install optional {label}?', default=True):
            if not pip_install([spec]):
                print(f'WARNING: Failed to install optional {label}; it can be retried later.')
    dll = runtime_directory() / DLL
    legacy = HERE / 'live2d_sdk' / DLL
    if dll.is_file():
        print('Permanent Live2D Core: available at', dll)
    elif legacy.is_file():
        print('Found older local Cubism Core:', legacy)
        if ask('Copy it to the permanent user runtime location?', default=True):
            dll.parent.mkdir(parents=True, exist_ok=True)
            dll.write_bytes(legacy.read_bytes())
            print('Installed permanent Live2D Core:', dll)
    else:
        print('Live2D Core is optional until you load a Live2D model.')
        if ask('Install Cubism Core from your officially downloaded SDK ZIP?', default=True):
            archive = input('Enter full path to Cubism SDK ZIP (Enter to skip): ').strip() if sys.stdin.isatty() else ''
            if archive:
                try:
                    print('Installed:', install_sdk(archive))
                except (OSError, ValueError) as error:
                    print('WARNING: SDK installation failed:', error)
    if ask('Install optional CMake / Visual Studio developer tools via winget?', default=False):
        subprocess.call(['cmd', '/c', str(HERE / 'install_build_tools_windows.bat')]) if os.name == 'nt' else print('Developer installer is Windows-only.')
    print('Setup complete. Launch the application with start_windows.bat.')
    return 0

if __name__ == '__main__':
    sys.exit(main())
