DYNAMIC TELEPROMPTER v0.2.11 — WINDOWS 11
=======================================

SETUP
1. Install Python 3.10 or newer from python.org (check Add Python to PATH).
2. Extract the entire ZIP into a folder.
3. Double-click start_windows.bat. First run installs Python dependencies.
4. Open the Voice Tracking tab and click Download Offline English Model.
   This downloads the ~40 MB Vosk English model from alphacephei.com.
   It is stored in your user home folder and reused on future launches.
5. Choose your microphone and click Start Voice Following, then speak your script.

VOICE FOLLOWING
- Tracking is OFF until you click Start Voice Following.
- Automatic mode matches recognized words to the nearby script and moves slides.
- Hybrid (voice + manual correction) supports Previous/Next at any time.
- Manual mode displays recognized speech but does not auto-advance.
- Silence does not advance slides; recognition can make mistakes, especially
  for unusual names or when you deviate significantly from the script.
- The fantasy dictionary currently affects SPELLCHECK ONLY, not Vosk recognition.
- The Speech output mode displays recently heard speech; Hybrid chooses either
  script or spoken text using a similarity score. Both are experimental.
- Press Stop Voice Following before making major edits to the script.

DISPLAY
- GUI 1 edits text, changes settings, and controls manual navigation.
- GUI 2 is an independent OBS output window; green background and black text.
- Output Settings controls characters per line, lines per slide, animations,
  color, alignment and Script/Speech/Hybrid/Comparison modes.
- Previous/Next shortcuts: Ctrl+Left / Ctrl+Right.
- Prepare / Reset Script clears output and returns to the beginning.
- Save Display Settings persists display settings.
- Dictionary entries persist in ~/.dynamic_teleprompter_dictionary.json.

REQUIREMENTS & PRIVACY
- Windows 11, Python 3.10+, working input microphone.
- Internet is needed initially to install Python packages and model.
- Speech recognition then runs locally. No audio is uploaded by this app.
- Speech model and custom dictionary remain stored locally.

KNOWN LIMITATIONS
- This is an early automatic alignment prototype, not a precision word-timed
  captioning engine; recognition may lag a little or miss fantasy words.
- Grammar checking is heuristic. Animations support Fade or None at present.
- Large model downloads and installation can take some time.
- Window and microphone behavior require testing on your Windows machine.


NEW IN 0.2.2 - MICROPHONE DIAGNOSTICS
On Script & Playback a microphone diagnostics panel shows a live input meter,
a listening status, and Mute Listening / Unmute Listening. Start Voice Following
on Voice Tracking first. The meter shows raw microphone activity even when muted.
Muting drops incoming sound from recognition and prevents slide movement due to
new microphone input. When unmuting, recognition resumes with a fresh decoder.
If the meter never moves, check the selected input device and Windows mic access.

VERSION 0.2.2 CHANGES
- Start/Stop Voice Following button beside microphone diagnostics in Script & Playback.
- Rename Dictionary from within Dictionary dialog; the custom name is saved between sessions.
- Avoid resetting unchanged output text/fade animation on partial speech recognition updates.
- Timed output no longer extends its countdown on identical recognition updates.


VERSION 0.2.3: Text Effects + Projects
- Text Effects tab: outlines, drop shadows, and glow with adjustable colors/offsets/radii.
- Apply to the entire presentation or a numbered slide range. Slide overrides take priority.
- Open Project / Save Project / Save Project As save script, display settings, and all effects
  together in a .teleprompter.json file. Each script can have separate settings.
- When you change pagination (character count, line count, grouping), slide numbering
  changes. Existing per-slide rules remain assigned to their original slide numbers;
  revisit overrides after changing pagination.
- Existing global Save Display Settings remains available for legacy preferences.
- The editable spell dictionary remains stored separately in your home folder.

Version 0.2.4: Text Effects includes Ignore Global Effects, Restore Global Effects,
and Remove Slide-Specific Effects for selected slide ranges. Global exclusions
are saved with each project and existing v0.2.3 project files remain compatible.

