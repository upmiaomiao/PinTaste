"""Collectron Streamable HTTP client, shared by the two independent runtimes.

Only the administrator chooses the endpoint. Search references are resolved by
the calling runtime; external text never becomes tool instructions. Keep this
file identical to model-chat/wechat_mcp.py when packaging either application.
"""
import asyncio
from datetime import timedelta
import ipaddress
import json
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

DEFAULT_ENDPOINT = 'http://collectron-wechat-search:8077/mcp'
TIME_RANGES = ['day', 'week', 'month', 'year', 'two_years', 'any']
LABELS = {'wechat_search': '微信公众号搜索', 'wechat_get_article': '公众号正文读取'}
TOOLS = [
    {'type': 'function', 'function': {'name': 'wechat_search',
     'description': '检索微信公众号公开文章，补充行业动态、实践案例、技术解读及 PPT 背景资料。与网页搜索结合；政策和论文结论需核对原始来源。默认最近两年。搜索返回摘要和 reference_id，需要正文时尽快调用 wechat_get_article。',
     'parameters': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'description': '简洁的主题关键词，不传私人材料或整段对话'},
         'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'default': 10},
         'max_pages': {'type': 'integer', 'minimum': 1, 'maximum': 3, 'default': 3},
         'time_range': {'type': 'string', 'enum': TIME_RANGES, 'default': 'two_years'},
         'sort': {'type': 'string', 'enum': ['recent', 'relevance'], 'default': 'recent'}},
         'required': ['query'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {'name': 'wechat_get_article',
     'description': '读取本轮公众号搜索实际返回的 reference_id 对应正文。引用会过期，应尽快读取。next_offset 非空时继续分页；截断或仅仅读取片段不能称为已阅读全文。',
     'parameters': {'type': 'object', 'properties': {
         'reference_id': {'type': 'string', 'minLength': 20, 'maxLength': 256},
         'offset': {'type': 'integer', 'minimum': 0, 'maximum': 1000000, 'default': 0},
         'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 12000, 'default': 12000}},
         'required': ['reference_id'], 'additionalProperties': False}}},
]
_state_lock = threading.Lock()
_cooldowns = {}
_active = set()


class WechatError(ValueError):
    def __init__(self, code, message, retry_after_s=0):
        super().__init__(message)
        self.code = code
        self.retry_after_s = retry_after_s


def endpoint(config=None):
    value = (config or {}).get('WUYU_WECHAT_MCP_URL') or os.getenv('WUYU_WECHAT_MCP_URL', DEFAULT_ENDPOINT)
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WechatError('wechat_config_invalid', '公众号 MCP 地址配置无效。')
    return value


def enabled(config=None):
    value = (config or {}).get('WUYU_WECHAT_ENABLED', os.getenv('WUYU_WECHAT_ENABLED', 'true'))
    return str(value).lower() not in {'false', '0', 'off'}


def _retry(value):
    if isinstance(value, dict):
        number = value.get('retry_after_s')
        if type(number) in (int, float) and math.isfinite(number) and number > 0:
            return min(86400, math.ceil(number))
        return max((_retry(v) for v in value.values()), default=0)
    if isinstance(value, str):
        found = re.search(r'retry_after_s[\s\"\':=]+(\d+(?:\.\d+)?)', value)
        if found:
            return min(86400, max(1, math.ceil(float(found[1]))))
    return 0


def _data(result):
    value = result.structuredContent
    if value is None:
        for block in result.content:
            if getattr(block, 'type', None) == 'text':
                try:
                    value = json.loads(block.text)
                except (ValueError, TypeError):
                    if result.isError:
                        value = {'error': block.text}
                if isinstance(value, dict):
                    break
    if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False)) > 2 * 1024 * 1024:
        raise WechatError('wechat_invalid_response', '公众号服务返回格式异常。')
    delay = _retry(value)
    # A paged search may return useful items together with a partial error.
    if result.isError or (value.get('error') and not value.get('items')) or (delay and not value.get('items')):
        raise WechatError('wechat_cooldown' if delay else 'wechat_provider_failed',
                          '公众号服务正在冷却，请稍后重试。' if delay else '公众号服务未完成查询，已有资料仍可使用。', delay)
    return value


