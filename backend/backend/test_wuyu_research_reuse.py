"""Existing sources are adopted only by explicit owner UI consent; zero provider calls."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flask import Flask, request
from models import db, Project
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_deep_research import can_reuse_research, digest, signature, research_public
from services.wuyu_research_workflow import ResearchWorkflowMixin
from services.wuyu_workflow_service import WorkflowAPIError


class Owner(ResearchWorkflowMixin):
    def _research_invalidate_content(self, body):
        raise AssertionError('Adoption must not invalidate or cancel existing work')


def body_fixture():
    body = {'brief': {'topic': 'Self-created public research', 'goal': 'Check sources', 'requirements': 'Original scope', 'page_count': 12},
            'workflow': {'project_id': 'self-reuse-project', 'pages': [{'title': 'Keep'}]},
            'epoch': 7, 'outline': [{'title': 'Keep outline'}], 'slide_specs': [{'title': 'Keep spec'}],
            'reading': {'status': 'idle'}, 'artifacts': [{'id': 'keep-output'}], 'materials': []}
    text = 'Self-created source evidence: 12.50% and 300 kg are retained.'
    report = {'summary': 'A self-created report', 'partial': False, 'completed_at': '2026-11-09T00:00:00Z',
              'findings': [{'statement': 'Observed 12.50%', 'evidence': [{'source_id': 'S1', 'quote': text}]}],
              'sources': [{'id': 'S1', 'url': 'https://example.test/reuse', 'title': 'Self-created source', 'text': text, 'content_scope': 'fetched_excerpt'}]}
    body['research'] = {'signature': signature(body), 'mode': 'deep', 'status': 'accepted', 'requested': True,
                        'report_id': 'self-research-report', 'prompt': 'Original self-created public question',
                        'report': report, 'progress': {'model_calls': 3, 'reserved_tokens': 9000},
                        '_steps': {'keep': {'status': 'completed'}}, 'accepted_at': '2026-11-09T00:00:01Z'}
    body['materials'].append({'id': 'self-research-report', 'origin': 'web_research', 'status': 'completed',
                              'text': text, 'sha256': digest(text), 'chunks': [{'id': 'keep-chunk'}],
                              'metadata': {'research_report_id': 'self-research-report'}})
    # op 176: research consent follows topic/goal; free-text requirements no longer stale it.
    body['brief']['goal'] = 'Updated goal; retain the old evidence when explicitly chosen'
    return body


class ExistingReportReuseTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.temporary = TemporaryDirectory(prefix='research-reuse134-')
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + self.temporary.name + '/fixture.db', SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True)
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        db.session.add(Project(id='self-reuse-project', user_id='self-owner')); db.session.commit()
        self.owner = Owner(); self.owner.app = self.app; self.body = body_fixture()

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.engine.dispose(); self.context.pop(); self.temporary.cleanup()

    @contextmanager
    def ui(self, actor='self-owner', header=True):
        headers = {'X-Wuyu-Interaction': 'research-confirmation'} if header else {}
        with self.app.test_request_context(headers=headers):
            request.current_user = SimpleNamespace(id=actor, role='user')
            yield

    def payload(self):
        return {'report_id': self.body['research']['report_id'], 'allow_partial': False}

    def accept(self, payload=None):
        self.owner._research_validate(self.body, 'accept_research', self.payload() if payload is None else payload)
        self.owner._research_apply(self.body, 'accept_research', self.payload() if payload is None else payload)

    def test_stale_report_is_visible_but_not_implicitly_ready(self):
        public = research_public(self.body)
        self.assertTrue(public['can_reuse_existing_report']); self.assertFalse(public['ready_for_planning'])
        self.assertFalse(public['consent_current']); self.assertEqual(public['status'], 'choice_required')

    def test_explicit_owner_ui_adopts_only_a_separate_scope_receipt(self):
        before = deepcopy(self.body)
        with self.ui(), patch('services.wuyu_research_workflow.WorkflowCharge') as charge:
            self.accept(); charge.assert_not_called()
        receipt = self.body['research'].pop('reuse_confirmation')
        self.assertEqual(self.body, before)
        self.assertEqual(receipt['original_signature'], before['research']['signature'])
        self.assertEqual(receipt['signature'], signature(self.body))
        self.assertEqual(receipt['confirmed_via'], 'research_confirmation_ui')

    def test_adopted_report_is_ready_without_new_material_or_budget(self):
        with self.ui(): self.accept()
        public = research_public(self.body)
        self.assertTrue(public['ready_for_planning']); self.assertTrue(public['reused_for_current_scope'])
        self.assertFalse(public['can_reuse_existing_report']); self.assertEqual(len(self.body['materials']), 1)
        self.assertEqual(self.body['research']['progress'], {'model_calls': 3, 'reserved_tokens': 9000})

    def test_new_requirement_change_requires_another_choice(self):
        with self.ui(): self.accept()
        self.body['brief']['topic'] = 'A different task'
        self.assertFalse(research_public(self.body)['ready_for_planning'])
        self.assertTrue(can_reuse_research(self.body))

    def test_layout_fields_do_not_invalidate_research(self):
        self.body['research']['signature'] = signature(self.body)
        self.body['brief'].update(page_count=20, language='en', output_preference='editable')
        self.assertTrue(research_public(self.body)['ready_for_planning'])
        self.assertFalse(can_reuse_research(self.body))

    def test_no_ui_header_cannot_adopt(self):
        with self.ui(header=False), self.assertRaises(WorkflowAPIError) as error: self.accept()
        self.assertEqual(error.exception.code, 'RESEARCH_CONFIRMATION_REQUIRED')
        self.assertNotIn('reuse_confirmation', self.body['research'])

    def test_another_user_cannot_adopt(self):
        with self.ui(actor='another-owner'), self.assertRaises(WorkflowAPIError): self.accept()

    def test_no_request_context_cannot_adopt(self):
        with self.assertRaises(WorkflowAPIError): self.accept()

    def test_wrong_report_extra_fields_or_partial_flag_refused(self):
        for payload in [dict(self.payload(), report_id='other-report'), dict(self.payload(), allow_partial=True),
                        dict(self.payload(), approved=True), dict(self.payload(), allow_partial=0)]:
            with self.subTest(payload=payload), self.ui(), self.assertRaises(WorkflowAPIError):
                self.owner._research_validate(self.body, 'accept_research', payload)

    def test_active_or_unknown_job_refuses_adoption_without_cancellation(self):
        for status in ['queued', 'running', 'unknown_paid_state']:
            job = WuyuWorkflowJob(project_id='self-reuse-project', action='analyze_materials', status=status)
            db.session.add(job); db.session.commit()
            with self.subTest(status=status), self.ui(), self.assertRaises(WorkflowAPIError) as error: self.accept()
            self.assertEqual(error.exception.code, 'RESEARCH_REUSE_BUSY')
            self.assertEqual(job.status, status); self.assertFalse(job.cancel_requested)
            db.session.delete(job); db.session.commit()

    def test_terminal_job_does_not_block_local_adoption(self):
        db.session.add(WuyuWorkflowJob(project_id='self-reuse-project', action='run_research', status='completed')); db.session.commit()
        with self.ui(): self.accept()
        self.assertEqual(WuyuWorkflowJob.query.count(), 1)

    def test_missing_excluded_duplicate_or_changed_material_is_not_reintroduced(self):
        variants=[]
        body=deepcopy(self.body);body['materials']=[];variants.append(body)
        body=deepcopy(self.body);body['reading_exclusions']={'self-research-report':'User excluded'};variants.append(body)
        body=deepcopy(self.body);body['materials']*=2;variants.append(body)
        body=deepcopy(self.body);body['materials'][0]['text']='Changed';variants.append(body)
        body=deepcopy(self.body);body['materials'][0]['sha256']='bad';variants.append(body)
        body=deepcopy(self.body);body['materials'][0]['metadata']={};variants.append(body)
        for body in variants:
            with self.subTest(body=body): self.assertFalse(can_reuse_research(body))

    def test_partial_failed_running_or_unaccepted_report_not_offered(self):
        for status in ['failed', 'running', 'queued', 'review_required']:
            body=deepcopy(self.body);body['research']['status']=status
            self.assertFalse(can_reuse_research(body))
        self.body['research']['report']['partial']=True
        self.assertFalse(can_reuse_research(self.body))

    def test_bad_report_material_shapes_fail_closed(self):
        for material in [None, [None], [{'id':'self-research-report','metadata':None}]]:
            body=deepcopy(self.body);body['materials']=material
            self.assertFalse(can_reuse_research(body))

    def test_public_read_is_pure_and_keeps_quotes_urls_and_original_signature(self):
        before=deepcopy(self.body);result=research_public(self.body)
        self.assertEqual(self.body,before);self.assertEqual(result['report'],before['research']['report'])
        result['report']['summary']='view mutation'
        self.assertEqual(self.body,before)


if __name__ == '__main__': unittest.main()
