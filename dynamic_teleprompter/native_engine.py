"""Optional native C++ backend. Safe NumPy fallback remains available.

Compile native/engine.cpp into native/dt_engine.dll (Windows), or .so (Linux).
Chroma key and color correction are native when the optional library is present.
"""
import ctypes
import os
import sys
from pathlib import Path

_BACKEND = None
_BACKEND_ERROR = None


def _load_backend():
    global _BACKEND, _BACKEND_ERROR
    extension = '.dll' if os.name == 'nt' else ('.dylib' if sys.platform == 'darwin' else '.so')
    dll = Path(__file__).resolve().parent / 'native' / ('dt_engine' + extension)
    if not dll.is_file():
        _BACKEND_ERROR = 'Native engine not compiled; NumPy fallback active'
        return
    try:
        lib = ctypes.CDLL(str(dll))
        lib.dt_engine_abi_version.argtypes = []
        lib.dt_engine_abi_version.restype = ctypes.c_int
        if lib.dt_engine_abi_version() != 1:
            raise RuntimeError('Native engine ABI mismatch')
        lib.dt_chroma_key_bgra.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 7
        lib.dt_chroma_key_bgra.restype = ctypes.c_int
        lib.dt_color_adjust_bgra.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 6
        lib.dt_color_adjust_bgra.restype = ctypes.c_int
        if hasattr(lib, 'dt_audio_process_s16'):
            lib.dt_audio_process_s16.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_int,
                                                ctypes.POINTER(ctypes.c_int)]
            lib.dt_audio_process_s16.restype = ctypes.c_int
        _BACKEND = lib
    except (OSError, AttributeError, RuntimeError) as error:
        _BACKEND_ERROR = str(error)


_load_backend()


def backend_status():
    return 'C++ chroma-key + color-adjustment engine active' if _BACKEND is not None else (_BACKEND_ERROR or 'NumPy fallback active')


def chroma_key_inplace(pixels, key_bgr, tolerance):
    """Modify a contiguous HxWx4 uint8 NumPy array. False => caller uses fallback.

    Never retain the data pointer across calls; ctypes executes synchronously.
    """
    if _BACKEND is None or pixels.ndim != 3 or pixels.shape[2] != 4 or not pixels.flags.c_contiguous:
        return False
    h, w = pixels.shape[:2]
    try:
        status = _BACKEND.dt_chroma_key_bgra(
            ctypes.c_void_p(pixels.ctypes.data), int(w), int(h), int(pixels.strides[0]),
            int(key_bgr[0]), int(key_bgr[1]), int(key_bgr[2]), int(tolerance))
        return status == 0
    except (TypeError, ValueError):
        return False


def color_adjust_inplace(pixels, brightness, contrast, saturation):
    """Apply native BGRA color adjustments if DLL is available, else return False."""
    if _BACKEND is None or pixels.ndim != 3 or pixels.shape[2] != 4 or not pixels.flags.c_contiguous:
        return False
    h, w = pixels.shape[:2]
    if not (-100 <= brightness <= 100 and -100 <= contrast <= 100 and 0 <= saturation <= 200):
        return False
    try:
        status = _BACKEND.dt_color_adjust_bgra(ctypes.c_void_p(pixels.ctypes.data),
                    int(w), int(h), int(pixels.strides[0]),
                    int(brightness), int(contrast), int(saturation))
        return status == 0
    except (TypeError, ValueError):
        return False


def audio_backend_status():
    if _BACKEND is not None and hasattr(_BACKEND, 'dt_audio_process_s16'):
        return 'C++ audio DSP active'
    return 'Python audio DSP fallback active (build_native_windows.bat to enable C++)'


def process_audio_s16(audio_bytes, gain_percent=100, gate_threshold=0):
    """Process interleaved mono s16 LE PCM; return (processed_bytes, peak_amplitude).

    Both the native and fallback paths use integer truncation toward zero.
    Filtering is performed on the recorder's worker thread, never in PortAudio's callback.
    """
    from array import array
    if len(audio_bytes) % 2:
        raise ValueError('16-bit PCM must contain an even number of bytes')
    gain = int(gain_percent)
    gate = int(gate_threshold)
    if not 0 <= gain <= 400 or not 0 <= gate <= 32767:
        raise ValueError('gain or gate is out of range')
    buf = bytearray(audio_bytes)
    peak = ctypes.c_int(0)
    if _BACKEND is not None and hasattr(_BACKEND, 'dt_audio_process_s16'):
        data = (ctypes.c_uint8 * len(buf)).from_buffer(buf)
        result = _BACKEND.dt_audio_process_s16(ctypes.cast(data, ctypes.c_void_p),
                     len(buf) // 2, gain, gate, ctypes.byref(peak))
        if result == 0:
            return bytes(buf), peak.value
    values = array('h')
    values.frombytes(buf)
    import sys as _sys
    if _sys.byteorder != 'little':
        values.byteswap()
    max_abs = 0
    for i, sample in enumerate(values):
        v = 0 if abs(sample) < gate else (abs(sample) * gain // 100) * (1 if sample >= 0 else -1)
        v = max(-32768, min(32767, v))
        values[i] = v
        max_abs = max(max_abs, abs(v))
    if _sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes(), max_abs
