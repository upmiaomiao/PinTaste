"""Private model proxy: independent bounded admission for each enabled model.

Only a server-owned internal credential is accepted. No request/response content
or upstream credential is logged. Start one instance for every Host and worker.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import hmac
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web


ALLOWED_MODELS = frozenset({'deepseek-v4.1-flash', 'gpt-image-2.5-sunburst-ssvip',
    'gemini-nano-banana-2.1', 'gpt-6.1-sol', 'gemini-4-argon','gemini-4-argon-ssvip'})
GEMINI_WRITING_MODELS=frozenset({'gemini-4-argon','gemini-4-argon-ssvip'})
UPSTREAM_ORIGIN = 'https://www.dmxapi.cn'
WEB_TOOL_PATHS = frozenset({'/v1/tools/search', '/v1/tools/fetch'})
MAX_WEB_RESPONSE = 2 * 1024 * 1024

# op 175: selected traffic goes to the DeepSeek official API instead of DMX.
# Only these fixed routes may use the official origin; everything else keeps DMX.
OFFICIAL_ORIGIN = 'https://api.deepseek.com'
OFFICIAL_MODEL_MAP = {'deepseek-v4.1-flash': 'deepseek-flash'}
OCR_PATH = '/v1/tools/ocr'
OCR_PURPOSES = frozenset({'text_recognition', 'rich_layout_recognition'})
MAX_OCR_RESPONSE = 16 * 1024 * 1024
MAX_OFFICIAL_JSON_RESPONSE = 32 * 1024 * 1024
REFUSAL_NOTICE = '（这条回复未通过内容安全审核，已停止输出。请调整说法或内容后再试。）'
INPUT_RISK_NOTICE = '本次输入未通过内容安全审核，请调整内容后再试。'


def official_settings(value, testing=False):
    """Validate the optional official-route policy; None keeps every route on DMX."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Invalid official route settings')
    key = value.get('api_key')
    if not isinstance(key, str) or not key.startswith('sk-') or not 20 <= len(key) <= 256 or any(c.isspace() for c in key):
        raise ValueError('Invalid official credential')
    origin = value.get('origin', OFFICIAL_ORIGIN)
    if origin != OFFICIAL_ORIGIN and not testing:
        raise ValueError('Only the DeepSeek official origin is supported')
    flags = {name: value.get(name, False) for name in ('chat', 'web_tools', 'ocr')}
    if any(type(flag) is not bool for flag in flags.values()):
        raise ValueError('Official route switches must be booleans')
    safety = value.get('safety_level', 'high')
    if safety not in (None, 'low', 'high'):
        raise ValueError('Invalid safety level')
    effort = value.get('reasoning_effort', 'high')
    if effort not in ('low', 'high', 'max'):
        raise ValueError('Invalid reasoning effort')
    return {'api_key': key, 'origin': origin, **flags, 'safety_level': safety, 'reasoning_effort': effort}


def official_chat_body(data, policy):
    """Rewrite one chat request for the official API (verified against it 2026-11-14).

    - Client function tools (PPT chat) may be combined with safety_check only in
      thinking mode, so tool requests always enable thinking.
    - Requests without tools (reading, outline, research) keep their own thinking
      choice; safety_check only accepts response_format text. op181: a no-tool
      JSON-mode request keeps response_format and is sent WITHOUT safety_check;
      tool/chat requests keep safety_check.
    """
    body = dict(data)
    body['model'] = OFFICIAL_MODEL_MAP[data['model']]
    if data.get('tools'):
        body['thinking'] = {'type': 'enabled'}
        body['reasoning_effort'] = policy['reasoning_effort']
    fmt = body.get('response_format')
    json_mode = fmt is not None and not (isinstance(fmt, dict) and fmt.get('type') == 'text')
    if policy['safety_level']:
        if json_mode and not data.get('tools'):
            # op181 A3 (user-approved 2026-11-15): internal no-tool JSON requests (reading,
            # outline, research planning) keep JSON mode. The official API only accepts
            # safety_check with text format; without JSON mode the reader produced broken JSON
            # that voided whole paid readings. Chat/tool requests keep safety_check unchanged.
            pass
        else:
            body['safety_check'] = {'level': policy['safety_level']}
            if json_mode:
                body.pop('response_format')
    return body


