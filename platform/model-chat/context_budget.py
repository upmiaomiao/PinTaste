"""Fit evidence into the deployed context window without re-running searches."""
import copy
import json
import logging
import math
import re
from urllib.parse import urlsplit

import httpx

DEFAULT_CONTEXT = 32768
SAFETY_TOKENS = 512
IMAGE_TOKEN_RESERVE = 6144  # Uploads are bounded to a 2048-pixel longest edge.
logger = logging.getLogger('model_chat.context')


class ContextCapacityError(ValueError):
    pass


def compact_tool_result(name, batches):
    """Full search results go to the UI; the model receives each source once."""
    summaries, sources = [], {}
    for batch in batches:
        summaries.append({k: v for k, v in batch.items() if k in {
            'language', 'query', 'ok', 'error', 'date_window', 'date_policy',
            'note', 'requested_count', 'returned_count', 'excluded_by_date', 'cached',
            'provider', 'error_code', 'retry_after_s'}})
        summaries[-1]['source_ids'] = [s['source_id'] for s in batch.get('sources', [])]
        for source in batch.get('sources', []):
            evidence = {k: v for k, v in source.items() if k in {
                'source_id', 'title', 'url', 'snippet', 'date', 'authors', 'type', 'year_status',
                'account', 'reference_id', 'content_kind', 'truncated', 'next_offset', 'provider'}}
            # A prompt body read enriches the same citation rather than leaving
            # its earlier index snippet as the model's only evidence.
            if batch.get('data') and source.get('content_kind') in {'excerpt', 'full_text'}:
                evidence['snippet'] = batch['data'][:6000]
                if len(batch['data']) > 6000:
                    evidence['excerpt_truncated'] = True
            if source['source_id'] not in sources or source.get('content_kind') in {'excerpt', 'full_text'}:
                sources[source['source_id']] = evidence
    return json.dumps({'tool': name, 'bilingual_results': summaries,
                       'sources': list(sources.values())}, ensure_ascii=False, separators=(',', ':'))


def estimated_tokens(payload):
    """Conservative fallback only; the team endpoint uses its own tokenizer."""
    images = 0
    messages = copy.deepcopy(payload['messages'])
    for message in messages:
        if isinstance(message.get('content'), list):
            images += sum(part.get('type') == 'image_url' for part in message['content'])
            message['content'] = [part for part in message['content'] if part.get('type') != 'image_url']
    text = json.dumps({'messages': messages, 'tools': payload.get('tools', [])}, ensure_ascii=False)
    ascii_chars = sum(ord(c) < 128 for c in text)
    return math.ceil(ascii_chars / 3 + (len(text) - ascii_chars) * 1.5) + images * IMAGE_TOKEN_RESERVE + 256


async def measure_context(client, provider, key, payload):
    # On the deployed service, /tokenize before the first image chat can leave
    # an invalid multimodal cache entry: later chats return HTTP 500 / zero tokens.
    # Never send image-bearing turns (including historical images) to that route.
    if any(isinstance(m.get('content'), list) and any(
            p.get('type') == 'image_url' for p in m['content']) for m in payload['messages']):
        return estimated_tokens(payload), provider.get('context_window', DEFAULT_CONTEXT), 'multimodal_estimate'
    # This route was verified on the deployed vLLM service. Other providers use
    # the conservative estimate and the bounded context-error recovery below.
    if provider.get('tokenize_url'):
        try:
            response = await client.post(provider['tokenize_url'],
                headers={'Authorization': 'Bearer ' + key}, timeout=10,
                json={k: v for k, v in payload.items() if k in {
                    'model', 'messages', 'tools', 'chat_template_kwargs'}} | {'add_generation_prompt': True})
            if response.status_code == 200:
                data = response.json()
                count, limit = data.get('count'), data.get('max_model_len')
                if isinstance(count, int) and count > 0 and isinstance(limit, int) and limit >= 2048:
                    return count, limit, 'tokenizer'
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
    return estimated_tokens(payload), provider.get('context_window', DEFAULT_CONTEXT), 'estimate'


def trim_evidence(messages, snippet_limit):
    changed = False
    seen = set()
    for message in messages:
        if message['role'] != 'tool':
            continue
        try:
            data = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get('sources'), list):
            continue
        # Custom query tools return bounded text in `data`. Keep full records in
        # the UI, but progressively shorten this excerpt alongside search snippets.
        data_limit = snippet_limit * 4
        if isinstance(data.get('data'), str) and len(data['data']) > data_limit:
            data['data'] = data['data'][:data_limit] + '…'
            data['data_truncated_for_context'] = True
            changed = True
        unique = []
        for source in data['sources']:
            if not isinstance(source, dict):
                continue
            identity = (source.get('source_id'), source.get('snippet'))
            if identity in seen:
                changed = True
                continue
            seen.add(identity)
            source = dict(source)
            for field, maximum in (('snippet', snippet_limit), ('title', 180)):
                if isinstance(source.get(field), str) and len(source[field]) > maximum:
                    source[field] = source[field][:maximum] + '…'
                    source['excerpt_truncated'] = True
                    changed = True
            # Citation IDs resolve to the original URL in the app. Very long
            # tracking URLs add no useful evidence to the model context.
            if len(source.get('url', '')) > 400:
                source.pop('url')
                changed = True
            unique.append(source)
        data['sources'] = unique
        message['content'] = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return changed


