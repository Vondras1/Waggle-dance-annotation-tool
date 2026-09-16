"""Annotation validation, interpolation, durable storage, and YOLO planning.

Frame bounds are inclusive. Boxes use pixel coordinates with exclusive maximum
bounds. Angles point toward the bee's head, clockwise from image-up (signed degrees).
The original coordinate space is the full-resolution *undistorted* source image;
these crops cannot recover distorted camera pixels without calibration.
"""

from bisect import bisect_left
from copy import deepcopy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import warnings
import uuid


from .config import (
    SUPPORTED_CLIP_SCHEMA_VERSIONS, MASTER_SCHEMA_VERSION, COORDINATE_SPACE, YOLO_CLASS_ID,
    LEGACY_ANGLE_CONVENTION, ANGLE_CONVENTION, STATUSES,
)


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _integer(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _string(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value


def _angle(value, name):
    return None if value is None else (_number(value, name) + 180.0) % 360.0 - 180.0


def validate_metadata(metadata):
    """Validate the mapping needed to annotate a clip, preserving all metadata."""
    if not isinstance(metadata, dict):
        raise ValueError("Clip metadata must be an object")
    # Both versions use the same geometry and frame-time mapping; preserve the
    # v2 encoding metadata along with the original schema version.
    if (type(metadata.get("schema_version")) is not int
            or metadata["schema_version"] not in SUPPORTED_CLIP_SCHEMA_VERSIONS):
        raise ValueError("Unsupported clip metadata schema_version")
    width = _integer(metadata.get("width"), "width", 1)
    height = _integer(metadata.get("height"), "height", 1)
    count = _integer(metadata.get("frame_count"), "frame_count", 1)
    if _number(metadata.get("fps"), "fps") <= 0:
        raise ValueError("fps must be positive")
    source = metadata.get("source")
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    for field in ("bag", "topic"):
        if not _string(source.get(field), f"source.{field}").strip():
            raise ValueError(f"source.{field} cannot be empty")
    run = metadata.get("run")
    if not isinstance(run, dict):
        raise ValueError("run must be an object")
    _integer(run.get("run_id"), "run.run_id", 0)
    start = _number(run.get("start_time_s"), "run.start_time_s")
    end = _number(run.get("end_time_s"), "run.end_time_s")
    if start < 0 or end < start:
        raise ValueError("Detector run times must be nonnegative and ordered")
    bounds = [_integer(run.get(f"bbox_{axis}_{edge}_px"), f"run.bbox_{axis}_{edge}_px")
              for axis, edge in (("x", "min"), ("y", "min"), ("x", "max"), ("y", "max"))]
    if bounds[2] - bounds[0] != width or bounds[3] - bounds[1] != height:
        raise ValueError("Crop bounds must match the clip width and height")
    for field in ("output_frame_times_s", "source_frame_times_s"):
        values = metadata.get(field)
        if not isinstance(values, list) or len(values) != count:
            raise ValueError(f"{field} must contain frame_count ({count}) timestamps")
        times = [_number(value, f"{field}[{index}]") for index, value in enumerate(values)]
        if any(value < 0 for value in times):
            raise ValueError(f"{field} must be nonnegative")
        # The fixed-FPS exporter can duplicate a source frame, never an output time.
        ordered = all(a < b if field == "output_frame_times_s" else a <= b
                      for a, b in zip(times, times[1:]))
        if not ordered:
            raise ValueError(f"{field} must be {'strictly increasing' if field == 'output_frame_times_s' else 'nondecreasing'}")
    for field, index in (("clip_start_time_s", 0), ("clip_end_time_s", -1)):
        if field in metadata:
            time_s = _number(metadata[field], field)
            if not math.isclose(time_s, metadata["output_frame_times_s"][index], abs_tol=1e-6):
                raise ValueError(f"{field} does not match output_frame_times_s")
    return metadata


def discover_clips(clips_dir):
    """Read safe JSON/MP4 pairs; warn about malformed candidates.

    Mixed source bags/topics fail as a whole: dance coordinates and timestamps
    only have meaning within one source session.
    """
    root = Path(clips_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Clip directory does not exist: {root}")
    clips = {}
    for path in sorted(root.glob("*.json")):
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("Metadata path escapes the clip directory")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", path.stem):
                raise ValueError("Clip ID contains unsupported characters")
            metadata = json.loads(path.read_text(encoding="utf-8"))
            validate_metadata(metadata)
            video_file = _string(metadata.get("video_file"), "video_file")
            if Path(video_file).is_absolute():
                raise ValueError("video_file must be relative to the clip directory")
            video_path = (root / video_file).resolve()
            if not video_path.is_relative_to(root):
                raise ValueError("video_file escapes the clip directory")
            if video_path.suffix.lower() != ".mp4" or not video_path.is_file():
                raise ValueError(f"Missing MP4 video: {video_file}")
            clips[path.stem] = {"id": path.stem, "metadata": metadata, "video_path": video_path}
        except (ValueError, OSError, TypeError) as exc:
            warnings.warn(f"Skipping clip {path.name}: {exc}", stacklevel=2)
    sessions = {(clip["metadata"]["source"]["bag"], clip["metadata"]["source"]["topic"])
                for clip in clips.values()}
    if len(sessions) > 1:
        raise ValueError("Clip directory mixes source bags or image topics; use one session per annotation project")
    return clips


def default_annotation(metadata):
    validate_metadata(metadata)
    times = metadata["source_frame_times_s"]
    run = metadata["run"]
    nearest = lambda time_s: min(range(len(times)), key=lambda index: abs(times[index] - time_s))
    return {
        "start_frame": nearest(run["start_time_s"]),
        "end_frame": nearest(run["end_time_s"]),
        "status": "unreviewed",
        "uncertain": False,
        "notes": "",
        "direction_deg": None,
        "keyframes": [],
        "excluded_frames": [],
    }


def validate_annotation(annotation, metadata):
    """Return a normalized editable annotation or raise a useful ValueError."""
    validate_metadata(metadata)
    if not isinstance(annotation, dict):
        raise ValueError("annotation must be an object")
    start = _integer(annotation.get("start_frame"), "start_frame", 0)
    end = _integer(annotation.get("end_frame"), "end_frame", 0)
    if start > end or end >= metadata["frame_count"]:
        raise ValueError("Run frame bounds must be ordered and inside the clip (inclusive)")
    status = annotation.get("status", "unreviewed")
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("status must be unreviewed, accepted, or rejected")
    keyframes = annotation.get("keyframes", [])
    if not isinstance(keyframes, list):
        raise ValueError("keyframes must be a list")
    normalized = []
    seen = set()
    for keyframe in keyframes:
        if not isinstance(keyframe, dict):
            raise ValueError("Each keyframe must be an object")
        frame = _integer(keyframe.get("frame"), "keyframe.frame", 0)
        if frame >= metadata["frame_count"]:
            raise ValueError(f"Keyframe {frame} is outside the clip")
        if frame in seen:
            raise ValueError(f"Duplicate keyframe at frame {frame}")
        seen.add(frame)
        bbox = keyframe.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"Keyframe {frame} bbox must contain [x_min, y_min, x_max, y_max]")
        x0, y0, x1, y1 = [_number(value, f"keyframe {frame} bbox") for value in bbox]
        if not (0 <= x0 < x1 <= metadata["width"] and 0 <= y0 < y1 <= metadata["height"]):
            raise ValueError(f"Keyframe {frame} box must have positive area and stay inside the clip")
        normalized.append({
            "frame": frame,
            "bbox": [x0, y0, x1, y1],
            "orientation_deg": _angle(keyframe.get("orientation_deg"), "orientation_deg"),
            "uncertain": _boolean(keyframe.get("uncertain", False), "keyframe.uncertain"),
        })
    excluded = annotation.get("excluded_frames", [])
    if not isinstance(excluded, list):
        raise ValueError("excluded_frames must be a list of frame indices")
    for frame in excluded:
        _integer(frame, "excluded frame", 0)
        if frame >= metadata["frame_count"]:
            raise ValueError("Excluded frame is outside the clip")
    excluded = sorted(set(excluded))
    if any(key["frame"] in excluded for key in normalized):
        raise ValueError("A frame cannot contain both a box keyframe and a deleted-box marker")
    normalized.sort(key=lambda keyframe: keyframe["frame"])
    active = [frame for frame in range(start, end + 1) if frame not in excluded]
    if status == "accepted" and (not active or not normalized or normalized[0]["frame"] > active[0] or normalized[-1]["frame"] < active[-1]):
        raise ValueError("Accepted runs need box keyframes at or before the start and at or after the end; add endpoint boxes first")
    return {
        "start_frame": start,
        "end_frame": end,
        "status": status,
        "uncertain": _boolean(annotation.get("uncertain", False), "uncertain"),
        "notes": _string(annotation.get("notes", ""), "notes"),
        "direction_deg": _angle(annotation.get("direction_deg"), "direction_deg"),
        "keyframes": normalized,
        "excluded_frames": excluded,
    }


def materialize(annotation, metadata):
    """Interpolate each included frame, keeping measured source times intact.

    Missing orientation at either anchor yields unknown orientation between the
    anchors. Uncertainty propagates from either neighboring keyframe or the run.
    Preview holds outside the keyed interval are marked and never YOLO-exported.
    """
    annotation = validate_annotation(annotation, metadata)
    keys = annotation["keyframes"]
    if not keys or annotation["status"] == "rejected":
        return []
    key_indices = [key["frame"] for key in keys]
    offset_x = metadata["run"]["bbox_x_min_px"]
    offset_y = metadata["run"]["bbox_y_min_px"]
    result = []
    for frame in range(annotation["start_frame"], annotation["end_frame"] + 1):
        if frame in annotation["excluded_frames"]:
            continue
        right_index = bisect_left(key_indices, frame)
        if right_index < len(keys) and keys[right_index]["frame"] == frame:
            left = right = keys[right_index]
            provenance = "keyframe"
        elif right_index == 0 or right_index == len(keys):
            left = right = keys[0 if right_index == 0 else -1]
            provenance = "held"
        else:
            left, right = keys[right_index - 1], keys[right_index]
            provenance = "interpolated"
        ratio = 0.0 if left is right else (frame - left["frame"]) / (right["frame"] - left["frame"])
        box = [a + (b - a) * ratio for a, b in zip(left["bbox"], right["bbox"])]
        angle_a, angle_b = left["orientation_deg"], right["orientation_deg"]
        if angle_a is None or angle_b is None:
            orientation = None
        else:
            shortest_delta = (angle_b - angle_a + 180.0) % 360.0 - 180.0
            orientation = _angle(angle_a + shortest_delta * ratio, "orientation_deg")
        result.append({
            "frame": frame,
            "output_time_s": metadata["output_frame_times_s"][frame],
            "source_time_s": metadata["source_frame_times_s"][frame],
            "bbox_clip": box,
            "bbox_original": [box[0] + offset_x, box[1] + offset_y, box[2] + offset_x, box[3] + offset_y],
            "orientation_deg": orientation,
            "uncertain": annotation["uncertain"] or left["uncertain"] or right["uncertain"],
            "provenance": provenance,
        })
    return result


def _validate_dances(dances, clips):
    if not isinstance(dances, list):
        raise ValueError("dances must be a list")
    result, ids, memberships = [], set(), set()
    for dance in dances:
        if not isinstance(dance, dict):
            raise ValueError("Each dance must be an object")
        dance_id = _string(dance.get("id"), "dance.id")
        if not dance_id.strip() or dance_id in ids:
            raise ValueError("Dance IDs must be nonempty and unique")
        ids.add(dance_id)
        run_ids = dance.get("run_ids")
        if not isinstance(run_ids, list) or not run_ids:
            raise ValueError(f"Dance {dance_id} must contain at least one accepted run")
        for run_id in run_ids:
            if not isinstance(run_id, str) or run_id not in clips:
                raise ValueError(f"Dance {dance_id} references an unknown run: {run_id}")
            if clips[run_id]["annotation"]["status"] != "accepted":
                raise ValueError(f"Run {run_id} belongs to a dance and must remain accepted; remove it from the dance first")
            if run_id in memberships:
                raise ValueError(f"Run {run_id} can belong to only one dance and appear only once")
            memberships.add(run_id)
        result.append({
            "id": dance_id,
            "name": _string(dance.get("name", ""), "dance.name"),
            "run_ids": list(run_ids),
            "notes": _string(dance.get("notes", ""), "dance.notes"),
            "uncertain": _boolean(dance.get("uncertain", False), "dance.uncertain"),
        })
    return result


class MasterStore:
    """Atomic JSON storage with revision checks across browser tabs and processes."""

    def __init__(self, path, clips):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._legacy_backup = None
        self._backup_kind = "v1-angle"
        self._master = {
            "schema_version": MASTER_SCHEMA_VERSION,
            "coordinate_space": deepcopy(COORDINATE_SPACE),
            "angle_convention": ANGLE_CONVENTION,
            "clips": {},
            "dances": [],
            "revision": 0,
        }
        if self.path.exists():
            try:
                original_bytes = self.path.read_bytes()
                saved = json.loads(original_bytes)
            except (ValueError, OSError) as exc:
                raise ValueError(f"Cannot load master annotations at {self.path}: {exc}") from exc
            if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int
                    or saved["schema_version"] not in (1, 2, 3, MASTER_SCHEMA_VERSION)):
                raise ValueError("Unsupported master annotation schema_version")
            if saved["schema_version"] == 1 and saved.get("angle_convention") == LEGACY_ANGLE_CONVENTION:
                if not isinstance(saved.get("clips"), dict):
                    raise ValueError("Master clips must be an object")
                # Preserve physical directions: old right=0 becomes new right=90.
                self._legacy_backup = original_bytes
                for entry in saved.get("clips", {}).values():
                    annotation = entry["annotation"]
                    value = annotation.get("direction_deg")
                    annotation["direction_deg"] = None if value is None else _angle(value + 90, "direction_deg")
                    for key in annotation.get("keyframes", []):
                        value = key.get("orientation_deg")
                        key["orientation_deg"] = None if value is None else _angle(value + 90, "orientation_deg")
                saved["schema_version"] = MASTER_SCHEMA_VERSION
                saved["angle_convention"] = ANGLE_CONVENTION
            if saved["schema_version"] == 2:
                self._legacy_backup = original_bytes
                self._backup_kind = "v2-multirun"
                saved["schema_version"] = MASTER_SCHEMA_VERSION
            if saved["schema_version"] == 3:
                self._legacy_backup = original_bytes
                self._backup_kind = "v3-box-exclusions"
                saved["schema_version"] = MASTER_SCHEMA_VERSION
            if saved.get("coordinate_space") != COORDINATE_SPACE or saved.get("angle_convention") != ANGLE_CONVENTION:
                raise ValueError("Master coordinate space or angle convention differs from this tool")
            if not isinstance(saved.get("clips"), dict):
                raise ValueError("Master clips must be an object")
            _integer(saved.get("revision"), "revision", 0)
            sources = {entry.get("source_clip_id", run_id) for run_id, entry in saved["clips"].items()}
            missing = sources - set(clips)
            if missing:
                raise ValueError(f"Saved clips are missing or invalid in the selected directory: {', '.join(sorted(missing))}")
            self._master = deepcopy(saved)
        for clip_id in dict.fromkeys([*clips, *self._master["clips"]]):
            stored = self._master["clips"].get(clip_id)
            source_id = stored.get("source_clip_id", clip_id) if stored else clip_id
            metadata = validate_metadata(clips[source_id]["metadata"])
            if stored is None:
                annotation = default_annotation(metadata)
            else:
                if not isinstance(stored, dict) or stored.get("metadata") != metadata:
                    raise ValueError(f"Source metadata changed for {clip_id}; use a separate master file or restore its original sidecar")
                annotation = validate_annotation(stored.get("annotation"), metadata)
            run_number = _integer(stored.get("run_number", 1), "run_number", 1) if stored else 1
            self._master["clips"][clip_id] = {
                "source_clip_id": source_id, "run_number": run_number,
                "metadata": deepcopy(metadata), "annotation": annotation,
                "frames": materialize(annotation, metadata),
            }
        self._master["dances"] = _validate_dances(self._master.get("dances", []), self._master["clips"])

    def get_master(self):
        with self._lock:
            return deepcopy(self._master)

    def _check_revision(self, expected_revision):
        if expected_revision is not None:
            _integer(expected_revision, "expected_revision", 0)
            if expected_revision != self._master["revision"]:
                raise ValueError("Revision conflict: annotations changed in another tab; reload before saving")

    def _commit(self, candidate):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unix advisory locking uses a persistent sidecar: locking the master
        # itself would lose protection when the atomic rename replaces its inode.
        # Never unlink the lock file, including after a failed save.
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                self._check_disk_revision()
                return self._commit_locked(candidate)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def copy_to(self, path, clips):
        """Save this snapshot to a new destination without replacing an existing project."""
        with self._lock:
            self._check_disk_revision()
            destination = MasterStore(path, clips)
            candidate = self.get_master()
            discovered = destination.get_master()["clips"]
            for clip_id, entry in candidate["clips"].items():
                source_id = entry.get("source_clip_id", clip_id)
                if source_id not in discovered or entry["metadata"] != discovered[source_id]["metadata"]:
                    raise ValueError(f"Source metadata changed for {clip_id}; reload before copying annotations")
            for clip_id, entry in discovered.items():
                candidate["clips"].setdefault(clip_id, entry)
            destination._require_new_file = True
            destination._commit(candidate)
            return destination

    def _check_disk_revision(self):
        """Check under the process lock so a stale server cannot replace a save."""
        if getattr(self, "_require_new_file", False) and self.path.exists():
            raise ValueError("A master file already exists in the save folder; reopen it instead of replacing it")
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if self._master["revision"] == 0:
                return
            raise ValueError("Master annotation file was removed; restore it and reload before saving") from None
        except ValueError as exc:
            raise ValueError("Master annotation file is invalid; repair it and reload before saving") from exc
        if (not isinstance(saved, dict) or type(saved.get("revision")) is not int
                or saved["revision"] != self._master["revision"]):
            raise ValueError("Revision conflict: annotations changed in another server; reload before saving")

    def _commit_locked(self, candidate):
        if self._legacy_backup is not None:
            backup = self.path.with_name(f"{self.path.name}.{self._backup_kind}-backup-r{self._master['revision']}.json")
            try:
                with backup.open("xb") as handle:
                    handle.write(self._legacy_backup)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if backup.read_bytes() != self._legacy_backup:
                    raise ValueError(f"Migration backup already exists with different contents: {backup}")
        candidate["revision"] = self._master["revision"] + 1
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump(candidate, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            self._master = candidate
            self._legacy_backup = None
            self._require_new_file = False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return deepcopy(self._master)

    def save_run(self, clip_id, annotation, expected_revision=None):
        with self._lock:
            self._check_revision(expected_revision)
            if clip_id not in self._master["clips"]:
                raise ValueError(f"Unknown clip: {clip_id}")
            candidate = deepcopy(self._master)
            clip = candidate["clips"][clip_id]
            clip["annotation"] = validate_annotation(annotation, clip["metadata"])
            clip["frames"] = materialize(clip["annotation"], clip["metadata"])
            _validate_dances(candidate["dances"], candidate["clips"])
            return self._commit(candidate)

    def add_run(self, clip_id, frame, expected_revision=None):
        with self._lock:
            self._check_revision(expected_revision)
            if clip_id not in self._master["clips"]:
                raise ValueError("Unknown clip")
            source = self._master["clips"][clip_id]["source_clip_id"]
            original = self._master["clips"][source]
            frame = _integer(frame, "frame", 0)
            count = original["metadata"]["frame_count"]
            if frame >= count:
                raise ValueError("Frame is outside the clip")
            siblings = [c for c in self._master["clips"].values() if c["source_clip_id"] == source]
            annotation = default_annotation(original["metadata"])
            annotation.update(start_frame=frame, end_frame=frame)
            run_id = source + "__run_" + uuid.uuid4().hex[:12]
            candidate = deepcopy(self._master)
            candidate["clips"][run_id] = {
                "source_clip_id": source, "run_number": max(c["run_number"] for c in siblings) + 1,
                "metadata": deepcopy(original["metadata"]), "annotation": annotation, "frames": [],
            }
            saved = self._commit(candidate)
            return run_id, saved

    def delete_run(self, run_id, expected_revision=None):
        with self._lock:
            self._check_revision(expected_revision)
            entry = self._master["clips"].get(run_id)
            if entry is None or entry["source_clip_id"] == run_id:
                raise ValueError("The original candidate stays in the clip; reject it instead of deleting it")
            if any(run_id in dance["run_ids"] for dance in self._master["dances"]):
                raise ValueError("Remove this run from its dance before deleting it")
            candidate = deepcopy(self._master)
            del candidate["clips"][run_id]
            return self._commit(candidate)

    def save_dances(self, dances, expected_revision=None):
        with self._lock:
            self._check_revision(expected_revision)
            candidate = deepcopy(self._master)
            candidate["dances"] = _validate_dances(dances, candidate["clips"])
            return self._commit(candidate)


def yolo_rows(master, include_uncertain=False, keyframes_only=False, stride=1):
    """Plan one bee label per accepted crop frame, class 0, normalized to crop.

    Stride counts from the annotated interval's start, before filtering. Source
    timestamps and orientation remain in the master/manifest, since ordinary
    YOLO detection labels have no fields for them.
    """
    _integer(stride, "stride", 1)
    _boolean(include_uncertain, "include_uncertain")
    _boolean(keyframes_only, "keyframes_only")
    rows = []
    for clip_id, clip in sorted(master["clips"].items()):
        metadata = clip["metadata"]
        annotation = validate_annotation(clip["annotation"], metadata)
        if annotation["status"] != "accepted":
            continue
        for frame in materialize(annotation, metadata):
            if (frame["frame"] - annotation["start_frame"]) % stride:
                continue
            if frame["provenance"] == "held" or (keyframes_only and frame["provenance"] != "keyframe"):
                continue
            if frame["uncertain"] and not include_uncertain:
                continue
            x0, y0, x1, y1 = frame["bbox_clip"]
            width, height = metadata["width"], metadata["height"]
            values = ((x0 + x1) / (2 * width), (y0 + y1) / (2 * height),
                      (x1 - x0) / width, (y1 - y0) / height)
            label = f"{YOLO_CLASS_ID} " + " ".join(f"{value:.10f}" for value in values) + "\n"
            rows.append({"clip_id": clip_id, "frame": frame["frame"], "label": label, "annotation": frame, "source_clip_id": clip.get("source_clip_id", clip_id)})
    return rows
