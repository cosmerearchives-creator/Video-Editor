import json
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    import app
    from post_export import TextTimeline
    HAS_APP = True
except ImportError:
    HAS_APP = False


@unittest.skipUnless(HAS_APP, 'Full application dependencies required')
class PostRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def count_visible(self, image):
        return sum(image.pixelColor(x, y).alpha() > 0 for y in range(image.height()) for x in range(image.width()))

    def test_clean_recording_omits_text_without_changing_preview(self):
        out = app.OutputWindow()
        self.addCleanup(out.close)
        out._preview_timer.stop()
        out.set_output(['Hello', '', ''], dict(canvas_resolution='320x180', instant=True,
                       foreground='#ffffff', background='#000000', font_size=30, media_layers=[]))
        normal = out.render_frame()
        before = bytes(out._snapshot.constBits())
        clean = out.render_frame(include_text=False, include_foreground=False)
        self.assertNotEqual(bytes(normal.constBits()), bytes(clean.constBits()))
        self.assertEqual(bytes(out._snapshot.constBits()), before)
        self.assertTrue(out.scene.labels[0].isVisibleTo(out.scene))
        self.assertEqual(out.scene.labels[0].text(), 'Hello')

    def test_word_animation_uses_spoken_starts_and_original_style(self):
        for animation in app.ANIMATION_OPTIONS:
            with self.subTest(animation=animation):
                settings = dict(font_size=30, foreground='#ffffff', chars=30,
                    motion=dict(intro=animation, outro='Fade', unit='Word', intro_ms=200, outro_ms=200, stagger_ms=65))
                session = {'groups': ['Hello world'], 'slides': [settings], 'resolution': [320, 180], 'duration': 3.}
                words = [dict(text='Hello', slide=0, start=.5, end=1., include=True, reviewed=True),
                         dict(text='world', slide=0, start=1.5, end=2., include=True, reviewed=True)]
                timeline = TextTimeline(app.CanvasScene, session, words)
                try:
                    self.assertEqual(self.count_visible(timeline.frame(.1)), 0)
                    visible = timeline.frame(.9)
                    self.assertGreater(self.count_visible(visible), 0)
                    label = timeline.scenes[0].labels[0]
                    self.assertEqual(label.motion['intro'], animation)
                    self.assertEqual(label._progress(0, 2), 1.)
                    self.assertEqual(label._progress(1, 2), 0.)
                    self.assertGreater(self.count_visible(timeline.frame(1.9)), self.count_visible(visible))
                finally:
                    timeline.close()


if __name__ == '__main__':
    unittest.main()
