import io
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QColor
    from recorder import CanvasRecorder, resolve_ffmpeg
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'Qt required')
class PostRecorderTests(unittest.TestCase):
    def test_audio_queue_gaps_are_silence_in_both_tracks(self):
        recorder = CanvasRecorder('unused.mp4', 64, 64, 10)
        raw, processed = io.BytesIO(), io.BytesIO()
        recorder._raw_audio_file = wave.open(raw, 'wb')
        recorder._audio_file = wave.open(processed, 'wb')
        for output in (recorder._raw_audio_file, recorder._audio_file):
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
        recorder._audio_offset_samples = 20
        recorder._audio_queue.put((100, struct.pack('<10h', *([1000] * 10))))
        recorder._audio_capture_stopped.set()
        with patch('recorder.process_chain', side_effect=lambda data, *args: (data, 1000)):
            recorder._process_audio()
        recorder._raw_audio_file.close()
        recorder._audio_file.close()
        for content in (raw, processed):
            content.seek(0)
            with wave.open(content) as audio:
                samples = struct.unpack('<130h', audio.readframes(130))
            self.assertEqual(samples[:120], (0,) * 120)
            self.assertEqual(samples[120:], (1000,) * 10)

    def test_post_recording_writes_clean_video_and_reopenable_assets(self):
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            self.skipTest('FFmpeg required')

        class Stream:
            def __init__(self, callback, **kwargs):
                self.callback = callback
                self.stop_event = threading.Event()

            def start(self):
                def run():
                    while not self.stop_event.is_set():
                        now = time.monotonic()
                        data = struct.pack('<480h', *(round(5000 * math.sin(i * .1)) for i in range(480)))
                        self.callback(data, 480, SimpleNamespace(currentTime=now, inputBufferAdcTime=now), None)
                        self.stop_event.wait(.01)
                self.thread = threading.Thread(target=run)
                self.thread.start()

            def stop(self):
                self.stop_event.set()
                self.thread.join(2)

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recorder = CanvasRecorder(root / 'test.mp4', 64, 64, 10, ffmpeg_path=ffmpeg,
                session={'groups': ['Hello'], 'slides': [{}], 'language': 'en'})
            image = QImage(64, 64, QImage.Format.Format_RGBA8888)
            image.fill(QColor('blue'))
            foreground = QImage(64, 64, QImage.Format.Format_RGBA8888)
            foreground.fill(Qt.GlobalColor.transparent)
            with patch.dict(sys.modules, {'sounddevice': SimpleNamespace(RawInputStream=Stream)}):
                recorder.start()
                try:
                    for _ in range(8):
                        recorder.push_frame(image, foreground)
                        time.sleep(.1)
                    recorder.finish_async()
                    deadline = time.monotonic() + 30
                    while not recorder.finished:
                        if time.monotonic() > deadline:
                            self.fail('Recording did not finish')
                        time.sleep(.02)
                    self.assertFalse(recorder.error, recorder.error)
                    self.assertTrue(recorder.path.is_file())
                    session = json.loads(recorder.session_path.read_text())
                    self.assertGreater(session['duration'], .5)
                    for key in ('video', 'voice', 'background', 'foreground'):
                        self.assertTrue((recorder.session_path.parent / session[key]).is_file(), key)
                    with wave.open(str(recorder.session_path.parent / session['voice'])) as voice:
                        self.assertGreater(voice.getnframes() / voice.getframerate(), .5)
                finally:
                    if not recorder.finished:
                        recorder.abort()


if __name__ == '__main__':
    unittest.main()
