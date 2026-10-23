"""Release check of the actual review decoder and Qt overlay preview."""
import json
import time
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget
from post_ui import AlignmentDialog


def run_review(root, session, words, ffmpeg, app, scene_class):
    session = dict(session, format='CosmereCamRecording', version=1)
    path = root / 'review.session.json'
    path.write_text(json.dumps(session), encoding='utf-8')
    words = [dict(w) for w in words]
    words[0].update(reviewed=False, flags=['Low confidence'])
    path.with_suffix('.alignment.json').write_text(json.dumps(dict(words=words)),encoding='utf-8')
    owner = QWidget()
    dialog = AlignmentDialog(owner, path, ffmpeg, scene_class)
    try:
        dialog.player.stop()
        assert not dialog.table.isRowHidden(0) and dialog.table.isRowHidden(1)
        dialog.table.item(0,5).setCheckState(Qt.CheckState.Checked)
        assert dialog.table.isRowHidden(0) and len(dialog.collect()) == 2
        dialog.table.item(1,2).setText('0.900')
        assert not dialog.table.isRowHidden(1)
        dialog.table.item(1,2).setText('0.500')
        assert dialog.table.isRowHidden(1)
        dialog.show_all.setChecked(True)
        assert not dialog.table.isRowHidden(1)
        dialog.update_timestamp(650)
        assert dialog.timestamp.text() == '0.650 s / 1.000 s'
        dialog.rebuild_preview()
        dialog.preview_refresh.stop()
        received = []
        def capture(index, generation, image, error):
            received.append((index, error))
        dialog.sources.ready.connect(capture)
        def wait_for(index):
            received.clear()
            dialog.request_preview(milliseconds=index*100)
            deadline = time.monotonic()+15
            while not any(i==index for i,e in received) and time.monotonic()<deadline:
                app.processEvents()
                time.sleep(.01)
            assert any(i==index and not error for i,error in received), received
            assert not dialog.video.frame.isNull()
            center = dialog.video.frame.pixelColor(160,90)
            assert center.red()>170 and center.blue()<100, 'Foreground must cover text in preview'
            return bytes(dialog.video.frame.constBits())
        with_text = wait_for(7)
        dialog.show_overlay.setChecked(False)
        clean = wait_for(7)
        edge = dialog.video.frame.pixelColor(20,20)
        assert edge.blue()>170 and edge.red()<60, 'Clean preview background must be preserved'
        assert with_text != clean, 'Text overlay toggle did not change the preview'
        dialog.show_overlay.setChecked(True)
        wait_for(2)  # seek backward through the same decoder
        dialog.player.setPosition(700)
        dialog.player.play()
        deadline = time.monotonic()+5
        while dialog.player.position()<800 and time.monotonic()<deadline:
            app.processEvents()
            time.sleep(.01)
        dialog.player.pause()
        assert dialog.player.position()>=800, 'Actual QMediaPlayer playback did not advance'
        assert dialog.save_edits()
    finally:
        dialog.close()
        assert not dialog.sources.thread.is_alive()
        dialog.deleteLater()
        owner.deleteLater()
        from PySide6.QtCore import QEvent
        app.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        app.processEvents()
