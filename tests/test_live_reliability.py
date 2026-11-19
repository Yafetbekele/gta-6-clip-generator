"""Coverage gaps, unique chatters, channel churn, subscription errors and render retries."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from threading import Barrier
import subprocess
import time
import unittest

from gta_clips.config import ConfigError, LiveConfig, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.diagnostics import CommandFailed
from gta_clips.download import VodUnavailable
from gta_clips.live import (Broadcast, ChatRate, required_unique, _chat_event, _sync_subscriptions, choose_channels,
                            record_burst)
from gta_clips.live_render import muted_overlap, render_due, retry_delay

ROOT = Path(__file__).resolve().parents[1]


def _config(folder: str, **live):
    config = load_config(ROOT / "config.example.toml")
    return replace(config, paths=PathsConfig(Path(folder)), live=replace(LiveConfig(), **live))


def _message(message_id: str, timestamp: float, chatter: str | None, login: str = "viewer"):
    stamp = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    event = {"broadcaster_user_id": "42", "chatter_user_login": login}
    if chatter is not None:
        event["chatter_user_id"] = chatter
    return {"metadata": {"subscription_type": "channel.chat.message", "message_id": message_id,
                         "message_timestamp": stamp},
            "payload": {"event": event}}


class ChatRateTest(unittest.TestCase):
    def test_warmup_counts_covered_time_not_wall_time(self):
        rate = ChatRate()
        settings = LiveConfig(warmup_seconds=300)
        rate.start_coverage(1000)
        rate.observe(1000, settings, "a")
        rate.observe(1100, settings, "a")
        rate.pause(1100)
        rate.start_coverage(1400)  # 300 s unwatched
        rate.observe(1401, settings, "a")
        self.assertFalse(rate.warm(settings))
        self.assertAlmostEqual(rate.covered_seconds, 101)
        rate.observe(1600, settings, "a")
        self.assertTrue(rate.warm(settings))

    def test_baseline_ignores_unwatched_time(self):
        rate = ChatRate()
        settings = LiveConfig(warmup_seconds=0)
        rate.start_coverage(1000)
        for second in range(1000, 1150):
            rate.observe(second, settings, "a")
        rate.pause(1150)
        rate.start_coverage(1300)
        rate.observe(1320, settings, "a")
        # 150 messages over 150 covered seconds is 10 per 10 s, not diluted by the 150 s gap.
        self.assertAlmostEqual(rate.baseline, 150 * 10 / 160, delta=1.5)

    def test_one_spammer_cannot_trigger_a_burst(self):
        settings = LiveConfig(warmup_seconds=0, messages_per_10_seconds=15, min_unique_chatters=5)
        spam = ChatRate()
        counts = [spam.observe(1000 + index / 10, settings, "spammer") for index in range(40)]
        self.assertEqual(max(counts), 0)
        crowd = ChatRate()
        counts = [crowd.observe(1000 + index / 10, settings, f"user{index % 8}") for index in range(40)]
        self.assertGreater(max(counts), 0)

    def test_without_a_floor_a_burst_is_relative_to_the_baseline(self):
        settings = LiveConfig(warmup_seconds=0, messages_per_10_seconds=0, spike_multiplier=4.0,
                              min_unique_chatters=6)
        self.assertEqual(required_unique(settings), 6)
        rate = ChatRate()
        # A quiet chat: one message every 5 seconds for 5 minutes is 2 per 10 s.
        for index in range(60):
            self.assertEqual(rate.observe(1000 + index * 5, settings, f"regular{index % 3}"), 0)
        # 10 messages from 8 people in a few seconds is 5x normal; 15 messages used to be required.
        counts = [rate.observe(1300 + index * 0.3, settings, f"viewer{index % 8}") for index in range(10)]
        self.assertGreater(max(counts), 0)
        self.assertLess(max(counts), 15)

    def test_without_a_floor_few_chatters_still_cannot_trigger(self):
        settings = LiveConfig(warmup_seconds=0, messages_per_10_seconds=0, spike_multiplier=4.0,
                              min_unique_chatters=6)
        rate = ChatRate()
        for index in range(60):
            rate.observe(1000 + index * 5, settings, f"regular{index % 3}")
        counts = [rate.observe(1300 + index * 0.3, settings, f"viewer{index % 3}") for index in range(20)]
        self.assertEqual(max(counts), 0)

    def test_late_message_lands_at_its_own_time(self):
        rate = ChatRate()
        settings = LiveConfig(warmup_seconds=0)
        rate.observe(1005, settings, "a")
        rate.observe(1001, settings, "b")
        self.assertEqual([timestamp for timestamp, _ in rate.recent], [1001, 1005])
        rate.observe(1012, settings, "c")
        self.assertEqual([timestamp for timestamp, _ in rate.recent], [1005, 1012])

    def test_two_argument_observe_still_works(self):
        rate = ChatRate()
        settings = LiveConfig(warmup_seconds=0, messages_per_10_seconds=2)
        rate.observe(1000, settings)
        self.assertEqual(rate.observe(1000, settings), 2)


class ChatEventTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def test_bots_are_ignored(self):
        config = _config(self.folder, warmup_seconds=0, messages_per_10_seconds=2, min_unique_chatters=1)
        streams = {"42": Broadcast("12", "42", "streamer", 0)}
        rates, seen = {}, {}
        for index in range(5):
            _chat_event(config, _message(f"m{index}", 1000, f"bot{index}", "nightbot"), streams, rates, seen)
        self.assertNotIn("12", rates)

    def test_burst_records_unique_chatters_and_peak(self):
        config = _config(self.folder, warmup_seconds=0, messages_per_10_seconds=5, min_unique_chatters=3,
                         control_interval_seconds=0, near_miss_interval_seconds=0)
        streams = {"42": Broadcast("12", "42", "streamer", 0)}
        rates, seen = {}, {}
        for index in range(5):
            _chat_event(config, _message(f"a{index}", 1000, f"user{index}"), streams, rates, seen)
        for index in range(10):
            _chat_event(config, _message(f"b{index}", 1010, f"user{index}"), streams, rates, seen)
        _chat_event(config, _message("late", 1031, "user1"), streams, rates, seen)
        with connect(config.paths.database) as db:
            row = db.execute("""SELECT unique_chatters, peak_messages, peak_offset_s, trigger_kind
                                FROM burst_features JOIN live_bursts USING(stream_id, anchor)""").fetchone()
        # The window is (t-10, t], so the five messages at 1000 have left it by 1010.
        self.assertEqual(tuple(row), (5, 10, 10, "burst"))

    def test_control_does_not_block_a_later_burst(self):
        config = _config(self.folder)
        stream = Broadcast("12", "42", "streamer", 0)
        self.assertTrue(record_burst(config, stream, 1000, 3, kind="control"))
        self.assertTrue(record_burst(config, stream, 1050, 30))
        self.assertFalse(record_burst(config, stream, 1100, 3, kind="near_miss"))
        self.assertTrue(record_burst(config, stream, 1200, 3, kind="near_miss"))


class ChannelSelectionTest(unittest.TestCase):
    def _streams(self, order):
        return [Broadcast(f"s{login}", login, login, 0, 100 - rank, rank + 1) for rank, login in enumerate(order)]

    def test_watched_channel_survives_small_rank_changes(self):
        candidates = self._streams(["a", "b", "c", "d"])
        chosen = choose_channels(candidates, {}, {}, set(), 2, 0)
        self.assertEqual(set(chosen), {"a", "b"})
        swapped = self._streams(["a", "c", "b", "d"])
        chosen = choose_channels(swapped, {"a": 0, "b": 0}, {}, set(), 2, 1000)
        self.assertEqual(set(chosen), {"a", "b"})

    def test_denied_and_unclippable_channels_are_backfilled(self):
        candidates = self._streams(["a", "b", "c", "d"])
        chosen = choose_channels(candidates, {}, {"a": 500}, {"sb"}, 2, 0)
        self.assertEqual(set(chosen), {"c", "d"})


class SubscriptionTest(unittest.TestCase):
    def test_one_rejected_channel_does_not_stop_the_others(self):
        desired = {login: Broadcast(f"s{login}", login, login, 0) for login in ("a", "b", "c")}
        responses = {
            "a": MagicMock(status_code=400, json=lambda: {"message": "chat restricted"}),
            "b": MagicMock(status_code=409, json=lambda: {"message": "exists"}),
            "c": MagicMock(status_code=202, json=lambda: {"data": [{"id": "sub-c"}]}),
        }

        def post(url, **kwargs):
            return responses[kwargs["json"]["condition"]["broadcaster_user_id"]]

        subscriptions, denied = {}, {}
        with patch("gta_clips.live.requests.post", side_effect=post):
            added = _sync_subscriptions("session", "token", "client", "reader", desired,
                                        subscriptions, denied, __import__("collections").deque())
        self.assertEqual(added, {"c"})
        self.assertEqual(subscriptions, {"c": "sub-c"})
        self.assertEqual(set(denied), {"a", "b"})
        self.assertGreater(denied["a"] - denied["b"], 400)

    def test_rate_limit_stops_the_batch(self):
        desired = {login: Broadcast(f"s{login}", login, login, 0) for login in ("a", "b")}
        with patch("gta_clips.live.requests.post", return_value=MagicMock(status_code=429)) as post:
            added = _sync_subscriptions("session", "token", "client", "reader", desired,
                                        {}, {}, __import__("collections").deque())
        self.assertEqual(added, set())
        self.assertEqual(post.call_count, 1)


class RenderRetryTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = _config(folder.name)
        self.anchor = int(time.time()) - 120
        self.start = self.anchor - 75
        record_burst(self.config, Broadcast("12", "42", "streamer", self.start), self.anchor, 22)
        self.video = {"stream_id": "12", "id": "9", "duration": "3m", "viewable": "public",
                      "created_at": datetime.fromtimestamp(self.start, timezone.utc).isoformat(),
                      "muted_segments": [{"offset": 40, "duration": 10}]}

    def _run(self, **download):
        with patch("gta_clips.live_render.Twitch") as twitch, \
                patch("gta_clips.live_render.download_vod", **download), \
                patch("gta_clips.live_render.render", return_value="words"):
            twitch.return_value.get.return_value = {"data": [self.video]}
            render_due(self.config)
        with connect(self.config.paths.database) as db:
            return db.execute("SELECT * FROM live_bursts").fetchone()

    def test_backoff_grows_and_is_capped(self):
        self.assertEqual([retry_delay(n) for n in (1, 2, 3)], [120, 240, 480])
        self.assertEqual(retry_delay(20), 3600)

    def test_failure_is_recorded_without_secrets(self):
        error = CommandFailed(1, ["yt-dlp", "https://x.test/v?token=SECRET"],
                              stderr="ERROR: fetch https://usher.test/a.m3u8?sig=SECRET failed")
        row = self._run(side_effect=error)
        self.assertEqual((row["state"], row["attempts"]), ("waiting", 1))
        self.assertNotIn("SECRET", row["last_error"])
        self.assertGreater(row["retry_at"], time.time() + 60)

    def test_subscriber_only_vod_stops_retrying(self):
        row = self._run(side_effect=VodUnavailable("This video is only available to subscribers"))
        self.assertEqual(row["state"], "unavailable")
        with connect(self.config.paths.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unclippable_streams").fetchone()[0], 1)

    def test_private_archive_is_not_downloaded(self):
        self.video["viewable"] = "private"
        # A download attempt would raise AssertionError, which render_due does not catch.
        row = self._run(side_effect=AssertionError("downloaded a private archive"))
        self.assertEqual(row["state"], "unavailable")

    def test_muted_overlap(self):
        self.assertEqual(muted_overlap([self.video], "9", 30, 60), 10)
        self.assertEqual(muted_overlap([self.video], "9", 45, 60), 5)

    def test_unexpected_subprocess_error_is_retried(self):
        row = self._run(side_effect=subprocess.TimeoutExpired("yt-dlp", 300))
        self.assertEqual((row["state"], row["attempts"]), ("waiting", 1))



class ParallelRenderTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.anchor = int(time.time()) - 120
        self.start = self.anchor - 75
        self.folder = folder.name

    def _render_two(self, workers: int):
        config = _config(self.folder, render_workers=workers)
        videos = {}
        for stream, broadcaster, login in (("12", "42", "first"), ("13", "43", "second")):
            record_burst(config, Broadcast(stream, broadcaster, login, self.start), self.anchor, 22)
            videos[broadcaster] = {"stream_id": stream, "id": stream + "9", "duration": "3m", "viewable": "public",
                                   "created_at": datetime.fromtimestamp(self.start, timezone.utc).isoformat()}
        both_started = Barrier(2, timeout=5)
        transcribers = []

        def render(*args, **kwargs):
            transcribers.append(args[6])
            if workers > 1:
                both_started.wait()  # Times out unless the two clips render at the same time.
            return "words"

        with patch("gta_clips.live_render.Twitch") as twitch, \
                patch("gta_clips.live_render.download_vod"), \
                patch("gta_clips.live_render.render", side_effect=render):
            twitch.return_value.get.side_effect = lambda _, params: {"data": [videos[params["user_id"]]]}
            render_due(config)
        with connect(config.paths.database) as db:
            states = [row[0] for row in db.execute("SELECT state FROM live_bursts")]
        return states, transcribers

    def test_two_clips_render_at_once_with_their_own_transcriber(self):
        states, transcribers = self._render_two(2)
        self.assertEqual(states, ["queued", "queued"])
        self.assertEqual(len({id(transcriber) for transcriber in transcribers}), 2)

    def test_one_worker_renders_in_turn_and_reuses_the_transcriber(self):
        states, transcribers = self._render_two(1)
        self.assertEqual(states, ["queued", "queued"])
        self.assertIs(transcribers[0], transcribers[1])

    def test_render_workers_is_validated(self):
        example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        self.assertIn("render_workers = 2\n", example)
        too_many = Path(self.folder) / "config.toml"
        too_many.write_text(example.replace("render_workers = 2\n", "render_workers = 9\n"), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "render_workers"):
            load_config(too_many)


if __name__ == "__main__":
    unittest.main()
