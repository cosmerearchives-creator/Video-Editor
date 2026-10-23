"""Streaming microphone processors. Each DSP worker owns its own state."""
import ctypes
from pathlib import Path
import threading
import numpy as np

RATE = 48000
MAX_EQ_BANDS = 32
EQ_TYPES = ('Peak', 'Low shelf', 'High shelf', 'High pass', 'Low pass')
DEFAULT_BANDS = [dict(kind='High pass', frequency=80., gain=0., q=.707, enabled=False),
                 *[dict(kind='Peak', frequency=f, gain=0., q=1., enabled=True)
                   for f in (150., 500., 1500., 4000.)],
                 dict(kind='High shelf', frequency=8000., gain=0., q=.707, enabled=True)]
_init_lock = threading.Lock()


def bands_value(bands):
    result = []
    for band in (bands if isinstance(bands, list) else DEFAULT_BANDS)[:MAX_EQ_BANDS]:
        if not isinstance(band, dict):
            continue
        result.append(dict(kind=band.get('kind') if band.get('kind') in EQ_TYPES else 'Peak',
            frequency=max(20., min(20000., float(band.get('frequency', 1000)))),
            gain=max(-24., min(24., float(band.get('gain', 0)))),
            q=max(.1, min(18., float(band.get('q', 1)))), enabled=bool(band.get('enabled', True))))
    return result


def make_eq(bands):
    from pedalboard import Pedalboard, PeakFilter, LowShelfFilter, HighShelfFilter, HighpassFilter, LowpassFilter
    filters = []
    for b in bands_value(bands):
        if not b['enabled']:
            continue
        kind, f, gain, q = b['kind'], b['frequency'], b['gain'], b['q']
        if kind in ('High pass', 'Low pass'):
            filters.append((HighpassFilter if kind == 'High pass' else LowpassFilter)(cutoff_frequency_hz=f))
        else:
            cls = {'Peak': PeakFilter, 'Low shelf': LowShelfFilter, 'High shelf': HighShelfFilter}[kind]
            filters.append(cls(cutoff_frequency_hz=f, gain_db=gain, q=q))
    return Pedalboard(filters)


def eq_response(bands):
    # Measure the actual processor so the graph also matches shelf/pass filters.
    size = 16384
    impulse = np.zeros(size, dtype=np.float32)
    impulse[0] = 1
    spectrum = np.fft.rfft(make_eq(bands)(impulse, RATE))
    return np.fft.rfftfreq(size, 1 / RATE), 20 * np.log10(np.maximum(1e-6, np.abs(spectrum)))


class NoiseSuppressor:
    """RNNoise frames adapted to arbitrary callback sizes with a fixed 10ms FIFO.

    RNNoise itself adds 10ms lookahead; dry audio is delayed to match wet audio.
    No sample insertion/removal when strength changes. One instance per worker.
    """
    def __init__(self):
        dll = Path(__file__).resolve().parent / 'native' / 'rnnoise.dll'
        self.lib = ctypes.CDLL(str(dll))
        self.lib.rnnoise_create.restype = ctypes.c_void_p
        self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        pointer = ctypes.POINTER(ctypes.c_float)
        self.lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, pointer, pointer]
        self.lib.rnnoise_process_frame.restype = ctypes.c_float
        # v0.1 shares immutable initialized FFT tables; serialize first use.
        with _init_lock:
            self.state = self.lib.rnnoise_create()
            if not self.state:
                raise MemoryError('RNNoise state allocation failed')
            silent = np.zeros(480, dtype=np.float32)
            self._frame(silent)
        self.pending = np.empty(0, dtype=np.float32)
        self.wet = np.zeros(480, dtype=np.float32)
        self.dry = np.zeros(960, dtype=np.float32)

    def _frame(self, frame):
        output = np.empty(480, dtype=np.float32)
        ptr = ctypes.POINTER(ctypes.c_float)
        self.lib.rnnoise_process_frame(self.state, output.ctypes.data_as(ptr), frame.ctypes.data_as(ptr))
        return output

    def process(self, samples, strength):
        self.pending = np.concatenate((self.pending, samples * 32768))
        while len(self.pending) >= 480:
            self.wet = np.concatenate((self.wet, self._frame(np.ascontiguousarray(self.pending[:480])) / 32768))
            self.pending = self.pending[480:]
        count = len(samples)
        self.dry = np.concatenate((self.dry, samples))
        output = self.wet[:count] * strength + self.dry[:count] * (1 - strength)
        self.wet, self.dry = self.wet[count:], self.dry[count:]
        return output

    def __del__(self):
        if getattr(self, 'state', None):
            self.lib.rnnoise_destroy(self.state)
            self.state = None


def process_builtin(samples, entry, state, gate_db):
    import json
    kind = entry['type']
    if kind == 'suppress':
        processor = state.setdefault('suppress', None)
        if processor is None:
            processor = state['suppress'] = NoiseSuppressor()
        return processor.process(samples, max(0, min(100, float(entry.get('strength', 80)))) / 100)
    if kind == 'eq':
        bands = bands_value(entry.get('bands'))
        key = json.dumps(bands, sort_keys=True)
        if state.get('eq_key') != key:
            state['eq'] = make_eq(bands)
            state['eq_key'] = key
        return state['eq'](samples, RATE, reset=False).reshape(-1)
    if kind == 'gate':
        from pedalboard import NoiseGate
        key = (gate_db, float(entry.get('attack', 5)), float(entry.get('release', 150)))
        if state.get('gate_key') != key:
            state['gate'] = NoiseGate(threshold_db=key[0], ratio=10, attack_ms=key[1], release_ms=key[2])
            state['gate_key'] = key
        return state['gate'](samples, RATE, reset=False).reshape(-1)
    return samples