NEW IN v0.2.5 — ANIMATIONS
--------------------------
Open the Animations tab to choose separate Entrance and Exit effects.
Choose Animate by: Phrase, Word or Letter, plus individual animation durations
and the stagger delay between each unit.
Apply either to the entire presentation or to a specified slide range.
Slide range animations override the presentation-wide animation defaults.
They are saved inside each .teleprompter.json project.

Some animations (especially Letter + long phrases + high stagger delay) take
longer to complete. Reduce the stagger or duration for fast speech and rapid
slide changes. "Draw In" and "Wipe" are progressive reveal effects; they do
not trace individual pen strokes. Slide-specific assignments follow the
current pagination numbers and may need review if line grouping is changed.

Version 0.2.6: Animation tab contains Speech-gated reveal and Minimum hold after entrance.
When voice following is active, recognized script words appear progressively; manually advancing slides bypasses the hold.

VERSION 0.2.7: If a recognized word does not show, check Voice Tracking's recognized text and whether Speech-gated reveal is checked. Auto advancement now waits for the most recently displayed word animation plus the minimum hold.

v0.2.9 Reader Teleprompter:
- A third window, 'Dynamic Teleprompter — Reader', shows the whole script.
- The next word to speak is highlighted in yellow. The reader view follows
  the recognized script position with smooth continuous scrolling.
- Use 'Show Reader Teleprompter' on Script & Playback if you closed the window.
- On launch, the app attempts to position both preview windows beside the main
  control center when display dimensions permit. On smaller screens it tiles
  them below the main window. All windows remain independently movable.
- A reader scrolls with detected *word position*, not a timed auto-scroll;
  recognition quality and latency affect how closely it tracks your voice.


v0.2.10: Reduced voice onset delay using smaller audio chunks and immediate exact one-word script matching. Speech recognition may still introduce a short delay depending on your microphone and voice model.

v0.2.11: Full slide height is reserved for incremental speech-gated text reveal; the first line no longer jumps when a second line appears.

Version 0.2.12: Manually pre-displayed slides stay stable as voice tracking catches up. Use Bring All Display Windows to Front on the Script & Playback tab to recover the reader and OBS windows.


VERSION 0.3.0 INTERFACE GUIDE
-----------------------------
Four main tabs: Script Playback, Voice Tracking, Output, Settings.
Voice Tracking includes live recognition and Speech Test.
Output includes General, Text Effects, and Animations.
File menu: open/save scripts and projects; Exit.
Edit menu: spelling and grammar toggles; dictionary.
View menu: show reader/OBS windows, bring both to front, and
independently lock each window above other desktop windows.
Settings tab: dictionary and default display settings.
The script playback tab includes a compact microphone signal meter,
start/stop listening, mute, and basic script navigation.
Animations tab: sample text preview and five-slide transition test.
These tests use a separate window and do not change the active OBS output.
Existing .teleprompter.json files from v0.2.x are supported.

v0.3.1 additions
-----------------
- Start Presentation is on Script Playback. Choose whether to show slide 1 in
  advance and whether to start voice following, then click Start Presentation.
- Microphone diagnostics are beside navigation controls. Voice model status is
  shown in the Voice Tracking and Speech Test tabs. On first launch, missing
  English model prompts for download. You can decline and download later.
- Output > Animations > Run 5-Slide Transition Test now listens to your voice.
  Read the five supplied sentences in sequence to advance; it uses an isolated
  preview, not the OBS window. Normal microphone tracking stops while this
  isolated test has the microphone. Use Stop Test to end it.
- Close the main window or choose File > Exit to save any unsaved project changes.
- Startup options are saved with each project. Older projects use enabled
  defaults for both options.


0.3.2: Press Alt for File/Edit/View/Help. View > Organize Windows arranges the three windows across the top half of your monitor. The microphone strip uses the speaker icon to mute and the circular status icon to start/stop listening.

0.3.3: View > Organize Windows now accounts for title bars and native window borders and tiles Reader / OBS without a gap.

Live2D initial-pose experimental rendering: see LIVE2D_SETUP.txt.
