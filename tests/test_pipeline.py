"""Pipeline review keeps detector labels and dance memberships independent of master.json."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from annotation_tool.pipeline import PipelineProject


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write('metadata.json', dict(predictions_complete=True, width=100, height=80,
                                        fps=10, frame_count=3))
        self.write('runs.json', [dict(run_id=i, start_frame=0, end_frame=2,
            start_time_s=0, end_time_s=.2, duration_s=.2, angle_deg=90) for i in (1, 2)])
        rows = [dict(frame_id=f, source_time_s=f/10, run_ids=[1, 2],
                     boxes_normalized_xyxy=[[.1,.2,.3,.4],[.5,.6,.7,.8]]) for f in range(3)]
        (self.root / 'frames.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
        self.project = PipelineProject(self.root)
        for i in (1, 2):
            self.project.review.clip(i).write_bytes(b'test-video')

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def payload(self, **fields):
        p = self.project.project()
        return dict(root=p['root'], revision=p['revision'], labels_token=p['labels_token'], **fields)

    def yes(self, run_id=1):
        return self.project.save(self.payload(run_id=run_id, label='yes'))

    def dance(self, ids=None):
        return dict(id='dance_1', name='Dance 1', run_ids=ids or ['1'], notes='same bee', uncertain=True)

    def test_existing_labels_and_full_resolution_trajectories(self):
        self.write('review/labels.json', dict(schema_version=1,
            runs_sha256=self.project.review.fingerprint,
            labels={'1':dict(label='yes', updated_at='original'), '2':dict(label='uncertain')}))
        original = self.project.review.path.read_bytes()
        p = PipelineProject(self.root).project()
        self.assertEqual(p['clips'][0]['annotation']['status'], 'accepted')
        self.assertEqual(p['clips'][1]['label'], 'uncertain')
        self.assertEqual(p['clips'][0]['frames'][1]['source_time_s'], .1)
        self.assertEqual(p['clips'][0]['frames'][0]['bbox_original'], [10,16,30,32])
        self.assertEqual(original, self.project.review.path.read_bytes())

    def test_label_format_survives_reload_and_stale_write_is_rejected(self):
        old = self.payload(run_id=2, label='no')
        self.yes()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.project.save(old)
        p = PipelineProject(self.root).project()
        self.assertEqual(p['clips'][0]['label'], 'yes')
        self.assertIsNone(p['clips'][1]['label'])
        self.project.save(self.payload(run_id=2, label='no'))
        saved = json.loads(self.project.review.path.read_text())
        self.assertEqual(saved['runs_sha256'], hashlib.sha256((self.root/'runs.json').read_bytes()).hexdigest())
        self.assertEqual(saved['labels']['2']['label'], 'no')

    def test_dances_only_take_yes_runs_and_preserve_labels(self):
        with self.assertRaisesRegex(ValueError, 'accepted'):
            self.project.save(self.payload(dances=[self.dance()]))
        self.yes()
        original = self.project.review.path.read_bytes()
        self.project.save(self.payload(dances=[self.dance()]))
        p = PipelineProject(self.root).project()
        self.assertEqual(p['dances'], [self.dance()])
        self.assertEqual(p['revision'], 1)
        self.assertEqual(original, self.project.review.path.read_bytes())
        for label in ('no', 'uncertain'):
            with self.assertRaisesRegex(ValueError, 'Remove'):
                self.project.save(self.payload(run_id=1, label=label))
        with self.assertRaisesRegex(ValueError, 'only one dance'):
            self.project.save(self.payload(dances=[self.dance(), dict(self.dance(), id='dance_2')]))
        self.project.save(self.payload(dances=[]))
        self.project.save(self.payload(run_id=1, label='no'))
        self.assertFalse((self.root/'master.json').exists())

    def test_changed_source_predictions_and_missing_video_rejected(self):
        self.project.review.clip(1).unlink()
        with self.assertRaisesRegex(ValueError, 'video'):
            self.yes()
        with self.assertRaisesRegex(ValueError, 'project changed'):
            self.project.save(dict(self.payload(run_id=2, label='yes'), root='/other'))
        self.write('review/labels.json', dict(runs_sha256='changed', labels={}))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.project.project()

    def test_http_open_video_and_independent_revision(self):
        from .test_server import ServerTests
        server = ServerTests()
        server.setUp()
        self.addCleanup(server.doCleanups)
        # The detailed annotation project's revision must not gate pipeline saves.
        server.app.store.save_dances([], expected_revision=0)
        status, _, content = server.request('POST', '/api/pipeline/open', {'root': str(self.root)})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(content)['clips']), 2)
        status, _, content = server.request('PUT', '/api/pipeline', self.payload(run_id=1, label='yes'))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['clips'][0]['label'], 'yes')
        from urllib.parse import quote
        status, headers, content = server.request('GET', '/api/pipeline/video/1?root='+quote(str(self.root)))
        self.assertEqual(status, 200)
        self.assertIn('video/mp4', headers)
        self.assertEqual(content, b'test-video')
        status, _, _ = server.request('GET', '/api/pipeline/video/1?root=/other')
        self.assertEqual(status, 400)
