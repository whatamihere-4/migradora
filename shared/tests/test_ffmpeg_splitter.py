"""Tests for ffmpeg keyframe-aligned splitting."""

from __future__ import annotations

import unittest

from migradora.ffmpeg_splitter import (
    _format_read_interval,
    _select_keyframe_at_or_after,
    plan_keyframe_part_starts,
)


class KeyframeSplitPlanTests(unittest.TestCase):
    def test_single_part_when_target_exceeds_duration(self) -> None:
        kf = [0.0, 10.0, 20.0, 30.0]
        self.assertEqual(plan_keyframe_part_starts(kf, duration=25.0, target_segment_time=60), [0.0])

    def test_aligns_to_next_keyframe_after_target(self) -> None:
        kf = [0.0, 10.0, 20.0, 30.0, 60.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=55.0, target_segment_time=15),
            [0.0, 20.0],
        )

    def test_skips_duplicate_keyframe_times(self) -> None:
        kf = [0.0, 10.0, 10.0, 25.0, 40.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=50.0, target_segment_time=12),
            [0.0, 25.0, 40.0],
        )

    def test_inserts_zero_when_first_keyframe_is_late(self) -> None:
        kf = [2.0, 12.0, 24.0]
        self.assertEqual(
            plan_keyframe_part_starts(kf, duration=30.0, target_segment_time=10),
            [0.0, 12.0, 24.0],
        )


class SparseKeyframeHelperTests(unittest.TestCase):
    def test_format_read_interval_window(self) -> None:
        self.assertEqual(_format_read_interval(100.0, 200.0, 1000.0), "100.000%200.000")

    def test_select_keyframe_at_or_after(self) -> None:
        kf = [0.0, 10.0, 20.0, 30.0]
        self.assertEqual(_select_keyframe_at_or_after(kf, 15.0), 20.0)
        self.assertEqual(_select_keyframe_at_or_after(kf, 30.0), 30.0)
        self.assertIsNone(_select_keyframe_at_or_after(kf, 31.0))


if __name__ == "__main__":
    unittest.main()
