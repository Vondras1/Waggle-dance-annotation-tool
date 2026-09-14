"""Geometry, annotation integrity, and durable storage regression tests."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings

from annotation_tool.model import (
    ANGLE_CONVENTION, LEGACY_ANGLE_CONVENTION, MasterStore, default_annotation, discover_clips, materialize,
    validate_annotation, validate_metadata, yolo_rows,
)


def metadata():
    return {
        "schema_version": 1,
        "video_file": "run_000001.mp4",
        "source": {"bag": "/recording", "topic": "/images", "camera_info_topic": "/camera"},
        "run": {"run_id": 1, "start_time_s": 1.0, "end_time_s": 1.12,
                "bbox_x_min_px": -5, "bbox_y_min_px": 200,
                "bbox_x_max_px": 95, "bbox_y_max_px": 280},
        "coordinate_space": "full-resolution undistorted image; max bounds exclusive",
        "fps": 25.0, "width": 100, "height": 80, "frame_count": 5,
        "clip_start_time_s": 1.0, "clip_end_time_s": 1.16,
        "output_frame_times_s": [1.0, 1.04, 1.08, 1.12, 1.16],
        "source_frame_times_s": [1.0, 1.0, 1.04, 1.08, 1.12],
        "extra_source_field": {"keep": [1, 2, 3]},
    }


def keyframe(frame, box=None, angle=0.0, uncertain=False):
    return {"frame": frame, "bbox": box or [10, 20, 30, 40],
            "orientation_deg": angle, "uncertain": uncertain}


def accepted():
    annotation = default_annotation(metadata())
    annotation.update(status="accepted", keyframes=[
        keyframe(0, [10, 20, 30, 40], 350), keyframe(4, [30, 40, 50, 60], 10),
    ])
    return annotation


class InterpolationTests(unittest.TestCase):
    def test_default_bounds_use_actual_source_frames(self):
        annotation = default_annotation(metadata())
        self.assertEqual((annotation["start_frame"], annotation["end_frame"]), (0, 4))
        self.assertEqual(materialize(annotation, metadata()), [])

    def test_interpolation_geometry_angles_and_source_times(self):
        frames = materialize(accepted(), metadata())
        self.assertEqual(len(frames), 5)
        self.assertEqual(frames[2]["bbox_clip"], [20, 30, 40, 50])
        self.assertEqual(frames[2]["bbox_original"], [15, 230, 35, 250])
        self.assertEqual(frames[2]["orientation_deg"], 0)
        self.assertEqual(frames[1]["orientation_deg"], -5)
        self.assertEqual(frames[0]["provenance"], "keyframe")
        self.assertEqual(frames[2]["provenance"], "interpolated")
        self.assertEqual(frames[1]["output_time_s"], 1.04)
        self.assertEqual(frames[1]["source_time_s"], 1.0)

    def test_uncertainty_and_missing_orientation_propagate(self):
        annotation = accepted()
        annotation["keyframes"][1].update(uncertain=True, orientation_deg=None)
        frames = materialize(annotation, metadata())
        self.assertFalse(frames[0]["uncertain"])
        self.assertTrue(all(frame["uncertain"] for frame in frames[1:]))
        self.assertEqual(frames[0]["orientation_deg"], -10)
        self.assertTrue(all(frame["orientation_deg"] is None for frame in frames[1:]))
        annotation["uncertain"] = True
        self.assertTrue(all(frame["uncertain"] for frame in materialize(annotation, metadata())))

    def test_draft_holds_and_accepted_endpoint_coverage(self):
        annotation = default_annotation(metadata())
        annotation["keyframes"] = [keyframe(2)]
        self.assertEqual([frame["provenance"] for frame in materialize(annotation, metadata())],
                         ["held", "held", "keyframe", "held", "held"])
        annotation["status"] = "accepted"
        with self.assertRaisesRegex(ValueError, "endpoint"):
            validate_annotation(annotation, metadata())
        annotation.update(start_frame=2, end_frame=2)
        self.assertEqual(len(materialize(annotation, metadata())), 1)

    def test_keyframes_outside_run_can_anchor_interval(self):
        annotation = accepted()
        annotation.update(start_frame=1, end_frame=3)
        frames = materialize(annotation, metadata())
        self.assertEqual([frame["frame"] for frame in frames], [1, 2, 3])
        self.assertTrue(all(frame["provenance"] == "interpolated" for frame in frames))

    def test_validation_rejects_invalid_values(self):
        invalid_updates = [
            {"start_frame": True}, {"end_frame": 5}, {"end_frame": -1},
            {"status": "done"}, {"uncertain": "false"}, {"notes": 123},
            {"direction_deg": float("nan")},
            {"keyframes": [keyframe(0), keyframe(0)]},
            {"keyframes": [keyframe(5)]},
            {"keyframes": [keyframe(0, [-1, 0, 1, 1])]},
            {"keyframes": [keyframe(0, [1, 0, 1, 1])]},
            {"keyframes": [keyframe(0, [0, 0, float("inf"), 1])]},
        ]
        for update in invalid_updates:
            with self.subTest(update=update):
                annotation = default_annotation(metadata())
                annotation.update(update)
                with self.assertRaises(ValueError):
                    validate_annotation(annotation, metadata())

    def test_signed_wrap_across_down_and_neutral_frames(self):
        a = accepted()
        a["keyframes"][0]["orientation_deg"] = 170
        a["keyframes"][1]["orientation_deg"] = -170
        self.assertEqual(materialize(a, metadata())[2]["orientation_deg"], -180)
        a.update(start_frame=1, end_frame=3)
        self.assertEqual([f["frame"] for f in materialize(a, metadata())], [1, 2, 3])
        a["status"] = "rejected"
        self.assertEqual(materialize(a, metadata()), [])

    def test_deleted_frame_stays_empty_and_is_not_exported(self):
        a = accepted()
        a["excluded_frames"] = [2]
        frames = materialize(a, metadata())
        self.assertEqual([f["frame"] for f in frames], [0, 1, 3, 4])
        master = {"clips": {"run1": {"metadata": metadata(), "annotation": a}}}
        self.assertEqual([r["frame"] for r in yolo_rows(master)], [0, 1, 3, 4])
        a["keyframes"].append(keyframe(2))
        with self.assertRaisesRegex(ValueError, "deleted-box marker"):
            validate_annotation(a, metadata())
        a["excluded_frames"] = []
        self.assertEqual(len(materialize(a, metadata())), 5)

    def test_deleted_endpoint_coverage_and_invalid_markers(self):
        a = accepted()
        a["keyframes"] = [keyframe(1), keyframe(4)]
        a["excluded_frames"] = [0]
        self.assertEqual([f["frame"] for f in materialize(a, metadata())], [1, 2, 3, 4])
        for value in ([True], [-1], [5], "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_annotation({**a, "excluded_frames": value}, metadata())

    def test_angle_normalization_and_sorted_keys(self):
        annotation = accepted()
        annotation["direction_deg"] = -90
        annotation["keyframes"].reverse()
        annotation["keyframes"][0]["orientation_deg"] = 370
        result = validate_annotation(annotation, metadata())
        self.assertEqual(result["direction_deg"], -90)
        self.assertEqual([key["frame"] for key in result["keyframes"]], [0, 4])
        self.assertEqual(result["keyframes"][1]["orientation_deg"], 10)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_clip(self, stem="run_000001", meta=None):
        meta = deepcopy(metadata() if meta is None else meta)
        (self.root / f"{stem}.json").write_text(json.dumps(meta), encoding="utf-8")
        (self.root / f"{stem}.mp4").write_bytes(b"fixture-video")

    def test_discovery_preserves_all_metadata(self):
        self.write_clip()
        result = discover_clips(self.root)
        self.assertEqual(result["run_000001"]["metadata"], metadata())
        self.assertEqual(result["run_000001"]["video_path"], self.root / "run_000001.mp4")

    def test_bad_json_bad_times_and_path_escape_are_warned(self):
        self.write_clip()
        (self.root / "bad.json").write_text("{")
        invalid = metadata()
        invalid["source_frame_times_s"] = [1, 0, 1, 1, 1]
        self.write_clip("bad_times", invalid)
        escape = metadata()
        escape["video_file"] = "../outside.mp4"
        self.write_clip("escape", escape)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = discover_clips(self.root)
        self.assertEqual(list(result), ["run_000001"])
        self.assertEqual(len(caught), 3)
        self.assertTrue(any("escapes" in str(warning.message) for warning in caught))

    def test_different_sources_rejected(self):
        self.write_clip()
        other = metadata()
        other["source"]["bag"] = "/other"
        other["video_file"] = "run_000002.mp4"
        self.write_clip("run_000002", other)
        with self.assertRaisesRegex(ValueError, "mixes source"):
            discover_clips(self.root)

    def test_crop_dimensions_and_timestamp_lengths_validated(self):
        for field, value in (("width", 200), ("frame_count", 6), ("fps", 0),
                             ("output_frame_times_s", [1, 1, 1.08, 1.12, 1.16])):
            with self.subTest(field=field):
                invalid = metadata()
                invalid[field] = value
                with self.assertRaises(ValueError):
                    validate_metadata(invalid)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "annotations.json"
        self.clips = {"run_000001": {"id": "run_000001", "metadata": metadata(),
                                     "video_path": Path("run_000001.mp4")}}
        self.store = MasterStore(self.path, self.clips)

    def test_roundtrip_full_metadata_and_detached_snapshots(self):
        saved = self.store.save_run("run_000001", accepted(), expected_revision=0)
        restored = MasterStore(self.path, self.clips).get_master()
        self.assertEqual(restored, saved)
        self.assertEqual(restored["clips"]["run_000001"]["metadata"], metadata())
        saved["clips"].clear()
        self.assertEqual(len(self.store.get_master()["clips"]), 1)

    def test_legacy_angles_migrate_once_and_original_is_backed_up_on_save(self):
        old = self.store.save_run("run_000001", accepted(), expected_revision=0)
        old["schema_version"] = 1
        old["angle_convention"] = LEGACY_ANGLE_CONVENTION
        old["clips"]["run_000001"]["annotation"]["direction_deg"] = 270
        old["clips"]["run_000001"]["annotation"]["keyframes"][0]["orientation_deg"] = 350
        original = json.dumps(old).encode()
        self.path.write_bytes(original)
        migrated = MasterStore(self.path, self.clips)
        master = migrated.get_master()
        a = master["clips"]["run_000001"]["annotation"]
        self.assertEqual(master["schema_version"], 4)
        self.assertEqual(master["angle_convention"], ANGLE_CONVENTION)
        self.assertEqual(a["direction_deg"], 0)
        self.assertEqual(a["keyframes"][0]["orientation_deg"], 80)
        self.assertEqual(self.path.read_bytes(), original)
        saved = migrated.save_run("run_000001", a, expected_revision=1)
        backup = self.path.with_name(self.path.name + ".v1-angle-backup-r1.json")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(MasterStore(self.path, self.clips).get_master(), saved)

    def test_version_two_migration_and_run_allocation(self):
        a = accepted()
        a["end_frame"] = 2
        old = self.store.save_run("run_000001", a, expected_revision=0)
        old["schema_version"] = 2
        old["clips"]["run_000001"].pop("source_clip_id")
        old["clips"]["run_000001"].pop("run_number")
        raw = json.dumps(old).encode();self.path.write_bytes(raw)
        migrated = MasterStore(self.path, self.clips)
        run_id, saved = migrated.add_run("run_000001", 1, expected_revision=1)
        self.assertEqual(saved["clips"][run_id]["annotation"]["start_frame"], 1)
        self.assertEqual(saved["clips"][run_id]["source_clip_id"], "run_000001")
        backup = self.path.with_name(self.path.name + ".v2-multirun-backup-r1.json")
        self.assertEqual(backup.read_bytes(), raw)
        self.assertEqual(MasterStore(self.path, self.clips).get_master(), saved)
        third_id, _ = migrated.add_run(run_id, 1, expected_revision=2)
        self.assertEqual(migrated.get_master()["clips"][third_id]["run_number"], 3)
        fourth_id, _ = migrated.add_run(run_id, 0, expected_revision=3)
        self.assertEqual(migrated.get_master()["clips"][fourth_id]["annotation"]["start_frame"], 0)

    def test_deleted_boxes_survive_save_reload_and_schema_three_upgrade(self):
        old = self.store.save_run("run_000001", accepted(), expected_revision=0)
        old["schema_version"] = 3
        old["clips"]["run_000001"]["annotation"].pop("excluded_frames")
        raw = json.dumps(old).encode();self.path.write_bytes(raw)
        store = MasterStore(self.path, self.clips)
        a = store.get_master()["clips"]["run_000001"]["annotation"]
        a["excluded_frames"] = [2]
        saved = store.save_run("run_000001", a, expected_revision=1)
        self.assertEqual(saved["schema_version"], 4)
        self.assertEqual(MasterStore(self.path, self.clips).get_master(), saved)
        self.assertEqual(self.path.with_name(self.path.name+".v3-box-exclusions-backup-r1.json").read_bytes(), raw)

    def test_revision_conflict_keeps_previous_save(self):
        saved = self.store.save_run("run_000001", accepted(), expected_revision=0)
        with self.assertRaisesRegex(ValueError, "Revision conflict"):
            self.store.save_run("run_000001", accepted(), expected_revision=0)
        self.assertEqual(self.store.get_master(), saved)

    def test_independent_stale_store_cannot_overwrite_new_save(self):
        stale = MasterStore(self.path, self.clips)
        original_stale_state = stale.get_master()
        first_annotation = accepted()
        first_annotation["notes"] = "Keep the first server's saved annotation"
        saved = self.store.save_run("run_000001", first_annotation, expected_revision=0)
        original_disk = self.path.read_bytes()
        second_annotation = accepted()
        second_annotation["notes"] = "This stale save must be rejected"
        with self.assertRaisesRegex(ValueError, "another server; reload"):
            stale.save_run("run_000001", second_annotation, expected_revision=0)
        self.assertEqual(self.path.read_bytes(), original_disk)
        self.assertEqual(stale.get_master(), original_stale_state)
        self.assertEqual(MasterStore(self.path, self.clips).get_master(), saved)

    def test_removed_saved_master_is_not_silently_recreated(self):
        saved = self.store.save_run("run_000001", accepted())
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "removed; restore it and reload"):
            self.store.save_run("run_000001", accepted())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.get_master(), saved)

    def test_invalid_disk_master_is_not_overwritten(self):
        saved = self.store.save_run("run_000001", accepted())
        self.path.write_text("{broken JSON", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid; repair it and reload"):
            self.store.save_run("run_000001", accepted())
        self.assertEqual(self.path.read_text(), "{broken JSON")
        self.assertEqual(self.store.get_master(), saved)

    def test_independent_simultaneous_stores_only_one_save_succeeds(self):
        stores = [self.store, MasterStore(self.path, self.clips)]
        barrier = threading.Barrier(2)
        outcomes = []
        def save(store):
            barrier.wait()
            try:
                store.save_run("run_000001", accepted(), expected_revision=0)
                outcomes.append("saved")
            except ValueError:
                outcomes.append("conflict")
        threads = [threading.Thread(target=save, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["saved", "conflict"])
        self.assertEqual(json.loads(self.path.read_text())["revision"], 1)

    def test_separate_process_cannot_overwrite_a_new_save(self):
        script = """