def validate_ocr_form(content_type, body):
    """Exactly one purpose field and one named file part; forwarded byte-for-byte."""
    if not content_type.startswith('multipart/form-data'):
        raise ValueError('OCR requires multipart form data')
    from email.parser import BytesParser
    from email.policy import default
    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + content_type.encode('ascii') + b'\r\n\r\n' + body)
    parts = list(message.iter_parts())
    names = [p.get_param('name', header='content-disposition') for p in parts]
    if sorted(names) != ['file', 'purpose']:
        raise ValueError('Unsupported OCR fields')
    purpose = parts[names.index('purpose')].get_payload(decode=True).decode('ascii').strip()
    if purpose not in OCR_PURPOSES:
        raise ValueError('Unsupported OCR purpose')
    if not parts[names.index('file')].get_filename():
        raise ValueError('OCR file needs a filename')


def patch_refusal_chunk(value):
    """content_filter ends with an empty answer; show a fixed Chinese notice instead."""
    changed = False
    for choice in value.get('choices') or []:
        if isinstance(choice, dict) and choice.get('finish_reason') == 'content_filter':
            target = choice.get('delta') if isinstance(choice.get('delta'), dict) else choice.get('message')
            if isinstance(target, dict):
                target['content'] = (target.get('content') or '') + REFUSAL_NOTICE
            choice['finish_reason'] = 'stop'
            changed = True
    return changed


class RefusalStreamRewriter:
    """Line-buffered SSE pass-through that only rewrites content_filter chunks."""
    def __init__(self):
        self._pending = b''

    def feed(self, chunk):
        data = self._pending + chunk
        lines = data.split(b'\n')
        self._pending = lines.pop()
        return b''.join(self._line(line) + b'\n' for line in lines)

    def flush(self):
        rest, self._pending = self._pending, b''
        return self._line(rest) if rest else b''

    @staticmethod
    def _line(line):
        stripped = line.rstrip(b'\r')
        if not stripped.startswith(b'data:') or b'content_filter' not in stripped:
            return line
        payload = stripped[5:].strip()
        try:
            value = json.loads(payload)
        except (ValueError, UnicodeError):
            return line
        if not isinstance(value, dict) or not patch_refusal_chunk(value):
            return line
        return b'data: ' + json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def validate_web_tool(path, payload):
    """Only the documented, bounded read-only DMX tool bodies may be forwarded."""
    if not isinstance(payload, dict):
        raise ValueError('Invalid web tool body')
    if path == '/v1/tools/search':
        if set(payload) - {'query', 'count', 'need_content', 'ori_question', 'localization'}:
            raise ValueError('Unsupported search field')
        query = payload.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            raise ValueError('Invalid search query')
        if type(payload.get('count', 10)) is not int or not 1 <= payload.get('count', 10) <= 50:
            raise ValueError('Invalid search count')
        if type(payload.get('need_content', False)) is not bool:
            raise ValueError('Invalid content flag')
        question = payload.get('ori_question')
        if question is not None and (not isinstance(question, str) or not question.strip() or len(question) > 128):
            raise ValueError('Invalid original question')
        localization = payload.get('localization', '')
        if not isinstance(localization, str) or len(localization) > 64:
            raise ValueError('Invalid localization')
        if localization:
            ipaddress.ip_address(localization)
    elif path == '/v1/tools/fetch':
        if set(payload) != {'url'}:
            raise ValueError('Unsupported fetch field')
        url = payload['url']
        if not isinstance(url, str) or not 1 <= len(url) <= 8192 or any(ord(c) < 33 for c in url):
            raise ValueError('Invalid fetch URL')
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.').lower()
        if (parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password
                or parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)
                or host in {'localhost', 'metadata.google.internal'}
                or host.endswith(('.localhost', '.local', '.internal', '.onion'))):
            raise ValueError('Invalid public fetch URL')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host.encode('idna').decode('ascii'))
                    or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*', host)):
                raise ValueError('Invalid public fetch host') from None
        else:
            if not address.is_global:
                raise ValueError('Nonpublic fetch address')
    else:
        raise ValueError('Unsupported web tool')



