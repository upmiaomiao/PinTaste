"""Exact same-delta dual-channel display fix; all upstream IO is synthetic."""
import ast
import asyncio
import copy
import importlib.util
import json
import re
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent


def extract(path, names, scope):
    nodes = []
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        assigned = {target.id for target in node.targets if isinstance(target, ast.Name)} if isinstance(node, ast.Assign) else set()
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names) or assigned & names:
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), scope)
    return scope


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class Response:
    def __init__(self, frames, *, pause=None):
        self.status_code = 200
        self.frames = frames
        self.pause = pause
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def aiter_lines(self):
        for frame in self.frames:
            if isinstance(frame, BaseException):
                raise frame
            if frame == 'pause':
                await self.pause.wait()
                continue
            yield 'data: ' + (frame if isinstance(frame, str) else json.dumps(frame))
            yield ''


class Client:
    def __init__(self, responses):
        self.responses = responses
        self.payloads = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def stream(self, method, url, *, headers, json):
        assert method == 'POST' and url == 'https://fixture.invalid/v1/chat/completions'
        self.payloads.append(copy.deepcopy(json))
        return self.responses.pop(0)

    async def post(self, *_args, **_kwargs):
        raise AssertionError('No real HTTP/tokenizer allowed')


def frame(delta, finish=None):
    return {'model': 'self-stream87', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}


