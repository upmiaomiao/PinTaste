"""Real backend stage handoff for source-only ACK versus changed requirements.

Synthetic materials/cache and local temporary SQLite only. Host registration,
native-source provenance and empty-patch projection are tested separately in the
paired Host fixture; here its actual marker-only projection enters the real
workflow service, including reading invalidation and continue validation.
"""
from copy import deepcopy
import unittest
import hashlib
from PIL import Image
import uuid
from unittest.mock import patch

import test_wuyu_reading_replan as fixture
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_reading import encoded, reader_acknowledgement, sha
from services.wuyu_reading_executor import _write
from services.wuyu_workflow_service import WorkflowAPIError


class DraftConfirmationBoundaryTests(unittest.TestCase):
    raw_reader = staticmethod(fixture.ReadingReplanTests.raw_reader)
    tearDown = fixture.ReadingReplanTests.tearDown
    cache_root = fixture.ReadingReplanTests.cache_root
    change = fixture.ReadingReplanTests.change
    run_cached_reading = fixture.ReadingReplanTests.run_cached_reading

    def setUp(self):
        fixture.ReadingReplanTests.setUp(self)
        # An uploaded self-created template is available but unconfirmed:
        # continuing builds the outline without authorizing image generation.
        template = self.root / self.project / 'materials' / 'self-created-template.png'
        Image.new('RGB', (16, 9), 'white').save(template)
        artifact = self.service._record_artifact(self.project, template, 'template_preview', None)
        self.service._mutate(self.project, lambda body: body['materials'].append({
            'id': str(uuid.uuid4()), 'role': 'template', 'status': 'completed',
            'filename': 'self-created-template.png', 'relative_path': template.relative_to(self.root).as_posix(),
            'sha256': hashlib.sha256(template.read_bytes()).hexdigest(), 'bytes': template.stat().st_size,
            'template_images': [{'page': 1, 'relative_path': template.relative_to(self.root).as_posix(),
                                 'artifact_id': artifact.removeprefix('artifact:')}]}))
        self.change(page_count=2)
        for task in self.original_plan['tasks']:
            value = {**reader_acknowledgement(task), 'findings': [
                {'segment_id': s['id'], 'quote': s['text'], 'summary': s['text'],
                 'topic': '原文证据', 'relevance': 3, 'source_asset_ids': [], 'data_ids': []}
                for s in task['segments']], 'issues': []}
            raw = encoded(value)
            _write(self.cache_root(task) / 'response.json', {
                'task_id': task['id'], 'raw': raw, 'raw_sha256': sha(raw), 'finish_reason': 'stop'})
        self.ready = self.run_cached_reading()
        self.assertEqual(self.ready['reading']['status'], 'draft_ready')
        self.assertTrue(self.ready['reading']['content_plan_complete'])
        self.original_revision = self.service._read(self.project)[0]
        self.jobs_before = WuyuWorkflowJob.query.filter_by(project_id=self.project).count()

    def continue_draft(self, draft_revision=None):
        revision, body = self.service._read(self.project)
        return self.service.execute_action(self.project, 'continue_from_draft', {
            'draft_revision': draft_revision or body['reading']['draft_revision'],
            'continue_requested': True}, expected_revision=revision,
            idempotency_key=str(uuid.uuid4()))

    def test_marker_only_ack_preserves_reading_and_allows_real_continue(self):
        before = deepcopy(self.ready)
        with patch.object(self.service, '_pipeline', side_effect=AssertionError('PROVIDER_FORBIDDEN')):
            self.change(conversation_config_revision=17)
            _, acknowledged = self.service._read(self.project)
            self.assertEqual(acknowledged['reading'], before['reading'])
            self.assertEqual(acknowledged['material_revision'], before['material_revision'])
            result = self.continue_draft()
        _, body = self.service._read(self.project)
        self.assertEqual(body['reading']['status'], 'accepted')
        self.assertEqual(len(body['outline']), 2)
        self.assertEqual(body['reading']['accepted_draft_revision'], before['reading']['draft_revision'])
        self.assertEqual(WuyuWorkflowJob.query.filter_by(project_id=self.project).count(), self.jobs_before)

    def test_rewriting_approval_into_requirements_reproduces_the_latest_rejection(self):
        draft = self.ready['reading']['draft_revision']
        self.change(requirements='Use this completed draft and continue making the presentation.')
        _, changed = self.service._read(self.project)
        self.assertEqual(changed['reading']['status'], 'idle')
        self.assertEqual(changed['material_revision'], self.ready['material_revision'] + 1)
        with self.assertRaises(WorkflowAPIError) as caught:
            self.continue_draft(draft)
        self.assertEqual(caught.exception.code, 'DRAFT_NOT_READY')
        self.assertEqual(WuyuWorkflowJob.query.filter_by(project_id=self.project).count(), self.jobs_before)

    def test_actual_content_change_still_invalidates_the_draft(self):
        self.change(requirements='Compare the delivery process with a newly requested alternative.')
        _, changed = self.service._read(self.project)
        self.assertEqual(changed['reading']['status'], 'idle')
        self.assertFalse(changed['reading']['coverage_complete'])
        with self.assertRaises(WorkflowAPIError):
            self.continue_draft(self.ready['reading']['draft_revision'])
        self.assertEqual(WuyuWorkflowJob.query.filter_by(project_id=self.project).count(), self.jobs_before)

    def test_marker_only_ack_does_not_accept_an_invented_draft_revision(self):
        self.change(conversation_config_revision=18)
        with self.assertRaises(WorkflowAPIError) as caught:
            self.continue_draft(self.ready['reading']['draft_revision'] + 1)
        self.assertEqual(caught.exception.code, 'DRAFT_REVISION_CONFLICT')
        _, body = self.service._read(self.project)
        self.assertEqual(body['reading']['status'], 'draft_ready')
        self.assertEqual(WuyuWorkflowJob.query.filter_by(project_id=self.project).count(), self.jobs_before)


if __name__ == '__main__':
    unittest.main()
