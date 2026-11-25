"""The hook test: a random kind of hook box per clip, recorded and reported with views."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import time
import unittest

from gta_clips import hook_styles
from gta_clips.config import ConfigError, PathsConfig, _editing, load_config
from gta_clips.context_text import (GAME_TAKES, GENERIC_TAKES, MAX_HOOK_LENGTH, QUOTE_LABELS, SEQUEL_TAKES,
                                    STYLES, TEASES, TEST_STYLES, debate_hook)
from gta_clips.db import connect
from gta_clips.live import Broadcast, record_burst
from gta_clips.live_render import render_due
from gta_clips.view_model import clip_features, hook_results

SPEECH = "I accidentally booked the flight for next year. That is the worst mistake I have made."


class HookStyleTest(unittest.TestCase):
    def test_choice_is_stable_covers_every_style_and_is_off_without_the_test(self):
        self.assertIsNone(hook_styles.choose("live-12-1000", False))
        self.assertEqual(hook_styles.choose("live-12-1000", True), hook_styles.choose("live-12-1000", True))
        chosen = {hook_styles.choose(f"live-12-{anchor}", True) for anchor in range(200)}
        self.assertEqual(chosen, set(TEST_STYLES))

    def test_plain_and_tease_hooks_have_no_debate_in_them(self):
        plain = debate_hook(SPEECH, "Grand Theft Auto VI", "k", style="plain")
        self.assertEqual((plain.method, plain.label), ("plain_quote_v1", ""))
        self.assertIn(plain.text, SPEECH)
        # Without a usable quote, "plain" falls back to a keep-watching line, never a debate.
        for style in ("plain", "tease"):
            for language in ("en", "es", "pt"):
                hook = debate_hook("uh", "Grand Theft Auto VI", "k", language=language, style=style)
                self.assertEqual((hook.method, hook.label), ("tease_v1", ""))
                self.assertIn(hook.text, TEASES[language])
        self.assertTrue(set(TEST_STYLES) <= set(STYLES))

    def test_each_style_makes_its_kind_of_hook(self):
        quote = debate_hook(SPEECH, "Grand Theft Auto VI", "k", style="quote")
        self.assertEqual(quote.method, "debate_quote_v1")
        self.assertEqual(debate_hook(SPEECH, "Grand Theft Auto VI", "k").method, "debate_quote_v1")
        game = debate_hook(SPEECH, "Grand Theft Auto VI", "k", style="game")
        self.assertEqual(game.method, "debate_topic_v1")
        self.assertNotIn(game.text, SPEECH)
        generic = debate_hook(SPEECH, "Grand Theft Auto VI", "k", style="generic")
        self.assertEqual(generic.method, "debate_generic_v1")
        self.assertIn((generic.label, generic.text), GENERIC_TAKES["en"])
        none = debate_hook(SPEECH, "Grand Theft Auto VI", "k", style="none")
        self.assertEqual((none.text, none.label, none.method), ("", "", "none"))
        with self.assertRaises(ValueError):
            debate_hook(SPEECH, "GTA", "k", style="other")

    def test_a_style_that_cannot_apply_falls_back_and_says_so(self):
        # No game to ask about, or game takes not written in the account's language.
        self.assertEqual(debate_hook(SPEECH, "Just Chatting", "k", style="game").method, "debate_generic_v1")
        self.assertEqual(debate_hook(SPEECH, "Grand Theft Auto VI", "k", style="game", language="es").method,
                         "debate_generic_v1")
        # No usable quote: the normal hook asks about the game instead.
        self.assertEqual(debate_hook("", "Grand Theft Auto VI", "k", style="quote").method, "debate_topic_v1")

    def test_every_template_fits_on_screen(self):
        for language, labels in QUOTE_LABELS.items():
            self.assertGreaterEqual(len(labels), 10)
            for label in labels:
                self.assertLessEqual(len(label), MAX_HOOK_LENGTH, label)
        for language, takes in GENERIC_TAKES.items():
            self.assertGreaterEqual(len(takes), 6)
            for label, text in takes:
                self.assertLessEqual(len(text), MAX_HOOK_LENGTH, text)
        self.assertGreaterEqual(len(GAME_TAKES + SEQUEL_TAKES), 10)
        for label, text in GAME_TAKES + SEQUEL_TAKES:
            self.assertLessEqual(len(text.format(game="GTA 6", previous="GTA 5")), MAX_HOOK_LENGTH, text)
        # Every game question can be reached for GTA 6.
        shown = {debate_hook("", "Grand Theft Auto VI", str(key), style="game").text for key in range(300)}
        self.assertEqual(len(shown), len(GAME_TAKES + SEQUEL_TAKES))

    def test_setting_is_read_from_the_editing_section(self):
        self.assertFalse(_editing({}).test_hooks)
        self.assertTrue(_editing({"test_hooks": True}).test_hooks)
        with self.assertRaises(ConfigError):
            _editing({"test_hooks": "yes"})


class HookRecordingTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(config, paths=PathsConfig(Path(folder.name)))

    def _render_live(self, config):
        anchor = int(time.time()) - 60
        start = anchor - 75
        self.assertTrue(record_burst(config, Broadcast("12", "42", "streamer", start), anchor, 22))

        def rendered(*args, **kwargs):
            args[7]["hook_method"] = "debate_generic_v1"
            return "The jump worked"
        with patch("gta_clips.live_render.Twitch") as twitch, \
                patch("gta_clips.live_render.download_vod"), \
                patch("gta_clips.live_render.render", side_effect=rendered) as render:
            twitch.return_value.get.return_value = {"data": [
                {"stream_id": "12", "id": "9", "duration": "3m",
                 "created_at": datetime.fromtimestamp(start, timezone.utc).isoformat()},
            ]}
            render_due(config)
        with connect(config.paths.database) as db:
            row = db.execute("SELECT id, hook_style, hook_method FROM clips").fetchone()
        return render.call_args.kwargs["hook_style"], tuple(row)

    def test_a_test_style_is_rendered_and_recorded(self):
        config = replace(self.config, editing=replace(self.config.editing, test_hooks=True))
        given, (clip_id, style, method) = self._render_live(config)
        self.assertEqual(style, hook_styles.choose(clip_id, True))
        self.assertEqual(given, style)
        self.assertEqual(method, "debate_generic_v1")

    def test_without_the_test_the_normal_hook_is_used_and_no_style_recorded(self):
        given, (_, style, method) = self._render_live(self.config)
        self.assertEqual(given, "quote")
        self.assertIsNone(style)
        self.assertEqual(method, "debate_generic_v1")

    def test_style_is_a_model_feature_and_reported_with_views(self):
        with connect(self.config.paths.database) as db:
            db.execute("INSERT INTO vods(id, streamer, status) VALUES ('v', 's', 'done')")
            for index, (style, method, views) in enumerate(
                    [("none", "none", 100), ("none", "none", 300), ("game", "debate_topic_v1", 50), (None, None, 9)]):
                clip = f"c{index}"
                db.execute("""INSERT INTO clips(id, vod_id, streamer, path, start, duration, transcript, title,
                                  description, hashtags, hook_style, hook_method)
                              VALUES (?, 'v', 's', 'p', 0, 60, '', '', '', '', ?, ?)""", (clip, style, method))
                db.execute("INSERT INTO posts(platform, video_id, clip_id, source) VALUES ('youtube', ?, ?, 'api')",
                           (clip, clip))
                db.execute("""INSERT INTO post_stats(platform, video_id, fetched, published, views)
                              VALUES ('youtube', ?, ?, 1000, ?)""", (clip, 1000 + 24 * 3600, views))
            features = clip_features(db, ["c0", "c2", "c3"])
            report = hook_results(db)
        self.assertEqual(features["c0"].get("hook=none"), 1.0)
        self.assertEqual(features["c2"].get("hook=game"), 1.0)
        self.assertFalse(any(name.startswith("hook=") for name in features["c3"]))
        self.assertEqual(report["youtube"]["none"], {"posts": 2, "median_views": 200})
        self.assertEqual(report["youtube"]["game, shown debate_topic_v1"], {"posts": 1, "median_views": 50})
        self.assertNotIn("quote", report["youtube"])


if __name__ == "__main__":
    unittest.main()
