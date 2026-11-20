import unittest

from gta_clips.metadata import generate


class MetadataQualityTests(unittest.TestCase):
    def test_skips_filler_and_keeps_credit_and_source(self):
        result = generate('Okay. Wow! We need to find the car before the race.', 'Racing', 's', '123', 42)
        self.assertEqual(result.title, 'We need to find the car before the race. | @s')
        self.assertIn('@s on Twitch\nhttps://www.twitch.tv/s', result.description)
        self.assertIn('https://www.twitch.tv/videos/123?t=42s', result.description)

    def test_explicit_transcript_is_not_copied_to_public_metadata(self):
        result = generate('What the fuck is happening with this car?', 'Racing', 's', '1', 0)
        self.assertEqual(result.title, 'Racing highlight | @s')
        self.assertNotIn('fuck', result.description)

    def test_empty_numeric_and_one_word_transcripts_use_honest_fallback(self):
        for transcript in ('', '4!', 'Sick.', 'man.'):
            self.assertEqual(generate(transcript, 'Racing', 's', '1', 0).title, 'Racing highlight | @s')
