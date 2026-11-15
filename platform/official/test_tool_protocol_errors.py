"""Safe upstream classification without network, model calls or raw-body echo."""
import asyncio
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'model-chat'))
from context_budget import upstream_problem

MISSING = '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
SAFE = '当前模型尚未配置好联网工具，请联系管理员处理。你可以关闭联网工具继续普通问答。'


class Response:
    def __init__(self, status, body, chunk_size=7):
        self.status_code, self.body, self.chunk_size = status, body.encode(), chunk_size
        self.yielded = 0

    async def aiter_bytes(self):
        for offset in range(0, len(self.body), self.chunk_size):
            self.yielded += 1
            yield self.body[offset:offset + self.chunk_size]


class ToolProtocolErrors(unittest.IsolatedAsyncioTestCase):
    async def classify(self, status, body):
        return await upstream_problem(Response(status, body))

    async def test_verified_nested_provider_configuration_error(self):
        body = json.dumps({'error': {'message': MISSING, 'type': 'BadRequestError', 'code': 400}})
        self.assertEqual(await self.classify(400, body), ('tool_configuration', SAFE, None))

    async def test_verified_top_level_message_and_trailing_period(self):
        self.assertEqual(await self.classify(400, json.dumps({'message': MISSING + '.'})), ('tool_configuration', SAFE, None))

    async def test_verified_plain_response_and_case(self):
        self.assertEqual(await self.classify(400, MISSING.upper()), ('tool_configuration', SAFE, None))

    async def test_arbitrary_prompt_text_does_not_classify_configuration(self):
        result = await self.classify(400, json.dumps({'error': {'message': 'Invalid tools'}, 'request_prompt': MISSING}))
        self.assertEqual(result[0], 'tool_parameters')
        self.assertNotIn(MISSING, result[1])

    async def test_other_tool_argument_failures_stay_distinct(self):
        for message in ['Invalid tool_choice value', 'Invalid tool_calls JSON', 'Extra tools property']:
            with self.subTest(message=message):
                self.assertEqual((await self.classify(400, json.dumps({'error': {'message': message}})))[0], 'tool_parameters')

    async def test_partial_flag_match_is_not_configuration(self):
        result = await self.classify(400, json.dumps({'message': '--tool-call-parser is not a request field'}))
        self.assertEqual(result[0], 'tool_parameters')

    async def test_non_400_does_not_invent_configuration_failure(self):
        for status in [401, 403, 422, 429, 500]:
            with self.subTest(status=status):
                self.assertEqual((await self.classify(status, json.dumps({'message': MISSING})))[0], 'upstream_error')

    async def test_real_context_recovery_contract_is_preserved(self):
        kind, _, limit = await self.classify(400, json.dumps({'error': {'message': 'maximum context length is 32768 tokens'}}))
        self.assertEqual((kind, limit), ('context_length', 32768))

    async def test_quoted_input_context_terms_cannot_override_verified_error(self):
        body = json.dumps({'error': {'message': MISSING}, 'request_prompt': 'context_length_exceeded'})
        self.assertEqual(await self.classify(400, body), ('tool_configuration', SAFE, None))

    async def test_unknown_and_malformed_errors_never_echo_body(self):
        for body in ['DO_NOT_ECHO_INPUT', '{"bad":"DO_NOT_ECHO_INPUT"', '[]', 'null']:
            with self.subTest(body=body):
                result = await self.classify(400, body)
                self.assertEqual(result[0], 'upstream_error')
                self.assertNotIn('DO_NOT_ECHO_INPUT', result[1])

    async def test_response_body_read_remains_bounded(self):
        response = Response(400, 'X' * 100000, chunk_size=4096)
        self.assertEqual((await upstream_problem(response))[0], 'upstream_error')
        self.assertEqual(response.yielded, 4)


if __name__ == '__main__':
    unittest.main()
