"""Migrations, redaction, publishing order, VOD retries, hashtags, chat lag and rating analysis."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import csv
import sqlite3
import unittest

from gta_clips.config import ConfigError, HighlightsConfig, PathsConfig, ScheduleConfig, load_config
from gta_clips.db import SCHEMA, SCHEMA_VERSION, connect
from gta_clips.diagnostics import CommandFailed, describe, redact
from gta_clips.discovery import Vod
from gta_clips.download import VodUnavailable
from gta_clips.highlights import rank_moments
from gta_clips.metadata import generate
from gta_clips.pipeline import MAX_ATTEMPTS, ingest
from gta_clips.review import decide, export_ratings, rate, ratings_summary, spearman
from gta_clips.scheduler import publish_due
from gta_clips.upload.common import Result

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        settings = load_config(ROOT / "config.example.toml")
        self.config = replace(
            settings, paths=PathsConfig(Path(folder.name)),
            twitch=replace(settings.twitch, streamers=("streamer",)),
            schedule={**settings.schedule, "youtube": ScheduleConfig(daily_limit=5, min_spacing_minutes=0)},
        )

    def seed_clip(self, clip_id: str, streamer: str = "streamer"):
        path = f"{clip_id}.mp4"
        (self.config.paths.data_dir / path).write_bytes(b"mp4 fixture")
        with connect(self.config.paths.database) as db:
            db.execute("INSERT OR IGNORE INTO vods(id, streamer, status) VALUES ('12', ?, 'done')", (streamer,))
            db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript,
                                            title, description, hashtags)
                          VALUES (?, '12', ?, ?, 0, 15, '', 'A highlight', '@streamer on Twitch', '#Gaming')""",
                       (clip_id, streamer, path))


