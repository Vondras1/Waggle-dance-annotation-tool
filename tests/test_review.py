"""Plain-clip review never depends on detection predictions or modifies master.json."""
import json
import unittest
from urllib.parse import quote

from annotation_tool.review import ClipReview


class ClipReviewTests(unittest.TestCase):
    def setUp(self):
        from .test_server import ServerTests
        self.server = ServerTests()
        self.server.setUp()
        self.addCleanup(self.server.doCleanups)
        self.server.make_clip('run_000002', 2)
        self.server.app.rescan()
        self.review = ClipReview(self.server.clips)
        self.addCleanup(self.review.close)

    def payload(self, **fields):
        project = self.review.project()
        return dict({'root': project['root'], 'labels_token': project['labels_token']}, **fields)

    def test_plain_clips_open_without_predictions_or_writing_annotations(self):
        project = self.review.project()
        self.assertEqual([c['id'] for c in project['clips']], ['run_000001', 'run_000002'])
        self.assertTrue(all(c['label'] is None for c in project['clips']))
        self.assertFalse(self.review.path.exists())
        self.assertFalse(self.server.master_path.exists())
        self.assertFalse((self.server.clips / 'runs.json').exists())
        self.assertTrue(self.review.reader.frame('run_000001', 0).startswith(b'\xff\xd8'))

    def test_labels_persist_independently_and_stale_windows_cannot_overwrite(self):
        other = ClipReview(self.server.clips)
        self.addCleanup(other.close)
        stale = dict(root=str(other.root), labels_token=other.project()['labels_token'],
                     clip_id='run_000002', label='no')
        self.server.app.store.save_dances([], expected_revision=0)
        original_master = self.server.master_path.read_bytes()
        self.review.save(self.payload(clip_id='run_000001', label='yes'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            other.save(stale)
        other.save(dict(stale, labels_token=other.project()['labels_token']))
        project = self.review.project()
        self.assertEqual([c['label'] for c in project['clips']], ['yes', 'no'])
        self.assertEqual(self.server.master_path.read_bytes(), original_master)
        self.assertEqual(json.loads(self.review.path.read_text())['labels']['run_000001']['label'], 'yes')

    def test_invalid_labels_sources_and_changed_clip_metadata_are_rejected(self):
        for fields in (dict(clip_id='missing', label='yes'),
                       dict(clip_id='run_000001', label='maybe'),
                       dict(clip_id='run_000001', label='yes', root='/other')):
            with self.assertRaises(ValueError):
                self.review.save(self.payload(**fields))
        self.review.save(self.payload(clip_id='run_000001', label='uncertain'))
        sidecar = self.server.clips / 'run_000001.json'
        data = json.loads(sidecar.read_text())
        data['extra'] = 'changed clip provenance'
        sidecar.write_text(json.dumps(data))
        changed = ClipReview(self.server.clips)
        self.addCleanup(changed.close)
        with self.assertRaisesRegex(ValueError, 'changed'):
            changed.project()

    def annotate(self, status, *, uncertain=False, run_id='run_000001'):
        store = self.server.app.store
        master = store.get_master()
        annotation = master['clips'][run_id]['annotation']
        annotation.update(status=status, uncertain=uncertain)
        if status == 'accepted' and not uncertain:
            annotation.update(direction_mode='manual', direction_deg=90,
                keyframes=[dict(frame=f, bbox=[10,10,20,20], orientation_deg=None,
                                uncertain=False) for f in (0,5)])
        store.save_run(run_id, annotation, expected_revision=master['revision'])

    def test_shared_folder_and_live_inheritance_without_rewriting_manual_labels(self):
        app = self.server.app
        self.review.save(self.payload(clip_id='run_000001', label='no'))
        raw = self.review.path.read_bytes()
        self.assertEqual(app.review_project()['root'], str(app.clips_dir))
        self.annotate('accepted')
        clip = app.review_project()['clips'][0]
        self.assertEqual((clip['label'], clip['label_source']), ('yes', 'run_review'))
        self.assertEqual(self.review.path.read_bytes(), raw)
        with self.assertRaisesRegex(ValueError, 'inherited'):
            app.clip_review.save(self.payload(clip_id='run_000001', label='no'))
        self.annotate('rejected')
        self.assertEqual(app.review_project()['clips'][0]['label'], 'no')
        self.annotate('unreviewed')
        clip = app.review_project()['clips'][0]
        self.assertEqual((clip['label'], clip['label_source']), ('no', 'manual'))

    def test_multiple_runs_require_all_rejected_and_uncertain_runs_inherit_unsure(self):
        app = self.server.app
        added, _ = app.store.add_run('run_000001', 0, expected_revision=0)
        self.annotate('rejected')
        self.assertIsNone(app.review_project()['clips'][0]['label'])
        self.annotate('rejected', run_id=added)
        self.assertEqual(app.review_project()['clips'][0]['label'], 'no')
        self.annotate('accepted', uncertain=True, run_id=added)
        self.assertEqual(app.review_project()['clips'][0]['label'], 'uncertain')
        self.annotate('accepted')
        self.assertEqual(app.review_project()['clips'][0]['label'], 'yes')

    def test_changing_shared_folder_replaces_review_and_independent_open_is_rejected(self):
        app = self.server.app
        old_review = app.review_project()
        other = self.server.root / 'other_clips'
        import shutil
        shutil.copytree(self.server.clips, other)
        status, _, _ = self.server.request('POST', '/api/review/open', {'root':str(other)})
        self.assertEqual(status, 400)
        self.assertEqual(app.review_project()['root'], old_review['root'])
        app.open_project(str(other), str(self.server.root / 'other_annotations'))
        self.assertEqual(app.review_project()['root'], str(other))
        self.assertEqual(app.clip_review.clips.keys(), app.clips.keys())

    def test_reload_reads_disk_without_rewriting_labels_or_master(self):
        self.review.save(self.payload(clip_id='run_000001', label='yes'))
        self.annotate('rejected')
        self.server.app.review_project()
        # Simulate a later edit on disk after the running application's snapshot.
        master_path = self.server.master_path
        saved = json.loads(master_path.read_text())
        saved['clips']['run_000001']['annotation']['status'] = 'unreviewed'
        master_path.write_text(json.dumps(saved))
        labels_before = self.review.path.read_bytes()
        master_before = master_path.read_bytes()
        times = (self.review.path.stat().st_mtime_ns, master_path.stat().st_mtime_ns)
        status, _, content = self.server.request('GET', '/api/review')
        self.assertEqual(status, 200)
        clip = json.loads(content)['clips'][0]
        self.assertEqual((clip['label'], clip['label_source']), ('yes', 'manual'))
        self.assertEqual(self.review.path.read_bytes(), labels_before)
        self.assertEqual(master_path.read_bytes(), master_before)
        self.assertEqual((self.review.path.stat().st_mtime_ns,
                          master_path.stat().st_mtime_ns), times)

    def test_http_folder_switch_frame_access_and_independent_labels(self):
        server = self.server
        server.app.store.save_dances([], expected_revision=0)
        status, _, content = server.request('POST', '/api/review/open', {'root': str(self.server.clips)})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(content)['clips']), 2)
        status, _, content = server.request('PUT', '/api/review', self.payload(clip_id='run_000001', label='yes'))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['clips'][0]['label'], 'yes')
        status, headers, content = server.request('GET', '/api/review/frame/run_000001/2?root='+quote(str(self.review.root)))
        self.assertEqual(status, 200)
        self.assertIn('image/jpeg', headers)
        self.assertTrue(content.startswith(b'\xff\xd8'))
        status, _, _ = server.request('GET', '/api/review/frame/run_000001/2?root=/other')
        self.assertEqual(status, 400)
        status, _, _ = server.request('POST', '/api/review/open', {'root': '/missing'})
        self.assertEqual(status, 400)
        self.assertEqual(server.app.clip_review.root, self.review.root)
        status, _, _ = server.request('GET', '/api/pipeline')
        self.assertEqual(status, 404)