import sys
from pathlib import Path
from annotation_tool.model import MasterStore
from annotation_tool.test_model import accepted, metadata
clips = {'run_000001': {'metadata': metadata(), 'video_path': Path('run_000001.mp4')}}
store = MasterStore(sys.argv[1], clips)
before = store.get_master()
print('ready', flush=True)
sys.stdin.readline()
try:
    store.save_run('run_000001', accepted(), expected_revision=0)
except ValueError:
    assert store.get_master() == before
    print('conflict', flush=True)
else:
    print('overwritten', flush=True)
"""
        with subprocess.Popen([sys.executable, "-c", script, str(self.path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            saved = self.store.save_run("run_000001", accepted(), expected_revision=0)
            output, error = process.communicate("save now\n", timeout=10)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(output.strip(), "conflict")
        self.assertEqual(json.loads(self.path.read_text()), saved)

    def test_simultaneous_tabs_only_one_save_succeeds(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def save():
            barrier.wait()
            try:
                self.store.save_run("run_000001", accepted(), expected_revision=0)
                outcomes.append("saved")
            except ValueError:
                outcomes.append("conflict")
        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["saved", "conflict"])

    def test_failed_atomic_replace_preserves_disk_and_memory(self):
        saved = self.store.save_run("run_000001", accepted())
        disk = self.path.read_bytes()
        with patch("annotation_tool.model.os.replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.store.save_run("run_000001", accepted())
        self.assertEqual(self.path.read_bytes(), disk)
        self.assertEqual(self.store.get_master(), saved)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_metadata_changes_and_missing_saved_clips_fail(self):
        self.store.save_run("run_000001", accepted())
        changed = deepcopy(self.clips)
        changed["run_000001"]["metadata"]["source"]["bag"] = "/other"
        with self.assertRaisesRegex(ValueError, "metadata changed"):
            MasterStore(self.path, changed)
        with self.assertRaisesRegex(ValueError, "missing or invalid"):
            MasterStore(self.path, {})

    def test_new_clips_join_saved_project(self):
        saved = self.store.save_run("run_000001", accepted())
        expanded = deepcopy(self.clips)
        expanded["run_000002"] = deepcopy(expanded["run_000001"])
        expanded["run_000002"]["metadata"]["run"]["run_id"] = 2
        master = MasterStore(self.path, expanded).get_master()
        self.assertEqual(master["clips"]["run_000001"], saved["clips"]["run_000001"])
        self.assertEqual(master["clips"]["run_000002"]["annotation"]["status"], "unreviewed")

    def test_dance_membership_requires_unique_accepted_runs(self):
        dance = {"id": "dance_1", "name": "Dance 1", "run_ids": ["run_000001"]}
        with self.assertRaisesRegex(ValueError, "remain accepted"):
            self.store.save_dances([dance])
        self.store.save_run("run_000001", accepted())
        with self.assertRaisesRegex(ValueError, "only one dance"):
            self.store.save_dances([dance, {**dance, "id": "dance_2"}])
        self.store.save_dances([dance])
        rejected = accepted()
        rejected["status"] = "rejected"
        with self.assertRaisesRegex(ValueError, "remove it from the dance first"):
            self.store.save_run("run_000001", rejected)
        self.store.save_dances([])
        self.store.save_run("run_000001", rejected)


class YoloTests(unittest.TestCase):
    def master(self, annotation=None):
        return {"clips": {"run_000001": {"metadata": metadata(), "annotation": annotation or accepted()}}}

    def test_crop_normalization_and_interval_stride(self):
        rows = yolo_rows(self.master(), stride=2)
        self.assertEqual([row["frame"] for row in rows], [0, 2, 4])
        self.assertEqual([float(value) for value in rows[0]["label"].split()], [0, .2, .375, .2, .25])
        self.assertEqual(rows[0]["annotation"]["bbox_original"], [5, 220, 25, 240])
        self.assertEqual([row["frame"] for row in yolo_rows(self.master(), keyframes_only=True)], [0, 4])

    def test_filters_drafts_rejected_and_uncertain_frames(self):
        annotation = accepted()
        for status in ("unreviewed", "rejected"):
            annotation["status"] = status
            self.assertEqual(yolo_rows(self.master(annotation)), [])
        annotation["status"] = "accepted"
        annotation["keyframes"][1]["uncertain"] = True
        self.assertEqual([row["frame"] for row in yolo_rows(self.master(annotation))], [0])
        self.assertEqual(len(yolo_rows(self.master(annotation), include_uncertain=True)), 5)
        annotation["uncertain"] = True
        self.assertEqual(yolo_rows(self.master(annotation)), [])


if __name__ == "__main__":
    unittest.main()