def finish():
    return [{'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}, '[DONE]']


class Runtime:
    def __init__(self, tools):
        self.tools = tools
        self.items = {'web_search': {}, 'scholar_search': {}}
        self.count = 0

    @property
    def can_call(self):
        return self.count < 8

    def instructions(self):
        return 'SELF FIXTURE TOOL INSTRUCTIONS'

    async def execute(self, call, registry, round_index):
        self.count += 1
        yield {'type': '_tool_message', 'message': {'role': 'tool', 'tool_call_id': call['id'], 'content': '{"ok":true,"sources":[]}'}}


def runtime(responses, root=ROOT, *, network=False):
    stream = module(root / 'streaming.py', 'self_stream87_' + str(id(responses)))
    context = module(root / 'context_budget.py', 'self_context87_' + str(id(responses)))
    client = Client(responses)
    scope = {'asyncio': asyncio, 'copy': copy, 'json': json, 're': re, 'time': time,
             'datetime': datetime, 'timedelta': timedelta, 'timezone': timezone}
    extract(root / 'search_tools.py', {'ToolCallAccumulator', 'TOOLS', 'TOOL_LABELS', 'MAX_SEARCH_CALLS', 'MAX_TOOL_ROUNDS', 'search_window', 'tool_instructions'}, scope)
    scope.update({'event': stream.event, 'sse_data': stream.sse_data, 'ThinkSplitter': stream.ThinkSplitter,
                  'fit_context': context.fit_context, 'compact_tool_result': context.compact_tool_result,
                  'ContextCapacityError': context.ContextCapacityError, 'upstream_problem': context.upstream_problem,
                  'log_rejection': context.log_rejection, 'model_client_options': lambda _: {}, 'PROVIDERS': {},
                  'extract_citations': lambda *_: {'sources': []},
                  'httpx': SimpleNamespace(AsyncClient=lambda **_: client, Timeout=httpx.Timeout,
                                           HTTPError=httpx.HTTPError, TimeoutException=httpx.TimeoutException)})
    if hasattr(stream, 'same_delta_think_duplicate'):
        scope['same_delta_think_duplicate'] = stream.same_delta_think_duplicate
    extract(root / 'agent_stream.py', {'needs_verified_search', 'run_agent'}, scope)
    body = SimpleNamespace(system='SELF SYSTEM', provider='baseline', thinking=True, network_search=network,
                           messages=[SimpleNamespace(content='self fixture question')], temperature=.2, max_tokens=2048,
                           _prepared_messages=[{'role': 'user', 'content': 'self fixture question'}], _attachment_notices=[])
    provider = {'model': 'self-stream87', 'base_url': 'https://fixture.invalid/v1', 'context_window': 32768,
                'thinking_mode': 'chat_template', 'tool_call_format': 'native', 'reasoning_format': 'auto'}
    iterator = scope['run_agent'](body, 'self-fixture-key', provider_config=provider,
                                  tool_runtime=Runtime(scope['TOOLS']) if network else None, capture_trace=True)
    return iterator, client, stream


async def collect(responses, root=ROOT, *, network=False):
    iterator, client, _ = runtime(responses, root, network=network)
    return [json.loads(item[6:]) async for item in iterator], client


def text(events, kind):
    return ''.join(event['text'] for event in events if event['type'] == kind)


class StreamChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_delta_exact_duplicate_only_once_display_raw_and_usage_unchanged(self):
        for field in ('reasoning', 'reasoning_content'):
            events, _ = await collect([Response([frame({field: '甲乙', 'content': '<think>甲乙</think>答案'}, 'stop'), *finish()])])
            self.assertEqual(text(events, 'reasoning'), '甲乙')
            self.assertEqual(text(events, 'content'), '答案')
            raw = next(event for event in events if event['type'] == 'model_response')
            self.assertEqual(raw['reasoning'], '甲乙甲乙')
            self.assertEqual(raw['content'], '答案')
            self.assertEqual(events[-1]['usage'], {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30})
            self.assertEqual(events[-1]['reasoning_sources'], [field, 'think_tag'])

    async def test_different_channels_are_both_immediately_preserved(self):
        events, _ = await collect([Response([frame({'reasoning_content': '原生A', 'content': '<think>标签B</think>正文'}, 'stop'), *finish()])])
        self.assertEqual(text(events, 'reasoning'), '原生A标签B')
        self.assertEqual([event['text'] for event in events if event['type'] == 'reasoning'], ['原生A', '标签B'])
        self.assertEqual(text(events, 'content'), '正文')

    async def test_answer_same_as_native_is_never_removed(self):
        for content in ('相同文本', '<think>相同文本</think>相同文本', '正文中的 <think>相同文本</think> 是代码'):
            events, _ = await collect([Response([frame({'reasoning_content': '相同文本', 'content': content}, 'stop'), *finish()])])
            self.assertEqual(text(events, 'reasoning'), '相同文本')
            self.assertEqual(text(events, 'content'), '相同文本' if content.startswith('<think>') else content)

    async def test_native_legal_repeats_and_repeats_inside_a_thought_are_preserved(self):
        events, _ = await collect([Response([frame({'reasoning': '再核对'}), frame({'reasoning': '再核对', 'content': '答案'}, 'stop'), *finish()])])
        self.assertEqual(text(events, 'reasoning'), '再核对再核对')
        events, _ = await collect([Response([frame({'reasoning': '重复重复', 'content': '<think>重复重复</think>答案'}, 'stop'), *finish()])])
        self.assertEqual(text(events, 'reasoning'), '重复重复')

    async def test_no_normalization_no_prefix_or_substring_deduplication(self):
        for native, tagged in [('A', ' A'), ('A', 'A\n'), ('AB', 'A'), ('A', 'AB'), ('é', 'e\u0301')]:
            events, _ = await collect([Response([frame({'reasoning': native, 'content': '<think>' + tagged + '</think>答案'}, 'stop'), *finish()])])
            self.assertEqual(text(events, 'reasoning'), native + tagged)

    async def test_split_native_and_tag_before_after_remain_outside_fix(self):
        thought = '分块思考'; wrapped = '<think>' + thought + '</think>答案'
        for width in range(1, len(wrapped)):
            chunks = [frame({'reasoning': thought})] + [frame({'content': wrapped[index:index + width]}) for index in range(0, len(wrapped), width)]
            chunks[-1]['choices'][0]['finish_reason'] = 'stop'
            events, _ = await collect([Response(chunks + finish())])
            self.assertEqual(text(events, 'reasoning'), thought + thought)
            self.assertEqual(text(events, 'content'), '答案')
        events, _ = await collect([Response([frame({'content': wrapped}), frame({'reasoning_content': thought}, 'stop'), *finish()])])
        self.assertEqual(text(events, 'reasoning'), thought + thought)

    async def test_partial_tag_and_length_finalize_preserve_partial_reasoning(self):
        for ending in ('未完', '未完</thi'):
            events, _ = await collect([Response([frame({'reasoning_content': '原生', 'content': '<think>' + ending}, 'length'), *finish()])])
            self.assertEqual(text(events, 'reasoning'), '原生' + ending)
            self.assertFalse(events[-1]['complete'])

    async def test_native_alias_selection_and_implicit_prefix_contract_are_unchanged(self):
        events, _ = await collect([Response([frame({'reasoning': '首别名', 'reasoning_content': '次别名', 'content': '答案'}, 'stop'), *finish()])])
        self.assertEqual(text(events, 'reasoning'), '首别名')
        events, _ = await collect([Response([frame({'reasoning_content': '原生', 'content': '无开标签</think>正文'}, 'stop'), *finish()])])
        self.assertEqual(text(events, 'reasoning'), '原生')
        self.assertEqual(text(events, 'content'), '无开标签</think>正文')

    async def test_model_error_network_error_and_timeout_do_not_buffer_different_content(self):
        for failure in ({'error': {'message': 'self fixture upstream failure'}}, httpx.ReadError('self fixture read'), httpx.ReadTimeout('self fixture timeout')):
            response = Response([frame({'reasoning_content': '原生A', 'content': '<think>不同B</think>正文'}), failure])
            events, client = await collect([response])
            self.assertEqual(text(events, 'reasoning'), '原生A不同B')
            self.assertEqual(text(events, 'content'), '正文')
            self.assertEqual(events[-1]['type'], 'error')
            self.assertTrue(client.closed and response.closed)

    async def test_generator_exit_each_visible_prefix_has_no_deferred_secondary(self):
        for stop_after in (1, 2, 3):
            response = Response([frame({'reasoning_content': '原生A', 'content': '<think>不同B</think>正文'}), 'pause'], pause=asyncio.Event())
            iterator, client, _ = runtime([response]); events = []
            while sum(event['type'] in ('reasoning', 'content') for event in events) < stop_after:
                events.append(json.loads((await anext(iterator))[6:]))
            await iterator.aclose()
            expected = ('原生A', '原生A不同B', '原生A不同B')[stop_after - 1]
            self.assertEqual(text(events, 'reasoning'), expected)
            self.assertTrue(client.closed and response.closed)

    async def test_outer_cancel_keeps_already_emitted_different_channels(self):
        response = Response([frame({'reasoning_content': '原生A', 'content': '<think>不同B</think>正文'}), 'pause'], pause=asyncio.Event())
        iterator, client, _ = runtime([response]); events = []
        while text(events, 'content') != '正文':
            events.append(json.loads((await anext(iterator))[6:]))
        pending = asyncio.create_task(anext(iterator)); await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(text(events, 'reasoning'), '原生A不同B')
        self.assertTrue(client.closed and response.closed)

    async def test_tool_round_metadata_and_continuation_payload_are_unchanged(self):
        call = {'index': 0, 'id': 'self-call', 'type': 'function', 'function': {'name': 'web_search', 'arguments': '{"query":"self fixture"}'}}
        events, client = await collect([Response([frame({'reasoning': '甲', 'content': '<think>甲</think>', 'tool_calls': [call]}, 'tool_calls'), *finish()]), Response([frame({'reasoning_content': '乙', 'content': '<think>乙</think>答案'}, 'stop'), *finish()])], network=True)
        self.assertEqual([(event['round'], event['text']) for event in events if event['type'] == 'reasoning'], [(1, '甲'), (2, '乙')])
        self.assertEqual(text(events, 'content'), '答案')
        self.assertEqual([event['reasoning'] for event in events if event['type'] == 'model_response'], ['甲甲', '乙乙'])
        self.assertFalse(any('reasoning' in message or 'reasoning_content' in message for message in client.payloads[1]['messages']))
        self.assertEqual(events[-1]['model_calls'], 2)
        self.assertEqual(events[-1]['usage']['completion_tokens'], 40)

    def test_every_existing_tag_boundary_and_plain_text_regression(self):
        stream = module(ROOT / 'streaming.py', 'self87_parser')
        full = ' \n<think>先计算17×23。</think>391。'
        for width in range(1, len(full) + 1):
            parser = stream.ThinkSplitter(); events = []
            for index in range(0, len(full), width):
                events.extend(parser.feed(full[index:index + width]))
            events.extend(parser.finish())
            self.assertEqual(''.join(t for kind, t in events if kind == 'reasoning'), '先计算17×23。')
            self.assertEqual(''.join(t for kind, t in events if kind == 'content'), '391。')
        parser = stream.ThinkSplitter()
        self.assertEqual(parser.feed('代码中的 <think> 是标签。') + parser.finish(), [('content', '代码中的 <think> 是标签。')])


if __name__ == '__main__':
    unittest.main()
