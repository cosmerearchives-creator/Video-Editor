import json
import math
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout)

from postprocess import align_recording, validate_words, write_json
from post_export import TextTimeline, VideoExport
from review_preview import ReviewVideo, SourceFrames, composite_frame


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
        self.video = ReviewVideo()
        self.preview_timeline = None
        self.preview_index, self.preview_generation = 0, 0
        self.sources = SourceFrames(self.path, self.session, self.ffmpeg, self)
        self.sources.ready.connect(self.source_ready)
        self.preview_refresh = QTimer(self)
        self.preview_refresh.setSingleShot(True)
        self.preview_refresh.setInterval(100)
        self.preview_refresh.timeout.connect(self.rebuild_preview)
        self.player, self.audio = QMediaPlayer(self), QAudioOutput(self)
        self.video_sink = QVideoSink(self)
        self.player.setVideoSink(self.video_sink)
        self.video_sink.videoFrameChanged.connect(self.video_frame)
        self.player.setAudioOutput(self.audio)
        self.player.setSource(QUrl.fromLocalFile(str(self.path.parent / self.session['video'])))
        layout.addWidget(self.video)
        row = QHBoxLayout()
        play = QPushButton('Play / Pause')
        play.clicked.connect(self.toggle_play)
        row.addWidget(play)
        self.timestamp = QLabel()
        self.timestamp.setMinimumWidth(180)
        self.update_timestamp(0)
        row.addWidget(self.timestamp)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, round(self.session['duration'] * 1000))
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self.position_changed)
        row.addWidget(self.seek, 1)
        layout.addLayout(row)
        filters = QHBoxLayout()
        self.show_all = QCheckBox('Show all words')
        self.show_all.toggled.connect(self.update_queue)
        filters.addWidget(self.show_all)
        self.show_overlay = QCheckBox('Show text overlay')
        self.show_overlay.setChecked(True)
        self.show_overlay.toggled.connect(self.request_preview)
        filters.addWidget(self.show_overlay)
        self.queue_count = QLabel()
        filters.addWidget(self.queue_count, 1)
        self.preview_status = QLabel('Text preview follows saved presentation styling and edited timings.')
        self.preview_status.setWordWrap(True)
        layout.addLayout(filters)
        layout.addWidget(self.preview_status)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Slide', 'Word', 'Start (s)', 'End (s)', 'Include', 'Reviewed', 'Needs attention'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.seek_selected)
        self.table.itemChanged.connect(self.edited)
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
        self.table.blockSignals(True)
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
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        self.update_queue()
        self.preview_refresh.start()
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(1, max(140, self.table.columnWidth(1)))
        for column in (2,3):
            self.table.setColumnWidth(column, max(80, self.table.columnWidth(column)))
        self.table.horizontalHeader().setStretchLastSection(True)

    def collect(self, tolerant=False):
        words = []
        for row, old in enumerate(self.data.get('words', [])):
            word = dict(old)
            word['text'] = self.table.item(row, 1).text().strip()
            for column, key in ((2, 'start'), (3, 'end')):
                text = self.table.item(row, column).text().strip()
                try:
                    word[key] = float(text) if text else None
                except ValueError:
                    if not tolerant:
                        raise ValueError(f'Word {row+1}: enter a numeric {key} time in seconds')
                    word[key] = None
            word['include'] = self.table.item(row, 4).checkState() == Qt.CheckState.Checked
            word['reviewed'] = self.table.item(row, 5).checkState() == Qt.CheckState.Checked
            words.append(word)
        return words

    def row_errors(self, words):
        errors, previous = [], 0.
        for word in words:
            error = ''
            if word.get('include'):
                start, end = word.get('start'), word.get('end')
                if not word.get('text'):
                    error = 'Empty word: enter text or uncheck Include'
                elif not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                    error = 'Invalid or missing audio timing'
                elif not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= self.session['duration'] + .05):
                    error = 'Invalid start/end times'
                elif start < previous - .01:
                    error = 'Overlaps or precedes the previous word'
                else:
                    previous = end
            errors.append(error)
        return errors

    def update_queue(self, *_):
        words = self.collect(tolerant=True)
        errors = self.row_errors(words)
        pending = 0
        self.table.blockSignals(True)
        try:
            for row, (word, error) in enumerate(zip(words, errors)):
                unresolved = bool(error) or not word.get('reviewed')
                pending += unresolved
                self.table.setRowHidden(row, not self.show_all.isChecked() and not unresolved)
                notes = ([error] if error else [])
                if not word.get('include') and not word.get('reviewed'):
                    notes.append('Confirm skipped word')
                if not word.get('reviewed'):
                    notes += [f for f in word.get('flags', []) if 'Invalid' not in f and f not in notes]
                self.table.item(row, 6).setText('; '.join(notes))
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    item.setBackground(QColor('#fff0c2') if unresolved else QColor('white'))
                    item.setForeground(QColor('#202020'))
        finally:
            self.table.blockSignals(False)
        self.queue_count.setText(f'{pending} words need attention / {len(words)} total')

    def edited(self, item):
        self.update_queue()
        self.preview_refresh.start()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def update_timestamp(self, milliseconds):
        self.timestamp.setText(f'{milliseconds/1000:.3f} s / {self.session["duration"]:.3f} s')

    def position_changed(self, milliseconds):
        if not self.seek.isSliderDown():
            self.seek.setValue(milliseconds)
        self.update_timestamp(milliseconds)
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.request_preview()

    def video_frame(self, frame):
        if getattr(self, '_preview_closed', False) or not frame.isValid():
            return
        # The clean recording remains a fallback while the source worker seeks.
        if not self.show_overlay.isChecked() or self.preview_timeline is None:
            self.video.display(frame.toImage())
        milliseconds = frame.startTime()/1000 if frame.startTime() >= 0 else self.player.position()
        self.request_preview(milliseconds=milliseconds)

    def request_preview(self, *_args, milliseconds=None):
        if getattr(self, '_preview_closed', False):
            return
        milliseconds = self.player.position() if milliseconds is None else milliseconds
        index = min(max(0, math.floor(milliseconds*self.session['fps']/1000)),
                    max(0, math.ceil(self.session['duration']*self.session['fps'])-1))
        if index < self.preview_index or abs(index-self.preview_index) > self.session['fps']//2:
            self.preview_generation += 1
        self.preview_index = index
        self.sources.request(index, self.preview_generation)

    def rebuild_preview(self):
        if self.preview_timeline:
            self.preview_timeline.close()
            self.preview_timeline = None
        words = self.collect(tolerant=True)
        errors = self.row_errors(words)
        preview = [dict(word, reviewed=True) for word, error in zip(words, errors)
                   if word.get('include') and not error and word.get('text')]
        if preview:
            try:
                self.preview_timeline = TextTimeline(self.scene_class, self.session, preview)
                self.preview_status.setText('Preview uses edited timings and saved presentation animations, styles and layer order.'
                    + (' Words with invalid timing are omitted until corrected.' if any(errors) else ''))
            except Exception as exc:
                self.preview_status.setText('Text preview unavailable: ' + str(exc))
        else:
            self.preview_status.setText('Align the recording or correct word timings to preview presentation text.')
        self.preview_generation += 1
        self.request_preview()

    def source_ready(self, index, generation, image, error):
        if getattr(self, '_preview_closed', False) or generation != self.preview_generation or index < self.preview_index-3:
            return
        if error:
            self.preview_status.setText('Preview source error: ' + error)
            return
        width = image.width()//2
        background = image.copy(0, 0, width, image.height())
        foreground = image.copy(width, 0, width, image.height())
        if self.show_overlay.isChecked() and self.preview_timeline:
            overlay = self.preview_timeline.frame(index/self.session['fps'])
            self.video.display(composite_frame(background, overlay, foreground))
        else:
            self.video.display(composite_frame(background, QImage(), foreground))

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
        for index in list(self.table.selectionModel().selectedRows()):
            self.table.item(index.row(), 5).setCheckState(Qt.CheckState.Checked)

    def seek_selected(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 2) is None:
            return
        try:
            self.player.setPosition(round(float(self.table.item(row, 2).text()) * 1000))
        except (ValueError, OverflowError):
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
            self.table.setEnabled(False)
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
            self.table.setEnabled(True)
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

    def done(self, result):
        if self.worker is not None or self.export is not None:
            return
        self.stop_preview()
        super().done(result)

    def stop_preview(self):
        if getattr(self, '_preview_closed', False):
            return
        self._preview_closed = True
        self.player.stop()
        self.player.setVideoSink(None)
        self.player.setSource(QUrl())
        self.preview_refresh.stop()
        self.sources.stop()
        if self.preview_timeline:
            self.preview_timeline.close()
            self.preview_timeline = None

    def closeEvent(self, event):
        if self.worker is not None or self.export is not None:
            QMessageBox.information(self, 'Work in progress', 'Wait for alignment or export to finish before closing.')
            event.ignore()
            return
        self.stop_preview()
        super().closeEvent(event)
