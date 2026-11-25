"""Render on demand: windows are rendered only when an account set's buffer of postable clips runs low."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import re
import time
import unittest

from gta_clips import live_render, on_demand
from gta_clips.config import load_config
from gta_clips.db import connect

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = """
[[accounts]]
name = "gta6-4"
category = "Grand Theft Auto VI"
language = "en"
"""
NOW = time.time()


class OnDemandTest(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        folder = Path(self.folder.name)
        text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        text = re.sub(r'data_dir = "data"', f'data_dir = "{folder.as_posix()}/data"', text)
        (folder / "config.toml").write_text(text + ACCOUNTS, encoding="utf-8")
        config = load_config(folder / "config.toml")
        self.config = replace(config, live=replace(config.live, render_on_demand=True, render_buffer=2,
                                                   test_viewer_clips=True))
        self.count = 0

    def tearDown(self):
        self.folder.cleanup()

    def _window(self, kind="burst", messages=50, baseline=10.0, views=0, age=3600, requested=None, state="waiting"):
        self.count += 1
        anchor = int(NOW - age) + self.count
        with connect(self.config.paths.database) as db:
            db.execute("""INSERT INTO live_bursts(stream_id, anchor, broadcaster_id, streamer, game_name, messages,
                              state, retry_at, detail, trigger_kind, account, render_requested)
                          VALUES ('555', ?, 'b', 's', 'Grand Theft Auto VI', ?, ?, ?, ?, ?, 'gta6-4', ?)""",
                       (anchor, messages, state, anchor, f"Twitch clip x ({views} views)" if views else None,
                        kind, requested))
            if kind == "burst":
                db.execute("""INSERT INTO burst_features(stream_id, anchor, baseline_per_10s, multiplier, minimum_messages,
                              warmup_seconds) VALUES ('555', ?, ?, 6, 0, 300)""",
                           (anchor, baseline))
        return anchor

    def _requested(self):
        with connect(self.config.paths.database) as db:
            return {row[0] for row in db.execute(
                "SELECT anchor FROM live_bursts WHERE render_requested IS NOT NULL")}

    def test_off_asks_for_nothing(self):
        self._window()
        off = replace(self.config, live=replace(self.config.live, render_on_demand=False))
        self.assertEqual(on_demand.request_renders(off, NOW), 0)

    def test_fills_the_buffer_with_the_strongest_windows_once(self):
        off_test = replace(self.config, live=replace(self.config.live, test_viewer_clips=False))
        weak = self._window(messages=20)          # 2x its baseline
        strong = self._window(messages=90)        # 9x
        middle = self._window(messages=200, baseline=40.0)  # 5x
        self.assertEqual(on_demand.request_renders(off_test, NOW), 2)
        self.assertEqual(self._requested(), {strong, middle})
        # Those two are still rendering, so the buffer counts as full.
        self.assertEqual(on_demand.request_renders(off_test, NOW + 60), 0)
        self.assertNotIn(weak, self._requested())

    def test_old_moments_and_finished_windows_are_not_asked_for(self):
        self._window(age=40 * 3600)
        self._window(state="skipped")
        self.assertEqual(on_demand.request_renders(self.config, NOW), 0)

    def test_a_request_that_never_rendered_stops_holding_its_place(self):
        self._window(requested=int(NOW - 7 * 3600))
        fresh = self._window()
        self.assertEqual(on_demand.request_renders(self.config, NOW), 1)
        self.assertIn(fresh, self._requested())

    def test_the_source_test_gets_both_kinds(self):
        self._window(messages=90)
        self._window(messages=80)
        self._window(messages=70)
        viewer = self._window(kind="viewer_clip", messages=0, views=5000)
        on_demand.request_renders(self.config, NOW)
        self.assertIn(viewer, self._requested())
        self.assertEqual(len(self._requested()), 2)

    def test_render_live_only_renders_requested_windows_when_on_demand(self):
        asked = self._window(requested=int(NOW))
        self._window()
        seen = []
        with mock.patch.object(live_render, "_attempt", lambda config, burst, action: seen.append(burst["anchor"])):
            live_render._process_due(self.config, None, None)
        self.assertEqual(seen, [asked])
        seen.clear()
        off = replace(self.config, live=replace(self.config.live, render_on_demand=False))
        with mock.patch.object(live_render, "_attempt", lambda config, burst, action: seen.append(burst["anchor"])):
            live_render._process_due(off, None, None)
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
