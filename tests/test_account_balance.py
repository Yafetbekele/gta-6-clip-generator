"""Account sets sharing a category get windows evenly over the last day, not over all time."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips.config import AccountConfig, PathsConfig, load_config
from gta_clips.db import connect
from gta_clips.live import BALANCE_SECONDS, pick_account

NOW = 1_800_000_000


class BalanceTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = load_config(ROOT / "config.example.toml")
        self.config = replace(base, paths=PathsConfig(Path(folder.name)), accounts=tuple(
            AccountConfig(name, "Grand Theft Auto VI", "en") for name in ("old1", "old2", "new1")))
        self.feed = self.config.feeds[0].key
        self.anchor = 0

    def _windows(self, account, count, at, state="clipped"):
        with connect(self.config.paths.database) as db:
            for _ in range(count):
                self.anchor += 1
                db.execute("""INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name,
                              messages, state, retry_at, account) VALUES (?, ?, 'b', 's', 'g', 10, ?, 0, ?)""",
                           (str(self.anchor), at - self.anchor % 60, state, account))

    def _pick(self, at=NOW):
        with connect(self.config.paths.database) as db:
            return pick_account(db, self.config, self.feed, at)

    def test_a_set_added_later_does_not_take_every_window(self):
        # The old sets had hundreds of windows days ago; the new one has fewer today.
        self._windows("old1", 300, NOW - 3 * 86400)
        self._windows("old2", 300, NOW - 3 * 86400)
        self._windows("new1", 5, NOW - 3600)
        self.assertEqual(self._pick(), "old1")
        self._windows("old1", 6, NOW - 600)
        self.assertEqual(self._pick(), "old2")

    def test_windows_older_than_a_day_and_skipped_ones_do_not_count(self):
        self._windows("old1", 3, NOW - BALANCE_SECONDS - 100)
        self._windows("old2", 3, NOW - 100, state="skipped")
        self._windows("new1", 1, NOW - 100)
        self.assertEqual(self._pick(), "old1")

    def test_sorting_old_windows_balances_as_of_each_window(self):
        # accounts.py sorts past windows with their own anchor as the moment.
        self._windows("old1", 2, NOW - 5 * 86400)
        self.assertEqual(self._pick(NOW - 5 * 86400 + 30), "old2")
        self.assertEqual(self._pick(), "old1")


if __name__ == "__main__":
    unittest.main()
