"""Durable lifecycle regression checks on disposable SQLite and self-created files.

No provider object is initialized. The only executed pipeline method is the
existing local image ZIP exporter; generation checks stop after durable claim.
"""
import hashlib
import json
from pathlib import Path
import unittest
import uuid
import zipfile
from unittest.mock import Mock, patch

from PIL import Image
import test_workflow_execution as fixture
from models import db, Project, Task, User
from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob
from services.wuyu_state import apply_event, phase
from services.wuyu_template_review import choice_signature, current_template_choice
from services.wuyu_workflow_service import CancelledWork, WorkflowAPIError
from services.wuyu_workflow_quota import current_workflow_charge, recover_workflow_charges


class LifecycleTests(unittest.TestCase):
    tearDown = fixture.WorkflowExecutionTests.tearDown
    service = fixture.WorkflowExecutionTests.service

    def setUp(self):
        fixture.WorkflowExecutionTests.setUp(self)
        self.root = Path(self.app.config['UPLOAD_FOLDER'])
        self.root.mkdir(parents=True)
        catalog = self.root / 'catalog.json'
        catalog.write_text('{"schema_version":"1.0","sources":[]}', encoding='utf-8')
        self.app.config.update(WUYU_READING_ENABLED=True,
            WUYU_TEMPLATE_LIBRARY_PATH=str(catalog),
            WUYU_TEMPLATE_STORE_ROOT=str(self.root / 'template-store'),
            WUYU_PIPELINE_FACTORY=self.local_pipeline)
        self.workflow = self.service('combined')
        self.project = str(uuid.uuid4())
        db.session.add(User(id='lifecycle-owner', username='lifecycle-fixture', password_hash='unused-fixture-hash'))
        db.session.add(Project(id=self.project, user_id='lifecycle-owner', project_title='Self-created lifecycle fixture'))
        db.session.commit()
        self.prepare_generation()

    @staticmethod
    def local_pipeline(config):
        # Export exercises real code without constructing a gateway/client.
        from services.wuyu_pipeline import WuyuPipeline
        pipeline = object.__new__(WuyuPipeline)
        pipeline.config, pipeline.demo = config, True
        return pipeline

    def prepare_generation(self, policy='template_and_final'):
        def prepare(body):
            body['brief'].update(page_count=2, topic='Self-created lifecycle fixture')
            state = self.workflow._new_state(self.project, body['brief'], policy)
            state = apply_event(state, {'event_id': str(uuid.uuid4()), 'type': 'template.selected',
                'payload': {'template_id': 'self-created-template'}})
            state = apply_event(state, {'event_id': str(uuid.uuid4()), 'type': 'template.choice_confirmed',
                'payload': {'template_revision': state['template']['revision'], 'source_kind': 'upload'}})
            body['workflow'] = state
            body['slide_specs'] = [{'number': n, 'title': 'Fixture page ' + str(n),
                'copy': 'Self-created page content ' + str(n), 'points': []} for n in (1, 2)]
            body['outline'] = [{'number': spec['number'], 'title': spec['title']} for spec in body['slide_specs']]
            # This is a synthetic consent fixture, never consent on a live account.
            body['template_choice'] = {'source_kind': 'upload', 'source_id': 'self-created-template',
                'confirmed_via': 'template_confirmation_ui', 'signature': choice_signature(body)}
        self.workflow._mutate(self.project, prepare)
        body = self.body()
        self.assertTrue(current_template_choice(body))
        self.assertEqual(phase(body['workflow']), 'page_generation')

    def body(self):
        return self.workflow._read(self.project)[1]

    def action(self, action, payload=None, request_id=None):
        return self.workflow.execute_action(self.project, action, payload or {}, idempotency_key=request_id)

    def queued_job(self, result):
        job = db.session.get(WuyuWorkflowJob, result['job']['id'])
        self.assertEqual(job.status, 'queued')
        self.assertEqual(job.project_id, self.project)
        self.assertEqual(job.input_epoch, self.body()['epoch'])
        self.assertEqual(db.session.get(Task, job.task_id).status, 'PENDING')
        return job

    def explicit_generation_after(self, stopped_action, generation_action):
        self.action(stopped_action)
        before = self.body()
        self.assertTrue(before['paused'])
        payload = {'generation_requested': True} if generation_action == 'generate_images' else {}
        with patch('services.wuyu_workflow_quota.WorkflowCharge.reserve', side_effect=AssertionError('Claim must not charge')):
            result = self.action(generation_action, payload, request_id='explicit-generation')
            job = self.queued_job(result)
            self.assertEqual(job.action, generation_action)
            self.assertTrue(self.workflow._claim(job.id),
                'A valid new explicit generation must become claimable after ' + stopped_action)
        self.assertFalse(self.body()['paused'])
        self.assertEqual(self.body()['epoch'], before['epoch'] + 1)
        self.assertEqual(db.session.get(Task, job.task_id).status, 'PROCESSING')
        self.assertFalse(self.workflow._claim(job.id), 'A claimed job cannot be claimed twice')
        self.assertEqual(WuyuWorkflowJob.query.count(), 1)
        self.assertEqual(Task.query.count(), 1)

    def test_pause_then_explicit_generate_images_is_claimable(self):
        self.explicit_generation_after('pause', 'generate_images')

    def test_cancel_then_explicit_generate_images_is_claimable(self):
        self.explicit_generation_after('cancel', 'generate_images')

    def test_pause_then_explicit_generate_next_is_claimable(self):
        self.explicit_generation_after('pause', 'generate_next')

    def test_cancel_then_explicit_generate_next_is_claimable(self):
        self.explicit_generation_after('cancel', 'generate_next')

    def test_invalid_generation_cannot_unpause_or_enqueue(self):
        self.action('pause')
        before = self.workflow._read(self.project)
        for payload in ({}, {'generation_requested': False}, {'generation_requested': True, 'extra': True}):
            with self.subTest(payload=payload), self.assertRaises(WorkflowAPIError):
                self.action('generate_images', payload)
            self.assertEqual(self.workflow._read(self.project), before)
        self.assertEqual(WuyuWorkflowJob.query.count(), 0)
        self.assertEqual(Task.query.count(), 0)

    def test_paused_page_revision_waits_for_resume_and_reuses_one_continuation(self):
        self.action('pause')
        before = self.body()
        result = self.action('revise_page', {'number': 1, 'feedback': 'Move the self-created heading left.'})
        queued = self.queued_job(result)
        self.assertEqual(queued.action, '_continue')
        revised = self.body()
        self.assertTrue(revised['paused'])
        self.assertEqual(revised['epoch'], before['epoch'] + 1)
        self.assertEqual(revised['workflow']['pages'][0]['requested_change'], 'Move the self-created heading left.')
        self.assertFalse(self.workflow._claim(queued.id))
        with patch('services.wuyu_workflow_quota.WorkflowCharge.reserve', side_effect=AssertionError('Resume must not charge')):
            resumed = self.action('resume', request_id='resume-once')
            replay = self.action('resume', request_id='resume-once')
            again = self.action('resume')
        self.assertFalse(self.body()['paused'])
        for response in (resumed, replay, again):
            self.assertFalse(response['paused'])
            self.assertEqual(response['epoch'], revised['epoch'])
            self.assertEqual(response['workflow'], resumed['workflow'])
            self.assertEqual([job['id'] for job in response['jobs']], [queued.id])
            if 'job' in response:
                self.assertEqual(response['job']['id'], queued.id)
        # The continuation was created by revise_page, so its request key is
        # not the resume request key. Its replay may return only the snapshot.
        self.assertEqual(replay['revision'], resumed['revision'])
        self.assertEqual(WuyuWorkflowJob.query.count(), 1)
        self.assertEqual(Task.query.count(), 1)
        self.assertTrue(self.workflow._claim(queued.id))
        self.assertFalse(self.workflow._claim(queued.id))

    def test_pause_cancels_queued_task_and_resume_preserves_action_payload_once(self):
        first = self.queued_job(self.action('generate_images', {'generation_requested': True}, 'first'))
        original_payload = json.loads(first.payload)
        self.action('pause', request_id='pause-once')
        paused = self.body()
        self.assertEqual(paused['resume_action']['action'], 'generate_images')
        self.assertEqual(paused['resume_action']['payload'], original_payload)
        db.session.refresh(first)
        self.assertEqual(first.status, 'cancelled')
        self.assertTrue(first.cancel_requested)
        self.assertEqual(db.session.get(Task, first.task_id).status, 'FAILED')
        self.assertFalse(self.workflow._claim(first.id))
        self.action('pause', request_id='pause-once')
        self.assertEqual(self.body()['epoch'], paused['epoch'])
        resumed = self.action('resume', request_id='resume-request')
        repeated = self.action('resume', request_id='resume-request')
        replacement = self.queued_job(resumed)
        self.assertEqual(repeated['job']['id'], replacement.id)
        self.assertEqual(replacement.action, first.action)
        self.assertEqual(json.loads(replacement.payload)['generation_requested'], True)
        self.assertEqual(replacement.input_epoch, paused['epoch'])
        self.assertEqual(first.payload, json.dumps(original_payload, ensure_ascii=False, separators=(',', ':')))
        self.assertEqual(WuyuWorkflowJob.query.count(), 2)
        self.assertEqual(Task.query.count(), 2)
        self.assertTrue(self.workflow._claim(replacement.id))

    def test_explicit_generation_request_replay_and_conflict_do_not_duplicate_jobs(self):
        result = self.action('generate_images', {'generation_requested': True}, 'generation-request')
        job = self.queued_job(result)
        self.assertEqual(self.action('generate_images', {'generation_requested': True}, 'generation-request')['job']['id'], job.id)
        with self.assertRaises(WorkflowAPIError) as caught:
            self.action('generate_next', {}, 'generation-request')
        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(WuyuWorkflowJob.query.count(), 1)
        self.assertEqual(Task.query.count(), 1)

    def test_explicit_new_generation_supersedes_paused_continuation_and_resume_memo(self):
        original = self.queued_job(self.action('generate_next'))
        self.action('pause')
        self.assertEqual(self.body()['resume_action']['template_source_job_id'], original.id)
        continuation = self.queued_job(self.action('revise_page', {
            'number': 1, 'feedback': 'Use the corrected self-created page content.'}))
        self.assertEqual(continuation.action, '_continue')
        before = self.body()
        self.assertTrue(before['paused'])
        self.assertFalse(self.workflow._claim(continuation.id))
        result = self.action('generate_images', {'generation_requested': True}, 'new-generation')
        replacement = self.queued_job(result)
        after = self.body()
        self.assertFalse(after['paused'])
        self.assertNotIn('resume_action', after)
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertEqual(after['workflow']['pages'][0]['requested_change'],
            before['workflow']['pages'][0]['requested_change'])
        for prior in (original, continuation):
            db.session.refresh(prior)
            self.assertEqual(prior.status, 'cancelled')
            self.assertTrue(prior.cancel_requested)
            self.assertFalse(self.workflow._claim(prior.id))
            self.assertEqual(db.session.get(Task, prior.task_id).status, 'FAILED')
        self.assertEqual([job.id for job in WuyuWorkflowJob.query.filter_by(status='queued')], [replacement.id])
        self.assertEqual(self.action('generate_images', {'generation_requested': True}, 'new-generation')['job']['id'], replacement.id)
        self.assertTrue(self.workflow._claim(replacement.id))
        self.assertEqual(WuyuWorkflowJob.query.count(), 3)
        self.assertEqual(Task.query.count(), 3)

    def test_active_job_excludes_another_claim_in_the_same_project(self):
        first = self.queued_job(self.action('generate_images', {'generation_requested': True}))
        second = self.queued_job(self.action('generate_next'))
        self.assertTrue(self.workflow._claim(first.id))
        self.assertFalse(self.workflow._claim(second.id))
        self.assertEqual(db.session.get(WuyuWorkflow, self.project).active_job_id, first.id)
        self.assertEqual(db.session.get(WuyuWorkflowJob, second.id).status, 'queued')
        self.assertEqual(db.session.get(Task, second.task_id).status, 'PENDING')

    def test_cancelled_running_job_rejects_old_epoch_callback_before_charge_receipt(self):
        result = self.action('generate_images', {'generation_requested': True})
        job = self.queued_job(result)
        self.assertTrue(self.workflow._claim(job.id))
        self.workflow._event(self.project, 'page.generate_started', {'number': 1, 'attempt_id': 'old-attempt'}, job=job)
        self.action('cancel')
        before = self.workflow._read(self.project)
        charge = Mock()
        token = current_workflow_charge.set(charge)
        try:
            with self.assertRaises(CancelledWork):
                self.workflow._event(self.project, 'page.generated', {'number': 1, 'attempt_id': 'old-attempt',
                    'copy': 'Late self-created output', 'image_uri': 'artifact:late-output'}, job=job)
        finally:
            current_workflow_charge.reset(token)
        charge.mark_accepted.assert_not_called()
        self.assertEqual(self.workflow._read(self.project), before)
        with self.assertRaises(CancelledWork):
            self.workflow._check_cancel(job)
        self.assertTrue(db.session.get(WuyuWorkflowJob, job.id).cancel_requested)

    def reviewed_images(self):
        records = {}
        for number in (1, 2):
            path = self.root / self.project / ('page-' + str(number) + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (160, 90), (number * 40, 80, 120)).save(path)
            uri = self.workflow._record_artifact(self.project, path, 'slide_image', None, {'number': number})
            self.workflow._event(self.project, 'page.generate_started', {'number': number, 'attempt_id': 'fixture-' + str(number)})
            self.workflow._event(self.project, 'page.generated', {'number': number, 'attempt_id': 'fixture-' + str(number),
                'copy': 'Self-created page ' + str(number), 'image_uri': uri})
            records[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.workflow._event(self.project, 'images.confirmed', {'image_versions': {'1': 1, '2': 1}})
        self.assertEqual(phase(self.body()['workflow']), 'tracing')
        return records

    def test_paused_local_image_zip_runs_without_charge_and_preserves_original_images(self):
        images = self.reviewed_images()
        self.action('pause')
        paused_epoch = self.body()['epoch']
        with patch('services.wuyu_workflow_quota.WorkflowCharge.reserve', side_effect=AssertionError('Local export must not charge')):
            job = self.queued_job(self.action('export', {'format': 'images_zip'}))
            self.assertTrue(self.workflow._claim(job.id))
            self.assertFalse(self.workflow._check_cancel(job))
            self.workflow._run(job.id)
        db.session.refresh(job)
        self.assertEqual(job.status, 'completed', job.error_code)
        self.assertEqual(db.session.get(Task, job.task_id).status, 'COMPLETED')
        body = self.body()
        self.assertTrue(body['paused'])
        self.assertEqual(body['epoch'], paused_epoch)
        artifact = next(item for item in body['artifacts'] if item['kind'] == 'images_zip')
        with zipfile.ZipFile(self.root / artifact['relative_path']) as archive:
            self.assertEqual({name for name in archive.namelist() if name.endswith('.png')}, {'001.png', '002.png'})
        for path, digest in images.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_paused_native_export_still_waits_for_resume(self):
        self.reviewed_images()
        self.action('pause')
        job = self.queued_job(self.action('export', {'format': 'editable_pptx'}))
        self.assertFalse(self.workflow._claim(job.id))
        self.assertTrue(self.body()['paused'])
        self.assertEqual(db.session.get(Task, job.task_id).status, 'PENDING')

    def test_cancelled_queued_charge_is_refunded_once_by_recovery(self):
        from services.wuyu_quota import get_quota_service
        from models.wuyu_quota import QuotaLedger, QuotaReservation
        job = self.queued_job(self.action('generate_images', {'generation_requested': True}))
        quota = get_quota_service(self.app)
        reservation = quota.reserve('lifecycle-owner', 'self-created-hold', 'text', input_tokens=1,
            output_tokens=1, project_id=self.project, job_id=job.id)
        self.action('cancel', request_id='cancel-once')
        self.action('cancel', request_id='cancel-once')
        self.app.config['WUYU_QUOTA_ENABLED'] = True
        recover_workflow_charges(self.app)
        recover_workflow_charges(self.app)
        db.session.expire_all()
        hold = db.session.get(QuotaReservation, reservation['id'])
        self.assertEqual(hold.status, 'refunded')
        self.assertEqual(hold.actual_points, 0)
        events = QuotaLedger.query.filter_by(reservation_id=hold.id).all()
        self.assertEqual(sum(row.event == 'refund' for row in events), 1)
        self.assertEqual(sum(row.event == 'settle' for row in events), 0)
        self.assertEqual(WuyuWorkflowJob.query.count(), 1)
        self.assertEqual(Task.query.count(), 1)

    def test_cancel_after_accepted_output_settles_its_receipt_once_and_keeps_output(self):
        from services.wuyu_quota import get_quota_service
        from services.wuyu_workflow_quota import WorkflowCharge
        from models.wuyu_quota import QuotaLedger, QuotaReservation
        job = self.queued_job(self.action('generate_images', {'generation_requested': True}))
        self.assertTrue(self.workflow._claim(job.id))
        self.workflow._event(self.project, 'page.generate_started', {'number': 1, 'attempt_id': 'accepted-attempt'}, job=job)
        hold = get_quota_service(self.app).reserve('lifecycle-owner', 'accepted-self-created-hold', 'text',
            input_tokens=1, output_tokens=1, project_id=self.project, job_id=job.id)
        charge = WorkflowCharge(self.app, job, 'text')
        charge.reservation = hold
        with charge.accepting():
            self.workflow._event(self.project, 'page.generated', {'number': 1, 'attempt_id': 'accepted-attempt',
                'copy': 'Accepted self-created result', 'image_uri': 'artifact:accepted-fixture'}, job=job)
        self.assertTrue(charge.accepted)
        accepted = self.body()['workflow']['pages'][0]['artifact']
        self.action('cancel')
        self.app.config['WUYU_QUOTA_ENABLED'] = True
        # _run exits in its first cancellation guard, before constructing a pipeline.
        with patch.object(self.workflow, '_pipeline', side_effect=AssertionError('Cancelled work must not construct provider')):
            self.workflow._run(job.id)
        recover_workflow_charges(self.app)
        recover_workflow_charges(self.app)
        db.session.expire_all()
        reservation = db.session.get(QuotaReservation, hold['id'])
        self.assertEqual(reservation.status, 'settled')
        self.assertEqual(reservation.actual_points, hold['held_points'])
        events = QuotaLedger.query.filter_by(reservation_id=hold['id']).all()
        self.assertEqual(sum(row.event == 'settle' for row in events), 1)
        self.assertEqual(sum(row.event == 'refund' for row in events), 0)
        self.assertEqual(db.session.get(WuyuWorkflowJob, job.id).status, 'cancelled')
        self.assertEqual(self.body()['workflow']['pages'][0]['artifact'], accepted)


if __name__ == '__main__':
    unittest.main()
