"""Saved recording sessions and automatic word alignment against recorded audio."""
from difflib import SequenceMatcher
import json
import math
import os
from pathlib import Path
import re


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def normalized(word):
    return re.sub(r'[^\w]', '', word.casefold())


def script_words(groups):
    return [{'text': text, 'slide': slide, 'start': None, 'end': None,
             'include': False, 'reviewed': False, 'flags': ['Unaligned']}
            for slide, group in enumerate(groups) for text in group.split()]


def flatten_words(result):
    words = []
    for segment in result.get('segments', []):
        for word in segment.get('words', []):
            parts = word.get('word', '').split()
            for index, text in enumerate(parts):
                start, end = float(word['start']), float(word['end'])
                width = (end - start) / max(1, len(parts))
                words.append({'text': text, 'start': start + width * index,
                              'end': start + width * (index + 1),
                              'probability': float(word.get('probability', 1.))})
    return words


def merge_alignment(groups, aligned, recognized, duration):
    """Keep every script word; uncertain or absent speech is explicitly reviewed."""
    expected = script_words(groups)
    target = [normalized(w['text']) for w in expected]
    heard = [normalized(w['text']) for w in recognized]
    confirmed = set()
    for block in SequenceMatcher(None, target, heard, autojunk=False).get_matching_blocks():
        confirmed.update(range(block.a, block.a + block.size))
    actual = [normalized(w['text']) for w in aligned]
    for block in SequenceMatcher(None, target, actual, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            index = block.a + offset
            word, timing = expected[index], aligned[block.b + offset]
            start, end = timing['start'], timing['end']
            flags = []
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + .05):
                flags.append('Invalid or missing audio timing')
            if timing.get('probability', 1) < .45:
                flags.append('Low confidence')
            if end - start > 2.5:
                flags.append('Long word or pause')
            if index not in confirmed:
                flags.append('Script differs from recognized speech; check skip/repeat')
            word.update(start=start, end=end, include=not any('Invalid' in f for f in flags),
                        flags=flags, reviewed=not flags)
    # Repeated phrases may be forced onto one take; flag every repeated
    # multi-word slide rather than silently presenting the alignment as certain.
    repeated = {g.casefold() for g in groups if sum(x.casefold() == g.casefold() for x in groups) > 1}
    for word in expected:
        if groups[word['slide']].casefold() in repeated:
            word['flags'].append('Repeated script passage: verify take')
            word['reviewed'] = False
    return expected


def validate_words(words, duration, require_review=True):
    previous = 0.
    for index, word in enumerate(words):
        if not word.get('include'):
            if require_review and not word.get('reviewed'):
                raise ValueError(f'Word {index + 1} needs review or confirmation to skip')
            continue
        start, end = word.get('start'), word.get('end')
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f'Word {index + 1} has no timing')
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + .05):
            raise ValueError(f'Word {index + 1} has invalid start/end times')
        if start < previous - .01:
            raise ValueError(f'Word {index + 1} overlaps or precedes the previous word')
        if require_review and not word.get('reviewed'):
            raise ValueError(f'Word {index + 1} needs review')
        previous = end


def align_recording(session_path, ffmpeg, progress=lambda message: None, model_name='base.en'):
    """Local inference. Live preview timing is deliberately not used as alignment."""
    from stable_whisper import load_model
    session_path = Path(session_path)
    session = json.loads(session_path.read_text(encoding='utf-8'))
    audio = session_path.parent / session['voice']
    if not audio.is_file():
        raise ValueError('The original voice track is missing')
    if session.get('audio_dropped_chunks', 0):
        progress('Audio capture had dropped chunks; their gaps will need review.')
    cache = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'DynamicTeleprompter' / 'Runtime' / 'Whisper'
    cache.mkdir(parents=True, exist_ok=True)
    # Whisper's decoder calls ffmpeg by name; pass the selected executable via
    # an explicit decode here so no global PATH mutation is needed.
    import subprocess
    import numpy as np
    decoded = subprocess.run([ffmpeg, '-v', 'error', '-i', str(audio), '-f', 's16le',
                              '-ac', '1', '-ar', '16000', 'pipe:1'], capture_output=True, check=True)
    waveform = np.frombuffer(decoded.stdout, dtype=np.int16).astype(np.float32) / 32768.
    progress('Loading the local speech model (first use downloads model weights)…')
    model = load_model(model_name, device='cpu', download_root=str(cache))
    progress('Automatically aligning your script to the recorded voice…')
    result = model.align(waveform, '\n'.join(session['groups']), language=session.get('language', 'en'),
                         verbose=None, remove_instant_words=False)
    if result is None:
        raise ValueError('No speech could be aligned. Check the voice track and script.')
    progress('Checking for skipped, repeated and off-script words…')
    recognized = model.transcribe(waveform, language=session.get('language', 'en'), verbose=None, fp16=False)
    words = merge_alignment(session['groups'], flatten_words(result.to_dict()),
                            flatten_words(recognized.to_dict()), session['duration'])
    if session.get('audio_dropped_chunks', 0):
        for word in words:
            word['flags'].append('Capture dropped audio: verify timing')
            word['reviewed'] = False
    value = {'format': 'CosmereCamAlignment', 'version': 1,
             'engine': 'stable-ts/Whisper ' + model_name, 'words': words}
    write_json(session_path.with_suffix('.alignment.json'), value)
    return value


def cues_from_words(session, words):
    """Slide-level timing plus word offsets for the original presentation painter."""
    cues = []
    for index, group in enumerate(session['groups']):
        included = [w for w in words if w['slide'] == index and w.get('include')]
        if not included:
            continue
        cues.append({'slide': index, 'text': ' '.join(w['text'] for w in included),
                     'start': included[0]['start'], 'speech_end': included[-1]['end'],
                     'words': included, 'settings': session['slides'][index]})
    for index, cue in enumerate(cues):
        cue['end'] = cues[index + 1]['start'] if index + 1 < len(cues) else session['duration']
    return cues
