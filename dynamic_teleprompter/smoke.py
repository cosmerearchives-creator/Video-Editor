"""Short real Qt/FFmpeg/export check, also executed inside the frozen release."""
import json
import os
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
    from mic_dsp import NoiseSuppressor, make_eq, DEFAULT_BANDS
    import numpy as np
    samples = np.random.default_rng(14).normal(0, .008, 512).astype(np.float32)
    suppression = NoiseSuppressor()
    for _ in range(120):
        cleaned = suppression.process(samples, 1.)
    assert len(cleaned) == len(samples) and np.isfinite(cleaned).all()
    assert np.mean(cleaned ** 2) < np.mean(samples ** 2), 'RNNoise did not reduce steady noise'
    assert make_eq(DEFAULT_BANDS)(samples, 48000).shape == samples.shape
    ffmpeg = resolve_ffmpeg()
    assert ffmpeg and Path(ffmpeg).is_file(), 'Bundled FFmpeg not found'
    settings = dict(font_size=24, foreground='#ffffff', chars=30,
        motion=dict(intro='Fade', outro='Fade', unit='Word', intro_ms=100, outro_ms=100, stagger_ms=0))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        window_report = root / 'window-smoke.json'
        command = ([sys.executable] if getattr(sys, 'frozen', False) else
                   [sys.executable, str(Path(__file__).with_name('desktop_entry.py'))])
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        child = subprocess.run([*command, '--smoke-window', '--smoke-report', str(window_report)],
                               timeout=90, env=env)
        if child.returncode:
            details = window_report.read_text(encoding='utf-8') if window_report.exists() else 'No window report was written'
            raise RuntimeError('Packaged control window failed: ' + details)
        assert json.loads(window_report.read_text(encoding='utf-8'))['success']
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
            from review_smoke import run_review
            run_review(root, session, words, ffmpeg, app, CanvasScene)
        finally:
            timeline.close()
            if not export.finished:
                export.cancel()
                export.thread.join(5)
    result = {'success': True, 'version': VERSION, 'checks': [
        'real control window startup', 'bundled RNNoise processes audio', 'parametric EQ processes audio', 'Qt presentation text painter', 'bundled FFmpeg', 'alpha foreground capture',
        'deterministic aligned overlay frames', 'H.264/AAC final export',
        'foreground preserved above text', 'review queue and millisecond timestamp',
        'live aligned text preview with source-layer order and seeking',
        'reader/script recording controls', 'add/remove EQ bands beyond twelve',
        'speech/camera/VST dependencies import']}
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


