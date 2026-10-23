"""Experimental FFmpeg canvas recorder. Frame rendering must run on the Qt GUI thread.

The encoder runs on a dedicated thread. A bounded frame queue avoids blocking Qt on
FFmpeg input. Sounddevice supplies bounded microphone chunks processed on a worker thread and
written to temporary WAV. The final MP4 is muxed once Stop is pressed; never screen-captures any window.
"""
import queue
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

from PySide6.QtGui import QImage
from native_engine import audio_backend_status
from audio_effects import process_chain, normalize_chain, DEFAULT_CHAIN
from audio_diagnostics import snapshot as audio_diagnostics_snapshot


def resolve_ffmpeg(configured=''):
    """Locate FFmpeg: explicitly configured executable, next to app, bin/, then PATH."""
    app_dir = Path(__file__).resolve().parent
    candidates = [configured, app_dir / 'ffmpeg.exe', app_dir / 'ffmpeg',
                  app_dir / 'bin' / 'ffmpeg.exe', app_dir / 'bin' / 'ffmpeg',
                  shutil.which('ffmpeg')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        pass
    return None


class CanvasRecorder:
    def __init__(self, filename, width, height, fps, microphone=True, ffmpeg_path="", audio_gain=100, audio_gate_db=-60, audio_gate_enabled=False, audio_chain=None, audio_clip_drive=150, session=None):
        self.path = Path(filename)
        self.width, self.height, self.fps = int(width), int(height), int(fps)
        self.microphone = microphone
        self.audio_gain = int(audio_gain)
        self.audio_gate_db = int(audio_gate_db)
        self.audio_gate_enabled = bool(audio_gate_enabled)
        self.audio_chain = normalize_chain(audio_chain if audio_chain is not None else [dict(type="gain", enabled=True), dict(type="gate", enabled=audio_gate_enabled), dict(type="clip", enabled=False)])
        self.audio_clip_drive = int(audio_clip_drive)
        self.audio_dsp_backend = audio_backend_status()
        self.audio_peak = 0
        self.ffmpeg_path = ffmpeg_path
        self.frame_count = 0
        self.late_frames = 0
        self.error = None
        self.stopping = False
        self.finished = False
        self._frames = queue.Queue(maxsize=12)
        self._audio_queue = queue.Queue(maxsize=256)
        self.audio_listener = None
        self.audio_level_listener = None
        self._audio_worker = None
        self.audio_dropped_chunks = 0
        self._temp = None
        self._video = None
        self._audio = None
        self._stream = None
        self._last_bytes = None
        self.capture_calls = 0
        self.capture_total_ms = 0.0
        self.capture_max_ms = 0.0
        self.encoder_writes = 0
        self.encoder_write_total_ms = 0.0
        self.encoder_write_max_ms = 0.0
        self.peak_queue_depth = 0
        self.duplicate_frames = 0
        self.audio_samples = 0
        self.report_path = self.path.with_suffix('.diagnostics.json')
        self.session = session
        self.session_path = self.path.with_suffix('.session.json')
        self._foreground = None
        self._raw_audio_file = None
        self._last_foreground = None
        self._audio_written_samples = 0
        self._audio_offset_samples = None
        self._audio_capture_stopped = threading.Event()
        self.assets_dir = self.path.with_name(self.path.stem + '.assets')
        if session is not None:
            self.session_path = self.assets_dir / 'recording.session.json'
            self.report_path = self.assets_dir / 'diagnostics.json'

    def start(self):
        ffmpeg = resolve_ffmpeg(self.ffmpeg_path)
        self._ffmpeg = ffmpeg
        if not ffmpeg:
            raise RuntimeError('FFmpeg not found. Open Settings → FFmpeg Location to select ffmpeg.exe, or put it beside app.py.')
        self._temp = tempfile.TemporaryDirectory(prefix='dt_record_')
        self._silent = Path(self._temp.name) / 'video.mp4'
        self._wave = Path(self._temp.name) / 'microphone.wav'
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                   '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size',
                   f'{self.width}x{self.height}', '-framerate', str(self.fps),
                   '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'ultrafast',
                   '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(self._silent)]
        self._video = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._started = time.monotonic()
        try:
            if self.session is not None:
                if not self.microphone:
                    raise ValueError('Post-recording alignment requires microphone recording')
                self.assets_dir.mkdir()
                self._foreground_path = self.assets_dir / 'foreground.mkv'
                self._foreground = subprocess.Popen([ffmpeg, '-v', 'error', '-y',
                    '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{self.width}x{self.height}',
                    '-framerate', str(self.fps), '-i', 'pipe:0', '-an', '-c:v', 'ffv1',
                    '-pix_fmt', 'bgra', str(self._foreground_path)], stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self._voice_path = self.assets_dir / 'voice.wav'
                self._raw_audio_file = wave.open(str(self._voice_path), 'wb')
                self._raw_audio_file.setnchannels(1)
                self._raw_audio_file.setsampwidth(2)
                self._raw_audio_file.setframerate(48000)
            if self.microphone:
                import sounddevice as sd
                self._sd = sd
                self._audio_rate = 48000
                self._audio_file = wave.open(str(self._wave), 'wb')
                self._audio_file.setnchannels(1)
                self._audio_file.setsampwidth(2)
                self._audio_file.setframerate(self._audio_rate)
                def callback(indata, frames, time_info, status):
                    start_sample = self.audio_samples
                    self.audio_samples += frames
                    if self._audio_offset_samples is None:
                        # Relate PortAudio's first ADC sample to the same monotonic
                        # origin used for video. Initial silence preserves offset.
                        age = max(0., time_info.currentTime - time_info.inputBufferAdcTime)
                        offset = time.monotonic() - age - self._started
                        self._audio_offset_samples = max(0, round(offset * self._audio_rate))
                    try:
                        self._audio_queue.put_nowait((start_sample, bytes(indata)))
                    except queue.Full:
                        self.audio_dropped_chunks += 1
                self._stream = sd.RawInputStream(samplerate=self._audio_rate, channels=1,
                                                 dtype='int16', callback=callback)
                self._stream.start()
                self._audio_worker = threading.Thread(target=self._process_audio, daemon=True)
                self._audio_worker.start()
        except Exception:
            self.abort()
            raise
        self._writer = threading.Thread(target=self._write_frames, daemon=True)
        self._writer.start()

    def elapsed(self):
        return time.monotonic() - self._started

    def frames_due(self):
        return max(0, int(self.elapsed() * self.fps) + 1 - self.frame_count)

    def note_capture(self, elapsed_ms):
        self.capture_calls += 1
        self.capture_total_ms += elapsed_ms
        self.capture_max_ms = max(self.capture_max_ms, elapsed_ms)

    def push_frame(self, image, foreground=None):
        if self.stopping or self.error:
            return
        # Use one Qt conversion and keep a copy of pixels; no Qt objects enter threads.
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        data = bytes(rgba.constBits())
        foreground_data = None
        if self.session is not None:
            if foreground is None:
                raise ValueError('Foreground frame is required for aligned recording')
            foreground_rgba = foreground.convertToFormat(QImage.Format.Format_RGBA8888)
            foreground_data = bytes(foreground_rgba.constBits())
        due = min(self.frames_due(), self.fps * 2)
        if due > 1:
            self.late_frames += due - 1
        if due > 1 and self._last_bytes:
            self.duplicate_frames += due - 1
        for i in range(due):
            # If capture is behind schedule duplicate the prior frame, preserving time.
            pixels = self._last_bytes if i < due - 1 and self._last_bytes else data
            top = self._last_foreground if i < due - 1 and self._last_foreground else foreground_data
            try:
                self._frames.put_nowait((pixels, top) if self.session is not None else pixels)
            except queue.Full:
                self.error = 'Video encoder cannot keep up; stopped instead of producing a shortened recording.'
                return
            self.frame_count += 1
            self.peak_queue_depth = max(self.peak_queue_depth, self._frames.qsize())
        self._last_bytes = data
        self._last_foreground = foreground_data

    def _write_frames(self):
        try:
            while True:
                pixels = self._frames.get()
                if pixels is None:
                    break
                if self.session is not None:
                    pixels, foreground = pixels
                    self._foreground.stdin.write(foreground)
                write_started = time.perf_counter()
                self._video.stdin.write(pixels)
                elapsed_ms = (time.perf_counter() - write_started) * 1000
                self.encoder_writes += 1
                self.encoder_write_total_ms += elapsed_ms
                self.encoder_write_max_ms = max(self.encoder_write_max_ms, elapsed_ms)
        except Exception as exc:
            self.error = 'Video writing failed: ' + str(exc)

    def _process_audio(self):
        """Continuous audio DSP off the PortAudio callback; playback receives same PCM."""
        while True:
            try:
                start_sample, data = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                if self._audio_capture_stopped.is_set():
                    break
                continue
            gain = int(self.audio_gain)
            gate = bool(self.audio_gate_enabled)
            db = int(self.audio_gate_db)
            try:
                gap = max(0, start_sample + (self._audio_offset_samples or 0) - self._audio_written_samples)
                # Preserve capture-clock gaps instead of compressing the voice
                # timeline if the bounded callback queue drops a chunk.
                if gap:
                    silence = b'\0\0' * gap
                    self._audio_file.writeframesraw(silence)
                    if self._raw_audio_file:
                        self._raw_audio_file.writeframesraw(silence)
                    self._audio_written_samples += gap
                if self._raw_audio_file:
                    self._raw_audio_file.writeframesraw(data)
                self._audio_written_samples += len(data) // 2
                if self.audio_level_listener is not None:
                    self.audio_level_listener(data)
                data, peak = process_chain(data, self.audio_chain, gain, db, self.audio_clip_drive)
                self.audio_peak = max(self.audio_peak, peak)
                self._audio_file.writeframesraw(data)
                listener = self.audio_listener
                if listener is not None:
                    listener(data)
            except Exception as exc:
                self.error = 'Audio processing failed: ' + str(exc)
                break

    def finish_async(self):
        if self.stopping:
            return
        self.stopping = True
        threading.Thread(target=self._finish, daemon=True).start()

    def diagnostic_report(self):
        elapsed = self.elapsed() if hasattr(self, '_started') else 0.0
        audio_seconds = self.audio_samples / self._audio_rate if self.microphone and hasattr(self, '_audio_rate') else None
        return {
            'app_release': '0.5.0-alpha5', 'output': self.path.name,
            'resolution': [self.width, self.height], 'target_fps': self.fps,
            'wall_clock_seconds': round(elapsed, 3),
            'video_duration_seconds': round(self.frame_count / self.fps, 3),
            'audio_dsp_backend': self.audio_dsp_backend,
            'audio_effect_chain': self.audio_chain,
            'audio_soft_clip_drive_percent': self.audio_clip_drive,
            'audio_gain_percent': self.audio_gain,
            'audio_gate_enabled': self.audio_gate_enabled,
            'audio_gate_threshold_dbfs': self.audio_gate_db if self.audio_gate_enabled else None,
            'audio_processed_peak': self.audio_peak,
            'audio_dropped_chunks': self.audio_dropped_chunks,
            'audio_dsp_timings': audio_diagnostics_snapshot(),
            'encoded_frames': self.frame_count, 'late_frames': self.late_frames,
            'duplicated_frames': self.duplicate_frames,
            'capture_calls': self.capture_calls,
            'capture_mean_ms': round(self.capture_total_ms / max(1, self.capture_calls), 2),
            'capture_max_ms': round(self.capture_max_ms, 2),
            'encoder_writes': self.encoder_writes,
            'encoder_write_mean_ms': round(self.encoder_write_total_ms / max(1, self.encoder_writes), 2),
            'encoder_write_max_ms': round(self.encoder_write_max_ms, 2),
            'encoder_queue_peak': self.peak_queue_depth,
            'encoder_queue_capacity': self._frames.maxsize,
            'microphone_duration_seconds': round(audio_seconds, 3) if audio_seconds is not None else None,
            'audio_video_duration_difference_seconds': round(audio_seconds - self.frame_count / self.fps, 3) if audio_seconds is not None else None,
            'recording_error': self.error,
            'notes': 'Timing diagnostics are indicative; not a frame-perfect AV synchronization measurement.'
        }

    def _finish(self):
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
            self._audio_capture_stopped.set()
            if self._audio_worker is not None:
                self._audio_worker.join(timeout=10)
                if self._audio_worker.is_alive():
                    raise RuntimeError('Audio DSP worker timed out')
            if hasattr(self, '_audio_file'):
                remaining = max(0, self.audio_samples + (self._audio_offset_samples or 0) - self._audio_written_samples)
                if remaining:
                    self._audio_file.writeframesraw(b'\0\0' * remaining)
                    if self._raw_audio_file:
                        self._raw_audio_file.writeframesraw(b'\0\0' * remaining)
                self._audio_file.close()
            if self._raw_audio_file:
                self._raw_audio_file.close()
            self._frames.put(None, timeout=15)
            self._writer.join(timeout=60)
            if self._writer.is_alive():
                raise RuntimeError('Encoder frame writer timed out')
            self._video.stdin.close()
            diagnostics = self._video.stderr.read().decode('utf-8', 'replace')
            self._video.stderr.close()
            code = self._video.wait(timeout=60)
            if code != 0:
                raise RuntimeError('FFmpeg video encoding failed: ' + diagnostics[-1500:])
            if self.error:
                raise RuntimeError(self.error)
            if self.frame_count == 0:
                raise RuntimeError('No frames were recorded')
            if self.session is not None:
                self._foreground.stdin.close()
                fg_error = self._foreground.stderr.read().decode('utf-8', 'replace')
                self._foreground.stderr.close()
                if self._foreground.wait(timeout=60):
                    raise RuntimeError('Foreground encoding failed: ' + fg_error[-1500:])
                background = self.assets_dir / 'background.mp4'
                shutil.copyfile(self._silent, background)
                command = [self._ffmpeg, '-v', 'error', '-y', '-i', str(background),
                    '-i', str(self._foreground_path), '-i', str(self._wave),
                    '-filter_complex', '[0:v][1:v]overlay=format=auto[clean]',
                    '-map', '[clean]', '-map', '2:a:0', '-c:v', 'libx264', '-crf', '18',
                    '-preset', 'fast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k',
                    '-t', str(self.frame_count / self.fps), '-movflags', '+faststart', str(self.path)]
                subprocess.run(command, check=True, capture_output=True)
                self.session.update(format='CosmereCamRecording', version=1,
                    video='../' + self.path.name, background=background.name, foreground=self._foreground_path.name,
                    voice=self._voice_path.name, duration=self.frame_count / self.fps,
                    resolution=[self.width, self.height], fps=self.fps,
                    audio_dropped_chunks=self.audio_dropped_chunks)
                from postprocess import write_json
                write_json(self.session_path, self.session)
            elif self.microphone and self._wave.exists() and self._wave.stat().st_size > 44:
                command = [self._ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                           '-i', str(self._silent), '-i', str(self._wave),
                           '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
                           '-b:a', '160k', '-shortest', '-movflags', '+faststart', str(self.path)]
                subprocess.run(command, check=True, capture_output=True, timeout=180)
            else:
                shutil.copyfile(self._silent, self.path)
        except Exception as exc:
            self.error = str(exc)
        finally:
            for process in (self._foreground, self._video):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if process is not None:
                    for pipe in (process.stdin, process.stderr):
                        if pipe is not None and not pipe.closed:
                            pipe.close()
            try:
                if self.frame_count:
                    self.report_path.write_text(json.dumps(self.diagnostic_report(), indent=2), encoding='utf-8')
            except Exception:
                # Diagnostics must never prevent finalizing an otherwise valid MP4.
                pass
            if self._temp is not None:
                self._temp.cleanup()
            self.finished = True

    def abort(self):
        self.stopping = True
        self._audio_capture_stopped.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
            if self._audio_worker is not None:
                self._audio_worker.join(timeout=5)
            if hasattr(self, '_audio_file'):
                self._audio_file.close()
            if self._raw_audio_file:
                self._raw_audio_file.close()
            if self._foreground is not None:
                self._foreground.kill()
                self._foreground.wait(timeout=5)
            if self._video is not None:
                self._video.kill()
                self._video.wait(timeout=5)
        finally:
            if self._temp is not None:
                self._temp.cleanup()
