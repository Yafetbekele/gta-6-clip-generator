"""Only clips made with the running tests' current settings are offered, and every test arm gets posted."""

from collections import Counter
from dataclasses import fields, replace
from pathlib import Path
import random
import sys
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gta_clips import cuts, recipe
from gta_clips.config import EditingConfig, LiveConfig, load_config
from gta_clips.ready import _order, fitting
from gta_clips.recipe import EXPERIMENTS, blind, misfits


def _config(**settings):
    config = load_config(ROOT / "config.example.toml")
    editing = {key: value for key, value in settings.items() if key in {f.name for f in fields(EditingConfig)}}
    live = {key: value for key, value in settings.items() if key in {f.name for f in fields(LiveConfig)}}
    return replace(config, editing=replace(config.editing, **editing), live=replace(config.live, **live))


ALL_ON = dict(test_hooks=True, filler="on", trim_dead_time="test", test_captions=True, test_cuts=True)


def _clip(**values):
    clip = dict(id="c", kind="burst", hook_style="tease", filler="subway_surfers", trim="none",
                caption_style="standard", cut_before=20, cut_after=5)
    clip.update(values)
    return clip


class EveryTestIsCheckedTest(unittest.TestCase):
    def test_every_test_switch_in_the_config_has_a_rule(self):
        # A test is a switch named test_* or an "off"/"test"/"on" mode. Adding one without a rule in
        # recipe.EXPERIMENTS would let clips made without it be posted again; this fails instead.
        switches = {f"{section}.{f.name}" for section, kind in (("editing", EditingConfig), ("live", LiveConfig))
                    for f in fields(kind) if f.name.startswith("test_") or f.default == "off"}
        self.assertTrue(switches)
        self.assertEqual(switches - set(EXPERIMENTS), set())
        self.assertEqual(set(recipe.ARMS), set(EXPERIMENTS))

    def test_the_ready_step_reads_every_column_the_rules_need(self):
        from gta_clips.ready import CANDIDATES
        for column in recipe.COLUMNS + ("kind",):
            self.assertIn(column, CANDIDATES)


