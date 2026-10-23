import json
import math
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout)

from postprocess import align_recording, validate_words, write_json
from post_export import TextTimeline, VideoExport


class AlignmentTask(QThread):
    progress = Signal(str)
    result = Signal(object, str)

    def __init__(self, path, ffmpeg, parent):
        super().__init__(parent)
        self.path, self.ffmpeg = path, ffmpeg

    def run(self):
        try:
            result = align_recording(self.path, self.ffmpeg, self.progress.emit)
            self.result.emit(result, '')
        except Exception as exc:
            self.result.emit(None, str(exc))


class AlignmentDialog(QDialog):
    def __init__(self, parent, session_path, ffmpeg, scene_class):
        super().__init__(parent)
        self.setWindowTitle('Review Speech Alignment and Export')
        self.resize(1200, 850)
        self.path, self.ffmpeg, self.scene_class = Path(session_path), ffmpeg, scene_class
        self.session = json.loads(self.path.read_text(encoding='utf-8'))
        if self.session.get('format') != 'CosmereCamRecording' or self.session.get('version') != 1:
            raise ValueError('Choose a Cosmere Cam recording session JSON file')
        self.alignment_path = self.path.with_suffix('.alignment.json')
        self.data = json.loads(self.alignment_path.read_text(encoding='utf-8')) if self.alignment_path.exists() else {'words': []}
        self.worker, self.export, self.timeline = None, None, None
        self.frame_index, self.ending, self.pending_image = 0, False, None
        layout = QVBoxLayout(self)
        hint = QLabel('Automatic alignment uses the original recorded voice, not live timing. '
            'Review flagged words; adjust start/end seconds or uncheck Include for skipped words. '
            'The final export uses the saved presentation animations, styles and layer order.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.align = QPushButton('Automatically Align Script to Voice')
        self.align.clicked.connect(self.start_alignment)
        layout.addWidget(self.align)
        video = QVideoWidget()
        video.setMinimumHeight(180)
        self.player, self.audio = QMediaPlayer(self), QAudioOutput(self)
        self.player.setVideoOutput(video)
        self.player.setAudioOutput(self.audio)
        self.player.setSource(QUrl.fromLocalFile(str(self.path.parent / self.session['video'])))
        layout.addWidget(video)
        row = QHBoxLayout()
        play = QPushButton('Play / Pause')
        play.clicked.connect(lambda: self.player.pause() if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState else self.player.play())
        row.addWidget(play)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, round(self.session['duration'] * 1000))
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self.seek.setValue)
        row.addWidget(self.seek, 1)
        layout.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Slide', 'Word', 'Start (s)', 'End (s)', 'Include', 'Reviewed', 'Needs attention'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.seek_selected)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        save = QPushButton('Save Timing Edits')
        save.clicked.connect(self.save_edits)
        buttons.addWidget(save)
        reviewed = QPushButton('Confirm Selected Rows Reviewed')
        reviewed.clicked.connect(self.confirm_selected)
        buttons.addWidget(reviewed)
        self.export_button = QPushButton('Export Video with Presentation Text…')
        self.export_button.clicked.connect(self.start_export)
        buttons.addWidget(self.export_button)
        layout.addLayout(buttons)
        self.status = QLabel('Ready. First alignment downloads local Whisper model weights (~145 MB). Audio stays on this computer.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.export_timer = QTimer(self)
        self.export_timer.setInterval(10)
        self.export_timer.timeout.connect(self.export_step)
        self.populate()

    def populate(self):
        words = self.data.get('words', [])
        self.table.setRowCount(len(words))
        for row, word in enumerate(words):
            values = [str(word['slide'] + 1), word['text'],
                      '' if word['start'] is None else f"{word['start']:.3f}",
                      '' if word['end'] is None else f"{word['end']:.3f}", '', '', '; '.join(word.get('flags', []))]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (4, 5):
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if word['include' if column == 4 else 'reviewed'] else Qt.CheckState.Unchecked)
                elif column in (0, 6):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if word.get('flags') and not word.get('reviewed'):
                    item.setBackground(QColor('#5c4718'))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    def collect(self):
        words = []
        for row, old in enumerate(self.data.get('words', [])):
            word = dict(old)
            word['text'] = self.table.item(row, 1).text().strip()
            for column, key in ((2, 'start'), (3, 'end')):
                text = self.table.item(row, column).text().strip()
                word[key] = float(text) if text else None
            word['include'] = self.table.item(row, 4).checkState() == Qt.CheckState.Checked
            word['reviewed'] = self.table.item(row, 5).checkState() == Qt.CheckState.Checked
            words.append(word)
        return words

    def save_edits(self):
        try:
            self.data['words'] = self.collect()
            write_json(self.alignment_path, self.data)
            self.status.setText('Timing edits saved.')
            return True
        except Exception as exc:
            QMessageBox.warning(self, 'Could not save edits', str(exc))
            return False

    def confirm_selected(self):
        for index in self.table.selectionModel().selectedRows():
            self.table.item(index.row(), 5).setCheckState(Qt.CheckState.Checked)

    def seek_selected(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 2) is None:
            return
        try:
            self.player.setPosition(round(float(self.table.item(row, 2).text()) * 1000))
        except ValueError:
            pass

    def start_alignment(self):
        if self.worker is not None or self.export is not None:
            return
        if self.data.get('words') and QMessageBox.question(self, 'Replace Timing Edits?',
                'Run automatic alignment again and replace the current timing edits?') != QMessageBox.StandardButton.Yes:
            return
        self.align.setEnabled(False)
        self.export_button.setEnabled(False)
        self.parent()._post_busy = True
        self.worker = AlignmentTask(self.path, self.ffmpeg, self)
        self.worker.progress.connect(self.status.setText)
        self.worker.result.connect(self.aligned)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def aligned(self, result, error):
        self.worker = None
        self.parent()._post_busy = False
        self.align.setEnabled(True)
        self.export_button.setEnabled(True)
        if error:
            self.status.setText('Alignment failed: ' + error)
            QMessageBox.warning(self, 'Alignment failed', error)
            return
        self.data = result
        self.populate()
        flagged = sum(not w['reviewed'] for w in result['words'])
        self.status.setText(f'Automatically aligned {len(result["words"])} words. {flagged} words need review.')

    def start_export(self):
        if self.worker is not None or self.export is not None or not self.save_edits():
            return
        try:
            words = self.data.get('words', [])
            if not words:
                raise ValueError('Align the recording first')
            validate_words(words, self.session['duration'])
            destination, _ = QFileDialog.getSaveFileName(self, 'Export Aligned Video',
                str((self.path.parent / self.session['video']).resolve().with_stem(Path(self.session['video']).stem + '_aligned')), 'MP4 video (*.mp4)')
            if not destination:
                return
            if Path(destination).resolve() in {(self.path.parent / self.session[key]).resolve()
                                               for key in ('video', 'background', 'foreground', 'voice')}:
                raise ValueError('Choose a new destination; recording source files must be preserved')
            self.player.pause()
            self.timeline = TextTimeline(self.scene_class, self.session, words)
            self.export = VideoExport(self.path, self.session, destination, self.ffmpeg)
            self.parent()._post_busy = True
            self.align.setEnabled(False)
            self.export_button.setEnabled(False)
            self.frame_index, self.ending, self.pending_image = 0, False, None
            self.export_timer.start()
        except Exception as exc:
            if self.timeline:
                self.timeline.close()
                self.timeline = None
            QMessageBox.warning(self, 'Could not export', str(exc))

    def export_step(self):
        if self.export.finished:
            self.export_timer.stop()
            error, output = self.export.error, self.export.path
            self.export = None
            self.timeline.close()
            self.timeline = None
            self.parent()._post_busy = False
            self.align.setEnabled(True)
            self.export_button.setEnabled(True)
            self.status.setText('Export failed: ' + error if error else 'Saved aligned video: ' + str(output))
            return
        if self.ending:
            return
        total = math.ceil(self.session['duration'] * self.session['fps'])
        if self.frame_index >= total:
            self.ending = self.export.finish()
            return
        try:
            if self.pending_image is None:
                self.pending_image = self.timeline.frame(self.frame_index / self.session['fps'])
            if self.export.push(self.pending_image):
                self.pending_image = None
                self.frame_index += 1
                self.status.setText(f'Exporting frame {self.frame_index} / {total}')
        except Exception as exc:
            self.export.cancel()
            self.export.error = str(exc)

    def closeEvent(self, event):
        if self.worker is not None or self.export is not None:
            QMessageBox.information(self, 'Work in progress', 'Wait for alignment or export to finish before closing.')
            event.ignore()
            return
        self.player.stop()
        super().closeEvent(event)
