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

    def test_empty_install_report_does_not_invent_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'browser-upload.json'
            settings.write_text('{}')
            folder = export_tracking(settings)
            self.assertEqual(json.loads((folder / 'posts.json').read_text()), [])
            self.assertIn('No linked posts', (folder / 'index.html').read_text())
