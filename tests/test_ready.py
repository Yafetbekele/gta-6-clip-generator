from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips.config import AccountConfig, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.ready import FOLDER, allowed_by, export_due


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
                              accounts=(AccountConfig("gta6-1", "Grand Theft Auto VI", label="GTA 6"),
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
            anchor = len(clip_id) * 1000 + sum(map(ord, clip_id))
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

    def test_ranks_rating_then_burst_then_spike_and_skips_the_rest(self):
        self._clip("small", messages=10)
        self._clip("big", messages=40)
        self._clip("loved", messages=5, score=5)
        self._clip("control", messages=99, kind="control")
        self._clip("disliked", messages=99, score=2)
        self._clip("rejected", messages=99, state="rejected")
        self._clip("old", messages=99, age=4 * 86400)
        self._clip("missing", messages=99, write=False)
        added = export_due(replace(self.config, runtime=replace(self.config.runtime, ready_per_day=4)), self.now)
        self.assertEqual(self._exported(), ["loved", "big", "small", "control"])
        folder = self.config.paths.data_dir / FOLDER / "2026-11-19" / "gta6-1"
        self.assertEqual({path.parent for path in added}, {folder})
        note = added[0].with_suffix(".txt").read_text(encoding="utf-8")
        self.assertIn("Channel: GTA 6 (gta6-1)", note)
        self.assertIn("Title loved", note)
        self.assertIn("#gta6", note)
        self.assertTrue((self.config.paths.data_dir / "clips/gta6-1/loved.mp4").is_file())  # copied, not moved

    def test_daily_share_is_kept_and_clips_are_offered_once(self):
        for index in range(5):
            self._clip(f"c{index}", messages=10 + index)
        self._clip("other", account="irl-en")
        config = replace(self.config, runtime=replace(self.config.runtime, ready_per_day=2))
        early = datetime(2026, 11, 19, 1, 0).timestamp()
        self.assertEqual(len(export_due(config, early)), 2)  # one per account set so far today
        self.assertEqual(len(export_due(config, early)), 0)
        self.assertEqual(len(export_due(config, self.now)), 1)  # gta6-1 gets its second; irl-en has no more
        self.assertEqual(sorted(self._exported()), ["c3", "c4", "other"])


if __name__ == "__main__":
    unittest.main()
