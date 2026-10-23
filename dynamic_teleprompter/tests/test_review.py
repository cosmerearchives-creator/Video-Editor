import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
try:
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QColor
    from post_ui import AlignmentDialog
    from review_preview import composite_frame
    from app import CanvasScene
    HAS_APP = True
except ImportError:
    HAS_APP = False


@unittest.skipUnless(HAS_APP,'Application dependencies required')
class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name)/'sample.session.json'
        session = dict(format='CosmereCamRecording',version=1,duration=3.,fps=10,
            resolution=[320,180],groups=['Hello world'],slides=[dict(font_size=20,
            foreground='#ffffff',motion=dict(intro='Fade',outro='None',unit='Word',intro_ms=100))],
            video='none.mp4',background='none.mp4',foreground='none.mkv')
        path.write_text(json.dumps(session),encoding='utf-8')
        words=[dict(text='Hello',slide=0,start=.2,end=.8,include=True,reviewed=False,flags=['Low confidence']),
               dict(text='world',slide=0,start=1.,end=1.8,include=True,reviewed=True,flags=[])]
        path.with_suffix('.alignment.json').write_text(json.dumps(dict(words=words)),encoding='utf-8')
        self.parent = QWidget()
        self.dialog = AlignmentDialog(self.parent,path,'ffmpeg',CanvasScene)
        self.dialog.sources.stop()
        self.dialog.sources.request = lambda *args: None
        self.dialog.preview_refresh.stop()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.parent.deleteLater()
        self.temporary.cleanup()

    def test_reviewed_valid_rows_hide_without_removing_words(self):
        d=self.dialog
        self.assertFalse(d.table.isRowHidden(0))
        self.assertTrue(d.table.isRowHidden(1))
        d.table.item(0,5).setCheckState(Qt.CheckState.Checked)
        self.assertTrue(d.table.isRowHidden(0))
        self.assertEqual(len(d.collect()),2)
        self.assertTrue(d.collect()[0]['include'])
        self.assertTrue(d.save_edits())
        self.assertEqual(len(json.loads(d.alignment_path.read_text())['words']),2)

    def test_invalid_reviewed_timing_remains_until_corrected(self):
        d=self.dialog
        d.table.item(1,2).setText('2.0')
        self.assertFalse(d.table.isRowHidden(1))
        self.assertIn('Invalid',d.table.item(1,6).text())
        d.table.item(1,2).setText('0.4')
        self.assertIn('Overlaps',d.table.item(1,6).text())
        d.table.item(1,2).setText('1.000')
        self.assertTrue(d.table.isRowHidden(1))

    def test_show_all_and_timestamp_precision(self):
        d=self.dialog
        d.show_all.setChecked(True)
        self.assertFalse(d.table.isRowHidden(1))
        d.update_timestamp(1234)
        self.assertEqual(d.timestamp.text(),'1.234 s / 3.000 s')
        positions=[]
        d.player.setPosition=positions.append
        d.table.setCurrentCell(1,1)
        self.assertEqual(positions[-1],1000)

    def test_preview_uses_edited_timings_without_confirming_review(self):
        d=self.dialog
        d.rebuild_preview()
        self.assertIsNotNone(d.preview_timeline)
        self.assertFalse(d.collect()[0]['reviewed'])
        self.assertEqual(d.preview_timeline.cues[0]['start'],.2)
        d.table.item(0,2).setText('0.300')
        d.rebuild_preview()
        self.assertEqual(d.preview_timeline.cues[0]['start'],.3)
        self.assertEqual(d.preview_timeline.cues[0]['settings']['motion']['intro'],'Fade')

    def test_foreground_alpha_is_composited_once_above_text(self):
        def solid(color):
            image=QImage(10,10,QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(color)
            return image
        result=composite_frame(solid(QColor('blue')),solid(QColor('white')),solid(QColor(255,0,0,128)))
        color=result.pixelColor(5,5)
        self.assertGreater(color.red(),250)
        self.assertAlmostEqual(color.blue(),127,delta=2)


if __name__=='__main__':
    unittest.main()
