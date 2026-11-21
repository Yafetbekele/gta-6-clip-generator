from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips.config import ConfigError, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.storage import plan_trim, trim_clips

NOW = 2_000_000_000


class TrimTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = load_config(ROOT / "config.example.toml")
        self.config = replace(base, paths=PathsConfig(Path(folder.name)),
                              runtime=replace(base.runtime, max_clips_per_account=2))
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")

    def _clip(self, clip_id, age_hours, account="gta6-1", state="pending", measured=True, path=None):
        relative = path or f"clips/{account}/{clip_id}.mp4"
        video = self.config.paths.data_dir / relative
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"x" * 10)
        video.with_suffix(".edit.json").write_text("{}")
        with connect(self.config.paths.database) as db:
            db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                          description, hashtags, state, created, account)
                          VALUES (?, 'v', 's', ?, 0, 30, '', 't', 'd', '', ?, ?, ?)""",
                       (clip_id, relative, state, NOW - age_hours * 3600, account))
            if measured:
                db.execute("INSERT INTO clip_stats(clip_id, measured) VALUES (?, ?)", (clip_id, NOW))
        return video

    def test_keeps_newest_per_account_and_removes_the_rest(self):
        newest = self._clip("a", 1)
        second = self._clip("b", 2)
        oldest = self._clip("c", 3)
        other = self._clip("d", 4, account="league")
        self.assertEqual(trim_clips(self.config, NOW), (1, 10))
        self.assertTrue(newest.exists() and second.exists() and other.exists())
        self.assertFalse(oldest.exists())
        self.assertFalse(oldest.with_suffix(".edit.json").exists())
        with connect(self.config.paths.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM clips").fetchone()[0], 4)

    def test_removes_videos_too_old_to_be_offered(self):
        stale = self._clip("a", 80)
        self.assertEqual([p for p, _ in plan_trim(self.config, NOW)], [stale.resolve()])

    def test_keeps_approved_and_unmeasured_clips(self):
        self._clip("a", 1)
        self._clip("b", 2)
        approved = self._clip("c", 3, state="approved")
        unmeasured = self._clip("d", 4, measured=False)
        self.assertEqual(plan_trim(self.config, NOW), [])
        self.assertTrue(approved.exists() and unmeasured.exists())

    def test_shared_file_stays_while_any_clip_still_needs_it(self):
        self._clip("a", 1)
        shared = self._clip("b", 2, path="clips/gta6-1/shared.mp4")
        with connect(self.config.paths.database) as db:
            db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                          description, hashtags, state, created, account)
                          VALUES ('z', 'v', 's', 'clips/gta6-1/shared.mp4', 0, 30, '', 't', 'd', '', 'pending', ?, 'gta6-1')""",
                       (NOW - 99 * 3600,))
            db.execute("INSERT INTO clip_stats(clip_id, measured) VALUES ('z', ?)", (NOW,))
        self.assertEqual(plan_trim(self.config, NOW), [])
        self.assertTrue(shared.exists())

    def test_never_touches_files_outside_the_clips_folder(self):
        outside = self._clip("a", 90, path="ready-to-post/2026-11-01/gta6-1/a.mp4")
        self.assertEqual(plan_trim(self.config, NOW), [])
        self.assertTrue(outside.exists())

    def test_zero_cap_keeps_everything(self):
        self._clip("a", 90)
        self.config = replace(self.config, runtime=replace(self.config.runtime, max_clips_per_account=0))
        self.assertEqual(trim_clips(self.config, NOW), (0, 0))

    def test_settings_are_validated(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text("[runtime]\nmax_clips_per_account = 40\nready_keep_days = 3\n")
            runtime = load_config(path).runtime
            self.assertEqual((runtime.max_clips_per_account, runtime.ready_keep_days), (40, 3))
            path.write_text("[runtime]\nready_keep_days = 0\n")
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
