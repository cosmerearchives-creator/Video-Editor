import multiprocessing
import os
from pathlib import Path
import sys
import traceback


def main():
    multiprocessing.freeze_support()
    if '--smoke-test' in sys.argv:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        try:
            from smoke import run
            run()
            return
        except Exception:
            if '--smoke-report' in sys.argv:
                path = Path(sys.argv[sys.argv.index('--smoke-report') + 1])
                path.write_text(traceback.format_exc(), encoding='utf-8')
            raise
    # Windowed applications have no console; libraries such as Whisper's
    # progress bars still expect write/flush methods on stdout and stderr.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        folder = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'DynamicTeleprompter'
        folder.mkdir(parents=True, exist_ok=True)
        sys.stderr = (folder / 'application.log').open('a', encoding='utf-8')
    from app import main as launch
    launch()


if __name__ == '__main__':
    main()
