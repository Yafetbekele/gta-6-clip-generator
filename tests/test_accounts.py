"""Account sets: config, channel quotas per feed, even assignment, folders, logins and publishing."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
import json
import re
import unittest

from gta_clips.accounts import sort_existing, summary
from gta_clips.config import AccountConfig, ConfigError, PathsConfig, load_config
from gta_clips.context_text import debate_hook
from gta_clips.db import connect
from gta_clips.live import Broadcast, _active_streams, choose_feeds, record_burst
from gta_clips.oauth import access_token, token_key
from gta_clips.scheduler import publish_due

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = """
[[accounts]]
name = "gta6-1"
category = "Grand Theft Auto VI"
language = "en"
channels = 5

[[accounts]]
name = "gta6-2"
category = "Grand Theft Auto VI"
language = "en"
channels = 5

[[accounts]]
name = "irl-es"
label = "IRL Spanish"
category = "Just Chatting"
language = "es"
channels = 3
"""


def _write(folder: Path, extra: str) -> Path:
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    text = re.sub(r'data_dir = "data"', f'data_dir = "{folder.as_posix()}/data"', text)
    path = folder / "config.toml"
    path.write_text(text + extra, encoding="utf-8")
    return path


def _stream(stream_id: str, broadcaster: str, rank: int, feed: str, game: str = "Grand Theft Auto VI"):
    return Broadcast(stream_id, broadcaster, f"user{broadcaster}", 0, 100 - rank, rank, "en", game, feed)


class AccountConfigTest(unittest.TestCase):
    def test_accounts_sharing_a_category_share_one_feed(self):
        with TemporaryDirectory() as folder:
            config = load_config(_write(Path(folder), ACCOUNTS))
        feeds = {feed.key: feed for feed in config.feeds}
        self.assertEqual(feeds["Grand Theft Auto VI|en"].channels, 10)
        self.assertEqual(feeds["Grand Theft Auto VI|en"].accounts, ("gta6-1", "gta6-2"))
        self.assertEqual(feeds["Just Chatting|es"].accounts, ("irl-es",))
        self.assertEqual(config.total_channels, 13)
        self.assertEqual(config.account("irl-es").title, "IRL Spanish")

    def test_without_accounts_the_single_category_still_works(self):
        config = load_config(ROOT / "config.example.toml")
        self.assertEqual(len(config.feeds), 1)
        self.assertEqual(config.feeds[0].category, config.twitch.game_name)
        self.assertEqual(config.feeds[0].accounts, ())

    def test_bad_accounts_are_rejected(self):
        bad = {
            'name = "Bad Name!"': "lowercase",
            'language = "english"': "two-letter",
            'channels = 50': "between 1 and 20",
        }
        for change, message in bad.items():
            key = change.split(" =")[0]
            block = re.sub(rf'(?m)^{key} = .*$', change, ACCOUNTS, count=1)
            with self.subTest(change), TemporaryDirectory() as folder, self.assertRaisesRegex(ConfigError, message):
                load_config(_write(Path(folder), block))
        with TemporaryDirectory() as folder, self.assertRaisesRegex(ConfigError, "Two accounts"):
            load_config(_write(Path(folder), ACCOUNTS + '\n[[accounts]]\nname = "gta6-1"\ncategory = "Minecraft"\n'))
        many = "".join(f'\n[[accounts]]\nname = "a{i}"\ncategory = "Minecraft"\nchannels = 20\n' for i in range(6))
        with TemporaryDirectory() as folder, self.assertRaisesRegex(ConfigError, "the most is 100"):
            load_config(_write(Path(folder), many))


class FeedTest(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.config = load_config(_write(Path(self.folder.name), ACCOUNTS))

    def tearDown(self):
        self.folder.cleanup()

    def test_each_feed_fills_its_own_quota(self):
        gta = [_stream(f"g{i}", f"{i}", i, "Grand Theft Auto VI|en") for i in range(1, 30)]
        irl = [_stream(f"j{i}", f"j{i}", i, "Just Chatting|es", "Just Chatting") for i in range(1, 10)]
        chosen = choose_feeds(self.config, {"Grand Theft Auto VI|en": gta, "Just Chatting|es": irl}, {}, {}, set(), 0)
        feeds = [stream.feed for stream in chosen.values()]
        self.assertEqual(feeds.count("Grand Theft Auto VI|en"), 10)
        self.assertEqual(feeds.count("Just Chatting|es"), 3)

    def test_language_is_asked_of_twitch_and_checked(self):
        feed = next(feed for feed in self.config.feeds if feed.language == "es")
        twitch = MagicMock()
        twitch.pages.return_value = iter([
            {"id": "1", "user_id": "a", "user_login": "a", "game_id": "509658", "type": "live",
             "started_at": "2026-11-19T10:00:00Z", "viewer_count": 9, "language": "es", "game_name": "Just Chatting"},
            {"id": "2", "user_id": "b", "user_login": "b", "game_id": "509658", "type": "live",
             "started_at": "2026-11-19T10:00:00Z", "viewer_count": 5, "language": "en", "game_name": "Just Chatting"},
        ])
        streams = _active_streams(twitch, self.config, "509658", 10, feed)
        self.assertEqual(twitch.pages.call_args[0][1]["language"], "es")
        self.assertEqual([stream.login for stream in streams], ["a"])
        self.assertEqual(streams[0].feed, "Just Chatting|es")
        self.assertEqual(streams[0].game_name, "Just Chatting")

    def test_windows_alternate_between_accounts_sharing_a_feed(self):
        streams = [replace(_stream(f"s{i}", f"{i}", 1, "Grand Theft Auto VI|en"), started_at=0) for i in range(4)]
        for stream in streams:
            self.assertTrue(record_burst(self.config, stream, 1000, 20))
        irl = _stream("j1", "j1", 1, "Just Chatting|es", "Just Chatting")
        self.assertTrue(record_burst(self.config, irl, 1000, 20))
        with connect(self.config.paths.database) as db:
            rows = dict(db.execute("SELECT stream_id, account FROM live_bursts").fetchall())
            games = dict(db.execute("SELECT stream_id, game_name FROM live_bursts").fetchall())
        self.assertEqual(sorted(rows[f"s{i}"] for i in range(4)), ["gta6-1", "gta6-1", "gta6-2", "gta6-2"])
        self.assertEqual(rows["j1"], "irl-es")
        self.assertEqual(games["j1"], "Just Chatting")


class LoginTest(unittest.TestCase):
    def test_each_account_set_has_its_own_login(self):
        self.assertEqual(token_key("youtube"), "youtube")
        self.assertEqual(token_key("youtube", "gta6-1"), "youtube@gta6-1")
        with TemporaryDirectory() as folder:
            database = Path(folder) / "state.sqlite3"
            with connect(database) as db:
                db.execute("INSERT INTO oauth(platform, token) VALUES ('youtube', ?)",
                           (json.dumps({"access_token": "old", "expires_at": 9e12}),))
                db.execute("INSERT INTO oauth(platform, token) VALUES ('youtube@gta6-1', ?)",
                           (json.dumps({"access_token": "new", "expires_at": 9e12}),))
            self.assertEqual(access_token(database, "youtube")[0], "old")
            self.assertEqual(access_token(database, "youtube", "gta6-1")[0], "new")
            with self.assertRaisesRegex(RuntimeError, "--account gta6-2"):
                access_token(database, "youtube", "gta6-2")


class SortAndPublishTest(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.config = load_config(_write(Path(self.folder.name), ACCOUNTS))
        self.base = self.config.paths.data_dir
        (self.base / "clips").mkdir(parents=True)
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'live-window')")
            for stream, game, language in (("s1", "Just Chatting", "es"), ("s2", "Just Chatting", "fr"),
                                           ("s3", "Just Chatting", "es")):
                clip = f"live-{stream}-1000"
                (self.base / "clips" / f"{clip}.mp4").write_bytes(b"video")
                (self.base / "clips" / f"{clip}.edit.json").write_text("{}", encoding="utf-8")
                db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript,
                              title, description, hashtags) VALUES (?, 'v', 's', ?, 0, 60, '', 't', 'd', '#')""",
                           (clip, f"clips/{clip}.mp4"))
                db.execute("""INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name,
                              messages, retry_at, state, clip_id) VALUES (?, 1000, 'b', 's', ?, 9, 0, 'queued', ?)""",
                           (stream, game, clip))
                db.execute("""INSERT INTO burst_features(stream_id, anchor, multiplier, minimum_messages,
                              warmup_seconds, language) VALUES (?, 1000, 4, 0, 300, ?)""", (stream, language))
                db.execute("INSERT INTO clip_ratings(clip_id, score, notes) VALUES (?, 4, 'kept')", (clip,))
            # Already queued for the original login, so it must stay put.
            db.execute("INSERT INTO uploads(clip_id, platform) VALUES ('live-s3-1000', 'youtube')")

    def tearDown(self):
        self.folder.cleanup()

    def test_sort_moves_matching_clips_and_keeps_ratings(self):
        result = sort_existing(self.config, wait_seconds=0)
        self.assertEqual(result["moved"], 1)
        with connect(self.config.paths.database) as db:
            clips = {row["id"]: dict(row) for row in db.execute("SELECT id, path, account FROM clips")}
            ratings = db.execute("SELECT COUNT(*) FROM clip_ratings WHERE notes='kept'").fetchone()[0]
        self.assertEqual(clips["live-s1-1000"]["account"], "irl-es")
        self.assertEqual(clips["live-s1-1000"]["path"], "clips/irl-es/live-s1-1000.mp4")
        self.assertTrue((self.base / "clips" / "irl-es" / "live-s1-1000.mp4").is_file())
        self.assertTrue((self.base / "clips" / "irl-es" / "live-s1-1000.edit.json").is_file())
        self.assertIsNone(clips["live-s2-1000"]["account"])  # French: no matching account set
        self.assertIsNone(clips["live-s3-1000"]["account"])  # Has an upload record
        self.assertEqual(clips["live-s3-1000"]["path"], "clips/live-s3-1000.mp4")
        self.assertEqual(ratings, 3)
        rows = {row["name"]: row for row in summary(self.config)}
        self.assertEqual(rows["irl-es"]["pending"], 1)
        self.assertEqual(rows[""]["pending"], 2)
        self.assertEqual(sort_existing(self.config, wait_seconds=0)["moved"], 0)

    def test_publishing_uses_each_account_sets_login(self):
        sort_existing(self.config, wait_seconds=0)
        config = replace(self.config, schedule={**self.config.schedule,
                                                "youtube": replace(self.config.schedule["youtube"], daily_limit=5)})
        with connect(config.paths.database) as db:
            db.execute("UPDATE clips SET state='approved'")
            db.execute("INSERT INTO uploads(clip_id, platform) VALUES ('live-s1-1000', 'youtube')")
        sent = []

        def fake_token(database, platform, account=None):
            return ("token-" + (account or "original"), {})

        def fake_send(upload):
            sent.append((upload.clip["id"], upload.token))
            from gta_clips.upload.common import Result
            return Result("posted", "remote", "ok")

        with patch("gta_clips.scheduler.access_token", side_effect=fake_token), \
                patch.dict("gta_clips.scheduler.SENDERS", {"youtube": fake_send}):
            publish_due(config)
        self.assertIn(("live-s1-1000", "token-irl-es"), sent)
        self.assertIn(("live-s3-1000", "token-original"), sent)


class LocalizedHookTest(unittest.TestCase):
    def test_spanish_and_portuguese_accounts_get_their_own_language(self):
        es = debate_hook("", "Just Chatting", "k", language="es")
        pt = debate_hook("", "Just Chatting", "k", language="pt")
        self.assertIn(es.label, ("SÉ SINCERO", "DEBATE", "OPINIÓN POLÉMICA"))
        self.assertIn(pt.label, ("SEJA SINCERO", "DEBATE", "OPINIÃO POLÊMICA"))
        quote = debate_hook("Esto es lo mejor que he visto en mi vida entera.", "Just Chatting", "k", language="es")
        self.assertEqual(quote.method, "debate_quote_v1")
        self.assertTrue(quote.label.startswith("¿") or quote.label.isupper())


if __name__ == "__main__":
    unittest.main()
