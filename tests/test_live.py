"""Regression coverage for live chat bursts and exact archive windows."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import time
import unittest

from gta_clips.config import LiveConfig, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.live import Broadcast, ChatRate, _chat_event, record_burst
from gta_clips.live_render import archive_window, render_due
from gta_clips.oauth import _verify_callback


class LiveTest(unittest.TestCase):
    def test_warmup_blocks_a_large_early_spike_for_five_minutes(self):
        rate = ChatRate()
        settings = LiveConfig()
        for timestamp in (1000, 1100, 1299):
            for _ in range(1000):
                self.assertEqual(rate.observe(timestamp, settings), 0)
        # A separate channel must establish its own baseline too.
        self.assertEqual(ChatRate().observe(1400, settings), 0)

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(config, paths=PathsConfig(Path(folder.name)))

    def test_relative_threshold_ignores_a_steady_busy_chat(self):
        rate = ChatRate()
        for second in range(310):
            for _ in range(3):
                count = rate.observe(1000 + second, LiveConfig())
                self.assertEqual(count, 0)
        self.assertEqual(count, 0)
        for _ in range(80):
            count = rate.observe(1310, LiveConfig())
        self.assertGreaterEqual(count, 90)

    def test_duplicate_events_and_shared_chat_mirrors_do_not_inflate_the_rate(self):
        config = replace(self.config, live=LiveConfig(messages_per_10_seconds=2, warmup_seconds=0))
        streams = {"42": Broadcast("12", "42", "streamer", 1000)}
        rates, seen = {}, {}
        message = {
            "metadata": {"subscription_type": "channel.chat.message", "message_id": "first",
                         "message_timestamp": "1970-01-01T00:18:20Z"},
            "payload": {"event": {"broadcaster_user_id": "42"}},
        }
        _chat_event(config, message, streams, rates, seen)
        _chat_event(config, message, streams, rates, seen)
        message["metadata"]["message_id"] = "mirror"
        message["payload"]["event"]["source_broadcaster_user_id"] = "not-42"
        _chat_event(config, message, streams, rates, seen)
        with connect(config.paths.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_bursts").fetchone()[0], 0)
        message["metadata"]["message_id"] = "second"
        message["payload"]["event"]["source_broadcaster_user_id"] = "42"
        _chat_event(config, message, streams, rates, seen)
        with connect(config.paths.database) as db:
            self.assertEqual(db.execute("SELECT messages FROM live_bursts").fetchone()[0], 2)

    def test_burst_persists_post_roll_and_cooldown(self):
        stream = Broadcast("12", "42", "streamer", 950)
        config = replace(self.config, live=replace(self.config.live, cooldown_seconds=120))
        self.assertFalse(record_burst(config, stream, 970, 15))
        self.assertTrue(record_burst(config, stream, 1000, 16, 4, 310))
        self.assertFalse(record_burst(config, stream, 1100, 20))
        self.assertTrue(record_burst(config, stream, 1120, 17))
        with connect(config.paths.database) as db:
            rows = db.execute("SELECT anchor, retry_at FROM live_bursts ORDER BY anchor").fetchall()
            feature = db.execute("SELECT baseline_per_10s, observed_seconds, warmup_seconds "
                                 "FROM burst_features WHERE anchor=1000").fetchone()
        self.assertEqual(tuple(feature), (4, 310, 300))
        self.assertEqual([tuple(row) for row in rows], [(1000, 1030), (1120, 1150)])

    def test_archive_window_requires_the_matching_stream_and_full_postroll(self):
        video = {"stream_id": "12", "id": "9", "duration": "1m15s",
                 "created_at": datetime.fromtimestamp(1000, timezone.utc).isoformat()}
        self.assertIsNone(archive_window([video], "wrong-stream", 1050))
        self.assertIsNone(archive_window([video], "12", 1050))
        video["duration"] = "1m20s"
        self.assertEqual(archive_window([video], "12", 1050), ("9", 20))
        self.assertIsNone(archive_window([video], "12", 1010))

    def test_segment_is_queued_for_review_and_resume_is_idempotent(self):
        anchor = int(time.time()) - 60
        start = anchor - 75
        stream = Broadcast("12", "42", "streamer", start)
        self.assertTrue(record_burst(self.config, stream, anchor, 22))
        with patch("gta_clips.live_render.Twitch") as twitch, \
                patch("gta_clips.live_render.download_vod") as download, \
                patch("gta_clips.live_render.render", return_value="The jump worked") as render:
            twitch.return_value.get.return_value = {"data": [
                {"stream_id": "12", "id": "9", "duration": "3m",
                 "created_at": datetime.fromtimestamp(start, timezone.utc).isoformat()},
            ]}
            render_due(self.config)
            render_due(self.config)
        # Three seconds of padding on each side; render() cuts the exact 60 s from offset 3.
        self.assertEqual(download.call_args.kwargs["section"], (42, 108))
        self.assertEqual(render.call_args.args[3].start, 3)
        self.assertEqual(render.call_args.args[3].duration, 60)
        render.assert_called_once()
        with connect(self.config.paths.database) as db:
            self.assertEqual(tuple(db.execute("SELECT state, start, duration FROM clips").fetchone()),
                             ("pending", 45, 60))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM live_bursts").fetchone()[0], "queued")

    def test_twitch_redirect_accepts_implicit_root(self):
        result = _verify_callback("http://localhost:3000/?state=expected&code=test",
                                  "http://localhost:3000", "expected")
        self.assertEqual(result["code"], ["test"])
        with self.assertRaises(ValueError):
            _verify_callback("http://localhost:3000/?state=wrong&code=test",
                             "http://localhost:3000", "expected")
