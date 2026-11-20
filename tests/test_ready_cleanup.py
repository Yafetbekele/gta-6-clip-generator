import tempfile
from pathlib import Path
import unittest

from gta_clips.browser_upload import scan, ledger, claim, finish
from gta_clips.ready_cleanup import cleanup_used


class ReadyCleanupTests(unittest.TestCase):
    def test_keeps_clip_until_all_destinations_accept_even_if_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'ready'
            folder = root / '2026-11-20' / 'test'
            folder.mkdir(parents=True)
            video = folder / '0000 clip.mp4'
            video.write_bytes(b'video')
            note = video.with_suffix('.txt')
            note.write_text('Title:\nTitle\n\nDescription:\nhttps://twitch.tv/streamer\n\nHashtags:\n#clips\n\nClip ID: clip-1\n')
            state = Path(directory) / 'state'
            db = ledger(state / 'posts.sqlite3')
            try:
                config = {'accounts': {'test': {'youtube': {'channel_id': 'configured'},
                                              'tiktok': {'handle': 'configured', 'auto_post': False}}}}
                clip, = scan(root, 'test')
                claim(db, 'youtube', 'test', clip)
                finish(db, 'youtube', 'test', clip, 'posted')
                self.assertEqual(cleanup_used(config, root, state), 0)
                claim(db, 'tiktok', 'test', clip)
                self.assertEqual(cleanup_used(config, root, state), 0)
                finish(db, 'tiktok', 'test', clip, 'under_review')
                source = Path(directory) / 'source.mp4'
                source.write_bytes(b'source')
                self.assertEqual(cleanup_used(config, root, state), 1)
                self.assertFalse(video.exists())
                self.assertFalse(note.exists())
                self.assertTrue(source.exists())
                self.assertEqual(db.execute('select count(*) from posts').fetchone()[0], 2)
                self.assertEqual(cleanup_used(config, root, state), 0)
            finally:
                db.close()
