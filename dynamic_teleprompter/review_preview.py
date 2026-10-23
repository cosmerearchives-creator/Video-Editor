"""Bounded, seekable source-frame decoding for the alignment preview.

Qt owns the playback clock/audio. FFmpeg decodes the recorded background and
alpha foreground together on a worker. The GUI uses the export text painter
between them. No video decoding or subprocess waiting runs on the GUI thread.
"""
from collections import OrderedDict
from pathlib import Path
import subprocess
import threading
from PySide6.QtCore import QObject, Qt, Signal, QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QWidget


def composite_frame(background, overlay, foreground):
    result = background.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not overlay.isNull():
            painter.drawImage(result.rect(), overlay)
        painter.drawImage(result.rect(), foreground)
    finally:
        painter.end()
    return result


class ReviewVideo(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(180)
        self.frame = QImage()

    def display(self, frame):
        self.frame = frame
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if not self.frame.isNull():
            size = self.frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((self.width()-size.width())/2, (self.height()-size.height())/2,
                            size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, self.frame)
        painter.end()


class SourceFrames(QObject):
    ready = Signal(int, int, QImage, str)

    def __init__(self, session_path, session, ffmpeg, parent=None):
        super().__init__(parent)
        self.base, self.session, self.ffmpeg = Path(session_path).parent, session, ffmpeg
        width, height = session['resolution']
        self.width = min(960, width)
        self.height = max(2, round(height*self.width/width))
        self.fps = session['fps']
        self.condition = threading.Condition()
        self.pending, self.process, self.stopped = None, None, False
        self.next_index = -1
        self.cache = OrderedDict()
        self.thread = threading.Thread(target=self.run, daemon=True, name='Review source frames')
        self.thread.start()

    def request(self, index, generation):
        with self.condition:
            self.pending = (index, generation)
            self.condition.notify()

    def kill_decoder(self):
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdout.close()

    def open_decoder(self, index):
        self.kill_decoder()
        position = f'{index/self.fps:.6f}'
        command = [self.ffmpeg, '-v', 'error', '-threads', '1', '-ss', position,
            '-i', str(self.base / self.session['background']), '-threads', '1', '-ss', position,
            '-i', str(self.base / self.session['foreground']), '-filter_complex_threads', '1',
            '-filter_complex', f'[0:v]scale={self.width}:{self.height},format=rgba[b];'
                f'[1:v]scale={self.width}:{self.height},format=rgba[f];[b][f]hstack=shortest=1',
            '-an', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1']
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.next_index = index

    def frame(self, index):
        if index in self.cache:
            return self.cache[index]
        if self.process is None or index < self.next_index or index-self.next_index > self.fps//2:
            self.open_decoder(index)
        size = self.width*self.height*8
        while self.next_index <= index:
            data = bytearray()
            while len(data) < size and not self.stopped:
                chunk = self.process.stdout.read(size-len(data))
                if not chunk:
                    raise RuntimeError('Could not decode recording source frames')
                data.extend(chunk)
            if self.stopped:
                return QImage()
            image = QImage(bytes(data), self.width*2, self.height, self.width*8,
                           QImage.Format.Format_RGBA8888).copy()
            self.cache[self.next_index] = image
            self.next_index += 1
            while len(self.cache) > 6:
                self.cache.popitem(last=False)
        return self.cache[index]

    def run(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or self.pending is not None)
                    if self.stopped:
                        return
                    index, generation = self.pending
                    self.pending = None
                try:
                    image = self.frame(index)
                    if not self.stopped:
                        self.ready.emit(index, generation, image, '')
                except Exception as exc:
                    self.kill_decoder()
                    if not self.stopped:
                        self.ready.emit(index, generation, QImage(), str(exc))
        finally:
            self.kill_decoder()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify()
        # Interrupt a blocked pipe read before joining the worker.
        process = self.process
        if process and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        self.thread.join(timeout=4)