class CapacityError(Exception):
    """Fixed, content-free capacity diagnostics across the HTTP boundary."""
    def __init__(self, code, stage, status=504, queue_seconds=0.):
        self.code, self.stage, self.status = code, stage, status
        self.queue_seconds = max(0., time.monotonic() - queue_seconds) if queue_seconds else 0.
        super().__init__(code)


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid ' + name)
    return value


class ModelPool:
    """Independent model FIFOs; key counters are per model, never shared caps."""
    WEB_BUCKET = '__web_tools__'

    def __init__(self, keys, per_model=200, max_queue=256, web_capacity=16, web_queue=128):
        if len(keys) != 2 or len(set(keys)) != 2 or any(not isinstance(k, str) or not k.strip() for k in keys):
            raise ValueError('Two distinct private DMX credentials are required')
        self._keys = tuple(keys)
        _integer(per_model, 1, 200, 'per-model capacity')
        _integer(max_queue, 1, 2048, 'per-model queue')
        _integer(web_capacity, 1, 200, 'web-tool capacity')
        _integer(web_queue, 1, 2048, 'web-tool queue')
        self._condition = asyncio.Condition()
        self._buckets = {name: {'capacity': per_model, 'queue_limit': max_queue,
            'active': [0, 0], 'peak': [0, 0], 'finished': [0, 0], 'queue': deque(), 'next': 0}
            for name in sorted(ALLOWED_MODELS)}
        self._buckets[self.WEB_BUCKET] = {'capacity': web_capacity, 'queue_limit': web_queue,
            'active': [0, 0], 'peak': [0, 0], 'finished': [0, 0], 'queue': deque(), 'next': 0}
        self.peak = [0, 0]
        self.writing_peak = 0

    @property
    def active(self):
        return [sum(bucket['active'][i] for bucket in self._buckets.values()) for i in (0, 1)]

    @asynccontextmanager
    async def slot(self, timeout=60, model=None):
        name = model if model in ALLOWED_MODELS else self.WEB_BUCKET
        bucket = self._buckets[name]
        ticket, index, began = object(), None, time.monotonic()
        async with self._condition:
            if len(bucket['queue']) >= bucket['queue_limit']:
                raise CapacityError('MODEL_QUEUE_FULL', 'model_queue', 429)
            bucket['queue'].append(ticket)
            try:
                async with asyncio.timeout(timeout):
                    await self._condition.wait_for(lambda: bucket['queue'][0] is ticket
                        and sum(bucket['active']) < bucket['capacity'])
                    # Least-busy allocation within this model; keys have no global
                    # 100-request cap that could couple otherwise independent models.
                    first = bucket['next']
                    index = min((first, 1-first), key=lambda i: bucket['active'][i])
                    bucket['next'] = 1-index
                    bucket['queue'].popleft()
                    bucket['active'][index] += 1
                    bucket['peak'][index] = max(bucket['peak'][index], bucket['active'][index])
                    self.peak = [max(old, active) for old, active in zip(self.peak, self.active)]
                    self.writing_peak = max(self.writing_peak,
                        sum(sum(self._buckets[name]['active']) for name in GEMINI_WRITING_MODELS))
                    self._condition.notify_all()
            except TimeoutError:
                raise CapacityError('MODEL_QUEUE_TIMEOUT', 'model_queue', queue_seconds=began) from None
            finally:
                if index is None and ticket in bucket['queue']:
                    bucket['queue'].remove(ticket)
                    self._condition.notify_all()
        try:
            yield index, self._keys[index]
        finally:
            async with self._condition:
                bucket['active'][index] -= 1
                bucket['finished'][index] += 1
                self._condition.notify_all()

    def status(self):
        def view(bucket):
            return {'capacity': bucket['capacity'], 'active': list(bucket['active']),
                'active_total': sum(bucket['active']), 'peak': list(bucket['peak']),
                'queued': len(bucket['queue']), 'queue_limit': bucket['queue_limit'],
                'finished_attempts': list(bucket['finished'])}
        models = {name: view(self._buckets[name]) for name in sorted(ALLOWED_MODELS)}
        writing = [models[name] for name in GEMINI_WRITING_MODELS]
        return {'capacity': sum(bucket['capacity'] for bucket in self._buckets.values()),
            'capacity_scope': 'sum_of_independent_model_caps_plus_web_tools_not_a_shared_pool',
            'per_key_capacity': None, 'per_key_capacity_scope': 'no_global_key_request_limit',
            'active': self.active, 'peak': list(self.peak),
            'finished_attempts': [sum(b['finished'][i] for b in self._buckets.values()) for i in (0, 1)],
            'queued': sum(len(b['queue']) for b in self._buckets.values()),
            'per_model': models, 'web_tools': view(self._buckets[self.WEB_BUCKET]),
            'writing_capacity': sum(b['capacity'] for b in writing),
            'writing_capacity_scope': 'sum_of_two_independent_models_no_extra_writing_cap',
            'writing_active': sum(b['active_total'] for b in writing),
            'writing_peak': self.writing_peak}


