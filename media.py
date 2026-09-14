"""Lazy, bounded media access for the local run annotation server.

Clip IDs always resolve through the server's discovered clip catalog.  Full-comb
images use the same full-resolution *undistorted* coordinates as exported clips;
a supplied image must already be undistorted with the detector's calibration.
"""

from collections import OrderedDict
import importlib.util
from numbers import Integral
from pathlib import Path
import sys
import threading

from . import config


def _opencv():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required; install opencv-python and numpy.") from exc
    return cv2


def _encode_jpeg(image):
    cv2 = _opencv()
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, config.JPEG_QUALITY])
    if not ok:
        raise RuntimeError("OpenCV could not encode the image as JPEG.")
    return encoded.tobytes()


class FrameReader:
    """Read exact clip frame indices, sharing a small pool of decoders.

    All decoding and cache operations are serialized because OpenCV captures are
    not thread-safe. Sequential requests avoid seeking; arbitrary requests seek
    by frame index and fall back to decoding from the start when unsupported.
    """

    def __init__(self, clips, *, max_captures=config.MAX_CAPTURES, cache_frames=config.CACHE_FRAMES,
                 cache_bytes=config.CACHE_BYTES):
        if max_captures < 1 or cache_frames < 1 or cache_bytes < 1:
            raise ValueError("Media cache limits must be positive.")
        self.clips = clips
        self.max_captures = max_captures
        self.cache_frames = cache_frames
        self.cache_bytes = cache_bytes
        self._captures = OrderedDict()
        self._cache = OrderedDict()
        self._cache_size = 0
        self._lock = threading.RLock()

    def _validate(self, clip_id, index):
        if clip_id not in self.clips:
            raise KeyError(f"Unknown clip: {clip_id}")
        metadata = self.clips[clip_id]["metadata"]
        if isinstance(index, bool) or not isinstance(index, Integral):
            raise ValueError("Frame index must be an integer.")
        if index < 0 or index >= metadata["frame_count"]:
            raise ValueError(
                f"Frame index must be between 0 and {metadata['frame_count'] - 1}."
            )
        return metadata

    def _open_capture(self, clip_id):
        cv2 = _opencv()
        capture = cv2.VideoCapture(str(self.clips[clip_id]["video_path"]))
        if not capture.isOpened():
            capture.release()
            raise RuntimeError(f"Could not open video for {clip_id}.")
        self._captures[clip_id] = [capture, 0]
        while len(self._captures) > self.max_captures:
            _, (old, _) = self._captures.popitem(last=False)
            old.release()
        return self._captures[clip_id]

    def _decode(self, clip_id, index, metadata):
        cv2 = _opencv()
        state = self._captures.get(clip_id)
        if state is None:
            state = self._open_capture(clip_id)
        else:
            self._captures.move_to_end(clip_id)
        capture, next_index = state
        if next_index != index:
            did_seek = capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            if not did_seek or abs(capture.get(cv2.CAP_PROP_POS_FRAMES) - index) > 0.5:
                capture.release()
                del self._captures[clip_id]
                state = self._open_capture(clip_id)
                capture = state[0]
                for _ in range(index):
                    if not capture.grab():
                        raise RuntimeError(f"Could not seek to frame {index} of {clip_id}.")
        ok, image = capture.read()
        state[1] = index + 1
        if not ok or image is None:
            capture.release()
            del self._captures[clip_id]
            raise RuntimeError(f"Could not decode frame {index} of {clip_id}.")
        height, width = image.shape[:2]
        if (width, height) != (metadata["width"], metadata["height"]):
            raise RuntimeError(
                f"Video dimensions for {clip_id} are {width}×{height}, but the "
                f"metadata declares {metadata['width']}×{metadata['height']}. "
                "Use the matching clip video and metadata before annotating."
            )
        run = metadata.get("run", {})
        bounds = ("bbox_x_min_px", "bbox_y_min_px", "bbox_x_max_px", "bbox_y_max_px")
        if all(name in run for name in bounds):
            crop_width = run[bounds[2]] - run[bounds[0]]
            crop_height = run[bounds[3]] - run[bounds[1]]
            if (crop_width, crop_height) != (width, height):
                raise RuntimeError(
                    f"Crop bounds for {clip_id} do not match its video dimensions; "
                    "full-image coordinates would be incorrect."
                )
        return image

    def _entry(self, clip_id, index):
        metadata = self._validate(clip_id, index)
        key = (clip_id, int(index))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        image = self._decode(clip_id, int(index), metadata)
        entry = {"image": image, "jpeg": None}
        self._cache[key] = entry
        self._cache_size += image.nbytes
        self._trim()
        return entry

    def _trim(self):
        while self._cache and (len(self._cache) > self.cache_frames
                               or self._cache_size > self.cache_bytes):
            _, old = self._cache.popitem(last=False)
            self._cache_size -= old["image"].nbytes + len(old["jpeg"] or b"")

    def frame(self, clip_id, index):
        """Return a JPEG for a zero-based frame index in a discovered clip."""
        with self._lock:
            entry = self._entry(clip_id, index)
            if entry["jpeg"] is None:
                entry["jpeg"] = _encode_jpeg(entry["image"])
                if (clip_id, int(index)) in self._cache:
                    self._cache_size += len(entry["jpeg"])
                    self._trim()
            return entry["jpeg"]

    def read_image(self, clip_id, index):
        """Return a caller-owned BGR ndarray for export, without JPEG loss."""
        with self._lock:
            return self._entry(clip_id, index)["image"].copy()

    def close(self):
        """Release all video decoders and cached images; safe to call twice."""
        with self._lock:
            for capture, _ in self._captures.values():
                capture.release()
            self._captures.clear()
            self._cache.clear()
            self._cache_size = 0


