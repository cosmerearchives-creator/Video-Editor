import subprocess
import time
import weakref

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from updates import check_for_update, install_update, installation_root, rollback


class Task(QThread):
    done = Signal(object, str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.done.emit(self.function(), '')
        except Exception as exc:
            self.done.emit(None, str(exc))


class UpdateController(QObject):
    def __init__(self, window, menu):
        super().__init__(window)
        # QObject already owns this controller through its parent. Keeping a
        # strong Python reference back to the window creates a teardown cycle.
        self._window_ref = weakref.ref(window)
        self.task, self.installing = None, False
        menu.addAction('Check for Updates…', lambda: self.check(True))
        self.automatic = menu.addAction('Check for Updates at Startup')
        self.automatic.setCheckable(True)
        self.automatic.setChecked(window.local_settings.value('updates/automatic', True, type=bool))
        self.automatic.toggled.connect(lambda value: self.window.local_settings.setValue('updates/automatic', value))
        menu.addAction('Roll Back to Previous Version…', self.roll_back)
        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.timeout.connect(self.startup)
        self.startup_timer.start(3000)

    @property
    def window(self):
        return self._window_ref()

    def startup(self):
        last = float(self.window.local_settings.value('updates/last_check', 0))
        if self.automatic.isChecked() and time.time() - last > 6 * 3600:
            self.check(False)

    def busy(self):
        return (self.window.recorder is not None or
                bool(getattr(self.window, '_post_busy', False)) or
                bool(getattr(self.window, '_post_dialog', None)))

    def check(self, manual):
        if self.task is not None:
            return
        self.task = Task(check_for_update, self)
        self.task.done.connect(lambda result, error: self.checked(result, error, manual))
        self.task.finished.connect(self.task.deleteLater)
        self.task.start()

    def checked(self, release, error, manual):
        self.task = None
        if error:
            if manual:
                QMessageBox.warning(self.window, 'Update check failed', error)
            return
        self.window.local_settings.setValue('updates/last_check', time.time())
        if not release:
            if manual:
                QMessageBox.information(self.window, 'Updates', 'You have the latest available version.')
            return
        if self.busy():
            if manual:
                QMessageBox.information(self.window, 'Update available', 'Finish recording or exporting, then check again.')
            return
        if installation_root() is None:
            QMessageBox.information(self.window, 'Update available',
                'Version ' + release['tag'] + ' is available. Install the Windows portable ZIP from:\n' + release['page'])
            return
        answer = QMessageBox.question(self.window, 'Update and Restart?',
            release['tag'] + '\n\n' + release['notes'][:6000] +
            '\n\nDownload, verify and install this update? Your project can be saved before restart.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.installing = True
        self.window.record_toggle.setEnabled(False)
        self.task = Task(lambda: install_update(release, installation_root()), self)
        self.task.done.connect(self.installed)
        self.task.finished.connect(self.task.deleteLater)
        self.task.start()

    def installed(self, result, error):
        self.task = None
        self.installing = False
        self.window.record_toggle.setEnabled(True)
        if error:
            QMessageBox.warning(self.window, 'Update failed', error + '\nThe current version remains available.')
            return
        self.restart()

    def restart(self):
        # Existing closeEvent handles project saving and recording finalization.
        if self.window.close():
            subprocess.Popen([str(installation_root() / 'Cosmere Cam.exe')], cwd=installation_root())

    def roll_back(self):
        if self.task is not None or self.busy() or installation_root() is None:
            QMessageBox.information(self.window, 'Rollback unavailable', 'Use a portable installation and finish current work first.')
            return
        if QMessageBox.question(self.window, 'Roll Back', 'Switch to the previous installed version and restart?') != QMessageBox.StandardButton.Yes:
            return
        try:
            rollback(installation_root())
        except Exception as exc:
            QMessageBox.warning(self.window, 'Rollback failed', str(exc))
            return
        self.restart()
