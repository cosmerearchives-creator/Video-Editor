# v0.5.0-alpha18

- Built-in six-band parametric microphone EQ with frequency, gain, Q/bandwidth,
  peak/shelf/pass filters and a response graph. No ReaPlugs installation needed.
- Local RNNoise speech noise suppression with adjustable strength for fan noise
  and keyboard clicks, plus a smooth gate with attack/release controls.
- Open Recording > Microphone EQ / Noise Cleanup. Apply previews through the
  existing microphone monitor; the same settings process recordings. Use headphones.
  EQ and suppression start bypassed so existing recordings keep their original tone.
  Start suppression at 80%; overlapping clicks may remain. Adds about 20 ms delay.
- Mouth sensitivity defaults to 15 for new/missing settings, accepts typed values
  from 0 to 100, and steps by 1. Existing saved sensitivity values are preserved.
- Select Text Output in Visual Layer Editor and drag the preview to reposition it.
  X/Y controls also work; text positioning is preserved in recordings and aligned exports.
- The last working session autosaves every two seconds and when closing. Startup
  restores the script, layers, effects, model setup and presentation position,
  including work not saved as a named project. Recording/listening do not restart.
- Fixed editor ownership during shutdown; added streaming DSP, text/export and
  real window/editor/session regression checks, including the packaged executable.

Alpha17 installations can use Help > Check for Updates. For a first installation,
extract the entire Windows-x64.zip and launch Cosmere Cam.exe. The binary is unsigned.
Licensed Cubism Core and Live2D models remain separate. The first speech alignment
still downloads its English model; microphone suppression itself needs no download.
