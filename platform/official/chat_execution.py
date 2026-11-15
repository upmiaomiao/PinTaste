"""Chat execution adapter, imported by worker or explicit combined mode only."""
import asyncio
import json
from pathlib import Path
import sys
from . import store as db, memory, model_profiles
from .remote_sensing_view import RemoteSensingChannel
from .remote_sensing_identity import identity_response
CORE=Path(__file__).resolve().parent.parent/'model-chat'
if str(CORE) not in sys.path:
    sys.path.append(str(CORE))
from server import ChatRequest
import attachments
from agent_stream import run_agent
from . import document_rag, tool_registry

async def runner(row, output):
    saved = json.loads(row['input']); cfg = saved['config']
    messages = saved['request'].get('messages', [])
    latest = messages[-1] if messages else {}
    identity = identity_response(cfg.get('model_id', 'team'), latest.get('content', '')) if latest.get('role') == 'user' else None
    if identity is not None:
        # A fixed product introduction is a local policy reply, not a model call.
        yield {'type': 'content', 'text': identity}
        yield {'type': 'done', 'model': cfg['model'], 'complete': True, 'finish_reason': 'stop',
               'has_reasoning': False, 'usage': {'total_tokens': 0}, 'usage_estimated': False,
               'usage_source': 'local_identity_rule', 'model_calls': 0,
               'identity_policy': 'RS_IDENTITY_V1', 'seconds': 0.0}
        return
    body = ChatRequest(**saved['request'])
    body.max_tokens = min(output, body.max_tokens)
    if saved.get('rag_enabled'):
        body._prepared_messages, body._attachment_notices = await asyncio.to_thread(document_rag.prepare_base, body, saved.get('supports_vision', False))
    else:
        body._prepared_messages, body._attachment_notices = await asyncio.to_thread(attachments.prepare_messages, body)
    body._prepared_messages = memory.inject(body._prepared_messages, saved.get('memory_context', ''))
    rag_file_ids = (await db.run_sync(document_rag.document_file_ids, row['user_id'], saved.get('rag_file_ids', []))) if saved.get('rag_enabled') else []
    if rag_file_ids:
        from context_budget import estimated_tokens
        reserve = 2048 + (4096 if body.network_search else 0)
        used = estimated_tokens({'messages': [{'role': 'system', 'content': body.system}] + body._prepared_messages})
        budget = cfg['context_window'] - body.max_tokens - reserve - used - 512
        yield {'type': 'document_sources', 'sources': [], 'info': {'phase': 'retrieving', 'document_file_count': len(rag_file_ids)}}
        evidence = await document_rag.retrieve(row['user_id'], rag_file_ids, body.messages[-1].content, budget)
        yield {'type': 'document_sources', 'sources': evidence['sources'], 'info': evidence['info']}
        body._attachment_notices.extend(evidence['notices'])
        content = body._prepared_messages[-1]['content']
        if isinstance(content, str):
            body._prepared_messages[-1]['content'] = content + evidence['context']
        else:
            content[0]['text'] += evidence['context']
    provider = {'model': cfg['model'], 'base_url': cfg['base_url'], 'context_window': cfg['context_window']}
    provider.update({k: cfg[k] for k in ('thinking_mode', 'display_name', 'tool_call_format', 'tls_public_key_pin', 'reasoning_format') if k in cfg})
    if cfg.get('model_id') == 'b2':
        provider['preserve_tool_output_budget'] = True
    if saved.get('memory_context'): provider['trim_history'] = memory.trimmer(saved['memory_context'])
    # No tokenizer endpoint for configurable services: context is conservatively
    # bounded and the shared native Agent handles capacity errors without searches repeating.
    key = (await db.run_sync(db.secret, model_profiles.secret_name(cfg.get('model_id', 'team'))))
    if not key: raise ValueError('所选模型尚未配置 API 密钥。')
    channel = RemoteSensingChannel(cfg)
    async for raw in run_agent(body, key, provider_config=provider,
                               search_key=(await db.run_sync(db.secret, 'search')), total_token_budget=row['reserved'],
                               tool_runtime=(await db.run_sync(tool_registry.ToolRuntime, saved.get('tools'))) if body.network_search else None,
                               capture_trace=True,
                               timeout_seconds=row.get('_timeout_seconds') or (await db.run_sync(db.settings))['job_timeout']):
        for visible in channel.feed(json.loads(raw[6:])):
            yield visible
