"""Cubism model-package inventory/validation (not a model renderer).

A Live2D model is opened through its .model3.json manifest. The Cubism
Native SDK is required to interpret .moc3 and render animated meshes.
"""
import json
from pathlib import Path


class Live2DModelError(ValueError):
    pass


def inspect_model(manifest_path):
    """Return a JSON-compatible report; raise for unusable model packages."""
    manifest = Path(manifest_path).expanduser().resolve()
    if not manifest.name.lower().endswith('.model3.json'):
        raise Live2DModelError('Select a .model3.json file, not a standalone .moc3 file.')
    if not manifest.is_file():
        raise Live2DModelError(f'Model manifest not found: {manifest}')
    try:
        model = json.loads(manifest.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Live2DModelError(f'Could not read model manifest: {exc}') from exc
    if not isinstance(model, dict) or not isinstance(model.get('FileReferences'), dict):
        raise Live2DModelError('Model manifest has no valid FileReferences object.')
    refs = model['FileReferences']
    root = manifest.parent
    resources = []
    errors = []
    warnings = []

    def register(kind, relative, required=False):
        if not isinstance(relative, str) or not relative.strip():
            (errors if required else warnings).append(f'{kind}: invalid resource path')
            return
        # Prevent absolute and traversal paths even if a malformed manifest supplies them.
        rel = Path(relative.replace('\\', '/'))
        candidate = (root / rel).resolve()
        if rel.is_absolute() or candidate != root and root not in candidate.parents or ':' in relative:
            errors.append(f'{kind}: unsafe resource path: {relative}')
            return
        exists = candidate.is_file()
        resources.append({'kind': kind, 'path': relative, 'exists': exists})
        if not exists:
            (errors if required else warnings).append(f'{kind}: missing {relative}')

    register('Cubism moc3', refs.get('Moc'), required=True)
    textures = refs.get('Textures')
    if not isinstance(textures, list) or not textures:
        errors.append('No texture files listed in FileReferences.Textures')
    else:
        for path in textures:
            register('Texture', path, required=True)
    for key in ('Physics', 'Pose', 'UserData', 'DisplayInfo'):
        if refs.get(key):
            register(key, refs[key])
    expressions = refs.get('Expressions', [])
    if isinstance(expressions, list):
        for entry in expressions:
            if isinstance(entry, dict):
                register('Expression', entry.get('File'))
    motions = refs.get('Motions', {})
    if isinstance(motions, dict):
        for group, entries in motions.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    register('Motion: ' + str(group), entry.get('File'))
                    if entry.get('Sound'):
                        register('Motion sound', entry['Sound'])
    return {
        'manifest': str(manifest),
        'name': manifest.name[:-len('.model3.json')],
        'model_version': model.get('Version'),
        'resources': resources,
        'errors': errors,
        'warnings': warnings,
        'texture_count': sum(x['kind'] == 'Texture' for x in resources),
        'motion_count': sum(x['kind'].startswith('Motion:') for x in resources),
        'expression_count': sum(x['kind'] == 'Expression' for x in resources),
        'valid': not errors,
    }
