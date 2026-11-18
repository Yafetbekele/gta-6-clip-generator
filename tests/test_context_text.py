from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from gta_clips.config import PathsConfig, load_config
from gta_clips.context_text import manual_hook, suggest_hook
from gta_clips.db import connect
from gta_clips.hook_edit import edit_hook


class ContextTextTest(unittest.TestCase):
    def test_looks_beyond_greetings_and_preserves_quote(self):
        text = 'Hey chat, welcome back to the stream! I accidentally booked the flight for next year.'
        hook = suggest_hook(text)
        self.assertEqual(hook.text, 'I accidentally booked the flight for next year.')
        self.assertEqual(hook.evidence, hook.text)
        self.assertIn(hook.text, text)

    def test_keeps_negation_and_does_not_invent_visual_context(self):
        text = 'I did not crash the rented sports car.'
        self.assertEqual(suggest_hook(text).text, text)
        for text in ('', 'yeah okay uh um', 'Thanks for the gifted subs!',
                     'I do not know what I said.', 'word ' * 50):
            self.assertEqual(suggest_hook(text).text, '')

    def test_manual_limits_and_clear(self):
        self.assertEqual(manual_hook('  Clear\ncontext  ').text, 'Clear context')
        self.assertEqual(manual_hook('').method, 'disabled')
        with self.assertRaises(ValueError):
            manual_hook('a' * 73)


class HookEditTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = replace(load_config(Path(__file__).resolve().parents[1] / 'config.example.toml'),
                              paths=PathsConfig(self.base))
        (self.base / 'original.mp4').write_bytes(b'original')
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO vods(id,streamer,status) VALUES ('1','streamer','done')")
            db.execute("""INSERT INTO clips(id,vod_id,streamer,path,start,duration,transcript,title,description,hashtags)
                VALUES ('clip','1','streamer','original.mp4',40,30,
                'I accidentally booked the flight for next year.','Title','Credit','#Twitch')""")

    def state(self):
        with connect(self.config.paths.database) as db:
            return dict(db.execute("SELECT * FROM clips WHERE id='clip'").fetchone())

    def test_suggest_only_does_not_download_or_change_clip(self):
        with patch('gta_clips.hook_edit.download_vod') as download:
            hook = edit_hook(self.config, 'clip', suggest_only=True)
        download.assert_not_called()
        self.assertEqual(hook.method, 'transcript_quote_v1')
        self.assertEqual(self.state()['path'], 'original.mp4')

    def test_approved_clip_is_not_edited(self):
        with connect(self.config.paths.database) as db:
            db.execute("UPDATE clips SET state='approved'")
        with patch('gta_clips.hook_edit.download_vod') as download, self.assertRaises(ValueError):
            edit_hook(self.config, 'clip', 'New context')
        download.assert_not_called()

    def test_rebuild_keeps_original_and_returns_to_same_pending_queue(self):
        def rendered(source, work, target, *args, **kwargs):
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b'edited')
        with patch('gta_clips.hook_edit.download_vod', return_value=self.base/'raw.mp4') as download, \
                patch('gta_clips.hook_edit.render', side_effect=rendered) as render:
            edit_hook(self.config, 'clip', 'Booked for the wrong year')
        self.assertEqual(download.call_args.kwargs['section'], (37, 73))
        self.assertEqual(render.call_args.kwargs['hook_text'], 'Booked for the wrong year')
        self.assertEqual(self.state()['state'], 'pending')
        self.assertEqual((self.base / 'original.mp4').read_bytes(), b'original')
        self.assertEqual((self.base / self.state()['path']).read_bytes(), b'edited')

    def test_concurrent_approval_prevents_video_swap(self):
        def rendered(source, work, target, *args, **kwargs):
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b'edited')
            with connect(self.config.paths.database) as db:
                db.execute("UPDATE clips SET state='approved'")
        with patch('gta_clips.hook_edit.download_vod', return_value=self.base/'raw.mp4'), \
                patch('gta_clips.hook_edit.render', side_effect=rendered), self.assertRaises(ValueError):
            edit_hook(self.config, 'clip', 'New context')
        self.assertEqual(self.state()['path'], 'original.mp4')
        self.assertFalse(list((self.base/'clips').glob('*.mp4')))
