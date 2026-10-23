"""Public GitHub releases and side-by-side, checksum-verified portable updates."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import urllib.request
import zipfile

from release_info import PRODUCT, REPOSITORY, VERSION

MAX_DOWNLOAD = 2 * 1024 ** 3
MAX_EXPANDED = 6 * 1024 ** 3


def version_key(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)(\d+))?', value)
    if not match:
        raise ValueError('Unsupported version: ' + value)
    major, minor, patch, channel, number = match.groups()
    return int(major), int(minor), int(patch), {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[channel], int(number or 0)


def installation_root():
    value = os.environ.get('COSMERE_CAM_INSTALL_ROOT')
    return Path(value).resolve() if value else None


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temp, path)


def _request(url):
    return urllib.request.Request(url, headers={'User-Agent': 'Cosmere-Cam/' + VERSION,
                                                'Accept': 'application/vnd.github+json'})


def select_release(releases, current=VERSION):
    candidates = []
    allow_preview = '-' in current
    for release in releases:
        if release.get('draft') or (release.get('prerelease') and not allow_preview):
            continue
        tag = release.get('tag_name', '')
        try:
            key = version_key(tag)
        except ValueError:
            continue
        if key <= version_key(current):
            continue
        expected = f'{PRODUCT}-{tag}-Windows-x64.zip'
        for asset in release.get('assets', []):
            digest = asset.get('digest', '') or ''
            url = asset.get('browser_download_url', '')
            prefix = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
            if (asset.get('name') == expected and url == prefix + expected
                    and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest)):
                candidates.append((key, {'version': tag.removeprefix('v'), 'tag': tag,
                    'url': url, 'sha256': digest[7:].lower(), 'size': asset.get('size', 0),
                    'notes': release.get('body', ''), 'page': release.get('html_url', '')}))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None


def check_for_update():
    url = f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=30'
    with urllib.request.urlopen(_request(url), timeout=20) as response:
        raw = response.read(2 * 1024 ** 2 + 1)
    if len(raw) > 2 * 1024 ** 2:
        raise ValueError('Release metadata exceeds size limit')
    return select_release(json.loads(raw))


def extract_version(archive_path, root, version):
    """Extract only the expected new version, never the running app or user data."""
    version_key(version)
    root = Path(root).resolve()
    versions = root / 'app' / 'versions'
    versions.mkdir(parents=True, exist_ok=True)
    if versions.is_symlink() or (root / 'app').is_symlink():
        raise ValueError('Install directories must not be symbolic links')
    target = versions / version
    if target.exists():
        raise ValueError('That version is already installed')
    prefix = f'app/versions/{version}/'
    with tempfile.TemporaryDirectory(prefix='.update-', dir=versions) as temp:
        staging = Path(temp) / 'version'
        staging.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_EXPANDED:
                raise ValueError('Expanded update exceeds size limit')
            seen = set()
            for item in archive.infolist():
                name = item.filename
                parts = PurePosixPath(name).parts
                if ('\\' in name or ':' in name or name.startswith('/') or '..' in parts
                        or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError('Unsafe path in update package')
                if not name.startswith(prefix) or item.is_dir():
                    continue
                relative = name[len(prefix):]
                if not relative:
                    continue
                if relative.casefold() in seen:
                    raise ValueError('Duplicate path in update package')
                seen.add(relative.casefold())
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, destination.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
        info = json.loads((staging / 'build.json').read_text(encoding='utf-8'))
        if info != {'product': PRODUCT, 'version': version, 'launcher_protocol': 1}:
            raise ValueError('Update identity or launcher protocol mismatch')
        if not (staging / 'CosmereCam.exe').is_file():
            raise ValueError('Update executable is missing')
        os.replace(staging, target)
    return target


def install_update(release, root):
    root = Path(root).resolve()
    if not (root / 'Cosmere Cam.exe').is_file():
        raise ValueError('Use the portable release launcher before installing updates')
    root_app = root / 'app'
    with tempfile.TemporaryDirectory(prefix='cosmere-update-') as temp:
        archive = Path(temp) / 'update.zip'
        checksum, size = hashlib.sha256(), 0
        with urllib.request.urlopen(_request(release['url']), timeout=60) as response, archive.open('wb') as out:
            while True:
                chunk = response.read(1024 ** 2)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError('Update exceeds download size limit')
                checksum.update(chunk)
                out.write(chunk)
        if checksum.hexdigest() != release['sha256']:
            raise ValueError('Update checksum verification failed')
        if release.get('size') and size != release['size']:
            raise ValueError('Update download is incomplete')
        extract_version(archive, root, release['version'])
    pointer = root_app / 'current.json'
    old = json.loads(pointer.read_text(encoding='utf-8'))
    atomic_json(root_app / 'previous.json', old)
    atomic_json(pointer, {'version': release['version']})


def rollback(root):
    root = Path(root)
    previous = json.loads((root / 'app' / 'previous.json').read_text(encoding='utf-8'))
    version_key(previous['version'])
    if not (root / 'app' / 'versions' / previous['version'] / 'CosmereCam.exe').is_file():
        raise ValueError('Previous version is unavailable')
    current = json.loads((root / 'app' / 'current.json').read_text(encoding='utf-8'))
    atomic_json(root / 'app' / 'current.json', previous)
    atomic_json(root / 'app' / 'previous.json', current)
