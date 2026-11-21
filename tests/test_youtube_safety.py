"""Swearing kept off screen and out of titles, and YouTube restrictions recorded per post."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips.captions import Word, write_ass
from gta_clips.config import PathsConfig, load_config
from gta_clips.context_text import ContextHook
from gta_clips.db import connect
from gta_clips.metadata import clean_title, generate, mask_explicit
from gta_clips.post_stats import restrictions
from gta_clips.ready import retitle_explicit
from gta_clips.view_model import restriction_results

SWEARY = "You're fucking corner sports, man, right? Then we drove all the way across the city tonight."


class MaskTest(unittest.TestCase):
    def test_keeps_first_letter_and_length(self):
        self.assertEqual(mask_explicit("what the fuck, Shit happens"), "what the f***, S*** happens")
        self.assertEqual(mask_explicit("a classic dickens novel"), "a classic dickens novel")

    def test_captions_and_hook_are_masked_but_credit_is_not(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "captions.ass"
            words = [Word(0.0, 0.5, "holy"), Word(0.5, 1.0, "shit")]
            hook = ContextHook(text="this is fucking wild", method="debate_quote_v1", label="WHO'S RIGHT?")
            write_ass(path, words, "shitposter", 5.0, hook)
            text = path.read_text(encoding="utf-8")
        self.assertIn("s***", text)
        self.assertIn("f******", text)
        self.assertNotIn("fucking", text)
        self.assertIn("@shitposter on Twitch", text)


class CleanTitleTest(unittest.TestCase):
    def test_clean_title_is_left_alone(self):
        made = generate("We drove all the way across the city tonight to the airport.", "GTA", "s", "v", 0)
        self.assertIsNone(clean_title(made.title, made.description, "", "GTA"))

    def test_explicit_headline_is_replaced_and_credit_kept(self):
        old = generate("", "Just Chatting", "kaicenat", "v", 10)
        title = "You're fucking corner sports, man, right? | @kaicenat"
        description = f"You're fucking corner sports, man, right?\n\n{old.description}"
        new_title, new_description = clean_title(title, description, SWEARY, "Just Chatting")
        self.assertEqual(new_title, "Then we drove all the way across the city tonight. | @kaicenat")
        self.assertTrue(new_description.startswith("Then we drove"))
        self.assertTrue(new_description.endswith(old.description))
        self.assertNotIn("fucking", new_description)

    def test_falls_back_to_game_when_every_sentence_swears(self):
        new_title, new_description = clean_title("Shit. | @s", "@s on Twitch\nhttps://www.twitch.tv/s",
                                                 "Shit shit shit.", "Fortnite")
        self.assertEqual(new_title, "Fortnite highlight | @s")
        self.assertEqual(new_description, "@s on Twitch\nhttps://www.twitch.tv/s")


class RetitleTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = replace(load_config(ROOT / "config.example.toml"), paths=PathsConfig(Path(folder.name)))
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")
            for clip_id in ("queued", "on-youtube", "held", "clean"):
                title = "Clean title here | @s" if clip_id == "clean" else "You're fucking corner sports | @s"
                db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                              description, hashtags) VALUES (?, 'v', 's', 'x.mp4', 0, 30, ?, ?, 'd', '#t')""",
                           (clip_id, SWEARY, title))
            db.execute("INSERT INTO posts(platform, video_id, clip_id, account, url, source) "
                       "VALUES ('youtube', 'abc', 'on-youtube', 'gta6-1', '', 'browser')")
        ledger = self.config.paths.data_dir / "browser-upload" / "posts.sqlite3"
        ledger.parent.mkdir(parents=True)
        source = sqlite3.connect(ledger)
        source.execute("CREATE TABLE posts(platform, account, clip_id, digest, state, started, finished, url, detail)")
        source.execute("INSERT INTO posts VALUES ('instagram', 'a', 'held', 'd', 'held', 0, 0, '', '')")
        source.commit()
        source.close()

    def test_only_unposted_explicit_titles_change(self):
        self.assertEqual(retitle_explicit(self.config), 1)
        with connect(self.config.paths.database) as db:
            titles = dict(db.execute("SELECT id, title FROM clips"))
        self.assertEqual(titles["queued"], "Then we drove all the way across the city tonight. | @s")
        self.assertIn("fucking", titles["on-youtube"])
        self.assertIn("fucking", titles["held"])
        self.assertEqual(titles["clean"], "Clean title here | @s")
        self.assertEqual(retitle_explicit(self.config), 0)


class RestrictionTest(unittest.TestCase):
    def test_reads_age_rating_and_blocked_countries(self):
        self.assertEqual(restrictions({}), {"age_restricted": 0, "blocked_countries": 0})
        self.assertEqual(restrictions({"contentRating": {"ytRating": "ytAgeRestricted"},
                                       "regionRestriction": {"blocked": ["DE", "RU"]}}),
                         {"age_restricted": 1, "blocked_countries": 2})
        self.assertEqual(restrictions({"regionRestriction": {"allowed": ["US"]}})["blocked_countries"], 248)

    def test_report_counts_latest_read_per_streamer(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "db.sqlite3"
            with connect(database) as db:
                db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")
                db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                              description, hashtags) VALUES ('c', 'v', 'tfue', 'x', 0, 30, '', 't', 'd', '')""")
                db.execute("INSERT INTO posts(platform, video_id, clip_id, source) VALUES ('youtube', 'a', 'c', 'x')")
                db.execute("INSERT INTO posts(platform, video_id, source) VALUES ('youtube', 'b', 'x')")
                for video, fetched, age, blocked in (("a", 1, 0, 0), ("a", 2, 1, 2), ("b", 1, None, None)):
                    db.execute("""INSERT INTO post_stats(platform, video_id, fetched, age_restricted, blocked_countries)
                                  VALUES ('youtube', ?, ?, ?, ?)""", (video, fetched, age, blocked))
                report = restriction_results(db)
        self.assertEqual(report, {"tfue": {"posts": 1, "age_restricted": 1, "region_blocked": 1}})


if __name__ == "__main__":
    unittest.main()
