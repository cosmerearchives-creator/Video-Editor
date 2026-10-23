"""Short real Qt/FFmpeg/export check, also executed inside the frozen release."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtCore import Qt, QSettings
    from PySide6.QtWidgets import QMessageBox
    from unittest.mock import patch
    import app as app_module
    from app import CanvasScene, OutputWindow
    from post_export import TextTimeline, VideoExport
    from recorder import resolve_ffmpeg
    import cv2
    import pedalboard
    import stable_whisper
    from release_info import VERSION

    app = QApplication.instance() or QApplication([])
    ffmpeg = resolve_ffmpeg()
    assert ffmpeg and Path(ffmpeg).is_file(), 'Bundled FFmpeg not found'
    settings = dict(font_size=24, foreground='#ffffff', chars=30,
        motion=dict(intro='Fade', outro='Fade', unit='Word', intro_ms=100, outro_ms=100, stagger_ms=0))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        # Construct the real control window with isolated settings, no personal
        # projects/models and no downloads. This catches missing UI bindings in
        # the packaged application, not just in the small rendering harnesses.
        with patch.object(app_module, 'SETTINGS_PATH', root / 'display.json'), \
                patch.object(app_module, 'DICTIONARY_PATH', root / 'dictionary.json'), \
                patch.object(app_module, 'DICTIONARY_NAME_PATH', root / 'dictionary-name.json'), \
                patch.object(app_module, 'MODEL_DIR', root / 'voice-model'), \
                patch.object(app_module, 'QSettings', side_effect=lambda *args: QSettings(str(root / 'settings.ini'), QSettings.Format.IniFormat)), \
                patch.object(app_module.MainWindow, 'prompt_model_if_missing', lambda self: None), \
                patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Discard):
            window = app_module.MainWindow()
            assert window.record_post.isChecked()
            assert window.updater is not None
            window.updater.automatic.setChecked(False)
            assert window.close(), 'The real control window could not close safely'
        background, foreground, clean = root / 'background.mp4', root / 'foreground.mkv', root / 'clean.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=10:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(background)], check=True)
        image = QImage(320, 180, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        # A foreground square covers the text center. The final export must
        # restore that layer above the aligned text, including alpha blending.
        from PySide6.QtGui import QPainter
        painter = QPainter(image)
        painter.fillRect(155, 85, 10, 10, QColor('red'))
        painter.end()
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pixel_format', 'rgba',
            '-video_size', '320x180', '-framerate', '10', '-i', 'pipe:0', '-c:v', 'ffv1',
            '-pix_fmt', 'bgra', str(foreground)], input=bytes(image.constBits()) * 10, check=True)
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(background), '-f', 'lavfi',
            '-i', 'sine=frequency=300:duration=1', '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(clean)], check=True)
        session = dict(groups=['Hello world'], slides=[settings], resolution=[320, 180], duration=1., fps=10,
            background=background.name, foreground=foreground.name, video=clean.name)
        words = [dict(text='Hello', slide=0, start=.1, end=.3, include=True, reviewed=True),
                 dict(text='world', slide=0, start=.5, end=.8, include=True, reviewed=True)]
        timeline = TextTimeline(CanvasScene, session, words)
        export = VideoExport(root / 'recording.session.json', session, root / 'final.mp4', ffmpeg)
        try:
            for frame in range(10):
                image = timeline.frame(frame / 10)
                deadline = time.monotonic() + 30
                while not export.push(image):
                    if export.finished or time.monotonic() > deadline:
                        raise RuntimeError('Export writer stopped: ' + export.error)
                    app.processEvents()
                    time.sleep(.01)
            while not export.finish():
                if export.finished:
                    raise RuntimeError(export.error)
                time.sleep(.01)
            deadline = time.monotonic() + 30
            while not export.finished:
                app.processEvents()
                if time.monotonic() > deadline:
                    export.cancel()
                    raise RuntimeError('Export did not finish')
                time.sleep(.01)
            assert not export.error, export.error
            decoded = subprocess.run([ffmpeg, '-v', 'error', '-ss', '0.7', '-i', str(root / 'final.mp4'),
                '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                capture_output=True)
            assert decoded.returncode == 0, decoded.stderr.decode('utf-8', 'replace')
            pixels = decoded.stdout
            assert len(pixels) == 320 * 180 * 3
            center = pixels[(90 * 320 + 160) * 3:(90 * 320 + 160) * 3 + 3]
            assert center[0] > 170 and center[2] < 100, 'Foreground must remain above text'
            assert (root / 'final.mp4').stat().st_size > 1000
        finally:
            timeline.close()
            if not export.finished:
                export.cancel()
                export.thread.join(5)
    result = {'success': True, 'version': VERSION, 'checks': [
        'real control window startup', 'Qt presentation text painter', 'bundled FFmpeg', 'alpha foreground capture',
        'deterministic aligned overlay frames', 'H.264/AAC final export',
        'foreground preserved above text', 'speech/camera/VST dependencies import']}
    if '--smoke-report' in sys.argv:
        Path(sys.argv[sys.argv.index('--smoke-report') + 1]).write_text(json.dumps(result, indent=2), encoding='utf-8')
    else:
        print(json.dumps(result))


if __name__ == '__main__':
    run()


def run_alignment(voice_path):
    import wave
    from postprocess import align_recording, validate_words
    from recorder import resolve_ffmpeg
    with wave.open(str(voice_path)) as voice:
        duration = voice.getnframes() / voice.getframerate()
    session_path = voice_path.parent / 'speech-test.session.json'
    session_path.write_text(json.dumps(dict(voice=voice_path.name, language='en', duration=duration,
        groups=['Hello world.', 'This is a timing test.'])), encoding='utf-8')
    result = align_recording(session_path, resolve_ffmpeg())
    assert len(result['words']) == 7, result
    assert sum(word['include'] for word in result['words']) >= 5, result
    validate_words(result['words'], duration, require_review=False)
    report = {'success': True, 'engine': result['engine'], 'words': len(result['words']),
              'included': sum(word['include'] for word in result['words']),
              'flagged_for_review': sum(not word['reviewed'] for word in result['words'])}
    Path(sys.argv[sys.argv.index('--smoke-report') + 1]).write_text(json.dumps(report, indent=2), encoding='utf-8')
