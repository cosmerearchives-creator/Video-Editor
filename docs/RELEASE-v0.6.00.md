# v0.6.00 — Final

This is the final v0.6.00 release, published without an alpha or beta suffix.

- The alignment review screen now previews presentation text over the recorded
  video, using saved styles, animations, position and correct source-layer order.
  Edited timings update the preview. Show text overlay compares against clean video.
- Reviewed words with valid timings leave the review queue without removing them
  from the recording/export. Show all words reveals completed rows for editing.
  Invalid timings stay visible; confirmed skipped words can leave the queue.
- Playback timestamps display seconds to three decimal places, matching Start/End.
- Start/Stop Recording is available directly in Script Playback and the Reader.
  All controls share the same recorder, recording indicator and elapsed time,
  and remain disabled while a recording finishes saving.
- Add and remove parametric EQ bands, up to 32. Extra bands keep the same filter,
  frequency, gain and Q controls and appear in the response graph. Settings persist.

Existing alpha17/alpha18 users: Help > Check for Updates offers this release.
For a manual installation, extract the entire Windows-x64.zip and launch
Cosmere Cam.exe. Source code ZIPs are for developers.

Microphone suppression, EQ, session restore, speech alignment and Live2D movement
remain included. Speech alignment uses a local English model (first-use download
about 145 MB). Cubism Core and user models remain separate licensed assets.
The Windows binary remains unsigned; the Final label does not change signing.

Validation includes regression tests plus actual packaged-application checks for
video export, source-layer preview, backward seeking, real playback, review
filtering, timestamp precision, shared recording controls, expandable EQ and
speech alignment. Review uncertain word timings before exporting.
