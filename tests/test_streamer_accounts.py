"""Streamer accounts: an account set fed by named Twitch channels instead of a category."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock
import re
import unittest

from gta_clips.config import ConfigError, load_config
from gta_clips.db import connect
from gta_clips.live import Broadcast, _active_streams, choose_feeds, record_burst

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = """
[[accounts]]
name = "gta6-4"
category = "Grand Theft Auto VI"
language = "en"
channels = 5

[[accounts]]
name = "kai"
label = "Kai Cenat clips"
streamers = ["KaiCenat"]

[[accounts]]
name = "duo"
streamers = ["darkviperau", "jynxzi"]
"""


def _write(folder: Path, extra: str) -> Path:
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    text = re.sub(r'data_dir = "data"', f'data_dir = "{folder.as_posix()}/data"', text)
    path = folder / "config.toml"
    path.write_text(text + extra, encoding="utf-8")
    return path


def _row(stream_id, login, viewers, game="Grand Theft Auto VI", kind="live"):
    return {"id": stream_id, "user_id": f"id-{login}", "user_login": login, "game_id": "1", "type": kind,
            "started_at": "2026-11-22T10:00:00Z", "viewer_count": viewers, "language": "en",
            "game_name": game, "title": "t", "tags": []}


class StreamerAccountTest(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.config = load_config(_write(Path(self.folder.name), ACCOUNTS))

    def tearDown(self):
        self.folder.cleanup()

    def test_each_streamer_account_gets_its_own_feed_ahead_of_categories(self):
        feeds = self.config.feeds
        self.assertEqual([feed.key for feed in feeds],
                         ["streamers|kai", "streamers|duo", "Grand Theft Auto VI|en"])
        self.assertEqual(feeds[0].streamers, ("kaicenat",))
        self.assertEqual(feeds[1].channels, 2)
        kai = self.config.account("kai")
        self.assertEqual((kai.categories, kai.watches), ((), "streamer kaicenat"))
        self.assertEqual(self.config.total_channels, 5 + 1 + 2)

    def test_streamers_are_watched_live_in_any_category_and_any_size(self):
        twitch = MagicMock()
        twitch.get.return_value = {"data": [_row("2", "jynxzi", 30, "Rainbow Six Siege"),
                                            _row("1", "darkviperau", 900),
                                            _row("3", "jynxzi", 1, kind="")]}
        feed = self.config.feeds[1]
        streams = _active_streams(twitch, self.config, "", 10, feed)
        self.assertEqual(twitch.get.call_args[0][1]["user_login"], ["darkviperau", "jynxzi"])
        self.assertEqual([(s.login, s.rank, s.feed, s.game_name) for s in streams],
                         [("darkviperau", 1, "streamers|duo", "Grand Theft Auto VI"),
                          ("jynxzi", 2, "streamers|duo", "Rainbow Six Siege")])
        twitch.pages.assert_not_called()

    def test_a_streamer_found_in_a_category_too_goes_only_to_their_account(self):
        kai = Broadcast("s1", "id-kaicenat", "kaicenat", 0, 50000, 1, "en", "Grand Theft Auto VI", "streamers|kai")
        same_in_category = replace(kai, feed="Grand Theft Auto VI|en")
        other = Broadcast("s2", "id-other", "other", 0, 900, 2, "en", "Grand Theft Auto VI", "Grand Theft Auto VI|en")
        chosen = choose_feeds(self.config, {"streamers|kai": [kai], "streamers|duo": [],
                                            "Grand Theft Auto VI|en": [same_in_category, other]},
                              {}, {}, set(), 0)
        self.assertEqual(chosen["id-kaicenat"].feed, "streamers|kai")
        self.assertEqual(chosen["id-other"].feed, "Grand Theft Auto VI|en")

    def test_bursts_from_a_streamer_feed_go_to_its_account(self):
        kai = Broadcast("s1", "id-kaicenat", "kaicenat", 0, 50000, 1, "en", "Just Chatting", "streamers|kai")
        self.assertTrue(record_burst(self.config, kai, 1000, 20))
        with connect(self.config.paths.database) as db:
            self.assertEqual(db.execute("SELECT account, game_name FROM live_bursts").fetchone()[:],
                             ("kai", "Just Chatting"))

    def test_bad_streamer_accounts_are_rejected(self):
        bad = {
            '\n[[accounts]]\nname = "x"\nstreamers = ["kaicenat"]\n': "already has its own account",
            '\n[[accounts]]\nname = "x"\nstreamers = ["bad login!"]\n': "Twitch logins",
            '\n[[accounts]]\nname = "x"\nstreamers = ["a"]\ncategory = "Minecraft"\n': "takes no category",
        }
        for block, message in bad.items():
            with self.subTest(message), TemporaryDirectory() as folder, self.assertRaisesRegex(ConfigError, message):
                load_config(_write(Path(folder), ACCOUNTS + block))


if __name__ == "__main__":
    unittest.main()
