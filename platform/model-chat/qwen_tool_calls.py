"""Strict adapter for Qwen XML calls when an upstream lacks a tool parser."""
import json
import re

START = '<tool_call>'
CALL = re.compile(r'<tool_call>\s*<function=([a-z][a-z0-9_]{0,47})>\s*(.*?)\s*</function>\s*</tool_call>', re.S)
PARAMETER = re.compile(r'<parameter=([A-Za-z_][A-Za-z0-9_]*)>\s*([\s\S]*?)\s*</parameter>')


class QwenToolText:
    """Keep a split opening tag and call markup out of the visible answer."""
    def __init__(self):
        self.pending = ''
        self.prefix = ''
        self.tool = None
        self.literal = False

    def feed(self, text):
        if self.literal:
            return text
        self.pending += text
        if self.tool is True:
            return ''
        position = self.pending.find(START)
        if position >= 0:
            preamble = self.prefix + self.pending[:position]
            if len(preamble) > 600 or any(char in preamble for char in ('`', '<', '>')):
                self.literal = True
                visible, self.pending = self.pending, ''
                return visible
            self.tool = True
            visible, self.pending = self.pending[:position], self.pending[position:]
            return visible
        # Hold a split opening marker, while allowing a short model preamble.
        held = max((size for size in range(1, len(START)) if self.pending.endswith(START[:size])), default=0)
        visible = self.pending[:-held] if held else self.pending
        self.pending = self.pending[-held:] if held else ''
        self.prefix += visible
        if len(self.prefix) > 600 or any(char in self.prefix for char in ('`', '<', '>')):
            self.literal = True
        return visible

    def finish(self):
        if self.tool is True:
            return ''
        visible, self.pending = self.pending, ''
        return visible


def complete_calls(text, tools, round_index):
    """Accept closed calls after a short plain preamble, for configured tools only.

    This adapter is explicitly selected per provider. Fenced examples, markup,
    trailing prose, incomplete calls and unknown tools never execute.
    """
    text = text.strip()
    position = text.find(START)
    if position < 0:
        return []
    preamble = text[:position]
    if len(preamble) > 600 or any(char in preamble for char in ('`', '<', '>')):
        return []
    text = text[position:]
    if len(text) > 50000:
        raise ValueError('Tool document too long')
    configured = {item['function']['name']: item['function']['parameters'] for item in tools}
    calls, position = [], 0
    for match in CALL.finditer(text):
        if text[position:match.start()].strip() or len(calls) >= 12:
            raise ValueError('Invalid tool document')
        name, raw = match.groups()
        if name not in configured:
            raise ValueError('Tool is not configured')
        schema = configured[name]
        properties = schema.get('properties', {})
        arguments, parameter_end = {}, 0
        for parameter in PARAMETER.finditer(raw):
            if raw[parameter_end:parameter.start()].strip():
                raise ValueError('Invalid tool parameter document')
            key, value = parameter.groups()
            if key in arguments or key not in properties:
                raise ValueError('Unknown or duplicate tool parameter')
            expected = properties[key].get('type', 'string')
            arguments[key] = value if expected == 'string' else json.loads(value)
            parameter_end = parameter.end()
        if raw[parameter_end:].strip() or any(key not in arguments for key in schema.get('required', [])):
            raise ValueError('Incomplete tool parameters')
        encoded = json.dumps(arguments, ensure_ascii=False)
        if len(encoded) > 5000:
            raise ValueError('Tool arguments too long')
        calls.append({'id': f'call_xml_{round_index}_{len(calls)}', 'type': 'function',
                      'function': {'name': name, 'arguments': encoded}})
        position = match.end()
    if not calls or text[position:].strip():
        raise ValueError('Unclosed or mixed tool document')
    return calls