def drop_oldest_pair(messages):
    # Only remove complete user/assistant history before the current question.
    users = [i for i, m in enumerate(messages) if m['role'] == 'user']
    if len(users) < 2:
        return False
    first, second = users[:2]
    if second != first + 2 or messages[first + 1]['role'] != 'assistant' or messages[first + 1].get('tool_calls'):
        return False
    del messages[first:second]
    return True


def evidence_catalog(messages):
    """Collect original excerpts before progressive trimming, once per global ID."""
    sources = {}
    for message in messages:
        if message.get('role') != 'tool':
            continue
        try:
            value = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        for source in value.get('sources', []) if isinstance(value, dict) else []:
            if not isinstance(source, dict) or not source.get('source_id'):
                continue
            previous = sources.get(source['source_id'])
            if previous is None or source.get('content_kind') in {'excerpt', 'full_text'}:
                sources[source['source_id']] = copy.deepcopy(source)
    return sources


def compact_evidence_catalog(messages, sources, title_limit, excerpt_budget):
    """Share one bounded catalog; full source records stay in the job/UI.

    Preserve every citation ID and source type. Prioritize excerpts across web,
    scholarly and WeChat evidence instead of letting the largest provider crowd
    out the others. URLs resolve through IDs in the app and need not be repeated
    in every model message.
    """
    documents = []
    for message in messages:
        if message.get('role') != 'tool':
            continue
        try:
            value = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get('sources'), list):
            documents.append((message, value))
    if not documents or not sources:
        return False
    excerpts = {}
    groups = [[s for s in sources.values() if s.get('type', 'webpage') == kind]
              for kind in ('wechat', 'scholar', 'webpage')]
    groups[0].sort(key=lambda s: s.get('content_kind') not in {'excerpt', 'full_text'})
    for index in range(max(map(len, groups), default=0)):
        for group in groups:
            if index >= len(group) or excerpt_budget <= 0:
                continue
            source = group[index]
            maximum = 600 if source.get('content_kind') in {'excerpt', 'full_text'} else 200
            excerpt = str(source.get('snippet') or '')[:min(maximum, excerpt_budget)]
            if excerpt:
                excerpts[source['source_id']] = excerpt
                excerpt_budget -= len(excerpt)
    catalog = []
    for sid, source in sources.items():
        item = {'source_id': sid, 'type': source.get('type', 'webpage'),
                'title': str(source.get('title') or '')[:title_limit]}
        try:
            item['host'] = urlsplit(source.get('url', '')).hostname or ''
        except ValueError:
            pass
        if source.get('date'):
            item['date'] = str(source['date'])[:32]
        if source.get('type') == 'wechat':
            item.update({k: source[k] for k in ('reference_id', 'content_kind', 'next_offset', 'truncated') if k in source})
            if source.get('account'):
                item['account'] = str(source['account'])[:80]
        elif source.get('type') == 'scholar' and source.get('authors'):
            item['authors'] = [str(x)[:60] for x in source['authors'][:3]]
        if sid in excerpts:
            item['snippet'] = excerpts[sid]
            if len(excerpts[sid]) < len(str(source.get('snippet') or '')):
                item['context_excerpt_truncated'] = True
        catalog.append(item)
    for index, (message, value) in enumerate(documents):
        value['sources'] = catalog if index == len(documents) - 1 else []
        value['context_source_catalog'] = '所有来源编号和简要目录合并在最后一条工具结果中；仅部分来源保留摘要，完整记录在界面。不能仅凭标题推断具体结论。'
        message['content'] = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return True


