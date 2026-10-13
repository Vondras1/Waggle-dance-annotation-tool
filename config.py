"""Annotation defaults. Edit here and restart the server; CLI options take priority."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
STATIC = ROOT / "static"
# Choose a dataset here. Paths and square side lengths (clip pixels) live together.
DEFAULT_DATASET = "prague_2026-09-03T11"
DATASETS = {
    "example_dataset": {
        "clips_dir": ROOT / "guide" / "example_dataset" / "run_clips",
        "bbox_size": 70,
    },
    "berlin_2021": {
        "clips_dir": REPO / "dance_decoder/yowo_data/berlin_2021" / "cam0_2021" / "run_clips",
        "bbox_size": 55,
    },
    "prague_2026-09-08T14": {
        "clips_dir": REPO / "dance_decoder/yowo_data/prague_2026-09-08T14" / "run_clips",
        "bbox_size": 70,
    },
    "prague_2026-09-10T11": {
        "clips_dir": REPO / "dance_decoder/yowo_data/prague_2026-09-10T11_1000_2000" / "run_clips",
        "bbox_size": 70,
    },
    "prague_2026-09-03T11": {
        "clips_dir": REPO / "dance_decoder/yowo_data/prague_2026-09-03T11_1000_2000" / "run_clips",
        "bbox_size": 70,
    }

}
DEFAULT_CLIPS_DIR = DATASETS[DEFAULT_DATASET]["clips_dir"]
# Compatibility for scripts that use the old list; no automatic path selection.
CLIPS_DIR_CANDIDATES = tuple(dataset["clips_dir"] for dataset in DATASETS.values())
DEFAULT_BBOX_SIZE = 70  # Folders opened via CLI/UI that are not listed above.


def bbox_size_for(clips_dir):
    source = Path(clips_dir).expanduser().resolve()
    size = next((dataset["bbox_size"] for dataset in DATASETS.values()
                 if Path(dataset["clips_dir"]).expanduser().resolve() == source),
                DEFAULT_BBOX_SIZE)
    if type(size) is not int or size < 1:
        raise ValueError("Dataset bbox_size must be a positive integer (clip pixels)")
    return size


ANNOTATIONS_DIR_NAME = "annotations"
MASTER_FILENAME = "master.json"
DETECTOR_DIR = REPO / "dance_decoder/candidate_run_detector"
ROSBAG_READER_PATH = DETECTOR_DIR / "rosbag_io.py"
IMAGE_TOPIC = "/dancinghive/image/compressed"
CAMERA_INFO_TOPIC = "/dancinghive/camera_info"


SERVER_HOST = "127.0.0.1"
ALLOWED_HOSTS = ("localhost", SERVER_HOST)
SERVER_PORT = 8765
JPEG_QUALITY = 100
MAX_CAPTURES = 3
CACHE_FRAMES = 64
CACHE_BYTES = 64 * 1024 * 1024
EXPORT_SPOOL_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# File-format constants: changing these requires matching migration/client changes.
SUPPORTED_CLIP_SCHEMA_VERSIONS = (1, 2)
MASTER_SCHEMA_VERSION = 5
COORDINATE_SPACE = {
    "bbox_clip": "clip pixels; [x_min, y_min, x_max, y_max]; max bounds exclusive",
    "bbox_original": "full-resolution undistorted image pixels; max bounds exclusive",
    "frame_bounds": "zero-based inclusive start_frame and end_frame",
}
LEGACY_ANGLE_CONVENTION = "degrees clockwise from image-right, y down; directed head angle, [0, 360)"
ANGLE_CONVENTION = "degrees clockwise from image-up; 0 up, 90 right, -90 left, -180 down; [-180, 180)"
STATUSES = frozenset({"unreviewed", "accepted", "rejected"})


EXPORT_SCHEMA_VERSION = 1
YOLO_CLASS_ID = 0
YOLO_CLASS_NAME = "waggle_bee"
ANNOTATION_API_VERSION = 5
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# Only this dictionary is sent to the browser. Never place private paths here.
UI = {
    "masterFilename": MASTER_FILENAME,
    "toastMs": 3500,
    "autosaveMs": 750,
    "downloadRevokeMs": 30000,
    "frameStep": 10,
    "boxHitRadius": 10,
    "combHitRadius": 20,
    "directionLength": 65,
    "combWidth": 1280,
    "combHeight": 960,
    "combGridSpacing": 100,
    "combMinDisplayWidth": 500,
    "danceNameMaxLength": 120,
    "uncertainColor": "#ff9e80",
    "keyframeColor": "#94efad",
    "interpolatedColor": "#ffd083",
    "directionColor": "#ffcd69",
    "dancePalette": ["#9ddeac", "#9fc9ff", "#edb2e3", "#ffd080", "#82e0d3", "#ffaca0"],
    "combBackground": "#111a14",
    "combDimOverlay": "rgba(0,0,0,.18)",
    "combGridColor": "#2c3b2e",
    "combGridTextColor": "#8eaa94",
    "selectedRunColor": "#ffe498",
    "ungroupedRunColor": "#9bebb0",
}

EXPORT_README = """YOLO detection export

images/ contains the exact cropped clip frames. labels/ contains matching
class_id center_x center_y width height rows, normalized to the crop dimensions.
Class 0 is waggle_bee: the single target bee annotated in each accepted run.
Other bees in the crop have not been exhaustively annotated.

The inclusive definite waggle interval is exported by default. Optional boundary
transitions are uncertain; definite non-waggle markers are never exported.
Rejected/unreviewed runs and held boxes are excluded. Uncertain runs/frames and
transitions are excluded unless explicitly selected. Interpolated boxes are included unless keyframes-only is set.

master.json preserves all source metadata, timing, orientation, direction,
uncertainty, dances, and original undistorted coordinates. manifest.json maps
each exported image back to its run, frame, and source/output timestamp.
Standard YOLO detection labels cannot represent those additional attributes.

This export does not create a train/validation split. Split by recording or dance,
not adjacent frames or overlapping crops, to avoid near-duplicate data leakage.
Repeated source timestamps represent camera frames repeated by the clip exporter.
"""
