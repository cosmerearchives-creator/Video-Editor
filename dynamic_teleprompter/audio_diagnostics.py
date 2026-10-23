"""Low-overhead audio DSP timing diagnostics. No Qt dependencies.
Values represent processing/IPC timing, not acoustic round-trip latency.
"""
import threading
from collections import deque
from time import perf_counter

_lock = threading.Lock()
_stats = {}


def observe(label, elapsed_ms, block_ms=None):
    if elapsed_ms < 0:
        return
    with _lock:
        entry = _stats.setdefault(label, {'samples': deque(maxlen=200), 'above_budget': 0, 'blocks': 0})
        entry['samples'].append(float(elapsed_ms))
        entry['blocks'] += 1
        if block_ms is not None and elapsed_ms > block_ms:
            entry['above_budget'] += 1


def snapshot():
    with _lock:
        return {key: {
            'last_ms': round(entry['samples'][-1], 2) if entry['samples'] else 0.0,
            'mean_ms': round(sum(entry['samples']) / len(entry['samples']), 2) if entry['samples'] else 0.0,
            'max_ms': round(max(entry['samples']), 2) if entry['samples'] else 0.0,
            'blocks': entry['blocks'],
            'above_budget': entry['above_budget'],
        } for key, entry in _stats.items()}


def reset():
    with _lock:
        _stats.clear()
