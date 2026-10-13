import unittest

from annotation_tool.angles import direction_from_centers
from annotation_tool.model import default_annotation, validate_annotation
from annotation_tool.tests.test_model import metadata, keyframe


class AutoDirectionTests(unittest.TestCase):
    def test_compass_directions_and_reverse(self):
        for point, expected in [((0, -10), 0), ((10, 0), 90),
                                ((0, 10), -180), ((-10, 0), -90), ((10, 10), 135)]:
            with self.subTest(point=point):
                self.assertAlmostEqual(direction_from_centers([(0, 0), point]), expected)
                reverse = direction_from_centers([point, (0, 0)])
                self.assertAlmostEqual((reverse - expected) % 360, 180)

    def test_ambiguous_trajectories_remain_unknown(self):
        for points in [[], [(0, 0)], [(2, 3), (2, 3)],
                       [(0, 0), (2, 0), (0, 0)], [(0, 0), (0, 2), (2, 0), (2, 2)]]:
            self.assertIsNone(direction_from_centers(points))

    def test_auto_uses_only_definite_annotated_frames_and_recomputes(self):
        a = default_annotation(metadata())
        self.assertEqual(a['direction_mode'], 'auto')
        a.update(start_frame=1, end_frame=3, direction_deg=42,
                 keyframes=[keyframe(0, [60,60,70,70]), keyframe(1, [0,0,10,10]),
                            keyframe(3, [10,0,20,10]), keyframe(4, [70,60,80,70])])
        self.assertAlmostEqual(validate_annotation(a, metadata())['direction_deg'], 90)
        a['keyframes'][2]['bbox'] = [0,10,10,20]
        self.assertAlmostEqual(validate_annotation(a, metadata())['direction_deg'], -180)
        a['keyframes'] = [keyframe(1)]
        self.assertIsNone(validate_annotation(a, metadata())['direction_deg'])

    def test_manual_and_legacy_directions_preserved(self):
        a = default_annotation(metadata())
        a.update(direction_mode='manual', direction_deg=37, keyframes=[keyframe(0), keyframe(4)])
        self.assertEqual(validate_annotation(a, metadata())['direction_deg'], 37)
        del a['direction_mode']
        self.assertEqual(validate_annotation(a, metadata())['direction_mode'], 'manual')
        a['direction_deg'] = None
        self.assertEqual(validate_annotation(a, metadata())['direction_mode'], 'auto')
        a['direction_mode'] = 'invalid'
        with self.assertRaises(ValueError):
            validate_annotation(a, metadata())
