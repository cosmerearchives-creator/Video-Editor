"""Dynamic Teleprompter — presentation, recording and Live2D canvas.
Run: pip install -r requirements.txt && python app.py
"""
import weakref
import os
import json
import math
import time
import threading
import subprocess
import re
import sys
import queue
import zipfile
import tempfile
import shutil
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from recorder import CanvasRecorder, resolve_ffmpeg
from live2d_models import inspect_model, Live2DModelError
from live2d_runtime import draw_live2d, live2d_error
from live2d_idle import DEFAULT_ANIMATION, microphone as lip_microphone
from live2d_movement import step_motion, DEFAULT_MOVEMENT
from release_info import VERSION
from face_tracking import start_tracking, stop_tracking, tracking_status, tracking_active, probe_cameras, directshow_camera_names, camera_preview, inspect_camera
from audio_monitor import AudioMonitor
from audio_diagnostics import snapshot as audio_diagnostics_snapshot, reset as reset_audio_diagnostics
from audio_effects import DEFAULT_CHAIN, normalize_chain, EFFECT_NAMES, vst3_backend_available, plugin_errors, inspect_vst3_parameters

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal, QThread, QEvent, QPoint, QSettings
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QTextCursor, QTextCharFormat, QSyntaxHighlighter, QPainter, QPainterPath, QPen, QTextDocument, QIcon, QPixmap, QImage, QTransform, QRegion
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFileDialog, QFontComboBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget, QLineEdit, QSlider, QGraphicsOpacityEffect, QProgressBar, QFrame, QMenu, QListWidget, QListWidgetItem, QDialog, QInputDialog
)

import numpy as np
from native_engine import chroma_key_inplace, color_adjust_inplace, backend_status

from spellchecker import SpellChecker

APP_VERSION = 'v' + VERSION
APP_NEWS = ('New: parametric microphone EQ, RNNoise suppression and smooth gate controls; '
            'draggable canvas text, improved mouth sensitivity controls and automatic session restore.')

DICTIONARY_PATH = Path.home() / '.dynamic_teleprompter_dictionary.json'
DICTIONARY_NAME_PATH = Path.home() / '.dynamic_teleprompter_dictionary_name.json'

def dictionary_display_name():
    try:
        value = json.loads(DICTIONARY_NAME_PATH.read_text(encoding='utf-8'))
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError, TypeError):
        pass
    return 'Dictionary'

WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")


class Proofreader(QSyntaxHighlighter):
    """On-device basic proofreader; custom words persist between sessions."""
    def __init__(self, document):
        super().__init__(document)
        self.speller = SpellChecker(distance=1)
        self.custom_words = set()
        try:
            data = json.loads(DICTIONARY_PATH.read_text(encoding='utf-8'))
            self.custom_words = {str(x).lower() for x in data if isinstance(x, str)}
        except (OSError, ValueError, TypeError):
            pass
        self.speller.word_frequency.load_words(self.custom_words)
        self.check_spelling = True
        self.check_grammar = True
        self.spelling_format = QTextCharFormat()
        self.spelling_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        self.spelling_format.setUnderlineColor(QColor('#E04545'))
        self.grammar_format = QTextCharFormat()
        self.grammar_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        self.grammar_format.setUnderlineColor(QColor('#D99416'))

    def add_word(self, word):
        word = word.strip().lower()
        if not re.fullmatch(r"[a-z]+(?:['’][a-z]+)?", word):
            return False
        self.custom_words.add(word)
        self.speller.word_frequency.load_words([word])
        DICTIONARY_PATH.write_text(json.dumps(sorted(self.custom_words), indent=2), encoding='utf-8')
        self.rehighlight()
        return True

    def highlightBlock(self, text):
        words = list(WORD_RE.finditer(text))
        if self.check_spelling:
            for match in words:
                word = match.group()
                if (len(word) > 1 and word.lower() not in self.custom_words
                        and self.speller.unknown([word.lower()])):
                    self.setFormat(match.start(), len(word), self.spelling_format)
        if self.check_grammar:
            # A small set of useful local heuristics, not a full grammar engine.
            for i in range(1, len(words)):
                if words[i].group().lower() == words[i-1].group().lower():
                    self.setFormat(words[i].start(), len(words[i].group()), self.grammar_format)
            for match in re.finditer(r' {2,}|[!?.,]{2,}', text):
                self.setFormat(match.start(), len(match.group()), self.grammar_format)
            for match in re.finditer(r'\b([Aa])\s+([aeiouAEIOU][A-Za-z]*)', text):
                self.setFormat(match.start(), len(match.group()), self.grammar_format)


