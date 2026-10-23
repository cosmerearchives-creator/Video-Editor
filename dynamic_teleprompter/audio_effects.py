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

EFFECT_NAMES = {'gain': 'Gain', 'gate': 'Noise Gate', 'clip': 'Soft Clip'}
DEFAULT_CHAIN = [dict(type='gain', enabled=True), dict(type='gate', enabled=False),
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
            result.append(dict(type=kind, enabled=bool(entry.get('enabled', True))))
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
    for entry in normalize_chain(chain):
        if not entry['enabled']:
            continue
        kind = entry['type']
        if kind == 'gain':
            data, _ = process_audio_s16(data, int(gain), 0)
        elif kind == 'gate':
            threshold = round(32767 * 10 ** (int(gate_db) / 20))
            data, _ = process_audio_s16(data, 100, threshold)
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
