"""Regression coverage for VOD deduplication and the publish approval boundary."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from gta_clips.config import HighlightsConfig, PathsConfig, ScheduleConfig, load_config
from gta_clips.db import connect
from gta_clips.discovery import PopularClip, Vod
from gta_clips.highlights import rank_moments
from gta_clips.metadata import attribution
from gta_clips.pipeline import ingest
from gta_clips.review import decide, edit
from gta_clips.scheduler import publish_due
from gta_clips.upload.common import Result, UploadError


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        settings = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(
            settings, paths=PathsConfig(path),
            twitch=replace(settings.twitch, streamers=("streamer",)),
            schedule={**settings.schedule, "youtube": ScheduleConfig(daily_limit=1, min_spacing_minutes=0)},
        )

    def seed_clip(self, clip_id: str):
        path = f"{clip_id}.mp4"
        (self.config.paths.data_dir / path).write_bytes(b"mp4 fixture")
        with connect(self.config.paths.database) as db:
            db.execute("INSERT OR IGNORE INTO vods(id, streamer, status) VALUES ('12', 'streamer', 'done')")
            db.execute(
                """INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript,
                                     title, description, hashtags)
                   VALUES (?, '12', 'streamer', ?, 0, 15, '', 'A highlight', ?, '#Gaming')""",
                (clip_id, path, attribution("streamer")),
            )

    def test_approval_and_daily_limit(self):
        self.seed_clip("12-1")
        self.seed_clip("12-2")
        self.seed_clip("12-3")
        decide(self.config, "12-3", [])
        edit(self.config, "12-1", "Updated title", "An edited description", None)
        with self.assertRaises(ValueError):
            decide(self.config, "12-1", ["youtube"], "private")
        with patch("gta_clips.scheduler.access_token", return_value=("test-token", {})), \
                patch.dict("gta_clips.scheduler.SENDERS", {
                    "youtube": lambda upload: Result("posted", "remote-id", upload.clip["title"]),
                }):
            self.assertEqual(publish_due(self.config), 0)
            for clip_id in ("12-1", "12-2"):
                decide(self.config, clip_id, ["youtube"], "private", False)
            self.assertEqual(publish_due(self.config), 0)
            self.assertEqual(publish_due(self.config), 0)
        with connect(self.config.paths.database) as db:
            clips = db.execute("SELECT description FROM clips WHERE id='12-1'").fetchone()
            states = db.execute("SELECT * FROM uploads ORDER BY clip_id").fetchall()
        self.assertIn(attribution("streamer"), clips["description"])
        self.assertEqual([row["state"] for row in states], ["posted", "queued"])
        self.assertEqual(states[0]["detail"], "Updated title")
        self.assertEqual(states[0]["remote_id"], "remote-id")

    def test_retry_uses_checkpoint_and_backoff(self):
        self.seed_clip("12-1")
        decide(self.config, "12-1", ["youtube"], "private", False)

        def transient(upload):
            upload.save(session="existing-session")
            raise UploadError("HTTP 429", retry=True, delay=900)

        def recovered(upload):
            self.assertEqual(upload.checkpoint["session"], "existing-session")
            return Result("posted", "remote-id", "complete")

        with patch("gta_clips.scheduler.access_token", return_value=("test-token", {})):
            with patch.dict("gta_clips.scheduler.SENDERS", {"youtube": transient}):
                self.assertEqual(publish_due(self.config), 1)
                self.assertEqual(publish_due(self.config), 0)
            with connect(self.config.paths.database) as db:
                db.execute("UPDATE uploads SET retry_at=0")
            with patch.dict("gta_clips.scheduler.SENDERS", {"youtube": recovered}):
                self.assertEqual(publish_due(self.config), 0)

    def test_completed_vod_is_skipped(self):
        vod = Vod("12", "streamer", "42", 60, datetime.now(timezone.utc), None)
        with patch("gta_clips.pipeline.Twitch") as factory, patch("gta_clips.pipeline._process") as process:
            twitch = factory.return_value
            twitch.discover.side_effect = lambda _: iter((vod,))
            twitch.is_live.return_value = False

            def complete(*_):
                with connect(self.config.paths.database) as db:
                    db.execute("UPDATE vods SET status='done' WHERE id='12'")

            process.side_effect = complete
            self.assertEqual(ingest(self.config), 0)
            self.assertEqual(ingest(self.config), 0)
            process.assert_called_once()

    def test_highlights_clamp_to_vod_and_do_not_overlap(self):
        settings = HighlightsConfig(clips_per_vod=3, duration_seconds=15, lead_in_seconds=3)
        moments = rank_moments(50, [48.1] * 10, {0: -10, 20: -30, 44: -25},
                               [PopularClip(20, 3, 100)], settings)
        self.assertEqual(sorted(clip.start for clip in moments), [0, 17, 35])
        self.assertTrue(all(clip.duration == 15 for clip in moments))
        self.assertEqual(rank_moments(14, [2.0], {}, [], settings), [])


if __name__ == "__main__":
    unittest.main()
