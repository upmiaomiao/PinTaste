"""Follow-ups from the 173 acceptance run: replan-image failure recovery and generate_samples guard."""
import json
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
import test_wuyu_page_retry as base
from models import db
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_state import apply_event
from services.wuyu_workflow_service import WorkflowAPIError


class Followups(base.PageRetryTests):
    def setUp(self):
        super().setUp()
        self.prompts, self.failures = [], {}

    def image(self, prompt, images, cancel):
        payload = json.loads(prompt)
        n = payload['slide_spec']['number']
        self.prompts.append(payload)
        if self.failures.get(n, 0):
            self.failures[n] -= 1
            raise PipelineError('self_created_image_failure', 'fixture failure')
        return Image.new('RGB', (160, 90), (n * 40, 90, 130))

    def lock_first_page(self):
        first = self.run_job(self.queued_job(self.action('generate_images', {'generation_requested': True}, 'f-first')))
        self.assertEqual(first.status, 'completed', first.error_code)
        res = self.action('lock_pages', {'numbers': [1], 'locked': True, 'source': 'user_ui'}, 'f-lock')
        self.assertTrue(self.body()['workflow']['pages'][0]['locked'])

    def fake_replan(self):
        """Stand-in for the paid planner: returns new specs for both pages."""
        specs = [{'number': n, 'title': 'Replanned %d' % n, 'copy': 'New copy %d' % n, 'points': []} for n in (1, 2)]
        return patch('services.wuyu_pipeline.WuyuPipeline.create_outline', lambda *a, **k: specs)

    # ---- fix 1: image failure inside _replan_unlocked is recoverable per page
    def test_replan_image_failure_can_retry_only_that_page(self):
        self.lock_first_page()
        before = self.preserved_success()
        self.failures = {2: 1}
        with self.fake_replan():
            queued = self.queued_job(self.action('regenerate_unlocked', {'feedback': 'shorter', 'locked_numbers': [1]}, 'f-regen'))
            self.assertEqual(queued.action, '_replan_unlocked')
            job = self.run_job(queued)
        self.assertEqual(job.status, 'failed')
        pages = self.body()['workflow']['pages']
        self.assertEqual([p['generation_status'] for p in pages], ['ready', 'failed'])
        planned = self.body()['slide_specs'][1]['title']
        self.assertEqual(planned, 'Replanned 2')
        calls_before = len(self.prompts)
        # Per-page retry now has a valid source: no second plan, only page 2 renders.
        with self.fake_replan() as planner:
            result = self.action('retry', {'number': 2}, 'f-retry')
            retry = self.queued_job(result)
            self.assertEqual(retry.retry_of, job.id)
            done = self.run_job(retry)
        self.assertEqual(done.status, 'completed', done.error_code)
        self.assertEqual([p['number'] for p in map(lambda x: x['slide_spec'], self.prompts[calls_before:])], [2])
        self.assertEqual(self.body()['slide_specs'][1]['title'], 'Replanned 2')
        self.assert_success_preserved(before)
        self.assertTrue(self.body()['workflow']['pages'][0]['locked'])

    # ---- fix 2: sample generation is rejected up front while pages are locked
    def test_sample_actions_rejected_before_queueing_when_locked(self):
        self.lock_first_page()
        jobs_before = db.session.query(WuyuWorkflowJob).count()
        for action in ('generate_samples', 'regenerate_samples'):
            with self.assertRaises(WorkflowAPIError) as caught:
                self.action(action, {}, 'f-' + action)
            self.assertEqual(caught.exception.code, 'PAGES_LOCKED', action)
        self.assertEqual(db.session.query(WuyuWorkflowJob).count(), jobs_before)
        self.assertTrue(self.body()['workflow']['pages'][0]['locked'])


for name in [n for n in dir(base.PageRetryTests) if n.startswith('test_')]:
    setattr(Followups, name, None)

if __name__ == '__main__':
    unittest.main()
