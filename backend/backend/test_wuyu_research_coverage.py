"""Research query breadth and dispatch guards; self-created providers, no IO."""
from copy import deepcopy
import unittest

from services.wuyu_deep_research import PROFILES, run_research


class ResearchCoverageTests(unittest.TestCase):
    def run_case(self, mode='deep', planned=None, followups=None, scholar=True,
                 seed_operations=None, search_status='completed', fetch_status='completed',
                 cancel=None, search_cap=None):
        planned = ['q1', 'q2', 'q3'] if planned is None else planned
        followups = ['q4', 'q5', 'q6'] if followups is None else followups
        self.calls = []
        self.fetches = []
        self.stages = []
        self.models = []
        self.cancel_checks = 0
        profile = deepcopy(PROFILES[mode])
        if search_cap is not None:
            profile['searches'] = search_cap

        def model(step, request):
            self.models.append(step)
            if step == 'plan':
                return {'queries': planned}
            if step == 'reflect':
                return {'queries': followups}
            return {'summary': 'Self-created bounded research.', 'findings': [],
                    'uncertainties': [], 'conflicts': [], 'suggested_sections': []}

        def provider(kind):
            def call(query):
                self.calls.append((kind, query))
                return {'status': search_status, 'results': [
                    {'url': 'https://example.org/' + kind + '/' + query + '/' + str(i),
                     'title': kind + ' ' + query + ' article ' + str(i),
                     'content': 'Self-created evidence for bounded research scheduling.'}
                    for i in range(3)]}
            return call

        def fetch(url):
            self.fetches.append(url)
            return {'status': fetch_status, 'results': [] if fetch_status == 'failed' else [
                {'content': 'Self-created fetched evidence. The original text is preserved.'}]}

        def check():
            self.cancel_checks += 1
            if cancel is not None:
                cancel(self.cancel_checks)

        result = run_research('Self-created public question', profile, model,
                              provider('search'), fetch,
                              lambda value: self.stages.append(deepcopy(value)), check,
                              scholar=provider('scholar') if scholar else None,
                              seed_operations=seed_operations)
        return result

    def test_web_covers_both_planned_queries_before_scholar(self):
        self.run_case('web', planned=['q1', 'q2'])
        self.assertEqual(self.calls, [('search', 'q1'), ('search', 'q2')])
        self.assertEqual(self.models, ['plan', 'synthesize'])

    def test_deep_covers_all_three_initial_queries(self):
        self.run_case()
        self.assertEqual(self.calls[:3], [('search', 'q1'), ('search', 'q2'), ('search', 'q3')])

    def test_reflection_covers_each_new_query_before_scholar(self):
        self.run_case()
        self.assertEqual(self.calls[3:], [('search', 'q4'), ('search', 'q5'), ('search', 'q6')])

    def test_deep_search_cap_remains_six(self):
        result = self.run_case()
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(sum(op['kind'] in ('search', 'scholar') for op in result['operations']), 6)
        self.assertEqual(self.models, ['plan', 'reflect', 'synthesize'])

    def test_web_single_query_can_use_scholar_remainder(self):
        self.run_case('web', planned=['q1'])
        self.assertEqual(self.calls, [('search', 'q1'), ('scholar', 'q1')])

    def test_deep_short_plan_keeps_bounded_scholar_supplement(self):
        self.run_case(planned=['q1', 'q2'], followups=['q3', 'q4'])
        self.assertEqual(self.calls, [('search', 'q1'), ('search', 'q2'), ('scholar', 'q1'),
                                      ('search', 'q3'), ('search', 'q4'), ('scholar', 'q3')])

    def test_no_scholar_keeps_existing_web_only_path(self):
        self.run_case(scholar=False)
        self.assertEqual(self.calls, [('search', 'q' + str(i)) for i in range(1, 7)])

    def test_completed_previous_requests_are_not_redispatched(self):
        seeds = [{'kind': 'search', 'query': 'q1', 'status': 'completed'},
                 {'kind': 'scholar', 'query': 'q1', 'status': 'partial'}]
        before = deepcopy(seeds)
        result = self.run_case(seed_operations=seeds)
        self.assertNotIn(('search', 'q1'), self.calls)
        self.assertNotIn(('scholar', 'q1'), self.calls)
        self.assertEqual(self.calls[:3], [('search', 'q2'), ('search', 'q3'), ('scholar', 'q2')])
        self.assertEqual(seeds, before)
        self.assertEqual(result['operations'][:2], before)

    def test_failed_previous_request_remains_eligible(self):
        self.run_case(seed_operations=[{'kind': 'search', 'query': 'q1', 'status': 'failed'}])
        self.assertEqual(self.calls[0], ('search', 'q1'))

    def test_provider_failed_status_still_consumes_dispatch_budget(self):
        self.run_case(search_status='failed')
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(len(set(self.calls)), 6)

    def test_custom_smaller_search_cap_is_not_expanded(self):
        self.run_case(planned=['q1', 'q2'], search_cap=2)
        self.assertEqual(self.calls, [('search', 'q1'), ('search', 'q2')])

    def test_fetch_cap_and_failed_fetches_are_still_bounded(self):
        self.run_case(fetch_status='failed')
        self.assertEqual(len(self.fetches), 8)
        self.assertEqual(sum(s['stage'] == 'fetching' for s in self.stages), 8)

    def test_cancel_before_first_provider_does_not_dispatch(self):
        class Cancelled(Exception):
            pass
        def cancel(count):
            if count == 2:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            self.run_case(cancel=cancel)
        self.assertEqual(self.calls, [])

    def test_reflection_does_not_repeat_original_queries(self):
        self.run_case(followups=['q1', 'q4'])
        self.assertEqual(self.calls.count(('search', 'q1')), 1)
        self.assertEqual(self.calls[3:], [('search', 'q4'), ('scholar', 'q4')])


if __name__ == '__main__':
    unittest.main()
