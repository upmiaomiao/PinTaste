"""Explicit material scope preserves failed research. Local SQLite/cache, zero providers."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import request
from models import db
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_deep_research import signature, research_public, research_guidance
from services.wuyu_workflow_service import WorkflowAPIError
import test_wuyu_reading_replan as reading_fixture


class ResearchMaterialRecoveryTests(unittest.TestCase):
    tearDown = reading_fixture.ReadingReplanTests.tearDown
    cache_root = reading_fixture.ReadingReplanTests.cache_root
    raw_reader = staticmethod(reading_fixture.ReadingReplanTests.raw_reader)
    run_cached_reading = reading_fixture.ReadingReplanTests.run_cached_reading
    assert_cache_unchanged = reading_fixture.ReadingReplanTests.assert_cache_unchanged

    def setUp(self):
        reading_fixture.ReadingReplanTests.setUp(self)
        self.install()

    def install(self, status='failed', count=25, report=True):
        def initialize(body):
            sources = [{'id': 'S'+str(n), 'url': 'https://example.test/synthetic/'+str(n),
                        'title': 'Self-created '+str(n), 'text': 'Retained unreviewed evidence '+str(n)} for n in range(count)]
            body['research'] = {'requested': True, 'signature': signature(body), 'mode': 'deep', 'status': status,
                'prompt': 'Self-created public question', 'report_id': 'self-research-170',
                'partial_sources': sources, '_steps': {'keep_raw': {'status': 'completed', 'result': {'raw': 'Do not remove'}}},
                'progress': {'source_count': count, 'model_calls': 3, 'reserved_tokens': 12000,
                             'operations': [{'kind': 'search', 'status': 'completed', 'query': 'Synthetic'}]},
                'last_error': {'code': 'research_synthesis_invalid', 'cost': 'unknown'}}
            if report:
                body['research']['report'] = {'partial': True, 'summary': '', 'findings': [], 'sources': deepcopy(sources)}
            body['paused'] = status == 'paused'
            body['resume_action'] = {'action': 'run_research', 'payload': {}}
        self.service._mutate(self.project, initialize)

    @contextmanager
    def ui(self, actor='owner', header=True):
        with self.app.test_request_context(headers={'X-Wuyu-Interaction': 'research-confirmation'} if header else {}):
            request.current_user = SimpleNamespace(id=actor, role='user')
            yield

    def payload(self):
        state = self.service._read(self.project)[1]['research']
        return {'mode': 'materials', 'prompt': state['prompt'], 'acknowledge_cost': False,
                'preserve_research': True, 'report_id': state['report_id']}

    def recover(self, payload=None, revision=None, key='self-recovery-170'):
        revision = self.service._read(self.project)[0] if revision is None else revision
        return self.service.execute_action(self.project, 'confirm_research', payload or self.payload(),
                                           expected_revision=revision, idempotency_key=key)

    def test_explicit_local_choice_keeps_every_research_field_and_upload(self):
        _, before = self.service._read(self.project)
        with self.ui(), patch.object(self.service, '_pipeline', side_effect=AssertionError('No provider')):
            result = self.recover()
        _, after = self.service._read(self.project)
        receipt = after['research'].pop('materials_confirmation')
        self.assertEqual(after['research'], before['research'])
        self.assertEqual(after['materials'], before['materials'])
        self.assertEqual(after['epoch'], before['epoch'] + 1)
        self.assertEqual(receipt['report_id'], before['research']['report_id'])
        self.assertEqual(receipt['confirmed_via'], 'research_confirmation_ui')
        self.assertEqual(WuyuWorkflowJob.query.count(), 0)
        self.assertTrue(result['research']['materials_only_current'])
        self.assertEqual(result['research']['status'], 'failed')

    def test_zero_sources_paused_and_partial_finding_remain_recoverable(self):
        for status, count, report in [('failed', 0, False), ('failed',25,False), ('paused',25,True), ('failed',25,True)]:
            self.install(status, count, report)
            if report:
                self.service._mutate(self.project, lambda b: b['research']['report']['findings'].append({'statement': 'Unreviewed'}))
            with self.subTest(status=status, count=count, report=report), self.ui():
                self.recover(key='variant-'+status+str(count)+str(report))
            self.assertEqual(self.service._read(self.project)[1]['research']['status'], status)

    def test_original_partial_not_accepted_or_added_to_reading(self):
        with self.ui(): self.recover()
        _, body = self.service._read(self.project)
        self.assertIsNone(research_guidance(body))
        self.assertEqual(self.service._reading_web_supplement(SimpleNamespace(project_id=self.project), {}, {}),
                         {'status': 'not_requested', 'sources': [], 'actual_cost': 'unknown', 'model_read': False})
        self.assertEqual(len(body['materials']), 1)
        with self.ui(), self.assertRaises(WorkflowAPIError):
            self.service._research_validate(body, 'accept_research', {'report_id': body['research']['report_id'], 'allow_partial': True})

    def test_cached_uploaded_reader_works_without_partial_web_sources_or_new_call(self):
        with self.ui(): self.recover()
        _, body = self.service._read(self.project)
        self.service._research_validate(body, 'analyze_materials', {})
        after = self.run_cached_reading()
        self.assert_cache_unchanged()
        self.assertEqual(after['reading']['status'], 'draft_ready')
        self.assertTrue(research_public(after)['materials_only_current'])
        self.assertEqual(len(after['materials']), 1)

    def test_research_cannot_restart_even_after_material_receipt_becomes_stale(self):
        with self.ui(): self.recover()
        for stale in [False, True]:
            if stale: self.service._mutate(self.project, lambda b: b['materials'][0].update(sha256='changed'))
            body = self.service._read(self.project)[1]
            with self.subTest(stale=stale), self.assertRaises(WorkflowAPIError) as error:
                self.service._research_validate(body, 'run_research', {})
            self.assertEqual(error.exception.code, 'RESEARCH_REVIEW_REQUIRED')

    def test_source_report_material_and_scope_changes_invalidate_receipt(self):
        changes = [lambda b: b['research']['partial_sources'][0].update(text='changed'),
                   lambda b: b['research']['report'].update(summary='changed'),
                   lambda b: b['research'].update(report_id='other'),
                   lambda b: b['research'].update(prompt='Different research question'),
                   lambda b: b['research'].update(_base_question='Different original question'),
                   lambda b: b['materials'][0].update(sha256='changed'),
                   lambda b: b.setdefault('reading_exclusions', {}).update({b['materials'][0]['id']: 'explicit'}),
                   lambda b: b.setdefault('reading_asset_exclusions', {}).update({'self-asset': {'reason':'explicit'}}),
                   # op 176: free-text requirements no longer bind research consent; a goal change does.
                   lambda b: b['brief'].update(goal='Changed research task')]
        for n, change in enumerate(changes):
            self.install()
            with self.ui(): self.recover(key='stale-'+str(n))
            self.service._mutate(self.project, change)
            body = self.service._read(self.project)[1]
            with self.subTest(case=n):
                self.assertFalse(research_public(body)['ready_for_planning'])
                with self.assertRaises(WorkflowAPIError): self.service._research_validate(body, 'analyze_materials', {})

    def test_active_and_unknown_jobs_refused_without_cancellation(self):
        self.assertFalse(WuyuWorkflowJob.__table__.columns.status.nullable)
        for status in ['queued', 'running', 'unconfirmed_cost']:
            job = WuyuWorkflowJob(project_id=self.project, action='analyze_materials', status=status, input_epoch=-1)
            db.session.add(job); db.session.commit()
            with self.subTest(status=status), self.ui(), self.assertRaises(WorkflowAPIError) as error: self.recover()
            self.assertEqual(error.exception.code, 'RESEARCH_RECOVERY_BUSY')
            self.assertFalse(job.cancel_requested)
            db.session.delete(job); db.session.commit()

    def test_all_terminal_jobs_allow_choice_without_new_job(self):
        for status in ['completed','failed','cancelled','interrupted']:
            db.session.add(WuyuWorkflowJob(project_id=self.project, action='run_research', status=status))
        db.session.commit()
        with self.ui(): self.recover()
        self.assertEqual(WuyuWorkflowJob.query.count(), 4)

    def test_complete_accepted_review_and_running_states_are_not_recovery(self):
        for status in ['accepted','review_required','queued','running']:
            self.install(status)
            with self.subTest(status=status), self.ui(), self.assertRaises(WorkflowAPIError): self.recover(key='bad-'+status)
        self.install()
        self.service._mutate(self.project, lambda b: b['research']['report'].update(partial=False))
        with self.ui(), self.assertRaises(WorkflowAPIError): self.recover()

    def test_wrong_scope_report_prompt_or_extra_cost_cannot_change_original(self):
        for field,value in [('report_id','other'),('prompt','other'),('acknowledge_cost',True),('auto_accept',True),('preserve_research',1)]:
            _, before = self.service._read(self.project)
            payload = self.payload(); payload[field] = value
            with self.subTest(field=field), self.ui(), self.assertRaises(WorkflowAPIError): self.recover(payload)
            self.assertEqual(self.service._read(self.project)[1], before)

    def test_legacy_destructive_material_reset_is_rejected(self):
        payload=self.payload(); payload.pop('preserve_research'); payload.pop('report_id')
        with self.ui(), self.assertRaises(WorkflowAPIError): self.recover(payload)

    def test_ui_identity_header_and_request_context_are_required(self):
        for actor, header in [('owner',False),('another',True)]:
            with self.subTest(actor=actor), self.ui(actor, header), self.assertRaises(WorkflowAPIError) as error: self.recover()
            self.assertEqual(error.exception.code, 'RESEARCH_CONFIRMATION_REQUIRED')
        with self.assertRaises(WorkflowAPIError): self.recover()

    def test_same_request_replays_once_but_different_input_conflicts(self):
        revision = self.service._read(self.project)[0]; payload=self.payload()
        with self.ui():
            first=self.recover(payload,revision); second=self.recover(payload,revision)
            self.assertEqual(first['revision'],second['revision'])
            changed=dict(payload,prompt='different')
            with self.assertRaises(WorkflowAPIError) as error: self.recover(changed,revision)
        self.assertEqual(error.exception.code,'IDEMPOTENCY_CONFLICT')
        self.assertEqual(WuyuWorkflowJob.query.count(),0)

    def test_stale_revision_cannot_replace_newer_state(self):
        revision=self.service._read(self.project)[0]
        self.service._mutate(self.project,lambda b:b['brief'].update(topic='new task'))
        with self.ui(),self.assertRaises(WorkflowAPIError) as error:self.recover(revision=revision)
        self.assertEqual(error.exception.code,'REVISION_CONFLICT')

    def test_search_fetch_and_wechat_routes_reject_receipt(self):
        with self.ui(): self.recover()
        for kind,payload in [('web/search',{'query':'synthetic'}),('web/fetch',{'url':'https://example.test'}),
                             ('wechat/search',{'query':'synthetic'}),('wechat/article',{'reference_id':'synthetic'})]:
            response=self.client.post('/api/projects/'+self.project+'/workflow/'+kind,
                                      headers={'X-Test-User':'owner'},json=payload)
            self.assertEqual(response.status_code,409,(kind,response.json))
        self.assertEqual(WuyuWorkflowJob.query.count(),0)


if __name__ == '__main__': unittest.main()
