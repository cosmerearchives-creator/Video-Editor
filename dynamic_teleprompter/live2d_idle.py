"""Camera-free Live2D animation scheduler and non-blocking microphone level tap.

All Cubism API calls remain on the pose worker thread. Audio callbacks only update
one small shared meter, never render or access Qt widgets.
"""
import math
import random
import struct
import threading
import time

DEFAULT_ANIMATION = {
    'blink_enabled': True,
    'blink_interval': 4.0,
    'breath_enabled': True,
    'breath_period': 4.0,
    'breath_strength': 0.65,
    'lip_enabled': True,
    'lip_sensitivity': 15.0,
    'lip_threshold': 0.018,
}


def config(layer):
    saved = layer.get('idle_animation', {})
    return {**DEFAULT_ANIMATION, **(saved if isinstance(saved, dict) else {})}


class AnimationState:
    def __init__(self, now=None, seed=None):
        self.random = random.Random(seed)
        self.started = time.monotonic() if now is None else now
        self.next_blink = self.started + 1.5
        self.blink_start = None
        self.mouth = 0.0
        self.last_t = self.started
        self.double_blink = False

    def values(self, cfg, audio, now=None):
        now = time.monotonic() if now is None else now
        dt = max(0.0, min(0.2, now-self.last_t))
        self.last_t = now
        results = {}
        if cfg['blink_enabled']:
            if self.blink_start is None and now >= self.next_blink:
                self.blink_start = now
                self.double_blink = self.random.random() < 0.12
            if self.blink_start is not None:
                elapsed = now - self.blink_start
                duration = 0.17
                if elapsed < duration:
                    # close quickly, then reopen
                    phase = elapsed / duration
                    eye = 1.0 - (phase / 0.38 if phase < 0.38 else (1 - phase)/0.62)
                    eye = max(0.0, min(1.0, eye))
                else:
                    eye = 1.0
                    self.blink_start = None
                    if self.double_blink:
                        self.next_blink = now + 0.16
                        self.double_blink = False
                    else:
                        avg = max(1.0, float(cfg['blink_interval']))
                        self.next_blink = now + self.random.uniform(avg*0.55, avg*1.5)
                results['ParamEyeLOpen'] = eye
                results['ParamEyeROpen'] = eye
            else:
                results['ParamEyeLOpen'] = 1.0
                results['ParamEyeROpen'] = 1.0
        if cfg['breath_enabled']:
            period = max(1.0, float(cfg['breath_period']))
            strength = max(0.0, min(1.0, float(cfg['breath_strength'])))
            results['ParamBreath'] = (0.5 + 0.5 * math.sin((now - self.started)*2*math.pi/period)) * strength
        if cfg['lip_enabled']:
            threshold = max(0.0, float(cfg['lip_threshold']))
            target = max(0.0, min(1.0, (audio - threshold) * float(cfg['lip_sensitivity'])))
            tau = 0.045 if target > self.mouth else 0.13
            factor = 1-math.exp(-dt/tau) if dt else 0
            self.mouth += (target - self.mouth) * factor
            results['ParamMouthOpenY'] = max(0.0, min(1.0, self.mouth))
        return results


_states = {}

def animation_values(layer, audio, now=None):
    key = id(layer)
    if key not in _states:
        _states[key] = AnimationState(now=now)
    return _states[key].values(config(layer), audio, now)


class MicrophoneLevel:
    """Shares recorder PCM, or acquires a separate input stream only when idle."""
    def __init__(self):
        self._lock = threading.Lock()
        self._level = 0.0
        self._last_input = 0.0
        self._stream = None
        self.error = ''

    def feed(self, pcm):
        if not pcm:
            return
        # RMS in 16-bit PCM; callback avoids allocation of NumPy arrays.
        n = len(pcm) // 2
        if not n:
            return
        samples = struct.unpack_from('<' + 'h'*n, pcm)
        level = math.sqrt(sum(v*v for v in samples) / n) / 32768.0
        with self._lock:
            self._level = max(0.0, min(1.0, level))
            self._last_input = time.monotonic()

    def level(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_input
            return self._level if elapsed < 0.20 else self._level * math.exp(-max(0, elapsed-0.20)*12)

    def stop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def start(self):
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
            stream = sd.RawInputStream(samplerate=48000, channels=1, dtype='int16', blocksize=1024,
                                       callback=lambda data, frames, ti, status: self.feed(bytes(data)))
            stream.start()
            self._stream = stream
            self.error = ''
        except Exception as exc:
            self.error = str(exc)
            self.stop()


microphone = MicrophoneLevel()
