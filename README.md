# Cosmere Cam / Video Editor

Windows presentation, teleprompter and VTuber recording application. This is the
public source and release repository, replacing the earlier private repository.

## Download and launch

Open [Releases](https://github.com/cosmerearchives-creator/Video-Editor/releases)
and download the asset ending **Windows-x64.zip**. Extract the entire ZIP, then
double-click **Cosmere Cam.exe**. The app folder contains internal files; the
docs folder contains instructions and old release notes. Python and FFmpeg are
included in the portable build. Licensed Cubism Core and your models are separate.

Help → Check for Updates offers checksum-verified updates from this public repo.
Startup checks can be disabled. Updates ask before installing and restarting;
finish recording/exporting first. The previous version is kept for rollback.

## Correct text timing after recording

Use Script output mode with microphone recording. The post-recording option
keeps text visible in preview while recording a clean picture. After Stop, local
Whisper alignment matches the script to the original recorded voice. Review
flagged words, fine-tune start/end times, then export with the presentation's
saved text styles, layout and animations. Final foreground layer ordering and
processed microphone audio are preserved.

First use downloads the English speech model (~145 MB); audio is processed
locally. The clean MP4 and its .assets folder remain available for re-export.
Choose File → Review / Align Recorded Presentation to reopen a session. Turn off
the post-recording option to use the original live-text recording workflow.

## Development

Python 3.13 is the CI baseline. Install requirements-release.txt, then run
**Start Cosmere Cam.bat** from the source root. The original
dynamic_teleprompter/start_windows.bat remains for source compatibility.
Reference notes are in docs/archive.

Run tests from dynamic_teleprompter:

```
python -m unittest discover -s tests -v
python desktop_entry.py --smoke-test --smoke-report ../smoke-source.json
```

python tools/build_release.py builds the Windows portable package and tests the
actual frozen executable before creating a ZIP. The GitHub workflow tests source
on pull requests and publishes a versioned alpha release after a verified main
build. Never overwrite a published version; bump release_info.VERSION first.

Proprietary SDK binaries, models, plugins, credentials and user recordings do not
belong in source control. Existing features include voice following, recording,
visual layers, Live2D idle animation/movement, camera tracking and experimental
isolated VST3 effects. This unsigned alpha build requires normal review,
particularly for skipped/repeated or unusual speech.

The alignment adapter uses [stable-ts](https://github.com/jianfch/stable-ts) and
OpenAI Whisper; dependency versions and license notices are included in portable
releases. Stable-ts development is paused; its pinned version is covered by
release checks rather than assumed compatible.
