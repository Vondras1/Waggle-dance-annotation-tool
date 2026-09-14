"""Local HTTP application for frame-accurate run annotation."""

import argparse
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit
import zipfile
import uuid

from .media import CombImage, FrameReader
from .model import MasterStore, discover_clips, yolo_rows


from . import config
from .config import STATIC, EXPORT_README


def media_catalog(clips, store):
    return {run_id: clips[entry.get("source_clip_id", run_id)]
            for run_id, entry in store.get_master()["clips"].items()}


def default_clips_dir():
    for path in config.CLIPS_DIR_CANDIDATES:
        if path.is_dir():
            return path
    return config.DEFAULT_CLIPS_DIR


class Application:
    def __init__(self, clips_dir, master_path=None, *, comb_image=None, bag=None, allow_empty=False):
        self.clips_dir = Path(clips_dir).expanduser().resolve()
        self.master_path = (Path(master_path).expanduser().resolve() if master_path else
                            self.clips_dir.parent / config.ANNOTATIONS_DIR_NAME / config.MASTER_FILENAME)
        self.clips = discover_clips(self.clips_dir) if self.clips_dir.is_dir() else {}
        if not self.clips and not allow_empty:
            raise ValueError(f"No complete MP4/JSON run pairs found in {self.clips_dir}")
        self.store = MasterStore(self.master_path, self.clips)
        self.reader = FrameReader(media_catalog(self.clips, self.store))
        self.comb_image_path = comb_image
        self.bag_path = bag
        self.comb = CombImage(self.clips, image_path=comb_image, bag_path=bag)
        self.lock = threading.RLock()
        self.project_id = uuid.uuid4().hex

    def project(self):
        with self.lock:
            master = self.store.get_master()
            return {
                "clips": [{"id": clip_id, **entry}
                          for clip_id, entry in master["clips"].items()],
                "dances": master["dances"], "revision": master["revision"],
                "angle_convention": master["angle_convention"],
                "comb": self.comb.info(), "clips_dir": str(self.clips_dir),
                "master_path": str(self.master_path),
                "save_dir": str(self.master_path.parent), "project_id": self.project_id,
                "supports_multiple_runs": True, "supports_box_exclusions": True,
                "annotation_api_version": config.ANNOTATION_API_VERSION,
            }

    def open_project(self, clips_dir, save_dir):
        """Validate a replacement completely before releasing the current project."""
        for name, value in (("Source folder", clips_dir), ("Save folder", save_dir)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        source = Path(clips_dir.strip()).expanduser().resolve()
        folder = Path(save_dir.strip()).expanduser().resolve()
        with self.lock:
            same_source = source == self.clips_dir
            target = folder / config.MASTER_FILENAME
            if same_source and target == self.master_path:
                return self.rescan()
            candidate = Application(source, target,
                                    comb_image=self.comb_image_path if same_source else None,
                                    bag=self.bag_path if same_source else None)
            try:
                candidate.comb.prepare()
                folder.mkdir(parents=True, exist_ok=True)
                # Fail early for unwritable destinations instead of losing a later annotation.
                with tempfile.TemporaryFile(dir=folder):
                    pass
                if not target.exists():
                    if same_source:
                        candidate.store = self.store.copy_to(target, candidate.clips)
                        candidate.reader.close()
                        candidate.reader = FrameReader(media_catalog(candidate.clips, candidate.store))
                    else:
                        candidate.store.save_dances([], expected_revision=0)
                old_reader = self.reader
                for name in ("clips_dir", "master_path", "clips", "store", "reader",
                             "comb_image_path", "bag_path", "comb", "project_id"):
                    setattr(self, name, getattr(candidate, name))
                old_reader.close()
                return self.project()
            except BaseException:
                candidate.close()
                raise

    def rescan(self):
        with self.lock:
            clips = discover_clips(self.clips_dir)
            if not clips:
                raise ValueError(f"No complete MP4/JSON run pairs found in {self.clips_dir}")
            # MasterStore validates saved metadata before replacing live state.
            store = MasterStore(self.master_path, clips)
            reader = FrameReader(media_catalog(clips, store))
            old_source = next(iter(self.clips.values()))["metadata"]["source"] if self.clips else None
            new_source = next(iter(clips.values()))["metadata"]["source"]
            comb = self.comb
            if old_source != new_source or not comb.info()["available"]:
                comb = CombImage(clips, image_path=self.comb_image_path, bag_path=self.bag_path)
                comb.prepare()
            self.reader.close()
            self.clips, self.store, self.reader, self.comb = clips, store, reader, comb
            return self.project()

    def export(self, options):
        include_uncertain = options.get("include_uncertain", False)
        keyframes_only = options.get("keyframes_only", False)
        stride = options.get("stride", 1)
        if type(include_uncertain) is not bool or type(keyframes_only) is not bool:
            raise ValueError("Export uncertainty/keyframe options must be booleans")
        if type(stride) is not int or stride < 1:
            raise ValueError("Export stride must be a positive integer")
        # Spill large exports to disk at the configured memory limit.
        output = tempfile.SpooledTemporaryFile(max_size=config.EXPORT_SPOOL_BYTES)
        try:
            with self.lock:
                master = copy.deepcopy(self.store.get_master())
                rows = list(yolo_rows(master, include_uncertain=include_uncertain,
                                      keyframes_only=keyframes_only, stride=stride))
                if not rows:
                    raise ValueError("No eligible frames. Save an accepted run with boxes; "
                                     "uncertain annotations are excluded by default.")
                manifest = {
                    "schema_version": config.EXPORT_SCHEMA_VERSION, "master_revision": master["revision"],
                    "class_names": {str(config.YOLO_CLASS_ID): config.YOLO_CLASS_NAME},
                    "options": {"include_uncertain": include_uncertain,
                                "keyframes_only": keyframes_only, "stride": stride},
                    "frames": [],
                }
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    for row in rows:
                        stem = f'{row["clip_id"]}_{row["frame"]:06d}'
                        archive.writestr(f"images/{stem}.jpg",
                                         self.reader.frame(row["clip_id"], row["frame"]))
                        archive.writestr(f"labels/{stem}.txt", row["label"].rstrip() + "\n")
                        manifest["frames"].append({
                            "image": f"images/{stem}.jpg", "clip_id": row["clip_id"],
                            "frame": row["frame"], "source_clip_id": row["source_clip_id"],
                            "annotation_run_id": row["clip_id"], **row["annotation"],
                        })
                    archive.writestr("classes.txt", config.YOLO_CLASS_NAME + "\n")
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2, allow_nan=False))
                    archive.writestr(config.MASTER_FILENAME, json.dumps(master, indent=2, allow_nan=False))
                    archive.writestr("README.txt", EXPORT_README)
            output.seek(0)
            return output
        except BaseException:
            output.close()
            raise

    def close(self):
        self.reader.close()




class AnnotationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, application):
        self.application = application
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "DanceAnnotator/1.0"

    def log_message(self, format, *args):
        # Frame stepping creates many requests; retain errors without flooding stdout.
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            super().log_message(format, *args)

    @property
    def app(self):
        return self.server.application

    def send_bytes(self, payload, content_type, *, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; "
                         "style-src 'self'; script-src 'self'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, value, *, status=200, filename=None):
        self.send_bytes(json.dumps(value, allow_nan=False).encode(),
                        "application/json; charset=utf-8", status=status, filename=filename)

    def trusted_request(self):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        allowed = {f"{host}:{port}" for host in config.ALLOWED_HOSTS}
        if host not in allowed:
            self.send_json({"error": "Use the localhost URL printed by the server"}, status=403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + name for name in allowed}:
            self.send_json({"error": "Cross-origin requests are not allowed"}, status=403)
            return False
        return True

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Expected an application/json request")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= config.MAX_REQUEST_BYTES:
            raise ValueError(f"JSON request must be between 1 and {config.MAX_REQUEST_BYTES} bytes")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def do_GET(self):
        self.dispatch("GET")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        if not self.trusted_request():
            return
        path = unquote(urlsplit(self.path).path)
        try:
            with self.app.lock:
                query = dict(parse_qsl(urlsplit(self.path).query))
                requested_project = self.headers.get("X-Annotation-Project") or query.get("project_id")
                if path != "/api/project" and requested_project and requested_project != self.app.project_id:
                    self.send_json({"error": "Project changed in another window. Refresh before continuing."}, status=409)
                    return
                if method == "GET":
                    self.get(path)
                elif method == "PUT":
                    self.put(path)
                else:
                    self.post(path)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Scrubbing may cancel a pending image request.
        except KeyError as error:
            self.send_json({"error": f"Unknown clip or missing field: {error}"}, status=404)
        except (ValueError, TypeError) as error:
            self.send_json({"error": str(error)}, status=400)
        except (OSError, RuntimeError) as error:
            self.send_json({"error": str(error)}, status=500)

    def get(self, path):
        static = config.STATIC_ROUTES
        if path in static:
            filename, content_type = static[path]
            self.send_bytes((STATIC / filename).read_bytes(), content_type)
        elif path == "/config.js":
            payload = "window.ANNOTATION_CONFIG = Object.freeze(" + json.dumps(config.UI, allow_nan=False) + ");\n"
            self.send_bytes(payload.encode("utf-8"), "text/javascript; charset=utf-8")
        elif path == "/api/project":
            self.send_json(self.app.project())
        elif path == "/api/master":
            self.send_json(self.app.store.get_master(), filename=config.MASTER_FILENAME)
        elif path == "/api/comb":
            self.send_bytes(self.app.comb.get(), "image/jpeg")
        elif path.startswith("/api/frame/"):
            parts = path.split("/")
            if len(parts) != 5:
                raise ValueError("Expected /api/frame/clip_id/frame_index")
            with self.app.lock:
                payload = self.app.reader.frame(parts[3], int(parts[4]))
            self.send_bytes(payload, "image/jpeg")
        else:
            self.send_json({"error": "Not found"}, status=404)

    def project_matches(self, payload):
        if payload.get("project_id") != self.app.project_id:
            self.send_json({"error": "Project changed or server restarted. Refresh before saving."}, status=409)
            return False
        return True

    def put(self, path):
        payload = self.body()
        if not self.project_matches(payload):
            return
        with self.app.lock:
            revision = payload.get("revision")
            if type(revision) is not int or revision != self.app.store.get_master()["revision"]:
                self.send_json({"error": "Annotations changed in another window. Reload before saving."},
                               status=409)
                return
            if path.startswith("/api/runs/") and len(path.split("/")) == 4:
                if payload.get("angle_convention") != self.app.store.get_master()["angle_convention"]:
                    self.send_json({"error": "Angle convention changed. Refresh the browser before saving."}, status=409)
                    return
                self.app.store.save_run(path.split("/")[3], payload["annotation"],
                                        expected_revision=revision)
            elif path == "/api/dances":
                self.app.store.save_dances(payload["dances"], expected_revision=revision)
            else:
                self.send_json({"error": "Not found"}, status=404)
                return
            self.send_json(self.app.project())

    def post(self, path):
        payload = self.body()
        if not self.project_matches(payload):
            return
        if path in ("/api/runs/add", "/api/runs/delete"):
            if payload.get("revision") != self.app.store.get_master()["revision"]:
                self.send_json({"error": "Annotations changed. Reload before adding or deleting a run."}, status=409)
                return
            selected = payload.get("run_id")
            if path.endswith("/add"):
                selected, _ = self.app.store.add_run(selected, payload.get("frame"), payload["revision"])
            else:
                self.app.store.delete_run(selected, payload["revision"])
            self.app.reader.close()
            self.app.reader = FrameReader(media_catalog(self.app.clips, self.app.store))
            self.send_json({**self.app.project(), "selected_run_id": selected})
        elif path == "/api/project/open":
            if payload.get("revision") != self.app.store.get_master()["revision"]:
                self.send_json({"error": "Annotations changed in another window. Reload before switching folders."}, status=409)
                return
            self.send_json(self.app.open_project(payload.get("clips_dir"), payload.get("save_dir")))
        elif path == "/api/rescan":
            self.send_json(self.app.rescan())
        elif path == "/api/export":
            with self.app.export(payload) as archive:
                archive.seek(0, io.SEEK_END)
                size = archive.tell()
                archive.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="waggle_yolo.zip"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := archive.read(config.DOWNLOAD_CHUNK_BYTES):
                    self.wfile.write(chunk)
        else:
            self.send_json({"error": "Not found"}, status=404)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clips-dir", type=Path, default=default_clips_dir(),
                        help="Directory containing run_*.mp4 and matching JSON sidecars")
    parser.add_argument("--annotations", type=Path,
                        help="Master JSON path (default: sibling annotations/master.json)")
    parser.add_argument("--comb-image", type=Path,
                        help="Full-resolution undistorted comb image; otherwise read source ROS bag")
    parser.add_argument("--bag", type=Path, help="Override source bag path for the comb background")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    app = None
    try:
        app = Application(args.clips_dir, args.annotations, comb_image=args.comb_image, bag=args.bag, allow_empty=True)
        print(f"Loaded {len(app.clips)} run clips from {app.clips_dir}", flush=True)
        print("Preparing full-comb background…", flush=True)
        comb = app.comb.info()
        print(comb["message"], flush=True)
        with AnnotationServer((config.SERVER_HOST, args.port), app) as server:
            print(f"Open http://localhost:{server.server_port}", flush=True)
            print(f"Master annotations: {app.master_path}", flush=True)
            print("Press Ctrl+C to stop.", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")
    finally:
        if app is not None:
            app.close()


if __name__ == "__main__":
    main()