async def fit_context(client, provider, key, payload, *, recovery=False, reported_limit=None):
    payload = copy.deepcopy(payload)
    messages = payload['messages']
    original_evidence = evidence_catalog(messages)
    changes = set()
    for message in messages:
        if message.get('tool_calls'):
            # Keep tool arguments and results. Prior reasoning remains in the UI
            # and need not occupy the next model request.
            message.pop('reasoning', None)
            message.pop('reasoning_content', None)
    if recovery:
        if trim_evidence(messages, 240):
            changes.add('evidence')
        while drop_oldest_pair(messages):
            changes.add('history')
    requested_output = payload['max_tokens']
    margin = 2048 if recovery else SAFETY_TOKENS

    async def measure():
        count, limit, method = await measure_context(client, provider, key, payload)
        if reported_limit:
            limit = min(limit, reported_limit)
        return count, limit, method

    count, limit, method = await measure()
    # First shorten excerpts, then retire old dialogue, keeping the current
    # question and all source IDs intact. Never truncate arbitrary JSON/history.
    for snippet_limit in (900, 500):
        if count + requested_output + margin <= limit:
            break
        if trim_evidence(messages, snippet_limit):
            changes.add('evidence')
            count, limit, method = await measure()
    while count + requested_output + margin > limit and drop_oldest_pair(messages):
        changes.add('history')
        count, limit, method = await measure()
    # The formal platform supplies a request-local callback that removes only
    # its own structured memory prefixes. Legacy/custom clients remain intact.
    trim_memory = provider.get('trim_history')
    while count + requested_output + margin > limit and trim_memory and trim_memory(messages):
        changes.add('memory')
        count, limit, method = await measure()
    for snippet_limit in (240, 100):
        if count + requested_output + margin <= limit:
            break
        if trim_evidence(messages, snippet_limit):
            changes.add('evidence')
            count, limit, method = await measure()
    # Many 50-result searches can exceed capacity through metadata alone.
    # Keep all IDs and balance the available excerpts across providers, while
    # leaving immutable full records available to the source panels.
    # B2's deployed 32K window must share space between tools and its requested
    # reasoning/answer budget. Prefer a bounded source catalog before reducing
    # that output; all source IDs and full UI records remain available.
    catalog_output = requested_output if provider.get('preserve_tool_output_budget') else min(4096, requested_output)
    for title_limit, excerpt_budget in ((80, 4000), (40, 2000), (20, 800)):
        if limit - count - margin >= catalog_output:
            break
        if compact_evidence_catalog(messages, original_evidence, title_limit, excerpt_budget):
            changes.add('evidence')
            count, limit, method = await measure()
    available = limit - count - margin
    if available < min(1024, requested_output):
        raise ContextCapacityError('本轮问题、附件和必要证据仍超过模型上下文容量。请拆分附件或缩小问题范围后重试；已取得的检索记录已保留。')
    payload['max_tokens'] = min(requested_output, available)
    if payload['max_tokens'] < requested_output:
        changes.add('output')
    return payload, {'input_tokens': count, 'context_window': limit,
                     'max_output_tokens': payload['max_tokens'], 'count_method': method,
                     'adjustments': sorted(changes), 'recovery': recovery}


async def upstream_problem(response):
    # Never forward or log raw upstream bodies: they can contain prompt text.
    raw = bytearray()
    async for block in response.aiter_bytes():
        raw.extend(block[:16384 - len(raw)])
        if len(raw) >= 16384:
            break
    text = raw.decode('utf-8', errors='replace').lower()
    # Recognise only the verified service-configuration rejection. Never echo
    # upstream prose, infer tool support, or retry with different parameters.
    try:
        problem = json.loads(text)
        if isinstance(problem, dict) and isinstance(problem.get('error'), dict):
            problem = problem['error']
        message = problem.get('message') if isinstance(problem, dict) else None
    except (ValueError, TypeError):
        message = text.strip()
    missing_auto_tools = '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set'
    if response.status_code == 400 and message in (missing_auto_tools, missing_auto_tools + '.'):
        return 'tool_configuration', '当前模型尚未配置好联网工具，请联系管理员处理。你可以关闭联网工具继续普通问答。', None
    context = any(term in text for term in ('maximum context length', 'context_length_exceeded',
        'context window', 'max_model_len', 'too many tokens', 'input_tokens'))
    match = re.search(r'maximum context length\D{0,20}(\d{4,7})', text)
    limit = int(match[1]) if match else None
    if context:
        return 'context_length', '问题、附件和检索结果超过模型上下文容量，请拆分问题或减少携带的历史。', limit
    if response.status_code == 400 and any(term in text for term in (
        'enable-auto-tool-choice', 'tool-call-parser', 'tool_choice', 'tool_calls', 'tools')):
        return 'tool_parameters', '模型服务拒绝了工具调用参数，请检查服务端工具协议配置。', None
    return 'upstream_error', {401: '模型接口认证失败，请检查本机凭据。',
        403: '模型接口拒绝访问。', 429: '模型接口繁忙或额度受限，请稍后再试。'}.get(
        response.status_code, f'模型接口返回错误（HTTP {response.status_code}），请检查服务状态或请求参数。'), None


def log_rejection(status, kind, round_index, search_calls, budget):
    logger.warning('upstream_rejection status=%s kind=%s round=%s searches=%s input_tokens=%s output_tokens=%s context=%s',
        status, kind, round_index + 1, search_calls, budget['input_tokens'],
        budget['max_output_tokens'], budget['context_window'])
