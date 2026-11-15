"""Material-workflow page retries against disposable SQLite and fake image IO."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
import test_wuyu_lifecycle as fixture
from models import db, Project, Task
from models.wuyu_workflow import WuyuWorkflowJob
from models.wuyu_quota import QuotaLedger, QuotaReservation
from services.wuyu_errors import PipelineError
from services.wuyu_materials import parse_local_material
from services.wuyu_reading_partial import accepted_reading
from services.wuyu_template_review import choice_signature
from services.wuyu_workflow_quota import current_workflow_actor
from services.wuyu_workflow_service import WorkflowAPIError
from services.wuyu_state import apply_event


class PageRetryTests(unittest.TestCase):
    tearDown = fixture.LifecycleTests.tearDown
    service = fixture.LifecycleTests.service
    prepare_generation = fixture.LifecycleTests.prepare_generation
    body = fixture.LifecycleTests.body
    queued_job = fixture.LifecycleTests.queued_job
    local_pipeline = staticmethod(fixture.LifecycleTests.local_pipeline)

    def setUp(self):
        fixture.LifecycleTests.setUp(self)
        self.calls = []
        self.pipeline_configs = []
        self.failures = {2: 1}
        material_path = self.root / self.project / 'materials' / 'self-created.txt'
        material_path.parent.mkdir(parents=True)
        material_path.write_text('The self-created delivery process checks inputs before publishing.\n'
            'The reviewed process retains accepted work and retries failed pages.', encoding='utf-8')
        parsed = parse_local_material(material_path, self.app.config)
        material = {**parsed, 'id': str(uuid.uuid4()), 'role': 'material', 'status': 'completed',
            'relative_path': material_path.relative_to(self.root).as_posix(), 'bytes': material_path.stat().st_size,
            'text': '\n\n'.join(chunk['text'] for chunk in parsed['chunks'])}
        style_path = material_path.parent / 'self-created-style.png'
        Image.new('RGB', (160, 90), '#ddeeff').save(style_path)
        style_sha = hashlib.sha256(style_path.read_bytes()).hexdigest()
        template = {'id': str(uuid.uuid4()), 'role': 'template', 'status': 'completed',
            'filename': style_path.name, 'relative_path': style_path.relative_to(self.root).as_posix(),
            'sha256': style_sha, 'bytes': style_path.stat().st_size, 'chunks': [], 'source_assets': [],
            'template_images': [{'number': 1, 'relative_path': style_path.relative_to(self.root).as_posix(), 'sha256': style_sha}]}
        style_uri = self.workflow._record_artifact(self.project, style_path, 'uploaded_template_preview', None,
            {'material_id': template['id'], 'number': 1})
        template['template_images'][0]['artifact_id'] = style_uri.removeprefix('artifact:')
        def materials(body):
            body['materials'] = [material, template]
            body['reference_recipe'] = {'style_anchor': 'material:' + template['id'], 'references': []}
            self.workflow._reading_invalidate(body)
            # Synthetic consent fixture for self-created text, never live consent.
            body['reading'] = {'status': 'accepted', 'source_revision': body['material_revision'],
                'coverage_complete': True, 'draft_revision': 1, 'issues': []}
            body['template_choice'].update(source_id='material:' + template['id'], signature=choice_signature(body))
        self.workflow._mutate(self.project, materials)
        self.material_path = material_path
        self.material_sha = hashlib.sha256(material_path.read_bytes()).hexdigest()
        self.app.config.update(WUYU_QUOTA_ENABLED=True, WUYU_IMAGE_WORKERS=1, WUYU_PIPELINE_FACTORY=self.pipeline)
        self.assertTrue(accepted_reading(self.body()['reading'], self.body()['material_revision']))

    def action(self, action, payload=None, request_id=None):
        token = current_workflow_actor.set('lifecycle-owner')
        try:
            return self.workflow.execute_action(self.project, action, payload or {}, idempotency_key=request_id)
        finally:
            current_workflow_actor.reset(token)

    def pipeline(self, config):
        from services.wuyu_pipeline import WuyuPipeline
        pipeline = object.__new__(WuyuPipeline)
        self.pipeline_configs.append((config.get('WUYU_RUNTIME_SNAPSHOT', {}).get('settings_version'),
            config.get('WUYU_IMAGE_WORKERS')))
        pipeline.config, pipeline.demo = dict(config), False
        pipeline._hybrid_page_qa_cache = {}
        pipeline.gateway = types.SimpleNamespace(image=self.image, last_response_metadata={})
        return pipeline

    def image(self, prompt, images, cancel):
        payload = json.loads(prompt)
        number = payload['slide_spec']['number']
        self.calls.append(number)
        if self.failures.get(number, 0):
            self.failures[number] -= 1
            raise PipelineError('self_created_image_failure', 'Self-created image fixture failure')
        return Image.new('RGB', (160, 90), (number * 40, 90, 130))

    def run_job(self, job):
        self.assertTrue(self.workflow._claim(job.id))
        with patch('services.wuyu_identity.authorize_paid'):
            self.workflow._run(job.id)
        db.session.expire_all()
        return db.session.get(WuyuWorkflowJob, job.id)

    def failed_material_job(self):
        original = self.queued_job(self.action('generate_images', {'generation_requested': True}, 'material-images'))
        job = self.run_job(original)
        self.assertEqual(job.action, 'generate_images')
        self.assertEqual(job.status, 'failed', job.error_code)
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual([page['generation_status'] for page in self.body()['workflow']['pages']], ['ready', 'failed'])
        self.assertEqual(json.loads(job.progress)['failed_pages'], [2])
        return job

    def preserved_success(self):
        body = self.body()
        page = deepcopy(body['workflow']['pages'][0])
        artifact = next(item for item in body['artifacts'] if 'artifact:' + item['id'] == page['artifact']['image_uri'])
        path = self.root / artifact['relative_path']
        return {'page': page, 'artifact': deepcopy(artifact), 'path': path,
            'bytes': path.read_bytes(), 'attempts': deepcopy(body['workflow']['used_attempt_ids']),
            'events': deepcopy(body['workflow']['events'])}

    def assert_success_preserved(self, before):
        body = self.body()
        self.assertEqual(body['workflow']['pages'][0], before['page'])
        self.assertEqual(before['path'].read_bytes(), before['bytes'])
        self.assertEqual(next(item for item in body['artifacts'] if item['id'] == before['artifact']['id']), before['artifact'])
        self.assertEqual(body['workflow']['used_attempt_ids'][:len(before['attempts'])], before['attempts'])
        self.assertEqual(body['workflow']['events'][:len(before['events'])], before['events'])
        self.assertEqual(hashlib.sha256(self.material_path.read_bytes()).hexdigest(), self.material_sha)

    def test_material_generate_images_failure_can_retry_only_failed_page(self):
        original = self.failed_material_job()
        before = self.preserved_success()
        calls = len(self.calls)
        result = self.action('retry', {'number': 2}, 'failed-page-retry')
        retry = self.queued_job(result)
        self.assertEqual(retry.retry_of, original.id)
        self.assertEqual(json.loads(retry.payload)['__runtime_page_retry']['number'], 2)
        with (patch.object(self.workflow, '_trace_and_export', side_effect=AssertionError('Page retry must not trace')),
              patch.object(self.workflow, '_export_additional', side_effect=AssertionError('Page retry must not export'))):
            self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertEqual(self.calls[calls:], [2])
        self.assertEqual(self.body()['workflow']['pages'][1]['generation_status'], 'ready')
        self.assert_success_preserved(before)

    def test_nonfailed_and_invalid_page_numbers_reject_without_jobs_or_holds(self):
        self.failed_material_job()
        before = self.workflow._read(self.project)
        counts = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count())
        for number in (1, 0, -1, 3, True, '2', None):
            with self.subTest(number=number), self.assertRaises(WorkflowAPIError):
                self.action('retry', {'number': number})
            self.assertEqual(self.workflow._read(self.project), before)
            self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count()), counts)
        original = WuyuWorkflowJob.query.filter_by(project_id=self.project, action='generate_images').first()
        for selector in ({'number': 1, 'job_id': original.id}, {'number': 3, 'task_id': original.task_id}):
            with self.subTest(selector=selector), self.assertRaises(WorkflowAPIError):
                self.action('retry', selector)
            self.assertEqual(self.workflow._read(self.project), before)
            self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count()), counts)

    def test_replayed_page_retry_adds_no_second_job_hold_or_provider_call(self):
        self.failed_material_job()
        retry = self.queued_job(self.action('retry', {'number': 2}, 'one-page-request'))
        before_run = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count())
        self.assertEqual(self.action('retry', {'number': 2}, 'one-page-request')['job']['id'], retry.id)
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count()), before_run)
        with self.assertRaises(WorkflowAPIError):
            self.action('retry', {'number': 2}, 'different-page-request')
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count()), before_run)
        self.assertEqual(self.run_job(retry).status, 'completed')
        settled = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count(), len(self.calls))
        self.assertEqual(self.action('retry', {'number': 2}, 'one-page-request')['job']['id'], retry.id)
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count(), len(self.calls)), settled)
        holds = QuotaReservation.query.filter_by(job_id=retry.id).all()
        self.assertEqual(len(holds), 1)
        self.assertEqual((holds[0].kind, holds[0].status), ('image', 'settled'))
        self.assertEqual(QuotaLedger.query.filter_by(reservation_id=holds[0].id, event='settle').count(), 1)

    def test_client_cannot_forge_server_page_retry_scope(self):
        self.failed_material_job()
        before = self.workflow._read(self.project)
        counts = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count())
        for action, payload in (
            ('retry', {'number': 2, '__runtime_page_retry': {'number': 1}}),
            ('generate_images', {'generation_requested': True, '__runtime_page_retry': {'number': 2}}),
        ):
            with self.subTest(action=action), self.assertRaises(WorkflowAPIError):
                self.action(action, payload)
        self.assertEqual(self.workflow._read(self.project), before)
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), QuotaLedger.query.count()), counts)

    def test_explicit_job_id_retry_keeps_existing_whole_job_contract(self):
        original = self.failed_material_job()
        retry = self.queued_job(self.action('retry', {'job_id': original.id}, 'whole-job'))
        self.assertEqual(retry.action, original.action)
        self.assertEqual(retry.retry_of, original.id)
        payload = json.loads(retry.payload)
        self.assertTrue(payload['generation_requested'])
        self.assertNotIn('__runtime_page_retry', payload)
        self.assertEqual(payload['__template_catalog_binding'], json.loads(original.payload)['__template_catalog_binding'])

    def test_explicit_task_id_retry_keeps_existing_whole_job_contract(self):
        original = self.failed_material_job()
        retry = self.queued_job(self.action('retry', {'task_id': original.task_id}, 'whole-task'))
        self.assertEqual((retry.action, retry.retry_of), (original.action, original.id))
        self.assertTrue(json.loads(retry.payload)['generation_requested'])
        self.assertNotIn('__runtime_page_retry', json.loads(retry.payload))

    def test_claimed_retry_then_pause_dispatches_no_image_or_new_hold(self):
        self.failed_material_job()
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertTrue(self.workflow._claim(retry.id))
        calls, holds = len(self.calls), QuotaReservation.query.count()
        self.action('pause')
        with patch('services.wuyu_identity.authorize_paid'):
            self.workflow._run(retry.id)
        db.session.expire_all()
        self.assertEqual(db.session.get(WuyuWorkflowJob, retry.id).status, 'cancelled')
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(QuotaReservation.query.count(), holds)

    def test_claimed_retry_revalidates_page_change_before_any_image_hold(self):
        self.failed_material_job()
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertTrue(self.workflow._claim(retry.id))
        calls, holds = len(self.calls), QuotaReservation.query.count()
        # Exercise a valid state event without an epoch bump: the retry must
        # recheck the page snapshot in addition to the usual epoch guard.
        self.workflow._event(self.project, 'page.changed', {'number': 2, 'change': 'New self-created content.'})
        with patch('services.wuyu_identity.authorize_paid'):
            self.workflow._run(retry.id)
        db.session.expire_all()
        self.assertIn(db.session.get(WuyuWorkflowJob, retry.id).status, {'failed', 'cancelled'})
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(QuotaReservation.query.count(), holds)

    def test_selected_page_does_not_retry_another_failed_page(self):
        def three_pages(body):
            body['brief']['page_count'] = 3
            state = self.workflow._new_state(self.project, body['brief'], 'template_and_final')
            state = apply_event(state, {'event_id': str(uuid.uuid4()), 'type': 'template.selected',
                'payload': {'template_id': 'self-created-template'}})
            body['workflow'] = apply_event(state, {'event_id': str(uuid.uuid4()), 'type': 'template.choice_confirmed',
                'payload': {'template_revision': state['template']['revision'], 'source_kind': 'upload'}})
            body['slide_specs'].append({'number': 3, 'title': 'Fixture page 3', 'copy': 'Self-created page content 3', 'points': []})
            body['outline'].append({'number': 3, 'title': 'Fixture page 3'})
        self.workflow._mutate(self.project, three_pages)
        self.app.config['WUYU_IMAGE_WORKERS'] = 3
        self.failures = {2: 1, 3: 1}
        original = self.queued_job(self.action('generate_images', {'generation_requested': True}))
        self.assertEqual(self.run_job(original).status, 'failed')
        self.assertEqual(sorted(self.calls), [1, 2, 3])
        self.assertEqual([p['generation_status'] for p in self.body()['workflow']['pages']], ['ready', 'failed', 'failed'])
        before = self.preserved_success()
        other_failed = deepcopy(self.body()['workflow']['pages'][2])
        calls = len(self.calls)
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertEqual(self.calls[calls:], [2])
        self.assertEqual(self.body()['workflow']['pages'][2], other_failed)
        self.assert_success_preserved(before)
        self.assertEqual(QuotaReservation.query.filter_by(job_id=retry.id).count(), 1)

    def test_unknown_or_other_project_job_and_task_ids_are_not_retry_sources(self):
        self.failed_material_job()
        original_project = self.project
        self.project = str(uuid.uuid4())
        db.session.add(Project(id=self.project, user_id='lifecycle-owner', project_title='Other self-created project'))
        db.session.commit()
        try:
            self.prepare_generation()
            self.failures = {2: 1}
            foreign = self.queued_job(self.action('generate_images', {'generation_requested': True}))
            self.assertEqual(self.run_job(foreign).status, 'failed')
            foreign_job_id, foreign_task_id = foreign.id, foreign.task_id
        finally:
            self.project = original_project
        before = self.workflow._read(self.project)
        counts = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count())
        for selector in ({'job_id': foreign_job_id}, {'task_id': foreign_task_id},
                         {'job_id': str(uuid.uuid4())}, {'task_id': str(uuid.uuid4())}):
            with self.subTest(selector=selector), self.assertRaises(WorkflowAPIError):
                self.action('retry', selector)
            self.assertEqual(self.workflow._read(self.project), before)
            self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count()), counts)

    def test_page_retry_after_pause_keeps_valid_failure_and_resumes_only_target(self):
        original = self.failed_material_job()
        preserved = self.preserved_success()
        self.action('pause')
        paused_epoch = self.body()['epoch']
        self.assertTrue(self.body()['paused'])
        calls = len(self.calls)
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertEqual(retry.retry_of, original.id)
        self.assertFalse(self.body()['paused'])
        self.assertEqual(retry.input_epoch, paused_epoch)
        self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertEqual(self.calls[calls:], [2])
        self.assert_success_preserved(preserved)

    def test_paused_scoped_queue_resumes_with_its_retry_source_and_scope_once(self):
        original = self.failed_material_job()
        scoped = self.queued_job(self.action('retry', {'number': 2}))
        self.action('pause')
        db.session.refresh(scoped)
        self.assertEqual(scoped.status, 'cancelled')
        self.assertFalse(self.workflow._claim(scoped.id))
        calls = len(self.calls)
        resumed_result = self.action('resume', request_id='resume-page-scope')
        resumed = self.queued_job(resumed_result)
        self.assertEqual(resumed.retry_of, original.id)
        self.assertEqual(json.loads(resumed.payload)['__runtime_page_retry']['number'], 2)
        replay = self.action('resume', request_id='resume-page-scope')
        self.assertEqual(replay['job']['id'], resumed.id)
        self.assertEqual(self.run_job(resumed).status, 'completed')
        self.assertEqual(self.calls[calls:], [2])
        self.assertEqual(QuotaReservation.query.filter_by(job_id=scoped.id).count(), 0)
        self.assertEqual(QuotaReservation.query.filter_by(job_id=resumed.id).count(), 1)

    def test_changed_inputs_cannot_reuse_a_failure_from_before_the_edit(self):
        self.failed_material_job()
        self.action('pause')
        edited = self.queued_job(self.action('revise_page', {'number': 1, 'feedback': 'Use a new self-created heading.'}))
        self.assertEqual(edited.action, '_continue')
        self.action('cancel')
        db.session.refresh(edited)
        self.assertEqual(edited.status, 'cancelled')
        self.assertEqual(self.body()['workflow']['pages'][1]['generation_status'], 'failed')
        before = self.workflow._read(self.project)
        counts = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count())
        with self.assertRaises(WorkflowAPIError):
            self.action('retry', {'number': 2})
        self.assertEqual(self.workflow._read(self.project), before)
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count()), counts)

    def test_old_scope_rejects_a_later_failure_with_unchanged_page_versions(self):
        original = self.failed_material_job()
        stale = self.queued_job(self.action('retry', {'number': 2}))
        before_page = deepcopy(self.body()['workflow']['pages'][1])
        whole = self.queued_job(self.action('retry', {'job_id': original.id}))
        self.assertNotIn('__runtime_page_retry', json.loads(whole.payload))
        self.failures[2] = 1
        self.assertEqual(self.run_job(whole).status, 'failed')
        after_page = self.body()['workflow']['pages'][1]
        self.assertEqual(after_page['input_revision'], before_page['input_revision'])
        self.assertEqual(after_page['image_version'], before_page['image_version'])
        calls, holds = len(self.calls), QuotaReservation.query.count()
        result = self.run_job(stale)
        self.assertIn(result.status, {'failed', 'cancelled'})
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(QuotaReservation.query.count(), holds)

    def test_job_and_task_retries_of_failed_scopes_cannot_expand_to_whole_batch(self):
        self.failed_material_job()
        scoped = self.queued_job(self.action('retry', {'number': 2}))
        self.failures[2] = 1
        self.assertEqual(self.run_job(scoped).status, 'failed')
        for key in ('job_id', 'task_id'):
            calls = len(self.calls)
            selector = scoped.id if key == 'job_id' else scoped.task_id
            next_retry = self.queued_job(self.action('retry', {key: selector}))
            self.assertEqual(json.loads(next_retry.payload)['__runtime_page_retry']['number'], 2)
            self.failures[2] = 1 if key == 'job_id' else 0
            self.assertEqual(self.run_job(next_retry).status, 'failed' if key == 'job_id' else 'completed')
            self.assertEqual(self.calls[calls:], [2])
            self.assertEqual(QuotaReservation.query.filter_by(job_id=next_retry.id).count(), 1)
            scoped = next_retry

    def test_retry_keeps_original_catalog_binding_after_active_catalog_changes(self):
        from services.wuyu_template_library import TemplateLibrary, capture_catalog_binding
        from services.wuyu_template_import import import_public_templates
        original = self.failed_material_job()
        old_binding = deepcopy(json.loads(original.payload)['__template_catalog_binding'])
        assets = self.root / 'self-created-catalog-import'
        assets.mkdir()
        image = assets / 'reference.png'
        Image.new('RGB', (320, 180), '#eef4ff').save(image)
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        target = 'original-previews/' + digest + '.png'
        record = {'id': 'self-created-new-catalog', 'title': 'Self-created reference',
            'source_type': 'local', 'neutralized': False, 'preview_kind': 'original_template_render',
            'preview_disclaimer': 'Self-created visual reference, not project evidence',
            'reference_scope': 'single_image', 'cover': target, 'warnings': [],
            'pages': [{'id': 'self-created-new-catalog:p1', 'page_number': 1, 'image': target,
                'title': 'Self-created reference', 'selected': True, 'is_body_reference': True, 'layout_family': 'flow'}],
            'tags': {'purpose': ['技术方案'], 'page_role': ['技术路线'], 'visual_style': ['蓝白'],
                'information_density': ['中'], 'layout_structure': ['流程图'],
                'industry_topic': ['通用'], 'template_category': ['技术路线']}}
        imported = import_public_templates(TemplateLibrary(self.app), {'entries': [{'record': record,
            'files': [{'source': image.name, 'target': target, 'sha256': digest}]}]}, assets)
        self.assertEqual(imported['added'], 1)
        self.assertNotEqual(capture_catalog_binding(self.app.config), old_binding)
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertEqual(json.loads(retry.payload)['__template_catalog_binding'], old_binding)
        calls = len(self.calls)
        self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertEqual(self.calls[calls:], [2])

    def test_missing_bound_catalog_fails_without_falling_back_or_reserving(self):
        from services.wuyu_template_library import resolve_catalog_binding
        original = self.failed_material_job()
        binding = json.loads(original.payload)['__template_catalog_binding']
        resolve_catalog_binding(self.app.config, binding).unlink()
        counts = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), len(self.calls))
        with self.assertRaises(WorkflowAPIError) as caught:
            self.action('retry', {'number': 2})
        self.assertEqual(caught.exception.code, 'TEMPLATE_BINDING_INVALID')
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), len(self.calls)), counts)

    def test_runtime_revision_is_inherited_instead_of_current_profile_settings(self):
        from services.wuyu_runtime_settings import get_runtime_settings, update_runtime_settings
        self.app.config.update(WUYU_RUNTIME_SETTINGS_PATH=str(self.directory / 'self-created-runtime.json'),
            WUYU_IMAGE_BASE_URL='https://self-fixture.invalid/v1', WUYU_IMAGE_API_KEY='self-created-unused-key')
        current = get_runtime_settings(self.app.config)
        update_runtime_settings(self.app.config, {'expected_revision': current['settings_version'], 'image_workers': 1})
        original = self.failed_material_job()
        old = deepcopy(json.loads(original.payload)['__runtime_snapshot'])
        update_runtime_settings(self.app.config, {'expected_revision': old['settings_version'], 'image_workers': 3})
        self.pipeline_configs.clear()
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertEqual(json.loads(retry.payload)['__runtime_snapshot'], old)
        self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertTrue(self.pipeline_configs)
        self.assertTrue(all(version == old['settings_version'] and workers == 1 for version, workers in self.pipeline_configs))

    def test_claimed_retry_rejects_a_new_current_image_version_before_dispatch(self):
        self.failed_material_job()
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertTrue(self.workflow._claim(retry.id))
        old_version = self.body()['workflow']['pages'][1]['image_version']
        existing_image = next(item for item in self.body()['artifacts'] if item['kind'] == 'uploaded_template_preview')
        self.workflow._event(self.project, 'page.generate_started', {'number': 2, 'attempt_id': 'newer-self-created-image'})
        self.workflow._event(self.project, 'page.generated', {'number': 2, 'attempt_id': 'newer-self-created-image',
            'copy': 'Self-created newer accepted output', 'image_uri': 'artifact:' + existing_image['id']})
        self.assertEqual(self.body()['workflow']['pages'][1]['image_version'], old_version + 1)
        calls, holds = len(self.calls), QuotaReservation.query.count()
        with patch('services.wuyu_identity.authorize_paid'):
            self.workflow._run(retry.id)
        db.session.expire_all()
        self.assertIn(db.session.get(WuyuWorkflowJob, retry.id).status, {'failed', 'cancelled'})
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(QuotaReservation.query.count(), holds)

    def remove_failure_provenance_for_legacy_fixture(self):
        def legacy(body):
            latest = next(event for event in reversed(body['workflow']['events'])
                if event.get('type') == 'page.failed' and event.get('payload', {}).get('number') == 2)
            self.assertIn('__page_retry_source', latest['payload'])
            latest['payload'].pop('__page_retry_source')
        self.workflow._mutate(self.project, legacy)

    def test_legacy_failure_uses_actual_same_epoch_job_timestamps_and_progress(self):
        original = self.failed_material_job()
        proof = (original.started_at, original.completed_at, original.progress, original.input_epoch)
        self.remove_failure_provenance_for_legacy_fixture()
        calls = len(self.calls)
        retry = self.queued_job(self.action('retry', {'number': 2}))
        self.assertEqual(retry.retry_of, original.id)
        self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertEqual(self.calls[calls:], [2])
        db.session.refresh(original)
        self.assertEqual((original.started_at, original.completed_at, original.progress, original.input_epoch), proof)

    def test_legacy_failure_without_provenance_cannot_cross_a_pause_epoch(self):
        self.failed_material_job()
        self.remove_failure_provenance_for_legacy_fixture()
        self.action('pause')
        before = self.workflow._read(self.project)
        counts = (WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), len(self.calls))
        with self.assertRaises(WorkflowAPIError):
            self.action('retry', {'number': 2})
        self.assertEqual(self.workflow._read(self.project), before)
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count(), QuotaReservation.query.count(), len(self.calls)), counts)


if __name__ == '__main__':
    unittest.main()
