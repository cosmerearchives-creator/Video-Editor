# WASD background rendering test build

This is a source build of the existing alpha16.1 application with a targeted
Live2D movement fix. It is not a bundled executable. Proprietary Cubism Core,
models, plugins and recordings are not included.

## Try on Windows

1. Extract this source ZIP into a new folder. Keep your working installation.
2. Run `setup_windows.bat`, then `start_windows.bat`. The existing shared
   user runtime or LIVE2D_CORE_PATH is still used for Cubism Core discovery.
3. Load your existing model and enable Movement for its Live2D layer. Focus
   Output or a non-text control; WASD intentionally does not intercept typing.
4. Hold D and A, then W and S, and try diagonals and direction reversals.
   Translation should remain responsive while the head pose changes.
5. Release the keys. Position should remain fixed while the head recenters.
6. Hold movement against every boundary and test different layer scales and
   perspective depth settings. Visible mesh bounds should remain inside the
   canvas when the character can fit. Oversized characters are centered on
   an axis where fitting is impossible.
7. Test idle blinking/breathing/lip sync, tracking, model switching, resolution
   changes, PNG export after the model appears, and a short recording.
8. Switch to another application while holding a key, return, and verify that
   movement does not stick. Close while a model is loading or animating.

## What changed

- One background worker owns Cubism loading, pose computation and QImage
  triangle rasterization. UI painting draws the latest completed image.
- Pending poses are replaced with the newest request. In-flight results are
  displayed even when newer poses are waiting, preventing image starvation.
- Both control and model/image caches are bounded to five recent instances.
  Separate layers using the same model maintain separate poses.
- Translation uses a virtual head signal and bounded frame times; release
  stops translation immediately. Diagonal acceleration is normalized.
- Proposed positions are clamped using worker-computed mesh bounds, layer
  scale and perspective depth. No DLL/vertex access occurs on the UI thread.
- Errors preserve the last usable image and remain available through the
  existing model error reporting. Shutdown never waits on a raster operation.

## Automated verification

Run from this directory after installing PySide6:

```
python -m unittest discover -s tests -v
python -m compileall -q .
```

On Windows, Python 3.12.14 / PySide6 6.12.0: 16 tests passed. Syntax compilation
passed. A Qt integration test using the actual app input/tick methods and a
blocked stand-in model handled 49 timer ticks in 250 ms, accepted D press/release,
and measured zero translation after release. Worker tests cover coalescing,
thread ownership, errors, eviction and shutdown; movement tests cover frame
times, release, speed, diagonals and scaled geometry bounds.

The GitHub Windows source-check workflow now runs the same tests on Python
3.13 and publishes its usual Cosmere-Cam-Windows-source artifact after success.
Python 3.13 CI status must be checked separately; the local interpreter was 3.12.

## Limits requiring a real model test

The local Qt tests use stand-in meshes and do not validate an actual licensed
Cubism SDK/model, audio devices, full application startup or recording. Existing
CPU mesh rendering and its clipping/blending limitations remain. Slow models
can still have a low head-animation frame rate; movement uses the last image
without waiting for it. Initial loading is asynchronous, so the model is absent
until its first image is ready; wait for it to appear before exporting a PNG
or starting a recording. Bounds are conservative vertex bounds, not an exact
alpha silhouette. More than five simultaneously active model instances can
cause cache churn. Other expensive UI operations can still stall Qt, and the
existing 33 ms movement cap deliberately drops long stalled intervals.

For diagnostics, `live2d_render_stats(layer)` in live2d_runtime returns the last
worker job time and completion count. OutputWindow.render_time_ms measures the
UI canvas render. Compare them while moving your actual model.
