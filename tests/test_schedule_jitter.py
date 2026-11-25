import unittest

from gta_clips.config import ConfigError, ScheduleConfig, _schedule
from gta_clips.scheduler import spacing_seconds


class ScheduleJitterTests(unittest.TestCase):
    def test_no_jitter_keeps_exact_spacing(self):
        settings = ScheduleConfig(daily_limit=3, min_spacing_minutes=480)
        self.assertEqual(spacing_seconds(settings, "instagram", "gta6-1", 1_000_000), 480 * 60)

    def test_jitter_stays_in_range_and_is_stable_per_post(self):
        settings = ScheduleConfig(daily_limit=3, min_spacing_minutes=480, jitter_minutes=90)
        delays = [spacing_seconds(settings, "instagram", "gta6-1", last)
                  for last in range(1_000_000, 1_000_200)]
        self.assertTrue(all(480 * 60 <= d <= (480 + 90) * 60 for d in delays))
        self.assertGreater(len(set(delays)), 50)  # different posts get different delays
        # Checking again for the same last post gives the same due time.
        self.assertEqual(spacing_seconds(settings, "instagram", "gta6-1", 1_000_000), delays[0])

    def test_jitter_differs_between_accounts(self):
        settings = ScheduleConfig(daily_limit=3, min_spacing_minutes=480, jitter_minutes=90)
        self.assertNotEqual(spacing_seconds(settings, "instagram", "gta6-1", 1_000_000),
                            spacing_seconds(settings, "instagram", "league", 1_000_000))

    def test_config_reads_and_checks_jitter(self):
        schedule = _schedule({"instagram": {"daily_limit": 3, "min_spacing_minutes": 480,
                                            "jitter_minutes": 90}})
        self.assertEqual(schedule["instagram"].jitter_minutes, 90)
        self.assertEqual(schedule["youtube"].jitter_minutes, 0)
        with self.assertRaises(ConfigError):
            _schedule({"instagram": {"jitter_minutes": 500}})


if __name__ == "__main__":
    unittest.main()
