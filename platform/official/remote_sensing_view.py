"""Public presentation metadata for the remote-sensing model only.

Bind annotations to the images actually submitted to the model, including
inherited follow-up images. Never infer this binding from a conversation's
current attachments, image names, or model-generated identifiers.
"""
import json
import re
import xml.etree.ElementTree as ET

from . import store as db

RS_PROFILE_ID = 'wuyu_remote_sensing'
FORMAT_MARKER = 'RS_VIEW_V1'
FILE_ID = re.compile(r'^[a-f0-9]{32}$')
MAX_XML_CHARS = 131072


def recover_reasoning_answer(reasoning, answer, meta):
    """Recognize a complete RS result misrouted into the reasoning channel."""
    if (not isinstance(meta, dict) or meta.get('complete') is not False
            or meta.get('finish_reason') != 'stop' or not isinstance(answer, str)
            or answer.strip() or not isinstance(reasoning, str)
            or len(reasoning) > MAX_XML_CHARS or meta.get('search_calls', 0)):
        return None
    text = reasoning.strip()
    fenced = re.fullmatch(r'(`{3,})xml[ \t]*\r?\n([\s\S]*?)\r?\n\1', text, re.I)
    if fenced:
        text = fenced.group(2).strip()
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        return None
    try:
        root = ET.fromstring('<rs>' + text + '</rs>')
    except (ET.ParseError, ValueError, UnicodeError):
        return None
    if (root.text and root.text.strip()) or any(child.tail and child.tail.strip() for child in root):
        return None
    if any(child.tag not in ('thinking', 'answer') for child in root):
        return None
    answers = root.findall('answer')
    if len(answers) != 1 or len(list(root.iter('answer'))) != 1:
        return None
    result = answers[0]
    for tag in ('decision', 'conclusion'):
        fields = result.findall(tag)
        if len(fields) != 1 or len(fields[0]) or not (fields[0].text or '').strip():
            return None
    result.tail = None
    return ET.tostring(result, encoding='unicode')


class RemoteSensingChannel:
    """Normalize this profile's final channel without changing provider traces."""
    def __init__(self, config):
        self.enabled = isinstance(config, dict) and config.get('model_id') == RS_PROFILE_ID
        self.reasoning = ''
        self.answer = ''
        self.denied = False
        self.emitted = False

    def feed(self, event):
        if not self.enabled:
            return [event]
        kind = event.get('type')
        if kind in ('error', 'tool_plan', 'tool_start', 'tool_result'):
            self.denied = True
        if kind == 'model_response' and event.get('tool_calls'):
            self.denied = True
        if kind in ('reasoning', 'content') and isinstance(event.get('text'), str):
            field = 'reasoning' if kind == 'reasoning' else 'answer'
            current = getattr(self, field)
            if len(current) + len(event['text']) > MAX_XML_CHARS:
                self.denied = True
            elif not self.denied:
                setattr(self, field, current + event['text'])
        if kind == 'done' and not self.denied and not self.emitted:
            answer = recover_reasoning_answer(self.reasoning, self.answer, event)
            if answer:
                self.emitted = True
                self.answer = answer
                return [{'type': 'content', 'text': answer, 'rs_channel_normalized': True},
                        {**event, 'complete': True, 'rs_channel_normalized': True, 'original_complete': False}]
        return [event]


def _saved_input(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def enrich_turn(turn, row):
    """Return an RS presentation copy; leave stored answers and status intact."""
    saved = _saved_input(row.get('input'))
    config = saved.get('config')
    if not isinstance(config, dict) or config.get('model_id') != RS_PROFILE_ID:
        return turn

    result = dict(turn)
    # These are derived fields, never authoritative stored turn data.
    result['visualFiles'] = []
    result.pop('remoteSensingFormatVersion', None)
    result.pop('remoteSensingImageIndexBase', None)
    result.pop('remoteSensingRecoveredAnswer', None)
    if (row.get('status') == 'incomplete' and not turn.get('toolCalls')
            and recover_reasoning_answer(turn.get('reasoning'), turn.get('answer'), turn.get('meta'))):
        result['remoteSensingRecoveredAnswer'] = True
    request = saved.get('request')
    if not isinstance(request, dict):
        return result
    system = request.get('system')
    if isinstance(system, str) and FORMAT_MARKER in system:
        result['remoteSensingFormatVersion'] = 'rs-view-v1'
        result['remoteSensingImageIndexBase'] = 1

    messages = request.get('messages')
    if not isinstance(messages, list) or not messages:
        return result
    latest = messages[-1]
    if not isinstance(latest, dict) or latest.get('role') != 'user':
        return result
    ids = latest.get('file_ids')
    owner = row.get('user_id')
    if not isinstance(ids, list) or len(ids) > 5 or not isinstance(owner, str) or not owner:
        return result

    # Imported lazily: app_main configures the upload directory at startup.
    import attachments
    seen = set()
    for fid in ids:
        if not isinstance(fid, str) or not FILE_ID.fullmatch(fid) or fid in seen:
            continue
        seen.add(fid)
        if not db.query('SELECT id FROM files WHERE id=? AND user_id=?', (fid, owner), one=True):
            continue
        try:
            item = attachments.read_file(fid)
        except (ValueError, OSError):
            # Removed historical files must not prevent reading the answer.
            continue
        if not isinstance(item, dict) or item.get('id') != fid or item.get('kind') != 'image':
            continue
        public = attachments.public_file(item)
        # A canonical same-origin preview path avoids trusting stored URLs.
        public['preview_url'] = '/api/files/' + fid + '/preview'
        result['visualFiles'].append(public)
    return result
