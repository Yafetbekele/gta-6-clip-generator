from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import random
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips.config import AccountConfig, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.post_stats import parse_url, record
from gta_clips.ready import FOLDER, allowed_by, export_due
from gta_clips.view_model import MIN_POSTS, train


class AllowedByTest(unittest.TestCase):
    def test_clips_are_spread_through_the_day(self):
        day = datetime(2026, 11, 19)
        self.assertEqual(allowed_by(6, day.replace(hour=0, minute=1)), 1)
        self.assertEqual(allowed_by(6, day.replace(hour=3, minute=59)), 1)
        self.assertEqual(allowed_by(6, day.replace(hour=4)), 2)
        self.assertEqual(allowed_by(6, day.replace(hour=23, minute=59)), 6)
        self.assertEqual(allowed_by(0, day.replace(hour=12)), 0)


class ExportTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = load_config(ROOT / "config.example.toml")
        self.config = replace(base, paths=PathsConfig(Path(folder.name)),
                              accounts=(AccountConfig("gta6-1", "Grand Theft Auto VI", label="GTA 6", youtube="GTA 6 Clips",
                                                      tiktok="gta_clips", email="clips@example.com"),
                                        AccountConfig("irl-en", "Just Chatting", "en")))
        self.now = datetime(2026, 11, 19, 23, 0).timestamp()
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")

    def _clip(self, clip_id, account="gta6-1", messages=10, baseline=1.0, score=None, state="pending",
              kind="burst", age=3600, write=True):
        relative = f"clips/{account}/{clip_id}.mp4"
        if write:
            video = self.config.paths.data_dir / relative
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(clip_id.encode())
        with connect(self.config.paths.database) as db:
            db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                          description, hashtags, state, created, account)
                          VALUES (?, 'v', 'streamer', ?, 0, 30, '', ?, 'desc', '#gta6', ?, ?, ?)""",
                       (clip_id, relative, f"Title {clip_id}", state, int(self.now - age), account))
            anchor = db.execute("SELECT COALESCE(MAX(anchor), 0) + 1000 FROM live_bursts").fetchone()[0]
            db.execute("""INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name, messages, state, retry_at, clip_id,
                          trigger_kind, account) VALUES ('st', ?, 'b', 'streamer', 'g', ?, 'clipped', 0, ?, ?, ?)""",
                       (anchor, messages, clip_id, kind, account))
            db.execute("""INSERT INTO burst_features(stream_id, anchor, baseline_per_10s, observed_seconds,
                          multiplier, minimum_messages, warmup_seconds, unique_chatters)
                          VALUES ('st', ?, ?, 600, 3, 8, 300, 5)""", (anchor, baseline))
            if score is not None:
                db.execute("INSERT INTO clip_ratings(clip_id, score, notes) VALUES (?, ?, '')", (clip_id, score))

    def _exported(self):
        with connect(self.config.paths.database) as db:
            return [row[0] for row in db.execute("SELECT clip_id FROM ready_exports ORDER BY exported, rowid")]

    def test_offers_only_allowed_clips_with_their_note(self):
        self._clip("small", messages=10)
        self._clip("big", messages=40)
        self._clip("loved", messages=5, score=5)
        self._clip("control", messages=99, kind="control")
        self._clip("disliked", messages=99, score=2)
        self._clip("rejected", messages=99, state="rejected")
        self._clip("old", messages=99, age=4 * 86400)
        self._clip("missing", messages=99, write=False)
        added = export_due(replace(self.config, runtime=replace(self.config.runtime, ready_per_day=10)), self.now)
        self.assertEqual(sorted(self._exported()), ["big", "control", "loved", "small"])
        folder = self.config.paths.data_dir / FOLDER / "2026-11-19" / "gta6-1"
        self.assertEqual({path.parent for path in added}, {folder})
        note = next(path for path in added if "Title loved" in path.name).with_suffix(".txt").read_text(encoding="utf-8")
        self.assertIn("Channel: GTA 6 (gta6-1)\nPost to:\n  YouTube: GTA 6 Clips\n  TikTok: gta_clips\n"
                      "Sign-in email: clips@example.com\n\n", note)
        self.assertNotIn("Instagram", note)
        self.assertIn("#gta6", note)
        self.assertTrue((self.config.paths.data_dir / "clips/gta6-1/loved.mp4").is_file())  # copied, not moved

    def test_order_is_random_without_enough_posts(self):
        for index in range(8):
            self._clip(f"c{index}", messages=10 + index)
        config = replace(self.config, runtime=replace(self.config.runtime, ready_per_day=1))
        picks = set()
        for seed in range(6):
            with connect(config.paths.database) as db:
                db.execute("DELETE FROM ready_exports")
            export_due(config, self.now, random.Random(seed))
            picks.update(self._exported())
        self.assertGreater(len(picks), 1)

    def test_views_model_ranks_by_stats_of_posted_clips(self):
        # Posted clips whose chat jumped more got more views; the model should prefer such clips.
        fetched = int(time.time())
        for index in range(MIN_POSTS + 5):
            clip_id = f"posted{index}"
            self._clip(clip_id, messages=10 + 3 * index, age=10 * 86400)
            record(self.config, f"https://youtube.com/shorts/vid{index:05d}", clip_id)
            with connect(self.config.paths.database) as db:
                db.execute("""INSERT INTO post_stats(platform, video_id, fetched, published, views)
                              VALUES ('youtube', ?, ?, ?, ?)""",
                           (f"vid{index:05d}", fetched, fetched - 25 * 3600, 100 * (index + 1)))
            # The same clip on TikTok, where nothing gets views, must not teach the model anything.
            record(self.config, f"https://www.tiktok.com/@gta_clips/video/{7710000000000000000 + index}", clip_id)
            with connect(self.config.paths.database) as db:
                db.execute("""INSERT INTO post_stats(platform, video_id, fetched, published, views)
                              VALUES ('tiktok', ?, ?, ?, 0)""",
                           (str(7710000000000000000 + index), fetched, fetched - 25 * 3600))
        model = train(self.config)
        self.assertIsNotNone(model)
        self.assertEqual(model.posts, MIN_POSTS + 5)
        self.assertNotIn("platform=tiktok", model.names)
        self._clip("quiet", messages=10)
        self._clip("loud", messages=150)
        config = replace(self.config, runtime=replace(self.config.runtime, ready_per_day=1))
        self.assertIn("time:hour_sin", model.names)
        with patch("gta_clips.ready.EXPLORE", 0):
            added = export_due(config, datetime(2026, 11, 19, 1, 0).timestamp(), random.Random(0))
        self.assertEqual(self._exported(), ["loud"])
        self.assertTrue(added[0].name.startswith("0100-01 streamer - "))
        with connect(self.config.paths.database) as db:
            guess = db.execute("SELECT predicted FROM ready_exports WHERE clip_id='loud'").fetchone()[0]
        self.assertIsNotNone(guess)

    def test_predictions_are_checked_against_real_views(self):
        from gta_clips.view_model import check
        fetched = int(time.time())
        for index in range(12):
            clip_id = f"guessed{index}"
            self._clip(clip_id, age=10 * 86400)
            record(self.config, f"https://youtube.com/shorts/vid{index:05d}", clip_id)
            with connect(self.config.paths.database) as db:
                db.execute("INSERT INTO ready_exports(clip_id, account, day, path, predicted) VALUES (?, 'gta6-1', 'd', ?, ?)",
                           (clip_id, f"p{index}", float(index)))
                db.execute("""INSERT INTO post_stats(platform, video_id, fetched, published, views)
                              VALUES ('youtube', ?, ?, ?, ?)""",
                           (f"vid{index:05d}", fetched, fetched - 25 * 3600, 10 * (index + 1)))
        with connect(self.config.paths.database) as db:
            result = check(db)
        self.assertEqual(result["posts"], 12)
        self.assertEqual(result["rank_correlation"], 1.0)

    def test_daily_share_is_kept_and_clips_are_offered_once(self):
        for index in range(5):
            self._clip(f"c{index}", messages=10 + index)
        self._clip("other", account="irl-en")
        config = replace(self.config, runtime=replace(self.config.runtime, ready_per_day=2))
        early = datetime(2026, 11, 19, 1, 0).timestamp()
        self.assertEqual(len(export_due(config, early)), 2)  # one per account set so far today
        self.assertEqual(len(export_due(config, early)), 0)
        self.assertEqual(len(export_due(config, self.now)), 1)  # gta6-1 gets its second; irl-en has no more
        exported = self._exported()
        self.assertEqual(len(exported), len(set(exported)))
        self.assertIn("other", exported)


class PostLinkTest(unittest.TestCase):
    def test_links_are_parsed(self):
        self.assertEqual(parse_url("https://www.youtube.com/shorts/AbC_123-xy"), ("youtube", "AbC_123-xy"))
        self.assertEqual(parse_url("https://youtu.be/AbC_123-xy"), ("youtube", "AbC_123-xy"))
        self.assertEqual(parse_url("https://www.tiktok.com/@gta_clips/video/7311111111111"), ("tiktok", "7311111111111"))
        self.assertEqual(parse_url("https://www.instagram.com/reel/C9xYz/"), ("instagram", "C9xYz"))
        self.assertIsNone(parse_url("https://example.com/video/1"))


class PostTimeTest(unittest.TestCase):
    def test_publish_time_is_read_from_post_ids(self):
        from gta_clips.post_stats import posted_at
        self.assertEqual(posted_at("tiktok", "7710089425287499030"), 1795145083.0)
        self.assertAlmostEqual(posted_at("instagram", "Df8zS4RIuk_"), 1795149034.491, places=2)
        self.assertIsNone(posted_at("instagram", "bad code!"))
        self.assertIsNone(posted_at("youtube", "hWMhn5jvMjw"))


class ViewsAtTest(unittest.TestCase):
    def test_views_are_read_at_24_hours(self):
        from gta_clips.view_model import views_at
        hour = 3600
        self.assertEqual(views_at([(20 * hour, 100), (28 * hour, 300)]), 200)
        self.assertEqual(views_at([(30 * hour, 500)]), 500)  # only a later snapshot, close enough
        self.assertIsNone(views_at([(40 * hour, 500)]))  # too late to stand in
        self.assertIsNone(views_at([(10 * hour, 50)]))  # not 24 hours old yet


class AudioParseTest(unittest.TestCase):
    def test_loudness_and_silence(self):
        from gta_clips.clip_stats import parse_audio
        stdout = ("frame:0 pts:0 pts_time:0\nlavfi.r128.M=-40.0\n"
                  "frame:1 pts:4800 pts_time:12.5\nlavfi.r128.M=-12.25\n")
        stderr = ("[silencedetect @ 1] silence_start: 0\n[silencedetect @ 1] silence_end: 1.5 | silence_duration: 1.5\n"
                  "[silencedetect @ 1] silence_start: 50\n"
                  "Summary:\n  Integrated loudness:\n    I:         -20.4 LUFS\n  Loudness range:\n"
                  "    LRA:         6.1 LU\n  True peak:\n    Peak:       -0.5 dBFS\n")
        stats = parse_audio(stdout, stderr, 60)
        self.assertEqual((stats["loudness_lufs"], stats["loudness_range_lu"], stats["true_peak_dbfs"]), (-20.4, 6.1, -0.5))
        self.assertEqual((stats["max_momentary_lufs"], stats["loudest_at_s"]), (-12.25, 12.5))
        self.assertEqual(stats["first_sound_s"], 1.5)
        self.assertAlmostEqual(stats["silence_fraction"], (1.5 + 10) / 60, places=3)


if __name__ == "__main__":
    unittest.main()


class InstagramQueueTest(ExportTest):
    """Ready picks count as Instagram approval for accounts that post there by API."""

    def _config(self, limit=3):
        schedule = dict(self.config.schedule)
        schedule["instagram"] = replace(schedule["instagram"], daily_limit=limit)
        return replace(self.config, schedule=schedule,
                       runtime=replace(self.config.runtime, ready_per_day=10))

    def _uploads(self):
        with connect(self.config.paths.database) as db:
            return [tuple(row) for row in db.execute(
                "SELECT u.clip_id, c.state FROM uploads u JOIN clips c ON c.id = u.clip_id WHERE u.platform='instagram'")]

    def test_one_waiting_clip_per_api_account(self):
        for name in ("a", "b"):
            self._clip(name, account="irl-en")
        self._clip("gta", account="gta6-1")
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO oauth(platform, token) VALUES ('instagram@irl-en', '{}')")
        export_due(self._config(), self.now)
        uploads = self._uploads()
        # Only irl-en has an API login, and only one of its clips waits at a time.
        self.assertEqual(len(uploads), 1)
        self.assertIn(uploads[0][0], ("a", "b"))
        self.assertEqual(uploads[0][1], "approved")

    def test_nothing_queued_when_instagram_is_off(self):
        self._clip("a", account="irl-en")
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO oauth(platform, token) VALUES ('instagram@irl-en', '{}')")
        export_due(self._config(limit=0), self.now)
        self.assertEqual(self._uploads(), [])
