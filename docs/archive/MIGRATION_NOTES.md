# v0.5.0-alpha1 — native engine foundation

This build preserves the alpha10 application and recording workflow. It adds a **real C++ DLL interface** for one performance-critical operation, BGRA chroma keying. Everything else still uses Python/NumPy/Qt/FFmpeg.

## Windows testing

1. Extract ZIP and run `start_windows.bat` normally. Application should work without the native DLL (NumPy fallback).
2. To test native mode, install **Visual Studio 2022 Build Tools** with *Desktop development with C++* and **CMake**, then run `build_native_windows.bat` in the application directory. The script builds `native/dt_engine.dll`.
3. Restart `start_windows.bat`, enable chroma key on a video layer, check transparency and record an MP4. Compare recording diagnostics with the fallback build. You can rename `native/dt_engine.dll` to disable native mode.

## Scope and limits

- Native ABI 1, stateless synchronous C function; no Qt data structures cross the boundary.
- This is **not** GPU rendering, VST hosting, Live2D integration, shared audio capture, or accelerated color grading.
- C++ chroma key does not automatically make full canvas rendering multithreaded; UI/recording behavior remains as in alpha10.
- A developer toolkit is required to compile the optional native DLL. No prebuilt Windows DLL is included. No need to rebuild Python dependencies.
- C++ source lives in `native/` and is suitable for future extension or replacement.
- If native component cannot load, the Python implementation remains functional.

## Next architecture milestones

1. Native frame lifecycle and compositing with bounded queues / timestamp ownership.
2. Windows WASAPI audio engine with safe realtime processing and VST plugin hosting (evaluate JUCE licensing and hosting API).
3. Live2D Cubism Native SDK integration after confirming licensing and getting official SDK resources.
4. Tracking and synchronized audio/video/model timelines.

We deliberately avoid distributing a third-party SDK or pretending its runtime is already implemented.

## v0.5.0-alpha2

Adds optional native CPU brightness/contrast/saturation filtering using an additional C ABI entry point. Python/NumPy fallback remains available. Developer-tool setup can be performed through `install_build_tools_windows.bat`. No Windows binary is bundled and this is not GPU accelerated.
