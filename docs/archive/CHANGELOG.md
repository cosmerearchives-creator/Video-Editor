## v0.5.0-alpha13.3
- Discover webcam indexes asynchronously; show selection dropdown and Refresh button.
- Remember selected camera and display device/backend tracking status.
- Better camera access errors; auto-enable tracking for selected Live2D layer.

## v0.5.0-alpha10
- Live2D model manifest import and resource validation in Visual Layer Editor.
- Model layers saved with projects; native Cubism rendering remains pending.

## v0.5.0-alpha9
- Audio/VST process timing telemetry, monitoring queue/dropout counters and diagnostics reset.
- Recording JSON includes DSP timing snapshot.

## v0.5.0-alpha6
- Experimental, optional VST3 effect hosting with Pedalboard.
- Add/remove, reorder, bypass, project persistence, and status reporting.
- Included optional host installer.

# Dynamic Teleprompter Changelog

## v0.5.0-alpha5
- Ordered built-in microphone effects chain in Output -> Recording: Gain, Noise Gate, Soft Clip.
- Per-effect bypass with a check box; Up/Down buttons change effect order.
- Soft Clip drive control (100-500%); gate threshold and gain retain existing controls.
- Same chain processes live microphone monitoring and recorded microphone audio.
- Effects chain and controls are persisted in project settings.
- Diagnostics JSON includes active effects chain and Soft Clip drive.
- Audio monitoring worker now reads synchronized setting snapshots instead of GUI widget values.
- No VST2/VST3 plugin hosting yet; native audio gain and gate remain available.

## v0.5.0-alpha4
- Opt-in live audio monitoring with output device selector and independent volume.
- Continuous worker-thread DSP for recorded audio, with audio streamed to monitoring.
- Gain and gate update without restarting audio capture.
- Safe stop/start stream transitions for preview-only and recording monitoring.
- Recording diagnostic adds dropped microphone chunk count.

## Previous releases
The v0.5 alpha3 C++ DSP foundation, v0.5 alpha2 native color filters, and v0.4
presentation/recording features remain included.

## v0.5.0-alpha8
- Isolated VST3 audio processing and generic parameter inspection into a spawned subprocess.
- Added host timeouts, five-second plugin failure cooldown, automatic restart on future processing.
- Kept built-in effects and audio monitoring available if VST3 processing fails.

## v0.5.0-alpha12
- Experimental initial-pose Live2D rendering now supports basic clipping masks, inverted masks, and add/multiply blend modes.
- Still no webcam tracking, physics, expressions, or full Cubism GPU rendering.

## v0.5.0-alpha13.2
- Permanent per-user Cubism Core runtime discovery and installer.
- Unified first-run dependency setup, with optional developer tool setup.
- Retained fallback to legacy per-release local Cubism Core DLL.
