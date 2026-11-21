"""Instagram Reel numbers are read through the API and stored as snapshots next to the browser's."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import post_stats
from gta_clips.db import connect

NOW = 1_800_000_000


def reply(status, body):
    response = MagicMock(ok=200 <= status < 300, status_code=status)
    response.json.return_value = body
    return response


class InstagramStatsTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "db.sqlite3"
        self.config = SimpleNamespace(paths=SimpleNamespace(database=self.database))
        with connect(self.database) as db:
            db.execute("INSERT INTO oauth(platform, token) VALUES ('instagram@cs2', '{}')")
            db.execute("INSERT INTO posts(platform, video_id, account, url, source) VALUES "
                       "('instagram', 'ShortCode1', 'cs2', 'https://www.instagram.com/cs2/reel/ShortCode1/', 'browser')")
            db.execute("INSERT INTO post_stats(platform, video_id, fetched, views) VALUES "
                       "('instagram', 'ShortCode1', ?, 91)", (NOW - 3600,))

    def fetch(self, responses):
        with patch.object(post_stats, "access_token", return_value=("token", {"user_id": "42"})), \
                patch.object(post_stats, "_instagram_get", side_effect=responses) as get:
            result = post_stats.fetch_instagram(self.config, now=NOW)
        return result, get

    def test_stores_views_for_a_known_post_and_adds_a_new_one(self):
        media = {"data": [
            {"id": "111", "permalink": "https://www.instagram.com/reel/ShortCode1/",
             "timestamp": "2027-01-15T08:00:00+0000", "like_count": 3, "comments_count": 1},
            {"id": "222", "permalink": "https://www.instagram.com/reel/NewCode2/",
             "timestamp": "2027-01-15T09:00:00+0000", "like_count": 0, "comments_count": 0},
        ]}
        views = lambda n: reply(200, {"data": [{"name": "views", "values": [{"value": n}]},
                                               {"name": "shares", "values": [{"value": 2}]}]})
        result, get = self.fetch([reply(200, media), views(150), views(40)])
        self.assertEqual(result["fetched"], 2)
        self.assertTrue(get.call_args_list[0].args[0].startswith("https://graph.facebook.com/v"))
        with connect(self.database) as db:
            rows = db.execute("SELECT video_id, views, likes, shares FROM post_stats WHERE fetched=? "
                              "ORDER BY video_id", (NOW,)).fetchall()
            added = db.execute("SELECT account, source FROM posts WHERE video_id='NewCode2'").fetchone()
        self.assertEqual([tuple(row) for row in rows], [("NewCode2", 40, 0, 2), ("ShortCode1", 150, 3, 2)])
        self.assertEqual(tuple(added), ("cs2", "instagram-api"))

    def test_media_without_insights_keeps_the_browser_views(self):
        media = {"data": [{"id": "111", "permalink": "https://www.instagram.com/reel/ShortCode1/",
                           "timestamp": "2027-01-15T08:00:00+0000", "like_count": 0, "comments_count": 0}]}
        pre_conversion = reply(400, {"error": {"code": 100, "error_subcode": post_stats.PRE_CONVERSION}})
        result, _ = self.fetch([reply(200, media), pre_conversion])
        self.assertEqual((result["fetched"], result["no_insights"]), (0, 1))
        with connect(self.database) as db:
            self.assertEqual(db.execute("SELECT views FROM post_stats").fetchall()[0][0], 91)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM post_stats").fetchone()[0], 1)

    def test_rejected_token_is_reported_not_raised(self):
        result, _ = self.fetch([reply(401, {"error": {"code": 190}})])
        self.assertEqual(result["missing_login"], {"cs2"})


if __name__ == "__main__":
    unittest.main()
