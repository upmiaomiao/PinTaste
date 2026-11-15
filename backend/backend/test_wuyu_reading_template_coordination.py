"""Real service/SQLite regressions for reading and visual-template serialization.

Only self-created files and durable fixture rows are used. No provider/reader
is constructed, no uploaded business data or runtime ENV is read.
"""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from werkzeug.datastructures import FileStorage
import test_wuyu_relayout as fixture
from models import db, ReferenceFile
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_workflow_contracts import WorkflowAPIError
from services.wuyu_reading_workflow import template_reading_guard


class ReadingTemplateCoordinationTests(unittest.TestCase):
    tearDown = fixture.RelayoutTests.tearDown

    def setUp(self):
        fixture.RelayoutTests.setUp(self)
        catalog = self.root / 'catalog.json'
        catalog.write_text('{"schema_version":"1.0","sources":[]}', encoding='utf-8')
        self.app.config.update(WUYU_TEMPLATE_LIBRARY_PATH=str(catalog),
                               WUYU_TEMPLATE_STORE_ROOT=str(self.root / 'template-store'),
                               WUYU_PIPELINE_FACTORY=lambda config: SimpleNamespace(config=config))

    def body(self):
        return self.service._read(self.project)[1]

    def reading_job(self, *, status='running', epoch=None, action='analyze_materials'):
        body = self.body()
        job = WuyuWorkflowJob(project_id=self.project, action=action, status=status,
                              input_epoch=body['epoch'] if epoch is None else epoch, payload='{}')
        db.session.add(job); db.session.commit()
        return job

    def label_reading(self, job):
        def set_label(body):
            body['reading'] = {'status': 'reading', '_owner_job_id': job.id,
                'source_revision': body.get('material_revision', 0), 'draft_revision': 1,
                'total_segments': 4, 'completed_segments': 1, 'coverage_complete': False,
                'source_reading_complete': False, 'issues': []}
        self.service._mutate(self.project, set_label)

    def assert_busy(self, action, payload):
        before = self.service._read(self.project)
        with self.assertRaises(WorkflowAPIError) as caught:
            self.service._reading_validate_action(before[1], action, payload)
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ('TEMPLATE_READING_IN_PROGRESS', 409))
        self.assertEqual(self.service._read(self.project), before)

    def test_running_reader_blocks_confirmation_before_style_or_epoch_change(self):
        job = self.reading_job(); self.label_reading(job)
        before = self.service._read(self.project)
        with patch.object(self.service, '_require_template_confirmation_ui'):
            with self.assertRaises(WorkflowAPIError) as caught:
                self.service.execute_action(self.project, 'confirm_template_choice', {'material_id': 'unused'})
        self.assertEqual(caught.exception.code, 'TEMPLATE_READING_IN_PROGRESS')
        self.assertEqual(self.service._read(self.project), before)
        db.session.refresh(job); self.assertFalse(job.cancel_requested)
        self.service._check_cancel(job)

    def test_queued_reader_blocks_visual_changes_even_without_reading_label(self):
        self.reading_job(status='queued')
        self.assert_busy('select_references', {})
        self.assert_busy('update_brief', {'style': 'A new explicit visual style'})

    def test_metadata_and_same_style_projection_do_not_cancel_reader(self):
        job = self.reading_job(); self.label_reading(job)
        before = self.body()
        self.service.execute_action(self.project, 'update_brief', {
            'conversation_config_revision': 7, 'style': before['brief']['style']})
        after = self.body()
        self.assertEqual(after['epoch'], before['epoch'])
        self.assertEqual(after['reading'], before['reading'])
        db.session.refresh(job); self.assertFalse(job.cancel_requested)
        self.service._check_cancel(job)

    def test_real_content_change_still_cancels_old_reader(self):
        job = self.reading_job(); self.label_reading(job)
        before = self.body()
        self.service.execute_action(self.project, 'update_brief', {'topic': 'Changed factual content goal'})
        db.session.refresh(job)
        self.assertTrue(job.cancel_requested)
        self.assertGreater(self.body()['epoch'], before['epoch'])
        self.assertEqual(self.body()['reading']['status'], 'idle')

    def test_template_upload_busy_rejects_before_opening_file(self):
        job = self.reading_job(); self.label_reading(job)
        before = self.service._read(self.project)
        with patch.object(Path, 'open', side_effect=AssertionError('No file write is allowed before busy rejection')):
            with self.assertRaises(WorkflowAPIError) as caught:
                self.service.add_materials(self.project,
                    [FileStorage(stream=io.BytesIO(b'fixture'), filename='fixture.png')], None, None, role='template')
        self.assertEqual(caught.exception.code, 'TEMPLATE_READING_IN_PROGRESS')
        self.assertEqual(ReferenceFile.query.count(), 0)
        self.assertEqual(self.service._read(self.project), before)

    def test_upload_cas_recheck_rolls_back_only_new_ref_and_file(self):
        before_files = {p for p in self.root.rglob('*') if p.is_file()}
        original_sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        original_mutate = self.service._mutate
        inserted = []
        def with_concurrent_reader(project_id, operation, *args, **kwargs):
            if not inserted:
                inserted.append(self.reading_job())
            return original_mutate(project_id, operation, *args, **kwargs)
        with patch.object(self.service, '_mutate', side_effect=with_concurrent_reader):
            with self.assertRaises(WorkflowAPIError) as caught:
                self.service.add_materials(self.project,
                    [FileStorage(stream=io.BytesIO(b'fixture'), filename='fixture.png')], None, None, role='template')
        self.assertEqual(caught.exception.code, 'TEMPLATE_READING_IN_PROGRESS')
        self.assertEqual(ReferenceFile.query.count(), 0)
        self.assertEqual({p for p in self.root.rglob('*') if p.is_file()}, before_files)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original_sha)
        self.assertFalse(inserted[0].cancel_requested)

    def test_pending_template_blocks_new_read_before_any_job_is_created(self):
        self.service._mutate(self.project, lambda body: body['materials'].append({
            'id': 'fixture-template', 'role': 'template', 'status': 'pending',
            'filename': 'fixture-template.png', 'bytes': 1, 'chunks': []}))
        before = self.service._read(self.project)
        for action in ['analyze_materials', 'revise_draft']:
            with self.assertRaises(WorkflowAPIError) as caught:
                self.service._reading_validate_job(before[1], action, {})
            self.assertEqual(caught.exception.code, 'TEMPLATE_PARSE_INCOMPLETE')
        with self.assertRaises(WorkflowAPIError) as caught:
            self.service.execute_action(self.project, 'analyze_materials', {})
        self.assertEqual(caught.exception.code, 'TEMPLATE_PARSE_INCOMPLETE')
        self.assertEqual(WuyuWorkflowJob.query.count(), 0)
        self.assertEqual(self.service._read(self.project), before)

    def test_active_parse_is_waitable_but_cancelled_parse_requires_explicit_recovery(self):
        self.service._mutate(self.project, lambda body: body['materials'].append({
            'id': 'fixture-template', 'role': 'template', 'status': 'processing',
            'filename': 'fixture-template.png', 'bytes': 1, 'chunks': []}))
        parsing = self.reading_job(action='_parse_materials')
        parsing.payload = json.dumps({'material_ids': ['fixture-template']}); db.session.commit()
        with self.assertRaises(WorkflowAPIError) as caught:
            self.service._reading_validate_action(self.body(), 'analyze_materials', {})
        self.assertEqual(caught.exception.code, 'TEMPLATE_PARSE_PENDING')
        self.service.execute_action(self.project, 'update_brief', {'topic': 'A genuine changed source goal'})
        parsing.status = 'cancelled'; db.session.commit()
        with self.assertRaises(WorkflowAPIError) as caught:
            self.service._reading_validate_action(self.body(), 'analyze_materials', {})
        self.assertEqual(caught.exception.code, 'TEMPLATE_PARSE_INCOMPLETE')
        prior_count = WuyuWorkflowJob.query.count()
        retry = self.service.execute_action(self.project, 'retry', {'job_id': parsing.id})
        self.assertEqual(WuyuWorkflowJob.query.count(), prior_count + 1)
        self.assertEqual(retry['job']['action'], '_parse_materials')
        with self.assertRaises(WorkflowAPIError) as caught:
            self.service._reading_validate_action(self.body(), 'analyze_materials', {})
        self.assertEqual(caught.exception.code, 'TEMPLATE_PARSE_PENDING')

    def test_terminal_cancelled_label_closes_even_after_epoch_changes(self):
        job = self.reading_job(); self.label_reading(job)
        self.service._mutate(self.project, lambda body: body.update(epoch=body['epoch'] + 1))
        self.service._reading_failed(job, 'CANCELLED_OR_INPUT_CHANGED')
        reading = self.body()['reading']
        self.assertEqual(reading['status'], 'draft_incomplete')
        self.assertEqual(reading['completed_segments'], 1)
        self.assertEqual(reading['failed_segments'], 3)
        self.assertEqual(reading['interruption']['error_code'], 'reading_cancelled')
        self.assertEqual(reading['interruption']['reason'], 'inputs_changed')

    def test_old_cancelled_owner_cannot_overwrite_new_reading_or_scope(self):
        old = self.reading_job(); self.label_reading(old)
        new = self.reading_job(); self.label_reading(new)
        before = self.service._read(self.project)
        self.service._reading_failed(old, 'CANCELLED_OR_INPUT_CHANGED')
        self.assertEqual(self.service._read(self.project), before)
        self.service._mutate(self.project, lambda body: body.update(material_revision=body.get('material_revision', 0) + 1))
        before = self.service._read(self.project)
        self.service._reading_failed(new, 'CANCELLED_OR_INPUT_CHANGED')
        self.assertEqual(self.service._read(self.project), before)

    def test_snapshot_repairs_legacy_terminal_label_without_persisting_or_retrying(self):
        job = self.reading_job(status='cancelled'); self.label_reading(job)
        before = self.service._read(self.project)
        view = self.service.snapshot(self.project)
        self.assertEqual(view['reading']['status'], 'draft_incomplete')
        self.assertEqual(view['reading']['interruption']['job_status'], 'cancelled')
        self.assertEqual(view['workflow']['phase'], 'draft_review')
        self.assertEqual(self.service._read(self.project), before)
        self.assertEqual(WuyuWorkflowJob.query.count(), 1)
        template_reading_guard(before[1])  # A stale label cannot lock template controls.

    def test_running_owner_is_still_projected_as_reading(self):
        job = self.reading_job(); self.label_reading(job)
        view = self.service.snapshot(self.project)
        self.assertEqual(view['reading']['status'], 'reading')
        self.assertNotIn('interruption', view['reading'])


if __name__ == '__main__':
    unittest.main()
