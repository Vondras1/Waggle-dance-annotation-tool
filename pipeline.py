"""Review existing pipeline clips and assemble their detections into dances.

Detector boxes remain predictions, separate from the hand annotations in master.json.
"""
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path

from deepwdt.src.run_review import Review, atomic_json
from .model import _validate_dances


class PipelineProject:
    def __init__(self, root):
        self.review = Review(root)
        self.root = self.review.root
        self.path = self.review.folder / 'dances.json'
        self.clips = []
        width, height = self.review.metadata['width'], self.review.metadata['height']
        observations = {r['run_id']: [] for r in self.review.runs}
        with (self.root / 'frames.jsonl').open() as file:
            for line in file:
                row = json.loads(line)
                for run_id, box in zip(row['run_ids'], row['boxes_normalized_xyxy']):
                    if run_id in observations:
                        observations[run_id].append(dict(frame=row['frame_id'],
                            source_time_s=row['source_time_s'],
                            bbox_original=[v*s for v, s in zip(box, [width, height, width, height])]))
        for run in self.review.runs:
            self.clips.append(dict(id=str(run['run_id']), metadata=dict(run=run),
                annotation=dict(status='unreviewed', direction_deg=run.get('angle_deg')),
                frames=observations[run['run_id']]))

    def _labels(self):
        if not self.review.path.exists():
            return {}, None
        raw = self.review.path.read_bytes()
        saved = json.loads(raw)
        if saved.get('runs_sha256') != self.review.fingerprint:
            raise ValueError('runs.json changed since review; preserve the original labels')
        labels = saved['labels']
        if any(int(k) not in self.review.ids or v.get('label') not in ('yes', 'no', 'uncertain')
               for k, v in labels.items()):
            raise ValueError('Invalid saved pipeline labels')
        return labels, hashlib.sha256(raw).hexdigest()

    def _dances(self):
        if not self.path.exists():
            return dict(revision=0, dances=[])
        saved = json.loads(self.path.read_text())
        if saved.get('schema_version') != 1 or saved.get('runs_sha256') != self.review.fingerprint:
            raise ValueError('Pipeline dances belong to different predictions')
        return saved

    def project(self):
        labels, token = self._labels()
        dances = self._dances()
        clips = deepcopy(self.clips)
        for clip in clips:
            label = labels.get(clip['id'], {}).get('label')
            clip['label'] = label
            clip['ready'] = self.review.clip(int(clip['id'])).is_file()
            clip['annotation']['status'] = 'accepted' if label == 'yes' else 'unreviewed'
        # Keep existing memberships visible if another reviewer changes a label.
        return dict(root=str(self.root), clips=clips, labels_token=token,
                    dances=dances['dances'], revision=dances['revision'],
                    label_path=str(self.review.path), dance_path=str(self.path),
                    comb=dict(available=False, width=self.review.metadata['width'],
                              height=self.review.metadata['height'],
                              message='Pipeline detector trajectories · full-resolution undistorted coordinates.'))

    def save(self, payload):
        # One lock for labels and memberships; reread disk state on every edit.
        with (self.review.folder / '.annotation-workspace.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            labels, token = self._labels()
            dances = self._dances()
            if payload.get('root') != str(self.root):
                raise ValueError('Pipeline project changed; reload before saving')
            if payload.get('labels_token') != token or payload.get('revision') != dances['revision']:
                raise ValueError('Pipeline annotations changed; reload before saving')
            if 'dances' in payload:
                clips = {c['id']: c for c in self.project()['clips']}
                validated = _validate_dances(payload['dances'], clips)
                atomic_json(self.path, dict(schema_version=1, runs_sha256=self.review.fingerprint,
                    revision=dances['revision'] + 1, dances=validated))
            else:
                run_id = payload.get('run_id')
                if type(run_id) is not int or run_id not in self.review.ids:
                    raise ValueError('Unknown pipeline run')
                if payload.get('label') != 'yes' and any(str(run_id) in d['run_ids'] for d in dances['dances']):
                    raise ValueError('Remove this run from its dance before changing its label')
                self.review.labels = labels
                self.review.save(run_id, payload.get('label'))
            return self.project()
