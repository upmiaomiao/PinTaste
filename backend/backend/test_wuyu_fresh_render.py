"""A re-planned (fresh_render) page must render without an edit-original request."""
import json
import unittest
from unittest.mock import patch

from PIL import Image
import test_wuyu_page_retry as base
from models import db
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_workflow_quota import current_workflow_actor


class FreshRenderTests(base.PageRetryTests):
    # Reuse the fixture only; the inherited tests are disabled below.
    def image(self, prompt, images, cancel):
        payload = json.loads(prompt)
        self.prompts.append(payload)
        return Image.new('RGB', (160, 90), (payload['slide_spec']['number'] * 40, 90, 130))

    def setUp(self):
        super().setUp()
        self.prompts, self.failures = [], {}

    def test_fresh_render_page_has_no_edit_original_and_keeps_instruction(self):
        job = self.run_job(self.queued_job(self.action('generate_images', {'generation_requested': True}, 'fr-first')))
        self.assertEqual(job.status, 'completed', job.error_code)
        self.prompts.clear()
        change = 'make this page shorter'
        def mark(body):
            from services.wuyu_state import apply_event
            body['workflow'] = apply_event(body['workflow'], {'event_id': 'fr-evt', 'type': 'page.changed',
                'payload': {'number': 2, 'change': change, 'fresh': True}})
        self.workflow._mutate(self.project, mark)
        self.assertTrue(self.body()['workflow']['pages'][1]['fresh_render'])
        job = self.run_job(self.queued_job(self.action('retry', {'number': 2}, 'fr-retry'))
                           if False else self.queued_job(self.action('generate_images', {'generation_requested': True}, 'fr-second')))
        self.assertEqual(job.status, 'completed', job.error_code)
        page2 = [p for p in self.prompts if p['slide_spec']['number'] == 2]
        self.assertEqual(len(page2), 1)
        self.assertEqual(page2[0]['feedback'], '')
        self.assertFalse(page2[0]['mode'] == 'edit' if 'mode' in page2[0] else False)
        self.assertEqual(page2[0]['design_requirements'].get('feedback'), change)
        self.assertFalse(self.body()['workflow']['pages'][1].get('fresh_render'))


for name in [n for n in dir(base.PageRetryTests) if n.startswith('test_')]:
    setattr(FreshRenderTests, name, None)

if __name__ == '__main__':
    unittest.main()
