"""Action-state extraction contracts on the existing disposable SQLite fixture."""
from copy import deepcopy
import ast
from dataclasses import FrozenInstanceError, fields
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_wuyu_lifecycle as fixture
from models import db, Task
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_workflow_service import WorkflowAPIError


class ActionStateTests(unittest.TestCase):
    tearDown = fixture.LifecycleTests.tearDown
    service = fixture.LifecycleTests.service
    prepare_generation = fixture.LifecycleTests.prepare_generation
    body = fixture.LifecycleTests.body
    action = fixture.LifecycleTests.action
    queued_job = fixture.LifecycleTests.queued_job
    local_pipeline = staticmethod(fixture.LifecycleTests.local_pipeline)

    def setUp(self):
        fixture.LifecycleTests.setUp(self)
        self.app.config['WUYU_PROVIDER_STATUS'] = lambda: {'mode': 'self-created-test-stub'}
        for target in ('services.wuyu_providers.WuyuGateway',
                       'services.wuyu_workflow_quota.WorkflowCharge.reserve'):
            blocker = patch(target, side_effect=AssertionError('Action acceptance must not dispatch or reserve'))
            mock = blocker.start()
            self.addCleanup(blocker.stop)
            self.addCleanup(mock.assert_not_called)

    def test_state_only_request_replay_precedes_stale_revision_and_never_creates_task(self):
        revision, before = self.workflow._read(self.project)
        payload = {'conversation_config_revision': 7}
        first = self.workflow.execute_action(self.project, 'update_brief', payload,
            expected_revision=revision, idempotency_key='state-only-request')
        committed = self.workflow._read(self.project)
        replay = self.workflow.execute_action(self.project, 'update_brief', payload,
            expected_revision=revision, idempotency_key='state-only-request')
        self.assertEqual(replay, first)
        self.assertEqual(self.workflow._read(self.project), committed)
        self.assertEqual(committed[0], revision + 1)
        self.assertEqual(committed[1]['epoch'], before['epoch'])
        self.assertEqual(committed[1]['workflow'], before['workflow'])
        self.assertEqual(len(committed[1]['history']), len(before['history']) + 1)
        with self.assertRaises(WorkflowAPIError) as caught:
            self.workflow.execute_action(self.project, 'update_brief', {'conversation_config_revision': 8},
                idempotency_key='state-only-request')
        self.assertEqual((caught.exception.code, caught.exception.status), ('IDEMPOTENCY_CONFLICT', 409))
        self.assertEqual(self.workflow._read(self.project), committed)
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (0, 0))

    def test_interleaved_cas_change_rejects_action_without_new_state_job_or_task(self):
        revision, before = self.workflow._read(self.project)
        original = self.workflow._mutate
        interleaved = []
        def concurrent_change(project_id, operation, **kwargs):
            # A second actor commits after execute_action's public validation
            # read, but before its real database CAS operation begins.
            interleaved.append(original(project_id,
                lambda body: body['brief'].update(conversation_config_revision=13)))
            return original(project_id, operation, **kwargs)
        with patch.object(self.workflow, '_mutate', side_effect=concurrent_change):
            with self.assertRaises(WorkflowAPIError) as caught:
                self.workflow.execute_action(self.project, 'generate_images', {'generation_requested': True},
                    expected_revision=revision, idempotency_key='must-not-commit')
        self.assertEqual(caught.exception.code, 'REVISION_CONFLICT')
        self.assertEqual(len(interleaved), 1)
        latest_revision, latest = self.workflow._read(self.project)
        self.assertEqual(latest_revision, revision + 1)
        self.assertEqual(latest['brief']['conversation_config_revision'], 13)
        self.assertEqual(latest['epoch'], before['epoch'])
        self.assertEqual(latest['workflow'], before['workflow'])
        self.assertEqual(latest['history'], before['history'])
        self.assertNotIn('must-not-commit', latest['request_ids'])
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (0, 0))

    def test_failed_task_creation_does_not_commit_pause_epoch_or_old_job_cancellation(self):
        self.action('pause')
        previous = self.queued_job(self.action('revise_page', {
            'number': 1, 'feedback': 'Keep this self-created revision queued until resumed.'}))
        self.assertEqual(previous.action, '_continue')
        before = self.workflow._read(self.project)
        self.assertTrue(before[1]['paused'])
        Path(self.app.config['WUYU_TEMPLATE_LIBRARY_PATH']).write_text('invalid self-created catalog JSON', encoding='utf-8')
        with self.assertRaises(WorkflowAPIError) as caught:
            self.workflow.execute_action(self.project, 'generate_images', {'generation_requested': True},
                idempotency_key='failed-new-task')
        self.assertEqual(caught.exception.code, 'TEMPLATE_BINDING_INVALID')
        # The request's session teardown rolls back its uncommitted SQL work.
        db.session.rollback()
        self.assertEqual(self.workflow._read(self.project), before)
        db.session.refresh(previous)
        self.assertEqual(previous.status, 'queued')
        self.assertFalse(previous.cancel_requested)
        self.assertEqual(db.session.get(Task, previous.task_id).status, 'PENDING')
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (1, 1))
        self.assertNotIn('failed-new-task', self.body()['request_ids'])

    def test_visual_brief_change_preserves_plan_but_invalidates_confirmation_once(self):
        before = self.body()
        result = self.action('update_brief', {'style': 'A new self-created visual style', 'preserve_outline': True})
        after = self.body()
        for key in ('outline', 'slide_specs', 'materials', 'artifacts'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['brief']['style'], 'A new self-created visual style')
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertEqual(after['workflow']['requirements']['revision'], before['workflow']['requirements']['revision'] + 1)
        self.assertEqual(after['workflow']['template']['revision'], before['workflow']['template']['revision'] + 1)
        self.assertIsNone(after['workflow']['template']['confirmed_revision'])
        self.assertFalse(result['template_selection']['confirmed'])
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (0, 0))

    def test_page_copy_edit_cancels_old_queue_and_creates_current_epoch_continuation(self):
        old = self.queued_job(self.action('generate_images', {'generation_requested': True}))
        revision, before = self.workflow._read(self.project)
        first_page = deepcopy(before['workflow']['pages'][0])
        first_spec = deepcopy(before['slide_specs'][0])
        result = self.workflow.execute_action(self.project, 'revise_page', {
            'number': 2, 'feedback': 'Use this corrected self-created wording.',
            'copy': 'Corrected self-created page copy'}, expected_revision=revision,
            idempotency_key='correct-page-copy')
        queued = self.queued_job(result)
        after = self.body()
        self.assertEqual(queued.action, '_continue')
        self.assertEqual(after['workflow']['pages'][0], first_page)
        self.assertEqual(after['slide_specs'][0], first_spec)
        self.assertEqual(after['slide_specs'][1], before['slide_specs'][1] | {'copy': 'Corrected self-created page copy'})
        self.assertEqual(after['workflow']['pages'][1]['requested_change'], 'Use this corrected self-created wording.')
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertEqual(queued.input_epoch, after['epoch'])
        self.assertEqual(json.loads(queued.payload)['__runtime_requirements_snapshot']['input_epoch'], after['epoch'])
        db.session.refresh(old)
        self.assertEqual(old.status, 'cancelled')
        self.assertTrue(old.cancel_requested)
        self.assertEqual(db.session.get(Task, old.task_id).status, 'FAILED')
        self.assertEqual(db.session.get(Task, queued.task_id).status, 'PENDING')
        self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (2, 2))

    def test_action_state_module_has_only_explicit_frozen_ports_and_pure_handlers(self):
        from services import wuyu_action_state as module
        expected = ('new_state', 'validate_recipe', 'confirm_template_choice', 'patch_specs',
            'validate_specs', 'normalize_specs', 'bind_source_assets', 'validate_sample_promotion',
            'public_source', 'event_id', 'error_type', 'phase', 'current_relayout_source', 'apply_event')
        self.assertEqual(tuple(field.name for field in fields(module.ActionStateDependencies)), expected)
        self.assertTrue(module.ActionStateDependencies.__dataclass_params__.frozen)
        deps = module.ActionStateDependencies(**{name: (ValueError if name == 'error_type' else lambda *a, **k: None)
            for name in expected})
        with self.assertRaises(FrozenInstanceError):
            deps.event_id = lambda: 'changed'
        self.assertTrue(callable(module._update_brief))
        self.assertTrue(callable(module._update_outline))
        self.assertTrue(callable(module.apply_action_state))
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        imports = [name.name for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names]
        imports.extend(node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        self.assertFalse(any(name.split('.')[0] in {'models', 'sqlalchemy', 'flask'} or
            'wuyu_workflow_service' in name or 'wuyu_workflow_quota' in name for name in imports))
        self.assertFalse({'db', 'service', 'host', 'app', 'WorkflowCharge'} &
            {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})


if __name__ == '__main__':
    unittest.main()
