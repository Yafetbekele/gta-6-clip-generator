from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gta_clips.captions import Word, write_ass
from gta_clips.context_text import MAX_HOOK_LENGTH, debate_hook, manual_hook


class DebateHookTest(unittest.TestCase):
    def test_frames_a_spoken_line_as_a_debate(self):
        hook = debate_hook('I think the new police chases are way too hard now.', 'Grand Theft Auto VI', 'clip-1')
        self.assertEqual(hook.method, 'debate_quote_v1')
        self.assertEqual(hook.text, 'I think the new police chases are way too hard now.')
        self.assertTrue(hook.label)

    def test_without_speech_asks_about_the_game_not_the_streamer(self):
        for key in ('a', 'b', 'c', 'd', 'e', 'f'):
            hook = debate_hook('', 'Grand Theft Auto VI', key)
            self.assertEqual(hook.method, 'debate_topic_v1')
            self.assertIn('GTA 6', hook.text)
            self.assertLessEqual(len(hook.text), MAX_HOOK_LENGTH)

    def test_non_game_categories_get_a_generic_prompt(self):
        hook = debate_hook('uh', 'Just Chatting', 'x')
        self.assertEqual(hook.method, 'debate_generic_v1')
        self.assertNotIn('Just Chatting', hook.text)

    def test_choice_is_stable_for_a_clip(self):
        self.assertEqual(debate_hook('', 'Grand Theft Auto VI', 'live-1-2'),
                         debate_hook('', 'Grand Theft Auto VI', 'live-1-2'))

    def test_headline_is_burned_in_and_escaped(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'captions.ass'
            hook = debate_hook('Nobody {is} beating this mission first try honestly.', 'Grand Theft Auto VI', 'k')
            write_ass(path, [Word(0, 1, 'hi')], 'someone', 60, hook)
            text = path.read_text(encoding='utf-8')
        line = next(line for line in text.splitlines() if ',Hook,' in line)
        self.assertIn('0:00:00.00,0:01:00.00', line)
        self.assertIn(hook.label, line)
        self.assertIn('“', line)
        self.assertNotIn('{is}', line)

    def test_unreliable_speech_is_not_quoted(self):
        hook = debate_hook('I think the new police chases are way too hard now.', 'Grand Theft Auto VI', 'k',
                           quote=False)
        self.assertEqual(hook.method, 'debate_topic_v1')

    def test_headline_can_sit_just_above_the_video(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'captions.ass'
            write_ass(path, [], 'someone', 10, debate_hook('', 'Grand Theft Auto VI', 'k'), 626)
            self.assertIn(r',Hook,,0,0,0,,{\an2\pos(540,626)}', path.read_text(encoding='utf-8'))

    def test_cleared_hook_draws_nothing(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'captions.ass'
            write_ass(path, [], 'someone', 10, manual_hook(''))
            self.assertNotIn(',Hook,,', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
