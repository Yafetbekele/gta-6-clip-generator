import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gta_clips.captions import Word
from gta_clips.config import ConfigError, _editing
from gta_clips.highlights import Moment
from gta_clips.remotion_render import caption_data, render_polished


class RemotionTest(unittest.TestCase):
    def test_caption_times_are_clamped_and_invalid_words_removed(self):
        captions = caption_data([Word(2, 4, 'last'), Word(-1, .5, 'first'),
                                 Word(3, 4, 'outside'), Word(1, 1, 'empty'),
                                 Word(float('nan'), 2, 'invalid')], 3)
        self.assertEqual([(w['text'], w['startMs'], w['endMs']) for w in captions],
                         [('first', 0, 500), ('last', 2000, 3000)])

    def test_opt_in_and_unsupported_layouts(self):
        self.assertEqual(_editing({}).renderer, 'ffmpeg')
        self.assertEqual(_editing({'renderer': 'remotion'}).renderer, 'remotion')
        for options in ({'renderer': 'other'}, {'renderer': 'remotion', 'layout': 'split'}):
            with self.assertRaises(ConfigError):
                _editing(options)

    def test_bridge_trims_before_compositing_and_uses_argument_arrays(self):
        with TemporaryDirectory() as folder:
            work = Path(folder)
            project = work / 'project with spaces'
            (project / 'node_modules/@remotion/renderer').mkdir(parents=True)
            with patch.dict('os.environ', {'REMOTION_PROJECT': str(project)}), \
                    patch('gta_clips.remotion_render.shutil.which', return_value='node'), \
                    patch('gta_clips.editing.video_duration', return_value=30), \
                    patch('gta_clips.remotion_render.run') as run:
                render_polished(work / 'input.mp4', work, work / 'output.mp4',
                                Moment(12, 30, 1), 'streamer', [Word(0, 1, 'hello')], 'ffmpeg')
            trim = run.call_args_list[0].args[0]
            self.assertGreater(trim.index('-ss'), trim.index('-i'))
            self.assertEqual(trim[trim.index('-ss') + 1], '12')
            self.assertNotIn('ass=', ' '.join(trim))
            job = json.loads((work / 'remotion-job.json').read_text())
            self.assertEqual(job['durationSeconds'], 30)
            self.assertEqual(job['captions'][0]['startMs'], 0)
            self.assertEqual(run.call_args_list[1].kwargs['timeout'], 1800)
