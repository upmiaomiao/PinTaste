"""Scalar evidence for manual-stop platform quota; never a supplier invoice.

Only one traced plain-text request may use received-output estimation. Unknown
or uncovered multi-round/tool/image consumption keeps the reservation fallback.
"""
import math

KEY = '_stop_accounting'
MAX_TOKENS = 100_000_000


def token_count(value):
    return type(value) is int and 0 <= value <= MAX_TOKENS


def collect_stop_evidence(turn, event):
    kind = event.get('type')
    if kind == 'model_request':
        state = turn.get(KEY)
        if not isinstance(state, dict) or state.get('version') != 1:
            state = {'version': 1, 'requests': 0, 'valid': True}
            turn[KEY] = state
        if type(state.get('requests')) is not int or not 0 <= state['requests'] < 64:
            state.update(requests=0, valid=False, usage_unsafe=True)
        state['requests'] += 1
        budget, payload = event.get('context_budget'), event.get('payload')
        valid = isinstance(budget, dict) and isinstance(payload, dict)
        if valid:
            input_tokens, output_limit = budget.get('input_tokens'), payload.get('max_tokens')
            messages = payload.get('messages')
            valid = (token_count(input_tokens) and input_tokens > 0
                     and token_count(output_limit) and output_limit > 0
                     and isinstance(messages, list) and bool(messages)
                     and all(isinstance(m, dict) and isinstance(m.get('content'), (str, type(None)))
                             for m in messages)
                     and not payload.get('tools')
                     and budget.get('count_method') in {'estimate', 'tokenizer'})
        state['valid'] = state['valid'] and valid
        if valid:
            state.update(input_tokens=input_tokens, output_limit=output_limit,
                         ascii_chars=0, other_chars=0)
    elif kind in {'content', 'reasoning'}:
        state = turn.get(KEY)
        text = event.get('text')
        if isinstance(state, dict) and state.get('valid') and isinstance(text, str):
            ascii_chars = sum(ord(c) < 128 for c in text)
            state['ascii_chars'] += ascii_chars
            state['other_chars'] += len(text) - ascii_chars
    elif kind == 'usage':
        state = turn.get(KEY)
        usage = event.get('usage')
        if isinstance(state, dict) and not isinstance(usage, dict):
            state.pop('usage_sequence', None)
            state['usage_unsafe'] = True
        if isinstance(state, dict) and isinstance(usage, dict):
            values = [usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
            if (all(token_count(n) for n in values) and values[0] + values[1] == values[2]
                    and type(event.get('estimated')) is bool
                    and values[2] >= state.get('usage_tokens', 0)
                    and not (state.get('usage_estimated') is True and event['estimated'] is False)
                    and (state.get('usage_sequence') in (None, state['requests'])
                         or values[2] > state.get('usage_tokens', 0))):
                state.update(usage_tokens=values[2], usage_sequence=state['requests'],
                             usage_estimated=event['estimated'])
            else:
                state.pop('usage_sequence', None)
                state['usage_unsafe'] = True
    elif kind in {'tool_plan', 'tool_start'}:
        state = turn.get(KEY)
        if isinstance(state, dict):
            state['valid'] = False


def stopped_accounting(state, reserved):
    """None means insufficient evidence, so the caller keeps old settlement."""
    if not isinstance(state, dict) or state.get('version') != 1:
        return None
    requests = state.get('requests')
    if type(requests) is not int or requests <= 0:
        return None
    if state.get('usage_unsafe') is True:
        return None
    # run_agent usage events are cumulative, not incremental. A later request
    # invalidates coverage until another complete cumulative event arrives.
    if (state.get('usage_sequence') == requests and token_count(state.get('usage_tokens'))
            and type(state.get('usage_estimated')) is bool):
        estimated = state['usage_estimated']
        if not estimated and state['usage_tokens'] > reserved:
            return None
        return {'tokens': min(reserved, state['usage_tokens']), 'estimated': estimated,
                'basis': 'stopped_round_budget_estimate' if estimated else 'stopped_reported_usage'}
    if requests != 1 or state.get('valid') is not True:
        return None
    fields = [state.get(k) for k in ('input_tokens', 'output_limit', 'ascii_chars', 'other_chars')]
    if not all(token_count(n) for n in fields) or fields[0] <= 0 or fields[1] <= 0:
        return None
    # Same conservative text ratio used by context_budget, without duplicating
    # prompt, answer, tool payloads or image bytes in the persisted turn.
    output = math.ceil(fields[2] / 3 + fields[3] * 1.5)
    if output == 0:
        return None
    return {'tokens': min(reserved, fields[0] + min(fields[1], output)), 'estimated': True,
            'basis': 'stopped_observed_estimate'}
