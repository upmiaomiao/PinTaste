"""Composition compatibility through the old facade, disposable SQLite and fake IO."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import is_dataclass
import hashlib
import inspect
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import has_app_context, request
from pptx import Presentation
from pptx.util import Inches
import test_wuyu_lifecycle as fixture
from models import db, Task
from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob
from services import wuyu_workflow_service as facade
from services.wuyu_errors import PipelineError
from services.wuyu_state import phase
from services.wuyu_workflow_quota import current_workflow_actor, current_workflow_charge
from services.wuyu_design_memory import current_design_context


class NativeFixture:
    """Synthetic traced objects and actual self-created PPTX files; no gateway."""
    def __init__(self, owner, config):
        self.owner, self.config = owner, dict(config)

    def trace_slide(self, source, spec, output_dir, **kwargs):
        number = spec['number']
        self.owner.trace_calls.append(number)
        if number in self.owner.trace_failures:
            raise PipelineError('invalid_trace', 'Self-created trace rejection')
        target = Path(output_dir) / 'self-created-manifest.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = {'objects': [{'id': 'self-object', 'kind': 'text', 'text': spec['title']}]}
        target.write_text(json.dumps(manifest), encoding='utf-8')
        return {'path': str(target), 'editable_manifest': manifest,
            'qa': {'passed': True, 'checks': {'self_created_objects': True}},
            'model_report': {'provider_called': False}}

    def export_deck(self, pages, format, output_path, cancel_check=None):
        self.owner.export_calls.append(format)
        if cancel_check:
            cancel_check()
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        deck = Presentation()
        for page in pages:
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2)).text_frame.text = page['spec']['title']
        deck.save(target)
        return {'path': str(target), 'qa': {'passed': True,
            'checks': {'page_count': len(Presentation(target).slides) == len(pages), 'self_created_fixture': True}}}


class ServiceComponentTests(unittest.TestCase):
    tearDown = fixture.LifecycleTests.tearDown
    service = fixture.LifecycleTests.service
    prepare_generation = fixture.LifecycleTests.prepare_generation
    body = fixture.LifecycleTests.body
    action = fixture.LifecycleTests.action
    queued_job = fixture.LifecycleTests.queued_job
    reviewed_images = fixture.LifecycleTests.reviewed_images
    local_pipeline = staticmethod(fixture.LifecycleTests.local_pipeline)

    def setUp(self):
        fixture.LifecycleTests.setUp(self)
        self.app.config['WUYU_PROVIDER_STATUS'] = lambda: {'mode': 'self-created-test-stub'}
        blocker = patch('services.wuyu_providers.WuyuGateway', side_effect=AssertionError('No provider construction'))
        self.gateway = blocker.start()
        self.addCleanup(blocker.stop)
        self.addCleanup(self.gateway.assert_not_called)

    def make_job(self, action='generate_images', payload=None):
        data = dict(payload or {})
        data.setdefault('__template_catalog_binding', self.workflow._job_catalog_binding())
        data.setdefault('__design_context', {'self_created_job_design': action})
        task = Task(project_id=self.project, task_type='WUYU_WORKFLOW', status='PENDING')
        db.session.add(task)
        db.session.flush()
        job = WuyuWorkflowJob(project_id=self.project, task_id=task.id, action=action,
            payload=json.dumps(data), input_epoch=self.body()['epoch'], actor_user_id='lifecycle-owner')
        db.session.add(job)
        db.session.commit()
        self.assertTrue(self.workflow._claim(job.id))
        return job

    def run_job(self, job):
        if job.status == 'queued':
            self.assertTrue(self.workflow._claim(job.id), 'Queued fixture must acquire real execution ownership')
        with patch('services.wuyu_identity.authorize_paid'):
            self.workflow._run(job.id)
        db.session.expire_all()
        return db.session.get(WuyuWorkflowJob, job.id)

    def test_identity_ports_and_charge_context_reach_a_real_accepted_event(self):
        from services import wuyu_workflow_contracts as contracts, wuyu_workflow_reports as reports
        for name in ('WorkflowAPIError', 'CancelledWork', '_UNCAPTURED_CATALOG', '_id', '_json', '_now'):
            self.assertIs(getattr(facade, name), getattr(contracts, name), name)
        for name in ('_safe_failure_code', '_failure_diagnostics', '_public_source', '_quality_report'):
            self.assertIs(getattr(facade, name), getattr(reports, name), name)
        self.assertIs(facade.current_workflow_actor, current_workflow_actor)
        self.assertIs(facade.current_workflow_charge, current_workflow_charge)
        self.assertGreaterEqual(len(self.workflow._components), 9)
        for component in self.workflow._components.values():
            self.assertTrue(is_dataclass(component.ports))
            self.assertTrue(component.ports.__dataclass_params__.frozen)
        job = self.make_job()
        self.workflow._event(self.project, 'page.generate_started', {'number': 1, 'attempt_id': 'self-attempt'}, job=job)
        charge = Mock()
        token = current_workflow_charge.set(charge)
        try:
            self.assertIs(facade.current_workflow_charge.get(), charge)
            self.workflow._event(self.project, 'page.generated', {'number': 1, 'attempt_id': 'self-attempt',
                'copy': 'Self-created accepted output', 'image_uri': 'artifact:self-created'}, job=job)
        finally:
            current_workflow_charge.reset(token)
        charge.mark_accepted.assert_called_once()
        self.assertEqual(charge.mark_accepted.call_args.args[0].id, job.id)
        self.assertEqual(self.body()['workflow']['pages'][0]['generation_status'], 'ready')

    def test_static_and_regular_method_class_calls_keep_the_original_signatures(self):
        cls = facade.WuyuWorkflowService
        valid = {'number': 1, 'feedback': 'Self-created edit'}
        for target in (cls._validate_page_revision_payload, self.workflow._validate_page_revision_payload):
            self.assertEqual(tuple(inspect.signature(target).parameters), ('payload', 'page_count'))
            self.assertIsNone(target(valid, 2))
            with self.assertRaises(facade.WorkflowAPIError):
                target({'number': True, 'feedback': 'invalid'}, 2)
        for target in (cls._diagram_preset_field, self.workflow._diagram_preset_field):
            self.assertEqual(tuple(inspect.signature(target).parameters), ('brief',))
            self.assertEqual(target({'creation_kind': 'pages'}), {})
        self.assertEqual(tuple(inspect.signature(cls._versions).parameters), ('self', 'body'))
        self.assertEqual(tuple(inspect.signature(self.workflow._versions).parameters), ('body',))
        self.assertEqual(cls._versions(self.workflow, self.body()), self.workflow._versions(self.body()))

    def test_instance_method_replacements_after_compose_are_used_across_domains(self):
        with patch.object(self.workflow, '_run') as run:
            self.workflow._run_context('self-created-job')
        run.assert_called_once_with('self-created-job')
        with patch.object(self.workflow, '_new_job_record', return_value=None) as create:
            self.action('generate_images', {'generation_requested': True})
        create.assert_called_once()
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (0, 0))
        job = SimpleNamespace(id='self-created-job', project_id=self.project,
            input_epoch=self.body()['epoch'], action='generate_images', payload='{}')
        with (patch.object(self.workflow, '_model_batch') as batch,
              patch.object(self.workflow, '_model_workers', return_value=1) as workers,
              patch.object(self.workflow, '_pipeline_recipe', return_value={'self_created': True}) as recipe):
            self.workflow._generate_batch(job)
        batch.assert_called_once()
        self.assertEqual(batch.call_args.args[1], [1, 2])
        workers.assert_called_once_with(job, 'image')
        recipe.assert_called_once()

    def test_main_helper_replacements_remain_late_bound(self):
        fixed = '00000000-0000-0000-0000-000000000122'
        with patch.object(facade, '_id', return_value=fixed) as ident:
            self.workflow._event(self.project, 'page.generate_started', {'number': 1, 'attempt_id': 'late-hook'})
        ident.assert_called_once()
        self.assertIn(fixed, self.body()['workflow']['applied_events'])
        job = SimpleNamespace(project_id=self.project, action='_continue')
        with (patch.object(facade, 'phase', return_value='requirements') as stage,
              patch.object(self.workflow, '_check_cancel'), patch.object(self.workflow, '_progress')):
            self.workflow._continue(job, object())
        stage.assert_called_once()
        with patch.object(facade, 'recover_workflow_charges') as recover:
            self.workflow.initialize()
        recover.assert_called_once_with(self.app)

    def test_live_config_hooks_and_stop_resource_do_not_replace_frozen_job_settings(self):
        for label, workers in (('first', 2), ('second', 7)):
            self.app.config.update(WUYU_IMAGE_WORKERS=workers,
                WUYU_PIPELINE_FACTORY=lambda config, label=label: SimpleNamespace(label=label, config=config),
                WUYU_PROVIDER_STATUS=lambda label=label: {'self_created': label})
            pipeline = self.workflow._pipeline()
            self.assertEqual((pipeline.label, pipeline.config['WUYU_IMAGE_WORKERS']), (label, workers))
            self.assertEqual(self.workflow.provider_status(), {'self_created': label})
        frozen = SimpleNamespace(_runtime_config={'WUYU_IMAGE_WORKERS': 3, 'WUYU_TRACE_WORKERS': 1})
        self.assertEqual(self.workflow._model_workers(frozen, 'image'), 3)
        self.assertEqual(self.workflow._model_workers(frozen, 'trace'), 1)
        self.assertEqual(self.workflow._model_workers(SimpleNamespace(), 'image'), 7)
        job = self.make_job()
        previous = self.workflow.stop_event
        try:
            self.workflow.stop_event = threading.Event()
            self.workflow.stop_event.set()
            with self.assertRaises(facade.CancelledWork):
                self.workflow._check_cancel(job)
        finally:
            self.workflow.stop_event = previous

    def test_run_dispatch_uses_current_facade_handlers_and_releases_context_and_lease(self):
        cases = [('_parse_materials', {'material_ids': []}, '_parse_materials'),
            ('run_research', {}, '_run_research'), ('analyze_materials', {}, '_reading_run'),
            ('revise_draft', {}, '_reading_run'), ('generate_outline', {}, '_outline'),
            ('generate_focused', {}, '_generate_focused'), ('generate_images', {}, '_generate_review_images'),
            ('generate_samples', {}, '_samples'), ('regenerate_samples', {}, '_samples'),
            ('generate_next', {}, '_continue'), ('_continue', {}, '_continue'),
            ('start_tracing', {}, '_trace_and_export'), ('export', {'format': 'editable_pptx'}, '_trace_and_export'),
            ('export', {'format': 'images_zip'}, '_export_additional'), ('_prepare_relayout', {}, '_prepare_relayout')]
        handlers = {case[2] for case in cases} | {'_generate_next'}
        pipeline = SimpleNamespace(config=dict(self.app.config))
        seen = []
        def handler(name):
            def invoke(job, *args, **kwargs):
                self.assertEqual(current_workflow_actor.get(), 'lifecycle-owner')
                self.assertEqual(current_design_context.get(), {'self_created_job_design': job.action})
                if name != '_prepare_relayout':
                    self.assertIs(args[0], pipeline)
                seen.append(name)
            return invoke
        actor_token = current_workflow_actor.set('outer-self-created-actor')
        design_token = current_design_context.set({'outer': True})
        try:
            with ExitStack() as stack:
                for name in handlers:
                    stack.enter_context(patch.object(self.workflow, name, side_effect=handler(name)))
                stack.enter_context(patch.object(self.workflow, '_research_validate'))
                factory = stack.enter_context(patch.object(self.workflow, '_pipeline', return_value=pipeline))
                stack.enter_context(patch.object(facade.WorkflowCharge, 'reserve', side_effect=AssertionError('Handlers are stubs')))
                for action, payload, expected in cases:
                    with self.subTest(action=action, format=payload.get('format')):
                        job = self.make_job(action, payload)
                        self.workflow._run(job.id)
                        self.assertEqual(seen[-1], expected)
                        db.session.expire_all()
                        self.assertEqual(db.session.get(WuyuWorkflowJob, job.id).status, 'completed')
                        self.assertEqual(db.session.get(Task, job.task_id).status, 'COMPLETED')
                        self.assertIsNone(db.session.get(WuyuWorkflow, self.project).active_job_id)
                        self.assertEqual(current_workflow_actor.get(), 'outer-self-created-actor')
                        self.assertEqual(current_design_context.get(), {'outer': True})
                self.assertEqual(factory.call_count, len(cases) - 1)
        finally:
            current_design_context.reset(design_token)
            current_workflow_actor.reset(actor_token)

    def test_run_failure_and_cancel_restore_context_and_terminal_task(self):
        for error, status, code in ((PipelineError('provider_timeout', 'self-created private detail'), 'failed', 'provider_timeout'),
                                    (facade.CancelledWork(), 'cancelled', 'CANCELLED_OR_INPUT_CHANGED')):
            with self.subTest(status=status):
                actor = current_workflow_actor.set('outer-actor')
                design = current_design_context.set({'outer': True})
                try:
                    job = self.make_job('generate_outline')
                    with patch.object(self.workflow, '_outline', side_effect=error):
                        self.workflow._run(job.id)
                    db.session.expire_all()
                    final = db.session.get(WuyuWorkflowJob, job.id)
                    self.assertEqual((final.status, final.error_code), (status, code))
                    self.assertEqual(db.session.get(Task, job.task_id).status, 'FAILED')
                    self.assertIsNone(db.session.get(WuyuWorkflow, self.project).active_job_id)
                    self.assertEqual(current_workflow_actor.get(), 'outer-actor')
                    self.assertEqual(current_design_context.get(), {'outer': True})
                    self.assertNotIn('self-created private detail', json.dumps(self.workflow.get_job(self.project, job.id)))
                finally:
                    current_design_context.reset(design)
                    current_workflow_actor.reset(actor)

    def charge_class(self, events):
        class Charge:
            def __init__(self, app, job, kind):
                self.number = len([e for e in events if e[0] == 'reserve']) + 1
            def reserve(self):
                events.append(('reserve', threading.get_ident()))
                return self
            @contextmanager
            def accepting(self, **kwargs):
                events.append(('accepting', threading.get_ident()))
                yield
            def settle(self, **kwargs):
                events.append(('settle', threading.get_ident()))
            def refund(self, *args, **kwargs):
                events.append(('refund', threading.get_ident()))
        return Charge

    def test_real_model_batch_keeps_orm_and_charges_on_owner_thread(self):
        job = self.make_job()
        owner = threading.get_ident()
        events, accepted, io_threads = [], [], []
        def prepare(number):
            self.assertEqual(threading.get_ident(), owner)
            self.assertIsNotNone(db.session.get(Task, job.task_id))
            return {'number': number, 'path': self.root / ('self-io-' + str(number) + '.bin')}
        def invoke(plan, pipeline, cancel):
            self.assertFalse(has_app_context())
            self.assertFalse(cancel())
            io_threads.append(threading.get_ident())
            plan['path'].write_bytes(b'self-created model IO file')
            return {'number': plan['number']}
        def accept(plan, output):
            self.assertEqual(threading.get_ident(), owner)
            self.assertIsNotNone(db.session.get(Task, job.task_id))
            accepted.append(output['number'])
        with patch.object(facade, 'WorkflowCharge', self.charge_class(events)):
            self.workflow._model_batch(job, [1, 2], prepare, invoke, accept,
                lambda *args: self.fail('No page should fail'), 2)
        self.assertEqual(sorted(accepted), [1, 2])
        self.assertTrue(io_threads and all(tid != owner for tid in io_threads))
        self.assertTrue(all(tid == owner for _, tid in events))
        self.assertEqual([kind for kind, _ in events].count('reserve'), 2)
        self.assertEqual([kind for kind, _ in events].count('settle'), 2)

    def test_real_batch_pause_before_acceptance_keeps_io_file_and_refunds_once(self):
        job = self.make_job()
        owner = threading.get_ident()
        started, release = threading.Event(), threading.Event()
        events, invoked, accepted = [], [], []
        target = self.root / 'self-created-drained-output.bin'
        original_check = self.workflow._check_cancel
        paused = []
        def check(current):
            if started.is_set() and not paused:
                self.assertEqual(threading.get_ident(), owner)
                paused.append(True)
                self.action('pause')
                release.set()
            return original_check(current)
        def invoke(plan, pipeline, cancel):
            self.assertFalse(has_app_context())
            invoked.append(plan['number'])
            started.set()
            self.assertTrue(release.wait(5), 'Owner must drain the in-flight fixture')
            target.write_bytes(b'self-created in-flight result retained after pause')
            self.assertFalse(cancel())
            return {'number': plan['number']}
        try:
            with (patch.object(facade, 'WorkflowCharge', self.charge_class(events)),
                  patch.object(self.workflow, '_check_cancel', side_effect=check)):
                with self.assertRaises(facade.CancelledWork):
                    self.workflow._model_batch(job, [1, 2], lambda n: {'number': n}, invoke,
                        lambda *args: accepted.append(args), lambda *args: self.fail('Cancelled work is not rejected as model error'), 1)
        finally:
            release.set()
        self.assertEqual(invoked, [1])
        self.assertEqual(accepted, [])
        self.assertTrue(target.is_file())
        self.assertEqual([kind for kind, _ in events].count('refund'), 1)
        self.assertNotIn('settle', [kind for kind, _ in events])
        self.assertTrue(all(tid == owner for _, tid in events))

    def configure_native(self):
        self.trace_calls, self.export_calls, self.trace_failures = [], [], set()
        self.app.config.update(WUYU_TRACE_WORKERS=1, WUYU_HYBRID_EXPORT_AFTER_SECONDS=3600,
            WUYU_PIPELINE_FACTORY=lambda config: NativeFixture(self, config))
        return self.reviewed_images()

    def test_native_trace_export_and_rebuild_preserve_images_and_do_not_retrace(self):
        images = self.configure_native()
        job = self.queued_job(self.action('start_tracing'))
        self.assertEqual(self.run_job(job).status, 'completed')
        self.assertEqual(self.trace_calls, [1, 2])
        self.assertEqual(phase(self.body()['workflow']), 'exported')
        traces = deepcopy([page['trace'] for page in self.body()['workflow']['pages']])
        rebuild = self.queued_job(self.action('export', {'format': 'editable_pptx', 'rebuild': True}))
        self.assertEqual(self.run_job(rebuild).status, 'completed')
        self.assertEqual(self.trace_calls, [1, 2])
        self.assertEqual([page['trace'] for page in self.body()['workflow']['pages']], traces)
        self.assertEqual(self.export_calls.count('editable_pptx'), 2)
        for item in self.body()['artifacts']:
            if item['kind'] == 'pptx':
                self.assertEqual(len(Presentation(self.root / item['relative_path']).slides), 2)
        for path, sha in images.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)

    def test_partial_native_retry_keeps_accepted_trace_and_retries_only_missing_page(self):
        images = self.configure_native()
        self.trace_failures = {2}
        first = self.queued_job(self.action('start_tracing'))
        self.assertEqual(self.run_job(first).status, 'failed')
        self.assertEqual(self.trace_calls, [1, 2])
        accepted = deepcopy(self.body()['workflow']['pages'][0]['trace'])
        self.assertIsNotNone(accepted)
        self.assertIsNone(self.body()['workflow']['pages'][1]['trace'])
        self.assertIn('hybrid_pptx', self.export_calls)
        self.assertNotIn('editable_pptx', self.export_calls)
        self.trace_failures.clear()
        retry = self.queued_job(self.action('retry', {'job_id': first.id}))
        self.assertEqual(self.run_job(retry).status, 'completed')
        self.assertEqual(self.trace_calls, [1, 2, 2])
        self.assertEqual(self.body()['workflow']['pages'][0]['trace'], accepted)
        for path, sha in images.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), sha)

    def test_controller_uses_facade_identity_and_preserves_authorization_and_error_status(self):
        from controllers.wuyu_workflow_controller import wuyu_workflow_bp
        self.app.extensions['wuyu_workflow'] = self.workflow
        self.app.register_blueprint(wuyu_workflow_bp)
        @self.app.before_request
        def identity():
            ident = request.headers.get('X-Self-User')
            request.current_user = SimpleNamespace(id=ident, role='user') if ident else None
        client = self.app.test_client()
        url = '/api/projects/' + self.project + '/workflow'
        self.assertEqual(client.get(url).status_code, 401)
        self.assertEqual(client.get(url, headers={'X-Self-User': 'other-self-user'}).status_code, 403)
        self.assertEqual(client.get(url, headers={'X-Self-User': 'lifecycle-owner'}).status_code, 200)
        response = client.post(url + '/action', headers={'X-Self-User': 'lifecycle-owner'},
            json={'action': 'revise_page', 'payload': {'number': True, 'feedback': 'Self-created invalid request'}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('PAGE_REVISION_INPUT_INVALID', response.get_data(as_text=True))
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (0, 0))


if __name__ == '__main__':
    unittest.main()