class BodyBudget:
    """FIFO weighted bytes before reading; waiters retain metadata, not bodies.

    Reservation stays held through parsing, model waiting, and response streaming.
    The multiplier budgets copies/parser overhead; it is not a request-count cap.
    """
    def __init__(self, byte_limit, factor=4, max_queue=2048, minimum=65536):
        self.limit = _integer(byte_limit, 1024*1024, 1024*1024*1024, 'body byte budget')
        self.factor = _integer(factor, 2, 8, 'body memory factor')
        self.max_queue = _integer(max_queue, 1, 8192, 'body wait queue')
        self.minimum = _integer(minimum, 1, 1024*1024, 'minimum body reservation')
        self.used = self.peak = self.active = self.peak_active = 0
        self._queue, self._condition = deque(), asyncio.Condition()

    @asynccontextmanager
    async def reserve(self, raw_bytes, timeout):
        weight = max(raw_bytes, self.minimum) * self.factor
        if weight > self.limit:
            raise CapacityError('MODEL_BODY_QUEUE_FULL', 'body_bytes', 429)
        ticket, acquired, began = object(), False, time.monotonic()
        async with self._condition:
            if len(self._queue) >= self.max_queue:
                raise CapacityError('MODEL_BODY_QUEUE_FULL', 'body_bytes', 429)
            self._queue.append(ticket)
            try:
                async with asyncio.timeout(timeout):
                    await self._condition.wait_for(lambda: self._queue[0] is ticket and self.used+weight <= self.limit)
                    self._queue.popleft()
                    self.used += weight
                    self.active += 1
                    self.peak = max(self.peak, self.used)
                    self.peak_active = max(self.peak_active, self.active)
                    acquired = True
                    self._condition.notify_all()
            except TimeoutError:
                raise CapacityError('MODEL_QUEUE_TIMEOUT', 'body_bytes', queue_seconds=began) from None
            finally:
                if not acquired and ticket in self._queue:
                    self._queue.remove(ticket)
                    self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self.used -= weight
                self.active -= 1
                self._condition.notify_all()

    def status(self):
        return {'limit_bytes': self.limit, 'reserved_bytes': self.used,
            'peak_reserved_bytes': self.peak, 'memory_factor': self.factor,
            'minimum_raw_reservation_bytes': self.minimum, 'active_bodies': self.active,
            'peak_active_bodies': self.peak_active, 'waiting_requests': len(self._queue),
            'waiting_limit': self.max_queue, 'unread_waiters_hold_body_reservations': False}


async def bounded_request_body(request, maximum, timeout):
    # aiohttp may have buffered a bounded protocol window before the handler;
    # it is covered by the minimum per-active-body reservation. Never call the
    # unbounded request.read() ahead of byte admission.
    data = bytearray()
    async with asyncio.timeout(timeout):
        async for chunk in request.content.iter_chunked(65536):
            if len(data)+len(chunk) > maximum:
                raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=len(data)+len(chunk))
            data.extend(chunk)
    return bytes(data)


def capacity_response(code, stage, *, status=504, queue_seconds=0., upstream_seconds=0.):
    return web.json_response({'error': {'message': 'Model admission or request did not complete',
        'code': code, 'stage': stage, 'queue_seconds': round(max(0., queue_seconds), 6),
        'upstream_seconds': round(max(0., upstream_seconds), 6)}}, status=status)


