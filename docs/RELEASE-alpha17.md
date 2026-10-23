# v0.5.0-alpha17

- Fixed live WASD movement by giving Output the active layer objects instead of
  stale settings copies. Head poses render in a bounded background worker.
- One portable Windows launcher: **Cosmere Cam.exe**. Dependencies live under
  `app`; old notes and reference material are under `docs`.
- Public GitHub update checks, download verification, update-and-restart and
  previous-version rollback. No GitHub sign-in or token needed.
- Optional clean recording with text kept in the preview, automatic local
  script-to-speech alignment, word timing review and corrected text export.
- Final text uses the saved presentation layout, styles and animation settings.
  Foreground layers remain above text; voice filters affect the final audio
  while alignment uses the original unfiltered voice track.
- Recording assets are grouped in one `.assets` folder beside the clean MP4.

Download the **Windows-x64.zip** asset, extract it completely, and launch
**Cosmere Cam.exe**. GitHub's automatic “Source code” archives are for developers.

The first speech-alignment run downloads the English model (~145 MB). Cubism
Core, Live2D models and VST plugin binaries remain separate licensed/user assets.
Alignment is automatic but imperfect; review flagged words, especially repeated
takes, skipped passages and unusual names. Script output mode is required for
this first post-recording release. Other output modes can still record live text
when the post-recording option is disabled.

The portable binary is unsigned. App settings, model caches and the shared
Cubism runtime are outside the replaceable version folder. Existing source
installations need this one manual download before built-in updates are available.
