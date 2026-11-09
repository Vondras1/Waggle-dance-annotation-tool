"""Human run/no-run labels for ordinary MP4 clips, independent of box annotations."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .media import FrameReader
from .model import discover_clips


class ClipReview:
    def __init__(self, root, *, clips=None, master=None):
        self.root = Path(root).expanduser().resolve()
        self.clips = discover_clips(self.root) if clips is None else clips
        self.master = master
        if not self.clips:
            raise ValueError(f"No complete MP4/JSON clip pairs found in {self.root}")
        self.path = self.root.parent / 'annotations' / 'clip_labels.json'
        self.reader = FrameReader(self.clips)
        self.fingerprint = hashlib.sha256(json.dumps(
            {key: clip['metadata'] for key, clip in self.clips.items()},
            sort_keys=True).encode()).hexdigest()

    def _labels(self):
        if not self.path.exists():
            return {}, None
        raw = self.path.read_bytes()
        saved = json.loads(raw)
        if (saved.get('schema_version') != 1 or saved.get('source') != str(self.root)
                or saved.get('clips_sha256') != self.fingerprint):
            raise ValueError('Review clips changed; preserve clip_labels.json before starting a new review')
        labels = saved.get('labels')
        if not isinstance(labels, dict) or any(
                key not in self.clips or not isinstance(value, dict)
                or value.get('label') not in ('yes', 'no', 'uncertain')
                for key, value in labels.items()):
            raise ValueError('Invalid saved clip labels')
        return labels, hashlib.sha256(raw).hexdigest()

    def _inherited(self):
        if self.master is None:
            return {}
        grouped = {}
        for run_id, entry in self.master()['clips'].items():
            grouped.setdefault(entry.get('source_clip_id', run_id), []).append(entry['annotation'])
        inherited = {}
        for clip_id, annotations in grouped.items():
            accepted = [a for a in annotations if a['status'] == 'accepted']
            if accepted:
                inherited[clip_id] = ('yes' if any(not a.get('uncertain', False) for a in accepted)
                                      else 'uncertain')
            elif all(a['status'] == 'rejected' for a in annotations):
                inherited[clip_id] = 'no'
        return inherited

    def project(self):
        labels, token = self._labels()
        inherited = self._inherited()
        return dict(root=str(self.root), label_path=str(self.path), labels_token=token,
            clips=[dict(id=key, metadata=clip['metadata'],
                        label=inherited.get(key, labels.get(key, {}).get('label')),
                        label_source='run_review' if key in inherited else 'manual')
                   for key, clip in self.clips.items()])

    def save(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.json.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            labels, token = self._labels()
            if payload.get('root') != str(self.root):
                raise ValueError('Review folder changed; reload before saving')
            if payload.get('labels_token') != token:
                raise ValueError('Clip labels changed; Open / reload before saving')
            clip_id, label = payload.get('clip_id'), payload.get('label')
            if not isinstance(clip_id, str) or clip_id not in self.clips:
                raise ValueError('Unknown review clip')
            if label not in ('yes', 'no', 'uncertain'):
                raise ValueError('Label must be Run, No run, or Unsure')
            if clip_id in self._inherited():
                raise ValueError('This label is inherited from Run review; change its annotation in mode 01')
            labels[clip_id] = dict(label=label, updated_at=datetime.now(timezone.utc).isoformat())
            saved = dict(schema_version=1, source=str(self.root),
                         clips_sha256=self.fingerprint, labels=labels)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent,
                        prefix='.clip_labels-', suffix='.tmp', delete=False) as file:
                    temporary = Path(file.name)
                    json.dump(saved, file, indent=2, allow_nan=False)
                    file.write('\n')
                    file.flush()
                    os.fsync(file.fileno())
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return self.project()

    def close(self):
        self.reader.close()
