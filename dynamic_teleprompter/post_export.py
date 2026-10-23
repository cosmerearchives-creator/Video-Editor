"""Offline text painting using the presentation's original CanvasScene renderer."""
import math
from pathlib import Path
import queue
import subprocess
import tempfile
import threading

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPainter

from postprocess import cues_from_words, validate_words


class TextTimeline:
    def __init__(self, scene_class, session, words):
        validate_words(words, session['duration'])
        self.session = session
        self.cues = cues_from_words(session, words)
        self.scenes = [scene_class(), scene_class()]
        self.loaded = [None, None]
        for scene in self.scenes:
            scene.setFixedSize(*session['resolution'])
            scene.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def close(self):
        for scene in self.scenes:
            scene.close()
            scene.deleteLater()

    def frame(self, timestamp):
        width, height = self.session['resolution']
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        active = [i for i, cue in enumerate(self.cues) if cue['start'] <= timestamp < cue['end']]
        current = active[-1] if active else None
        draw = []
        if current is not None and current > 0:
            previous = self.cues[current - 1]
            motion = previous['settings'].get('motion', {})
            # Character/letter animations can have many more units than words.
            outro = (motion.get('outro_ms', 0) + len(previous['text']) * motion.get('stagger_ms', 0)) / 1000
            if motion.get('outro', 'None') != 'None' and timestamp < previous['end'] + outro:
                draw.append((current - 1, True))
        if current is not None:
            draw.append((current, False))
        try:
            for slot, (index, leaving) in enumerate(draw):
                cue, scene = self.cues[index], self.scenes[slot]
                settings = dict(cue['settings'], transparent=True, background_image='', instant=True, mode='Script')
                settings['media_layers'] = [dict(v) for v in settings.get('media_layers', []) if v.get('type') == 'text']
                text, timing = cue['text'], list(cue['words'])
                behavior = settings.get('behavior')
                if behavior in ('Stack', 'Scroll'):
                    count = 3 if behavior == 'Stack' else 5
                    prior = self.cues[max(0, index - count):index]
                    text = '\n'.join([c['text'] for c in prior] + [text])
                    timing = [w for c in prior for w in c['words']] + timing
                if self.loaded[slot] != index:
                    scene.set_output([text, '', ''], settings, layout_templates=[text])
                    scene.ensurePolished()
                    scene.layout.activate()
                    self.loaded[slot] = index
                label = scene.labels[0]
                motion = label.motion
                unit = motion.get('unit', 'Phrase')
                if unit == 'Word':
                    starts = [w['start'] for w in timing]
                elif unit in ('Letter', 'Character'):
                    starts = [w['start'] + (w['end'] - w['start']) * n / max(1, len(w['text']))
                              for w in timing for n, char in enumerate(w['text']) if not char.isspace()]
                else:
                    starts = [cue['start'] + n * motion.get('stagger_ms', 0) / 1000
                              for n in range(len(label._unit_parts()))]
                label._ticker.stop()
                label._export_time = timestamp
                label._aligned_unit_starts = starts
                label._export_outro_at = cue['end']
                label._phase = 'outro' if leaving else 'intro'
                # CanvasScene clears its own transparent background using Source
                # composition. Render each transition into a separate image so
                # the next scene cannot erase the preceding scene's outro.
                part = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
                part.fill(Qt.GlobalColor.transparent)
                target = QPainter(part)
                try:
                    scene.render(target, QPoint(0, 0))
                finally:
                    target.end()
                painter.drawImage(0, 0, part)
        finally:
            painter.end()
        return image


class VideoExport:
    """FFmpeg I/O on a worker; GUI supplies bounded, deterministic overlay frames."""
    def __init__(self, session_path, session, destination, ffmpeg):
        self.session = session
        self.path = Path(destination)
        self.temporary = self.path.with_name(self.path.stem + '.partial.mp4')
        if self.temporary.exists():
            raise ValueError('An export with this destination is already in progress')
        base = Path(session_path).parent
        width, height = session['resolution']
        command = [ffmpeg, '-v', 'error', '-y', '-i', str(base / session['background']),
            '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{width}x{height}',
            '-framerate', str(session['fps']), '-i', 'pipe:0',
            '-i', str(base / session['foreground']), '-i', str(base / session['video']),
            '-filter_complex', '[0:v][1:v]overlay=format=auto[text];[text][2:v]overlay=format=auto[final]',
            '-map', '[final]', '-map', '3:a?', '-c:v', 'libx264', '-preset', 'medium',
            '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-t', str(session['duration']),
            '-movflags', '+faststart', str(self.temporary)]
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=self.log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.frames = queue.Queue(maxsize=3)
        self.finished, self.error, self.cancelled = False, '', False
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def push(self, image):
        if self.finished or self.cancelled:
            return False
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        try:
            self.frames.put_nowait(bytes(rgba.constBits()))
            return True
        except queue.Full:
            return False

    def finish(self):
        try:
            self.frames.put_nowait(None)
            return True
        except queue.Full:
            return False

    def cancel(self):
        self.cancelled = True
        self.process.kill()
        try:
            self.frames.put_nowait(None)
        except queue.Full:
            pass

    def _write(self):
        try:
            while not self.cancelled:
                frame = self.frames.get()
                if frame is None:
                    break
                self.process.stdin.write(frame)
            self.process.stdin.close()
            code = self.process.wait()
            if code:
                self.log.seek(0)
                raise RuntimeError(self.log.read().decode('utf-8', 'replace')[-3000:])
            if not self.cancelled:
                self.temporary.replace(self.path)
        except Exception as exc:
            self.error = str(exc)
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
        finally:
            self.log.close()
            if self.temporary.exists():
                self.temporary.unlink()
            self.finished = True
