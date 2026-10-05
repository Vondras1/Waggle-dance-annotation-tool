"""Media regression tests; no ROS installation or user recordings required."""

from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from annotation_tool import config
from annotation_tool.media import CombImage, FrameReader, _calibrated_frames


class FrameReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "clip.mp4"
        writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
        if not writer.isOpened():
            self.skipTest("OpenCV build has no MP4 encoder")
        try:
            for index in range(12):
                image = np.zeros((48, 64, 3), np.uint8)
                image[:] = (index * 18, 220 - index * 12, index * 8)
                image[:, index * 4:index * 4 + 4] = (255, 255, 255)
                writer.write(image)
        finally:
            writer.release()
        capture = cv2.VideoCapture(str(self.path))
        self.reference = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                self.reference.append(frame)
        finally:
            capture.release()
        self.assertEqual(len(self.reference), 12)
        self.clips = {"run_000001": {
            "video_path": self.path,
            "metadata": {"frame_count": 12, "width": 64, "height": 48,
                         "run": {"bbox_x_min_px": -4, "bbox_y_min_px": 20,
                                 "bbox_x_max_px": 60, "bbox_y_max_px": 68}},
        }}
        self.reader = FrameReader(self.clips, cache_frames=2)
        self.addCleanup(self.reader.close)

    def test_exact_frames_sequential_and_random_access(self):
        for index in (0, 1, 9, 3, 11, 0, 4, 5, 8, 2):
            with self.subTest(index=index):
                np.testing.assert_array_equal(
                    self.reader.read_image("run_000001", index), self.reference[index],
                )
                _, expected = cv2.imencode(
                    ".jpg", self.reference[index], [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY],
                )
                self.assertEqual(self.reader.frame("run_000001", index), expected.tobytes())

    def test_concurrent_requests_and_defensive_image_copy(self):
        indices = (11, 0, 8, 2, 5, 3, 7, 1, 9, 4, 6, 10) * 3
        with ThreadPoolExecutor(max_workers=4) as executor:
            frames = list(executor.map(lambda i: self.reader.read_image("run_000001", i), indices))
        for index, frame in zip(indices, frames):
            np.testing.assert_array_equal(frame, self.reference[index])
        first = self.reader.read_image("run_000001", 0)
        first[:] = 0
        np.testing.assert_array_equal(self.reader.read_image("run_000001", 0), self.reference[0])

    def test_rejects_unknown_clip_and_invalid_indices(self):
        with self.assertRaises(KeyError):
            self.reader.frame("../../clip.mp4", 0)
        for index in (-1, 12, 1.5, "0", True):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.reader.frame("run_000001", index)

    def test_rejects_video_and_crop_dimension_mismatches(self):
        self.clips["run_000001"]["metadata"]["width"] = 63
        with self.assertRaisesRegex(RuntimeError, "Video dimensions"):
            self.reader.frame("run_000001", 0)
        self.clips["run_000001"]["metadata"]["width"] = 64
        self.clips["run_000001"]["metadata"]["run"]["bbox_x_max_px"] = 59
        with self.assertRaisesRegex(RuntimeError, "Crop bounds"):
            self.reader.frame("run_000001", 0)

    def test_caches_are_bounded_and_capture_eviction_closes_decoder(self):
        self.clips["run_000002"] = copy.deepcopy(self.clips["run_000001"])
        reader = FrameReader(self.clips, max_captures=1, cache_frames=2, cache_bytes=24000)
        self.addCleanup(reader.close)
        reader.frame("run_000001", 0)
        capture = reader._captures["run_000001"][0]
        reader.frame("run_000002", 1)
        self.assertFalse(capture.isOpened())
        for index in range(12):
            reader.frame("run_000002", index)
            self.assertLessEqual(len(reader._cache), 2)
            self.assertLessEqual(reader._cache_size, 24000)
            self.assertLessEqual(len(reader._captures), 1)
        reader.close()
        reader.close()
        self.assertEqual(reader._cache_size, 0)

    def test_single_frame_larger_than_cache_budget_still_works(self):
        reader = FrameReader(self.clips, cache_bytes=1)
        self.addCleanup(reader.close)
        image = cv2.imdecode(np.frombuffer(reader.frame("run_000001", 0), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (48, 64, 3))
        self.assertEqual(reader._cache_size, 0)


class CombImageTests(unittest.TestCase):
    def test_reader_import_survives_detector_module_shadowing_without_ros(self):
        # The detector's CLI file and directory share a name. Import its reader
        # by file, even when the CLI is already imported as a top-level module.
        serialization = ModuleType("rclpy.serialization")
        serialization.deserialize_message = lambda *args: None
        messages = ModuleType("sensor_msgs.msg")
        messages.CompressedImage = type("CompressedImage", (), {})
        messages.CameraInfo = type("CameraInfo", (), {})
        with patch.dict(sys.modules, {
            "candidate_run_detector": ModuleType("candidate_run_detector"),
            "rosbag2_py": ModuleType("rosbag2_py"),
            "rclpy.serialization": serialization,
            "sensor_msgs.msg": messages,
        }):
            iterator = _calibrated_frames("/unused", "/image", "/info")
            self.assertEqual(Path(iterator.gi_code.co_filename).name, "rosbag_io.py")
            self.assertEqual(iterator.gi_frame.f_locals["topic"], "/image")
            iterator.close()  # No bag access or ROS execution is needed.

    def test_supplied_image_needs_no_ros_and_preserves_full_dimensions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "undistorted.png"
            cv2.imwrite(str(path), np.full((180, 320, 3), 130, np.uint8))
            with patch("annotation_tool.media._calibrated_frames", side_effect=AssertionError("ROS called")):
                comb = CombImage({}, image_path=path)
                self.assertTrue(comb.info()["available"])
                self.assertEqual((comb.info()["width"], comb.info()["height"]), (320, 180))
                decoded = cv2.imdecode(np.frombuffer(comb.get(), np.uint8), cv2.IMREAD_COLOR)
                self.assertEqual(decoded.shape, (180, 320, 3))
                self.assertIs(comb.get(), comb.get())

    def test_first_calibrated_bag_frame_is_used_and_iterator_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []

            def frames():
                try:
                    events.append("read")
                    yield 0.0, np.full((180, 320), 130, np.uint8)
                    events.append("read another")
                    yield 0.1, np.full((180, 320), 160, np.uint8)
                finally:
                    events.append("closed")

            clips = {"run": {"metadata": {"source": {
                "bag": directory, "topic": "/my/image", "camera_info_topic": "/my/info",
            }}}}
            with patch("annotation_tool.media._calibrated_frames", return_value=frames()) as reader:
                comb = CombImage(clips)
                self.assertTrue(comb.prepare()["available"])
                self.assertEqual((comb.info()["width"], comb.info()["height"]), (320, 180))
                comb.get()
                reader.assert_called_once_with(directory, "/my/image", "/my/info")
                self.assertEqual(events, ["read", "closed"])

    def test_missing_ros_keeps_clips_independent_and_reports_image_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("annotation_tool.media._calibrated_frames", side_effect=ModuleNotFoundError("rosbag2_py")):
                comb = CombImage({}, bag_path=directory)
                self.assertFalse(comb.info()["available"])
                self.assertIn("--comb-image", comb.info()["message"])
                self.assertIn("rosbag2_py", comb.info()["message"])
                with self.assertRaisesRegex(RuntimeError, "--comb-image"):
                    comb.get()

    def test_missing_bag_and_invalid_image_are_recoverable(self):
        self.assertFalse(CombImage({}).info()["available"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.png"
            path.write_text("not an image")
            info = CombImage({}, image_path=path).info()
            self.assertFalse(info["available"])
            self.assertIn("--comb-image", info["message"])


if __name__ == "__main__":
    unittest.main()
