"""Exact-context query whitespace compatibility; no charge/provider may be replayed."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services import wuyu_research_workflow as workflow
from services.wuyu_deep_research import PROFILES, digest, run_research
from services.wuyu_errors import PipelineError


def key(step, request):
    return digest({'step': step, 'prompt': json.dumps(request, ensure_ascii=False)})


class CacheProjectionTests(unittest.TestCase):
    def setUp(self):
        self.raw = ['20\n26', '白\n名单', 'q3']
        self.request = {'question': 'Self-created public question', 'searched': ['2026', '白名单', 'q3'],
                        'sources': [{'id': 'S1', 'url': 'https://example.org/source', 'text': 'Original 12.5% evidence.'}],
                        'previous_summary': 'Original summary', 'instruction': 'Original fixed instruction'}
        self.legacy = deepcopy(self.request);self.legacy['searched'] = self.raw
        self.saved = {'status': 'completed', 'step': 'reflect', 'result': {'queries': ['q4']}}
        self.state = {'_steps': {key('reflect', self.legacy): self.saved}}

    def test_reflect_old_query_whitespace_reuses_exact_legacy_context(self):
        self.assertIs(workflow.cached_research_step(self.state, 'reflect', self.request, self.raw), self.saved)

    def test_exact_current_key_wins_over_different_legacy_result(self):
        current = {'status': 'completed', 'step': 'reflect', 'result': {'queries': ['exact']}}
        self.state['_steps'][key('reflect', self.request)] = current
        self.assertIs(workflow.cached_research_step(self.state, 'reflect', self.request, self.raw), current)

    def test_uncertain_exact_current_step_does_not_fall_back_to_old_completion(self):
        current = {'status': 'dispatched', 'step': 'reflect'}
        self.state['_steps'][key('reflect', self.request)] = current
        self.assertIs(workflow.cached_research_step(self.state, 'reflect', self.request, self.raw), current)

    def test_legacy_uncertain_state_is_returned_for_existing_refusal(self):
        self.saved['status'] = 'dispatched';self.saved.pop('result')
        self.assertIs(workflow.cached_research_step(self.state, 'reflect', self.request, self.raw), self.saved)

    def test_question_change_does_not_reuse_old_cache(self):
        request = deepcopy(self.request);request['question'] = 'Different public question'
        self.assertIsNone(workflow.cached_research_step(self.state, 'reflect', request, self.raw))

    def test_source_text_change_does_not_reuse_old_cache(self):
        request = deepcopy(self.request);request['sources'][0]['text'] = 'Different 99% evidence.'
        self.assertIsNone(workflow.cached_research_step(self.state, 'reflect', request, self.raw))

    def test_source_identity_and_URL_change_do_not_reuse_old_cache(self):
        for field, value in [('id', 'S2'), ('url', 'https://example.org/another')]:
            request = deepcopy(self.request);request['sources'][0][field] = value
            self.assertIsNone(workflow.cached_research_step(self.state, 'reflect', request, self.raw))

    def test_summary_and_instruction_changes_do_not_reuse_old_cache(self):
        for field in ['previous_summary', 'instruction']:
            request = deepcopy(self.request);request[field] = 'Different context'
            self.assertIsNone(workflow.cached_research_step(self.state, 'reflect', request, self.raw))

    def test_changed_query_or_order_does_not_reuse_old_cache(self):
        for searched in [['2027', '白名单', 'q3'], ['白名单', '2026', 'q3'], ['2026']]:
            request = deepcopy(self.request);request['searched'] = searched
            self.assertIsNone(workflow.cached_research_step(self.state, 'reflect', request, self.raw))

    def test_no_cross_step_or_arbitrary_cache_scan(self):
        self.assertIsNone(workflow.cached_research_step(self.state, 'synthesize', self.request, self.raw))
        self.state['_steps'] = {'arbitrary-self-created-key': self.saved}
        self.assertIsNone(workflow.cached_research_step(self.state, 'reflect', self.request, self.raw))

    def test_lookup_does_not_mutate_request_or_saved_cache(self):
        before = deepcopy((self.state, self.request, self.raw))
        workflow.cached_research_step(self.state, 'reflect', self.request, self.raw)
        self.assertEqual((self.state, self.request, self.raw), before)

    def test_corrupt_exact_cache_fails_closed(self):
        for saved in [None, {}, {'status': 'completed'}, {'status': 'completed', 'result': []}]:
            self.state['_steps'][key('reflect', self.request)] = saved
            with self.assertRaises(PipelineError) as error:
                workflow.cached_research_step(self.state, 'reflect', self.request, self.raw)
            self.assertEqual(error.exception.code, 'research_dispatch_uncertain')

    def test_plan_previous_queries_use_only_successful_same_kind_projection(self):
        pairs = [('search', '20\n26'), ('scholar', '白\n名单')]
        request = {'question': 'Self-created question', 'already_searched': [
            {'kind': 'scholar', 'query': '白名单'}, {'kind': 'search', 'query': '2026'}]}
        legacy = deepcopy(request);legacy['already_searched'] = [{'kind': k, 'query': q} for k, q in sorted(pairs)]
        state = {'_steps': {key('plan', legacy): self.saved}, '_seed_operations': [
            {'kind': k, 'query': q, 'status': 'completed'} for k, q in pairs] + [
            {'kind': 'search', 'query': 'ignored failure', 'status': 'failed'}]}
        self.assertIs(workflow.cached_research_step(state, 'plan', request, []), self.saved)

    def test_plan_changed_kind_or_query_prevents_projection(self):
        request = {'already_searched': [{'kind': 'search', 'query': '2026'}]}
        state = {'_steps': {}, '_seed_operations': [{'kind': 'scholar', 'query': '20\n26', 'status': 'completed'}]}
        self.assertIsNone(workflow.cached_research_step(state, 'plan', request, []))

    def test_plan_without_seed_operations_has_no_false_legacy_hit(self):
        self.assertIsNone(workflow.cached_research_step({'_steps': {}, '_seed_operations': None}, 'plan', {'already_searched': []}, []))

    def test_raw_plan_alias_extracts_original_strings_without_rewriting(self):
        result = {'q\nueries': [' 20\n26 ', '白\n名单', '白\n名单']};before = deepcopy(result)
        self.assertEqual(workflow._raw_plan_queries(result), ['20\n26', '白\n名单']);self.assertEqual(result, before)

    def test_ambiguous_or_invalid_plan_does_not_supply_a_projection(self):
        for result in [None, {'queries': ['q'], 'q\nueries': ['q']}, {'queries': [1]}, {'queries': ['a'] * 4}]:
            self.assertEqual(workflow._raw_plan_queries(result), [])


class RealModelClosureTests(unittest.TestCase):
    def fixture(self, *, current=False):
        planned = ['20\n26', '白\n名单', 'q3'];followups = ['q4', 'q5', 'q6']
        operations = [{'kind': kind, 'query': query, 'status': 'completed'}
                      for query in planned + followups for kind in ['search', 'scholar']]
        sources = [{'id': 'S1', 'url': 'https://example.org/self-created', 'title': 'Self-created public source',
                    'text': 'Self-created original public evidence with 12.5%.', 'content_scope': 'fetched_excerpt'}]
        results = {'plan': {'queries': planned}, 'reflect': {'queries': followups},
                   'synthesize': {'summary': 'Self-created summary', 'findings': [],
                                  'uncertainties': [], 'conflicts': [], 'suggested_sections': []}}
        requests = {}
        def record(step, request):requests[step] = deepcopy(request);return deepcopy(results[step])
        forbidden = Mock(side_effect=AssertionError('No provider may be replayed'))
        run_research('Self-created public cache question', PROFILES['deep'], record, forbidden, forbidden,
                     lambda update: None, lambda: None, scholar=forbidden,
                     seed_sources=sources, seed_operations=operations)
        saved = {}
        for step, request in requests.items():
            raw = deepcopy(request)
            if not current:
                if step == 'plan':raw['already_searched'] = [{'kind': k, 'query': q} for k, q in sorted({(op['kind'], op['query']) for op in operations})]
                if step == 'reflect':raw['searched'] = list(planned)
            saved[key(step, raw)] = {'status': 'completed', 'step': step, 'result': deepcopy(results[step]), 'usage': {}}
        body = {'research': {'mode': 'deep', 'profile': deepcopy(PROFILES['deep']), 'prompt': 'Self-created public cache question',
                            'report_id': 'self-created-cache-131', '_steps': saved, '_seed_sources': sources,
                            '_seed_operations': operations, 'progress': {'model_calls': 3, 'reserved_tokens': 300000}}}
        class Owner:
            app = SimpleNamespace(config={'TESTING': True})
            def _read(self, project):return 0, deepcopy(body)
            def _mutate(self, project, operation, **kwargs):operation(body)
            def _check_cancel(self, job):pass
        job = SimpleNamespace(project_id='self-created-project', id='self-created-job', actor_user_id='self-created-owner',
                              payload='{}', _runtime_config={})
        gateway = SimpleNamespace(models={}, text=forbidden)
        pipeline = SimpleNamespace(gateway=gateway)
        return Owner(), job, pipeline, body, requests, forbidden

    def execute(self, owner, job, pipeline):
        charge = Mock(side_effect=AssertionError('Cached steps must not reserve another charge'))
        provider = Mock(side_effect=AssertionError('Cached steps must not call provider'))
        web = SimpleNamespace(deepseek_search=provider, scholar_search=provider, fetch=provider)
        policy = {'deep_search_limit': 6, 'web_search_limit': 2, 'deep_fetch_limit': 8, 'web_fetch_limit': 3}
        with patch.object(workflow, 'WorkflowCharge', charge), \
             patch('services.wuyu_web_research.WuyuWebResearch', return_value=web), \
             patch('services.wuyu_runtime_settings.get_research_policy', return_value=policy):
            try:workflow.ResearchWorkflowMixin._run_research(owner, job, pipeline)
            finally:
                charge.assert_not_called();provider.assert_not_called();pipeline.gateway.text.assert_not_called()

    def test_actual_model_closure_recovers_all_old_cached_steps_with_exhausted_budget(self):
        owner, job, pipeline, body, requests, forbidden = self.fixture()
        original_steps = deepcopy(body['research']['_steps'])
        self.execute(owner, job, pipeline)
        self.assertEqual(body['research']['status'], 'review_required')
        self.assertEqual(body['research']['progress']['model_calls'], 3)
        self.assertEqual(body['research']['_steps'], original_steps)

    def test_actual_model_closure_keeps_current130_cache_compatible(self):
        owner, job, pipeline, body, requests, forbidden = self.fixture(current=True)
        self.execute(owner, job, pipeline)
        self.assertEqual(body['research']['status'], 'review_required')

    def test_actual_prior_exact_only_lookup_reproduces_budget_failure_without_provider(self):
        owner, job, pipeline, body, requests, forbidden = self.fixture()
        exact = lambda state, step, request, raw: state['_steps'].get(key(step, request))
        with patch.object(workflow, 'cached_research_step', exact), self.assertRaises(PipelineError) as error:
            self.execute(owner, job, pipeline)
        self.assertEqual(error.exception.code, 'research_budget_exhausted')

    def test_actual_model_closure_refuses_uncertain_old_step_without_new_charge(self):
        owner, job, pipeline, body, requests, forbidden = self.fixture()
        row = next(v for v in body['research']['_steps'].values() if v['step'] == 'reflect')
        row['status'] = 'dispatched';row.pop('result')
        with self.assertRaises(PipelineError) as error:self.execute(owner, job, pipeline)
        self.assertEqual(error.exception.code, 'research_dispatch_uncertain')

    def test_actual_model_closure_does_not_borrow_different_source_context(self):
        owner, job, pipeline, body, requests, forbidden = self.fixture()
        body['research']['_seed_sources'][0]['text'] = 'Different source evidence.'
        with self.assertRaises(PipelineError) as error:self.execute(owner, job, pipeline)
        self.assertEqual(error.exception.code, 'research_budget_exhausted')


if __name__ == '__main__':unittest.main()
