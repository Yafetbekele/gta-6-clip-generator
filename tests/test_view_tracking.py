import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gta_clips.view_tracking import export_tracking


class TrackingExportTests(unittest.TestCase):
    def test_exports_latest_and_history_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            settings = root / 'browser-upload.json'
            settings.write_text('{}')
            db = sqlite3.connect(data / 'state.sqlite3')
            db.executescript('''CREATE TABLE posts(platform,video_id,clip_id,account,url);
                CREATE TABLE post_stats(platform,video_id,fetched,views,likes,comments);
                INSERT INTO posts VALUES('youtube','abcdefg','<clip>','gta6-1','https://youtube.com/shorts/abcdefg');
                INSERT INTO post_stats VALUES('youtube','abcdefg',1,10,1,0);
                INSERT INTO post_stats VALUES('youtube','abcdefg',2,25,2,1);''')
            db.close()
            before = (data / 'state.sqlite3').read_bytes()
            folder = export_tracking(settings)
            self.assertEqual(json.loads((folder / 'posts.json').read_text())[0]['views'], 25)
            self.assertEqual(len(json.loads((folder / 'view-history.json').read_text())), 2)
            self.assertIn('&lt;clip&gt;', (folder / 'index.html').read_text())
            self.assertEqual((data / 'state.sqlite3').read_bytes(), before)
            export_tracking(settings)

    def test_banned_accounts_and_old_handles_are_left_off_the_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            settings = root / 'browser-upload.json'
            settings.write_text('{}')
            (root / 'config.toml').write_text(
                '[[accounts]]\nname = "gta6-1"\nbanned = ["instagram"]\n'
                '[[accounts]]\nname = "gta6-2"\nbanned = ["instagram:@old_handle"]\n')
            db = sqlite3.connect(root / 'data' / 'state.sqlite3')
            db.executescript('''CREATE TABLE posts(platform,video_id,clip_id,account,url);
                CREATE TABLE post_stats(platform,video_id,fetched,views,likes,comments);
                CREATE TABLE clips(id,title);
                INSERT INTO clips VALUES('c1','Big moment');
                INSERT INTO clips VALUES('c2','Old handle moment');
                INSERT INTO clips VALUES('c3','New handle moment');
                INSERT INTO posts VALUES('instagram','abc','c1','gta6-1','');
                INSERT INTO posts VALUES('youtube','defghij','c1','gta6-1','');
                INSERT INTO posts VALUES('instagram','Old1','c2','gta6-2','https://www.instagram.com/old_handle/reel/Old1/');
                INSERT INTO posts VALUES('instagram','New1','c3','gta6-2','https://www.instagram.com/new_handle/reel/New1/');
                INSERT INTO post_stats VALUES('instagram','abc',1,5000,NULL,NULL);
                INSERT INTO post_stats VALUES('instagram','Old1',1,4000,NULL,NULL);
                INSERT INTO post_stats VALUES('instagram','New1',1,77,NULL,NULL);
                INSERT INTO post_stats VALUES('youtube','defghij',1,120,3,0);''')
            db.close()
            page = (export_tracking(settings) / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('5,000', page)
            self.assertNotIn('Old handle moment', page)
            self.assertIn('New handle moment', page)
            self.assertIn('<b>120</b>', page)
            self.assertIn('2 posts on banned accounts are hidden', page)

    def test_same_reel_from_publisher_and_browser_is_counted_once(self):
        from gta_clips.view_tracking import merge_same_reel
        rows = merge_same_reel([
            {'platform': 'instagram', 'video_id': '1815', 'clip_id': 'c1', 'url': 'https://www.instagram.com/reel/AbC/',
             'views': 150, 'likes': 2, 'fetched': 10},
            {'platform': 'instagram', 'video_id': 'AbC', 'clip_id': None, 'url': 'https://www.instagram.com/me/reel/AbC/',
             'views': 160, 'likes': None, 'fetched': 20},
            {'platform': 'instagram', 'video_id': 'Other', 'clip_id': None, 'url': 'https://www.instagram.com/me/reel/Other/',
             'views': 5, 'fetched': 20},
        ])
        self.assertEqual(len(rows), 2)
        kept = next(r for r in rows if r['video_id'] == '1815')
        self.assertEqual((kept['clip_id'], kept['views'], kept['likes']), ('c1', 160, 2))

    def test_empty_install_report_does_not_invent_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'browser-upload.json'
            settings.write_text('{}')
            folder = export_tracking(settings)
            self.assertEqual(json.loads((folder / 'posts.json').read_text()), [])
            self.assertIn('No linked posts', (folder / 'index.html').read_text())
