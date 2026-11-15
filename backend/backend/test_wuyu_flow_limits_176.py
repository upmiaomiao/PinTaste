"""op 176: one over-long or invalid item must not void a whole paid result."""
import unittest

from services.wuyu_errors import PipelineError
from services.wuyu_json_normalization import parse_object
from services import wuyu_deep_research as research


class FlowLimitTests(unittest.TestCase):
    def test_single_object_array_is_unwrapped(self):
        self.assertEqual(parse_object('[ {"findings": [], "issues": []} ]'), {'findings': [], 'issues': []})
        with self.assertRaises(PipelineError):
            parse_object('[{"a": 1}, {"b": 2}]')
        with self.assertRaises(PipelineError):
            parse_object('[1, 2]')

    def test_research_plan_with_extra_queries_keeps_first(self):
        plan = {'queries': ['清华 x-lab 官网', '清华 x-lab 孵化', '清华 x-lab 赛事', '清华 x-lab 融资', '清华 x-lab 课程']}
        self.assertEqual(research._queries(plan, 3), ['清华 x-lab 官网', '清华 x-lab 孵化', '清华 x-lab 赛事'])
        with self.assertRaises(PipelineError):
            research._queries({'queries': []}, 3)

    def test_requirements_text_no_longer_voids_research_consent(self):
        body = {'brief': {'topic': '清华x-lab', 'goal': '介绍平台', 'requirements': '12页'}}
        stored = research.signature(body)
        legacy = research.legacy_signature(body)
        body['brief']['requirements'] = '12页。用户已确认继续'
        self.assertTrue(research.signature_current(stored, body))
        # A pre-176 record stays current while its requirements are unchanged...
        body['brief']['requirements'] = '12页'
        self.assertTrue(research.signature_current(legacy, body))
        # ...and a real task change still needs a fresh choice.
        body['brief']['topic'] = '北大创业园'
        self.assertFalse(research.signature_current(stored, body))
        self.assertFalse(research.signature_current(None, body))

    def test_research_public_stays_ready_after_requirements_edit(self):
        body = {'brief': {'topic': 'T', 'goal': 'G', 'requirements': 'R'}, 'materials': []}
        body['research'] = {'requested': True, 'status': 'accepted', 'mode': 'web', 'signature': research.signature(body), 'report_id': 'r1'}
        self.assertTrue(research.research_public(body)['ready_for_planning'])
        body['brief']['requirements'] = 'R 用户已选择重新分析'
        self.assertTrue(research.research_public(body)['ready_for_planning'])
        body['brief']['goal'] = 'G2'
        self.assertFalse(research.research_public(body)['ready_for_planning'])
    def test_reading_batch_tokens_caps_each_reader_call(self):
        # op 183: batch/admin values only size TEXT batches; one request may use the model limit.
        import os
        from unittest import mock
        from services.wuyu_reading import reading_policy
        config = {'WUYU_PLANNER_MODEL': 'deepseek-v4.1-flash', 'WUYU_PLANNER_CONTEXT_TOKENS': 1048576, 'WUYU_PLANNER_OUTPUT_TOKENS': 32768}
        model_max = 1048576 - 32768 - 3000
        with mock.patch.dict(os.environ, {'WUYU_READING_BATCH_TOKENS': '64000'}):
            policy = reading_policy(config)
            self.assertEqual((policy['batch_tokens'], policy['input_tokens']), (64000, model_max))
        with mock.patch.dict(os.environ, {'WUYU_READING_BATCH_TOKENS': ''}):
            self.assertEqual(reading_policy(config)['batch_tokens'], 800000)
            self.assertEqual(reading_policy(config)['input_tokens'], model_max)
        # The admin policy remains an upper bound for batching.
        with mock.patch.dict(os.environ, {'WUYU_READING_BATCH_TOKENS': '64000'}):
            policy = reading_policy(dict(config, WUYU_READING_INPUT_TOKENS=20000))
            self.assertEqual((policy['batch_tokens'], policy['input_tokens']), (20000, model_max))

    def test_image_heavy_single_file_is_not_refused_by_batch_size(self):
        """op 183: >=38 images in one file used to raise reading_budget_invalid at 64K."""
        import hashlib, os, tempfile
        from unittest import mock
        from PIL import Image
        from services.wuyu_reading import prepare_reading
        root = tempfile.mkdtemp(prefix='op183-')
        assets = []
        for i in range(40):
            path = os.path.join(root, f'img{i}.png'); Image.new('RGB', (64, 36), (i, 90, 160)).save(path)
            assets.append({'id': f'a{i}', 'path': path, 'sha256': hashlib.sha256(open(path, 'rb').read()).hexdigest(),
                           'caption': f'第{i+1}页', 'figure_label': '', 'nearby_text': '示例', 'source': {'page': i + 1}})
        material = {'id': 'm', 'filename': 'template.pptx', 'role': 'material', 'status': 'completed', 'sha256': 'x', 'text': '',
                    'chunks': [{'id': f'c{i}', 'text': f'第{i+1}页 标题 正文', 'source': {'page': i + 1}} for i in range(40)],
                    'source_assets': assets}
        config = {'UPLOAD_FOLDER': root, 'WUYU_PLANNER_MODEL': 'deepseek-v4.1-flash', 'WUYU_PLANNER_OUTPUT_TOKENS': 32768}
        with mock.patch.dict(os.environ, {'WUYU_READING_BATCH_TOKENS': '64000'}):
            plan = prepare_reading([material], {'topic': 't', 'creation_kind': 'deck', 'page_count': 10}, config)
        self.assertEqual(sum(len(t.get('visual_inputs', [])) for t in plan['tasks']), 40)
        self.assertTrue(all(t['input_tokens'] <= plan['policy']['input_tokens'] for t in plan['tasks']))

if __name__ == '__main__':
    unittest.main()