async def call_wechat(name, arguments, config=None):
    """Initialize, discover tools, call once; never retry a timed-out search."""
    if not enabled(config):
        raise WechatError('wechat_disabled', '管理员已关闭微信公众号搜索。')
    if name not in LABELS:
        raise WechatError('wechat_invalid_tool', '未开放此公众号工具。')
    address = endpoint(config)
    with _state_lock:
        delay = max(0, math.ceil(_cooldowns.get(address, 0) - time.monotonic()))
        if delay:
            raise WechatError('wechat_cooldown', '公众号服务正在冷却，请稍后重试。', delay)
        if address in _active:
            raise WechatError('wechat_busy', '公众号服务正在处理查询，请稍后重试。', 2)
        _active.add(address)

    async def dispatch():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        headers = {}
        token = (config or {}).get('WUYU_WECHAT_MCP_TOKEN') or os.getenv('WUYU_WECHAT_MCP_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer ' + token
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=12), trust_env=False,
                                     follow_redirects=False, headers=headers) as http:
            async with streamable_http_client(address, http_client=http) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    if name not in {tool.name for tool in listing.tools}:
                        raise WechatError('wechat_tool_missing', '公众号服务缺少所需工具，请检查部署。')
                    return _data(await session.call_tool(name, arguments))
    try:
        value = await asyncio.wait_for(dispatch(), timeout=190)
        delay = _retry(value)
        if delay:
            with _state_lock:
                _cooldowns[address] = time.monotonic() + delay
        return value
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # AnyIO task groups can wrap the original MCP/HTTP exception.
        def find(exc):
            if isinstance(exc, WechatError):
                return exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                raw = exc.response.headers.get('Retry-After', '30')
                return WechatError('wechat_cooldown', '公众号服务正在冷却，请稍后重试。', int(raw) if raw.isdecimal() else 30)
            for child in getattr(exc, 'exceptions', ()):
                found = find(child)
                if found:
                    return found
        safe = find(error) or WechatError('wechat_unavailable', '公众号搜索连接超时或暂不可用，已有资料仍可使用。')
        if safe.retry_after_s:
            with _state_lock:
                _cooldowns[address] = time.monotonic() + safe.retry_after_s
        raise safe from None
    finally:
        with _state_lock:
            _active.discard(address)


def _public_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').rstrip('.').lower()
        if parsed.scheme not in {'https', 'http'} or not host or parsed.username or parsed.password:
            return None
        try:
            return value if ipaddress.ip_address(host).is_global else None
        except ValueError:
            return value if '.' in host and not host.endswith(('.local', '.internal', '.localhost')) else None
    except ValueError:
        return None


def _metadata(item):
    url = _public_url(item.get('original_url')) or _public_url(item.get('url'))
    reference = item.get('reference_id')
    if not url or not isinstance(reference, str) or not 20 <= len(reference) <= 256:
        return None
    return {'title': str(item.get('title') or '公众号文章')[:300], 'url': url,
            'account': str(item.get('account') or '')[:160],
            'date': str(item.get('published_at') or '')[:80], 'reference_id': reference,
            'reference_expires_at': str(item.get('reference_expires_at') or '')[:80],
            'type': 'wechat', 'source_kind': 'wechat', 'untrusted': True, 'authors': []}


def search_result(value, max_results=10):
    if not isinstance(value.get('items'), list):
        raise WechatError('wechat_invalid_response', '公众号服务没有返回有效的文章列表。')
    sources, seen = [], set()
    for item in value['items'][:max_results]:
        source = _metadata(item) if isinstance(item, dict) else None
        if source and source['reference_id'] not in seen and source['url'] not in seen:
            seen.update((source['reference_id'], source['url']))
            sources.append(source | {'snippet': str(item.get('snippet') or '')[:1800], 'content_kind': 'snippet'})
    return {'ok': True, 'sources': sources, 'returned_count': len(sources),
            'has_more': bool(value.get('has_more')), 'truncated': bool(value.get('truncated')),
            'partial': bool(value.get('error')), 'retry_after_s': _retry(value),
            'note': '实时公众号索引摘要，默认最近两年；未读取正文。' + ('本次结果不完整。' if value.get('error') else '')}


def article_result(value, source, offset=0, max_chars=12000):
    text = value.get('text')
    if value.get('reference_id') != source['reference_id'] or not isinstance(text, str) or not text.strip() or value.get('body_status', 'text') != 'text':
        raise WechatError('wechat_article_unavailable', '公众号正文未取得或引用已过期，请重新搜索；不能称为已读全文。')
    if value.get('offset', offset) != offset:
        raise WechatError('wechat_invalid_response', '公众号正文返回的偏移不匹配。')
    next_offset = value.get('next_offset')
    if next_offset is not None and (type(next_offset) is not int or next_offset <= offset or next_offset > 1000000):
        raise WechatError('wechat_invalid_response', '公众号正文分页偏移无效。')
    truncated = bool(value.get('truncated') or value.get('content_truncated') or next_offset is not None or offset or len(text) > max_chars)
    text = text[:max_chars]
    updated = source | {'title': str(value.get('title') or source.get('title') or '公众号文章')[:300],
                        'account': str(value.get('account') or source.get('account') or '')[:160],
                        'date': str(value.get('published_at') or source.get('date') or '')[:80],
                        'snippet': text[:1800], 'content_kind': 'excerpt' if truncated else 'full_text',
                        'truncated': truncated, 'offset': offset, 'next_offset': next_offset,
                        'content_truncated': bool(value.get('content_truncated'))}
    return {'ok': True, 'sources': [updated], 'data': text, 'offset': offset,
            'next_offset': next_offset, 'truncated': truncated,
            'note': '正文片段；有后续偏移或截断时不能称为已阅读全文。' if truncated else '已取得此篇正文；外部资料仍需核验。'}
