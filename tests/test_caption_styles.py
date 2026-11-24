"""The caption style test: per-clip choice, how each style is drawn, and the report."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import caption_styles
from gta_clips.captions import Word, write_ass
from gta_clips.config import ConfigError, load_config
from gta_clips.db import connect
from gta_clips.view_model import TARGET_AGE, caption_results

WORDS = [Word(0.0 + i * 0.3, 0.25 + i * 0.3, w) for i, w in enumerate("one two three four five six".split())]


def _captions(style: str) -> list[str]:
    with TemporaryDirectory() as folder:
        path = Path(folder) / "c.ass"
        write_ass(path, WORDS, "s", 5, style=style)
        return path.read_text(encoding="utf-8").splitlines()


class ChoiceTest(unittest.TestCase):
    def test_off_gives_none_and_the_test_spreads_over_every_style(self):
        self.assertIsNone(caption_styles.choose("a", False))
        picks = [caption_styles.choose(f"clip-{n}", True) for n in range(300)]
        self.assertEqual(set(picks), set(caption_styles.STYLES))
        self.assertEqual(caption_styles.choose("clip-3", True), caption_styles.choose("clip-3", True))

    def test_the_draw_is_independent_of_the_trim_test(self):
        from gta_clips import trim
        pairs = {(trim.choose(f"c{n}", "test"), caption_styles.choose(f"c{n}", True)) for n in range(300)}
        self.assertEqual(len(pairs), 2 * len(caption_styles.STYLES))


class DrawTest(unittest.TestCase):
    def test_standard_is_unchanged(self):
        lines = _captions("standard")
        self.assertIn("Style: Caption,Arial,64,&H00FFFFFF,&H00FFFFFF,&H00131313,&H80000000,-1,0,0,0,100,100,"
                      "0,0,1,5,2,2,85,120,340,1", lines)
        captions = [line for line in lines if ",Caption," in line]
        self.assertTrue(captions[0].endswith(r"{\c&H004DDFFF&}one{\r} two three four five"))

    def test_big_shows_three_words_at_most_in_large_type(self):
        lines = _captions("big")
        self.assertTrue(any(line.startswith("Style: Caption,Arial,92,") for line in lines))
        captions = [line for line in lines if ",Caption," in line]
        self.assertTrue(captions[0].endswith(r"{\c&H004DDFFF&}one{\r} two three"))
        self.assertTrue(captions[3].endswith(r"{\c&H004DDFFF&}four{\r} five six"))

    def test_box_draws_a_box_with_no_highlight(self):
        lines = _captions("box")
        style = next(line for line in lines if line.startswith("Style: Caption,"))
        self.assertEqual(style.split(",")[15], "3")
        captions = [line for line in lines if ",Caption," in line]
        self.assertTrue(captions[0].endswith(",,one two three four five"))

    def test_every_style_starts_with_the_first_word(self):
        for style in caption_styles.STYLES:
            first = next(line for line in _captions(style) if ",Caption," in line)
            self.assertIn("0:00:00.00", first, style)


class ConfigTest(unittest.TestCase):
    def test_test_captions_must_be_true_or_false(self):
        text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "c.toml"
            path.write_text(text.replace("[editing]", "[editing]\ntest_captions = true", 1), encoding="utf-8")
            self.assertTrue(load_config(path).editing.test_captions)
            path.write_text(text.replace("[editing]", '[editing]\ntest_captions = "yes"', 1), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class ReportTest(unittest.TestCase):
    def test_views_and_percent_watched_per_style(self):
        with TemporaryDirectory() as folder, connect(Path(folder) / "s.sqlite3") as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")
            for n, (style, views, watched) in enumerate([("big", 300, 70.0), ("big", 100, 50.0),
                                                         ("box", 40, None), (None, 999, 99.0)]):
                db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                              description, hashtags, caption_style) VALUES (?, 'v', 's', 'x', 0, 60, '', 't', 'd', '', ?)""",
                           (f"c{n}", style))
                db.execute("INSERT INTO posts(platform, video_id, clip_id, source) VALUES ('youtube', ?, ?, 'api')",
                           (f"p{n}", f"c{n}"))
                db.execute("""INSERT INTO post_stats(platform, video_id, fetched, published, views, avg_view_percent)
                              VALUES ('youtube', ?, ?, 0, ?, ?)""", (f"p{n}", TARGET_AGE, views, watched))
            found = caption_results(db)["youtube"]
        self.assertEqual(found, {
            "big": {"posts": 2, "posts_with_24h_views": 2, "median_views": 200, "avg_percent_watched": 60.0},
            "box": {"posts": 1, "posts_with_24h_views": 1, "median_views": 40, "avg_percent_watched": None}})


if __name__ == "__main__":
    unittest.main()