def run_window():
    """Use the normal Qt event-loop lifetime in a fresh process for startup."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QSettings, QTimer
    from unittest.mock import patch
    import app as application
    def stage(message):
        print('Window smoke: ' + message, flush=True)
    app = QApplication.instance() or QApplication([])
    stage('application created')
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with patch.object(application, 'SETTINGS_PATH', root / 'display.json'), \
                patch.object(application, 'DICTIONARY_PATH', root / 'dictionary.json'), \
                patch.object(application, 'DICTIONARY_NAME_PATH', root / 'dictionary-name.json'), \
                patch.object(application, 'MODEL_DIR', root / 'voice-model'), \
                patch.object(application, 'QSettings', side_effect=lambda *args: QSettings(str(root / 'settings.ini'), QSettings.Format.IniFormat)), \
                patch.object(application.MainWindow, 'prompt_model_if_missing', lambda self: None), \
                patch.object(QMessageBox, 'question', lambda *args, **kwargs: QMessageBox.StandardButton.Discard):
            window = application.MainWindow()
            stage('control window constructed')
            from mic_ui import MicrophoneEffectsDialog
            from live2d_idle import DEFAULT_ANIMATION
            from PySide6.QtWidgets import QDoubleSpinBox, QDialog
            editor = application.VisualLayerEditor(window)
            window.layer_editor = editor
            window.script.setPlainText('Restored script with canvas text.')
            window.prepare_groups()
            window.next()
            editor.items.setCurrentRow(0)
            assert editor.x.isEnabled() and editor.y.isEnabled()
            editor.drag_layer(12, -8)
            assert window.media_layers[0]['x'] == 12
            assert window.out.scene.text_container.x() == round(window.out.canvas_size[0] * .12)
            # Probe real spinbox validation with a typed value above the old cap.
            window.media_layers.insert(0, dict(type='live2d', name='test', path='', idle_enabled=False))
            editor.refresh_items(0)
            def inspect_idle(dialog):
                box = dialog.findChild(QDoubleSpinBox, 'lip_sensitivity')
                assert box.value() == DEFAULT_ANIMATION['lip_sensitivity'] == 15
                assert box.minimum() == 0 and box.singleStep() == 1
                box.lineEdit().setText('25.5')
                box.interpretText()
                assert box.value() == 25.5
                dialog.reject()
                return QDialog.DialogCode.Rejected
            with patch.object(QDialog, 'exec', inspect_idle):
                editor.edit_idle_animation()
            window.media_layers.pop(0)
            editor.refresh_items(0)
            effects = MicrophoneEffectsDialog(window)
            effects.eq_enabled.setChecked(True)
            effects.suppress_enabled.setChecked(True)
            effects.rows[1][3].setValue(3)
            for _ in range(9):
                effects.add_band()
            effects.rows[-1][2].setValue(2700)
            effects.apply()
            eq = next(e for e in window._audio_chain_snapshot() if e['type']=='eq')
            assert len(eq['bands']) == 15 and eq['bands'][-1]['frequency'] == 2700
            effects.remove_buttons[6].click()
            effects.apply()
            assert len(next(e for e in window._audio_chain_snapshot() if e['type']=='eq')['bands']) == 14
            assert next(e for e in window._audio_chain_snapshot() if e['type']=='eq')['bands'][1]['gain'] == 3
            effects.reject()
            effects.deleteLater()
            # Restore through the same startup method, using isolated disk state.
            with patch.dict(os.environ, {'COSMERE_CAM_NO_SESSION': ''}), \
                    patch.object(application.MainWindow, 'session_path', lambda self: root / 'session.json'):
                window.autosave_session()
                assert (root / 'session.json').exists()
                window.script.setPlainText('Changed')
                window.media_layers[0]['x'] = 0
                window.restore_session()
                assert window.script.toPlainText() == 'Restored script with canvas text.'
                assert window.media_layers[0]['x'] == 12
                assert not window.awaiting_first
            editor.close()
            editor.deleteLater()
            window.layer_editor = None
            # Exercise reader Start -> script Stop through the actual shared handlers.
            class ProbeRecorder:
                def __init__(self, path, *args, **kwargs):
                    self.path = path
                    self.stopping = self.finished = False
                    self.session = None
                    self.error = None
                    self.frame_count, self.late_frames = 1, 0
                    self.report_path = root / 'recording.diagnostics.json'
                def start(self):
                    self.started = True
                def frames_due(self):
                    return 0
                def elapsed(self):
                    return 1.234
                def finish_async(self):
                    self.stopping = True
                def abort(self):
                    pass
            window.record_dir = str(root / 'recordings')
            with patch.object(application, 'CanvasRecorder', ProbeRecorder), \
                    patch.object(QMessageBox, 'information', lambda *args, **kwargs: None):
                window.reader.record_controls.toggle.click()
                assert window.recorder is not None and window.recorder.started
                window._sync_record_controls()
                assert window.reader.record_controls.indicator.text() == '● REC 1.234 s'
                assert 'Stop' in window.script_record_controls.toggle.text()
                window.script_record_controls.toggle.click()
                window._sync_record_controls()
                assert window.recorder.stopping
                assert not window.reader.record_controls.toggle.isEnabled()
                window.recorder.finished = True
                window._record_finished()
                window._sync_record_controls()
                assert window.recorder is None
                assert 'Start' in window.reader.record_controls.toggle.text()
            stage('text editing, expandable EQ, session restore and synchronized recording controls verified')
            assert window.record_post.isChecked() and window.updater is not None
            window.updater.automatic.setChecked(False)
            window.show()
            stage('shown')
            QTimer.singleShot(150, window.close)
            QTimer.singleShot(3000, app.quit)
            assert app.exec() == 0
            stage('event loop exited')
            assert not window.isVisible()
            from PySide6.QtCore import QEvent
            window.out.deleteLater()
            window.reader.deleteLater()
            window.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            stage('widgets destroyed')
    Path(sys.argv[sys.argv.index('--smoke-report') + 1]).write_text(json.dumps({'success': True}), encoding='utf-8')
    stage('completed')
