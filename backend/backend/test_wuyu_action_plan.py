"""Task-intent precedence and host deduplication; no provider execution."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

import test_wuyu_lifecycle as fixture
from models import db, Task
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_action_state import (
    JobIntent, JobPlanDependencies, JobRetrySource, plan_action_job,
)


SCOPE_KEY = '__runtime_page_retry'
SOURCE_ID = '00000000-0000-0000-0000-000000000001'
TEMPLATE_SOURCE_ID = '00000000-0000-0000-0000-000000000002'


class JobPlanTests(unittest.TestCase):
    def body(self, policy='template_and_final'):
        return {'workflow': {'review_policy': policy}, 'retained': {'self_created': True}}

    def dependencies(self, reading=None, stage='page_generation'):
        return JobPlanDependencies(
            reading_queue_action=reading or (lambda body, action: None),
            phase=lambda state: stage,
            async_actions=frozenset({'generate_outline', 'generate_samples', 'generate_next',
                'generate_images', 'generate_focused', 'analyze_materials', 'revise_draft', 'export'}),
            read_actions=frozenset({'analyze_materials', 'revise_draft', 'continue_from_draft'}),
            page_retry_scope_key=SCOPE_KEY)

    def plan(self, body, action, payload, *, source=None, scope=None, deps=None):
        result = plan_action_job(body, action, payload, retry_source=source,
            retry_scope=scope, deps=deps or self.dependencies())
        self.assertIsInstance(result, JobIntent)
        return result

    def stored_payload(self):
        return {'generation_requested': True,
            '__runtime_snapshot': {'self_created_opaque_revision': 7},
            '__template_catalog_binding': {'version': 'a' * 64, 'sha256': 'a' * 64},
            '__design_context': {'self_created_visual_note': '保留原快照'}}

    def test_retry_deserializes_original_payload_and_replaces_only_server_scope(self):
        stored = self.stored_payload()
        stored[SCOPE_KEY] = {'number': 1, 'source_job_id': TEMPLATE_SOURCE_ID}
        encoded = json.dumps(stored, ensure_ascii=False)
        source = JobRetrySource(action='generate_images', payload_json=encoded, id=SOURCE_ID)
        scope = {'number': 2, 'source_job_id': SOURCE_ID}
        body = self.body()
        before = deepcopy(body)
        result = self.plan(body, 'retry', {'job_id': SOURCE_ID}, source=source, scope=scope)
        self.assertEqual(result.action, 'generate_images')
        self.assertEqual(result.payload, stored | {SCOPE_KEY: scope})
        self.assertIs(result.payload[SCOPE_KEY], scope)
        self.assertEqual(result.retry_of, SOURCE_ID)
        self.assertIsNone(result.template_source_job_id)
        self.assertEqual(source.payload_json, encoded)
        self.assertEqual(body, before)
        with self.assertRaises(FrozenInstanceError):
            source.action = 'generate_next'
        with self.assertRaises(FrozenInstanceError):
            result.action = '_continue'

    def test_resume_consumes_truthy_memo_once_and_keeps_payload_and_scope_lineage(self):
        payload = self.stored_payload()
        payload[SCOPE_KEY] = {'number': 2, 'source_job_id': SOURCE_ID}
        body = self.body()
        body['resume_action'] = {'action': 'generate_images', 'payload': payload,
            'template_source_job_id': TEMPLATE_SOURCE_ID}
        result = self.plan(body, 'resume', {})
        self.assertEqual(result.action, 'generate_images')
        self.assertIs(result.payload, payload)
        self.assertEqual(result.retry_of, SOURCE_ID)
        self.assertEqual(result.template_source_job_id, TEMPLATE_SOURCE_ID)
        self.assertNotIn('resume_action', body)
        self.assertEqual(body, self.body())
        second = self.plan(body, 'resume', {})
        self.assertEqual((second.action, second.payload), ('_continue', {}))
        self.assertIsNone(second.retry_of)
        self.assertIsNone(second.template_source_job_id)

    def test_empty_memos_no_task_actions_and_regular_actions_keep_existing_contracts(self):
        for memo in (None, {}):
            with self.subTest(memo=memo):
                body = self.body()
                body['resume_action'] = memo
                result = self.plan(body, 'resume', {})
                self.assertEqual((result.action, result.payload), ('_continue', {}))
                self.assertIn('resume_action', body)
                self.assertIs(body['resume_action'], memo)
        for action, policy, payload, expected in (
            ('update_brief', 'template_and_final', {'conversation_config_revision': 7}, None),
            ('generate_next', 'template_and_final', {}, 'generate_next'),
            ('confirm_page', 'guided', {'number': 1, 'image_version': 1}, 'generate_next'),
        ):
            with self.subTest(action=action, policy=policy):
                body = self.body(policy)
                body['resume_action'] = {'action': 'export', 'payload': {'format': 'images_zip'}}
                before = deepcopy(body)
                result = self.plan(body, action, payload)
                self.assertEqual(result.action, expected)
                if action in {'update_brief', 'generate_next'}:
                    self.assertIs(result.payload, payload)
                else:
                    self.assertEqual(result.payload, {})
                self.assertEqual(body, before)
                self.assertIsNone(result.retry_of)
                self.assertIsNone(result.template_source_job_id)

    def test_reading_projection_overrides_generic_plan_with_the_original_payload_rules(self):
        cases = (
            ('generate_samples', {'feedback': 'Self-created style feedback'}, 'analyze_materials', {}),
            ('analyze_materials', {'cache_only': True}, 'analyze_materials', {'cache_only': True}),
            ('continue_from_draft', {'draft_revision': 3, 'continue_requested': True}, 'generate_images',
                {'draft_revision': 3, 'continue_requested': True}),
            ('generate_focused', {'generation_requested': True}, 'generate_focused', {'generation_requested': True}),
        )
        for action, payload, reading_action, expected in cases:
            with self.subTest(action=action):
                body = self.body()
                before = deepcopy(body)
                calls = []
                def project(current, current_action):
                    self.assertIs(current, body)
                    calls.append(current_action)
                    return reading_action
                result = self.plan(body, action, payload, deps=self.dependencies(project))
                self.assertEqual((result.action, result.payload), (reading_action, expected))
                if action == 'analyze_materials':
                    self.assertIs(result.payload, payload)
                self.assertEqual(calls, [action])
                self.assertEqual(body, before)

    def test_overrides_keep_existing_lineage_and_research_has_final_priority(self):
        # The selector's injected projection may override an already inherited
        # intent. This checks lineage ownership at the pure boundary only.
        payload = self.stored_payload()
        payload[SCOPE_KEY] = {'number': 2, 'source_job_id': SOURCE_ID}
        body = self.body()
        body['resume_action'] = {'action': 'generate_images', 'payload': payload,
            'template_source_job_id': TEMPLATE_SOURCE_ID}
        projected = self.plan(body, 'resume', {},
            deps=self.dependencies(lambda current, action: 'analyze_materials'))
        self.assertEqual((projected.action, projected.payload), ('analyze_materials', {}))
        self.assertEqual((projected.retry_of, projected.template_source_job_id), (SOURCE_ID, TEMPLATE_SOURCE_ID))
        source = JobRetrySource('generate_images', json.dumps(self.stored_payload()), SOURCE_ID)
        for action in ('confirm_research', 'supplement_research'):
            for mode in ('web', 'deep'):
                with self.subTest(action=action, mode=mode):
                    body = self.body()
                    before = deepcopy(body)
                    result = self.plan(body, action, {'mode': mode, 'prompt': 'Self-created research question',
                        'acknowledge_cost': True}, source=source,
                        deps=self.dependencies(lambda current, current_action: 'analyze_materials'))
                    self.assertEqual((result.action, result.payload), ('run_research', {}))
                    self.assertEqual(result.retry_of, SOURCE_ID)
                    self.assertIsNone(result.template_source_job_id)
                    self.assertEqual(body, before)
        body = self.body()
        public = {'mode': 'materials', 'prompt': 'Self-created research question', 'acknowledge_cost': False}
        result = self.plan(body, 'confirm_research', public)
        self.assertIsNone(result.action)
        self.assertIs(result.payload, public)


class JobPlanIntegrationTests(unittest.TestCase):
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

    def test_host_reuses_paused_revision_continuation_with_empty_resume_memos_and_replay(self):
        with (patch('services.wuyu_providers.WuyuGateway', side_effect=AssertionError('No provider construction')) as provider,
              patch('services.wuyu_workflow_quota.WorkflowCharge.reserve', side_effect=AssertionError('No model reserve')) as charge):
            self.action('pause')
            pending = self.queued_job(self.action('revise_page', {
                'number': 1, 'feedback': 'Self-created revision waiting for resume.'}))
            self.assertEqual(pending.action, '_continue')
            epoch = self.body()['epoch']
            for index, memo in enumerate((None, {})):
                with self.subTest(memo=memo):
                    self.workflow._mutate(self.project, lambda body: body.update(resume_action=memo))
                    request_id = 'empty-memo-resume-' + str(index)
                    result = self.action('resume', request_id=request_id)
                    replay = self.action('resume', request_id=request_id)
                    self.assertEqual(result['job']['id'], pending.id)
                    self.assertEqual(replay['revision'], result['revision'])
                    self.assertEqual([job['id'] for job in replay['jobs']], [pending.id])
                    if 'job' in replay:
                        self.assertEqual(replay['job']['id'], pending.id)
                    body = self.body()
                    self.assertFalse(body['paused'])
                    self.assertEqual(body['epoch'], epoch)
                    self.assertIn('resume_action', body)
                    self.assertEqual(body['resume_action'], memo)
                    self.assertEqual((WuyuWorkflowJob.query.count(), Task.query.count()), (1, 1))
            self.assertEqual(db.session.get(WuyuWorkflowJob, pending.id).status, 'queued')
            self.assertEqual(db.session.get(Task, pending.task_id).status, 'PENDING')
            provider.assert_not_called()
            charge.assert_not_called()


if __name__ == '__main__':
    unittest.main()
