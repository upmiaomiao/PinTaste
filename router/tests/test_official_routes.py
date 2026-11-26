"""op 175: DeepSeek official routes. Real local aiohttp, fake upstreams, synthetic keys."""
import asyncio
import importlib.util
import json
from pathlib import Path
import unittest

from aiohttp import ClientSession, ClientTimeout, FormData, web

R = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('candidate_gateway_official', R / 'dmx_model_gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
KEY = 'self_created_fixture_internal_key_' * 2
KEYS = ['self_created_fixture_primary', 'self_created_fixture_standby']
OFFICIAL_KEY = 'sk-fixture-official-key-000000'
TOOLS = [{'type': 'function', 'function': {'name': 'f', 'parameters': {'type': 'object', 'properties': {}}}}]


class OfficialRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runners, self.seen = [], []
        self.session = ClientSession(timeout=ClientTimeout(total=30))

    async def asyncTearDown(self):
        await self.session.close()
        for runner in reversed(self.runners):
            await runner.cleanup()

    async def serve(self, handler):
        app = web.Application(client_max_size=110 * 1024 * 1024)
        app.router.add_route('*', '/{path:.*}', handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.runners.append(runner)
        return 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])

    async def start(self, official_handler, dmx_handler=None, **policy):
        async def dmx(request):
            self.seen.append(('dmx', request.path, request.headers.get('Authorization'), await request.read()))
            return web.json_response({'route': 'dmx'})
        async def official(request):
            self.seen.append(('official', request.path, request.headers.get('Authorization'), await request.read()))
            return await official_handler(request)
        dmx_url = await self.serve(dmx_handler or dmx)
        official_url = await self.serve(official)
        settings = {'api_key': OFFICIAL_KEY, 'origin': official_url, 'chat': True, 'web_tools': True, 'ocr': True, **policy}
        app = gateway.create_app(KEYS, KEY, upstream=dmx_url, testing=True, official=settings)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        self.runners.append(runner)
        self.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])

    async def post(self, path, **kwargs):
        async with self.session.post(self.url + path, headers={'Authorization': 'Bearer ' + KEY}, **kwargs) as response:
            return response.status, await response.read()

    async def test_chat_model_rewritten_with_thinking_and_safety(self):
        async def handler(request):
            return web.json_response({'choices': [{'message': {'role': 'assistant', 'content': '你好'}, 'finish_reason': 'stop'}]})
        await self.start(handler)
        status, raw = await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': [], 'tools': TOOLS, 'thinking': {'type': 'disabled'}})
        self.assertEqual(status, 200)
        route, path, auth, body = self.seen[-1]
        self.assertEqual((route, path, auth), ('official', '/v1/chat/completions', 'Bearer ' + OFFICIAL_KEY))
        sent = json.loads(body)
        self.assertEqual(sent['model'], 'deepseek-flash')
        self.assertEqual(sent['thinking'], {'type': 'enabled'})
        self.assertEqual(sent['safety_check'], {'level': 'high'})
        self.assertEqual(sent['reasoning_effort'], 'high')
        self.assertEqual(json.loads(raw)['choices'][0]['message']['content'], '你好')

    async def test_planner_json_request_keeps_thinking_choice_and_drops_json_mode(self):
        async def handler(request):
            return web.json_response({'choices': [{'message': {'role': 'assistant', 'content': '{}'}, 'finish_reason': 'stop'}]})
        await self.start(handler)
        await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': [], 'max_tokens': 32768,
            'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'}})
        sent = json.loads(self.seen[-1][3])
        self.assertEqual(sent['thinking'], {'type': 'disabled'})
        self.assertNotIn('response_format', sent)
        self.assertNotIn('reasoning_effort', sent)
        self.assertEqual(sent['safety_check'], {'level': 'high'})
        self.assertEqual(sent['max_tokens'], 32768)

    async def test_json_mode_kept_when_safety_disabled(self):
        await self.start(lambda request: web.json_response({'choices': []}), safety_level=None)
        await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': [], 'response_format': {'type': 'json_object'}})
        sent = json.loads(self.seen[-1][3])
        self.assertEqual(sent['response_format'], {'type': 'json_object'})
        self.assertNotIn('safety_check', sent)

    async def test_planner_refusal_not_rewritten(self):
        async def handler(request):
            return web.json_response({'choices': [{'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'content_filter'}]})
        await self.start(handler)
        status, raw = await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': []})
        self.assertEqual(json.loads(raw)['choices'][0]['finish_reason'], 'content_filter')

    async def test_other_models_stay_on_dmx_with_dmx_key(self):
        await self.start(lambda request: web.json_response({}))
        for model in ['gemini-4-argon', 'gpt-6.1-sol', 'gpt-6.2-sol']:
            status, _ = await self.post('/v1/chat/completions', json={'model': model, 'messages': []})
            self.assertEqual(status, 200)
            route, _, auth, body = self.seen[-1]
            self.assertEqual(route, 'dmx')
            self.assertIn(auth, {'Bearer ' + k for k in KEYS})
            self.assertEqual(json.loads(body)['model'], model)

    async def test_chat_switch_off_keeps_deepseek_on_dmx(self):
        await self.start(lambda request: web.json_response({}), chat=False)
        await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': []})
        route, _, _, body = self.seen[-1]
        self.assertEqual(route, 'dmx')
        self.assertEqual(json.loads(body)['model'], 'deepseek-v4.1-flash')

    async def test_web_tools_route_official(self):
        async def handler(request):
            return web.json_response({'results': [{'title': 't', 'url': 'https://example.com'}]})
        await self.start(handler)
        status, raw = await self.post('/v1/tools/search', json={'query': '清华 x-lab', 'count': 10, 'need_content': True})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[-1][:3], ('official', '/v1/tools/search', 'Bearer ' + OFFICIAL_KEY))
        self.assertEqual(json.loads(raw)['results'][0]['title'], 't')
        status, _ = await self.post('/v1/tools/fetch', json={'url': 'https://example.com/a'})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[-1][:2], ('official', '/v1/tools/fetch'))

    async def test_web_tools_switch_off_keeps_dmx(self):
        await self.start(lambda request: web.json_response({}), web_tools=False)
        await self.post('/v1/tools/search', json={'query': 'x'})
        self.assertEqual(self.seen[-1][0], 'dmx')

    async def test_ocr_multipart_forwarded_and_leading_newlines_parsed(self):
        async def handler(request):
            response = web.StreamResponse(status=200, headers={'Content-Type': 'application/json'})
            await response.prepare(request)
            await response.write(b'\n\n')
            await response.write(json.dumps({'object': 'ocr.result', 'pages': [{'index': 0, 'text': 'ok'}]}).encode())
            await response.write_eof()
            return response
        await self.start(handler)
        form = FormData()
        form.add_field('purpose', 'rich_layout_recognition')
        form.add_field('file', b'\x89PNG fake', filename='a.png', content_type='image/png')
        status, raw = await self.post('/v1/tools/ocr', data=form)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)['pages'][0]['text'], 'ok')
        route, path, auth, body = self.seen[-1]
        self.assertEqual((route, path, auth), ('official', '/v1/tools/ocr', 'Bearer ' + OFFICIAL_KEY))
        self.assertIn(b'rich_layout_recognition', body)

    async def test_ocr_rejects_bad_fields_and_is_disabled_without_switch(self):
        await self.start(lambda request: web.json_response({}))
        form = FormData()
        form.add_field('purpose', 'other')
        form.add_field('file', b'x', filename='a.png')
        status, _ = await self.post('/v1/tools/ocr', data=form)
        self.assertEqual(status, 400)
        self.assertEqual(self.seen, [])

    async def test_ocr_path_unknown_when_switch_off(self):
        await self.start(lambda request: web.json_response({}), ocr=False)
        form = FormData()
        form.add_field('purpose', 'text_recognition')
        form.add_field('file', b'x', filename='a.png')
        status, _ = await self.post('/v1/tools/ocr', data=form)
        self.assertEqual(status, 404)
        self.assertEqual(self.seen, [])

    async def test_stream_refusal_rewritten_to_notice(self):
        async def handler(request):
            response = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            first = json.dumps({'choices': [{'index': 0, 'delta': {'content': '部分'}, 'finish_reason': None}]}, ensure_ascii=False)
            last = json.dumps({'choices': [{'index': 0, 'delta': {'content': ''}, 'finish_reason': 'content_filter'}]})
            payload = (': keep-alive\n\ndata: ' + first + '\n\ndata: ' + last + '\n\ndata: [DONE]\n\n').encode()
            for i in range(0, len(payload), 7):  # split lines across chunks
                await response.write(payload[i:i + 7])
            await response.write_eof()
            return response
        await self.start(handler)
        status, raw = await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': [], 'tools': TOOLS, 'stream': True})
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn(': keep-alive', text)
        self.assertIn('部分', text)
        self.assertIn(gateway.REFUSAL_NOTICE, text)
        self.assertNotIn('content_filter', text)
        self.assertTrue(text.rstrip().endswith('data: [DONE]'))

    async def test_nonstream_refusal_and_input_risk(self):
        calls = []
        async def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return web.json_response({'choices': [{'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'content_filter'}]})
            return web.json_response({'error': {'message': 'Content Exists Risk'}}, status=400)
        await self.start(handler)
        status, raw = await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': [], 'tools': TOOLS})
        value = json.loads(raw)
        self.assertEqual(value['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(value['choices'][0]['message']['content'], gateway.REFUSAL_NOTICE)
        status, raw = await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': []})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)['error']['code'], 'CONTENT_RISK')

    async def test_upstream_error_body_not_echoed(self):
        async def handler(request):
            return web.json_response({'error': {'message': 'secret upstream detail sk-xyz'}}, status=500)
        await self.start(handler)
        status, raw = await self.post('/v1/chat/completions', json={'model': 'deepseek-v4.1-flash', 'messages': []})
        self.assertEqual(status, 502)
        self.assertNotIn(b'secret', raw)

    async def test_health_reports_routes_without_key(self):
        await self.start(lambda request: web.json_response({}))
        async with self.session.get(self.url + '/health', headers={'Authorization': 'Bearer ' + KEY}) as response:
            raw = await response.read()
        value = json.loads(raw)
        self.assertEqual(value['official_routes'], {'chat_models': ['deepseek-v4.1-flash'], 'web_tools': True, 'ocr': True, 'safety_level': 'high'})
        self.assertNotIn(OFFICIAL_KEY.encode(), raw)

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            gateway.official_settings({'api_key': 'not-a-key'})
        with self.assertRaises(ValueError):
            gateway.official_settings({'api_key': OFFICIAL_KEY, 'origin': 'https://evil.example'})
        with self.assertRaises(ValueError):
            gateway.official_settings({'api_key': OFFICIAL_KEY, 'chat': 'yes'})
        self.assertIsNone(gateway.official_settings(None))
        same = 'sk-' + 'a' * 30
        with self.assertRaises(ValueError):
            gateway.create_app([same, 'sk-' + 'b' * 30], KEY, official={'api_key': same})


if __name__ == '__main__':
    unittest.main()
