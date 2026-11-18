"""Run schedule, settings editing, the supervisor, watcher status and the dashboard server."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import MagicMock, patch
import http.client
import json
import re
import time
import unittest

from gta_clips import dashboard, runner
from gta_clips.config import ConfigError, RuntimeConfig, load_config
from gta_clips.db import connect
from gta_clips.live import STATUS_FILE, STOP_FILE, Broadcast, WatchStatus
from gta_clips.settings_edit import save, set_value

ROOT = Path(__file__).resolve().parents[1]


class TempConfig(unittest.TestCase):
    """A copy of config.example.toml whose data folder is a temporary directory."""

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        # data_dir = "data" resolves next to the copied config file.
        self.config_path = self.folder / "config.toml"
        self.config_path.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
        self.config = load_config(self.config_path)
        self.assertEqual(self.config.paths.data_dir, (self.folder / "data").resolve())


class RuntimeScheduleTest(unittest.TestCase):
    def test_overnight_window_belongs_to_its_start_day(self):
        friday_night = RuntimeConfig(watch_days=("fri",), watch_start="22:00", watch_end="02:00")
        self.assertTrue(friday_night.watching_at(datetime(2026, 11, 20, 23, 0)))   # Friday
        self.assertTrue(friday_night.watching_at(datetime(2026, 11, 21, 1, 30)))   # Saturday morning
        self.assertFalse(friday_night.watching_at(datetime(2026, 11, 21, 2, 0)))
        self.assertFalse(friday_night.watching_at(datetime(2026, 11, 21, 23, 0)))  # Saturday night
        self.assertFalse(friday_night.watching_at(datetime(2026, 11, 20, 21, 59)))
        self.assertEqual(friday_night.describe(), "22:00–02:00, Fri")

    def test_daytime_window_and_all_day(self):
        weekdays = RuntimeConfig(watch_days=("mon", "tue", "wed", "thu", "fri"), watch_start="09:00",
                                 watch_end="17:00")
        self.assertTrue(weekdays.watching_at(datetime(2026, 11, 18, 9, 0)))
        self.assertFalse(weekdays.watching_at(datetime(2026, 11, 18, 17, 0)))
        self.assertFalse(weekdays.watching_at(datetime(2026, 11, 22, 12, 0)))  # Sunday
        self.assertTrue(RuntimeConfig().watching_at(datetime(2026, 11, 22, 3, 0)))
        self.assertEqual(RuntimeConfig().describe(), "All day, every day")


class RuntimeValidationTest(TempConfig):
    def write(self, runtime: str):
        path = self.folder / "check.toml"
        # Swap the example's [runtime] table for the one under test.
        text = re.sub(r"(?ms)^\[runtime\].*?(?=^\[)", "", self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("[runtime]", text)
        path.write_text(text + "\n[runtime]\n" + runtime, encoding="utf-8")
        return load_config(path)

    def test_valid_runtime_loads(self):
        config = self.write('watch_days = ["sat", "fri"]\nwatch_start = "18:30"\nrender_every_minutes = 30\n')
        self.assertEqual(config.runtime.watch_days, ("fri", "sat"))
        self.assertEqual(config.runtime.render_every_minutes, 30)

    def test_invalid_runtime_is_rejected(self):
        for bad in ('watch_start = "25:00"', 'watch_end = "9:00"', 'watch_days = ["monday"]',
                    "render_every_minutes = 2", "ingest_every_minutes = -5", "unknown = 1"):
            with self.subTest(bad), self.assertRaises(ConfigError):
                self.write(bad + "\n")


class SettingsEditTest(TempConfig):
    def test_set_value_keeps_comments_and_other_tables(self):
        text = '# top comment\n[live]\n# keep me\nmax_channels = 5  # old\n\n[editing]\nfps = 30\n'
        result = set_value(text, "live", "max_channels", 8)
        self.assertIn("# top comment", result)
        self.assertIn("# keep me", result)
        self.assertIn("max_channels = 8\n", result)
        self.assertIn("[editing]\nfps = 30", result)
        added = set_value(result, "live", "cooldown_seconds", 120)
        self.assertLess(added.index("cooldown_seconds = 120"), added.index("[editing]"))
        table = set_value(added, "runtime", "watch_days", ["fri", "sat"])
        self.assertTrue(table.endswith('[runtime]\nwatch_days = ["fri", "sat"]\n'))

    def test_multiline_value_is_left_alone(self):
        with self.assertRaises(ConfigError):
            set_value('[live]\nstreamers = [\n  "a",\n]\n', "live", "streamers", ["b"])

    def test_save_validates_backs_up_and_replaces(self):
        before = self.config_path.read_text(encoding="utf-8")
        config = save(self.config_path, {"runtime.watch_start": "20:00", "runtime.watch_end": "01:00",
                                         "live.max_channels": 4, "highlights.require_motion": False})
        self.assertEqual(config.runtime.watch_start, "20:00")
        self.assertEqual(config.live.max_channels, 4)
        self.assertFalse(config.highlights.require_motion)
        backups = list((self.folder / "data" / "backups").glob("config-*.toml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), before)
        self.assertEqual(load_config(self.config_path).runtime.watch_end, "01:00")

    def test_invalid_change_leaves_config_untouched(self):
        before = self.config_path.read_text(encoding="utf-8")
        for changes in ({"live.max_channels": 50}, {"live.max_channels": "5"}, {"runtime.watch_start": "7pm"},
                        {"paths.data_dir": "elsewhere"}, {"runtime.render_every_minutes": 3}, {}):
            with self.subTest(changes), self.assertRaises(ConfigError):
                save(self.config_path, changes)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertFalse((self.folder / "config.toml.candidate").exists())


class RunnerTest(TempConfig):
    def setUp(self):
        super().setUp()
        save(self.config_path, {"runtime.watch_days": ["wed"], "runtime.watch_start": "09:00",
                                "runtime.watch_end": "17:00", "runtime.render_every_minutes": 15})
        self.spawned = []

        def spawn(_path, name):
            process = MagicMock()
            process.poll.return_value = None
            process.pid = 1000 + len(self.spawned)
            self.spawned.append((name, process))
            return process

        patcher = patch.object(runner, "_spawn", side_effect=spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = runner.Runner(self.config_path, load_config(self.config_path))

    def test_starts_watcher_in_window_and_jobs_after_a_minute(self):
        start = datetime(2026, 11, 18, 10, 0).timestamp()  # Wednesday
        self.runner.tick(start)
        self.assertEqual([name for name, _ in self.spawned], ["watch-live"])
        self.runner.tick(start + 61)
        self.assertEqual([name for name, _ in self.spawned], ["watch-live", "render-live"])
        status = json.loads((self.folder / "data" / runner.STATUS_FILE).read_text(encoding="utf-8"))
        self.assertTrue(status["in_window"])
        self.assertTrue(status["watcher"]["running"])
        self.assertEqual(status["jobs"]["render-live"]["every_minutes"], 15)
        self.assertEqual(status["jobs"]["publish"]["every_minutes"], 0)

    def test_asks_watcher_to_stop_outside_window(self):
        self.runner.tick(datetime(2026, 11, 18, 16, 59).timestamp())
        self.runner.tick(datetime(2026, 11, 18, 17, 0).timestamp())
        self.assertTrue((self.folder / "data" / STOP_FILE).exists())
        self.assertIsNotNone(self.runner.watcher.stopping_since)

    def test_failing_watcher_backs_off(self):
        start = datetime(2026, 11, 18, 10, 0).timestamp()
        self.runner.tick(start)
        _, process = self.spawned[0]
        process.poll.return_value, process.returncode = 1, 1
        self.runner.tick(start + 120)
        self.assertEqual(len([n for n, _ in self.spawned if n == "watch-live"]), 1)
        self.assertEqual(self.runner.watcher.next_run, start + 180)

    def test_bad_hand_edit_keeps_last_good_settings(self):
        self.config_path.write_text(self.config_path.read_text(encoding="utf-8").replace(
            'watch_start = "09:00"', 'watch_start = "nine"'), encoding="utf-8")
        self.runner.tick(datetime(2026, 11, 18, 10, 0).timestamp())
        self.assertIn("watch_start", self.runner.config_error)
        self.assertEqual(self.runner.config.runtime.watch_start, "09:00")


class WatchStatusTest(TempConfig):
    def test_writes_status_and_one_snapshot_per_interval(self):
        with connect(self.config.paths.database):
            pass
        status = WatchStatus(self.config, 7)
        desired = {"b1": Broadcast("s1", "b1", "alpha", time.time() - 600, 1500, 1, "en"),
                   "b2": Broadcast("s2", "b2", "beta", time.time() - 60, 300, 2, "en")}
        status.write("watching", desired, {"b1": "sub"}, {"b1": time.monotonic() - 30})
        status.write("watching", desired, {"b1": "sub"}, {"b1": time.monotonic() - 40})
        value = json.loads((self.folder / "data" / STATUS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(value["session_id"], 7)
        alpha = next(item for item in value["channels"] if item["login"] == "alpha")
        self.assertTrue(alpha["watching"])
        self.assertEqual(alpha["viewers"], 1500)
        with connect(self.config.paths.database) as db:
            rows = db.execute("SELECT streamer, viewers, watched FROM channel_snapshots ORDER BY streamer").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("alpha", 1500, 1), ("beta", 300, 0)])


class DashboardTest(TempConfig):
    def test_overview_on_empty_database(self):
        result = dashboard.overview(self.config)
        self.assertEqual(result["channels"], [])
        self.assertEqual(result["totals"]["clips"], 0)
        self.assertEqual(len(result["windows_24h"]), 24)
        self.assertEqual(len(result["chat_60m"]), 60)
        json.dumps(result)

    def test_overview_ignores_stale_watcher_status(self):
        data = self.folder / "data"
        data.mkdir(parents=True, exist_ok=True)
        channel = {"login": "alpha", "watching": True, "viewers": 10}
        (data / STATUS_FILE).write_text(json.dumps(
            {"state": "watching", "updated": time.time() - 600, "channels": [channel]}), encoding="utf-8")
        self.assertEqual(dashboard.overview(self.config)["channels"], [])
        (data / STATUS_FILE).write_text(json.dumps(
            {"state": "watching", "updated": time.time(), "channels": [channel]}), encoding="utf-8")
        result = dashboard.overview(self.config)
        self.assertEqual(result["totals"]["viewers_now"], 10)

    def test_server_checks_host_and_token_and_saves_settings(self):
        server = dashboard.make_server(self.config, self.config_path, 0)
        port = server.server_port
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, body=None, host=f"127.0.0.1:{port}", token=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Host": host, "Content-Type": "application/json"}
            if token:
                headers["X-Dashboard-Token"] = token
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            data = response.read()
            connection.close()
            return response.status, data

        status, _ = request("GET", "/api/overview", host="evil.example:80")
        self.assertEqual(status, 403)
        status, page = request("GET", "/")
        self.assertEqual(status, 200)
        token = page.decode().split("const token = '", 1)[1].split("'", 1)[0]
        self.assertNotEqual(token, "__TOKEN__")
        status, _ = request("POST", "/api/settings", {"changes": {"live.max_channels": 3}})
        self.assertEqual(status, 403)
        status, body = request("POST", "/api/settings", {"changes": {"live.max_channels": 99}}, token=token)
        self.assertEqual(status, 400)
        self.assertIn("between", json.loads(body)["error"])
        status, body = request("POST", "/api/settings", {"changes": {"live.max_channels": 3}}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["values"]["live.max_channels"], 3)
        self.assertEqual(load_config(self.config_path).live.max_channels, 3)
        status, body = request("GET", "/api/overview")
        self.assertEqual((status, json.loads(body)["max_channels"]), (200, 3))
        status, _ = request("POST", "/api/control", {"action": "format-disk"}, token=token)
        self.assertEqual(status, 400)

    def test_control_writes_stop_files(self):
        data = self.folder / "data"
        data.mkdir(parents=True, exist_ok=True)
        self.assertIn("not running", dashboard.control(self.config, self.config_path, "stop"))
        (data / runner.STATUS_FILE).write_text(json.dumps({"state": "running", "updated": time.time()}),
                                               encoding="utf-8")
        dashboard.control(self.config, self.config_path, "stop")
        self.assertTrue((data / runner.STOP_FILE).exists())
        dashboard.control(self.config, self.config_path, "restart-watcher")
        self.assertTrue((data / STOP_FILE).exists())


if __name__ == "__main__":
    unittest.main()
