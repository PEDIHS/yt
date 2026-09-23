import unittest

from analytics import _duration_seconds, _summary_from_row, normalize_period


class AnalyticsHelpersTest(unittest.TestCase):
    def test_normalize_period(self):
        self.assertEqual(normalize_period(7), 7)
        self.assertEqual(normalize_period("90"), 90)
        self.assertEqual(normalize_period(30), 28)
        self.assertEqual(normalize_period(None), 28)

    def test_duration_seconds(self):
        self.assertEqual(_duration_seconds("PT1M30S"), 90)
        self.assertEqual(_duration_seconds("PT2H3M4S"), 7384)
        self.assertEqual(_duration_seconds(None), 0)

    def test_summary_calculates_net_subscribers(self):
        summary = _summary_from_row({
            "views": 1200,
            "likes": 90,
            "comments": 12,
            "shares": 8,
            "subscribersGained": 33,
            "subscribersLost": 7,
            "estimatedMinutesWatched": 5000,
            "averageViewDuration": 41.5,
        })
        self.assertEqual(summary["views"], 1200)
        self.assertEqual(summary["subscribers_net"], 26)
        self.assertEqual(summary["watch_minutes"], 5000)
        self.assertEqual(summary["average_view_duration"], 41.5)


if __name__ == "__main__":
    unittest.main()