class ScriptEditor(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.proofreader = Proofreader(self.document())

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        cursor = self.cursorForPosition(event.pos())
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText().strip()
        if word and self.proofreader.check_spelling and self.proofreader.speller.unknown([word.lower()]):
            candidates = self.proofreader.speller.candidates(word.lower()) or set()
            suggestions = sorted(candidates, key=lambda x: (self.proofreader.speller.word_usage_frequency(x) * -1, x))[:5]
            if suggestions:
                menu.addSeparator()
                for suggestion in suggestions:
                    action = menu.addAction('Replace with: ' + suggestion)
                    action.triggered.connect(lambda checked=False, c=QTextCursor(cursor), replacement=suggestion: self.replace_word(c, replacement))
            action = menu.addAction('Add "' + word + '" to dictionary')
            action.triggered.connect(lambda checked=False, w=word: self.add_dictionary_word(w))
        menu.exec(event.globalPos())
        menu.deleteLater()

    def replace_word(self, cursor, replacement):
        cursor.insertText(replacement)

    def add_dictionary_word(self, word):
        try:
            self.proofreader.add_word(word)
        except OSError as error:
            QMessageBox.warning(self, 'Dictionary could not be saved', str(error))


DEFAULT_SCRIPT = (
    "Thank you for downloading this program. "
    "The Coding Director is Cosmere Archives. "
    "Check him out on TikTok where he uses this program for his videos. "
    "All coding was done using ChatGPT."
)
SETTINGS_PATH = Path.home() / '.dynamic_teleprompter_v01.json'


def _paginate_text(text, chars_per_line, max_lines):
    """Split a phrase into complete, word-aware display pages without losing text."""
    width = max(1, chars_per_line)
    max_lines = max(1, max_lines)
    pages, page_lines, row = [], [], ''

    def emit_line():
        nonlocal row, page_lines
        if not row:
            return
        page_lines.append(row)
        row = ''
        if len(page_lines) >= max_lines:
            pages.append('\n'.join(page_lines))
            page_lines = []

    for word in text.split():
        # A word longer than the configured line width must also be preserved.
        while len(word) > width:
            emit_line()
            row = word[:width]
            word = word[width:]
            emit_line()
        if not word:
            continue
        if row and len(row) + 1 + len(word) > width:
            emit_line()
        row = f'{row} {word}' if row else word
    emit_line()
    if page_lines:
        pages.append('\n'.join(page_lines))
    return pages


def groups_from_script(text, grouping, limit, chars_per_line=24, max_lines=2):
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []
    if grouping == 'Words':
        chunks = text.split()
    elif grouping == 'Sentences':
        chunks = re.split(r'(?<=[.!?])\s+', text)
    elif grouping == 'Phrases':
        chunks = re.split(r'(?<=[,;:.!?])\s+', text)
    else:
        # Split groups at the configured character limit, without losing words.
        chunks, current = [], ''
        for word in text.split():
            if current and len(current) + len(word) + 1 > limit:
                chunks.append(current)
                current = word
            else:
                current += (' ' if current else '') + word
        if current:
            chunks.append(current)
    # Each group may occupy several slides; never truncate its leftover words.
    pages = []
    for chunk in chunks:
        pages.extend(_paginate_text(chunk.strip(), chars_per_line, max_lines))
    return pages


def wrap_lines(text, max_chars, max_lines):
    """Wrap without discarding content (including separately rendered mode headers)."""
    return '\n'.join(
        line for paragraph in text.split('\n')
        for line in _paginate_text(paragraph, max_chars, 1)
    )


ANIMATION_OPTIONS = [
    'None', 'Fade', 'Slide Up', 'Slide Down', 'Slide Left', 'Slide Right',
    'Typewriter', 'Draw In', 'Pop', 'Zoom', 'Rotate', 'Wipe',
    'Bounce', 'Rise', 'Scatter', 'Flip'
]
ANIMATION_DEFAULT = dict(intro='Fade', outro='Fade', unit='Phrase',
                         intro_ms=350, outro_ms=240, stagger_ms=65)


class StyledOutputLabel(QLabel):
    """Paint animated individual glyphs/words/lines, retaining outline/glow/shadow."""
    def __init__(self, *args):
        super().__init__(*args)
        self.visual = {}
        self.motion = dict(ANIMATION_DEFAULT)
        self._phase = 'idle'
        self._phase_started = time.monotonic()
        self._pending = None
        self._target_text = ''
        self.reserved_lines = 1  # Full-slide line count; never derive from spoken prefix.
        self._reveal_skip = 0
        self._completed_at = time.monotonic()
        self._ticker = QTimer(self)
        self._ticker.setInterval(16)
        self._ticker.timeout.connect(self._tick)

    def cancel_motion(self):
        self._ticker.stop()
        self._phase = 'idle'
        self._pending = None
        self._target_text = self.text()
        self._reveal_skip = 0
        self._completed_at = time.monotonic()
        self.update()

    def show_content(self, content, motion, animate=True, incremental=False):
        self.motion = dict(motion)
        if content == self._target_text:
            return
        # Incremental recognition must never re-run the exit animation just
        # because word-aware wrapping moved a word onto a new line. Compare
        # the actual word stream, not newline/spacing in the painted label.
        old_words = self.text().split()
        new_words = content.split()
        extends_text = (old_words and len(new_words) > len(old_words)
                        and new_words[:len(old_words)] == old_words)
        if incremental and extends_text:
            old_parts = len(self._unit_parts())
            self._ticker.stop()
            self._target_text = content
            self._pending = None
            self.setText(content)
            self._reveal_skip = old_parts
            if self.motion.get('intro') != 'None' and self.motion.get('intro_ms', 0) > 0:
                self._start_phase('intro')
            else:
                self._phase = 'idle'
                self._completed_at = time.monotonic()
                self.update()
            return
        self._reveal_skip = 0
        self._target_text = content
        self._pending = content
        if not animate:
            self._ticker.stop()
            self.setText(content)
            self._pending = None
            self._phase = 'idle'
            self._completed_at = time.monotonic()
            return
        if self.text() and self.motion.get('outro') != 'None' and self.motion.get('outro_ms', 0) > 0:
            # Don't keep restarting the exit on fast partial transcription updates.
            if self._phase != 'outro':
                self._start_phase('outro')
        else:
            self._commit_pending()

    def _start_phase(self, phase):
        self._phase = phase
        self._phase_started = time.monotonic()
        self._ticker.start()
        self.update()

    def _commit_pending(self):
        content = self._pending if self._pending is not None else self._target_text
        self._pending = None
        self._reveal_skip = 0
        self.setText(content)
        if content and self.motion.get('intro') != 'None' and self.motion.get('intro_ms', 0) > 0:
            self._start_phase('intro')
        else:
            self._phase = 'idle'
            self._ticker.stop()
            self._completed_at = time.monotonic()
            self.update()

    def _unit_parts(self):
        parts = []
        fm = self.fontMetrics()
        lines = self.text().split('\n')
        line_spacing = fm.lineSpacing()
        top = (self.height() - max(len(lines), self.reserved_lines) * line_spacing) / 2 + fm.ascent()
        align = self.alignment()
        mode = self.motion.get('unit', 'Phrase')
        for line_no, line in enumerate(lines):
            line_width = fm.horizontalAdvance(line)
            if align & Qt.AlignmentFlag.AlignRight:
                x = self.width() - line_width - 12
            elif align & Qt.AlignmentFlag.AlignHCenter:
                x = (self.width() - line_width) / 2
            else:
                x = 12
            y = top + line_no * line_spacing
            if mode == 'Phrase':
                sections = [line]
            elif mode == 'Word':
                sections = re.findall(r'\S+\s*|\s+', line)
            else:
                sections = list(line)
            for section in sections:
                if section.strip():
                    path = QPainterPath()
                    path.addText(x, y, self.font(), section)
                    parts.append((path, path.boundingRect().center()))
                x += fm.horizontalAdvance(section)
        return parts

    def _tick(self):
        if self._phase == 'idle':
            self._ticker.stop()
            return
        duration = max(1, int(self.motion.get('intro_ms' if self._phase == 'intro' else 'outro_ms', 350)))
        count = max(1, len(self._unit_parts()))
        animate_count = count - self._reveal_skip if self._phase == 'intro' else count
        total = duration + max(0, animate_count-1) * int(self.motion.get('stagger_ms', 0))
        if (time.monotonic() - self._phase_started) * 1000 >= total:
            if self._phase == 'outro':
                self._commit_pending()
            else:
                self._phase = 'idle'
                self._ticker.stop()
                self._completed_at = time.monotonic()
            self.update()
        else:
            self.update()

    def _progress(self, n, count):
        timestamp = getattr(self, '_export_time', None)
        if timestamp is not None:
            # Offline rendering uses the same visual painter with audio-derived
            # unit starts; animation duration/style remain presentation settings.
            phase = self._phase
            duration = max(1, int(self.motion.get('intro_ms' if phase == 'intro' else 'outro_ms', 350))) / 1000
            animation = self.motion.get('intro' if phase == 'intro' else 'outro', 'None')
            if phase == 'intro':
                starts = self._aligned_unit_starts
                start = starts[n] if n < len(starts) else starts[-1] if starts else 0.
                return (float(timestamp >= start) if animation == 'None' else
                        max(0., min(1., (timestamp - start) / duration)))
            start = self._export_outro_at + (count - 1 - n) * self.motion.get('stagger_ms', 0) / 1000
            return 0. if animation == 'None' else 1. - max(0., min(1., (timestamp - start) / duration))
        duration = max(1, int(self.motion.get('intro_ms' if self._phase == 'intro' else 'outro_ms', 350)))
        stagger = int(self.motion.get('stagger_ms', 0))
        elapsed = (time.monotonic() - self._phase_started) * 1000
        # Exit starts at the last unit; presentation reads naturally in reverse.
        if self._phase == 'intro' and n < self._reveal_skip:
            return 1.0
        order = n - self._reveal_skip if self._phase == 'intro' else count - 1 - n
        fraction = max(0.0, min(1.0, (elapsed - order * stagger) / duration))
        return fraction if self._phase == 'intro' else 1.0 - fraction

    def _draw_styled(self, painter, path):
        spec = self.visual
        if spec.get('shadow'):
            painter.save()
            painter.translate(spec.get('shadow_x', 3), spec.get('shadow_y', 3))
            painter.fillPath(path, QColor(spec.get('shadow_color', '#333333')))
            painter.restore()
        if spec.get('glow'):
            color = QColor(spec.get('glow_color', '#FFFF99'))
            radius = spec.get('glow_width', 5)
            for extra, alpha in ((radius * 3, 35), (radius * 2, 55), (radius, 90)):
                shade = QColor(color)
                shade.setAlpha(alpha)
                painter.strokePath(path, QPen(shade, max(1, extra), Qt.PenStyle.SolidLine,
                                              Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        thickness = spec.get('outline_width', 0)
        if thickness:
            painter.strokePath(path, QPen(QColor(spec.get('outline_color', '#FFFFFF')),
                                          thickness * 2, Qt.PenStyle.SolidLine,
                                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.fillPath(path, QColor(spec.get('foreground', self.palette().color(self.foregroundRole()).name())))

    def paintEvent(self, event):
        if not self.text():
            return
        # Use the same painter/layout when idle and animated. QLabel's default
        # painter centers only the currently revealed prefix, causing a jump.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        parts = self._unit_parts()
        count = len(parts)
        animation = self.motion.get('intro' if self._phase == 'intro' else 'outro', 'None')
        for i, (path, center) in enumerate(parts):
            visible = self._progress(i, count) if self._phase != 'idle' else 1.0
            if visible <= 0:
                continue
            eased = 1.0 - (1.0-visible)**3
            painter.save()
            # Different motion styles share one renderer and keep visual effects intact.
            if animation in ('Fade', 'Typewriter', 'Draw In', 'Wipe'):
                if animation == 'Fade':
                    painter.setOpacity(visible)
                elif animation == 'Typewriter':
                    painter.setOpacity(1 if visible >= 0.15 else visible / 0.15)
                else:
                    bounds = path.boundingRect()
                    painter.setClipRect(bounds.x() - 8, bounds.y() - 20,
                                        max(0.1, (bounds.width()+16)*visible), bounds.height()+40)
            elif animation in ('Slide Up', 'Rise', 'Bounce'):
                offset = (1-eased)*45
                if animation == 'Bounce':
                    offset += abs(math.sin(visible*math.pi*2.5)) * (1-visible) * 16
                painter.translate(0, offset)
                painter.setOpacity(visible)
            elif animation == 'Slide Down':
                painter.translate(0, -(1-eased)*45)
                painter.setOpacity(visible)
            elif animation == 'Slide Left':
                painter.translate((1-eased)*60, 0)
                painter.setOpacity(visible)
            elif animation == 'Slide Right':
                painter.translate(-(1-eased)*60, 0)
                painter.setOpacity(visible)
            elif animation in ('Pop', 'Zoom', 'Flip', 'Rotate', 'Scatter'):
                painter.translate(center)
                if animation == 'Pop':
                    scale = 0.5 + 0.5*eased + math.sin(visible*math.pi)*0.12
                    painter.scale(scale, scale)
                elif animation == 'Zoom':
                    scale = 1.8-0.8*eased
                    painter.scale(scale, scale)
                elif animation == 'Flip':
                    painter.scale(max(0.01, eased), 1)
                elif animation == 'Rotate':
                    painter.rotate((1-eased)*-65)
                    painter.scale(max(0.1,eased), max(0.1,eased))
                else:
                    painter.translate((1-eased) * (35 if i%2 else -35), (1-eased) * (25 if i%3 else -25))
                painter.translate(-center)
                painter.setOpacity(visible)
            self._draw_styled(painter, path)
            painter.restore()


class EffectsSample(StyledOutputLabel):
    """Display real output text effects using the exact production painter."""
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#e6e6e6'))
        painter.end()
        super().paintEvent(event)


VIDEO_DECODE_LIMIT = 720  # vertical pixels; configurable per project


class ContinuousVideoDecoder:
    """Persistent FFmpeg decoder on a worker thread, with a latest-frame buffer.

    Each decoder is paced using source FPS on a monotonic clock. It never creates
    a QPixmap outside Qt's GUI thread. Frames are RGB24 and are copied before
    crossing the thread boundary. Looping restarts the decoder at EOF.
    """
    def __init__(self, path, ffmpeg, loop, decode_limit=720):
        self.path, self.ffmpeg, self.loop = path, ffmpeg, loop
        self.decode_limit = decode_limit
        self.lock = threading.Lock()
        self.latest = None
        self.serial = 0
        self.width = self.height = 0
        self.running = True
        self.proc = None
        self.last_used = time.monotonic()
        self.thread = threading.Thread(target=self._run, name='Video decoder', daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try: proc.kill()
            except OSError: pass

    def _metadata(self):
        probe = Path(self.ffmpeg).with_name('ffprobe.exe' if Path(self.ffmpeg).suffix.lower() == '.exe' else 'ffprobe')
        probe_exe = str(probe) if probe.is_file() else shutil.which('ffprobe')
        if not probe_exe:
            return (30.0, 1280, 720)
        try:
            import fractions
            result = subprocess.run([probe_exe, '-v', 'error', '-select_streams', 'v:0',
                      '-show_entries', 'stream=width,height,avg_frame_rate', '-of', 'json', self.path],
                      capture_output=True, timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            info = json.loads(result.stdout.decode('utf-8'))['streams'][0]
            fps = float(fractions.Fraction(info.get('avg_frame_rate') or '30/1'))
            return (max(1, min(60, fps or 30)), int(info['width']), int(info['height']))
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError):
            return (30.0, 1280, 720)

    def _run(self):
        fps, source_w, source_h = self._metadata()
        # Cap decoded size for interactive composition; keep source aspect ratio.
        # Limit maximum height and preserve aspect ratio; 720p is default.
        scale = min(1., (self.decode_limit * 16 / 9)/max(1,source_w), self.decode_limit/max(1,source_h))
        width = max(2, (int(source_w * scale)//2)*2)
        height = max(2, (int(source_h * scale)//2)*2)
        self.width, self.height = width, height
        length = width * height * 3
        interval = 1. / fps
        while self.running:
            cmd = [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                   '-i', self.path, '-an', '-sn', '-vf', f'fps={fps:.6f},scale={width}:{height}',
                   '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                due = time.monotonic()
                count = 0
                while self.running:
                    # Read a complete raw RGB frame, not an arbitrary pipe chunk.
                    chunks, remaining = [], length
                    while remaining and self.running:
                        chunk = self.proc.stdout.read(remaining)
                        if not chunk: break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    if remaining: break
                    payload = b''.join(chunks)
                    with self.lock:
                        self.latest = payload
                        self.serial += 1
                    count += 1
                    due += interval
                    # Drop timing debt instead of playing too fast after a stall.
                    delay = due - time.monotonic()
                    if delay > 0:
                        time.sleep(min(delay, interval))
                    elif delay < -0.5:
                        due = time.monotonic()
            except (OSError, ValueError):
                break
            finally:
                proc = self.proc
                self.proc = None
                if proc:
                    try:
                        if proc.poll() is None: proc.kill()
                        proc.wait(timeout=2)
                    except (OSError, subprocess.SubprocessError): pass
            if not self.loop or not self.running: break
            time.sleep(0.02)
        self.running = False


class VideoFrameCache:
    """GUI-thread adapter for persistent off-thread video decoders."""
    def __init__(self):
        self.decoders = {}
        self.cache = {}

    def frame(self, layer, ffmpeg):
        if not ffmpeg: return QPixmap()
        path = str(layer.get('path', ''))
        if not Path(path).is_file(): return QPixmap()
        key = (path, bool(layer.get('loop', True)), ffmpeg, VIDEO_DECODE_LIMIT)
        decoder = self.decoders.get(key)
        if decoder is None:
            decoder = ContinuousVideoDecoder(path, ffmpeg, bool(layer.get('loop', True)), VIDEO_DECODE_LIMIT)
            self.decoders[key] = decoder
        decoder.last_used = time.monotonic()
        with decoder.lock:
            serial, payload = decoder.serial, decoder.latest
            width, height = decoder.width, decoder.height
        cached = self.cache.get(key)
        if cached and cached[0] == serial: return cached[1]
        if payload is None or width < 2 or height < 2: return cached[1] if cached else QPixmap()
        image = QImage(payload, width, height, width*3, QImage.Format.Format_RGB888).copy()
        pix = QPixmap.fromImage(image)
        self.cache[key] = (serial, pix)
        if len(self.decoders) > 8:
            now = time.monotonic()
            for old_key, old in list(self.decoders.items()):
                if now - old.last_used > 5:
                    old.stop()
                    self.decoders.pop(old_key, None)
                    self.cache.pop(old_key, None)
        return pix

    def shutdown(self):
        for decoder in self.decoders.values(): decoder.stop()
        self.decoders.clear()
        self.cache.clear()


VIDEO_FRAMES = VideoFrameCache()
VIDEO_FFMPEG_PATH = ''
IMAGE_PIXMAP_CACHE = {}


FILTERED_PIXMAP_CACHE = {}


def filtered_pixmap(pix, layer):
    """Vectorized RGBA filtering, cached by source frame and effect parameters.

    Qt's ARGB32 is BGRA in memory on little-endian Windows. NumPy works on a
    local QImage copy; its arrays never escape the function or cross threads.
    Video decoder serial changes only when a new frame is ready, so the cached
    QPixmap is shared by preview, editor, PNG export and MP4 recording.
    """
    if pix.isNull():
        return pix
    brightness = int(layer.get('brightness', 0))
    contrast = int(layer.get('contrast', 0))
    saturation = int(layer.get('saturation', 100))
    chroma = bool(layer.get('chroma', False))
    sharpness = int(layer.get('sharpness', 0))
    if not (brightness or contrast or saturation != 100 or chroma or sharpness):
        return pix
    key_color = QColor(layer.get('key_color', '#00FF00'))
    tolerance = max(0, min(255, int(layer.get('tolerance', 65))))
    cache_key = (str(layer.get('path', '')), pix.cacheKey(), brightness, contrast,
                 saturation, chroma, key_color.name(), tolerance, sharpness)
    cached = FILTERED_PIXMAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    image = pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    # The decoder has already limited video dimensions; avoid additional
    # downscaling and preserve full image quality for still-image sources.
    height, width = image.height(), image.width()
    stride = image.bytesPerLine()
    buffer = np.frombuffer(image.bits(), dtype=np.uint8, count=stride * height)
    pixels = buffer.reshape(height, stride)[:, :width * 4].reshape(height, width, 4)
    # In-place alpha keying uses vectorized integer comparisons, never Python
    # per-pixel loops. BGRA byte order applies on little-endian Windows.
    if chroma:
        # Compare bounded channels directly (uint8), avoiding three full-size
        # int16 conversion buffers and their large allocation cost per frame.
        if not chroma_key_inplace(pixels, (key_color.blue(), key_color.green(), key_color.red()), tolerance):
            # Existing vectorized path remains available if native DLL is absent.
            key_mask = np.ones((height, width), dtype=np.bool_)
            for channel, target in enumerate((key_color.blue(), key_color.green(), key_color.red())):
                plane = pixels[:, :, channel]
                np.logical_and(key_mask, plane >= max(0, target - tolerance), out=key_mask)
                np.logical_and(key_mask, plane <= min(255, target + tolerance), out=key_mask)
            pixels[:, :, 3][key_mask] = 0
    if (brightness or contrast or saturation != 100) and not color_adjust_inplace(pixels, brightness, contrast, saturation):
        rgb = pixels[:, :, :3].astype(np.float32)
        gray = (rgb[:, :, 0] * 0.114 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.299)[:, :, None]
        if saturation != 100:
            rgb = gray + (rgb - gray) * (saturation / 100.0)
        if contrast:
            rgb = (rgb - 128.0) * (1.0 + contrast / 100.0) + 128.0
        if brightness:
            rgb += brightness * 2.55
        np.clip(rgb, 0, 255, out=rgb)
        pixels[:, :, :3] = rgb.astype(np.uint8)
    if sharpness:
        # 5-point cross blur uses a bounded uint16 accumulator to avoid overflow.
        # Positive sharpness applies an unsharp mask; negative values blend
        # towards the blurred image. No extra work occurs at sharpness=0.
        original = pixels[:, :, :3].astype(np.float32)
        padded = np.pad(pixels[:, :, :3], ((1, 1), (1, 1), (0, 0)), mode='edge')
        blurred = (padded[1:-1, 1:-1].astype(np.uint16) * 4
                   + padded[:-2, 1:-1].astype(np.uint16)
                   + padded[2:, 1:-1].astype(np.uint16)
                   + padded[1:-1, :-2].astype(np.uint16)
                   + padded[1:-1, 2:].astype(np.uint16)) / 8.0
        if sharpness > 0:
            updated = original + (original - blurred) * (sharpness / 100.0)
        else:
            updated = original + (blurred - original) * (-sharpness / 100.0)
        pixels[:, :, :3] = np.clip(updated, 0, 255).astype(np.uint8)
    # QPixmap.fromImage transfers/copies the image data; no buffer ownership
    # is held by the resulting pixmap.
    result = QPixmap.fromImage(image)
    FILTERED_PIXMAP_CACHE[cache_key] = result
    if len(FILTERED_PIXMAP_CACHE) > 24:
        FILTERED_PIXMAP_CACHE.pop(next(iter(FILTERED_PIXMAP_CACHE)))
    return result


def source_image_pixmap(path):
    try:
        stamp = Path(path).stat().st_mtime_ns
    except OSError:
        return QPixmap()
    cached = IMAGE_PIXMAP_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    pix = QPixmap(path)
    IMAGE_PIXMAP_CACHE[path] = (stamp, pix)
    if len(IMAGE_PIXMAP_CACHE) > 30:
        IMAGE_PIXMAP_CACHE.pop(next(iter(IMAGE_PIXMAP_CACHE)))
    return pix


def draw_media_layers(painter, layers, canvas_size, foreground=False):
    """Draw image layers into native canvas coordinates, independently of preview size."""
    width, height = canvas_size
    text_index = next((i for i, entry in enumerate(layers) if entry.get('type') == 'text'), 0)
    # Index zero is topmost in editor. All media after text is behind it.
    for index in range(len(layers)-1, -1, -1):
        layer = layers[index]
        if layer.get('type') not in ('image', 'video', 'live2d') or not layer.get('visible', True):
            continue
        if (index < text_index) != foreground:
            continue
        if layer.get('type') == 'live2d':
            draw_live2d(painter, layer, canvas_size)
            continue
        path = layer.get('path', '')
        pix = (VIDEO_FRAMES.frame(layer, resolve_ffmpeg(VIDEO_FFMPEG_PATH)) if layer.get('type') == 'video'
               else source_image_pixmap(path))
        if pix.isNull():
            continue
        pix = filtered_pixmap(pix, layer)
        scale = max(0.01, float(layer.get('scale', 1.0)))
        # Percentage offsets and scale relative to the native canvas.
        fit = min(width / pix.width(), height / pix.height())
        target_width = max(1, round(pix.width() * fit * scale))
        target_height = max(1, round(pix.height() * fit * scale))
        x = round((width - target_width) / 2 + width * float(layer.get('x', 0))/100)
        y = round((height - target_height) / 2 + height * float(layer.get('y', 0))/100)
        painter.save()
        painter.setOpacity(max(0., min(1., float(layer.get('opacity', 100))/100)))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(x, y, target_width, target_height, pix)
        painter.restore()


class LayerCanvasPreview(QWidget):
    """Aspect-correct canvas preview. Drag selected layer to move it in canvas space."""
    moved = Signal(float, float)

    @property
    def main(self):
        return self._main_ref()

    def __init__(self, main):
        super().__init__()
        self._main_ref = weakref.ref(main)
        self.setMinimumSize(260, 180)
        self.setMouseTracking(True)
        self.setToolTip('Select a layer in the list, then drag here to move it, including Text Output')
        self.start = None

    def content_rect(self):
        cw, ch = self.main.out.canvas_size
        factor = min(self.width() / cw, self.height() / ch)
        w, h = cw * factor, ch * factor
        return ((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#1c2530'))
        snapshot = self.main.out._snapshot
        if snapshot.isNull():
            return
        x, y, w, h = self.content_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        from PySide6.QtCore import QRectF
        painter.drawImage(QRectF(x,y,w,h), snapshot)
        painter.setPen(QPen(QColor('#83b8d7'), 1))
        painter.drawRect(QRectF(x,y,w,h))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = event.position()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            _, _, w, h = self.content_rect()
            if w > 0 and h > 0:
                dx = (event.position().x() - self.start.x()) * 100.0 / w
                dy = (event.position().y() - self.start.y()) * 100.0 / h
                self.start = event.position()
                self.moved.emit(dx, dy)

    def mouseReleaseEvent(self, event):
        self.start = None


class VisualLayerEditor(QDialog):
    """Nonmodal media layer arranger; modifies the project's native rendering stack."""
    def refresh_tracking_status(self):
        # Drain the background camera scan without accessing Qt from its thread.
        if hasattr(self, '_camera_scan_results'):
            try:
                devices, names, error = self._camera_scan_results.get_nowait()
            except queue.Empty:
                pass
            else:
                self.camera_refresh.setEnabled(True)
                self._populate_cameras(devices, names)
                self._camera_scan_note = (error or
                    (f'{len(devices)} working camera(s) found.' if devices
                     else 'No working cameras detected. Check device permissions or use a manual index.'))
        
        if hasattr(self, '_camera_identify_results'):
            try:
                jpeg, err = self._camera_identify_results.get_nowait()
                if err:
                    self._camera_scan_note = f'Camera identification failed: {err}'
                elif jpeg:
                    pix = QPixmap()
                    if pix.loadFromData(jpeg, 'JPG'):
                        self.camera_preview_label.setPixmap(pix.scaled(320, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                        self._camera_scan_note = f'Snapshot from {self.camera_choice.currentText()}. If this is the correct camera, click Start Tracking.'
            except queue.Empty:
                pass
        status = 'Camera: ' + tracking_status()
        row = self.items.currentRow() if hasattr(self, 'items') else -1
        if 0 <= row < len(self.main.media_layers):
            layer = self.main.media_layers[row]
            if layer.get('type') == 'live2d':
                error = live2d_error(layer.get('path', ''))
                if error:
                    status += '\nLive2D render error: ' + error
        self.tracker_status.setText(status)
        active = tracking_active()
        self.camera_toggle.setText('Stop Tracking' if active else 'Start Tracking')
        self.camera_toggle.setToolTip('Stop webcam tracking' if active else 'Start tracking using the selected camera')
        if hasattr(self, 'camera_note'):
            self.camera_note.setText(self._camera_scan_note)
        if hasattr(self, 'camera_preview_label'):
            jpeg, camera_idx = camera_preview()
            if jpeg and tracking_active():
                pix = QPixmap()
                if pix.loadFromData(jpeg, 'JPG'):
                    self.camera_preview_label.setPixmap(pix.scaled(320, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    self.camera_preview_label.setToolTip(f'Live view: Camera {camera_idx}')


    @property
    def main(self):
        return self._main_ref()

    def __init__(self, main):
        super().__init__(main)
        self._main_ref = weakref.ref(main)
        self.setWindowTitle('Visual Layer Editor — Dynamic Teleprompter')
        self.resize(950, 600)
        outer = QVBoxLayout(self)
        self.hint = QLabel('Top of list = foreground. Move media above Text Output to overlay text.\nLive2D: use Idle Animation for automatic blinking, breathing, and microphone lip sync (no camera required).')
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)
        columns = QHBoxLayout()
        outer.addLayout(columns, 1)
        left = QVBoxLayout()
        columns.addLayout(left, 2)
        self.items = QListWidget()
        self.items.currentRowChanged.connect(self.select_layer)
        left.addWidget(self.items)
        for action_row in [
            [('Image…', self.add_image), ('Video…', self.add_video), ('Live2D…', self.add_live2d), ('Model Info', self.model_info), ('Idle Animation…', self.edit_idle_animation), ('Movement…', self.edit_movement)],
            [('Model Library…', self.open_model_library), ('Next Model', self.next_model)],
            [('Rename', self.rename_layer), ('Duplicate', self.duplicate_layer), ('Remove', self.remove_layer), ('↑', self.move_up), ('↓', self.move_down)],
        ]:
            actions = QHBoxLayout()
            left.addLayout(actions)
            for label, callback in action_row:
                b = QPushButton(label)
                b.clicked.connect(callback)
                actions.addWidget(b)
        # Camera controls are intentionally hidden pending a separate tracking editor.
        # All webcam backend functions remain available for future development.
        self.preview = LayerCanvasPreview(self.main)
        self.preview.moved.connect(self.drag_layer)
        columns.addWidget(self.preview, 5)
        right = QVBoxLayout()
        columns.addLayout(right, 3)
        form = QFormLayout()
        right.addLayout(form)
        self.tracking = QCheckBox('Enable face tracking for this Live2D layer')
        self.tracking.toggled.connect(self.apply_controls)
        self.tracking.hide()  # stored per-layer; future tracking editor will expose this
        self.visible = QCheckBox('Visible')
        self.visible.toggled.connect(self.apply_controls)
        form.addRow('Layer:', self.visible)
        self.x = QDoubleSpinBox(); self.x.setRange(-200, 200); self.x.setSuffix(' %')
        self.y = QDoubleSpinBox(); self.y.setRange(-200, 200); self.y.setSuffix(' %')
        self.scale = QDoubleSpinBox(); self.scale.setRange(0.05, 10); self.scale.setSingleStep(0.1); self.scale.setValue(1)
        self.opacity = QSpinBox(); self.opacity.setRange(0,100); self.opacity.setValue(100); self.opacity.setSuffix(' %')
        self.brightness = QSpinBox(); self.brightness.setRange(-100,100); self.brightness.setSuffix(' %')
        self.contrast = QSpinBox(); self.contrast.setRange(-100,100); self.contrast.setSuffix(' %')
        self.saturation = QSpinBox(); self.saturation.setRange(0,200); self.saturation.setValue(100); self.saturation.setSuffix(' %')
        self.sharpness = QSpinBox(); self.sharpness.setRange(-100, 100); self.sharpness.setSuffix(' %')
        self.chroma = QCheckBox('Enable chroma key')
        self.key_color = '#00ff00'
        self.key_color_button = QPushButton('Key color: #00FF00')
        self.key_color_button.clicked.connect(self.choose_key_color)
        self.tolerance = QSpinBox(); self.tolerance.setRange(0,255); self.tolerance.setValue(65)
        self.loop = QCheckBox('Loop video')
        self.locked = QCheckBox('Lock layer position and properties')
        self.locked.toggled.connect(self.apply_controls)
        self.chroma.toggled.connect(self.apply_controls)
        self.loop.toggled.connect(self.apply_controls)
        for label, widget in [('X offset:', self.x), ('Y offset:', self.y), ('Scale:', self.scale), ('Opacity:', self.opacity), ('Brightness:', self.brightness), ('Contrast:', self.contrast), ('Saturation:', self.saturation), ('Sharpness:', self.sharpness), ('Chroma tolerance:', self.tolerance)]:
            form.addRow(label, widget)
            widget.valueChanged.connect(self.apply_controls)
        form.addRow('Chroma key:', self.chroma)
        form.addRow('Key color:', self.key_color_button)
        form.addRow('Video playback:', self.loop)
        form.addRow('Lock layer:', self.locked)
        self.text_note = QLabel('Select a layer to adjust it. Chroma key, color adjustments and sharpness are applied to the project canvas and recording. Negative sharpness softens; positive values sharpen.')
        self.text_note.setWordWrap(True)
        right.addWidget(self.text_note)
        right.addStretch()
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(33)
        self.preview_timer.timeout.connect(self.preview.update)
        self.preview_timer.start()
        close = QPushButton('Close Editor')
        close.clicked.connect(self.close)
        outer.addWidget(close)
        self.refresh_items()

    def _populate_cameras(self, devices, names=None):
        previous = self.camera_choice.currentData()
        if previous is None:
            try:
                previous = int(self.main.local_settings.value('tracking_camera_index', 0))
            except (TypeError, ValueError):
                previous = 0
        names = names or []
        known = {int(i): backend for i, backend in devices}
        self.camera_choice.blockSignals(True)
        self.camera_choice.clear()
        # Detected indexes first; preserve manual selection even when a camera isn't found.
        indices = sorted(known)
        if int(previous) not in indices:
            indices.append(int(previous))
        for idx in indices:
            backend = known.get(idx)
            if backend:
                # DirectShow names follow DirectShow indexes; never guess for MSMF.
                name = names[idx] if backend == 'DirectShow' and 0 <= idx < len(names) else None
                label = f'{name} (Camera {idx})' if name else f'Camera {idx} ({backend})'
            else:
                label = f'Camera {idx} (manual / not detected)'
            self.camera_choice.addItem(label, idx)
            self.camera_choice.setItemData(self.camera_choice.count()-1, label, Qt.ItemDataRole.ToolTipRole)
        self.camera_choice.addItem('Other camera index…', -1)
        index = self.camera_choice.findData(previous)
        self.camera_choice.setCurrentIndex(max(0, index))
        self.camera_choice.blockSignals(False)
        self.camera_choice.view().setMinimumWidth(max(420, self.camera_choice.sizeHint().width() + 90))

    def remember_camera(self, *_):
        idx = self.camera_choice.currentData()
        if idx == -1:
            idx, ok = QInputDialog.getInt(self, 'Camera index', 'Enter OpenCV camera index:', 0, 0, 99)
            if ok:
                self.camera_choice.insertItem(self.camera_choice.count()-1, f'Camera {idx} (manual)', idx)
                self.camera_choice.setCurrentIndex(self.camera_choice.findData(idx))
            else:
                self.camera_choice.setCurrentIndex(0)
            return
        if idx is not None:
            self.main.local_settings.setValue('tracking_camera_index', int(idx))
            self._camera_scan_note = f'Selected: {self.camera_choice.currentText()}. Click Start Tracking.' 

    def refresh_cameras(self):
        self.camera_refresh.setEnabled(False)
        self._camera_scan_note = 'Scanning webcam indexes 0–7…'
        # Camera probing can take several seconds, especially for unavailable devices.
        def scan():
            try:
                result = probe_cameras(range(8))
                names = directshow_camera_names()
                self._camera_scan_results.put((result, names, ''))
            except Exception as exc:
                self._camera_scan_results.put(([], [], f'Camera scan failed: {exc}'))
        threading.Thread(target=scan, daemon=True, name='Webcam device discovery').start()

    def identify_camera(self):
        index = self.camera_choice.currentData()
        if index is None or int(index) < 0:
            self._camera_scan_note = 'Select a camera before identifying it.'
            return
        if tracking_active():
            self._camera_scan_note = 'Live camera view is already shown while tracking.'
            return
        self._camera_scan_note = f'Opening Camera {index} for a diagnostic snapshot…'
        def work():
            try:
                self._camera_identify_results.put((inspect_camera(int(index)), ''))
            except Exception as exc:
                self._camera_identify_results.put((None, str(exc)))
        threading.Thread(target=work, daemon=True, name='Camera identify').start()

    def toggle_camera_tracking(self):
        if tracking_active():
            stop_tracking()
            self._camera_scan_note = 'Tracking stopped. Select a camera or click Start Tracking.'
            self.refresh_tracking_status()
        else:
            self.start_selected_camera()

    def start_selected_camera(self):
        index = self.camera_choice.currentData()
        if index is None:
            self._camera_scan_note = 'Select a camera first.'
            return
        row = self.items.currentRow()
        if 0 <= row < len(self.main.media_layers):
            layer = self.main.media_layers[row]
            if layer.get('type') == 'live2d' and not layer.get('tracking', False):
                self.tracking.setChecked(True)
        self.remember_camera()
        start_tracking(int(index))
        self._camera_scan_note = f'Using {self.camera_choice.currentText()}. Tracking is starting…'
        self.refresh_tracking_status()

    def choose_key_color(self):
        row = self.items.currentRow()
        if row < 0 or row >= len(self.main.media_layers): return
        layer = self.main.media_layers[row]
        if layer.get('type') not in ('image', 'video', 'live2d') or layer.get('locked'): return
        current = QColor(layer.get('key_color', '#00ff00'))
        selected = QColorDialog.getColor(current, self, 'Choose chroma key color')
        if not selected.isValid(): return
        self.key_color = selected.name()
        self.key_color_button.setText('Key color: ' + self.key_color.upper())
        self.apply_controls()

    def edit_idle_animation(self):
        row = self.items.currentRow()
        if row < 0 or row >= len(self.main.media_layers):
            QMessageBox.information(self, 'Live2D animation', 'Select a Live2D layer first.')
            return
        layer = self.main.media_layers[row]
        if layer.get('type') != 'live2d':
            QMessageBox.information(self, 'Live2D animation', 'Select a Live2D model, not an image or text layer.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Live2D — Camera-Free Animation')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)
        cfg = {**DEFAULT_ANIMATION, **layer.get('idle_animation', {})}
        controls = {}
        for key, title, low, high, step in [
            ('blink_interval', 'Average blink interval (sec)', 1, 15, .5),
            ('breath_period', 'Breathing period (sec)', 1, 12, .5),
            ('breath_strength', 'Breath strength', 0, 1, .05),
            ('lip_sensitivity', 'Mouth sensitivity', 0, 100, 1),
            ('lip_threshold', 'Microphone noise floor', 0, .2, .005),
        ]:
            box = QDoubleSpinBox()
            box.setObjectName(key)
            box.setRange(low, high)
            box.setSingleStep(step)
            box.setDecimals(3)
            box.setKeyboardTracking(False)
            box.setValue(float(cfg[key]))
            form.addRow(title, box)
            controls[key] = box
        for key, label in [('blink_enabled','Randomized blinking'),('breath_enabled','Automatic breathing'),('lip_enabled','Microphone lip sync')]:
            check = QCheckBox(label)
            check.setChecked(bool(cfg[key]))
            form.addRow('', check)
            controls[key] = check
        note = QLabel('Mouth motion follows microphone loudness, not spoken phonemes. The model must contain the relevant Cubism parameters. Changes apply immediately when saved.')
        note.setWordWrap(True)
        layout.addWidget(note)
        status = QLabel('Microphone: ' + (lip_microphone.error or 'Ready / starts when lip sync is enabled'))
        status.setWordWrap(True)
        layout.addWidget(status)
        actions = QHBoxLayout()
        cancel = QPushButton('Cancel')
        save = QPushButton('Save Animation Settings')
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)
        cancel.clicked.connect(dialog.reject)
        save.clicked.connect(dialog.accept)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            layer['idle_animation'] = {key: (control.isChecked() if isinstance(control, QCheckBox) else control.value())
                                       for key,control in controls.items()}
            layer['idle_enabled'] = True
            self.main.update_lip_input()
            self.main.render()
            self.preview.update()

    def edit_movement(self):
        row = self.items.currentRow()
        if row < 0 or row >= len(self.main.media_layers) or self.main.media_layers[row].get('type') != 'live2d':
            QMessageBox.information(self, 'Character movement', 'Select a Live2D layer first.')
            return
        layer = self.main.media_layers[row]
        current = {**DEFAULT_MOVEMENT, **layer.get('movement', {})}
        dialog = QDialog(self)
        dialog.setWindowTitle('Live2D — WASD Movement & Perspective')
        layout = QVBoxLayout(dialog)
        intro = QLabel('W / S = move away / closer (scale changes). A / D = move left / right and turn head.\n'
                       'Release = stop moving; head smoothly returns to center.\n'
                       'Keys work while Output or the non-text controls are focused.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        layout.addLayout(form)
        enabled = QCheckBox('Enable keyboard movement for this character')
        enabled.setChecked(bool(current['enabled']))
        form.addRow(enabled)
        definitions = [('horizontal_speed', 'Horizontal speed (canvas %/sec)', 1, 200, 30),
                       ('vertical_speed', 'Depth speed (canvas %/sec)', 1, 200, 20),
                       ('turn_seconds', 'Head turn / horizontal acceleration (ms)', 50, 1500, 350),
                       ('vertical_seconds', 'Vertical acceleration / braking (ms)', 50, 1500, 350),
                       ('head_strength', 'Head turn amount (%)', 0, 100, 65),
                       ('far_y', 'Far boundary Y (% from center)', -100, 0, -35),
                       ('near_y', 'Near boundary Y (% from center)', 0, 100, 35),
                       ('far_scale', 'Far scale (%)', 10, 200, 40),
                       ('near_scale', 'Near scale (%)', 10, 300, 100)]
        controls = {}
        for key, caption, low, high, default in definitions:
            widget = QSpinBox(dialog)
            widget.setRange(low, high)
            value = current.get(key, default)
            if key in ('turn_seconds', 'vertical_seconds'):
                widget.setSuffix(' ms')
            elif key in ('head_strength', 'far_y', 'near_y', 'far_scale', 'near_scale'):
                widget.setSuffix(' %')
            widget.setValue(int(value))
            controls[key] = widget
            form.addRow(caption, widget)
        hint = QLabel('For models without ParamAngleY, movement still works without head turning.\n'
                      'Live2D movements are relative to the saved layer position and native project canvas.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        cancel = QPushButton('Cancel')
        save = QPushButton('Save Movement Settings')
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        cancel.clicked.connect(dialog.reject)
        save.clicked.connect(dialog.accept)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = {key: control.value() for key, control in controls.items()}
            values['enabled'] = enabled.isChecked()
            if values['far_y'] >= values['near_y'] or values['far_scale'] > values['near_scale']:
                QMessageBox.warning(self, 'Perspective settings', 'Far boundary must be above near boundary, and far scale cannot exceed near scale.')
                return
            layer['movement'] = values
            self.main._movement_target = layer
            self.main.out.render_frame()
            self.preview.update()

    def drag_layer(self, dx, dy):
        row = self.items.currentRow()
        if row < 0 or row >= len(self.main.media_layers):
            return
        layer = self.main.media_layers[row]
        if layer.get('type') not in ('image', 'video', 'live2d', 'text') or layer.get('locked'):
            return
        layer['x'] = max(-200, min(200, float(layer.get('x', 0)) + dx))
        layer['y'] = max(-200, min(200, float(layer.get('y', 0)) + dy))
        self.main.render()
        self.select_layer(row)
        self.preview.update()

    def refresh_items(self, selected=None):
        if selected is None: selected = max(self.items.currentRow(), 0)
        self.items.blockSignals(True)
        self.items.clear()
        for layer in self.main.media_layers:
            prefix = '👁 ' if layer.get('visible', True) else '○ '
            self.items.addItem(prefix + layer.get('name', 'Layer') + (' [Cubism mesh preview]' if layer.get('type') == 'live2d' else ''))
        self.items.blockSignals(False)
        if self.items.count(): self.items.setCurrentRow(min(selected, self.items.count()-1))

    def select_layer(self, row):
        if row < 0 or row >= len(self.main.media_layers): return
        layer = self.main.media_layers[row]
        editable = layer.get('type') in ('image','video','live2d')
        positionable = editable or layer.get('type') == 'text'
        for widget, val in [(self.tracking, bool(layer.get('tracking', False))), (self.visible, bool(layer.get('visible', True))), (self.x, float(layer.get('x',0))),
                            (self.y,float(layer.get('y',0))), (self.scale,float(layer.get('scale',1))),
                            (self.opacity,int(layer.get('opacity',100))),
                            (self.brightness,int(layer.get('brightness',0))),
                            (self.contrast,int(layer.get('contrast',0))),
                            (self.saturation,int(layer.get('saturation',100))),
                            (self.sharpness,int(layer.get('sharpness',0))),
                            (self.tolerance,int(layer.get('tolerance',65))),
                            (self.chroma,bool(layer.get('chroma',False))),
                            (self.loop,bool(layer.get('loop',True))),
                            (self.locked,bool(layer.get('locked',False)))]:
            widget.blockSignals(True)
            if isinstance(widget, QCheckBox): widget.setChecked(val)
            else: widget.setValue(val)
            widget.blockSignals(False)
            widget.setEnabled((editable or (positionable and widget in (self.x, self.y, self.visible, self.locked))) and (widget is self.locked or not layer.get('locked', False)))
        self.tracking.setEnabled(layer.get('type') == 'live2d' and not layer.get('locked', False))
        self.key_color = QColor(layer.get('key_color', '#00ff00')).name()
        self.key_color_button.setText('Key color: ' + self.key_color.upper())
        self.key_color_button.setEnabled(editable and not layer.get('locked', False))

    def apply_controls(self, *_):
        row = self.items.currentRow()
        if row < 0 or row >= len(self.main.media_layers): return
        layer = self.main.media_layers[row]
        if layer.get('locked', False) and self.locked.isChecked(): return
        if layer.get('type') == 'text':
            layer.update(x=self.x.value(), y=self.y.value(), visible=self.visible.isChecked(), locked=self.locked.isChecked())
            self.main.render()
            self.refresh_items(row)
            self.preview.update()
            return
        if layer.get('type') not in ('image','video','live2d'): return
        layer.update(tracking=self.tracking.isChecked(), visible=self.visible.isChecked(), x=self.x.value(), y=self.y.value(),
                     scale=self.scale.value(), opacity=self.opacity.value(),
                     brightness=self.brightness.value(), contrast=self.contrast.value(),
                     saturation=self.saturation.value(), sharpness=self.sharpness.value(),
                     chroma=self.chroma.isChecked(), key_color=self.key_color,
                     tolerance=self.tolerance.value(), loop=self.loop.isChecked(),
                     locked=self.locked.isChecked())
        self.main.render()
        self.refresh_items(row)
        self.preview.update()

    def add_image(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Add canvas image', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp)')
        if path:
            if QPixmap(path).isNull():
                QMessageBox.warning(self, 'Image could not be opened', path)
                return
            entry = dict(type='image', name=Path(path).name, path=path, visible=True,
                         x=0, y=0, scale=1, opacity=100)
            self.main.media_layers.insert(0, entry)
            self.main.render()
            self.refresh_items(0)

    def add_video(self):
        if not resolve_ffmpeg(self.main.ffmpeg_path):
            QMessageBox.warning(self, 'FFmpeg required', 'Select ffmpeg.exe in the Recording tab first.')
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Add canvas video', '',
                                              'Video files (*.mp4 *.mov *.mkv *.webm *.avi)')
        if path:
            self.main.media_layers.insert(0, dict(type='video', name=Path(path).name, path=path,
                                                  visible=True, loop=True, x=0, y=0, scale=1, opacity=100))
            self.main.render()
            self.refresh_items(0)

    def add_live2d(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Import a Live2D model', '',
                                              'Live2D manifests (*.model3.json)')
        if not path:
            return
        try:
            report = inspect_model(path)
        except Live2DModelError as exc:
            QMessageBox.warning(self, 'Live2D import failed', str(exc))
            return
        if report['errors']:
            QMessageBox.warning(self, 'Incomplete Live2D model',
                                'The model package is incomplete:\n\n' + '\n'.join(report['errors']))
            return
        entry = dict(type='live2d', name=report['name'], path=report['manifest'],
                     visible=True, tracking=False, idle_enabled=True, idle_animation=dict(DEFAULT_ANIMATION), x=0, y=0, scale=1, opacity=100)
        self.main.media_layers.insert(0, entry)
        self.main.render()
        self.refresh_items(0)
        self.model_info()

    def open_model_library(self):
        row = self.items.currentRow()
        if 0 <= row < len(self.main.media_layers) and self.main.media_layers[row].get('type') == 'live2d':
            self.main.active_model_layer = row
        self.main.open_model_library()
        self.refresh_items(self.main.active_model_layer)

    def next_model(self):
        row = self.items.currentRow()
        if 0 <= row < len(self.main.media_layers) and self.main.media_layers[row].get('type') == 'live2d':
            self.main.active_model_layer = row
        self.main.cycle_model()
        self.refresh_items(self.main.active_model_layer)

    def model_info(self):
        row = self.items.currentRow()
        if row < 0 or row >= len(self.main.media_layers):
            return
        layer = self.main.media_layers[row]
        if layer.get('type') != 'live2d':
            QMessageBox.information(self, 'Model information', 'Select a Live2D model layer first.')
            return
        try:
            report = inspect_model(layer.get('path', ''))
        except Live2DModelError as exc:
            QMessageBox.warning(self, 'Model unavailable', str(exc))
            return
        info = [f"Manifest: {report['manifest']}",
                f"Textures: {report['texture_count']}",
                f"Motions: {report['motion_count']}",
                f"Expressions: {report['expression_count']}",
                f"Status: {'Valid package' if report['valid'] else 'Missing required resources'}"]
        if report['errors']:
            info.extend(['', 'ERRORS:'] + report['errors'])
        if report['warnings']:
            info.extend(['', 'WARNINGS:'] + report['warnings'])
        info.extend(['', 'Camera-free idle blinking, breathing and microphone amplitude lip sync are available in Idle Animation settings. Webcam tracking is preserved but hidden. Physics and advanced shader modes remain unsupported.'])
        QMessageBox.information(self, 'Live2D model assets', '\n'.join(info))

    def rename_layer(self):
        row = self.items.currentRow()
        if row < 0 or self.main.media_layers[row].get('type') == 'text': return
        layer = self.main.media_layers[row]
        name, accepted = QInputDialog.getText(self, 'Rename layer', 'Layer name:',
                                              text=layer.get('name', 'Layer'))
        if accepted and name.strip():
            layer['name'] = name.strip()
            self.refresh_items(row)

    def duplicate_layer(self):
        row = self.items.currentRow()
        if row < 0 or self.main.media_layers[row].get('type') == 'text': return
        layer = dict(self.main.media_layers[row])
        layer['name'] = layer.get('name', 'Layer') + ' copy'
        layer['locked'] = False
        self.main.media_layers.insert(row, layer)
        self.main.render()
        self.refresh_items(row)

    def remove_layer(self):
        row = self.items.currentRow()
        if row < 0 or self.main.media_layers[row].get('type') == 'text' or self.main.media_layers[row].get('locked'): return
        del self.main.media_layers[row]
        self.main.render()
        self.refresh_items(row)
        self.preview.update()

    def move_up(self):
        self.move(-1)

    def move_down(self):
        self.move(1)

    def move(self, direction):
        row = self.items.currentRow()
        other = row + direction
        if row < 0 or other < 0 or other >= len(self.main.media_layers): return
        if self.main.media_layers[row].get('locked') or self.main.media_layers[other].get('locked'): return
        layers = self.main.media_layers
        layers[row], layers[other] = layers[other], layers[row]
        self.main.render()
        self.refresh_items(other)


class CanvasScene(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Dynamic Teleprompter — Output')
        self.resize(850, 440)
        self.text_container = QWidget(self)
        self.text_container.setGeometry(self.rect())
        self.layout = QVBoxLayout(self.text_container)
        self.layout.setContentsMargins(24, 24, 24, 24)
        self.layout.setSpacing(12)
        self.labels = []
        for _ in range(3):
            label = StyledOutputLabel('', self.text_container)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(False)
            label.setMinimumHeight(30)
            self.layout.addWidget(label, 1)
            self.labels.append(label)
        self.current_settings = {}
        self._media_layers = [{'type':'text','name':'Text Output','visible':True}]
        self._output_background = QColor('#00FF00')
        self._background_image_path = ''
        self._background_pixmap = QPixmap()
        self._output_transparent = False
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.set_output(['', '', ''], {})

    def position_text(self):
        layer = next((v for v in self._media_layers if v.get('type') == 'text'), {})
        self.text_container.setGeometry(round(self.width() * float(layer.get('x', 0)) / 100),
            round(self.height() * float(layer.get('y', 0)) / 100), self.width(), self.height())
        self.text_container.setVisible(layer.get('visible', True))

    def resizeEvent(self, event):
        self.position_text()
        super().resizeEvent(event)

    def paintEvent(self, event):
        # Paint the full window, including the space between labels, on EVERY
        # repaint. Never depend on a stylesheet or on the presence of text.
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        if self._output_transparent:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        else:
            painter.fillRect(self.rect(), self._output_background)
            if not self._background_pixmap.isNull():
                scaled = self._background_pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                        Qt.TransformationMode.SmoothTransformation)
                x = (self.width() - scaled.width()) // 2
                y = (self.height() - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
        draw_media_layers(painter, self._media_layers, (self.width(), self.height()), foreground=False)
        painter.end()

    def set_output(self, texts, settings, transition_key=None, layout_templates=None):
        settings = dict(settings)
        old = self.current_settings
        self.current_settings = settings
        self._media_layers = settings.get('media_layers', self._media_layers)
        self.position_text()
        bg = QColor(settings.get('background', '#00FF00'))
        if not bg.isValid():
            bg = QColor('#00FF00')
        transparent = bool(settings.get('transparent', False))
        image_path = str(settings.get('background_image', ''))
        if image_path != self._background_image_path:
            self._background_image_path = image_path
            self._background_pixmap = QPixmap(image_path) if image_path else QPixmap()
        background_key = (bg.name(QColor.NameFormat.HexArgb), transparent, image_path)
        if getattr(self, '_background_key', None) != background_key:
            # The output uses custom-painted child widgets. Painting the background
            # explicitly avoids platform/style-dependent QWidget stylesheet behavior.
            # Do not rebuild the labels or restart animations when a color changes.
            transparency_changed = self._output_transparent != transparent
            self._output_background = bg
            self._output_transparent = transparent
            if transparency_changed:
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, transparent)
            self._background_key = background_key
            self.update()
        count = 3 if settings.get('mode') == 'Comparison' else 1
        style_keys = ('font', 'font_size', 'bold', 'align', 'foreground', 'visual')
        style_changed = any(old.get(key) != settings.get(key) for key in style_keys)
        for i, label in enumerate(self.labels):
            should_show = i < count
            if label.isHidden() == should_show:
                label.setVisible(should_show)
            if not should_show:
                label.cancel_motion()
                label.setText('')
                continue
            visual = dict(settings.get('visual', {}))
            visual['foreground'] = settings.get('foreground', '#000000')
            if label.visual != visual:
                label.visual = visual
                label.update()
            if style_changed or not getattr(label, '_styled_once', False):
                label.setFont(QFont(settings.get('font', 'Arial'), settings.get('font_size', 32),
                                    QFont.Weight.Bold if settings.get('bold') else QFont.Weight.Normal))
                alignment = {'Left': Qt.AlignmentFlag.AlignLeft, 'Center': Qt.AlignmentFlag.AlignHCenter,
                             'Right': Qt.AlignmentFlag.AlignRight}.get(settings.get('align'), Qt.AlignmentFlag.AlignHCenter)
                label.setAlignment(alignment | Qt.AlignmentFlag.AlignVCenter)
                label.setStyleSheet(f'color: {visual["foreground"]}; background: transparent;')
                label._styled_once = True
            raw = texts[i] if i < len(texts) else ''
            template = (layout_templates[i] if layout_templates and i < len(layout_templates) else raw)
            full_layout = wrap_lines(template, settings.get('chars', 24), settings.get('lines', 2)) if template else ''
            new_reservation = max(1, len(full_layout.split('\n')))
            if label.reserved_lines != new_reservation:
                label.reserved_lines = new_reservation
                label.update()
            content = wrap_lines(raw, settings.get('chars', 24), settings.get('lines', 2)) if raw else ''
            motion = settings.get('motion', ANIMATION_DEFAULT)
            label.show_content(content, motion, animate=not settings.get('instant', False),
                               incremental=settings.get('incremental', False))


class OutputWindow(QWidget):
    """Window-sized preview of a separate fixed-pixel presentation canvas.

    This is deliberately not a screen capture. PNG export and the preview both
    use the same offscreen QPainter render of CanvasScene at project dimensions.
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Dynamic Teleprompter — Output')
        self.resize(850, 440)
        self.scene = CanvasScene()
        self.scene.setParent(self)
        self.canvas_size = (1920, 1080)
        self.frame_rate = 30
        self._snapshot = QImage()
        self._dirty = True
        self._rendering = False
        self.render_time_ms = 0.0
        self.render_count = 0
        self.scene.setFixedSize(*self.canvas_size)
        self.scene.move(-self.canvas_size[0]-20, -self.canvas_size[1]-20)
        self.scene.show()
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(16)
        self._preview_timer.timeout.connect(self.refresh_preview)
        self._preview_timer.start()

    @property
    def labels(self):
        return self.scene.labels

    @property
    def current_settings(self):
        return self.scene.current_settings

    def set_output(self, texts, settings, transition_key=None, layout_templates=None):
        resolution = str(settings.get('canvas_resolution', '1920x1080'))
        try:
            width, height = map(int, resolution.split('x'))
        except (ValueError, TypeError):
            width, height = 1920, 1080
        width = max(64, min(3840, width))
        height = max(64, min(3840, height))
        if (width, height) != self.canvas_size:
            self.canvas_size = (width, height)
            self.scene.setFixedSize(width, height)
            self.scene.move(-width-20, -height-20)
        self.frame_rate = int(settings.get('canvas_fps', 30))
        self._preview_timer.setInterval(max(1, round(1000 / max(1, self.frame_rate))))
        self.scene._media_layers = settings.get('media_layers', [{'type':'text','name':'Text Output'}])
        self.scene.set_output(texts, settings, transition_key, layout_templates)
        self._dirty = True
        self.update()

    def render_frame(self, include_text=True, include_foreground=True):
        """Generate native-resolution pixels, regardless of preview window size."""
        if self._rendering:
            return self._snapshot
        self._rendering = True
        started = time.perf_counter()
        try:
            width, height = self.canvas_size
            frame = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            frame.fill(Qt.GlobalColor.transparent)
            painter = QPainter(frame)
            try:
                # PySide6 requires the targetOffset argument for the QPainter overload.
                if include_text:
                    self.scene.render(painter, QPoint(0, 0))
                else:
                    # Draw the background and its media without text children.
                    # Do not hide labels: that would disturb the live preview.
                    self.scene.render(painter, QPoint(0, 0), QRegion(), QWidget.RenderFlag.DrawWindowBackground)
            finally:
                painter.end()
            # Foreground media is drawn after QWidget.render, above the text widgets.
            overlay = QPainter(frame)
            try:
                if include_foreground:
                    draw_media_layers(overlay, self.scene._media_layers, self.canvas_size, foreground=True)
            finally:
                overlay.end()
            if include_text and include_foreground:
                self._snapshot = frame
            self.render_time_ms = (time.perf_counter() - started) * 1000.0
            self.render_count += 1
            self._dirty = False
            return frame
        finally:
            self._rendering = False

    def render_foreground(self):
        frame = QImage(*self.canvas_size, QImage.Format.Format_ARGB32_Premultiplied)
        frame.fill(Qt.GlobalColor.transparent)
        painter = QPainter(frame)
        try:
            draw_media_layers(painter, self.scene._media_layers, self.canvas_size, foreground=True)
        finally:
            painter.end()
        return frame

    def refresh_preview(self):
        if self.isVisible() and not self.isMinimized():
            self.render_frame()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#232323'))
        if self._snapshot.isNull():
            return
        target_size = self._snapshot.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - target_size.width()) // 2
        y = (self.height() - target_size.height()) // 2
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(x, y, self._snapshot.scaled(target_size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))

    def export_png(self, filename):
        return self.render_frame().save(str(filename), 'PNG')


MODEL_DIR = Path.home() / '.dynamic_teleprompter_models' / 'vosk-model-small-en-us-0.15'
MODEL_URL = 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip'


# The small English Vosk model does NOT ship graph/words.txt. It ships
# graph/HCLr.fst and graph/Gr.fst instead; checking words.txt caused valid
# installations to be reported as incomplete.
VOSK_REQUIRED_FILES = (
    'am/final.mdl', 'conf/model.conf', 'conf/mfcc.conf',
    'graph/HCLr.fst', 'graph/Gr.fst',
    'graph/phones/word_boundary.int', 'ivector/final.ie',
)


def model_is_installed():
    """Detect a fully extracted Vosk small English model at its expected path."""
    return all((MODEL_DIR / name).is_file() and (MODEL_DIR / name).stat().st_size > 0
               for name in VOSK_REQUIRED_FILES)


def normalized_words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower().replace('’', "'"))


class ModelDownloader(QThread):
    progress = Signal(int)
    completed = Signal(str)
    failed = Signal(str)

    def run(self):
        try:
            MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / 'model.zip'
                with urllib.request.urlopen(MODEL_URL, timeout=30) as response, open(archive, 'wb') as target:
                    total = int(response.headers.get('Content-Length', 0))
                    downloaded = 0
                    while True:
                        block = response.read(131072)
                        if not block:
                            break
                        target.write(block)
                        downloaded += len(block)
                        if total:
                            self.progress.emit(min(95, int(downloaded * 95 / total)))
                with zipfile.ZipFile(archive) as z:
                    expected = MODEL_DIR.name + '/'
                    if not all(n.startswith(expected) and '..' not in Path(n).parts for n in z.namelist()):
                        raise ValueError('Unexpected model archive layout')
                    for entry in z.infolist():
                        if entry.is_dir():
                            continue
                        dest = MODEL_DIR.parent / entry.filename
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(entry) as source, open(dest, 'wb') as target:
                            shutil.copyfileobj(source, target)
            # Verify the real files before reporting success. A failed or
            # interrupted extraction must never be marked as installed.
            if not model_is_installed():
                missing = [name for name in VOSK_REQUIRED_FILES
                           if not (MODEL_DIR / name).is_file()
                           or (MODEL_DIR / name).stat().st_size == 0]
                raise ValueError('Model extraction incomplete; missing files: ' + ', '.join(missing))
            self.progress.emit(100)
            self.completed.emit('Offline recognition model installed.')
        except Exception as exc:
            self.failed.emit(str(exc))


class VoiceWorker(QThread):
    recognized = Signal(str, bool)
    status = Signal(str)
    failed = Signal(str)
    input_level = Signal(float)

    def __init__(self, device=None):
        super().__init__()
        self.device = device
        self.running = True
        self.muted = threading.Event()

    def set_muted(self, muted):
        if muted:
            self.muted.set()
        else:
            self.muted.clear()

    def stop(self):
        self.running = False

    def run(self):
        try:
            import sounddevice as sd
            from vosk import Model, KaldiRecognizer, SetLogLevel
            SetLogLevel(-1)
            model = Model(str(MODEL_DIR))
            samplerate = 16000
            recognizer = KaldiRecognizer(model, samplerate)
            audio_queue = queue.Queue(maxsize=32)

            def callback(indata, frames, time_info, status):
                if not self.running:
                    return
                # Keep metering independent of recognition, even while muted.
                # PCM16 samples are native little-endian on Windows.
                samples = memoryview(indata).cast('h')
                if samples:
                    step = max(1, len(samples) // 512)
                    sampled = samples[::step]
                    rms = math.sqrt(sum(int(x) * int(x) for x in sampled) / len(sampled))
                    self.input_level.emit(min(100.0, rms / 32768.0 * 650.0))
                if self.muted.is_set():
                    return
                try:
                    audio_queue.put_nowait(bytes(indata))
                except queue.Full:
                    try:
                        audio_queue.get_nowait()
                        audio_queue.put_nowait(bytes(indata))
                    except queue.Empty:
                        pass

            with sd.RawInputStream(samplerate=samplerate, blocksize=800, device=self.device,
                                   dtype='int16', channels=1, callback=callback):
                self.status.emit('Listening — speak your script')
                previous_partial = ''
                was_muted = False
                while self.running:
                    if self.muted.is_set():
                        was_muted = True
                        # Discard previously queued audio; muting also blocks transcription.
                        while True:
                            try:
                                audio_queue.get_nowait()
                            except queue.Empty:
                                break
                        self.msleep(60)
                        continue
                    if was_muted:
                        recognizer = KaldiRecognizer(model, samplerate)
                        previous_partial = ''
                        was_muted = False
                    try:
                        audio = audio_queue.get(timeout=.05)
                    except queue.Empty:
                        continue
                    if recognizer.AcceptWaveform(audio):
                        words = json.loads(recognizer.Result()).get('text', '')
                        if words:
                            self.recognized.emit(words, True)
                        previous_partial = ''
                    else:
                        words = json.loads(recognizer.PartialResult()).get('partial', '')
                        if words and words != previous_partial:
                            self.recognized.emit(words, False)
                            previous_partial = words
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.status.emit('Microphone stopped')


class ReaderWindow(QWidget):
    """Speech-following reader view, separate from OBS output.

    Document contents are stable during recognition updates. Only a character
    highlight and smoothly interpolated scroll position change, avoiding jumps.
    """
    TOKEN_RE = re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)?", re.I)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Dynamic Teleprompter — Reader')
        self.resize(580, 510)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        self.status = QLabel('NEXT WORD  •  Ready')
        self.status.setStyleSheet('color: #FACC15; font-weight: bold; font-size: 15px; padding: 6px;')
        layout.addWidget(self.status)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setUndoRedoEnabled(False)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.view.setStyleSheet('QTextEdit {background: #10151E; color: #F2F5F8; border: 0; padding: 22px;}')
        self.view.setFont(QFont('Arial', 25))
        layout.addWidget(self.view, 1)
        self.tokens = []
        self.position = -1
        self._scroll_goal = 0.0
        self._target_offset = None
        self._last_tick = time.monotonic()
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setInterval(16)
        self.scroll_timer.timeout.connect(self._animate_scroll)
        self.scroll_timer.start()

    def set_script(self, script):
        self.view.setPlainText(script)
        self.tokens = [(m.start(), m.end(), m.group()) for m in self.TOKEN_RE.finditer(script)]
        self.position = -1
        self.view.verticalScrollBar().setValue(0)
        self.set_progress(0, force=True)

    def set_progress(self, spoken_words, force=False):
        if not self.tokens:
            self.status.setText('NEXT WORD  •  Paste a script to begin')
            self.view.setExtraSelections([])
            return
        # spoken_words points to the NEXT expected word, not the last one heard.
        index = min(max(0, int(spoken_words)), len(self.tokens))
        if index == self.position and not force:
            return
        self.position = index
        if index >= len(self.tokens):
            self.status.setText('SCRIPT COMPLETE')
            self.view.setExtraSelections([])
            return
        start, end, word = self.tokens[index]
        self.status.setText(f'NEXT WORD  •  {word}   ({index + 1} / {len(self.tokens)})')
        cursor = self.view.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(QColor('#FACC15'))
        selection.format.setForeground(QColor('#10151E'))
        selection.format.setFontWeight(QFont.Weight.Bold)
        self.view.setExtraSelections([selection])
        # cursorRect is viewport-relative. Add current offset to find its
        # absolute scroll coordinate, then put the next word 35% down the view.
        scrollbar = self.view.verticalScrollBar()
        rect = self.view.cursorRect(cursor)
        self._scroll_goal = max(0, min(scrollbar.maximum(),
            scrollbar.value() + rect.top() - round(self.view.viewport().height() * .35)))

    def _animate_scroll(self):
        bar = self.view.verticalScrollBar()
        if self._scroll_goal > bar.maximum():
            self._scroll_goal = bar.maximum()
        current = bar.value()
        distance = self._scroll_goal - current
        if abs(distance) < 1:
            if current != round(self._scroll_goal):
                bar.setValue(round(self._scroll_goal))
            return
        # Time-based eased interpolation, independent of screen refresh rate.
        now = time.monotonic()
        dt = min(.075, max(.001, now - self._last_tick))
        self._last_tick = now
        fraction = 1 - math.exp(-dt * 7.5)
        step = distance * fraction
        if abs(step) < 1:
            step = 1 if distance > 0 else -1
        bar.setValue(round(current + step))

    def showEvent(self, event):
        super().showEvent(event)
        self._last_tick = time.monotonic()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'Dynamic Teleprompter {APP_VERSION} — Control Center')
        self.resize(1050, 740)
        self.out = OutputWindow()
        self.reader = ReaderWindow()
        self.recorder = None
        self.audio_monitor = None
        self.record_timer = QTimer(self)
        self.record_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.record_timer.timeout.connect(self._capture_record_frame)
        self.record_finish_timer = QTimer(self)
        self.record_finish_timer.setInterval(150)
        self.record_finish_timer.timeout.connect(self._record_finished)
        self.record_dir = str(Path.home() / 'Videos')
        self.index = 0
        self.awaiting_first = True
        self.history = []
        self.color_bg = '#00FF00'
        self.color_fg = '#000000'
        self.background_image = ''
        self.media_layers = [dict(type='text', name='Text Output', visible=True)]
        self._lip_timer = QTimer(self)
        self._lip_timer.setInterval(120)
        self._lip_timer.timeout.connect(self.update_lip_input)
        self._lip_timer.start()
        self.layer_editor = None
        self.local_settings = QSettings('CosmereArchives', 'DynamicTeleprompter')
        self.model_library = self._read_model_library()
        self.active_model_layer = None
        self._movement_keys = set()
        self._movement_last_tick = time.monotonic()
        self._movement_target = None
        self._movement_timer = QTimer(self)
        self._movement_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._movement_timer.setInterval(16)
        self._movement_timer.timeout.connect(self._tick_movement)
        self._movement_timer.start()
        self.ffmpeg_path = self.local_settings.value('ffmpeg_path', '', type=str)
        global VIDEO_FFMPEG_PATH
        VIDEO_FFMPEG_PATH = self.ffmpeg_path
        self.groups = []
        self.project_path = None
        self.visual_global = {}
        self.slide_visuals = {}
        self.global_effect_exclusions = set()
        self.animation_global = dict(ANIMATION_DEFAULT)
        self.animation_slides = {}
        self.voice_worker = None
        self.downloader = None
        self.voice_cursor = 0
        self._voice_slide_ready_at = 0.0
        self._last_voice_slide = -1
        self._speech_gated = False
        self._manual_full_slide_index = None  # Pre-displayed slide stays fully rendered when voice catches up.
        self.heard_final = ''
        self.voice_enabled = False
        self.voice_muted = False
        self.last_input_level = 0.0
        self.last_input_time = 0.0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.clear_output)
        self.advance_timer = QTimer(self)
        self.advance_timer.setInterval(45)
        self.advance_timer.timeout.connect(self._process_voice_progress)
        self.animation_test_window = None
        self.animation_test_index = -1
        self.test_voice_worker = None
        self.test_cursor = 0
        self.test_heard_final = ''
        self.test_groups = [
            'Welcome to the first animation test.',
            'Words can move across two lines in your output.',
            'The third slide should transition smoothly.',
            'Here is a longer sentence to check the text wrapping and movement.',
            'The fifth and final slide completes our test.'
        ]
        self.test_advance_timer = QTimer(self)
        self.test_advance_timer.setInterval(45)
        self.test_advance_timer.timeout.connect(self._animation_test_progress)
        self._test_rendered_prefix = None
        self.build_ui()
        self.load_settings()
        self.prepare_groups()
        self._saved_project_snapshot = self._project_snapshot()
        self._session_last = None
        self.restore_session()
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(2000)
        self._session_timer.timeout.connect(self.autosave_session)
        self._session_timer.start()
        self.update_model_status()
        QTimer.singleShot(500, self.prompt_model_if_missing)
        self.out.show()
        self.reader.show()
        QTimer.singleShot(0, self.arrange_windows)

    def arrange_windows(self):
        """Place new windows beside main app instead of behind it."""
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        gap = 12
        # Prefer a compact three-column layout on large monitors. Otherwise
        # put the two previews side by side to the right (if space permits),
        # and stack them on smaller desktops. Keep every window on-screen.
        if area.width() >= 2000:
            main_w = min(850, max(610, int(area.width() * .39)))
            right_w = min(640, (area.width() - main_w - 3 * gap) // 2)
            self.setGeometry(area.left()+gap, area.top()+gap, main_w, min(800, area.height()-2*gap))
            self.reader.setGeometry(self.geometry().right()+gap, area.top()+gap, right_w, min(710, area.height()-2*gap))
            self.out.setGeometry(self.reader.geometry().right()+gap, area.top()+gap, right_w, min(500, area.height()-2*gap))
        elif area.width() >= 1200:
            main_w = max(580, round(area.width() * .55))
            side_w = area.width()-main_w-3*gap
            main_h = min(800, area.height()-2*gap)
            side_h = max(200, (area.height()-3*gap)//2)
            self.setGeometry(area.left()+gap, area.top()+gap, main_w, main_h)
            x = self.geometry().right()+gap
            self.reader.setGeometry(x,area.top()+gap,side_w,side_h)
            self.out.setGeometry(x,area.top()+2*gap+side_h,side_w,side_h)
        else:
            # Small screen: show the reader and output as accessible windows
            # rather than hiding either underneath the full-sized main window.
            main_h = max(250, round(area.height() * .54))
            lower_h = max(170, area.height()-main_h-3*gap)
            self.setGeometry(area.left()+gap,area.top()+gap,area.width()-2*gap,main_h)
            half = max(200,(area.width()-3*gap)//2)
            self.reader.setGeometry(area.left()+gap,area.top()+main_h+2*gap,half,lower_h)
            self.out.setGeometry(area.left()+2*gap+half,area.top()+main_h+2*gap,half,lower_h)
        self.reader.raise_()
        self.out.raise_()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _scroll_page(page):
        """Keep both axes available if the application's compact window clips controls."""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        page.setMinimumWidth(660)
        area.setWidget(page)
        return area

    def pick_background_image(self):
        name, _ = QFileDialog.getOpenFileName(self, 'Select output background image', '',
                                              'Images (*.png *.jpg *.jpeg *.bmp *.webp)')
        if name:
            self.background_image = name
            self.image_bg_btn.setText(Path(name).name)
            self.render()

    def clear_background_image(self):
        self.background_image = ''
        self.image_bg_btn.setText('Choose background image…')
        self.render()

    def export_canvas_frame(self):
        name, _ = QFileDialog.getSaveFileName(self, 'Export native-resolution canvas frame',
                                              'teleprompter_frame.png', 'PNG image (*.png)')
        if not name:
            return
        try:
            self.render()
            if not self.out.export_png(name):
                raise OSError('Qt failed to write the PNG frame.')
        except Exception as error:
            QMessageBox.warning(self, 'Canvas export failed', str(error))

    def _choose_ffmpeg(self):
        existing = self.ffmpeg_path or str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(self, 'Locate ffmpeg executable', existing,
                                                  'FFmpeg executable (ffmpeg.exe ffmpeg);;All files (*)')
        if not filename: return
        if Path(filename).name.lower() not in ('ffmpeg.exe', 'ffmpeg'):
            QMessageBox.warning(self, 'Invalid selection', 'Please select ffmpeg.exe.')
            return
        self.ffmpeg_path = filename
        global VIDEO_FFMPEG_PATH
        VIDEO_FFMPEG_PATH = filename
        self.local_settings.setValue('ffmpeg_path', filename)
        self.update_ffmpeg_label()

    def update_ffmpeg_label(self):
        found = resolve_ffmpeg(self.ffmpeg_path)
        self.ffmpeg_label.setText('Ready: ' + found if found else 'Not found — select ffmpeg.exe or place it beside app.py.')

    def update_lip_input(self):
        needs = any(layer.get('type') == 'live2d' and layer.get('visible', True)
                    and layer.get('idle_enabled', False)
                    and layer.get('idle_animation', {}).get('lip_enabled', True)
                    for layer in self.media_layers)
        # Recorder supplies PCM directly; idle mode uses the microphone independently.
        if not needs or self.recorder is not None or self.audio_monitor is not None:
            lip_microphone.stop()
        elif lip_microphone._stream is None:
            # Don't repeatedly open a busy or unavailable microphone every 120ms.
            now = time.monotonic()
            if now >= getattr(self, '_lip_retry_at', 0):
                lip_microphone.start()
                self._lip_retry_at = now + 5

    def _read_model_library(self):
        try:
            raw = json.loads(self.local_settings.value('live2d_model_library', '[]', type=str))
            if not isinstance(raw, list):
                return []
            result = []
            for entry in raw:
                if (isinstance(entry, dict) and isinstance(entry.get('path'), str)
                        and isinstance(entry.get('name'), str)):
                    result.append({'path': entry['path'], 'name': entry['name'],
                                   'idle_animation': dict(entry.get('idle_animation') or DEFAULT_ANIMATION),
                                   'idle_enabled': bool(entry.get('idle_enabled', True))})
            return result
        except (TypeError, ValueError):
            return []

    def _write_model_library(self):
        self.local_settings.setValue('live2d_model_library', json.dumps(self.model_library))

    def _live2d_target_index(self):
        i = self.active_model_layer
        if isinstance(i, int) and 0 <= i < len(self.media_layers) and self.media_layers[i].get('type') == 'live2d':
            return i
        return next((i for i, layer in enumerate(self.media_layers) if layer.get('type') == 'live2d'), None)

    def register_current_model(self):
        i = self._live2d_target_index()
        if i is None:
            QMessageBox.information(self, 'Model Library', 'Import a Live2D model layer first.')
            return False
        layer = self.media_layers[i]
        return self.add_model_preset(layer.get('path', ''), layer.get('name', ''), layer)

    def add_model_preset(self, path, name=None, layer=None):
        try:
            report = inspect_model(path)
            if report['errors']:
                raise Live2DModelError('; '.join(report['errors']))
        except (Live2DModelError, OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Model Library', str(exc))
            return False
        path = str(Path(report['manifest']).resolve())
        old = next((item for item in self.model_library if item['path'] == path), None)
        if old is None:
            self.model_library.append({'path': path, 'name': name or report['name'],
                                       'idle_animation': dict((layer or {}).get('idle_animation') or DEFAULT_ANIMATION),
                                       'idle_enabled': bool((layer or {}).get('idle_enabled', True))})
        else:
            if layer:
                old['idle_animation'] = dict(layer.get('idle_animation') or DEFAULT_ANIMATION)
                old['idle_enabled'] = bool(layer.get('idle_enabled', True))
        self._write_model_library()
        return True

    def activate_model_preset(self, preset_index):
        if not 0 <= preset_index < len(self.model_library):
            return False
        preset = self.model_library[preset_index]
        try:
            report = inspect_model(preset['path'])
            if report['errors']:
                raise Live2DModelError('; '.join(report['errors']))
        except (Live2DModelError, OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Model unavailable', str(exc))
            return False
        i = self._live2d_target_index()
        if i is None:
            self.media_layers.insert(0, {'type':'live2d', 'visible': True,
                                         'tracking': False, 'x': 0, 'y': 0,
                                         'scale': 1, 'opacity': 100})
            i = 0
        old = self.media_layers[i]
        # Save outgoing model's animation profile automatically before replacing it.
        outgoing_path = str(Path(old.get('path', '')).resolve()) if old.get('path') else ''
        for other in self.model_library:
            if other['path'] == outgoing_path:
                other['idle_animation'] = dict(old.get('idle_animation') or DEFAULT_ANIMATION)
                other['idle_enabled'] = bool(old.get('idle_enabled', True))
                break
        old.update(path=report['manifest'], name=preset['name'],
                   idle_animation=dict(preset.get('idle_animation') or DEFAULT_ANIMATION),
                   idle_enabled=bool(preset.get('idle_enabled', True)), tracking=False)
        self._write_model_library()
        self.active_model_layer = i
        self.render()
        if self.layer_editor is not None:
            self.layer_editor.refresh_items(i)
            self.layer_editor.preview.update()
        return True

    def cycle_model(self):
        if not self.model_library:
            self.open_model_library()
            return
        i = self._live2d_target_index()
        current = self.media_layers[i].get('path', '') if i is not None else ''
        paths = [item['path'] for item in self.model_library]
        current = str(Path(current).resolve()) if current else ''
        next_index = (paths.index(current) + 1) % len(paths) if current in paths else 0
        self.activate_model_preset(next_index)

    def open_model_library(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Live2D Model Library')
        dialog.resize(520, 400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Saved models are shared across projects. Switching replaces the active Live2D layer while keeping its canvas position and scale.'))
        items = QListWidget()
        layout.addWidget(items)
        def fill():
            index = items.currentRow()
            items.clear()
            for entry in self.model_library:
                name = entry['name']
                if not Path(entry['path']).is_file():
                    name += ' [missing]'
                items.addItem(name + '  —  ' + Path(entry['path']).name)
            if self.model_library:
                items.setCurrentRow(max(0, min(index, len(self.model_library)-1)))
        fill()
        def add_file():
            path, _ = QFileDialog.getOpenFileName(dialog, 'Register Live2D Model', '', 'Live2D manifests (*.model3.json)')
            if path and self.add_model_preset(path):
                fill()
        def add_current():
            if self.register_current_model():
                fill()
        def remove():
            i = items.currentRow()
            if 0 <= i < len(self.model_library):
                del self.model_library[i]
                self._write_model_library()
                fill()
        def activate():
            if self.activate_model_preset(items.currentRow()):
                dialog.accept()
        for controls in [[('Add Model…', add_file), ('Save Current Model', add_current)],
                         [('Switch to Selected', activate), ('Remove Preset', remove), ('Close', dialog.accept)]]:
            row = QHBoxLayout()
            layout.addLayout(row)
            for title, fn in controls:
                button = QPushButton(title)
                button.clicked.connect(fn)
                row.addWidget(button)
        items.itemDoubleClicked.connect(lambda *_: activate())
        dialog.exec()

    def open_layer_editor(self):
        if self.layer_editor is None:
            self.layer_editor = VisualLayerEditor(self)
        self.layer_editor.refresh_items()
        self.layer_editor.show()
        self.layer_editor.raise_()
        self.layer_editor.activateWindow()

    def _change_video_quality(self, *_):
        global VIDEO_DECODE_LIMIT
        limit = self.video_decode_quality.currentData()
        if limit not in (720, 1080):
            return
        if VIDEO_DECODE_LIMIT != limit:
            VIDEO_DECODE_LIMIT = limit
            VIDEO_FRAMES.shutdown()
            FILTERED_PIXMAP_CACHE.clear()
            self.render()

    def _update_performance_label(self):
        if not hasattr(self, 'performance_label'):
            return
        ms = self.out.render_time_ms
        budget = 1000.0 / max(1, self.out.frame_rate)
        video_count = sum(1 for layer in self.media_layers
                          if layer.get('type') == 'video' and layer.get('visible', True))
        decoder_count = len(VIDEO_FRAMES.decoders)
        verdict = 'Above frame budget' if ms > budget else 'Within frame budget'
        self.performance_label.setText(
            f'Last canvas render: {ms:.1f} ms / {budget:.1f} ms budget '
            f'({verdict}) | Active video layers: {video_count} | Decoders: {decoder_count}')

    def _choose_record_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose recordings folder', self.record_dir)
        if folder:
            self.record_dir = folder
            self.record_folder.setText('Folder: ' + folder)

    def _monitor_settings(self):
        # Audio workers read an immutable snapshot, never QWidget state.
        return self._audio_live_settings

    def edit_microphone_effects(self):
        from mic_ui import MicrophoneEffectsDialog
        MicrophoneEffectsDialog(self).exec()

    def _audio_chain_snapshot(self):
        chain = []
        for i in range(self.audio_chain.count()):
            item = self.audio_chain.item(i)
            entry = dict(item.data(Qt.ItemDataRole.UserRole))
            entry['enabled'] = item.checkState() == Qt.CheckState.Checked
            chain.append(entry)
        return chain

    def _set_audio_chain(self, chain):
        self.audio_chain.blockSignals(True)
        self.audio_chain.clear()
        for effect in normalize_chain(chain):
            if effect['type'] == 'vst3':
                label = 'VST3: ' + Path(effect['path']).stem
            else:
                label = EFFECT_NAMES[effect['type']]
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, dict(effect))
            item.setToolTip(effect.get('path', ''))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if effect['enabled'] else Qt.CheckState.Unchecked)
            self.audio_chain.addItem(item)
        self.audio_chain.blockSignals(False)
        self._update_live_record_settings()

    def _add_vst3(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select a VST3 plugin',
                                              str(Path.home()), 'VST3 plugins (*.vst3);;All files (*)')
        if not path:
            folder = QFileDialog.getExistingDirectory(self, 'Or choose a VST3 bundle folder', str(Path.home()))
            if not folder or not folder.lower().endswith('.vst3'):
                return
            path = folder
        chain = self._audio_chain_snapshot()
        chain.append(dict(type='vst3', path=path, enabled=True))
        self._set_audio_chain(chain)
        self.audio_chain.setCurrentRow(self.audio_chain.count() - 1)
        if not vst3_backend_available():
            QMessageBox.information(self, 'VST3 dependency missing',
                'VST3 entry added but bypassed until you install the optional host. '
                'Run install_vst3_windows.bat and restart the application.')

    def _edit_vst3_parameters(self):
        row = self.audio_chain.currentRow()
        if row < 0:
            QMessageBox.information(self, 'Choose a plugin', 'Select a VST3 effect in the chain first.')
            return
        item = self.audio_chain.item(row)
        entry = dict(item.data(Qt.ItemDataRole.UserRole))
        if entry.get('type') != 'vst3':
            QMessageBox.information(self, 'Built-in effect', 'Use the Gain, Gate, and Soft Clip controls above for built-in effects.')
            return
        if not vst3_backend_available():
            QMessageBox.warning(self, 'VST3 host missing', 'Run install_vst3_windows.bat and restart.')
            return
        # Inspection creates a separate plugin instance; never touch the live audio instance.
        try:
            available = inspect_vst3_parameters(entry['path'], entry.get('plugin_name', ''))
        except Exception as exc:
            QMessageBox.warning(self, 'Could not inspect VST3', str(exc))
            return
        if not available:
            QMessageBox.information(self, 'No exposed parameters', 'This plugin does not expose editable generic parameters.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('VST3 Parameters — ' + Path(entry['path']).stem)
        dialog.resize(480, 480)
        root = QVBoxLayout(dialog)
        hint = QLabel('Edit generic parameter values. Invalid or out-of-range values may be rejected by the plugin.\n'
                      'The plugin vendor’s custom editor is not available in this build.')
        hint.setWordWrap(True)
        root.addWidget(hint)
        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        contents = QWidget()
        form = QFormLayout(contents)
        editors = {}
        saved = entry.get('parameters', {})
        for name, value in available.items():
            editor = QLineEdit(str(saved.get(name, value)))
            editor.setToolTip('Default: ' + str(value))
            form.addRow(name + ':', editor)
            editors[name] = (editor, value)
        scroll.setWidget(contents)
        root.addWidget(scroll)
        controls = QHBoxLayout()
        reset = QPushButton('Reset to plugin defaults')
        reset.clicked.connect(lambda: [editor.setText(str(default)) for editor, default in editors.values()])
        controls.addWidget(reset)
        save = QPushButton('Apply')
        save.clicked.connect(dialog.accept)
        controls.addWidget(save)
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(dialog.reject)
        controls.addWidget(cancel)
        root.addLayout(controls)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = {}
        try:
            for name, (editor, default) in editors.items():
                text = editor.text().strip()
                if isinstance(default, bool):
                    if text.lower() not in ('true', 'false', '1', '0'):
                        raise ValueError(name + ': expected True or False')
                    value = text.lower() in ('true', '1')
                elif isinstance(default, (int, float)):
                    value = float(text)
                    if not math.isfinite(value):
                        raise ValueError(name + ': expected finite number')
                else:
                    value = text
                values[name] = value
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid parameter value', str(exc))
            return
        entry['parameters'] = values
        item.setData(Qt.ItemDataRole.UserRole, entry)
        self._update_live_record_settings()

    def _remove_vst3(self):
        row = self.audio_chain.currentRow()
        if row < 0:
            return
        entry = self.audio_chain.item(row).data(Qt.ItemDataRole.UserRole)
        if entry.get('type') != 'vst3':
            QMessageBox.information(self, 'Built-in effect', 'Only added VST3 plugins can be removed. Uncheck a built-in effect to bypass it.')
            return
        self.audio_chain.takeItem(row)
        self._update_live_record_settings()

    def _refresh_vst3_status(self):
        errors = plugin_errors()
        if errors:
            self.vst3_status.setText('Audio effects bypassed due to error: ' + '; '.join(
                Path(k).name + ': ' + v for k, v in errors.items())[:400])
        elif vst3_backend_available():
            self.vst3_status.setText('VST3 host available (experimental). Third-party plugins run in an isolated process (experimental); failed plugins are bypassed.')
        else:
            self.vst3_status.setText('VST3 unavailable: run install_vst3_windows.bat to add the optional host. Built-in effects work normally.')

    def _move_audio_effect(self, direction):
        row = self.audio_chain.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= self.audio_chain.count():
            return
        item = self.audio_chain.takeItem(row)
        self.audio_chain.insertItem(target, item)
        self.audio_chain.setCurrentRow(target)
        self._update_live_record_settings()

    def _refresh_monitor_devices(self):
        previous = self.monitor_device.currentData()
        self.monitor_device.clear()
        self.monitor_device.addItem('System default output', None)
        try:
            import sounddevice as sd
            for index, device in enumerate(sd.query_devices()):
                if device['max_output_channels'] > 0:
                    self.monitor_device.addItem(f"{device['name']} ({index})", index)
        except Exception as exc:
            self.monitor_status.setText('Could not list output devices: ' + str(exc))
        index = self.monitor_device.findData(previous)
        self.monitor_device.setCurrentIndex(max(0, index))

    def _stop_monitor(self, keep_checkbox=False):
        if self.recorder is not None:
            self.recorder.audio_listener = None
        if self.audio_monitor is not None:
            self.audio_monitor.stop()
            self.audio_monitor = None
        if not keep_checkbox and self.monitor_toggle.isChecked():
            self.monitor_toggle.blockSignals(True)
            self.monitor_toggle.setChecked(False)
            self.monitor_toggle.blockSignals(False)
        self.monitor_status.setText('Monitoring off')

    def _start_monitor(self, external=False):
        monitor = AudioMonitor(self._monitor_settings, lambda: self._monitor_level, self.monitor_device.currentData())
        try:
            monitor.start(external=external)
        except Exception as exc:
            self.monitor_status.setText('Monitor failed: ' + str(exc))
            QMessageBox.warning(self, 'Audio monitor',
                'Could not start audio monitoring. Try selecting headphones or another output device.\n\n' + str(exc))
            self.monitor_toggle.blockSignals(True)
            self.monitor_toggle.setChecked(False)
            self.monitor_toggle.blockSignals(False)
            return
        self.audio_monitor = monitor
        if external and self.recorder is not None:
            self.recorder.audio_listener = monitor.receive_processed
        self.monitor_status.setText('Listening to processed mic — use headphones to avoid feedback')

    def _refresh_audio_diagnostics(self):
        if not hasattr(self, 'audio_diagnostics_label'):
            return
        dsp = audio_diagnostics_snapshot()
        chain = dsp.get('audio_chain', {})
        host = dsp.get('vst_host_roundtrip', {})
        lines = [
            'DSP: %.2f ms average / %.2f ms peak (%d overruns)' % (
                chain.get('mean_ms', 0), chain.get('max_ms', 0), chain.get('above_budget', 0)),
            'VST3 IPC: %.2f ms average / %.2f ms peak (%d overruns)' % (
                host.get('mean_ms', 0), host.get('max_ms', 0), host.get('above_budget', 0)),
        ]
        monitor = getattr(self, 'audio_monitor', None)
        if monitor is not None:
            d = monitor.diagnostics()
            lines.append('Monitor queues: input %d / output %d | dropped %d | underruns %d' % (
                d['input_queue'], d['output_queue'], d['dropped_blocks'], d['output_underruns']))
        lines.append('Processing timings only; does not measure microphone-to-ear latency.')
        self.audio_diagnostics_label.setText('\n'.join(lines))

    def _toggle_monitor(self, enabled):
        self._stop_monitor(keep_checkbox=True)
        if enabled:
            if self.recorder is not None and not self.record_microphone.isChecked():
                self.monitor_status.setText('Monitoring during a silent recording is unavailable')
                self.monitor_toggle.setChecked(False)
                return
            self._start_monitor(external=self.recorder is not None)

    def _change_monitor_device(self, *_):
        if self.monitor_toggle.isChecked():
            self._toggle_monitor(True)

    def _update_live_record_settings(self, *_):
        self._audio_live_settings = (self._audio_chain_snapshot(), self.audio_gain.value(),
                                     self.audio_gate_threshold.value(), self.audio_clip_drive.value())
        if self.recorder is not None:
            self.recorder.audio_gain = self.audio_gain.value()
            self.recorder.audio_gate_enabled = any(e['type'] == 'gate' and e['enabled'] for e in self._audio_chain_snapshot())
            self.recorder.audio_gate_db = self.audio_gate_threshold.value()
            self.recorder.audio_chain = self._audio_chain_snapshot()
            self.recorder.audio_clip_drive = self.audio_clip_drive.value()

    def _toggle_recording(self):
        if self.recorder is not None:
            self._stop_recording()
            return
        try:
            session = None
            if self.record_post.isChecked():
                if not self.record_microphone.isChecked():
                    raise ValueError('Enable microphone recording for automatic text alignment.')
                if self.mode.currentText() != 'Script':
                    raise ValueError('Choose Script output mode for post-recording alignment.')
                if not self.groups or not self.script.toPlainText().strip():
                    raise ValueError('Prepare a script before recording an aligned presentation.')
                frozen = json.loads(json.dumps(self.settings()))
                session = {'groups': list(self.groups), 'language': 'en',
                           'slides': [dict(frozen, visual=self.visual_for_slide(i), motion=self.motion_for_slide(i))
                                      for i in range(len(self.groups))]}
            self.render()
            directory = Path(self.record_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime('%Y%m%d_%H%M%S') + f'_{time.time_ns() % 1000000000:09d}'
            filename = directory / ('Dynamic_Teleprompter_' + stamp + '.mp4')
            width, height = self.out.canvas_size
            self.recorder = CanvasRecorder(filename, width, height, self.out.frame_rate,
                                            microphone=self.record_microphone.isChecked(), ffmpeg_path=self.ffmpeg_path,
                                            audio_gain=self.audio_gain.value(),
                                            audio_gate_db=self.audio_gate_threshold.value(),
                                            audio_gate_enabled=any(e['type'] == 'gate' and e['enabled'] for e in self._audio_chain_snapshot()),
                                            audio_chain=self._audio_chain_snapshot(),
                                            audio_clip_drive=self.audio_clip_drive.value(), session=session)
            if self.audio_monitor is not None:
                self._stop_monitor(keep_checkbox=True)
            lip_microphone.stop()
            self.recorder.audio_level_listener = lip_microphone.feed
            self.recorder.start()
            if self.monitor_toggle.isChecked() and self.record_microphone.isChecked():
                self._start_monitor(external=True)
            self.record_microphone.setEnabled(False)
            self.record_post.setEnabled(False)
            self.script.setReadOnly(session is not None)
            self.canvas_resolution.setEnabled(False)
            self.canvas_fps.setEnabled(False)
            self.record_toggle.setText('■ Stop Canvas Recording')
            self.record_status.setText('Recording: ' + str(filename))
            self.record_timer.start(10)
            self._capture_record_frame()
        except Exception as error:
            self._stop_monitor(keep_checkbox=True)
            if self.recorder is not None:
                self.recorder.abort()
            self.recorder = None
            self.record_status.setText('Recording failed to start: ' + str(error))
            QMessageBox.warning(self, 'Recording failed to start', str(error))
            self.record_microphone.setEnabled(True)
            self.record_post.setEnabled(True)
            self.script.setReadOnly(False)
            self.canvas_resolution.setEnabled(True)
            self.canvas_fps.setEnabled(True)

    def _capture_record_frame(self):
        if self.recorder is None or self.recorder.stopping:
            return
        try:
            recorder = self.recorder
            due = recorder.frames_due()
            if due <= 0:
                return
            capture_start = time.perf_counter()
            if recorder.session is not None:
                frame = self.out.render_frame(include_text=False, include_foreground=False)
                recorder.push_frame(frame, self.out.render_foreground())
            else:
                frame = self.out.render_frame()
                recorder.push_frame(frame)
            recorder.note_capture((time.perf_counter() - capture_start) * 1000)
            if recorder.error:
                raise RuntimeError(recorder.error)
            if recorder.frame_count % max(1, recorder.fps) == 0:
                self.record_status.setText(
                    'Recording: %s | %.1fs | %d late | %d duplicate | '
                    'capture %.1f ms avg | encoder queue %d/%d' %
                    (recorder.path.name, recorder.elapsed(), recorder.late_frames,
                     recorder.duplicate_frames,
                     recorder.capture_total_ms / max(1, recorder.capture_calls),
                     recorder._frames.qsize(), recorder._frames.maxsize))
        except Exception as error:
            self.record_timer.stop()
            self.record_status.setText('Recording error: ' + str(error))
            self._stop_recording()

    def _stop_recording(self):
        if self.recorder is None:
            return
        self.record_timer.stop()
        if self.audio_monitor is not None:
            self._stop_monitor(keep_checkbox=True)
        self.record_toggle.setEnabled(False)
        self.record_status.setText('Finalizing MP4… Please wait.')
        self.recorder.finish_async()
        self.record_finish_timer.start()

    def _record_finished(self):
        recorder = self.recorder
        if recorder is None or not recorder.finished:
            return
        self.record_finish_timer.stop()
        self.recorder = None
        self.record_toggle.setEnabled(True)
        self.record_toggle.setText('● Start Canvas Recording')
        self.record_microphone.setEnabled(True)
        self.record_post.setEnabled(True)
        self.script.setReadOnly(False)
        if self.monitor_toggle.isChecked():
            self._start_monitor(external=False)
        self.canvas_resolution.setEnabled(True)
        self.canvas_fps.setEnabled(True)
        if recorder.error:
            self.record_status.setText('Recording failed: ' + recorder.error)
            QMessageBox.warning(self, 'Recording failed', recorder.error)
        else:
            self.record_status.setText('Saved: %s (%d frames, %d late)' %
                                       (recorder.path, recorder.frame_count, recorder.late_frames))
            if recorder.session is not None:
                self.open_alignment(recorder.session_path, automatic=True)
                return
            QMessageBox.information(self, 'Recording saved',
                                    str(recorder.path) + '\n\nDiagnostics: ' + str(recorder.report_path))

    def open_alignment(self, path=None, automatic=False):
        if self.recorder is not None or getattr(self, '_post_busy', False):
            QMessageBox.information(self, 'Finish recording first', 'Finish the current recording or export before opening alignment review.')
            return
        if not isinstance(path, (str, Path)):
            path, _ = QFileDialog.getOpenFileName(self, 'Open Recording Session', self.record_dir,
                                                'Recording session (*.session.json);;JSON (*.json)')
        if not path:
            return
        try:
            from post_ui import AlignmentDialog
            ffmpeg = resolve_ffmpeg(self.ffmpeg_path)
            if not ffmpeg:
                raise ValueError('Select FFmpeg before opening alignment review.')
            dialog = AlignmentDialog(self, path, ffmpeg, CanvasScene)
            self._post_dialog = dialog
            self._post_busy = True
            if automatic:
                QTimer.singleShot(0, dialog.start_alignment)
            dialog.exec()
            dialog.deleteLater()
        except Exception as exc:
            QMessageBox.warning(self, 'Could not open alignment', str(exc))
        finally:
            self._post_busy = False
            self._post_dialog = None

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        note = QLabel(f'{APP_VERSION} — {APP_NEWS}')
        note.setWordWrap(True)
        note.setStyleSheet('color: #B25B00; font-weight: bold;')
        main.addWidget(note)
        tabs = QTabWidget()
        main.addWidget(tabs)
        script_page = QWidget()
        page = QVBoxLayout(script_page)
        page.addWidget(QLabel('Paste or edit your script:'))
        self.script = ScriptEditor()
        self.script.setPlainText(DEFAULT_SCRIPT)
        self.script.setFont(QFont('Arial', 17))
        page.addWidget(self.script, 1)

        # These toggles live in Edit, not on the script screen.
        self.spell_toggle = QCheckBox('Check spelling')
        self.spell_toggle.setChecked(True)
        self.spell_toggle.toggled.connect(self.toggle_spell)
        self.grammar_toggle = QCheckBox('Check grammar')
        self.grammar_toggle.setChecked(True)
        self.grammar_toggle.toggled.connect(self.toggle_grammar)
        self.dictionary_button = QPushButton(dictionary_display_name())
        self.dictionary_button.clicked.connect(self.manage_dictionary)
        self.load_btn = QPushButton('Open Script')
        self.load_btn.clicked.connect(self.open_script)
        self.project_label = QLabel('Unsaved project')

        # Bottom bar: slide/navigation actions, slim live level meter and
        # presentation startup in a single row.
        bottom = QHBoxLayout()
        bottom.setSpacing(9)
        left = QVBoxLayout()
        left.setSpacing(5)
        nav_top = QHBoxLayout()
        prep = QPushButton('Prepare / Reset Script')
        prep.clicked.connect(self.prepare_groups)
        nav_top.addWidget(prep)
        clear_button = QPushButton('Clear Output')
        clear_button.clicked.connect(self.clear_output)
        nav_top.addWidget(clear_button)
        left.addLayout(nav_top)
        nav_bottom = QHBoxLayout()
        prev = QPushButton('◀ Previous')
        prev.clicked.connect(self.previous)
        nav_bottom.addWidget(prev)
        nxt = QPushButton('Next ▶')
        nxt.clicked.connect(self.next)
        nav_bottom.addWidget(nxt)
        self.progress = QLabel('0 / 0')
        nav_bottom.addWidget(self.progress)
        left.addLayout(nav_bottom)
        bottom.addLayout(left, 1)

        mic_panel = QWidget()
        mic_panel.setFixedWidth(36)
        mic_layout = QVBoxLayout(mic_panel)
        mic_layout.setContentsMargins(0, 0, 0, 0)
        mic_layout.setSpacing(4)
        self.mic_indicator = QLabel('Stopped')
        self.mic_indicator.setVisible(False)
        self.mic_meter = QProgressBar()
        self.mic_meter.setOrientation(Qt.Orientation.Vertical)
        self.mic_meter.setInvertedAppearance(False)
        self.mic_meter.setRange(0, 100)
        self.mic_meter.setValue(0)
        self.mic_meter.setTextVisible(False)
        self.mic_meter.setFixedSize(20, 55)
        self.mic_meter.setStyleSheet('QProgressBar {background:#1d2427; border:1px solid #444;} QProgressBar::chunk {background:qlineargradient(x1:0,y1:1,x2:0,y2:0,stop:0 #13b84a,stop:0.55 #8bd936,stop:0.75 #f1da26,stop:1 #ed3434);}')
        mic_layout.addWidget(self.mic_meter, 0, Qt.AlignmentFlag.AlignHCenter)
        self.mute_button = QPushButton('🔊')
        self.mute_button.setFixedSize(32, 27)
        self.mute_button.setToolTip('Mute listening')
        self.mute_button.setEnabled(False)
        self.mute_button.clicked.connect(self.toggle_mute)
        mic_layout.addWidget(self.mute_button)
        self.quick_listen_button = QPushButton('◉')
        self.quick_listen_button.setFixedSize(32, 27)
        self.quick_listen_button.setToolTip('Start voice following')
        self.quick_listen_button.clicked.connect(self.toggle_listening)
        mic_layout.addWidget(self.quick_listen_button)
        bottom.addWidget(mic_panel, 0, Qt.AlignmentFlag.AlignBottom)

        presentation_box = QGroupBox('Presentation startup')
        presentation_layout = QVBoxLayout(presentation_box)
        presentation_layout.setSpacing(5)
        self.start_presentation_button = QPushButton('▶ Start Presentation')
        self.start_presentation_button.setMinimumHeight(34)
        self.start_presentation_button.clicked.connect(self.start_presentation)
        presentation_layout.addWidget(self.start_presentation_button)
        self.start_first_slide = QCheckBox('Start with first slide already displayed')
        self.start_first_slide.setChecked(True)
        presentation_layout.addWidget(self.start_first_slide)
        self.start_voice_following = QCheckBox('Initiate Voice Following')
        self.start_voice_following.setChecked(True)
        presentation_layout.addWidget(self.start_voice_following)
        presentation_box.setFixedWidth(250)
        bottom.addWidget(presentation_box, 0, Qt.AlignmentFlag.AlignBottom)
        page.addLayout(bottom)

        self.current = QLabel('Current group appears here.')
        self.current.setWordWrap(True)
        self.current.setStyleSheet('padding: 6px; border: 1px solid #888;')
        page.addWidget(self.current)
        self.mic_decay_timer = QTimer(self)
        self.mic_decay_timer.setInterval(250)
        self.mic_decay_timer.timeout.connect(self.update_mic_indicator)
        self.mic_decay_timer.start()
        tabs.addTab(self._scroll_page(script_page), 'Script Playback')

        settings_page = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        self.mode = QComboBox()
        self.mode.addItems(['Script', 'Speech', 'Hybrid', 'Comparison'])
        self.mode.currentTextChanged.connect(self.render)
        form.addRow('Output mode:', self.mode)
        self.grouping = QComboBox()
        self.grouping.addItems(['Phrases', 'Words', 'Sentences', 'Character limit'])
        self.grouping.currentTextChanged.connect(self.prepare_groups)
        form.addRow('Text grouping:', self.grouping)
        self.group_limit = QSpinBox()
        self.group_limit.setRange(5, 1000)
        self.group_limit.setValue(75)
        self.group_limit.valueChanged.connect(self.prepare_groups)
        form.addRow('Max characters per group:', self.group_limit)
        self.chars = QSpinBox()
        self.chars.setRange(5, 200)
        self.chars.setValue(24)
        self.chars.valueChanged.connect(self.prepare_groups)
        form.addRow('Max characters per line:', self.chars)
        self.lines = QSpinBox()
        self.lines.setRange(1, 15)
        self.lines.setValue(2)
        self.lines.valueChanged.connect(self.prepare_groups)
        form.addRow('Maximum visible lines:', self.lines)
        # Specialized pages keep canvas, typography and recording discoverable.
        canvas_page = QWidget(); canvas_form = QFormLayout(canvas_page)
        record_page = QWidget(); record_form = QFormLayout(record_page)
        text_style_page = QWidget(); text_style_form = QFormLayout(text_style_page)
        self.font_picker = QFontComboBox()
        self.font_picker.currentFontChanged.connect(self.render)
        text_style_form.addRow('Font:', self.font_picker)
        self.font_size = QSpinBox()
        self.font_size.setRange(10, 110)
        self.font_size.setValue(32)
        self.font_size.valueChanged.connect(self.render)
        text_style_form.addRow('Font size:', self.font_size)
        self.bold = QCheckBox()
        self.bold.stateChanged.connect(self.render)
        text_style_form.addRow('Bold:', self.bold)
        self.align = QComboBox()
        self.align.addItems(['Center', 'Left', 'Right'])
        self.align.currentTextChanged.connect(self.render)
        text_style_form.addRow('Text alignment:', self.align)
        self.bg_btn = QPushButton('Background: #00FF00')
        self.bg_btn.clicked.connect(self.pick_bg)
        canvas_form.addRow('Background color:', self.bg_btn)
        self.image_bg_btn = QPushButton('Choose background image…')
        self.image_bg_btn.clicked.connect(self.pick_background_image)
        canvas_form.addRow('Image background:', self.image_bg_btn)
        self.clear_image_btn = QPushButton('Remove image background')
        self.clear_image_btn.clicked.connect(self.clear_background_image)
        canvas_form.addRow('', self.clear_image_btn)
        self.layer_editor_button = QPushButton('Open Visual Layer Editor…')
        self.layer_editor_button.clicked.connect(self.open_layer_editor)
        canvas_form.addRow('Visual layer editor:', self.layer_editor_button)
        self.model_library_button = QPushButton('Open Live2D Model Library…')
        self.model_library_button.clicked.connect(self.open_model_library)
        canvas_form.addRow('Live2D models:', self.model_library_button)
        self.next_model_button = QPushButton('Next Saved Model (Ctrl+Shift+M)')
        self.next_model_button.clicked.connect(self.cycle_model)
        canvas_form.addRow('Switch character:', self.next_model_button)
        self.canvas_resolution = QComboBox()
        self.canvas_resolution.addItems(['1920x1080', '1280x720', '1080x1920', '1080x1080'])
        canvas_form.addRow('Canvas resolution:', self.canvas_resolution)
        self.canvas_resolution.currentTextChanged.connect(self.render)
        self.canvas_fps = QComboBox()
        self.canvas_fps.addItems(['24', '30', '60'])
        self.canvas_fps.setCurrentText('30')
        canvas_form.addRow('Project recording FPS:', self.canvas_fps)
        self.canvas_fps.currentTextChanged.connect(self.render)
        self.video_decode_quality = QComboBox()
        self.video_decode_quality.addItem('Performance (720p)', 720)
        self.video_decode_quality.addItem('Quality (1080p)', 1080)
        self.video_decode_quality.setToolTip('Maximum internal video-layer decoding size. Higher quality uses more CPU and memory. Image layers and canvas resolution are unaffected.')
        self.video_decode_quality.currentIndexChanged.connect(self._change_video_quality)
        canvas_form.addRow('Video layer quality:', self.video_decode_quality)
        self.performance_label = QLabel('Canvas rendering: waiting for first frame')
        self.performance_label.setWordWrap(True)
        canvas_form.addRow('Performance:', self.performance_label)
        self.performance_timer = QTimer(self)
        self.performance_timer.setInterval(750)
        self.performance_timer.timeout.connect(self._update_performance_label)
        self.performance_timer.start()
        canvas_form.addRow('', QLabel('Video quality affects image decoding, not the MP4 output dimensions.'))
        canvas_form.addRow('', QLabel('The canvas is recorded at project resolution/FPS, regardless of Output Window size.'))
        export_frame = QPushButton('Export canvas frame as PNG…')
        export_frame.clicked.connect(self.export_canvas_frame)
        canvas_form.addRow('Canvas test:', export_frame)
        self.record_microphone = QCheckBox('Include default microphone audio (experimental)')
        self.record_microphone.setChecked(True)
        record_form.addRow('Microphone:', self.record_microphone)
        self.audio_gain = QSpinBox()
        self.audio_gain.setRange(0, 400)
        self.audio_gain.setValue(100)
        self.audio_gain.setSuffix('%')
        self.audio_gain.setToolTip('Live processing for recordings and optional monitoring; does not change system volume.')
        record_form.addRow('Recording mic gain:', self.audio_gain)
        self.audio_gate_threshold = QSpinBox()
        self.audio_gate_threshold.setRange(-60, -15)
        self.audio_gate_threshold.setValue(-45)
        self.audio_gate_threshold.setSuffix(' dBFS')
        record_form.addRow('Gate threshold:', self.audio_gate_threshold)
        self.audio_clip_drive = QSpinBox()
        self.audio_clip_drive.setRange(100, 500)
        self.audio_clip_drive.setValue(150)
        self.audio_clip_drive.setSuffix('%')
        record_form.addRow('Soft Clip drive:', self.audio_clip_drive)
        self.audio_chain = QListWidget()
        self.audio_chain.setMaximumHeight(160)
        self.audio_chain.setToolTip('Top to bottom processing order. Uncheck any effect to bypass it.')
        record_form.addRow('Mic effects chain:', self.audio_chain)
        edit_mic = QPushButton('Microphone EQ / Noise Cleanup…')
        edit_mic.clicked.connect(self.edit_microphone_effects)
        record_form.addRow('', edit_mic)
        chain_buttons = QWidget()
        chain_buttons_row = QHBoxLayout(chain_buttons)
        chain_buttons_row.setContentsMargins(0, 0, 0, 0)
        move_up = QPushButton('Move Up')
        move_down = QPushButton('Move Down')
        move_up.clicked.connect(lambda: self._move_audio_effect(-1))
        move_down.clicked.connect(lambda: self._move_audio_effect(1))
        chain_buttons_row.addWidget(move_up)
        chain_buttons_row.addWidget(move_down)
        add_vst3 = QPushButton('Add VST3…')
        add_vst3.clicked.connect(self._add_vst3)
        chain_buttons_row.addWidget(add_vst3)
        edit_vst3 = QPushButton('Edit VST3 Parameters…')
        edit_vst3.clicked.connect(self._edit_vst3_parameters)
        chain_buttons_row.addWidget(edit_vst3)
        remove_vst3 = QPushButton('Remove VST3')
        remove_vst3.clicked.connect(self._remove_vst3)
        chain_buttons_row.addWidget(remove_vst3)
        record_form.addRow('', chain_buttons)
        self.audio_chain.itemChanged.connect(self._update_live_record_settings)
        self._set_audio_chain(DEFAULT_CHAIN)
        self.audio_processing_status = QLabel('Built-in EQ, Noise Suppression, Gain, smooth Noise Gate and Soft Clip can be bypassed or reordered. '
            'Live monitoring and recording share this chain. VST3 plugins are experimental; generic parameters can be edited. Native plugin UIs and VST2 are not supported.')
        self.audio_processing_status.setWordWrap(True)
        record_form.addRow('', self.audio_processing_status)
        self.vst3_status = QLabel()
        self.vst3_status.setWordWrap(True)
        record_form.addRow('VST3 status:', self.vst3_status)
        self.audio_diagnostics_label = QLabel('Waiting for microphone processing…')
        self.audio_diagnostics_label.setWordWrap(True)
        record_form.addRow('Audio diagnostics:', self.audio_diagnostics_label)
        reset_audio_diag_button = QPushButton('Reset Audio Diagnostics')
        reset_audio_diag_button.clicked.connect(lambda: (reset_audio_diagnostics(), self._refresh_audio_diagnostics()))
        record_form.addRow('', reset_audio_diag_button)
        self.audio_diag_timer = QTimer(self)
        self.audio_diag_timer.setInterval(1000)
        self.audio_diag_timer.timeout.connect(self._refresh_audio_diagnostics)
        self.audio_diag_timer.start()

        self.vst3_timer = QTimer(self)
        self.vst3_timer.setInterval(1500)
        self.vst3_timer.timeout.connect(self._refresh_vst3_status)
        self.vst3_timer.start()
        self._refresh_vst3_status()
        self.monitor_toggle = QCheckBox('Listen to yourself (headphones recommended)')
        self.monitor_toggle.setChecked(False)
        self.monitor_toggle.toggled.connect(self._toggle_monitor)
        record_form.addRow('Live monitoring:', self.monitor_toggle)
        self.monitor_volume = QSpinBox()
        self.monitor_volume.setRange(0, 150)
        self.monitor_volume.setValue(70)
        self._monitor_level = 70
        self.monitor_volume.valueChanged.connect(lambda value: setattr(self, '_monitor_level', value))
        self.monitor_volume.setSuffix('%')
        self.monitor_volume.setToolTip('Headphone monitor level only. Does not affect recorded volume or system audio.')
        record_form.addRow('Monitor volume:', self.monitor_volume)
        self.monitor_device = QComboBox()
        record_form.addRow('Playback device:', self.monitor_device)
        refresh_output = QPushButton('Refresh playback devices')
        refresh_output.clicked.connect(self._refresh_monitor_devices)
        record_form.addRow('', refresh_output)
        self.monitor_status = QLabel('Monitoring off — use headphones, not speakers, to prevent feedback.')
        self.monitor_status.setWordWrap(True)
        record_form.addRow('', self.monitor_status)
        self._refresh_monitor_devices()
        self.monitor_device.currentIndexChanged.connect(self._change_monitor_device)
        self.audio_gain.valueChanged.connect(self._update_live_record_settings)
        self.audio_gate_threshold.valueChanged.connect(self._update_live_record_settings)
        self.audio_clip_drive.valueChanged.connect(self._update_live_record_settings)
        self.record_folder = QPushButton('Choose recording folder…')
        self.record_folder.clicked.connect(self._choose_record_folder)
        record_form.addRow('Recordings folder:', self.record_folder)
        self.ffmpeg_browse = QPushButton('Browse for ffmpeg.exe…')
        self.ffmpeg_browse.clicked.connect(self._choose_ffmpeg)
        record_form.addRow('FFmpeg location:', self.ffmpeg_browse)
        self.ffmpeg_label = QLabel()
        self.ffmpeg_label.setWordWrap(True)
        record_form.addRow('Encoder found:', self.ffmpeg_label)
        self.update_ffmpeg_label()
        self.record_toggle = QPushButton('● Start Canvas Recording')
        self.record_toggle.clicked.connect(self._toggle_recording)
        record_form.addRow('Recorder:', self.record_toggle)
        self.record_status = QLabel('Ready — FFmpeg required. Save as MP4 (H.264 + optional AAC audio).')
        self.record_status.setWordWrap(True)
        record_form.addRow('Status:', self.record_status)
        self.record_post = QCheckBox('Preview text only; align recorded speech and export text afterward')
        self.record_post.setToolTip('Script mode + microphone required. First use downloads a local speech model. '
            'Clean MP4 and source assets are preserved; final text uses presentation settings saved at recording start.')
        self.record_post.setChecked(True)
        record_form.addRow('Text timing:', self.record_post)

        self.fg_btn = QPushButton('Text: #000000')
        self.fg_btn.clicked.connect(self.pick_fg)
        text_style_form.addRow('Text color:', self.fg_btn)
        self.transparent = QCheckBox('Use transparent background (if supported by OBS capture method)')
        self.transparent.stateChanged.connect(self.render)
        canvas_form.addRow('Transparency:', self.transparent)
        legacy_animation_form = QFormLayout()
        self.animation = QComboBox()
        self.animation.addItems(['Fade', 'None'])
        self.animation.currentTextChanged.connect(self.render)
        legacy_animation_form.addRow('Entrance animation:', self.animation)
        self.duration = QSpinBox()
        self.duration.setRange(0, 3000)
        self.duration.setSingleStep(50)
        self.duration.setValue(350)
        legacy_animation_form.addRow('Legacy fade duration (ms):', self.duration)
        self.behavior = QComboBox()
        self.behavior.addItems(['Replace', 'Stack', 'Scroll', 'Timed'])
        self.behavior.currentTextChanged.connect(self.render)
        legacy_animation_form.addRow('Text behavior:', self.behavior)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.2, 30)
        self.delay.setSingleStep(0.5)
        self.delay.setValue(3.0)
        legacy_animation_form.addRow('Timed disappearance (seconds):', self.delay)
        scroll.setWidget(inner)
        v = QVBoxLayout(settings_page)
        v.addWidget(scroll)
        # Output pages are added under one top-level Output tab.

        effects_page = QWidget()
        effect_layout = QVBoxLayout(effects_page)
        effect_layout.addWidget(QLabel('Text effects are applied to the full script or a range of slide numbers.'))
        effect_form = QFormLayout()
        self.effect_scope = QComboBox()
        self.effect_scope.addItems(['Entire presentation', 'Slide range'])
        effect_form.addRow('Apply to:', self.effect_scope)
        slide_row = QHBoxLayout()
        self.effect_first = QSpinBox()
        self.effect_first.setRange(1, 99999)
        self.effect_last = QSpinBox()
        self.effect_last.setRange(1, 99999)
        slide_row.addWidget(QLabel('From'))
        slide_row.addWidget(self.effect_first)
        slide_row.addWidget(QLabel('To'))
        slide_row.addWidget(self.effect_last)
        effect_form.addRow('Slides:', slide_row)
        self.effect_outline = QSpinBox()
        self.effect_outline.setRange(0, 15)
        self.effect_outline.setValue(0)
        effect_form.addRow('Outline thickness (0 = off):', self.effect_outline)
        self.effect_outline_color = '#FFFFFF'
        self.outline_color_button = QPushButton('Outline: #FFFFFF')
        self.outline_color_button.clicked.connect(lambda: self.select_effect_color('outline'))
        effect_form.addRow('Outline color:', self.outline_color_button)
        self.effect_shadow = QCheckBox('Enable shadow')
        effect_form.addRow('Shadow:', self.effect_shadow)
        self.effect_shadow_color = '#333333'
        self.shadow_color_button = QPushButton('Shadow: #333333')
        self.shadow_color_button.clicked.connect(lambda: self.select_effect_color('shadow'))
        effect_form.addRow('Shadow color:', self.shadow_color_button)
        self.effect_shadow_x = QSpinBox()
        self.effect_shadow_x.setRange(-20, 20)
        self.effect_shadow_x.setValue(3)
        effect_form.addRow('Shadow horizontal offset:', self.effect_shadow_x)
        self.effect_shadow_y = QSpinBox()
        self.effect_shadow_y.setRange(-20, 20)
        self.effect_shadow_y.setValue(3)
        effect_form.addRow('Shadow vertical offset:', self.effect_shadow_y)
        self.effect_glow = QCheckBox('Enable glow')
        effect_form.addRow('Glow:', self.effect_glow)
        self.effect_glow_color = '#FFFF99'
        self.glow_color_button = QPushButton('Glow: #FFFF99')
        self.glow_color_button.clicked.connect(lambda: self.select_effect_color('glow'))
        effect_form.addRow('Glow color:', self.glow_color_button)
        self.effect_glow_width = QSpinBox()
        self.effect_glow_width.setRange(1, 20)
        self.effect_glow_width.setValue(5)
        effect_form.addRow('Glow radius:', self.effect_glow_width)
        effect_layout.addLayout(effect_form)
        apply_effect = QPushButton('Apply Visual Effects')
        apply_effect.clicked.connect(self.apply_visual_effects)
        effect_layout.addWidget(apply_effect)
        ignore_global = QPushButton('Ignore Global Effects for Slide Range')
        ignore_global.setToolTip('Keep the global effects unchanged, but do not apply them to these slides.')
        ignore_global.clicked.connect(self.ignore_global_effects)
        effect_layout.addWidget(ignore_global)
        restore_global = QPushButton('Restore Global Effects for Slide Range')
        restore_global.setToolTip('Allow these slides to inherit the presentation-wide effects again.')
        restore_global.clicked.connect(self.restore_global_effects)
        effect_layout.addWidget(restore_global)
        remove_effect = QPushButton('Remove Slide-Specific Effects from Range')
        remove_effect.setToolTip('Delete only the individually assigned effects for these slides.')
        remove_effect.clicked.connect(self.remove_visual_overrides)
        effect_layout.addWidget(remove_effect)
        self.effect_status = QLabel('No effects configured yet. Changes are previewed after Apply.')
        self.effect_status.setWordWrap(True)
        effect_layout.addWidget(self.effect_status)
        effect_layout.addStretch()
        # Output > Text Effects

        animation_page = QWidget()
        animation_scroll = QScrollArea()
        animation_scroll.setWidgetResizable(True)
        animation_inner = QWidget()
        animation_layout = QVBoxLayout(animation_inner)
        animation_layout.addWidget(QLabel('Set independent entrance and exit animations. Letter, word, or phrase timing can be customized.'))
        animation_form = QFormLayout()
        self.motion_scope = QComboBox()
        self.motion_scope.addItems(['Entire presentation', 'Slide range'])
        animation_form.addRow('Apply to:', self.motion_scope)
        self.motion_intro = QComboBox()
        self.motion_intro.addItems(ANIMATION_OPTIONS)
        self.motion_intro.setCurrentText('Fade')
        animation_form.addRow('Entrance:', self.motion_intro)
        self.motion_outro = QComboBox()
        self.motion_outro.addItems(ANIMATION_OPTIONS)
        self.motion_outro.setCurrentText('Fade')
        animation_form.addRow('Exit:', self.motion_outro)
        self.motion_unit = QComboBox()
        self.motion_unit.addItems(['Phrase', 'Word', 'Letter'])
        animation_form.addRow('Animate by:', self.motion_unit)
        self.motion_intro_ms = QSpinBox()
        self.motion_intro_ms.setRange(0, 5000)
        self.motion_intro_ms.setValue(350)
        self.motion_intro_ms.setSuffix(' ms')
        animation_form.addRow('Entrance duration per unit:', self.motion_intro_ms)
        self.motion_outro_ms = QSpinBox()
        self.motion_outro_ms.setRange(0, 5000)
        self.motion_outro_ms.setValue(240)
        self.motion_outro_ms.setSuffix(' ms')
        animation_form.addRow('Exit duration per unit:', self.motion_outro_ms)
        self.motion_stagger = QSpinBox()
        self.motion_stagger.setRange(0, 700)
        self.motion_stagger.setValue(65)
        self.motion_stagger.setSuffix(' ms')
        animation_form.addRow('Delay between units:', self.motion_stagger)
        self.motion_hold = QDoubleSpinBox()
        self.motion_hold.setRange(0.0, 10.0)
        self.motion_hold.setSingleStep(0.25)
        self.motion_hold.setDecimals(2)
        self.motion_hold.setValue(0.5)
        self.motion_hold.setSuffix(' seconds')
        animation_form.addRow('Minimum hold after entrance:', self.motion_hold)
        self.motion_hold.setToolTip('In voice-following, the current slide remains visible for at least this long after its entrance completes.')
        self.motion_speech_gate = QCheckBox('Reveal only spoken words during voice-following')
        self.motion_speech_gate.setChecked(True)
        animation_form.addRow('Speech-gated reveal:', self.motion_speech_gate)
        motion_range = QHBoxLayout()
        self.motion_first = QSpinBox()
        self.motion_first.setRange(1, 99999)
        self.motion_last = QSpinBox()
        self.motion_last.setRange(1, 99999)
        motion_range.addWidget(QLabel('From'))
        motion_range.addWidget(self.motion_first)
        motion_range.addWidget(QLabel('To'))
        motion_range.addWidget(self.motion_last)
        animation_form.addRow('Slide range:', motion_range)
        animation_layout.addLayout(legacy_animation_form)
        animation_layout.addLayout(animation_form)
        motion_apply = QPushButton('Apply Animations')
        motion_apply.clicked.connect(self.apply_animations)
        animation_layout.addWidget(motion_apply)
        motion_remove = QPushButton('Remove Slide-Range Animation Overrides')
        motion_remove.clicked.connect(self.remove_animation_overrides)
        animation_layout.addWidget(motion_remove)
        self.motion_status = QLabel('Select and apply animations. Existing text will animate on the next slide transition.')
        self.motion_status.setWordWrap(True)
        animation_layout.addWidget(self.motion_status)
        test_box = QGroupBox('Animation preview & five-slide reading test')
        test_layout = QVBoxLayout(test_box)
        test_layout.addWidget(QLabel('Preview text (does not change your script or live OBS output):'))
        self.animation_sample = QLineEdit('The ancient kingdom hides a remarkable secret.')
        test_layout.addWidget(self.animation_sample)
        preview_row = QHBoxLayout()
        preview_button = QPushButton('Preview Current Animation')
        preview_button.clicked.connect(self.preview_animation)
        preview_row.addWidget(preview_button)
        test_button = QPushButton('Run 5-Slide Transition Test')
        test_button.clicked.connect(self.start_animation_test)
        preview_row.addWidget(test_button)
        stop_test_button = QPushButton('Stop Test')
        stop_test_button.clicked.connect(self.stop_animation_test)
        preview_row.addWidget(stop_test_button)
        test_layout.addLayout(preview_row)
        self.animation_test_status = QLabel('Preview and test use your current animation controls.')
        test_layout.addWidget(self.animation_test_status)
        animation_layout.addWidget(test_box)
        animation_layout.addStretch()
        animation_scroll.setWidget(animation_inner)
        animation_page_layout = QVBoxLayout(animation_page)
        animation_page_layout.addWidget(animation_scroll)
        # Output > Animations

        self.speech_demo = QWidget()
        dm = QVBoxLayout(self.speech_demo)
        model_box = QGroupBox('Offline English Speech Model')
        model_box_layout = QVBoxLayout(model_box)
        self.model_test_status = QLabel('Checking installed model…')
        model_box_layout.addWidget(self.model_test_status)
        dm.addWidget(model_box)
        dm.addWidget(QLabel('Speech/Hybrid comparison test (manual simulation):'))
        dm.addWidget(QLabel('Enter what the recognition engine might have heard for the current script group.'))
        self.spoken = QLineEdit()
        self.spoken.setPlaceholderText('e.g. The ancient kingdom of Veloria')
        self.spoken.textChanged.connect(self.render)
        dm.addWidget(self.spoken)
        dm.addWidget(QLabel('In v0.1, Hybrid chooses script text when sufficiently similar, otherwise spoken text.'))
        dm.addStretch()
        # Voice Tracking > Speech Test

        voice_page = QWidget()
        voice_layout = QVBoxLayout(voice_page)
        voice_layout.addWidget(QLabel('One-time setup: download the free English speech model (~40 MB). Recognition runs locally after installation.'))
        self.model_button = QPushButton('Download Offline English Model')
        self.model_button.clicked.connect(self.download_model)
        voice_layout.addWidget(self.model_button)
        self.model_status = QLabel('Checking model installation…')
        voice_layout.addWidget(self.model_status)
        self.model_progress = QProgressBar()
        self.model_progress.setValue(0)
        voice_layout.addWidget(self.model_progress)
        voice_layout.addWidget(QLabel('Microphone:'))
        mic_row = QHBoxLayout()
        self.microphones = QComboBox()
        mic_row.addWidget(self.microphones, 1)
        refresh = QPushButton('Refresh microphones')
        refresh.clicked.connect(self.refresh_microphones)
        mic_row.addWidget(refresh)
        voice_layout.addLayout(mic_row)
        self.listen_button = QPushButton('Start Voice Following')
        self.listen_button.clicked.connect(self.toggle_listening)
        voice_layout.addWidget(self.listen_button)
        self.tracking_mode = QComboBox()
        self.tracking_mode.addItems(['Automatic', 'Manual', 'Hybrid (voice + manual correction)'])
        voice_layout.addWidget(QLabel('Tracking mode (Automatic is default):'))
        voice_layout.addWidget(self.tracking_mode)
        self.voice_status = QLabel('Microphone not started')
        self.voice_status.setWordWrap(True)
        voice_layout.addWidget(self.voice_status)
        voice_layout.addWidget(QLabel('Live recognized speech:'))
        self.heard_display = QLabel('(waiting)')
        self.heard_display.setWordWrap(True)
        self.heard_display.setMinimumHeight(65)
        self.heard_display.setStyleSheet('border: 1px solid #888; padding: 8px;')
        voice_layout.addWidget(self.heard_display)
        voice_layout.addWidget(QLabel('Matching is conservative: if the engine is uncertain, use Previous/Next to correct the location.'))
        voice_layout.addStretch()
        # Voice Tracking uses a nested tab group.
        self.refresh_microphones()

        # Four top-level tabs. Tabs are rearranged here so the controls and
        # signal connections above remain unchanged from v0.2.12.
        voice_tabs = QTabWidget()
        voice_tabs.addTab(self._scroll_page(voice_page), 'Voice Tracking')
        voice_tabs.addTab(self._scroll_page(self.speech_demo), 'Speech Test')
        tabs.addTab(voice_tabs, 'Voice Tracking')
        output_tabs = QTabWidget()
        output_tabs.addTab(self._scroll_page(settings_page), 'Content')
        output_tabs.addTab(self._scroll_page(canvas_page), 'Canvas & Layers')
        output_tabs.addTab(self._scroll_page(record_page), 'Recording')
        # A single compact two-column Text Effects page, with a full-width sample.
        combined_text_page = QWidget()
        combined_layout = QVBoxLayout(combined_text_page)
        side_by_side = QHBoxLayout()
        side_by_side.setSpacing(12)
        typography_group = QGroupBox('Typography')
        typography_column = QVBoxLayout(typography_group)
        typography_column.addWidget(text_style_page)
        effects_group = QGroupBox('Effects & Slide Overrides')
        effects_column = QVBoxLayout(effects_group)
        effects_column.addWidget(effects_page)
        side_by_side.addWidget(typography_group, 1)
        side_by_side.addWidget(effects_group, 1)
        combined_layout.addLayout(side_by_side)
        sample_group = QGroupBox('Live text preview')
        sample_layout = QVBoxLayout(sample_group)
        self.text_effects_sample = EffectsSample('AaBb YyZz')
        self.text_effects_sample.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_effects_sample.setMinimumHeight(150)
        sample_layout.addWidget(self.text_effects_sample)
        combined_layout.addWidget(sample_group)
        combined_layout.addStretch()
        for form in (text_style_form, effect_form):
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
            for row_number in range(form.rowCount()):
                field = form.itemAt(row_number, QFormLayout.ItemRole.FieldRole)
                if field and field.widget():
                    field.widget().setMaximumWidth(235)
        self.font_picker.setMaximumWidth(235)
        for control in (self.font_picker, self.font_size, self.bold, self.align,
                        self.effect_outline, self.effect_shadow, self.effect_glow,
                        self.effect_glow_width, self.effect_shadow_x, self.effect_shadow_y):
            signal = getattr(control, 'valueChanged', None) or getattr(control, 'stateChanged', None) or getattr(control, 'currentFontChanged', None)
            if signal: signal.connect(self.update_text_effects_sample)
        self.fg_btn.clicked.connect(lambda: QTimer.singleShot(0, self.update_text_effects_sample))
        for button in (self.outline_color_button, self.shadow_color_button, self.glow_color_button):
            button.clicked.connect(self.update_text_effects_sample)
        self.update_text_effects_sample()
        output_tabs.addTab(self._scroll_page(combined_text_page), 'Text Effects')
        output_tabs.addTab(self._scroll_page(animation_page), 'Animations')
        tabs.addTab(output_tabs, 'Output')
        settings_home = QWidget()
        settings_home_layout = QVBoxLayout(settings_home)
        settings_home_layout.addWidget(QLabel('Application settings and project information'))
        settings_home_layout.addWidget(self.project_label)
        settings_home_layout.addWidget(self.dictionary_button)
        save_display = QPushButton('Save Display Settings as Defaults')
        save_display.clicked.connect(self.save_settings)
        settings_home_layout.addWidget(save_display)
        settings_home_layout.addStretch()
        tabs.addTab(self._scroll_page(settings_home), 'Settings')
        self.build_menus()

        status = QHBoxLayout()
        show = QPushButton('Show Output Window')
        show.clicked.connect(self.out.show)
        # Accessed through View menu.
        config = QPushButton('Save Display Settings')
        config.clicked.connect(self.save_settings)
        # Accessed through Settings.
        # Window visibility and display settings are now in View and Settings.
        QShortcut(QKeySequence('Ctrl+Right'), self, self.next)
        QShortcut(QKeySequence('Ctrl+Left'), self, self.previous)

    def build_menus(self):
        menu = self.menuBar()
        file_menu = menu.addMenu('&File')
        file_menu.addAction('Open Script…', self.open_script)
        file_menu.addAction('Save Script…', self.save_script)
        file_menu.addSeparator()
        file_menu.addAction('Open Project…', self.open_project)
        save_action = file_menu.addAction('Save Project', lambda: self.save_project())
        # A main-window shortcut also works when the menu bar is hidden.
        file_menu.addAction('Save Project As…', lambda: self.save_project(save_as=True))
        file_menu.addSeparator()
        file_menu.addAction('Exit', self.close)

        edit_menu = menu.addMenu('&Edit')
        spell_action = edit_menu.addAction('Check Spelling')
        spell_action.setCheckable(True)
        spell_action.setChecked(self.spell_toggle.isChecked())
        spell_action.toggled.connect(self.spell_toggle.setChecked)
        self.spell_toggle.toggled.connect(spell_action.setChecked)
        grammar_action = edit_menu.addAction('Check Grammar')
        grammar_action.setCheckable(True)
        grammar_action.setChecked(self.grammar_toggle.isChecked())
        grammar_action.toggled.connect(self.grammar_toggle.setChecked)
        self.grammar_toggle.toggled.connect(grammar_action.setChecked)
        edit_menu.addSeparator()
        edit_menu.addAction('Dictionary…', self.manage_dictionary)

        view_menu = menu.addMenu('&View')
        view_menu.addAction('Show Output Window', self._show_obs_window)
        view_menu.addAction('Show Reader Teleprompter', self.show_reader)
        view_menu.addAction('Bring All Display Windows to Front', self.bring_windows_to_front)
        view_menu.addAction('Organize Windows', self.organize_windows)
        view_menu.addSeparator()
        obs_lock = view_menu.addAction('Keep Output Window on Top')
        obs_lock.setCheckable(True)
        obs_lock.toggled.connect(lambda on: self._lock_window(self.out, on))
        reader_lock = view_menu.addAction('Keep Reader Window on Top')
        reader_lock.setCheckable(True)
        reader_lock.toggled.connect(lambda on: self._lock_window(self.reader, on))

        help_menu = menu.addMenu('&Help')
        from update_ui import UpdateController
        self.updater = UpdateController(self, help_menu)
        file_menu.addAction('Review / Align Recorded Presentation…', self.open_alignment)
        help_menu.addAction('About Dynamic Teleprompter', lambda: QMessageBox.information(
            self, 'About Dynamic Teleprompter',
            f'Dynamic Teleprompter {APP_VERSION}\n\n{APP_NEWS}'))
        # Classic Alt-to-show menu; no permanent menu row consuming screen space.
        self._menu_open = False
        self._alt_down = False
        menu.hide()
        for sub in (file_menu, edit_menu, view_menu, help_menu):
            sub.aboutToShow.connect(self._show_menu_temporarily)
            sub.aboutToHide.connect(self._hide_menu_after_action)
        QApplication.instance().installEventFilter(self)
        QShortcut(QKeySequence('Ctrl+S'), self, lambda: self.save_project())
        QShortcut(QKeySequence('Ctrl+Shift+M'), self, self.cycle_model)


    def _show_menu_temporarily(self):
        self.menuBar().show()
        self._menu_open = True

    def _hide_menu_after_action(self):
        self._menu_open = False
        QTimer.singleShot(0, self._hide_menu_if_idle)

    def _hide_menu_if_idle(self):
        if not self._menu_open and not any(m.isVisible() for m in self.menuBar().findChildren(QMenu)):
            self.menuBar().hide()

    def _movement_layer(self):
        # Respect the selected layer in the editor, then the active saved character.
        editor = self.layer_editor
        if editor is not None and editor.isVisible() and editor.items.currentRow() >= 0:
            row = editor.items.currentRow()
            if row < len(self.media_layers) and self.media_layers[row].get('type') == 'live2d':
                return self.media_layers[row]
        idx = self._live2d_target_index()
        return self.media_layers[idx] if idx is not None else None

    def _tick_movement(self):
        now = time.monotonic()
        # Never accumulate a long blocked-UI interval into a jump in position.
        # After expensive Live2D rendering, resume with one bounded step instead.
        dt = min(0.033, max(0.0, now - self._movement_last_tick))
        self._movement_last_tick = now
        layer = self._movement_layer()
        if layer is None or not layer.get('movement', {}).get('enabled', False) or layer.get('locked'):
            self._movement_keys.clear()
            return
        # Release held keys when another application is activated (prevents stuck motion).
        active = QApplication.activeWindow()
        permitted = (self, self.out, self.layer_editor)
        keys = self._movement_keys if active in permitted else set()
        if active not in permitted:
            self._movement_keys.clear()
        if step_motion(layer, keys, dt):
            if self.layer_editor is not None and self.layer_editor.isVisible():
                self.layer_editor.preview.update()
            # The preview/recorder timer renders the changed layer; avoid resetting text animations.
            self.out._dirty = True

    def eventFilter(self, watched, event):
        # WASD is handled at the app level so it works while the Output window is active.
        kind = event.type()
        if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            keymap = {Qt.Key.Key_A:'a', Qt.Key.Key_D:'d', Qt.Key.Key_W:'w', Qt.Key.Key_S:'s'}
            key = keymap.get(event.key())
            active = QApplication.activeWindow()
            if key and not event.isAutoRepeat() and active in (self, self.out, self.layer_editor):
                focus = QApplication.focusWidget()
                typing = isinstance(focus, (QTextEdit, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox))
                modifiers = event.modifiers()
                if not typing and not (modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)):
                    layer = self._movement_layer()
                    if layer and layer.get('movement', {}).get('enabled', False):
                        if kind == QEvent.Type.KeyPress: self._movement_keys.add(key)
                        else: self._movement_keys.discard(key)
                        return True
                if kind == QEvent.Type.KeyRelease:
                    self._movement_keys.discard(key)
        # Event filter on QApplication catches Alt even while script editor has focus.
        kind = event.type()
        if kind == QEvent.Type.KeyPress and (event.modifiers() & Qt.KeyboardModifier.AltModifier):
            menu_index = {Qt.Key.Key_F: 0, Qt.Key.Key_E: 1, Qt.Key.Key_V: 2, Qt.Key.Key_H: 3}.get(event.key())
            if menu_index is not None:
                bar = self.menuBar()
                bar.show()
                action = bar.actions()[menu_index]
                bar.setActiveAction(action)
                action.menu().popup(bar.mapToGlobal(bar.actionGeometry(action).bottomLeft()))
                return True
        if kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Alt and not event.isAutoRepeat():
            self._alt_down = True
            return False
        if kind == QEvent.Type.KeyRelease and event.key() == Qt.Key.Key_Alt and not event.isAutoRepeat():
            self._alt_down = False
            if self.menuBar().isVisible():
                self.menuBar().hide()
            else:
                self.menuBar().show()
                self.menuBar().setFocus()
            return True
        if kind == QEvent.Type.MouseButtonPress and self.menuBar().isVisible():
            # Click outside the menu hides it after the click completes.
            if isinstance(watched, QWidget) and watched.window() not in [self.menuBar(), self]:
                pass
            elif isinstance(watched, QWidget) and (watched is self.menuBar() or isinstance(watched, QMenu)):
                pass
            else:
                QTimer.singleShot(0, self._hide_menu_if_idle)
        return super().eventFilter(watched, event)

    @staticmethod
    def _outer_frame_margins(window):
        """Return actual decorated-frame margins around Qt's client geometry.

        Qt's setGeometry() sets the *client* rectangle for top-level windows;
        frameGeometry() includes the OS title bar and resize borders.
        """
        client = window.geometry()
        frame = window.frameGeometry()
        return (
            max(0, client.left() - frame.left()),
            max(0, client.top() - frame.top()),
            max(0, frame.right() - client.right()),
            max(0, frame.bottom() - client.bottom()),
        )

    @staticmethod
    def _fit_window_frame(window, x, y, width, height):
        """Set an exact outer frame rectangle, keeping the title bar in view."""
        left, top, right, bottom = MainWindow._outer_frame_margins(window)
        client_w = max(1, width - left - right)
        client_h = max(1, height - top - bottom)
        # Qt enforces widgets' minimum dimensions: suppress inflated layout
        # minimums for the compact half-screen recording arrangement.
        window.setMinimumSize(1, 1)
        window.setGeometry(x + left, y + top, client_w, client_h)

    def organize_windows(self):
        """Tile complete decorated window frames across the upper work area."""
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()  # Logical pixels; excludes taskbar.
        x, y, width, height = area.x(), area.y(), area.width(), area.height()
        left_width = width // 2
        right_width = width - left_width
        total_height = height // 2
        reader_height = total_height // 2
        output_height = total_height - reader_height

        for window in (self, self.reader, self.out):
            if window.isMinimized() or window.isMaximized():
                window.showNormal()
            else:
                window.show()
        # Let Windows update native decoration measurements before tiling.
        QApplication.processEvents()
        self._fit_window_frame(self, x, y, left_width, total_height)
        self._fit_window_frame(self.reader, x + left_width, y, right_width, reader_height)
        self._fit_window_frame(self.out, x + left_width, y + reader_height, right_width, output_height)
        for window in (self.reader, self.out):
            window.raise_()
        self.raise_()
        self.activateWindow()

    def _show_obs_window(self):
        if self.out.isMinimized():
            self.out.showNormal()
        self.out.show()
        self.out.raise_()
        self.out.activateWindow()

    def _lock_window(self, window, enabled):
        # Modifying a Qt top-level window flag can briefly hide the window.
        # Restore its geometry and visibility rather than resetting output.
        geometry = window.geometry()
        window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        window.setGeometry(geometry)
        window.show()
        window.raise_()

    def _preview_window(self):
        if self.animation_test_window is None:
            self.animation_test_window = OutputWindow()
            self.animation_test_window.setWindowTitle('Dynamic Teleprompter — Animation Test (not OBS)')
            self.animation_test_window.resize(660, 300)
        self.animation_test_window.show()
        self.animation_test_window.raise_()
        return self.animation_test_window

    def _test_settings(self):
        values = self.settings()
        values['mode'] = 'Script'
        values['behavior'] = 'Replace'
        values['motion'] = self.motion_from_controls()
        values['visual'] = self.visual_global.copy()
        values['instant'] = False
        values['incremental'] = False
        return values

    def preview_animation(self):
        self.stop_animation_test()
        sample = self.animation_sample.text().strip() or 'Animation preview text.'
        win = self._preview_window()
        # Clear first so the same sample can replay its entrance effect.
        win.set_output(['', '', ''], {**self._test_settings(), 'instant': True})
        win.set_output([sample, '', ''], self._test_settings(), layout_templates=[sample])
        self.animation_test_status.setText('Showing sample animation in separate preview window.')

    def start_animation_test(self):
        self.stop_animation_test()
        if not model_is_installed():
            QMessageBox.information(self, 'Speech model required',
                                    'Install the English model to run a voice-controlled reading test.')
            return
        if self.voice_worker is not None and self.voice_worker.isRunning():
            self.stop_listening()
            # Give PortAudio time to release the capture device before reopening it.
            self.voice_worker.wait(1600)
            if self.voice_worker.isRunning():
                QMessageBox.warning(self, 'Microphone busy',
                                    'Wait for normal voice following to stop, then retry the test.')
                return
            self.animation_test_status.setText('Main voice following stopped for the isolated reading test.')
        win = self._preview_window()
        self.animation_test_index = 0
        self.test_cursor = 0
        self.test_heard_final = ''
        self._test_rendered_prefix = None
        win.set_output(['', '', ''], {**self._test_settings(), 'instant': True})
        self.test_voice_worker = VoiceWorker(self.microphones.currentData())
        self.test_voice_worker.recognized.connect(self._on_animation_test_recognized)
        self.test_voice_worker.failed.connect(self._on_animation_test_failed)
        self.test_voice_worker.finished.connect(self._animation_test_worker_finished)
        self.test_voice_worker.start()
        self.test_advance_timer.start()
        self.animation_test_status.setText('Listening: read slide 1 aloud. The preview follows your speech.')

    def _on_animation_test_failed(self, message):
        QMessageBox.warning(self, 'Animation test microphone error', message)
        self.stop_animation_test()

    def _animation_test_worker_finished(self):
        self.test_advance_timer.stop()

    def _on_animation_test_recognized(self, heard, final):
        if self.animation_test_index < 0:
            return
        if final:
            self.test_heard_final = (self.test_heard_final + ' ' + heard).strip()
        transcript = (self.test_heard_final + ('' if final else ' ' + heard)).strip()
        script = normalized_words(' '.join(self.test_groups))
        tokens = normalized_words(transcript)
        if not tokens:
            return
        # Align a short recognized suffix to upcoming words, without jumping
        # ahead more than a few words on one recognition update.
        recent = tokens[-12:]
        begin = max(0, self.test_cursor - 4)
        finish = min(len(script), self.test_cursor + 14)
        matcher = SequenceMatcher(None, script[begin:finish], recent, autojunk=False)
        matches = [m for m in matcher.get_matching_blocks() if m.size]
        if matches:
            end = max(begin + m.a + m.size for m in matches)
            if end <= self.test_cursor + 12:
                self.test_cursor = max(self.test_cursor, end)
        self._animation_test_progress()

    def _animation_test_progress(self):
        if self.animation_test_index < 0 or self.animation_test_index >= len(self.test_groups):
            self.test_advance_timer.stop()
            return
        index = self.animation_test_index
        before = sum(len(normalized_words(g)) for g in self.test_groups[:index])
        group = self.test_groups[index]
        count = len(normalized_words(group))
        spoken = max(0, min(count, self.test_cursor - before))
        motion = self.motion_from_controls()
        # The regular renderer is used with the same animation values and reveal settings.
        speech_gate = motion.get('speech_gate', True)
        prefix = ' '.join(group.split()[:spoken]) if speech_gate else (group if spoken else '')
        if prefix != self._test_rendered_prefix:
            self._test_rendered_prefix = prefix
            settings = self._test_settings()
            settings['incremental'] = bool(speech_gate)
            self.animation_test_window.set_output([prefix, '', ''], settings,
                                                  transition_key=('reading-test', index),
                                                  layout_templates=[group])
        self.animation_test_status.setText(
            f'Reading test slide {index + 1}/5 — {spoken}/{count} words recognized')
        if spoken < count:
            return
        labels = self.animation_test_window.labels[:1]
        if not labels or any(label._phase != 'idle' or label._pending is not None for label in labels):
            return
        ready = max(label._completed_at for label in labels)
        if time.monotonic() < ready + max(0, motion.get('hold_s', 0.5)):
            return
        if index == len(self.test_groups)-1:
            self.animation_test_status.setText('Five-slide voice-controlled test complete.')
            self.test_advance_timer.stop()
            if self.test_voice_worker is not None:
                self.test_voice_worker.stop()
            return
        self.animation_test_index += 1
        self._test_rendered_prefix = None

    def stop_animation_test(self):
        self.test_advance_timer.stop()
        if self.test_voice_worker is not None:
            worker = self.test_voice_worker
            worker.stop()
            # Don't block the GUI while the audio stream winds down.
            if worker.isRunning():
                worker.wait(1200)
            self.test_voice_worker = None
        self.animation_test_index = -1
        self._test_rendered_prefix = None
        if hasattr(self, 'animation_test_status'):
            self.animation_test_status.setText('Test stopped. Live OBS output was not changed.')

    def bring_windows_to_front(self):
        """Restore hidden/minimized displays and raise both without moving them."""
        for window in (self.reader, self.out):
            if window.isMinimized():
                window.showNormal()
            else:
                window.show()
            window.raise_()
        # Last activation is intentional: the OBS output stays visible on top.
        self.out.activateWindow()

    def show_reader(self):
        self.reader.show()
        self.reader.raise_()
        self.reader.activateWindow()

    def on_input_level(self, level):
        if not self.voice_enabled:
            return
        self.last_input_time = time.monotonic()
        self.last_input_level = max(0.0, level)
        self.mic_meter.setValue(round(self.last_input_level))
        self.update_mic_indicator()

    def update_mic_indicator(self):
        worker_active = self.voice_worker is not None and self.voice_worker.isRunning() and self.voice_enabled
        if time.monotonic() - self.last_input_time > 0.7:
            self.last_input_level = 0.0
            self.mic_meter.setValue(0)
        if not worker_active:
            self.mic_indicator.setText('● Stopped — microphone not listening')
            self.mic_indicator.setStyleSheet('color: #777; font-weight: bold;')
            self.mic_meter.setValue(0)
        elif self.voice_muted:
            self.mic_indicator.setText('● Muted — speech recognition paused')
            self.mic_indicator.setStyleSheet('color: #B08000; font-weight: bold;')
        elif self.last_input_level >= 3:
            self.mic_indicator.setText('● Listening — microphone input detected')
            self.mic_indicator.setStyleSheet('color: #17813A; font-weight: bold;')
        else:
            self.mic_indicator.setText('● Listening — no significant input detected')
            self.mic_indicator.setStyleSheet('color: #B05000; font-weight: bold;')

    def toggle_mute(self):
        if self.voice_worker is None or not self.voice_worker.isRunning():
            return
        self.voice_muted = not self.voice_muted
        self.voice_worker.set_muted(self.voice_muted)
        self.mute_button.setText('🔇' if self.voice_muted else '🔊')
        self.mute_button.setToolTip('Unmute listening' if self.voice_muted else 'Mute listening')
        self.update_mic_indicator()

    def refresh_microphones(self):
        self.microphones.clear()
        self.microphones.addItem('System default microphone', None)
        try:
            import sounddevice as sd
            for idx, device in enumerate(sd.query_devices()):
                if device['max_input_channels'] > 0:
                    self.microphones.addItem(f"{device['name']} ({idx})", idx)
        except Exception as exc:
            self.voice_status.setText('Could not enumerate microphones: ' + str(exc))

    def update_model_status(self):
        installed = model_is_installed()
        status = ('Installed — offline English recognition is ready.' if installed
                  else 'Not installed — download required for voice tracking.')
        self.model_status.setText(status)
        self.model_test_status.setText(status)
        self.model_button.setVisible(not installed)
        self.model_progress.setVisible(not installed)
        return installed

    def prompt_model_if_missing(self):
        if self.update_model_status():
            return
        answer = QMessageBox.question(
            self, 'Install offline English speech model?',
            'Voice following needs the free offline English model (~40 MB). '
            'Would you like to download it now? You can also download it later from Voice Tracking.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer == QMessageBox.StandardButton.Yes:
            self.download_model()

    def download_model(self):
        if self.update_model_status():
            return
        if self.downloader is not None and self.downloader.isRunning():
            return
        self.model_button.setEnabled(False)
        self.downloader = ModelDownloader(self)
        self.downloader.progress.connect(self.model_progress.setValue)
        self.downloader.completed.connect(self._model_download_succeeded)
        self.downloader.failed.connect(self._model_download_failed)
        self.downloader.finished.connect(self._model_download_finished)
        self.downloader.start()

    def _model_download_succeeded(self, message):
        self.voice_status.setText(message)
        if not self.update_model_status():
            QMessageBox.warning(self, 'Model incomplete',
                                'The download completed but required model files were not found.')

    def _model_download_failed(self, message):
        self.update_model_status()
        QMessageBox.warning(self, 'Model download failed', message)

    def _model_download_finished(self):
        self.model_button.setEnabled(True)
        self.update_model_status()

    def set_listen_button_text(self, label):
        self.listen_button.setText(label)
        active = label.startswith('Stop')
        self.quick_listen_button.setText('◉' if active else '◉̸')
        self.quick_listen_button.setToolTip('Stop voice following' if active else 'Start voice following')
        self.quick_listen_button.setStyleSheet('color:#148f47; font-weight:bold;' if active else 'color:#b35d26; font-weight:bold;')

    def start_presentation(self):
        """Prepare exactly once; opening voice recognition must not reset slide one."""
        if self.test_voice_worker is not None and self.test_voice_worker.isRunning():
            QMessageBox.information(self, 'Animation test running', 'Stop the reading test first.')
            return
        if self.start_voice_following.isChecked() and not model_is_installed():
            QMessageBox.information(self, 'Model needed', 'Install the offline English model before starting voice following.')
            return
        if not self.script.toPlainText().strip():
            QMessageBox.warning(self, 'Empty script', 'Paste a script before starting.')
            return
        self.stop_listening()
        self.prepare_groups()
        if not self.groups:
            return
        self._show_obs_window()
        self.show_reader()
        if self.start_first_slide.isChecked():
            self.next()
        if self.start_voice_following.isChecked():
            self.toggle_listening(preserve_presentation=True)
        self.start_presentation_button.setText('▶ Restart Presentation')

    def toggle_listening(self, preserve_presentation=False):
        if self.voice_worker is not None and self.voice_worker.isRunning():
            self.stop_listening()
            return
        if not model_is_installed():
            QMessageBox.information(self, 'Model needed', 'Download the offline English model first, then start listening.')
            return
        if not preserve_presentation:
            self.prepare_groups()
        if not self.groups:
            QMessageBox.warning(self, 'No script', 'Paste a script before starting.')
            return
        self.heard_final = ''
        if not preserve_presentation:
            self.voice_cursor = 0
        else:
            # Reading starts at word 1; a pre-displayed slide stays fully visible.
            self.voice_cursor = 0
        self.voice_enabled = True
        self.voice_muted = False
        self.mute_button.setText('🔊')
        self.mute_button.setEnabled(True)
        self.last_input_level = 0.0
        self.last_input_time = 0.0
        self.voice_worker = VoiceWorker(self.microphones.currentData())
        self.voice_worker.recognized.connect(self.on_recognized)
        self.voice_worker.input_level.connect(self.on_input_level)
        self.voice_worker.status.connect(self.voice_status.setText)
        self.voice_worker.failed.connect(lambda message: QMessageBox.warning(self, 'Microphone error', message))
        self.voice_worker.finished.connect(self.on_voice_finished)
        self.set_listen_button_text('Stop Voice Following')
        self.voice_worker.start()

    def stop_listening(self):
        self.voice_enabled = False
        self.voice_muted = False
        self.mute_button.setEnabled(False)
        self.mute_button.setText('🔊')
        self.last_input_level = 0.0
        self.mic_meter.setValue(0)
        self.update_mic_indicator()
        if self.voice_worker is not None:
            self.voice_worker.stop()
        self.set_listen_button_text('Start Voice Following')

    def on_voice_finished(self):
        self.voice_enabled = False
        self.voice_muted = False
        self.mute_button.setEnabled(False)
        self.mute_button.setText('🔊')
        self.last_input_level = 0.0
        self.update_mic_indicator()
        self.set_listen_button_text('Start Voice Following')

    def on_recognized(self, heard, final):
        if not self.voice_enabled or self.voice_muted:
            return
        if final:
            self.heard_final = (self.heard_final + ' ' + heard).strip()
        combined = (self.heard_final + (' ' + heard if not final else '')).strip()
        self.heard_display.setText(combined[-350:] or '(waiting)')
        self.spoken.blockSignals(True)
        self.spoken.setText(' '.join(combined.split()[-18:]))
        self.spoken.blockSignals(False)
        if self.tracking_mode.currentText() == 'Manual':
            self._speech_gated = False
            self.render()
            return
        self.align_voice(combined)
        self.render()

    def align_voice(self, heard):
        # Vosk's partial transcript changes continually; match a short trailing
        # window to a nearby forward script window, rather than jumping globally.
        script_words = normalized_words(' '.join(self.groups))
        recent = normalized_words(heard)[-14:]
        if not recent or not script_words:
            return
        # IMPORTANT: Vosk often reports just one word at the beginning of a
        # sentence. Previously we returned early on len(recent) < 2, so the
        # single-word acceptance branch below could never run. Reveal an exact
        # next-word match immediately, without allowing a one-word jump.
        if len(recent) == 1:
            if (self.voice_cursor < len(script_words)
                    and recent[0] == script_words[self.voice_cursor]):
                self.voice_cursor += 1
                self._speech_gated = True
                if not self.advance_timer.isActive():
                    self.advance_timer.start()
                self._process_voice_progress()
            return
        start = max(0, self.voice_cursor - 5)
        end = min(len(script_words), self.voice_cursor + 38)
        segment = script_words[start:end]
        matcher = SequenceMatcher(None, recent, segment, autojunk=False)
        matches = [m for m in matcher.get_matching_blocks() if m.size > 0]
        total = sum(m.size for m in matches)
        if total < 2 or total / len(recent) < .45:
            # One-word interim updates are common in slow narration. Accept
            # one recognized word only when it matches the next script word
            # exactly; never use a lone match to jump to a later location.
            if recent and self.voice_cursor < len(script_words) and recent[-1] == script_words[self.voice_cursor]:
                self.voice_cursor += 1
                self._speech_gated = True
                if not self.advance_timer.isActive():
                    self.advance_timer.start()
                self._process_voice_progress()
            return
        last = max(start + m.b + m.size for m in matches)
        if last > self.voice_cursor + 18:
            return
        self.voice_cursor = max(self.voice_cursor, last)
        self._speech_gated = True
        if not self.advance_timer.isActive():
            self.advance_timer.start()
        self._process_voice_progress()

    def _process_voice_progress(self):
        if not self.voice_enabled or self.voice_muted or not self.groups:
            self.advance_timer.stop()
            return
        self.reader.set_progress(self.voice_cursor)
        boundaries = []
        total = 0
        for group in self.groups:
            total += len(normalized_words(group))
            boundaries.append(total)
        now = time.monotonic()
        if self.awaiting_first:
            self.awaiting_first = False
            self.index = 0
            self._manual_full_slide_index = None
            self._last_voice_slide = 0
            self.render()
            return
        if self.index >= len(self.groups):
            self.advance_timer.stop()
            return
        # Advance only after the final recognized words have actually been
        # presented and their entrance + hold have completed.
        hold = max(0.0, self.motion_for_slide(self.index).get('hold_s', .5))
        # CRITICAL: render newly recognized words even after crossing the
        # current slide's final word. Previously this path skipped render(),
        # so the end of a slide could stay invisible until the NEXT slide.
        self.render()
        if self.voice_cursor < boundaries[self.index]:
            return
        # A slide cannot advance while its latest content is still pending
        # in an outro or while its entrance is still revealing those words.
        # Reading the rendered labels *after* render() also avoids using an
        # obsolete completed_at from the previous shorter prefix.
        displayed = self.out.labels if self.mode.currentText() == 'Comparison' else self.out.labels[:1]
        displayed = [label for label in displayed if not label.isHidden()]
        if not displayed:
            return
        if any(label._phase != 'idle' or label._pending is not None
               or label._target_text != label.text() for label in displayed):
            return
        ready_at = max(label._completed_at for label in displayed)
        if now < ready_at + hold:
            return
        if self.index + 1 < len(self.groups):
            self.history = self.groups[:self.index+1]
            self.index += 1
            self._manual_full_slide_index = None
            self._last_voice_slide = self.index
            self.render()
        # Keep final slide visible instead of suddenly clearing at the end.


    def toggle_spell(self, enabled):
        self.script.proofreader.check_spelling = enabled
        self.script.proofreader.rehighlight()

    def toggle_grammar(self, enabled):
        self.script.proofreader.check_grammar = enabled
        self.script.proofreader.rehighlight()

    def manage_dictionary(self):
        from PySide6.QtWidgets import QDialog, QListWidget, QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle(dictionary_display_name())
        dialog.resize(430, 380)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Custom words are saved on this computer and accepted in every script.'))
        rename_row = QHBoxLayout()
        name_edit = QLineEdit(dictionary_display_name())
        name_edit.setMaxLength(60)
        name_edit.setPlaceholderText('Dictionary name')
        rename_button = QPushButton('Rename Dictionary')
        rename_row.addWidget(name_edit, 1)
        rename_row.addWidget(rename_button)
        layout.addLayout(rename_row)
        def rename_dictionary():
            name = name_edit.text().strip()
            if not name:
                QMessageBox.warning(dialog, 'Invalid name', 'Please enter a dictionary name.')
                return
            try:
                DICTIONARY_NAME_PATH.write_text(json.dumps(name), encoding='utf-8')
            except OSError as error:
                QMessageBox.warning(dialog, 'Could not rename dictionary', str(error))
                return
            self.dictionary_button.setText(name)
            dialog.setWindowTitle(name)
        rename_button.clicked.connect(rename_dictionary)
        name_edit.returnPressed.connect(rename_dictionary)
        words = QListWidget()
        words.addItems(sorted(self.script.proofreader.custom_words))
        layout.addWidget(words)
        new_word = QLineEdit()
        new_word.setPlaceholderText('Enter a name or fantasy word')
        layout.addWidget(new_word)
        buttons = QHBoxLayout()
        add = QPushButton('Add Word')
        remove = QPushButton('Remove Selected')
        buttons.addWidget(add)
        buttons.addWidget(remove)
        layout.addLayout(buttons)

        def add_word():
            if self.script.proofreader.add_word(new_word.text()):
                words.clear()
                words.addItems(sorted(self.script.proofreader.custom_words))
                new_word.clear()
        def remove_word():
            current = words.currentItem()
            if current:
                value = current.text()
                self.script.proofreader.custom_words.discard(value)
                # Reload spell checker so removed words stop being accepted.
                self.script.proofreader.speller = SpellChecker(distance=1)
                self.script.proofreader.speller.word_frequency.load_words(self.script.proofreader.custom_words)
                try:
                    DICTIONARY_PATH.write_text(json.dumps(sorted(self.script.proofreader.custom_words), indent=2), encoding='utf-8')
                    self.script.proofreader.rehighlight()
                    words.takeItem(words.row(current))
                except OSError as error:
                    QMessageBox.warning(dialog, 'Could not save dictionary', str(error))
        add.clicked.connect(add_word)
        new_word.returnPressed.connect(add_word)
        remove.clicked.connect(remove_word)
        done = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        done.rejected.connect(dialog.accept)
        layout.addWidget(done)
        dialog.exec()

    def settings(self, *, live_media=False):
        """Use active layer objects for Output; omit runtime state when saving."""
        return dict(mode=self.mode.currentText(), grouping=self.grouping.currentText(),
                    group_limit=self.group_limit.value(), chars=self.chars.value(),
                    lines=self.lines.value(), font=self.font_picker.currentFont().family(),
                    font_size=self.font_size.value(), bold=self.bold.isChecked(),
                    align=self.align.currentText(), background=self.color_bg,
                    foreground=self.color_fg, transparent=self.transparent.isChecked(),
                    background_image=self.background_image,
                    media_layers=(self.media_layers if live_media else
                                  [{key: val for key, val in layer.items() if not key.startswith("_movement_")}
                                   for layer in self.media_layers]),
                    canvas_resolution=self.canvas_resolution.currentText(),
                    canvas_fps=int(self.canvas_fps.currentText()),
                    video_decode_quality=self.video_decode_quality.currentData(),
                    audio_chain=self._audio_chain_snapshot(),
                    audio_gain=self.audio_gain.value(),
                    audio_gate_threshold=self.audio_gate_threshold.value(),
                    audio_clip_drive=self.audio_clip_drive.value(),
                    animation=self.animation.currentText(), duration=self.duration.value(),
                    behavior=self.behavior.currentText(), delay=self.delay.value())

    def prepare_groups(self, *_):
        if getattr(self, 'voice_enabled', False):
            self.stop_listening()
        self.voice_cursor = 0
        self._voice_slide_ready_at = 0.0
        self._last_voice_slide = -1
        self._speech_gated = False
        self._manual_full_slide_index = None
        self.advance_timer.stop()
        self.timer.stop()
        self.reader.set_script(self.script.toPlainText())
        self.groups = groups_from_script(self.script.toPlainText(), self.grouping.currentText(), self.group_limit.value(), self.chars.value(), self.lines.value())
        self.index = 0
        self.awaiting_first = True
        self.history = []
        if hasattr(self, 'spoken'):
            self.spoken.clear()
        self.progress.setText(f'0 / {len(self.groups)}')
        self.current.setText('(Ready — press Next to display the first group)')
        self.clear_output()

    def sync_voice_cursor(self):
        self.voice_cursor = sum(len(normalized_words(g)) for g in self.groups[:self.index])

    def next(self):
        if self.awaiting_first:
            self.awaiting_first = False
        elif self.index < len(self.groups):
            self.history.append(self.groups[self.index])
            self.index += 1
        self.sync_voice_cursor()
        self._speech_gated = False
        self._manual_full_slide_index = self.index if self.index < len(self.groups) else None
        self._last_voice_slide = -1
        self.advance_timer.stop()
        self.reader.set_progress(self.voice_cursor)
        self.render()

    def previous(self):
        if self.awaiting_first:
            return
        self.index = max(0, self.index - 1)
        self.history = self.groups[:self.index]
        self.sync_voice_cursor()
        self._speech_gated = False
        self._manual_full_slide_index = self.index if self.index < len(self.groups) else None
        self._last_voice_slide = -1
        self.advance_timer.stop()
        self.reader.set_progress(self.voice_cursor)
        self.render()

    def clear_output(self):
        self.timer.stop()
        self._last_visible_state = None
        self.out.set_output(['', '', ''], {**self.settings(live_media=True), 'instant': True})

    def render(self, *_):
        if not hasattr(self, 'groups') or not hasattr(self, 'out'):
            return
        if self.awaiting_first:
            self.progress.setText(f'0 / {len(self.groups)}')
            self.current.setText('(Ready — press Next to display the first group)')
            self.clear_output()
            return
        active = self.index < len(self.groups)
        self.progress.setText(f'{self.index + 1 if active else len(self.groups)} / {len(self.groups)}')
        script_text = self.groups[self.index] if active else ''
        speech_gated = (active and self.index != self._manual_full_slide_index
                        and self.voice_enabled and self._speech_gated
                        and self.tracking_mode.currentText() != 'Manual'
                        and self.motion_for_slide(self.index).get('speech_gate', True))
        if speech_gated:
            before = sum(len(normalized_words(g)) for g in self.groups[:self.index])
            spoken_count = max(0, self.voice_cursor - before)
            # Include punctuation that belongs to already spoken words.
            script_text = ' '.join(self.groups[self.index].split()[:spoken_count])
        self.current.setText(script_text or '(End of script)')
        spoken_text = self.spoken.text().strip() if active and hasattr(self, 'spoken') else ''
        if active and not spoken_text and self.mode.currentText() in ('Speech', 'Comparison'):
            spoken_text = '(Waiting for speech)'
        from difflib import SequenceMatcher
        score = SequenceMatcher(None, re.sub(r'\W+', '', script_text.lower()), re.sub(r'\W+', '', spoken_text.lower())).ratio()
        hybrid_text = script_text if not spoken_text or score >= 0.7 or spoken_text.startswith('(') else spoken_text
        mode = self.mode.currentText()
        if not active:
            texts = ['', '', '']
        elif mode == 'Comparison':
            texts = [f'SCRIPT\n{script_text}', f'SPEECH\n{spoken_text}', f'HYBRID\n{hybrid_text}']
        else:
            selected = {'Script': script_text, 'Speech': spoken_text, 'Hybrid': hybrid_text}[mode]
            texts = [selected, '', '']
        behavior = self.behavior.currentText()
        if active and behavior in ('Stack', 'Scroll') and mode != 'Comparison':
            previous = self.history[-3:] if behavior == 'Stack' else self.history[-5:]
            texts[0] = '\n'.join(previous + ([texts[0]] if texts[0] else []))
        # A partial recognition update must not reset the display timeout.
        visible_state = (tuple(texts), behavior, self.delay.value())
        should_restart_timer = visible_state != getattr(self, '_last_visible_state', None)
        self._last_visible_state = visible_state
        render_settings = self.settings(live_media=True)
        render_settings['visual'] = self.visual_for_slide(self.index)
        render_settings['motion'] = self.motion_for_slide(self.index)
        render_settings['incremental'] = bool(speech_gated)
        # Reserve the *full slide's* layout before displaying a spoken prefix.
        # This prevents one-line prefixes from being vertically recentered when
        # the next line becomes visible later in the same slide.
        full_script = self.groups[self.index] if active else ''
        if mode == 'Comparison':
            templates = [f'SCRIPT\n{full_script}', f'SPEECH\n{full_script}',
                         f'HYBRID\n{full_script}']
        elif mode == 'Speech':
            templates = [full_script, '', '']
        else:
            templates = [full_script, '', '']
        if active and behavior in ('Stack', 'Scroll') and mode != 'Comparison':
            templates[0] = '\n'.join(previous + ([full_script] if full_script else []))
        self.out.set_output(texts, render_settings, transition_key=(self.index, mode, behavior),
                            layout_templates=templates)
        if behavior != 'Timed':
            self.timer.stop()
        if behavior == 'Timed' and any(texts) and should_restart_timer:
            self.timer.start(round(self.delay.value() * 1000))

    def motion_from_controls(self):
        return dict(intro=self.motion_intro.currentText(), outro=self.motion_outro.currentText(),
                    unit=self.motion_unit.currentText(), intro_ms=self.motion_intro_ms.value(),
                    outro_ms=self.motion_outro_ms.value(), stagger_ms=self.motion_stagger.value(),
                    hold_s=self.motion_hold.value(), speech_gate=self.motion_speech_gate.isChecked())

    def motion_for_slide(self, index):
        return {**ANIMATION_DEFAULT, 'hold_s': 0.5, 'speech_gate': True,
                **self.animation_global, **self.animation_slides.get(index + 1, {})}

    def apply_animations(self):
        motion = self.motion_from_controls()
        if self.motion_scope.currentText() == 'Entire presentation':
            self.animation_global = motion
            self.motion_status.setText('Updated presentation-wide animation defaults.')
        else:
            first, last = self.motion_first.value(), self.motion_last.value()
            if first > last:
                QMessageBox.warning(self, 'Invalid range', 'Start slide must be before or equal to end slide.')
                return
            for n in range(first, last + 1):
                self.animation_slides[n] = dict(motion)
            self.motion_status.setText(f'Applied animations to slides {first}–{last}.')
        self.render()

    def remove_animation_overrides(self):
        first, last = self.motion_first.value(), self.motion_last.value()
        if first > last:
            QMessageBox.warning(self, 'Invalid range', 'Start slide must be before or equal to end slide.')
            return
        for n in range(first, last + 1):
            self.animation_slides.pop(n, None)
        self.motion_status.setText(f'Removed animation overrides from slides {first}–{last}.')
        self.render()

    def select_effect_color(self, which):
        attr = f'effect_{which}_color'
        old = getattr(self, attr)
        selected = QColorDialog.getColor(QColor(old), self, f'{which.title()} color')
        if selected.isValid():
            setattr(self, attr, selected.name())
            getattr(self, f'{which}_color_button').setText(f'{which.title()}: {selected.name()}')
            self.update_text_effects_sample()

    def update_text_effects_sample(self, *_):
        """Show an immediate typography preview without applying slide overrides."""
        if not hasattr(self, 'text_effects_sample'): return
        font = QFont(self.font_picker.currentFont())
        font.setPointSize(min(72, self.font_size.value()))
        font.setBold(self.bold.isChecked())
        self.text_effects_sample.setFont(font)
        color = getattr(self, 'color_fg', '#000000')
        if not isinstance(color, str): color = '#000000'
        self.text_effects_sample.visual = {
            **self.effects_from_controls(), 'foreground': color
        }
        self.text_effects_sample.reserved_lines = 1
        self.text_effects_sample.update()

    def effects_from_controls(self):
        return dict(outline_width=self.effect_outline.value(), outline_color=self.effect_outline_color,
                    shadow=self.effect_shadow.isChecked(), shadow_color=self.effect_shadow_color,
                    shadow_x=self.effect_shadow_x.value(), shadow_y=self.effect_shadow_y.value(),
                    glow=self.effect_glow.isChecked(), glow_color=self.effect_glow_color,
                    glow_width=self.effect_glow_width.value())

    def visual_for_slide(self, index):
        number = index + 1
        global_effects = {} if number in self.global_effect_exclusions else self.visual_global
        return {**global_effects, **self.slide_visuals.get(number, {})}

    def validated_effect_range(self):
        first, last = self.effect_first.value(), self.effect_last.value()
        if first > last or last > len(self.groups):
            QMessageBox.warning(self, 'Invalid slide range',
                                f'Choose a range between 1 and {max(1, len(self.groups))}.')
            return None
        return first, last

    def apply_visual_effects(self):
        effects = self.effects_from_controls()
        if self.effect_scope.currentText() == 'Entire presentation':
            self.visual_global = effects
            self.effect_status.setText('Applied default effects to the presentation (slide overrides take priority).')
        else:
            selected_range = self.validated_effect_range()
            if selected_range is None:
                return
            first, last = selected_range
            for number in range(first, last + 1):
                self.slide_visuals[number] = dict(effects)
            self.effect_status.setText(f'Applied effects to slides {first}–{last}.')
        self.render()

    def ignore_global_effects(self):
        selected_range = self.validated_effect_range()
        if selected_range is None:
            return
        first, last = selected_range
        self.global_effect_exclusions.update(range(first, last + 1))
        self.effect_status.setText(
            f'Global effects ignored on slides {first}–{last}. Slide-specific effects remain active.')
        self.render()

    def restore_global_effects(self):
        selected_range = self.validated_effect_range()
        if selected_range is None:
            return
        first, last = selected_range
        self.global_effect_exclusions.difference_update(range(first, last + 1))
        self.effect_status.setText(f'Global effects restored on slides {first}–{last}.')
        self.render()

    def remove_visual_overrides(self):
        selected_range = self.validated_effect_range()
        if selected_range is None:
            return
        first, last = selected_range
        for number in range(first, last + 1):
            self.slide_visuals.pop(number, None)
        self.effect_status.setText(
            f'Removed slide-specific effects on slides {first}–{last}. Global exclusions are unchanged.')
        self.render()

    def _project_payload(self):
        return dict(format='DynamicTeleprompterProject', version=1,
                    script=self.script.toPlainText(), settings=self.settings(),
                    visual_global=self.visual_global,
                    slide_visuals={str(k): v for k, v in self.slide_visuals.items()},
                    global_effect_exclusions=sorted(self.global_effect_exclusions),
                    animation_global=self.animation_global,
                    animation_slides={str(k): v for k, v in self.animation_slides.items()},
                    startup=dict(first_slide=self.start_first_slide.isChecked(),
                                 voice_following=self.start_voice_following.isChecked()))

    def _project_snapshot(self):
        return json.dumps(self._project_payload(), sort_keys=True, ensure_ascii=False)

    def save_project(self, save_as=False):
        path = self.project_path
        if save_as or not path:
            selected, _ = QFileDialog.getSaveFileName(self, 'Save teleprompter project',
                                                       str(path or Path.home() / 'Untitled.teleprompter.json'),
                                                       'Teleprompter projects (*.teleprompter.json);;JSON files (*.json)')
            if not selected:
                return False
            path = Path(selected)
        payload = self._project_payload()
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
            self.project_path = path
            self.project_label.setText(path.name)
            self._saved_project_snapshot = self._project_snapshot()
            return True
        except Exception as exc:
            QMessageBox.warning(self, 'Project save error', str(exc))
            return False

    def open_project(self):
        selected, _ = QFileDialog.getOpenFileName(self, 'Open teleprompter project', '',
                                                  'Teleprompter projects (*.teleprompter.json *.json)')
        if not selected:
            return
        try:
            path = Path(selected)
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('format') != 'DynamicTeleprompterProject':
                raise ValueError('This is not a Dynamic Teleprompter project file.')
            if not isinstance(data.get('settings'), dict) or not isinstance(data.get('script'), str):
                raise ValueError('Project is missing valid script or settings data.')
            self.apply_project_payload(data)
            self.project_path = path
            self.project_label.setText(path.name)
            self.effect_status.setText(f'Loaded effects from {path.name}. Use Apply to edit them.')
            self.motion_status.setText(f'Loaded animation settings from {path.name}.')
            self._saved_project_snapshot = self._project_snapshot()
        except Exception as exc:
            QMessageBox.warning(self, 'Project open error', str(exc))

    def apply_project_payload(self, data):
        self.stop_listening()
        self.apply_settings_values(data['settings'])
        self.visual_global = dict(data.get('visual_global') or {})
        self.slide_visuals = {int(key): dict(value) for key, value in
                              (data.get('slide_visuals') or {}).items()}
        self.global_effect_exclusions = {
            int(n) for n in (data.get('global_effect_exclusions') or [])
            if isinstance(n, int) and n > 0
        }
        self.animation_global = {**ANIMATION_DEFAULT, **dict(data.get('animation_global') or {})}
        self.animation_slides = {int(k): dict(v) for k, v in (data.get('animation_slides') or {}).items()}
        self.script.setPlainText(data['script'])
        startup = data.get('startup') or {}
        self.start_first_slide.setChecked(bool(startup.get('first_slide', True)))
        self.start_voice_following.setChecked(bool(startup.get('voice_following', True)))
        self.prepare_groups()

    def session_path(self):
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'DynamicTeleprompter'
        return base / 'last-session.json'

    def autosave_session(self):
        if os.environ.get('COSMERE_CAM_NO_SESSION'):
            return
        try:
            from updates import atomic_json
            payload = dict(project=self._project_payload(),
                project_path=str(self.project_path) if self.project_path else '',
                saved_snapshot=getattr(self, '_saved_project_snapshot', None),
                index=self.index, awaiting_first=self.awaiting_first,
                cursor=self.script.textCursor().position())
            serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if serialized == self._session_last:
                return
            path = self.session_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(path, payload)
            self._session_last = serialized
        except Exception as exc:
            self.project_label.setToolTip('Session autosave could not complete: ' + str(exc))

    def restore_session(self):
        if os.environ.get('COSMERE_CAM_NO_SESSION'):
            return
        try:
            path = self.session_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding='utf-8'))
            project = data['project']
            if project.get('format') != 'DynamicTeleprompterProject':
                raise ValueError('Invalid saved session')
            self.apply_project_payload(project)
            self.project_path = Path(data['project_path']) if data.get('project_path') else None
            self.project_label.setText(self.project_path.name if self.project_path else 'Restored working session')
            self._saved_project_snapshot = data.get('saved_snapshot') or self._project_snapshot()
            self.index = max(0, min(len(self.groups), int(data.get('index', 0))))
            self.awaiting_first = bool(data.get('awaiting_first', True))
            self.history = self.groups[:self.index]
            self.sync_voice_cursor()
            cursor = self.script.textCursor()
            cursor.setPosition(max(0, min(len(self.script.toPlainText()), int(data.get('cursor', 0)))))
            self.script.setTextCursor(cursor)
            self.render()
        except Exception as exc:
            self.project_label.setToolTip('Previous session could not be restored: ' + str(exc))

    def apply_settings_values(self, values):
        # Signal blocking prevents reconstructing pages with half-loaded settings.
        widgets = ((self.mode, 'mode'), (self.grouping, 'grouping'),
                   (self.align, 'align'), (self.animation, 'animation'),
                   (self.behavior, 'behavior'), (self.group_limit, 'group_limit'),
                   (self.chars, 'chars'), (self.lines, 'lines'),
                   (self.font_size, 'font_size'), (self.duration, 'duration'),
                   (self.delay, 'delay'), (self.bold, 'bold'),
                   (self.transparent, 'transparent'))
        for widget, key in widgets:
            if key not in values:
                continue
            widget.blockSignals(True)
            try:
                if isinstance(widget, QComboBox):
                    widget.setCurrentText(str(values[key]))
                elif isinstance(widget, QCheckBox):
                    widget.setChecked(bool(values[key]))
                else:
                    widget.setValue(values[key])
            finally:
                widget.blockSignals(False)
        self.font_picker.blockSignals(True)
        self.font_picker.setCurrentFont(QFont(values.get('font', 'Arial')))
        self.font_picker.blockSignals(False)
        self.color_bg = values.get('background', '#00FF00')
        self.color_fg = values.get('foreground', '#000000')
        self.media_layers = [dict(layer) for layer in values.get('media_layers', []) if isinstance(layer, dict)]
        if not any(layer.get('type') == 'text' for layer in self.media_layers):
            self.media_layers.append(dict(type='text', name='Text Output', visible=True))
        if self.layer_editor is not None:
            self.layer_editor.refresh_items()
        self.background_image = str(values.get('background_image', ''))
        self.image_bg_btn.setText(Path(self.background_image).name if self.background_image else 'Choose background image…')
        if values.get('canvas_resolution') in ['1920x1080', '1280x720', '1080x1920', '1080x1080']:
            self.canvas_resolution.setCurrentText(values['canvas_resolution'])
        if str(values.get('canvas_fps', '30')) in ('24', '30', '60'):
            self.canvas_fps.setCurrentText(str(values['canvas_fps']))
        quality = values.get('video_decode_quality', 720)
        index = self.video_decode_quality.findData(quality)
        if index >= 0:
            self.video_decode_quality.setCurrentIndex(index)
        if 'audio_chain' in values:
            self._set_audio_chain(values['audio_chain'])
        for control, key in ((self.audio_gain, 'audio_gain'),
                             (self.audio_gate_threshold, 'audio_gate_threshold'),
                             (self.audio_clip_drive, 'audio_clip_drive')):
            if key in values:
                control.setValue(int(values[key]))
        self.bg_btn.setText(f'Background: {self.color_bg}')
        self.fg_btn.setText(f'Text: {self.color_fg}')

    def pick_bg(self):
        color = QColorDialog.getColor(QColor(self.color_bg), self, 'Choose background')
        if color.isValid():
            self.color_bg = color.name()
            self.bg_btn.setText(f'Background: {self.color_bg}')
            self.render()

    def pick_fg(self):
        color = QColorDialog.getColor(QColor(self.color_fg), self, 'Choose text color')
        if color.isValid():
            self.color_fg = color.name()
            self.fg_btn.setText(f'Text: {self.color_fg}')
            self.render()
            self.update_text_effects_sample()

    def open_script(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open script', '', 'Text files (*.txt);;All files (*)')
        if path:
            try:
                self.script.setPlainText(Path(path).read_text(encoding='utf-8-sig'))
                self.prepare_groups()
            except Exception as exc:
                QMessageBox.warning(self, 'Could not open file', str(exc))

    def save_script(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save script', 'script.txt', 'Text files (*.txt)')
        if path:
            try:
                Path(path).write_text(self.script.toPlainText(), encoding='utf-8')
            except Exception as exc:
                QMessageBox.warning(self, 'Could not save file', str(exc))

    def save_settings(self):
        try:
            SETTINGS_PATH.write_text(json.dumps(self.settings(), indent=2), encoding='utf-8')
            QMessageBox.information(self, 'Saved', f'Display settings saved to {SETTINGS_PATH}')
        except Exception as exc:
            QMessageBox.warning(self, 'Could not save settings', str(exc))

    def load_settings(self):
        if not SETTINGS_PATH.exists():
            return
        try:
            values = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
            self.apply_settings_values(values)
        except Exception:
            pass

    def closeEvent(self, event):
        updater = getattr(self, 'updater', None)
        if ((updater is not None and updater.task is not None) or getattr(self, '_post_busy', False)):
            QMessageBox.information(self, 'Work in progress', 'Wait for updating, speech alignment or export to finish before closing.')
            event.ignore()
            return
        if self._project_snapshot() != getattr(self, '_saved_project_snapshot', None):
            answer = QMessageBox.question(
                self, 'Save changes before exiting?',
                'Your project has unsaved changes. Save before closing?',
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Save and not self.save_project():
                event.ignore()
                return
        if self.downloader is not None and self.downloader.isRunning():
            QMessageBox.information(self, 'Download in progress',
                                    'Please wait for the model download to finish before closing.')
            event.ignore()
            return
        if self.recorder is not None:
            if not self.recorder.stopping:
                self._stop_recording()
            QMessageBox.information(self, 'Recording in progress',
                                    'Please wait until the recording finishes saving before exiting.')
            event.ignore()
            return
        self.autosave_session()
        self._session_timer.stop()
        self.stop_animation_test()
        if self.animation_test_window is not None:
            self.animation_test_window.close()
        self.advance_timer.stop()
        self.stop_listening()
        if self.voice_worker is not None and self.voice_worker.isRunning():
            self.voice_worker.wait(3000)
        self._stop_monitor()
        lip_microphone.stop()
        stop_tracking()
        VIDEO_FRAMES.shutdown()
        # The application outlives this window during Qt teardown. Remove the
        # Python filter before child layouts are destroyed (notably on 3.13).
        QApplication.instance().removeEventFilter(self)
        self.reader.scroll_timer.stop()
        self.reader.view.setExtraSelections([])
        self.script.proofreader.setDocument(None)
        self.script.proofreader.deleteLater()
        for label in self.out.labels:
            label.cancel_motion()
        self.out.close()
        self.reader.close()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    import os
    ready_file = os.environ.get('COSMERE_CAM_READY_FILE')
    if ready_file:
        try:
            Path(ready_file).write_text('ready', encoding='utf-8')
        except OSError:
            pass
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
