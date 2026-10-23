"""Opt-in low-latency microphone monitoring; never performs DSP on PortAudio callback threads.

When recording, processed audio comes from CanvasRecorder.audio_listener. Otherwise an
independent input stream feeds a bounded DSP queue. Output has its own device selector.
"""
import queue
import threading
from audio_diagnostics import snapshot
from native_engine import process_audio_s16
from audio_effects import process_chain
from live2d_idle import microphone as lip_microphone


class AudioMonitor:
    RATE = 48000
    BLOCK = 512

    def __init__(self, settings, volume, device=None):
        self.settings = settings   # callable returning (gain percent, gate enabled, gate dBFS)
        self.volume = volume     # independent monitor playback gain
        self.device = device
        self.input_stream = None
        self.output_stream = None
        self._source = queue.Queue(maxsize=16)
        self._playback = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._worker = None
        self.external = False
        self.dropped = 0
        self.underruns = 0
        self.input_overflows = 0
        self.error = None

    def start(self, external=False):
        import sounddevice as sd
        self.external = external
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        try:
            self.output_stream = sd.RawOutputStream(
                samplerate=self.RATE, channels=1, dtype='int16', blocksize=self.BLOCK,
                latency='low', device=self.device, callback=self._output_callback)
            self.output_stream.start()
            if not external:
                self.input_stream = sd.RawInputStream(
                    samplerate=self.RATE, channels=1, dtype='int16', blocksize=self.BLOCK,
                    latency='low', callback=self._input_callback)
                self.input_stream.start()
            self._worker.start()
        except Exception:
            self.stop()
            raise

    def _offer(self, q, data):
        try:
            q.put_nowait(data)
        except queue.Full:
            self.dropped += 1
            try:
                q.get_nowait()  # favor the newest audio; don't grow latency
                q.put_nowait(data)
            except queue.Empty:
                pass
            except queue.Full:
                pass

    def _input_callback(self, indata, frames, time_info, status):
        if status:
            self.input_overflows += 1
        if not self._stop.is_set():
            self._offer(self._source, (bytes(indata), False))

    def receive_processed(self, data):
        """Recorder worker supplies already-filtered audio; avoids double processing."""
        if not self._stop.is_set():
            self._offer(self._source, (data, True))

    def _run(self):
        while not self._stop.is_set():
            try:
                raw, processed = self._source.get(timeout=0.05)
                if not processed:
                    lip_microphone.feed(raw)
                    chain, gain, gate_db, drive = self.settings()
                    raw, _ = process_chain(raw, chain, gain, gate_db, drive)
                volume = int(self.volume())
                if volume != 100:
                    raw, _ = process_audio_s16(raw, volume, 0)
                self._offer(self._playback, raw)
            except queue.Empty:
                continue
            except Exception as exc:
                self.error = str(exc)
                self._stop.set()

    def _output_callback(self, outdata, frames, time_info, status):
        try:
            data = self._playback.get_nowait()
        except queue.Empty:
            self.underruns += 1
            data = b''
        requested = len(outdata)
        outdata[:] = (data[:requested] + bytes(max(0, requested - len(data))))

    def diagnostics(self):
        return {
            'dropped_blocks': self.dropped,
            'output_underruns': self.underruns,
            'input_status_events': self.input_overflows,
            'input_queue': self._source.qsize(),
            'output_queue': self._playback.qsize(),
            'block_ms': round(self.BLOCK / self.RATE * 1000, 2),
        }

    def stop(self):
        self._stop.set()
        for key in ('input_stream', 'output_stream'):
            stream = getattr(self, key)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
                setattr(self, key, None)
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=1)
        self._worker = None
