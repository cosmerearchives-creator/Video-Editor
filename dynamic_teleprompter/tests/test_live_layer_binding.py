"""Exercise actual settings -> presentation -> output-frame data flow.

Only unrelated window/device initialization and model rasterization are replaced.
The application methods are compiled unchanged from app.py into small harnesses.
"""
import ast
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication, QWidget
    HAS_QT = True
except ImportError:
    HAS_QT = False

if HAS_QT:
    from live2d_movement import step_motion


@unittest.skipUnless(HAS_QT, 'PySide6 is required for output integration tests')
class LiveLayerBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.source = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))

    def harness(self, original, name, base, methods, namespace):
        definition = next(n for n in self.source.body if isinstance(n, ast.ClassDef) and n.name == original)
        body = [n for n in definition.body if isinstance(n, ast.FunctionDef) and n.name in methods]
        self.assertEqual(len(body), len(methods))
        harness = ast.ClassDef(name=name, bases=[ast.Name(id=base, ctx=ast.Load())],
                               keywords=[], body=body, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[]))
        exec(compile(module, 'app.py live layer harness', 'exec'), namespace)
        return namespace[name]

    def setup_flow(self, foreground=False):
        namespace = dict(globals())
        self.drawn = []

        def draw_model(painter, layer, canvas_size):
            self.drawn.append((layer, layer.get('_movement_turn', 0)))
            w, h = canvas_size
            x, y = round(w * (.5 + layer.get('x', 0) / 100)), round(h * (.5 + layer.get('y', 0) / 100))
            painter.fillRect(x - 4, y - 4, 9, 9, QColor('red'))

        namespace['draw_live2d'] = draw_model
        draw_function = next(n for n in self.source.body if isinstance(n, ast.FunctionDef) and n.name == 'draw_media_layers')
        exec(compile(ast.Module(body=[draw_function], type_ignores=[]), 'app.py draw_media_layers', 'exec'), namespace)
        draw_layers = namespace['draw_media_layers']

        class Scene(QWidget):
            def __init__(self):
                super().__init__()
                self._media_layers = []
                self.presentation_updates = 0

            def set_output(self, *args):
                self.presentation_updates += 1

            def paintEvent(self, event):
                painter = QPainter(self)
                painter.fillRect(self.rect(), QColor('black'))
                draw_layers(painter, self._media_layers, (self.width(), self.height()), foreground=False)
                painter.end()

        output_type = self.harness('OutputWindow', 'OutputHarness', 'QWidget',
                                   {'set_output', 'render_frame'}, namespace)
        out = output_type()
        out.scene, out.canvas_size = Scene(), (320, 180)
        out.scene.setFixedSize(320, 180)
        out._preview_timer = QTimer(out)
        out._snapshot, out._rendering, out.render_count = QImage(), False, 0
        self.addCleanup(out.close)
        self.addCleanup(out.scene.close)
        host_type = self.harness('MainWindow', 'MainHarness', 'object',
                                {'settings', 'render', 'clear_output', '_tick_movement'}, namespace)
        host = host_type()
        host.out, host.layer_editor = out, None
        self.layer = {'type': 'live2d', 'path': 'fake.model3.json', 'x': 0., 'y': 0.,
                      'idle_enabled': True, 'movement': {'enabled': True},
                      '_movement_bounds': (.25, .2, .75, .8)}
        text = {'type': 'text', 'name': 'Text Output'}
        host.media_layers = [self.layer, text] if foreground else [text, self.layer]
        host._movement_layer = lambda: self.layer
        host._movement_keys = {'d'}
        host._movement_last_tick = time.monotonic() - .016
        namespace['QApplication'] = SimpleNamespace(activeWindow=lambda: out)
        controls = {
            'mode': 'Script', 'grouping': 'Sentence', 'group_limit': 1, 'chars': 80,
            'lines': 2, 'font_size': 32, 'bold': False, 'align': 'Center',
            'transparent': False, 'canvas_resolution': '320x180', 'canvas_fps': '30',
            'video_decode_quality': 'auto', 'audio_gain': 100, 'audio_gate_threshold': 0,
            'audio_clip_drive': 0, 'animation': 'None', 'duration': 100,
            'behavior': 'Manual', 'delay': 1}
        for name, value in controls.items():
            setattr(host, name, SimpleNamespace(currentText=lambda v=value: str(v),
                    value=lambda v=value: v, isChecked=lambda v=value: bool(v),
                    currentData=lambda v=value: v))
        host.font_picker = SimpleNamespace(currentFont=lambda: SimpleNamespace(family=lambda: 'Arial'))
        host.color_bg, host.color_fg, host.background_image = '#000000', '#ffffff', ''
        host._audio_chain_snapshot = lambda: {}
        host.timer = Mock()
        host.groups, host.index, host.history = ['Hello'], 0, []
        host.awaiting_first, host.voice_enabled = False, False
        host._manual_full_slide_index = 0
        host.progress, host.current = Mock(), Mock()
        host.motion_for_slide, host.visual_for_slide = lambda i: {}, lambda i: {}
        return host

    def test_wasd_changes_output_pixels_without_restarting_presentation(self):
        for foreground in (False, True):
            with self.subTest(foreground=foreground):
                host = self.setup_flow(foreground)
                host.render()
                initial = host.out.render_frame()
                self.assertEqual(initial.pixelColor(160, 90), QColor('red'))
                updates = host.out.scene.presentation_updates
                for _ in range(30):
                    host._movement_last_tick = time.monotonic() - .016
                    host._tick_movement()
                after = host.out.render_frame()
                new_x = round(320 * (.5 + self.layer['x'] / 100))
                self.assertGreater(new_x, 170)
                self.assertEqual(after.pixelColor(new_x, 90), QColor('red'))
                self.assertNotEqual(after.pixelColor(160, 90), QColor('red'))
                self.assertIs(self.drawn[-1][0], self.layer)
                self.assertEqual(self.drawn[-1][1], self.layer['_movement_turn'])
                self.assertEqual(updates, host.out.scene.presentation_updates)
                host._movement_keys.clear()
                position = self.layer['x'], self.layer['y']
                for _ in range(10):
                    host._movement_last_tick = time.monotonic() - .016
                    host._tick_movement()
                self.assertEqual(position, (self.layer['x'], self.layer['y']))

    def test_ready_screen_and_subsequent_slide_keep_live_layer_identity(self):
        host = self.setup_flow()
        host.awaiting_first = True
        host.render()
        self.assertIs(host.out.scene._media_layers, host.media_layers)
        host.awaiting_first = False
        host.render()
        self.assertIs(host.out.scene._media_layers, host.media_layers)
        self.assertIn('_movement_bounds', host.out.scene._media_layers[1])

    def test_saved_settings_keep_runtime_fields_out_and_do_not_drive_output(self):
        host = self.setup_flow()
        self.layer['_movement_turn'] = .7
        saved = host.settings()
        self.assertIsNot(saved['media_layers'][1], self.layer)
        self.assertNotIn('_movement_turn', saved['media_layers'][1])
        self.assertNotIn('_movement_bounds', saved['media_layers'][1])
        self.assertEqual(json.loads(json.dumps(saved))['media_layers'][1]['x'], 0)
        host.render()
        self.layer['x'] = 12
        self.assertEqual(saved['media_layers'][1]['x'], 0)
        self.assertEqual(host.out.scene._media_layers[1]['x'], 12)


if __name__ == '__main__':
    unittest.main()
