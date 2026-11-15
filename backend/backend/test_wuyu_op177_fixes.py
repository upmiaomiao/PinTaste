"""op177: single-image template isolation, research/template serialization, resume of paused research."""
import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from flask import Flask, request
from models import db, Project
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_pipeline import with_template_content_isolation, TEMPLATE_CONTENT_ISOLATION_TASK
from services.wuyu_research_workflow import ResearchWorkflowMixin
from services.wuyu_workflow_service import WorkflowAPIError
from services.wuyu_deep_research import signature


class TemplateIsolationTests(unittest.TestCase):
    base = json.dumps({'task': 'Render one slide.', 'slide_spec': {'title': 'x'}}, ensure_ascii=False)

    def test_no_template_reference_leaves_prompt_unchanged(self):
        self.assertEqual(with_template_content_isolation(self.base, []), self.base)
        self.assertEqual(with_template_content_isolation(self.base, [{'id': 'approved_sample:1', 'role': 'approved_sample'}]), self.base)

    def test_template_reference_adds_contract_and_task_sentence_once(self):
        refs = [{'id': 'figure75-001:p1', 'role': 'template', 'reference_scope': 'single_image'},
                {'id': 'deck:p3', 'role': 'template'}]
        once = with_template_content_isolation(self.base, refs)
        twice = with_template_content_isolation(once, refs)
        value = json.loads(twice)
        contract = value['template_content_isolation']
        self.assertEqual(contract['applies_to_reference_ids'], ['figure75-001:p1', 'deck:p3'])
        self.assertEqual(contract['single_image_reference_ids'], ['figure75-001:p1'])
        self.assertIn('never_reuse', contract)
        self.assertEqual(value['task'].count(TEMPLATE_CONTENT_ISOLATION_TASK.strip()), 1)
        self.assertEqual(value['slide_spec'], {'title': 'x'})

    def test_multi_page_template_has_no_single_image_rule(self):
        value = json.loads(with_template_content_isolation(self.base, [{'id': 'deck:p3', 'role': 'template'}]))
        self.assertNotIn('single_image_reference_ids', value['template_content_isolation'])


class Owner(ResearchWorkflowMixin):
    pass


def body_fixture(status='running'):
    body = {'brief': {'topic': 'Battery recycling', 'goal': 'Summarize', 'page_count': 1, 'creation_kind': 'pages'},
            'workflow': {'project_id': 'p177', 'pages': []}, 'epoch': 3, 'materials': [], 'outline': [], 'slide_specs': [],
            'reading': {'status': 'idle'}, 'artifacts': []}
    body['research'] = {'signature': signature(body), 'mode': 'deep', 'status': status, 'requested': True,
                        'report_id': 'r177', 'prompt': 'q', 'confirmed_via': 'research_confirmation_ui',
                        'profile': {}, '_steps': {}, 'progress': {'stage': status, 'model_calls': 1, 'reserved_tokens': 10}}
    return body


class ResearchSerializationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.tmp = TemporaryDirectory(prefix='op177-')
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///' + self.tmp.name + '/f.db', SQLALCHEMY_TRACK_MODIFICATIONS=False, TESTING=True)
        db.init_app(self.app)
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.add(Project(id='p177', user_id='owner')); db.session.commit()
        self.owner = Owner(); self.owner.app = self.app

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.engine.dispose(); self.ctx.pop(); self.tmp.cleanup()

    def test_template_confirmation_refused_while_research_runs(self):
        for status in ['queued', 'running']:
            job = WuyuWorkflowJob(project_id='p177', action='run_research', status=status)
            db.session.add(job); db.session.commit()
            for action in ['confirm_template_choice', 'select_references']:
                with self.subTest(status=status, action=action), self.assertRaises(WorkflowAPIError) as error:
                    self.owner._research_validate(body_fixture(), action, {'source_id': 'x'})
                self.assertEqual(error.exception.code, 'TEMPLATE_RESEARCH_IN_PROGRESS')
                self.assertEqual(error.exception.status, 409)
            self.assertFalse(job.cancel_requested)
            db.session.delete(job); db.session.commit()

    def test_template_confirmation_allowed_after_research_finished(self):
        db.session.add(WuyuWorkflowJob(project_id='p177', action='run_research', status='completed')); db.session.commit()
        self.owner._research_validate(body_fixture('accepted'), 'confirm_template_choice', {'source_id': 'x'})

    def test_resume_requeues_paused_round_and_job_validation_passes(self):
        body = body_fixture('paused')
        body['resume_action'] = {'action': 'run_research', 'payload': {}}
        self.owner._research_apply(body, 'resume', {})
        self.assertEqual(body['research']['status'], 'queued')
        self.assertEqual(body['research']['report_id'], 'r177')
        self.owner._research_validate(body, 'run_research', {}, running=True)  # used to raise -> HTTP 500

    def test_resume_without_research_memo_does_not_touch_research(self):
        body = body_fixture('paused')
        body['resume_action'] = {'action': '_continue', 'payload': {}}
        self.owner._research_apply(body, 'resume', {})
        self.assertEqual(body['research']['status'], 'paused')


if __name__ == '__main__':
    unittest.main()
