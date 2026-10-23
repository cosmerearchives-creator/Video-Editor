# Local fix: live movement was detached from Output

This local build includes the previous background-rendering changes plus a
small correction to the live presentation layer binding. This correction has
not been pushed to GitHub or added to the existing pull request.

## Confirmed cause

MainWindow.settings() made new dictionaries for every layer and removed all
_movement_ fields. MainWindow.render() and clear_output() gave those snapshots
to OutputWindow. WASD changed MainWindow.media_layers, but Output kept painting
the old position. Idle animation continued because it independently animates
the layer being painted. Restarting the presentation generated new snapshots,
which finally included the accumulated position.

## Correction

settings(live_media=True) gives the active layer list to the presentation.
render() and clear_output() request that live list. Ordinary settings() calls
still produce copies without movement runtime fields for project/settings saves.
The movement timer does not call render() or restart text animations. Output's
normal frame timer reads the updated layer position and head turn directly.
Renderer-computed bounds now reach the same object that movement updates.

## Verification

Before this correction, the three new integration tests failed (including both
foreground and background pixel checks). Afterward, all 19 tests and Python
syntax compilation passed locally on Windows, Python 3.12.14 / PySide6 6.12.0.

The new tests execute the real settings(), render(), clear_output(), movement
tick, OutputWindow.set_output()/render_frame() and draw_media_layers() methods
in a small Qt harness. Unrelated UI/device initialization and the proprietary
model renderer are replaced. They verify that pixels change position after
movement without another presentation update; the live layer/head state stays
shared before the first slide and after slide changes; and saved settings
remain serializable and exclude runtime fields.

These local changes have not run through GitHub CI and have not been tested
with your licensed model or in the complete application/device environment.
The previous TEST_WASD.md documents the earlier background-rendering work and
its separate CI run; that CI result does not cover this local correction.

## Try it

Extract the new source ZIP into a separate folder. In dynamic_teleprompter,
run setup_windows.bat, then start_windows.bat. Load your model and enable
Movement. With Output focused, hold/release each WASD key without restarting
the presentation. Position should update immediately and remain fixed after
release. Also try changing slides, changing scale, and saving/reopening the
project. Your shared Cubism runtime is discovered in the same locations.

The full ZIP includes previous local rendering/movement work. The accompanying
live-output-binding.patch isolates this correction to app.py and can be applied
to the previous test build (or reviewed separately from the renderer changes).