def create_app(keys, internal_key, *, upstream=UPSTREAM_ORIGIN, per_model_capacity=200, per_model_queue=256, web_capacity=16, web_queue=128, queue_timeout=60, request_timeout=900, max_body=100 * 1024 * 1024, body_budget_bytes=512 * 1024 * 1024, body_memory_factor=4, body_queue_limit=2048, body_read_timeout=60, official=None, testing=False):
    """Construct the private proxy; tests alone may replace the fixed DMX origin."""
    if upstream != UPSTREAM_ORIGIN and not testing:
        raise ValueError('Only the verified DMX CN origin is supported')
    official = official_settings(official, testing)
    if official and official['api_key'] in keys:
        raise ValueError('The official credential must differ from DMX credentials')
    if not isinstance(internal_key, str) or len(internal_key) < 32 or internal_key in keys:
        raise ValueError('A distinct internal credential is required')
    origin = urlsplit(upstream)
    if origin.username or origin.password or origin.query or origin.fragment or origin.path not in {'', '/'}:
        raise ValueError('Invalid upstream origin')
    _integer(max_body, 1, 100 * 1024 * 1024, 'maximum body')
    for name, value, high in [('queue timeout', queue_timeout, 3600), ('request timeout', request_timeout, 3600), ('body read timeout', body_read_timeout, 300)]:
        if type(value) not in (int, float) or not 0 < value <= high:
            raise ValueError('Invalid ' + name)
    pool = ModelPool(keys, per_model_capacity, per_model_queue, web_capacity, web_queue)
    bodies = BodyBudget(body_budget_bytes, body_memory_factor, body_queue_limit)
    if max_body * body_memory_factor > body_budget_bytes:
        raise ValueError('Body budget must admit one maximum-sized request')
    app = web.Application(client_max_size=max_body)
    app['pool'] = pool
    app['body_budget'] = bodies

    async def lifecycle(application):
        application['client'] = ClientSession(timeout=ClientTimeout(total=request_timeout),
            connector=TCPConnector(limit=0, limit_per_host=0, ttl_dns_cache=60), trust_env=False, auto_decompress=False)
        yield
        await application['client'].close()
    app.cleanup_ctx.append(lifecycle)

    def authorized(request):
        bearer = request.headers.get('Authorization', '')
        supplied = bearer[7:] if bearer.startswith('Bearer ') else request.headers.get('x-goog-api-key', '')
        return hmac.compare_digest(supplied, internal_key)

    async def health(request):
        if not authorized(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        state, body_state = pool.status(), bodies.status()
        routes = {'chat_models': sorted(OFFICIAL_MODEL_MAP) if official and official['chat'] else [],
            'web_tools': bool(official and official['web_tools']), 'ocr': bool(official and official['ocr']),
            'safety_level': official['safety_level'] if official else None}
        return web.json_response({'status': 'ok', **state,
            'queued': state['queued'] + body_state['waiting_requests'],
            'model_queue_queued': state['queued'], 'body_queue_queued': body_state['waiting_requests'],
            'body_budget': body_state, 'official_routes': routes})

    async def admitted_proxy(request):
        active_response = None
        model_wait = upstream_started = 0.
        if not authorized(request):
            return web.json_response({'error': {'message': 'Unauthorized'}}, status=401)
        path = request.path
        tooling = path in WEB_TOOL_PATHS
        ocr = path == OCR_PATH and bool(official and official['ocr'])
        if (tooling or ocr) and request.query:
            return web.json_response({'error': {'message': 'Web tools do not accept query parameters'}}, status=400)
        if request.query and (set(request.query) != {'alt'} or request.query.get('alt') != 'sse'):
            return web.json_response({'error': {'message': 'Unsupported query parameters'}}, status=400)
        native = re.fullmatch(r'/v1beta/models/([A-Za-z0-9._-]+):(generateContent|streamGenerateContent)', path)
        allowed = path in {'/v1/chat/completions', '/v1/images/generations', '/v1/images/edits'} or tooling or ocr
        listing = request.method == 'GET' and path == '/v1/models'
        if not listing and (request.method != 'POST' or not (allowed or native)):
            return web.json_response({'error': {'message': 'Unsupported model operation'}}, status=404)
        try:
            content_type = request.headers.get('Content-Type', '')
            try:
                body = await bounded_request_body(request, max_body, body_read_timeout)
            except TimeoutError:
                return capacity_response('MODEL_REQUEST_BODY_TIMEOUT', 'body_read', status=408)
            if tooling:
                if len(body) > 16384 or not content_type.startswith('application/json'):
                    raise ValueError('Invalid web tool request')
                validate_web_tool(path, json.loads(body))
                model = None
            elif ocr:
                validate_ocr_form(content_type, body)
                model = None
            elif listing:
                model = None
            elif native:
                model = native.group(1)
            elif content_type.startswith('multipart/form-data') and path == '/v1/images/edits':
                # Parse only the bounded model field. Forward the original
                # multipart body byte-for-byte, including image references.
                from email.parser import BytesParser
                from email.policy import default
                message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + content_type.encode('ascii') + b'\r\n\r\n' + body)
                parts = list(message.iter_parts())
                fields = [p for p in parts if p.get_param('name', header='content-disposition') == 'model']
                if len(parts) > 64 or len(fields) != 1:
                    raise ValueError('Invalid model field')
                model = fields[0].get_payload(decode=True).decode('utf-8')
                counts = [p.get_payload(decode=True).decode('ascii') for p in parts if p.get_param('name', header='content-disposition') == 'n']
                if counts and counts != ['1']:
                    raise ValueError('One output per operation is required')
            else:
                data = json.loads(body)
                if not isinstance(data, dict) or data.get('n', 1) != 1:
                    raise ValueError('Invalid model request')
                model = data.get('model')
            if not listing and not tooling and not ocr and (model not in ALLOWED_MODELS or (native and model != 'gemini-nano-banana-2.1')):
                raise ValueError('Model is not enabled')
            official_chat = bool(official and official['chat'] and path == '/v1/chat/completions'
                                 and not native and model in OFFICIAL_MODEL_MAP)
            use_official = official_chat or ocr or bool(tooling and official and official['web_tools'])
            streaming = False
            conversational = False
            if official_chat:
                streaming = data.get('stream') is True
                # Only user-facing tool conversations get the Chinese refusal notice;
                # JSON consumers keep finish_reason=content_filter and handle it.
                conversational = bool(data.get('tools'))
                body = json.dumps(official_chat_body(data, official), ensure_ascii=False).encode('utf-8')
            model_wait = time.monotonic()
            async with pool.slot(queue_timeout, model=model) as (_index, key):
                model_wait = time.monotonic() - model_wait
                upstream_started = time.monotonic()
                if use_official:
                    key = official['api_key']
                headers = {'Content-Type': content_type or 'application/json',
                    'x-goog-api-key' if native else 'Authorization': key if native else 'Bearer ' + key,
                    'Accept-Encoding': 'identity'}
                origin_url = official['origin'] if use_official else upstream
                target = origin_url.rstrip('/') + path + ('?alt=sse' if request.query else '')
                async with app['client'].request(request.method, target, data=body if not listing else None, headers=headers, allow_redirects=False) as response:
                    if not 200 <= response.status < 300:
                        # Avoid echoing upstream error bodies, request payloads,
                        # credentials or redirects into a product transcript.
                        status = response.status if response.status in {400, 401, 403, 404, 408, 413, 429} else 502
                        if use_official and response.status == 400:
                            detail = await response.content.read(65536)
                            if b'Content Exists Risk' in detail:
                                return web.json_response({'error': {'message': INPUT_RISK_NOTICE, 'code': 'CONTENT_RISK'}}, status=400)
                        return web.json_response({'error': {'message': 'Model provider did not complete the request', 'code': 'MODEL_UPSTREAM_FAILED'}}, status=status)
                    if ocr or (official_chat and not streaming):
                        limit = MAX_OCR_RESPONSE if ocr else MAX_OFFICIAL_JSON_RESPONSE
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > limit:
                                return web.json_response({'error': {'code': 'OFFICIAL_RESPONSE_TOO_LARGE'}}, status=502)
                        try:
                            value = json.loads(bytes(raw).strip() or b'null')
                            if not isinstance(value, dict):
                                raise ValueError()
                        except (ValueError, UnicodeError):
                            return web.json_response({'error': {'code': 'OFFICIAL_INVALID_RESPONSE'}}, status=502)
                        if conversational:
                            patch_refusal_chunk(value)
                        return web.json_response(value, headers={'Cache-Control': 'no-store'})
                    if tooling:
                        if response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                            return web.json_response({'error': {'code': 'WEB_INVALID_RESPONSE'}}, status=502)
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > MAX_WEB_RESPONSE:
                                return web.json_response({'error': {'code': 'WEB_RESPONSE_TOO_LARGE'}}, status=502)
                        try:
                            value = json.loads(raw)
                            if not isinstance(value, dict):
                                raise ValueError()
                        except (ValueError, UnicodeError):
                            return web.json_response({'error': {'code': 'WEB_INVALID_RESPONSE'}}, status=502)
                        return web.json_response(value, headers={'Cache-Control': 'no-store'})
                    outgoing = {'Content-Type': response.headers.get('Content-Type', 'application/json'),
                        'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}
                    if response.headers.get('Content-Encoding'):
                        outgoing['Content-Encoding'] = response.headers['Content-Encoding']
                    stream = web.StreamResponse(status=response.status, headers=outgoing)
                    active_response = stream
                    await stream.prepare(request)
                    rewriter = RefusalStreamRewriter() if conversational else None
                    async for chunk in response.content.iter_chunked(65536):
                        if rewriter is not None:
                            chunk = rewriter.feed(chunk)
                            if not chunk:
                                continue
                        await stream.write(chunk)
                    if rewriter is not None:
                        rest = rewriter.flush()
                        if rest:
                            await stream.write(rest)
                    await stream.write_eof()
                    return stream
        except TimeoutError:
            if active_response is not None and active_response.prepared:
                if request.transport is not None:
                    request.transport.close()
                raise asyncio.CancelledError() from None
            return capacity_response('MODEL_UPSTREAM_TIMEOUT', 'upstream', queue_seconds=model_wait, upstream_seconds=time.monotonic()-upstream_started if upstream_started else 0.)
        except CapacityError as error:
            return capacity_response(error.code, error.stage, status=error.status, queue_seconds=error.queue_seconds)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return web.json_response({'error': {'message': 'Invalid or unsupported model request'}}, status=400)
        except (ClientError, ConnectionError, OSError):
            if active_response is not None and active_response.prepared:
                if request.transport is not None:
                    request.transport.close()
                raise asyncio.CancelledError() from None
            return web.json_response({'error': {'message': 'Model connection unavailable'}}, status=502)

    async def proxy(request):
        # Authenticate and reject a declared oversize before any body is read.
        if not authorized(request):
            return web.json_response({'error': {'message': 'Unauthorized'}}, status=401)
        length = request.content_length
        if length is not None and length > max_body:
            raise web.HTTPRequestEntityTooLarge(max_size=max_body, actual_size=length)
        reservation = max_body if length is None else length
        try:
            async with bodies.reserve(reservation, queue_timeout):
                return await admitted_proxy(request)
        except CapacityError as error:
            return capacity_response(error.code, error.stage, status=error.status, queue_seconds=error.queue_seconds)

    app.router.add_get('/health', health)
    app.router.add_route('*', '/{path:.*}', proxy)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    options = parser.parse_args()
    config = json.loads(Path(options.config).read_text(encoding='utf-8'))
    private = json.loads(Path(config['credentials_path']).read_text(encoding='utf-8'))
    internal = Path(config['internal_key_path']).read_text(encoding='utf-8').strip()
    app = create_app([private['primary'], private['standby']], internal,
        per_model_capacity=config.get('per_model_capacity', 200), per_model_queue=config.get('per_model_queue', 256),
        web_capacity=config.get('web_capacity', 16), web_queue=config.get('web_queue', 128),
        queue_timeout=config['queue_timeout'], request_timeout=config['request_timeout'], max_body=config['max_body'],
        body_budget_bytes=config.get('body_budget_bytes', 512*1024*1024), body_memory_factor=config.get('body_memory_factor', 4),
        body_queue_limit=config.get('body_queue_limit', 2048), body_read_timeout=config.get('body_read_timeout', 60),
        official=load_official(config))
    web.run_app(app, host=config['host'], port=config['port'], access_log=None, print=None, handler_cancellation=True)


def load_official(config):
    """Optional private file named by official_path; absent keeps every route on DMX."""
    path = config.get('official_path')
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
