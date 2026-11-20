import sqlite3
import unittest
from unittest.mock import MagicMock

from gta_clips.browser_metrics import exact_count, video_id, store_rows, collect_rows


class BrowserMetricTests(unittest.TestCase):
    def test_legacy_table_and_new_content_overview_navigation(self):
        for legacy in (True, False):
            page = MagicMock()
            shorts = page.get_by_role.return_value
            shorts.is_visible.return_value = legacy
            page.locator.return_value.evaluate_all.return_value = []
            self.assertEqual(collect_rows(page), [])
            if legacy:
                shorts.click.assert_called_once()
            else:
                page.get_by_text.return_value.first.click.assert_called_once()

    def test_counts_preserve_unknown_and_real_zero(self):
        for text, expected in [('1,103', 1103), ('902', 902), ('0', 0), ('1.1K', None), ('—', None), ('', None), (None, None)]:
            self.assertEqual(exact_count(text), expected)

    def test_video_identity_uses_remote_id(self):
        self.assertEqual(video_id('/video/hWMhn5jvMjw/edit'), 'hWMhn5jvMjw')
        self.assertEqual(video_id('https://youtube.com/shorts/hWMhn5jvMjw'), 'hWMhn5jvMjw')
        self.assertIsNone(video_id('/video/invalid/edit'))

    def test_snapshot_history_matches_exact_ids_and_is_idempotent(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''CREATE TABLE posts(platform,video_id,clip_id,account,url,source,PRIMARY KEY(platform,video_id));
            CREATE TABLE post_stats(platform,video_id,fetched,published,views,comments,PRIMARY KEY(platform,video_id,fetched));''')
        known = {'hWMhn5jvMjw': {'clip_id': 'clip1', 'published': 10}}
        rows = [{'href': '/video/hWMhn5jvMjw/edit', 'views': '1,103', 'comments': '0'},
                {'href': '/video/Uj8vCyykIKU/edit', 'views': '900', 'comments': '2'}]
        self.assertEqual(store_rows(db, 'gta6-2', known, rows, 100), 2)
        self.assertEqual(store_rows(db, 'gta6-2', known, rows, 100), 0)
        rows[0]['views'] = '1,200'
        self.assertEqual(store_rows(db, 'gta6-2', known, rows, 200), 2)
        self.assertEqual(db.execute('SELECT views FROM post_stats ORDER BY fetched,video_id').fetchall(), [(900,), (1103,), (900,), (1200,)])
        self.assertEqual(db.execute('SELECT clip_id FROM posts ORDER BY video_id').fetchall(), [(None,), ('clip1',)])

    def test_only_exact_public_match_recovers_held_upload(self):
        from gta_clips.browser_upload import ledger
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            db = sqlite3.connect(':memory:')
            uploads = ledger(Path(folder) / 'posts.sqlite3')
            self.addCleanup(db.close)
            self.addCleanup(uploads.close)
            db.executescript('''CREATE TABLE posts(platform,video_id,clip_id,account,url,source,PRIMARY KEY(platform,video_id));
                CREATE TABLE post_stats(platform,video_id,fetched,published,views,comments,PRIMARY KEY(platform,video_id,fetched));''')
            uploads.execute("INSERT INTO posts VALUES('youtube','a','clip','hash','held',1,2,'','failed')")
            uploads.commit()
            clip = {'id': 'clip', 'title': 'A full title', 'description': 'https://www.twitch.tv/videos/123?t=42s'}
            row = {'href': '/video/hWMhn5jvMjw/edit', 'views': '0', 'title': clip['title'],
                   'description': clip['description'], 'visibility': 'Private'}
            store_rows(db, 'a', {}, [row], 100, [clip], uploads)
            self.assertEqual(uploads.execute('SELECT state FROM posts').fetchone()[0], 'held')
            row.update(visibility='Public', description=clip['description'].replace('42s', '43s'))
            store_rows(db, 'a', {}, [row], 101, [clip], uploads)
            self.assertEqual(uploads.execute('SELECT state FROM posts').fetchone()[0], 'held')
            row['description'] = clip['description']
            store_rows(db, 'a', {}, [row], 102, [clip], uploads)
            self.assertEqual(tuple(uploads.execute('SELECT state,finished,url FROM posts').fetchone()),
                             ('posted', 102, 'https://youtube.com/shorts/hWMhn5jvMjw'))
            uploads.close()
            db.close()