def _calibrated_frames(bag_path, topic, camera_info_topic):
    """Import the existing ROS reader only when a bag background is requested."""
    paths = [str(config.DETECTOR_DIR)]
    inserted = []
    try:
        # rosbag_io currently imports its sibling config as a top-level module.
        for path in reversed(paths):
            if path not in sys.path:
                sys.path.insert(0, path)
                inserted.append(path)
        # That directory also contains candidate_run_detector.py, which shadows
        # a namespace-package import when the directory is on sys.path.
        spec = importlib.util.spec_from_file_location(
            "_annotation_rosbag_io", config.ROSBAG_READER_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for path in inserted:
            sys.path.remove(path)
    return module.iter_calibrated_frames(
        str(bag_path), topic=topic, camera_info_topic=camera_info_topic,
    )


class CombImage:
    """One undistorted, full-resolution background for grouping runs."""

    def __init__(self, clips, image_path=None, bag_path=None):
        self.clips = clips
        self.image_path = Path(image_path).expanduser() if image_path else None
        self.bag_path = Path(bag_path).expanduser() if bag_path else None
        self._jpeg = None
        self._prepared = False
        self._lock = threading.RLock()
        self._info = {"available": False, "width": 0, "height": 0,
                      "message": "Full-comb background has not been loaded."}

    def prepare(self):
        """Load once; report recoverable extraction failures without blocking clips."""
        with self._lock:
            if self._prepared:
                return dict(self._info)
            self._prepared = True
            try:
                if self.image_path is not None:
                    image = _opencv().imread(str(self.image_path), _opencv().IMREAD_COLOR)
                    if image is None:
                        raise RuntimeError(f"Could not read image {self.image_path}.")
                    message = "Using the supplied full-resolution undistorted comb image."
                else:
                    first = next(iter(self.clips.values()), None)
                    source = first["metadata"].get("source", {}) if first else {}
                    bag = self.bag_path or source.get("bag")
                    if not bag:
                        raise RuntimeError("Clip metadata does not identify a source ROS bag.")
                    if not Path(bag).exists():
                        raise RuntimeError(f"Source ROS bag does not exist: {bag}.")
                    iterator = _calibrated_frames(
                        bag,
                        source.get("topic", config.IMAGE_TOPIC),
                        source.get("camera_info_topic", config.CAMERA_INFO_TOPIC),
                    )
                    try:
                        _, image = next(iterator)
                    finally:
                        close = getattr(iterator, "close", None)
                        if close is not None:
                            close()
                    message = "Using the first calibrated, undistorted frame from the source bag."
                if image is None or image.ndim not in (2, 3) or min(image.shape[:2]) < 1:
                    raise RuntimeError("The full-comb image is empty or invalid.")
                height, width = image.shape[:2]
                self._jpeg = _encode_jpeg(image)
                self._info = {"available": True, "width": int(width), "height": int(height),
                              "message": message}
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                self._info = {
                    "available": False, "width": 0, "height": 0,
                    "message": f"Full-comb background unavailable: {reason} "
                    "Source your ROS 2 environment and check --bag, or start with "
                    "--comb-image /path/to/full-resolution-undistorted-comb.png.",
                }
            return dict(self._info)

    def info(self):
        return self.prepare()

    def get(self):
        with self._lock:
            info = self.prepare()
            if not info["available"]:
                raise RuntimeError(info["message"])
            return self._jpeg
