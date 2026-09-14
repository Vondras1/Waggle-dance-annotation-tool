"""Exercise the actual HTTP handler and export workflow without binding a port."""

import copy
import io
import json
from pathlib import Path
import tempfile
import shutil
from unittest.mock import patch
from types import SimpleNamespace
import unittest
import zipfile

import cv2
import numpy as np

from annotation_tool import config
from annotation_tool.server import Application, Handler, default_clips_dir


class MemoryConnection:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clips = self.root / "run_clips"
        self.clips.mkdir()
        self.make_clip("run_000001", 1)
        self.comb = self.root / "comb.png"
        cv2.imwrite(str(self.comb), np.zeros((96, 128, 3), np.uint8))
        self.master_path = self.root / "annotations/master.json"
        self.app = Application(self.clips, self.master_path, comb_image=self.comb)
        self.addCleanup(self.app.close)

    def make_clip(self, name, run_id):
        video = self.clips / f"{name}.mp4"
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
        self.assertTrue(writer.isOpened())
        try:
            for index in range(6):
                writer.write(np.full((48, 64, 3), index * 35, np.uint8))
        finally:
            writer.release()
        metadata = {
            "schema_version": 1, "video_file": video.name,
            "source": {"bag": "/nonexistent/session", "topic": "/image"},
            "run": {"run_id": run_id, "start_time_s": 10.1, "end_time_s": 10.4,
                    "bbox_x_min_px": -8, "bbox_y_min_px": 30,
                    "bbox_x_max_px": 56, "bbox_y_max_px": 78},
            "width": 64, "height": 48, "fps": 10, "frame_count": 6,
            "coordinate_space": "full-resolution undistorted image; max bounds exclusive",
            "output_frame_times_s": [10 + index / 10 for index in range(6)],
            "source_frame_times_s": [10, 10.08, 10.18, 10.18, 10.38, 10.48],
        }
        video.with_suffix(".json").write_text(json.dumps(metadata))

    def request(self, method, path, body=None, *, origin=None, host="localhost:8765"):
        if body is not None:
            body = {"project_id": self.app.project_id, **body}
        payload = json.dumps(body).encode() if body is not None else b""
        headers = [f"{method} {path} HTTP/1.0", f"Host: {host}"]
        if body is not None:
            headers += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
        if origin is not None:
            headers.append(f"Origin: {origin}")
        connection = MemoryConnection(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
        server = SimpleNamespace(application=self.app, server_address=("127.0.0.1", 8765))
        Handler(connection, ("127.0.0.1", 1), server)
        head, content = connection.output.getvalue().split(b"\r\n\r\n", 1)
        status = int(head.split(b" ", 2)[1])
        return status, head.decode(), content

    def test_browser_config_contains_only_public_settings(self):
        with patch.dict(config.UI, {"autosaveMs": 1234}):
            status, headers, content = self.request("GET", "/config.js")
            self.assertEqual(status, 200)
            self.assertIn("text/javascript", headers)
            prefix = b"window.ANNOTATION_CONFIG = Object.freeze("
            self.assertTrue(content.startswith(prefix))
            settings = json.loads(content[len(prefix):-3])
            self.assertEqual(settings, config.UI)
            self.assertEqual(settings["autosaveMs"], 1234)
            self.assertNotIn(str(config.REPO).encode(), content)
        _, _, html = self.request("GET", "/")
        self.assertLess(html.index(b'/config.js'), html.index(b'/app.js'))

    def test_configured_paths_and_request_limit(self):
        missing = self.root / "missing"
        with patch.object(config, "CLIPS_DIR_CANDIDATES", (missing, self.clips)), \
             patch.object(config, "DEFAULT_CLIPS_DIR", missing):
            self.assertEqual(default_clips_dir(), self.clips)
            with patch.object(config, "CLIPS_DIR_CANDIDATES", (missing,)):
                self.assertEqual(default_clips_dir(), missing)
        with patch.object(config, "MAX_REQUEST_BYTES", 1):
            status, _, _ = self.request("PUT", "/api/dances", {"dances": []})
            self.assertEqual(status, 400)

    def accepted(self):
        annotation = copy.deepcopy(self.app.project()["clips"][0]["annotation"])
        annotation.update({"start_frame": 1, "end_frame": 4, "status": "accepted",
                           "direction_deg": 90, "notes": "one target bee", "keyframes": [
            {"frame": 1, "bbox": [8, 10, 24, 26], "orientation_deg": 350, "uncertain": False},
            {"frame": 4, "bbox": [20, 16, 36, 32], "orientation_deg": 10, "uncertain": False},
        ]})
        return annotation

    def save(self, annotation, revision=0):
        return self.request("PUT", "/api/runs/run_000001",
                            {"revision": revision, "annotation": annotation, "angle_convention": self.app.project()["angle_convention"]})

    def test_run_dance_and_reload_round_trip(self):
        self.assertEqual(self.app.project()["annotation_api_version"], 5)
        status, _, content = self.save(self.accepted())
        self.assertEqual(status, 200, content)
        project = json.loads(content)
        self.assertEqual(project["revision"], 1)
        self.assertEqual(project["clips"][0]["frames"][0]["bbox_original"], [0, 40, 16, 56])
        dance = {"id": "dance_1", "name": "Dance A", "run_ids": ["run_000001"],
                 "notes": "Repeated runs", "uncertain": True}
        status, _, content = self.request("PUT", "/api/dances", {"revision": 1, "dances": [dance]})
        self.assertEqual(status, 200, content)
        reopened = Application(self.clips, self.master_path, comb_image=self.comb)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.project()["dances"], [dance])
        self.assertEqual(reopened.project()["revision"], 2)

    def test_old_browser_cannot_save_angles_using_obsolete_convention(self):
        status, _, body = self.request("PUT", "/api/runs/run_000001",
                                       {"revision": 0, "annotation": self.accepted()})
        self.assertEqual(status, 409)
        self.assertIn("Refresh", json.loads(body)["error"])
        self.assertFalse(self.master_path.exists())

    def test_conflict_and_invalid_edit_do_not_overwrite(self):
        self.assertEqual(self.save(self.accepted())[0], 200)
        before = self.master_path.read_bytes()
        self.assertEqual(self.save(self.accepted(), revision=0)[0], 409)
        bad = self.accepted()
        bad["keyframes"] = []
        self.assertEqual(self.save(bad, revision=1)[0], 400)
        self.assertEqual(self.master_path.read_bytes(), before)

    def test_yolo_zip_images_coordinates_and_provenance(self):
        self.assertEqual(self.save(self.accepted())[0], 200)
        status, _, data = self.request("POST", "/api/export", {})
        self.assertEqual(status, 200, data[:100])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len(manifest["frames"]), 4)
            row = manifest["frames"][0]
            self.assertEqual(row["source_time_s"], 10.08)
            self.assertEqual(row["output_time_s"], 10.1)
            label = archive.read("labels/run_000001_000001.txt").decode().split()
            self.assertEqual(label[0], "0")
            np.testing.assert_allclose([float(v) for v in label[1:]], [0.25, 0.375, 0.25, 1 / 3])
            image = cv2.imdecode(np.frombuffer(archive.read(row["image"]), np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(image.shape[:2], (48, 64))
            self.assertLess(abs(float(image.mean()) - 35), 8)
            self.assertEqual(json.loads(archive.read("master.json"))["revision"], 1)

    def test_uncertainty_filter_and_explicit_inclusion(self):
        annotation = self.accepted()
        annotation["uncertain"] = True
        self.assertEqual(self.save(annotation)[0], 200)
        self.assertEqual(self.request("POST", "/api/export", {})[0], 400)
        status, _, data = self.request("POST", "/api/export", {"include_uncertain": True,
                                                                "keyframes_only": True})
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len(json.loads(archive.read("manifest.json"))["frames"]), 2)

    def test_rescan_preserves_saved_work_and_discovers_new_clips(self):
        self.assertEqual(self.save(self.accepted())[0], 200)
        self.make_clip("run_000002", 2)
        status, _, data = self.request("POST", "/api/rescan", {})
        self.assertEqual(status, 200, data)
        project = json.loads(data)
        self.assertEqual(len(project["clips"]), 2)
        self.assertEqual(project["clips"][0]["annotation"]["status"], "accepted")
        self.assertEqual(project["clips"][1]["annotation"]["status"], "unreviewed")

    def test_frame_and_comb_routes_and_invalid_frame(self):
        status, head, image = self.request("GET", "/api/frame/run_000001/5")
        self.assertEqual(status, 200)
        self.assertIn("image/jpeg", head)
        decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        self.assertLess(abs(float(decoded.mean()) - 175), 8)
        self.assertEqual(self.request("GET", "/api/frame/run_000001/6")[0], 400)
        self.assertEqual(self.request("GET", "/api/frame/unknown/0")[0], 404)
        self.assertEqual(self.request("GET", "/api/comb")[0], 200)

    def test_rescan_cannot_empty_project_and_refreshes_changed_source(self):
        sidecar = self.clips / "run_000001.json"
        metadata = json.loads(sidecar.read_text())
        metadata["source"]["bag"] = "/another/recording"
        sidecar.write_text(json.dumps(metadata))
        old_comb = self.app.comb
        self.assertEqual(self.request("POST", "/api/rescan", {})[0], 200)
        self.assertIsNot(self.app.comb, old_comb)
        sidecar.unlink()
        self.assertEqual(self.request("POST", "/api/rescan", {})[0], 400)
        self.assertEqual(len(self.app.project()["clips"]), 1)

    def open_folders(self, source, destination, **extra):
        return self.request("POST", "/api/project/open", {
            "clips_dir": str(source), "save_dir": str(destination),
            "revision": self.app.project()["revision"], **extra})

    def test_new_save_folder_copies_work_and_keeps_original(self):
        self.assertEqual(self.save(self.accepted())[0], 200)
        original = self.master_path.read_bytes()
        previous_id = self.app.project_id
        target = self.root / "new annotation folder"
        status, _, content = self.open_folders(self.clips, target)
        self.assertEqual(status, 200, content)
        project = json.loads(content)
        self.assertEqual(project["master_path"], str(target / "master.json"))
        self.assertNotEqual(project["project_id"], previous_id)
        self.assertEqual(project["clips"][0]["annotation"]["status"], "accepted")
        self.assertEqual(self.master_path.read_bytes(), original)
        self.assertEqual(json.loads((target / "master.json").read_text())["clips"]["run_000001"]["annotation"]["status"], "accepted")

    def test_open_different_source_and_reopen_existing_project(self):
        self.assertEqual(self.save(self.accepted())[0], 200)
        second = self.root / "second clips"
        shutil.copytree(self.clips, second)
        sidecar = second / "run_000001.json"
        metadata = json.loads(sidecar.read_text())
        metadata["source"]["bag"] = "/second-recording"
        sidecar.write_text(json.dumps(metadata))
        target = self.root / "second annotations"
        status, _, data = self.open_folders(second, target)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["clips"][0]["annotation"]["status"], "unreviewed")
        self.assertIsNone(self.app.comb_image_path)
        status, _, data = self.open_folders(self.clips, self.master_path.parent)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["clips"][0]["annotation"]["status"], "accepted")

    def test_invalid_source_and_unwritable_destination_keep_current_project(self):
        previous_id = self.app.project_id
        previous_reader = self.app.reader
        status, _, _ = self.open_folders(self.root / "missing", self.root / "destination")
        self.assertEqual(status, 400)
        with patch("annotation_tool.server.tempfile.TemporaryFile", side_effect=PermissionError("Permission denied")):
            status, _, _ = self.open_folders(self.clips, self.root / "unwritable")
        self.assertEqual(status, 500)
        self.assertEqual(self.app.project_id, previous_id)
        self.assertIs(self.app.reader, previous_reader)
        self.assertTrue(self.app.reader.frame("run_000001", 0))

    def test_stale_tab_cannot_save_or_export_into_new_project(self):
        old_id = self.app.project_id
        self.assertEqual(self.open_folders(self.clips, self.root / "new")[0], 200)
        before = self.app.master_path.read_bytes()
        body = {"project_id": old_id, "revision": self.app.project()["revision"],
                "annotation": self.accepted(), "angle_convention": self.app.project()["angle_convention"]}
        self.assertEqual(self.request("PUT", "/api/runs/run_000001", body)[0], 409)
        self.assertEqual(self.request("POST", "/api/export", {"project_id": old_id})[0], 409)
        self.assertEqual(self.request("GET", "/api/frame/run_000001/0?project_id=" + old_id)[0], 409)
        self.assertEqual(self.app.master_path.read_bytes(), before)

    def test_empty_startup_can_open_folders_in_app(self):
        empty = Application(self.root / "absent", self.root / "empty/master.json", allow_empty=True)
        self.addCleanup(empty.close)
        self.assertEqual(empty.project()["clips"], [])
        project = empty.open_project(str(self.clips), str(self.root / "chosen"))
        self.assertEqual(len(project["clips"]), 1)
        self.assertTrue(Path(project["master_path"]).is_file())

    def test_changed_source_metadata_cannot_be_copied_as_current_annotations(self):
        self.assertEqual(self.save(self.accepted())[0], 200)
        sidecar = self.clips / "run_000001.json"
        metadata = json.loads(sidecar.read_text())
        metadata["source"]["bag"] = "/different-recording"
        sidecar.write_text(json.dumps(metadata))
        before = self.app.project_id
        status, _, _ = self.open_folders(self.clips, self.root / "new")
        self.assertEqual(status, 400)
        self.assertEqual(self.app.project_id, before)

    def test_multiple_runs_share_video_keep_gaps_neutral_and_export_independently(self):
        first = self.accepted()
        first["end_frame"] = 2
        self.assertEqual(self.save(first)[0], 200)
        status, _, body = self.request("POST", "/api/runs/add", {
            "run_id": "run_000001", "frame": 4, "revision": 1})
        self.assertEqual(status, 200, body)
        project = json.loads(body)
        added_id = project["selected_run_id"]
        added = next(c for c in project["clips"] if c["id"] == added_id)
        self.assertEqual(added["source_clip_id"], "run_000001")
        self.assertEqual(added["run_number"], 2)
        self.assertEqual(self.request("GET", f"/api/frame/{added_id}/4")[0], 200)
        second = added["annotation"]
        second.update(end_frame=5, direction_deg=-90, status="accepted", keyframes=[
            {"frame": 4, "bbox": [20, 20, 35, 35], "uncertain": False},
            {"frame": 5, "bbox": [25, 22, 40, 37], "uncertain": False}])
        status, _, body = self.request("PUT", f"/api/runs/{added_id}", {
            "annotation": second, "revision": project["revision"],
            "angle_convention": project["angle_convention"]})
        self.assertEqual(status, 200, body)
        master = self.app.store.get_master()
        self.assertEqual([f["frame"] for f in master["clips"]["run_000001"]["frames"]], [1, 2])
        self.assertEqual([f["frame"] for f in master["clips"][added_id]["frames"]], [4, 5])
        reopened = Application(self.clips, self.master_path, comb_image=self.comb)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.get_master(), master)
        self.assertTrue(reopened.reader.frame(added_id, 5))
        status, _, data = self.request("POST", "/api/export", {})
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual({r["frame"] for r in manifest["frames"]}, {1, 2, 4, 5})
            self.assertEqual({r["source_clip_id"] for r in manifest["frames"]}, {"run_000001"})
            self.assertEqual({r["annotation_run_id"] for r in manifest["frames"]}, {"run_000001", added_id})
        self.assertEqual(self.request("POST", "/api/rescan", {})[0], 200)
        self.assertTrue(self.app.reader.frame(added_id, 4))
        target = self.root / "multi copy"
        self.assertEqual(self.open_folders(self.clips, target)[0], 200)
        self.assertEqual(len(self.app.store.get_master()["clips"]), 2)
        self.assertTrue(self.app.reader.frame(added_id, 4))

    def test_extra_run_overlap_is_saved_and_grouped_deletion_is_blocked(self):
        first = self.accepted()
        first["end_frame"] = 2
        self.assertEqual(self.save(first)[0], 200)
        _, _, body = self.request("POST", "/api/runs/add", {"run_id": "run_000001", "frame": 4, "revision": 1})
        project = json.loads(body);added_id = project["selected_run_id"]
        a = next(c["annotation"] for c in project["clips"] if c["id"] == added_id)
        a.update(start_frame=2, status="accepted", keyframes=[
            {"frame": 2, "bbox": [1, 2, 10, 12]},
            {"frame": 4, "bbox": [2, 3, 11, 13]},
        ])
        self.assertEqual(self.request("PUT", f"/api/runs/{added_id}", {"annotation": a, "revision": 2,
                                   "angle_convention": project["angle_convention"]})[0], 200)
        reopened = Application(self.clips, self.master_path, comb_image=self.comb)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.get_master()["clips"][added_id]["annotation"],
                         self.app.store.get_master()["clips"][added_id]["annotation"])
        self.assertEqual(self.request("PUT", "/api/dances", {"revision": 3,
                            "dances": [{"id": "dance1", "name": "Both bouts", "run_ids": ["run_000001", added_id]}]})[0], 200)
        self.assertEqual(self.request("POST", "/api/runs/delete", {"run_id": added_id, "revision": 4})[0], 400)
        self.assertEqual(self.request("PUT", "/api/dances", {"revision": 4, "dances": []})[0], 200)
        self.assertEqual(self.request("POST", "/api/runs/delete", {"run_id": added_id, "revision": 5})[0], 200)
        self.assertEqual(len(self.app.project()["clips"]), 1)
        self.assertEqual(self.request("POST", "/api/runs/delete", {"run_id": "run_000001", "revision": 6})[0], 400)

    def test_foreign_origins_and_nonlocal_hosts_cannot_mutate(self):
        self.assertEqual(self.request("PUT", "/api/dances", {"revision": 0, "dances": []},
                                      origin="https://foreign.example")[0], 403)
        self.assertEqual(self.request("GET", "/api/master", host="foreign.example:8765")[0], 403)
        self.assertFalse(self.master_path.exists())


if __name__ == "__main__":
    unittest.main()
