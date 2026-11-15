"""Single-line public query commands; source values and old evidence remain raw."""
from copy import deepcopy
import json
import unittest

from services.wuyu_deep_research import _queries
from services.wuyu_errors import PipelineError
from services.wuyu_json_normalization import parse_object
import test_wuyu_research_coverage as coverage_fixture


class ResearchQueryTests(unittest.TestCase):
    def query(self, value):
        return _queries({'queries': [value]}, 3)[0]

    def rejects(self, value):
        with self.assertRaises(PipelineError) as error:self.query(value)
        self.assertEqual(error.exception.code, 'research_plan_invalid')

    def test_wrapped_year_is_joined_for_each_line_ending(self):
        for ending in ['\n', '\r', '\r\n']:
            with self.subTest(ending=repr(ending)):
                self.assertEqual(self.query('202' + ending + '6年政策'), '2026年政策')

    def test_wrapped_CJK_word_is_joined(self):
        self.assertEqual(self.query('新能源白\n名单企业'), '新能源白名单企业')

    def test_English_line_boundary_is_a_word_separator(self):
        self.assertEqual(self.query('electric\nvehicle recycling'), 'electric vehicle recycling')

    def test_existing_spaces_and_tabs_are_single_query_separators(self):
        self.assertEqual(self.query('  public\t data   review  '), 'public data review')

    def test_already_single_line_query_is_unchanged(self):
        self.assertEqual(self.query('2026年 official data 12.5%'), '2026年 official data 12.5%')

    def test_normalized_duplicates_are_not_dispatched_twice(self):
        self.assertEqual(_queries({'queries': ['20\n26', '2026', 'other']}, 3), ['2026', 'other'])

    def test_original_plan_is_not_rewritten(self):
        plan = {'queries': ['20\n26 白\n名单'], 'questions': ['原始\n问题']};before = deepcopy(plan)
        self.assertEqual(_queries(plan, 3), ['2026 白名单']);self.assertEqual(plan, before)

    def test_nonquery_evidence_values_are_not_normalized(self):
        raw = {'queries': ['白\n名单'], 'findings': [{'quote': '2\n030年原文', 'source_id': 'S\n1'}]}
        before = deepcopy(raw);_queries(raw, 3)
        self.assertEqual(raw, before)

    def test_empty_query_is_still_rejected(self):
        self.rejects(' \t\r\n ')

    def test_raw_query_size_has_a_hard_bound(self):
        self.rejects('q' + '\n' * 512)

    def test_final_query_limit_remains_128(self):
        self.rejects('研' * 129)

    def test_wrapped_input_can_fit_the_existing_final_query_limit(self):
        self.assertEqual(self.query(('研\n' * 128).strip()), '研' * 128)

    def test_folded_phone_cannot_bypass_privacy_check(self):
        self.rejects('139\n00000000')

    def test_folded_email_cannot_bypass_privacy_check(self):
        self.rejects('self-fixture@\nexample.invalid')

    def test_folded_key_prefix_cannot_bypass_privacy_check(self):
        self.rejects('s\nk-SELF_CREATED_NOT_A_KEY')

    def test_folded_Bearer_prefix_cannot_bypass_privacy_check(self):
        self.rejects('Bea\nrer SELF_CREATED_NOT_A_TOKEN')

    def test_public_Bearer_word_without_a_token_keeps_previous_behavior(self):
        self.assertEqual(self.query('OAuth Bearer'), 'OAuth Bearer')

    def test_existing_sensitive_patterns_remain_blocked(self):
        for value in ['13900000000', 'fixture@example.invalid', 'sk-SELF_CREATED_NOT_A_KEY', 'Bearer SELF_CREATED_NOT_A_TOKEN']:
            with self.subTest(value=value):self.rejects(value)

    def test_known_query_key_alias_still_works(self):
        self.assertEqual(_queries({'q\nueries': ['20\n26']}, 3), ['2026'])

    def test_ambiguous_key_alias_remains_rejected(self):
        with self.assertRaises(PipelineError):_queries({'queries': ['q1'], 'q\nueries': ['q2']}, 3)

    def test_fresh_and_JSON_persisted_query_results_have_identical_commands(self):
        wire = json.dumps({'queries': ['20\n26 白\n名单']}).replace('\\n', '\n')
        parsed = parse_object(wire);persisted = json.loads(json.dumps(parsed));before = deepcopy(parsed)
        self.assertEqual(_queries(parsed, 3), _queries(persisted, 3));self.assertEqual(parsed, before)

    def test_successful_previous_folded_query_is_not_redispatched_on_resume(self):
        helper = coverage_fixture.ResearchCoverageTests()
        seed = [{'kind': 'search', 'query': '20\n26', 'status': 'completed'}];before = deepcopy(seed)
        report = helper.run_case(planned=['2026', 'q2', 'q3'], seed_operations=seed)
        self.assertNotIn(('search', '2026'), helper.calls)
        self.assertEqual(seed, before);self.assertEqual(report['operations'][0]['query'], '20\n26')
        self.assertLessEqual(len(helper.calls), 6)

    def test_actual_research_dispatch_uses_single_line_commands_and_keeps_original_plan(self):
        helper = coverage_fixture.ResearchCoverageTests()
        planned = ['20\n26', '白\n名单', 'q3'];before = deepcopy(planned)
        report = helper.run_case(planned=planned)
        self.assertEqual(helper.calls[:3], [('search', '2026'), ('search', '白名单'), ('search', 'q3')])
        self.assertTrue(all('\n' not in query and '\r' not in query for _, query in helper.calls))
        self.assertEqual(report['plan']['queries'], before);self.assertEqual(planned, before)
        self.assertEqual(len(helper.calls), 6)


if __name__ == '__main__':unittest.main()
