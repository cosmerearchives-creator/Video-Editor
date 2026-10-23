import multiprocessing
import os
from pathlib import Path
import sys
import traceback


def main():
    multiprocessing.freeze_support()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        if any(flag in sys.argv for flag in ('--smoke-test', '--smoke-align', '--smoke-window')):
            sys.stderr = open(os.devnull, 'w')
        else:
            folder = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'DynamicTeleprompter'
            folder.mkdir(parents=True, exist_ok=True)
            sys.stderr = (folder / 'application.log').open('a', encoding='utf-8')
    if any(flag in sys.argv for flag in ('--smoke-test', '--smoke-align', '--smoke-window')):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        try:
            from smoke import run, run_alignment, run_window
            if '--smoke-window' in sys.argv:
                run_window()
            elif '--smoke-align' in sys.argv:
                run_alignment(Path(sys.argv[sys.argv.index('--smoke-align') + 1]))
            else:
                run()
            return
        except Exception:
            if '--smoke-report' in sys.argv:
                path = Path(sys.argv[sys.argv.index('--smoke-report') + 1])
                path.write_text(traceback.format_exc(), encoding='utf-8')
            raise
    from app import main as launch
    launch()


if __name__ == '__main__':
    main()
