"""Build the one-launcher Windows portable ZIP; never package private assets."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'dynamic_teleprompter'
sys.path.insert(0, str(SOURCE))
from release_info import PRODUCT, VERSION


def main():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    output = build / 'dist'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
        '--onefile', '--name', 'Cosmere Cam', '--distpath', str(output),
        '--workpath', str(build / 'launcher'), '--specpath', str(build), str(ROOT / 'launcher.py')], check=True)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
        '--onedir', '--name', 'CosmereCam', '--distpath', str(output),
        '--workpath', str(build / 'application'), '--specpath', str(build), '--paths', str(SOURCE)]
    for package in ('stable_whisper', 'whisper', 'tiktoken', 'imageio_ffmpeg', 'vosk', 'cv2', 'pedalboard', '_sounddevice_data'):
        command += ['--collect-all', package]
    command += ['--hidden-import', 'tiktoken_ext.openai_public', '--copy-metadata', 'stable-ts',
                '--copy-metadata', 'openai-whisper', '--exclude-module', 'matplotlib',
                '--exclude-module', 'IPython', '--exclude-module', 'pytest']
    native = SOURCE / 'native' / 'dt_engine.dll'
    if native.exists():
        command += ['--add-binary', str(native) + ';native']
    command += [str(SOURCE / 'desktop_entry.py')]
    subprocess.run(command, check=True)
    portable = build / 'portable'
    if portable.exists():
        # Only this script's fixed staging directory is replaced on rebuild.
        assert portable.resolve().parent == build.resolve()
        shutil.rmtree(portable)
    version_dir = portable / 'app' / 'versions' / VERSION
    version_dir.parent.mkdir(parents=True)
    shutil.copytree(output / 'CosmereCam', version_dir)
    shutil.copy2(output / 'Cosmere Cam.exe', portable / 'Cosmere Cam.exe')
    (version_dir / 'build.json').write_text(json.dumps({
        'product': PRODUCT, 'version': VERSION, 'launcher_protocol': 1}), encoding='utf-8')
    (portable / 'app' / 'current.json').write_text(json.dumps({'version': VERSION}), encoding='utf-8')
    shutil.copytree(ROOT / 'docs', portable / 'docs')
    shutil.copy2(ROOT / 'docs' / 'START-HERE.txt', portable / 'START-HERE.txt')
    licenses = portable / 'docs' / 'third-party-licenses'
    licenses.mkdir()
    inventory = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', 'unknown')
        inventory.append(f"{name}=={distribution.version}")
        for file in distribution.files or []:
            text = str(file).replace('\\', '/')
            if '.dist-info/' in text and any(token in Path(text).name.casefold() for token in ('license', 'copying', 'notice')):
                origin = Path(distribution.locate_file(file))
                if origin.is_file():
                    directory = licenses / name
                    directory.mkdir(exist_ok=True)
                    shutil.copy2(origin, directory / origin.name)
    (portable / 'docs' / 'dependency-versions.txt').write_text('\n'.join(sorted(inventory)), encoding='utf-8')
    # Verify the actual frozen application, including Qt image export and FFmpeg.
    report = build / 'frozen-smoke.json'
    subprocess.run([str(version_dir / 'CosmereCam.exe'), '--smoke-test', '--smoke-report', str(report)],
                   check=True, timeout=180)
    if not json.loads(report.read_text(encoding='utf-8')).get('success'):
        raise RuntimeError('Frozen application smoke test failed')
    voice = build / 'speech-test.wav'
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                    str(ROOT / 'tools' / 'make_test_voice.ps1'), '-Destination', str(voice)], check=True)
    speech_report = build / 'frozen-alignment.json'
    subprocess.run([str(version_dir / 'CosmereCam.exe'), '--smoke-align', str(voice),
                    '--smoke-report', str(speech_report)], check=True, timeout=600)
    if not json.loads(speech_report.read_text(encoding='utf-8')).get('success'):
        raise RuntimeError('Frozen speech alignment test failed')
    artifacts = ROOT / 'dist'
    artifacts.mkdir(exist_ok=True)
    target = artifacts / f'{PRODUCT}-v{VERSION}-Windows-x64.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(portable.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(portable).as_posix())
    with target.open('rb') as file:
        checksum = hashlib.file_digest(file, 'sha256').hexdigest()
    (artifacts / 'SHA256SUMS.txt').write_text(f'{checksum}  {target.name}\n', encoding='utf-8')
    shutil.copy2(report, artifacts / 'frozen-smoke.json')
    shutil.copy2(speech_report, artifacts / 'frozen-alignment.json')
    print(f'Built {target.name}: {target.stat().st_size / 1024 ** 2:.1f} MB')


if __name__ == '__main__':
    main()