class RuleTest(unittest.TestCase):
    def test_a_clip_made_with_every_current_test_fits(self):
        self.assertEqual(misfits(_config(**ALL_ON), _clip()), [])

    def test_a_clip_from_before_filler_was_on_does_not_fit(self):
        # The 23 Nov gap: 474 clips without background gameplay kept being picked after it was on.
        config = _config(**ALL_ON)
        self.assertEqual(misfits(config, _clip(filler=None)), ["editing.filler"])
        self.assertEqual(misfits(config, _clip(filler="none")), ["editing.filler"])
        self.assertEqual(misfits(_config(**{**ALL_ON, "filler": "test"}), _clip(filler="none")), [])
        self.assertEqual(misfits(_config(**{**ALL_ON, "filler": "off"}), _clip(filler="subway_surfers")),
                         ["editing.filler"])

    def test_switches_need_their_style_recorded_exactly_when_on(self):
        config = _config(**ALL_ON)
        self.assertIn("editing.test_hooks", misfits(config, _clip(hook_style=None)))
        self.assertIn("editing.test_captions", misfits(config, _clip(caption_style=None)))
        off = _config(**{**ALL_ON, "test_hooks": False, "test_captions": False})
        self.assertEqual(misfits(off, _clip(hook_style=None, caption_style=None)), [])
        self.assertIn("editing.test_hooks", misfits(off, _clip(caption_style=None)))

    def test_trim_modes(self):
        self.assertIn("editing.trim_dead_time", misfits(_config(**ALL_ON), _clip(trim=None)))
        self.assertEqual(misfits(_config(**{**ALL_ON, "trim_dead_time": "on"}), _clip(trim="trim")), [])
        self.assertIn("editing.trim_dead_time", misfits(_config(**{**ALL_ON, "trim_dead_time": "on"}), _clip()))

    def test_cuts_must_be_from_the_running_cut_test(self):
        config = _config(**ALL_ON)
        self.assertEqual(misfits(config, _clip(cut_before=30, cut_after=30)), [])
        # 45 s + 30 s came from the first cut test, which tried other lengths.
        self.assertEqual(misfits(config, _clip(cut_before=45, cut_after=30)), ["live.test_cuts"])
        self.assertEqual(misfits(config, _clip(cut_before=None, cut_after=None)), ["live.test_cuts"])
        # Controls and VOD clips always keep the standard window.
        self.assertEqual(misfits(config, _clip(kind="control", cut_before=30, cut_after=30)), [])
        self.assertEqual(misfits(config, _clip(kind="vod", cut_before=None, cut_after=None)), [])
        off = _config(**{**ALL_ON, "test_cuts": False})
        self.assertEqual(misfits(off, _clip(cut_before=20, cut_after=5)), ["live.test_cuts"])
        self.assertEqual(misfits(off, _clip(cut_before=30, cut_after=30)), [])

    def test_fitting_leaves_out_misfits_and_logs_why(self):
        config = _config(**ALL_ON)
        with self.assertLogs(level="INFO") as logs:
            kept = fitting(config, [_clip(id="good"), _clip(id="old", filler=None)], "gta6-1")
        self.assertEqual([clip["id"] for clip in kept], ["good"])
        self.assertIn("editing.filler: 1", "\n".join(logs.output))

    def test_a_warning_when_nothing_fits(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(fitting(_config(**ALL_ON), [_clip(filler=None)], "gta6-1"), [])


class FairPickingTest(unittest.TestCase):
    def test_ranking_ignores_the_arm_a_clip_was_given(self):
        features = {"hook=tease": 1.0, "filler=subway_surfers": 1.0, "trim=trim": 1.0, "captions=bold": 1.0,
                    "clip_seconds": 25, "cut_before": 20, "messages": 40, "account=gta6-1": 1.0}
        self.assertEqual(blind(features), {"clip_seconds": None, "cut_before": None, "messages": 40,
                                           "account=gta6-1": 1.0})

    def _ranked(self, clips, rank):
        class Model:
            def predict(self, rows):
                import numpy as np
                return np.array([row["rank"] for row in rows])
        return Model(), unittest.mock.patch("gta_clips.ready.clip_features",
                                            lambda db, ids: {clip_id: {"rank": rank[clip_id]} for clip_id in ids})

    def test_the_arm_with_the_fewest_posts_goes_first_even_if_the_model_prefers_another(self):
        config = _config(**ALL_ON)
        clips = [_clip(id=f"s{i}", cut_before=20, cut_after=5) for i in range(10)] +                 [_clip(id=f"l{i}", cut_before=30, cut_after=30) for i in range(10)]
        rank = {clip["id"]: (100 if clip["id"].startswith("s") else 0) + i for i, clip in enumerate(clips)}
        model, features = self._ranked(clips, rank)
        counts = {"live.test_cuts": {"20+5": 12, "30+30": 3}}
        with features, unittest.mock.patch("gta_clips.ready.EXPLORE", 0):
            ordered, _ = _order(None, clips, model, random.Random(0), config, counts)
        # 30+30 is 9 posts behind, so its best clips go first until it is nearly level (NEED_SLACK);
        # from there the model's choice and the counts keep the two close.
        self.assertEqual(ordered[0]["id"], "l9")
        firsts = [clip["id"][0] for clip in ordered]
        self.assertEqual(firsts[:7], ["l"] * 7)
        level = Counter(firsts[:13])
        self.assertLessEqual(abs((12 + level["s"]) - (3 + level["l"])), 2)

    def test_every_running_test_is_balanced_at_once(self):
        config = _config(**ALL_ON)
        clips = [_clip(id=f"{hook}-{trim}-{n}", hook_style=hook, trim=trim)
                 for hook in ("tease", "plain") for trim in ("trim", "none") for n in range(5)]
        counts = {"editing.test_hooks": {"tease": 8, "plain": 0}, "editing.trim_dead_time": {"trim": 0, "none": 8}}
        ordered, _ = _order(None, clips, None, random.Random(3), config, counts)
        # The plain-hook, trimmed clips are behind in both tests.
        self.assertTrue(all(clip["id"].startswith("plain-trim") for clip in ordered[:4]))
        # Plain is 8 posts behind, so nearly all 10 plain clips go before the tease ones.
        self.assertGreaterEqual(Counter(clip["hook_style"] for clip in ordered[:12])["plain"], 8)

    def test_arms_from_an_earlier_test_run_do_not_hold_the_rest_back(self):
        config = _config(**ALL_ON)
        clips = [_clip(id="a", cut_before=20, cut_after=5), _clip(id="b", cut_before=30, cut_after=30)]
        counts = {"live.test_cuts": {"45+30": 90, "20+5": 0, "30+30": 4}}
        ordered, _ = _order(None, clips, None, random.Random(0), config, counts)
        self.assertEqual(ordered[0]["id"], "a")

    def test_off_tests_have_no_arms(self):
        self.assertEqual(recipe.arms(_config(**{**ALL_ON, "test_hooks": False, "test_cuts": False,
                                                 "trim_dead_time": "on"}), _clip()),
                         {"editing.filler": "subway_surfers", "editing.test_captions": "standard"})

if __name__ == "__main__":
    unittest.main()
