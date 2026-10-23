"""Ordered microphone effects shared by recording and live monitoring.

Built-in effects apply on each block; third-party plugins are hosted in a child process.
Native gain/gate remain available through the existing optional C++ DLL.
"""
import numpy as np
import threading
import math
import time
from audio_diagnostics import observe
from pathlib import Path

_thread_state = threading.local()
_plugin_errors = {}
_error_lock = threading.Lock()

def vst3_backend_available():
    try:
        import pedalboard
        return True
    except ImportError:
        return False

def plugin_errors():
    with _error_lock:
        return dict(_plugin_errors)

def inspect_vst3_parameters(path, plugin_name=''):
    """Probe a VST3 outside the main process, including its binary loader."""
    from vst3_isolation import inspect
    return inspect(path, plugin_name)


def _process_vst3(data, entry):
    """Run plugin processing in a disposable spawned child process."""
    from vst3_isolation import process
    path = str(entry.get('path', ''))
    result = process(path, str(entry.get('plugin_name', '')),
                     dict(entry.get('parameters', {})), data)
    with _error_lock:
        _plugin_errors.pop(path, None)
    return result

from native_engine import process_audio_s16

EFFECT_NAMES = {'suppress': 'Noise Suppression (RNNoise)', 'eq': 'Parametric EQ', 'gain': 'Gain', 'gate': 'Noise Gate', 'clip': 'Soft Clip'}
DEFAULT_CHAIN = [dict(type='suppress', enabled=False, strength=80), dict(type='eq', enabled=False), dict(type='gain', enabled=True), dict(type='gate', enabled=False),
                 dict(type='clip', enabled=False)]


def normalize_chain(chain):
    """Preserve ordered VST3 instances and one copy of each built-in processor."""
    result = []
    used = set()
    for entry in chain or []:
        if not isinstance(entry, dict):
            continue
        kind = entry.get('type')
        if kind == 'vst3':
            path = str(entry.get('path', '')).strip()
            if path:
                result.append(dict(type='vst3', path=path, plugin_name=str(entry.get('plugin_name', '')),
                                   enabled=bool(entry.get('enabled', True)),
                                   parameters=dict(entry.get('parameters', {})) if isinstance(entry.get('parameters', {}), dict) else {}))
            continue
        if kind in EFFECT_NAMES and kind not in used:
            clean = dict(type=kind, enabled=bool(entry.get('enabled', True)))
            if kind == 'eq':
                from mic_dsp import bands_value
                clean['bands'] = bands_value(entry.get('bands'))
            if kind == 'suppress':
                clean['strength'] = max(0, min(100, float(entry.get('strength', 80))))
            if kind == 'gate':
                clean.update(attack=max(.1, min(100, float(entry.get('attack', 5)))),
                             release=max(10, min(2000, float(entry.get('release', 150)))))
            result.append(clean)
            used.add(kind)
    for entry in DEFAULT_CHAIN:
        if entry['type'] not in used:
            result.append(dict(entry))
    return result


def process_chain(data, chain, gain=100, gate_db=-45, clip_drive=150):
    """Process signed 16-bit mono PCM; return bytes and output peak.

    DSP is executed on workers, never inside a sounddevice callback.
    """
    started = time.perf_counter()
    block_budget_ms = len(data) / 2 / 48000 * 1000
    state = getattr(_thread_state, 'processors', None)
    if state is None:
        state = _thread_state.processors = {}
    for entry in normalize_chain(chain):
        kind = entry['type']
        if not entry['enabled']:
            state.pop(kind, None)
            state.pop(kind + '_key', None)
            continue
        if kind == 'gain':
            data, _ = process_audio_s16(data, int(gain), 0)
        elif kind in ('gate', 'suppress', 'eq'):
            from mic_dsp import process_builtin
            samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768
            try:
                samples = process_builtin(samples, entry, state, gate_db)
                data = np.clip(np.rint(samples * 32768), -32768, 32767).astype('<i2').tobytes()
                with _error_lock:
                    _plugin_errors.pop(kind, None)
            except Exception as exc:
                with _error_lock:
                    _plugin_errors[kind] = str(exc)
        elif kind == 'vst3':
            try:
                data = _process_vst3(data, entry)
            except Exception as exc:
                # Bypass the faulty plugin; capture its status for the interface instead of stopping recording.
                with _error_lock:
                    _plugin_errors[str(entry.get('path', ''))] = str(exc)
        elif kind == 'clip':
            if not data:
                continue
            samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0
            drive = max(1.0, float(clip_drive) / 100.0)
            samples = np.tanh(samples * drive) / np.tanh(drive)
            data = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype('<i2').tobytes()
    peak = int(np.max(np.abs(np.frombuffer(data, dtype='<i2').astype(np.int32)))) if data else 0
    observe('audio_chain', (time.perf_counter() - started) * 1000, block_budget_ms)
    return data, peak
