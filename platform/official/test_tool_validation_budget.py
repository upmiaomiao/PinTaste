"""Tool validation/admission budget boundary; actual validator, fake provider IO."""
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from official import tool_registry as tools


class ToolValidationBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.item = tools.ToolConfig(id='web_search', label='Self-created web tool',
                                     kind='builtin', description='Self-created fixture').model_dump(exclude={'api_key'})
        self.catalog_items = [deepcopy(self.item)]
        self.search = AsyncMock(return_value={'ok': True, 'sources': []})
        self.http = AsyncMock(return_value={'ok': True, 'sources': []})
        for patcher in [patch.object(tools, 'catalog', lambda: {'tools': self.catalog_items}),
                        patch.object(tools, 'search', self.search),
                        patch.object(tools, 'http_call', self.http),
                        patch.object(tools, 'register_sources', lambda result, registry: result),
                        patch.object(tools, 'compact_tool_result', lambda *args: 'Self-created tool result'),
                        patch.object(tools.db, 'settings', lambda: {'search_enabled': True})]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = tools.ToolRuntime([self.item], test_mode=True)

    async def execute(self, arguments, *, name='web_search', raw=False, runtime=None):
        call = {'id': 'self-created-call', 'function': {'name': name, 'arguments': arguments if raw else json.dumps(arguments)}}
        events = [e async for e in (runtime or self.runtime).execute(call, [], 0)]
        return next(e for e in events if e['type'] == 'tool_result')

    async def rejected_without_spending(self, arguments, **kwargs):
        result = await self.execute(arguments, **kwargs)
        self.assertFalse(result['ok'])
        self.assertEqual(self.runtime.count, 0)
        self.search.assert_not_awaited()
        self.http.assert_not_awaited()

    async def test_missing_schema_fields_do_not_consume_budget(self):
        await self.rejected_without_spending({})

    async def test_wrong_schema_type_does_not_consume_budget(self):
        await self.rejected_without_spending({'query': 7})

    async def test_empty_query_does_not_consume_budget(self):
        await self.rejected_without_spending({'query': ''})

    async def test_whitespace_query_failing_builtin_validation_costs_zero(self):
        await self.rejected_without_spending({'query': '   '})

    async def test_bilingual_query_semantics_are_validated_before_cost_two(self):
        await self.rejected_without_spending({'query_zh': 'English only', 'query_en': '中文'})

    async def test_malformed_JSON_does_not_consume_budget(self):
        await self.rejected_without_spending('{"query":', raw=True)

    async def test_unknown_tool_does_not_consume_budget(self):
        await self.rejected_without_spending({'query': 'self-created'}, name='unknown_fixture')

    async def test_valid_single_query_still_costs_one(self):
        result = await self.execute({'query': 'self-created query'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.runtime.count, 1)
        self.assertEqual(self.search.await_count, 1)

    async def test_valid_bilingual_query_still_costs_two(self):
        result = await self.execute({'query_zh': '自制查询', 'query_en': 'self-created query'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.runtime.count, 2)
        self.assertEqual(self.search.await_count, 2)

    async def test_valid_provider_failure_retains_execution_cost(self):
        self.search.side_effect = RuntimeError('self-created provider failure')
        result = await self.execute({'query': 'self-created query'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.runtime.count, 1)
        self.assertEqual(self.search.await_count, 1)

    async def test_cached_valid_call_keeps_original_budget_policy(self):
        await self.execute({'query': 'self-created query'})
        await self.execute({'query': 'self-created query'})
        self.assertEqual(self.runtime.count, 2)
        self.assertEqual(self.search.await_count, 1)

    async def test_exhausted_budget_blocks_valid_provider_dispatch(self):
        self.runtime.count = self.runtime.max_calls
        result = await self.execute({'query': 'self-created query'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.runtime.count, self.runtime.max_calls)
        self.search.assert_not_awaited()

    async def test_disabled_tool_remains_blocked_without_budget(self):
        self.catalog_items[0]['enabled'] = False
        self.runtime.test_mode = False
        await self.rejected_without_spending({'query': 'self-created query'})

    async def test_changed_catalog_remains_blocked_without_budget(self):
        self.catalog_items[0]['label'] = 'Changed fixture'
        await self.rejected_without_spending({'query': 'self-created query'})

    async def test_custom_HTTP_schema_is_validated_before_budget(self):
        item = tools.ToolConfig(id='self_http', label='Self-created HTTP tool',
                                description='Self-created fixture', endpoint='https://example.org/fixture').model_dump(exclude={'api_key'})
        self.catalog_items[:] = [item]
        self.runtime = tools.ToolRuntime([item], test_mode=True)
        await self.rejected_without_spending({'query': 7}, name='self_http')
        result = await self.execute({'query': 'self-created query'}, name='self_http')
        self.assertTrue(result['ok'])
        self.assertEqual(self.runtime.count, 1)
        self.http.assert_awaited_once()

    async def test_corrected_arguments_can_use_budget_after_invalid_attempt(self):
        await self.execute({'query': '   '})
        result = await self.execute({'query': 'self-created corrected query'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.runtime.count, 1)
        self.search.assert_awaited_once()

    async def test_bilingual_budget_remains_atomic_at_last_slot(self):
        self.runtime.count = self.runtime.max_calls - 1
        result = await self.execute({'query_zh': '自制查询', 'query_en': 'self-created query'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.runtime.count, self.runtime.max_calls - 1)
        self.search.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
