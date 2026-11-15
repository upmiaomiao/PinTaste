"""op 175: reading/outline/research on DeepSeek official (deepseek-flash via the private gateway)."""
import unittest
from types import SimpleNamespace

from services.wuyu_errors import PipelineError
from services.wuyu_providers import WuyuGateway
from services.wuyu_workflow_errors import failure_message


class FakeCompletions:
    def __init__(self, finish, content):
        self.finish, self.content, self.calls = finish, content, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.content, reasoning_content=None, reasoning=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=self.finish)],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)

    def with_options(self, **kwargs):
        return self


def gateway(finish='stop', content='{"ok": true}'):
    completions = FakeCompletions(finish, content)
    config = {'TESTING': True, 'WUYU_PLANNER_MODEL': 'deepseek-v4.1-flash', 'WUYU_SHENSI_BASE_URL': 'https://example.invalid/v1',
              'WUYU_SHENSI_API_KEY': 'fixture-key-0000000000', 'WUYU_CLIENT_FACTORY': lambda **kwargs: FakeClient(completions)}
    return WuyuGateway(config), completions


class DeepSeekPlannerTests(unittest.TestCase):
    def test_planner_uses_deepseek_nonthinking_json_contract(self):
        g, _ = gateway()
        self.assertEqual(g.models['planner'], 'deepseek-v4.1-flash')
        policy = g.planner_request_policy()
        self.assertEqual(policy['mode'], 'deepseek_nonthinking_json')
        self.assertEqual(policy['parameters']['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertEqual(policy['parameters']['response_format'], {'type': 'json_object'})

    def test_planner_text_returns_json(self):
        g, completions = gateway()
        self.assertEqual(g.text('planner', 'Return JSON'), '{"ok": true}')
        self.assertEqual(completions.calls[0]['model'], 'deepseek-v4.1-flash')

    def test_content_filter_is_a_named_failure_not_bad_json(self):
        g, _ = gateway('content_filter', '')
        with self.assertRaises(PipelineError) as raised:
            g.text('planner', 'Return JSON')
        self.assertEqual(raised.exception.code, 'content_filtered')
        self.assertIn('内容安全审核', failure_message('content_filtered'))

    def test_research_message_mentions_deepseek(self):
        self.assertIn('DeepSeek', failure_message('research_model_required'))

    def test_report_with_more_than_16_findings_keeps_first_16(self):
        from services.wuyu_deep_research import validate_report
        quote = '清华x-lab于2013年4月25日发起成立，依托清华大学经济管理学院。'
        sources = [{'id': 'S1', 'url': 'https://example.test/a', 'text': quote}]
        raw = {'summary': '综述', 'findings': [{'statement': '发现%d' % i, 'evidence': [{'source_id': 'S1', 'quote': quote}]}
                                              for i in range(22)], 'uncertainties': [], 'conflicts': [], 'suggested_sections': []}
        report = validate_report(raw, sources)
        self.assertEqual(len(report['findings']), 16)
        self.assertEqual(report['omitted_findings'], 0)
        self.assertEqual(report['trimmed_findings'], 6)


if __name__ == '__main__':
    unittest.main()
