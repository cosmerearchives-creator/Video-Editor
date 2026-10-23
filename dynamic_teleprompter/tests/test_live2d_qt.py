"""Qt integration with a deliberately blocked model; no licensed SDK required.

The input harness compiles the actual input/tick methods from app.py, avoiding
initialization of microphones, cameras, dictionaries and unrelated app windows.
"""
import ast
import os
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QEvent, QEventLoop, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                                  QLineEdit, QMainWindow, QMenu, QSpinBox, QTextEdit, QWidget)
    HAS_QT = True
except ImportError:
    HAS_QT = False

if HAS_QT:
    import live2d_runtime as runtime
    from live2d_movement import step_motion


@unittest.skipUnless(HAS_QT, 'PySide6 is required for Qt integration tests')
class QtRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        if runtime._worker:
            runtime._worker.close(2)
        runtime.shutdown_live2d()
        runtime._last_error.clear()
        runtime._errors.clear()

    def frame(self, layer):
        image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            runtime.draw_live2d(painter, layer, (320, 180))
        finally:
            painter.end()
        return image

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Qt renderer did not complete')
            QTest.qWait(5)

    def test_ui_keys_and_motion_continue_during_slow_pose_render(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        class Model:
            def set_tracking(self, pose):
                calls.append(threading.get_ident())
                return True

            def draw(self, painter, w, h):
                entered.set()
                release.wait(3)
                painter.fillRect(QRectF(0, 0, w, h), QColor('red'))

            def movement_bounds(self, w, h):
                return .25, .2, .75, .8

        class Core:
            def load(self, path):
                calls.append(threading.get_ident())
                return Model()

        source = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        main = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'MainWindow')
        methods = [n for n in main.body if isinstance(n, ast.FunctionDef)
                   and n.name in ('eventFilter', '_tick_movement')]
        harness = ast.ClassDef(name='InputHarness', bases=[ast.Name(id='QMainWindow', ctx=ast.Load())],
                               keywords=[], body=methods, decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[]))
        namespace = dict(globals())
        exec(compile(module, 'app.py input harness', 'exec'), namespace)
        host = namespace['InputHarness']()
        host.out = QWidget()
        host.layer_editor = None
        host._movement_keys = set()
        host._movement_last_tick = time.monotonic()
        layer = {'path': 'fake.model3.json', 'movement': {'enabled': True},
                 '_movement_bounds': (.25, .2, .75, .8)}
        host._movement_layer = lambda: layer
        host.out.installEventFilter(host)
        host.out.show()
        host.out.activateWindow()
        QTest.qWait(20)
        self.assertIs(self.app.activeWindow(), host.out)
        try:
            with patch.object(runtime, 'CubismCore', Core):
                self.frame(layer)
                self.assertTrue(entered.wait(1))
                ticks, after_release, held_positions = [], [], []
                timer = QTimer()
                timer.setInterval(5)

                def tick():
                    ticks.append(time.monotonic())
                    host._tick_movement()
                    self.frame(layer)

                def key_up():
                    held_positions.append(layer.get('x', 0))
                    QTest.keyRelease(host.out, Qt.Key.Key_D)
                    after_release.append(layer['x'])

                timer.timeout.connect(tick)
                timer.start()
                loop = QEventLoop()
                QTimer.singleShot(10, lambda: QTest.keyPress(host.out, Qt.Key.Key_D))
                QTimer.singleShot(150, key_up)
                QTimer.singleShot(250, loop.quit)
                loop.exec()
                timer.stop()
                self.assertGreater(len(ticks), 10)
                self.assertGreater(held_positions[0], 0)
                self.assertEqual(host._movement_keys, set())
                self.assertEqual(after_release[0], layer['x'])
                self.assertEqual(runtime.live2d_render_stats(layer)['completed'], 0)
                print(f'Qt blocked-render probe: {len(ticks)} timer ticks in 250 ms; '
                      f'held-D x={held_positions[0]:.3f}; release drift=0')
                release.set()
                self.wait_until(lambda: runtime.live2d_render_stats(layer).get('completed', 0) > 0)
                image = self.frame(layer)
                self.assertGreater(image.pixelColor(160, 90).red(), 200)
                self.assertEqual(layer['_movement_bounds'], (.25, .2, .75, .8))
                self.assertTrue(all(t != threading.get_ident() for t in calls))
                self.assertEqual(len(set(calls)), 1)
        finally:
            release.set()
            host.out.removeEventFilter(host)
            host.out.close()
            host.close()

    def test_model_failure_is_reported_without_blocking_paint(self):
        class Core:
            def load(self, path):
                raise ValueError('test model failure')
        layer = {'path': 'bad.model3.json'}
        with patch.object(runtime, 'CubismCore', Core):
            self.frame(layer)
            self.wait_until(lambda: runtime.live2d_render_stats(layer).get('completed') == 1)
            self.frame(layer)
            self.assertIn('test model failure', runtime.live2d_error(layer['path']))

    def test_geometry_bounds_account_for_canvas_padding(self):
        model = runtime.CubismMeshModel.__new__(runtime.CubismMeshModel)
        points = [SimpleNamespace(X=-.25, Y=-.4), SimpleNamespace(X=.25, Y=.4)]
        model.dll = SimpleNamespace(
            csmGetDrawableVertexCounts=lambda m: [2],
            csmGetDrawableVertexPositions=lambda m: [points],
            csmGetDrawableOpacities=lambda m: [1.])
        model._model, model.count = None, 1
        model.canvas = SimpleNamespace(X=100, Y=100)
        model.origin = SimpleNamespace(X=50, Y=50)
        model.ppu = SimpleNamespace(value=100)
        expected = (.359375, .1, .640625, .9)
        for actual, target in zip(model.movement_bounds(320, 180), expected):
            self.assertAlmostEqual(actual, target)

    def test_shared_model_instances_and_resolution_have_separate_images(self):
        loaded = []

        class Model:
            def __init__(self):
                self.pose = None

            def set_tracking(self, pose):
                changed = pose != self.pose
                self.pose = pose
                return changed

            def draw(self, painter, w, h):
                color = 'red' if self.pose.get('ParamAngleY', 0) > 0 else 'blue'
                painter.fillRect(QRectF(0, 0, w, h), QColor(color))

            def movement_bounds(self, w, h):
                return 0., 0., 1., 1.

        class Core:
            def load(self, path):
                loaded.append(path)
                return Model()

        renderer = runtime._PoseRenderer()
        with patch.object(runtime, 'CubismCore', Core):
            red = renderer(('same', 1), (320, 180, (('ParamAngleY', 10),)))
            blue = renderer(('same', 2), (320, 180, (('ParamAngleY', -10),)))
            self.assertEqual(red[0].pixelColor(160, 90), QColor('red'))
            self.assertEqual(blue[0].pixelColor(160, 90), QColor('blue'))
            self.assertEqual(len(loaded), 2)
            self.assertIs(red, renderer(('same', 1), (320, 180, (('ParamAngleY', 10),))))
            resized = renderer(('same', 1), (640, 360, (('ParamAngleY', 10),)))
            self.assertEqual((resized[0].width(), resized[0].height()), (640, 360))


if __name__ == '__main__':
    unittest.main()

