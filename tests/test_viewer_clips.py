"""Viewer clips: Twitch's most-viewed clips become windows render-live cuts, tested against chat bursts."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest

from gta_clips import cuts, recipe, viewer_clips
from gta_clips.config import load_config
from gta_clips.db import connect

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = """
[[accounts]]
name = "gta6-4"
category = "Grand Theft Auto VI"
language = "en"

[[accounts]]
name = "kai"
streamers = ["KaiCenat"]
"""
NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc).timestamp()
STARTED = "2026-11-24T10:00:00Z"
STARTED_TS = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc).timestamp()


def _clip(clip_id, views, offset=600, language="en", video="v1", broadcaster="b1", game="g1"):
    return {"id": clip_id, "view_count": views, "vod_offset": offset, "duration": 28.0, "language": language,
            "video_id": video, "broadcaster_id": broadcaster, "game_id": game}


class FakeTwitch:
    def __init__(self, clips):
        self.clips = clips
        self.calls = []

    def game_id(self, name):
        return "g1"

    def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint == "clips":
            return {"data": list(self.clips)}
        if endpoint == "videos":
            return {"data": [{"id": video, "stream_id": "555", "created_at": STARTED, "user_login": "Streamer",
                              "viewable": "public"} for video in params["id"]]}
        if endpoint == "users":
            return {"data": [{"id": "b1", "login": "kaicenat"}]}
        if endpoint == "games":
            return {"data": [{"id": "g1", "name": "Just Chatting"}]}
        raise AssertionError(endpoint)


class ViewerClipTest(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        folder = Path(self.folder.name)
        text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        text = re.sub(r'data_dir = "data"', f'data_dir = "{folder.as_posix()}/data"', text)
        (folder / "config.toml").write_text(text + ACCOUNTS, encoding="utf-8")
        config = load_config(folder / "config.toml")
        self.config = replace(config, live=replace(config.live, test_viewer_clips=True))
        self.feed = next(feed for feed in self.config.feeds if feed.key == "Grand Theft Auto VI|en")

    def tearDown(self):
        self.folder.cleanup()

    def _windows(self):
        with connect(self.config.paths.database) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM live_bursts WHERE trigger_kind='viewer_clip' ORDER BY anchor")]

    def test_off_by_default_collects_nothing(self):
        twitch = FakeTwitch([_clip("a", 5000)])
        off = replace(self.config, live=replace(self.config.live, test_viewer_clips=False))
        self.assertEqual(viewer_clips.collect(off, twitch, NOW), 0)
        self.assertEqual(twitch.calls, [])

    def test_most_viewed_clips_become_windows_ending_where_the_clip_ends(self):
        twitch = FakeTwitch([_clip("low", 50, offset=100), _clip("es", 9000, offset=200, language="es"),
                             _clip("novod", 9000, offset=None), _clip("top", 5000, offset=900),
                             _clip("second", 800, offset=300)])
        saved = viewer_clips._collect_feed(self.config, twitch, self.feed, {}, NOW)
        self.assertEqual(saved, 2)
        windows = self._windows()
        self.assertEqual([w["anchor"] for w in windows], [int(STARTED_TS + 300 + 28), int(STARTED_TS + 900 + 28)])
        top = windows[1]
        self.assertEqual((top["stream_id"], top["messages"], top["account"], top["streamer"], top["state"]),
                         ("555", 0, "gta6-4", "streamer", "waiting"))
        self.assertIn("5000 views", top["detail"])
        # Asked for the category's clips of the last day only.
        clips_params = next(params for endpoint, params in twitch.calls if endpoint == "clips")
        self.assertEqual((clips_params["game_id"], clips_params["started_at"]), ("g1", "2026-11-23T18:00:00Z"))

    def test_the_same_moment_is_not_saved_twice(self):
        twitch = FakeTwitch([_clip("top", 5000, offset=900)])
        viewer_clips._collect_feed(self.config, twitch, self.feed, {}, NOW)
        twitch.clips = [_clip("again", 4000, offset=930)]
        self.assertEqual(viewer_clips._collect_feed(self.config, twitch, self.feed, {}, NOW), 0)
        self.assertEqual(len(self._windows()), 1)

    def test_at_most_three_a_day_per_account_set(self):
        twitch = FakeTwitch([_clip(str(n), 1000 + n, offset=300 + 200 * n) for n in range(6)])
        self.assertEqual(viewer_clips._collect_feed(self.config, twitch, self.feed, {}, NOW), 3)
        self.assertEqual(viewer_clips._collect_feed(self.config, twitch, self.feed, {}, NOW), 0)
        # The three most viewed.
        self.assertEqual(sorted(int(w["detail"].split()[3][1:]) for w in self._windows()), [1003, 1004, 1005])

    def test_streamer_feeds_ask_for_their_channels_clips(self):
        feed = next(feed for feed in self.config.feeds if feed.streamers)
        twitch = FakeTwitch([_clip("kai", 3000, language="fr")])
        self.assertEqual(viewer_clips._collect_feed(self.config, twitch, feed, {}, NOW), 1)
        self.assertIn(("clips", {"started_at": "2026-11-23T18:00:00Z", "ended_at": "2026-11-24T18:00:00Z",
                                 "first": 100, "broadcaster_id": "b1"}), twitch.calls)
        window = self._windows()[0]
        self.assertEqual((window["account"], window["game_name"]), ("kai", "Just Chatting"))

    def test_a_clip_from_the_first_seconds_of_a_stream_is_skipped(self):
        twitch = FakeTwitch([_clip("early", 5000, offset=0)])
        self.assertEqual(viewer_clips._collect_feed(self.config, twitch, self.feed, {}, NOW), 0)

    def test_a_new_clip_of_an_old_moment_is_skipped(self):
        # Clipped today from a broadcast more than a day ago: it wouldn't count toward the daily cap.
        twitch = FakeTwitch([_clip("old", 5000, offset=900)])
        later = NOW + 2 * 24 * 3600
        self.assertEqual(viewer_clips._collect_feed(self.config, twitch, self.feed, {}, later), 0)


class ViewerClipRecipeTest(unittest.TestCase):
    def setUp(self):
        config = load_config(ROOT / "config.example.toml")
        self.on = replace(config, live=replace(config.live, test_viewer_clips=True, test_cuts=True))
        self.off = replace(config, live=replace(config.live, test_viewer_clips=False, test_cuts=True))

    def test_viewer_clips_get_their_own_cut(self):
        self.assertEqual(cuts.choose("555", 1000, "viewer_clip", True), cuts.VIEWER)
        self.assertEqual(cuts.choose("555", 1000, "viewer_clip", False), cuts.VIEWER)

    def test_viewer_clips_are_posted_only_while_the_test_runs(self):
        clip = {"kind": "viewer_clip", "cut_before": 30, "cut_after": 5}
        self.assertNotIn("live.test_viewer_clips", recipe.misfits(self.on, clip))
        self.assertNotIn("live.test_cuts", recipe.misfits(self.on, clip))
        self.assertIn("live.test_viewer_clips", recipe.misfits(self.off, clip))
        self.assertIn("live.test_cuts", recipe.misfits(self.on, {**clip, "cut_after": 30}))

    def test_arms_compare_viewer_clips_with_chat_bursts(self):
        arm = recipe.ARMS["live.test_viewer_clips"]
        self.assertEqual(arm(self.on, {"kind": "viewer_clip"}), "viewer clip")
        self.assertEqual(arm(self.on, {"kind": "burst"}), "chat burst")
        self.assertIsNone(arm(self.on, {"kind": "control"}))
        self.assertIsNone(arm(self.off, {"kind": "burst"}))
        # Viewer clips aren't in the cut test.
        self.assertIsNone(recipe.ARMS["live.test_cuts"](self.on, {"kind": "viewer_clip"}))

    def test_ranking_ignores_the_source(self):
        self.assertEqual(recipe.blind({"kind=viewer_clip": 1, "x": 2}), {"kind=burst": 1, "x": 2})


if __name__ == "__main__":
    unittest.main()