class MigrationTest(Base):
    def test_old_database_keeps_ratings_and_is_backed_up(self):
        database = self.config.paths.database
        database.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(database)
        old.executescript(SCHEMA)
        old.execute("INSERT INTO vods(id, streamer, status) VALUES ('1', 's', 'done')")
        old.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                       description, hashtags) VALUES ('c', '1', 's', 'c.mp4', 0, 60, '', 't', 'd', '')""")
        old.execute("INSERT INTO clip_ratings(clip_id, score, notes) VALUES ('c', 3, 'kept')")
        old.commit()
        old.close()
        with connect(database) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(tuple(db.execute("SELECT score, notes FROM clip_ratings").fetchone()), (3, "kept"))
            self.assertEqual(tuple(db.execute("SELECT score, notes FROM rating_events").fetchone()), (3, "kept"))
        backups = list((database.parent / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        copy = sqlite3.connect(backups[0])
        try:
            self.assertEqual(copy.execute("SELECT score FROM clip_ratings").fetchone()[0], 3)
        finally:
            copy.close()
        # Opening again neither migrates nor backs up a second time.
        with connect(database):
            pass
        self.assertEqual(len(list((database.parent / "backups").glob("*.sqlite3"))), 1)


class DiagnosticsTest(unittest.TestCase):
    def test_redaction_removes_signed_urls_and_tokens(self):
        text = redact("GET https://usher.ttvnw.net/v.m3u8?sig=abc&token=xyz failed; access_token=secret1")
        for secret in ("abc", "xyz", "secret1"):
            self.assertNotIn(secret, text)
        self.assertIn("usher.ttvnw.net", text)

    def test_command_failure_summary_names_the_program(self):
        error = CommandFailed(1, ["python", "-m", "yt_dlp", "https://x"], stderr="ERROR: boom")
        self.assertIn("yt_dlp exited with 1", describe(error))
        self.assertIn("boom", describe(error))


class SchedulerTest(Base):
    def test_backed_off_upload_does_not_block_the_next_one(self):
        self.seed_clip("12-1")
        self.seed_clip("12-2")
        for clip_id in ("12-1", "12-2"):
            decide(self.config, clip_id, ["youtube"], "private", False)
        with connect(self.config.paths.database) as db:
            db.execute("UPDATE uploads SET retry_at=unixepoch() + 3600 WHERE clip_id='12-1'")
        with patch("gta_clips.scheduler.access_token", return_value=("token", {})), \
                patch.dict("gta_clips.scheduler.SENDERS", {
                    "youtube": lambda upload: Result("posted", "remote", "ok")}):
            self.assertEqual(publish_due(self.config), 0)
        with connect(self.config.paths.database) as db:
            states = dict(db.execute("SELECT clip_id, state FROM uploads").fetchall())
        self.assertEqual(states, {"12-1": "queued", "12-2": "posted"})


class IngestTest(Base):
    def _ingest(self, error):
        vod = Vod("77", "streamer", "42", 600, datetime.now(timezone.utc), None)
        with patch("gta_clips.pipeline.Twitch") as factory, \
                patch("gta_clips.pipeline.Transcriber"), \
                patch("gta_clips.pipeline._process", side_effect=error):
            factory.return_value.discover.return_value = [vod]
            factory.return_value.is_live.return_value = False
            ingest(self.config)
        with connect(self.config.paths.database) as db:
            return db.execute("SELECT status, attempts, detail FROM vods WHERE id='77'").fetchone()

    def test_subscriber_only_vod_is_not_retried(self):
        row = self._ingest(VodUnavailable("subscriber-only"))
        self.assertEqual(row["status"], "unavailable")

    def test_repeated_failures_stop_after_the_cap(self):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            row = self._ingest(RuntimeError("disk full"))
            self.assertEqual(row["attempts"], attempt)
            with connect(self.config.paths.database) as db:
                db.execute("UPDATE vods SET updated=0")  # Skip the 15-minute wait.
        self.assertEqual(row["status"], "abandoned")
        self.assertIn("disk full", row["detail"])
        self.assertEqual(self._ingest(RuntimeError("never called"))["attempts"], MAX_ATTEMPTS)


class HighlightSettingsTest(Base):
    def test_chat_lag_moves_the_peak_earlier(self):
        settings = HighlightsConfig(clips_per_vod=1, duration_seconds=15, lead_in_seconds=0,
                                    audio_weight=0, twitch_clips_weight=0)
        chat = [40.0] * 20
        plain = rank_moments(100, chat, {}, [], settings)[0]
        lagged = rank_moments(100, chat, {}, [], replace(settings, chat_reaction_lag_seconds=6))[0]
        self.assertEqual(plain.start - lagged.start, 6)
        self.assertGreater(lagged.chat_score, 0)

    def test_motion_requirement_can_be_turned_off(self):
        settings = HighlightsConfig(clips_per_vod=1, duration_seconds=15, lead_in_seconds=0)
        audio = {second: -40 for second in range(60)} | {30: -5}
        self.assertEqual(rank_moments(60, [], audio, [], settings, active_seconds=set()), [])
        moments = rank_moments(60, [], audio, [], replace(settings, require_motion=False), active_seconds=set())
        self.assertEqual(len(moments), 1)
        self.assertIsNone(moments[0].motion_seconds)

    def test_config_validates_new_settings(self):
        path = self.config.paths.data_dir / "bad.toml"
        path.write_text("[highlights]\nrequire_motion = \"no\"\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)
        path.write_text("[live]\nnear_miss_multiplier = 3.0\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)


class MetadataTest(unittest.TestCase):
    def test_just_chatting_has_no_gaming_or_driving_tags(self):
        tags = generate("we had to race to the car", "Just Chatting", "s", "1", 0).hashtags.split()
        self.assertEqual(tags, ["#JustChatting", "#TwitchClips"])
        tags = generate("we had to race to the car", "Grand Theft Auto VI", "s", "1", 0).hashtags.split()
        self.assertEqual(tags, ["#GTA6", "#TwitchClips", "#Gaming", "#Cars", "#Racing"])


class RatingAnalysisTest(Base):
    def test_spearman_handles_ties_and_small_samples(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [1, 2, 3, 4]), 1)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1)
        self.assertIsNone(spearman([1, 2], [1, 2]))
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))

    def test_summary_groups_by_source_and_export_writes_csv(self):
        for index, (score, chat) in enumerate([(1, 0.1), (2, 0.3), (4, 0.6), (5, 0.9)]):
            clip_id = f"12-{index}"
            self.seed_clip(clip_id)
            with connect(self.config.paths.database) as db:
                db.execute("INSERT INTO clip_features(clip_id, source, chat_score) VALUES (?, 'vod', ?)",
                           (clip_id, chat))
            rate(self.config, clip_id, score, "", 30.0)
        summary = ratings_summary(self.config)
        self.assertEqual(summary["rated"], 4)
        self.assertEqual(summary["by_source"]["vod"], {"n": 4, "mean": 3.0})
        self.assertAlmostEqual(summary["correlations"]["chat_score"]["spearman"], 1.0)
        destination = self.config.paths.data_dir / "ratings.csv"
        export_ratings(self.config, destination)
        with destination.open(encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["source"] for row in rows}, {"vod"})
        self.assertIn("rated_at", rows[0])
        self.assertEqual(rows[0]["watched_seconds"], "30.0")


if __name__ == "__main__":
    unittest.main()
