import unittest

from postprocess import cues_from_words, merge_alignment, validate_words


class AlignmentTests(unittest.TestCase):
    def test_script_words_align_to_audio_not_live_events(self):
        words = [{'text': 'Hello', 'start': 1., 'end': 1.3},
                 {'text': 'world.', 'start': 1.5, 'end': 2.}]
        result = merge_alignment(['Hello world.'], words, words, 3)
        self.assertEqual([w['start'] for w in result], [1., 1.5])
        self.assertTrue(all(w['reviewed'] for w in result))
        validate_words(result, 3)

    def test_skipped_and_repeated_lines_are_flagged_for_review(self):
        words = [{'text': 'Hello', 'start': 1., 'end': 1.3}]
        result = merge_alignment(['Hello there', 'Hello there'], words, words, 3)
        self.assertEqual(len(result), 4)
        self.assertTrue(all(not w['reviewed'] for w in result))
        self.assertFalse(result[-1]['include'])
        with self.assertRaisesRegex(ValueError, 'review'):
            validate_words(result, 3)

    def test_off_script_low_confidence_and_invalid_times_are_not_silently_accepted(self):
        words = [{'text': 'Hello', 'start': 1., 'end': 1., 'probability': .1}]
        result = merge_alignment(['Hello'], words, [], 3)
        self.assertFalse(result[0]['include'])
        self.assertFalse(result[0]['reviewed'])
        self.assertEqual(len(result[0]['flags']), 3)

    def test_overlap_nan_and_out_of_bounds_block_export(self):
        for start, end in ((-1, 1), (0, 5), (float('nan'), 1), (2, 1)):
            with self.assertRaises(ValueError):
                validate_words([dict(start=start, end=end, include=True, reviewed=True)], 3)

    def test_cues_keep_frozen_settings_and_skip_omitted_words(self):
        spoken = [{'text': 'Hello', 'slide': 0, 'start': 1., 'end': 2., 'include': True},
                  {'text': 'skipped', 'slide': 0, 'include': False},
                  {'text': 'Again', 'slide': 1, 'start': 3., 'end': 4., 'include': True}]
        session = {'groups': ['Hello skipped', 'Again'], 'duration': 5,
                   'slides': [{'motion': {'intro': 'Pop'}}, {'motion': {'intro': 'Fade'}}]}
        cues = cues_from_words(session, spoken)
        self.assertEqual(cues[0]['text'], 'Hello')
        self.assertEqual(cues[0]['end'], 3.)
        self.assertEqual(cues[1]['settings']['motion']['intro'], 'Fade')


if __name__ == '__main__':
    unittest.main()
