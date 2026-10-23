"""Built-in microphone EQ and cleanup editor."""
import copy
import weakref
import math
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath
from PySide6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox, QLabel,
    QPushButton, QDialogButtonBox, QGridLayout)
from mic_dsp import DEFAULT_BANDS, EQ_TYPES, bands_value, eq_response


class EQGraph(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(460, 190)
        self.curve = None

    def set_bands(self, bands):
        self.curve = eq_response(bands)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#18212b'))
        left, top, width, height = 42, 15, self.width()-55, self.height()-40
        def point(f, db):
            return QPointF(left + math.log10(max(20, f)/20)/3*width,
                           top + (24-max(-24, min(24, db)))/48*height)
        p.setPen(QColor('#465362'))
        for db in (-24, -12, 0, 12, 24):
            p.drawLine(point(20, db), point(20000, db))
            p.drawText(2, int(point(20, db).y())+4, str(db))
        for f in (20, 100, 1000, 10000, 20000):
            p.drawLine(point(f, -24), point(f, 24))
            p.drawText(int(point(f, 0).x())-12, self.height()-5, str(f//1000)+'k' if f>=1000 else str(f))
        if self.curve is not None:
            path = QPainterPath()
            first = True
            for f, db in zip(*self.curve):
                if 20 <= f <= 20000:
                    if first:
                        path.moveTo(point(f, db)); first = False
                    else:
                        path.lineTo(point(f, db))
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor('#70d6ec'), 2))
            p.drawPath(path)
        p.end()


class MicrophoneEffectsDialog(QDialog):
    """Edits a copy; Apply previews through the existing monitor/record chain."""
    def __init__(self, main):
        super().__init__(main)
        self._main_ref = weakref.ref(main)
        self.original = copy.deepcopy(main._audio_chain_snapshot())
        self.chain = copy.deepcopy(self.original)
        self.setWindowTitle('Microphone EQ and Noise Cleanup')
        self.resize(820, 650)
        layout = QVBoxLayout(self)
        note = QLabel('Apply to hear changes through microphone monitoring. Use headphones to avoid feedback. '
                      'The same chain processes recordings. Cancel restores the previous settings.')
        note.setWordWrap(True); layout.addWidget(note)
        tabs = QTabWidget(); layout.addWidget(tabs)
        eq_page = QWidget(); eq_layout = QVBoxLayout(eq_page)
        self.eq_enabled = QCheckBox('Enable parametric EQ')
        eq = next(e for e in self.chain if e['type']=='eq')
        self.eq_enabled.setChecked(eq['enabled']); eq_layout.addWidget(self.eq_enabled)
        self.graph = EQGraph(); eq_layout.addWidget(self.graph)
        grid = QGridLayout(); eq_layout.addLayout(grid)
        for column, title in enumerate(('On', 'Filter', 'Frequency (Hz)', 'Gain (dB)', 'Q / bandwidth')):
            grid.addWidget(QLabel(title), 0, column)
        self.rows = []
        for row, band in enumerate(bands_value(eq.get('bands')), 1):
            enabled = QCheckBox(); enabled.setChecked(band['enabled'])
            kind = QComboBox(); kind.addItems(EQ_TYPES); kind.setCurrentText(band['kind'])
            f = QDoubleSpinBox(); f.setRange(20, 20000); f.setValue(band['frequency']); f.setSingleStep(10)
            gain = QDoubleSpinBox(); gain.setRange(-24, 24); gain.setValue(band['gain']); gain.setSingleStep(.5)
            q = QDoubleSpinBox(); q.setRange(.1, 18); q.setValue(band['q']); q.setSingleStep(.1)
            for column, widget in enumerate((enabled, kind, f, gain, q)):
                grid.addWidget(widget, row, column)
                if isinstance(widget, QDoubleSpinBox):
                    widget.setKeyboardTracking(False)
                    widget.valueChanged.connect(self.refresh_graph)
            enabled.toggled.connect(self.refresh_graph)
            kind.currentTextChanged.connect(self.refresh_graph)
            self.rows.append((enabled, kind, f, gain, q))
        info = QLabel('Peak and shelf bands use Q to control width. High/low pass filters use a fixed slope; gain and Q do not apply.')
        info.setWordWrap(True); eq_layout.addWidget(info)
        reset = QPushButton('Reset EQ to flat'); reset.clicked.connect(self.reset_eq); eq_layout.addWidget(reset)
        tabs.addTab(eq_page, 'Parametric EQ')
        cleanup = QWidget(); form = QFormLayout(cleanup)
        suppress = next(e for e in self.chain if e['type']=='suppress')
        self.suppress_enabled = QCheckBox('Enable speech noise suppression')
        self.suppress_enabled.setChecked(suppress['enabled']); form.addRow(self.suppress_enabled)
        self.strength = QSpinBox(); self.strength.setRange(0, 100); self.strength.setValue(int(suppress.get('strength',80))); self.strength.setSuffix(' %')
        form.addRow('Suppression strength:', self.strength)
        info = QLabel('RNNoise reduces fan noise and keyboard clicks. Start at 80%; reduce strength if your voice sounds processed. '
                      'Clicks overlapping speech may remain. Processing stays on your computer; no model download is needed. '
                      'Suppression adds approximately 20 ms of audio delay.')
        info.setWordWrap(True); form.addRow(info)
        gate = next(e for e in self.chain if e['type']=='gate')
        self.gate_enabled = QCheckBox('Enable smooth noise gate'); self.gate_enabled.setChecked(gate['enabled']); form.addRow(self.gate_enabled)
        self.threshold = QSpinBox(); self.threshold.setRange(-60,-15); self.threshold.setValue(main.audio_gate_threshold.value()); self.threshold.setSuffix(' dBFS'); form.addRow('Gate threshold:',self.threshold)
        self.original_threshold = main.audio_gate_threshold.value()
        self.attack = QDoubleSpinBox(); self.attack.setRange(.1,100); self.attack.setValue(gate.get('attack',5)); self.attack.setSuffix(' ms'); form.addRow('Gate attack:',self.attack)
        self.release = QDoubleSpinBox(); self.release.setRange(10,2000); self.release.setValue(gate.get('release',150)); self.release.setSuffix(' ms'); form.addRow('Gate release:',self.release)
        tabs.addTab(cleanup, 'Noise Cleanup')
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.refresh_graph()

    @property
    def main(self):
        return self._main_ref()

    def bands(self):
        return [dict(enabled=e.isChecked(),kind=k.currentText(),frequency=f.value(),gain=g.value(),q=q.value()) for e,k,f,g,q in self.rows]

    def refresh_graph(self, *_):
        for _, kind, _, gain, q in self.rows:
            adjustable = kind.currentText() not in ('High pass','Low pass')
            gain.setEnabled(adjustable); q.setEnabled(adjustable)
        self.graph.set_bands(self.bands())

    def reset_eq(self):
        for (e,k,f,g,q), b in zip(self.rows, DEFAULT_BANDS):
            e.setChecked(b['enabled']); k.setCurrentText(b['kind']); f.setValue(b['frequency']); g.setValue(0); q.setValue(b['q'])
        self.refresh_graph()

    def apply(self):
        for e in self.chain:
            if e['type']=='eq': e.update(enabled=self.eq_enabled.isChecked(),bands=self.bands())
            if e['type']=='suppress': e.update(enabled=self.suppress_enabled.isChecked(),strength=self.strength.value())
            if e['type']=='gate': e.update(enabled=self.gate_enabled.isChecked(),attack=self.attack.value(),release=self.release.value())
        self.main.audio_gate_threshold.setValue(self.threshold.value())
        self.main._set_audio_chain(copy.deepcopy(self.chain))

    def accept(self):
        self.apply(); super().accept()

    def reject(self):
        self.main.audio_gate_threshold.setValue(self.original_threshold)
        self.main._set_audio_chain(self.original)
        super().reject()
