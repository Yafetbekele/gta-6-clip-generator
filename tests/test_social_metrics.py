import sqlite3
import tempfile
from pathlib import Path
import unittest

from gta_clips.browser_upload import ledger
from gta_clips.social_metrics import match_clip, remote_id, store_social


class SocialMetricTests(unittest.TestCase):
    def test_clip_match_requires_full_title_source_timestamp_and_unique_candidate(self):
        clip = {'id': 'clip', 'title': 'A moment | @streamer',
                'description': 'Source VOD: https://www.twitch.tv/videos/123?t=42s'}
        caption = clip['title'] + '\n' + clip['description']
        self.assertEqual(match_clip(caption, [clip]), clip)
        self.assertIsNone(match_clip(clip['title'], [clip]))
        self.assertIsNone(match_clip(caption.replace('42s', '43s'), [clip]))
        self.assertIsNone(match_clip(caption, [clip, {**clip, 'id': 'other'}]))

    def test_platform_ids_and_account_scoped_instagram_paths(self):
        self.assertEqual(remote_id('tiktok', '/@test/video/123456789/'), '123456789')
        self.assertEqual(remote_id('instagram', '/test/reel/AbCd123/'), 'AbCd123')
        self.assertEqual(remote_id('instagram', '/reel/AbCd123/'), 'AbCd123')
        self.assertIsNone(remote_id('instagram', '/test/reels/'))

    def test_zero_review_state_link_and_history_persist_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(':memory:')
            db.row_factory = sqlite3.Row
            uploads = ledger(Path(directory) / 'posts.sqlite3')
            try:
                db.executescript('''CREATE TABLE posts(platform,video_id,clip_id,account,url,source,PRIMARY KEY(platform,video_id));
                    CREATE TABLE post_stats(platform,video_id,fetched,published,views,likes,comments,PRIMARY KEY(platform,video_id,fetched));''')
                clip = {'id': 'clip', 'title': 'Title | @streamer',
                        'description': 'https://www.twitch.tv/videos/123?t=42s', 'finished': 1}
                with uploads:
                    uploads.execute("INSERT INTO posts VALUES('tiktok','test','clip','digest','under_review',1,2,'','')")
                row = {'href': '/@test/video/123456789', 'caption': clip['title'] + ' ' + clip['description'],
                       'views': '0', 'likes': '0', 'comments': '0', 'privacy': 'Only me', 'status': 'Content under review'}
                self.assertEqual(store_social(db, uploads, 'test', 'tiktok', [row], [clip], 100), 1)
                self.assertEqual(db.execute('SELECT views,likes,comments FROM post_stats').fetchone()[:], (0, 0, 0))
                self.assertEqual(db.execute('SELECT clip_id FROM posts').fetchone()[0], 'clip')
                self.assertEqual(db.execute('SELECT privacy,status FROM post_observations').fetchone()[:], ('Only me', 'Content under review'))
                self.assertEqual(uploads.execute('SELECT state,url FROM posts').fetchone()[:],
                                 ('under_review', 'https://www.tiktok.com/@test/video/123456789'))
                self.assertEqual(store_social(db, uploads, 'test', 'tiktok', [row], [clip], 100), 0)
                row['views'] = '15'
                self.assertEqual(store_social(db, uploads, 'test', 'tiktok', [row], [clip], 200), 1)
                row['views'] = '1.5K'
                self.assertEqual(store_social(db, uploads, 'test', 'tiktok', [row], [clip], 300), 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM post_stats').fetchone()[0], 2)
            finally:
                db.close()
                uploads.close()
